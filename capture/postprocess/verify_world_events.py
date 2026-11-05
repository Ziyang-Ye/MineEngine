#!/usr/bin/env python3
"""Verify per-episode world-state logs and align them with the per-frame actions.

For every episode found under DIR this script
  1. parses <stem>_world_events.jsonl in one streaming pass (valid JSON per
     line, seq order, episode_begin first / episode_end last, counts per
     record type); `entities` payloads are summarised, never kept;
  2. decodes the start/end block snapshots, replays every `block_state`
     record inside the box onto the start snapshot and compares the result
     with the end snapshot (a mismatch means some change was not observed,
     e.g. a /setblock or /fill issued during recording);
  3. builds the game_time <-> epoch_ms map from the `tick` records and checks
     it against each bot's per-frame action JSON (epochTime, worldAgeTicks);
  4. maps every block-changing record to the nearest frame of each bot;
  5. runs the capture-quality checks (requirement ids T4, T8, T12-T17 of the
     repository's docs/requirements.md):
     absolute yaw/pitch in every frame, `tick` / `entities` continuity,
     episode time + weather, camera parameters (fov 70, fovEffectScale 0),
     rendered-camera vs bot angle error, mutual visibility, damage, arm
     swings before damage, per-frame state fields, dig progress before breaks.

Every episode gets a status: FAIL (errors), WARN (warnings) or OK. The
--report JSON holds all per-episode stats plus a batch-level summary.

DIR can be a batch output directory (.../batch_0/output), a batch directory
(.../batch_0, its output/ is used) or a prepared dataset directory. Aligned
camera metas are read from <batch>/aligned (or DIR itself for a dataset),
raw camera metas / frame sidecars from <batch>/camera.
Only the Python standard library is used.

Examples:
  python postprocess/verify_world_events.py output/data_collection/train/batch_0
  python postprocess/verify_world_events.py OUT --episode 3 --report report.json
  python postprocess/verify_world_events.py OUT --alignment-dir aligned_events/
"""
from __future__ import annotations

import argparse
import bisect
import gzip
import json
import math
import re
import statistics
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

EPISODE_KEY_RE = re.compile(r"(?:^|_)(?P<episode>\d{6})_(?P<who>world|Alpha|Bravo)_instance_(?P<instance>\d{3})")
ACTION_RE = re.compile(r"_(?P<episode>\d{6})_(?P<bot>Alpha|Bravo)_instance_(?P<instance>\d{3})\.json$")

REPORT_FORMAT = "solaris-world-events-report-v2"

# Records that describe (or confirm) a change of block state
BLOCK_RECORD_TYPES = {
    "block_place", "block_break", "block_burn", "block_ignite", "block_fade",
    "block_form", "block_spread", "block_grow", "liquid_flow", "fluid_level_change",
    "block_explode", "entity_explode", "tnt_prime", "leaves_decay", "piston_extend",
    "piston_retract", "entity_change_block", "structure_grow", "block_fertilize",
    "sponge_absorb", "bucket_empty", "bucket_fill", "block_state",
}

# Episodes in which damage must really happen (T14)
DAMAGE_EPISODE_TYPES = {"pvp", "pve", "pveMobs", "pvpMelee", "pvpRanged"}

# Per-frame player state (T16). Required keys produce a WARN when missing;
# optional keys are only counted.
STATE_REQUIRED_KEYS = ("health", "food", "hotbar", "selected_slot", "cursor")
STATE_OPTIONAL_KEYS = ("saturation", "offhand", "armor", "xp_level", "xp_progress", "oxygen",
                       "effects", "body_yaw", "head_yaw", "on_ground", "sneaking", "sprinting",
                       "flying", "in_water", "alive")
CURSOR_KEYS = ("block", "entity")
PEER_REQUIRED_KEYS = ("name", "known", "pos", "yaw", "pitch", "head_yaw", "distance")

# Blocks that break on the first hit (hardness 0): no dig progress is expected (T17)
INSTANT_BREAK_BLOCKS = {
    "short_grass", "grass", "tall_grass", "fern", "large_fern", "dead_bush", "seagrass",
    "tall_seagrass", "torch", "wall_torch", "soul_torch", "soul_wall_torch", "redstone_torch",
    "redstone_wall_torch", "redstone_wire", "repeater", "comparator", "tnt", "fire", "soul_fire",
    "sugar_cane", "wheat", "carrots", "potatoes", "beetroots", "nether_wart", "melon_stem",
    "pumpkin_stem", "attached_melon_stem", "attached_pumpkin_stem", "sweet_berry_bush",
    "lily_pad", "brown_mushroom", "red_mushroom", "crimson_fungus", "warped_fungus",
    "crimson_roots", "warped_roots", "nether_sprouts", "kelp", "kelp_plant", "slime_block",
    "honey_block", "scaffolding", "flower_pot", "tripwire", "tripwire_hook", "end_rod",
    "frogspawn", "moss_carpet", "pink_petals", "hanging_roots", "small_dripleaf",
    "big_dripleaf_stem", "spore_blossom", "cave_vines", "cave_vines_plant", "twisting_vines",
    "twisting_vines_plant", "weeping_vines", "weeping_vines_plant", "glow_lichen",
    "structure_void", "bubble_column", "light", "decorated_pot", "torchflower_crop",
    "pitcher_crop", "cocoa", "vine",
}
INSTANT_BREAK_SUFFIXES = ("_sapling", "_tulip", "_flower", "_coral", "_coral_fan",
                          "_coral_wall_fan", "potted_")
INSTANT_BREAK_FLOWERS = {
    "dandelion", "poppy", "blue_orchid", "allium", "azure_bluet", "oxeye_daisy", "cornflower",
    "lily_of_the_valley", "wither_rose", "sunflower", "lilac", "rose_bush", "peony",
    "torchflower", "pitcher_plant",
}

EYE_HEIGHT = 1.62
BODY_CENTER = 0.9  # target point on the other player: feet + half of the 1.8 bbox


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------

