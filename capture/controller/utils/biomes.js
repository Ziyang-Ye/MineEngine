/**
 * biomes.js - Per-episode biome diversity for NORMAL worlds (round 2).
 *
 * Before an episode the leader picks a target biome from a weighted list
 * covering the overworld surface biomes, finds it with
 *   /execute positioned <x> 64 <z> run locate biome minecraft:<id>
 * from a seeded random origin, and teleports both bots next to it with
 * /spreadplayers (spreadplayers never places players on fluids, so river /
 * ocean-shore targets put the bots on the bank or the beach).
 *
 * Determinism: the plan (target biome, fallback order, search origin) comes
 * from a separate seeded RNG `${bot_rng_seed}_biome_${episode}`; the shared
 * bot RNG is never touched. Which candidate is actually used depends on the
 * locate results (fallbacks), so the leader passes the result to the other
 * bot (see the wiring notes in windows/schema_parts/r2-world-diversity.md).
 *
 * Episode classes may declare
 *   static PREFERRED_BIOMES = ["swamp", "@water", ...]   // restrict the choice
 *   static AVOID_BIOMES = ["@dry"]                       // remove candidates
 * Entries are biome ids (with or without "minecraft:") or groups ("@cold",
 * "@dry", "@water", "@trees", "@open", "@shore", "@mountain").
 *
 * Nothing here may break an episode: every RCON call is wrapped, every wait
 * has a timeout, failures return null and the caller falls back to the old
 * random spreadplayers teleport.
 */

const seedrandom = require("seedrandom");
const { Vec3 } = require("vec3");

// ---------------------------------------------------------------------------
// Biome table
// ---------------------------------------------------------------------------

/**
 * Overworld surface biomes. `w` = sampling weight, `t` = base temperature
 * (1.21 data, for the rain/snow decision), `p` = has precipitation,
 * `g` = groups, `shore` = locate this biome, then the nearest beach from
 * there (bots stand on the beach next to that ocean type).
 * Weights favour common, visually distinct biomes; very rare biomes (slow
 * locate, up to ~16 s for a miss over the 6400-block search) get small
 * weights.
 */
const BIOMES = [
  // temperate / open
  { id: "plains", w: 6, t: 0.8, p: true, g: ["open", "temperate"] },
  { id: "sunflower_plains", w: 3, t: 0.8, p: true, g: ["open", "temperate"] },
  { id: "meadow", w: 3, t: 0.5, p: true, g: ["open", "mountain", "temperate"] },
  { id: "cherry_grove", w: 3, t: 0.5, p: true, g: ["trees", "mountain", "temperate"] },
  // forests
  { id: "forest", w: 5, t: 0.7, p: true, g: ["trees", "temperate"] },
  { id: "flower_forest", w: 3, t: 0.7, p: true, g: ["trees", "temperate"] },
  { id: "birch_forest", w: 4, t: 0.6, p: true, g: ["trees", "temperate"] },
  { id: "old_growth_birch_forest", w: 2, t: 0.6, p: true, g: ["trees", "temperate"] },
  { id: "dark_forest", w: 4, t: 0.7, p: true, g: ["trees", "temperate"] },
  { id: "taiga", w: 4, t: 0.25, p: true, g: ["trees", "temperate"] },
  { id: "old_growth_pine_taiga", w: 2, t: 0.3, p: true, g: ["trees", "temperate"] },
  { id: "old_growth_spruce_taiga", w: 2, t: 0.25, p: true, g: ["trees", "temperate"] },
  { id: "windswept_forest", w: 1, t: 0.2, p: true, g: ["trees", "mountain"] },
  // jungles
  { id: "jungle", w: 3, t: 0.95, p: true, g: ["trees", "warm"] },
  { id: "sparse_jungle", w: 2, t: 0.95, p: true, g: ["trees", "warm"] },
  { id: "bamboo_jungle", w: 2, t: 0.95, p: true, g: ["trees", "warm"] },
  // wet
  { id: "swamp", w: 4, t: 0.8, p: true, g: ["water", "trees", "temperate"] },
  { id: "mangrove_swamp", w: 3, t: 0.8, p: true, g: ["water", "trees", "warm"] },
  { id: "river", w: 3, t: 0.5, p: true, g: ["water", "shore", "temperate"] },
  { id: "beach", w: 4, t: 0.8, p: true, g: ["water", "shore", "open"] },
  { id: "stony_shore", w: 2, t: 0.2, p: true, g: ["water", "shore", "mountain"] },
  { id: "warm_ocean", w: 1, t: 0.5, p: true, g: ["water", "shore", "warm"], shore: true },
  { id: "lukewarm_ocean", w: 1, t: 0.5, p: true, g: ["water", "shore"], shore: true },
  { id: "ocean", w: 1, t: 0.5, p: true, g: ["water", "shore"], shore: true },
  { id: "cold_ocean", w: 1, t: 0.5, p: true, g: ["water", "shore"], shore: true },
  { id: "mushroom_fields", w: 0.5, t: 0.9, p: true, g: ["open", "rare"] },
  // dry (no precipitation)
  { id: "desert", w: 5, t: 2.0, p: false, g: ["dry", "open", "warm"] },
  { id: "savanna", w: 4, t: 2.0, p: false, g: ["dry", "open", "warm"] },
  { id: "savanna_plateau", w: 2, t: 2.0, p: false, g: ["dry", "open", "warm"] },
  { id: "windswept_savanna", w: 1, t: 2.0, p: false, g: ["dry", "mountain", "warm"] },
  { id: "badlands", w: 3, t: 2.0, p: false, g: ["dry", "open", "warm"] },
  { id: "wooded_badlands", w: 2, t: 2.0, p: false, g: ["dry", "trees", "warm"] },
  { id: "eroded_badlands", w: 1, t: 2.0, p: false, g: ["dry", "mountain", "warm", "rare"] },
  // mountains (temperate)
  { id: "windswept_hills", w: 2, t: 0.2, p: true, g: ["mountain", "open"] },
  { id: "windswept_gravelly_hills", w: 1, t: 0.2, p: true, g: ["mountain", "open"] },
  { id: "stony_peaks", w: 1, t: 1.0, p: true, g: ["mountain"] },
  // cold / snowy
  { id: "snowy_plains", w: 4, t: 0.0, p: true, g: ["cold", "open"] },
  { id: "ice_spikes", w: 2, t: 0.0, p: true, g: ["cold", "open", "rare"] },
  { id: "snowy_taiga", w: 3, t: -0.5, p: true, g: ["cold", "trees"] },
  { id: "snowy_beach", w: 2, t: 0.05, p: true, g: ["cold", "water", "shore"] },
  { id: "frozen_river", w: 1, t: 0.0, p: true, g: ["cold", "water", "shore"] },
  { id: "grove", w: 2, t: -0.2, p: true, g: ["cold", "trees", "mountain"] },
  { id: "snowy_slopes", w: 2, t: -0.3, p: true, g: ["cold", "mountain"] },
  { id: "frozen_peaks", w: 0.5, t: -0.7, p: true, g: ["cold", "mountain"] },
  { id: "jagged_peaks", w: 0.5, t: -0.7, p: true, g: ["cold", "mountain"] },
];

