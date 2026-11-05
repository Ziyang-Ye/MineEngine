/**
 * env-setup.js - Per-episode environment and damage policy.
 *
 * T1  time & weather: before the start snapshot the leader bot freezes the
 *     daylight and weather cycles, sets the time of day and the weather.
 *     The choice comes from a separate seeded RNG
 *     (`<bot_rng_seed>_env_<episode>`), so both bots compute the same value
 *     without consuming the shared bot RNG.
 *     Round 2 (world diversity): 85% day / 15% dusk, night or dawn (the exact
 *     tick is jittered inside each band unless TIME_JITTER=0); weather clear
 *     80%, rain 12%, thunder 3%, "snow" 5% (= rain, the biome chooser then
 *     prefers cold biomes; what actually falls is recorded as
 *     `precipitation`). Episode classes may request a time / weather with
 *     `static PREFERRED_TIME` / `static PREFERRED_WEATHER`.
 *     Weather side effects are natural game mechanics and are logged by the
 *     plugin: snow layers / ice (block_form), fire put out by rain
 *     (block_fade), lightning fires (block_ignite cause LIGHTNING, only on
 *     difficulty normal, i.e. damage episodes).
 * T14 damage: damage episode types (pvp, pve, ...) run on difficulty normal
 *     without resistance/saturation so hearts can actually be lost; all other
 *     types stay protected (peaceful, resistance 255, saturation). Natural
 *     mob spawning is off and leftover hostile mobs are killed before a
 *     damage episode, so only the episode's own mobs can deal damage.
 *
 * The values are stored in episodeInstance._envMetadata and merged with the
 * handler's _stateMetadata when episode_info is written (handler keys win).
 */

const seedrandom = require("seedrandom");
const { precipitationType } = require("./biomes");

// Time-of-day presets (ticks, /time set).
const TIME_DAY = 6000;
const TIME_DUSK = 12500;
const TIME_NIGHT = 18000;
const TIME_DAWN = 23000;
// `/time set day` (used by eval episodes with --eval_time_set_day).
const TIME_EVAL_DAY = 1000;

// Preset (TIME_JITTER=0) and jitter band [lo, hi) of each time-of-day label.
// Night stays far from dusk/dawn (dark sky); day keeps the sun high enough
// for well-lit frames.
const TIME_BANDS = {
  day: { preset: TIME_DAY, lo: 2000, hi: 10000 },
  dusk: { preset: TIME_DUSK, lo: 12000, hi: 13000 },
  night: { preset: TIME_NIGHT, lo: 15000, hi: 21000 },
  dawn: { preset: TIME_DAWN, lo: 22800, hi: 23600 },
};
// Split of the dusk/night fraction (DUSK_NIGHT_FRACTION, default 0.15).
const NON_DAY_SPLIT = [
  ["dusk", 0.42],
  ["night", 0.42],
  ["dawn", 0.16],
];

// Requested weather distribution. "snow" is applied as rain (the biome
// chooser then prefers cold biomes; flat worlds keep their biome).
const DEFAULT_WEATHER_WEIGHTS = { clear: 0.8, rain: 0.12, thunder: 0.03, snow: 0.05 };
const WEATHER_COMMAND = { clear: "clear", rain: "rain", thunder: "thunder", snow: "rain" };

// Built-in preferences for episode types whose semantics break in the rain
// (rain puts out fire under the open sky). A class-level static
// PREFERRED_TIME / PREFERRED_WEATHER wins over this table.
const EPISODE_TYPE_ENV_DEFAULTS = {
  igniteAndExtinguish: { weather: "clear" },
  iceNearFire: { weather: "clear" },
};

// Time the server needs to ramp the rain/thunder level 0 <-> 1 (0.01 / tick).
const WEATHER_RAMP_MS = 5500;

// Episode types in which the bots take real damage (T14). Handlers may also
// opt in with `static DAMAGE_ENABLED = true`.
const DAMAGE_EPISODE_TYPES = new Set([
  "pvp",
  "pve",
  "pveMobs",
  "pvpMelee",
  "pvpRanged",
]);

const DAMAGE_DIFFICULTY = "normal";
const PROTECTED_DIFFICULTY = "peaceful";

