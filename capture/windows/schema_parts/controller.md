## Controller additions (per-frame `state` / `peer`, episode environment, damage policy)

### 3.x Per-frame action JSON: `TS_EEEEEE_<Bot>_instance_III.json` (additions)

The samples are produced by `controller/recorder/frame-recorder.js`, a
drop-in replacement for `prismarine-viewer-colalab/lib/headless-base64.js`
when viewer rendering is disabled (`VIEWER_RENDERING_DISABLED=1`, the
default of `generate_compose.py` and of the Windows runtime). All upstream
fields keep their names, order and semantics; two keys are appended to every
sample and passed through unchanged by `act_recorder.py`. With rendering
enabled the upstream viewer is used and these two keys are absent.

Sampling cadence: on Linux the next sample is taken `VIEWER_RECORDING_INTERVAL`
(50) ms after the previous one finished, as upstream (~19.5 Hz). On Windows
the samples are placed on a 50 ms deadline grid (mean 20.0 Hz; individual
gaps jitter by up to ~16 ms because of the Windows timer granularity, which
would otherwise make the upstream scheme run at 62.5 ms = 16 Hz).
`renderTime` is the exact sample time in both cases.

Values are read in the controller at sample time, i.e. this client's view of
the world (the server-side truth is in the world log).

| field | type | meaning |
|---|---|---|
| `state` | object, New (T16) | own player state, see below. Never missing; any sub-field whose source fails is `null` |
| `pose_seq` | int or null, New (T12) | only with the camera pose link (env `CAMERA_POSE_PORT`): `seq` of the last movement packet forwarded to the camera (`controller/recorder/camera-pose-link.js`); its yaw/pitch are this sample's `yaw`/`pitch`. null before the first packet. The camera sidecar's `pose_seq` says which packet each frame rendered |
| `state.health` | float | `bot.health` (0-20, half hearts) |
| `state.food` | int | `bot.food` (0-20) |
| `state.saturation` | float | `bot.foodSaturation` |
| `state.xp_level`, `state.xp_progress` | int, float | experience level and progress to the next level (0-1, 4 dp) |
| `state.oxygen` | int | `bot.oxygenLevel` (0-20; air supply ticks / 15). Mineflayer only sets it when the server sends an air-supply update, i.e. once the air changes (or after a respawn); until then the value is 20 (full air, 300 ticks), which is the true value because air can only drop underwater |
| `state.effects` | list | active status effects `{id, amplifier, duration}`; `id` = namespaced effect (`minecraft:resistance`), the numeric registry id if unknown; `duration` in ticks, `-1` = infinite |
| `state.hotbar` | list of 9 | `{slot: 0-8, id: "minecraft:stone" \| null, count}` (count 0 for an empty slot) |
| `state.selected_slot` | int | selected hotbar slot 0-8 (= `inventory.quickBarSlot`) |
| `state.offhand` | object \| null | `{id, count}` of the off-hand slot |
| `state.armor` | object | `{head, chest, legs, feet}`, each `{id, count}` or null |
| `state.on_ground` | bool | Mineflayer physics `onGround` |
| `state.sneaking` | bool | sneak control held, or the entity metadata says crouching |
| `state.sprinting` | bool | sprint control held (Mineflayer applies sprint whenever it is held while moving forward) |
| `state.flying` | bool | creative/allowed flight active (abilities packet flag) or elytra flying |
| `state.in_water` | bool | same as `isInWater` |
| `state.alive` | bool | `health > 0` and Mineflayer `isAlive`. With `doImmediateRespawn` a death usually lasts less than one sample; use `episode_info.bot_died` / world-log `entity_death` |
| `state.head_yaw` | float | head (look) yaw, Mineflayer radians, = top-level `yaw` (last look sent to the server, falls back to `entity.yaw` while `yaw` is null) |
| `state.body_yaw` | null | not tracked client side (Minecraft computes the body rotation on each client). The server's body yaw is `tick.players[].body_yaw` in the world log |
| `state.cursor.block` | object \| null | block under the crosshair within 5 blocks of the eye: `pos` [x,y,z], `face` hit face (`up`, `down`, `north`, `south`, `west`, `east`; a block placed from this hit goes to `pos` + face normal), `name` (`minecraft:oak_planks`), `state` (block-state string in the world-log format, properties sorted by name, e.g. `minecraft:oak_stairs[facing=east,half=bottom,shape=straight,waterlogged=false]`), `distance` (eye to hit point, blocks, 3 dp). Ray: eye = feet + 1.62 (1.27 sneaking), direction from the rendered (last sent) yaw/pitch, fluids and air are ignored. Shapes: the block's collision shapes (Mineflayer) when it has any; blocks without collision are targeted like in vanilla using an approximate outline box (short grass/fern/dead bush, flowers, standing torches, rails/redstone wire/tripwire as a 1/16 slab, pressure plates, 1-layer snow; every other non-colliding block, e.g. wall torches, crops, saplings, signs, cobweb, as the full cube). Caveats: blocks whose collision shape differs from their outline (fences/walls 1.5 high, etc.) use the collision shape; random XZ offsets of flowers/grass are not modelled |
| `state.cursor.entity` | object \| null | nearest targetable entity hit by the same ray before the block (items, XP orbs, projectiles, falling blocks, camera/spectator players excluded): `uuid`, `id` (namespaced type, `minecraft:zombie`, `minecraft:player`), `entity_id` (network entity id), `type` (Mineflayer category: `player`, `hostile`, `animal`, ...), `name` (player name or entity type name), `distance` (eye to hit point) |
| `state.cursor_at_action` | object \| null | `null` unless `action.attack`, `action.use` or `action.place_block` is true in this sample; then `{attack, use, place_block}`, each `{block, entity}` (same format as `state.cursor`) for an action that fired, else null. It is the crosshair target latched at the moment of the action (first one of each kind since the previous sample), from the bot's look at that moment; `state.cursor` is read at sample time, up to 50 ms later. For `place_block` it is latched when the block_place packet is sent, so `cursor_at_action.place_block.block` is the block placed against (world-log `block_place.against`, `pos` + `face` normal = `block_place.pos`), whereas `state.cursor.block` of that sample already shows the new block: Mineflayer only fires the place action once the server has confirmed the block |
| `peer` | object \| null, New (T16) | the other bot as seen by this client (null only if no peer name is configured) |
| `peer.name` | string | other bot's name |
| `peer.known` | bool | the other bot's entity is currently loaded/visible to this client. If false, all fields below are null |
| `peer.pos` | [x,y,z] | other bot's feet position (4 dp) |
| `peer.yaw`, `peer.pitch` | float | other bot's look rotation from the entity move/look packets, Mineflayer convention (radians; yaw 0 faces -Z, pi/2 faces -X; pitch positive = up). Received as 1/256-turn bytes, so quantised to ~1.4 deg |
| `peer.head_yaw` | float | head yaw from the head-rotation packet, same convention and quantisation |
| `peer.distance` | float | distance between the two bots' feet (blocks, 3 dp) |

