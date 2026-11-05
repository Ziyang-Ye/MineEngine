## Round 2: world-mechanics episodes (`controller/episode-handlers/diverse/`)

Six non-damage (protected) episode types that add interaction effects driven
by game mechanics. All of them follow the stateful-episode rules
(`state-common.js`): every `/fill` `/setblock` `/time` edit happens in
`setupEpisode`, i.e. before the leader opens the world log and takes the
start snapshot; during recording only the bots' own actions and natural
game mechanics change the world, so `start snapshot + replayed block_state
records == end snapshot` (`replay_mismatch=0`).

| type key | class | file |
|---|---|---|
| `farming` | `FarmingEpisode` | `farming-episode.js` |
| `redstoneDoors` | `RedstoneDoorsEpisode` | `redstone-doors-episode.js` |
| `fallingBlocks` | `FallingBlocksEpisode` | `falling-blocks-episode.js` |
| `lightAndFire` | `LightAndFireEpisode` | `light-and-fire-episode.js` |
| `waterWorks` | `WaterWorksEpisode` | `water-works-episode.js` |
| `snowIce` | `SnowIceEpisode` | `snow-ice-episode.js` |

Shared base: `MechanicsEpisodeBase` in `mechanics-common.js` (extends
`StateEpisodeBase`). All six are `WORKS_IN_NON_FLAT_WORLD = true`,
`DAMAGE_ENABLED` is not set (protected: peaceful, resistance 255,
saturation). Measured length 35-65 s (recording start to stop).

Registration (in `episodes-loop.js` / `episode-weights.js`):

```js
const { FarmingEpisode } = require("./episode-handlers/diverse/farming-episode");
const { RedstoneDoorsEpisode } = require("./episode-handlers/diverse/redstone-doors-episode");
const { FallingBlocksEpisode } = require("./episode-handlers/diverse/falling-blocks-episode");
const { LightAndFireEpisode } = require("./episode-handlers/diverse/light-and-fire-episode");
const { WaterWorksEpisode } = require("./episode-handlers/diverse/water-works-episode");
const { SnowIceEpisode } = require("./episode-handlers/diverse/snow-ice-episode");
// episodeClassMap: farming, redstoneDoors, fallingBlocks, lightAndFire, waterWorks, snowIce
// episodeTypicalLengths (s, measured in live tests): farming 40, redstoneDoors 40,
//   fallingBlocks 35, lightAndFire 60, waterWorks 55, snowIce 45
```

### Common behaviour

- **Site.** Both bots compute a site candidate near their midpoint: a flat
  `(2*half+1)^2` patch (solid ground, 3 free blocks above every column)
  whose clearing volume (`(2*half+3)^2` x `CLEAR_HEIGHT+1`) contains no logs
  or leaves, ring search up to 16 blocks. The follower then adopts the
  leader's candidate (coordinator handshake `mech_site`), so both always use
  the same site. If no such patch exists (normal terrain) the leader
  flattens one before the snapshot: `fill` dirt (sandstone under sand) 3
  deep, the original top block (grass_block / sand / snow_block / ... else
  grass_block) on top and air 6 high, over `(2*half+3)^2` columns
  (`site_flattened: true`); if that cut into trees, the natural leaves within
  `half+8` blocks are made `persistent=true` (same look) so they cannot decay
  during the episode. The work area is always cleared to air `CLEAR_HEIGHT`
  blocks high. In normal worlds the leader also sets every `kelp` head in the
  snapshot box (below y=63) to `age=25` (fully grown, same look) so natural
  kelp cannot grow during the episode (its head -> `kelp_plant` change is not
  logged, see the gap table). A second handshake (`mech_ready`) makes the
  follower wait for the leader's commands before teleporting.
- **Test hook.** `MECH_FORCE_STATIONS=<4 station types>` (environment of both
  controllers) forces the redstoneDoors station list (the shared RNG is
  consumed as usual). Not used in production.
- **Roles.** `actor` / `observer` as in the stateful episodes
  (`decidePrimaryBot`). In these episodes the observer is also a worker:
  both bots act, usually facing each other across the work area (homes
  at u = +3 / -3), and whoever is not acting in a phase watches a blend of
  the work spot and the other bot (`startObserving`: look target between
  both when they fit in the view, small sidesteps every few seconds), so
  the other player is in view most of the time (live tests: 0.66-1.00 of the
  frames per bot).
- **Shared RNG.** `sharedBotRng` is consumed only in `setupEpisode` /
  `planEpisode`, identically on both bots (plans never depend on
  `isActor`). Local timing jitter uses `Math.random`.
