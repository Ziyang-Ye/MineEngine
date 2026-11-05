"""Capture -> proxy: our patched SolarisEngine state logs (cap_reader) -> per episode and bot a white-box G-buffer
rendered from the exact block state, the per-frame state of the player and of the other player / mobs / items, the state
proxy video and training clips (layout: docs/whitebox_channels.md). One output frame per action entry; frame i of the aligned video
belongs to action entry i only for i < n_pair = len(frame_mapping) of aligned/<stem>_camera_meta.json (process_recordings drops
the trailing entries the recording no longer covers: diagnostics.unmatched_actions_end).
  pairing  entry i < n_pair is paired only if i >= diagnostics.unmatched_actions_start (earlier entries were matched to frame 0)
           and frame_mapping[i].delta_sec is in PAIR_WIN = [-1, 1.5] frames (1 / fps; a stalled / restarted recording reuses a
           later frame), or PAIR_WIN_POSE = [-2.25, 2.25] frames when pose_pair is same / prev / next (the frame rendered the
           entry's own packet; process_recordings recomputes delta_sec for a re-paired frame, so a "next" pair is >= 1 frame
           late and a "prev" one across a dropped capture slot > 1 frame early); a paired frame is clean without cam_screen
           (loading screen, chat, ...) and with a logged fov (the world was rendered). Clips, side_by_side and encode_todo only use usable
           entries (paired and clean: cams.npz pair_ok; clips inside contiguous runs of them); rejected entries are counted
           per reason (episode.json bots[b].rejected / n_rejected, summary.json pair_rejected). A video without its aligned
           meta (process_recordings failed after writing frames) is unpaired (n_pair 0).
  time     cap_reader.frame_times: renderTime when it is epoch ms (>= 1e12), else epochTime * 1000 (act_recorder's relative
           renderTime); source per bot (frame_time_source).
  camera   per entry (cams.npz cam_source, state.jsonl cam.source): "frame_mapping" = the camera the paired frame was
           rendered with (the capture mod's sidecar copied into frame_mapping: cam_pos = the eye, cam_yaw / cam_pitch MC
           degrees unwrapped; pose link "injected" frames = the bot's movement packet, "server" frames = the camera player);
           otherwise, with the pose link (pose_seq in the action entries), "action" = the entry's absolute yaw / pitch (null
           entries filled from the nearest valid one), eye = feet + 1.62 (1.27 while action.sneak, the datagram's flag);
           without the pose link (Docker / --no-pose-link) "camera_pose" = T12 camera records of the bot's camera player
           (cap_reader.camera_track: tick "cameras", camera_pose-like records or its own tick "players" entry; held: the last
           record at or before t; eye = feet + 1.62, x 0.785 while the bot sneaks = CameraManager's camera scale; records
           already at eye level are detected (median height / expected ~2) and used as the eye), else "action". Pitch
           clipped to +-1.569 rad for c2w and cams.npz "pitch" (camera_basis degenerates at 90 deg; the raw value is "pitch64").
  vfov     the capture mod's options.fov_deg / fov_effect_scale (the aligned meta's camera_params copy, or the camera meta
           via camera_meta_path: a Windows path's file name, camera/output_<bot>/<instance>/), else the older fov (a value
           in [-1, 1] is options.txt's scale: 70 + 40 v deg) / fovEffectScale, x (1 + 0.1 fovEffectScale): the camera player
           flies, so without fovEffectScale 0 (absent = 1) it renders fov x 1.1 (70 -> 77 deg); --vfov only when no meta gives
           a fov. Source "camera_meta" / "camera_meta_flying" / "default"; the per-frame projection fov of frame_mapping is
           kept as cams.npz vfov_frame (vfov_frame_mismatch counts usable frames > 0.5 deg off).
  skins    info["skin"] = {file, skin_id, sha1, sha256, model, slim, source, tags, texture_mode, applied_epoch_ms, ok, png,
           camera_check, camera_note, assignment_ok, problems}: the engine's state_metadata.skins[bot] (both bots' episode_info
           carry both; the bot's own file first) with the PNG exported next to an episode_info (<stem>_skin_<Bot>.png,
           tools/skins/export_episode_skins.py) or from --skin-pool (capture/server/skins/pool: <file>, then the legacy
           server/skins/<skin_id>.png) by file / skin_id, sha256-verified, sha1 computed, copied to <base>/output/skins/
           <sha1>.png (what entity_refs reads; output/skins/index.json maps sha1 -> skin_id, sha256, model, source, tags; a copy
           failure, e.g. a read-only batch, is a problem, an index failure only index_error); ok = no problem: skin_assignment
           applied[bot] true and no error of this bot (skins.js's ok counts both players' errors and the follower's "pool
           differed" note: kept as assignment_ok, advisory), the PNG verified, and - when the camera that sees the bot wrote a
           FINALIZED meta (stop_epoch_seconds / capture_stats: local_skins.skins is filled in only when the recording closes; a
           killed / crashed camera keeps its start-time meta -> camera_check None, camera_note) with local_skins (texture_mode
           local) - its "<skin_id>:<model>" entry loaded with sha256_match; applied_epoch_ms = that entry's loaded_epoch_ms.
           The schema layout's top-level skin {file, sha1, ...} is kept (ok = its PNG exists with that sha1). entity_refs only uses ok skins;
           summary.json skins counts ok / not ok / no PNG.
  valid    episode.json / clip meta valid = no bot's episode_info has encountered_error / peer_encountered_error or
           episode_recording_started false (invalid_reasons; flagged, still converted).
  blocks   BlockTimeline.states_at(t) (start snapshot + block_state, visible from the first frame with time >= epoch_ms): the
           full state strings, drawn by voxel_proxy.StateMesher from the client jar's models (mineengine.mc_registry registry
           json: --registry, else $MINEENGINE_REGISTRY, else $MINEENGINE_HOME/mc_registry_1.21.json; one without face alpha crops is
           refused; path / registry_id / geom_id / md5s printed at start and in summary.json registry): full cubes on the fast
           path, every other state from its model elements (variant / element rotation, cullface, texture-alpha crop; torches,
           flowers, crops, rails, buttons, redstone wire drawn), fluids by level, block entities from a hand table, invisible
           types none; the mesh follows every STATE version (door open, furnace lit, snow layers, water level, crop age; cams /
           state.jsonl state_version): the first one and changes on the box's x / z border mesh the padded grid as a whole,
           the others StateMesher.update (only the 16 x 16 columns around the changed cells, a few ms; leaf decay / water flow
           bursts stay cheap); --pad m around the box: flat worlds the most common border column,
           normal worlds that column cut at the border's median ground height (ground continuation, nothing above; edge
           repetition turned border trees / walls into 48 m ridges); pad_mode recorded.
  gbuf v2  (docs/whitebox_channels.md) gbuf.npz adds btype uint16 (index into types.json =
           [""] + sorted registry types; 0 = air / sky / nothing / entity, 65535 = a type outside the registry), bstate uint16
           (index into the episode's states.json "states": [null, {state, type, vkey, gid, tgid, mgid, btype, geom, legacy,
           known, exact[, implied]}, ...]; 0 = nothing; water surfaces of waterlogged / seagrass / kelp cells carry the
           implied "minecraft:water[level=0]" entry), face uint8 (0 none, 1..6 = +x, -x, +y, -y, +z, -z of the surface normal
           turned toward the camera, dominant axis for rotated faces; entity boxes too), fx uint16 (FX_DOC: bits 0..3 = the
           T17 dig stage + 1 of state.jsonl self.dig on the pixels of the dug cell's geometry; bits 4.. reserved). Entities stay
           pid only: pid -> entity type per frame is state.jsonl (pid 1 = "other" (a player), entities[k] {pid, type, uuid,
           ...}). block / block_ext keep their old meaning (cap_palette ids, legacy_id: block_id of the drawn state; a
           decoration block_id folds to air / water, now drawn, has its material id or UNKNOWN, so no hit block pixel has
           block_ext 0). types.json / states.json are written to the episode dir and copied into every clip dir.
           episode.json gbv2 = registry / geom id, palette / rendered counts (states, types, gid / tgid / mgid groups), geometry
           kinds rendered, the rendered state strings; per bot gbv2 = state versions, whole meshes / updates / columns
           re-meshed, triangles, ms per whole mesh / update, render ms per frame, frames with fx.
  proxy    proxy.mp4 = the white-box only (plain_frames): geometry + block colours (state_proxy.colorize on bstate with
           state_rgb per palette entry, entity colours as before) + the dig cracks of the fx plane (world-space block
           appearance: dig_cracks, no target outline); no HUD / hand / crosshair / entity facing or HP bars;
           --hud-proxy also writes proxy_hud.mp4 = a HUD-decorated variant for visualisation (state_proxy.compose on
           block_ext; the default is plain_frames). side_by_side.mp4 =
           real | proxy.mp4.
  entities the other player (body box 0.6 x (h - 0.5), head box 0.5 m on top, h = 1.8 / 1.5 sneaking; pid 1; not drawn
           while dead; while riding, raised to the seat: mount_of / seat_height, state.jsonl other.riding / draw_pos; with
           --entity-lag-ms / --entity-lag-json the other player and the entities are drawn where the camera client shows them,
           at their logged positions that many ms before the action time (state other.draw_pos / entities[].draw_pos; state
           fields stay at the action time))
           + EntityTrack entities (mobs, items, primed TNT, ...) with their Minecraft sizes; pids 2..255 from a
           free list of the entities alive in the frame (kept while alive, reused least recently freed first; overflow
           not drawn). Colours (per frame team_of): items = cap_palette.item_rgb (T3 "name" = item name), TNT / falling
           blocks = their block colour, hostile mobs MOB_HOSTILE, other mobs MOB_PASSIVE (a reserved family >= 50 RGB from
           every shaded block colour; cap_selftest), other non-living = item_rgb(type). Without per-tick entity lists
           (EntityTrack mode "events") a mob whose position is older than 1 s is not drawn and flagged "stale" in state.
  state    health = the latest of tick health, entity_damage health_after, death (0) and respawn (20) at or before t (the
           HUD; pose-link captures draw max(hp, state hp_floor 0.5): CameraManager keeps the camera alive at >= half a heart); food (tick / action
           T16, else 20 = full bar; source per bot), held item / slot / hotbar (action entry, else tick), swing / hurt ages
           from swing / damage events, using (the tick's hand_raised, else use_start .. use_end), crosshair block (T16
           crosshair when logged: the action entry's state.cursor or a crosshair field, a block hit beyond REACH 4.5 m = no
           target (the controller casts 5 m, the client outlines within 4.5 m); else a voxel DDA within 4.5 m blocked by the
           other player / mobs), dig stage (T17, < 1 s old, block
           still there); the other player's pose / health / held / sneaking from the ticks, its T16 body / head yaw, food,
           hotbar from the ticks (MC degrees) or else its own action entry at or before t (state: radians; top level:
           radians or MC degrees, detected against its yaw); per entity uuid, type, pos, yaw (head), body_yaw, health, pid,
           rgb, stale; the events since the previous frame; per paired entry "pair" (frame_index, delta_ms, pose_source,
           pose_pair, screen, fov) and "usable".
Outputs OUT/episodes/<name>/{episode.json, world.npz (origin, start / final / end ids)} and per bot OUT/episodes/<name>/<bot>/
{gbuf.npz (logdepth, shade, pid, edge, block = palette v0 ids (cap_palette.to_v0: mc.py's 12 ids, what mc_proxy_style
reads), block_ext = cap_palette ids [F, 192, 336] uint8), cams.npz (c2w, yaw, pitch, other (NaN while the other is dead
/ unknown), eye, t_ms, vfov, other_alive, block_version, pitch64, n_pair, pair_ok, cam_source, vfov_frame),
state.jsonl (one line per action entry), proxy.mp4 (docstring "proxy", streamed in chunks of 200 frames), side_by_side.mp4
(aligned real video | proxy, first n_pair frames, real half black where not usable)} + gbuf.npz btype / bstate / face / fx and
OUT/episodes/<name>/{types.json, states.json} (docstring "gbuf v2"); --clips:
OUT/clips/<name>_<bot>_<start ds>/ = 81 frames at 16 fps every --hop s inside each run of usable entries (src_frames =
nearest action entries) with gbuf.npz, cams.npz, state.jsonl, whitebox.npz (lo / hi = box min / max+1,
static_cells = collidable cells from the feet - 1 up at the first frame (local), ids = exact cap ids at the first frame, edit_cells
/ edit_t_on / edit_t_off and change_cells / change_frame / change_id / change_prev = the block_state changes inside the clip, in
action-entry (src frame) units), meta.json (sky_iou 1.0 / player_iou None: camera and blocks are exact, not
measured - quality_source says so; state_source "capture", palette, video, n_pair) and OUT/encode_todo/<id>.json (aligned
video path + src_frames, when the video exists). Names: <batch dir relative to --root, or its basename when --root is the batch
itself>_EEEEEE_III; an existing episode.json from another base dir is a collision (error, never overwritten). Resumable: an
episode whose episode.json exists (rendered when rendering) is skipped unless --force; a failing episode is reported in
summary.json and the batch goes on (exit code 1 if any failed). --no-render: CPU only (no G-buffer / videos); --cpu-render:
the G-buffer from voxel_proxy's numpy rasteriser (same channels, no GPU); --select EEEEEE_III,... only those episodes.
  python -m mineengine.cap2proxy --root DIR --out DIR [--episodes N] [--select 000011_003] [--clips] [--no-render | --cpu-render] [--force]
                         [--vfov 70] [--pad 48] [--hop 2.5] [--registry REG.json] [--hud-proxy] [--skin-pool capture/server/skins/pool]
"""
import argparse
import bisect
import hashlib
import json
import math
import os
import re
import sys
import time
import traceback
from collections import Counter, deque
from pathlib import PurePosixPath, PureWindowsPath

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__)); CODE = os.path.dirname(HERE)
from mineengine import cap_palette as CP  # noqa: E402
from mineengine import cap_reader as CR  # noqa: E402

