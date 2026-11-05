package org.solaris.capture;

import com.google.gson.JsonElement;
import com.google.gson.JsonObject;
import com.google.gson.JsonParser;
import java.net.DatagramPacket;
import java.net.DatagramSocket;
import java.net.InetAddress;
import java.net.InetSocketAddress;
import java.nio.charset.StandardCharsets;
import java.util.LinkedHashMap;
import java.util.Map;
import net.minecraft.client.Camera;
import net.minecraft.client.Minecraft;
import net.minecraft.client.player.LocalPlayer;
import net.minecraft.util.Mth;
import net.minecraft.world.entity.Entity;
import net.minecraft.world.phys.Vec3;

/**
 * T12 pose link: renders the bot's pose directly instead of the server-followed camera.
 *
 * <p>The camera player normally follows its bot through the server (the plugin teleports
 * it every tick), so the rendered view lags the bot by 50-80 ms. The bot's controller
 * (controller/recorder/camera-pose-link.js) sends one UDP datagram per outgoing movement
 * packet to 127.0.0.1:{@code pose_port}:
 * {@code {"v":1,"bot":"Alpha","seq":n,"t":<epoch ms>,"x","y","z" (feet),"yaw","pitch"
 * (Minecraft degrees),"sneak":bool,"riding":bool,"pkt":"position_look"}}. A daemon thread keeps the
 * newest pose; {@code CameraMixin} (Camera#setup TAIL) and {@code GameRendererMixin}
 * (crosshair pick) use it while it is fresh. Otherwise the vanilla camera is kept.
 *
 * <p>Limitations: the eye height switches instantly between 1.62 and 1.27 when the datagram's
 * sneak flag changes (vanilla eases it in Camera#tick by half the difference per tick), and
 * swimming / crawling / gliding poses (eye 0.4) are not modelled (rendered at 1.62). While the
 * bot rides a vehicle ({@code "riding": true}) Mineflayer sends no position packets, so the
 * datagram position is stale: the vanilla (server-followed, seated) camera is kept (reason
 * {@code riding}).
 */
public final class PoseLink {
    /** Player eye heights in 1.21 (Player.STANDING_DIMENSIONS / Pose.CROUCHING). */
    public static final double EYE_STANDING = 1.62;
    public static final double EYE_SNEAKING = 1.27;

    /** One received datagram (immutable). */
    public static final class Pose {
        public final long seq;
        public final double t;
        public final double rxMs;
        public final double x, y, z;
        public final float yaw, pitch;
        public final boolean sneak;
        /** the bot rides a vehicle: x/y/z are stale (no position packets while mounted) */
        public final boolean riding;

        Pose(long seq, double t, double rxMs, double x, double y, double z, float yaw, float pitch, boolean sneak,
             boolean riding) {
            this.seq = seq;
            this.t = t;
            this.rxMs = rxMs;
            this.x = x;
            this.y = y;
            this.z = z;
            this.yaw = yaw;
            this.pitch = pitch;
            this.sneak = sneak;
            this.riding = riding;
        }

        public double eyeHeight() {
            return sneak ? EYE_SNEAKING : EYE_STANDING;
        }

        public Vec3 eye() {
            return new Vec3(x, y + eyeHeight(), z);
        }
    }

    /** What Camera#setup did in one render frame. */
    public static final class Decision {
        public final long frame;
        /** non-null = injected */
        public final Pose pose;
        public final double renderMs;
        /** pose held past pose_max_age_ms because the server-followed pose still equals it */
        public final boolean held;
        /** why the vanilla camera was kept (null when injected) */
        public final String fallback;
        public final float playerYaw, playerPitch;

        Decision(long frame, Pose pose, double renderMs, boolean held, String fallback, float playerYaw, float playerPitch) {
            this.frame = frame;
            this.pose = pose;
            this.renderMs = renderMs;
            this.held = held;
            this.fallback = fallback;
            this.playerYaw = playerYaw;
            this.playerPitch = playerPitch;
        }
    }

    /** Pose-link outcome of one captured frame; counted only once the frame is encoded. */
    public static final class FrameStat {
        final boolean injected;
        final boolean held;
        final double ageMs;
        final String reason;

        FrameStat(boolean injected, boolean held, double ageMs, String reason) {
            this.injected = injected;
            this.held = held;
            this.ageMs = ageMs;
            this.reason = reason;
        }
    }

    private static PoseLink instance;

