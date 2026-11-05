/**
 * materials.js - Material pools for every building episode (T7, 03 §3).
 *
 * All choices are made with the shared bot RNG (`sharedBotRng`), so both
 * bots compute the same palette without messaging. Every picker consumes a
 * FIXED number of RNG draws (independent of the values drawn), so the shared
 * RNG stays in lock-step on both bots whatever the outcome.
 *
 * Pools (03 §3):
 *   walls    planks (oak, spruce, birch, dark oak), cobblestone, stone bricks,
 *            bricks, concrete and wool in white/gray/red/blue/yellow/green
 *   pillars  oak / spruce / birch logs (also used for beams and roof rims)
 *   windows  glass, stained glass
 *   roof     planks, stone bricks, dark oak planks
 *   floor    planks, stone, cobblestone
 *   extras   sand, gravel (gravity blocks), tnt
 *
 * Round-2 fixer (diversity): by default the pools are EXTENDED beyond 03 §3
 * with more woods (acacia, jungle, mangrove, cherry, bamboo, crimson,
 * warped planks / logs), stone families (sandstone, red sandstone,
 * polished andesite / granite / diorite, deepslate bricks / tiles, mud
 * bricks, quartz, prismarine), terracotta and more concrete / wool / glass
 * colours. MATERIAL_POOL=spec restores the exact 03 §3 pools. Draw counts
 * are unchanged (one draw picks from a longer list). scaffoldSafe gains the
 * extra planks (all listed in movement.js DEFAULT_SCAFFOLDING_BLOCK_NAMES);
 * gravity and refill are not extended.
 *
 * Non-full blocks (doors, torches) are kept <= 10% of the placed blocks by
 * the episodes themselves (see nonFullFraction()).
 */

const { ensureBotHasEnough } = require("../primitives/items");

const SPEC_POOL = String(process.env.MATERIAL_POOL || "extended").toLowerCase() === "spec";

const COLORS = ["white", "gray", "red", "blue", "yellow", "green"];
// extra colours / woods / stones of the extended pool (not 03 §3)
const EXTRA_COLORS = SPEC_POOL ? [] : ["orange", "light_blue", "lime", "cyan", "purple", "pink", "black", "brown", "magenta", "light_gray"];
const EXTRA_PLANKS = SPEC_POOL
  ? []
  : ["acacia_planks", "jungle_planks", "mangrove_planks", "cherry_planks", "bamboo_planks", "crimson_planks", "warped_planks"];
const EXTRA_STONES = SPEC_POOL
  ? []
  : [
      "sandstone",
      "cut_sandstone",
      "red_sandstone",
      "polished_andesite",
      "polished_granite",
      "polished_diorite",
      "deepslate_bricks",
      "mud_bricks",
      "quartz_block",
      "prismarine_bricks",
      "dark_prismarine",
      "terracotta",
      "white_terracotta",
      "orange_terracotta",
      "light_blue_terracotta",
      "brown_terracotta",
      "cyan_terracotta",
    ];

const PLANKS = ["oak_planks", "spruce_planks", "birch_planks", "dark_oak_planks", ...EXTRA_PLANKS];

