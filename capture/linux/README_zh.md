# 无头 Linux

[English](README.md) | 中文

这套做法让原生运行器 `windows/run_windows.py` 在无头的 Linux 作业容器里运行。运行器会启动 Paper、两个 bot，以及两个装了 `solaris-capture` 模组的相机客户端。
- 只用一个 bash 脚本，不需要 Docker。
- GPU 节点可以完全没有外网。
- Windows 上的行为不变：新参数都是可选的，它们在 Windows 上的默认值就是原来的行为。

Docker 路径（`run.sh`、`camera/entrypoint.sh`）在这里不能用。它跑的是原版客户端 + x11grab，不加载模组，也不写 `camera_<bot>_frames.jsonl`。

这套做法已在 RTX 6000D（Blackwell）GPU 集群上大规模实测：每张卡 4 个实例，两轮共采约 3000 局。第 6 节是我们当时验证过的清单。

## 1. Linux 上改了什么

| 项 | Linux 上的做法 |
|---|---|
| 显示 | `--xvfb BIN` 为每个 batch 启动一个 Xvfb，作为受管服务：最先启动、最后关闭，日志写到 `logs/xvfb.log`，它一旦退出，这个 batch 就中止。<ul><li>`--display`（默认 `:99`）作为 `DISPLAY` 交给相机进程。没有 `--xvfb` 时，用 `--display` 或环境里的 `DISPLAY`。</li><li>屏幕默认是 `<width>x<height>x24`（`--xvfb-screen`）。</li><li>其他参数放在 `--xvfb-args` 里，默认是 `+extension GLX +render -noreset`，与 `camera/entrypoint.sh` 相同。</li></ul> |
| GPU GL | `--camera-prefix 'vglrun -d egl'` 加在每条相机启动命令前面。Minecraft 和模组启动的 ffmpeg 继承它的环境（`LD_PRELOAD` 等）。<ul><li>运行器的这些变量会写进相机服务的 env，可以在 `run_config.json` 里查到：`DISPLAY`、`VGL_*`、`__EGL_VENDOR_LIBRARY_DIRS`、`__EGL_VENDOR_LIBRARY_FILENAMES`、`__GLX_VENDOR_LIBRARY_NAME`、`LD_LIBRARY_PATH`。</li><li>`--camera-env KEY=VALUE` 可以追加或覆盖其中一个。</li></ul> |
| 停止 | SIGTERM 的效果与第一次 Ctrl+C 相同：<ol><li>写 stop 文件；</li><li>等相机关闭录像（`--camera-stop-timeout`，默认 60 s）；</li><li>停录制器（5 s）；</li><li>发 RCON `stop`（最多约 105 s）。</li></ol>但**不**跑对齐和校验，因为作业随时可能被 SIGKILL。这时 `run_config.json` 里有 `postprocess.skipped = "SIGTERM"`，之后在 CPU 作业里补跑（第 3 节）。<ul><li>如果 SIGTERM 在对齐期间到达，还没开始的对齐任务会被跳过。</li><li>再来一次 Ctrl+C，或距第一次超过 5 s 的 SIGTERM，会立即强杀全部进程。</li><li>5 s 内重复的 SIGTERM（进程组信号加上包装脚本转发的那一次）会被忽略。</li><li>`abort_reason` 为 `SIGTERM`。</li></ul> |
| 强杀 | Linux 没有 Job Object。每个服务先杀自己的进程组，再杀掉动手前从 `/proc` 找到的、已经离开该组的后代进程。<ul><li>在 Linux 上，运行器给 `launch_camera.py` 加 `--keep-process-group`。这样 Minecraft 和模组的 ffmpeg 留在启动器的进程组里，相机超时被杀或全部强杀时会一起结束。</li><li>手动单独运行 `launch_camera.py` 时，它仍然有自己的会话。</li></ul> |
| 离线 | `--offline`，见第 4 节。 |
| 采样节奏 | 控制器按 50 ms 的 deadline 网格采样。在 Linux 上，运行器默认导出 `RECORDER_CADENCE=deadline`，与 Windows 相同。<ul><li>`frame-recorder.js` 以前在 Linux 上默认用 `after`，约 51 ms 一个样本；`--recorder-cadence after` 可以恢复这个行为。</li><li>Docker 路径不设这个变量，行为不变。</li></ul> |
| 皮肤 | `skins.js` 在 Windows 以外的系统上默认用 `signed`，这是为 Docker 的原版客户端准备的。原生运行器的相机跑的是模组，所以在 Linux 上运行器导出 `SKIN_MODE=auto`，与 Windows 相同。<ul><li>环境里已经设了 `SKIN_MODE` 时保留它。</li><li>用 `--offline` 时一律是 `local`。</li></ul> |
| 路径 | 全部都能用参数指定：`--java --node --python --ffmpeg-dir --paper-jar --paper-repo`（新）`--mc-home --mod-jar --camera-java`。`--camera-java` 是新参数，默认用 mc_home 里的 Mojang 运行时。 |
| 安装 | `windows/setup_windows.py --platform linux`（在 Linux 上是默认值）对 Windows 的安装流程做了这些改动：<ul><li>不装 pywin32；</li><li>不下载 ffmpeg，也不下载 Maven，而是从 PATH、`--ffmpeg-dir` 和 `--maven` 找；</li><li>MirrorBot 插件不在仓库里。安装脚本用 Maven 构建它，或者保留 `server/plugins/` 里已有的构建好的 jar（第 2 节）。两者都没有时，它会停在 `plugin` 这一步。</li></ul>`--no-venv` 直接用当前解释器，比如一个 conda 环境。 |
| EULA | 未改。必须由操作者本人加 `--accept-minecraft-eula`，运行器才会写 `eula.txt` 并启动 Paper。示例脚本只在设置了 `ACCEPT_MINECRAFT_EULA=1` 时才加这个参数。 |