### 4.x Episode info: `state_metadata` (additions)

`state_metadata` is now the merge of keys set by the controller for every
episode and the episode handler's own keys (section 8); on a name clash the
handler's value wins. Both bots write the same values.

| key | meaning |
|---|---|
| `time_of_day` | `/time set` value applied before the start snapshot (ticks, 0-23999): 6000 by default, 12500 or 18000 for `DUSK_NIGHT_FRACTION` (default 0.15) of the episodes (half each), 1000 for eval episodes with `EVAL_TIME_SET_DAY=1`, or `FIXED_TIME` when >= 0 |
| `time_of_day_label` | `day` (< 12000), `dusk` (12000-13799), `night` (13800-22199), `dawn` (22200+; only with `FIXED_TIME`) |
| `time_policy` | `random` (seeded by `<bot_rng_seed>_env_<episode>`, reproducible and independent of the bots' shared RNG), `eval_day`, or `fixed` |
| `weather` | `clear` |
| `daylight_cycle`, `weather_cycle` | `false`: the `doDaylightCycle` / `doWeatherCycle` gamerules are off, so time and weather stay constant during the episode (also visible in `episode_begin.world.gamerules`, `time_of_day`, `storm`) |
| `mob_spawning` | `false`: before every episode the leader sets `doMobSpawning`, `doInsomnia` (phantoms), `doPatrolSpawning` and `doTraderSpawning` to false, so no natural mobs appear (also at dusk/night on normal difficulty). Before a damage episode it also kills loaded hostile mobs left over from earlier episodes (`kill @e[type=...]` for zombie, husk, drowned, skeleton, stray, bogged, creeper, spider, cave spider, enderman, witch, slime, phantom, silverfish and the raiders); this happens before the episode's own setup, so mobs a handler `/summon`s are not affected. Monster spawner blocks are not affected by the gamerule |
| `difficulty` | `normal` for damage episodes, else `peaceful` (also `episode_begin.world.difficulty`) |
| `damage_enabled` | `true` for damage episodes (`pvp`, `pve`, `pveMobs`, `pvpMelee`, `pvpRanged`, or handlers with `static DAMAGE_ENABLED = true`): no resistance or saturation effect, health and food start full (instant health + 1 s saturation) and then change naturally (`naturalRegeneration` default, `keepInventory` and `doImmediateRespawn` on, fall damage off). Otherwise `false`: resistance 255 + saturation, as upstream. Camera players are always protected |

New controller flags / env (entrypoint.sh): `--dusk_night_fraction` /
`DUSK_NIGHT_FRACTION` (0.15), `--fixed_time` / `FIXED_TIME` (-1 = random
policy), `--world_entities_every` / `WORLD_ENTITIES_EVERY` (1) and
`--world_entities_radius` / `WORLD_ENTITIES_RADIUS` (64), passed to the
plugin's `worldstate begin` for the per-tick `entities` records.
