# Headless Linux

English | [中文](README_zh.md)

This setup runs the native runner `windows/run_windows.py` inside a headless Linux job container. The runner starts Paper, two bots and two camera clients with the `solaris-capture` mod.
- It is one bash script, with no Docker.
- GPU nodes can have no internet access at all.
- Behaviour on Windows is unchanged: all new options are optional, and their defaults on Windows are the old behaviour.

The Docker path (`run.sh`, `camera/entrypoint.sh`) cannot be used here. It runs the vanilla client with x11grab, loads no mod, and writes no `camera_<bot>_frames.jsonl`.

It has been tested at scale on an RTX 6000D (Blackwell) GPU cluster: 4 instances per GPU, about 3,000 episodes in two waves. Section 6 is the checklist we verified at the time.

## 1. What changes on Linux

| item | on Linux |
|---|---|
| display | `--xvfb BIN` starts one Xvfb per batch as a managed service. It starts first and stops last, logs to `logs/xvfb.log`, and the batch aborts if it dies. <ul><li>`--display` (default `:99`) is passed to the camera processes as `DISPLAY`. Without `--xvfb`, `--display` or the `DISPLAY` from the environment is used.</li><li>The screen is `<width>x<height>x24` by default (`--xvfb-screen`).</li><li>Other options go in `--xvfb-args`; the default is `+extension GLX +render -noreset`, as in `camera/entrypoint.sh`.</li></ul> |
| GPU GL | `--camera-prefix 'vglrun -d egl'` is put in front of every camera launch command. Minecraft, and the ffmpeg the mod starts, inherit its environment (`LD_PRELOAD` and so on). <ul><li>These variables of the runner are written into the camera services' env, which you can check in `run_config.json`: `DISPLAY`, `VGL_*`, `__EGL_VENDOR_LIBRARY_DIRS`, `__EGL_VENDOR_LIBRARY_FILENAMES`, `__GLX_VENDOR_LIBRARY_NAME`, `LD_LIBRARY_PATH`.</li><li>`--camera-env KEY=VALUE` adds or overrides one.</li></ul> |
| stopping | SIGTERM acts like the first Ctrl+C: <ol><li>write the stop file;</li><li>wait for the cameras to close their recordings (`--camera-stop-timeout`, default 60 s);</li><li>stop the recorders (5 s);</li><li>send RCON `stop` (up to about 105 s).</li></ol> Alignment and verification are **not** run, because the job may be SIGKILLed at any moment. `run_config.json` then has `postprocess.skipped = "SIGTERM"`, and you run them later in a CPU job (section 3). <ul><li>If the SIGTERM arrives during alignment, the alignment tasks not yet started are skipped.</li><li>A second Ctrl+C, or a SIGTERM more than 5 s after the first, kills every process at once.</li><li>A repeat SIGTERM within 5 s (a process-group signal plus the one the wrapper forwards) is ignored.</li><li>`abort_reason` is `SIGTERM`.</li></ul> |
| force kill | Linux has no Job Object. Each service kills its process group, then the descendants found in `/proc` before the kill that had left the group. <ul><li>On Linux the runner passes `--keep-process-group` to `launch_camera.py`. Minecraft and the mod's ffmpeg therefore stay in the launcher's group and die with it when a camera times out or everything is killed.</li><li>`launch_camera.py` run by hand still gets its own session.</li></ul> |
| offline | `--offline`, section 4. |
| sampling cadence | The controller samples on a 50 ms deadline grid. On Linux the runner exports `RECORDER_CADENCE=deadline` by default, as on Windows. <ul><li>`frame-recorder.js` used to default to `after` on Linux, about 51 ms per sample; `--recorder-cadence after` brings that back.</li><li>The Docker path does not set the variable and is unchanged.</li></ul> |
| skins | `skins.js` defaults to `signed` off Windows, for Docker's vanilla client. The native runner's cameras run the mod, so on Linux the runner exports `SKIN_MODE=auto`, as on Windows. <ul><li>A `SKIN_MODE` already set in the environment is kept.</li><li>With `--offline` the mode is always `local`.</li></ul> |
| paths | Everything can be given as an option: `--java --node --python --ffmpeg-dir --paper-jar --paper-repo` (new) `--mc-home --mod-jar --camera-java`. `--camera-java` is new and defaults to the Mojang runtime in mc_home. |
| setup | `windows/setup_windows.py --platform linux` (the default on Linux) changes the Windows setup: <ul><li>it installs no pywin32;</li><li>it downloads neither ffmpeg nor Maven: it takes them from PATH, `--ffmpeg-dir` and `--maven`;</li><li>the MirrorBot plugin is not in the repository. Setup builds it with Maven, or keeps a built jar already in `server/plugins/` (section 2). With neither, it stops at its `plugin` step.</li></ul> `--no-venv` uses the current interpreter, such as a conda env. |
| EULA | Unchanged. The operator must pass `--accept-minecraft-eula` before the runner writes `eula.txt` and starts Paper. The example script only passes it when `ACCEPT_MINECRAFT_EULA=1` is set. |

