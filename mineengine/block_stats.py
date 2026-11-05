"""World-level block-state statistics of capture episodes against the jar block registry (which
blocks / states occur, how often they change). WORLD-LEVEL ONLY: the blocks of the logged box (~65^3 around
the players) and their logged changes, not what any camera saw; per-clip visibility needs G-buffers re-rendered with
btype / bstate. Per episode (cap_reader.episodes; start snapshot + block_state records replayed by BlockTimeline, prev =
the replayed state; a log without block_state records falls back to the block events, source "events"):
  states     distinct full state strings of the start box (with voxel counts) and over the episode (start + the state of
             every in-box record) -> (type, vkey, gid, tgid, mgid) by mc_registry.Registry.resolve; counted: states, types,
             variants (type, vkey), the three group keys (KEYS: mgid texture multiset = appearance variant,
             tgid texture set, gid appearance+geometry), cap_palette ids; invisible types (air, cave_air, void_air,
             barrier, light, structure_void) are left out of all but states;
  changes    every in-box record: noop (state == prev), prop (same type: one kind per changed property, "furnace lit
             false->true"; a record changing two properties counts under both, with the record's effect), place
             (air-like -> X), break (X -> air-like), replace (X -> Y); effect per record, the strongest of texture (tgid
             differs) > code_appear > geom_only (gid differs, tgid same) > code_geom > neutral (same gid: door powered,
             leaves distance, fire age, stone <-> infested_stone), where code_* = a changed property carries a hand-table
             role (src code: redstone power, stem age, waterlogged (any type) / fluid level, sign rotation) and is taken
             over the jar outcome when stronger (stem age: gid changes, tint too -> code_appear); unresolved = a side not
             in the registry; appear = texture + geom_only, appear_code = code_appear + code_geom; appear_same_cap =
             texture / geom_only / code_appear with the same cap_palette id before and after (lost by the current
             G-buffer, which keeps cap ids only);
  worlds     an instance runs its episodes in one persistent world (episode_begin.world.uid), so later boxes can cover
             earlier ones; a pre-pass reads every episode_begin, orders a world's episodes by epoch_ms and hands each the
             earlier boxes: per world summed vs unique (union) box voxels and every overlapping pair (share of the later
             box), and unique world voxels = start voxels outside every earlier box of the same world (first episode
             wins), next to the per-episode sums; each group also counts the worlds it is seen in;
  outside    records outside the box (the readback logs the whole loaded world: liquid flows / fire spread in caves, median
             ~64 blocks off the box in a sample capture) are only tallied (records, kinds, Chebyshev distance to the box)
             unless --with-outside (then they join the episode set and the change counts like in-box records, prev = the
             logged prev_state);
  fold       cap_palette.block_id of each state: invisible, own (the type's own id), folded (another type's id: stairs /
             doors -> planks, ores -> host rock, infested_stone -> stone), to_air (decorations drawn as air: torches,
             flowers, crops, rails, ...), to_fluid (seagrass, kelp, waterlogged decorations -> water), cap_unknown (cap
             UNKNOWN); per episode and key merged_* = groups sharing a non-air cap id with another group of that key (the
             texture / shape information the G-buffer drops), air_* = groups drawn as air;
  props      property value frequencies: start-box voxels, episodes, records (as the new state); registry role per type;
  unknown    states of a type not in the registry, multipart combinations never enumerated (gid None), partial strings.
Aggregate: totals, per-episode n / mean / p50 / p90 / max of the counts, top change kinds, per key every seen group
(episodes, start episodes, worlds, start voxels, unique world voxels, records) and the rare ones (seen in < --rare
episodes; histograms by episodes and by worlds), world overlaps, end-snapshot check (replay vs end; an end box unlike the
start box is an end_error, not a pass), registry coverage, per episode type and world type (episode_info); the summary
leads with the texture keys. Files are parsed in --workers processes; registry lookups run in the parent (cached).
Registry: mc_registry.py --jar 1.21.jar --out REG.json.
  python -m mineengine.block_stats --root CAPTURE_ROOT --reg REG.json --out STATS.json [--rare 5] [--top 30] [--workers 8]
                           [--limit N] [--with-outside]   (writes STATS.json and STATS.txt = the text summary, also printed)
  python -m mineengine.block_stats --test [--reg REG.json]   (synthetic two-episode capture; a mini registry unless --reg)
"""
import argparse
import gzip
import json
import multiprocessing as mp
import os
import shutil
import sys
import tempfile
import time
from collections import Counter, defaultdict
from functools import lru_cache

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
from mineengine import cap_palette as CP  # noqa: E402
from mineengine import cap_reader as CR  # noqa: E402
from mineengine import mc_registry as MR  # noqa: E402

AIRLIKE = {"air", "cave_air", "void_air"}
FOLDS = ("invisible", "own", "folded", "to_air", "to_fluid", "cap_unknown")
CATS = ("noop", "prop", "place", "break", "replace", "no_prev")
APPEAR, CODE = ("texture", "geom_only"), ("code_appear", "code_geom")
ERANK = {"neutral": 0, "code_geom": 1, "geom_only": 2, "code_appear": 3, "texture": 4}
KEYS = (("mgid", "mgroups", "texture-multiset groups"), ("tgid", "tgroups", "texture-set groups"), ("gid", "groups", "appearance+geometry groups"))
SETKEYS = ("states", "types", "variants") + tuple(p + n for p in ("", "merged_", "air_") for _, n, _ in KEYS) + ("cap_ids", "unknown")
CHKEYS = ("records", "prop", "prop_appear", "appear", "appear_code", "appear_same_cap")


# ---- per-episode file scan (workers; no registry) -------------------------------------------------------------------
@lru_cache(maxsize=None)
def _status(base):
    try:
        with open(os.path.join(base, "world_events_report.json")) as fh:
            d = json.load(fh)
    except (OSError, ValueError):
        return {}
    return {(int(e["episode"]), int(e["instance"])): e.get("status") for e in d.get("episodes", [])
            if str(e.get("episode", "")).isdigit() and str(e.get("instance", "")).isdigit()}


def _world_type(f):
    for k in sorted(k for k in f if k.startswith("info_") and f[k]):
        try:
            d = CR.load_json(f[k])
            return d.get("world_type") or (d.get("world") or {}).get("world_type")
        except (OSError, ValueError):
            pass
    return None


def _counts(grid, palette):
    c, out = np.bincount(np.asarray(grid).reshape(-1), minlength=len(palette)), Counter()
    for i in np.nonzero(c)[0]:
        out[CR.norm_state(palette[i])] += int(c[i])
    return out


def begin(path):
    """the episode_begin record of an events file (looked for in the first 64 lines) -> dict, {} if none"""
    try:
        with CR._open(path) as fh:
            for i, line in enumerate(fh):
                if '"episode_begin"' in line:
                    return json.loads(line)
                if i >= 63:
                    break
    except (OSError, ValueError):
        pass
    return {}


def worlds(eps):
    """pre-pass: ep["world"] (episode_begin.world.uid), ep["wbox"] ([min, max] of the logged box), ep["t0"], ep["prior"] =
    the boxes of the earlier episodes (epoch_ms, eid) of the same world"""
    by = defaultdict(list)
    for e in eps:
        b = begin(e["files"]["events"]) if e["files"].get("events") else {}
        box = b.get("box") or {}
        e.update(world=(b.get("world") or {}).get("uid"), t0=b.get("epoch_ms"),
                 wbox=[list(box["min"]), list(box["max"])] if box.get("min") and box.get("max") else None)
        if e["world"] and e["wbox"]:
            by[e["world"]].append(e)
    for v in by.values():
        v.sort(key=lambda e: (e["t0"] or 0, e["base"], e["eid"], e["inst"]))
        for i, e in enumerate(v):
            e["prior"] = [x["wbox"] for x in v[:i]]
    return eps