    private final CaptureConfig cfg;
    private final String bot;
    private volatile Pose latest;
    private volatile String bindError;
    private volatile long received = 0;
    private volatile long rejected = 0;
    private volatile long seqResets = 0;
    private volatile double transitSumMs = 0;
    private volatile double transitMaxMs = Double.NEGATIVE_INFINITY;
    private volatile double transitMinMs = Double.POSITIVE_INFINITY;
    // Render thread only
    private Decision last;
    // Recording statistics of the encoded frames (writer thread, guarded by this)
    private long usedFrames = 0;
    private long heldFrames = 0;
    private long serverFrames = 0;
    private double maxAgeSeenMs = Double.NaN;
    private double maxHeldAgeMs = Double.NaN;
    private final Map<String, Long> fallbackCounts = new LinkedHashMap<>();

    private PoseLink(CaptureConfig cfg) {
        this.cfg = cfg;
        String name = cfg.camera_name == null ? "" : cfg.camera_name;
        this.bot = name.startsWith("Camera") ? name.substring("Camera".length()) : name;
    }

    static void init(CaptureConfig cfg) {
        if (cfg.pose_port <= 0) {
            return;
        }
        instance = new PoseLink(cfg);
        instance.startReceiver();
    }

    public static PoseLink get() {
        return instance;
    }

    private void startReceiver() {
        Thread t = new Thread(this::receiveLoop, "solaris-capture-pose-link");
        t.setDaemon(true);
        t.start();
    }

    private void receiveLoop() {
        DatagramSocket socket;
        try {
            socket = new DatagramSocket(null);
            socket.setReuseAddress(false);
            socket.bind(new InetSocketAddress(InetAddress.getLoopbackAddress(), cfg.pose_port));
        } catch (Exception e) {
            bindError = e.toString();
            SolarisCapture.LOGGER.error("[solaris-capture] pose link: cannot bind udp 127.0.0.1:{} ({}); "
                    + "the camera follows the server only", cfg.pose_port, e.toString());
            return;
        }
        SolarisCapture.LOGGER.info("[solaris-capture] pose link: listening on udp 127.0.0.1:{} for bot {} (max age {} ms)",
                cfg.pose_port, bot, cfg.pose_max_age_ms);
        byte[] buf = new byte[4096];
        DatagramPacket packet = new DatagramPacket(buf, buf.length);
        boolean warned = false;
        while (true) {
            try {
                packet.setLength(buf.length);
                socket.receive(packet);
                double rx = EpochClock.nowMs();
                if (!packet.getAddress().isLoopbackAddress()) {
                    rejected++;
                    continue;
                }
                Pose p = parse(new String(packet.getData(), packet.getOffset(), packet.getLength(), StandardCharsets.UTF_8), rx);
                if (p == null) {
                    rejected++;
                    continue;
                }
                Pose prev = latest;
                if (prev != null && p.seq <= prev.seq) {
                    if (p.t <= prev.t) {
                        rejected++; // reordered / duplicate datagram
                        continue;
                    }
                    seqResets++; // the controller restarted (new seq counter, newer time)
                }
                latest = p;
                received++;
                double transit = rx - p.t;
                transitSumMs += transit;
                transitMaxMs = Math.max(transitMaxMs, transit);
                transitMinMs = Math.min(transitMinMs, transit);
            } catch (Exception e) {
                if (!warned) {
                    warned = true;
                    SolarisCapture.LOGGER.warn("[solaris-capture] pose link: receive failed: {}", e.toString());
                }
                if (socket.isClosed()) {
                    return;
                }
            }
        }
    }

    private Pose parse(String text, double rx) {
        try {
            JsonObject o = JsonParser.parseString(text).getAsJsonObject();
            if (!o.has("v") || o.get("v").getAsInt() != 1) {
                return null;
            }
            if (!o.has("bot") || !bot.equals(o.get("bot").getAsString())) {
                return null;
            }
            double x = num(o, "x"), y = num(o, "y"), z = num(o, "z"), yaw = num(o, "yaw"), pitch = num(o, "pitch");
            double t = num(o, "t");
            if (!(Double.isFinite(x) && Double.isFinite(y) && Double.isFinite(z) && Double.isFinite(yaw)
                    && Double.isFinite(pitch) && Double.isFinite(t)) || !o.has("seq")) {
                return null;
            }
            boolean sneak = o.has("sneak") && o.get("sneak").isJsonPrimitive() && o.get("sneak").getAsBoolean();
            boolean riding = o.has("riding") && o.get("riding").isJsonPrimitive() && o.get("riding").getAsBoolean();
            return new Pose(o.get("seq").getAsLong(), t, rx, x, y, z, (float) yaw, (float) pitch, sneak, riding);
        } catch (RuntimeException e) {
            return null;
        }
    }

    private static double num(JsonObject o, String key) {
        JsonElement e = o.get(key);
        if (e == null || !e.isJsonPrimitive() || !e.getAsJsonPrimitive().isNumber()) {
            return Double.NaN;
        }
        return e.getAsDouble();
    }