- **Timeouts.** Every phase has a timeout (default 30 s); every walk, dig,
  place and use call is bounded; an expired phase lets the barrier chain
  continue.
- **Notes.** Each bot writes a `note` record (world log) at the end of each
  phase: `{"ep", "bot", "phase", ...result}` (see per-type tables).
- **Preferred time of day / weather.** `static TIME_PREFERENCE = {night: p,
  dusk: q}`: when `env-setup.js` chose the time at *random* (policy
  `random`) and it is day, the leader switches the episode to night
  (`TIME_NIGHT` = 18000) with probability p or dusk (`TIME_DUSK` = 12500)
  with probability q (`time set` before the snapshot; the daylight cycle is
  frozen). Used by `lightAndFire` (night 0.4, dusk 0.3) and `redstoneDoors`
  (dusk 0.15, night 0.1). `static PREFERRED_WEATHER` uses the env-setup.js
  format (label or list); `farming` and `lightAndFire` declare `"clear"`
  (rain hydrates farmland - not logged - and puts out fire). If env-setup
  did not honour it (weather policy not `fixed`, weather not allowed), the
  leader sends `weather clear` in setup and waits 4.5 s for the rain level
  to ramp down before the snapshot. The env keys in `state_metadata` are
  updated (`time_policy` / `weather_policy: "episode_preferred"`, plus
  `time_of_day_base`, `time_policy_base`, `weather_base`). `fixed` /
  `eval_day` policies are never overridden. Once `episodes-loop.js` passes
  the episode class to `chooseEpisodeEnvironment`, PREFERRED_WEATHER is
  applied there and the setup override becomes a no-op; TIME_PREFERENCE is
  deliberately not named PREFERRED_TIME (env-setup reads that static as a
  label list, which cannot express "70 % dark").

### `state_metadata` keys (all six types)

