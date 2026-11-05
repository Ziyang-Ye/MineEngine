## Round 2: world diversity (biomes, flat presets, time of day, weather)

Code: `controller/utils/biomes.js` (target biome per episode on normal
worlds), `controller/utils/site-prep.js` (ground finding, canopy fix,
optional levelled pad), `controller/utils/env-setup.js` (time / weather),
`generate_compose.py` (`FLAT_TERRAIN_OPTIONS`, `FLAT_TERRAIN_LAYERS`,
`flat_terrain_for_instance`, `generate_terrain_settings`) and the preview
helper `windows/biome_plan.py`.

Every choice is deterministic and never consumes the shared bot RNG:

| RNG seed | used for |
|---|---|
| `<bot_rng_seed>_env_<episode>` | time of day and weather (4 draws, always all four) |
| `<bot_rng_seed>_biome_<episode>` | search origin (2 draws) and the weighted order of target biomes |
| `<bot_rng_seed>_biome_<episode>_near_<biome>` | search origin near a remembered site for a biome whose earlier locate was slow (> 2 s) |
| sha256(`<level seed>|<instance>`) | flat-world preset (`generate_compose.py`) |

All world changes made here (`/spreadplayers`, `/tp`, `/fill`, `/weather`,
`/time`) happen in the teleport phase, i.e. before `worldStateBegin`, so they
are part of the start snapshot. `prepareSite` refuses to run once the
episode's world log is open (`_worldState` / `_episodeRecordingStarted`).

### Flat worlds (`generate_compose.py`)

The flat preset is a weighted, deterministic function of (level seed,
instance id). The level seed is `<instance><unix time>` unless `--world_seed`
is fixed, so presets change from batch to batch and between instances.
`FLAT_PRESET=<biome>[:<surface>]` forces one (tests). Every preset keeps 128
layers (bedrock 1 + 127), so the surface is still at y = 63 and players stand
at y = 64. On flat worlds the biome only changes colours (grass, foliage,
water, sky, fog) and what falls when it rains (rain / snow / nothing).
Flat worlds have no features, so a flat "forest" has no trees.

| biome | surface | layers under the surface (top first, then stone, bedrock) | weight |
|---|---|---|---|
| plains | grass_block | dirt x2 | 3 |
| sunflower_plains, meadow, flower_forest, cherry_grove, birch_forest, dark_forest, jungle, windswept_hills | grass_block | dirt x2 | 1 each |
| swamp | grass_block | dirt x2 | 1.5 |
| mangrove_swamp | grass_block | mud x2, dirt | 1 |
| old_growth_pine_taiga | podzol | dirt x2 | 1 |
| savanna | grass_block | coarse_dirt, dirt x2 | 1.5 |
| savanna_plateau | coarse_dirt | dirt x2 | 1 |
| desert | sand (x2) | sandstone x3 | 2 |
| beach | sand (x3) | sandstone x2 | 1 |
| badlands | red_sand | orange_terracotta, terracotta x3 | 1.5 |
| wooded_badlands | coarse_dirt | terracotta x2, yellow_terracotta, terracotta | 1 |
| eroded_badlands | orange_terracotta | white, plain, yellow, red terracotta | 1 |
| mushroom_fields | mycelium | dirt x3 | 1 |
| snowy_plains | snow (layer) | snow_block x2, dirt x2 | 2 |
| snowy_taiga | snow (layer) | grass_block, dirt x2 | 1 |
| ice_spikes | snow_block (x2) | packed_ice, dirt x2 | 1 |
| stony_shore | stone | andesite, gravel, stone | 0.5 |
| windswept_gravelly_hills | gravel | stone x2 | 0.5 |

`episode_info.world` (= `WORLD_GEN_INFO`): `flat_biome`, `flat_surface_block`
(unchanged keys) and `generator_settings` (full layer list + biome).

### Normal worlds: target biome per episode (`biomes.js`)

The leader (`Alpha`) picks the episode's target biome from a weighted list of
45 overworld surface biomes and locates it with

    execute positioned <x> 64 <z> run locate biome minecraft:<id>

