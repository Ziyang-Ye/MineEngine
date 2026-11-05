# 采集改动编号对照表（T0–T20）

[English](requirements.md) | 中文

采集代码和 `mineengine` 的注释里常写 `T12`、`T16` 这样的编号。每个编号对应我们在上游 SolarisEngine 采集上做的一项改动，目的都是让日志足够完整、足够精确，可以逐帧重建白模。这里列出每个编号要求什么、做在哪里。“位置”一栏里的路径，除了以 `capture/` 或 `mineengine` 开头的，都是相对 `capture/` 目录的。

状态：**已完成** = 已实现，并由 `postprocess/verify_world_events.py` 或 `mineengine` 自测检查；**由 mineengine 完成** = 改由离线白模渲染解决，不在采集端做；**部分完成** = 采集端只做了一部分；**未做** = 还没实现。

## 世界和相机设置

| 编号 | 要求 | 状态 | 位置 |
|---|---|---|---|
| T0 | 改过的插件、控制器和相机代码能完整编译并跑通 | 已完成 | 整个 `capture/` |
| T1 | 每局冻结时间和天气，在初始快照之前设好并写进 `episode_info`：白天 85%，黄昏 / 夜晚 / 黎明 15%；晴 80%、雨 12%、雷雨 3%、雪 5% | 已完成 | `controller/episodes-loop.js`、`controller/utils/env-setup.js` |
| T2 | 固定相机客户端设置（视场角 70°、渲染和模拟距离 12、关闭视角摇晃和平滑镜头、固定界面缩放），写进 `*_camera_meta.json` | 已完成 | `camera/launch_minecraft.py`、`camera/windows/launch_camera.py` |
| T13 | 飞行、疾跑、拉弓时视场角也保持 70°（`fovEffectScale = 0`），并记录 `fov`、`fovEffectScale`、`guiScale` 和分辨率 | 已完成 | 同 T2；`mineengine` 读取记录下来的值 |
| T12 | 相机画面就是 bot 的视角：控制器每发一个移动包，就经本机 UDP 把 bot 的位姿直接发给相机客户端（pose link）；插件仍然每 tick 移动相机作为兜底，并把相机自己的位姿和 bot 的分开记录 | 已完成 | `camera/capture-mod/`（`PoseLink`、`CameraMixin`、`GameRendererMixin`）、`server/.../CameraManager.java` |

## 世界状态日志

| 编号 | 要求 | 状态 | 位置 |
|---|---|---|---|
| T3 | 每 tick 记录快照中心 64 格内的全部实体：编号、种类、名字、位置、朝向、速度、碰撞箱大小、血量、状态、手持物、TNT 引信 | 已完成 | `server/episode-manager-plugin/.../WorldStateLogger.java` |
| T14 | 战斗类回合（pvp、pve、怪物）用普通难度并开启 pvp，伤害、击退、死亡真实发生并写进日志；记录难度 | 已完成 | `controller/episodes-loop.js`、`pvp-episode.js`、`pve-episode.js` |
| T15 | 动作和动画事件，带 tick、时间和玩家名：挥手、受击（攻击者、伤害值、伤害类型、击退、受击后血量）、开始和结束使用物品（吃东西、举盾、拉弓）、切换手持格 | 已完成 | `WorldStateLogger.java` |
| T16 | 每帧完整的玩家状态：快捷栏、选中格、副手、护甲、血量、饥饿、饱和度、经验、氧气、状态效果、身体朝向和头部朝向分开记、是否着地 / 潜行 / 疾跑 / 飞行，以及准星指向（方块和面，或实体）和距离 | 已完成 | `controller/recorder/frame-recorder.js`；由 `mineengine.cap_reader` 读取 |
| T17 | 挖掘进度（裂纹 0–9 级），带方块坐标、阶段和玩家，以及开始 / 中止 / 完成事件 | 已完成 | `WorldStateLogger.java`、`CameraManager.java`；`mineengine` 画在白模的 `fx` 通道里 |

## 校验

| 编号 | 要求 | 状态 | 位置 |
|---|---|---|---|
| T4 | 每局校验再加几项：每帧都有绝对 yaw / pitch、实体记录不缺 tick、记录了时间、记录了相机设置；每局输出一行 OK / WARN / FAIL | 已完成 | `postprocess/verify_world_events.py` |

## 回合和行为

| 编号 | 要求 | 状态 | 位置 |
|---|---|---|---|
| T6 | 新回合类型：coBuild、buildVsBreak、digDown / tunnel / crater、tntDemolish、forestBuild、pvpMelee / pvpRanged、pveMobs、itemExchange（后来又加了 rideMount、snowIce、farming、animalCare、waterWorks、redstoneDoors、lightAndFire、fallingBlocks）；每种回合把角色、材料和关键参数写进 `state_metadata` | 已完成 | `controller/episode-handlers/diverse/` |
| T7 | 建造类回合从材料池里随机取材料，并记录下来 | 已完成 | `controller/utils/materials.js`、`build-*.js`、`tower-bridge-episode.js` |
| T8 | 不干活的一方保持 3–20 格距离，看着施工点或对方，时不时换个位置，让两人经常出现在对方画面里 | 已完成 | `controller/episode-handlers/partner-gaze.js` 及调用它的回合 |

