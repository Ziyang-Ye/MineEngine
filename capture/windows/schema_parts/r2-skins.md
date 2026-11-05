# Schema additions (round 2): per-episode character skins + reference images

Extends `OUTPUT_SCHEMA.md` (sections 4 `episode_info.state_metadata`, 5 world
events `note`, and a new dataset section). Code: `controller/utils/skins.js`,
`tools/skins/*`, `regenerate_skin_cache.py`, MirrorBot `SkinManager` / `/episode skin`.

## 1. Skin pool: `server/skins/`

```
server/skins/
  session_a.png, session_b.png                         session skins of the episode start command (copies of pool/proc_0000.png, proc_0001.png)
  catalogue.json                                       pool index (below)
  pool/default_<name>_<wide|slim>.png                  18 default skins from the 1.21 client jar
  pool/proc_NNNN.png                                   procedural skins (tools/skins/generate_skins.py)
  pool/<file>.png.customskin                           optional Mojang-signed texture property (regenerate_skin_cache.py --upload; only mode=signed)
  reference/<skin_id>.png                              reference sheet (tools/skins/render_reference.py)
  reference/<skin_id>_skin.png                         copy of the raw 64x64 skin
  reference/<skin_id>_views.json                       panel rectangles inside the sheet
```

`<name>` in steve, alex, ari, efe, kai, makena, noor, sunny, zuri. `wide` = classic (4 px
arms), `slim` = 3 px arms. All PNGs are 64x64 RGBA (1.8+ layout, both layers).

### 1.1 `catalogue.json`

```json
{"schema": "solaris.skin_catalogue/1", "generated_utc": "...",
 "generator": {"script": "tools/skins/generate_skins.py", "base_seed": 20251023, "count": 300,
               "seed_formula": "base_seed * 100003 + index"},
 "counts": {"total": 344, "cached": 0, "legacy": 2, "default": 18, "procedural": 300, "heldout": 24},
 "skins": [ {entry}, ... ]}
```

| entry key | type | meaning |
|---|---|---|
| `skin_id` | str | unique id = PNG stem (`proc_0042`, `default_alex_slim`, `session_a`); also the argument of `/episode skin` |
| `file` | str | path relative to `server/skins/` |
| `model` | `"classic"` \| `"slim"` | arm model; it is part of the signed texture (`textures.SKIN.metadata.model`) |
| `source` | `"default"` \| `"procedural"` \| `"heldout"` \| `"legacy"` | origin (`legacy` = the 2 session skins) |
| `seed` | int \| null | per-skin generator seed (procedural only); `generate_skins.generate(seed)` reproduces the PNG |
| `sha256` | str | SHA-256 of the PNG bytes |
| `tags` | [str] | descriptive tags, `key:value` (see 1.2) |
| `attributes` | object | procedural only: `model`, `skin_tone`, `hair`, `hair_colour`, `top`, `top_colour`, `top_pattern`, `bottom`, `bottom_colour`, `shoes`, optional `beard`, `outer`, `outer_colour`, `hat`, `glasses`, `extras` [..] |
| `cached` | bool | a fresh `.customskin` exists (PNG hash matches) at catalogue build time |
| `texture_hash` | str | (when cached) id of the texture on textures.minecraft.net |
| `reference` | str | reference sheet path relative to `server/skins/` |

### 1.2 Tags (procedural)

