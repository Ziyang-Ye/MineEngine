"""Minecraft state proxy (capture pipeline): the per-player state of the patched SolarisEngine logs drawn
deterministically on top of the block proxy, so a renderer sees HP, hunger, hotbar, held items, swings, hurt and facing.
A layer is drawn only when its state is given.
  colorize(gb)     = mc_proxy_style mat2 with the extended block table (cap_palette.BLOCK_RGB; ids 0..11 = mc.py)
                     and optional per-entity colours (team_of {pid: team or rgb}); == proxy_frames(gb, "mat2", team) on
                     mc.py ids bit for bit (selftest). [F, h, w] G-buffer -> [F, 2h, 2w, 3] uint8.
  draw_hud(img, st) = vanilla survival HUD, 1280 x 720 GUI scale st["gui_scale"] (default 3; layout anchored at the bottom centre) geometry (measured on vanilla frames: hearts x 366 + 24 i,
                     hunger x 885 - 24 i, y 603, 27 px; hotbar slot k x 366 + 60 k, y 654..720) mapped as the real frames
                     are (scale H / 720, width centre-cropped / padded: 384 x 672 = the renderer frame's resize + crop; hud_xform):
                     10 hearts (hp 0..20, ceil, half = left half; outlines white for 1 s after damage = vanilla blink),
                     10 drumsticks (food, filled from the right, half = right half), 9-slot hotbar with item icons (cube =
                     block, diagonal = tool / weapon, disc = other; colour cap_palette.item_rgb), count bars (count / 64),
                     selected-slot frame.
  draw_hand(img, held, swing_phase, using) = first-person right hand at the lower right: the held item's icon (mirrored:
                     handle lower right, tip toward the crosshair), empty hand = skin-tone arm; vanilla swing (x -0.4 sin(sqrt(p) pi), y +0.2 sin(sqrt(p) 2 pi), tilt, depth shrink)
                     over the 6-tick (300 ms, ~5-6 frames) swing; while using (eat / bow / shield) moved toward the centre
                     with a ROSE outline.
  overlay_entities(img, pid, cam, others, ld) = per other entity (G-buffer pid, Mineflayer camera): RED hurt tint on its
                     pixels for 500 ms after damage; BLUE face square on the side its head faces (head_yaw, pitch) + nose
                     stroke; VIOLET chest square on its body front (body_yaw); held-item swatch at its right hand (CYAN
                     frame, ROSE while using); ROSE arm stroke + hand trail during a swing; HP bar (RED fill, black rest)
                     at the vanilla name-tag spot (feet + height + 0.5 m), drawn through blocks like the name tag unless
                     sneaking. With the logdepth frame the other strokes are depth-tested against the G-buffer.
  overlay_block(img, cam, cell, stage, ld) = crosshair target (black block wireframe, T16) and dig progress (T17 stage 0..9:
                     black crack texels growing from the face centre on the visible faces).
Key colours are RGB corners that no block colour x shade (0.21..1, cap_palette's 157 ids), team colour x 0.45..1 or the
sky comes near (selftest: >= 50 RGB): RED health / hurt, GREEN hunger, BLUE head facing, VIOLET body facing, CYAN held
item / selected slot, ROSE swing / use. Chrome: black outlines / empty cells, white counts / blink, dark grey slots, skin arm.
compose(gb, frame_states, cams): per frame colorize -> entities -> block target -> hand -> HUD (dead: red death tint, no
hand / HUD); empty or None states == colorize. frame_states[k] (dict, every key optional): hp (alias health), max_hp,
food, hotbar (9 x item name / {name|id, count} / None, or prismarine items with slot 36..44), held_slot (0..8), held,
swing_phase (0..1; swing_phase(age_ms)), using, hurt_age_ms (own), others [dict(pid, pos (feet), body_yaw, head_yaw,
pitch (Mineflayer radians), hp, max_hp, hurt_age_ms, swing_phase, held, sneaking, using, size (w, h))], target (cell
xyz), dig ({cell, stage}). cams: [(eye, yaw, pitch[, vfov])] per frame or a cams.npz mapping (eye or c2w, yaw, pitch[, vfov]).
  python -m mineengine.state_proxy --test                (CPU only: a ray-cast synthetic G-buffer stands in for VoxelRenderer)
  python -m mineengine.state_proxy --preview OUT.png     (the selftest scene with every layer, for eyeballing)
"""
import math
import os
import sys

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__)); CODE = os.path.dirname(os.path.dirname(HERE))
from mineengine import mc_proxy_style as PS  # noqa: E402  (SKY, TEAM_RGB2, up2, proxy_frames)
from mineengine import mc as MCW  # noqa: E402  (BLOCK_NAMES, BLOCK_RGB)
from mineengine import voxel_proxy as VP  # noqa: E402  (camera_basis)
try:
    from mineengine import cap_palette as CP  # noqa: E402  (BLOCK_RGB, ID, item_rgb, is_empty, SKIN_RGB)
except ImportError:
    CP = None

RED, GREEN, BLUE, CYAN, ROSE, VIOLET = (255, 0, 0), (0, 255, 0), (0, 0, 255), (0, 255, 255), (255, 0, 128), (128, 0, 255)
BLACK, WHITE, SLOT_RGB, HANDLE = (0, 0, 0), (255, 255, 255), (48, 48, 48), (110, 80, 45)
SKIN = tuple(CP.SKIN_RGB) if CP is not None else (196, 145, 110)
SKIN_SIDE = tuple(int(round(v * 0.78)) for v in SKIN)
KEY_RGB = {"health / hurt": RED, "hunger": GREEN, "head facing": BLUE, "body facing": VIOLET, "held item": CYAN, "swing / use": ROSE}
HURT_MS, SWING_MS, BLINK_MS, TAG_DIST, STACK = 500.0, 300.0, 1000.0, 64.0, 64
W0, H0, GUI = 1280, 720, 3                                                       # HUD reference screen, px per GUI px
TOOL_TOKENS = {"sword", "pickaxe", "axe", "shovel", "hoe", "bow", "crossbow", "trident", "stick", "rod", "shears", "mace", "steel", "spear"}
BLOCK_SUFFIX = ("_planks", "_log", "_wood", "_wool", "_concrete", "_bricks", "_block", "_ore", "_stairs", "_slab", "_glass", "_leaves",
                "_terracotta", "stone", "sand", "sandstone", "dirt", "gravel", "tnt")
FALLBACK_RGB = (("diamond", (80, 220, 205)), ("netherite", (66, 61, 63)), ("golden", (246, 208, 62)), ("iron", (210, 210, 210)),
                ("stone", (128, 128, 128)), ("wooden", (150, 116, 65)), ("bow", (125, 90, 50)), ("bucket", (190, 190, 195)),
                ("apple", (215, 40, 40)), ("bread", (190, 140, 60)), ("beef", (125, 75, 50)), ("porkchop", (200, 120, 100)))


# ---- block proxy -------------------------------------------------------------------------------------------------------
def block_table(block_rgb=None):
    """block colour table: given, else cap_palette.BLOCK_RGB (mc.py's 12 ids + extensions), else mc.py BLOCK_RGB."""
    return np.asarray(block_rgb if block_rgb is not None else CP.BLOCK_RGB if CP is not None else MCW.BLOCK_RGB, np.uint8)


def _pid_lut(n, team_rgb, team, team_of):
    """pid -> float32 rgb [n, 3] and unknown-team flags [n] (colorize_frames' white / red flicker)."""
    lut, unk = np.zeros((n, 3), np.float32), np.zeros(n, bool)
    for p in range(1, n):
        v = team_of.get(p, team) if team_of else team
        if isinstance(v, (tuple, list, np.ndarray)) and len(v) == 3:
            lut[p] = v
        elif v in team_rgb:
            lut[p] = team_rgb[v]
        else:
            unk[p] = True
    return lut, unk


