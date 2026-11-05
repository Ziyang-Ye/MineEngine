// Modified by Ziyang Ye for MineEngine, 2026 (from SolarisEngine 78f23e7); see capture/MODIFICATIONS.md
// Typical episode lengths in seconds (used for weighted random sampling)
// Weight = 1 / sqrt(length), so shorter episodes are sampled more frequently
const episodeTypicalLengths = {
  straightLineWalk: 20,
  chase: 10,
  orbit: 60,
  walkLook: 60,
  walkLookAway: 60,
  pvp: 15,
  pve: 60,
  buildStructure: 15,
  buildTower: 18, // round-2 fixer: turn-based (one builds, one watches), measured 13-19 s
  mine: 10,
  towerBridge: 37, // round-2 fixer: turn-based towers and bridges, measured ~37 s
  buildHouse: 140, // round-2 fixer: with the (short) partner glances, ~125-180 s measured
  collector: 150,
  placeAndMine: 120,
  // Stateful-capture episodes (typical lengths, seconds)
  igniteAndExtinguish: 40,
  iceNearFire: 45,
  tntBlast: 30,
  lavaWater: 40,
  buildAndBreak: 60,
  // Round 2: build / destroy (diverse/, measured in live tests)
  coBuild: 105,
  buildVsBreak: 50,
  digDown: 60,
  tunnel: 70,
  crater: 80,
  tntDemolish: 50,
  forestBuild: 80,
  // Round 2: mechanics (diverse/, measured in live tests)
  farming: 40,
  redstoneDoors: 40,
  fallingBlocks: 35,
  lightAndFire: 60,
  waterWorks: 55,
  snowIce: 45,
  // Round 2: combat / creatures (diverse/)
  pvpMelee: 45,
  pvpRanged: 50,
  pveMobs: 60,
  animalCare: 70,
  rideMount: 60,
  itemExchange: 50,
  structureEval: 60,
  translationEval: 60,
  bothLookAwayEval: 60,
  oneLooksAwayEval: 60,
  rotationEval: 60,
  turnToLookEval: 60,
  turnToLookOppositeEval: 60,
};

/**
 * Episode categories and their default shares of the episodes (03 section 4,
 * extended with the round-2 creature and mechanics types). Used by the
 * category-aware sampler (EPISODE_TYPES=diverse or a category alias): first a
 * category is drawn with these shares (renormalised over the categories that
 * have an eligible type in the current world), then a type inside the
 * category with weight multiplier / sqrt(typical length).
 *
 * Membership of the round-2 types that 03 section 4 does not list:
 *  - pveMobs (hostile mobs) -> fight (03 lists it there);
 *  - rideMount (horses, camels, pigs, boats, minecarts) -> move: it is
 *    locomotion with both players in view, like walkLook / orbit;
 *  - animalCare (breed, milk, shear, lead, tame, brush, eggs) and
 *    itemExchange (toss, chest, villager trade) -> interaction;
 *  - the six mechanics types (farming, redstoneDoors, fallingBlocks,
 *    lightAndFire, waterWorks, snowIce) -> interaction: block-state
 *    mechanics, gravity, fluids, fire, snow / ice, like the 03 "physics and
 *    fluids" types.
 * The 03 category "physics and fluids" is called `interaction` here
 * (alias `physics`); its share stays 10 % by default. Override with
 * CATEGORY_WEIGHTS / --category_weights, e.g. "interaction=0.2,build=0.3".
 */
const EPISODE_CATEGORIES = {
  build: {
    share: 0.35,
    types: [
      "buildHouse",
      "buildStructure",
      "buildTower",
      "towerBridge",
      "coBuild",
      "forestBuild",
      "buildAndBreak",
    ],
  },
  destroy: {
    share: 0.2,
    types: [
      "mine",
      "placeAndMine",
      "buildVsBreak",
      "digDown",
      "tunnel",
      "crater",
      "tntDemolish",
      "tntBlast",
    ],
  },
  fight: {
    share: 0.25,
    types: ["pvp", "pvpMelee", "pvpRanged", "pve", "pveMobs", "chase"],
  },
  move: {
    share: 0.1,
    types: [
      "walkLook",
      "walkLookAway",
      "orbit",
      "straightLineWalk",
      "collector",
      "rideMount",
    ],
  },
  interaction: {
    share: 0.1,
    types: [
      "igniteAndExtinguish",
      "iceNearFire",
      "lavaWater",
      "itemExchange",
      "animalCare",
      "farming",
      "redstoneDoors",
      "fallingBlocks",
      "lightAndFire",
      "waterWorks",
      "snowIce",
    ],
  },
};

/** Alternative names accepted in CATEGORY_WEIGHTS and EPISODE_TYPES. */
const CATEGORY_ALIASES = {
  build: "build",
  destroy: "destroy",
  dig: "destroy",
  fight: "fight",
  move: "move",
  observe: "move",
  interaction: "interaction",
  physics: "interaction",
};

