#!/usr/bin/env python3
"""Copy each episode's character skins + reference sheets next to the episode files.

For every `*_episode_info.json` found in <batch dir>/output (or in <batch dir> itself, or
recursively with --recursive) whose `state_metadata.skins` was written by
controller/utils/skins.js, writes next to it (same stem, `_episode_info` replaced):

    <stem>_skin_<Player>.png              raw 64x64 skin of each player (Alpha and Bravo)
    <stem>_skin_<Player>_reference.png    reference sheet (front/back/left/right T-pose + texture)
    <stem>_skins.json                     manifest (see windows/schema_parts/r2-skins.md)

`<stem>` is the episode_info stem `TS_EEEEEE_<Bot>_instance_III`, so every bot file gets both
players' skins: `self` = the bot whose view was recorded (its skin shows in the first-person
hand), `other` = the player seen in the video.

Usage:
    python tools/skins/export_episode_skins.py <batch dir> [--out <dir>] [--skins-dir server/skins]
                                               [--recursive] [--dry-run]

Missing reference sheets are rendered on the fly (tools/skins/render_reference.py).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

REPO = Path(__file__).resolve().parents[2]
SKINS = REPO / "server" / "skins"


def find_infos(batch: Path, recursive: bool):
    if recursive:
        return sorted(batch.rglob("*_episode_info.json"))
    for d in (batch / "output", batch):
        found = sorted(d.glob("*_episode_info.json")) if d.is_dir() else []
        if found:
            return found
    return []


def sha256(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def export(batch: Path, out: Path | None, skins_dir: Path, recursive: bool, dry: bool) -> dict:
    cat_path = skins_dir / "catalogue.json"
    catalogue = {}
    if cat_path.is_file():
        catalogue = {s["skin_id"]: s for s in json.loads(cat_path.read_text(encoding="utf-8"))["skins"]}
    stats = {"episode_infos": 0, "with_skins": 0, "files_written": 0, "missing": []}
    for info_path in find_infos(batch, recursive):
        stats["episode_infos"] += 1
        try:
            info = json.loads(info_path.read_text(encoding="utf-8"))
        except Exception as e:  # noqa: BLE001
            stats["missing"].append(f"{info_path.name}: unreadable ({e})")
            continue
        sm = info.get("state_metadata") or {}
        skins = sm.get("skins")
        if not skins:
            continue
        stats["with_skins"] += 1
        stem = info_path.name[: -len("_episode_info.json")]
        dst_dir = out if out else info_path.parent
        parts = stem.split("_")  # YYYYMMDD, HHMMSS, EEEEEE, Bot, instance, III
        me = info.get("bot_name") or (parts[3] if len(parts) > 3 else None)
        manifest = {"episode_info": info_path.name, "episode_number": info.get("episode_number"),
                    "self": me, "other": None, "skins": {}, "skin_assignment": sm.get("skin_assignment")}
        for player, s in skins.items():
            if player != me:
                manifest["other"] = player
            sid = s.get("skin_id")
            entry = catalogue.get(sid, {})
            src = skins_dir / (entry.get("file") or s.get("file") or f"pool/{sid}.png")
            ref = skins_dir / (entry.get("reference") or s.get("reference") or f"reference/{sid}.png")
            rec = {k: s.get(k) for k in ("skin_id", "model", "source", "sha256", "texture_hash", "seed", "tags")}
            if not src.is_file():
                stats["missing"].append(f"{stem}: skin file {src} not found")
                manifest["skins"][player] = rec
                continue
            if s.get("sha256") and sha256(src) != s["sha256"]:
                stats["missing"].append(f"{stem}: {src.name} sha256 differs from episode_info (pool regenerated?)")
            skin_dst = dst_dir / f"{stem}_skin_{player}.png"
            ref_dst = dst_dir / f"{stem}_skin_{player}_reference.png"
            rec["skin_file"] = skin_dst.name
            rec["reference_file"] = ref_dst.name
            manifest["skins"][player] = rec
            if dry:
                continue
            dst_dir.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(src, skin_dst)
            if not ref.is_file():
                import render_reference
                render_reference.render_catalogue_skin(
                    {"skin_id": sid, "file": str(src.relative_to(skins_dir)), "model": s.get("model", "classic"),
                     "sha256": s.get("sha256")}, ref.parent, skins_dir)
            shutil.copyfile(ref, ref_dst)
            stats["files_written"] += 2
        if not dry:
            (dst_dir / f"{stem}_skins.json").write_text(json.dumps(manifest, indent=1), encoding="utf-8")
            stats["files_written"] += 1
    return stats


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("batch", type=Path, help="batch dir (data_collection/train/batch_<k>) or a dir of episode_info files")
    ap.add_argument("--out", type=Path, default=None, help="write here instead of next to each episode_info")
    ap.add_argument("--skins-dir", type=Path, default=SKINS)
    ap.add_argument("--recursive", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    st = export(a.batch, a.out, a.skins_dir, a.recursive, a.dry_run)
    print(json.dumps(st, indent=1))
    return 0 if not st["missing"] else 2


if __name__ == "__main__":
    sys.exit(main())
