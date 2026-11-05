// Modified by Ziyang Ye for MineEngine, 2026 (from SolarisEngine 78f23e7); see capture/MODIFICATIONS.md
package me.berrycraft.mirrorbot;

import org.bukkit.Bukkit;
import org.bukkit.entity.Player;

import net.skinsrestorer.api.SkinsRestorer;
import net.skinsrestorer.api.SkinsRestorerProvider;
import net.skinsrestorer.api.exception.DataRequestException;
import net.skinsrestorer.api.property.InputDataResult;
import net.skinsrestorer.api.property.SkinIdentifier;
import net.skinsrestorer.api.property.SkinProperty;
import net.skinsrestorer.api.storage.PlayerStorage;
import net.skinsrestorer.api.storage.SkinStorage;

import java.io.File;
import java.io.IOException;
import java.nio.file.Files;
import java.nio.charset.StandardCharsets;
import java.util.*;

import com.destroystokyo.paper.profile.ProfileProperty;

import com.google.gson.JsonObject;
import com.google.gson.JsonParser;

public class SkinManager {

    private final EpisodeManager plugin;
    private final SkinsRestorer skinsRestorer;
    private final File skinsDir;
    private static final String POOL_DIR = "pool";
    /** Host of the unsigned local-pool texture URLs (the camera mod maps them to its local pool). */
    public static final String LOCAL_HOST = "solaris.local";
    /** texture_hash of a local skin: "local-" + PNG sha256 (Mojang ids are bare 64-hex). */
    public static final String LOCAL_HASH_PREFIX = "local-";
    /** catalogue.json skin_id -> model ("classic" | "slim"), reloaded when the file changes. */
    private Map<String, String> catalogueModels = Collections.emptyMap();
    private long catalogueMtime = Long.MIN_VALUE;
    /** player UUID -> skin id last applied through applySkinNow() / applySharedSkin(). */
    private final Map<UUID, String> appliedSkinIds = new java.util.concurrent.ConcurrentHashMap<>();

    public SkinManager(EpisodeManager plugin) {
        this.plugin = plugin;

        if (Bukkit.getPluginManager().getPlugin("SkinsRestorer") != null) {
            skinsRestorer = SkinsRestorerProvider.get();
            plugin.getLogger().info("[SkinManager] SkinsRestorer detected, API attached.");
        } else {
            plugin.getLogger().warning("[SkinManager] SkinsRestorer not found. Skins will not be applied.");
            skinsRestorer = null;
        }

        this.skinsDir = resolveSkinsDirectory();
        if (skinsDir != null) {
            boolean created = skinsDir.mkdirs();
            plugin.getLogger().info("[SkinManager] Skins directory resolved to: " + skinsDir.getAbsolutePath() + " (created=" + created + ")");
        } else {
            plugin.getLogger().warning("[SkinManager] Failed to resolve skins directory; custom skins will be unavailable.");
        }
    }

    // ------------------------------------------------------------------------------------
    // Skin Loading
    // ------------------------------------------------------------------------------------

    public Map<String, File> loadSkins() {
        if (skinsDir == null || !skinsDir.exists()) {
            plugin.getLogger().warning("[SkinManager] Skins directory missing: " + skinsDir);
            return Collections.emptyMap();
        }

        Map<String, File> map = new TreeMap<>();
        // pool/ first so a top-level skin with the same normalized name wins
        File poolDir = new File(skinsDir, POOL_DIR);
        File[] pool = poolDir.isDirectory() ? poolDir.listFiles((dir, name) -> name.endsWith(".png")) : null;
        if (pool != null) {
            for (File f : pool) map.put(normalizeName(f.getName()), f);
        }
        File[] list = skinsDir.listFiles((dir, name) -> name.endsWith(".png"));
        if (list == null) {
            plugin.getLogger().warning("[SkinManager] listFiles returned null for: " + skinsDir.getAbsolutePath());
            return map;
        }

        for (File f : list) {
            map.put(normalizeName(f.getName()), f);
        }

        plugin.getLogger().info("[SkinManager] Loaded " + map.size() + " skin(s) from " + skinsDir.getAbsolutePath()
                + " (" + (pool == null ? 0 : pool.length) + " in " + POOL_DIR + "/)");
        return map;
    }

