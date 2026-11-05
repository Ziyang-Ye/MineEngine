## Round 2 – integration: episode registry, categories, sampling, world / skin wiring

Code: `controller/episodes-loop.js` (registry, aliases, eligibility, sampling,
biome / site-prep / environment / skin wiring, random-tick policy, RNG seed
salt, `state_metadata` merge), `controller/utils/episode-weights.js` (typical
lengths, categories, category-first sampler), `controller/config/args.js` and
`controller/entrypoint.sh` (new options), `windows/run_windows.py`
(`--smoke-test`, `--mode` help), `run_laptop.sh` (mode aliases).

### Episode types

45 registered types: 14 upstream training types, 5 round-1 stateful types,
19 round-2 types (`controller/episode-handlers/diverse/`) and 7 eval types.

| type | category | typical length (s) | flat | normal | damage | line |
|---|---|---|---|---|---|---|
| `buildHouse` | build | 140 (fixer; was 120; sampling multiplier 0.5) | yes | yes | no | upstream |
| `buildStructure` | build | 15 | yes | yes | no | upstream |
| `buildTower` | build | 18 (fixer; was 12) | yes | yes | no | upstream |
| `towerBridge` | build | 37 (fixer; was 22) | yes | yes | no | upstream |
| `buildAndBreak` | build | 60 | yes | yes | no | round 1 |
| `coBuild` | build | 105 | yes | yes | no | r2 build-destroy |
| `forestBuild` | build | 80 | **no** (`REQUIRES_NON_FLAT_WORLD`) | yes | no | r2 build-destroy |
| `mine` | destroy | 10 | yes | yes | no | upstream |
| `placeAndMine` | destroy | 120 | yes | yes | no | upstream |
| `tntBlast` | destroy | 30 | yes | no | no | round 1 |
| `buildVsBreak` | destroy | 50 | yes | yes | no | r2 build-destroy |
| `digDown` | destroy | 60 | yes | yes | no | r2 build-destroy |
| `tunnel` | destroy | 70 | yes | yes | no | r2 build-destroy |
| `crater` | destroy | 80 | yes | yes | no | r2 build-destroy |
| `tntDemolish` | destroy | 50 | yes | yes | no | r2 build-destroy |
| `pvp` | fight | 15 | yes | yes | yes | upstream |
| `pve` | fight | 60 | yes | yes | yes | upstream |
| `chase` | fight | 10 | yes | yes | no | upstream |
| `pvpMelee` | fight | 45 | yes | yes | yes | r2 combat-creatures |
| `pvpRanged` | fight | 50 | yes | yes | yes | r2 combat-creatures |
| `pveMobs` | fight | 60 | yes | yes | yes | r2 combat-creatures |
| `walkLook`, `walkLookAway`, `orbit` | move | 60 | yes | yes | no | upstream |
| `straightLineWalk` | move | 20 | yes | yes | no | upstream |
| `collector` | move | 150 | yes | yes | no | upstream |
| `rideMount` | move | 60 | yes | yes | no | r2 combat-creatures |
| `igniteAndExtinguish` | interaction | 40 | yes | no | no | round 1 |
| `iceNearFire` | interaction | 45 | yes | no | no | round 1 |
| `lavaWater` | interaction | 40 | yes | no | no | round 1 |
| `itemExchange` | interaction | 50 | yes | yes | no | r2 combat-creatures |
| `animalCare` | interaction | 70 | yes | yes | no | r2 combat-creatures |
| `farming` | interaction | 40 | yes | yes | no | r2 mechanics |
| `redstoneDoors` | interaction | 40 | yes | yes | no | r2 mechanics |
| `fallingBlocks` | interaction | 35 | yes | yes | no | r2 mechanics |
| `lightAndFire` | interaction | 60 | yes | yes | no | r2 mechanics |
| `waterWorks` | interaction | 55 | yes | yes | no | r2 mechanics |
| `snowIce` | interaction | 45 | yes | yes | no | r2 mechanics |

Eval types (`structureEval`, `translationEval`, `bothLookAwayEval`,
`oneLooksAwayEval`, `rotationEval`, `turnToLookEval`,
`turnToLookOppositeEval`) have no category and are never sampled by the
category sampler. "damage" = `isDamageEpisode` (difficulty normal, no
resistance; `DAMAGE_EPISODE_TYPES` in env-setup.js or `static DAMAGE_ENABLED`).