## 2. Staging layout (once)

If your GPU nodes have no internet access, stage everything once, in a CPU job or on a login machine that can reach Mojang, Paper, Fabric, GitHub, npm, Maven and Gradle. The capture nodes only read the staged files. Files that need GitHub or Maven can also be copied from another machine (see below).

```
$STAGE/
  repo/                                a checkout of this repository
    capture/                           the capture tree (REPO in section 3)
      node_modules/                    see below
      server/plugin-starter/node_modules/
      server/plugins/                  MirrorBot-1.0.jar, ProtocolLib.jar, SkinsRestorer.jar (see below)
      .cache/windows/paper-1.21-130.jar  Paper 1.21 build 130 (sha256 in windows/common.py)
      .cache/windows/paper_repo/       pre-patched Paperclip repo (versions/ libraries/ cache/)
  minecraft/                           the Linux mc_home (.solaris_install_ok, versions/, libraries/, assets/, runtime/)
  mod/solaris-capture-0.1.0.jar        the mod jar, also usable if built on Windows (JVM bytecode)
  jdk-21/bin/java                      Java 21+ for Paper (if the image has only an older Java)
  ffmpeg/ffmpeg, ffmpeg/ffprobe        static build >= 5.1 (the mod uses -fps_mode / -enc_time_base) with libx264; h264_nvenc optional
  xprefix/                             Xvfb + VirtualGL (.deb files unpacked with dpkg -x)
Python 3.10+ (conda env, any path): numpy, opencv-python-headless, minecraft-launcher-lib==6.5, pyyaml, tqdm, pillow
Node.js 18+ with npm (any path; setup takes --node, run_headless.sh takes NODE=; default: node on PATH)
```

The commands below run in `$STAGE/repo/capture` unless they say otherwise.

- **node_modules.** `package.json` has two GitHub dependencies (mineflayer, mineflayer-pathfinder), so `npm ci` fails on a node without GitHub access. Two ways out:
  - copy the `node_modules` from a Windows machine where `npm ci --ignore-scripts` ran. `package-lock.json` has no OS- or CPU-specific packages, and the native canvas / gl modules are only loaded when viewer rendering is on;
  - run `npm ci --ignore-scripts` on a machine that can reach GitHub.

  `server/plugin-starter/node_modules` only holds rcon-client.
- **MirrorBot plugin.** The jar is not in the repository; it is built from `server/episode-manager-plugin`. Do one of these:
  - pass `--maven /path/to/mvn` to the setup command below. Maven then needs the repositories listed in `server/episode-manager-plugin/pom.xml` (Maven Central, PaperMC, Sonatype, CodeMC);
  - run `bash server/build_plugin.sh` on a machine with Docker;
  - build it on another machine and copy it to `server/plugins/MirrorBot-1.0.jar`. Setup keeps a jar that contains `WorldStateLogger.class`.

  With no Maven and no jar, setup stops at its `plugin` step ("Maven not found and --platform linux (pass --maven)").
