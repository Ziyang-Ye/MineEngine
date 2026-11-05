package org.solaris.capture.mixin;

import com.mojang.authlib.GameProfile;
import java.util.concurrent.CompletableFuture;
import net.minecraft.client.resources.PlayerSkin;
import net.minecraft.client.resources.SkinManager;
import org.solaris.capture.LocalSkins;
import org.spongepowered.asm.mixin.Mixin;
import org.spongepowered.asm.mixin.injection.At;
import org.spongepowered.asm.mixin.injection.Inject;
import org.spongepowered.asm.mixin.injection.callback.CallbackInfoReturnable;

/**
 * Local skins: SkinManager#getOrLoad(GameProfile) is the single entry point of
 * PlayerInfo#createSkinLookup / lookupInsecure / getInsecureSkin. Profiles whose (unsigned)
 * textures property names a local pool skin get a secure PlayerSkin built from the local PNG
 * (see {@link LocalSkins}); all other profiles keep the vanilla (Mojang) path.
 */
@Mixin(SkinManager.class)
public abstract class SkinManagerMixin {
    @Inject(method = "getOrLoad", at = @At("HEAD"), cancellable = true)
    private void solaris$localSkin(GameProfile profile, CallbackInfoReturnable<CompletableFuture<PlayerSkin>> cir) {
        CompletableFuture<PlayerSkin> local = LocalSkins.lookup(profile);
        if (local != null) {
            cir.setReturnValue(local);
        }
    }
}
