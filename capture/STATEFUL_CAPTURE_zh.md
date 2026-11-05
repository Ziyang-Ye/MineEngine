# 用 Docker 做有状态采集（笔记本用法）

[English](STATEFUL_CAPTURE.md) | 中文

本目录是 SolarisEngine（Apache-2.0）的修改版。这一页讲它原来的 Docker 用法：在一台装了 Docker 和 NVIDIA 显卡的 Linux 游戏本上采集 Minecraft 双人数据。现在主要用的是 Windows 原生（`windows/README.md`）和无头 Linux（`linux/README.md`）两种方式。

除了上游 SolarisEngine 原有的视频和逐帧动作，每次运行还会记录**显式、可编辑的世界状态**：
- 每个 episode 区域在开始时的方块快照；
- 逐 tick 的方块和实体变化日志；
- 真实的世界种子；
- 几类会改变世界的新 episode：点火再灭火、冰在热源旁融化、TNT 爆炸、岩浆遇水、先搭建再拆除。

字段的精确定义见本目录的 `OUTPUT_SCHEMA.md`。

---

## 1. 改了什么

| 位置 | 改动 |
|---|---|
| `server/episode-manager-plugin/.../WorldStateLogger.java`（新） | Paper 插件里的世界状态记录器，新增 `/worldstate` 命令，由控制 bot 通过 RCON 调用。说明见表格下方。 |
| 同上 | episode 区域的方块快照（默认以场地为中心 65 × 65 格，高度从玩家下方 24 格到上方 40 格）：episode 开始时写 `*_world_snapshot_start.json.gz`，结束时写 `*_world_snapshot_end.json.gz`。可选：`save-all flush` 之后拷贝覆盖该区域的 `.mca` 区域文件；在 episode 之间把该区域恢复到初始状态。 |
| `EpisodeManager.java`、`plugin.yml` | 注册记录器和 `/worldstate` 命令。 |
| `server/build_plugin.sh`（新） | 在 Maven 容器里重新编译插件 jar，主机不需要装 Java。 |
| `controller/utils/world-state.js`（新）、`controller/episodes-loop.js` | 说明见表格下方。 |
| `controller/episode-handlers/state/*.js`（新） | 5 个新 episode：`igniteAndExtinguish`、`iceNearFire`、`tntBlast`、`lavaWater`、`buildAndBreak`（第 5 节）。 |
| `controller/utils/episode-weights.js` | 新类型的典型时长（抽样权重和上游一样，仍是 1/√时长），以及可选的按类型权重倍数。 |
| `controller/act_recorder/act_recorder.py` | 逐帧 JSON 里硬编码的 `"seed": 42` 换成配置的种子和世界类型。绝对 `yaw` / `pitch` 以更高精度保存（1e-5 弧度），并新增 Minecraft 角度制的 `yaw_mc_deg` / `pitch_mc_deg`。 |
| `controller/config/args.js`、`controller/entrypoint.sh` | 透传新参数。 |
| `generate_compose.py` | 新参数，说明见表格下方。 |
| `run.sh` | 对应的命令行开关；后处理并行数可调；每个 batch 结束后校验世界日志；`--skip-dataset`。 |
| `run_laptop.sh`（新） | 单台笔记本的预设：1 个实例（2 名玩家）、平地世界、默认只跑新 episode。 |
| `postprocess/verify_world_events.py`（新） | 校验世界日志，重放 `block_state` 并与结束快照比对，检查与逐帧 JSON 的时间对齐。 |
| `postprocess/prepare_train_dataset.py` | 把世界日志和快照一起拷进数据集。 |

**`WorldStateLogger.java`** 监听：
- 方块放置、破坏、燃烧、点燃和消退（冰融化、火熄灭）；
- 方块生成（水 + 岩浆 → 黑曜石 / 圆石）、液体流动和流体等级变化；
- 爆炸、TNT 点燃、活塞、下落方块、树木生长、用桶倒出和装起，以及玩家交互；
- 实体生成、死亡和移除，以及天气变化。