    /** True for skins of the per-episode pool (skins/pool/), which are cached lazily on use. */
    public boolean isPoolSkin(File png) {
        File parent = png.getAbsoluteFile().getParentFile();
        return parent != null && POOL_DIR.equals(parent.getName());
    }

    /**
     * Pre-populates SkinsRestorer's SkinStorage from the .customskin cache files (pre-signed texture data) of the
     * top-level skins that have one. A skin without a cache is applied as a local (unsigned) skin at episode start.
     *
     * @param skins the map returned by {@link #loadSkins()}
     * @return false only if SkinsRestorer is present but its skin storage is unusable
     */
    public boolean preloadSkinCache(Map<String, File> skins) {
        if (skinsRestorer == null) {
            plugin.getLogger().warning("[SkinManager] SkinsRestorer unavailable: no signed skins; skins are applied as local (unsigned) skins.");
            return true;
        }

        SkinStorage storage = skinsRestorer.getSkinStorage();
        if (storage == null) {
            plugin.getLogger().severe("[SkinManager] SkinStorage is null; cannot preload skin cache.");
            return false;
        }

        boolean allOk = true;
        int skippedPool = 0;
        for (Map.Entry<String, File> entry : skins.entrySet()) {
            File pngFile = entry.getValue();
            if (isPoolSkin(pngFile)) {
                // per-episode pool skins are injected on demand by applySkinNow(); a pool skin whose
                // .customskin has not been generated yet must not block the episode start
                skippedPool++;
                continue;
            }
            File cacheFile = new File(pngFile.getAbsolutePath() + ".customskin");

            if (!cacheFile.exists()) {
                plugin.getLogger().info("[SkinManager] No .customskin cache for " + pngFile.getName()
                        + "; it is applied as a local (unsigned) skin.");
                continue;
            }

            try {
                String json = Files.readString(cacheFile.toPath());
                JsonObject obj = JsonParser.parseString(json).getAsJsonObject();
                String value = obj.get("value").getAsString();
                String signature = obj.get("signature").getAsString();

                String key = skinKey(pngFile);
                SkinProperty property = SkinProperty.of(value, signature);
                storage.setCustomSkinData(key, property);

                plugin.getLogger().info("[SkinManager] Pre-cached skin: " + key + " from " + cacheFile.getName());
            } catch (IOException e) {
                plugin.getLogger().severe("[SkinManager] Failed to read cache file " + cacheFile.getName() + ": " + e.getMessage());
                allOk = false;
            } catch (Exception e) {
                plugin.getLogger().severe("[SkinManager] Failed to parse cache file " + cacheFile.getName() + ": " + e.getMessage());
                allOk = false;
            }
        }

        if (skippedPool > 0) {
            plugin.getLogger().info("[SkinManager] " + skippedPool + " pool skin(s) will be cached on demand.");
        }
        return allOk;
    }

    /**
     * SkinsRestorer custom-skin key. SkinsRestorer's file storage uses the key as a file name, and
     * ':' is illegal in Windows file names, so Windows uses "file_" instead of "file:".
     */
    private static String skinKey(File png) {
        boolean windows = System.getProperty("os.name", "").toLowerCase(Locale.ROOT).startsWith("windows");
        return (windows ? "file_" : "file:") + png.getName().toLowerCase(Locale.ROOT);
    }

    public File resolveSkin(String key, Map<String, File> skins) {
        return skins.get(normalizeName(key));
    }

    private String normalizeName(String name) {
        name = name.toLowerCase(Locale.ROOT);
        if (name.endsWith(".png")) name = name.substring(0, name.length() - 4);

        int underscore = name.indexOf('_');
        if (underscore > 0) {
            String prefix = name.substring(0, underscore);
            if (prefix.chars().allMatch(Character::isDigit)) {
                name = name.substring(underscore + 1);
            }
        }
        return name;
    }

