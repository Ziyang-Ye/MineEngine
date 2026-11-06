"""Capture pipeline selftest: synthetic episodes (cap_synth.write_root) -> cap_reader -> cap2proxy, checked against the
synth truth and against OUTPUT_SCHEMA.md itself (field lists, file names, the schema's RLE formula, a block_state replay written
from the schema text - not through cap_reader, so a format assumption shared by the generator and the reader still fails).
  root/batch_0   one episode per kind (build, break, pvp, pve, tnt): canonical record names, T16 in the tick records, T13
                 camera meta (70 deg), aligned placeholder videos that end 2..4 action entries early
  root/batch_1   the persistent chain cap_synth.CHAIN: build (alt T12 / T15 / T17 names: camera players in the tick list,
                 entity_damage_by_entity with actor() fields, ...; T16 per frame), break (alt names, relative renderTime), tnt
                 (camera_pose at eye level); camera meta without fovEffectScale (flying: 77 deg)
  root/batch_2   the capture engine's own layout (cap_synth engine "real", REAL kinds build / break / pvp / pve with the REAL_CASES
                 switches: exported skin PNGs + the follower's pool-differed note / aligned meta without camera_params + a
                 restarted camera whose meta was never finalized / a skin the camera never loaded / an episode_info error flag +
                 a skin the server never applied): tick "cameras" one tick behind the rendered pose, pose_seq (controller
                 hiccups) + nested T16 state (cursor = the 5 m cast), sidecar frame_mapping (late start, a stall, latency
                 spikes -> late "next" re-pairs, dropped slots -> early "prev" re-pairs, server-pose and loading-screen
                 frames), state_metadata.skins + the pool root/skin_pool/pool (cap2proxy --skin-pool), finalized mod camera
                 meta (options.fov_deg, local_skins, capture_stats), entities records
Checks: (f) schema conformance of every file (incl. relative renderTime, T16 placement, aligned meta; batch_2: the capture-engine
record / field names, check_schema_real); (a) start snapshot +
block_state replay == end snapshot == synth truth + checkpoints + world.npz; (b) persistent chain end(k) == start(k+1); (c)
state.jsonl health == truth; (d) camera == truth (< 1e-3 rad / m, the sneaking camera's 1.62 x 0.785 eye included) and vfov ==
truth; (e) one state line per action entry at the host-clock time, pvp hp / deaths, tnt crater, pve mobs, no unknown record
types; (k) T16 crosshair / T17 dig / T15 using / hotbar == truth, the other player's T16 from its action entries; (n) n_pair =
frames with video, other NaN while dead; (g) deterministic per seed; (i) clips inside the paired frames: meta / cams / state,
exact whitebox.npz, encode_todo, the clip quality filter; (u) pad / pid free list / vfov rules /
colour margins / stale mobs; (j) a corrupt episode does not stop the batch, reruns skip done episodes, name collisions are
refused; batch_2 also: (d) the camera per frame = frame_mapping on usable pairs, else the action pose (cam_source == truth;
"d camera controls": tick cameras / the action pose would miss the truth), (n) paired / usable rows == truth (start /
delta / screen rejections counted; "n pose pairs": pose-exact re-pairs outside the time window kept with the frame camera),
(k cursor reach) logged cursor blocks > 4.5 m give no target, (s) skin record == truth (sha1 copied to output/skins,
applied_epoch_ms from the camera's local_skins, ok per bot - not skins.js's ok -, camera_check None for the unfinalized meta,
ok false for the skin the camera never loaded / the server never applied) and entity_refs gives skin tiles only for ok skins, (v)
episode_info flags -> valid false, (e) respawns from tick dead, mob body_yaw from the entities record; units (u pairing incl.
the pose-exact window / crosshair reach / skin rules: per-bot assignment, start-time vs finalized camera meta, read-only batch
/ using / vfov: options.fov_deg, Windows camera_meta_path of a restarted camera in instance 2). --render (GPU VoxelRenderer) / --cpu-render
(voxel_proxy's numpy rasteriser, same channel code, at 48 x 84; batch_0 build / break / pvp / tnt + batch_2 pvp rendered): G-buffer
(block = v0 ids, block_ext = cap ids, btype / bstate / fx uint16, face uint8), the other player rendered (pid 1) when in front within
20 m with a clear line of sight, streamed proxy / proxy_hud / side-by-side videos (side-by-side = the paired frames, real half
only on usable ones). G-buffer v2 (gb, rendering only; registry: --registry, else cap2proxy's default): fixed cameras on the
batch_0 build episode's garden (cap_synth GARDEN) through cap2proxy's own path (states_at -> StateMesher -> renderer) before /
after its state flip: (a) door closed vs open = different geometry and door states, (b) bottom vs top slab on disjoint image rows,
(c) wheat age 0 -> 7 changes bstate, drawn both times (block_id air, legacy id non-air), (d) snow layers 1 -> 6 = 3x+ the side
height, (e) water level 0 -> 4 = about half the side height, (f) torch / poppy drawn (block_id air, legacy id non-air), (g)
furnace lit: bstate only; on the rendered episodes (h) btype == the state palette's type for every block pixel, types.json ==
[""] + sorted registry types (round trip), clips carry the planes + tables, (i) proxy.mp4 == the white-box colorize of the
G-buffer (no HUD / hand / crosshair / HP bars) while proxy_hud.mp4 still has the HUD; (gb rebuild) every state version meshed
(whole or StateMesher.update), a state-only change (same cap ids) seen; (gb mesh update) the incremental mesh == a whole remesh
at every state version of the build episode; (gb legacy ids) no hit block pixel has block_ext 0; (gb fx dig) fx = dig stage + 1
on the dug cell in the break episode (batch_0/2, also CPU-rendered), 0 elsewhere, cracks in proxy.mp4; (gb waterlogged) a
waterlogged fence keeps more pixels than its implied water, a waterlogged slab no fewer than a dry one; --render only: (gb gpu
parity) nvdiffrast vs the numpy rasteriser per channel on garden views and batch_0/3 frames + GPU ms / frame.
  python -m mineengine.cap_selftest --work DIR [--no-render (default) | --render | --cpu-render] [--seconds 20] [--registry REG.json]
"""
import argparse
import glob
import gzip
import hashlib
import json
import math
import os
import re
import shutil
import sys
import time
import types

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
from mineengine import cap2proxy as C2P  # noqa: E402
from mineengine import cap_reader as CR  # noqa: E402
from mineengine import cap_synth as CS  # noqa: E402

TAU = 2 * math.pi
ROWS = []


def row(check, scope, ok, detail=""):
    ROWS.append((check, scope, "PASS" if ok else "FAIL", str(detail)))
    print(f"{'PASS' if ok else 'FAIL'} {check} [{scope}] {detail}", flush=True)
    return ok


def table():
    w = [max(len(r[i]) for r in ROWS + [("check", "scope", "result", "detail")]) for i in range(3)]
    lines = [f"{'check':<{w[0]}}  {'scope':<{w[1]}}  {'result':<{w[2]}}  detail", "-" * (sum(w) + 6 + 40)]
    lines += [f"{c:<{w[0]}}  {s:<{w[1]}}  {r:<{w[2]}}  {d[:150]}" for c, s, r, d in ROWS]
    return "\n".join(lines)


# ---- schema conformance (OUTPUT_SCHEMA.md, 07, 08; independent of cap_reader) ------------------------------------------
NAME = {"action": r"^\d{8}_\d{6}_(\d{6})_(Alpha|Bravo)_instance_(\d{3})\.json$", "meta": r"^\d{8}_\d{6}_(\d{6})_(Alpha|Bravo)_instance_(\d{3})_meta\.json$",
        "info": r"^\d{8}_\d{6}_(\d{6})_(Alpha|Bravo)_instance_(\d{3})_episode_info\.json$",
        "events": r"^\d{8}_\d{6}_(\d{6})_world_instance_(\d{3})_world_events\.jsonl$",
        "start": r"^\d{8}_\d{6}_(\d{6})_world_instance_(\d{3})_world_snapshot_start\.json\.gz$",
        "end": r"^\d{8}_\d{6}_(\d{6})_world_instance_(\d{3})_world_snapshot_end\.json\.gz$"}
COMMON = ("type", "seq", "game_time", "server_tick", "epoch_ms")
BLOCK_EV = ("pos", "old_type", "old_state", "new_type", "new_state")
ENTITY = ("uuid", "type", "pos", "yaw", "pitch")
REQ = {"episode_begin": ("format", "stem", "episode_id", "instance_id", "episode_type", "events_file", "world", "box", "center", "log_radius", "heartbeat_ticks",
                         "tracked_players", "snapshot", "server_version", "bukkit_version", "plugin_version"),
       "tick": ("full_time", "time_of_day", "players"), "note": ("text",), "episode_end": ("stem", "episode_id", "reason", "events_logged", "snapshot_end_file",
                                                                                            "restored", "restored_blocks", "removed_entities"),
       "block_state": ("pos", "block", "state", "prev_state"), "block_place": BLOCK_EV + ("player", "item", "against", "multi"),
       "block_break": BLOCK_EV + ("player", "tool", "drop_items"), "block_fade": BLOCK_EV, "tnt_prime": BLOCK_EV + ("cause", "source"),
       "entity_explode": ("center", "yield", "blocks", "entity_type", "entity_uuid"), "entity_spawn": ("entity", "reason"), "entity_death": ("entity", "cause", "killer"),
       "entity_remove": ("entity",), "player_interact": ("player", "action", "pos", "state", "face", "item", "cancelled"),
       "player_animation": ("player", "animation"), "entity_damage": ("entity", "damager", "damage", "cause", "knockback", "health_after"),
       "item_held": ("player", "old_slot", "new_slot", "item"), "camera_pose": ("pos", "yaw", "pitch"), "player_respawn": ("player", "pos")}
WORLD = ("name", "uid", "seed", "seed_str", "environment", "world_type", "generate_structures", "min_height", "max_height", "difficulty", "storm", "thundering",
         "full_time", "time_of_day", "spawn", "gamerules")
TICK_PLAYER = ("name", "world", "pos", "yaw", "pitch", "sneaking", "sprinting", "on_ground", "health", "fire_ticks", "held_slot", "held_item", "held_count")
T16 = ("food", "saturation", "body_yaw", "head_yaw", "crosshair")                # T16: per frame JSON or world log (either)
SNAP = ("format", "which", "stem", "episode_id", "world", "seed", "game_time", "full_time", "server_tick", "epoch_ms", "origin", "size", "order", "palette", "rle",
        "biome_y", "biome_palette", "biomes_rle", "entities")
ACTION = ("x", "y", "z", "yaw", "pitch", "yaw_mc_deg", "pitch_mc_deg", "action", "inventory", "renderTime", "physicsTime", "worldAgeTicks", "isInWater", "frame_count",
          "episode_id", "relativeTimeMs", "epochTime", "extra_info")
KEYS = ("forward", "back", "left", "right", "jump", "sprint", "sneak", "camera", "attack", "use", "mount", "dismount", "place_block", "place_entity", "mine") + tuple(
    f"hotbar.{k}" for k in range(1, 10))
INV = ("quickBarSlot", "hotbar", "mainInventory", "heldItem", "offHandItem")
META = ("episode_start_epoch", "first_render_time_ms", "render_time_is_epoch", "frames", "video_fps", "episode_id")
INFO = ("timestamp", "bot_name", "world_type", "episode_number", "episode_type", "instance_id", "encountered_error", "peer_encountered_error", "bot_died",
        "episode_recording_started", "eval_metadata", "world", "world_state_leader", "world_state", "world_state_end", "reset_world_between_episodes", "state_metadata",
        "angle_convention", "skin")
INFO_WORLD = ("seed", "seed_config", "seed_value_expected", "world_type", "level_type", "generator_settings", "flat_biome", "flat_surface_block", "generate_structures",
              "server_version", "server_type")
# the capture engine's own output (WorldStateLogger.java, frame-recorder.js, skins.js, episodes-loop.js, CaptureController.java / LocalSkins.java, process_recordings.py)
REAL = ("build", "break", "pvp", "pve")                                         # root/batch_2 kinds (cap_synth.REAL_CASES switches)
REAL_TICK_PLAYER = TICK_PLAYER + ("head_yaw", "body_yaw", "food", "saturation", "dead", "gamemode", "flying", "blocking", "hand_raised")
REAL_CAMERA = ("name", "target", "world", "pos", "yaw", "pitch", "flying", "gamemode")
REAL_ENTITY = ("uuid", "id", "kind", "type", "name", "x", "y", "z", "yaw", "pitch", "vx", "vy", "vz", "bbox_w", "bbox_h", "health", "max_health", "alive", "on_ground",
               "sneaking", "sprinting", "blocking", "hand_raised", "fire_ticks")
REAL_STATE = ("health", "food", "saturation", "xp_level", "xp_progress", "oxygen", "effects", "hotbar", "selected_slot", "offhand", "armor", "on_ground", "sneaking",
              "sprinting", "flying", "in_water", "alive", "head_yaw", "body_yaw", "cursor", "cursor_at_action")