- **Paper + mc_home.** In a CPU job with access to Mojang, Paper and Fabric (and the Maven repositories, with `--maven`), and a Python env that already has the dependencies:
  ```bash
  cd $STAGE/repo/capture
  $PY windows/setup_windows.py --platform linux --no-venv --skip-pip --skip-npm --skip-mod-build \
      --java $STAGE/jdk-21/bin/java --ffmpeg-dir $STAGE/ffmpeg --mc-home $STAGE/minecraft \
      --maven /path/to/mvn
  ```
  Leave out `--maven` if you copied a built MirrorBot jar. The command does four things:
  - downloads and checks Paper;
  - pre-patches it, which downloads the Mojang server;
  - builds the MirrorBot plugin, or keeps the jar already in `server/plugins/`;
  - runs `launch_camera.py --install-only`, which installs Minecraft 1.21 for Linux with the Mojang runtime and Fabric.

  A `.cache/windows/paper_repo` from a Windows machine can be copied over as well. `setup.json` records the staging machine's paths. Capture passes every path explicitly and does not depend on it.
- **Paper plugins and assets.** Then run `scripts/setup_assets.py` from the repository root, with the client jar that setup installed:
  ```bash
  cd $STAGE/repo
  $PY scripts/setup_assets.py --jar $STAGE/minecraft/versions/1.21/1.21.jar
  ```
  - It puts ProtocolLib.jar and SkinsRestorer.jar into `capture/server/plugins/`. MirrorBot does not start without ProtocolLib.
  - It also makes the files this repository does not ship (the default skins and others) from your own client jar; see the top of the script.
  - It downloads the two plugins from their GitHub releases. Without GitHub access, fetch the two jars on another machine, put them in one directory and add `--plugins-from DIR`. Both ways check the pinned sha256.
- **Xvfb / VirtualGL.** Unpack these `.deb` files into `xprefix/` with `dpkg -x`:
  - the `xvfb` package of your image's distribution release, and its libraries;
  - VirtualGL 3.1.x.

  The example script adds `xprefix/usr/lib/x86_64-linux-gnu`, `usr/lib`, `opt/VirtualGL/lib64` and `opt/VirtualGL/lib` to `LD_LIBRARY_PATH` (`X_LD` overrides this). If the prefix approach fails and you have sudo, `sudo dpkg -i` the staged `.deb` files (offline).

## 3. One instance in a scheduler job

The example script is `linux/run_headless.sh`. Every argument is `KEY=VALUE`. Options for `run_windows.py` go in `EXTRA`, with spaces written as `_SP_` (for example `EXTRA=--skip-postprocess` or `EXTRA=--num-flat-world_SP_2`).

```bash
bash linux/run_headless.sh STAGE=/path/to/stage REPO=/path/to/stage/repo/capture \
     PY=/path/to/env/bin/python OUT=/path/to/output \
     ACCEPT_MINECRAFT_EULA=1 EPISODES=20 MODE=stateful
```

`REPO` is the capture tree. The other paths follow the layout of section 2; the variables at the top of the script override them.

In order, it:
1. checks the staged files;
2. writes a glvnd vendor json for `libEGL_nvidia.so.0` and sets `__EGL_VENDOR_LIBRARY_DIRS`;
3. with `PORTS=auto` (the default), takes a free port block and X display (`linux/pick_ports.py`), so several instances can share one GPU and one host network;
4. copies mc_home and paper_repo to local disk, because the Minecraft runtime unpacks natives into mc_home and Paperclip checks its repo;
5. sets `VGL_READBACK=none`;
6. starts the runner in the background in its own session (`setsid`). A Ctrl+C in the terminal, or a signal to the script's process group, does not reach it; only the one SIGTERM the script forwards does. `tee` ignores INT / TERM, so the log pipe stays open;
7. copies each finished batch while the next one runs. Once `batch_<i+1>` exists, `batch_<i>` is complete (aligned too). The script copies it to `$OUT/$TAG/data_collection/train/` and deletes the local copy;
8. copies the rest when the runner exits.

