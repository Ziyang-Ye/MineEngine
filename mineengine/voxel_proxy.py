"""Minecraft voxel proxy: a block world -> per-view G-buffer
(logdepth, shade, block, pid, edge, btype, bstate, face, fx; docs/whitebox_channels.md) with nvdiffrast.
World: dense uint16 block-id grid over a box (Minecraft axes x east, y up, z south; 1 block = 1 m). Only faces between a
solid block and air are meshed (one quad = 2 triangles per visible face), each triangle tagged with its block id and
axis normal, so the rasteriser's triangle id gives depth, normal and block type per pixel. Entities (players, mobs) are
axis-aligned boxes drawn with entity id + 1 in the id buffer (players 0.6 x 1.8 x 0.6 m, sneaking 1.5 m).
Camera: Mineflayer convention (yaw 0 = north / -z, positive yaw turns toward west / -x; pitch positive = up; radians),
eye 1.62 m above the feet (1.27 sneaking); vertical FOV `vfov` degrees (Minecraft default 70). The yaw/pitch signs are
the documented Mineflayer ones and are checked against video before use.
G-buffer v2 (docs/whitebox_channels.md): StateMesher(reg, states) draws a grid of
palette indices (cap_reader.BlockTimeline.states_at) from the client jar's models (mineengine.mc_registry.Registry):
  cube     a state whose model has a 0..16 element without rotation (stone, planks, glass, leaves, snow layers=8, double
           slabs, shulker boxes, unknown types) = the fast path: only faces toward a non-cube cell (box border = edge
           repetition, as mesh_world); cubes are the only occluders;
  model    every other state: the faces of its model elements (first alternative of each applied part; multipart combined),
           element rotation (right-handed about origin, rescale 1 / cos on the other two axes: cross plants at 45 deg),
           the variant's x-then-y rotation about the block centre (mc_registry.rot), each face cropped to the opaque part of
           its texture (reg.alpha, mc_registry "Geometry"), coincident duplicate faces dropped; block entities (no jar
           elements) from be_boxes (chests incl. double halves, signs, hanging signs, banners, beds, heads, conduit, decorated
           pot, end portal), else cube;
  culling  per state and side a 16 x 16 boundary mask (texel centres covered by its opaque faces lying on that cell side:
           cubes full, a snow layer the bottom rows, a stair's back full, plants / torches none); a cube face is drawn unless
           the neighbour's opposite mask is full, a model face with a cullface unless the neighbour's opposite mask covers the
           face's rectangle (snow layers / slabs / same stairs side by side hide each other, a stair's back stays where the
           neighbour's step leaves it open), a fluid face unless that mask is full;
  fluid    water / lava / bubble_column: a volume of height (8 - level) / 9 (level >= 8 falling = 1), 1 under the same
           fluid; faces toward cells without that fluid (sides above a lower same-fluid neighbour too);
           waterlogged=true states and seagrass / tall_seagrass / kelp / kelp_plant are their model + the surface (top face at
           8 / 9, none under the same fluid) of a level-0 water volume carrying the palette entry "minecraft:water[level=0]"
           (implied); its sides / bottom are not drawn: they would box the model in at a water / air border and share planes with
           the model's boundary faces (depth ties that nvdiffrast resolves differently from raster_cpu);
  none     air, cave_air, void_air, barrier, light, structure_void, moving_piston (and models without faces).
  Per triangle: legacy id (legacy_of(state): the caller's cap_palette id, so block / block_ext keep their meaning; cap2proxy
  gives decorations now drawn their material id or UNKNOWN, never 0), btype = index in type_table(reg) = [""] + sorted(registry
  types) (0 = nothing, UNKNOWN_TYPE 65535 = a type outside the registry), bstate = index in sm.palette (1-based episode state
  palette; 0 = nothing), world normal, cell (the world cell whose geometry it is). VoxelRenderer.render(mesh, ...) also returns
  btype / bstate (uint16), face (uint8: 0 none, 1..6 = +x, -x, +y, -y, +z, -z of the world normal turned toward the camera,
  dominant axis for rotated faces; entity boxes too) and fx (uint16); mesh_world's 4-tuple meshes still render (btype / bstate /
  fx 0).
  update   StateMesher.update(mesh, grid, changed cells) re-meshes only the 16 x 16 columns (full height) within one cell of a
           change, each with a one-cell halo, and keeps the other quads (= mesh(grid) up to the quad order): a state change costs
           a few ms instead of a whole remesh.
  fx       u16 bit field (docs/whitebox_channels.md): bits 0..3 = destroy stage + 1 (1..10; 0 = none) on the pixels of the
           dug cell's geometry (render(fx=[[(cell, stage + 1)]]): per-triangle cell, entities never); bits 4.. reserved in this
           order (on_fire, hurt, glowing, frozen, wet, glint), not filled yet.
Renderers: VoxelRenderer() = nvdiffrast on CUDA (world mesh uploaded once per mesh object, vertices transformed on the
device); VoxelRenderer("cpu", raster="cpu") = the same channel code on a numpy rasteriser (raster_cpu: near-plane clipping,
pixel-centre coverage, perspective-correct depth, nearest hit per pixel) - cap_selftest --cpu-render and CPU-only runs.
"""
import math
import re

import numpy as np
import torch

AIR = 0
FLAT_SURFACES = ("grass_block", "sand", "snow_block", "red_sand", "mycelium")  # SolarisEngine flat presets (to confirm)


def flat_world(box_min, box_max, surface_id=1, stone_id=2, dirt_id=3, bedrock_id=4, ground_y=63):
    """Superflat column: bedrock at y = -64, stone up to ground_y - 3, 2 dirt, one surface block at ground_y."""
    (x0, y0, z0), (x1, y1, z1) = box_min, box_max
    g = np.zeros((x1 - x0, y1 - y0, z1 - z0), np.uint16)
    for y in range(y0, y1):
        v = surface_id if y == ground_y else dirt_id if ground_y - 2 <= y < ground_y else stone_id if -64 < y < ground_y - 2 else bedrock_id if y == -64 else AIR
        g[:, y - y0, :] = v
    return g


FACES = [((1, 0, 0), [(1, 0, 0), (1, 1, 0), (1, 1, 1), (1, 0, 1)]), ((-1, 0, 0), [(0, 0, 1), (0, 1, 1), (0, 1, 0), (0, 0, 0)]),
         ((0, 1, 0), [(0, 1, 0), (0, 1, 1), (1, 1, 1), (1, 1, 0)]), ((0, -1, 0), [(0, 0, 1), (0, 0, 0), (1, 0, 0), (1, 0, 1)]),
         ((0, 0, 1), [(1, 0, 1), (1, 1, 1), (0, 1, 1), (0, 0, 1)]), ((0, 0, -1), [(0, 0, 0), (0, 1, 0), (1, 1, 0), (1, 0, 0)])]


