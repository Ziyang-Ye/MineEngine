/**
 * tntDemolish - build a small hut together, blow it up with TNT.
 *
 * Plan (shared RNG): palette (materials.js), hut size 3x3 or 4x4, wall
 * height 2-3, corner pillars (50%), 1-2 glass windows, roof (75%), 2-4 TNT
 * (inside the hut and / or against its outer walls), observer distance
 * 10-15, crater inspection (60%).
 *
 * Both bots build the hut walls (actor: the +p half incl. the door wall
 * side, other bot: the -p half), the actor adds the roof while the other
 * bot walks to its viewing spot 10-15 blocks away. The actor places the
 * TNT (inside through the door gap, and outside against the walls), lights
 * one outside TNT with flint and steel and sprints 9-12 blocks away; the
 * chain reaction demolishes the hut and leaves a crater and item drops.
 * Both watch the blast; the actor optionally walks back to look into the
 * crater. Non-damage episode: resistance 255 keeps both bots alive.
 *
 * Logged changes: block_place (hut, TNT), tnt_prime, entity_spawn (tnt,
 * items), entity_explode (every destroyed block with its old state),
 * entity_remove, block_state.
 */

const { Vec3 } = require("vec3");

const dc = require("./diverse-common");
const sc = require("../state/state-common");
const { countMaterials, nonFullFraction, paletteMetadata, pickPalette } = require("../../utils/materials");

const { randInt, note, sleep } = dc;

class TntDemolishEpisode extends dc.DiverseEpisodeBase {
  static INIT_MIN_BOTS_DISTANCE = 6;
  static INIT_MAX_BOTS_DISTANCE = 12;
  static WORKS_IN_NON_FLAT_WORLD = true;
  static SITE_HALF_SIZE = 3;
  static SITE_SEARCH_RADIUS = 20;

  /** No trees near the blast (leaf state changes would not be logged). */
  acceptSite(bot, site) {
    for (let dx = -7; dx <= 7; dx++) {
      for (let dz = -7; dz <= 7; dz++) {
        for (let dy = -2; dy <= 9; dy++) {
          const b = bot.blockAt(new Vec3(site.x + dx, site.y + dy, site.z + dz));
          if (b && (dc.isTreeBlock(b) || b.name.includes("vine") || dc.isLiquidName(b.name))) return false;
        }
      }
    }
    return true;
  }


