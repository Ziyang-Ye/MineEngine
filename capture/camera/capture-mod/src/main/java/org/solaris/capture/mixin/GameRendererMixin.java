package org.solaris.capture.mixin;

import net.minecraft.client.Camera;
import net.minecraft.client.renderer.GameRenderer;
import net.minecraft.world.entity.Entity;
import net.minecraft.world.level.ClipContext;
import net.minecraft.world.phys.HitResult;
import net.minecraft.world.phys.Vec3;
import org.solaris.capture.CaptureController;
import org.solaris.capture.PoseLink;
import org.spongepowered.asm.mixin.Mixin;
import org.spongepowered.asm.mixin.injection.At;
import org.spongepowered.asm.mixin.injection.Inject;
import org.spongepowered.asm.mixin.injection.Redirect;
import org.spongepowered.asm.mixin.injection.callback.CallbackInfoReturnable;

@Mixin(GameRenderer.class)
public abstract class GameRendererMixin {
    /**
     * The vertical FOV GameRenderer actually uses for the world projection this frame
     * (getFov(camera, partialTick, useFovSetting=true) in renderLevel): option value x
     * the dynamic FOV modifier (flying, sprinting, bow; neutralised by fovEffectScale 0),
     * death / underwater adjustments included.
     */
    @Inject(method = "getFov", at = @At("RETURN"))
    private void solaris$onGetFov(Camera camera, float partialTick, boolean useFovSetting,
                                  CallbackInfoReturnable<Double> cir) {
        if (useFovSetting) {
            CaptureController.onWorldFov(cir.getReturnValue());
        }
    }

    // ------------------------------------------------------------------
    // T12 pose link: the crosshair target (block outline, entity under the crosshair) is
    // picked from the injected pose too, so the outline matches the rendered view.
    // GameRenderer#pick(Entity, double blockRange, double entityRange, float partialTick).
    // ------------------------------------------------------------------

    private static final String PICK = "pick(Lnet/minecraft/world/entity/Entity;DDF)Lnet/minecraft/world/phys/HitResult;";

    @Redirect(method = PICK, at = @At(value = "INVOKE",
            target = "Lnet/minecraft/world/entity/Entity;getEyePosition(F)Lnet/minecraft/world/phys/Vec3;"))
    private Vec3 solaris$pickEye(Entity entity, float partialTick) {
        PoseLink.Pose p = solaris$pickPose(entity);
        return p != null ? p.eye() : entity.getEyePosition(partialTick);
    }

    @Redirect(method = PICK, at = @At(value = "INVOKE",
            target = "Lnet/minecraft/world/entity/Entity;getViewVector(F)Lnet/minecraft/world/phys/Vec3;"))
    private Vec3 solaris$pickView(Entity entity, float partialTick) {
        PoseLink.Pose p = solaris$pickPose(entity);
        return p != null ? entity.calculateViewVector(p.pitch, p.yaw) : entity.getViewVector(partialTick);
    }

    /** Entity#pick(range, partialTick, fluids) re-implemented from the injected pose. */
    @Redirect(method = PICK, at = @At(value = "INVOKE",
            target = "Lnet/minecraft/world/entity/Entity;pick(DFZ)Lnet/minecraft/world/phys/HitResult;"))
    private HitResult solaris$pickBlock(Entity entity, double range, float partialTick, boolean fluids) {
        PoseLink.Pose p = solaris$pickPose(entity);
        if (p == null) {
            return entity.pick(range, partialTick, fluids);
        }
        Vec3 eye = p.eye();
        Vec3 view = entity.calculateViewVector(p.pitch, p.yaw);
        Vec3 end = eye.add(view.x * range, view.y * range, view.z * range);
        return entity.level().clip(new ClipContext(eye, end, ClipContext.Block.OUTLINE,
                fluids ? ClipContext.Fluid.ANY : ClipContext.Fluid.NONE, entity));
    }

    private static PoseLink.Pose solaris$pickPose(Entity entity) {
        PoseLink link = PoseLink.get();
        return link == null ? null : link.forPick(entity);
    }
}
