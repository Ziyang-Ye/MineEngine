/**
 * creatures.js - Creature pools for the combat / creature episodes
 * (pveMobs, animalCare, rideMount; see episode-handlers/diverse/).
 *
 * Everything here is pure: given the shared bot RNG and the biome name read at
 * the episode site, both bots compute exactly the same choices. Every chooser
 * consumes a FIXED number of RNG draws for a given set of arguments, so the
 * shared RNG stays in step on both bots.
 *
 * Summons use Minecraft 1.21.0 entity NBT (Paper 1.21). When NBT is given,
 * /summon does NOT call finalizeSpawn, i.e. skeletons get no bow, pillagers no
 * crossbow, etc. - hand items are therefore always written explicitly.
 */

// Tags written on every entity summoned by these episodes (cleanup / filters).
const SUMMON_TAG = "solaris_r2";

// ----------------------------------------------------------------------------
// RNG helpers (fixed draw counts)
// ----------------------------------------------------------------------------

function randInt(rng, lo, hi) {
  return lo + Math.floor(rng() * (hi - lo + 1));
}

function pick(rng, arr) {
  return arr[Math.floor(rng() * arr.length) % arr.length];
}

/** Weighted pick, one draw. items: [{w, ...}] ; returns the item (or null if all w=0). */
function pickWeighted(rng, items, weightOf = (x) => x.w) {
  const u = rng();
  let total = 0;
  for (const it of items) total += Math.max(0, weightOf(it));
  if (total <= 0) return null;
  let acc = 0;
  for (const it of items) {
    acc += Math.max(0, weightOf(it));
    if (u * total < acc) return it;
  }
  return items[items.length - 1];
}

// ----------------------------------------------------------------------------
// Biomes
// ----------------------------------------------------------------------------

const BIOME_GROUPS = {
  desert: ["desert"],
  badlands: ["badlands", "eroded_badlands", "wooded_badlands"],
  savanna: ["savanna", "savanna_plateau", "windswept_savanna"],
  jungle: ["jungle", "sparse_jungle", "bamboo_jungle"],
  swamp: ["swamp", "mangrove_swamp"],
  snowy: [
    "snowy_plains",
    "ice_spikes",
    "snowy_taiga",
    "snowy_beach",
    "frozen_river",
    "frozen_ocean",
    "deep_frozen_ocean",
    "grove",
    "snowy_slopes",
    "frozen_peaks",
    "jagged_peaks",
  ],
  taiga: ["taiga", "old_growth_pine_taiga", "old_growth_spruce_taiga"],
  mountain: [
    "windswept_hills",
    "windswept_gravelly_hills",
    "windswept_forest",
    "stony_peaks",
    "meadow",
    "cherry_grove",
    "stony_shore",
  ],
  forest: ["forest", "flower_forest", "birch_forest", "old_growth_birch_forest", "dark_forest"],
  plains: ["plains", "sunflower_plains"],
  ocean: [
    "ocean",
    "deep_ocean",
    "warm_ocean",
    "lukewarm_ocean",
    "deep_lukewarm_ocean",
    "cold_ocean",
    "deep_cold_ocean",
  ],
  river: ["river"],
  beach: ["beach"],
  mushroom: ["mushroom_fields"],
  cave: ["dripstone_caves", "lush_caves", "deep_dark"],
};

const BIOME_TO_GROUP = {};
for (const [g, names] of Object.entries(BIOME_GROUPS)) {
  for (const n of names) BIOME_TO_GROUP[n] = g;
}

/** Strip the namespace: "minecraft:plains" -> "plains". */
function bareBiome(name) {
  if (!name) return null;
  const s = String(name);
  return s.includes(":") ? s.split(":")[1] : s;
}

/** Biome group of a biome name ("plains", "desert", ..., "other"). */
function biomeGroup(name) {
  const b = bareBiome(name);
  if (!b) return "other";
  return BIOME_TO_GROUP[b] || "other";
}

