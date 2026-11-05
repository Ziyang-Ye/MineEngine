"""Capture reader: one batch of our patched SolarisEngine stateful capture (OUTPUT_SCHEMA.md + windows/schema_parts) -> exact
per-time state, numpy only. Files are joined on (episode EEEEEE, instance III), never on the writers' timestamps. Time is the
host clock: an action entry's time (frame_times: renderTime when it is epoch ms >= 1e12, else epochTime * 1000 - act_recorder
logs a relative renderTime when the client clock is not epoch) = world-log epoch_ms, frame i of aligned/<action stem>_camera.mp4
= action entry i (for i < len(frame_mapping)), and a record with epoch_ms = t is visible from the first frame whose time >= t.
  episodes(root)       every episode under root (a batch dir with output/ + aligned/, a prepare_train_dataset dir (videos
                       <...>_EEEEEE_<Bot>_instance_III.mp4 without "_camera"), or any tree, recursive), grouped per
                       (base dir, eid, inst) -> files {action_/meta_/info_/video_/video_meta_<Bot>, events, snap_start,
                       snap_end} (paths or None);
  load_snapshot(path)  gz JSON -> origin int64[3], size, grid int32 [sx, sy, sz] (x, y, z) of palette indices; the RLE is in
                       "order" (default "yzx" = y slowest, x fastest: i = ((y - y0) * sz + (z - z0)) * sx + (x - x0));
  BlockTimeline        start snapshot + block_state records in seq order (the exact block state) as cap_palette ids; box =
                       the snapshot extent (episode_begin box min..max inclusive), records outside counted (n_outside);
                       states first seen in the log grow the palette; a record's visibility time is the running max of
                       epoch_ms in seq order (n_nonmono counts clock steps back); grid_at(t) applies incrementally for
                       increasing t; version = number of cap-id changes applied (equal versions = equal id grids);
  PlayerTrack          tick records -> per-player pose (linear / circular interpolation, held across teleports > 8 m and
                       dead <-> alive; MC degrees -> Mineflayer radians, body_yaw / head_yaw too: the world log never
                       writes radians) and health / held / flags / T16 extras (hand_raised, dead, ...) from the last
                       record <= t;
  EventIndex           T15 / T17 / entity / block events under canonical kinds with aliases for the not-yet-fixed names
                       (swing, damage (by victim) / hit (by attacker), use_start, use_end, use, held, dig, spawn, remove,
                       death, explode, prime, place, break, respawn, animation; the engine's use_item_stop / bow_shoot
                       end a use); paired damage records deduplicated; damage = final_damage when written; a player
                       without respawn records gets one per tick "dead" true -> false (type "tick_respawn"); unknown
                       record types counted;
  EntityTrack          non-player entities alive at t (per-tick entity lists when logged, T3 "entities" records or tick
                       "entities" / T19 per-tick "mobs"; uuids never listed and every entity without lists: start snapshot /
                       entity_spawn until entity_remove / entity_death, last known pos);
  camera_track         T12 camera-pose records of one player (or the camera player's own tick "players" entry) -> (t, eye,
                       yaw, pitch) arrays; eye = feet pos + 1.62, x 0.785 while the bot sneaks (CameraManager scales the
                       camera player), unless the record gives eye / eye_height / scale;
  action_state         one per-frame action entry -> absolute camera, keys, held / hotbar, + T16 fields if written per frame
                       (t16_of: frame-recorder's nested "state" (Mineflayer radians, cursor = crosshair, selected_slot =
                       held_slot) or older top-level fields, units as written).
Mineflayer yaw radians [0, 2pi) (0 = north / -z, +pi/2 = west / -x), pitch up-positive; log angles are Minecraft degrees:
yaw = (pi - radians(mc_yaw)) mod 2pi, pitch = -radians(mc_pitch); eye = feet + 1.62 (1.27 sneaking).
  python -m mineengine.cap_reader --test | python -m mineengine.cap_reader --root BATCH_DIR [--report OUT.json]
"""
import argparse
import bisect
import gzip
import json
import math
import os
import re
import shutil
import sys
import tempfile
from collections import Counter

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
try:
    from mineengine import cap_palette as CP  # noqa: E402  (block_id)
except ImportError:                                                             # item 1 absent: the 12 mc.py ids, else stone
    CP = None

MC12 = ["air", "grass_block", "stone", "dirt", "bedrock", "oak_planks", "cobblestone", "glass", "oak_log", "tnt", "red_wool", "blue_wool"]
AIR_STATE, EYE, EYE_SNEAK, TAU, JUMP_M = "minecraft:air", 1.62, 1.27, 2 * math.pi, 8.0
CAM_SNEAK_SCALE = 0.785                                                         # CameraManager.SNEAK_SCALE: the camera player's GENERIC_SCALE while its bot sneaks
BOTS = ("Alpha", "Bravo")
PRUNE = {"world_regions", "world", "world_nether", "world_the_end", "plugins", "libraries", "versions", "assets", "logs",
         "compose_configs", "__pycache__", "node_modules"}
_K = r"(?:^|_)(?P<eid>\d{6})_(?P<who>[A-Za-z][A-Za-z0-9]*)_instance_(?P<inst>\d{3})"
PATS = [("events", re.compile(_K + r"_world_events\.jsonl(?:\.gz)?$")), ("snap_start", re.compile(_K + r"_world_snapshot_start\.json(?:\.gz)?$")),
        ("snap_end", re.compile(_K + r"_world_snapshot_end\.json(?:\.gz)?$")), ("video_meta", re.compile(_K + r"_camera_meta\.json$")),
        ("video", re.compile(_K + r"_camera\.(?:mp4|mkv)$")), ("video", re.compile(_K + r"\.(?:mp4|mkv)$")),   # 2nd: prepare_train_dataset drops "_camera"
        ("info", re.compile(_K + r"_episode_info\.json$")),
        ("meta", re.compile(_K + r"_meta\.json$")), ("action", re.compile(_K + r"\.json$"))]
WORLD_KINDS = ("events", "snap_start", "snap_end")

ALIASES = {
    "swing": ("player_animation", "arm_swing", "swing", "player_swing", "hand_swing", "player_arm_swing", "animation", "player_animation_event"),
    "damage": ("entity_damage", "entity_damage_by_entity", "entity_damage_by_block", "damage", "player_damage", "entity_hurt", "player_hurt",
               "hurt", "entity_damaged"),
    "use_start": ("use_start", "item_use_start", "use_item_start", "start_using_item", "player_use_start", "using_start", "player_item_use_start",
                  "eat_start", "bow_draw_start", "shield_raise", "block_start"),
    "use_end": ("use_end", "item_use_end", "use_item_end", "use_item_stop", "stop_using_item", "player_use_end", "using_end", "player_item_use_end",
                "item_consume", "player_item_consume", "entity_shoot_bow", "shoot_bow", "bow_shoot", "eat_end", "release_use", "shield_lower"),
    "use": ("use", "item_use", "use_item", "player_use", "player_item_use", "player_use_item"),
    "held": ("item_held", "held_change", "held_item_change", "player_item_held", "hotbar_change", "held_slot_change", "slot_change", "held",
             "player_held_item"),
    "dig": ("dig_progress", "block_dig", "dig", "block_damage", "block_break_animation", "break_animation", "block_break_progress", "dig_start",
            "dig_abort", "dig_cancel", "dig_stop", "dig_finish", "dig_complete", "dig_end", "block_damage_abort", "dig_stage", "mining_progress"),
    "spawn": ("entity_spawn", "creature_spawn", "item_spawn", "entity_add", "spawn"),
    "remove": ("entity_remove", "entity_removed", "entity_despawn"),
    "death": ("entity_death", "player_death", "death", "entity_died", "player_died"),
    "explode": ("entity_explode", "block_explode", "explosion", "tnt_explode", "explode"),
    "prime": ("tnt_prime", "explosion_prime"),
    "place": ("block_place",), "break": ("block_break",), "respawn": ("player_respawn", "respawn")}
KIND_OF = {t: k for k, ts in ALIASES.items() for t in ts}
CAMERA_TYPES = ("camera_pose", "camera", "camera_state", "camera_location", "camera_tick", "spectator_pose")
ENTITY_LIST_TYPES = ("entities", "entity_tick", "entity_state", "entity_states", "entities_tick", "tick_entities")
BLOCK_TYPES = ("block_place", "block_break", "block_burn", "block_ignite", "block_fade", "block_form", "block_spread", "block_grow", "liquid_flow",
               "fluid_level_change", "block_explode", "entity_explode", "tnt_prime", "leaves_decay", "piston_extend", "piston_retract",
               "entity_change_block", "structure_grow", "block_fertilize", "sponge_absorb", "bucket_empty", "bucket_fill", "player_interact")
KNOWN = set(KIND_OF) | set(CAMERA_TYPES) | set(ENTITY_LIST_TYPES) | set(BLOCK_TYPES) | {
    "episode_begin", "tick", "note", "weather", "episode_end", "block_state", "entity_knockback"}
SIZES = {"player": (0.6, 1.8), "zombie": (0.6, 1.95), "husk": (0.6, 1.95), "drowned": (0.6, 1.95), "zombie_villager": (0.6, 1.95),
         "skeleton": (0.6, 1.99), "stray": (0.6, 1.99), "wither_skeleton": (0.7, 2.4), "creeper": (0.6, 1.7), "spider": (1.4, 0.9),
         "cave_spider": (0.7, 0.5), "enderman": (0.6, 2.9), "witch": (0.6, 1.95), "villager": (0.6, 1.95), "pillager": (0.6, 1.95),
         "vindicator": (0.6, 1.95), "iron_golem": (1.4, 2.7), "cow": (0.9, 1.4), "mooshroom": (0.9, 1.4), "pig": (0.9, 0.9),
         "sheep": (0.9, 1.3), "chicken": (0.4, 0.7), "horse": (1.3965, 1.6), "wolf": (0.6, 0.85), "cat": (0.6, 0.7), "rabbit": (0.4, 0.5),
         "fox": (0.6, 0.7), "bat": (0.5, 0.9), "slime": (0.52, 0.52), "item": (0.25, 0.25), "experience_orb": (0.5, 0.5),
         "tnt": (0.98, 0.98), "falling_block": (0.98, 0.98), "arrow": (0.5, 0.5), "spectral_arrow": (0.5, 0.5), "trident": (0.5, 0.5),
         "snowball": (0.25, 0.25), "egg": (0.25, 0.25), "ender_pearl": (0.25, 0.25), "potion": (0.25, 0.25), "fireball": (1.0, 1.0),
         "small_fireball": (0.3125, 0.3125), "boat": (1.375, 0.5625), "minecart": (0.98, 0.7), "armor_stand": (0.5, 1.975)}


# ---- small helpers --------------------------------------------------------------------------------------------------
def bare(s):
    """'minecraft:oak_stairs[facing=east]' -> 'oak_stairs' (None / '' -> '')."""
    return str(s or "").split("[", 1)[0].split(":", 1)[-1].strip().lower()


def norm_state(s):
    s = str(s or "").strip()
    if not s:
        return AIR_STATE
    return s if ":" in s.split("[", 1)[0] else "minecraft:" + s


def block_id(state):
    if CP is not None:
        return int(CP.block_id(state))
    b = bare(state)
    return 0 if b in ("air", "cave_air", "void_air", "") else MC12.index(b) if b in MC12 else 2


def item_name(v):
    """held item (str, prismarine / Bukkit dict, None) -> bare name or None for empty / air."""
    if isinstance(v, dict):
        v = v.get("name") or v.get("type") or v.get("id") or v.get("item")
    b = bare(v) if isinstance(v, str) else None
    return None if b in (None, "", "air", "cave_air", "void_air") else b


def hotbar9(h):
    """hotbar in any of the plausible T16 / inventory forms (9-list, list of items with slot 36-44 or 0-8, {slot: item}) ->
    [(name or None, count)] * 9, or None."""
    if h is None:
        return None
    out = [(None, 0)] * 9
    it = h.items() if isinstance(h, dict) else enumerate(h) if isinstance(h, list) else ()
    for k, v in it:
        s = v.get("slot") if isinstance(v, dict) and v.get("slot") is not None else k
        try:
            s = int(s)
        except (TypeError, ValueError):
            continue
        s = s - 36 if 36 <= s <= 44 else s
        if 0 <= s < 9:
            nm = item_name(v)
            out[s] = (nm, int(v.get("count", 1) or 1) if isinstance(v, dict) and nm else 1 if nm else 0)
    return out


def mc_to_mineflayer(yaw_deg, pitch_deg):
    """Minecraft / F3 degrees (yaw 0 = south, 90 = west; pitch +90 = down) -> Mineflayer radians (yaw [0, 2pi), pitch up+)."""
    y = np.mod(math.pi - np.radians(np.asarray(yaw_deg, np.float64)), TAU)
    p = -np.radians(np.asarray(pitch_deg, np.float64))
    return (float(y), float(p)) if y.ndim == 0 else (y, p)


