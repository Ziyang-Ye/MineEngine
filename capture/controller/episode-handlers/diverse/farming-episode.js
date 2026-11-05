/**
 * farming - tilling, planting, bone meal growth, harvest and replanting.
 *
 * Layout (frame u = towards the actor, p = perpendicular, level = feet):
 *
 *      partner (u=-3)      row B (u=-1)   walkway (u=0)   row A (u=+1)      actor (u=+3)
 *
 * Both bots work their own crop row while facing each other across the
 * field (distance 6), so each one keeps the other in view while working.
 *
 * Flow:
 *   intro      both look at the field, eye contact
 *   till       both till their row with a hoe (coarse dirt takes two clicks:
 *              coarse_dirt -> dirt -> farmland)
 *   plant      both plant their crop (wheat / carrots / potatoes / beetroots)
 *   bonemeal   both apply bone meal to some crops (1-3 times each); with
 *              p=0.35 the actor also bone-meals a grass block next to the
 *              field (grass and flowers appear around it)
 *   harvest    both break the crops of their row (mature ones drop produce)
 *   collect    both walk along the row edge (picks up the drops, never steps
 *              onto farmland)
 *   replant    both replant the harvested cells
 *   outro      eye contact
 *
 * No water is placed (see r2-mechanics.md: farmland hydration is a
 * MoistureChangeEvent, which the logger does not record); farmland with a
 * crop on it never reverts, unplanted farmland may turn back to dirt
 * (block_fade, logged).
 *
 * Logged state changes: player_interact + block_state (dirt -> farmland,
 * coarse_dirt -> dirt), block_place (crops), block_fertilize (bone meal),
 * block_grow (natural growth), block_break (harvest), entity_spawn (items).
 */

const {
  FACES,
  MechanicsEpisodeBase,
  blockName,
  clickBlock,
  digAt,
  glance,
  introStep,
  localJitterMs,
  observe,
  outroStep,
  pick,
  propsOf,
  randInt,
  shuffle,
  sleep,
  toArr,
  walkTo,
} = require("./mechanics-common");

const CROPS = [
  { seed: "wheat_seeds", block: "wheat", maxAge: 7 },
  { seed: "carrot", block: "carrots", maxAge: 7 },
  { seed: "potato", block: "potatoes", maxAge: 7 },
  { seed: "beetroot_seeds", block: "beetroots", maxAge: 3 },
];
const HOES = ["wooden_hoe", "stone_hoe", "iron_hoe", "golden_hoe", "diamond_hoe", "netherite_hoe"];
const TILLABLE = new Set(["grass_block", "dirt", "coarse_dirt", "dirt_path", "rooted_dirt"]);

class FarmingEpisode extends MechanicsEpisodeBase {
  static INIT_MIN_BOTS_DISTANCE = 5;
  static INIT_MAX_BOTS_DISTANCE = 9;
  static WORKS_IN_NON_FLAT_WORLD = true;
  static SITE_HALF_SIZE = 4;
  // rain hydrates farmland (MoistureChangeEvent, not logged)
  static PREFERRED_WEATHER = "clear";

  siteInfo(bot, site) {
    // are all ground blocks of the 5x5 field area tillable?
    let tillable = true;
    for (let dx = -2; dx <= 2; dx++) {
      for (let dz = -2; dz <= 2; dz++) {
        if (!TILLABLE.has(blockName(bot, site.offset(dx, 0, dz)))) tillable = false;
      }
    }
    return { field_tillable: tillable };
  }