def scan(ep):
    """One episode -> raw block facts: start-box {state: voxels} (and over the voxels outside the earlier boxes of its
    world), [prev, new, where, source, n] record pairs, Chebyshev distances of the outside records to the box, replay vs
    end snapshot."""
    f, t0 = ep["files"], time.time()
    out = dict(eid=ep["eid"], inst=ep["inst"], base=ep["base"], status=_status(ep["base"]).get((ep["eid"], ep["inst"])), world_type=_world_type(f),
               world=ep.get("world"), wbox=ep.get("wbox"), t0=ep.get("t0"))
    miss = [k for k in ("events", "snap_start") if not f.get(k)]
    if miss:
        out.update(error="missing " + ",".join(miss), sec=0.0)
        return out
    try:
        st = {}
        ev = CR.load_events(f["events"], st)
        snap = CR.load_snapshot(f["snap_start"])
        bt = CR.BlockTimeline(snap, ev)
        beg = next((e for e in ev if e.get("type") == "episode_begin"), {})
        recs = [e for e in ev if e.get("type") == "block_state"]
        by_seq = {e.get("seq"): e for e in recs}
        pairs, odist = Counter(), Counter()
        for c in bt.changes:
            pairs[(c["prev_state"], c["state"], "box", (by_seq.get(c["seq"]) or {}).get("source") or bt.source)] += 1
        lo, hi = bt.origin, bt.origin + np.asarray(bt.size) - 1
        for o in bt.outside:
            r = by_seq.get(o["seq"]) or {}
            pairs[(CR.norm_state(r["prev_state"]) if r.get("prev_state") else None, o["state"], "outside", r.get("source") or bt.source)] += 1
            p = np.asarray(o["pos"]); odist[int(np.maximum(0, np.maximum(lo - p, p - hi)).max())] += 1
        start = _counts(snap["grid"], snap["palette"])
        out.update(episode_type=beg.get("episode_type"), snapshot_mode=(beg.get("snapshot") or {}).get("mode"), box=list(bt.size),
                   source=bt.source, n_block_state=len(recs), bad_lines=st.get("bad_lines", 0), n_outside=bt.n_outside,
                   n_prev_mismatch=bt.n_prev_mismatch, n_nonmono=bt.n_nonmono, n_bad=bt.n_bad, start=dict(start),
                   pairs=[[a, b, w, s, n] for (a, b, w, s), n in sorted(pairs.items(), key=lambda kv: str(kv[0]))],
                   outside_dist=sorted(odist.items()), n_new=int(np.prod(bt.size)))
        if ep.get("prior"):
            m = np.ones(bt.size, bool)
            for lo2, hi2 in ep["prior"]:
                a, b = np.maximum(np.asarray(lo2) - bt.origin, 0), np.minimum(np.asarray(hi2) - bt.origin + 1, bt.size)
                if (b > a).all():
                    m[a[0]:b[0], a[1]:b[1], a[2]:b[2]] = False
            out.update(n_new=int(m.sum()), start_new=dict(_counts(np.asarray(snap["grid"])[m], snap["palette"])))
        if f.get("snap_end"):
            end = CR.load_snapshot(f["snap_end"])
            seen = set(start) | {p[1] for p in out["pairs"] if p[2] == "box"}
            c = bt.compare_end(end)
            out["end_mismatch"], out["end_error"] = c.get("mismatch_states"), c.get("error")
            out["end_only"] = sorted(set(_counts(end["grid"], end["palette"])) - seen)
    except Exception as e:                                                       # one bad episode must not stop a dataset pass
        out["error"] = "%s: %s" % (type(e).__name__, e)
    out["sec"] = round(time.time() - t0, 3)
    return out


# ---- registry side (parent) -----------------------------------------------------------------------------------------
class Look:
    """state string -> registry + cap_palette facts, cached."""

    def __init__(self, reg):
        self.reg, self.c = reg, {}

    def __call__(self, s):
        x = self.c.get(s)
        if x is None:
            o = self.reg.resolve(s); t = o["type"]; r = self.reg.types.get(t)
            cap = CP.block_id(s); inv = (r is not None and r["render"] == "invisible") or t in AIRLIKE
            fold = "invisible" if inv else "cap_unknown" if cap == CP.UNKNOWN else "own" if CP.NAMES[cap] == t else \
                "to_air" if cap == CP.AIR else "to_fluid" if cap in (CP.WATER, CP.LAVA) else "folded"
            x = self.c[s] = dict(type=t, vkey=o["vkey"], gid=o["gid"], tgid=o["tgid"], mgid=o.get("mgid"), known=o["known"], exact=o["exact"],
                                 inv=inv, air=t in AIRLIKE, cap=int(cap), fold=fold, props=MR.parse_state(s)[1])
        return x

    def prop(self, t, p):
        return {"role": "unknown_type"} if t not in self.reg.types else self.reg.prop(t, p) or {"role": "absent"}

    def role(self, t, p):
        return self.prop(t, p).get("role")

    def why_unknown(self, x):
        return "type" if not x["known"] else "no_group" if x["gid"] is None else "partial" if not x["exact"] else None


def setstats(L, states):
    xs = [L(s) for s in states]; vis = [x for x in xs if not x["inv"]]
    out = dict(states=len(xs), types=len({x["type"] for x in vis}), variants=len({(x["type"], x["vkey"]) for x in vis if x["vkey"] is not None}))
    for k, n, _ in KEYS:
        caps = defaultdict(set)
        for x in vis:
            if x[k] is not None:
                caps[x["cap"]].add(x[k])
        out.update({n: len({x[k] for x in vis if x[k] is not None}), "merged_" + n: sum(len(g) - 1 for c, g in caps.items() if c != CP.AIR),
                    "air_" + n: len(caps.get(CP.AIR, ()))})
    out.update(cap_ids=len({x["cap"] for x in vis if x["cap"] != CP.AIR}), unknown=sum(L.why_unknown(x) is not None for x in xs))
    return out


def classify(L, a, b):
    """record (prev, new) -> (category, effect, [(kind, type, prop)], same cap id)"""
    if a is None:
        y = L(b)
        return "no_prev", None, [("? -> %s" % y["type"], y["type"], None)], False
    if a == b:
        return "noop", None, [], False
    x, y = L(a), L(b)
    d = []
    if x["type"] == y["type"]:
        d = sorted(p for p in set(x["props"]) | set(y["props"]) if x["props"].get(p) != y["props"].get(p))
        if not d:
            return "noop", None, [], False
        cat, kinds = "prop", [("%s %s %s->%s" % (x["type"], p, x["props"].get(p, "-"), y["props"].get(p, "-")), x["type"], p) for p in d]
    else:
        cat = "replace" if x["air"] == y["air"] else "place" if x["air"] else "break"
        kinds = [("%s -> %s" % (x["type"], y["type"]), None, None)]
    eff = "unresolved" if x["gid"] is None or y["gid"] is None else "neutral" if x["gid"] == y["gid"] else \
        "texture" if x["tgid"] != y["tgid"] else "geom_only"
    if eff != "unresolved":
        for p in d:
            q = L.prop(x["type"], p)
            if q.get("src") == "code" and q.get("role") in ("appear", "geom") and ERANK["code_" + q["role"]] > ERANK[eff]:
                eff = "code_" + q["role"]
    return cat, eff, kinds, eff in APPEAR + ("code_appear",) and x["cap"] == y["cap"]


def dist(v):
    v = np.asarray(v, np.float64)
    if not len(v):
        return dict(n=0)
    return dict(n=int(len(v)), mean=round(float(v.mean()), 2), p50=float(np.percentile(v, 50)), p90=float(np.percentile(v, 90)), max=float(v.max()))


