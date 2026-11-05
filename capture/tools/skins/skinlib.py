"""Shared Minecraft 64x64 player-skin layout + a small orthographic software renderer.

UV layout (Minecraft 1.8+ 64x64 skins). A box part with texture origin (u, v) and
size (w, h, d) (x = width, y = height, z = depth) owns these face rectangles
(x, y, width, height) in the texture, exactly as net.minecraft.client.model.geom.ModelPart.Cube
lays them out:

    top    (u+d,       v,     w, d)
    bottom (u+d+w,     v,     w, d)   (v axis flipped by the model)
    right  (u,         v+d,   d, h)   character's right side (model -X)
    front  (u+d,       v+d,   w, h)   model -Z (the face of the character)
    left   (u+d+w,     v+d,   d, h)   character's left side (model +X)
    back   (u+2d+w,    v+d,   w, h)

The four side faces right|front|left|back are contiguous, so a horizontal "wrap" of
width 2d+2w at rows v+d .. v+d+h goes all the way round the part.

The renderer reproduces the client's PlayerModel geometry (classic and slim arms, the
outer layer inflated by 0.5 (hat) / 0.25 (jacket, sleeves, pants)) in model space
(+Y down, character faces -Z, character's left = +X) and projects it orthographically.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

from PIL import Image, ImageDraw

SKIN_W = SKIN_H = 64


@dataclass(frozen=True)
class Part:
    name: str
    u: int
    v: int
    w: int
    h: int
    d: int
    layer: str  # 'base' | 'overlay'

    def faces(self) -> dict:
        u, v, w, h, d = self.u, self.v, self.w, self.h, self.d
        return {
            "top": (u + d, v, w, d),
            "bottom": (u + d + w, v, w, d),
            "right": (u, v + d, d, h),
            "front": (u + d, v + d, w, h),
            "left": (u + d + w, v + d, d, h),
            "back": (u + 2 * d + w, v + d, w, h),
        }

    @property
    def wrap(self) -> tuple:
        """(x0, y0, width, height) of the contiguous right|front|left|back strip."""
        return (self.u, self.v + self.d, 2 * self.d + 2 * self.w, self.h)


def parts_for(model: str) -> dict:
    """Base + overlay parts for model 'classic' or 'slim' (arm width 4 or 3)."""
    aw = 3 if model == "slim" else 4
    ps = [
        Part("head", 0, 0, 8, 8, 8, "base"),
        Part("hat", 32, 0, 8, 8, 8, "overlay"),
        Part("body", 16, 16, 8, 12, 4, "base"),
        Part("jacket", 16, 32, 8, 12, 4, "overlay"),
        Part("right_arm", 40, 16, aw, 12, 4, "base"),
        Part("right_sleeve", 40, 32, aw, 12, 4, "overlay"),
        Part("left_arm", 32, 48, aw, 12, 4, "base"),
        Part("left_sleeve", 48, 48, aw, 12, 4, "overlay"),
        Part("right_leg", 0, 16, 4, 12, 4, "base"),
        Part("right_pants", 0, 32, 4, 12, 4, "overlay"),
        Part("left_leg", 16, 48, 4, 12, 4, "base"),
        Part("left_pants", 0, 48, 4, 12, 4, "overlay"),
    ]
    return {p.name: p for p in ps}


OVERLAY_OF = {"head": "hat", "body": "jacket", "right_arm": "right_sleeve", "left_arm": "left_sleeve",
              "right_leg": "right_pants", "left_leg": "left_pants"}


def base_mask(model: str) -> Image.Image:
    """'L' mask (255 = pixel belongs to a base-layer face) for the given model."""
    m = Image.new("L", (SKIN_W, SKIN_H), 0)
    dr = ImageDraw.Draw(m)
    for p in parts_for(model).values():
        if p.layer != "base":
            continue
        for (x, y, w, h) in p.faces().values():
            dr.rectangle([x, y, x + w - 1, y + h - 1], fill=255)
    return m


def overlay_mask(model: str) -> Image.Image:
    m = Image.new("L", (SKIN_W, SKIN_H), 0)
    dr = ImageDraw.Draw(m)
    for p in parts_for(model).values():
        if p.layer != "overlay":
            continue
        for (x, y, w, h) in p.faces().values():
            dr.rectangle([x, y, x + w - 1, y + h - 1], fill=255)
    return m


def validate_skin(img: Image.Image, model: str) -> list:
    """Returns a list of problems (empty = valid): size/mode, base layer fully opaque,
    nothing drawn outside the face rectangles."""
    errs = []
    if img.size != (64, 64):
        errs.append(f"size {img.size} != (64, 64)")
        return errs
    img = img.convert("RGBA")
    bm, om = base_mask(model), overlay_mask(model)
    px, b, o = img.load(), bm.load(), om.load()
    bad_alpha = stray = 0
    for y in range(64):
        for x in range(64):
            a = px[x, y][3]
            if b[x, y] and a != 255:
                bad_alpha += 1
            if not b[x, y] and not o[x, y] and a != 0:
                stray += 1
    if bad_alpha:
        errs.append(f"{bad_alpha} base-layer pixels not fully opaque")
    if stray:
        errs.append(f"{stray} pixels outside any face")
    return errs


def detect_model(img: Image.Image) -> str:
    """Heuristic: slim skins leave the 4th column of the arm front/back (x=54..55 / 50..51,
    y=20..31 for the right arm) transparent."""
    img = img.convert("RGBA")
    px = img.load()
    alpha = sum(px[x, y][3] for x in (54, 55) for y in range(20, 32))
    return "slim" if alpha == 0 else "classic"


# ------------------------------------------------------------------------------------------
# 3D model (mirrors PlayerModel.createMesh for 1.21)
# ------------------------------------------------------------------------------------------

@dataclass
class Box:
    tex: Part
    origin: tuple          # (x, y, z) min corner before inflation, part-local
    size: tuple            # (w, h, d)
    inflate: float
    pivot: tuple           # part pivot in model space
    rot_z: float = 0.0     # radians (rotation around the pivot, about the model Z axis)


def player_boxes(model: str, pose: str = "tpose") -> list:
    """Boxes of the player model. pose 'tpose' raises both arms horizontally,
    'stand' keeps them down."""
    P = parts_for(model)
    slim = model == "slim"
    aw = 3 if slim else 4
    ay = 2.5 if slim else 2.0
    arm_rot = math.radians(90) if pose == "tpose" else 0.0
    boxes = [
        Box(P["head"], (-4, -8, -4), (8, 8, 8), 0.0, (0, 0, 0)),
        Box(P["hat"], (-4, -8, -4), (8, 8, 8), 0.5, (0, 0, 0)),
        Box(P["body"], (-4, 0, -2), (8, 12, 4), 0.0, (0, 0, 0)),
        Box(P["jacket"], (-4, 0, -2), (8, 12, 4), 0.25, (0, 0, 0)),
        Box(P["right_arm"], (-(aw - 1), -2, -2), (aw, 12, 4), 0.0, (-5, ay, 0), arm_rot),
        Box(P["right_sleeve"], (-(aw - 1), -2, -2), (aw, 12, 4), 0.25, (-5, ay, 0), arm_rot),
        Box(P["left_arm"], (-1, -2, -2), (aw, 12, 4), 0.0, (5, ay, 0), -arm_rot),
        Box(P["left_sleeve"], (-1, -2, -2), (aw, 12, 4), 0.25, (5, ay, 0), -arm_rot),
        Box(P["right_leg"], (-2, 0, -2), (4, 12, 4), 0.0, (-1.9, 12, 0)),
        Box(P["right_pants"], (-2, 0, -2), (4, 12, 4), 0.25, (-1.9, 12, 0)),
        Box(P["left_leg"], (-2, 0, -2), (4, 12, 4), 0.0, (1.9, 12, 0)),
        Box(P["left_pants"], (-2, 0, -2), (4, 12, 4), 0.25, (1.9, 12, 0)),
    ]
    return boxes


def _box_polygons(b: Box):
    """Yields (corners[4] in part space, uv[4], normal) like ModelPart.Cube."""
    i, j = b.tex.u, b.tex.v
    k, l, m = b.size
    f, g, h = b.origin
    s, t, u = f + k, g + l, h + m
    n = b.inflate
    f, g, h, s, t, u = f - n, g - n, h - n, s + n, t + n, u + n
    v1, v2, v3, v4 = (f, g, h), (s, g, h), (s, t, h), (f, t, h)
    v5, v6, v7, v8 = (f, g, u), (s, g, u), (s, t, u), (f, t, u)
    w_, x_, y_, z_ = i, i + m, i + m + k, i + m + k + k
    aa, ab = i + m + k + m, i + m + k + m + k
    ac, ad, ae = j, j + m, j + m + l
    polys = [
        ([v6, v5, v1, v2], (x_, ac, y_, ad), (0, -1, 0)),   # DOWN (model -Y = top of the part)
        ([v3, v4, v8, v7], (y_, ad, z_, ac), (0, 1, 0)),    # UP (bottom of the part)
        ([v1, v5, v8, v4], (w_, ad, x_, ae), (-1, 0, 0)),   # WEST (character's right)
        ([v2, v1, v4, v3], (x_, ad, y_, ae), (0, 0, -1)),   # NORTH (front)
        ([v6, v2, v3, v7], (y_, ad, aa, ae), (1, 0, 0)),    # EAST (character's left)
        ([v5, v6, v7, v8], (aa, ad, ab, ae), (0, 0, 1)),    # SOUTH (back)
    ]
    for verts, (u1, vv1, u2, vv2), normal in polys:
        # ModelPart.Polygon: verts[0]->(u2,v1) [1]->(u1,v1) [2]->(u1,v2) [3]->(u2,v2)
        yield verts, ((u2, vv1), (u1, vv1), (u1, vv2), (u2, vv2)), normal


def _rot_z(p, ang, pivot):
    c, s = math.cos(ang), math.sin(ang)
    x, y, z = p
    return (pivot[0] + x * c - y * s, pivot[1] + x * s + y * c, pivot[2] + z)


def model_quads(model: str, pose: str = "tpose", layers=("base", "overlay")):
    """Texel quads [(corners3d[4], (tx, ty), normal3d, layer)] of the whole player."""
    quads = []
    for b in player_boxes(model, pose):
        if b.tex.layer not in layers:
            continue
        for verts, uvs, normal in _box_polygons(b):
            (ua, va), (ub, vb), (uc, vc), _ = uvs   # p0=(u2,v1) p1=(u1,v1) p2=(u1,v2)
            P0, P1, P2 = verts[0], verts[1], verts[2]
            du, dv = ua - ub, vc - vb               # du along P1->P0, dv along P1->P2
            if du == 0 or dv == 0:
                continue
            nu, nv = int(round(abs(du))), int(round(abs(dv)))
            n_rot = _rot_z(normal, b.rot_z, (0, 0, 0))
            for a in range(nu):
                for c in range(nv):
                    # texel (ub + a, vb + c) if du,dv > 0; handle flipped axes
                    tx = int(min(ub, ua)) + a
                    ty = int(min(vb, vc)) + c
                    corners = []
                    for (uu, vv) in ((tx, ty), (tx + 1, ty), (tx + 1, ty + 1), (tx, ty + 1)):
                        fu = (uu - ub) / du
                        fv = (vv - vb) / dv
                        p = tuple(P1[q] + fu * (P0[q] - P1[q]) + fv * (P2[q] - P1[q]) for q in range(3))
                        corners.append(_rot_z(p, b.rot_z, (0, 0, 0)))
                    corners = [(c3[0] + b.pivot[0], c3[1] + b.pivot[1], c3[2] + b.pivot[2]) for c3 in corners]
                    quads.append((corners, (tx, ty), n_rot, b.tex.layer))
    return quads


VIEWS = {
    # name: camera direction (from the camera towards the model), model space
    "front": (0.0, 0.0, 1.0),     # camera in front of the face (at -Z) looking +Z
    "back": (0.0, 0.0, -1.0),
    "left": (-1.0, 0.0, 0.0),     # camera on the character's left (+X) looking -X
    "right": (1.0, 0.0, 0.0),     # camera on the character's right (-X) looking +X
}


def render_view(skin: Image.Image, model: str, view, scale: int = 8, pose: str = "tpose",
                size=None, bg=(0, 0, 0, 0), layers=("base", "overlay"), yaw_deg: float = None):
    """Orthographic render. `view` is a VIEWS key or a direction tuple; yaw_deg (optional)
    builds a direction rotated around the vertical axis (0 = front, 90 = left side)."""
    skin = skin.convert("RGBA")
    px = skin.load()
    if yaw_deg is not None:
        a = math.radians(yaw_deg)
        d = (-math.sin(a), 0.0, math.cos(a))
    else:
        d = VIEWS[view] if isinstance(view, str) else view
    up = (0.0, -1.0, 0.0)
    # right = d x up
    r = (d[1] * up[2] - d[2] * up[1], d[2] * up[0] - d[0] * up[2], d[0] * up[1] - d[1] * up[0])
    rl = math.sqrt(sum(c * c for c in r)) or 1.0
    r = tuple(c / rl for c in r)
    quads = model_quads(model, pose, layers)
    items = []
    for corners, (tx, ty), n, layer in quads:
        if sum(n[q] * d[q] for q in range(3)) >= -1e-6:
            continue  # back-face
        col = px[tx, ty]
        if layer == "base":
            col = (col[0], col[1], col[2], 255)
        elif col[3] < 26:   # the client renders the outer layer with an alpha cutout
            continue
        depth = sum(sum(c[q] * d[q] for q in range(3)) for c in corners) / 4.0
        pts = [(sum(c[q] * r[q] for q in range(3)), c[1]) for c in corners]
        # overlay drawn after base at equal depth (it is inflated, so it is nearer anyway)
        items.append((-depth, 0 if layer == "base" else 1, pts, col))
    items.sort(key=lambda it: (it[0], it[1]))
    xs = [p[0] for it in items for p in it[2]] or [0]
    ys = [p[1] for it in items for p in it[2]] or [0]
    minx, maxx, miny, maxy = min(xs), max(xs), min(ys), max(ys)
    if size is None:
        size = (int(math.ceil((maxx - minx) * scale)) + 2, int(math.ceil((maxy - miny) * scale)) + 2)
    W, H = size
    ox = W / 2.0 - (minx + maxx) / 2.0 * scale
    oy = H / 2.0 - (miny + maxy) / 2.0 * scale
    img = Image.new("RGBA", (W, H), bg)
    dr = ImageDraw.Draw(img)
    for _, _, pts, col in items:
        poly = [(ox + x * scale, oy + y * scale) for x, y in pts]
        xs_ = [p[0] for p in poly]
        ys_ = [p[1] for p in poly]
        x0, x1 = round(min(xs_)), round(max(xs_)) - 1
        y0, y1 = round(min(ys_)), round(max(ys_)) - 1
        axis_aligned = len({round(v, 3) for v in xs_}) <= 2 and len({round(v, 3) for v in ys_}) <= 2
        if not axis_aligned:  # oblique views (yaw_deg): plain polygon fill
            dr.polygon(poly, fill=col[:3] + (255,))
            continue
        if x1 < x0 or y1 < y0:
            continue
        if col[3] == 255:
            dr.rectangle([x0, y0, x1, y1], fill=col)
        else:  # translucent outer-layer pixel: alpha-blend
            tile = Image.new("RGBA", (x1 - x0 + 1, y1 - y0 + 1), col)
            img.alpha_composite(tile, (x0, y0))
    return img
