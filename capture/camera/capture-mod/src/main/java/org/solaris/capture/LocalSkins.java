package org.solaris.capture;

import com.google.gson.JsonElement;
import com.google.gson.JsonObject;
import com.google.gson.JsonParser;
import com.mojang.authlib.GameProfile;
import com.mojang.authlib.properties.Property;
import com.mojang.blaze3d.platform.NativeImage;
import java.io.IOException;
import java.net.URI;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.nio.file.Paths;
import java.security.MessageDigest;
import java.security.NoSuchAlgorithmException;
import java.util.ArrayList;
import java.util.Base64;
import java.util.Collection;
import java.util.HexFormat;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Locale;
import java.util.Map;
import java.util.Optional;
import java.util.TreeMap;
import java.util.concurrent.CompletableFuture;
import java.util.concurrent.ConcurrentHashMap;
import java.util.concurrent.atomic.AtomicLong;
import java.util.regex.Pattern;
import net.minecraft.Util;
import net.minecraft.client.Minecraft;
import net.minecraft.client.renderer.texture.DynamicTexture;
import net.minecraft.client.resources.DefaultPlayerSkin;
import net.minecraft.client.resources.PlayerSkin;
import net.minecraft.resources.ResourceLocation;

/**
 * Fully local player skins (no Mojang signature, no download).
 *
 * <p>The 1.21 client only renders another player's skin when its {@code textures} profile
 * property is Mojang-signed and points to textures.minecraft.net ({@code PlayerInfo#createSkinLookup}
 * drops insecure skins, authlib's {@code unpackTextures} drops other domains). MirrorBot's local skin
 * mode instead sets an UNSIGNED property whose payload names a skin of the local pool:
 *
 * <pre>
 * {"timestamp": .., "profileId": "..", "profileName": "Alpha", "signatureRequired": false,
 *  "textures": {"SKIN": {"url": "http://solaris.local/skins/proc_0042.png", "metadata": {"model": "slim"}}},
 *  "solaris": {"v": 1, "skin_id": "proc_0042", "file": "pool/proc_0042.png", "sha256": "<png sha256>", "model": "slim"}}
 * </pre>
 *
 * <p>{@code SkinManagerMixin} hooks {@code SkinManager#getOrLoad(GameProfile)} (used by
 * {@code PlayerInfo#createSkinLookup}, {@code lookupInsecure}, {@code getInsecureSkin}): for such a
 * profile it returns a {@link PlayerSkin} whose texture is decoded from {@code <skins_dir>/<skin_id>.png}
 * (64x64 skins only; the base layer is made opaque, as the game does for player skins) and registered as
 * a {@link DynamicTexture}, with the payload's
 * model and {@code secure = true}. No {@code HttpTexture} is involved, so there is no code path that
 * opens the URL or writes to the pool: a missing / unreadable file gives the default skin. Every
 * other profile (signed Mojang skins) keeps the vanilla path.
 *
 * <p>getOrLoad also runs every frame for player heads ({@code getInsecureSkin}), so the property
 * parse is memoised per property value and the counters are cheap atomics.
 */
public final class LocalSkins {
    public static final String HOST = "solaris.local";
    private static final Pattern SAFE_ID = Pattern.compile("[a-z0-9_.\\-]{1,64}");

    private record Ref(String skinId, String file, String sha256, PlayerSkin.Model model, String url) {
        String key() {
            return skinId + "|" + model.id() + "|" + (sha256 == null ? "" : sha256);
        }
    }

    /** Per-skin diagnostics for the camera meta. */
    private static final class Stat {
        final String skinId;
        final String model;
        String path;
        String status = "loading"; // loading | loaded | missing | invalid
        String sha256Expected;
        String sha256File;
        long requests;
        double loadedEpochMs = -1;

        Stat(Ref ref) {
            this.skinId = ref.skinId();
            this.model = ref.model() == PlayerSkin.Model.SLIM ? "slim" : "classic";
            this.sha256Expected = ref.sha256();
        }
    }

