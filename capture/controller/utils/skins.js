/**
 * skins.js - per-episode character skins for Alpha / Bravo (and their cameras).
 *
 * The pool is server/skins/catalogue.json (tools/skins/build_pool.py): the 18 default
 * Minecraft skins + 300 procedural skins + 24 held-out procedural skins + the 2 session
 * skins (344). By default every skin is
 * applied as an UNSIGNED local texture (MirrorBot `mode=auto` -> local; the solaris-capture
 * camera mod renders it from its local copy of the pool), so no Mojang-signed .customskin
 * (regenerate_skin_cache.py) is needed. Per episode both bots compute the SAME choice from a
 * separate seeded RNG (`${seed}_skin_${blockStart}`, never the shared episode RNG, so
 * enabling skins does not change any episode plan). The leader (name sorts first) applies
 * both skins over RCON with `/episode skin <controller> <skin_id>` (MirrorBot), then
 * every bot waits until the server-side GameProfile carries the expected texture.
 *
 * Options (CLI `--skin_xxx` via minimist, or env `SKIN_XXX`; CLI wins):
 *   skins                 1 = enabled (default), 0 = do nothing (keep the session skins)
 *   skin_mode             auto (unsigned local texture, signed cache as fallback), local, or
 *                         signed (Mojang-signed .customskin through SkinsRestorer). Default: auto
 *                         on Windows (camera clients run the solaris-capture mod, which renders
 *                         local skins), signed elsewhere (Docker cameras are vanilla clients)
 *   skin_sources          comma list of catalogue sources: default,procedural,heldout,legacy
 *                         (default: default,procedural = 318 skins; the 24 held-out skins and
 *                         the 2 session skins (legacy), copies of proc_0000 / proc_0001, only when listed)
 *   skin_models           comma list: classic,slim (default both)
 *   skin_default_fraction probability of drawing from source "default" when both default
 *                         and procedural are enabled; -1 = uniform over the pool (default)
 *   skin_hold_episodes    K >= 1: keep the same pair of skins for K consecutive episode
 *                         numbers (block = floor(episodeNum / K)) to create identity repeats
 *   skin_seed             RNG base (default: bot_rng_seed)
 *   skin_fixed            "Alpha=proc_0001,Bravo=default_alex_slim" forces skins
 *   skin_catalogue        path of catalogue.json (default <repo>/server/skins/catalogue.json)
 *   skin_require_cached   1: only skins whose .customskin exists (default 1 with skin_mode=signed, else 0)
 *   skin_wait_ms          max wait for the skin to be applied (default 8000)
 *   skin_settle_ms        extra wait after applying, lets clients fetch the texture (default 1500)
 *
 * Nothing here may break an episode: every failure is logged and reported in the
 * returned metadata (`skin_assignment.ok = false`).
 */

const fs = require("fs");
const path = require("path");
const seedrandom = require("seedrandom");

const REPO_ROOT = path.resolve(__dirname, "..", "..");
const DEFAULT_CATALOGUE = path.join(REPO_ROOT, "server", "skins", "catalogue.json");

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

let _catalogueCache = null; // {file, mtimeMs, data}

/**
 * Read an option: CLI arg (args.skin_xxx) > env SKIN_XXX > default.
 */
function opt(args, key, def) {
  if (args && args[key] !== undefined && args[key] !== null && args[key] !== "") return args[key];
  const env = process.env[key.toUpperCase()];
  if (env !== undefined && env !== "") return env;
  return def;
}

function listOpt(v) {
  return String(v)
    .split(",")
    .map((s) => s.trim())
    .filter(Boolean);
}

/**
 * Effective skin options for this run.
 * @param {Object} args
 * @returns {Object}
 */
