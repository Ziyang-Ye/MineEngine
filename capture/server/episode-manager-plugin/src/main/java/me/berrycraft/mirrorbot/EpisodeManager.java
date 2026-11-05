// Modified by Ziyang Ye for MineEngine, 2026 (from SolarisEngine 78f23e7); see capture/MODIFICATIONS.md
package me.berrycraft.mirrorbot;

import org.bukkit.Bukkit;
import org.bukkit.ChatColor;
import org.bukkit.GameMode;
import org.bukkit.command.Command;
import org.bukkit.command.CommandSender;
import org.bukkit.entity.Player;
import org.bukkit.plugin.java.JavaPlugin;

import com.comphenix.protocol.ProtocolLibrary;
import com.comphenix.protocol.ProtocolManager;

import org.bukkit.event.EventHandler;
import org.bukkit.event.Listener;
import org.bukkit.event.player.PlayerJoinEvent;
import org.bukkit.event.player.PlayerQuitEvent;

import java.io.File;
import java.util.*;
import java.util.concurrent.ConcurrentHashMap;

public class EpisodeManager extends JavaPlugin implements Listener {

    // ---- STATE ----
    private final Map<String, String> controllerToCamera = new LinkedHashMap<>();
    private final Map<Player, Player> activePairs          = new ConcurrentHashMap<>();
    private final Map<UUID, UUID> activeCameraControllers = new ConcurrentHashMap<>();
    private final Map<String, File> activeSkinSelections  = new ConcurrentHashMap<>();
    private final Map<UUID, UUID> activeSpectatorsByController = new ConcurrentHashMap<>();
    private final Map<UUID, UUID> controllerBySpectator = new ConcurrentHashMap<>();

    private boolean episodeRunning = false;

    // ---- MANAGERS ----
    private CameraManager cameraManager;
    private SkinManager skinManager;
    private ProtocolManager protocolManager;
    private volatile WorldStateLogger worldStateLogger; // read from the ProtocolLib packet thread

    @Override
    public void onEnable() {

        if (Bukkit.getPluginManager().getPlugin("ProtocolLib") == null) {
            getLogger().severe("ProtocolLib is required for MirrorBot to run.");
            Bukkit.getPluginManager().disablePlugin(this);
            return;
        }

        protocolManager = ProtocolLibrary.getProtocolManager();

        skinManager = new SkinManager(this);
        cameraManager = new CameraManager(this, protocolManager);

        Bukkit.getPluginManager().registerEvents(this, this);
        Bukkit.getPluginManager().registerEvents(cameraManager, this);

        // Per-episode world-state logging (world_events.jsonl + block snapshots), driven over RCON
        // by the controller bots via the /worldstate command.
        worldStateLogger = new WorldStateLogger(this);
        Bukkit.getPluginManager().registerEvents(worldStateLogger, this);
        if (getCommand("worldstate") != null) {
            getCommand("worldstate").setExecutor(worldStateLogger);
        } else {
            getLogger().severe("worldstate command missing from plugin.yml");
        }
        worldStateLogger.start();

        getLogger().info("EpisodeManager enabled.");
    }

    @Override
    public void onDisable() {
        stopEpisode(null);
        if (worldStateLogger != null) {
            worldStateLogger.shutdown();
        }
    }

    // =====================================================================================
    // Episode Command
    // =====================================================================================

    @Override
    public boolean onCommand(CommandSender sender, Command cmd, String label, String[] args) {

        if (args.length == 0) {
            sendUsage(sender);
            return true;
        }

        switch (args[0].toLowerCase(Locale.ROOT)) {
            case "start" -> startEpisode(sender, Arrays.copyOfRange(args, 1, args.length));
            case "stop"  -> stopEpisode(sender);
            case "skin"  -> skinCommand(sender, Arrays.copyOfRange(args, 1, args.length));
            case "skinstatus" -> skinStatusCommand(sender, Arrays.copyOfRange(args, 1, args.length));
            default      -> sendUsage(sender);
        }

        return true;
    }