    private static final Map<String, CompletableFuture<PlayerSkin>> CACHE = new ConcurrentHashMap<>();
    private static final Map<String, Stat> STATS = new ConcurrentHashMap<>();
    /** Parse memo per textures property value (empty = not a local-pool property). */
    private static final Map<String, Optional<Ref>> PARSED = new ConcurrentHashMap<>();
    private static final int PARSED_MAX = 4096;
    /** getOrLoad calls that went to the vanilla path (per call, may be per frame with heads in view). */
    private static final AtomicLong nonLocalLookups = new AtomicLong();
    /** Distinct non-local textures property values seen (i.e. distinct Mojang / vanilla skins). */
    private static final AtomicLong nonLocalTextures = new AtomicLong();

    private LocalSkins() {
    }

    public static boolean enabled() {
        CaptureConfig cfg = SolarisCapture.config();
        return cfg.loaded && cfg.local_skins;
    }

    public static Path skinsDir() {
        CaptureConfig cfg = SolarisCapture.config();
        return cfg.skins_dir == null || cfg.skins_dir.isEmpty() ? null : Paths.get(cfg.skins_dir).toAbsolutePath();
    }

    /**
     * Skin future for a profile with a local-pool textures property, or null (vanilla path).
     * Called from SkinManager#getOrLoad (render thread).
     */
    public static CompletableFuture<PlayerSkin> lookup(GameProfile profile) {
        if (!enabled() || profile == null) {
            return null;
        }
        Collection<Property> props = profile.getProperties().get("textures");
        if (props == null || props.isEmpty()) {
            return null;
        }
        Ref ref = parseCached(props.iterator().next());
        if (ref == null) {
            nonLocalLookups.incrementAndGet();
            return null;
        }
        Stat st = STATS.computeIfAbsent(ref.key(), k -> new Stat(ref));
        synchronized (st) {
            st.requests++;
        }
        return CACHE.computeIfAbsent(ref.key(), k -> load(ref, st));
    }

    /** {@link #parse} memoised per property value (the value is immutable for a given skin). */
    private static Ref parseCached(Property prop) {
        if (prop == null || prop.value() == null) {
            return null;
        }
        String value = prop.value();
        Optional<Ref> hit = PARSED.get(value);
        if (hit == null) {
            if (PARSED.size() >= PARSED_MAX) {
                PARSED.clear(); // bounded; only a handful of distinct skins per session in practice
            }
            Optional<Ref> parsed = Optional.ofNullable(parse(prop));
            hit = PARSED.putIfAbsent(value, parsed);
            if (hit == null) {
                hit = parsed;
                if (parsed.isEmpty()) {
                    nonLocalTextures.incrementAndGet();
                }
            }
        }
        return hit.orElse(null);
    }

    /** Parses a textures property; null unless it names a skin of the local pool. */
    private static Ref parse(Property prop) {
        if (prop == null || prop.value() == null) {
            return null;
        }
        try {
            String json = new String(Base64.getDecoder().decode(prop.value()), StandardCharsets.UTF_8);
            JsonObject root = JsonParser.parseString(json).getAsJsonObject();
            JsonObject sol = root.has("solaris") && root.get("solaris").isJsonObject()
                    ? root.getAsJsonObject("solaris") : null;
            String url = null;
            String modelName = null;
            if (root.has("textures") && root.get("textures").isJsonObject()) {
                JsonObject tex = root.getAsJsonObject("textures");
                if (tex.has("SKIN") && tex.get("SKIN").isJsonObject()) {
                    JsonObject skin = tex.getAsJsonObject("SKIN");
                    url = str(skin.get("url"));
                    if (skin.has("metadata") && skin.get("metadata").isJsonObject()) {
                        modelName = str(skin.getAsJsonObject("metadata").get("model"));
                    }
                }
            }
            boolean localUrl = false;
            String urlId = null;
            if (url != null) {
                URI uri = URI.create(url);
                localUrl = HOST.equalsIgnoreCase(uri.getHost());
                String path = uri.getPath() == null ? "" : uri.getPath();
                String last = path.substring(path.lastIndexOf('/') + 1);
                urlId = last.toLowerCase(Locale.ROOT).endsWith(".png") ? last.substring(0, last.length() - 4) : last;
            }
            if (sol == null && !localUrl) {
                return null; // a normal (Mojang) texture: vanilla path
            }
            String skinId = sol != null && str(sol.get("skin_id")) != null ? str(sol.get("skin_id")) : urlId;
            if (skinId == null) {
                return null;
            }
            skinId = skinId.toLowerCase(Locale.ROOT);
            if (!SAFE_ID.matcher(skinId).matches()) {
                SolarisCapture.LOGGER.warn("[solaris-capture] local skin id {} rejected", skinId);
                return null;
            }
            if (sol != null && str(sol.get("model")) != null) {
                modelName = str(sol.get("model"));
            }
            PlayerSkin.Model model = PlayerSkin.Model.byName(modelName);
            String file = sol == null ? null : str(sol.get("file"));
            String sha = sol == null ? null : str(sol.get("sha256"));
            if (url == null) {
                url = "http://" + HOST + "/skins/" + skinId + ".png";
            }
            return new Ref(skinId, file, sha == null ? null : sha.toLowerCase(Locale.ROOT), model, url);
        } catch (RuntimeException e) {
            return null;
        }
    }

