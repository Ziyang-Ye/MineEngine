## Camera recordings on Windows (in-client capture, `solaris-capture` mod)

On Windows the camera clients record **inside the client** (Fabric mod
`camera/capture-mod`, launched by `camera/windows/launch_camera.py`): the main
render target is read back right before every buffer swap that falls on a
capture slot, so the window may be hidden, covered or off-screen. With the
default `--hide-window 1` the GLFW window is created invisible and is never shown
or focused (`hide_at_startup`); `--hide-window 0` keeps it visible. File names and
locations are the same as on Linux:

```
camera/output_<bot>/0/camera_<bot>.mkv           H.264, 1280x720, ~20 fps, PTS = epoch seconds
camera/output_<bot>/0/camera_<bot>_meta.json     recording metadata (below)
camera/output_<bot>/0/camera_<bot>_frames.jsonl  NEW: one line per encoded frame (below)
camera/data_<bot>/0/logs/solaris-capture-ffmpeg-camera_<bot>.log   ffmpeg log (diagnostics)
```

### Frame clock and timestamps

| item | meaning |
|---|---|
| slot | frame `k` belongs to the wall-clock slot starting at `t0 + k*50 ms` (`t0` = recording start). The first rendered frame at or after the slot time is captured; slots without any rendered frame are skipped, never duplicated. The client renders at up to `maxFps` 60 and wakes up at every slot, so the capture lands 1-3 ms after the slot start |
| MKV PTS | capture time of each frame (Unix epoch seconds, 1 ms MKV resolution). The mod writes a Matroska stream with the exact capture timestamps into ffmpeg (`-f matroska -i - -copyts -fps_mode passthrough -enc_time_base 1:1000`), so `PTS == epoch_ms / 1000` of the sidecar line with the same index (rounded to 1 ms). `postprocess/process_recordings.py` works unchanged (`wallclock_timestamps: true`) |
| `timestamp_source` (meta) | `capture_time` (default) or `ffmpeg_wallclock_at_pipe` (launcher `--timestamps wallclock`: rawvideo + `-use_wallclock_as_timestamps 1`; ffmpeg's rawvideo demuxer has a 1/fps time base, so these PTS are pipe-arrival times snapped to a 50 ms grid, ~18 ms after the capture) |
| first seconds | while the client loads the chunks around the spawn point (about 5 s after joining), single slots can be missed (gaps up to ~0.4 s, counted in `capture_stats.slots_skipped`); afterwards capture intervals are 50.0 ms ± <1 ms on an otherwise idle CPU |
| CPU load | the 20 Hz clock is wall-clock based, so frames are lost (never duplicated or delayed) when the CPU is saturated: `dropped_writer_backlog` = ffmpeg (libx264) fell behind, `slots_skipped` = the client did not render in time. The client and its ffmpeg run at Windows priority `above_normal` (launcher `--priority`) to protect the capture from other processes. While recording the mod logs a WARN line `frame loss in the last 10.0 s: ...` whenever >= 1 % of the slots of a 10 s window were lost |

### `camera_<bot>_frames.jsonl` (new)

One JSON object per line, one line per **encoded** frame, in encode order
(line `i` = video frame `i`; the line count equals the MKV frame count). This is
the exact pose the client rendered (T12 ground truth).

| field | type | meaning |
|---|---|---|
| `i` | int | video frame index (0-based) |
| `epoch_ms` | float (3 dp) | capture time (host wall clock, Unix ms; the moment the readback of this frame was issued, after the frame including HUD was drawn) |
| `slot` | int | wall-clock slot index (see above); gaps in `slot` = skipped slots |
| `render_epoch_ms` | float (3 dp) | start of this frame's render loop iteration (when the game state was interpolated); `epoch_ms - render_epoch_ms` is the render time (~1-3 ms) |
| `game_time` | int | `ClientLevel#getGameTime()` of the client (same counter as the world log's `game_time`, as last received from the server) |
| `partial_tick` | float | interpolation factor between the last two client ticks used for this frame |
| `cam_pos` | [x,y,z] (5 dp) | camera (eye) position used to render (`Camera#getPosition`); equals the spectated entity's eye when spectating. With the pose link (`pose_source: "injected"`) it is the bot's feet + 1.62 (1.27 sneaking) |
| `cam_yaw` | float (4 dp) | camera yaw in Minecraft degrees (`Camera#getYRot`; 0 = +Z south, 90 = −X west; **not** wrapped, take it mod 360) |
| `cam_pitch` | float (4 dp) | camera pitch in Minecraft degrees (`Camera#getXRot`; +90 = straight down) |
| `fov` | float (4 dp) or null | vertical FOV in degrees that `GameRenderer` actually used for the world projection of this frame (`getFov(camera, partialTick, true)`: option × dynamic FOV modifier, death / underwater changes included). 70.0 with the fixed options even while flying or sprinting; during the first ~0.5 s after joining it converges from a small value to 70 (vanilla FOV smoothing, frames show the "Loading terrain" screen). null if no world was rendered in that frame |
| `player_pos` | [x,y,z] (5 dp) | interpolated feet position of the camera's own player at `partial_tick` |
| `camera_entity` | string or null | name of the entity the camera is attached to when it is not the camera player (spectator mode), else null |
| `fb` | [w,h] | size of the client's main render target (equals `width`,`height` of the meta; if it ever differs the frame is scaled to the configured size) |
| `gui_hidden` | bool | F1 state (`Options#hideGui`); false = HUD visible |
| `screen` | string or null | open screen: `ChatScreen`, `PauseScreen`, `DeathScreen`, `ReceivingLevelScreen`, `DisconnectedScreen`, `AbstractContainerScreen:<class>`, ... or the runtime class name; null = in game |
| `pose_source` | string | `injected`: `cam_pos` / `cam_yaw` / `cam_pitch` are the bot's pose received over the pose link (below); `server`: the vanilla camera of the server-followed camera player |
| `pose_seq` | int or null | sequence number of the pose-link datagram rendered (`injected`), or of the newest datagram received (`server`, diagnostics); null = none received / link off |
| `pose_t` | float (3 dp) or null | sender time of that datagram (controller clock, Unix ms) = when the bot wrote the movement packet |
| `pose_age_ms` | float (3 dp) or null | camera setup time of this frame (mod clock) − `pose_t`: how old the rendered pose is. The bot only sends a packet when it moves or turns (and once per second when idle), so ages of hundreds of ms just mean that the bot stood still; the pose is still its current one |
| `pose_held` | bool | the datagram was older than `pose_max_age_ms` but kept because the camera player's server-followed pose (position, yaw, pitch, eye height) still equals it, i.e. the bot has not moved since (idle bot) |
| `pose_sneak` | bool or null | sneak flag of the rendered datagram (eye height 1.27 instead of 1.62) |
| `pose_fallback` | string or null | why the vanilla (server-followed) camera was used: `pose_link_off`, `no_pose` (nothing received yet, e.g. before the controllers start recording), `stale` (no datagram within `pose_max_age_ms` and the server pose differs from the last one), `too_far` (injected eye more than `pose_max_distance` from the vanilla camera, e.g. right after a teleport, when the chunks there may not be loaded yet), `not_player_camera` (spectating another entity), `third_person`, `player_state` (camera player dead / sleeping), `riding` (the datagram says the bot sits on a vehicle: Mineflayer then sends no position packets, so the datagram position is the stale mount point; the server-followed camera sits on the vehicle), `bind_failed`, `no_world_render` |
| `player_yaw`, `player_pitch` | float (4 dp) or null | the camera player's own (server-followed) view rotation at `partial_tick`, for comparison with `cam_yaw` / `cam_pitch` |

### `camera_<bot>_meta.json` keys

Written when recording starts and rewritten at stop. Upstream keys keep their
meaning (`recording_path`, `start_epoch_seconds`, `start_time_utc`, `fps`,
`width`, `height`, `camera_name`, `video_encoder`, `wallclock_timestamps: true`).
New keys:

| key | type | meaning |
|---|---|---|
| `capture_method` | string | `in_client_fbo_readback` |
| `timestamp_source` | string | `capture_time` or `ffmpeg_wallclock_at_pipe` (see above) |
| `frames_file` | string | file name of the frames sidecar (same directory) |
| `frames` | int | encoded frames (= MKV frames = sidecar lines); written at stop |
| `stop_epoch_seconds` | float | stop time; written at stop |
| `stop_reason` | string | `stop_file` (also Ctrl+C in the launcher's console: the launcher writes the stop file), `launcher_exit` (launch_camera.py was killed without a stop file; the mod then stops and quits the same way), `disconnect` (no world for >1 s), `jvm_shutdown` (window closed / JVM killed by a console event), `writer_failed` |
| `capture_stats` | object | `captured`, `encoded`, `dropped_writer_backlog` (frames dropped because ffmpeg fell behind), `slots_skipped`, `slot_lag_ms_mean`, `max_capture_gap_ms`, `forced_readback_waits`, `duration_seconds`, `mean_capture_fps`, `render_frames`, `ffmpeg_exit_code`, `loss_warnings` (number of frame-loss WARN lines logged) |
| `process_priority` | string or null | Windows priority class of the client and ffmpeg (`above_normal` by default; null off Windows) |
| `platform` | string | `windows` |
| `mc_version`, `fabric_loader_version`, `mod_version` | string | `1.21`, `0.16.14`, `0.1.0` |
| `framebuffer` | [w,h] | main render target size at start (1280x720; checked at 150 % Windows DPI scaling: the window is created DPI-aware, so window = framebuffer pixels) |
| `window_size` | [w,h] | GLFW window size (pixels) |
| `window_hidden` | bool | the window was hidden (`glfwHideWindow`) while recording |
| `gl_renderer`, `gl_vendor`, `gl_version` | string | OpenGL renderer used (should be the NVIDIA GPU on Optimus laptops) |
| `server` | string | `host:port` the camera joined |
| `ffmpeg_command` | [string] | ffmpeg argument list |
| `options` | object | live values of the client's `Options` (below) |
| `pose_link` | object | pose link settings and counters (below; the counters are final in the rewrite at stop) |
| `toasts` | object | clean frames: `enabled` (config `suppress_toasts`), `total` toasts dropped, `by_class` `{AdvancementToast|RecipeToast|SystemToast|TutorialToast|<class>: n}` (see "Clean frames") |
| `local_skins` | object | local skins (see "Local skins"): `enabled`, `skins_dir`, `non_local_lookups` (`SkinManager#getOrLoad` calls that went to the vanilla Mojang path; per call, so it grows per frame while player heads are in view), `non_local_textures` (distinct non-local `textures` values seen, i.e. distinct Mojang / vanilla skins), `skins` `{"<skin_id>:<model>": {skin_id, model, status (loaded/missing/invalid/loading), file, sha256_expected (from the server's property), sha256_file, sha256_match, requests (getOrLoad calls for the skin), loaded_epoch_ms}}` |

`pose_link`:

| key | meaning |
|---|---|
| `enabled` | the mod listens for pose datagrams (`pose_port` > 0 and the UDP port could be bound) |
| `port`, `max_age_ms`, `hold_when_matching`, `max_distance` | config (`pose_port`, `pose_max_age_ms` 500, `pose_hold_when_matching` true, `pose_max_distance` 16 blocks) |
| `eye_height_standing`, `eye_height_sneaking` | 1.62 / 1.27 (1.21 `Player` STANDING / CROUCHING dimensions) |
| `bot` | bot whose datagrams are accepted (`camera_name` without the `Camera` prefix) |
| `bind_error` | why the port could not be bound (the camera then follows the server only), else null |
| `received`, `rejected`, `seq_resets` | datagrams accepted / rejected (wrong bot, malformed, older `seq` and `t`, not from 127.0.0.1) / accepted with a lower `seq` but a newer `t` (controller restarted) |
| `transit_ms_mean`, `transit_ms_min`, `transit_ms_max` | receive time (mod clock) − `t` (controller clock): UDP transit plus the offset between the two clocks (~1 ms) |
| `used_frames`, `held_frames`, `server_frames` | encoded frames (= sidecar lines; frames dropped by the writer are not counted) rendered with an injected pose (of which held) / with the vanilla camera |
| `max_age_seen_ms`, `max_held_age_ms` | largest `pose_age_ms` of the encoded injected frames that were not / were held |
| `server_frame_reasons` | `{pose_fallback: frames}` for the encoded `server` frames (the sidecar's `pose_fallback` histogram) |

`options` (read from the running client, re-read at stop):

| key | value with the fixed settings | meaning |
|---|---|---|
| `fov_deg` | 70 | FOV option (degrees) |
| `fov_effect_scale` | 0.0 | T13: flying/sprinting/bow do not change the FOV |
| `render_distance` | 12 | render distance option (chunks) |
| `simulation_distance` | 12 | simulation distance option (chunks) |
| `server_view_distance` | 12 | view distance announced by the server (null if not yet known) |
| `effective_render_distance` | 12 | `min(render_distance, server_view_distance)`, what is actually rendered |
| `gui_scale` | 2 | GUI scale option (0 = auto) |
| `gui_scale_effective` | 2.0 | GUI scale actually used |
| `bob_view` | false | view bobbing |
| `damage_tilt_strength` | 0.0 | camera tilt when hurt |
| `screen_effect_scale` | 1.0 | nausea / portal distortion (default) |
| `darkness_effect_scale` | 1.0 | darkness pulsing (default) |
| `entity_shadows` | true | Minecraft default |
| `clouds` | `fancy` | Minecraft default |
| `graphics_mode` | `fancy` | Minecraft default |
| `particles` | `all` | Minecraft default |
| `max_fps` | 60 | render frame cap (frames are sampled at `fps`) |
| `enable_vsync` | false | |
| `smooth_camera` | false | cinematic camera (keybind toggle, off) |
| `pause_on_lost_focus` | false | hidden window does not open the pause menu |
| `hud_visible` | true | HUD drawn (not F1) |
| `name_tags_visible` | true | player name tags are rendered (never disabled) |
| `toasts_suppressed` | true | no toast is ever drawn (mod config `suppress_toasts`) |
| `chat_visibility` | `hidden` | live `chatVisibility` option (`full` / `system` / `hidden`); `hidden` = no chat lines in the frames |

### Pose link: the camera renders the bot's own pose (T12)

Without it the camera follows its bot through the server: the bot sends a movement
packet, `CameraManager` teleports the camera player at the start of the next server
tick, the camera client receives the teleport and renders it. That round trip made the
rendered view lag the bot by 50-80 ms (rendered-vs-bot angle error p99 6-22° in a
5-episode run). With the pose link (`run_windows.py` default; `--no-pose-link` turns it off):

- the bot's controller (`controller/recorder/camera-pose-link.js`, env `CAMERA_POSE_PORT`)
  sends every outgoing `position` / `look` / `position_look` packet — exactly what the server
  receives — as one UDP datagram to `127.0.0.1:<pose port>`:
  `{"v":1,"bot":"Alpha","seq":n,"t":<epoch ms>,"x","y","z" (feet),"yaw","pitch" (Minecraft degrees),"sneak":bool,"riding":bool,"pkt":"position_look"}`
  (`riding` = `bot.vehicle` is set). The controller also writes the `seq` of the last datagram
  into every action sample (`pose_seq`, `windows/schema_parts/controller.md`), so that
  `process_recordings.py` can pair each sample with a frame that rendered exactly that packet;
- the mod (`pose_port` in its config, launcher `--pose-port`) receives them on a daemon
  thread (only from 127.0.0.1, only for its own bot, ignoring older `seq`) and, at the end of
  `Camera#setup` (1.21: `setup(BlockGetter, Entity, detached, mirrored, partialTick)`, called by
  `GameRenderer#renderLevel` before the projection and the frustum are built), replaces the
  camera pose by the newest datagram as-is (no extrapolation): position = (x, y + 1.62, z), or
  y + 1.27 when `sneak` (the eye height comes from the datagram, not from the camera player,
  whose server-side scale is set to 0.785 while its bot sneaks), rotation = (yaw, pitch).
  Frustum culling, section sorting, sky / fog, fluid-in-camera and the sound listener read
  the camera afterwards, so they use the injected pose. The crosshair pick (block outline,
  targeted entity) is computed from the same pose. The first-person hand and the HUD are
  drawn as before (view space);
- a datagram is used while it is at most `pose_max_age_ms` (500) old, measured on the mod's
  clock from its arrival, or longer while the camera player's server-followed pose still equals
  it (`pose_held`: the bot stands still; Mineflayer then sends one position packet per second).
  Otherwise, and before the first datagram, the vanilla server-followed camera is used
  (`pose_source: "server"`, `pose_fallback`). Chunk loading still follows the server-side
  camera player (the plugin keeps teleporting it every tick).
- limitations: the eye height switches instantly between 1.62 and 1.27 when the datagram's
  `sneak` flag changes (vanilla eases it in `Camera#tick`, half of the remaining difference per
  tick, so the vanilla camera takes ~3 ticks); swimming / crawling / gliding (eye height 0.4)
  are not modelled (rendered at 1.62); while the bot rides (`riding`) the vanilla camera is
  used (Mineflayer moves the vehicle with `vehicle_move` and sends no player position).

Camera fidelity vs training pairs. The camera renders the bot's latest packet (action samples
vs the packet the frame rendered, `pose_matched`: p99 0.0003°, float rounding). The training
data, however, pairs each action sample with a frame, and samples (controller, 20 Hz) and
frames (client, 20 fps) are taken asynchronously: with the time-only pairing ("first frame at
or after the sample") 4-20 % of the frames already show the next packet, so the frame-vs-label
error stays at p99 2.5-8.6° even with the pose link. `process_recordings.py` therefore pairs
by `pose_seq` (`windows/schema_parts/postprocess.md`).

Measured (3-episode stateful run, both cameras, NVENC, shared busy machine; same frames and
labels re-aligned both ways): every frame inside the episodes was injected; frame vs its
action label, injected frames: time pairing p99 2.55-8.59° per bot-episode (5.0 % of the
pairs > 0.2°), pose-seq pairing p99 0.0003° (0.09 % > 0.2°: 2 of 2240 samples whose packet no
neighbouring frame rendered); the server-followed `player_yaw/pitch` of the same frames vs the
same labels: 2.2-18 % > 0.2°, p99 3.6-8.6°. A towerBridge episode (sneaking) rendered
`cam_pos.y` = feet + 1.27 in all 271 injected sneaking frames. `pose_age_ms` p50 is ~200 ms
because the bots are often idle (see `pose_age_ms` above).

### Clean frames: no toasts, no chat (HUD otherwise vanilla)

The frames show the vanilla HUD, kept on purpose (hotbar, hearts, food, XP bar, crosshair,
held-item name, action bar / titles, player name tags) but nothing that only exists because
the camera is a real client:

- toasts: the mod cancels `ToastComponent#addToast` (and `#render`), so advancement, recipe,
  tutorial and system toasts (e.g. "chat messages can't be verified") are never queued or
  drawn (launcher `--suppress-toasts 1`, config `suppress_toasts`). The meta counts what was
  dropped (`toasts`);
- chat: the launcher writes `chatVisibility:2` (hidden; `--chat-visibility hidden|system|full`).
  The client then draws no chat lines, and the server sends it neither player chat nor
  non-overlay system messages (`/say`, `tellraw`, broadcasts, death / advancement announcements).
  Overlay system messages = the action bar are part of the HUD and stay visible.

Measured (two live buildAndBreak runs, both cameras; run 1: 3 episodes, a test driver granted
all 1399 advancements and every recipe to both camera players and sent `tellraw` / `say` /
`title actionbar` probes; run 2: 2 normal episodes): run 1 dropped 1393 toasts per camera
(1275 advancement, 117 recipe, 1 system), run 2 still 15 / 60 (recipe toasts when the camera
players receive items, plus the "chat can't be verified" system toast); the toast-sprite NCC
in the top-right toast slot stayed <= 0.05 in all 1241 sampled in-game frames (a drawn toast
scores ~1); no chat text in the chat area of any sampled frame; hotbar-sprite NCC median
0.75-0.84, hotbar found in 80-100 % of the sampled frames (misses = the hand / a placed block in
front of it); the action-bar probe and the held-item name are visible above the hotbar.

### Local skins (unsigned, rendered from the local pool)

The 1.21 client renders another player's skin only from a Mojang-signed `textures` property on
textures.minecraft.net. Instead of signing every skin (MineSkin upload), MirrorBot's local skin
mode (`/episode skin ... mode=local|auto`, `windows/schema_parts/r2-skins.md`) sets an UNSIGNED
property whose payload names a skin of the local pool:
`{"textures":{"SKIN":{"url":"http://solaris.local/skins/<skin_id>.png","metadata":{"model":"slim"}}},
"solaris":{"v":1,"skin_id","file","sha256","model"},...}`. The mod hooks
`SkinManager#getOrLoad(GameProfile)` (the entry point of `PlayerInfo#createSkinLookup`,
`lookupInsecure`, `getInsecureSkin`): for such a profile it loads `<skins_dir>/<skin_id>.png`
(else `<skins_dir>/<file name>`, `<skins_dir>/../<file>`, `<skins_dir>/../<skin_id>.png` for the
top-level session skins) on a background thread, accepts only 64x64 PNGs (other sizes give
the default skin), makes the base layer opaque (as the game does for downloaded skins) and
registers the image as an in-memory
`DynamicTexture`; it returns a `PlayerSkin` (`textureUrl` null) with the payload's model and
`secure = true`, so it is shown for other players and for the camera's own first-person arm.
No `HttpTexture` is involved, so no code path opens the `solaris.local` URL or writes to the
pool: a missing / undecodable file gives the default skin (`status` in the meta). The property
parse is memoised per property value (`getOrLoad` also runs per frame for player heads). All
other profiles (Mojang-signed skins) keep the vanilla path. Launcher:
`--local-skins 1` (config `local_skins`), `--skins-dir` (config `skins_dir`, default
`<repo>/server/skins/pool`; the server's copy has the same files).

**A skin change respawns the camera client.** `/episode skin` (local mode: Paper
`setPlayerProfile`; signed mode: SkinsRestorer's refresh) also gives the camera player the skin
(for its first-person arm), and re-sending a player's own profile to its client is a respawn:
the camera records `screen = "ReceivingLevelScreen"` in `camera_<bot>_frames.jsonl` for 1-3
frames and a capture hitch of ~130-190 ms (frame gap) at every apply; the other camera sees the
bot re-added (no screen). `controller/utils/skins.js` therefore applies skins only between
episodes (before the teleport and `worldstate begin`). Guards: the plugin logs a warning and
replies `"world_log_open": true` when `/episode skin` runs while a world log (episode
recording) is open (reply / note also carry `camera_respawn`), and
`postprocess/process_recordings.py` counts matched frames with a non-null `cam_screen` in the
alignment diagnostics (`screen_frames`, `screen_frames_by_screen`, with a warning); such frames
stay in the clip (dropping them would shift the action alignment) and are flagged per frame
by `frame_mapping[i].cam_screen`.

Verified live (3 + 2 buildAndBreak episodes, two cameras; run 1 skins applied over RCON,
run 2 by `controller/utils/skins.js` with its defaults, incl. procedural skins without any
`.customskin` and a default skin): 13 different local skins loaded, all `status: loaded`,
`sha256_match: true`, `skin_assignment.ok` in every episode (`pool_size` 320, ~1.6 s per
assignment); the camera registers the new texture 15-60 ms after the plugin applied it (also on
the camera's own first-person arm). In frames where the other bot is in view (bounding box from
the other camera's sidecar pose), the assigned skin was the best colour match among all 320 pool
skins in 85 of 160 scored crops and in the top 3 in 130 of 160 (`default_efe_wide` always
ranks 2 behind `default_efe_slim`, which has the same colours); the low-ranked crops are frames
where the bot is hidden behind placed blocks. Side-by-side crops vs the reference sheets match.
Re-verified after the switch to the in-memory `DynamicTexture` (3 buildAndBreak episodes, two
cameras, `SKIN_SOURCES=procedural SKIN_MODE=local`, verify_world_events exit 0): 6 unsigned
procedural skins per camera, all `status: loaded`, `sha256_match: true`, `requests: 1`,
`non_local_lookups` = `non_local_textures` = 3 / 4; no "http texture" line in the camera
logs and no pool file modified; skin of the other bot and the camera's own arm match the
reference sheets in full-frame side-by-sides (palette rank 1 in 8, top 3 in 20 of 80 scored
crops, mostly dark / occluded crops below); `ReceivingLevelScreen` frames only at the
bootstrap and the skin applies between episodes (none inside an episode window,
`screen_frames: 0` in all 6 aligned metas); 1630 sampled frames incl. 995 within 4 s of a
chat / say / action-bar / advancement / recipe probe: no chat text, toast-template NCC max
0.16 (1279 toasts dropped per camera), hotbar found in 89-98 %.

### options.txt written by the launchers (T2, T13)

Windows (`camera/windows/launch_camera.py`, existing user keys are kept):
`fov:0.0` (= 70°), `fovEffectScale:0.0`, `renderDistance:12`, `simulationDistance:12`,
`bobView:false`, `guiScale:2` (auto = round(height/360)), `damageTiltStrength:0.0`,
`pauseOnLostFocus:false`, `maxFps:60`, `enableVsync:false`, `inactivityFpsLimit:minimized`
(ignored by 1.21, which has no inactive-window frame cap), `soundCategory_master:0.0`,
`narrator:0`, `tutorialStep:none`, `skipMultiplayerWarning:true`, `joinedFirstServer:true`,
`onboardAccessibility:false`, `skipQuickPlayFirstLaunchPrompt:true`, `fullscreen:false`,
`chatVisibility:2` (hidden; `--chat-visibility`) (and `version:3953` if the file had none).

Linux / Docker (`camera/launch_minecraft.py`): `fov:0.0`, `fovEffectScale:0.0`,
`renderDistance` / `simulationDistance` from env `RENDER_DISTANCE` / `SIMULATION_DISTANCE`
(default 12; note `generate_compose.py` passes its own `--render_distance` /
`--simulation_distance`), `bobView:false`, `damageTiltStrength:0.0`, `guiScale` from env
`GUI_SCALE` (default 2), `pauseOnLostFocus:false`, `chatVisibility` from env `CHAT_VISIBILITY`
(`full`/`system`/`hidden`, default `hidden` = 2; recorded as `chat_visibility`, with
`toasts_suppressed: false`: toast suppression needs the solaris-capture mod, which only the
Windows-native cameras load, so Linux-path recordings can still show toasts). The values are recorded in
`camera/output_<bot>/0/camera_options.json` (directory of `RECORDING_PATH`):

| key | meaning |
|---|---|
| `options_txt` | the options.txt key/value strings written |
| `fov_deg`, `fov_effect_scale`, `render_distance`, `simulation_distance`, `bob_view`, `damage_tilt_strength`, `gui_scale`, `pause_on_lost_focus` | same values, typed |
| `smooth_camera`, `hud_visible`, `name_tags_visible` | false, true, true (not changed by the launcher) |
| `width`, `height` | client window size |

### Resolution, encoder and CPU budget (Windows)

Measured on the reference laptop (i9-13980HX, RTX 4070 Laptop, driver 566.07),
hidden windows, steady state after the first 15 s. NVENC cannot be used there with
ffmpeg 9.0.2: it needs NVENC API 13.1 (NVIDIA driver 610 or newer) and driver 566.07
provides 12.2, so `--encoder auto` falls back to libx264 (`-preset veryfast`,
launcher `--x264-preset`). ffmpeg 7.0.2 (`run_windows.py --ffmpeg-dir
C:\Users\<you>\tools\ffmpeg-7.0.2-essentials_build\bin`) uses an NVENC SDK the driver
supports; `auto` then selects `h264_nvenc`.

| setup | result |
|---|---|
| 1 or 2 cameras 1280x720, otherwise idle CPU | 19.9-20.0 fps, no frames dropped; per camera java ~0.4-0.7 cores, ffmpeg ~0.2-0.3 cores (CPU time per process roughly doubles when the machine is busy) |
| 2 cameras 1280x720, other load ~80-100 % | 19.2-19.5 fps, 0 frames dropped, ~1 % slots skipped (A/B in the same run: priority `normal` 18.8 fps with 71 frames dropped) |
| 1 camera 1920x1080, other load ~80 % | 19.98 fps, 0 dropped |
| 2 cameras 1920x1080, other load ~70-80 % | 19.4-19.9 fps, 11-47 frames dropped per minute; each ffmpeg ~1.8 cores (`ultrafast`: ~1.3 cores, 4x larger files, losses similar) |
| 1 camera 2560x1440 (earlier measurement, priority normal) | ~14 fps with dropped frames: not usable without NVENC |

1280x720 (default) is the safe size. 1920x1080 works with some frame loss when the
machine is busy (check `capture_stats` and the WARN lines). Larger sizes need NVENC:
update the NVIDIA driver (>= 610) or use an ffmpeg build with an NVENC SDK the
driver supports, then re-measure.