REAL_PEER = ("name", "known", "pos", "yaw", "pitch", "head_yaw", "distance")
REAL_SKIN = ("skin_id", "model", "source", "sha256", "texture_hash", "seed", "file", "reference", "tags")
REAL_FM = ("action_index", "renderTime_ms", "action_time_sec", "relative_time_ms", "frame_index", "frame_time_sec", "frame_pts_sec", "delta_sec", "cam_yaw", "cam_pitch",
           "cam_pos", "fov", "sidecar_epoch_ms", "cam_game_time", "cam_screen", "pose_source", "pose_seq", "pose_fallback", "action_pose_seq")
REAL_LOCAL_SKIN = ("skin_id", "model", "status", "file", "sha256_expected", "sha256_file", "sha256_match", "requests", "loaded_epoch_ms")
SCHEMA_ONLY = {"camera_pose", "player_respawn", "item_held", "player_animation", "block_damage", "block_break_progress", "item_use_start",
               "item_use_end", "entity_damage_by_entity", "player_item_held", "player_item_consume"}


def schema_snapshot(path):
    """the schema's decoding snippet (section 6.1) -> (dense yzx indices, palette, header)."""
    s = json.load(gzip.open(path, "rt"))
    sx, sy, sz = s["size"]; dense = []
    for run, idx in zip(s["rle"][0::2], s["rle"][1::2]):
        dense += [idx] * run
    assert len(dense) == sx * sy * sz
    return dense, s["palette"], s


def schema_replay(start_path, events):
    """verify_world_events semantics from the schema text: block_state records inside the box onto the start snapshot ->
    {flat index: state string} overrides + the start (dense, palette, header)."""
    dense, pal, s = schema_snapshot(start_path)
    (ox, oy, oz), (sx, sy, sz) = s["origin"], s["size"]; ov = {}
    for e in events:
        if e["type"] == "block_state":
            x, y, z = e["pos"]
            if ox <= x < ox + sx and oy <= y < oy + sy and oz <= z < oz + sz:
                ov[((y - oy) * sz + (z - oz)) * sx + (x - ox)] = e["state"]
    return ov, dense, pal, s


def check_schema(ep, scope):
    """-> list of problems (empty = conforms)."""
    bad, f = [], ep["files"]
    for kind, key in (("events", "events"), ("start", "snap_start"), ("end", "snap_end")):
        if not f.get(key) or not re.match(NAME[kind], os.path.basename(f[key])):
            bad.append(f"name {kind}: {f.get(key)}")
    ev = [json.loads(line) for line in open(f["events"], encoding="utf-8")]
    if ev[0]["type"] != "episode_begin" or ev[-1]["type"] != "episode_end":
        bad.append("first / last record")
    if [e["seq"] for e in ev] != list(range(len(ev))):
        bad.append("seq not 0.. strictly increasing")
    miss, t16_tick = {}, True
    for e in ev:
        for k in COMMON + REQ.get(e["type"], ()):
            if k not in e:
                miss[f"{e['type']}.{k}"] = miss.get(f"{e['type']}.{k}", 0) + 1
        if not all(isinstance(e[k], int) for k in ("seq", "game_time", "server_tick", "epoch_ms")):
            miss["int clocks"] = 1
        if e["type"] == "tick":
            t16_tick = t16_tick and all(all(k in p for k in T16) for p in e["players"] if p["name"] in ("Alpha", "Bravo"))
            for p in e["players"]:
                for k in TICK_PLAYER:
                    if k not in p:
                        miss[f"tick.players.{k}"] = 1
                if not (-180.0 <= p["yaw"] < 180.0 and -90.0 <= p["pitch"] <= 90.0):
                    miss["tick yaw / pitch degree range"] = 1
            for m in e.get("mobs", []):
                for k in ("uuid", "type", "pos", "yaw", "pitch", "body_yaw", "bbox", "health", "max_health", "target_uuid"):
                    if k not in m:
                        miss[f"tick.mobs.{k}"] = 1
        for k in ("entity", "damager"):
            if isinstance(e.get(k), dict):
                for q in ENTITY:
                    if q not in e[k]:
                        miss[f"{e['type']}.{k}.{q}"] = 1
        if e["type"] == "entity_explode":
            for b in e["blocks"]:
                if not all(q in b for q in ("pos", "old_type", "old_state")):
                    miss["entity_explode.blocks"] = 1
        if e["type"] in ("block_state", "block_place", "block_break") and not all(isinstance(v, int) for v in e["pos"]):
            miss["integer block pos"] = 1
    b0 = ev[0]
    miss.update({f"episode_begin.world.{k}": 1 for k in WORLD if k not in b0["world"]})
    if not isinstance(b0["world"]["seed"], int):
        miss["episode_begin.world.seed number"] = 1
    bad += [f"missing {k} x{v}" for k, v in miss.items()]
    tk = [e for e in ev if e["type"] == "tick"]
    dt = np.diff([e["epoch_ms"] for e in tk]); dg = np.diff([e["game_time"] for e in tk])
    if not (45 <= np.median(dt) <= 55 and (dg[:-1] == 1).all()):
        bad.append(f"tick cadence median {np.median(dt)} ms, game_time steps {set(dg.tolist())}")
    for key in ("snap_start", "snap_end"):
        dense, pal, s = schema_snapshot(f[key])
        bad += [f"{key} missing {k}" for k in SNAP if k not in s]
        if s["order"] != "yzx" or s["format"] != "solaris-block-snapshot-v1" or s["which"] != key[5:] or sum(s["biomes_rle"][0::2]) != s["size"][0] * s["size"][2]:
            bad.append(f"{key} header")
        if list(s["origin"]) != b0["box"]["min"] or [o + n - 1 for o, n in zip(s["origin"], s["size"])] != b0["box"]["max"]:
            bad.append(f"{key} box != episode_begin box")
    ov, dense, pal, s = schema_replay(f["snap_start"], ev)
    ed, ep_, _ = schema_snapshot(f["snap_end"])
    mism = sum(1 for i in range(len(dense)) if ov.get(i, pal[dense[i]]) != ep_[ed[i]])
    if mism:
        bad.append(f"schema replay != end snapshot: {mism} voxels")
    stem_w = os.path.basename(f["events"])[:-len("_world_events.jsonl")]
    if b0["stem"] != stem_w or b0["events_file"] != os.path.basename(f["events"]):
        bad.append("episode_begin stem / events_file")
    for b in ("Alpha", "Bravo"):
        for kind, key in (("action", "action"), ("meta", "meta"), ("info", "info")):
            if not f.get(f"{key}_{b}") or not re.match(NAME[kind], os.path.basename(f[f"{key}_{b}"])):
                bad.append(f"name {kind} {b}")
        A = json.load(open(f[f"action_{b}"])); M = json.load(open(f[f"meta_{b}"])); I = json.load(open(f[f"info_{b}"]))
        am = {k for a in A for k in ACTION if k not in a} | {f"action.{k}" for a in A for k in KEYS if k not in a["action"]} | {
            f"inventory.{k}" for a in A for k in INV if k not in a["inventory"]}
        bad += [f"{b} action missing {k}" for k in sorted(am)]
        if not t16_tick and not all(all(k in a for k in T16) for a in A):
            bad.append(f"{b} T16 fields neither in every tick record nor in every action entry")
        rt = np.array([a["renderTime"] for a in A]); et = np.array([a["epochTime"] * 1000.0 for a in A])
        if M.get("render_time_is_epoch"):                                       # act_recorder: renderTime epoch ms, epochTime = renderTime / 1000
            clk = rt[0] >= 1e12 and np.abs(et - rt).max() < 1e-3
        else:                                                                   # relative renderTime; epochTime from the recorder's clock
            clk = rt[0] < 1e12 and np.abs(et - (M["episode_start_epoch"] * 1000.0 + (rt - rt[0]))).max() < 1e-2
        if not (np.all(np.diff(rt) > 0) and np.all(np.diff(et) > 0) and clk and [a["frame_count"] for a in A] == list(range(len(A)))
                and all(abs(a["relativeTimeMs"] - (a["renderTime"] - rt[0])) < 1e-6 for a in A)):
            bad.append(f"{b} action clocks / frame_count (render_time_is_epoch {M.get('render_time_is_epoch')})")
        if np.all((rt - rt[0]) % 50 == 0):
            bad.append(f"{b} renderTime tick-locked")
        if f.get(f"video_meta_{b}"):                                            # process_recordings: matched = the first n_matched entries
            V = json.load(open(f[f"video_meta_{b}"])); fm = V["frame_mapping"]; dg = V.get("diagnostics", {})
            if [x["action_index"] for x in fm] != list(range(len(fm))) or dg.get("n_matched") != len(fm) or len(fm) + dg.get("unmatched_actions_end", -1) != len(A):
                bad.append(f"{b} aligned meta frame_mapping / diagnostics")
        for a in A:
            if a["yaw"] is None:
                if a["pitch"] is not None or a["yaw_mc_deg"] is not None:
                    bad.append(f"{b} null yaw entry"); break
                continue
            ydeg = ((math.degrees(math.pi - a["yaw"]) + 180) % 360) - 180          # the recorder's _mc_yaw_deg
            if not (0 <= a["yaw"] < TAU and -math.pi / 2 <= a["pitch"] <= math.pi / 2 and abs(ydeg - a["yaw_mc_deg"]) < 2e-3
                    and abs(-math.degrees(a["pitch"]) - a["pitch_mc_deg"]) < 2e-3 and round(a["yaw"], 5) == a["yaw"]):
                bad.append(f"{b} yaw / pitch convention at frame {a['frame_count']}"); break
            if a["inventory"]["heldItem"] is not None and a["inventory"]["heldItem"].get("slot") != 36 + a["inventory"]["quickBarSlot"]:
                bad.append(f"{b} heldItem slot"); break
        prev = None
        for a in A:                                                             # action.camera = change since the previous sample
            if a["yaw"] is not None and prev is not None:
                d = ((a["yaw"] - prev["yaw"] + math.pi) % TAU) - math.pi
                if abs(d - a["action"]["camera"][0]) > 2e-5 or abs((a["pitch"] - prev["pitch"]) - a["action"]["camera"][1]) > 2e-5:
                    bad.append(f"{b} action.camera != delta at frame {a['frame_count']}"); break
            prev = a if a["yaw"] is not None else prev
        bad += [f"{b} meta missing {k}" for k in META if k not in M]
        if M.get("frames") != len(A) or M.get("first_render_time_ms") != rt[0]:
            bad.append(f"{b} meta frames / first_render_time")
        bad += [f"{b} info missing {k}" for k in INFO if k not in I] + [f"{b} info.world missing {k}" for k in INFO_WORLD if k not in I.get("world", {})]
        if not isinstance(I["world"]["seed"], str) or str(b0["world"]["seed"]) != I["world"]["seed"] or I["world_state"]["stem"] != stem_w:
            bad.append(f"{b} info seed string / world_state stem")
        sk = I.get("skin") or {}
        png = os.path.join(os.path.dirname(f[f"info_{b}"]), "skins", f"{sk.get('sha1')}.png")
        if not all(k in sk for k in ("file", "sha1", "model", "slim", "applied_epoch_ms")) or not os.path.exists(png) or hashlib.sha1(open(png, "rb").read()).hexdigest() != sk["sha1"]:
            bad.append(f"{b} skin record / png sha1")
        if I["episode_number"] != ep["eid"] or I["instance_id"] != ep["inst"]:
            bad.append(f"{b} info episode / instance")
    return bad


