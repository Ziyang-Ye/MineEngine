"""Minecraft-like block world written as code: an editable block world shared by N players in two teams.
State = a dense block grid + players + primed TNT. Rules (Minecraft numbers where they matter for the look):
  walking 4.3 m/s, gravity 32 m/s^2, jump 9 m/s (about 1.25 m), player box 0.6 x 1.8 m, eye 1.62 m;
  reach 4.5 m; attack (Action.fire): hit the first player on the crosshair ray within 3 m before any block (6 damage of
  20 hp, 0.6 s cooldown, knockback), otherwise break the first block on the ray (0.25 s cooldown; bedrock unbreakable);
  use (Action.extra["use"]): place Action.extra["block"] on the face the ray hits (not inside a player), or ignite TNT;
  TNT explodes 4 s after ignition: blocks within 3 m removed (not bedrock), players within 5 m damaged by distance;
  death -> respawn 2 s later at the team's spawn.
Agents without an action follow the scene's bots: "builder" builds its team's blueprint (house / tower / wall) block by
block by walking up to it and aiming; "fighter" chases the nearest enemy and attacks; "raider" walks to the enemy
structure and breaks it, fighting back when hit. World edits (scripted or by hand): edit(op) with ops box, clear,
structure, tnt, set_bot, heal (see EDIT_OPS); every edit becomes an event.
Axes are Minecraft's (x east, y up, z south; 1 block = 1 m); yaw / pitch follow Mineflayer (radians: yaw 0 = north / -z,
positive yaw turns west; pitch positive = up). Views come from voxel_proxy.py (render_views).
"""
import math
import os
import sys

import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
from mineengine.core import Action, World  # noqa: E402
from mineengine import voxel_proxy as VP  # noqa: E402

AIR, GRASS, STONE, DIRT, BEDROCK, PLANKS, COBBLE, GLASS, LOG, TNT, RED_WOOL, BLUE_WOOL = range(12)
BLOCK_NAMES = ["air", "grass_block", "stone", "dirt", "bedrock", "oak_planks", "cobblestone", "glass", "oak_log", "tnt",
               "red_wool", "blue_wool"]
BLOCK_RGB = np.array([[0, 0, 0], [106, 170, 64], [125, 125, 125], [134, 96, 67], [60, 60, 60], [162, 130, 78], [110, 110, 110],
                      [190, 220, 230], [102, 81, 50], [200, 50, 40], [180, 40, 40], [50, 70, 180]], np.uint8)
TEAM_RGB = {0: (235, 140, 40), 1: (60, 125, 235)}
TEAM_WOOL = {0: RED_WOOL, 1: BLUE_WOOL}
W_P, H_P, EYE = 0.6, 1.8, 1.62
WALK, GRAV, JUMP_V = 4.3, 32.0, 9.0
REACH, MELEE, DMG, HP = 4.5, 3.0, 6.0, 20.0
EDIT_OPS = ("box", "clear", "structure", "tnt", "set_bot", "heal")


def blueprint(kind, at, material=PLANKS, size=5, height=3, axis="x"):
    """Cells [((x, y, z), block)] of a structure whose footprint corner is `at` (x, y = floor level, z), bottom-up order.
    house: size x size walls (door on the north side, glass windows) + log roof; tower: hollow 3 x 3 column; wall: one block
    thick along `axis`; bridge: 2 wide along `axis` at level y; platform: size x size slab at level y."""
    x0, y0, z0 = (int(v) for v in at)
    cells = []
    if kind == "house":
        for y in range(y0, y0 + height):
            for x in range(x0, x0 + size):
                for z in range(z0, z0 + size):
                    edge = x in (x0, x0 + size - 1) or z in (z0, z0 + size - 1)
                    door = x == x0 + size // 2 and z == z0 and y < y0 + 2
                    window = y == y0 + 1 and z == z0 + size // 2 and x in (x0, x0 + size - 1)
                    if edge and not door:
                        cells.append(((x, y, z), GLASS if window else material))
        for x in range(x0, x0 + size):
            for z in range(z0, z0 + size):
                cells.append(((x, y0 + height, z), LOG))
    elif kind == "tower":
        for y in range(y0, y0 + height):
            for x in range(x0, x0 + 3):
                for z in range(z0, z0 + 3):
                    if (x, z) != (x0 + 1, z0 + 1):
                        cells.append(((x, y, z), material))
    elif kind == "wall":
        for y in range(y0, y0 + height):
            for s in range(size):
                cells.append(((x0 + s, y, z0) if axis == "x" else (x0, y, z0 + s), material))
    elif kind == "bridge":
        for s in range(size):
            for w in (0, 1):
                cells.append(((x0 + s, y0, z0 + w) if axis == "x" else (x0 + w, y0, z0 + s), material))
    elif kind == "platform":
        for x in range(x0, x0 + size):
            for z in range(z0, z0 + size):
                cells.append(((x, y0, z), material))
    else:
        raise ValueError(kind)
    return cells


