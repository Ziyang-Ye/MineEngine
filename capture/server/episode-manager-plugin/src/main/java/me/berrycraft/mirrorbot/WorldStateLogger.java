package me.berrycraft.mirrorbot;

import com.destroystokyo.paper.event.entity.EntityKnockbackByEntityEvent;
import com.destroystokyo.paper.event.entity.EntityRemoveFromWorldEvent;
import io.papermc.paper.event.entity.EntityKnockbackEvent;
import io.papermc.paper.event.entity.EntityPushedByEntityAttackEvent;
import io.papermc.paper.event.player.PlayerArmSwingEvent;

import org.bukkit.Bukkit;
import org.bukkit.Chunk;
import org.bukkit.ChunkSnapshot;
import org.bukkit.GameRule;
import org.bukkit.Location;
import org.bukkit.Material;
import org.bukkit.World;
import org.bukkit.attribute.Attribute;
import org.bukkit.attribute.AttributeInstance;
import org.bukkit.block.Biome;
import org.bukkit.block.Block;
import org.bukkit.block.BlockFace;
import org.bukkit.block.BlockState;
import org.bukkit.block.data.BlockData;
import org.bukkit.command.Command;
import org.bukkit.command.CommandExecutor;
import org.bukkit.command.CommandSender;
import org.bukkit.entity.Entity;
import org.bukkit.entity.ExperienceOrb;
import org.bukkit.entity.FallingBlock;
import org.bukkit.entity.HumanEntity;
import org.bukkit.entity.Item;
import org.bukkit.entity.LivingEntity;
import org.bukkit.entity.Player;
import org.bukkit.entity.Projectile;
import org.bukkit.entity.TNTPrimed;
import org.bukkit.event.EventHandler;
import org.bukkit.event.EventPriority;
import org.bukkit.event.Listener;
import org.bukkit.event.block.BlockBreakEvent;
import org.bukkit.event.block.BlockBurnEvent;
import org.bukkit.event.block.BlockDamageAbortEvent;
import org.bukkit.event.block.BlockDamageEvent;
import org.bukkit.event.block.BlockExplodeEvent;
import org.bukkit.event.block.BlockFadeEvent;
import org.bukkit.event.block.BlockFertilizeEvent;
import org.bukkit.event.block.BlockFormEvent;
import org.bukkit.event.block.BlockFromToEvent;
import org.bukkit.event.block.BlockGrowEvent;
import org.bukkit.event.block.BlockIgniteEvent;
import org.bukkit.event.block.BlockMultiPlaceEvent;
import org.bukkit.event.block.BlockPistonExtendEvent;
import org.bukkit.event.block.BlockPistonRetractEvent;
import org.bukkit.event.block.BlockPlaceEvent;
import org.bukkit.event.block.BlockSpreadEvent;
import org.bukkit.event.block.EntityBlockFormEvent;
import org.bukkit.event.block.FluidLevelChangeEvent;
import org.bukkit.event.block.LeavesDecayEvent;
import org.bukkit.event.block.SpongeAbsorbEvent;
import org.bukkit.event.block.TNTPrimeEvent;
import org.bukkit.event.entity.CreatureSpawnEvent;
import org.bukkit.event.entity.EntityChangeBlockEvent;
import org.bukkit.event.entity.EntityDamageByBlockEvent;
import org.bukkit.event.entity.EntityDamageByEntityEvent;
import org.bukkit.event.entity.EntityDamageEvent;
import org.bukkit.event.entity.EntityDeathEvent;
import org.bukkit.event.entity.EntityExplodeEvent;
import org.bukkit.event.entity.EntityShootBowEvent;
import org.bukkit.event.entity.EntitySpawnEvent;
import org.bukkit.event.entity.ItemSpawnEvent;
import org.bukkit.event.entity.PlayerDeathEvent;
import org.bukkit.event.player.PlayerBucketEmptyEvent;
import org.bukkit.event.player.PlayerBucketFillEvent;
import org.bukkit.event.player.PlayerAnimationEvent;
import org.bukkit.event.player.PlayerInteractEvent;
import org.bukkit.event.player.PlayerItemConsumeEvent;
import org.bukkit.event.player.PlayerItemHeldEvent;
import org.bukkit.event.weather.ThunderChangeEvent;
import org.bukkit.event.weather.WeatherChangeEvent;
import org.bukkit.event.world.StructureGrowEvent;
import org.bukkit.event.world.WorldLoadEvent;
import org.bukkit.inventory.EntityEquipment;
import org.bukkit.inventory.EquipmentSlot;
import org.bukkit.inventory.ItemStack;
import org.bukkit.plugin.java.JavaPlugin;
import org.bukkit.scheduler.BukkitTask;
import org.bukkit.util.BoundingBox;
import org.bukkit.util.Vector;

import java.io.BufferedWriter;
import java.io.File;
import java.io.FileOutputStream;
import java.io.IOException;
import java.io.OutputStreamWriter;
import java.io.Writer;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.StandardCopyOption;
import java.util.ArrayList;
import java.util.Arrays;
import java.util.Collection;
import java.util.HashMap;
import java.util.HashSet;
import java.util.LinkedHashMap;
import java.util.LinkedHashSet;
import java.util.List;
import java.util.Locale;
import java.util.Map;
import java.util.Objects;
import java.util.Set;
import java.util.UUID;
import java.util.concurrent.ConcurrentLinkedQueue;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.Future;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicInteger;
import java.util.zip.GZIPOutputStream;

/**
 * Per-episode world-state logger.
 *
 * <p>Driven over RCON by the leader controller bot:
 * <pre>
 *   worldstate begin stem=STEM episode=N [instance=I] [type=T] [players=Alpha,Bravo]
 *                    [center=x,y,z] [radius=32] [down=24] [up=40] [log_radius=64]
 *                    [snapshot=blocks|region|both|none] [heartbeat=1]
 *                    [entities_every=1] [entities_radius=64] [diff_every=20]
 *   worldstate end  [stem=STEM] [snapshot=blocks|none] [restore=0|1]
 *   worldstate note  free text (stored verbatim as a string)
 *   worldstate info  episode=N
 *   worldstate status
 * </pre>
 *
 * <p>Writes, into $WORLD_STATE_OUTPUT_DIR (default /episode_output):
 * <ul>
 *   <li>STEM_world_events.jsonl            one JSON object per line (see OUTPUT_SCHEMA.md)</li>
 *   <li>STEM_world_snapshot_start.json.gz  block snapshot of the episode box at "begin"</li>
 *   <li>STEM_world_snapshot_end.json.gz    block snapshot of the same box at "end"</li>
 *   <li>world_regions/STEM_start/r.X.Z.mca region files (snapshot=region|both)</li>
 * </ul>
 *
 * <p>Every record carries game_time (World#getGameTime, the same counter that Mineflayer exposes as
 * bot.time.age and that the per-frame action JSON stores as worldAgeTicks), server_tick and epoch_ms
 * (host wall clock, the same clock as the per-frame epochTime and the camera MKV timestamps).
 */
public final class WorldStateLogger implements Listener, CommandExecutor {

    static final String EVENTS_FORMAT = "solaris-world-events-v1";
    static final String SNAPSHOT_FORMAT = "solaris-block-snapshot-v1";

    private final JavaPlugin plugin;
    private final File outputDir;
    private final ExecutorService io;
    private final Map<Integer, String> summaryByEpisode = new LinkedHashMap<>();
    private BukkitTask tickTask;
    private Session session;
    // T17: BLOCK_BREAK_ANIMATION packets captured by CameraManager's ProtocolLib listener (possibly
    // off the main thread); drained into dig_progress records by the logger tick.
    private final ConcurrentLinkedQueue<DigPacket> digQueue = new ConcurrentLinkedQueue<>();
    private volatile boolean acceptDigPackets = false;

    public WorldStateLogger(JavaPlugin plugin) {
        this.plugin = plugin;
        String dir = System.getenv("WORLD_STATE_OUTPUT_DIR");
        if (dir == null || dir.isBlank()) dir = "/episode_output";
        this.outputDir = new File(dir);
        this.io = Executors.newSingleThreadExecutor(r -> {
            Thread t = new Thread(r, "WorldStateLogger-IO");
            t.setDaemon(true);
            return t;
        });
    }

    public void start() {
        tickTask = Bukkit.getScheduler().runTaskTimer(plugin, this::onTick, 1L, 1L);
        plugin.getLogger().info("[worldstate] output dir: " + outputDir.getAbsolutePath());
        List<String> changed = disableAdvancementAnnouncements();
        plugin.getLogger().info("[worldstate] gamerule announceAdvancements=false in all worlds"
                + (changed.isEmpty() ? "" : " (changed: " + changed + ")"));
    }

    /**
     * Sets gamerule announceAdvancements=false in every loaded world: the vanilla
     * "X has made the advancement [...]" chat line would otherwise appear in the camera frames.
     * Returns the names of the worlds where the rule was still true.
     */
    static List<String> disableAdvancementAnnouncements() {
        List<String> changed = new ArrayList<>();
        for (World w : Bukkit.getWorlds()) {
            try {
                if (!Boolean.FALSE.equals(w.getGameRuleValue(GameRule.ANNOUNCE_ADVANCEMENTS))) {
                    w.setGameRule(GameRule.ANNOUNCE_ADVANCEMENTS, false);
                    changed.add(w.getName());
                }
            } catch (Throwable ignored) {
            }
        }
        return changed;
    }

    @EventHandler(priority = EventPriority.MONITOR)
    public void onWorldLoad(WorldLoadEvent e) {
        try {
            e.getWorld().setGameRule(GameRule.ANNOUNCE_ADVANCEMENTS, false);
        } catch (Throwable ignored) {
        }
    }

    public void shutdown() {
        if (tickTask != null) {
            tickTask.cancel();
            tickTask = null;
        }
        if (session != null) {
            try {
                endSession(false, false, "plugin_disable");
            } catch (Exception e) {
                plugin.getLogger().warning("[worldstate] failed to close session on disable: " + e);
            }
        }
        io.shutdown();
        try {
            io.awaitTermination(30, TimeUnit.SECONDS);
        } catch (InterruptedException ignored) {
            Thread.currentThread().interrupt();
        }
    }

    // =====================================================================================
    // Commands
    // =====================================================================================

    @Override
    public boolean onCommand(CommandSender sender, Command cmd, String label, String[] args) {
        if (args.length == 0) {
            sender.sendMessage("usage: worldstate <begin|end|note|info|status> [key=value...]");
            return true;
        }
        String sub = args[0].toLowerCase(Locale.ROOT);
        String[] rest = Arrays.copyOfRange(args, 1, args.length);
        try {
            switch (sub) {
                case "begin" -> sender.sendMessage(cmdBegin(parseKv(rest)));
                case "end" -> sender.sendMessage(cmdEnd(parseKv(rest)));
                case "note" -> sender.sendMessage(cmdNote(String.join(" ", rest)));
                case "info" -> sender.sendMessage(cmdInfo(parseKv(rest)));
                case "status" -> sender.sendMessage(cmdStatus());
                default -> sender.sendMessage("{\"ok\":false,\"error\":\"unknown subcommand\"}");
            }
        } catch (Exception e) {
            plugin.getLogger().warning("[worldstate] command failed: " + e);
            e.printStackTrace();
            sender.sendMessage(new J().put("ok", false).put("error", String.valueOf(e)).done());
        }
        return true;
    }

    private static Map<String, String> parseKv(String[] tokens) {
        Map<String, String> m = new HashMap<>();
        for (String t : tokens) {
            int eq = t.indexOf('=');
            if (eq > 0) m.put(t.substring(0, eq).toLowerCase(Locale.ROOT), t.substring(eq + 1));
        }
        return m;
    }

    private static int intArg(Map<String, String> kv, String key, int def) {
        String v = kv.get(key);
        if (v == null || v.isBlank()) return def;
        try {
            return Integer.parseInt(v.trim());
        } catch (NumberFormatException e) {
            return def;
        }
    }

    private static String sanitizeStem(String stem) {
        if (stem == null || stem.isBlank()) return null;
        return stem.replaceAll("[^A-Za-z0-9_.-]", "_");
    }

