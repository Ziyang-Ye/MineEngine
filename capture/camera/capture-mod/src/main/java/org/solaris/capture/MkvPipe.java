package org.solaris.capture;

import java.io.ByteArrayOutputStream;
import java.io.IOException;
import java.io.OutputStream;
import java.nio.charset.StandardCharsets;

/**
 * Minimal Matroska writer for the ffmpeg pipe in "capture" timestamp mode: one
 * V_UNCOMPRESSED RGBA track, one cluster per frame whose timestamp is the frame's
 * capture time in epoch microseconds. ffmpeg demuxes it with {@code -f matroska}
 * and keeps the timestamps with {@code -copyts}, so the output PTS equal the
 * sidecar {@code epoch_ms} (to the 1 ms MKV resolution) instead of the pipe
 * arrival time.
 */
final class MkvPipe {
    private static final long TIMESTAMP_SCALE_NS = 1000L; // 1 tick = 1 us

    private MkvPipe() {}

    /** EBML header, Segment start (unknown size), Info and Tracks. */
    static byte[] header(int width, int height, int fps) {
        ByteArrayOutputStream out = new ByteArrayOutputStream(256);
        ByteArrayOutputStream ebml = new ByteArrayOutputStream();
        uint(ebml, 0x4286, 1); // EBMLVersion
        uint(ebml, 0x42F7, 1); // EBMLReadVersion
        uint(ebml, 0x42F2, 4); // EBMLMaxIDLength
        uint(ebml, 0x42F3, 8); // EBMLMaxSizeLength
        string(ebml, 0x4282, "matroska"); // DocType
        uint(ebml, 0x4287, 4); // DocTypeVersion
        uint(ebml, 0x4285, 2); // DocTypeReadVersion
        element(out, 0x1A45DFA3, ebml.toByteArray());

        // Segment of unknown size (live stream)
        id(out, 0x18538067);
        out.write(0x01);
        for (int i = 0; i < 7; i++) {
            out.write(0xFF);
        }

        ByteArrayOutputStream info = new ByteArrayOutputStream();
        uint(info, 0x2AD7B1, TIMESTAMP_SCALE_NS); // TimestampScale
        string(info, 0x4D80, "solaris-capture"); // MuxingApp
        string(info, 0x5741, "solaris-capture"); // WritingApp
        element(out, 0x1549A966, info.toByteArray());

        ByteArrayOutputStream video = new ByteArrayOutputStream();
        uint(video, 0xB0, width); // PixelWidth
        uint(video, 0xBA, height); // PixelHeight
        bytes(video, 0x2EB524, "RGBA".getBytes(StandardCharsets.US_ASCII)); // ColourSpace (FourCC)
        ByteArrayOutputStream track = new ByteArrayOutputStream();
        uint(track, 0xD7, 1); // TrackNumber
        uint(track, 0x73C5, 1); // TrackUID
        uint(track, 0x83, 1); // TrackType = video
        uint(track, 0x9C, 0); // FlagLacing
        uint(track, 0x23E383, 1_000_000_000L / fps); // DefaultDuration (nominal rate for the encoder)
        string(track, 0x86, "V_UNCOMPRESSED"); // CodecID
        element(track, 0xE0, video.toByteArray());
        ByteArrayOutputStream tracks = new ByteArrayOutputStream();
        element(tracks, 0xAE, track.toByteArray());
        element(out, 0x1654AE6B, tracks.toByteArray());
        return out.toByteArray();
    }

    /** Cluster header + SimpleBlock header; the caller writes {@code frameBytes} of pixels next. */
    static byte[] frameHeader(double epochMs, int frameBytes) {
        long ts = Math.round(epochMs * 1000.0); // microseconds
        ByteArrayOutputStream h = new ByteArrayOutputStream(64);
        ByteArrayOutputStream tsEl = new ByteArrayOutputStream(16);
        uint(tsEl, 0xE7, ts); // Cluster Timestamp
        byte[] tsBytes = tsEl.toByteArray();
        long blockSize = 4L + frameBytes; // track vint + int16 relative timestamp + flags
        long blockElSize = 1 + 8 + blockSize; // SimpleBlock id + 8-byte size + payload
        id(h, 0x1F43B675); // Cluster
        size8(h, tsBytes.length + blockElSize);
        h.write(tsBytes, 0, tsBytes.length);
        id(h, 0xA3); // SimpleBlock
        size8(h, blockSize);
        h.write(0x81); // track 1
        h.write(0);
        h.write(0); // relative timestamp 0
        h.write(0x80); // keyframe
        return h.toByteArray();
    }

    private static void id(OutputStream out, int id) {
        try {
            if ((id & 0xFF000000) != 0) {
                out.write(id >>> 24);
            }
            if ((id & 0xFFFF0000) != 0) {
                out.write((id >>> 16) & 0xFF);
            }
            if ((id & 0xFFFFFF00) != 0) {
                out.write((id >>> 8) & 0xFF);
            }
            out.write(id & 0xFF);
        } catch (IOException e) {
            throw new IllegalStateException(e);
        }
    }

    private static void size8(OutputStream out, long size) {
        try {
            out.write(0x01);
            for (int shift = 48; shift >= 0; shift -= 8) {
                out.write((int) ((size >>> shift) & 0xFF));
            }
        } catch (IOException e) {
            throw new IllegalStateException(e);
        }
    }

    private static void element(OutputStream out, int id, byte[] payload) {
        id(out, id);
        size8(out, payload.length);
        try {
            out.write(payload);
        } catch (IOException e) {
            throw new IllegalStateException(e);
        }
    }

    private static void uint(OutputStream out, int id, long value) {
        int n = 1;
        while (n < 8 && (value >>> (8 * n)) != 0) {
            n++;
        }
        byte[] b = new byte[n];
        for (int i = 0; i < n; i++) {
            b[n - 1 - i] = (byte) (value >>> (8 * i));
        }
        element(out, id, b);
    }

    private static void string(OutputStream out, int id, String s) {
        element(out, id, s.getBytes(StandardCharsets.UTF_8));
    }

    private static void bytes(OutputStream out, int id, byte[] b) {
        element(out, id, b);
    }
}
