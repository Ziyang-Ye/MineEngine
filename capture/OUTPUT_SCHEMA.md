# Output schema

This describes every file a capture run produces that a
downstream proxy builder consumes: per-frame actions (with absolute camera
angles), per-episode world-state logs, block snapshots and episode metadata.
Field names are exact. "New" marks fields/files that this repository adds on top of upstream SolarisEngine.

## 1. Directory layout

`run.sh` / `run_laptop.sh` write one tree per batch:

```
<output-dir>/data_collection/train/batch_<k>/
  compose_configs/docker-compose-000.yml     generated compose file (instance 0)
  logs/docker-compose-000/<service>.log      container logs (mc_instance_0.log has "[worldstate]" lines)
  data/0/                                    Paper server dir: world/ (full Anvil save), plugins/, logs/
  output/                                    per-episode files, flat directory (sections 2-6)
  output/world_regions/<stem>_start/         New, only with --snapshot-mode region|both (section 6)
  camera/output_alpha/0/camera_alpha.mkv     raw 1280x720 recording of Alpha's view (whole session)
  camera/output_alpha/0/camera_alpha_meta.json
  camera/output_bravo/0/camera_bravo.mkv     same for Bravo
  camera/data_alpha/0/, camera/data_bravo/0/ Minecraft client install of the cameras (not data)
  aligned/<action_stem>_camera.mp4           one video frame per action entry (section 7)
  aligned/<action_stem>_camera_meta.json
  world_events_report.json                   New, output of postprocess/verify_world_events.py
```

File stems inside `output/` (`TS` = `YYYYMMDD_HHMMSS` local time of the writer,
`EEEEEE` = 6-digit episode number, `III` = 3-digit instance id):

| file | writer | per |
|---|---|---|
| `TS_EEEEEE_<Bot>_instance_III.json` | act_recorder | bot, episode |
| `TS_EEEEEE_<Bot>_instance_III_meta.json` | act_recorder | bot, episode |
| `TS_EEEEEE_<Bot>_instance_III_episode_info.json` | controller | bot, episode |
| `TS_EEEEEE_world_instance_III_world_events.jsonl` | server plugin (New) | episode |
| `TS_EEEEEE_world_instance_III_world_snapshot_start.json.gz` | server plugin (New) | episode |
| `TS_EEEEEE_world_instance_III_world_snapshot_end.json.gz` | server plugin (New) | episode |

`<Bot>` is `Alpha` or `Bravo`. The three writers use their own `TS`, so join
files on `(EEEEEE, III)`, never on the timestamp. The `world_*` files are
shared by both bots (one server, one world). `postprocess/prepare_train_dataset.py`
copies the world files of valid episodes into the dataset with the timestamp
stripped (`<prefix>_EEEEEE_world_instance_III_world_events.jsonl`).

## 2. Clocks and alignment

Three time bases appear; all are recorded so they can be joined exactly.

| field | meaning | where |
|---|---|---|
| `epoch_ms` / `epochTime` / `renderTime` | host wall clock (all containers share the host kernel clock) | world log (ms), action JSON (`epochTime` s, `renderTime` ms) |
| `game_time` | `World#getGameTime()`: ticks since world creation, monotonic, unaffected by `/time set` | world log |
| `worldAgeTicks` | Mineflayer `bot.time.age`, the same counter as `game_time`, but only refreshed when the server sends `update_time` (every 20 ticks) | action JSON |
| `server_tick` | `Bukkit.getCurrentTick()` (ticks since server start) | world log |
| MKV PTS | wall-clock seconds of each captured frame | camera recordings |

Recommended join: map each frame's `epochTime*1000` to game time with the
piecewise-linear map built from the `tick` records of the world log
(`(epoch_ms, game_time)` every tick); `worldAgeTicks` lags that estimate by
0..~25 ticks and is only a sanity check. A block change with `epoch_ms = t` is
visible from the first frame whose action time is `>= t` onward (in the
aligned video, frame `i` is captured at or after action `i`).
`postprocess/verify_world_events.py --alignment-dir D` writes, per episode,
`seq -> frame index` maps for both bots (`first_frame_at_or_after`, `nearest_frame`).

## 3. Per-frame action JSON: `TS_EEEEEE_<Bot>_instance_III.json`

A JSON array, one object per sample (~20 Hz, `VIEWER_RECORDING_INTERVAL=50` ms;
samples are not locked to server ticks). After alignment, entry `i`
corresponds to frame `i` of `aligned/<same stem>_camera.mp4`.