H, W, NF, FPS_OUT, CROP = 192, 336, 81, 16.0, (384, 672)
GB = ("logdepth", "shade", "pid", "block", "edge")
GB_EXT = GB + ("block_ext",)
GB_V2 = ("btype", "bstate", "face", "fx")                                        # docstring "gbuf v2"
GB_U16 = ("btype", "bstate", "fx")
REG_DEFAULT = os.path.join(os.environ.get("MINEENGINE_HOME") or os.path.join(os.path.expanduser("~"), ".cache", "mineengine"), "mc_registry_1.21.json")
FACE_NAMES = ("none", "+x", "-x", "+y", "-y", "+z", "-z")
FX_DIG = 15
FX_DOC = {"bits": "0..3 destroy stage + 1 (1..10, 0 = none) on the dug cell's geometry; 4 on_fire, 5 hurt, 6 glowing, 7 frozen, 8 wet, 9 glint reserved (0)",
          "dig_mask": FX_DIG}
PLAIN_KEYS = ("logdepth", "shade", "pid", "bstate", "edge", "fx", "face")
DECO_RGB = (("torch", (235, 170, 55)), ("lantern", (235, 170, 55)), ("fire", (240, 120, 30)), ("redstone", (170, 25, 25)), ("rail", (120, 105, 85)),
            ("cobweb", (220, 220, 220)), ("wheat", (190, 170, 70)), ("beetroot", (150, 60, 60)), ("poppy", (190, 30, 30)), ("rose", (190, 30, 30)),
            ("red_", (190, 30, 30)), ("dandelion", (230, 200, 40)), ("sunflower", (230, 200, 40)), ("orange", (230, 120, 30)), ("orchid", (40, 150, 220)),
            ("cornflower", (70, 100, 220)), ("allium", (170, 100, 220)), ("lilac", (190, 140, 200)), ("pink", (235, 150, 180)), ("peony", (235, 150, 180)),
            ("white", (225, 225, 225)), ("oxeye", (225, 225, 225)), ("azure", (225, 225, 225)), ("lily_of", (225, 225, 225)), ("mushroom", (150, 110, 80)),
            ("", (85, 140, 50)))                                                 # drawn decorations without a material colour; last = plants
_REG = {}
REACH, MAX_HP, FOOD_FULL, CHUNK, STALE_MS, PITCH_MAX = 4.5, 20.0, 20.0, 200, 1000.0, 1.5690
PAIR_WIN, PAIR_WIN_POSE, POSE_EXACT = (-1.0, 1.5), (-2.25, 2.25), ("same", "prev", "next")   # frame_mapping delta_sec windows in frames (docstring "pairing")
CAM_MIN_HP = 0.5                                                                 # CameraManager.MIN_CAMERA_HEALTH: the camera's HUD never shows < half a heart
ALIGN_KEYS = ("sidecar_used", "sidecar_reason", "match_clock", "pose_seq_pairing", "screen_frames", "duplicate_frame_count", "duplicate_frame_count_after",
              "dropped_frame_gaps", "interior_unconsumed_count", "max_abs_delta_sec")
EV_KINDS = ("swing", "damage", "death", "respawn", "place", "break", "explode", "prime", "spawn", "remove", "held", "use_start", "use_end", "dig")
MOB_HOSTILE, MOB_PASSIVE = (50, 255, 0), (0, 0, 205)                           # reserved mob family (cap_selftest: >= 50 RGB from shaded blocks)
HOSTILE = {"zombie", "husk", "drowned", "zombie_villager", "skeleton", "stray", "bogged", "wither_skeleton", "creeper", "spider", "cave_spider",
           "enderman", "witch", "pillager", "vindicator", "evoker", "ravager", "vex", "slime", "magma_cube", "phantom", "blaze", "ghast", "silverfish",
           "endermite", "guardian", "elder_guardian", "piglin_brute", "hoglin", "zoglin", "warden", "breeze", "shulker", "wither"}
NONLIVING = {"item", "experience_orb", "tnt", "falling_block", "arrow", "spectral_arrow", "trident", "snowball", "egg", "ender_pearl", "potion",
             "fireball", "small_fireball", "wither_skull", "boat", "chest_boat", "minecart", "armor_stand", "item_frame", "glow_item_frame", "painting",
             "firework_rocket", "fishing_bobber", "end_crystal", "area_effect_cloud", "marker", "interaction", "block_display", "item_display",
             "text_display", "lightning_bolt", "leash_knot", "evoker_fangs", "llama_spit", "shulker_bullet", "dragon_fireball", "wind_charge"}
NO_HUD = ("item", "experience_orb", "tnt", "falling_block", "arrow")            # entities without facing / HP overlays


# ---- geometry ---------------------------------------------------------------------------------------------------------
def camera_basis(yaw, pitch):
    """= voxel_proxy.camera_basis (without the torch import): Mineflayer yaw / pitch -> forward, right, up."""
    f = np.array([-math.sin(yaw) * math.cos(pitch), math.sin(pitch), -math.cos(yaw) * math.cos(pitch)])
    r = np.cross(f, [0.0, 1.0, 0.0]); r /= np.linalg.norm(r) + 1e-12
    return f, r, np.cross(r, f)


def c2w_from(eye, yaw, pitch):
    """Camera-to-world 4 x 4 (camera x right, y down, z forward)."""
    f, r, u = camera_basis(yaw, pitch)
    m = np.eye(4); m[:3, :3] = np.stack([r, -u, f], 1); m[:3, 3] = eye
    return m


def ray_box(o, d, lo, hi):
    """slab test -> entry distance or inf."""
    with np.errstate(divide="ignore", invalid="ignore"):
        t1, t2 = (lo - o) / d, (hi - o) / d
    tn, tf = np.nanmax(np.minimum(t1, t2)), np.nanmin(np.maximum(t1, t2))
    return float(max(tn, 0.0)) if tf >= max(tn, 0.0) else math.inf


def crosshair(ids, origin, eye, f, reach=REACH, boxes=()):
    """first colliding cell on the view ray within reach (voxel DDA on the id grid) unless an entity box is hit first."""
    o = np.asarray(eye, np.float64); d = np.asarray(f, np.float64); c = np.floor(o).astype(int); step = np.sign(d).astype(int)
    with np.errstate(divide="ignore", invalid="ignore"):
        tdel = np.where(d != 0, np.abs(1.0 / d), np.inf)
        tmax = np.where(d > 0, (c + 1 - o) / d, np.where(d < 0, (c - o) / d, np.inf))
    tb = min([ray_box(o, d, lo, hi) for lo, hi in boxes] + [math.inf])
    t, sh = 0.0, ids.shape
    while t <= reach:
        i, j, k = c - origin
        if 0 <= i < sh[0] and 0 <= j < sh[1] and 0 <= k < sh[2] and CP.COLLIDES[min(int(ids[i, j, k]), CP.N - 1)]:
            return None if tb < t else [int(v) for v in c]
        ax = int(np.argmin(tmax)); t = float(tmax[ax]); c[ax] += step[ax]; tmax[ax] += tdel[ax]
    return None


def line_clear(ids, origin, a, b, n_per_m=4):
    """no colliding cell on the segment a-b (sampled; for the 'other player visible' bookkeeping)."""
    a, b = np.asarray(a, np.float64), np.asarray(b, np.float64); n = max(int(np.linalg.norm(b - a) * n_per_m), 2)
    p = a + (b - a) * np.linspace(0.02, 0.98, n)[:, None]; q = np.floor(p).astype(int) - origin
    ok = ((q >= 0) & (q < np.array(ids.shape))).all(1)
    return not CP.COLLIDES[np.minimum(ids[q[ok, 0], q[ok, 1], q[ok, 2]].astype(int), CP.N - 1)].any()


def collides_at(ids, origin, cell):
    q = np.asarray(cell, np.int64)[:3] - origin
    return bool((q >= 0).all() and (q < np.array(ids.shape)).all() and CP.COLLIDES[min(int(ids[tuple(q)]), CP.N - 1)])


def log_crosshair(v, reach=REACH):
    """T16 crosshair field (blockAtCursor / entityAtCursor) -> ("block", [x, y, z]) | ("entity", uuid) | ("none", None). A block
    farther than reach is "none": frame-recorder casts up to CURSOR_MAX_DISTANCE 5 m, the camera client's pick (no outline
    beyond it) the survival block range 4.5 m; an entity hit first hides the outline in both."""
    if not isinstance(v, dict):
        return ("none", None)
    ty = str(v.get("type") or v.get("kind") or ("entity" if v.get("uuid") or v.get("entity") else "block")).lower()
    if "entity" in ty:
        e = v.get("entity")
        return ("entity", str(v.get("uuid") or (e.get("uuid") if isinstance(e, dict) else e)))
    b = v.get("block") if isinstance(v.get("block"), dict) else v
    p = CR._pos(b)
    if p is None or (isinstance(b.get("distance"), (int, float)) and b["distance"] > reach):
        return ("none", None)
    return ("block", [int(math.floor(x)) for x in p])


# ---- episode inputs ---------------------------------------------------------------------------------------------------
def _parse_fov(m):
    """-> (fov deg, fovEffectScale or None): the capture mod's options.fov_deg / fov_effect_scale (the aligned meta copies them
    to camera_params), else the older fov (degrees or options.txt's scale) / fovEffectScale at top level or under options /
    camera / client_options; (None, None) without a fov."""
    for d in (m, m.get("camera_params"), m.get("options"), m.get("camera"), m.get("client_options")):
        if not isinstance(d, dict):
            continue
        if d.get("fov_deg") is not None:
            fes = d.get("fov_effect_scale")
            return float(d["fov_deg"]), (None if fes is None else float(fes))
        if d.get("fov") is not None:
            v = float(d["fov"]); fes = d.get("fovEffectScale", m.get("fovEffectScale"))
            return (70.0 + 40.0 * v if -1.0 <= v <= 1.0 else v), (None if fes is None else float(fes))
    return None, None


def camera_meta_paths(ep, bot):
    """aligned meta, then the camera's own meta: camera_meta_path as written (POSIX: relative to the batch / absolute), its file
    name under camera/output_<bot>/<instance>/ (also for a Windows path: run_windows writes the resolved C:\\... path, and a
    restarted camera camera_<bot>_r<n>_meta.json), then camera/output_<bot>/<instance>/camera_<bot>_meta.json."""
    f, b = ep["files"], bot.lower(); out = []
    cdir = os.path.join(ep["base"], "camera", f"output_{b}", str(int(ep.get("inst") or 0)))
    if f.get(f"video_meta_{bot}"):
        out.append(f[f"video_meta_{bot}"])
        try:
            cm = CR.load_json(f[f"video_meta_{bot}"]).get("camera_meta_path")
        except (OSError, ValueError, AttributeError):
            cm = None
        if cm:
            cm = str(cm); win = "\\" in cm or bool(re.match(r"^[A-Za-z]:", cm))
            if not win:
                out.append(cm if os.path.isabs(cm) else os.path.join(ep["base"], cm))
            out.append(os.path.join(cdir, (PureWindowsPath(cm) if win else PurePosixPath(cm)).name))
    out.append(os.path.join(cdir, f"camera_{b}_meta.json"))
    return list(dict.fromkeys(out))


def camera_meta(ep, bot, key):
    """(meta, path) of the first camera_meta_paths file that has `key`, else (None, None)."""
    for p in camera_meta_paths(ep, bot):
        if os.path.exists(p):
            try:
                m = CR.load_json(p)
            except (OSError, ValueError):
                continue
            if isinstance(m, dict) and m.get(key) is not None:
                return m, p
    return None, None


def vfov_of(ep, bot, default):
    """-> (vfov deg, source, note) (module docstring)."""
    for p in camera_meta_paths(ep, bot):
        if not os.path.exists(p):
            continue
        try:
            m = CR.load_json(p)
        except (OSError, ValueError):
            continue
        fov, fes = _parse_fov(m)
        if fov is None:
            continue
        if fes == 0.0:
            return fov, "camera_meta", os.path.relpath(p, ep["base"])
        k = 1.0 + 0.1 * (1.0 if fes is None else fes)
        print(f"[cap2proxy] WARNING {bot}: {os.path.relpath(p, ep['base'])} fov {fov:g} without fovEffectScale 0: flying camera, vfov {fov * k:g}", flush=True)
        return fov * k, "camera_meta_flying", f"{os.path.relpath(p, ep['base'])}: fov {fov:g}, fovEffectScale {'absent (= 1)' if fes is None else fes}: x {k:g}"
    return float(default), "default", "no camera meta with fov"


