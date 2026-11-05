"""Synthetic batch directories for the postprocess unit tests.

`make_batch(root, **overrides)` writes a tiny but complete batch in the layout
of OUTPUT_SCHEMA.md / windows/CONTRACT.md:

  root/output/   world log (every record type), start/end snapshots, two
                 action JSONs, two episode_info files
  root/camera/output_<bot>/0/   camera meta (with options) + frames sidecar
  root/aligned/  <action stem>_camera_meta.json with camera_params and the
                 rendered pose in frame_mapping (what process_recordings writes)

Alpha stands at (0, 64, 0) looking north at Bravo (0, 64, -10) and sweeps its
yaw by a few degrees; Bravo looks back at Alpha. The camera renders each bot's
pose exactly one sample (50 ms) late, so the rendered-vs-bot error is 0 at a
50 ms lag. Alpha hits Bravo once (arm_swing right before entity_damage) and
digs + breaks one stone block (dig_start / dig_progress 0..9 / block_break).
"""
from __future__ import annotations

import gzip
import json
import math
from pathlib import Path
from typing import Any, Dict, List, Optional

T0_MS = 1761200000000.0   # epoch ms of the first tick
GT0 = 100000              # game_time of the first tick
EPISODE = "000007"
INSTANCE = "000"
TS = "20261023_120000"
STEM = f"{TS}_{EPISODE}_world_instance_{INSTANCE}"
BOTS = ("Alpha", "Bravo")
ORIGIN = [-2, 63, -2]
SIZE = [4, 3, 4]
DIG_POS = [0, 63, -1]


def mc_yaw_from_mf(yaw: float) -> float:
    return (math.degrees(math.pi - yaw) + 180.0) % 360.0 - 180.0


def mf_yaw(t_idx: int, bot: str) -> float:
    """Mineflayer yaw of a bot at sample t_idx: Alpha sweeps around north (0), Bravo faces south (pi)."""
    base = 0.0 if bot == "Alpha" else math.pi
    return (base + math.radians(5.0) * math.sin(t_idx / 10.0)) % (2 * math.pi)


def tick_yaw(i: int, bot: str) -> float:
    """Server-side (MC degrees) yaw of a bot in tick i: both bots keep turning 3 deg per tick."""
    base = 0.0 if bot == "Alpha" else 180.0
    return (base + 3.0 * i + 180.0) % 360.0 - 180.0


def mf_pitch(t_idx: int) -> float:
    return math.radians(2.0) * math.cos(t_idx / 13.0)


def rle(seq: List[int]) -> List[int]:
    out: List[int] = []
    for v in seq:
        if out and out[-1] == v:
            out[-2] += 1
        else:
            out += [1, v]
    return out


def snapshot(which: str, overrides: Dict[tuple, str]) -> Dict[str, Any]:
    palette = ["minecraft:air", "minecraft:stone", "minecraft:oak_planks"]
    sx, sy, sz = SIZE
    dense = []
    for y in range(ORIGIN[1], ORIGIN[1] + sy):
        for z in range(ORIGIN[2], ORIGIN[2] + sz):
            for x in range(ORIGIN[0], ORIGIN[0] + sx):
                state = overrides.get((x, y, z), "minecraft:stone" if y == 63 else "minecraft:air")
                dense.append(palette.index(state))
    return {"format": "solaris-block-snapshot-v1", "which": which, "origin": ORIGIN, "size": SIZE,
            "order": "yzx", "palette": palette, "rle": rle(dense), "biome_y": 64,
            "biome_palette": ["minecraft:plains"], "biomes_rle": [sx * sz, 0], "entities": []}


