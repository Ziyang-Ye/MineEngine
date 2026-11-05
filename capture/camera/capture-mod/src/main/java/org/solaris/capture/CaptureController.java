package org.solaris.capture;

import com.google.gson.GsonBuilder;
import com.mojang.blaze3d.pipeline.RenderTarget;
import com.mojang.blaze3d.platform.GlUtil;
import com.mojang.blaze3d.platform.Window;
import java.io.IOException;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.nio.file.StandardCopyOption;
import java.time.Instant;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Locale;
import java.util.Map;
import java.util.concurrent.CompletableFuture;
import java.util.concurrent.ExecutionException;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.TimeoutException;
import net.fabricmc.loader.api.FabricLoader;
import net.minecraft.SharedConstants;
import net.minecraft.client.Camera;
import net.minecraft.client.Minecraft;
import net.minecraft.client.Options;
import net.minecraft.world.entity.Entity;
import net.minecraft.world.phys.Vec3;
import org.lwjgl.glfw.GLFW;
import org.solaris.capture.mixin.OptionsAccessor;

/**
 * Recording state machine, driven from the render thread by the Minecraft mixin
 * (windows/CONTRACT.md section 5.4): start once the player is in a world, capture one
 * frame per wall-clock slot of 1000/fps ms right before the buffer swap, stop on
 * the stop file / disconnect / JVM shutdown.
 */
public final class CaptureController {
    private enum State { WAITING, RECORDING, STOPPED }

    private static CaptureController instance;

    private final CaptureConfig cfg;
    private final double periodMs;
    private volatile State state = State.WAITING;
    private volatile boolean stopFileSeen = false;
    /** Why stopFileSeen was set: stop_file, or launcher_exit (launcher process gone). */
    private volatile String stopCause = "stop_file";
    private volatile boolean finished = false;
    private volatile boolean quitRequested = false;
    private FrameReader reader;
    private FrameWriter writer;
    private Path videoPath;
    private Path metaPath;
    private Path framesPath;
    private Path ffmpegLog;
    private Map<String, Object> meta;
    private double t0Ms;
    private long lastSlot = -1;
    private long captured = 0;
    private long slotsSkipped = 0;
    private double lastCaptureMs = -1;
    private double maxGapMs = 0;
    private long renderFrames = 0;
    private double frameStartMs = 0;
    private double nominalNextDraw = 0;
    private double levelNullSinceMs = -1;
    private String stopReason = null;
    private long slotLagSumUs = 0;
    private boolean windowHidden = false;
    // Rate-limited frame-loss warnings while recording
    private double lossCheckMs = 0;
    private int lossLastDropped = 0;
    private long lossLastSkipped = 0;
    private long lossLastCaptured = 0;
    private int lossWarnings = 0;
    private CompletableFuture<String> encoderProbe;
    // Rendered FOV: set by GameRendererMixin during renderLevel of the current frame
    private static volatile double worldFov = Double.NaN;
    private static volatile long worldFovFrame = -1;
    private static long frameCounter = 0;

    private CaptureController(CaptureConfig cfg) {
        this.cfg = cfg;
        this.periodMs = 1000.0 / cfg.fps;
    }

    static void init(CaptureConfig cfg) {
        instance = new CaptureController(cfg);
        PoseLink.init(cfg);
        instance.startStopFileWatcher();
        instance.startEncoderProbe();
        Runtime.getRuntime().addShutdownHook(new Thread(instance::onShutdown, "solaris-capture-shutdown"));
    }

    public static CaptureController get() {
        return instance;
    }

    public static CaptureConfig config() {
        return instance == null ? null : instance.cfg;
    }

    /** Render frame counter (incremented at Minecraft#runTick HEAD). */
    public static long frameIndex() {
        return frameCounter;
    }

    /** GameRendererMixin: FOV returned by GameRenderer#getFov(camera, partial, true). */
    public static void onWorldFov(double fov) {
        worldFov = fov;
        worldFovFrame = frameCounter;
    }

