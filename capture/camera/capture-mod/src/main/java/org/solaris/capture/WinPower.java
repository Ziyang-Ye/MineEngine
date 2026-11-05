package org.solaris.capture;

import com.sun.jna.Function;
import com.sun.jna.Memory;
import com.sun.jna.NativeLibrary;
import com.sun.jna.Pointer;

/**
 * Windows-only scheduling tweaks for a client whose window is hidden or covered
 * (JNA ships with Minecraft). Windows 11 throttles processes without a visible
 * window: EcoQoS lowers their CPU clock, and their {@code timeBeginPeriod}
 * requests are ignored, so 1 ms waits become 15.6 ms and the frame pacing breaks.
 * Opting out of both throttling kinds (SetProcessInformation /
 * ProcessPowerThrottling) and requesting a 1 ms timer keeps the render loop and
 * the encoder at full speed. Every call is best effort.
 */
public final class WinPower {
    private static final boolean WINDOWS = System.getProperty("os.name", "").toLowerCase().contains("win");
    private static final int PROCESS_POWER_THROTTLING_CLASS = 4; // ProcessPowerThrottling
    private static final int THROTTLE_EXECUTION_SPEED = 0x1;
    private static final int THROTTLE_IGNORE_TIMER_RESOLUTION = 0x4;
    private static final int PROCESS_SET_INFORMATION = 0x0200;
    private static final int NORMAL_PRIORITY_CLASS = 0x20;
    private static final int ABOVE_NORMAL_PRIORITY_CLASS = 0x8000;
    private static final int HIGH_PRIORITY_CLASS = 0x80;

    private WinPower() {}

    public static boolean isWindows() {
        return WINDOWS;
    }

    /** timeBeginPeriod(1) for this process (never released; the process ends with the camera). */
    public static boolean requestTimerResolution() {
        if (!WINDOWS) {
            return false;
        }
        try {
            Function f = NativeLibrary.getInstance("winmm").getFunction("timeBeginPeriod");
            return f.invokeInt(new Object[] {1}) == 0;
        } catch (Throwable t) {
            SolarisCapture.LOGGER.warn("[solaris-capture] timeBeginPeriod failed: {}", t.toString());
            return false;
        }
    }

    /** Disable EcoQoS and timer-resolution throttling for the current process. */
    public static boolean disableThrottlingSelf() {
        if (!WINDOWS) {
            return false;
        }
        try {
            Function current = NativeLibrary.getInstance("kernel32").getFunction("GetCurrentProcess");
            return disableThrottling((Pointer) current.invoke(Pointer.class, new Object[0]));
        } catch (Throwable t) {
            SolarisCapture.LOGGER.warn("[solaris-capture] power throttling opt-out failed: {}", t.toString());
            return false;
        }
    }

    /** Same for another process (ffmpeg), plus its priority class (see {@link #setPriority}). */
    public static boolean disableThrottling(long pid, String priority) {
        if (!WINDOWS) {
            return false;
        }
        try {
            NativeLibrary k32 = NativeLibrary.getInstance("kernel32");
            Pointer h = (Pointer) k32.getFunction("OpenProcess")
                    .invoke(Pointer.class, new Object[] {PROCESS_SET_INFORMATION, 0, (int) pid});
            if (h == null) {
                return false;
            }
            try {
                boolean ok = disableThrottling(h);
                if (priority != null) {
                    ok &= setPriority(h, priority);
                }
                return ok;
            } finally {
                k32.getFunction("CloseHandle").invokeInt(new Object[] {h});
            }
        } catch (Throwable t) {
            SolarisCapture.LOGGER.warn("[solaris-capture] power throttling opt-out for pid {} failed: {}", pid, t.toString());
            return false;
        }
    }

    /** Priority class of the current process: normal | above_normal | high. */
    public static boolean setPrioritySelf(String priority) {
        if (!WINDOWS) {
            return false;
        }
        try {
            Function current = NativeLibrary.getInstance("kernel32").getFunction("GetCurrentProcess");
            return setPriority((Pointer) current.invoke(Pointer.class, new Object[0]), priority);
        } catch (Throwable t) {
            SolarisCapture.LOGGER.warn("[solaris-capture] SetPriorityClass failed: {}", t.toString());
            return false;
        }
    }

    private static boolean setPriority(Pointer process, String priority) {
        int cls = switch (priority == null ? "" : priority.toLowerCase()) {
            case "normal" -> NORMAL_PRIORITY_CLASS;
            case "high" -> HIGH_PRIORITY_CLASS;
            case "above_normal" -> ABOVE_NORMAL_PRIORITY_CLASS;
            default -> 0;
        };
        if (cls == 0) {
            SolarisCapture.LOGGER.warn("[solaris-capture] unknown process_priority '{}'; left unchanged", priority);
            return false;
        }
        Function f = NativeLibrary.getInstance("kernel32").getFunction("SetPriorityClass");
        return f.invokeInt(new Object[] {process, cls}) != 0;
    }

    private static boolean disableThrottling(Pointer process) {
        // PROCESS_POWER_THROTTLING_STATE { ULONG Version; ULONG ControlMask; ULONG StateMask; }
        Memory state = new Memory(12);
        state.setInt(0, 1);
        state.setInt(4, THROTTLE_EXECUTION_SPEED | THROTTLE_IGNORE_TIMER_RESOLUTION);
        state.setInt(8, 0); // controlled bits cleared = throttling off
        Function f = NativeLibrary.getInstance("kernel32").getFunction("SetProcessInformation");
        return f.invokeInt(new Object[] {process, PROCESS_POWER_THROTTLING_CLASS, state, 12}) != 0;
    }
}