def wdist(hist):
    """[(value, count)] -> dist of the expanded values"""
    return dist(np.repeat([h[0] for h in hist], [h[1] for h in hist])) if hist else dict(n=0)


def overlaps(rs):
    """episodes of one world in (t0, eid) order -> [{a, b, voxels, frac_later}] for every pair of intersecting logged boxes"""
    out = []
    for j, b in enumerate(rs):
        for a in rs[:j]:
            if not (a.get("wbox") and b.get("wbox")):
                continue
            lo, hi = np.maximum(a["wbox"][0], b["wbox"][0]), np.minimum(a["wbox"][1], b["wbox"][1])
            if (hi >= lo).all():
                iv, vb = int(np.prod(hi - lo + 1)), int(np.prod(np.asarray(b["wbox"][1]) - b["wbox"][0] + 1))
                out.append(dict(a=[a["inst"], a["eid"], a.get("episode_type")], b=[b["inst"], b["eid"], b.get("episode_type")], voxels=iv,
                                frac_later=round(iv / max(vb, 1), 4)))
    return out


def aggregate(reg, raws, rare=5, top=30, with_outside=False):
    L = Look(reg)
    eps, errs = [], []
    GS = {k: dict(eps=Counter(), seps=Counter(), vox=Counter(), uvox=Counter(), rec=Counter(), wor=defaultdict(set), ex={}) for k, _, _ in KEYS}
    s_eps, s_vox, s_uvox = Counter(), Counter(), Counter()
    pv = defaultdict(lambda: [0, 0, 0])
    tot, src, eff_by_cat, kinds = Counter(), Counter(), defaultdict(Counter), {}
    out_tot, out_kinds, out_dist, out_eps = Counter(), Counter(), Counter(), 0
    fold_states, fold_vox, fold_uvox, fold_rec, fold_types = defaultdict(set), Counter(), Counter(), Counter(), {}
    cap_groups, cap_types, unk, end_bad, wl = defaultdict(lambda: defaultdict(set)), defaultdict(set), {}, Counter(), defaultdict(list)
    for r in raws:
        if r.get("error"):
            errs.append(dict(eid=r["eid"], inst=r["inst"], base=r["base"], error=r["error"])); continue
        start, wkey = r["start"], r.get("world") or "%s/%d/%d" % (r["base"], r["inst"], r["eid"])
        wl[wkey].append(r)
        outs = [p for p in r["pairs"] if p[2] == "outside"]
        pairs = r["pairs"] if with_outside else [p for p in r["pairs"] if p[2] == "box"]
        for a, b, _, _, n in outs:
            out_tot["records"] += n; out_kinds["%s -> %s" % (L(a)["type"] if a else "?", L(b)["type"]) if not a or L(a)["type"] != L(b)["type"]
                                                  else "%s (state)" % L(b)["type"]] += n
        out_eps += bool(outs)
        for k, n in r.get("outside_dist") or []:
            out_dist[k] += n
        E = set(start) | {p[1] for p in pairs} | {p[0] for p in pairs if p[2] == "outside" and p[0]}
        ss, se = setstats(L, start), setstats(L, E)
        ep_kinds, ch = set(), Counter()
        for a, b, where, so, n in pairs:
            cat, eff, ks, same_cap = classify(L, a, b)
            tot["records"] += n; tot[where] += n; tot[cat] += n; src[so] += n; ch["records"] += n; ch[cat] += n
            if eff:
                eff_by_cat[cat][eff] += n; ch[eff] += n
                if eff in APPEAR:
                    tot["appear"] += n; ch["appear"] += n; ch["prop_appear"] += n if cat == "prop" else 0
                elif eff in CODE:
                    tot["appear_code"] += n; ch["appear_code"] += n
            if same_cap:
                tot["appear_same_cap"] += n; ch["appear_same_cap"] += n
            if cat == "noop":
                continue
            y = L(b)
            for k, _, _ in KEYS:
                if y[k] is not None:
                    GS[k]["rec"][y[k]] += n
            fold_rec[y["fold"]] += n
            for p, v in y["props"].items():
                pv[(p, v)][2] += n
            for k, t, p in ks:
                d = kinds.setdefault(k, dict(kind=k, cat=cat, records=0, episodes=0, effect=Counter(), same_cap=0, type=t, prop=p,
                                             role=L.role(t, p) if p else None, role_src=L.prop(t, p).get("src") if p else None))
                d["records"] += n; d["effect"][eff or "unresolved"] += n; d["same_cap"] += n if same_cap else 0
                if k not in ep_kinds:
                    ep_kinds.add(k); d["episodes"] += 1
        for s, v in start.items():
            x = L(s); s_vox[s] += v; fold_vox[x["fold"]] += v
            for k, _, _ in KEYS:
                if x[k] is not None and not x["inv"]:
                    GS[k]["vox"][x[k]] += v
            for p, val in x["props"].items():
                pv[(p, val)][0] += v
            fold_types.setdefault(x["type"], dict(type=x["type"], fold=Counter(), cap=Counter(), episodes=0, voxels=0))["voxels"] += v
        for s, v in r.get("start_new", start).items():
            x = L(s); s_uvox[s] += v; fold_uvox[x["fold"]] += v
            for k, _, _ in KEYS:
                if x[k] is not None and not x["inv"]:
                    GS[k]["uvox"][x[k]] += v
        for k, _, _ in KEYS:
            GS[k]["seps"].update({L(s)[k] for s in start if L(s)[k] is not None and not L(s)["inv"]})
            ge = {L(s)[k] for s in E if L(s)[k] is not None and not L(s)["inv"]}
            GS[k]["eps"].update(ge)
            for g in ge:
                GS[k]["wor"][g].add(wkey)
        pseen = set()
        for s in sorted(E):
            x = L(s); s_eps[s] += 1; fold_states[x["fold"]].add(s)
            ft = fold_types.setdefault(x["type"], dict(type=x["type"], fold=Counter(), cap=Counter(), episodes=0, voxels=0))
            ft["fold"][x["fold"]] += 1; ft["cap"][CP.NAMES[x["cap"]]] += 1
            if not x["inv"]:
                cap_types[x["cap"]].add(x["type"])
                for k, _, _ in KEYS:
                    if x[k] is not None:
                        GS[k]["ex"].setdefault(x[k], s); cap_groups[k][x["cap"]].add(x[k])
            for p, val in x["props"].items():
                if (p, val) not in pseen:
                    pseen.add((p, val)); pv[(p, val)][1] += 1
            w = L.why_unknown(x)
            if w:
                u = unk.setdefault(s, dict(state=s, why=w, episodes=0, voxels=0, records=0)); u["episodes"] += 1; u["voxels"] += start.get(s, 0)
                u["records"] += sum(p[4] for p in pairs if p[1] == s and p[0] != s)
        for t in {L(s)["type"] for s in E}:
            fold_types[t]["episodes"] += 1
        if "end_only" in r:
            end_bad["episodes_end_checked"] += 1
        else:
            end_bad["episodes_no_end_snapshot"] += 1
        if r.get("end_error"):
            end_bad["episodes_end_error"] += 1
        if r.get("end_mismatch"):
            end_bad["episodes_replay_mismatch"] += 1; end_bad["voxels"] += r["end_mismatch"]
        if r.get("end_only"):
            end_bad["episodes_end_only_states"] += 1; end_bad["end_only_states"] += len(r["end_only"])
        od = r.get("outside_dist") or []
        eps.append(dict(eid=r["eid"], inst=r["inst"], base=r["base"], episode_type=r.get("episode_type"), world_type=r.get("world_type"),
                        world=r.get("world"), status=r.get("status"), snapshot_mode=r.get("snapshot_mode"), box=r.get("box"), source=r.get("source"),
                        n_block_state=r.get("n_block_state"), n_outside=r.get("n_outside"), outside_dist_p50=wdist(od).get("p50"),
                        n_prev_mismatch=r.get("n_prev_mismatch"), end_mismatch=r.get("end_mismatch"), end_error=r.get("end_error"),
                        end_only=r.get("end_only"), new_voxels=r.get("n_new"), sec=r.get("sec"), start=ss, episode=se, changes=dict(ch),
                        gids_start=sorted([[L(s)["gid"], v] for s, v in start.items() if L(s)["gid"] is not None and not L(s)["inv"]]),
                        gids_changed=sorted({L(p[1])["gid"] for p in pairs if p[0] != p[1] and L(p[1])["gid"] is not None})))
    roles = defaultdict(Counter)
    for (t, p) in {(L(s)["type"], p) for s in s_eps for p in L(s)["props"]}:
        roles[p][L.role(t, p)] += 1
    props = {}
    for (p, v), (vox, ne, nr) in pv.items():
        d = props.setdefault(p, dict(prop=p, voxels=0, records=0, types=dict(sorted(roles[p].items())), values={}))
        d["voxels"] += vox; d["records"] += nr; d["values"][v] = dict(voxels=vox, episodes=ne, records=nr)
    RG = {"gid": reg.groups, "tgid": reg.tgroups, "mgid": getattr(reg, "mgroups", [])}
    groups, rare_by = {}, dict(threshold=rare)
    for k, _, _ in KEYS:
        A, tab = GS[k], RG[k]
        rows = [dict(id=g, types=list(tab[g].get("types", [])) if g < len(tab) else [], example=A["ex"].get(g), episodes=A["eps"][g],
                     start_episodes=A["seps"][g], worlds=len(A["wor"][g]), voxels=A["vox"][g], unique_voxels=A["uvox"][g], records=A["rec"][g],
                     **(dict(tgid=tab[g].get("tgid"), mgid=tab[g].get("mgid")) if k == "gid" else {})) for g in sorted(A["eps"])]
        rl = sorted([g for g in rows if g["episodes"] < rare], key=lambda g: (g["episodes"], g["voxels"], g["id"]))
        groups[k] = rows
        rare_by[k] = dict(n=len(rl), of=len(rows), n_one_world=sum(g["worlds"] < 2 for g in rows),
                          hist={"<%d" % c: sum(g["episodes"] < c for g in rows) for c in (2, 3, 5, 10, 20, 50)},
                          hist_worlds={"<%d" % c: sum(g["worlds"] < c for g in rows) for c in (2, 3, 5, 10, 20)}, groups=rl)
    kl = sorted(kinds.values(), key=lambda d: (-d["records"], d["kind"]))
    for d in kl:
        d["effect"] = dict(d["effect"])
    nvis, nuvis = sum(v for c, v in fold_vox.items() if c != "invisible"), sum(v for c, v in fold_uvox.items() if c != "invisible")
    fold = {c: dict(states=len(fold_states[c]), voxels=fold_vox[c], frac_visible_voxels=None if c == "invisible" else round(fold_vox[c] / max(nvis, 1), 5),
                    voxels_unique=fold_uvox[c], records=fold_rec[c]) for c in FOLDS}
    ftl = []
    for t, d in fold_types.items():
        c = d["fold"].most_common(1)[0][0] if d["fold"] else "own"
        if c not in ("own", "invisible"):
            ftl.append(dict(type=t, fold=c, into=d["cap"].most_common(1)[0][0], episodes=d["episodes"], voxels=d["voxels"]))
    ftl.sort(key=lambda d: (-d["voxels"], -d["episodes"], d["type"]))
    capl = sorted([dict(cap=CP.NAMES[c], types=len(cap_types[c]), **{n: len(cap_groups[k].get(c, ())) for k, n, _ in KEYS}) for c in cap_types],
                  key=lambda d: (-d["tgroups"], -d["groups"], d["cap"]))
    wrows = []
    for w, rs in wl.items():
        rs = sorted(rs, key=lambda r: (r.get("t0") or 0, r["eid"]))
        bv, uv = sum(int(np.prod(r["box"])) for r in rs), sum(int(r.get("n_new") or 0) for r in rs)
        wrows.append(dict(world=w, insts=sorted({r["inst"] for r in rs}), episodes=len(rs), box_voxels=bv, unique_voxels=uv,
                          dup_frac=round(1 - uv / max(bv, 1), 4), pairs=sorted(overlaps(rs), key=lambda p: -p["frac_later"])))
    wrows.sort(key=lambda d: (-d["dup_frac"], str(d["world"])))
    allp = sorted([dict(p, world=w["world"]) for w in wrows for p in w["pairs"]], key=lambda p: -p["frac_later"])

    def split(key):
        by = defaultdict(list)
        for e in eps:
            by[e[key] or "?"].append(e)
        return {k: dict(episodes=len(v), types=dist([e["episode"]["types"] for e in v]), **{n: dist([e["episode"][n] for e in v]) for _, n, _ in KEYS},
                        **{c: sum(e["changes"].get(c, 0) for e in v) for c in CHKEYS}) for k, v in sorted(by.items())}
    vis = [s for s in s_eps if not L(s)["inv"]]
    totals = dict(episodes=len(raws), ok=len(eps), errors=len(errs), states=len(s_eps), types=len({L(s)["type"] for s in vis}),
                  types_unknown=len({L(s)["type"] for s in s_eps if not L(s)["known"]}),
                  variants=len({(L(s)["type"], L(s)["vkey"]) for s in vis if L(s)["vkey"] is not None}),
                  **{n: len(GS[k]["eps"]) for k, n, _ in KEYS}, unknown_states=len(unk),
                  start_voxels=int(sum(s_vox.values())), start_voxels_visible=int(nvis),
                  start_voxels_unique=int(sum(s_uvox.values())), start_voxels_unique_visible=int(nuvis),
                  worlds=len(wl), overlap_pairs=len(allp), episodes_overlapping=len({(p["world"], p["b"][0], p["b"][1]) for p in allp}),
                  scan_sec=round(sum(r.get("sec") or 0 for r in raws), 2),
                  registry=dict(types=len(reg.types), groups=len(reg.groups), tgroups=len(reg.tgroups), mgroups=len(RG["mgid"]),
                                variants=sum(len(r["variants"]) for r in reg.types.values())))
    return dict(totals=totals, dist=dict(start={k: dist([e["start"][k] for e in eps]) for k in SETKEYS},
                                        episode={k: dist([e["episode"][k] for e in eps]) for k in SETKEYS},
                                        records={k: dist([e["changes"].get(k, 0) for e in eps]) for k in CHKEYS}),
                changes=dict(with_outside=with_outside, totals={k: tot[k] for k in ("records", "box", "outside") + CATS + ("appear", "appear_code", "appear_same_cap")},
                             effect={k: dict(eff_by_cat[k]) for k in CATS if eff_by_cat[k]}, source=dict(src), n_kinds=len(kl),
                             top=kl[:top], top_prop=[d for d in kl if d["cat"] == "prop"][:top],
                             top_prop_appear=sorted([d for d in kl if d["cat"] == "prop" and any(d["effect"].get(e) for e in APPEAR + CODE)],
                                                    key=lambda d: (-sum(d["effect"].get(e, 0) for e in APPEAR + CODE), d["kind"]))[:top], kinds=kl),
                outside=dict(records=out_tot["records"], episodes=out_eps, distance=wdist(sorted(out_dist.items())), kinds=[[k, n] for k, n in out_kinds.most_common(top)]),
                fold=dict(classes=fold, types=ftl, caps=capl), props=dict(sorted(props.items(), key=lambda kv: -kv[1]["voxels"])),
                groups=groups, rare=rare_by, worlds=dict(worlds=wrows, pairs=allp),
                unknown=sorted(unk.values(), key=lambda u: (-u["episodes"], u["state"])), end_check=dict(end_bad),
                by_episode_type=split("episode_type"), by_world_type=split("world_type"), errors=errs, episodes=eps)