它的记录：
- 每个事件是一行 JSON，写入该 episode 自己的 `*_world_events.jsonl`。
- 每条记录都带三个时钟：
  - `game_time`，和逐帧 JSON 里的 `worldAgeTicks` 是同一个计数；
  - `server_tick`；
  - `epoch_ms`，和逐帧的 `epochTime` 是同一个主机时钟。
- 每个 tick 写一条 `tick` 记录，给出这几个时钟之间的对应关系，以及两名玩家在服务器端的位置和朝向。
- 每次事件之后的下一个 tick，会回读该方块的实际状态，写成一条 `block_state` 记录。这是能精确重放的真值流。

**`world-state.js` / `episodes-loop.js`：**
- 领队 bot Alpha 在两名玩家开始录制之前打开日志，并拍初始快照。episode 结束后，它关闭日志并拍结束快照，然后可选地做复原。
- 启动时用 `/seed` 查询真实种子。
- episode_info JSON 新增 `world`（真实种子、世界类型、生成参数）、`world_state` 和 `state_metadata`。
- 传送目标由带种子的随机数生成器给出，所以可以复现。
- 新的 episode 类型在这里注册。

**`generate_compose.py` 的新参数：**
- `--world_seed`（固定种子）、`--world_state_logging`、`--world_snapshot_mode`、`--world_snapshot_radius`；
- `--reset_world_between_episodes`、`--teleport_radius`、`--episode_weight_multipliers`、`--state_random_tick_speed`；
- `--server_memory`、`--eula`、`--nvidia_disable_require`。

服务器容器会挂载输出目录，用来写世界日志。平地世界里关闭结构生成的开关（`--flatland_world_disable_structures`）原本就有，现在也能从 `run.sh` 使用。

**逐帧绝对角度。** 逐帧 JSON 一直都有绝对 `yaw` / `pitch`（Mineflayer 弧度），后处理也从没删掉它们。唯一的问题是：登录后 yaw 还是 null 的那些帧会被静默丢掉（`round(None)` 会报错）。这一版保留了这些帧，并以更高精度保存角度。

---

## 2. 笔记本需要什么

| 项目 | 要求（1 个实例 = 2 名玩家） |
|---|---|
| 系统 | Ubuntu 22.04 或 24.04（64 位） |
| 显卡 | NVIDIA 独立显卡，显存 ≥ 4 GB（每个实例约 1.2 GB），带 NVENC 硬件编码。GTX 16 系和 RTX 20/30/40/50 系笔记本显卡都有；部分 MX 型号没有，这时编码会回退到 CPU，CPU 负载更高。 |
| 驱动 | NVIDIA 专有驱动，550 或更新。预设加了 `--nvidia-disable-require`，所以不要求 580+。 |
| 内存 | 至少 16 GB，建议 32 GB：服务器约 2.5 GB，两个 Minecraft 客户端各 2–4 GB，bot 和录制进程约 2 GB。 |
| CPU | 至少 8 线程（上游建议每个实例 4 个物理核）。 |
| 磁盘 | 至少 50 GB 空闲：Docker 镜像约 10 GB，Minecraft 客户端约 1.5 GB，数据量见第 7 节。 |
| 网络 | 首次运行要下载 Docker 镜像、Paper 服务端、Minecraft 客户端文件和 Maven 依赖：总共约 12–15 GB。 |
| 供电 | 插电运行，并关闭自动休眠。 |

---

## 3. 安装（Ubuntu 22.04 / 24.04）

下面的命令都在终端里执行。

### 3.1 NVIDIA 驱动

```bash
sudo ubuntu-drivers install        # 或者：sudo apt install -y nvidia-driver-570
sudo reboot
nvidia-smi                         # 应能看到显卡和驱动版本
```

如果开了 Secure Boot，安装时会要求设一个 MOK 密码；下次开机时选 "Enroll MOK"。双显卡（Optimus）笔记本用默认的 on-demand 模式就行，只要 `nvidia-smi` 正常。

### 3.2 Docker Engine 和 Compose 插件（官方源）

