#!/usr/bin/env python3
"""Procedural Minecraft character-skin generator (64x64, classic + slim), deterministic by seed.

Every skin is drawn from a per-skin integer seed (`base_seed * 100003 + index`), so the same
(base_seed, index) always yields the same PNG bytes (given the same Pillow version).
A character is composed of: skin tone, face (eyes / brows / mouth / blush / freckles),
hair style + colour (both layers, long hair continues on the back), optional beard, top
(t-shirt, long-sleeve, tank, button shirt, hoodie, sweater, dress, overalls, ...), outer
layer (open jacket, vest, cardigan, scarf, backpack), bottom (jeans, chinos, shorts,
skirts, leggings), shoes / boots / sandals, and optional hat / glasses / headphones /
gloves / belt / necklace / watch. Base layer is always fully opaque; the outer layer is
used for hair volume, hats, glasses, jackets, sleeves, skirts and accessories.

Usage:
    python tools/skins/generate_skins.py [--count 300] [--seed 20251023] [--out server/skins/pool]

Writes <out>/proc_NNNN.png and prints one JSON line per skin (skin_id, model, seed, tags).
The full catalogue is written by tools/skins/build_pool.py.
"""
from __future__ import annotations

import argparse
import colorsys
import json
import random
import sys
from pathlib import Path

from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parent))
import skinlib  # noqa: E402

REPO = Path(__file__).resolve().parents[2]
DEFAULT_OUT = REPO / "server" / "skins" / "pool"
DEFAULT_SEED = 20251023
SEED_STRIDE = 100003

# ------------------------------------------------------------------------------------------
# Colour helpers
# ------------------------------------------------------------------------------------------


def clamp(v):
    return max(0, min(255, int(round(v))))


def shade(c, f):
    """Multiply brightness (f<1 darker, f>1 lighter, lighter blends towards white)."""
    r, g, b = c[:3]
    if f <= 1.0:
        return (clamp(r * f), clamp(g * f), clamp(b * f))
    t = f - 1.0
    return (clamp(r + (255 - r) * t), clamp(g + (255 - g) * t), clamp(b + (255 - b) * t))


def mix(a, b, t):
    return tuple(clamp(a[i] * (1 - t) + b[i] * t) for i in range(3))


def hsv(h, s, v):
    r, g, b = colorsys.hsv_to_rgb(h % 1.0, max(0, min(1, s)), max(0, min(1, v)))
    return (clamp(r * 255), clamp(g * 255), clamp(b * 255))


SKIN_TONES = [  # (name, rgb) from very light to very dark
    ("porcelain", (250, 224, 205)), ("light", (241, 205, 176)), ("light_warm", (236, 196, 160)),
    ("fair_olive", (224, 189, 150)), ("medium_light", (214, 170, 128)), ("medium", (198, 150, 108)),
    ("olive", (185, 145, 100)), ("tan", (170, 122, 82)), ("brown", (141, 95, 62)),
    ("dark_brown", (112, 72, 46)), ("deep", (86, 55, 37)), ("very_deep", (66, 42, 30)),
]

HAIR_COLOURS = [
    ("black", (28, 24, 24), 14), ("dark_brown", (58, 38, 26), 16), ("brown", (96, 64, 40), 14),
    ("light_brown", (140, 100, 64), 8), ("auburn", (122, 52, 30), 5), ("ginger", (196, 98, 42), 5),
    ("blonde", (222, 186, 110), 8), ("platinum", (236, 226, 196), 3), ("grey", (150, 148, 146), 5),
    ("white", (232, 230, 226), 3), ("blue", (52, 92, 196), 2), ("pink", (232, 118, 168), 2),
    ("green", (60, 150, 90), 1), ("purple", (122, 66, 168), 2), ("teal", (40, 150, 150), 1),
]

EYE_COLOURS = [("brown", (92, 58, 34)), ("dark_brown", (52, 34, 22)), ("blue", (58, 104, 190)),
               ("light_blue", (110, 160, 220)), ("green", (64, 132, 72)), ("hazel", (128, 100, 52)),
               ("grey", (110, 118, 124)), ("amber", (176, 118, 40))]

NEUTRALS = [("black", (34, 34, 38)), ("charcoal", (62, 64, 70)), ("grey", (128, 130, 134)),
            ("light_grey", (190, 190, 192)), ("white", (236, 236, 232)), ("cream", (232, 222, 196)),
            ("beige", (204, 184, 150)), ("khaki", (176, 160, 112)), ("navy", (36, 46, 88)),
            ("olive", (98, 104, 58)), ("brown", (110, 74, 48)), ("burgundy", (110, 32, 46)),
            ("forest", (40, 84, 54)), ("tan", (178, 136, 92)), ("slate", (84, 100, 120))]
DENIMS = [("denim", (60, 86, 140)), ("dark_denim", (36, 50, 86)), ("light_denim", (112, 140, 184)),
          ("black_denim", (44, 46, 54)), ("grey_denim", (104, 110, 124))]


def weighted(rng, items, wkey=2):
    tot = sum(it[wkey] for it in items)
    x = rng.random() * tot
    for it in items:
        x -= it[wkey]
        if x <= 0:
            return it
    return items[-1]


def garment_colour(rng, style=None):
    """Returns (name, rgb)."""
    style = style or rng.choices(["neutral", "bright", "pastel", "muted", "dark"], [38, 20, 12, 20, 10])[0]
    if style == "neutral":
        return rng.choice(NEUTRALS)
    h = rng.random()
    if style == "bright":
        return ("bright", hsv(h, rng.uniform(0.55, 0.9), rng.uniform(0.6, 0.92)))
    if style == "pastel":
        return ("pastel", hsv(h, rng.uniform(0.18, 0.38), rng.uniform(0.85, 0.97)))
    if style == "dark":
        return ("dark", hsv(h, rng.uniform(0.3, 0.7), rng.uniform(0.18, 0.38)))
    return ("muted", hsv(h, rng.uniform(0.25, 0.5), rng.uniform(0.4, 0.7)))


# ------------------------------------------------------------------------------------------
# Canvas with face/strip addressing
# ------------------------------------------------------------------------------------------

