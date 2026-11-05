# Windows 原生运行指南

[English](README.md) | 中文

整套采集直接在 Windows 11 上运行，**不需要 Docker，也不需要 WSL**。
- 原来的每个容器都变成本机上的一个普通进程。除了 bot 协调端口（见第 2 节），所有进程都只监听 `127.0.0.1`。
- 输出目录结构和文件名与 Linux / Docker 版相同（见 `OUTPUT_SCHEMA.md`），所以后处理不用改。

## 相机窗口、分辨率和后台录制

- **不需要开着任何窗口。** 两个相机客户端是普通的 Minecraft 1.21 客户端，加载我们的 `solaris-capture` 模组。
  - 模组从客户端的渲染缓冲区读取画面（GPU 回读），再交给 ffmpeg。
  - 所以窗口默认是**隐藏**的。窗口被别的窗口挡住或移到屏幕外，照样录制，你可以照常使用电脑。
  - 唯一的限制：**不要把相机窗口最小化**，最小化的窗口什么都不渲染。隐藏模式可以避开这个问题。
  - 想看相机画面时加 `--show-cameras`。
- **分辨率可以调高。** 默认 1280×720、20 fps，与公开的 Solaris 数据相同。
  - 需要更高分辨率时用 `--width 1920 --height 1080`。录像分辨率与屏幕大小无关。
  - 分辨率越高，占用的 GPU 时间和磁盘空间越多。
- **录制在后台进行**，每个客户端一路。使用 NVENC 硬件编码；没有 NVENC 时改用 CPU 的 libx264。

## 1. 一次性准备（可以重复执行）

在 `capture/` 目录打开 cmd 或 PowerShell：

```bat
setup_windows.cmd
```

它会依次：
1. 检查 Java（21+）、Node.js（18+）、Python（3.10+）和 ffmpeg；
2. 没有 `.venv` 时创建它，并安装 Python 依赖：numpy、opencv-python-headless、minecraft-launcher-lib 6.5、pywin32、pyyaml、tqdm、pillow；
3. 运行 `npm ci --ignore-scripts`（在 `capture/` 里），并在 `server/plugin-starter` 里运行 `npm install`；
4. 下载 Paper 1.21（build 130，校验 sha256）到 `.cache/windows/`，并预先打补丁。第一次会从 Mojang 下载服务端（约 1 分钟）；
5. 找不到 ffmpeg 时下载它（gyan.dev 的 essentials 版）；
6. 用 Maven 编译 MirrorBot 服务器插件（找不到 Maven 时会下载 Maven 3.9.9 并校验 sha512），并检查 jar 里有 `WorldStateLogger`；
7. 用 Gradle 编译相机模组 `camera/capture-mod`；
8. 安装相机客户端（Minecraft 1.21 + Fabric，放在 `.cache/windows/minecraft`）；
9. 把找到的工具路径写进 `.cache/windows/setup.json`；采集时从这里读取。

已经完成的步骤会跳过，所以随时可以再运行一次。常用开关：
- `--paper-jar <jar>`：使用已有的 Paper jar；
- `--rebuild-plugin` / `--rebuild-mod`：强制重新编译；
- `--force-npm`；
- `--skip-camera-install`。

然后仍在 `capture/` 里，获取本仓库不附带的文件：

```bat
.venv\Scripts\python ..\scripts\setup_assets.py
```

它把 ProtocolLib 和 SkinsRestorer 下载到 `server\plugins\`（校验 sha256），并从第 8 步安装的客户端 jar 中提取默认皮肤和 HUD 模板。没有 ProtocolLib，MirrorBot 就启动不了，所以不要跳过这一步。

## 2. 采集

**请先自己阅读并同意 [Minecraft EULA](https://aka.ms/MinecraftEULA)。** 你需要有自己的 Minecraft: Java Edition 授权；这里使用的离线模式只是让自动采集不用登录也能运行。然后：

```bat
rem 冒烟测试：5 个回合
run_windows.cmd --accept-minecraft-eula --mode stateful --episodes 5 --output-dir output_smoke

