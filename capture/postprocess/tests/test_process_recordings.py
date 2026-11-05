"""Unit tests for postprocess/process_recordings.py with synthetic MKV recordings.

Two kinds of recordings are made with ffmpeg:
  * a "mod-like" one: raw RGBA frames piped in real time to
    `ffmpeg -f rawvideo -use_wallclock_as_timestamps 1 -i - -vf vflip -copyts
    -fps_mode passthrough` (exactly the in-client recorder of CONTRACT.md 5.4),
    with some capture slots skipped, plus its frames sidecar;
  * a deterministic variable-frame-rate one from lavfi with epoch PTS and gaps,
    used to check that frame extraction picks exactly frame k after seeks.
Every frame encodes its own index as two grey levels (left/right half, steps of
16 so the lossy mp4v re-encode of the aligned output stays decodable), so the
aligned output can be checked frame by frame.

ffmpeg/ffprobe are taken from $FFMPEG / PATH / the Windows tools folder; the
tests are skipped when ffmpeg is unavailable.
Run:  python -m unittest discover -s postprocess/tests -v
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from typing import List, Optional
from unittest import mock

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

import cv2  # noqa: E402
import process_recordings as pr  # noqa: E402

W, H, FPS = 160, 90, 20
EPOCH0 = 1761200000.0


def find_ffmpeg() -> Optional[Path]:
    cands = [os.environ.get("FFMPEG"), shutil.which("ffmpeg"),
             os.path.join(os.environ.get("USERPROFILE", ""), r"tools\ffmpeg-9.0.2-essentials_build\bin\ffmpeg.exe")]
    for c in cands:
        if c and Path(c).exists():
            return Path(c)
    return None


FFMPEG = find_ffmpeg()


def ffprobe_path() -> str:
    p = FFMPEG.with_name("ffprobe.exe" if FFMPEG.suffix == ".exe" else "ffprobe")
    return str(p) if p.exists() else "ffprobe"


def frame_rgba(k: int) -> bytes:
    left, right = (k % 16) * 16, (k // 16) * 16
    row = bytes([left, left, left, 255]) * (W // 2) + bytes([right, right, right, 255]) * (W // 2)
    return row * H


def decode_index(frame) -> int:
    left = int(frame[H // 2, W // 4].mean())
    right = int(frame[H // 2, 3 * W // 4].mean())
    return round(left / 16) + 16 * round(right / 16)


def probe_pts(path: Path) -> List[float]:
    out = subprocess.run([ffprobe_path(), "-v", "quiet", "-select_streams", "v:0", "-show_entries",
                          "packet=pts_time", "-of", "csv=p=0", str(path)],
                         capture_output=True, text=True, check=True).stdout
    return sorted(float(x) for x in out.split())


def read_all(path: Path) -> list:
    cap = cv2.VideoCapture(str(path))
    frames = []
    while True:
        ok, f = cap.read()
        if not ok:
            break
        frames.append(f)
    cap.release()
    return frames


def make_wallclock_mkv(path: Path, n_slots: int, skip: set, burst: int = 1) -> List[dict]:
    """Pipe frames in real time like the capture mod; returns the sidecar records.

    Frames are captured at their slot times; with ``burst`` > 1 they reach
    ffmpeg in groups of that many (a stalled pipe / writer thread), so several
    frames get the same wall-clock PTS (rounded to 1/fps by the rawvideo demuxer).
    """
    cmd = [str(FFMPEG), "-hide_banner", "-loglevel", "error", "-y", "-f", "rawvideo", "-pix_fmt", "rgba",
           "-s", f"{W}x{H}", "-framerate", str(FPS), "-use_wallclock_as_timestamps", "1", "-i", "-",
           "-vf", "vflip", "-copyts", "-fps_mode", "passthrough", "-c:v", "libx264", "-preset", "veryfast",
           "-qp", "0", "-bf", "0", "-pix_fmt", "yuv420p", str(path)]
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE)
    t0 = time.time() + 0.2
    side = []
    pending: List[bytes] = []
    k = 0
    for slot in range(n_slots):
        target = t0 + slot / FPS
        while time.time() < target:
            time.sleep(0.002)
        if slot in skip:
            continue
        side.append({"i": k, "epoch_ms": time.time() * 1000.0, "slot": slot, "game_time": 5000 + slot,
                     "cam_pos": [0.5, 65.62, float(k)], "cam_yaw": -180.0 + k, "cam_pitch": 10.0 - k / 10,
                     "fov": 70.0, "player_pos": [0.5, 64.0, float(k)], "fb": [W, H], "gui_hidden": False,
                     "screen": None})
        if k % 2:  # pose-link fields (camera mod with --pose-port); absent on even frames
            side[-1].update({"pose_source": "injected", "pose_seq": k, "pose_t": side[-1]["epoch_ms"] - 20.0,
                             "pose_age_ms": 19.0, "pose_held": False})
        pending.append(frame_rgba(k))
        k += 1
        if len(pending) >= burst:
            for buf in pending:
                proc.stdin.write(buf)
                proc.stdin.flush()
            pending.clear()
    for buf in pending:
        proc.stdin.write(buf)
        proc.stdin.flush()
    proc.stdin.close()
    proc.wait(timeout=60)
    assert proc.returncode == 0
    return side


def make_vfr_mkv(path: Path, n: int) -> None:
    """Deterministic VFR file: 2 slots skipped after every 7th frame, PTS in epoch seconds."""
    vf = (f"color=c=black:s={W}x{H}:r={FPS}:d={n / FPS},format=gray,"
          f"geq=lum='if(lt(X,{W // 2}),mod(N,16)*16,floor(N/16)*16)',format=yuv420p,settb=1/1000,"
          f"setpts='(N+floor(N/7)*2)/{FPS}/TB'")
    subprocess.run([str(FFMPEG), "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i", vf,
                    "-output_ts_offset", str(EPOCH0), "-fps_mode", "passthrough", "-c:v", "libx264", "-qp", "0",
                    "-g", "30", "-bf", "0", "-pix_fmt", "yuv420p", str(path)], check=True)


def write_actions(path: Path, times_sec: List[float]) -> None:
    acts = [{"x": 0.0, "y": 64.0, "z": 0.0, "yaw": 0.1, "pitch": 0.0, "renderTime": int(t * 1000),
             "epochTime": t, "relativeTimeMs": (t - times_sec[0]) * 1000.0, "frame_count": i}
            for i, t in enumerate(times_sec)]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(acts), encoding="utf-8")


WINDOWS_OPTIONS = {"fov_deg": 70.0, "fov_effect_scale": 0.0, "render_distance": 12, "simulation_distance": 12,
                   "gui_scale": 2, "gui_scale_effective": 2, "bob_view": False, "hud_visible": True,
                   "name_tags_visible": True}


@unittest.skipIf(FFMPEG is None, "ffmpeg not available")
class TestWallclockRecording(unittest.TestCase):
    """Mod-like recording + sidecar, aligned through the unchanged CLI."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.tmp = Path(tempfile.mkdtemp(prefix="pr_test_"))
        cam = cls.tmp / "camera" / "output_alpha" / "0"
        cam.mkdir(parents=True)
        cls.mkv = cam / "camera_alpha.mkv"
        cls.side = make_wallclock_mkv(cls.mkv, 60, skip={10, 11, 25, 40})
        with (cam / "camera_alpha_frames.jsonl").open("w", encoding="utf-8") as fh:
            for s in cls.side:
                fh.write(json.dumps(s) + "\n")
        cls.pts = probe_pts(cls.mkv)
        cls.meta = {"recording_path": r"C:\somewhere\else\camera_alpha.mkv",
                    "start_epoch_seconds": cls.pts[0], "fps": FPS, "width": W, "height": H,
                    "camera_name": "CameraAlpha", "video_encoder": "libx264", "wallclock_timestamps": True,
                    "capture_method": "in_client_fbo_readback", "frames_file": "camera_alpha_frames.jsonl",
                    "platform": "windows", "mc_version": "1.21", "frames": len(cls.side),
                    "options": WINDOWS_OPTIONS}
        (cam / "camera_alpha_meta.json").write_text(json.dumps(cls.meta), encoding="utf-8")
        # actions: 20 Hz from 0.4 s after the first frame for 1.5 s, not tick-locked
        cls.actions = cls.tmp / "output" / "20261023_120000_000003_Alpha_instance_000.json"
        write_actions(cls.actions, [cls.pts[0] + 0.4 + 0.05 * i + 0.013 for i in range(30)])

    @classmethod
    def tearDownClass(cls) -> None:
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def run_cli(self, out_dir: Path) -> subprocess.CompletedProcess:
        env = dict(os.environ, FFPROBE=ffprobe_path())
        return subprocess.run(
            [sys.executable, str(HERE.parent / "process_recordings.py"), "--bot", "Alpha",
             "--actions-dir", str(self.actions.parent), "--camera-prefix", str(self.tmp / "camera" / "output_alpha" / "0"),
             "--episode-file", str(self.actions), "--output-dir", str(out_dir)],
            capture_output=True, text=True, env=env, timeout=300)

    def test_frame_count_matches_sidecar(self):
        self.assertEqual(len(self.pts), len(self.side))
        self.assertEqual(len(self.pts), 56)

    def test_cli_alignment_with_sidecar(self):
        out_dir = self.tmp / "aligned"
        res = self.run_cli(out_dir)
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        meta = json.loads((out_dir / f"{self.actions.stem}_camera_meta.json").read_text(encoding="utf-8"))
        mapping = meta["frame_mapping"]
        self.assertEqual(len(mapping), 30)
        # matched on the sidecar capture times: first frame captured at or after each action
        self.assertEqual(meta["diagnostics"]["match_clock"], "sidecar_capture_time")
        cap = [s["epoch_ms"] / 1000.0 for s in self.side]
        for m in mapping:
            k = m["frame_index"]
            self.assertGreaterEqual(cap[k], m["action_time_sec"])
            self.assertTrue(k == 0 or cap[k - 1] < m["action_time_sec"])
            self.assertEqual(m["frame_time_sec"], cap[k])
            self.assertEqual(m["frame_pts_sec"], self.pts[k])
            self.assertAlmostEqual(m["delta_sec"], cap[k] - m["action_time_sec"])
            s = self.side[k]
            self.assertEqual(m["cam_yaw"], s["cam_yaw"])
            self.assertEqual(m["cam_pitch"], s["cam_pitch"])
            self.assertEqual(m["cam_pos"], s["cam_pos"])
            self.assertEqual(m["fov"], 70.0)
            self.assertEqual(m["sidecar_epoch_ms"], s["epoch_ms"])
            self.assertEqual(m["cam_game_time"], s["game_time"])
            if k % 2:
                self.assertEqual((m["pose_source"], m["pose_seq"], m["pose_age_ms"], m["pose_held"]),
                                 ("injected", k, 19.0, False))
                self.assertEqual(m["pose_t"], s["pose_t"])
            else:
                self.assertNotIn("pose_source", m)
        cp = meta["camera_params"]
        for k, v in WINDOWS_OPTIONS.items():
            self.assertEqual(cp[k], v)
        self.assertEqual((cp["width"], cp["height"], cp["fps"], cp["encoder"]), (W, H, FPS, "libx264"))
        self.assertEqual((cp["platform"], cp["mc_version"], cp["capture_method"]),
                         ("windows", "1.21", "in_client_fbo_readback"))
        self.assertTrue(cp["options_recorded"])
        d = meta["diagnostics"]
        self.assertTrue(d["sidecar_used"])
        self.assertEqual(d["sidecar_lines"], 56)
        self.assertEqual(d["camera_meta_frames"], 56)
        self.assertEqual(d["pts_minus_sidecar_ms"]["n"], len(set(m["frame_index"] for m in mapping)))
        self.assertLess(abs(d["pts_minus_sidecar_ms"]["median"]), 200.0)
        self.assertTrue(d["extraction"]["exact_seek"])
        # recording path from another machine resolved next to the meta file
        self.assertEqual(Path(meta["recording_path"]).resolve(), self.mkv.resolve())
        # aligned video: frame i shows exactly recording frame mapping[i].frame_index
        frames = read_all(Path(meta["aligned_video_path"]))
        self.assertEqual(len(frames), 30)
        self.assertEqual([decode_index(f) for f in frames], [m["frame_index"] for m in mapping])

    def test_sidecar_count_mismatch_degrades(self):
        cam = self.tmp / "camera_short" / "output_alpha" / "0"
        cam.mkdir(parents=True)
        shutil.copy2(self.mkv, cam / "camera_alpha.mkv")
        with (cam / "camera_alpha_frames.jsonl").open("w", encoding="utf-8") as fh:
            for s in self.side[:-1]:
                fh.write(json.dumps(s) + "\n")
        (cam / "camera_alpha_meta.json").write_text(json.dumps(self.meta), encoding="utf-8")
        cfg = pr.AlignmentInput(self.actions, cam / "camera_alpha_meta.json", self.tmp / "short.mp4",
                                self.tmp / "short_meta.json", "ffmpeg", 0.0, 0.0)
        with mock.patch.dict(os.environ, {"FFPROBE": ffprobe_path()}), \
                contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()) as err:
            meta = pr.align_recording(cfg)
        self.assertFalse(meta["diagnostics"]["sidecar_used"])
        self.assertIn("55 valid lines", meta["diagnostics"]["sidecar_reason"])
        self.assertNotIn("cam_yaw", meta["frame_mapping"][0])
        self.assertEqual(meta["diagnostics"]["match_clock"], "pts")
        m0 = meta["frame_mapping"][0]
        self.assertEqual(m0["frame_time_sec"], m0["frame_pts_sec"])
        self.assertIn("no rendered-pose sidecar", err.getvalue())
        self.assertEqual(len(meta["frame_mapping"]), 30)

    def test_restarted_camera_meta_selected(self):
        """Only the restart's files cover the episode: camera_alpha_r1_meta.json is used."""
        cam = self.tmp / "camera_restart" / "output_alpha" / "0"
        cam.mkdir(parents=True)
        shutil.copy2(self.mkv, cam / "camera_alpha_r1.mkv")
        shutil.copy2(self.mkv.with_name("camera_alpha_frames.jsonl"), cam / "camera_alpha_r1_frames.jsonl")
        first = dict(self.meta, recording_path="camera_alpha.mkv", start_epoch_seconds=self.pts[0] - 100,
                     stop_epoch_seconds=self.pts[0] - 50, frames=1000)
        (cam / "camera_alpha_meta.json").write_text(json.dumps(first), encoding="utf-8")
        r1 = dict(self.meta, recording_path="camera_alpha_r1.mkv", frames_file="camera_alpha_r1_frames.jsonl",
                  stop_epoch_seconds=self.pts[-1] + 0.1)
        (cam / "camera_alpha_r1_meta.json").write_text(json.dumps(r1), encoding="utf-8")
        self.assertEqual(pr.select_camera_meta(cam / "camera_alpha_meta.json", self.actions).name,
                         "camera_alpha_r1_meta.json")
        out_dir = self.tmp / "aligned_restart"
        env = dict(os.environ, FFPROBE=ffprobe_path())
        res = subprocess.run(
            [sys.executable, str(HERE.parent / "process_recordings.py"), "--bot", "Alpha",
             "--actions-dir", str(self.actions.parent), "--camera-prefix", str(cam),
             "--episode-file", str(self.actions), "--output-dir", str(out_dir)],
            capture_output=True, text=True, env=env, timeout=300)
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        self.assertIn("using camera meta camera_alpha_r1_meta.json", res.stdout)
        meta = json.loads((out_dir / f"{self.actions.stem}_camera_meta.json").read_text(encoding="utf-8"))
        self.assertTrue(meta["camera_meta_path"].endswith("camera_alpha_r1_meta.json"))
        self.assertTrue(meta["diagnostics"]["sidecar_used"])
        # a lone restart meta (the first camera never recorded) is used directly
        (cam / "camera_alpha_meta.json").unlink()
        self.assertEqual(pr.select_camera_meta(cam / "camera_alpha_meta.json", self.actions).name,
                         "camera_alpha_r1_meta.json")

    def test_linux_meta_degrades(self):
        cam = self.tmp / "camera_linux" / "output_alpha" / "0"
        cam.mkdir(parents=True)
        shutil.copy2(self.mkv, cam / "camera_alpha.mkv")
        linux = {k: self.meta[k] for k in ("start_epoch_seconds", "fps", "width", "height", "video_encoder",
                                           "wallclock_timestamps")}
        linux["recording_path"] = "/output/camera_alpha.mkv"
        (cam / "camera_alpha_meta.json").write_text(json.dumps(linux), encoding="utf-8")
        cfg = pr.AlignmentInput(self.actions, cam / "camera_alpha_meta.json", self.tmp / "linux.mp4",
                                self.tmp / "linux_meta.json", "ffmpeg", 0.0, 0.0)
        with mock.patch.dict(os.environ, {"FFPROBE": ffprobe_path()}), \
                contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()) as err:
            meta = pr.align_recording(cfg)
        self.assertFalse(meta["diagnostics"]["sidecar_used"])
        self.assertFalse(meta["camera_params"]["options_recorded"])
        self.assertEqual(meta["camera_params"]["width"], W)
        self.assertIn("no 'options'", err.getvalue())
        self.assertEqual(len(meta["frame_mapping"]), 30)


