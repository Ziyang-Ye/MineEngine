# Windows-native runtime: component contract (developer reference)

This runtime runs the whole SolarisEngine stateful-capture
pipeline natively on Windows 11 — no Docker, no WSL. Every container of the
upstream compose file becomes a native process on 127.0.0.1 (except the bot
coordination ports, which listen on all interfaces). Output files,
names and directory layout are IDENTICAL to the Docker/Linux path
(`OUTPUT_SCHEMA.md`), so downstream tooling does not change. The Linux/Docker
path must keep working (do not break `run.sh`, `generate_compose.py`, Dockerfiles).

Target machine: Windows 11, i9-13980HX (32 threads), 32 GB RAM, RTX 4070 Laptop
8 GB (driver 566), 2560x1600 screen at 150 % DPI. Tools available:
JDK 23 (`java` on PATH), Node 24, Python 3.13 (repo venv at `.venv/`, has
numpy, opencv-python-headless, minecraft-launcher-lib 6.5, pywin32, pyyaml),
Maven at `C:\Users\<you>\tools\apache-maven-3.9.9\bin\mvn.cmd`
(local repo `C:/Users/<you>/tools/m2repo`), ffmpeg/ffprobe 9.x (has h264_nvenc)
at `C:\Users\<you>\tools\ffmpeg-9.0.2-essentials_build\bin\`, Paper jar at
`C:\Users\<you>\tools\paper\paper-1.21-130.jar`. `npm ci --ignore-scripts`
has been run (native `gl`/`canvas` are NOT built and must not be required when
viewer rendering is disabled). Git Bash is the shell; scripts must run from
`cmd.exe`/PowerShell too (use `python`, not `python3`, in Windows code paths;
use `pathlib`, never hard-code `/`-only paths).

## 1. Processes of one instance (instance 0)

| upstream service | native process | ports |
|---|---|---|
| mc_instance_0 | `java -Xmx<mem> -jar paper.jar --nogui` in `BATCH/data/0` | game 25590, RCON 25600 |
| act_recorder_alpha / bravo | `python controller/act_recorder/act_recorder.py --port P ...` | Alpha 8110, Bravo 8111 |
| controller_alpha / bravo | `node controller/main.js <same flags as controller/entrypoint.sh>` | coord: Alpha listens 8120 and connects to 127.0.0.1:8121; Bravo listens 8121, connects to 8120 |
| spectator_alpha / bravo | `node spectator/spectator.js` (env MC_HOST, MC_PORT, MC_USERNAME) | – |
| plugin_starter | `node server/plugin-starter/plugin_starter.js` (ES module, its own package.json) | – |
| camera_alpha / bravo | `python camera/windows/launch_camera.py ... --pose-port P` -> real Minecraft 1.21 client (Fabric loader + `solaris-capture` mod), recording **inside the client** | pose link (UDP, the mod listens): Alpha 8130, Bravo 8131; the matching controller gets env `CAMERA_POSE_PORT=P` (§5.5) |

Instance k (k>0, optional): game/RCON ports `+k` exactly like `generate_compose.py`
does (`--base-port`, `--base-rcon-port`); recorder, coord and pose ports `+30*k`
(`NATIVE_PORT_STEP`; one instance uses the block 8110-8131, so instance 3 ends at 8221).
The earlier `+10*k` collided (instance 1 recorders 8120/8121 = instance 0 coord ports).
`run_windows.py` checks the whole plan (all instances, all roles) for duplicate ports
and refuses to start on a collision (e.g. `--base-rcon-port` = `--base-port` + 1 with two
instances). `--pose-port` sets Alpha's pose port of instance 0, `--no-pose-link` disables
the link (cameras then follow the server only).

## 2. Server directory (`BATCH/data/0`)

- `server.properties`: `server-port=25590`, `enable-rcon=true`, `rcon.port=25600`,
  `rcon.password=research`, `broadcast-rcon-to-ops=true`, `online-mode=false`,
  `enforce-secure-profile=false`, `allow-flight=true`, `spawn-protection=0`,
  `gamemode=survival`, `pvp=true`, `difficulty=peaceful` (controllers change it per episode),
  `view-distance=12`, `simulation-distance=12` (the camera render distance can never exceed
  the server view distance), `level-seed=<seed>`, flat worlds: `level-type=minecraft\:flat`,
  `generator-settings=<json from generate_compose.generate_terrain_settings>`,
  `generate-structures=<bool>`; normal worlds: `level-type=minecraft\:normal`.
- `eula.txt` with `eula=true` is written ONLY when `--accept-minecraft-eula` was given.
- `plugins/` = copy of `server/plugins/` (MirrorBot jar must contain WorldStateLogger),
  `skins/` = copy of `server/skins/` (same as the compose `prep_data` service).
- Env: `WORLD_STATE_OUTPUT_DIR=<abs BATCH/output>`.
- Readiness = RCON connects and `list` answers.

## 3. Controller-side env / args

The native orchestrator builds the per-service env by calling
`generate_compose.generate_compose_config(...)` (single source of truth) and then
overrides: `MC_HOST=RCON_HOST=OTHER_COORD_HOST=ACT_RECORDER_HOST=127.0.0.1`,
`ACT_RECORDER_PORT` (8110/8111), `COORD_PORT`/`OTHER_COORD_PORT` (see §1).
Controllers are started as `node controller/main.js` with the same flag list as
`controller/entrypoint.sh` (Xvfb is not needed on Windows).

## 4. Per-frame recorder (controller) — replaces prismarine-viewer headless

`controller/recorder/frame-recorder.js` exports
`startFrameRecorder(bot, {output: "host:port", interval: 50, disableRendering: true})`.
Same wire protocol, events (`startepisode`, `endepisode`, `episodeended`,
`action_*`) and the same per-frame fields as
`prismarine-viewer-colalab/lib/headless-base64.js` (copied there verbatim in
behaviour), plus the T16 fields (OUTPUT_SCHEMA.md §3). When
`disableRendering` is false it delegates to `require("prismarine-viewer-colalab").headless`
(lazy require) so the Docker path with rendering still works.

## 5. Camera client (Windows) — `camera/windows/` + `camera/capture-mod/`

The camera is a normal Minecraft 1.21 Java client (offline name `CameraAlpha` /
`CameraBravo`), Fabric loader 0.16.x, plus our mod `solaris-capture`
(`camera/capture-mod/`, Gradle + Fabric Loom, Mojang mappings, no Fabric API
dependency). The mod captures frames **from the client's main render target**
(PBO async `glReadPixels`), so the window may be hidden / covered / off-screen
(but never minimized — a minimized GLFW window has a 0x0 framebuffer).

### 5.1 Launcher CLI

```
python camera/windows/launch_camera.py --name CameraAlpha --server 127.0.0.1:25590
    --mc-home <shared .minecraft: versions, libraries, assets, runtime>
    --game-dir <BATCH/camera/data_alpha/0>        (options.txt, mods/, logs/)
    --output-dir <BATCH/camera/output_alpha/0>
    --video-name camera_alpha.mkv
    --width 1280 --height 720 --fps 20
    --ffmpeg <path to ffmpeg.exe> --encoder auto|h264_nvenc|libx264
    --render-distance 12 --gui-scale auto --max-memory 3G
    --hide-window 1 --stop-file <path> --mod-jar <path>
    [--install-only]
