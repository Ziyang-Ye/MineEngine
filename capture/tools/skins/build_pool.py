#!/usr/bin/env python3
"""Build / refresh the per-episode skin pool under server/skins/.

Steps (all idempotent):
  1. extract the 18 default skins (9 characters x wide/slim) from the 1.21 client jar
  2. generate N procedural skins (generate_skins.py, deterministic by --seed)
  3. write server/skins/catalogue.json (see windows/schema_parts/r2-skins.md)
  4. render reference sheets server/skins/reference/<skin_id>.png (render_reference.py)

Signed caches (.customskin) are produced separately (network, rate limited):
    python regenerate_skin_cache.py --upload server/skins/pool --verify   (uploads to MineSkin; only for mode=signed / Docker)
then refresh the `cached` flags in the catalogue:
    python tools/skins/build_pool.py --catalogue-only

Usage:
    python tools/skins/build_pool.py [--count 300] [--seed 20251023] [--no-references]
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import extract_defaults  # noqa: E402
import generate_skins  # noqa: E402
import render_reference  # noqa: E402

REPO = Path(__file__).resolve().parents[2]
SKINS = REPO / "server" / "skins"
POOL = SKINS / "pool"
CATALOGUE = SKINS / "catalogue.json"
LEGACY = [("session_a", "session_a.png", "classic"), ("session_b", "session_b.png", "classic")]


def sha256(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def cache_info(png: Path) -> dict:
    cp = png.with_suffix(png.suffix + ".customskin")
    if not cp.exists():
        return {"cached": False}
    try:
        d = json.loads(cp.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {"cached": False}
    fresh = bool(d.get("value") and d.get("signature")) and d.get("png_sha256") in (None, sha256(png))
    out = {"cached": fresh}
    if d.get("texture_hash"):
        out["texture_hash"] = d["texture_hash"]
    return out


def build_catalogue(count: int, seed: int, generate: bool) -> dict:
    skins = []
    for name, file, model in LEGACY:
        p = SKINS / file
        if p.exists():
            skins.append({"skin_id": name, "file": file, "model": model, "source": "legacy", "seed": None,
                          "sha256": sha256(p), "tags": [f"model:{model}", "legacy"]})
    jar = extract_defaults.DEFAULT_JAR
    if jar.is_file():
        rows = extract_defaults.extract(jar, POOL)
    else:  # jar missing (e.g. Linux checkout): use what is already in pool/
        rows = []
        for n in extract_defaults.NAMES:
            for variant, model in extract_defaults.VARIANTS.items():
                f = POOL / f"default_{n}_{variant}.png"
                if f.exists():
                    rows.append({"skin_id": f.stem, "file": f, "model": model, "name": n, "variant": variant,
                                 "sha256": sha256(f)})
    for r in rows:
        skins.append({"skin_id": r["skin_id"], "file": f"pool/{r['file'].name}", "model": r["model"],
                      "source": "default", "seed": None, "sha256": r["sha256"],
                      "tags": [f"model:{r['model']}", f"default:{r['name']}"]})
    if generate:
        prow = generate_skins.generate_pool(POOL, count, seed)
    else:
        prow = []
        for i in range(count):
            f = POOL / f"proc_{i:04d}.png"
            if not f.exists():
                continue
            s = generate_skins.skin_seed(seed, i)
            _, model, tags, attrs = generate_skins.generate(s)
            prow.append({"skin_id": f.stem, "file": f, "model": model, "seed": s, "tags": tags, "attributes": attrs})
    for r in prow:
        skins.append({"skin_id": r["skin_id"], "file": f"pool/{Path(r['file']).name}", "model": r["model"],
                      "source": "procedural", "seed": r["seed"], "sha256": sha256(Path(r["file"])),
                      "tags": r["tags"], "attributes": r.get("attributes", {})})
    for s in skins:
        s.update(cache_info(SKINS / s["file"]))
        s["reference"] = f"reference/{s['skin_id']}.png"
    by_src = {}
    for s in skins:
        by_src.setdefault(s["source"], 0)
        by_src[s["source"]] += 1
    return {
        "schema": "solaris.skin_catalogue/1",
        "generated_utc": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "generator": {"script": "tools/skins/generate_skins.py", "base_seed": seed, "count": count,
                      "seed_formula": f"base_seed * {generate_skins.SEED_STRIDE} + index"},
        "counts": {"total": len(skins), "cached": sum(1 for s in skins if s.get("cached")), **by_src},
        "skins": skins,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--count", type=int, default=300)
    ap.add_argument("--seed", type=int, default=generate_skins.DEFAULT_SEED)
    ap.add_argument("--catalogue-only", action="store_true",
                    help="do not regenerate PNGs / references; only rewrite catalogue.json (cache flags)")
    ap.add_argument("--no-references", action="store_true")
    a = ap.parse_args()
    if a.catalogue_only and CATALOGUE.exists():
        old = json.loads(CATALOGUE.read_text(encoding="utf-8"))
        a.count = old.get("generator", {}).get("count", a.count)
        a.seed = old.get("generator", {}).get("base_seed", a.seed)
    cat = build_catalogue(a.count, a.seed, generate=not a.catalogue_only)
    CATALOGUE.write_text(json.dumps(cat, indent=1), encoding="utf-8")
    print(f"catalogue: {CATALOGUE} ({cat['counts']})")
    if not a.no_references and not a.catalogue_only:
        for e in cat["skins"]:
            render_reference.render_catalogue_skin(e, SKINS / "reference")
        print(f"reference sheets -> {SKINS / 'reference'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
