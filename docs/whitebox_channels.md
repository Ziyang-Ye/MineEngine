# White-box channels

`python -m mineengine.cap2proxy` renders the white box from the replayed world state (start snapshot plus logged block changes) for every player and paired frame. The module docstring of `mineengine/cap2proxy.py` is the reference; this page is the summary.

## Per episode and player: `episodes/<episode>/<bot>/gbuf.npz`

All planes are `[F, 192, 336]`, one frame per paired action entry. `proxy.mp4` draws them at 2× (384 × 672).

| key | dtype | meaning |
|---|---|---|
| `logdepth` | uint8 | 0 = no surface (sky). Otherwise depth on a log scale: `z_m = 0.2032 · 2^(10 (v − 1) / 254)`, covering about 0.2–208 m |
| `shade` | uint8 | shading term from the surface normal and the view direction |
| `edge` | uint8 | 255 on block-face borders: the 1 m grid lines that coplanar neighbours would hide, about 1.5 px wide |
| `pid` | uint8 | 0 none; 1 = the other player (body + head boxes, hidden while dead); 2..255 = mobs, items, primed TNT and falling blocks from a free list. Entity types per frame are in `state.jsonl` |
| `btype` | uint16 | block type: index into `types.json` = `[""] +` the sorted registry types of the client jar. 0 = air / sky / entity; 65535 = a type outside the registry |
| `bstate` | uint16 | full block state: index into the episode's `states.json` `"states"` (`null`, then `{state, type, btype, geom, ...}`). The water surface of waterlogged cells carries the implied `minecraft:water[level=0]` |
| `face` | uint8 | 0 none; 1..6 = +x, −x, +y, −y, +z, −z: the surface normal turned toward the camera, with the dominant axis for rotated faces |
| `fx` | uint16 | bits 0–3: block-breaking stage + 1 (1..10, 0 = none) on the dug block's own geometry. Bits 4–9 are reserved for on_fire, hurt, glowing, frozen, wet and glint |
| `block`, `block_ext` | uint8 | compact palette ids: 12 classes (v0), or the extended palette of `mineengine.cap_palette` |

## Geometry

- Geometry comes from the client jar's `blockstates` and `models` (`python -m mineengine.mc_registry`).
- Full cubes go through a fast path. Every other state is built from its model elements, with variant and element rotation, cullface and texture-alpha crops. This covers torches, flowers, crops, rails, buttons and redstone wire.
- Fluids are drawn by level. Block entities come from a small hand table.
- The mesh follows every state version: door opened, furnace lit, snow layers, water level, crop age. A change re-meshes only the 16 × 16 columns around it.

## Cameras and state

- `cams.npz`:
  - `c2w` uses OpenCV axes, in metres (1 block = 1 m);
  - also `eye`, `yaw`, `pitch`, `vfov`, `t_ms`;
  - `pair_ok`, set when the frame really rendered this action;
  - `cam_source`.
- `state.jsonl` has one line per frame:
  - **the player:** health, food, hotbar, held item and slot, swing and hurt ages, item use, crosshair target, dig stage;
  - **the other player:** pose, health, held item, sneaking;
  - **every entity:** pid, type, position, yaw, health;
  - **the events since the previous frame.**
- UI is never drawn into the white box. HUD state stays in this log. `--hud-proxy` additionally writes a decorated `proxy_hud.mp4` for visualisation.
- **Where moving things are drawn.** The camera client draws the other player and other entities about 0.13–0.21 s behind their logged positions, because it smooths their movement. This lag was measured from name tags in real frames.
  - By default the white box draws everything at the logged positions of the frame's action time, so moving entities are drawn slightly ahead of the video.
  - `--entity-lag-ms MS` draws them where the client shows them, at their positions `MS` ms earlier; `--entity-lag-json` gives a value per view (`{"<episode>/<bot>": ms}`).
  - A move of more than 3 m within that window counts as a teleport or respawn, which the client shows at once, so it is not delayed.
  - The drawn position goes to `draw_pos` (the other player and each entity in `state.jsonl`). `pos` and every other state field stay at the action time. The lag used is stored as `entity_lag_ms` in `episode.json` and in each clip's `meta.json`.
  - A seated rider is raised to the seat and recorded in `draw_pos` as well.

## Clips: `clips/<episode>_<bot>_<start>/`

Each clip is 81 frames at 16 fps, cut inside runs of usable frames. It holds:
- `gbuf.npz`, `cams.npz` and `state.jsonl` for those frames;
- `whitebox.npz`: the box bounds, static cells, the block ids at the first frame, and every block change inside the clip with its frame;
- `meta.json`;
- `types.json` / `states.json`.