rem 试采
run_windows.cmd --accept-minecraft-eula --mode all --episodes 60 --batches 2 --output-dir output_pilot
```

参数和 `run_laptop.sh` 相同：
- `--mode stateful|mix|all|<list of types>`；
- `--episodes`、`--batches`、`--output-dir`；
- `--world-seed`、`--reset-world`、`--normal-world`、`--snapshot-mode`。

`--` 之后的参数会传给 `run.sh`。Windows 版新增的参数：

| 参数 | 含义 |
|---|---|
| `--width 1280 --height 720` | 录像分辨率（默认 1280×720） |
| `--show-cameras` | 显示相机窗口（默认隐藏；录制在后台进行） |
| `--server-memory 2G` | Paper 服务器内存 |
| `--render-distance 12` | 相机渲染距离，同时也是服务器视距 |
| `--encoder auto` | `auto` / `h264_nvenc` / `libx264` |
| `--batch-timeout <seconds>` | 每批最长运行时间，单位为秒（默认不限） |
| `--dry-run` | 打印将要使用的全部命令、环境变量和端口，不启动任何进程 |
| `--no-cameras` | 调试用：只运行服务器和 bot，不开相机（没有视频，也不做对齐） |

每批按以下顺序启动进程：
1. Paper 服务器，然后等待 RCON 就绪；
2. 动作记录器；
3. 两个相机、两个旁观 bot 和启动脚本；
4. 两个控制 bot。

两个控制 bot 都结束后，会通知相机停止录像，并关闭所有进程。服务器通过 RCON `stop` 正常保存。然后对齐视频，用 `verify_world_events.py` 检查本批，最后打印汇总。

端口：
- 只在 `127.0.0.1` 上：游戏 25590、RCON 25600、动作记录器 8110/8111、相机位姿链路 8130/8131（UDP）；
- 在所有网卡上：bot 协调 8120/8121，由 bot 控制器打开。请在可信网络中运行采集，或者在防火墙里保持这些端口关闭。

RCON 使用固定密码 `research`。同一台机器上的其他用户也能连上它。

## 3. 停止

- 在运行窗口里按**一次 Ctrl+C**，正常停止。相机收尾，服务器保存世界，已经录完的回合照常对齐和检查。
- **再按一次 Ctrl+C**，立即结束所有进程。
- 如果关掉窗口或程序崩溃，所有子进程（java、node、ffmpeg）会随之结束（Windows Job Object）。不会留下后台进程，也不需要额外的停止命令。

第一次运行时，Windows 防火墙可能询问是否允许 java / python / node 使用网络。采集只使用本机内部的连接，所以选“取消”不影响采集，而且这样 bot 协调端口对其他机器保持关闭。

## 4. 输出

`<output-dir>\data_collection\train\batch_<k>\`：

```
output\                         每个回合的动作 json、episode_info、world_events.jsonl、快照 .json.gz
aligned\                        对齐后的 *_camera.mp4 + meta
camera\output_alpha\0\          原始录像 camera_alpha.mkv、camera_alpha_meta.json、camera_alpha_frames.jsonl
camera\output_bravo\0\          Bravo 的同类文件
camera\data_alpha\0\ ...        相机客户端的设置和日志（一般用不到）
data\0\                         Paper 服务器目录（世界存档、插件）
logs\docker-compose-000\        每个进程一个日志，文件名与 Docker 版相同（mc_instance_0.log, ...）
logs\run_windows.log            运行脚本自己的日志
logs\postprocess\               对齐和检查的日志
compose_configs\run_config.json 本批的全部命令、选项、端口、工具版本、git 版本和退出码
world_events_report.json        检查报告
```

## 5. 常见问题

- **提示“端口被占用”。** 上一次采集还没退出，或者别的程序占用了 25590/25600/8110/8111/8120/8121/8130/8131 中的某个端口。
- **相机中途退出。** 运行脚本会重启它一次，新录像的文件名以 `_r1` 结尾。窗口和 `run_config.json` 里都会明确标出这次重启。
  - 第一段录像会保留。对齐时，会为每个回合选用覆盖该回合时间段的那段录像。
  - 相机掉线时，服务器也会把它对应的 bot 踢下线（"Your partner left"）。当时正在进行的回合通常会丢失，这个实例之后的回合也可能丢失。
  - 请单独标记这类批次。
- **某个 bot 一直不退出。** 同一实例里一个 bot 结束后，另一个最多再等 `--controller-exit-grace` 秒（默认 300）。这个时限按实例计算，不影响其他实例。超时的 bot 会被强制停止，本批记为 `completed_with_kills`（退出码 1）。
- **服务器启动不了。** 查看 `logs\docker-compose-000\mc_instance_0.log`。
- **没有 NVENC。** 相机会改用 CPU 编码。相机日志和 `camera_*_meta.json` 中的 `video_encoder` 会写明这一点。