| field | type | meaning |
|---|---|---|
| `x`,`y`,`z` | float (3 dp) | feet position of the bot (block units). Eye = `y + 1.62` standing, `+1.27` sneaking |
| `yaw` | float (5 dp, New precision) | **absolute** yaw, Mineflayer radians in `[0, 2π)`: `yaw = (π − radians(mc_yaw)) mod 2π`; 0 faces −Z (north), π/2 faces −X (west). It is the last look sent to the server, i.e. what the camera client renders |
| `pitch` | float (5 dp, New precision) | **absolute** pitch, Mineflayer radians, `pitch = −radians(mc_pitch)`, positive = looking up, range `[−π/2, π/2]` |
| `yaw_mc_deg` | float, New | same yaw in Minecraft/F3 degrees `[−180, 180)` (0 = +Z south, 90 = −X west) |
| `pitch_mc_deg` | float, New | same pitch in Minecraft degrees (+90 = straight down) |
| `action.forward/back/left/right/jump/sprint/sneak` | bool | movement keys held at sample time |
| `action.camera` | `[dyaw, dpitch]` rad | change of (`yaw`, `pitch`) since the previous sample (dyaw wrapped to (−π, π]) |
| `action.attack/use/mount/dismount/place_block/place_entity` | bool | one-shot: happened since previous sample. `use` is also set by the new episodes for flint-and-steel / bucket use |
| `action.mine` | bool | digging in progress |
| `action.hotbar.1` … `hotbar.9` | bool | one-shot hotbar selection |
| `inventory` | object | `quickBarSlot`, `hotbar` (slots 36-44), `mainInventory` (9-35), `heldItem`, `offHandItem` (prismarine-item objects: `type`, `name`, `count`, `slot`, …) |
| `renderTime` | int ms | `Date.now()` in the controller at sample time |
| `physicsTime` | float ms | process-monotonic time of the bot's last physics tick |
| `worldAgeTicks` | int | see section 2 |
| `isInWater` | bool | |
| `frame_count` | int | 0-based sample index within the episode |
| `episode_id` | int | episode number |
| `relativeTimeMs` | float | `renderTime − first renderTime` |
| `epochTime` | float s | `renderTime / 1000` |
| `extra_info` | object, New | `{"seed": <int>, "seed_config": <str>, "world_type": "flat"|"normal", "level_type": "minecraft:flat"|"minecraft:normal"}`. Replaces the former hardcoded `{"seed": 42}`. `seed` is derived from the configured level-seed string; the authoritative value is `episode_info.world.seed` |

About yaw/pitch in upstream data: in this code base the per-frame JSON always
contained absolute `yaw`/`pitch` (rounded to 1e-3 rad) and nothing in
`postprocess/` removes them; samples whose yaw was still `null` (first tick
after login) were silently dropped by the recorder. This version keeps them,
raises the precision and adds the degree versions.

`TS_EEEEEE_<Bot>_instance_III_meta.json`: `episode_start_epoch`,
`first_render_time_ms`, `render_time_is_epoch`, `frames`, `video_fps`, `episode_id`.

## 4. Episode info: `TS_EEEEEE_<Bot>_instance_III_episode_info.json`

Upstream fields: `timestamp`, `bot_name`, `world_type`, `episode_number`,
`episode_type`, `instance_id`, `encountered_error`, `peer_encountered_error`,
`bot_died`, `episode_recording_started`, `eval_metadata`. New fields:

| field | meaning |
|---|---|
| `world.seed` | real seed reported by the server (`/seed`), decimal **string** (64-bit safe) |
| `world.seed_config` | level-seed string given to the server (`--world_seed` or `<instance><unix time>`) |
| `world.seed_value_expected` | numeric seed the server derives from `seed_config` |
| `world.world_type`, `world.level_type` | `flat`/`normal`, `minecraft:flat`/`minecraft:normal` |
| `world.generator_settings` | flat-world layers + biome JSON (null for normal worlds) |
| `world.flat_biome`, `world.flat_surface_block` | e.g. `plains`, `grass_block` |
| `world.generate_structures` | false when `--flat-no-structures` |
| `world.server_version`, `world.server_type` | `1.21`, `PAPER` |
| `world_state_leader` | true for the bot that drives the world log (Alpha) |
| `world_state` | plugin reply to `worldstate begin` (`stem`, `events_file`, `snapshot_start_file`, `region_files`, `seed`, `seed_str`, `world_type`, `generate_structures`, `game_time`, `server_tick`, `epoch_ms`, `box`) |
| `world_state_end` | plugin reply to `worldstate end` (`events_logged`, `snapshot_end_file`, `restored`, `restored_blocks`, `removed_entities`, `game_time`, `epoch_ms`) |
| `reset_world_between_episodes` | whether the box was restored after this episode |
| `state_metadata` | new episodes only: roles, site, layout, materials, variant (section 8) |
| `angle_convention` | human-readable reminder of the yaw/pitch convention |

