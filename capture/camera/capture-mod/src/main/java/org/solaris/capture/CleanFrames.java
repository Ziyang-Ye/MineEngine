package org.solaris.capture;

import java.util.LinkedHashMap;
import java.util.Map;
import java.util.TreeMap;
import java.util.concurrent.ConcurrentHashMap;
import java.util.concurrent.atomic.AtomicLong;
import net.minecraft.client.gui.components.toasts.AdvancementToast;
import net.minecraft.client.gui.components.toasts.RecipeToast;
import net.minecraft.client.gui.components.toasts.SystemToast;
import net.minecraft.client.gui.components.toasts.Toast;
import net.minecraft.client.gui.components.toasts.TutorialToast;

/**
 * Clean camera frames: toast suppression (ToastComponentMixin) and its counters for the
 * meta. Chat is hidden through options.txt (chatVisibility:2 written by the launcher; the
 * server then sends no chat / non-overlay system messages, and ChatComponent draws
 * nothing); the action bar, titles, hotbar, hearts, crosshair, held-item name and name
 * tags (vanilla HUD) stay.
 */
public final class CleanFrames {
    private static final Map<String, AtomicLong> TOASTS = new ConcurrentHashMap<>();
    private static final AtomicLong TOTAL = new AtomicLong();

    private CleanFrames() {
    }

    public static boolean suppressToasts() {
        CaptureConfig cfg = SolarisCapture.config();
        return cfg.loaded && cfg.suppress_toasts;
    }

    public static void countToast(Toast toast) {
        String name = toastName(toast);
        long n = TOASTS.computeIfAbsent(name, k -> new AtomicLong()).incrementAndGet();
        TOTAL.incrementAndGet();
        if (n == 1) {
            SolarisCapture.LOGGER.info("[solaris-capture] suppressed toast {} (further ones are only counted)", name);
        }
    }

    /** Readable toast kind (the runtime class names are obfuscated in production). */
    static String toastName(Toast toast) {
        if (toast == null) return "null";
        if (toast instanceof AdvancementToast) return "AdvancementToast";
        if (toast instanceof RecipeToast) return "RecipeToast";
        if (toast instanceof SystemToast) return "SystemToast";
        if (toast instanceof TutorialToast) return "TutorialToast";
        String n = toast.getClass().getSimpleName();
        return n.isEmpty() ? toast.getClass().getName() : n;
    }

    /** {"enabled", "total", "by_class": {class: n}} for the camera meta. */
    public static Map<String, Object> meta() {
        Map<String, Object> m = new LinkedHashMap<>();
        m.put("enabled", suppressToasts());
        m.put("total", TOTAL.get());
        Map<String, Long> by = new TreeMap<>();
        TOASTS.forEach((k, v) -> by.put(k, v.get()));
        m.put("by_class", by);
        return m;
    }
}