`model:{classic,slim}`, `skin_tone:{porcelain,light,light_warm,fair_olive,medium_light,medium,olive,tan,brown,dark_brown,deep,very_deep}`,
`eyes:<colour>`, `hair:{bald,buzz,short,side_part,fringe,spiky,mohawk,curly,afro,bob,long,ponytail,bun,braids,pigtails}`,
`hair_colour:<name>[_tips]`, `beard:{stubble,moustache,goatee,full}`,
`top:{tshirt,longsleeve,tank,buttonshirt,hoodie,sweater,polo,dress,overalls,uniform}`, `top_colour:<name>`,
`top_pattern:{solid,hstripe,vstripe,check,twotone,raglan,gradient,dots}`, `graphic:<shape>`,
`bottom:{jeans,trousers,shorts,skirt,leggings,cargo,joggers,dress_short,dress_long,overalls}`, `bottom_colour:<name>`,
`legwear:{bare,tights,socks}`, `shoes:{sneakers,boots,tall_boots,dress_shoes,sandals,slip_ons}`,
`outer:{open_jacket,closed_jacket,vest,cardigan,coat,lab_coat}`, `outer_colour:<name>`,
`hat:{cap,beanie,headband,bandana,beret,flower,bucket,hood_up}`, `glasses:{round,square,sunglasses,goggles}`,
`extra:{headphones,scarf,backpack,watch,gloves,necklace}`, `belt`, `blush`, `freckles`, `eyebrows`, `hair_on_back`.
Default skins: `model:*`, `default:<name>`. Session skins (source `legacy`): `model:classic`, `session`.

### 1.3 `.customskin` (JSON)

| key | meaning |
|---|---|
| `value`, `signature` | Mojang-signed `textures` profile property (base64); the only fields MirrorBot reads |
| `skinName`, `dataVersion` | upstream format |
| `variant` | `classic` / `slim` requested at upload (checked against the signed payload) |
| `texture_url`, `texture_hash` | textures.minecraft.net URL / id |
| `png_sha256` | hash of the PNG that was signed; a mismatch marks the cache stale |
| `mineskin_uuid`, `generated_utc` | provenance |
| `verified_pixels` | (with `--verify`) the Mojang copy is pixel-identical to the PNG |

Why signed: a VANILLA 1.21 client shows another player's skin only if the `textures` property
carries a valid Mojang signature (`PlayerInfo#createSkinLookup` falls back to the default
skin when `PlayerSkin.secure()` is false) and the URL is on `textures.minecraft.net`.
Signed caches are now OPTIONAL: they are only used by `/episode skin ... mode=signed` (and as
the fallback of `mode=auto`), by the session skins of `episode start` when a cache exists
(without one they are applied as local skins), and on the Docker path, whose vanilla camera
clients show only signed skins. Signing
uploads the PNG to the third-party MineSkin service, so `regenerate_skin_cache.py` uploads
nothing without an explicit `--upload` flag (dry run otherwise); no setup script calls it.

### 1.4 Local skins (default, fully offline): unsigned property + camera mod

Since we control the camera clients, MirrorBot's local mode sets an UNSIGNED `textures`
property (Paper `Player#setPlayerProfile`, which re-sends the player to every viewer that may
see it and to itself) whose base64 JSON names the local skin:

```json
{"timestamp": 1792785555000, "profileId": "<uuid without dashes>", "profileName": "Alpha",
 "signatureRequired": false,
 "textures": {"SKIN": {"url": "http://solaris.local/skins/proc_0042.png", "metadata": {"model": "slim"}}},
 "solaris": {"v": 1, "skin_id": "proc_0042", "file": "pool/proc_0042.png", "sha256": "<PNG sha256>", "model": "slim"}}
```