## 5. World events: `TS_EEEEEE_world_instance_III_world_events.jsonl` (New)

UTF-8, one JSON object per line, written by the MirrorBot Paper plugin
(`WorldStateLogger`). Opened by the leader bot right before both bots start
recording; closed after the episode's teardown. All listeners run at
`MONITOR` priority and ignore cancelled events, so records describe what
actually happened.

Common fields of every record:

| field | meaning |
|---|---|
| `type` | record type (table below) |
| `seq` | 0-based, strictly increasing within the file |
| `game_time` | `World#getGameTime()` when the record was written |
| `server_tick` | `Bukkit.getCurrentTick()` |
| `epoch_ms` | host wall clock, ms |

Block states are written as Minecraft block-state strings,
e.g. `minecraft:oak_stairs[facing=east,half=bottom,shape=straight,waterlogged=false]`;
`*_type` fields are the bare block id (`minecraft:oak_stairs`). Positions are
integer block coordinates `[x, y, z]`; entity positions are floats.

Scope: a record is written if its position lies in the snapshot box or within
`log_radius` blocks (horizontal, default 64) of a tracked player
(Alpha/Bravo); `log_radius = -1` logs the whole world.

### 5.1 Session records

| type | fields |
|---|---|
| `episode_begin` | `format` (`solaris-world-events-v1`), `stem`, `episode_id`, `instance_id`, `episode_type`, `events_file`, `world` {`name`, `uid`, `seed` (number), `seed_str`, `environment`, `world_type`, `generate_structures`, `min_height`, `max_height`, `difficulty`, `storm`, `thundering`, `full_time`, `time_of_day`, `spawn`, `gamerules` {name: value string}}, `box` {`min`, `max`} (inclusive), `center` [x,y,z], `log_radius`, `heartbeat_ticks`, `tracked_players`, `snapshot` {`mode`, `start_file`, `end_file`, `region_files`}, `server_version`, `bukkit_version`, `plugin_version` |
| `tick` | `full_time` (day time incl. days, affected by `/time set`), `time_of_day` (0-23999), `players`: per tracked player {`name`, `world`, `pos` [x,y,z] feet, `yaw`, `pitch` (Minecraft degrees, server side), `sneaking`, `sprinting`, `on_ground`, `health`, `fire_ticks`, `held_slot`, `held_item`, `held_count`}. Written every `heartbeat_ticks` (default every tick) and once right after `episode_begin` and before `episode_end` |
| `note` | `text`: free text sent by a controller (`worldstate note`). The new episodes send compact JSON strings, e.g. `{"ep":3,"bot":"Alpha","phase":"ignite","fire":[10,64,-3]}` |
| `weather` | `storm` or `thundering` (new value) |
| `episode_end` | `stem`, `episode_id`, `reason` (`command`, `superseded`, `plugin_disable`), `events_logged`, `snapshot_end_file`, `restored`, `restored_blocks`, `removed_entities` |

### 5.2 Ground-truth state stream

| type | fields |
|---|---|
| `block_state` | `pos`, `block`, `state`, `prev_state` (last known state, may be null) |

After any event touches a position, the plugin re-reads that position at the
start of the next server tick and writes `block_state` if the state differs
from the last known one. **Start snapshot + `block_state` records in `seq`
order = exact block state of every touched voxel at every tick.** The
event records below add causes and actors; their `new_*` fields can be null
when the result is only known after the tick (liquids, fire, breaking).

### 5.3 Block events

Each has `pos`, `old_type`, `old_state`, `new_type`, `new_state` (new may be null) unless noted.