# ---- text summary ---------------------------------------------------------------------------------------------------
def fmt(d):
    return "%g / %g / %g" % (d["p50"], d["p90"], d["max"]) if d.get("n") else "-"


def summary_text(R):
    T, D, C, F, O, W = R["totals"], R["dist"], R["changes"], R["fold"], R["outside"], R["worlds"]
    rg, ct = T["registry"], C["totals"]
    L = ["block stats: %s; registry %s (%s); %d episodes (%d ok, %d errors); scan %.1f s summed over episodes, wall %.1f s"
         % (R["root"], R["registry"]["registry_id"], R["registry"]["version"], T["episodes"], T["ok"], T["errors"], T["scan_sec"], R["wall_sec"]),
         "WORLD-LEVEL: logged box (~65^3) + its block_state records, not per view / clip (that needs re-rendered G-buffers).",
         "distinct over all episodes (invisible types excluded): states %d, types %d / %d (%d not in registry), variants %d; groups seen / in the registry:"
         " texture-multiset (appearance variants) %d / %d, texture-set %d / %d, appearance+geometry %d / %d; unknown / partial / unenumerated states %d"
         % (T["states"], T["types"], rg["types"], T["types_unknown"], T["variants"], T["mgroups"], rg["mgroups"], T["tgroups"], rg["tgroups"],
            T["groups"], rg["groups"], T["unknown_states"]),
         "worlds %d (one persistent world per instance): %d episodes overlap an earlier box of their world (%d pairs); start voxels %d summed over episodes,"
         " %d unique world voxels (%.1f%% duplicate); visible %d / %d unique"
         % (T["worlds"], T["episodes_overlapping"], T["overlap_pairs"], T["start_voxels"], T["start_voxels_unique"],
            100 * (1 - T["start_voxels_unique"] / max(T["start_voxels"], 1)), T["start_voxels_visible"], T["start_voxels_unique_visible"])]
    if W["pairs"]:
        L.append("  largest overlaps (share of the later box): " + ", ".join(
            "%s inst %d ep %d %s -> ep %d %s %.0f%%" % (str(p["world"])[:8], p["b"][0], p["a"][1], p["a"][2], p["b"][1], p["b"][2], 100 * p["frac_later"])
            for p in W["pairs"][:8]))
    L.append("per episode p50 / p90 / max        start box            episode (start + %s records)" % ("all" if C["with_outside"] else "in-box"))
    rows = [("states", "states (all)"), ("types", "types"), ("variants", "variants")] + [(n, lab) for _, n, lab in KEYS] + [("cap_ids", "cap ids (non-air)")] \
        + [("merged_" + n, "merged by cap id: " + lab.split(" ")[0]) for _, n, lab in KEYS] + [("air_" + n, "drawn as air: " + lab.split(" ")[0]) for _, n, lab in KEYS]
    for k, lab in rows:
        L.append("  %-40s %-20s %s" % (lab, fmt(D["start"][k]), fmt(D["episode"][k])))
    L.append("records per episode p50 / p90 / max: all %s; same-type prop %s; appear %s; appear same cap id %s; code-role %s"
             % (fmt(D["records"]["records"]), fmt(D["records"]["prop"]), fmt(D["records"]["appear"]), fmt(D["records"]["appear_same_cap"]), fmt(D["records"]["appear_code"])))
    L.append("block_state records %s %d (box %d, outside %d; sources %s): noop %d, prop %d, place %d, break %d, replace %d, no_prev %d"
             % ("(all)" if C["with_outside"] else "in the box", ct["records"], ct["box"], ct["outside"], C["source"], ct["noop"], ct["prop"], ct["place"],
                ct["break"], ct["replace"], ct["no_prev"]))
    L.append("  appear (texture or geom_only from the jar groups) %d; hand-table role decided (code_appear + code_geom) %d; texture / geom_only /"
             " code_appear with the same cap id %d (lost by the cap-id G-buffer)" % (ct["appear"], ct["appear_code"], ct["appear_same_cap"]))
    L.append("  effect by category %s" % C["effect"])
    if not C["with_outside"]:
        L.append("outside the box (not counted above): %d records in %d episodes, distance to the box p50 / p90 / max %s; top %s"
                 % (O["records"], O["episodes"], fmt(O["distance"]), ", ".join("%s %d" % tuple(kv) for kv in O["kinds"][:6])))
    hdr = "records  episodes  role  texture/geom/code/neutral/unres  kind"
    eff = lambda e: "%d/%d/%d/%d/%d" % (e.get("texture", 0), e.get("geom_only", 0), e.get("code_appear", 0) + e.get("code_geom", 0), e.get("neutral", 0), e.get("unresolved", 0))  # noqa: E731
    L.append("top %d same-type property changes (%d kinds in all): %s" % (len(C["top_prop"]), C["n_kinds"], hdr))
    for d in C["top_prop"]:
        L.append("  %6d %5d  %-7s %-12s %s" % (d["records"], d["episodes"], d["role"] + ("*" if d["role_src"] == "code" else ""), eff(d["effect"]), d["kind"]))
    L.append("top 15 appearance-changing property changes (texture / geom / code-role): %s" % hdr)
    for d in C["top_prop_appear"][:15]:
        L.append("  %6d %5d  %-7s %-12s %s" % (d["records"], d["episodes"], d["role"] + ("*" if d["role_src"] == "code" else ""), eff(d["effect"]), d["kind"]))
    L.append("top 15 type changes: records  episodes  category  kind")
    for d in [d for d in C["kinds"] if d["cat"] != "prop"][:15]:
        L.append("  %6d %5d  %-7s %s" % (d["records"], d["episodes"], d["cat"], d["kind"]))
    L.append("cap_palette fold (distinct states / start voxels (share of visible) / unique world voxels / records as new state): " + "; ".join(
        "%s %d / %d%s / %d / %d" % (c, v["states"], v["voxels"], "" if v["frac_visible_voxels"] is None else " (%.2f%%)" % (100 * v["frac_visible_voxels"]),
                                    v["voxels_unique"], v["records"]) for c, v in F["classes"].items()))
    L.append("  types not kept as themselves (top 20 by start voxels): " + ", ".join(
        "%s->%s %d vox %d ep" % (d["type"], d["into"], d["voxels"], d["episodes"]) for d in F["types"][:20]))
    L.append("  cap ids holding most groups (multiset / set / +geometry, types): " + ", ".join(
        "%s %d/%d/%d, %d" % (d["cap"], d["mgroups"], d["tgroups"], d["groups"], d["types"]) for d in F["caps"][:12]))
    L.append("properties (start voxels, records; roles over the types seen) and top values value:voxels/records:")
    for p, d in list(R["props"].items())[:18]:
        vs = sorted(d["values"].items(), key=lambda kv: -kv[1]["voxels"] - kv[1]["records"])[:6]
        L.append("  %-14s vox %-8d rec %-6d %s | %s" % (p, d["voxels"], d["records"], d["types"], " ".join("%s:%d/%d" % (v, x["voxels"], x["records"]) for v, x in vs)))
    rr = R["rare"]
    L.append("groups seen in < %d episodes (of those seen; in 1 world only; histogram by episodes; by worlds):" % rr["threshold"])
    for k, _, lab in KEYS:
        q = rr[k]
        L.append("  %-32s %d of %d; 1 world %d; %s; worlds %s" % (lab, q["n"], q["of"], q["n_one_world"], q["hist"], q["hist_worlds"]))
    L.append("  e.g. (texture-set) " + ", ".join("%s (%d ep, %d vox, %d rec)" % (str(g["example"]).replace("minecraft:", ""), g["episodes"], g["voxels"], g["records"])
                                                 for g in rr["tgid"]["groups"][:15]))
    L.append("unknown / partial / unenumerated states (%d)%s" % (len(R["unknown"]), ": " + ", ".join(
        "%s [%s, %d ep, %d rec]" % (u["state"], u["why"], u["episodes"], u["records"]) for u in R["unknown"][:15]) if R["unknown"] else ""))
    ec = R["end_check"]
    bad = {k: v for k, v in ec.items() if k not in ("episodes_end_checked", "episodes_no_end_snapshot")}
    L.append("end snapshot check (replay of start + in-box records vs end): %d episodes with an end snapshot, %d without; %s"
             % (ec.get("episodes_end_checked", 0), ec.get("episodes_no_end_snapshot", 0), bad or "all equal"))
    for key, lab in (("by_world_type", "world type"), ("by_episode_type", "episode type")):
        L.append("by %s: episodes  types p50/p90/max  texture-set groups p50/p90/max  records  prop  prop_appear  appear  same_cap  code" % lab)
        for k, d in R[key].items():
            L.append("  %-18s %3d  %-16s %-16s %6d %5d %5d %6d %5d %5d" % (k, d["episodes"], fmt(d["types"]), fmt(d["tgroups"]), d["records"], d["prop"], d["prop_appear"],
                                                                       d["appear"], d["appear_same_cap"], d["appear_code"]))
    if R["errors"]:
        L.append("errors: " + "; ".join("%06d/%03d %s" % (e["eid"], e["inst"], e["error"]) for e in R["errors"][:10]))
    return "\n".join(L)


