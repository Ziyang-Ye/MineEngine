package org.solaris.capture;

import java.time.Instant;

/**
 * Sub-millisecond wall clock (Unix epoch, milliseconds as double).
 *
 * <p>The Windows system time only advances at the clock-interrupt rate (1 to 15.6 ms
 * steps), so it is read relative to {@link System#nanoTime()}: every sample of
 * {@code systemTime - nanoTime} lags the true offset by up to one interrupt period,
 * and the maximum over a sliding window (two 2 s buckets) is the best estimate. The
 * offset therefore follows NTP slews / steps within a few seconds, like the
 * wall clock ffmpeg uses for {@code -use_wallclock_as_timestamps}.
 */
public final class EpochClock {
    private static final long BUCKET_NS = 2_000_000_000L;

    private static long bucketStartNs;
    private static long bucketMax = Long.MIN_VALUE;
    private static long prevBucketMax = Long.MIN_VALUE;
    private static double lastMs = 0.0;

    static {
        // Seed the estimate: sample for ~25 ms so at least one interrupt edge is seen.
        long start = System.nanoTime();
        bucketStartNs = start;
        while (System.nanoTime() - start < 25_000_000L) {
            sample(System.nanoTime());
        }
    }

    private EpochClock() {}

    private static void sample(long nano) {
        Instant now = Instant.now();
        long offset = now.getEpochSecond() * 1_000_000_000L + now.getNano() - nano;
        if (nano - bucketStartNs > BUCKET_NS) {
            prevBucketMax = bucketMax;
            bucketMax = Long.MIN_VALUE;
            bucketStartNs = nano;
        }
        if (offset > bucketMax) {
            bucketMax = offset;
        }
    }

    /** Current epoch time in milliseconds (monotonic non-decreasing). */
    public static synchronized double nowMs() {
        long nano = System.nanoTime();
        sample(nano);
        long offset = Math.max(bucketMax, prevBucketMax);
        double ms = (nano + offset) / 1.0e6;
        if (ms < lastMs) {
            ms = lastMs;
        }
        lastMs = ms;
        return ms;
    }
}
