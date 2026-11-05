package org.solaris.capture.mixin;

import com.mojang.blaze3d.platform.Window;
import org.lwjgl.glfw.GLFW;
import org.solaris.capture.CaptureConfig;
import org.solaris.capture.SolarisCapture;
import org.spongepowered.asm.mixin.Mixin;
import org.spongepowered.asm.mixin.injection.At;
import org.spongepowered.asm.mixin.injection.Inject;
import org.spongepowered.asm.mixin.injection.callback.CallbackInfo;

/** hide_at_startup: create the GLFW window invisible and unfocused (it is never shown). */
@Mixin(Window.class)
public abstract class WindowMixin {
    @Inject(method = "<init>", at = @At(value = "INVOKE",
            target = "Lorg/lwjgl/glfw/GLFW;glfwCreateWindow(IILjava/lang/CharSequence;JJ)J", remap = false))
    private void solaris$hiddenWindowHints(CallbackInfo ci) {
        CaptureConfig cfg = SolarisCapture.config();
        if (cfg.loaded && cfg.hide_window && cfg.hide_at_startup) {
            GLFW.glfwWindowHint(GLFW.GLFW_VISIBLE, GLFW.GLFW_FALSE);
            GLFW.glfwWindowHint(GLFW.GLFW_FOCUSED, GLFW.GLFW_FALSE);
            GLFW.glfwWindowHint(GLFW.GLFW_FOCUS_ON_SHOW, GLFW.GLFW_FALSE);
            SolarisCapture.LOGGER.info("[solaris-capture] creating the window hidden (hide_at_startup)");
        }
    }
}