    private String cmdBegin(Map<String, String> kv) throws Exception {
        if (session != null) {
            plugin.getLogger().warning("[worldstate] begin while session " + session.stem + " active; closing it");
            endSession(false, false, "superseded");
        }
        List<String> advChanged = disableAdvancementAnnouncements();
        int episode = intArg(kv, "episode", -1);
        int instance = intArg(kv, "instance", -1);
        String stem = sanitizeStem(kv.get("stem"));
        if (stem == null) {
            stem = String.format(Locale.ROOT, "%d_%06d_world_instance_%03d",
                    System.currentTimeMillis() / 1000L, Math.max(episode, 0), Math.max(instance, 0));
        }
        List<String> players = new ArrayList<>();
        for (String p : kv.getOrDefault("players", "Alpha,Bravo").split(",")) {
            if (!p.isBlank()) players.add(p.trim());
        }

        World world = null;
        List<Location> locs = new ArrayList<>();
        for (String name : players) {
            Player p = Bukkit.getPlayerExact(name);
            if (p != null && p.isOnline()) {
                if (world == null) world = p.getWorld();
                if (p.getWorld().equals(world)) locs.add(p.getLocation());
            }
        }
        if (world == null) world = Bukkit.getWorlds().get(0);

        double cx, cy, cz;
        String centerArg = kv.get("center");
        if (centerArg != null && centerArg.split(",").length == 3) {
            String[] c = centerArg.split(",");
            cx = Double.parseDouble(c[0]);
            cy = Double.parseDouble(c[1]);
            cz = Double.parseDouble(c[2]);
        } else if (!locs.isEmpty()) {
            cx = 0; cy = 0; cz = 0;
            for (Location l : locs) { cx += l.getX(); cy += l.getY(); cz += l.getZ(); }
            cx /= locs.size(); cy /= locs.size(); cz /= locs.size();
        } else {
            Location s = world.getSpawnLocation();
            cx = s.getX(); cy = s.getY(); cz = s.getZ();
        }
        double lowY = cy, highY = cy;
        for (Location l : locs) { lowY = Math.min(lowY, l.getY()); highY = Math.max(highY, l.getY()); }

        int radius = Math.max(0, intArg(kv, "radius", 32));
        int down = Math.max(0, intArg(kv, "down", 24));
        int up = Math.max(0, intArg(kv, "up", 40));
        int minX = (int) Math.floor(cx) - radius;
        int maxX = (int) Math.floor(cx) + radius;
        int minZ = (int) Math.floor(cz) - radius;
        int maxZ = (int) Math.floor(cz) + radius;
        int minY = Math.max(world.getMinHeight(), (int) Math.floor(lowY) - down);
        int maxY = Math.min(world.getMaxHeight() - 1, (int) Math.floor(highY) + up);

        String snapshotMode = kv.getOrDefault("snapshot", "blocks").toLowerCase(Locale.ROOT);
        boolean writeBlocks = snapshotMode.equals("blocks") || snapshotMode.equals("both");
        boolean writeRegion = snapshotMode.equals("region") || snapshotMode.equals("both");

        if (!outputDir.exists() && !outputDir.mkdirs()) {
            throw new IOException("cannot create output dir " + outputDir);
        }
        File eventsFile = new File(outputDir, stem + "_world_events.jsonl");
        Session s = new Session(stem, episode, instance, kv.getOrDefault("type", ""), world, players,
                minX, minY, minZ, maxX, maxY, maxZ,
                intArg(kv, "log_radius", 64), Math.max(1, intArg(kv, "heartbeat", 1)),
                Math.max(0, intArg(kv, "entities_every", 1)), Math.max(1, intArg(kv, "entities_radius", 64)),
                Math.max(0, intArg(kv, "diff_every", 20)), eventsFile);
        s.diffFaultAt = Math.max(0, intArg(kv, "diff_fault", 0)); // test only: Nth diff throws halfway
        session = s;
        digQueue.clear();
        acceptDigPackets = true;
        s.refreshPlayerCache();

        // ---- initial snapshot (always captured in memory: needed for restore and as the first
        // baseline of the periodic full-box diff) ----
        String startSnapshotName = writeBlocks ? stem + "_world_snapshot_start.json.gz" : null;
        s.diffBaselineGameTime = world.getGameTime();
        s.initial = captureBox(s, "start", startSnapshotName);

        List<String> regionFiles = new ArrayList<>();
        if (writeRegion) {
            regionFiles = copyRegionFiles(s, stem + "_start");
        }

        // ---- begin record ----
        J w = new J()
                .put("name", world.getName())
                .put("uid", world.getUID().toString())
                .put("seed", world.getSeed())
                .put("seed_str", Long.toString(world.getSeed()))
                .put("environment", world.getEnvironment().name())
                .put("world_type", String.valueOf(world.getWorldType()))
                .put("generate_structures", world.canGenerateStructures())
                .put("min_height", world.getMinHeight())
                .put("max_height", world.getMaxHeight())
                .put("difficulty", world.getDifficulty().name())
                .put("storm", world.hasStorm())
                .put("thundering", world.isThundering())
                .put("full_time", world.getFullTime())
                .put("time_of_day", world.getTime())
                .pos("spawn", world.getSpawnLocation().getBlockX(), world.getSpawnLocation().getBlockY(),
                        world.getSpawnLocation().getBlockZ())
                .raw("gamerules", gamerulesJson(world));
        J snap = new J()
                .put("mode", snapshotMode)
                .put("start_file", startSnapshotName)
                .put("end_file", null)
                .raw("region_files", jsonStringArray(regionFiles));
        J begin = s.event("episode_begin")
                .put("format", EVENTS_FORMAT)
                .put("stem", stem)
                .put("episode_id", episode)
                .put("instance_id", instance)
                .put("episode_type", s.episodeType)
                .put("events_file", eventsFile.getName())
                .raw("world", w.done())
                .raw("box", boxJson(s))
                .vec("center", cx, cy, cz)
                .put("log_radius", s.logRadius)
                .put("heartbeat_ticks", s.heartbeatEvery)
                .put("entities_every", s.entitiesEvery)
                .put("entities_radius", s.entitiesRadius)
                .vec("entities_center", s.entCx, s.entCy, s.entCz)
                .put("diff_every", s.diffEvery)
                .put("diff_fault", s.diffFaultAt)
                .put("announce_advancements", false)
                .raw("announce_advancements_changed", jsonStringArray(advChanged))
                .raw("tracked_players", jsonStringArray(players))
                .raw("snapshot", snap.done())
                .put("server_version", Bukkit.getVersion())
                .put("bukkit_version", Bukkit.getBukkitVersion())
                .put("plugin_version", plugin.getDescription().getVersion());
        s.write(begin);
        s.writePlayersTick(true);
        if (s.entitiesEvery > 0) s.writeEntities();
        s.flush();

        String reply = new J()
                .put("ok", true)
                .put("stem", stem)
                .put("episode_id", episode)
                .put("events_file", eventsFile.getName())
                .put("snapshot_start_file", startSnapshotName)
                .raw("region_files", jsonStringArray(regionFiles))
                .put("seed", world.getSeed())
                .put("seed_str", Long.toString(world.getSeed()))
                .put("world", world.getName())
                .put("world_type", String.valueOf(world.getWorldType()))
                .put("generate_structures", world.canGenerateStructures())
                .put("game_time", world.getGameTime())
                .put("server_tick", Bukkit.getCurrentTick())
                .put("epoch_ms", System.currentTimeMillis())
                .raw("box", boxJson(s))
                .put("diff_every", s.diffEvery)
                .done();
        s.beginSummary = reply;
        rememberSummary(episode, reply, null);
        plugin.getLogger().info("[worldstate] begin " + reply);
        return reply;
    }

    private String cmdEnd(Map<String, String> kv) throws Exception {
        if (session == null) {
            return "{\"ok\":false,\"error\":\"no active session\"}";
        }
        String stem = sanitizeStem(kv.get("stem"));
        if (stem != null && !stem.equals(session.stem)) {
            return new J().put("ok", false).put("error", "stem mismatch")
                    .put("active_stem", session.stem).done();
        }
        boolean writeEnd = !"none".equalsIgnoreCase(kv.getOrDefault("snapshot", "blocks"));
        boolean restore = "1".equals(kv.getOrDefault("restore", "0")) || "true".equalsIgnoreCase(kv.get("restore"));
        return endSession(writeEnd, restore, "command");
    }

    private String cmdNote(String text) throws IOException {
        if (session == null) return "{\"ok\":false,\"error\":\"no active session\"}";
        session.write(session.event("note").put("text", text));
        return "{\"ok\":true}";
    }

    private String cmdInfo(Map<String, String> kv) {
        int episode = intArg(kv, "episode", -1);
        String s = summaryByEpisode.get(episode);
        if (s == null) return new J().put("ok", false).put("error", "unknown episode").put("episode_id", episode).done();
        return s;
    }

    /** Stem of the open world-log session (an episode is being recorded), or null. Main thread. */
    public String activeStem() {
        Session s = session;
        return s == null ? null : s.stem;
    }

    private String cmdStatus() {
        if (session == null) return new J().put("ok", true).put("active", false).put("output_dir", outputDir.getAbsolutePath()).done();
        return new J().put("ok", true).put("active", true).put("stem", session.stem)
                .put("episode_id", session.episodeId).put("events", session.eventCount)
                .put("output_dir", outputDir.getAbsolutePath()).done();
    }

    private void rememberSummary(int episode, String begin, String end) {
        String merged = new J().put("ok", true).raw("begin", begin).raw("end", end).done();
        summaryByEpisode.put(episode, merged);
        while (summaryByEpisode.size() > 512) {
            Integer first = summaryByEpisode.keySet().iterator().next();
            summaryByEpisode.remove(first);
        }
    }

    private String endSession(boolean writeEndSnapshot, boolean restore, String reason) throws Exception {
        Session s = session;
        if (s == null) return "{\"ok\":false,\"error\":\"no active session\"}";
        acceptDigPackets = false;
        // results of box diffs still running on the IO thread: wait, then write them BEFORE the forced
        // readback below, so every voxel they make 'touched' is re-read in this same tick
        if (s.diffEvery > 0) {
            long w0 = System.nanoTime();
            try {
                io.submit(() -> null).get(60, TimeUnit.SECONDS);
            } catch (Exception e) {
                plugin.getLogger().warning("[worldstate] waiting for box diffs failed: " + e);
            }
            s.diffEndWaitMs += (System.nanoTime() - w0) / 1e6;
            s.drainBoxDiffs();
        }
        s.requeueTouched(true);
        s.processDirty();
        s.drainDigPackets();
        s.updateItemUse(true);
        s.writePlayersTick(true);
        if (s.entitiesEvery > 0) s.writeEntities();

        String endName = writeEndSnapshot ? s.stem + "_world_snapshot_end.json.gz" : null;
        long t0 = System.nanoTime();
        Map<Long, ChunkSnapshot> endChunks = snapshotChunks(s);
        s.endSnapshotMs = (System.nanoTime() - t0) / 1e6;
        String endEntities = entitiesJson(s);
        long gt = s.world.getGameTime();
        long ft = s.world.getFullTime();
        long ms = System.currentTimeMillis();
        int st = Bukkit.getCurrentTick();
        // final full-box diff on exactly the chunks of the end snapshot (same tick, nothing can change in
        // between): every untouched voxel that differs from the last baseline gets its box_diff record, so
        // start snapshot + block_state replay == end snapshot. It also runs after a failed periodic diff:
        // runBoxDiff never leaves a half-updated baseline, so the comparison is still exact.
        if (s.diffEvery > 0) {
            long w0 = System.nanoTime();
            try {
                DiffResult fin = io.submit(() -> runBoxDiff(s, endChunks, gt, st, 0.0))
                        .get(60, TimeUnit.SECONDS);
                s.diffEndWaitMs += (System.nanoTime() - w0) / 1e6;
                s.drainBoxDiffs();
                s.diffFinal = fin == null || fin.failed() ? "failed" : "ok";
                if (fin != null) s.applyBoxDiff(fin);
            } catch (Exception e) {
                s.diffEndWaitMs += (System.nanoTime() - w0) / 1e6;
                s.diffFinal = "failed";
                plugin.getLogger().warning("[worldstate] final box diff failed: " + e);
            }
        }
        if (endName != null) {
            final File f = new File(outputDir, endName);
            io.submit(() -> {
                encodeAndWrite(s, "end", endChunks, endEntities, gt, ft, ms, st, f);
                return null;
            });
        }

        int restoredBlocks = 0;
        int removedEntities = 0;
        if (restore) {
            Captured init = s.initial.get(60, TimeUnit.SECONDS);
            restoredBlocks = restoreBox(s, init, endChunks);
            removedEntities = removeEpisodeEntities(s);
        }

        J end = s.event("episode_end")
                .put("stem", s.stem)
                .put("episode_id", s.episodeId)
                .put("reason", reason)
                .put("events_logged", s.eventCount + 1)
                .put("snapshot_end_file", endName)
                .put("restored", restore)
                .put("restored_blocks", restoredBlocks)
                .put("removed_entities", removedEntities)
                .raw("box_diff", s.diffStatsJson());
        s.write(end);
        s.close();
        session = null;

        String reply = new J()
                .put("ok", true)
                .put("stem", s.stem)
                .put("episode_id", s.episodeId)
                .put("events_logged", s.eventCount)
                .put("snapshot_end_file", endName)
                .put("restored", restore)
                .put("restored_blocks", restoredBlocks)
                .put("removed_entities", removedEntities)
                .put("game_time", gt)
                .put("epoch_ms", ms)
                .raw("box_diff", s.diffStatsJson())
                .done();
        rememberSummary(s.episodeId, s.beginSummary, reply);
        plugin.getLogger().info("[worldstate] end " + reply);
        return reply;
    }

    // =====================================================================================
    // Tick: heartbeat + dirty-position readback
    // =====================================================================================