def gui_scale_of(ep, bot):
    """GUI scale of the camera's HUD from its meta (options / camera_params gui_scale_effective, gui_scale; the mod records 2 at
    720p) -> (scale, source); schema-layout / Docker captures: 3 (state_proxy's reference layout, Solaris)."""
    for p in camera_meta_paths(ep, bot):
        try:
            m = CR.load_json(p) if os.path.exists(p) else None
        except (OSError, ValueError):
            m = None
        for o in ((m or {}).get("options"), (m or {}).get("camera_params")):
            if isinstance(o, dict):
                g = o.get("gui_scale_effective") or o.get("gui_scale")
                if isinstance(g, (int, float)) and g > 0:
                    return float(g), "camera_meta"
    return 3.0, "default"


def paired_frames(ep, bot, F):
    """-> (n_pair or None, info): the aligned video has frames for action entries 0..n_pair-1 (module docstring "pairing");
    info ok [F] = entry i has its own frame (i >= diagnostics.unmatched_actions_start, frame_mapping[i].delta_sec in PAIR_WIN /
    fps, PAIR_WIN_POSE / fps for a pose-exact pose_pair), clean [F] = that frame shows the world (no cam_screen, fov logged),
    fm = frame_mapping[:n_pair], rejected = counts per reason, n_rejected = entries < n_pair not ok or not clean. A video without its aligned meta (a failed
    process_recordings leaves a partial mp4) or a meta without frame_mapping / n_matched: n_pair 0."""
    vp, mp = ep["files"].get(f"video_{bot}"), ep["files"].get(f"video_meta_{bot}")
    info = {"video": vp, "aligned_meta": mp, "fm": [], "ok": np.zeros(F, bool), "clean": np.ones(F, bool), "rejected": {}, "n_rejected": 0}
    if mp:
        try:
            m = CR.load_json(mp)
        except (OSError, ValueError) as e:
            m = {}; info["note"] = f"aligned meta unreadable: {e}"
        m = m if isinstance(m, dict) else {}
        fm, dg = m.get("frame_mapping") or [], m.get("diagnostics") or {}
        if fm or dg.get("n_matched") is not None:
            n = min(len(fm) if fm else int(dg["n_matched"]), F)
            if fm and [x.get("action_index") for x in fm] != list(range(len(fm))):
                info["note"] = "frame_mapping action_index is not 0..n-1"
            fps = float(m.get("fps") or 20.0); win = {k: (w[0] / fps - 1e-6, w[1] / fps + 1e-6) for k, w in (("time", PAIR_WIN), ("pose", PAIR_WIN_POSE))}
            us, rej = int(dg.get("unmatched_actions_start") or 0), Counter()
            for i in range(n):
                x = fm[i] if i < len(fm) else {}
                d = x.get("delta_sec"); lo, hi = win["pose" if x.get("pose_pair") in POSE_EXACT else "time"]
                if i < us:                                                       # matched to frame 0 although earlier than the recording
                    rej["before_recording"] += 1
                elif isinstance(d, (int, float)) and not lo <= d <= hi:          # dropped / stalled frames: a later frame reused
                    rej["delta"] += 1
                else:
                    info["ok"][i] = True
                if x.get("cam_screen"):
                    info["clean"][i] = False; rej["screen"] += 1
                elif "fov" in x and x["fov"] is None:
                    info["clean"][i] = False; rej["no_world_render"] += 1
            info.update(fm=fm[:n], n_pair=n, unmatched_actions_end=int(dg.get("unmatched_actions_end", F - n)), unmatched_actions_start=dg.get("unmatched_actions_start"),
                        rejected=dict(rej), n_rejected=int(n - (info["ok"] & info["clean"])[:n].sum()), pair_window_s={k: list(v) for k, v in win.items()},
                        align={k: dg[k] for k in ALIGN_KEYS if k in dg})
            return n, info
        info.update(n_pair=0, note=info.get("note") or "aligned meta without frame_mapping / n_matched: unpaired")
        return 0, info
    if vp:
        info.update(n_pair=0, note="aligned video without its aligned meta (process_recordings failed after writing frames?): unpaired")
        return 0, info
    info["n_pair"] = None
    return None, info


def fm_camera(x):
    """frame_mapping entry -> (eye, yaw, pitch) of the camera the frame was rendered with (cam_pos = the eye, cam_yaw / cam_pitch
    MC degrees, yaw unwrapped -> Mineflayer rad), or None when the sidecar pose is missing."""
    p, y, q = x.get("cam_pos"), x.get("cam_yaw"), x.get("cam_pitch")
    if not (isinstance(p, (list, tuple)) and len(p) >= 3 and all(isinstance(v, (int, float)) for v in p[:3])
            and isinstance(y, (int, float)) and isinstance(q, (int, float))):
        return None
    yw, pt = CR.mc_to_mineflayer(float(y), float(q))
    return np.asarray(p[:3], np.float64), yw, pt


def runs(mask):
    """[(i0, i1)] of the contiguous True stretches of a bool mask."""
    d = np.diff(np.r_[0, np.asarray(mask, bool).astype(np.int8), 0])
    return list(zip(np.nonzero(d == 1)[0].tolist(), np.nonzero(d == -1)[0].tolist()))


def _digest(path, algo):
    with open(path, "rb") as fh:
        return hashlib.new(algo, fh.read()).hexdigest()


def assign_problems(asg, bot, names):
    """skin_assignment (of the file that lists bot in applied) -> problems of bot. skins.js sets ok = no error at all: in the
    leader's file over BOTH players, in the follower's also for its note "<bot>: leader applied <id> (pool differed between
    bots)" (record corrected, applied true). Counted: applied[bot] not true, errors "<bot>: ..." except that note, and errors
    naming no player (an exception message)."""
    out = [] if asg["applied"].get(bot) is True else [f"skin_assignment applied {asg['applied'].get(bot)}"]
    for e in (str(x) for x in asg.get("errors") or []):
        who = e.split(":", 1)[0].strip() if ":" in e else None
        if (who == bot and "(pool differed between bots)" not in e) or who not in names:
            out.append(f"skin_assignment error: {e}")
    return out


def skin_copy(sd, rec, data):
    """verified skin bytes -> sd/<sha1>.png (content-addressed; pid-unique temp + rename, so concurrent runs write identical
    files) and its entry in sd/index.json (advisory: re-read just before a pid-unique temp + rename; a concurrent writer can
    drop an entry, never a PNG) -> a problem string (OSError: read-only batch, ...) or None; an index error -> rec["index_error"]."""
    dst, tmp = os.path.join(sd, rec["sha1"] + ".png"), f".{os.getpid()}.part"
    try:
        if not os.path.exists(dst):
            os.makedirs(sd, exist_ok=True)
            with open(dst + tmp, "wb") as fh:
                fh.write(data)
            os.replace(dst + tmp, dst)
    except OSError as e:
        return f"cannot copy the PNG to {dst}: {e}"
    ip, ent = os.path.join(sd, "index.json"), {k: rec[k] for k in ("skin_id", "sha256", "model", "slim", "source", "tags")}
    try:
        try:
            idx = CR.load_json(ip)
        except (OSError, ValueError):
            idx = {}
        idx = idx if isinstance(idx, dict) else {}
        if idx.get(rec["sha1"]) != ent:
            idx[rec["sha1"]] = ent
            with open(ip + tmp, "w", encoding="utf-8") as fh:
                json.dump(idx, fh, indent=1)
            os.replace(ip + tmp, ip)
    except OSError as e:
        rec["index_error"] = f"{type(e).__name__}: {e}"
    return None


def skin_record(ep, bot, infos, pool=None):
    """-> info["skin"] of one bot (module docstring "skins") or None (no skin metadata); unreadable PNGs / a read-only batch are
    problems (ok false), never exceptions."""
    base, f = ep["base"], ep["files"]
    own = infos.get(bot) or {}
    if isinstance(own.get("skin"), dict):                                        # T18 (schema layout): top-level record + output/skins/<sha1>.png
        s = dict(own["skin"]); p = os.path.join(base, "output", "skins", f"{s.get('sha1')}.png")
        try:
            s["ok"] = bool(s.get("sha1")) and os.path.exists(p) and _digest(p, "sha1") == s["sha1"]
        except OSError:
            s["ok"] = False
        return s
    order = [bot] + sorted(b for b in infos if b != bot)
    sm = {b: (infos.get(b) or {}).get("state_metadata") or {} for b in order}
    s = next((sm[b]["skins"][bot] for b in order if isinstance(sm[b].get("skins"), dict) and isinstance(sm[b]["skins"].get(bot), dict)), None)
    if s is None:
        return None
    asg = next((sm[b]["skin_assignment"] for b in order if isinstance(sm[b].get("skin_assignment"), dict)
                and bot in (sm[b]["skin_assignment"].get("applied") or {})), None)    # the follower's file lists only its own name
    names = set(ep["bots"]) | {k for b in order if isinstance(sm[b].get("skins"), dict) for k in sm[b]["skins"]}
    out = {k: s.get(k) for k in ("file", "skin_id", "sha256", "model", "source", "tags", "texture_mode")}
    out.update(slim=s.get("model") == "slim", sha1=None, applied_epoch_ms=None, png=None, camera_check=None, assignment_ok=None if asg is None else asg.get("ok"),
               problems=[] if asg is not None else ["no skin_assignment.applied entry"])
    if asg is not None:
        out["problems"] += assign_problems(asg, bot, names)
    cands = [os.path.join(os.path.dirname(f[f"info_{b}"]), os.path.basename(f[f"info_{b}"])[:-len("_episode_info.json")] + f"_skin_{bot}.png")
             for b in order if f.get(f"info_{b}")]                               # tools/skins/export_episode_skins.py
    if pool:                                                                     # LocalSkins.resolve: server/skins/<file>, then the legacy <skin_id>.png
        fl, pd = str(s.get("file") or ""), os.path.dirname(os.path.normpath(pool))
        cands += [os.path.join(pool, os.path.basename(fl)), os.path.join(pd, fl), os.path.join(pool, f"{s.get('skin_id')}.png"), os.path.join(pd, f"{s.get('skin_id')}.png")]
    found, data = [], None
    for p in dict.fromkeys(cands):
        if not os.path.isfile(p):
            continue
        found.append(p)
        try:
            with open(p, "rb") as fh:
                b_ = fh.read()
        except OSError:
            continue
        if s.get("sha256") and hashlib.sha256(b_).hexdigest() == s["sha256"]:
            out["png"], data = p, b_
            break
    if data is None:
        out["problems"].append(f"no PNG with sha256 {s.get('sha256')} ({len(found)} candidate files of {len(cands)} paths{'' if pool else ', no --skin-pool'})")
    else:
        out["sha1"] = hashlib.sha1(data).hexdigest()
        err = skin_copy(os.path.join(base, "output", "skins"), out, data)
        if err:
            out["problems"].append(err)
    key, ls, unfin = f"{s.get('skin_id')}:{s.get('model')}", [], []
    for o in (b for b in ep["bots"] if b != bot):                                # the cameras that see this bot
        m, mp_ = camera_meta(ep, o, "local_skins")
        if m is None or not isinstance(m["local_skins"], dict) or not m["local_skins"].get("enabled", True):
            continue
        if m.get("stop_epoch_seconds") is None and m.get("capture_stats") is None:   # start-time meta (camera killed / crashed): skins not filled in
            unfin.append(f"Camera of {o}: {os.path.basename(mp_)} not finalized (no stop_epoch_seconds / capture_stats)")
            continue
        ls.append((o, (m["local_skins"].get("skins") or {}).get(key)))
    if unfin:
        out["camera_note"] = "; ".join(unfin)
    if ls and s.get("texture_mode") != "signed":
        good = [e for _, e in ls if isinstance(e, dict) and e.get("status") == "loaded" and e.get("sha256_match") is True]
        out["camera_check"] = len(good) == len(ls)
        out["applied_epoch_ms"] = min((e["loaded_epoch_ms"] for e in good if isinstance(e.get("loaded_epoch_ms"), (int, float))), default=None)
        if not out["camera_check"]:
            out["problems"].append(f"camera local_skins {key}: " + ", ".join(f"Camera of {o}: {(e or {}).get('status', 'absent')}, sha256_match "
                                                                              f"{(e or {}).get('sha256_match')}" for o, e in ls))
    out["ok"] = not out["problems"]
    return out


def validity(infos):
    """episode_info flags of every bot -> reasons the episode is not clean (empty = valid)."""
    bad = []
    for b, i in sorted(infos.items()):
        bad += [f"{b}.{k}" for k in ("encountered_error", "peer_encountered_error") if i.get(k)]
        if i.get("episode_recording_started") is False:
            bad.append(f"{b}.episode_recording_started false")
    return bad


def health_series(pt, ix, name):
    """(t, hp) of the HUD: tick health + damage health_after + death 0 + respawn 20, sorted by time (stable)."""
    rows = []
    if name in pt:
        tr = pt.tracks[name]
        rows += [(float(t), float(r["health"]), 1) for t, r in zip(tr["t"], tr["rec"]) if isinstance(r.get("health"), (int, float))]
    for e in ix.all("damage", name):
        if isinstance(e.get("health_after"), (int, float)):
            rows.append((e["t"], max(float(e["health_after"]), 0.0), 0))
    rows += [(e["t"], 0.0, 0) for e in ix.all("death", name)]
    for e in ix.all("respawn", name):
        h = e["raw"].get("health")
        rows.append((e["t"], float(h) if isinstance(h, (int, float)) else MAX_HP, 0))
    rows.sort(key=lambda r: (r[0], r[2]))
    return np.array([r[0] for r in rows], np.float64), np.array([r[1] for r in rows], np.float64)


def at_series(ts, vs, t, default=None):
    i = int(np.searchsorted(ts, t, side="right")) - 1
    return float(vs[i]) if i >= 0 else default