(`metadata` only for slim, like Mojang's payloads; model from `catalogue.json`, else detected
from the PNG.) The camera mod (`solaris-capture`, `windows/schema_parts/camera.md` "Local
skins") recognises the `solaris` block / the `solaris.local` host and renders the PNG from its
local copy of the pool (`--skins-dir`, default `server/skins/pool`; session skins from its
parent), with the given model, as a secure skin; it never downloads anything (in-memory
`DynamicTexture` from the local PNG, no `HttpTexture`, so the URL is never opened and the pool
is never written). Vanilla
clients ignore the property (default skin). The texture id of a local skin is
`texture_hash = "local-" + <PNG sha256>` (Mojang ids are bare 64-hex), so `skinstatus`,
`state_metadata.skins.<Bot>.texture_hash` and the world-log notes tell the two modes apart.
SkinsRestorer's binding of the two players is removed when a local skin is applied, so it
cannot re-apply an older signed skin over it.

Both modes re-send the camera player's own profile to its client, i.e. a respawn: one
`ReceivingLevelScreen` frame and a ~150 ms capture hitch in that camera's raw recording per
apply (`camera.md` "Local skins"). Apply skins between episodes only (as `skins.js` does);
`/episode skin` warns in the server log and replies `"world_log_open": true` when a world log
is open, and every reply carries `"camera_respawn": true|false`.

## 2. `episode_info.state_metadata` (new keys, via `addEnvMetadata`)

| key | type | meaning |
|---|---|---|
| `skins` | object \| null | `{"Alpha": {skin}, "Bravo": {skin}}`, identical in both bots' files |
| `skins.<Bot>.skin_id` | str | catalogue id |
| `skins.<Bot>.model` | `classic` \| `slim` | |
| `skins.<Bot>.source` | `default` \| `procedural` \| `heldout` \| `legacy` | |
| `skins.<Bot>.sha256` | str | PNG hash (join key with the catalogue / exported files) |
| `skins.<Bot>.texture_hash` | str | texture id actually applied on the server (plugin reply / `skinstatus`): `local-<sha256>` (local mode) or the textures.minecraft.net id (signed) |
| `skins.<Bot>.texture_mode` | `local` \| `signed` | how the texture was applied (plugin reply / `skinstatus`) |
| `skins.<Bot>.seed` | int \| null | generator seed |
| `skins.<Bot>.file`, `.reference` | str | paths relative to `server/skins/` |
| `skins.<Bot>.tags` | [str] | catalogue tags |
| `skins.<Bot>.camera` | str \| null | (leader's file only) camera player that got the same skin |
| `skin_assignment.ok` | bool | all skins applied and confirmed on the server-side GameProfile |
| `skin_assignment.rng_key` | str | `"<skin_seed>_skin_<block_start_episode>"` (seedrandom key; separate from the episode RNG) |
| `skin_assignment.pool_size` | int | number of eligible skins after filtering |
| `skin_assignment.mode` | `auto` \| `local` \| `signed` | `skin_mode` option (default `auto` = local with signed fallback on Windows, `signed` elsewhere) |
| `skin_assignment.sources`, `.models` | [str] | pool filter (default sources `default,procedural` = 318 skins; add `heldout` and `legacy` for all 344) |
| `skin_assignment.default_fraction` | number | -1 = uniform over the pool |
| `skin_assignment.hold_episodes` | int | K: the same pair is kept for episode numbers `[block_start, block_start+K)` |
| `skin_assignment.block_start_episode` | int | `episodeNum - episodeNum mod K` |
| `skin_assignment.applied_by` | str | leader bot that sent `/episode skin` |
| `skin_assignment.applied` | {bot: bool} | texture confirmed via `/episode skinstatus` (the leader checks both, the follower itself) |
| `skin_assignment.errors` | [str] | empty when ok |
| `skin_assignment.duration_ms` | int | time spent (between episodes, before the teleport) |

Choice rule (both bots compute it): pool = catalogue entries with `source` in `sources`,
`model` in `models` (and a `.customskin` only when `skin_require_cached=1`, the default of
`skin_mode=signed`; otherwise no cache check), in catalogue order; players sorted by name; for each
player two draws `r1, r2` from `seedrandom(rng_key)`; `r1 < default_fraction` selects the
default/procedural sub-pool (only when `default_fraction >= 0`); `r2` picks the index among
skins whose texture is not already used (the two players never share a skin).
`skin_fixed` overrides per player.

## 3. World log (`*_world_events.jsonl`) notes

- Mid-session change through `/episode skin` while a world log is open (unless `note=0`):
  `{"type":"note", ..., "text":"{\"phase\":\"skin\",\"player\":\"Alpha\",\"camera\":\"CameraAlpha\"|null,\"skin_id\":\"proc_0005\",\"model\":\"classic\",\"texture_hash\":\"...\"}"}`
- Optional episode marker written by the leader right after `worldstate begin`
  (`noteEpisodeSkins`): `text = {"ep":N,"bot":"Alpha","phase":"skins","Alpha":"proc_0031:slim","Bravo":"proc_0003:slim"}`.

Skin changes do not touch blocks or non-player entities, so they do not affect replay.

## 4. Plugin commands (MirrorBot, RCON; one JSON line reply)

| command | reply |
|---|---|
| `episode skin <controller> <skin> [camera\|-] [note=0\|1] [mode=auto\|local\|signed]` | `{"ok":true,"player","camera"(null if offline/"-"),"skin_id","model","texture_hash","file","mode"("local"\|"signed"),"signed","sha256"(local),"fallback_from"(null, or why auto fell back to signed),"applied","noted","camera_respawn"(a camera got the skin: it respawns),"world_log_open"(applied during an open world log)}` / `{"ok":false,"error"}`. `<skin>` = skin_id or PNG name (top-level or `pool/`). Camera defaults to the pair from `episode start`, else `Camera<controller>`. `mode=local`: unsigned local property (1.4), any 64x64 PNG; `mode=signed`: `.customskin` through SkinsRestorer (`applied` is usually `false` in the reply, SkinsRestorer applies on the next tick; poll `skinstatus`); `mode=auto` (default): local, falling back to signed when the local apply throws and a `.customskin` exists. |
| `episode skinstatus <player>` | `{"ok":true,"player","online","skin_id","model","texture_hash","signed","mode"}` from the live GameProfile (`mode` from the texture id: `local-...` = local) |

`episode start` applies the session skins `session_a` / `session_b`: signed through SkinsRestorer when a
`.customskin` exists, otherwise as local skins (no cache needed); pool skins are no
longer required to have a `.customskin` for the start command (they are injected on demand).

### 4.1 Controller options (`controller/utils/skins.js`, CLI `--skin_xxx` or env `SKIN_XXX`)

| option | default | meaning |
|---|---|---|
| `skins` | 1 | 0 = keep the session skins |
| `skin_mode` | `auto` on Windows, `signed` elsewhere | sent as `mode=` with `/episode skin`; the bots accept the local and (auto) the signed texture id of the chosen skin. Windows camera clients run the solaris-capture mod (renders local skins); the Docker cameras are vanilla clients and need signed skins |
| `skin_sources` | `default,procedural` | catalogue sources (318 skins; the 24 `heldout` skins and the 2 `legacy` session skins, copies of proc_0000 / proc_0001, are drawn only when listed) |
| `skin_models` | `classic,slim` | |
| `skin_require_cached` | 0 (1 with `skin_mode=signed`) | only skins with a `.customskin` |
| `skin_default_fraction`, `skin_hold_episodes`, `skin_seed`, `skin_fixed`, `skin_catalogue`, `skin_wait_ms` (8000), `skin_settle_ms` (1500) | | see the file header |

E.g. force unsigned procedural skins only: `SKIN_SOURCES=procedural SKIN_MODE=local`.

## 5. Dataset export: `tools/skins/export_episode_skins.py <batch dir>`

Next to every `TS_EEEEEE_<Bot>_instance_III_episode_info.json` that has `state_metadata.skins`:

| file | content |
|---|---|
| `TS_EEEEEE_<Bot>_instance_III_skin_<Player>.png` | raw skin of `<Player>` (both players) |
| `TS_EEEEEE_<Bot>_instance_III_skin_<Player>_reference.png` | reference sheet of `<Player>` |
| `TS_EEEEEE_<Bot>_instance_III_skins.json` | `{"episode_info", "episode_number", "self": "<Bot>", "other": "<other bot>", "skins": {Player: {skin_id, model, source, sha256, texture_hash, seed, tags, skin_file, reference_file}}, "skin_assignment": {...}}` |

`self` is the recorded bot (its skin is visible only as the first-person arm), `other` is the
player seen in the video.

## 6. Reference sheet layout

RGB PNG, 1440x328: caption strip 24 px, then 5 panels of 288x304 px: `front`, `back`, `left`
(camera on the character's left), `right`, `texture` (raw skin x4 on a checkerboard).
Orthographic projection of the client's PlayerModel (1.21 geometry and UVs; outer layer
inflated 0.5 / 0.25; alpha-cutout like the client; slim arms for slim skins), T-pose (arms
rotated 90 degrees about the shoulder pivot), 8 px per texel, background (190,190,190).
`<skin_id>_views.json` gives each panel rectangle `[x, y, w, h]`.