/** Biomes that are not used as targets but may be found at the bots' feet. */
const OTHER_BIOME_TEMPS = {
  deep_ocean: 0.5,
  deep_lukewarm_ocean: 0.5,
  deep_cold_ocean: 0.5,
  frozen_ocean: 0.0,
  deep_frozen_ocean: 0.5,
  dripstone_caves: 0.8,
  lush_caves: 0.5,
  deep_dark: 0.8,
};

const BIOME_BY_ID = Object.fromEntries(BIOMES.map((b) => [b.id, b]));
const ALL_BIOME_IDS = [...BIOMES.map((b) => b.id), ...Object.keys(OTHER_BIOME_TEMPS)];
const DRY_BIOMES = new Set(BIOMES.filter((b) => !b.p).map((b) => b.id));
const BIOME_GROUPS = (() => {
  const groups = {};
  for (const b of BIOMES) {
    for (const g of b.g) (groups[g] = groups[g] || []).push(b.id);
  }
  return groups;
})();

// ---------------------------------------------------------------------------
// Small helpers
// ---------------------------------------------------------------------------

/** "minecraft:plains" / "Plains" -> "plains". */
function normalizeBiomeId(id) {
  if (id === undefined || id === null) return null;
  let s = String(id).trim().toLowerCase();
  if (s.startsWith("#")) return s; // tag, keep as is
  const i = s.indexOf(":");
  if (i >= 0) s = s.slice(i + 1);
  return s || null;
}

function envOr(args, key, envKey, dflt) {
  const v = args && args[key] !== undefined && args[key] !== null && args[key] !== "" ? args[key] : process.env[envKey];
  if (v === undefined || v === null || v === "") return dflt;
  return v;
}

function numOr(v, dflt) {
  const n = Number(v);
  return Number.isFinite(n) ? n : dflt;
}

function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

/** Promise with a timeout; rejects with Error("timeout ...") when it fires. */
function withTimeout(promise, ms, what) {
  let timer;
  return Promise.race([
    promise,
    new Promise((_, reject) => {
      timer = setTimeout(() => reject(new Error(`timeout after ${ms} ms: ${what}`)), ms);
    }),
  ]).finally(() => clearTimeout(timer));
}

/** Strip Minecraft formatting codes from an RCON reply. */
function stripFormatting(text) {
  return String(text === undefined || text === null ? "" : text).replace(/§./g, "");
}

/**
 * Expand PREFERRED_BIOMES / AVOID_BIOMES entries (ids or "@group") to ids.
 * Unknown ids are kept (an episode may ask for any surface biome).
 * @param {string|string[]|null|undefined} spec
 * @returns {string[]|null} null when nothing was specified
 */
function expandBiomeSpec(spec) {
  if (spec === undefined || spec === null) return null;
  const list = Array.isArray(spec) ? spec : String(spec).split(",");
  const out = [];
  for (const raw of list) {
    const s = String(raw).trim();
    if (!s) continue;
    if (s.startsWith("@")) {
      for (const id of BIOME_GROUPS[s.slice(1).toLowerCase()] || []) out.push(id);
    } else {
      const id = normalizeBiomeId(s);
      if (id && !id.startsWith("#")) out.push(id);
    }
  }
  return out.length ? [...new Set(out)] : null;
}

