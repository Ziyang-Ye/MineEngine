# Stateful capture with Docker (the laptop path)

English | [中文](STATEFUL_CAPTURE_zh.md)

This directory is a modified version of SolarisEngine (Apache-2.0). This page covers its original Docker path: two-player Minecraft capture on one Linux gaming laptop with Docker and an NVIDIA GPU. The main paths today are Windows-native (`windows/README.md`) and headless Linux (`linux/README.md`).

Besides the video and the per-frame actions of upstream SolarisEngine, a run records an **explicit, editable world state**:
- a block snapshot of each episode's area at the start;
- a per-tick log of block and entity changes;
- the real world seed;
- new episode types that change the world: ignite and extinguish, ice melting next to heat, TNT blasts, lava meeting water, build then break.

The exact field definitions are in `OUTPUT_SCHEMA.md` in this directory.

---

## 1. What changed

| where | change |
|---|---|
| `server/episode-manager-plugin/.../WorldStateLogger.java` (new) | A world-state logger inside the Paper plugin, with a new `/worldstate` command that the controller bot calls over RCON. See the notes below the table. |
| same | Block snapshots of the episode area (by default 65 × 65 blocks around the arena, from 24 blocks below the players to 40 above): `*_world_snapshot_start.json.gz` when the episode starts and `*_world_snapshot_end.json.gz` when it ends. Optional: copy the `.mca` region files that cover the area after `save-all flush`; restore the area to its start state between episodes. |
| `EpisodeManager.java`, `plugin.yml` | Register the logger and the `/worldstate` command. |
| `server/build_plugin.sh` (new) | Rebuilds the plugin jar inside a Maven container, so the host needs no Java. |
| `controller/utils/world-state.js` (new), `controller/episodes-loop.js` | See the notes below the table. |
| `controller/episode-handlers/state/*.js` (new) | 5 new episodes: `igniteAndExtinguish`, `iceNearFire`, `tntBlast`, `lavaWater`, `buildAndBreak` (section 5). |
| `controller/utils/episode-weights.js` | Typical durations of the new types (sampling weights stay 1/√duration, as upstream) and optional per-type weight multipliers. |
| `controller/act_recorder/act_recorder.py` | The hard-coded `"seed": 42` in the per-frame JSON becomes the configured seed and world type. Absolute `yaw` / `pitch` are kept at higher precision (1e-5 rad), and `yaw_mc_deg` / `pitch_mc_deg` add Minecraft's degrees. |
| `controller/config/args.js`, `controller/entrypoint.sh` | Pass the new options through. |
| `generate_compose.py` | New options; see the notes below the table. |
| `run.sh` | The matching command-line switches; adjustable post-processing parallelism; the world-log check after every batch; `--skip-dataset`. |
| `run_laptop.sh` (new) | A one-laptop preset: 1 instance (2 players), a flat world, only the new episodes by default. |
| `postprocess/verify_world_events.py` (new) | Checks the world log, replays `block_state` against the end snapshot, and checks the time alignment with the per-frame JSON. |
| `postprocess/prepare_train_dataset.py` | Copies the world logs and snapshots into the dataset. |

**`WorldStateLogger.java`** listens to:
- block place, break, burn, ignite and fade (ice melting, fire going out);
- block forming (water + lava → obsidian / cobblestone), liquid flow and fluid level changes;
- explosions, TNT ignition, pistons, falling blocks, tree growth, bucket empty and fill, and player interaction;
- entity spawn, death and removal, and weather changes.

Its records:
- Every event is one JSON line in the episode's own `*_world_events.jsonl`.
- Every record carries three clocks:
  - `game_time`, the same counter as `worldAgeTicks` in the per-frame JSON;
  - `server_tick`;
  - `epoch_ms`, the same host clock as the per-frame `epochTime`.
- One `tick` record is written every tick. It maps the clocks to each other and gives both players' server-side position and facing.
- On the tick after each event, the block's actual state is read back as a `block_state` record. This is the ground-truth stream that replays exactly.

**`world-state.js` / `episodes-loop.js`:**
- Alpha, the leading bot, opens the log and takes the start snapshot before the two players start recording. After the episode ends, it closes the log and takes the end snapshot, optionally followed by the restore.
- The real seed is queried with `/seed` at start-up.
- The episode_info JSON gains `world` (real seed, world type, generator settings), `world_state` and `state_metadata`.
- Teleport targets come from a seeded random generator, so they can be reproduced.
- The new episode types are registered here.