const POOLS = Object.freeze({
  walls: [
    ...PLANKS,
    "cobblestone",
    "stone_bricks",
    "bricks",
    ...[...COLORS, ...EXTRA_COLORS].map((c) => `${c}_concrete`),
    ...[...COLORS, ...EXTRA_COLORS].map((c) => `${c}_wool`),
    ...EXTRA_STONES,
  ],
  pillars: [
    "oak_log",
    "spruce_log",
    "birch_log",
    ...(SPEC_POOL ? [] : ["dark_oak_log", "acacia_log", "jungle_log", "mangrove_log", "cherry_log", "crimson_stem", "warped_stem"]),
  ],
  windows: [
    "glass",
    "white_stained_glass",
    "gray_stained_glass",
    "light_blue_stained_glass",
    "blue_stained_glass",
    "yellow_stained_glass",
    "green_stained_glass",
    "red_stained_glass",
    ...(SPEC_POOL ? [] : ["orange_stained_glass", "lime_stained_glass", "cyan_stained_glass", "purple_stained_glass", "pink_stained_glass", "brown_stained_glass", "tinted_glass"]),
  ],
  roof: [
    ...PLANKS,
    "stone_bricks",
    "dark_oak_planks",
    ...(SPEC_POOL ? [] : ["deepslate_tiles", "mud_bricks", "red_sandstone", "prismarine_bricks", "bricks"]),
  ],
  floor: [
    ...PLANKS,
    "stone",
    "cobblestone",
    ...(SPEC_POOL ? [] : ["smooth_stone", "polished_andesite", "polished_deepslate", "smooth_sandstone", "terracotta"]),
  ],
  extras: ["sand", "gravel", "tnt"],
  gravity: ["sand", "gravel"],
  // refill material of the dig episodes (natural + floor pool + gravity)
  refill: ["dirt", "cobblestone", "stone", "gravel", "sand"],
  // full blocks the pathfinder may use as scaffolding (movement.js list)
  scaffoldSafe: [...PLANKS, "cobblestone", "stone_bricks", "bricks"],
  doors: ["oak_door", "spruce_door", "birch_door", "dark_oak_door"],
});

/** Wood family of a planks block -> matching log (dark oak -> spruce/oak). */
const PLANKS_TO_LOG = {
  oak_planks: "oak_log",
  spruce_planks: "spruce_log",
  birch_planks: "birch_log",
  ...(SPEC_POOL
    ? {}
    : {
        dark_oak_planks: "dark_oak_log",
        acacia_planks: "acacia_log",
        jungle_planks: "jungle_log",
        mangrove_planks: "mangrove_log",
        cherry_planks: "cherry_log",
        crimson_planks: "crimson_stem",
        warped_planks: "warped_stem",
      }),
};
const PLANKS_TO_DOOR = {
  oak_planks: "oak_door",
  spruce_planks: "spruce_door",
  birch_planks: "birch_door",
  dark_oak_planks: "dark_oak_door",
  ...(SPEC_POOL
    ? {}
    : {
        acacia_planks: "acacia_door",
        jungle_planks: "jungle_door",
        mangrove_planks: "mangrove_door",
        cherry_planks: "cherry_door",
        bamboo_planks: "bamboo_door",
        crimson_planks: "crimson_door",
        warped_planks: "warped_door",
      }),
};

/** Blocks that are not full cubes (count towards the 10% budget). */
const NON_FULL_RE = /(_door|_stairs|_slab|torch|_fence|_fence_gate|_pane|_trapdoor|_wall$|lantern|ladder|_carpet)/;

function isNonFull(name) {
  return NON_FULL_RE.test(String(name || ""));
}

function isGravityBlock(name) {
  return ["sand", "red_sand", "gravel", "suspicious_sand", "suspicious_gravel"].includes(name) ||
    String(name || "").endsWith("_concrete_powder");
}

function familyOf(name) {
  if (PLANKS.includes(name)) return "wood";
  if (name.endsWith("_terracotta") || name === "terracotta") return "terracotta";
  if (name.endsWith("_concrete")) return "concrete";
  if (name.endsWith("_wool")) return "wool";
  if (name.endsWith("_log") || name.endsWith("_stem")) return "log";
  if (name.includes("glass")) return "glass";
  return "stone";
}

/** Index from one uniform draw (u in [0,1)). */
function idx(u, n) {
  return Math.min(n - 1, Math.floor(u * n));
}

/** Pick from a list with one draw, skipping `exclude` deterministically. */
function pickFrom(u, list, exclude = []) {
  const allowed = list.filter((x) => !exclude.includes(x));
  const src = allowed.length > 0 ? allowed : list;
  return src[idx(u, src.length)];
}

/**
 * Pick one material from a named pool. Consumes exactly ONE draw.
 * @param {Function} rng
 * @param {string} pool - key of POOLS
 * @param {string[]} [exclude]
 * @returns {string}
 */
function pickMaterial(rng, pool, exclude = []) {
  const list = POOLS[pool];
  if (!list) throw new Error(`unknown material pool ${pool}`);
  return pickFrom(rng(), list, exclude);
}