    private void onTick() {
        Session s = session;
        if (s == null) return;
        try {
            s.refreshPlayerCache();
            s.drainBoxDiffs(); // before the readback: voxels found by a box diff are re-read this tick
            s.requeueTouched(false);
            s.processDirty();
            s.drainDigPackets();
            s.updateItemUse(false);
            s.ticks++;
            if (s.ticks % s.heartbeatEvery == 0) s.writePlayersTick(false);
            if (s.entitiesEvery > 0 && s.ticks % s.entitiesEvery == 0) s.writeEntities();
            if (s.diffEvery > 0 && s.ticks % s.diffEvery == 0) scheduleBoxDiff(s);
            if (s.ticks % 20 == 0) s.flush();
        } catch (IOException e) {
            plugin.getLogger().warning("[worldstate] write failed: " + e);
        } catch (RuntimeException e) {
            // never let one bad entity / record kill the per-tick logging; warn at most once a second
            if (s.ticks % 20 == 0 || s.tickErrors++ == 0) {
                plugin.getLogger().warning("[worldstate] tick failed: " + e);
                e.printStackTrace();
            }
        }
    }

    // =====================================================================================
    // Snapshot capture / encode / restore
    // =====================================================================================

    /** Result of encoding a box: palette + per-voxel palette indices (YZX order, x fastest). */
    static final class Captured {
        final List<BlockData> palette;
        final int[] idx;

        Captured(List<BlockData> palette, int[] idx) {
            this.palette = palette;
            this.idx = idx;
        }
    }

    private static long chunkKey(int cx, int cz) {
        return (((long) cx) << 32) ^ (cz & 0xffffffffL);
    }

    private Map<Long, ChunkSnapshot> snapshotChunks(Session s) {
        Map<Long, ChunkSnapshot> chunks = new HashMap<>();
        for (int cx = Math.floorDiv(s.minX, 16); cx <= Math.floorDiv(s.maxX, 16); cx++) {
            for (int cz = Math.floorDiv(s.minZ, 16); cz <= Math.floorDiv(s.maxZ, 16); cz++) {
                Chunk c = s.world.getChunkAt(cx, cz);
                chunks.put(chunkKey(cx, cz), c.getChunkSnapshot(false, true, false));
            }
        }
        return chunks;
    }

    private Future<Captured> captureBox(Session s, String which, String fileName) {
        Map<Long, ChunkSnapshot> chunks = snapshotChunks(s);
        String entities = entitiesJson(s);
        long gt = s.world.getGameTime();
        long ft = s.world.getFullTime();
        long ms = System.currentTimeMillis();
        int st = Bukkit.getCurrentTick();
        final File f = fileName == null ? null : new File(outputDir, fileName);
        return io.submit(() -> encodeAndWrite(s, which, chunks, entities, gt, ft, ms, st, f));
    }

    /** Runs on the IO thread. Builds the palette encoding and (optionally) writes the gzipped JSON. */
    private Captured encodeAndWrite(Session s, String which, Map<Long, ChunkSnapshot> chunks, String entities,
                                    long gameTime, long fullTime, long epochMs, int serverTick, File out) throws IOException {
        int sx = s.maxX - s.minX + 1, sy = s.maxY - s.minY + 1, sz = s.maxZ - s.minZ + 1;
        int[] idx = new int[sx * sy * sz];
        List<BlockData> palette = new ArrayList<>();
        Map<BlockData, Integer> lookup = new HashMap<>();
        int i = 0;
        for (int y = s.minY; y <= s.maxY; y++) {
            for (int z = s.minZ; z <= s.maxZ; z++) {
                for (int x = s.minX; x <= s.maxX; x++) {
                    ChunkSnapshot c = chunks.get(chunkKey(Math.floorDiv(x, 16), Math.floorDiv(z, 16)));
                    BlockData d = c.getBlockData(Math.floorMod(x, 16), y, Math.floorMod(z, 16));
                    Integer p = lookup.get(d);
                    if (p == null) {
                        p = palette.size();
                        palette.add(d);
                        lookup.put(d, p);
                    }
                    idx[i++] = p;
                }
            }
        }
        Captured cap = new Captured(palette, idx);
        if (out == null) return cap;

        // biomes: one sample per (x,z) column at the box centre height
        int by = Math.max(s.minY, Math.min(s.maxY, (s.minY + s.maxY) / 2));
        List<String> biomePalette = new ArrayList<>();
        Map<String, Integer> biomeLookup = new HashMap<>();
        int[] bidx = new int[sx * sz];
        int j = 0;
        for (int z = s.minZ; z <= s.maxZ; z++) {
            for (int x = s.minX; x <= s.maxX; x++) {
                ChunkSnapshot c = chunks.get(chunkKey(Math.floorDiv(x, 16), Math.floorDiv(z, 16)));
                String key = biomeKey(c.getBiome(Math.floorMod(x, 16), by, Math.floorMod(z, 16)));
                Integer p = biomeLookup.get(key);
                if (p == null) {
                    p = biomePalette.size();
                    biomePalette.add(key);
                    biomeLookup.put(key, p);
                }
                bidx[j++] = p;
            }
        }

        File tmp = new File(out.getParentFile(), out.getName() + ".tmp");
        try (Writer w = new BufferedWriter(new OutputStreamWriter(
                new GZIPOutputStream(new FileOutputStream(tmp), 1 << 16), StandardCharsets.UTF_8), 1 << 16)) {
            J head = new J()
                    .put("format", SNAPSHOT_FORMAT)
                    .put("which", which)
                    .put("stem", s.stem)
                    .put("episode_id", s.episodeId)
                    .put("world", s.world.getName())
                    .put("seed", s.world.getSeed())
                    .put("game_time", gameTime)
                    .put("full_time", fullTime)
                    .put("server_tick", serverTick)
                    .put("epoch_ms", epochMs)
                    .pos("origin", s.minX, s.minY, s.minZ)
                    .pos("size", sx, sy, sz)
                    .put("order", "yzx")
                    .put("biome_y", by)
                    .raw("entities", entities);
            String h = head.done();
            w.write(h, 0, h.length() - 1); // leave object open
            w.write(",\"palette\":[");
            for (int k = 0; k < palette.size(); k++) {
                if (k > 0) w.write(',');
                StringBuilder sb = new StringBuilder();
                J.str(sb, palette.get(k).getAsString());
                w.write(sb.toString());
            }
            w.write("],\"rle\":");
            writeRle(w, idx);
            w.write(",\"biome_palette\":");
            w.write(jsonStringArray(biomePalette));
            w.write(",\"biomes_rle\":");
            writeRle(w, bidx);
            w.write("}");
        }
        Files.move(tmp.toPath(), out.toPath(), StandardCopyOption.REPLACE_EXISTING);
        return cap;
    }

    private static void writeRle(Writer w, int[] a) throws IOException {
        w.write('[');
        boolean first = true;
        int i = 0;
        while (i < a.length) {
            int v = a[i];
            int run = 1;
            while (i + run < a.length && a[i + run] == v) run++;
            if (!first) w.write(',');
            first = false;
            w.write(Integer.toString(run));
            w.write(',');
            w.write(Integer.toString(v));
            i += run;
        }
        w.write(']');
    }

    private static String biomeKey(Biome b) {
        if (b == null) return "unknown";
        try {
            return b.getKey().toString();
        } catch (Throwable t) {
            return String.valueOf(b);
        }
    }

    // =====================================================================================
    // Periodic full-box diff: block changes that fire no Bukkit event
    // =====================================================================================
    //
    // Random ticks (vines, kelp, cocoa, sugar cane, cactus, bamboo, ...), leaf 'distance' updates
    // caused by neighbour changes, snow / ice by weather, liquid levels of untouched voxels, ... change
    // blocks without any event, so the start snapshot + logged changes would not reproduce the end
    // snapshot. Every diff_every ticks the main thread takes ChunkSnapshots of the box (palette copies,
    // cheap); the IO thread compares them voxel by voxel with the previous full-box state (an int[] of
    // palette indices, seeded from the start snapshot) and hands the changed voxels back through a
    // queue; the main thread writes them (seq order) as block_state records with source "box_diff" and
    // makes the voxels 'touched', so the per-tick readback follows them from then on.

    private record DiffChange(long key, String state, String block, String prev) {}

    private record DiffResult(long observedGameTime, int observedServerTick, long baselineGameTime,
                              List<DiffChange> changes, double mainMs, double ioMs, boolean failed,
                              int chunksSkipped) {}

    /**
     * Main thread: snapshots of the box chunks for a periodic diff. Chunks that are not loaded are NOT
     * loaded here (that would load / generate them synchronously every diff_every ticks); they are left
     * out and their voxels keep the old baseline (an unloaded chunk does not tick, so nothing changes).
     * The final diff uses the force-loaded chunks of the end snapshot instead.
     */
    private Map<Long, ChunkSnapshot> snapshotChunksForDiff(Session s) {
        Map<Long, ChunkSnapshot> chunks = new HashMap<>();
        for (int cx = Math.floorDiv(s.minX, 16); cx <= Math.floorDiv(s.maxX, 16); cx++) {
            for (int cz = Math.floorDiv(s.minZ, 16); cz <= Math.floorDiv(s.maxZ, 16); cz++) {
                if (!s.world.isChunkLoaded(cx, cz)) continue;
                Chunk c = s.world.getChunkAt(cx, cz);
                chunks.put(chunkKey(cx, cz), c.getChunkSnapshot(false, false, false));
            }
        }
        return chunks;
    }

    /** Main thread: snapshot the box and queue a diff on the IO thread (skipped while 2 are pending). */
    private void scheduleBoxDiff(Session s) {
        if (s.diffFailed) return;
        if (s.diffPending.get() >= 2) {
            s.diffsSkipped++;
            return;
        }
        long t0 = System.nanoTime();
        Map<Long, ChunkSnapshot> chunks = snapshotChunksForDiff(s);
        double mainMs = (System.nanoTime() - t0) / 1e6;
        long gt = s.world.getGameTime();
        int st = Bukkit.getCurrentTick();
        s.diffPending.incrementAndGet();
        io.submit(() -> {
            try {
                DiffResult r = runBoxDiff(s, chunks, gt, st, mainMs);
                if (r != null) s.diffResults.add(r);
            } finally {
                s.diffPending.decrementAndGet();
            }
            return null;
        });
    }

    /**
     * IO thread only (owns diffPalette / diffBaseline). Compares the box in {@code chunks} with the previous
     * full-box state, returns the changed voxels and makes {@code chunks} the new baseline. Voxels of chunks
     * missing from {@code chunks} (not loaded) are skipped and keep their baseline. The baseline is only
     * updated after the whole box was compared, so a failure leaves it intact (that diff's changes are not
     * lost: the next diff - at the latest the final one - still sees them against the consistent baseline).
     */
    private DiffResult runBoxDiff(Session s, Map<Long, ChunkSnapshot> chunks, long gameTime, int serverTick,
                                  double mainMs) {
        long t0 = System.nanoTime();
        try {
            if (s.diffBaseline == null) {
                // the start snapshot was submitted to this (single) IO thread first, so it is done
                Captured init = s.initial.get();
                List<BlockData> pal = new ArrayList<>(init.palette);
                Map<BlockData, Integer> look = new HashMap<>();
                for (int k = 0; k < pal.size(); k++) look.put(pal.get(k), k);
                s.diffPalette = pal;
                s.diffLookup = look;
                s.diffBaseline = init.idx.clone(); // init.idx stays untouched (used by restore)
            }
            int cx0 = Math.floorDiv(s.minX, 16), cz0 = Math.floorDiv(s.minZ, 16);
            int ncx = Math.floorDiv(s.maxX, 16) - cx0 + 1, ncz = Math.floorDiv(s.maxZ, 16) - cz0 + 1;
            ChunkSnapshot[] grid = new ChunkSnapshot[ncx * ncz];
            int skipped = 0;
            for (int a = 0; a < ncx; a++) {
                for (int b = 0; b < ncz; b++) {
                    grid[a * ncz + b] = chunks.get(chunkKey(cx0 + a, cz0 + b));
                    if (grid[a * ncz + b] == null) skipped++;
                }
            }
            int[] base = s.diffBaseline;
            // palette / lookup are append-only (an entry added before a failure is harmless)
            List<BlockData> palette = s.diffPalette;
            Map<BlockData, Integer> lookup = s.diffLookup;
            List<DiffChange> changes = new ArrayList<>();
            int[] chIdx = new int[64], chVal = new int[64];
            int n = 0;
            int i = 0;
            boolean fault = ++s.diffRuns == s.diffFaultAt;
            int faultY = (s.minY + s.maxY) / 2;
            for (int y = s.minY; y <= s.maxY; y++) {
                if (fault && y == faultY) throw new IllegalStateException("diff_fault injected at y=" + y);
                for (int z = s.minZ; z <= s.maxZ; z++) {
                    int row = Math.floorDiv(z, 16) - cz0, lz = Math.floorMod(z, 16);
                    int x = s.minX;
                    while (x <= s.maxX) {
                        // one run of x inside a single chunk column
                        int cx = Math.floorDiv(x, 16);
                        int runEnd = Math.min(s.maxX, cx * 16 + 15);
                        ChunkSnapshot c = grid[(cx - cx0) * ncz + row];
                        if (c == null) { // chunk not loaded: keep the baseline of this run
                            i += runEnd - x + 1;
                            x = runEnd + 1;
                            continue;
                        }
                        for (; x <= runEnd; x++, i++) {
                            BlockData d = c.getBlockData(Math.floorMod(x, 16), y, lz);
                            Integer p = lookup.get(d);
                            if (p == null) {
                                p = palette.size();
                                palette.add(d);
                                lookup.put(d, p);
                            }
                            int old = base[i];
                            if (p != old) {
                                changes.add(new DiffChange(pack(x, y, z), d.getAsString(), key(d.getMaterial()),
                                        palette.get(old).getAsString()));
                                if (n == chIdx.length) {
                                    chIdx = Arrays.copyOf(chIdx, n * 2);
                                    chVal = Arrays.copyOf(chVal, n * 2);
                                }
                                chIdx[n] = i;
                                chVal[n] = p;
                                n++;
                            }
                        }
                    }
                }
            }
            if (i != base.length) throw new IllegalStateException("box size " + i + " != baseline " + base.length);
            // commit: only now does the baseline become this snapshot
            for (int k = 0; k < n; k++) base[chIdx[k]] = chVal[k];
            long baselineGt = s.diffBaselineGameTime;
            s.diffBaselineGameTime = gameTime;
            return new DiffResult(gameTime, serverTick, baselineGt, changes, mainMs,
                    (System.nanoTime() - t0) / 1e6, false, skipped);
        } catch (Throwable t) {
            s.diffFailed = true;
            plugin.getLogger().warning("[worldstate] box diff failed (periodic diffs disabled for this episode,"
                    + " baseline kept): " + t);
            return new DiffResult(gameTime, serverTick, s.diffBaselineGameTime, List.of(), mainMs,
                    (System.nanoTime() - t0) / 1e6, true, 0);
        }
    }

