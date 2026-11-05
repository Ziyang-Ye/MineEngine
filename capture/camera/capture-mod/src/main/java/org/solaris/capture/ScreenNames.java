package org.solaris.capture;

import java.util.LinkedHashMap;
import java.util.Map;
import net.minecraft.client.gui.screens.ChatScreen;
import net.minecraft.client.gui.screens.ConnectScreen;
import net.minecraft.client.gui.screens.DeathScreen;
import net.minecraft.client.gui.screens.DisconnectedScreen;
import net.minecraft.client.gui.screens.GenericMessageScreen;
import net.minecraft.client.gui.screens.LevelLoadingScreen;
import net.minecraft.client.gui.screens.PauseScreen;
import net.minecraft.client.gui.screens.ProgressScreen;
import net.minecraft.client.gui.screens.ReceivingLevelScreen;
import net.minecraft.client.gui.screens.Screen;
import net.minecraft.client.gui.screens.TitleScreen;
import net.minecraft.client.gui.screens.WinScreen;
import net.minecraft.client.gui.screens.inventory.AbstractContainerScreen;

/**
 * Readable names for the open screen in the sidecar. At runtime Minecraft classes
 * carry intermediary names (class_437), so common screens are mapped back to their
 * Mojang names; anything else falls back to the runtime class name.
 */
final class ScreenNames {
    private static final Map<Class<?>, String> KNOWN = new LinkedHashMap<>();

    static {
        KNOWN.put(ChatScreen.class, "ChatScreen");
        KNOWN.put(PauseScreen.class, "PauseScreen");
        KNOWN.put(DeathScreen.class, "DeathScreen");
        KNOWN.put(ReceivingLevelScreen.class, "ReceivingLevelScreen");
        KNOWN.put(LevelLoadingScreen.class, "LevelLoadingScreen");
        KNOWN.put(ConnectScreen.class, "ConnectScreen");
        KNOWN.put(DisconnectedScreen.class, "DisconnectedScreen");
        KNOWN.put(ProgressScreen.class, "ProgressScreen");
        KNOWN.put(GenericMessageScreen.class, "GenericMessageScreen");
        KNOWN.put(TitleScreen.class, "TitleScreen");
        KNOWN.put(WinScreen.class, "WinScreen");
        KNOWN.put(AbstractContainerScreen.class, "AbstractContainerScreen");
    }

    private ScreenNames() {}

    static String of(Screen screen) {
        if (screen == null) {
            return null;
        }
        for (Map.Entry<Class<?>, String> e : KNOWN.entrySet()) {
            if (e.getKey().isInstance(screen)) {
                return e.getKey() == screen.getClass() ? e.getValue() : e.getValue() + ":" + screen.getClass().getName();
            }
        }
        return screen.getClass().getName();
    }
}