    private static String str(JsonElement e) {
        return e == null || e.isJsonNull() || !e.isJsonPrimitive() ? null : e.getAsString();
    }

    /** Candidate files: <skins_dir>/<id>.png, <skins_dir>/<file name>, <skins_dir>/../<file>. */
    private static Path resolve(Ref ref) {
        Path dir = skinsDir();
        if (dir == null) {
            return null;
        }
        List<Path> cands = new ArrayList<>();
        cands.add(dir.resolve(ref.skinId() + ".png"));
        String file = ref.file();
        if (file != null && !file.contains("..") && !file.contains(":") && !file.startsWith("/") && !file.startsWith("\\")) {
            Path fp = Paths.get(file);
            cands.add(dir.resolve(fp.getFileName().toString()));
            if (dir.getParent() != null) {
                cands.add(dir.getParent().resolve(fp));
            }
        }
        if (dir.getParent() != null) {
            cands.add(dir.getParent().resolve(ref.skinId() + ".png")); // top-level legacy skins
        }
        for (Path p : cands) {
            if (Files.isRegularFile(p)) {
                return p;
            }
        }
        return null;
    }

    private static CompletableFuture<PlayerSkin> load(Ref ref, Stat st) {
        Minecraft mc = Minecraft.getInstance();
        CompletableFuture<PlayerSkin> out = new CompletableFuture<>();
        PlayerSkin fallback = new PlayerSkin(DefaultPlayerSkin.getDefaultTexture(), null, null, null, ref.model(), true);
        CompletableFuture.runAsync(() -> {
            Path file = resolve(ref);
            if (file == null) {
                synchronized (st) {
                    st.status = "missing";
                }
                SolarisCapture.LOGGER.warn("[solaris-capture] local skin {} not found under {}; default skin",
                        ref.skinId(), skinsDir());
                out.complete(fallback);
                return;
            }
            byte[] bytes;
            NativeImage processed;
            try {
                bytes = Files.readAllBytes(file);
                NativeImage img = NativeImage.read(bytes);
                int w = img.getWidth();
                int h = img.getHeight();
                if (w != 64 || h != 64) {
                    img.close();
                    throw new IOException("unexpected size " + w + "x" + h + " (local skins must be 64x64)");
                }
                processed = opaqueBaseLayer(img);
            } catch (IOException | RuntimeException e) {
                synchronized (st) {
                    st.status = "invalid";
                    st.path = file.toString();
                }
                SolarisCapture.LOGGER.warn("[solaris-capture] local skin {} ({}) unreadable: {}; default skin",
                        ref.skinId(), file, e.toString());
                out.complete(fallback);
                return;
            }
            String sha = sha256(bytes);
            synchronized (st) {
                st.path = file.toString();
                st.sha256File = sha;
            }
            if (ref.sha256() != null && !ref.sha256().equals(sha)) {
                SolarisCapture.LOGGER.warn("[solaris-capture] local skin {}: file sha256 {} != server {} (rendering the local file)",
                        ref.skinId(), sha, ref.sha256());
            }
            String tail = sha.substring(0, 12);
            ResourceLocation loc = ResourceLocation.fromNamespaceAndPath(SolarisCapture.MOD_ID,
                    "skins/" + ref.skinId() + "_" + tail);
            // textureUrl stays null: nothing downstream may treat this skin as downloadable
            PlayerSkin skin = new PlayerSkin(loc, null, null, null, ref.model(), true);
            mc.execute(() -> {
                try {
                    // In-memory texture from the bytes decoded above (render thread: uploads now).
                    // Never an HttpTexture, so no URL is opened and the pool file is never written.
                    mc.getTextureManager().register(loc, new DynamicTexture(processed));
                    synchronized (st) {
                        st.status = "loaded";
                        st.loadedEpochMs = EpochClock.nowMs();
                    }
                    out.complete(skin);
                    SolarisCapture.LOGGER.info("[solaris-capture] local skin {} ({}) <- {}", ref.skinId(),
                            ref.model() == PlayerSkin.Model.SLIM ? "slim" : "classic", file);
                } catch (RuntimeException e) {
                    processed.close();
                    synchronized (st) {
                        st.status = "invalid";
                    }
                    SolarisCapture.LOGGER.warn("[solaris-capture] local skin {} register failed: {}", ref.skinId(), e.toString());
                    out.complete(fallback);
                }
            });
        }, Util.backgroundExecutor());
        return out;
    }