  planEpisode(bot, rng, args, frame, isActor) {
    const palette = pickPalette(rng);
    const S = rng() < 0.5 ? 3 : 4;
    const H = rng() < 0.65 ? 3 : 2;
    const pillars = rng() < 0.5;
    const roof = rng() < 0.75;
    const nTnt = randInt(rng, 2, 4);
    const tntInside = rng() < 0.7;
    const outsideOrder = sc.shuffle(rng, [0, 1, 2, 3]);
    const observerDist = randInt(rng, 10, 15);
    const observerLeft = rng() < 0.5;
    const inspect = rng() < 0.6;
    const winSeed = rng();

    const house = dc.makeHouse(frame, {
      width: S,
      depth: S,
      height: H,
      palette,
      floor: false,
      pillars,
      doorSide: "u+",
      windowCount: H >= 3 ? (S === 4 ? 2 : 1) : 0,
      windowSeed: winSeed,
      roofStyle: "flat",
      door: false,
    });
    const walls = house.placements.filter((p) => p.part !== "roof");
    const roofList = roof ? house.placements.filter((p) => p.part === "roof") : [];
    // split the walls: actor takes dp >= 0, the other bot dp < 0
    const actorWalls = walls.filter((p) => p.local[1] >= 0);
    const otherWalls = walls.filter((p) => p.local[1] < 0);

    // TNT spots (dy = 0): inside centre, then outside the -u, +p, -p walls and beside the door
    const midP = house.p0 + Math.floor((S - 1) / 2);
    const midU = house.u0 + Math.floor((S - 1) / 2);
    const outside = [
      frame.at(house.u0 - 1, midP, 0),
      frame.at(midU, house.p1 + 1, 0),
      frame.at(midU, house.p0 - 1, 0),
      frame.at(house.u1 + 1, house.p1, 0),
    ];
    const tnt = [];
    if (tntInside) tnt.push({ pos: frame.at(midU, midP, 0), where: "inside" });
    for (const i of outsideOrder) {
      if (tnt.length >= nTnt) break;
      tnt.push({ pos: outside[i], where: "outside" });
    }
    const igniteIdx = tnt.findIndex((t) => t.where === "outside");

    const all = [...walls, ...roofList];
    const counts = countMaterials(all);
    const actorCounts = countMaterials([...actorWalls, ...roofList]);
    const otherCounts = countMaterials(otherWalls);
    const actorItems = [...Object.entries(actorCounts).map(([m, c]) => [m, c + 2]), ["tnt", tnt.length + 1], ["flint_and_steel", 1]];
    const otherItems = Object.entries(otherCounts).map(([m, c]) => [m, c + 2]);

    const retreatSide = observerLeft ? -1 : 1;
    return {
      actorStand: frame.at(house.u1 + 2, 1),
      observerStand: frame.at(house.u0 - 2, -1),
      watchStand: frame.at(Math.round(observerDist * 0.6), retreatSide * Math.round(observerDist * 0.8)),
      retreat: frame.at(house.u1 + 10, -retreatSide * 3),
      inspectStand: frame.at(house.u1 + 4, 0),
      items: { actor: actorItems, observer: otherItems },
      house,
      actorWalls,
      otherWalls,
      roofList,
      tnt,
      igniteIdx,
      inspect,
      observerDist,
      clearBox: { x1: frame.site.x - 7, z1: frame.site.z - 7, x2: frame.site.x + 7, z2: frame.site.z + 7, y1: frame.level, y2: frame.level + 2 },
      metadata: {
        hut: {
          size: [S, S],
          wall_height: H,
          pillars,
          roof,
          door_gap: [house.door.x, house.door.y, house.door.z],
          windows: house.windows.map((p) => [p.x, p.y, p.z]),
          blocks: dc.placementsMetadata(all),
        },
        materials: paletteMetadata(palette, ["walls", ...(pillars ? ["pillar"] : []), ...(house.windows.length ? ["window"] : []), ...(roof ? ["roof"] : [])]),
        material_counts: counts,
        non_full_fraction: nonFullFraction(all),
        tnt: tnt.map((t) => ({ pos: [t.pos.x, t.pos.y, t.pos.z], where: t.where })),
        ignite_index: igniteIdx >= 0 ? igniteIdx : 0,
        builders: { actor: "walls +p half, roof, tnt, ignite", other: "walls -p half" },
        observer_distance: observerDist,
        inspect_crater: inspect,
        observer_policy: "builds, then watches from 10-15 blocks (T8)",
      },
    };
  }