/** Warm groups (snow golems melt, water never freezes). */
function isWarmGroup(group) {
  return ["desert", "badlands", "savanna", "jungle"].includes(group);
}

/** Cold groups (water can freeze into ice by random ticks). */
function isColdGroup(group) {
  return group === "snowy";
}

/**
 * Biome at a block position as seen by Mineflayer (prismarine-block `biome`).
 * @returns {string|null} bare biome name ("plains") or null
 */
function biomeAt(bot, pos) {
  try {
    const b = bot.blockAt(pos);
    if (!b || !b.biome) return null;
    if (b.biome.name) return bareBiome(b.biome.name);
    if (typeof b.biome.id === "number" && bot.registry && bot.registry.biomes) {
      const reg = bot.registry.biomes[b.biome.id];
      if (reg && reg.name) return bareBiome(reg.name);
    }
  } catch (_) {}
  return null;
}

// ----------------------------------------------------------------------------
// NBT helpers (1.21.0 item stack / entity NBT)
// ----------------------------------------------------------------------------

function itemNbt(id, count = 1, { unbreakable = false } = {}) {
  if (!id) return "{}";
  const comps = unbreakable ? ',components:{"minecraft:unbreakable":{}}' : "";
  return `{id:"minecraft:${id}",count:${count}${comps}}`;
}

/** HandItems:[main, off] */
function handItemsNbt(main, off = null) {
  return `HandItems:[${main ? itemNbt(main) : "{}"},${off ? itemNbt(off) : "{}"}]`;
}

/** ArmorItems order is feet, legs, chest, head. */
function helmetNbt(helmet) {
  return `ArmorItems:[{},{},{},${itemNbt(helmet, 1, { unbreakable: true })}],ArmorDropChances:[0.0f,0.0f,0.0f,0.0f]`;
}

function tagsNbt(extraTags = []) {
  const tags = [SUMMON_TAG, ...extraTags].map((t) => `"${t}"`).join(",");
  return `Tags:[${tags}]`;
}

/** Join NBT fragments into a compound string. */
function compound(parts) {
  return `{${parts.filter(Boolean).join(",")}}`;
}

function fmt(n) {
  return Number(n).toFixed(2);
}

/** /summon command string. */
function summonCommand(id, pos, nbtParts = []) {
  return `summon minecraft:${id} ${fmt(pos.x)} ${fmt(pos.y)} ${fmt(pos.z)} ${compound(nbtParts)}`;
}

// ----------------------------------------------------------------------------
// Hostile / neutral mob pool (pveMobs)
// ----------------------------------------------------------------------------

/**
 * w: base weight; mult: per biome-group weight multiplier; size: [min,max]
 * group size; burns: burns in daylight (gets an unbreakable helmet by day);
 * hand: main hand item(s) (picked with one draw); needsWater: only when water
 * is near the site; neutral: has to be provoked (the bots hit it first);
 * notIn: biome groups where the mob is not used (e.g. blaze fireballs in forests).
 */