    /** Restores every voxel of the box to its initial state (no physics). Main thread. */
    private int restoreBox(Session s, Captured init, Map<Long, ChunkSnapshot> current) {
        int changed = 0;
        int i = 0;
        for (int y = s.minY; y <= s.maxY; y++) {
            for (int z = s.minZ; z <= s.maxZ; z++) {
                for (int x = s.minX; x <= s.maxX; x++) {
                    BlockData want = init.palette.get(init.idx[i++]);
                    ChunkSnapshot c = current.get(chunkKey(Math.floorDiv(x, 16), Math.floorDiv(z, 16)));
                    BlockData have = c.getBlockData(Math.floorMod(x, 16), y, Math.floorMod(z, 16));
                    if (!have.equals(want)) {
                        s.world.getBlockAt(x, y, z).setBlockData(want, false);
                        changed++;
                    }
                }
            }
        }
        return changed;
    }

    /** Removes entities spawned during the episode and loose drops/TNT/falling blocks in the box. */
    private int removeEpisodeEntities(Session s) {
        int removed = 0;
        for (UUID id : s.spawned) {
            Entity e = Bukkit.getEntity(id);
            if (e != null && e.isValid() && !(e instanceof Player)) {
                e.remove();
                removed++;
            }
        }
        BoundingBox bb = new BoundingBox(s.minX, s.minY, s.minZ, s.maxX + 1, s.maxY + 1, s.maxZ + 1);
        for (Entity e : s.world.getNearbyEntities(bb)) {
            if (e instanceof Item || e instanceof ExperienceOrb || e instanceof FallingBlock || e instanceof TNTPrimed) {
                if (e.isValid()) {
                    e.remove();
                    removed++;
                }
            } else if (e instanceof Player p) {
                p.setFireTicks(0);
            }
        }
        return removed;
    }

    /** Saves the world (flush) and copies the region files that overlap the box. Main thread. */
    private List<String> copyRegionFiles(Session s, String dirName) {
        List<String> copied = new ArrayList<>();
        try {
            Bukkit.dispatchCommand(Bukkit.getConsoleSender(), "save-all flush");
            File worldDir = s.world.getWorldFolder();
            File regionDir = new File(worldDir, "region");
            File dest = new File(new File(outputDir, "world_regions"), dirName);
            if (!dest.exists() && !dest.mkdirs()) throw new IOException("cannot create " + dest);
            Set<String> names = new LinkedHashSet<>();
            for (int cx = Math.floorDiv(s.minX, 16); cx <= Math.floorDiv(s.maxX, 16); cx++) {
                for (int cz = Math.floorDiv(s.minZ, 16); cz <= Math.floorDiv(s.maxZ, 16); cz++) {
                    names.add("r." + Math.floorDiv(cx, 32) + "." + Math.floorDiv(cz, 32) + ".mca");
                }
            }
            for (String n : names) {
                File src = new File(regionDir, n);
                if (src.exists()) {
                    Files.copy(src.toPath(), new File(dest, n).toPath(), StandardCopyOption.REPLACE_EXISTING);
                    copied.add("world_regions/" + dirName + "/" + n);
                }
            }
            File levelDat = new File(worldDir, "level.dat");
            if (levelDat.exists()) {
                Files.copy(levelDat.toPath(), new File(dest, "level.dat").toPath(), StandardCopyOption.REPLACE_EXISTING);
                copied.add("world_regions/" + dirName + "/level.dat");
            }
        } catch (Exception e) {
            plugin.getLogger().warning("[worldstate] region copy failed: " + e);
        }
        return copied;
    }

    private String entitiesJson(Session s) {
        BoundingBox bb = new BoundingBox(s.minX, s.minY, s.minZ, s.maxX + 1, s.maxY + 1, s.maxZ + 1);
        StringBuilder sb = new StringBuilder("[");
        boolean first = true;
        for (Entity e : s.world.getNearbyEntities(bb)) {
            if (!first) sb.append(',');
            first = false;
            sb.append(entityJson(e));
        }
        return sb.append(']').toString();
    }

    // =====================================================================================
    // Helpers: JSON fragments
    // =====================================================================================

    private static String boxJson(Session s) {
        return new J().pos("min", s.minX, s.minY, s.minZ).pos("max", s.maxX, s.maxY, s.maxZ).done();
    }

    private static String gamerulesJson(World world) {
        J j = new J();
        try {
            String[] names = world.getGameRules();
            Arrays.sort(names);
            for (String r : names) j.put(r, world.getGameRuleValue(r));
        } catch (Throwable ignored) {
        }
        return j.done();
    }

    static String jsonStringArray(Collection<String> items) {
        StringBuilder sb = new StringBuilder("[");
        boolean first = true;
        for (String it : items) {
            if (!first) sb.append(',');
            first = false;
            J.str(sb, it);
        }
        return sb.append(']').toString();
    }

    static String key(Material m) {
        if (m == null) return null;
        try {
            return m.getKey().toString();
        } catch (Throwable t) {
            return m.name();
        }
    }

    static String typeKey(Entity e) {
        try {
            return e.getType().getKey().toString();
        } catch (Throwable t) {
            return e.getType().name();
        }
    }

    static String entityJson(Entity e) {
        Location l = e.getLocation();
        J j = new J()
                .put("uuid", e.getUniqueId().toString())
                .put("type", typeKey(e))
                .vec("pos", l.getX(), l.getY(), l.getZ())
                .put("yaw", l.getYaw())
                .put("pitch", l.getPitch());
        if (e instanceof Player p) j.put("name", p.getName());
        if (e instanceof Item it) {
            ItemStack st = it.getItemStack();
            j.put("item", key(st.getType())).put("count", st.getAmount());
        }
        if (e instanceof FallingBlock fb) j.put("block_state", fb.getBlockData().getAsString());
        if (e instanceof TNTPrimed t) j.put("fuse_ticks", t.getFuseTicks());
        return j.done();
    }

    /** {"id": namespaced item id, "count": n}, or null for empty / air stacks. */
    static String itemJson(ItemStack st) {
        if (st == null || st.getType().isAir() || st.getAmount() <= 0) return "null";
        return new J().put("id", key(st.getType())).put("count", st.getAmount()).done();
    }

    /** Compact entity reference {uuid, type, name}; "null" for null. */
    static String refJson(Entity e) {
        if (e == null) return "null";
        return new J().put("uuid", e.getUniqueId().toString()).put("type", typeKey(e)).put("name", entityName(e)).done();
    }

    /** Player name, else custom name, else item id for dropped items, else null. */
    @SuppressWarnings("deprecation")
    static String entityName(Entity e) {
        if (e instanceof Player p) return p.getName();
        String custom = e.getCustomName();
        if (custom != null) return custom;
        if (e instanceof Item it) return key(it.getItemStack().getType());
        return null;
    }

    /** Signed angle difference wrapped to [-180, 180). */
    static double wrapDeg(double d) {
        return d - 360.0 * Math.floor((d + 180.0) / 360.0);
    }

    /** T3: full per-tick state of one entity (see OUTPUT_SCHEMA.md, "entities" record). */
    static String entityStateJson(Entity e) {
        Location l = e.getLocation();
        Vector v = e.getVelocity();
        String type = typeKey(e);
        int colon = type.indexOf(':');
        J j = new J()
                .put("uuid", e.getUniqueId().toString())
                .put("id", e.getEntityId())
                .put("kind", colon >= 0 ? type.substring(colon + 1) : type)
                .put("type", type)
                .put("name", entityName(e))
                .put("x", l.getX())
                .put("y", l.getY())
                .put("z", l.getZ())
                .put("yaw", l.getYaw())
                .put("pitch", l.getPitch());
        LivingEntity le = e instanceof LivingEntity x ? x : null;
        // CraftBukkit reports a living entity's yaw as its head yaw (LivingEntity#getBukkitYaw =
        // yHeadRot), so head_yaw == yaw; the body orientation is body_yaw.
        if (le != null) j.put("head_yaw", l.getYaw()).put("body_yaw", le.getBodyYaw());
        j.put("vx", v.getX()).put("vy", v.getY()).put("vz", v.getZ())
                .put("bbox_w", e.getWidth())
                .put("bbox_h", e.getHeight());
        if (le != null) {
            AttributeInstance max = le.getAttribute(Attribute.GENERIC_MAX_HEALTH);
            j.put("health", le.getHealth()).put("max_health", max == null ? Double.NaN : max.getValue());
        } else {
            j.raw("health", null).raw("max_health", null);
        }
        j.put("alive", le != null ? !le.isDead() && le.isValid() : e.isValid())
                .put("on_ground", e.isOnGround())
                .put("sneaking", e.isSneaking())
                .put("sprinting", e instanceof Player p && p.isSprinting())
                .put("blocking", e instanceof HumanEntity h && h.isBlocking())
                .put("hand_raised", le != null && le.isHandRaised());
        if (e instanceof Player p) {
            j.raw("held_item", itemJson(p.getInventory().getItemInMainHand()))
                    .raw("offhand_item", itemJson(p.getInventory().getItemInOffHand()))
                    .put("held_slot", p.getInventory().getHeldItemSlot())
                    .put("gamemode", p.getGameMode().name());
        } else if (le != null) {
            EntityEquipment eq = le.getEquipment();
            if (eq != null) j.raw("held_item", itemJson(eq.getItemInMainHand()));
        }
        if (e instanceof TNTPrimed t) {
            j.put("fuse_ticks", t.getFuseTicks());
            Entity src = t.getSource();
            j.put("source", src == null ? null : src.getUniqueId().toString());
        }
        if (e instanceof Item it) j.raw("item", itemJson(it.getItemStack()));
        if (e instanceof FallingBlock fb) j.put("block_state", fb.getBlockData().getAsString());
        if (e instanceof Projectile pr) {
            j.put("shooter", pr.getShooter() instanceof Entity s ? s.getUniqueId().toString() : null);
        }
        j.put("fire_ticks", e.getFireTicks());
        return j.done();
    }

    // =====================================================================================
    // Helpers: helper players, T17 packet hand-off, per-player action state
    // =====================================================================================

    private EpisodeManager episodeManager() {
        return plugin instanceof EpisodeManager em ? em : null;
    }

    /**
     * Invisible helper players (camera clients, spectator bots) never appear in the logs. Tracked
     * players are never helpers; the name prefixes are a fallback for when no episode is running.
     */
    boolean isHelper(Player p) {
        Session s = session;
        if (s != null && s.players.contains(p.getName())) return false;
        EpisodeManager em = episodeManager();
        if (em != null && (em.isCamera(p) || em.isSpectator(p))) return true;
        String n = p.getName();
        return n.startsWith("Spectator") || n.startsWith("Camera");
    }

    /**
     * Called by CameraManager's ProtocolLib listener for every original (non-relayed)
     * BLOCK_BREAK_ANIMATION packet, once per receiving viewer. May run on a Netty thread: only
     * enqueues; the main-thread logger tick dedupes, resolves the breaker and writes dig_progress.
     */
    public void offerBreakAnimation(int breakerId, int receiverId, int x, int y, int z, int stage) {
        if (!acceptDigPackets || digQueue.size() > 100_000) return;
        digQueue.add(new DigPacket(breakerId, receiverId, x, y, z, stage,
                System.currentTimeMillis(), Bukkit.getCurrentTick()));
    }