    private void startStopFileWatcher() {
        Path stop = cfg.stopFile();
        ProcessHandle launcher = null;
        if (cfg.launcher_pid > 0) {
            launcher = ProcessHandle.of(cfg.launcher_pid).orElse(null);
            if (launcher == null) {
                SolarisCapture.LOGGER.warn("[solaris-capture] launcher pid {} not found; launcher watchdog disabled",
                        cfg.launcher_pid);
            }
        }
        if (stop == null && launcher == null) {
            return;
        }
        ProcessHandle parent = launcher;
        Thread t = new Thread(() -> {
            // Runs until the stop file appears or the launcher dies (also after a
            // disconnect ended the recording, so the client still quits)
            while (!quitRequested) {
                if (stop != null && Files.exists(stop)) {
                    SolarisCapture.LOGGER.info("[solaris-capture] stop file {} found", stop);
                    stopCause = "stop_file";
                    stopFileSeen = true;
                    return;
                }
                if (parent != null && !parent.isAlive()) {
                    SolarisCapture.LOGGER.warn("[solaris-capture] launcher (pid {}) is gone; stopping as for the stop file",
                            parent.pid());
                    stopCause = "launcher_exit";
                    stopFileSeen = true;
                    return;
                }
                try {
                    Thread.sleep(200);
                } catch (InterruptedException e) {
                    return;
                }
            }
        }, "solaris-capture-stopfile");
        t.setDaemon(true);
        t.start();
    }

    // ------------------------------------------------------------------
    // Render-thread hooks
    // ------------------------------------------------------------------

    /** Minecraft#runTick HEAD. */
    public void onFrameStart(Minecraft mc) {
        frameCounter++;
        frameStartMs = EpochClock.nowMs();
        if (state == State.RECORDING) {
            reader.poll(this::onPixels, false);
        }
    }

    /** Minecraft#runTick, right before Window#updateDisplay (buffer swap). */
    public void beforeSwap(Minecraft mc) {
        renderFrames++;
        switch (state) {
            case WAITING -> {
                if (stopFileSeen) {
                    state = State.STOPPED;
                    stopReason = stopCause + "_before_start";
                    requestQuit(mc);
                } else if (mc.level != null && mc.player != null) {
                    start(mc);
                }
            }
            case RECORDING -> recordFrame(mc);
            case STOPPED -> {
                if (stopFileSeen && !quitRequested) {
                    requestQuit(mc);
                }
            }
        }
    }

    private void recordFrame(Minecraft mc) {
        double now = EpochClock.nowMs();
        if (stopFileSeen) {
            stop(mc, stopCause);
            requestQuit(mc);
            return;
        }
        if (mc.level == null) {
            // Disconnected (a dimension change replaces the level without a null gap)
            if (levelNullSinceMs < 0) {
                levelNullSinceMs = now;
            } else if (now - levelNullSinceMs > 1000.0) {
                stop(mc, "disconnect");
            }
            return;
        }
        levelNullSinceMs = -1;
        applyDebugMotion(mc, now);
        if (writer.isFailed()) {
            SolarisCapture.LOGGER.error("[solaris-capture] {}", writer.failure());
            stop(mc, "writer_failed");
            return;
        }
        long slot = (long) Math.floor((now - t0Ms) / periodMs);
        if (slot <= lastSlot) {
            return;
        }
        if (lastSlot >= 0 && slot > lastSlot + 1) {
            slotsSkipped += slot - lastSlot - 1;
        }
        lastSlot = slot;
        captureNow(mc, now, slot);
        warnLosses(now);
    }