const HOSTILE_MOBS = [
  { id: "zombie", w: 10, size: [1, 3], burns: true, hand: [null, null, "iron_shovel", "stone_sword"], baby: 0.15, mult: { desert: 0.3, snowy: 0.6 } },
  { id: "husk", w: 3, size: [1, 3], hand: [null, null, "iron_shovel"], baby: 0.1, mult: { desert: 7, badlands: 4, savanna: 2 } },
  { id: "drowned", w: 2.5, size: [1, 2], burns: true, hand: [null, "trident", "fishing_rod"], needsWater: true, mult: { ocean: 6, river: 5, beach: 4, swamp: 3 } },
  { id: "skeleton", w: 8, size: [1, 2], burns: true, hand: ["bow"], mult: { snowy: 0.4, swamp: 0.6 } },
  { id: "stray", w: 2, size: [1, 2], burns: true, hand: ["bow"], mult: { snowy: 8, taiga: 1.5 } },
  { id: "bogged", w: 2, size: [1, 2], burns: true, hand: ["bow"], mult: { swamp: 8, jungle: 1.5 } },
  { id: "spider", w: 7, size: [1, 2], mult: { jungle: 2, forest: 1.5 } },
  { id: "cave_spider", w: 2, size: [1, 3], mult: { cave: 4, mountain: 1.5 } },
  { id: "creeper", w: 6, size: [1, 2], mult: { jungle: 1.5, forest: 1.3 } },
  { id: "slime", w: 3, size: [1, 2], mult: { swamp: 6 } },
  { id: "witch", w: 2, size: [1, 1], mult: { swamp: 5, forest: 1.3 } },
  { id: "pillager", w: 2, size: [1, 3], hand: ["crossbow"], mult: { taiga: 2, plains: 1.5, savanna: 1.5, desert: 1.3 } },
  { id: "vindicator", w: 1.5, size: [1, 2], hand: ["iron_axe"], mult: { forest: 2 } },
  { id: "evoker", w: 0.5, size: [1, 1], mult: { forest: 2 } },
  { id: "zombie_villager", w: 2, size: [1, 2], burns: true, hand: [null], baby: 0.1, mult: {} },
  { id: "enderman", w: 1.5, size: [1, 1], neutral: true, mult: { desert: 1.5, snowy: 1.3, plains: 1.2 } },
  { id: "zombified_piglin", w: 1.5, size: [1, 3], neutral: true, hand: ["golden_sword"], baby: 0.1, mult: { badlands: 2, desert: 1.5 } },
  { id: "piglin", w: 1, size: [1, 2], hand: ["golden_sword", "crossbow"], mult: { badlands: 2, desert: 1.5 } },
  { id: "magma_cube", w: 1, size: [1, 2], mult: { badlands: 2, desert: 1.5 } },
  { id: "blaze", w: 0.6, size: [1, 1], notIn: ["forest", "jungle", "taiga", "swamp", "savanna"], mult: { desert: 2, badlands: 2 } },
  { id: "silverfish", w: 1, size: [2, 4], mult: { mountain: 3 } },
  { id: "breeze", w: 0.6, size: [1, 1], mult: {} },
  { id: "hoglin", w: 0.4, size: [1, 1], mult: { badlands: 2 } },
  { id: "spider_jockey", w: 0.5, size: [1, 1], burns: true, mult: { jungle: 1.5, forest: 1.5 } },
  { id: "chicken_jockey", w: 0.3, size: [1, 1], mult: {} },
];

// Friendly helpers that fight hostile mobs (optional in pveMobs).
// No snow golem: its snow trail flips grass_block[snowy] below the snow
// layer through a neighbour update the world log does not re-read
// (replay mismatches), see the r2-combat-creatures schema notes.
const ALLY_MOBS = [{ id: "iron_golem", w: 3, mult: {} }];

const HELMETS = ["leather_helmet", "chainmail_helmet", "iron_helmet", "golden_helmet", "diamond_helmet", "carved_pumpkin"];

const VILLAGER_TYPE_BY_GROUP = {
  desert: "desert",
  badlands: "desert",
  savanna: "savanna",
  jungle: "jungle",
  swamp: "swamp",
  snowy: "snow",
  taiga: "taiga",
};

function villagerTypeFor(group) {
  return VILLAGER_TYPE_BY_GROUP[group] || "plains";
}

const VILLAGER_PROFESSIONS = [
  "farmer",
  "fisherman",
  "shepherd",
  "fletcher",
  "librarian",
  "cartographer",
  "cleric",
  "armorer",
  "weaponsmith",
  "toolsmith",
  "butcher",
  "leatherworker",
  "mason",
];

function mobWeight(entry, group, opts) {
  if (entry.notIn && entry.notIn.includes(group)) return 0;
  if (entry.needsWater && !opts.waterNear) return 0;
  if (opts.exclude && opts.exclude.includes(entry.id)) return 0;
  const m = entry.mult && entry.mult[group] !== undefined ? entry.mult[group] : 1;
  return entry.w * m;
}