The equivalent runner command. `$LOCAL` is a directory on local disk with the vendor json of step 2 and the copies of step 4; `$XPREFIX` is `$STAGE/xprefix`.

```bash
cd $REPO
export __EGL_VENDOR_LIBRARY_DIRS=$LOCAL/egl_vendor.d VGL_READBACK=none
export LD_LIBRARY_PATH=$XPREFIX/usr/lib/x86_64-linux-gnu:$XPREFIX/usr/lib:$XPREFIX/opt/VirtualGL/lib64:${LD_LIBRARY_PATH:-}
$PY -u windows/run_windows.py --accept-minecraft-eula --offline \
    --output-dir $LOCAL/out --mode stateful --episodes 20 \
    --java $STAGE/jdk-21/bin/java --node $(command -v node) --python $PY --ffmpeg-dir $STAGE/ffmpeg \
    --paper-jar $REPO/.cache/windows/paper-1.21-130.jar --paper-repo $LOCAL/paper_repo \
    --mc-home $LOCAL/minecraft --mod-jar $STAGE/mod/solaris-capture-0.1.0.jar \
    --xvfb $XPREFIX/usr/bin/Xvfb --display :99 --camera-prefix="$XPREFIX/opt/VirtualGL/bin/vglrun -d egl"
```

- **Resources.** One instance is 1 Paper (2 GB heap), 2 camera clients (3 GB heap each), 4 node and 2 python processes.
  - That is at least about 8 GB of Java heap; from the Windows runs, plan for 32 GB of RAM or more.
  - With libx264, each camera takes about 0.5–1 extra CPU core.
  - `--dry-run` prints the plan and starts nothing.
- **A test without cameras:** `--no-cameras`. It needs no Xvfb or VirtualGL and does no alignment.
- **Time budget for stopping.** After a SIGTERM the runner needs up to about 60 + 5 + 105 s to wind down (without alignment). The script then copies the running batch to the output disk. The grace period must cover both.
  - If it is too short, only the running batch is lost; earlier ones were already copied.
  - `--camera-stop-timeout` can be lowered, but an MKV is only finalised when its camera exits normally.
  - To copy each batch as soon as it ends, without waiting for alignment on the GPU node, add `EXTRA=--skip-postprocess` and align everything in a CPU job.
- **Alignment afterwards** (a CPU job is enough; ffmpeg / ffprobe must be on PATH):
  ```bash
  cd $REPO && export PATH=$STAGE/ffmpeg:$PATH
  $PY orchestrate.py postprocess --output-dir <batch_dir>/aligned --workers 4
  $PY postprocess/verify_world_events.py <batch_dir> --report <batch_dir>/world_events_report.json
  ```
- A job restarted by the scheduler starts from scratch. Without an explicit `TAG` it gets a new one (hostname + time) and never overwrites earlier output.

## 4. What `--offline` does

