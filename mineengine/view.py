"""Viewer for cap2proxy episodes of capture data: one mp4 per episode, both players in time sync (Bravo frame =
nearest t_ms to Alpha's), rows Alpha / Bravo, columns
  real (aligned capture frame, cap2proxy side_by_side left half; black where the entry has no usable frame) |
  white box (cap2proxy proxy.mp4: geometry, block colours, entities, dig cracks; no UI drawn - HUD / player state is in the
  panel; an older cap2proxy run without G-buffer v2 drew the decorated proxy here) |
  G-buffer (logdepth TURBO, sky light blue, instance pids on top: the other player MAGENTA, mobs / items one colour per pid, edges black),
and a state panel: episode type and time, block changes so far, per player HP / food / held item / slot / using / sneaking /
crosshair target, events of the last second (damage attacker -> victim, death, place, break, prime, explode, use, held item
change; not item spawn / remove), and a top-down map of
the episode box (start heights, grey; cells that change during the episode, orange; mobs yellow; Alpha blue / Bravo red
arrows = position + yaw). Window: the --seconds span with the most events (whole episode if shorter). H.264 via ffmpeg.
  python -m mineengine.view --ep EPISODE_DIR --report WORLD_EVENTS_REPORT --out OUT.mp4 [--seconds 50] [--ffmpeg BIN]
  python -m mineengine.view --pick PROXY_ROOTS --capture CAPTURE_ROOT --types pvpMelee,coBuild,... --outdir DIR   (one per type)
    (--pick: PROXY_ROOTS is a glob of directories <TAG>_b<N>; <TAG>_b<N>/proxy is the cap2proxy --out of the capture batch
    CAPTURE_ROOT/<TAG>/data_collection/train/batch_<N>, whose world_events_report.json gives the episode types)
"""
import argparse
import glob
import re
import hashlib
import json
import math
import os
import shutil
import subprocess
import sys

import cv2
import numpy as np

TH, TW, PH = 288, 504, 300                                                       # tile (0.75 of 384 x 672), panel height
FPS = 20
BOTS = ("Alpha", "Bravo")
SHOW = ("damage", "death", "respawn", "place", "break", "explode", "prime", "use_start", "held")   # not item spawn / remove
PID_RGB = [(255, 0, 255), (255, 210, 0), (0, 230, 255), (120, 255, 80), (255, 120, 40), (180, 120, 255), (255, 255, 255)]


def read_range(path, a, b):
    """frames a..b-1 of a video (fewer at its end) -> {index: frame}."""
    out = {}
    if not os.path.exists(path):
        return out
    cap = cv2.VideoCapture(path); cap.set(cv2.CAP_PROP_POS_FRAMES, a)
    for k in range(a, b):
        ok, f = cap.read()
        if not ok:
            break
        out[k] = f
    return out


def state_rgb(states):
    """stable colour per episode state index (hash of the state string); 0 = none."""
    out = np.zeros((len(states), 3), np.uint8)
    for k, e in enumerate(states):
        if e:
            h = int(hashlib.md5(e["state"].encode()).hexdigest()[:6], 16)
            out[k] = (60 + (h & 0xFF) % 190, 60 + (h >> 8 & 0xFF) % 190, 60 + (h >> 16 & 0xFF) % 190)
    return out


FACE_SHADE = np.array([1.0, 0.8, 0.8, 1.0, 0.55, 0.9, 0.9], np.float32)          # face 0..6 (+x,-x,+y,-y,+z,-z)


def state_tile(g, i, srgb):
    """G-buffer v2: per-pixel block state (colour per state, shaded by face), entities on top, sky light blue."""
    img = (srgb[np.minimum(g["bstate"][i], len(srgb) - 1)].astype(np.float32) * FACE_SHADE[g["face"][i]][..., None]).astype(np.uint8)[..., ::-1].copy()
    img[g["logdepth"][i] == 0] = (235, 206, 135)
    pid = g["pid"][i]
    for p in np.unique(pid[pid > 0]):
        r, gg, b = PID_RGB[0] if p == 1 else PID_RGB[1 + (int(p) - 2) % (len(PID_RGB) - 1)]
        img[pid == p] = (b, gg, r)
    img[g["edge"][i] > 0] = 0
    return cv2.resize(img, (TW, TH), interpolation=cv2.INTER_NEAREST)