```bash
sudo apt-get update
sudo apt-get install -y ca-certificates curl
sudo install -m 0755 -d /etc/apt/keyrings
sudo curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
sudo chmod a+r /etc/apt/keyrings/docker.asc
echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu $(. /etc/os-release && echo "${UBUNTU_CODENAME:-$VERSION_CODENAME}") stable" | sudo tee /etc/apt/sources.list.d/docker.list > /dev/null
sudo apt-get update
sudo apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
sudo usermod -aG docker $USER      # 然后注销并重新登录一次
docker run --rm hello-world        # 重新登录后检查
docker compose version             # 需要 v2
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
docker run --rm --runtime=nvidia --gpus all ubuntu nvidia-smi   # 容器里能看到显卡
```

### 3.4 主机上的 Python（后处理用）

二选一：

```bash
# 方式 A：conda（上游推荐）
# 先安装 Miniconda：https://docs.conda.io/en/latest/miniconda.html
conda env create -f env.yaml
conda activate SolarisEngine

# 方式 B：系统包
sudo apt install -y python3-yaml python3-numpy python3-opencv python3-tqdm python3-pil ffmpeg
```

下面所有步骤都在这个环境里运行（用方式 A 的话，先 `conda activate SolarisEngine`）。

### 3.5 代码、插件和资源文件

把整个仓库放到笔记本上，不要只放 `capture/`，例如放在 `~/MineEngine`。采集代码在它的 `capture/` 目录里；下面所有命令都在这个目录下运行：

```bash
cd ~/MineEngine/capture
python3 ../scripts/setup_assets.py
```

`setup_assets.py` 会准备好本仓库没有附带的文件：
- 把 ProtocolLib 和 SkinsRestorer 下载到 `server/plugins/`，并校验它们的 sha256。没有 ProtocolLib，MirrorBot 起不来；
- 从你自己的 Minecraft 1.21 客户端 jar 里提取默认皮肤和 HUD 模板。它会到 `~/.minecraft/versions/1.21/1.21.jar` 找这个 jar：只要你玩过 1.21，官方启动器就会把它放在这里；否则用 `--jar PATH` 指定。

### 3.6 签名皮肤（仅 Docker 用法）

Docker 里的相机跑的是原版客户端，只显示带 Mojang 签名的皮肤。所以这条路径用到的每个皮肤都要有一份签名缓存（一个 `.customskin` 文件），而仓库里一个都没带：
- 两个会话皮肤 `server/skins/session_a.png` 和 `session_b.png` 都有缓存之前，`generate_compose.py` 拒绝启动；
- 每个 episode 换用的皮肤，也只有带缓存的才会用到。

先生成一次缓存：

```bash
pip install requests                 # 3.4 节的方式 B：sudo apt install -y python3-requests
python3 regenerate_skin_cache.py --upload server/skins/               # 两个会话皮肤
python3 regenerate_skin_cache.py --upload server/skins/ --recursive   # 连同每个 episode 用的皮肤池（可选）
```

`--upload` 会把 PNG 发给 MineSkin 网络服务（mineskin.org），由它用自己的账号拿到 Mojang 签名。没有 MineSkin API key 时，每小时大约能处理 60 个皮肤；再运行一次，会从上次停下的地方接着做。详见 `regenerate_skin_cache.py` 开头的说明和 `server/skins/README.md`。

Windows 和 Linux 原生运行器不需要这些：它们的相机运行采集模组，所有皮肤都在本地渲染。

---

## 4. 构建并运行一个实例

### 4.1 编译服务器插件（只需一次）

```bash
cd ~/MineEngine/capture
server/build_plugin.sh
```

最后一行应为 `.../MirrorBot-1.0.jar: contains WorldStateLogger`。第一次编译会下载 Maven 镜像和依赖（约 1 GB）。`run_laptop.sh` 发现插件过期时，也会调用这个脚本。

`run.sh` 启动时会构建 Docker 镜像（`solaris-engine-base`、`solaris-engine-camera`）。第一次构建约 15–30 分钟，之后走缓存。

### 4.2 先跑一个短测试