def _ffill_angles(acts):
    y = np.array([np.nan if a.get("yaw") is None else float(a["yaw"]) for a in acts]); p = np.array([np.nan if a.get("pitch") is None else float(a["pitch"]) for a in acts])
    ok = np.isfinite(y) & np.isfinite(p)
    if not ok.any():
        return None, None
    idx = np.where(ok, np.arange(len(y)), -1); np.maximum.accumulate(idx, out=idx); idx[idx < 0] = int(np.argmax(ok))
    return y[idx], p[idx]


def angle_units(acts, fields=CR.ANG_EXTRA):
    """'rad' (Mineflayer, like yaw) or 'deg' (Minecraft) for T16 body / head yaw written in the action entries: frame-recorder's
    nested "state" is radians (cap_reader.t16_of); top-level fields: the one closer to the entry's own yaw over the file (None
    when absent)."""
    dr, dd = [], []
    for a in acts:
        t16, u = CR.t16_of(a)
        if u is not None and any(isinstance(t16.get(f), (int, float)) for f in fields):
            return u
        if a.get("yaw") is None:
            continue
        for f in fields:
            if isinstance(t16.get(f), (int, float)):
                v, y = float(t16[f]), float(a["yaw"])
                dr.append(abs((v - y + math.pi) % CR.TAU - math.pi)); dd.append(abs((CR.mc_to_mineflayer(v, 0.0)[0] - y + math.pi) % CR.TAU - math.pi))
    return None if not dr else "rad" if np.median(dr) <= np.median(dd) else "deg"


def using_at(ix, name, t):
    s, e = ix.last("use_start", name, t), ix.last("use_end", name, t)
    return bool(s is not None and (e is None or e["t"] < s["t"]))


def age(ev, t):
    return None if ev is None else float(t - ev["t"])


def clean(o, n=6):
    """JSON-able copy: numpy -> lists / scalars, floats rounded to n dp, NaN / inf -> None."""
    if isinstance(o, dict):
        return {str(k): clean(v, n) for k, v in o.items()}
    if isinstance(o, (list, tuple, np.ndarray)):
        return [clean(v, n) for v in o]
    if isinstance(o, (bool, np.bool_)):
        return bool(o)
    if isinstance(o, (int, np.integer)):
        return int(o)
    if isinstance(o, (float, np.floating)):
        return round(float(o), n) if math.isfinite(o) else None
    return o


def write_jsonl(path, rows):
    with open(path + ".part", "w", encoding="utf-8") as fh:
        for r in rows:
            fh.write(json.dumps(clean(r), separators=(",", ":")) + "\n")
    os.replace(path + ".part", path)


def save_npz(path, **arrs):
    with open(path + ".part", "wb") as fh:
        np.savez_compressed(fh, **arrs)
    os.replace(path + ".part", path)


def save_json(path, obj):
    with open(path + ".part", "w", encoding="utf-8") as fh:
        json.dump(clean(obj), fh, indent=1, default=lambda o: o.tolist() if hasattr(o, "tolist") else str(o))
    os.replace(path + ".part", path)


def load_registry(path=None):
    """block registry (mineengine.mc_registry.Registry): path, else $MINEENGINE_REGISTRY, else REG_DEFAULT; loaded once per path. A registry
    without face alpha crops (built without PIL) is refused: plants / torches / rails would be drawn as full
    16 x 16 planes."""
    p = path or os.environ.get("MINEENGINE_REGISTRY") or REG_DEFAULT
    if not os.path.exists(p):
        raise FileNotFoundError(f"block registry {p} missing (build it: python -m mineengine.mc_registry --jar 1.21.jar --out {REG_DEFAULT}; or --registry / $MINEENGINE_REGISTRY)")
    if p not in _REG:
        from mineengine import mc_registry as MR
        r = MR.Registry.load(p)
        if not r.alpha:
            raise ValueError(f"block registry {p} has no face alpha crops (geom.alpha): rebuild it with PIL (python -m mineengine.mc_registry --jar 1.21.jar --out {REG_DEFAULT})")
        _REG[p] = r; r.path = os.path.abspath(p)
    return _REG[p]


def registry_provenance(reg):
    """-> {path, registry_id, geom_id, alpha_models, mc_registry_md5, voxel_proxy_md5} (printed at start, summary.json)."""
    md5 = lambda f: _digest(f, "md5")[:8] if os.path.exists(f) else None          # noqa: E731
    return {"path": getattr(reg, "path", None), "registry_id": reg.d.get("registry_id"), "geom_id": getattr(reg, "geom_id", None), "alpha_models": len(reg.alpha),
            "mc_registry_md5": md5(os.path.join(HERE, "mc_registry.py")), "voxel_proxy_md5": md5(os.path.join(HERE, "voxel_proxy.py"))}


def legacy_id(state):
    """block / block_ext id of a drawn state: cap_palette.block_id, except a decoration it folds to air (or water when
    waterlogged), now drawn from its model: its material id (cap_palette's name rules without the fold: buttons -> planks /
    stone, saplings -> planks, ...), else UNKNOWN (the generic solid). A hit block pixel never has block_ext 0."""
    i = CP.block_id(state); n = CP.norm(state)
    if i in (CP.AIR, CP.WATER) and CP._deco(n):
        j = CP._resolve(n)
        return j if j not in (CP.AIR, CP.UNKNOWN) else CP.UNKNOWN
    return i


def state_rgb(state):
    """proxy colour of a drawn state: cap_palette's colour of its legacy_id; a decoration without a material (UNKNOWN) DECO_RGB
    by name token, kept off the player colours as the palette is (cap_palette._separate)."""
    i, n = legacy_id(state), CP.norm(state)
    if i != CP.UNKNOWN or not CP._deco(n):
        return tuple(int(v) for v in CP.BLOCK_RGB[i])
    return CP._separate(next(c for k, c in DECO_RGB if k in n))


def pad_grid(ids, origin, pad, flat, collides=None, air=None):
    """render grid: the ids padded by pad cells in x / z (module docstring) -> (grid, origin, pad_mode). collides / air: a
    bool LUT over the grid's values and its air value (default: cap_palette ids; a states_at grid passes its own)."""
    if pad <= 0:
        return ids, np.asarray(origin), "none"
    cols = np.concatenate([ids[0].T, ids[-1].T, ids[:, :, 0], ids[:, :, -1]], 0)    # border columns [n, sy]
    u, inv = np.unique(cols, axis=0, return_inverse=True)
    col = u[np.bincount(inv.reshape(-1)).argmax()].copy()
    mode = "border_column"
    if not flat:
        solid = CP.COLLIDES[np.minimum(cols.astype(np.int64), CP.N - 1)] if collides is None else collides[cols]
        top = np.where(solid.any(1), solid.shape[1] - 1 - np.argmax(solid[:, ::-1], 1), -1)
        g = int(np.floor(np.median(top))); col[g + 1:] = CP.AIR if air is None else air; mode = f"ground_continuation (y <= {g} of the box)"
    g_ = np.broadcast_to(col[None, :, None], (ids.shape[0] + 2 * pad, ids.shape[1], ids.shape[2] + 2 * pad)).copy()
    g_[pad:pad + ids.shape[0], :, pad:pad + ids.shape[2]] = ids
    return g_, np.asarray(origin) - [pad, 0, pad], mode


class Pids:
    """pid 2..255 per alive entity: kept while it lives, freed when it is gone, reused least recently freed first."""

    def __init__(self, lo=2, hi=255):
        self.free, self.of, self.overflow = deque(range(lo, hi + 1)), {}, 0

    def frame(self, uuids):
        alive = set(uuids)
        for u in [u for u in self.of if u not in alive]:
            self.free.append(self.of.pop(u))
        for u in uuids:
            if u not in self.of:
                if self.free:
                    self.of[u] = self.free.popleft()
                else:
                    self.overflow += 1
        return {u: self.of.get(u) for u in uuids}


def dig_cracks(img, fx, face, cam, cell, vfov=70.0):
    """dig cracks of the white-box proxy on one frame img [2h, 2w, 3] (in place): pixels whose fx carries a destroy stage (fx &
    FX_DIG = stage + 1; the h x w plane upsampled 2x) get state_proxy.CRACK's texels of that stage black, the texel = where the
    pixel ray meets the cell's cube face on the side of the pixel's face channel (exact on cube faces, approximate on model
    faces inside the cell). No target outline (UI)."""
    from mineengine import state_proxy as SPX
    up = lambda a: np.repeat(np.repeat(np.asarray(a), 2, 0), 2, 1)              # noqa: E731
    v, fc = up(np.asarray(fx) & FX_DIG).astype(np.int64), up(face).astype(np.int64)
    ys, xs = np.nonzero((v > 0) & (fc > 0))
    if cell is None or not len(ys):
        return img
    H2, W2 = img.shape[:2]; eye, yaw, pitch = cam; f, r, u = camera_basis(float(yaw), float(pitch))
    th = math.tan(math.radians(vfov) / 2); tw = th * W2 / H2; n = np.arange(len(ys))
    d = f + ((xs + 0.5) / W2 * 2 - 1)[:, None] * tw * r - ((ys + 0.5) / H2 * 2 - 1)[:, None] * th * u
    c0, e = np.floor(np.asarray(cell, np.float64)[:3]), np.asarray(eye, np.float64)
    ax = (fc[ys, xs] - 1) // 2; pl = c0[ax] + ((fc[ys, xs] - 1) % 2 == 0)       # face 1 / 3 / 5 = + side: the cell's far plane
    da = d[n, ax]; t = (pl - e[ax]) / np.where(np.abs(da) < 1e-9, 1e-9, da)
    X = e + t[:, None] * d; o1, o2 = np.where(ax == 0, 1, 0), np.where(ax == 2, 1, 2)
    uu, vv = X[n, o1] - c0[o1], X[n, o2] - c0[o2]
    tex = SPX.CRACK[np.clip((uu * 16).astype(int), 0, 15), np.clip((vv * 16).astype(int), 0, 15)] < v[ys, xs] / 10.0
    img[ys[tex], xs[tex]] = SPX.BLACK
    return img


def plain_frames(gb, rgb, team_of, t0=0, cams=None, digs=None, vfov=70.0):
    """white-box proxy (docstring "proxy") of G-buffer slices gb (logdepth, shade, pid, bstate, edge [F, h, w]; fx / face for
    the cracks) -> [F, 2h, 2w, 3] uint8: state_proxy.colorize on bstate with the state colours rgb (states.json "rgb"), the
    entities per team_of; cams [(eye, yaw, pitch)] + digs [cell or None] per frame add the dig cracks (dig_cracks)."""
    from mineengine import state_proxy as SPX
    out = SPX.colorize({"logdepth": gb["logdepth"], "shade": gb["shade"], "pid": gb["pid"], "block": gb["bstate"], "edge": gb["edge"]},
                       block_rgb=rgb, team=0, team_of=team_of, t0=t0)
    if cams is not None and digs is not None and "fx" in gb and "face" in gb:
        for k, c in enumerate(digs):
            if c is not None:
                dig_cracks(out[k], gb["fx"][k], gb["face"][k], cams[k], c, vfov)
    return out


def entity_rgb(e):
    ty = e["type"]
    if ty == "item":
        return tuple(int(v) for v in CP.item_rgb(e.get("item")))
    if ty in ("tnt", "falling_block"):
        return tuple(int(v) for v in CP.BLOCK_RGB[CP.TNT if ty == "tnt" else CP.block_id(e.get("block_state")) if e.get("block_state") else CP.UNKNOWN])
    if ty in HOSTILE:
        return MOB_HOSTILE
    if ty not in NONLIVING:
        return MOB_PASSIVE
    return tuple(int(v) for v in CP.item_rgb(ty))


def is_stale(mode, e, t):
    """a living entity whose position is older than STALE_MS without per-tick entity lists (EntityTrack mode 'events')."""
    return mode == "events" and e["type"] not in NONLIVING and (e.get("pos_t") is None or t - e["pos_t"] > STALE_MS)


RIDEABLE = ("horse", "donkey", "mule", "camel", "llama", "pig", "strider", "boat", "raft", "minecart")


def mount_of(pos, ents, tol=0.05):
    """the vehicle a player rides: a rideable entity at the player's logged position (the capture logs a passenger at its
    vehicle's position), else None."""
    for e in ents:
        if e.get("size") and any(k in e["type"] for k in RIDEABLE) and np.abs(np.asarray(e["pos"], np.float64) - pos).max() < tol:
            return e
    return None


def seat_height(e):
    """feet of a seated player above its vehicle's position: passenger offset 0.75 x vehicle height minus the player's riding
    offset 0.35 (0.85 m on a horse; matches the rider in real frames of a riding episode)."""
    return max(0.0, 0.75 * float(e["size"][1]) - 0.35)


LAG_JUMP_M = 3.0      # a move larger than this inside the lag window is a teleport / respawn, which the client shows at once


def drawn_pos(e):
    """where the other player / an entity is drawn: draw_pos (seated rider, --entity-lag-ms) when set, else its logged pos."""
    return e["draw_pos"] if e.get("draw_pos") is not None else e["pos"]