from a seeded random origin within `max(teleport_radius, BIOME_ORIGIN_RADIUS
(2000))` of the teleport centre. Then `/spreadplayers` puts both bots next to
the located point (`INIT_MIN/MAX_BOTS_DISTANCE`; spreadplayers never places
players on fluids, so river and ocean-shore targets put the bots on the
bank / beach). Ocean targets (`warm_ocean`, `lukewarm_ocean`, `ocean`,
`cold_ocean`) locate the ocean, then `#minecraft:is_beach` from there (<= 400
blocks) and stand on that beach.

After the spread:
* each bot standing on leaves / logs / in a tree is moved down to the ground
  under it (`/tp`; spreadplayers itself uses the highest block, i.e. the
  canopy in forests and jungles);
* if the leader is not in the located biome (the located point is the biome
  sample nearest to the origin, usually on its border), loaded columns 16-48
  blocks around are probed with `execute if biome` and the bots are spread
  again there (`recentered`).

Fallbacks: up to `BIOME_MAX_ATTEMPTS` (4) candidates in the seeded weighted
order, `BIOME_LOCATE_TIMEOUT_MS` (12000) per locate and
`BIOME_LOCATE_BUDGET_MS` (30000) in total; a biome that was not found or timed
out twice is skipped for 10 episodes. If nothing is found, or the spread fails
at the located point and 32 / 64 blocks further into the biome, the old random
spreadplayers teleport is used (`biome_site_source = "random"`).

Weights (sum 108.5): plains 6; forest, desert 5; sunflower_plains,
birch_forest, dark_forest, taiga, swamp, savanna, beach, snowy_plains 4;
meadow, cherry_grove, flower_forest, jungle, mangrove_swamp, river, badlands,
snowy_taiga 3; old_growth_birch_forest, old_growth_pine_taiga,
old_growth_spruce_taiga, sparse_jungle, bamboo_jungle, stony_shore,
savanna_plateau, wooded_badlands, windswept_hills, ice_spikes, snowy_beach,
grove, snowy_slopes 2; windswept_forest, warm_ocean, lukewarm_ocean, ocean,
cold_ocean, windswept_savanna, eroded_badlands, windswept_gravelly_hills,
stony_peaks, frozen_river 1; mushroom_fields, frozen_peaks, jagged_peaks 0.5.

Episode classes may restrict the choice:

| static | example | meaning |
|---|---|---|
| `PREFERRED_BIOMES` | `["@water", "swamp"]` | only these biomes / groups (ids with or without `minecraft:`) |
| `AVOID_BIOMES` | `["@dry"]` | never these |
| `SITE_PREP` | `{level: true, radius: 3}` | level a pad around the bots' midpoint before setup (normal worlds) |

Groups: `@open`, `@trees`, `@water`, `@shore`, `@cold`, `@dry`, `@warm`,
`@temperate`, `@mountain`, `@rare`. A requested `snow` weather restricts the
candidates to cold biomes (unless the class list has none). `BIOME_WHITELIST`
(comma list, groups allowed) restricts globally; `BIOME_DIVERSITY=0` disables
the feature (old random teleport).

### Site preparation (`site-prep.js`)

`prepareSite(bot, rcon, args, center, {level, radius<=8, clearHeight=4,
fillDepth=2, fillWater=false, settleBots=true})`: finds the topmost ground
block of the centre column (solid, not leaves / logs / snow layer). A water
centre moves to the nearest land column (<= 8 blocks); with no land nearby
the pad is not built (`reason = "water_site"`). With `level` the leader runs
`/fill` to clear everything from ground+1 to ground+`clearHeight` (vegetation,
trunks, boulders, snow layers) and to fill air / water / lava / kelp /
seagrass / powder snow in the top `fillDepth`+1 layers (deepest first; filler
dirt, sandstone, terracotta, stone or snow_block below a matching natural
surface block). Counts come from the `/fill` replies. Afterwards both bots
are put back on the ground. Natural surroundings outside the pad stay; trees
cut by the pad may drop leaves later (`leaves_decay` events, logged).

### `episode_info.state_metadata` keys (both bots; handler keys win)

Time / weather (existing names kept):