**New options of `generate_compose.py`:**
- `--world_seed` (a fixed seed), `--world_state_logging`, `--world_snapshot_mode`, `--world_snapshot_radius`;
- `--reset_world_between_episodes`, `--teleport_radius`, `--episode_weight_multipliers`, `--state_random_tick_speed`;
- `--server_memory`, `--eula`, `--nvidia_disable_require`.

The server container mounts the output directory to write the world logs. The switch that turns off structure generation in flat worlds (`--flatland_world_disable_structures`) already existed. It can now be used from `run.sh` too.

**Per-frame absolute angles.** The per-frame JSON always had absolute `yaw` / `pitch` (Mineflayer radians), and post-processing never removed them. The one problem: frames right after login, where yaw is still null, were silently dropped (`round(None)` raises). This version keeps those frames and stores the angles at higher precision.

---

## 2. What the laptop needs

| item | requirement (1 instance = 2 players) |
|---|---|
| OS | Ubuntu 22.04 or 24.04 (64-bit) |
| GPU | A discrete NVIDIA GPU with ≥ 4 GB of memory (about 1.2 GB per instance) and NVENC hardware encoding. GTX 16 and RTX 20/30/40/50 laptop GPUs have it; some MX models do not, and then encoding falls back to the CPU, which loads the CPU more. |
| driver | The NVIDIA proprietary driver, 550 or newer. The preset adds `--nvidia-disable-require`, so 580+ is not required. |
| memory | At least 16 GB, 32 GB recommended: the server takes about 2.5 GB, each of the two Minecraft clients 2–4 GB, the bots and recorders about 2 GB. |
| CPU | At least 8 threads (upstream suggests 4 physical cores per instance). |
| disk | At least 50 GB free: Docker images about 10 GB, the Minecraft client about 1.5 GB, data as in section 7. |
| network | The first run downloads the Docker images, the Paper server, the Minecraft client files and the Maven dependencies: about 12–15 GB in total. |
| power | Run on mains power and turn off automatic sleep. |

---

## 3. Installation (Ubuntu 22.04 / 24.04)

Run every command below in a terminal.

### 3.1 NVIDIA driver

```bash
sudo ubuntu-drivers install        # or: sudo apt install -y nvidia-driver-570
sudo reboot
nvidia-smi                         # you should see the GPU and the driver version
```

With Secure Boot on, the installation asks for a MOK password; choose "Enroll MOK" at the next boot. On dual-GPU (Optimus) laptops, the default on-demand mode is fine as long as `nvidia-smi` works.

### 3.2 Docker Engine and the Compose plugin (official repository)

```bash
sudo apt-get update
sudo apt-get install -y ca-certificates curl
sudo install -m 0755 -d /etc/apt/keyrings
sudo curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
sudo chmod a+r /etc/apt/keyrings/docker.asc
echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu $(. /etc/os-release && echo "${UBUNTU_CODENAME:-$VERSION_CODENAME}") stable" | sudo tee /etc/apt/sources.list.d/docker.list > /dev/null
sudo apt-get update
sudo apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
sudo usermod -aG docker $USER      # then log out and back in once
docker run --rm hello-world        # check after logging back in
docker compose version             # needs v2
```

### 3.3 NVIDIA Container Toolkit

```bash
curl -fsSL https://nvidia.github.io/libnvidia-container/gpgkey | sudo gpg --dearmor -o /usr/share/keyrings/nvidia-container-toolkit-keyring.gpg
curl -s -L https://nvidia.github.io/libnvidia-container/stable/deb/nvidia-container-toolkit.list | \
  sed 's#deb https://#deb [signed-by=/usr/share/keyrings/nvidia-container-toolkit-keyring.gpg] https://#g' | \
  sudo tee /etc/apt/sources.list.d/nvidia-container-toolkit.list
sudo apt-get update
sudo apt-get install -y nvidia-container-toolkit
sudo nvidia-ctk runtime configure --runtime=docker
sudo systemctl restart docker
docker run --rm --runtime=nvidia --gpus all ubuntu nvidia-smi   # the GPU shows up inside the container
```

