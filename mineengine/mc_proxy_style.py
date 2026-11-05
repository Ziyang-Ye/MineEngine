"""Minecraft proxy styles: a voxel G-buffer (logdepth, shade,
pid, block, edge [F, h, w]: cap2proxy clips, mc.py render_views) -> RGB proxy frames [F, 2h, 2w, 3] uint8
(nearest x2: 192 x 336 -> the 384 x 672 renderer frame).
  mat  = _RefColorize.colorize per frame (block colour x shade, grid lines, sky colour, players in team colour) with every
         player pixel (pid > 0) in team `team`, block ids = mc.py ids as stored; vectorised over frames and equal to
         colorize(g_k, {pid - 1: team for every pid > 0}, t0 + k) bit for bit (selftest). t0 only matters for a team
         outside MCW.TEAM_RGB (colorize's white / red flicker of unknown entities by frame time).
  mat2 = mat with the players in colours no block material has (TEAM_RGB2: team 0 magenta, team 1 pure yellow: with mat's
         orange team 0, tan / red blocks - an oak_planks tower, a TNT block - can be taken for players).
  mono = the material-free style: every solid pixel that is not analytic ground drawn as MONO = mc.py COBBLE (6,
         neutral grey 110); ground = mc.py GRASS / STONE / DIRT / BEDROCK (= VoxelRenderer.flat_world's column ids, the
         superflat ground of the clips and of mc.py), keeping its id. With gb["ground"] (a material-labelled
         gbuf_mat.npz: the gbuf.npz block id on its flat_world pixels, 0 elsewhere; a 0 / 1 mask reads as GRASS = 1) the
         ground is the solid pixels where it is > 0, drawn with ITS id, so the labelled ground overrides and ground ids
         given to reconstructed cells never reach mono: mono(gbuf_mat.npz) == mono(gbuf.npz) exactly (selftest). Sky and
         players as mat.
noent(gb) = the same G-buffer without players: pid = 0 and every other [F, h, w]
  (or [F, h, w, ...]) array at player pixels taken from the nearest non-player pixel of the same row (left / right,
  whichever is nearer, ties left; a row of only player pixels -> 0 = sky). An approximation: the clip's voxel world is
  not stored, so what the player hides is guessed from its row neighbours.
write_video(path, frames, fps=16): h264 mp4 (yuv420p, crf 10; PyAV, else imageio-ffmpeg),
  written to path.part.mp4 and renamed.
method_of(gb) = (signature, method dict) of the material labelling a gbuf_mat.npz carries (key "method", canonical JSON),
  (None, None) without one: a loader can check that the clips it reads share it.
  python -m mineengine.mc_proxy_style --test      (colorize equivalence, mono, noent)
"""
import hashlib
import json
import os
import sys

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
from mineengine import mc as MCW  # noqa: E402

STYLES = ("mat", "mono", "mat2")
TEAM_RGB2 = {0: (255, 0, 255), 1: (255, 230, 0)}                                # mat2: player colours no block material has
MONO = MCW.COBBLE
GROUND_IDS = (MCW.GRASS, MCW.STONE, MCW.DIRT, MCW.BEDROCK)
SKY = (150, 190, 235)
METHOD_KEY = "method"                                                            # gbuf_mat.npz: the labelling method (0-d str)


def up2(a):
    """[F, h, w, ...] -> twice the spatial size (nearest)."""
    return a.repeat(2, axis=1).repeat(2, axis=2)


def mono_blocks(block, ground=None):
    """Block ids of style mono: solid non-ground -> MONO, air unchanged; ground = ids GROUND_IDS (kept), or with a ground
    array the solid pixels where it is > 0, drawn with its value (the original ground id; module docstring)."""
    mono = np.asarray(MONO, block.dtype)
    if ground is None:
        return np.where((block > 0) & ~np.isin(block, GROUND_IDS), mono, block)
    g = np.asarray(ground); solid = block > 0
    return np.where(solid & (g > 0), g.astype(block.dtype), np.where(solid, mono, block))


