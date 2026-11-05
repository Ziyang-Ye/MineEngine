"""Unit tests for postprocess/verify_world_events.py on synthetic batches.

Run:  python -m unittest discover -s postprocess/tests -v
"""
from __future__ import annotations

import contextlib
import io
import json
import math
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent))

import synth_batch  # noqa: E402
import verify_world_events as vwe  # noqa: E402


class VerifyCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="vwe_test_"))

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def run_verify(self, *extra: str, **overrides):
        batch = self.tmp / "batch_0"
        shutil.rmtree(batch, ignore_errors=True)  # no files left from an earlier call
        synth_batch.make_batch(batch, **overrides)
        report = self.tmp / "world_events_report.json"
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = vwe.main([str(batch), "--report", str(report), *extra])
        doc = json.loads(report.read_text(encoding="utf-8"))
        ep = doc["episodes"][0]
        return rc, doc, ep, out.getvalue()

    def assertWarns(self, ep, needle: str) -> None:  # noqa: N802 - unittest style
        self.assertTrue(any(needle in w for w in ep["warnings"]), f"no warning with {needle!r}: {ep['warnings']}")

    def assertFails(self, ep, needle: str) -> None:  # noqa: N802
        self.assertEqual(ep["status"], "FAIL")
        self.assertTrue(any(needle in e for e in ep["errors"]), f"no error with {needle!r}: {ep['errors']}")


class TestCleanBatch(VerifyCase):
    def test_all_checks_ok(self):
        rc, doc, ep, out = self.run_verify()
        self.assertEqual(rc, 0)
        self.assertEqual(ep["status"], "OK", (ep["errors"], ep["warnings"]))
        self.assertIn("[OK] episode 000007 instance 000 type=pvp", out)
        self.assertEqual(doc["format"], vwe.REPORT_FORMAT)
        ck = ep["checks"]
        self.assertEqual(ep["replay_mismatches"], 0)
        self.assertEqual(ep["replay_changed_voxels"], 2)
        self.assertEqual(ck["ticks"]["missing_ticks"], 0)
        self.assertEqual(ck["entities"]["missing_ticks"], 0)
        self.assertEqual(ck["entities"]["kinds"], {"player": 240})
        self.assertEqual(ck["time_weather"]["Alpha"]["weather"], "clear")
        self.assertEqual(ck["camera_params"]["Alpha"]["fov_deg"], 70.0)
        self.assertEqual(ck["camera_params"]["Alpha"]["source"], "aligned_meta.camera_params")
        # the synthetic client renders the previous sample: zero error at 50 ms lag
        rv = ck["t12"]["rendered"]["Alpha"]
        self.assertEqual(rv["best_lag_ms"], 50)
        self.assertLess(rv["best_lag"]["combined"]["p99"], 0.001)
        self.assertGreater(rv["lag0"]["combined"]["p99"], 0.2)
        srv = ck["t12"]["server"]["Bravo"]
        self.assertEqual(srv["basis"], "follow")
        self.assertAlmostEqual(srv["combined"]["p99"], 0.01, places=6)
        self.assertAlmostEqual(srv["follow"]["combined"]["p99"], 0.01, places=6)
        self.assertEqual(srv["follow"]["pos"]["max"], 0.0)
        self.assertAlmostEqual(srv["lag_compensated"]["combined"]["p99"], 0.01, places=6)
        # the bots turn 3 deg per tick: dyaw_to_target is that one tick of input, not an error
        self.assertAlmostEqual(srv["one_tick_input"]["combined"]["p50"], 2.99, places=6)
        self.assertAlmostEqual(doc["summary"]["t12_deg"]["server_one_tick_input_combined"]["p50"], 2.99,
                               places=6)
        self.assertEqual(ck["visibility"]["Alpha"]["fraction"], 1.0)
        self.assertEqual(ck["swing_before_damage"]["fraction"], 1.0)
        self.assertEqual(ck["dig_before_break"]["fraction"], 1.0)
        self.assertEqual(ck["dig_before_break"]["with_increasing_progress"], 1)
        s = doc["summary"]
        self.assertEqual(s["status_counts"], {"OK": 1, "WARN": 0, "FAIL": 0})
        self.assertEqual(s["episode_types"], {"pvp": 1})
        self.assertEqual(s["encountered_error"]["ratio"], 0.0)
        self.assertEqual(s["t12_deg"]["rendered_best_lag_ms_median"], 50)
        self.assertEqual(s["visibility"]["all"]["fraction"], 1.0)
        # private pooled lists never reach the JSON
        self.assertFalse([k for k in ep if k.startswith("_")])

    def test_output_dir_and_alignment_dir(self):
        batch = self.tmp / "batch_0"
        synth_batch.make_batch(batch)
        align = self.tmp / "align"
        with contextlib.redirect_stdout(io.StringIO()):
            rc = vwe.main([str(batch / "output"), "--alignment-dir", str(align)])
        self.assertEqual(rc, 0)
        files = list(align.glob("*_event_frame_alignment.json"))
        self.assertEqual(len(files), 1)
        al = json.loads(files[0].read_text(encoding="utf-8"))
        self.assertEqual(set(al["bots"]), {"Alpha", "Bravo"})

    def test_dataset_dir_with_copied_camera_meta(self):
        """A prepared dataset is flat: camera metas sit next to the action JSONs."""
        batch = self.tmp / "batch_0"
        synth_batch.make_batch(batch)
        ds = self.tmp / "dataset"
        ds.mkdir()
        for p in list((batch / "output").iterdir()) + list((batch / "aligned").iterdir()):
            shutil.copy2(p, ds / ("batch_0_" + p.name[len("20261023_120000_"):]))
        with contextlib.redirect_stdout(io.StringIO()):
            rc = vwe.main([str(ds), "--report", str(self.tmp / "r.json")])
        ep = json.loads((self.tmp / "r.json").read_text(encoding="utf-8"))["episodes"][0]
        self.assertEqual(rc, 0, ep["warnings"])
        self.assertEqual(ep["checks"]["camera_params"]["Bravo"]["fov_effect_scale"], 0.0)
        self.assertEqual(ep["checks"]["t12"]["rendered"]["Bravo"]["best_lag_ms"], 50)

    def test_no_episodes(self):
        (self.tmp / "empty").mkdir()
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(vwe.main([str(self.tmp / "empty")]), 2)


