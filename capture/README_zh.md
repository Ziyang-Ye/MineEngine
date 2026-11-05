# 采集部分：Minecraft 多人数据采集（基于 SolarisEngine，Windows 原生 + 无头 Linux）

[English](README.md) | 中文

基于 SolarisEngine（Apache-2.0）修改扩展，起点是上游 2026-07-23 的提交 `78f23e7`。和上层的 `mineengine` 白模流水线一起发布（仓库根目录 README.md）。用途：采集**两名玩家同时录制的第一视角 Minecraft 视频**，并附带**可精确重放的世界状态**（白模真值）、逐帧动作和每帧实际渲染的相机位姿。

- 上游 SolarisEngine 文档：https://solaris-wm.github.io/solaris-engine/（源码在 `docs/source/`）；`STATEFUL_CAPTURE_zh.md`（Docker 用法：皮肤要先上传到 MineSkin 签名，端口对网络开放）；无头 Linux：`linux/README_zh.md`。

## 和 Solaris 原版相比做了什么

| 方面 | 内容 |
|---|---|
| 运行方式 | Windows 本机原生运行，不需要 Docker、WSL：`windows/run_windows.py`。Linux/Docker 原路径保留 |
| 相机录制 | Fabric 模组 `camera/capture-mod` 在客户端内部截帧（后台、窗口可隐藏），NVENC/x264 编码；逐帧记录**实际渲染的相机位姿和 FOV**（`camera_<bot>_frames.jsonl`） |
| 相机精度（T12） | bot 发出的位姿经本机 UDP 直接送给相机（pose link），按位姿包序号把动作标签和视频帧配对 |
| 视场角（T13） | 固定 70°，`fovEffectScale=0`（修复 Solaris 实际 77° 的问题） |
| 世界状态 | 每 tick 实体记录（T3）、伤害/击退/死亡、挥手/用物品/切换手持物（T15）、挖掘进度（T17）、相机自身位姿；被碰过的方块每 tick 回读 + 每秒整区域差分，保证 `replay_mismatch=0` |
| 每帧状态（T16） | 快捷栏、护甲、血量饥饿、状态效果、准星指向（含动作发生时刻的准星） |
| 环境（T1/T14） | 白天为主（黄昏/夜晚/黎明约 15%），晴/雨/雷/雪；战斗回合真实伤害 |
| 多样性 | 45+ 种回合（建造、破坏挖掘、战斗、生物、骑乘、种地、红石、流体、雪冰……），`--mode diverse` 按类别比例抽样；正常地形按回合定向传送到 45 种群系；超平坦 25 种地表 + 随机换群系；环境动物；扩展材质池 |
| 角色外观 | 300 个程序生成皮肤 + 24 个留出测试用皮肤 + 18 个 Minecraft 默认皮肤（安装时从你自己的客户端 jar 里提取，`scripts/setup_assets.py`），每回合随机分配；用 Windows / Linux 原生方式采集时由采集模组**本地渲染、不依赖任何外部服务**；每个皮肤有正/背/侧面参考图（`server/skins/reference/`） |
| 画面清洁 | 相机里屏蔽弹窗、隐藏聊天，HUD 保留 |
| 质检 | `postprocess/verify_world_events.py`：重放一致性、T12 角度误差、互相可见比例、伤害/挥手/挖掘一致性等 |

表里和代码注释里的 T 编号（T0–T20），完整对照见 [../docs/requirements_zh.md](../docs/requirements_zh.md)。

字段说明：`OUTPUT_SCHEMA.md`（上游格式）+ `windows/schema_parts/*.md`（本仓库新增的每个组件的字段）。组件之间的接口约定：`windows/CONTRACT.md`。

## 目录速览

```
windows/            Windows 原生编排：run_windows.py、setup_windows.py、common.py、CONTRACT.md、schema_parts/
camera/capture-mod/ 相机录制模组（Gradle + Fabric Loom，MC 1.21）
camera/windows/     相机启动器 launch_camera.py
controller/         Mineflayer bot：episodes-loop.js、episode-handlers/（含 state/、diverse/）、recorder/、utils/
server/             Paper 插件源码 episode-manager-plugin/、皮肤池 skins/
postprocess/        对齐、校验、数据集制作、tests/
tools/skins/        皮肤生成、参考图渲染
```

## Windows 快速开始

前提：Windows 10/11，NVIDIA 显卡，JDK 21+（`java` 在 PATH 上），Node 18+，Python 3.10+。完整说明见 [windows/README_zh.md](windows/README_zh.md)。