def gbuf_tile(g, i):
    d = 255 - g["logdepth"][i]; img = cv2.applyColorMap(d, cv2.COLORMAP_TURBO)
    img[g["logdepth"][i] == 0] = (235, 206, 135)                                 # sky (no hit, logdepth 0): light blue (BGR)
    pid = g["pid"][i]
    for p in np.unique(pid[pid > 0]):
        r, gg, b = PID_RGB[0] if p == 1 else PID_RGB[1 + (int(p) - 2) % (len(PID_RGB) - 1)]
        img[pid == p] = (b, gg, r)
    img[g["edge"][i] > 0] = 0
    return cv2.resize(img, (TW, TH), interpolation=cv2.INTER_NEAREST)


def label(img, text, y=18, col=(255, 255, 255)):
    cv2.putText(img, text, (6, y), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 0, 0), 3, cv2.LINE_AA)
    cv2.putText(img, text, (6, y), cv2.FONT_HERSHEY_SIMPLEX, 0.5, col, 1, cv2.LINE_AA)


def topdown(world):
    s, e = world["start"], world["final"]                                         # [x, y, z] ids, 0 = air
    occ = s > 0; h = np.where(occ.any(1), occ.shape[1] - 1 - np.argmax(occ[:, ::-1, :], 1), -1)   # [x, z] top solid y
    base = np.zeros(h.shape, np.uint8); v = h >= 0
    if v.any():
        base[v] = np.clip((h[v] - h[v].min()) * 6 + 60, 0, 230)
    img = cv2.cvtColor(base, cv2.COLOR_GRAY2BGR); ch = (s != e).any(1)
    img[ch] = (0, 140, 255)
    return img                                                                    # [x, z] -> image rows x, cols z


def draw_map(panel, x0, mp, origin, rows_now, ents):
    k = (PH - 20) // mp.shape[0]; im = cv2.resize(mp, (mp.shape[1] * k, mp.shape[0] * k), interpolation=cv2.INTER_NEAREST)
    def at(p):
        return int((p[2] - origin[2]) * k), int((p[0] - origin[0]) * k)        # (col = z, row = x)
    for e in ents:
        if e.get("pos") is not None and e.get("type") != "player":
            cv2.circle(im, at(e["pos"]), 2, (0, 230, 255), -1)
    for r, col in zip(rows_now, ((255, 120, 0), (0, 0, 255))):
        if r is None or r["cam"]["eye"] is None:
            continue
        c = at(r["cam"]["eye"]); yaw = float(r["cam"]["yaw"])                    # mineflayer yaw: 0 = -z, +pi/2 = -x
        tip = (int(c[0] - 9 * math.cos(yaw)), int(c[1] - 9 * math.sin(yaw)))
        cv2.circle(im, c, 4, col, -1); cv2.line(im, c, tip, col, 2)
    h, w = im.shape[:2]; panel[10:10 + h, x0:x0 + w] = im[:PH - 10, :panel.shape[1] - x0]


def ev_text(e):
    k = e["kind"]
    if k == "damage":
        return f"damage {e.get('attacker') or '?'} -> {e.get('victim') or e.get('name')} {e.get('damage') or 0:.1f}"
    return f"{k} {e.get('name') or ''}".strip()


