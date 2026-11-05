package org.solaris.capture;

import com.google.gson.Gson;
import com.google.gson.JsonObject;
import com.google.gson.JsonParser;
import java.io.IOException;
import java.io.Reader;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.nio.file.Paths;

/**
 * {@code <game-dir>/config/solaris-capture.json} (windows/CONTRACT.md section 5.3),
 * written by camera/windows/launch_camera.py. Missing keys keep the defaults below.
 */
public final class CaptureConfig {
    public String camera_name = "Camera";
    public String output_dir = "recordings";
    public String video_name = "camera.mkv";
    public String meta_name = null;
    public String frames_name = null;
    public String ffmpeg = "ffmpeg";
    /** auto | h264_nvenc | libx264 */
    public String encoder = "auto";
    public int fps = 20;
    public int width = 1280;
    public int height = 720;
    public boolean hide_window = true;
    /**
     * Create the window hidden (never shown, never focused); only with hide_window.
     * When false the window is visible until the player is in a world.
     */
    public boolean hide_at_startup = true;
    public String stop_file = null;
    public String server = null;
    /**
     * MKV PTS source. "capture" (default): a minimal Matroska stream on the pipe
     * carries each frame's capture time (= sidecar epoch_ms), PTS exact to 1 ms.
     * "wallclock": the contract's rawvideo command with -use_wallclock_as_timestamps;
     * ffmpeg's rawvideo demuxer has a 1/fps time base, so those PTS are arrival times
     * snapped to a 50 ms grid (and collide when two frames arrive within one cell).
     */
    public String timestamps = "capture";
    /** Number of PBOs in the readback ring (>= 3). */
    public int pbo_count = 4;
    /** Raw frame buffers between the render thread and the writer thread. */
    public int queue_frames = 8;
    /** Disable Windows power throttling / EcoQoS and request a 1 ms timer (Windows only). */
    public boolean windows_power_tweaks = true;
    /**
     * Windows priority class of the client and of ffmpeg: normal | above_normal | high.
     * above_normal keeps the 20 Hz capture and the encoder on time when other
     * processes (server, bots, a second camera) load the CPU.
     */
    public String process_priority = "above_normal";
    /** libx264 preset when NVENC is not used (veryfast per contract; ultrafast is ~25 % cheaper). */
    public String x264_preset = "veryfast";
    /**
     * Check frame loss this often while recording and log a warning when >= 1 % of
     * the slots were lost (dropped or skipped) in that interval (0 = never).
     */
    public double loss_warning_interval_s = 10.0;
    /**
     * PID of camera/windows/launch_camera.py (0 = none). When that process dies
     * (killed without a stop file) the mod stops recording cleanly and quits, as for
     * the stop file, so no client is left recording without its launcher.
     */
    public long launcher_pid = 0;
    /**
     * T12 pose link: UDP port on 127.0.0.1 where the bot's controller
     * (controller/recorder/camera-pose-link.js, env CAMERA_POSE_PORT) sends one datagram
     * per movement packet. The render camera then uses that pose directly instead of
     * the server-followed camera player (0 = off).
     */
    public int pose_port = 0;
    /** A pose-link pose received longer ago than this is stale (server-followed camera instead). */
    public double pose_max_age_ms = 500.0;
    /**
     * Keep using a stale pose while the camera player's server-followed pose still equals
     * it (the bot is idle: Mineflayer then sends only one position packet per second).
     */
    public boolean pose_hold_when_matching = true;
    /**
     * Injected eye farther than this from the vanilla camera position (blocks) is not
     * used (teleports: the chunks around the new position may not be loaded yet).
     */
    public double pose_max_distance = 16.0;
    /**
     * Clean frames: drop every toast (advancement, recipe, tutorial, system) before it is
     * queued, so none is ever drawn (ToastComponentMixin).
     */
    public boolean suppress_toasts = true;
    /**
     * Chat visibility the launcher wrote to options.txt (chatVisibility: full | system |
     * hidden); informational, the live option is recorded in the meta.
     */
    public String chat_visibility = null;
    /**
     * Local skins: render players whose (unsigned) textures property names a skin of the
     * local pool from {@link #skins_dir} instead of requiring a Mojang-signed texture.
     */
    public boolean local_skins = true;
    /** Directory with the pool PNGs (<skin_id>.png); legacy skins are looked up in its parent too. */
    public String skins_dir = null;
    /** Test only: start flying once recording (needs creative / mayfly). */
    public boolean debug_fly = false;
    /** Test only: turn the camera player by this many degrees per second while recording. */
    public double debug_spin_deg_per_s = 0.0;

    /** Absent when the file could not be read (the mod then stays passive). */
    public transient boolean loaded = false;
    public transient Path path;

    public static CaptureConfig load(Path gameDir) {
        Path path = gameDir.resolve("config").resolve("solaris-capture.json");
        CaptureConfig cfg = new CaptureConfig();
        if (Files.isRegularFile(path)) {
            try (Reader r = Files.newBufferedReader(path, StandardCharsets.UTF_8)) {
                JsonObject obj = JsonParser.parseReader(r).getAsJsonObject();
                cfg = new Gson().fromJson(obj, CaptureConfig.class);
                cfg.loaded = true;
            } catch (IOException | RuntimeException e) {
                SolarisCapture.LOGGER.error("[solaris-capture] cannot read {}: {}", path, e.toString());
            }
        } else {
            SolarisCapture.LOGGER.warn("[solaris-capture] no config at {}; capture disabled", path);
        }
        cfg.path = path;
        String stem = cfg.video_name.replaceFirst("\\.[^.]+$", "");
        if (cfg.meta_name == null || cfg.meta_name.isEmpty()) {
            cfg.meta_name = stem + "_meta.json";
        }
        if (cfg.frames_name == null || cfg.frames_name.isEmpty()) {
            cfg.frames_name = stem + "_frames.jsonl";
        }
        cfg.fps = Math.max(1, cfg.fps);
        cfg.pbo_count = Math.max(3, cfg.pbo_count);
        cfg.queue_frames = Math.max(2, cfg.queue_frames);
        return cfg;
    }

    public Path outputDir() {
        return Paths.get(output_dir).toAbsolutePath();
    }

    public Path stopFile() {
        return stop_file == null || stop_file.isEmpty() ? null : Paths.get(stop_file).toAbsolutePath();
    }

    public boolean captureTimestamps() {
        return "capture".equalsIgnoreCase(timestamps);
    }
}
