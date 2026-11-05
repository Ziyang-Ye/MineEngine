## Round 2: combat and creature episodes (`controller/episode-handlers/diverse/`)

Six episode types with real combat, hostile / passive / neutral creatures,
mounts and vehicles, item hand-over and trading.

| type key | class | file | category | damage | typical length (s) |
|---|---|---|---|---|---|
| `pvpMelee` | `PvpMeleeEpisode` | `pvp-melee-episode.js` | fight | yes | 45 |
| `pvpRanged` | `PvpRangedEpisode` | `pvp-ranged-episode.js` | fight | yes | 50 |
| `pveMobs` | `PveMobsEpisode` | `pve-mobs-episode.js` | fight | yes | 60 |
| `rideMount` | `RideMountEpisode` | `ride-mount-episode.js` | move | no | 60 |
| `itemExchange` | `ItemExchangeEpisode` | `item-exchange-episode.js` | interaction | no | 50 |
| `animalCare` | `AnimalCareEpisode` | `animal-care-episode.js` | interaction | no | 70 |

Shared base: `CreatureEpisodeBase` (`creature-common.js`, extends
`BaseEpisode`); pools and summon NBT: `controller/utils/creatures.js`. All six
are `WORKS_IN_NON_FLAT_WORLD = true`. "damage" = `static DAMAGE_ENABLED =
true` (env-setup.js damage policy: difficulty normal, no resistance /
saturation, `keepInventory` and `doImmediateRespawn` stay on, so a killed bot
respawns at once with its gear).

### Setup (all six, before the start snapshot)