    private void sendUsage(CommandSender sender) {
        sender.sendMessage(ChatColor.YELLOW + "Usage:");
        sender.sendMessage(ChatColor.YELLOW + "/episode start <controller> <camera> <skin> [repeat...]");
        sender.sendMessage(ChatColor.YELLOW + "/episode stop");
        sender.sendMessage(ChatColor.YELLOW + "/episode skin <controller> <skin> [camera|-] [note=0|1] [mode=auto|local|signed]");
        sender.sendMessage(ChatColor.YELLOW + "/episode skinstatus <player>");
    }

    // =====================================================================================
    // Skin change (per-episode skins). Replies with one JSON line (read over RCON).
    // =====================================================================================

    /**
     * /episode skin <controller> <skin> [camera|-] [note=0|1] [mode=auto|local|signed]
     * Applies the skin (pool/ or top-level PNG) to the controller and its camera (explicit name, else the
     * camera paired by `episode start`, else "Camera" + controller; skipped when offline or "-").
     * mode=local: unsigned 'textures' property naming the local skin (any PNG, no .customskin; rendered by
     * the solaris-capture camera mod from its local pool), set with Paper's setPlayerProfile, which
     * refreshes the player for all viewers. mode=signed: the Mojang-signed .customskin through
     * SkinsRestorer. mode=auto (default): local, falling back to signed if local fails and a cache exists.
     * If a world log is open and note != 0, a JSON note {"phase":"skin",...} is appended to it.
     * Both modes respawn the camera client (its own profile is re-sent to it): one ReceivingLevelScreen frame
     * and a ~150 ms capture hitch, so apply between episodes; an apply while a world log is open is logged.
     * Reply: {"ok":true,"player":..,"camera":..|null,"skin_id":..,"model":..,"texture_hash":..,"file":..,
     *         "mode":"local"|"signed","signed":bool,"sha256":..,"fallback_from":..,"applied":bool,"noted":bool,
     *         "camera_respawn":bool,"world_log_open":bool}
     *        or {"ok":false,"error":..}.
     */
    private void skinCommand(CommandSender sender, String[] args) {
        String[] pos = Arrays.stream(args).filter(a -> !a.contains("=")).toArray(String[]::new);
        boolean note = Arrays.stream(args).noneMatch(a -> a.equalsIgnoreCase("note=0") || a.equalsIgnoreCase("note=false"));
        if (pos.length < 2) {
            sender.sendMessage(jsonError("usage: episode skin <controller> <skin> [camera|-] [note=0|1] [mode=auto|local|signed]"));
            return;
        }
        String ctrlName = pos[0];
        Player controller = Bukkit.getPlayerExact(ctrlName);
        if (controller == null) {
            sender.sendMessage(jsonError("controller not online: " + ctrlName));
            return;
        }
        String camName = pos.length >= 3 ? pos[2] : controllerToCamera.getOrDefault(ctrlName, "Camera" + ctrlName);
        Player camera = "-".equals(camName) ? null : Bukkit.getPlayerExact(camName);

        File skin = skinManager.resolveSkin(pos[1], skinManager.loadSkins());
        if (skin == null) {
            sender.sendMessage(jsonError("skin not found: " + pos[1]));
            return;
        }
        String mode = Arrays.stream(args).filter(a -> a.toLowerCase(Locale.ROOT).startsWith("mode="))
                .map(a -> a.substring(5).toLowerCase(Locale.ROOT)).reduce((a, b) -> b).orElse("auto");
        if (!mode.equals("auto") && !mode.equals("local") && !mode.equals("signed")) {
            sender.sendMessage(jsonError("mode must be auto|local|signed: " + mode));
            return;
        }
        // A skin change re-sends the camera player to its own client (Paper setPlayerProfile / SkinsRestorer
        // refresh = respawn): the camera records a ReceivingLevelScreen frame and a ~150 ms hitch. Skins are
        // meant to change between episodes; flag an apply while a world log (episode recording) is open.
        WorldStateLogger wsl = worldStateLogger;
        String openStem = wsl == null ? null : wsl.activeStem();
        if (openStem != null && camera != null) {
            getLogger().warning("[episode skin] " + ctrlName + " -> " + pos[1] + " while world log " + openStem
                    + " is open: camera " + camera.getName() + " respawns (loading-screen frame in the recording)");
        }
        SkinManager.SkinInfo info;
        String fallbackFrom = null;
        try {
            if (mode.equals("signed")) {
                info = skinManager.applySkinNow(controller, camera, skin);
            } else {
                try {
                    info = skinManager.applyLocalSkinNow(controller, camera, skin);
                } catch (Exception e) {
                    // auto: fall back to the Mojang-signed SkinsRestorer path when a .customskin exists
                    if (!mode.equals("auto") || !skinManager.hasSignedCache(skin)) throw e;
                    getLogger().warning("[episode skin] local apply of " + pos[1] + " failed (" + e + "); using the signed cache");
                    fallbackFrom = "local: " + e;
                    info = skinManager.applySkinNow(controller, camera, skin);
                }
            }
        } catch (Exception e) {
            getLogger().warning("[episode skin] " + ctrlName + " " + pos[1] + " failed: " + e);
            sender.sendMessage(jsonError(String.valueOf(e.getMessage() != null ? e.getMessage() : e)));
            return;
        }
        activeSkinSelections.put(ctrlName, skin);
        if (camera != null) activeSkinSelections.put(camera.getName(), skin);

        String[] cur = skinManager.currentTexture(controller);
        boolean applied = cur != null && info.textureHash != null && info.textureHash.equals(cur[1]);

        boolean noted = false;
        if (note) {
            String text = "{\"phase\":\"skin\",\"player\":" + jq(ctrlName) + ",\"camera\":" + jq(camera == null ? null : camera.getName())
                    + ",\"skin_id\":" + jq(info.skinId) + ",\"model\":" + jq(info.model)
                    + ",\"texture_hash\":" + jq(info.textureHash) + ",\"mode\":" + jq(info.mode)
                + ",\"camera_respawn\":" + (camera != null) + "}";
            StringBuilder out = new StringBuilder();
            try {
                CommandSender capture = Bukkit.createCommandSender(c ->
                        out.append(net.kyori.adventure.text.serializer.plain.PlainTextComponentSerializer.plainText().serialize(c)));
                Bukkit.dispatchCommand(capture, "worldstate note " + text);
                noted = out.toString().contains("\"ok\":true");
            } catch (Exception e) {
                getLogger().warning("[episode skin] world-log note failed: " + e);
            }
        }
        sender.sendMessage("{\"ok\":true,\"player\":" + jq(ctrlName)
                + ",\"camera\":" + jq(camera == null ? null : camera.getName())
                + ",\"skin_id\":" + jq(info.skinId) + ",\"model\":" + jq(info.model)
                + ",\"texture_hash\":" + jq(info.textureHash) + ",\"file\":" + jq(skin.getName())
                + ",\"mode\":" + jq(info.mode) + ",\"signed\":" + info.signed()
                + ",\"sha256\":" + jq(info.sha256) + ",\"fallback_from\":" + jq(fallbackFrom)
                + ",\"applied\":" + applied + ",\"noted\":" + noted
                + ",\"camera_respawn\":" + (camera != null) + ",\"world_log_open\":" + (openStem != null) + "}");
    }

