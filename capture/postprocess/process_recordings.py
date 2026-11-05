#!/usr/bin/env python3
# Modified by Ziyang Ye for MineEngine, 2026 (from SolarisEngine 78f23e7); see capture/MODIFICATIONS.md
"""Run camera alignment."""
from __future__ import annotations

import argparse
import bisect
import json
import os
import shutil
import statistics
import subprocess
import sys
import time
from collections import Counter
from dataclasses import dataclass
from pathlib import Path, PureWindowsPath
from typing import Any, Dict, Iterable, List, Optional, Tuple

import cv2


# Unconsumed frames within this many seconds of the recording start/end are
# considered normal (the camera typically starts before and ends after the
# episode actions).
_BOUNDARY_GRACE_SEC = 10.0


@dataclass
class AlignmentInput:
    actions_path: Path
    camera_meta_path: Path
    output_video_path: Path
    output_metadata_path: Path
    ffmpeg_path: str  # retained for CLI compatibility, unused internally
    margin_start: float  # unused but kept for backward compatibility
    margin_end: float    # unused but kept for backward compatibility
    # "auto": match on the sidecar capture times when the sidecar is usable,
    # else on the MKV PTS; "pts": always match on the MKV PTS.
    match_clock: str = "auto"
    # "seq": with the camera pose link (action samples and sidecar lines both have
    # pose_seq) re-pair a sample whose time-matched frame rendered another packet with
    # the neighbouring frame that rendered the sample's packet; "time": time only.
    pose_pairing: str = "seq"


def _load_actions(path: Path) -> List[Dict[str, Any]]:
    with path.open("r", encoding="utf-8") as fh:
        data = json.load(fh)
    if not isinstance(data, list) or not data:
        raise ValueError(f"Action file {path} is empty or invalid")
    return data


def _ensure_camera_meta(path: Path) -> Dict[str, Any]:
    with path.open("r", encoding="utf-8") as fh:
        meta = json.load(fh)
    for key in ("start_epoch_seconds", "fps", "recording_path"):
        if key not in meta:
            raise ValueError(f"Camera metadata {path} missing '{key}'")
    return meta


def _resolve_recorded_path(raw: str, meta_dir: Path) -> Path:
    """Resolve a path written into the camera meta by the recorder.

    The recorder may have run on another OS (``/output/camera_alpha.mkv`` from
    the Linux container, ``C:\\...\\camera_alpha.mkv`` from the Windows client),
    so fall back to the file of the same name next to the meta file.
    """
    p = Path(raw)
    if not p.is_absolute():
        p = meta_dir / p
    if p.exists():
        return p
    # PureWindowsPath splits on both '\\' and '/', so .name works for either origin
    alt = meta_dir / PureWindowsPath(raw).name
    return alt if alt.exists() else p


def _ffprobe_executable() -> str:
    """ffprobe to use: $FFPROBE, PATH, or next to $FFMPEG / ffmpeg on PATH."""
    env = os.environ.get("FFPROBE")
    if env:
        return env
    found = shutil.which("ffprobe")
    if found:
        return found
    ffmpeg = os.environ.get("FFMPEG") or shutil.which("ffmpeg")
    if ffmpeg:
        ffmpeg_path = Path(ffmpeg)
        for name in ("ffprobe.exe", "ffprobe"):
            cand = ffmpeg_path.with_name(name)
            if cand.exists():
                return str(cand)
    return "ffprobe"


# ---------------------------------------------------------------------------
# Rendered-camera sidecar (Windows in-client recorder, CONTRACT.md 5.4)
# ---------------------------------------------------------------------------

# Camera-meta keys copied into the aligned meta's camera_params (besides options)
_CAMERA_PARAM_KEYS = (
    "width", "height", "fps", "capture_method", "platform", "mc_version",
    "fabric_loader_version", "mod_version", "framebuffer", "window_hidden",
)