class Canvas:
    def __init__(self, model, rng):
        self.model = model
        self.rng = rng
        self.img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
        self.px = self.img.load()
        self.parts = skinlib.parts_for(model)
        self.noise = 0.05

    # raw pixel with fabric noise
    def put(self, x, y, c, noise=None, alpha=255):
        n = self.noise if noise is None else noise
        if n:
            f = 1.0 + self.rng.uniform(-n, n)
            c = shade(c, f) if f <= 1 else shade(c, 1 + (f - 1) * 0.6)
        self.px[x, y] = (c[0], c[1], c[2], alpha)

    def clear(self, x, y):
        self.px[x, y] = (0, 0, 0, 0)

    def get(self, x, y):
        return self.px[x, y]

    def face(self, part, name):
        return self.parts[part].faces()[name]

    def fill_face(self, part, name, c, noise=None):
        x0, y0, w, h = self.face(part, name)
        for y in range(h):
            for x in range(w):
                self.put(x0 + x, y0 + y, c, noise)

    def fill_part(self, part, c, noise=None):
        for n in ("top", "bottom", "right", "front", "left", "back"):
            self.fill_face(part, n, c, noise)

    def fput(self, part, name, x, y, c, noise=None, alpha=255):
        """Face-local put; ignores out-of-range coordinates."""
        x0, y0, w, h = self.face(part, name)
        if 0 <= x < w and 0 <= y < h:
            self.put(x0 + x, y0 + y, c, noise, alpha)

    def fclear(self, part, name, x, y):
        x0, y0, w, h = self.face(part, name)
        if 0 <= x < w and 0 <= y < h:
            self.clear(x0 + x, y0 + y)

    def wrap_row(self, part, row, c, noise=None, fn=None):
        """Paint row `row` all the way round the part (right|front|left|back).
        fn(col, row) -> colour overrides c (col is the wrap column)."""
        x0, y0, w, h = self.parts[part].wrap
        if not 0 <= row < h:
            return
        for col in range(w):
            cc = fn(col, row) if fn else c
            if cc is not None:
                self.put(x0 + col, y0 + row, cc, noise)

    def wrap_rows(self, part, rows, c, noise=None, fn=None):
        for r in rows:
            self.wrap_row(part, r, c, noise, fn)

    def wrap_clear_rows(self, part, rows):
        x0, y0, w, h = self.parts[part].wrap
        for r in rows:
            if 0 <= r < h:
                for col in range(w):
                    self.clear(x0 + col, y0 + r)

    def wrap_face_of(self, part, col):
        """Which side face a wrap column belongs to + face-local x."""
        p = self.parts[part]
        d, w = p.d, p.w
        if col < d:
            return "right", col
        if col < d + w:
            return "front", col - d
        if col < 2 * d + w:
            return "left", col - d - w
        return "back", col - 2 * d - w


# ------------------------------------------------------------------------------------------
# Patterns
# ------------------------------------------------------------------------------------------