    /**
     * At most every loss_warning_interval_s: warn when at least 1 % of the slots of
     * that interval were lost, i.e. frames dropped (ffmpeg behind) or slots skipped
     * (client stalled), so frame loss is visible while recording and not only in the
     * final meta.
     */
    private void warnLosses(double now) {
        if (cfg.loss_warning_interval_s <= 0 || now - lossCheckMs < cfg.loss_warning_interval_s * 1000.0) {
            return;
        }
        int dropped = writer.dropped();
        long skipped = slotsSkipped;
        double secs = (now - lossCheckMs) / 1000.0;
        long lost = (dropped - lossLastDropped) + (skipped - lossLastSkipped);
        if (lost > 0 && lost >= 0.01 * secs * cfg.fps) {
            lossWarnings++;
            SolarisCapture.LOGGER.warn("[solaris-capture] frame loss in the last {} s: {} frames dropped because "
                            + "ffmpeg ({}) fell behind, {} slots skipped because the client stalled; {} fps encoded "
                            + "(totals: {} dropped, {} skipped). Is the CPU overloaded?",
                    String.format(Locale.ROOT, "%.1f", secs), dropped - lossLastDropped, writer.encoder(),
                    skipped - lossLastSkipped,
                    String.format(Locale.ROOT, "%.1f", (captured - lossLastCaptured - (dropped - lossLastDropped)) / secs),
                    dropped, skipped);
        }
        lossCheckMs = now;
        lossLastDropped = dropped;
        lossLastSkipped = skipped;
        lossLastCaptured = captured;
    }

    /** Test knobs (debug_fly, debug_spin_deg_per_s); no effect with the default config. */
    private void applyDebugMotion(Minecraft mc, double now) {
        if (mc.player == null) {
            return;
        }
        if (cfg.debug_fly && mc.player.getAbilities().mayfly && !mc.player.getAbilities().flying) {
            mc.player.getAbilities().flying = true;
            mc.player.onUpdateAbilities();
            SolarisCapture.LOGGER.info("[solaris-capture] debug_fly: flying");
        }
        if (cfg.debug_spin_deg_per_s != 0.0 && lastDebugMs > 0) {
            // Entity#turn scales by 0.15 (mouse sensitivity units)
            mc.player.turn(cfg.debug_spin_deg_per_s * (now - lastDebugMs) / 1000.0 / 0.15, 0.0);
        }
        lastDebugMs = now;
    }

    private double lastDebugMs = -1;

    private void captureNow(Minecraft mc, double now, long slot) {
        FrameReader.Pending p = new FrameReader.Pending();
        p.epochMs = now;
        p.sidecarTail = sidecarTail(mc, now, slot);
        p.poseStat = lastPoseStat;
        reader.capture(mc.getMainRenderTarget(), p, this::onPixels);
        captured++;
        slotLagSumUs += Math.round((now - (t0Ms + slot * periodMs)) * 1000.0);
        if (lastCaptureMs > 0) {
            maxGapMs = Math.max(maxGapMs, now - lastCaptureMs);
        }
        lastCaptureMs = now;
    }

    private void onPixels(java.nio.ByteBuffer pixels, FrameReader.Pending p) {
        FrameWriter.Frame f = writer.acquire();
        if (f == null) {
            return; // writer behind: frame dropped (counted by the writer)
        }
        pixels.get(0, f.data, 0, f.data.length);
        f.epochMs = p.epochMs;
        f.sidecarTail = p.sidecarTail;
        f.poseStat = p.poseStat;
        writer.submit(f);
    }

    /**
     * Replacement for RenderSystem#limitDisplayFPS (Minecraft#runTick): vanilla
     * frame cap, but never below 2x the capture rate, and the wait ends early at
     * the next capture slot so that a frame is rendered right at each slot.
     * Finished readbacks are mapped while waiting.
     */
    public void limitDisplayFps(int limit) {
        int eff = Math.max(limit, state == State.RECORDING ? 2 * cfg.fps : limit);
        double now = GLFW.glfwGetTime();
        double deadline = nominalNextDraw + 1.0 / eff;
        if (state == State.RECORDING) {
            double nextSlotMs = t0Ms + (lastSlot + 1) * periodMs;
            double untilSlot = (nextSlotMs - EpochClock.nowMs()) / 1000.0;
            deadline = Math.min(deadline, now + Math.max(0.0, untilSlot));
        }
        while (now < deadline) {
            if (state == State.RECORDING) {
                reader.poll(this::onPixels, false);
            }
            double remaining = deadline - now;
            GLFW.glfwWaitEventsTimeout(Math.min(remaining, 0.004));
            now = GLFW.glfwGetTime();
        }
        nominalNextDraw = now;
    }