| type | Bukkit event | extra fields |
|---|---|---|
| `block_place` | BlockPlaceEvent / BlockMultiPlaceEvent | `player`, `item`, `against` [x,y,z], `multi` (one record per placed part) |
| `block_break` | BlockBreakEvent | `player`, `tool`, `drop_items` |
| `block_burn` | BlockBurnEvent (block destroyed by fire) | `source` |
| `block_ignite` | BlockIgniteEvent (fire appears) | `cause` (`FLINT_AND_STEEL`, `SPREAD`, `LAVA`, `EXPLOSION`, …), `player`/`entity_type`/`entity_uuid`, `source` |
| `block_fade` | BlockFadeEvent (ice melts, fire burns out, snow melts, …) | – |
| `block_form` | BlockFormEvent / EntityBlockFormEvent (water+lava → obsidian/cobblestone/stone, snow, ice) | actor fields for entity forms |
| `block_spread` | BlockSpreadEvent (fire spread, grass, mycelium, vines) | `source`, `source_state` |
| `block_grow` | BlockGrowEvent (crops, …) | – |
| `liquid_flow` | BlockFromToEvent | `pos` = destination, `old_*` = destination before flow, `from`, `from_state`, `face`; `new_*` null (see `block_state`) |
| `fluid_level_change` | FluidLevelChangeEvent (flowing level changes, drying) | – |
| `block_explode` | BlockExplodeEvent | `pos`, `yield`, `blocks`: [{`pos`, `old_type`, `old_state`}] |
| `entity_explode` | EntityExplodeEvent (TNT) | `center` [x,y,z], `yield`, `blocks` as above, `entity_type`, `entity_uuid` |
| `tnt_prime` | TNTPrimeEvent | `cause` (`PLAYER`, `FIRE`, `EXPLOSION`, `REDSTONE`, …), actor fields, `source` |
| `leaves_decay` | LeavesDecayEvent | – |
| `piston_extend` / `piston_retract` | BlockPistonExtend/RetractEvent | `pos` (piston), `direction`, `blocks` (moved, old states) |
| `entity_change_block` | EntityChangeBlockEvent (falling sand start/land, …) | actor fields; `new_state` = `getBlockData()` |
| `structure_grow` | StructureGrowEvent | `pos`, `species`, `from_bonemeal`, `player`, `blocks`: [{`pos`, `old_state`, `new_state`}] |
| `block_fertilize` | BlockFertilizeEvent | `pos`, `player`, `blocks` as above |
| `sponge_absorb` | SpongeAbsorbEvent | `pos`, `blocks` as above |
| `bucket_empty` | PlayerBucketEmptyEvent | `pos` = target block, `player`, `bucket` (`minecraft:water_bucket`, …), `clicked`, `clicked_state`, `face` |
| `bucket_fill` | PlayerBucketFillEvent | `pos` = clicked fluid block, `player`, `bucket`, `face` |
| `player_interact` | PlayerInteractEvent with a clicked block (tracked players only) | `player`, `action` (`RIGHT_CLICK_BLOCK`, `LEFT_CLICK_BLOCK`, `PHYSICAL`), `pos`, `state`, `face`, `item`, `cancelled`; marks the block and its neighbours for `block_state` readback (doors, levers, …) |

### 5.4 Entity events

| type | fields |
|---|---|
| `entity_spawn` | `entity`, `reason` (`CreatureSpawnEvent` reason, `ITEM` for dropped items, null otherwise) |
| `entity_death` | `entity`, `cause` (last damage cause), `killer` |
| `entity_remove` | `entity` (any non-player entity leaving the world: item pickup, despawn, primed TNT exploding, chunk unload) |

`entity` = {`uuid`, `type` (`minecraft:tnt`, `minecraft:item`, `minecraft:falling_block`, …), `pos` [x,y,z], `yaw`, `pitch`, optional `name` (players), `item`+`count` (items), `block_state` (falling blocks), `fuse_ticks` (primed TNT)}.

### 5.5 What is not captured

Block changes made by server commands during recording (`/setblock`,
`/fill`, `/clone`), redstone-only state changes that do not go through the
listed events, and block-entity contents (chest items, sign text). The new
episodes do all command-based edits in setup, before the start snapshot.
`verify_world_events.py` reports any voxel where start snapshot + replay
differs from the end snapshot.

## 6. Block snapshots (New)

### 6.1 `..._world_snapshot_start.json.gz` / `..._world_snapshot_end.json.gz`

Gzipped single JSON object (`format = solaris-block-snapshot-v1`). Start is
taken at `episode_begin`, end at `episode_end`, over the same box.

