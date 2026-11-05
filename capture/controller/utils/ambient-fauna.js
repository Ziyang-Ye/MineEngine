/**
 * ambient-fauna.js - optional background animals for the non-creature
 * episodes (round-2 fixer, "creatures everywhere" diversity gap).
 *
 * Before round 2's fixer pass, animals appeared only in the creature
 * episodes (animalCare, rideMount, itemExchange; flat worlds have no natural
 * animals at all) and non-damage episodes run on peaceful with mob spawning
 * off. With probability AMBIENT_FAUNA_PROB (default 0.35) a build / destroy /
 * move / mechanics / chase episode gets a small herd of 2-6 passive or
 * neutral animals (biome weighted, utils/creatures.js ANIMALS pools; 1-2
 * species, ~20 % babies) 12-26 blocks from the episode site.
 *
 *  - The leader summons them after setupEpisode, i.e. BEFORE the start
 *    snapshot (worldStateBegin); afterwards only their own AI acts (grazing
 *    sheep turn grass into dirt, wolves hunt sheep / rabbits, chickens lay
 *    eggs: all logged as entity / block events, entities every tick).
 *  - Separate seeded RNG `${bot_rng_seed}_fauna_${episode}`: the shared bot
 *    RNG is never touched, enabling the option does not change any plan.
 *  - Summons carry the tag `solaris_r2` (creatures.js SUMMON_TAG) and are
 *    removed after the episode (episodes-loop cleanupEpisodeEntities).
 *  - Not used for damage episodes (a sword swing could hit them), creature
 *    episodes (their own creatures), eval episodes, or when
 *    AMBIENT_FAUNA_PROB=0.
 *
 * Nothing here may break an episode: every failure is logged and returns
 * metadata with `summoned: 0`.
 */

const seedrandom = require("seedrandom");
const { Vec3 } = require("vec3");

const { ANIMALS, animalSummonCommand, animalVariant, animalWeight, biomeGroup, pickWeighted } = require("./creatures");

// goats ram players, bees sting and pollinate crops: not as background
const FAUNA_EXCLUDE = ["goat", "bee"];
// per-type exclusions (animals that would interfere with the episode subject)
const TYPE_EXCLUDE = {
  farming: ["rabbit", "chicken", "pig"], // eat / trample crops near the farm
};
const MIN_DIST = 12;
const MAX_DIST = 26;

function numOr(v, d) {
  const n = Number(v);
  return Number.isFinite(n) ? n : d;
}

/** AMBIENT_FAUNA_PROB (env) / args.ambient_fauna_prob, default 0.35. */
function faunaProbability(args) {
  const v = args && args.ambient_fauna_prob !== undefined && args.ambient_fauna_prob !== ""
    ? args.ambient_fauna_prob
    : process.env.AMBIENT_FAUNA_PROB;
  return Math.max(0, Math.min(1, numOr(v === undefined || v === "" ? 0.35 : v, 0.35)));
}

/**
 * Plan (deterministic per episode; the same on both bots).
 * @returns {{enabled: boolean, probability: number, species: string[], animals: Array}|null}
 *   null when the episode is not eligible
 */
function planAmbientFauna(args, episodeNum, { episodeType, biome, eligible }) {
  if (!eligible) return null;
  const prob = faunaProbability(args);
  const rng = seedrandom(`${args.bot_rng_seed}_fauna_${episodeNum}`);
  const uOn = rng();
  const uCount = rng();
  const uSpecies2 = rng();
  const uAngle = rng();
  const uDist = rng();
  const draws = [];
  for (let i = 0; i < 6; i++) draws.push([rng(), rng(), rng(), rng(), rng()]);
  const sp1 = rng();
  const sp2 = rng();
  if (!(uOn < prob)) return { enabled: false, probability: prob };
  const group = biomeGroup(biome);
  const exclude = [...FAUNA_EXCLUDE, ...(TYPE_EXCLUDE[episodeType] || [])];
  const ids = Object.keys(ANIMALS).filter((id) => !exclude.includes(id));
  // biome specialists (mooshroom, panda, ocelot, parrot, frog, turtle, camel,
  // armadillo: a multiplier >= 8 somewhere) only in their own biome groups
  const w = (id) => {
    const a = ANIMALS[id];
    const mults = Object.values(a.mult || {});
    if (mults.some((m) => m >= 8) && !(a.mult && a.mult[group] !== undefined && a.mult[group] >= 1)) return 0;
    return animalWeight(id, group, { exclude });
  };
  const first = pickWeighted(() => sp1, ids, w);
  if (!first) return { enabled: false, probability: prob, reason: "no_species" };
  const second = uSpecies2 < 0.4 ? pickWeighted(() => sp2, ids.filter((id) => id !== first), w) : null;
  const count = 2 + Math.floor(uCount * 5); // 2..6
  const angle = uAngle * 2 * Math.PI;
  const dist = MIN_DIST + uDist * (MAX_DIST - MIN_DIST);
  const animals = [];
  for (let i = 0; i < count; i++) {
    const [uBaby, uOffA, uOffR, uVar1, uVar2] = draws[i];
    const id = second && i % 2 === 1 ? second : first;
    let k = 0;
    const variant = animalVariant(() => (k++ % 2 === 0 ? uVar1 : uVar2), id, biome);
    animals.push({
      id,
      baby: uBaby < 0.2,
      variantNbt: variant.nbt,
      variant: variant.variant,
      off: [Math.cos(uOffA * 2 * Math.PI) * uOffR * 3.5, Math.sin(uOffA * 2 * Math.PI) * uOffR * 3.5],
      yaw: Math.floor(uOffA * 360),
    });
  }
  return {
    enabled: true,
    probability: prob,
    species: second ? [first, second] : [first],
    angle,
    dist,
    animals,
  };
}