// ---------------------------------------------------------------------------
// Precipitation (rain vs snow vs none)
// ---------------------------------------------------------------------------

/**
 * Base temperature of a biome (1.21 data), null if unknown.
 * @param {string} biome
 * @returns {number|null}
 */
function biomeTemperature(biome) {
  const id = normalizeBiomeId(biome);
  if (!id) return null;
  if (BIOME_BY_ID[id]) return BIOME_BY_ID[id].t;
  if (id in OTHER_BIOME_TEMPS) return OTHER_BIOME_TEMPS[id];
  return null;
}

/**
 * What falls at (biome, y) while it is raining: "none" (dry biome), "snow"
 * or "rain". Uses the vanilla height-adjusted temperature without the
 * small per-column noise (+-8 blocks of altitude), so it is an estimate
 * close to the 0.15 threshold.
 * @param {string} biome
 * @param {number} [y]
 * @returns {"none"|"rain"|"snow"|"unknown"}
 */
function precipitationType(biome, y = 64) {
  const id = normalizeBiomeId(biome);
  if (!id) return "unknown";
  if (DRY_BIOMES.has(id)) return "none";
  const t = biomeTemperature(id);
  if (t === null) return "unknown";
  const yy = Number.isFinite(Number(y)) ? Number(y) : 64;
  const adjusted = yy > 80 ? t - ((yy - 80) * 0.05) / 40 : t;
  return adjusted < 0.15 ? "snow" : "rain";
}

/** True if the biome is snowy at ordinary heights. */
function isColdBiome(biome) {
  return precipitationType(biome, 64) === "snow";
}

// ---------------------------------------------------------------------------
// Plan (pure, deterministic)
// ---------------------------------------------------------------------------

/**
 * Weighted order of candidates: weighted sampling without replacement.
 * @param {Array<{id:string,w:number}>} entries
 * @param {Function} rng
 * @returns {string[]}
 */
function weightedOrder(entries, rng) {
  const pool = entries.filter((e) => e.w > 0).map((e) => ({ ...e }));
  const out = [];
  while (pool.length) {
    const total = pool.reduce((s, e) => s + e.w, 0);
    let u = rng() * total;
    let idx = pool.length - 1;
    for (let i = 0; i < pool.length; i++) {
      u -= pool[i].w;
      if (u < 0) {
        idx = i;
        break;
      }
    }
    out.push(pool[idx].id);
    pool.splice(idx, 1);
  }
  return out;
}

/**
 * Deterministic biome plan for one episode (same on both bots).
 * @param {Object} args - bot_rng_seed, teleport_radius, biome_* overrides
 * @param {number} episodeNum
 * @param {Function|null} episodeClass - may define PREFERRED_BIOMES / AVOID_BIOMES
 * @param {Object} [opts]
 * @param {string} [opts.weatherRequested] - "snow" restricts to cold biomes
 * @param {{x:number,z:number}} [opts.center] - centre of the origin disc
 * @param {number} [opts.radius] - radius of the origin disc
 * @returns {{candidates: string[], origin: {x:number,z:number}, preferred: string[]|null,
 *   avoided: string[]|null, restricted_by: string[]}}
 */
function planEpisodeBiomes(args, episodeNum, episodeClass = null, opts = {}) {
  const rng = seedrandom(`${args.bot_rng_seed}_biome_${episodeNum}`);
  // Fixed draw order: origin angle, origin distance, then the permutation.
  const a = rng() * 2 * Math.PI;
  const d = Math.sqrt(rng());
  const center = opts.center || { x: 0, z: 0 };
  const radius = Math.max(0, numOr(opts.radius, 2000));
  const origin = {
    x: Math.floor(center.x + d * radius * Math.cos(a)),
    z: Math.floor(center.z + d * radius * Math.sin(a)),
  };

  const restrictedBy = [];
  let entries = BIOMES.map((b) => ({ id: b.id, w: b.w }));

  // Global override (comma list / groups), e.g. BIOME_WHITELIST=@cold,desert
  const whitelist = expandBiomeSpec(envOr(args, "biome_whitelist", "BIOME_WHITELIST", null));
  if (whitelist) {
    entries = whitelist.map((id) => ({ id, w: BIOME_BY_ID[id] ? BIOME_BY_ID[id].w : 1 }));
    restrictedBy.push("biome_whitelist");
  }

  const preferred = expandBiomeSpec(episodeClass ? episodeClass.PREFERRED_BIOMES : null);
  if (preferred) {
    const inter = entries.filter((e) => preferred.includes(e.id));
    entries = inter.length
      ? inter
      : preferred.map((id) => ({ id, w: BIOME_BY_ID[id] ? BIOME_BY_ID[id].w : 1 }));
    restrictedBy.push("PREFERRED_BIOMES");
  }
  const avoided = expandBiomeSpec(episodeClass ? episodeClass.AVOID_BIOMES : null);
  if (avoided) {
    const rest = entries.filter((e) => !avoided.includes(e.id));
    if (rest.length) {
      entries = rest;
      restrictedBy.push("AVOID_BIOMES");
    }
  }
  if (opts.weatherRequested === "snow") {
    const cold = entries.filter((e) => isColdBiome(e.id));
    if (cold.length) {
      entries = cold;
      restrictedBy.push("weather_snow");
    }
  }

  return {
    candidates: weightedOrder(entries, rng),
    origin,
    preferred,
    avoided,
    restricted_by: restrictedBy,
  };
}