World eligibility: flat worlds run every listed type except
`REQUIRES_NON_FLAT_WORLD` ones (`forestBuild`); normal worlds run the types
with `static WORKS_IN_NON_FLAT_WORLD = true`. An unknown type key in
`EPISODE_TYPES` now aborts the controller at start-up with the list of valid
keys and aliases.

### Categories (03 section 4, extended)

| category (alias) | share | members |
|---|---|---|
| `build` | 35 % | buildHouse, buildStructure, buildTower, towerBridge, coBuild, forestBuild, buildAndBreak |
| `destroy` (`dig`) | 20 % | mine, placeAndMine, buildVsBreak, digDown, tunnel, crater, tntDemolish, tntBlast |
| `fight` | 25 % | pvp, pvpMelee, pvpRanged, pve, pveMobs, chase |
| `move` (`observe`) | 10 % | walkLook, walkLookAway, orbit, straightLineWalk, collector, **rideMount** |
| `interaction` (`physics`) | 10 % | igniteAndExtinguish, iceNearFire, lavaWater, itemExchange, **animalCare, farming, redstoneDoors, fallingBlocks, lightAndFire, waterWorks, snowIce** |

Membership of types that 03 section 4 does not list: `pveMobs` (hostile
mobs) is in `fight` as 03 says; `rideMount` (horses, donkeys, mules, camels,
pigs, boats, minecarts) is locomotion with both players in view and goes to
`move`; `animalCare` (passive / neutral animals: breed, milk, shear, lead,
tame, brush, eggs) joins `itemExchange` (villagers, wandering traders) in the
03 "physics and fluids" category, renamed `interaction`; the six mechanics
types (block-state mechanics, gravity, fluids, fire, snow / ice) also go
there, next to igniteAndExtinguish / iceNearFire / lavaWater. Hostile,
passive and neutral creatures therefore appear in `fight` (pve, pveMobs),
`move` (rideMount) and `interaction` (animalCare, itemExchange).

The category shares are shares of **episodes**. Categories without an
eligible type in the current world are dropped and the remaining shares are
renormalised. Inside a category a type is drawn with weight
`multiplier / sqrt(typical length)` (`EPISODE_WEIGHT_MULTIPLIERS`;
`uniform_weights=1` gives every type weight `multiplier`).

Resulting per-episode probabilities (default weights, from
`categoryPlan`; checked against 20000 seeded draws, max deviation 0.4 pp):

| world | build | destroy | fight | move | interaction | mean length | time share build / destroy / fight / move / interaction |
|---|---|---|---|---|---|---|---|
| flat (37 types) | buildTower 9.4, buildStructure 8.4, towerBridge 6.9, buildAndBreak 4.2, coBuild 3.2, buildHouse 3.0 | mine 5.1, tntBlast 3.0, buildVsBreak 2.3, tntDemolish 2.3, digDown 2.1, tunnel 1.9, crater 1.8, placeAndMine 1.5 | chase 7.0, pvp 5.8, pvpMelee 3.3, pvpRanged 3.2, pve 2.9, pveMobs 2.9 | straightLineWalk 2.7, orbit / walkLook / walkLookAway / rideMount 1.6 each, collector 1.0 | fallingBlocks 1.0, farming / redstoneDoors / igniteAndExtinguish / lavaWater 1.0, iceNearFire / snowIce 0.9, itemExchange 0.9, waterWorks 0.8, lightAndFire 0.8, animalCare 0.7 | 41 s | 32 / 23 / 20 / 14 / 11 % |
| normal (34 types) | buildTower 8.5, buildStructure 7.6, towerBridge 6.3, buildAndBreak 3.8, forestBuild 3.3, coBuild 2.9, buildHouse 2.7 | mine 6.0, buildVsBreak 2.7, tntDemolish 2.7, digDown 2.5, tunnel 2.3, crater 2.1, placeAndMine 1.7 | as flat | as flat | fallingBlocks 1.5, farming / redstoneDoors 1.4, snowIce 1.3, itemExchange 1.2, waterWorks 1.2, lightAndFire 1.1, animalCare 1.0 | 44 s | 34 / 23 / 19 / 13 / 11 % |

(Round-2 fixer: buildTower / towerBridge / buildHouse typical lengths are
now 18 / 37 / 140 s and buildHouse has a built-in multiplier 0.5, so their probabilities inside `build` are slightly
lower than in this table; `CATEGORY_SHARE_MODE=time` makes the shares time
shares, see r2-fixer.md.)