/**
 * Choose the hostile group of a pveMobs episode.
 *
 * Draws: 1 (type count) + 3 per type slot (type, count, variant seed) +
 * 4 per mob (hand, baby, helmet, size) - always the maximum number of slots /
 * mobs, so the RNG consumption is fixed.
 *
 * @param {Function} rng
 * @param {string} biome - bare biome name (or null)
 * @param {Object} opts
 * @param {boolean} opts.isDay - daylight: burning mobs get unbreakable helmets
 * @param {boolean} opts.waterNear - allows drowned
 * @param {number} [opts.maxTypes=3]
 * @param {number} [opts.maxTotal=7]
 * @returns {{group: string, mobs: Array<{id: string, summonId: string, hand: string|null,
 *   baby: boolean, helmet: string|null, size: number|null, neutral: boolean, villagerType: string|null}>,
 *   types: Object<string, number>}}
 */
function chooseHostileGroup(rng, biome, opts = {}) {
  const group = biomeGroup(biome);
  const maxTypes = opts.maxTypes || 3;
  const maxTotal = opts.maxTotal || 7;
  const MAX_PER_TYPE = 4;
  const minTypes = Math.min(maxTypes, opts.minTypes || 2);
  let nTypes = minTypes + Math.floor(rng() * (maxTypes - minTypes + 1)); // minTypes..maxTypes
  // Test hook (opts.force = ["witch", "slime"]): these types instead of the
  // weighted pick; the RNG draws are unchanged.
  const force = (opts.force || [])
    .map((id) => HOSTILE_MOBS.find((e) => e.id === id))
    .filter(Boolean)
    .slice(0, maxTypes);
  if (force.length) nTypes = force.length;
  const chosen = [];
  const used = new Set();
  for (let slot = 0; slot < maxTypes; slot++) {
    const uType = rng();
    const uCount = rng();
    const uVar = rng();
    if (slot >= nTypes) continue;
    if (force.length) {
      const fe = force[slot];
      used.add(fe.id);
      const [flo, fhi] = fe.size;
      chosen.push({ entry: fe, count: flo + Math.floor(uCount * (fhi - flo + 1)), uVar });
      continue;
    }
    const pool = HOSTILE_MOBS.map((e) => ({
      e,
      w: used.has(e.id) ? 0 : mobWeight(e, group, opts),
    }));
    let total = pool.reduce((a, p) => a + p.w, 0);
    if (total <= 0) continue;
    let acc = 0;
    let entry = null;
    for (const p of pool) {
      acc += p.w;
      if (uType * total < acc) {
        entry = p.e;
        break;
      }
    }
    if (!entry) entry = pool[pool.length - 1].e;
    used.add(entry.id);
    const [lo, hi] = entry.size;
    const count = lo + Math.floor(uCount * (hi - lo + 1));
    chosen.push({ entry, count, uVar });
  }
  // Cap the total.
  let total = chosen.reduce((a, c) => a + c.count, 0);
  while (total > maxTotal) {
    const big = chosen.reduce((a, c) => (c.count > a.count ? c : a), chosen[0]);
    big.count -= 1;
    total -= 1;
  }
  const mobs = [];
  let mobIndex = 0;
  for (const c of chosen) {
    for (let i = 0; i < MAX_PER_TYPE; i++) {
      const uHand = rng();
      const uBaby = rng();
      const uHelmet = rng();
      const uSize = rng();
      if (i >= c.count) continue;
      const e = c.entry;
      const hand = e.hand ? e.hand[Math.floor(uHand * e.hand.length) % e.hand.length] : null;
      const baby = Boolean(e.baby && uBaby < e.baby);
      let helmet = null;
      if (e.burns) {
        if (opts.isDay || uBaby > 0.6) helmet = HELMETS[Math.floor(uHelmet * HELMETS.length) % HELMETS.length];
      }
      let size = null;
      if (e.id === "slime" || e.id === "magma_cube") size = Math.floor(uSize * 3); // 0..2
      mobs.push({
        index: mobIndex++,
        id: e.id,
        summonId: e.id === "spider_jockey" ? "spider" : e.id === "chicken_jockey" ? "chicken" : e.id,
        hand,
        baby,
        helmet,
        size,
        neutral: Boolean(e.neutral),
        villagerType: e.id === "zombie_villager" ? villagerTypeFor(group) : null,
        profession: e.id === "zombie_villager" ? VILLAGER_PROFESSIONS[Math.floor(uSize * VILLAGER_PROFESSIONS.length) % VILLAGER_PROFESSIONS.length] : null,
      });
    }
  }
  const types = {};
  for (const m of mobs) types[m.id] = (types[m.id] || 0) + 1;
  return { group, mobs, types };
}