```bat
rem 一次性：建 venv、装依赖、下载 Paper、编译插件和模组、安装 MC 客户端
setup_windows.cmd
rem 一次性：下载 ProtocolLib 和 SkinsRestorer（校验 sha256），从你的客户端 jar 里提取默认皮肤和 HUD 模板
.venv\Scripts\python ..\scripts\setup_assets.py
rem 采集
run_windows.cmd --accept-minecraft-eula --mode diverse --episodes 20 --output-dir output_win
```

这几条命令都在 `capture/` 目录里运行。没有 ProtocolLib，MirrorBot 插件不会启动，所以 `setup_assets.py` 不能跳过。

- `--accept-minecraft-eula` 表示**运行者本人**同意 Minecraft EULA（https://aka.ms/MinecraftEULA）。
- 服务器和所有客户端都是离线模式，不需要、也不要使用任何个人微软/Mojang 账号。
- 你需要自己拥有正版 Minecraft: Java 版。离线模式只是让本地自动采集不用登录，并不改变这一点。
- NVIDIA 驱动 < 610 时，NVENC 需要 ffmpeg 7.x（`--ffmpeg-dir` 指向 ffmpeg 7.0.2 的 bin），否则自动退回 CPU 编码。
- 常用参数：`--num-flat-world N --num-normal-world M`（多世界并行）、`--no-cameras`（只测逻辑）、`--dry-run`。完整列表：`run_windows.cmd --help`。
- 输出目录结构与 Docker 版完全相同（见 `OUTPUT_SCHEMA.md` 第 1 节）；每批结束自动对齐和校验，结果在 `world_events_report.json`。

## 当前状态与已知问题

- 已完成并实测：上表全部功能；1 个世界（2 路相机）稳定 20 fps，重放全部精确。
- 正在开发：tick 锁定采集（动作标签与帧逐 tick 一一对应，修复高负载/快速转头时的配对误差）、无人值守连续采集脚本 `windows/collect.py`、水域/洞穴/下界与更多交互回合、遮挡感知的可见率与多样性报表。
- 笔记本（i9-13980HX / 32 GB / RTX 4070 8 GB）上 2 个世界（4 路相机）接近上限。
- 无头 Linux GPU 服务器：`linux/run_headless.sh`（Xvfb + VirtualGL/EGL，自动分配端口，一张卡可跑多个实例），说明见 `linux/README_zh.md`。

## 许可与注意

- 上游 SolarisEngine：Apache-2.0（`LICENSE`）。仓库根目录的 `NOTICE` 写明了改动范围。
- 仓库里不放任何 Minecraft 游戏文件（客户端/服务端 jar、默认皮肤、方块模型、HUD 图标），都由 `scripts/setup_assets.py` 从你自己的安装里读取。仓库里唯一的 Minecraft 内容是截图里的游戏画面（见 `NOTICE`）。
- 第三方插件（ProtocolLib、SkinsRestorer）不放进仓库，安装时从官方发布页下载并校验 sha256，见 `THIRD_PARTY.md`。仓库里只留了两个插件配置文件，都改成了不联网：`server/plugins/SkinsRestorer/config.yml`（SkinsRestorer 的默认文件，沿用它的 GPL-3.0，改了 `advanced.noConnections: true` 和 `api.fetchRecommendedSkins: false`）和 `server/plugins/bStats/config.yml`（`enabled: false`，去掉了服务器 id）。MirrorBot 插件由 `setup_windows.cmd` 或 `server/build_plugin.sh` 从源码编译。
- 会话开局用的两个皮肤 `server/skins/session_a.png` / `session_b.png` 是程序生成皮肤的副本。原生方式采集时不需要给它们做签名缓存。
- 网络：原生方式采集时，Paper 服务器（游戏端口和 RCON）、动作记录器和相机位姿通道只监听 `127.0.0.1`；bot 协调端口（默认 8120/8121，每多一个实例加 30）监听所有网卡，所以请在可信的网络里采集。RCON 用固定密码 `research`，同一台机器上的其他用户也能连上。Docker 路径下，游戏端口和 RCON 监听所有网卡，见 `STATEFUL_CAPTURE_zh.md` 第 4.2 节。
- 采集结果里记有你这台机器上的绝对路径（例如 `run_config.json` 和对齐后的 `*_meta.json`），分享数据集之前请先检查。