class MC(World):
    tick_hz, render_hz = 20, 10
    name = "mc"

    def __init__(self, n_agents=4, half=24, ground_y=-61, seed=0, bots=None, prompt=None):
        self.n, self.half, self.gy = int(n_agents), int(half), int(ground_y)
        self._prompt, self._bots = prompt, bots
        self.reset(seed)

    # ---- state ---------------------------------------------------------------------------------------------------
    def reset(self, seed=0):
        self.rng = np.random.default_rng(seed)
        h = self.half
        self.origin = np.array([-h, self.gy - 3, -h])                           # grid covers 3 layers below the surface
        self.grid = VP.flat_world(tuple(self.origin), (h, self.gy + 29, h), surface_id=GRASS, stone_id=STONE, dirt_id=DIRT,
                                  bedrock_id=BEDROCK, ground_y=self.gy)
        self.grid[:, 0, :] = BEDROCK                                            # the box floor is unbreakable
        self.t, self.tnt, self._mesh, self._next_tnt = 0, [], None, 1000
        self.sites = {0: (-h // 2 - 2, self.gy + 1, -2), 1: (h // 2 - 3, self.gy + 1, -2)}   # team structure corners
        self.spawn_pt = {0: np.array([-h + 4.5, self.gy + 1.0, 0.5]), 1: np.array([h - 4.5, self.gy + 1.0, 0.5])}
        roles = self._bots or ["builder", "builder", "fighter", "raider", "builder", "fighter", "raider", "fighter"]
        self.ag, self.plans = [], {}
        for i in range(self.n):
            team = i % 2
            a = {"id": i, "team": team, "bot": roles[i % len(roles)], "plan": [], "stuck": 0}
            self.ag.append(a); self._spawn(a)
        for team in (0, 1):
            kind = "house" if team == 0 else "tower"
            self.plans[team] = blueprint(kind, self.sites[team], material=PLANKS if team == 0 else COBBLE, height=3 if team == 0 else 5)

    def _spawn(self, a):
        p = self.spawn_pt[a["team"]] + [self.rng.uniform(-2, 2), 0, self.rng.uniform(-3, 3)]
        p[1] = self._surface(p[0], p[2])
        a.update(pos=p, vel=np.zeros(3), yaw=math.pi / 2 if a["team"] == 0 else -math.pi / 2, pitch=0.0, hp=HP, alive=True,
                 cool=0.0, dead_t=0.0, on_ground=True, last_hit_by=None)

    def _surface(self, x, z):
        """Feet height standing on the highest solid block of column (x, z)."""
        i, k = int(math.floor(x)) - self.origin[0], int(math.floor(z)) - self.origin[2]
        col = np.nonzero(self.grid[i, :, k])[0] if 0 <= i < self.grid.shape[0] and 0 <= k < self.grid.shape[2] else []
        return float(self.origin[1] + (col.max() + 1 if len(col) else 0))

    def block(self, c):
        i, j, k = c[0] - self.origin[0], c[1] - self.origin[1], c[2] - self.origin[2]
        if 0 <= i < self.grid.shape[0] and 0 <= j < self.grid.shape[1] and 0 <= k < self.grid.shape[2]:
            return int(self.grid[i, j, k])
        return BEDROCK if j < 0 else AIR

    def _set(self, c, v):
        i, j, k = c[0] - self.origin[0], c[1] - self.origin[1], c[2] - self.origin[2]
        if 0 <= i < self.grid.shape[0] and 0 < j < self.grid.shape[1] and 0 <= k < self.grid.shape[2]:
            if self.grid[i, j, k] != v:
                self.grid[i, j, k] = v; self._mesh = None
                return True
        return False

    # ---- geometry --------------------------------------------------------------------------------------------------
    def _box_hits(self, lo, hi):
        """Any solid block intersecting the box [lo, hi) (world metres)?"""
        a = np.floor(np.asarray(lo) + 1e-6).astype(int); b = np.floor(np.asarray(hi) - 1e-6).astype(int)
        for x in range(a[0], b[0] + 1):
            for y in range(a[1], b[1] + 1):
                for z in range(a[2], b[2] + 1):
                    if self.block((x, y, z)) != AIR:
                        return True
        return False

    def _player_box(self, p):
        return p - [W_P / 2, 0, W_P / 2], p + [W_P / 2, H_P, W_P / 2]

    def eye(self, a):
        return a["pos"] + [0, EYE, 0]

    @staticmethod
    def forward(yaw, pitch):
        return np.array([-math.sin(yaw) * math.cos(pitch), math.sin(pitch), -math.cos(yaw) * math.cos(pitch)])

    def raycast(self, o, d, maxd=REACH):
        """Voxel DDA: first solid cell on the ray within maxd -> (cell, face normal, distance) or (None, None, inf)."""
        c = np.floor(o).astype(int); step = np.sign(d).astype(int)
        with np.errstate(divide="ignore"):
            tdel = np.where(d != 0, np.abs(1.0 / d), np.inf)
            tmax = np.where(d > 0, (c + 1 - o) / d, np.where(d < 0, (c - o) / d, np.inf))
        n, t = np.zeros(3, int), 0.0
        while t <= maxd:
            if self.block(tuple(c)) != AIR:
                return tuple(int(v) for v in c), tuple(int(v) for v in n), t
            ax = int(np.argmin(tmax)); t = float(tmax[ax]); c[ax] += step[ax]; tmax[ax] += tdel[ax]
            n = np.zeros(3, int); n[ax] = -step[ax]
        return None, None, np.inf

    def _ray_player(self, o, d, maxd, skip):
        """Nearest player whose box the ray enters within maxd (slab test) -> (agent, t)."""
        best = (None, maxd)
        for b in self.ag:
            if b is skip or not b["alive"]:
                continue
            lo, hi = self._player_box(b["pos"])
            with np.errstate(divide="ignore", invalid="ignore"):
                t1, t2 = (lo - o) / d, (hi - o) / d
            tn, tf = np.nanmax(np.minimum(t1, t2)), np.nanmin(np.maximum(t1, t2))
            if tf >= max(tn, 0) and max(tn, 0) < best[1]:
                best = (b, max(tn, 0.0))
        return best

    # ---- rules -----------------------------------------------------------------------------------------------------
    def _move(self, a, wish, jump, dt):
        if jump and a["on_ground"]:
            a["vel"][1] = JUMP_V
        a["vel"][1] -= GRAV * dt
        p = a["pos"]
        dy = a["vel"][1] * dt
        lo, hi = self._player_box(p + [0, dy, 0])
        if self._box_hits(lo, hi):
            if dy < 0:                                                           # land on the top face of the block below
                y = math.floor(p[1] + dy) + 1.0
                if y <= p[1] + 1e-6 and not self._box_hits(*self._player_box(np.array([p[0], y, p[2]]))):
                    p[1] = y
                a["on_ground"] = True
            a["vel"][1] = 0.0
        else:
            p[1] += dy; a["on_ground"] = False
        blocked = False
        for ax in (0, 2):
            d = wish[ax] * dt
            if d:
                q = p.copy(); q[ax] += d
                if self._box_hits(*self._player_box(q)) or abs(q[ax]) > self.half - 0.4:
                    blocked = True
                else:
                    p[ax] = q[ax]
        return blocked

    def _attack(self, a, ev):
        o, f = self.eye(a), self.forward(a["yaw"], a["pitch"])
        cell, n, tb = self.raycast(o, f)
        b, tp = self._ray_player(o, f, min(MELEE, tb), a)
        if b is not None:
            a["cool"] = 0.6
            if b["team"] == a["team"]:
                return
            b["hp"] -= DMG; b["last_hit_by"] = a["id"]
            kb = b["pos"] - a["pos"]; kb[1] = 0; kb /= np.linalg.norm(kb) + 1e-9
            b["vel"][1] = 5.0; b["kb"] = kb * 6.0
            ev.append({"t": self.t, "type": "hit", "by": a["id"], "victim": b["id"], "hp": round(b["hp"], 1)})
            if b["hp"] <= 0:
                b["alive"], b["dead_t"] = False, self.t / self.tick_hz
                ev.append({"t": self.t, "type": "kill", "by": a["id"], "victim": b["id"]})
        elif cell is not None and self.block(cell) != BEDROCK:
            a["cool"] = 0.25
            old = self.block(cell)
            if old == TNT:
                self._ignite(cell, ev, a["id"])
            elif self._set(cell, AIR):
                ev.append({"t": self.t, "type": "break", "by": a["id"], "cell": cell, "block": BLOCK_NAMES[old]})

    def _occupied(self, c):
        for b in self.ag:
            if b["alive"]:
                lo, hi = self._player_box(b["pos"])
                if (lo < np.asarray(c) + 1).all() and (hi > np.asarray(c)).all():
                    return True
        return False

    def _has_neighbour(self, c):
        return any(self.block((c[0] + dx, c[1] + dy, c[2] + dz)) != AIR
                   for dx, dy, dz in ((1, 0, 0), (-1, 0, 0), (0, 1, 0), (0, -1, 0), (0, 0, 1), (0, 0, -1)))

    def _place(self, a, blk, ev, at=None):
        if at is None:
            o, f = self.eye(a), self.forward(a["yaw"], a["pitch"])
            cell, n, _ = self.raycast(o, f)
            if cell is None:
                return False
            if self.block(cell) == TNT and blk != TNT:
                self._ignite(cell, ev, a["id"]); a["cool"] = 0.25
                return True
            at = tuple(cell[i] + n[i] for i in range(3))
        else:
            if np.linalg.norm(np.asarray(at) + 0.5 - self.eye(a)) > REACH + 0.5:
                return False
            if not self._has_neighbour(at):
                return False                                                     # needs a neighbour to place against
        if self.block(at) != AIR or self._occupied(at):
            return False
        self._set(at, blk); a["cool"] = 0.25
        ev.append({"t": self.t, "type": "place", "by": a["id"], "cell": tuple(int(v) for v in at), "block": BLOCK_NAMES[blk]})
        return True

    def _ignite(self, cell, ev, by):
        self._set(cell, AIR)
        self.tnt.append({"id": self._next_tnt, "pos": np.asarray(cell, float) + [0.5, 0, 0.5], "fuse": 4.0, "by": by})
        self._next_tnt += 1
        ev.append({"t": self.t, "type": "ignite", "by": by, "cell": tuple(cell)})

    def _explode(self, x, ev, by=-1, r=3.0):
        c = np.floor(x).astype(int); removed = 0; chain = []
        R = int(math.ceil(r))
        for dx in range(-R, R + 1):
            for dy in range(-R, R + 1):
                for dz in range(-R, R + 1):
                    q = (c[0] + dx, c[1] + dy, c[2] + dz)
                    if dx * dx + dy * dy + dz * dz <= r * r and self.block(q) not in (AIR, BEDROCK):
                        if self.block(q) == TNT:
                            chain.append(q)
                        removed += self._set(q, AIR)
        ev.append({"t": self.t, "type": "explode", "by": by, "at": [round(float(v), 2) for v in x], "removed": int(removed)})
        for q in chain:
            self.tnt.append({"id": self._next_tnt, "pos": np.asarray(q, float) + [0.5, 0, 0.5], "fuse": 0.5 + self.rng.uniform(0, 0.5), "by": by})
            self._next_tnt += 1
        for b in self.ag:
            dist = np.linalg.norm(b["pos"] + [0, 0.9, 0] - x)
            if b["alive"] and dist < 5.0:
                b["hp"] -= 16.0 * (1 - dist / 5.0); b["vel"][1] = 7.0
                ev.append({"t": self.t, "type": "hit", "by": by, "victim": b["id"], "hp": round(b["hp"], 1), "cause": "explosion"})
                if b["hp"] <= 0:
                    b["alive"], b["dead_t"] = False, self.t / self.tick_hz
                    ev.append({"t": self.t, "type": "kill", "by": by, "victim": b["id"], "cause": "explosion"})

    # ---- bots ------------------------------------------------------------------------------------------------------
    def _aim(self, a, target):
        v = np.asarray(target, float) - self.eye(a)
        yaw = math.atan2(-v[0], -v[2]); pitch = math.atan2(v[1], math.hypot(v[0], v[2]))
        return yaw, pitch

    def _free(self, x, z, y):
        """Standable column cell: feet and head air, solid ground below, inside the arena."""
        return (self.block((x, y, z)) == AIR and self.block((x, y + 1, z)) == AIR and self.block((x, y - 1, z)) != AIR
                and abs(x + 0.5) < self.half - 0.5 and abs(z + 0.5) < self.half - 0.5)

    def _clear_line(self, p, q, y):
        n = int(np.hypot(*(q - p)[[0, 2]]) * 3) + 1
        return all(self._free(int(math.floor(p[0] + (q[0] - p[0]) * s)), int(math.floor(p[2] + (q[2] - p[2]) * s)), y) for s in np.linspace(0, 1, n))

    N8 = ((1, 0), (-1, 0), (0, 1), (0, -1), (1, 1), (1, -1), (-1, 1), (-1, -1))

    def _bfs3(self, start, goals, limit=4000):
        """BFS over standable cells (x, y, z): walk on the level, step up 1 (jump, needs head room), drop up to 3;
        diagonals only on the level without corner cutting. goals = set of (x, z) columns -> path [(x, y, z)] or None."""
        from collections import deque
        prev, q = {start: None}, deque([start])
        while q:
            c = q.popleft()
            if (c[0], c[2]) in goals:
                path = []
                while c is not None:
                    path.append(c); c = prev[c]
                return path[::-1]
            x, y, z = c
            for dx, dz in self.N8:
                for dy in (0, 1, -1, -2, -3):
                    nb = (x + dx, y + dy, z + dz)
                    if not self._free(nb[0], nb[2], nb[1]):
                        continue
                    if dy == 1 and self.block((x, y + 2, z)) != AIR:
                        continue
                    if dy < 0 and any(self.block((nb[0], yy, nb[2])) != AIR for yy in range(nb[1] + 2, y + 2)):
                        continue
                    if dx and dz and (dy != 0 or not (self._free(x + dx, z, y) and self._free(x, z + dz, y))):
                        break
                    if nb not in prev:
                        prev[nb] = c; q.append(nb)
                    break
            if len(prev) > limit:
                return None
        return None

    def _goal_columns(self, gc, y):
        def ok(col):
            return any(self._free(col[0], col[1], yy) for yy in range(y - 4, y + 5))
        if ok(gc):
            return {gc}
        for r in (1, 2):
            ring = {(gc[0] + dx, gc[1] + dz) for dx in range(-r, r + 1) for dz in range(-r, r + 1) if max(abs(dx), abs(dz)) == r}
            ring = {c for c in ring if ok(c)}
            if ring:
                return ring
        return {gc}

    def _walk_to(self, a, goal, stop=0.8):
        """Wish velocity toward goal: straight when the level line is clear, else along a BFS path over standable cells
        (re-planned every 0.5 s); sets a["jump_req"] when the next waypoint is one block up."""
        goal = np.asarray(goal, float); v = goal - a["pos"]; v[1] = 0; d = float(np.linalg.norm(v))
        if d <= stop:
            return np.zeros(3), d
        y = int(math.floor(a["pos"][1] + 1e-3))
        if not self._clear_line(a["pos"], goal, y):
            gc = (int(math.floor(goal[0])), int(math.floor(goal[2])))
            sc = (int(math.floor(a["pos"][0])), y, int(math.floor(a["pos"][2])))
            pl = a.get("path")
            if pl is None or pl[0] != gc or self.t - pl[2] > 10:
                a["path"] = pl = (gc, self._bfs3(sc, self._goal_columns(gc, y)), self.t)
            path = pl[1]
            if path and len(path) > 1:
                k = next((i for i, c in enumerate(path) if (c[0], c[2]) == (sc[0], sc[2])), 0)
                nxt = path[min(k + 1, len(path) - 1)]
                for c in path[min(k + 1, len(path) - 1):][:6][::-1]:                # furthest same-level waypoint in sight
                    if c[1] == y and all(cc[1] == y for cc in path[k:path.index(c) + 1]) and self._clear_line(a["pos"], np.array([c[0] + 0.5, 0, c[2] + 0.5]), y):
                        nxt = c; break
                if nxt[1] > y:
                    a["jump_req"] = True
                v = np.array([nxt[0] + 0.5, 0, nxt[2] + 0.5]) - a["pos"]; v[1] = 0
        n = float(np.linalg.norm(v))
        return (v / n * WALK if n > 1e-6 else np.zeros(3)), d

    def _bot(self, a):
        """-> (wish velocity [3], jump, target yaw, target pitch, attack, place (block, cell) or None)."""
        enemies = [b for b in self.ag if b["alive"] and b["team"] != a["team"]]
        near = min(enemies, key=lambda b: np.linalg.norm(b["pos"] - a["pos"])) if enemies else None
        dn = np.linalg.norm(near["pos"] - a["pos"]) if near is not None else np.inf
        role = a["bot"]
        if role == "builder" and a["last_hit_by"] is not None and near is not None and dn < 4:
            role = "fighter"                                                      # builders fight back at close range
        if role == "raider" and near is not None and dn < 3.5:
            role = "fighter"
        if role == "fighter" and near is not None:
            wish, d = self._walk_to(a, near["pos"], stop=2.0)
            yaw, pitch = self._aim(a, near["pos"] + [0, 1.2, 0])
            strafe = np.array([math.cos(yaw), 0, -math.sin(yaw)]) * (1.5 if (self.t // 30 + a["id"]) % 2 else -1.5)
            return wish + (strafe if d < 3 else 0), False, yaw, pitch, d < MELEE - 0.3, None
        if role == "builder":
            plan = [(c, b) for c, b in self.plans[a["team"]] if self.block(c) != b and not self._occupied(c)]
            if not plan:
                return np.zeros(3), False, a["yaw"], 0.0, False, None
            c, blk = plan[min(a["id"] // 2 % 3, len(plan) - 1)]                   # team-mates take different cells
            if self.block(c) != AIR:
                blk = None                                                        # a wrong block there: break it first
            else:
                for _ in range(4):                                                # no support (crater): fill from below
                    below = (c[0], c[1] - 1, c[2])
                    if self.block(below) != AIR or self._has_neighbour(c):
                        break
                    c, blk = below, DIRT
            ctr = np.asarray(c, float) + 0.5
            fp = np.array([cc for cc, _ in self.plans[a["team"]]])
            x0, x1, z0, z1 = fp[:, 0].min() - 0.8, fp[:, 0].max() + 1.8, fp[:, 2].min() - 0.8, fp[:, 2].max() + 1.8
            cands = [np.array([x0, 0, ctr[2]]), np.array([x1, 0, ctr[2]]), np.array([ctr[0], 0, z0]), np.array([ctr[0], 0, z1])]
            stand = min(cands, key=lambda s: (round(float(np.hypot(*(s - ctr)[[0, 2]])), 1), float(np.linalg.norm((s - a["pos"])[[0, 2]]))))
            stand[1] = a["pos"][1]
            wish, d = self._walk_to(a, stand, stop=0.4)
            yaw, pitch = self._aim(a, ctr + [0, -0.5, 0] if blk is not None else ctr)
            close = np.linalg.norm(ctr - self.eye(a)) < REACH - 0.3
            if blk is None:
                return wish, False, yaw, pitch, close, None
            return wish, False, yaw, pitch, False, (blk, c) if close else None
        if role == "raider":
            enemy = 1 - a["team"]
            built = [c for c, b in self.plans[enemy] if self.block(c) == b]
            if not built:
                if near is not None:
                    wish, d = self._walk_to(a, near["pos"], stop=2.0)
                    yaw, pitch = self._aim(a, near["pos"] + [0, 1.2, 0])
                    return wish, False, yaw, pitch, d < MELEE - 0.3, None
                return np.zeros(3), False, a["yaw"], 0.0, False, None
            built.sort(key=lambda c: np.linalg.norm(np.asarray(c) + 0.5 - self.eye(a)))
            ctr = np.asarray(built[0], float) + 0.5
            wish, d = self._walk_to(a, ctr, stop=2.2)
            yaw, pitch = self._aim(a, ctr)
            return wish, False, yaw, pitch, np.linalg.norm(ctr - self.eye(a)) < REACH - 0.5, None
        return np.zeros(3), False, a["yaw"], 0.0, False, None

    # ---- step ------------------------------------------------------------------------------------------------------
    def step(self, actions):
        dt = 1.0 / self.tick_hz
        self.t += 1
        ev = []
        for a in self.ag:
            if not a["alive"]:
                if self.t / self.tick_hz - a["dead_t"] > 2.0:
                    self._spawn(a); ev.append({"t": self.t, "type": "respawn", "who": a["id"]})
                continue
            x = actions.get(a["id"])
            a["cool"] = max(0.0, a["cool"] - dt)
            if x is not None:                                                    # an external controller (player / agent)
                a["yaw"] = a["yaw"] - math.radians(x.dyaw)                       # Action.dyaw: degrees, positive = turn right
                a["pitch"] = float(np.clip(a["pitch"] + math.radians(x.dpitch), -1.55, 1.55))
                f = np.array([-math.sin(a["yaw"]), 0, -math.cos(a["yaw"])]); r = np.array([math.cos(a["yaw"]), 0, -math.sin(a["yaw"])])
                wish = (f * x.move[0] + r * x.move[1]) * WALK
                jump, attack = bool(x.extra.get("jump")), bool(x.fire)
                place = (int(x.extra.get("block", PLANKS)), None) if x.extra.get("use") else None
            else:
                wish, jump, ty, tp, attack, place = self._bot(a)
                jump = jump or a.pop("jump_req", False)
                dyaw = (ty - a["yaw"] + math.pi) % (2 * math.pi) - math.pi
                a["yaw"] += float(np.clip(dyaw, -0.35, 0.35)); a["pitch"] += float(np.clip(tp - a["pitch"], -0.3, 0.3))
                attack = attack and abs(dyaw) < 0.3
            if "kb" in a:
                wish = wish + a.pop("kb")
            blocked = self._move(a, wish, jump, dt)
            if blocked and x is None and a["on_ground"]:
                a["stuck"] += 1
                if a["stuck"] % 6 == 0:
                    a["vel"][1] = JUMP_V; a["on_ground"] = False
            else:
                a["stuck"] = 0
            if a["pos"][1] < self.origin[1]:
                a["hp"] = 0; a["alive"], a["dead_t"] = False, self.t / self.tick_hz
                ev.append({"t": self.t, "type": "kill", "by": None, "victim": a["id"], "cause": "void"}); continue
            if a["cool"] == 0.0 and attack:
                self._attack(a, ev)
            elif a["cool"] == 0.0 and place is not None:
                blk, at = place                                                  # bots name the cell, players aim
                if not self._place(a, blk, ev, at=at) and at is not None:
                    a["stuck"] += 1
        for t in list(self.tnt):
            t["fuse"] -= dt
            if t["fuse"] <= 0:
                self.tnt.remove(t); self._explode(t["pos"] + [0, 0.5, 0], ev, t["by"])
        return ev

    # ---- world edits (scripted or by hand) -------------------------------------------------------------------------
    def edit(self, op):
        """op = {"op": "box"|"clear", "lo": [x,y,z], "hi": [x,y,z], "block": name}; {"op": "structure", "kind": house|tower|wall|bridge,
        "at": [x,y,z], "block": name, "for_team": 0|1 (becomes that team's blueprint, built by its builders) or absent (placed now)};
        {"op": "tnt", "at": [x,y,z], "fuse": s}; {"op": "set_bot", "agent": i, "bot": role}; {"op": "heal", "agent": i}."""
        ev, kind = [], op.get("op")
        blk = BLOCK_NAMES.index(op.get("block", "oak_planks"))
        if kind in ("box", "clear"):
            lo, hi = np.minimum(op["lo"], op["hi"]), np.maximum(op["lo"], op["hi"])
            n = 0
            for x in range(int(lo[0]), int(hi[0]) + 1):
                for y in range(int(lo[1]), int(hi[1]) + 1):
                    for z in range(int(lo[2]), int(hi[2]) + 1):
                        n += self._set((x, y, z), AIR if kind == "clear" else blk)
            ev.append({"t": self.t, "type": "edit", "op": kind, "cells": n})
        elif kind == "structure":
            cells = blueprint(op["kind"], op["at"], material=blk, size=int(op.get("size", 5)), height=int(op.get("height", 3)), axis=op.get("axis", "x"))
            if "for_team" in op:
                self.plans[int(op["for_team"])] = cells; self.sites[int(op["for_team"])] = tuple(int(v) for v in op["at"])
                ev.append({"t": self.t, "type": "edit", "op": "blueprint", "team": int(op["for_team"]), "cells": len(cells)})
            else:
                n = sum(self._set(c, b) for c, b in cells)
                ev.append({"t": self.t, "type": "edit", "op": "structure", "kind": op["kind"], "cells": n})
        elif kind == "tnt":
            self.tnt.append({"id": self._next_tnt, "pos": np.asarray(op["at"], float) + [0.5, 0, 0.5], "fuse": float(op.get("fuse", 3.0)), "by": -1})
            self._next_tnt += 1
            ev.append({"t": self.t, "type": "edit", "op": "tnt", "at": list(op["at"])})
        elif kind == "set_bot":
            self.ag[int(op["agent"])]["bot"] = op["bot"]; ev.append({"t": self.t, "type": "edit", "op": "set_bot", "agent": int(op["agent"]), "bot": op["bot"]})
        elif kind == "heal":
            self.ag[int(op["agent"])]["hp"] = HP; ev.append({"t": self.t, "type": "edit", "op": "heal", "agent": int(op["agent"])})
        else:
            raise ValueError(f"unknown edit op {kind}; known {EDIT_OPS}")
        return ev

    # ---- views -----------------------------------------------------------------------------------------------------
    def agents(self):
        return [a["id"] for a in self.ag]

    def mesh(self):
        if self._mesh is None:
            self._mesh = VP.mesh_world(self.grid, tuple(int(v) for v in self.origin))
        return self._mesh

    def entities(self):
        """[(entity id, bottom centre, size, team or -1, owner agent or -1)]: players, then primed TNT."""
        out = [(a["id"], a["pos"].copy(), (W_P, H_P, W_P), a["team"], a["id"]) for a in self.ag if a["alive"]]
        out += [(100 + (t["id"] % 100), t["pos"].copy(), (0.98, 0.98, 0.98), -1, -1) for t in self.tnt]
        return out

    def camera(self, agent_id):
        a = self.ag[agent_id]
        return self.eye(a), a["yaw"], a["pitch"]

    def render_views(self, renderer, H=192, W=336, vfov=70.0, agents=None):
        """Per-agent G-buffers from ONE state snapshot (each agent's own body hidden) -> {agent: dict of [H, W] arrays}."""
        out = {}
        ents = self.entities()
        for i in (agents if agents is not None else self.agents()):
            if not self.ag[i]["alive"]:
                continue
            e = [(eid, pos, size) for eid, pos, size, _, owner in ents if owner != i]
            g = renderer.render(self.mesh(), [self.camera(i)], e, H=H, W=W, vfov=vfov)
            out[i] = {k: v[0] for k, v in g.items()}
        return out

    def team_of_entity(self):
        return {eid: team for eid, _, _, team, _ in self.entities()}

    def state(self):
        """Compact JSON-able state (for external controllers and the logs)."""
        return {"t": self.t, "players": [{"id": a["id"], "team": a["team"], "bot": a["bot"], "alive": a["alive"], "hp": round(a["hp"], 1),
                                          "pos": [round(float(v), 2) for v in a["pos"]]} for a in self.ag],
                "tnt": [{"pos": [round(float(v), 2) for v in t["pos"]], "fuse": round(t["fuse"], 2)} for t in self.tnt],
                "progress": {team: f"{sum(self.block(c) == b for c, b in self.plans[team])}/{len(self.plans[team])}" for team in (0, 1)},
                "solid_blocks_above_ground": int((self.grid[:, self.gy + 1 - self.origin[1]:, :] != AIR).sum())}

    def prompt(self):
        return self._prompt or ("First-person Minecraft gameplay on a superflat grass world: blocky terrain, players building "
                                "and breaking blocks, other players nearby.")

    def info(self):
        return {"name": self.name, "agents": self.n, "half": self.half, "ground_y": self.gy, "grid": list(self.grid.shape)}