def mineflayer_to_mc(yaw, pitch):
    y = np.mod(np.degrees(math.pi - np.asarray(yaw, np.float64)) + 180.0, 360.0) - 180.0
    p = -np.degrees(np.asarray(pitch, np.float64))
    return (float(y), float(p)) if y.ndim == 0 else (y, p)


def _lerp_ang(a, b, w):
    return (a + w * (((b - a + math.pi) % TAU) - math.pi)) % TAU


def _pos(e):
    if not isinstance(e, dict):
        return None
    p = e.get("pos", e.get("location", e.get("loc")))
    if isinstance(p, dict):
        p = [p.get("x"), p.get("y"), p.get("z")]
    if p is None and all(k in e for k in "xyz"):
        p = [e["x"], e["y"], e["z"]]
    try:
        return np.asarray(p, np.float64)[:3] if p is not None and len(p) >= 3 and None not in list(p)[:3] else None
    except (TypeError, ValueError):
        return None


def _who(v):
    if isinstance(v, dict):
        v = v.get("name") or v.get("player") or v.get("uuid")
    return None if v is None else str(v)


def _first(e, keys):
    for k in keys:
        if e.get(k) is not None:
            return e[k]
    return None


def _open(path):
    with open(path, "rb") as fh:
        gz = fh.read(2) == b"\x1f\x8b"
    return gzip.open(path, "rt", encoding="utf-8") if gz else open(path, "r", encoding="utf-8")


def load_json(path):
    with _open(path) as fh:
        return json.load(fh)


# ---- files ----------------------------------------------------------------------------------------------------------
def episodes(root):
    """Every episode under root -> [dict(eid, inst, base, bots, files, dupes)], sorted by (base, eid, inst). Files of one
    episode share a base dir (output/ and aligned/ map to their parent); duplicates keep the first sorted path."""
    groups, r = {}, os.path.normpath(root)
    roots = [r] + ([os.path.join(os.path.dirname(r), "aligned")] if os.path.basename(r) == "output" and os.path.isdir(os.path.join(os.path.dirname(r), "aligned")) else [])
    for d, sub, fs in (w for r_ in roots for w in os.walk(r_)):
        sub[:] = sorted(s for s in sub if s not in PRUNE and not s.startswith(".") and not (os.path.basename(d) == "camera" and s.startswith("data_")))
        base = os.path.dirname(d) if os.path.basename(d) in ("output", "aligned") else d
        for f in sorted(fs):
            for kind, rx in PATS:
                m = rx.search(f)
                if m:
                    if (kind in WORLD_KINDS) == (m["who"] == "world"):
                        g = groups.setdefault((base, int(m["eid"]), int(m["inst"])), {})
                        g.setdefault(kind if kind in WORLD_KINDS else f"{kind}_{m['who']}", []).append(os.path.join(d, f))
                    break
    out = []
    for (base, eid, inst), g in sorted(groups.items()):
        bots = sorted({k.split("_", 1)[1] for k in g if k.startswith(("action_", "info_", "meta_"))} | set(BOTS))
        files = {k: None for k in WORLD_KINDS}
        for b in bots:
            files.update({f"{k}_{b}": None for k in ("action", "meta", "info", "video", "video_meta")})
        dupes = {}
        for k, v in g.items():
            v = sorted(v)
            if k.startswith(("video_", "video_meta_")) and g.get("action_" + k.rsplit("_", 1)[1]):
                stem = os.path.basename(sorted(g["action_" + k.rsplit("_", 1)[1]])[0])[:-5]
                v = sorted(v, key=lambda p: not os.path.basename(p).startswith(stem + "_camera"))
            files[k] = v[0]
            if len(v) > 1:
                dupes[k] = v[1:]
        out.append(dict(eid=eid, inst=inst, base=base, bots=[b for b in bots if files.get("action_" + b) or b in BOTS], files=files, dupes=dupes))
    return out


def decode_snapshot(s):
    sx, sy, sz = (int(v) for v in s["size"])
    rle = np.asarray(s["rle"], np.int64).reshape(-1)
    if rle.size % 2 or (rle[0::2] < 0).any():
        raise ValueError("snapshot rle: odd length or negative run")
    flat = np.repeat(rle[1::2], rle[0::2])
    pal = list(s["palette"])
    if flat.size != sx * sy * sz:
        raise ValueError(f"snapshot rle decodes to {flat.size} voxels, size {sx}x{sy}x{sz} = {sx * sy * sz}")
    if flat.size and (flat.min() < 0 or flat.max() >= len(pal)):
        raise ValueError("snapshot rle index outside the palette")
    order = str(s.get("order") or "yzx").lower()
    if sorted(order) != ["x", "y", "z"]:
        raise ValueError(f"snapshot order {order!r}")
    dim = dict(x=sx, y=sy, z=sz)
    g = flat.astype(np.int32).reshape([dim[a] for a in order]).transpose([order.index(a) for a in "xyz"])
    out = {k: v for k, v in s.items() if k not in ("rle", "biomes_rle")}
    out.update(origin=np.asarray(s["origin"], np.int64)[:3], size=(sx, sy, sz), grid=np.ascontiguousarray(g), palette=pal,
               entities=list(s.get("entities") or []), order=order)
    if s.get("biomes_rle"):
        b = np.asarray(s["biomes_rle"], np.int64).reshape(-1)
        b = np.repeat(b[1::2], b[0::2])
        if b.size == sx * sz:
            out["biomes"] = np.ascontiguousarray(b.astype(np.int32).reshape(sz, sx).T)  # [sx, sz], "zx" order (x fastest)
    return out


def load_snapshot(path):
    """..._world_snapshot_{start,end}.json.gz -> dict(origin, size, grid int32 [sx, sy, sz], palette, entities, + header)."""
    return decode_snapshot(load_json(path))


def load_events(path, stats=None):
    """world_events.jsonl -> records sorted by seq (stable; a record without seq stays after its predecessor). Unparseable
    lines (an interrupted run's last line) are skipped and counted in stats['bad_lines']."""
    ev, keys, bad, last = [], [], 0, -1.0
    with _open(path) as fh:
        for i, line in enumerate(fh):
            line = line.strip()
            if not line:
                continue
            try:
                e = json.loads(line)
            except json.JSONDecodeError:
                bad += 1
                continue
            if not isinstance(e, dict):
                bad += 1
                continue
            s = e.get("seq")
            k = float(s) if isinstance(s, (int, float)) else last + 0.5
            last = k if isinstance(s, (int, float)) else last
            ev.append(e); keys.append((k, i))
    order = sorted(range(len(ev)), key=keys.__getitem__)
    ev = [ev[i] for i in order]
    if stats is not None:
        seqs = [e.get("seq") for e in ev if isinstance(e.get("seq"), (int, float))]
        stats.update(n=len(ev), bad_lines=bad, dup_seq=len(seqs) - len(set(seqs)), out_of_order=sum(1 for a, b in zip(order, order[1:]) if b < a))
    return ev


def load_actions(path):
    a = load_json(path)
    if isinstance(a, dict):
        a = next((a[k] for k in ("frames", "actions", "samples", "data") if isinstance(a.get(k), list)), [])
    return a


def _ftime(f):
    """-> (host-clock ms, source) of one action entry: renderTime when it is epoch ms (>= 1e12, act_recorder's rule), else
    epochTime * 1000 (act_recorder synthesises it from its own clock when renderTime is relative), else renderTime as is."""
    r, e = f.get("renderTime"), f.get("epochTime")
    if r is not None and float(r) >= 1e12:
        return float(r), "renderTime"
    if e is not None:
        return round(float(e) * 1000.0, 3), "epochTime"
    return (float(r), "relative_renderTime") if r is not None else (np.nan, "none")


def frame_times(actions):
    """Per action entry the host-clock time in ms (_ftime: epoch renderTime, else epochTime * 1000)."""
    return np.array([_ftime(f)[0] for f in actions], np.float64)


def frame_time_source(actions):
    """'renderTime' | 'epochTime' | 'relative_renderTime' | 'none' | 'mixed:<counts>' for a whole action file."""
    c = Counter(_ftime(f)[1] for f in actions)
    return next(iter(c)) if len(c) == 1 else "mixed:" + ",".join(f"{k}={v}" for k, v in sorted(c.items()))


ACTION_STD = {"x", "y", "z", "yaw", "pitch", "yaw_mc_deg", "pitch_mc_deg", "action", "inventory", "renderTime", "physicsTime", "worldAgeTicks",
              "isInWater", "frame_count", "episode_id", "relativeTimeMs", "epochTime", "extra_info"}
T16_TOP = ("food", "saturation", "health", "body_yaw", "head_yaw", "crosshair", "hotbar", "held_slot", "selected_slot", "offhand", "alive")
T16_RENAME = {"cursor": "crosshair", "selected_slot": "held_slot"}


def t16_of(a):
    """the T16 fields of one action entry -> (dict, angle units): frame-recorder nests them under "state" (health, food,
    saturation, hotbar [{slot 0-8, id, count}], selected_slot -> held_slot, offhand, armor, effects, oxygen, xp_*, flying,
    in_water, alive, head_yaw = the sent yaw in Mineflayer radians, body_yaw always null (dropped), cursor {block, entity}
    -> crosshair, cursor_at_action) -> units "rad"; older layouts write them top level (units as written: None = detect)."""
    st = a.get("state")
    if isinstance(st, dict):
        out = {T16_RENAME.get(k, k): v for k, v in st.items() if v is not None}
        if not isinstance(out.get("crosshair"), dict):
            out.pop("crosshair", None)
        return out, "rad"
    return {k: a[k] for k in T16_TOP if a.get(k) is not None}, None


def action_state(a):
    """One per-frame action entry -> dict(t, pos, eye, yaw, pitch (absolute Mineflayer rad, what the camera renders), sneaking,
    keys (the action dict), held_item, held_slot, offhand, hotbar9, t16_units) + every non-schema top-level field as written
    + the nested T16 "state" fields (t16_of; top-level fields win). The eye height follows action.sneak (the pose link's
    datagram flag: the camera renders feet + 1.27 while it is set); sneaking = that or state.sneaking."""
    inv, act = a.get("inventory") or {}, a.get("action") or {}
    pos = np.array([a["x"], a["y"], a["z"]], np.float64) if all(a.get(k) is not None for k in "xyz") else None
    sneak = bool(a.get("sneaking", act.get("sneak")))
    out = {k: v for k, v in a.items() if k not in ACTION_STD}
    t16, units = t16_of(a)
    if units is not None:
        for k, v in t16.items():
            out.setdefault(k, v)
    slot = inv.get("quickBarSlot", _first(a, ("held_slot", "selected_slot")))
    hb = a["hotbar"] if a.get("hotbar") is not None else inv.get("hotbar") if inv.get("hotbar") is not None else t16.get("hotbar") if units else None
    out.update(t=frame_times([a])[0], pos=pos, eye=None if pos is None else pos + [0.0, EYE_SNEAK if sneak else EYE, 0.0],
               yaw=None if a.get("yaw") is None else float(a["yaw"]), pitch=None if a.get("pitch") is None else float(a["pitch"]),
               sneaking=sneak or bool(units and t16.get("sneaking")), keys=act,
               held_item=item_name(inv["heldItem"]) if "heldItem" in inv else item_name(a.get("held_item")),
               held_slot=slot if slot is not None else t16.get("held_slot") if units else None,
               offhand=item_name(inv["offHandItem"]) if inv.get("offHandItem") is not None else item_name(t16.get("offhand")) if units else None,
               hotbar9=hotbar9(hb), t16_units=units)
    return out


# ---- blocks ---------------------------------------------------------------------------------------------------------
def _pseudo_states(events):
    """Fallback when a log has no block_state records: the new states of the block events (broken / exploded -> air)."""
    out = []
    for e in events:
        ty, t = e.get("type"), e.get("epoch_ms")
        if t is None:
            continue
        if ty in ("block_explode", "entity_explode", "structure_grow", "block_fertilize", "sponge_absorb"):
            for b in e.get("blocks") or []:
                st = b.get("new_state") or (AIR_STATE if ty.endswith("explode") else None)
                if st and b.get("pos") is not None:
                    out.append(dict(pos=b["pos"], state=st, epoch_ms=t, seq=e.get("seq")))
        elif ty in BLOCK_TYPES and e.get("pos") is not None:
            st = e.get("new_state") or e.get("new_type") or (AIR_STATE if ty in ("block_break", "block_burn") else None)
            if st:
                out.append(dict(pos=e["pos"], state=st, epoch_ms=t, seq=e.get("seq")))
    return out


