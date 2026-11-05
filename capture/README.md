<!-- Modified by Ziyang Ye for MineEngine, 2026 (from SolarisEngine 78f23e7); see capture/MODIFICATIONS.md -->
# capture/: two-player Minecraft capture (from SolarisEngine; Windows-native and headless Linux)

English | [中文](README_zh.md)

The capture engine is a modified version of [SolarisEngine](https://solaris-wm.github.io/solaris-engine/) (Apache-2.0),
starting from upstream commit `78f23e7` (2026-07-23). It is released together with the `mineengine` white-box pipeline (see the
repository [README](../README.md)).

It records **two players' first-person Minecraft videos at the same time**, together with:
- a **world state that replays exactly** (the ground truth of the white box);
- per-frame actions;
- the camera pose that actually rendered each frame.

It has four parts:
- a Paper server with the MirrorBot plugin, which logs the world state;
- Mineflayer bots;
- camera clients running the capture mod;
- post-processing, which aligns and checks each batch.

Other docs:
- upstream SolarisEngine documentation: [solaris-wm.github.io/solaris-engine](https://solaris-wm.github.io/solaris-engine/) (sources in `docs/source/`);
- Docker usage (the original laptop path; it needs skins signed through MineSkin, and its ports are reachable from the network): [STATEFUL_CAPTURE.md](STATEFUL_CAPTURE.md);
- headless Linux: [linux/README.md](linux/README.md);
- requirement ids in the comments (T0–T20): [../docs/requirements.md](../docs/requirements.md);
- output format: [OUTPUT_SCHEMA.md](OUTPUT_SCHEMA.md) (upstream format) and [windows/schema_parts/](windows/schema_parts/) (the fields each component adds);
- component contract: [windows/CONTRACT.md](windows/CONTRACT.md).

## What changed compared with SolarisEngine

| area | change |
|---|---|
| running | Runs natively on Windows with no Docker or WSL (`windows/run_windows.py`). The Linux / Docker paths are kept. |
| video | The Fabric mod `camera/capture-mod` grabs frames inside the client and encodes them with NVENC or x264. It works in the background and the window can be hidden. Every frame logs the **camera pose and FOV that were actually rendered** (`camera_<bot>_frames.jsonl`). |
| camera accuracy (T12) | The bot's pose goes straight to its camera over local UDP (pose link). Action labels are paired with video frames by pose-packet sequence number. |
| field of view (T13) | Fixed at 70° with `fovEffectScale = 0`. Upstream Solaris cameras fly, and the flying FOV effect (×1.1) makes them render at 77°. |
| world state | Logged every tick: <ul><li>all entities (T3);</li><li>damage, knockback and deaths;</li><li>arm swings, item use and held-slot changes (T15);</li><li>digging progress (T17);</li><li>the camera's own pose.</li></ul> Every block that was touched is read back each tick, and the whole region is diffed every second, so the log replays with `replay_mismatch = 0`. |
| per-frame state (T16) | Hotbar, armour, health and food, status effects, and the crosshair target, including the target at the moment of each action. |
| environment (T1 / T14) | Mostly daytime; about 15 % dusk, night or dawn. Clear, rain, thunder or snow. Combat episodes deal real damage. |
| diversity | 45+ episode types: building, breaking and digging, combat, mobs, riding, farming, redstone, fluids, snow and ice, and more. `--mode diverse` samples them by category share. Normal-terrain episodes teleport to one of 45 biomes; flat worlds use 25 surface presets plus biome changes. Ambient animals and a large material pool add variety. |
| player skins | 300 procedural skins, 24 held-out test skins, and the 18 Minecraft defaults. The defaults are extracted from your own client jar at setup (`scripts/setup_assets.py`). Skins are assigned at random per episode. On the native Windows and Linux runners they are **rendered locally by the capture mod, with no external service**. Every skin has a front / back / side reference sheet (`server/skins/reference/`). |
| clean frames | Pop-ups are blocked and chat is hidden in the cameras. The HUD stays. |
| checks | `postprocess/verify_world_events.py` checks, among others:<ul><li>replay consistency;</li><li>T12 angle error;</li><li>how often each player sees the other;</li><li>damage / swing / digging consistency.</li></ul> |

## Layout

```
windows/            Windows-native orchestration: run_windows.py, setup_windows.py, common.py, CONTRACT.md, schema_parts/
camera/capture-mod/ the capture mod (Gradle + Fabric Loom, MC 1.21)
camera/windows/     camera launcher launch_camera.py
controller/         Mineflayer bots: episodes-loop.js, episode-handlers/ (with state/, diverse/), recorder/, utils/
server/             Paper plugin source episode-manager-plugin/, skin pool skins/
postprocess/        alignment, checks, dataset building, tests/
tools/skins/        skin generation, reference-sheet rendering
```

## Windows quick start

You need:
- Windows 10 or 11;
- an NVIDIA GPU;
- JDK 21+ (`java` on PATH);
- Node 18+;
- Python 3.10+.

The full guide is [windows/README.md](windows/README.md).

```bat
rem once: venv, dependencies, Paper, plugin and mod builds, the Minecraft client
setup_windows.cmd
rem once: ProtocolLib and SkinsRestorer (sha256-checked), default skins and the HUD template from your client jar
.venv\Scripts\python ..\scripts\setup_assets.py
rem capture
run_windows.cmd --accept-minecraft-eula --mode diverse --episodes 20 --output-dir output_win
```

Run these in `capture/`. MirrorBot does not start without ProtocolLib, so do not skip `setup_assets.py`.

- `--accept-minecraft-eula` means that **you**, the operator, accept the [Minecraft EULA](https://aka.ms/MinecraftEULA).
- The server and every client run in offline mode. They neither need nor use a Microsoft or Mojang account.
- You need your own Minecraft: Java Edition licence. Offline mode only lets the automated local capture run without logging in; it does not change that.
- With an NVIDIA driver older than 610, NVENC needs ffmpeg 7.x: point `--ffmpeg-dir` at the bin folder of ffmpeg 7.0.2. Otherwise encoding falls back to the CPU.
- Useful options:
  - `--num-flat-world N --num-normal-world M`: several worlds in parallel;
  - `--no-cameras`: logic only;
  - `--dry-run`.

  The full list is in `run_windows.cmd --help`.
- The output layout is the same as the Docker version's (`OUTPUT_SCHEMA.md`, section 1). Each batch is aligned and checked when it ends; the results are in `world_events_report.json`.

## Status and known issues

- Done and tested:
  - everything in the table above;
  - one world (two cameras) holds 20 fps;
  - every replay is exact.
- In development:
  - tick-locked capture, which pairs action labels and frames one to one per tick and fixes pairing errors under heavy load or fast turns;
  - an unattended collection script (`windows/collect.py`);
  - water, caves, the Nether and more interaction episodes;
  - occlusion-aware visibility and diversity reports.
- On a laptop (i9-13980HX, 32 GB, RTX 4070 8 GB), two worlds (four cameras) are close to the limit.
- Headless Linux GPU servers: `linux/run_headless.sh` uses Xvfb + VirtualGL / EGL, picks free ports, and runs several instances per GPU. See `linux/README.md`.

## Licences and notes

- Upstream SolarisEngine is Apache-2.0 (`LICENSE`). The repository `NOTICE` says what changed.
- No Minecraft game files are in this repository: no client or server jar, no default skins, no block models, no HUD sprites. `scripts/setup_assets.py` reads them from your own installation. The only Minecraft content is the gameplay shown in the screenshots (see `NOTICE`).
- Third-party plugins (ProtocolLib, SkinsRestorer) are not in the repository either. They are downloaded from their official releases at setup and sha256-checked; see `THIRD_PARTY.md`. Only two plugin configuration files are kept, both changed so that the plugins make no web requests: `server/plugins/SkinsRestorer/config.yml` (SkinsRestorer's default file, under its GPL-3.0, with `advanced.noConnections: true` and `api.fetchRecommendedSkins: false`) and `server/plugins/bStats/config.yml` (`enabled: false`, no server id). The MirrorBot plugin is built from source by `setup_windows.cmd` or `server/build_plugin.sh`.
- The two session-start skins `server/skins/session_a.png` and `session_b.png` are copies of procedural skins. The native runners need no signed cache for them.
- Network: on the native runners, the Paper server (game port and RCON), the action recorders and the camera pose link listen only on `127.0.0.1`. The bot coordination ports (by default 8120/8121, +30 per instance) listen on all interfaces, so run captures on a trusted network. RCON uses the fixed password `research`, so other users of a shared machine can reach it. On the Docker path the game port and RCON listen on all interfaces; see [STATEFUL_CAPTURE.md](STATEFUL_CAPTURE.md), section 4.2.
- Captures record absolute paths of your machine (for example in `run_config.json` and the aligned `*_meta.json` files). Check them before you share a dataset.