/** type -> category name (null for evals / unknown types). */
function categoryOf(type) {
  for (const [name, cat] of Object.entries(EPISODE_CATEGORIES)) {
    if (cat.types.includes(type)) return name;
  }
  return null;
}

/**
 * Parse "build=0.3,physics=0.2" into {build: 0.3, interaction: 0.2}.
 * @param {string} spec
 * @returns {Object<string, number>}
 */
function parseCategoryWeights(spec) {
  const out = {};
  if (!spec) return out;
  for (const part of String(spec).split(",")) {
    const [k, v] = part.split("=").map((x) => (x || "").trim());
    const name = CATEGORY_ALIASES[k] || (k === "other" ? "other" : null);
    const num = Number(v);
    if (name && Number.isFinite(num) && num >= 0) out[name] = num;
  }
  return out;
}

/**
 * Parse "typeA=2,typeB=0.5" into {typeA: 2, typeB: 0.5}.
 * @param {string} spec
 * @returns {Object<string, number>}
 */
function parseWeightMultipliers(spec) {
  const out = {};
  if (!spec) return out;
  for (const part of String(spec).split(",")) {
    const [k, v] = part.split("=").map((x) => (x || "").trim());
    const num = Number(v);
    if (k && Number.isFinite(num) && num >= 0) out[k] = num;
  }
  return out;
}

/**
 * Built-in per-type multipliers (round-2 fixer), used when
 * EPISODE_WEIGHT_MULTIPLIERS does not name the type:
 *  - buildHouse 0.5: the upstream 5x5 house keeps the partner in view in only
 *    ~25-35 % of the frames even with the partner glances (both bots face
 *    their own walls) and runs ~130-180 s; halving its weight keeps it in the
 *    data without letting it dominate the build time.
 */
const DEFAULT_TYPE_MULTIPLIERS = Object.freeze({ buildHouse: 0.5 });

/**
 * Per-type sampling weight (before normalisation).
 * @param {string} type
 * @param {boolean} uniform
 * @param {Object<string, number>} multipliers
 * @returns {number}
 */
function typeWeight(type, uniform, multipliers) {
  const mult =
    multipliers && multipliers[type] !== undefined
      ? multipliers[type]
      : DEFAULT_TYPE_MULTIPLIERS[type] !== undefined
        ? DEFAULT_TYPE_MULTIPLIERS[type]
        : 1;
  if (uniform) return mult;
  const length = episodeTypicalLengths[type];
  if (length === undefined) {
    throw new Error(`Episode type "${type}" not found in episodeTypicalLengths`);
  }
  return mult / Math.sqrt(length);
}

/**
 * Pick one entry of `items` with weights `weights` using ONE draw `u`.
 * @returns {number} index
 */
function pickIndex(weights, u) {
  const total = weights.reduce((s, w) => s + w, 0);
  let cumulative = 0;
  for (let i = 0; i < weights.length; i++) {
    cumulative += weights[i] / total;
    if (u < cumulative) return i;
  }
  return weights.length - 1;
}

/**
 * Weighted random selection based on inverse square root of episode lengths
 * @param {string[]} episodeTypes - Array of eligible episode type names
 * @param {Function} sharedBotRng - Random number generator function (returns 0-1)
 * @param {boolean} uniform - If true, use uniform weights instead of length-based weights
 * @param {boolean} ignoreEvalEpisodes - If true, filter out episode types ending in "Eval"
 * @param {Object<string, number>} multipliers - Optional per-type weight multipliers
 * @returns {string} Selected episode type
 */
function selectWeightedEpisodeType(
  episodeTypes,
  sharedBotRng,
  uniform = false,
  ignoreEvalEpisodes = true,
  multipliers = {},
) {
  // Filter out eval episodes if requested
  const filteredTypes = ignoreEvalEpisodes
    ? episodeTypes.filter((type) => !type.toLowerCase().endsWith("eval"))
    : episodeTypes;

  if (filteredTypes.length === 0) {
    throw new Error(
      "No episode types available to sample from after filtering out eval episodes",
    );
  }

  const weights = filteredTypes.map((type) =>
    typeWeight(type, uniform, multipliers),
  );
  const totalWeight = weights.reduce((sum, w) => sum + w, 0);
  if (!(totalWeight > 0)) {
    throw new Error("All episode weights are zero (check EPISODE_WEIGHT_MULTIPLIERS)");
  }
  // Sample using the sharedBotRng (one draw)
  return filteredTypes[pickIndex(weights, sharedBotRng())];
}