@unittest.skipIf(FFMPEG is None, "ffmpeg not available")
class TestQuantisedPts(unittest.TestCase):
    """Frames reaching ffmpeg in bursts share one wall-clock PTS (rounded to 1/fps)."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.tmp = Path(tempfile.mkdtemp(prefix="pr_dup_"))
        cam = cls.tmp / "output_alpha" / "0"
        cam.mkdir(parents=True)
        cls.mkv = cam / "camera_alpha.mkv"
        cls.side = make_wallclock_mkv(cls.mkv, 100, skip={50}, burst=4)
        with (cam / "camera_alpha_frames.jsonl").open("w", encoding="utf-8") as fh:
            for s in cls.side:
                fh.write(json.dumps(s) + "\n")
        cls.pts = probe_pts(cls.mkv)
        cls.meta_path = cam / "camera_alpha_meta.json"
        cls.meta_path.write_text(json.dumps(
            {"recording_path": "camera_alpha.mkv", "start_epoch_seconds": cls.pts[0], "fps": FPS, "width": W,
             "height": H, "video_encoder": "libx264", "wallclock_timestamps": True,
             "frames_file": "camera_alpha_frames.jsonl", "platform": "windows", "options": WINDOWS_OPTIONS}),
            encoding="utf-8")
        cls.actions = cls.tmp / "out" / "20261023_120000_000005_Alpha_instance_000.json"
        cap0 = cls.side[0]["epoch_ms"] / 1000.0
        write_actions(cls.actions, [cap0 + 0.5 + 0.05 * i + 0.011 for i in range(60)])

    @classmethod
    def tearDownClass(cls) -> None:
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def align(self, name: str, match_clock: str = "auto") -> dict:
        cfg = pr.AlignmentInput(self.actions, self.meta_path, self.tmp / f"{name}.mp4",
                                self.tmp / f"{name}_meta.json", "ffmpeg", 0.0, 0.0, match_clock)
        with mock.patch.dict(os.environ, {"FFPROBE": ffprobe_path()}), \
                contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            return pr.align_recording(cfg)

    def test_pts_are_quantised(self):
        self.assertEqual(len(self.pts), len(self.side))
        self.assertGreater(pr._pts_duplicates(self.pts), len(self.pts) // 3)
        for t in self.pts:  # every PTS lies on the 1/fps grid
            self.assertAlmostEqual(t * FPS, round(t * FPS), delta=1e-3)

    def test_sidecar_clock_alignment(self):
        meta = self.align("dup")
        d = meta["diagnostics"]
        self.assertEqual(d["match_clock"], "sidecar_capture_time")
        self.assertGreater(d["pts_duplicate_frames"], 0)
        cap = [s["epoch_ms"] / 1000.0 for s in self.side]
        idx = [m["frame_index"] for m in meta["frame_mapping"]]
        self.assertEqual(len(idx), 60)
        for m, k in zip(meta["frame_mapping"], idx):
            self.assertGreaterEqual(cap[k], m["action_time_sec"])
            self.assertLess(cap[k - 1], m["action_time_sec"])
        self.assertEqual([decode_index(f) for f in read_all(self.tmp / "dup.mp4")], idx)

    def test_pts_clock_option(self):
        meta = self.align("dup_pts", "pts")
        self.assertEqual(meta["diagnostics"]["match_clock"], "pts")
        self.assertIn("match_clock_reason", meta["diagnostics"])
        for m in meta["frame_mapping"]:
            k = m["frame_index"]
            self.assertEqual(m["frame_time_sec"], self.pts[k])
            self.assertGreaterEqual(self.pts[k], m["action_time_sec"])
            # first of its PTS group: PTS matching cannot tell the frames of a group apart
            self.assertTrue(k == 0 or self.pts[k - 1] < self.pts[k])
        self.assertEqual([decode_index(f) for f in read_all(self.tmp / "dup_pts.mp4")],
                         [m["frame_index"] for m in meta["frame_mapping"]])

    def test_exact_frames_with_seeks_on_duplicate_pts(self):
        n = len(self.pts)
        dups = [k for k in range(1, n) if self.pts[k] == self.pts[k - 1]]
        indices = [n - 25, n - 24, n - 23, n - 22, n - 21, 61, 25, n - 5, 13, 50, 3, 70, 71, 72, 40, 41, 0]
        indices += dups[-6:] + dups[:3]
        out = self.tmp / "dup_exact.mp4"
        with mock.patch.object(pr, "_SEQUENTIAL_READ_LIMIT", 2), contextlib.redirect_stdout(io.StringIO()):
            stats = pr._write_frames_by_index(self.mkv, indices, FPS, out, self.pts)
        self.assertGreater(stats["seeks"], 0)
        self.assertEqual([decode_index(f) for f in read_all(out)], indices)


@unittest.skipIf(FFMPEG is None, "ffmpeg not available")
class TestExactExtraction(unittest.TestCase):
    """Variable-frame-rate file: OpenCV time seeks must be corrected to exact frames."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.tmp = Path(tempfile.mkdtemp(prefix="pr_vfr_"))
        cls.mkv = cls.tmp / "camera_bravo.mkv"
        make_vfr_mkv(cls.mkv, 250)
        cls.pts = probe_pts(cls.mkv)

    @classmethod
    def tearDownClass(cls) -> None:
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_vfr_pts(self):
        self.assertEqual(len(self.pts), 250)
        self.assertGreater(self.pts[0], 1e9)
        gaps = [round(b - a, 3) for a, b in zip(self.pts, self.pts[1:])]
        self.assertIn(0.15, gaps)

    def test_exact_frames_with_seeks(self):
        # scattered, partly backwards indices force container seeks
        indices = [230, 231, 231, 232, 30, 31, 249, 120, 7, 8, 9, 200, 200, 0, 1, 180]
        out = self.tmp / "exact.mp4"
        with mock.patch.object(pr, "_SEQUENTIAL_READ_LIMIT", 5), contextlib.redirect_stdout(io.StringIO()):
            stats = pr._write_frames_by_index(self.mkv, indices, FPS, out, self.pts)
        self.assertGreater(stats["seeks"], 0)
        got = [decode_index(f) for f in read_all(out)]
        self.assertEqual(got, indices)

    def test_opencv_frame_seek_is_wrong_for_vfr(self):
        """Documents why the PTS-verified reader exists."""
        cap = cv2.VideoCapture(str(self.mkv))
        cap.set(cv2.CAP_PROP_POS_FRAMES, 230)
        ok, f = cap.read()
        cap.release()
        self.assertTrue(ok)
        self.assertNotEqual(decode_index(f), 230)

    def test_late_episode_alignment(self):
        """Episode starting late in a long session: wallclock match + exact extraction."""
        cam = self.tmp / "output_bravo" / "0"
        cam.mkdir(parents=True)
        shutil.copy2(self.mkv, cam / "camera_bravo.mkv")
        (cam / "camera_bravo_meta.json").write_text(json.dumps(
            {"recording_path": "camera_bravo.mkv", "start_epoch_seconds": EPOCH0, "fps": FPS, "width": W,
             "height": H, "video_encoder": "libx264", "wallclock_timestamps": True}), encoding="utf-8")
        actions = self.tmp / "out" / "20261023_120000_000004_Bravo_instance_000.json"
        times = [EPOCH0 + 7.0 + 0.05 * i + 0.02 for i in range(40)]
        write_actions(actions, times)
        cfg = pr.AlignmentInput(actions, cam / "camera_bravo_meta.json", self.tmp / "late.mp4",
                                self.tmp / "late_meta.json", "ffmpeg", 0.0, 0.0)
        with mock.patch.dict(os.environ, {"FFPROBE": ffprobe_path()}), \
                contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            meta = pr.align_recording(cfg)
        idx = [m["frame_index"] for m in meta["frame_mapping"]]
        for t, k in zip(times, idx):
            self.assertGreaterEqual(self.pts[k], t)
            self.assertLess(self.pts[k - 1], t)
        self.assertGreater(meta["diagnostics"]["duplicate_frame_count"], 0)  # dropped slots reuse frames
        self.assertEqual([decode_index(f) for f in read_all(self.tmp / "late.mp4")], idx)