class TestDefects(VerifyCase):
    def test_null_yaw_fails(self):
        rc, doc, ep, out = self.run_verify(null_yaw={"Bravo": 3})
        self.assertEqual(rc, 1)
        self.assertFails(ep, "Bravo: some frames lack yaw/pitch")
        self.assertEqual(ep["checks"]["yaw_pitch"]["Bravo"]["missing"], 3)
        self.assertEqual(ep["checks"]["yaw_pitch"]["Bravo"]["first_missing_indices"], [0, 1, 2])
        self.assertEqual(doc["summary"]["status_counts"]["FAIL"], 1)
        self.assertIn("[FAIL]", out)

    def test_entities_absent_fails(self):
        rc, _, ep, _ = self.run_verify(no_entities=True)
        self.assertEqual(rc, 1)
        self.assertFails(ep, "no entities records although episode_begin.entities_every=1")

    def test_entities_absent_without_announcement_warns(self):
        rc, _, ep, _ = self.run_verify(entities_every=0)
        self.assertEqual(rc, 0)
        self.assertEqual(ep["status"], "WARN")
        self.assertWarns(ep, "no entities records")

    def test_entities_gaps_warn(self):
        rc, _, ep, _ = self.run_verify(drop_entities=[10, 11, 12, 40])
        self.assertEqual(rc, 0)
        self.assertEqual(ep["checks"]["entities"]["missing_ticks"], 4)
        self.assertEqual(ep["checks"]["entities"]["gaps"], 2)
        self.assertEqual(ep["checks"]["entities"]["gap_examples"][0], [synth_batch.GT0 + 9, synth_batch.GT0 + 13])
        self.assertWarns(ep, "entities records: 4 missing ticks")

    def test_entities_every_two(self):
        rc, _, ep, _ = self.run_verify(entities_every=2)
        self.assertEqual(ep["checks"]["entities"]["missing_ticks"], 0)
        self.assertEqual(ep["status"], "OK", ep["warnings"])

    def test_tick_gap_warns(self):
        _, _, ep, _ = self.run_verify(drop_ticks=[70, 71])
        self.assertEqual(ep["checks"]["ticks"]["missing_ticks"], 2)
        self.assertWarns(ep, "tick records: 2 missing ticks")

    def test_time_weather_missing_warns(self):
        _, _, ep, _ = self.run_verify(drop_state_metadata=["weather"])
        self.assertWarns(ep, "state_metadata lacks weather")

    def test_fov_effect_scale_fails(self):
        rc, _, ep, _ = self.run_verify(fov_effect_scale=1.0)
        self.assertEqual(rc, 1)
        self.assertFails(ep, "fov_effect_scale=1.0")

    def test_fov_fails_and_is_configurable(self):
        _, _, ep, _ = self.run_verify(fov_deg=80.0)
        self.assertFails(ep, "fov_deg=80.0, expected 70")
        rc, _, ep, _ = self.run_verify("--expected-fov", "80", fov_deg=80.0)
        self.assertFalse(any("fov_deg" in e for e in ep["errors"]))

    def test_linux_camera_degrades(self):
        """Old recordings: no options, no sidecar -> WARN, never crash."""
        rc, _, ep, _ = self.run_verify(linux_camera=True)
        self.assertEqual(rc, 0)
        self.assertEqual(ep["status"], "WARN")
        self.assertWarns(ep, "camera parameters not recorded")
        self.assertEqual(ep["checks"]["t12"]["rendered"]["Alpha"]["frames"], 0)

    def test_raw_sidecar_fallback(self):
        """Without aligned metas, the batch camera meta + frames sidecar are used."""
        _, _, ep, _ = self.run_verify(no_aligned=True)
        self.assertEqual(ep["status"], "OK", ep["warnings"])
        self.assertEqual(ep["checks"]["camera_params"]["Alpha"]["source"], "camera_meta.options")
        rv = ep["checks"]["t12"]["rendered"]["Alpha"]
        self.assertEqual(rv["source"], "camera_frames.jsonl")
        self.assertEqual(rv["best_lag_ms"], 50)

    def test_linux_camera_options_json(self):
        """Linux recordings: no meta.options, but camera_options.json next to the meta."""
        _, _, ep, _ = self.run_verify(linux_camera=True, camera_options_json=True)
        cp = ep["checks"]["camera_params"]["Alpha"]
        self.assertEqual(cp["source"], "camera_options.json")
        self.assertEqual(cp["fov_deg"], 70.0)
        self.assertEqual(cp["fov_effect_scale"], 0.0)
        self.assertFalse(any("camera parameters not recorded" in w for w in ep["warnings"]), ep["warnings"])
        _, _, ep, _ = self.run_verify(linux_camera=True, camera_options_json=True, fov_effect_scale=1.0)
        self.assertFails(ep, "fov_effect_scale=1.0")

    def _check_pose_link(self, ep, source):
        rv = ep["checks"]["t12"]["rendered"]["Alpha"]
        self.assertEqual(rv["source"], source)
        pl = rv["pose_link"]
        self.assertEqual(pl["frames"], {"injected": 99, "server": 11})
        self.assertEqual(pl["injected_fraction"], 0.9)
        self.assertEqual(pl["pose_age_ms"]["p50"], 25.0)
        inj, srv = pl["by_source"]["injected"], pl["by_source"]["server"]
        self.assertEqual(inj["best_lag_ms"], 0)
        self.assertLess(inj["best_lag"]["combined"]["p99"], 0.001)
        self.assertEqual(srv["best_lag_ms"], 50)
        # every action sample lies in an injected frame's packet window: exact match
        self.assertEqual(pl["pose_matched"]["samples"], 99)
        self.assertLess(pl["pose_matched"]["combined"]["max"], 0.001)
        self.assertEqual(rv["threshold_basis"], "injected frames vs their action labels (paired)")
        self.assertEqual(pl["server_frame_reasons"], {"stale": 11})
        # packet n+1 was always sent after the frame showing packet n was set up
        self.assertEqual(pl["outdated"]["outdated"], 0)
        self.assertEqual(pl["outdated"]["checked"], 88)
        return rv

    def test_pose_link_split_by_source(self):
        """Injected frames render the bot's current pose; the T12 threshold applies to them only."""
        rc, doc, ep, out = self.run_verify("--t12-min-injected", "0.85", pose_link=True)
        self.assertEqual(ep["status"], "OK", (ep["errors"], ep["warnings"]))
        pd = ep["checks"]["t12"]["rendered"]["Alpha"]["paired_detail"]
        self.assertEqual(pd["by_source"]["injected"]["samples"], 99)
        self.assertLess(pd["by_source"]["injected"]["combined"]["max"], 0.001)
        self.assertEqual(pd["by_source"]["injected"]["frac_over_threshold"], 0.0)
        # same frames: the synthetic server-followed look equals the rendered one
        self.assertEqual(pd["injected_vs_server_followed_same_frames"]["samples"], 99)
        rv = self._check_pose_link(ep, "aligned_meta.frame_mapping")
        # all frames mixed: the 10 % server frames lag 50 ms, so no lag fits everything
        self.assertGreater(rv["best_lag"]["combined"]["p99"], 0.2)
        t12 = doc["summary"]["t12_deg"]
        self.assertLess(t12["rendered_paired_injected_combined"]["p99"], 0.001)
        self.assertEqual(t12["paired_injected_frac_over_threshold"], 0.0)
        self.assertEqual(t12["pose_source_frames"], {"injected": 198, "server": 22})
        self.assertEqual(t12["injected_fraction"], 0.9)
        self.assertLess(t12["rendered_injected_best_lag_combined"]["p99"], 0.001)
        self.assertLess(t12["rendered_pose_matched_combined"]["max"], 0.001)
        self.assertEqual(t12["pose_age_ms"]["p50"], 25.0)
        self.assertIn("injected=0.90", out)

    def test_pose_link_from_sidecar(self):
        """Raw sidecar only, and aligned metas written before the pose fields were copied."""
        _, _, ep, _ = self.run_verify("--t12-min-injected", "0.85", pose_link=True, no_aligned=True)
        self.assertEqual(ep["status"], "OK", (ep["errors"], ep["warnings"]))
        rv = self._check_pose_link(ep, "camera_frames.jsonl")
        self.assertTrue(rv["paired_detail"]["simulated"])
        self.assertEqual(rv["paired_detail"]["pose_pair"], {"same": 99})
        _, _, ep, _ = self.run_verify("--t12-min-injected", "0.85", pose_link="old_aligned")
        self.assertEqual(ep["status"], "OK", (ep["errors"], ep["warnings"]))
        self._check_pose_link(ep, "aligned_meta.frame_mapping")

    def test_pose_link_min_injected_fraction(self):
        """A pose link that drives only 90 % of the frames is flagged, with the fallback reasons."""
        _, _, ep, _ = self.run_verify(pose_link=True)
        self.assertWarns(ep, "T12 rendered: Alpha only 90.0% of the frames in the episode span used the "
                             "pose link (< 95%); server-frame reasons: {'stale': 11}")

    def test_pose_link_frame_one_packet_ahead(self):
        """Frames that show the next packet: invisible to pose-matched, caught by the paired gate."""
        _, doc, ep, _ = self.run_verify("--t12-min-injected", "0.85", pose_link=True, pose_link_swap=True)
        rv = ep["checks"]["t12"]["rendered"]["Alpha"]
        # camera fidelity is still exact (every frame shows a packet the bot really sent)
        self.assertFalse(any("camera fidelity" in w for w in ep["warnings"]), ep["warnings"])
        self.assertWarns(ep, "T12 rendered: Alpha injected frames vs their action labels (paired) p99")
        self.assertGreater(rv["paired_detail"]["by_source"]["injected"]["frac_over_threshold"], 0.1)
        # raw sidecar only: the verifier pairs like process_recordings.py, by pose_seq
        _, doc, ep, _ = self.run_verify("--t12-min-injected", "0.85", pose_link=True, pose_link_swap=True,
                                        no_aligned=True)
        rv = ep["checks"]["t12"]["rendered"]["Alpha"]
        self.assertEqual(ep["status"], "OK", (ep["errors"], ep["warnings"]))
        pd = rv["paired_detail"]
        self.assertLess(pd["by_source"]["injected"]["combined"]["max"], 0.001)
        self.assertEqual(set(pd["pose_pair"]), {"same", "prev", "next"})
        self.assertEqual(pd["pose_pair"]["prev"], pd["pose_pair"]["next"])
        self.assertEqual(doc["summary"]["t12_deg"]["pose_pair"]["next"], 2 * pd["pose_pair"]["next"])

    def test_pose_link_injected_noise_warns(self):
        _, _, ep, _ = self.run_verify(pose_link=True, injected_noise_deg=1.0)
        self.assertWarns(ep, "T12 rendered: Alpha camera fidelity (pose-matched) p99 angle error 1.000")
        self.assertWarns(ep, "T12 rendered: Alpha injected frames vs their action labels (paired) p99 angle "
                             "error 1.000")
        pm = ep["checks"]["t12"]["rendered"]["Alpha"]["pose_link"]["pose_matched"]
        self.assertAlmostEqual(pm["combined"]["max"], 1.0, places=3)

    def test_render_noise_warns(self):
        _, _, ep, _ = self.run_verify(render_noise_deg=1.0)
        self.assertWarns(ep, "T12 rendered: Alpha camera vs bot (best lag")
        self.assertGreaterEqual(ep["checks"]["t12"]["rendered"]["Alpha"]["best_lag"]["combined"]["max"], 0.99)

    def test_server_camera_error_warns(self):
        _, doc, ep, _ = self.run_verify(server_cam_error=0.5)
        self.assertWarns(ep, "T12 server: camera of Alpha p99 follow angle error 0.500")
        self.assertAlmostEqual(doc["summary"]["t12_deg"]["server_combined"]["p99"], 0.5)

    def test_server_camera_without_since_sync_uses_lag_compensation(self):
        """Older plugin logs (no *_since_sync): camera vs the target's pose one tick earlier."""
        _, _, ep, _ = self.run_verify(no_since_sync=True)
        srv = ep["checks"]["t12"]["server"]["Alpha"]
        self.assertEqual(srv["basis"], "lag_compensated")
        self.assertIsNone(srv["follow"])
        self.assertAlmostEqual(srv["combined"]["p99"], 0.01, places=6)
        self.assertEqual(ep["status"], "OK", ep["warnings"])
        _, _, ep, _ = self.run_verify(no_since_sync=True, server_cam_error=0.5)
        self.assertWarns(ep, "T12 server: camera of Alpha p99 lag-compensated angle error 0.500")

    def test_restarted_camera_meta_is_used(self):
        """Orchestrator restart: camera_bravo_r1_meta.json covers the episode, the first meta does not."""
        _, _, ep, _ = self.run_verify(camera_restart=True, no_aligned=True)
        self.assertEqual(ep["status"], "OK", (ep["errors"], ep["warnings"]))
        cp = ep["checks"]["camera_params"]["Bravo"]
        self.assertEqual(cp["camera_meta"], "camera_bravo_r1_meta.json")
        self.assertEqual(cp["fov_deg"], 70.0)
        rv = ep["checks"]["t12"]["rendered"]["Bravo"]
        self.assertEqual(rv["source"], "camera_frames.jsonl")
        self.assertEqual(rv["best_lag_ms"], 50)

    def test_t12_threshold_configurable(self):
        _, _, ep, _ = self.run_verify("--t12-max-p99", "0.6", server_cam_error=0.5)
        self.assertFalse(any("T12" in w for w in ep["warnings"]))

    def test_visibility_warns(self):
        _, doc, ep, _ = self.run_verify(alpha_faces_away=True)
        self.assertEqual(ep["checks"]["visibility"]["Alpha"]["fraction"], 0.0)
        self.assertWarns(ep, "Alpha: Bravo inside the view frustum in 0% of frames")
        self.assertEqual(doc["summary"]["visibility"]["all"]["fraction"], 0.0)

    def test_damage_episode_without_damage_warns(self):
        _, _, ep, _ = self.run_verify(no_damage=True)
        self.assertWarns(ep, "damage episode (pvp) without entity_damage")

    def test_non_damage_episode_ok_without_damage(self):
        _, _, ep, _ = self.run_verify(no_damage=True, episode_type="buildAndBreak",
                                      drop_state_metadata=["damage_enabled"])
        self.assertFalse(any("damage episode" in w for w in ep["warnings"]))

    def test_hit_without_swing_warns(self):
        _, _, ep, _ = self.run_verify(no_swing=True)
        self.assertEqual(ep["checks"]["swing_before_damage"]["fraction"], 0.0)
        self.assertWarns(ep, "only 0/1 melee player hits have an attacker arm_swing within [-500, +100] ms")

    def test_frame_fields_missing_warns(self):
        _, _, ep, _ = self.run_verify(no_state=["Alpha"])
        self.assertEqual(ep["checks"]["frame_fields"]["Alpha"]["missing"], {"state": 110})
        self.assertWarns(ep, "Alpha: per-frame state/peer fields missing (state:110)")

    def test_break_without_dig_warns_and_instant_excluded(self):
        _, _, ep, _ = self.run_verify(no_dig=True, torch_break=True)
        dig = ep["checks"]["dig_before_break"]
        self.assertEqual(dig["player_breaks"], 2)
        self.assertEqual(dig["excluded"], {"instant-break block": 1})
        self.assertEqual(dig["checked"], 1)
        self.assertEqual(dig["fraction"], 0.0)
        self.assertEqual(dig["examples_without_dig"][0]["pos"], synth_batch.DIG_POS)
        self.assertWarns(ep, "only 0/1 player block_breaks")

    def test_encountered_error_ratio(self):
        _, doc, _, _ = self.run_verify(encountered_error=True)
        enc = doc["summary"]["encountered_error"]
        self.assertEqual((enc["true"], enc["episode_info_files"], enc["ratio"]), (1, 2, 0.5))
        self.assertEqual(enc["per_bot"]["Bravo"]["ratio"], 1.0)

    def test_bad_line_fails(self):
        batch = self.tmp / "batch_0"
        synth_batch.make_batch(batch)
        log = next((batch / "output").glob("*_world_events.jsonl"))
        with log.open("a", encoding="utf-8") as fh:
            fh.write("{not json\n")
        with contextlib.redirect_stdout(io.StringIO()):
            rc = vwe.main([str(batch), "--report", str(self.tmp / "r.json")])
        ep = json.loads((self.tmp / "r.json").read_text(encoding="utf-8"))["episodes"][0]
        self.assertEqual(rc, 1)
        self.assertFails(ep, "1 unparseable lines")