// Natural spawning is off in every episode: on normal difficulty (damage
// episodes) at dusk/night hostile mobs would otherwise spawn next to the
// bots and corrupt the labels. /summon (pve) is not affected.
const SPAWN_GAMERULES = [
  "gamerule doMobSpawning false",
  "gamerule doInsomnia false", // phantoms
  "gamerule doPatrolSpawning false",
  "gamerule doTraderSpawning false",
];

// Hostile mobs removed before a damage episode (leftovers from earlier
// episodes or from before the gamerules; peaceful removes them otherwise).
const HOSTILE_MOB_TYPES = [
  "zombie",
  "zombie_villager",
  "husk",
  "drowned",
  "skeleton",
  "stray",
  "bogged",
  "creeper",
  "spider",
  "cave_spider",
  "enderman",
  "witch",
  "slime",
  "phantom",
  "silverfish",
  "pillager",
  "vindicator",
  "evoker",
  "vex",
  "ravager",
];

/**
 * Label of a time of day in ticks (0-23999).
 * @param {number} t
 * @returns {"day"|"dusk"|"night"|"dawn"}
 */
function timeOfDayLabel(t) {
  const d = ((Math.floor(Number(t)) % 24000) + 24000) % 24000;
  if (d < 12000) return "day";
  if (d < 13800) return "dusk";
  if (d < 22200) return "night";
  return "dawn";
}

/**
 * Normalise a PREFERRED_TIME / PREFERRED_WEATHER value to a list of allowed
 * labels (null = no preference).
 * @param {string|string[]|undefined|null} spec
 * @param {string[]} known
 * @param {Object<string,string[]>} [aliases]
 * @returns {string[]|null}
 */
function allowedLabels(spec, known, aliases = {}) {
  if (spec === undefined || spec === null || spec === "") return null;
  const list = Array.isArray(spec) ? spec : String(spec).split(",");
  const out = [];
  for (const raw of list) {
    const s = String(raw).trim().toLowerCase();
    if (!s || s === "any") return null;
    if (aliases[s]) out.push(...aliases[s]);
    else if (known.includes(s)) out.push(s);
  }
  return out.length ? [...new Set(out)] : null;
}

/**
 * Parse "clear=0.8,rain=0.12,thunder=0.03,snow=0.05" (WEATHER_WEIGHTS).
 * Labels not listed get weight 0; an unparsable spec gives the defaults.
 * @param {string|Object|null|undefined} spec
 * @returns {Object<string, number>}
 */
function parseWeatherWeights(spec) {
  if (!spec) return { ...DEFAULT_WEATHER_WEIGHTS };
  if (typeof spec === "object") return { ...DEFAULT_WEATHER_WEIGHTS, ...spec };
  const out = {};
  for (const part of String(spec).split(",")) {
    const [k, v] = part.split("=").map((t) => (t || "").trim());
    const n = Number(v);
    if (k in DEFAULT_WEATHER_WEIGHTS && Number.isFinite(n) && n >= 0) out[k] = n;
  }
  if (!Object.keys(out).length) return { ...DEFAULT_WEATHER_WEIGHTS };
  const zero = Object.fromEntries(Object.keys(DEFAULT_WEATHER_WEIGHTS).map((k) => [k, 0]));
  return { ...zero, ...out };
}

/** Weighted pick from [[label, weight], ...] with u in [0, 1). */
function pickWeighted(pairs, u) {
  const total = pairs.reduce((acc, [, w]) => acc + w, 0);
  if (!(total > 0)) return pairs[0][0];
  let x = u * total;
  for (const [label, w] of pairs) {
    x -= w;
    if (x < 0) return label;
  }
  return pairs[pairs.length - 1][0];
}

/** args[key] if set, else process.env[envKey], else undefined. */
function argOrEnv(args, key, envKey) {
  const v = args ? args[key] : undefined;
  if (v !== undefined && v !== null && v !== "") return v;
  const e = process.env[envKey];
  return e === undefined || e === "" ? undefined : e;
}

/**
 * Flat-world biome from WORLD_GEN_INFO (null for normal worlds).
 * @param {Object} args
 * @returns {string|null}
 */
