"""prepare_train_dataset.py copies the aligned camera metas, and the result verifies.

Run:  python -m unittest discover -s postprocess/tests -v
"""
from __future__ import annotations

import contextlib
import io
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import cv2
import numpy as np

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent))

import synth_batch  # noqa: E402
import verify_world_events as vwe  # noqa: E402


class TestPrepareDataset(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="prep_test_"))

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_camera_meta_copied_and_dataset_verifies(self):
        batch = self.tmp / "train" / "batch_0"
        info = synth_batch.make_batch(batch)
        for bot in synth_batch.BOTS:
            stem = f"{synth_batch.TS}_{synth_batch.EPISODE}_{bot}_instance_{synth_batch.INSTANCE}"
            w = cv2.VideoWriter(str(batch / "aligned" / f"{stem}_camera.mp4"), cv2.VideoWriter_fourcc(*"mp4v"),
                                20, (64, 36))
            for _ in info["frames"][bot]:
                w.write(np.zeros((36, 64, 3), np.uint8))
            w.release()
        dest = self.tmp / "dataset"
        res = subprocess.run([sys.executable, str(HERE.parent / "prepare_train_dataset.py"),
                              "--source-dir", str(self.tmp / "train"), "--destination-dir", str(dest)],
                             capture_output=True, text=True, timeout=120)
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        self.assertIn("Camera metas copied -> total: 2", res.stdout)
        names = sorted(p.name for p in dest.iterdir())
        self.assertIn("batch_0_000007_Alpha_instance_000.mp4", names)
        self.assertIn("batch_0_000007_Alpha_instance_000_camera_meta.json", names)
        self.assertIn("batch_0_000007_Bravo_instance_000_camera_meta.json", names)
        self.assertIn("batch_0_000007_world_instance_000_world_events.jsonl", names)
        report = self.tmp / "r.json"
        with contextlib.redirect_stdout(io.StringIO()):
            rc = vwe.main([str(dest), "--report", str(report)])
        ep = json.loads(report.read_text(encoding="utf-8"))["episodes"][0]
        self.assertEqual(rc, 0)
        self.assertEqual(ep["status"], "OK", ep["warnings"])
        self.assertEqual(ep["checks"]["t12"]["rendered"]["Alpha"]["source"], "aligned_meta.frame_mapping")


if __name__ == "__main__":
    unittest.main()