    /** /episode skinstatus <player>: {"ok":true,"player":..,"online":bool,"skin_id":..,"model":..,"texture_hash":..,"signed":bool,"mode":"local"|"signed"|null} */
    private void skinStatusCommand(CommandSender sender, String[] args) {
        if (args.length < 1) {
            sender.sendMessage(jsonError("usage: episode skinstatus <player>"));
            return;
        }
        Player p = Bukkit.getPlayerExact(args[0]);
        if (p == null) {
            sender.sendMessage("{\"ok\":true,\"player\":" + jq(args[0]) + ",\"online\":false}");
            return;
        }
        String[] cur = skinManager.currentTexture(p);
        sender.sendMessage("{\"ok\":true,\"player\":" + jq(p.getName()) + ",\"online\":true"
                + ",\"skin_id\":" + jq(skinManager.appliedSkinId(p))
                + ",\"model\":" + jq(cur == null ? null : cur[0])
                + ",\"texture_hash\":" + jq(cur == null ? null : cur[1])
                + ",\"signed\":" + (cur != null && "1".equals(cur[2]))
                + ",\"mode\":" + jq(cur == null || cur[1] == null ? null
                        : cur[1].startsWith(SkinManager.LOCAL_HASH_PREFIX) ? "local" : "signed") + "}");
    }

