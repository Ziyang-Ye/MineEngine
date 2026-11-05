# Schema additions: MirrorBot plugin (`WorldStateLogger`, `CameraManager`)

Extends `OUTPUT_SCHEMA.md` section 5 (world events). All records keep
the common fields (`type`, `seq`, `game_time`, `server_tick`, `epoch_ms`).
Angles are Minecraft degrees as reported by the server (`Location#getYaw/getPitch`,
yaw 0 = south (+z), 90 = west, not normalised: values outside [-180, 180) occur);
`*_item` objects are `{"id": "minecraft:...", "count": n}` or `null` (empty hand / air).
Entity references (`victim`, `attacker`, ...) are `{"uuid", "type", "name"}` or `null`.

## 5.1 Session records (changes)

| type | new fields |
|---|---|
| `episode_begin` | `entities_every` (ticks between `entities` records, 0 = off, default 1), `entities_radius` (default 64), `entities_center` [x,y,z] (centre of the snapshot box), `diff_every` (ticks between full-box diffs, 0 = off, default 20; see 5.2), `announce_advancements` (always `false`, see below), `announce_advancements_changed` (worlds where the rule was still `true` and was switched off by this `begin`; normally `[]` because the plugin already sets it on enable) |
| `episode_end` | `box_diff` statistics of the session (see 5.2) |
| `tick` | per player in `players`, in addition to the existing fields: `head_yaw` (= `yaw`, the look direction), `body_yaw` (`LivingEntity#getBodyYaw`, not normalised), `food`, `saturation`, `dead`, `gamemode` (`SURVIVAL`, ...), `flying`, `blocking` (shield raised and active), `hand_raised` (any item in use). New array `cameras` (see below; `[]` when no `/episode` pairing is active) |

`tick.cameras[]`: one entry per active camera player (sorted by name):

| field | meaning |
|---|---|
| `name`, `target` | camera player (`CameraAlpha`) and the controller it follows (`Alpha`) |
| `world`, `pos` [x,y,z], `yaw`, `pitch` | the camera player's OWN server-side pose (`camera.getLocation()`), independent of the bot's |
| `flying`, `gamemode` | camera player state (cameras fly, so the vanilla client would apply the x1.1 flying FOV unless `fovEffectScale` = 0) |
| `dyaw_to_target`, `dpitch_to_target`, `dpos_to_target` | camera pose minus the controller's pose at the moment the record is written (yaw wrapped to [-180, 180), pitch difference, distance in blocks); the residual follow error, 0 when the camera follows exactly |
| `last_sync_tick` | `server_tick` at whose START the camera was last teleported onto its controller (-1 = not yet) |
| `dyaw_since_sync`, `dpitch_since_sync`, `dpos_since_sync` | camera pose now minus the pose it was teleported to at `last_sync_tick` |
| `dyaw_before_sync`, `dpitch_before_sync`, `dpos_before_sync` | camera pose just BEFORE the sync at `last_sync_tick` minus the controller pose it was synced to (the controller motion received since the previous sync) |

Sampling / sync timing (T12). `CameraManager` teleports every camera (and its
`Spectator<Bot>`) onto its controller at the START of every server tick
(`ServerTickStartEvent`, before the scheduler tasks, the world tick and the
per-player connection tick), with no distance / angle thresholds. Incoming
movement packets are processed between ticks, not during them, so this is the
newest controller pose the server has (a sync at the end of the tick would use
the same pose, only later). The `tick` record is written afterwards in the same
tick (Bukkit scheduler task), i.e. AFTER that tick's sync. Hence normally
`server_tick == last_sync_tick`, and:

- `*_to_target` and `*_since_sync` are the server-side follow error; both are 0
  when the camera follows exactly, and `cameras[].yaw/pitch` equals the
  target's `players[].yaw/pitch` in the same record. Use either for the
  server-side "camera vs bot" accuracy check.
- `*_before_sync` is how far the controller moved / turned since the previous
  sync (one tick of input, e.g. 9 deg for a bot turning 9 deg per tick; large
  after a teleport or respawn). It shows the lag the camera would have without
  the per-tick sync; it is not a sync error.