| key | meaning |
|---|---|
| `episode` | handler class name |
| `site` [x,y,z] | ground block at the site centre (feet level = y+1) |
| `site_flattened` | the leader flattened the area (normal terrain) |
| `biome_client` | biome at the site as known to the client (`plains`, `desert`, `snowy_plains`, ...) or null. (Round-2 fixer: was `biome`, which shadowed the episode-level server-resolved `biome`; `biome` is now the leader's server-side biome at the site, the same on both bots, see r2-fixer.md) |
| `random_tick_speed_base`, `random_tick_speed_base_before_weather` | when PREFERRED_WEATHER switched the weather and the randomTickSpeed policy depends on it (`EPISODE_RANDOM_TICK_SPEED=auto`), the leader re-sets the gamerule before the snapshot; the new value and the one it replaced |
| `axis`, `side` | layout frame: u axis (`x`/`z`) and its sign towards the actor; p is the other horizontal axis |
| `actor`, `observer` | bot names |
| `actor_stand`, `observer_stand` [x,y,z] | home columns (feet level) |
| `observer_distance` | horizontal distance between the homes (blocks) |
| `preferred_time` | `night` / `dusk` if TIME_PREFERENCE switched the time, else null |
| `preferred_weather` | weather the leader switched to (PREFERRED_WEATHER), else null |
| `time_of_day`, `time_of_day_label`, `time_policy`, `weather*` | from env-setup; `time_policy` / `weather_policy: "episode_preferred"` (+ `time_policy_base`, `time_of_day_base`, `weather_base`) when switched |

### `farming`

Two crop rows between the bots (row A at u=+1 worked by the actor, row B at
u=-1 by the observer, 3 or 5 cells along p, walkway u=0). Phases: `till`
(hoe; coarse dirt needs two clicks) -> `plant` -> `bonemeal` (1-3 uses on 2-3
crops; p=0.35 the actor also bone-meals a grass block next to the field) ->
`harvest` (mature crops, or all with p=0.6) -> `collect` (walk the row edge,
picks up drops) -> `replant`. No water is placed (farmland hydration is a
`MoistureChangeEvent`, not logged).

| key | meaning |
|---|---|
| `field_size` | [2, n] |
| `ground_material` | `keep` (existing grass/dirt), `dirt`, `coarse_dirt`, `grass_block` (set before the snapshot when the ground was not tillable) |
| `actor_crop` / `observer_crop` | `wheat`, `carrots`, `potatoes`, `beetroots` (different per row) |
| `actor_seed` / `observer_seed` | item used |
| `actor_hoe` / `observer_hoe` | wooden ... netherite hoe |
| `actor_row` / `observer_row` | ground blocks [[x,y,z], ...] |
| `bonemeal_counts` | crops bone-mealed per bot |
| `bonemeal_grass` | grass block bone-mealed, or null |
| `harvest_all` | also break immature crops |
| `irrigation` | always false |

Notes: `till` {tool, tilled}, `plant` {crop, planted}, `bonemeal` {uses, grass}, `harvest` {broken, ages}, `replant` {replanted}.
World log: `player_interact` + `block_state` (farmland, dirt), `block_place` (crops), `block_fertilize`, `block_grow`, `block_break`, `entity_spawn` (items), `structure_grow` is not expected.

### `redstoneDoors`

Four stations on the line u=0 at p = -4, -2, +2, +4 (p=0 stays free for the
line of sight), operated alternately by actor and observer (`operator`);
the operator walks to (u=+-2, p) when out of reach. Mix: two redstone
stations, one manual, one placed during the episode. Pre-built station
blocks are `/setblock` before the snapshot.

| station type | blocks | interaction |
|---|---|---|
| `lamp_lever` | redstone lamp + floor lever on top | lever toggled 2-3 times |
| `lamp_button` | lamp + floor button on top (`button`) | pressed 1-2 times |
| `plate_lamp` | lamp sunk into the ground + pressure plate (`plate`) on it | operator walks over the plate |
| `iron_door` | iron door (2 high) or iron trapdoor (`block`), floor lever / button (`trigger`) next to it on the operator side | toggled / pressed |
| `piston` | piston / sticky piston facing up + block on top (`top_block`), floor lever next to it | 1-2 extend/retract cycles (may end extended) |
| `note_block` | note block on an instrument block (`instrument_block`, `instrument`) | 2-5 right clicks (note +1 each) |
| `wood_door`, `wood_trapdoor`, `fence_gate` | 11 wood types (gate between two fence posts) | opened / closed 2-3 times |
| `bell` | floor bell | rung 1-2 times |
| `place_door`, `place_trapdoor`, `place_gate` | none | operator places the `item`, then toggles it |

`state_metadata.stations`: list of `{index, type, pos [x,y,z], operator: "actor"|"observer", ...type parameters}`; `readback_inspection: true`.
Notes: `station_<i>_<type>` {pos, toggles / presses / clicks / rings / placed / block / stepped, inspected: [block names]}.

**Logging of redstone effects.** The logger reads block states back after
`player_interact` (clicked block, clicked-face neighbour, block above and
below), after block events and after piston events, but it has no record for
state changes caused only by redstone: lamp `lit`, iron door / trapdoor
`open`+`powered` (both door halves), button / pressure-plate release
(`powered=false` 20-30 ticks later), lamp switching off (4 ticks later),
the final `piston_head` / moved block after a piston movement (the
piston-event readback runs one tick later and still sees `moving_piston`).
The episode makes these visible with **inspection clicks**: after the circuit
settled (>= 0.4 s, >= 2.3 s for buttons / plates) the operator right-clicks
with an empty hand a block that has no use action (lamp, iron door lower
half, iron trapdoor, pressure plate, piston base, piston head, pushed block);
the resulting `player_interact` (`item: null`) makes the logger re-read the
affected positions. A final `final_check` phase repeats the inspection.
Live tests: replay exact. Plugin extension that would make the clicks
unnecessary: handle `BlockRedstoneEvent` (MONITOR: mark the block dirty,
for doors also the other half) and re-read positions touched by piston
events / interactions again a few ticks later (e.g. +2 and +6 ticks).

### `fallingBlocks`

Tower column T (u=0, p=+2), water hole H (u=+1, p=+2), side pile (u=-1,
p=+2), anvil column (u=0, p=+3). Phases: `tower` (actor stacks 3-4 gravity
blocks, optionally on a support block) -> `drop` (actor breaks the support /
bottom block: the column falls) -> `side_drop` (observer places 1-3 gravity
blocks against the side of the tower top: each falls at once) -> `concrete`
(actor digs H, pours water, drops concrete powder from the tower side into
the water -> concrete; a second powder lands on top and stays powder) ->
`anvil` (p=0.45: anvil placed against the tower top falls next to it).

| key | meaning |
|---|---|
| `tower_material` | sand, red_sand, gravel or a concrete powder |
| `tower_height`, `tower_column` | gravity blocks in the tower, column [x,y,z] (ground level) |
| `support_block` | non-gravity support under the tower (broken in `drop`) or null |
| `side_material`, `side_count`, `side_column` | blocks dropped by the observer |
| `water_hole` | [x,y,z] |
| `concrete_powders` | [first (hardens), second (stays powder)] |
| `anvil`, `anvil_column` | anvil / chipped_anvil / damaged_anvil, or null |

Notes: `tower` {material, support, placed}, `drop` {broke, ok}, `side_drop` {material, dropped}, `concrete` {dug, water, first, second}, `anvil` {anvil, from_dy, landed}.
World log: `entity_spawn` (falling_block), `entity_change_block` (block -> entity, entity -> block), `block_form` (concrete), `bucket_empty`, `block_break`, `block_place`.

### `lightAndFire`

Lights on both sides of the site (actor: (1,-2) (2,2) (2,-3); observer:
(-1,-2) (-2,2) (-2,3)), candles (0,-1), fire base (0,-3), cake (0,+1),
campfire (0,+2), pumpkin (+-2,-1) on Y's side; Y works the campfire from
(+-1,+1) (diagonally next to it: bucket rays from further away hit other
blocks). Bot X (`fire_and_candles_by`)
does candles and fire, bot Y (`campfire_by`) the campfire, the candle cake
and the pumpkin. Phases: `lights` (both, 2-3 each) -> `candles` -> `campfire`
-> `fire` -> `carve` -> `safety` (fire must be gone) -> outro.

| key | meaning |
|---|---|
| `actor_lights`, `observer_lights` | `[{kind: ground|wall_torch|wall_lantern, item, support, pos}]`; items: torch, soul_torch, redstone_torch, lantern, soul_lantern, jack_o_lantern, glowstone, sea_lantern, shroomlight, end_rod, ochre/verdant/pearlescent_froglight; `wall_torch` = torch on the side of a stone block, `wall_lantern` = lantern on a wall block |
| `candle`, `candle_count`, `candle_blow_out`, `candle_pos` | candle colour item, 1-3 candles in one block, blown out (empty-hand click) and relit |
| `campfire`, `campfire_pos`, `campfire_douse`, `campfire_igniter` | campfire / soul_campfire, put out with `shovel` or `water` (water bucket -> waterlogged, then scooped back), relit with flint_and_steel / fire_charge |
| `fire_base`, `fire_block`, `fire_pos`, `burn_ms` | netherrack (fire) / soul_sand, soul_soil (soul_fire), burn time before X punches it out |
| `candle_cake` | candle item put on a cake (then lit) or null |
| `pumpkin_carving` | pumpkin position (carved with shears) or null |

Notes: `lights` {placed}, `candles` {candles, lit, blown_out, relit}, `campfire` {placed, doused {lit, waterlogged}, scooped, relit, cake}, `fire` {placed, fire, after}, `carve` {placed, block}.
World log: `block_place`, `block_ignite` (flint and steel, fire charge), `player_interact` + `block_state` (candles, lit, waterlogged, carved_pumpkin, candle_cake), `bucket_empty`/`bucket_fill`, `liquid_flow` (a waterlogged campfire leaks water until scooped), `block_break` (fire).
Fire `age` changes are not logged by the server (no event), so the fire is always removed before the end.

### `waterWorks`

Trench (u=0, p=-3..-1) dug by the actor, plus-shaped moat around the pillar
column (u=0, p=+2) dug by the observer. Phases: `dig` -> `pour_ends` (one
bucket at each trench end, poured from right next to the cell) -> `infinite`
(the middle became a source; actor scoops it, it refills) -> `fountain`
(observer builds a 1-3 high pillar and pours water: fountain into all four
moat cells from a 1-high pillar, waterfall into one moat cell from a taller
one; then scoops the source, water recedes) -> `aquatic` (actor: kelp or
seagrass, 1-2 sea pickles, then a slab (waterlogged) or a sponge (absorbs
the trench) in the middle cell; observer: slab / stairs in a dry moat cell,
waterlogged with a bucket and scooped back). Water never reaches the
ground the bots stand on.

| key | meaning |
|---|---|
| `trench`, `moat` | dug cells [[x,y,z], ...] |
| `pillar_material`, `pillar_height`, `water_feature` | `fountain` (h=1) / `waterfall` |
| `fall_seconds` | how long the water runs before it is scooped |
| `aquatic_plant`, `sea_pickles` | kelp / seagrass, 1-2 |
| `middle_cell` | `sponge` or the slab item placed into the middle trench cell |
| `moat_waterlogged_block` | slab / stairs waterlogged by the observer |

Notes: `dig`, `pour_end` {cell, block, above, source}, `infinite` {before, scooped_to, refilled, bucket_full}, `fountain` {built, source, source_block, scooped}, `aquatic` {plant_block, pickles, middle, middle_waterlogged}, `moat_waterlog` {placed, waterlogged, after}.
World log: `block_break`, `bucket_empty`, `bucket_fill`, `liquid_flow`, `fluid_level_change`, `block_place`, `sponge_absorb`.
No cobblestone generator: a reliable one needs a pre-built frame or ends as
obsidian, which `lavaWater` already covers.

### `snowIce`

Golem pattern (u=+1, p=-2), snow layers (u=-1, p=-2), snow block (u=-1,
p=-3), powder snow (u=-1, p=-1), ice blocks (u=+1/+2, p=+2), ice rink:
ground cells u=-3..+3 at p=+4 replaced before the snapshot. Phases: `build`
(actor: two snow blocks + carved pumpkin / jack o'lantern -> snow golem;
observer: snow layer stacked to 2-4 layers + snow block) -> `golem` (both
watch it) -> `powder_ice` (observer: powder snow bucket, maybe scooped
back; actor: 1-2 ice blocks) -> `slide` (the slider sprints along the rink
and slides out) -> `snowballs` (observer shovels its snow block, both throw
2-4 snowballs at each other) -> outro.

| key | meaning |
|---|---|
| `golem_pattern`, `golem_head` | [[x,y,z] x3], carved_pumpkin / jack_o_lantern |
| `snow_layers`, `snow_layer_pos`, `snow_block_pos` | target layer count (2-4) |
| `powder_snow_pos`, `powder_snow_scooped` | |
| `ice_blocks` | ice / packed_ice / blue_ice placed by the actor |
| `rink_material`, `rink` | ice / packed_ice / blue_ice, [start, end] ground blocks |
| `slider` | `actor` / `observer` |
| `snowballs` | {actor: n, observer: n} |

Notes: `snow_golem` {snow0, snow1, after: [3 block names], golem}, `snow_layers` {layers, block}, `ice_blocks`, `powder_snow` {placed, scooped}, `slide` {distance, on}, `snowballs` {thrown, shoveled}.
World log: `block_place`, `entity_spawn` (snow_golem reason BUILD_SNOWMAN, snowball, item), `block_state` (snow layers, pattern blocks -> air), `block_form` (golem snow trail in cold biomes), `bucket_fill` (powder snow), `block_break`.
The golem pattern removal has no block event; the pumpkin is placed by
clicking the top face of the upper snow block, so the `player_interact`
readback covers the upper snow block, the head position and (DOWN) the
lower snow block.

### Known logger gaps relevant to these mechanics

| change | event in Paper | logged? | handling here |
|---|---|---|---|
| redstone-only state changes (lamp, iron door, button/plate release, note block `powered`) | `BlockRedstoneEvent` (partly) | no | inspection clicks (redstoneDoors) |
| piston head / moved block after the movement | none (block entity finishes) | readback 1 tick after the event sees `moving_piston` | inspection clicks |
| farmland hydration | `MoistureChangeEvent` | no | no water near farmland |
| fire `age` | none | no | fire removed before the end |
| snow golem / iron golem pattern removal | none (`CreatureSpawnEvent` only) | no | click readback (snow golem only) |
| stem -> attached stem when a pumpkin / melon grows | none | no | no stems planted |
| door upper half on placement | none | no | first toggle click re-reads both halves |
| grass / podzol / mycelium `snowy` under a snow layer formed by a snow golem or snowfall | none (shape update) | no | snowIce golem pen floor without `snowy` property; snowfall (env-setup "snow" weather) is not handled |
| block resting on leaves (snow layer) when the leaves decay | none (support lost) | no | tree-free sites; leaves made persistent when flattening cut trees |
| kelp (and other growing plants: vines, cave vines) old head -> body (`kelp` -> `kelp_plant`) when it grows | `BlockSpreadEvent` for the new head only | no (seen in 3 normal-world episodes: natural kelp 20-30 blocks from the site, 1-3 voxels) | kelp heads in the snapshot box set to age 25 before the snapshot (normal worlds); plugin: mark the `source` of block_spread / block_grow dirty |
| cave vines head -> `cave_vines_plant` when it grows | `BlockSpreadEvent` for the new head only | no (seen once underground, 1 voxel) | none |
| vines that lose their support (natural, far from the site) | none (shape update) | no (seen once, 2 voxels 29 blocks from the site) | none |
| sugar cane / cactus `age` increments (top block, every random tick) | none | no | none (not seen in the live tests; affects every episode type with sugar cane / cactus in the box) |

Suggested plugin extensions (would make every case above exact without
workarounds): (1) `BlockRedstoneEvent` -> mark the block (and the other door
half) dirty; (2) `MoistureChangeEvent` -> log like `block_fade`; (3) for
every logged block event also mark the 6 neighbours dirty (catches snowy
grass, attached stems, kelp bodies, blocks that lose support, fence
connections) and re-read dirty positions once more a few ticks later
(pistons, lamps switching off, buttons / plates releasing).