    private static String jsonError(String msg) {
        return "{\"ok\":false,\"error\":" + jq(msg) + "}";
    }

    private static String jq(String v) {
        if (v == null) return "null";
        StringBuilder b = new StringBuilder("\"");
        for (char c : v.toCharArray()) {
            switch (c) {
                case '"' -> b.append("\\\"");
                case '\\' -> b.append("\\\\");
                case '\n' -> b.append("\\n");
                case '\r' -> b.append("\\r");
                case '\t' -> b.append("\\t");
                default -> {
                    if (c < 0x20) b.append(String.format("\\u%04x", (int) c));
                    else b.append(c);
                }
            }
        }
        return b.append('"').toString();
    }

    // =====================================================================================
    // Episode Logic
    // =====================================================================================

    private void startEpisode(CommandSender starter, String[] args) {

        if (episodeRunning) {
            if (starter != null)
                starter.sendMessage(ChatColor.RED + "Episode already running!");
            return;
        }

        if (args.length == 0 || args.length % 3 != 0) {
            if (starter != null)
                starter.sendMessage(ChatColor.RED + "You must supply triples: <controller> <camera> <skin>");
            return;
        }

        Map<String, File> availableSkins = skinManager.loadSkins();
        if (availableSkins.isEmpty()) {
            starter.sendMessage(ChatColor.RED + "No skins found.");
            return;
        }

        if (!skinManager.preloadSkinCache(availableSkins)) {
            starter.sendMessage(ChatColor.RED + "SkinsRestorer's skin storage is unavailable. Check server log for details.");
            return;
        }

        Map<String, String> pairMap = new LinkedHashMap<>();
        List<StartConfig> configs = new ArrayList<>();

        for (int i = 0; i < args.length; i += 3) {
            String ctrl = args[i];
            String cam = args[i + 1];
            String skinKey = args[i + 2];

            if (pairMap.containsKey(ctrl)) {
                starter.sendMessage(ChatColor.RED + "Duplicate controller: " + ctrl);
                return;
            }
            if (pairMap.containsValue(cam)) {
                starter.sendMessage(ChatColor.RED + "Duplicate camera: " + cam);
                return;
            }

            File skin = skinManager.resolveSkin(skinKey, availableSkins);
            if (skin == null) {
                starter.sendMessage(ChatColor.RED + "Skin not found: " + skinKey);
                return;
            }

            configs.add(new StartConfig(ctrl, cam, skin));
            pairMap.put(ctrl, cam);
        }

        // ---- ACTIVATE EPISODE ----
        episodeRunning = true;
        controllerToCamera.clear();
        controllerToCamera.putAll(pairMap);

        activePairs.clear();
        activeSkinSelections.clear();
        activeCameraControllers.clear();
        activeSpectatorsByController.clear();
        controllerBySpectator.clear();

        for (StartConfig cfg : configs) {

            activeSkinSelections.put(cfg.controller, cfg.skin);
            activeSkinSelections.put(cfg.camera, cfg.skin);

            Player controller = Bukkit.getPlayerExact(cfg.controller);
            Player camera     = Bukkit.getPlayerExact(cfg.camera);

            if (controller == null || camera == null) {
                getLogger().warning("Missing players for pair: " + cfg.controller + " -> " + cfg.camera);
                continue;
            }

            controller.setGameMode(GameMode.SURVIVAL);
            camera.setGameMode(GameMode.SURVIVAL);

            activePairs.put(controller, camera);
            activeCameraControllers.put(camera.getUniqueId(), controller.getUniqueId());

            // Optional spectator bot (e.g., SpectatorAlpha)
            Player spectator = Bukkit.getPlayerExact("Spectator" + cfg.controller);
            if (spectator != null) {
                spectator.setGameMode(GameMode.SPECTATOR);
                activeSpectatorsByController.put(controller.getUniqueId(), spectator.getUniqueId());
                controllerBySpectator.put(spectator.getUniqueId(), controller.getUniqueId());
            }

            cameraManager.prepareCamera(camera);
            if (skinManager.hasSignedCache(cfg.skin) && skinManager.canApplySigned()) {
                skinManager.applySharedSkin(controller, camera, cfg.skin);
            } else {
                try {                                                  // no signed cache: a local (unsigned) skin
                    skinManager.applyLocalSkinNow(controller, camera, cfg.skin);
                } catch (Exception ex) {
                    getLogger().warning("[Episode] local session skin " + cfg.skin.getName() + " failed: " + ex);
                }
            }
        }

        refreshEpisodeVisibility();
        cameraManager.startFollowingTask();

        Bukkit.broadcastMessage(ChatColor.GREEN + "[Episode] Episode started!");
    }

