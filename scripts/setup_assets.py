#!/usr/bin/env python3
"""One-time setup of the files this repository does not ship. Run it after capture setup has installed Minecraft 1.21.
  1. block registry: geometry, states and face crops, built from YOUR Minecraft 1.21 client jar
     -> $MINEENGINE_HOME/mc_registry_1.21.json (default ~/.cache/mineengine/), see mineengine.mc_registry;
  2. the 18 Minecraft default skins from the same jar -> capture/server/skins/pool/default_*, plus their reference sheets;
  3. the air-bar template of the water-episode filter (capture/postprocess/detect_water_episodes_batch.py), drawn from
     the jar's air bubble sprite -> capture/postprocess/assets/minecraft-hud-oxygen-bar-rgba.png;
  4. third-party Paper plugins (ProtocolLib, SkinsRestorer) from their official releases, sha256-checked
     -> capture/server/plugins/.
  python scripts/setup_assets.py [--jar PATH] [--skip-plugins | --plugins-from DIR]   (offline: copy the plugin jars from DIR)
The client jar is looked up in this order:
  --jar; $MC_CLIENT_JAR; capture/.cache/windows/minecraft/versions/1.21/1.21.jar; ~/.minecraft/versions/1.21/1.21.jar.
Minecraft and its assets belong to Mojang. They are read from your own installation and never redistributed by this repository.
The MirrorBot Paper plugin is built from source: on Windows setup_windows.cmd builds it with Maven; elsewhere
bash capture/server/build_plugin.sh (builds it in a Maven container, so it needs Docker) or setup_windows.py --maven."""
import argparse
import hashlib
import os
import shutil
import subprocess
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CAP = ROOT / "capture"
PLUGINS = [  # name, version, url, sha256, licence
    ("ProtocolLib.jar", "5.4.0", "https://github.com/dmulloy2/ProtocolLib/releases/download/5.4.0/ProtocolLib.jar",
     "ee2e7ab9b5386f2d103081c4d108e61b1035df2ca692b53d6e2409fb1f5caccf", "GPL-2.0"),
    ("SkinsRestorer.jar", "15.8.2", "https://github.com/SkinsRestorer/SkinsRestorer/releases/download/15.8.2/SkinsRestorer.jar",
     "7defc4d64580f649afd3a89775d1f1e6685a51fd10cf8488c1b6a8308410a2cd", "GPL-3.0"),
]


def sha256(p):
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()


def find_jar(explicit):
    for c in (explicit, os.environ.get("MC_CLIENT_JAR"), CAP / ".cache/windows/minecraft/versions/1.21/1.21.jar",
              Path.home() / ".minecraft/versions/1.21/1.21.jar"):
        if c and Path(c).is_file():
            return Path(c)
    sys.exit("no Minecraft 1.21 client jar found: run the capture setup first (it installs the client), or pass --jar")


def registry(jar):
    home = Path(os.environ.get("MINEENGINE_HOME") or Path.home() / ".cache" / "mineengine"); home.mkdir(parents=True, exist_ok=True)
    out = home / "mc_registry_1.21.json"
    subprocess.run([sys.executable, "-m", "mineengine.mc_registry", "--jar", str(jar), "--out", str(out)], check=True, cwd=ROOT)
    print(f"[setup] block registry -> {out}")


def default_skins(jar):
    sys.path.insert(0, str(CAP / "tools" / "skins"))
    import extract_defaults
    import render_reference
    pool = CAP / "server" / "skins" / "pool"
    rows = extract_defaults.extract(jar, pool)
    for r in rows:
        render_reference.render_catalogue_skin({"skin_id": r["skin_id"], "file": f"pool/{r['file'].name}", "model": r["model"]},
                                               CAP / "server" / "skins" / "reference")
    print(f"[setup] {len(rows)} default skins -> {pool} (+ reference sheets)")


def oxygen_template(jar):
    """The jar's 9 x 9 air bubble sprite, x3 nearest (GUI scale 3 at 1280 x 720), 10 bubbles 24 px apart drawn left to
    right, columns 1..242: the 27 x 242 RGBA template detect_water_episodes_batch.py matches in the frames."""
    import zipfile

    import numpy as np
    from PIL import Image
    with zipfile.ZipFile(jar) as z, z.open("assets/minecraft/textures/gui/sprites/hud/air.png") as f:
        s = np.asarray(Image.open(f).convert("RGBA"))
    b = s.repeat(3, 0).repeat(3, 1); m = b[..., 3] > 0
    img = np.zeros((27, 243, 4), np.uint8)
    for i in range(10):
        img[:, 24 * i:24 * i + 27][m] = b[m]
    img = np.ascontiguousarray(img[:, 1:])
    if s.shape != (9, 9, 4) or int((img[..., 3] == 255).sum()) != 3258:
        sys.exit("unexpected air bubble sprite in the jar: is it the Minecraft 1.21 client jar?")
    out = CAP / "postprocess" / "assets" / "minecraft-hud-oxygen-bar-rgba.png"; out.parent.mkdir(parents=True, exist_ok=True)
    Image.fromarray(img).save(out)
    print(f"[setup] air-bar template -> {out}")


def plugins(src_dir):
    dst = CAP / "server" / "plugins"; dst.mkdir(parents=True, exist_ok=True)
    for name, ver, url, want, lic in PLUGINS:
        p = dst / name
        if p.is_file() and sha256(p) == want:
            print(f"[setup] {name} {ver} present"); continue
        tmp = p.with_suffix(".part")
        if src_dir:
            shutil.copyfile(Path(src_dir) / name, tmp)
        else:
            print(f"[setup] downloading {name} {ver} ({lic}) from {url}")
            with urllib.request.urlopen(url, timeout=120) as r, open(tmp, "wb") as f:
                shutil.copyfileobj(r, f)
        got = sha256(tmp)
        if got != want:
            tmp.unlink(); sys.exit(f"{name}: sha256 {got} != pinned {want}")
        tmp.replace(p); print(f"[setup] {name} {ver} ok")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--jar"); ap.add_argument("--skip-plugins", action="store_true"); ap.add_argument("--plugins-from")
    a = ap.parse_args()
    jar = find_jar(a.jar); print(f"[setup] client jar {jar}")
    registry(jar); default_skins(jar); oxygen_template(jar)
    if not a.skip_plugins:
        plugins(a.plugins_from)


if __name__ == "__main__":
    main()
