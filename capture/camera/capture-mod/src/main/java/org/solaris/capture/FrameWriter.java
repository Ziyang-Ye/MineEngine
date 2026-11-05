package org.solaris.capture;

import java.io.BufferedWriter;
import java.io.File;
import java.io.IOException;
import java.io.OutputStream;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.ArrayList;
import java.util.List;
import java.util.concurrent.ArrayBlockingQueue;
import java.util.concurrent.BlockingQueue;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicInteger;
import java.util.concurrent.atomic.AtomicLong;

/**
 * Writer thread: pipes raw RGBA frames (bottom-up rows, flipped by ffmpeg) into
 * ffmpeg's stdin and appends one sidecar line per frame <em>after</em> the frame
 * was handed to ffmpeg, so the sidecar line count equals the encoded frame count.
 * Pixel buffers come from a fixed pool; when ffmpeg falls behind and the pool is
 * empty the render thread drops the frame (counted) instead of blocking.
 */
final class FrameWriter {
    static final class Frame {
        final byte[] data;
        double epochMs;
        /** Sidecar JSON without the leading {"i": n, (added here). */
        String sidecarTail;
        /** PoseLink.FrameStat, counted once the frame is encoded (null without a pose link) */
        Object poseStat;

        Frame(int size) {
            this.data = new byte[size];
        }
    }

    private static final Frame POISON = new Frame(0);

    private final CaptureConfig cfg;
    private final int frameBytes;
    private final BlockingQueue<Frame> free;
    private final BlockingQueue<Frame> queue;
    private final CountDownLatch done = new CountDownLatch(1);
    private final AtomicLong written = new AtomicLong();
    private final AtomicInteger dropped = new AtomicInteger();
    private volatile boolean failed = false;
    private volatile String failure = null;
    private volatile boolean closing = false;
    private Process ffmpeg;
    private OutputStream stdin;
    private BufferedWriter sidecar;
    private Thread thread;
    private final String encoder;
    private final List<String> command = new ArrayList<>();
    private final Path ffmpegLog;
    private volatile Integer ffmpegExit = null;

    FrameWriter(CaptureConfig cfg, String encoder, Path ffmpegLog) {
        this.cfg = cfg;
        this.encoder = encoder;
        this.ffmpegLog = ffmpegLog;
        this.frameBytes = cfg.width * cfg.height * 4;
        this.free = new ArrayBlockingQueue<>(cfg.queue_frames);
        this.queue = new ArrayBlockingQueue<>(cfg.queue_frames + 1);
        for (int i = 0; i < cfg.queue_frames; i++) {
            free.add(new Frame(frameBytes));
        }
    }

    List<String> buildCommand(Path video) {
        List<String> c = new ArrayList<>();
        c.add(cfg.ffmpeg);
        c.add("-hide_banner");
        c.add("-nostats");
        c.add("-loglevel");
        c.add("info");
        c.add("-y");
        if (cfg.captureTimestamps()) {
            // Our Matroska stream carries the capture timestamps
            c.add("-f");
            c.add("matroska");
        } else {
            c.add("-f");
            c.add("rawvideo");
            c.add("-pix_fmt");
            c.add("rgba");
            c.add("-s");
            c.add(cfg.width + "x" + cfg.height);
            c.add("-framerate");
            c.add(Integer.toString(cfg.fps));
            c.add("-use_wallclock_as_timestamps");
            c.add("1");
        }
        c.add("-i");
        c.add("-");
        c.add("-vf");
        c.add("vflip");
        c.add("-copyts");
        c.add("-fps_mode");
        c.add("passthrough");
        if (cfg.captureTimestamps()) {
            // Default encoder time base is 1/fps, which would snap the PTS to the slot grid
            c.add("-enc_time_base");
            c.add("1:1000");
        }
        c.add("-c:v");
        c.add(encoder);
        if ("h264_nvenc".equals(encoder)) {
            c.add("-preset");
            c.add("p4");
        } else {
            c.add("-preset");
            c.add(cfg.x264_preset == null || cfg.x264_preset.isEmpty() ? "veryfast" : cfg.x264_preset);
        }
        c.add("-bf");
        c.add("0");
        c.add("-pix_fmt");
        c.add("yuv420p");
        c.add(video.toString());
        return c;
    }

    void start(Path video, Path framesFile) throws IOException {
        Files.createDirectories(video.toAbsolutePath().getParent());
        command.addAll(buildCommand(video));
        ProcessBuilder pb = new ProcessBuilder(command);
        pb.redirectOutput(ProcessBuilder.Redirect.DISCARD);
        if (ffmpegLog != null) {
            Files.createDirectories(ffmpegLog.getParent());
            pb.redirectError(ffmpegLog.toFile());
        } else {
            pb.redirectError(ProcessBuilder.Redirect.DISCARD);
        }
        SolarisCapture.LOGGER.info("[solaris-capture] ffmpeg: {}", String.join(" ", command));
        ffmpeg = pb.start();
        if (cfg.windows_power_tweaks) {
            // The encoder must keep up in real time: same priority class as the client
            WinPower.disableThrottling(ffmpeg.pid(), cfg.process_priority);
        }
        stdin = ffmpeg.getOutputStream();
        if (cfg.captureTimestamps()) {
            stdin.write(MkvPipe.header(cfg.width, cfg.height, cfg.fps));
        }
        sidecar = Files.newBufferedWriter(framesFile, StandardCharsets.UTF_8);
        thread = new Thread(this::run, "solaris-capture-writer");
        thread.setDaemon(true);
        thread.setPriority(Thread.NORM_PRIORITY + 1);
        thread.start();
    }