```
`--install-only` installs Minecraft 1.21 + its Mojang Java runtime + Fabric
loader into `--mc-home` and exits. Otherwise: ensure installed, write
`options.txt`, copy the mod jar into `<game-dir>/mods/`, write the mod config
`<game-dir>/config/solaris-capture.json`, launch with quick-play multiplayer,
wait for the process, return its exit code. Uses the Java runtime that
minecraft-launcher-lib installs for 1.21 (not the system JDK 23).
`--pose-port P` (default 0 = off) and `--pose-max-age-ms` (500) configure the pose link (§5.5).
Clean frames / local skins: `--suppress-toasts 1` (mod drops every toast), `--chat-visibility hidden`
(options.txt `chatVisibility:2`), `--local-skins 1` and `--skins-dir <repo>/server/skins/pool`
(unsigned local-pool skins, `windows/schema_parts/camera.md`, `r2-skins.md` §1.4).

### 5.2 options.txt (T2, T13) — values the launcher writes

`fov:0.0` (= 70 deg), `fovEffectScale:0.0`, `renderDistance:12`,
`simulationDistance:12`, `bobView:false`, `guiScale:<auto: round(height/360), 2 at 720p>`,
`damageTiltStrength:0.0`, `pauseOnLostFocus:false`, `maxFps:60`, `enableVsync:false`,
`inactivityFpsLimit:minimized`, `soundCategory_master:0.0`, `narrator:0`,
`tutorialStep:none`, `skipMultiplayerWarning:true`, `joinedFirstServer:true`,
`onboardAccessibility:false`, `skipQuickPlayFirstLaunchPrompt:true`
(the launcher already wrote the last one), `fullscreen:false`, `chatVisibility:2` (hidden). Entity shadows, clouds,
graphics mode, particles stay at Minecraft defaults (recorded). Smooth camera is a
keybind toggle that defaults to off. HUD and name tags stay visible.

### 5.3 Mod config `<game-dir>/config/solaris-capture.json`

```json
{"camera_name": "CameraAlpha", "output_dir": "...", "video_name": "camera_alpha.mkv",
 "meta_name": "camera_alpha_meta.json", "frames_name": "camera_alpha_frames.jsonl",
 "ffmpeg": "C:/.../ffmpeg.exe", "encoder": "auto", "fps": 20, "width": 1280, "height": 720,
 "hide_window": true, "stop_file": "...", "server": "127.0.0.1:25590",
 "pose_port": 8130, "pose_max_age_ms": 500, "suppress_toasts": true, "chat_visibility": "hidden",
 "local_skins": true, "skins_dir": "C:/.../server/skins/pool"}