/** One draw: optional ally (iron golem / snow golem) or null. */
function chooseAlly(rng, biome, probability = 0.25) {
  const group = biomeGroup(biome);
  const u = rng();
  if (u >= probability) return null;
  const pool = ALLY_MOBS.map((e) => ({ e, w: e.notIn && e.notIn.includes(group) ? 0 : e.w * (e.mult[group] || 1) }));
  const total = pool.reduce((a, p) => a + p.w, 0);
  let acc = 0;
  const v = (u / probability) * total;
  for (const p of pool) {
    acc += p.w;
    if (v < acc) return p.e.id;
  }
  return "iron_golem";
}

/**
 * /summon command for one chosen hostile mob (see chooseHostileGroup).
 * @param {Object} mob
 * @param {{x,y,z}} pos
 * @param {number} yawDeg - initial yaw (Minecraft degrees)
 */
function hostileSummonCommand(mob, pos, yawDeg = 0) {
  const parts = ["PersistenceRequired:1b", tagsNbt(["solaris_r2_hostile", `solaris_r2_mob_${mob.index}`]), `Rotation:[${fmt(yawDeg)}f,0.0f]`];
  if (mob.hand) parts.push(handItemsNbt(mob.hand));
  if (mob.helmet) parts.push(helmetNbt(mob.helmet));
  if (mob.baby) parts.push("IsBaby:1b");
  if (mob.size !== null && mob.size !== undefined) parts.push(`Size:${mob.size}`);
  if (mob.id === "zombie_villager") {
    parts.push(`VillagerData:{type:"minecraft:${mob.villagerType}",profession:"minecraft:${mob.profession}",level:1}`);
  }
  if (mob.id === "piglin" || mob.id === "hoglin") parts.push("IsImmuneToZombification:1b");
  if (mob.id === "enderman") parts.push('carriedBlockState:{Name:"minecraft:grass_block",Properties:{snowy:"false"}}');
  if (mob.id === "spider_jockey") {
    parts.push(
      `Passengers:[{id:"minecraft:skeleton",PersistenceRequired:1b,${tagsNbt(["solaris_r2_hostile"])},${handItemsNbt("bow")}${mob.helmet ? "," + helmetNbt(mob.helmet) : ""}}]`,
    );
  }
  if (mob.id === "chicken_jockey") {
    parts.push(
      `Passengers:[{id:"minecraft:zombie",IsBaby:1b,PersistenceRequired:1b,${tagsNbt(["solaris_r2_hostile"])}${mob.helmet ? "," + helmetNbt(mob.helmet) : ',ArmorItems:[{},{},{},{id:"minecraft:leather_helmet",count:1,components:{"minecraft:unbreakable":{}}}]'}}]`,
    );
  }
  return summonCommand(mob.summonId, pos, parts);
}

function allySummonCommand(id, pos, yawDeg = 0) {
  const parts = ["PersistenceRequired:1b", tagsNbt(["solaris_r2_ally"]), `Rotation:[${fmt(yawDeg)}f,0.0f]`];
  if (id === "iron_golem") parts.push("PlayerCreated:1b");
  if (id === "snow_golem") parts.push("Pumpkin:1b");
  return summonCommand(id, pos, parts);
}