    private record DigPacket(int breakerId, int receiverId, int x, int y, int z, int stage,
                             long epochMs, int serverTick) {}

    private record DigKey(int breakerId, long pos) {}

    /** Stage sequence logged for one (breaker, pos) plus each viewer's position in it. */
    private static final class DigStream {
        final List<Integer> logged = new ArrayList<>();
        final Map<Integer, Integer> next = new HashMap<>();

        /** True if this packet is new (log it); false if it repeats one already logged. */
        boolean accept(int receiver, int stage) {
            int i = next.getOrDefault(receiver, 0);
            for (int j = i; j < logged.size(); j++) {
                if (logged.get(j) == stage) {
                    next.put(receiver, j + 1);
                    return false;
                }
            }
            logged.add(stage);
            next.put(receiver, logged.size());
            return true;
        }
    }

    private static final class UseState {
        final String item;
        final String hand;
        final String itemJson;
        final int startTick;
        int usedTime = 0;

        UseState(String item, String hand, String itemJson, int startTick) {
            this.item = item;
            this.hand = hand;
            this.itemJson = itemJson;
            this.startTick = startTick;
        }
    }

    private static final class DigState {
        final int x, y, z;
        final int startTick;
        final boolean instaBreak;

        DigState(int x, int y, int z, int startTick, boolean instaBreak) {
            this.x = x; this.y = y; this.z = z;
            this.startTick = startTick;
            this.instaBreak = instaBreak;
        }
    }

    // =====================================================================================
    // Session
    // =====================================================================================

    private final class Session {
        final String stem;
        final int episodeId;
        final int instanceId;
        final String episodeType;
        final World world;
        final List<String> players;
        final int minX, minY, minZ, maxX, maxY, maxZ;
        final int logRadius;
        final int heartbeatEvery;
        final int entitiesEvery;
        final int entitiesRadius;
        final double entCx, entCy, entCz;
        final File eventsFile;
        final BufferedWriter out;
        final Set<Long> dirty = new LinkedHashSet<>();
        final Map<Long, String> lastKnown = new HashMap<>();
        final Set<UUID> spawned = new HashSet<>();
        final List<double[]> playerXZ = new ArrayList<>();
        final Map<String, UseState> using = new HashMap<>();
        final Map<String, DigState> digs = new HashMap<>();
        final Map<DigKey, DigStream> digStreams = new HashMap<>();
        // ---- periodic full-box diff ----
        final int diffEvery;
        final ConcurrentLinkedQueue<DiffResult> diffResults = new ConcurrentLinkedQueue<>();
        final AtomicInteger diffPending = new AtomicInteger();
        volatile boolean diffFailed = false;
        // IO thread only: previous full-box state (palette indices, YZX order) and its game time
        List<BlockData> diffPalette;
        Map<BlockData, Integer> diffLookup;
        int[] diffBaseline;
        volatile long diffBaselineGameTime;
        // main thread only: statistics (episode_end.box_diff)
        long diffsDone = 0, diffsSkipped = 0, diffChanged = 0, diffRecords = 0, diffTouchedSkipped = 0;
        double diffMainMsSum = 0, diffMainMsMax = 0, diffIoMsSum = 0, diffIoMsMax = 0;
        double diffWriteMsSum = 0, diffWriteMsMax = 0;
        long diffChunksUnloaded = 0;          // chunk-periods skipped by periodic diffs (chunk not loaded)
        String diffFinal = "not_run";         // final diff: ok | failed | not_run
        double diffEndWaitMs = 0;             // main thread blocked on the IO thread in endSession
        double endSnapshotMs = 0;             // main thread: ChunkSnapshots of the end snapshot (force-load)
        int diffFaultAt = 0;                  // test only (begin arg diff_fault): the Nth diff throws halfway
        int diffRuns = 0;                     // IO thread only: runBoxDiff calls
        Future<Captured> initial;
        String beginSummary;
        long seq = 0;
        long eventCount = 0;
        long ticks = 0;
        long tickErrors = 0;

        Session(String stem, int episodeId, int instanceId, String episodeType, World world, List<String> players,
                int minX, int minY, int minZ, int maxX, int maxY, int maxZ, int logRadius, int heartbeatEvery,
                int entitiesEvery, int entitiesRadius, int diffEvery, File eventsFile) throws IOException {
            this.stem = stem;
            this.episodeId = episodeId;
            this.instanceId = instanceId;
            this.episodeType = episodeType;
            this.world = world;
            this.players = players;
            this.minX = minX; this.minY = minY; this.minZ = minZ;
            this.maxX = maxX; this.maxY = maxY; this.maxZ = maxZ;
            this.logRadius = logRadius;
            this.heartbeatEvery = heartbeatEvery;
            this.entitiesEvery = entitiesEvery;
            this.entitiesRadius = entitiesRadius;
            this.diffEvery = diffEvery;
            // entity log centre = centre of the snapshot box (inclusive block bounds)
            this.entCx = (minX + maxX + 1) / 2.0;
            this.entCy = (minY + maxY + 1) / 2.0;
            this.entCz = (minZ + maxZ + 1) / 2.0;
            this.eventsFile = eventsFile;
            this.out = new BufferedWriter(new OutputStreamWriter(new FileOutputStream(eventsFile), StandardCharsets.UTF_8), 1 << 16);
        }

        J event(String type) {
            return event(type, System.currentTimeMillis());
        }

        /** Common fields with an explicit epoch_ms (records captured earlier, e.g. off-thread packets). */
        J event(String type, long epochMs) {
            return new J()
                    .put("type", type)
                    .put("seq", seq++)
                    .put("game_time", world.getGameTime())
                    .put("server_tick", Bukkit.getCurrentTick())
                    .put("epoch_ms", epochMs);
        }

        boolean tracked(Player p) {
            return p != null && players.contains(p.getName());
        }

        void write(J j) throws IOException {
            out.write(j.done());
            out.write('\n');
            eventCount++;
        }

        void flush() throws IOException {
            out.flush();
        }

        void close() throws IOException {
            out.flush();
            out.close();
        }

        void refreshPlayerCache() {
            playerXZ.clear();
            for (String name : players) {
                Player p = Bukkit.getPlayerExact(name);
                if (p != null && p.isOnline() && p.getWorld().equals(world)) {
                    Location l = p.getLocation();
                    playerXZ.add(new double[]{l.getX(), l.getZ()});
                }
            }
        }

        boolean inScope(World w, int x, int y, int z) {
            if (w == null || !w.equals(world)) return false;
            if (logRadius < 0) return true;
            if (x >= minX && x <= maxX && z >= minZ && z <= maxZ && y >= minY && y <= maxY) return true;
            for (double[] p : playerXZ) {
                if (Math.abs(x - p[0]) <= logRadius && Math.abs(z - p[1]) <= logRadius) return true;
            }
            return false;
        }

        boolean inScope(Block b) {
            return b != null && inScope(b.getWorld(), b.getX(), b.getY(), b.getZ());
        }

        boolean inScope(Location l) {
            return l != null && inScope(l.getWorld(), l.getBlockX(), l.getBlockY(), l.getBlockZ());
        }

        void markDirty(int x, int y, int z, String oldState) {
            long k = pack(x, y, z);
            dirty.add(k);
            if (oldState != null) lastKnown.putIfAbsent(k, oldState);
        }

        void markDirty(Block b, String oldState) {
            if (b != null) markDirty(b.getX(), b.getY(), b.getZ(), oldState);
        }

        /**
         * Re-queues every voxel touched so far for readback. Some changes of an already touched voxel fire no
         * event at all (fire ageing, flowing-liquid levels, crop/frosted-ice ages via random or scheduled ticks),
         * so without this the replay would miss them. Large sets are re-read every few ticks only.
         */
        void requeueTouched(boolean force) {
            int n = lastKnown.size();
            if (n == 0) return;
            if (force || n <= 20000 || ticks % 4 == 0) dirty.addAll(lastKnown.keySet());
        }

        /** Emits a block_state record for every touched position whose state differs from the last known one. */
        void processDirty() throws IOException {
            if (dirty.isEmpty()) return;
            List<Long> keys = new ArrayList<>(dirty);
            dirty.clear();
            for (long k : keys) {
                int x = unpackX(k), y = unpackY(k), z = unpackZ(k);
                if (!world.isChunkLoaded(Math.floorDiv(x, 16), Math.floorDiv(z, 16))) continue;
                BlockData d = world.getBlockAt(x, y, z).getBlockData();
                String now = d.getAsString();
                String prev = lastKnown.get(k);
                if (!now.equals(prev)) {
                    write(event("block_state").pos("pos", x, y, z)
                            .put("block", key(d.getMaterial())).put("state", now)
                            .put("prev_state", prev)
                            .put("source", "readback"));
                    lastKnown.put(k, now);
                }
            }
        }

        /** Writes the results of finished box diffs (main thread, seq order). */
        void drainBoxDiffs() throws IOException {
            DiffResult r;
            while ((r = diffResults.poll()) != null) applyBoxDiff(r);
        }

        /**
         * One block_state record (source "box_diff") per changed voxel that is not 'touched' yet; touched
         * voxels (in lastKnown) are followed by the per-tick readback already. The voxel becomes touched
         * and is queued for readback, so a later change (even in this tick) is logged as "readback".
         */
        void applyBoxDiff(DiffResult r) throws IOException {
            if (r.failed()) return;
            long t0 = System.nanoTime();
            diffsDone++;
            diffChunksUnloaded += r.chunksSkipped();
            diffChanged += r.changes().size();
            diffMainMsSum += r.mainMs();
            diffMainMsMax = Math.max(diffMainMsMax, r.mainMs());
            diffIoMsSum += r.ioMs();
            diffIoMsMax = Math.max(diffIoMsMax, r.ioMs());
            long now = world.getGameTime();
            for (DiffChange c : r.changes()) {
                if (lastKnown.containsKey(c.key())) {
                    diffTouchedSkipped++;
                    continue;
                }
                write(event("block_state").pos("pos", unpackX(c.key()), unpackY(c.key()), unpackZ(c.key()))
                        .put("block", c.block()).put("state", c.state())
                        .put("prev_state", c.prev())
                        .put("source", "box_diff")
                        .put("observed_game_time", r.observedGameTime())
                        .put("observed_server_tick", r.observedServerTick())
                        .put("baseline_game_time", r.baselineGameTime())
                        .put("detected_game_time", now));
                lastKnown.put(c.key(), c.state());
                dirty.add(c.key());
                diffRecords++;
            }
            double ms = (System.nanoTime() - t0) / 1e6;
            diffWriteMsSum += ms;
            diffWriteMsMax = Math.max(diffWriteMsMax, ms);
        }

        String diffStatsJson() {
            double n = Math.max(1, diffsDone);
            return new J().put("every", diffEvery)
                    .put("failed", diffFailed)
                    .put("diffs", diffsDone)
                    .put("skipped_busy", diffsSkipped)
                    .put("voxels", (long) (maxX - minX + 1) * (maxY - minY + 1) * (maxZ - minZ + 1))
                    .put("changed_voxels", diffChanged)
                    .put("records", diffRecords)
                    .put("touched_skipped", diffTouchedSkipped)
                    .put("main_snapshot_ms_mean", diffMainMsSum / n)
                    .put("main_snapshot_ms_max", diffMainMsMax)
                    .put("main_write_ms_mean", diffWriteMsSum / n)
                    .put("main_write_ms_max", diffWriteMsMax)
                    .put("io_diff_ms_mean", diffIoMsSum / n)
                    .put("io_diff_ms_max", diffIoMsMax)
                    .put("chunks_skipped_unloaded", diffChunksUnloaded)
                    .put("final_diff", diffFinal)
                    .put("end_wait_ms", diffEndWaitMs)
                    .put("end_snapshot_ms", endSnapshotMs)
                    .done();
        }

        void writePlayersTick(boolean force) throws IOException {
            StringBuilder arr = new StringBuilder("[");
            boolean first = true;
            for (String name : players) {
                Player p = Bukkit.getPlayerExact(name);
                if (p == null || !p.isOnline()) continue;
                Location l = p.getLocation();
                ItemStack held = p.getInventory().getItemInMainHand();
                J pj = new J()
                        .put("name", name)
                        .put("world", l.getWorld() == null ? null : l.getWorld().getName())
                        .vec("pos", l.getX(), l.getY(), l.getZ())
                        .put("yaw", l.getYaw())
                        .put("pitch", l.getPitch())
                        .put("sneaking", p.isSneaking())
                        .put("sprinting", p.isSprinting())
                        .put("on_ground", p.isOnGround())
                        .put("health", p.getHealth())
                        .put("fire_ticks", p.getFireTicks())
                        .put("held_slot", p.getInventory().getHeldItemSlot())
                        .put("held_item", held == null ? null : key(held.getType()))
                        .put("held_count", held == null ? 0 : held.getAmount())
                        .put("head_yaw", l.getYaw())
                        .put("body_yaw", p.getBodyYaw())
                        .put("food", p.getFoodLevel())
                        .put("saturation", p.getSaturation())
                        .put("dead", p.isDead())
                        .put("gamemode", p.getGameMode().name())
                        .put("flying", p.isFlying())
                        .put("blocking", p.isBlocking())
                        .put("hand_raised", p.isHandRaised());
                if (!first) arr.append(',');
                first = false;
                arr.append(pj.done());
            }
            arr.append(']');
            write(event("tick")
                    .put("full_time", world.getFullTime())
                    .put("time_of_day", world.getTime())
                    .raw("players", arr.toString())
                    .raw("cameras", camerasJson()));
        }