/**
 * Category-aware sampling plan: probability of every type when the category
 * is drawn first (shares renormalised over categories with at least one
 * type of positive weight), then the type inside the category.
 * Types that belong to no category form the category "other" (share
 * CATEGORY_WEIGHTS.other, default 0; if ONLY uncategorised types are
 * eligible they are sampled as one category).
 * @param {string[]} episodeTypes - eligible types (evals are removed)
 * @param {Object} [opts]
 * @param {boolean} [opts.uniform=false] - uniform type weights inside a category
 * @param {Object<string, number>} [opts.multipliers] - per-type multipliers
 * @param {Object<string, number>} [opts.categoryWeights] - share overrides
 * @param {string} [opts.shareMode="episodes"] - "episodes": the category
 *   shares are shares of the episodes (round-2 behaviour); "time": shares of
 *   the recorded time - each category's episode share is divided by its
 *   expected episode length (typical lengths of its types, weighted by their
 *   in-category probabilities) and renormalised (round-2 fixer,
 *   CATEGORY_SHARE_MODE=time)
 * @returns {{categories: Array<{name: string, share: number, types: string[], typeProbs: number[],
 *   expected_length: number, time_share: number}>, probabilities: Object<string, number>}}
 */
function categoryPlan(
  episodeTypes,
  { uniform = false, multipliers = {}, categoryWeights = {}, shareMode = "episodes" } = {},
) {
  const types = episodeTypes.filter((t) => !t.toLowerCase().endsWith("eval"));
  const groups = {};
  for (const t of types) {
    const c = categoryOf(t) || "other";
    (groups[c] = groups[c] || []).push(t);
  }
  const shareOf = (name) => {
    if (categoryWeights[name] !== undefined) return categoryWeights[name];
    if (name === "other") return 0;
    return EPISODE_CATEGORIES[name].share;
  };
  // stable order: the EPISODE_CATEGORIES order, then "other"
  const order = [...Object.keys(EPISODE_CATEGORIES), "other"].filter((n) => groups[n]);
  let cats = [];
  for (const name of order) {
    const ts = groups[name];
    const w = ts.map((t) => typeWeight(t, uniform, multipliers));
    const tw = w.reduce((s, x) => s + x, 0);
    if (!(tw > 0)) continue;
    cats.push({ name, share: shareOf(name), types: ts, typeProbs: w.map((x) => x / tw) });
  }
  let total = cats.reduce((s, c) => s + c.share, 0);
  if (!(total > 0) && cats.length > 0) {
    // e.g. only uncategorised types, or all shares overridden to 0
    cats = cats.map((c) => ({ ...c, share: 1 }));
    total = cats.length;
  }
  cats = cats.filter((c) => c.share > 0).map((c) => ({ ...c, share: c.share / total }));
  if (cats.length === 0) {
    throw new Error("No episode category with a positive weight (check CATEGORY_WEIGHTS / EPISODE_WEIGHT_MULTIPLIERS)");
  }
  // expected episode length per category (typical lengths)
  for (const c of cats) {
    c.expected_length = c.types.reduce(
      (s, t, i) => s + c.typeProbs[i] * (episodeTypicalLengths[t] || 60),
      0,
    );
  }
  if (String(shareMode || "episodes").toLowerCase() === "time") {
    // the configured shares are time shares: episode share ~ share / length
    const z = cats.reduce((s, c) => s + c.share / c.expected_length, 0);
    cats = cats.map((c) => ({ ...c, share: c.share / c.expected_length / z }));
  }
  const meanLength = cats.reduce((s, c) => s + c.share * c.expected_length, 0);
  for (const c of cats) c.time_share = (c.share * c.expected_length) / meanLength;
  const probabilities = {};
  for (const c of cats) {
    c.types.forEach((t, i) => {
      probabilities[t] = c.share * c.typeProbs[i];
    });
  }
  return { categories: cats, probabilities };
}

/**
 * Category-first selection. Always consumes exactly TWO draws of the shared
 * RNG (category, then type), on both bots alike.
 * @param {string[]} episodeTypes - eligible types
 * @param {Function} sharedBotRng
 * @param {Object} [opts] - see categoryPlan
 * @returns {{type: string, category: string, category_share: number, type_probability: number}}
 */
function selectCategoryEpisodeType(episodeTypes, sharedBotRng, opts = {}) {
  const plan = categoryPlan(episodeTypes, opts);
  const u1 = sharedBotRng();
  const u2 = sharedBotRng();
  const cat = plan.categories[pickIndex(plan.categories.map((c) => c.share), u1)];
  const type = cat.types[pickIndex(cat.typeProbs, u2)];
  return {
    type,
    category: cat.name,
    category_share: Number(cat.share.toFixed(4)),
    type_probability: Number(plan.probabilities[type].toFixed(4)),
  };
}

module.exports = {
  CATEGORY_ALIASES,
  DEFAULT_TYPE_MULTIPLIERS,
  EPISODE_CATEGORIES,
  categoryOf,
  categoryPlan,
  episodeTypicalLengths,
  parseCategoryWeights,
  parseWeightMultipliers,
  selectCategoryEpisodeType,
  selectWeightedEpisodeType,
};
