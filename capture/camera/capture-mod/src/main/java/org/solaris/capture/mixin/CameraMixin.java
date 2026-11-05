package org.solaris.capture.mixin;

import net.minecraft.client.Camera;
import net.minecraft.world.entity.Entity;
import net.minecraft.world.level.BlockGetter;
import org.solaris.capture.PoseLink;
import org.spongepowered.asm.mixin.Mixin;
import org.spongepowered.asm.mixin.Shadow;
import org.spongepowered.asm.mixin.injection.At;
import org.spongepowered.asm.mixin.injection.Inject;
import org.spongepowered.asm.mixin.injection.callback.CallbackInfo;

/**
 * T12 pose link: at the end of Camera#setup (1.21: setup(BlockGetter, Entity, boolean
 * detached, boolean mirrored, float partialTick), called by GameRenderer#renderLevel before
 * the projection, the frustum (prepareCullFrustum(camera.getPosition(), rotation)) and the
 * level / hand rendering), replace the server-followed pose by the bot's pose received over
 * UDP: position = feet + eye height (1.62 standing, 1.27 sneaking), rotation = (yaw, pitch).
 * Everything that reads the camera afterwards (frustum culling, section sorting, sky, fog,
 * fluid-in-camera, sound listener, frames sidecar) uses that pose.
 */
@Mixin(Camera.class)
public abstract class CameraMixin {
    @Shadow
    protected abstract void setRotation(float yRot, float xRot);

    @Shadow
    protected abstract void setPosition(double x, double y, double z);

    @Inject(method = "setup", at = @At("TAIL"))
    private void solaris$injectPose(BlockGetter level, Entity entity, boolean detached, boolean mirrored,
                                    float partialTick, CallbackInfo ci) {
        PoseLink link = PoseLink.get();
        if (link == null) {
            return;
        }
        PoseLink.Pose p = link.onCameraSetup((Camera) (Object) this, entity, detached, partialTick);
        if (p != null) {
            this.setRotation(p.yaw, p.pitch);
            this.setPosition(p.x, p.y + p.eyeHeight(), p.z);
        }
    }
}
