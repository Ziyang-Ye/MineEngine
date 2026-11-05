"""Block registry from the Minecraft client jar (see
docs/whitebox_channels.md): assets/minecraft/blockstates + models of a vanilla jar, read as a zip (nothing extracted, no pixels copied)
-> type -> state variants -> models + textures, appearance groups, property roles, and a state-string lookup.
  Variants   'variants' keys as written ("facing=east,half=lower,hinge=left,open=false", "" = single); 'multipart' blocks
             (fences, walls, panes, redstone wire, fire, vines, ...): every combination of the properties named in the
             'when' clauses (OR / AND, "a|b"), domain = the values named there completed to true|false or to the smallest
             vanilla enum holding them (none|side|up, none|low|tall, x|y|z, 4 / 6 directions, none|small|large) or KNOWN
             (composter level 0..8, bamboo age 0..1, pink_petals flower_amount 1..4), else + "_other" (any value not
             named; none left in 1.21); combinations drawing nothing (wall up=false with no sides: the game never makes
             them) give the one empty group, skipped by the role flips. A type whose product exceeds CAP = 4096 keeps the
             base combination and its one-property flips only ("capped"; none in 1.21, largest chiseled_bookshelf 256).
  Models     parent chains merged (child textures override, the first 'elements' up the chain win), '#var' resolved to
             "block/oak_door_top" (a face texture is a variable name with or without '#', as in the game: heavy_core
             writes "all"); mhash = sha1 of the resolved elements (boxes, element rotation, face texture / uv / uv
             rotation / tintindex; not cullface). Element-less models (fluids, chests, signs, banners, beds, shulker
             boxes, heads, air, ...) draw nothing from the jar: EXC adds their code-side textures (entity/..., water_still).
  Groups     a variant = parts (one per applied blockstate entry; a weighted random list = alternatives) of (model, x, y,
             uvlock, weight); three global keys, ahash fixing the other two (infested_stone shares all three with stone):
               mshash  texture MULTISET = "appearance variant": (texture, tinted) counted per face, a part's
                       distinct alternative multisets summed (rotations of one model count once; fire's fire_0 / fire_1
                       alternatives both), + EXC textures once each; mgid. Face counts follow geometry, so stairs shape,
                       fence / wall / pane arms and redstone wire arms split it (door 64 states -> 2);
               thash   texture SET of (texture, tinted) + EXC textures (what a reference slot needs; stairs, fences: 1); tgid;
               ahash   appearance+geometry: (texture, role) multiset, role = top | side | bottom of the face after the
                       blockstate x-then-y rotation, + geometry identity (mhash, x, y, uvlock, weight per part; + the type
                       for block-entity renders); gid (door 32).
             A code-side tint that changes the look without changing a texture (banner dye, TKEY: "dye_color:red") joins
             all three hashes. Per gid: textures with roles, tinted ones, element count, full cube, bbox (block frame,
             element rotations ignored), the parts of its first variant (models keep their elements), its tgid and mgid.
  Roles      flip one property with the others fixed, strongest outcome over all flips of the type: thash
             changes -> appear (farmland moisture: 0..6 one group, 7 farmland_moist); only ahash -> geom; never -> inert
             (light level: every model element-less); one value only -> single. role decides on the texture SET, not the
             texture multiset, since the multiset makes pure shape changes (stairs shape, fence / wall arms) "appear";
             role_ms = the same flips on the multiset is kept next to it (the summary counts where they differ). A property
             in no key / when clause is "absent": appearance-neutral in the assets (powered, distance, ...). The full
             property list of a block is only in the data generator's blocks.json report (not in the client jar), so
             absent properties are only listed from real state strings (--batch). EXC overrides with src "code": fluid
             level -> geom, redstone power / stem age -> appear (tint), sign / banner / head rotation -> geom, chest type ->
             appear, bed part -> geom; tints (tintindex faces) get tint_src (biome_grass, biome_foliage, biome_water,
             fixed, redstone_power, stem_age, dye_color; code = tintindex faces of a type outside the table). ANYPROP:
             code roles for properties the jar cannot tie to types: waterlogged -> appear (a water volume in the cell;
             resolve() lists water_still / water_flow in "overlay" when true, gid / tgid / mgid stay the block's own; the
             one type listing it in its keys, sea_pickle, gets the code role with its jar role kept in role_jar);
             reg.prop(type, p) = the type's own entry, else ANYPROP, else None.
  Registry.build(jar) / Registry.load(json) / reg.save(out); reg.lookup("minecraft:oak_door[facing=east,half=lower,
             hinge=left,open=true,powered=false]") -> ("oak_door", "facing=east,half=lower,hinge=left,open=true", gid):
             variants = the key whose pairs all hold (properties outside the keys ignored; a partial state takes the first
             consistent key, exact False); multipart = union of the matching parts, vkey over the 'when' properties, gid
             of its ahash (None if never enumerated). reg.resolve(state) -> dict with tgid, mgid, exact, parts, overlay too.
  Geometry (G-buffer v2: voxel_proxy.StateMesher draws every state from these models): the models keep their
             elements (from / to boxes in 1/16, faces with texture / uv / uv rotation / cullface, element rotation); reg.parts_of
             (state) = the applied parts ([[alt]], the first alternative is drawn); d["geom"]["alpha"] {model: per element {face:
             [s0, t0, s1, t1]}} = the opaque texels (alpha > 0, first frame of an animated strip) of the face's uv rect as a
             face-local box (s left -> right, t top -> bottom seen from outside; MC default uv per face when none is written; uv
             rotation r clockwise), [] = fully transparent face, [0, 0, 1, 1] = holes but nothing to crop, a face absent = every
             texel opaque (StateMesher lets only those hide a neighbour's face): cross plants, torches, rails, crops shrink to their
             drawn part. Derived numbers only (no pixels kept); outside registry_id and every hash, so gids
             stay those of a registry built without it (Registry.load of an older json: no crops, reg.alpha empty: cap2proxy
             refuses it); reg.geom_id = sha1 of d["geom"] (None without) tells the crops apart in provenance.
  1.21 jar: 1062 types (992 variants + 70 multipart), 11250 variants (5895 keys + 5355 multipart combinations); groups
             global / summed per type: texture multiset 1549 / 1881, texture set 957 / 1312, appearance+geometry 9731 / 10129
             (360 shared by > 1 type); 359 jar type-property pairs are geom by set but appear by multiset; 1054 textures (49
             animated), 1925 models.
  python -m mineengine.mc_registry --jar 1.21.jar --out REG.json [--batch BATCH_DIR] [--test]   (or --reg REG.json instead of --jar)
             (summary: types, variants, multipart blocks, groups, textures, top properties, 20 examples; --batch: lookup
             coverage and absent properties over the snapshot palettes + block records of one capture batch)
"""
import argparse
import glob
import gzip
import hashlib
import itertools
import json
import os
import re
import sys
import zipfile
from collections import Counter, defaultdict

BS, MD, TX = "assets/minecraft/blockstates/", "assets/minecraft/models/", "assets/minecraft/textures/"
CAP = 4096
DIRS = {"up": (0, 1, 0), "down": (0, -1, 0), "north": (0, 0, -1), "south": (0, 0, 1), "west": (-1, 0, 0), "east": (1, 0, 0)}
ENUMS = [("true", "false"), ("none", "side", "up"), ("none", "low", "tall"), ("x", "y", "z"), ("none", "small", "large"),
         ("north", "east", "south", "west"), ("north", "east", "south", "west", "up", "down")]