def write_json(path: Path, obj: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        json.dump(obj, fh)


def make_batch(root: Path, n: int = 120, **o: Any) -> Dict[str, Any]:
    """Write the batch; keyword overrides switch individual defects on (see tests)."""
    out = root / "output"
    out.mkdir(parents=True, exist_ok=True)
    entities_every = o.get("entities_every", 1)
    drop_ticks = set(o.get("drop_ticks", ()))            # tick indices without a tick record
    drop_entities = set(o.get("drop_entities", ()))      # tick indices without an entities record
    episode_type = o.get("episode_type", "pvp")

    recs: List[Dict[str, Any]] = []

    def rec(type_: str, i: float, **fields: Any) -> None:
        fields.update({"type": type_, "game_time": GT0 + int(i), "server_tick": 5000 + int(i),
                       "epoch_ms": T0_MS + i * 50.0})
        recs.append(fields)

    rec("episode_begin", 0, format="solaris-world-events-v1", stem=STEM, episode_id=int(EPISODE),
        instance_id=0, episode_type=episode_type, world={"seed": 1, "name": "world"},
        box={"min": ORIGIN, "max": [ORIGIN[i] + SIZE[i] - 1 for i in range(3)]}, center=[0, 64, 0],
        log_radius=64, heartbeat_ticks=1, tracked_players=list(BOTS),
        entities_every=entities_every, entities_radius=64)
    end_overrides: Dict[tuple, str] = {}
    for i in range(n):
        if i not in drop_ticks:
            # server-side camera follow error; the camera holds its target's pose of the
            # previous tick (synced at the end of that tick), so dyaw_to_target is the
            # 3 deg the target turned in one tick, not a camera error
            cam_err = o.get("server_cam_error", 0.01) * (1 if i % 2 else -1)
            cams = []
            for b in BOTS:
                c = {"name": "Camera" + b, "target": b, "pos": [0, 65.62, 0],
                     "yaw": tick_yaw(max(0, i - 1), b) + cam_err, "pitch": 0.0,
                     "flying": True, "gamemode": "SPECTATOR"}
                c["dyaw_to_target"] = (c["yaw"] - tick_yaw(i, b) + 180.0) % 360.0 - 180.0
                c["dpitch_to_target"] = 0.0
                if not o.get("no_since_sync"):
                    c.update({"last_sync_tick": 5000 + i - 1, "dyaw_since_sync": cam_err,
                              "dpitch_since_sync": 0.0, "dpos_since_sync": 0.0})
                cams.append(c)
            rec("tick", i, full_time=6000 + i, time_of_day=6000 + i, players=[
                {"name": b, "pos": [0, 64, 0 if b == "Alpha" else -10], "yaw": tick_yaw(i, b), "pitch": 0.0,
                 "head_yaw": 0.0, "body_yaw": 0.0, "food": 20, "saturation": 5.0, "dead": False,
                 "gamemode": "survival", "blocking": False, "hand_raised": False, "health": 20.0}
                for b in BOTS],
                cameras=cams)
        if entities_every and i % entities_every == 0 and i not in drop_entities and not o.get("no_entities"):
            rec("entities", i, center=[0, 64, 0], radius=64, entities=[
                {"uuid": f"u-{b}", "kind": "player", "type": "minecraft:player", "name": b,
                 "x": 0.0, "y": 64.0, "z": 0.0 if b == "Alpha" else -10.0, "yaw": 0.0, "pitch": 0.0,
                 "head_yaw": 0.0, "body_yaw": 0.0, "vx": 0, "vy": 0, "vz": 0, "bbox_w": 0.6, "bbox_h": 1.8,
                 "health": 20.0, "max_health": 20.0, "alive": True, "on_ground": True, "sneaking": False,
                 "sprinting": False, "blocking": False, "hand_raised": False, "held_item": None,
                 "offhand_item": None, "fuse_ticks": None, "item": None, "block_state": None,
                 "shooter": None, "fire_ticks": 0} for b in BOTS])
        if i == 20:
            # place a plank on top of the stone floor
            rec("block_place", i + 0.1, pos=[1, 64, 1], player="Alpha", item="minecraft:oak_planks",
                against=[1, 63, 1], old_type="minecraft:air", old_state="minecraft:air",
                new_type="minecraft:oak_planks", new_state="minecraft:oak_planks", multi=False)
            rec("arm_swing", i + 0.1, player="Alpha", animation="ARM_SWING")
        if i == 21:
            rec("block_state", i, pos=[1, 64, 1], block="minecraft:oak_planks", state="minecraft:oak_planks",
                prev_state="minecraft:air")
            end_overrides[(1, 64, 1)] = "minecraft:oak_planks"
        if i == 30 and not o.get("no_dig"):
            rec("dig_start", i, player="Alpha", pos=DIG_POS, face="up", block="minecraft:stone")
            for st in range(10):
                rec("dig_progress", i + st * 0.1, player="Alpha", pos=DIG_POS, stage=st)
        if i == 32:
            if not o.get("no_dig"):
                rec("dig_finish", i, player="Alpha", pos=DIG_POS)
            rec("block_break", i + 0.1, pos=DIG_POS, player="Alpha", tool="minecraft:stone_pickaxe",
                drop_items=True, old_type="minecraft:stone", old_state="minecraft:stone",
                new_type=None, new_state=None)
            if o.get("torch_break"):
                rec("block_break", i + 0.2, pos=[-1, 64, -1], player="Bravo", tool=None, drop_items=True,
                    old_type="minecraft:torch", old_state="minecraft:torch", new_type=None, new_state=None)
        if i == 33:
            rec("block_state", i, pos=DIG_POS, block="minecraft:air", state="minecraft:air",
                prev_state="minecraft:stone")
            end_overrides[tuple(DIG_POS)] = "minecraft:air"
        if i == 50:
            rec("held_item_change", i, player="Bravo", previous_slot=0, new_slot=1,
                previous_item="minecraft:stone_sword", new_item="minecraft:bow")
            rec("use_item_start", i, player="Bravo", item="minecraft:bow", hand="HAND", ticks_used=0)
        if i == 55:
            rec("use_item_stop", i, player="Bravo", item="minecraft:bow", hand="HAND", ticks_used=5)
            rec("bow_shoot", i, player="Bravo", force=1.0)
            rec("item_consume", i, player="Bravo", item="minecraft:bread")
        if i == 60 and not o.get("no_damage"):
            if not o.get("no_swing"):
                rec("arm_swing", i - 2, player="Alpha", animation="ARM_SWING")
            rec("entity_damage", i, victim={"uuid": "u-Bravo", "type": "minecraft:player", "name": "Bravo"},
                cause="ENTITY_ATTACK", damage=4.0, final_damage=4.0, health_before=20.0, health_after=16.0,
                attacker={"uuid": "u-Alpha", "type": "minecraft:player", "name": "Alpha"}, projectile=None)
            rec("entity_knockback", i, entity={"uuid": "u-Bravo", "name": "Bravo"},
                attacker={"uuid": "u-Alpha", "name": "Alpha"}, vector=[0.0, 0.36, -0.4])
    rec("episode_end", n - 1, stem=STEM, episode_id=int(EPISODE), reason="command", events_logged=len(recs) + 1)
    recs.sort(key=lambda r: (r["epoch_ms"], r["type"] != "episode_begin", r["type"] == "episode_end"))
    for k, r in enumerate(recs):
        r["seq"] = k
    with (out / f"{STEM}_world_events.jsonl").open("w", encoding="utf-8") as fh:
        for r in recs:
            fh.write(json.dumps(r, separators=(",", ":")) + "\n")
    for which, ov in (("start", {}), ("end", end_overrides)):
        with gzip.open(out / f"{STEM}_world_snapshot_{which}.json.gz", "wt", encoding="utf-8") as fh:
            json.dump(snapshot(which, ov), fh)

    # ---- per-frame actions, episode info --------------------------------
    positions = {"Alpha": (0.0, 64.0, 0.0), "Bravo": (0.0, 64.0, -10.0)}
    if o.get("alpha_faces_away"):
        positions["Bravo"] = (0.0, 64.0, 10.0)
    frames_by_bot: Dict[str, List[Dict[str, Any]]] = {}
    for b in BOTS:
        frames = []
        for i in range(n - 10):
            t_ms = T0_MS + 100.0 + i * 50.0
            yaw, pitch = mf_yaw(i, b), mf_pitch(i)
            x, y, z = positions[b]
            other = positions["Bravo" if b == "Alpha" else "Alpha"]
            f: Dict[str, Any] = {
                "x": x, "y": y, "z": z, "yaw": round(yaw, 5), "pitch": round(pitch, 5),
                "action": {"forward": False, "camera": [0.0, 0.0]}, "renderTime": int(t_ms),
                "epochTime": t_ms / 1000.0, "relativeTimeMs": i * 50.0, "frame_count": i,
                "worldAgeTicks": GT0 + 2 + i - (i % 20), "episode_id": int(EPISODE),
                "state": {"health": 20.0, "food": 20, "saturation": 5.0, "hotbar": [], "selected_slot": 0,
                          "head_yaw": round(yaw, 5), "body_yaw": None,
                          "cursor": {"block": None, "entity": None}},
                "peer": {"name": "Bravo" if b == "Alpha" else "Alpha", "known": True, "pos": list(other),
                         "yaw": 0.0, "pitch": 0.0, "head_yaw": 0.0, "distance": 10.0},
            }
            if o.get("pose_link"):
                f["pose_seq"] = i  # the sample's look = movement packet i (controller pose link)
            frames.append(f)
        frames_by_bot[b] = frames
    for b, k in o.get("null_yaw", {}).items():
        for f in frames_by_bot[b][:k]:
            f["yaw"] = None
            f["pitch"] = None
    for b in o.get("no_state", ()):
        for f in frames_by_bot[b]:
            del f["state"]
    for b in BOTS:
        stem = f"{TS}_{EPISODE}_{b}_instance_{INSTANCE}"
        write_json(out / f"{stem}.json", frames_by_bot[b])
        write_json(out / f"{stem}_meta.json", {"frames": len(frames_by_bot[b]), "video_fps": 20})
        sm = {"time_of_day": 6000, "time_of_day_label": "day", "weather": "clear", "daylight_cycle": False,
              "weather_cycle": False, "difficulty": "normal", "damage_enabled": True}
        for k in o.get("drop_state_metadata", ()):
            sm.pop(k, None)
        write_json(out / f"{stem}_episode_info.json", {
            "bot_name": b, "episode_number": int(EPISODE), "episode_type": episode_type, "instance_id": 0,
            "encountered_error": bool(o.get("encountered_error")) and b == "Bravo",
            "state_metadata": sm})

    # ---- camera metas, sidecars, aligned metas -------------------------
    options = {"fov_deg": o.get("fov_deg", 70.0), "fov_effect_scale": o.get("fov_effect_scale", 0.0),
               "render_distance": 12, "simulation_distance": 12, "gui_scale": 2, "gui_scale_effective": 2,
               "bob_view": False, "damage_tilt_strength": 0.0, "hud_visible": True, "name_tags_visible": True}
    noise = o.get("render_noise_deg", 0.0)
    for b in BOTS:
        cam_dir = root / "camera" / f"output_{b.lower()}" / "0"
        meta = {"recording_path": f"C:\\data\\camera_{b.lower()}.mkv", "start_epoch_seconds": T0_MS / 1000,
                "fps": 20, "width": 1280, "height": 720, "camera_name": "Camera" + b,
                "video_encoder": "h264_nvenc", "wallclock_timestamps": True}
        if not o.get("linux_camera"):
            meta.update({"capture_method": "in_client_fbo_readback", "frames_file": f"camera_{b.lower()}_frames.jsonl",
                         "platform": "windows", "mc_version": "1.21", "options": options})
        cam_stem = f"camera_{b.lower()}"
        if o.get("camera_restart") and b == "Bravo":
            # the first recording ended (camera crashed) long before the episode and had a
            # wrong fov; the orchestrator restarted it as camera_bravo_r1.*
            write_json(cam_dir / f"{cam_stem}_meta.json", dict(
                meta, start_epoch_seconds=T0_MS / 1000 - 100, stop_epoch_seconds=T0_MS / 1000 - 50,
                frames=1000, options=dict(options, fov_deg=90.0)))
            cam_stem += "_r1"
            meta.update({"recording_path": f"{cam_stem}.mkv", "stop_epoch_seconds": T0_MS / 1000 + n * 0.05 + 1})
            if "frames_file" in meta:
                meta["frames_file"] = f"{cam_stem}_frames.jsonl"
        write_json(cam_dir / f"{cam_stem}_meta.json", meta)
        frames = frames_by_bot[b]
        side = []
        mapping = []
        pose_link = o.get("pose_link")
        for i, f in enumerate(frames):
            # the client renders the pose of the previous sample (50 ms lag). With pose_link
            # 9 of 10 frames render the bot's current movement packet (sent 20 ms before the
            # sample, camera set up 5 ms after it), every 10th frame the server-followed pose.
            injected = bool(pose_link) and i % 10 != 0
            j = i if injected else max(0, i - 1)
            if o.get("pose_link_swap") and injected and i + 1 < len(frames) and (i + 1) % 10 != 0:
                # frame i already shows packet i+1, frame i+1 still packet i (sample and frame
                # clocks are asynchronous): time pairing labels both with the wrong packet
                if i % 5 == 2:
                    j = i + 1
                elif i % 5 == 3:
                    j = i - 1
            src = frames[j]
            inj_noise = o.get("injected_noise_deg", 0.0) if injected and i % 7 == 3 else 0.0
            cam_yaw = mc_yaw_from_mf(mf_yaw(j, b)) + (noise if i % 7 == 3 else 0.0) + inj_noise
            cam_pitch = -math.degrees(mf_pitch(j))
            ems = f["epochTime"] * 1000.0
            side.append({"i": i, "epoch_ms": ems, "slot": i, "game_time": GT0 + 2 + i,
                         "cam_pos": [src["x"], src["y"] + 1.62, src["z"]], "cam_yaw": cam_yaw,
                         "cam_pitch": cam_pitch, "fov": 70.0, "player_pos": [src["x"], src["y"], src["z"]],
                         "fb": [1280, 720], "gui_hidden": False, "screen": None})
            pose = {}
            if pose_link:
                pose = {"pose_source": "injected" if injected else "server", "pose_seq": j if injected else i,
                        "pose_t": (src["epochTime"] * 1000.0 if injected else ems) - 20.0, "pose_age_ms": 25.0 if injected else 70.0, "pose_held": False}
                side[-1].update(pose, pose_sneak=False, pose_fallback=None if injected else "stale",
                                player_yaw=cam_yaw, player_pitch=cam_pitch)
            mapping.append({"action_index": i, "renderTime_ms": f["renderTime"], "action_time_sec": f["epochTime"],
                            "relative_time_ms": f["relativeTimeMs"], "frame_index": i,
                            "frame_time_sec": ems / 1000.0 + 0.03, "delta_sec": 0.035,
                            "cam_yaw": cam_yaw, "cam_pitch": cam_pitch, "cam_pos": side[-1]["cam_pos"],
                            "fov": 70.0, "sidecar_epoch_ms": ems, "cam_game_time": GT0 + 2 + i,
                            "cam_screen": None})
            if pose_link != "old_aligned":  # older process_recordings.py did not copy the pose fields
                mapping[-1].update(pose)
        if o.get("linux_camera") and o.get("camera_options_json"):
            # camera/launch_minecraft.py (Linux): options next to the recording
            write_json(cam_dir / "camera_options.json", dict(
                options, options_txt={"fov": "0.0"}, smooth_camera=False, width=1280, height=720))
        if not o.get("linux_camera"):
            with (cam_dir / f"{cam_stem}_frames.jsonl").open("w", encoding="utf-8") as fh:
                for s in side:
                    fh.write(json.dumps(s) + "\n")
        if not o.get("no_aligned"):
            camera_params = dict(options) if not o.get("linux_camera") else {}
            camera_params.update({"width": 1280, "height": 720, "fps": 20, "encoder": "h264_nvenc",
                                  "options_recorded": not o.get("linux_camera")})
            aligned = {"alignment_mode": "wallclock", "fps": 20, "camera_params": camera_params,
                       "diagnostics": {"sidecar_used": not o.get("linux_camera")},
                       "frame_mapping": [
                           {k: v for k, v in m.items()
                            if not (o.get("linux_camera") and k.startswith(("cam_", "fov", "sidecar")))}
                           for m in mapping]}
            write_json(root / "aligned" / f"{TS}_{EPISODE}_{b}_instance_{INSTANCE}_camera_meta.json", aligned)
    return {"root": root, "frames": frames_by_bot}