1. **Site handshake** (round-2 fixer). Both bots search a flat 7x7 core
   (`findFlatSite`, 14 blocks around their midpoint; else the ground at the
   midpoint) on UNEDITED terrain and read the client biome and whether water
   is within 16 blocks. The leader also resolves the real gameplay biome at
   the site over RCON (`execute if biome`, utils/biomes.js `resolveBiome`).
   Handshake `cre_site` (a barrier) sends the leader's facts; the follower
   adopts them (`site_handshake`). Then both plan with the shared RNG
   (identical draws), measure the arena flatness, and handshake `cre_plan`
   (the follower adopts the leader's `arena_flat_fraction`). Only then does
   the leader edit the world; handshake `cre_ready` makes the follower wait
   for the edits before it reads stand heights and teleports. (Before the
   fix each bot read the world on its own while the leader was already
   levelling, so plans could differ.)
2. **Arena.** The leader kills leftovers tagged `solaris_r2` within 96
   blocks and items / arrows within 48, then either clears plants on the
   flat core or levels a square of half size `levelled_half_size`
   (`fill` air above, the site's top block, dirt below; snow layers kept)
   when the site is not flat or less than 85 % of the arena columns are
   flat. Episode-specific terrain (`terrainCommands`: boat pool, rails,
   lever) follows.
3. Both bots `tp` to their stand columns, get items (`give`, enchanted /
   component items as raw item strings) and armour / shield
   (`item replace entity ... armor.* / weapon.offhand`). Damage types set the
   bot's spawn point to its stand (episodes-loop now also sets the spawn
   point after every episode's setup).
4. The leader summons the creatures / vehicles LAST (`summon` with
   `PersistenceRequired:1b` and the tag `solaris_r2` plus a role tag
   `solaris_r2_hostile` / `_ally` / `_animal` / `_vehicle` / `_trader`, mob
   index tags `solaris_r2_mob_<i>`). `summon_ok` / `summon_failed` count the
   replies (leader's episode_info only).

During recording only the bots' actions and game mechanics change the world
(attacks, use / place / break, mount / dismount, AI, breeding, explosions,
arrows). Every step has a timeout; a failing step is logged and the phase
chain continues. Runtime jitter uses `Math.random`; plans use only the
shared RNG with fixed draw counts.

**Leftover entities** (round-2 fixer). After the episode (world log closed)
the leader removes every loaded `solaris_r2` entity and every non-player
entity within `CLEANUP_ENTITIES_RADIUS` = 32 blocks of the site except
item frames, paintings, armour stands, markers and display / interaction
entities (bred babies, chicks from eggs, vexes, untagged drops). The next
episode's teleport phase kills tagged entities, items and arrows again,
before and after the teleport.

### Common `state_metadata` keys

| key | meaning |
|---|---|
| `episode` | handler class name |
| `site` [x,y,z] | ground block at the arena centre (feet level y+1) |
| `axis`, `side` | layout frame (u axis `x`/`z` and its sign towards the actor) |
| `actor`, `observer` | bot names (roles: who acts first / rides / feeds) |
| `biome` | leader's server-resolved biome at the site (same on both bots, same as the episode-level `biome`, see r2-fixer.md); the creature pools are weighted by its group |
| `biome_source` | `rcon` (server) or `client` (fallback) |
| `biome_client` | the leader's client-side biome at the site (raw 4x4x4 cell; differs near borders) |
| `biome_group` | creatures.js group: `desert`, `badlands`, `savanna`, `jungle`, `swamp`, `snowy`, `taiga`, `mountain`, `forest`, `plains`, `ocean`, `river`, `beach`, `mushroom`, `cave`, `other` |
| `site_handshake` | `leader`, `adopted` (follower used the leader's facts) or `own_timeout` (no answer in 25 s; own facts) |
| `flat_site` | a naturally flat 7x7 core was found |
| `levelled_half_size` | half size of the square levelled with `/fill`, or null (only plants cleared) |
| `arena_flat_fraction` | share of the arena columns at site level with 2 free blocks (leader's value) |
| `water_near` | water within 16 blocks of the site (drowned / boat weighting) |
| `variant` | episode variant (below) |
| `summon_ok`, `summon_failed` | leader only: summon replies |

Phase notes (`note` records in world_events.jsonl, `{"ep","bot","phase",...}`):
listed per type below.

### `pvpMelee`

Sword / axe duel. Steps: face_off -> round_1 -> breather (85 %) -> round_2 ->
outro.

| key | meaning |
|---|---|
| `variant` | `melee_duel` |
| `loadouts` {actor, observer} | per bot: `weapon` (`<tier>_sword` / `<tier>_axe`), `tier` (wooden..netherite), `kind`, `armor` {head, chest, legs, feet: item or null}, `armor_tier`, `shield` (off hand), `golden_apple` |
| `start_distance` | 5-10 blocks |
| `round_ms` [round1, round2] | 18-30 s, 12-22 s (0 = no second round) |
| `breather` | back off, eat, eye contact between the rounds |
| `crit_chance`, `strafe` {actor, observer} | fighting style |

Notes: `round_1_start/end`, `round_2_start/end`, `face_off`,
`eat_golden_apple`, `eat_beef`. World log: `entity_damage`
(`player_attack`, `victim_blocking`), `entity_knockback`, `arm_swing`,
`use_item_start/stop` (shield, food), `item_consume`, `entity_death`.

### `pvpRanged`

Bow / crossbow duel at 8-20 blocks, then close-in melee, then (70 %) the
bots walk over arrows lying near the arena.

| key | meaning |
|---|---|
| `variant` | `ranged_duel` |
| `loadouts` {actor, observer} | as pvpMelee plus `ranged` (`bow`/`crossbow`), `enchant` (`power`, `punch`, `quick_charge` or null), `enchant_level`, `arrows` (24-48) |
| `start_distance` | 8-20 |
| `ranged_ms`, `melee_ms` | phase lengths |
| `collect_arrows` | arrow pick-up step |
| `strafe` | per bot |

Notes: `face_off`, `ranged_end` {shots}, `close_in`, `close_in_end`,
`collect_arrows`. World log: `bow_shoot`, arrow `entity_spawn` /
`entity_remove` and per-tick trajectories, `entity_damage`.

### `pveMobs`

Both bots fight a summoned group of 4-7 hostile / neutral mobs (1-3 types,
biome weighted, creatures.js `HOSTILE_MOBS`) 7-12 blocks in front of them,
optionally with an allied iron golem (25 %). Daylight-burning mobs get an
unbreakable helmet (or a carved pumpkin) by day. Steps: ready -> fight (until
no hostile is left within 40 blocks for 3 s or 30-55 s passed; partner
glances every 1.5-3.2 s for 0.8-1.3 s, round-2 fixer) -> loot -> outro.
Round-2 fixer: when no mob is left the bots regroup to ~4 blocks (was 3)
and step back facing the partner when closer than 2.5 blocks (`keepApart`;
the creature `outroStep` does the same for all six types), so they do not
end the episode standing ~1 block apart.

| key | meaning |
|---|---|
| `variant` | `pve_group` |
| `mobs` [] | per mob: `type`, `hand`, `helmet`, `baby`, `size` (slime / magma cube 0-2), `neutral`, `villager_type`, `profession` (zombie villagers), `offset` [-r, sideways] in the frame |
| `mob_counts` {type: n}, `mob_total` | |
| `forced_mobs` | types forced by the test hook `PVE_MOBS_FORCE`, else null |
| `ally` | `iron_golem` or null |
| `archer` | `actor` / `observer` (gets bow + arrows) or null |
| `fight_ms` | 30-55 s cap |
| `is_day` | daylight at the episode time |
| `loadouts` | as pvpMelee |

Pool (base weight; biome multipliers in creatures.js): zombie, husk,
drowned (only with water near), skeleton, stray, bogged, spider,
cave_spider, creeper, slime, witch, pillager, vindicator, evoker,
zombie_villager, enderman (neutral), zombified_piglin (neutral), piglin
(immune to zombification), magma_cube, blaze (not in forests / jungles /
taiga / swamps / savannas), silverfish, breeze, hoglin, spider_jockey,
chicken_jockey. The `cave` group (cave_spider x4) is never reached: sites are
on the surface; `ocean` occurs only when a shore site resolves to an ocean
biome. Live-tested: zombie, zombie_villager, skeleton, stray, creeper (one
explosion), pillager, silverfish, enderman, cave_spider (round 2) and, with
`PVE_MOBS_FORCE` (round-2 fixer, flat world, all summoned and fought,
replay_mismatch 0): witch, blaze, breeze (wind charges: `entity_explode`),
spider, husk, drowned (on land), bogged, slime, magma_cube, vindicator,
evoker (+ vexes), piglin, zombified_piglin, hoglin, spider_jockey,
chicken_jockey.

Test hook: `PVE_MOBS_FORCE="witch,blaze+breeze,slime"` (environment of both
controllers): one comma entry per episode, cycled by episode number; `+`
joins up to 3 types. The RNG draws are unchanged.

Notes: `pve_ready`, `fight_end` {remaining_hostiles, kills_seen, deaths,
health, rounds}, `eat_golden_apple`, `loot`. World log: `entity_damage`,
`entity_death`, `entity_explode` + block changes (creepers),
`entity_change_block` (endermen), `bow_shoot`, split slimes / vexes /
potions as `entity_spawn`.

### `rideMount`

One variant per episode (biome weighted; test hook `RIDE_MOUNT_VARIANT=<v>[,<v>...]`
cycles by episode number):

| variant | what happens |
|---|---|
| `horse` | tamed saddled horse / donkey / mule / skeleton / zombie horse (coat variants); the actor rides a loop; the observer rides its own mount the other way round (65 %) or watches from the middle |
| `camel` | the actor rides a saddled camel round the loop while the observer watches from the middle (4-6 blocks from the path); 60 %: the observer climbs on as passenger for a short 3-5 s double ride (round-2 fixer: was 75 % and 6-10 s; the riders sit < 1 block apart) |
| `pig` | saddled pig steered with a carrot on a stick (boost); the observer rides a second pig (50 %) or watches |
| `boat` | 11x7, 2-deep pool made before the snapshot; one boat per bot (random wood, 25 % chest boat for the observer) at OPPOSITE ends of the pool; both go round the same way at the same speed, half a lap apart (round-2 fixer: they started side by side and stayed < 1.5 blocks apart) |
| `minecart` | 15-block track (powered rail + floor lever at the start, an always-powered rail at the end); the actor launches itself with the lever, rides to the end and back (2-3 launches), then the observer takes a turn (85 %) |
| `tame_horse` | untamed horse / donkey / mule without saddle; the actor mounts repeatedly until it stops bucking |

Riders look at the partner 85 % of the time (switching every 1.5-4 s); when
not, they look at the partner whenever it is within +-60 deg of the travel
direction, else ahead (round-2 fixer). Client-controlled vehicles are moved
with `vehicle_move` packets; the rider's head look is sent separately.

| key | meaning |
|---|---|
| `variant` | above |
| `ride_ms` | 15-27 s main ride |
| `mounts` {actor, observer} | entity types (`camel_passenger` = the observer rides the actor's camel) |
| `coats` / `coat` | horse coat variants |
| `passenger_ms` | camel double ride length (0 = none) |
| `boats` [actor wood, observer wood or `<wood>_chest_boat`], `pool` {min, max} | boat variant |
| `track` {start_block, start_powered_rail, end_powered_rail, end_block, lever, block, shape, length}, `actor_launches`, `observer_rides` | minecart variant |
| `max_attempts` | tame_horse mount attempts |
| `loop` {half_u, half_p, direction} | riding loop (rectangle round the site) |

Notes: `mount` {type, ok, role}, `ride_end` {type, ticks, distance,
corrections, wall_skips, ground_check_off}, `dismount`, `minecart_ride`,
`taming` {attempts}. `wall_skips` / `ground_check_off` (round-2 fixer): a
rider whose every waypoint looked like a wall used to stay put for the whole
ride (seen once: `ticks: 0`); after one full round of skips the ground check
is switched off.

### `itemExchange`

| key | meaning |
|---|---|
| `variant` | `item_exchange` |
| `actor_items`, `observer_items` [[name, count]] | 4 / 3 random stacks from a 60-item pool |
| `order` [] | middle steps in order: `toss`, `toss_back`, `drop_at_feet` (60 %), `chest` (75 %), `trade` (40 %) |
| `chest` | {type `chest`/`trapped_chest`, pos, break_after} or null |
| `trade` | {trader `villager` (biome villager type, random profession, level 2) / `wandering_trader`, profession, offers [{cost (emeralds), good, count}]} or null |

Notes: `toss`, `toss_back`, `pickup`, `drop_at_feet`, `chest_deposit`,
`chest_break`, `chest_withdraw`, `trade`. Chest contents are block-entity
data (not in the world log); the notes record them.

### `animalCare`

2-3 activities (no kind twice, biome-weighted animals, variants: sheep
colours, wolf / cat / frog / rabbit / horse / llama / fox / parrot /
mooshroom / panda variants):

| kind | animals | what happens |
|---|---|---|
| `breed` | breedable adults (not wolf / cat / ocelot / turtle) | both feed one adult -> love mode -> baby + XP |
| `milk` | cow, goat (bucket), mooshroom (bowl) | actor milks, sometimes drinks / eats |
| `shear` | sheep (optionally dyed first), mooshroom | wool / mushrooms drop, observer collects |
| `lead` | leashable animals (not bee / parrot) | leash, walk 5-7 blocks, unleash |
| `tame` | wolf (bones), cat / ocelot (cod), parrot (seeds) | feed until tamed, toggle sit |
| `feed_baby` | breedable, not tame-to-breed, not bee / frog; turtles included (round-2 fixer: turtles were in the pool but no activity accepted them; they are fed seagrass as babies) | both feed two babies (growth) |
| `brush` | armadillo | scute drops |
| `eggs` | chickens | actor throws eggs (1/8 hatch) |

| key | meaning |
|---|---|
| `variant` | `animal_care` |
| `activities` [] | {kind, animal, variant, food (breed / feed_baby food, tame item), dye, anchor [x,y,z]} |
| `animals` [] | distinct animal types |

Notes: `breed_feed`, `breed_result`, `milk`, `consume`, `dye`, `shear`,
`collect_drops`, `leash`, `unleash`, `tame_attempts`, `feed_baby`, `brush`,
`collect_scute`, `throw_eggs`, `eggs_result`, `activity_<kind>`.

Unreachable pool entries: `axolotl` has variant code in creatures.js but no
`ANIMALS` entry (no water-pool activity yet); the `cave` biome group never
occurs.