KNOWN = {("composter", "level"): [str(i) for i in range(9)], ("bamboo", "age"): ["0", "1"], ("pink_petals", "flower_amount"): ["1", "2", "3", "4"]}
RANK = {"absent": -1, "single": 0, "inert": 1, "geom": 2, "appear": 3}
INVIS = {"air", "cave_air", "void_air", "barrier", "light", "structure_void"}
HEADS = {"skeleton_skull": "skeleton/skeleton", "wither_skeleton_skull": "skeleton/wither_skeleton", "zombie_head": "zombie/zombie",
         "creeper_head": "creeper/creeper", "piglin_head": "piglin/piglin", "dragon_head": "enderdragon/dragon",
         "player_head": "player/wide/steve"}
TINT = [(r"(grass_block|short_grass|tall_grass|fern|large_fern|potted_fern|sugar_cane)", "biome_grass"),
        (r"((oak|jungle|acacia|dark_oak|mangrove)_leaves|vine)", "biome_foliage"),
        (r"((spruce|birch)_leaves|lily_pad|attached_(pumpkin|melon)_stem)", "fixed"),
        (r"(water|bubble_column|water_cauldron)", "biome_water"), (r"redstone_wire", "redstone_power"),
        (r"(pumpkin|melon)_stem", "stem_age"), (r".+_(wall_)?banner", "dye_color")]
R16, H4 = [str(i) for i in range(16)], ["north", "south", "west", "east"]
ANYPROP = {"waterlogged": {"values": ["false", "true"], "role": "appear", "role_ms": "appear", "role_jar": "absent", "src": "code",
                           "overlay": {"true": ["block/water_still", "block/water_flow"]}}}


def nid(s): return s.split(":", 1)[1] if ":" in s else s