/** Mineflayer entity names that count as targets of the pveMobs fight. */
// (jockeys: the spider / skeleton / baby zombie parts are targeted; the
// chicken of a chicken jockey is not)
const HOSTILE_ENTITY_NAMES = new Set([
  ...HOSTILE_MOBS.map((m) => m.id).filter((id) => !id.endsWith("_jockey")),
  "vex",
]);

// ----------------------------------------------------------------------------
// Passive / neutral animals (animalCare, rideMount)
// ----------------------------------------------------------------------------

/**
 * food: items that breed / feed it (first is the "main" one); breed: can be bred
 * by feeding two adults; milk: item used to milk; shear: can be sheared;
 * leash: can be leashed; tame: item that tames it; tameRequiredToBreed.
 */
const ANIMALS = {
  cow: { food: ["wheat"], breed: true, milk: "bucket", leash: true, w: 6, mult: { plains: 2, forest: 1.5, mountain: 1.3, desert: 0.2, snowy: 0.5 } },
  mooshroom: { food: ["wheat"], breed: true, milk: "bowl", shear: true, leash: true, w: 0.8, mult: { mushroom: 20 } },
  sheep: { food: ["wheat"], breed: true, shear: true, dye: true, leash: true, w: 6, mult: { plains: 2, mountain: 2, desert: 0.2 } },
  pig: { food: ["carrot", "potato", "beetroot"], breed: true, leash: true, w: 5, mult: { plains: 1.5, forest: 1.5, desert: 0.2 } },
  chicken: { food: ["wheat_seeds", "melon_seeds", "pumpkin_seeds", "beetroot_seeds"], breed: true, leash: true, w: 5, mult: { plains: 1.5, jungle: 1.5 } },
  rabbit: { food: ["carrot", "dandelion", "golden_carrot"], breed: true, leash: true, w: 3, mult: { desert: 4, snowy: 3, taiga: 2, plains: 1 } },
  goat: { food: ["wheat"], breed: true, milk: "bucket", leash: true, w: 2, mult: { mountain: 5, snowy: 4 } },
  horse: { food: ["golden_carrot", "golden_apple"], breed: true, tameRequiredToBreed: true, leash: true, w: 2, mult: { plains: 3, savanna: 3 } },
  donkey: { food: ["golden_carrot"], breed: true, tameRequiredToBreed: true, leash: true, w: 1.5, mult: { plains: 2, savanna: 2 } },
  llama: { food: ["hay_block"], breed: true, tameRequiredToBreed: true, leash: true, w: 1.5, mult: { savanna: 4, mountain: 4 } },
  camel: { food: ["cactus"], breed: true, leash: true, w: 1, mult: { desert: 10 } },
  wolf: { food: ["beef", "porkchop", "chicken", "mutton"], tame: "bone", breed: true, tameRequiredToBreed: true, leash: true, w: 3, mult: { taiga: 4, forest: 2, snowy: 3, savanna: 2, badlands: 2 } },
  cat: { food: ["cod", "salmon"], tame: "cod", breed: true, tameRequiredToBreed: true, leash: true, w: 2, mult: { plains: 1.5, swamp: 2 } },
  ocelot: { food: ["cod", "salmon"], tame: "cod", breed: true, leash: false, w: 0.8, mult: { jungle: 10 } },
  fox: { food: ["sweet_berries", "glow_berries"], breed: true, leash: true, w: 1, mult: { taiga: 6, snowy: 4 } },
  panda: { food: ["bamboo"], breed: false, leash: true, w: 0.6, mult: { jungle: 10 } },
  bee: { food: ["dandelion", "poppy", "cornflower", "oxeye_daisy"], breed: true, leash: true, w: 1.5, mult: { plains: 2, forest: 2, mountain: 2 } },
  frog: { food: ["slime_ball"], breed: false, leash: true, w: 1, mult: { swamp: 10 } },
  // breed: true = seagrass makes it grow / love; animalCare only uses it for
  // feed_baby (baby turtles grow when fed; see ANIMALS_FOR there)
  turtle: { food: ["seagrass"], breed: true, leash: false, w: 0.6, mult: { beach: 10, ocean: 3 } },
  armadillo: { food: ["spider_eye"], breed: true, brush: true, leash: true, w: 1.2, mult: { savanna: 8, badlands: 8 } },
  parrot: { food: ["wheat_seeds", "melon_seeds"], tame: "wheat_seeds", breed: false, leash: true, w: 0.6, mult: { jungle: 10 } },
};