The rendered camera pose (ground truth for the video) is in the camera mod's
`camera_<bot>_frames.jsonl`.

## 5.2 Block changes without an event: `block_state` sources (exact replay)

`worldstate begin ... diff_every=N` (default 20, 0 = off). Some blocks change without any
Bukkit event (random ticks of kelp / vines / sugar cane / cocoa / mangrove propagules, bubble
columns forming above soul sand / magma, leaf `distance` updates after a neighbour change,
snow / ice by weather, liquid levels in untouched voxels, ...). The per-tick readback only
covers *touched* voxels (any voxel named by an event, see `block_state` in section 5), so
every N ticks the plugin also diffs the WHOLE snapshot box: the main thread takes
`ChunkSnapshot`s of the box chunks, the IO thread compares them voxel by voxel with the
previous full-box state (seeded from the start snapshot) and hands the changed voxels back;
the main thread writes them (in `seq` order) at the start of a later tick. One final diff runs
on exactly the chunks of the end snapshot (same tick), so **start snapshot + all `block_state`
records in `seq` order == end snapshot** also in normal worlds (`replay_mismatch` 0 in
`verify_world_events.py`).

Periodic diffs only look at box chunks that are currently loaded (`World.isChunkLoaded`); an
unloaded chunk is not loaded / generated for the diff, its voxels keep their baseline (an
unloaded chunk does not tick, so nothing in it changes). The final diff uses the end snapshot,
which force-loads every box chunk, so it always covers the whole box.

A diff only commits its result to the baseline after the whole box was compared. If a periodic
diff throws, the baseline stays consistent, `failed` becomes `true`, no further periodic diffs
run in that episode, and the final diff still runs against the intact baseline (it reports the
changes the failed diff would have reported, with the older `baseline_game_time`), so replay
stays exact. (Test-only begin argument `diff_fault=N`: the N-th diff of the session throws half
way through the box; recorded as `episode_begin.diff_fault`, 0 = off.)

`block_state` gets a new field `source`:

| `source` | meaning / extra fields |
|---|---|
| `readback` | per-tick re-read of a touched voxel (all earlier `block_state` records); `game_time` = when it was read |
| `box_diff` | an untouched voxel found changed by the full-box diff. `prev_state` = its state in the previous diff baseline, `state` = the state in the diff snapshot. `observed_game_time` / `observed_server_tick` = tick of the snapshot that saw the change, `baseline_game_time` = tick of the previous full-box state, so the change happened in (`baseline_game_time`, `observed_game_time`]; `game_time` = when the record was written (normally 1-2 ticks after `observed_game_time`); `detected_game_time` is an alias of `game_time` (always equal, kept for the field name of the original spec; use `observed_game_time` for the tick of the snapshot that saw the change). From then on the voxel is touched: later changes come as `readback` records (possibly in the same tick) |

Voxels that were already touched when the diff result arrives are skipped (the readback has
them). Consumers that replay `block_state` should accept any `source`. Positions outside the
box (inside `log_radius`) are covered by the readback only.

`episode_end.box_diff` (also in the `worldstate end` reply): `every`, `failed`, `diffs`
(diffs written), `skipped_busy` (periods skipped because two diffs were still queued on the IO
thread), `voxels` (box size), `changed_voxels` (voxels that differed from the baseline),
`records` (`box_diff` records written), `touched_skipped`, and the cost:
`main_snapshot_ms_mean/max` (main thread, `ChunkSnapshot`s of the loaded box chunks for a
periodic diff; the final diff reuses the end snapshot and counts 0 here),
`main_write_ms_mean/max` (main thread, writing the records), `io_diff_ms_mean/max` (IO thread
compare), `chunks_skipped_unloaded` (sum over periodic diffs of box chunks skipped because
they were not loaded), `final_diff` (`ok` | `failed` | `not_run` when `diff_every=0`),
`end_wait_ms` (main thread blocked in `worldstate end` waiting for the IO thread: first for
diffs still queued, then for the final diff; on the default box ~10-50 ms, ~40-170 ms with
JIT warm-up / a 129 x 69 x 129 box; the wait times out after 60 s per step, then
`final_diff` = `failed`), `end_snapshot_ms` (main thread, `ChunkSnapshot`s for the end snapshot
incl. force-loading unloaded box chunks; ~1 ms when loaded, ~220-280 ms for 81 unloaded
chunks). Measured on the default box (radius 32, 65 x 67..89 x 65 = 283k..376k voxels,
25 chunks, Paper 1.21, i9-13980HX): main thread 0.9-1.7 ms mean per diff (max 2-9 ms, one
42 ms outlier), IO thread 13-31 ms mean (max ~110 ms, first diff incl. JIT warm-up), i.e.
< 0.1 ms per tick on the main thread at the default `diff_every=20`.
Larger box (radius 64, 129 x 69 x 129 = 1.13M voxels, 81 chunks): all chunks loaded: main
thread 1.8 ms mean (max 12 ms), IO 47 ms mean (max 76 ms); box away from every player (its
chunks unload a few seconds after `begin`, 4779-4860 chunk-periods skipped in 60 s): main
0.06-0.15 ms mean (max 1.5-6.6 ms), IO 1.8-4.9 ms mean (before the loaded-chunk check this case
loaded the chunks on every diff: main max ~345 ms).