### 3.4 Python on the host (for post-processing)

Pick one:

```bash
# option A: conda (what upstream recommends)
# install Miniconda first: https://docs.conda.io/en/latest/miniconda.html
conda env create -f env.yaml
conda activate SolarisEngine

# option B: system packages
sudo apt install -y python3-yaml python3-numpy python3-opencv python3-tqdm python3-pil ffmpeg
```

Run everything below inside this environment (with option A, `conda activate SolarisEngine` first).

### 3.5 The code, plugins and assets

Put the whole repository on the laptop, not only `capture/`, for example as `~/MineEngine`. The capture code is its `capture/` directory; every command below runs there:

```bash
cd ~/MineEngine/capture
python3 ../scripts/setup_assets.py
```

`setup_assets.py` makes the files this repository does not ship:
- it downloads ProtocolLib and SkinsRestorer into `server/plugins/` and checks their sha256. MirrorBot does not start without ProtocolLib;
- it extracts the default skins and the HUD template from your Minecraft 1.21 client jar. It looks for the jar in `~/.minecraft/versions/1.21/1.21.jar`, where the official launcher puts it once you have played 1.21; otherwise pass `--jar PATH`.

### 3.6 Signed skins (Docker path only)

The Docker cameras run the vanilla client, which only shows skins signed by Mojang. So this path needs a signed cache (a `.customskin` file) for each skin it uses, and the repository ships none:
- `generate_compose.py` refuses to start until the two session skins `server/skins/session_a.png` and `session_b.png` have one;
- per-episode skins are only used if they have one too.

Make the caches once:

```bash
pip install requests                 # option B of section 3.4: sudo apt install -y python3-requests
python3 regenerate_skin_cache.py --upload server/skins/               # the two session skins
python3 regenerate_skin_cache.py --upload server/skins/ --recursive   # also the per-episode pool (optional)
```

`--upload` sends the PNGs to the MineSkin web service (mineskin.org), which gets them signed by Mojang through its own accounts. Without a MineSkin API key it manages about 60 skins an hour; run it again and it goes on where it stopped. See the top of `regenerate_skin_cache.py` and `server/skins/README.md`.

The native Windows and Linux runners need none of this: their cameras run the capture mod, which renders every skin locally.

---

## 4. Build and run one instance

### 4.1 Build the server plugin (once)

```bash
cd ~/MineEngine/capture
server/build_plugin.sh
```

The last line should read `.../MirrorBot-1.0.jar: contains WorldStateLogger`. The first build downloads the Maven image and dependencies (about 1 GB). `run_laptop.sh` also calls this script when it finds the plugin out of date.

`run.sh` builds the Docker images (`solaris-engine-base`, `solaris-engine-camera`) at start-up. The first build takes about 15–30 minutes; later runs use the cache.

### 4.2 A short test first

```bash
cd ~/MineEngine/capture
./run_laptop.sh --accept-minecraft-eula --mode stateful --episodes 5
```

Section 10 explains `--accept-minecraft-eula`. Without it, the script exits and says why.

Recording starts about 3–5 minutes after launch: the server starts, the clients download their files the first time, and the players get into place. While it runs, you can watch both cameras in a browser:

- Alpha: http://localhost:6901, Bravo: http://localhost:6902 (VNC password `research`)

The server, the two camera containers and the plugin starter use host networking. Two ports of each instance listen on every network interface, not only on `localhost`:
- the game port 25590;
- RCON 25600, with the fixed password `research`, which gives full control of the server console.

Anyone on the same network can reach these two (and the per-instance ports above them when you run several instances). Use the Docker path only on a trusted network, or block them in your firewall. VNC (5901/5902) and noVNC (6901/6902) listen only on `127.0.0.1`; to watch the cameras of a remote machine, forward the port first, for example `ssh -L 6901:localhost:6901 <host>`. The bots and the action recorders (8110) stay on the internal Docker network of their instance.

When all episodes are done, the script stops the containers, fixes file ownership, aligns the videos, runs the world-log check, and prints the output directory.

### 4.3 Real capture