# ---- driver ---------------------------------------------------------------------------------------------------------
def run(root, reg, workers=1, rare=5, top=30, limit=None, with_outside=False, log=print):
    t0 = time.time()
    eps = worlds([e for e in CR.episodes(root) if e["files"].get("events") or e["files"].get("snap_start")][:limit])
    log("%d episode(s) in %d world(s) under %s" % (len(eps), len({e["world"] for e in eps if e["world"]}), root))
    if workers > 1 and len(eps) > 1:
        ctx = mp.get_context("fork") if sys.platform.startswith("linux") else mp.get_context()
        raws = []
        with ctx.Pool(workers) as pool:
            for i, r in enumerate(pool.imap(scan, eps, chunksize=1)):
                raws.append(r)
                if (i + 1) % 100 == 0:
                    log("  scanned %d / %d (%.0f s)" % (i + 1, len(eps), time.time() - t0))
    else:
        raws = [scan(e) for e in eps]
    R = aggregate(reg, raws, rare=rare, top=top, with_outside=with_outside)
    src = reg.d.get("source") or {}
    R.update(format="block-stats-v2", root=os.path.abspath(root), level="world (logged box + records), not view",
             registry=dict(registry_id=str(reg.d.get("registry_id", ""))[:12], version=src.get("version"), jar_sha1=str(src.get("jar_sha1") or "")[:12]),
             params=dict(rare=rare, top=top, workers=workers, limit=limit, with_outside=with_outside), wall_sec=round(time.time() - t0, 2))
    for e in R["episodes"] + R["errors"]:
        e["base"] = os.path.relpath(e["base"], root)
    return R


