# MineEngine

[English](README.md) | 中文

全自动的 Minecraft 多人数据引擎：两个 bot 自己玩 45 种以上的回合（建造、拆除、PvP、打怪、骑乘、种地、红石、流体……），每一局都录下两名玩家的第一视角视频、逐帧动作、相机位姿和能精确重放的世界状态日志，再把记录下来的世界逐像素渲染成"白模"，画面里每个像素都有真值。

<p align="center"><img src="docs/assets/hero.gif" width="900" alt="同一时刻的两名玩家：左边是真实画面，右边是按记录的世界状态渲染的白模"></p>

左列是游戏里录下的真实画面，右列是白模，上下两行是同一时刻的两名玩家。

- `capture/`：采集引擎。录两名玩家的第一视角视频、逐帧动作、每一帧实际渲染用的相机位姿，以及能精确重放的世界状态日志。
- `mineengine/`：把采集结果变成训练数据。按日志重放世界，逐像素渲染白模：是什么方块、什么状态、哪一面、哪个实体、挖到了第几阶段。同时输出对齐好的片段、每个实体的参考图、统计和可视化。

白模不是从画面估计出来的，而是从记录的世界状态直接渲染的。门开没开、作物长到第几阶段、水位多高、对方玩家在哪，每个像素都是真值。

<p align="center"><img src="docs/assets/pipeline.svg" width="900" alt="采集 -> 精确日志 -> mineengine 白模"></p>

## 每个像素记了什么

<p align="center"><img src="docs/assets/channels.png" width="900" alt="一帧真实画面和它的各个白模通道"></p>

| 通道 | 含义 |
|---|---|
| `btype` | 方块类型：用的是你本机客户端 jar 里的方块注册表，1000 多种 |
| `bstate` | 完整方块状态，例如 `minecraft:oak_door[facing=east,half=lower,open=true]` |
| `face` | 看到的是方块的哪一面（±x、±y、±z） |
| `pid` | 实体编号：1 = 另一名玩家，2 以后是生物、掉落物、点燃的 TNT |
| `fx` | 正在被挖的方块挖到了第几阶段 |
| `logdepth`、`shade`、`edge` | 深度、明暗、方块棱线 |

完整说明见 [docs/whitebox_channels.md](docs/whitebox_channels.md)。

几何形状来自客户端 jar 里的方块模型：台阶、楼梯、门、作物、火把、铁轨、不同水位的水、含水方块都按原样画。白模里不画界面；血量、物品栏、准星指向这些状态记在每一帧的日志里。

## 世界在变，白模跟着变

<p align="center"><img src="docs/assets/tnt.png" width="900" alt="TNT 爆破：上面是真实画面，下面是白模"></p>

放方块、拆方块、爆炸、开门、作物生长、水流，每一次变化都写进日志并重放。白模里炸出的坑和真实的坑一格不差。

## 两个视角，同一个世界

<p align="center"><img src="docs/assets/multiview.png" width="900" alt="两名玩家的视角、对方玩家的精确像素和俯视地图"></p>

- 两名玩家的视频按时间对齐。
- 每一帧都记录了渲染它的相机位置、朝向和视场角。
- 对方玩家在自己画面里占的像素是精确标出来的（洋红色）。

## 能采什么

<p align="center"><img src="docs/assets/episodes.png" width="900" alt="各种回合：上面是真实画面，下面是白模"></p>

- 45 种以上回合类型：建造、拆除、PvP、打怪、骑乘、种地、红石、流体……
- 45 种地形加超平坦世界，天气和时间随机变化。
- 每回合两名玩家一起行动，动作逐帧对齐。

代码注释里的 `T12`（相机同步）、`T16`（每帧玩家状态）这类编号，对照表见 [docs/requirements_zh.md](docs/requirements_zh.md)。

<p align="center"><img src="docs/assets/skins.png" width="900" alt="程序生成的皮肤"></p>

- 皮肤：300 个程序生成皮肤，另有 24 个留作测试。
- 18 个 Minecraft 默认皮肤在安装时从你自己的客户端里提取。
- 每个皮肤都有正面、背面、侧面参考图。
- 用 Windows 或 Linux 原生方式采集时，皮肤全部在本地渲染，不依赖任何在线服务。老的 Docker 路径只能显示 Mojang 签过名的皮肤，要先把皮肤上传到 MineSkin 网站一次（见 [capture/STATEFUL_CAPTURE_zh.md](capture/STATEFUL_CAPTURE_zh.md) 第 3.6 节）。

