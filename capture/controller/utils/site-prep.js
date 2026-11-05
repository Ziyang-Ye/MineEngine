/**
 * site-prep.js - Make a normal-terrain site usable BEFORE the start snapshot.
 *
 *   prepareSite()        ground height at a centre column, optional small
 *                        levelled pad (/fill: clear everything above the
 *                        ground level, fill holes / water below it)
 *   settleBotsOnGround() /spreadplayers puts players on the highest block,
 *                        i.e. on tree canopies in forests and jungles; move
 *                        each bot down to the real ground under it
 *
 * Both only use /fill and /tp and must run before worldStateBegin (they
 * refuse to run once the episode's world log is open). The natural
 * surroundings are kept: only the pad (radius <= 8) is touched.
 */

const { Vec3 } = require("vec3");

const MAX_PAD_RADIUS = 8;

const AIR_NAMES = new Set(["air", "cave_air", "void_air"]);

// Blocks that are not "ground" when looking for where to stand.
const NON_GROUND_SUBSTRINGS = [
  "leaves",
  "_log",
  "_wood",
  "_stem",
  "hyphae",
  "mushroom_block",
  "bamboo",
  "vine",
  "cactus",
  "pointed_dripstone",
  "powder_snow",
  "scaffolding",
  "cobweb",
  "sweet_berry_bush",
  "magma_block",
  "campfire",
  "fire",
];

// Blocks a /fill may replace when filling holes in the pad layer(s).
const HOLE_FILL_REPLACE = [
  "air",
  "cave_air",
  "water",
  "lava",
  "seagrass",
  "tall_seagrass",
  "kelp",
  "kelp_plant",
  "bubble_column",
  "powder_snow",
];

function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

function withTimeout(promise, ms, what) {
  let timer;
  return Promise.race([
    promise,
    new Promise((_, reject) => {
      timer = setTimeout(() => reject(new Error(`timeout after ${ms} ms: ${what}`)), ms);
    }),
  ]).finally(() => clearTimeout(timer));
}

function isAir(block) {
  return !block || AIR_NAMES.has(block.name);
}

function isFluid(block) {
  return Boolean(block) && (block.name === "water" || block.name === "lava" || block.name === "bubble_column");
}

/** Passable for a standing player (air, grass, flowers, snow layer <= 1, ...). */
function isPassable(block) {
  if (isAir(block)) return true;
  if (isFluid(block)) return false;
  if (NON_GROUND_SUBSTRINGS.some((s) => block.name.includes(s)) && block.boundingBox === "block") return false;
  return block.boundingBox === "empty" || block.name === "snow";
}

/** Solid ground a player can stand on (not leaves / logs / fluids). */
function isGround(block) {
  if (!block || isAir(block) || isFluid(block)) return false;
  if (block.boundingBox !== "block") return false;
  if (block.name === "snow") return false; // snow layer: stand on the block below
  if (NON_GROUND_SUBSTRINGS.some((s) => block.name.includes(s))) return false;
  return true;
}

/**
 * Topmost ground block of a column at or below fromY (client view): solid,
 * not leaves/logs/snow layers. The scan stops at the first such block (it
 * never dives into caves). With requireHeadroom it must also have two
 * passable blocks above it (else null). Water columns return {water: true}
 * with the water surface.
 * @param {*} bot
 * @param {number} x
 * @param {number} z
 * @param {number} [fromY] - start of the downward scan (default: 40 above the bot)
 * @param {Object} [opts]
 * @param {boolean} [opts.requireHeadroom=true]
 * @param {number} [opts.minY=-64]
 * @returns {{pos: Vec3, block: string, water: boolean}|null} null if unloaded / nothing found
 */
function findGround(bot, x, z, fromY = null, { requireHeadroom = true, minY = -64 } = {}) {
  const bx = Math.floor(x);
  const bz = Math.floor(z);
  let y = Number.isFinite(fromY)
    ? Math.floor(fromY)
    : Math.floor((bot.entity && bot.entity.position ? bot.entity.position.y : 100) + 40);
  const top = bot.blockAt(new Vec3(bx, y, bz));
  if (top === null) return null; // unloaded
  for (; y > minY; y--) {
    const b = bot.blockAt(new Vec3(bx, y, bz));
    if (!b) return null;
    if (isFluid(b)) {
      // Standing in water is never "ground"; report the surface.
      const above = bot.blockAt(new Vec3(bx, y + 1, bz));
      if (isAir(above) || isPassable(above)) {
        return { pos: new Vec3(bx, y, bz), block: b.name, water: true };
      }
      continue;
    }
    if (!isGround(b)) continue;
    if (requireHeadroom) {
      const a1 = bot.blockAt(new Vec3(bx, y + 1, bz));
      const a2 = bot.blockAt(new Vec3(bx, y + 2, bz));
      if (!isPassable(a1) || !isPassable(a2)) return null;
    }
    return { pos: new Vec3(bx, y, bz), block: b.name, water: false };
  }
  return null;
}

