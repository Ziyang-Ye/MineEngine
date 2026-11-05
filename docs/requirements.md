# Capture requirement ids (T0–T20)

English | [中文](requirements_zh.md)

Comments in the capture code and in `mineengine` cite requirement ids such as `T12` or `T16`. Each id is one change made to the
upstream SolarisEngine capture so that its logs are complete and exact enough to rebuild the white box frame by frame.
This page lists every id, what it requires and where it lives. In the *where* column, paths are relative to `capture/` unless
they start with `capture/` or `mineengine`.

Status: **done** = implemented and checked by `postprocess/verify_world_events.py` or the `mineengine` self-tests;
**mineengine** = solved by the offline white-box renderer instead of the capture; **partly** = only part of it is in the
capture; **open** = not implemented.

## World and camera setup

| id | requirement | status | where |
|---|---|---|---|
| T0 | build and run the modified plugin, controller and camera code end to end | done | whole `capture/` tree |
| T1 | time of day and weather frozen for each episode, set before the first snapshot and recorded in `episode_info`: 85 % day, 15 % dusk / night / dawn; clear 80 %, rain 12 %, thunder 3 %, snow 5 % | done | `controller/episodes-loop.js`, `controller/utils/env-setup.js` |
| T2 | fixed camera client options (FOV 70°, render and simulation distance 12, no view bobbing, no smooth camera, fixed GUI scale), written to `*_camera_meta.json` | done | `camera/launch_minecraft.py`, `camera/windows/launch_camera.py` |
| T13 | the field of view stays at 70° while flying, sprinting or drawing a bow (`fovEffectScale = 0`); `fov`, `fovEffectScale`, `guiScale` and resolution recorded | done | same files as T2; `mineengine` reads the recorded value |
| T12 | the camera shows exactly the bot's view: the controller sends the bot's pose to its camera client over local UDP with every movement packet (pose link), the plugin still moves the camera every tick as a fallback, and the camera's own pose is logged next to the bot's | done | `camera/capture-mod/` (`PoseLink`, `CameraMixin`, `GameRendererMixin`), `server/.../CameraManager.java` |

## World state logs

| id | requirement | status | where |
|---|---|---|---|
| T3 | every tick, one record of all entities within 64 blocks of the snapshot centre: id, kind, name, position, look, velocity, box size, health, flags, held item, TNT fuse | done | `server/episode-manager-plugin/.../WorldStateLogger.java` |
| T14 | combat episodes (pvp, pve, mobs) run on normal difficulty with pvp on, so damage, knockback and death really happen and are logged; difficulty recorded | done | `controller/episodes-loop.js`, `pvp-episode.js`, `pve-episode.js` |
| T15 | action and animation events with tick, time and player: arm swing, damage taken (attacker, amount, cause, knockback, health after), item use start / stop (eat, shield, bow), held-slot change | done | `WorldStateLogger.java` |
| T16 | full per-frame player state: hotbar, selected slot, off hand, armour, health, food, saturation, XP, air, effects, body yaw and head yaw separately, on-ground / sneak / sprint / fly, and the crosshair target (block and face, or entity) with distance | done | `controller/recorder/frame-recorder.js`; read by `mineengine.cap_reader` |
| T17 | block-breaking progress (crack stages 0–9) with block position, stage and player, plus start / abort / finish events | done | `WorldStateLogger.java`, `CameraManager.java`; drawn by `mineengine` in the `fx` channel |

## Checks

| id | requirement | status | where |
|---|---|---|---|
| T4 | the per-episode checker also tests absolute yaw / pitch on every frame, gap-free entity records, recorded time of day and recorded camera options; one OK / WARN / FAIL line per episode | done | `postprocess/verify_world_events.py` |

## Episodes and behaviour

| id | requirement | status | where |
|---|---|---|---|
| T6 | new episode types: coBuild, buildVsBreak, digDown / tunnel / crater, tntDemolish, forestBuild, pvpMelee / pvpRanged, pveMobs, itemExchange (later also rideMount, snowIce, farming, animalCare, waterWorks, redstoneDoors, lightAndFire, fallingBlocks); each writes its roles, materials and key parameters to `state_metadata` | done | `controller/episode-handlers/diverse/` |
| T7 | building episodes draw their materials at random from a material pool and record them | done | `controller/utils/materials.js`, `build-*.js`, `tower-bridge-episode.js` |
| T8 | the idle player keeps 3–20 blocks away, looks at the work site or the other player and moves now and then, so each player is in the other's view often | done | `controller/episode-handlers/partner-gaze.js` and the handlers that use it |