```

### 5.4 Mod behaviour

- Starts recording when the player is in a world (level != null); keeps recording
  until the stop file appears, the client disconnects, or the JVM exits; then
  closes ffmpeg stdin, waits for ffmpeg, and (stop file case) calls
  `Minecraft.stop()`. Never leaves a corrupt MKV on a normal stop.
- Frame clock: one frame per wall-clock slot of `1000/fps` ms (slot k at
  `t0 + k*50 ms`); a frame is captured on the first rendered frame at or after the
  slot time. Slots missed entirely are not duplicated by the mod.
- Readback: right before the buffer swap (after the main target was blitted),
  read the main render target with a ring of >=3 PBOs; the writer thread pipes raw
  RGBA to ffmpeg: `ffmpeg -f rawvideo -pix_fmt rgba -s WxH -framerate FPS
  -use_wallclock_as_timestamps 1 -i - -vf vflip -copyts -fps_mode passthrough
  -c:v h264_nvenc -preset p4 -bf 0 -pix_fmt yuv420p out.mkv` (libx264
  `-preset veryfast -bf 0` fallback; `auto` probes NVENC with a 1-frame test encode).
  MKV PTS are therefore wall-clock epoch seconds, exactly like the Linux
  x11grab recordings, so `postprocess/process_recordings.py` keeps working.
- Per-frame sidecar `camera_<bot>_frames.jsonl`, one line per encoded frame,
  in encode order: `{"i": n, "epoch_ms": <float, capture time>, "slot": k,
  "game_time": level.getGameTime(), "cam_pos": [x,y,z] (camera/eye position used to render),
  "cam_yaw": <MC deg>, "cam_pitch": <MC deg>, "fov": <deg actually used by GameRenderer>,
  "player_pos": [x,y,z], "fb": [w,h], "gui_hidden": bool, "screen": <open screen class or null>}`.
  This is the exact rendered camera pose (T12 ground truth).
- Meta `camera_<bot>_meta.json` (written at recording start, rewritten at stop with
  `frames` count and `stop_epoch_seconds`): upstream keys `recording_path`,
  `start_epoch_seconds`, `start_time_utc`, `fps`, `width`, `height`, `camera_name`,
  `video_encoder`, `wallclock_timestamps: true`, plus `capture_method:
  "in_client_fbo_readback"`, `frames_file`, `platform: "windows"`, `mc_version`,
  `fabric_loader_version`, `mod_version`, `framebuffer: [w,h]`, `window_hidden`,
  and `options` read from the live `Options` object: `fov_deg`, `fov_effect_scale`,
  `render_distance`, `simulation_distance`, `server_view_distance` (if known),
  `gui_scale` (setting) and `gui_scale_effective`, `bob_view`, `damage_tilt_strength`,
  `screen_effect_scale`, `entity_shadows`, `clouds`, `graphics_mode`, `particles`,
  `max_fps`, `hud_visible` (true), `name_tags_visible` (true), `toasts_suppressed` (true),
  `chat_visibility` (`hidden`); `pose_link` (§5.5); `toasts` (dropped toasts) and `local_skins`
  (skins loaded from the local pool).
- Clean frames: no toast is ever drawn (`ToastComponent#addToast` cancelled), chat hidden; the
  rest of the vanilla HUD (hotbar, hearts, crosshair, action bar, held-item name, name tags) stays.