    // ------------------------------------------------------------------
    // Start / stop
    // ------------------------------------------------------------------

    private void start(Minecraft mc) {
        Path out = cfg.outputDir();
        videoPath = out.resolve(cfg.video_name);
        metaPath = out.resolve(cfg.meta_name);
        framesPath = out.resolve(cfg.frames_name);
        ffmpegLog = FabricLoader.getInstance().getGameDir().resolve("logs")
                .resolve("solaris-capture-ffmpeg-" + cfg.video_name.replaceFirst("\\.[^.]+$", "") + ".log");
        try {
            Files.createDirectories(out);
            Files.createDirectories(ffmpegLog.getParent());
            Files.deleteIfExists(ffmpegLog);
            String encoder = chooseEncoder();
            reader = new FrameReader(cfg.width, cfg.height, cfg.pbo_count);
            reader.init();
            writer = new FrameWriter(cfg, encoder, ffmpegLog);
            writer.start(videoPath, framesPath);
        } catch (IOException | RuntimeException e) {
            SolarisCapture.LOGGER.error("[solaris-capture] cannot start recording", e);
            if (reader != null) {
                reader.destroy();
            }
            state = State.STOPPED;
            stopReason = "start_failed: " + e;
            return;
        }
        if (cfg.hide_window && !windowHidden) {
            GLFW.glfwHideWindow(mc.getWindow().getWindow());
            windowHidden = true;
        }
        RenderTarget main = mc.getMainRenderTarget();
        if (main.width != cfg.width || main.height != cfg.height) {
            SolarisCapture.LOGGER.warn("[solaris-capture] framebuffer {}x{} differs from the configured {}x{}; frames are scaled",
                    main.width, main.height, cfg.width, cfg.height);
        }
        t0Ms = EpochClock.nowMs();
        lastSlot = -1;
        lossCheckMs = t0Ms;
        meta = buildMeta(mc, t0Ms);
        writeMeta();
        state = State.RECORDING;
        SolarisCapture.LOGGER.info("[solaris-capture] recording {} ({}x{} @ {} fps, {}, timestamps={}, GL renderer {})",
                videoPath, cfg.width, cfg.height, cfg.fps, writer.encoder(), cfg.timestamps, GlUtil.getRenderer());
        recordFrame(mc);
    }

    /** encoder "auto": run the NVENC test encode in the background while the client starts. */
    private void startEncoderProbe() {
        String enc = cfg.encoder == null ? "auto" : cfg.encoder.toLowerCase(Locale.ROOT);
        if (!enc.equals("auto")) {
            encoderProbe = CompletableFuture.completedFuture(enc);
            return;
        }
        Path logs = FabricLoader.getInstance().getGameDir().resolve("logs");
        encoderProbe = CompletableFuture.supplyAsync(() -> {
            try {
                Files.createDirectories(logs);
            } catch (IOException ignored) {
                // the probe then runs without a log
            }
            if (FrameWriter.probeEncoder(cfg.ffmpeg, "h264_nvenc", logs.resolve("solaris-capture-nvenc-probe.log").toFile())) {
                return "h264_nvenc";
            }
            SolarisCapture.LOGGER.warn("[solaris-capture] NVENC test encode failed; using libx264");
            return "libx264";
        });
    }