class TestHelpers(unittest.TestCase):
    def test_angle_conversion(self):
        self.assertAlmostEqual(vwe.mf_yaw_to_mc_deg(0.0), -180.0)       # north
        self.assertAlmostEqual(vwe.mf_yaw_to_mc_deg(math.pi), 0.0)      # south
        self.assertAlmostEqual(vwe.mf_yaw_to_mc_deg(math.pi / 2), 90.0)  # west
        self.assertAlmostEqual(vwe.mf_pitch_to_mc_deg(math.radians(30)), -30.0)
        self.assertAlmostEqual(vwe.wrap180(359.5), -0.5)

    def test_frustum(self):
        th, tv = math.tan(math.radians(35)) * 16 / 9, math.tan(math.radians(35))
        eye = (0.0, 65.62, 0.0)
        self.assertTrue(vwe.in_view(eye, 0.0, 0.0, (0, 64.9, -10), th, tv, 64)[0])      # north, ahead
        self.assertFalse(vwe.in_view(eye, math.pi, 0.0, (0, 64.9, -10), th, tv, 64)[0])  # facing south
        self.assertTrue(vwe.in_view(eye, math.pi / 2, 0.0, (-10, 65, 0), th, tv, 64)[0])  # west
        self.assertFalse(vwe.in_view(eye, 0.0, 0.0, (0, 64.9, -70), th, tv, 64)[0])     # too far
        # just inside / outside the horizontal half-angle (~51.3 deg at 16:9)
        self.assertTrue(vwe.in_view(eye, 0.0, 0.0, (-math.tan(math.radians(50)) * 10, 65.62, -10), th, tv, 64)[0])
        self.assertFalse(vwe.in_view(eye, 0.0, 0.0, (-math.tan(math.radians(53)) * 10, 65.62, -10), th, tv, 64)[0])

    def test_continuity(self):
        c = vwe.continuity([1, 2, 3, 6, 7, 7, 10], 1)
        self.assertEqual((c["missing_ticks"], c["gaps"], c["max_gap"]), (4, 2, 3))
        c = vwe.continuity([0, 2, 4, 8], 2)
        self.assertEqual(c["missing_ticks"], 1)

    def test_percentiles(self):
        p = vwe.percentiles([float(i) for i in range(1, 101)])
        self.assertEqual((p["p50"], p["p90"], p["p99"], p["max"]), (50.0, 90.0, 99.0, 100.0))
        self.assertIsNone(vwe.percentiles([]))

    def test_angle_track_unwraps(self):
        frames = [{"epochTime": t, "yaw": y, "pitch": 0.0}
                  for t, y in ((0.0, 0.01), (0.05, 2 * math.pi - 0.01))]  # crosses north
        tr = vwe.AngleTrack(frames)
        (yaw, _), = tr.sample_sorted([25.0])
        self.assertAlmostEqual(vwe.wrap180(yaw), -180.0, places=6)


if __name__ == "__main__":
    unittest.main()