        /**
         * T12: server-side pose of every active camera player (camera.getLocation()), separate from
         * its controller. Sampled together with the rest of the tick record by a Bukkit scheduler
         * task, i.e. AFTER CameraManager's sync in ServerTickStartEvent of the same tick (normally
         * last_sync_tick == server_tick). Hence:
         * dyaw/dpitch/dpos_to_target = camera pose minus controller pose at the moment the record is
         * written (the residual follow error; 0 when the camera follows exactly);
         * dyaw/dpitch/dpos_since_sync = camera pose now vs. the pose commanded at the last sync
         * (non-zero only if the camera client moved on its own);
         * dyaw/dpitch/dpos_before_sync = camera pose just before this tick's sync minus the pose it
         * was synced to (the controller motion received since the previous sync = the one tick of
         * lag the camera would have without the sync).
         */
        String camerasJson() {
            EpisodeManager em = episodeManager();
            if (em == null || em.getActivePairs().isEmpty()) return "[]";
            CameraManager cm = em.getCameraManager();
            List<Map.Entry<Player, Player>> pairs = new ArrayList<>(em.getActivePairs().entrySet());
            pairs.sort((a, b) -> a.getValue().getName().compareTo(b.getValue().getName()));
            StringBuilder arr = new StringBuilder("[");
            boolean first = true;
            for (Map.Entry<Player, Player> pair : pairs) {
                Player ctrl = pair.getKey();
                Player cam = pair.getValue();
                if (cam == null || !cam.isOnline()) continue;
                Location c = cam.getLocation();
                J cj = new J()
                        .put("name", cam.getName())
                        .put("target", ctrl == null ? null : ctrl.getName())
                        .put("world", c.getWorld() == null ? null : c.getWorld().getName())
                        .vec("pos", c.getX(), c.getY(), c.getZ())
                        .put("yaw", c.getYaw())
                        .put("pitch", c.getPitch())
                        .put("flying", cam.isFlying())
                        .put("gamemode", cam.getGameMode().name());
                if (ctrl != null && ctrl.isOnline()) {
                    Location t = ctrl.getLocation();
                    boolean sameWorld = Objects.equals(c.getWorld(), t.getWorld());
                    cj.put("dyaw_to_target", wrapDeg(c.getYaw() - t.getYaw()))
                            .put("dpitch_to_target", c.getPitch() - t.getPitch())
                            .put("dpos_to_target", sameWorld ? c.distance(t) : Double.NaN);
                }
                cj.put("last_sync_tick", cm == null ? -1 : cm.getLastSyncTick(cam.getUniqueId()));
                // camera pose now vs. the pose it was teleported to at the last sync: non-zero only
                // if the camera client moved / turned on its own after the sync (should stay ~0)
                Location sp = cm == null ? null : cm.getLastSyncPose(cam.getUniqueId());
                if (sp != null) {
                    boolean sameWorld = Objects.equals(c.getWorld(), sp.getWorld());
                    cj.put("dyaw_since_sync", wrapDeg(c.getYaw() - sp.getYaw()))
                            .put("dpitch_since_sync", c.getPitch() - sp.getPitch())
                            .put("dpos_since_sync", sameWorld ? c.distance(sp) : Double.NaN);
                }
                double[] pre = cm == null ? null : cm.getLastPreSyncDelta(cam.getUniqueId());
                if (pre != null) {
                    cj.put("dyaw_before_sync", pre[0])
                            .put("dpitch_before_sync", pre[1])
                            .put("dpos_before_sync", pre[2]);
                }
                if (!first) arr.append(',');
                first = false;
                arr.append(cj.done());
            }
            return arr.append(']').toString();
        }

        /**
         * T3: one "entities" record with the full state of every entity within entitiesRadius
         * blocks (Euclidean, entity position) of the snapshot box centre. One getNearbyEntities
         * call on the bounding cube, then a distance filter. Camera / spectator helper players are
         * excluded; tracked players are always included.
         */
        void writeEntities() throws IOException {
            double r = entitiesRadius;
            double r2 = r * r;
            BoundingBox bb = new BoundingBox(entCx - r, entCy - r, entCz - r, entCx + r, entCy + r, entCz + r);
            StringBuilder arr = new StringBuilder(4096).append('[');
            Set<UUID> seen = new HashSet<>();
            int n = 0;
            for (Entity e : world.getNearbyEntities(bb)) {
                if (e instanceof Player p && isHelper(p)) continue;
                Location el = e.getLocation();
                double dx = el.getX() - entCx, dy = el.getY() - entCy, dz = el.getZ() - entCz;
                if (dx * dx + dy * dy + dz * dz > r2) continue;
                if (!seen.add(e.getUniqueId())) continue;
                if (n++ > 0) arr.append(',');
                arr.append(entityStateJson(e));
            }
            for (String name : players) {
                Player p = Bukkit.getPlayerExact(name);
                if (p == null || !p.isOnline() || !p.getWorld().equals(world) || seen.contains(p.getUniqueId())) continue;
                if (n++ > 0) arr.append(',');
                arr.append(entityStateJson(p));
            }
            arr.append(']');
            write(event("entities")
                    .vec("center", entCx, entCy, entCz)
                    .put("radius", entitiesRadius)
                    .put("shape", "sphere")
                    .put("count", n)
                    .raw("entities", arr.toString()));
        }

        /**
         * T15: use_item_start / use_item_stop from per-tick transitions of LivingEntity#hasActiveItem
         * (eating, drinking, shield raised, bow / crossbow / trident drawn, spyglass, ...).
         */
        void updateItemUse(boolean closing) throws IOException {
            int now = Bukkit.getCurrentTick();
            for (String name : players) {
                Player p = Bukkit.getPlayerExact(name);
                boolean active = !closing && p != null && p.isOnline() && p.hasActiveItem();
                ItemStack it = active ? p.getActiveItem() : null;
                String id = it == null ? null : key(it.getType());
                EquipmentSlot handSlot = active ? p.getActiveItemHand() : null;
                String hand = handSlot == null ? null : handSlot.name();
                UseState prev = using.get(name);
                boolean changed = prev != null
                        && (!active || !Objects.equals(prev.item, id) || !Objects.equals(prev.hand, hand));
                if (changed) {
                    write(event("use_item_stop")
                            .put("player", name)
                            .raw("item", prev.itemJson)
                            .put("hand", prev.hand)
                            .put("start_tick", prev.startTick)
                            .put("ticks_used", now - prev.startTick)
                            .put("used_time", prev.usedTime)
                            .put("reason", closing ? "episode_end" : (active ? "item_changed" : "stopped")));
                    using.remove(name);
                    prev = null;
                }
                if (active && prev == null) {
                    UseState st = new UseState(id, hand, itemJson(it), now);
                    using.put(name, st);
                    write(event("use_item_start")
                            .put("player", name)
                            .raw("item", st.itemJson)
                            .put("hand", hand)
                            .put("held_slot", p.getInventory().getHeldItemSlot()));
                } else if (active) {
                    prev.usedTime = p.getActiveItemUsedTime();
                }
            }
        }

        /**
         * T17: dig_progress records from queued BLOCK_BREAK_ANIMATION packets. The server sends the
         * same packet sequence to every viewer (each viewer's stream is FIFO, but streams of
         * different viewers may interleave), so a packet is logged only when it extends the
         * sequence already logged for (breaker, pos); viewers that lag behind are matched
         * against that sequence instead of being logged twice.
         */
        void drainDigPackets() throws IOException {
            DigPacket d;
            while ((d = digQueue.poll()) != null) {
                DigKey k = new DigKey(d.breakerId(), pack(d.x(), d.y(), d.z()));
                if (digStreams.size() > 8192 && !digStreams.containsKey(k)) digStreams.clear();
                if (!digStreams.computeIfAbsent(k, x -> new DigStream()).accept(d.receiverId(), d.stage())) continue;
                if (!inScope(world, d.x(), d.y(), d.z())) continue;
                Player breaker = null;
                for (Player p : Bukkit.getOnlinePlayers()) {
                    if (p.getEntityId() == d.breakerId()) {
                        breaker = p;
                        break;
                    }
                }
                if (breaker != null && isHelper(breaker)) continue;
                J j = event("dig_progress", d.epochMs())
                        .put("player", breaker == null ? null : breaker.getName())
                        .put("breaker_id", d.breakerId())
                        .pos("pos", d.x(), d.y(), d.z())
                        .put("stage", d.stage())
                        .put("cleared", d.stage() < 0 || d.stage() > 9)
                        .put("packet_server_tick", d.serverTick());
                if (world.isChunkLoaded(Math.floorDiv(d.x(), 16), Math.floorDiv(d.z(), 16))) {
                    j.put("block_state", world.getBlockAt(d.x(), d.y(), d.z()).getBlockData().getAsString());
                }
                write(j);
            }
        }
    }

    // BlockPos.asLong-compatible packing (x:26 bits, z:26 bits, y:12 bits)
    static long pack(int x, int y, int z) {
        return (((long) x & 0x3FFFFFFL) << 38) | (((long) z & 0x3FFFFFFL) << 12) | ((long) y & 0xFFFL);
    }

    static int unpackX(long k) { return (int) (k >> 38); }

    static int unpackY(long k) { return (int) (k << 52 >> 52); }

    static int unpackZ(long k) { return (int) (k << 26 >> 38); }

    // =====================================================================================
    // Event listeners (MONITOR: observe the final, non-cancelled outcome)
    // =====================================================================================

    private void log(J j) {
        try {
            session.write(j);
        } catch (IOException e) {
            plugin.getLogger().warning("[worldstate] write failed: " + e);
        }
    }

    private J blockEvent(String type, Block b, BlockData oldData, BlockData newData) {
        J j = session.event(type).pos("pos", b.getX(), b.getY(), b.getZ());
        if (oldData != null) j.put("old_type", key(oldData.getMaterial())).put("old_state", oldData.getAsString());
        else j.put("old_type", null).put("old_state", null);
        if (newData != null) j.put("new_type", key(newData.getMaterial())).put("new_state", newData.getAsString());
        else j.put("new_type", null).put("new_state", null);
        session.markDirty(b, oldData == null ? null : oldData.getAsString());
        return j;
    }

    private static void actor(J j, Entity e) {
        if (e == null) return;
        if (e instanceof Player p) j.put("player", p.getName());
        j.put("entity_type", typeKey(e)).put("entity_uuid", e.getUniqueId().toString());
    }

    private String statesArray(List<BlockState> states, boolean markOld) {
        StringBuilder sb = new StringBuilder("[");
        boolean first = true;
        for (BlockState st : states) {
            if (!first) sb.append(',');
            first = false;
            Block cur = st.getWorld().getBlockAt(st.getX(), st.getY(), st.getZ());
            String curState = cur.getBlockData().getAsString();
            sb.append(new J().pos("pos", st.getX(), st.getY(), st.getZ())
                    .put("old_state", curState)
                    .put("new_state", st.getBlockData().getAsString()).done());
            session.markDirty(st.getX(), st.getY(), st.getZ(), markOld ? curState : null);
        }
        return sb.append(']').toString();
    }

    private String blocksArray(List<Block> blocks) {
        StringBuilder sb = new StringBuilder("[");
        boolean first = true;
        for (Block b : blocks) {
            if (!first) sb.append(',');
            first = false;
            String st = b.getBlockData().getAsString();
            sb.append(new J().pos("pos", b.getX(), b.getY(), b.getZ())
                    .put("old_type", key(b.getType())).put("old_state", st).done());
            session.markDirty(b, st);
        }
        return sb.append(']').toString();
    }

    @EventHandler(priority = EventPriority.MONITOR, ignoreCancelled = true)
    public void onBlockPlace(BlockPlaceEvent e) {
        if (session == null || !session.inScope(e.getBlockPlaced())) return;
        String hand = key(e.getItemInHand() == null ? null : e.getItemInHand().getType());
        Block against = e.getBlockAgainst();
        if (e instanceof BlockMultiPlaceEvent m) {
            for (BlockState replaced : m.getReplacedBlockStates()) {
                Block b = replaced.getBlock();
                J j = blockEvent("block_place", b, replaced.getBlockData(), b.getBlockData())
                        .put("player", e.getPlayer().getName()).put("item", hand).put("multi", true);
                log(j);
            }
            return;
        }
        Block b = e.getBlockPlaced();
        J j = blockEvent("block_place", b, e.getBlockReplacedState().getBlockData(), b.getBlockData())
                .put("player", e.getPlayer().getName()).put("item", hand);
        if (against != null) j.pos("against", against.getX(), against.getY(), against.getZ());
        log(j);
    }