def entity_lag_of(a, name, bot):
    """ms by which this view's camera client draws the other player / entities behind their logged positions:
    --entity-lag-json {"<episode name>/<bot>": ms} for this view, else --entity-lag-ms (0 = off)."""
    tab = getattr(a, "_entity_lag_table", None)
    if tab is None:
        p = getattr(a, "entity_lag_json", None)
        with (open(p) if p else open(os.devnull)) as fh:
            tab = json.load(fh) if p else {}
        try:
            a._entity_lag_table = tab
        except AttributeError:
            pass
    v = tab.get(f"{name}/{bot}")
    return float(v) if isinstance(v, (int, float)) else float(getattr(a, "entity_lag_ms", 0.0) or 0.0)


def episode_name(ep, root):
    rel = os.path.relpath(ep["base"], root)
    pre = os.path.basename(os.path.abspath(ep["base"])) if rel == "." else rel.replace(os.sep, "-")
    return f"{pre}_{ep['eid']:06d}_{ep['inst']:03d}"


# ---- one episode --------------------------------------------------------------------------------------------------------
class EpisodeProxy:
    def __init__(self, ep, root, a):
        self.ep, self.a, f = ep, a, ep["files"]
        self.name = episode_name(ep, root)
        self.stats = {}
        self.ev = CR.load_events(f["events"], self.stats)
        self.snap = CR.load_snapshot(f["snap_start"])
        self.bt = CR.BlockTimeline(self.snap, self.ev)
        self.pt, self.ix = CR.PlayerTrack(self.ev), CR.EventIndex(self.ev)
        self.et = CR.EntityTrack(self.snap, self.ev, players=ep["bots"])
        self.begin = next((e for e in self.ev if e.get("type") == "episode_begin"), {})
        self.info = {b: CR.load_json(f[f"info_{b}"]) if f.get(f"info_{b}") else {} for b in ep["bots"]}
        for b in ep["bots"]:                                                     # skin adapter (module docstring "skins"); input files untouched
            try:
                sk = skin_record(ep, b, self.info, getattr(a, "skin_pool", None))
            except (OSError, ValueError, TypeError, KeyError, AttributeError) as e:     # malformed skin metadata never fails the episode
                sk = {"ok": False, "problems": [f"skin adapter: {type(e).__name__}: {e}"]}
            if sk is not None:
                self.info[b] = dict(self.info[b], skin=sk)
        self.invalid = validity(self.info)
        wt = next((i.get("world", {}).get("world_type") for i in self.info.values() if i.get("world")), None) or (self.begin.get("world") or {}).get("world_type")
        self.flat = str(wt or "").lower() == "flat"
        self.end = CR.load_snapshot(f["snap_end"]) if f.get("snap_end") else None
        self.evs = sorted([e for k in EV_KINDS for e in self.ix.all(k)], key=lambda e: (e["t"], e.get("seq") or 0))
        self.evt = [e["t"] for e in self.evs]
        self.hp, self.acts, self.units, self.sm = {}, {}, {}, None

    def mesher(self):
        """voxel_proxy.StateMesher over the timeline's palette (built once; registry: docstring "blocks") + the proxy colour
        LUT over its state palette."""
        if self.sm is None:
            from mineengine import voxel_proxy as VP
            self.reg = load_registry(getattr(self.a, "registry", None))
            self.sm = VP.StateMesher(self.reg, self.bt.palette, legacy_id)
            self.rgb = np.array([CP.SKY_RGB] + [state_rgb(e["state"]) for e in self.sm.palette[1:]], np.uint8)
        return self.sm

    def padded(self, s):
        """states_at grid -> (render grid (a private array), origin, pad_mode): padded like pad_grid (collides / air of the states)."""
        sm = self.mesher()
        coll = np.r_[CP.COLLIDES[np.minimum(np.asarray(self.bt.lut, np.int64), CP.N - 1)], False]
        g, o, mode = pad_grid(s, self.bt.origin, self.a.pad, self.flat, collides=coll, air=sm.air)
        return (g.copy() if g is s else g), tuple(int(v) for v in o), mode

    def state_mesh(self, s):
        """states_at grid -> (mesh, pad_mode): padded, meshed by the StateMesher as a whole."""
        g, o, mode = self.padded(s)
        return self.mesher().mesh(g, o), mode

    def palette_json(self):
        """types.json / states.json contents (docstring "gbuf v2")."""
        sm = self.mesher(); rid = self.reg.d.get("registry_id")
        return ({"format": "gbv2-types-v1", "registry_id": rid, "version": (self.reg.d.get("source") or {}).get("version"), "unknown": 65535, "types": sm.types},
                {"format": "gbv2-states-v1", "registry_id": rid, "geom_id": getattr(self.reg, "geom_id", None), "types": "types.json", "faces": list(FACE_NAMES),
                 "fx": FX_DOC, "states": sm.palette, "rgb": self.rgb.tolist()})

    def render_view(self, t, cams, R, H_=None, W_=None, vfov=70.0, entities=()):
        """the block world at time t from arbitrary cameras [(eye, yaw, pitch)] -> R.render output (tests / previews)."""
        s, _ = self.bt.states_at(t)
        mesh, _ = self.state_mesh(s)
        return R.render(mesh, cams, entities, H=H_ or self.a.hw[0], W=W_ or self.a.hw[1], vfov=vfov)

    def actions(self, bot):
        """-> (action entries, frame times) of a bot (cached; ([], []) without an action file)."""
        if bot not in self.acts:
            p = self.ep["files"].get(f"action_{bot}")
            acts = CR.load_actions(p) if p else []
            self.acts[bot] = (acts, CR.frame_times(acts)); self.units[bot] = angle_units(acts)
        return self.acts[bot]

    def act_state_at(self, name, t):
        """the action entry of `name` at or before t (else its first) -> cap_reader.action_state, or None."""
        acts, ts = self.actions(name)
        if not len(acts):
            return None
        return CR.action_state(acts[max(int(np.searchsorted(ts, t, side="right")) - 1, 0)])

    def hp_at(self, name, t):
        if name not in self.hp:
            self.hp[name] = health_series(self.pt, self.ix, name)
        return at_series(*self.hp[name], t)

    def player_state(self, name, t, act=None):
        """self (act = its action entry) or other player at t (module docstring)."""
        p = self.pt.at(name, t) or {}
        s = {"name": name, "health": self.hp_at(name, t), "food": p.get("food"), "food_source": "tick" if p.get("food") is not None else None,
             "sneaking": bool(p.get("sneaking")), "sprinting": bool(p.get("sprinting")), "on_ground": bool(p.get("on_ground", True)),
             "held_item": p.get("held_item"), "held_slot": p.get("held_slot"), "pos": p.get("pos"), "yaw": p.get("yaw"), "pitch": p.get("pitch"),
             "body_yaw": p.get("body_yaw"), "head_yaw": p.get("head_yaw"), "hotbar": p.get("hotbar9"),
             "crosshair": log_crosshair(p["crosshair"]) if "crosshair" in p else None}
        st = CR.action_state(act) if act is not None else None
        if act is not None:
            s.update(pos=st["pos"] if st["pos"] is not None else s["pos"], yaw=st["yaw"] if st["yaw"] is not None else s["yaw"],
                     pitch=st["pitch"] if st["pitch"] is not None else s["pitch"], sneaking=bool(st["sneaking"] or s["sneaking"]),
                     held_item=st["held_item"] if "heldItem" in (act.get("inventory") or {}) else s["held_item"],
                     held_slot=st["held_slot"] if st["held_slot"] is not None else s["held_slot"], hotbar=st["hotbar9"] or s["hotbar"],
                     keys={k: v for k, v in (st["keys"] or {}).items() if v not in (False, None)})
        elif None in (s["body_yaw"], s["head_yaw"], s["food"], s["hotbar"]):            # T16 written per frame: the other's own entry
            st = self.act_state_at(name, t)
            if st is not None and s["hotbar"] is None:
                s["hotbar"] = st["hotbar9"]
        if st is not None:
            u = self.units.get(name)
            for f in CR.ANG_EXTRA:
                if s[f] is None and isinstance(st.get(f), (int, float)):
                    v = float(st[f]); s[f] = v % CR.TAU if u != "deg" else CR.mc_to_mineflayer(v, 0.0)[0]
            for k in ("food", "health"):
                if isinstance(st.get(k), (int, float)) and s.get(k) is None:
                    s[k] = float(st[k]); s["food_source"] = "action" if k == "food" else s["food_source"]
            if s["crosshair"] is None and "crosshair" in st:
                s["crosshair"] = log_crosshair(st["crosshair"])
        if s["food"] is None:
            s["food"], s["food_source"] = FOOD_FULL, "default"
        if s["health"] is None and isinstance(p.get("health"), (int, float)):
            s["health"] = float(p["health"])
        s["alive"] = s["health"] is None or s["health"] > 0
        s["swing_age_ms"] = age(self.ix.last("swing", name, t), t); s["hurt_age_ms"] = age(self.ix.last("damage", name, t), t)
        hr = p.get("hand_raised")                                                # the tick's isHandRaised: the per-tick use truth
        s["using"], s["using_source"] = (hr, "tick") if isinstance(hr, bool) else (using_at(self.ix, name, t), "events")
        return s

    def camera_level(self, ct, bot):
        """camera_track adds the eye height to a record's pos (camera.getLocation() = the camera player's feet). If the records
        are already at eye level (getEyeLocation()), (eye - feet) / expected height is ~2 over the episode: re-read as eyes."""
        if bot not in self.pt:
            return ct, "no tick track to check the camera height"
        t, eye, y, p = ct; k = np.linspace(0, len(t) - 1, min(len(t), 200)).astype(int)
        q = [self.pt.at(bot, t[i]) for i in k]
        r = float(np.median([(eye[i, 1] - s["pos"][1]) / (CR.EYE * (CR.CAM_SNEAK_SCALE if s["sneaking"] else 1.0)) for i, s in zip(k, q)]))
        if abs(r - 2.0) < 0.25:
            return CR.camera_track(self.ev, bot, pos_is="eye"), f"camera pos at eye level (median {r:.2f} x the eye height above the feet): used as the eye"
        return ct, None if abs(r - 1.0) < 0.15 else f"camera eye {r:.2f} x the expected eye height above the bot's feet"

    def bot(self, bot, other, out_dir, R=None):
        a = self.a
        lag = entity_lag_of(a, self.name, bot)                                  # ms the camera client draws remote entities late
        acts, ts = self.actions(bot); F = len(acts)
        if not F:
            raise ValueError(f"{bot}: no action entries")
        if not np.isfinite(ts).all():
            raise ValueError(f"{bot}: {int((~np.isfinite(ts)).sum())} action entries without renderTime / epochTime")
        n_pair, pinfo = paired_frames(self.ep, bot, F)
        ok, fm = pinfo["ok"], pinfo["fm"]
        usable = np.ones(F, bool) if n_pair is None else (np.arange(F) < n_pair) & ok & pinfo["clean"]
        vfov, vsrc, vnote = vfov_of(self.ep, bot, a.vfov); gui, gsrc = gui_scale_of(self.ep, bot)
        link = any(x.get("pose_seq") is not None for x in acts) or any(x.get("pose_source") == "injected" for x in fm)
        ct = None if link else CR.camera_track(self.ev, bot); cam_note = "pose link: tick cameras (server-followed, lagging) not used" if link else None
        if ct is not None:
            ct, cam_note = self.camera_level(ct, bot)
            eye, yaw, pitch = CR.camera_at(ct, ts, mode=a.cam_mode); csrc = "camera_pose"
        else:
            y, p = _ffill_angles(acts)
            if y is None:
                tr = [self.pt.at(bot, t) for t in ts]; y = np.array([q["yaw"] for q in tr]); p = np.array([q["pitch"] for q in tr])
            eye = np.array([CR.action_state(x)["eye"] if all(x.get(k) is not None for k in "xyz") else self.pt.at(bot, t)["eye"] for x, t in zip(acts, ts)])
            yaw, pitch, csrc = y, p, "action"
        eye, yaw, pitch = np.array(eye, np.float64), np.array(yaw, np.float64), np.array(pitch, np.float64)
        src, vfr, psrc = np.full(F, csrc, dtype="U16"), np.full(F, np.nan), Counter()
        for i, x in enumerate(fm):                                               # the rendered camera of every paired frame (sidecar)
            if isinstance(x.get("fov"), (int, float)):
                vfr[i] = x["fov"]
            c = fm_camera(x) if ok[i] else None
            if c is not None:
                eye[i], yaw[i], pitch[i] = c; src[i] = "frame_mapping"; psrc[str(x.get("pose_source"))] += 1
        yaw = np.mod(yaw, CR.TAU); pc = np.clip(pitch, -PITCH_MAX, PITCH_MAX)
        origin = self.bt.origin
        rows, other_pos, other_alive, vers, svers = [], [], [], [], []
        pids, cnt = Pids(), Counter()
        k_ev = 0
        for i, (x, t) in enumerate(zip(acts, ts)):
            ids, ver = self.bt.grid_at(t)
            sver = self.bt.states_at(t)[1]
            me = self.player_state(bot, t, x)
            ot = self.player_state(other, t) if other in self.pt else None
            f_, r_, u_ = camera_basis(yaw[i], pc[i]); tv = math.tan(math.radians(vfov) / 2)
            raw = [e for e in self.et.at(t) if e["pos"] is not None]
            lag_pos = None
            if lag > 0:                                                          # no lag across a teleport / respawn: the client snaps
                now_pos = {e["uuid"]: e["pos"] for e in raw}
                lag_pos = {u: p for u, p in ((e["uuid"], e["pos"]) for e in self.et.at(t - lag) if e["pos"] is not None)
                           if u not in now_pos or np.linalg.norm(np.subtract(p, now_pos[u])) <= LAG_JUMP_M}
            obox = []
            if ot is not None and ot["pos"] is not None and ot["alive"]:
                otl = self.player_state(other, t - lag) if lag > 0 else None             # the camera client draws remote entities late
                if otl is not None and (otl["pos"] is None or np.linalg.norm(np.subtract(otl["pos"], ot["pos"])) > LAG_JUMP_M):
                    otl = None; cnt["lag_jump"] += 1
                h = 1.5 if ot["sneaking"] else 1.8; op = np.asarray(otl["pos"] if otl is not None and otl["pos"] is not None else ot["pos"], np.float64)
                mt = mount_of(op, raw if lag_pos is None else [dict(e, pos=lag_pos.get(e["uuid"], e["pos"])) for e in raw])   # riding: the log gives the vehicle's position
                if mt is not None:
                    op = op + [0, seat_height(mt), 0]; ot.update(riding=mt["type"]); cnt["riding"] += 1
                if mt is not None or lag_pos is not None:
                    ot["draw_pos"] = op.tolist()
                obox = [(op - [0.3, 0, 0.3], op + [0.3, h, 0.3])]
                v = op + [0, 0.9 * h, 0] - eye[i]; dist = float(np.linalg.norm(v))
                z = float(v @ f_)
                ot.update(dist=dist, in_front=bool(z > 0.05 and abs(v @ r_) / z < tv * W / H and abs(v @ u_) / z < tv),
                          line_of_sight=line_clear(ids, origin, eye[i], op + [0, 0.9 * h, 0]), pid=1, size=[0.6, h])
            pid_of = pids.frame([e["uuid"] for e in raw])
            ents = []
            for e in raw:
                stale = is_stale(self.et.mode, e, t)
                cnt["stale"] += stale
                ents.append({"uuid": e["uuid"], "type": e["type"], "pos": e["pos"], "yaw": e["yaw"], "body_yaw": e.get("body_yaw"), "pitch": e["pitch"], "health": e.get("health"),
                             "size": list(e["size"]), "item": e.get("item"), "pid": None if stale else pid_of[e["uuid"]], "rgb": entity_rgb(e), "stale": stale,
                             "hurt_age_ms": age(self.ix.last("damage", e["uuid"], t), t)})
                if lag_pos is not None:
                    ents[-1]["draw_pos"] = [float(v) for v in lag_pos.get(e["uuid"], e["pos"])]
            mob_boxes = [(np.asarray(drawn_pos(e)) - [e["size"][0] / 2, 0, e["size"][0] / 2],                  # what the client's crosshair hits
                          np.asarray(drawn_pos(e)) + [e["size"][0] / 2, e["size"][1], e["size"][0] / 2])
                         for e in ents if e["type"] not in ("item", "experience_orb") and not e["stale"]]
            xh = me.pop("crosshair")
            if not me["alive"]:
                me["target"], me["target_source"] = None, None
            elif xh is not None:                                                  # T16 logged: Mineflayer's own answer
                me["target"], me["target_source"] = (xh[1] if xh[0] == "block" else None), "log"
                if xh[0] == "entity":
                    me["target_entity"] = xh[1]
            else:
                me["target"], me["target_source"] = crosshair(ids, origin, eye[i], f_, REACH, obox + mob_boxes), "dda"
            cnt[f"target_{me['target_source']}"] += 1; cnt[f"food_{me['food_source']}"] += 1
            if ot is not None:
                ot.pop("crosshair", None)
            dg = self.ix.last("dig", bot, t)
            me["dig"] = {"cell": dg["pos"], "stage": dg["stage"]} if dg is not None and t - dg["t"] < 1000 and dg["phase"] in ("start", "progress") \
                and isinstance(dg.get("stage"), (int, float)) and 0 <= dg["stage"] <= 9 and dg.get("pos") is not None and collides_at(ids, origin, dg["pos"]) else None
            k1 = bisect.bisect_right(self.evt, t)
            evs = [{"kind": e["kind"], "t": e["t"], "name": e.get("name"), **({"victim": e.get("victim"), "attacker": e.get("attacker"), "damage": e.get("damage")}
                                                                             if e["kind"] == "damage" else {})} for e in self.evs[k_ev:k1]]
            k_ev = k1
            cnt[f"using_{me['using_source']}"] += 1
            me["hp_floor"], me["gui_scale"] = (CAM_MIN_HP if link else 0.0), gui  # MIN_CAMERA_HEALTH: pose-link captures only
            r_ = {"i": i, "t_ms": float(t), "frame_count": x.get("frame_count", i), "block_version": ver, "state_version": sver,
                  "paired": bool(n_pair is None or (i < n_pair and ok[i])),
                  "usable": bool(usable[i]), "cam": {"eye": eye[i], "yaw": yaw[i], "pitch": pitch[i], "vfov": vfov, "source": src[i]}, "self": me, "other": ot,
                  "entities": ents, "events": evs}
            if i < len(fm):                                                      # per-frame alignment quality (frame_mapping)
                q = fm[i]
                r_["pair"] = {"frame_index": q.get("frame_index"), "delta_ms": q["delta_sec"] * 1000.0 if isinstance(q.get("delta_sec"), (int, float)) else None,
                              "pose_source": q.get("pose_source"), "pose_pair": q.get("pose_pair"), "screen": q.get("cam_screen"), "fov": q.get("fov")}
            rows.append(r_)
            alive = bool(ot and ot["alive"] and ot["pos"] is not None)
            other_pos.append(ot["pos"] if alive else [np.nan] * 3); other_alive.append(alive)
            vers.append(ver); svers.append(sver)
        os.makedirs(out_dir, exist_ok=True)
        cams = {"c2w": np.stack([c2w_from(eye[i], yaw[i], pc[i]) for i in range(F)]).astype(np.float32), "yaw": yaw.astype(np.float32),
                "pitch": pc.astype(np.float32), "other": np.asarray(other_pos, np.float32), "eye": eye.astype(np.float32), "t_ms": ts.astype(np.float64),
                "vfov": np.float32(vfov), "other_alive": np.asarray(other_alive), "block_version": np.asarray(vers, np.int64), "yaw64": yaw, "pitch64": pitch,
                "n_pair": np.int64(F if n_pair is None else n_pair), "pair_ok": usable, "cam_source": src, "vfov_frame": vfr.astype(np.float32),
                "state_version": np.asarray(svers, np.int64)}
        save_npz(os.path.join(out_dir, "cams.npz"), **cams)
        sc, mv = Counter(src.tolist()), np.isfinite(vfr) & usable
        res = {"frames": F, "frame_time_source": CR.frame_time_source(acts), "camera_source": next(iter(sc)) if len(sc) == 1 else "mixed", "camera_sources": dict(sc),
               "pose_sources": dict(psrc), "pose_link": bool(link), "camera_note": cam_note, "vfov": vfov, "vfov_source": vsrc, "vfov_note": vnote, "gui_scale": gui, "gui_scale_source": gsrc,
               "vfov_frame_mismatch": int((np.abs(vfr[mv] - vfov) > 0.5).sum()), "span_ms": [float(ts[0]), float(ts[-1])], "n_usable": int(usable.sum()),
               **{k: v for k, v in pinfo.items() if k not in ("video", "aligned_meta", "fm", "ok", "clean")},
               "counts": dict(cnt), "pid_overflow": pids.overflow, "t16_angle_units_other": self.units.get(other), "entity_lag_ms": lag}
        gb = None
        if R is not None:
            gb = self.render(rows, eye, yaw, pc, vfov, R, res)
            save_npz(os.path.join(out_dir, "gbuf.npz"), **gb)
            vis = [bool((gb["pid"][i] == 1).any()) for i in range(F)]
            res["other_visible_frames"] = int(sum(vis))
            for r, v in zip(rows, vis):
                if r["other"] is not None:
                    r["other"]["rendered"] = v
            if not a.no_video:
                self.videos(bot, other, rows, gb, cams, vfov, out_dir, res, n_pair)
        write_jsonl(os.path.join(out_dir, "state.jsonl"), rows)
        return rows, cams, gb, res

    def render(self, rows, eye, yaw, pitch, vfov, R, res):
        """per action entry the G-buffer (GB + GB_V2); the mesh follows every state version: the first one and changes on the
        box's x / z border (they can change the padding) mesh the whole padded grid, any other one StateMesher.update (only
        the 16 x 16 columns around the changed cells). fx = the dig stage (state.jsonl self.dig) on its cell. res["gbv2"] = state
        versions meshed, whole meshes / updates / columns re-meshed, triangles (mean / max), quads per kind of the last mesh, ms
        per whole mesh / per update, render ms per frame, raster."""
        Hh, Ww = self.a.hw; F = len(rows); sm = self.mesher(); pad = self.a.pad
        gb = {k: np.zeros((F, Hh, Ww), np.uint16 if k in GB_U16 else np.uint8) for k in GB + GB_V2}
        cur, mesh, g, prev, tris = None, None, None, None, []
        st = Counter(); tm = Counter()
        for i, r in enumerate(rows):
            s, sver = self.bt.states_at(r["t_ms"])
            if sver != cur or mesh is None:
                t0 = time.perf_counter(); ch = None if mesh is None else np.argwhere(s != prev)
                if ch is None or (pad > 0 and len(ch) and ((ch[:, 0] == 0) | (ch[:, 0] == s.shape[0] - 1) | (ch[:, 2] == 0) | (ch[:, 2] == s.shape[2] - 1)).any()):
                    g, o, res["pad_mode"] = self.padded(s); mesh = sm.mesh(g, o); st["full"] += 1; tm["full"] += time.perf_counter() - t0
                elif len(ch):
                    q = ch + [max(pad, 0), 0, max(pad, 0)]; g[tuple(q.T)] = s[tuple(ch.T)]
                    mesh = sm.update(mesh, g, q); k_ = mesh.get("n_chunks", 0)
                    st["full" if k_ < 0 else "update"] += 1; st["chunks"] += max(k_, 0); tm["full" if k_ < 0 else "update"] += time.perf_counter() - t0
                prev = s.copy(); cur = sver; st["versions"] += 1
            ents = []
            ot = r["other"]
            if ot is not None and ot["pos"] is not None and ot["alive"]:
                h = ot["size"][1]; op = np.asarray(drawn_pos(ot), np.float64)                         # draw_pos: seated rider / --entity-lag-ms
                ents += [(0, tuple(op), (0.6, h - 0.5, 0.6)), (0, tuple(op + [0, h - 0.5, 0]), (0.5, 0.5, 0.5))]
            for e in r["entities"]:
                if e["pid"] is not None:
                    w_, h_ = e["size"]; ents.append((e["pid"] - 1, tuple(np.asarray(drawn_pos(e), np.float64)), (w_, h_, w_)))
            dg = r["self"].get("dig")
            fx = [[([int(math.floor(float(v))) for v in dg["cell"][:3]], int(dg["stage"]) + 1)]] if dg else None
            t0 = time.perf_counter()
            g1 = R.render(mesh, [(eye[i], float(yaw[i]), float(pitch[i]))], ents, H=Hh, W=Ww, vfov=vfov, fx=fx)
            tm["render"] += time.perf_counter() - t0; tris.append(len(mesh["F"]))
            for k in GB + GB_V2:
                gb[k][i] = g1[k][0]
        gb["block_ext"] = gb["block"]; gb["block"] = CP.to_v0(gb["block_ext"]).astype(np.uint8)   # 12-id consumers read "block"
        used = np.unique(gb["bstate"])
        res["gbv2"] = {"mesh_versions": st["versions"], "mesh_full": st["full"], "mesh_updates": st["update"], "chunks_remeshed": st["chunks"],
                       "tris_mean": float(np.mean(tris)) if tris else 0.0, "tris_max": int(max(tris, default=0)),
                       "quads_last": dict(mesh["n_quads"]) if mesh is not None else {}, "mesh_ms": round(1000 * tm["full"] / max(st["full"], 1), 2),
                       "update_ms": round(1000 * tm["update"] / max(st["update"], 1), 2), "render_ms_per_frame": round(1000 * tm["render"] / max(F, 1), 2),
                       "raster": getattr(R, "raster", None), "states_rendered": int((used > 0).sum()), "fx_frames": int((gb["fx"] > 0).any((1, 2)).sum())}
        return gb

    def frame_states(self, rows):
        from mineengine import state_proxy as SPX
        out = []
        for r in rows:
            me, ot = r["self"], r["other"]
            st = {"hp": None if me["health"] is None else max(me["health"], me.get("hp_floor", 0.0)), "max_hp": MAX_HP, "food": me["food"], "held_slot": me["held_slot"], "held": me["held_item"],
                  "swing_phase": SPX.swing_phase(me["swing_age_ms"]), "using": me["using"], "hurt_age_ms": me["hurt_age_ms"], "target": me["target"],
                  "dig": me["dig"], "gui_scale": me.get("gui_scale"), "others": []}
            if me["hotbar"] is not None:
                st["hotbar"] = [{"name": n, "count": c} if n else None for n, c in me["hotbar"]]
            if ot is not None and ot["pos"] is not None and ot["alive"]:
                st["others"].append(dict(pid=1, pos=ot["pos"], body_yaw=ot["body_yaw"] if ot["body_yaw"] is not None else ot["yaw"], head_yaw=ot["head_yaw"] if ot["head_yaw"] is not None else ot["yaw"],
                                         pitch=ot["pitch"], hp=ot["health"], max_hp=MAX_HP, hurt_age_ms=ot["hurt_age_ms"], swing_phase=SPX.swing_phase(ot["swing_age_ms"]),
                                         held=ot["held_item"], sneaking=ot["sneaking"], using=ot["using"], size=ot["size"]))
            for e in r["entities"]:
                if e["pid"] is not None and e["type"] not in NO_HUD:
                    st["others"].append(dict(pid=e["pid"], pos=e["pos"], body_yaw=e["body_yaw"] if e.get("body_yaw") is not None else e["yaw"] or 0.0,
                                             head_yaw=e["yaw"] or 0.0, pitch=e["pitch"] or 0.0,
                                             hurt_age_ms=e["hurt_age_ms"], size=e["size"]))
            out.append(st)
        return out

    def plain_proxy(self, gb, tofs, s0, s1, rows=None, cams=None, vfov=70.0):
        """frames s0..s1 of the white-box proxy (plain_frames; tofs[s0] holds over the slice; rows + cams {eye, yaw, pitch}: dig
        cracks of the rows' self.dig)."""
        sl = slice(s0, s1)
        C = None if cams is None else [(cams["eye"][k], float(cams["yaw"][k]), float(cams["pitch"][k])) for k in range(s0, s1)]
        D = None if rows is None else [(r["self"].get("dig") or {}).get("cell") for r in rows[sl]]
        return plain_frames({k: gb[k][sl] for k in PLAIN_KEYS if k in gb}, self.rgb, dict(tofs[s0]), s0, C, D, vfov)

    def videos(self, bot, other, rows, gb, cams, vfov, out_dir, res, n_pair):
        """proxy.mp4 (all frames, white-box only), --hud-proxy: proxy_hud.mp4 (HUD-decorated, for viewing) and side_by_side.mp4
        (real | proxy.mp4 for entries < n_pair; the real half black where the pair is not usable), CHUNK frames at a time."""
        from mineengine import state_proxy as SPX
        team = {"Alpha": 0, "Bravo": 1}.get(other, 1); hud = bool(getattr(self.a, "hud_proxy", False))
        states = self.frame_states(rows) if hud else None; F = len(rows); ok = cams["pair_ok"]
        cd = {"eye": cams["eye"].astype(np.float64), "yaw": cams["yaw64"], "pitch": np.clip(cams["pitch64"], -PITCH_MAX, PITCH_MAX), "vfov": vfov}
        tofs = [tuple(sorted({1: team, **{e["pid"]: e["rgb"] for e in r["entities"] if e["pid"] is not None}}.items())) for r in rows]
        vp = self.ep["files"].get(f"video_{bot}")
        real = real_frames(vp, 2 * self.a.hw[0], 2 * self.a.hw[1]) if vp and os.path.exists(vp) and n_pair else None
        wp, ws, n_real = VideoOut(os.path.join(out_dir, "proxy.mp4")), (VideoOut(os.path.join(out_dir, "side_by_side.mp4")) if real is not None else None), 0
        wh = VideoOut(os.path.join(out_dir, "proxy_hud.mp4")) if hud else None
        for c0 in range(0, F, CHUNK):
            c1 = min(F, c0 + CHUNK); parts, hparts, s0 = [], [], c0
            for s1 in range(c0 + 1, c1 + 1):
                if s1 == c1 or tofs[s1] != tofs[s0]:
                    parts.append(self.plain_proxy(gb, tofs, s0, s1, rows, cd, vfov))
                    if hud:
                        sl = slice(s0, s1)
                        hparts.append(SPX.compose({k: gb["block_ext" if k == "block" else k][sl] for k in GB}, states[sl], {k: (v[sl] if np.ndim(v) else v) for k, v in cd.items()},
                                                  block_rgb=CP.BLOCK_RGB, vfov=vfov, team=0, team_of=dict(tofs[s0]), t0=s0))
                    s0 = s1
            prox = np.concatenate(parts); wp.write(prox)
            if hud:
                wh.write(np.concatenate(hparts))
            if ws is not None and c0 < n_pair:
                k1 = min(c1, n_pair) - c0; rf = np.zeros((k1,) + prox.shape[1:], np.uint8)
                for j in range(k1):
                    fr = next(real, None)
                    if fr is None:
                        break
                    if ok[c0 + j]:
                        rf[j] = fr; n_real += 1
                ws.write(np.concatenate([rf, prox[:k1]], 2))
        wp.close(); res["proxy_video"] = "proxy.mp4"
        if hud:
            wh.close(); res["proxy_hud_video"] = "proxy_hud.mp4"
        if ws is not None:
            ws.close(); res["side_by_side"] = "side_by_side.mp4"; res["real_frames"] = n_real


