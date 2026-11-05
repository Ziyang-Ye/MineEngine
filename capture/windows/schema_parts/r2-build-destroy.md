## Round 2 – build & destroy episodes (03 §2 / T6) and material pools (T7)

New episode types (handlers in `controller/episode-handlers/diverse/`, shared
helpers in `diverse/diverse-common.js`, material pools in
`controller/utils/materials.js`):

| type key | class | file | typical length (s) | INIT_MIN / MAX bot distance | WORKS_IN_NON_FLAT_WORLD |
|---|---|---|---|---|---|
| `coBuild` | `CoBuildEpisode` | `co-build-episode.js` | 105 | 4 / 10 | true |
| `buildVsBreak` | `BuildVsBreakEpisode` | `build-vs-break-episode.js` | 50 | 4 / 9 | true |
| `digDown` | `DigDownEpisode` | `dig-down-episode.js` | 60 | 4 / 9 | true |
| `tunnel` | `TunnelEpisode` | `tunnel-episode.js` | 70 | 4 / 9 | true |
| `crater` | `CraterEpisode` | `crater-episode.js` | 80 | 5 / 11 | true |
| `tntDemolish` | `TntDemolishEpisode` | `tnt-demolish-episode.js` | 50 | 6 / 12 | true |
| `forestBuild` | `ForestBuildEpisode` | `forest-build-episode.js` | 80 | 4 / 10 | true (normal worlds ONLY, see below) |

Typical lengths are measured from live runs (flat + normal worlds; range
35-115 s; deadlines keep every episode below ~2 min).

All seven are non-damage episodes (protected: peaceful, resistance 255 –
`tntDemolish` relies on it). Episode categories (03 §4): `coBuild`,
`forestBuild` → building; `buildVsBreak`, `digDown`, `tunnel`, `crater`,
`tntDemolish` → destruction & digging.

### Common rules

* All server-side world edits of these handlers happen in `setupEpisode`
  (leader bot, BEFORE `worldstate begin` takes the start snapshot):
  removal of plant / snow cover (`fill … air replace <cover>` – grass, ferns,
  flowers, snow layers, dead bushes, berry bushes, mushrooms – nothing else),
  the optional site levelling (normal worlds, see `site_prepared`), fluid
  removal around dig boxes (normal worlds) and the `randomTickSpeed`
  gamerule (`forestBuild`). During recording the world only changes through
  the bots' own place / break / use actions and game mechanics (falling
  sand/gravel, explosions, leaf decay, grass spread), all of which are in
  `world_events.jsonl`; the episodes were verified with
  `replay_mismatch = 0`.
* Both bots draw the plan from the shared bot RNG in the same order with a
  fixed number of draws, so `state_metadata` is identical in both
  `episode_info` files.
* Observer behaviour (T8): the bot that is not acting keeps 3–20 blocks
  from the work (never < ~2 blocks from the other player), looks at the
  other player ~70 % of the time and at the work site otherwise, and changes
  its vantage point every 6–11 s with p≈0.5 (pathfinder that never digs or
  places). The acting bot glances at its partner between blocks (p≈0.35,
  skipped when the partner is already in view) and after each layer / tree.
  Each phase ends when the worker reports "done" over the coordinator
  (`diverseSignal` messages), so the observer watches exactly as long as the
  work lasts.
* `note` markers (`worldstate note`, JSON text) are written at every phase
  change, e.g. `{"ep":3,"bot":"Bravo","phase":"dig_layer","layer":2,"y":61,"failed":0}`.

### `state_metadata` keys (all seven types)

| key | type | meaning |
|---|---|---|
| `episode` | string | handler class name |
| `site` | [x,y,z] | ground block of the work site centre (placements at `y+1`); also the world-log snapshot centre |
| `axis`, `side` | string, ±1 | layout frame: `u` = `side` × unit vector of `axis`, `p` = the perpendicular axis (+x or +z) |
| `actor`, `observer` | string | bot names of the two roles (role meaning per type below) |
| `site_prepared` | bool | true if a pad was levelled with `/fill` before the start snapshot (normal worlds when no flat site was found) |
| `site_prepare_commands` | [string] | only when `site_prepared`: the exact `/fill` commands (fill holes with dirt, set the top layer, clear 6+ blocks above) |
| `materials` | object | material per role (see T7 below) |
| `observer_policy` | string | short description of the observer behaviour used |