### Advancement announcements

The plugin sets gamerule `announceAdvancements` to `false` in every world on enable, on every
`WorldLoadEvent` and again on every `worldstate begin` (the vanilla chat line
"X has made the advancement [...]" would otherwise appear in the camera frames). Recorded in
`episode_begin` (`announce_advancements`, `announce_advancements_changed`, and
`world.gamerules.announceAdvancements` = `"false"`).

## 5.4 Entity records (additions)

### `entities` (T3)

Written every `entities_every` ticks (default every tick, same tick as the
`tick` record), plus once right after `episode_begin` and before `episode_end`.

| field | meaning |
|---|---|
| `center` [x,y,z], `radius`, `shape` | query volume: sphere (`shape: "sphere"`) of radius `radius` blocks around the snapshot box centre (entity position, Euclidean distance `<= radius`) |
| `count` | number of entries |
| `entities` | array of entity states (below) |

Camera players and `Spectator*` helper players are never listed (invisible
helpers); tracked players (Alpha/Bravo) are always listed, even outside the sphere. The
centre is the snapshot box centre (by default the midpoint between the tracked
players), so with players more than about 2 x `radius` apart, mobs near either
player can fall outside the sphere.

Entity state:

| field | meaning |
|---|---|
| `uuid`, `id` | UUID and the network entity id (matches `dig_progress.breaker_id`) |
| `kind` | entity type key path: `player`, `zombie`, `skeleton`, `creeper`, `item`, `tnt`, `arrow`, `falling_block`, `experience_orb`, ... |
| `type` | namespaced type, `minecraft:zombie` |
| `name` | player name, else custom name, else item id for dropped items, else null |
| `x`, `y`, `z`, `yaw`, `pitch` | position (feet) and orientation. For living entities `yaw` is the HEAD yaw (CraftBukkit reports `yHeadRot` as a living entity's yaw) |
| `head_yaw`, `body_yaw` | living entities only (`head_yaw` = `yaw`) |
| `vx`, `vy`, `vz` | velocity, blocks/tick |
| `bbox_w`, `bbox_h` | bounding box width / height (blocks, includes scale) |
| `health`, `max_health` | living entities, else null |
| `alive` | living: not dead and valid; others: `isValid()` |
| `on_ground`, `sneaking`, `sprinting` (players only, else false), `blocking` (players), `hand_raised` (living) | flags |
| `held_item` | players and equipped mobs: main-hand item |
| `offhand_item`, `held_slot`, `gamemode` | players only |
| `fuse_ticks`, `source` | primed TNT: remaining fuse, UUID of the igniting entity (or null) |
| `item` | dropped items: `{id, count}` |
| `block_state` | falling blocks |
| `shooter` | projectiles: shooter UUID or null |
| `fire_ticks` | `Entity#getFireTicks` (negative = fire immunity countdown / not burning) |

### Damage, knockback, death (T3 / T14 / T15)

| type | fields |
|---|---|
| `entity_damage` | `EntityDamageEvent` (MONITOR, cancelled events are not logged, so damage to cameras never appears): `victim`, `cause` (`ENTITY_ATTACK`, `PROJECTILE`, `FALL`, `FIRE_TICK`, ...), `damage_type` (`minecraft:player_attack`, `minecraft:arrow`, ...), `damage` (raw), `final_damage` (after armour / blocking / effects), `health_before`, `health_after` (estimate `max(0, health_before - final_damage)`, ignores absorption), `attacker` (direct damager; for projectiles the shooter), `projectile` {`uuid`, `type`} or null, `causing_entity` (DamageSource causing entity, e.g. the player who lit the TNT), `victim_blocking`, optional `damager_block` [x,y,z] |
| `entity_knockback` | Paper `io.papermc.paper.event.entity.EntityKnockbackEvent` (incl. `EntityKnockbackByEntityEvent`): `entity`, `cause` (`ENTITY_ATTACK`, `EXPLOSION`, `SHIELD_BLOCK`, `SWEEP_ATTACK`, `DAMAGE`, `PUSH`, ...), `attacker` (or null), `vector` [x,y,z] (knockback velocity added), optional `strength` |
| `entity_death` | existing fields plus `victim` (ref), `victim_name`, `killer_entity` (ref of the last damager, projectiles resolved to the shooter; falls back to `getKiller()`), `killer_name`. `killer` (player name, vanilla kill credit) is unchanged |

## 5.6 Action events (T15, new section)

Tracked players only (the `players` of the session; never cameras / spectators).

| type | source | fields |
|---|---|---|
| `arm_swing` | `PlayerAnimationEvent` | `player`, `animation` (`ARM_SWING`, `OFF_ARM_SWING`), `hand` (`HAND` / `OFF_HAND`) |
| `held_item_change` | `PlayerItemHeldEvent` | `player`, `previous_slot`, `new_slot`, `previous_item`, `new_item` |
| `use_item_start` | per-tick transition of `hasActiveItem()` (sampled with the `tick` record) | `player`, `item`, `hand`, `held_slot` |
| `use_item_stop` | same | `player`, `item`, `hand`, `start_tick`, `ticks_used` (server ticks since `use_item_start`), `used_time` (`getActiveItemUsedTime` at the last sample), `reason` (`stopped`, `item_changed`, `episode_end`) |
| `item_consume` | `PlayerItemConsumeEvent` | `player`, `item`, `hand` |
| `bow_shoot` | `EntityShootBowEvent` (bows and crossbows) | `player`, `bow`, `consumable`, `hand`, `force` (0-3 for bows), `projectile` (UUID), `projectile_type` |

## 5.7 Digging (T17, new section)

| type | source | fields |
|---|---|---|
| `dig_start` | `BlockDamageEvent` | `player`, `pos`, `block_state`, `face`, `item`, `insta_break` |
| `dig_progress` | `BLOCK_BREAK_ANIMATION` packets (ProtocolLib) | `player` (breaker name, null if not a player), `breaker_id` (entity id), `pos`, `stage` (0-9 crack stage; any other value, e.g. -1 or 10, removes the crack), `cleared` (stage outside 0-9), `packet_server_tick` (server tick when the packet was sent), `block_state` (at write time) |
| `dig_abort` | `BlockDamageAbortEvent` | `player`, `pos`, `block_state`, `item`, `start_tick`, `dig_ticks` (-1 if the start was not seen) |
| `dig_finish` | written right after the matching `block_break` | `player`, `pos`, `block_state` (broken block), `tool`, `start_tick`, `dig_ticks`, `insta_break` |

`dig_progress` notes: the server sends one packet per viewer (not to the breaker
itself), so the plugin keeps a single copy per (breaker, pos) stage sequence.
Packets are captured on the network thread and written by the main thread
(next tick start, or just before a `dig_start` / `dig_abort` / `block_break`
record); `epoch_ms` is the capture time, so a `dig_progress` can appear a few
`seq` after the event it precedes. Order by `epoch_ms` / `packet_server_tick`
when reconstructing the crack animation. A dig with no other player within 32
blocks of the block produces no packets and hence no `dig_progress` (in normal
runs the other bot, cameras and spectators are always nearby). Relayed packets
that `CameraManager` sends to camera clients are ignored.