class BlockTimeline:
    """Exact block state over time: snapshot + block_state records in seq order, ids in cap_palette ids (uint16)."""

    def __init__(self, snap, events, fallback=True, lead_ms=0.0):
        self.lead_ms = float(lead_ms)
        self.origin, self.size = np.asarray(snap["origin"], np.int64), tuple(int(v) for v in snap["size"])
        sx, sy, sz = self.size
        self.palette, self._pi = [norm_state(p) for p in snap["palette"]], {}
        for i, p in enumerate(self.palette):
            self._pi.setdefault(p, i)
        self.lut = [block_id(p) for p in self.palette]
        self.sgrid0 = np.ascontiguousarray(snap["grid"], np.int32)
        self.ids0 = np.asarray(self.lut, np.uint16)[self.sgrid0]
        self.n_palette0 = len(self.palette)
        recs = [e for e in events if e.get("type") == "block_state"]
        self.source = "block_state"
        if not recs and fallback:
            recs = _pseudo_states(events); self.source = "events" if recs else "none"
        b = next((e.get("box") for e in events if e.get("type") == "episode_begin" and isinstance(e.get("box"), dict)), None)
        self.box_mismatch = bool(b and (list(b.get("min", [])) != self.origin.tolist()
                                        or [int(v1) - int(v0) + 1 for v0, v1 in zip(b["min"], b["max"])] != list(self.size)))
        cur_s, cur_i = self.sgrid0.copy(), self.ids0.copy()
        self.changes, self.outside = [], []
        self.n_outside = self.n_nonmono = self.n_prev_mismatch = self.n_bad = 0
        tmax = -math.inf
        for e in recs:
            p, t = e.get("pos"), e.get("epoch_ms")
            if p is None or t is None or len(p) < 3:
                self.n_bad += 1
                continue
            x, y, z = (int(math.floor(float(v))) for v in p[:3])
            st = norm_state(e.get("state") or e.get("block") or AIR_STATE)
            t = float(t)
            if t < tmax:
                self.n_nonmono += 1
            tmax = max(tmax, t)
            i, j, k = x - int(self.origin[0]), y - int(self.origin[1]), z - int(self.origin[2])
            if not (0 <= i < sx and 0 <= j < sy and 0 <= k < sz):
                self.n_outside += 1; self.outside.append(dict(t=tmax, t_raw=t, seq=e.get("seq"), pos=(x, y, z), state=st))
                continue
            if st not in self._pi:
                self._pi[st] = len(self.palette); self.palette.append(st); self.lut.append(block_id(st))
            pi = self._pi[st]
            ps, pid = int(cur_s[i, j, k]), int(cur_i[i, j, k])
            if e.get("prev_state") is not None and norm_state(e["prev_state"]) != self.palette[ps]:
                self.n_prev_mismatch += 1
            nid = self.lut[pi]
            self.changes.append(dict(t=tmax, t_raw=t, seq=e.get("seq"), pos=(x, y, z), idx=(i, j, k), state=st, pal=pi, id=nid,
                                     prev_state=self.palette[ps], prev_id=pid, changed=nid != pid, state_changed=pi != ps))
            cur_s[i, j, k], cur_i[i, j, k] = pi, nid
        c = self.changes
        self.ct = np.array([r["t"] for r in c], np.float64) - self.lead_ms
        self.cflat = np.array([(r["idx"][0] * sy + r["idx"][1]) * sz + r["idx"][2] for r in c], np.int64)
        self.cpal = np.array([r["pal"] for r in c], np.int32)
        self.cid = np.array([r["id"] for r in c], np.uint16)
        self.cver = np.cumsum([r["changed"] for r in c]).astype(np.int64)
        self.csver = np.cumsum([r["state_changed"] for r in c]).astype(np.int64)
        self._ids, self._s, self._n = self.ids0.copy(), self.sgrid0.copy(), 0

    def _apply(self, ids, s, a, n):
        f = self.cflat[a:n]
        if len(f):
            _, last = np.unique(f[::-1], return_index=True)
            sel = a + (len(f) - 1 - last)                                       # last write per voxel wins (seq order)
            ids.reshape(-1)[self.cflat[sel]] = self.cid[sel]; s.reshape(-1)[self.cflat[sel]] = self.cpal[sel]

    def _seek(self, t):
        n = int(np.searchsorted(self.ct, t, side="right"))
        if n < self._n:
            np.copyto(self._ids, self.ids0); np.copyto(self._s, self.sgrid0); self._n = 0
        if n > self._n:
            self._apply(self._ids, self._s, self._n, n); self._n = n
        return n

    def grid_at(self, t_ms, copy=False):
        """-> (ids uint16 [sx, sy, sz], version). The array is the timeline's buffer (valid until the next call) unless copy."""
        n = self._seek(t_ms)
        return (self._ids.copy() if copy else self._ids), (int(self.cver[n - 1]) if n else 0)

    def states_at(self, t_ms, copy=False):
        """-> (palette indices int32 [sx, sy, sz] into self.palette, state version)."""
        n = self._seek(t_ms)
        return (self._s.copy() if copy else self._s), (int(self.csver[n - 1]) if n else 0)

    def final(self):
        """-> (ids, states) after every record, fresh arrays."""
        ids, s = self.ids0.copy(), self.sgrid0.copy()
        self._apply(ids, s, 0, len(self.ct))
        return ids, s

    def changes_between(self, t0, t1):
        """Records becoming visible in (t0, t1]."""
        return self.changes[int(np.searchsorted(self.ct, t0, side="right")):int(np.searchsorted(self.ct, t1, side="right"))]

    def compare_end(self, end, max_examples=10):
        """Replay vs end snapshot: voxels whose state string / cap id differ (verify_world_events' replay_mismatch)."""
        if tuple(int(v) for v in end["size"]) != self.size or not np.array_equal(np.asarray(end["origin"], np.int64), self.origin):
            return dict(error="start / end boxes differ")
        ids, s = self.final()
        canon = np.array([self._pi[p] for p in self.palette], np.int64)
        epal = [norm_state(p) for p in end["palette"]]
        emap = np.array([self._pi.get(p, -1) for p in epal], np.int64)
        eg = np.asarray(end["grid"]).reshape(-1)
        bad = np.nonzero(canon[s.reshape(-1)] != emap[eg])[0]
        bad_id = int((ids.reshape(-1) != np.array([block_id(p) for p in epal], np.uint16)[eg]).sum())
        ex = [dict(pos=(np.array(np.unravel_index(i, self.size)) + self.origin).tolist(), replayed=self.palette[s.reshape(-1)[i]],
                   end_snapshot=epal[eg[i]]) for i in bad[:max_examples]]
        return dict(mismatch_states=int(len(bad)), mismatch_ids=bad_id, examples=ex)


# ---- players --------------------------------------------------------------------------------------------------------
def _plist(v):
    if isinstance(v, dict):
        return [dict(p, name=p.get("name", k)) for k, p in v.items() if isinstance(p, dict)]
    return [p for p in (v or []) if isinstance(p, dict)]


LIST_KEYS = ("entities", "mobs", "monsters")                                    # T3 "entities" records / tick lists, T19 per-tick mobs


def _has_list(e):
    return any(isinstance(e.get(k), list) for k in LIST_KEYS)


def _elist(e):
    return [x for k in LIST_KEYS if isinstance(e.get(k), list) for x in e[k] if isinstance(x, dict)]


def _etype(e):
    return bare(e.get("type") or e.get("kind") or e.get("entity_type") or "unknown")


STD_PLAYER = {"name", "player", "world", "pos", "x", "y", "z", "yaw", "pitch", "location", "loc", "type", "kind"}
ANG_EXTRA = ("body_yaw", "head_yaw")


def _prec(p):
    r = {k: v for k, v in p.items() if k not in STD_PLAYER and k not in ANG_EXTRA}
    if "held_item" in r:
        r["held_item"] = item_name(r["held_item"])
    for k in ("health", "food", "saturation"):
        if isinstance(r.get(k), (int, float)):
            r[k] = float(r[k])
    if r.get("alive") is None and isinstance(r.get("health"), float):
        r["alive"] = r["health"] > 0
    if "hotbar" in r:
        r["hotbar9"] = hotbar9(r["hotbar"])
    return r


def _ffill(a):
    a = np.array(a, np.float64)
    ok = np.isfinite(a) if a.ndim == 1 else np.isfinite(a).all(1)
    if not ok.any():
        return a
    idx = np.where(ok, np.arange(len(a)), 0)
    np.maximum.accumulate(idx, out=idx)
    idx[:np.argmax(ok)] = np.argmax(ok)
    return a[idx]


class PlayerTrack:
    """Per tracked player, from tick records (players from per-tick entity lists only when a name has no tick entries)."""

    def __init__(self, events):
        tick, ent = {}, {}
        for e in events:
            t, ty = e.get("epoch_ms"), e.get("type")
            if t is None:
                continue
            if ty == "tick":
                for p in _plist(e.get("players")):
                    nm = p.get("name") or p.get("player")
                    if nm:
                        tick.setdefault(str(nm), []).append((float(t), p))
            if ty == "tick" or ty in ENTITY_LIST_TYPES:
                for p in _elist(e):
                    if _etype(p) == "player" and p.get("name"):
                        ent.setdefault(str(p["name"]), []).append((float(t), p))
        for nm, v in ent.items():
            tick.setdefault(nm, v)
        self.tracks, self.units = {}, {}
        for nm, rows in tick.items():
            rows = [r for r in sorted(rows, key=lambda r: r[0]) if _pos(r[1]) is not None]
            if not rows:
                continue
            T = np.maximum.accumulate(np.array([r[0] for r in rows], np.float64))
            P = np.array([_pos(r[1]) for r in rows])
            yd = _ffill([r[1].get("yaw", np.nan) if r[1].get("yaw") is not None else np.nan for r in rows])
            pd = _ffill([r[1].get("pitch", np.nan) if r[1].get("pitch") is not None else np.nan for r in rows])
            Y, PI = mc_to_mineflayer(np.nan_to_num(yd), np.nan_to_num(pd))
            tr = dict(t=T, pos=P, yaw=np.atleast_1d(Y), pitch=np.atleast_1d(PI), rec=[_prec(r[1]) for r in rows])
            for f in ANG_EXTRA:                                                   # world log: MC degrees (getBodyYaw / Location yaw), never radians
                v = np.array([r[1][f] if isinstance(r[1].get(f), (int, float)) else np.nan for r in rows], np.float64)
                if not np.isfinite(v).any():
                    continue
                self.units.setdefault(nm, {})[f] = "deg"
                tr[f] = np.mod(math.pi - np.radians(_ffill(v)), TAU)
            self.tracks[nm] = tr

    @property
    def names(self):
        return sorted(self.tracks)

    def __contains__(self, name):
        return name in self.tracks

    def span(self, name):
        t = self.tracks[name]["t"]
        return float(t[0]), float(t[-1])

    def at(self, name, t_ms):
        """-> dict(pos, eye, yaw, pitch (Mineflayer rad), health, held_item (bare name / None), held_slot, sneaking, sprinting,
        on_ground, alive, + any extra fields of the record (food, saturation, body_yaw, head_yaw, hotbar / hotbar9, ...),
        t_rec) or None for an unknown name. Pose interpolated, the rest from the last record at or before t."""
        tr = self.tracks.get(name)
        if tr is None:
            return None
        T, n = tr["t"], len(tr["t"])
        k = int(np.searchsorted(T, t_ms, side="right")) - 1
        k0, k1 = min(max(k, 0), n - 1), min(max(k + 1, 0), n - 1)
        w = 0.0 if k < 0 or k1 == k0 or T[k1] <= T[k0] else min(max((t_ms - T[k0]) / (T[k1] - T[k0]), 0.0), 1.0)
        if np.linalg.norm(tr["pos"][k1] - tr["pos"][k0]) > JUMP_M or bool(tr["rec"][k0].get("dead")) != bool(tr["rec"][k1].get("dead")):
            w = 0.0                                                             # teleport / respawn: hold
        out = dict(tr["rec"][k0])
        pos = tr["pos"][k0] + w * (tr["pos"][k1] - tr["pos"][k0])
        sneak = bool(out.get("sneaking"))
        out.update(name=name, pos=pos, eye=pos + [0.0, EYE_SNEAK if sneak else EYE, 0.0], yaw=float(_lerp_ang(tr["yaw"][k0], tr["yaw"][k1], w)),
                   pitch=float(tr["pitch"][k0] + w * (tr["pitch"][k1] - tr["pitch"][k0])), t_rec=float(T[k0]),
                   sneaking=sneak, sprinting=bool(out.get("sprinting")), on_ground=bool(out.get("on_ground", True)))
        for k_ in ("health", "held_item", "held_slot"):
            out.setdefault(k_, None)
        for f in ANG_EXTRA:
            if f in tr:
                out[f] = float(_lerp_ang(tr[f][k0], tr[f][k1], w))
        return out