class VideoOut:
    """streaming h264 mp4 (PyAV libx264, else imageio-ffmpeg, else OpenCV mp4v) written to path.part.mp4, renamed on close."""

    def __init__(self, path, fps=20, crf=10):
        self.path, self.tmp, self.fps, self.crf, self.kind = path, path + ".part.mp4", fps, crf, None

    def _open(self, Hh, Ww):
        try:
            import av
            self.c = av.open(self.tmp, "w", format="mp4"); s = self.c.add_stream("libx264", rate=int(round(self.fps)))
            s.width, s.height, s.pix_fmt, s.options = Ww, Hh, "yuv420p", {"crf": str(self.crf)}; self.s, self.av, self.kind = s, av, "av"
            return
        except ImportError:
            pass
        try:
            import imageio.v2 as iio
            self.w = iio.get_writer(self.tmp, format="FFMPEG", fps=self.fps, codec="libx264", quality=None, pixelformat="yuv420p", macro_block_size=1,
                                    output_params=["-crf", str(self.crf)]); self.kind = "imageio"
            return
        except Exception:                                                        # noqa: BLE001  (no imageio-ffmpeg)
            pass
        import cv2
        self.cv2, self.kind = cv2, "cv2"; self.w = cv2.VideoWriter(self.tmp, cv2.VideoWriter_fourcc(*"mp4v"), float(self.fps), (Ww, Hh))

    def write(self, frames):
        for fr in frames:
            fr = np.ascontiguousarray(fr)
            if self.kind is None:
                self._open(*fr.shape[:2])
            if self.kind == "av":
                for pk in self.s.encode(self.av.VideoFrame.from_ndarray(fr, format="rgb24")):
                    self.c.mux(pk)
            elif self.kind == "imageio":
                self.w.append_data(fr)
            else:
                self.w.write(fr[..., ::-1].copy())

    def close(self):
        if self.kind == "av":
            for pk in self.s.encode():
                self.c.mux(pk)
            self.c.close()
        elif self.kind is not None:
            (self.w.close if self.kind == "imageio" else self.w.release)()
        if self.kind is not None:
            os.replace(self.tmp, self.path)


