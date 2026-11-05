"""Per-entity reference tiles for cap2proxy clips.
Per clip, reference images of the OTHER player (G-buffer slot pid 1) as 384 x 224 tiles (H x W; three tiles side by side
fill one 384 x 672 frame, tile borders at pixel columns 224 / 448):
  skin : the other player's skin PNG (cap2proxy episode.json bots[pov].other -> bots[other].skin.sha1 ->
         <batch base>/output/skins/<sha1>.png: T18 (per-player skin record), or cap2proxy's adapter of the engine's state_metadata.skins), only
         when that skin record says ok (applied for that bot without its own assignment errors, PNG sha256-verified, a
         finalized camera meta - when there is one - loaded it); the 64 x 64 atlas
         scaled x3 nearest, centred on grey;
  crop : the other player cut from this POV's aligned real video (cap2proxy.real_frames: the 384 x 672 clip frame)
         at usable paired action entries (episode cams.npz pair_ok, when written) >= --gap s outside the
         clip window, where the episode G-buffer
         (episodes/<ep>/<pov>/gbuf.npz) has >= --min-px pixels of pid 1 (192 x 336): the largest first, then the largest
         >= --spread s from those taken; box of the pid-1 pixels x 2, padded --pad, letterboxed on grey.
-> OUT/refs/<clip id>.npz {tiles [n, 384, 224, 3] uint8, slot [n] (1), kind [n] (0 skin, 1 crop), frame [n] (action
   entry; -1 skin), area [n] (pid-1 pixels; 0 skin)} and OUT/refs/summary.json. Clips without a skin and without a crop
get no file (no references for that clip). Mobs (pid >= 2) get no refs in v0.
  python -m mineengine.entity_refs --out CAP2PROXY_OUT [--gap 2 --min-px 150 --ncrop 2 --spread 1 --pad 0.15] [--force]
  python -m mineengine.entity_refs --test           (unit checks; the end-to-end run is part of mineengine.cap_selftest)
"""
import argparse
import glob
import json
import os
import sys

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
from mineengine import cap2proxy as C2P  # noqa: E402

TH, TW, GREY = 384, 224, 128
FPS_ACT = 20.0                                                                   # action entries (cap2proxy: frame i = entry i)


def letterbox(img, th=TH, tw=TW, fill=GREY, nearest=False):
    """fit img [h, w, 3] inside a th x tw tile, centred, aspect kept."""
    import cv2
    h, w = img.shape[:2]; s = min(th / h, tw / w)
    nh, nw = max(1, int(round(h * s))), max(1, int(round(w * s)))
    r = cv2.resize(img, (nw, nh), interpolation=cv2.INTER_NEAREST if nearest else (cv2.INTER_AREA if s < 1 else cv2.INTER_LINEAR))
    out = np.full((th, tw, 3), fill, np.uint8); y0, x0 = (th - nh) // 2, (tw - nw) // 2
    out[y0:y0 + nh, x0:x0 + nw] = r
    return out


