# Running natively on Windows

The whole capture runs directly on Windows 11, with **no Docker and no WSL**.
- Each former container becomes an ordinary local process. All of them listen only on `127.0.0.1`, except the bot coordination ports (section 2).
- The output layout and file names are the same as the Linux / Docker version's (see `OUTPUT_SCHEMA.md`), so post-processing is unchanged.

## Camera windows, resolution and background recording

- **No window needs to stay open.** The two camera clients are normal Minecraft 1.21 clients that load our `solaris-capture` mod.
  - The mod reads frames from the client's render buffer (GPU readback) and hands them to ffmpeg.
  - The windows are therefore **hidden** by default. Windows covered by other windows or moved off screen still record, so you can keep using the computer.
  - The one limit: **do not minimise a camera window**, since a minimised window renders nothing. Hidden mode avoids this.
  - Add `--show-cameras` to watch the cameras.
- **The resolution can go up.** The default is 1280×720 at 20 fps, the same as the public Solaris data.
  - Use `--width 1920 --height 1080` for more. The recording resolution does not depend on the screen size.
  - Higher resolutions cost more GPU time and disk space.
- **Recording runs in the background**, one stream per client. It uses NVENC hardware encoding, or the CPU's libx264 when NVENC is missing.

## 1. One-time setup (safe to repeat)

Open cmd or PowerShell in `capture/`:

```bat
setup_windows.cmd
```

In order, it:
1. checks Java (21+), Node.js (18+), Python (3.10+) and ffmpeg;
2. creates `.venv` if missing and installs the Python dependencies: numpy, opencv-python-headless, minecraft-launcher-lib 6.5, pywin32, pyyaml, tqdm, pillow;
3. runs `npm ci --ignore-scripts` (in `capture/`) and `npm install` in `server/plugin-starter`;
4. downloads Paper 1.21 (build 130, sha256-checked) into `.cache/windows/` and pre-patches it. The first time, this downloads the server from Mojang (about 1 minute);
5. downloads ffmpeg (the gyan.dev essentials build) if it finds none;
6. builds the MirrorBot server plugin with Maven (it downloads Maven 3.9.9 and checks its sha512 if it finds none) and checks that the jar contains `WorldStateLogger`;
7. builds the camera mod `camera/capture-mod` with Gradle;
8. installs the camera client (Minecraft 1.21 + Fabric, in `.cache/windows/minecraft`);
9. writes the tool paths it found into `.cache/windows/setup.json`; capture reads them from there.

Steps already done are skipped, so it can be run again at any time. Useful switches:
- `--paper-jar <jar>`: use an existing Paper jar;
- `--rebuild-plugin` / `--rebuild-mod`: force a rebuild;
- `--force-npm`;
- `--skip-camera-install`.

Then, still in `capture/`, fetch the files this repository does not ship:

```bat
.venv\Scripts\python ..\scripts\setup_assets.py
```

It downloads ProtocolLib and SkinsRestorer into `server\plugins\` (sha256-checked), and extracts the default skins and the HUD template from the client jar that step 8 installed. MirrorBot does not start without ProtocolLib, so do not skip this step.

## 2. Capture

**First read and accept the [Minecraft EULA](https://aka.ms/MinecraftEULA) yourself.** You need your own Minecraft: Java Edition licence; the offline mode used here only lets the automated capture run without logging in. Then:

```bat
rem smoke test: 5 episodes
run_windows.cmd --accept-minecraft-eula --mode stateful --episodes 5 --output-dir output_smoke