    // ------------------------------------------------------------------------------------
    // Skin Application
    // ------------------------------------------------------------------------------------

    public void applySharedSkin(Player controller, Player camera, File file) {
        if (skinsRestorer == null) {
            plugin.getLogger().warning("[SkinManager] SkinsRestorer unavailable; skipping skin apply for " + controller.getName() + " / " + camera.getName());
            return;
        }

        plugin.getLogger().info("[SkinManager] Applying shared skin " + file.getName() + " to " + controller.getName() + " and " + camera.getName());
        appliedSkinIds.put(controller.getUniqueId(), skinIdOf(file));
        appliedSkinIds.put(camera.getUniqueId(), skinIdOf(file));

        Bukkit.getScheduler().runTaskAsynchronously(plugin, () -> {
            try {
                SkinStorage storage = skinsRestorer.getSkinStorage();
                if (storage == null) {
                    plugin.getLogger().severe("[SkinManager] SkinStorage is null; cannot apply skin.");
                    return;
                }

                String key = skinKey(file);

                Optional<InputDataResult> cached = storage.findSkinData(key);
                if (cached.isEmpty()) {
                    plugin.getLogger().severe("[SkinManager] No pre-cached skin data for key " + key
                            + ". Ensure .customskin files are present in the skins directory.");
                    return;
                }

                plugin.getLogger().info("[SkinManager] Using pre-cached skin for key " + key);
                SkinIdentifier id = cached.get().getIdentifier();

                PlayerStorage ps = skinsRestorer.getPlayerStorage();
                ps.setSkinIdOfPlayer(controller.getUniqueId(), id);
                ps.setSkinIdOfPlayer(camera.getUniqueId(), id);

                Bukkit.getScheduler().runTask(plugin, () -> {
                    try {
                        skinsRestorer.getSkinApplier(Player.class).applySkin(controller);
                        skinsRestorer.getSkinApplier(Player.class).applySkin(camera);
                    } catch (DataRequestException e) {
                        e.printStackTrace();
                    }
                });

            } catch (Exception ex) {
                plugin.getLogger().severe("[SkinManager] Failed to apply skin " + file.getName() + ": " + ex.getMessage());
                ex.printStackTrace();
            }
        });
    }

    // ------------------------------------------------------------------------------------
    // Synchronous skin change (/episode skin), used between episodes
    // ------------------------------------------------------------------------------------

    /** Parsed .customskin of one PNG. */
    public static final class SkinInfo {
        public final String skinId;
        public final File file;
        public final String value;
        public final String signature;
        public final String model;        // "classic" | "slim" (from the signed / local payload)
        public final String textureHash;  // textures.minecraft.net id, or "local-<png sha256>" (local mode)
        /** "signed" (Mojang-signed .customskin via SkinsRestorer) | "local" (unsigned local-pool texture). */
        public final String mode;
        public final String sha256;       // PNG sha256 (local mode), else null

        SkinInfo(String skinId, File file, String value, String signature, String model, String textureHash) {
            this(skinId, file, value, signature, model, textureHash, "signed", null);
        }

        SkinInfo(String skinId, File file, String value, String signature, String model, String textureHash,
                 String mode, String sha256) {
            this.skinId = skinId;
            this.file = file;
            this.value = value;
            this.signature = signature;
            this.model = model;
            this.textureHash = textureHash;
            this.mode = mode;
            this.sha256 = sha256;
        }

        public boolean signed() {
            return signature != null;
        }
    }

    public String skinIdOf(File png) {
        return normalizeName(png.getName());
    }