    @EventHandler(priority = EventPriority.MONITOR, ignoreCancelled = true)
    public void onBlockBreak(BlockBreakEvent e) {
        if (session == null || !session.inScope(e.getBlock())) return;
        drainDigQueue();
        ItemStack tool = e.getPlayer().getInventory().getItemInMainHand();
        J j = blockEvent("block_break", e.getBlock(), e.getBlock().getBlockData(), null)
                .put("player", e.getPlayer().getName())
                .put("tool", tool == null ? null : key(tool.getType()))
                .put("drop_items", e.isDropItems());
        log(j);
        if (session.tracked(e.getPlayer())) {
            Block b = e.getBlock();
            // keyed by (player, pos): Paper may finish a "delayed destroy" of the previous block
            // after the player already started digging the next one
            DigState d = session.digs.remove(digKey(e.getPlayer(), b));
            boolean same = d != null;
            log(session.event("dig_finish")
                    .put("player", e.getPlayer().getName())
                    .pos("pos", b.getX(), b.getY(), b.getZ())
                    .put("block_state", b.getBlockData().getAsString())
                    .put("tool", tool == null ? null : key(tool.getType()))
                    .put("start_tick", same ? d.startTick : -1)
                    .put("dig_ticks", same ? Bukkit.getCurrentTick() - d.startTick : -1)
                    .put("insta_break", same && d.instaBreak));
        }
    }

    // ---- T17 digging ---------------------------------------------------------------------

    /**
     * Writes already-captured dig_progress packets before a dig_start / dig_abort / block_break
     * record so that seq order matches capture order as far as possible (a packet still in flight
     * on the Netty thread is written on the next tick; its epoch_ms is the capture time).
     */
    private static String digKey(Player p, Block b) {
        return p.getName() + "@" + pack(b.getX(), b.getY(), b.getZ());
    }

    private void drainDigQueue() {
        try {
            session.drainDigPackets();
        } catch (IOException e) {
            plugin.getLogger().warning("[worldstate] write failed: " + e);
        }
    }

    @EventHandler(priority = EventPriority.MONITOR, ignoreCancelled = true)
    public void onDigStart(BlockDamageEvent e) {
        if (session == null || !session.tracked(e.getPlayer())) return;
        drainDigQueue();
        Block b = e.getBlock();
        if (session.digs.size() > 4096) session.digs.clear();
        session.digs.put(digKey(e.getPlayer(), b),
                new DigState(b.getX(), b.getY(), b.getZ(), Bukkit.getCurrentTick(), e.getInstaBreak()));
        log(session.event("dig_start")
                .put("player", e.getPlayer().getName())
                .pos("pos", b.getX(), b.getY(), b.getZ())
                .put("block_state", b.getBlockData().getAsString())
                .put("face", e.getBlockFace() == null ? null : e.getBlockFace().name())
                .raw("item", itemJson(e.getItemInHand()))
                .put("insta_break", e.getInstaBreak()));
    }

    @EventHandler(priority = EventPriority.MONITOR)
    public void onDigAbort(BlockDamageAbortEvent e) {
        if (session == null || !session.tracked(e.getPlayer())) return;
        drainDigQueue();
        Block b = e.getBlock();
        DigState d = session.digs.remove(digKey(e.getPlayer(), b));
        boolean same = d != null;
        log(session.event("dig_abort")
                .put("player", e.getPlayer().getName())
                .pos("pos", b.getX(), b.getY(), b.getZ())
                .put("block_state", b.getBlockData().getAsString())
                .raw("item", itemJson(e.getItemInHand()))
                .put("start_tick", same ? d.startTick : -1)
                .put("dig_ticks", same ? Bukkit.getCurrentTick() - d.startTick : -1));
    }

    // ---- T15 player actions (tracked players only) ---------------------------------------

    @EventHandler(priority = EventPriority.MONITOR, ignoreCancelled = true)
    public void onArmSwing(PlayerAnimationEvent e) {
        if (session == null || !session.tracked(e.getPlayer())) return;
        String hand = e instanceof PlayerArmSwingEvent a && a.getHand() != null ? a.getHand().name() : null;
        log(session.event("arm_swing")
                .put("player", e.getPlayer().getName())
                .put("animation", e.getAnimationType().name())
                .put("hand", hand));
    }

    @EventHandler(priority = EventPriority.MONITOR, ignoreCancelled = true)
    public void onHeldItemChange(PlayerItemHeldEvent e) {
        if (session == null || !session.tracked(e.getPlayer())) return;
        org.bukkit.inventory.PlayerInventory inv = e.getPlayer().getInventory();
        log(session.event("held_item_change")
                .put("player", e.getPlayer().getName())
                .put("previous_slot", e.getPreviousSlot())
                .put("new_slot", e.getNewSlot())
                .raw("previous_item", itemJson(inv.getItem(e.getPreviousSlot())))
                .raw("new_item", itemJson(inv.getItem(e.getNewSlot()))));
    }

    @EventHandler(priority = EventPriority.MONITOR, ignoreCancelled = true)
    public void onItemConsume(PlayerItemConsumeEvent e) {
        if (session == null || !session.tracked(e.getPlayer())) return;
        log(session.event("item_consume")
                .put("player", e.getPlayer().getName())
                .raw("item", itemJson(e.getItem()))
                .put("hand", e.getHand() == null ? null : e.getHand().name()));
    }

    @EventHandler(priority = EventPriority.MONITOR, ignoreCancelled = true)
    public void onShootBow(EntityShootBowEvent e) {
        if (session == null || !(e.getEntity() instanceof Player p) || !session.tracked(p)) return;
        Entity proj = e.getProjectile();
        log(session.event("bow_shoot")
                .put("player", p.getName())
                .raw("bow", itemJson(e.getBow()))
                .raw("consumable", itemJson(e.getConsumable()))
                .put("hand", e.getHand() == null ? null : e.getHand().name())
                .put("force", e.getForce())
                .put("projectile", proj == null ? null : proj.getUniqueId().toString())
                .put("projectile_type", proj == null ? null : typeKey(proj)));
    }

    // ---- T3 / T15 damage and knockback ---------------------------------------------------

    /** Projectiles are attributed to their shooter; the projectile itself is reported separately. */
    private static Entity resolveAttacker(Entity damager) {
        if (damager instanceof Projectile pr) {
            return pr.getShooter() instanceof Entity s ? s : null;
        }
        return damager;
    }

    @EventHandler(priority = EventPriority.MONITOR, ignoreCancelled = true)
    public void onEntityDamage(EntityDamageEvent e) {
        Entity victim = e.getEntity();
        if (session == null || !session.inScope(victim.getLocation())) return;
        if (victim instanceof Player p && isHelper(p)) return;
        Entity damager = e instanceof EntityDamageByEntityEvent be ? be.getDamager() : null;
        Entity attacker = resolveAttacker(damager);
        Entity projectile = damager instanceof Projectile ? damager : null;
        double before = victim instanceof LivingEntity le ? le.getHealth() : Double.NaN;
        double fin = e.getFinalDamage();
        String damageType = null;
        Entity causing = null;
        try {
            damageType = e.getDamageSource().getDamageType().getKey().toString();
            causing = e.getDamageSource().getCausingEntity();
        } catch (Throwable ignored) {
        }
        J j = session.event("entity_damage")
                .raw("victim", refJson(victim))
                .put("cause", e.getCause().name())
                .put("damage_type", damageType)
                .put("damage", e.getDamage())
                .put("final_damage", fin)
                .put("health_before", before)
                .put("health_after", Double.isNaN(before) ? Double.NaN : Math.max(0.0, before - fin))
                .raw("attacker", refJson(attacker))
                .raw("projectile", projectile == null ? "null"
                        : new J().put("uuid", projectile.getUniqueId().toString()).put("type", typeKey(projectile)).done())
                .raw("causing_entity", refJson(causing))
                .put("victim_blocking", victim instanceof HumanEntity h && h.isBlocking());
        if (e instanceof EntityDamageByBlockEvent bb && bb.getDamager() != null) {
            Block db = bb.getDamager();
            j.pos("damager_block", db.getX(), db.getY(), db.getZ());
        }
        log(j);
    }

    @EventHandler(priority = EventPriority.MONITOR, ignoreCancelled = true)
    public void onKnockback(EntityKnockbackEvent e) {
        Entity ent = e.getEntity();
        if (session == null || !session.inScope(ent.getLocation())) return;
        if (ent instanceof Player p && isHelper(p)) return;
        Entity attacker = e instanceof EntityPushedByEntityAttackEvent pe ? pe.getPushedBy() : null;
        Vector v = e.getKnockback();
        J j = session.event("entity_knockback")
                .raw("entity", refJson(ent))
                .put("cause", e.getCause().name())
                .raw("attacker", refJson(attacker))
                .vec("vector", v.getX(), v.getY(), v.getZ());
        if (e instanceof EntityKnockbackByEntityEvent ke) j.put("strength", ke.getKnockbackStrength());
        log(j);
    }

    @EventHandler(priority = EventPriority.MONITOR, ignoreCancelled = true)
    public void onBlockBurn(BlockBurnEvent e) {
        if (session == null || !session.inScope(e.getBlock())) return;
        J j = blockEvent("block_burn", e.getBlock(), e.getBlock().getBlockData(), null);
        Block src = e.getIgnitingBlock();
        if (src != null) j.pos("source", src.getX(), src.getY(), src.getZ());
        log(j);
    }

    @EventHandler(priority = EventPriority.MONITOR, ignoreCancelled = true)
    public void onBlockIgnite(BlockIgniteEvent e) {
        if (session == null || !session.inScope(e.getBlock())) return;
        J j = blockEvent("block_ignite", e.getBlock(), e.getBlock().getBlockData(), null)
                .put("cause", e.getCause().name());
        actor(j, e.getIgnitingEntity());
        Block src = e.getIgnitingBlock();
        if (src != null) j.pos("source", src.getX(), src.getY(), src.getZ());
        log(j);
    }

    @EventHandler(priority = EventPriority.MONITOR, ignoreCancelled = true)
    public void onBlockFade(BlockFadeEvent e) {
        if (session == null || !session.inScope(e.getBlock())) return;
        log(blockEvent("block_fade", e.getBlock(), e.getBlock().getBlockData(), e.getNewState().getBlockData()));
    }

    @EventHandler(priority = EventPriority.MONITOR, ignoreCancelled = true)
    public void onBlockForm(BlockFormEvent e) {
        if (e instanceof BlockSpreadEvent) return; // handled by onBlockSpread
        if (session == null || !session.inScope(e.getBlock())) return;
        J j = blockEvent("block_form", e.getBlock(), e.getBlock().getBlockData(), e.getNewState().getBlockData());
        if (e instanceof EntityBlockFormEvent ef) actor(j, ef.getEntity());
        log(j);
    }

    @EventHandler(priority = EventPriority.MONITOR, ignoreCancelled = true)
    public void onBlockSpread(BlockSpreadEvent e) {
        if (session == null || !session.inScope(e.getBlock())) return;
        Block src = e.getSource();
        J j = blockEvent("block_spread", e.getBlock(), e.getBlock().getBlockData(), e.getNewState().getBlockData());
        if (src != null) j.pos("source", src.getX(), src.getY(), src.getZ()).put("source_state", src.getBlockData().getAsString());
        log(j);
    }

    @EventHandler(priority = EventPriority.MONITOR, ignoreCancelled = true)
    public void onBlockGrow(BlockGrowEvent e) {
        if (e instanceof BlockFormEvent) return; // forms/spreads have dedicated handlers
        if (session == null || !session.inScope(e.getBlock())) return;
        log(blockEvent("block_grow", e.getBlock(), e.getBlock().getBlockData(), e.getNewState().getBlockData()));
    }

    @EventHandler(priority = EventPriority.MONITOR, ignoreCancelled = true)
    public void onBlockFromTo(BlockFromToEvent e) {
        Block to = e.getToBlock();
        if (session == null || !session.inScope(to)) return;
        Block from = e.getBlock();
        J j = blockEvent("liquid_flow", to, to.getBlockData(), null)
                .pos("from", from.getX(), from.getY(), from.getZ())
                .put("from_state", from.getBlockData().getAsString())
                .put("face", e.getFace().name());
        log(j);
    }

    @EventHandler(priority = EventPriority.MONITOR, ignoreCancelled = true)
    public void onFluidLevelChange(FluidLevelChangeEvent e) {
        if (session == null || !session.inScope(e.getBlock())) return;
        log(blockEvent("fluid_level_change", e.getBlock(), e.getBlock().getBlockData(), e.getNewData()));
    }

    @EventHandler(priority = EventPriority.MONITOR, ignoreCancelled = true)
    public void onBlockExplode(BlockExplodeEvent e) {
        if (session == null || !session.inScope(e.getBlock())) return;
        Block b = e.getBlock();
        J j = session.event("block_explode")
                .pos("pos", b.getX(), b.getY(), b.getZ())
                .put("yield", e.getYield())
                .raw("blocks", blocksArray(e.blockList()));
        session.markDirty(b, null);
        log(j);
    }