def _sneak_series(events, name):
    """tick records -> (t [N], sneaking bool [N]) of one player (empty arrays if never listed)."""
    T, S = [], []
    for e in events:
        if e.get("type") == "tick" and e.get("epoch_ms") is not None:
            for p in _plist(e.get("players")):
                if (p.get("name") or p.get("player")) == name:
                    T.append(float(e["epoch_ms"])); S.append(bool(p.get("sneaking")))
    return np.maximum.accumulate(np.array(T, np.float64)) if T else np.zeros(0), np.array(S, bool)


def camera_names(events, name):
    """Names of `name`'s camera player: episode_begin "cameras" ({bot: cam} or [{bot / follows, camera / name}]), else the
    usual spellings (CameraAlpha, camera_alpha, CamAlpha, AlphaCamera; case-insensitive)."""
    out = set()
    for e in events:
        if e.get("type") == "episode_begin":
            c = e.get("cameras")
            if isinstance(c, dict):
                out |= {str(v).lower().replace("_", "") for k, v in c.items() if str(k) == name and v}
            elif isinstance(c, list):
                out |= {str(x.get("camera") or x.get("name")).lower().replace("_", "") for x in c if isinstance(x, dict) and name in (x.get("bot"), x.get("follows"), x.get("player"))}
            break
    b = name.lower()
    return out | {f"camera{b}", f"cam{b}", f"{b}camera", f"{b}cam"}


def camera_track(events, name, pos_is="feet"):
    """T12 camera-pose records following `name` -> (t_ms [N], eye [N, 3], yaw [N], pitch [N]) Mineflayer radians, or None.
    A record may hold one camera (type camera_pose / camera / camera_state ...) or a list "cameras"; tick records with
    "cameras" / "camera" count too. Matching: name / player / bot / target ... == name, else name contained (case-insensitive).
    Without such records, the camera player's own entry in the tick "players" list (camera_names) is used (the plugin logs
    every player of `worldstate begin players=`). Eye: the record's eye / eye_pos as is; else pos (camera.getLocation() =
    the camera player's feet; pos_is="eye": already the eye) + eye_height, or 1.62 x scale, else 1.62 x 0.785 while the
    followed bot's last tick record says sneaking (CameraManager shrinks the camera with GENERIC_SCALE 0.785), else 1.62."""
    rows, trows, cn = [], [], camera_names(events, name)
    st, ss = _sneak_series(events, name)

    def height(c, t):
        if c.get("eye_height") is not None:
            return float(c["eye_height"])
        if c.get("scale") is not None:
            return EYE * float(c["scale"])
        k = int(np.searchsorted(st, t, side="right")) - 1
        return EYE * (CAM_SNEAK_SCALE if k >= 0 and ss[k] else 1.0)

    def row(c, t):
        eye = c.get("eye") or c.get("eye_pos")
        p = np.asarray(eye, np.float64)[:3] if eye is not None else _pos(c)
        if p is None or c.get("yaw") is None or c.get("pitch") is None:
            return None
        if eye is None and pos_is != "eye":
            p = p + [0.0, height(c, t), 0.0]
        return (t, p, float(c["yaw"]), float(c["pitch"]))

    for e in events:
        ty, t = e.get("type"), e.get("epoch_ms")
        if t is None:
            continue
        t = float(t)
        if ty in CAMERA_TYPES:
            cams = e["cameras"] if isinstance(e.get("cameras"), list) else [e]
        elif ty == "tick":
            cams = list(e["cameras"]) if isinstance(e.get("cameras"), list) else [e["camera"]] if isinstance(e.get("camera"), dict) else []
            for p in _plist(e.get("players")):
                if isinstance(p.get("camera"), dict):
                    cams.append(dict(p["camera"], follows=p.get("name")))
                elif p.get("camera_yaw") is not None:
                    cams.append(dict(follows=p.get("name"), pos=p.get("camera_pos") or p.get("pos"), eye=p.get("camera_eye"), yaw=p["camera_yaw"],
                                     pitch=p.get("camera_pitch")))
                elif str(p.get("name") or p.get("player") or "").lower().replace("_", "") in cn:
                    r = row(p, t)
                    if r is not None:
                        trows.append(r)
        else:
            continue
        for c in cams:
            if not isinstance(c, dict):
                continue
            who = [_who(c.get(k)) for k in ("name", "player", "bot", "target", "follows", "follow", "subject", "for", "of", "camera", "owner")]
            who = [w for w in who if w]
            if name not in who and not any(name.lower() in w.lower() for w in who):
                continue
            r = row(c, t)
            if r is not None:
                rows.append(r)
    rows = rows or trows
    if not rows:
        return None
    rows.sort(key=lambda r: r[0])
    y, p = mc_to_mineflayer([r[2] for r in rows], [r[3] for r in rows])
    return (np.maximum.accumulate(np.array([r[0] for r in rows])), np.array([r[1] for r in rows]), np.atleast_1d(y), np.atleast_1d(p))


def camera_at(track, ts, mode="linear"):
    """camera_track output sampled at times ts (ms) -> (eye [N, 3], yaw [N], pitch [N]); mode linear or hold (last <= t)."""
    t, eye, yaw, pitch = track
    ts = np.atleast_1d(np.asarray(ts, np.float64))
    k = np.clip(np.searchsorted(t, ts, side="right") - 1, 0, len(t) - 1); k1 = np.minimum(k + 1, len(t) - 1)
    dt = t[k1] - t[k]
    w = np.zeros_like(ts) if mode == "hold" else np.where(dt > 0, np.clip((ts - t[k]) / np.where(dt > 0, dt, 1), 0, 1), 0.0)
    w = np.where(ts < t[0], 0.0, w)
    return (eye[k] + w[:, None] * (eye[k1] - eye[k]), _lerp_ang(yaw[k], yaw[k1], w), pitch[k] + w * (pitch[k1] - pitch[k]))


# ---- events ---------------------------------------------------------------------------------------------------------
ACTOR = ("player", "name", "digger", "breaker", "actor", "bot", "entity", "entity_name", "source_player")
VICTIM = ("victim", "entity", "target", "damaged", "player", "name")
ATTACKER = ("attacker", "damager", "source_entity", "causing_entity", "direct_entity", "killer", "by", "shooter")


def _phase(ty, e):
    for tok, ph in (("start", "start"), ("begin", "start"), ("abort", "abort"), ("cancel", "abort"), ("stop", "abort"),
                    ("finish", "finish"), ("complete", "finish"), ("done", "finish"), ("end", "finish")):
        if ty.endswith("_" + tok):
            return ph
    v = _first(e, ("phase", "status", "action", "event"))
    if isinstance(v, str):
        v = v.lower()
        for tok, ph in (("start", "start"), ("begin", "start"), ("abort", "abort"), ("cancel", "abort"), ("stop", "abort"),
                        ("finish", "finish"), ("complete", "finish"), ("done", "finish"), ("end", "finish"), ("progress", "progress")):
            if tok in v:
                return ph
    return "start" if ty == "block_damage" else None


class EventIndex:
    """Normalised events per (kind, name) with time lookups. Every event: kind, type, t (epoch_ms), seq, game_time,
    server_tick, name (indexing actor), raw, + kind fields (damage: victim, attacker, damage, cause, health_after,
    knockback; held: old_slot, new_slot, item; use*: item, hand; dig: pos, stage, phase (start / progress / abort / finish /
    clear); spawn / remove / death: uuid, etype, pos, killer, cause; explode: center, n_blocks; place / break: pos, state)."""

    def __init__(self, events):
        self.type_counts, self.unknown_types, self.counts = Counter(), Counter(), Counter()
        self._ix, seen, self.uuid_name = {}, {}, {}
        for e in events:                                                        # player uuid -> name (victims given by uuid only)
            ps = _plist(e.get("players")) + [x for x in _elist(e) if _etype(x) == "player"] \
                if e.get("type") == "tick" or e.get("type") in ENTITY_LIST_TYPES else []
            ps += [x for x in (e.get(f) for f in ("victim", "attacker", "damager", "entity", "killer")) if isinstance(x, dict)]
            for x in ps:
                if x.get("uuid") and x.get("name"):
                    self.uuid_name[str(x["uuid"])] = str(x["name"])

        def who(v):
            w = _who(v)
            return self.uuid_name.get(w, w) if w else w

        for e in events:
            ty = str(e.get("type"))
            self.type_counts[ty] += 1
            if ty not in KNOWN:
                self.unknown_types[ty] += 1
                continue
            k, t = KIND_OF.get(ty), e.get("epoch_ms")
            if k is None or t is None:
                continue
            ev = dict(kind=k, type=ty, t=float(t), seq=e.get("seq"), game_time=e.get("game_time"), server_tick=e.get("server_tick"), raw=e)
            names = []
            if k == "swing":
                an = str(_first(e, ("animation", "animation_type", "anim", "hand")) or "ARM_SWING").upper()
                if "SWING" not in an and ty in ("player_animation", "animation", "player_animation_event"):
                    ev["kind"] = k = "animation"
                ev["hand"] = "off" if "OFF" in an else "main"
                names = [who(_first(e, ACTOR))]
            elif k == "damage":
                vk = next((f for f in VICTIM if e.get(f) is not None), None)
                vic, att = (e[vk] if vk else None), _first(e, ATTACKER)
                if att is None and vk in ("victim", "entity", "target", "damaged"):  # WorldStateLogger.actor() fields for the damager
                    att = e.get("player") or e.get("entity_uuid")
                    vu = vic.get("uuid") if isinstance(vic, dict) else None
                    if att is not None and (who(att) == who(vic) or (vu is not None and str(att) == str(vu))):
                        att = None                                              # actor() of the victim itself: no damager
                ev.update(victim=who(vic), attacker=who(att) if isinstance(att, (dict, str)) else None,
                          victim_uuid=vic.get("uuid") if isinstance(vic, dict) else e.get("victim_uuid"),
                          attacker_uuid=att.get("uuid") if isinstance(att, dict) else e.get("attacker_uuid") or e.get("entity_uuid"),
                          attacker_type=bare(att.get("type")) if isinstance(att, dict) and att.get("type") else bare(e.get("entity_type")) or None,
                          damage=_first(e, ("final_damage", "damage", "amount", "final", "dmg")), cause=_first(e, ("cause", "damage_cause", "damage_type")),
                          health_after=_first(e, ("health_after", "victim_health", "hp_after", "new_health", "health")),
                          knockback=_first(e, ("knockback", "kb", "knockback_vec", "velocity")))
                tk = e.get("server_tick", e.get("game_time", t))
                key = (ev["victim"], tk, round(ev["damage"], 3) if isinstance(ev["damage"], (int, float)) else str(ev["damage"]), str(ev["cause"] or "").upper())
                if key in seen:                                                 # EntityDamageByEntityEvent is also an EntityDamageEvent
                    d = seen[key]
                    for f in ("attacker", "attacker_uuid", "attacker_type", "health_after", "knockback", "victim_uuid"):
                        if d.get(f) is None and ev.get(f) is not None:
                            d[f] = ev[f]
                    d.setdefault("also", []).append(ty)
                    continue
                seen[key] = ev
                names = [ev["victim"]]
            elif k in ("use_start", "use_end", "use"):
                if k == "use":
                    ph = _phase(ty, e)
                    ev["kind"] = k = "use_start" if ph == "start" else "use_end" if ph in ("finish", "abort") else "use"
                ev.update(item=item_name(_first(e, ("item", "item_name", "held_item"))), hand=e.get("hand"))
                names = [who(_first(e, ACTOR))]
            elif k == "held":
                ev.update(old_slot=_first(e, ("old_slot", "previous_slot", "prev_slot", "from_slot", "from")),
                          new_slot=_first(e, ("new_slot", "slot", "to_slot", "to", "held_slot")),
                          item=item_name(_first(e, ("item", "new_item", "item_name", "held_item"))))
                names = [who(_first(e, ACTOR))]
            elif k == "dig":
                st = _first(e, ("stage", "progress", "destroy_stage", "level"))
                ph = _phase(ty, e) or "progress"
                if ph == "progress" and isinstance(st, (int, float)) and not 0 <= st <= 9:
                    ph = "clear"
                ev.update(pos=e.get("pos"), stage=st, phase=ph)
                names = [who(_first(e, ACTOR))]
            elif k in ("spawn", "remove", "death", "respawn"):
                en = e.get("entity") if isinstance(e.get("entity"), dict) else e
                ev.update(uuid=en.get("uuid"), etype=_etype(en), pos=_pos(en), ename=en.get("name"), killer=who(e.get("killer")), cause=e.get("cause"),
                          reason=e.get("reason"))
                names = [en.get("name") or who(e.get("player")), en.get("uuid")]
            elif k in ("explode", "prime"):
                ev.update(center=e.get("center") or e.get("pos"), n_blocks=len(e.get("blocks") or []), etype=bare(e.get("entity_type")),
                          uuid=e.get("entity_uuid"), cause=e.get("cause"))
                names = [who(e.get("player"))]
            elif k in ("place", "break"):
                ev.update(pos=e.get("pos"), state=e.get("new_state") if k == "place" else e.get("old_state"), item=item_name(e.get("item")),
                          tool=item_name(e.get("tool")))
                names = [who(e.get("player"))]
            ev["name"] = names[0] if names else None
            for nm in {n for n in names if n}:
                self._add(k, nm, ev)
            self._add(k, None, ev)
        explicit, dead, self.n_tick_respawn = {n for k, n in self._ix if k == "respawn" and n}, {}, 0
        for e in events:                                                        # WorldStateLogger logs no respawn: tick "dead" true -> false
            if e.get("type") != "tick" or e.get("epoch_ms") is None:
                continue
            for p in _plist(e.get("players")):
                nm, d = p.get("name") or p.get("player"), p.get("dead")
                if not nm or not isinstance(d, bool):
                    continue
                if dead.get(nm) and not d and str(nm) not in explicit:
                    ev = dict(kind="respawn", type="tick_respawn", t=float(e["epoch_ms"]), seq=e.get("seq"), game_time=e.get("game_time"),
                              server_tick=e.get("server_tick"), raw=dict(health=p.get("health"), pos=p.get("pos"), synthetic=True), uuid=p.get("uuid"),
                              etype="player", pos=_pos(p), ename=str(nm), killer=None, cause=None, reason="tick dead -> alive", name=str(nm))
                    self._add("respawn", str(nm), ev); self._add("respawn", None, ev); self.n_tick_respawn += 1
                dead[nm] = d
        for ev in seen.values():                                                # attacker side, after the pairs merged
            self._add("hit", None, ev)
            if ev["attacker"]:
                self._add("hit", ev["attacker"], ev)
        for v in self._ix.values():
            o = sorted(range(len(v[0])), key=v[0].__getitem__)
            v[0][:], v[1][:] = [v[0][i] for i in o], [v[1][i] for i in o]

    def _add(self, kind, name, ev):
        ts, evs = self._ix.setdefault((kind, name), ([], []))
        ts.append(ev["t"]); evs.append(ev)
        if name is None:
            self.counts[kind] += 1

    def all(self, kind, name=None):
        return list(self._ix.get((kind, name), ([], []))[1])

    def recent(self, kind, name, t_ms, window_ms):
        """Events of kind (for name; None = anyone) with t_ms - window_ms <= t <= t_ms, oldest first."""
        ts, evs = self._ix.get((kind, name), ([], []))
        return evs[bisect.bisect_left(ts, t_ms - window_ms):bisect.bisect_right(ts, t_ms)]

    def last(self, kind, name, t_ms):
        ts, evs = self._ix.get((kind, name), ([], []))
        i = bisect.bisect_right(ts, t_ms)
        return evs[i - 1] if i else None