    /** Reads the .customskin next to the PNG. Throws with a readable message if missing/invalid. */
    public SkinInfo readSkinInfo(File png) throws IOException {
        File cacheFile = new File(png.getAbsolutePath() + ".customskin");
        if (!cacheFile.exists()) {
            throw new IOException("missing .customskin for " + png.getName() + " (run regenerate_skin_cache.py)");
        }
        JsonObject obj = JsonParser.parseString(Files.readString(cacheFile.toPath())).getAsJsonObject();
        if (!obj.has("value") || !obj.has("signature")) {
            throw new IOException("invalid .customskin for " + png.getName() + " (no value/signature)");
        }
        String value = obj.get("value").getAsString();
        String signature = obj.get("signature").getAsString();
        String[] mh = decodeModelAndHash(value);
        return new SkinInfo(skinIdOf(png), png, value, signature, mh[0], mh[1]);
    }

    /** {model, textureHash} of a base64 textures property value; model "classic" unless slim. */
    public static String[] decodeModelAndHash(String value) {
        String model = "classic";
        String hash = null;
        try {
            String json = new String(Base64.getDecoder().decode(value), StandardCharsets.UTF_8);
            JsonObject root = JsonParser.parseString(json).getAsJsonObject();
            JsonObject skin = root.getAsJsonObject("textures").getAsJsonObject("SKIN");
            if (skin != null) {
                if (skin.has("url")) {
                    String url = skin.get("url").getAsString();
                    hash = url.substring(url.lastIndexOf('/') + 1);
                }
                JsonObject sol = root.has("solaris") && root.get("solaris").isJsonObject() ? root.getAsJsonObject("solaris") : null;
                if (sol != null && sol.has("sha256")) {
                    hash = LOCAL_HASH_PREFIX + sol.get("sha256").getAsString();
                }
                if (skin.has("metadata") && skin.getAsJsonObject("metadata").has("model")) {
                    model = skin.getAsJsonObject("metadata").get("model").getAsString();
                }
            }
        } catch (Exception ignored) {
            // malformed payload: keep defaults
        }
        return new String[]{"slim".equals(model) ? "slim" : "classic", hash};
    }

    /** Current textures property of an online player: {model, textureHash, signed("1"/"0")} or null. */
    public String[] currentTexture(Player p) {
        if (p == null) return null;
        for (ProfileProperty prop : p.getPlayerProfile().getProperties()) {
            if ("textures".equals(prop.getName())) {
                String[] mh = decodeModelAndHash(prop.getValue());
                return new String[]{mh[0], mh[1], prop.isSigned() ? "1" : "0"};
            }
        }
        return null;
    }

    public String appliedSkinId(Player p) {
        return p == null ? null : appliedSkinIds.get(p.getUniqueId());
    }

    /**
     * Injects the signed texture into SkinsRestorer (custom skin key, as at episode start), binds it to the
     * controller and (if online) its camera, and applies it right away. SkinsRestorer refreshes the player
     * for every viewer (and re-sends the player's own profile). Must be called on the main server thread.
     */
    public SkinInfo applySkinNow(Player controller, Player camera, File png) throws Exception {
        if (skinsRestorer == null) throw new IllegalStateException("SkinsRestorer unavailable");
        SkinInfo info = readSkinInfo(png);
        SkinStorage storage = skinsRestorer.getSkinStorage();
        if (storage == null) throw new IllegalStateException("SkinsRestorer SkinStorage is null");
        String key = skinKey(png);
        SkinProperty property = SkinProperty.of(info.value, info.signature);
        storage.setCustomSkinData(key, property);
        Optional<InputDataResult> cached = storage.findSkinData(key);
        PlayerStorage ps = skinsRestorer.getPlayerStorage();
        if (cached.isPresent()) {
            SkinIdentifier id = cached.get().getIdentifier();
            ps.setSkinIdOfPlayer(controller.getUniqueId(), id);
            if (camera != null) ps.setSkinIdOfPlayer(camera.getUniqueId(), id);
        } else {
            plugin.getLogger().warning("[SkinManager] findSkinData(" + key + ") empty after setCustomSkinData; applying property directly");
        }
        skinsRestorer.getSkinApplier(Player.class).applySkin(controller, property);
        appliedSkinIds.put(controller.getUniqueId(), info.skinId);
        if (camera != null) {
            skinsRestorer.getSkinApplier(Player.class).applySkin(camera, property);
            appliedSkinIds.put(camera.getUniqueId(), info.skinId);
        }
        plugin.getLogger().info("[SkinManager] Applied skin " + info.skinId + " (" + info.model + ", " + info.textureHash
                + ") to " + controller.getName() + (camera != null ? " + " + camera.getName() : ""));
        return info;
    }