    private String chooseEncoder() {
        try {
            return encoderProbe.get(30, TimeUnit.SECONDS);
        } catch (InterruptedException e) {
            Thread.currentThread().interrupt();
            return "libx264";
        } catch (ExecutionException | TimeoutException e) {
            SolarisCapture.LOGGER.warn("[solaris-capture] encoder probe failed ({}); using libx264", e.toString());
            return "libx264";
        }
    }

    /** Render thread: map the remaining readbacks, drain the writer, finalize the MKV and meta. */
    private void stop(Minecraft mc, String reason) {
        if (state != State.RECORDING) {
            return;
        }
        state = State.STOPPED;
        stopReason = reason;
        SolarisCapture.LOGGER.info("[solaris-capture] stopping recording ({})", reason);
        try {
            reader.poll(this::onPixels, true);
        } catch (RuntimeException e) {
            SolarisCapture.LOGGER.error("[solaris-capture] final readback failed", e);
        }
        reader.destroy();
        finishWriter(mc);
    }

    private synchronized void finishWriter(Minecraft mc) {
        if (finished || writer == null) {
            return;
        }
        boolean ok = writer.close(90_000);
        if (!ok) {
            SolarisCapture.LOGGER.error("[solaris-capture] writer did not finish in time");
        }
        double stopMs = EpochClock.nowMs();
        if (mc != null) {
            meta.put("options", readOptions(mc));
        }
        meta.put("frames", writer.written());
        meta.put("stop_epoch_seconds", stopMs / 1000.0);
        meta.put("stop_reason", stopReason);
        Map<String, Object> stats = new LinkedHashMap<>();
        stats.put("captured", captured);
        stats.put("encoded", writer.written());
        stats.put("dropped_writer_backlog", writer.dropped());
        stats.put("slots_skipped", slotsSkipped);
        stats.put("slot_lag_ms_mean", captured > 0 ? slotLagSumUs / 1000.0 / captured : null);
        stats.put("max_capture_gap_ms", maxGapMs);
        stats.put("forced_readback_waits", reader.forcedWaits);
        double secs = (stopMs - t0Ms) / 1000.0;
        stats.put("duration_seconds", secs);
        stats.put("mean_capture_fps", secs > 0 ? captured / secs : null);
        stats.put("render_frames", renderFrames);
        stats.put("ffmpeg_exit_code", writer.ffmpegExit());
        stats.put("loss_warnings", lossWarnings);
        meta.put("capture_stats", stats);
        meta.put("pose_link", PoseLink.meta(cfg));
        meta.put("toasts", CleanFrames.meta());
        meta.put("local_skins", LocalSkins.meta());
        writeMeta();
        finished = true;
        SolarisCapture.LOGGER.info("[solaris-capture] recording finished: {} frames encoded, {} dropped, {} slots skipped",
                writer.written(), writer.dropped(), slotsSkipped);
    }

    private void requestQuit(Minecraft mc) {
        quitRequested = true;
        SolarisCapture.LOGGER.info("[solaris-capture] {}: quitting Minecraft", stopCause);
        mc.stop();
    }

    /** JVM shutdown (window closed, Ctrl+C, normal exit): finish the writer; in-flight PBOs are lost. */
    private void onShutdown() {
        if (state == State.RECORDING) {
            state = State.STOPPED;
            stopReason = "jvm_shutdown";
            finishWriter(null);
        }
    }

    // ------------------------------------------------------------------
    // Sidecar and meta
    // ------------------------------------------------------------------

    private static String num(double v, int decimals) {
        if (Double.isNaN(v) || Double.isInfinite(v)) {
            return "null";
        }
        return String.format(Locale.ROOT, "%." + decimals + "f", v);
    }

    private static String vec(Vec3 v, int decimals) {
        return "[" + num(v.x, decimals) + ", " + num(v.y, decimals) + ", " + num(v.z, decimals) + "]";
    }