  buildSteps(ctx) {
    const { bot, rcon, args, episodeNum, plan, frame } = ctx;
    const hutCenter = () => plan.house.center;
    const inHut = (x, z) => plan.house.inFootprint(x, z);
    const near = () => ({ focus: hutCenter, avoid: inHut, minR: 3.5, maxR: 7 });
    const far = () => ({ focus: hutCenter, avoid: inHut, minR: Math.max(10, plan.observerDist - 1), maxR: plan.observerDist + 2, moveProb: 0.25 });
    const standOpts = { forbid: (x, z) => inHut(x, z), otherName: ctx.otherName };

    const buildMine = (list) => async () => {
      const order = dc.startNearest(bot, list);
      // keep rows bottom-up
      order.sort((a, b) => a.pos.y - b.pos.y);
      const res = await dc.buildPlacements(ctx, order, { deadline: Date.now() + 45000, glanceProb: 0.5, standOpts });
      await note(rcon, args, bot, episodeNum, "hut_walls", { placed: res.placed.length, failed: res.failed.length });
      return {};
    };

    const roofAndGo = async () => {
      if (plan.roofList.length === 0) return {};
      const res = await dc.buildPlacements(ctx, plan.roofList, { deadline: Date.now() + 35000, glanceProb: 0.1, standOpts });
      await note(rcon, args, bot, episodeNum, "hut_roof", { placed: res.placed.length, failed: res.failed.length });
      return {};
    };
    const goWatch = async () => {
      const s = plan.watchStand;
      const feet = dc.surfaceAt(bot, s.x, s.z, frame.level) || s;
      await dc.moveTo(bot, feet, { timeoutMs: 8000, avoid: inHut });
      return {};
    };

    const placeTnt = async () => {
      const placed = [];
      for (const t of plan.tnt) {
        const opts = t.where === "inside"
          ? { otherName: ctx.otherName, forbid: (x, z) => inHut(x, z) && !(x === plan.house.door.x && z === plan.house.door.z) }
          : standOpts;
        if (await dc.placeBlockAt(bot, t.pos, "tnt", opts)) placed.push(t.pos);
        await sleep(dc.rand(150, 350));
      }
      await note(rcon, args, bot, episodeNum, "place_tnt", { placed });
      return {};
    };

    const igniteAndRun = async () => {
      let primed = false;
      const t = plan.tnt[plan.igniteIdx >= 0 ? plan.igniteIdx : 0];
      const candidates = [t, ...plan.tnt.filter((x) => x !== t)];
      for (const c of candidates) {
        const b = bot.blockAt(c.pos);
        if (!b || b.name !== "tnt") continue;
        if (!(await dc.ensureReach(bot, c.pos, standOpts))) continue;
        if (!(await dc.holdItem(bot, "flint_and_steel"))) break;
        await dc.lookAtPoint(bot, new Vec3(c.pos.x + 0.5, c.pos.y + 1, c.pos.z + 0.5), 171, { randomized: false });
        await sc.useItemOnBlock(bot, bot.blockAt(c.pos), new Vec3(0, 1, 0));
        await sleep(250);
        const after = bot.blockAt(c.pos);
        if (!after || after.name !== "tnt") {
          primed = true;
          break;
        }
      }
      await note(rcon, args, bot, episodeNum, "ignite_tnt", { primed });
      const r = plan.retreat;
      const feet = dc.surfaceAt(bot, r.x, r.z, frame.level) || r;
      await dc.moveTo(bot, feet, { timeoutMs: 3800, avoid: inHut });
      await dc.lookAtPoint(bot, hutCenter(), 150);
      return { primed };
    };

    const inspect = async () => {
      if (!plan.inspect) {
        await dc.watchFor(bot, hutCenter, dc.rand(2000, 3000));
        return {};
      }
      const s = plan.inspectStand;
      const feet = dc.surfaceAt(bot, s.x, s.z, frame.level) || s;
      await dc.moveTo(bot, feet, { timeoutMs: 7000 });
      const c = frame.site;
      await dc.watchFor(bot, () => new Vec3(c.x + 0.5, c.y - 0.5, c.z + 0.5), dc.rand(2000, 3200));
      await dc.glance(ctx, 0.8, [700, 1100]);
      await note(rcon, args, bot, episodeNum, "inspect_crater", {});
      return {};
    };

    return dc.bindSteps(ctx, [
      dc.approachStep(),
      dc.dualStep("build_walls", buildMine(plan.actorWalls), buildMine(plan.otherWalls), { maxMs: 50000, observe: near }),
      dc.dualStep("roof", roofAndGo, goWatch, { maxMs: 40000, observe: far }),
      dc.dualStep("place_tnt", placeTnt, null, { maxMs: 25000, observe: far }),
      dc.dualStep("ignite", igniteAndRun, null, { maxMs: 12000, observe: far }),
      {
        name: "blast",
        run: async function () {
          await dc.watchFor(bot, hutCenter, dc.rand(4000, 5000));
          // react to the partner after the blast (partner in view, T8)
          await dc.glance(ctx, 1.0, [900, 1400]);
          return {};
        },
      },
      dc.dualStep("inspect", inspect, null, { maxMs: 15000, observe: () => ({ ...far(), minR: 6, maxR: 10 }) }),
      dc.outroStep(),
    ]);
  }
}

module.exports = { TntDemolishEpisode };