def write(R, out):
    os.makedirs(os.path.dirname(os.path.abspath(out)), exist_ok=True)
    with open(out, "w") as fh:
        json.dump(R, fh, indent=1, default=lambda o: o.tolist() if hasattr(o, "tolist") else str(o))
    txt = summary_text(R)
    with open(os.path.splitext(out)[0] + ".txt", "w") as fh:
        fh.write(txt + "\n")
    return txt


# ---- selftest -------------------------------------------------------------------------------------------------------
MINI = {"air": {"": ("-", "")}, "stone": {"": ("cube", "stone")}, "infested_stone": {"": ("cube", "stone")}, "dirt": {"": ("cube", "dirt")},
        "grass_block": {"snowy=false": ("cube", "grass"), "snowy=true": ("cube", "grass_snow")},
        "furnace": {"facing=%s,lit=%s" % (f, l): ("y_" + f, "furnace_" + l) for f in ("north", "east") for l in ("false", "true")},
        "wheat": {"age=%d" % i: ("cross", "wheat%d" % i) for i in range(8)},
        "oak_door": {"facing=north,half=%s,hinge=left,open=%s" % (h, o): ("door_open_" + o, "door_" + h) for h in ("lower", "upper") for o in ("false", "true")},
        "torch": {"": ("torch", "torch")}, "oak_leaves": {"": ("cube", "oak_leaves")}, "seagrass": {"": ("cross", "seagrass")},
        "oak_planks": {"": ("cube", "oak_planks")}, "oak_stairs": {"facing=east,half=bottom,shape=straight": ("stairs", "oak_planks", "oak_planks*12")},
        "water": {"": ("fluid", "water_still")}, "pumpkin_stem": {"age=%d" % i: ("stem%d" % i, "pumpkin_stem") for i in range(8)}}
CODE_MINI = {("water", "level"): ("geom", 16), ("pumpkin_stem", "age"): ("appear", 8)}


def mini_registry():
    """A variants-only registry in mc_registry's layout: gid = equal (geometry, texture) tags, tgid = equal texture tag, mgid
    = equal multiset tag (the texture tag unless a third entry gives one); water level and stem age carry hand-table roles
    (src code), waterlogged comes from mc_registry.ANYPROP, like the real registry."""
    types, groups, tgroups, mgroups, gix, tix, mix = {}, [], [], [], {}, {}, {}
    for t, vs in MINI.items():
        rec = {"tid": len(types), "kind": "variants", "render": "invisible" if t == "air" else "fluid" if t == "water" else "model", "extra_tex": [], "variants": {}}
        for k, (geo, tex, *ms) in vs.items():
            ms = ms[0] if ms else tex
            if tex not in tix:
                tix[tex] = len(tgroups); tgroups.append({"tgid": len(tgroups), "thash": "t:" + tex, "tex": [tex] if tex else [], "types": []})
            if ms not in mix:
                mix[ms] = len(mgroups); mgroups.append({"mgid": len(mgroups), "mshash": "m:" + ms, "tex": [ms] if ms else [], "types": []})
            if (geo, tex) not in gix:
                gix[(geo, tex)] = len(groups); groups.append({"gid": len(groups), "ahash": "a:%s:%s" % (geo, tex), "thash": "t:" + tex, "tgid": tix[tex],
                                                             "mshash": "m:" + ms, "mgid": mix[ms], "types": []})
            g = groups[gix[(geo, tex)]]
            for x in (g, tgroups[tix[tex]], mgroups[mix[ms]]):
                if t not in x["types"]:
                    x["types"].append(t)
            rec["variants"][k] = [g["gid"], tix[tex]]
        rows = [(MR.parse_key(k), g, tg, groups[g]["mgid"]) for k, (g, tg) in rec["variants"].items()]
        rec["props"] = {p: dict(v, role_jar=v["role"], src="jar") for p, v in MR.flip_roles(rows).items()}
        for (ct, p), (role, n) in CODE_MINI.items():
            if ct == t:
                rec["props"][p] = {"values": [str(i) for i in range(n)], "role": role, "role_ms": role, "role_jar": rec["props"].get(p, {}).get("role", "absent"), "src": "code"}
        rec["ngroups"], rec["ntgroups"], rec["nmgroups"] = (len({g for g, _ in rec["variants"].values()}), len({tg for _, tg in rec["variants"].values()}),
                                                            len({groups[g]["mgid"] for g, _ in rec["variants"].values()}))
        types[t] = rec
    return MR.Registry({"format": "mini", "registry_id": "mini-registry", "source": {"version": "test"}, "models": {}, "types": types,
                        "groups": groups, "tgroups": tgroups, "mgroups": mgroups, "textures": {}})