def make_pattern(rng, base, accent):
    kind = rng.choices(["solid", "hstripe", "vstripe", "check", "twotone", "raglan", "gradient", "dots"],
                       [44, 12, 5, 7, 8, 8, 8, 4])[0]
    period = rng.choice([2, 3, 4])
    split = rng.randint(4, 8)

    def fn(col, row):
        if kind == "hstripe":
            return accent if row % period == 0 else base
        if kind == "vstripe":
            return accent if col % period == 0 else base
        if kind == "check":
            return accent if ((col // 2) + (row // 2)) % 2 == 0 else base
        if kind == "twotone":
            return accent if row >= split else base
        if kind == "gradient":
            return mix(base, accent, min(1.0, row / 12.0))
        if kind == "dots":
            return accent if (col % 3 == 1 and row % 3 == 1) else base
        return base
    return kind, fn


# ------------------------------------------------------------------------------------------
# Character composition
# ------------------------------------------------------------------------------------------

def generate(seed: int, model: str = None):
    """Returns (PIL.Image RGBA 64x64, model, tags list, attrs dict)."""
    rng = random.Random(seed)
    if model is None:
        model = "slim" if rng.random() < 0.45 else "classic"
    cv = Canvas(model, rng)
    tags = [f"model:{model}"]
    attrs = {"model": model}

    # ---- skin ------------------------------------------------------------------------
    tone_name, tone = rng.choice(SKIN_TONES)
    tone = tuple(clamp(c * rng.uniform(0.96, 1.04)) for c in tone)
    tone_dark = shade(tone, 0.86)
    tags.append(f"skin_tone:{tone_name}")
    attrs["skin_tone"] = tone_name
    cv.noise = 0.025
    for p in ("head", "body", "right_arm", "left_arm", "right_leg", "left_leg"):
        cv.fill_part(p, tone)
    cv.noise = 0.05

    # ---- face (head front, 8x8) ------------------------------------------------------
    eye_row = rng.choice([3, 4, 4, 4])
    eye_name, eye_col = rng.choice(EYE_COLOURS)
    eye_style = rng.choices(["white_iris", "iris_only", "tall", "lashes"], [50, 18, 14, 18])[0]
    iris_inner = rng.random() < 0.75
    tags.append(f"eyes:{eye_name}")
    white = (240, 240, 236)
    for side in (0, 1):
        xs = (1, 2) if side == 0 else (6, 5)  # (outer, inner)
        outer, inner = xs
        if eye_style == "iris_only":
            cv.fput("head", "front", inner if iris_inner else outer, eye_row, eye_col, noise=0)
        else:
            cv.fput("head", "front", outer, eye_row, white if iris_inner else eye_col, noise=0)
            cv.fput("head", "front", inner, eye_row, eye_col if iris_inner else white, noise=0)
            if eye_style == "tall":
                cv.fput("head", "front", inner if iris_inner else outer, eye_row - 1, shade(eye_col, 0.7), noise=0)
                cv.fput("head", "front", outer if iris_inner else inner, eye_row - 1, white, noise=0)
            if eye_style == "lashes":
                cv.fput("head", "front", outer, eye_row - 1, (30, 26, 26), noise=0)
    # nose shading + mouth
    if rng.random() < 0.6:
        cv.fput("head", "front", 3, eye_row + 1, tone_dark, noise=0)
        cv.fput("head", "front", 4, eye_row + 1, tone_dark, noise=0)
    mouth = rng.choices(["none", "line", "lips", "smile", "open"], [18, 30, 22, 20, 10])[0]
    mrow = min(7, eye_row + 2 + (1 if rng.random() < 0.4 else 0))
    lip = mix(tone, (170, 70, 80), rng.uniform(0.25, 0.55)) if rng.random() < 0.5 else shade(tone, 0.7)
    if mouth == "line":
        for x in (3, 4):
            cv.fput("head", "front", x, mrow, shade(tone, 0.68), noise=0)
    elif mouth == "lips":
        for x in (3, 4):
            cv.fput("head", "front", x, mrow, lip, noise=0)
    elif mouth == "smile":
        for x in (3, 4):
            cv.fput("head", "front", x, mrow, shade(tone, 0.62), noise=0)
        cv.fput("head", "front", 2, mrow - 1 if mrow > eye_row + 1 else mrow, shade(tone, 0.72), noise=0)
        cv.fput("head", "front", 5, mrow - 1 if mrow > eye_row + 1 else mrow, shade(tone, 0.72), noise=0)
    elif mouth == "open":
        for x in (3, 4):
            cv.fput("head", "front", x, mrow, (90, 30, 34), noise=0)
        if mrow < 7:
            cv.fput("head", "front", 3, mrow + 1, lip, noise=0)
            cv.fput("head", "front", 4, mrow + 1, lip, noise=0)
    if rng.random() < 0.22:  # blush
        bc = mix(tone, (230, 110, 120), 0.35)
        cv.fput("head", "front", 1, eye_row + 1, bc, noise=0)
        cv.fput("head", "front", 6, eye_row + 1, bc, noise=0)
        tags.append("blush")
    if rng.random() < 0.1:  # freckles
        fc = shade(tone, 0.78)
        for x in (1, 2, 5, 6):
            if rng.random() < 0.6:
                cv.fput("head", "front", x, eye_row + 1, fc, noise=0)
        tags.append("freckles")
    # ears hint on the side faces
    if rng.random() < 0.5:
        cv.fput("head", "right", 4, eye_row, tone_dark, noise=0)
        cv.fput("head", "left", 3, eye_row, tone_dark, noise=0)

    # ---- hair ------------------------------------------------------------------------
    hair_name, hair, _ = weighted(rng, HAIR_COLOURS)
    if rng.random() < 0.06 and hair_name not in ("grey", "white"):
        hair_name = hair_name + "_tips"
    hair = tuple(clamp(c * rng.uniform(0.92, 1.08)) for c in hair)
    hair_hi, hair_lo = shade(hair, 1.18), shade(hair, 0.78)
    tip_col = hsv(rng.random(), 0.65, 0.85)
    attrs["hair_colour"] = hair_name
    long_bias = 0.55 if model == "slim" else 0.18
    styles = ["bald", "buzz", "short", "side_part", "fringe", "spiky", "mohawk", "curly", "afro",
              "bob", "long", "ponytail", "bun", "braids", "pigtails"]
    weights = [3, 7, 22, 14, 10, 5, 2, 7, 5,
               10 * long_bias * 2, 22 * long_bias * 2, 12 * long_bias * 2, 7 * long_bias * 2,
               5 * long_bias * 2, 3 * long_bias * 2]
    hstyle = rng.choices(styles, weights)[0]
    tags += [f"hair:{hstyle}", f"hair_colour:{hair_name}"]
    attrs["hair"] = hstyle

    def hair_px(part, face, x, y, alpha=255):
        r = rng.random()
        c = hair_hi if r < 0.15 else hair_lo if r < 0.32 else hair
        if hair_name.endswith("_tips") and face in ("front", "right", "left", "back") and y >= 5:
            c = mix(c, tip_col, 0.7)
        cv.fput(part, face, x, y, c, noise=0.04, alpha=alpha)

    def hair_rows(face, rows, cols=range(8), part="head"):
        for y in rows:
            for x in cols:
                hair_px(part, face, x, y)

    if hstyle != "bald":
        hair_rows("top", range(8))
    if hstyle == "buzz":
        hair_rows("back", range(0, 5))
        hair_rows("right", range(0, 2))
        hair_rows("left", range(0, 2))
        hair_rows("front", range(0, 1))
    elif hstyle in ("short", "side_part", "fringe", "spiky", "curly"):
        back_len = rng.randint(4, 6)
        hair_rows("back", range(back_len))
        side_len = rng.randint(2, 4)
        hair_rows("right", range(side_len))
        hair_rows("left", range(side_len))
        # sideburns: column next to the face front
        sb = rng.randint(0, 2)
        for y in range(side_len, side_len + sb):
            hair_px("head", "right", 7, y)
            hair_px("head", "left", 0, y)
        front_rows = 1 if eye_row == 3 else rng.choice([1, 2, 2])
        hair_rows("front", range(front_rows))
        if hstyle == "side_part":
            part_side = rng.choice([0, 1])
            cols = range(0, 5) if part_side == 0 else range(3, 8)
            hair_rows("front", range(front_rows, front_rows + 1), cols)
        elif hstyle == "fringe":
            for x in range(8):
                if rng.random() < 0.7:
                    hair_rows("front", range(front_rows, min(eye_row, front_rows + 1)), [x])
        elif hstyle == "spiky":
            for x in range(0, 8, 2):
                cv.fput("hat", "top", x, rng.randint(0, 7), hair_hi, noise=0.04)
                cv.fput("hat", "front", x, 0, hair, noise=0.04)
            for x in range(8):
                if rng.random() < 0.5:
                    cv.fput("hat", "back", x, 0, hair, noise=0.04)
        elif hstyle == "curly":
            for face in ("top", "front", "back", "right", "left"):
                for x in range(8):
                    for y in range(8 if face == "top" else 2):
                        if face == "front" and y > 0:
                            continue
                        if rng.random() < 0.55:
                            cv.fput("hat", face, x, y, hair_hi if rng.random() < 0.3 else hair, noise=0.05)
    elif hstyle == "mohawk":
        hair_rows("top", range(8))
        # shaved sides: lighten top edges, the crest in the outer layer
        cv.fill_face("head", "top", shade(tone, 0.85), noise=0.03)
        for y in range(8):
            for x in (3, 4):
                hair_px("head", "top", x, y)
                cv.fput("hat", "top", x, y, hair_hi, noise=0.04)
        for x in (3, 4):
            hair_px("head", "front", x, 0)
            hair_px("head", "back", x, 0)
            hair_px("head", "back", x, 1)
            cv.fput("hat", "front", x, 0, hair, noise=0.04)
            cv.fput("hat", "back", x, 0, hair, noise=0.04)
    elif hstyle == "afro":
        hair_rows("back", range(8))
        hair_rows("right", range(6))
        hair_rows("left", range(6))
        hair_rows("front", range(2))
        for face in ("top", "back", "right", "left", "front"):
            for x in range(8):
                for y in range(8):
                    if face == "front" and y > 1:
                        continue
                    if face in ("right", "left") and y > 5:
                        continue
                    if rng.random() < 0.85:
                        cv.fput("hat", face, x, y, hair_hi if rng.random() < 0.25 else hair, noise=0.06)
    else:  # bob, long, ponytail, bun, braids, pigtails
        hair_rows("back", range(8))
        side_cols = range(0, 8)
        hair_rows("right", range(8) if hstyle in ("bob", "long", "braids") else range(4), side_cols)
        hair_rows("left", range(8) if hstyle in ("bob", "long", "braids") else range(4), side_cols)
        if hstyle in ("bob", "long", "braids"):
            # keep the ear-front column of the side faces partially skin
            for y in range(eye_row + 1, 8):
                cv.fput("head", "right", 7, y, tone)
                cv.fput("head", "left", 0, y, tone)
        front_rows = 1 if eye_row == 3 else 2
        hair_rows("front", range(front_rows))
        if rng.random() < 0.5:  # fringe / side sweep
            for x in range(8):
                if rng.random() < 0.55:
                    hair_rows("front", [front_rows], [x])
        # framing strands down the face edges
        if hstyle in ("long", "bob", "braids") and rng.random() < 0.7:
            for y in range(front_rows, 8):
                hair_px("head", "front", 0, y)
                hair_px("head", "front", 7, y)
        if hstyle in ("long", "bob"):
            # volume on the outer layer
            for face in ("back", "right", "left"):
                for x in range(8):
                    for y in range(8):
                        if face != "back" and ((face == "right" and x >= 6) or (face == "left" and x <= 1)) and y >= eye_row:
                            continue
                        if rng.random() < 0.6:
                            cv.fput("hat", face, x, y, hair_hi if rng.random() < 0.3 else hair, noise=0.05)
        if hstyle == "long":
            length = rng.randint(3, 9)
            for y in range(length):
                for x in range(8):
                    if y < length - 1 or rng.random() < 0.6:
                        cv.fput("jacket", "back", x, y, hair if rng.random() > 0.2 else hair_lo, noise=0.05)
            tags.append("hair_on_back")
        elif hstyle == "ponytail":
            for y in range(3, 8):
                for x in (3, 4):
                    cv.fput("hat", "back", x, y, hair_hi, noise=0.04)
            for y in range(0, rng.randint(4, 8)):
                for x in (3, 4):
                    cv.fput("jacket", "back", x, y, hair, noise=0.05)
            cv.fput("hat", "back", 3, 2, garment_colour(rng, "bright")[1], noise=0)
            cv.fput("hat", "back", 4, 2, garment_colour(rng, "bright")[1], noise=0)
        elif hstyle == "bun":
            for x in range(2, 6):
                for y in range(1, 4):
                    cv.fput("hat", "back", x, y, hair_hi if (x + y) % 3 == 0 else hair, noise=0.04)
            for x in range(2, 6):
                for y in range(5, 8):
                    cv.fput("hat", "top", x, y, hair, noise=0.04)
        elif hstyle == "braids":
            for y in range(0, rng.randint(5, 10)):
                for x in (1, 6):
                    cv.fput("jacket", "back", x, y, hair_lo if y % 2 else hair, noise=0.03)
                cv.fput("jacket", "front", 0, y, hair_lo if y % 2 else hair, noise=0.03)
                cv.fput("jacket", "front", 7, y, hair_lo if y % 2 else hair, noise=0.03)
        elif hstyle == "pigtails":
            for y in range(4, 8):
                cv.fput("hat", "right", 4, y, hair_hi, noise=0.04)
                cv.fput("hat", "left", 3, y, hair_hi, noise=0.04)
                cv.fput("hat", "right", 5, y, hair, noise=0.04)
                cv.fput("hat", "left", 2, y, hair, noise=0.04)
    # eyebrows
    if hstyle != "bald" and rng.random() < 0.65 and eye_row >= 3:
        brow = shade(hair if hstyle != "bald" else tone, 0.8)
        for x in ((1, 2, 5, 6) if rng.random() < 0.6 else (2, 5)):
            cv.fput("head", "front", x, eye_row - 1, brow, noise=0)
        tags.append("eyebrows")

    # ---- beard -----------------------------------------------------------------------
    beard = None
    if rng.random() < (0.26 if model == "classic" else 0.04):
        beard = rng.choice(["stubble", "moustache", "goatee", "full", "full"])
        tags.append(f"beard:{beard}")
        attrs["beard"] = beard
        bc = shade(hair, 0.95) if hair_name not in ("pink", "blue", "green", "purple", "teal") else (60, 40, 30)
        if beard == "stubble":
            for y in range(eye_row + 2, 8):
                for x in range(1, 7):
                    if rng.random() < 0.45 and cv.get(*(_fxy(cv, "head", "front", x, y)))[:3] != (240, 240, 236):
                        cv.fput("head", "front", x, y, mix(tone, bc, 0.45), noise=0)
        else:
            if beard in ("moustache", "full"):
                for x in range(2, 6):
                    cv.fput("head", "front", x, min(7, eye_row + 2), bc, noise=0.05)
            if beard in ("goatee",):
                for y in range(min(7, eye_row + 3), 8):
                    for x in (3, 4):
                        cv.fput("head", "front", x, y, bc, noise=0.05)
            if beard == "full":
                for y in range(eye_row + 2, 8):
                    for x in range(0, 8):
                        if y == min(7, eye_row + 3) and x in (3, 4):
                            continue  # mouth gap
                        cv.fput("head", "front", x, y, bc, noise=0.05)
                for y in range(eye_row + 1, 8):
                    for x in range(5, 8):
                        cv.fput("head", "right", x, y, bc, noise=0.05)
                    for x in range(0, 3):
                        cv.fput("head", "left", x, y, bc, noise=0.05)
                for y in range(0, 3):
                    for x in range(8):
                        cv.fput("head", "bottom", x, y, bc, noise=0.05)

    # ---- top garment -----------------------------------------------------------------
    top_kinds = ["tshirt", "longsleeve", "tank", "buttonshirt", "hoodie", "sweater", "polo", "dress",
                 "overalls", "uniform"]
    top_w = [22, 14, 6, 10, 12, 10, 6, 12 if model == "slim" else 2, 4, 4]
    top = rng.choices(top_kinds, top_w)[0]
    tname, tcol = garment_colour(rng)
    aname, acol = garment_colour(rng)
    pkind, pfn = make_pattern(rng, tcol, acol)
    if top in ("buttonshirt", "uniform", "polo") and pkind in ("dots", "gradient"):
        pkind, pfn = "solid", (lambda c, r: tcol)
    tags += [f"top:{top}", f"top_colour:{tname}", f"top_pattern:{pkind}"]
    attrs.update(top=top, top_colour=tname, top_pattern=pkind)

    def body_fn(col, row):
        return shade(pfn(col, row), 0.92 if row == 11 else 1.0)

    cv.wrap_rows("body", range(12), tcol, fn=body_fn)
    cv.fill_face("body", "top", tcol)
    cv.fill_face("body", "bottom", tcol)
    # sleeves
    sleeve = {"tshirt": rng.choice([3, 4, 5]), "polo": 4, "tank": 0, "dress": rng.choice([0, 3, 12]),
              "overalls": rng.choice([4, 12]), "uniform": 12}.get(top, 12)
    for arm in ("right_arm", "left_arm"):
        if sleeve > 0:
            cv.fill_face(arm, "top", tcol)
            cv.wrap_rows(arm, range(sleeve), tcol, fn=lambda c, r: pfn(c, r))
            if sleeve < 12:
                cv.wrap_row(arm, sleeve - 1, shade(tcol, 0.85))
            elif rng.random() < 0.5:  # cuff
                cv.wrap_row(arm, 11, shade(tcol, 0.8))
                if top in ("hoodie", "sweater"):
                    cv.wrap_row(arm, 11, shade(tcol, 0.75))
    if sleeve >= 12:
        for arm in ("right_arm", "left_arm"):
            cv.fill_face(arm, "bottom", tone)  # hand
            # hand on the last row for long sleeves (skin), sometimes
            if rng.random() < 0.5:
                cv.wrap_row(arm, 11, tone, noise=0.02)
    # neckline
    neck = rng.choice(["crew", "crew", "v", "scoop"]) if top in ("tshirt", "longsleeve", "sweater", "dress", "tank") else "none"
    if neck == "crew":
        for x in (3, 4):
            cv.fput("body", "front", x, 0, tone, noise=0.02)
    elif neck == "v":
        for x in (2, 3, 4, 5):
            cv.fput("body", "front", x, 0, tone, noise=0.02)
        for x in (3, 4):
            cv.fput("body", "front", x, 1, tone, noise=0.02)
    elif neck == "scoop":
        for x in range(1, 7):
            cv.fput("body", "front", x, 0, tone, noise=0.02)
        for x in range(2, 6):
            cv.fput("body", "front", x, 1, tone, noise=0.02)
    if top == "tank":
        for face, xs in (("front", (0, 1, 6, 7)), ("back", (0, 1, 6, 7))):
            for x in xs:
                cv.fput("body", face, x, 0, tone, noise=0.02)
        cv.fill_face("body", "top", tone)
        for x in (1, 6):
            cv.fput("body", "top", x, 1, tcol)
            cv.fput("body", "top", x, 2, tcol)
    if top in ("buttonshirt", "polo", "uniform"):
        btn = shade(tcol, 0.62) if sum(tcol) > 300 else shade(tcol, 1.45)
        for y in range(1, 12 if top != "polo" else 4, 2):
            cv.fput("body", "front", 3 if top != "uniform" else 4, y, btn, noise=0)
        collar = shade(tcol, 1.2) if top != "uniform" else shade(tcol, 0.8)
        for x in (1, 2, 5, 6):
            cv.fput("jacket", "front", x, 0, collar, noise=0.02)
        cv.fput("body", "front", 3, 0, tone)
        cv.fput("body", "front", 4, 0, tone)
        if top == "uniform":
            badge = garment_colour(rng, "bright")[1]
            cv.fput("body", "front", 6, 2, badge, noise=0)
            cv.fput("body", "front", 1, 2, (220, 200, 90), noise=0)
            for arm in ("right_arm", "left_arm"):
                cv.fput(arm, "right" if arm == "right_arm" else "left", 1, 2, badge, noise=0)
    if top == "hoodie":
        hood = shade(tcol, 0.88)
        for y in range(0, 3):
            for x in range(8):
                cv.fput("jacket", "back", x, y, hood, noise=0.04)
        for x in (0, 1, 6, 7):
            cv.fput("jacket", "front", x, 0, hood, noise=0.04)
        for side in ("right", "left"):
            for x in range(4):
                cv.fput("jacket", side, x, 0, hood, noise=0.04)
        # pocket + strings
        pk = shade(tcol, 0.85)
        for x in range(2, 6):
            cv.fput("body", "front", x, 7, pk)
            cv.fput("body", "front", x, 8, pk)
        sc = (230, 230, 230) if sum(tcol) < 400 else (40, 40, 40)
        cv.fput("body", "front", 3, 1, sc, noise=0)
        cv.fput("body", "front", 4, 1, sc, noise=0)
        cv.fput("body", "front", 3, 2, sc, noise=0)
        cv.fput("body", "front", 4, 3, sc, noise=0)
    if top in ("tshirt", "sweater", "longsleeve", "hoodie") and rng.random() < 0.35:
        # chest graphic
        gc = garment_colour(rng, rng.choice(["bright", "pastel", "neutral"]))[1]
        shape = rng.choice(["block", "heart", "stripe", "diamond", "letter"])
        tags.append(f"graphic:{shape}")
        if shape == "block":
            for x in range(3, 5 + rng.randint(0, 1)):
                for y in range(3, 6):
                    cv.fput("body", "front", x, y, gc, noise=0.02)
        elif shape == "heart":
            for (x, y) in ((2, 3), (3, 3), (5, 3), (4, 3), (2, 4), (3, 4), (4, 4), (5, 4), (3, 5), (4, 5)):
                if (x, y) in ((4, 3),):
                    continue
                cv.fput("body", "front", x, y, gc, noise=0)
        elif shape == "stripe":
            for x in range(8):
                cv.fput("body", "front", x, 4, gc, noise=0.02)
                cv.fput("body", "back", x, 4, gc, noise=0.02)
        elif shape == "diamond":
            for (x, y) in ((3, 3), (4, 3), (2, 4), (5, 4), (3, 5), (4, 5)):
                cv.fput("body", "front", x, y, gc, noise=0)
        else:
            for (x, y) in ((2, 3), (2, 4), (2, 5), (3, 5), (5, 3), (5, 4), (5, 5), (4, 3)):
                cv.fput("body", "front", x, y, gc, noise=0)

    # ---- bottoms ---------------------------------------------------------------------
    if top == "dress":
        bottom = rng.choice(["dress_short", "dress_long"])
        bcol = tcol
        bname = tname
    elif top == "overalls":
        bottom = "overalls"
        bname, bcol = rng.choice(DENIMS + [("brown", (110, 74, 48)), ("khaki", (176, 160, 112))])
    else:
        bottom = rng.choices(["jeans", "trousers", "shorts", "skirt", "leggings", "cargo", "joggers"],
                             [26, 20, 12, 12 if model == "slim" else 1, 8 if model == "slim" else 1, 8, 10])[0]
        if bottom == "jeans":
            bname, bcol = rng.choice(DENIMS)
        elif bottom in ("trousers", "cargo"):
            bname, bcol = rng.choice(NEUTRALS)
        else:
            bname, bcol = garment_colour(rng)
    tags += [f"bottom:{bottom}", f"bottom_colour:{bname}"]
    attrs.update(bottom=bottom, bottom_colour=bname)
    seam = shade(bcol, 0.82)
    if bottom in ("jeans", "trousers", "cargo", "joggers", "leggings", "overalls"):
        for leg in ("right_leg", "left_leg"):
            cv.fill_face(leg, "top", bcol)
            cv.wrap_rows(leg, range(12), bcol)
            # inner/outer seams for jeans
            if bottom in ("jeans", "overalls"):
                for y in range(12):
                    cv.fput(leg, "right", 0, y, seam, noise=0.02)
                    cv.fput(leg, "left", 3, y, seam, noise=0.02)
            if bottom == "cargo":
                for y in (5, 6, 7):
                    for x in (1, 2):
                        cv.fput(leg, "right" if leg == "right_leg" else "left", x, y, shade(bcol, 0.85))
            if bottom == "joggers":
                cv.wrap_row(leg, 10, shade(bcol, 0.8))
        # waist on the body
        waist_rows = range(10, 12) if top not in ("dress", "overalls") else range(0)
        cv.wrap_rows("body", waist_rows, bcol)
    elif bottom == "shorts":
        length = rng.randint(3, 6)
        for leg in ("right_leg", "left_leg"):
            cv.fill_face(leg, "top", bcol)
            cv.wrap_rows(leg, range(length), bcol)
            cv.wrap_row(leg, length - 1, shade(bcol, 0.88))
        cv.wrap_rows("body", range(10, 12), bcol)
    elif bottom in ("skirt", "dress_short", "dress_long"):
        length = {"skirt": rng.randint(4, 8), "dress_short": rng.randint(4, 7), "dress_long": rng.randint(8, 11)}[bottom]
        if bottom == "skirt":
            cv.wrap_rows("body", range(10, 12), bcol)
        skirt_p = pfn if bottom != "skirt" else (lambda c, r: bcol)
        for leg, pants in (("right_leg", "right_pants"), ("left_leg", "left_pants")):
            cv.fill_face(leg, "top", bcol)
            cv.wrap_rows(leg, range(length), bcol, fn=lambda c, r: skirt_p(c, r + 12))
            # outer layer flare on the lower part of the skirt
            cv.wrap_rows(pants, range(max(0, length - 3), length), bcol, fn=lambda c, r: skirt_p(c, r + 12))
            cv.wrap_row(pants, length - 1, shade(bcol, 0.82))
        # legwear below the skirt: bare / tights / socks
        legwear = rng.choice(["bare", "bare", "tights", "socks"])
        tags.append(f"legwear:{legwear}")
        if legwear == "tights":
            tcol2 = rng.choice([(30, 30, 34), (60, 50, 50), (200, 180, 160), garment_colour(rng, "dark")[1]])
            for leg in ("right_leg", "left_leg"):
                cv.wrap_rows(leg, range(length, 12), tcol2, noise=0.03)
        elif legwear == "socks":
            scol = rng.choice([(236, 236, 232), (30, 30, 34), garment_colour(rng, "pastel")[1]])
            for leg in ("right_leg", "left_leg"):
                cv.wrap_rows(leg, range(max(length, 7), 12), scol, noise=0.03)
    if bottom == "overalls":
        # bib + straps on the body
        for y in range(5, 12):
            for x in range(1, 7):
                cv.fput("body", "front", x, y, bcol)
        for y in range(0, 6):
            cv.fput("body", "front", 1, y, bcol)
            cv.fput("body", "front", 6, y, bcol)
            cv.fput("body", "back", 1, y, bcol)
            cv.fput("body", "back", 6, y, bcol)
        cv.fput("body", "front", 1, 5, (210, 190, 90), noise=0)
        cv.fput("body", "front", 6, 5, (210, 190, 90), noise=0)
        cv.wrap_rows("body", range(9, 12), bcol)

    # belt
    if bottom in ("jeans", "trousers", "cargo", "shorts", "skirt") and rng.random() < 0.55:
        belt = rng.choice([(40, 30, 24), (90, 60, 36), (30, 30, 30), (140, 100, 60)])
        cv.wrap_row("body", 10, belt, noise=0.02)
        cv.fput("body", "front", 3, 10, (200, 190, 120), noise=0)
        cv.fput("body", "front", 4, 10, (200, 190, 120), noise=0)
        tags.append("belt")

    # ---- shoes -----------------------------------------------------------------------
    shoe = rng.choices(["sneakers", "boots", "tall_boots", "dress_shoes", "sandals", "slip_ons"],
                       [34, 20, 8, 14, 6, 12])[0]
    sname, scol = rng.choice(NEUTRALS + [("bright", garment_colour(rng, "bright")[1])])
    if shoe == "dress_shoes":
        sname, scol = rng.choice([("black", (28, 28, 30)), ("brown", (92, 56, 34)), ("oxblood", (90, 30, 30))])
    tags += [f"shoes:{shoe}"]
    attrs["shoes"] = shoe
    sole = rng.choice([(236, 236, 232), (40, 40, 40), (120, 90, 60)]) if shoe == "sneakers" else shade(scol, 0.6)
    rows = {"sneakers": range(10, 12), "boots": range(8, 12), "tall_boots": range(5, 12),
            "dress_shoes": range(11, 12), "sandals": range(11, 12), "slip_ons": range(11, 12)}[shoe]
    for leg in ("right_leg", "left_leg"):
        if shoe == "sandals":
            cv.wrap_row(leg, 11, tone)
            for x in range(4):
                cv.fput(leg, "front", x, 11, scol if x in (1, 2) else tone)
            cv.fill_face(leg, "bottom", shade(scol, 0.7))
            continue
        cv.wrap_rows(leg, rows, scol)
        cv.wrap_row(leg, 11, sole if shoe in ("sneakers",) else shade(scol, 0.75))
        cv.fill_face(leg, "bottom", sole)
        if shoe == "sneakers":
            acc = rng.choice([(236, 236, 232), garment_colour(rng, "bright")[1]])
            cv.fput(leg, "front", 1, 10, acc, noise=0)
            cv.fput(leg, "front", 2, 10, acc, noise=0)
            if rng.random() < 0.5:
                cv.fput(leg, "right", 2, 10, acc, noise=0)
                cv.fput(leg, "left", 1, 10, acc, noise=0)
        if shoe == "tall_boots":
            cv.wrap_row("right_pants" if leg == "right_leg" else "left_pants", 5, shade(scol, 0.9))

    # ---- outer layer: jackets / vests / cardigans --------------------------------------
    outer = rng.choices(["none", "open_jacket", "closed_jacket", "vest", "cardigan", "coat", "lab_coat"],
                        [52, 14, 7, 8, 7, 8, 2])[0]
    if top in ("hoodie", "dress", "overalls", "uniform") and outer in ("closed_jacket",):
        outer = "none"
    if outer != "none":
        jname, jcol = garment_colour(rng, rng.choice(["neutral", "neutral", "muted", "dark", "bright"]))
        if outer == "lab_coat":
            jname, jcol = "white", (236, 238, 240)
        tags += [f"outer:{outer}", f"outer_colour:{jname}"]
        attrs.update(outer=outer, outer_colour=jname)
        length = {"coat": 12, "lab_coat": 12}.get(outer, rng.choice([10, 11, 12]))
        jdark = shade(jcol, 0.82)
        for y in range(length):
            for col in range(24):
                face, fx = cv.wrap_face_of("jacket", col)
                if face == "front" and outer in ("open_jacket", "cardigan", "coat", "lab_coat", "vest") and 2 <= fx <= 5:
                    continue  # open front shows the top underneath
                cv.put(cv.parts["jacket"].wrap[0] + col, cv.parts["jacket"].wrap[1] + y,
                       jdark if (face == "front" and fx in (1, 6) and outer != "vest") else jcol, noise=0.04)
        for x in range(8):
            for y in range(4):
                if outer in ("vest",) and 2 <= x <= 5:
                    continue
                cv.fput("jacket", "top", x, y, jcol, noise=0.04)
        if outer == "closed_jacket":
            for y in range(length):
                cv.fput("jacket", "front", 3, y, shade(jcol, 0.7), noise=0)  # zip
        if outer in ("coat", "lab_coat"):
            # coat continues over the legs (outer layer)
            for pants in ("right_pants", "left_pants"):
                cv.wrap_rows(pants, range(0, rng.randint(3, 7)), jcol, noise=0.04)
        if outer != "vest":
            sl_len = 12 if rng.random() < 0.85 else 6
            for sl in ("right_sleeve", "left_sleeve"):
                cv.fill_face(sl, "top", jcol, noise=0.04)
                cv.wrap_rows(sl, range(sl_len), jcol, noise=0.04)
                if sl_len == 12:
                    cv.wrap_row(sl, 11, jdark, noise=0.03)
        if outer == "cardigan" and rng.random() < 0.6:
            for y in range(1, length, 3):
                cv.fput("jacket", "front", 1, y, shade(jcol, 0.6), noise=0)

    # ---- accessories -----------------------------------------------------------------
    hat = rng.choices(["none", "cap", "beanie", "headband", "bandana", "beret", "flower", "bucket", "hood_up"],
                      [62, 9, 8, 5, 3, 3, 3, 4, 3])[0]
    if hstyle == "afro" and hat in ("cap", "beanie", "bucket", "beret", "hood_up"):
        hat = "headband"
    if hat != "none":
        hname, hcol = garment_colour(rng)
        tags.append(f"hat:{hat}")
        attrs["hat"] = hat
        hdark = shade(hcol, 0.8)
        if hat in ("cap", "beanie", "bucket", "beret", "hood_up"):
            # wipe hair volume on the outer layer where the hat sits
            for face in ("top",):
                for x in range(8):
                    for y in range(8):
                        cv.fput("hat", face, x, y, hcol, noise=0.04)
            rows = {"cap": 2, "beanie": 3, "bucket": 3, "beret": 1, "hood_up": 8}[hat]
            for face in ("front", "back", "right", "left"):
                for x in range(8):
                    for y in range(rows):
                        if hat == "hood_up" and face == "front" and 1 <= x <= 6 and y >= 1:
                            continue
                        if hat == "hood_up" and face in ("right", "left") and y >= 7:
                            continue
                        cv.fput("hat", face, x, y, hcol if y < rows - 1 or hat == "hood_up" else hdark, noise=0.04)
            if hat == "cap":
                logo = garment_colour(rng, "bright")[1]
                cv.fput("hat", "front", 3, 0, logo, noise=0)
                cv.fput("hat", "front", 4, 0, logo, noise=0)
                for x in range(8):
                    cv.fput("hat", "front", x, 1, hdark, noise=0.02)   # brim
                if rng.random() < 0.3:  # backwards cap
                    for x in range(8):
                        cv.fput("hat", "back", x, 1, hdark, noise=0.02)
                        cv.fput("hat", "front", x, 1, hcol, noise=0.02)
            if hat == "beanie" and rng.random() < 0.5:
                pom = garment_colour(rng, "pastel")[1]
                for (x, y) in ((3, 3), (4, 3), (3, 4), (4, 4)):
                    cv.fput("hat", "top", x, y, pom, noise=0.03)
            if hat == "hood_up":
                for x in range(8):
                    for y in range(8):
                        cv.fput("hat", "back", x, y, hcol, noise=0.04)
        elif hat == "headband":
            band_row = 1 if eye_row == 4 else 0
            for face in ("front", "back", "right", "left"):
                for x in range(8):
                    cv.fput("hat", face, x, band_row, hcol, noise=0.03)
        elif hat == "bandana":
            for x in range(8):
                for y in range(8):
                    cv.fput("hat", "top", x, y, hcol, noise=0.04)
            for face in ("front", "back", "right", "left"):
                for x in range(8):
                    cv.fput("hat", face, x, 0, hcol, noise=0.04)
            cv.fput("hat", "back", 3, 1, hcol)
            cv.fput("hat", "back", 4, 1, hcol)
            cv.fput("hat", "back", 4, 2, hdark)
        elif hat == "flower":
            fc = garment_colour(rng, "bright")[1]
            side = rng.choice(["right", "left"])
            for (x, y) in ((3, 1), (4, 0), (4, 2), (5, 1)):
                cv.fput("hat", side, x, y, fc, noise=0)
            cv.fput("hat", side, 4, 1, (240, 210, 70), noise=0)
    glasses = rng.choices(["none", "round", "square", "sunglasses", "goggles"], [74, 8, 9, 7, 2])[0]
    if glasses != "none":
        tags.append(f"glasses:{glasses}")
        attrs["glasses"] = glasses
        frame = rng.choice([(24, 24, 26), (70, 50, 40), (160, 150, 140), garment_colour(rng, "bright")[1]])
        y = eye_row
        if glasses == "sunglasses":
            lens = rng.choice([(20, 20, 24), (40, 30, 70), (70, 40, 20)])
            for x in (1, 2, 5, 6):
                cv.fput("hat", "front", x, y, lens, noise=0)
            for x in (0, 3, 4, 7):
                cv.fput("hat", "front", x, y, frame, noise=0)
        elif glasses == "goggles":
            for x in range(8):
                cv.fput("hat", "front", x, y, frame if x in (0, 3, 4, 7) else (150, 200, 220), noise=0, alpha=255)
            for face in ("right", "left", "back"):
                for x in range(8):
                    cv.fput("hat", face, x, y, shade(frame, 0.8), noise=0)
        else:
            for x in (0, 3, 4, 7):
                cv.fput("hat", "front", x, y, frame, noise=0)
            if glasses == "square":
                for x in (1, 2, 5, 6):
                    cv.fput("hat", "front", x, y - 1, frame, noise=0)
            else:
                for x in (1, 2, 5, 6):
                    cv.fput("hat", "front", x, y + 1, frame, noise=0)
        if glasses != "goggles":
            cv.fput("hat", "right", 7, y, frame, noise=0)
            cv.fput("hat", "right", 6, y, frame, noise=0)
            cv.fput("hat", "left", 0, y, frame, noise=0)
            cv.fput("hat", "left", 1, y, frame, noise=0)
    extras = []
    if rng.random() < 0.08:
        extras.append("headphones")
        hp = rng.choice([(30, 30, 30), (230, 230, 230), garment_colour(rng, "bright")[1]])
        for x in range(8):
            cv.fput("hat", "top", x, 3, hp, noise=0.02)
        for side in ("right", "left"):
            for x in (3, 4):
                for yy in (eye_row - 1, eye_row, eye_row + 1):
                    cv.fput("hat", side, x, yy, hp, noise=0.02)
    if rng.random() < 0.1 and top not in ("hoodie",):
        extras.append("scarf")
        sc = garment_colour(rng)[1]
        for col in range(24):
            for y in (0, 1):
                cv.put(cv.parts["jacket"].wrap[0] + col, cv.parts["jacket"].wrap[1] + y,
                       sc if (col + y) % 4 else shade(sc, 0.8), noise=0.03)
        for y in range(2, rng.randint(5, 8)):
            cv.fput("jacket", "front", 2, y, sc, noise=0.03)
        for x in range(8):
            cv.fput("head", "bottom", x, 7, sc, noise=0.03)
    if rng.random() < 0.1:
        extras.append("backpack")
        bp = garment_colour(rng, rng.choice(["bright", "dark", "neutral"]))[1]
        for y in range(0, 9):
            cv.fput("jacket", "front", 1, y, shade(bp, 0.8), noise=0.02)
            cv.fput("jacket", "front", 6, y, shade(bp, 0.8), noise=0.02)
        for y in range(1, 10):
            for x in range(1, 7):
                cv.fput("jacket", "back", x, y, bp if y != 5 else shade(bp, 0.75), noise=0.04)
        for x in range(1, 7):
            for y in range(4):
                cv.fput("jacket", "top", x, y, bp, noise=0.04)
    if rng.random() < 0.15 and sleeve < 12:
        extras.append("watch")
        wc = rng.choice([(30, 30, 30), (190, 190, 200), (200, 170, 80)])
        cv.wrap_row("left_arm", 9, wc, noise=0.02)
    if rng.random() < 0.07:
        extras.append("gloves")
        gc = rng.choice([(30, 30, 30), (100, 70, 50), garment_colour(rng)[1]])
        for arm in ("right_arm", "left_arm"):
            cv.wrap_rows(arm, range(10, 12), gc, noise=0.03)
            cv.fill_face(arm, "bottom", gc, noise=0.03)
    if rng.random() < 0.1 and neck in ("crew", "v", "scoop", "none"):
        extras.append("necklace")
        nc = rng.choice([(220, 190, 80), (200, 200, 210), garment_colour(rng, "bright")[1]])
        cv.fput("body", "front", 3, 1, nc, noise=0)
        cv.fput("body", "front", 4, 1, nc, noise=0)
        cv.fput("body", "front", 2, 0, nc, noise=0)
        cv.fput("body", "front", 5, 0, nc, noise=0)
    tags += [f"extra:{e}" for e in extras]
    attrs["extras"] = extras

    # ---- finalise: base layer opaque, nothing outside faces -------------------------
    img = cv.img
    bm, om = skinlib.base_mask(model).load(), skinlib.overlay_mask(model).load()
    px = img.load()
    for y in range(64):
        for x in range(64):
            if bm[x, y]:
                r, g, b, a = px[x, y]
                px[x, y] = (r, g, b, 255)
            elif not om[x, y]:
                px[x, y] = (0, 0, 0, 0)
    tags = [t for t in tags if t]
    return img, model, sorted(set(tags), key=tags.index), attrs


def _fxy(cv, part, face, x, y):
    x0, y0, _, _ = cv.face(part, face)
    return (x0 + x, y0 + y)


def skin_seed(base_seed: int, index: int) -> int:
    return base_seed * SEED_STRIDE + index


def generate_pool(out: Path, count: int, base_seed: int, start: int = 0) -> list:
    out.mkdir(parents=True, exist_ok=True)
    rows = []
    for i in range(start, start + count):
        seed = skin_seed(base_seed, i)
        img, model, tags, attrs = generate(seed)
        errs = skinlib.validate_skin(img, model)
        if errs:
            raise RuntimeError(f"generated skin {i} invalid: {errs}")
        skin_id = f"proc_{i:04d}"
        path = out / f"{skin_id}.png"
        img.save(path, format="PNG", optimize=False, compress_level=9)
        rows.append({"skin_id": skin_id, "file": path, "model": model, "seed": seed, "tags": tags,
                     "attributes": attrs})
    return rows


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--count", type=int, default=300)
    ap.add_argument("--start", type=int, default=0, help="first index (skin ids proc_<index>)")
    ap.add_argument("--seed", type=int, default=DEFAULT_SEED, help="base seed")
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT)
    a = ap.parse_args()
    rows = generate_pool(a.out, a.count, a.seed, a.start)
    for r in rows:
        print(json.dumps({"skin_id": r["skin_id"], "model": r["model"], "seed": r["seed"], "tags": r["tags"]}))
    print(f"generated {len(rows)} skins -> {a.out}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