# ---- entities -------------------------------------------------------------------------------------------------------
def norm_entity(e, i=0):
    """Snapshot / spawn entity object or T3 per-tick entity -> dict(uuid, type (bare), name, pos, yaw, pitch (Mineflayer
    rad or None; a living entity's "yaw" is its head yaw), head_yaw, body_yaw (body_yaw when logged, else the yaw), size
    (w, h), health, alive, vel, item, count (top level or the "item" {id, count}), block_state, fuse_ticks)."""
    ty = _etype(e)
    w, h = _first(e, ("bbox_w", "width", "w")), _first(e, ("bbox_h", "height", "h"))
    bb = e.get("bbox") if isinstance(e.get("bbox"), (list, tuple)) else e.get("size") if isinstance(e.get("size"), (list, tuple)) else None
    if (w is None or h is None) and bb is not None:
        w, h = (bb[0], bb[1]) if len(bb) == 2 else (abs(bb[3] - bb[0]), abs(bb[4] - bb[1])) if len(bb) == 6 else (w, h)
    if w is None or h is None:
        w, h = SIZES.get(ty, (0.6, 0.6))
    yaw = pitch = None
    if e.get("yaw") is not None:
        yaw, pitch = mc_to_mineflayer(float(e["yaw"]), float(e.get("pitch") or 0.0))
    body = mc_to_mineflayer(float(e["body_yaw"]), 0.0)[0] if isinstance(e.get("body_yaw"), (int, float)) else yaw
    v = _first(e, ("vel", "velocity"))
    if v is None and all(k in e for k in ("vx", "vy", "vz")):
        v = [e["vx"], e["vy"], e["vz"]]
    it = e.get("item") or (e.get("item_name") or e.get("name") if ty == "item" else None)      # T3: "name" = player or item name
    return dict(uuid=str(_first(e, ("uuid", "id", "entity_id", "entity_uuid")) or f"anon{i}"), type=ty, type_raw=e.get("type") or e.get("kind"),
                name=e.get("name"), pos=_pos(e), yaw=yaw, pitch=pitch, head_yaw=yaw, body_yaw=body, size=(float(w), float(h)), health=e.get("health"),
                alive=e.get("alive", True), vel=v, item=item_name(it) if it else None,
                count=e["count"] if e.get("count") is not None else it.get("count") if isinstance(it, dict) else None,
                block_state=e.get("block_state"), fuse_ticks=e.get("fuse_ticks"))


class EntityTrack:
    """Non-player entities alive at t. mode 'lists' (per-tick entity lists "entities" / "mobs", pos interpolated between lists
    for the same uuid; entities never listed - items, TNT when only mobs are listed - still come from the events) or 'events'
    (start snapshot + entity_spawn until entity_remove / entity_death; pos = last known at or before t)."""

    def __init__(self, snap, events, players=()):
        self.players = set(players)
        for e in events:
            if e.get("type") == "episode_begin":
                self.players |= {str(p) for p in e.get("tracked_players") or [] if isinstance(p, str)}
            elif e.get("type") == "tick":
                self.players |= {str(p.get("name")) for p in _plist(e.get("players")) if p.get("name")}
        self.lists = []
        for e in events:
            ty = e.get("type")
            if (ty in ENTITY_LIST_TYPES or ty == "tick") and _has_list(e) and e.get("epoch_ms") is not None:
                d = {}
                for i, x in enumerate(_elist(e)):
                    if isinstance(x, dict) and _pos(x) is not None:
                        n = norm_entity(x, i)
                        if not self._player(n):
                            d[n["uuid"]] = n
                self.lists.append((float(e["epoch_ms"]), d))
        self.mode = "lists" if self.lists else "events"
        self.lists.sort(key=lambda r: r[0])
        self.lt = np.maximum.accumulate(np.array([r[0] for r in self.lists], np.float64)) if self.lists else np.zeros(0)
        self.lives, cur = [], {}
        for i, x in enumerate((snap or {}).get("entities") or []):
            n = norm_entity(x, i)
            if not self._player(n) and n["pos"] is not None:
                cur[n["uuid"]] = dict(t0=-math.inf, t1=math.inf, ent=n, pt=[-math.inf], pp=[n["pos"]]); self.lives.append(cur[n["uuid"]])
        for e in events:
            t, k = e.get("epoch_ms"), KIND_OF.get(e.get("type"))
            en = e.get("entity") if isinstance(e.get("entity"), dict) else None
            if t is None:
                continue
            t = float(t)
            if k == "spawn" and en is not None:
                n = norm_entity(en)
                if self._player(n) or n["pos"] is None:
                    continue
                r = cur.get(n["uuid"])
                if r is None or r["t1"] <= t:
                    cur[n["uuid"]] = dict(t0=t, t1=math.inf, ent=n, pt=[t], pp=[n["pos"]]); self.lives.append(cur[n["uuid"]])
                continue
            for x in [en] + [e.get(f) for f in ("victim", "attacker", "damager") if isinstance(e.get(f), dict)]:
                if not isinstance(x, dict) or str(x.get("uuid")) not in cur:
                    continue
                r = cur[str(x["uuid"])]
                if r["t1"] < math.inf:
                    continue
                if _pos(x) is not None:
                    r["pt"].append(t); r["pp"].append(_pos(x))
                if k in ("remove", "death") and x is en:
                    r["t1"] = t
        self.listed = {u for _, d in self.lists for u in d}                    # list-tracked uuids; the rest (items, TNT) by events
        self.n_event_only = len({id(r) for r in self.lives if r["ent"]["uuid"] not in self.listed})

    def _player(self, n):
        return n["type"] == "player" or (n["type"] in ("", "unknown") and n.get("name") in self.players)

    def at(self, t_ms):
        """-> [dict(uuid, type, name, pos, yaw, pitch, size, health, alive, ..., age_ms, pos_t (time of the position; None = the
        start snapshot's))] alive at t (removed at t1: gone from t1)."""
        if self.mode == "lists":
            n = len(self.lt)
            k = int(np.searchsorted(self.lt, t_ms, side="right")) - 1
            k0, k1 = min(max(k, 0), n - 1), min(max(k + 1, 0), n - 1)
            w = 0.0 if k < 0 or k1 == k0 or self.lt[k1] <= self.lt[k0] else (t_ms - self.lt[k0]) / (self.lt[k1] - self.lt[k0])
            out = []
            for u, a in self.lists[k0][1].items():
                d = dict(a, age_ms=None, pos_t=float(self.lt[k0]))
                b = self.lists[k1][1].get(u)
                if b is not None and w > 0 and np.linalg.norm(b["pos"] - a["pos"]) <= JUMP_M:
                    d["pos"] = a["pos"] + w * (b["pos"] - a["pos"]); d["pos_t"] = float(t_ms)
                    for f in ("yaw", "head_yaw", "body_yaw"):
                        if a.get(f) is not None and b.get(f) is not None:
                            d[f] = float(_lerp_ang(a[f], b[f], w))
                out.append(d)
        else:
            out = []
        for r in self.lives:
            if r["t0"] <= t_ms < r["t1"] and (self.mode == "events" or r["ent"]["uuid"] not in self.listed):
                j = max(bisect.bisect_right(r["pt"], t_ms) - 1, 0)
                out.append(dict(r["ent"], pos=r["pp"][j], age_ms=None if r["t0"] == -math.inf else t_ms - r["t0"], pos_t=r["pt"][j] if r["pt"][j] > -math.inf else None))
        return out


# ---- per-episode summary (CLI) --------------------------------------------------------------------------------------
def summary(ep):
    """Load one episode and report what the converter will see: record / kind counts, unknown types, replay vs end snapshot,
    per-bot frames, the frame-time join to the tick map and the action-vs-log yaw agreement."""
    f = ep["files"]
    out = dict(eid=ep["eid"], inst=ep["inst"], base=ep["base"], files={k: os.path.basename(v) for k, v in f.items() if v},
               missing=[k for k, v in f.items() if v is None])
    if not f["events"]:
        return out
    st = {}
    ev = load_events(f["events"], st)
    ix, pt = EventIndex(ev), PlayerTrack(ev)
    out.update(records=len(ev), load=st, types=dict(ix.type_counts), kinds=dict(ix.counts), unknown_types=dict(ix.unknown_types),
               players={n: dict(ticks=len(pt.tracks[n]["t"]), span_ms=pt.span(n)) for n in pt.names})
    if f["snap_start"]:
        snap = load_snapshot(f["snap_start"])
        bt = BlockTimeline(snap, ev)
        out.update(box=dict(origin=bt.origin.tolist(), size=bt.size, mismatch_episode_begin=bt.box_mismatch), block_source=bt.source,
                   block_changes=len(bt.changes), id_changes=int(bt.cver[-1]) if len(bt.cver) else 0, n_outside=bt.n_outside,
                   n_nonmono=bt.n_nonmono, n_prev_mismatch=bt.n_prev_mismatch, palette=[bt.n_palette0, len(bt.palette)],
                   entities=EntityTrack(snap, ev).mode)
        if f["snap_end"]:
            out["replay"] = bt.compare_end(load_snapshot(f["snap_end"]))
    for b in ep["bots"]:
        if not f.get("action_" + b):
            continue
        acts = load_actions(f["action_" + b])
        ts = frame_times(acts)
        r = dict(frames=len(acts), span_ms=[float(np.nanmin(ts)), float(np.nanmax(ts))] if len(ts) else None, camera_pose=camera_track(ev, b) is not None,
                 pose_link=any(a.get("pose_seq") is not None for a in acts))           # pose link: the client renders the action pose, not tick cameras
        if b in pt and len(ts):
            t0, t1 = pt.span(b)
            r["frames_inside_ticks"] = float(np.mean((ts >= t0) & (ts <= t1)))
            dy = [abs(((float(a["yaw"]) - pt.at(b, t)["yaw"] + math.pi) % TAU) - math.pi) for a, t in zip(acts, ts) if a.get("yaw") is not None]
            r["yaw_action_vs_tick_deg"] = dict(median=float(np.degrees(np.median(dy))), p99=float(np.degrees(np.percentile(dy, 99)))) if dy else None
        out[b] = r
    return out