```bash
cd ~/MineEngine/capture
./run_laptop.sh --accept-minecraft-eula --mode stateful --episodes 5
```

`--accept-minecraft-eula` 的含义见第 10 节。不加它，脚本会退出并说明原因。

启动后约 3–5 分钟开始录制：要等服务器启动、客户端第一次下载文件、玩家就位。运行时可以在浏览器里看两个相机的画面：

- Alpha：<http://localhost:6901>，Bravo：<http://localhost:6902>（VNC 密码 `research`）

服务器、两个相机容器和插件启动器使用 host 网络。每个实例有两个端口监听所有网卡，而不只是 `localhost`：
- 游戏端口 25590；
- RCON 25600，密码固定为 `research`，有了它就能完全控制服务器控制台。

同一网络里的任何人都能访问这两个端口（开多个实例时还有它们后面按实例递增的端口）。只在可信网络里使用 Docker 用法，或者在防火墙里封掉它们。VNC（5901/5902）和 noVNC（6901/6902）只监听 `127.0.0.1`；要看远程机器上的相机画面，先转发端口，例如 `ssh -L 6901:localhost:6901 <主机>`。bot 和动作记录器（8110）留在各实例内部的 Docker 网络里。

所有 episode 跑完后，脚本会停掉容器、修正文件属主、对齐视频、校验世界日志，然后打印输出目录。

### 4.3 正式采集

```bash
# 只跑 5 种会改变状态的新 episode（默认）
./run_laptop.sh --accept-minecraft-eula --mode stateful --episodes 60

# 混合：上游全部训练 episode + 新 episode（新类型权重 x3，约占一半 episode）
./run_laptop.sh --accept-minecraft-eula --mode mix --episodes 60

# 只跑列出的类型（逗号分隔）
./run_laptop.sh --accept-minecraft-eula --mode tntBlast,lavaWater --episodes 30

# 固定种子 + 每个 episode 后恢复区域 + 同时保存 .mca 区域文件
./run_laptop.sh --accept-minecraft-eula --world-seed 20261021 --reset-world --snapshot-mode both

# 多个 batch（每个 batch 都重启全部容器：每个 batch 一个新世界）
./run_laptop.sh --accept-minecraft-eula --batches 3 --episodes 40
```

`run_laptop.sh` 的参数：

| 参数 | 默认值 | 含义 |
|---|---|---|
| `--mode` | `stateful` | `stateful` / `mix` / `all`（上游默认集合 + 新 episode，按时长加权）/ 逗号分隔的类型列表 |
| `--episodes` | 20 | 每个 batch 的 episode 数 |
| `--batches` | 1 | batch 数 |
| `--output-dir` | `output_laptop` | 输出目录 |
| `--world-seed` | 无 | 固定的世界种子；不给时种子为 `<实例号><当前时间戳>`。真实值总会记录下来。 |
| `--reset-world` | 关 | 每个 episode 后把快照区域恢复为初始方块，并移除该 episode 生成的实体 |
| `--normal-world` | 关 | 用正常地形代替平地世界；这时 `stateful` 模式下只有 `buildAndBreak` 能跑 |
| `--snapshot-mode` | `blocks` | `blocks`（紧凑的方块快照）/ `region`（.mca 区域文件）/ `both` / `none` |
| `-- <options>` | | `--` 后面的所有参数原样传给 `run.sh`，例如 `-- --flat-no-structures --snapshot-radius 48` |

其余设置由预设固定：
- 1 个平地世界实例，传送半径 2000，这样生成的新区块更少；
- 服务器堆内存 2G，后处理 4 个进程；
- 水下的 episode 保留，不过滤；
- 不制作最终数据集（`--skip-dataset`；这一步可以之后在服务器上做）；
- 相机容器设 `NVIDIA_DISABLE_REQUIRE=1`。

中途要停的话，在终端按 Ctrl+C，然后运行：

```bash
python3 orchestrate.py stop --compose-dir output_laptop/data_collection/train/batch_0/compose_configs
```

