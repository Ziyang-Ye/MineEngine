package org.solaris.capture.mixin;

import com.mojang.blaze3d.systems.RenderSystem;
import net.minecraft.client.Minecraft;
import org.solaris.capture.CaptureController;
import org.spongepowered.asm.mixin.Mixin;
import org.spongepowered.asm.mixin.injection.At;
import org.spongepowered.asm.mixin.injection.Inject;
import org.spongepowered.asm.mixin.injection.Redirect;
import org.spongepowered.asm.mixin.injection.callback.CallbackInfo;

/**
 * Frame hooks in Minecraft#runTick: frame start, readback right before the buffer
 * swap (after the main target was blitted to the screen), and the frame limiter.
 */
@Mixin(Minecraft.class)
public abstract class MinecraftMixin {
    @Inject(method = "runTick", at = @At("HEAD"))
    private void solaris$frameStart(boolean tick, CallbackInfo ci) {
        CaptureController c = CaptureController.get();
        if (c != null) {
            c.onFrameStart((Minecraft) (Object) this);
        }
    }

    @Inject(method = "runTick", at = @At(value = "INVOKE",
            target = "Lcom/mojang/blaze3d/platform/Window;updateDisplay()V"))
    private void solaris$beforeSwap(boolean tick, CallbackInfo ci) {
        CaptureController c = CaptureController.get();
        if (c != null) {
            c.beforeSwap((Minecraft) (Object) this);
        }
    }

    /**
     * 1.21 has no unfocused / hidden-window frame cap (inactivityFpsLimit arrived in
     * 1.21.2): the cap is maxFps (60 when there is no level and a menu is open). The
     * replacement keeps that cap but never drops below 2x the capture rate and wakes
     * up at the next capture slot. (RenderSystem is not obfuscated: remap = false.)
     */
    @Redirect(method = "runTick", at = @At(value = "INVOKE",
            target = "Lcom/mojang/blaze3d/systems/RenderSystem;limitDisplayFPS(I)V", remap = false))
    private void solaris$limitDisplayFps(int limit) {
        CaptureController c = CaptureController.get();
        if (c != null) {
            c.limitDisplayFps(limit);
        } else {
            RenderSystem.limitDisplayFPS(limit);
        }
    }
}
