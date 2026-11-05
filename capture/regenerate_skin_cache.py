#!/usr/bin/env python3
# Modified by Ziyang Ye for MineEngine, 2026 (from SolarisEngine 78f23e7); see capture/MODIFICATIONS.md
"""
Generate .customskin cache files (Mojang-signed texture properties) for Minecraft skin PNGs.

Why this is needed
------------------
The Minecraft 1.21 client only shows another player's skin if the profile's `textures`
property carries a valid Mojang signature (net.minecraft.client.multiplayer.PlayerInfo
#createSkinLookup falls back to the default Steve/Alex/... skin when
`PlayerSkin.secure()` is false) and the texture URL is on textures.minecraft.net.
An unsigned property, or a self-hosted texture URL, therefore never reaches the camera
clients. Only Mojang can produce the signature, so each PNG is uploaded once to the
MineSkin service (https://mineskin.org), which sets it on one of its Minecraft accounts
and returns the signed `value` + `signature`. The signed data does not expire; the
`.customskin` file stores it so the server never needs network access at runtime
(SkinManager injects it into SkinsRestorer as a custom skin).

The skin model (classic = 4px arms, slim = 3px arms) is part of the signed payload
(`textures.SKIN.metadata.model`), so the correct `variant` must be sent at upload time.
It is taken from server/skins/catalogue.json when the PNG is listed there, otherwise it
is detected from the PNG (slim skins leave the 4th arm column transparent).

NOT NEEDED for the per-episode pool any more: MirrorBot's local skin mode (`/episode skin ...
mode=auto|local`, the default of controller/utils/skins.js) sets an unsigned texture that the
solaris-capture camera mod renders from its local copy of the pool, fully offline. Signed
caches are only used by `mode=signed` (and by the top-level session skins of `episode start`).

Nothing is ever uploaded without the explicit `--upload` flag: without it the script only
lists the PNGs that have no (fresh) cache and exits (dry run). Setup scripts never call it.

Usage (Windows: `python`, Linux/macOS: `python3`):
    # dry run: which PNGs would be uploaded
    python regenerate_skin_cache.py server/skins/ --recursive
    # one skin
    python regenerate_skin_cache.py --upload server/skins/my_skin.png
    # every PNG without a (fresh) cache in a directory; --recursive includes pool/
    python regenerate_skin_cache.py --upload server/skins/ --recursive
    # the whole catalogue pool, at most 50 uploads, stop after 60 minutes
    python regenerate_skin_cache.py --upload server/skins/pool --limit 50 --max-minutes 60
    # force regenerate
    python regenerate_skin_cache.py --upload --force server/skins/my_skin.png

Rate limits: without an API key MineSkin allows ~10 generations/minute and ~60/hour
(the script follows the `rateLimit` hints and resumes where it stopped: already cached
skins are skipped). With a key (free at https://account.mineskin.org) set
MINESKIN_API_KEY or pass --api-key for much higher limits.

Skins are uploaded with visibility "unlisted" by default (not shown in the public
MineSkin gallery). Only the PNG image itself is sent.

.customskin format (JSON; MirrorBot reads `value` + `signature`, the rest is provenance):
    {"skinName": "<png name>", "value": "<base64 textures property>",
     "signature": "<base64 Mojang signature>", "dataVersion": 1,
     "variant": "classic|slim", "texture_url": "...", "texture_hash": "...",
     "png_sha256": "<sha256 of the PNG that was uploaded>", "mineskin_uuid": "...",
     "generated_utc": "..."}
A cache whose png_sha256 differs from the current PNG is treated as stale and regenerated.

Requirements: requests (pip install requests); Pillow for variant detection / --verify.
"""

import argparse
import base64
import datetime as _dt
import hashlib
import io
import json
import os
import sys
import time
from pathlib import Path

try:
    import requests
except ImportError:
    print("Error: 'requests' package is required. Install it with: pip install requests", file=sys.stderr)
    sys.exit(1)