def check_schema_real(ep):
    """the capture engine's own output (engine real): the record / field names its writers use (cap_synth must keep them) -> problems."""
    bad, f, miss = [], ep["files"], set()
    ev = [json.loads(line) for line in open(f["events"], encoding="utf-8")]
    if ev[0]["type"] != "episode_begin" or ev[-1]["type"] != "episode_end" or [e["seq"] for e in ev] != list(range(len(ev))):
        bad.append("first / last record or seq")
    extra = {e["type"] for e in ev} & SCHEMA_ONLY
    if extra:
        bad.append(f"schema-only record types {sorted(extra)}")
    for e in ev:
        miss |= {f"{e['type']}.{k}" for k in COMMON if k not in e}
        if e["type"] == "tick":
            miss |= {f"tick.players.{k}" for p in e["players"] for k in REAL_TICK_PLAYER if k not in p} | {f"tick.players.{k} (not logged)" for p in e["players"]
                                                                                                          for k in ("crosshair", "hotbar") if k in p}
            miss |= {f"tick.cameras.{k}" for c in e.get("cameras", [None]) for k in REAL_CAMERA if not isinstance(c, dict) or k not in c} | ({"tick.mobs"} if "mobs" in e else set())
        elif e["type"] == "entities":
            miss |= {f"entities.{k}" for x in e["entities"] for k in REAL_ENTITY if k not in x} | {"entities.body_yaw"
                                                                                                   for x in e["entities"] if "head_yaw" not in x or "body_yaw" not in x}
        elif e["type"] == "entity_damage":
            miss |= {f"entity_damage.{k}" for k in ("victim", "attacker", "cause", "damage", "final_damage", "health_after") if k not in e}
        elif e["type"] in ("use_item_start", "use_item_stop"):
            miss |= {f"{e['type']}.{k}" for k in ("player", "item", "hand") + (("reason", "ticks_used") if e["type"] == "use_item_stop" else ()) if k not in e}
    ov, dense, pal, _ = schema_replay(f["snap_start"], ev)
    ed, ep_, _ = schema_snapshot(f["snap_end"])
    if sum(1 for i in range(len(dense)) if ov.get(i, pal[dense[i]]) != ep_[ed[i]]):
        bad.append("schema replay != end snapshot")
    for b in ("Alpha", "Bravo"):
        A = json.load(open(f[f"action_{b}"])); I = json.load(open(f[f"info_{b}"]))
        miss |= {f"{b} action.{k}" for a in A for k in ACTION + ("pose_seq", "state", "peer") if k not in a} | {f"{b} action top-level {k}" for a in A for k in T16 if k in a}
        miss |= {f"{b} state.{k}" for a in A for k in REAL_STATE if k not in a["state"]} | {f"{b} peer.{k}" for a in A for k in REAL_PEER if k not in a["peer"]}
        if any(a["state"]["body_yaw"] is not None or set(a["state"]["cursor"]) != {"block", "entity"} for a in A):
            bad.append(f"{b} state body_yaw not null / cursor not {{block, entity}}")
        miss |= {f"{b} info.{k}" for k in INFO if k != "skin" and k not in I} | ({f"{b} info.skin (not written)"} if "skin" in I else set())
        sm = I["state_metadata"]; sk = sm.get("skins") or {}
        miss |= {f"{b} skins.{p}.{k}" for p in ("Alpha", "Bravo") for k in REAL_SKIN + ("texture_mode",) if k not in sk.get(p, {})}
        if not isinstance(I["angle_convention"], dict) or b not in (sm.get("skin_assignment") or {}).get("applied", {}):
            bad.append(f"{b} angle_convention / skin_assignment.applied")
        if f.get(f"video_meta_{b}"):
            V = json.load(open(f[f"video_meta_{b}"])); fm, dg = V["frame_mapping"], V["diagnostics"]
            if [x["action_index"] for x in fm] != list(range(len(fm))) or dg["n_matched"] != len(fm) or len(fm) + dg["unmatched_actions_end"] != len(A) \
                    or not dg.get("sidecar_used") or "\\" not in V["camera_meta_path"] or ("camera_params" in V and "fov_deg" not in V["camera_params"]):
                bad.append(f"{b} aligned meta frame_mapping / diagnostics / Windows camera_meta_path / camera_params")
            miss |= {f"{b} frame_mapping.{k}" for x in fm for k in REAL_FM if k not in x}
        cm = json.load(open(os.path.join(ep["base"], "camera", f"output_{b.lower()}", str(ep["inst"]), f"camera_{b.lower()}_meta.json")))
        if not {"fov_deg", "fov_effect_scale"} <= set(cm["options"]) or "fov" in cm or not cm["local_skins"]["skins"]:
            bad.append(f"{b} camera meta options.fov_deg / local_skins")
        miss |= {f"{b} local_skins.{k}" for e in cm["local_skins"]["skins"].values() for k in REAL_LOCAL_SKIN if k not in e}
    return bad + [f"missing {k}" for k in sorted(miss)]


# ---- unit checks of cap2proxy pieces -------------------------------------------------------------------------------------
def colour_margins():
    """min RGB distance of the player (TEAM_RGB2) and mob colours (entity shade x 0.64..1) to every block colour (cap_palette +
    the white-box proxy's decoration colours, cap2proxy.state_rgb of DECO_RGB) under the proxy's shading (x 0.51..1, edges x 0.6)."""
    from mineengine import mc_proxy_style as PS
    deco = [C2P.CP._separate(c) for _, c in C2P.DECO_RGB]
    B = np.concatenate([C2P.CP.BLOCK_RGB[1:], np.asarray(deco, np.uint8)]).astype(np.float64); fb = np.linspace(0.5125, 1.0, 120); fe = np.linspace(0.6425, 1.0, 60)
    blk = np.concatenate([(B[:, None] * fb[None, :, None]).reshape(-1, 3), (B[:, None] * fb[None, :, None] * 0.6).reshape(-1, 3)])
    cols = {f"team {k}": v for k, v in PS.TEAM_RGB2.items()}; cols.update(hostile=C2P.MOB_HOSTILE, passive=C2P.MOB_PASSIVE)
    return {k: float(np.sqrt((((np.asarray(v, float)[None] * fe[:, None])[:, None] - blk[None]) ** 2).sum(-1)).min()) for k, v in cols.items()}


def unit_checks(work):
    CP = C2P.CP
    # pad: a normal world with trees on the border -> ground continuation, nothing above the ground in the pad
    ids = np.zeros((20, 30, 20), np.uint16); ids[:, :10] = CP.ID["stone"]; ids[:, 10] = CP.ID["grass_block"]
    for x, z in ((0, 5), (19, 7), (4, 0), (9, 19)):
        ids[x, 11:16, z] = CP.ID["oak_log"]
    g, o, mode = C2P.pad_grid(ids, np.array([0, 60, 0]), 12, flat=False)
    pad = np.ones(g.shape[::2], bool); pad[12:32, 12:32] = False
    ok_pad = not CP.COLLIDES[g[:, 11:, :][np.broadcast_to(pad[:, None, :], g[:, 11:, :].shape)]].any() and (g[:, 10, :][pad] != 0).all() and "ground" in mode
    gf, _, mf = C2P.pad_grid(ids, np.array([0, 60, 0]), 12, flat=True)
    row("u pad", "normal / flat", ok_pad and mf == "border_column" and np.array_equal(g[12:32, :, 12:32], ids),
        f"normal: {mode}, no cells above the ground in the pad (edge mode put {int((np.pad(ids, ((12, 12), (0, 0), (12, 12)), mode='edge') == CP.ID['oak_log']).sum())} log cells there)")
    # pid free list: kept while alive, freed, reused least recently freed first, overflow counted
    P = C2P.Pids(2, 4); a1 = P.frame(["a", "b", "c"]); a2 = P.frame(["b", "c", "d"]); a3 = P.frame(["c", "d", "e", "f"])
    row("u pids", "free list", a1 == {"a": 2, "b": 3, "c": 4} and a2 == {"b": 3, "c": 4, "d": 2} and a3["c"] == 4 and a3["d"] == 2 and a3["e"] == 3 and a3["f"] is None
        and P.overflow == 1, f"{a1} {a2} {a3}")
    # vfov: T13 meta, flying meta without fovEffectScale, options.txt scale, no meta, the capture mod's options.fov_deg / fov_effect_scale
    d = os.path.join(work, "vfov"); shutil.rmtree(d, ignore_errors=True)
    res = []
    for k, m in enumerate(({"fov": 0.0, "fovEffectScale": 0.0}, {"fov": 70}, {"fov": 0.25, "fovEffectScale": 0.5}, None, {"options": {"fov_deg": 70, "fov_effect_scale": 0.0}},
                           {"options": {"fov_deg": 90, "fov_effect_scale": 1.0}})):
        base = os.path.join(d, str(k)); os.makedirs(os.path.join(base, "camera", "output_alpha", "0"))
        if m is not None:
            json.dump(m, open(os.path.join(base, "camera", "output_alpha", "0", "camera_alpha_meta.json"), "w"))
        res.append(C2P.vfov_of({"base": base, "files": {}}, "Alpha", 70.0)[:2])
    base = os.path.join(d, "win"); os.makedirs(os.path.join(base, "camera", "output_alpha", "2")); os.makedirs(os.path.join(base, "aligned"))
    am = os.path.join(base, "aligned", "x_000001_Alpha_instance_002_camera_meta.json")       # run_windows: absolute Windows path, restarted camera, instance 2
    json.dump({"camera_meta_path": "C:\\cap\\batch_0\\camera\\output_alpha\\2\\camera_alpha_r1_meta.json", "frame_mapping": []}, open(am, "w"))
    json.dump({"options": {"fov_deg": 80, "fov_effect_scale": 0.0}}, open(os.path.join(base, "camera", "output_alpha", "2", "camera_alpha_r1_meta.json"), "w"))
    vw = C2P.vfov_of({"base": base, "inst": 2, "files": {"video_meta_Alpha": am}}, "Alpha", 70.0)
    row("u vfov", "meta rules", [(round(v, 6), s_) for v, s_ in res] == [(70.0, "camera_meta"), (77.0, "camera_meta_flying"), (84.0, "camera_meta_flying"), (70.0, "default"),
                                                                         (70.0, "camera_meta"), (99.0, "camera_meta_flying")]
        and vw[:2] == (80.0, "camera_meta") and vw[2].endswith("camera_alpha_r1_meta.json"),
        f"T13 / no fovEffectScale (flying x1.1) / scale 0.25 + fes 0.5 / none / options.fov_deg 70 fes 0 / fov_deg 90 fes 1 -> {res}; Windows camera_meta_path, "
        f"instance 2, r1 -> {vw}")
    # pairing rules: video without its aligned meta, unmatched_actions_start, delta window, loading screen / no world render
    pb = os.path.join(d, "pair"); os.makedirs(os.path.join(pb, "aligned"))
    open(os.path.join(pb, "aligned", "v.mp4"), "wb").close()
    n0, i0 = C2P.paired_frames({"base": pb, "files": {"video_Alpha": os.path.join(pb, "aligned", "v.mp4")}}, "Alpha", 10)
    fmp = [dict(action_index=q, delta_sec=dl, cam_screen=sc, fov=fv, **({"pose_pair": pp} if pp else {})) for q, (dl, sc, fv, pp) in enumerate(
        [(0.2, None, 70.0, None), (0.03, None, 70.0, None), (0.01, None, 70.0, None), (-0.049, None, 70.0, None), (0.074, None, 70.0, None), (0.09, None, 70.0, None),
         (-0.06, None, 70.0, None), (0.0, "ChatScreen", 70.0, None), (0.0, None, None, None),
         (0.09, None, 70.0, "next"), (-0.07, None, 70.0, "prev"), (0.09, None, 70.0, "none"), (0.12, None, 70.0, "next")])]   # pose-exact: [-2.25, 2.25] frames
    mpp = os.path.join(pb, "aligned", "m.json"); json.dump({"fps": 20.0, "frame_mapping": fmp, "diagnostics": {"n_matched": 13, "unmatched_actions_start": 2, "unmatched_actions_end": 1}}, open(mpp, "w"))
    n1, i1 = C2P.paired_frames({"base": pb, "files": {"video_Alpha": os.path.join(pb, "aligned", "v.mp4"), "video_meta_Alpha": mpp}}, "Alpha", 14)
    row("u pairing", "rules", n0 == 0 and n1 == 13 and i1["ok"].tolist() == [False, False, True, True, True, False, False, True, True, True, True, False, False, False]
        and i1["clean"].tolist() == [True] * 7 + [False, False] + [True] * 5 and i1["n_rejected"] == 8
        and i1["rejected"] == {"before_recording": 2, "delta": 4, "screen": 1, "no_world_render": 1} and C2P.runs(i1["ok"] & i1["clean"]) == [(2, 5), (9, 11)],
        f"video without meta -> n_pair {n0}; ok {i1['ok'].astype(int).tolist()} clean {i1['clean'].astype(int).tolist()} rejected {i1['rejected']} "
        f"(next +90 ms / prev -70 ms pose-exact kept, none +90 ms / next +120 ms rejected)")
    xs = [C2P.log_crosshair(v) for v in ({"block": {"pos": [1, 60, 2], "distance": 4.8}, "entity": None}, {"block": {"pos": [1, 60, 2], "distance": 4.4}, "entity": None},
                                         {"block": {"pos": [1, 60, 2], "distance": 4.8}, "entity": {"uuid": "u1", "distance": 4.6}}, {"block": None, "entity": None},
                                         {"type": "block", "pos": [3, 61, 4]})]
    row("u crosshair reach", "log_crosshair", xs == [("none", None), ("block", [1, 60, 2]), ("entity", "u1"), ("none", None), ("block", [3, 61, 4])],
        f"cursor block 4.8 m / 4.4 m / entity first / empty / schema field without distance -> {xs}")
    skin_unit_checks(d)
    ixu = CR.EventIndex([dict(type="use_item_start", seq=0, epoch_ms=0, player="Bravo", item=dict(id="minecraft:shield", count=1)),
                         dict(type="use_item_stop", seq=1, epoch_ms=160, player="Bravo", item=dict(id="minecraft:shield", count=1), reason="stopped"),
                         dict(type="use_item_start", seq=2, epoch_ms=300, player="Bravo", item=dict(id="minecraft:bow", count=1)),
                         dict(type="bow_shoot", seq=3, epoch_ms=500, player="Bravo", force=1.0)])
    us_ = [C2P.using_at(ixu, "Bravo", t) for t in (100, 200, 400, 600)]
    row("u using", "use_item_stop / bow_shoot", us_ == [True, False, True, False] and not ixu.unknown_types, f"using at 100 / 200 / 400 / 600 ms: {us_}")
    m = colour_margins()
    row("u colours", "teams / mobs", min(m.values()) >= 50, f"min RGB distance to every shaded block colour: { {k: round(v, 1) for k, v in m.items()} }")
    et = CR.EntityTrack(None, [dict(type="entity_spawn", epoch_ms=0, entity=dict(uuid="z", type="minecraft:zombie", pos=[0, 61, 0])),
                               dict(type="entity_spawn", epoch_ms=0, entity=dict(uuid="i", type="minecraft:item", pos=[1, 61, 0], name="minecraft:stone")),
                               dict(type="entity_damage", epoch_ms=1500, entity=dict(uuid="z", type="minecraft:zombie", pos=[3, 61, 0]), damage=1.0)])
    st_ = {t: {e["uuid"]: C2P.is_stale(et.mode, e, t) for e in et.at(t)} for t in (500, 1400, 1600, 2400, 2600)}
    row("u stale", "events-mode mobs", et.mode == "events" and [st_[t]["z"] for t in sorted(st_)] == [False, True, False, False, True] and not any(v["i"] for v in st_.values())
        and et.at(1600)[0]["pos"][0] == 3 and [e for e in et.at(0) if e["uuid"] == "i"][0]["item"] == "stone",
        f"zombie stale after 1 s without a position update, fresh again after one; items never: {st_}")
    ent = [C2P.entity_rgb({"type": t, "item": it}) for t, it in (("zombie", None), ("cow", None), ("item", "oak_planks"), ("tnt", None))]
    row("u entity rgb", "mobs / items", ent[0] == C2P.MOB_HOSTILE and ent[1] == C2P.MOB_PASSIVE and ent[2] == tuple(int(v) for v in CP.item_rgb("oak_planks"))
        and ent[3] == tuple(int(v) for v in CP.BLOCK_RGB[CP.TNT]), f"{ent}")