def skin_tile(path):
    import cv2
    im = cv2.imread(path, cv2.IMREAD_UNCHANGED)
    if im is None:
        return None
    if im.ndim == 2:
        im = np.repeat(im[..., None], 3, -1)
    if im.shape[2] == 4:                                                         # transparent texels -> grey
        a = im[..., 3:4].astype(np.float32) / 255.0
        im = (im[..., :3].astype(np.float32) * a + GREY * (1 - a)).astype(np.uint8)
    im = im[..., :3][..., ::-1]                                                  # BGR -> RGB
    s = max(1, min(TH, TW) // max(im.shape[:2]))
    return letterbox(np.ascontiguousarray(im.repeat(s, 0).repeat(s, 1)), nearest=True)


def pick_frames(area, lo, hi, n_pair, gap, min_px, ncrop, spread, usable=None):
    """entries j < n_pair (and usable[j]) outside [lo - gap, hi + gap] (entries) with area >= min_px: largest, then largest >=
    spread away."""
    j = np.arange(min(len(area), n_pair))
    ok = ((j < lo - gap) | (j > hi + gap)) & (area[:len(j)] >= min_px) & (np.asarray(usable, bool)[:len(j)] if usable is not None else True)
    cand = sorted(j[ok].tolist(), key=lambda q: (-int(area[q]), q)); took = []
    for q in cand:
        if len(took) >= ncrop:
            break
        if all(abs(q - t) >= spread for t in took):
            took.append(q)
    return took


def crop_tile(frame, mask, pad):
    """frame [384, 672, 3], mask [192, 336] bool (pid 1) -> tile around the mask's box x 2, padded by pad of its size."""
    ys, xs = np.nonzero(mask)
    y0, y1, x0, x1 = 2 * ys.min(), 2 * ys.max() + 2, 2 * xs.min(), 2 * xs.max() + 2
    ph, pw = int(round((y1 - y0) * pad)), int(round((x1 - x0) * pad))
    H, W = frame.shape[:2]
    return letterbox(frame[max(0, y0 - ph):min(H, y1 + ph), max(0, x0 - pw):min(W, x1 + pw)])


def frames_at(video, want):
    """{entry: [384, 672, 3] RGB} for the wanted entries (one pass over the aligned video)."""
    out, last = {}, max(want)
    for i, fr in enumerate(C2P.real_frames(video, 2 * C2P.H, 2 * C2P.W)):
        if i in want:
            out[i] = fr
        if i >= last:
            break
    return out


def refs_for_episode(out, ep_name, clips, a):
    """all clips of one episode -> {clip id: summary}; one G-buffer load and one video pass per POV."""
    ej = json.load(open(os.path.join(out, "episodes", ep_name, "episode.json"))); res = {}
    for pov in sorted({m["player"] for m in clips.values()}):
        mine = {c: m for c, m in clips.items() if m["player"] == pov}
        bot = ej["bots"].get(pov, {}); other = bot.get("other")
        sk = ((ej["bots"].get(other) or {}).get("skin") or {}) if other else {}
        sp = os.path.join(ej["base"], "output", "skins", sk["sha1"] + ".png") if sk.get("sha1") and sk.get("ok") is True else None
        skin = skin_tile(sp) if sp and os.path.exists(sp) else None
        gp = os.path.join(out, "episodes", ep_name, pov, "gbuf.npz")
        pid = np.load(gp)["pid"] if os.path.exists(gp) else None
        area = (pid == 1).sum((1, 2)) if pid is not None else None
        cp = os.path.join(out, "episodes", ep_name, pov, "cams.npz")
        cz = np.load(cp) if os.path.exists(cp) else None
        usable = cz["pair_ok"] if cz is not None and "pair_ok" in cz.files else None
        gap, spread = int(round(a.gap * FPS_ACT)), int(round(a.spread * FPS_ACT))
        picks = {}
        for c, m in mine.items():
            src = m["src_frames"]
            picks[c] = pick_frames(area, min(src), max(src), int(m.get("n_pair") or 0), gap, a.min_px, a.ncrop, spread, usable) if area is not None else []
        want = {q for v in picks.values() for q in v}
        vp = next((m.get("video") for m in mine.values() if m.get("video")), None)
        frs = frames_at(vp, want) if want and vp and os.path.exists(vp) else {}
        for c in mine:
            tiles, kind, frame, ar = [], [], [], []
            if skin is not None:
                tiles.append(skin); kind.append(0); frame.append(-1); ar.append(0)
            for q in picks[c]:
                if q in frs:
                    tiles.append(crop_tile(frs[q], pid[q] == 1, a.pad)); kind.append(1); frame.append(q); ar.append(int(area[q]))
            res[c] = {"skin": sp if skin is not None else None, "skin_rejected": (sk.get("problems") or True) if sk.get("ok") is False else None,
                      "crops": [f for f, k in zip(frame, kind) if k == 1], "n": len(tiles)}
            if tiles:
                dst = os.path.join(out, "refs", c + ".npz"); tmp = dst + f".{os.getpid()}.part.npz"
                np.savez_compressed(tmp, tiles=np.stack(tiles), slot=np.ones(len(tiles), np.int64), kind=np.array(kind, np.int64),
                                    frame=np.array(frame, np.int64), area=np.array(ar, np.int64))
                os.replace(tmp, dst)
    return res


def main(argv=None):
    ap = argparse.ArgumentParser(); ap.add_argument("--out"); ap.add_argument("--gap", type=float, default=2.0)
    ap.add_argument("--min-px", type=int, default=150); ap.add_argument("--ncrop", type=int, default=2)
    ap.add_argument("--spread", type=float, default=1.0); ap.add_argument("--pad", type=float, default=0.15)
    ap.add_argument("--force", action="store_true"); ap.add_argument("--test", action="store_true")
    a = ap.parse_args(argv)
    if a.test:
        return selftest()
    os.makedirs(os.path.join(a.out, "refs"), exist_ok=True)
    by_ep = {}
    for mp in sorted(glob.glob(os.path.join(a.out, "clips", "*", "meta.json"))):
        c = os.path.basename(os.path.dirname(mp))
        if not a.force and os.path.exists(os.path.join(a.out, "refs", c + ".npz")):
            continue
        m = json.load(open(mp)); by_ep.setdefault(m["episode"], {})[c] = m
    summ, nfail = {}, 0
    for e, clips in by_ep.items():
        try:
            summ.update(refs_for_episode(a.out, e, clips, a))
        except Exception as ex:                                                  # one bad episode does not stop the batch
            import traceback
            traceback.print_exc(); print(f"[entity_refs] {e}: {type(ex).__name__}: {ex}", flush=True); nfail += 1
    sp = os.path.join(a.out, "refs", "summary.json"); old = json.load(open(sp)) if os.path.exists(sp) else {}
    old.update(summ); json.dump(old, open(sp + ".part", "w"), indent=1); os.replace(sp + ".part", sp)
    n_skin = sum(1 for v in summ.values() if v["skin"]); n_crop = sum(len(v["crops"]) for v in summ.values())
    print(f"[entity_refs] {len(summ)} clips in {len(by_ep)} episodes: {sum(1 for v in summ.values() if v['n'])} with refs, "
          f"skin {n_skin}, crops {n_crop}, failed episodes {nfail}", flush=True)
    return 1 if nfail else 0


def selftest():
    """unit checks of the pure parts (frame choice, crop box, skin tile); the end-to-end path runs in mineengine.cap_selftest."""
    import tempfile
    import cv2
    area = np.zeros(200, np.int64); area[[10, 11, 12, 90, 150, 151, 180]] = [500, 900, 400, 600, 800, 790, 100]
    got = pick_frames(area, 60, 140, 190, 40, 150, 2, 20)                         # window 60..140, gap 40 -> j < 20 or j > 180
    assert got == [11], got                                                      # 150 / 151 are within 40 of 140; 180 too small
    got = pick_frames(area, 100, 110, 200, 5, 150, 3, 20)
    assert got == [11, 150, 90], got                                             # 151 and 10 / 12 are within 20 of a pick
    us = np.ones(200, bool); us[[11, 150]] = False
    got = pick_frames(area, 100, 110, 200, 5, 150, 3, 20, us)
    assert got == [151, 90, 10], got                                             # unusable pairs (rejected by cap2proxy) never cropped
    fr = np.zeros((384, 672, 3), np.uint8); fr[100:300, 200:260] = 255
    m = np.zeros((192, 336), bool); m[50:150, 100:130] = True
    t = crop_tile(fr, m, 0.0)
    assert t.shape == (TH, TW, 3) and t[TH // 2, TW // 2].tolist() == [255, 255, 255]
    with tempfile.TemporaryDirectory() as d:
        sk = np.zeros((64, 64, 4), np.uint8); sk[:8, :8] = (0, 0, 255, 255)           # BGRA red texel block
        cv2.imwrite(os.path.join(d, "s.png"), sk)
        st = skin_tile(os.path.join(d, "s.png"))
        assert st.shape == (TH, TW, 3)
        y0, x0 = (TH - 192) // 2, (TW - 192) // 2
        assert st[y0 + 1, x0 + 1].tolist() == [255, 0, 0] and st[y0 + 100, x0 + 100].tolist() == [GREY] * 3, (st[y0 + 1, x0 + 1], st[y0 + 100, x0 + 100])
    print("[entity_refs] selftest PASS (frame choice, crop box, skin tile)", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