## 2. 暂存布局（一次性）

如果 GPU 节点没有外网，就在能访问 Mojang、Paper、Fabric、GitHub、npm、Maven 和 Gradle 的 CPU 作业或登录机上，把所有东西一次性暂存好。采集节点只读这些暂存文件。需要 GitHub 或 Maven 的文件也可以从别的机器拷过来（见下文）。

```
$STAGE/
  repo/                                本仓库的一份检出
    capture/                           采集代码目录（第 3 节的 REPO）
      node_modules/                    见下文
      server/plugin-starter/node_modules/
      server/plugins/                  MirrorBot-1.0.jar、ProtocolLib.jar、SkinsRestorer.jar（见下文）
      .cache/windows/paper-1.21-130.jar  Paper 1.21 build 130（sha256 见 windows/common.py）
      .cache/windows/paper_repo/       预先打好补丁的 Paperclip 仓库（versions/ libraries/ cache/）
  minecraft/                           Linux 的 mc_home（含 .solaris_install_ok、versions/、libraries/、assets/、runtime/）
  mod/solaris-capture-0.1.0.jar        模组 jar；在 Windows 上构建的也能用（JVM 字节码）
  jdk-21/bin/java                      Paper 用的 Java 21+（镜像里只有更旧的 Java 时需要）
  ffmpeg/ffmpeg, ffmpeg/ffprobe        静态版 ≥ 5.1（模组用到 -fps_mode / -enc_time_base），含 libx264；h264_nvenc 可选
  xprefix/                             Xvfb + VirtualGL（.deb 用 dpkg -x 解开）
Python 3.10+（conda 环境，路径任意）：numpy、opencv-python-headless、minecraft-launcher-lib==6.5、pyyaml、tqdm、pillow
Node.js 18+ 及 npm（路径任意；安装脚本用 --node，run_headless.sh 用 NODE=；默认取 PATH 里的 node）
```

除非另有说明，下面的命令都在 `$STAGE/repo/capture` 里运行。

- **node_modules。** `package.json` 有两个 GitHub 依赖（mineflayer、mineflayer-pathfinder），所以在不能访问 GitHub 的节点上 `npm ci` 会失败。有两种办法：
  - 拷一份 Windows 机器上跑过 `npm ci --ignore-scripts` 的 `node_modules`。`package-lock.json` 里没有限定操作系统或 CPU 的包，canvas / gl 的原生模块只在打开 viewer 渲染时才加载；
  - 在能访问 GitHub 的机器上运行 `npm ci --ignore-scripts`。

  `server/plugin-starter/node_modules` 里只有 rcon-client。