不要用你自己的 Minecraft 客户端连这台服务器：多出来的玩家会出现在 bot 的视频里。

### 4.4 NVENC

- 每个相机容器先用 NVENC 试编码一次。成功就用 `h264_nvenc`，否则回退到 CPU 的 `libx264`。在 `logs/docker-compose-000/camera_alpha_instance_0.log` 里找 `Using NVENC hardware encoding` 或 `Using CPU encoding`。
- 1 个实例同时占 2 路 NVENC 会话，远在消费级显卡的上限之内。
- 相机镜像基于 `nvidia/cuda:13.0`，正常情况下要求驱动支持 CUDA 13（580+）。相机只用 EGL 和 NVENC，不用 CUDA，所以预设加了 `--nvidia-disable-require` 来跳过这个检查。驱动已是 580+ 时，这个开关没有任何影响。
- 如果画面卡顿而 GPU 占用不高，瓶颈通常在 CPU（上游文档也这么说）。关掉其他程序。

---

## 5. 新的 episode 类型

每个新 episode 里都是一名玩家操作、另一名玩家观看。两名玩家全程录制，所以每次状态变化都有两个视角。
- 角色分配、场地朝向、观察者站在对面还是侧面、距离都是随机的，并写在 episode_info 的 `state_metadata` 里。
- 用服务器命令做的场地准备（清掉雪层和草）在初始快照之前完成。

| 类型 | 过程 | 主要记录到的状态变化 |
|---|---|---|
| `igniteAndExtinguish` | 放 2–4 个易燃方块（木板、原木、干草块、羊毛），用打火石点燃，烧 3–7 秒。然后由操作者或观察者（观察者会先走过来）用水桶灭火。大约一半的情况下，还会用空桶把水舀回来。 | 放置、点燃、火蔓延、燃烧、火熄灭、放水、水流、舀水、水位变化 |
| `iceNearFire` | 2–4 块冰排成一行，行尾放一个热源：火把、点燃的下界岩（永久火）或岩浆源。玩家看着冰在 14–22 秒里融化成水；岩浆变体还会生成黑曜石或圆石。为了让冰在一个 episode 内融化，episode 期间 `randomTickSpeed` 设为 100（默认是 3；之后会恢复，数值记录在日志里）。 | 放置、冰融化（fade）、水流、方块生成 |
| `tntBlast` | 放 1–3 块 TNT，用打火石点燃。操作者跑到 8–10 格外，观察者在 10–13 格外看。一半的情况下，操作者会回到坑边往下看。 | 放置、TNT 点燃、实体生成（TNT、掉落物）、爆炸（含每个被炸方块原来的状态）、实体移除 |
| `lavaWater` | 挖一个 1 × 1 的坑，倒入岩浆源。操作者或观察者在旁边倒水；水流过坑，把岩浆变成黑曜石。60 % 的情况下会再把水舀回来，玩家看着水退去。 | 破坏、放岩浆、放水、水流、黑曜石 / 圆石生成、舀水、水位变化 |
| `buildAndBreak` | 用几种材料搭一个小结构（墙、柱子、2 × 2 × 2 立方体、拱、L 形、台阶）。然后由观察者（60 %，会先走过来）或操作者从上往下拆掉其中 30–80 %。有时会用沙子或沙砾，下面的方块被拆掉时它们会掉下来。 | 放置、破坏、掉落物、下落方块 |

前四种只在平地世界运行；`buildAndBreak` 在正常地形上也能运行。

---

## 6. 两条时间轴怎么对齐（简要）

- 世界日志的每条记录都有 `epoch_ms`（主机时钟，毫秒）和 `game_time`（世界 tick）。
- 逐帧动作 JSON 的每一帧都有 `epochTime`（同一个主机时钟，单位秒）和 `worldAgeTicks`（同一个 tick 计数）。`worldAgeTicks` 只在服务器每秒同步一次时更新，所以会滞后 0 到 20 多个 tick。
- 日志里每个 tick 都有一条 `tick` 记录，给出 `epoch_ms` 到 `game_time` 的对应。用它把每帧的 `epochTime` 换算成精确的 tick。
- 对齐后的视频里，第 i 帧是动作第 i 条之后拍到的第一张相机画面。所以在时刻 t 发生的方块变化，从 `epochTime ≥ t` 的第一帧起就能看到。