function skinOptions(args) {
  const seedBase = opt(args, "skin_seed", args && args.bot_rng_seed !== undefined ? args.bot_rng_seed : "12345");
  const fixed = {};
  for (const kv of listOpt(opt(args, "skin_fixed", ""))) {
    const [k, v] = kv.split("=");
    if (k && v) fixed[k.trim()] = v.trim();
  }
  const defMode = process.platform === "win32" ? "auto" : "signed";
  let mode = String(opt(args, "skin_mode", defMode)).toLowerCase();
  if (!["auto", "local", "signed"].includes(mode)) mode = defMode;
  return {
    enabled: Number(opt(args, "skins", 1)) !== 0,
    mode,
    // "heldout" and "legacy" (the 2 session skins) only on request
    sources: listOpt(opt(args, "skin_sources", "default,procedural")),
    models: listOpt(opt(args, "skin_models", "classic,slim")),
    defaultFraction: Number(opt(args, "skin_default_fraction", -1)),
    holdEpisodes: Math.max(1, Math.floor(Number(opt(args, "skin_hold_episodes", 1)) || 1)),
    seed: String(seedBase),
    fixed,
    catalogue: String(opt(args, "skin_catalogue", DEFAULT_CATALOGUE)),
    requireCached: Number(opt(args, "skin_require_cached", mode === "signed" ? 1 : 0)) !== 0,
    waitMs: Number(opt(args, "skin_wait_ms", 8000)),
    settleMs: Number(opt(args, "skin_settle_ms", 1500)),
  };
}

/**
 * Load (and memoise) the catalogue.
 * @param {string} file
 * @returns {Object|null}
 */
function loadCatalogue(file = DEFAULT_CATALOGUE) {
  try {
    const st = fs.statSync(file);
    if (_catalogueCache && _catalogueCache.file === file && _catalogueCache.mtimeMs === st.mtimeMs) {
      return _catalogueCache.data;
    }
    const data = JSON.parse(fs.readFileSync(file, "utf8"));
    _catalogueCache = { file, mtimeMs: st.mtimeMs, data };
    return data;
  } catch (err) {
    console.warn(`[skins] cannot read catalogue ${file}: ${err.message}`);
    return null;
  }
}

/**
 * True when the skin has a signed cache. Uses the catalogue flag, and when the skins
 * directory is reachable from this process also checks the .customskin file itself
 * (both bots run against the same checkout, so they agree).
 */
function isCached(entry, catalogueFile) {
  const dir = path.dirname(catalogueFile);
  const cachePath = path.join(dir, `${entry.file}.customskin`);
  if (fs.existsSync(path.join(dir, entry.file))) {
    return fs.existsSync(cachePath);
  }
  return Boolean(entry.cached);
}

let _missingLogged = false;

/**
 * Filtered, ordered skin pool (catalogue order => identical on both bots). A skin whose PNG and
 * signed cache are both missing on disk is left out (the 18 Minecraft defaults exist only after
 * scripts/setup_assets.py); both bots read the same files, so they still agree.
 * @param {Object} opts - skinOptions()
 * @returns {Array<Object>}
 */
function skinPool(opts) {
  const cat = loadCatalogue(opts.catalogue);
  if (!cat || !Array.isArray(cat.skins)) return [];
  const dir = path.dirname(opts.catalogue);
  const checkFiles = fs.existsSync(path.join(dir, "pool")); // skins dir not reachable: trust the catalogue
  let missing = 0;
  const pool = cat.skins.filter((s) => {
    if (!opts.sources.includes(s.source) || !opts.models.includes(s.model)) return false;
    if (opts.requireCached && !isCached(s, opts.catalogue)) return false;
    if (checkFiles && !fs.existsSync(path.join(dir, s.file)) && !fs.existsSync(path.join(dir, `${s.file}.customskin`))) {
      missing += 1;
      return false;
    }
    return true;
  });
  if (missing > 0 && !_missingLogged) {
    _missingLogged = true;
    console.warn(`[skins] ${missing} catalogue skins are missing on disk and are not drawn (run scripts/setup_assets.py)`);
  }
  return pool;
}

function findSkin(opts, skinId) {
  const cat = loadCatalogue(opts.catalogue);
  if (!cat) return null;
  return cat.skins.find((s) => s.skin_id === skinId) || null;
}

/**
 * Deterministic choice for one episode (pure function of options + episodeNum + names).
 * @param {Object} args
 * @param {number} episodeNum
 * @param {Array<string>} playerNames - e.g. ["Alpha", "Bravo"]
 * @param {Object} [opts]
 * @returns {{assignment: Object, rngKey: string, blockStart: number, poolSize: number}|null}
 */