function flatWorldBiome(args) {
  if (String(args.world_type || "flat").toLowerCase() !== "flat") return null;
  try {
    const info = args.world_gen_info ? JSON.parse(String(args.world_gen_info)) : {};
    return info.flat_biome ? String(info.flat_biome).replace(/^minecraft:/, "") : null;
  } catch (err) {
    return null;
  }
}

/**
 * Deterministic per-episode environment (same result on both bots).
 *
 * RNG draws (always all four, so the sequence never depends on options):
 *   u1 day vs dusk/night/dawn, u2 which non-day label (or which preferred
 *   label), u3 weather, u4 tick jitter inside the label's band.
 *
 * @param {Object} args - bot_rng_seed, dusk_night_fraction, fixed_time; optional
 *   fixed_weather / FIXED_WEATHER, weather_weights / WEATHER_WEIGHTS,
 *   time_jitter / TIME_JITTER ("0" = preset ticks 6000/12500/18000/23000)
 * @param {number} episodeNum
 * @param {Object} [opts]
 * @param {boolean} [opts.evalDay=false] - eval episode with eval_time_set_day
 * @param {boolean} [opts.isEval=false] - eval episode: always clear weather
 * @param {Function} [opts.episodeClass] - PREFERRED_TIME / PREFERRED_WEATHER statics
 * @param {string} [opts.episodeType] - built-in per-type defaults (EPISODE_TYPE_ENV_DEFAULTS)
 * @returns {{time_of_day: number, time_of_day_label: string, time_policy: string,
 *   weather: string, weather_requested: string, weather_policy: string,
 *   precipitation: string, rain_level: number, thunder_level: number,
 *   daylight_cycle: boolean, weather_cycle: boolean, mob_spawning: boolean}}
 */
function chooseEpisodeEnvironment(
  args,
  episodeNum,
  { evalDay = false, isEval = false, episodeClass = null, episodeType = null } = {},
) {
  const rng = seedrandom(`${args.bot_rng_seed}_env_${episodeNum}`);
  const u1 = rng();
  const u2 = rng();
  const u3 = rng();
  const u4 = rng();
  const typeDefaults = (episodeType && EPISODE_TYPE_ENV_DEFAULTS[episodeType]) || {};
  const evalLike = Boolean(isEval) || Boolean(evalDay) || /Eval$/.test(String(episodeType || ""));

  // ---- time of day --------------------------------------------------------
  const fixedTime = Number(args.fixed_time);
  let fraction = Number(args.dusk_night_fraction);
  if (!Number.isFinite(fraction)) fraction = 0.15;
  const jitterRaw = argOrEnv(args, "time_jitter", "TIME_JITTER");
  const jitter = jitterRaw === undefined ? true : String(jitterRaw) !== "0";
  const preferredTime = allowedLabels(
    episodeClass && episodeClass.PREFERRED_TIME !== undefined
      ? episodeClass.PREFERRED_TIME
      : typeDefaults.time,
    Object.keys(TIME_BANDS),
    { dark: ["dusk", "night", "dawn"], light: ["day"] },
  );
  const tickIn = (label) => {
    const band = TIME_BANDS[label];
    return jitter ? Math.floor(band.lo + u4 * (band.hi - band.lo)) : band.preset;
  };
  let time;
  let policy;
  if (Number.isFinite(fixedTime) && fixedTime >= 0) {
    time = Math.floor(fixedTime) % 24000;
    policy = "fixed";
  } else if (evalDay) {
    time = TIME_EVAL_DAY;
    policy = "eval_day";
  } else if (preferredTime) {
    const idx = Math.min(preferredTime.length - 1, Math.floor(u2 * preferredTime.length));
    time = tickIn(preferredTime[idx]);
    policy = "preferred";
  } else if (u1 < fraction) {
    time = tickIn(pickWeighted(NON_DAY_SPLIT, u2));
    policy = "random";
  } else {
    time = tickIn("day");
    policy = "random";
  }

  // ---- weather ------------------------------------------------------------
  const known = Object.keys(DEFAULT_WEATHER_WEIGHTS);
  const fixedWeather = allowedLabels(argOrEnv(args, "fixed_weather", "FIXED_WEATHER"), known);
  const preferredWeather = allowedLabels(
    episodeClass && episodeClass.PREFERRED_WEATHER !== undefined
      ? episodeClass.PREFERRED_WEATHER
      : typeDefaults.weather,
    known,
    { dry: ["clear"], wet: ["rain", "thunder", "snow"], precipitation: ["rain", "thunder", "snow"] },
  );
  const weights = parseWeatherWeights(argOrEnv(args, "weather_weights", "WEATHER_WEIGHTS"));
  let weatherRequested;
  let weatherPolicy;
  if (fixedWeather) {
    weatherRequested = fixedWeather[0];
    weatherPolicy = "fixed";
  } else if (evalLike) {
    weatherRequested = "clear";
    weatherPolicy = "eval";
  } else if (preferredWeather) {
    const pairs = preferredWeather.map((w) => [w, weights[w] > 0 ? weights[w] : DEFAULT_WEATHER_WEIGHTS[w]]);
    weatherRequested = pickWeighted(pairs, u3);
    weatherPolicy = "preferred";
  } else {
    weatherRequested = pickWeighted(known.map((w) => [w, weights[w] || 0]), u3);
    weatherPolicy = "random";
  }
  const weather = WEATHER_COMMAND[weatherRequested] || "clear";

  // Flat worlds know their biome now; normal worlds resolve precipitation
  // once the site is known (resolvePrecipitation).
  const flatBiome = flatWorldBiome(args);
  let precipitation = weather === "clear" ? "none" : "unknown";
  if (weather !== "clear" && flatBiome) precipitation = precipitationType(flatBiome, 64);

  return {
    time_of_day: time,
    time_of_day_label: timeOfDayLabel(time),
    time_policy: policy,
    weather,
    weather_requested: weatherRequested,
    weather_policy: weatherPolicy,
    precipitation,
    rain_level: weather === "clear" ? 0 : 1,
    thunder_level: weather === "thunder" ? 1 : 0,
    daylight_cycle: false,
    weather_cycle: false,
    mob_spawning: false,
  };
}