`CATEGORY_WEIGHTS` / `--category_weights` (e.g. `interaction=0.2,build=0.3`;
aliases allowed; `other=<share>` adds uncategorised types) overrides the
shares; they are renormalised.

### Modes and aliases (`EPISODE_TYPES`, `--mode`)

| token | types | sampling |
|---|---|---|
| `all` (default) | every registered type (evals skipped when sampling) | weighted, 1/sqrt(length) |
| `diverse` | every training type | **category first**, then type |
| `build`, `destroy`/`dig`, `fight`, `move`/`observe`, `interaction`/`physics` | the category | category first (shares renormalised over the given categories) |
| `stateful` | the 5 round-1 types | weighted |
| `upstream` | the 14 upstream training types | weighted |
| `new` / `round2` | the 19 round-2 types | weighted |
| `mechanics` | farming, redstoneDoors, fallingBlocks, lightAndFire, waterWorks, snowIce | weighted |
| `creatures` | pveMobs, animalCare, rideMount, itemExchange | weighted |
| `combat` | pvpMelee, pvpRanged, pveMobs | weighted |
| `buildDestroy` | the 7 round-2 build / destroy types | weighted |
| `mix` (run_laptop / run_windows) | `all` with the 5 stateful types x3 | weighted |

Tokens can be combined (`diverse` wins: any category alias or `diverse`
switches on category sampling; `CATEGORY_SAMPLING=1|0` forces it). With
`SMOKE_TEST=1` (`run_windows.py --smoke-test`) the controllers cycle through
the eligible types alphabetically instead of sampling.

Shared-RNG use: the weighted sampler draws once, the category sampler always
twice (category, type), identically on both bots, before the episode class
is constructed.

### Per-episode sequence (normal and flat worlds)

1. Type selection (above), then **skins** (`utils/skins.js`
   `assignEpisodeSkins`, both bots; the leader applies both, each bot waits
   for its texture; separate RNG `<skin_seed>_skin_<block>`, never throws).
2. Teleport phase: `chooseEpisodeEnvironment(args, ep, {evalDay, isEval,
   episodeClass, episodeType})` (time / weather incl. `PREFERRED_TIME` /
   `PREFERRED_WEATHER`) **before** the teleport; the leader (Alpha) then
   - on normal worlds (non-eval): `chooseEpisodeSite` (target biome, see
     r2-world-diversity) + `teleportToSite`; the old random
     `spreadplayers` teleport runs only if that fails;
   - if the class has `static SITE_PREP` (normal worlds): `prepareSite` at
     the bots' midpoint;
   - `applyEpisodeEnvironment` (gamerules, time, weather, hostile clean-up
     for damage episodes);
   - `gamerule randomTickSpeed <base>` (below).
3. Post-teleport: both bots resolve the real biome at their feet
   (`resolveBiome`, server truth), the site / failure metadata and
   `precipitation`, as a background task (awaited, max 5 s, before
   `episode_info` is written). The leader's site travels to the follower in
   both the "teleportPhase end" and "postTeleportPhase beginning" messages.
4. Setup, then start recording: the leader opens the world log
   (`worldStateBegin`) and writes a `note` `{"phase": "skins", "Alpha":
   "<skin_id>:<model>", "Bravo": ...}`; both bots add the observed weather
   (`weatherObservation`).

Integration biome preferences (used when the class declares none):
`forestBuild` prefers forest, flower_forest, birch_forest, windswept_forest,
taiga (simple 3-7 log oak / birch / small spruce trunks; round-2 fixer:
plains dropped, taiga added); `snowIce` avoids `@dry`
(the snow golem melts in deserts, savannas, badlands); `tntDemolish` avoids
`@trees` and `@mountain` (needs a level tree-free site).

### Random ticks

Random ticks change blocks without a logged Bukkit event (kelp / cave-vine
heads, sugar cane / cactus / sapling age, farmland moisture), which breaks
the exact replay (`replay_mismatch`) in water / jungle / desert sites. The
leader therefore sets the base `randomTickSpeed` in the teleport phase
(before the start snapshot):