class TestHelpers(unittest.TestCase):
    def test_repair_pairs_by_pose_seq(self):
        """Samples are moved to the neighbouring frame that rendered their movement packet."""
        # frames every 50 ms; frame k rendered packet seq[k] (None = server-followed)
        seqs = [0, 2, 1, 3, None, 5, 7]
        side = [{"pose_source": "injected" if s is not None else "server", "pose_seq": s} for s in seqs]
        times = [1000.0 + 50.0 * k for k in range(len(seqs))]
        # sample i at frame time - 10 ms with packet i -> time match = frame i
        actions = [{"epochTime": t - 10.0, "pose_seq": i} for i, t in enumerate(times)]
        actions[6] = {"epochTime": times[6] - 10.0}  # no pose link in the sample
        idx = list(range(len(seqs)))
        out, kinds, diag = pr._repair_pairs_by_pose_seq(actions, idx, side, times)
        self.assertEqual(out, [0, 2, 1, 3, 4, 5, 6])
        self.assertEqual(kinds, ["same", "next", "prev", "same", None, "same", None])
        self.assertEqual(diag["checked"], 5)
        self.assertEqual((diag["same"], diag["prev"], diag["next"], diag["none"]), (3, 1, 1, 0))
        self.assertEqual((diag["skipped_no_seq"], diag["skipped_not_injected"]), (1, 1))
        self.assertEqual(diag["same_packet_fraction_after"], 1.0)
        # no neighbour rendered the packet: time match kept
        out, kinds, diag = pr._repair_pairs_by_pose_seq([{"epochTime": 990.0, "pose_seq": 9}], [0], side, times)
        self.assertEqual((out, kinds, diag["none"]), ([0], ["none"], 1))
        # mapping carries the pair kind, the sample's packet and the server-followed look
        rec = dict(side[1], cam_yaw=1.0, cam_pitch=2.0, epoch_ms=times[1], player_yaw=3.0, player_pitch=4.0,
                   pose_fallback=None)
        m = pr._build_action_mapping_wallclock([{"epochTime": 1.0, "pose_seq": 1}], [0], [1.0], [rec],
                                               pair_kinds=["next"])
        self.assertEqual((m[0]["pose_pair"], m[0]["action_pose_seq"], m[0]["player_yaw"]), ("next", 1, 3.0))
        self.assertIn("pose_fallback", m[0])

    def test_resolve_recorded_path(self):
        tmp = Path(tempfile.mkdtemp(prefix="pr_path_"))
        try:
            (tmp / "camera_alpha.mkv").write_bytes(b"")
            for raw in ("/output/camera_alpha.mkv", r"C:\x\y\camera_alpha.mkv", "camera_alpha.mkv"):
                self.assertEqual(pr._resolve_recorded_path(raw, tmp), tmp / "camera_alpha.mkv")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_ffprobe_from_env(self):
        with mock.patch.dict(os.environ, {"FFPROBE": "X:/ff/ffprobe.exe"}):
            self.assertEqual(pr._ffprobe_executable(), "X:/ff/ffprobe.exe")

    def test_select_camera_meta_single_candidate(self):
        tmp = Path(tempfile.mkdtemp(prefix="pr_sel_"))
        try:
            meta = tmp / "camera_bravo_meta.json"
            meta.write_text("{}", encoding="utf-8")
            # one candidate: returned without reading the actions
            self.assertEqual(pr.select_camera_meta(meta, tmp / "missing.json"), meta)
            self.assertEqual(pr.select_camera_meta(tmp / "none" / "camera_bravo_meta.json", tmp / "x.json"),
                             tmp / "none" / "camera_bravo_meta.json")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_camera_params(self):
        p = pr._build_camera_params({"fps": 20, "width": 1280, "height": 720, "video_encoder": "h264_nvenc",
                                     "platform": "windows", "options": {"fov_deg": 70.0}})
        self.assertEqual(p, {"fov_deg": 70.0, "width": 1280, "height": 720, "fps": 20, "platform": "windows",
                             "encoder": "h264_nvenc", "options_recorded": True})

    def test_camera_params_from_camera_options_json(self):
        """Linux metas have no options: camera_options.json next to the meta (launch_minecraft.py)."""
        tmp = Path(tempfile.mkdtemp(prefix="pr_opts_"))
        try:
            meta = {"fps": 20, "width": 1280, "height": 720, "video_encoder": "libx264"}
            self.assertFalse(pr._build_camera_params(meta, tmp)["options_recorded"])
            (tmp / "camera_options.json").write_text(json.dumps(
                {"options_txt": {"fov": "0.0"}, "fov_deg": 70.0, "fov_effect_scale": 0.0, "width": 640,
                 "height": 360}), encoding="utf-8")
            p = pr._build_camera_params(meta, tmp)
            self.assertEqual(p, {"fov_deg": 70.0, "fov_effect_scale": 0.0, "width": 1280, "height": 720,
                                 "fps": 20, "encoder": "libx264", "options_recorded": True,
                                 "options_source": "camera_options.json"})
            # meta.options wins over the file
            p = pr._build_camera_params(dict(meta, options={"fov_deg": 80.0}), tmp)
            self.assertEqual(p["fov_deg"], 80.0)
            self.assertNotIn("options_source", p)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