/**
 * What actually falls at the episode site: "none" for clear weather or dry
 * biomes (desert, savanna, badlands), "snow" in cold biomes (height
 * adjusted), else "rain"; "unknown" if the biome is unknown.
 * @param {Object} env - chooseEpisodeEnvironment result
 * @param {string|null} biome - biome at the site (biomes.biomeAt / resolveBiome)
 * @param {number} [y] - site height
 * @returns {{precipitation: string, precipitation_biome: string|null}}
 */
function resolvePrecipitation(env, biome, y = 64) {
  if (!env || env.weather === "clear") {
    return { precipitation: "none", precipitation_biome: biome || null };
  }
  return {
    precipitation: biome ? precipitationType(biome, y) : "unknown",
    precipitation_biome: biome || null,
  };
}

/**
 * Client-side weather as the bot sees it (mineflayer rain/thunder state).
 * Call at the start of recording to record the real rain level.
 * @param {*} bot
 * @returns {{observed_rain_level: number|null, observed_thunder_level: number|null,
 *   observed_is_raining: boolean|null}}
 */
function weatherObservation(bot) {
  const num = (v) => (Number.isFinite(Number(v)) ? Number(Number(v).toFixed(3)) : null);
  return {
    observed_rain_level: num(bot && bot.rainState),
    observed_thunder_level: num(bot && bot.thunderState),
    observed_is_raining: bot && typeof bot.isRaining === "boolean" ? bot.isRaining : null,
  };
}

/**
 * Send one RCON command; log and return null instead of throwing.
 * @param {*} bot
 * @param {*} rcon
 * @param {string} cmd
 * @returns {Promise<string|null>}
 */
async function rconSafe(bot, rcon, cmd) {
  try {
    const res = await rcon.send(cmd);
    console.log(`[${bot.username}] [env] ${cmd} -> ${res}`);
    return res;
  } catch (err) {
    console.warn(`[${bot.username}] [env] ${cmd} failed: ${err.message}`);
    return null;
  }
}

/**
 * Leader only: apply the chosen environment over RCON. Runs in the teleport
 * phase, i.e. before either bot's episode setup (so the hostile clean-up
 * cannot remove mobs a handler summons).
 * @param {*} bot
 * @param {*} rcon
 * @param {Object} env - result of chooseEpisodeEnvironment
 * @param {Object} [opts]
 * @param {boolean} [opts.damageEnabled=false] - also kill loaded hostile mobs
 * @param {boolean} [opts.waitForWeather=true] - wait ~5.5 s for the rain level
 *   ramp when the weather changed since the previous episode
 * @returns {Promise<boolean>} true if every command got a reply
 */