- **MirrorBot 插件。** 仓库里没有这个 jar，它由 `server/episode-manager-plugin` 构建。任选一种：
  - 在下面的安装命令里加 `--maven /path/to/mvn`。这时 Maven 要能访问 `server/episode-manager-plugin/pom.xml` 里列出的仓库（Maven Central、PaperMC、Sonatype、CodeMC）；
  - 在有 Docker 的机器上运行 `bash server/build_plugin.sh`；
  - 在别的机器上构建好，拷到 `server/plugins/MirrorBot-1.0.jar`。安装脚本会保留含有 `WorldStateLogger.class` 的 jar。

  既没有 Maven 也没有 jar 时，安装脚本会停在 `plugin` 这一步（"Maven not found and --platform linux (pass --maven)"）。
- **Paper + mc_home。** 在能访问 Mojang、Paper 和 Fabric（用 `--maven` 时还要能访问那些 Maven 仓库）的 CPU 作业里，用一个已经装好依赖的 Python 环境运行：
  ```bash
  cd $STAGE/repo/capture
  $PY windows/setup_windows.py --platform linux --no-venv --skip-pip --skip-npm --skip-mod-build \
      --java $STAGE/jdk-21/bin/java --ffmpeg-dir $STAGE/ffmpeg --mc-home $STAGE/minecraft \
      --maven /path/to/mvn
  ```
  如果拷的是构建好的 MirrorBot jar，就去掉 `--maven`。这条命令做四件事：
  - 下载并校验 Paper；
  - 预先打补丁，这一步会下载 Mojang 服务端；
  - 构建 MirrorBot 插件，或保留 `server/plugins/` 里已有的 jar；
  - 运行 `launch_camera.py --install-only`，安装 Linux 版 Minecraft 1.21、Mojang 运行时和 Fabric。

  Windows 机器上已有的 `.cache/windows/paper_repo` 也可以直接拷过来。`setup.json` 记录的是暂存机器上的路径；采集时所有路径都显式传入，不依赖它。
- **Paper 插件和资源文件。** 接着在仓库根目录运行 `scripts/setup_assets.py`，并传入安装脚本装好的客户端 jar：
  ```bash
  cd $STAGE/repo
  $PY scripts/setup_assets.py --jar $STAGE/minecraft/versions/1.21/1.21.jar
  ```
  - 它把 ProtocolLib.jar 和 SkinsRestorer.jar 放进 `capture/server/plugins/`。没有 ProtocolLib，MirrorBot 不会启动。
  - 它还会用你自己的客户端 jar 生成本仓库不附带的文件（默认皮肤等），详见脚本开头的说明。
  - 它从这两个插件的 GitHub 发布页下载。不能访问 GitHub 时，在别的机器上下载这两个 jar，放进同一个目录，再加 `--plugins-from DIR`。两种方式都会核对固定的 sha256。
- **Xvfb / VirtualGL。** 用 `dpkg -x` 把这些 `.deb` 解到 `xprefix/`：
  - 与你镜像的发行版版本对应的 `xvfb` 包及其依赖库；
  - VirtualGL 3.1.x。

  示例脚本会把 `xprefix/usr/lib/x86_64-linux-gnu`、`usr/lib`、`opt/VirtualGL/lib64` 和 `opt/VirtualGL/lib` 加到 `LD_LIBRARY_PATH`（可用 `X_LD` 改）。如果前缀方式不行，而你有 sudo，可以 `sudo dpkg -i` 暂存的 `.deb` 文件（离线）。

## 3. 在调度器作业里跑一个实例

示例脚本是 `linux/run_headless.sh`。每个参数都写成 `KEY=VALUE`。给 `run_windows.py` 的参数放在 `EXTRA` 里，空格写成 `_SP_`（例如 `EXTRA=--skip-postprocess` 或 `EXTRA=--num-flat-world_SP_2`）。

```bash
bash linux/run_headless.sh STAGE=/path/to/stage REPO=/path/to/stage/repo/capture \
     PY=/path/to/env/bin/python OUT=/path/to/output \
     ACCEPT_MINECRAFT_EULA=1 EPISODES=20 MODE=stateful
```

`REPO` 是采集代码目录。其余路径按第 2 节的布局取值；脚本开头的变量可以覆盖它们。