## 渲染和规模

| 编号 | 要求 | 状态 | 位置 |
|---|---|---|---|
| T5 | 每名玩家再出一路逐像素对齐的"白模视频" | 由 mineengine 完成 | 由 `mineengine.cap2proxy` 根据日志离线渲染，不用再开一个游戏客户端，不占采集时间，以后还能重新渲染 |
| T9 | 深度图和方块编号图 | 由 mineengine 完成 | 白模的 `logdepth`、`btype`、`bstate`、`face` 通道（见 [whitebox_channels.md](whitebox_channels.md)） |
| T10 | 回合在正常生成的地形上也能跑，不只是平坦世界 | 已完成 | 每个实例单独选世界类型（`generate_compose.py`），并记录为 `extra_info.world_type` |
| T11 | 一台机器上同时跑多个采集实例 | 已完成 | `capture/linux/run_headless.sh` 自动挑空闲端口和显示编号；实际每张卡跑 4 个实例 |
| — | 逐 tick 锁定采集（每个游戏 tick 对应一帧画面） | 未做 | 正在开发，见 [capture/README_zh.md](../capture/README_zh.md) |

## 后来补充的三项（T18–T20）

这三项来自后来修订的一版输出格式。`mineengine.cap_synth` 生成的合成回合完全按这一版写。采集端用自己的格式记录 T18 和 T19，`mineengine` 两种格式都能读。

| 编号 | 要求 | 状态 | 位置 |
|---|---|---|---|
| T18 | 每局记录每名玩家的皮肤：皮肤文件、它的 sha1、手臂模型（粗手臂或细手臂）和换上的时间（`episode_info` 里的 `skin`），PNG 存成 `output/skins/<sha1>.png` | 已完成 | 采集端写在 `episode_info` 的 `state_metadata.skins` 里（`controller/utils/skins.js`），`tools/skins/export_episode_skins.py` 把 PNG 复制到回合文件旁边；`mineengine.cap2proxy` 把两种格式都转成同样的记录，并把 PNG 复制到 `output/skins/<sha1>.png`，`mineengine.entity_refs` 从那里读取 |
| T19 | 每 tick 的生物列表：每条 tick 记录都列出当时的生物（`mobs`：uuid、种类、位置、朝向、碰撞箱、血量、攻击目标） | 已完成 | 采集端把同样的生物信息记成 T3 的 `entities` 记录（`WorldStateLogger.java`）；`mineengine.cap_reader` 两种格式都能读 |
| T20 | 持续世界：一局从上一局结束的地方接着开始。开局快照就是上一局的结束快照（不做场地准备，不还原），游戏时间接着走，掉落物留着，同一串回合共用一个 `session_id` | 部分完成 | 采集端同一个实例的各局共用一个世界（`episode_begin.world.uid`），不加 `--reset-world` 时前面的改动会留下来；但没有 `session_id`，而且每局通常会传送到新的地点。`mineengine.cap_synth` 会生成完整的回合串，`mineengine.cap_selftest` 检查每局的结束快照和下一局的开局快照是否一致 |


## 注释里的"03 第 N 节"和"round 2"

有些注释写着 `03 section N` 或 `03 §N`，指的是最初那份回合设计说明的某一节。这份说明没有放进仓库，各节的要求如下：

| 引用 | 内容 | 现在做在哪里 |
|---|---|---|
| 03 §2 | 新增的回合类型 | 见上面的 T6；`controller/episode-handlers/diverse/` |
| 03 §3 | 建造类回合的材料池：墙（橡木、云杉、白桦、深色橡木木板，圆石，石砖，砖块，六种颜色的混凝土，羊毛）、柱和梁（橡木、云杉、白桦原木）、窗（玻璃、染色玻璃）、屋顶、地板，另有会下落的沙子和沙砾、TNT | `controller/utils/materials.js`；设 `MATERIAL_POOL=spec` 就是这几个池子，默认的池子更大 |
| 03 §4 | 各大类占比：建造 35%、破坏和挖掘 20%、打斗 25%、移动和观察 10%、物理和流体 10%；白天 85%；超平坦 60%、正常地形 40% | `controller/utils/episode-weights.js` 里的 `EPISODE_CATEGORIES`（可用 `CATEGORY_WEIGHTS` 改）；`controller/utils/env-setup.js` |
| 03 §5 | 观察方的行为：不干活的一方保持 3–20 格，大部分时间看着施工点或对方，时不时走动；对方出现在画面里的帧至少 40%；不长时间贴脸（小于 1.5 格），不背对 | 见上面的 T8；`controller/episode-handlers/partner-gaze.js` |

注释和 `capture/windows/schema_parts/r2-*.md` 里的"Round 2""R2""round-2 fixer"，指采集代码的第二轮开发：加了更多回合类型、世界多样性和皮肤，也包括之后按评审意见做的修复。