# ---- selftest -------------------------------------------------------------------------------------------------------
def _rle(flat):
    out = []
    for v in flat:
        if out and out[-1] == v:
            out[-2] += 1
        else:
            out += [1, v]
    return out


def _encode(states, origin, size, order="yzx", extra=None):
    """Dense {(x, y, z): state} -> snapshot dict with an independent loop encoder (slowest axis first in `order`)."""
    pal, flat = [], []
    rng = {a: range(n) for a, n in zip("xyz", size)}
    for a in rng[order[0]]:
        for b in rng[order[1]]:
            for c in rng[order[2]]:
                loc = {order[0]: a, order[1]: b, order[2]: c}
                s = states[(loc["x"], loc["y"], loc["z"])]
                if s not in pal:
                    pal.append(s)
                flat.append(pal.index(s))
    return dict(format="solaris-block-snapshot-v1", origin=list(origin), size=list(size), order=order, palette=pal, rle=_rle(flat), **(extra or {}))


def selftest():
    T0 = 1_761_200_000_000
    origin, size = (-3, 60, -2), (4, 3, 5)                                     # x -3..0, y 60..62, z -2..2 (inclusive)
    sx, sy, sz = size
    STAIR_E = "minecraft:oak_stairs[facing=east,half=bottom,shape=straight,waterlogged=false]"
    STAIR_W = STAIR_E.replace("east", "west")
    s0 = {}
    for x in range(sx):
        for y in range(sy):
            for z in range(sz):
                s0[(x, y, z)] = ("minecraft:oak_planks" if (x, z) == (1, 3) else "minecraft:stone") if y == 0 else \
                    STAIR_E if (y, x, z) == (1, 1, 0) else "minecraft:grass_block[snowy=false]" if (y, x, z) == (1, 0, 4) else AIR_STATE
    snap_d = _encode(s0, origin, size, extra=dict(which="start", entities=[
        dict(uuid="u-cow", type="minecraft:cow", pos=[-1.5, 61.0, 1.5], yaw=90.0, pitch=0.0),
        dict(uuid="u-alpha", type="minecraft:player", name="Alpha", pos=[0.5, 61.0, 0.5], yaw=0.0, pitch=0.0)]))
    # decoding: schema formula on the raw rle, then the [x, y, z] grid
    snap = decode_snapshot(snap_d)
    dense = np.repeat(snap_d["rle"][1::2], snap_d["rle"][0::2])
    for (x, y, z), s in s0.items():
        assert snap["palette"][snap["grid"][x, y, z]] == s
        assert snap_d["palette"][dense[(y * sz + z) * sx + x]] == s
    assert snap["grid"].shape == size and snap["grid"].dtype == np.int32 and snap["origin"].dtype == np.int64
    for order in ("xyz", "zxy"):
        g2 = decode_snapshot(_encode(s0, origin, size, order=order))
        assert all(g2["palette"][g2["grid"][k]] == s for k, s in s0.items()), order
    bad = dict(snap_d, rle=snap_d["rle"][:-2] + [snap_d["rle"][-2] - 1, snap_d["rle"][-1]])
    try:
        decode_snapshot(bad); raise AssertionError("short rle accepted")
    except ValueError:
        pass
    # events: ticks every 50 ms, block_state stream, T12 / T15 / T17 records, an unknown type
    ev, seq = [], [0]

    def rec(t, ty, **kw):
        ev.append(dict(type=ty, seq=seq[0], game_time=1000 + int(t // 50), server_tick=5000 + int(t // 50), epoch_ms=T0 + t, **kw)); seq[0] += 1

    rec(0, "episode_begin", format="solaris-world-events-v1", episode_id=3, instance_id=0, tracked_players=["Alpha", "Bravo"],
        box=dict(min=list(origin), max=[o + n - 1 for o, n in zip(origin, size)]))
    blocks = [(120, (-1, 61, 0), "minecraft:red_wool", None), (120, (-2, 61, 1), "minecraft:oak_planks", None),
              (300, (1, 61, 0), "minecraft:stone", None), (350, (-1, 61, 0), AIR_STATE, "minecraft:red_wool"),
              (340, (-2, 61, -2), STAIR_W, STAIR_E), (345, (-3, 61, 2), "minecraft:grass_block[snowy=true]", None),
              (500, (-3, 60, 2), "minecraft:blue_concrete", None), (600, (-2, 61, 1), AIR_STATE, "minecraft:stone"),
              (850, (0, 62, 2), "minecraft:tnt", None), (860, (0, 60, -2), AIR_STATE, "minecraft:stone")]
    bi = 0
    for k in range(21):
        t = 50 * k
        while bi < len(blocks) and blocks[bi][0] <= t:                        # seq order = list order (340, 345 after 350)
            bt_, p, s, prev = blocks[bi]
            ev.append(dict(type="block_state", seq=seq[0], game_time=0, server_tick=0, epoch_ms=T0 + bt_, pos=list(p), block=bare(s), state=s, prev_state=prev))
            seq[0] += 1; bi += 1
        ya = ((170 + 2 * k + 180) % 360) - 180
        rec(t, "tick", full_time=6000, time_of_day=6000, players=[
            dict(name="Alpha", world="world", pos=[0.5 + 0.05 * k, 61.0, 0.5], yaw=ya, pitch=10.0, sneaking=False, sprinting=k > 10,
                 on_ground=True, health=20.0, fire_ticks=0, held_slot=0 if k < 8 else 2, held_item="minecraft:diamond_sword" if k < 8 else "minecraft:oak_planks",
                 held_count=1, food=18, saturation=5.0, body_yaw=ya - 10, head_yaw=ya,
                 hotbar=[dict(name="diamond_sword", count=1), None, dict(name="oak_planks", count=32)] + [None] * 6),
            dict(name="Bravo", world="world", pos=[-2.5 if k != 15 else 30.0, 61.0, -1.5], yaw=0.0, pitch=-30.0, sneaking=k >= 18,
                 sprinting=False, on_ground=True, health=20.0 if k < 5 else 14.0, fire_ticks=0, held_slot=1, held_item=None, held_count=0)])
        rec(t, "camera_pose", name="Alpha", pos=[0.5 + 0.05 * k, 61.0, 0.5], yaw=ya, pitch=10.0)
        if k == 2:
            rec(t + 5, "foo_bar", x=1)
    rec(200, "player_animation", player="Alpha", animation="ARM_SWING")
    rec(210, "entity_damage", victim="Bravo", damage=6.0, cause="ENTITY_ATTACK", health_after=14.0)
    ev[-1]["server_tick"] = 7777
    rec(210, "entity_damage_by_entity", victim=dict(name="Bravo", uuid="u-b"), attacker=dict(name="Alpha", uuid="u-a"), damage=6.0, cause="ENTITY_ATTACK")
    ev[-1]["server_tick"] = 7777
    rec(260, "arm_swing", name="Alpha")
    rec(270, "player_animation", player="Bravo", animation="LEAVE_BED")
    rec(300, "entity_spawn", entity=dict(uuid="u-tnt", type="minecraft:tnt", pos=[-1.5, 61.0, 0.5], fuse_ticks=80), reason=None)
    rec(400, "item_held", player="Alpha", old_slot=0, new_slot=2, item="minecraft:oak_planks")
    rec(450, "item_use", player="Bravo", item="minecraft:bow", phase="start")
    rec(700, "item_use_end", player="Bravo", item="minecraft:bow")
    rec(500, "dig_progress", player="Alpha", pos=[-2, 61, 1], stage=0)
    rec(550, "dig_progress", player="Alpha", pos=[-2, 61, 1], stage=3)
    rec(600, "block_break_animation", digger="Alpha", pos=[-2, 61, 1], stage=-1)
    rec(620, "dig_finish", player="Alpha", pos=[-2, 61, 1])
    rec(800, "entity_explode", center=[-1.5, 61.0, 0.5], blocks=[dict(pos=[0, 60, -2], old_type="minecraft:stone", old_state="minecraft:stone")],
        entity_type="minecraft:tnt", entity_uuid="u-tnt")
    rec(800, "entity_remove", entity=dict(uuid="u-tnt", type="minecraft:tnt", pos=[-1.5, 60.8, 0.5]))
    rec(900, "entity_spawn", entity=dict(uuid="u-item", type="minecraft:item", pos=[-1.0, 61.2, 0.0], item="minecraft:tnt", count=1), reason="ITEM")
    rec(950, "note", text='{"ep":3,"phase":"done"}')
    rec(1010, "episode_end", reason="command")
    # end snapshot = start + every in-box block_state (palette in another order)
    s1 = dict(s0)
    for _, p, s, _ in blocks:
        q = (p[0] - origin[0], p[1] - origin[1], p[2] - origin[2])
        if q in s1:
            s1[q] = s
    end_d = _encode(s1, origin, size, extra=dict(which="end", entities=[]))
    end_d["palette"] = end_d["palette"][::-1]
    n_p = len(end_d["palette"])
    end_d["rle"] = [v if i % 2 == 0 else n_p - 1 - v for i, v in enumerate(end_d["rle"])]
    tmp = tempfile.mkdtemp(prefix="cap_reader_")
    try:
        return _selftest_files(tmp, T0, origin, size, s0, snap_d, end_d, ev, blocks, STAIR_E, STAIR_W)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _selftest_files(tmp, T0, origin, size, s0, snap_d, end_d, ev, blocks, STAIR_E, STAIR_W):
    b0, b1 = os.path.join(tmp, "batch_0"), os.path.join(tmp, "batch_1")
    for d in ("output", "aligned"):
        os.makedirs(os.path.join(b0, d))
    os.makedirs(os.path.join(b1, "output"))
    out0, al0 = os.path.join(b0, "output"), os.path.join(b0, "aligned")
    lines = [json.dumps(e) for e in ev]
    rs = np.random.default_rng(0)
    rs.shuffle(lines)
    with open(os.path.join(out0, "20261023_101459_000003_world_instance_000_world_events.jsonl"), "w") as fh:
        fh.write("\n".join(lines) + '\n{"type": "tick", "seq": 99')
    with gzip.open(os.path.join(out0, "20261023_101459_000003_world_instance_000_world_snapshot_start.json.gz"), "wt") as fh:
        json.dump(snap_d, fh)
    with gzip.open(os.path.join(out0, "20261023_101459_000003_world_instance_000_world_snapshot_end.json.gz"), "wt") as fh:
        json.dump(end_d, fh)
    with open(os.path.join(b1, "output", "x_000003_world_instance_000_world_events.jsonl"), "w") as fh:
        fh.write(json.dumps(ev[0]) + "\n")
    acts, pt0 = {}, PlayerTrack(ev)
    for bot, ts in (("Alpha", "20261023_101500"), ("Bravo", "20261023_101501")):
        stem = f"{ts}_000003_{bot}_instance_000"
        a = []
        for i in range(20):
            rt = T0 + 50 * i + int(rs.integers(3, 47))                          # not tick-locked
            p = pt0.at(bot, rt)
            a.append(dict(x=p["pos"][0], y=p["pos"][1], z=p["pos"][2], yaw=round(p["yaw"], 5), pitch=round(p["pitch"], 5), renderTime=rt,
                          epochTime=rt / 1000.0, frame_count=i, episode_id=3, action=dict(forward=False), inventory=dict(quickBarSlot=0)))
        acts[bot] = a
        for suf, obj in (("", a), ("_meta", dict(frames=20)), ("_episode_info", dict(bot_name=bot, world=dict(seed="1")))):
            with open(os.path.join(out0, stem + suf + ".json"), "w") as fh:
                json.dump(obj, fh)
        if bot == "Alpha":
            open(os.path.join(al0, stem + "_camera.mp4"), "wb").close()
            with open(os.path.join(al0, stem + "_camera_meta.json"), "w") as fh:
                json.dump(dict(frame_mapping=[]), fh)
    # discovery
    eps = episodes(tmp)
    assert [(os.path.basename(e["base"]), e["eid"], e["inst"]) for e in eps] == [("batch_0", 3, 0), ("batch_1", 3, 0)], eps
    f = eps[0]["files"]
    for k in ("events", "snap_start", "snap_end", "action_Alpha", "action_Bravo", "meta_Alpha", "info_Bravo", "video_Alpha", "video_meta_Alpha"):
        assert f[k] and os.path.exists(f[k]), k
    assert f["video_Bravo"] is None and f["video_meta_Bravo"] is None and f["action_Alpha"].endswith("Alpha_instance_000.json")
    assert eps[1]["files"]["snap_start"] is None and eps[1]["files"]["action_Alpha"] is None
    st = {}
    E = load_events(f["events"], st)
    assert st["bad_lines"] == 1 and [e["seq"] for e in E] == sorted(e["seq"] for e in ev) and E[0]["type"] == "episode_begin"
    # blocks: time join, inclusive box, palette growth, version, non-monotone clock, prev mismatch, end replay
    snap = load_snapshot(f["snap_start"])
    bt = BlockTimeline(snap, E)
    assert bt.n_outside == 1 and bt.n_nonmono == 2 and bt.n_prev_mismatch == 1 and not bt.box_mismatch and bt.source == "block_state"
    assert {"minecraft:red_wool", "minecraft:blue_concrete", "minecraft:tnt", STAIR_W} <= set(bt.palette[bt.n_palette0:])
    assert len(bt.palette) > bt.n_palette0 and bt.changes[-1]["pos"] == (0, 60, -2)
    teff = np.maximum.accumulate([b[0] for b in blocks])
    inbox = [i for i, b in enumerate(blocks) if b[1] != (1, 61, 0)]

    def expect(t):
        g = {k: block_id(v) for k, v in s0.items()}
        ver = 0
        for i in inbox:
            if teff[i] <= t:
                _, p, s, _ = blocks[i]
                q = (p[0] - origin[0], p[1] - origin[1], p[2] - origin[2])
                ver += block_id(s) != g[q]; g[q] = block_id(s)
        return g, ver

    for t in (0, 119, 120, 121, 339, 345, 349, 350, 499, 500, 600, 1000, 130, 350, 2000, -5):
        ids, ver = bt.grid_at(T0 + t)
        g, v = expect(t)
        assert ver == v and all(ids[k] == g[k] for k in g), (t, ver, v)
    assert bt.grid_at(T0 + 119)[1] == 0 and np.array_equal(bt.grid_at(T0 + 119)[0], bt.ids0)
    assert bt.grid_at(T0 + 120)[0][2, 1, 2] == block_id("minecraft:red_wool")    # (-1, 61, 0) visible at renderTime == epoch_ms
    if block_id(STAIR_E) == block_id(STAIR_W):
        assert not bt.changes[[c["pos"] for c in bt.changes].index((-2, 61, -2))]["changed"]
    s_at, _ = bt.states_at(T0 + 1000)
    assert bt.palette[s_at[3, 2, 4]] == "minecraft:tnt" and bt.palette[s_at[1, 1, 0]] == STAIR_W
    rep = bt.compare_end(load_snapshot(f["snap_end"]))
    assert rep["mismatch_states"] == 0 and rep["mismatch_ids"] == 0, rep
    assert len(bt.changes_between(T0 + 119, T0 + 120)) == 2
    for a in acts["Alpha"]:                                                       # the join: frame shows every record with t <= renderTime
        assert bt.grid_at(a["renderTime"])[1] == expect(a["renderTime"] - T0)[1]
    fb = BlockTimeline(snap, [dict(type="block_place", epoch_ms=T0 + 5, pos=[-1, 61, 0], new_state=None, new_type="minecraft:oak_log",
                                   player="Alpha")] + [e for e in E if e["type"] != "block_state"])
    assert fb.source == "events" and fb.changes[0]["state"] == "minecraft:oak_log" and fb.changes[1]["state"] == AIR_STATE
    # players: interpolation, circular yaw, step fields, teleport hold, extras, eye
    pt = PlayerTrack(E)
    assert pt.names == ["Alpha", "Bravo"]
    p = pt.at("Alpha", T0 + 25)
    assert abs(p["pos"][0] - 0.525) < 1e-9 and abs(p["eye"][1] - 62.62) < 1e-9 and p["food"] == 18.0 and p["hotbar9"][2] == ("oak_planks", 32)
    p = pt.at("Alpha", T0 + 275)                                                  # MC yaw 180 -> 182 (= -178): through north-ish, not via 0
    assert abs(((p["yaw"] - math.radians(-1.0)) + math.pi) % TAU - math.pi) < 1e-9, p["yaw"]
    assert abs(p["pitch"] + math.radians(10)) < 1e-12 and abs(((p["head_yaw"] - p["yaw"]) + math.pi) % TAU - math.pi) < 1e-9
    assert abs(((p["body_yaw"] - p["yaw"] - math.radians(10)) + math.pi) % TAU - math.pi) < 1e-9 and pt.units["Alpha"]["body_yaw"] == "deg"
    assert pt.at("Alpha", T0 + 399)["held_item"] == "diamond_sword" and pt.at("Alpha", T0 + 400)["held_slot"] == 2
    q = pt.at("Bravo", T0 + 0)
    assert abs(q["yaw"] - math.pi) < 1e-12 and abs(q["pitch"] - math.radians(30)) < 1e-12 and q["held_item"] is None
    assert pt.at("Bravo", T0 + 249)["health"] == 20.0 and pt.at("Bravo", T0 + 250)["health"] == 14.0 and pt.at("Bravo", T0 + 250)["alive"]
    assert pt.at("Bravo", T0 + 725)["pos"][0] == -2.5 and pt.at("Bravo", T0 + 760)["pos"][0] == 30.0      # teleport: hold, no blend
    assert abs(pt.at("Bravo", T0 + 900)["eye"][1] - 62.27) < 1e-9 and pt.at("Bravo", -1e18)["t_rec"] == T0 and pt.at("Nobody", T0) is None
    assert pt.at("Alpha", T0 + 5000)["pos"][0] == 1.5
    for a in acts["Alpha"]:
        assert abs(((a["yaw"] - pt.at("Alpha", a["renderTime"])["yaw"]) + math.pi) % TAU - math.pi) < 1e-4
    # conventions
    assert np.allclose(mc_to_mineflayer(0.0, 0.0), (math.pi, 0.0)) and np.allclose(mc_to_mineflayer(90.0, 90.0), (math.pi / 2, -math.pi / 2))
    assert np.allclose(mc_to_mineflayer(-90.0, -45.0), (1.5 * math.pi, math.pi / 4)) and abs(mc_to_mineflayer(180.0, 0)[0]) < 1e-12
    y, pp = mc_to_mineflayer(np.array([10.0, -170.0]), np.array([5.0, -5.0]))
    assert np.allclose(mineflayer_to_mc(y, pp), ([10.0, -170.0], [5.0, -5.0]))
    # events
    ix = EventIndex(E)
    assert dict(ix.unknown_types) == {"foo_bar": 1}
    assert len(ix.recent("swing", "Alpha", T0 + 260, 500)) == 2 and len(ix.recent("swing", "Alpha", T0 + 259, 500)) == 1
    assert len(ix.recent("swing", None, T0 + 1000, 1e9)) == 2 and len(ix.all("animation", "Bravo")) == 1
    d = ix.all("damage", "Bravo")
    assert len(d) == 1 and d[0]["attacker"] == "Alpha" and d[0]["damage"] == 6.0 and d[0]["health_after"] == 14.0 and d[0]["victim_uuid"] == "u-b"
    assert ix.all("hit", "Alpha") == d and ix.last("damage", "Bravo", T0 + 209) is None and ix.last("damage", "Bravo", T0 + 210) is d[0]
    assert ix.last("held", "Alpha", T0 + 1000)["new_slot"] == 2 and ix.last("held", "Alpha", T0 + 1000)["item"] == "oak_planks"
    assert len(ix.all("use_start", "Bravo")) == 1 and len(ix.all("use_end", "Bravo")) == 1 and ix.all("use_start", "Bravo")[0]["item"] == "bow"
    assert [(x["phase"], x["stage"]) for x in ix.all("dig", "Alpha")] == [("progress", 0), ("progress", 3), ("clear", -1), ("finish", None)]
    assert len(ix.all("explode")) == 1 and ix.all("spawn", "u-tnt")[0]["etype"] == "tnt" and len(ix.all("remove", "u-tnt")) == 1
    # entities: event mode (snapshot + spawn / remove), then list mode
    et = EntityTrack(snap, E)
    assert et.mode == "events"
    ty = lambda t: sorted(x["type"] for x in et.at(T0 + t))  # noqa: E731
    assert ty(299) == ["cow"] and ty(300) == ["cow", "tnt"] and ty(799) == ["cow", "tnt"] and ty(800) == ["cow"] and ty(950) == ["cow", "item"]
    tn = [x for x in et.at(T0 + 500) if x["type"] == "tnt"][0]
    assert np.allclose(tn["pos"], [-1.5, 61.0, 0.5]) and tn["age_ms"] == 200 and tn["size"] == (0.98, 0.98) and tn["fuse_ticks"] == 80
    assert abs([x for x in et.at(T0) if x["type"] == "cow"][0]["yaw"] - math.pi / 2) < 1e-12
    L = [dict(type="entities", seq=0, epoch_ms=T0 + 0, entities=[dict(uuid="z1", kind="zombie", x=0.0, y=61.0, z=0.0, yaw=0.0, pitch=0.0, health=20.0),
                                                                 dict(uuid="pA", kind="player", name="Alpha", x=5.0, y=61.0, z=5.0, yaw=0.0, pitch=0.0)]),
         dict(type="entities", seq=1, epoch_ms=T0 + 100, entities=[dict(uuid="z1", kind="zombie", x=1.0, y=61.0, z=0.0, yaw=90.0, pitch=0.0,
                                                                        bbox_w=0.6, bbox_h=1.95, health=15.0)])]
    el = EntityTrack(None, L)
    z = el.at(T0 + 50)
    assert el.mode == "lists" and len(z) == 1 and z[0]["type"] == "zombie" and np.allclose(z[0]["pos"], [0.5, 61.0, 0.0]) and z[0]["size"] == (0.6, 1.95)
    assert abs(z[0]["yaw"] - _lerp_ang(math.pi, math.pi / 2, 0.5)) < 1e-12 and el.at(T0 + 150)[0]["health"] == 15.0
    assert PlayerTrack(L).at("Alpha", T0 + 50)["pos"][0] == 5.0
    M = [dict(type="tick", seq=0, epoch_ms=T0, players=[], mobs=[dict(uuid="z2", type="minecraft:zombie", pos=[0, 61, 0], yaw=0.0, bbox=[0.6, 1.95])]),
         dict(type="entity_spawn", seq=1, epoch_ms=T0 + 10, entity=dict(uuid="it", type="minecraft:item", pos=[1, 61, 1], item="minecraft:stone", count=1)),
         dict(type="tick", seq=2, epoch_ms=T0 + 50, players=[], mobs=[])]
    em = EntityTrack(None, M)
    assert em.mode == "lists" and sorted(x["type"] for x in em.at(T0 + 20)) == ["item", "zombie"] and [x["type"] for x in em.at(T0 + 60)] == ["item"]
    # camera (T12)
    ct = camera_track(E, "Alpha")
    assert ct is not None and len(ct[0]) == 21 and np.allclose(ct[1][:, 1], 62.62) and camera_track(E, "Bravo") is None
    eye, yw, pc = camera_at(ct, [T0 + 275, T0 - 10, T0 + 9999])
    assert abs(((yw[0] - math.radians(-1.0)) + math.pi) % TAU - math.pi) < 1e-9 and abs(eye[1, 0] - 0.5) < 1e-12 and abs(eye[2, 0] - 1.5) < 1e-12
    assert camera_track([dict(type="camera_state", epoch_ms=1, cameras=[dict(target="Bravo", eye=[1, 2, 3], yaw=0, pitch=0)])], "Bravo")[1].tolist() == [[1, 2, 3]]
    # actions, item helpers, summary
    A = load_actions(f["action_Alpha"])
    ft = frame_times(A)
    assert len(A) == 20 and np.all(np.diff(ft) > 0) and np.any((ft - T0) % 50 != 0)
    assert hotbar9([dict(slot=36, name="minecraft:stone", count=5), dict(slot=44, type="bow")])[::8] == [("stone", 5), ("bow", 1)]
    assert item_name("minecraft:air") is None and item_name({"name": "minecraft:bow"}) == "bow" and bare(STAIR_E) == "oak_stairs"
    st_a = action_state(dict(A[3], inventory=dict(quickBarSlot=2, heldItem=dict(name="oak_planks", count=7, slot=38),
                                                 hotbar=[dict(name="diamond_sword", count=1, slot=36), None, dict(name="oak_planks", count=7, slot=38)]),
                             action=dict(sneak=True), food=17))
    assert st_a["held_item"] == "oak_planks" and st_a["held_slot"] == 2 and st_a["hotbar9"][2] == ("oak_planks", 7) and st_a["food"] == 17
    assert st_a["sneaking"] and abs(st_a["eye"][1] - st_a["pos"][1] - EYE_SNEAK) < 1e-12 and st_a["t"] == A[3]["renderTime"] and st_a["yaw"] == A[3]["yaw"]
    eo = episodes(os.path.join(os.path.dirname(f["events"])))                     # root = output/: aligned/ sibling still found
    assert len(eo) == 1 and eo[0]["files"]["video_Alpha"] == f["video_Alpha"]
    bl = BlockTimeline(snap, E, lead_ms=30)
    assert bl.grid_at(T0 + 90)[1] >= 1 and bt.grid_at(T0 + 90)[1] == 0
    ix2 = EventIndex([dict(type="tick", seq=0, epoch_ms=T0, players=[dict(name="Bravo", uuid="u-b", pos=[0, 0, 0], yaw=0, pitch=0)]),
                      dict(type="entity_damage", seq=1, epoch_ms=T0 + 1, victim="u-b", attacker=dict(uuid="u-z", type="minecraft:zombie"), damage=3.0)])
    assert len(ix2.all("damage", "Bravo")) == 1 and len(ix2.all("hit", "u-z")) == 1
    ct2 = camera_track([dict(type="tick", epoch_ms=T0, players=[dict(name="Bravo", pos=[0, 61, 0], camera=dict(name="cam_b", pos=[1, 61, 0], yaw=90.0, pitch=0.0)),
                                                                dict(name="Alpha", pos=[0, 61, 0], camera_yaw=0.0, camera_pitch=45.0, camera_pos=[2, 61, 2])])], "Bravo")
    assert np.allclose(ct2[1][0], [1, 62.62, 0]) and abs(ct2[2][0] - math.pi / 2) < 1e-12
    ct3 = camera_track([dict(type="tick", epoch_ms=T0, players=[dict(name="Alpha", pos=[0, 61, 0], camera_yaw=0.0, camera_pitch=45.0, camera_pos=[2, 61, 2])])], "Alpha")
    assert np.allclose(ct3[1][0], [2, 62.62, 2]) and abs(ct3[3][0] + math.pi / 4) < 1e-12
    hs = EYE * CAM_SNEAK_SCALE                                                    # camera player shrunk while its bot sneaks
    sn = [dict(type="tick", epoch_ms=T0, players=[dict(name="Bravo", pos=[0, 61, 0], yaw=0, pitch=0, sneaking=False)]),
          dict(type="camera_pose", epoch_ms=T0, camera="CameraBravo", follows="Bravo", pos=[0, 61, 0], yaw=0, pitch=0),
          dict(type="tick", epoch_ms=T0 + 50, players=[dict(name="Bravo", pos=[0, 61, 0], yaw=0, pitch=0, sneaking=True)]),
          dict(type="camera_pose", epoch_ms=T0 + 50, camera="CameraBravo", follows="Bravo", pos=[0, 61, 0], yaw=0, pitch=0),
          dict(type="camera_pose", epoch_ms=T0 + 100, camera="CameraBravo", follows="Bravo", pos=[0, 61, 0], yaw=0, pitch=0, scale=1.0)]
    assert np.allclose(camera_track(sn, "Bravo")[1][:, 1], [62.62, 61 + hs, 62.62]) and np.allclose(camera_track(sn, "Bravo", pos_is="eye")[1][:, 1], 61)
    tp = [dict(type="episode_begin", epoch_ms=T0, cameras={"Bravo": "Cam_B"}),
          dict(type="tick", epoch_ms=T0, players=[dict(name="Alpha", pos=[0, 61, 0], yaw=0, pitch=0, sneaking=True), dict(name="Bravo", pos=[5, 61, 0], yaw=0, pitch=0),
                                                  dict(name="CameraAlpha", pos=[0, 61, 0], yaw=90.0, pitch=10.0, sneaking=False),
                                                  dict(name="Cam_B", pos=[5, 61, 0], yaw=0.0, pitch=0.0)])]
    cta, ctb = camera_track(tp, "Alpha"), camera_track(tp, "Bravo")             # T12 as the camera players' own tick entries
    assert np.allclose(cta[1][0], [0, 61 + hs, 0]) and abs(cta[2][0] - math.pi / 2) < 1e-12 and np.allclose(ctb[1][0], [5, 62.62, 0])
    ix3 = EventIndex([dict(type="entity_damage", seq=0, epoch_ms=T0, entity=dict(uuid="u-b", type="minecraft:player", name="Bravo"), player="Alpha",
                           entity_type="minecraft:player", entity_uuid="u-a", damage=4.0, cause="ENTITY_ATTACK", health_after=16.0),
                      dict(type="entity_damage", seq=1, epoch_ms=T0 + 9, entity=dict(uuid="u-b", type="minecraft:player", name="Bravo"),
                           entity_type="minecraft:zombie", entity_uuid="u-z", damage=2.0, cause="ENTITY_ATTACK")])
    d3 = ix3.all("damage", "Bravo")                                             # actor() fields carry the damager
    assert EventIndex([dict(type="entity_damage", epoch_ms=T0, entity=dict(uuid="u-b", name="Bravo"), player="Bravo", entity_uuid="u-b", damage=1.0,
                            cause="FALL")]).all("damage", "Bravo")[0]["attacker"] is None     # actor() of the victim itself
    assert [x["attacker"] for x in d3] == ["Alpha", "u-z"] and d3[1]["attacker_type"] == "zombie" and len(ix3.all("hit", "Alpha")) == 1 and not ix3.unknown_types
    assert frame_times([dict(renderTime=4321.5, epochTime=1761200000.1234), dict(renderTime=T0 + 7, epochTime=(T0 + 7) / 1000)]).tolist() == [1761200000123.4, T0 + 7]
    assert frame_time_source([dict(renderTime=4321.5, epochTime=1.7e9)]) == "epochTime" and frame_time_source(A) == "renderTime"
    assert norm_entity(dict(uuid="i1", kind="item", name="minecraft:oak_planks", x=0, y=61, z=0))["item"] == "oak_planks"
    pr = os.path.join(tmp, "prepared")                                           # prepare_train_dataset layout: flat dir, no TS prefix, "_camera" dropped
    os.makedirs(pr)
    for fn in ("000003_Alpha_instance_000.json", "000003_Alpha_instance_000_episode_info.json", "000003_Alpha_instance_000.mp4",
               "000003_world_instance_000_world_events.jsonl"):
        open(os.path.join(pr, fn), "w").close()
    ep_p = episodes(pr)
    assert len(ep_p) == 1 and ep_p[0]["files"]["video_Alpha"].endswith("000003_Alpha_instance_000.mp4") and ep_p[0]["files"]["action_Alpha"].endswith("_000.json")
    # capture-engine shapes: use_item_stop / bow_shoot, body_yaw degrees near 0, tick dead -> respawn, nested T16 state, entities record
    R = [dict(type="tick", seq=k, epoch_ms=T0 + 50 * k, players=[dict(name="Bravo", pos=[0, 61, 0] if k < 3 else [3, 61, 0], yaw=40.0 * k, pitch=0.0,
                                                                        body_yaw=2.0, head_yaw=40.0 * k, dead=k in (1, 2), health=0.0 if k in (1, 2) else 20.0,
                                                                        hand_raised=k == 4)]) for k in range(6)]
    R += [dict(type="use_item_start", seq=10, epoch_ms=T0 + 10, player="Bravo", item=dict(id="minecraft:shield", count=1), hand="OFF_HAND"),
          dict(type="use_item_stop", seq=11, epoch_ms=T0 + 160, player="Bravo", item=dict(id="minecraft:shield", count=1), reason="stopped"),
          dict(type="use_item_start", seq=12, epoch_ms=T0 + 200, player="Bravo", item=dict(id="minecraft:bow", count=1), hand="HAND"),
          dict(type="bow_shoot", seq=13, epoch_ms=T0 + 240, player="Bravo", force=1.0, projectile="u-arrow"),
          dict(type="entity_knockback", seq=14, epoch_ms=T0 + 245, entity=dict(uuid="u-b", type="minecraft:player", name="Bravo"), strength=0.4),
          dict(type="entity_damage", seq=15, epoch_ms=T0 + 246, victim=dict(uuid="u-b", type="minecraft:player", name="Bravo"), damage=6.0, final_damage=4.5,
               health_after=15.5, attacker=dict(uuid="u-z", type="minecraft:zombie", name=None))]
    ixr = EventIndex(R); ptr = PlayerTrack(R)
    assert len(ixr.all("use_end", "Bravo")) == 2 and not ixr.unknown_types and ixr.last("use_start", "Bravo", T0 + 400)["t"] < ixr.last("use_end", "Bravo", T0 + 400)["t"]
    assert [e["t"] for e in ixr.all("respawn", "Bravo")] == [T0 + 150] and ixr.all("respawn")[0]["raw"]["health"] == 20.0 and ixr.n_tick_respawn == 1
    assert ixr.all("damage", "Bravo")[0]["damage"] == 4.5 and ixr.all("damage", "Bravo")[0]["attacker"] == "u-z"
    assert abs(ptr.at("Bravo", T0 + 200)["body_yaw"] - mc_to_mineflayer(2.0, 0.0)[0]) < 1e-12 and ptr.units["Bravo"]["body_yaw"] == "deg"
    assert ptr.at("Bravo", T0 + 125)["pos"][0] == 0 and ptr.at("Bravo", T0 + 150)["pos"][0] == 3 and ptr.at("Bravo", T0 + 200)["hand_raised"]   # 3 m: no blend across the respawn
    ex = dict(A[3], state=dict(health=12.0, food=15, hotbar=[dict(slot=0, id="minecraft:stone", count=3)] + [dict(slot=q, id=None, count=0) for q in range(1, 9)],
                               selected_slot=4, offhand=dict(id="minecraft:shield", count=1), sneaking=True, head_yaw=1.25, body_yaw=None,
                               cursor=dict(block=dict(pos=[1, 60, 2], face="up", name="minecraft:stone", state="minecraft:stone", distance=2.1), entity=None)),
              inventory=dict(heldItem=None), action=dict(sneak=False), pose_seq=77)
    sx_ = action_state(ex)
    assert sx_["health"] == 12.0 and sx_["food"] == 15 and sx_["held_slot"] == 4 and sx_["hotbar9"][0] == ("stone", 3) and sx_["offhand"] == "shield"
    assert sx_["crosshair"]["block"]["pos"] == [1, 60, 2] and "body_yaw" not in sx_ and sx_["head_yaw"] == 1.25 and sx_["t16_units"] == "rad"
    assert sx_["sneaking"] and abs(sx_["eye"][1] - sx_["pos"][1] - EYE) < 1e-12 and t16_of(A[3]) == ({}, None)
    ne = norm_entity(dict(uuid="m1", id=7, kind="zombie", type="minecraft:zombie", x=0, y=61, z=0, yaw=90.0, pitch=0.0, head_yaw=90.0, body_yaw=40.0))
    ni = norm_entity(dict(uuid="i2", type="minecraft:item", x=0, y=61, z=0, yaw=0.0, item=dict(id="minecraft:oak_log", count=5)))
    assert abs(ne["head_yaw"] - math.pi / 2) < 1e-12 and abs(ne["body_yaw"] - mc_to_mineflayer(40.0, 0.0)[0]) < 1e-12 and ni["item"] == "oak_log" and ni["count"] == 5
    sm = summary(eps[0])
    assert sm["replay"]["mismatch_states"] == 0 and sm["unknown_types"] == {"foo_bar": 1} and sm["Alpha"]["frames_inside_ticks"] == 1.0
    assert sm["Alpha"]["yaw_action_vs_tick_deg"]["p99"] < 0.01 and sm["Bravo"]["camera_pose"] is False
    summary(eps[1])
    print(f"cap_reader selftest ok (cap_palette {'present' if CP is not None else 'ABSENT: fallback ids'}; {len(E)} records, "
          f"{len(bt.changes)} in-box block_state, palette {bt.n_palette0}->{len(bt.palette)})")
    return True


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--test", action="store_true")
    ap.add_argument("--root", default=None)
    ap.add_argument("--report", default=None)
    a = ap.parse_args()
    if a.test:
        selftest(); return
    if not a.root:
        ap.error("--test or --root")
    reps = []
    for ep in episodes(a.root):
        r = summary(ep); reps.append(r)
        rp = r.get("replay") or {}
        print(f"episode {r['eid']:06d} inst {r['inst']:03d} {os.path.relpath(r['base'], a.root)}: records={r.get('records')} "
              f"block_changes={r.get('block_changes')} outside={r.get('n_outside')} replay_mismatch={rp.get('mismatch_states')} "
              f"unknown={r.get('unknown_types')} missing={r['missing']} | "
              + ", ".join(f"{b}: {r[b]['frames']} frames, cam_pose={r[b]['camera_pose']}, pose_link={r[b]['pose_link']}, yaw_diff={r[b].get('yaw_action_vs_tick_deg')}"
                          for b in ("Alpha", "Bravo") if b in r))
    if a.report:
        with open(a.report, "w") as fh:
            json.dump(reps, fh, indent=1, default=lambda o: o.tolist() if hasattr(o, "tolist") else str(o))
    print(f"{len(reps)} episode(s)")


if __name__ == "__main__":
    main()