function numOr(v, dflt) {
  const n = Number(v);
  return Number.isFinite(n) ? n : dflt;
}

/**
 * Nearest non-water ground column around (x, z) (square rings up to maxR).
 * @returns {{pos: Vec3, block: string, water: boolean}|null}
 */
function findLandNear(bot, x, z, fromY, maxR = 8, requireHeadroom = true) {
  const cx = Math.floor(x);
  const cz = Math.floor(z);
  for (let r = 1; r <= maxR; r++) {
    for (let dx = -r; dx <= r; dx++) {
      for (let dz = -r; dz <= r; dz++) {
        if (Math.max(Math.abs(dx), Math.abs(dz)) !== r) continue;
        const g = findGround(bot, cx + dx, cz + dz, fromY, { requireHeadroom });
        if (g && !g.water) return g;
      }
    }
  }
  return null;
}

/** Parse "/fill" replies: "Successfully filled 42 block(s)" -> 42, "No blocks were filled" -> 0. */
function parseFillCount(reply) {
  const s = String(reply === undefined || reply === null ? "" : reply);
  const m = s.match(/filled\s+(\d+)\s+block/i);
  if (m) return Number(m[1]);
  if (/no blocks were filled/i.test(s)) return 0;
  return null; // error (not loaded, too many blocks, ...)
}

async function rconFill(bot, rcon, cmd, log) {
  let reply = null;
  try {
    reply = String(await withTimeout(Promise.resolve(rcon.send(cmd)), 10000, cmd));
  } catch (err) {
    reply = `error: ${err.message}`;
  }
  const count = parseFillCount(reply);
  log.push({ cmd, reply: reply.slice(0, 200), count });
  console.log(`[${bot.username}] [site-prep] ${cmd} -> ${reply}`);
  return count;
}

/** Filler for the layers under the pad surface (no gravity blocks). */
function subsurfaceFor(surface) {
  if (!surface) return "dirt";
  if (surface === "sand" || surface === "sandstone") return "sandstone";
  if (surface === "red_sand" || surface.endsWith("terracotta")) return "terracotta";
  if (surface === "gravel" || surface.includes("stone") || surface.includes("deepslate") || surface === "calcite" || surface === "tuff") return "stone";
  if (surface === "snow_block" || surface === "packed_ice" || surface === "ice" || surface === "blue_ice") return "snow_block";
  if (surface === "mud" || surface === "muddy_mangrove_roots") return "mud";
  return "dirt";
}

/** Surface block used to fill pad holes (natural, full, non-gravity if possible). */
function surfaceFor(groundName) {
  if (!groundName) return "grass_block";
  const keep = new Set([
    "grass_block",
    "podzol",
    "mycelium",
    "coarse_dirt",
    "dirt",
    "rooted_dirt",
    "mud",
    "sand",
    "red_sand",
    "snow_block",
    "stone",
    "andesite",
    "diorite",
    "granite",
    "calcite",
    "terracotta",
    "packed_ice",
    "moss_block",
    "gravel",
    "sandstone",
  ]);
  if (keep.has(groundName) || groundName.endsWith("terracotta")) return groundName;
  if (groundName === "dirt_path" || groundName === "farmland") return "dirt";
  if (groundName === "ice" || groundName === "blue_ice") return "packed_ice";
  return "dirt";
}