    // ------------------------------------------------------------------------------------
    // Local (unsigned) skins: any pool PNG, no Mojang signature / MineSkin needed
    // ------------------------------------------------------------------------------------

    /** Model of a skin: catalogue.json entry, else detected from the PNG (slim = arm columns 54-55 transparent). */
    public String modelOf(File png) {
        String m = catalogueModels().get(skinIdOf(png));
        if (m != null) return m;
        try {
            java.awt.image.BufferedImage img = javax.imageio.ImageIO.read(png);
            if (img != null && img.getWidth() == 64 && img.getHeight() == 64) {
                int alpha = 0;
                for (int x = 54; x <= 55; x++)
                    for (int y = 20; y < 32; y++) alpha += (img.getRGB(x, y) >>> 24);
                return alpha == 0 ? "slim" : "classic";
            }
        } catch (Exception ignored) {
            // fall through
        }
        return "classic";
    }

    private synchronized Map<String, String> catalogueModels() {
        if (skinsDir == null) return Collections.emptyMap();
        File cat = new File(skinsDir, "catalogue.json");
        long mt = cat.isFile() ? cat.lastModified() : -1;
        if (mt == catalogueMtime) return catalogueModels;
        Map<String, String> map = new HashMap<>();
        if (mt >= 0) {
            try {
                JsonObject root = JsonParser.parseString(Files.readString(cat.toPath())).getAsJsonObject();
                for (com.google.gson.JsonElement el : root.getAsJsonArray("skins")) {
                    JsonObject e = el.getAsJsonObject();
                    if (e.has("skin_id") && e.has("model")) {
                        map.put(e.get("skin_id").getAsString().toLowerCase(Locale.ROOT),
                                "slim".equals(e.get("model").getAsString()) ? "slim" : "classic");
                    }
                }
            } catch (Exception e) {
                plugin.getLogger().warning("[SkinManager] cannot read " + cat + ": " + e);
            }
        }
        catalogueModels = map;
        catalogueMtime = mt;
        return map;
    }

    private static String sha256Hex(File f) throws IOException {
        try {
            byte[] d = java.security.MessageDigest.getInstance("SHA-256").digest(Files.readAllBytes(f.toPath()));
            return java.util.HexFormat.of().formatHex(d);
        } catch (java.security.NoSuchAlgorithmException e) {
            throw new IOException(e);
        }
    }

    /** PNG path relative to the skins directory ("pool/proc_0001.png", "session_a.png"). */
    private String relativeFile(File png) {
        if (skinsDir != null) {
            try {
                java.nio.file.Path base = skinsDir.getCanonicalFile().toPath();
                java.nio.file.Path p = png.getCanonicalFile().toPath();
                if (p.startsWith(base)) return base.relativize(p).toString().replace(File.separatorChar, '/');
            } catch (IOException ignored) {
                // fall through
            }
        }
        return (isPoolSkin(png) ? POOL_DIR + "/" : "") + png.getName();
    }