    public void stopEpisode(CommandSender caller) {
        if (!episodeRunning) return;

        episodeRunning = false;

        cameraManager.stopFollowingTask();

        // Restore physics + collisions
        for (Player p : Bukkit.getOnlinePlayers()) {
            cameraManager.restorePlayer(p);
        }

        resetAllVisibility();

        controllerToCamera.clear();
        activePairs.clear();
        activeCameraControllers.clear();
        activeSkinSelections.clear();
        for (UUID spectatorId : controllerBySpectator.keySet()) {
            Player spectator = Bukkit.getPlayer(spectatorId);
            if (spectator != null) spectator.setGameMode(GameMode.SURVIVAL);
        }
        activeSpectatorsByController.clear();
        controllerBySpectator.clear();

        Bukkit.broadcastMessage(ChatColor.RED + "[Episode] Episode stopped.");
    }

    // =====================================================================================
    // Visibility Logic
    // =====================================================================================

    private void refreshEpisodeVisibility() {

        // Everyone sees everyone first
        resetAllVisibility();

        // Hide cameras from spectators
        for (Player spectator : Bukkit.getOnlinePlayers()) {
            boolean participant =
                    controllerToCamera.containsKey(spectator.getName()) ||
                    controllerToCamera.containsValue(spectator.getName()) ||
                    isSpectator(spectator);

            if (!participant) {
                for (UUID camId : activeCameraControllers.keySet()) {
                    Player cam = Bukkit.getPlayer(camId);
                    if (cam != null) spectator.hidePlayer(this, cam);
                }
            }
        }

        // Controllers never see cameras
        for (Player controller : activePairs.keySet()) {
            hideAllCamerasFrom(controller);
        }

        // Cameras hide their controller + other cameras
        for (Map.Entry<UUID, UUID> entry : activeCameraControllers.entrySet()) {
            Player camera = Bukkit.getPlayer(entry.getKey());
            Player controller = Bukkit.getPlayer(entry.getValue());

            if (camera == null) continue;

            for (Player other : Bukkit.getOnlinePlayers()) {
                boolean hide = other.equals(controller) || isCamera(other);
                if (hide) camera.hidePlayer(this, other);
                else camera.showPlayer(this, other);
            }
        }
    }