def mesh_world(grid, origin):
    """Visible faces of a block grid -> vertices [V, 3] (world, metres), faces [F, 3], per-face block id and normal."""
    solid = grid != AIR
    verts, faces, bid, nrm = [], [], [], []
    pad = np.pad(solid, 1, mode="edge")                                    # outside the box = same as its border (no cliffs)
    for n, quad in FACES:
        nb = pad[1 + n[0]:pad.shape[0] - 1 + n[0], 1 + n[1]:pad.shape[1] - 1 + n[1], 1 + n[2]:pad.shape[2] - 1 + n[2]]
        vis = solid & ~nb
        idx = np.argwhere(vis)
        if not len(idx):
            continue
        base = idx + np.asarray(origin)
        q = np.asarray(quad, np.float32)
        v = (base[:, None, :] + q[None]).reshape(-1, 3)
        k0 = sum(len(x) for x in verts)
        o = np.arange(len(idx))[:, None] * 4 + k0
        f = np.concatenate([o + [0, 1, 2], o + [0, 2, 3]], 0)
        verts.append(v); faces.append(f)
        ids = grid[tuple(idx.T)]
        bid.append(np.concatenate([ids, ids])); nrm.append(np.tile(np.asarray(n, np.float32), (2 * len(idx), 1)))
    return (np.concatenate(verts).astype(np.float32), np.concatenate(faces).astype(np.int32),
            np.concatenate(bid).astype(np.int32), np.concatenate(nrm))


def box_mesh(c, size):
    """Axis-aligned box: bottom centre c (x, y, z), size (sx, sy, sz) -> 8 vertices, 12 triangles."""
    sx, sy, sz = size
    v = np.array([[c[0] + dx * sx / 2, c[1] + dy * sy, c[2] + dz * sz / 2] for dy in (0, 1) for dz in (-1, 1) for dx in (-1, 1)], np.float32)
    f = np.array([[0, 1, 3], [0, 3, 2], [4, 6, 7], [4, 7, 5], [0, 4, 5], [0, 5, 1], [2, 3, 7], [2, 7, 6], [0, 2, 6], [0, 6, 4], [1, 5, 7], [1, 7, 3]], np.int32)
    return v, f


def camera_basis(yaw, pitch):
    """Mineflayer yaw / pitch (radians) -> forward, right, up in Minecraft world axes (x east, y up, z south)."""
    f = np.array([-math.sin(yaw) * math.cos(pitch), math.sin(pitch), -math.cos(yaw) * math.cos(pitch)])
    r = np.cross(f, [0.0, 1.0, 0.0]); r /= np.linalg.norm(r) + 1e-12
    u = np.cross(r, f)
    return f, r, u


# ---- block-state geometry (G-buffer v2) ---------------------------------------------------------------------------------
DIRS = ("east", "west", "up", "down", "south", "north")                         # = FACES order = face channel 1..6 (+x, -x, +y, -y, +z, -z)
DV = np.array([n for n, _ in FACES], np.int64)
DIX = {d: i for i, d in enumerate(DIRS)}
NONE, CUBE, MODEL, FLUID = 0, 1, 2, 3
INVISIBLE = {"air", "cave_air", "void_air", "barrier", "light", "structure_void", "moving_piston"}
IMPLIED_WATER = {"seagrass", "tall_seagrass", "kelp", "kelp_plant"}
FLUIDS = {"water": 1, "bubble_column": 1, "lava": 2}
WATER0 = "minecraft:water[level=0]"
UNKNOWN_TYPE = 65535
CHUNK, QUAD_KINDS, NOCELL = 16, ("cube", "model", "fluid"), -(1 << 30)          # StateMesher.update columns; per-quad kind codes; "no cell"
FX_DIG = 15                                                                       # fx bits 0..3: destroy stage + 1 (module docstring "fx")
H4 = {"north": 0, "east": 90, "south": 180, "west": 270}


def parse_state(s):
    """'minecraft:oak_door[half=lower,open=true]' -> ('oak_door', {'half': 'lower', 'open': 'true'})"""
    m = re.fullmatch(r"\s*([^\[\s]+)\s*(?:\[(.*)\])?\s*", str(s))
    if not m:
        return "", {}
    t = m.group(1).split(":", 1)[-1].lower()
    return t, dict(kv.split("=", 1) for kv in (m.group(2) or "").split(",") if "=" in kv)


def type_table(reg):
    """btype table: [""] + sorted registry type names (index 0 = nothing)."""
    return [""] + sorted(reg.types)