def is_num(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


def wrap180(d: float) -> float:
    """Wrap an angle difference in degrees to [-180, 180)."""
    return (d + 180.0) % 360.0 - 180.0


def mf_yaw_to_mc_deg(yaw_rad: float) -> float:
    """Mineflayer yaw (rad, 0 = north, +west) -> Minecraft yaw in degrees [-180, 180)."""
    return wrap180(math.degrees(math.pi - yaw_rad))


def mf_pitch_to_mc_deg(pitch_rad: float) -> float:
    """Mineflayer pitch (rad, + = up) -> Minecraft pitch in degrees (+90 = down)."""
    return -math.degrees(pitch_rad)


def percentiles(values: List[float]) -> Optional[Dict[str, float]]:
    """n / mean / p50 / p90 / p99 / max (nearest-rank) of a list of numbers."""
    if not values:
        return None
    v = sorted(values)

    def q(p: float) -> float:
        return v[min(len(v) - 1, int(math.ceil(p * len(v))) - 1)] if p > 0 else v[0]

    return {"n": len(v), "mean": round(sum(v) / len(v), 6), "p50": round(q(0.5), 6),
            "p90": round(q(0.9), 6), "p99": round(q(0.99), 6), "max": round(v[-1], 6)}


def player_name(v: Any) -> Optional[str]:
    """`player` / `attacker` fields may be a bare name or an entity object."""
    if isinstance(v, str):
        return v
    if isinstance(v, dict):
        n = v.get("name")
        return n if isinstance(n, str) else None
    return None


def bare_block_id(v: Any) -> str:
    if not isinstance(v, str):
        return ""
    v = v.split("[", 1)[0]
    return v.split(":", 1)[1] if ":" in v else v


def is_instant_break_block(block: Any) -> bool:
    b = bare_block_id(block)
    if not b:
        return False
    if b in INSTANT_BREAK_BLOCKS or b in INSTANT_BREAK_FLOWERS:
        return True
    return any(b.endswith(s) or b.startswith(s) for s in INSTANT_BREAK_SUFFIXES)


def truthy_flag(rec: Optional[Dict[str, Any]], *keys: str) -> bool:
    return bool(rec) and any(rec.get(k) is True for k in keys)


def continuity(game_times: List[int], every: int) -> Dict[str, Any]:
    """Gaps in a sorted list of game times that should be `every` ticks apart."""
    gts = sorted(set(game_times))
    out: Dict[str, Any] = {"records": len(game_times), "distinct_game_times": len(gts),
                           "every": every, "missing_ticks": 0, "gaps": 0, "max_gap": 0,
                           "gap_examples": []}
    if not gts:
        return out
    out["span"] = [gts[0], gts[-1]]
    for a, b in zip(gts, gts[1:]):
        gap = b - a
        out["max_gap"] = max(out["max_gap"], gap)
        if gap > every:
            out["gaps"] += 1
            out["missing_ticks"] += int(math.ceil(gap / every)) - 1
            if len(out["gap_examples"]) < 10:
                out["gap_examples"].append([a, b])
    return out


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------

def load_events(path: Path) -> Tuple[List[Dict[str, Any]], int]:
    """Load a whole events file (kept for external users; the checks stream)."""
    events, bad = [], 0
    with path.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                bad += 1
    return events, bad


def rle_decode(rle: List[int], total: int) -> List[int]:
    out: List[int] = []
    for i in range(0, len(rle), 2):
        out.extend([rle[i + 1]] * rle[i])
    if len(out) != total:
        raise ValueError(f"RLE length {len(out)} != expected {total}")
    return out


def load_snapshot(path: Path) -> Dict[str, Any]:
    """Return the snapshot dict plus 'blocks' (dense palette indices, YZX order)."""
    with gzip.open(path, "rt", encoding="utf-8") as fh:
        snap = json.load(fh)
    sx, sy, sz = snap["size"]
    snap["blocks"] = rle_decode(snap["rle"], sx * sy * sz)
    snap["biomes"] = rle_decode(snap["biomes_rle"], sx * sz)
    return snap


def snapshot_index(snap: Dict[str, Any], x: int, y: int, z: int) -> Optional[int]:
    ox, oy, oz = snap["origin"]
    sx, sy, sz = snap["size"]
    if not (ox <= x < ox + sx and oy <= y < oy + sy and oz <= z < oz + sz):
        return None
    return ((y - oy) * sz + (z - oz)) * sx + (x - ox)


def block_at(snap: Dict[str, Any], x: int, y: int, z: int) -> Optional[str]:
    i = snapshot_index(snap, x, y, z)
    return None if i is None else snap["palette"][snap["blocks"][i]]


def load_json(path: Optional[Path]) -> Optional[Any]:
    if path is None or not path.exists():
        return None
    try:
        with path.open("r", encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, json.JSONDecodeError):
        return None


# ---------------------------------------------------------------------------
# Streaming scan of the events file
# ---------------------------------------------------------------------------

class EventScan:
    """Everything the checks need from one pass over a (possibly large) log."""

    def __init__(self) -> None:
        self.records = 0
        self.bad_lines = 0
        self.counts: Counter = Counter()
        self.first: Optional[Dict[str, Any]] = None
        self.last_type: Optional[str] = None
        self.last_epoch_ms: Optional[float] = None
        self.begin: Dict[str, Any] = {}
        self.seq_ok = True
        self._last_seq: Optional[int] = None
        self.gt_min: Optional[int] = None
        self.gt_max: Optional[int] = None
        self.tick_points: List[Tuple[float, int]] = []        # (epoch_ms, game_time)
        self.entity_gts: List[int] = []
        self.entity_sizes: List[int] = []
        self.entity_kinds: Counter = Counter()
        self.block_states: Dict[Tuple[int, int, int], str] = {}
        self.change_events: List[Tuple[int, float]] = []      # (seq, epoch_ms)
        # server-side camera vs target per target bot (tick.cameras), lists of abs values:
        # follow error (camera pose vs the pose commanded at the last sync: yaw, pitch, pos),
        # one-tick input lag (dyaw/dpitch_to_target, sampled before this tick's sync), and
        # lag-compensated error (camera now vs the target's pose in the previous tick record)
        self.cam_follow: Dict[str, Dict[str, List[float]]] = defaultdict(lambda: defaultdict(list))
        self.cam_input: Dict[str, Dict[str, List[float]]] = defaultdict(lambda: defaultdict(list))
        self.cam_lagcomp: Dict[str, Dict[str, List[float]]] = defaultdict(lambda: defaultdict(list))
        self._prev_tick: Optional[Tuple[int, Dict[str, Tuple[float, float]]]] = None
        self.cam_flying: Counter = Counter()
        self.cam_gamemode: Dict[str, Counter] = defaultdict(Counter)
        self.cam_ticks: Counter = Counter()
        self.player_gamemode: Dict[str, str] = {}
        self.damage: List[Dict[str, Any]] = []
        self.swings: Dict[str, List[float]] = defaultdict(list)
        self.deaths = 0
        # dig progress per position: {"player", "start", "stages", "insta"}
        self._dig: Dict[Tuple[int, int, int], Dict[str, Any]] = {}
        self.breaks: List[Dict[str, Any]] = []
        self.dig_records = 0

    # -- record handlers ---------------------------------------------------
    def feed(self, ev: Dict[str, Any]) -> None:
        self.records += 1
        t = ev.get("type")
        self.counts[t] += 1
        if self.first is None:
            self.first = ev
            if t == "episode_begin":
                self.begin = ev
        self.last_type = t
        if is_num(ev.get("epoch_ms")):
            self.last_epoch_ms = float(ev["epoch_ms"])
        seq = ev.get("seq", -1)
        if self._last_seq is not None and not (isinstance(seq, int) and seq > self._last_seq):
            self.seq_ok = False
        self._last_seq = seq if isinstance(seq, int) else self._last_seq
        gt = ev.get("game_time")
        if isinstance(gt, int):
            self.gt_min = gt if self.gt_min is None else min(self.gt_min, gt)
            self.gt_max = gt if self.gt_max is None else max(self.gt_max, gt)
        if t in BLOCK_RECORD_TYPES and is_num(ev.get("epoch_ms")):
            self.change_events.append((seq, float(ev["epoch_ms"])))
        handler = getattr(self, "_on_" + str(t), None)
        if handler is not None:
            handler(ev)

    def _on_tick(self, ev: Dict[str, Any]) -> None:
        if is_num(ev.get("epoch_ms")) and isinstance(ev.get("game_time"), int):
            self.tick_points.append((float(ev["epoch_ms"]), ev["game_time"]))
        poses: Dict[str, Tuple[float, float]] = {}
        for p in ev.get("players") or []:
            if isinstance(p, dict) and p.get("name") and p.get("gamemode") is not None:
                self.player_gamemode[p["name"]] = str(p["gamemode"]).lower()
            if isinstance(p, dict) and p.get("name") and is_num(p.get("yaw")) and is_num(p.get("pitch")):
                poses[p["name"]] = (float(p["yaw"]), float(p["pitch"]))
        gt = ev.get("game_time")
        prev = None
        if self._prev_tick is not None and isinstance(gt, int) and gt - self._prev_tick[0] == 1:
            prev = self._prev_tick[1]
        self._prev_tick = (gt, poses) if isinstance(gt, int) else None
        for c in ev.get("cameras") or []:
            if not isinstance(c, dict):
                continue
            target = c.get("target") or c.get("name") or "?"
            self.cam_ticks[target] += 1
            if c.get("flying"):
                self.cam_flying[target] += 1
            if c.get("gamemode") is not None:
                self.cam_gamemode[target][str(c["gamemode"])] += 1
            dy, dp = c.get("dyaw_since_sync"), c.get("dpitch_since_sync")
            if is_num(dy) and is_num(dp):
                f = self.cam_follow[target]
                f["yaw"].append(abs(wrap180(float(dy))))
                f["pitch"].append(abs(float(dp)))
                if is_num(c.get("dpos_since_sync")):
                    f["pos"].append(abs(float(c["dpos_since_sync"])))
            dy, dp = c.get("dyaw_to_target"), c.get("dpitch_to_target")
            if is_num(dy) and is_num(dp):
                f = self.cam_input[target]
                f["yaw"].append(abs(wrap180(float(dy))))
                f["pitch"].append(abs(float(dp)))
            if prev is not None and target in prev and is_num(c.get("yaw")) and is_num(c.get("pitch")):
                f = self.cam_lagcomp[target]
                f["yaw"].append(abs(wrap180(float(c["yaw"]) - prev[target][0])))
                f["pitch"].append(abs(float(c["pitch"]) - prev[target][1]))

    def _on_entities(self, ev: Dict[str, Any]) -> None:
        if isinstance(ev.get("game_time"), int):
            self.entity_gts.append(ev["game_time"])
        ents = ev.get("entities") or []
        self.entity_sizes.append(len(ents))
        for e in ents:
            if isinstance(e, dict):
                self.entity_kinds[e.get("kind") or e.get("type")] += 1

    def _on_block_state(self, ev: Dict[str, Any]) -> None:
        pos = ev.get("pos")
        if isinstance(pos, list) and len(pos) == 3:
            self.block_states[(pos[0], pos[1], pos[2])] = ev.get("state")

    def _on_entity_damage(self, ev: Dict[str, Any]) -> None:
        victim = ev.get("victim") or {}
        self.damage.append({
            "seq": ev.get("seq"), "epoch_ms": ev.get("epoch_ms"), "server_tick": ev.get("server_tick"),
            "victim": player_name(victim), "victim_type": victim.get("type") if isinstance(victim, dict) else None,
            "attacker": player_name(ev.get("attacker")), "projectile": ev.get("projectile") is not None,
            "cause": ev.get("cause"), "final_damage": ev.get("final_damage"),
            "health_after": ev.get("health_after"),
        })

    def _on_entity_death(self, ev: Dict[str, Any]) -> None:
        self.deaths += 1

    def _on_arm_swing(self, ev: Dict[str, Any]) -> None:
        name = player_name(ev.get("player"))
        if name and is_num(ev.get("epoch_ms")):
            self.swings[name].append(float(ev["epoch_ms"]))

    @staticmethod
    def _pos(ev: Dict[str, Any]) -> Optional[Tuple[int, int, int]]:
        pos = ev.get("pos")
        if isinstance(pos, list) and len(pos) == 3:
            return (int(pos[0]), int(pos[1]), int(pos[2]))
        return None

    def _on_dig_start(self, ev: Dict[str, Any]) -> None:
        self.dig_records += 1
        pos = self._pos(ev)
        if pos is not None:
            self._dig[pos] = {"player": player_name(ev.get("player")), "start": True, "stages": [],
                              "insta": truthy_flag(ev, "insta_break", "instaBreak", "instant")}

    def _on_dig_progress(self, ev: Dict[str, Any]) -> None:
        self.dig_records += 1
        pos = self._pos(ev)
        if pos is None:
            return
        st = self._dig.setdefault(pos, {"player": player_name(ev.get("player")), "start": False,
                                        "stages": [], "insta": False})
        if is_num(ev.get("stage")):
            st["stages"].append(ev["stage"])

    def _on_dig_abort(self, ev: Dict[str, Any]) -> None:
        self.dig_records += 1
        pos = self._pos(ev)
        if pos is not None:
            self._dig.pop(pos, None)

    def _on_dig_finish(self, ev: Dict[str, Any]) -> None:
        self.dig_records += 1

    def _on_block_break(self, ev: Dict[str, Any]) -> None:
        player = player_name(ev.get("player"))
        pos = self._pos(ev)
        if player is None or pos is None:
            return
        st = self._dig.pop(pos, None)
        if st is not None and st.get("player") and st["player"] != player:
            st = None  # someone else was digging this block
        excluded = None
        if truthy_flag(ev, "insta_break", "instaBreak", "instant") or (st and st.get("insta")):
            excluded = "insta_break flag"
        elif self.player_gamemode.get(player) == "creative":
            excluded = "creative"
        elif is_instant_break_block(ev.get("old_type") or ev.get("old_state")):
            excluded = "instant-break block"
        stages = st["stages"] if st else []
        self.breaks.append({
            "seq": ev.get("seq"), "pos": list(pos), "player": player, "block": ev.get("old_type"),
            "excluded": excluded, "has_dig": bool(st and (st["start"] or stages)),
            "increasing": bool(stages) and all(b >= a for a, b in zip(stages, stages[1:])),
            "stages": len(stages),
        })


def scan_events(path: Path) -> EventScan:
    sc = EventScan()
    with path.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                sc.bad_lines += 1
                continue
            if isinstance(ev, dict):
                sc.feed(ev)
            else:
                sc.bad_lines += 1
    return sc


# ---------------------------------------------------------------------------
# Replay
# ---------------------------------------------------------------------------

def replay_block_states(start: Dict[str, Any], block_states) -> Dict[Tuple[int, int, int], str]:
    """Apply `block_state` records to the start snapshot; returns {pos: state} overrides.

    `block_states` is either {pos: last state} (from EventScan) or a list of records.
    """
    overrides: Dict[Tuple[int, int, int], str] = {}
    if isinstance(block_states, dict):
        items = block_states.items()
    else:
        items = ((tuple(ev["pos"]), ev["state"]) for ev in block_states if ev.get("type") == "block_state")
    for (x, y, z), state in items:
        if snapshot_index(start, x, y, z) is not None:
            overrides[(x, y, z)] = state
    return overrides


def compare_with_end(start: Dict[str, Any], end: Dict[str, Any], overrides) -> Tuple[int, List[Dict[str, Any]]]:
    if start["origin"] != end["origin"] or start["size"] != end["size"]:
        return -1, [{"error": "start/end boxes differ"}]
    ox, oy, oz = start["origin"]
    sx, sy, sz = start["size"]
    sp, ep = start["palette"], end["palette"]
    sb, eb = start["blocks"], end["blocks"]
    mismatches, examples = 0, []
    i = 0
    for y in range(oy, oy + sy):
        for z in range(oz, oz + sz):
            for x in range(ox, ox + sx):
                want = overrides.get((x, y, z), sp[sb[i]])
                have = ep[eb[i]]
                if want != have:
                    mismatches += 1
                    if len(examples) < 10:
                        examples.append({"pos": [x, y, z], "replayed": want, "end_snapshot": have})
                i += 1
    return mismatches, examples


# ---------------------------------------------------------------------------
# Time alignment
# ---------------------------------------------------------------------------

class TickMap:
    """Piecewise-linear epoch_ms <-> game_time map from `tick` records.

    Accepts a list of (epoch_ms, game_time) points or a list of records.
    """

    def __init__(self, source):
        if source and isinstance(source[0], dict):
            source = [(e["epoch_ms"], e["game_time"]) for e in source if e.get("type") == "tick"]
        pts = sorted(set(source))
        self.ms = [p[0] for p in pts]
        self.gt = [p[1] for p in pts]

    def __bool__(self) -> bool:
        return len(self.ms) >= 2

    def game_time_at(self, epoch_ms: float) -> float:
        ms, gt = self.ms, self.gt
        j = bisect.bisect_left(ms, epoch_ms)
        if j <= 0:
            return gt[0] + (epoch_ms - ms[0]) / 50.0
        if j >= len(ms):
            return gt[-1] + (epoch_ms - ms[-1]) / 50.0
        t0, t1 = ms[j - 1], ms[j]
        g0, g1 = gt[j - 1], gt[j]
        if t1 == t0:
            return float(g1)
        return g0 + (g1 - g0) * (epoch_ms - t0) / (t1 - t0)


def frame_times(frames: List[Dict[str, Any]]) -> List[float]:
    return [float(f["epochTime"]) * 1000.0 for f in frames]


def nearest_frame(times_ms: List[float], t_ms: float) -> int:
    j = bisect.bisect_left(times_ms, t_ms)
    if j <= 0:
        return 0
    if j >= len(times_ms):
        return len(times_ms) - 1
    return j if abs(times_ms[j] - t_ms) < abs(times_ms[j - 1] - t_ms) else j - 1


def first_frame_at_or_after(times_ms: List[float], t_ms: float) -> Optional[int]:
    """The first frame whose action time is >= t (the frame that can show the change)."""
    j = bisect.bisect_left(times_ms, t_ms)
    return j if j < len(times_ms) else None


# ---------------------------------------------------------------------------
# Bot angle track (for T12)
# ---------------------------------------------------------------------------

class AngleTrack:
    """Bot yaw/pitch over time in Minecraft degrees, yaw unwrapped for interpolation."""

    def __init__(self, frames: List[Dict[str, Any]]):
        pts = []
        for f in frames:
            if is_num(f.get("yaw")) and is_num(f.get("pitch")) and is_num(f.get("epochTime")):
                pts.append((float(f["epochTime"]) * 1000.0, mf_yaw_to_mc_deg(f["yaw"]),
                            mf_pitch_to_mc_deg(f["pitch"])))
        pts.sort(key=lambda p: p[0])
        self.t = [p[0] for p in pts]
        self.pitch = [p[2] for p in pts]
        self.yaw: List[float] = []
        for _, y, _ in pts:
            if self.yaw:
                y = self.yaw[-1] + wrap180(y - self.yaw[-1])
            self.yaw.append(y)

    def __len__(self) -> int:
        return len(self.t)

    def sample_sorted(self, queries: List[float]) -> List[Optional[Tuple[float, float]]]:
        """Linear interpolation at ascending query times (None outside the track)."""
        out: List[Optional[Tuple[float, float]]] = []
        t, n, j = self.t, len(self.t), 0
        for q in queries:
            if n < 2 or q < t[0] or q > t[-1]:
                out.append(None)
                continue
            while j < n - 2 and t[j + 1] < q:
                j += 1
            t0, t1 = t[j], t[j + 1]
            w = 0.0 if t1 == t0 else (q - t0) / (t1 - t0)
            out.append((self.yaw[j] + (self.yaw[j + 1] - self.yaw[j]) * w,
                        self.pitch[j] + (self.pitch[j + 1] - self.pitch[j]) * w))
        return out


def angle_errors(cam: List[Tuple[float, float, float]], track: AngleTrack,
                 lag_ms: float) -> Tuple[List[float], List[float], List[float]]:
    """|dyaw|, |dpitch|, max of both between rendered pose at t and bot pose at t - lag."""
    vals = track.sample_sorted([c[0] - lag_ms for c in cam])
    ey, ep, ec = [], [], []
    for (_, cy, cp), v in zip(cam, vals):
        if v is None:
            continue
        dy = abs(wrap180(cy - v[0]))
        dp = abs(cp - v[1])
        ey.append(dy)
        ep.append(dp)
        ec.append(max(dy, dp))
    return ey, ep, ec


def rendered_vs_bot(cam: List[Tuple[float, float, float]], track: AngleTrack,
                    lag_range: Tuple[int, int, int] = (-250, 1000, 10),
                    max_search_points: int = 3000) -> Dict[str, Any]:
    """Rendered camera pose vs interpolated bot pose, at lag 0 and at the best lag."""
    out: Dict[str, Any] = {"frames": len(cam)}
    ey, ep, ec = angle_errors(cam, track, 0.0)
    out["lag0"] = {"yaw": percentiles(ey), "pitch": percentiles(ep), "combined": percentiles(ec)}
    stride = max(1, len(cam) // max_search_points)
    sub = cam[::stride]
    best_lag, best_mean = 0, None
    lo, hi, step = lag_range
    for lag in range(lo, hi + 1, step):
        _, _, c = angle_errors(sub, track, float(lag))
        if len(c) < max(3, len(sub) // 2):
            continue  # lag shifts most samples out of the action span
        m = sum(c) / len(c)
        if best_mean is None or m < best_mean - 1e-12:
            best_lag, best_mean = lag, m
    ey, ep, ec = angle_errors(cam, track, float(best_lag))
    out["best_lag_ms"] = best_lag
    out["best_lag"] = {"yaw": percentiles(ey), "pitch": percentiles(ep), "combined": percentiles(ec)}
    out["_combined_best"] = ec
    return out


# ---------------------------------------------------------------------------
# Mutual visibility (T8)
# ---------------------------------------------------------------------------

def in_view(eye: Tuple[float, float, float], yaw: float, pitch: float, target: Tuple[float, float, float],
            tan_h: float, tan_v: float, max_dist: float) -> Tuple[bool, float]:
    """Is `target` inside the view frustum of a camera at `eye` (Mineflayer yaw/pitch, rad)?"""
    dx, dy, dz = target[0] - eye[0], target[1] - eye[1], target[2] - eye[2]
    dist = math.sqrt(dx * dx + dy * dy + dz * dz)
    if dist > max_dist or dist < 1e-6:
        return False, dist
    cy, sy, cp, sp = math.cos(yaw), math.sin(yaw), math.cos(pitch), math.sin(pitch)
    # forward (yaw 0 = -Z, +yaw turns west), right, up
    f = (-sy * cp, sp, -cy * cp)
    r = (cy, 0.0, -sy)
    u = (r[1] * f[2] - r[2] * f[1], r[2] * f[0] - r[0] * f[2], r[0] * f[1] - r[1] * f[0])
    zc = dx * f[0] + dy * f[1] + dz * f[2]
    if zc <= 0:
        return False, dist
    xc = dx * r[0] + dy * r[1] + dz * r[2]
    yc = dx * u[0] + dy * u[1] + dz * u[2]
    return abs(xc) <= tan_h * zc and abs(yc) <= tan_v * zc, dist


def visibility(frames: List[Dict[str, Any]], other: List[Dict[str, Any]], vfov_deg: float,
               aspect: float, max_dist: float, max_dt_ms: float = 500.0) -> Dict[str, Any]:
    tan_v = math.tan(math.radians(vfov_deg) / 2.0)
    tan_h = tan_v * aspect
    o_pts = [(float(f["epochTime"]) * 1000.0, f) for f in other
             if is_num(f.get("epochTime")) and all(is_num(f.get(k)) for k in ("x", "y", "z"))]
    o_pts.sort(key=lambda p: p[0])
    o_t = [p[0] for p in o_pts]
    n = vis = 0
    dists: List[float] = []
    for f in frames:
        if not all(is_num(f.get(k)) for k in ("x", "y", "z", "yaw", "pitch", "epochTime")) or not o_t:
            continue
        t = float(f["epochTime"]) * 1000.0
        j = nearest_frame(o_t, t)
        if abs(o_t[j] - t) > max_dt_ms:
            continue
        o = o_pts[j][1]
        ok, dist = in_view((f["x"], f["y"] + EYE_HEIGHT, f["z"]), f["yaw"], f["pitch"],
                           (o["x"], o["y"] + BODY_CENTER, o["z"]), tan_h, tan_v, max_dist)
        n += 1
        vis += ok
        dists.append(dist)
    return {"frames": n, "visible": vis, "fraction": round(vis / n, 4) if n else None,
            "median_distance": round(statistics.median(dists), 3) if dists else None}


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------

def resolve_dir(d: Path) -> Path:
    if (d / "output").is_dir():
        return d / "output"
    return d


def batch_dir_of(output_dir: Path) -> Optional[Path]:
    """The batch directory (parent of output/, has camera/ and aligned/), if any."""
    if output_dir.name == "output":
        return output_dir.parent
    return None


def discover(d: Path, aligned_dir: Optional[Path] = None):
    groups: Dict[Tuple[str, str], Dict[str, Any]] = defaultdict(
        lambda: {"actions": {}, "info": {}, "camera_meta": {}})
    dirs = [d] + ([aligned_dir] if aligned_dir is not None and aligned_dir.is_dir()
                  and aligned_dir.resolve() != d.resolve() else [])
    for base in dirs:
        for p in sorted(base.iterdir()):
            if not p.is_file():
                continue
            name = p.name
            m = EPISODE_KEY_RE.search(name)
            if not m:
                continue
            key = (m.group("episode"), m.group("instance"))
            g = groups[key]
            if name.endswith("_camera_meta.json"):
                if m.group("who") != "world":
                    g["camera_meta"][m.group("who")] = p
                continue
            if base is not d:
                continue
            if name.endswith("_world_events.jsonl"):
                g["events"] = p
            elif name.endswith("_world_snapshot_start.json.gz"):
                g["snap_start"] = p
            elif name.endswith("_world_snapshot_end.json.gz"):
                g["snap_end"] = p
            elif name.endswith("_episode_info.json"):
                g["info"][m.group("who")] = p
            elif name.endswith("_meta.json"):
                continue
            else:
                a = ACTION_RE.search(name)
                if a:
                    g["actions"][a.group("bot")] = p
    return groups


class CameraSources:
    """Raw camera metas and frame sidecars under <batch>/camera (cached per bot/instance)."""

    def __init__(self, camera_dir: Optional[Path]):
        self.camera_dir = camera_dir
        self._metas: Dict[Tuple[str, str], List[Dict[str, Any]]] = {}
        self._sidecar: Dict[str, Optional[List[Tuple[float, float, float, Any]]]] = {}

    def _dir(self, bot: str, instance: str) -> Optional[Path]:
        if self.camera_dir is None:
            return None
        return self.camera_dir / f"output_{bot.lower()}" / str(int(instance))

    def metas(self, bot: str, instance: str) -> List[Dict[str, Any]]:
        """camera_<bot>_meta.json plus the metas of restarted cameras (camera_<bot>_r<n>_meta.json)."""
        key = (bot, instance)
        if key not in self._metas:
            out: List[Dict[str, Any]] = []
            d = self._dir(bot, instance)
            if d is not None and d.is_dir():
                stem = f"camera_{bot.lower()}"
                paths = [d / f"{stem}_meta.json"] + sorted(d.glob(f"{stem}_r*_meta.json"))
                for path in paths:
                    m = load_json(path) if path.exists() else None
                    if isinstance(m, dict):
                        m["_dir"] = str(d)
                        m["_file"] = path.name
                        out.append(m)
            self._metas[key] = out
        return self._metas[key]

    def meta(self, bot: str, instance: str,
             span_ms: Optional[Tuple[float, float]] = None) -> Optional[Dict[str, Any]]:
        """The camera meta whose recording overlaps ``span_ms`` (epoch ms) the most.

        Without a span, or when nothing overlaps, the first one (normally
        camera_<bot>_meta.json) is returned.
        """
        metas = self.metas(bot, instance)
        if not metas:
            return None
        if span_ms is None or len(metas) == 1:
            return metas[0]
        best, best_overlap = metas[0], -1.0
        for m in metas:
            start = float(m["start_epoch_seconds"]) * 1000.0 if is_num(m.get("start_epoch_seconds")) else 0.0
            if is_num(m.get("stop_epoch_seconds")):
                stop = float(m["stop_epoch_seconds"]) * 1000.0
            elif is_num(m.get("frames")) and is_num(m.get("fps")) and m["fps"] > 0:
                stop = start + float(m["frames"]) / float(m["fps"]) * 1000.0
            else:
                stop = float("inf")
            overlap = min(span_ms[1], stop) - max(span_ms[0], start)
            if overlap >= 0 and overlap > best_overlap:
                best, best_overlap = m, overlap
        return best

    def sidecar(self, bot: str, instance: str,
                span_ms: Optional[Tuple[float, float]] = None) -> Optional[List[Tuple[float, float, float, Any, Any]]]:
        """[(epoch_ms, cam_yaw, cam_pitch, fov, pose_info)] sorted by time, or None.

        ``pose_info`` is ``pose_fields(line)`` (pose-link fields of the Windows camera mod) or None.
        """
        meta = self.meta(bot, instance, span_ms)
        if not meta:
            return None
        key = str(Path(meta["_dir"]) / meta["_file"])
        if key in self._sidecar:
            return self._sidecar[key]
        recs = None
        if meta.get("frames_file"):
            d = Path(meta["_dir"])
            raw = str(meta["frames_file"])
            p = Path(raw)
            if not p.is_absolute() or not p.exists():
                p = d / re.split(r"[\\/]", raw)[-1]
            if p.exists():
                recs = []
                with p.open("r", encoding="utf-8") as fh:
                    for line in fh:
                        try:
                            r = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        if is_num(r.get("epoch_ms")) and is_num(r.get("cam_yaw")) and is_num(r.get("cam_pitch")):
                            recs.append((float(r["epoch_ms"]), float(r["cam_yaw"]), float(r["cam_pitch"]),
                                         r.get("fov"), pose_fields(r)))
                recs.sort(key=lambda x: x[0])
        self._sidecar[key] = recs
        return recs


    def pose_info_by_time(self, bot: str, instance: str,
                          span_ms: Optional[Tuple[float, float]] = None) -> Dict[float, Dict[str, Any]]:
        """{round(epoch_ms, 3): pose_info} from the raw sidecar (to complete older aligned metas)."""
        side = self.sidecar(bot, instance, span_ms) or []
        return {round(r[0], 3): r[4] for r in side if r[4] is not None}


def pose_fields(rec: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Pose-link fields of a sidecar line / frame_mapping entry, or None if it has none (T12)."""
    src = rec.get("pose_source")
    if src is None:
        return None
    return {"source": src, "age_ms": rec.get("pose_age_ms"), "t": rec.get("pose_t"),
            "seq": rec.get("pose_seq"), "held": bool(rec.get("pose_held")),
            "fallback": rec.get("pose_fallback"),
            "player_yaw": rec.get("player_yaw"), "player_pitch": rec.get("player_pitch")}


def label_angles(f: Dict[str, Any]) -> Optional[Tuple[float, float]]:
    """(yaw, pitch) of an action sample in Minecraft degrees, or None."""
    if is_num(f.get("yaw")) and is_num(f.get("pitch")):
        return mf_yaw_to_mc_deg(f["yaw"]), mf_pitch_to_mc_deg(f["pitch"])
    return None


def pair_entry(cy: float, cp: float, info: Optional[Dict[str, Any]], label: Tuple[float, float],
               kind: Optional[str] = None) -> Tuple[float, str, Optional[float], Optional[str]]:
    """(frame-vs-label error, pose_source, server-followed look vs label, pose_pair kind)."""
    err = max(abs(wrap180(cy - label[0])), abs(cp - label[1]))
    srv = None
    if info and is_num(info.get("player_yaw")) and is_num(info.get("player_pitch")):
        srv = max(abs(wrap180(float(info["player_yaw"]) - label[0])), abs(float(info["player_pitch"]) - label[1]))
    return err, ((info or {}).get("source") or "unknown"), srv, kind


def simulate_pairs(frames: List[Dict[str, Any]],
                   side: List[Tuple[float, float, float, Any, Any]]) -> List[Tuple[float, str, Optional[float],
                                                                               Optional[str]]]:
    """Frame-vs-label pairs as process_recordings.py makes them, from the raw sidecar.

    Each action sample is paired with the first frame captured at or after it; with the
    pose link (``pose_seq`` in the sample and in the sidecar) a frame that rendered another
    packet is replaced by the neighbouring frame that rendered the sample's packet.
    """
    times = [r[0] for r in side]

    def seq_of(k: int) -> Optional[int]:
        if 0 <= k < len(side):
            inf = side[k][4]
            if inf and inf.get("source") == "injected" and isinstance(inf.get("seq"), int):
                return inf["seq"]
        return None

    out = []
    for f in frames:
        label = label_angles(f)
        if label is None or not is_num(f.get("epochTime")):
            continue
        t = float(f["epochTime"]) * 1000.0
        k = bisect.bisect_left(times, t)
        if k >= len(side):
            break
        kind = None
        seq = f.get("pose_seq")
        if isinstance(seq, int) and not isinstance(seq, bool) and seq_of(k) is not None:
            if seq_of(k) == seq:
                kind = "same"
            else:
                cands = [(abs(times[j] - t), j, name) for j, name in ((k - 1, "prev"), (k + 1, "next"))
                         if seq_of(j) == seq]
                if cands:
                    _, k, kind = min(cands)
                else:
                    kind = "none"
        out.append(pair_entry(side[k][1], side[k][2], side[k][4], label, kind))
    return out


def paired_report(pairs: List[Tuple[float, str, Optional[float], Optional[str]]],
                  threshold: float) -> Dict[str, Any]:
    """Rendered frame vs its action label (what the training data contains), split by pose_source."""
    def frac_over(v: List[float]) -> Optional[float]:
        return round(sum(1 for x in v if x >= threshold) / len(v), 4) if v else None

    errs = [p[0] for p in pairs]
    out: Dict[str, Any] = {"samples": len(pairs), "combined": percentiles(errs),
                           "frac_over_threshold": frac_over(errs), "by_source": {}}
    for src in sorted({p[1] for p in pairs}):
        v = [p[0] for p in pairs if p[1] == src]
        out["by_source"][src] = {"samples": len(v), "combined": percentiles(v), "frac_over_threshold": frac_over(v)}
    same = [p for p in pairs if p[1] == "injected" and p[2] is not None]
    if same:
        # same frames, same labels: the rendered (injected) look vs the server-followed player look
        a, b = [p[0] for p in same], [p[2] for p in same]
        out["injected_vs_server_followed_same_frames"] = {
            "samples": len(same), "injected": percentiles(a), "server_followed": percentiles(b),
            "injected_frac_over_threshold": frac_over(a), "server_followed_frac_over_threshold": frac_over(b)}
    kinds = Counter(p[3] for p in pairs if p[3])
    if kinds:
        out["pose_pair"] = dict(sorted(kinds.items()))
    return out


def pose_matched_errors(cam: List[Tuple[float, float, float]], info: List[Optional[Dict[str, Any]]],
                        frames: List[Dict[str, Any]], margin_ms: float = 5.0) -> List[float]:
    """Exact rendered-vs-bot check for injected frames (no interpolation of the bot track).

    An injected frame rendered the bot's movement packet ``pose_seq`` sent at ``pose_t``; when the
    camera was set up (``pose_t + pose_age_ms``) no newer packet had arrived, so the bot's
    pose was that packet during [pose_t, setup - margin]. Every action sample taken inside such
    a window must show exactly the rendered yaw/pitch. Returns max(|dyaw|, |dpitch|) per sample.
    """
    windows: Dict[Any, List[float]] = {}
    for (_, cy, cp), inf in zip(cam, info):
        if not inf or inf.get("source") != "injected" or not is_num(inf.get("t")) or not is_num(inf.get("age_ms")):
            continue
        t0 = float(inf["t"])
        t1 = t0 + float(inf["age_ms"]) - margin_ms
        key = (inf.get("seq"), t0)
        w = windows.get(key)
        if w is None:
            windows[key] = [t0, t1, cy, cp]
        else:
            w[1] = max(w[1], t1)
    if not windows:
        return []
    wl = sorted(v for v in windows.values() if v[1] >= v[0])
    starts = [w[0] for w in wl]
    out: List[float] = []
    for f in frames:
        if not (is_num(f.get("yaw")) and is_num(f.get("pitch")) and is_num(f.get("epochTime"))):
            continue
        s = float(f["epochTime"]) * 1000.0
        j = bisect.bisect_right(starts, s) - 1
        if j < 0 or s > wl[j][1]:
            continue
        dy = abs(wrap180(wl[j][2] - mf_yaw_to_mc_deg(f["yaw"])))
        dp = abs(wl[j][3] - mf_pitch_to_mc_deg(f["pitch"]))
        out.append(max(dy, dp))
    return out


def outdated_packet_frames(info: List[Optional[Dict[str, Any]]]) -> Dict[str, Any]:
    """Injected frames that rendered packet n although packet n+1 had already been sent.

    The send time of packet n+1 is known when some frame rendered it (its ``pose_t``). A frame
    set up at ``pose_t + pose_age_ms`` that still shows packet n while n+1 was sent before
    that is outdated by the difference (the UDP transit time, normally ~1 ms).
    """
    first_t: Dict[Any, float] = {}
    for i in info:
        if i and i.get("source") == "injected" and is_num(i.get("t")) and isinstance(i.get("seq"), int):
            first_t.setdefault(i["seq"], float(i["t"]))
    checked, late = 0, []
    for i in info:
        if not (i and i.get("source") == "injected" and is_num(i.get("t")) and is_num(i.get("age_ms"))
                and isinstance(i.get("seq"), int)):
            continue
        nxt = first_t.get(i["seq"] + 1)
        if nxt is None:
            continue
        checked += 1
        setup = float(i["t"]) + float(i["age_ms"])
        if nxt < setup:
            late.append(setup - nxt)
    return {"checked": checked, "outdated": len(late), "max_newer_packet_ms": max(late) if late else None}


def rendered_by_pose_source(cam: List[Tuple[float, float, float]], info: List[Optional[Dict[str, Any]]],
                            track: "AngleTrack", frames: List[Dict[str, Any]]) -> Dict[str, Any]:
    """T12 split of the rendered frames by pose_source (camera pose link) and the pose age."""
    counts = Counter((i or {}).get("source") or "unknown" for i in info)
    inj = [k for k, i in enumerate(info) if i and i.get("source") == "injected"]
    ages = [float(info[k]["age_ms"]) for k in inj if is_num(info[k].get("age_ms"))]
    fresh_ages = [float(info[k]["age_ms"]) for k in inj
                  if is_num(info[k].get("age_ms")) and not info[k].get("held")]
    out: Dict[str, Any] = {
        "frames": dict(sorted(counts.items())),
        "injected_fraction": round(len(inj) / len(info), 4) if info else None,
        "held_frames": sum(1 for k in inj if info[k].get("held")),
        "pose_age_ms": percentiles(ages),
        "pose_age_ms_not_held": percentiles(fresh_ages),
        "server_frame_reasons": dict(sorted(Counter(
            (i or {}).get("fallback") or "unknown" for i in info
            if not (i and i.get("source") == "injected")).items())),
        "by_source": {},
    }
    for src in sorted(counts):
        idx = [k for k, i in enumerate(info) if ((i or {}).get("source") or "unknown") == src]
        sub = [cam[k] for k in idx]
        if len(sub) < 3:
            out["by_source"][src] = {"frames": len(sub)}
            continue
        rv = rendered_vs_bot(sub, track)
        comb = rv.pop("_combined_best")
        rv["_lag0_combined"] = angle_errors(sub, track, 0.0)[2]
        if src == "injected":
            rv["_combined_best"] = comb
        out["by_source"][src] = rv
    pm = pose_matched_errors(cam, info, frames)
    out["pose_matched"] = {"samples": len(pm), "combined": percentiles(pm)}
    out["outdated"] = outdated_packet_frames(info)
    out["_pose_matched"] = pm
    out["_ages"] = ages
    return out


# ---------------------------------------------------------------------------
# Per-episode checks
# ---------------------------------------------------------------------------

def check_frame_fields(frames: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Absolute yaw/pitch in every frame (T4) and per-frame state fields (T16)."""
    bad_idx = [i for i, f in enumerate(frames) if not (is_num(f.get("yaw")) and is_num(f.get("pitch")))]
    yp = {"frames": len(frames), "missing": len(bad_idx), "first_missing_indices": bad_idx[:10]}
    missing: Counter = Counter()
    optional: Counter = Counter()
    for f in frames:
        st = f.get("state")
        if not isinstance(st, dict):
            missing["state"] += 1
        else:
            for k in STATE_REQUIRED_KEYS:
                if k not in st:
                    missing["state." + k] += 1
            cur = st.get("cursor")
            if "cursor" in st and not (isinstance(cur, dict) and all(k in cur for k in CURSOR_KEYS)):
                missing["state.cursor{block,entity}"] += 1
            for k in STATE_OPTIONAL_KEYS:
                if k in st:
                    optional[k] += 1
        pe = f.get("peer")
        if not isinstance(pe, dict):
            missing["peer"] += 1
        else:
            for k in PEER_REQUIRED_KEYS:
                if k not in pe:
                    missing["peer." + k] += 1
    fields = {"frames": len(frames), "missing": dict(missing),
              "optional_state_present": {k: optional.get(k, 0) for k in STATE_OPTIONAL_KEYS}}
    return {"yaw_pitch": yp, "fields": fields}


def check_camera_params(bot: str, aligned_meta: Optional[Dict[str, Any]], raw_meta: Optional[Dict[str, Any]],
                        expected_fov: float) -> Tuple[Dict[str, Any], List[str], List[str]]:
    """T2/T13: recorded camera settings. Returns (stats, errors, warnings)."""
    errors: List[str] = []
    warnings: List[str] = []
    params, source = None, None
    if aligned_meta and isinstance(aligned_meta.get("camera_params"), dict) \
            and aligned_meta["camera_params"].get("options_recorded", True) \
            and "fov_deg" in aligned_meta["camera_params"]:
        params, source = aligned_meta["camera_params"], "aligned_meta.camera_params"
    elif raw_meta and isinstance(raw_meta.get("options"), dict):
        params = dict(raw_meta["options"])
        for k in ("width", "height", "fps", "video_encoder", "capture_method", "platform", "mc_version"):
            if k in raw_meta:
                params[k] = raw_meta[k]
        source = "camera_meta.options"
    else:
        # Linux / Docker recordings: camera/launch_minecraft.py writes the options it set into
        # camera_options.json next to the camera meta (same key names as meta.options)
        dirs = []
        if raw_meta and raw_meta.get("_dir"):
            dirs.append(Path(raw_meta["_dir"]))
        if aligned_meta and aligned_meta.get("camera_meta_path"):
            dirs.append(Path(re.sub(r"[\\/]+", "/", str(aligned_meta["camera_meta_path"]))).parent)
        for d in dirs:
            opts_file = load_json(d / "camera_options.json") if (d / "camera_options.json").exists() else None
            if isinstance(opts_file, dict):
                params = {k: v for k, v in opts_file.items() if k != "options_txt"}
                for k in ("width", "height", "fps", "video_encoder", "capture_method", "platform", "mc_version"):
                    if raw_meta and k in raw_meta:
                        params[k] = raw_meta[k]
                source = "camera_options.json"
                break
    out: Dict[str, Any] = {"source": source}
    if params is None:
        warnings.append(f"{bot}: camera parameters not recorded (no camera_params / options in camera meta)")
        return out, errors, warnings
    for k in ("fov_deg", "fov_effect_scale", "render_distance", "simulation_distance", "gui_scale",
              "gui_scale_effective", "bob_view", "width", "height", "fps", "hud_visible", "name_tags_visible"):
        if k in params:
            out[k] = params[k]
    fov = params.get("fov_deg")
    fes = params.get("fov_effect_scale")
    if not is_num(fov):
        warnings.append(f"{bot}: camera fov_deg not recorded")
    elif abs(float(fov) - expected_fov) > 0.01:
        errors.append(f"{bot}: camera fov_deg={fov}, expected {expected_fov:g}")
    if not is_num(fes):
        warnings.append(f"{bot}: camera fov_effect_scale not recorded")
    elif float(fes) != 0.0:
        errors.append(f"{bot}: camera fov_effect_scale={fes}, must be 0 (flying/sprint change the FOV)")
    if not is_num(params.get("render_distance")):
        warnings.append(f"{bot}: camera render_distance not recorded")
    return out, errors, warnings


def check_episode(key, g, alignment_dir: Optional[Path], opts: Optional[argparse.Namespace] = None,
                  cams: Optional[CameraSources] = None) -> Dict[str, Any]:
    opts = opts or parse_args([".", ])
    cams = cams or CameraSources(None)
    episode, instance = key
    rep: Dict[str, Any] = {"episode": episode, "instance": instance, "errors": [], "warnings": []}
    checks: Dict[str, Any] = {}
    rep["checks"] = checks
    errors, warnings = rep["errors"], rep["warnings"]
    infos = {who: load_json(p) for who, p in g["info"].items()}
    infos = {who: v for who, v in infos.items() if isinstance(v, dict)}
    rep["encountered_error"] = {who: bool(v.get("encountered_error")) for who, v in infos.items()}
    if "events" not in g:
        errors.append("no world_events.jsonl")
        return finish(rep)
    sc = scan_events(g["events"])
    rep["events_file"] = g["events"].name
    rep["records"] = sc.records
    rep["bad_lines"] = sc.bad_lines
    if sc.bad_lines:
        errors.append(f"{sc.bad_lines} unparseable lines")
    if not sc.records:
        errors.append("empty events file")
        return finish(rep)
    if sc.first.get("type") != "episode_begin":
        errors.append("first record is not episode_begin")
    if sc.last_type != "episode_end":
        warnings.append("last record is not episode_end (run interrupted?)")
    if not sc.seq_ok:
        errors.append("seq not strictly increasing")
    rep["counts"] = dict(sc.counts)
    begin = sc.begin
    rep["seed"] = (begin.get("world") or {}).get("seed")
    leader_info = infos.get("Alpha") or next(iter(infos.values()), {})
    rep["episode_type"] = begin.get("episode_type") or leader_info.get("episode_type")
    rep["game_time_range"] = [sc.gt_min, sc.gt_max] if sc.gt_min is not None else None
    rep["epoch_ms_range"] = [sc.first.get("epoch_ms"), sc.last_epoch_ms]
    tracked = begin.get("tracked_players") or ["Alpha", "Bravo"]
    if isinstance(tracked, str):
        tracked = [p.strip() for p in tracked.split(",") if p.strip()]

    # ---- snapshots + replay ------------------------------------------------
    if "snap_start" in g:
        start = load_snapshot(g["snap_start"])
        rep["snapshot_box"] = {"origin": start["origin"], "size": start["size"],
                               "palette_size": len(start["palette"])}
        if "snap_end" in g:
            end = load_snapshot(g["snap_end"])
            overrides = replay_block_states(start, sc.block_states)
            mism, ex = compare_with_end(start, end, overrides)
            rep["replay_changed_voxels"] = len(overrides)
            rep["replay_mismatches"] = mism
            if mism:
                warnings.append(f"{mism} voxels differ between replay and end snapshot")
                rep["replay_mismatch_examples"] = ex
            del end
        else:
            warnings.append("no end snapshot")
        del start
    else:
        warnings.append("no start snapshot")

    # ---- tick / entities continuity (T4, T3) ----------------------------
    hb = begin.get("heartbeat_ticks") if isinstance(begin.get("heartbeat_ticks"), int) else 1
    ticks = continuity([p[1] for p in sc.tick_points], max(1, hb))
    checks["ticks"] = ticks
    if ticks["missing_ticks"]:
        warnings.append(f"tick records: {ticks['missing_ticks']} missing ticks in {ticks['gaps']} gap(s)")
    every = begin.get("entities_every")
    step = every if isinstance(every, int) and every > 0 else None
    if step is None:
        # Not announced (older plugin): assume the most common spacing
        gaps = Counter(b - a for a, b in zip(sorted(set(sc.entity_gts)), sorted(set(sc.entity_gts))[1:]))
        step = gaps.most_common(1)[0][0] if gaps else 1
    ent = continuity(sc.entity_gts, step)
    ent["entities_every"] = every
    ent["entities_radius"] = begin.get("entities_radius")
    ent["entities_per_record"] = percentiles([float(n) for n in sc.entity_sizes])
    ent["kinds"] = dict(sc.entity_kinds.most_common(30))
    if ticks.get("span") and ent.get("span"):
        ent["head_gap_ticks"] = ent["span"][0] - ticks["span"][0]
        ent["tail_gap_ticks"] = ticks["span"][1] - ent["span"][1]
    checks["entities"] = ent
    if not sc.entity_gts:
        if isinstance(every, int) and every > 0:
            errors.append(f"no entities records although episode_begin.entities_every={every}")
        else:
            warnings.append("no entities records (entity logging off or old plugin)")
    elif ent["missing_ticks"]:
        warnings.append(f"entities records: {ent['missing_ticks']} missing ticks in {ent['gaps']} gap(s)")

    # ---- episode time / weather / difficulty (T1, T14) ---------------------
    tw: Dict[str, Any] = {}
    for who, info in sorted(infos.items()):
        sm = info.get("state_metadata") or {}
        tw[who] = {k: sm.get(k) for k in ("time_of_day", "time_of_day_label", "weather", "daylight_cycle",
                                           "weather_cycle", "difficulty", "damage_enabled")}
        miss = [k for k in ("time_of_day", "weather") if sm.get(k) is None]
        if miss:
            warnings.append(f"{who}: episode_info.state_metadata lacks {', '.join(miss)}")
    if not infos:
        warnings.append("no episode_info found; time/weather not verifiable")
    checks["time_weather"] = tw

    # ---- damage (T14) and arm swings before damage (T15) -------------------
    sm_any = {k: v for info in infos.values() for k, v in (info.get("state_metadata") or {}).items()}
    damage_episode = bool(sm_any.get("damage_enabled")) or rep["episode_type"] in DAMAGE_EPISODE_TYPES
    player_hits = [d for d in sc.damage if d["victim"] in tracked]
    dmg = {"damage_episode": damage_episode, "entity_damage": len(sc.damage),
           "player_damage": len(player_hits), "entity_death": sc.deaths,
           "min_player_health_after": min((d["health_after"] for d in player_hits
                                           if is_num(d["health_after"])), default=None)}
    checks["damage"] = dmg
    if damage_episode and not sc.damage:
        warnings.append(f"damage episode ({rep['episode_type']}) without entity_damage records")
    pvp_hits = [d for d in player_hits if d["attacker"] in tracked and d["attacker"] != d["victim"]]
    melee = [d for d in pvp_hits if not d["projectile"]]
    swing_ok = 0
    for d in melee:
        ts = sc.swings.get(d["attacker"], [])
        t = float(d["epoch_ms"]) if is_num(d["epoch_ms"]) else None
        if t is None:
            continue
        j = bisect.bisect_left(ts, t - opts.swing_window_ms)
        if j < len(ts) and ts[j] <= t + opts.swing_after_ms:
            swing_ok += 1
    sw = {"pvp_hits": len(pvp_hits), "melee_hits": len(melee), "with_swing": swing_ok,
          "fraction": round(swing_ok / len(melee), 4) if melee else None,
          "window_ms": [-opts.swing_window_ms, opts.swing_after_ms],
          "arm_swing_records": sc.counts.get("arm_swing", 0)}
    checks["swing_before_damage"] = sw
    if melee and swing_ok / len(melee) < opts.min_swing_fraction:
        warnings.append(f"only {swing_ok}/{len(melee)} melee player hits have an attacker arm_swing "
                        f"within [-{opts.swing_window_ms:g}, +{opts.swing_after_ms:g}] ms of the hit")

    # ---- dig progress before block_break (T17) ---------------------------
    counted = [b for b in sc.breaks if b["excluded"] is None]
    with_dig = sum(1 for b in counted if b["has_dig"])
    digc = {"player_breaks": len(sc.breaks), "excluded": dict(Counter(b["excluded"] for b in sc.breaks
                                                                       if b["excluded"])),
            "checked": len(counted), "with_dig": with_dig,
            "with_increasing_progress": sum(1 for b in counted if b["increasing"]),
            "fraction": round(with_dig / len(counted), 4) if counted else None,
            "dig_records": sc.dig_records,
            "examples_without_dig": [{k: b[k] for k in ("seq", "pos", "player", "block")}
                                     for b in counted if not b["has_dig"]][:5]}
    checks["dig_before_break"] = digc
    if counted and with_dig / len(counted) < opts.min_dig_fraction:
        warnings.append(f"only {with_dig}/{len(counted)} player block_breaks are preceded by dig_start/"
                        f"dig_progress at that position")

    # ---- server-side camera vs bot (T12) ------------------------------------
    t12: Dict[str, Any] = {"server": {}, "rendered": {}}
    checks["t12"] = t12
    rep["_t12_server"], rep["_t12_server_input"] = [], []

    def angle_stats(d: Optional[Dict[str, List[float]]]) -> Tuple[Optional[Dict[str, Any]], List[float]]:
        if not d or not d.get("yaw"):
            return None, []
        comb = [max(a, b) for a, b in zip(d["yaw"], d["pitch"])]
        s = {"yaw": percentiles(d["yaw"]), "pitch": percentiles(d["pitch"]), "combined": percentiles(comb)}
        if d.get("pos"):
            s["pos"] = percentiles(d["pos"])
        return s, comb

    for target in sorted(sc.cam_ticks):
        # The follow error (dyaw/dpitch_since_sync) is the T12 measure. dyaw/dpitch_to_target
        # are sampled at the start of the tick, before the camera is synced, so they show
        # how far the bot turned in one tick (input lag), not a camera error. Older logs
        # without *_since_sync fall back to the lag-compensated comparison.
        follow, follow_c = angle_stats(sc.cam_follow.get(target))
        inp, inp_c = angle_stats(sc.cam_input.get(target))
        lagc, lagc_c = angle_stats(sc.cam_lagcomp.get(target))
        basis, comb = ("follow", follow_c) if follow else (("lag_compensated", lagc_c) if lagc else (None, []))
        t12["server"][target] = {"ticks": sc.cam_ticks[target], "basis": basis,
                                 "combined": percentiles(comb),
                                 "follow": follow, "lag_compensated": lagc, "one_tick_input": inp,
                                 "flying_fraction": round(sc.cam_flying[target] / sc.cam_ticks[target], 4),
                                 "gamemode": dict(sc.cam_gamemode.get(target, {}))}
        rep["_t12_server"].extend(comb)
        rep["_t12_server_input"].extend(inp_c)
        if comb and percentiles(comb)["p99"] >= opts.t12_max_p99:
            warnings.append(f"T12 server: camera of {target} p99 {basis.replace('_', '-')} angle error "
                            f"{percentiles(comb)['p99']:.3f} deg >= {opts.t12_max_p99:g}")

    # ---- per-bot frame checks ---------------------------------------------
    tmap = TickMap(sc.tick_points)
    if not tmap:
        warnings.append("fewer than 2 tick records; cannot map epoch time to game time")
    change_events = sc.change_events
    rep["bots"] = {}
    checks["yaw_pitch"] = {}
    checks["frame_fields"] = {}
    checks["camera_params"] = {}
    checks["visibility"] = {}
    rep["_t12_rendered"], rep["_t12_paired"] = [], []
    rep["_t12_pose_matched"], rep["_t12_pose_age"] = [], []
    alignment: Dict[str, Any] = {"episode": episode, "instance": instance, "bots": {}}
    all_frames: Dict[str, List[Dict[str, Any]]] = {}
    for bot, path in sorted(g["actions"].items()):
        with path.open("r", encoding="utf-8") as fh:
            frames = json.load(fh)
        if not frames:
            warnings.append(f"{bot}: empty action file")
            continue
        all_frames[bot] = frames
        times = frame_times(frames)
        b: Dict[str, Any] = {"action_file": path.name, "frames": len(frames),
                             "epoch_ms_range": [times[0], times[-1]]}
        ff = check_frame_fields(frames)
        checks["yaw_pitch"][bot] = ff["yaw_pitch"]
        checks["frame_fields"][bot] = ff["fields"]
        b["yaw_pitch_present"] = ff["yaw_pitch"]["missing"] == 0
        if not b["yaw_pitch_present"]:
            errors.append(f"{bot}: some frames lack yaw/pitch ({ff['yaw_pitch']['missing']}/{len(frames)} "
                          f"frames, first at {ff['yaw_pitch']['first_missing_indices']})")
        if ff["fields"]["missing"]:
            miss = ", ".join(f"{k}:{v}" for k, v in sorted(ff["fields"]["missing"].items()))
            warnings.append(f"{bot}: per-frame state/peer fields missing ({miss})")
        if tmap:
            diffs = [tmap.game_time_at(t) - float(f.get("worldAgeTicks", 0) or 0)
                     for t, f in zip(times, frames) if f.get("worldAgeTicks") is not None]
            if diffs:
                b["est_game_time_minus_worldAgeTicks"] = {
                    "median": statistics.median(diffs), "min": min(diffs), "max": max(diffs)}
                # worldAgeTicks only advances when the server sends update_time
                # (every 20 ticks), so 0..~25 ticks of lag is expected.
                if statistics.median(diffs) < -5 or statistics.median(diffs) > 60:
                    warnings.append(f"{bot}: worldAgeTicks disagrees with tick map "
                                    f"(median diff {statistics.median(diffs):.1f} ticks)")
        inside = [e for e in change_events if times[0] <= e[1] <= times[-1]]
        b["change_records_within_frames"] = len(inside)
        b["change_records_total"] = len(change_events)
        rep["bots"][bot] = b
        alignment["bots"][bot] = {
            "action_file": path.name,
            # seq -> index of the first frame whose action time is >= the record time
            "first_frame_at_or_after": {str(s): first_frame_at_or_after(times, t) for s, t in change_events},
            "nearest_frame": {str(s): nearest_frame(times, t) for s, t in change_events},
        }

        # camera parameters (T2/T13)
        aligned_meta = load_json(g["camera_meta"].get(bot))
        raw_meta = cams.meta(bot, instance, (times[0], times[-1]))
        cp, e2, w2 = check_camera_params(bot, aligned_meta, raw_meta, opts.expected_fov)
        errors.extend(e2)
        warnings.extend(w2)
        checks["camera_params"][bot] = cp
        if raw_meta is not None:
            cp["camera_meta"] = raw_meta.get("_file")
        if aligned_meta is not None:
            diag = aligned_meta.get("diagnostics") or {}
            cp["aligned_meta"] = g["camera_meta"][bot].name
            cp["sidecar_used"] = diag.get("sidecar_used")
            if diag.get("pts_minus_sidecar_ms"):
                cp["pts_minus_sidecar_ms"] = diag["pts_minus_sidecar_ms"]

        # rendered camera pose vs bot pose (T12)
        track = AngleTrack(frames)
        cam: List[Tuple[float, float, float]] = []
        cam_info: List[Optional[Dict[str, Any]]] = []
        fovs: List[float] = []
        pairs: List[Tuple[float, str, Optional[float], Optional[str]]] = []
        source = None
        mapping = (aligned_meta or {}).get("frame_mapping") or []
        if mapping and any(is_num(m.get("cam_yaw")) for m in mapping):
            source = "aligned_meta.frame_mapping"
            seen = set()
            side_info: Optional[Dict[float, Dict[str, Any]]] = None
            for m in mapping:
                if not (is_num(m.get("cam_yaw")) and is_num(m.get("cam_pitch"))):
                    continue
                info = pose_fields(m)
                if (info is None or "player_yaw" not in m) and is_num(m.get("sidecar_epoch_ms")):
                    # aligned by an older process_recordings.py: take the pose fields from the sidecar
                    if side_info is None:
                        side_info = cams.pose_info_by_time(bot, instance, (times[0], times[-1]))
                    info = side_info.get(round(float(m["sidecar_epoch_ms"]), 3)) or info
                ai = m.get("action_index")
                label = label_angles(frames[ai]) if isinstance(ai, int) and 0 <= ai < len(frames) else None
                if label is not None:
                    pairs.append(pair_entry(float(m["cam_yaw"]), float(m["cam_pitch"]), info, label,
                                            m.get("pose_pair")))
                fi = m.get("frame_index")
                if fi in seen:
                    continue
                seen.add(fi)
                t = m.get("sidecar_epoch_ms")
                if not is_num(t):
                    t = float(m.get("frame_time_sec", 0.0)) * 1000.0
                cam.append((float(t), float(m["cam_yaw"]), float(m["cam_pitch"])))
                cam_info.append(info)
                if is_num(m.get("fov")):
                    fovs.append(float(m["fov"]))
        else:
            side = cams.sidecar(bot, instance, (times[0], times[-1]))
            if side:
                source = "camera_frames.jsonl"
                lo = bisect.bisect_left(side, (times[0],))
                hi = bisect.bisect_right(side, (times[-1], float("inf")))
                for t, cy, cpi, fov, info in side[lo:hi]:
                    cam.append((t, cy, cpi))
                    cam_info.append(info)
                    if is_num(fov):
                        fovs.append(float(fov))
                # no aligned meta: pair the action samples with frames like process_recordings.py
                pairs = simulate_pairs(frames, side)
        order = sorted(range(len(cam)), key=lambda k: cam[k][0])
        cam = [cam[k] for k in order]
        cam_info = [cam_info[k] for k in order]
        if cam and len(track) >= 2:
            rv = rendered_vs_bot(cam, track)
            rv["source"] = source
            paired_c = [p[0] for p in pairs]
            rv["paired"] = percentiles(paired_c)
            rv["paired_detail"] = paired_report(pairs, opts.t12_max_p99)
            if source == "camera_frames.jsonl":
                rv["paired_detail"]["simulated"] = True
            comb_best = rv.pop("_combined_best")
            rep["_t12_rendered"].extend(comb_best)
            rep["_t12_paired"].extend(paired_c)
            rep.setdefault("_t12_paired_injected", []).extend(p[0] for p in pairs if p[1] == "injected")
            rep.setdefault("_t12_paired_server_frames", []).extend(p[0] for p in pairs if p[1] == "server")
            rep.setdefault("_t12_paired_same_injected", []).extend(
                p[0] for p in pairs if p[1] == "injected" and p[2] is not None)
            rep.setdefault("_t12_paired_same_server_followed", []).extend(
                p[2] for p in pairs if p[1] == "injected" and p[2] is not None)
            rep.setdefault("_t12_pose_pair", []).extend(p[3] for p in pairs if p[3])
            # T12 threshold basis: p99 of `gate_stats` (default: all frames, best lag)
            gate_stats = (rv["best_lag"] or {}).get("combined") or {}
            gate_name = f"camera vs bot (best lag {rv['best_lag_ms']} ms)"
            if any(i is not None for i in cam_info):
                ps = rendered_by_pose_source(cam, cam_info, track, frames)
                rep["_t12_pose_matched"].extend(ps.pop("_pose_matched"))
                rep["_t12_pose_age"].extend(ps.pop("_ages"))
                for src, sv in ps["by_source"].items():
                    rep.setdefault(f"_t12_rendered_{src}", []).extend(sv.pop("_combined_best", []))
                    rep.setdefault(f"_t12_rendered_{src}_lag0", []).extend(sv.pop("_lag0_combined", []))
                rv["pose_link"] = ps
                inj = ps["by_source"].get("injected")
                paired_inj = [p[0] for p in pairs if p[1] == "injected"]
                if paired_inj:
                    # what the training data contains: injected frames vs their action labels
                    gate_stats = percentiles(paired_inj)
                    gate_name = "injected frames vs their action labels (paired)"
                elif ps["pose_matched"]["samples"]:
                    gate_stats = ps["pose_matched"]["combined"]
                    gate_name = "injected-pose frames (pose-matched)"
                elif inj and "best_lag" in inj:
                    gate_stats = (inj["best_lag"] or {}).get("combined") or {}
                    gate_name = f"injected-pose frames (best lag {inj['best_lag_ms']} ms)"
                else:
                    gate_stats = {}  # no (usable) injected frames: the pose link did not work
                    warnings.append(f"T12 rendered: {bot} camera rendered no pose-link (injected) frames "
                                    f"(frames by pose_source: {ps['frames']})")
                # camera fidelity: injected frames vs the packet they rendered (exact)
                pm99 = (ps["pose_matched"]["combined"] or {}).get("p99")
                if paired_inj and pm99 is not None and pm99 >= opts.t12_max_p99:
                    warnings.append(f"T12 rendered: {bot} camera fidelity (pose-matched) p99 angle error "
                                    f"{pm99:.3f} deg >= {opts.t12_max_p99:g}")
                # the pose link must actually drive the camera, not only some frames
                link_on = bool(((raw_meta or {}).get("pose_link") or {}).get("enabled")) \
                    or ps["frames"].get("injected", 0) > 0
                frac = ps["injected_fraction"]
                if link_on and frac is not None and frac < opts.t12_min_injected:
                    warnings.append(f"T12 rendered: {bot} only {frac:.1%} of the frames in the episode span "
                                    f"used the pose link (< {opts.t12_min_injected:.0%}); server-frame "
                                    f"reasons: {ps['server_frame_reasons']}")
            if fovs:
                off = [f for f in fovs if abs(f - opts.expected_fov) > 0.05]
                rv["fov_used"] = {"min": min(fovs), "max": max(fovs), "frames_off_expected": len(off)}
                if off:
                    warnings.append(f"{bot}: {len(off)}/{len(fovs)} rendered frames used fov != "
                                    f"{opts.expected_fov:g} (min {min(fovs):.2f}, max {max(fovs):.2f})")
            t12["rendered"][bot] = rv
            rv["threshold_basis"] = gate_name
            p99 = gate_stats.get("p99")
            if p99 is not None and p99 >= opts.t12_max_p99:
                warnings.append(f"T12 rendered: {bot} {gate_name} p99 angle error {p99:.3f} deg "
                                f">= {opts.t12_max_p99:g}")
        else:
            t12["rendered"][bot] = {"frames": 0, "source": None}
            meta_has_sidecar = bool((raw_meta or {}).get("frames_file")) or bool(
                ((aligned_meta or {}).get("diagnostics") or {}).get("sidecar_path"))
            if meta_has_sidecar:
                warnings.append(f"{bot}: camera has a frames sidecar but no rendered pose matched the episode")

    # ---- mutual visibility (T8) -------------------------------------------
    bots = sorted(all_frames)
    if len(bots) == 2:
        for bot, other in ((bots[0], bots[1]), (bots[1], bots[0])):
            v = visibility(all_frames[bot], all_frames[other], opts.vfov, opts.aspect, opts.view_distance)
            checks["visibility"][bot] = v
            if v["fraction"] is not None and v["fraction"] < opts.min_visibility:
                warnings.append(f"{bot}: {other} inside the view frustum in {v['fraction']:.0%} of frames "
                                f"(< {opts.min_visibility:.0%})")
    if not g["actions"]:
        warnings.append("no per-frame action JSON found for this episode")
    if alignment_dir is not None:
        alignment_dir.mkdir(parents=True, exist_ok=True)
        out = alignment_dir / (g["events"].name.replace("_world_events.jsonl", "_event_frame_alignment.json"))
        with out.open("w", encoding="utf-8") as fh:
            json.dump(alignment, fh)
        rep["alignment_file"] = str(out)
    return finish(rep)


def finish(rep: Dict[str, Any]) -> Dict[str, Any]:
    rep["status"] = "FAIL" if rep["errors"] else ("WARN" if rep["warnings"] else "OK")
    return rep


# ---------------------------------------------------------------------------
# Batch summary
# ---------------------------------------------------------------------------

def summarize(reports: List[Dict[str, Any]], opts: argparse.Namespace) -> Dict[str, Any]:
    status = Counter(r["status"] for r in reports)
    types = Counter(r.get("episode_type") or "unknown" for r in reports)
    enc_total = enc_true = 0
    per_bot: Dict[str, List[int]] = defaultdict(lambda: [0, 0])
    for r in reports:
        for who, v in (r.get("encountered_error") or {}).items():
            enc_total += 1
            enc_true += bool(v)
            per_bot[who][0] += 1
            per_bot[who][1] += bool(v)
    vis: Dict[str, Dict[str, Any]] = {}
    for r in reports:
        for bot, v in ((r.get("checks") or {}).get("visibility") or {}).items():
            a = vis.setdefault(bot, {"frames": 0, "visible": 0})
            a["frames"] += v["frames"]
            a["visible"] += v["visible"]
    all_f = sum(v["frames"] for v in vis.values())
    all_v = sum(v["visible"] for v in vis.values())
    for v in vis.values():
        v["fraction"] = round(v["visible"] / v["frames"], 4) if v["frames"] else None
    vis["all"] = {"frames": all_f, "visible": all_v, "fraction": round(all_v / all_f, 4) if all_f else None}
    lags = [rv["best_lag_ms"] for r in reports for rv in ((r.get("checks") or {}).get("t12") or {})
            .get("rendered", {}).values() if "best_lag_ms" in rv]
    pool = lambda k: [x for r in reports for x in r.get(k, [])]  # noqa: E731
    pose_counter: Counter = Counter()
    for r in reports:
        for rv in ((r.get("checks") or {}).get("t12") or {}).get("rendered", {}).values():
            pose_counter.update((rv.get("pose_link") or {}).get("frames") or {})
    pose_frames = dict(sorted(pose_counter.items()))
    mism = [r.get("replay_mismatches") for r in reports if r.get("replay_mismatches") is not None]
    return {
        "episodes": len(reports),
        "status_counts": {s: status.get(s, 0) for s in ("OK", "WARN", "FAIL")},
        "episode_types": dict(sorted(types.items())),
        "encountered_error": {"episode_info_files": enc_total, "true": enc_true,
                              "ratio": round(enc_true / enc_total, 4) if enc_total else None,
                              "per_bot": {b: {"files": n, "true": k, "ratio": round(k / n, 4) if n else None}
                                          for b, (n, k) in sorted(per_bot.items())}},
        "t12_deg": {"server_combined": percentiles(pool("_t12_server")),
                    "server_one_tick_input_combined": percentiles(pool("_t12_server_input")),
                    "rendered_best_lag_combined": percentiles(pool("_t12_rendered")),
                    "rendered_paired_combined": percentiles(pool("_t12_paired")),
                    # frame vs its action label (training pairs), split by pose_source; and on the
                    # same injected frames the server-followed player look vs the same labels
                    "rendered_paired_injected_combined": percentiles(pool("_t12_paired_injected")),
                    "rendered_paired_server_frames_combined": percentiles(pool("_t12_paired_server_frames")),
                    "paired_same_frames_injected_combined": percentiles(pool("_t12_paired_same_injected")),
                    "paired_same_frames_server_followed_combined":
                        percentiles(pool("_t12_paired_same_server_followed")),
                    "paired_injected_frac_over_threshold": (
                        round(sum(1 for x in pool("_t12_paired_injected") if x >= opts.t12_max_p99)
                              / len(pool("_t12_paired_injected")), 4) if pool("_t12_paired_injected") else None),
                    "pose_pair": dict(sorted(Counter(pool("_t12_pose_pair")).items())),
                    "rendered_best_lag_ms_median": statistics.median(lags) if lags else None,
                    # camera pose link (Windows mod): frames by pose_source, injected-only errors
                    "rendered_injected_best_lag_combined": percentiles(pool("_t12_rendered_injected")),
                    "rendered_injected_lag0_combined": percentiles(pool("_t12_rendered_injected_lag0")),
                    "rendered_server_best_lag_combined": percentiles(pool("_t12_rendered_server")),
                    "rendered_pose_matched_combined": percentiles(pool("_t12_pose_matched")),
                    "pose_age_ms": percentiles(pool("_t12_pose_age")),
                    "pose_source_frames": pose_frames,
                    "injected_fraction": (round(pose_frames.get("injected", 0) / sum(pose_frames.values()), 4)
                                          if pose_frames else None)},
        "visibility": vis,
        "replay_mismatch_total": sum(m for m in mism if m > 0),
        "episodes_with_replay_mismatch": sum(1 for m in mism if m != 0),
        "thresholds": {"expected_fov": opts.expected_fov, "min_visibility": opts.min_visibility,
                       "t12_max_p99_deg": opts.t12_max_p99, "t12_min_injected": opts.t12_min_injected,
                       "swing_window_ms": opts.swing_window_ms,
                       "min_swing_fraction": opts.min_swing_fraction,
                       "min_dig_fraction": opts.min_dig_fraction},
    }


def fmt_pct(v: Optional[float]) -> str:
    return "-" if v is None else f"{v:.2f}"


def print_episode(r: Dict[str, Any]) -> None:
    counts = r.get("counts", {})
    ck = r.get("checks", {})
    parts = []
    for b, v in r.get("bots", {}).items():
        vis = (ck.get("visibility") or {}).get(b, {}).get("fraction")
        rv = (ck.get("t12") or {}).get("rendered", {}).get(b, {})
        p99 = ((rv.get("best_lag") or {}).get("combined") or {}).get("p99")
        pl = rv.get("pose_link") or {}
        inj = ((((pl.get("by_source") or {}).get("injected") or {}).get("best_lag") or {}).get("combined")
               or {}).get("p99")
        matched = (((pl.get("pose_matched") or {}).get("combined")) or {}).get("p99")
        pinj = (((((rv.get("paired_detail") or {}).get("by_source") or {}).get("injected") or {})
                 .get("combined")) or {}).get("p99")
        link = (f", injected={fmt_pct(pl.get('injected_fraction'))} inj_p99={fmt_pct(inj)} "
                f"paired_inj_p99={fmt_pct(pinj)} pose_matched_p99={fmt_pct(matched)} "
                f"pose_age_p50={fmt_pct((pl.get('pose_age_ms') or {}).get('p50'))}ms") if pl else ""
        parts.append(f"{b}: {v['frames']} frames, {v['change_records_within_frames']}/"
                     f"{v['change_records_total']} changes inside frame span, vis={fmt_pct(vis)}, "
                     f"cam_p99={fmt_pct(p99)}{link}")
    srv = [((s.get("combined") or {}).get("p99")) for s in (ck.get("t12") or {}).get("server", {}).values()]
    srv = [x for x in srv if x is not None]
    ent = ck.get("entities", {})
    print(f"[{r['status']}] episode {r['episode']} instance {r['instance']} "
          f"type={r.get('episode_type')} records={r.get('records')} "
          f"changes={sum(counts.get(t, 0) for t in BLOCK_RECORD_TYPES)} "
          f"ticks={counts.get('tick', 0)} entities={counts.get('entities', 0)} "
          f"(missing {ent.get('missing_ticks', '-')}) replay_mismatch={r.get('replay_mismatches')} "
          f"damage={counts.get('entity_damage', 0)} srv_cam_p99={fmt_pct(max(srv) if srv else None)} "
          f"| {' | '.join(parts)}")
    for e in r["errors"]:
        print(f"    error: {e}")
    for w in r["warnings"]:
        print(f"    warning: {w}")


def parse_args(argv=None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("dir", type=Path)
    ap.add_argument("--episode", type=int, default=None, help="only this episode number")
    ap.add_argument("--instance", type=int, default=None, help="only this instance id")
    ap.add_argument("--report", type=Path, default=None, help="write the full JSON report here")
    ap.add_argument("--alignment-dir", type=Path, default=None,
                    help="write <stem>_event_frame_alignment.json files here (keep it outside the output dir)")
    ap.add_argument("--aligned-dir", type=Path, default=None,
                    help="aligned videos + *_camera_meta.json (default: <batch>/aligned, or DIR for a dataset)")
    ap.add_argument("--camera-dir", type=Path, default=None,
                    help="raw camera outputs with output_<bot>/<instance>/ (default: <batch>/camera)")
    ap.add_argument("--expected-fov", type=float, default=70.0, help="required camera fov_deg (default 70)")
    ap.add_argument("--min-visibility", type=float, default=0.40,
                    help="WARN if the other bot is in view in fewer frames (default 0.40, T8)")
    ap.add_argument("--t12-max-p99", type=float, default=0.2,
                    help="WARN if the camera-vs-bot angle error p99 reaches this many degrees (default 0.2)")
    ap.add_argument("--t12-min-injected", type=float, default=0.95,
                    help="WARN if the camera has a pose link but fewer of the frames in the episode span "
                         "rendered the injected pose (default 0.95)")
    ap.add_argument("--swing-window-ms", type=float, default=500.0,
                    help="arm_swing must precede a melee hit by at most this (default 500, T15)")
    ap.add_argument("--swing-after-ms", type=float, default=100.0,
                    help="also accept a swing logged this long after the hit (1.21 clients send the "
                         "swing right after the attack packet; default 100)")
    ap.add_argument("--min-swing-fraction", type=float, default=0.95)
    ap.add_argument("--min-dig-fraction", type=float, default=0.90,
                    help="WARN if fewer non-instant block_breaks have dig records before (T17)")
    ap.add_argument("--vfov", type=float, default=70.0, help="vertical FOV for the visibility check")
    ap.add_argument("--aspect", type=float, default=16.0 / 9.0, help="aspect ratio for the visibility check")
    ap.add_argument("--view-distance", type=float, default=64.0, help="max distance for the visibility check")
    return ap.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)

    d = resolve_dir(args.dir)
    batch = batch_dir_of(d)
    aligned_dir = args.aligned_dir or (batch / "aligned" if batch is not None else None)
    camera_dir = args.camera_dir or (batch / "camera" if batch is not None else None)
    groups = discover(d, aligned_dir)
    keys = sorted(k for k, g in groups.items() if "events" in g)
    if args.episode is not None:
        keys = [k for k in keys if int(k[0]) == args.episode]
    if args.instance is not None:
        keys = [k for k in keys if int(k[1]) == args.instance]
    if not keys:
        print(f"No *_world_events.jsonl found in {d}", file=sys.stderr)
        return 2

    cams = CameraSources(camera_dir if camera_dir is not None and camera_dir.is_dir() else None)
    reports = []
    for k in keys:
        try:
            reports.append(check_episode(k, groups[k], args.alignment_dir, args, cams))
        except Exception as exc:  # noqa: BLE001 - one broken episode must not hide the others
            reports.append(finish({"episode": k[0], "instance": k[1], "errors": [f"check crashed: {exc!r}"],
                                   "warnings": [], "checks": {}}))
    for r in reports:
        print_episode(r)
    summary = summarize(reports, args)
    for r in reports:
        for k in [k for k in r if k.startswith("_")]:
            del r[k]
    sc = summary["status_counts"]
    t12 = summary["t12_deg"]
    print(f"{len(reports)} episode(s) checked: {sc['OK']} OK, {sc['WARN']} WARN, {sc['FAIL']} FAIL | "
          f"T12 p99 server={fmt_pct((t12['server_combined'] or {}).get('p99'))} "
          f"rendered={fmt_pct((t12['rendered_best_lag_combined'] or {}).get('p99'))} "
          f"injected={fmt_pct((t12['rendered_injected_best_lag_combined'] or {}).get('p99'))} "
          f"paired-injected={fmt_pct((t12['rendered_paired_injected_combined'] or {}).get('p99'))} "
          f"pose-matched={fmt_pct((t12['rendered_pose_matched_combined'] or {}).get('p99'))} "
          f"({fmt_pct(t12['injected_fraction'])} of frames) deg | "
          f"visibility={fmt_pct(summary['visibility']['all']['fraction'])} | "
          f"encountered_error={summary['encountered_error']['true']}/"
          f"{summary['encountered_error']['episode_info_files']}")
    if args.report:
        doc = {"format": REPORT_FORMAT, "generated_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
               "dir": str(args.dir), "summary": summary, "episodes": reports}
        args.report.parent.mkdir(parents=True, exist_ok=True)
        with args.report.open("w", encoding="utf-8") as fh:
            json.dump(doc, fh, indent=2)
        print(f"report written to {args.report}")
    return 1 if sc["FAIL"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
