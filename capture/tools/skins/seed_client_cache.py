#!/usr/bin/env python3
"""Pre-seed a Minecraft client's skin download cache with the pool textures, so camera clients
never depend on downloading skins from textures.minecraft.net at runtime.

Why: the 1.21 client downloads each player's skin over plain HTTP from the URL in the signed
textures property. If that download fails once (timeout, no internet), the client keeps the
failure for that profile+texture and shows the DEFAULT skin (Steve/Alex/...) for the rest of
the session - silently. HttpTexture first looks for a local copy at

    <assets dir>/skins/<sha1[0:2]>/<sha1>      sha1 = SHA-1 of the texture hash (UTF-16LE chars,
                                                 Guava hashUnencodedChars), texture hash = last
                                                 path segment of the textures.minecraft.net URL

and loads it without any network access. This script writes every cached pool skin there.
The PNG written is the pool PNG itself (regenerate_skin_cache.py --verify checks that Mojang's
copy is pixel-identical; `verified_pixels` in the .customskin), or with --download the exact
bytes from textures.minecraft.net.

Usage:
    python tools/skins/seed_client_cache.py [--mc-home .cache/windows/minecraft] [--download] [--force]

The default --mc-home is the shared client home used by camera/windows/launch_camera.py
(its assets dir is <mc-home>/assets). Run it after regenerate_skin_cache.py and before the
camera clients start (idempotent; existing files are kept unless --force).
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
SKINS = REPO / "server" / "skins"
DEFAULT_MC_HOME = REPO / ".cache" / "windows" / "minecraft"


def client_cache_name(texture_hash: str) -> str:
    return hashlib.sha1(texture_hash.encode("utf-16-le")).hexdigest()


def texture_hash_of(cache: dict) -> str | None:
    if cache.get("texture_hash"):
        return cache["texture_hash"]
    try:
        url = json.loads(base64.b64decode(cache["value"]))["textures"]["SKIN"]["url"]
        return url.rsplit("/", 1)[-1]
    except Exception:  # noqa: BLE001
        return None


def iter_cached_pngs(skins_dir: Path):
    for cp in sorted(list(skins_dir.glob("*.png.customskin")) + list((skins_dir / "pool").glob("*.png.customskin"))):
        png = cp.with_name(cp.name[: -len(".customskin")])
        if png.exists():
            yield png, json.loads(cp.read_text(encoding="utf-8"))


def seed(mc_home: Path, skins_dir: Path = SKINS, download: bool = False, force: bool = False) -> dict:
    root = mc_home / "assets" / "skins"
    stats = {"written": 0, "kept": 0, "failed": 0, "unverified": 0}
    for png, cache in iter_cached_pngs(skins_dir):
        th = texture_hash_of(cache)
        if not th:
            stats["failed"] += 1
            continue
        name = client_cache_name(th)
        dst = root / name[:2] / name
        if dst.exists() and not force:
            stats["kept"] += 1
            continue
        data = None
        if download:
            try:
                import requests
                r = requests.get(f"https://textures.minecraft.net/texture/{th}", timeout=30)
                r.raise_for_status()
                data = r.content
            except Exception as e:  # noqa: BLE001
                print(f"  download failed for {png.name}: {e}; using the pool PNG", file=sys.stderr)
        if data is None:
            data = png.read_bytes()
            if cache.get("verified_pixels") is not True:
                stats["unverified"] += 1
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_bytes(data)
        stats["written"] += 1
    return stats


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--mc-home", type=Path, default=DEFAULT_MC_HOME)
    ap.add_argument("--skins-dir", type=Path, default=SKINS)
    ap.add_argument("--download", action="store_true", help="fetch the exact Mojang bytes instead of the pool PNG")
    ap.add_argument("--force", action="store_true")
    a = ap.parse_args()
    st = seed(a.mc_home, a.skins_dir, a.download, a.force)
    print(f"client skin cache {a.mc_home / 'assets' / 'skins'}: {st}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