    List<String> command() {
        return command;
    }

    /** A free pixel buffer, or null when the writer is behind (frame dropped). */
    Frame acquire() {
        if (failed || closing) {
            return null;
        }
        Frame f = free.poll();
        if (f == null) {
            dropped.incrementAndGet();
        }
        return f;
    }

    void release(Frame f) {
        free.offer(f);
    }

    void submit(Frame f) {
        if (failed || closing) {
            release(f);
            return;
        }
        if (!queue.offer(f)) {
            // Cannot happen (queue capacity > pool size); keep the pool intact anyway.
            dropped.incrementAndGet();
            release(f);
        }
    }

    private void run() {
        try {
            while (true) {
                Frame f = queue.take();
                if (f == POISON) {
                    break;
                }
                try {
                    if (!failed) {
                        if (cfg.captureTimestamps()) {
                            stdin.write(MkvPipe.frameHeader(f.epochMs, frameBytes));
                        }
                        stdin.write(f.data, 0, frameBytes);
                        long i = written.getAndIncrement();
                        sidecar.write("{\"i\": " + i + ", " + f.sidecarTail);
                        sidecar.write('\n');
                        sidecar.flush();
                        PoseLink.countEncoded(f.poseStat);
                    }
                } catch (IOException e) {
                    fail("ffmpeg pipe closed: " + e);
                } finally {
                    release(f);
                }
            }
        } catch (InterruptedException e) {
            Thread.currentThread().interrupt();
        } finally {
            finishProcess();
            done.countDown();
        }
    }

    private void fail(String why) {
        if (!failed) {
            failed = true;
            failure = why;
            SolarisCapture.LOGGER.error("[solaris-capture] writer failed: {}", why);
        }
    }

    private void finishProcess() {
        try {
            stdin.flush();
        } catch (IOException ignored) {
            // ffmpeg already gone
        }
        try {
            stdin.close();
        } catch (IOException ignored) {
            // ffmpeg already gone
        }
        try {
            sidecar.flush();
            sidecar.close();
        } catch (IOException e) {
            SolarisCapture.LOGGER.error("[solaris-capture] sidecar close failed: {}", e.toString());
        }
        try {
            if (ffmpeg.waitFor(60, TimeUnit.SECONDS)) {
                ffmpegExit = ffmpeg.exitValue();
            } else {
                SolarisCapture.LOGGER.error("[solaris-capture] ffmpeg did not exit within 60 s; killing it");
                ffmpeg.destroyForcibly();
                ffmpegExit = -1;
            }
        } catch (InterruptedException e) {
            Thread.currentThread().interrupt();
        }
        SolarisCapture.LOGGER.info("[solaris-capture] ffmpeg exited with {} after {} frames", ffmpegExit, written.get());
    }

    /** Stop accepting frames, let the queue drain, close ffmpeg's stdin and wait for it. */
    boolean close(long timeoutMs) {
        if (!closing) {
            closing = true;
            queue.offer(POISON);
        }
        try {
            return done.await(timeoutMs, TimeUnit.MILLISECONDS);
        } catch (InterruptedException e) {
            Thread.currentThread().interrupt();
            return false;
        }
    }

    boolean isFailed() {
        return failed || (ffmpeg != null && !ffmpeg.isAlive() && !closing);
    }

    String failure() {
        if (failure == null && ffmpeg != null && !ffmpeg.isAlive() && !closing) {
            return "ffmpeg exited early with code " + ffmpeg.exitValue() + " (see " + ffmpegLog + ")";
        }
        return failure;
    }

    long written() {
        return written.get();
    }

    int dropped() {
        return dropped.get();
    }

    Integer ffmpegExit() {
        return ffmpegExit;
    }

    String encoder() {
        return encoder;
    }

    /**
     * One-frame test encode; true when the encoder works on this machine. ffmpeg
     * listing h264_nvenc only proves it is compiled in: the GPU may lack NVENC, or
     * the driver may be older than the NVENC API of the ffmpeg build.
     */
    static boolean probeEncoder(String ffmpeg, String encoder, File log) {
        List<String> c = List.of(ffmpeg, "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i",
                "color=black:s=256x256:r=10", "-frames:v", "1", "-c:v", encoder, "-f", "null", "-");
        try {
            ProcessBuilder pb = new ProcessBuilder(c);
            pb.redirectOutput(ProcessBuilder.Redirect.DISCARD);
            pb.redirectError(log != null ? ProcessBuilder.Redirect.to(log) : ProcessBuilder.Redirect.DISCARD);
            Process p = pb.start();
            if (!p.waitFor(20, TimeUnit.SECONDS)) {
                p.destroyForcibly();
                return false;
            }
            if (p.exitValue() != 0 && log != null) {
                try {
                    List<String> lines = java.nio.file.Files.readAllLines(log.toPath());
                    SolarisCapture.LOGGER.warn("[solaris-capture] {} test encode failed: {}", encoder,
                            lines.isEmpty() ? "?" : String.join(" | ", lines.subList(0, Math.min(2, lines.size()))));
                } catch (IOException ignored) {
                    // log only
                }
            }
            return p.exitValue() == 0;
        } catch (IOException e) {
            SolarisCapture.LOGGER.error("[solaris-capture] cannot run {}: {}", ffmpeg, e.toString());
            return false;
        } catch (InterruptedException e) {
            Thread.currentThread().interrupt();
            return false;
        }
    }
}