---

## 7. 输出

运行结束后，`output_laptop/data_collection/train/batch_0/` 下主要有：

```
compose_configs/docker-compose-000.yml       本次使用的配置
logs/docker-compose-000/*.log                每个容器一个日志
data/0/                                      服务器目录，含完整世界存档 world/
output/                                      每个 episode 的文件（见下表）
output/world_regions/<stem>_start/           仅 --snapshot-mode region/both 时有：r.X.Z.mca + level.dat
camera/output_alpha/0/camera_alpha.mkv       Alpha 的原始录像（整个会话，1280×720）
camera/output_bravo/0/camera_bravo.mkv       Bravo 的原始录像
camera/data_alpha/0/, camera/data_bravo/0/   Minecraft 客户端安装文件（不是数据，不用拷）
aligned/*_camera.mp4 + *_camera_meta.json    按动作逐帧切好的视频
world_events_report.json                     世界日志校验结果
```

`output/` 里每个 episode 的文件（`TS` = 时间戳，`EEEEEE` = episode 编号，`III` = 实例编号）：

| 文件 | 内容 |
|---|---|
| `TS_EEEEEE_Alpha_instance_III.json` / `..._Bravo_...json` | 逐帧动作 + 位置 + 绝对 yaw/pitch + 物品栏 + 时间 |
| `..._meta.json` | 录制元数据 |
| `..._episode_info.json` | episode 类型、真实种子、世界类型、错误标记、`world_state`、`state_metadata` |
| `TS_EEEEEE_world_instance_III_world_events.jsonl` | 世界状态事件日志（两名玩家共用） |
| `TS_EEEEEE_world_instance_III_world_snapshot_start.json.gz` | 初始方块快照 |
| `TS_EEEEEE_world_instance_III_world_snapshot_end.json.gz` | 结束方块快照 |

每小时的数据量（1 个实例；都是估算值，第一次短测试后请用 `du -sh` 实测）：

| 内容 | 每小时约 |
|---|---|
| 两路原始 MKV 录像 | 1.5–3 GB |
| 对齐后的 MP4（两名玩家） | 2–6 GB |
| 逐帧动作 JSON（两名玩家） | 0.2–0.5 GB |
| 世界事件日志 | 30–60 MB |
| 方块快照（blocks 模式，每个 episode 2 个，每个约 20–150 KB） | 5–25 MB |
| 区域文件（region / both 模式） | 每个 episode 多 1–5 MB |
| 合计 | 每小时约 4–10 GB |

也就是每小时约 50–70 个新 episode：每个 30–60 秒，另加 10–20 秒传送和准备。

---

## 8. 怎么判断一次运行是否正常

1. 运行结束时会打印校验结果。也可以手动运行校验：

   ```bash
   python3 postprocess/verify_world_events.py output_laptop/data_collection/train/batch_0
   ```

   每个 episode 打印一行，例如：

   ```
   [OK] episode 000003 instance 000 type=tntBlast records=1543 changes=212 ticks=1180 replay_mismatch=0 | Alpha: 1102 frames, 212/212 changes inside frame span, Bravo: 1098 frames, 212/212 changes inside frame span
   ```

   要看的：
   - 状态是 `OK`，且 `replay_mismatch=0`：初始快照加上重放的日志，等于结束快照。少量不一致会显示为 `WARN`，报告里会列出这些方块。
   - `changes=` 大于 0（新 episode 一定会改变方块）。
   - 两名玩家的 `frames` 都大于 0，且大部分变化落在帧的时间范围内（`x/y changes inside frame span`）。
   - 没有 `some frames lack yaw/pitch` 错误。