    // ------------------------------------------------------------------
    // Render thread
    // ------------------------------------------------------------------

    /**
     * The pose to render now, or null (vanilla camera). {@code reason[0]} receives the
     * fallback reason, {@code held[0]} whether a stale pose is held because the server-followed
     * pose still equals it.
     */
    private Pose choose(Minecraft mc, Entity entity, boolean detached, Vec3 vanillaPos, double now,
                        String[] reason, boolean[] held) {
        Pose p = latest;
        LocalPlayer player = mc.player;
        if (bindError != null) {
            reason[0] = "bind_failed";
            return null;
        }
        if (p == null) {
            reason[0] = "no_pose";
            return null;
        }
        if (player == null || entity != player) {
            reason[0] = "not_player_camera";
            return null;
        }
        if (detached) {
            reason[0] = "third_person";
            return null;
        }
        if (player.isSleeping() || !player.isAlive()) {
            reason[0] = "player_state";
            return null;
        }
        if (p.riding) {
            // Mineflayer sends no position packets while mounted: the datagram position is the
            // mount point, while the server-followed camera sits on the vehicle
            reason[0] = "riding";
            return null;
        }
        if (now - p.rxMs > cfg.pose_max_age_ms) {
            if (cfg.pose_hold_when_matching && matchesServerPose(player, p)) {
                held[0] = true;
            } else {
                reason[0] = "stale";
                return null;
            }
        }
        if (vanillaPos != null && p.eye().distanceTo(vanillaPos) > cfg.pose_max_distance) {
            reason[0] = "too_far";
            return null;
        }
        return p;
    }

    /** The camera player's (server-followed) pose equals the datagram pose: the bot did not move since. */
    private static boolean matchesServerPose(LocalPlayer player, Pose p) {
        return Math.abs(player.getX() - p.x) < 1e-3 && Math.abs(player.getY() - p.y) < 1e-3
                && Math.abs(player.getZ() - p.z) < 1e-3
                && Math.abs(Mth.wrapDegrees(player.getYRot() - p.yaw)) < 0.01f
                && Math.abs(player.getXRot() - p.pitch) < 0.01f
                // sneaking toggles without movement: the camera player is rescaled server side
                && Math.abs(player.getEyeHeight() - p.eyeHeight()) < 0.1;
    }

    /**
     * Camera#setup TAIL (CameraMixin): returns the pose to apply to the main camera, or
     * null to keep the vanilla pose. Records the decision for the frames sidecar.
     */
    public Pose onCameraSetup(Camera camera, Entity entity, boolean detached, float partial) {
        Minecraft mc = Minecraft.getInstance();
        if (camera != mc.gameRenderer.getMainCamera()) {
            return null;
        }
        double now = EpochClock.nowMs();
        String[] reason = new String[1];
        boolean[] held = new boolean[1];
        Pose p = choose(mc, entity, detached, camera.getPosition(), now, reason, held);
        float pyaw = mc.player != null ? mc.player.getViewYRot(partial) : Float.NaN;
        float ppitch = mc.player != null ? mc.player.getViewXRot(partial) : Float.NaN;
        last = new Decision(CaptureController.frameIndex(), p, now, held[0], p == null ? reason[0] : null, pyaw, ppitch);
        return p;
    }

    /** GameRenderer#pick: pose for the crosshair ray of this entity (null = vanilla). */
    public Pose forPick(Entity entity) {
        Minecraft mc = Minecraft.getInstance();
        boolean detached = !mc.options.getCameraType().isFirstPerson();
        // same distance guard as the camera (vanilla eye = the entity's current eye position)
        return choose(mc, entity, detached, entity.getEyePosition(), EpochClock.nowMs(), new String[1],
                new boolean[1]);
    }

