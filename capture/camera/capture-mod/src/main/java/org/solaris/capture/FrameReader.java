package org.solaris.capture;

import com.mojang.blaze3d.pipeline.RenderTarget;
import com.mojang.blaze3d.platform.GlStateManager;
import java.nio.ByteBuffer;
import java.util.ArrayDeque;
import org.lwjgl.opengl.GL11;
import org.lwjgl.opengl.GL15;
import org.lwjgl.opengl.GL30;
import org.lwjgl.opengl.GL32;

/**
 * Asynchronous readback of the main render target (render thread only).
 *
 * <p>Each capture blits the main target into a WxH capture framebuffer (a plain
 * copy when the sizes match, scaled otherwise), starts {@code glReadPixels} into
 * the next PBO of a ring and inserts a fence. {@link #poll} maps PBOs whose fence
 * has signalled, in capture order, without waiting; only when every PBO of the
 * ring is in flight does a capture wait for the oldest one.
 */
final class FrameReader {
    private static final int GL_READ_FRAMEBUFFER = 0x8CA8;
    private static final int GL_DRAW_FRAMEBUFFER = 0x8CA9;
    private static final int GL_PIXEL_PACK_BUFFER = 0x88EB;

    interface Sink {
        /** Called in capture order with the mapped pixels (valid only during the call). */
        void accept(ByteBuffer pixels, Pending meta);
    }

    static final class Pending {
        int pbo;
        long fence;
        double epochMs;
        String sidecarTail;
        /** PoseLink.FrameStat of this frame (null without a pose link) */
        Object poseStat;
    }

    private final int width;
    private final int height;
    private final int bytes;
    private final int[] pbos;
    private final ArrayDeque<Pending> inFlight = new ArrayDeque<>();
    private final ArrayDeque<Integer> freePbos = new ArrayDeque<>();
    private int fbo = 0;
    private int rbo = 0;
    long forcedWaits = 0;

    FrameReader(int width, int height, int ringSize) {
        this.width = width;
        this.height = height;
        this.bytes = width * height * 4;
        this.pbos = new int[ringSize];
    }

    void init() {
        rbo = GL30.glGenRenderbuffers();
        GL30.glBindRenderbuffer(GL30.GL_RENDERBUFFER, rbo);
        GL30.glRenderbufferStorage(GL30.GL_RENDERBUFFER, GL11.GL_RGBA8, width, height);
        GL30.glBindRenderbuffer(GL30.GL_RENDERBUFFER, 0);
        fbo = GL30.glGenFramebuffers();
        GlStateManager._glBindFramebuffer(GL30.GL_FRAMEBUFFER, fbo);
        GL30.glFramebufferRenderbuffer(GL30.GL_FRAMEBUFFER, GL30.GL_COLOR_ATTACHMENT0, GL30.GL_RENDERBUFFER, rbo);
        int status = GL30.glCheckFramebufferStatus(GL30.GL_FRAMEBUFFER);
        GlStateManager._glBindFramebuffer(GL30.GL_FRAMEBUFFER, 0);
        if (status != GL30.GL_FRAMEBUFFER_COMPLETE) {
            throw new IllegalStateException("capture framebuffer incomplete: 0x" + Integer.toHexString(status));
        }
        for (int i = 0; i < pbos.length; i++) {
            pbos[i] = GL15.glGenBuffers();
            GL15.glBindBuffer(GL_PIXEL_PACK_BUFFER, pbos[i]);
            GL15.glBufferData(GL_PIXEL_PACK_BUFFER, bytes, GL15.GL_STREAM_READ);
            freePbos.add(pbos[i]);
        }
        GL15.glBindBuffer(GL_PIXEL_PACK_BUFFER, 0);
    }