2. 文件数量：`ls output_laptop/data_collection/train/batch_0/output/*_world_events.jsonl | wc -l` 应约等于已完成的 episode 数。
3. 抽查一个日志：

   ```bash
   f=$(ls output_laptop/data_collection/train/batch_0/output/*_world_events.jsonl | head -1)
   head -1 "$f" | python3 -m json.tool | head -40          # episode_begin：种子、区域、gamerules
   grep -c '"type":"tick"' "$f"                            # 约等于 episode 秒数 x 20
   grep -o '"type":"[a-z_]*"' "$f" | sort | uniq -c        # 按类型统计记录数
   ```
4. 服务器日志里应有 `[worldstate] begin` 和 `[worldstate] end`：

   ```bash
   grep -c "\[worldstate\] begin" output_laptop/data_collection/train/batch_0/logs/docker-compose-000/mc_instance_0.log
   ```
5. 打开几个 `aligned/*_camera.mp4`，确认两个视角都拍到了点火、融化、爆炸等过程。

---

## 9. 拷出数据

把整个输出目录拷出来（默认是 `~/MineEngine/capture/output_laptop/`）。`camera/data_alpha` 和 `camera/data_bravo` 是 Minecraft 客户端安装文件，可以不拷。

---

## 10. Minecraft EULA

- 服务器容器（itzg/minecraft-server，Paper 1.21）只有在环境变量 `EULA=TRUE` 时才会启动，这表示运行它的人接受 [Minecraft EULA](https://aka.ms/MinecraftEULA)。上游默认直接写 `TRUE`。这里不会替你接受 EULA：除非你自己加上 `--accept-minecraft-eula`，否则 `run_laptop.sh`、`run.sh` 和 `run_evals.sh` 都会停下（`run.sh --eula TRUE` 也可以）。
- 服务器以离线模式运行（`online-mode=false`）。bot 和相机客户端用离线身份登录，不需要也不会使用任何 Microsoft 或 Mojang 账号。
- 相机用的是官方 Java 版客户端文件，由 `minecraft-launcher-lib` 从 Mojang 的服务器下载到 `camera/data_*`。
- 你需要自己拥有 Minecraft: Java Edition 的正版授权。离线模式只是让本地的自动采集不用登录就能运行，并不改变这一点。

---

## 11. 常见问题

| 现象 | 处理方法 |
|---|---|
| `server/build_plugin.sh` 失败 | 通常是访问 `repo.papermc.io` 或 Maven Central 的网络问题：重试或换个网络。 |
| 相机容器报 `unsatisfied condition: cuda>=13.0` | 确认用的是 `run_laptop.sh`（它会加 `--nvidia-disable-require`），或者升级到 580+ 驱动。 |
| 相机容器找不到 `nvidia` runtime | 重做 3.3 节的 `nvidia-ctk runtime configure`，然后重启 Docker。 |
| 端口被占用 | 关掉占用 25590、25600、5901/5902 或 6901/6902 的程序。 |
| 内存不够，系统卡死 | 关掉浏览器和其他程序；必要时加 `-- --server-memory 1G`。 |
| 校验报告里有很多 `replay_mismatch` | 看 `world_events_report.json` 里不一致的方块；通常是日志漏记了某类方块变化。可以带上报告和那个 episode 的文件提 issue。 |
| 某些 episode 的 `encountered_error` 为 true | 少量是正常的（bot 寻路或放置失败），数据仍可用。如果比例很高，看控制器日志。 |
| 想重新开始 | 换一个 `--output-dir`，或者删掉旧的输出目录。 |

---

## 12. 已知限制

- 录制期间用服务器命令（`/setblock`、`/fill`）做的方块改动不会作为事件出现，只会体现在结束快照和重放结果的差异里。新 episode 从不这样做。
- 箱子内容、告示牌文字这类方块实体数据，只在 `region` 模式的 `.mca` 文件里有。
- 快照只覆盖 episode 区域（默认半径 32）。区域外的变化仍会作为事件记录（默认 64 格以内），但没有初始快照。
- 复原（`--reset-world`）只覆盖快照区域，而且不记录在日志里。
- 在新机器上，先跑一个 `--episodes 5` 的短测试，确认日志和校验报告看起来正常。