它依次：
1. 检查暂存文件；
2. 为 `libEGL_nvidia.so.0` 写一个 glvnd vendor json，并设置 `__EGL_VENDOR_LIBRARY_DIRS`；
3. 在 `PORTS=auto`（默认）下，取一段空闲端口和一个 X display（`linux/pick_ports.py`），这样多个实例可以共用一张 GPU 和同一个主机网络；
4. 把 mc_home 和 paper_repo 拷到本地盘，因为 Minecraft 运行时会往 mc_home 里解出 natives，Paperclip 也会校验它的仓库；
5. 设置 `VGL_READBACK=none`；
6. 用 `setsid` 在独立会话里后台启动运行器。终端里的 Ctrl+C 或发给脚本进程组的信号都到不了它，只有脚本转发的那一次 SIGTERM 能到。`tee` 忽略 INT / TERM，所以日志管道不会断；
7. 下一个 batch 运行时，拷走已经完成的 batch。`batch_<i+1>` 出现时，`batch_<i>` 就已经完成（包括对齐）。脚本把它拷到 `$OUT/$TAG/data_collection/train/`，并删除本地副本；
8. 运行器退出后，拷走剩下的部分。

等价的运行器命令如下。`$LOCAL` 是本地盘上的一个目录，里面有第 2 步的 vendor json 和第 4 步的副本；`$XPREFIX` 就是 `$STAGE/xprefix`。

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

- **资源。** 一个实例 = 1 个 Paper（2 GB 堆）、2 个相机客户端（各 3 GB 堆）、4 个 node 进程和 2 个 python 进程。
  - 也就是至少约 8 GB Java 堆；按 Windows 上的经验，内存要准备 32 GB 或更多。
  - 用 libx264 编码时，每个相机额外占约 0.5–1 个 CPU 核。
  - `--dry-run` 只打印计划，不启动任何进程。
- **先不开相机测一遍：** `--no-cameras`。它不需要 Xvfb 或 VirtualGL，也不做对齐。
- **停止的时间预算。** 收到 SIGTERM 后，运行器最多需要约 60 + 5 + 105 s 收尾（不含对齐）。之后脚本还要把正在跑的 batch 拷到输出盘。宽限期要覆盖这两段。
  - 宽限期不够时，只丢掉正在跑的那个 batch；之前的已经拷走了。
  - 可以调小 `--camera-stop-timeout`，但 MKV 只有在相机正常退出时才会完整收尾。
  - 想让每个 batch 一结束就拷走、不在 GPU 节点上等对齐，就加 `EXTRA=--skip-postprocess`，全部放到 CPU 作业里对齐。
- **之后补对齐**（CPU 作业即可；PATH 里要有 ffmpeg / ffprobe）：
  ```bash
  cd $REPO && export PATH=$STAGE/ffmpeg:$PATH
  $PY orchestrate.py postprocess --output-dir <batch_dir>/aligned --workers 4
  $PY postprocess/verify_world_events.py <batch_dir> --report <batch_dir>/world_events_report.json
  ```
- 被调度器重启的作业会从头开始。不显式给 `TAG` 时，它会得到一个新的 `TAG`（主机名 + 时间），不会覆盖之前的输出。

## 4. `--offline` 做了什么

- **控制器：** `SKIN_MODE=local`。只用无签名皮肤，由模组从本地皮肤池渲染，从不访问 `textures.minecraft.net`。
- **每个实例的服务器副本**（`batch_*/data/<k>/plugins/`；仓库里的 `server/plugins` 不改）。仓库自带的配置文件本来就是这些值；`--offline` 会在每个实例的副本里再设一遍，以防这些文件被换过：
  - SkinsRestorer 设为 `advanced.noConnections: true` 和 `api.fetchRecommendedSkins: false`；
  - 写入 `plugins/SkinsRestorer/noupdate.txt`，这是该插件文档给出的关闭更新的开关；
  - bStats 设为 `enabled: false`。

  某个键没找到时只打印 WARNING，保持原样。这些改动记录在 `logs/docker-compose-000/prep_data_instance_0.log`（实例 0）。
- **相机：** `launch_camera.py --offline`。mc_home 没有暂存好时，它直接报错退出，不会尝试下载。
- **不变的部分：** 服务器的 `online-mode=false` 和客户端的离线登录（token `0`）。这些本来就不需要网络。