function isFreeName(b) {
  return !b || (b.boundingBox === "empty" && !/water|lava/.test(b.name));
}

/** Standable ground column near (x, z): {x, y (feet), z} or null. */
function feetNear(bot, groundAt, x, z) {
  for (const [dx, dz] of [[0, 0], [1, 0], [0, 1], [-1, 0], [0, -1], [2, 1], [-1, 2], [-2, -1], [1, -2]]) {
    const gx = Math.floor(x) + dx;
    const gz = Math.floor(z) + dz;
    const g = groundAt(bot, gx, gz);
    if (!g) continue;
    const b = bot.blockAt(g);
    if (!b || b.boundingBox !== "block" || /water|lava|leaves|ice|magma|cactus|powder_snow/.test(b.name)) continue;
    if (!isFreeName(bot.blockAt(g.offset(0, 1, 0))) || !isFreeName(bot.blockAt(g.offset(0, 2, 0)))) continue;
    return new Vec3(gx + 0.5, g.y + 1, gz + 0.5);
  }
  return null;
}

/**
 * Leader: summon the planned animals around `center`. Returns the metadata
 * (`ambient_fauna`). Never throws.
 */
async function summonAmbientFauna(bot, rcon, plan, center, groundAt) {
  if (!plan) return null;
  if (!plan.enabled) return { enabled: false, probability: plan.probability };
  const meta = {
    enabled: true,
    probability: plan.probability,
    species: plan.species,
    herd_distance: Number(plan.dist.toFixed(1)),
    herd_center: null,
    requested: plan.animals.length,
    summoned: 0,
    animals: [],
  };
  try {
    const hc = { x: center.x + Math.cos(plan.angle) * plan.dist, z: center.z + Math.sin(plan.angle) * plan.dist };
    meta.herd_center = [Math.floor(hc.x), Math.floor(hc.z)];
    for (const a of plan.animals) {
      const p = feetNear(bot, groundAt, hc.x + a.off[0], hc.z + a.off[1]);
      if (!p) {
        meta.animals.push({ type: a.id, baby: a.baby, variant: a.variant, pos: null, ok: false });
        continue;
      }
      const cmd = animalSummonCommand(a.id, p, { variantNbt: a.variantNbt, baby: a.baby, yawDeg: a.yaw });
      let ok = false;
      try {
        const res = await rcon.send(cmd);
        ok = /Summoned/i.test(String(res));
        console.log(`[${bot.username}] [fauna] ${cmd} -> ${String(res).slice(0, 100)}`);
      } catch (err) {
        console.warn(`[${bot.username}] [fauna] summon failed: ${err.message}`);
      }
      if (ok) meta.summoned++;
      meta.animals.push({
        type: a.id,
        baby: a.baby,
        variant: a.variant,
        pos: [Number(p.x.toFixed(1)), p.y, Number(p.z.toFixed(1))],
        ok,
      });
    }
  } catch (err) {
    console.warn(`[${bot.username}] [fauna] failed: ${err.message}`);
    meta.error = err.message;
  }
  return meta;
}

module.exports = { FAUNA_EXCLUDE, faunaProbability, planAmbientFauna, summonAmbientFauna };