    /** Base-layer rectangles {x0, y0, x1, y1} of the 64x64 skin layout: head; body, right arm and right leg; left arm and left leg. */
    private static final int[][] BASE_LAYER = {{0, 0, 32, 16}, {0, 16, 64, 32}, {16, 48, 48, 64}};

    /**
     * The game draws the base layer of a player skin fully opaque (only the outer layer may be transparent), so a
     * local skin gets alpha 255 on every base-layer pixel before it is registered. Input: a 64x64 skin.
     */
    private static NativeImage opaqueBaseLayer(NativeImage img) {
        for (int[] r : BASE_LAYER) {
            for (int y = r[1]; y < r[3]; y++) {
                for (int x = r[0]; x < r[2]; x++) {
                    img.setPixelRGBA(x, y, img.getPixelRGBA(x, y) | 0xFF000000);
                }
            }
        }
        return img;
    }

    private static String sha256(byte[] bytes) {
        try {
            return HexFormat.of().formatHex(MessageDigest.getInstance("SHA-256").digest(bytes));
        } catch (NoSuchAlgorithmException e) {
            throw new IllegalStateException(e);
        }
    }

    /** Camera meta {@code local_skins}. */
    public static Map<String, Object> meta() {
        Map<String, Object> m = new LinkedHashMap<>();
        m.put("enabled", enabled());
        Path dir = skinsDir();
        m.put("skins_dir", dir == null ? null : dir.toString().replace('\\', '/'));
        m.put("non_local_lookups", nonLocalLookups.get());
        m.put("non_local_textures", nonLocalTextures.get());
        Map<String, Object> skins = new TreeMap<>();
        for (Stat st : STATS.values()) {
            synchronized (st) {
                Map<String, Object> s = new LinkedHashMap<>();
                s.put("skin_id", st.skinId);
                s.put("model", st.model);
                s.put("status", st.status);
                s.put("file", st.path == null ? null : st.path.replace('\\', '/'));
                s.put("sha256_expected", st.sha256Expected);
                s.put("sha256_file", st.sha256File);
                s.put("sha256_match", st.sha256Expected == null || st.sha256File == null
                        ? null : st.sha256Expected.equals(st.sha256File));
                s.put("requests", st.requests);
                s.put("loaded_epoch_ms", st.loadedEpochMs < 0 ? null : st.loadedEpochMs);
                skins.put(st.skinId + ":" + st.model, s);
            }
        }
        m.put("skins", skins);
        return m;
    }
}