def colorize(gb, block_rgb=None, team_rgb=PS.TEAM_RGB2, team=0, team_of=None, t0=0):
    """mat2 proxy with an extended block table: gb mapping with logdepth, shade, pid, block [F, h, w] (+ edge) ->
    [F, 2h, 2w, 3] uint8. Every pid > 0 in team `team`, or per pid team_of {pid: team | (r, g, b)}; same float32 steps as
    mc_proxy_style.colorize_frames."""
    ld, sh, pid, blk = (np.asarray(gb[k]) for k in ("logdepth", "shade", "pid", "block"))
    edge = np.asarray(gb["edge"]) if "edge" in set(gb.keys()) else None
    if ld.ndim == 2:
        ld, sh, pid, blk = ld[None], sh[None], pid[None], blk[None]; edge = None if edge is None else edge[None]
    tab = block_table(block_rgb)
    img = tab[np.minimum(blk, len(tab) - 1)].astype(np.float32)
    s = sh.astype(np.float32)[..., None] / 255.0
    img = img * (0.35 + 0.65 * s)
    if edge is not None:
        img[edge > 0] *= 0.6
    img[ld == 0] = PS.SKY
    m = pid > 0
    if m.any():
        lut, unk = _pid_lut(max(256, int(pid.max()) + 1), team_rgb, team, team_of)
        u = m & unk[pid]; k_ = m & ~u
        img[k_] = lut[pid[k_]] * (0.45 + 0.55 * s[k_])
        for k in np.nonzero(u.any((1, 2)))[0]:
            mk = u[k]; col = np.array((255, 255, 255) if ((t0 + int(k)) // 2) % 2 else (220, 40, 30), np.float32)
            img[k][mk] = col * (0.45 + 0.55 * s[k][mk])
    return PS.up2(img.clip(0, 255).astype(np.uint8))


# ---- items -------------------------------------------------------------------------------------------------------------
def _clean(item):
    """item name / prismarine item / {id | name | item} -> bare lower-case name, None for empty."""
    if isinstance(item, dict):
        item = item.get("name") or item.get("id") or item.get("item")
    if not isinstance(item, str):
        return None
    n = item.split("[")[0].split(":")[-1].strip().lower()
    if CP is not None and CP.is_empty(n):
        return None
    return None if n in ("", "air", "none", "null", "empty") else n


_RGB = {}


def item_rgb(item):
    """held / hotbar item -> (r, g, b) (cap_palette.item_rgb; a small keyword table without it)."""
    n = _clean(item)
    if n is None:
        return SKIN
    if n not in _RGB:
        if CP is not None:
            c = CP.item_rgb(n)
        elif n in MCW.BLOCK_NAMES:
            c = MCW.BLOCK_RGB[MCW.BLOCK_NAMES.index(n)]
        else:
            c = next((v for k, v in FALLBACK_RGB if k in n), (175, 165, 150))
        _RGB[n] = tuple(int(v) for v in c)
    return _RGB[n]


def item_kind(item):
    """'tool' (tools, weapons, rods), 'block' (a placeable block), 'disc' (anything else), None (empty)."""
    n = _clean(item)
    if n is None:
        return None
    if TOOL_TOKENS & set(n.split("_")):
        return "tool"
    names = CP.ID if CP is not None else MCW.BLOCK_NAMES
    return "block" if n in names or n.endswith(BLOCK_SUFFIX) else "disc"


def _icons():
    """16 x 16 GUI-pixel icon codes: 1 top / 2 left / 3 right cube face, 4 handle, 5 item colour, 6 outline."""
    v, u = np.mgrid[0:16, 0:16] + 0.5

    def inside(pts):
        pts = np.asarray(pts, float); e = np.roll(pts, -1, 0) - pts
        cr = e[:, 0, None, None] * (v - pts[:, 1, None, None]) - e[:, 1, None, None] * (u - pts[:, 0, None, None])
        return (cr >= 0).all(0) | (cr <= 0).all(0)
    cube = np.zeros((16, 16), np.uint8)
    cube[inside([(8, 0.5), (15.5, 4.25), (8, 8), (0.5, 4.25)])] = 1
    cube[inside([(0.5, 4.25), (8, 8), (8, 15.5), (0.5, 11.75)])] = 2
    cube[inside([(8, 8), (15.5, 4.25), (15.5, 11.75), (8, 15.5)])] = 3
    a, d = np.array([2.5, 13.5]), np.array([11.0, -11.0])
    t = np.clip(((u - a[0]) * d[0] + (v - a[1]) * d[1]) / (d @ d), 0, 1)
    dist = np.hypot(u - a[0] - t * d[0], v - a[1] - t * d[1])
    tool = np.where(dist <= 1.6, np.where(t < 0.38, 4, 5), np.where(dist <= 2.6, 6, 0)).astype(np.uint8)
    r = np.hypot(u - 8, v - 8.5)
    disc = np.where(r <= 5.2, 5, np.where(r <= 6.4, 6, 0)).astype(np.uint8)
    return {"block": cube, "tool": tool, "disc": disc}


ICONS = _icons()


def _item_lut(c):
    c = np.asarray(c, np.float32)
    return np.stack([np.zeros(3), c, c * 0.78, c * 0.58, HANDLE, c, BLACK]).round().clip(0, 255).astype(np.uint8)


def _mask(rows):
    """'#' outline 1, 'x' interior 2 (column <= 4) / 3, 'w' bone 4."""
    a = np.array([list(r) for r in rows]); col = np.arange(a.shape[1])[None]
    c = np.zeros(a.shape, np.uint8); c[a == "#"] = 1; c[(a == "x") & (col <= 4)] = 2; c[(a == "x") & (col > 4)] = 3; c[a == "w"] = 4
    return c


HEART = _mask([".###.###.", "#xxx#xxx#", "#xxxxxxx#", "#xxxxxxx#", ".#xxxxx#.", "..#xxx#..", "...#x#...", "....#....", "........."])
FOOD = _mask(["...###...", "..#xxx#..", ".#xxxxx#.", ".#xxxxx#.", ".#xxxxx#.", "..#xxx#..", "...#w#...", "..#www#..", "..##.##.."])


# ---- raster primitives (pixel centres; keep = [H, W] bool mask or None) -------------------------------------------------
def _box(pts, H, W):
    pts = np.asarray(pts, np.float64)
    if not np.isfinite(pts).all():
        return None
    x0, y0 = np.maximum(np.floor(pts.min(0)).astype(int), 0); x1, y1 = np.minimum(np.ceil(pts.max(0)).astype(int) + 1, (W, H))
    return None if x0 >= x1 or y0 >= y1 else (x0, y0, x1, y1)


def _poly(img, pts, col, keep=None):
    """fill the convex polygon pts [n, 2] (image x, y) -> pixels drawn."""
    b = _box(pts, *img.shape[:2])
    if b is None:
        return 0
    x0, y0, x1, y1 = b; pts = np.asarray(pts, np.float64)
    ys, xs = np.mgrid[y0:y1, x0:x1] + 0.5
    e = np.roll(pts, -1, 0) - pts
    cr = e[:, 0, None, None] * (ys - pts[:, 1, None, None]) - e[:, 1, None, None] * (xs - pts[:, 0, None, None])
    m = (cr >= 0).all(0) | (cr <= 0).all(0)
    if keep is not None:
        m &= keep[y0:y1, x0:x1]
    img[y0:y1, x0:x1][m] = col
    return int(m.sum())


def _seg(img, p0, p1, wd, col, keep=None):
    """thick segment with square caps."""
    p0, p1 = np.asarray(p0, np.float64), np.asarray(p1, np.float64); d = p1 - p0; L = float(np.hypot(*d))
    t = d / L if L > 1e-6 else np.array([1.0, 0.0]); n = np.array([-t[1], t[0]]) * wd / 2; t = t * wd / 2
    return _poly(img, [p0 - t + n, p1 + t + n, p1 + t - n, p0 - t - n], col, keep)


def _rect(img, x0, y0, x1, y1, col, keep=None):
    H, W = img.shape[:2]
    x0, y0, x1, y1 = (int(math.floor(v + 0.5)) for v in (x0, y0, x1, y1))
    x0, y0, x1, y1 = max(x0, 0), max(y0, 0), min(x1, W), min(y1, H)
    if x0 >= x1 or y0 >= y1:
        return 0
    if keep is None:
        img[y0:y1, x0:x1] = col; return (x1 - x0) * (y1 - y0)
    m = keep[y0:y1, x0:x1]; img[y0:y1, x0:x1][m] = col
    return int(m.sum())


def _blit(img, codes, lut, M, keep=None):
    """codes [h, w] (0 = transparent) through the affine M (2 x 3: icon px (u, v) -> image x, y), nearest, lut [n, 3]."""
    h, w = codes.shape; A, t = M[:, :2], M[:, 2]
    b = _box(np.array([[0, 0], [w, 0], [w, h], [0, h]], float) @ A.T + t, *img.shape[:2])
    if b is None:
        return 0
    x0, y0, x1, y1 = b; ys, xs = np.mgrid[y0:y1, x0:x1] + 0.5; Ai = np.linalg.inv(A)
    u = np.floor(Ai[0, 0] * (xs - t[0]) + Ai[0, 1] * (ys - t[1])).astype(int); v = np.floor(Ai[1, 0] * (xs - t[0]) + Ai[1, 1] * (ys - t[1])).astype(int)
    ok = (u >= 0) & (u < w) & (v >= 0) & (v < h)
    c = np.zeros(u.shape, codes.dtype); c[ok] = codes[v[ok], u[ok]]; m = c > 0
    if keep is not None:
        m &= keep[y0:y1, x0:x1]
    img[y0:y1, x0:x1][m] = lut[c[m]]
    return int(m.sum())


# ---- HUD ---------------------------------------------------------------------------------------------------------------
def hud_xform(H, W):
    """1280 x 720 screen px -> image px: (scale, x offset) = (H / 720, centre crop / pad of the width)."""
    s = H / float(H0)
    return s, (W - W0 * s) / 2.0


def _gshift(g):
    """GUI coords of the scale-3 layout -> the scale-g screen: vanilla anchors the HUD at the bottom centre."""
    return W0 / 2.0 / g - W0 / 2.0 / GUI, H0 / float(g) - H0 / float(GUI)


def _gm(H, W, gx, gy, k=1.0, g=GUI):
    """affine of an icon at GUI (gx, gy) (scale-3 layout) with k GUI px per icon px, drawn at GUI scale g."""
    s, ox = hud_xform(H, W); a = g * s; dx, dy = _gshift(g)
    return np.array([[a * k, 0.0, (gx + dx) * a + ox], [0.0, a * k, (gy + dy) * a]])


def _grect(img, gx0, gy0, gx1, gy1, col, g=GUI):
    s, ox = hud_xform(*img.shape[:2]); a = g * s; dx, dy = _gshift(g)
    return _rect(img, (gx0 + dx) * a + ox, (gy0 + dy) * a, (gx1 + dx) * a + ox, (gy1 + dy) * a, col)


def _get(st, *keys, default=None):
    return next((st[k] for k in keys if k in st and st[k] is not None), default)


def hotbar_items(st):
    """frame state -> 9 x (name, count) or None (accepts 9-lists, {slot: item}, prismarine items with slot 36..44)."""
    hb, out = st.get("hotbar"), [None] * 9
    if isinstance(hb, dict):
        hb = [dict(v, slot=int(k)) if isinstance(v, dict) else {"name": v, "slot": int(k)} for k, v in hb.items()]
    if isinstance(hb, (list, tuple)):
        for i, e in enumerate(hb):
            k = e.get("slot", i) if isinstance(e, dict) else i
            k = k - 36 if k is not None and k >= 36 else k
            n = _clean(e)
            if n is not None and k is not None and 0 <= k < 9:
                out[k] = (n, int(e.get("count", 1) or 1) if isinstance(e, dict) else 1)
    elif hb is None:
        k, n = _get(st, "held_slot", "selected_slot", "quickBarSlot"), _clean(st.get("held"))
        if k is not None and n is not None and 0 <= int(k) < 9:
            out[int(k)] = (n, 1)
    return out


def draw_hud(img, st):
    """vanilla survival HUD of frame state st on img [H, W, 3] (in place; module docstring) -> img."""
    H, W = img.shape[:2]; g = float(st.get("gui_scale") or GUI)                   # the capture mod records GUI scale 2 at 720p
    hp, food = _get(st, "hp", "health"), _get(st, "food", "hunger")
    if hp is not None:
        hp = int(math.ceil(max(float(hp), 0.0))); n = min(10, int(math.ceil(float(_get(st, "max_hp", "max_health", default=20)) / 2)))
        age = st.get("hurt_age_ms"); blink = age is not None and 0 <= age < BLINK_MS and int(age // 150) % 2 == 0
        for i in range(n):
            lut = np.array([BLACK, WHITE if blink else BLACK, RED if hp >= 2 * i + 1 else BLACK, RED if hp >= 2 * i + 2 else BLACK, WHITE], np.uint8)
            _blit(img, HEART, lut, _gm(H, W, 122 + 8 * i, 201, g=g))
    if food is not None:
        food = int(round(float(food)))
        for i in range(10):
            lut = np.array([BLACK, BLACK, GREEN if food >= 2 * i + 2 else BLACK, GREEN if food >= 2 * i + 1 else BLACK, WHITE], np.uint8)
            _blit(img, FOOD, lut, _gm(H, W, 295 - 8 * i, 201, g=g))
    sel = _get(st, "held_slot", "selected_slot", "quickBarSlot")
    if "hotbar" in st or sel is not None:
        _grect(img, 122, 218, 304, 240, BLACK, g=g)
        for k, it in enumerate(hotbar_items(st)):
            _grect(img, 124 + 20 * k, 220, 142 + 20 * k, 238, SLOT_RGB, g=g)
            if it is not None:
                _blit(img, ICONS[item_kind(it[0])], _item_lut(item_rgb(it[0])), _gm(H, W, 125 + 20 * k, 221, g=g))
                if it[1] > 1:
                    _grect(img, 124 + 20 * k, 236, 124 + 20 * k + 18.0 * min(it[1], STACK) / STACK, 238, WHITE, g=g)
        if sel is not None and 0 <= int(sel) < 9:
            gx = 121 + 20 * int(sel)
            for r in ((gx, 217, gx + 24, 219), (gx, 238, gx + 24, 240), (gx, 217, gx + 2, 240), (gx + 22, 217, gx + 24, 240)):
                _grect(img, *r, CYAN, g=g)
    return img


# ---- first-person hand -------------------------------------------------------------------------------------------------
HAND_AT = np.array([1010.0, 565.0])                                              # 1280 x 720 px: item centre at rest
HAND_K = {"block": 13.0, "tool": 17.0, "disc": 10.0}                             # 1280 x 720 px per icon px
ARM = np.array([[60, 260], [235, 260], [95, -90], [-30, -60]], float)            # skin front face, relative to HAND_AT
ARM_SIDE = np.array([[235, 260], [300, 260], [140, -95], [95, -90]], float)


def hand_pose(swing_phase=None, using=False):
    """first-person pose: (offset px at 1280 x 720, rotation rad, scale): vanilla swing translate / tilt / depth, use =
    toward the centre."""
    off, rot, sc = np.zeros(2), 0.0, 1.0
    if swing_phase is not None and 0 <= swing_phase < 1:
        p = float(swing_phase); f1 = math.sin(math.sqrt(p) * math.pi)
        off += [-0.4 * f1 * 715, -0.2 * math.sin(math.sqrt(p) * 2 * math.pi) * 715]
        rot, sc = -math.radians(40) * f1, 0.72 / (0.72 + 0.2 * math.sin(p * math.pi))
    if using:
        off += [-300.0, -70.0]
    return off, rot, sc


def draw_hand(img, held, swing_phase=None, using=False):
    """first-person right hand of the viewer on img [H, W, 3] (in place) -> img."""
    H, W = img.shape[:2]; s, ox = hud_xform(H, W)
    off, rot, sc = hand_pose(swing_phase, using)
    R = np.array([[math.cos(rot), -math.sin(rot)], [math.sin(rot), math.cos(rot)]]) * sc; A = HAND_AT + off
    to_img = lambda P: (np.asarray(P, float) @ R.T + A) * s + [ox, 0.0]  # noqa: E731
    kind, wd = item_kind(held), 3.0 * s
    if kind is None:
        quad = to_img(ARM); side = to_img(ARM_SIDE)
        _poly(img, quad, SKIN); _poly(img, side, SKIN_SIDE)
        for P in (quad, side):
            for a, b in zip(P, np.roll(P, -1, 0)):
                _seg(img, a, b, wd, BLACK)
    else:
        k = HAND_K[kind]; quad = to_img(k * (np.array([[0, 0], [16, 0], [16, 16], [0, 16]], float) - 8))
        Rf = R * [-k, k]                                                         # icon mirrored: handle lower right, tip toward the crosshair
        Mb = np.c_[Rf, A - Rf @ np.array([8.0, 8.0])] * s; Mb[0, 2] += ox
        _blit(img, ICONS[kind], _item_lut(item_rgb(held)), Mb)
    if using:
        for a, b in zip(quad, np.roll(quad, -1, 0)):
            _seg(img, a, b, 4.0 * s, ROSE)
    return img


# ---- cameras / projection ----------------------------------------------------------------------------------------------
def _cam(cam, vfov=70.0):
    eye, yaw, pitch = cam[:3]
    return np.asarray(eye, np.float64), float(yaw), float(pitch), float(cam[3]) if len(cam) > 3 and cam[3] is not None else float(vfov)


def cams_list(cams, F, vfov=70.0):
    """cams (None, [(eye, yaw, pitch[, vfov])], or a mapping with eye | c2w, yaw, pitch[, vfov]) -> F x tuple or None."""
    if cams is None:
        return [None] * F
    if hasattr(cams, "keys"):
        keys = set(cams.keys())
        eye = np.asarray(cams["eye"]) if "eye" in keys else np.asarray(cams["c2w"])[:, :3, 3]
        yaw, pitch = np.asarray(cams["yaw"]), np.asarray(cams["pitch"])
        fov = np.broadcast_to(np.asarray(cams["vfov"], np.float64), (F,)) if "vfov" in keys else np.full(F, float(vfov))
        return [(eye[k].astype(np.float64), float(yaw[k]), float(pitch[k]), float(fov[k])) for k in range(F)]
    out = [None if c is None else _cam(c, vfov) for c in cams]
    assert len(out) == F, (len(out), F)
    return out


def depth_m(ld):
    """logdepth (VoxelRenderer's log scale) -> forward depth in metres (inf: sky, or the 208 m cap)."""
    ld = np.asarray(ld).astype(np.float32)
    return np.where((ld > 0) & (ld < 255), 0.0254 * 8 * np.exp2((ld - 1) / 254 * 10), np.inf).astype(np.float32)


def _up(a, H, W):
    a = np.asarray(a)
    if a.shape[:2] == (H, W):
        return a
    ky, kx = H // a.shape[0], W // a.shape[1]
    assert ky * a.shape[0] == H and kx * a.shape[1] == W, (a.shape, H, W)
    return a.repeat(ky, 0).repeat(kx, 1)


class _View:
    """projection of one camera into an H x W image (the renderer's rays: continuous px, pixel centres at +0.5)."""

    def __init__(self, cam, H, W, ld=None, pid=None):
        self.eye, yaw, pitch, vfov = _cam(cam)
        self.f, self.r, self.u = VP.camera_basis(yaw, pitch)
        self.H, self.W, self.th = H, W, math.tan(math.radians(vfov) / 2); self.tw = self.th * W / H
        self.fpx, self.q = H / 2 / self.th, W / 672.0
        self.zb = None if ld is None else _up(depth_m(ld), H, W)
        self.pid = None if pid is None else _up(pid, H, W)

    def __call__(self, P):
        """world points [n, 3] -> image xy [n, 2], forward depth [n]."""
        c = np.atleast_2d(np.asarray(P, np.float64)) - self.eye; z = c @ self.f; zs = np.maximum(z, 1e-6)
        return np.stack([((c @ self.r) / zs / self.tw * 0.5 + 0.5) * self.W, (0.5 - (c @ self.u) / zs / self.th * 0.5) * self.H], -1), z

    def front(self, zo, tol=0.03, own=None, slack=0.0):
        """depth test of a stroke at depth zo (None without a depth buffer); on the entity's own pixels `own` it may lie
        up to `slack` m behind the box (the 0.6 m hitbox face is in front of the arms)."""
        if self.zb is None:
            return None
        k = self.zb >= zo * (1 - tol)
        return k | (own & (self.zb >= zo - slack)) if own is not None and slack > 0 else k

    def seg3(self, img, A, B, wd, col, n=1, tol=0.03):
        """world segment A-B in n depth-tested pieces, clipped at 5 cm in front of the camera."""
        A, B = np.asarray(A, np.float64), np.asarray(B, np.float64)
        za, zb = (np.asarray([A, B]) - self.eye) @ self.f
        if za < 0.05 and zb < 0.05:
            return 0
        if za < 0.05:
            A = A + (B - A) * (0.05 - za) / (zb - za)
        elif zb < 0.05:
            B = B + (A - B) * (0.05 - zb) / (za - zb)
        P, z = self(A + (B - A) * np.linspace(0, 1, n + 1)[:, None]); k = 0
        for i in range(n):
            k += _seg(img, P[i], P[i + 1], wd, col, self.front(0.5 * (z[i] + z[i + 1]), tol))
        return k


# ---- other entities ----------------------------------------------------------------------------------------------------
def _dims(o):
    sneak = bool(o.get("sneaking"))
    if o.get("size") is not None:
        w, h = (float(v) for v in o["size"]); return w, h, float(o.get("eye_h", 0.9 * h))
    return 0.6, 1.5 if sneak else 1.8, float(o.get("eye_h", 1.27 if sneak else 1.62))


def overlay_entities(img, gb_frame_pid, cam, others, ld=None):
    """state overlays of the other entities (module docstring) on img [H, W, 3] in place; gb_frame_pid / ld = this view's
    G-buffer pid / logdepth frame at img size or an integer fraction of it; cam = (eye, yaw, pitch[, vfov]) -> img."""
    H, W = img.shape[:2]; V = _View(cam, H, W, ld, gb_frame_pid); q, up = V.q, np.array([0.0, 1.0, 0.0])
    order = sorted(others or (), key=lambda o: -float(np.linalg.norm(np.asarray(o["pos"], np.float64) - V.eye)))
    for o in order:
        p, pos = int(o.get("pid") or 0), np.asarray(o["pos"], np.float64); w_, h_, eh = _dims(o)
        if V(pos + up * h_ / 2)[1][0] <= 0.05:
            continue
        mine = V.pid == p if p > 0 and V.pid is not None else np.zeros((H, W), bool)
        age = o.get("hurt_age_ms")
        if age is not None and 0 <= age < HURT_MS:
            img[mine] = RED
        by = float(_get(o, "body_yaw", "yaw", default=0.0)); hy = float(_get(o, "head_yaw", default=by)); pt = float(_get(o, "pitch", default=0.0))
        fb, rb = np.array([-math.sin(by), 0.0, -math.cos(by)]), np.array([math.cos(by), 0.0, -math.sin(by)])
        cc = pos + up * 0.58 * h_ + fb * 0.13                                     # chest front: body facing
        if fb @ (V.eye - cc) > 0:
            P, z = V([cc + a * 0.15 * rb + b * 0.18 * up for a, b in ((-1, -1), (1, -1), (1, 1), (-1, 1))])
            if (z > 0.05).all():
                _poly(img, P, VIOLET, mine)
        fh, rh, uh = VP.camera_basis(hy, pt); fc = pos + up * eh + fh * 0.22      # face: head facing
        toward = fh @ (V.eye - fc) > 0
        if toward:
            P, z = V([fc + a * 0.15 * rh + b * 0.15 * uh for a, b in ((-1, -1), (1, -1), (1, 1), (-1, 1))])
            if (z > 0.05).all():
                _poly(img, P, BLUE, mine)
        P, z = V([fc, fc + fh * 0.35])
        if (z > 0.05).all():
            keep = V.front(z.mean()) if V.zb is not None else (None if toward else ~mine)
            _seg(img, P[0], P[1], float(np.clip(0.07 * V.fpx / z.mean(), 2 * q, 5 * q)), BLUE, keep)
        sp = o.get("swing_phase"); swing = sp is not None and 0 <= sp < 1
        sh_ = pos + up * 0.78 * h_ + rb * 0.36; L = 0.36 * h_                    # right shoulder, arm length

        def hand(ph):
            a = 0.15 + (1.45 * math.sin(math.pi * ph) if ph is not None else 0.0)
            return sh_ + L * (-math.cos(a) * up + math.sin(a) * fb)
        if swing:
            P, z = V([sh_] + [hand(x) for x in np.linspace(max(0.0, sp - 0.35), sp, 5)])
            if (z > 0.05).all():
                wd = float(np.clip(0.1 * V.fpx / z.mean(), 2 * q, 6 * q)); keep = V.front(z.mean(), own=mine, slack=0.35)
                _seg(img, P[0], P[-1], wd, ROSE, keep)
                for a, b in zip(P[1:-1], P[2:]):
                    _seg(img, a, b, max(0.6 * wd, 1.5 * q), ROSE, keep)
        held = _clean(o.get("held"))
        if held is not None:
            P, z = V(hand(sp if swing else None))
            if z[0] > 0.05:
                x, y = P[0]; hw = float(np.clip(0.11 * V.fpx / z[0], 2.5 * q, 8 * q)); t = max(1.0, round(q)); keep = V.front(z[0], own=mine, slack=0.35)
                _rect(img, x - hw, y - hw, x + hw, y + hw, ROSE if o.get("using") else CYAN, keep)
                _rect(img, x - hw + t, y - hw + t, x + hw - t, y + hw - t, item_rgb(held), keep)
        hp = _get(o, "hp", "health")
        if hp is not None:
            anc = pos + up * (h_ + 0.5); P, z = V(anc)
            if z[0] > 0.05 and np.linalg.norm(anc - V.eye) <= TAG_DIST:
                x, y = P[0]; bw = float(np.clip(0.8 * V.fpx / z[0], 22 * q, 80 * q)); bh = float(np.clip(0.1 * V.fpx / z[0], 4 * q, 8 * q))
                keep = V.front(z[0]) if o.get("sneaking") else None; b = max(1, int(round(q)))
                x0, y0 = int(round(x - bw / 2)), int(round(y - bh / 2)); x1, y1 = x0 + int(round(bw)), y0 + int(round(bh))
                frac = min(max(float(hp) / float(_get(o, "max_hp", "max_health", default=20.0)), 0.0), 1.0)
                _rect(img, x0, y0, x1, y1, BLACK, keep)
                _rect(img, x0 + b, y0 + b, x0 + b + int(round((x1 - x0 - 2 * b) * frac)), y1 - b, RED, keep)
    return img


# ---- crosshair target / dig progress -----------------------------------------------------------------------------------
CUBE = np.array([[(i >> 0) & 1, (i >> 1) & 1, (i >> 2) & 1] for i in range(8)], np.float64)
CUBE_EDGES = [(a, b) for a in range(8) for b in range(a + 1, 8) if bin(a ^ b).count("1") == 1]
_cr = np.random.default_rng(7).random((16, 16)); _cd = np.hypot(*(np.mgrid[0:16, 0:16] - 7.5)) / 10.61
CRACK = np.argsort(np.argsort((0.6 * _cd + 0.4 * _cr).ravel())).reshape(16, 16) / 256.0   # texel rank: stage k covers rank < (k + 1) / 10


def overlay_block(img, cam, cell, stage=None, ld=None, gb_frame_pid=None):
    """crosshair target outline (black wireframe of the cell) and dig cracks (stage 0..9) on img [H, W, 3] in place -> img."""
    H, W = img.shape[:2]; V = _View(cam, H, W, ld, gb_frame_pid); c0 = np.floor(np.asarray(cell, np.float64))
    if stage is not None and 0 <= int(stage) <= 9:
        for ax in range(3):
            for side in (0, 1):
                fcen = c0 + 0.5; fcen[ax] = c0[ax] + side; nrm = np.zeros(3); nrm[ax] = 1 if side else -1
                if nrm @ (V.eye - fcen) <= 0:
                    continue
                P, z = V(c0 + CUBE[CUBE[:, ax] == side])
                b = _box(P, H, W) if (z > 0.05).all() else None
                if b is None:
                    continue
                x0, y0, x1, y1 = b; ys, xs = np.mgrid[y0:y1, x0:x1] + 0.5
                d = V.f + ((xs / W * 2 - 1) * V.tw)[..., None] * V.r - ((ys / H * 2 - 1) * V.th)[..., None] * V.u   # d.f = 1: t = depth
                dd = d[..., ax]; t = (c0[ax] + side - V.eye[ax]) / np.where(np.abs(dd) < 1e-9, 1e-9, dd)
                X = V.eye + t[..., None] * d; o1, o2 = [a for a in range(3) if a != ax]
                uu, vv = X[..., o1] - c0[o1], X[..., o2] - c0[o2]
                m = (t > 0.05) & (uu >= 0) & (uu < 1) & (vv >= 0) & (vv < 1)
                if V.zb is not None:
                    m &= np.abs(V.zb[y0:y1, x0:x1] - t) <= 0.04 * t + 0.05
                if V.pid is not None:
                    m &= V.pid[y0:y1, x0:x1] == 0
                tex = CRACK[np.clip((uu * 16).astype(int), 0, 15), np.clip((vv * 16).astype(int), 0, 15)] < (int(stage) + 1) / 10.0
                img[y0:y1, x0:x1][m & tex] = BLACK
    wd = max(1.0, 1.5 * V.q)
    for a, b in CUBE_EDGES:
        V.seg3(img, c0 + CUBE[a], c0 + CUBE[b], wd, BLACK, n=8, tol=0.04)
    return img


# ---- per-frame composition ---------------------------------------------------------------------------------------------
def swing_phase(age_ms, dur_ms=SWING_MS):
    """time since the last swing event -> phase 0..1 of the vanilla 6-tick arm swing, None outside it."""
    return None if age_ms is None or not 0 <= age_ms < dur_ms else float(age_ms) / dur_ms


def compose_frame(img, st, cam=None, pid=None, ld=None):
    """every state layer of frame state st on one colorized frame img (in place) -> img."""
    if not st:
        return img
    if st.get("others") and cam is not None:
        overlay_entities(img, pid, cam, st["others"], ld=ld)
    if cam is not None and (st.get("target") is not None or st.get("dig") is not None):
        dig = st.get("dig") or {}; tgt = st.get("target")
        cell = dig.get("cell", tgt) if dig else tgt
        if isinstance(cell, dict):
            cell = cell.get("cell", cell.get("pos"))
        if cell is not None:
            overlay_block(img, cam, cell, dig.get("stage"), ld=ld, gb_frame_pid=pid)
    hp = _get(st, "hp", "health")
    if hp is not None and float(hp) <= 0:                                        # vanilla death screen: red tint, no hand / HUD
        img[:] = (img.astype(np.float32) * 0.5 + np.array(RED, np.float32) * 0.5).round().astype(np.uint8)
        return img
    if "held" in st:
        draw_hand(img, st["held"], st.get("swing_phase"), st.get("using"))
    return draw_hud(img, st)


def compose(gb, frame_states, cams, block_rgb=None, vfov=70.0, team=0, team_of=None, t0=0):
    """colorize(gb) + the state layers of frame_states[k] (module docstring) -> [F, 2h, 2w, 3] uint8."""
    out = colorize(gb, block_rgb, team=team, team_of=team_of, t0=t0)
    if frame_states is None:
        return out
    F = len(out); assert len(frame_states) == F, (len(frame_states), F)
    C = cams_list(cams, F, vfov)
    ld, pid = np.asarray(gb["logdepth"]), np.asarray(gb["pid"])
    ld, pid = (ld[None], pid[None]) if ld.ndim == 2 else (ld, pid)
    for k, st in enumerate(frame_states):
        compose_frame(out[k], st, C[k], pid[k], ld[k])
    return out


# ---- selftest ----------------------------------------------------------------------------------------------------------
def raycast(cam, boxes, h, w, ground_y=0.0):
    """test stand-in for VoxelRenderer (CPU): axis-aligned boxes [(lo, hi, block id, pid)] + a grass plane at ground_y with
    the renderer's pixel-centre rays, depth rule and shade -> dict of [h, w] uint8 (logdepth, shade, block, pid, edge)."""
    eye, yaw, pitch, vfov = _cam(cam); f, r, u = VP.camera_basis(yaw, pitch)
    th = math.tan(math.radians(vfov) / 2); tw = th * w / h
    ys, xs = np.mgrid[0:h, 0:w] + 0.5
    d = f + ((xs / w * 2 - 1) * tw)[..., None] * r - ((ys / h * 2 - 1) * th)[..., None] * u
    ds = np.where(np.abs(d) < 1e-12, 1e-12, d); dn = d / np.linalg.norm(d, axis=-1, keepdims=True)
    z, blk, pid, nax = np.full((h, w), np.inf), np.zeros((h, w), np.uint8), np.zeros((h, w), np.uint8), np.zeros((h, w), int)
    if ground_y is not None:
        t = (ground_y - eye[1]) / ds[..., 1]; m = (t > 0.05) & (t < z); z[m] = t[m]; blk[m] = MCW.GRASS; nax[m] = 1
    for lo, hi, b, p in boxes:
        t1, t2 = (np.asarray(lo, float) - eye) / ds, (np.asarray(hi, float) - eye) / ds
        tmin = np.minimum(t1, t2); ax = tmin.argmax(-1); tn = tmin.max(-1); tf = np.maximum(t1, t2).min(-1)
        m = (tn <= tf) & (tn > 0.05) & (tn < z); z[m] = tn[m]; blk[m] = b; pid[m] = p; nax[m] = ax[m]
    hit = np.isfinite(z); zin = np.maximum(np.where(hit, z, 1.0) / 0.0254, 8)
    ld = np.where(hit, np.round(np.clip(np.log2(zin / 8) / 10, 0, 1) * 254 + 1), 0)
    c = np.abs(np.take_along_axis(dn, nax[..., None], -1)[..., 0])
    sh = np.where(hit, np.where(pid > 0, 0.35 + 0.65 * c, 0.25 + 0.75 * c), 0)
    return {"logdepth": ld.astype(np.uint8), "shade": np.round(sh * 255).astype(np.uint8), "block": np.where(pid > 0, 0, blk).astype(np.uint8),
            "pid": pid, "edge": np.zeros((h, w), np.uint8)}


CAM0 = (np.array([0.0, 1.62, 0.0]), 0.0, 0.0, 70.0)                              # selftest camera: eye 1.62 m, facing north
ENT = np.array([0.0, 0.0, -5.0])                                                 # other player's feet, 5 m ahead
TGT = (-2, 0, -4)                                                                # a block cell ahead-left


def scene(h=192, w=336, wall=False, ent=True, ext_id=None):
    """selftest G-buffer: grass plane, the other player (pid 1) at ENT, a block at TGT (TNT or ext_id), optional wall."""
    boxes = [(np.array(TGT, float), np.array(TGT, float) + 1, MCW.TNT if ext_id is None else ext_id, 0)]
    if ent:
        boxes.append((ENT + [-0.3, 0, -0.3], ENT + [0.3, 1.8, 0.3], 0, 1))
    if wall:
        boxes.append((np.array([-2.0, 0, -3.2]), np.array([2.0, 4.0, -3.0]), MCW.COBBLE, 0))
    return {k: v[None] for k, v in raycast(CAM0, boxes, h, w).items()}


def demo_states():
    """one frame state with every layer (selftest / --preview)."""
    return {"hp": 13, "max_hp": 20, "food": 7, "held_slot": 2, "held": "diamond_sword", "swing_phase": None, "using": False,
            "hotbar": ["oak_planks", {"name": "minecraft:cobblestone", "count": 32}, "diamond_sword", None, "bread", "bow", None, "tnt", "water_bucket"],
            "others": [dict(pid=1, pos=ENT, body_yaw=math.pi, head_yaw=math.pi + 0.3, pitch=0.1, hp=10, max_hp=20, hurt_age_ms=None,
                            swing_phase=0.5, held="iron_sword", sneaking=False)],
            "target": TGT, "dig": {"cell": TGT, "stage": 6}}


def selftest(seed=0):
    """-> list of failed checks (empty = pass)."""
    rng, fails = np.random.default_rng(seed), []

    def check(ok, msg):
        print(("PASS " if ok else "FAIL ") + msg, flush=True)
        if not ok:
            fails.append(msg)

    def cnt(img, col, m=None):
        e = (img == np.array(col, np.uint8)).all(-1)
        return int((e if m is None else e & m).sum())

    def centroid(img, col, m=None):
        e = (img == np.array(col, np.uint8)).all(-1)
        e = e if m is None else e & m; ys, xs = np.nonzero(e)
        return (float(xs.mean()), float(ys.mean())) if len(xs) else (np.nan, np.nan)
    tab = block_table(); print(f"[state_proxy] block table: {'cap_palette' if CP is not None else 'mc.py'} {len(tab)} ids", flush=True)
    # colorize == mat2 on mc.py ids; extended ids take the extended table
    for dt in (np.uint8, np.uint16):
        for team, t0 in ((0, 0), (1, 3), (-1, 0), (-1, 5)):
            gb = PS.random_gbuf(rng, block_dtype=dt, nblk=len(MCW.BLOCK_NAMES))
            a, b = colorize(gb, team=team, t0=t0), PS.proxy_frames(gb, "mat2", team, t0)
            c = colorize(gb, block_rgb=MCW.BLOCK_RGB, team=team, t0=t0)
            check(a.shape == b.shape and np.array_equal(a, b) and np.array_equal(c, b), f"colorize == proxy_frames mat2 on mc.py ids ({dt.__name__}, team {team}, t0 {t0})")
    check(CP is None or np.array_equal(CP.BLOCK_RGB[:len(MCW.BLOCK_RGB)], MCW.BLOCK_RGB), "cap_palette ids 0..11 == mc.py BLOCK_RGB")
    gb = PS.random_gbuf(rng, nblk=len(tab)); gb["pid"][:] = 0; gb["edge"][:] = 0; s_ = gb["shade"].astype(np.float32)[..., None] / 255.0
    ref = PS.up2(np.where((gb["logdepth"] == 0)[..., None], np.array(PS.SKY, np.float32), tab[gb["block"]].astype(np.float32) * (0.35 + 0.65 * s_)).clip(0, 255).astype(np.uint8))
    check(np.array_equal(colorize(gb), ref), f"colorize extended ids -> block_table colour x shade ({len(tab)} ids)")
    gb = PS.random_gbuf(rng, nblk=12); gb["pid"][0, :, :5] = 1; gb["pid"][0, :, 5:10] = 2
    a = colorize(gb, team_of={1: 0, 2: (10, 20, 30)})
    s0 = gb["shade"][0, 0, 0].astype(np.float32) / 255.0
    check(np.array_equal(a[0, 0, 0], (np.array(PS.TEAM_RGB2[0], np.float32) * (0.45 + 0.55 * s0)).clip(0, 255).astype(np.uint8))
          and np.array_equal(a[0, 0, 10], (np.array((10, 20, 30), np.float32) * (0.45 + 0.55 * gb["shade"][0, 0, 5].astype(np.float32) / 255.0)).astype(np.uint8)),
          "colorize team_of: per-pid team / rgb")
    # key colours far from every shaded block / team / sky colour
    S = np.linspace(0.21, 1.0, 80)[None, :, None]
    world = np.concatenate([(tab[1:].astype(np.float64)[:, None] * S).reshape(-1, 3),
                            (np.array(list(PS.TEAM_RGB2.values()), np.float64)[:, None] * np.linspace(0.45, 1, 40)[None, :, None]).reshape(-1, 3), [PS.SKY]])
    dmin = {k: float(np.linalg.norm(world - np.array(v, float), axis=1).min()) for k, v in KEY_RGB.items()}
    keys = np.array(list(KEY_RGB.values()), float); kk = np.linalg.norm(keys[:, None] - keys[None], axis=-1)[np.triu_indices(len(keys), 1)]
    check(min(dmin.values()) >= 50 and kk.min() >= 100, f"key colours >= 50 RGB from shaded blocks / teams / sky ({ {k: round(v) for k, v in dmin.items()} }), >= 100 apart ({kk.min():.0f})")
    # compose with empty states == colorize; deterministic
    gb = scene(); cams = [CAM0]
    base = colorize(gb)
    check(np.array_equal(compose(gb, [{}], cams), base) and np.array_equal(compose(gb, None, None), base) and np.array_equal(compose(gb, [None], None), base)
          and np.array_equal(compose(gb, [{"others": []}], cams), base), "compose with empty / None states == colorize")
    st = demo_states()
    full = compose(gb, [st], cams)
    check(np.array_equal(full, compose(gb, [st], {"eye": CAM0[0][None], "yaw": np.zeros(1), "pitch": np.zeros(1), "vfov": 70.0})),
          "compose deterministic; cams as list == cams mapping")
    man = colorize(gb)[0]
    overlay_entities(man, gb["pid"][0], CAM0, st["others"], ld=gb["logdepth"][0]); overlay_block(man, CAM0, TGT, 6, gb["logdepth"][0], gb["pid"][0])
    draw_hand(man, st["held"], None, False); draw_hud(man, st)
    check(np.array_equal(full[0], man), "compose == colorize -> overlay_entities -> overlay_block -> draw_hand -> draw_hud")
    # entity overlays at the projected places
    H, W = base.shape[1:3]; V = _View(CAM0, H, W); pidu = _up(gb["pid"][0], H, W); mine = pidu == 1
    ys, xs = np.nonzero(mine); top, bot, cx = ys.min(), ys.max(), xs.mean()
    P, _ = V(ENT + [0, 2.3, 0]); ax_, ay_ = P[0]

    def ov(**kw):
        o = dict(pid=1, pos=ENT, body_yaw=math.pi, head_yaw=math.pi, pitch=0.0); o.update(kw)
        return overlay_entities(base[0].copy(), gb["pid"][0], CAM0, [o], ld=gb["logdepth"][0])
    i10, i20 = ov(hp=10), ov(hp=20)
    rx, ry = centroid(i20, RED); r10, r20 = cnt(i10, RED), cnt(i20, RED)
    check(abs(rx - ax_) < 4 and abs(ry - ay_) < 3 and ry < top and 0.4 < r10 / max(r20, 1) < 0.6 and cnt(ov(hp=0), RED) == 0,
          f"HP bar at the name-tag spot above the head (full bar centroid {rx:.1f},{ry:.1f} vs {ax_:.1f},{ay_:.1f}; head top {top}), fill hp 10 / 20 = {r10 / max(r20, 1):.2f}")
    hurt, late = ov(hurt_age_ms=100), ov(hurt_age_ms=600)
    check(cnt(hurt, RED, mine) / mine.sum() > 0.6 and cnt(late, RED, mine) == 0, f"hurt tint: {cnt(hurt, RED, mine) / mine.sum():.2f} of the body red at 100 ms, none at 600 ms")
    fa = ov(); (bx, by_), (vx, vy) = centroid(fa, BLUE, mine), centroid(fa, VIOLET, mine)
    P, _ = V([ENT + [0, 1.62 + 0, 0.22], ENT + [0, 0.58 * 1.8, 0.13]])
    check(abs(bx - P[0, 0]) < 3 and abs(by_ - P[0, 1]) < 4 and by_ < top + 0.3 * (bot - top) and abs(vx - P[1, 0]) < 3 and abs(vy - P[1, 1]) < 4,
          f"facing the camera: BLUE face at the head ({bx:.1f},{by_:.1f} vs {P[0, 0]:.1f},{P[0, 1]:.1f}), VIOLET chest ({vx:.1f},{vy:.1f} vs {P[1, 0]:.1f},{P[1, 1]:.1f})")
    away = ov(body_yaw=0.0, head_yaw=0.0)
    check(cnt(away, BLUE) == 0 and cnt(away, VIOLET) == 0, "facing away: no face / chest marker (nose hidden behind the head by the depth test)")
    east = ov(body_yaw=0.0, head_yaw=-math.pi / 2); eb = (east == np.array(BLUE, np.uint8)).all(-1); exs = np.nonzero(eb)[1]
    check(len(exs) > 0 and exs.min() > xs.max() - 2 and cnt(east, BLUE, mine) == 0, f"head turned east: BLUE nose right of the body (x {exs.min() if len(exs) else -1} > {xs.max()}), none on it")
    held = ov(held="diamond_sword"); hx, hy = centroid(held, CYAN)
    c_in = cnt(held, item_rgb("diamond_sword"))
    check(cnt(held, CYAN) > 0 and hx < cx - 3 and c_in > 0 and hy > top + 0.4 * (bot - top), f"held swatch at its right hand (screen left when facing the camera: x {hx:.1f} < {cx:.1f}), item colour inside")
    check(cnt(ov(held="diamond_sword", using=True), ROSE) > 0 and cnt(ov(held="diamond_sword", using=True), CYAN) == 0, "using: swatch frame ROSE instead of CYAN")
    sw0, sw5 = ov(), ov(swing_phase=0.5)
    check(cnt(sw0, ROSE) == 0 and cnt(sw5, ROSE) > 10 and cnt(ov(swing_phase=1.2), ROSE) == 0, f"swing: ROSE arm stroke + trail at phase 0.5 ({cnt(sw5, ROSE)} px), none outside the swing")
    gw = scene(wall=True); bw = colorize(gw)[0]
    wo = lambda **kw: overlay_entities(bw.copy(), gw["pid"][0], CAM0, [dict(pid=1, pos=ENT, body_yaw=math.pi, hp=12, **kw)], ld=gw["logdepth"][0])  # noqa: E731
    check((gw["pid"] == 1).sum() == 0 and cnt(wo(), RED) > 0 and cnt(wo(sneaking=True), RED) == 0 and cnt(wo(held="stick"), CYAN) == 0,
          "behind a wall: HP bar through blocks (vanilla name tag) unless sneaking; hand swatch depth-tested away")
    beh = overlay_entities(base[0].copy(), gb["pid"][0], CAM0, [dict(pid=1, pos=[0, 0, 5.0], hp=5, held="stick", swing_phase=0.3)], ld=gb["logdepth"][0])
    check(np.array_equal(beh, base[0]), "entity behind the camera: nothing drawn")
    # crosshair target + dig cracks
    P, z = V(np.asarray(TGT, float) + CUBE); bx0, by0 = np.floor(P.min(0)).astype(int); bx1, by1 = np.ceil(P.max(0)).astype(int)
    roi = np.zeros((H, W), bool); roi[max(by0 - 2, 0):by1 + 2, max(bx0 - 2, 0):bx1 + 2] = True
    o0 = overlay_block(base[0].copy(), CAM0, TGT, None, gb["logdepth"][0], gb["pid"][0])
    nb = [cnt(overlay_block(base[0].copy(), CAM0, TGT, s_, gb["logdepth"][0], gb["pid"][0]), BLACK, roi) for s_ in (0, 4, 9)]
    check(cnt(o0, BLACK, roi) > 20 and cnt(o0, BLACK, ~roi) == 0 and cnt(o0, BLACK, roi) < nb[0] < nb[1] < nb[2],
          f"target outline on the cell only ({cnt(o0, BLACK, roi)} px), dig cracks grow with stage 0 / 4 / 9: {nb}")
    # HUD at the vanilla geometry (1280 x 720) and at 384 x 672
    hb = ["oak_planks", {"name": "minecraft:cobblestone", "count": 32}, "diamond_sword", None, "bread", "bow", None, "tnt", "water_bucket"]
    for (Hh, Ww, g) in ((720, 1280, 3), (384, 672, 3), (720, 1280, 2), (384, 672, 2)):
        s, ox = hud_xform(Hh, Ww); dx, dy = _gshift(g); px = lambda gx, gy: (int((gx + dx) * g * s + ox), int((gy + dy) * g * s))  # noqa: E731
        for hp, food, sel in ((13, 7, 2), (20, 20, 0), (0.5, 0, 8), (6, 19, 5)):
            img = draw_hud(np.full((Hh, Ww, 3), PS.SKY, np.uint8), {"hp": hp, "food": food, "hotbar": hb, "held_slot": sel, "gui_scale": g})
            hpi, ok = math.ceil(hp), True
            for i in range(10):
                L, R = img[px(122 + 8 * i + 2.5, 203.5)[::-1]], img[px(122 + 8 * i + 6.5, 203.5)[::-1]]
                ok &= tuple(L) == (RED if hpi >= 2 * i + 1 else BLACK) and tuple(R) == (RED if hpi >= 2 * i + 2 else BLACK)
                L, R = img[px(295 - 8 * i + 2.5, 204.5)[::-1]], img[px(295 - 8 * i + 6.5, 204.5)[::-1]]
                ok &= tuple(R) == (GREEN if food >= 2 * i + 1 else BLACK) and tuple(L) == (GREEN if food >= 2 * i + 2 else BLACK)
            for k in range(9):
                ok &= (tuple(img[px(133 + 20 * k, 217.5)[::-1]]) == CYAN) == (k == sel)
            ok &= tuple(img[px(125 + 8.5, 221 + 2.5)[::-1]]) == item_rgb("oak_planks") and tuple(img[px(125 + 40 + 10.5, 221 + 5.5)[::-1]]) == item_rgb("diamond_sword")
            ok &= tuple(img[px(125 + 20 + 8.5, 221 + 2.5)[::-1]]) == item_rgb("cobblestone") and tuple(img[px(124 + 20 + 2.5, 237.5)[::-1]]) == WHITE
            ok &= tuple(img[px(124 + 20 + 16.5, 237.5)[::-1]]) != WHITE and tuple(img[px(125 + 60 + 8.5, 221 + 8.5)[::-1]]) == SLOT_RGB
            check(ok, f"HUD {Ww} x {Hh} gui {g}: hearts hp {hp}, drumsticks food {food}, hotbar icons / count bar, selected slot {sel}")
    img = draw_hud(np.full((720, 1280, 3), PS.SKY, np.uint8), {"hp": 20, "hurt_age_ms": 100})
    check(tuple(img[603 + 4, 366 + 1]) == WHITE and tuple(draw_hud(np.full((720, 1280, 3), PS.SKY, np.uint8), {"hp": 20, "hurt_age_ms": 200})[607, 367]) == BLACK,
          "heart outlines blink white after damage (vanilla 3-tick period)")
    img = draw_hud(np.full((720, 1280, 3), PS.SKY, np.uint8), {"hp": 20}); ys_, xs_ = np.nonzero((img != np.array(PS.SKY, np.uint8)).any(-1))
    check(xs_.min() == 366 and xs_.max() == 366 + 9 * 24 + 26 and ys_.min() == 603 and ys_.max() < 603 + 27, "hearts exactly in the measured HUD cells (x 366 + 24 i, y 603, 27 px)")
    # first-person hand
    sky = np.full((384, 672, 3), PS.SKY, np.uint8)
    nonsky = lambda im: np.nonzero((im != sky).any(-1))  # noqa: E731
    e = draw_hand(sky.copy(), None); ys_, xs_ = nonsky(e)
    check(cnt(e, SKIN) > 500 and xs_.mean() > 0.65 * 672 and ys_.mean() > 0.6 * 384, f"empty hand: skin arm at the lower right ({cnt(e, SKIN)} px)")
    blk = draw_hand(sky.copy(), "oak_planks"); tl = draw_hand(sky.copy(), "diamond_sword")
    check(cnt(blk, item_rgb("oak_planks")) > 500 and cnt(blk, SKIN) == 0 and cnt(tl, item_rgb("diamond_sword")) > 300, "held block = cube in its colour, held tool = blade, no arm")
    xs6 = [nonsky(draw_hand(sky.copy(), "diamond_sword", p_))[1].mean() for p_ in (0.0, 1 / 6, 2 / 6, 3 / 6, 4 / 6, 5 / 6)] + [nonsky(tl)[1].mean()]
    check(xs6[0] == xs6[-1] and min(xs6[1:4]) < xs6[0] - 40 and xs6[5] > min(xs6[1:4]) + 20, f"swing arc over 6 frames moves the item left and back: x {[round(v) for v in xs6]}")
    us = draw_hand(sky.copy(), "bread", None, True)
    check(cnt(us, ROSE) > 50 and nonsky(us)[1].mean() < nonsky(draw_hand(sky.copy(), "bread"))[1].mean() - 50, "using: item toward the centre with a ROSE outline")
    dead = compose(gb, [dict(st, hp=0)], cams)[0]
    check(cnt(dead, SKIN) == 0 and dead[..., 0].mean() > dead[..., 1].mean() + 60, "dead: red death tint, no hand / HUD")
    return fails


def preview(path):
    """the selftest scene with every layer -> PNG (two frames: state as demo_states, and hurt + swing + using)."""
    import cv2
    gb = scene(); st = demo_states()
    st2 = dict(st, swing_phase=0.35, held="oak_planks", hurt_age_ms=100, hp=5, others=[dict(st["others"][0], hurt_age_ms=200, swing_phase=None, using=True, head_yaw=-math.pi / 2, hp=4)])
    fr = [compose(gb, [s_], [CAM0])[0] for s_ in (st, st2)]
    cv2.imwrite(path, cv2.cvtColor(np.concatenate(fr, 0), cv2.COLOR_RGB2BGR))
    print(f"[state_proxy] preview -> {path}", flush=True)


if __name__ == "__main__":
    if "--test" in sys.argv:
        f = selftest()
        print("[state_proxy]", "PASS" if not f else f"FAIL {f}", flush=True)
        sys.exit(1 if f else 0)
    if "--preview" in sys.argv:
        preview(sys.argv[sys.argv.index("--preview") + 1])