rem pilot
run_windows.cmd --accept-minecraft-eula --mode all --episodes 60 --batches 2 --output-dir output_pilot
```

The options are the same as `run_laptop.sh`'s:
- `--mode stateful|mix|all|<list of types>`;
- `--episodes`, `--batches`, `--output-dir`;
- `--world-seed`, `--reset-world`, `--normal-world`, `--snapshot-mode`.

Options after `--` go to `run.sh`. The Windows version adds:

| option | meaning |
|---|---|
| `--width 1280 --height 720` | recording resolution (default 1280×720) |
| `--show-cameras` | show the camera windows (hidden by default; recording is in the background) |
| `--server-memory 2G` | Paper server memory |
| `--render-distance 12` | camera render distance, also the server view distance |
| `--encoder auto` | `auto` / `h264_nvenc` / `libx264` |
| `--batch-timeout <seconds>` | longest run time per batch (no limit by default) |
| `--dry-run` | print every command, environment variable and port that would be used, and start nothing |
| `--no-cameras` | debugging: server and bots only, no cameras (no video, no alignment) |

Each batch starts its processes in this order:
1. the Paper server, then a wait until RCON is ready;
2. the action recorder;
3. the two cameras, the two spectator bots and the start-up script;
4. the two controller bots.

When both controller bots finish, the cameras are told to stop recording and every process is closed. The server saves normally through RCON `stop`. Then the videos are aligned, `verify_world_events.py` checks the batch, and a summary is printed.

Ports:
- on `127.0.0.1` only: game 25590, RCON 25600, action recorder 8110/8111, camera pose link 8130/8131 (UDP);
- on all network interfaces: bot coordination 8120/8121, which the bot controllers open. Run captures on a trusted network, or keep these ports closed in the firewall.

RCON uses the fixed password `research`. Other users of the same machine can reach it.

## 3. Stopping

- Press **Ctrl+C once** in the run window for a clean stop. The cameras finish, the server saves the world, and the episodes already recorded are aligned and checked as usual.
- Press **Ctrl+C again** to end every process at once.
- If you close the window or the program crashes, every child process (java, node, ffmpeg) ends with it (a Windows Job Object). No background process is left behind, and no extra stop command is needed.

On the first run, Windows Firewall may ask whether java / python / node may use the network. Capture only uses connections inside this machine, so choosing "Cancel" does not affect it, and it keeps the bot coordination ports closed to other machines.

## 4. Output

`<output-dir>\data_collection\train\batch_<k>\`:

```
output\                         per-episode action json, episode_info, world_events.jsonl, snapshots .json.gz
aligned\                        aligned *_camera.mp4 + meta
camera\output_alpha\0\          raw recording camera_alpha.mkv, camera_alpha_meta.json, camera_alpha_frames.jsonl
camera\output_bravo\0\          the same for Bravo
camera\data_alpha\0\ ...        camera client settings and logs (rarely needed)
data\0\                         the Paper server folder (world save, plugins)
logs\docker-compose-000\        one log per process, with the same file names as the Docker version (mc_instance_0.log, ...)
logs\run_windows.log            the runner's own log
logs\postprocess\               alignment and check logs
compose_configs\run_config.json every command, option, port, tool version, git version and exit code of the batch
world_events_report.json        the check report
```

## 5. Common problems

- **"Port in use".** An earlier capture has not exited yet, or another program holds one of 25590/25600/8110/8111/8120/8121/8130/8131.
- **A camera quits in the middle.** The runner restarts it once, and the new recording's file name ends in `_r1`. The restart is flagged clearly in the window and in `run_config.json`.
  - The first recording is kept. For each episode, alignment picks the recording that covers that episode's time span.
  - When a camera drops, the server also kicks its bot ("Your partner left"). The episode running at that moment is usually lost, and later episodes of that instance may be lost too.
  - Mark such batches separately.
- **A bot never exits.** After one bot of an instance finishes, the other gets at most `--controller-exit-grace` seconds (default 300). The limit is per instance and does not affect other instances. A bot past the limit is force-stopped, and the batch is recorded as `completed_with_kills` (exit code 1).
- **The server does not start.** See `logs\docker-compose-000\mc_instance_0.log`.
- **No NVENC.** The cameras switch to CPU encoding. The camera log and `video_encoder` in `camera_*_meta.json` say so.