Per type:

| type | extra keys |
|---|---|
| `coBuild` | `house` {`width` (p), `depth` (u), `wall_height` (3), `door_side` (`u+`,`u-`,`p+`,`p-`), `door_gap` [x,y,z] (lower cell), `door_placed`, `windows` [[x,y,z]], `roof_style` (`flat` / `rim` = perimeter roof cells in the pillar log), `floor` ("interior ground replaced"), `torches` [[x,y,z]], `blueprint` [{`pos`, `material`, `part`: floor/pillar/wall/window/roof/door/torch}]}, `materials` {walls, pillar, floor, roof, window, door}, `material_counts`, `non_full_fraction`, `roles` {`walls`: bot, `interior`: bot} |
| `buildVsBreak` | `shape` (`wall` 3-5×2-3, `tower` 1×3-5, `pillar2` 2×2×2-3, `arch` 3 wide), `voxels` [[x,y,z]], `rounds` [{`mode`: `turns`/`contest`, `material`}], `keep_last_structure`, `materials` {`walls`: [..]}, `material_counts`, `roles` {`builder`, `breaker`} |
| `digDown` | `pit` {`size` [s_u, s_p] (2..5), `depth` (1..4, volume ≤ 40), `volume`, `box` [x1,y1,z1,x2,y2,z2], `top_y`, `bottom_y`}, `dig_method` (`layer_by_layer_in_place`), `exit_stairs` {`from` (bottom feet cell at the +u edge), `dir` [dx,dz], `steps`} or null (the staircase the digger cuts into the +u wall to climb out; not used with layer refill), `refill` (bool), `refill_mode` (`none` / `layer` = blocks placed from the bottom up while the digger pillars up / `gravity` = sand or gravel dropped from the rim), `refill_material`, `observer_view` |
| `tunnel` | `tunnel` {`mode` (`pit`: from the bottom of a 2×2 shaft; `hill`: straight into a hillside, normal worlds), `length` (4-8), `width` (1-2), `height` (2), `direction` [dx,dz], `floor_y`, `box`, `cells` [[x,y,z]]}, `shaft` {`size`, `depth` (3-4), `top_y`, `cols`} or null, `refill`, `refill_material`, `torch` (torch on the tunnel floor), `observer_view` |
| `crater` | `crater` {`radius` (2.5-4; reduced until the volume is ≤ 55, ≤ 36 when refilled), `diameter`, `center_depth` (1-2), `volume`, `top_y`, `box`, `cells` [[x, z, depth]]}, `dig_order` (`centre_out` / `rim_in`), `refill`, `refill_material` |
| `tntDemolish` | `hut` {`size` [3|4, 3|4], `wall_height` (2-3), `pillars`, `roof`, `door_gap`, `windows`, `blocks` [{pos, material, part}]}, `materials`, `material_counts`, `non_full_fraction`, `tnt` [{`pos`, `where`: inside/outside}], `ignite_index`, `builders`, `observer_distance` (10-15), `inspect_crater` |
| `forestBuild` | `trees_requested` (3-6), `trees` [{`base` [x,y,z] lowest log, `type` (log block), `height` (logs), `grove` (index), `feller` (bot)}], `groves` [{`trees`, `leaves`}], `felling`, `random_tick_speed`, `hut` {`origin`, `axis`, `side`, `size`, `wall_height`, `pillars`, `door_side`, `door_gap`, `windows`, `foundation` [[x,y,z]] (cells one block below the hut ground that get a floor-material block first), `blocks`} or null, `hut_skipped` (no free, level clearing found), `materials`, `material_counts`, `non_full_fraction`, `roles` |

Roles: `coBuild` actor = walls builder (corner posts + walls from
outside), observer = interior builder (digs the interior ground and lays
the floor, sets windows and a torch from inside, builds the roof from
inside, leaves and hangs the door in the gap). `buildVsBreak` actor =
builder, observer = breaker (both active; whoever is idle watches standing
still on its side of the structure). `digDown` / `tunnel` / `crater`
actor = digger, observer = watches from the rim. `tntDemolish`: both build
half of the hut walls, the actor adds the roof, places and lights the TNT
and runs 9-12 blocks away; the observer watches from 10-15 blocks.
`forestBuild`: both fell (trees alternate), both build hut walls, the
observer builds the roof from inside.