## Rendering and scale

| id | requirement | status | where |
|---|---|---|---|
| T5 | a second, pixel-aligned "white-box" video per player | mineengine | rendered offline from the logs by `mineengine.cap2proxy` instead of a second game client, so it costs no capture time and can be re-rendered later |
| T9 | depth and block-id images | mineengine | the `logdepth`, `btype`, `bstate` and `face` channels of the white box ([whitebox_channels.md](whitebox_channels.md)) |
| T10 | episodes also run on normal generated terrain, not only on flat worlds | done | the world type is chosen per instance (`generate_compose.py`) and recorded as `extra_info.world_type` |
| T11 | several capture instances on one machine | done | `capture/linux/run_headless.sh` picks free ports and displays; 4 instances per GPU were used |
| — | tick-locked capture (one video frame per game tick) | open | in development, listed in [capture/README.md](../capture/README.md) |

## Later additions (T18–T20)

These three came with a later revision of the output format. The synthetic episodes of `mineengine.cap_synth` follow them
exactly. The capture writes T18 and T19 in its own form, and `mineengine` reads both forms.

| id | requirement | status | where |
|---|---|---|---|
| T18 | each player's skin per episode: the skin file, its sha1, the arm model (classic or slim) and when it was applied (`skin` in `episode_info`), with the PNG stored as `output/skins/<sha1>.png` | done | the capture writes `state_metadata.skins` in `episode_info` (`controller/utils/skins.js`), and `tools/skins/export_episode_skins.py` copies the PNGs next to the episode files; `mineengine.cap2proxy` turns either form into the same record and copies the PNG to `output/skins/<sha1>.png`, where `mineengine.entity_refs` reads it |
| T19 | per-tick mob lists: every tick record lists the mobs (`mobs`: uuid, type, position, yaw, box, health, target) | done | the capture logs the same mobs as T3 `entities` records (`WorldStateLogger.java`); `mineengine.cap_reader` reads both forms |
| T20 | persistent worlds: an episode starts where the previous one ended. Its start snapshot is the previous end snapshot (no set-up edits, no restore), the game clock goes on, dropped items stay, and the episodes of one chain share a `session_id` | partly | in the capture, the episodes of one instance share one world (`episode_begin.world.uid`), and without `--reset-world` earlier changes stay. There is no `session_id`, and episodes normally teleport to a new site. `mineengine.cap_synth` writes full chains, and `mineengine.cap_selftest` checks that each end snapshot equals the next start snapshot |


## Design-spec references in comments ("03 §2" … "03 §5", "round 2")

Some comments cite sections of the original episode design spec as `03 section N` or `03 §N`. The spec itself is not part
of this repository. What each section asked for:

| reference | what it specifies | where it lives now |
|---|---|---|
| 03 §2 | the new episode types | T6 above; `controller/episode-handlers/diverse/` |
| 03 §3 | material pools for building episodes: walls (oak, spruce, birch and dark oak planks, cobblestone, stone bricks, bricks, six concrete colours, wool), pillars and beams (oak, spruce and birch logs), windows (glass, stained glass), roofs, floors, plus falling sand / gravel and TNT | `controller/utils/materials.js`; `MATERIAL_POOL=spec` gives exactly these pools, the default pool is larger |
| 03 §4 | category shares: building 35 %, breaking and digging 20 %, combat 25 %, movement and observation 10 %, physics and fluids 10 %; 85 % daytime; flat worlds 60 %, normal terrain 40 % | `EPISODE_CATEGORIES` in `controller/utils/episode-weights.js` (override with `CATEGORY_WEIGHTS`); `controller/utils/env-setup.js` |
| 03 §5 | observer behaviour: the idle player stays 3–20 blocks away, mostly looks at the work site or the other player and moves now and then; the other player is in view in at least 40 % of frames; no long close-ups under 1.5 blocks, no turning away | T8 above; `controller/episode-handlers/partner-gaze.js` |

"Round 2", "R2" and "round-2 fixer" (in comments and in `capture/windows/schema_parts/r2-*.md`) refer to the second
development round of the capture code: more episode types, world diversity and skins. They also cover the fixes that
followed its review.