  planEpisode(bot, rng, args, frame, isActor, siteInfo = {}) {
    const n = rng() < 0.5 ? 3 : 5;
    const k = (n - 1) / 2;
    const crops = shuffle(rng, CROPS);
    const cropA = crops[0];
    const cropB = crops[1];
    const hoeA = pick(rng, HOES);
    const hoeB = pick(rng, HOES);
    const uGround = rng();
    let ground = uGround < 0.6 ? "keep" : uGround < 0.85 ? "dirt" : "coarse_dirt";
    if (ground === "keep" && siteInfo.field_tillable === false) ground = "grass_block";
    const bonemealGrass = rng() < 0.35;
    const bmA = randInt(rng, 2, 3);
    const bmB = randInt(rng, 2, 3);
    const harvestAll = rng() < 0.6;

    const rowA = [];
    const rowB = [];
    for (let dp = -k; dp <= k; dp++) {
      rowA.push(frame.at(1, dp, -1));
      rowB.push(frame.at(-1, dp, -1));
    }
    const grassCell = frame.at(0, k + 1, -1);
    const leaderSetupCommands = [];
    if (ground !== "keep") {
      // rows (u=-1..+1, p=-k..k): pre-snapshot ground material
      const a = frame.at(-1, -k, -1);
      const b = frame.at(1, k, -1);
      leaderSetupCommands.push(
        `fill ${Math.min(a.x, b.x)} ${a.y} ${Math.min(a.z, b.z)} ${Math.max(a.x, b.x)} ${a.y} ${Math.max(a.z, b.z)} minecraft:${ground}`,
      );
    }
    if (bonemealGrass) {
      leaderSetupCommands.push(`setblock ${grassCell.x} ${grassCell.y} ${grassCell.z} minecraft:grass_block`);
    }

    return {
      actorStand: frame.at(3, 0),
      observerStand: frame.at(-3, 0),
      items: {
        actor: [
          [hoeA, 1],
          [cropA.seed, n * 2 + 2],
          ["bone_meal", 16],
        ],
        observer: [
          [hoeB, 1],
          [cropB.seed, n * 2 + 2],
          ["bone_meal", 16],
        ],
      },
      leaderSetupCommands,
      rowA,
      rowB,
      cropA,
      cropB,
      hoeA,
      hoeB,
      bmA,
      bmB,
      k,
      grassCell,
      bonemealGrass,
      harvestAll,
      metadata: {
        field_size: [2, n],
        ground_material: ground,
        actor_crop: cropA.block,
        observer_crop: cropB.block,
        actor_seed: cropA.seed,
        observer_seed: cropB.seed,
        actor_hoe: hoeA,
        observer_hoe: hoeB,
        actor_row: rowA.map(toArr),
        observer_row: rowB.map(toArr),
        bonemeal_counts: { actor: bmA, observer: bmB },
        bonemeal_grass: bonemealGrass ? toArr(grassCell) : null,
        harvest_all: harvestAll,
        irrigation: false,
      },
    };
  }