| key | values |
|---|---|
| `time_of_day` | ticks 0-23999 set before the start snapshot. 85% day, 15% (`DUSK_NIGHT_FRACTION`) dusk / night / dawn split 42/42/16. The tick is jittered inside the band: day 2000-9999, dusk 12000-12999, night 15000-20999, dawn 22800-23599; `TIME_JITTER=0` uses 6000 / 12500 / 18000 / 23000. 1000 for eval episodes with `EVAL_TIME_SET_DAY=1`, `FIXED_TIME` when >= 0 |
| `time_of_day_label` | `day` (< 12000), `dusk` (12000-13799), `night` (13800-22199), `dawn` (>= 22200) |
| `time_policy` | `random`, `preferred` (class `PREFERRED_TIME`), `fixed`, `eval_day` |
| `weather` | the `/weather` command applied: `clear`, `rain`, `thunder` |
| `weather_requested` | New. `clear` 80%, `rain` 12%, `thunder` 3%, `snow` 5% (applied as `rain`; normal worlds then go to a cold biome). `WEATHER_WEIGHTS="clear=..,rain=..,thunder=..,snow=.."` changes the split, `FIXED_WEATHER` fixes it |
| `weather_policy` | New. `random`, `preferred` (class `PREFERRED_WEATHER` or the built-in per-type default: `igniteAndExtinguish`, `iceNearFire` -> `clear`, because rain puts out fire), `fixed`, `eval` (eval episodes are always clear) |
| `precipitation` | New. What actually falls at the episode site (round-2 fixer: computed from the episode-level `biome`, same on both bots; before: at each bot's feet): `none` (clear weather, or a dry biome: desert, savanna*, badlands*), `snow` (cold biome; height-adjusted temperature < 0.15, without the per-column noise), `rain`, `unknown` |
| `precipitation_biome` | New. biome used for `precipitation` |
| `rain_level`, `thunder_level` | New. target levels 0 / 1 (the server ramps over ~5 s; the leader waits 5.5 s after a weather change, and in the first episode, before setup) |
| `observed_rain_level`, `observed_thunder_level`, `observed_is_raining` | New. Mineflayer `rainState` / `thunderState` / `isRaining` at the start of recording |
| `daylight_cycle`, `weather_cycle`, `mob_spawning` | `false` (unchanged) |

Biome / site (normal worlds; flat worlds: `biome_site_source = "flat_world"`):

| key | values |
|---|---|
| `biome` | Round-2 fixer: the EPISODE biome, resolved once by the leader after setup at the episode site (the handler's server-resolved site biome, else one block above the world-state centre, else the leader's feet) and sent to the follower with the start-recording message, so both bots' episode_info files carry the same value. Server truth (`execute if biome`, includes the vanilla fuzzy biome zoom) when available. (Before: each bot's own feet right after the teleport, which disagreed in 7 of 23 normal-world episodes.) |
| `biome_source` | `rcon`, `client` or `flat_world` |
| `biome_client` | raw client-side biome at the same position (4x4x4 cell; may differ from `biome` near borders), or null |
| `biome_pos` | [x, y, z] where `biome` was resolved |
| `biome_scope` | `episode_site`, `leader_feet` (episode without a site centre), `bot_feet` (fallback: no leader value arrived; per-bot) |
| `biome_at_feet`, `biome_at_feet_source`, `biome_at_feet_client` | per-bot biome at this bot's feet right after the teleport (the old `biome` / `biome_source` / `biome_client`) |
| `biome_site_source` | `locate`, `locate_beach` (ocean target), `random` (fallback), `flat_world` |
| `biome_target` | first-choice biome of the plan |
| `biome_found` | biome actually located and used (a fallback when the target failed) |
| `biome_located` | biome of the located point (`beach` etc. for ocean targets) |
| `biome_locate_ms`, `biome_locate_attempts`, `biome_locate_distance` | total locate time, number of locate commands, distance of the located point from the search origin |
| `biome_site_xz` | located point [x, z] |
| `site_prepared`, `site_prep_center`, `site_prep_radius`, `site_prep_removed_blocks`, `site_prep_filled_blocks`, `site_ground_block`, `site_in_water`, `site_prep_moved_from_water`, `site_prep_reason` | only for classes with `SITE_PREP` |