def skin_unit_checks(d):
    """skin adapter rules on hand-made episode_info / camera metas: per-bot skin_assignment (the leader's file failing the OTHER
    bot, the follower's pool-differed note), a start-time camera meta (not finalized) vs a finalized one, a read-only batch."""
    sb = os.path.join(d, "skin"); shutil.rmtree(sb, ignore_errors=True)
    pool = os.path.join(sb, "skins", "pool"); os.makedirs(pool); os.makedirs(os.path.join(sb, "camera", "output_alpha", "0")); os.makedirs(os.path.join(sb, "output"))
    png = {b: CS.png_bytes(np.full((64, 64, 4), (40 * k + 20, 90, 200, 255), np.uint8)) for k, b in enumerate(("Alpha", "Bravo"))}
    sk = {b: {"skin_id": f"u_{b.lower()}", "model": "classic", "source": "procedural", "sha256": hashlib.sha256(png[b]).hexdigest(), "file": f"pool/u_{b.lower()}.png",
              "tags": [], "texture_mode": "local"} for b in png}
    for b in png:
        open(os.path.join(pool, f"u_{b.lower()}.png"), "wb").write(png[b])
    lead = {"skins": sk, "skin_assignment": {"ok": False, "applied": {"Alpha": True, "Bravo": False}, "errors": ["Bravo: texture not applied within 15000 ms"]}}
    foll = {"skins": sk, "skin_assignment": {"ok": False, "applied": {"Bravo": True}, "errors": ["Bravo: leader applied u_bravo (pool differed between bots)"]}}
    gen = {"skins": sk, "skin_assignment": {"ok": False, "applied": {"Alpha": True, "Bravo": True}, "errors": ["rcon timeout"]}}
    cmp_ = os.path.join(sb, "camera", "output_alpha", "0", "camera_alpha_meta.json")
    ep = {"base": sb, "bots": ["Alpha", "Bravo"], "inst": 0, "files": {}}
    ent = {"status": "loaded", "sha256_match": True, "loaded_epoch_ms": 123.0}

    def rec(ia, ib, bot, meta):
        if meta is not None:
            json.dump(meta, open(cmp_, "w"))
        elif os.path.exists(cmp_):
            os.remove(cmp_)
        return C2P.skin_record(ep, bot, {"Alpha": {"state_metadata": ia}, "Bravo": {"state_metadata": ib}}, pool)
    ra = rec(lead, foll, "Alpha", None)                                           # leader's file: Bravo's failure does not fail Alpha
    rb_ = rec(lead, {"skins": sk, "skin_assignment": dict(foll["skin_assignment"], errors=[])}, "Bravo", None)  # follower's own file says applied
    rf = rec(lead, foll, "Bravo", None)                                           # follower's pool-differed note is informational
    rg = rec(gen, foll, "Alpha", None)                                            # an error naming no player counts for everyone
    start = {"options": {"fov_deg": 70}, "local_skins": {"enabled": True, "skins": {}}}
    fin = dict(start, stop_epoch_seconds=1.0, capture_stats={"captured": 1}, local_skins={"enabled": True, "skins": {"u_bravo:classic": ent}})
    ru = rec(lead, foll, "Bravo", start)                                          # Camera of Alpha sees Bravo: start-time meta -> unknown
    rl = rec(lead, foll, "Bravo", fin)
    rz = rec(lead, foll, "Bravo", dict(fin, local_skins={"enabled": True, "skins": {}}))   # finalized, entry absent -> not loaded
    sk = os.path.join(sb, "output", "skins"); os.remove(os.path.join(sk, f"{rl['sha1']}.png")); os.chmod(sk, 0o555)
    try:                                                                          # can this platform make a directory read-only? (Windows and root: no)
        open(os.path.join(sk, ".probe"), "w").close(); os.remove(os.path.join(sk, ".probe")); ro = False
    except OSError:
        ro = True
    try:
        rr = rec(lead, foll, "Bravo", fin) if ro else None                         # read-only batch: a problem, not an exception
    except OSError as e:
        rr = {"ok": None, "problems": [f"raised {e}"]}
    finally:
        os.chmod(sk, 0o755)
    row("u skin rules", "assignment / camera meta / read-only", ra["ok"] is True and ra["assignment_ok"] is False and rb_["ok"] is True and rf["ok"] is True
        and rg["ok"] is False and "rcon timeout" in str(rg["problems"]) and ru["ok"] is True and ru["camera_check"] is None and "not finalized" in ru.get("camera_note", "")
        and ru["applied_epoch_ms"] is None and rl["ok"] is True and rl["camera_check"] is True and rl["applied_epoch_ms"] == 123.0 and rz["ok"] is False
        and rz["camera_check"] is False and (rr is None or (rr["ok"] is False and "cannot copy" in str(rr["problems"]))),
        f"leader file + other bot failed -> Alpha ok {ra['ok']} (assignment_ok {ra['assignment_ok']}); follower note -> {rf['ok']}; unnamed error -> {rg['ok']}; "
        f"start-time camera meta -> camera_check {ru['camera_check']}, ok {ru['ok']}; finalized loaded -> {rl['camera_check']} {rl['applied_epoch_ms']}; absent -> "
        f"{rz['camera_check']}; read-only output/skins -> " + (f"ok {rr['ok']} {str(rr['problems'])[:60]}" if rr is not None else "not testable here (directory permissions not enforced)"))


def robust_checks(work, root):
    """one corrupt episode never stops the batch; done episodes are skipped on a rerun; --root = the batch dir names by its basename."""
    src = os.path.join(root, "batch_1"); brk = os.path.join(work, "brk", "batch_1"); bo = os.path.join(work, "brk_out")
    shutil.rmtree(os.path.dirname(brk), ignore_errors=True); shutil.rmtree(bo, ignore_errors=True); shutil.copytree(src, brk)
    bad = sorted(glob.glob(os.path.join(brk, "output", "*_000002_world_instance_000_world_snapshot_start.json.gz")))[0]
    open(bad, "wb").write(b"\x1f\x8b garbage")
    rc1 = C2P.main(["--root", brk, "--out", bo, "--no-render", "--clips"]); s1 = json.load(open(os.path.join(bo, "summary.json")))
    rc2 = C2P.main(["--root", brk, "--out", bo, "--no-render"]); s2 = json.load(open(os.path.join(bo, "summary.json")))
    names = sorted(os.listdir(os.path.join(bo, "episodes")))
    other = os.path.join(work, "brk2", "batch_1"); shutil.rmtree(os.path.dirname(other), ignore_errors=True); shutil.copytree(src, other)
    rc3 = C2P.main(["--root", other, "--out", bo, "--no-render", "--episodes", "1"]); s3 = json.load(open(os.path.join(bo, "summary.json")))
    row("j robust", "corrupt episode", rc1 == 1 and s1["counts"] == {"ok": 2, "failed": 1} and rc2 == 1 and s2["counts"] == {"skipped": 2, "failed": 1}
        and names == ["batch_1_000001_000", "batch_1_000003_000"] and rc3 == 1 and "name collision" in s3["episodes"][0].get("error", ""),
        f"rc {rc1}/{rc2}, counts {s1['counts']} then {s2['counts']}, episodes {names}; another batch_1 into the same --out: {s3['episodes'][0].get('error', '')[:60]}")


# ---- G-buffer v2 ----------------------------------------------------------------------------------------------------------
KEY_RGB = ((255, 0, 0), (0, 255, 0), (0, 0, 255), (0, 255, 255), (255, 0, 128), (128, 0, 255))   # state_proxy's HUD / overlay key colours


def decode(path):
    import cv2
    cap, out = cv2.VideoCapture(path), []
    while True:
        ok, fr = cap.read()
        if not ok:
            break
        out.append(fr[..., ::-1])
    cap.release()
    return np.stack(out) if out else np.zeros((0, 1, 1, 3), np.uint8)


