#!/usr/bin/env python3
"""Render a reference sheet per skin: front / back / left / right orthographic views of the
3D player model in a T-pose (both layers, slim arms for slim skins, the client's
PlayerModel geometry and UV mapping), plus the raw 64x64 texture, on a plain background.

Usage:
    python tools/skins/render_reference.py                      # every skin in the catalogue
    python tools/skins/render_reference.py --skin proc_0007 --skin default_alex_slim
    python tools/skins/render_reference.py --png my.png --model slim --out-file my_ref.png

Outputs (catalogue mode):
    server/skins/reference/<skin_id>.png        reference sheet (RGB, see layout below)
    server/skins/reference/<skin_id>_skin.png   copy of the raw skin texture (RGBA 64x64)
    server/skins/reference/<skin_id>_views.json view boxes inside the sheet (pixel rects)

Sheet layout (default scale 8 px per model pixel): 5 panels of 288x304 px left to right:
front, back, left (camera on the character's left), right, raw texture (4x, on a
checkerboard so transparent overlay pixels are visible). A 24 px caption strip on top
names the panels; background RGB (190,190,190); every view has a 2 px RGB (120,120,120)
silhouette outline so clothing close to the background colour keeps its contour.
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter

sys.path.insert(0, str(Path(__file__).resolve().parent))
import skinlib  # noqa: E402

REPO = Path(__file__).resolve().parents[2]
SKINS = REPO / "server" / "skins"
BG = (190, 190, 190)
OUTLINE = (120, 120, 120)
PANEL_W, PANEL_H, CAPTION = 288, 304, 24
VIEW_ORDER = ["front", "back", "left", "right"]


def outlined(im: Image.Image) -> Image.Image:
    """Composite a transparent render onto BG with a 2 px darker silhouette outline, so
    clothing close to the background colour keeps a visible contour."""
    alpha = im.getchannel("A").point(lambda a: 255 if a > 0 else 0)
    ring = alpha.filter(ImageFilter.MaxFilter(5))
    out = Image.new("RGB", im.size, BG)
    out.paste(OUTLINE, mask=ring)
    out.paste(im.convert("RGB"), mask=alpha)
    return out


def render_sheet(skin: Image.Image, model: str, title: str = "", scale: int = 8):
    skin = skin.convert("RGBA")
    n = len(VIEW_ORDER) + 1
    sheet = Image.new("RGB", (PANEL_W * n, PANEL_H + CAPTION), BG)
    dr = ImageDraw.Draw(sheet)
    boxes = {}
    for i, v in enumerate(VIEW_ORDER):
        im = skinlib.render_view(skin, model, v, scale=scale, size=(PANEL_W, PANEL_H), bg=(0, 0, 0, 0))
        sheet.paste(outlined(im), (i * PANEL_W, CAPTION))
        dr.text((i * PANEL_W + 6, 6), v, fill=(20, 20, 20))
        boxes[v] = [i * PANEL_W, CAPTION, PANEL_W, PANEL_H]
    # raw texture on a checkerboard
    x0 = len(VIEW_ORDER) * PANEL_W
    tex = skin.resize((256, 256), Image.NEAREST)
    chk = Image.new("RGBA", (256, 256))
    cd = ImageDraw.Draw(chk)
    for yy in range(0, 256, 16):
        for xx in range(0, 256, 16):
            cd.rectangle([xx, yy, xx + 15, yy + 15],
                         fill=(225, 225, 225, 255) if (xx + yy) // 16 % 2 == 0 else (245, 245, 245, 255))
    chk.alpha_composite(tex)
    ox, oy = x0 + (PANEL_W - 256) // 2, CAPTION + (PANEL_H - 256) // 2
    sheet.paste(chk.convert("RGB"), (ox, oy))
    dr.text((x0 + 6, 6), f"texture ({model})", fill=(20, 20, 20))
    boxes["texture"] = [ox, oy, 256, 256]
    if title:
        tw = dr.textlength(title)
        dr.text((sheet.width - tw - 8, 6), title, fill=(20, 20, 20))
    return sheet, boxes


def render_catalogue_skin(entry: dict, out_dir: Path, skins_dir: Path = SKINS, force: bool = False) -> Path:
    png = skins_dir / entry["file"]
    out = out_dir / f"{entry['skin_id']}.png"
    raw = out_dir / f"{entry['skin_id']}_skin.png"
    meta = out_dir / f"{entry['skin_id']}_views.json"
    if not force and out.exists() and raw.exists() and meta.exists() and \
            out.stat().st_mtime >= png.stat().st_mtime:
        return out
    sheet, boxes = render_sheet(Image.open(png), entry["model"], f"{entry['skin_id']}  {entry['model']}")
    out_dir.mkdir(parents=True, exist_ok=True)
    sheet.save(out, optimize=True)
    shutil.copyfile(png, raw)
    meta.write_text(json.dumps({"skin_id": entry["skin_id"], "model": entry["model"], "pose": "tpose",
                                "projection": "orthographic", "scale_px_per_texel": 8,
                                "background_rgb": list(BG), "panels": boxes,
                                "skin_sha256": entry.get("sha256")}, indent=1), encoding="utf-8")
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--catalogue", type=Path, default=SKINS / "catalogue.json")
    ap.add_argument("--out", type=Path, default=SKINS / "reference")
    ap.add_argument("--skin", action="append", default=[], help="only these skin_ids (repeatable)")
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--png", type=Path, help="render a single PNG instead of the catalogue")
    ap.add_argument("--model", choices=["classic", "slim", "auto"], default="auto")
    ap.add_argument("--out-file", type=Path)
    a = ap.parse_args()
    if a.png:
        img = Image.open(a.png)
        model = skinlib.detect_model(img) if a.model == "auto" else a.model
        sheet, _ = render_sheet(img, model, f"{a.png.stem}  {model}")
        dst = a.out_file or a.png.with_name(a.png.stem + "_reference.png")
        sheet.save(dst)
        print(dst)
        return 0
    cat = json.loads(a.catalogue.read_text(encoding="utf-8"))
    n = 0
    for e in cat["skins"]:
        if a.skin and e["skin_id"] not in a.skin:
            continue
        render_catalogue_skin(e, a.out, a.catalogue.parent, a.force)
        n += 1
    print(f"rendered {n} reference sheet(s) -> {a.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