  buildSteps(ctx) {
    const { bot, plan, isActor, frame } = ctx;
    const row = isActor ? plan.rowA : plan.rowB;
    const crop = isActor ? plan.cropA : plan.cropB;
    const hoe = isActor ? plan.hoeA : plan.hoeB;
    const bmCount = isActor ? plan.bmA : plan.bmB;
    const myU = isActor ? 1 : -1;
    const homeStand = isActor ? plan.actorStand : plan.observerStand;
    const rowFocus = () => frame.at(myU * 0.5, 0, -0.2).offset(0.5, 0, 0.5);
    // glance at the partner now and then (Math.random: local timing only)
    const maybeGlance = async () => {
      if (Math.random() < 0.3) await glance(ctx, localJitterMs(350, 700));
    };
    const cropAt = (g) => g.offset(0, 1, 0);

    return [
      introStep(),
      {
        name: "till",
        timeoutMs: 30000,
        run: async function () {
          let tilled = 0;
          for (const g of row) {
            if (this.timeUp()) break;
            for (let attempt = 0; attempt < 3; attempt++) {
              const name = blockName(bot, g);
              if (name === "farmland") break;
              if (!name || !["grass_block", "dirt", "coarse_dirt", "dirt_path", "rooted_dirt"].includes(name)) break;
              await clickBlock(bot, g, { item: hoe, face: FACES.up });
              await sleep(localJitterMs(280, 450));
            }
            if (blockName(bot, g) === "farmland") tilled++;
            await maybeGlance();
          }
          await this.note("till", { tool: hoe, tilled, row: row[0] });
          await observe(this, rowFocus, localJitterMs(600, 1000), { strafe: false });
          return { tilled };
        },
      },
      {
        name: "plant",
        timeoutMs: 25000,
        run: async function () {
          let planted = 0;
          for (const g of row) {
            if (this.timeUp()) break;
            if (blockName(bot, g) !== "farmland") continue;
            if (blockName(bot, cropAt(g)) !== "air") continue;
            await clickBlock(bot, g, { item: crop.seed, face: FACES.up });
            await sleep(localJitterMs(300, 500));
            if (blockName(bot, cropAt(g)) === crop.block) planted++;
            await maybeGlance();
          }
          await this.note("plant", { crop: crop.block, planted });
          await observe(this, rowFocus, localJitterMs(800, 1300), { strafe: false });
          return { planted };
        },
      },
      {
        name: "bonemeal",
        timeoutMs: 30000,
        run: async function () {
          const cells = row.filter((g) => blockName(bot, cropAt(g)) === crop.block);
          let uses = 0;
          // deterministic order: centre outwards
          const ordered = cells.slice().sort((a, b) => Math.abs(a.x + a.z) - Math.abs(b.x + b.z));
          for (const g of ordered.slice(0, bmCount)) {
            const times = 1 + ((g.x + g.z) & 1) + (Math.random() < 0.4 ? 1 : 0);
            for (let t = 0; t < times; t++) {
              if (this.timeUp()) break;
              const c = bot.blockAt(cropAt(g));
              if (!c || c.name !== crop.block) break;
              if (Number(propsOf(c).age) >= crop.maxAge) break;
              await clickBlock(bot, cropAt(g), { item: "bone_meal", face: FACES.up });
              uses++;
              await sleep(localJitterMs(450, 750));
            }
            await maybeGlance();
          }
          let grass = null;
          if (isActor && plan.bonemealGrass && !this.timeUp() && blockName(bot, plan.grassCell) === "grass_block") {
            await clickBlock(bot, plan.grassCell, { item: "bone_meal", face: FACES.up });
            grass = plan.grassCell;
            await sleep(500);
          }
          await this.note("bonemeal", { crop: crop.block, uses, grass });
          await observe(this, rowFocus, localJitterMs(1500, 2500), { strafe: false });
          return { uses };
        },
      },
      {
        name: "harvest",
        timeoutMs: 30000,
        run: async function () {
          const ages = [];
          let broken = 0;
          for (const g of row) {
            if (this.timeUp()) break;
            const c = bot.blockAt(cropAt(g));
            if (!c || c.name !== crop.block) continue;
            const age = Number(propsOf(c).age);
            if (!plan.harvestAll && age < crop.maxAge) continue;
            ages.push(age);
            if (await digAt(bot, cropAt(g), 4000)) broken++;
            await sleep(localJitterMs(250, 450));
            await maybeGlance();
          }
          await this.note("harvest", { crop: crop.block, broken, ages });
          return { broken };
        },
      },
      {
        name: "collect",
        timeoutMs: 22000,
        run: async function () {
          // walk along the row edge (u = +-2): picks up the drops lying on the
          // farmland next to it without stepping onto the farmland
          const edgeU = myU * 2;
          const a = frame.at(edgeU, -plan.k);
          const b = frame.at(edgeU, plan.k);
          await walkTo(bot, a, 0.6, 6000);
          await walkTo(bot, b, 0.6, 7000);
          await walkTo(bot, homeStand, 0.7, 6000);
          await glance(this, localJitterMs(600, 900));
          return {};
        },
      },
      {
        name: "replant",
        timeoutMs: 25000,
        run: async function () {
          let replanted = 0;
          for (const g of row) {
            if (this.timeUp()) break;
            if (blockName(bot, g) !== "farmland" || blockName(bot, cropAt(g)) !== "air") continue;
            await clickBlock(bot, g, { item: crop.seed, face: FACES.up });
            await sleep(localJitterMs(300, 500));
            if (blockName(bot, cropAt(g)) === crop.block) replanted++;
          }
          await this.note("replant", { crop: crop.block, replanted });
          await observe(this, rowFocus, localJitterMs(1200, 2000), { strafe: false });
          return {};
        },
      },
      outroStep(),
    ];
  }
}

module.exports = { FarmingEpisode };