def gbv2_checks(root, out, reps, args, R):
    """module docstring "G-buffer v2": (a)-(g) on the garden with fixed cameras, (h), (i), (gb rebuild) on the rendered episodes."""
    from mineengine import state_proxy as SPX
    ep = next(e for e in CR.episodes(root) if os.path.basename(e["base"]) == "batch_0" and e["eid"] == 1)
    tr = truth_of(ep); P = C2P.EpisodeProxy(ep, root, args); sm = P.mesher(); pal = sm.palette; tix = sm.tix
    cell = {str(s0): np.asarray(c, float) for c, s0 in zip(tr["garden_cells"], tr["garden_s0"])}
    tg = float(tr["garden_t"]); tb, ta = tg - 1.0, tg + 1000.0

    def view(s0, t, dy=0.5, dz=2.5, dx=0.5, pitch=0.0):
        g = P.render_view(t, [(cell[s0] + [dx, dy, 0.5 + dz], 0.0, pitch)], R, 96, 168, 70.0)
        return {k: v[0] for k, v in g.items()}

    def sel(g, sub):
        return np.array([False] + [sub in e["state"] for e in pal[1:]])[g["bstate"]]

    def names(g, m):
        return sorted({pal[b]["state"].split(":", 1)[1] for b in np.unique(g["bstate"][m]) if b})

    def ext(m):
        r = np.nonzero(m.any(1))[0]
        return int(r[-1] - r[0] + 1) if len(r) else 0
    door = "minecraft:oak_door[facing=south,half=lower,hinge=left,open=false,powered=false]"
    g0, g1 = view(door, tb, dy=1.0, dz=3.0), view(door, ta, dy=1.0, dz=3.0)
    m0, m1 = sel(g0, "oak_door"), sel(g1, "oak_door"); n0, n1 = names(g0, m0), names(g1, m1)
    row("gb a door", "batch_0/1 garden", m0.sum() > 50 and m1.sum() > 10 and int((g0["logdepth"] != g1["logdepth"]).sum()) > 50 and len(n0) == 2 and len(n1) == 2
        and all("open=false" in x for x in n0) and all("open=true" in x for x in n1),
        f"closed {int(m0.sum())} px {n0} -> open {int(m1.sum())} px {n1}; logdepth differs on {int((g0['logdepth'] != g1['logdepth']).sum())} px")
    g = view("minecraft:oak_slab[type=bottom,waterlogged=false]", tb, dy=0.5, dz=3.0, dx=1.0)
    rb, rt = np.nonzero(sel(g, "type=bottom"))[0], np.nonzero(sel(g, "type=top"))[0]
    row("gb b slabs", "batch_0/1 garden", len(rb) > 20 and len(rt) > 20 and rt.max() <= rb.min(),
        f"top slab rows {rt.min() if len(rt) else None}..{rt.max() if len(rt) else None} ({len(rt)} px), bottom slab rows {rb.min() if len(rb) else None}.."
        f"{rb.max() if len(rb) else None} ({len(rb)} px), eye at mid-height looking north")
    w0, w1 = view("minecraft:wheat[age=0]", tb, dy=0.8, dz=2.0, pitch=-0.35), view("minecraft:wheat[age=0]", ta, dy=0.8, dz=2.0, pitch=-0.35)
    mw0, mw1 = w0["btype"] == tix["wheat"], w1["btype"] == tix["wheat"]
    lw = C2P.legacy_id("minecraft:wheat[age=7]")
    row("gb c wheat", "batch_0/1 garden", 0 < mw0.sum() < mw1.sum() and names(w0, mw0) == ["wheat[age=0]"] and names(w1, mw1) == ["wheat[age=7]"]
        and (w0["block"][mw0] == lw).all() and (w1["block"][mw1] == lw).all() and C2P.CP.block_id("minecraft:wheat[age=7]") == C2P.CP.AIR and lw != C2P.CP.AIR,
        f"age 0: {int(mw0.sum())} px {names(w0, mw0)}, age 7: {int(mw1.sum())} px {names(w1, mw1)}; btype wheat both, legacy id {lw} "
        f"({C2P.CP.NAMES[lw]}: block_id folds wheat to air, the drawn crop keeps a non-air id)")
    sn = "minecraft:snow[layers=1]"
    s0_, s1_ = view(sn, tb, dy=0.3, dz=2.0), view(sn, ta, dy=0.3, dz=2.0)
    e0, e1 = ext(sel(s0_, "minecraft:snow[") & (s0_["face"] == 5)), ext(sel(s1_, "minecraft:snow[") & (s1_["face"] == 5))
    row("gb d snow", "batch_0/1 garden", e0 >= 1 and e1 >= 3 * e0 and names(s0_, sel(s0_, "snow[")) == ["snow[layers=1]"] and names(s1_, sel(s1_, "snow[")) == ["snow[layers=6]"],
        f"side (+z) height layers 1: {e0} px -> layers 6: {e1} px (expected x6)")
    wa = "minecraft:water[level=0]"
    a0_, a1_ = view(wa, tb, dy=0.5, dz=2.5), view(wa, ta, dy=0.5, dz=2.5)
    h0, h1 = ext(sel(a0_, "water[") & (a0_["face"] == 5)), ext(sel(a1_, "water[") & (a1_["face"] == 5))
    row("gb e water", "batch_0/1 garden", h0 > 5 and 0.35 < h1 / max(h0, 1) < 0.7 and names(a1_, sel(a1_, "water[")) == ["water[level=4]"],
        f"side (+z) height level 0: {h0} px -> level 4: {h1} px (expected (8 - 4) / 8 = 0.5)")
    vt, vp_ = view("minecraft:torch", tb, dy=0.6, dz=1.5), view("minecraft:poppy", tb, dy=0.6, dz=1.5)
    mt, mp = vt["btype"] == tix["torch"], vp_["btype"] == tix["poppy"]
    row("gb f torch / flower", "batch_0/1 garden", mt.sum() > 5 and mp.sum() > 5 and vt["block"][mt].all() and vp_["block"][mp].all()
        and C2P.CP.block_id("minecraft:torch") == C2P.CP.block_id("minecraft:poppy") == C2P.CP.AIR and mt.mean() < 0.2,
        f"torch {int(mt.sum())} px ({100 * mt.mean():.1f}% of the view: cropped to the stick), poppy {int(mp.sum())} px; block_id air (dropped before), drawn with "
        f"legacy ids {sorted(set(np.unique(vt['block'][mt]).tolist()) | set(np.unique(vp_['block'][mp]).tolist()))}")
    fu = "minecraft:furnace[facing=south,lit=false]"
    f0, f1 = view(fu, tb, dy=0.5, dz=2.5), view(fu, ta, dy=0.5, dz=2.5)
    mf = sel(f0, "furnace[")
    row("gb g furnace lit", "batch_0/1 garden", mf.sum() > 50 and all(np.array_equal(f0[k], f1[k]) for k in ("logdepth", "face", "btype", "block", "shade"))
        and not np.array_equal(f0["bstate"], f1["bstate"]) and names(f0, mf) == ["furnace[facing=south,lit=false]"] and names(f1, sel(f1, "furnace[")) == ["furnace[facing=south,lit=true]"],
        f"{int(mf.sum())} px: logdepth / face / btype / block / shade identical, bstate {names(f0, mf)} -> {names(f1, sel(f1, 'furnace['))}")
    reg = C2P.load_registry(args.registry); bad, nb, npx, ncl, nrt, why = [], 0, 0, 0, 0, ""
    for (bd, eid), rp in sorted(reps.items()):
        ed = os.path.join(out, "episodes", rp["name"])
        if not os.path.exists(os.path.join(ed, "states.json")):
            continue
        T = json.load(open(os.path.join(ed, "types.json")))["types"]; St = json.load(open(os.path.join(ed, "states.json")))["states"]
        rt_ = T == [""] + sorted(reg.types) and all(T[i] == t for t, i in {t: i for i, t in enumerate(T)}.items())
        okp = St[0] is None and all((T[e["btype"]] == e["type"]) if e["btype"] != 65535 else not e["known"] for e in St[1:])
        lut = np.array([0] + [e["btype"] for e in St[1:]], np.int64)
        for b in ("Alpha", "Bravo"):
            g = np.load(os.path.join(ed, b, "gbuf.npz")); bs, bt = g["bstate"].astype(np.int64), g["btype"].astype(np.int64)
            ok_ = rt_ and okp and np.array_equal(bt, lut[bs]) and not bt[g["pid"] > 0].any() and not (bs[g["logdepth"] == 0]).any() and ((bs > 0) | (g["pid"] > 0) | (g["logdepth"] == 0)).all()
            nb += 1; npx += int((bs > 0).sum())
            if not ok_:
                bad.append(f"{rp['name']}/{b}")
        for c in rp["clips"]:
            cd = os.path.join(out, "clips", c); z = np.load(os.path.join(cd, "gbuf.npz")); m = json.load(open(os.path.join(cd, "meta.json")))
            ncl += 1; ok_c = all(k in z.files for k in C2P.GB_V2) and os.path.exists(os.path.join(cd, "states.json")) and (m.get("gbuf_v2") or {}).get("bstate") == "states.json"
            nrt += ok_c
            if not ok_c and not why:
                why = f" first bad clip {c}"
    row("gb h btype round trip", "rendered episodes", nb > 0 and not bad and nrt == ncl > 0,
        f"{nb} bot G-buffers, {npx} block pixels: btype == types.json index of the bstate entry's type, 0 on entities / sky, block pixels all carry a state; "
        f"types.json = [''] + {len(reg.types)} sorted registry types; {nrt} / {ncl} clips with btype / bstate / face + states.json{why} {bad[:3]}")
    res_i, why = [], ""
    for (bd, eid), rp in sorted(reps.items()):
        ed = os.path.join(out, "episodes", rp["name"])
        for b, v in (rp.get("bots") or {}).items():
            pv, ph = os.path.join(ed, b, "proxy.mp4"), os.path.join(ed, b, "proxy_hud.mp4")
            if not (os.path.exists(pv) and os.path.exists(ph)):
                continue
            g = np.load(os.path.join(ed, b, "gbuf.npz")); St = json.load(open(os.path.join(ed, "states.json")))
            rows_ = [json.loads(x) for x in open(os.path.join(ed, b, "state.jsonl"))]
            team = {"Alpha": 0, "Bravo": 1}.get(v["other"], 1); rgb = np.asarray(St["rgb"], np.uint8)
            exp = np.concatenate([SPX.colorize({"logdepth": g["logdepth"][k:k + 1], "shade": g["shade"][k:k + 1], "pid": g["pid"][k:k + 1], "block": g["bstate"][k:k + 1],
                                                "edge": g["edge"][k:k + 1]}, block_rgb=rgb, team=0, team_of={1: team, **{e["pid"]: tuple(e["rgb"]) for e in r["entities"] if e["pid"] is not None}}, t0=k)
                                  for k, r in enumerate(rows_)])
            dp, dh = decode(pv).astype(np.int16), decode(ph).astype(np.int16)
            alive = np.array([r["self"]["health"] is None or r["self"]["health"] > 0 for r in rows_])
            Hh = exp.shape[1]; hud = (slice(None), slice(int(Hh * 0.8), Hh), slice(exp.shape[2] // 4, 3 * exp.shape[2] // 4))
            e_p = float(np.abs(dp - exp).mean()); e_h = float(np.abs(dh[alive][hud] - exp[alive][hud]).mean()) if alive.any() else 0.0
            kc = lambda x: float(np.mean([(np.abs(x - np.array(c)).max(-1) < 40).mean() for c in KEY_RGB]))   # noqa: E731
            res_i.append((f"{rp['name']}/{b}", len(dp) == len(exp), e_p, e_h, kc(dp), kc(dh)))
    ok_i = bool(res_i) and all(n_ and ep_ < 4.0 and eh > 12.0 and kp < 2e-4 for _, n_, ep_, eh, kp, kh in res_i) and max(r[5] for r in res_i) > 1e-4
    row("gb i no HUD", "rendered episodes", ok_i, "; ".join(f"{n}: proxy.mp4 vs white-box colorize mean |d| {ep_:.2f}, proxy_hud HUD strip |d| {eh:.1f}, key-colour px "
                                                              f"{kp:.1e} vs {kh:.1e}" for n, _, ep_, eh, kp, kh in res_i[:3]) + f" ({len(res_i)} videos)")
    rb_, why = [], []
    for (bd, eid), rp in sorted(reps.items()):
        for b, v in (rp.get("bots") or {}).items():
            cp_ = os.path.join(out, "episodes", rp["name"], b, "cams.npz")
            if "gbv2" not in v:
                continue
            c = np.load(cp_); sv, iv = c["state_version"], c["block_version"]; q = v["gbv2"]
            only = int(((np.diff(sv) > 0) & (np.diff(iv) == 0)).sum())
            rb_.append((q["mesh_versions"] == len(np.unique(sv)) and q["mesh_full"] >= 1 and q["mesh_full"] + q["mesh_updates"] <= q["mesh_versions"], only,
                        (bd, eid) == ("batch_0", 1), q["mesh_updates"]))
            why.append(f"{rp['name']}/{b}: {q['mesh_versions']} state versions meshed ({q['mesh_full']} whole {q['mesh_ms']} ms, {q['mesh_updates']} updates "
                       f"{q['update_ms']} ms, {q['chunks_remeshed']} columns), {only} state-only steps")
    row("gb rebuild", "rendered episodes", bool(rb_) and all(r[0] for r in rb_) and all(r[1] >= 1 and r[3] >= 1 for r in rb_ if r[2]), "; ".join(why[:4]))
    # incremental mesh == whole mesh over every state version of the build episode (StateMesher.update)
    bt = P.bt; cs_ = sorted(set(np.searchsorted(bt.ct, bt.ct, side="right").tolist())); s_, _ = bt.states_at(-1e18); prev = s_.copy()
    g, o, _ = P.padded(s_); m = sm.mesh(g, o); nv, nbad, nup, pad = 0, 0, 0, args.pad

    def canon(mm):
        Q = mm["quads"]; key = np.concatenate([np.round(Q["V"].reshape(len(Q["bid"]), -1) * 1e4).astype(np.int64), Q["cell"].astype(np.int64),
                                               np.stack([Q["bid"], Q["btype"], Q["bstate"]], 1).astype(np.int64), np.round(Q["nrm"] * 1e4).astype(np.int64)], 1)
        return key[np.lexsort(key.T[::-1])]
    for n in cs_:
        t = bt.ct[n - 1]; s_, _ = bt.states_at(t); ch = np.argwhere(s_ != prev); prev = s_.copy()
        if not len(ch):
            continue
        if ((ch[:, 0] == 0) | (ch[:, 0] == s_.shape[0] - 1) | (ch[:, 2] == 0) | (ch[:, 2] == s_.shape[2] - 1)).any():   # border: re-padded as a whole (render)
            g, o, _ = P.padded(s_); m = sm.mesh(g, o)
            continue
        qq = ch + [pad, 0, pad]; g[tuple(qq.T)] = s_[tuple(ch.T)]; m = sm.update(m, g, qq); nup += m.get("n_chunks", 0) > 0
        g2, o2, _ = P.padded(s_); f_ = sm.mesh(g2, o2); nv += 1
        nbad += not (np.array_equal(g, g2) and np.array_equal(canon(m), canon(f_)))
    row("gb mesh update", "batch_0/1 build", nv > 5 and nup > 5 and nbad == 0,
        f"{nv} state versions applied incrementally ({nup} column updates): quads == a whole remesh at every one ({nbad} differ), padded grid kept in sync")
    # legacy ids: a hit block pixel never has block_ext 0 (decorations now drawn carry their material id or UNKNOWN)
    lg, why = [], ""
    for (bd, eid), rp in sorted(reps.items()):
        for b in (rp.get("bots") or {}):
            gp = os.path.join(out, "episodes", rp["name"], b, "gbuf.npz")
            if os.path.exists(gp):
                g_ = np.load(gp); hit = (g_["logdepth"] > 0) & (g_["pid"] == 0)
                lg.append((int((hit & (g_["block_ext"] == 0)).sum()), int(hit.sum())))
    row("gb legacy ids", "rendered episodes", bool(lg) and sum(x for x, _ in lg) == 0 and C2P.legacy_id("minecraft:torch") == C2P.CP.UNKNOWN
        and C2P.legacy_id("minecraft:oak_button[face=wall,facing=north,powered=false]") == C2P.CP.ID["oak_planks"]
        and C2P.legacy_id("minecraft:lantern[hanging=false,waterlogged=true]") == C2P.CP.UNKNOWN and C2P.legacy_id("minecraft:water[level=0]") == C2P.CP.WATER,
        f"{sum(x for x, _ in lg)} of {sum(y for _, y in lg)} hit block pixels with block_ext 0 over {len(lg)} G-buffers; torch -> UNKNOWN, oak_button -> oak_planks, "
        f"waterlogged lantern -> UNKNOWN (not water), water -> water")
    # fx: the dig stage on the dug cell (break episode), nothing elsewhere; proxy.mp4 shows the cracks
    fxr = []
    for (bd, eid), rp in sorted(reps.items()):
        for b in (rp.get("bots") or {}):
            ed = os.path.join(out, "episodes", rp["name"]); gp = os.path.join(ed, b, "gbuf.npz")
            if not os.path.exists(gp):
                continue
            g_ = np.load(gp); rows_ = [json.loads(x) for x in open(os.path.join(ed, b, "state.jsonl"))]
            dg = [r["self"].get("dig") for r in rows_]; kd = [k for k, d_ in enumerate(dg) if d_]
            clean = all(not g_["fx"][k].any() for k, d_ in enumerate(dg) if not d_)
            vals = all(set(np.unique(g_["fx"][k]).tolist()) <= {0, int(dg[k]["stage"]) + 1} for k in kd)
            seen = [k for k in kd if g_["fx"][k].any()]
            ent = all(not (g_["fx"][k] & (g_["pid"][k] > 0)).any() and (g_["bstate"][k][g_["fx"][k] > 0] > 0).all() for k in seen)
            crk = 0
            pv = os.path.join(ed, b, "proxy.mp4")
            if seen and os.path.exists(pv):
                vid = decode(pv); rgb = np.asarray(json.load(open(os.path.join(ed, "states.json")))["rgb"], np.uint8)
                team = {"Alpha": 0, "Bravo": 1}.get(rp["bots"][b]["other"], 1)
                for k in seen:
                    mk = np.repeat(np.repeat(g_["fx"][k] > 0, 2, 0), 2, 1)
                    e_ = C2P.plain_frames({q: g_[q][k:k + 1] for q in C2P.PLAIN_KEYS}, rgb, {1: team, **{e["pid"]: tuple(e["rgb"]) for e in rows_[k]["entities"] if e["pid"] is not None}}, k)[0]
                    crk += int(dg[k]["stage"]) >= 5 and int((vid[k][mk].max(-1) < 40).sum()) >= int((e_[mk].max(-1) < 40).sum()) + 2
            fxr.append((f"{rp['name']}/{b}", len(kd), len(seen), clean and vals and ent, crk, sum(1 for k in seen if int(dg[k]["stage"]) >= 5)))
    dig_eps = [x for x in fxr if x[1]]
    row("gb fx dig", "rendered episodes", bool(dig_eps) and all(x[3] for x in fxr) and all(x[2] >= 0.6 * x[1] for x in dig_eps) and all(x[4] >= 0.6 * x[5] for x in dig_eps),
        "; ".join(f"{n}: {nk} dig frames, fx pixels in {ns}, values == stage + 1 on block pixels / 0 elsewhere {ok_}, cracks in proxy.mp4 on {ck} / {n5} frames with stage >= 5"
                  for n, nk, ns, ok_, ck, n5 in dig_eps[:3]) + f" ({len(fxr)} G-buffers, the others fx 0 everywhere)")
    # waterlogged models: only the implied water's surface (top at 8 / 9) is drawn; the model is not boxed in at a water / air border
    S_ = ["minecraft:stone", "minecraft:oak_fence[east=false,north=false,south=false,waterlogged=true,west=false]", "minecraft:oak_slab[type=bottom,waterlogged=true]",
          "minecraft:oak_slab[type=bottom,waterlogged=false]"]
    from mineengine import voxel_proxy as VP
    wl = VP.StateMesher(reg, S_, C2P.legacy_id); wv = []
    for k in (1, 2):
        gw = np.full((7, 4, 7), wl.air, np.int32); gw[:, 0, :] = 0; gw[3, 1, 3] = k
        mw = wl.mesh(gw, (0, 0, 0)); gw[3, 1, 3] = 3; md = wl.mesh(gw, (0, 0, 0))
        for eye in ((3.5, 1.45, 6.2), (3.5, 1.3, 9.0), (3.9, 1.6, 7.3)):
            a_ = R.render(mw, [(eye, 0.0, -0.1)], H=96, W=168)["bstate"][0]; d_ = R.render(md, [(eye, 0.0, -0.1)], H=96, W=168)["bstate"][0]
            nm, nw_, nd = int((a_ == wl.bstate[k]).sum()), int((a_ == wl.water_bstate).sum()), int((d_ == wl.bstate[3]).sum())
            wv.append((k, nm, nw_, nd, int(((d_ == wl.bstate[3]) & (a_ == wl.water_bstate)).sum())))
    row("gb waterlogged", "unit", all(nm > nw_ for k, nm, nw_, nd, _ in wv if k == 1) and all(x[4] == 0 for x in wv if x[0] == 2),
        "; ".join(f"{'fence' if k == 1 else 'slab'}: model {nm} px, implied water {nw_} px" + (f", dry-slab pixels turned water {cov}" if k == 2 else "") for k, nm, nw_, nd, cov in wv[::3])
        + " (side / bottom faces of the implied water not drawn)")
    if getattr(R, "raster", None) == "nvdiffrast":                             # GPU vs the numpy rasteriser (same channel code): parity + speed
        import torch
        Rc = C2P.make_renderer("cpu"); vw = [(cell[k] + [0.5, 1.0, 3.5], 0.0, -0.1) for k in list(cell)[:4]]
        mm, _ = P.state_mesh(P.bt.states_at(ta)[0]); bad_g = {}
        a_, b_ = R.render(mm, vw, H=C2P.H, W=C2P.W), Rc.render(mm, vw, H=C2P.H, W=C2P.W)
        for k in ("logdepth", "bstate", "btype", "face", "block", "pid"):
            d_ = np.abs(a_[k].astype(np.int64) - b_[k].astype(np.int64)); bad_g[k] = float((d_ > (1 if k == "logdepth" else 0)).mean())
        rp1 = reps[("batch_0", 3)]; c1 = np.load(os.path.join(out, "episodes", rp1["name"], "Alpha", "cams.npz"))
        P1 = C2P.EpisodeProxy(next(e for e in CR.episodes(root) if os.path.basename(e["base"]) == "batch_0" and e["eid"] == 3), root, args)
        m1, _ = P1.state_mesh(P1.bt.states_at(float(c1["t_ms"][0]))[0]); cv = [(c1["eye"][k], float(c1["yaw"][k]), float(c1["pitch"][k])) for k in range(0, len(c1["t_ms"]), 25)]
        a1, b1 = R.render(m1, cv, H=C2P.H, W=C2P.W), Rc.render(m1, cv, H=C2P.H, W=C2P.W)
        bad_e = {k: float((np.abs(a1[k].astype(np.int64) - b1[k].astype(np.int64)) > (1 if k == "logdepth" else 0)).mean()) for k in bad_g}
        torch.cuda.synchronize(); t0 = time.time()
        for v_ in cv:
            R.render(m1, [v_], H=C2P.H, W=C2P.W)
        torch.cuda.synchronize(); ms = 1000 * (time.time() - t0) / len(cv)
        row("gb gpu parity", "garden + batch_0/3", max(bad_g.values()) < 0.01 and max(bad_e.values()) < 0.01,
            f"nvdiffrast vs numpy rasteriser, fraction of pixels differing (logdepth by > 1): garden {bad_g}, episode {len(cv)} frames {bad_e}; "
            f"GPU {ms:.1f} ms / frame at {C2P.H} x {C2P.W} ({len(m1['F'])} triangles)")


# ---- main ---------------------------------------------------------------------------------------------------------------
def build(work, seconds):
    root = os.path.join(work, "root")
    shutil.rmtree(root, ignore_errors=True)
    return root, CS.write_root(root, seconds=seconds, video=True, log=lambda *_: None, real_kinds=REAL)


def truth_of(ep):
    p = glob.glob(os.path.join(ep["base"], "synth_truth", f"*_{ep['eid']:06d}_world_instance_{ep['inst']:03d}.npz"))
    return np.load(p[0]) if len(p) == 1 else None


def states_dense(path):
    dense, pal, s = schema_snapshot(path)
    return [pal[i] for i in dense]


def at_or_before(ts, t):
    return max(int(np.searchsorted(ts, t, side="right")) - 1, 0)


def entity_blocks(s):
    """an entity box of state line s cuts the eye -> other-centre segment (line_of_sight only looks at blocks; a mob in
    between hides the other player in the G-buffer, which is right)."""
    e0 = np.asarray(s["cam"]["eye"], np.float64); h = s["other"]["size"][1]
    d = np.asarray(s["other"]["pos"], np.float64) + [0, 0.9 * h, 0] - e0
    for e in s["entities"]:
        if e.get("pos") is None or not e.get("size"):
            continue
        p = np.asarray(e["pos"], np.float64); w, he = e["size"]
        lo, hi = p - [w / 2, 0, w / 2], p + [w / 2, he, w / 2]
        with np.errstate(divide="ignore", invalid="ignore"):
            t1, t2 = (lo - e0) / d, (hi - e0) / d
        tn, tf = np.nanmax(np.minimum(t1, t2)), np.nanmin(np.maximum(t1, t2))
        if tn <= tf and tf > 0 and tn < 1:
            return True
    return False


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--work", required=True); ap.add_argument("--render", action="store_true"); ap.add_argument("--cpu-render", action="store_true")
    ap.add_argument("--no-render", action="store_true", help="CPU only, no G-buffer (the default)"); ap.add_argument("--seconds", type=float, default=20.0)
    ap.add_argument("--registry", default=None, help="block registry json for the rendered runs (cap2proxy --registry)")
    a = ap.parse_args()
    if a.no_render:
        a.render = a.cpu_render = False
    t0 = time.time(); os.makedirs(a.work, exist_ok=True)
    root, made = build(a.work, a.seconds)
    print(f"[selftest] {len(made)} synthetic episodes in {time.time() - t0:.1f}s -> {root}", flush=True)
    eps = CR.episodes(root)
    row("discover", "all", len(eps) == 8 + len(REAL) and all(len(e["bots"]) == 2 and not e["dupes"] for e in eps),
        f"{len(eps)} episodes (5 kinds + 3-episode chain + {len(REAL)} capture-engine layout) joined on (eid, inst)")
    # (f) schema
    for ep in eps:
        rl = os.path.basename(ep["base"]) == "batch_2"
        bad = check_schema_real(ep) if rl else check_schema(ep, None)
        row("f schema", f"{os.path.basename(ep['base'])}/{ep['eid']}", not bad, "; ".join(bad[:6]) if bad else
            "capture-engine record / field names, nested state, skins, sidecar frame_mapping, mod camera meta, schema replay" if rl else
            "files, records, fields, clocks, RLE, schema replay, aligned meta")
    # (g) determinism
    det = os.path.join(a.work, "det"); shutil.rmtree(det, ignore_errors=True)
    r2 = CS.write_episode(os.path.join(det, "batch_0"), 3, "pvp", a.seconds, seed=0, video=True)
    files = sorted(glob.glob(os.path.join(det, "batch_0", "output", "*")) + glob.glob(os.path.join(det, "batch_0", "output", "skins", "*")))
    same = [open(p, "rb").read() == open(p.replace(det, root), "rb").read() for p in files if os.path.isfile(p)]
    t1, t2 = np.load(r2["truth"]), np.load(r2["truth"].replace(det, root))                 # npz zip entries carry a time stamp: compare arrays
    same.append(sorted(t1.files) == sorted(t2.files) and all(np.array_equal(t1[k], t2[k]) for k in t1.files))
    row("g deterministic", "batch_0/3 pvp", len(same) >= 11 and all(same), f"{sum(same)}/{len(same)} files identical on a re-run (output/ bytes, skins, truth arrays)")
    unit_checks(a.work)
    # cap2proxy
    out = os.path.join(a.work, "proxy"); shutil.rmtree(out, ignore_errors=True)
    hw = (48, 84) if a.cpu_render and not a.render else (C2P.H, C2P.W)
    args = types.SimpleNamespace(vfov=70.0, pad=8 if a.cpu_render and not a.render else 48, hop=2.5, cam_mode="hold", clips=True, no_video=False, hw=hw,
                                 no_render=not (a.render or a.cpu_render), force=True, skin_pool=os.path.join(root, "skin_pool", "pool"), registry=a.registry,
                                 hud_proxy=bool(a.render or a.cpu_render))
    R = C2P.make_renderer("gpu") if a.render else C2P.make_renderer("cpu") if a.cpu_render else None
    reps = {}
    for ep in eps:                                                              # CPU stand-in: only batch_0 build / break / pvp / tnt + batch_2 pvp rendered (speed)
        cpu_skip = a.cpu_render and not a.render and not (os.path.basename(ep["base"]) == "batch_0" and ep["eid"] in (1, 2, 3, 5)
                                                          or os.path.basename(ep["base"]) == "batch_2" and ep["eid"] == 3)
        rr = C2P.process(ep, root, out, args, None if cpu_skip else R)
        reps[(os.path.basename(ep["base"]), ep["eid"])] = rr
        print(f"[selftest] cap2proxy {rr['name']} {rr['seconds']}s", flush=True)
    passes = lambda m: m.get("sky_iou") is not None and m["sky_iou"] >= 0.8 and (m.get("player_iou") is None or m["player_iou"] >= 0.2)  # noqa: E731  (the clip quality filter)
    ctl, pex, xfar = [], [], []                                                 # (scope, tick-camera error, action-pose error on frame_mapping frames); pose pairs; far cursors
    for ep in eps:
        sc = f"{os.path.basename(ep['base'])}/{ep['eid']}"; tr = truth_of(ep); rp = reps[(os.path.basename(ep["base"]), ep["eid"])]
        kind = str(tr["kind"]); sc = f"{sc} {kind}"; variant = f"{tr['names']}, T16 {tr['t16']}, time {tr['render_time']}, camera {tr['cam_pos']}"
        real = "engine" in tr.files and str(tr["engine"]) == "real"; use = {}
        ev = CR.load_events(ep["files"]["events"]); snap = CR.load_snapshot(ep["files"]["snap_start"]); end = CR.load_snapshot(ep["files"]["snap_end"])
        bt = CR.BlockTimeline(snap, ev)
        end_ids = np.asarray([CR.block_id(s) for s in end["palette"]], np.uint16)[end["grid"]]
        cmp = bt.compare_end(end)
        lastm = []
        for b in ("Alpha", "Bravo"):
            ts = CR.frame_times(CR.load_actions(ep["files"][f"action_{b}"]))
            g, _ = bt.grid_at(ts[-1]); lastm.append(int((g != end_ids).sum()) + int((g != tr["final"]).sum()))
        ck = [int((bt.grid_at(t)[0] != tr["ckpt"][k]).sum()) for k, t in enumerate(tr["ckpt_t"])]
        wn = np.load(os.path.join(out, "episodes", rp["name"], "world.npz"))
        n_ch = int((tr["final"] != tr["start"]).sum())
        row("a replay", sc, cmp["mismatch_states"] == 0 and cmp["mismatch_ids"] == 0 and sum(lastm) == 0 and sum(ck) == 0 and np.array_equal(end_ids, tr["final"])
            and np.array_equal(wn["final"], tr["final"]) and np.array_equal(wn["start"], tr["start"]),
            f"last-frame replay vs end + truth {lastm}, {len(ck)} checkpoints {sum(ck)}, end states {cmp['mismatch_states']}, {n_ch} voxels changed, world.npz ok")
        row("e names", sc, not rp["unknown_types"], f"{variant}: unknown record types {rp['unknown_types']}; kinds {sorted(k for k in rp['kinds'] if k in ('swing', 'damage', 'held', 'use_start', 'use_end', 'dig'))}")
        for b in ("Alpha", "Bravo"):
            o = "Bravo" if b == "Alpha" else "Alpha"
            A = CR.load_actions(ep["files"][f"action_{b}"]); rb = rp["bots"][b]
            st = [json.loads(line) for line in open(os.path.join(out, "episodes", rp["name"], b, "state.jsonl"))]
            cams = np.load(os.path.join(out, "episodes", rp["name"], b, "cams.npz"))
            hp = np.array([s["self"]["health"] for s in st], np.float64)
            row("e lines", f"{sc} {b}", len(st) == len(A) == len(tr[f"t_{b}"]) and [s["i"] for s in st] == list(range(len(A)))
                and np.array_equal(np.array([s["t_ms"] for s in st]), tr[f"t_{b}"].astype(float)) and rb["frame_time_source"] == ("epochTime" if str(tr["render_time"]) == "relative" else "renderTime"),
                f"{len(st)} state lines / {len(A)} action entries, times == host clock truth ({rb['frame_time_source']})")
            nh = int((np.abs(hp - tr[f"health_{b}"]) > 1e-6).sum())
            row("c health", f"{sc} {b}", nh == 0, f"{nh} of {len(hp)} frames differ; min {hp.min():g}, truth deaths {int(tr[f'deaths_{b}'])}")
            dy = np.abs((cams["yaw64"] - tr[f"cam_yaw_{b}"] + math.pi) % TAU - math.pi).max(); dp = np.abs(cams["pitch64"] - tr[f"cam_pitch_{b}"]).max()
            de = np.abs(cams["eye"] - tr[f"cam_eye_{b}"]).max(); nsn = sum(1 for x in A if x["action"]["sneak"])
            src_ok, neg = rb["camera_source"] == "camera_pose", ""
            if real:                                                            # per-frame source; tick cameras / action pose would be wrong (controls)
                fmf = np.array([str(x) == "frame_mapping" for x in tr[f"cam_src_{b}"]])
                ct_ = CR.camera_track(ev, b); e_t, y_t, _ = CR.camera_at(ct_, CR.frame_times(A), mode="hold")
                e_a = np.array([CR.action_state(x)["eye"] for x in A])
                n_tick = float(np.abs(e_t - tr[f"cam_eye_{b}"]).max() + np.abs((y_t - tr[f"cam_yaw_{b}"] + math.pi) % TAU - math.pi).max())
                y_a, _ = C2P._ffill_angles(A)
                n_act = float((np.abs(e_a - tr[f"cam_eye_{b}"]).max(1) + np.abs((y_a - tr[f"cam_yaw_{b}"] + math.pi) % TAU - math.pi))[fmf].max()) if fmf.any() else 0.0
                src_ok = cams["cam_source"].tolist() == [str(x) for x in tr[f"cam_src_{b}"]] and rb["pose_link"] and fmf.sum() > len(A) // 2 and n_tick > 1e-2
                ctl.append((f"{os.path.basename(ep['base'])}/{ep['eid']} {b}", n_tick, n_act))
                neg = f"; {int(fmf.sum())} frames from frame_mapping {rb['pose_sources']}; tick cameras would be off by {n_tick:.3f}, the action pose by {n_act:.3f} on those frames"
            row("d camera", f"{sc} {b}", dy < 1e-3 and dp < 1e-3 and de < 1e-3 and src_ok and abs(rb["vfov"] - float(tr["vfov"])) < 1e-6
                and np.abs(cams["pitch"]).max() <= C2P.PITCH_MAX + 1e-6,
                f"max |dyaw| {dy:.2e} |dpitch| {dp:.2e} rad, |deye| {de:.2e} m over {nsn} sneaking frames ({rb['camera_source']}, vfov {rb['vfov']:g} {rb['vfov_source']} = truth {float(tr['vfov']):g}){neg}")
            # T15 / T16 / T17 state against the truth (alive frames for the crosshair: the HUD and the tick disagree for < 1 tick at a death)
            alive = (hp > 0) & (tr[f"health_{b}"] > 0)
            tg = [tuple(s["self"]["target"]) if s["self"]["target"] else None for s in st]; tt = [tuple(int(v) for v in x) if x[0] != -1 or x[1] != -1 else None for x in tr[f"target_{b}"]]
            ntg = sum(1 for k in range(len(st)) if alive[k] and tg[k] != tt[k])
            dg = [(tuple(s["self"]["dig"]["cell"]), s["self"]["dig"]["stage"]) if s["self"]["dig"] else None for s in st]
            dt_ = [(tuple(int(v) for v in x[:3]), int(x[3])) if x[3] >= 0 else None for x in tr[f"dig_{b}"]]
            nus = sum(1 for k in range(len(st)) if st[k]["self"]["using"] != bool(tr[f"using_{b}"][k]))
            hb = [[(n, c) for n, c in s["self"]["hotbar"]] for s in st]
            hbt = [[(str(n) or None, int(c)) for n, c in zip(tr[f"hotbar_names_{b}"][k], tr[f"hotbar_counts_{b}"][k])] for k in range(len(st))]
            nhb = sum(1 for k in range(len(st)) if hb[k] != hbt[k])
            ndg = sum(1 for k in range(len(st)) if dg[k] != dt_[k])
            src = rb["counts"]
            row("k t16 / t15 / t17", f"{sc} {b}", ntg == 0 and ndg == 0 and nus == 0 and nhb == 0 and src.get("target_log", 0) == int((hp > 0).sum()) and not src.get("target_dda")
                and not src.get("food_default") and (not real or src.get("using_tick") == len(st)),
                f"crosshair {ntg} / dig {ndg} ({sum(1 for x in dt_ if x)} frames) / using {nus} ({int(tr[f'using_{b}'].sum())} frames) / hotbar {nhb} frames differ; "
                f"sources {src}")
            if str(tr["t16"]) == "action":                                    # the other's T16 from its own action entries
                to = tr[f"t_{o}"]; bo_ = tr[f"body_yaw_{o}"]
                db = [abs((s["other"]["body_yaw"] - bo_[at_or_before(to, s["t_ms"])] + math.pi) % TAU - math.pi) for s in st if s["other"] is not None and s["other"]["body_yaw"] is not None]
                row("k other t16", f"{sc} {b}", len(db) == sum(1 for s in st if s["other"] is not None) and max(db) < 1e-4 and rb["t16_angle_units_other"] == "rad",
                    f"other body_yaw from its action entries on {len(db)} frames, max err {max(db):.1e} rad (units {rb['t16_angle_units_other']})")
            npair = int(tr[f"n_pair_{b}"]); oa = cams["other_alive"]; ip = np.arange(len(A)) < npair
            pok = tr[f"pair_ok_{b}"].astype(bool) & ip if real else ip; use[b] = pok & tr[f"clean_{b}"].astype(bool) if real else ip
            row("n pair", f"{sc} {b}", rb.get("n_pair") == npair and rb.get("unmatched_actions_end") == len(A) - npair and int(cams["n_pair"]) == npair
                and [s["paired"] for s in st] == pok.tolist() and [s["usable"] for s in st] == use[b].tolist() and np.array_equal(cams["pair_ok"], use[b])
                and rb.get("n_rejected") == int(npair - use[b].sum()) and (not real or {"before_recording", "delta", "screen"} <= set(rb.get("rejected") or {}))
                and np.isnan(cams["other"][~oa]).all() and np.isfinite(cams["other"][oa]).all(),
                f"{npair} of {len(A)} entries have a video frame (unmatched_actions_end {rb.get('unmatched_actions_end')}), usable {int(use[b].sum())}, rejected "
                f"{rb.get('n_rejected')} {rb.get('rejected')}; other NaN on {int((~oa).sum())} frames")
            if real:                                                            # pose-exact re-pairs outside the time window (PAIR_WIN_POSE); cursor blocks > 4.5 m
                fm_ = json.load(open(ep["files"][f"video_meta_{b}"]))["frame_mapping"]
                for q, x in enumerate(fm_):
                    if (x.get("pose_pair") == "next" and x["delta_sec"] > 1.5 / 20 + 1e-6) or (x.get("pose_pair") == "prev" and x["delta_sec"] < -1 / 20 - 1e-6):
                        pex.append((x["pose_pair"], st[q]["paired"] and str(cams["cam_source"][q]) == "frame_mapping"))
                for q, x in enumerate(A):
                    bl_ = (x["state"].get("cursor") or {}).get("block") or {}
                    if bl_.get("distance", 0) > C2P.REACH and hp[q] > 0:
                        xfar.append(st[q]["self"]["target"] is None and st[q]["self"]["target_source"] == "log" and tt[q] is None)
            if real:                                                            # skin adapter: state_metadata.skins + pool / exported PNG + camera local_skins
                sk = rb.get("skin") or {}; pp = os.path.join(ep["base"], "output", "skins", f"{sk.get('sha1')}.png"); ap_ = int(tr[f"skin_applied_{b}"])
                pool = os.path.join(root, "skin_pool", "pool"); cc = int(tr[f"skin_camcheck_{b}"]); I_ = json.load(open(ep["files"][f"info_{b}"]))
                row("s skin", f"{sc} {b}", sk.get("ok") is bool(tr[f"skin_ok_{b}"]) and sk.get("sha1") == str(tr[f"skin_sha1_{b}"]) and os.path.exists(pp)
                    and hashlib.sha1(open(pp, "rb").read()).hexdigest() == sk["sha1"] and (sk.get("applied_epoch_ms") is None if ap_ < 0 else sk.get("applied_epoch_ms") == ap_)
                    and (os.path.basename(sk["png"]).endswith(f"_skin_{b}.png") if kind == "build" else os.path.dirname(sk["png"]) == pool)
                    and sk.get("camera_check") is (None if cc < 0 else bool(cc)) and ("not finalized" in sk.get("camera_note", "")) is (cc < 0)
                    and sk.get("assignment_ok") is I_["state_metadata"]["skin_assignment"]["ok"]
                    and json.load(open(os.path.join(ep["base"], "output", "skins", "index.json")))[sk["sha1"]]["skin_id"] == sk["skin_id"],
                    f"{sk.get('skin_id')} ({sk.get('model')}) ok {sk.get('ok')} = truth {bool(tr[f'skin_ok_{b}'])} (own skin_assignment ok {sk.get('assignment_ok')}, "
                    f"errors {I_['state_metadata']['skin_assignment']['errors']}), camera_check {sk.get('camera_check')} {sk.get('camera_note', '')}, sha1 copied to "
                    f"output/skins, png {os.path.basename(str(sk.get('png')))}, applied {sk.get('applied_epoch_ms')}, problems {sk.get('problems')}")
            if kind == "pvp":
                row("e pvp", f"{sc} {b}", (hp < 20).any() and (hp == 0).any() and any(e["kind"] == "damage" for s in st for e in s["events"]),
                    f"hp min {hp.min():g}, frames dead {(hp == 0).sum()}, damage events {sum(1 for s in st for e in s['events'] if e['kind'] == 'damage')}")
            if kind == "pve":
                nm = [sum(1 for e in s["entities"] if e["type"] == "zombie") for s in st]
                row("e pve", f"{sc} {b}", max(nm) > 0 and sum(1 for x in nm if x) > len(nm) // 4 and all(e["rgb"] == list(C2P.MOB_HOSTILE) for s in st for e in s["entities"] if e["type"] == "zombie"),
                    f"zombies in {sum(1 for x in nm if x)} / {len(nm)} frames (max {max(nm)}), truth listed {int((tr[f'mobs_{b}'] > 0).sum())}, colour MOB_HOSTILE")
            if a.render or a.cpu_render:
                gp = os.path.join(out, "episodes", rp["name"], b, "gbuf.npz")
                if os.path.exists(gp):
                    g = np.load(gp)
                    shp = all(g[k].shape == (len(A),) + tuple(args.hw) and g[k].dtype == (np.uint16 if k in C2P.GB_U16 else np.uint8)
                              for k in C2P.GB_EXT + C2P.GB_V2)
                    v0 = int(g["block"].max()) <= 11 and np.array_equal(g["block"], C2P.CP.to_v0(g["block_ext"]))
                    cand = [k for k, s in enumerate(st) if s["other"] and s["other"]["alive"] and s["other"]["in_front"] and s["other"]["dist"] < 20 and s["other"]["line_of_sight"]
                            and s["other"]["dist"] > 1.0]
                    n_mob = len(cand); cand = [k for k in cand if not entity_blocks(st[k])]; n_mob -= len(cand)
                    vis = [k for k in cand if (g["pid"][k] == 1).any()]
                    vids = [v for v in ("proxy.mp4", "side_by_side.mp4") if os.path.exists(os.path.join(out, "episodes", rp["name"], b, v))]
                    row("h render", f"{sc} {b}", shp and v0 and (not cand or len(vis) >= 0.95 * len(cand)) and "proxy.mp4" in vids and ("side_by_side.mp4" in vids) == bool(ep["files"].get(f"video_{b}"))
                        and rb.get("real_frames", npair) == int(use[b].sum()),
                        f"gbuf {g['pid'].shape} (block v0 max {int(g['block'].max())}, block_ext max {int(g['block_ext'].max())}), other rendered in {len(vis)} / {len(cand)} "
                        f"frames in front < 20 m with line of sight ({n_mob} more hidden by an entity), videos {vids}, real frames {rb.get('real_frames')}")
        if kind == "tnt":
            y = np.arange(tr["start"].shape[1]) + int(tr["origin"][1]); below = y <= CS.GY
            crater = int(((tr["start"][:, below, :] != 0) & (tr["final"][:, below, :] == 0)).sum())
            nx = rp["kinds"].get("explode", 0)
            row("e tnt", sc, crater >= 10 and nx >= 1, f"{crater} ground voxels removed (crater), {nx} explosions")
        if real and kind == "pvp":                                              # no respawn records: one per tick dead true -> false
            nre = [sum(1 for s in (json.loads(x) for x in open(os.path.join(out, "episodes", rp["name"], b_, "state.jsonl"))) for e in s["events"] if e["kind"] == "respawn")
                   for b_ in ("Alpha", "Bravo")]
            row("e respawn", sc, rp["tick_respawns"] >= 1 and min(nre) >= 1 and not [e for e in ev if e["type"] in ("player_respawn", "respawn")],
                f"{rp['tick_respawns']} respawns from tick dead -> alive, respawn events in state.jsonl {nre}")
        if real and kind == "pve":                                              # entities record: yaw = head yaw, body_yaw separate (30 deg off)
            zs = [e for b_ in ("Alpha", "Bravo") for s in (json.loads(x) for x in open(os.path.join(out, "episodes", rp["name"], b_, "state.jsonl"))) for e in s["entities"]
                  if e["type"] == "zombie"]
            db_ = max(abs((e["yaw"] - e["body_yaw"] - math.radians(30) + math.pi) % TAU - math.pi) for e in zs)
            row("e mob body", sc, len(zs) > 0 and db_ < 1e-6 and rp["entity_mode"] == "lists", f"{len(zs)} zombie states, max |head - body - 30 deg| {db_:.1e} rad ({rp['entity_mode']})")
        if kind == "pve":
            md = [e for e in CR.EventIndex(ev).all("damage") if (e["raw"].get("entity") or e["raw"].get("victim") or {}).get("type") == "minecraft:zombie"
                  or (e["raw"].get("damager") or e["raw"].get("attacker") or {}).get("type") == "minecraft:zombie"]
            row("e pve", f"{sc} events", len(md) >= 2 and len(CR.EventIndex(ev).all("death")) >= 1, f"{len(md)} zombie damage events, {len(CR.EventIndex(ev).all('death'))} deaths")
        cl = rp["clips"]
        okc, nwb, ntodo, npass, why = bool(cl), 0, 0, 0, ""
        for cid in cl:
            cd = os.path.join(out, "clips", cid); m = json.load(open(os.path.join(cd, "meta.json"))); b = m["player"]
            ok1 = all(k in m for k in ("episode", "type", "player", "start_s", "src_frames", "vfov", "fps", "crop", "solid_cells", "camera", "sky_iou")) and m["state_source"] == "capture" \
                and m["skin"] and len(m["src_frames"]) == C2P.NF and max(m["src_frames"]) < int(tr[f"n_pair_{b}"]) and all(use[b][q] for q in m["src_frames"]) \
                and m["valid"] is bool(tr["valid"]) and m["pair_run"][0] <= min(m["src_frames"]) and max(m["src_frames"]) < m["pair_run"][1] \
                and sum(1 for _ in open(os.path.join(cd, "state.jsonl"))) == C2P.NF
            c = np.load(os.path.join(cd, "cams.npz"))
            ok1 &= c["c2w"].shape == (C2P.NF, 4, 4) and c["other"].shape == (C2P.NF, 3)
            wb = np.load(os.path.join(cd, "whitebox.npz")); ts = CR.frame_times(CR.load_actions(ep["files"][f"action_{b}"])); sf = m["src_frames"]
            g = wb["ids"].copy()
            for q, v in zip(wb["change_cells"], wb["change_id"]):
                g[tuple(q)] = v
            okw = np.array_equal(wb["ids"], bt.grid_at(ts[sf[0]])[0]) and np.array_equal(g, bt.grid_at(ts[sf[-1]])[0]) and np.array_equal(wb["lo"], bt.origin)
            nwb += okw; ok1 &= okw
            td = os.path.join(out, "encode_todo", cid + ".json")
            ntodo += os.path.exists(td) and json.load(open(td))["src_frames"] == sf and os.path.exists(json.load(open(td))["video"])
            npass += bool(passes(m))
            if not ok1 and not why:
                why = f" first bad {cid}"
            okc &= ok1
        nrun = len({tuple(json.load(open(os.path.join(out, "clips", c, "meta.json")))["pair_run"]) for c in cl})
        row("i clips", sc, okc and ntodo == len(cl) and npass == len(cl) and (not real or nrun >= 2 or a.seconds < 15),
            f"{len(cl)} clips inside {nrun} runs of usable pairs: whitebox exact {nwb}, encode_todo {ntodo}, clip filter {npass}{why}")
        if real:
            row("v valid", sc, rp["valid"] is bool(tr["valid"]) and bool(rp["invalid_reasons"]) is (not bool(tr["valid"])),
                f"episode_info flags -> valid {rp['valid']} {rp['invalid_reasons']} (truth {bool(tr['valid'])}), clips flagged the same")
    nl, ne = sum(1 for k, _ in pex if k == "next"), sum(1 for k, _ in pex if k == "prev")
    row("n pose pairs", "batch_2", nl > 0 and ne > 0 and all(v for _, v in pex),
        f"{nl} pose_seq 'next' pairs > 1.5 frames late and {ne} 'prev' pairs > 1 frame early (latency spikes / dropped slots): "
        f"{sum(v for _, v in pex)} / {len(pex)} paired with the frame_mapping camera (PAIR_WIN_POSE)")
    row("k cursor reach", "batch_2", len(xfar) > 0 and all(xfar),
        f"{len(xfar)} alive entries whose state.cursor block is > {C2P.REACH} m (frame-recorder casts 5 m): {sum(xfar)} -> no target (truth: no outline)")
    row("d camera controls", "batch_2", len(ctl) == 2 * len(REAL) and min(c[1] for c in ctl) > 1e-2 and sum(c[2] > 1e-3 for c in ctl) >= len(ctl) // 2,
        f"truth vs tick cameras (lagging) min {min(c[1] for c in ctl):.3f}; vs the action pose on frame_mapping frames > 1e-3 for {sum(c[2] > 1e-3 for c in ctl)} / {len(ctl)} bots "
        f"(max {max(c[2] for c in ctl):.3f}): the frame_mapping camera is what makes 'd camera' pass")
    # (b) persistent chain
    chain = sorted([e for e in eps if os.path.basename(e["base"]) == "batch_1"], key=lambda e: e["eid"])
    for e0, e1 in zip(chain, chain[1:]):
        s0, s1 = states_dense(e0["files"]["snap_end"]), states_dense(e1["files"]["snap_start"])
        i0, i1 = json.load(open(e0["files"]["info_Alpha"])), json.load(open(e1["files"]["info_Alpha"]))
        nm = sum(1 for x, y in zip(s0, s1) if x != y)
        ch = int((truth_of(e0)["final"] != truth_of(e0)["start"]).sum())
        row("b persistent", f"batch_1/{e0['eid']}->{e1['eid']}", len(s0) == len(s1) and nm == 0 and ch > 0 and i0["session_id"] == i1["session_id"]
            and i1["session_episode_index"] == i0["session_episode_index"] + 1 and i1["state_metadata"].get("revisit_of") == e0["eid"] and not i1["reset_world_between_episodes"],
            f"end(k) vs start(k+1): {nm} of {len(s0)} voxels differ; episode k changed {ch}; session index {i0['session_episode_index']}->{i1['session_episode_index']}")
    # vfov sources
    v0 = {(reps[k]["bots"]["Alpha"]["vfov_source"], reps[k]["bots"]["Alpha"]["vfov"]) for k in reps if k[0] == "batch_0"}
    v1 = {(reps[k]["bots"]["Alpha"]["vfov_source"], round(reps[k]["bots"]["Alpha"]["vfov"], 6)) for k in reps if k[0] == "batch_1"}
    v2 = {(reps[k]["bots"][b]["vfov_source"], reps[k]["bots"][b]["vfov"]) for k in reps if k[0] == "batch_2" for b in ("Alpha", "Bravo")}
    n2 = {k[1]: reps[k]["bots"]["Alpha"]["vfov_note"] for k in reps if k[0] == "batch_2"}
    row("vfov source", "all", v0 == {("camera_meta", 70.0)} and v1 == {("camera_meta_flying", 77.0)} and v2 == {("camera_meta", 70.0)}
        and n2[2].startswith(os.path.join("camera", "output_alpha", "0")) and all(n2[e].startswith("aligned") for e in n2 if e != 2),
        f"batch_0 (fov 0.0 = 70 deg, fovEffectScale 0) {v0}; batch_1 (no fovEffectScale: flying x1.1) {v1}; batch_2 (options.fov_deg / fov_effect_scale) {v2}, "
        f"from {sorted(set(n2.values()))[:2]}")
    from mineengine import entity_refs as SR                                                       # entity refs use only ok skins
    SR.main(["--out", out, "--force"]); rs = json.load(open(os.path.join(out, "refs", "summary.json"))); want, got = {}, {}
    for r_ in reps.values():
        for c in r_["clips"]:
            ob = r_["bots"][json.load(open(os.path.join(out, "clips", c, "meta.json")))["player"]]["other"]
            want[c], got[c] = bool((r_["bots"][ob].get("skin") or {}).get("ok")), bool(rs.get(c, {}).get("skin"))
    nb = sum(1 for c in want if not want[c])
    row("s entity_refs", "skin tiles", want == got and nb > 0 and sum(want.values()) > 0,
        f"{sum(got.values())} of {len(got)} clips with the other player's skin tile; {nb} clips whose other skin is not ok get none")
    if a.render or a.cpu_render:
        gbv2_checks(root, out, reps, args, R)
    robust_checks(a.work, root)
    nf = sum(1 for r in ROWS if r[2] == "FAIL")
    print("\n" + table() + f"\n\n[cap_selftest] {'PASS' if not nf else 'FAIL'}: {len(ROWS) - nf} / {len(ROWS)} checks passed in {time.time() - t0:.1f}s", flush=True)
    sys.exit(1 if nf else 0)


if __name__ == "__main__":
    main()