function chooseEpisodeSkins(args, episodeNum, playerNames, opts = skinOptions(args)) {
  const names = playerNames.slice().sort();
  const pool = skinPool(opts);
  const blockStart = episodeNum - (((episodeNum % opts.holdEpisodes) + opts.holdEpisodes) % opts.holdEpisodes);
  const rngKey = `${opts.seed}_skin_${blockStart}`;
  const rng = seedrandom(rngKey);
  const assignment = {};
  const used = new Set();
  const useFraction =
    opts.defaultFraction >= 0 && opts.sources.includes("default") && pool.some((s) => s.source !== "default");
  for (const name of names) {
    if (opts.fixed[name]) {
      const e = findSkin(opts, opts.fixed[name]);
      if (e) {
        assignment[name] = e;
        used.add(e.texture_hash || e.sha256);
        continue;
      }
      console.warn(`[skins] fixed skin ${opts.fixed[name]} for ${name} not in catalogue; drawing randomly`);
    }
    let candidates = pool.filter((s) => !used.has(s.texture_hash || s.sha256));
    // always consume the same number of RNG draws, whatever the branch
    const r1 = rng();
    const r2 = rng();
    if (useFraction) {
      const wantDefault = r1 < opts.defaultFraction;
      const sub = candidates.filter((s) => (s.source === "default") === wantDefault);
      if (sub.length > 0) candidates = sub;
    }
    if (candidates.length === 0) return null;
    const e = candidates[Math.min(candidates.length - 1, Math.floor(r2 * candidates.length))];
    assignment[name] = e;
    used.add(e.texture_hash || e.sha256);
  }
  return { assignment, rngKey, blockStart, poolSize: pool.length };
}

function parseJson(res) {
  const text = String(res ?? "");
  const a = text.indexOf("{");
  const b = text.lastIndexOf("}");
  if (a < 0 || b <= a) return null;
  try {
    return JSON.parse(text.slice(a, b + 1));
  } catch (_) {
    return null;
  }
}

/**
 * Apply one skin now (any time; mid-episode changes are noted in the open world log).
 * @param {*} rcon
 * @param {string} player - controller name (its camera gets the same skin)
 * @param {string} skinId - catalogue skin_id (or a top-level PNG name)
 * @param {Object} [o] - {note: bool, mode: "auto"|"local"|"signed"}
 * @returns {Promise<Object|null>} plugin reply {ok, skin_id, model, texture_hash, applied, noted}
 */
async function setSkin(rcon, player, skinId, o = {}) {
  const cmd = `episode skin ${player} ${skinId}${o.note === false ? " note=0" : ""}${o.mode ? ` mode=${o.mode}` : ""}`;
  try {
    const res = await rcon.send(cmd);
    const j = parseJson(res);
    if (!j) console.warn(`[skins] '${cmd}' -> unexpected reply: ${String(res).slice(0, 300)}`);
    return j;
  } catch (err) {
    console.warn(`[skins] '${cmd}' failed: ${err.message}`);
    return null;
  }
}

/**
 * Current texture of a player per the server ({ok, online, skin_id, model, texture_hash, signed}).
 */
async function skinStatus(rcon, player) {
  try {
    return parseJson(await rcon.send(`episode skinstatus ${player}`));
  } catch (err) {
    return null;
  }
}

async function waitForTexture(rcon, player, textureHash, timeoutMs) {
  const want = (Array.isArray(textureHash) ? textureHash : [textureHash]).filter(Boolean);
  const t0 = Date.now();
  let last = null;
  while (Date.now() - t0 < timeoutMs) {
    last = await skinStatus(rcon, player);
    if (last && last.texture_hash && want.includes(last.texture_hash)) return { ok: true, status: last };
    await sleep(250);
  }
  return { ok: false, status: last };
}

/**
 * Texture hash (textures.minecraft.net id) of a catalogue entry, from its .customskin when
 * reachable (falls back to the catalogue field).
 */