## 规模

- 可以在 Windows 本机直接运行，也可以在没有显示器的 Linux 服务器上运行（虚拟显示 + GPU 渲染）。
- 我们在 RTX 6000D 显卡上每张卡同时开 4 个采集实例，两轮共采了约 3000 局双人数据。

## 快速开始

需要：
- 采集：NVIDIA 显卡、Java 21、Node 18+、Python 3.10+；只有 `build_plugin.sh` 和 Docker 路径需要 Docker；
- `mineengine`：Python 3.10+，以及 numpy、OpenCV、Pillow、PyTorch、PyAV（`pip install -e .` 会自动装好）。

有 nvdiffrast 时用 GPU 渲染白模（192×336 每帧约 1.5 毫秒），没有也能用自带的 CPU 渲染器（`cap2proxy --cpu-render`），输出的通道相同。GPU 自测里逐像素对比过两者：约千分之一的像素不同，玩家通道完全一致。nvdiffrast 的许可只允许研究和评估用途，商用请用 CPU 渲染器。

```bash
pip install -e .                                   # 安装 mineengine
# 采集环境（Paper 服务端、相机客户端、采集模组）：见 capture/README_zh.md、capture/linux/README_zh.md
python scripts/setup_assets.py                     # 从你自己的客户端 jar 生成方块注册表、提取默认皮肤和 HUD 模板，下载插件并校验
# MirrorBot 插件：Windows 上 setup_windows.cmd 会用 Maven 编译；
# Linux 上运行 bash capture/server/build_plugin.sh（需要 Docker），或者给 capture/windows/setup_windows.py 加 --maven

# 采集（Windows）                                    # Linux 服务器：bash capture/linux/run_headless.sh ...
capture\run_windows.cmd --accept-minecraft-eula --mode diverse --episodes 20 --output-dir out

# 渲染白模
python -m mineengine.cap2proxy --root out/data_collection/train/batch_0 --out wb --clips --cpu-render   # 装好 nvdiffrast 和 CUDA 后去掉 --cpu-render
python -m mineengine.view --ep wb/episodes/<回合名> --report out/data_collection/train/batch_0/world_events_report.json --out view.mp4

# 自测（只用 CPU，不需要装 Minecraft：在合成数据上跑通整条流水线）
bash tests/run_cpu_selftests.sh
```

## Minecraft、素材与许可

- `--accept-minecraft-eula` 表示**运行者本人**同意 [Minecraft EULA](https://aka.ms/MinecraftEULA)。服务端和客户端都在离线模式运行，不使用任何微软或 Mojang 账号。
- 你需要自己拥有正版 Minecraft: Java 版。离线模式只是让本地自动采集不用登录，并不改变这一点。
- 本仓库不包含任何 Minecraft 游戏文件（代码、贴图、模型、声音）；仓库里唯一的 Minecraft 内容是截图和动图里的游戏画面，见 [NOTICE](NOTICE)。客户端 jar、默认皮肤、方块模型，以及水下回合过滤要用的那一个 HUD 图标，都由 `scripts/setup_assets.py` 从你自己的安装里读取。
- 第三方插件在安装时从官方发布页下载，各自遵循自己的许可，见 [THIRD_PARTY.md](THIRD_PARTY.md)。
- `capture/` 基于 [SolarisEngine](https://solaris-wm.github.io/solaris-engine/)（Apache-2.0），见 [NOTICE](NOTICE)。
- README 里的截图都是我们自己采集的画面；流程图和皮肤图是我们自己做的。
- 采集结果里记有你这台机器上的绝对路径（例如 `run_config.json` 和对齐后的 `*_meta.json`）。分享数据集之前请先检查，见 [THIRD_PARTY.md](THIRD_PARTY.md)。
- 非 Minecraft 官方产品，未经 Mojang 或 Microsoft 认可，与二者无关。NOT AN OFFICIAL MINECRAFT PRODUCT. NOT APPROVED BY OR ASSOCIATED WITH MOJANG OR MICROSOFT.

许可：本仓库自己的代码、程序生成的皮肤和它们的参考图、数据表采用 Apache-2.0（见 [LICENSE](LICENSE)、[NOTICE](NOTICE)）。[THIRD_PARTY.md](THIRD_PARTY.md) 里列出的文件各自沿用自己的许可。游戏截图里的 Minecraft 内容版权归 Mojang / Microsoft，不在 Apache-2.0 范围内。