    @EventHandler(priority = EventPriority.MONITOR, ignoreCancelled = true)
    public void onEntityExplode(EntityExplodeEvent e) {
        Location l = e.getLocation();
        if (session == null || !session.inScope(l)) return;
        J j = session.event("entity_explode")
                .vec("center", l.getX(), l.getY(), l.getZ())
                .put("yield", e.getYield())
                .raw("blocks", blocksArray(e.blockList()));
        actor(j, e.getEntity());
        log(j);
    }

    @EventHandler(priority = EventPriority.MONITOR, ignoreCancelled = true)
    public void onTntPrime(TNTPrimeEvent e) {
        if (session == null || !session.inScope(e.getBlock())) return;
        J j = blockEvent("tnt_prime", e.getBlock(), e.getBlock().getBlockData(), null)
                .put("cause", e.getCause().name());
        actor(j, e.getPrimingEntity());
        Block src = e.getPrimingBlock();
        if (src != null) j.pos("source", src.getX(), src.getY(), src.getZ());
        log(j);
    }

    @EventHandler(priority = EventPriority.MONITOR, ignoreCancelled = true)
    public void onLeavesDecay(LeavesDecayEvent e) {
        if (session == null || !session.inScope(e.getBlock())) return;
        log(blockEvent("leaves_decay", e.getBlock(), e.getBlock().getBlockData(), null));
    }

    @EventHandler(priority = EventPriority.MONITOR, ignoreCancelled = true)
    public void onPistonExtend(BlockPistonExtendEvent e) {
        if (session == null || !session.inScope(e.getBlock())) return;
        logPiston("piston_extend", e.getBlock(), e.getDirection(), e.getBlocks());
    }

    @EventHandler(priority = EventPriority.MONITOR, ignoreCancelled = true)
    public void onPistonRetract(BlockPistonRetractEvent e) {
        if (session == null || !session.inScope(e.getBlock())) return;
        logPiston("piston_retract", e.getBlock(), e.getDirection(), e.getBlocks());
    }

    private void logPiston(String type, Block piston, BlockFace dir, List<Block> moved) {
        for (Block b : moved) {
            Block dest = b.getRelative(dir);
            session.markDirty(dest, dest.getBlockData().getAsString());
        }
        Block head = piston.getRelative(dir);
        session.markDirty(head, head.getBlockData().getAsString());
        J j = session.event(type)
                .pos("pos", piston.getX(), piston.getY(), piston.getZ())
                .put("direction", dir.name())
                .raw("blocks", blocksArray(moved));
        session.markDirty(piston, piston.getBlockData().getAsString());
        log(j);
    }

    @EventHandler(priority = EventPriority.MONITOR, ignoreCancelled = true)
    public void onEntityChangeBlock(EntityChangeBlockEvent e) {
        if (session == null || !session.inScope(e.getBlock())) return;
        J j = blockEvent("entity_change_block", e.getBlock(), e.getBlock().getBlockData(), e.getBlockData());
        actor(j, e.getEntity());
        log(j);
    }

    @EventHandler(priority = EventPriority.MONITOR, ignoreCancelled = true)
    public void onStructureGrow(StructureGrowEvent e) {
        if (session == null || !session.inScope(e.getLocation())) return;
        Location l = e.getLocation();
        J j = session.event("structure_grow")
                .pos("pos", l.getBlockX(), l.getBlockY(), l.getBlockZ())
                .put("species", e.getSpecies() == null ? null : e.getSpecies().name())
                .put("from_bonemeal", e.isFromBonemeal())
                .put("player", e.getPlayer() == null ? null : e.getPlayer().getName())
                .raw("blocks", statesArray(e.getBlocks(), true));
        log(j);
    }

    @EventHandler(priority = EventPriority.MONITOR, ignoreCancelled = true)
    public void onFertilize(BlockFertilizeEvent e) {
        if (session == null || !session.inScope(e.getBlock())) return;
        Block b = e.getBlock();
        J j = session.event("block_fertilize")
                .pos("pos", b.getX(), b.getY(), b.getZ())
                .put("player", e.getPlayer() == null ? null : e.getPlayer().getName())
                .raw("blocks", statesArray(e.getBlocks(), true));
        log(j);
    }

    @EventHandler(priority = EventPriority.MONITOR, ignoreCancelled = true)
    public void onSpongeAbsorb(SpongeAbsorbEvent e) {
        if (session == null || !session.inScope(e.getBlock())) return;
        Block b = e.getBlock();
        J j = session.event("sponge_absorb")
                .pos("pos", b.getX(), b.getY(), b.getZ())
                .raw("blocks", statesArray(e.getBlocks(), true));
        session.markDirty(b, b.getBlockData().getAsString());
        log(j);
    }

    @EventHandler(priority = EventPriority.MONITOR, ignoreCancelled = true)
    public void onBucketEmpty(PlayerBucketEmptyEvent e) {
        Block clicked = e.getBlockClicked();
        if (session == null || !session.inScope(clicked)) return;
        Block target = clicked.getRelative(e.getBlockFace());
        session.markDirty(clicked, clicked.getBlockData().getAsString());
        J j = blockEvent("bucket_empty", target, target.getBlockData(), null)
                .put("player", e.getPlayer().getName())
                .put("bucket", key(e.getBucket()))
                .pos("clicked", clicked.getX(), clicked.getY(), clicked.getZ())
                .put("clicked_state", clicked.getBlockData().getAsString())
                .put("face", e.getBlockFace().name());
        log(j);
    }

    @EventHandler(priority = EventPriority.MONITOR, ignoreCancelled = true)
    public void onBucketFill(PlayerBucketFillEvent e) {
        Block clicked = e.getBlockClicked();
        if (session == null || !session.inScope(clicked)) return;
        Block target = clicked.getRelative(e.getBlockFace());
        session.markDirty(target, target.getBlockData().getAsString());
        J j = blockEvent("bucket_fill", clicked, clicked.getBlockData(), null)
                .put("player", e.getPlayer().getName())
                .put("bucket", key(e.getBucket()))
                .put("face", e.getBlockFace().name());
        log(j);
    }

    @EventHandler(priority = EventPriority.MONITOR)
    public void onInteract(PlayerInteractEvent e) {
        Block b = e.getClickedBlock();
        if (session == null || b == null || !session.inScope(b)) return;
        if (!session.players.contains(e.getPlayer().getName())) return; // ignore camera/spectator mirrors
        BlockFace face = e.getBlockFace();
        session.markDirty(b, b.getBlockData().getAsString());
        for (BlockFace f : new BlockFace[]{face, BlockFace.UP, BlockFace.DOWN}) {
            if (f == null) continue;
            Block r = b.getRelative(f);
            session.markDirty(r, r.getBlockData().getAsString());
        }
        ItemStack it = e.getItem();
        J j = session.event("player_interact")
                .put("player", e.getPlayer().getName())
                .put("action", e.getAction().name())
                .pos("pos", b.getX(), b.getY(), b.getZ())
                .put("state", b.getBlockData().getAsString())
                .put("face", face == null ? null : face.name())
                .put("item", it == null ? null : key(it.getType()))
                .put("cancelled", e.isCancelled());
        log(j);
    }

    // ---- entities ------------------------------------------------------------------------

    private void logSpawn(Entity ent, String reason) {
        Location l = ent.getLocation();
        if (session == null || !session.inScope(l)) return;
        session.spawned.add(ent.getUniqueId());
        if (ent instanceof TNTPrimed || ent instanceof FallingBlock) {
            // the source block disappears when these entities appear
            Block src = l.getBlock();
            session.markDirty(src, null);
        }
        J j = session.event("entity_spawn").raw("entity", entityJson(ent)).put("reason", reason);
        log(j);
    }

    @EventHandler(priority = EventPriority.MONITOR, ignoreCancelled = true)
    public void onEntitySpawn(EntitySpawnEvent e) {
        if (e instanceof CreatureSpawnEvent || e instanceof ItemSpawnEvent) return;
        logSpawn(e.getEntity(), null);
    }

    @EventHandler(priority = EventPriority.MONITOR, ignoreCancelled = true)
    public void onCreatureSpawn(CreatureSpawnEvent e) {
        logSpawn(e.getEntity(), e.getSpawnReason().name());
    }

    @EventHandler(priority = EventPriority.MONITOR, ignoreCancelled = true)
    public void onItemSpawn(ItemSpawnEvent e) {
        logSpawn(e.getEntity(), "ITEM");
    }

    private void logDeath(LivingEntity ent) {
        if (session == null || !session.inScope(ent.getLocation())) return;
        if (ent instanceof Player p && isHelper(p)) return;
        EntityDamageEvent last = ent.getLastDamageCause();
        Entity killerEntity = last instanceof EntityDamageByEntityEvent be ? resolveAttacker(be.getDamager()) : null;
        if (killerEntity == null) killerEntity = ent.getKiller();
        J j = session.event("entity_death")
                .raw("entity", entityJson(ent))
                .raw("victim", refJson(ent))
                .put("victim_name", entityName(ent))
                .put("cause", last == null ? null : last.getCause().name())
                .put("killer", ent.getKiller() == null ? null : ent.getKiller().getName())
                .raw("killer_entity", refJson(killerEntity))
                .put("killer_name", killerEntity == null ? null : entityName(killerEntity));
        log(j);
    }

    @EventHandler(priority = EventPriority.MONITOR)
    public void onEntityDeath(EntityDeathEvent e) {
        if (e instanceof PlayerDeathEvent) return;
        logDeath(e.getEntity());
    }

    @EventHandler(priority = EventPriority.MONITOR)
    public void onPlayerDeath(PlayerDeathEvent e) {
        logDeath(e.getEntity());
    }

    @EventHandler(priority = EventPriority.MONITOR)
    public void onEntityRemove(EntityRemoveFromWorldEvent e) {
        Entity ent = e.getEntity();
        if (session == null || ent instanceof Player || !session.inScope(ent.getLocation())) return;
        log(session.event("entity_remove").raw("entity", entityJson(ent)));
    }

    // ---- weather -------------------------------------------------------------------------

    @EventHandler(priority = EventPriority.MONITOR, ignoreCancelled = true)
    public void onWeather(WeatherChangeEvent e) {
        if (session == null || !e.getWorld().equals(session.world)) return;
        log(session.event("weather").put("storm", e.toWeatherState()));
    }

    @EventHandler(priority = EventPriority.MONITOR, ignoreCancelled = true)
    public void onThunder(ThunderChangeEvent e) {
        if (session == null || !e.getWorld().equals(session.world)) return;
        log(session.event("weather").put("thundering", e.toThunderState()));
    }

    // =====================================================================================
    // Minimal JSON builder (no external dependency)
    // =====================================================================================

    static final class J {
        private final StringBuilder sb = new StringBuilder(192);
        private boolean first = true;

        J() {
            sb.append('{');
        }

        private J k(String key) {
            if (!first) sb.append(',');
            first = false;
            str(sb, key);
            sb.append(':');
            return this;
        }

        J put(String key, String v) {
            k(key);
            if (v == null) sb.append("null");
            else str(sb, v);
            return this;
        }

        J put(String key, long v) {
            k(key);
            sb.append(v);
            return this;
        }

        J put(String key, double v) {
            k(key);
            num(sb, v);
            return this;
        }

        J put(String key, boolean v) {
            k(key);
            sb.append(v);
            return this;
        }

        J raw(String key, String json) {
            k(key);
            sb.append(json == null ? "null" : json);
            return this;
        }

        J pos(String key, int x, int y, int z) {
            k(key);
            sb.append('[').append(x).append(',').append(y).append(',').append(z).append(']');
            return this;
        }

        J vec(String key, double x, double y, double z) {
            k(key);
            sb.append('[');
            num(sb, x);
            sb.append(',');
            num(sb, y);
            sb.append(',');
            num(sb, z);
            sb.append(']');
            return this;
        }

        String done() {
            return sb.toString() + "}";
        }

        static void num(StringBuilder sb, double d) {
            if (Double.isNaN(d) || Double.isInfinite(d)) {
                sb.append("null");
                return;
            }
            double r = Math.round(d * 10000.0) / 10000.0;
            if (r == Math.rint(r) && Math.abs(r) < 1e15) sb.append((long) r);
            else sb.append(r);
        }

        static void str(StringBuilder sb, String s) {
            sb.append('"');
            for (int i = 0; i < s.length(); i++) {
                char c = s.charAt(i);
                switch (c) {
                    case '"' -> sb.append("\\\"");
                    case '\\' -> sb.append("\\\\");
                    case '\n' -> sb.append("\\n");
                    case '\r' -> sb.append("\\r");
                    case '\t' -> sb.append("\\t");
                    default -> {
                        if (c < 0x20) sb.append(String.format("\\u%04x", (int) c));
                        else sb.append(c);
                    }
                }
            }
            sb.append('"');
        }
    }
}