async function applyEpisodeEnvironment(
  bot,
  rcon,
  env,
  { damageEnabled = false, waitForWeather = true } = {},
) {
  const weather = ["clear", "rain", "thunder"].includes(env.weather) ? env.weather : "clear";
  const cmds = [
    "gamerule doDaylightCycle false",
    "gamerule doWeatherCycle false",
    ...SPAWN_GAMERULES,
    `time set ${env.time_of_day}`,
    `weather ${weather}`,
  ];
  let ok = true;
  for (const cmd of cmds) {
    if ((await rconSafe(bot, rcon, cmd)) === null) ok = false;
  }
  // The rain/thunder level ramps over ~5 s on the server (and on the camera
  // clients). Wait for the ramp when the weather changed (or is unknown, i.e.
  // the first episode: the world may still be raining from an earlier run),
  // so the episode starts at its final weather.
  const previous = bot._lastAppliedWeather;
  if (waitForWeather && previous !== weather) {
    await new Promise((resolve) => setTimeout(resolve, WEATHER_RAMP_MS));
  }
  bot._lastAppliedWeather = weather;
  if (damageEnabled) {
    // "No entity was found" replies are expected; only a failed send counts.
    for (const type of HOSTILE_MOB_TYPES) {
      if ((await rconSafe(bot, rcon, `kill @e[type=minecraft:${type}]`)) === null) ok = false;
    }
  }
  return ok;
}

/**
 * Whether an episode type takes real damage (T14).
 * @param {string} episodeType
 * @param {*} [episodeInstance]
 * @returns {boolean}
 */
function isDamageEpisode(episodeType, episodeInstance = null) {
  if (DAMAGE_EPISODE_TYPES.has(episodeType)) return true;
  return Boolean(episodeInstance?.constructor?.DAMAGE_ENABLED);
}

/**
 * Re-apply per-bot state that a death resets (effects, attribute base
 * values; scoreboard tags are re-added defensively). Idempotent.
 * @param {*} bot
 * @param {*} rcon
 * @returns {Promise<void>}
 */
async function ensureBotPersistentState(bot, rcon) {
  await rconSafe(bot, rcon, `tag ${bot.username} add minebot`);
  await rconSafe(
    bot,
    rcon,
    `effect give ${bot.username} minecraft:water_breathing 999999 0 true`,
  );
  await setFallDamageMultiplierZero(bot, rcon, bot.username);
}

/**
 * Set a player's fall damage multiplier attribute to 0. The attribute is
 * `generic.fall_damage_multiplier` up to 1.21.1 and `fall_damage_multiplier`
 * from 1.21.2 on; try both (gamerule fallDamage is off anyway).
 * @param {*} bot - for logging
 * @param {*} rcon
 * @param {string} playerName
 * @returns {Promise<string|null>} reply of the command that worked, else null
 */