- Local skins: a profile whose unsigned `textures` property names a local pool skin
  (`http://solaris.local/skins/<id>.png` + `solaris` block) gets that PNG from `skins_dir` as a
  secure skin (`SkinManager#getOrLoad` hook); nothing is downloaded.

### 5.5 Pose link (T12)

The controller of bot B (`controller/recorder/camera-pose-link.js`, enabled by env
`CAMERA_POSE_PORT`) sends one UDP datagram per outgoing movement packet
(`position`, `look`, `position_look`) to `127.0.0.1:CAMERA_POSE_PORT`:

```json
{"v":1,"bot":"Alpha","seq":n,"t":<epoch ms float, sender clock>,"x":..,"y":..,"z":.. (feet),
 "yaw":.., "pitch":.. (Minecraft degrees, as sent),"sneak":bool,"riding":bool,"pkt":"position_look"}
```

`riding` = the bot sits on a vehicle (Mineflayer then sends no position packets, so x/y/z
are stale; the mod keeps the vanilla camera, `pose_fallback: "riding"`). Every action sample
of that controller gets `pose_seq` = `seq` of the last datagram sent (its yaw/pitch are the
sample's `yaw`/`pitch`); `process_recordings.py` uses it to pair each sample with a frame that
rendered that packet (`--pose-pairing seq`, default).

The camera mod of `Camera<B>` (config `pose_port`) accepts datagrams only from 127.0.0.1,
only with `bot` = its `camera_name` minus `Camera`, and ignores ones with an older `seq`
(a lower `seq` with a newer `t` is a restarted controller). While a pose is fresh
(received <= `pose_max_age_ms` ago, or older but still equal to the camera player's
server-followed pose) and the bot is not riding, the render camera uses it as-is: `Camera#setup`
TAIL sets the position (x, y + 1.62, or + 1.27 sneaking, z) and the rotation (yaw, pitch); otherwise the vanilla
(server-followed) camera is used. The sidecar records the pose actually rendered plus
`pose_source` (`injected` | `server`), `pose_seq`, `pose_t`, `pose_age_ms`, `pose_held`,
`pose_sneak`, `pose_fallback`, `player_yaw`, `player_pitch`; the meta gets `pose_link`
(`port`, `max_age_ms`, `received`, `used_frames`, `server_frames` (encoded frames),
`max_age_seen_ms`, ...; see `windows/schema_parts/camera.md`).

## 6. Orchestrator — `windows/run_windows.py` (+ `windows/setup_windows.py`)

Same CLI and defaults as `run_laptop.sh` (`--accept-minecraft-eula`, `--mode`,
`--episodes`, `--batches`, `--output-dir`, `--world-seed`, `--reset-world`,
`--normal-world`, `--snapshot-mode`, `-- <extra>`), plus `--width/--height`,
`--show-cameras`, `--server-memory`. Writes the same tree:
`BATCH/{compose_configs/run_config.json, logs/docker-compose-000/<service>.log,
data/0, output, camera/output_alpha/0, camera/output_bravo/0, camera/data_alpha/0,
camera/data_bravo/0, aligned, world_events_report.json}` — log file names equal the
compose service names (`mc_instance_0.log`, `camera_alpha_instance_0.log`, ...).
Start order: server -> (ready) -> act recorders -> cameras + spectators + plugin
starter -> controllers. Batch ends when both controllers exit (or Ctrl+C /
timeout): create camera stop files, wait <=60 s, stop everything (server via RCON
`stop`), then alignment (`postprocess/process_recordings.py` per episode, parallel)
and `postprocess/verify_world_events.py BATCH --report BATCH/world_events_report.json`.
Ctrl+C must always clean up child processes (Windows Job Object or explicit tree kill).
`windows/setup_windows.py`: one-time setup (check tools, build MirrorBot with Maven,
build the mod with Gradle, `npm ci --ignore-scripts`, server/plugin-starter npm install,
camera `--install-only`), idempotent.