def method_of(gb):
    """G-buffer mapping / npz -> (md5[:10] of its labelling method JSON, method dict), or (None, None) without one."""
    if METHOD_KEY not in set(gb.keys()):
        return None, None
    js = str(np.asarray(gb[METHOD_KEY]))
    return hashlib.md5(js.encode()).hexdigest()[:10], json.loads(js)


def method_array(method):
    """Labeller side: method dict -> the 0-d str array stored under METHOD_KEY (canonical JSON: sorted keys)."""
    return np.array(json.dumps(method, sort_keys=True))


def colorize_frames(ld, shade, block, pid, edge=None, team=0, t0=0, team_rgb=None):
    """_RefColorize.colorize on [F, h, w] arrays at once, every pid > 0 drawn as team `team` (colours team_rgb, default
    MCW.TEAM_RGB) -> [F, h, w, 3] uint8."""
    team_rgb = MCW.TEAM_RGB if team_rgb is None else team_rgb
    img = MCW.BLOCK_RGB[np.minimum(block, len(MCW.BLOCK_RGB) - 1)].astype(np.float32)
    sh = shade.astype(np.float32)[..., None] / 255.0
    img = img * (0.35 + 0.65 * sh)
    if edge is not None:
        img[edge > 0] *= 0.6                                                     # block grid lines
    img[ld == 0] = SKY
    m = pid > 0
    if m.any():
        if team in team_rgb:
            img[m] = np.array(team_rgb[team], np.float32) * (0.45 + 0.55 * sh[m])
        else:                                                                    # colorize's unknown-entity flicker
            for k in np.nonzero(m.any((1, 2)))[0]:
                t = t0 + int(k); mk = m[k]
                col = np.array((255, 255, 255) if (t // 2) % 2 else (220, 40, 30), np.float32)
                img[k][mk] = col * (0.45 + 0.55 * sh[k][mk])
    return img.clip(0, 255).astype(np.uint8)


def proxy_frames(gb, style="mat", team=0, t0=0):
    """gb: mapping (dict / npz) with logdepth, shade, pid, block [F, h, w] (+ edge, optional ground mask) -> [F, 2h, 2w, 3]
    uint8 proxy of `style` (module docstring)."""
    assert style in STYLES, f"style {style!r} not in {STYLES}"
    ld, sh, pid, blk = (np.asarray(gb[k]) for k in ("logdepth", "shade", "pid", "block"))
    assert ld.ndim == 3 and ld.shape == sh.shape == pid.shape == blk.shape, (ld.shape, sh.shape, pid.shape, blk.shape)
    keys = set(gb.keys())                                                        # (an npz: no array is read to test a key)
    edge = np.asarray(gb["edge"]) if "edge" in keys else None
    if style == "mono":
        blk = mono_blocks(blk, np.asarray(gb["ground"]) if "ground" in keys else None)
    return up2(colorize_frames(ld, sh, blk, pid, edge, team, t0, TEAM_RGB2 if style == "mat2" else None))


def noent(gb):
    """Copy of gb without players (module docstring) -> dict of arrays."""
    g = {k: np.array(gb[k]) for k in gb.keys()}
    m = g["pid"] > 0
    assert m.ndim == 3, m.shape
    g["pid"] = np.zeros_like(g["pid"])
    if not m.any():
        return g
    w = m.shape[2]; x = np.arange(w)
    L = np.maximum.accumulate(np.where(m, -1, x), axis=2)                        # nearest non-player at / left of x (-1: none)
    R = np.minimum.accumulate(np.where(m, w, x)[..., ::-1], axis=2)[..., ::-1]   # at / right of x (w: none)
    dl, dr = np.where(L >= 0, x - L, 2 * w), np.where(R < w, R - x, 2 * w)
    src, none = np.clip(np.where(dl <= dr, L, R), 0, w - 1), np.minimum(dl, dr) >= 2 * w
    for k, v in g.items():
        if k == "pid" or v.shape[:3] != m.shape:
            continue
        v = np.take_along_axis(v, src.reshape(src.shape + (1,) * (v.ndim - 3)), axis=2)
        v[none] = 0
        g[k] = v
    return g


def write_video(path, frames, fps=16, crf=10):
    """frames [T, H, W, 3] uint8 (H, W even) -> h264 mp4 at path."""
    frames = np.asarray(frames)
    assert frames.ndim == 4 and frames.shape[3] == 3 and frames.dtype == np.uint8, (frames.shape, frames.dtype)
    H, W = frames.shape[1:3]
    assert H % 2 == 0 and W % 2 == 0, f"yuv420p needs even sizes, got {H} x {W}"
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    tmp = path + ".part.mp4"
    try:
        import av
    except ImportError:
        av = None
    if av is not None:
        with av.open(tmp, "w", format="mp4") as c:
            s = c.add_stream("libx264", rate=fps); s.width, s.height, s.pix_fmt = W, H, "yuv420p"; s.options = {"crf": str(crf)}
            for fr in frames:
                for pk in s.encode(av.VideoFrame.from_ndarray(np.ascontiguousarray(fr), format="rgb24")):
                    c.mux(pk)
            for pk in s.encode():
                c.mux(pk)
    else:
        import imageio.v2 as iio
        with iio.get_writer(tmp, format="FFMPEG", fps=fps, codec="libx264", quality=None, pixelformat="yuv420p",
                            macro_block_size=1, output_params=["-crf", str(crf)]) as wr:
            for fr in frames:
                wr.append_data(np.ascontiguousarray(fr))
    os.replace(tmp, path)


def random_gbuf(rng, F=6, h=24, w=40, block_dtype=np.uint8, nblk=16):
    """Random G-buffer for tests: sky, out-of-table block ids, several player ids, edges."""
    ld = rng.integers(1, 256, (F, h, w)).astype(np.uint8); ld[rng.random((F, h, w)) < 0.2] = 0
    pid = np.where(rng.random((F, h, w)) < 0.15, rng.integers(1, 4, (F, h, w)), 0).astype(np.uint8)
    pid[:, 3] = 2                                                                # one all-player row per frame
    return {"logdepth": ld, "shade": rng.integers(0, 256, (F, h, w)).astype(np.uint8), "pid": pid,
            "block": rng.integers(0, nblk, (F, h, w)).astype(block_dtype), "edge": (rng.random((F, h, w)) < 0.1).astype(np.uint8) * 255}


class _RefColorize:
    """The per-frame reference colorize: proxy_frames(gb, "mat") must equal it
    bit for bit (selftest)."""

    @staticmethod
    def colorize(g, team_of, t):
        blk = MCW.BLOCK_RGB[np.minimum(g["block"], len(MCW.BLOCK_RGB) - 1)].astype(np.float32)
        sh = g["shade"].astype(np.float32)[..., None] / 255.0
        img = blk * (0.35 + 0.65 * sh)
        if "edge" in g:
            img[g["edge"] > 0] *= 0.6                                                 # block grid lines
        img[g["logdepth"] == 0] = (150, 190, 235)                                    # sky
        for eid in np.unique(g["pid"]):
            if eid == 0:
                continue
            m = g["pid"] == eid; team = team_of.get(int(eid) - 1, -1)
            col = np.array(MCW.TEAM_RGB[team] if team in MCW.TEAM_RGB else ((255, 255, 255) if (t // 2) % 2 else (220, 40, 30)), np.float32)
            img[m] = col * (0.45 + 0.55 * sh[m])
        return img.clip(0, 255).astype(np.uint8)


def selftest(seed=0):
    """-> list of failed checks (empty = pass)."""
    MD = _RefColorize                                                           # the per-frame reference colorize
    rng, fails = np.random.default_rng(seed), []

    def check(ok, msg):
        print(("PASS " if ok else "FAIL ") + msg, flush=True)
        if not ok:
            fails.append(msg)
    for dt in (np.uint8, np.uint16):
        for team, t0 in ((0, 0), (1, 5), (-1, 0), (-1, 3)):
            gb = random_gbuf(rng, block_dtype=dt, nblk=300 if dt == np.uint16 else 16)
            px = proxy_frames(gb, "mat", team, t0)
            ref = [up2(MD.colorize({k: v[k_] for k, v in gb.items()}, {int(p) - 1: team for p in np.unique(gb["pid"][k_]) if p > 0}, t0 + k_)[None])[0]
                   for k_ in range(len(gb["pid"]))]
            check(px.shape == (6, 48, 80, 3) and px.dtype == np.uint8 and all(np.array_equal(px[k], ref[k]) for k in range(6)),
                  f"mat == the reference colorize per frame (block {dt.__name__}, team {team}, t0 {t0})")
            noedge = {k: v for k, v in gb.items() if k != "edge"}
            ref = [up2(MD.colorize({k: v[k_] for k, v in noedge.items()}, {int(p) - 1: team for p in np.unique(gb["pid"][k_]) if p > 0}, t0 + k_)[None])[0]
                   for k_ in range(6)]
            check(all(np.array_equal(proxy_frames(noedge, "mat", team, t0)[k], ref[k]) for k in range(6)), f"mat == colorize without edge (team {team})")
            mono = proxy_frames(gb, "mono", team, t0)
            g2 = dict(gb, block=np.where((gb["block"] > 0) & ~np.isin(gb["block"], GROUND_IDS), MONO, gb["block"]).astype(dt))
            check(np.array_equal(mono, proxy_frames(g2, "mat", team, t0)), f"mono == mat with non-ground solid -> {MCW.BLOCK_NAMES[MONO]} ({dt.__name__})")
    gb = random_gbuf(rng)
    ground = (rng.random(gb["pid"].shape) < 0.5).astype(np.uint8)
    g2 = dict(gb, block=np.where(gb["block"] > 0, np.where(ground > 0, MCW.GRASS, MONO), gb["block"]).astype(np.uint8))
    check(np.array_equal(proxy_frames(dict(gb, ground=ground), "mono"), proxy_frames(g2, "mat")), "mono with a 0 / 1 ground mask (masked solid -> GRASS)")
    for dt in (np.uint8, np.uint16):                                             # a labelled copy in the gbuf_mat.npz layout
        gb = random_gbuf(rng, block_dtype=dt, nblk=12); b = gb["block"]
        lab = dict(gb, block=np.where(b > 0, rng.integers(1, 12, b.shape), 0).astype(dt), ground=np.where(np.isin(b, GROUND_IDS), b, 0).astype(np.uint8),
                   method=method_array({"unshade": "gbuf", "version": 1}))
        sig, m = method_of(lab)
        check(np.array_equal(proxy_frames(lab, "mono"), proxy_frames(gb, "mono")) and not np.array_equal(proxy_frames(lab, "mat"), proxy_frames(gb, "mat"))
              and m == {"unshade": "gbuf", "version": 1} and len(sig) == 10 and method_of(gb) == (None, None),
              f"mono(labelled copy with ground ids + overrides) == mono(original) exactly; method_of ({dt.__name__})")
    gb = random_gbuf(rng)
    src = dict(gb, normal=rng.integers(0, 256, gb["pid"].shape + (3,)).astype(np.uint8), ground=ground, method=method_array({"v": 1}))
    ne, m = noent(src), gb["pid"] > 0
    ok = not ne["pid"].any() and ne["pid"].dtype == gb["pid"].dtype and set(ne) == set(src)
    for f, y in np.ndindex(m.shape[:2]):
        keep = np.nonzero(~m[f, y])[0]
        for x_ in range(m.shape[2]):
            s = x_ if not m[f, y, x_] else keep[np.argmin(np.abs(keep - x_) * 2 + (keep > x_))] if len(keep) else None   # ties left
            for k in ("logdepth", "shade", "block", "edge", "normal", "ground"):
                want = np.zeros_like(src[k][f, y, x_]) if s is None else src[k][f, y, s]
                ok &= bool(np.array_equal(ne[k][f, y, x_], want)) and ne[k].dtype == src[k].dtype
    check(ok, "noent == brute-force nearest non-player pixel in the row (ties left, all-player row -> 0, [F, h, w, 3] too)")
    check(not np.shares_memory(ne["logdepth"], src["logdepth"]) and np.array_equal(src["pid"] > 0, m) and method_of(ne) == method_of(src),
          "noent copies (input unchanged; a 0-d method entry passes through)")
    return fails


if __name__ == "__main__":
    if "--test" in sys.argv:
        f = selftest()
        print("[mc_proxy_style]", "PASS" if not f else f"FAIL {f}", flush=True)
        sys.exit(1 if f else 0)