REPO = Path(__file__).resolve().parent
MINESKIN_V2_URL = "https://api.mineskin.org/v2/generate"
MINESKIN_V2_QUEUE = "https://api.mineskin.org/v2/queue/"
MINESKIN_LEGACY_URL = "https://api.mineskin.org/generate/upload"
USER_AGENT = "mineengine-skin-cache/0.1 (+https://github.com/Ziyang-Ye/MineEngine; data-collection skin pool)"
DEFAULT_CATALOGUE = REPO / "server" / "skins" / "catalogue.json"


def sha256_file(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def cache_path_for(png: Path) -> Path:
    return png.with_suffix(png.suffix + ".customskin")


def decode_value(value: str) -> dict:
    return json.loads(base64.b64decode(value).decode("utf-8"))


def cache_is_fresh(png: Path) -> bool:
    cp = cache_path_for(png)
    if not cp.exists():
        return False
    try:
        data = json.loads(cp.read_text(encoding="utf-8"))
    except Exception:
        return False
    if not data.get("value") or not data.get("signature"):
        return False
    want = data.get("png_sha256")
    # legacy caches (session_a/session_b) carry no hash: accept them as-is
    return want is None or want == sha256_file(png)


def load_catalogue_models(path: Path) -> dict:
    """png file name -> model ('classic'|'slim') from catalogue.json (if present)."""
    out = {}
    if path and path.is_file():
        try:
            cat = json.loads(path.read_text(encoding="utf-8"))
            for s in cat.get("skins", []):
                out[Path(s["file"]).name.lower()] = s.get("model", "classic")
        except Exception as e:  # noqa: BLE001
            print(f"  warning: cannot read catalogue {path}: {e}", file=sys.stderr)
    return out


def detect_variant(png: Path) -> str:
    try:
        from PIL import Image
    except ImportError:
        return "classic"
    img = Image.open(png).convert("RGBA")
    if img.size != (64, 64):
        return "classic"
    px = img.load()
    alpha = sum(px[x, y][3] for x in (54, 55) for y in range(20, 32))
    return "slim" if alpha == 0 else "classic"


class RateGate:
    """Spaces requests according to MineSkin's rateLimit hints."""

    def __init__(self, min_delay: float):
        self.next_at = 0.0
        self.min_delay = min_delay

    def wait(self):
        dt = self.next_at - time.time()
        if dt > 30:
            print(f"    rate limit: waiting {dt:.0f}s for the next MineSkin slot ...", flush=True)
        if dt > 0:
            time.sleep(dt)

    def update(self, payload: dict):
        delay = self.min_delay
        try:
            rl = payload.get("rateLimit") or {}
            nxt = (rl.get("next") or {})
            if "absolute" in nxt:
                delay = max(delay, nxt["absolute"] / 1000.0 - time.time())
            elif "relative" in nxt:
                delay = max(delay, nxt["relative"] / 1000.0)
            lim = rl.get("limit") or {}
            hour = lim.get("hour") or {}
            if hour.get("remaining", 1) <= 0 and hour.get("reset"):
                delay = max(delay, hour["reset"] - time.time() + 2)
            minute = lim.get("minute") or {}
            if minute.get("remaining", 1) <= 0 and minute.get("reset"):
                delay = max(delay, minute["reset"] - time.time() + 1)
        except Exception:  # noqa: BLE001
            pass
        self.next_at = time.time() + max(0.0, delay)


def _headers(api_key):
    h = {"User-Agent": USER_AGENT, "Accept": "application/json"}
    if api_key:
        h["Authorization"] = f"Bearer {api_key}"
    return h


def upload_v2(png: Path, variant: str, visibility: str, api_key, gate: RateGate, max_wait: float):
    """Returns (texture_dict, skin_meta) or raises RuntimeError. Retries on 429."""
    deadline = time.time() + max_wait
    attempt = 0
    while True:
        attempt += 1
        gate.wait()
        try:
            with open(png, "rb") as f:
                resp = requests.post(
                    MINESKIN_V2_URL,
                    headers=_headers(api_key),
                    files={"file": (png.name, f, "image/png")},
                    data={"variant": variant, "visibility": visibility, "name": f"solaris_{png.stem}"[:20]},
                    timeout=120,
                )
        except requests.RequestException as e:  # connection reset / timeout: retry a few times
            if attempt >= 4:
                raise
            print(f"    network error ({e.__class__.__name__}), retrying in 15s", flush=True)
            gate.next_at = time.time() + 15
            continue
        try:
            payload = resp.json()
        except ValueError:
            payload = {}
        gate.update(payload)
        if resp.status_code == 429 or (payload.get("errors") and any(
                e.get("code") in ("rate_limit", "too_many_requests") for e in payload.get("errors", []))):
            wait = max(gate.next_at - time.time(), 5)
            if time.time() + wait > deadline:
                raise RuntimeError("rate limited and --max-wait exceeded")
            print(f"    rate limited, waiting {wait:.0f}s ...", flush=True)
            time.sleep(wait)
            continue
        if resp.status_code in (200, 201) and payload.get("skin"):
            skin = payload["skin"]
            return skin["texture"], skin
        if resp.status_code == 202 or (payload.get("job") and not payload.get("skin")):
            job = (payload.get("job") or {}).get("id") or (payload.get("job") or {}).get("uuid")
            if not job:
                raise RuntimeError(f"queued without job id: {str(payload)[:300]}")
            return poll_job(job, api_key, gate, deadline)
        if resp.status_code >= 500 and attempt < 4:
            print(f"    MineSkin HTTP {resp.status_code}, retrying in 20s", flush=True)
            time.sleep(20)
            continue
        raise RuntimeError(f"HTTP {resp.status_code}: {json.dumps(payload)[:500] if payload else resp.text[:500]}")


def poll_job(job_id: str, api_key, gate: RateGate, deadline: float):
    while time.time() < deadline:
        time.sleep(2)
        r = requests.get(MINESKIN_V2_QUEUE + job_id, headers=_headers(api_key), timeout=60)
        try:
            p = r.json()
        except ValueError:
            continue
        status = (p.get("job") or {}).get("status")
        if p.get("skin") and status in (None, "completed"):
            return p["skin"]["texture"], p["skin"]
        if status == "failed":
            raise RuntimeError(f"job {job_id} failed: {json.dumps(p)[:400]}")
    raise RuntimeError(f"job {job_id} did not finish in time")


def upload_legacy(png: Path, variant: str, gate: RateGate):
    gate.wait()
    with open(png, "rb") as f:
        resp = requests.post(MINESKIN_LEGACY_URL, headers={"User-Agent": USER_AGENT},
                             files={"file": (png.name, f, "image/png")},
                             data={"variant": variant, "visibility": 1}, timeout=120)
    payload = resp.json() if resp.headers.get("content-type", "").startswith("application/json") else {}
    gate.update(payload)
    if resp.status_code != 200:
        raise RuntimeError(f"legacy HTTP {resp.status_code}: {resp.text[:300]}")
    t = payload["data"]["texture"]
    return {"data": {"value": t["value"], "signature": t["signature"]}, "url": {"skin": t.get("url")}}, payload


def verify_texture(png: Path, value: str) -> str:
    """Downloads the signed texture URL and compares it with the PNG (RGBA of every
    pixel whose alpha > 0 in either). Returns '' when identical, else a description."""
    from PIL import Image
    url = decode_value(value)["textures"]["SKIN"]["url"]
    r = requests.get(url.replace("http://", "https://"), timeout=60, headers={"User-Agent": USER_AGENT})
    r.raise_for_status()
    a = Image.open(io.BytesIO(r.content)).convert("RGBA")
    b = Image.open(png).convert("RGBA")
    if a.size != b.size:
        return f"size {a.size} != {b.size}"
    pa, pb = a.load(), b.load()
    diff = 0
    for y in range(b.size[1]):
        for x in range(b.size[0]):
            ca, cb = pa[x, y], pb[x, y]
            if ca[3] == 0 and cb[3] == 0:
                continue
            if ca != cb:
                diff += 1
    return "" if diff == 0 else f"{diff} pixels differ from the Mojang copy"


def generate_customskin(png: Path, force: bool, variant: str, args, gate: RateGate) -> bool:
    cache_path = cache_path_for(png)
    if not force and cache_is_fresh(png):
        print(f"  Skipping {png.name} (cache up to date)")
        return True
    print(f"  Uploading {png.name} ({variant}) to MineSkin ...", flush=True)
    try:
        try:
            texture, skin = upload_v2(png, variant, args.visibility, args.api_key, gate, args.max_wait)
        except RuntimeError as e:
            if not args.legacy_fallback:
                raise
            print(f"    v2 failed ({e}); trying legacy endpoint", flush=True)
            texture, skin = upload_legacy(png, variant, gate)
        value = texture["data"]["value"]
        signature = texture["data"]["signature"]
    except (requests.RequestException, RuntimeError, KeyError, TypeError) as e:
        print(f"  ERROR: {png.name}: {e}", file=sys.stderr, flush=True)
        return False

    decoded = decode_value(value)
    skin_tex = decoded.get("textures", {}).get("SKIN", {})
    got_model = (skin_tex.get("metadata") or {}).get("model", "classic")
    if got_model != variant:
        print(f"  ERROR: {png.name}: signed model {got_model!r} != requested {variant!r}", file=sys.stderr)
        return False
    cache = {
        "skinName": png.name,
        "value": value,
        "signature": signature,
        "dataVersion": 1,
        "variant": variant,
        "texture_url": skin_tex.get("url"),
        "texture_hash": (skin_tex.get("url") or "").rsplit("/", 1)[-1],
        "png_sha256": sha256_file(png),
        "mineskin_uuid": skin.get("uuid") if isinstance(skin, dict) else None,
        "generated_utc": _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    if args.verify:
        try:
            problem = verify_texture(png, value)
        except Exception as e:  # noqa: BLE001
            problem = f"verify failed: {e}"
        cache["verified_pixels"] = problem == ""
        if problem:
            print(f"    warning: {problem}", flush=True)
    tmp = cache_path.with_suffix(cache_path.suffix + ".tmp")
    tmp.write_text(json.dumps(cache), encoding="utf-8")
    os.replace(tmp, cache_path)
    print(f"  Created {cache_path.name} -> {cache['texture_hash'][:16]}", flush=True)
    return True


def collect_pngs(path: Path, recursive: bool) -> list:
    if path.is_file():
        if path.suffix.lower() != ".png":
            raise SystemExit(f"Error: {path} is not a .png file")
        return [path]
    if path.is_dir():
        it = path.rglob("*.png") if recursive else path.glob("*.png")
        # never upload the reference sheets
        return sorted(p for p in it if "reference" not in p.relative_to(path).parts)
    raise SystemExit(f"Error: {path} does not exist")


def main():
    parser = argparse.ArgumentParser(
        description="Generate .customskin cache files (Mojang-signed textures via MineSkin) for skin PNGs.",
        epilog="The .customskin files are local caches (gitignored); do not commit them.",
    )
    parser.add_argument("path", type=Path, help="a .png file or a directory of .png files")
    parser.add_argument("--upload", action="store_true",
                        help="really upload PNGs to MineSkin (third-party service); without it nothing is sent "
                             "and the script only lists the PNGs that need a cache (dry run)")
    parser.add_argument("--force", action="store_true", help="overwrite existing .customskin files")
    parser.add_argument("--recursive", action="store_true", help="also scan sub-directories (except reference/)")
    parser.add_argument("--catalogue", type=Path, default=DEFAULT_CATALOGUE,
                        help="catalogue.json used to look up each skin's model (default: server/skins/catalogue.json)")
    parser.add_argument("--variant", choices=["auto", "classic", "slim"], default="auto",
                        help="force a variant (default: catalogue, else detected from the PNG)")
    parser.add_argument("--api-key", default=os.environ.get("MINESKIN_API_KEY"),
                        help="MineSkin API key (default: $MINESKIN_API_KEY)")
    parser.add_argument("--visibility", choices=["public", "unlisted", "private"], default="unlisted")
    parser.add_argument("--limit", type=int, default=0, help="upload at most N skins (0 = no limit)")
    parser.add_argument("--max-minutes", type=float, default=0, help="stop starting uploads after N minutes")
    parser.add_argument("--max-wait", type=float, default=4000, help="max seconds to wait for one skin (rate limits)")
    parser.add_argument("--min-delay", type=float, default=6.5, help="minimum seconds between uploads")
    parser.add_argument("--verify", action="store_true",
                        help="download the signed texture from textures.minecraft.net and compare pixels")
    parser.add_argument("--reverify", action="store_true",
                        help="no uploads: re-run --verify for existing caches that are not verified yet")
    parser.add_argument("--legacy-fallback", action="store_true", help="fall back to the legacy /generate/upload endpoint")
    args = parser.parse_args()

    png_files = collect_pngs(args.path, args.recursive)
    if not png_files:
        print(f"No .png files found in {args.path}")
        return 0
    models = load_catalogue_models(args.catalogue)

    if args.reverify:
        done = bad = 0
        for png in png_files:
            cp = cache_path_for(png)
            if not cache_is_fresh(png):
                continue
            data = json.loads(cp.read_text(encoding="utf-8"))
            if data.get("verified_pixels") is True:
                continue
            try:
                problem = verify_texture(png, data["value"])
            except Exception as e:  # noqa: BLE001
                print(f"  {png.name}: verify failed: {e}")
                bad += 1
                continue
            data["verified_pixels"] = problem == ""
            cp.write_text(json.dumps(data), encoding="utf-8")
            print(f"  {png.name}: {'identical' if not problem else problem}")
            done += 1
        print(f"re-verified {done} cache(s), {bad} failed")
        return 1 if bad else 0

    if not args.force:
        needed = [p for p in png_files if not cache_is_fresh(p)]
        if not needed:
            print(f"All {len(png_files)} skin(s) already have up-to-date .customskin files. Use --force to regenerate.")
            return 0
        print(f"Generating cache for {len(needed)} of {len(png_files)} skin(s)...")
        png_files = needed
    else:
        print(f"Generating cache for {len(png_files)} skin(s) (force mode)...")
    if args.limit > 0:
        png_files = png_files[: args.limit]

    if not args.upload:
        print(f"Dry run: {len(png_files)} PNG(s) would be uploaded to MineSkin:")
        for png in png_files[:50]:
            print(f"  {png}")
        if len(png_files) > 50:
            print(f"  ... and {len(png_files) - 50} more")
        print("Nothing was uploaded. Pass --upload to send them (not needed for the local skin mode, see the docstring).")
        return 0

    gate = RateGate(args.min_delay if not args.api_key else 0.5)
    t0 = time.time()
    success = failed = 0
    for png in png_files:
        if args.max_minutes and time.time() - t0 > args.max_minutes * 60:
            print("Time budget exhausted; stopping (re-run to resume).")
            break
        variant = args.variant if args.variant != "auto" else models.get(png.name.lower()) or detect_variant(png)
        if generate_customskin(png, args.force, variant, args, gate):
            success += 1
        else:
            failed += 1
    print(f"\nDone: {success} succeeded, {failed} failed, {len(png_files) - success - failed} not attempted.")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