def _vrot(x, y):
    """mc_registry.rot as a matrix: blockstate x rotation, then y (90-degree steps)."""
    rx, ry = np.array([[1, 0, 0], [0, 0, 1], [0, -1, 0]], float), np.array([[0, 0, -1], [0, 1, 0], [1, 0, 0]], float)
    return np.linalg.matrix_power(ry, (int(y) // 90) % 4) @ np.linalg.matrix_power(rx, (int(x) // 90) % 4)


def _erot(axis, deg):
    """right-handed rotation about a model axis."""
    a = math.radians(float(deg)); c, s = math.cos(a), math.sin(a)
    return {"x": np.array([[1, 0, 0], [0, c, -s], [0, s, c]]), "y": np.array([[c, 0, s], [0, 1, 0], [-s, 0, c]]),
            "z": np.array([[c, -s, 0], [s, c, 0], [0, 0, 1]])}[axis]


def face_corners(f, t, fd):
    """element box (1/16 units) -> the corners of face fd as seen from outside: top-left, top-right, bottom-right, bottom-left
    (the order of MC's default uv: s left -> right, t top -> bottom)."""
    (fx, fy, fz), (tx, ty, tz) = f, t
    return {"north": [(tx, ty, fz), (fx, ty, fz), (fx, fy, fz), (tx, fy, fz)], "south": [(fx, ty, tz), (tx, ty, tz), (tx, fy, tz), (fx, fy, tz)],
            "west": [(fx, ty, fz), (fx, ty, tz), (fx, fy, tz), (fx, fy, fz)], "east": [(tx, ty, tz), (tx, ty, fz), (tx, fy, fz), (tx, fy, tz)],
            "up": [(fx, ty, fz), (tx, ty, fz), (tx, ty, tz), (fx, ty, tz)], "down": [(fx, fy, tz), (tx, fy, tz), (tx, fy, fz), (fx, fy, fz)]}[fd]


def element_quads(e, x=0, y=0, alpha=None, rot=None):
    """one model element -> [(quad [4, 3] in block units, normal [3], cull direction index or -1, opaque)]; alpha = {face: box}
    of reg.alpha ([] drops the face; a face absent = every texel opaque); rot = an extra right-handed y rotation in degrees about
    the centre (block entities)."""
    f, t = np.asarray(e["from"], float), np.asarray(e["to"], float)
    M = _vrot(x, y) if rot is None else _erot("y", rot)
    er = e.get("rot") or e.get("rotation")
    R, sc, o = np.eye(3), np.ones(3), np.full(3, 8.0)
    if er:
        R, o = _erot(er["axis"], er["angle"]), np.asarray(er.get("origin", (8, 8, 8)), float)
        if er.get("rescale") and abs(math.cos(math.radians(er["angle"]))) > 1e-6:
            sc = np.where(np.array(["x", "y", "z"]) == er["axis"], 1.0, 1.0 / abs(math.cos(math.radians(er["angle"]))))
    out = []
    for fd, q in (e.get("faces") or {}).items():
        c = np.asarray(face_corners(f, t, fd), float)
        b = (alpha or {}).get(fd)
        if b is not None:
            if not b:
                continue
            s0, t0, s1, t1 = b; P = lambda s_, t_: c[0] + s_ * (c[1] - c[0]) + t_ * (c[3] - c[0])   # noqa: E731
            c = np.stack([P(s0, t0), P(s1, t0), P(s1, t1), P(s0, t1)])
        c = ((c - o) @ R.T) * sc + o
        c = (c - 8.0) @ M.T + 8.0
        if np.linalg.norm(np.cross(c[1] - c[0], c[3] - c[0])) < 1e-6:
            continue
        n = M @ (R @ DV[DIX[fd]].astype(float))
        cu = q.get("cull") or q.get("cullface")
        cd = -1
        if cu in DIX:
            v = M @ DV[DIX[cu]].astype(float); cd = int(np.argmax([v @ d for d in DV]))
        out.append((c / 16.0, n / (np.linalg.norm(n) + 1e-12), cd, b is None))
    return out


def _box(f, t):
    return {"from": list(f), "to": list(t), "faces": {d: {} for d in DIRS}}


def be_boxes(t, p):
    """block entities without jar elements -> ([element boxes], y rotation in degrees (right-handed) or None, variant y) or None
    (drawn as a cube): a hand table of their renderers' rough shapes."""
    fy = H4.get(p.get("facing"), 0)
    rr = -22.5 * int(p["rotation"]) if str(p.get("rotation", "")).isdigit() else 0.0
    if t in ("chest", "trapped_chest", "ender_chest"):
        f, to = [1, 0, 1], [15, 14, 15]
        pt = p.get("type", "single")
        if pt in ("left", "right"):                                              # the other half: clockwise of facing for left (ChestBlock)
            d = ["north", "east", "south", "west"][(["north", "east", "south", "west"].index(p.get("facing", "north")) + (1 if pt == "left" else 3)) % 4]
            {"east": lambda: to.__setitem__(0, 16), "west": lambda: f.__setitem__(0, 0), "south": lambda: to.__setitem__(2, 16), "north": lambda: f.__setitem__(2, 0)}[d]()
        return [_box(f, to)], None, 0
    if re.fullmatch(r".+_wall_hanging_sign", t):
        return [_box((1, 0, 7), (15, 10, 9)), _box((0, 14, 6), (16, 16, 10))], None, fy
    if re.fullmatch(r".+_hanging_sign", t):
        return [_box((1, 0, 7), (15, 10, 9))], rr, 0
    if re.fullmatch(r".+_wall_sign", t):
        return [_box((0, 4.67, 14.67), (16, 12.67, 16))], None, fy
    if re.fullmatch(r".+_sign", t):
        return [_box((7.33, 0, 7.33), (8.67, 9.33, 8.67)), _box((0, 9.33, 7.33), (16, 17.33, 8.67))], rr, 0
    if re.fullmatch(r".+_wall_banner", t):
        return [_box((-2, -12, 15), (18, 28, 16))], None, fy
    if re.fullmatch(r".+_banner", t):
        return [_box((7.5, 0, 7.5), (8.5, 42, 8.5)), _box((-2, 10, 8.5), (18, 42, 9.5))], rr, 0
    if t.endswith("_bed"):
        return [_box((0, 0, 0), (16, 9, 16))], None, 0
    if t in ("dragon_head", "dragon_wall_head"):
        return None
    if t.endswith("_wall_head") or t.endswith("_wall_skull"):
        return [_box((4, 4, 8), (12, 12, 16))], None, fy
    if t.endswith("_head") or t.endswith("_skull"):
        return [_box((4, 0, 4), (12, 8, 12))], rr, 0
    if t == "conduit":
        return [_box((5, 5, 5), (11, 11, 11))], None, 0
    if t == "decorated_pot":
        return [_box((1, 0, 1), (15, 16, 15))], None, 0
    if t == "end_portal":
        return [_box((0, 0, 0), (16, 12, 16))], None, 0
    return None


class StateMesher:
    """block-state palette -> geometry (module docstring "G-buffer v2"). states = the episode's palette strings (index i of a
    states_at grid); legacy_of(state) -> legacy (cap_palette) id. Index len(states) = air (pad_grid's fill value: sm.air).
    sm.palette = [None] + one dict per distinct drawn state: state, type, vkey, gid, tgid, mgid, btype, geom (cube / model /
    fluid / be / cube_fallback / be_cube / none), legacy, known, exact (+ implied for the added water entry); sm.kind / legacy /
    btype / bstate / fluid / fh are per-index LUTs, sm.bmask [n, 6, 256] the boundary masks (docstring "culling"), sm.full their
    all()."""

    def __init__(self, reg, states, legacy_of=None):
        self.reg, self.types = reg, type_table(reg)
        self.tix = {t: i for i, t in enumerate(self.types)}
        legacy_of = legacy_of or (lambda s: 0)
        n = len(states) + 1; self.air = n - 1
        self.kind, self.fluid = np.zeros(n, np.int8), np.zeros(n, np.int8)
        self.legacy, self.btype, self.bstate = np.zeros(n, np.int32), np.zeros(n, np.int32), np.zeros(n, np.int32)
        self.fh, self.tmpl = np.zeros(n, np.float32), {}
        self.palette, self._pix, self.n_alpha = [None], {}, 0
        for i, s in enumerate(states):
            self._add(i, str(s), legacy_of)
        self.water_bstate = self._entry(WATER0, legacy_of, implied=True)["bstate"]
        self.water_btype, self.water_legacy = self.tix.get("water", UNKNOWN_TYPE), int(legacy_of(WATER0))
        self.legacy[self.air] = 0
        self.bmask = np.zeros((n, 6, 256), bool); self.bmask[self.kind == CUBE] = True
        for i, (Q, N, C, O, Rm) in self.tmpl.items():
            for q in range(len(Q)):
                d = int(np.argmax(N[q] @ DV.T))
                if O[q] and N[q] @ DV[d] > 0.999 and np.allclose(Q[q][:, d // 2], 1.0 if d % 2 == 0 else 0.0, atol=1e-4):
                    self.bmask[i, d] |= self._rect(Q[q], d)
        self.full, self._cov = self.bmask.all(-1), {}

    def _entry(self, s, legacy_of, implied=False, geom=None):
        if s in self._pix:
            return self.palette[self._pix[s]]
        t, _ = parse_state(s)
        o = self.reg.resolve(s)
        e = {"state": s, "type": t, "vkey": o.get("vkey"), "gid": o.get("gid"), "tgid": o.get("tgid"), "mgid": o.get("mgid"),
             "btype": self.tix.get(t, UNKNOWN_TYPE), "geom": geom or "fluid", "legacy": int(legacy_of(s)), "known": bool(o.get("known")),
             "exact": bool(o.get("exact")), "bstate": len(self.palette)}
        if implied:
            e["implied"] = True
        self._pix[s] = len(self.palette); self.palette.append(e)
        return e

    def _add(self, i, s, legacy_of):
        t, p = parse_state(s)
        r = self.reg.types.get(t)
        self.legacy[i] = int(legacy_of(s))
        if t in INVISIBLE or (r is not None and r.get("render") == "invisible"):
            return
        if r is None:
            geom, k = "cube_fallback", CUBE
        elif r["render"] == "fluid":
            geom, k = "fluid", FLUID
            self.fluid[i] = FLUIDS.get(t, 1)
            lv = int(p["level"]) if str(p.get("level", "")).isdigit() else 0
            self.fh[i] = 1.0 if lv >= 8 else (8 - min(lv, 7)) / 9.0
        elif r["render"] == "block_entity":
            bx = be_boxes(t, p)
            if bx is None:
                geom, k = "be_cube", CUBE
            else:
                q = [qq for b in bx[0] for qq in element_quads(b, 0, bx[2], rot=bx[1])]
                geom, k = "be", MODEL; self.tmpl[i] = self._pack(q)
        else:
            _, parts = self.reg.parts_of(s)
            if parts is None:
                geom, k = "cube_fallback", CUBE
            else:
                alts = [ps[0] for ps in parts if ps]
                els = [(e, a, j) for a in alts for j, e in enumerate(self.reg.models.get(a[0], {}).get("elements") or [])]
                if any([float(c) for c in e["from"]] == [0, 0, 0] and [float(c) for c in e["to"]] == [16, 16, 16] and not (e.get("rot") or e.get("rotation"))
                       for e, _, _ in els):
                    geom, k = "cube", CUBE
                else:
                    q = []
                    for e, a, j in els:
                        al = self.reg.alpha.get(a[0]) if getattr(self.reg, "alpha", None) else None
                        self.n_alpha += bool(al and al[j])
                        q += element_quads(e, a[1], a[2], al[j] if al else None)
                    geom, k = ("model", MODEL) if q else ("none", NONE)
                    if q:
                        self.tmpl[i] = self._pack(q)
        if k == NONE:
            return
        self.kind[i] = k
        e = self._entry(s, legacy_of, geom=geom)
        self.btype[i], self.bstate[i] = e["btype"], e["bstate"]
        if k == MODEL and (p.get("waterlogged") == "true" or t in IMPLIED_WATER):
            self.fluid[i], self.fh[i] = 1, 8 / 9.0

    @staticmethod
    def _rect(c, d):
        """quad (block units) -> its 16 x 16 texel-centre mask on the plane across side d (the other two axes in xyz order)."""
        u, v = [a for a in range(3) if a != d // 2]; g = np.arange(16) + 0.5
        lo, hi = c[:, [u, v]].min(0) * 16 - 1e-4, c[:, [u, v]].max(0) * 16 + 1e-4
        return (((g >= lo[0]) & (g <= hi[0]))[:, None] & ((g >= lo[1]) & (g <= hi[1]))[None, :]).reshape(-1)

    @classmethod
    def _pack(cls, q):
        """[(quad, normal, cull, opaque)] -> (Q [n, 4, 3], N [n, 3], C [n], O [n], Rm [n, 256] cull-side rectangle) without
        coincident duplicates."""
        keep, seen = [], set()
        for c, n, cd, op in q:
            key = tuple(sorted(map(tuple, np.round(c, 4).tolist())))
            if key not in seen:
                seen.add(key); keep.append((c, n, cd, op))
        if not keep:
            return np.zeros((0, 4, 3), np.float32), np.zeros((0, 3), np.float32), np.zeros(0, np.int64), np.zeros(0, bool), np.zeros((0, 256), bool)
        Q = np.stack([k[0] for k in keep]).astype(np.float32); C = np.array([k[2] for k in keep], np.int64)
        return (Q, np.stack([k[1] for k in keep]).astype(np.float32), C, np.array([k[3] for k in keep], bool),
                np.stack([cls._rect(Q[j], C[j]) if C[j] >= 0 else np.zeros(256, bool) for j in range(len(keep))]))

    def cover(self, s):
        """[n states, nq] True where a neighbour in state n hides quad q of state s (docstring "culling"; cached)."""
        if s not in self._cov:
            _, _, C, _, Rm = self.tmpl[s]
            bm = self.bmask[:, np.maximum(C, 0) ^ 1, :]                          # opposite side of each quad's cull side
            self._cov[s] = (C >= 0)[None] & Rm.any(-1)[None] & ~(Rm[None] & ~bm).any(-1)
        return self._cov[s]

    def _quads(self, S, org, emit=None):
        """palette-index grid S + its origin -> per-quad arrays {V [q, 4, 3] float32 world m, bid / btype / bstate int32, nrm
        [q, 3], cell [q, 3] int32 world cell, kind int8 (QUAD_KINDS)}; emit = (x0, x1, z0, z1): only the cells x0 <= x < x1,
        z0 <= z < z1 (local) get faces, the others are a halo that only culls / clips them (StateMesher.update)."""
        S = np.asarray(S); org = np.asarray(org, np.int64)
        K = self.kind[S]; SP = np.pad(S, 1, mode="edge")
        Em = True
        if emit is not None:
            Em = np.zeros((S.shape[0], 1, S.shape[2]), bool); Em[emit[0]:emit[1], :, emit[2]:emit[3]] = True
        out = []
        U = np.asarray([q for _, q in FACES], np.float32)

        def put(V, cl, s, nrm, b=None, t=None, l_=None, kind=0):
            m = len(V)
            if m:
                out.append((V, self.legacy[s] if l_ is None else np.full(m, l_), self.btype[s] if t is None else np.full(m, t),
                            self.bstate[s] if b is None else np.full(m, b), np.broadcast_to(np.asarray(nrm, np.float32), (m, 3)), cl + org, np.full(m, kind, np.int8)))

        def nb(idx, d, G):
            q = idx + 1 + DV[d]
            return G[q[:, 0], q[:, 1], q[:, 2]]
        occ = (K == CUBE) & Em
        for d in range(6):                                                      # cubes: faces whose neighbour's opposite side is not full
            sh = SP[1 + DV[d][0]:SP.shape[0] - 1 + DV[d][0], 1 + DV[d][1]:SP.shape[1] - 1 + DV[d][1], 1 + DV[d][2]:SP.shape[2] - 1 + DV[d][2]]
            idx = np.argwhere(occ & ~self.full[sh, d ^ 1])
            if len(idx):
                put((idx + org)[:, None, :] + U[d][None], idx, S[tuple(idx.T)], DV[d])
        mi = np.argwhere((K == MODEL) & Em)                                     # models: per state template, cullface vs the neighbour's mask
        if len(mi):
            ms = S[tuple(mi.T)]; nbs = np.stack([nb(mi, d, SP) for d in range(6)], 1)
            o = np.argsort(ms, kind="stable"); cut = np.flatnonzero(np.diff(ms[o])) + 1
            for sel in np.split(o, cut):
                s0 = int(ms[sel[0]]); Q, N, C = self.tmpl.get(s0, (np.zeros((0, 4, 3)), None, None, None, None))[:3]
                if not len(Q):
                    continue
                keep = ~self.cover(s0)[nbs[sel][:, np.maximum(C, 0)], np.arange(len(Q))[None]]
                ci, qi = np.nonzero(keep)
                put((mi[sel][ci] + org)[:, None, :] + Q[qi], mi[sel][ci], np.full(len(ci), s0), N[qi], kind=1)
        FL = self.fluid[S]; fi = np.argwhere((FL > 0) & Em)                     # fluids: volume faces toward cells without that fluid
        if len(fi):
            fh = self.fh[S].copy(); up = np.zeros_like(FL); up[:, :-1] = FL[:, 1:]; fh[(FL > 0) & (up == FL)] = 1.0
            FP, HP = np.pad(FL, 1, mode="edge"), np.pad(fh, 1, mode="edge")
            f0, h, s = FL[tuple(fi.T)], fh[tuple(fi.T)], S[tuple(fi.T)]
            imp = self.kind[s] != FLUID                                          # implied water of a model: its surface only
            for d in range(6):
                same, no, hn = nb(fi, d, FP) == f0, self.full[nb(fi, d, SP), d ^ 1], nb(fi, d, HP)
                if d == 2:
                    draw, lo, hi = ~same & ~(no & (h >= 1)), h, h
                elif d == 3:
                    draw, lo, hi = ~same & ~no & ~imp, np.zeros_like(h), np.zeros_like(h)
                else:
                    lo = np.where(same, hn, 0.0); draw, hi = ~no & (h > lo + 1e-4) & ~imp, h
                k = np.nonzero(draw)[0]
                if not len(k):
                    continue
                q = np.broadcast_to(U[d], (len(k), 4, 3)).copy()
                if d < 2 or d > 3:
                    q[..., 1] = np.where(q[..., 1] > 0.5, hi[k, None], lo[k, None])
                else:
                    q[..., 1] = hi[k, None]
                V = (fi[k] + org)[:, None, :] + q
                for m_, b_, t_, l_ in ((~imp[k], None, None, None), (imp[k], self.water_bstate, self.water_btype, self.water_legacy)):
                    if m_.any():
                        put(V[m_], fi[k][m_], s[k][m_], DV[d], b_, t_, l_, kind=2)
        if not out:
            return {k: np.zeros(sh_, dt) for k, sh_, dt in (("V", (0, 4, 3), np.float32), ("bid", 0, np.int32), ("btype", 0, np.int32), ("bstate", 0, np.int32),
                                                             ("nrm", (0, 3), np.float32), ("cell", (0, 3), np.int32), ("kind", 0, np.int8))}
        return {k: np.concatenate([o_[j] for o_ in out]).astype(dt) for j, (k, dt) in enumerate(
            (("V", np.float32), ("bid", np.int32), ("btype", np.int32), ("bstate", np.int32), ("nrm", np.float32), ("cell", np.int32), ("kind", np.int8)))}

    @staticmethod
    def _finish(P, shape, origin, cs):
        """per-quad arrays (+ chunk key) -> mesh dict (docstring mesh)."""
        q = len(P["bid"])
        base = dict(n_quads={QUAD_KINDS[k]: int(v) for k, v in enumerate(np.bincount(P["kind"], minlength=3)) if v}, quads=P, shape=tuple(int(v) for v in shape),
                    origin=np.asarray(origin, np.int64), cs=int(cs))
        if not q:                                                                # nothing drawn: one degenerate triangle keeps the rasterisers happy
            return dict(V=np.zeros((3, 3), np.float32), F=np.array([[0, 1, 2]], np.int32), bid=np.zeros(1, np.int32), btype=np.zeros(1, np.int32),
                        bstate=np.zeros(1, np.int32), nrm=np.array([[0, 1, 0]], np.float32), cell=np.full((1, 3), NOCELL, np.int32), **base)
        o = np.arange(q, dtype=np.int64)[:, None] * 4
        F = np.concatenate([o + [0, 1, 2], o + [0, 2, 3]], 0).astype(np.int32)
        two = lambda a: np.concatenate([a, a])                                   # noqa: E731  (F lists all first triangles, then all second ones)
        return dict(V=P["V"].reshape(-1, 3), F=F, bid=two(P["bid"]), btype=two(P["btype"]), bstate=two(P["bstate"]), nrm=two(P["nrm"]), cell=two(P["cell"]), **base)

    @staticmethod
    def _keys(P, origin, shape, cs):
        """chunk key of every quad: its cell's (x // cs) * ncz + z // cs over the grid (cs x cs columns, full height)."""
        c = P["cell"].astype(np.int64) - np.asarray(origin, np.int64)
        return (c[:, 0] // cs) * (-(-int(shape[2]) // cs)) + c[:, 2] // cs

    def mesh(self, sgrid, origin, cs=CHUNK):
        """palette-index grid [sx, sy, sz] (sm.air = air) + its origin -> mesh dict: V [4q, 3] float32 (world m), F [2q, 3]
        int32, per triangle bid (legacy id), btype, bstate (int32), nrm (float32 [2q, 3]), cell (int32 [2q, 3]: the world cell
        whose geometry it is; render's fx); n_quads per kind; quads / shape / origin / cs for update()."""
        P = self._quads(sgrid, origin); P["key"] = self._keys(P, origin, np.shape(sgrid), cs)
        return self._finish(P, np.shape(sgrid), origin, cs)

    def update(self, m, sgrid, cells):
        """incremental mesh(): m = a mesh() / update() result of a grid of the same shape and origin, sgrid = the new grid, cells
        [k, 3] = the local cells that changed -> the mesh of sgrid (= mesh(sgrid) up to the quad order). A face depends on its cell,
        the 6 neighbours and their fluid heights (their own column), so only the cs x cs columns within one cell (x / z) of a
        change are re-meshed, each with a one-cell halo (edge repetition at the grid border, as mesh); more than half the
        columns dirty -> mesh(). m["n_chunks"] = columns re-meshed (-1 = whole)."""
        S = np.asarray(sgrid); sx, _, sz = S.shape; cs = m["cs"]; ncz = -(-sz // cs); org = m["origin"]
        c = np.asarray(cells, np.int64).reshape(-1, 3)
        if not len(c):
            return m
        dx = np.array([-1, 0, 1])
        X, Z = np.clip(c[:, :1] + dx, 0, sx - 1) // cs, np.clip(c[:, 2:3] + dx, 0, sz - 1) // cs
        keys = np.unique((X[:, :, None] * ncz + Z[:, None, :]).ravel())
        if tuple(S.shape) != m["shape"] or len(keys) > max(4, (-(-sx // cs)) * ncz // 2):
            r = self.mesh(S, org, cs); r["n_chunks"] = -1
            return r
        Q = m["quads"]; keep = ~np.isin(Q["key"], keys); parts = [{k: v[keep] for k, v in Q.items()}]
        for k in keys.tolist():
            x0, z0 = (k // ncz) * cs, (k % ncz) * cs; x1, z1 = min(x0 + cs, sx), min(z0 + cs, sz)
            xi, zi = np.clip(np.arange(x0 - 1, x1 + 1), 0, sx - 1), np.clip(np.arange(z0 - 1, z1 + 1), 0, sz - 1)
            P = self._quads(S[xi][:, :, zi], org + [x0 - 1, 0, z0 - 1], emit=(1, 1 + x1 - x0, 1, 1 + z1 - z0))
            P["key"] = np.full(len(P["bid"]), k, np.int64); parts.append(P)
        r = self._finish({k: np.concatenate([p[k] for p in parts]) for k in Q}, S.shape, org, cs); r["n_chunks"] = len(keys)
        return r


def as_mesh(m):
    """mesh_world 4-tuple or StateMesher dict -> dict (btype / bstate 0, cell NOCELL for the 4-tuple)."""
    if isinstance(m, dict):
        return m
    V, F, bid, nrm = m
    z = np.zeros(len(F), np.int32)
    return dict(V=V, F=F, bid=bid, btype=z, bstate=z, nrm=nrm, cell=np.full((len(F), 3), NOCELL, np.int32))


# ---- rasterisers + channels -----------------------------------------------------------------------------------------------
def raster_cpu(pc, F, H, W, tw, th, near=0.05, far=500.0, chunk=6_000_000):
    """numpy rasteriser: camera-space vertices pc [V, 3] (OpenGL: x right, y up, z back), triangles F [T, 3] -> (tid int64
    [H, W] = triangle index + 1 (0 = background, row 0 = top), forward depth float32 [H, W]). Triangles are clipped at the
    near plane, covered pixel centres (both windings) are resolved to the nearest hit by one min over (depth bits, tid)."""
    pc = np.asarray(pc, np.float64); F = np.asarray(F, np.int64)
    zt = -pc[F, 2]; xt, yt = pc[F, 0], pc[F, 1]
    nb = (zt < near).sum(1)
    out = (xt > tw * zt).all(1) | (xt < -tw * zt).all(1) | (yt > th * zt).all(1) | (yt < -th * zt).all(1) | (zt > far).all(1)
    k0 = np.nonzero((nb == 0) & ~out)[0]
    tris, ids = [pc[F[k0]]], [k0]
    for nbk in (1, 2):                                                          # near-plane clipping: 1 behind -> 2 triangles, 2 behind -> 1
        kk = np.nonzero(nb == nbk)[0]
        if not len(kk):
            continue
        T = pc[F[kk]]; z = -T[..., 2]; beh = z < near
        if nbk == 2:
            a = np.argmin(beh, 1); A_ = T[np.arange(len(kk)), a]; za = z[np.arange(len(kk)), a]
            res = [A_]
            for j in (1, 2):
                b = (a + j) % 3; B = T[np.arange(len(kk)), b]; zb = z[np.arange(len(kk)), b]
                res.append(A_ + (B - A_) * ((za - near) / (za - zb))[:, None])
            tris.append(np.stack(res, 1)); ids.append(kk)
        else:
            c = np.argmax(beh, 1); C = T[np.arange(len(kk)), c]; zc = z[np.arange(len(kk)), c]
            A_, B = T[np.arange(len(kk)), (c + 1) % 3], T[np.arange(len(kk)), (c + 2) % 3]
            za, zb = z[np.arange(len(kk)), (c + 1) % 3], z[np.arange(len(kk)), (c + 2) % 3]
            Ac = A_ + (C - A_) * ((za - near) / (za - zc))[:, None]; Bc = B + (C - B) * ((zb - near) / (zb - zc))[:, None]
            tris += [np.stack([A_, B, Bc], 1), np.stack([A_, Bc, Ac], 1)]; ids += [kk, kk]
    T = np.concatenate(tris); I = np.concatenate(ids)
    z = -T[..., 2]
    sx = (T[..., 0] / (z * tw) + 1.0) * (W / 2.0); sy = (1.0 - T[..., 1] / (z * th)) * (H / 2.0)
    x0 = np.maximum(np.ceil(sx.min(1) - 0.5), 0).astype(np.int64); x1 = np.minimum(np.floor(sx.max(1) - 0.5), W - 1).astype(np.int64)
    y0 = np.maximum(np.ceil(sy.min(1) - 0.5), 0).astype(np.int64); y1 = np.minimum(np.floor(sy.max(1) - 0.5), H - 1).astype(np.int64)
    area = (sx[:, 2] - sx[:, 1]) * (sy[:, 0] - sy[:, 1]) - (sy[:, 2] - sy[:, 1]) * (sx[:, 0] - sx[:, 1])
    ok = (x0 <= x1) & (y0 <= y1) & (np.abs(area) > 1e-12)
    T, I, sx, sy, z, x0, x1, y0, y1, area = (v[ok] for v in (T, I, sx, sy, z, x0, x1, y0, y1, area))
    nx = x1 - x0 + 1; cnt = nx * (y1 - y0 + 1)
    buf = np.full(H * W, np.iinfo(np.int64).max, np.int64)
    cs = np.cumsum(cnt); starts = np.r_[0, cs[:-1]]
    b0 = 0
    while b0 < len(cnt):
        b1 = int(np.searchsorted(cs, cs[b0] - cnt[b0] + chunk, side="right")); b1 = max(b1, b0 + 1)
        sl = slice(b0, b1); n = int(cnt[sl].sum())
        rep = np.repeat(np.arange(b0, b1), cnt[sl]); off = np.arange(n) - np.repeat(starts[sl] - starts[b0], cnt[sl])
        px = x0[rep] + off % nx[rep]; py = y0[rep] + off // nx[rep]
        X, Y = px + 0.5, py + 0.5
        e0 = (sx[rep, 2] - sx[rep, 1]) * (Y - sy[rep, 1]) - (sy[rep, 2] - sy[rep, 1]) * (X - sx[rep, 1])
        e1 = (sx[rep, 0] - sx[rep, 2]) * (Y - sy[rep, 2]) - (sy[rep, 0] - sy[rep, 2]) * (X - sx[rep, 2])
        e2 = (sx[rep, 1] - sx[rep, 0]) * (Y - sy[rep, 0]) - (sy[rep, 1] - sy[rep, 0]) * (X - sx[rep, 0])
        a = area[rep]; w0, w1, w2 = e0 / a, e1 / a, e2 / a
        m = (w0 >= -1e-9) & (w1 >= -1e-9) & (w2 >= -1e-9)
        iz = w0[m] / z[rep[m], 0] + w1[m] / z[rep[m], 1] + w2[m] / z[rep[m], 2]
        key = (np.ascontiguousarray((1.0 / iz).astype(np.float32)).view(np.int32).astype(np.int64) << 32) | (I[rep[m]] + 1)
        np.minimum.at(buf, py[m] * W + px[m], key)
        b0 = b1
    hit = buf != np.iinfo(np.int64).max
    tid = np.where(hit, buf & 0xFFFFFFFF, 0).reshape(H, W)
    zz = np.where(hit, (buf >> 32), 0).astype(np.int32).view(np.float32).reshape(H, W)
    return tid, zz


class VoxelRenderer:
    def __init__(self, device="cuda", raster="nvdiffrast"):
        self.dev, self.raster, self._m = torch.device(device), raster, (None, None)
        if raster == "nvdiffrast":
            import nvdiffrast.torch as dr
            self.dr = dr
            self.ctx = dr.RasterizeCudaContext(device=self.dev)

    def _upload(self, world_mesh):
        """world mesh -> device tensors (cached per mesh object; a new mesh replaces the cache)."""
        if self._m[0] is world_mesh:
            return self._m[1]
        M = as_mesh(world_mesh); d = self.dev
        z0 = lambda a, dt: torch.tensor(np.r_[0, np.asarray(a, np.int64)], device=d, dtype=dt)  # noqa: E731  (row 0 = background)
        G = dict(V=torch.tensor(np.asarray(M["V"], np.float32), device=d), F=torch.tensor(np.asarray(M["F"], np.int32), device=d),
                 tb=z0(M["bid"], torch.int64), tt=z0(M["btype"], torch.int64), ts=z0(M["bstate"], torch.int64),
                 tn=torch.tensor(np.r_[[[0, 0, 0]], np.asarray(M["nrm"], np.float32)], device=d, dtype=torch.float32),
                 tc=torch.tensor(np.r_[[[NOCELL] * 3], np.asarray(M["cell"], np.int32)], device=d, dtype=torch.int32),
                 Vn=np.asarray(M["V"], np.float64), Fn=np.asarray(M["F"], np.int64), nt=len(M["F"]))
        self._m = (world_mesh, G)
        return G

    def render(self, world_mesh, cams, entities=(), H=192, W=336, vfov=70.0, fx=None):
        """world_mesh = mesh_world(...) or StateMesher.mesh(...); cams = [(eye xyz, yaw, pitch)]; entities = [(id, bottom-centre
        xyz, size)]; fx = per camera None or [(world cell xyz, fx value)] (module docstring "fx"). -> dict of numpy arrays [N, H,
        W]: logdepth (log scale of docs/whitebox_channels.md), shade (|n.v| affine), block (legacy block id, clipped to 255), pid
        (entity id + 1), edge (255 on block-face borders: the 1 m grid lines that coplanar neighbouring blocks would otherwise
        hide, about 1.5 px wide at any distance) uint8; btype, bstate, fx uint16 (0 off blocks), face uint8 (module docstring);
        normal [N, H, W, 3] uint8 (camera space, x right y down, toward the camera)."""
        G = self._upload(world_mesh); d = self.dev
        ev, ef, eid, enrm, off = [], [], [], [], len(G["Vn"])
        for i, c, s in entities:
            v, f = box_mesh(c, s); ev.append(v); ef.append(f + off); off += len(v); eid += [i + 1] * len(f)
            tri = v[f]; n = np.cross(tri[:, 1] - tri[:, 0], tri[:, 2] - tri[:, 0]); enrm.append(n / (np.linalg.norm(n, axis=1, keepdims=True) + 1e-9))
        ne = len(eid)
        zl = torch.zeros(ne, dtype=torch.int64, device=d)
        tb, tt, ts = (torch.cat([G[k], zl]) for k in ("tb", "tt", "ts"))
        tp = torch.cat([torch.zeros(G["nt"] + 1, dtype=torch.int64, device=d), torch.tensor(eid, dtype=torch.int64, device=d)])
        tn = torch.cat([G["tn"], torch.tensor(np.concatenate(enrm), dtype=torch.float32, device=d)]) if ne else G["tn"]
        tc = torch.cat([G["tc"], torch.full((ne, 3), NOCELL, dtype=torch.int32, device=d)])
        EV = np.concatenate(ev).astype(np.float32) if ne else np.zeros((0, 3), np.float32)
        EF = np.concatenate(ef).astype(np.int32) if ne else np.zeros((0, 3), np.int32)
        if self.raster == "nvdiffrast":
            V = torch.cat([G["V"], torch.tensor(EV, device=d)]); F = torch.cat([G["F"], torch.tensor(EF, device=d)]).contiguous()
        else:
            Vn, Fn = np.concatenate([G["Vn"], EV]), np.concatenate([G["Fn"], EF])
        th = math.tan(math.radians(vfov) / 2); tw = th * W / H
        out = {k: [] for k in ("logdepth", "shade", "block", "pid", "edge", "normal", "btype", "bstate", "face", "fx")}
        for j, (eye, yaw, pitch) in enumerate(cams):
            f, r, u = camera_basis(yaw, pitch)
            R = np.stack([r, u, -f])                                             # world -> OpenGL camera (x r, y u, z back)
            if self.raster == "nvdiffrast":
                pc = (V - torch.tensor(np.asarray(eye, np.float32), device=d)) @ torch.tensor(R.T, dtype=torch.float32, device=d)
                n_, fa = 0.05, 500.0
                clip = torch.stack([pc[:, 0] / tw, pc[:, 1] / th, (-(fa + n_) / (fa - n_)) * pc[:, 2] - 2 * fa * n_ / (fa - n_), -pc[:, 2]], 1).contiguous()
                rast, _ = self.dr.rasterize(self.ctx, clip[None], F, resolution=[H, W])
                tid = rast[0, ..., 3].long().flip(0)                             # 0 = background
                z, _ = self.dr.interpolate((-pc[:, 2:3]).contiguous()[None], rast, F); z = z[0, ..., 0].flip(0)
            else:
                tid_n, z_n = raster_cpu((Vn - np.asarray(eye, np.float64)) @ R.T, Fn, H, W, tw, th)
                tid, z = torch.from_numpy(tid_n).to(d), torch.from_numpy(z_n).to(d)
            g = self._channels(tid, z, eye, f, r, u, tw, th, H, W, tb, tt, ts, tp, tn)
            fv = torch.zeros_like(tid)
            for cell, v in ((fx[j] if fx is not None else None) or ()):          # fx: the pixels of a cell's geometry
                m = (tp[tid] == 0) & (tid > 0) & (tc[tid] == torch.tensor(np.asarray(cell, np.int64).astype(np.int32), device=d)).all(-1)
                fv = torch.where(m, fv | int(v), fv)
            g["fx"] = fv.cpu().numpy().astype(np.uint16)
            for k, v in g.items():
                out[k].append(v)
        return {k: np.stack(v) for k, v in out.items()}

    def _channels(self, tid, z, eye, f, r, u, tw, th, H, W, tb, tt, ts, tp, tn):
        """per-pixel triangle id + forward depth -> the G-buffer channels of one view (same code for both rasterisers)."""
        d = self.dev; tt_ = lambda v: torch.tensor(np.asarray(v), device=d, dtype=torch.float32)   # noqa: E731
        hit = tid > 0
        zin = (z / 0.0254).clamp_min(8)                                          # metres -> inches for the log-depth scale
        ld = torch.where(hit, (torch.log2(zin / 8) / 10).clamp(0, 1) * 254 + 1, torch.zeros_like(z))
        nw = tn[tid]                                                             # world normals
        ys, xs = torch.meshgrid(torch.arange(H, device=d), torch.arange(W, device=d), indexing="ij")
        xn = (xs + 0.5) / W * 2 - 1; yn = (ys + 0.5) / H * 2 - 1
        ray = tt_(f) + (xn * tw)[..., None] * tt_(r) - (yn * th)[..., None] * tt_(u)   # world ray of every pixel (forward component 1)
        dot = (nw * (ray / ray.norm(dim=-1, keepdim=True))).sum(-1)
        nv = torch.where((dot > 0)[..., None], -nw, nw)                          # the side facing the camera
        nc = torch.stack([(nv * tt_(r)).sum(-1), -(nv * tt_(u)).sum(-1), (nv * tt_(f)).sum(-1)], -1)   # camera x right, y down, z fwd
        c = dot.abs()
        ent = tp[tid] > 0
        sh = torch.where(hit, torch.where(ent, 0.35 + 0.65 * c, 0.25 + 0.75 * c), torch.zeros_like(c))
        wpt = tt_(eye)[None, None] + z[..., None] * ray                          # world point of every pixel
        fr = wpt - torch.floor(wpt); dd = torch.minimum(fr, 1 - fr)
        dd = torch.where(nw.abs() > 0.5, torch.full_like(dd, 9.0), dd).amin(-1)   # in-plane axes of the face only
        px = 1.5 * z * 2 * th / H                                               # 1.5 px in metres at that depth
        ax = nv.abs().argmax(-1); neg = nv.gather(-1, ax[..., None])[..., 0] < 0
        face = torch.where(hit, 1 + 2 * ax + neg.long(), torch.zeros_like(ax))
        npy = lambda v, dt: v.cpu().numpy().astype(dt)                           # noqa: E731
        return {"logdepth": npy(ld.round(), np.uint8), "shade": npy((sh * 255).round(), np.uint8), "block": npy(tb[tid].clamp(0, 255), np.uint8),
                "pid": npy(tp[tid].clamp(0, 255), np.uint8), "edge": npy(((dd < px) & hit & ~ent).long() * 255, np.uint8),
                "normal": npy(((nc * 0.5 + 0.5) * 255 * hit[..., None]).round(), np.uint8),
                "btype": npy(tt[tid].clamp(0, 65535), np.uint16), "bstate": npy(ts[tid].clamp(0, 65535), np.uint16), "face": npy(face, np.uint8)}