- **Controller:** `SKIN_MODE=local`. It uses only unsigned skins, which the mod renders from the local skin pool, and never contacts `textures.minecraft.net`.
- **Server copy of each instance** (`batch_*/data/<k>/plugins/`; the repository's `server/plugins` is not touched). The shipped configuration files already have these values; `--offline` sets them again in each instance's copy, in case the files were replaced:
  - SkinsRestorer gets `advanced.noConnections: true` and `api.fetchRecommendedSkins: false`;
  - `plugins/SkinsRestorer/noupdate.txt` is written, the switch that plugin's docs give for turning updates off;
  - bStats gets `enabled: false`.

  A key that is not found only prints a WARNING and is left as is. The changes are logged in `logs/docker-compose-000/prep_data_instance_0.log` (instance 0).
- **Cameras:** `launch_camera.py --offline`. If mc_home is not staged, it exits with an error instead of trying to download.
- **Unchanged:** the server's `online-mode=false` and the clients' offline login (token `0`). These never needed the network.

## 5. What to check after a run

- `camera/output_<bot>/<k>/camera_<bot>_meta.json`:
  - `gl_renderer` / `gl_vendor` is NVIDIA, not llvmpipe;
  - `video_encoder` is `h264_nvenc`, or the `libx264` fallback;
  - `platform` is `linux`;
  - in `capture_stats`: `dropped_writer_backlog`, `slots_skipped`, and `mean_capture_fps` (about 20).
- The number of lines in `camera_<bot>_frames.jsonl` equals the MKV's frame count (`ffprobe -count_frames`).
- `compose_configs/run_config.json`:
  - the controller env has `RECORDER_CADENCE=deadline` and `SKIN_MODE`;
  - the camera env has `DISPLAY` and the VGL variables;
  - `batch_services.xvfb` records the Xvfb service.

  The mean sample interval in the action JSON should be 50 ms.
- The aligned output (`aligned/*_camera.mp4`) and `world_events_report.json` (verify exits with code 0).

## 6. Checklist for a new cluster

We went through this list before the large runs. It is the place to start on a new kind of machine.

1. **Xvfb from a `dpkg -x` prefix.** Xvfb looks for `xkbcomp` and the XKB data at their build-time paths (`/usr/bin/xkbcomp`, `/usr/share/X11/xkb`). If the image lacks them, keyboard setup fails and Xvfb exits. Start it once on its own in a probe job. If it fails, do one of these:
   - add `-xkbdir <prefix>/usr/share/X11/xkb` to `--xvfb-args` and put xkbcomp at its build-time path;
   - install with `sudo dpkg -i`.
2. **VirtualGL faker libraries.** Check that `vglrun -d egl` finds them in the prefix (`LD_PRELOAD` uses library names). `vglrun -d egl glxinfo -B` should report an NVIDIA renderer.
3. **The mod under VirtualGL.** Upstream only tested the vanilla client with x11grab. Check:
   - that a hidden window (GLFW `VISIBLE=false`) renders under VirtualGL;
   - that `VGL_READBACK=none` works, and its CPU cost compared with `pbo`;
   - whether Xvfb also works without the GLX extension (drop `+extension GLX` from `--xvfb-args`).
4. **NVENC in a static ffmpeg.** Check that `h264_nvenc` is compiled in and that the driver accepts its NVENC API version. If not, the mod's `auto` falls back to libx264; check `video_encoder` in the meta file and whether `capture_stats` shows dropped frames.
5. **Clients offline.**
   - The vanilla client still tries Mojang services (profile keys, Realms, ...). If DNS lookups in the container wait for a timeout, start-up and joining can be slow. Run one full episode on a node with no network.
   - ProtocolLib's update check also fails; it should only log.
   - The initial skins at `episode start` (session_a / session_b) are local skins and show offline too.
6. **SIGTERM.** Find out:
   - who receives the signal when the scheduler stops a job (only the script, or the whole process group / every process);
   - how long the grace period is.

   After a force kill (camera timeout, second signal), no java or ffmpeg should be left in `ps`. The `/proc` descendant cleanup only works on Linux.
7. **Resources.** Check one instance's CPU, memory and dropped frames on a GPU node. Then try several instances in one container (`EXTRA=--num-flat-world_SP_N` with `run_headless.sh`, or `--num-flat-world N` on the `run_windows.py` command, up to 4), or several containers per GPU with `PORTS=auto`.
8. **How our large runs differed from the defaults.** They ran this script's logic and its port picking (`PORTS=auto`), but Xvfb and VirtualGL were installed with `sudo dpkg -i` (with `X_LD=` empty), not run from a `dpkg -x` prefix. On a new machine, check the prefix path (item 1) first.
