package org.solaris.capture.mixin;

import net.minecraft.client.gui.GuiGraphics;
import net.minecraft.client.gui.components.toasts.Toast;
import net.minecraft.client.gui.components.toasts.ToastComponent;
import org.solaris.capture.CleanFrames;
import org.spongepowered.asm.mixin.Mixin;
import org.spongepowered.asm.mixin.injection.At;
import org.spongepowered.asm.mixin.injection.Inject;
import org.spongepowered.asm.mixin.injection.callback.CallbackInfo;

/**
 * Clean frames: no toast (advancement, recipe, tutorial, system / "chat messages can't be
 * verified" ...) is ever queued or drawn in a camera client (config suppress_toasts).
 * Every toast goes through ToastComponent#addToast (SystemToast.addOrUpdate, RecipeToast,
 * AdvancementToast, TutorialToast, ...), so nothing reaches the visible list; render is
 * cancelled as well in case another path fills the queue.
 */
@Mixin(ToastComponent.class)
public abstract class ToastComponentMixin {
    @Inject(method = "addToast", at = @At("HEAD"), cancellable = true)
    private void solaris$dropToast(Toast toast, CallbackInfo ci) {
        if (CleanFrames.suppressToasts()) {
            CleanFrames.countToast(toast);
            ci.cancel();
        }
    }

    @Inject(method = "render", at = @At("HEAD"), cancellable = true)
    private void solaris$noToastRender(GuiGraphics guiGraphics, CallbackInfo ci) {
        if (CleanFrames.suppressToasts()) {
            ci.cancel();
        }
    }
}
