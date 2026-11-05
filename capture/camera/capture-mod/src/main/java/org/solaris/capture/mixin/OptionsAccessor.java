package org.solaris.capture.mixin;

import net.minecraft.client.Options;
import org.spongepowered.asm.mixin.Mixin;
import org.spongepowered.asm.mixin.gen.Accessor;

@Mixin(Options.class)
public interface OptionsAccessor {
    /** View distance announced by the server (0 = not yet known). */
    @Accessor("serverRenderDistance")
    int solaris$getServerRenderDistance();
}