```bash
# only the 5 new state-changing episode types (the default)
./run_laptop.sh --accept-minecraft-eula --mode stateful --episodes 60

# mixed: every upstream training episode + the new ones (new types weighted x3, about half of the episodes)
./run_laptop.sh --accept-minecraft-eula --mode mix --episodes 60

# only the listed types (comma-separated)
./run_laptop.sh --accept-minecraft-eula --mode tntBlast,lavaWater --episodes 30

# a fixed seed + restore the area after every episode + also save the .mca region files
./run_laptop.sh --accept-minecraft-eula --world-seed 20261021 --reset-world --snapshot-mode both

# several batches (every batch restarts all containers: a new world per batch)
./run_laptop.sh --accept-minecraft-eula --batches 3 --episodes 40
```

Options of `run_laptop.sh`:

| option | default | meaning |
|---|---|---|
| `--mode` | `stateful` | `stateful` / `mix` / `all` (the upstream default set + the new episodes, weighted by duration) / a comma-separated list of types |
| `--episodes` | 20 | episodes per batch |
| `--batches` | 1 | number of batches |
| `--output-dir` | `output_laptop` | output directory |
| `--world-seed` | none | a fixed world seed; without it the seed is `<instance number><current timestamp>`. The real value is always recorded. |
| `--reset-world` | off | after every episode, put the snapshot area back to its start blocks and remove the entities the episode spawned |
| `--normal-world` | off | normal terrain instead of a flat world; in `stateful` mode only `buildAndBreak` can then run |
| `--snapshot-mode` | `blocks` | `blocks` (compact block snapshots) / `region` (.mca region files) / `both` / `none` |
| `-- <options>` | | everything after `--` goes to `run.sh` unchanged, e.g. `-- --flat-no-structures --snapshot-radius 48` |

The preset fixes the rest:
- 1 flat-world instance and a teleport radius of 2000, so fewer new chunks are generated;
- a 2G server heap and 4 post-processing processes;
- underwater episodes are kept, not filtered;
- no final dataset (`--skip-dataset`; this step can be run later on a server);
- `NVIDIA_DISABLE_REQUIRE=1` for the camera containers.

To stop in the middle, press Ctrl+C in the terminal, then:

```bash
python3 orchestrate.py stop --compose-dir output_laptop/data_collection/train/batch_0/compose_configs
```

Do not join this server with your own Minecraft client: an extra player would show up in the bots' videos.

### 4.4 NVENC

