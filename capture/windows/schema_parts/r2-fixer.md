## Round 2 fixer: handshakes, episode biome, clean-up, observer layer, ambient fauna

Follow-up fixes to round 2. New / changed metadata keys are
listed per item; everything else keeps the round-2 format.

### 1. Leader -> follower site handshake (creature and diverse bases)

`controller/utils/handshake.js` (`exchange`, `reviveVec3`, `toPlain`):
barrier exchanges over the bot coordinator (same pattern as
mechanics-common's `mech_site` / `mech_ready`).

* `CreatureEpisodeBase.setupEpisode` (pvpMelee, pvpRanged, pveMobs,
  animalCare, rideMount, itemExchange): `cre_site` (site, flat, server
  biome, client biome, water nearby; the follower adopts the leader's) ->
  plan -> `cre_plan` (the follower adopts the leader's arena flatness, and
  is done reading before any edit) -> leader `/fill` + summons of terrain ->
  `cre_ready` -> teleports.
* `DiverseEpisodeBase.setupEpisode` (coBuild, buildVsBreak, digDown, tunnel,
  crater, tntDemolish, forestBuild): `div_site` (the follower adopts the
  leader's whole `chooseSite` result, Vec3s revived; forestBuild groves
  included) -> plan -> `div_plan` (stand columns compared, log only) ->
  leader edits -> `div_ready`.
* Timeouts 25-30 s; on a timeout the bot keeps its own result.

| key | types | meaning |
|---|---|---|
| `site_handshake` | creature + diverse types | `leader`, `adopted` (follower used the leader's site) or `own_timeout` |
| `plan_agrees_with_other_bot` | diverse types | follower's stand columns equal the leader's (null on timeout) |

### 2. Episode biome (same on both bots)

episodes-loop.js: after `setupEpisode` the leader resolves the biome once at
the episode site (a creature handler's server-resolved site biome, else
`execute if biome` one block above `_worldStateCenter`, else at its feet)
and sends it with the start-recording message. Both bots write `biome`,
`biome_source`, `biome_client`, `biome_pos`, `biome_scope`, and
`precipitation` / `precipitation_biome` computed from that biome. The
per-bot feet values moved to `biome_at_feet`, `biome_at_feet_source`,
`biome_at_feet_client` (see r2-world-diversity). Creature handlers use the
leader's server biome for their pools (`biome`; client value
`biome_client`); mechanics handlers' client biome is now `biome_client`
(it used to shadow `biome`).

### 3. Leftover entities

`cleanupEpisodeEntities` (episodes-loop.js, leader):

* teleport phase, before the teleport: `kill @e[tag=solaris_r2]`, every
  loaded dropped item and arrow; after the teleport: tagged entities and
  items / arrows / XP orbs within 64 blocks of the new position;
* after `worldStateEnd`: tagged entities, and for creature episodes
  (`static CLEANUP_ENTITIES_RADIUS = 32`) every non-player entity within
  32 blocks of the site except item frames, paintings, armour stands,
  markers and display / interaction entities (bred babies, chicks, vexes).

Killed animals drop their loot; those items are removed by the next
teleport-phase clean-up, before the next start snapshot.

### 4. Spawn point

After every `setupEpisode` each bot sets its spawn point to its start
position (`spawnpoint <bot> x y z`), so a death in any damage episode
(including upstream `pvp` / `pve`) respawns the bot in the current arena,
not at an old one thousands of blocks away.

### 5. Random tick speed default and the mechanics weather override

* `EPISODE_RANDOM_TICK_SPEED` defaults to `vanilla` (3) (env-setup.js
  `randomTickSpeedFor`); `auto` keeps the round-2 policy (see
  r2-integration "Random ticks").
* mechanicsEpisodes that switch the weather in setup (PREFERRED_WEATHER) now
  update `bot._lastAppliedWeather` (the next episode waits for the rain ramp
  correctly) and, when the tick-speed policy depends on the weather (`auto`),
  re-set the gamerule before the snapshot: `random_tick_speed_base` (new
  value), `random_tick_speed_base_before_weather`.
* forestBuild: sites with random-tick-risk blocks (sugar cane, cactus,
  farmland, saplings, ...) are no longer skipped (box_diff records them);
  `FOREST_SKIP_RANDOM_TICK_RISK=1` restores the skip.

### 6. Biome locate on a dedicated RCON connection

* The leader opens a second RCON connection (60 s packet timeout) for
  `/locate biome` (`getLocateRcon`); the shared connection (10 s timeout)
  is no longer blocked by a slow search.
* utils/biomes.js: per-locate wait 25 s (`BIOME_LOCATE_TIMEOUT_MS`), total
  budget 28 s (`BIOME_LOCATE_BUDGET_MS`); after a timeout the server is
  drained (`list` with a 60 s timeout) before the next locate and the
  timeout is NOT counted as a miss (only "not found" is); after a locate
  slower than 2 s the leader pauses 1.5 s (`BIOME_LOCATE_BREATHER_MS`) so
  the server ticks (and sends packets to camera clients) between two
  main-thread stalls.

### 7. Observer layer for the upstream build types (03 section 5 / T8)

`controller/episode-handlers/partner-gaze.js` (`takeTurns`,
`watchPartnerUntil`, `glanceAtPartner`, `buildPhaseWithGlances`).

| type | change | new `state_metadata` keys |
|---|---|---|
| `buildTower` | turn-based: one bot builds its tower while the other watches it, then they swap (one shared RNG draw: who starts). `BUILD_TOWER_MODE=simultaneous` restores the upstream behaviour | `tower_height`, `build_mode` (`turns`/`simultaneous`), `first_builder`, `observer_distance` |
| `towerBridge` | towers turn-based, then bridges turn-based (the first bridger goes to the midpoint while the other watches from its tower, then the other closes the gap; 35 s per bridge turn). Two shared draws. `TOWER_BRIDGE_MODE=simultaneous` restores it | `build_mode`, `first_tower_builder`, `first_bridger`, `observer_distance` |
| `buildHouse` | the halves are built in chunks of 4 blocks with a 0.8-1.2 s partner glance in between; after each phase (floor, walls, roof) the faster bot watches the other until it is done (barrier, 40 s). `BUILD_HOUSE_GAZE=0` restores it. Measured with 3-block chunks / 1.0-1.6 s glances: partner in view 26-35 % (was 13-26 %) but 165-185 s (was ~125 s), hence the shorter glances and the built-in sampling multiplier 0.5 (episode-weights.js `DEFAULT_TYPE_MULTIPLIERS`; `EPISODE_WEIGHT_MULTIPLIERS=buildHouse=1` undoes it). Still below the 40 % target | `gaze_layer` {glance_every_blocks, glance_ms, phase_barrier} |
| `buildStructure` | chunks of 2 blocks with 0.6-1.0 s glances; the faster bot watches the other until it is done (25 s). `BUILD_STRUCTURE_GAZE=0` restores it | - |

Creature / build-destroy types: pveMobs partner glances every 1.5-3.2 s
(0.8-1.3 s; was every 3-6 s), tntDemolish wall-building glance probability
0.5 (was 0.3) and a partner glance after the blast; rideMount: riders look at
the partner 85 % of the time (was 75 %), otherwise at the partner when it is
within +-60 deg of the travel direction; boats start at opposite ends of the
pool and keep the same speed (half a lap apart); camel double ride 3-5 s
(60 %; was 6-10 s, 75 %). Creature `outroStep` and pveMobs regrouping:
`keepApart` (step back facing the partner when it is closer than 2.5
blocks; pveMobs bots ended episodes ~1.1 blocks apart). `ride_end` notes carry `wall_skips` /
`ground_check_off` (a rider that saw only "walls" used to stand still for the
whole ride; the ground check is now switched off after one round of skips).

### 8. Ambient fauna (new, `controller/utils/ambient-fauna.js`)

With probability `AMBIENT_FAUNA_PROB` (default 0.35; 0 disables) a
non-damage, non-creature, non-eval episode gets a herd of 2-6 passive /
neutral animals (1-2 species from the creatures.js `ANIMALS` pools, biome
weighted; biome specialists such as mooshroom, panda, ocelot, parrot, frog,
turtle, camel, armadillo only in their own biome groups; goats and bees
never; farming episodes: no rabbits, chickens, pigs; ~20 % babies) 12-26
blocks from the episode site. The leader summons them after setup, before
the start snapshot (tag `solaris_r2`, removed after the episode). Separate
RNG `<bot_rng_seed>_fauna_<episode>`: the shared bot RNG is not used, plans
do not change. Their AI acts freely during the episode (grazing, wolves
hunting, eggs); everything is in the world log (entities every tick, block
changes such as grass eaten by sheep).

| key | meaning |
|---|---|
| `ambient_fauna` | null (not eligible), `{enabled: false, probability}` or `{enabled: true, probability, species [ids], herd_distance, herd_center [x, z], requested, summoned, animals [{type, baby, variant, pos [x,y,z] or null, ok}]}` (same on both bots) |

### 9. Category time shares (`CATEGORY_SHARE_MODE`)

`CATEGORY_SHARE_MODE=time` (env, category sampling only): the category
shares (03 section 4 / `CATEGORY_WEIGHTS`) are shares of the recorded TIME:
each category's episode share is divided by its expected episode length
(typical lengths of its types weighted by their in-category probabilities)
and renormalised. Default `episodes` (round-2 behaviour: shares of the
episodes). The controller log prints both (`build=35.0% (time 35.5%)`).
With the default flat-world types: episodes mode gives time shares build
35.5 / destroy 21.9 / fight 18.6 / move 13.3 / interaction 10.6 %; time mode
gives episode shares 33.4 / 17.7 / 32.6 / 7.3 / 9.1 % and time shares
exactly 35 / 20 / 25 / 10 / 10 %. Typical lengths updated: buildTower 18 s,
towerBridge 37 s (turn-based now), buildHouse 140 s (glances). Built-in
multiplier buildHouse x0.5 (`DEFAULT_TYPE_MULTIPLIERS`, all sampling
modes; an `EPISODE_WEIGHT_MULTIPLIERS` entry for the type overrides it):
flat-world probability buildHouse 1.6 % (was 3.0 %).

### 10a. Per-episode biome on flat worlds (`utils/flat-biome.js`)

A flat world has one preset biome per world. With probability
`FLAT_BIOME_SWAP_PROB` (default 0.5; 0 disables) the leader runs, after the
teleport and before setup / the start snapshot,
`fillbiome <x-64> <y-16> <z-64> <x+63> <y+47> <z+63> minecraft:<biome>`
around the bots' midpoint (split into 4 quadrants if refused). The biome is
planned like a normal-world target (biomes.js `planEpisodeBiomes` with the
class / integration PREFERRED_BIOMES / AVOID_BIOMES; requested snow -> a
cold biome); river / beach / shore / ocean biomes are skipped. Biomes are
chunk data: the world log replay is unaffected; grass / foliage / water /
sky colours, rain vs snow and the creature pools change. Separate RNG
`<bot_rng_seed>_flatbiome_<episode>` (+ the planner's `_biome_` RNG).

| key | meaning |
|---|---|
| `flat_biome_swap` | flat worlds only: `{enabled: false, probability}` or `{enabled: true, probability, biome, region [x1,y1,z1,x2,y2,z2], ok, quadrants?}` (same on both bots). The episode-level `biome` (resolved at the site after setup) shows the result |

### 10. Materials (T7, diversity)

The materials.js pools are extended by default (more woods, stone
families, terracotta, colours; see r2-build-destroy T7); `MATERIAL_POOL=spec`
restores the 03 §3 pools.

### 11. Other

* Upstream `pve`: zombies and skeletons get an unbreakable helmet (leather /
  chainmail / iron / gold, random) so they do not burn by day; all its mobs
  carry the `solaris_r2` tags (removed after the episode).
* `pveMobs` test hook `PVE_MOBS_FORCE` (r2-combat-creatures); `forced_mobs`
  key.
* animalCare: turtles are reachable (`feed_baby` with seagrass).
* forestBuild biome preference: plains dropped, taiga added.
* Skins: the 2 `legacy` skins are drawn only when `SKIN_SOURCES` lists them
  (default `default,procedural`).
* `windows/schema_parts/r2-combat-creatures.md`: schema of the six combat /
  creature types.