const WOLF_VARIANTS_BY_GROUP = {
  taiga: ["pale", "woods", "rusty"],
  snowy: ["snowy", "ashen"],
  forest: ["woods", "pale"],
  savanna: ["spotted"],
  badlands: ["striped"],
  jungle: ["rusty"],
  mountain: ["black", "chestnut"],
};
const ALL_WOLF_VARIANTS = ["pale", "ashen", "black", "chestnut", "rusty", "snowy", "spotted", "striped", "woods"];
const CAT_VARIANTS = ["tabby", "black", "red", "siamese", "british_shorthair", "calico", "persian", "ragdoll", "white", "jellie", "all_black"];
const FROG_VARIANTS_BY_GROUP = { swamp: "temperate", jungle: "warm", desert: "warm", badlands: "warm", savanna: "warm", snowy: "cold", taiga: "cold", mountain: "cold" };
const DYE_COLORS = [
  "white", "orange", "magenta", "light_blue", "yellow", "lime", "pink", "gray",
  "light_gray", "cyan", "purple", "blue", "brown", "green", "red", "black",
];

function animalWeight(id, group, opts = {}) {
  const a = ANIMALS[id];
  if (!a) return 0;
  if (opts.exclude && opts.exclude.includes(id)) return 0;
  const m = a.mult && a.mult[group] !== undefined ? a.mult[group] : 1;
  return a.w * m;
}

/**
 * Pick one animal id satisfying `filter`, biome weighted. One draw.
 */
function chooseAnimal(rng, biome, filter = () => true, opts = {}) {
  const group = biomeGroup(biome);
  const items = Object.keys(ANIMALS).map((id) => ({ id, w: filter(id, ANIMALS[id]) ? animalWeight(id, group, opts) : 0 }));
  const it = pickWeighted(rng, items);
  return it ? it.id : null;
}

/**
 * Variant NBT for an animal (two draws, always).
 * @returns {{nbt: string[], variant: string|null}}
 */