def sha1(o): return hashlib.sha1(json.dumps(o, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def natkey(v): return (0, int(v), "") if re.fullmatch(r"-?\d+", v) else (1, 0, v)


def sval(v): return ("true" if v else "false") if isinstance(v, bool) else str(v)


def rot(v, x, y):
    a, b, c = v
    for _ in range((x // 90) % 4): a, b, c = a, c, -b
    for _ in range((y // 90) % 4): a, b, c = -c, b, a
    return a, b, c


def role(face, x, y):
    u = rot(DIRS[face], x, y)[1]
    return "top" if u > 0 else "bottom" if u < 0 else "side"


def rbox(box, x, y):
    if box is None: return None
    pts = [rot((px - 8, py - 8, pz - 8), x, y) for px in (box[0][0], box[1][0]) for py in (box[0][1], box[1][1]) for pz in (box[0][2], box[1][2])]
    return [[min(p[i] for p in pts) + 8 for i in range(3)], [max(p[i] for p in pts) + 8 for i in range(3)]]


def exc(t):
    """code-side appearance of type t -> (render, extra textures, {prop: (role, values)})"""
    if t in INVIS: return "invisible", [], {}
    if t == "moving_piston": return "block_entity", [], {}
    if t in ("water", "lava"): return "fluid", ["block/%s_still" % t, "block/%s_flow" % t], {"level": ("geom", R16)}
    if t == "bubble_column": return "fluid", ["block/water_still", "block/water_flow"], {"drag": ("inert", ["false", "true"])}
    m = re.fullmatch(r"(.+?)_(wall_)?hanging_sign", t)
    if m: return "block_entity", ["entity/signs/hanging/" + m.group(1)], {"facing": ("geom", H4)} if m.group(2) else {"rotation": ("geom", R16), "attached": ("geom", ["false", "true"])}
    m = re.fullmatch(r"(.+?)_(wall_)?sign", t)
    if m: return "block_entity", ["entity/signs/" + m.group(1)], {"facing": ("geom", H4)} if m.group(2) else {"rotation": ("geom", R16)}
    m = re.fullmatch(r"(.+?)_(wall_)?banner", t)
    if m: return "block_entity", ["entity/banner_base", "entity/banner/base"], {"facing": ("geom", H4)} if m.group(2) else {"rotation": ("geom", R16)}
    m = re.fullmatch(r"(.+)_bed", t)
    if m: return "block_entity", ["entity/bed/" + m.group(1)], {"facing": ("geom", H4), "part": ("geom", ["foot", "head"]), "occupied": ("inert", ["false", "true"])}
    if t in ("chest", "trapped_chest"):
        k = "normal" if t == "chest" else "trapped"
        return "block_entity", ["entity/chest/%s%s" % (k, s) for s in ("", "_left", "_right")], {"facing": ("geom", H4), "type": ("appear", ["single", "left", "right"])}
    if t == "ender_chest": return "block_entity", ["entity/chest/ender"], {"facing": ("geom", H4)}
    m = re.fullmatch(r"(?:(.+)_)?shulker_box", t)
    if m: return "block_entity", ["entity/shulker/shulker" + ("_" + m.group(1) if m.group(1) else "")], {"facing": ("geom", H4 + ["up", "down"])}
    h = t.replace("_wall_", "_")
    if h in HEADS:
        p = {"facing": ("geom", H4)} if "_wall_" in t else {"rotation": ("geom", R16)}
        if h in ("dragon_head", "piglin_head"): p["powered"] = ("geom", ["false", "true"])
        return "block_entity", ["entity/" + HEADS[h]], p
    if t == "conduit": return "block_entity", ["entity/conduit/base", "entity/conduit/cage"], {}
    if t == "decorated_pot": return "block_entity", ["entity/decorated_pot/decorated_pot_base", "entity/decorated_pot/decorated_pot_side"], {"facing": ("geom", H4)}
    if t in ("end_portal", "end_gateway"): return "block_entity", ["entity/end_portal", "environment/end_sky"], {}
    if t == "bell": return "model+be", ["entity/bell/bell_body"], {}
    if t == "enchanting_table": return "model+be", ["entity/enchanting_table_book"], {}
    if t == "lectern": return "model+be", ["entity/enchanting_table_book"], {"has_book": ("appear", ["false", "true"])}
    if t == "redstone_wire": return "model", [], {"power": ("appear", R16)}
    if t in ("pumpkin_stem", "melon_stem"): return "model", [], {"age": ("appear", [str(i) for i in range(8)])}
    return "model", [], {}


def tint_src(t):
    return next((s for p, s in TINT if re.fullmatch(p, t)), "code")


def tkey(t):
    """code-side tint that the textures do not carry (banner dye from the type name) -> key or None"""
    m = re.fullmatch(r"(.+?)_(wall_)?banner", t)
    return "dye_color:" + m.group(1) if m else None


def holds(w, props):
    if "OR" in w: return any(holds(c, props) for c in w["OR"])
    if "AND" in w: return all(holds(c, props) for c in w["AND"])
    return all(props.get(k) in sval(v).split("|") for k, v in w.items())


def when_props(w, out):
    for k, v in w.items():
        if k in ("OR", "AND"):
            for c in v: when_props(c, out)
        else: out[k] |= set(sval(v).split("|"))
    return out


def domain(t, p, vals):
    if (t, p) in KNOWN: return KNOWN[(t, p)]
    for e in ENUMS:
        if vals <= set(e): return list(e)
    return sorted(vals, key=natkey) + ["_other"]


def alts(a): return [[nid(v["model"]), v.get("x", 0), v.get("y", 0), bool(v.get("uvlock", False)), v.get("weight", 1)] for v in (a if isinstance(a, list) else [a])]


def parse_state(s):
    m = re.fullmatch(r"\s*([^\[\s]+)\s*(?:\[(.*)\])?\s*", s)
    if not m: return None, {}
    return nid(m.group(1)), dict(kv.split("=", 1) for kv in (m.group(2) or "").split(",") if "=" in kv)


def parse_key(k): return dict(kv.split("=", 1) for kv in k.split(",") if kv)


def load_model(z, names, mid, missing):
    chain, js, builtin, cur = [], [], None, mid
    while cur and len(chain) < 32:
        if cur.startswith("builtin/"): builtin = cur; break
        p = MD + cur + ".json"
        if p not in names: missing.add(cur); break
        d = json.loads(z.read(p)); chain.append(cur); js.append(d); cur = nid(d.get("parent", ""))
    tex = {}
    for d in reversed(js): tex.update(d.get("textures", {}))

    def rv(v):
        seen = set()
        while v.startswith("#"):
            k = v[1:]
            if k in seen or k not in tex: return "#" + k
            seen.add(k); v = tex[k]
        return nid(v)

    def rf(v):
        k = v[1:] if v.startswith("#") else v
        return rv("#" + k)
    els = next((d["elements"] for d in js if "elements" in d), None)
    ao = next((d["ambientocclusion"] for d in js if "ambientocclusion" in d), True)
    E, faces = [], []
    for e in els or []:
        f = {}
        for fd, fc in e.get("faces", {}).items():
            q = {"t": rf(fc["texture"])}
            if "uv" in fc: q["uv"] = fc["uv"]
            if fc.get("rotation"): q["r"] = fc["rotation"]
            if "tintindex" in fc: q["ti"] = fc["tintindex"]
            if "cullface" in fc: q["cull"] = fc["cullface"]
            f[fd] = q; faces.append([q["t"], fd, "ti" in q])
        r = {"from": e["from"], "to": e["to"], "faces": f}
        if "rotation" in e: r["rot"] = e["rotation"]
        if e.get("shade") is False: r["shade"] = False
        E.append(r)
    part = rv(tex["particle"]) if "particle" in tex else None
    if els is None:
        return {"chain": chain, "builtin": builtin, "elements": None, "particle": part, "faces": [], "nel": 0, "full": False,
                "bbox": None, "ao": ao, "mhash": sha1(["noelem", mid, part, builtin])}
    hE = [{**e, "faces": {k: {a: b for a, b in v.items() if a != "cull"} for k, v in e["faces"].items()}} for e in E]
    full = any([float(c) for c in e["from"]] == [0, 0, 0] and [float(c) for c in e["to"]] == [16, 16, 16] for e in E)
    box = [[min(min(e["from"][i], e["to"][i]) for e in E) for i in range(3)], [max(max(e["from"][i], e["to"][i]) for e in E) for i in range(3)]] if E else None
    return {"chain": chain, "builtin": builtin, "elements": E, "particle": part, "faces": faces, "nel": len(E), "full": full,
            "bbox": box, "ao": ao, "mhash": sha1(["el", hE, ao])}


def default_uv(f, t, fd):
    """MC's uv of an element face without an explicit 'uv' (BlockElement.uvsByFace)"""
    return {"down": [f[0], 16 - t[2], t[0], 16 - f[2]], "up": [f[0], f[2], t[0], t[2]], "north": [16 - t[0], 16 - t[1], 16 - f[0], 16 - f[1]],
            "south": [f[0], 16 - t[1], t[0], 16 - f[1]], "west": [f[2], 16 - t[1], t[2], 16 - f[1]], "east": [16 - t[2], 16 - t[1], 16 - f[2], 16 - f[1]]}[fd]


def alpha_box(mk, uv, r=0):
    """opaque mask [W, W] of a texture, face uv (0..16) and uv rotation -> face-local [s0, t0, s1, t1] of the opaque texels
    ([0, 0, 1, 1]: holes inside, nothing to crop), None (every texel opaque, or a zero-width rect) or [] (transparent)"""
    k = mk.shape[0] / 16.0; u0, v0, u1, v1 = (float(c) for c in uv)
    ua, ub, va, vb = min(u0, u1), max(u0, u1), min(v0, v1), max(v0, v1)
    i0, i1 = max(int((ua * k) // 1), 0), min(-int((-ub * k) // 1), mk.shape[1]); j0, j1 = max(int((va * k) // 1), 0), min(-int((-vb * k) // 1), mk.shape[0])
    if i1 <= i0 or j1 <= j0 or ub - ua < 1e-6 or vb - va < 1e-6: return None
    sub = mk[j0:j1, i0:i1]
    if sub.all(): return None
    if not sub.any(): return []
    cs, rs = sub.any(0).nonzero()[0], sub.any(1).nonzero()[0]
    cu = (max((i0 + cs[0]) / k, ua), min((i0 + cs[-1] + 1) / k, ub)); cv = (max((j0 + rs[0]) / k, va), min((j0 + rs[-1] + 1) / k, vb))
    s_ = sorted((c - u0) / (u1 - u0) for c in cu); t_ = sorted((c - v0) / (v1 - v0) for c in cv)
    r = int(r or 0) % 360
    s, t = {0: (s_, t_), 90: ([1 - t_[1], 1 - t_[0]], s_), 180: ([1 - s_[1], 1 - s_[0]], [1 - t_[1], 1 - t_[0]]), 270: (t_, [1 - s_[1], 1 - s_[0]])}[r]
    return [round(s[0], 4) + 0.0, round(t[0], 4) + 0.0, round(s[1], 4) + 0.0, round(t[1], 4) + 0.0]


def face_alpha(z, names, models, log=print):
    """d["geom"]["alpha"] (module docstring "Geometry"); needs PIL + numpy at build time only."""
    try:
        import io
        import numpy as np
        from PIL import Image
    except ImportError as e:
        log("no face alpha boxes (%s): faces drawn to their edges" % e); return None
    cache = {}

    def mask(tex):
        if tex not in cache:
            p = TX + tex + ".png"
            if p not in names: cache[tex] = None
            else:
                a = np.asarray(Image.open(io.BytesIO(z.read(p))).convert("RGBA"))[..., 3]
                cache[tex] = a[:a.shape[1]] > 0                                  # first frame of an animated strip
        return cache[tex]
    out = {}
    for mid, m in sorted(models.items()):
        rows, hit = [], False
        for e in m["elements"] or []:
            r = {}
            for fd, q in e["faces"].items():
                mk = mask(q["t"])
                b = None if mk is None else alpha_box(mk, q.get("uv") or default_uv(e["from"], e["to"], fd), q.get("r", 0))
                if b is not None: r[fd] = b; hit = True
            rows.append(r)
        if hit: out[mid] = rows
    return out


def appearance(models, t, parts, extra, be, tk=None):
    """parts [[alt]] -> (thash, ahash, mshash, info); alt = [model, x, y, uvlock, weight]; tk = code tint key"""
    tset, roles, geo, nel, full, box = set((x, False) for x in extra), Counter(), [], 0, False, None
    ms = Counter((x, False) for x in extra)
    for x in extra: roles[(x, "be")] += 1
    for ps in parts:
        g = []
        for k, (mid, x, y, uv, w) in enumerate(ps):
            m = models[mid]; g.append([m["mhash"], x, y, uv, w])
            for tx, fd, ti in m["faces"]:
                tset.add((tx, bool(ti))); roles[(tx, role(fd, x, y))] += 1
            if k == 0:
                nel += m["nel"]; full |= m["full"]; b = rbox(m["bbox"], x, y)
                if b: box = b if box is None else [[min(box[0][i], b[0][i]) for i in range(3)], [max(box[1][i], b[1][i]) for i in range(3)]]
        for c in {tuple(sorted(Counter((tx, bool(ti)) for tx, _, ti in models[a[0]]["faces"]).items())) for a in ps}:
            for kv, n in c: ms[kv] += n
        geo.append(sorted(g))
    geo.sort()
    if be: geo.append(["be", t])
    tl = sorted(tset); rl = sorted([list(k) + [v] for k, v in roles.items()]); ml = sorted([list(k) + [v] for k, v in ms.items()])
    texr = defaultdict(set)
    for tx, r in roles: texr[tx].add(r)
    tail = [["tint", tk]] if tk else []
    return sha1(tl + tail), sha1([rl, geo] + tail), sha1(ml + tail), {
        "tex": {k: sorted(v) for k, v in sorted(texr.items())}, "tinted": sorted({x for x, ti in tl if ti}), "nel": nel, "full": full,
        "bbox": box, "parts": parts, **({"tint_key": tk} if tk else {}), "tset": [list(x) for x in tl], "mset": ml}


def flip_roles(rows, empty=()):
    """rows [(props dict, gid, tgid[, mgid])] -> {prop: {"values", "role"[, "role_ms"]}} by one-property flips (rows of an
    empty group skipped); role: tgid change -> appear, gid -> geom; role_ms (rows with mgid): the same on mgid"""
    out, wm = {}, bool(rows) and len(rows[0]) > 3
    for p in sorted(set().union(*[set(r[0]) for r in rows]) if rows else ()):
        b, vals = defaultdict(list), set()
        for kd, g, tg, *mg in rows:
            if p in kd:
                vals.add(kd[p])
                if g in empty: continue
                b[(frozenset(kd), tuple(sorted((k, v) for k, v in kd.items() if k != p)))].append((kd[p], g, tg, mg[0] if mg else None))
        r = rm = "single" if len(vals) < 2 else "inert"
        for lst in b.values():
            if len({x[0] for x in lst}) < 2: continue
            ng = len({x[1] for x in lst}) > 1
            c = "appear" if len({x[2] for x in lst}) > 1 else "geom" if ng else "inert"
            cm = "appear" if len({x[3] for x in lst}) > 1 else "geom" if ng else "inert"
            if RANK[c] > RANK[r]: r = c
            if RANK[cm] > RANK[rm]: rm = cm
        out[p] = {"values": sorted(vals, key=natkey), "role": r, **({"role_ms": rm} if wm else {})}
    return out


class Registry:
    def __init__(self, d):
        self.d, self.models, self.types, self.groups, self.tgroups = d, d["models"], d["types"], d["groups"], d["tgroups"]
        self.mgroups, self.anyprop = d.get("mgroups", []), d.get("anyprop", ANYPROP)
        self.alpha = (d.get("geom") or {}).get("alpha") or {}
        self.geom_id = sha1(d["geom"]) if d.get("geom") else None                 # provenance of the alpha crops (outside registry_id)
        self.by_ahash = {g["ahash"]: g["gid"] for g in self.groups}
        self.by_thash = {g["thash"]: g["tgid"] for g in self.tgroups}
        self.by_mshash = {g["mshash"]: g["mgid"] for g in self.mgroups}
        self._keys = {t: [(k, parse_key(k)) for k in v["variants"]] for t, v in self.types.items() if v["kind"] == "variants"}
        self._cache = {}

    @classmethod
    def load(cls, path):
        with open(path) as f: return cls(json.load(f))

    def save(self, path):
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        with open(path, "w") as f: json.dump(self.d, f, separators=(",", ":"))

    @classmethod
    def build(cls, jar, log=print):
        z = zipfile.ZipFile(jar); names = set(z.namelist())
        ver = json.loads(z.read("version.json")) if "version.json" in names else {}
        with open(jar, "rb") as f: jsha = hashlib.sha1(f.read()).hexdigest()
        raw = {}
        for p in sorted(n for n in names if n.startswith(BS) and n.endswith(".json")):
            raw[p[len(BS):-5]] = json.loads(z.read(p))
        models, missing = {}, set()
        for d in raw.values():
            for e in (d.get("variants", {}).values() if "variants" in d else [q["apply"] for q in d["multipart"]]):
                for a in alts(e):
                    if a[0] not in models: models[a[0]] = load_model(z, names, a[0], missing)
        groups, tgroups, mgroups, gix, tix, mix, types, capped = [], [], [], {}, {}, {}, {}, []

        def group(t, parts, extra, be, tk):
            th, ah, mh, info = appearance(models, t, parts, extra, be, tk)
            tl, ml = info.pop("tset"), info.pop("mset")
            if th not in tix:
                tix[th] = len(tgroups); tgroups.append({"tgid": len(tgroups), "thash": th, "tex": tl, "types": [], "ngroups": 0, **({"tint_key": tk} if tk else {})})
            if mh not in mix:
                mix[mh] = len(mgroups); mgroups.append({"mgid": len(mgroups), "mshash": mh, "tex": ml, "types": [], "ngroups": 0, **({"tint_key": tk} if tk else {})})
            if ah not in gix:
                gix[ah] = len(groups); groups.append({"gid": len(groups), "ahash": ah, "thash": th, "tgid": tix[th], "mshash": mh, "mgid": mix[mh],
                                                      **info, "empty": not tl, "types": [], "nvar": 0})
                tgroups[tix[th]]["ngroups"] += 1; mgroups[mix[mh]]["ngroups"] += 1
            g = groups[gix[ah]]; g["nvar"] += 1
            for x in (g, tgroups[tix[th]], mgroups[mix[mh]]):
                if t not in x["types"]: x["types"].append(t)
            return g["gid"], tix[th]
        for t, d in raw.items():
            render, extra, cprops = exc(t)
            be, tk = render == "block_entity", tkey(t)
            rec = {"tid": len(types), "render": render, "extra_tex": extra, **({"tint_key": tk} if tk else {})}
            if "variants" in d:
                rec["kind"] = "variants"; rec["variants"] = {k: list(group(t, [alts(v)], extra, be, tk)) for k, v in d["variants"].items()}
                rows = [(parse_key(k), g, tg, groups[g]["mgid"]) for k, (g, tg) in rec["variants"].items()]
            else:
                rec["kind"] = "multipart"
                rec["parts"] = [{"when": q.get("when"), "apply": alts(q["apply"])} for q in d["multipart"]]
                dom = when_props({"AND": [q["when"] for q in d["multipart"] if q.get("when")]}, defaultdict(set))
                rec["domain"] = {p: domain(t, p, v) for p, v in sorted(dom.items())}
                ps = list(rec["domain"]); n = 1
                for p in ps: n *= len(rec["domain"][p])
                if n > CAP:
                    base = {p: rec["domain"][p][0] for p in ps}
                    combos = [base] + [{**base, p: v} for p in ps for v in rec["domain"][p][1:]]
                    capped.append((t, n)); rec["capped"] = n
                else: combos = [dict(zip(ps, c)) for c in itertools.product(*[rec["domain"][p] for p in ps])]
                rec["variants"], rows = {}, []
                for c in combos:
                    parts = [q["apply"] for q in rec["parts"] if q["when"] is None or holds(q["when"], c)]
                    g, tg = group(t, parts, extra, be, tk)
                    rec["variants"][",".join("%s=%s" % (p, c[p]) for p in ps)] = [g, tg]; rows.append((c, g, tg, groups[g]["mgid"]))
            props = flip_roles(rows, {r[1] for r in rows if groups[r[1]]["empty"]})
            for p in props: props[p].update(role_jar=props[p]["role"], src="jar")
            for p, (r, vals) in cprops.items():
                q = props.get(p)
                props[p] = {"values": q["values"] if q else vals, "role": r, "role_ms": r, "role_jar": q["role"] if q else "absent",
                            **({"role_ms_jar": q["role_ms"]} if q else {}), "src": "code"}
            for p, a in ANYPROP.items():
                q = props.get(p)
                if q and q["src"] == "jar":
                    props[p] = {"values": q["values"], "role": a["role"], "role_ms": a["role_ms"], "role_jar": q["role"], "role_ms_jar": q["role_ms"], "src": "code"}
            rec["props"] = props
            if render == "model" and all(groups[g]["nel"] == 0 for g, _ in rec["variants"].values()): rec["render"] = "noelem"
            if any(groups[g]["tinted"] for g, _ in rec["variants"].values()) or tint_src(t) in ("biome_water", "dye_color"): rec["tint_src"] = tint_src(t)
            rec["ngroups"] = len({g for g, _ in rec["variants"].values()}); rec["ntgroups"] = len({tg for _, tg in rec["variants"].values()})
            rec["nmgroups"] = len({groups[g]["mgid"] for g, _ in rec["variants"].values()})
            types[t] = rec
        tex = Counter(); texg = Counter()
        for g in groups:
            for x in g["tex"]: texg[x] += 1
        for t, r in types.items():
            for x in {x for g, _ in r["variants"].values() for x in groups[g]["tex"]}: tex[x] += 1
        textures = {x: {"png": (TX + x + ".png") in names, "anim": (TX + x + ".png.mcmeta") in names, "ntypes": tex[x], "ngroups": texg[x]}
                    for x in sorted(tex)}
        d = {"format": "mc-block-registry-v2", "source": {"jar": os.path.abspath(jar), "jar_sha1": jsha, "version": ver.get("id"),
             "world_version": ver.get("world_version")}, "cap": CAP, "capped": capped, "missing_models": sorted(missing),
             "notes": ["full property lists / default states / state counts need the data generator report (reports/blocks.json); "
                       "absent = in no blockstate key or when clause of this jar",
                       "EXC textures, tint keys, ANYPROP and src=code roles are a hand table of code-side rendering (block-entity "
                       "renderers, fluids, tints, waterlogging)",
                       "groups: gid = appearance+geometry (ahash), tgid = texture set (thash), mgid = texture multiset (mshash, "
                       "design 7.3 appearance variant); role = flips on tgid, role_ms = flips on mgid"],
             "anyprop": ANYPROP, "types": types, "groups": groups, "tgroups": tgroups, "mgroups": mgroups, "textures": textures, "models": models}
        d["registry_id"] = sha1({k: v for k, v in d.items() if k != "source"})
        al = face_alpha(z, names, models, log)
        if al is not None:
            d["geom"] = {"format": "face-alpha-v1", "alpha_gt": 0, "alpha": al}
        log("built %d types, %d models, %d / %d / %d groups (geometry / set / multiset) in registry %s; face alpha boxes for %s models"
            % (len(types), len(models), len(groups), len(tgroups), len(mgroups), d["registry_id"][:12], len(al) if al is not None else "no"))
        return cls(d)

    def resolve(self, state):
        if state in self._cache: return self._cache[state]
        t, props = parse_state(state)
        r = self.types.get(t)
        out = {"type": t, "vkey": None, "gid": None, "tgid": None, "mgid": None, "exact": False, "known": r is not None, "overlay": []}
        if r is None: pass
        elif r["kind"] == "variants":
            best = None
            for k, kd in self._keys[t]:
                if all(props.get(a) == b for a, b in kd.items()) and (best is None or len(kd) > len(best[1])): best = (k, kd)
            exact = best is not None
            if best is None: best = next(((k, kd) for k, kd in self._keys[t] if all(props.get(a, b) == b for a, b in kd.items())), (None, None))
            if best[0] is not None:
                g, tg = r["variants"][best[0]]; out.update(vkey=best[0], gid=g, tgid=tg, mgid=self.groups[g].get("mgid"), exact=exact)
        else:
            dom = r["domain"]
            vk = ",".join("%s=%s" % (p, "?" if p not in props else props[p] if props[p] in v or "_other" not in v else "_other") for p, v in dom.items())
            parts = [q["apply"] for q in r["parts"] if q["when"] is None or holds(q["when"], props)]
            th, ah, mh, _ = appearance(self.models, t, parts, r["extra_tex"], r["render"] == "block_entity", r.get("tint_key"))
            out.update(vkey=vk, gid=self.by_ahash.get(ah), tgid=self.by_thash.get(th), mgid=self.by_mshash.get(mh), exact=all(p in props for p in dom), parts=parts)
        if r is not None:
            for p, v in props.items():
                if p in self.anyprop: out["overlay"] += self.anyprop[p].get("overlay", {}).get(v, [])
        self._cache[state] = out
        return out

    def parts_of(self, state):
        """-> (resolve dict, applied parts [[alt]] or None): multipart = the matching parts; variants = the parts of its group (the
        first variant with that appearance+geometry hash: same models and rotations)"""
        o = self.resolve(state)
        if o.get("parts") is not None: return o, o["parts"]
        return o, (self.groups[o["gid"]]["parts"] if o["gid"] is not None else None)

    def prop(self, t, p):
        """the registry entry of property p of type t: the type's own, else the ANYPROP code rule, else None"""
        r = self.types.get(t)
        return None if r is None else r["props"].get(p) or self.anyprop.get(p)

    def lookup(self, state):
        o = self.resolve(state)
        return o["type"], o["vkey"], o["gid"]

    def counts(self):
        T = self.types
        return {"types": len(T), "variants_kind": sum(r["kind"] == "variants" for r in T.values()),
                "multipart_kind": sum(r["kind"] == "multipart" for r in T.values()),
                "variants": sum(len(r["variants"]) for r in T.values()),
                "variants_from_keys": sum(len(r["variants"]) for r in T.values() if r["kind"] == "variants"),
                "variants_from_multipart": sum(len(r["variants"]) for r in T.values() if r["kind"] == "multipart"),
                "mgroups": len(self.mgroups), "tgroups": len(self.tgroups), "groups": len(self.groups),
                "mgroups_shared_types": sum(len(g["types"]) > 1 for g in self.mgroups), "tgroups_shared_types": sum(len(g["types"]) > 1 for g in self.tgroups),
                "groups_shared_types": sum(len(g["types"]) > 1 for g in self.groups),
                "sum_type_mgroups": sum(r.get("nmgroups", 0) for r in T.values()), "sum_type_tgroups": sum(r["ntgroups"] for r in T.values()),
                "sum_type_groups": sum(r["ngroups"] for r in T.values()),
                "role_vs_role_ms": dict(Counter("%s/%s" % (v["role"], v["role_ms"]) for r in T.values() for v in r["props"].values()
                                                if v["src"] == "jar" and "role_ms" in v and v["role"] != v["role_ms"])),
                "textures": len(self.d["textures"]), "textures_missing_png": sum(not v["png"] for v in self.d["textures"].values()),
                "textures_animated": sum(v["anim"] for v in self.d["textures"].values()),
                "unresolved_tex": sum(x.startswith("#") for x in self.d["textures"]),
                "models": len(self.models), "models_noelem": sum(m["elements"] is None for m in self.models.values()),
                "missing_models": len(self.d["missing_models"]), "capped": len(self.d["capped"]),
                "render": dict(Counter(r["render"] for r in T.values())),
                "random_alt_variants": sum(any(len(p) > 1 for p in self.groups[g]["parts"]) for r in T.values() for g, _ in r["variants"].values()),
                "domains_with_other": sorted("%s.%s" % (t, p) for t, r in T.items() for p, v in r.get("domain", {}).items() if "_other" in v)}


EXAMPLES = ["oak_door[facing=east,half=lower,hinge=left,open=true,powered=false]", "wheat[age=5]", "furnace[facing=north,lit=true]",
            "redstone_wire[east=side,north=none,power=7,south=side,west=none]", "oak_stairs[facing=east,half=bottom,shape=straight,waterlogged=false]",
            "oak_slab[type=top,waterlogged=false]", "water[level=0]", "lava[level=8]", "fire[age=3,east=false,north=true,south=false,up=false,west=false]",
            "campfire[facing=north,lit=true,signal_fire=false,waterlogged=false]", "glass_pane[east=true,north=false,south=false,waterlogged=false,west=true]",
            "oak_fence[east=true,north=true,south=false,waterlogged=false,west=false]", "torch", "wall_torch[facing=west]", "farmland[moisture=7]",
            "snow[layers=3]", "grass_block[snowy=false]", "chest[facing=north,type=single,waterlogged=false]", "oak_sign[rotation=4,waterlogged=false]",
            "white_banner[rotation=0]"]


def summary(reg):
    c = reg.counts(); T = reg.types
    print("jar %s (version %s, sha1 %s); registry_id %s" % (reg.d["source"]["jar"], reg.d["source"]["version"], reg.d["source"]["jar_sha1"][:12], reg.d["registry_id"][:12]))
    print("types %d (variants %d, multipart %d); variants %d (keys %d + multipart combos %d); random-alternative variants %d"
          % (c["types"], c["variants_kind"], c["multipart_kind"], c["variants"], c["variants_from_keys"], c["variants_from_multipart"], c["random_alt_variants"]))
    print("groups (global / summed per type / shared by >1 type): texture-multiset (mshash, appearance variants) %d / %d / %d;"
          " texture-set (thash) %d / %d / %d; appearance+geometry (ahash) %d / %d / %d"
          % (c["mgroups"], c["sum_type_mgroups"], c["mgroups_shared_types"], c["tgroups"], c["sum_type_tgroups"], c["tgroups_shared_types"],
             c["groups"], c["sum_type_groups"], c["groups_shared_types"]))
    print("jar type-property roles where set (role) and multiset (role_ms) flips differ, role/role_ms: %s" % c["role_vs_role_ms"])
    print("generic code props (any type whose state carries them; over a jar role where the type lists it):",
          ", ".join("%s %s (+%s)" % (p, v["role"], ",".join(x for xs in v.get("overlay", {}).values() for x in xs)) for p, v in reg.anyprop.items()))
    print("textures %d distinct (%d without png in jar, %d animated, %d unresolved '#var'); models %d (%d element-less), missing models %d; capped types %d (CAP %d)"
          % (c["textures"], c["textures_missing_png"], c["textures_animated"], c["unresolved_tex"], c["models"], c["models_noelem"], c["missing_models"], c["capped"], CAP))
    print("render kinds", c["render"]); print("multipart domains completed with _other:", c["domains_with_other"])
    pc, pr = Counter(), defaultdict(Counter)
    for r in T.values():
        for p, v in r["props"].items():
            if v["role_jar"] != "absent": pc[p] += 1; pr[p][v["role_jar"]] += 1
    print("top properties (types using them in keys / when; jar roles):")
    for p, n in pc.most_common(20): print("  %-18s %4d  %s" % (p, n, dict(pr[p])))
    roles = Counter(v["role_jar"] for r in T.values() for v in r["props"].values() if v["role_jar"] != "absent")
    print("type-property pairs by jar role:", dict(roles), "; code overrides:", sum(v["src"] == "code" for r in T.values() for v in r["props"].values()))
    print("examples (type: kind, render, variants, groups ahash / thash / mshash | roles (role_ms if different) | lookup -> vkey, gid, tgid, mgid | textures of that group):")
    for s in EXAMPLES:
        o = reg.resolve("minecraft:" + s); r = T[o["type"]]
        rl = " ".join("%s:%s%s%s" % (p, v["role"], "/" + v["role_ms"] if v.get("role_ms", v["role"]) != v["role"] else "", "*" if v["src"] == "code" else "") for p, v in r["props"].items())
        tx = ",".join("%s%s" % (x.split("/")[-1], "~" if x in reg.groups[o["gid"]]["tinted"] else "") for x in reg.groups[o["gid"]]["tex"]) if o["gid"] is not None else "-"
        print("  %-15s %-9s %-12s v=%-4d g=%-4d tg=%-3d mg=%-3d | %s | %s -> %s gid=%s tgid=%s mgid=%s | %s" % (o["type"], r["kind"], r["render"], len(r["variants"]), r["ngroups"], r["ntgroups"], r.get("nmgroups", 0), rl or "-", s.split("[")[1][:-1] if "[" in s else "", o["vkey"], o["gid"], o["tgid"], o["mgid"], tx))
    print("  (* = role from the code table; ~ = tinted face)")


def batch_check(reg, root):
    """lookup coverage over the snapshot palettes and block records of one capture batch"""
    cnt = Counter()
    for f in sorted(glob.glob(os.path.join(root, "**", "*_world_snapshot_*.json.gz"), recursive=True)):
        with gzip.open(f) as fh: cnt.update(json.load(fh).get("palette", []))
    for f in sorted(glob.glob(os.path.join(root, "**", "*_world_events.jsonl"), recursive=True)):
        with open(f) as fh:
            for l in fh:
                if '"minecraft:' not in l: continue
                r = json.loads(l)
                for k in ("state", "prev_state", "old_state", "new_state", "source_state"):
                    if isinstance(r.get(k), str): cnt[r[k]] += 1
    st, unk, part, absent, gids, tg, mg, abt, anyp, ant = Counter(), Counter(), [], Counter(), set(), set(), set(), defaultdict(set), Counter(), defaultdict(set)
    for s in cnt:
        o = reg.resolve(s); t, props = parse_state(s)
        if not o["known"]: unk[o["type"]] += 1; st["unknown_type"] += 1; continue
        st["exact" if o["exact"] else "partial"] += 1
        if not o["exact"]: part.append(s)
        if o["gid"] is None: st["no_gid"] += 1
        else: gids.add(o["gid"]); tg.add(o["tgid"]); mg.add(o["mgid"])
        for p in props:
            if p in reg.types[t]["props"]: continue
            if p in reg.anyprop: anyp[p] += 1; ant[p].add(t)
            else: absent[p] += 1; abt[p].add(t)
    print("batch %s: %d distinct state strings, %d types; %s; groups: texture-multiset %d, texture-set %d, appearance+geometry %d"
          % (root, len(cnt), len({parse_state(s)[0] for s in cnt}), dict(st), len(mg), len(tg), len(gids)))
    if unk: print("  unknown types:", dict(unk))
    if part: print("  partial:", part[:10])
    for p, n in anyp.most_common(): print("  generic code prop %s: %d distinct strings, %d types" % (p, n, len(ant[p])))
    print("  absent properties seen in real strings (distinct strings, types):")
    for p, n in absent.most_common(25): print("    %-12s %4d  %3d types  %s" % (p, n, len(abt[p]), ",".join(sorted(abt[p])[:8])))
    return st, unk


def selftest(reg):
    ok = [0, 0]

    def chk(name, cond):
        ok[bool(cond)] += 1; print("%s  %s" % ("PASS" if cond else "FAIL", name))
    T, G = reg.types, reg.groups
    L = reg.resolve
    a, b = L("minecraft:furnace[facing=north,lit=false]"), L("minecraft:furnace[facing=north,lit=true]")
    chk("furnace lit false/true: different groups and texture groups", a["gid"] != b["gid"] and a["tgid"] != b["tgid"])
    c = L("minecraft:furnace[facing=east,lit=false]")
    chk("furnace facing north/east: different groups, same texture group", a["gid"] != c["gid"] and a["tgid"] == c["tgid"])
    chk("furnace roles lit=appear facing=geom", T["furnace"]["props"]["lit"]["role"] == "appear" and T["furnace"]["props"]["facing"]["role"] == "geom")
    chk("furnace lit=true textures include furnace_front_on", "block/furnace_front_on" in G[b["gid"]]["tex"])
    p = T["oak_planks"]
    chk("oak_planks single variant '' / 1 group / full cube / textures {oak_planks}", list(p["variants"]) == [""] and p["ngroups"] == 1
        and G[p["variants"][""][0]]["full"] and list(G[p["variants"][""][0]]["tex"]) == ["block/oak_planks"])
    w = [L("minecraft:wheat[age=%d]" % i) for i in range(8)]
    chk("wheat age 0..7: 8 groups, 8 texture groups, age=appear", len({x["gid"] for x in w}) == 8 and len({x["tgid"] for x in w}) == 8
        and T["wheat"]["ngroups"] == 8 and T["wheat"]["props"]["age"]["role"] == "appear")
    d0 = L("minecraft:oak_door[facing=east,half=lower,hinge=left,open=false,powered=false]")
    d1 = L("minecraft:oak_door[facing=east,half=lower,hinge=left,open=true,powered=false]")
    m0, m1 = G[d0["gid"]]["parts"][0][0], G[d1["gid"]]["parts"][0][0]
    chk("door open changes geometry (model / y rotation), not textures", d0["gid"] != d1["gid"] and d0["tgid"] == d1["tgid"] and (m0[0], m0[2]) != (m1[0], m1[2]))
    chk("door open: closed %s y=%d -> open %s y=%d" % (m0[0], m0[2], m1[0], m1[2]), m1[0].endswith("_open") and m1[2] == 90)
    dp = T["oak_door"]["props"]
    chk("door roles half=appear facing/hinge/open=geom; powered absent", dp["half"]["role"] == "appear" and all(dp[k]["role"] == "geom" for k in ("facing", "hinge", "open")) and "powered" not in dp)
    chk("door: 2 texture groups (lower / upper)", T["oak_door"]["ntgroups"] == 2)
    chk("door powered ignored by lookup", L("minecraft:oak_door[facing=east,half=lower,hinge=left,open=true,powered=true]")["gid"] == d1["gid"])
    chk("lookup tuple of the full door string", reg.lookup("minecraft:oak_door[facing=east,half=lower,hinge=left,open=true,powered=false]") == ("oak_door", "facing=east,half=lower,hinge=left,open=true", d1["gid"]))
    chk("log axis = geom (same texture group)", T["oak_log"]["props"]["axis"]["role"] == "geom" and T["oak_log"]["ntgroups"] == 1 and T["oak_log"]["ngroups"] == 3)
    f = [L("minecraft:farmland[moisture=%d]" % i)["gid"] for i in range(8)]
    chk("farmland moisture 0..6 one group, 7 another; moisture=appear", len(set(f[:7])) == 1 and f[7] != f[0] and T["farmland"]["props"]["moisture"]["role"] == "appear")
    chk("infested_stone shares stone's group (cross-type)", L("minecraft:infested_stone")["gid"] == L("minecraft:stone")["gid"])
    chk("stone: 4 random alternatives in one part", len(G[L("minecraft:stone")["gid"]]["parts"][0]) == 4)
    fe = L("minecraft:oak_fence[east=true,north=false,south=false,waterlogged=false,west=true]")
    ys = sorted(a[2] for ps in fe["parts"] for a in ps if a[0].endswith("_side"))
    chk("oak_fence multipart: post + sides y=90,270; vkey; 16 combos; enumerated gid", len(fe["parts"]) == 3 and ys == [90, 270] and fe["vkey"] == "east=true,north=false,south=false,west=true"
        and len(T["oak_fence"]["variants"]) == 16 and fe["gid"] == T["oak_fence"]["variants"][fe["vkey"]][0])
    chk("oak_fence sides = geom (texture group unchanged)", all(v["role"] == "geom" for v in T["oak_fence"]["props"].values()) and T["oak_fence"]["ntgroups"] == 1)
    cw = T["cobblestone_wall"]["props"]
    chk("wall sides / up = geom (empty combination skipped), one texture group", all(cw[k]["role"] == "geom" for k in ("east", "up")) and T["cobblestone_wall"]["ntgroups"] == 2
        and sum(G[g]["empty"] for g, _ in T["cobblestone_wall"]["variants"].values()) == 1)
    chk("brown_mushroom_block faces = appear (cap vs inside texture)", T["brown_mushroom_block"]["props"]["north"]["role"] == "appear")
    chk("heavy_core bare face texture 'all' resolves", list(G[L("minecraft:heavy_core[waterlogged=false]")["gid"]]["tex"]) == ["block/heavy_core"])
    rw = T["redstone_wire"]
    chk("redstone_wire: domain none|side|up, 81 combos, power=appear (code)", len(rw["variants"]) == 81 and rw["props"]["power"]["src"] == "code" and rw["props"]["east"]["role"] == "appear")
    wl = L("minecraft:cobblestone_wall[east=none,north=low,south=tall,up=true,waterlogged=false,west=none]")
    chk("wall side none -> enumerated (known enum none|low|tall), 162 combos", wl["gid"] is not None and len(T["cobblestone_wall"]["variants"]) == 162 and "none" in T["cobblestone_wall"]["domain"]["east"])
    cb = L("minecraft:chiseled_bookshelf[facing=west,slot_0_occupied=true,slot_1_occupied=false,slot_2_occupied=false,slot_3_occupied=true,slot_4_occupied=false,slot_5_occupied=false]")
    chk("chiseled_bookshelf (AND clauses) resolves to an enumerated group", cb["gid"] is not None and cb["exact"])
    wa = T["water"]
    chk("water: fluid, textures water_still / water_flow, level=geom (code)", wa["render"] == "fluid" and {"block/water_still", "block/water_flow"} <= set(G[wa["variants"][""][0]]["tex"]) and wa["props"]["level"]["role"] == "geom")
    chk("chest: block_entity with entity/chest/normal; oak_sign != oak_wall_sign", "entity/chest/normal" in G[L("minecraft:chest")["gid"]]["tex"] and L("minecraft:oak_sign")["gid"] != L("minecraft:oak_wall_sign")["gid"])
    wb, rb, wwb = L("minecraft:white_banner[rotation=0]"), L("minecraft:red_banner[rotation=0]"), L("minecraft:white_wall_banner[facing=north]")
    chk("banner dye in the hashes: white / red banner differ in tgid and mgid; white standing / wall share both",
        wb["tgid"] != rb["tgid"] and wb["mgid"] != rb["mgid"] and (wb["tgid"], wb["mgid"]) == (wwb["tgid"], wwb["mgid"]))
    bt = {tg for t, r in T.items() if t.endswith("_banner") for _, tg in r["variants"].values()}
    chk("16 banner texture groups, each one dye x (standing, wall)", len(bt) == 16 and all(len(reg.tgroups[x]["types"]) == 2 for x in bt))
    od, st_ = T["oak_door"], T["oak_stairs"]
    chk("door: 2 texture multisets = 2 texture sets, 32 appearance+geometry groups (64 states -> 2)",
        (od["nmgroups"], od["ntgroups"], od["ngroups"]) == (2, 2, 32))
    chk("stairs: 1 texture set, 2 multisets (inner shape has 3 elements); shape role geom, role_ms appear",
        (st_["ntgroups"], st_["nmgroups"]) == (1, 2) and st_["props"]["shape"]["role"] == "geom" and st_["props"]["shape"]["role_ms"] == "appear")
    chk("stone multiset: 4 rotated alternatives count once -> 6 x block/stone; infested_stone same mgid",
        reg.mgroups[L("minecraft:stone")["mgid"]]["tex"] == [["block/stone", False, 6]] and L("minecraft:infested_stone")["mgid"] == L("minecraft:stone")["mgid"])
    ws, wf = L("minecraft:oak_stairs[facing=east,half=bottom,shape=straight,waterlogged=true]"), L("minecraft:oak_stairs[facing=east,half=bottom,shape=straight,waterlogged=false]")
    wp = reg.prop("oak_stairs", "waterlogged") or {}
    sp = reg.prop("sea_pickle", "waterlogged")
    chk("waterlogged: generic code role appear, water overlay only when true, groups unchanged; sea_pickle (in its keys) code role over jar %s"
        % sp["role_jar"], (wp.get("role"), wp.get("src")) == ("appear", "code") and ws["overlay"] == ["block/water_still", "block/water_flow"]
        and wf["overlay"] == [] and ws["gid"] == wf["gid"] and (sp["role"], sp["src"]) == ("appear", "code") and sp["role_jar"] in RANK
        and L("minecraft:sea_pickle[pickles=1,waterlogged=true]")["overlay"] == ws["overlay"] and reg.prop("not_a_block", "waterlogged") is None)
    gb = G[L("minecraft:grass_block[snowy=false]")["gid"]]
    chk("grass_block snowy=false: 4 alternatives, tinted top; snowy=appear", len(gb["parts"][0]) == 4 and "block/grass_block_top" in gb["tinted"] and T["grass_block"]["props"]["snowy"]["role"] == "appear")
    chk("snow layers=geom (8 groups, 1 texture group)", T["snow"]["props"]["layers"]["role"] == "geom" and T["snow"]["ngroups"] == 8 and T["snow"]["ntgroups"] == 1)
    chk("oak_slab type: top/bottom geom-only vs double", L("minecraft:oak_slab[type=top]")["tgid"] == L("minecraft:oak_slab[type=bottom]")["tgid"] and G[L("minecraft:oak_slab[type=double]")["gid"]]["full"])
    chk("partial state takes a consistent key, exact False", (lambda o: o["vkey"] is not None and not o["exact"])(L("minecraft:oak_door[half=upper]")))
    chk("unknown type -> no gid", L("minecraft:not_a_block[x=1]")["gid"] is None and not L("minecraft:not_a_block")["known"])
    unres = [x for x in reg.d["textures"] if x.startswith("#")]; nopng = [x for x, v in reg.d["textures"].items() if not v["png"]]
    chk("every face texture resolved and present in the jar (unresolved %d, no png %d)" % (len(unres), len(nopng)), not unres and not nopng)
    chk("no missing model files", not reg.d["missing_models"])
    if reg.alpha:
        tw = reg.alpha.get("block/torch", [{}, {}])[1].get("west") or [0, 0, 1, 1]
        po = [b for r in reg.alpha.get("block/poppy", []) for b in r.values()]
        chk("face alpha: torch planes cropped to the stick %s, poppy planes cropped %s, full blocks uncropped" % (tw, po[:1]),
            tw[2] - tw[0] < 0.25 and tw[1] > 0.3 and abs(tw[3] - 1) < 1e-6 and po and all(b and (b[1] > 0 or b[2] - b[0] < 1) for b in po)
            and "block/oak_planks" not in reg.alpha and "block/cube_all" not in reg.alpha and "block/snow_height2" not in reg.alpha)
    bad = [(t, k) for t, r in T.items() if r["kind"] == "variants" for k, (g, tg) in r["variants"].items()
           if reg.resolve("minecraft:%s[%s]" % (t, k) if k else "minecraft:" + t)["gid"] != g]
    chk("every variant key looks itself up (%d mismatches)" % len(bad), not bad)
    badm = [(t, k) for t, r in T.items() if r["kind"] == "multipart" for k, (g, tg) in r["variants"].items()
            if "_other" not in k and reg.resolve("minecraft:%s[%s]" % (t, k))["gid"] != g]
    chk("every multipart combo looks itself up (%d mismatches)" % len(badm), not badm)
    badk = [(t, k) for t, r in T.items() for k, (g, tg) in r["variants"].items() if "_other" not in k
            and (lambda o: (o["tgid"], o["mgid"]) != (tg, G[g]["mgid"]))(reg.resolve("minecraft:%s[%s]" % (t, k) if k else "minecraft:" + t))]
    chk("every variant / combo resolves to its tgid and mgid (%d mismatches)" % len(badk), not badk)
    print("selftest: %d passed, %d failed" % (ok[1], ok[0]))
    return ok[0] == 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--jar", default=os.environ.get("MC_CLIENT_JAR"), help="your Minecraft 1.21 client jar ($MC_CLIENT_JAR)")
    ap.add_argument("--reg", help="load a built registry json instead of the jar")
    ap.add_argument("--out"); ap.add_argument("--batch"); ap.add_argument("--test", action="store_true")
    a = ap.parse_args()
    reg = Registry.load(a.reg) if a.reg else Registry.build(a.jar)
    if a.out:
        reg.save(a.out); print("wrote %s (%.1f MB)" % (a.out, os.path.getsize(a.out) / 1e6))
    summary(reg)
    if a.batch: batch_check(reg, a.batch)
    good = True
    if a.test:
        good = selftest(reg)
        if a.out:
            r2 = Registry.load(a.out)
            same = all(r2.lookup("minecraft:" + s) == reg.lookup("minecraft:" + s) for s in EXAMPLES)
            print("%s  json round trip: lookups of the 20 examples equal" % ("PASS" if same else "FAIL")); good &= same
    sys.exit(0 if good else 1)


if __name__ == "__main__":
    main()