def _write_episode(d, stem, T0, start, recs, end, etype, world="test-world-0", end_size=None):
    origin, size = (100, 60, 200), (4, 3, 4)
    for which, st, sz in (("start", start, size), ("end", end, end_size or size)):
        snap = CR._encode({tuple(a - o for a, o in zip(p, origin)): s for p, s in st.items()}, origin, sz, extra=dict(which=which, stem=stem))
        with gzip.open(os.path.join(d, "%s_world_snapshot_%s.json.gz" % (stem, which)), "wt") as fh:
            json.dump(snap, fh)
    ev = [dict(type="episode_begin", seq=0, epoch_ms=T0, episode_type=etype, box=dict(min=list(origin), max=[o + n - 1 for o, n in zip(origin, size)]),
               snapshot=dict(mode="blocks"), world=dict(uid=world, seed=1))]
    for i, (p, prev, new, so) in enumerate(recs):
        ev.append(dict(type="block_state", seq=i + 1, epoch_ms=T0 + 50 * (i + 1), pos=list(p), block=new.split("[")[0], state=new, prev_state=prev, source=so))
    ev.append(dict(type="episode_end", seq=len(ev), epoch_ms=T0 + 50 * (len(ev) + 1)))
    with open(os.path.join(d, stem + "_world_events.jsonl"), "w") as fh:
        fh.write("\n".join(json.dumps(e) for e in ev) + "\n")
    eid, inst = stem.split("_")[2], stem.split("_")[-1]
    with open(os.path.join(d, "%s_Alpha_instance_%s_episode_info.json" % ("_".join(stem.split("_")[:3]), inst)), "w") as fh:
        json.dump(dict(bot_name="Alpha", episode_type=etype, world_type="flat" if eid.endswith("1") else "normal"), fh)