def _build_camera_params(camera_meta: Dict[str, Any], meta_dir: Optional[Path] = None) -> Dict[str, Any]:
    """Actual camera settings of this recording (T2/T13).

    Flat copy of ``meta.options`` (fov_deg, fov_effect_scale, render_distance,
    gui_scale, ...) plus resolution / fps / encoder / capture method. Linux /
    Docker metas have no ``options``; then ``<meta_dir>/camera_options.json``
    (written by camera/launch_minecraft.py, same key names) is used
    (``options_source``). Without either only the generic keys are present and
    ``options_recorded`` is false.
    """
    options = camera_meta.get("options")
    source = "camera_meta.options" if isinstance(options, dict) else None
    if not isinstance(options, dict) and meta_dir is not None:
        try:
            loaded = json.loads((Path(meta_dir) / "camera_options.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            loaded = None
        if isinstance(loaded, dict):
            options = {k: v for k, v in loaded.items() if k not in ("options_txt", "width", "height")}
            source = "camera_options.json"
    params: Dict[str, Any] = dict(options) if isinstance(options, dict) else {}
    for key in _CAMERA_PARAM_KEYS:
        if key in camera_meta:
            params[key] = camera_meta[key]
    params["encoder"] = camera_meta.get("video_encoder")
    params["options_recorded"] = isinstance(options, dict)
    if source == "camera_options.json":
        params["options_source"] = source
    return params


def _load_frames_sidecar(path: Path) -> Tuple[List[Dict[str, Any]], int]:
    """Read camera_<bot>_frames.jsonl; returns (records in file order, bad lines)."""
    records: List[Dict[str, Any]] = []
    bad = 0
    with path.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                bad += 1
    return records, bad


def _percentiles(values: List[float]) -> Optional[Dict[str, float]]:
    if not values:
        return None
    v = sorted(values)

    def q(p: float) -> float:
        return v[min(len(v) - 1, int(round(p * (len(v) - 1))))]

    return {"n": len(v), "mean": sum(v) / len(v), "min": v[0], "p50": q(0.5), "p90": q(0.9),
            "p99": q(0.99), "max": v[-1]}


def _load_sidecar(
    camera_meta: Dict[str, Any],
    meta_dir: Path,
    n_frames: int,
) -> Tuple[Optional[List[Dict[str, Any]]], Dict[str, Any]]:
    """Load the per-frame sidecar if it is usable for this recording.

    The sidecar has one line per encoded frame in encode order, so line k is
    video frame k exactly when the counts agree (no B-frames, ``-bf 0``).
    Returns (sidecar records or None, diagnostics).
    """
    diag: Dict[str, Any] = {"sidecar_used": False}
    frames_file = camera_meta.get("frames_file")
    if not frames_file:
        diag["sidecar_reason"] = "camera meta has no frames_file (older recorder)"
        return None, diag
    path = _resolve_recorded_path(str(frames_file), meta_dir)
    diag["sidecar_path"] = str(path)
    if not path.exists():
        diag["sidecar_reason"] = "frames file not found"
        return None, diag
    try:
        records, bad = _load_frames_sidecar(path)
    except OSError as exc:
        diag["sidecar_reason"] = f"cannot read frames file: {exc}"
        return None, diag
    diag["sidecar_lines"] = len(records)
    diag["sidecar_bad_lines"] = bad
    if bad or len(records) != n_frames:
        diag["sidecar_reason"] = (f"sidecar has {len(records)} valid lines ({bad} bad), "
                                  f"video has {n_frames} frames")
        return None, diag
    diag["sidecar_used"] = True
    return records, diag


def _sidecar_capture_times(sidecar: List[Dict[str, Any]]) -> Tuple[Optional[List[float]], str]:
    """Capture times (epoch seconds) of all frames, or (None, reason).

    Usable as the matching clock only if every line has a numeric epoch_ms and
    the times never decrease (encode order == capture order).
    """
    times: List[float] = []
    for k, rec in enumerate(sidecar):
        ems = rec.get("epoch_ms")
        if not isinstance(ems, (int, float)) or isinstance(ems, bool):
            return None, f"sidecar line {k} has no numeric epoch_ms"
        t = float(ems) / 1000.0
        if times and t < times[-1]:
            return None, f"sidecar epoch_ms decreases at line {k}"
        times.append(t)
    return times, ""


def _pts_vs_sidecar_stats(
    sidecar: List[Dict[str, Any]],
    frame_timestamps: List[float],
    frame_indices: List[int],
) -> Optional[Dict[str, float]]:
    """``PTS*1000 - capture epoch_ms`` over the frames used by this episode.

    With the contract's rawvideo + ``-use_wallclock_as_timestamps`` command the
    PTS is ffmpeg's arrival time of the frame rounded to the input time base
    (1/fps), so this mixes the readback/pipe latency with up to half a frame
    period of rounding either way (it can be negative). With the mod's
    ``timestamps: capture`` mode the PTS is the capture time and this is ~0.
    """
    deltas = []
    for idx in sorted(set(frame_indices)):
        ems = sidecar[idx].get("epoch_ms")
        if isinstance(ems, (int, float)):
            deltas.append(frame_timestamps[idx] * 1000.0 - float(ems))
    stats = _percentiles(deltas)
    if stats is not None:
        stats["median"] = statistics.median(deltas)
    return stats


def _pts_duplicates(frame_timestamps: List[float]) -> int:
    """Frames whose PTS is not above the previous frame's PTS (quantised stamps)."""
    return sum(1 for a, b in zip(frame_timestamps, frame_timestamps[1:]) if b <= a)


# ---------------------------------------------------------------------------
# Per-frame timestamp extraction (wallclock mode)
# ---------------------------------------------------------------------------

def _extract_frame_timestamps(recording_path: Path) -> Optional[List[float]]:
    """Extract per-frame PTS (in seconds) from an MKV using ffprobe.

    Returns a **sorted** list of floats (one per video frame) or *None* if
    extraction fails.  When the MKV was recorded with
    ``-use_wallclock_as_timestamps 1 -copyts``, these values will be absolute
    Unix-epoch seconds (e.g. 1738540000.123).

    Sorting is necessary because ``ffprobe -show_entries packet=pts_time``
    returns timestamps in *decode* order, which differs from presentation
    order when B-frames are used.  Sorting restores presentation order so
    that index *i* in the returned list corresponds to frame *i* as decoded
    by ``cv2.VideoCapture``.
    """
    cmd = [
        _ffprobe_executable(),
        "-v", "quiet",
        "-select_streams", "v:0",
        "-show_entries", "packet=pts_time",
        "-of", "csv=p=0",
        str(recording_path),
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                                errors="replace", timeout=300)
    except FileNotFoundError:
        print("[align] ffprobe not found; falling back to legacy alignment", file=sys.stderr)
        return None
    except subprocess.TimeoutExpired:
        print("[align] ffprobe timed out; falling back to legacy alignment", file=sys.stderr)
        return None

    if result.returncode != 0:
        print(f"[align] ffprobe failed (rc={result.returncode}); falling back to legacy alignment",
              file=sys.stderr)
        return None

    timestamps: List[float] = []
    for line in result.stdout.strip().splitlines():
        line = line.strip()
        if not line or line.lower() == "n/a":
            continue
        try:
            timestamps.append(float(line))
        except ValueError:
            continue

    if not timestamps:
        return None

    # Sort to convert from decode order to presentation order (handles
    # B-frame reordering).  New recordings use -bf 0 which makes this a
    # no-op, but older recordings may still have B-frames.
    timestamps.sort()

    return timestamps


def _has_wallclock_timestamps(
    frame_timestamps: List[float],
    camera_meta: Dict[str, Any],
) -> bool:
    """Heuristic: wallclock PTS values are large Unix-epoch numbers (> 1e9).

    Legacy recordings have PTS starting near zero.  We also cross-check against
    the ``wallclock_timestamps`` flag in the metadata if available.
    """
    if camera_meta.get("wallclock_timestamps"):
        return True
    # Heuristic: first PTS > 1 billion ≈ 2001-09-09 → definitely epoch time
    if frame_timestamps and frame_timestamps[0] > 1e9:
        return True
    return False


# ---------------------------------------------------------------------------
# Two-pointer action ↔ frame matching (wallclock mode)
# ---------------------------------------------------------------------------

def _match_actions_to_frames(
    actions: List[Dict[str, Any]],
    frame_timestamps: List[float],
    fps: float,
) -> Tuple[List[int], Dict[str, Any]]:
    """Match each action to the first video frame at or after the action time.

    ``frame_timestamps`` are the per-frame times on the matching clock (MKV
    PTS, or the sidecar capture times); both they and ``action_times`` (from
    ``epochTime``) are non-decreasing sequences.  For each action we find the first frame whose
    timestamp is >= the action's effective time.

    Frames are **not** consumed: if a frame was dropped and two consecutive
    actions both land on the same frame, that frame is reused (duplicate).
    This avoids additive drift -- after the duplicate the sequences resync
    immediately.

    Returns ``(frame_indices, diagnostics)`` where *frame_indices* has one
    entry per matched action (the index into the recording to extract) and
    *diagnostics* is a dict with alignment quality stats.
    """
    n_actions = len(actions)
    n_frames = len(frame_timestamps)
    action_times = [float(a["epochTime"]) for a in actions]

    frame_indices: List[int] = []
    frame_ptr = 0

    # Track diagnostics
    time_deltas: List[float] = []      # frame_time - effective_action_time
    unmatched_actions_end = 0          # actions after last available frame

    for action_idx, action_time in enumerate(action_times):
        # Advance frame_ptr to the first frame at or after action_time.
        # frame_ptr never goes backwards, so this is O(n+m) overall.
        while frame_ptr < n_frames and frame_timestamps[frame_ptr] < action_time:
            frame_ptr += 1

        if frame_ptr >= n_frames:
            # No more frames -- remaining actions are unmatched at the end
            unmatched_actions_end = n_actions - action_idx
            break

        # Record the match.  Do NOT advance frame_ptr: the same frame may
        # be the correct match for the next action too (dropped-frame case).
        matched_frame = frame_ptr
        delta = frame_timestamps[matched_frame] - action_time
        time_deltas.append(delta)
        frame_indices.append(matched_frame)

    # --- Compute boundary statistics ---
    skipped_frames_start = 0
    skipped_frames_end = 0
    if frame_indices:
        skipped_frames_start = frame_indices[0]
        skipped_frames_end = max(0, n_frames - 1 - frame_indices[-1])

    # Count actions whose time falls before the first frame
    unmatched_actions_start = 0
    if frame_timestamps and action_times:
        for t in action_times:
            if t < frame_timestamps[0]:
                unmatched_actions_start += 1
            else:
                break

    # --- Check for duplicate frame usage (indicates dropped frames) ---
    frame_usage = Counter(frame_indices)
    duplicate_frames = {idx: cnt for idx, cnt in frame_usage.items() if cnt > 1}

    # --- Check for interior unconsumed frames ---
    # Frames within _BOUNDARY_GRACE_SEC of recording start/end are OK.
    # Any other unconsumed frame is flagged.
    consumed_set = set(frame_indices)
    rec_start = frame_timestamps[0] if frame_timestamps else 0.0
    rec_end = frame_timestamps[-1] if frame_timestamps else 0.0
    interior_unconsumed: List[int] = []
    for i in range(n_frames):
        if i in consumed_set:
            continue
        t = frame_timestamps[i]
        if (t - rec_start) <= _BOUNDARY_GRACE_SEC:
            continue  # within start grace period
        if (rec_end - t) <= _BOUNDARY_GRACE_SEC:
            continue  # within end grace period
        interior_unconsumed.append(i)

    # --- Check for dropped frames (large inter-frame gaps) ---
    # A gap significantly larger than 1/fps suggests x11grab missed a frame.
    expected_interval = 1.0 / fps
    gap_threshold = expected_interval * 1.8  # e.g. 90ms for 20fps (50ms expected)
    dropped_frame_gaps: List[Dict[str, Any]] = []
    for i in range(1, n_frames):
        gap = frame_timestamps[i] - frame_timestamps[i - 1]
        if gap > gap_threshold and (frame_timestamps[i - 1] - rec_start) > _BOUNDARY_GRACE_SEC:
            dropped_frame_gaps.append({
                "between_frames": [i - 1, i],
                "gap_sec": round(gap, 4),
                "expected_frames_missed": round(gap / expected_interval) - 1,
            })

    diagnostics = {
        "n_actions": n_actions,
        "n_frames": n_frames,
        "n_matched": len(frame_indices),
        "skipped_frames_start": skipped_frames_start,
        "skipped_frames_end": skipped_frames_end,
        "unmatched_actions_start": unmatched_actions_start,
        "unmatched_actions_end": unmatched_actions_end,
        "mean_delta_sec": (sum(time_deltas) / len(time_deltas)) if time_deltas else 0.0,
        "max_abs_delta_sec": max(abs(d) for d in time_deltas) if time_deltas else 0.0,
        "min_delta_sec": min(time_deltas) if time_deltas else 0.0,
        "max_delta_sec": max(time_deltas) if time_deltas else 0.0,
        "duplicate_frame_count": len(duplicate_frames),
        "interior_unconsumed_count": len(interior_unconsumed),
        "dropped_frame_gaps": len(dropped_frame_gaps),
    }

    return frame_indices, diagnostics




def _repair_pairs_by_pose_seq(
    actions: List[Dict[str, Any]],
    frame_indices: List[int],
    sidecar: Optional[List[Dict[str, Any]]],
    match_times: List[float],
) -> Tuple[List[int], List[Optional[str]], Dict[str, Any]]:
    """Pair action samples with frames that rendered the same movement packet (T12).

    Action samples (controller, 20 Hz) and camera frames (client, 20 fps) are sampled
    asynchronously. With the camera pose link, a sample's yaw/pitch is the bot's last
    movement packet (``pose_seq`` in the sample) and every injected frame records the
    packet it rendered (``pose_seq`` in the sidecar). The first frame at or after the
    sample time often already shows the next packet (sent between the sample and the
    frame), which puts a whole tick of camera motion between the image and its label.
    When the time-matched frame k rendered another packet, the neighbouring frame (k-1
    or k+1) that rendered the sample's packet is used instead (the one closer in time;
    k-1 can precede the sample time by less than one frame interval).

    Returns ``(frame_indices, pair kind per action, diagnostics)``; the pair kind is
    "same" (time match already right), "prev"/"next" (re-paired), "none" (no
    neighbour rendered the packet; time match kept) or None (not checkable).
    """
    kinds: List[Optional[str]] = [None] * len(frame_indices)
    diag: Dict[str, Any] = {"checked": 0, "same": 0, "prev": 0, "next": 0, "none": 0,
                            "skipped_no_seq": 0, "skipped_not_injected": 0}
    if sidecar is None:
        diag["reason"] = "no sidecar"
        return frame_indices, kinds, diag

    def rendered_seq(k: int) -> Optional[int]:
        if 0 <= k < len(sidecar):
            rec = sidecar[k]
            seq = rec.get("pose_seq")
            if rec.get("pose_source") == "injected" and isinstance(seq, int) and not isinstance(seq, bool):
                return seq
        return None

    out = list(frame_indices)
    for i, k in enumerate(frame_indices):
        seq = actions[i].get("pose_seq")
        if not isinstance(seq, int) or isinstance(seq, bool):
            diag["skipped_no_seq"] += 1
            continue
        if rendered_seq(k) is None:
            diag["skipped_not_injected"] += 1
            continue
        diag["checked"] += 1
        if rendered_seq(k) == seq:
            kinds[i] = "same"
            diag["same"] += 1
            continue
        t = float(actions[i].get("epochTime", 0.0))
        cands = [(abs(match_times[j] - t), j, name) for j, name in ((k - 1, "prev"), (k + 1, "next"))
                 if 0 <= j < len(match_times) and rendered_seq(j) == seq]
        if not cands:
            kinds[i] = "none"
            diag["none"] += 1
            continue
        _, j, name = min(cands)
        out[i] = j
        kinds[i] = name
        diag[name] += 1
    if diag["checked"]:
        diag["same_packet_fraction_before"] = round(diag["same"] / diag["checked"], 4)
        diag["same_packet_fraction_after"] = round(
            (diag["same"] + diag["prev"] + diag["next"]) / diag["checked"], 4)
    if diag["prev"] or diag["next"]:
        # the time-match diagnostics (delta_sec, duplicate frames) describe the pairing before this step
        deltas = [match_times[j] - float(a.get("epochTime", 0.0)) for a, j in zip(actions, out)]
        diag["min_delta_sec_after"] = min(deltas)
        diag["max_delta_sec_after"] = max(deltas)
        diag["duplicate_frame_count_after"] = sum(1 for c in Counter(out).values() if c > 1)
    return out, kinds, diag


# ---------------------------------------------------------------------------
# Frame extraction (shared by both modes)
# ---------------------------------------------------------------------------

# Seeking further than this many frames ahead is done with a container seek
# instead of decoding every frame in between.
_SEQUENTIAL_READ_LIMIT = 120


class _ExactFrameReader:
    """Read frame *k* of a recording, where k counts decoded frames.

    OpenCV seeks by time (``frame_number / fps``), which picks the wrong frame
    in variable-frame-rate recordings (wall-clock PTS with missed capture slots,
    as written by both the x11grab and the in-client recorder). When the
    per-frame PTS list from ffprobe is known, every seek is verified against
    the decoded frame's timestamp and corrected by decoding forward, so the
    returned frame is exactly frame k. Without PTS it degrades to OpenCV's own
    frame seek (the previous behaviour).
    """

    def __init__(self, recording_path: Path, frame_timestamps: Optional[List[float]]):
        self.path = recording_path
        self.pts = frame_timestamps
        self.cap = self._open()
        self.pos = -1  # index of the frame last grabbed
        self.seeks = 0
        self.grabs = 0
        self.msec0 = 0.0
        self.rel_ms: Optional[List[float]] = None
        if frame_timestamps:
            if not self._grab():
                raise RuntimeError(f"Failed to read the first frame of {recording_path}")
            self.msec0 = float(self.cap.get(cv2.CAP_PROP_POS_MSEC))
            self.rel_ms = [(t - frame_timestamps[0]) * 1000.0 + self.msec0 for t in frame_timestamps]
            # Half the smallest inter-frame gap: tolerance for "same frame"
            gaps = [b - a for a, b in zip(self.rel_ms, self.rel_ms[1:]) if b > a]
            self.tol_ms = max(0.4, min(gaps) / 2.0) if gaps else 0.5

    def _open(self):
        cap = cv2.VideoCapture(str(self.path))
        if not cap.isOpened():
            raise RuntimeError(f"Failed to open camera recording {self.path}")
        return cap

    def _grab(self) -> bool:
        ok = self.cap.grab()
        if ok:
            self.pos += 1
            self.grabs += 1
        return ok

    def _group_at_msec(self, msec: float) -> Optional[Tuple[int, int]]:
        """(first, last) frame index whose time is ``msec``, or None.

        Several frames can share one PTS (the in-client recorder's wall-clock
        stamps are rounded to 1/fps), so a decoded time may name a group.
        """
        assert self.rel_ms is not None
        j = bisect.bisect_left(self.rel_ms, msec)
        best = None
        for k in (j - 1, j):
            if 0 <= k < len(self.rel_ms) and abs(self.rel_ms[k] - msec) <= self.tol_ms:
                if best is None or abs(self.rel_ms[k] - msec) < abs(self.rel_ms[best] - msec):
                    best = k
        if best is None:
            return None
        t = self.rel_ms[best]
        first = bisect.bisect_left(self.rel_ms, t)
        last = bisect.bisect_right(self.rel_ms, t) - 1
        return first, last

    def _landed_index(self) -> Optional[int]:
        """Index of the frame just grabbed after a container seek, or None.

        If its time is shared by several frames, decode forward to the first
        frame of the next distinct time, whose index is unambiguous.
        """
        msec = float(self.cap.get(cv2.CAP_PROP_POS_MSEC))
        group = self._group_at_msec(msec)
        if group is None:
            return None
        first, last = group
        if first == last:
            return first
        for _ in range(last - first + 1):
            if not self.cap.grab():
                return None
            self.grabs += 1
            nxt = float(self.cap.get(cv2.CAP_PROP_POS_MSEC))
            if abs(nxt - msec) > self.tol_ms:
                after = self._group_at_msec(nxt)
                return after[0] if after is not None and after[0] == last + 1 else None
        return None

    def _restart(self) -> None:
        self.cap.release()
        self.cap = self._open()
        self.pos = -1

    def _seek_before(self, idx: int) -> None:
        """Position the decoder on some frame <= idx (verified via its PTS)."""
        assert self.rel_ms is not None
        margin = 1.0
        while True:
            target = self.rel_ms[idx] - margin * 1000.0
            if target <= self.rel_ms[0]:
                self._restart()
                return
            self.seeks += 1
            self.cap.set(cv2.CAP_PROP_POS_MSEC, target)
            if not self.cap.grab():
                self._restart()
                return
            self.grabs += 1
            cur = self._landed_index()
            if cur is not None and cur <= idx:
                self.pos = cur
                return
            margin *= 2.0

    def read(self, idx: int):
        if self.rel_ms is None:
            # No PTS list: OpenCV frame seek (exact only for constant frame rate)
            if idx != self.pos + 1:
                self.cap.set(cv2.CAP_PROP_POS_FRAMES, idx)
                self.seeks += 1
                self.pos = idx - 1
            ok, frame = self.cap.read()
            if not ok:
                raise RuntimeError(f"Failed to read frame {idx} from camera recording")
            self.pos += 1
            self.grabs += 1
            return frame
        if idx < self.pos or idx - self.pos > _SEQUENTIAL_READ_LIMIT:
            self._seek_before(idx)
        while self.pos < idx:
            if not self._grab():
                raise RuntimeError(f"Failed to read frame {idx} from camera recording")
        got = float(self.cap.get(cv2.CAP_PROP_POS_MSEC))
        if abs(got - self.rel_ms[idx]) > self.tol_ms:
            raise RuntimeError(
                f"Decoded frame {idx} has time {got:.1f} ms, expected {self.rel_ms[idx]:.1f} ms "
                f"(ffprobe and the decoder disagree on the frame order)")
        ok, frame = self.cap.retrieve()
        if not ok:
            raise RuntimeError(f"Failed to decode frame {idx} from camera recording")
        return frame

    def release(self) -> None:
        self.cap.release()


def _write_frames_by_index(
    recording_path: Path,
    frame_indices: List[int],
    fps: float,
    output_path: Path,
    frame_timestamps: Optional[List[float]] = None,
) -> Dict[str, Any]:
    """Write frame ``frame_indices[i]`` of the recording as output frame i.

    Duplicate indices (several actions per camera frame) reuse the cached
    frame. With ``frame_timestamps`` (ffprobe PTS, one per frame) the frame
    positions are exact even for variable-frame-rate recordings.
    Returns extraction stats.
    """
    start_time = time.time()

    if not frame_indices:
        raise ValueError("No frames requested for alignment")

    reader = _ExactFrameReader(recording_path, frame_timestamps)
    if frame_timestamps:
        total_frames = len(frame_timestamps)
    else:
        total_frames = int(reader.cap.get(cv2.CAP_PROP_FRAME_COUNT))
    width = int(reader.cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    height = int(reader.cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    fourcc = cv2.VideoWriter_fourcc(*"mp4v")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    writer = cv2.VideoWriter(str(output_path), fourcc, fps, (width, height))

    last_frame_idx = -1
    last_frame = None
    cache_hits = 0
    try:
        for i, frame_idx in enumerate(frame_indices):
            if frame_idx < 0 or frame_idx >= total_frames:
                raise RuntimeError(
                    f"Action {i} maps to frame {frame_idx}, but camera only has {total_frames} frames"
                )
            if frame_idx == last_frame_idx and last_frame is not None:
                writer.write(last_frame)
                cache_hits += 1
                continue
            frame = reader.read(frame_idx)
            writer.write(frame)
            last_frame_idx = frame_idx
            last_frame = frame
    finally:
        writer.release()
        reader.release()

    total_time = time.time() - start_time
    print(f"[align] Extracted {len(frame_indices)} frames in {total_time:.1f}s "
          f"({reader.seeks} seeks, {reader.grabs} decoded)")
    return {"exact_seek": frame_timestamps is not None, "seeks": reader.seeks,
            "decoded_frames": reader.grabs, "cache_hits": cache_hits,
            "extract_seconds": round(total_time, 3)}


# ---------------------------------------------------------------------------
# Metadata / mapping builders
# ---------------------------------------------------------------------------

def _build_action_mapping_wallclock(
    actions: List[Dict[str, Any]],
    frame_indices: List[int],
    frame_timestamps: List[float],
    sidecar: Optional[List[Dict[str, Any]]] = None,
    frame_pts: Optional[List[float]] = None,
    pair_kinds: Optional[List[Optional[str]]] = None,
) -> List[Dict[str, Any]]:
    """Build frame-to-action mapping for wallclock-timestamp alignment.

    ``frame_timestamps`` are the times used for matching (``frame_time_sec``),
    ``frame_pts`` the MKV PTS (``frame_pts_sec``; defaults to the same list).
    With a usable sidecar every entry also carries the pose the client
    actually rendered in the matched frame (MC degrees, eye position).
    """
    if frame_pts is None:
        frame_pts = frame_timestamps
    mapping: List[Dict[str, Any]] = []
    for action_idx, (entry, frame_idx) in enumerate(zip(actions, frame_indices)):
        action_time_sec = float(entry.get("epochTime", 0.0))
        frame_time_sec = frame_timestamps[frame_idx] if frame_idx < len(frame_timestamps) else 0.0
        item = {
            "action_index": action_idx,
            "renderTime_ms": float(entry.get("renderTime", 0.0)),
            "action_time_sec": action_time_sec,
            "relative_time_ms": float(entry.get("relativeTimeMs", 0.0)),
            "frame_index": frame_idx,
            "frame_time_sec": frame_time_sec,
            "frame_pts_sec": frame_pts[frame_idx] if frame_idx < len(frame_pts) else 0.0,
            "delta_sec": frame_time_sec - action_time_sec,
        }
        if sidecar is not None:
            rec = sidecar[frame_idx]
            item["cam_yaw"] = rec.get("cam_yaw")
            item["cam_pitch"] = rec.get("cam_pitch")
            item["cam_pos"] = rec.get("cam_pos")
            item["fov"] = rec.get("fov")
            item["sidecar_epoch_ms"] = rec.get("epoch_ms")
            item["cam_game_time"] = rec.get("game_time")
            item["cam_screen"] = rec.get("screen")
            # T12 pose link (Windows camera mod): where the rendered pose came from, and the
            # server-followed player look of the same frame (for comparison)
            for key in ("pose_source", "pose_age_ms", "pose_seq", "pose_t", "pose_held", "pose_fallback",
                        "player_yaw", "player_pitch"):
                if key in rec:
                    item[key] = rec.get(key)
        if "pose_seq" in entry:
            # packet the action sample's yaw/pitch came from (controller pose link)
            item["action_pose_seq"] = entry.get("pose_seq")
        if pair_kinds is not None and pair_kinds[action_idx] is not None:
            item["pose_pair"] = pair_kinds[action_idx]
        mapping.append(item)
    return mapping




# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def _print_wallclock_warnings(diagnostics: Dict[str, Any]) -> None:
    """Print compact warnings for alignment quality issues."""
    parts: List[str] = []
    gaps = diagnostics.get("dropped_frame_gaps", 0)
    if gaps:
        parts.append(f"{gaps} dropped-frame gap(s)")
    dup = diagnostics.get("duplicate_frame_count", 0)
    if dup:
        parts.append(f"{dup} duplicate frame(s)")
    interior = diagnostics.get("interior_unconsumed_count", 0)
    if interior:
        parts.append(f"{interior} interior unconsumed frame(s)")
    if parts:
        print(f"[align] WARNING: {'; '.join(parts)}", file=sys.stderr)


def align_recording(config: AlignmentInput) -> Dict[str, Any]:
    """Align camera recording to action trace.

    Uses per-frame wallclock timestamps extracted from the MKV via ffprobe.
    Falls back to legacy computed-index mode **only** if
    ``ALLOW_LEGACY_ALIGNMENT`` is ``True``.
    """
    actions = _load_actions(config.actions_path)
    camera_meta = _ensure_camera_meta(config.camera_meta_path)

    fps = float(camera_meta["fps"])
    camera_start_time_sec = float(camera_meta["start_epoch_seconds"])

    recording_path = _resolve_recorded_path(
        str(camera_meta["recording_path"]), config.camera_meta_path.parent
    )
    if not recording_path.exists():
        raise FileNotFoundError(
            f"Camera recording {camera_meta['recording_path']} not found "
            f"(also looked next to {config.camera_meta_path})"
        )

    # ------------------------------------------------------------------
    # Extract per-frame timestamps and decide alignment mode
    # ------------------------------------------------------------------
    frame_timestamps = _extract_frame_timestamps(recording_path)
    use_wallclock = (
        frame_timestamps is not None
        and _has_wallclock_timestamps(frame_timestamps, camera_meta)
    )

    if not use_wallclock:
        reason = (
            "Frame timestamps not found or not wallclock"
            if frame_timestamps is None
            else "Frame timestamps present but not wallclock (PTS too small)"
        )
        raise RuntimeError(
            f"[align] {reason}. "
            f"Wallclock-timestamp alignment is required. "
        )


    # ------------------------------------------------------------------
    # Wallclock-timestamp alignment (primary path)
    # ------------------------------------------------------------------
    assert frame_timestamps is not None  # for type checker
    print(f"[align] Using wallclock timestamps ({len(frame_timestamps)} frames extracted)")

    sidecar, sidecar_diag = _load_sidecar(
        camera_meta, config.camera_meta_path.parent, len(frame_timestamps),
    )
    # Matching clock. The in-client recorder's sidecar has the exact capture
    # time of every frame, whereas the PTS written by the contract's
    # rawvideo + wall-clock ffmpeg command are pipe-arrival times rounded to
    # 1/fps (several frames can share one PTS), so prefer the sidecar.
    match_times = frame_timestamps
    match_clock = "pts"
    if sidecar is not None and config.match_clock == "auto":
        cap_times, why = _sidecar_capture_times(sidecar)
        if cap_times is not None:
            match_times = cap_times
            match_clock = "sidecar_capture_time"
        else:
            sidecar_diag["match_clock_reason"] = why
    elif config.match_clock != "auto":
        sidecar_diag["match_clock_reason"] = f"match_clock={config.match_clock} requested"

    frame_indices, diagnostics = _match_actions_to_frames(
        actions, match_times, fps,
    )
    diagnostics["match_clock"] = match_clock
    diagnostics["pts_duplicate_frames"] = _pts_duplicates(frame_timestamps)

    if not frame_indices:
        raise RuntimeError("No actions could be matched to video frames")

    pair_kinds: Optional[List[Optional[str]]] = None
    if config.pose_pairing == "seq" and sidecar is not None:
        frame_indices, pair_kinds, pair_diag = _repair_pairs_by_pose_seq(
            actions, frame_indices, sidecar, match_times)
        diagnostics["pose_seq_pairing"] = pair_diag
    else:
        diagnostics["pose_seq_pairing"] = {
            "reason": "disabled" if config.pose_pairing != "seq" else "no sidecar"}

    # Trim actions to only those that were matched
    matched_actions = actions[: len(frame_indices)]

    extract_stats = _write_frames_by_index(
        recording_path, frame_indices, fps, config.output_video_path, frame_timestamps,
    )
    diagnostics["extraction"] = extract_stats

    diagnostics.update(sidecar_diag)
    if sidecar is not None:
        diagnostics["pts_minus_sidecar_ms"] = _pts_vs_sidecar_stats(sidecar, frame_timestamps, frame_indices)
    if "frames" in camera_meta:
        diagnostics["camera_meta_frames"] = camera_meta["frames"]
    camera_params = _build_camera_params(camera_meta, config.camera_meta_path.parent)

    action_times_sec = [float(a["epochTime"]) for a in matched_actions]
    mapping = _build_action_mapping_wallclock(
        matched_actions, frame_indices, match_times, sidecar, frame_timestamps, pair_kinds,
    )
    if sidecar is not None:
        # Frames matched to actions while the camera client showed a screen (e.g. the
        # ReceivingLevelScreen of a respawn: a skin change re-sends the camera player to itself).
        # Kept in the clip (dropping would shift the action alignment) but counted here and
        # visible per frame as frame_mapping[i].cam_screen.
        screen_counts: Dict[str, int] = {}
        for item in mapping:
            scr = item.get("cam_screen")
            if scr:
                screen_counts[scr] = screen_counts.get(scr, 0) + 1
        diagnostics["screen_frames"] = sum(screen_counts.values())
        diagnostics["screen_frames_by_screen"] = screen_counts

    output_metadata = {
        "actions_path": str(config.actions_path),
        "camera_meta_path": str(config.camera_meta_path),
        "recording_path": str(recording_path),
        "aligned_video_path": str(config.output_video_path),
        "alignment_mode": "wallclock",
        "fps": fps,
        "camera_start_time_sec": camera_start_time_sec,
        "first_frame_time_sec": match_times[0] if match_times else None,
        "last_frame_time_sec": match_times[-1] if match_times else None,
        "total_video_frames": len(frame_timestamps),
        "first_action_time_sec": min(action_times_sec) if action_times_sec else None,
        "last_action_time_sec": max(action_times_sec) if action_times_sec else None,
        "camera_params": camera_params,
        "diagnostics": diagnostics,
        "frame_mapping": mapping,
    }

    config.output_metadata_path.parent.mkdir(parents=True, exist_ok=True)
    with config.output_metadata_path.open("w", encoding="utf-8") as fh:
        json.dump(output_metadata, fh)

    # Print diagnostics summary
    d = diagnostics
    print(f"[align] Matched {d['n_matched']}/{d['n_actions']} actions to "
          f"{d['n_frames']} video frames")
    print(f"[align] Mean delta: {d['mean_delta_sec']*1000:.1f}ms, "
          f"max |delta|: {d['max_abs_delta_sec']*1000:.1f}ms")
    if d['skipped_frames_start'] > 0 or d['skipped_frames_end'] > 0:
        print(f"[align] Skipped frames: {d['skipped_frames_start']} at start, "
              f"{d['skipped_frames_end']} at end")
    if d['unmatched_actions_start'] > 0 or d['unmatched_actions_end'] > 0:
        print(f"[align] Unmatched actions: {d['unmatched_actions_start']} at start, "
              f"{d['unmatched_actions_end']} at end")

    print(f"[align] Matching clock: {match_clock}")
    if diagnostics.get("sidecar_used"):
        dpts = diagnostics.get("pts_minus_sidecar_ms") or {}
        print(f"[align] Rendered-pose sidecar attached "
              f"(PTS - capture time: median {dpts.get('median', float('nan')):.1f} ms)")
    else:
        print(f"[align] WARNING: no rendered-pose sidecar: {diagnostics.get('sidecar_reason')}",
              file=sys.stderr)
    if not camera_params.get("options_recorded"):
        print("[align] WARNING: camera meta has no 'options' (fov, render distance, ...)",
              file=sys.stderr)

    psp = diagnostics.get("pose_seq_pairing") or {}
    if psp.get("checked"):
        print(f"[align] Pose-seq pairing: {psp['checked']} samples checked, {psp['same']} already on "
              f"their packet, {psp['prev']} moved to the previous frame, {psp['next']} to the next, "
              f"{psp['none']} without a frame of their packet")

    if match_clock == "pts" and diagnostics["pts_duplicate_frames"]:
        print(f"[align] WARNING: {diagnostics['pts_duplicate_frames']} frame(s) do not have a later PTS "
              f"than the previous frame (PTS quantised to 1/fps); matching is only frame-accurate",
              file=sys.stderr)

    if diagnostics.get("screen_frames"):
        print(f"[align] WARNING: {diagnostics['screen_frames']} matched frame(s) show a client screen "
              f"{diagnostics['screen_frames_by_screen']} (not in-game view)", file=sys.stderr)

    # Warnings for data-quality issues
    _print_wallclock_warnings(diagnostics)

    return output_metadata

@dataclass
class BotConfig:
    name: str
    actions_suffix: str
    camera_meta: Path
    output_dir: Path


def parse_args(argv: Iterable[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--actions-dir",
        type=Path,
        required=True,
        help="Directory containing Mineflayer action traces (*.json)",
    )
    parser.add_argument(
        "--camera-prefix",
        type=Path,
        required=True,
        help="Directory containing camera outputs (expects output_alpha/ and output_bravo/)",
    )
    parser.add_argument(
        "--bot",
        type=str,
        choices=["Alpha", "Bravo"],
        required=True,
        help="Which bot to process",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=None,
        help="Optional base directory for outputs (default: ./aligned/<bot>)",
    )
    parser.add_argument(
        "--episode-file",
        type=Path,
        default=None,
        help="Process single episode file (overrides directory processing)",
    )
    parser.add_argument(
        "--pose-pairing",
        choices=["seq", "time"],
        default="seq",
        help="'seq' (default): with the camera pose link, pair each action sample with the "
             "(neighbouring) frame that rendered the sample's movement packet (pose_seq); "
             "'time': first frame at or after the sample time only",
    )
    parser.add_argument(
        "--match-clock",
        choices=["auto", "pts"],
        default="auto",
        help="Frame times used to match actions: 'auto' = the camera sidecar's capture "
             "times when usable, else the MKV PTS; 'pts' = always the MKV PTS",
    )
    return parser.parse_args(list(argv))


def build_bot_config(
    actions_dir: Path, camera_prefix: Path, bot: str, output_base: Optional[Path]
) -> Dict[str, BotConfig]:
    """Return BotConfig mapping for the selected bot.

    - Output directory defaults to ./aligned/<bot> unless overridden by --output-dir.
    - camera_prefix may already be an output_{alpha|bravo}/<instance> directory
      (as in orchestration); handle both prefixed and non-prefixed forms.
    """
    output_dir = (output_base or Path.cwd() / "aligned")
    if bot == "Alpha":
        # If camera_prefix already points into output_alpha, use it directly
        if "output_alpha" in str(camera_prefix):
            camera_meta = camera_prefix / "camera_alpha_meta.json"
        else:
            camera_meta = camera_prefix / "output_alpha" / "camera_alpha_meta.json"
        return {
            "Alpha": BotConfig(
                name="Alpha",
                actions_suffix="_Alpha_",
                camera_meta=camera_meta,
                output_dir=output_dir,
            )
        }
    else:
        # If camera_prefix already points into output_bravo, use it directly
        if "output_bravo" in str(camera_prefix):
            camera_meta = camera_prefix / "camera_bravo_meta.json"
        else:
            camera_meta = camera_prefix / "output_bravo" / "camera_bravo_meta.json"
        return {
            "Bravo": BotConfig(
                name="Bravo",
                actions_suffix="_Bravo_",
                camera_meta=camera_meta,
                output_dir=output_dir,
            )
        }


def bot_for_actions(path: Path, configs: Dict[str, BotConfig]) -> Optional[BotConfig]:
    for config in configs.values():
        if config.actions_suffix in path.name:
            return config
    return None


def resolve_actions_dir(explicit: Path) -> Path:
    actions_dir = explicit
    if not actions_dir.exists():
        raise FileNotFoundError(f"Actions directory not found: {actions_dir}")
    return actions_dir


def ensure_metadata(meta_path: Path) -> None:
    if not meta_path.exists():
        raise FileNotFoundError(f"Camera metadata missing: {meta_path}")


def _meta_interval(meta: Dict[str, Any]) -> Tuple[float, float]:
    """[start, stop] epoch seconds of a recording (stop open-ended if unknown)."""
    start = float(meta.get("start_epoch_seconds", 0.0))
    stop = meta.get("stop_epoch_seconds")
    if isinstance(stop, (int, float)):
        return start, float(stop)
    frames, fps = meta.get("frames"), meta.get("fps")
    if isinstance(frames, (int, float)) and isinstance(fps, (int, float)) and fps > 0:
        return start, start + float(frames) / float(fps)
    return start, float("inf")


def select_camera_meta(default_meta: Path, actions_path: Path) -> Path:
    """Pick the camera meta whose recording covers this episode.

    A camera restarted by the Windows orchestrator records to
    ``camera_<bot>_r<n>.mkv`` with ``camera_<bot>_r<n>_meta.json`` next to the
    first recording (which may not exist if the camera died before recording).
    Among ``camera_<bot>_meta.json`` and those, take the one whose
    [start_epoch_seconds, stop_epoch_seconds] overlaps the episode's action
    times the most; with a single candidate (the usual case) nothing is read.
    """
    stem = default_meta.name[: -len("_meta.json")] if default_meta.name.endswith("_meta.json") \
        else default_meta.stem
    candidates = [default_meta] if default_meta.exists() else []
    if default_meta.parent.is_dir():
        candidates += sorted(default_meta.parent.glob(f"{stem}_r*_meta.json"))
    if not candidates:
        return default_meta
    if len(candidates) == 1:
        return candidates[0]
    try:
        times = [float(a["epochTime"]) for a in _load_actions(actions_path) if "epochTime" in a]
    except (OSError, ValueError, TypeError, KeyError):
        times = []
    if not times:
        return candidates[0]
    t0, t1 = min(times), max(times)
    best, best_overlap = candidates[0], -1.0
    for cand in candidates:
        try:
            with cand.open("r", encoding="utf-8") as fh:
                start, stop = _meta_interval(json.load(fh))
        except (OSError, ValueError, TypeError):
            continue
        # zero-length episodes count as covered when inside the interval
        overlap = min(t1, stop) - max(t0, start)
        if overlap < 0:
            continue
        if overlap > best_overlap:
            best, best_overlap = cand, overlap
    if best != default_meta:
        print(f"[align] using camera meta {best.name} (covers the episode's action times)")
    return best



def process_actions(
    actions_dir: Path,
    configs: Dict[str, BotConfig],
    match_clock: str = "auto",
    pose_pairing: str = "seq",
) -> int:
    actions_processed = 0
    for actions_path in sorted(actions_dir.glob("*.json")):
        if actions_path.name.endswith("_meta.json"):
            continue
        config = bot_for_actions(actions_path, configs)
        if config is None:
            continue

        camera_meta = select_camera_meta(config.camera_meta, actions_path)
        ensure_metadata(camera_meta)
        config.output_dir.mkdir(parents=True, exist_ok=True)

        output_video = config.output_dir / f"{actions_path.stem}_camera.mp4"
        output_meta = config.output_dir / f"{actions_path.stem}_camera_meta.json"

        alignment_input = AlignmentInput(
            actions_path=actions_path,
            camera_meta_path=camera_meta,
            output_video_path=output_video,
            output_metadata_path=output_meta,
            ffmpeg_path="ffmpeg",
            margin_start=0.0,
            margin_end=0.0,
            match_clock=match_clock,
            pose_pairing=pose_pairing,
        )

        align_start = time.time()
        try:
            metadata = align_recording(alignment_input)
        except Exception as exc:  # noqa: BLE001 - surface alignment failure to caller
            print(f"[align] failed for {actions_path}: {exc}", file=sys.stderr)
            continue
        align_time = time.time() - align_start


        with output_meta.open("w", encoding="utf-8") as fh:
            json.dump(metadata, fh)

        print(
            f"[align] wrote {metadata['aligned_video_path']} (total: {align_time:.1f}s)"
        )
        actions_processed += 1

    return actions_processed


def process_single_episode(
    episode_path: Path,
    configs: Dict[str, BotConfig],
    match_clock: str = "auto",
    pose_pairing: str = "seq",
) -> bool:
    """Process a single episode file. Returns True if successful."""
    if episode_path.name.endswith("_meta.json"):
        return False

    config = bot_for_actions(episode_path, configs)
    if config is None:
        return False

    try:
        camera_meta = select_camera_meta(config.camera_meta, episode_path)
        ensure_metadata(camera_meta)
        config.output_dir.mkdir(parents=True, exist_ok=True)

        output_video = config.output_dir / f"{episode_path.stem}_camera.mp4"
        output_meta = config.output_dir / f"{episode_path.stem}_camera_meta.json"

        alignment_input = AlignmentInput(
            actions_path=episode_path,
            camera_meta_path=camera_meta,
            output_video_path=output_video,
            output_metadata_path=output_meta,
            ffmpeg_path="ffmpeg",
            margin_start=0.0,
            margin_end=0.0,
            match_clock=match_clock,
            pose_pairing=pose_pairing,
        )

        align_start = time.time()
        metadata = align_recording(alignment_input)
        align_time = time.time() - align_start

        

        with output_meta.open("w", encoding="utf-8") as fh:
            json.dump(metadata, fh)

        print(f"[align] wrote {metadata['aligned_video_path']} (total: {align_time:.1f}s)")
        return True
        
    except Exception as exc:
        print(f"[align] failed for {episode_path}: {exc}", file=sys.stderr)
        return False


def main(argv: Iterable[str]) -> int:
    args = parse_args(argv)
    actions_dir = resolve_actions_dir(args.actions_dir.resolve())
    configs = build_bot_config(
        actions_dir=actions_dir,
        camera_prefix=args.camera_prefix.resolve(),
        bot=args.bot,
        output_base=args.output_dir.resolve() if args.output_dir else None,
    )

    # Single-episode fast path if provided by orchestrator
    if args.episode_file:
        episode_path = args.episode_file.resolve()
        if not episode_path.exists():
            print(f"[align] episode file not found: {episode_path}", file=sys.stderr)
            return 1
        processed = process_single_episode(
            episode_path, configs, args.match_clock, args.pose_pairing
        )
        return 0 if processed else 1

    # Otherwise process all episodes under --actions-dir
    processed = process_actions(
        actions_dir, configs, args.match_clock, args.pose_pairing
    )
    if processed == 0:
        print("[align] no action traces found; nothing to do")
    return 0


if __name__ == "__main__":  # pragma: no cover - CLI entry point
    raise SystemExit(main(sys.argv[1:]))