/**
 * Coherent building palette. Consumes exactly SEVEN draws.
 *   walls   - main wall material
 *   accent  - second wall material (different from walls), for a second
 *             builder / trims / alternating rounds
 *   pillar  - corner posts / beams (log; same wood as plank walls 70%)
 *   window  - glass (50%) or a stained glass
 *   roof    - roof pool, never equal to walls
 *   floor   - floor pool, never equal to walls
 *   door    - door matching the wood of the walls/roof (only used when the
 *             episode decides to place a door)
 * @param {Function} rng - shared bot RNG
 * @returns {{walls:string, accent:string, pillar:string, window:string,
 *   roof:string, floor:string, door:string, wall_family:string}}
 */
function pickPalette(rng) {
  const u = [rng(), rng(), rng(), rng(), rng(), rng(), rng()];
  const walls = POOLS.walls[idx(u[0], POOLS.walls.length)];
  const accent = pickFrom(u[1], POOLS.walls, [walls]);
  let pillar;
  if (PLANKS_TO_LOG[walls] && u[2] < 0.7) pillar = PLANKS_TO_LOG[walls];
  else pillar = POOLS.pillars[idx(u[2] < 0.7 ? u[2] / 0.7 : (u[2] - 0.7) / 0.3, POOLS.pillars.length)];
  const window = u[3] < 0.5 ? "glass" : POOLS.windows[1 + idx((u[3] - 0.5) / 0.5, POOLS.windows.length - 1)];
  const roof = pickFrom(u[4], POOLS.roof, [walls]);
  const floor = pickFrom(u[5], POOLS.floor, [walls]);
  const door =
    PLANKS_TO_DOOR[walls] || PLANKS_TO_DOOR[roof] || POOLS.doors[idx(u[6], POOLS.doors.length)];
  return { walls, accent, pillar, window, roof, floor, door, wall_family: familyOf(walls) };
}

/**
 * Count materials of a placement list ([{material}] or [{block}]).
 * @param {Array<Object>} placements
 * @returns {Object<string, number>}
 */
function countMaterials(placements) {
  const out = {};
  for (const p of placements) {
    const m = p.material || p.block;
    if (!m) continue;
    out[m] = (out[m] || 0) + 1;
  }
  return out;
}

/** Fraction of non-full blocks in a placement list. */
function nonFullFraction(placements) {
  if (!placements || placements.length === 0) return 0;
  const n = placements.filter((p) => isNonFull(p.material || p.block)).length;
  return Math.round((n / placements.length) * 1000) / 1000;
}

/**
 * Give materials over RCON (`/give`, via ensureBotHasEnough).
 * @param {*} bot
 * @param {*} rcon
 * @param {Object<string, number>|Array<[string, number]>} counts
 * @param {Object} [opts]
 * @param {number} [opts.extra=2] - spare blocks per material
 * @param {number} [opts.factor=1] - multiply counts (scaffold reserve)
 */
async function giveMaterials(bot, rcon, counts, { extra = 2, factor = 1 } = {}) {
  const entries = Array.isArray(counts) ? counts : Object.entries(counts);
  for (const [name, count] of entries) {
    if (!name || !(count > 0)) continue;
    try {
      await ensureBotHasEnough(bot, rcon, name, Math.ceil(count * factor) + extra);
    } catch (err) {
      console.log(`[${bot.username}] giveMaterials ${name} failed: ${err.message}`);
    }
  }
}

/** state_metadata form of a palette (only the roles actually used). */
function paletteMetadata(palette, roles = null) {
  const keys = roles || ["walls", "accent", "pillar", "window", "roof", "floor"];
  const out = {};
  for (const k of keys) if (palette[k]) out[k] = palette[k];
  return out;
}

module.exports = {
  COLORS,
  PLANKS,
  POOLS,
  countMaterials,
  familyOf,
  giveMaterials,
  isGravityBlock,
  isNonFull,
  nonFullFraction,
  paletteMetadata,
  pickMaterial,
  pickPalette,
};
