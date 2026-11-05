package org.solaris.capture;

import com.mojang.logging.LogUtils;
import net.fabricmc.api.ClientModInitializer;
import net.fabricmc.loader.api.FabricLoader;
import org.slf4j.Logger;

/**
 * solaris-capture: records the client's main render target in the background
 * (windows/CONTRACT.md section 5). Without a config file the mod does nothing.
 */
public final class SolarisCapture implements ClientModInitializer {
    public static final String MOD_ID = "solaris-capture";
    public static final Logger LOGGER = LogUtils.getLogger();
    private static CaptureConfig config;

    /** Config, loaded on first use (the Window mixin may run before the entrypoint). */
    public static synchronized CaptureConfig config() {
        if (config == null) {
            config = CaptureConfig.load(FabricLoader.getInstance().getGameDir());
        }
        return config;
    }

    @Override
    public void onInitializeClient() {
        CaptureConfig cfg = config();
        if (!cfg.loaded) {
            return;
        }
        if (cfg.windows_power_tweaks && WinPower.isWindows()) {
            boolean timer = WinPower.requestTimerResolution();
            boolean throttle = WinPower.disableThrottlingSelf();
            boolean priority = WinPower.setPrioritySelf(cfg.process_priority);
            LOGGER.info("[solaris-capture] windows: timeBeginPeriod(1)={} power-throttling opt-out={} priority {}={}",
                    timer, throttle, cfg.process_priority, priority);
        }
        CaptureController.init(cfg);
        LOGGER.info("[solaris-capture] camera {} -> {} ({}x{} @ {} fps, encoder {}, hide_window {}, stop file {})",
                cfg.camera_name, cfg.outputDir().resolve(cfg.video_name), cfg.width, cfg.height, cfg.fps,
                cfg.encoder, cfg.hide_window, cfg.stopFile());
    }
}