| field | meaning |
|---|---|
| `which` | `start` or `end` |
| `stem`, `episode_id`, `world`, `seed` | as in the log |
| `game_time`, `full_time`, `server_tick`, `epoch_ms` | capture time |
| `origin` | `[x0, y0, z0]` = box minimum corner |
| `size` | `[sx, sy, sz]` (default 65 x ~65-80 x 65: radius 32 around the episode centre, 24 below the lowest and 40 above the highest player) |
| `order` | `"yzx"`: voxel index `i = ((y−y0)*sz + (z−z0))*sx + (x−x0)` (x fastest) |
| `palette` | list of block-state strings |
| `rle` | run-length encoded palette indices: `[run_1, idx_1, run_2, idx_2, …]`, total length `sx*sy*sz` |
| `biome_y`, `biome_palette`, `biomes_rle` | biome id per (x, z) column sampled at `y = biome_y`, order `zx` (x fastest), RLE as above |
| `entities` | entities inside the box at capture time (same object as section 5.4) |

Decoding (Python):

```python
import gzip, json
s = json.load(gzip.open(path, "rt"))
sx, sy, sz = s["size"]; dense = []
for run, idx in zip(s["rle"][0::2], s["rle"][1::2]): dense += [idx] * run
state = s["palette"][dense[((y - s["origin"][1]) * sz + (z - s["origin"][2])) * sx + (x - s["origin"][0])]]
```

### 6.2 Region snapshot (`--snapshot-mode region|both`)

At `episode_begin` the plugin runs `save-all flush` and copies the Anvil
region files overlapping the box plus `level.dat` to
`output/world_regions/<stem>_start/` (`r.<rx>.<rz>.mca`, region = 32x32
chunks). These are standard Minecraft 1.21 save files (readable with
`anvil-parser`, `amulet`, etc.) and contain block entities as well.

### 6.3 Reset between episodes (`--reset-world`)

After the end snapshot, every voxel of the box whose state differs from the
start snapshot is set back (no physics), entities spawned during the episode
are removed, and loose items / XP orbs / falling blocks / primed TNT in the box
are removed. The restore itself is not logged (the log is already closed);
`world_state_end.restored_blocks` gives the count. Changes outside the box are
not reverted. With the default random teleport each episode happens in a new
place anyway.

## 7. Camera recordings and aligned videos

`camera/output_<bot>/0/camera_<bot>.mkv`: H.264 (NVENC, or libx264 fallback),
1280x720, 20 fps nominal, PTS = wall-clock epoch seconds (`-use_wallclock_as_timestamps 1 -copyts`).
`camera_<bot>_meta.json`: `recording_path`, `start_epoch_seconds`, `fps`,
`width`, `height`, `video_encoder`, `wallclock_timestamps`.

`aligned/<action_stem>_camera.mp4`: one frame per action entry (frame `i`
is the first camera frame at or after action `i`). `_camera_meta.json`:
`frame_mapping` [{`action_index`, `renderTime_ms`, `action_time_sec`,
`relative_time_ms`, `frame_index`, `frame_time_sec`, `delta_sec`}] and
alignment `diagnostics`.

## 8. New episode types and `state_metadata`

All are flat-world episodes except `buildAndBreak` (also normal worlds).
Common `state_metadata` keys: `episode`, `site` [x,y,z] (ground block of the
work site; placements happen at `y+1`), `axis`, `side`, `actor`, `observer`,
`observer_view` (`opposite`/`side`), `observer_distance`.

| type | extra keys | typical records |
|---|---|---|
| `igniteAndExtinguish` | `material`, `layout`, `blocks`, `burn_seconds`, `extinguisher` (`actor`/`observer`), `pickup_water` | block_place, block_ignite, block_spread, block_burn, block_fade, bucket_empty, liquid_flow, bucket_fill, fluid_level_change |
| `iceNearFire` | `variant` (`torch`, `netherrack_fire`, `lava`), `ice`, `light`, `melt_seconds`, `random_tick_speed` | block_place, block_ignite / bucket_empty, block_fade (ice→water), liquid_flow, block_form |
| `tntBlast` | `layout`, `tnt`, `retreat_distance`, `inspect_crater` | block_place, tnt_prime, entity_spawn (tnt, items), entity_explode, entity_remove |
| `lavaWater` | `hole`, `water_ground`, `pourer`, `pickup_water` | block_break, bucket_empty (lava, water), liquid_flow, block_form (obsidian/cobblestone), bucket_fill, fluid_level_change |
| `buildAndBreak` | `template`, `blocks` [{`pos`, `material`}], `break_indices`, `breaker` | block_place, block_break, entity_spawn (drops, falling_block), entity_change_block |

`iceNearFire` raises the `randomTickSpeed` gamerule (default 100, recorded in
`episode_begin.world.gamerules`) during the episode and resets it to 3 in teardown.