/**
 * Prepare a site BEFORE the start snapshot.
 *
 * @param {*} bot
 * @param {*} rcon
 * @param {Object} args - other_bot_name (leader test)
 * @param {{x:number,y?:number,z:number}} center
 * @param {Object} [opts]
 * @param {boolean} [opts.level=false] - level a pad of radius `radius`
 * @param {number} [opts.radius=3] - pad radius (clamped to 0..8)
 * @param {number} [opts.clearHeight=4] - blocks cleared above the pad surface
 * @param {number} [opts.fillDepth=2] - layers under the surface whose holes are filled
 * @param {*} [opts.episodeInstance] - refuse to run once its world log is open
 * @param {boolean} [opts.issueCommands] - default: only the world-state leader runs /fill
 * @param {number} [opts.settleMs=1000] - wait after the fills (falling blocks, fluids)
 * @param {boolean} [opts.fillWater=false] - pave a pad over water; by default a
 *   water centre moves to the nearest land column (<= landSearchRadius, 8)
 *   and a site with no land nearby is not levelled (reason "water_site")
 * @param {boolean} [opts.settleBots=true] - afterwards move both bots onto the
 *   ground (a bot whose ground was cleared would otherwise hang in the air)
 * @returns {Promise<{site: Vec3|null, prepared: boolean, removed_blocks: number,
 *   filled_blocks: number, ground_block: string|null, water: boolean, radius: number,
 *   reason: string|null, commands: Object[], settled: Object[]|null}>}
 */
async function prepareSite(bot, rcon, args, center, opts = {}) {
  const radius = Math.max(0, Math.min(MAX_PAD_RADIUS, Math.floor(Number(opts.radius ?? 3))));
  const clearHeight = Math.max(1, Math.min(16, Math.floor(Number(opts.clearHeight ?? 4))));
  const fillDepth = Math.max(0, Math.min(4, Math.floor(Number(opts.fillDepth ?? 2))));
  const settleMs = Number.isFinite(Number(opts.settleMs)) ? Number(opts.settleMs) : 1000;
  const result = {
    site: null,
    prepared: false,
    removed_blocks: 0,
    filled_blocks: 0,
    ground_block: null,
    water: false,
    radius,
    reason: null,
    commands: [],
    settled: null,
    moved_from_water: null,
  };
  const ep = opts.episodeInstance;
  if (ep && (ep._worldState || ep._episodeRecordingStarted)) {
    result.reason = "world_log_open";
    console.warn(`[${bot.username}] [site-prep] refused: the episode's world log is already open`);
    return result;
  }
  if (!center) {
    result.reason = "no_center";
    return result;
  }
  const fromY = Number.isFinite(Number(center.y)) ? Number(center.y) + 24 : null;
  let g = findGround(bot, center.x, center.z, fromY, { requireHeadroom: !opts.level });
  if (g && g.water && !opts.fillWater) {
    // Centre in a river / lake / sea: use the nearest land column instead
    // (never pave the water unless asked to).
    const land = findLandNear(bot, center.x, center.z, fromY, numOr(opts.landSearchRadius, 8), !opts.level);
    if (land) {
      result.moved_from_water = [Math.floor(center.x), Math.floor(center.z)];
      g = land;
    }
  }
  if (!g) {
    result.reason = "unloaded_or_no_ground";
    return result;
  }
  result.site = g.pos;
  result.ground_block = g.block;
  result.water = g.water;
  if (g.water && opts.level && !opts.fillWater) {
    result.reason = "water_site";
    return result;
  }
  if (!opts.level) {
    result.reason = "level_not_requested";
    return result;
  }
  const leader = bot.username < args.other_bot_name;
  const issue = opts.issueCommands === undefined ? leader : Boolean(opts.issueCommands);
  if (!issue) {
    result.reason = "not_leader";
    return result;
  }

  const { x, y, z } = g.pos;
  const x1 = x - radius;
  const x2 = x + radius;
  const z1 = z - radius;
  const z2 = z + radius;
  const surface = g.water ? "dirt" : surfaceFor(g.block);
  const sub = subsurfaceFor(surface);
  const log = result.commands;
  let removed = 0;
  let filled = 0;
  let errors = 0;

  // 1) Clear everything above the pad level (vegetation, trunks, boulders, snow layers).
  const c = await rconFill(bot, rcon, `fill ${x1} ${y + 1} ${z1} ${x2} ${y + clearHeight} ${z2} minecraft:air`, log);
  if (c === null) errors++;
  else removed += c;

  // 2) Fill holes from the bottom up so gravity blocks are supported.
  for (let dy = fillDepth; dy >= 0; dy--) {
    const yy = y - dy;
    const block = dy === 0 ? surface : sub;
    for (const what of HOLE_FILL_REPLACE) {
      const n = await rconFill(
        bot,
        rcon,
        `fill ${x1} ${yy} ${z1} ${x2} ${yy} ${z2} minecraft:${block} replace minecraft:${what}`,
        log,
      );
      if (n === null) errors++;
      else filled += n;
    }
  }
  if (settleMs > 0) await sleep(settleMs);

  if (opts.settleBots !== false) {
    try {
      result.settled = await settleBotsOnGround(bot, rcon, args);
    } catch (err) {
      console.warn(`[${bot.username}] [site-prep] settle after prep failed: ${err.message}`);
    }
  }
  result.site = new Vec3(x, y, z);
  result.removed_blocks = removed;
  result.filled_blocks = filled;
  result.prepared = errors === 0;
  result.reason = errors === 0 ? null : `fill_errors:${errors}`;
  return result;
}