    /**
     * Start the readback of the current main target. Call right before the buffer
     * swap (the main target then holds the finished frame incl. HUD).
     */
    void capture(RenderTarget main, Pending meta, Sink sink) {
        if (freePbos.isEmpty()) {
            // Whole ring in flight: wait for the oldest (rare; logged as forced wait)
            forcedWaits++;
            mapOldest(sink, true);
        }
        int pbo = freePbos.poll();
        GlStateManager._disableScissorTest();
        GlStateManager._glBindFramebuffer(GL_READ_FRAMEBUFFER, main.frameBufferId);
        GlStateManager._glBindFramebuffer(GL_DRAW_FRAMEBUFFER, fbo);
        boolean sameSize = main.width == width && main.height == height;
        GL30.glBlitFramebuffer(0, 0, main.width, main.height, 0, 0, width, height,
                GL11.GL_COLOR_BUFFER_BIT, sameSize ? GL11.GL_NEAREST : GL11.GL_LINEAR);
        GlStateManager._glBindFramebuffer(GL_READ_FRAMEBUFFER, fbo);
        GL11.glReadBuffer(GL30.GL_COLOR_ATTACHMENT0);
        GL15.glBindBuffer(GL_PIXEL_PACK_BUFFER, pbo);
        GL11.glPixelStorei(GL11.GL_PACK_ROW_LENGTH, 0);
        GL11.glPixelStorei(GL11.GL_PACK_SKIP_PIXELS, 0);
        GL11.glPixelStorei(GL11.GL_PACK_SKIP_ROWS, 0);
        GL11.glPixelStorei(GL11.GL_PACK_ALIGNMENT, 4);
        GL11.glReadPixels(0, 0, width, height, GL11.GL_RGBA, GL11.GL_UNSIGNED_BYTE, 0L);
        GL15.glBindBuffer(GL_PIXEL_PACK_BUFFER, 0);
        // Main target was unbound (framebuffer 0) before the blit-to-screen; restore that
        GlStateManager._glBindFramebuffer(GL30.GL_FRAMEBUFFER, 0);
        meta.pbo = pbo;
        meta.fence = GL32.glFenceSync(GL32.GL_SYNC_GPU_COMMANDS_COMPLETE, 0);
        inFlight.add(meta);
    }

    /** Map every finished readback (non-blocking unless {@code block}). */
    void poll(Sink sink, boolean block) {
        while (!inFlight.isEmpty()) {
            if (!mapOldest(sink, block)) {
                return;
            }
        }
    }

    private boolean mapOldest(Sink sink, boolean block) {
        Pending p = inFlight.peek();
        if (p == null) {
            return false;
        }
        int r = GL32.glClientWaitSync(p.fence, block ? GL32.GL_SYNC_FLUSH_COMMANDS_BIT : 0,
                block ? 1_000_000_000L : 0L);
        if (r == GL32.GL_TIMEOUT_EXPIRED || (r == GL32.GL_WAIT_FAILED && !block)) {
            return false;
        }
        inFlight.poll();
        GL32.glDeleteSync(p.fence);
        GL15.glBindBuffer(GL_PIXEL_PACK_BUFFER, p.pbo);
        ByteBuffer mapped = GL30.glMapBufferRange(GL_PIXEL_PACK_BUFFER, 0, bytes, GL30.GL_MAP_READ_BIT);
        if (mapped != null) {
            try {
                sink.accept(mapped, p);
            } finally {
                GL15.glUnmapBuffer(GL_PIXEL_PACK_BUFFER);
            }
        } else {
            SolarisCapture.LOGGER.error("[solaris-capture] glMapBufferRange returned null; frame lost");
        }
        GL15.glBindBuffer(GL_PIXEL_PACK_BUFFER, 0);
        freePbos.add(p.pbo);
        return true;
    }

    int inFlight() {
        return inFlight.size();
    }

    void destroy() {
        for (Pending p : inFlight) {
            GL32.glDeleteSync(p.fence);
        }
        inFlight.clear();
        for (int pbo : pbos) {
            if (pbo != 0) {
                GL15.glDeleteBuffers(pbo);
            }
        }
        if (fbo != 0) {
            GL30.glDeleteFramebuffers(fbo);
        }
        if (rbo != 0) {
            GL30.glDeleteRenderbuffers(rbo);
        }
        fbo = rbo = 0;
    }
}
