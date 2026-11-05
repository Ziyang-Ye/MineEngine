/**
 * flat-biome.js - per-episode biome for FLAT worlds (round-2 fixer).
 *
 * A flat world has one preset biome per world (per instance and batch), so
 * every flat episode of a batch had the same grass / foliage / water / sky
 * colours, the same precipitation and the same creature pools. With
 * probability FLAT_BIOME_SWAP_PROB (default 0.5) the leader re-biomes the
 * area around the bots after the teleport, BEFORE setup and the start
 * snapshot:
 *
 *   fillbiome <x1> <y-4> <z1> <x1+31> <y+23> <z1+31> minecraft:<biome>
 *
 * over a 128 x 128 area (16 tiles of 32 x 28 x 32 blocks: the server
 * limits one fillbiome to 32768 blocks; tiles start on multiples of 4). The biome is planned like the normal-world
 * target biome (biomes.js planEpisodeBiomes: class PREFERRED_BIOMES /
 * AVOID_BIOMES, "snow" weather -> a cold biome), without water / shore
 * biomes. Biomes are chunk data, not blocks: the world log and its replay
 * are not affected; the episode-level `biome` key (resolved at the site
 * after setup) reports the new biome. Clients receive the change as a chunk
 * biome update (grass, foliage, water and sky colours; rain vs snow).
 *
 * Separate seeded RNG `${bot_rng_seed}_flatbiome_${episode}` (the planner's
 * own `_biome_` RNG picks the biome); the shared bot RNG is not used.
 * Never throws.
 */

const seedrandom = require("seedrandom");

const { BIOMES, planEpisodeBiomes } = require("./biomes");

const BY_ID = Object.fromEntries(BIOMES.map((b) => [b.id, b]));
const NO_FLAT = new Set(["river", "frozen_river", "beach", "snowy_beach", "stony_shore"]);
const HALF = 64;
const TILE = 32;
const DOWN = 4;
const UP = 23;

function swapProbability(args) {
  const v = args && args.flat_biome_swap_prob !== undefined && args.flat_biome_swap_prob !== ""
    ? args.flat_biome_swap_prob
    : process.env.FLAT_BIOME_SWAP_PROB;
  const n = Number(v === undefined || v === "" ? 0.5 : v);
  return Number.isFinite(n) ? Math.max(0, Math.min(1, n)) : 0.5;
}

/**
 * Plan (deterministic; the same on both bots).
 * @returns {{enabled: boolean, probability: number, biome?: string, restricted_by?: string[]}}
 */
function planFlatBiome(args, episodeNum, episodeClass, weatherRequested) {
  const prob = swapProbability(args);
  const rng = seedrandom(`${args.bot_rng_seed}_flatbiome_${episodeNum}`);
  if (!(rng() < prob)) return { enabled: false, probability: prob };
  const plan = planEpisodeBiomes(args, episodeNum, episodeClass, { weatherRequested });
  const biome = plan.candidates.find((id) => BY_ID[id] && !BY_ID[id].shore && !NO_FLAT.has(id));
  if (!biome) return { enabled: false, probability: prob, reason: "no_candidate" };
  return { enabled: true, probability: prob, biome, restricted_by: plan.restricted_by };
}

/**
 * Leader: apply the plan around `center` ({x, y, z}, feet level). Returns
 * the metadata (`flat_biome_swap`).
 */
async function applyFlatBiome(bot, rcon, plan, center) {
  if (!plan || !plan.enabled) return plan ? { enabled: false, probability: plan.probability } : null;
  const cx = Math.floor(center.x / 4) * 4;
  // y range aligned to biome cells (4 blocks): y0 .. y0+27
  const cy = Math.floor((Math.floor(center.y) - DOWN) / 4) * 4 + DOWN;
  const cz = Math.floor(center.z / 4) * 4;
  const meta = {
    enabled: true,
    probability: plan.probability,
    biome: plan.biome,
    region: [cx - HALF, cy - DOWN, cz - HALF, cx + HALF - 1, cy + UP, cz + HALF - 1],
    ok: false,
  };
  const send = async (x1, z1, x2, z2) => {
    const cmd = `fillbiome ${x1} ${cy - DOWN} ${z1} ${x2} ${cy + UP} ${z2} minecraft:${plan.biome}`;
    try {
      const res = String(await rcon.send(cmd));
      console.log(`[${bot.username}] [flat-biome] ${cmd} -> ${res.slice(0, 160)}`);
      return /^\s*\d+ biome/i.test(res) || /biome entr/i.test(res) || !/too many|error|unknown|invalid|incorrect|not loaded|expected/i.test(res);
    } catch (err) {
      console.warn(`[${bot.username}] [flat-biome] ${cmd} failed: ${err.message}`);
      return false;
    }
  };
  let n = 0;
  let total = 0;
  for (let x1 = cx - HALF; x1 < cx + HALF; x1 += TILE) {
    for (let z1 = cz - HALF; z1 < cz + HALF; z1 += TILE) {
      total++;
      if (await send(x1, z1, x1 + TILE - 1, z1 + TILE - 1)) n++;
    }
  }
  meta.tiles_ok = n;
  meta.tiles = total;
  meta.ok = n === total;
  return meta;
}

module.exports = { applyFlatBiome, planFlatBiome, swapProbability };