/**
 * Move one player down to the ground under (x, z) when it stands on leaves,
 * logs or in a tree, or float it up out of a 1-block hole. No-op when it
 * already stands on ground.
 * @returns {Promise<{name:string, moved:boolean, from:number[]|null, to:number[]|null, ground:string|null}>}
 */
async function settlePlayer(bot, rcon, name, pos) {
  const out = { name, moved: false, from: null, to: null, ground: null };
  if (!pos) return out;
  out.from = [Number(pos.x.toFixed(2)), Number(pos.y.toFixed(2)), Number(pos.z.toFixed(2))];
  const feet = new Vec3(Math.floor(pos.x), Math.floor(pos.y), Math.floor(pos.z));
  const below = bot.blockAt(feet.offset(0, -1, 0));
  if (below === null) return out; // unloaded
  if (isGround(below) && isPassable(bot.blockAt(feet)) && isPassable(bot.blockAt(feet.offset(0, 1, 0)))) {
    out.ground = below.name;
    return out;
  }
  const g = findGround(bot, pos.x, pos.z, feet.y + 2);
  if (!g || g.water) return out;
  out.ground = g.block;
  const tx = Math.floor(pos.x) + 0.5;
  const tz = Math.floor(pos.z) + 0.5;
  const ty = g.pos.y + 1;
  try {
    const reply = await withTimeout(Promise.resolve(rcon.send(`tp ${name} ${tx} ${ty} ${tz}`)), 5000, "tp");
    console.log(`[${bot.username}] [site-prep] settle ${name} ${out.from} -> ${tx} ${ty} ${tz}: ${reply}`);
    out.moved = true;
    out.to = [tx, ty, tz];
  } catch (err) {
    console.warn(`[${bot.username}] [site-prep] settle ${name} failed: ${err.message}`);
  }
  return out;
}

/**
 * Leader: after /spreadplayers, move both bots off tree canopies / out of
 * trunks to the real ground. The peer's position is the leader's view of
 * it (waits up to 4 s for the entity); unknown peers are left alone.
 * @param {*} bot
 * @param {*} rcon
 * @param {Object} args - other_bot_name
 * @returns {Promise<Object[]>} per-player settle results
 */
async function settleBotsOnGround(bot, rcon, args) {
  const results = [];
  results.push(await settlePlayer(bot, rcon, bot.username, bot.entity && bot.entity.position));
  const other = args.other_bot_name;
  if (other) {
    let ent = null;
    for (let i = 0; i < 20 && !ent; i++) {
      ent = bot.players && bot.players[other] && bot.players[other].entity;
      if (!ent) await sleep(200);
    }
    results.push(ent ? await settlePlayer(bot, rcon, other, ent.position) : { name: other, moved: false, reason: "not_visible" });
  }
  if (results.some((r) => r.moved)) await sleep(1000);
  return results;
}

/**
 * Metadata keys for episode_info.state_metadata.
 * @param {Object|null} prep - prepareSite result
 * @returns {Object}
 */
function sitePrepMetadata(prep) {
  if (!prep) return { site_prepared: false };
  return {
    site_prepared: Boolean(prep.prepared),
    site_prep_center: prep.site ? [prep.site.x, prep.site.y, prep.site.z] : null,
    site_prep_radius: prep.radius,
    site_prep_removed_blocks: prep.removed_blocks,
    site_prep_filled_blocks: prep.filled_blocks,
    site_ground_block: prep.ground_block,
    site_in_water: Boolean(prep.water),
    site_prep_moved_from_water: prep.moved_from_water || null,
    site_prep_reason: prep.reason || null,
  };
}

module.exports = {
  HOLE_FILL_REPLACE,
  MAX_PAD_RADIUS,
  findGround,
  findLandNear,
  isGround,
  isPassable,
  parseFillCount,
  prepareSite,
  settleBotsOnGround,
  settlePlayer,
  sitePrepMetadata,
  subsurfaceFor,
  surfaceFor,
};