    private void resetAllVisibility() {
        for (Player p1 : Bukkit.getOnlinePlayers()) {
            for (Player p2 : Bukkit.getOnlinePlayers()) {
                if (!p1.equals(p2)) p1.showPlayer(this, p2);
            }
        }
    }

    private void hideAllCamerasFrom(Player p) {
        for (UUID camId : activeCameraControllers.keySet()) {
            Player cam = Bukkit.getPlayer(camId);
            if (cam != null) p.hidePlayer(this, cam);
        }
        for (String camName : controllerToCamera.values()) {
            Player cam = Bukkit.getPlayerExact(camName);
            if (cam != null) p.hidePlayer(this, cam);
        }
    }

    // =====================================================================================
    // Events
    // =====================================================================================

    @EventHandler
    public void onJoin(PlayerJoinEvent e) {
        Player p = e.getPlayer();

        if (!episodeRunning) return;

        // If controller or camera rejoined, re-apply visibility rules
        refreshEpisodeVisibility();

        // If this is a camera that rejoined, reapply physics
        if (isCamera(p)) {
            cameraManager.prepareCamera(p);
        }

        // Restore spectator state if they reconnect mid-episode
        if (isSpectator(p)) {
            p.setGameMode(GameMode.SPECTATOR);
        } else {
            attachSpectatorIfMatches(p);
        }
    }

    @EventHandler
    public void onQuit(PlayerQuitEvent e) {
        Player leaving = e.getPlayer();

        Optional<Player> partner = activePairs.entrySet().stream()
                .filter(ent -> ent.getKey().equals(leaving) || ent.getValue().equals(leaving))
                .map(ent -> ent.getKey().equals(leaving) ? ent.getValue() : ent.getKey())
                .findFirst();

        partner.ifPresent(p ->
                p.kickPlayer(ChatColor.RED + "Your partner left. Episode ended.")
        );
    }

    // =====================================================================================
    // State Accessors for CameraManager
    // =====================================================================================

    public boolean isEpisodeRunning() { return episodeRunning; }

    public boolean isCamera(Player p) {
        return activeCameraControllers.containsKey(p.getUniqueId()) ||
               controllerToCamera.containsValue(p.getName());
    }

    public boolean isSpectator(Player p) {
        return controllerBySpectator.containsKey(p.getUniqueId());
    }

    public Map<Player, Player> getActivePairs() { return activePairs; }

    public Map<UUID, UUID> getActiveCameraControllers() { return activeCameraControllers; }

    public CameraManager getCameraManager() { return cameraManager; }

    public WorldStateLogger getWorldStateLogger() { return worldStateLogger; }

    public UUID getSpectatorForController(UUID controllerId) {
        return activeSpectatorsByController.get(controllerId);
    }

    public Map<UUID, UUID> getSpectatorsByController() {
        return activeSpectatorsByController;
    }

    private void attachSpectatorIfMatches(Player p) {
        if (!episodeRunning) return;
        if (controllerBySpectator.containsKey(p.getUniqueId())) return;

        for (String controllerName : controllerToCamera.keySet()) {
            if (("Spectator" + controllerName).equals(p.getName())) {
                Player controller = Bukkit.getPlayerExact(controllerName);
                if (controller != null) {
                    p.setGameMode(GameMode.SPECTATOR);
                    activeSpectatorsByController.put(controller.getUniqueId(), p.getUniqueId());
                    controllerBySpectator.put(p.getUniqueId(), controller.getUniqueId());
                }
                break;
            }
        }
    }

    // =====================================================================================

    private static class StartConfig {
        final String controller;
        final String camera;
        final File skin;

        StartConfig(String c, String m, File s) {
            this.controller = c;
            this.camera = m;
            this.skin = s;
        }
    }
}