def selftest(reg_path=None):
    reg = MR.Registry.load(reg_path) if reg_path else mini_registry()
    ok = [0, 0]

    def chk(name, cond):
        ok[bool(cond)] += 1; print("%s  %s" % ("PASS" if cond else "FAIL", name))
    m = "minecraft:"
    AIR, STONE, GRASS = m + "air", m + "stone", m + "grass_block[snowy=false]"
    FURN0, FURN1, W0, W1 = m + "furnace[facing=north,lit=false]", m + "furnace[facing=north,lit=true]", m + "wheat[age=0]", m + "wheat[age=1]"
    door = lambda h, o, pw="false": m + "oak_door[facing=north,half=%s,hinge=left,open=%s,powered=%s]" % (h, o, pw)   # noqa: E731
    LEAF1, LEAF2 = m + "oak_leaves[distance=1,persistent=false,waterlogged=false]", m + "oak_leaves[distance=2,persistent=false,waterlogged=false]"
    start = {}
    for x in range(100, 104):
        for z in range(200, 204):
            start[(x, 60, z)], start[(x, 61, z)], start[(x, 62, z)] = STONE, GRASS, AIR
    start.update({(100, 61, 200): FURN0, (101, 61, 200): W0, (102, 61, 200): door("lower", "false"), (102, 62, 200): door("upper", "false"),
                  (103, 61, 200): m + "oak_stairs[facing=east,half=bottom,shape=straight,waterlogged=false]", (100, 61, 201): m + "torch",
                  (101, 61, 201): LEAF1, (102, 61, 201): m + "seagrass", (103, 61, 201): m + "water[level=0]"})
    recs = [((100, 61, 200), FURN0, FURN1, "readback"), ((101, 61, 200), W0, W1, "readback"),
            ((102, 61, 200), door("lower", "false"), door("lower", "true"), "readback"), ((102, 62, 200), door("upper", "false"), door("upper", "true"), "readback"),
            ((102, 61, 200), door("lower", "true"), door("lower", "true", "true"), "readback"), ((101, 61, 201), LEAF1, LEAF2, "box_diff"),
            ((103, 61, 203), GRASS, AIR, "readback"), ((103, 62, 203), AIR, m + "dirt", "readback"), ((100, 60, 200), STONE, m + "infested_stone", "box_diff"),
            ((101, 62, 202), AIR, m + "not_a_block", "readback"), ((100, 61, 203), GRASS, GRASS, "readback"),
            ((103, 61, 201), m + "water[level=0]", m + "water[level=1]", "readback"), ((110, 61, 200), AIR, STONE, "box_diff")]
    end = dict(start)
    for p, _, s, _ in recs:
        if p in end:
            end[p] = s
    tmp = tempfile.mkdtemp(prefix="w1bs_test_")
    try:
        d = os.path.join(tmp, "batch_0", "output"); os.makedirs(d)
        _write_episode(d, "20261024_120000_000000_world_instance_000", 1_792_800_000_000, start, recs, end, "lightAndFire")
        _write_episode(d, "20261024_120100_000001_world_instance_000", 1_792_800_100_000, start, [], start, "mine", end_size=(4, 2, 4))
        rp = reg_path
        if not rp:
            rp = os.path.join(tmp, "mini_reg.json"); reg.save(rp)
        q = dict(rare=2, top=30, log=lambda *a: None)
        R1, R2, R3 = run(tmp, reg, workers=1, **q), run(tmp, MR.Registry.load(rp), workers=2, **q), run(tmp, reg, with_outside=True, **q)
        strip = lambda R: json.dumps({k: v for k, v in R.items() if k not in ("wall_sec", "params", "totals", "episodes")} | {"ep": [{k: v for k, v in e.items() if k != "sec"}  # noqa: E731
                                                                                                                    for e in R["episodes"]]}, sort_keys=True, default=str)
        chk("two episodes scanned, no errors", R1["totals"]["ok"] == 2 and not R1["errors"])
        chk("workers=2 (registry reloaded from json) gives the same result as workers=1", strip(R1) == strip(R2))
        A, B = R1["episodes"]
        chk("episode A start: 12 states, 10 types, 11 groups (%s)" % A["start"], (A["start"]["states"], A["start"]["types"], A["start"]["groups"]) == (12, 10, 11))
        chk("start merged 3 for every key (door lower / upper + stairs on oak_planks, seagrass + water on water); wheat + torch drawn as air",
            all(A["start"]["merged_" + n] == 3 and A["start"]["air_" + n] == 2 for _, n, _ in KEYS))
        chk("episode A over the episode: 22 states, 13 types, 16 / 14 / 14 groups (+geometry / set / multiset), 1 unknown (%s)" % A["episode"],
            (A["episode"]["states"], A["episode"]["types"], A["episode"]["groups"], A["episode"]["tgroups"], A["episode"]["mgroups"], A["episode"]["unknown"])
            == (22, 13, 16, 14, 14, 1))
        chk("episode B (no records) episode set = start set; world types from episode_info", B["episode"] == B["start"] == A["start"]
            and (A["world_type"], B["world_type"]) == ("normal", "flat"))
        ct = R1["changes"]["totals"]
        chk("in-box records 12: noop 1, prop 7, place 2, break 1, replace 1; outside 1 tallied apart (%s)" % ct,
            (ct["records"], ct["box"], ct["outside"], ct["noop"], ct["prop"], ct["place"], ct["break"], ct["replace"]) == (12, 12, 0, 1, 7, 2, 1, 1)
            and R1["outside"]["records"] == 1 and R1["outside"]["distance"]["max"] == 7 and R1["outside"]["kinds"] == [["air -> stone", 1]])
        ef = R1["changes"]["effect"]
        chk("prop effects: texture 2 (furnace lit, wheat age), geom_only 2 (door open), code_geom 1 (water level), neutral 2 (door powered, leaves distance)",
            ef.get("prop") == {"texture": 2, "geom_only": 2, "code_geom": 1, "neutral": 2})
        chk("appear 6, appear_code 1, same cap id 4 (furnace, wheat, 2 doors)", (ct["appear"], ct["appear_code"], ct["appear_same_cap"]) == (6, 1, 4))
        LK, st0 = Look(reg), m + "oak_stairs[facing=east,half=bottom,shape=straight,waterlogged=%s]"
        cs, cw = classify(LK, m + "pumpkin_stem[age=0]", m + "pumpkin_stem[age=1]"), classify(LK, st0 % "false", st0 % "true")
        chk("ranked effects: stem age (gid changes, code role appear) -> code_appear, same cap counted; waterlogged on stairs (ANYPROP) -> code_appear",
            cs[:2] == ("prop", "code_appear") and cs[3] and cw[:2] == ("prop", "code_appear") and LK.prop("oak_stairs", "waterlogged").get("src") == "code")
        K = {d["kind"]: d for d in R1["changes"]["kinds"]}
        chk("kind furnace lit false->true: texture, role appear", K["furnace lit false->true"]["effect"] == {"texture": 1} and K["furnace lit false->true"]["role"] == "appear")
        chk("kind oak_door open false->true: 2 records geom_only, role geom", K["oak_door open false->true"]["effect"] == {"geom_only": 2} and K["oak_door open false->true"]["role"] == "geom")
        chk("door powered neutral (absent), wheat age texture, leaves distance neutral, water level code_geom (src code)",
            K["oak_door powered false->true"]["effect"] == {"neutral": 1} and K["oak_door powered false->true"]["role"] == "absent"
            and K["wheat age 0->1"]["effect"] == {"texture": 1} and K["oak_leaves distance 1->2"]["effect"] == {"neutral": 1}
            and K["water level 0->1"]["effect"] == {"code_geom": 1} and K["water level 0->1"]["role_src"] == "code")
        chk("type kinds: stone -> infested_stone neutral, air -> not_a_block unresolved, grass_block -> air break",
            K["stone -> infested_stone"]["effect"] == {"neutral": 1} and K["air -> not_a_block"]["effect"] == {"unresolved": 1} and K["grass_block -> air"]["cat"] == "break")
        FT, FC = {d["type"]: d for d in R1["fold"]["types"]}, R1["fold"]["classes"]
        chk("fold: door / stairs / infested_stone folded into their caps, wheat / torch to_air, seagrass to_fluid, not_a_block cap_unknown; furnace, water own",
            FT["oak_door"]["fold"] == FT["oak_stairs"]["fold"] == FT["infested_stone"]["fold"] == "folded" and FT["oak_door"]["into"] == "oak_planks"
            and FT["infested_stone"]["into"] == "stone" and FT["wheat"]["fold"] == FT["torch"]["fold"] == "to_air" and FT["seagrass"]["fold"] == "to_fluid"
            and FT["not_a_block"]["fold"] == "cap_unknown" and "furnace" not in FT and "water" not in FT)
        chk("fold voxels: to_air 4 (wheat + torch, 2 episodes), invisible 2 x 15 air, visible voxels 66; unique world voxels half of each",
            FC["to_air"]["voxels"] == 4 and FC["invisible"]["voxels"] == 30 and R1["totals"]["start_voxels_visible"] == 66
            and (FC["to_air"]["voxels_unique"], FC["invisible"]["voxels_unique"], R1["totals"]["start_voxels_unique_visible"]) == (2, 15, 33))
        TT, WW = R1["totals"], R1["worlds"]
        chk("one world, B's box = A's box: 1 overlap pair (share 1.0), B adds no unique voxel, 96 -> 48 start voxels",
            (TT["worlds"], TT["overlap_pairs"], TT["episodes_overlapping"], TT["start_voxels"], TT["start_voxels_unique"]) == (1, 1, 1, 96, 48)
            and WW["pairs"][0]["frac_later"] == 1.0 and (A["new_voxels"], B["new_voxels"]) == (48, 0) and WW["worlds"][0]["dup_frac"] == 0.5)
        chk("unknown states: not_a_block (type, 1 episode, 1 record)", [(u["state"], u["why"], u["episodes"], u["records"]) for u in R1["unknown"]] == [(m + "not_a_block", "type", 1, 1)])
        rr = R1["rare"]
        chk("rare (< 2 episodes) by gid: the 5 groups only episode A reaches (furnace lit, wheat 1, 2 open doors, dirt)", rr["gid"]["n"] == 5
            and sorted(g["example"].split("[")[0] for g in rr["gid"]["groups"]) == sorted([m + "furnace", m + "wheat", m + "oak_door", m + "oak_door", m + "dirt"]))
        chk("rare by tgid / mgid: 3 (open doors keep the closed door's textures); every group in 1 world",
            rr["tgid"]["n"] == rr["mgid"]["n"] == 3 and all(rr[k]["n_one_world"] == rr[k]["of"] for k, _, _ in KEYS)
            and sorted(g["example"].split("[")[0] for g in rr["tgid"]["groups"]) == sorted([m + "furnace", m + "wheat", m + "dirt"]))
        chk("property values: lit false 2 voxels, lit true 1 record; distance 2 1 record", R1["props"]["lit"]["values"]["false"]["voxels"] == 2
            and R1["props"]["lit"]["values"]["true"]["records"] == 1 and R1["props"]["distance"]["values"]["2"]["records"] == 1)
        chk("per-episode distribution of episode types: p50 11.5, max 13", R1["dist"]["episode"]["types"]["p50"] == 11.5 and R1["dist"]["episode"]["types"]["max"] == 13)
        chk("end check: A equals the replay, B's end box differs -> end_error, not a pass (%s)" % R1["end_check"],
            R1["end_check"] == {"episodes_end_checked": 2, "episodes_end_error": 1} and B["end_error"] and not A["end_error"])
        chk("by episode type: lightAndFire 12 records, prop_appear 4; by world type flat / normal", R1["by_episode_type"]["lightAndFire"]["records"] == 12
            and R1["by_episode_type"]["lightAndFire"]["prop_appear"] == 4 and sorted(R1["by_world_type"]) == ["flat", "normal"])
        c3 = R3["changes"]["totals"]
        chk("--with-outside: 13 records, place 3, appear 7", (c3["records"], c3["outside"], c3["place"], c3["appear"]) == (13, 1, 3, 7))
        txt = write(R1, os.path.join(tmp, "stats.json"))
        chk("text summary written and says world-level", "WORLD-LEVEL" in txt and os.path.exists(os.path.join(tmp, "stats.txt")))
        print(txt)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    print("selftest (%s): %d passed, %d failed" % ("registry " + reg_path if reg_path else "mini registry", ok[1], ok[0]))
    return ok[0] == 0


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--root"); ap.add_argument("--reg"); ap.add_argument("--out")
    ap.add_argument("--rare", type=int, default=5); ap.add_argument("--top", type=int, default=30)
    ap.add_argument("--workers", type=int, default=1); ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--with-outside", action="store_true"); ap.add_argument("--test", action="store_true")
    a = ap.parse_args()
    if a.test:
        sys.exit(0 if selftest(a.reg) else 1)
    if not (a.root and a.reg and a.out):
        ap.error("--root, --reg and --out (or --test)")
    reg = MR.Registry.load(a.reg)
    R = run(a.root, reg, workers=a.workers, rare=a.rare, top=a.top, limit=a.limit, with_outside=a.with_outside)
    print(write(R, a.out))
    print("wrote %s and %s" % (a.out, os.path.splitext(a.out)[0] + ".txt"))


if __name__ == "__main__":
    main()