    private static String jsonString(String s) {
        if (s == null) {
            return "null";
        }
        StringBuilder b = new StringBuilder(s.length() + 2).append('"');
        for (char c : s.toCharArray()) {
            switch (c) {
                case '"' -> b.append("\\\"");
                case '\\' -> b.append("\\\\");
                default -> {
                    if (c < 0x20) {
                        b.append(String.format("\\u%04x", (int) c));
                    } else {
                        b.append(c);
                    }
                }
            }
        }
        return b.append('"').toString();
    }

    /** Pose-link outcome of the frame whose sidecar tail was built last (render thread). */
    private PoseLink.FrameStat lastPoseStat;

    private String sidecarTail(Minecraft mc, double now, long slot) {
        Camera cam = mc.gameRenderer.getMainCamera();
        float partial = mc.getTimer().getGameTimeDeltaPartialTick(true);
        double fov = worldFovFrame == frameCounter ? worldFov : Double.NaN;
        StringBuilder b = new StringBuilder(512);
        b.append("\"epoch_ms\": ").append(num(now, 3));
        b.append(", \"slot\": ").append(slot);
        b.append(", \"render_epoch_ms\": ").append(num(frameStartMs, 3));
        b.append(", \"game_time\": ").append(mc.level != null ? Long.toString(mc.level.getGameTime()) : "null");
        b.append(", \"partial_tick\": ").append(num(partial, 4));
        b.append(", \"cam_pos\": ").append(vec(cam.getPosition(), 5));
        b.append(", \"cam_yaw\": ").append(num(cam.getYRot(), 4));
        b.append(", \"cam_pitch\": ").append(num(cam.getXRot(), 4));
        b.append(", \"fov\": ").append(num(fov, 4));
        if (mc.player != null) {
            b.append(", \"player_pos\": ").append(vec(mc.player.getPosition(partial), 5));
        } else {
            b.append(", \"player_pos\": null");
        }
        Entity camEntity = mc.getCameraEntity();
        String spectating = camEntity != null && camEntity != mc.player ? camEntity.getName().getString() : null;
        b.append(", \"camera_entity\": ").append(jsonString(spectating));
        RenderTarget main = mc.getMainRenderTarget();
        b.append(", \"fb\": [").append(main.width).append(", ").append(main.height).append(']');
        b.append(", \"gui_hidden\": ").append(mc.options.hideGui);
        b.append(", \"screen\": ").append(jsonString(ScreenNames.of(mc.screen)));
        PoseLink link = PoseLink.get();
        if (link != null) {
            lastPoseStat = link.appendSidecar(b, frameCounter);
        } else {
            lastPoseStat = null;
            PoseLink.appendSidecarDisabled(b, mc, partial);
        }
        b.append('}');
        return b.toString();
    }

    private Map<String, Object> buildMeta(Minecraft mc, double startMs) {
        Map<String, Object> m = new LinkedHashMap<>();
        m.put("recording_path", videoPath.toString());
        m.put("start_epoch_seconds", startMs / 1000.0);
        m.put("start_time_utc", Instant.ofEpochMilli((long) Math.floor(startMs)).toString());
        m.put("fps", cfg.fps);
        m.put("width", cfg.width);
        m.put("height", cfg.height);
        m.put("camera_name", cfg.camera_name);
        m.put("video_encoder", writer.encoder());
        m.put("wallclock_timestamps", true);
        m.put("timestamp_source", cfg.captureTimestamps() ? "capture_time" : "ffmpeg_wallclock_at_pipe");
        m.put("capture_method", "in_client_fbo_readback");
        m.put("frames_file", framesPath.getFileName().toString());
        m.put("platform", WinPower.isWindows() ? "windows" : System.getProperty("os.name", "").toLowerCase(Locale.ROOT));
        m.put("mc_version", SharedConstants.getCurrentVersion().getName());
        m.put("fabric_loader_version", modVersion("fabricloader"));
        m.put("mod_version", modVersion(SolarisCapture.MOD_ID));
        RenderTarget main = mc.getMainRenderTarget();
        m.put("framebuffer", List.of(main.width, main.height));
        Window w = mc.getWindow();
        m.put("window_size", List.of(w.getScreenWidth(), w.getScreenHeight()));
        m.put("window_hidden", windowHidden);
        m.put("process_priority", WinPower.isWindows() && cfg.windows_power_tweaks ? cfg.process_priority : null);
        m.put("gl_renderer", GlUtil.getRenderer());
        m.put("gl_vendor", GlUtil.getVendor());
        m.put("gl_version", GlUtil.getOpenGLVersion());
        m.put("server", cfg.server);
        m.put("ffmpeg_command", writer.command());
        m.put("note", "MKV PTS values are absolute Unix epoch timestamps. Use ffprobe to extract per-frame times; "
                + "frames_file has one line per encoded frame (same order) with the rendered camera pose.");
        m.put("options", readOptions(mc));
        m.put("pose_link", PoseLink.meta(cfg));
        m.put("toasts", CleanFrames.meta());
        m.put("local_skins", LocalSkins.meta());
        return m;
    }