## 5. 跑完先看什么

- `camera/output_<bot>/<k>/camera_<bot>_meta.json`：
  - `gl_renderer` / `gl_vendor` 是 NVIDIA，不是 llvmpipe；
  - `video_encoder` 是 `h264_nvenc`，或回退的 `libx264`；
  - `platform` 是 `linux`；
  - `capture_stats` 里的 `dropped_writer_backlog`、`slots_skipped` 和 `mean_capture_fps`（约 20）。
- `camera_<bot>_frames.jsonl` 的行数等于 MKV 的帧数（`ffprobe -count_frames`）。
- `compose_configs/run_config.json`：
  - 控制器 env 里有 `RECORDER_CADENCE=deadline` 和 `SKIN_MODE`；
  - 相机 env 里有 `DISPLAY` 和 VGL 变量；
  - `batch_services.xvfb` 记录了 Xvfb 服务。

  动作 JSON 里的样本间隔均值应为 50 ms。
- 对齐结果（`aligned/*_camera.mp4`）和 `world_events_report.json`（verify 退出码为 0）。

## 6. 新集群上的检查清单

大规模采集之前，我们逐项过了这份清单。换一种新机器时，从这里开始。

1. **从 `dpkg -x` 前缀启动 Xvfb。** Xvfb 按编译时的路径找 `xkbcomp` 和 XKB 数据（`/usr/bin/xkbcomp`、`/usr/share/X11/xkb`）。镜像里没有它们时，键盘初始化会失败，Xvfb 随即退出。先在一个探测作业里单独启动它一次。如果失败，任选一种：
   - 在 `--xvfb-args` 里加 `-xkbdir <prefix>/usr/share/X11/xkb`，并把 xkbcomp 放到编译时的路径；
   - 用 `sudo dpkg -i` 安装。
2. **VirtualGL 的 faker 库。** 检查 `vglrun -d egl` 能在前缀里找到它们（`LD_PRELOAD` 用的是库名）。`vglrun -d egl glxinfo -B` 报告的 renderer 应为 NVIDIA。
3. **VirtualGL 下的模组。** 上游只测过原版客户端 + x11grab。请检查：
   - 隐藏窗口（GLFW `VISIBLE=false`）在 VirtualGL 下能正常渲染；
   - `VGL_READBACK=none` 可用，以及它相对 `pbo` 的 CPU 开销；
   - Xvfb 不开 GLX 扩展（从 `--xvfb-args` 去掉 `+extension GLX`）是否也行。
4. **静态 ffmpeg 里的 NVENC。** 检查 `h264_nvenc` 已编译进去，并且驱动接受它的 NVENC API 版本。不行时，模组的 `auto` 会回退到 libx264；看 meta 文件里的 `video_encoder`，以及 `capture_stats` 是否显示丢帧。
5. **离线下的客户端。**
   - 原版客户端仍会尝试访问 Mojang 服务（profile keys、Realms 等）。如果容器里的 DNS 解析要等到超时，启动和进服可能会变慢。在无网节点上完整跑一局。
   - ProtocolLib 的更新检查也会失败；它应该只打日志。
   - `episode start` 时的初始皮肤（session_a / session_b）是本地皮肤，离线也能显示。
6. **SIGTERM。** 弄清楚：
   - 调度器停作业时，信号发给谁（只发给脚本，还是整个进程组 / 所有进程）；
   - 宽限期有多长。

   强杀之后（相机超时、第二次信号），`ps` 里不应再有 java 或 ffmpeg。`/proc` 后代清理只在 Linux 上有效。
7. **资源。** 在 GPU 节点上看一个实例的 CPU、内存和丢帧。然后试一个容器里跑多个实例（用 `run_headless.sh` 时写 `EXTRA=--num-flat-world_SP_N`，直接跑 `run_windows.py` 时用 `--num-flat-world N`，最多 4 个），或用 `PORTS=auto` 让一张 GPU 跑多个容器。
8. **我们的大规模采集与默认设置的不同之处。** 它们用的是这个脚本的流程和端口分配（`PORTS=auto`），但 Xvfb 和 VirtualGL 是用 `sudo dpkg -i` 装的（`X_LD=` 设为空），而不是从 `dpkg -x` 前缀运行。在新机器上，先检查前缀方式（第 1 项）。