function textureHashOf(entry, catalogueFile = DEFAULT_CATALOGUE) {
  try {
    const p = path.join(path.dirname(catalogueFile), `${entry.file}.customskin`);
    if (fs.existsSync(p)) {
      const j = JSON.parse(fs.readFileSync(p, "utf8"));
      if (j.texture_hash) return j.texture_hash;
      const v = JSON.parse(Buffer.from(j.value, "base64").toString("utf8"));
      const url = v && v.textures && v.textures.SKIN && v.textures.SKIN.url;
      if (url) return url.slice(url.lastIndexOf("/") + 1);
    }
  } catch (_) {
    // fall through
  }
  return entry.texture_hash || null;
}

/**
 * texture_hash of the unsigned local texture MirrorBot sets in local mode: "local-" + PNG sha256
 * (hash of the file when reachable, else the catalogue's).
 */
function localHashOf(entry, catalogueFile = DEFAULT_CATALOGUE) {
  let sha = entry.sha256 || null;
  try {
    const p = path.join(path.dirname(catalogueFile), entry.file);
    if (fs.existsSync(p)) sha = require("crypto").createHash("sha256").update(fs.readFileSync(p)).digest("hex");
  } catch (_) {
    // keep the catalogue hash
  }
  return sha ? `local-${sha}` : null;
}

/** Texture hashes accepted for an entry under a skin mode (auto: local first, then signed). */
function expectedHashes(entry, catalogueFile, mode) {
  if (mode === "signed") return [textureHashOf(entry, catalogueFile)].filter(Boolean);
  if (mode === "local") return [localHashOf(entry, catalogueFile)].filter(Boolean);
  return [localHashOf(entry, catalogueFile), textureHashOf(entry, catalogueFile)].filter(Boolean);
}

function describe(entry, catalogueFile, mode = "signed") {
  return {
    skin_id: entry.skin_id,
    model: entry.model,
    source: entry.source,
    sha256: entry.sha256,
    texture_hash: expectedHashes(entry, catalogueFile, mode)[0] || null,
    seed: entry.seed ?? null,
    file: entry.file,
    reference: entry.reference || `reference/${entry.skin_id}.png`,
    tags: entry.tags || [],
  };
}

/**
 * Pick, apply and wait for the per-episode skins. Call on BOTH bots, between episodes,
 * before the teleport / recording start. Returns metadata to merge into state_metadata:
 *   {skins: {Alpha: {...}, Bravo: {...}}, skin_assignment: {...}}  or null when disabled.
 * @param {*} bot
 * @param {*} rcon
 * @param {Object} args
 * @param {number} episodeNum
 * @returns {Promise<Object|null>}
 */