* Round-2 fixer: default `vanilla` (= 3, also for an empty value). The
  plugin's periodic box diff (`block_state` records with source `box_diff`,
  every 20 ticks, plus a final diff) records the random-tick changes that
  fire no Bukkit event, so the replay stays exact and natural dynamics (crop
  and sapling growth, leaf decay, grass spread, snow / ice melt) stay on;
  their timing is known to 20 ticks (`baseline_game_time`,
  `observed_game_time`). `episode_end.box_diff.records` counts them;
* `EPISODE_RANDOM_TICK_SPEED=auto` (the round-2 default): `0` in clear
  weather, vanilla `3` when it rains / snows (only causally typed events, few
  `box_diff` records);
* a number: that value; `-1`: leave the gamerule alone;
* a class may set `static RANDOM_TICK_SPEED`;
* `iceNearFire` and `forestBuild` raise it themselves in setup (100) and reset
  it to 3 in teardown; the next episode sets its base again.

### RNG seed salt

All per-episode seeds (`<bot_rng_seed>_<episode>` shared RNG, `_teleport_`,
`_env_`, `_biome_`, skins) use `bot_rng_seed = <BOT_RNG_SEED>_<WORLD_SEED_CONFIG>`
(the level seed, `<instance><unix time>` per batch unless `--world-seed` is
fixed). Before, `BOT_RNG_SEED` was always 12345 and episode numbers restart
every batch, so every batch repeated the same plans, materials, times and
skins. `BOT_RNG_SEED_SALT=0` restores the plain seed. Reproduce an episode
with the same world seed and `BOT_RNG_SEED`.

### Options (controller CLI / environment)

| CLI (`args.js`) | env | default | meaning |
|---|---|---|---|
| `--bot_rng_seed_salt` | `BOT_RNG_SEED_SALT` | `1` | salt the bot RNG seed with the world seed |
| `--category_weights` | `CATEGORY_WEIGHTS` | 03 shares | category shares |
| `--category_sampling` | `CATEGORY_SAMPLING` | auto | force category sampling on (1) / off (0) |
| `--episode_random_tick_speed` | `EPISODE_RANDOM_TICK_SPEED` | `vanilla` (3; round 2: `auto`) | base randomTickSpeed (`vanilla`, `auto`, a number, `-1`) |

The entrypoint passes them as empty strings when unset; an empty CLI value
falls back to the environment variable (so `run_windows.py`, whose
controller flags come from the compose environment, can set them through
its own process environment). The world-diversity (`BIOME_*`,
`FIXED_WEATHER`, `WEATHER_WEIGHTS`, `TIME_JITTER`, `FLAT_PRESET`) and skin
(`SKIN_*`) variables are read from the environment by their modules.

### `episode_info.state_metadata` (added by the integration)

`state_metadata` = environment keys (`_envMetadata`: time / weather / damage
policy / biome / site / skins / sampling) merged with the handler's
`_stateMetadata`. Handler keys win on conflicts; the environment value a
handler replaced is kept in `env_shadowed.<key>` (e.g. a handler's own
client-side `biome` vs the server-resolved one), so no key is lost.

| key | meaning |
|---|---|
| `episode_category` | `build`, `destroy`, `fight`, `move`, `interaction`, or null (eval / uncategorised) |
| `episode_sampling` | `category`, `weighted` or `smoke_test` |
| `episode_category_share`, `episode_type_probability` | category share and per-episode probability of this type (category sampling only) |
| `bot_rng_seed` | effective (salted) bot RNG seed |
| `random_tick_speed_base` | randomTickSpeed set before the start snapshot (null = untouched); handlers may raise it (`random_tick_speed` in their own keys) |
| `biome_target`, `biome_found`, `biome_located`, `biome_site_source`, `biome_locate_*`, `biome_site_xz` | see r2-world-diversity; `biome_site_source` = `flat_world` on flat worlds |
| `biome_site_failure` | normal worlds, random-teleport fallback: `not_located` or `spread_failed` (`biome_target` is still the planned target) |
| `biome_match`, `biome_recentered` | the leader stood in the located biome after the spread / was re-spread into it |
| `biome`, `biome_source`, `biome_client`, `biome_pos`, `biome_scope`, `biome_at_feet*`, `precipitation`, `precipitation_biome`, `observed_*` | see r2-world-diversity (round-2 fixer: `biome` is the episode biome, the same on both bots) |
| `ambient_fauna` | background animals, see r2-fixer.md |
| `skins`, `skin_assignment` | see r2-skins |
| `env_shadowed` | environment values replaced by handler keys |
