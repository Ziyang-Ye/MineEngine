"""Synthetic capture episodes: mc.py's world program (bots Alpha / Bravo, PvP, TNT) run for `seconds` and written in
EXACTLY the layout of our patched SolarisEngine stateful capture (OUTPUT_SCHEMA.md + requirement ids T12-T16 and T18-T20 of docs/requirements.md), so the
capture -> proxy pipeline (cap_reader / cap2proxy) can be tested before any real capture exists. Field names, units and
clocks follow the schema, not the reader:
  out_root/output/TS_EEEEEE_<Bot>_instance_III.json        per-frame action array (~20 Hz, renderTime = epoch ms jittered and
                                                            not tick-locked; absolute yaw / pitch (5 dp; null on frame 0 as
                                                            the recorder keeps it) + *_mc_deg, action keys, prismarine
                                                            inventory, physicsTime, worldAgeTicks, extra_info);
                  ..._meta.json, ..._episode_info.json      (world.*, world_state(_end), state_metadata, skin {file, sha1,
                                                            model, slim, applied_epoch_ms}, session_id for persistent worlds);
                  TS_EEEEEE_world_instance_III_world_events.jsonl
                                                            episode_begin, tick every server tick (players: pos feet, yaw /
                                                            pitch MC degrees, health, held_slot, held_item, sneaking,
                                                            sprinting, on_ground + T16 food, saturation, body_yaw / head_yaw,
                                                            crosshair {type block, pos, face, distance | type entity, uuid};
                                                            pve: "mobs" with uuid, type, pos, yaw, bbox, health, target_uuid),
                                                            camera_pose per tick and camera (T12: the camera player's own
                                                            location = feet; its eye is 1.62 x 0.785 above them while the bot
                                                            sneaks: CameraManager's GENERIC_SCALE), block_state at the start of
                                                            the tick after a change, block_place / block_break / block_fade
                                                            (grass under a block -> dirt) / tnt_prime / entity_explode /
                                                            player_interact, entity_spawn / remove / death, player_respawn,
                                                            T15 player_animation ARM_SWING, entity_damage (entity, damager,
                                                            damage, cause, knockback, health_after), item_held, item_use_start /
                                                            end (Bravo eats in build / pvp), T17 block_damage +
                                                            block_break_progress stages 0..9 before every player break, stage -1
                                                            at the break; note, episode_end. names="alt": arm_swing,
                                                            entity_damage_by_entity with actor() fields (entity = victim,
                                                            player / entity_type / entity_uuid = damager), player_item_held
                                                            (previous_slot), use_item_start / player_item_consume, dig_start /
                                                            dig_progress / dig_finish, and T12 as CameraAlpha / CameraBravo
                                                            entries of the tick "players" list (no camera_pose records);
                  ..._world_snapshot_{start,end}.json.gz    palette + rle in yzx order + biomes + entities;
                  skins/<sha1>.png                          (T18: per-player skin record; a synthetic 64 x 64 skin per player)
  out_root/camera/output_<bot>/0/camera_<bot>_meta.json     session camera meta (T13 fov, fovEffectScale, guiScale)
  out_root/aligned/<action stem>_camera{.mp4,_meta.json}    with video=True: a placeholder video, one frame per action entry
                                                            except the last 2..4 (the recording ended first: frame_mapping /
                                                            diagnostics n_matched, unmatched_actions_end as process_recordings)
  out_root/synth_truth/<world stem>.npz                     truth: final / start / checkpoint dense grids (cap_palette ids,
                                                            [sx, sy, sz]), per bot and frame t_ms, health, camera eye / yaw /
                                                            pitch (Mineflayer rad), mobs alive, using, crosshair block, dig
                                                            (x, y, z, stage), hotbar names / counts, body yaw; n_pair (frames
                                                            with video); vfov (fov x 1.1 without fovEffectScale 0: flying).
Garden (G-buffer v2 checks; write_episode garden=True, write_root: the batch_0 build episode): GARDEN = a row of
block states at z = -20 on the grass (x -8..7, away from the sites, spawns and bot paths; not in mc.py's grid, so the world
program never touches it): door (lower + upper), bottom / top slab side by side, wheat on farmland, snow layers, a water
source, torch, poppy, furnace; at GARDEN_FLIP of the simulated ticks the door opens, the wheat grows to age 7, the snow grows
to 6 layers, the water drops to level 4 and the furnace lights (block_state records at the next tick); truth garden_cells /
garden_s0 / garden_s1 ("" = unchanged) / garden_t (epoch ms the new states are visible from).
Kinds: build (two builders: house + tower), break (raider vs builder; standalone: the tower is built in setup, before the
start snapshot), pvp (two fighters 17 m apart, deaths and respawns), pve (both fight zombies: per-tick mobs, mob damage,
deaths, drops), tnt (Alpha places TNT, ignites it with flint and steel, retreats; chain explosions, crater, drops).
Persistent worlds (T20): persist_from = the previous episode's end snapshot; the start snapshot is that grid (no setup
edits, no reset), game time / clock continue, items stay. Conventions: Mineflayer yaw rad [0, 2pi) (0 = -z, +pi/2 = -x),
pitch up+; world log MC degrees (yaw = (pi - radians(mc)) mod 2pi); the camera player follows its bot every tick (T12):
camera_pose pos = bot feet after the previous tick, rendered eye = pos + 1.62, x 0.785 while the bot sneaks (the camera player
itself never sneaks, it is scaled); a block_state record is visible from the first frame with time >= its epoch_ms; health
is what the HUD shows from the damage / death / respawn event time on. Variants (write_episode): names canonical | alt, t16
tick | action (T16 per frame, Mineflayer radians), render_time epoch | relative (renderTime = client ms, render_time_is_epoch
false, epochTime = host clock), cam_pos feet | eye. Deterministic per (seed, eid, kind, variant).
engine "real" (write_episode; write_root real_kinds -> DIR/batch_2): the capture engine's own output (Windows runtime + capture mod +
Paper plugin, as read from its source) instead of the schema layout: tick players with head_yaw / body_yaw (MC degrees, Alpha's
body_yaw not normalised), food, saturation, dead, gamemode, flying, blocking, hand_raised and no crosshair / hotbar; tick
"cameras" [{name, target, pos, yaw, pitch, ...}] = the server-followed camera players one tick behind; pve: "entities" records
(x / y / z, yaw = head yaw, body_yaw 30 deg off, bbox_w / bbox_h) instead of tick "mobs"; arm_swing, held_item_change,
use_item_start / use_item_stop (+ item_consume), entity_damage {victim, attacker, final_damage, health_after} + entity_knockback,
entity_death {entity, victim, killer_entity}, dig_start / dig_progress / dig_finish, no player_respawn (tick dead true ->
false); action entries with pose_seq (controller hiccups: no pose packet for one tick every 2.5 s, so an entry can carry a
50-100 ms old packet), T16 nested in "state" (head_yaw radians, body_yaw null, cursor {block, entity} = frame-recorder's 5 m
cast, selected_slot, hotbar [{slot, id, count}]) and "peer"; episode_info without "skin": state_metadata.skins {Bot: {skin_id,
model, source, sha256, texture_hash, seed, file pool/<id>.png, reference, tags, texture_mode, camera}} + skin_assignment as
skins.js writes it (the follower's applied lists only itself; ok = no error of either player: REAL_CASES assign_error fails a
skin in the leader's file too, pool_differed adds the follower's informational note), angle_convention {yaw, pitch}; skin PNGs
in the pool dir (REAL_CASES export_skins: also <stem>_skin_<Bot>.png next to each episode_info); camera/output_<bot>/<inst>/
camera_<bot>_meta.json = the mod's finalized meta (options.fov_deg 70 / fov_effect_scale 0, pose_link, local_skins of the other
bot's skins merged over the batch, stop_epoch_seconds, capture_stats; REAL_CASES camera_restart: that camera's
camera_<bot>_r1_meta.json as written at its recording start, never finalized); aligned meta with Windows paths, camera_params
(unless omit_camera_params), sidecar diagnostics and frame_mapping with cam_pos / cam_yaw (unwrapped) / cam_pitch / fov /
pose_source / pose_seq / pose_pair / cam_screen / delta_sec (write_video_real: late start, one 400 ms stall, datagram latency
spikes -> pose_seq "next" re-pairs up to 2 frames late, a dropped capture slot per controller packet gap -> "prev" re-pairs up
to 2 frames early, server-pose and loading-screen frames); truth adds pair_ok / clean / cam_src per bot, skin_ok / sha1 /
applied / camcheck (-1 = camera meta not finalized), valid, and uses the hand_raised ticks for "using" and the next tick record
for a respawn's health.
  python -m mineengine.cap_synth --out DIR [--kinds build,break,pvp,pve,tnt] [--chain 3] [--seconds 20] [--seed 0] [--video] [--real-kinds build,pvp]
      -> DIR/batch_0 (one episode per kind, eid 1..), DIR/batch_1 (a persistent chain of N episodes with the CHAIN variants) and
         DIR/batch_2 (--real-kinds: the capture engine's own layout)
  python -m mineengine.cap_synth --test          (8 s episodes: determinism + the schema's own replay / names checks, no cap_reader)
"""
import argparse
import gzip
import hashlib
import json
import math
import os
import struct
import sys
import time
import zlib

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__)); CODE = os.path.dirname(HERE)
from mineengine import cap_palette as CP  # noqa: E402
from mineengine import mc as MCW  # noqa: E402  (world program; pulls core.py and voxel_proxy)
from mineengine.core import Action  # noqa: E402

TAU = 2 * math.pi
HALF, GY = 32, -61                                                               # mc.py grid x, z in [-32, 32), grass top at y = -61
BOX_MIN, BOX_SIZE = np.array([-32, -64, -32]), (65, 45, 65)                      # snapshot box: radius 32, y -64 (world min) .. -20
BOTS, CAMS = ("Alpha", "Bravo"), ("CameraAlpha", "CameraBravo")
TICK_MS, EV_MS, FRAME_MS = 50, 8, 50
KINDS = {"build": dict(type="buildStructure", roles=("builder", "builder"), spawn=None),
         "break": dict(type="buildAndBreak", roles=("raider", "builder"), spawn=(-12.5, 12.5)),
         "pvp": dict(type="pvpArena", roles=("fighter", "fighter"), spawn=(-8.5, 8.5)),
         "pve": dict(type="pveZombies", roles=("fighter", "fighter"), spawn=(-10.5, 10.5)),
         "tnt": dict(type="tntBlast", roles=("builder", "builder"), spawn=None)}
KIND_IX = {k: i for i, k in enumerate(KINDS)}
STATE12 = {MCW.AIR: "minecraft:air", MCW.GRASS: "minecraft:grass_block[snowy=false]", MCW.STONE: "minecraft:stone",
           MCW.DIRT: "minecraft:dirt", MCW.BEDROCK: "minecraft:bedrock", MCW.PLANKS: "minecraft:oak_planks",
           MCW.COBBLE: "minecraft:cobblestone", MCW.GLASS: "minecraft:glass", MCW.LOG: "minecraft:oak_log[axis=y]",
           MCW.TNT: "minecraft:tnt[unstable=false]", MCW.RED_WOOL: "minecraft:red_wool", MCW.BLUE_WOOL: "minecraft:blue_wool"}
MATERIALS = {MCW.PLANKS: ("minecraft:oak_planks", "minecraft:spruce_planks", "minecraft:birch_planks", "minecraft:stone_bricks",
                          "minecraft:white_concrete", "minecraft:bricks"),
             MCW.COBBLE: ("minecraft:cobblestone", "minecraft:mossy_cobblestone", "minecraft:andesite", "minecraft:deepslate_bricks"),
             MCW.LOG: ("minecraft:oak_log[axis=y]", "minecraft:spruce_log[axis=y]", "minecraft:dark_oak_log[axis=y]"),
             MCW.GLASS: ("minecraft:glass", "minecraft:light_blue_stained_glass")}
WOODS = ("oak", "spruce")
STAIR = "minecraft:{w}_stairs[facing={f},half=bottom,shape=straight,waterlogged=false]"
SLAB = "minecraft:{w}_slab[type=bottom,waterlogged=false]"
REV = {s: i for i, s in STATE12.items()}
REV.update({s: i for i, ss in MATERIALS.items() for s in ss})
REV.update({STAIR.format(w=w, f=f): MCW.LOG for w in WOODS for f in ("north", "south", "east", "west")})
REV.update({SLAB.format(w=w): MCW.LOG for w in WOODS})
HELD = {"pvp": (("diamond_sword", "bread"), ("iron_axe", "cooked_beef")), "pve": (("iron_sword",), ("stone_sword",)),
        "break": (("iron_pickaxe",), None), "tnt": (None, None), "build": (None, None)}
SIZES = {"zombie": (0.6, 1.95), "item": (0.25, 0.25), "tnt": (0.98, 0.98)}
CAM_EYE, CAM_SNEAK = 1.62, 0.785                                                 # CameraManager: camera GENERIC_SCALE 0.785 while its bot sneaks
EAT_TICKS, DIG_TICKS = 32, 8
NAMES = {"canonical": dict(swing="player_animation", damage="entity_damage", held="item_held", use_start="item_use_start", use_end="item_use_end",
                           dig_start="block_damage", dig_progress="block_break_progress", dig_finish=None, camera="camera_pose"),
         "alt": dict(swing="arm_swing", damage="entity_damage_by_entity", held="player_item_held", use_start="use_item_start", use_end="player_item_consume",
                     dig_start="dig_start", dig_progress="dig_progress", dig_finish="dig_finish", camera=None),     # alt T12: camera players in tick "players"
         "real": dict(swing="arm_swing", damage="entity_damage", held="held_item_change", use_start="use_item_start", use_end="use_item_stop",
                      dig_start="dig_start", dig_progress="dig_progress", dig_finish="dig_finish", camera=None)}    # the capture plugin's WorldStateLogger (T12: tick "cameras")
CHAIN = ({"kind": "build", "names": "alt", "t16": "action"}, {"kind": "break", "names": "alt", "render_time": "relative"},
         {"kind": "tnt", "cam_pos": "eye"})                                    # batch_1 variants (cap_selftest uses the same)
SKIN_POOL = ((60, 90, 200), (200, 60, 60), (40, 160, 70), (230, 200, 60), (120, 60, 160), (30, 30, 30), (240, 240, 240), (150, 100, 50))
REAL_CASES = {"build": {"export_skins": True, "pool_differed": "Bravo"}, "break": {"omit_camera_params": True, "camera_restart": "Alpha"},
              "pvp": {"missing_skin": "Bravo"}, "pve": {"info_error": True, "assign_error": "Bravo"}}