    /**
     * Unsigned textures property naming a local skin (windows/schema_parts/r2-skins.md). Vanilla clients
     * ignore it (default skin); the camera mod (solaris-capture LocalSkins) renders the PNG from its local
     * copy of the pool, never downloading anything.
     */
    public SkinInfo buildLocalSkin(Player owner, File png) throws IOException {
        if (!png.isFile()) throw new IOException("skin file missing: " + png);
        String skinId = skinIdOf(png);
        String model = modelOf(png);
        String sha = sha256Hex(png);
        JsonObject skin = new JsonObject();
        skin.addProperty("url", "http://" + LOCAL_HOST + "/skins/" + skinId + ".png");
        if ("slim".equals(model)) {
            JsonObject meta = new JsonObject();
            meta.addProperty("model", "slim");
            skin.add("metadata", meta);
        }
        JsonObject textures = new JsonObject();
        textures.add("SKIN", skin);
        JsonObject sol = new JsonObject();
        sol.addProperty("v", 1);
        sol.addProperty("skin_id", skinId);
        sol.addProperty("file", relativeFile(png));
        sol.addProperty("sha256", sha);
        sol.addProperty("model", model);
        JsonObject root = new JsonObject();
        root.addProperty("timestamp", System.currentTimeMillis());
        if (owner != null) {
            root.addProperty("profileId", owner.getUniqueId().toString().replace("-", ""));
            root.addProperty("profileName", owner.getName());
        }
        root.addProperty("signatureRequired", false);
        root.add("textures", textures);
        root.add("solaris", sol);
        String value = Base64.getEncoder().encodeToString(root.toString().getBytes(StandardCharsets.UTF_8));
        return new SkinInfo(skinId, png, value, null, model, LOCAL_HASH_PREFIX + sha, "local", sha);
    }

    /** Unsigned textures property through the Paper profile API (re-sends the player to its viewers and itself). */
    private static void setUnsignedTextures(Player p, String value) {
        com.destroystokyo.paper.profile.PlayerProfile prof = p.getPlayerProfile();
        prof.removeProperty("textures");
        prof.setProperty(new ProfileProperty("textures", value));
        p.setPlayerProfile(prof);
    }

    /**
     * Local mode of /episode skin: any PNG (pool or top-level), no .customskin needed. The controller and (if
     * online) its camera get an unsigned 'textures' property naming the skin; Paper's setPlayerProfile
     * re-sends the player to every viewer that can see it and to itself. SkinsRestorer's binding of the two
     * players is removed so it does not re-apply a signed skin over it. Main server thread only.
     */
    public SkinInfo applyLocalSkinNow(Player controller, Player camera, File png) throws Exception {
        SkinInfo info = buildLocalSkin(controller, png);
        if (skinsRestorer != null) {
            try {
                PlayerStorage ps = skinsRestorer.getPlayerStorage();
                ps.removeSkinIdOfPlayer(controller.getUniqueId());
                if (camera != null) ps.removeSkinIdOfPlayer(camera.getUniqueId());
            } catch (Exception e) {
                plugin.getLogger().warning("[SkinManager] could not clear the SkinsRestorer binding: " + e);
            }
        }
        setUnsignedTextures(controller, info.value);
        appliedSkinIds.put(controller.getUniqueId(), info.skinId);
        if (camera != null) {
            setUnsignedTextures(camera, buildLocalSkin(camera, png).value);
            appliedSkinIds.put(camera.getUniqueId(), info.skinId);
        }
        plugin.getLogger().info("[SkinManager] Applied local skin " + info.skinId + " (" + info.model + ", sha256 "
                + info.sha256.substring(0, 12) + ", unsigned) to " + controller.getName()
                + (camera != null ? " + " + camera.getName() : ""));
        return info;
    }

    /** A signed cache (.customskin) exists next to the PNG. */
    public boolean hasSignedCache(File png) {
        return new File(png.getAbsolutePath() + ".customskin").isFile();
    }

    /** SkinsRestorer is loaded, so signed skins can be applied. */
    public boolean canApplySigned() {
        return skinsRestorer != null;
    }

    private File resolveSkinsDirectory() {
        try {
            File pluginFolder = plugin.getDataFolder().getCanonicalFile();
            File pluginsDir = pluginFolder.getParentFile();

            File dataDir = pluginsDir.getParentFile();
            return (dataDir != null)
                    ? new File(dataDir, "skins")
                    : new File(pluginsDir, "skins");
        } catch (IOException e) {
            return null;
        }
    }
}