The start snapshot also carries the truth: `episode_begin.world.storm`,
`thundering`, `time_of_day`, and the per-column `biome_palette` /
`biomes_rle` of the snapshot box.

### Weather side effects (all natural mechanics, logged by the plugin)

* snow: snow layers form on exposed tops while it snows (`block_form`
  `minecraft:snow[layers=1]`; on the `ice_spikes` flat preset ~1200 per
  episode), water freezes in cold biomes even in clear weather (`block_form`
  ice);
* rain: fire under the open sky goes out (`block_fade`), lava is unaffected;
* thunder: lightning can strike (1/100000 per chunk and tick where it
  rains). Lightning fire only happens on difficulty normal, i.e. damage
  episodes (`block_ignite` cause `LIGHTNING`); lightning damage on players is
  logged as `entity_damage` cause `LIGHTNING`. Skeleton-trap horses need
  `doMobSpawning`, which is off.
* NOT logged by the plugin (possible `replay_mismatch` if they happen in the
  box): rain filling cauldrons (`CauldronLevelChangeEvent`), farmland
  moisture (`MoistureChangeEvent`), copper de-oxidation and lightning-rod
  power from lightning. Cauldrons and farmland only exist in structures
  (villages, witch huts).

### Wiring (`controller/episodes-loop.js`)

How `episodes-loop.js` uses these modules:

1. Imports: `resolvePrecipitation`, `weatherObservation` from `env-setup`;
   `biomeSiteMetadata, chooseEpisodeSite, resolveBiome, teleportToSite` from
   `./utils/biomes`; `prepareSite, sitePrepMetadata` from `./utils/site-prep`.
2. `getOnTeleportPhaseFn`: compute `env = chooseEpisodeEnvironment(args,
   episodeNum, {evalDay, isEval: isEvalEpisode(ep), episodeClass:
   ep.constructor, episodeType: ep._episodeType})` BEFORE the teleport.
   In the leader branch, for non-eval episodes: `site = await
   chooseEpisodeSite(bot, rcon, args, episodeNum, ep.constructor,
   {weatherRequested: env.weather_requested})`; if `site`, `tp = await
   teleportToSite(bot, rcon, args, site, ep)`; call the old `teleport(...)`
   only when `!tp?.ok`. Store `ep._biomeSite = {...site, teleport_center:
   tp.center, biome_match: tp.biome_match, recentered: tp.recentered}`.
   Then, if `ep.constructor.SITE_PREP` and the world is not flat:
   `ep._sitePrep = sitePrepMetadata(await prepareSite(bot, rcon, args,
   midpointOfBots, {...SITE_PREP, episodeInstance: ep}))`.
   `applyEpisodeEnvironment` stays where it is (after the teleport).
3. The leader's `sendToOtherBot("postTeleportPhase", {...})` at the end of the
   teleport phase carries `{biomeSite: ep._biomeSite, sitePrep: ep._sitePrep}`.
4. `getOnPostTeleportPhaseFn` handler takes `(phaseDataOther)`; with
   `site = ep._biomeSite || phaseDataOther?.biomeSite` it adds
   `biomeSiteMetadata(site, {biome, biome_source, biome_client})` from
   `resolveBiome(bot, rcon, feet, [hint])`, the site-prep keys and
   `resolvePrecipitation(ep._envMetadata, biome, y)` to `_envMetadata`
   (flat worlds: `biome_site_source: "flat_world"`, biome hint
   `world.flat_biome`). IMPORTANT: this must run as a background promise
   (`ep._biomeMetaPromise = (async () => {...})().catch(...)`): the handler
   must not await anything before it registers the `setupEpisodePhase`
   listener, otherwise the peer's `setupEpisodePhase` message arrives with no
   listener and both bots hang (seen in testing). The episode
   loop awaits `Promise.race([ep._biomeMetaPromise, sleep(5000)])` right
   before `saveEpisodeInfo`.
5. `getOnStartRecordingFn`: `addEnvMetadata(ep, weatherObservation(bot))`
   right before `bot.emit("startepisode")`.