def real_frames(path, Hh, Ww):
    """aligned real video -> frames resized to height Hh and centre-cropped / padded to width Ww (the renderer frame's rule), RGB."""
    import cv2
    cap = cv2.VideoCapture(path)
    try:
        while True:
            ok, fr = cap.read()
            if not ok:
                return
            h, w = fr.shape[:2]; nw = int(round(w * Hh / h)); fr = cv2.resize(fr, (nw, Hh), interpolation=cv2.INTER_AREA)[..., ::-1]
            out = np.zeros((Hh, Ww, 3), np.uint8)
            if nw >= Ww:
                x0 = (nw - Ww) // 2; out[:] = fr[:, x0:x0 + Ww]
            else:
                x0 = (Ww - nw) // 2; out[:, x0:x0 + nw] = fr
            yield out
    finally:
        cap.release()


def whitebox(P, ts, src, gy):
    """exact white-box of a clip (module docstring): first-frame grid + the block_state changes visible inside it."""
    ids0, _ = P.bt.grid_at(ts[src[0]], copy=True)
    gy0 = max(gy - 1, 0); sx, sy, sz = ids0.shape
    static = np.argwhere(CP.COLLIDES[np.minimum(ids0[:, gy0:, :], CP.N - 1)]).astype(np.int32) + np.array([0, gy0, 0], np.int32)
    ch = P.bt.changes_between(ts[src[0]], ts[src[-1]])
    fr = np.array([float(src[min(int(np.searchsorted(ts[src], c["t"], side="left")), len(src) - 1)]) for c in ch], np.float32)   # first src frame showing it
    cells = np.array([c["idx"] for c in ch], np.int32).reshape(-1, 3)
    edit, cur = {}, {}
    for c, f in zip(ch, fr):
        q = tuple(c["idx"]); was = cur.get(q, bool(CP.COLLIDES[min(int(c["prev_id"]), CP.N - 1)])); now = bool(CP.COLLIDES[min(int(c["id"]), CP.N - 1)])
        cur[q] = now
        if now and not was:
            edit.setdefault(q, [np.inf, np.inf])[0] = f; edit[q][1] = np.inf
        elif was and not now:
            edit.setdefault(q, [-1.0, np.inf])[1] = f
    ec = np.array(list(edit), np.int32).reshape(-1, 3); ev = np.array(list(edit.values()), np.float32).reshape(-1, 2)
    return dict(lo=P.bt.origin.astype(np.int64), hi=(P.bt.origin + [sx, sy, sz]).astype(np.int64), static_cells=static, ids=ids0.astype(np.uint16),
                edit_cells=ec, edit_t_on=ev[:, 0], edit_t_off=ev[:, 1], change_cells=cells, change_frame=fr,
                change_id=np.array([c["id"] for c in ch], np.uint16), change_prev=np.array([c["prev_id"] for c in ch], np.uint16), units=np.array("src_frame"))


def cut_clips(P, bot, rows, cams, gb, res, out, a):
    """windows of NF frames at FPS_OUT every a.hop s inside each contiguous run of usable pairs (cams pair_ok: paired, clean,
    < n_pair) -> OUT/clips/<id>/ (module docstring) -> ids. The id carries the window start in s from the bot's first entry."""
    lim = int(cams["n_pair"]); T = cams["t_ms"]; info = P.info.get(bot, {}); ids = []
    ok = cams["pair_ok"] if "pair_ok" in cams else np.arange(len(T)) < lim
    win = (NF - 1) / FPS_OUT
    vp = P.ep["files"].get(f"video_{bot}"); stem = os.path.basename(P.ep["files"][f"action_{bot}"])[:-5]
    wins = []
    for r0, r1 in runs(ok):
        ts = T[r0:r1]
        if r1 - r0 < 2:
            continue
        for w0 in np.arange(0.0, (ts[-1] - ts[0]) / 1000.0 - win - 0.05 + 1e-9, a.hop):
            tgt = ts[0] + (w0 + np.arange(NF) / FPS_OUT) * 1000.0
            j = np.clip(np.searchsorted(ts, tgt), 1, len(ts) - 1)
            wins.append(((ts[0] - T[0]) / 1000.0 + w0, r0 + np.where(np.abs(ts[j - 1] - tgt) <= np.abs(ts[j] - tgt), j - 1, j), (r0, r1)))
    for w0, src, run in wins:
        cid = f"{P.name}_{bot}_{int(round(w0 * 10)):04d}"; cd = os.path.join(out, "clips", cid); os.makedirs(cd, exist_ok=True)
        csrc = "+".join(sorted(set(cams["cam_source"][src].tolist()))) if "cam_source" in cams else res["camera_source"]
        if gb is not None:
            save_npz(os.path.join(cd, "gbuf.npz"), **{k: gb[k][src] for k in GB_EXT + GB_V2 if k in gb})
            for j_, v_ in zip(("types.json", "states.json"), P.palette_json()):
                save_json(os.path.join(cd, j_), v_)
        save_npz(os.path.join(cd, "cams.npz"), **{k: (v[src] if np.ndim(v) and len(v) == len(cams["t_ms"]) else v) for k, v in cams.items()})
        write_jsonl(os.path.join(cd, "state.jsonl"), [rows[q] for q in src])
        feet = min((float(r["self"]["pos"][1]) for r in (rows[q] for q in src) if r["self"]["pos"] is not None), default=float(P.bt.origin[1]))
        gy = int(math.floor(feet)) - int(P.bt.origin[1])
        wb = whitebox(P, cams["t_ms"], src, gy)
        save_npz(os.path.join(cd, "whitebox.npz"), **wb)
        ver = [int(cams["block_version"][src[0]]), int(cams["block_version"][src[-1]])]
        save_json(os.path.join(cd, "meta.json"), {
            "episode": P.name, "type": info.get("episode_type") or P.begin.get("episode_type"), "player": bot, "start_s": float(w0), "src_frames": src.tolist(),
            "vfov": res["vfov"], "fps": FPS_OUT, "crop": [2 * a.hw[0], 2 * a.hw[1]], "solid_cells": int(CP.COLLIDES[np.minimum(wb["ids"][:, max(gy, 0):, :], CP.N - 1)].sum()),
            "camera": "capture_" + csrc, "sky_iou": 1.0, "player_iou": None,
            "quality_source": "exact capture: camera and blocks from the state logs, sky_iou / player_iou not measured (set so the clip quality filter keeps them)",
            "edit": {"blocks": int(len(wb["edit_cells"])), "changing_in_clip": int(len(wb["change_cells"]))}, "state_source": "capture",
            "palette": {"block": "palette v0 (cap_palette.to_v0)", "block_ext": "cap_palette", "n": CP.N}, "skin": info.get("skin"), "vfov_source": res["vfov_source"],
            "camera_source": csrc, "frame_time_source": res["frame_time_source"], "eid": P.ep["eid"], "inst": P.ep["inst"], "pair_run": list(run),
            "entity_lag_ms": res.get("entity_lag_ms", 0.0),
            "valid": not P.invalid, "invalid_reasons": P.invalid,
            "t_ms": [float(cams["t_ms"][src[0]]), float(cams["t_ms"][src[-1]])], "block_version": ver, "changing_in_clip": int(len(wb["change_cells"])),
            "rendered": gb is not None, "video": vp, "action_stem": stem, "n_pair": lim,
            "gbuf_v2": {"btype": "types.json", "bstate": "states.json", "face": list(FACE_NAMES), "fx": FX_DOC, "registry_id": P.reg.d.get("registry_id"),
                        "geom_id": getattr(P.reg, "geom_id", None)} if gb is not None and "bstate" in gb else None,
            "other_visible_frames": int(sum(1 for q in src if (rows[q]["other"] or {}).get("rendered"))) if gb is not None else None,
            "state_metadata": info.get("state_metadata"), "world_type": (info.get("world") or {}).get("world_type")})
        if vp:
            os.makedirs(os.path.join(out, "encode_todo"), exist_ok=True)
            save_json(os.path.join(out, "encode_todo", cid + ".json"), {"id": cid, "episode": P.name, "player": bot, "source": "capture", "video": os.path.abspath(vp),
                                                                       "action_stem": stem, "src_frames": src.tolist(), "n_pair": lim, "valid": not P.invalid})
        ids.append(cid)
    return ids