async function setFallDamageMultiplierZero(bot, rcon, playerName) {
  for (const attr of ["minecraft:generic.fall_damage_multiplier", "minecraft:fall_damage_multiplier"]) {
    const res = await rconSafe(bot, rcon, `attribute ${playerName} ${attr} base set 0`);
    if (res !== null && !/can't find|unknown|incorrect/i.test(String(res))) return res;
  }
  return null;
}

/**
 * Per-episode damage policy for this bot (both bots call it concurrently;
 * the difficulty is global but both compute the same value).
 * @param {*} bot
 * @param {*} rcon
 * @param {boolean} damageEnabled
 * @returns {Promise<{difficulty: string, damage_enabled: boolean}>}
 */
async function applyDamagePolicy(bot, rcon, damageEnabled) {
  const name = bot.username;
  if (damageEnabled) {
    await rconSafe(bot, rcon, `difficulty ${DAMAGE_DIFFICULTY}`);
    await rconSafe(bot, rcon, `effect clear ${name} minecraft:resistance`);
    await rconSafe(bot, rcon, `effect clear ${name} minecraft:saturation`);
    // Start with full health and food, then let them drop naturally
    // (naturalRegeneration stays at its default).
    await rconSafe(bot, rcon, `effect give ${name} minecraft:instant_health 1 10 true`);
    await rconSafe(bot, rcon, `effect give ${name} minecraft:saturation 1 20 true`);
    return { difficulty: DAMAGE_DIFFICULTY, damage_enabled: true };
  }
  await rconSafe(bot, rcon, `difficulty ${PROTECTED_DIFFICULTY}`);
  await rconSafe(bot, rcon, `effect give ${name} minecraft:resistance 999999 255 true`);
  await rconSafe(bot, rcon, `effect give ${name} minecraft:saturation 999999 255 true`);
  return { difficulty: PROTECTED_DIFFICULTY, damage_enabled: false };
}

/**
 * Base randomTickSpeed of an episode (the leader sets it before the start
 * snapshot; see r2-integration "Random ticks").
 *  - `static RANDOM_TICK_SPEED` on the class wins;
 *  - EPISODE_RANDOM_TICK_SPEED / --episode_random_tick_speed:
 *    "vanilla" or "" (default): 3. The plugin's periodic box diff
 *    (`block_state` records with source `box_diff`, every 20 ticks) records
 *    the random-tick changes that fire no Bukkit event, so the replay stays
 *    exact and natural dynamics (crop / sapling growth, leaf decay, grass
 *    spread, snow / ice melt) stay on;
 *    "auto": 0 in clear weather, 3 in rain / snow (only causally-typed
 *    events, fewer `box_diff` records);
 *    a number: that value; -1: leave the gamerule alone.
 * @param {Object} args
 * @param {Function|Object|null} episodeClass
 * @param {string|null} weather - "clear" | "rain" | "thunder"
 * @returns {number|null} value to set, or null to leave the gamerule alone
 */
function randomTickSpeedFor(args, episodeClass, weather) {
  const C = episodeClass || {};
  if (C.RANDOM_TICK_SPEED !== undefined && C.RANDOM_TICK_SPEED !== null && Number.isFinite(Number(C.RANDOM_TICK_SPEED))) {
    return Number(C.RANDOM_TICK_SPEED);
  }
  const raw = argOrEnv(args, "episode_random_tick_speed", "EPISODE_RANDOM_TICK_SPEED");
  const spec = String(raw === undefined ? "vanilla" : raw).toLowerCase();
  if (spec === "vanilla" || spec === "default") return 3;
  if (spec === "auto") return weather && weather !== "clear" ? 3 : 0;
  const n = Number(spec);
  if (!Number.isFinite(n) || n < 0) return null;
  return Math.floor(n);
}

/**
 * Add keys to episodeInstance._envMetadata (never touches _stateMetadata).
 * @param {*} episodeInstance
 * @param {Object} meta
 */
function addEnvMetadata(episodeInstance, meta) {
  episodeInstance._envMetadata = { ...(episodeInstance._envMetadata || {}), ...meta };
}

/**
 * state_metadata written to episode_info: environment keys merged with the
 * handler's keys (handler values win on conflicts).
 * @param {*} episodeInstance
 * @returns {Object}
 */
function mergedStateMetadata(episodeInstance) {
  return {
    ...(episodeInstance?._envMetadata || {}),
    ...(episodeInstance?._stateMetadata || {}),
  };
}

module.exports = {
  DAMAGE_DIFFICULTY,
  DAMAGE_EPISODE_TYPES,
  HOSTILE_MOB_TYPES,
  PROTECTED_DIFFICULTY,
  SPAWN_GAMERULES,
  DEFAULT_WEATHER_WEIGHTS,
  EPISODE_TYPE_ENV_DEFAULTS,
  TIME_BANDS,
  TIME_DAWN,
  TIME_DAY,
  TIME_DUSK,
  TIME_NIGHT,
  addEnvMetadata,
  applyDamagePolicy,
  applyEpisodeEnvironment,
  chooseEpisodeEnvironment,
  ensureBotPersistentState,
  flatWorldBiome,
  isDamageEpisode,
  mergedStateMetadata,
  parseWeatherWeights,
  randomTickSpeedFor,
  resolvePrecipitation,
  setFallDamageMultiplierZero,
  timeOfDayLabel,
  weatherObservation,
};