def view(ep, report, out, seconds=50.0, ffmpeg="ffmpeg", etype=None):
    info = json.load(open(os.path.join(ep, "episode.json")))
    if etype is None and report:
        r = json.load(open(report)); key = (f"{int(info['eid']):06d}", f"{int(info['inst']):03d}")
        etype = next((x.get("episode_type") for x in r["episodes"] if (x["episode"], x["instance"]) == key), "?")
    world = np.load(os.path.join(ep, "world.npz")); mp = topdown(world); origin = world["origin"]
    sp = os.path.join(ep, "states.json"); states = json.load(open(sp))["states"] if os.path.exists(sp) else []; srgb = state_rgb(states); top = []
    D = {}
    for b in BOTS:
        rows = [json.loads(l) for l in open(os.path.join(ep, b, "state.jsonl"))]
        D[b] = {"rows": rows, "t": np.array([x["t_ms"] for x in rows])}
    A = D["Alpha"]; F = len(A["rows"]); n = min(F, int(seconds * FPS))
    jB = np.clip(np.searchsorted(D["Bravo"]["t"], A["t"]), 0, len(D["Bravo"]["rows"]) - 1)
    dens = np.array([len(A["rows"][i]["events"]) + len(D["Bravo"]["rows"][j]["events"]) for i, j in enumerate(jB)], float)
    i0 = int(np.argmax(np.convolve(dens, np.ones(n), "valid"))) if F > n else 0
    for b in BOTS:                                                                # only the window: long episodes are GBs of frames
        d = D[b]; a_, b_ = (i0, i0 + n) if b == "Alpha" else (int(jB[i0]), int(jB[i0 + n - 1]) + 1)
        z = np.load(os.path.join(ep, b, "gbuf.npz")); d["v2"] = "bstate" in z.files
        d["g"] = {k: z[k][a_:b_] for k in ("logdepth", "pid", "edge") + (("bstate", "face") if d["v2"] else ())}; d["g0"] = a_
        d["sbs"] = read_range(os.path.join(ep, b, "side_by_side.mp4"), a_, b_); d["px"] = read_range(os.path.join(ep, b, "proxy.mp4"), a_, b_)
    W = 3 * TW; H = 2 * TH + PH
    if shutil.which(ffmpeg) or os.path.exists(ffmpeg):
        pr = subprocess.Popen([ffmpeg, "-y", "-loglevel", "error", "-f", "rawvideo", "-pix_fmt", "bgr24", "-s", f"{W}x{H}", "-r", str(FPS), "-i", "-",
                               "-c:v", "libx264", "-preset", "veryfast", "-crf", "22", "-pix_fmt", "yuv420p", "-movflags", "+faststart", out], stdin=subprocess.PIPE)
        put, close = (lambda im: pr.stdin.write(im.tobytes())), (lambda: (pr.stdin.close(), pr.wait()))
    else:                                                                         # no ffmpeg (local test): mp4v
        vw = cv2.VideoWriter(out, cv2.VideoWriter_fourcc(*"mp4v"), FPS, (W, H)); put, close = vw.write, vw.release
    recent = []
    for i in range(i0, i0 + n):
        canvas = np.zeros((H, W, 3), np.uint8); now = []
        for row_k, b in enumerate(BOTS):
            d = D[b]; j = i if b == "Alpha" else int(np.clip(np.searchsorted(d["t"], A["t"][i]), 0, len(d["rows"]) - 1))
            r = d["rows"][j]; now.append(r)
            if j in d["sbs"]:
                real, prox = d["sbs"][j][:, :672], d["sbs"][j][:, 672:]
            else:
                real, prox = np.zeros((384, 672, 3), np.uint8), d["px"].get(j, np.zeros((384, 672, 3), np.uint8))
            jg = min(max(j - d["g0"], 0), len(d["g"]["pid"]) - 1)
            tiles = [cv2.resize(real, (TW, TH), interpolation=cv2.INTER_AREA), cv2.resize(prox, (TW, TH), interpolation=cv2.INTER_AREA),
                     state_tile(d["g"], jg, srgb) if d["v2"] else gbuf_tile(d["g"], jg)]
            names = [f"{b}: real capture", f"{b}: white-box proxy" + ("" if d["v2"] else " + state"),
                     f"{b}: block state per pixel + instances" if d["v2"] else f"{b}: G-buffer depth + instances"]
            if d["v2"] and b == "Alpha":
                v, c = np.unique(d["g"]["bstate"][jg][d["g"]["bstate"][jg] > 0], return_counts=True)
                top = [states[int(x)]["state"].replace("minecraft:", "") for x in v[np.argsort(-c)][:3]]
            for c, (t, nm) in enumerate(zip(tiles, names)):
                label(t, nm); canvas[row_k * TH:(row_k + 1) * TH, c * TW:(c + 1) * TW] = t
            for e in r["events"]:
                if e["kind"] in SHOW:
                    recent.append((A["t"][i], b, ev_text(e)))
        recent = [x for x in recent if A["t"][i] - x[0] < 1000][-6:]
        pnl = canvas[2 * TH:]; t_s = (A["t"][i] - A["t"][0]) / 1000.0
        label(pnl, f"{etype}   {info['name']}   t = {t_s:5.1f} s   block changes so far: {now[0]['block_version']}", 20, (120, 255, 255))
        for k, r in enumerate(now):
            s = r["self"]; y = 46 + 70 * k; tgt = s.get("target")
            label(pnl, f"{s['name']}: HP {s.get('health') if s.get('health') is not None else '?'}   food {s.get('food')}   held {s.get('held_item')} [slot {s.get('held_slot')}]"
                       f"{'   USING' if s.get('using') else ''}{'   SNEAK' if s.get('sneaking') else ''}{'   DEAD' if s.get('alive') is False else ''}", y)
            label(pnl, f"   pos {np.round(s['pos'], 1).tolist() if s.get('pos') else '?'}   target {tgt if tgt else '-'}   cam {r['cam']['source']}"
                       f"   {'paired' if r.get('usable') else 'no usable frame'}", y + 20, (200, 200, 200))
        for k, (_, b, txt) in enumerate(recent):
            label(pnl, f"[{b}] {txt}", 196 + 16 * k, (120, 200, 255))
        if top:
            label(pnl, "Alpha view, top block states: " + "  |  ".join(t[:46] for t in top), 176, (160, 255, 160))
        label(pnl, "map: start heights (grey), changed cells (orange), mobs (yellow), Alpha blue / Bravo red", PH - 8, (200, 200, 200))
        draw_map(pnl, W - (PH - 20) // mp.shape[0] * mp.shape[1] - 20, mp, origin, now, now[0]["entities"])
        put(canvas)
    close()
    return {"episode": info["name"], "type": etype, "frames": n, "start_s": round((A["t"][i0] - A["t"][0]) / 1000.0, 1), "out": out}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--ep"); ap.add_argument("--report"); ap.add_argument("--out"); ap.add_argument("--seconds", type=float, default=50.0)
    ap.add_argument("--ffmpeg", default="ffmpeg"); ap.add_argument("--pick", default=None); ap.add_argument("--capture"); ap.add_argument("--types")
    ap.add_argument("--outdir")
    a = ap.parse_args()
    if not a.pick:
        print(json.dumps(view(a.ep, a.report, a.out, a.seconds, a.ffmpeg))); return
    want, got = a.types.split(","), {}
    os.makedirs(a.outdir, exist_ok=True)
    for root in sorted(glob.glob(a.pick)):
        m = re.fullmatch(r"(.+)_b(\d+)", os.path.basename(os.path.normpath(root)))
        if not m:
            raise SystemExit(f"--pick: {root} is not named <TAG>_b<N> (see the usage line at the top of this module)")
        cap = os.path.join(a.capture, m.group(1), "data_collection", "train", "batch_" + m.group(2))
        rep = os.path.join(cap, "world_events_report.json")
        if not os.path.exists(rep):
            continue
        types = {(x["episode"], x["instance"]): x.get("episode_type") for x in json.load(open(rep))["episodes"]}
        for ep in sorted(glob.glob(os.path.join(root, "proxy", "episodes", "*"))):
            if not os.path.exists(os.path.join(ep, "episode.json")) or not all(os.path.exists(os.path.join(ep, b, "side_by_side.mp4")) for b in BOTS):
                continue
            info = json.load(open(os.path.join(ep, "episode.json"))); t = types.get((f"{int(info['eid']):06d}", f"{int(info['inst']):03d}"))
            if t in want and t not in got:
                try:
                    got[t] = view(ep, None, os.path.join(a.outdir, f"{t}__{os.path.basename(root)}__{info['name']}.mp4"), a.seconds, a.ffmpeg, t)
                    print(json.dumps(got[t]), flush=True)
                except Exception as e:  # noqa: BLE001
                    print(f"[view] {ep}: {e!r}", flush=True)
        if len(got) == len(want):
            break
    json.dump(got, open(os.path.join(a.outdir, "index.json"), "w"), indent=1)
    print(f"[view] {len(got)} / {len(want)} types: {sorted(got)}; missing {sorted(set(want) - set(got))}")


if __name__ == "__main__":
    main()