Episode flow (barrier-synchronised steps, names in the controller logs and
world-log notes):

* `coBuild`: greet → foundation (posts ‖ floor) → walls (walls ‖ go inside,
  windows after the first wall row, torch) → roof (from inside) → exit (+door)
  → admire_walk → admire → outro.
* `buildVsBreak`: greet → per round `build_r` + `break_r` (turns) or
  `contest_r` (breaker digs while the builder places) + `clear_r` → admire →
  outro. The last break is skipped when `keep_last_structure`.
* `digDown`: greet → dig (stand on the middle column, dig the other cells of
  the layer under the feet, then the own block – drop one level) → pause →
  after (layer refill + pillar up / leave the pit + gravity refill / just
  leave) → admire → outro. Leaving the pit (`exit_pit` note, `method`):
  `walk` (depth 1), `stairs` (cut a staircase into the +u wall, the normal
  case), `pillar`, `pathfinder_dig`, `teleport` (last resort, RCON `tp`).
* `tunnel`: greet → dig (shaft in place, then 2-high tunnel slices, head then
  feet block, walking in) → pause → finish (refill the tunnel slice by slice
  walking back + refill the shaft from the bottom up, or torch + leave) →
  admire → outro.
* `crater`: greet → dig (top layer ring by ring, then the inner layer) →
  pause → after (refill: inner layer, then the top layer from the rim
  inwards; or walk along the rim and inspect) → admire → outro.
* `tntDemolish`: greet → build_walls (both) → roof (actor) / walk to the
  viewing spot (observer) → place_tnt → ignite (flint and steel, sprint
  away) → blast (both watch 5.5-6.5 s) → inspect → outro.
* `forestBuild`: greet → fell (2-5 leaves by hand, logs bottom-up with the
  axe) → clearing (wait 4 s, break remaining canopy leaves inside the hut
  volume) → hut_walls (both) → hut_roof (from inside) → canopy (wait ≤ 12 s
  until every leaf of the felled groves has decayed, break leftovers in
  reach) → admire → outro.

Typical world-log records: `block_break` (+ `dig_start` / `dig_progress` /
`dig_finish`), `block_place` (doors as `multi` two-block places),
`entity_spawn` / `entity_remove` (drops, pickups), `entity_change_block`
(falling sand / gravel), `tnt_prime`, `entity_explode` (`tntDemolish`),
`leaves_decay` (`forestBuild`), `block_spread` (grass regrowing on dug dirt),
`block_state` read-backs, `note`.

### Normal worlds