    private static String modVersion(String id) {
        return FabricLoader.getInstance().getModContainer(id)
                .map(c -> c.getMetadata().getVersion().getFriendlyString()).orElse(null);
    }

    static Map<String, Object> readOptions(Minecraft mc) {
        Options o = mc.options;
        Map<String, Object> m = new LinkedHashMap<>();
        m.put("fov_deg", o.fov().get());
        m.put("fov_effect_scale", o.fovEffectScale().get());
        m.put("render_distance", o.renderDistance().get());
        m.put("simulation_distance", o.simulationDistance().get());
        int server = ((OptionsAccessor) o).solaris$getServerRenderDistance();
        m.put("server_view_distance", server > 0 ? server : null);
        m.put("effective_render_distance", o.getEffectiveRenderDistance());
        m.put("gui_scale", o.guiScale().get());
        m.put("gui_scale_effective", mc.getWindow().getGuiScale());
        m.put("bob_view", o.bobView().get());
        m.put("damage_tilt_strength", o.damageTiltStrength().get());
        m.put("screen_effect_scale", o.screenEffectScale().get());
        m.put("darkness_effect_scale", o.darknessEffectScale().get());
        m.put("entity_shadows", o.entityShadows().get());
        m.put("clouds", o.cloudStatus().get().name().toLowerCase(Locale.ROOT));
        m.put("graphics_mode", o.graphicsMode().get().name().toLowerCase(Locale.ROOT));
        m.put("particles", o.particles().get().name().toLowerCase(Locale.ROOT));
        m.put("max_fps", o.framerateLimit().get());
        m.put("enable_vsync", o.enableVsync().get());
        m.put("smooth_camera", o.smoothCamera);
        m.put("pause_on_lost_focus", o.pauseOnLostFocus);
        m.put("hud_visible", !o.hideGui);
        m.put("name_tags_visible", true);
        // clean frames: toasts never drawn, chat hidden (action bar / titles / hotbar stay)
        m.put("toasts_suppressed", CleanFrames.suppressToasts());
        m.put("chat_visibility", o.chatVisibility().get().name().toLowerCase(Locale.ROOT));
        return m;
    }

    private void writeMeta() {
        try {
            Path tmp = metaPath.resolveSibling(metaPath.getFileName() + ".tmp");
            Files.writeString(tmp, new GsonBuilder().setPrettyPrinting().serializeNulls().create().toJson(meta),
                    StandardCharsets.UTF_8);
            Files.move(tmp, metaPath, StandardCopyOption.REPLACE_EXISTING, StandardCopyOption.ATOMIC_MOVE);
        } catch (IOException e) {
            SolarisCapture.LOGGER.error("[solaris-capture] cannot write {}: {}", metaPath, e.toString());
        }
    }
}