FACE = {(0, 1, 0): "up", (0, -1, 0): "down", (0, 0, -1): "north", (0, 0, 1): "south", (-1, 0, 0): "west", (1, 0, 0): "east"}
CAM_OPTIONS = {"fov_deg": 70, "fov_effect_scale": 0.0, "render_distance": 12, "simulation_distance": 12, "server_view_distance": 10, "effective_render_distance": 10,
               "gui_scale": 2, "gui_scale_effective": 2.0, "bob_view": False, "damage_tilt_strength": 0.0, "entity_shadows": True, "clouds": "fancy",
               "graphics_mode": "fancy", "particles": "all", "hud_visible": True, "name_tags_visible": True, "toasts_suppressed": True, "chat_visibility": "hidden"}
WIN_ROOT = "C:\\Users\\cap\\capture\\windows_runs"                           # run_windows writes resolved Windows paths into the aligned meta
GARDEN_Z, GARDEN_FLIP = -20, 0.5                                                 # module docstring "Garden"
DOOR = "minecraft:oak_door[facing=south,half={h},hinge=left,open={o},powered=false]"
GARDEN = ((-8, 0, DOOR.format(h="lower", o="false"), DOOR.format(h="lower", o="true")), (-8, 1, DOOR.format(h="upper", o="false"), DOOR.format(h="upper", o="true")),
          (-6, 0, "minecraft:oak_slab[type=bottom,waterlogged=false]", None), (-5, 0, "minecraft:oak_slab[type=top,waterlogged=false]", None),
          (-3, -1, "minecraft:farmland[moisture=7]", None), (-3, 0, "minecraft:wheat[age=0]", "minecraft:wheat[age=7]"),
          (-1, 0, "minecraft:snow[layers=1]", "minecraft:snow[layers=6]"), (1, 0, "minecraft:water[level=0]", "minecraft:water[level=4]"),
          (3, 0, "minecraft:torch", None), (5, 0, "minecraft:poppy", None),
          (7, 0, "minecraft:furnace[facing=south,lit=false]", "minecraft:furnace[facing=south,lit=true]"))


# ---- small helpers --------------------------------------------------------------------------------------------------
def uuid_of(*key):
    h = hashlib.md5("/".join(str(k) for k in key).encode()).hexdigest()
    return f"{h[:8]}-{h[8:12]}-4{h[13:16]}-a{h[17:20]}-{h[20:32]}"


def ts_str(ms):
    return time.strftime("%Y%m%d_%H%M%S", time.gmtime(ms / 1000.0))


def mc_deg(yaw, pitch):
    """Mineflayer rad -> Minecraft degrees (yaw wrapped to [-180, 180) as the server reports it, pitch +down)."""
    y = (math.degrees(math.pi - yaw) + 180.0) % 360.0 - 180.0
    return round(y, 3), round(-math.degrees(pitch), 3)


def wrap(a):
    return (a + math.pi) % TAU - math.pi


def bare(s):
    return str(s).split("[", 1)[0].split(":", 1)[-1]


def item_obj(name, count, slot):
    if not name:
        return None
    tool = any(k in name for k in ("sword", "axe", "pickaxe", "shovel", "flint_and_steel"))
    return {"type": zlib.crc32(name.encode()) % 1300, "count": int(count), "metadata": 0, "nbt": None, "name": name,
            "displayName": name.replace("_", " ").title(), "stackSize": 1 if tool else 64, "slot": int(slot)}


def png_bytes(rgba):
    """[H, W, 4] uint8 -> PNG file bytes (stdlib zlib)."""
    h, w = rgba.shape[:2]
    raw = b"".join(b"\x00" + rgba[y].tobytes() for y in range(h))
    chunk = lambda t, d: struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d) & 0xFFFFFFFF)  # noqa: E731
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 6, 0, 0, 0)) + chunk(b"IDAT", zlib.compress(raw, 9)) + chunk(b"IEND", b"")


def skin_png(k):
    """Synthetic 64 x 64 skin k: head skin tone, shirt / trousers from SKIN_POOL."""
    a = np.zeros((64, 64, 4), np.uint8); a[..., 3] = 255
    a[:, :] = (196, 145, 110, 255)
    a[16:32, :] = SKIN_POOL[k % len(SKIN_POOL)] + (255,)
    a[48:64, :] = SKIN_POOL[(k + 3) % len(SKIN_POOL)] + (255,)
    a[8:16, 8:16] = (90, 60, 40, 255); a[12, 10] = a[12, 13] = (255, 255, 255, 255)
    return png_bytes(a)


def write_json(path, obj, **kw):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path + ".part", "w", encoding="utf-8") as fh:
        json.dump(obj, fh, **kw)
    os.replace(path + ".part", path)


def vfov_of_meta(m):
    """the camera's vertical fov from its session meta: fov (options.txt scale v in [-1, 1] -> 70 + 40 v) x (1 + 0.1 x
    fovEffectScale) - the camera player flies (x1.1 at fovEffectScale 1, the default when not written; T13 sets 0)."""
    if not m or m.get("fov") is None:
        return 70.0
    v = float(m["fov"]); fov = 70.0 + 40.0 * v if -1.0 <= v <= 1.0 else v
    return fov * (1.0 + 0.1 * float(m.get("fovEffectScale", 1.0)))


def encode_snapshot(sgrid, palette):
    """[sx, sy, sz] palette indices + palette -> (compact palette, rle) in yzx order (x fastest), palette by first use."""
    flat = np.ascontiguousarray(sgrid.transpose(1, 2, 0)).reshape(-1)
    used, first = np.unique(flat, return_index=True)
    order = used[np.argsort(first)]
    remap = np.full(len(palette), -1, np.int64); remap[order] = np.arange(len(order))
    f = remap[flat]
    cut = np.r_[0, np.nonzero(np.diff(f))[0] + 1, len(f)]
    rle = np.stack([np.diff(cut), f[cut[:-1]]], 1).reshape(-1)
    return [palette[i] for i in order], [int(v) for v in rle]


def load_snapshot_states(path):
    """End snapshot -> (dense [sx, sy, sz] state strings as palette indices, palette, raw header) by the schema's formula."""
    s = json.load(gzip.open(path, "rt", encoding="utf-8"))
    sx, sy, sz = s["size"]
    dense = np.repeat(np.asarray(s["rle"][1::2], np.int64), np.asarray(s["rle"][0::2], np.int64))
    assert dense.size == sx * sy * sz and s.get("order", "yzx") == "yzx"
    return dense.reshape(sy, sz, sx).transpose(2, 0, 1).astype(np.int32), list(s["palette"]), s