* `forestBuild` throws in `setupEpisode` in flat worlds ("needs a normal
  (non-flat) world …") before any world change, i.e. the episode is aborted
  cleanly (`encountered_error=true`, no world log). The class carries
  `static REQUIRES_NON_FLAT_WORLD = true` so the scheduler can exclude it
  from flat-world type lists. In normal worlds it fells whole "groves": all
  simple trunks (one vertical column of 3-7 logs, no branches / 2×2 trunks,
  nothing attached) whose canopies form ONE connected leaf component
  (≤ 700 non-persistent, non-waterlogged leaves, touching no other log,
  carrying no vines / cocoa / bee nests / snow …); a lone tree is a grove of
  one. After the last trunk of a grove is gone every leaf of its component
  decays (`leaves_decay`), so no leaf keeps an unlogged `distance` change;
  the episode waits for the decay before it ends. Whole groves are added
  until `trees_requested` is reached (max 6 trunks), so fewer trees are
  felled when only small groves exist. `randomTickSpeed` is raised to
  `state_random_tick_speed` (default 100) before the snapshot and reset to 3
  in teardown (`random_tick_speed`, also in `episode_begin.world.gamerules`);
  because of that, sites whose snapshot box contains blocks that change state
  on random ticks without any Bukkit event (sugar cane / cactus `age`,
  sapling `stage`, farmland `moisture`, turtle / sniffer eggs, lit redstone
  ore) are skipped. Groves are searched within 96 blocks of the bots (the
  bots are teleported there in setup). No usable grove → `setupEpisode`
  throws (clean abort). The hut goes on a free clearing next to the felled
  trees; cells one block lower than the hut ground get a foundation block.
* The other six find a flat site (flat square of half-size
  `SITE_HALF_SIZE` with 3-5 free blocks above) within 14 blocks of the bots;
  otherwise a pad is levelled with `/fill` before the snapshot
  (`site_prepared: true`, commands recorded). `tunnel` first looks for a
  hillside (4+ columns of solid, non-gravity blocks 3 high in a straight
  line from a standable mouth) and digs into it (`tunnel.mode = "hill"`).
  `tntDemolish` rejects sites with trees, vines or fluids within 7 blocks
  (leaf `distance` changes caused by an explosion would not be logged).
  Dig episodes replace water / lava next to the dig box by stone before the
  snapshot (normal worlds only).

### T7 – material pools (`controller/utils/materials.js`)

`POOLS`: `walls` (oak / spruce / birch / dark oak planks, cobblestone, stone
bricks, bricks, white / gray / red / blue / yellow / green concrete and
wool), `pillars` (oak / spruce / birch log), `windows` (glass + 7 stained
glass colours), `roof` (planks, stone bricks, dark oak planks), `floor`
(planks, stone, cobblestone), `extras` (sand, gravel, tnt), `refill` (dirt,
cobblestone, stone, gravel, sand), `scaffoldSafe` (walls that the
pathfinder may place as scaffolding), `doors`.
Round-2 fixer: by default these pools are EXTENDED (03 §3 pool plus
acacia / jungle / mangrove / cherry / bamboo / crimson / warped planks and
logs / stems, dark oak log, orange / light blue / lime / cyan / purple /
pink / black / brown / magenta / light gray concrete and wool, sandstone, cut
sandstone, red sandstone, polished andesite / granite / diorite, deepslate
bricks / tiles, polished deepslate, mud bricks, quartz block, prismarine
bricks, dark prismarine, terracotta + 5 colours, smooth stone / sandstone,
7 more stained glass colours and tinted glass; walls 63, pillars 10, windows
15, roof 18, floor 18 entries). `MATERIAL_POOL=spec` restores the exact
03 §3 pools above. `scaffoldSafe` gains the extra planks (all of them are
in the movement.js scaffolding list); `gravity`, `refill`, `doors` are
unchanged; draw counts are unchanged.
`pickPalette(rng)` (exactly 7 draws) returns a coherent palette
{`walls`, `accent` (≠ walls), `pillar` (same wood as plank walls 70 %),
`window` (glass 50 %), `roof` (≠ walls), `floor` (≠ walls), `door` (wood of
the walls / roof), `wall_family`}; `pickMaterial(rng, pool)` draws once.
Non-full blocks (doors, torches) are ≤ 10 % of the placed blocks
(`non_full_fraction` in `state_metadata`; ~1-3 % in practice). Glass panes
and fences are never used (their connection state would change without a
logged event when a neighbour is placed).

Existing build episodes now draw their materials from the same pools and
record them in `state_metadata` (their logic is unchanged):

| type | `state_metadata` keys added | material choice |
|---|---|---|
| `buildHouse` | `materials` {floor, walls, roof}, `material_source` | `pickPalette` in the constructor (cobblestone is still given for the upstream scaffold helper) |
| `buildStructure` | `structure_type`, `materials` {<bot>: material}, `material_source` | wall / tower: first bot (name order) `palette.walls`, other bot `palette.accent`; platform: `palette.floor` |
| `buildTower` | `materials` {<bot>: material}, `material_source` | first bot `palette.walls`, other bot `palette.accent` |
| `towerBridge` | `materials` {tower, bridge}, `material_source` | tower: logs / concrete / wool; bridge: `scaffoldSafe` (the pathfinder only places scaffolding blocks; the default 64 stone is cleared from the inventory so the chosen material is used) |

The RNG sequence of these four types changed (palette draws in the
constructor; `buildStructure` no longer draws the block type in its build
phase); both bots stay in lock-step.