def make_renderer(kind="gpu"):
    """-> renderer with .render(mesh, cams, entities, H, W, vfov): VoxelRenderer on nvdiffrast ("gpu") or on voxel_proxy's numpy
    rasteriser ("cpu"), imported lazily."""
    from mineengine import voxel_proxy as VP
    return VP.VoxelRenderer() if kind == "gpu" else VP.VoxelRenderer("cpu", raster="cpu")


def gbv2_summary(P, gbs):
    """episode.json gbv2 (docstring "gbuf v2"): palette / rendered counts over the bots' bstate planes."""
    sm = P.mesher(); used = set()
    for g in gbs:
        used |= set(np.unique(g["bstate"]).tolist())
    E = [sm.palette[i] for i in sorted(used - {0})]
    return {"registry_id": P.reg.d.get("registry_id"), "geom_id": getattr(P.reg, "geom_id", None), "types": "types.json", "states": "states.json", "states_palette": len(sm.palette) - 1,
            "types_palette": len({e["type"] for e in sm.palette[1:]}), "states_rendered": len(E), "types_rendered": len({e["type"] for e in E}),
            "groups_rendered": len({e["gid"] for e in E if e["gid"] is not None}), "tgroups_rendered": len({e["tgid"] for e in E if e["tgid"] is not None}),
            "mgroups_rendered": len({e["mgid"] for e in E if e["mgid"] is not None}), "geom_rendered": dict(Counter(e["geom"] for e in E)),
            "unknown_types": sorted({e["type"] for e in sm.palette[1:] if not e["known"]}), "alpha_cropped_elements": sm.n_alpha,
            "rendered_states": [e["state"] for e in E]}


def process(ep, root, out, a, R=None):
    t_start = time.time(); f = ep["files"]
    name = episode_name(ep, root)
    if not f.get("events") or not f.get("snap_start"):
        return {"name": name, "eid": ep["eid"], "inst": ep["inst"], "skipped": "no world log / start snapshot"}
    ed = os.path.join(out, "episodes", name); ej = os.path.join(ed, "episode.json")
    if os.path.exists(ej) and not getattr(a, "force", False):
        old = CR.load_json(ej)
        if os.path.abspath(old.get("base", "")) != os.path.abspath(ep["base"]):
            raise ValueError(f"name collision: {ej} was written for {old.get('base')}, not {ep['base']} (use another --out, or --force)")
        if old.get("rendered") or R is None:
            return dict(old, skipped="done (episode.json exists; --force to redo)")
    P = EpisodeProxy(ep, root, a)
    os.makedirs(ed, exist_ok=True)
    fin, _ = P.bt.final()
    w = {"origin": P.bt.origin, "start": P.bt.ids0, "final": fin}
    rep = {"name": P.name, "eid": ep["eid"], "inst": ep["inst"], "base": os.path.abspath(ep["base"]), "load": P.stats, "block_source": P.bt.source,
           "n_changes": len(P.bt.changes), "n_outside": P.bt.n_outside, "n_nonmono": P.bt.n_nonmono, "n_prev_mismatch": P.bt.n_prev_mismatch,
           "box_mismatch": P.bt.box_mismatch, "unknown_types": dict(P.ix.unknown_types), "kinds": dict(P.ix.counts), "entity_mode": P.et.mode, "flat": P.flat,
           "pad": a.pad, "rendered": R is not None, "palette": {"block": "v0", "block_ext": "cap_palette", "n": CP.N}, "valid": not P.invalid,
           "invalid_reasons": P.invalid, "tick_respawns": P.ix.n_tick_respawn, "bots": {}, "clips": []}
    if P.invalid:
        print(f"[cap2proxy] WARNING {P.name}: episode_info flags {P.invalid} (kept, flagged valid=false)", flush=True)
    if P.end is not None:
        rep["replay"] = P.bt.compare_end(P.end)
        w["end"] = np.asarray([CR.block_id(s) for s in P.end["palette"]], np.uint16)[P.end["grid"]]
    save_npz(os.path.join(ed, "world.npz"), **w)
    bots = [b for b in ep["bots"] if f.get(f"action_{b}")]
    if R is not None:                                                            # type table + state palette before any clip copies them
        for j_, v_ in zip(("types.json", "states.json"), P.palette_json()):
            save_json(os.path.join(ed, j_), v_)
    gbs = []
    for b in bots:
        other = next((o for o in bots if o != b), next((o for o in P.pt.names if o != b and o in ep["bots"]), None))
        rows, cams, gb, res = P.bot(b, other, os.path.join(ed, b), R)
        if gb is not None:
            gbs.append(gb)
        res["other"] = other; res["skin"] = P.info.get(b, {}).get("skin"); res["bot_died"] = P.info.get(b, {}).get("bot_died")
        res["health_min"] = min((r["self"]["health"] for r in rows if r["self"]["health"] is not None), default=None)
        res["deaths"] = len(P.ix.all("death", b))
        if a.clips:
            rep["clips"] += cut_clips(P, b, rows, cams, gb, res, out, a)
        rep["bots"][b] = res
    if gbs:
        rep["gbv2"] = gbv2_summary(P, gbs)
    rep["seconds"] = round(time.time() - t_start, 2)
    save_json(ej, rep)
    return rep


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--root", required=True); ap.add_argument("--out", required=True); ap.add_argument("--episodes", type=int, default=0)
    ap.add_argument("--clips", action="store_true"); ap.add_argument("--no-render", action="store_true"); ap.add_argument("--no-video", action="store_true")
    ap.add_argument("--force", action="store_true", help="redo episodes whose episode.json exists (and overwrite name collisions)")
    ap.add_argument("--vfov", type=float, default=70.0, help="only when no camera meta gives a fov"); ap.add_argument("--pad", type=int, default=48)
    ap.add_argument("--hop", type=float, default=2.5)
    ap.add_argument("--skin-pool", default=None, help="capture/server/skins/pool (skin PNGs by state_metadata.skins file / skin_id) when the batch "
                                                       "has no exported <stem>_skin_<Bot>.png")
    ap.add_argument("--cam-mode", default="hold", choices=("hold", "linear")); ap.add_argument("--hw", default=f"{H}x{W}", help="G-buffer size HxW")
    ap.add_argument("--registry", default=None, help=f"block registry json (python -m mineengine.mc_registry --out); else $MINEENGINE_REGISTRY, else {REG_DEFAULT}")
    ap.add_argument("--hud-proxy", action="store_true", help="also write proxy_hud.mp4 = a HUD-decorated proxy for visualisation")
    ap.add_argument("--cpu-render", action="store_true", help="G-buffer from voxel_proxy's numpy rasteriser instead of nvdiffrast")
    ap.add_argument("--select", default="", help="comma-separated EEEEEE_III (episode_instance) to process, others skipped")
    ap.add_argument("--entity-lag-ms", type=float, default=0.0, help="draw the other player and entities at their logged positions this many ms "
                    "before the action time, where the camera client shows them (it smooths remote entities; measured from name "
                    "tags in real frames: 130-210 ms per view, about 180 overall); 0 = off")
    ap.add_argument("--entity-lag-json", default=None, help='per-view lags {"<episode name>/<bot>": ms}, overriding --entity-lag-ms for those views')
    a = ap.parse_args(argv); a.hw = tuple(int(v) for v in a.hw.lower().split("x"))
    eps = CR.episodes(a.root)
    if a.select:
        want = {tuple(int(v) for v in x.split("_")) for x in a.select.split(",") if x}
        eps = [e for e in eps if (e["eid"], e["inst"]) in want]
    eps = eps[:a.episodes] if a.episodes else eps
    R = None if a.no_render else make_renderer("cpu" if a.cpu_render else "gpu")
    prov = None
    if R is not None:                                                            # fail before the first episode, not inside each
        prov = registry_provenance(load_registry(a.registry))
        print(f"[cap2proxy] registry {prov['path']} id {str(prov['registry_id'])[:12]} geom {str(prov['geom_id'])[:12]} (alpha crops for {prov['alpha_models']} "
              f"models); mc_registry.py md5 {prov['mc_registry_md5']}, voxel_proxy.py md5 {prov['voxel_proxy_md5']}", flush=True)
    os.makedirs(a.out, exist_ok=True)
    reps = []
    for ep in eps:
        try:
            r = process(ep, a.root, a.out, a, R)
        except Exception as e:                                                   # noqa: BLE001  (one bad episode never stops the batch)
            traceback.print_exc()
            r = {"name": episode_name(ep, a.root), "eid": ep["eid"], "inst": ep["inst"], "base": ep["base"], "error": f"{type(e).__name__}: {e}"}
        reps.append(r)
        print(f"[cap2proxy] {r.get('name', ep['eid'])}: {r.get('error') or r.get('skipped') or ''} replay {(r.get('replay') or {}).get('mismatch_states')} changes {r.get('n_changes')} "
              + " | ".join(f"{b}: {v['frames']} frames ({v.get('n_pair')} with a frame, {v.get('n_rejected', 0)} rejected) cam {v['camera_source']} vfov {v['vfov']:g} ({v['vfov_source']}) "
                           f"hp_min {v['health_min']} deaths {v['deaths']}" + (f" other_vis {v.get('other_visible_frames')}" if "other_visible_frames" in v else "")
                           for b, v in (r.get("bots") or {}).items()) + f" clips {len(r.get('clips') or [])} {r.get('seconds')}s"
              + (f" | gbv2 states {r['gbv2']['states_rendered']} / {r['gbv2']['states_palette']}, types {r['gbv2']['types_rendered']}, groups {r['gbv2']['groups_rendered']}, "
                 f"{', '.join(str((v.get('gbv2') or {}).get('render_ms_per_frame')) for v in r['bots'].values())} ms / frame, mesh "
                 + ", ".join(f"{g_['mesh_full']} whole ({g_['mesh_ms']} ms) + {g_['mesh_updates']} updates ({g_['update_ms']} ms)" for g_ in (v.get('gbv2') or {} for v in r['bots'].values()) if g_)
                 if r.get("gbv2") else ""), flush=True)
    counts = dict(Counter("failed" if r.get("error") else "skipped" if r.get("skipped") else "ok" for r in reps))
    rej, skc = Counter(), Counter()
    for r in reps:
        for v in (r.get("bots") or {}).values():
            rej.update(v.get("rejected") or {}); rej["entries"] += v.get("n_rejected", 0)
            sk = v.get("skin")
            if isinstance(sk, dict):
                skc["ok" if sk.get("ok") else "not_ok"] += 1; skc["no_png"] += any(str(q).startswith("no PNG") for q in sk.get("problems") or [])
    gv = [r["gbv2"] for r in reps if r.get("gbv2")]
    g2 = {"episodes": len(gv), "states_rendered": len({s_ for g in gv for s_ in g["rendered_states"]}), "types_rendered": len({s_.split("[", 1)[0] for g in gv for s_ in g["rendered_states"]}),
          "states_rendered_per_episode": [g["states_rendered"] for g in gv], "types_rendered_per_episode": [g["types_rendered"] for g in gv],
          "groups_rendered_per_episode": [g["groups_rendered"] for g in gv]} if gv else None
    save_json(os.path.join(a.out, "summary.json"), {"root": a.root, "episodes": reps, "args": vars(a), "counts": counts, "pair_rejected": dict(rej), "skins": dict(skc),
                                                    "invalid_episodes": [r["name"] for r in reps if r.get("valid") is False], "gbv2": g2, "registry": prov})
    if skc["no_png"]:
        print(f"[cap2proxy] WARNING {skc['no_png']} skin record(s) without a sha256-matching PNG (no exported <stem>_skin_<Bot>.png"
              f"{', --skin-pool ' + str(a.skin_pool) if a.skin_pool else ' and no --skin-pool'}): ok false, entity_refs gets no skin tile for them", flush=True)
    print(f"[cap2proxy] {len(reps)} episode(s) -> {a.out}: {counts}; pair entries rejected {dict(rej)}; skins {dict(skc)}", flush=True)
    return 1 if counts.get("failed") else 0


if __name__ == "__main__":
    sys.exit(main())