# ---- the episode writer ---------------------------------------------------------------------------------------------
class Episode:
    def __init__(self, out_root, eid, kind, seconds, seed, persist_from, video, inst, cam_meta):
        assert kind in KINDS, kind
        self.out_root, self.out, self.eid, self.kind, self.inst, self.video = out_root, os.path.join(out_root, "output"), int(eid), kind, int(inst), video
        self.cfg, self.seconds, self.seed = KINDS[kind], float(seconds), int(seed)
        self.rng = np.random.default_rng([self.seed, self.eid, KIND_IX[kind], 7])
        self.cam_meta, self.cam_pos, self.persist_next = cam_meta, "feet", False
        self.names, self.t16, self.render_time = "canonical", "tick", "epoch"
        self.engine, self.real, self.skin_pool = "schema", {}, None               # engine "real": the capture engine's own layout (write_episode)
        self.garden, self.garden_t = False, None
        self.prev = None
        if persist_from:
            sg, pal, hdr = load_snapshot_states(persist_from)
            assert tuple(hdr["size"]) == BOX_SIZE and list(hdr["origin"]) == BOX_MIN.tolist(), "persistent world: box differs"
            self.prev = dict(sgrid=sg, palette=pal, hdr=hdr)
            self.T0 = int(hdr["epoch_ms"]) + 20000 + int(self.rng.integers(0, 900))
            self.G0, self.S0 = int(hdr["game_time"]) + 400, int(hdr["server_tick"]) + 400
            self.session = dict(hdr.get("_session") or {})
        else:
            self.T0 = 1_761_200_000_000 + self.seed * 86_400_000 + self.eid * 3_600_000 + int(self.rng.integers(0, 900))
            self.G0, self.S0 = 120_000 + 5000 * self.eid, 3000 + 5000 * self.eid
            self.session = {}
        self.K_sim = int(round(self.seconds * 1000 / TICK_MS)); self.K_total = self.K_sim + 18
        jit = self.rng.integers(0, 4, self.K_total + 1)
        spike = int(self.rng.integers(self.K_sim // 4, self.K_sim // 2))                 # one lag spike, caught up after 3 ticks
        lag = np.zeros(self.K_total + 1, np.int64); lag[spike:spike + 4] = [36, 24, 12, 0]
        self.tick_t = self.T0 + TICK_MS * np.arange(self.K_total + 1) + jit + lag
        self.ts = {"world": ts_str(self.T0), "Alpha": ts_str(self.T0 + 1400), "Bravo": ts_str(self.T0 + 2600)}
        self.stem = f"{self.ts['world']}_{self.eid:06d}_world_instance_{self.inst:03d}"
        self.astem = {b: f"{self.ts[b]}_{self.eid:06d}_{b}_instance_{self.inst:03d}" for b in BOTS}
        self.seq, self.recs = 0, []
        self.world_seed = int(zlib.crc32(f"synth{self.seed}".encode())) - 2 ** 31
        # frames: not tick-locked, from 0.6 s after episode_begin to 0.6 s after the last simulated tick
        self.frames = {}
        for j, b in enumerate(BOTS):
            a0 = self.T0 + 620 + 35 * j
            n = int((self.tick_t[self.K_sim] + 600 - a0) // FRAME_MS)
            r = a0 + FRAME_MS * np.arange(n) + self.rng.integers(-7, 8, n)
            self.frames[b] = r.astype(np.int64)
        assert all(np.all(np.diff(r) > 0) and r[-1] < self.tick_t[self.K_total - 2] for r in self.frames.values())

    # ---- records ----------------------------------------------------------------------------------------------------
    def rec(self, ty, t, k, **kw):
        r = {"type": ty, "seq": self.seq, "game_time": self.G0 + k, "server_tick": self.S0 + k, "epoch_ms": int(t)}
        r.update(kw); self.recs.append(r); self.seq += 1
        return r

    def note(self, t, k, **kw):
        self.rec("note", t, k, text=json.dumps(dict(ep=self.eid, **kw), separators=(",", ":")))

    # ---- states -----------------------------------------------------------------------------------------------------
    def pal(self, s):
        i = self.pidx.get(s)
        if i is None:
            i = self.pidx[s] = len(self.palette); self.palette.append(s); self.lut.append(CP.block_id(s))
        return i

    def state_of(self, cell, mid):
        if mid == MCW.LOG and tuple(cell) in self.roof:
            return self.roof[tuple(cell)]
        if mid in self.mat:
            return self.mat[mid]
        return STATE12.get(int(mid), "minecraft:cobblestone")

    def bidx(self, cell):
        return tuple(int(c) - int(o) for c, o in zip(cell, BOX_MIN))

    def state_at(self, cell):
        return self.palette[self.sgrid[self.bidx(cell)]]

    def cap_grid(self):
        return np.asarray(self.lut, np.uint16)[self.sgrid]

    # ---- setup ------------------------------------------------------------------------------------------------------
    def setup(self):
        k = self.kind
        roles = list(self.cfg["roles"])
        nmob = 3 if k == "pve" else 0
        w = self.w = MCW.MC(n_agents=2 + nmob, half=HALF, ground_y=GY, seed=int(self.seed * 1000 + self.eid * 10 + KIND_IX[k]),
                            bots=roles + ["fighter"] * nmob)
        self.mat = {m: str(self.rng.choice(v)) for m, v in MATERIALS.items()}
        wood = str(self.rng.choice(WOODS))
        x0, y0, z0 = w.sites[0]; size, top = 5, y0 + 3                            # house blueprint roof (mc.py blueprint("house"))
        self.roof = {}
        for x in range(x0, x0 + size):
            for z in range(z0, z0 + size):
                f = "north" if z == z0 else "south" if z == z0 + size - 1 else "west" if x == x0 else "east" if x == x0 + size - 1 else None
                self.roof[(x, top, z)] = STAIR.format(w=wood, f={"north": "south", "south": "north", "west": "east", "east": "west"}[f]) if f else SLAB.format(w=wood)
        if k == "tnt":
            w.plans[0] = MCW.blueprint("platform", (-20, GY + 1, 5), material=MCW.TNT, size=2)
        if k == "pve":
            for i, a in enumerate(w.ag):
                a["team"] = 0 if i < 2 else 1
        if self.cfg["spawn"]:
            w.spawn_pt = {0: np.array([self.cfg["spawn"][0], GY + 1.0, 0.5]), 1: np.array([self.cfg["spawn"][1], GY + 1.0, 0.5])}
        # world grid over the box: flat column + mc.py's grid, or the previous episode's end snapshot (persistent world)
        sx, sy, sz = BOX_SIZE
        self.palette, self.pidx, self.lut = [], {}, []
        if self.prev is not None:
            for s in self.prev["palette"]:
                self.pal(s)
            self.sgrid = self.prev["sgrid"].copy()
            mids = np.array([REV.get(s, MCW.AIR if CP.block_id(s) == CP.AIR else MCW.COBBLE) for s in self.palette], np.uint16)
            gx, gy_, gz = w.grid.shape
            w.grid[:] = mids[self.sgrid[:gx, :gy_, :gz]]; w.grid[:, 0, :] = MCW.BEDROCK; w._mesh = None
        else:
            col = np.full(sy, MCW.AIR, np.uint16)
            for j in range(sy):
                y = BOX_MIN[1] + j
                col[j] = MCW.GRASS if y == GY else MCW.DIRT if GY - 2 <= y < GY else MCW.BEDROCK if y == -64 else MCW.STONE if y < GY - 2 else MCW.AIR
            ids = np.broadcast_to(col[None, :, None], BOX_SIZE).copy()
            if k == "break":                                                    # setup command (before the start snapshot)
                for c, b in w.plans[1]:
                    w._set(c, b)
            gx, gy_, gz = w.grid.shape
            ids[:gx, :gy_, :gz] = w.grid
            self.sgrid = np.zeros(BOX_SIZE, np.int32)
            for v in np.unique(ids):
                m = ids == v
                if v in (MCW.AIR, MCW.GRASS, MCW.DIRT, MCW.BEDROCK, MCW.STONE):
                    self.sgrid[m] = self.pal(STATE12[int(v)])
                else:
                    for c in np.argwhere(m):
                        cell = tuple(int(q) for q in c + BOX_MIN); self.sgrid[tuple(c)] = self.pal(self.state_of(cell, int(v)))
        if self.garden and self.prev is None:                                   # module docstring "Garden"
            for dx, dy, s0, _ in GARDEN:
                self.sgrid[self.bidx((dx, GY + 1 + dy, GARDEN_Z))] = self.pal(s0)
        for a in w.ag[:2]:
            w._spawn(a)
        self.mobs = {}                                                          # agent id -> dict(uuid, state: pending / alive / dead / gone)
        for i in range(2, len(w.ag)):
            a = w.ag[i]
            pending = i == len(w.ag) - 1
            if pending:
                a.update(alive=False, dead_t=math.inf, pos=np.array([0.0, -1000.0, 0.0]), hp=0.0)
            else:
                w._spawn(a)
            self.mobs[i] = dict(uuid=uuid_of(self.seed, self.eid, "zombie", i), state="pending" if pending else "alive", t1=None,
                                spawn_k=int(self.K_sim * 0.35) if pending else 0, target=None)
        # per-bot inventory
        self.inv = {}
        for i, b in enumerate(BOTS):
            role = w.ag[i]["bot"]
            hb = [[None, 0] for _ in range(9)]
            held = HELD[k][i]
            if k == "tnt" and i == 0:
                hb[0], hb[1] = ["tnt", 64], ["flint_and_steel", 1]
            elif held:
                for j, n in enumerate(held):
                    hb[j] = [n, 1 if j == 0 else 16]
            elif role == "builder":
                for j, n in enumerate(dict.fromkeys(bare(self.state_of(c, m)) for c, m in w.plans[w.ag[i]["team"]])):
                    if j < 9:
                        hb[j] = [n, 64]
            self.inv[b] = dict(hb=hb, slot=0)
        self.items = {}                                                         # uuid -> dict(ent, k0)
        self.tnt_uuid = {}                                                      # mc.py tnt id -> uuid
        self.hp_hist = {b: [(-math.inf, 20.0)] for b in BOTS}
        self.body = {b: w.ag[i]["yaw"] for i, b in enumerate(BOTS)}
        self.fades, self.dead_bots = [], {b: 0 for b in BOTS}
        self.sneak = (int(self.K_sim * 0.30), int(self.K_sim * 0.42)) if k in ("build", "break", "pvp") else (-1, -1)
        self.ign = None

    # ---- entities -----------------------------------------------------------------------------------------------------
    def player_ent(self, i):
        a = self.w.ag[i]; b = BOTS[i]; y, p = mc_deg(a["yaw"], a["pitch"])
        return {"uuid": uuid_of("player", b), "type": "minecraft:player", "pos": [round(float(v), 4) for v in a["pos"]], "yaw": y, "pitch": p, "name": b}

    def mob_ent(self, i):
        a = self.w.ag[i]; y, p = mc_deg(a["yaw"], a["pitch"])
        return {"uuid": self.mobs[i]["uuid"], "type": "minecraft:zombie", "pos": [round(float(v), 4) for v in a["pos"]], "yaw": y, "pitch": p}

    def ent_of(self, i):
        return self.player_ent(i) if i < 2 else self.mob_ent(i)

    def tnt_ent(self, t, fuse=None):
        return {"uuid": self.tnt_uuid[t["id"]], "type": "minecraft:tnt", "pos": [round(float(v), 4) for v in t["pos"]], "yaw": 0.0, "pitch": 0.0,
                "fuse_ticks": int(round((t["fuse"] if fuse is None else fuse) * 20))}

    def spawn_item(self, t, k, pos, name):
        u = uuid_of(self.seed, self.eid, "item", self.seq)
        ent = {"uuid": u, "type": "minecraft:item", "pos": [round(float(v), 4) for v in pos], "yaw": 0.0, "pitch": 0.0, "item": "minecraft:" + name, "count": 1}
        self.items[u] = dict(ent=ent, k0=k)
        self.rec("entity_spawn", t, k, entity=ent, reason="ITEM")

    def sneaking(self, i, k):
        return bool(self.sneak[0] <= k < self.sneak[1] and i == 1)

    def food(self, k):
        return 20 - int(k * TICK_MS / 8000) if self.kind in ("pvp", "pve") else 20

    def crosshair(self, i, k, reach=MCW.REACH):
        """T16 crosshair (Mineflayer blockAtCursor / entityAtCursor within reach: 4.5 m = what the client outlines; engine real
        logs frame-recorder's 5 m cast, xh5) of bot i: {type block, pos, face, distance} | {type entity, uuid, distance} | None."""
        w = self.w; a = w.ag[i]
        if not a["alive"]:
            return None
        eye = a["pos"] + [0.0, 1.27 if self.sneaking(i, k) else 1.62, 0.0]; f = MCW.MC.forward(a["yaw"], a["pitch"])
        cell, n, tb = w.raycast(eye, f, reach)
        ag, tp = w._ray_player(eye, f, min(tb, reach), a)
        if ag is not None and tp < tb:
            j = w.ag.index(ag)
            return {"type": "entity", "uuid": uuid_of("player", BOTS[j]) if j < 2 else self.mobs[j]["uuid"], "distance": round(float(tp), 3)}
        return None if cell is None else {"type": "block", "pos": [int(v) for v in cell], "face": [int(v) for v in n], "distance": round(float(tb), 3)}

    def players_tick(self, k, t):
        out = []
        for i, b in enumerate(BOTS):
            a = self.w.ag[i]; y, p = mc_deg(a["yaw"], a["pitch"]); inv = self.inv[b]; n, c = inv["hb"][inv["slot"]]
            e = {"name": b, "world": "world", "pos": [round(float(v), 4) for v in a["pos"]], "yaw": y, "pitch": p, "sneaking": self.sneaking(i, k), "sprinting": False,
                 "on_ground": bool(a["on_ground"]), "health": round(max(float(a["hp"]), 0.0), 2) if a["alive"] else 0.0, "fire_ticks": 0, "held_slot": inv["slot"],
                 "held_item": "minecraft:" + n if n else "minecraft:air", "held_count": int(c) if n else 0}
            xh = self.crosshair(i, k)
            if self.t16 == "tick":                                               # T16 in the world log (else per frame, write_bots)
                by, _ = mc_deg(self.body[b], 0.0)
                e.update(food=self.food(k), saturation=5.0, body_yaw=by, head_yaw=y, crosshair=xh)
            elif self.engine == "real":                                          # WorldStateLogger.writePlayersTick: no crosshair / hotbar
                by, _ = mc_deg(self.body[b], 0.0); hr = self.raised(b)
                e.update(head_yaw=y, body_yaw=round(by + 360.0 * (i == 0), 3), food=self.food(k), saturation=5.0, dead=not a["alive"], gamemode="SURVIVAL",
                         flying=False, blocking=False, hand_raised=hr)          # getBodyYaw is not normalised
                self.hr_hist[b].append((int(t), hr))
            self.xh_hist[b].append((int(t), xh))
            out.append(e)
        if self.names == "alt":                                                  # T12 as the camera players' own entries (players= of worldstate begin)
            for i, b in enumerate(BOTS):
                a = self.w.ag[i]; y, p = mc_deg(a["yaw"], a["pitch"])
                out.append({"name": CAMS[i], "world": "world", "pos": [round(float(v), 4) for v in a["pos"]], "yaw": y, "pitch": p, "sneaking": False,
                            "sprinting": False, "on_ground": False, "health": 20.0, "fire_ticks": 0, "held_slot": 0, "held_item": "minecraft:air", "held_count": 0})
        return out

    def raised(self, b):
        """LivingEntity#isHandRaised of bot b now (Bravo eating)."""
        e = self.eat
        return bool(b == "Bravo" and e is not None and e.get("t0") is not None and e.get("t1") is None)

    def cameras_tick(self, k):
        """T12 tick "cameras" (WorldStateLogger.camerasJson): the server-side camera players = their bot's pose at the previous
        tick (the server-followed camera lags the pose the client renders with the pose link)."""
        out = []
        for i, b in enumerate(BOTS):
            a = self.w.ag[i]; p, yw, pt = self.lagp.get(b) or (a["pos"].copy(), a["yaw"], a["pitch"])
            y, q = mc_deg(yw, pt); ty, tq = mc_deg(a["yaw"], a["pitch"])
            out.append({"name": CAMS[i], "target": b, "world": "world", "pos": [round(float(v), 4) for v in p], "yaw": y, "pitch": q, "flying": True,
                        "gamemode": "SURVIVAL", "dyaw_to_target": round(wrap(math.radians(y - ty)) * 180 / math.pi, 3), "dpitch_to_target": round(q - tq, 3),
                        "dpos_to_target": round(float(np.linalg.norm(p - a["pos"])), 4), "last_sync_tick": self.S0 + k})
            self.lagp[b] = (a["pos"].copy(), a["yaw"], a["pitch"])
        return out

    def entities_real(self):
        """T3 "entities" record (WorldStateLogger.writeEntities / entityStateJson): every mob (yaw = head yaw, body_yaw 30 deg
        off) + the tracked players."""
        out = []
        for i, m in self.mobs.items():
            if m["state"] not in ("alive", "dead"):
                continue
            a = self.w.ag[i]; y, p = mc_deg(a["yaw"], a["pitch"]); by, _ = mc_deg(a["yaw"] - math.radians(30), 0.0)
            out.append({"uuid": m["uuid"], "id": 100 + i, "kind": "zombie", "type": "minecraft:zombie", "name": None, "x": round(float(a["pos"][0]), 4),
                        "y": round(float(a["pos"][1]), 4), "z": round(float(a["pos"][2]), 4), "yaw": y, "pitch": p, "head_yaw": y, "body_yaw": by, "vx": 0.0, "vy": 0.0,
                        "vz": 0.0, "bbox_w": SIZES["zombie"][0], "bbox_h": SIZES["zombie"][1], "health": round(max(float(a["hp"]), 0.0), 2) if m["state"] == "alive" else 0.0,
                        "max_health": 20.0, "alive": m["state"] == "alive", "on_ground": bool(a["on_ground"]), "sneaking": False, "sprinting": False, "blocking": False,
                        "hand_raised": False, "held_item": None, "fire_ticks": 0})
        for i, b in enumerate(BOTS):
            a = self.w.ag[i]; y, p = mc_deg(a["yaw"], a["pitch"]); inv = self.inv[b]; n, c = inv["hb"][inv["slot"]]
            out.append({"uuid": uuid_of("player", b), "id": 1 + i, "kind": "player", "type": "minecraft:player", "name": b, "x": round(float(a["pos"][0]), 4),
                        "y": round(float(a["pos"][1]), 4), "z": round(float(a["pos"][2]), 4), "yaw": y, "pitch": p, "head_yaw": y, "body_yaw": mc_deg(self.body[b], 0.0)[0],
                        "vx": 0.0, "vy": 0.0, "vz": 0.0, "bbox_w": 0.6, "bbox_h": 1.8, "health": round(max(float(a["hp"]), 0.0), 2) if a["alive"] else 0.0, "max_health": 20.0,
                        "alive": bool(a["alive"]), "on_ground": bool(a["on_ground"]), "sneaking": self.sneaking(i, self.k_now), "sprinting": False, "blocking": False,
                        "hand_raised": self.raised(b), "held_item": {"id": "minecraft:" + n, "count": int(c)} if n else None, "offhand_item": None, "held_slot": inv["slot"],
                        "gamemode": "SURVIVAL", "fire_ticks": 0})
        return out

    def tick_rec(self, k, t):
        """the tick record (+ the "entities" record of a real-engine pve episode)."""
        ex = {"mobs": self.mobs_tick()} if self.kind == "pve" and self.engine != "real" else {}
        if self.engine == "real":
            ex["cameras"] = self.cameras_tick(k)
        self.rec("tick", t, k, full_time=6000, time_of_day=6000, players=self.players_tick(k, t), **ex)
        if self.engine == "real" and self.kind == "pve":
            self.k_now = k; ents = self.entities_real()
            self.rec("entities", t, k, center=[0.5, -60.0, 0.5], radius=64, shape="sphere", count=len(ents), entities=ents)

    def mobs_tick(self):
        out = []
        for i, m in self.mobs.items():
            if m["state"] not in ("alive", "dead"):
                continue
            a = self.w.ag[i]; y, p = mc_deg(a["yaw"], a["pitch"])
            out.append({"uuid": m["uuid"], "type": "minecraft:zombie", "pos": [round(float(v), 4) for v in a["pos"]], "yaw": y, "pitch": p,
                        "body_yaw": y, "bbox": list(SIZES["zombie"]), "health": round(max(float(a["hp"]), 0.0), 2) if m["state"] == "alive" else 0.0,
                        "max_health": 20.0, "fire_ticks": 0, "on_ground": bool(a["on_ground"]), "held_item": "minecraft:air", "armor": [],
                        "target_uuid": uuid_of("player", BOTS[m["target"]]) if m["target"] is not None else None})
        return out

    # ---- inventory events -----------------------------------------------------------------------------------------
    def select(self, b, name, t, k, flags):
        inv = self.inv[b]
        j = next((j for j, (n, _) in enumerate(inv["hb"]) if n == name), None)
        if j is None:
            j = next((j for j, (n, _) in enumerate(inv["hb"]) if n is None), inv["slot"])
            inv["hb"][j] = [name, 64]
        if j != inv["slot"] and self.names == "real":                         # WorldStateLogger held_item_change
            pn, pc = inv["hb"][inv["slot"]]
            self.rec("held_item_change", t, k, player=b, previous_slot=inv["slot"], new_slot=j, previous_item={"id": "minecraft:" + pn, "count": int(pc)} if pn else None,
                     new_item={"id": "minecraft:" + name, "count": int(inv["hb"][j][1])})
        elif j != inv["slot"]:
            self.rec(self.nm["held"], t, k, player=b, **{"previous_slot" if self.names == "alt" else "old_slot": inv["slot"]}, new_slot=j, item="minecraft:" + name)
            inv["slot"] = j; flags["hotbar"] = j
        return j

    # ---- T15 / T17 records -------------------------------------------------------------------------------------------
    def swing(self, t, k, b):
        if self.names == "real":
            self.rec("arm_swing", t, k, player=b, animation="ARM_SWING", hand="HAND")
        elif self.names == "alt":
            self.rec(self.nm["swing"], t, k, player=b, hand="MAIN_HAND")
        else:
            self.rec("player_animation", t, k, player=b, animation="ARM_SWING")

    def dig(self, b, c, te, k, tool):
        """T17 for a block broken by b at tick k: start + crack stages 0..9 over the ticks since b's previous break (<= DIG_TICKS;
        written into those earlier ticks by run()'s merge), then the clear (stage -1) or finish record now."""
        k0 = max(self.last_break[b] + 1, k - DIG_TICKS, 0, self.set_k.get(tuple(c), -DIG_TICKS) + 1); self.last_break[b] = k; n = k - k0   # block visible
        if n >= 2:
            self.late.setdefault(k0, []).append((self.nm["dig_start"], int(self.tick_t[k0]) + EV_MS, dict(pos=list(c), player=b, item="minecraft:" + tool if tool else None)))
            for j in range(1, n):
                st = int(round(9 * j / (n - 1))); tj = int(self.tick_t[k0 + j]) + EV_MS
                self.late.setdefault(k0 + j, []).append((self.nm["dig_progress"], tj, dict(pos=list(c), player=b, stage=st)))
                if k0 + j in self.ctick[b]:                                      # (no client state on a hiccup tick)
                    fl = self.client[b][self.ctick[b][k0 + j]][1]["flags"]; fl["attack"] = fl["mine"] = True
                self.dig_hist[b].append((tj, tuple(int(v) for v in c), st))
        if self.nm["dig_finish"]:
            self.rec(self.nm["dig_finish"], te, k, pos=list(c), player=b)
        else:
            self.rec(self.nm["dig_progress"], te, k, pos=list(c), player=b, stage=-1)  # BLOCK_BREAK_ANIMATION clear
        self.dig_hist[b].append((te, None, None))

    def eat_tick(self, k, te, flags):
        """T15 use: Bravo eats (build / pvp) from k0 for EAT_TICKS, aborted when its held slot changes or it dies."""
        e, b = self.eat, "Bravo"
        if e is None:
            return
        if e.get("t0") is None:
            if k >= e["k0"] and self.w.ag[1]["alive"]:
                e["slot"] = self.select(b, e["item"], te, k, flags[b]); e["t0"] = te; flags[b]["use"] = True
                if self.names == "real":
                    self.rec("use_item_start", te, k, player=b, item={"id": "minecraft:" + e["item"], "count": 16}, hand="HAND", held_slot=e["slot"]); e["k_"] = k
                else:
                    self.rec(self.nm["use_start"], te, k, player=b, item="minecraft:" + e["item"], hand="HAND")
                self.use_hist[b].append([te, math.inf])
        elif e.get("t1") is None:
            if self.inv[b]["slot"] != e["slot"] or k >= e["k0"] + EAT_TICKS or not self.w.ag[1]["alive"]:
                if self.names == "real":                                         # item_consume when eaten up, then use_item_stop
                    done = k >= e["k0"] + EAT_TICKS and self.inv[b]["slot"] == e["slot"] and self.w.ag[1]["alive"]
                    it = {"id": "minecraft:" + e["item"], "count": 16}
                    if done:
                        self.rec("item_consume", te, k, player=b, item=it, hand="HAND")
                    self.rec("use_item_stop", te, k, player=b, item=it, hand="HAND", start_tick=self.S0 + e["k_"], ticks_used=k - e["k_"], used_time=k - e["k_"],
                             reason="stopped" if done or not self.w.ag[1]["alive"] else "item_changed")
                else:
                    self.rec(self.nm["use_end"], te, k, player=b, item="minecraft:" + e["item"])
                e["t1"] = te; self.use_hist[b][-1][1] = te
            else:
                flags[b]["use"] = True

    def merge_late(self):
        """T17 records of earlier ticks (dig) inserted at the end of their tick, then seq renumbered 0.. (file order = seq)."""
        if not self.late:
            return
        ks, out = sorted(self.late), []

        def flush(upto):
            while ks and ks[0] < upto:
                kk = ks.pop(0)
                out.extend({"type": ty, "seq": 0, "game_time": self.G0 + kk, "server_tick": self.S0 + kk, "epoch_ms": int(t_), **kw} for ty, t_, kw in self.late[kk])
        for r in self.recs:
            flush(r["game_time"] - self.G0); out.append(r)
        flush(math.inf)
        for i, r in enumerate(out):
            r["seq"] = i
        self.recs, self.seq, self.late = out, len(out), {}

    # ---- one server tick --------------------------------------------------------------------------------------------
    def tick(self, k):
        w, t = self.w, int(self.tick_t[k]); te = t + EV_MS
        flags = {b: {} for b in BOTS}
        # block_state readback of the previous tick's changes (start of this tick)
        for cell, prev, new in self.pending_bs:
            self.rec("block_state", t, k, pos=list(cell), block=new.split("[", 1)[0], state=new, prev_state=prev)
        self.pending_bs = []
        if self.on_visible is not None:
            self.on_visible(k)
        mobs = self.mobs_tick()
        self.mob_hist.append((t, sum(1 for m in mobs if m["health"] > 0)))
        self.tick_rec(k, t)
        for i, b in enumerate(BOTS):                                             # T12: camera teleported to the bot, its own location
            a = w.ag[i]; y, p = mc_deg(a["yaw"], a["pitch"]); h = CAM_EYE * (CAM_SNEAK if self.sneaking(i, k) else 1.0)
            if self.nm["camera"]:
                cp = a["pos"] + [0.0, h if self.cam_pos == "eye" else 0.0, 0.0]
                self.rec(self.nm["camera"], t, k, camera=CAMS[i], follows=b, pos=[round(float(v), 4) for v in cp], yaw=y, pitch=p)
            self.cam_hist[b].append((t, a["pos"] + [0.0, h, 0.0], a["yaw"] % TAU, float(a["pitch"])))
        for u, it in list(self.items.items()):                                  # item pickup (10-tick delay, 1 m)
            if k - it["k0"] >= 10 and any(w.ag[i]["alive"] and np.linalg.norm((w.ag[i]["pos"] - it["ent"]["pos"])[[0, 2]]) < 1.0
                                          and abs(w.ag[i]["pos"][1] - it["ent"]["pos"][1]) < 1.5 for i in range(2)):
                self.rec("entity_remove", te, k, entity=dict(it["ent"])); del self.items[u]
        for i, m in self.mobs.items():                                           # dead zombies leave after the 20-tick death animation
            if m["state"] == "dead" and k >= m["t1"]:
                self.rec("entity_remove", te, k, entity=self.mob_ent(i)); m["state"] = "gone"
        if k > self.K_sim:
            return
        if self.garden and self.prev is None and k == int(GARDEN_FLIP * self.K_sim):   # garden state flips (read back next tick)
            for dx, dy, _, s1 in GARDEN:
                c = (dx, GY + 1 + dy, GARDEN_Z)
                if s1:
                    self.pending_bs.append((c, self.state_at(c), s1)); self.sgrid[self.bidx(c)] = self.pal(s1); self.set_k[c] = k
            self.garden_t = int(self.tick_t[k + 1])
        # scripted world / player actions before the step
        prev_grid, prev_tnt = w.grid.copy(), {t_["id"]: dict(t_, pos=t_["pos"].copy()) for t_ in w.tnt}
        pre, acts, cause = [], {}, {}
        for i, m in self.mobs.items():
            if m["state"] == "pending" and k >= m["spawn_k"]:
                w.ag[i]["dead_t"] = 0.0; w._spawn(w.ag[i]); m["state"] = "alive"
                self.rec("entity_spawn", te, k, entity=self.mob_ent(i), reason="NATURAL")
        for _, cell in [f for f in self.fades if f[0] == k]:
            below = (cell[0], cell[1] - 1, cell[2])
            if w.block(below) == MCW.GRASS and w.block(cell) != MCW.AIR:
                w._set(below, MCW.DIRT); cause[below] = ("fade", None)
        if self.kind == "tnt":
            a = w.ag[0]
            if self.ign is None and k >= int(0.4 * self.K_sim):
                cells = [c for c, b_ in w.plans[0] if w.block(c) == MCW.TNT]
                if cells:
                    c = min(cells, key=lambda c: np.linalg.norm(np.asarray(c) + 0.5 - w.eye(a)))
                    a["yaw"], a["pitch"] = w._aim(a, np.asarray(c, float) + 0.5)
                    self.select("Alpha", "flint_and_steel", te, k, flags["Alpha"])
                    w._ignite(c, pre, 0); self.ign = k; self.note(te, k, bot="Alpha", phase="ignite", tnt=list(c))
            if self.ign is not None:
                acts[0] = Action(move=(-1.0, 0.0)) if k - self.ign < 50 else Action()
        if self.kind == "pve":
            for i, m in self.mobs.items():
                if m["state"] == "alive":
                    near = [j for j in range(2) if w.ag[j]["alive"]]
                    m["target"] = min(near, key=lambda j: np.linalg.norm(w.ag[j]["pos"] - w.ag[i]["pos"])) if near else None
        before = {i: (w.ag[i]["pos"].copy(), bool(w.ag[i]["alive"]), bool(w.ag[i]["on_ground"])) for i in range(2)}
        ev = pre + w.step(acts)
        # ---- translate mc.py events -------------------------------------------------------------------------------
        gone = [t_ for i_, t_ in prev_tnt.items() if i_ not in {x["id"] for x in w.tnt}]
        new_tnt = [t_ for t_ in w.tnt if t_["id"] not in prev_tnt]
        diff = [tuple(int(v) for v in c + w.origin) for c in np.argwhere(w.grid != prev_grid)]
        for e in ev:
            ty = e["type"]
            if ty == "place":
                cause[tuple(e["cell"])] = ("place", e["by"])
            elif ty == "break":
                cause[tuple(e["cell"])] = ("break", e["by"])
            elif ty == "ignite":
                cause[tuple(e["cell"])] = ("ignite", e["by"])
        expl = [e for e in ev if e["type"] == "explode"]
        ex_cells = {j: [] for j in range(len(expl))}
        for c in diff:
            if c in cause:
                continue
            for j, e in enumerate(expl):
                if np.linalg.norm(np.asarray(c) - np.floor(e["at"])) <= 3.0 + 1e-6:
                    cause[c] = ("explode", j); ex_cells[j].append(c); break
            else:
                cause[c] = ("other", None)
        for e in ev:
            ty = e["type"]
            if ty in ("place", "break", "ignite") and e["by"] is not None and 0 <= e["by"] < 2:
                b = BOTS[e["by"]]; c = tuple(e["cell"])
                if ty == "place":
                    st = self.state_of(c, w.block(c)); self.select(b, bare(st), te, k, flags[b])
                    self.swing(te, k, b)
                    below = (c[0], c[1] - 1, c[2]); ag = below if w.block(below) != MCW.AIR else next(
                        (q for q in ((c[0] + 1, c[1], c[2]), (c[0] - 1, c[1], c[2]), (c[0], c[1], c[2] + 1), (c[0], c[1], c[2] - 1), (c[0], c[1] + 1, c[2]))
                         if w.block(q) != MCW.AIR), below)
                    self.rec("block_place", te, k, pos=list(c), old_type="minecraft:air", old_state="minecraft:air", new_type=st.split("[", 1)[0],
                             new_state=st, player=b, item="minecraft:" + bare(st), against=list(ag), multi=False)
                    inv = self.inv[b]; inv["hb"][inv["slot"]][1] = max(1, inv["hb"][inv["slot"]][1] - 1)
                    flags[b]["place_block"] = True
                    if w.block(below) == MCW.GRASS:
                        self.fades.append((k + int(self.rng.integers(20, 60)), c))
                elif ty == "break":
                    old = self.state_at(c); inv = self.inv[b]; tool = inv["hb"][inv["slot"]][0]
                    self.swing(te, k, b)
                    self.rec("block_break", te, k, pos=list(c), old_type=old.split("[", 1)[0], old_state=old, new_type=None, new_state=None, player=b,
                             tool="minecraft:" + tool if tool else None, drop_items=True)
                    self.dig(b, c, te, k, tool)
                    self.spawn_item(te, k, np.asarray(c, float) + [0.5, 0.25, 0.5], bare(old))
                    flags[b]["attack"] = flags[b]["mine"] = True
                else:
                    old = self.state_at(c)
                    self.swing(te, k, b)
                    self.rec("player_interact", te, k, player=b, action="RIGHT_CLICK_BLOCK", pos=list(c), state=old, face="UP",
                             item="minecraft:flint_and_steel", cancelled=False)
                    self.rec("tnt_prime", te, k, pos=list(c), old_type=old.split("[", 1)[0], old_state=old, new_type=None, new_state=None,
                             cause="PLAYER", player=b, source=None)
                    flags[b]["use"] = True
        for t_ in new_tnt:                                                       # primed TNT entities (ignited or chained)
            self.tnt_uuid[t_["id"]] = uuid_of(self.seed, self.eid, "tnt", t_["id"])
            c = tuple(int(v) for v in np.floor(t_["pos"]))
            if cause.get(c, ("",))[0] != "ignite":
                old = STATE12[MCW.TNT]
                self.rec("tnt_prime", te, k, pos=list(c), old_type="minecraft:tnt", old_state=old, new_type=None, new_state=None, cause="EXPLOSION",
                         entity_type="minecraft:tnt", source=None)
            self.rec("entity_spawn", te, k, entity=self.tnt_ent(t_), reason=None)
        src = None
        for j, e in enumerate(expl):
            src = min(gone, key=lambda t_: np.linalg.norm(t_["pos"] + [0, 0.5, 0] - e["at"])) if gone else None
            blocks = [{"pos": list(c), "old_type": self.state_at(c).split("[", 1)[0], "old_state": self.state_at(c)} for c in ex_cells[j]]
            self.rec("entity_explode", te, k, center=[round(float(v), 4) for v in e["at"]], **{"yield": 1.0}, blocks=blocks, entity_type="minecraft:tnt",
                     entity_uuid=self.tnt_uuid.get(src["id"]) if src else None)
            for bl in blocks:
                if bl["old_type"] != "minecraft:tnt":
                    self.spawn_item(te, k, np.asarray(bl["pos"], float) + [0.5, 0.25, 0.5], bare(bl["old_state"]))
            if src is not None:
                self.rec("entity_remove", te, k, entity=self.tnt_ent(src, fuse=0.0))
                gone.remove(src)
        for e in ev:                                                            # damage, deaths, respawns
            ty = e["type"]
            if ty == "hit":
                vi, by = e["victim"], e["by"]
                hp = max(float(e["hp"]), 0.0)
                if e.get("cause") == "explosion":
                    dmg_by, cause_s = (self.tnt_ent(src, 0.0) if src is not None else None), "ENTITY_EXPLOSION"
                else:
                    dmg_by, cause_s = self.ent_of(by), "ENTITY_ATTACK"
                    if by < 2:
                        self.swing(te, k, BOTS[by]); flags[BOTS[by]]["attack"] = True
                kb = (w.ag[vi]["pos"] - w.ag[by]["pos"]) if by is not None and by >= 0 and e.get("cause") != "explosion" else np.zeros(3)
                kb[1] = 0; kb = kb / (np.linalg.norm(kb) + 1e-9) * 0.4; kb[1] = 0.36
                prev_hp = self.hp_hist[BOTS[vi]][-1][1] if vi < 2 else None
                dmg = round((prev_hp - hp) if vi < 2 and prev_hp is not None else 6.0, 3)
                if self.names == "real":                                        # the capture plugin's WorldStateLogger.onDamage / onKnockback
                    ref = lambda x: None if x is None else {"uuid": x["uuid"], "type": x["type"], "name": x.get("name")}  # noqa: E731
                    self.rec("entity_damage", te, k, victim=ref(self.ent_of(vi)), cause=cause_s, damage_type="minecraft:" + ("explosion" if src is not None and cause_s == "ENTITY_EXPLOSION" else "player_attack" if by is not None and 0 <= by < 2 else "mob_attack"),
                             damage=dmg, final_damage=dmg, health_before=round(prev_hp if vi < 2 and prev_hp is not None else hp + dmg, 2), health_after=round(hp, 2),
                             attacker=ref(dmg_by), projectile=None, causing_entity=ref(dmg_by), victim_blocking=False)
                    if dmg_by is not None and cause_s == "ENTITY_ATTACK":
                        self.rec("entity_knockback", te, k, entity=ref(self.ent_of(vi)), cause="ENTITY_ATTACK", attacker=ref(dmg_by), vector=[round(float(v), 4) for v in kb], strength=0.4)
                elif self.names == "alt":                                       # WorldStateLogger.actor() fields for the damager
                    act = {} if dmg_by is None else dict(entity_type=dmg_by["type"], entity_uuid=dmg_by["uuid"], **({"player": dmg_by["name"]} if dmg_by.get("name") else {}))
                    self.rec(self.nm["damage"], te, k, entity=self.ent_of(vi), **act, damage=dmg, final_damage=dmg, cause=cause_s,
                             knockback=[round(float(v), 4) for v in kb], health_after=round(hp, 2))
                else:
                    self.rec("entity_damage", te, k, entity=self.ent_of(vi), damager=dmg_by, damage=dmg, final_damage=dmg, cause=cause_s,
                             knockback=[round(float(v), 4) for v in kb], health_after=round(hp, 2))
                if vi < 2:
                    self.hp_hist[BOTS[vi]].append((te, hp))
            elif ty == "kill":
                vi, by = e["victim"], e["by"]
                cs = {"explosion": "ENTITY_EXPLOSION", "void": "VOID"}.get(e.get("cause"), "ENTITY_ATTACK")
                killer = BOTS[by] if by is not None and 0 <= by < 2 else None
                if self.names == "real":
                    ke = self.ent_of(by) if by is not None and 0 <= by < len(w.ag) and cs == "ENTITY_ATTACK" else None
                    self.rec("entity_death", te, k, entity=self.ent_of(vi), victim={"uuid": self.ent_of(vi)["uuid"], "type": self.ent_of(vi)["type"], "name": self.ent_of(vi).get("name")},
                             victim_name=self.ent_of(vi).get("name"), cause=cs, killer=killer, killer_entity=None if ke is None else {"uuid": ke["uuid"], "type": ke["type"], "name": ke.get("name")},
                             killer_name=None if ke is None else ke.get("name"))
                else:
                    self.rec("entity_death", te, k, entity=self.ent_of(vi), cause=cs, killer=killer)
                if vi < 2:
                    self.hp_hist[BOTS[vi]].append((te, 0.0)); self.dead_bots[BOTS[vi]] += 1
                else:
                    m = self.mobs[vi]; m["state"], m["t1"] = "dead", k + 20; w.ag[vi]["dead_t"] = math.inf
                    self.spawn_item(te, k, w.ag[vi]["pos"] + [0, 0.25, 0], "rotten_flesh")
            elif ty == "respawn" and e["who"] < 2:
                b = BOTS[e["who"]]; a = w.ag[e["who"]]
                if self.names == "real":                                         # no respawn record: tick dead -> false from the next tick record
                    self.hp_hist[b].append((int(self.tick_t[k + 1]), 20.0))
                else:
                    self.rec("player_respawn", te, k, player=b, pos=[round(float(v), 4) for v in a["pos"]], health=20.0)
                    self.hp_hist[b].append((te, 20.0))
        for i, m in self.mobs.items():                                           # no zombie respawns
            if not w.ag[i]["alive"]:
                w.ag[i]["dead_t"] = math.inf
        self.eat_tick(k, te, flags)
        # block_state for every changed voxel (read back at the start of the next tick)
        for c in diff:
            if not all(0 <= q < n for q, n in zip(self.bidx(c), BOX_SIZE)):
                continue
            prev = self.state_at(c); new = self.state_of(c, w.block(c)) if w.block(c) != MCW.AIR else "minecraft:air"
            if cause.get(c, ("",))[0] == "fade":
                self.rec("block_fade", te, k, pos=list(c), old_type=prev.split("[", 1)[0], old_state=prev, new_type=new.split("[", 1)[0], new_state=new)
            if new != prev:
                self.sgrid[self.bidx(c)] = self.pal(new); self.pending_bs.append((c, prev, new)); self.set_k[tuple(c)] = k
        # body yaw and the bot client state after the step (what the action recorder samples)
        for i, b in enumerate(BOTS):
            a = w.ag[i]; d = wrap(a["yaw"] - self.body[b]); moved = np.linalg.norm((a["pos"] - before[i][0])[[0, 2]]) > 1e-3
            self.body[b] = a["yaw"] - math.copysign(math.radians(50), d) if abs(d) > math.radians(50) else self.body[b] + (0.3 * d if moved else 0.0)
            dp = a["pos"] - before[i][0]; f = np.array([-math.sin(a["yaw"]), 0, -math.cos(a["yaw"])]); r = np.array([math.cos(a["yaw"]), 0, -math.sin(a["yaw"])])
            fl = flags[b]; v = dp / (TICK_MS / 1000.0)
            fl.update(forward=v @ f > 1.0, back=v @ f < -1.0, right=v @ r > 1.0, left=v @ r < -1.0, jump=bool(before[i][2] and dp[1] > 0.05))
            inv = self.inv[b]
            if self.engine == "real" and k > 20 and (k + 23 * i) % 50 == 25:     # controller event-loop hiccup: no pose packet this tick (100 ms gap)
                continue
            self.ctick[b][k] = len(self.client[b])
            self.client[b].append((te, dict(pos=a["pos"].copy(), yaw=a["yaw"] % TAU, pitch=float(a["pitch"]), slot=inv["slot"],
                                            hb=[list(x) for x in inv["hb"]], sneak=self.sneaking(i, k), flags=fl, body=self.body[b] % TAU, food=self.food(k),
                                            xh=self.crosshair(i, k), xh5=self.crosshair(i, k, 5.0) if self.engine == "real" else None)))

    # ---- whole episode ------------------------------------------------------------------------------------------------
    def run(self):
        self.setup()
        w = self.w
        self.pending_bs, self.cam_hist, self.mob_hist, self.on_visible = [], {b: [] for b in BOTS}, [], None
        self.nm, self.late, self.last_break, self.set_k = NAMES[self.names], {}, {b: -1 for b in BOTS}, {}
        self.dig_hist, self.use_hist, self.xh_hist = {b: [] for b in BOTS}, {b: [] for b in BOTS}, {b: [] for b in BOTS}
        self.hr_hist, self.lagp, self.ctick = {b: [] for b in BOTS}, {}, {b: {} for b in BOTS}
        self.eat = dict(k0=int(0.55 * self.K_sim), item=next((n for n, _ in self.inv["Bravo"]["hb"] if n in ("bread", "cooked_beef")), "bread")) \
            if self.kind in ("build", "pvp") else None
        self.client = {b: [(-math.inf, dict(pos=w.ag[i]["pos"].copy(), yaw=w.ag[i]["yaw"] % TAU, pitch=float(w.ag[i]["pitch"]), slot=0,
                                            hb=[list(x) for x in self.inv[b]["hb"]], sneak=False, flags={}, body=self.body[b] % TAU, food=self.food(0),
                                            xh=self.crosshair(i, 0), xh5=self.crosshair(i, 0, 5.0) if self.engine == "real" else None))] for i, b in enumerate(BOTS)}
        start_cap = self.cap_grid()
        start_ents = self.snapshot_entities(start=True)
        t0 = int(self.tick_t[0])
        if self.prev is not None:                                               # session bookkeeping (T20: persistent worlds)
            self.session = dict(id=self.session.get("id") or uuid_of("session", self.seed, self.eid), index=int(self.session.get("index", -1)) + 1,
                                revisit_of=int(self.prev["hdr"]["episode_id"]))
        else:
            self.session = dict(id=uuid_of("session", self.seed, self.eid), index=0, revisit_of=None)
        self.persistent = self.prev is not None or self.persist_next
        files = dict(events=f"{self.stem}_world_events.jsonl", start=f"{self.stem}_world_snapshot_start.json.gz", end=f"{self.stem}_world_snapshot_end.json.gz")
        box = {"min": BOX_MIN.tolist(), "max": (BOX_MIN + np.array(BOX_SIZE) - 1).tolist()}
        self.world = {"name": "world", "uid": uuid_of("world", self.seed), "seed": self.world_seed, "seed_str": str(self.world_seed), "environment": "NORMAL",
                      "world_type": "FLAT", "generate_structures": False, "min_height": -64, "max_height": 320, "difficulty": "NORMAL", "storm": False,
                      "thundering": False, "full_time": 6000, "time_of_day": 6000, "spawn": [0, -60, 0],
                      "gamerules": {"doDaylightCycle": "false", "doWeatherCycle": "false", "keepInventory": "true", "naturalRegeneration": "false",
                                    "randomTickSpeed": "3", "doMobSpawning": "false"}}
        self.rec("episode_begin", t0, 0, format="solaris-world-events-v1", stem=self.stem, episode_id=self.eid, instance_id=self.inst,
                 episode_type=self.cfg["type"], events_file=files["events"], world=self.world, box=box, center=[0, -60, 0], log_radius=64, heartbeat_ticks=1,
                 tracked_players=list(BOTS), snapshot={"mode": "json", "start_file": files["start"], "end_file": files["end"], "region_files": []},
                 server_version="1.21-R0.1-SNAPSHOT", bukkit_version="1.21-R0.1-SNAPSHOT", plugin_version="1.4.0-stateful")
        self.write_snapshot("start", files["start"], t0, 0, start_ents)
        self.note(t0 + 3, 0, bot="Alpha", phase="start", kind=self.kind)
        for i, m in self.mobs.items():
            if m["state"] == "alive":
                self.rec("entity_spawn", t0 + 5, 0, entity=self.mob_ent(i), reason="SPAWNER_EGG")
        ck_t = {b: self.frames[b][np.linspace(0, len(self.frames[b]) - 1, 4).astype(int)] for b in BOTS}
        ck_all = np.unique(np.concatenate(list(ck_t.values())))
        ck_grid, ck_i = [], [0]

        def visible(k):                                                         # grid visible during [t_k, t_k+1) (block_state rule)
            tn = self.tick_t[k + 1] if k < self.K_total else math.inf
            while ck_i[0] < len(ck_all) and ck_all[ck_i[0]] < tn:
                assert ck_all[ck_i[0]] >= self.tick_t[k]
                ck_grid.append(self.cap_grid()); ck_i[0] += 1
        self.on_visible = visible
        for k in range(self.K_total + 1):
            self.tick(k)
        assert len(ck_grid) == len(ck_all)
        assert not self.pending_bs, "block changes after the last tick"
        self.merge_late()
        tk = int(self.tick_t[self.K_total]) + 20
        for i, m in self.mobs.items():                                           # teardown: mobs removed, items stay
            if m["state"] in ("alive", "dead"):
                self.rec("entity_remove", tk, self.K_total, entity=self.mob_ent(i)); m["state"] = "gone"
        self.tick_rec(self.K_total, tk + 1)
        end_ents = self.snapshot_entities(start=False)
        n_logged = self.seq + 1
        changed = int((self.cap_grid() != start_cap).sum())
        self.rec("episode_end", tk + 3, self.K_total, stem=self.stem, episode_id=self.eid, reason="command", events_logged=n_logged,
                 snapshot_end_file=files["end"], restored=not self.persistent, restored_blocks=0 if self.persistent else changed, removed_entities=0)
        self.write_snapshot("end", files["end"], tk + 3, self.K_total, end_ents)
        os.makedirs(self.out, exist_ok=True)
        with open(os.path.join(self.out, files["events"]) + ".part", "w", encoding="utf-8") as fh:
            for r in self.recs:
                fh.write(json.dumps(r, separators=(",", ":")) + "\n")
        os.replace(os.path.join(self.out, files["events"]) + ".part", os.path.join(self.out, files["events"]))
        truth = self.write_bots(files, t0, tk + 3, n_logged, changed)
        tp = os.path.join(self.out_root, "synth_truth", self.stem + ".npz")
        os.makedirs(os.path.dirname(tp), exist_ok=True)
        if self.garden_t is not None:
            truth.update(garden_cells=np.array([(dx, GY + 1 + dy, GARDEN_Z) for dx, dy, _, _ in GARDEN], np.int64), garden_s0=np.array([g[2] for g in GARDEN]),
                         garden_s1=np.array([g[3] or "" for g in GARDEN]), garden_t=np.int64(self.garden_t))
        np.savez_compressed(tp, origin=BOX_MIN, size=np.array(BOX_SIZE), start=start_cap, final=self.cap_grid(), ckpt_t=ck_all,
                            ckpt=np.stack(ck_grid).astype(np.uint16), kind=np.array(self.kind), eid=self.eid, inst=self.inst, **truth)
        return dict(stem=self.stem, out=self.out, events=os.path.join(self.out, files["events"]), snap_start=os.path.join(self.out, files["start"]),
                    snap_end=os.path.join(self.out, files["end"]), truth=tp, actions={b: os.path.join(self.out, self.astem[b] + ".json") for b in BOTS},
                    kind=self.kind, eid=self.eid, session=self.session, deaths=dict(self.dead_bots), records=self.seq)

    def snapshot_entities(self, start):
        w, out = self.w, []
        for i in range(2):
            out.append(self.player_ent(i))
            a = w.ag[i]; y, p = mc_deg(a["yaw"], a["pitch"])
            out.append({"uuid": uuid_of("camera", CAMS[i]), "type": "minecraft:player", "pos": [round(float(v), 4) for v in a["pos"]], "yaw": y, "pitch": p, "name": CAMS[i]})
        if start:
            out += [self.mob_ent(i) for i, m in self.mobs.items() if m["state"] == "alive"]
            out += [dict(e) for e in (self.prev["hdr"].get("entities") or []) if e.get("type") == "minecraft:item"] if self.prev is not None else []
            for e in out:
                if e.get("type") == "minecraft:item":
                    self.items[e["uuid"]] = dict(ent=e, k0=-100)
        else:
            out += [dict(it["ent"]) for it in self.items.values()]
        return out

    def write_snapshot(self, which, fname, t, k, ents):
        palette, rle = encode_snapshot(self.sgrid, self.palette)
        sx, sz = BOX_SIZE[0], BOX_SIZE[2]
        s = {"format": "solaris-block-snapshot-v1", "which": which, "stem": self.stem, "episode_id": self.eid, "world": "world", "seed": self.world_seed,
             "game_time": self.G0 + k, "full_time": 6000, "server_tick": self.S0 + k, "epoch_ms": int(t), "origin": BOX_MIN.tolist(), "size": list(BOX_SIZE),
             "order": "yzx", "palette": palette, "rle": rle, "biome_y": -60, "biome_palette": ["minecraft:plains"], "biomes_rle": [sx * sz, 0], "entities": ents}
        if which == "end":
            s["_session"] = dict(self.session)                                  # synth-only: lets the next persistent episode continue the session
        p = os.path.join(self.out, fname); os.makedirs(self.out, exist_ok=True)
        with open(p + ".part", "wb") as raw, gzip.GzipFile(fileobj=raw, mode="wb", mtime=0) as gz:   # mtime 0: byte-deterministic
            gz.write(json.dumps(s, separators=(",", ":")).encode("utf-8"))
        os.replace(p + ".part", p)

    def write_bots(self, files, t0, t_end, n_logged, changed):
        w, truth, real = self.w, {}, self.engine == "real"
        skins = {}
        for i, b in enumerate(BOTS):
            if real:                                                             # skins.js describe(): a pool PNG per skin_id, sha256 of its bytes
                q = 2 * self.eid + i; sid = f"proc_{q:04d}"; data = skin_png(q); model = "slim" if q % 3 == 2 else "classic"; sha = hashlib.sha256(data).hexdigest()
                pp = os.path.join(self.skin_pool, sid + ".png"); os.makedirs(self.skin_pool, exist_ok=True)
                if not os.path.exists(pp):
                    with open(pp, "wb") as fh:
                        fh.write(data)
                skins[b] = (q, {"skin_id": sid, "model": model, "source": "procedural", "sha256": sha, "texture_hash": "local-" + sha, "seed": 20251023 * 100003 + q,
                                "file": f"pool/{sid}.png", "reference": f"reference/{sid}.png", "tags": [f"model:{model}", "procedural"], "texture_mode": "local",
                                "camera": CAMS[i]}, data)
                continue
            k_ = int(self.rng.integers(0, len(SKIN_POOL)))
            k_ = (skins["Alpha"][0] + 1 + k_ % (len(SKIN_POOL) - 1)) % len(SKIN_POOL) if i else k_
            data = skin_png(k_); sha = hashlib.sha1(data).hexdigest()
            sp = os.path.join(self.out, "skins", sha + ".png"); os.makedirs(os.path.dirname(sp), exist_ok=True)
            if not os.path.exists(sp):
                with open(sp, "wb") as fh:
                    fh.write(data)
            skins[b] = (k_, {"file": f"synth_skin_{k_:02d}.png", "sha1": sha, "model": "slim" if k_ % 3 == 2 else "classic", "slim": k_ % 3 == 2,
                             "applied_epoch_ms": t0 + 150 + 40 * i})
        for i, b in enumerate(BOTS):
            r = self.frames[b]; C = self.client[b]; ct = np.array([c[0] for c in C]); acts = []
            hh = self.hp_hist[b]; ht = np.array([h[0] for h in hh]); cam = self.cam_hist[b]; camt = np.array([c[0] for c in cam])
            H, E, Y, P, U, TG, DG, HBN, HBC, BY = [], [], [], [], [], [], [], [], [], []
            xt = np.array([x_[0] for x_ in self.xh_hist[b]]); dgh = sorted(self.dig_hist[b], key=lambda d_: d_[0]); dgt = np.array([d_[0] for d_ in dgh])
            mt = np.array([m_[0] for m_ in self.mob_hist]); mc_ = np.array([m_[1] for m_ in self.mob_hist])
            MOB = mc_[np.searchsorted(mt, r, side="right") - 1]
            o = BOTS[1 - i]; Co = self.client[o]; cto = np.array([c[0] for c in Co]); hrt = np.array([h_[0] for h_ in self.hr_hist[b]])
            prev_j, prev_yaw = 0, None
            for n, rt in enumerate(r):
                j = int(np.searchsorted(ct, rt, side="right")) - 1; s = C[j][1]
                hpv = hh[int(np.searchsorted(ht, rt, side="right")) - 1][1]
                fl = {}
                for q in range(prev_j + 1, j + 1):
                    for kk, v in C[q][1]["flags"].items():
                        fl[kk] = fl.get(kk) or v
                prev_j = j
                yaw, pitch = (None, None) if n == 0 else (round(s["yaw"], 5) % TAU, round(s["pitch"], 5))
                yd, pd = mc_deg(yaw, pitch) if yaw is not None else (None, None)
                cam_d = [0.0, 0.0] if yaw is None or prev_yaw is None else [round(wrap(yaw - prev_yaw[0]), 5), round(pitch - prev_yaw[1], 5)]
                prev_yaw = (yaw, pitch) if yaw is not None else prev_yaw
                hs = fl.get("hotbar")
                action = {"forward": bool(fl.get("forward")), "back": bool(fl.get("back")), "left": bool(fl.get("left")), "right": bool(fl.get("right")),
                          "jump": bool(fl.get("jump")), "sprint": False, "sneak": s["sneak"], "camera": cam_d, "attack": bool(fl.get("attack")),
                          "use": bool(fl.get("use")), "mount": False, "dismount": False, "place_block": bool(fl.get("place_block")), "place_entity": False,
                          "mine": bool(fl.get("mine")), **{f"hotbar.{q + 1}": hs == q for q in range(9)}}
                hb = [item_obj(nm, c_, 36 + q) for q, (nm, c_) in enumerate(s["hb"])]
                kt = int(np.searchsorted(self.tick_t, rt, side="right")) - 1
                rtime = int(rt) if self.render_time == "epoch" else float(5000 + rt - r[0])  # relative: act_recorder then derives epochTime
                acts.append({"x": round(float(s["pos"][0]), 3), "y": round(float(s["pos"][1]), 3), "z": round(float(s["pos"][2]), 3), "yaw": yaw, "pitch": pitch,
                             "action": action, "inventory": {"quickBarSlot": s["slot"], "hotbar": hb, "mainInventory": [None] * 27, "heldItem": hb[s["slot"]],
                                                             "offHandItem": None},
                             "renderTime": rtime, "physicsTime": round(5_000_000.0 + 50.0 * kt + 0.123, 3), "worldAgeTicks": self.G0 + 20 * max((kt - 2) // 20, 0),
                             "isInWater": False, "frame_count": n, "episode_id": self.eid, "relativeTimeMs": float(rt - r[0]), "epochTime": rt / 1000.0,
                             "yaw_mc_deg": yd, "pitch_mc_deg": pd,
                             "extra_info": {"seed": self.world_seed, "seed_config": f"synth{self.seed}", "world_type": "flat", "level_type": "minecraft:flat"}})
                if self.t16 == "action":                                          # T16 per frame (Mineflayer radians like yaw)
                    acts[-1].update(food=s["food"], saturation=5.0, body_yaw=round(s["body"], 5), head_yaw=round(s["yaw"], 5), crosshair=s["xh"])
                if real:                                                          # frame-recorder.js: pose_seq + T16 nested in "state" + "peer"
                    so = Co[max(int(np.searchsorted(cto, rt, side="right")) - 1, 0)][1]
                    acts[-1].update(pose_seq=1000 + j, state={
                        "health": hpv, "food": s["food"], "saturation": 5.0, "xp_level": 0, "xp_progress": 0.0, "oxygen": 20, "effects": [],
                        "hotbar": [{"slot": q, "id": "minecraft:" + nm if nm else None, "count": int(c_) if nm else 0} for q, (nm, c_) in enumerate(s["hb"])],
                        "selected_slot": s["slot"], "offhand": None, "armor": {"head": None, "chest": None, "legs": None, "feet": None}, "on_ground": True,
                        "sneaking": s["sneak"], "sprinting": False, "flying": False, "in_water": False, "alive": hpv > 0, "head_yaw": round(s["yaw"] % TAU, 5),
                        "body_yaw": None, "cursor": self.cursor_of(s["xh5"]), "cursor_at_action": None},
                        peer={"name": o, "known": True, "pos": [round(float(v), 4) for v in so["pos"]], "yaw": round(so["yaw"], 5), "pitch": round(so["pitch"], 5),
                              "head_yaw": round(so["yaw"], 5), "distance": round(float(np.linalg.norm(so["pos"] - s["pos"])), 3)})
                xh = s["xh"] if self.t16 in ("action", "state") else self.xh_hist[b][max(int(np.searchsorted(xt, rt, side="right")) - 1, 0)][1]
                TG.append(xh["pos"] if xh and xh["type"] == "block" else [-1, -1, -1])
                jd = int(np.searchsorted(dgt, rt, side="right")) - 1
                DG.append(list(dgh[jd][1]) + [dgh[jd][2]] if jd >= 0 and dgh[jd][1] is not None and rt - dgh[jd][0] < 1000 else [0, 0, 0, -1])
                U.append(self.hr_hist[b][int(np.searchsorted(hrt, rt, side="right")) - 1][1] if real else any(u0 <= rt < u1 for u0, u1 in self.use_hist[b]))
                BY.append(s["body"]); HBN.append([nm_ or "" for nm_, _ in s["hb"]]); HBC.append([int(c_) if nm_ else 0 for nm_, c_ in s["hb"]])
                H.append(hpv)
                if real:                                                          # pose link: the camera renders the entry's packet (eye 1.62 / 1.27)
                    E.append(np.array([acts[-1]["x"], acts[-1]["y"], acts[-1]["z"]]) + [0.0, 1.27 if s["sneak"] else 1.62, 0.0]); Y.append(yaw); P.append(pitch)
                else:
                    c = cam[int(np.searchsorted(camt, rt, side="right")) - 1]
                    E.append(c[1]); Y.append(c[2]); P.append(c[3])
            if real:                                                              # null first yaw: filled from the first valid entry
                fv = next(q for q, v in enumerate(Y) if v is not None)
                Y = [Y[fv] if v is None else v for v in Y]; P = [P[fv] if v is None else v for v in P]
            stem = self.astem[b]
            write_json(os.path.join(self.out, stem + ".json"), acts)
            real_fps = len(r) / ((r[-1] - r[0]) / 1000.0)
            write_json(os.path.join(self.out, stem + "_meta.json"), {"episode_start_epoch": r[0] / 1000.0, "first_render_time_ms": float(acts[0]["renderTime"]),
                                                                     "render_time_is_epoch": self.render_time == "epoch", "frames": len(r), "video_fps": min(real_fps, 20.0),
                                                                     "episode_id": self.eid})
            sm = {"episode": self.eid, "site": list(w.sites[0]) if i == 0 else list(w.sites[1]), "axis": "x", "side": "north", "actor": "Alpha", "observer": "Bravo",
                  "observer_view": "side", "observer_distance": round(float(np.linalg.norm(w.ag[0]["pos"] - w.ag[1]["pos"])), 2), "difficulty": "normal",
                  "roles": {bb: w.ag[j]["bot"] for j, bb in enumerate(BOTS)}}
            if self.kind == "tnt":
                sm.update(layout="platform2x2", tnt=[list(c) for c, _ in w.plans[0]], retreat_distance=10, inspect_crater=False)
            if self.kind in ("break", "build"):
                sm.update(template="house+tower", blocks=[{"pos": list(c), "material": self.state_of(c, m)} for c, m in w.plans[1]][:40], breaker="Alpha" if self.kind == "break" else None,
                          break_indices=[])
            if self.session.get("revisit_of") is not None:
                sm["revisit_of"] = self.session["revisit_of"]
            info = {"timestamp": self.ts[b], "bot_name": b, "world_type": "flat", "episode_number": self.eid, "episode_type": self.cfg["type"], "instance_id": self.inst,
                    "encountered_error": False, "peer_encountered_error": False, "bot_died": self.dead_bots[b] > 0, "episode_recording_started": True, "eval_metadata": {},
                    "world": {"seed": str(self.world_seed), "seed_config": f"synth{self.seed}", "seed_value_expected": self.world_seed, "world_type": "flat",
                              "level_type": "minecraft:flat", "generator_settings": {"layers": [{"block": "minecraft:bedrock", "height": 1}, {"block": "minecraft:dirt", "height": 2},
                                                                                              {"block": "minecraft:grass_block", "height": 1}], "biome": "minecraft:plains"},
                              "flat_biome": "plains", "flat_surface_block": "grass_block", "generate_structures": False, "server_version": "1.21", "server_type": "PAPER"},
                    "world_state_leader": b == "Alpha",
                    "world_state": {"stem": self.stem, "events_file": files["events"], "snapshot_start_file": files["start"], "region_files": [], "seed": self.world_seed,
                                    "seed_str": str(self.world_seed), "world_type": "FLAT", "generate_structures": False, "game_time": self.G0, "server_tick": self.S0,
                                    "epoch_ms": t0, "box": {"min": BOX_MIN.tolist(), "max": (BOX_MIN + np.array(BOX_SIZE) - 1).tolist()}},
                    "world_state_end": {"events_logged": n_logged, "snapshot_end_file": files["end"], "restored": not self.persistent,
                                        "restored_blocks": 0 if self.persistent else changed, "removed_entities": 0,
                                        "game_time": self.G0 + self.K_total, "epoch_ms": t_end},
                    "reset_world_between_episodes": not self.persistent,
                    "state_metadata": sm, "angle_convention": "yaw/pitch: Mineflayer radians (yaw 0 = north/-Z, +pi/2 = west/-X; pitch + = up); *_mc_deg and the "
                                                              "world log: Minecraft degrees (yaw 0 = south/+Z, 90 = west; pitch +90 = down)",
                    "skin": skins[b][1]}
            if self.persistent:
                info.update(session_id=self.session["id"], session_episode_index=self.session["index"])
            if real:                                                              # episodes-loop.js saveEpisodeInfo + skins.js assignEpisodeSkins
                del info["skin"]
                err = bool(self.real.get("info_error"))
                info.update(encountered_error=err and b == "Alpha", peer_encountered_error=err and b == "Bravo", angle_convention={
                    "yaw": "mineflayer radians in [0, 2pi): yaw = (pi - radians(mc_yaw)) mod 2pi; 0 faces -Z (north), pi/2 faces -X (west)",
                    "pitch": "mineflayer radians: pitch = -radians(mc_pitch); positive looks up"})
                ae, nm_ = self.real.get("assign_error"), BOTS if b == "Alpha" else (b,)     # the follower waits for its own skin only
                errs = [f"{bb}: texture not applied within 15000 ms" for bb in nm_ if bb == ae] + (
                    [f"{b}: leader applied {skins[b][1]['skin_id']} (pool differed between bots)"] if self.real.get("pool_differed") == b != "Alpha" else [])
                sm.update(skins={bb: dict(skins[bb][1]) for bb in BOTS}, skin_assignment={
                    "ok": not errs, "rng_key": f"{self.seed}_skin_{self.eid}", "pool_size": 318, "mode": "auto", "sources": ["default", "procedural"], "models": ["classic", "slim"],
                    "default_fraction": -1, "hold_episodes": 1, "block_start_episode": self.eid, "applied_by": "Alpha", "errors": errs, "duration_ms": 1650 + 40 * i,
                    "applied": {bb: bb != ae for bb in nm_}})                     # skins.js: ok = no error of either player (or the pool note)
                if self.real.get("export_skins"):                                 # tools/skins/export_episode_skins.py
                    for bb in BOTS:
                        with open(os.path.join(self.out, f"{stem}_skin_{bb}.png"), "wb") as fh:
                            fh.write(skins[bb][2])
            write_json(os.path.join(self.out, stem + "_episode_info.json"), info)
            if real and self.video:
                n_pair, pok, pcl, csrc = self.write_video_real(b, stem, r, acts, E, Y, P)
                truth.update({f"pair_ok_{b}": pok, f"clean_{b}": pcl, f"cam_src_{b}": csrc})
            else:
                n_pair = self.write_video(b, stem, r) if self.video else len(r)
            if real:                                                              # skin of b: seen by the camera of o
                ob = BOTS.index(o); miss = b in (self.real.get("missing_skin"), self.real.get("assign_error")); unf = self.real.get("camera_restart") == o
                truth.update({f"skin_ok_{b}": not miss, f"skin_sha1_{b}": hashlib.sha1(skins[b][2]).hexdigest(), f"skin_applied_{b}": -1 if miss or unf else t0 + 900 + 40 * ob,
                              f"skin_camcheck_{b}": -1 if unf else int(not miss)})
            truth.update({f"t_{b}": r, f"health_{b}": np.array(H, np.float32), f"cam_eye_{b}": np.array(E), f"cam_yaw_{b}": np.array(Y),
                          f"cam_pitch_{b}": np.array(P), f"mobs_{b}": np.array(MOB, np.int32), f"deaths_{b}": self.dead_bots[b], f"n_pair_{b}": n_pair,
                          f"using_{b}": np.array(U, bool), f"target_{b}": np.array(TG, np.int32), f"dig_{b}": np.array(DG, np.int32),
                          f"hotbar_names_{b}": np.array(HBN, dtype="U40"), f"hotbar_counts_{b}": np.array(HBC, np.int32), f"body_yaw_{b}": np.array(BY)})
        if real:
            self.write_camera_meta_real(skins, t0, t_end); truth["vfov"] = float(CAM_OPTIONS["fov_deg"])
        else:
            self.write_camera_meta(); truth["vfov"] = vfov_of_meta(self.cam_meta)
        truth.update(names=np.array(self.names), t16=np.array(self.t16), render_time=np.array(self.render_time), cam_pos=np.array(self.cam_pos),
                     engine=np.array(self.engine), valid=not self.real.get("info_error"))
        return truth

    def write_camera_meta(self):
        if self.cam_meta is None:
            return
        for i, b in enumerate(BOTS):
            p = os.path.join(self.out_root, "camera", f"output_{b.lower()}", "0", f"camera_{b.lower()}_meta.json")
            if os.path.exists(p):
                continue
            m = {"recording_path": f"/output/camera_{b.lower()}.mkv", "start_epoch_seconds": (self.T0 - 30000) / 1000.0, "start_time_utc": "", "fps": 20,
                 "width": 1280, "height": 720, "display": f":{99 + i}", "camera_name": CAMS[i], "gpu_mode": "egl", "video_encoder": "libx264",
                 "wallclock_timestamps": True, "note": "synthetic (cap_synth.py)", "guiScale": 3, "renderDistance": 12}
            m.update(self.cam_meta)
            write_json(p, m)

    def cursor_of(self, xh):
        """synth crosshair -> frame-recorder state.cursor {block {pos, face, name, state, distance} | null, entity {uuid, id,
        entity_id, type, name, distance} | null} (name / state of the block not tracked here: null)."""
        if xh is None:
            return {"block": None, "entity": None}
        if xh["type"] == "entity":
            nm = next((bb for bb in BOTS if uuid_of("player", bb) == xh["uuid"]), None)
            return {"block": None, "entity": {"uuid": xh["uuid"], "id": "minecraft:player" if nm else "minecraft:zombie", "entity_id": None,
                                              "type": "player" if nm else "hostile", "name": nm or "zombie", "distance": xh["distance"]}}
        return {"block": {"pos": list(xh["pos"]), "face": FACE.get(tuple(xh["face"])), "name": None, "state": None, "distance": xh["distance"]}, "entity": None}

    def win_path(self, *parts):
        return "\\".join((WIN_ROOT, os.path.basename(os.path.normpath(self.out_root))) + parts)

    def cam_meta_name(self, b):
        """camera meta file name of bot b's camera for this episode (REAL_CASES camera_restart: run_windows restarted it -> _r1)."""
        return f"camera_{b.lower()}{'_r1' if self.real.get('camera_restart') == b else ''}_meta.json"

    def write_camera_meta_real(self, skins, t0, t_end):
        """camera/output_<bot>/<inst>/camera_<bot>_meta.json as the capture mod writes it (CaptureController.buildMeta: options.fov_deg /
        fov_effect_scale, pose_link, local_skins {skins {"<skin_id>:<model>": status, sha256_*, loaded_epoch_ms}}; finishWriter adds
        frames, stop_epoch_seconds, stop_reason, capture_stats and the final local_skins), one per camera session: local_skins
        accumulates over the batch's episodes. Each camera renders the OTHER bot's skin; REAL_CASES missing_skin -> that skin
        "missing" (the camera showed the default skin), assign_error -> no entry (never applied on the server); camera_restart ->
        this episode's camera of that bot is camera_<bot>_r1_meta.json as written at its recording start (killed at the end: no
        finishWriter, local_skins.skins {})."""
        for i, b in enumerate(BOTS):
            d = os.path.join(self.out_root, "camera", f"output_{b.lower()}", str(self.inst)); p = os.path.join(d, self.cam_meta_name(b))
            if os.path.exists(p):
                with open(p, encoding="utf-8") as fh:
                    m = json.load(fh)
            else:
                m = {"recording_path": self.win_path("camera", f"output_{b.lower()}", str(self.inst), os.path.basename(p).replace("_meta.json", ".mkv")),
                     "start_epoch_seconds": (self.T0 - 30000) / 1000.0, "start_time_utc": "", "fps": 20, "width": 1280, "height": 720, "camera_name": CAMS[i],
                     "video_encoder": "libx264", "wallclock_timestamps": True, "timestamp_source": "capture_time", "capture_method": "in_client_fbo_readback",
                     "frames_file": os.path.basename(p).replace("_meta.json", "_frames.jsonl"), "platform": "windows", "mc_version": "1.21", "framebuffer": [1280, 720],
                     "window_hidden": True, "note": "synthetic (cap_synth.py, engine real)", "options": dict(CAM_OPTIONS), "pose_link": {"enabled": True, "port": 8130 + i},
                     "toasts": {"total": 0, "by_class": {}},
                     "local_skins": {"enabled": True, "skins_dir": "C:/Users/cap/capture/server/skins", "non_local_lookups": 0, "non_local_textures": 0, "skins": {}}}
            if self.real.get("camera_restart") == b:                              # start-time meta only
                write_json(p, m, indent=1)
                continue
            o = BOTS[1 - i]; sk = skins[o][1]; miss = self.real.get("missing_skin") == o
            if self.real.get("assign_error") != o:
                m["local_skins"]["skins"][f"{sk['skin_id']}:{sk['model']}"] = {
                    "skin_id": sk["skin_id"], "model": sk["model"], "status": "missing" if miss else "loaded",
                    "file": None if miss else f"C:/Users/cap/capture/server/skins/{sk['file']}", "sha256_expected": sk["sha256"], "sha256_file": None if miss else sk["sha256"],
                    "sha256_match": None if miss else True, "requests": 3, "loaded_epoch_ms": None if miss else t0 + 900 + 40 * i}
            n_ = int((t_end - self.T0 + 30000) // 50)
            m.update(frames=n_, stop_epoch_seconds=(t_end + 4000) / 1000.0, stop_reason="stop_file", capture_stats={
                "captured": n_, "encoded": n_, "dropped_writer_backlog": 0, "slots_skipped": 2, "slot_lag_ms_mean": 3.1, "max_capture_gap_ms": 400.0,
                "forced_readback_waits": 0, "duration_seconds": (t_end + 4000 - self.T0 + 30000) / 1000.0, "mean_capture_fps": 19.9, "render_frames": n_ + 40,
                "ffmpeg_exit_code": 0, "loss_warnings": 0})
            write_json(p, m, indent=1)

    def write_video_real(self, b, stem, r, acts, E, Y, P):
        """aligned/<stem>_camera{.mp4,_meta.json} as process_recordings writes them for the Windows capture mod (sidecar used, pose link
        on, pose_seq pairing): the recording starts after the first 3..5 entries (unmatched_actions_start: matched to frame 0),
        stalls once for 400 ms at 40 % (a later frame reused: delta > 1.5 frames), ends before the last 2..4 entries; a frame renders
        the newest movement packet that has arrived (eye = feet + 1.62 / 1.27): packets of a 600 ms datagram-latency spike every
        4.5 s arrive 45 ms late (the first frame after an entry can show the previous packet: a pose_seq "next" re-pair up to 2
        frames late), and one capture slot is dropped inside a controller packet gap (> 75 ms) right before an entry sampled in
        that gap (a "prev" re-pair over a 100 ms frame gap: up to 2 frames early); 4 frames the server-followed camera player
        (pose_source server, stale: the previous packet, eye x 0.785 while sneaking), 3 frames a loading screen (cam_screen, fov
        null); cam_yaw unwrapped. -> (n_pair, pair_ok [F] (not before the recording, delta in [-1, 1.5] frames of 50 ms, [-2.25,
        2.25] for pose_pair same / prev / next), clean [F], camera source per entry); E / Y / P (lists, per entry) are set to the
        rendered camera of every pair_ok entry."""
        from mineengine import mc_proxy_style as PS
        i = BOTS.index(b); C = self.client[b]; ct = np.array([c[0] for c in C]); n_all = len(r)
        lead, drop = 3 + (self.eid + i) % 3, 2 + (self.eid + i) % 3
        f0, t_end, st0 = int(r[lead]) - 5, int(r[n_all - drop]) - 1, int(r[int(n_all * 0.4)])
        gaps = [(ct[j], ct[j + 1]) for j in range(1, len(ct) - 1) if ct[j + 1] - ct[j] > 75]       # controller packet gaps (hiccups, tick lag)
        hole = set()
        for g0, g1 in gaps:                                                      # the capture slot dropped in a gap: before the entry sampled there
            re_ = [int(x) for x in r if g0 + 50 < x < g1]
            hole |= {q for q in range(int((g0 + 50 - f0) // 50), int((g1 - f0) // 50) + 1) if re_ and g0 + 50 < f0 + 50 * q + (7 * q) % 4 < re_[0]}
        FT, q = [], 0
        while f0 + 50 * q + (7 * q) % 4 < t_end:
            tq = f0 + 50 * q + (7 * q) % 4; q += 1
            if not st0 <= tq < st0 + 400 and q - 1 not in hole:
                FT.append(tq)
        if FT[-1] < r[n_all - drop - 1]:
            FT.append(int(r[n_all - drop - 1]) + 1)
        nf = len(FT); srv = set(range(int(nf * 0.3), int(nf * 0.3) + 4)); scr = set(range(int(nf * 0.85), int(nf * 0.85) + 3))
        arr = ct + np.array([45.0 if (j + 17 * i) % 90 in range(40, 52) else 0.0 for j in range(len(ct))])   # packet arrival at the camera client
        fr = []
        for kf, tf in enumerate(FT):                                             # the sidecar line of every recorded frame
            jf = int(np.flatnonzero(arr <= tf)[-1]); sv = kf in srv; s = C[max(jf - 1, 0) if sv else jf][1]
            eye = s["pos"] + [0.0, CAM_EYE * (CAM_SNEAK if s["sneak"] else 1.0) if sv else (1.27 if s["sneak"] else 1.62), 0.0]
            yd, pd = mc_deg(s["yaw"], s["pitch"]); kt = int(np.searchsorted(self.tick_t, tf, side="right")) - 1
            fr.append(dict(t=tf, eye=eye, yaw=s["yaw"] % TAU, pitch=float(s["pitch"]), seq=None if sv else 1000 + jf, src="server" if sv else "injected",
                           cam={"cam_yaw": round(yd + 360.0 * (kf % 3 - 1), 4), "cam_pitch": round(pd, 4), "cam_pos": [round(float(v), 5) for v in eye],
                                "fov": None if kf in scr else 70.0, "sidecar_epoch_ms": float(tf), "cam_game_time": self.G0 + kt,
                                "cam_screen": "ReceivingLevelScreen" if kf in scr else None, "pose_source": "server" if sv else "injected",
                                "pose_age_ms": 4.0, "pose_seq": None if sv else 1000 + jf, "pose_t": float(tf - 4), "pose_held": False,
                                "pose_fallback": "stale" if sv else None, "player_yaw": yd, "player_pitch": pd}))
        idx, p_ = [], 0
        for tq in r:                                                             # _match_actions_to_frames: first frame at or after
            while p_ < nf and FT[p_] < tq:
                p_ += 1
            if p_ >= nf:
                break
            idx.append(p_)
        n, us = len(idx), next((q for q, tq in enumerate(r) if tq >= FT[0]), len(r))       # unmatched_actions_start: earlier than frame 0
        rs = lambda kf: fr[kf]["seq"] if 0 <= kf < nf and fr[kf]["src"] == "injected" else None  # noqa: E731
        kinds, pc = [], dict(checked=0, same=0, prev=0, next=0, none=0, skipped_no_seq=0, skipped_not_injected=0)
        for a_, kf in enumerate(idx):                                            # _repair_pairs_by_pose_seq
            sq = acts[a_]["pose_seq"]
            if rs(kf) is None:
                kinds.append(None); pc["skipped_not_injected"] += 1
                continue
            pc["checked"] += 1
            if rs(kf) == sq:
                kinds.append("same"); pc["same"] += 1
                continue
            cand = [(abs(FT[jj] - r[a_]), jj, nm) for jj, nm in ((kf - 1, "prev"), (kf + 1, "next")) if rs(jj) == sq]
            if not cand:
                kinds.append("none"); pc["none"] += 1
                continue
            _, jj, nm = min(cand); idx[a_] = jj; kinds.append(nm); pc[nm] += 1
        mp, pok, pcl, src = [], np.zeros(n_all, bool), np.ones(n_all, bool), np.array(["action"] * n_all, dtype="U16")
        for a_, kf in enumerate(idx):
            d = (FT[kf] - int(r[a_])) / 1000.0; f_ = fr[kf]
            mp.append({"action_index": a_, "renderTime_ms": float(acts[a_]["renderTime"]), "action_time_sec": acts[a_]["epochTime"],
                       "relative_time_ms": acts[a_]["relativeTimeMs"], "frame_index": kf, "frame_time_sec": FT[kf] / 1000.0, "frame_pts_sec": FT[kf] / 1000.0,
                       "delta_sec": d, **f_["cam"], "action_pose_seq": acts[a_]["pose_seq"], **({"pose_pair": kinds[a_]} if kinds[a_] else {})})
            lo, hi = (-2.25 * 0.05, 2.25 * 0.05) if kinds[a_] in ("same", "prev", "next") else (-0.05, 0.075)
            pok[a_] = a_ >= us and lo - 1e-6 <= d <= hi + 1e-6; pcl[a_] = f_["cam"]["cam_screen"] is None
            if pok[a_]:
                E[a_], Y[a_], P[a_], src[a_] = f_["eye"], f_["yaw"], f_["pitch"], "frame_mapping"
        fv = np.zeros((n, 180, 320, 3), np.uint8); fv[:, :90] = (150, 190, 235); fv[:, 90:] = (106, 170, 64)
        al = os.path.join(self.out_root, "aligned"); os.makedirs(al, exist_ok=True)
        PS.write_video(os.path.join(al, stem + "_camera.mp4"), fv, fps=20)
        dl = [x["delta_sec"] for x in mp]; bl = b.lower()
        dg = {"n_actions": n_all, "n_frames": nf, "n_matched": n, "skipped_frames_start": idx[0], "skipped_frames_end": nf - 1 - idx[-1], "unmatched_actions_start": us,
              "unmatched_actions_end": n_all - n, "mean_delta_sec": float(np.mean(dl)), "max_abs_delta_sec": float(np.abs(dl).max()), "min_delta_sec": float(min(dl)),
              "max_delta_sec": float(max(dl)), "duplicate_frame_count": sum(1 for c in np.unique(idx, return_counts=True)[1] if c > 1), "interior_unconsumed_count": 0,
              "dropped_frame_gaps": 1, "match_clock": "sidecar_capture_time", "pts_duplicate_frames": 0, "pose_seq_pairing": pc, "sidecar_used": True,
              "sidecar_path": self.win_path("camera", f"output_{bl}", str(self.inst), self.cam_meta_name(b).replace("_meta.json", "_frames.jsonl")), "sidecar_lines": nf, "sidecar_bad_lines": 0,
              "screen_frames": sum(1 for x in mp if x["cam_screen"]), "screen_frames_by_screen": {"ReceivingLevelScreen": sum(1 for x in mp if x["cam_screen"])}}
        cmn = self.cam_meta_name(b)
        meta = {"actions_path": self.win_path("output", stem + ".json"), "camera_meta_path": self.win_path("camera", f"output_{bl}", str(self.inst), cmn),
                "recording_path": self.win_path("camera", f"output_{bl}", str(self.inst), cmn.replace("_meta.json", ".mkv")), "aligned_video_path": self.win_path("aligned", stem + "_camera.mp4"),
                "alignment_mode": "wallclock", "fps": 20.0, "camera_start_time_sec": (self.T0 - 30000) / 1000.0, "first_frame_time_sec": FT[0] / 1000.0,
                "last_frame_time_sec": FT[-1] / 1000.0, "total_video_frames": nf, "first_action_time_sec": r[0] / 1000.0, "last_action_time_sec": r[n - 1] / 1000.0}
        if not self.real.get("omit_camera_params"):                              # _build_camera_params (else: only the camera meta has the fov)
            meta["camera_params"] = dict(CAM_OPTIONS, width=1280, height=720, fps=20, capture_method="in_client_fbo_readback", platform="windows", mc_version="1.21",
                                         encoder="libx264", options_recorded=True)
        meta.update(diagnostics=dg, frame_mapping=mp)
        write_json(os.path.join(al, stem + "_camera_meta.json"), meta)
        return n, pok, pcl, src

    def write_video(self, b, stem, r):
        """aligned/<stem>_camera.mp4 placeholder + its alignment meta, as process_recordings writes them: the camera recording
        ends before the last 2..4 action entries, so only the first n_matched entries get a frame -> n_matched."""
        from mineengine import mc_proxy_style as PS
        drop = 2 + (self.eid + BOTS.index(b)) % 3; n_all, r = len(r), r[:len(r) - drop]
        n = len(r); fr = np.zeros((n, 180, 320, 3), np.uint8)
        fr[:, :90] = (150, 190, 235); fr[:, 90:] = (106, 170, 64)
        for q in range(n):
            fr[q, 160:170, :int(320 * q / max(n - 1, 1))] = (255, 255, 255)
        al = os.path.join(self.out_root, "aligned"); os.makedirs(al, exist_ok=True)
        PS.write_video(os.path.join(al, stem + "_camera.mp4"), fr, fps=20)
        mp = [{"action_index": q, "renderTime_ms": int(rt), "action_time_sec": rt / 1000.0, "relative_time_ms": float(rt - r[0]), "frame_index": q + 3,
               "frame_time_sec": (rt + 12) / 1000.0, "delta_sec": 0.012} for q, rt in enumerate(r)]
        write_json(os.path.join(al, stem + "_camera_meta.json"), {"actions_path": f"output/{stem}.json", "camera_meta_path": f"camera/output_{b.lower()}/0/camera_{b.lower()}_meta.json",
                                                                  "recording_path": f"camera/output_{b.lower()}/0/camera_{b.lower()}.mkv", "aligned_video_path": f"aligned/{stem}_camera.mp4",
                                                                  "alignment_mode": "wallclock", "fps": 20.0, "camera_start_time_sec": (self.T0 - 30000) / 1000.0,
                                                                  "total_video_frames": n + 3, "diagnostics": {"n_actions": n_all, "n_frames": n + 3, "n_matched": n,
                                                                                                               "unmatched_actions_start": 0, "unmatched_actions_end": n_all - n,
                                                                                                               "dropped_frame_gaps": [], "duplicate_frame_count": 0},
                                                                  "frame_mapping": mp})
        return n


def write_episode(out_root, eid, kind, seconds=20, seed=0, persist_from=None, video=False, inst=0, cam_meta=None, persist_next=False, cam_pos="feet",
                  names="canonical", t16="tick", render_time="epoch", engine="schema", skin_pool=None, real=None, garden=False):
    """One synthetic episode in the capture layout under out_root/output/ (module docstring) -> dict of paths and facts
    (stem, events, snap_start, snap_end, truth, actions {bot: path}, deaths, session). cam_meta: extra keys of the session camera
    meta (default T13: fov 0.0 (options.txt scale = 70 deg), fovEffectScale 0.0); persist_next marks the first episode of a
    persistent chain (no reset after it); cam_pos "eye" logs camera_pose at eye level (getEyeLocation) instead of the feet;
    names "alt" = the not-yet-fixed T12 / T15 / T17 names (NAMES); t16 "action" = T16 fields per frame instead of in the tick
    records; render_time "relative" = act_recorder's non-epoch renderTime (epochTime then carries the host clock). engine "real"
    = the capture engine's own output instead of the schema layout (module docstring; names / t16 / cam_meta ignored): skin PNGs go to
    skin_pool (default <out_root>/../skin_pool/pool), real = REAL_CASES-style switches (export_skins, omit_camera_params,
    missing_skin, info_error, assign_error, pool_differed, camera_restart). garden: the GARDEN row of states (module docstring)."""
    assert names in NAMES and t16 in ("tick", "action") and render_time in ("epoch", "relative") and cam_pos in ("feet", "eye") and engine in ("schema", "real")
    ep = Episode(out_root, eid, kind, seconds, seed, persist_from, video, inst, {"fov": 0.0, "fovEffectScale": 0.0} if cam_meta is None else cam_meta)
    ep.persist_next, ep.cam_pos, ep.names, ep.t16, ep.render_time, ep.garden = bool(persist_next), cam_pos, names, t16, render_time, bool(garden)
    if engine == "real":
        ep.engine, ep.names, ep.t16, ep.render_time, ep.real = "real", "real", "state", "epoch", dict(real or {})
        ep.skin_pool = skin_pool or os.path.join(os.path.dirname(os.path.abspath(out_root)), "skin_pool", "pool")
    return ep.run()


def write_root(out, kinds=tuple(KINDS), chain=len(CHAIN), seconds=20.0, seed=0, video=False, log=print, real_kinds=()):
    """out/batch_0: one episode per kind (eid 1..; canonical names, T16 in ticks, T13 camera meta; build with the
    GARDEN); out/batch_1: a persistent
    chain of `chain` episodes with the CHAIN variants (camera meta without fovEffectScale); out/batch_2: one capture-engine-layout
    episode per real_kinds entry (engine "real", REAL_CASES switches; skin pool out/skin_pool/pool) -> list of write_episode
    results."""
    made = []
    for j, kind in enumerate(kinds):
        r = write_episode(os.path.join(out, "batch_0"), j + 1, kind, seconds, seed, video=video, garden=kind == "build"); made.append(r)
        log(f"[cap_synth] batch_0 {r['stem']} {kind}: {r['records']} records, deaths {r['deaths']}")
    prev = None
    for j in range(chain):
        v = CHAIN[j % len(CHAIN)]
        r = write_episode(os.path.join(out, "batch_1"), j + 1, v["kind"], seconds, seed + 1, persist_from=prev, video=video, cam_meta={"fov": 70}, persist_next=True,
                          **{k: x for k, x in v.items() if k != "kind"})
        prev = r["snap_end"]; made.append(r)
        log(f"[cap_synth] batch_1 (persistent) {r['stem']} {v}: {r['records']} records, session {r['session']}")
    for j, kind in enumerate(real_kinds):
        r = write_episode(os.path.join(out, "batch_2"), j + 1, kind, seconds, seed + 2, video=video, engine="real", skin_pool=os.path.join(out, "skin_pool", "pool"),
                          real=REAL_CASES.get(kind)); made.append(r)
        log(f"[cap_synth] batch_2 (capture-engine layout) {r['stem']} {kind} {REAL_CASES.get(kind)}: {r['records']} records, deaths {r['deaths']}")
    return made


def selftest(seconds=8.0):
    """write_root for every kind into a temp dir (twice: byte-deterministic), then per episode with the schema's own rules (not
    cap_reader): RLE decode in yzx order, start + block_state replay == end snapshot == truth final grid, seq 0.. in file order,
    camera_pose / camera tick entries == truth eye (sneak scale), T15 / T17 / alt names written."""
    import shutil
    import tempfile
    tmp = tempfile.mkdtemp(prefix="cap_synth_")
    ok = True

    def check(c, msg):
        nonlocal ok
        ok &= bool(c); print(f"{'PASS' if c else 'FAIL'} {msg}", flush=True)
    try:
        made = write_root(os.path.join(tmp, "a"), seconds=seconds, log=lambda *_: None)
        write_root(os.path.join(tmp, "b"), seconds=seconds, log=lambda *_: None)
        fa = sorted(os.path.relpath(os.path.join(d, f), os.path.join(tmp, "a")) for d, _, fs in os.walk(os.path.join(tmp, "a")) for f in fs if "synth_truth" not in d)
        same = all(open(os.path.join(tmp, "a", f), "rb").read() == open(os.path.join(tmp, "b", f), "rb").read() for f in fa)
        check(same and len(fa) > 30, f"deterministic: {len(fa)} files byte-identical on a re-run")
        cn = {"alt": set(NAMES["alt"].values()) - {None}, "canonical": set(NAMES["canonical"].values()) - {None}}
        for r in made:
            ev = [json.loads(x) for x in open(r["events"], encoding="utf-8")]
            tr = np.load(r["truth"])
            st, pal, h = load_snapshot_states(r["snap_start"]); se, pe, _ = load_snapshot_states(r["snap_end"])
            g = np.array(pal, object)[st]
            for e in ev:
                if e["type"] == "block_state":
                    g[tuple(np.asarray(e["pos"]) - BOX_MIN)] = e["state"]
            cap = np.vectorize(CP.block_id)(g).astype(np.uint16)
            types = {e["type"] for e in ev}
            names = str(tr["names"])
            cam = [e for e in ev if e["type"] == "camera_pose"] if names == "canonical" else [p for e in ev if e["type"] == "tick" for p in e["players"] if p["name"] in CAMS]
            check([e["seq"] for e in ev] == list(range(len(ev))) and (np.array(pe, object)[se] == g).all() and np.array_equal(cap, tr["final"])
                  and len(cam) > 100 and (cn[names] & types) and not (cn["alt" if names == "canonical" else "canonical"] - {"item_held"}) & types - cn[names],
                  f"{os.path.basename(os.path.dirname(os.path.dirname(r['events'])))}/{r['eid']} {r['kind']} ({names}): seq, replay == end == truth, "
                  f"{len(cam)} camera rows, names {sorted(cn[names] & types)}")
            if r["kind"] == "break":
                dg = [e for e in ev if e["type"] in (NAMES[names]["dig_progress"],) and e.get("stage", -1) >= 0]
                check(len(dg) >= 3 and any(d["stage"] == 9 for d in dg), f"break: {len(dg)} T17 crack-stage records (stages up to 9)")
            if r["kind"] in ("build", "pvp"):
                check(NAMES[names]["use_start"] in types and NAMES[names]["use_end"] in types, f"{r['kind']}: T15 use start / end records")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    print(f"[cap_synth] {'PASS' if ok else 'FAIL'}", flush=True)
    return ok


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out"); ap.add_argument("--test", action="store_true")
    ap.add_argument("--kinds", nargs="*", default=[",".join(KINDS)], help="comma- or space-separated subset of " + ",".join(KINDS))
    ap.add_argument("--chain", type=int, default=len(CHAIN)); ap.add_argument("--seconds", type=float, default=20.0)
    ap.add_argument("--seed", type=int, default=0); ap.add_argument("--video", action="store_true")
    ap.add_argument("--real-kinds", default="", help="comma-separated kinds for DIR/batch_2 in the capture engine's own layout (engine real)")
    a = ap.parse_args()
    if a.test:
        sys.exit(0 if selftest() else 1)
    if not a.out:
        ap.error("--out DIR (or --test)")
    kinds = [k for x in a.kinds for k in x.split(",") if k]
    bad = [k for k in kinds if k not in KINDS]
    if bad:
        ap.error(f"unknown kinds {bad}; choose from {list(KINDS)}")
    rk = [k for k in a.real_kinds.split(",") if k]
    if [k for k in rk if k not in KINDS]:
        ap.error(f"unknown --real-kinds {rk}; choose from {list(KINDS)}")
    write_root(a.out, kinds, a.chain, a.seconds, a.seed, a.video, log=lambda m: print(m, flush=True), real_kinds=rk)


if __name__ == "__main__":
    main()