// ---------------------------------------------------------------------------
// RCON locate
// ---------------------------------------------------------------------------

const LOCATE_OK_RE =
  /nearest\s+(#?[\w:./-]+)(?:\s+\(([\w:./-]+)\))?\s+is\s+at\s+\[\s*(-?\d+|~)\s*,\s*(-?\d+|~)\s*,\s*(-?\d+|~)\s*\]\s*\((\d+)\s+blocks?\s+away\)/i;

/**
 * Parse a `/locate biome` reply.
 *   "The nearest minecraft:cherry_grove is at [96, 126, -96] (160 blocks away)"
 *   "The nearest #minecraft:is_beach (minecraft:beach) is at [-256, 126, -1024] (1086 blocks away)"
 *   "Could not find a biome of type "minecraft:x" within reasonable distance"
 * @param {string} text
 * @returns {{found: boolean, biome: string|null, query: string|null, x: number|null,
 *   y: number|null, z: number|null, distance: number|null, error: string|null}}
 */
function parseLocateResponse(text) {
  const s = stripFormatting(text).replace(/\s+/g, " ").trim();
  const m = s.match(LOCATE_OK_RE);
  if (m) {
    const toNum = (v) => (v === "~" ? null : Number(v));
    const query = m[1];
    const biome = normalizeBiomeId(m[2] || m[1]);
    return {
      found: true,
      biome,
      query,
      x: toNum(m[3]),
      y: toNum(m[4]),
      z: toNum(m[5]),
      distance: Number(m[6]),
      error: null,
    };
  }
  let error = "unparsed";
  if (/could not find|couldn't find|not find a biome/i.test(s)) error = "not_found";
  else if (/can't find element|unknown|incorrect|invalid|expected/i.test(s)) error = "bad_request";
  return { found: false, biome: null, query: null, x: null, y: null, z: null, distance: null, error };
}

/**
 * Run `/execute positioned <x> <y> <z> run locate biome <id>` over RCON.
 * @param {*} rcon - rcon-client instance (send(cmd) -> Promise<string>)
 * @param {string} biomeId - id ("cherry_grove", "minecraft:x") or tag ("#minecraft:is_beach")
 * @param {{x:number,z:number,y?:number}} origin
 * @param {Object} [opts]
 * @param {number} [opts.timeoutMs=20000]
 * @returns {Promise<Object>} parseLocateResponse(...) + {ms, cmd, raw}
 */
async function locateBiome(rcon, biomeId, origin, { timeoutMs = 20000 } = {}) {
  const id = String(biomeId).startsWith("#")
    ? String(biomeId)
    : `minecraft:${normalizeBiomeId(biomeId)}`;
  const y = Number.isFinite(Number(origin.y)) ? Math.floor(origin.y) : 64;
  const cmd = `execute positioned ${Math.floor(origin.x)} ${y} ${Math.floor(origin.z)} run locate biome ${id}`;
  const t0 = Date.now();
  let raw = null;
  let res;
  try {
    raw = await withTimeout(Promise.resolve(rcon.send(cmd)), timeoutMs, cmd);
    res = parseLocateResponse(raw);
  } catch (err) {
    res = parseLocateResponse("");
    res.error = /timeout/i.test(err.message) ? "timeout" : `rcon_error: ${err.message}`;
  }
  return { ...res, ms: Date.now() - t0, cmd, raw: raw === null ? null : stripFormatting(raw) };
}

/**
 * Wait until the server answers a cheap command again (a timed-out /locate
 * keeps the main thread busy until it finishes). Returns the wait in ms, or
 * -1 if the server did not answer within `timeoutMs`.
 */
async function drainServer(rcon, timeoutMs = 60000) {
  const t0 = Date.now();
  try {
    await withTimeout(Promise.resolve(rcon.send("list")), timeoutMs, "drain list");
    return Date.now() - t0;
  } catch (err) {
    return -1;
  }
}

// Biomes that failed to be located in this process (leader only): not found
// (a timeout is not counted, see chooseEpisodeSite). Two misses in a row skip the biome for the next
// MISS_SKIP_EPISODES episodes (a miss costs a full 6400-block search, ~10-20 s
// of server main-thread time).
const _locateMisses = new Map();
const MISS_SKIP_THRESHOLD = 2;
const MISS_SKIP_EPISODES = 10;

function _recordLocate(id, found, episodeNum) {
  if (found) {
    _locateMisses.delete(id);
    return;
  }
  const prev = _locateMisses.get(id) || { count: 0, lastEp: episodeNum };
  _locateMisses.set(id, { count: prev.count + 1, lastEp: episodeNum });
}

function _shouldSkip(id, episodeNum) {
  const m = _locateMisses.get(id);
  if (!m || m.count < MISS_SKIP_THRESHOLD) return false;
  if (Math.abs(episodeNum - m.lastEp) >= MISS_SKIP_EPISODES) {
    // Give it another chance (one more miss skips it again).
    _locateMisses.set(id, { count: MISS_SKIP_THRESHOLD - 1, lastEp: m.lastEp });
    return false;
  }
  return true;
}

// Biomes whose locate was slow (far from the random origin; up to ~16 s of
// server main-thread time for badlands / desert in some seeds). Later
// episodes search them from a seeded point near the remembered site, which
// is fast and still lands on a different part of the biome.
const _slowSites = new Map();
const SLOW_LOCATE_MS = 2000;
const NEAR_ORIGIN_RADIUS = 1200;

/** Forget remembered misses and slow sites (tests). */
function resetLocateMemory() {
  _locateMisses.clear();
  _slowSites.clear();
}

/**
 * Origin of the locate for one candidate: the plan's random origin, or a
 * seeded point near a remembered slow site of that biome.
 */
function _originFor(args, episodeNum, id, planOrigin) {
  const slow = _slowSites.get(id);
  if (!slow) return { origin: planOrigin, mode: "random" };
  const rng = seedrandom(`${args.bot_rng_seed}_biome_${episodeNum}_near_${id}`);
  const a = rng() * 2 * Math.PI;
  const d = Math.sqrt(rng()) * NEAR_ORIGIN_RADIUS;
  return {
    origin: { x: Math.floor(slow.x + d * Math.cos(a)), z: Math.floor(slow.z + d * Math.sin(a)) },
    mode: "near_known_site",
  };
}

/**
 * Leader only: choose and locate the episode's biome site (normal worlds).
 *
 * @param {*} bot - leader bot (used for the origin centre and logging)
 * @param {*} rcon
 * @param {Object} args - world_type, bot_rng_seed, teleport_radius; optional
 *   biome_diversity (0 disables), biome_locate_budget_ms, biome_locate_timeout_ms,
 *   biome_max_attempts, biome_origin_radius, biome_whitelist (or the env vars
 *   BIOME_DIVERSITY, BIOME_LOCATE_BUDGET_MS, BIOME_LOCATE_TIMEOUT_MS,
 *   BIOME_MAX_ATTEMPTS, BIOME_ORIGIN_RADIUS, BIOME_WHITELIST)
 * @param {number} episodeNum
 * @param {Function|null} episodeClass - may define PREFERRED_BIOMES / AVOID_BIOMES
 * @param {Object} [opts]
 * @param {string} [opts.weatherRequested] - from chooseEpisodeEnvironment (computed if omitted)
 * @returns {Promise<{x:number, z:number, y:number|null, biome_target:string,
 *   biome_found:string, locate_ms:number, distance:number, origin:{x:number,z:number},
 *   attempts:Array<Object>, restricted_by:string[], via_beach:boolean}|null>}
 */
async function chooseEpisodeSite(bot, rcon, args, episodeNum, episodeClass = null, opts = {}) {
  const name = bot && bot.username ? bot.username : "bot";
  const worldType = String(args.world_type || "flat").toLowerCase();
  if (worldType === "flat") return null;
  if (String(envOr(args, "biome_diversity", "BIOME_DIVERSITY", "1")) === "0") return null;

  let weatherRequested = opts.weatherRequested;
  if (weatherRequested === undefined) {
    try {
      // Lazy require: env-setup requires this module for precipitation.
      const { chooseEpisodeEnvironment } = require("./env-setup");
      weatherRequested = chooseEpisodeEnvironment(args, episodeNum, { episodeClass })
        .weather_requested;
    } catch (err) {
      weatherRequested = null;
    }
  }

  // Origin disc centre: the teleport centre of episodes-loop.js if set, else
  // the leader's position at the first call (kept for the whole run, so the
  // search area does not drift with the bots).
  const pos = bot && bot.entity && bot.entity.position;
  if (bot && !bot._teleport_center && !bot._biomeOriginCenter) {
    bot._biomeOriginCenter = pos ? { x: Math.floor(pos.x), z: Math.floor(pos.z) } : { x: 0, z: 0 };
  }
  const center = (bot && (bot._teleport_center || bot._biomeOriginCenter)) || { x: 0, z: 0 };
  const radius = Math.max(
    numOr(bot && bot._teleport_radius, 0),
    numOr(args.teleport_radius, 0),
    numOr(envOr(args, "biome_origin_radius", "BIOME_ORIGIN_RADIUS", 2000), 2000),
  );
  const plan = planEpisodeBiomes(args, episodeNum, episodeClass, {
    weatherRequested,
    center,
    radius,
  });
  // Total budget (locates + breathers). A /locate runs on the server main
  // thread (up to ~16 s for a miss over the 6400-block search): the caller
  // should pass a dedicated RCON connection with a long packet timeout
  // (episodes-loop.js getLocateRcon), so a slow search is not mistaken for a
  // miss and the next locate does not queue behind it.
  const budgetMs = numOr(envOr(args, "biome_locate_budget_ms", "BIOME_LOCATE_BUDGET_MS", 28000), 28000);
  const maxAttempts = Math.max(1, numOr(envOr(args, "biome_max_attempts", "BIOME_MAX_ATTEMPTS", 4), 4));
  // Per-locate controller wait. After a timeout the server is still
  // searching: drainServer() waits until it answers again before the next
  // locate, and a timeout is NOT counted as a miss (only "not found" is).
  const locateTimeoutMs = numOr(envOr(args, "biome_locate_timeout_ms", "BIOME_LOCATE_TIMEOUT_MS", 25000), 25000);
  // Pause after a slow locate so the server ticks (and sends packets to the
  // camera clients) between two main-thread stalls.
  const breatherMs = numOr(envOr(args, "biome_locate_breather_ms", "BIOME_LOCATE_BREATHER_MS", 1500), 1500);
  const target = plan.candidates[0];
  const attempts = [];
  const t0 = Date.now();

  let tried = 0;
  for (let i = 0; i < plan.candidates.length && tried < maxAttempts; i++) {
    const id = plan.candidates[i];
    // Skip remembered misses, but never skip the last remaining chance.
    if (_shouldSkip(id, episodeNum) && i < plan.candidates.length - 1) {
      attempts.push({ biome: id, skipped: "recent_miss" });
      continue;
    }
    const remaining = budgetMs - (Date.now() - t0);
    if (remaining < 1000) break;
    tried++;
    const { origin, mode } = _originFor(args, episodeNum, id, plan.origin);
    const res = await locateBiome(rcon, id, origin, { timeoutMs: Math.min(remaining, locateTimeoutMs) });
    attempts.push({ biome: id, found: res.found, ms: res.ms, distance: res.distance, error: res.error, origin_mode: mode });
    console.log(
      `[${name}] [biome] ep ${episodeNum} locate ${id} from (${origin.x}, ${origin.z}, ${mode}): ${res.raw} (${res.ms} ms)`,
    );
    if (res.error === "timeout" || (res.error && res.error.startsWith("rcon_error"))) {
      // still searching on the server (or the connection failed): wait until
      // it answers again; not a miss
      const drained = await drainServer(rcon);
      console.log(`[${name}] [biome] ep ${episodeNum} drained after ${res.error}: ${drained} ms`);
    } else {
      _recordLocate(id, res.found, episodeNum);
    }
    if (res.ms > SLOW_LOCATE_MS && breatherMs > 0) await sleep(breatherMs);
    if (!res.found) continue;
    if (res.ms > SLOW_LOCATE_MS && !_slowSites.has(id)) _slowSites.set(id, { x: res.x, z: res.z, ms: res.ms });

    let site = { x: res.x, y: res.y, z: res.z, biome: res.biome, distance: res.distance };
    let viaBeach = false;
    if (BIOME_BY_ID[id] && BIOME_BY_ID[id].shore) {
      // Ocean target: stand on the nearest beach of that ocean.
      const remaining2 = budgetMs - (Date.now() - t0);
      if (remaining2 < 500) break;
      const beach = await locateBiome(rcon, "#minecraft:is_beach", { x: res.x, z: res.z }, {
        timeoutMs: Math.min(remaining2, 15000),
      });
      attempts.push({ biome: "#minecraft:is_beach", found: beach.found, ms: beach.ms, distance: beach.distance, error: beach.error });
      console.log(`[${name}] [biome] ep ${episodeNum} beach next to ${id}: ${beach.raw} (${beach.ms} ms)`);
      if (beach.error === "timeout") await drainServer(rcon);
      if (!beach.found || beach.distance > 400) continue;
      site = { x: beach.x, y: beach.y, z: beach.z, biome: beach.biome, distance: res.distance };
      viaBeach = true;
    }
    const result = {
      x: site.x,
      z: site.z,
      y: site.y,
      biome_target: target,
      biome_found: id,
      biome_located: site.biome,
      locate_ms: Date.now() - t0,
      distance: site.distance,
      origin,
      origin_mode: mode,
      attempts,
      restricted_by: plan.restricted_by,
      via_beach: viaBeach,
      weather_requested: weatherRequested || null,
    };
    console.log(`[${name}] [biome] ep ${episodeNum} site: ${JSON.stringify(result)}`);
    return result;
  }
  console.warn(
    `[${name}] [biome] ep ${episodeNum}: no biome site (target ${target}, ${attempts.length} attempts, ${Date.now() - t0} ms); falling back to a random teleport`,
  );
  if (bot) bot._lastBiomeSiteFailure = { biome_target: target, attempts, locate_ms: Date.now() - t0 };
  return null;
}

// ---------------------------------------------------------------------------
// Reading the actual biome
// ---------------------------------------------------------------------------

/**
 * Biome at a position as seen by the bot's client (dynamic registry of the
 * server, so the names match /locate). Synchronous; null if unloaded.
 * @param {*} bot
 * @param {{x:number,y:number,z:number}} pos
 * @returns {string|null} biome id without namespace
 */
function biomeAt(bot, pos) {
  try {
    if (!bot || !pos) return null;
    const block = bot.blockAt(new Vec3(Math.floor(pos.x), Math.floor(pos.y), Math.floor(pos.z)));
    const b = block && block.biome;
    if (!b) return null;
    const name = normalizeBiomeId(b.name);
    if (name && name !== "unknown") return name;
    if (Number.isInteger(b.id) && bot.registry && bot.registry.biomes && bot.registry.biomes[b.id]) {
      return normalizeBiomeId(bot.registry.biomes[b.id].name);
    }
    return null;
  } catch (err) {
    return null;
  }
}

/**
 * Authoritative biome at a position via `/execute if biome` (server side).
 * Tries the hints first, then every known overworld biome (~1 ms each).
 * @param {*} rcon
 * @param {{x:number,y:number,z:number}} pos
 * @param {string[]} [hints]
 * @param {Object} [opts]
 * @param {number} [opts.timeoutMs=8000] - total budget
 * @returns {Promise<string|null>}
 */
async function biomeAtRcon(rcon, pos, hints = [], { timeoutMs = 8000 } = {}) {
  if (!rcon || !pos) return null;
  const x = Math.floor(pos.x);
  const y = Math.floor(pos.y);
  const z = Math.floor(pos.z);
  const order = [...new Set([...hints.map(normalizeBiomeId).filter(Boolean), ...ALL_BIOME_IDS])];
  const t0 = Date.now();
  for (const id of order) {
    if (id.startsWith("#")) continue;
    if (Date.now() - t0 > timeoutMs) return null;
    try {
      const res = stripFormatting(
        await withTimeout(Promise.resolve(rcon.send(`execute if biome ${x} ${y} ${z} minecraft:${id}`)), 3000, "if biome"),
      );
      if (/test passed/i.test(res)) return id;
      if (/not loaded|unknown|incorrect|can't find/i.test(res)) return null;
    } catch (err) {
      return null;
    }
  }
  return null;
}

/**
 * Biome at a position. The server answer (`execute if biome`) is the real
 * gameplay biome (it applies the vanilla "fuzzy" biome zoom); the client view
 * (raw 4x4x4 cell) is used as the first hint and as a fallback when RCON
 * cannot answer (unloaded, no rcon). They differ near biome borders.
 * @param {*} bot
 * @param {*} rcon - may be null
 * @param {{x:number,y:number,z:number}} [pos] - default: bot feet
 * @param {string[]} [hints]
 * @returns {Promise<{biome: string|null, source: "rcon"|"client"|null, client_biome: string|null}>}
 */
async function resolveBiome(bot, rcon, pos = null, hints = []) {
  const p = pos || (bot && bot.entity ? bot.entity.position : null);
  if (!p) return { biome: null, source: null, client_biome: null };
  const client = biomeAt(bot, p);
  const server = rcon ? await biomeAtRcon(rcon, p, [client, ...hints].filter(Boolean)) : null;
  if (server) return { biome: server, source: "rcon", client_biome: client };
  return { biome: client, source: client ? "client" : null, client_biome: client };
}

// ---------------------------------------------------------------------------
// Teleport to the located site
// ---------------------------------------------------------------------------

async function _spread(bot, rcon, x, z, minDistance, maxRange) {
  const cmd = `spreadplayers ${Math.floor(x)} ${Math.floor(z)} ${minDistance} ${maxRange} false @a[tag=minebot]`;
  let reply;
  try {
    reply = stripFormatting(await withTimeout(Promise.resolve(rcon.send(cmd)), 15000, cmd));
  } catch (err) {
    reply = `error: ${err.message}`;
  }
  console.log(`[${bot.username}] [biome] ${cmd} -> ${reply}`);
  return { ok: /^Spread 2 player/i.test(reply), reply };
}

/**
 * Probe loaded columns around (x, z) for the target biome (server side).
 * Returns the first {x, z} on rings of 16..48 blocks (8 directions) where the
 * biome at height y is the target, or null.
 */
async function _probeTargetNearby(rcon, x, y, z, target, { maxMs = 4000 } = {}) {
  const t0 = Date.now();
  for (const r of [16, 32, 48]) {
    for (let k = 0; k < 8; k++) {
      if (Date.now() - t0 > maxMs) return null;
      const a = (k * Math.PI) / 4;
      const px = Math.floor(x + r * Math.cos(a));
      const pz = Math.floor(z + r * Math.sin(a));
      let res = "";
      try {
        res = stripFormatting(
          await withTimeout(Promise.resolve(rcon.send(`execute if biome ${px} ${Math.floor(y)} ${pz} minecraft:${target}`)), 3000, "if biome"),
        );
      } catch (err) {
        return null;
      }
      if (/test passed/i.test(res)) return { x: px, z: pz, ring: r };
    }
  }
  return null;
}

/**
 * Leader only: put both bots (@a[tag=minebot]) next to the located site with
 * /spreadplayers, move each bot from tree canopies down to the ground, and
 * check that the leader really stands in the located biome (the located point
 * is the nearest sample, i.e. usually on the biome border). If not, probe
 * loaded columns 16-48 blocks around for the biome and spread again there.
 * Replaces the random-centre loop of episodes-loop.js teleport() when it
 * succeeds (ok: true); on ok: false the caller falls back to that loop.
 * @param {*} bot
 * @param {*} rcon
 * @param {Object} args
 * @param {{x:number,z:number,biome_located?:string,biome_found?:string}} site - chooseEpisodeSite result
 * @param {*} episodeInstance - INIT_MIN/MAX_BOTS_DISTANCE
 * @param {Object} [opts]
 * @param {number} [opts.settleWaitMs=5000] - chunk loading wait after the spread
 * @param {boolean} [opts.settle=true] - move bots off canopies
 * @param {boolean} [opts.recenter=true] - re-spread into the biome if missed
 * @returns {Promise<{ok: boolean, reply: string|null, max_range: number|null,
 *   settled: Object[]|null, biome_at_leader: string|null, biome_match: boolean|null,
 *   recentered: boolean, center: number[]|null}>}
 */
async function teleportToSite(bot, rcon, args, site, episodeInstance, opts = {}) {
  const settleWaitMs = numOr(opts.settleWaitMs, 5000);
  const C = (episodeInstance && episodeInstance.constructor) || {};
  const minDistance = Math.max(1, numOr(C.INIT_MIN_BOTS_DISTANCE, 4));
  const baseRange = Math.max(minDistance + 1, Math.floor(numOr(C.INIT_MAX_BOTS_DISTANCE, 10) / 2));
  const target = normalizeBiomeId(site.biome_located || site.biome_found);
  const out = {
    ok: false,
    reply: null,
    max_range: null,
    settled: null,
    biome_at_leader: null,
    biome_match: null,
    recentered: false,
    center: null,
  };
  const settle = async () => {
    if (opts.settle === false) return null;
    try {
      const { settleBotsOnGround } = require("./site-prep");
      return await settleBotsOnGround(bot, rcon, args);
    } catch (err) {
      console.warn(`[${bot.username}] [biome] settle failed: ${err.message}`);
      return null;
    }
  };

  // Centres: the located point, then 32 / 64 blocks further from the search
  // origin (the located point is the biome sample nearest to the origin, so
  // the biome continues beyond it; helps when the border is mostly water).
  const centres = [{ x: site.x, z: site.z }];
  if (site.origin && Number.isFinite(site.origin.x) && Number.isFinite(site.origin.z)) {
    const dx = site.x - site.origin.x;
    const dz = site.z - site.origin.z;
    const len = Math.hypot(dx, dz);
    if (len > 1) {
      for (const step of [32, 64]) {
        centres.push({ x: Math.floor(site.x + (dx / len) * step), z: Math.floor(site.z + (dz / len) * step) });
      }
    }
  }
  spread: for (const c of centres) {
    for (const mult of [1, 2, 4]) {
      const maxRange = baseRange * mult;
      const res = await _spread(bot, rcon, c.x, c.z, minDistance, maxRange);
      out.reply = res.reply;
      if (!res.ok) {
        await sleep(300);
        continue;
      }
      out.ok = true;
      out.max_range = maxRange;
      out.center = [Math.floor(c.x), Math.floor(c.z)];
      break spread;
    }
  }
  if (!out.ok) return out;
  await sleep(settleWaitMs);
  out.settled = await settle();

  if (target && opts.recenter !== false && bot.entity) {
    const p = bot.entity.position;
    const r = await resolveBiome(bot, rcon, p, [target]);
    out.biome_at_leader = r.biome;
    out.biome_match = r.biome === target;
    if (r.biome && !out.biome_match) {
      const near = await _probeTargetNearby(rcon, p.x, p.y + 1, p.z, target);
      if (near) {
        const res = await _spread(bot, rcon, near.x, near.z, minDistance, out.max_range);
        if (res.ok) {
          out.reply = res.reply;
          out.recentered = true;
          out.center = [near.x, near.z];
          await sleep(Math.min(settleWaitMs, 2500));
          out.settled = await settle();
          const r2 = await resolveBiome(bot, rcon, bot.entity.position, [target]);
          out.biome_at_leader = r2.biome;
          out.biome_match = r2.biome === target;
        }
      }
    }
  }
  return out;
}

/**
 * Metadata keys for episode_info.state_metadata (both bots).
 * @param {Object|null} site - chooseEpisodeSite result (leader, or passed to the peer)
 * @param {Object} [extra] - e.g. {biome: <actual at the bot>, biome_source}
 * @returns {Object}
 */
function biomeSiteMetadata(site, extra = {}) {
  if (!site) {
    return { biome_site_source: "random", biome_target: null, biome_found: null, ...extra };
  }
  return {
    biome_site_source: site.via_beach ? "locate_beach" : "locate",
    biome_target: site.biome_target,
    biome_found: site.biome_found,
    biome_located: site.biome_located || site.biome_found,
    biome_locate_ms: site.locate_ms,
    biome_locate_attempts: Array.isArray(site.attempts) ? site.attempts.filter((a) => !a.skipped).length : null,
    biome_site_xz: [site.x, site.z],
    biome_locate_distance: site.distance,
    ...extra,
  };
}

module.exports = {
  ALL_BIOME_IDS,
  BIOMES,
  BIOME_GROUPS,
  biomeAt,
  biomeAtRcon,
  biomeSiteMetadata,
  biomeTemperature,
  chooseEpisodeSite,
  expandBiomeSpec,
  isColdBiome,
  locateBiome,
  normalizeBiomeId,
  parseLocateResponse,
  planEpisodeBiomes,
  precipitationType,
  resetLocateMemory,
  resolveBiome,
  teleportToSite,
  weightedOrder,
};