function animalVariant(rng, id, biome) {
  const group = biomeGroup(biome);
  const u1 = rng();
  const u2 = rng();
  const nbt = [];
  let variant = null;
  switch (id) {
    case "sheep": {
      // natural colours mostly, sometimes a dyed-looking one
      const natural = ["white", "white", "white", "black", "gray", "light_gray", "brown", "pink"];
      const color = u2 < 0.25 ? DYE_COLORS[Math.floor(u1 * 16) % 16] : natural[Math.floor(u1 * natural.length) % natural.length];
      nbt.push(`Color:${DYE_COLORS.indexOf(color)}b`);
      variant = color;
      break;
    }
    case "wolf": {
      const pool = WOLF_VARIANTS_BY_GROUP[group] && u2 < 0.75 ? WOLF_VARIANTS_BY_GROUP[group] : ALL_WOLF_VARIANTS;
      variant = pool[Math.floor(u1 * pool.length) % pool.length];
      nbt.push(`variant:"minecraft:${variant}"`);
      break;
    }
    case "cat":
      variant = CAT_VARIANTS[Math.floor(u1 * CAT_VARIANTS.length) % CAT_VARIANTS.length];
      nbt.push(`variant:"minecraft:${variant}"`);
      break;
    case "frog":
      variant = FROG_VARIANTS_BY_GROUP[group] || ["temperate", "warm", "cold"][Math.floor(u1 * 3) % 3];
      nbt.push(`variant:"minecraft:${variant}"`);
      break;
    case "rabbit": {
      const byGroup = { desert: [4], snowy: [1, 3], taiga: [0, 2, 5] };
      const pool = byGroup[group] && u2 < 0.8 ? byGroup[group] : [0, 1, 2, 3, 4, 5];
      const t = pool[Math.floor(u1 * pool.length) % pool.length];
      nbt.push(`RabbitType:${t}`);
      variant = String(t);
      break;
    }
    case "horse": {
      const color = Math.floor(u1 * 7);
      const marking = Math.floor(u2 * 5);
      const v = color + 256 * marking;
      nbt.push(`Variant:${v}`);
      variant = String(v);
      break;
    }
    case "llama": {
      const v = Math.floor(u1 * 4);
      nbt.push(`Variant:${v}`);
      variant = String(v);
      break;
    }
    case "fox":
      variant = group === "snowy" || u1 < 0.2 ? "snow" : "red";
      nbt.push(`Type:"${variant}"`);
      break;
    case "parrot": {
      const v = Math.floor(u1 * 5);
      nbt.push(`Variant:${v}`);
      variant = String(v);
      break;
    }
    case "mooshroom":
      variant = u1 < 0.15 ? "brown" : "red";
      nbt.push(`Type:"${variant}"`);
      break;
    case "panda": {
      const genes = ["normal", "lazy", "worried", "playful", "aggressive", "weak", "brown"];
      variant = genes[Math.floor(u1 * genes.length) % genes.length];
      nbt.push(`MainGene:"${variant}",HiddenGene:"${variant}"`);
      break;
    }
    case "axolotl": {
      const v = Math.floor(u1 * 4);
      nbt.push(`Variant:${v}`);
      variant = String(v);
      break;
    }
    default:
      break;
  }
  return { nbt, variant };
}

/**
 * /summon for a passive animal.
 * @param {string} id
 * @param {{x,y,z}} pos
 * @param {Object} o
 * @param {string[]} [o.variantNbt]
 * @param {boolean} [o.baby]
 * @param {boolean} [o.tame] - Tame:1b (horses/llamas/donkeys)
 * @param {boolean} [o.saddle]
 * @param {number} [o.yawDeg]
 * @param {string[]} [o.extra] - additional NBT fragments
 */
function animalSummonCommand(id, pos, o = {}) {
  const parts = ["PersistenceRequired:1b", tagsNbt(["solaris_r2_animal"]), `Rotation:[${fmt(o.yawDeg || 0)}f,0.0f]`];
  if (o.variantNbt) parts.push(...o.variantNbt);
  if (o.baby) parts.push("Age:-24000");
  else parts.push("Age:0");
  if (o.tame) parts.push("Tame:1b");
  if (o.saddle) {
    if (id === "pig" || id === "strider") parts.push("Saddle:1b");
    else parts.push(`SaddleItem:${itemNbt("saddle")}`);
  }
  if (o.extra) parts.push(...o.extra);
  return summonCommand(id, pos, parts);
}

module.exports = {
  ALLY_MOBS,
  ANIMALS,
  BIOME_GROUPS,
  CAT_VARIANTS,
  DYE_COLORS,
  HELMETS,
  HOSTILE_ENTITY_NAMES,
  HOSTILE_MOBS,
  SUMMON_TAG,
  VILLAGER_PROFESSIONS,
  allySummonCommand,
  animalSummonCommand,
  animalVariant,
  animalWeight,
  bareBiome,
  biomeAt,
  biomeGroup,
  chooseAlly,
  chooseAnimal,
  chooseHostileGroup,
  compound,
  handItemsNbt,
  helmetNbt,
  hostileSummonCommand,
  isColdGroup,
  isWarmGroup,
  itemNbt,
  pick,
  pickWeighted,
  randInt,
  summonCommand,
  tagsNbt,
  villagerTypeFor,
};
