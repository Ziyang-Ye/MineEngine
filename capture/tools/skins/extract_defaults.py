#!/usr/bin/env python3
"""Extract the 9 default Minecraft player skins (wide + slim = 18 PNGs) from a client jar.

Usage:
    python tools/skins/extract_defaults.py [--jar <1.21.jar>] [--out server/skins/pool]

Output files: <out>/default_<name>_<wide|slim>.png (64x64 RGBA, byte-identical to the jar entry).
The jar defaults to the client downloaded by windows/setup_windows.py
(.cache/windows/minecraft/versions/1.21/1.21.jar).
"""
import argparse
import hashlib
import sys
import zipfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
DEFAULT_JAR = REPO / ".cache" / "windows" / "minecraft" / "versions" / "1.21" / "1.21.jar"
DEFAULT_OUT = REPO / "server" / "skins" / "pool"
NAMES = ["steve", "alex", "ari", "efe", "kai", "makena", "noor", "sunny", "zuri"]
VARIANTS = {"wide": "classic", "slim": "slim"}


def extract(jar: Path, out: Path) -> list:
    """Returns a list of dicts {skin_id, file, model, name, variant, sha256}."""
    out.mkdir(parents=True, exist_ok=True)
    result = []
    with zipfile.ZipFile(jar) as zf:
        for name in NAMES:
            for variant, model in VARIANTS.items():
                entry = f"assets/minecraft/textures/entity/player/{variant}/{name}.png"
                data = zf.read(entry)
                skin_id = f"default_{name}_{variant}"
                dst = out / f"{skin_id}.png"
                if not dst.exists() or dst.read_bytes() != data:
                    dst.write_bytes(data)
                result.append({"skin_id": skin_id, "file": dst, "model": model, "name": name,
                               "variant": variant, "sha256": hashlib.sha256(data).hexdigest()})
    return result


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--jar", type=Path, default=DEFAULT_JAR)
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT)
    a = ap.parse_args()
    if not a.jar.is_file():
        print(f"client jar not found: {a.jar} (run windows/setup_windows.py or pass --jar)", file=sys.stderr)
        return 1
    rows = extract(a.jar, a.out)
    for r in rows:
        print(f"{r['skin_id']:<26} {r['model']:<8} {r['sha256'][:12]}")
    print(f"extracted {len(rows)} default skins -> {a.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