    /**
     * Sidecar fields for the frame being captured (render thread). Returns the outcome to
     * count with {@link #countEncoded} once the writer has encoded the frame.
     */
    public FrameStat appendSidecar(StringBuilder b, long frame) {
        Decision d = last != null && last.frame == frame ? last : null;
        FrameStat stat;
        if (d != null && d.pose != null) {
            Pose p = d.pose;
            double age = d.renderMs - p.t;
            stat = new FrameStat(true, d.held, age, null);
            b.append(", \"pose_source\": \"injected\"");
            b.append(", \"pose_seq\": ").append(p.seq);
            b.append(", \"pose_t\": ").append(fmt(p.t, 3));
            b.append(", \"pose_age_ms\": ").append(fmt(age, 3));
            b.append(", \"pose_held\": ").append(d.held);
            b.append(", \"pose_sneak\": ").append(p.sneak);
            b.append(", \"pose_fallback\": null");
        } else {
            String reason = d == null ? "no_world_render" : d.fallback;
            stat = new FrameStat(false, false, Double.NaN, reason);
            Pose p = latest;
            b.append(", \"pose_source\": \"server\"");
            // newest datagram (not rendered) for diagnostics
            b.append(", \"pose_seq\": ").append(p != null ? Long.toString(p.seq) : "null");
            b.append(", \"pose_t\": ").append(p != null ? fmt(p.t, 3) : "null");
            b.append(", \"pose_age_ms\": ").append(p != null && d != null ? fmt(d.renderMs - p.t, 3) : "null");
            b.append(", \"pose_held\": false");
            b.append(", \"pose_sneak\": null");
            b.append(", \"pose_fallback\": ").append(reason == null ? "null" : "\"" + reason + "\"");
        }
        b.append(", \"player_yaw\": ").append(d != null ? fmt(d.playerYaw, 4) : "null");
        b.append(", \"player_pitch\": ").append(d != null ? fmt(d.playerPitch, 4) : "null");
        return stat;
    }

    /** Writer thread: the frame with this outcome was encoded (meta used/server frame counts). */
    public static void countEncoded(Object stat) {
        PoseLink l = instance;
        if (l == null || !(stat instanceof FrameStat)) {
            return;
        }
        FrameStat s = (FrameStat) stat;
        synchronized (l) {
            if (s.injected) {
                l.usedFrames++;
                if (s.held) {
                    l.heldFrames++;
                    l.maxHeldAgeMs = Double.isNaN(l.maxHeldAgeMs) ? s.ageMs : Math.max(l.maxHeldAgeMs, s.ageMs);
                } else {
                    l.maxAgeSeenMs = Double.isNaN(l.maxAgeSeenMs) ? s.ageMs : Math.max(l.maxAgeSeenMs, s.ageMs);
                }
            } else {
                l.serverFrames++;
                l.fallbackCounts.merge(s.reason == null ? "unknown" : s.reason, 1L, Long::sum);
            }
        }
    }

    /** Without a pose link the sidecar still says where the pose came from. */
    public static void appendSidecarDisabled(StringBuilder b, Minecraft mc, float partial) {
        b.append(", \"pose_source\": \"server\", \"pose_seq\": null, \"pose_t\": null, \"pose_age_ms\": null");
        b.append(", \"pose_held\": false, \"pose_sneak\": null, \"pose_fallback\": \"pose_link_off\"");
        b.append(", \"player_yaw\": ").append(mc.player != null ? fmt(mc.player.getViewYRot(partial), 4) : "null");
        b.append(", \"player_pitch\": ").append(mc.player != null ? fmt(mc.player.getViewXRot(partial), 4) : "null");
    }

    /** Meta {@code pose_link} object. */
    public static Map<String, Object> meta(CaptureConfig cfg) {
        Map<String, Object> m = new LinkedHashMap<>();
        PoseLink l = instance;
        m.put("enabled", l != null && l.bindError == null);
        m.put("port", cfg.pose_port);
        m.put("max_age_ms", cfg.pose_max_age_ms);
        m.put("hold_when_matching", cfg.pose_hold_when_matching);
        m.put("max_distance", cfg.pose_max_distance);
        m.put("eye_height_standing", EYE_STANDING);
        m.put("eye_height_sneaking", EYE_SNEAKING);
        if (l == null) {
            return m;
        }
        m.put("bot", l.bot);
        m.put("bind_error", l.bindError);
        m.put("received", l.received);
        m.put("rejected", l.rejected);
        m.put("seq_resets", l.seqResets);
        m.put("transit_ms_mean", l.received > 0 ? l.transitSumMs / l.received : null);
        m.put("transit_ms_min", l.received > 0 ? l.transitMinMs : null);
        m.put("transit_ms_max", l.received > 0 ? l.transitMaxMs : null);
        synchronized (l) {
            // counts of encoded frames (= sidecar lines), not of captured frames
            m.put("used_frames", l.usedFrames);
            m.put("held_frames", l.heldFrames);
            m.put("server_frames", l.serverFrames);
            m.put("max_age_seen_ms", Double.isNaN(l.maxAgeSeenMs) ? null : l.maxAgeSeenMs);
            m.put("max_held_age_ms", Double.isNaN(l.maxHeldAgeMs) ? null : l.maxHeldAgeMs);
            m.put("server_frame_reasons", new LinkedHashMap<>(l.fallbackCounts));
        }
        return m;
    }

    private static String fmt(double v, int decimals) {
        if (Double.isNaN(v) || Double.isInfinite(v)) {
            return "null";
        }
        return String.format(java.util.Locale.ROOT, "%." + decimals + "f", v);
    }
}