async function assignEpisodeSkins(bot, rcon, args, episodeNum) {
  const opts = skinOptions(args);
  if (!opts.enabled) return null;
  const me = bot.username;
  const other = args.other_bot_name;
  const leader = me < other;
  const t0 = Date.now();
  let plan = null;
  try {
    plan = chooseEpisodeSkins(args, episodeNum, [me, other], opts);
  } catch (err) {
    console.warn(`[${me}] [skins] choice failed: ${err.message}`);
  }
  const assignment = {
    ok: false,
    rng_key: plan ? plan.rngKey : null,
    pool_size: plan ? plan.poolSize : 0,
    mode: opts.mode,
    sources: opts.sources,
    models: opts.models,
    default_fraction: opts.defaultFraction,
    hold_episodes: opts.holdEpisodes,
    block_start_episode: plan ? plan.blockStart : null,
    applied_by: leader ? me : other,
    applied: {},
    errors: [],
  };
  if (!plan) {
    assignment.errors.push("empty skin pool (no cached skins match the filters?)");
    console.warn(`[${me}] [skins] ${assignment.errors[0]}`);
    return { skins: null, skin_assignment: assignment };
  }
  const skins = {};
  for (const [name, e] of Object.entries(plan.assignment)) skins[name] = describe(e, opts.catalogue, opts.mode);

  try {
    if (leader) {
      for (const [name, e] of Object.entries(plan.assignment)) {
        const reply = await setSkin(rcon, name, e.skin_id, { note: false, mode: opts.mode });
        if (!reply || !reply.ok) {
          assignment.errors.push(`${name}: ${reply ? reply.error : "no reply"}`);
          continue;
        }
        if (reply.texture_hash) skins[name].texture_hash = reply.texture_hash;
        if (reply.mode) skins[name].texture_mode = reply.mode;
        if (reply.model && reply.model !== e.model) {
          assignment.errors.push(`${name}: signed model ${reply.model} != catalogue ${e.model}`);
        }
        skins[name].camera = reply.camera || null;
      }
    }
    // every bot waits until the server-side profiles carry the expected textures
    const names = leader ? Object.keys(plan.assignment) : [me];
    for (const name of names) {
      const want = leader ? [skins[name].texture_hash] : expectedHashes(plan.assignment[name], opts.catalogue, opts.mode);
      if (!want.filter(Boolean).length) {
        assignment.applied[name] = false;
        continue;
      }
      const w = await waitForTexture(rcon, name, want, opts.waitMs);
      assignment.applied[name] = w.ok;
      if (w.ok) {
        skins[name].texture_hash = w.status.texture_hash;
        if (w.status.mode) skins[name].texture_mode = w.status.mode;
      }
      if (!w.ok && !leader && w.status && w.status.skin_id && w.status.skin_id !== skins[name].skin_id) {
        // the leader chose differently (pool changed between the two reads): report what is on the server
        const e = findSkin(opts, w.status.skin_id);
        if (e && expectedHashes(e, opts.catalogue, opts.mode).includes(w.status.texture_hash)) {
          skins[name] = describe(e, opts.catalogue, opts.mode);
          skins[name].texture_hash = w.status.texture_hash;
          assignment.applied[name] = true;
          assignment.errors.push(`${name}: leader applied ${e.skin_id} (pool differed between bots)`);
          continue;
        }
      }
      if (!w.ok) assignment.errors.push(`${name}: texture not applied within ${opts.waitMs} ms`);
    }
    if (opts.settleMs > 0) await sleep(opts.settleMs);
    // Re-check after settling: an older asynchronous skin apply (e.g. the session skin of
    // `episode start`) could land after ours. The leader re-applies once; the follower
    // waits for that.
    for (const name of names) {
      const want = skins[name].texture_hash;
      if (!want || !assignment.applied[name]) continue;
      const st = await skinStatus(rcon, name);
      if (st && st.texture_hash === want) continue;
      console.warn(`[${me}] [skins] ${name} lost its texture after settling (now ${st && st.texture_hash}); re-applying`);
      if (leader) await setSkin(rcon, name, skins[name].skin_id, { note: false, mode: opts.mode });
      const w = await waitForTexture(rcon, name, want, opts.waitMs);
      assignment.applied[name] = w.ok;
      if (!w.ok) assignment.errors.push(`${name}: texture overwritten after apply`);
    }
  } catch (err) {
    assignment.errors.push(String(err && err.message ? err.message : err));
  }
  assignment.ok = assignment.errors.length === 0;
  assignment.duration_ms = Date.now() - t0;
  console.log(
    `[${me}] [skins] episode ${episodeNum}: ` +
      Object.entries(skins)
        .map(([n, s]) => `${n}=${s.skin_id}(${s.model})`)
        .join(" ") +
      ` ok=${assignment.ok}${assignment.errors.length ? " errors=" + assignment.errors.join("; ") : ""}`,
  );
  return { skins, skin_assignment: assignment };
}

/**
 * Optional: annotate the open world log with the episode's skins (leader, right after
 * worldStateBegin). Uses the same JSON note format as state-common.note().
 */
async function noteEpisodeSkins(rcon, bot, episodeNum, meta) {
  if (!meta || !meta.skins) return;
  const payload = { ep: episodeNum, bot: bot.username, phase: "skins" };
  for (const [name, s] of Object.entries(meta.skins)) payload[name] = `${s.skin_id}:${s.model}`;
  try {
    await rcon.send(`worldstate note ${JSON.stringify(payload).slice(0, 900)}`);
  } catch (err) {
    console.warn(`[skins] note failed: ${err.message}`);
  }
}

module.exports = {
  DEFAULT_CATALOGUE,
  assignEpisodeSkins,
  chooseEpisodeSkins,
  expectedHashes,
  loadCatalogue,
  localHashOf,
  noteEpisodeSkins,
  setSkin,
  skinOptions,
  textureHashOf,
  skinPool,
  skinStatus,
};