- Each camera container first tries a test encode with NVENC. If it works, it uses `h264_nvenc`; otherwise it falls back to the CPU's `libx264`. Look for `Using NVENC hardware encoding` or `Using CPU encoding` in `logs/docker-compose-000/camera_alpha_instance_0.log`.
- One instance uses 2 NVENC sessions at once, well within the limit of consumer GPUs.
- The camera image is based on `nvidia/cuda:13.0`, which normally requires a driver with CUDA 13 (580+). The camera only uses EGL and NVENC, not CUDA, so the preset adds `--nvidia-disable-require` to skip that check. With a 580+ driver the switch changes nothing.
- If the video stutters while GPU use is low, the CPU is usually the limit (upstream's docs say the same). Close other programs.

---

## 5. New episode types

In every new episode one player acts and the other watches. Both players record the whole time, so every state change is seen from two views.
- The roles, the arena's facing, whether the observer stands opposite or to the side, and the distance are random, and are written in episode_info's `state_metadata`.
- Arena preparation by server command (clearing snow layers and grass) happens before the start snapshot.

| type | what happens | main state changes recorded |
|---|---|---|
| `igniteAndExtinguish` | 2–4 flammable blocks (planks, logs, hay, wool) are placed and lit with flint and steel, and burn for 3–7 s. Then the actor or the observer (who walks up first) puts the fire out with a water bucket. About half the time the water is scooped back with an empty bucket. | place, ignite, fire spread, burn, fire out, water placed, flow, water scooped, level changes |
| `iceNearFire` | 2–4 ice blocks in a row, with a heat source at the end: a torch, lit netherrack (permanent fire) or a lava source. The players watch the ice melt into water for 14–22 s; the lava variant also forms obsidian or cobblestone. So that the ice melts within one episode, `randomTickSpeed` is set to 100 during the episode (the default is 3; it is restored afterwards and the value is logged). | place, ice melting (fade), flow, block forming |
| `tntBlast` | 1–3 TNT blocks are placed and lit with flint and steel. The actor runs 8–10 blocks away, and the observer watches from 10–13 blocks. Half the time the actor goes back to the crater and looks down. | place, TNT ignition, entity spawn (TNT, dropped items), explosion (with each destroyed block's former state), entity removal |
| `lavaWater` | A 1 × 1 pit is dug and filled with a lava source. The actor or the observer pours water next to it; the water flows over the pit and turns the lava into obsidian. 60 % of the time the water is scooped back and the players watch it recede. | break, lava placed, water placed, flow, obsidian / cobblestone forming, water scooped, level changes |
| `buildAndBreak` | A small structure (wall, pillar, 2 × 2 × 2 cube, arch, L shape, steps) is built from several materials. Then the observer (60 %, after walking up) or the actor breaks 30–80 % of it from the top down. Sometimes sand or gravel is used, which falls when the block below is broken. | place, break, dropped items, falling blocks |

The first four run only in flat worlds; `buildAndBreak` also runs on normal terrain.

---

## 6. How the two timelines line up (short version)

- Every world-log record has `epoch_ms` (host clock, milliseconds) and `game_time` (world ticks).
- Every frame of the per-frame action JSON has `epochTime` (the same host clock, in seconds) and `worldAgeTicks` (the same tick counter). `worldAgeTicks` only updates when the server syncs once per second, so it lags by 0 to 20+ ticks.
- The log has a `tick` record for every tick, which maps `epoch_ms` to `game_time`. Use it to turn each frame's `epochTime` into an exact tick.
- In the aligned video, frame i is the first camera image taken after action entry i. A block change at time t is therefore visible from the first frame with `epochTime ≥ t`.

---

## 7. Output

After a run, `output_laptop/data_collection/train/batch_0/` mainly holds:

```
compose_configs/docker-compose-000.yml       the configuration used
logs/docker-compose-000/*.log                one log per container
data/0/                                      the server folder, with the full world save world/
output/                                      per-episode files (table below)
output/world_regions/<stem>_start/           only with --snapshot-mode region/both: r.X.Z.mca + level.dat
camera/output_alpha/0/camera_alpha.mkv       Alpha's raw recording (the whole session, 1280×720)
camera/output_bravo/0/camera_bravo.mkv       Bravo's raw recording
camera/data_alpha/0/, camera/data_bravo/0/   Minecraft client install files (not data; no need to copy)
aligned/*_camera.mp4 + *_camera_meta.json    the videos cut frame by frame to the actions
world_events_report.json                     the world-log check results
```

Per episode in `output/` (`TS` = timestamp, `EEEEEE` = episode number, `III` = instance number):

| file | content |
|---|---|
| `TS_EEEEEE_Alpha_instance_III.json` / `..._Bravo_...json` | per-frame actions + position + absolute yaw/pitch + inventory + time |
| `..._meta.json` | recording metadata |
| `..._episode_info.json` | episode type, real seed, world type, error flags, `world_state`, `state_metadata` |
| `TS_EEEEEE_world_instance_III_world_events.jsonl` | the world-state event log (shared by both players) |
| `TS_EEEEEE_world_instance_III_world_snapshot_start.json.gz` | the start block snapshot |
| `TS_EEEEEE_world_instance_III_world_snapshot_end.json.gz` | the end block snapshot |

Data per hour (1 instance; estimates, so measure with `du -sh` after the first short test):

| content | about per hour |
|---|---|
| the two raw MKV recordings | 1.5–3 GB |
| the aligned MP4s (both players) | 2–6 GB |
| the per-frame action JSON (both players) | 0.2–0.5 GB |
| the world event logs | 30–60 MB |
| block snapshots (blocks mode, 2 per episode, about 20–150 KB each) | 5–25 MB |
| region files (region / both mode) | 1–5 MB more per episode |
| total | about 4–10 GB per hour |

That is about 50–70 new episodes per hour: 30–60 s each, plus 10–20 s for teleport and set-up.

---

## 8. How to tell a run went well

1. The check results are printed at the end of the run. You can also run the check by hand:

   ```bash
   python3 postprocess/verify_world_events.py output_laptop/data_collection/train/batch_0
   ```

   It prints one line per episode, for example:

   ```
   [OK] episode 000003 instance 000 type=tntBlast records=1543 changes=212 ticks=1180 replay_mismatch=0 | Alpha: 1102 frames, 212/212 changes inside frame span, Bravo: 1098 frames, 212/212 changes inside frame span
   ```

   What to look for:
   - The status is `OK` and `replay_mismatch=0`: the start snapshot plus the replayed log equals the end snapshot. A few mismatches show as `WARN`, and the report lists the blocks.
   - `changes=` is above 0 (the new episodes always change blocks).
   - Both players have `frames` above 0, and most changes fall inside the frames' time span (`x/y changes inside frame span`).
   - There is no `some frames lack yaw/pitch` error.
2. File count: `ls output_laptop/data_collection/train/batch_0/output/*_world_events.jsonl | wc -l` should be about the number of finished episodes.
3. Spot-check one log:

   ```bash
   f=$(ls output_laptop/data_collection/train/batch_0/output/*_world_events.jsonl | head -1)
   head -1 "$f" | python3 -m json.tool | head -40          # episode_begin: seed, area, gamerules
   grep -c '"type":"tick"' "$f"                            # about the episode's seconds x 20
   grep -o '"type":"[a-z_]*"' "$f" | sort | uniq -c        # record counts by type
   ```
4. The server log should contain `[worldstate] begin` and `[worldstate] end`:

   ```bash
   grep -c "\[worldstate\] begin" output_laptop/data_collection/train/batch_0/logs/docker-compose-000/mc_instance_0.log
   ```
5. Open a few `aligned/*_camera.mp4` files and check that both views caught the lighting, melting, explosions and so on.

---

## 9. Copying the data out

Copy the whole output directory (by default `~/MineEngine/capture/output_laptop/`). `camera/data_alpha` and `camera/data_bravo` are Minecraft client install files and can be left out.

---

## 10. The Minecraft EULA

- The server container (itzg/minecraft-server, Paper 1.21) starts only with the environment variable `EULA=TRUE`, which means that the person running it accepts the [Minecraft EULA](https://aka.ms/MinecraftEULA). Upstream writes `TRUE` by default. Here nothing accepts the EULA for you: `run_laptop.sh`, `run.sh` and `run_evals.sh` all stop unless you add `--accept-minecraft-eula` yourself (`run.sh --eula TRUE` also works).
- The server runs in offline mode (`online-mode=false`). The bots and camera clients log in with offline identities; no Microsoft or Mojang account is needed or used.
- The cameras use the official Java Edition client files, which `minecraft-launcher-lib` downloads from Mojang's servers into `camera/data_*`.
- You need your own Minecraft: Java Edition licence. Offline mode only lets the automated local capture run without logging in; it does not change that.

---

## 11. Common problems

| symptom | what to do |
|---|---|
| `server/build_plugin.sh` fails | Usually a network problem reaching `repo.papermc.io` or Maven Central: retry or change network. |
| A camera container reports `unsatisfied condition: cuda>=13.0` | Check that you are using `run_laptop.sh` (it adds `--nvidia-disable-require`), or upgrade to a 580+ driver. |
| A camera container cannot find the `nvidia` runtime | Redo `nvidia-ctk runtime configure` from section 3.3 and restart Docker. |
| A port is in use | Close the program holding 25590, 25600, 5901/5902 or 6901/6902. |
| Out of memory, the system freezes | Close the browser and other programs; if needed, add `-- --server-memory 1G`. |
| Many `replay_mismatch` in the check report | Look at the mismatching blocks in `world_events_report.json`; usually the log missed some kind of block change. You can open an issue with the report and that episode's files. |
| `encountered_error` is true for some episodes | A few are normal (a bot failed to path or place) and the data is still usable. If the share is high, read the controller logs. |
| You want to start over | Use another `--output-dir`, or delete the old output directory. |

---

## 12. Known limits

- Block changes made with server commands during recording (`/setblock`, `/fill`) do not appear as events. They show up only as differences between the end snapshot and the replay. The new episodes never do this.
- Block-entity data such as chest contents and sign text is only in the `.mca` files of `region` mode.
- Snapshots cover only the episode area (radius 32 by default). Changes outside it are still logged as events (within 64 blocks by default) but have no start snapshot.
- The restore (`--reset-world`) covers only the snapshot area and is not logged.
- On a new machine, run a short `--episodes 5` test first and check that the logs and the check report look right.
