/**
 * waterWorks - water as a building material: infinite source, fountain /
 * waterfall, waterlogging, aquatic plants, sponge.
 *
 * Different from lavaWater (lava + water -> obsidian): this episode is about
 * how water itself flows and is contained. Every water body lives in 1-deep
 * dug cells, so it never spreads over the ground the bots stand on.
 *
 * Layout (u = towards the actor, p = perpendicular, ground at dy=-1):
 *   trench    3 cells (0,-3) (0,-2) (0,-1), dug by the actor
 *   moat      4 cells (0,1) (0,3) (+1,2) (-1,2) around the pillar column (0,2),
 *             dug by the partner
 *   actor home (3,0), digs from (2,-2), pours / scoops from (1,-3);
 *   partner home (-3,0), pours from (-1,-1), works the moat from (-2,2)
 *
 * Flow:
 *   intro
 *   dig        actor digs the trench, partner digs the moat (simultaneously)
 *   pour_ends  actor pours water into one trench end, partner into the other:
 *              the middle cell becomes a source (infinite water)
 *   infinite   actor scoops the middle cell; it refills (bucket stays full)
 *   fountain   partner builds a 1-3 high pillar in the moat centre and pours
 *              water on it: on top of a 1-high pillar = fountain into all four
 *              moat cells, against the side of a taller one = waterfall into
 *              one moat cell; then scoops the source back, the water recedes
 *   aquatic    actor puts kelp or seagrass and 1-2 sea pickles into the
 *              trench sources and either a slab (waterlogged) or a sponge
 *              (absorbs the whole trench) into the middle cell; meanwhile the
 *              partner places a slab / stairs into a dry moat cell, pours a
 *              bucket into it (waterlogged) and scoops it back
 *   outro
 *
 * Logged: block_break (dig), bucket_empty / bucket_fill, liquid_flow,
 * fluid_level_change, block_place (kelp, seagrass, sea pickles, slab,
 * pillar), sponge_absorb, block_state readbacks.
 */

const { Vec3 } = require("vec3");

const {
  FACES,
  MechanicsEpisodeBase,
  blockName,
  clickBlock,
  digAt,
  glance,
  introStep,
  isFluidSource,
  localJitterMs,
  observe,
  observeUntilSignal,
  outroStep,
  pick,
  placeItemAt,
  propsOf,
  randInt,
  signalDone,
  sleep,
  toArr,
  useItemTowards,
  walkTo,
} = require("./mechanics-common");

const PILLARS = ["cobblestone", "stone_bricks", "prismarine", "mossy_stone_bricks", "andesite", "sandstone", "quartz_block", "deepslate_tiles", "prismarine_bricks"];
const SLABS = ["oak_slab", "stone_slab", "cobblestone_slab", "spruce_slab", "prismarine_slab", "brick_slab", "cut_sandstone_slab", "mud_brick_slab"];
const STAIRS = ["oak_stairs", "stone_brick_stairs", "cobblestone_stairs", "birch_stairs", "prismarine_stairs", "brick_stairs"];

class WaterWorksEpisode extends MechanicsEpisodeBase {
  static INIT_MIN_BOTS_DISTANCE = 5;
  static INIT_MAX_BOTS_DISTANCE = 9;
  static WORKS_IN_NON_FLAT_WORLD = true;
  static SITE_HALF_SIZE = 4;
  static CLEAR_HEIGHT = 5;

  planEpisode(bot, rng, args, frame, isActor) {
    const pillar = pick(rng, PILLARS);
    const h = randInt(rng, 1, 3);
    const fallSeconds = randInt(rng, 4, 6);
    const plant = rng() < 0.6 ? "kelp" : "seagrass";
    const pickles = randInt(rng, 1, 2);
    const useSponge = rng() < 0.4;
    const midSlab = pick(rng, SLABS);
    const moatBlock = rng() < 0.5 ? pick(rng, SLABS) : pick(rng, STAIRS);

    const trench = [frame.at(0, -3, -1), frame.at(0, -2, -1), frame.at(0, -1, -1)];
    const moat = [frame.at(0, 1, -1), frame.at(0, 3, -1), frame.at(1, 2, -1), frame.at(-1, 2, -1)];
    return {
      actorStand: frame.at(3, 0),
      observerStand: frame.at(-3, 0),
      actorWork: frame.at(2, -2),
      actorPour: frame.at(1, -3), // next to trench cell t1
      partnerPour: frame.at(-1, -1), // next to trench cell t3
      partnerWorkMoat: frame.at(-2, 2),
      items: {
        actor: [
          ["water_bucket", 1],
          [plant, 2],
          ["sea_pickle", pickles + 1],
          [useSponge ? "sponge" : midSlab, 1],
        ],
        observer: [
          ["water_bucket", 2],
          [pillar, h + 1],
          [moatBlock, 1],
        ],
      },
      trench,
      moat,
      pillarCol: frame.at(0, 2, 0),
      pillar,
      h,
      fallSeconds,
      plant,
      pickles,
      useSponge,
      midSlab,
      moatBlock,
      metadata: {
        trench: trench.map(toArr),
        moat: moat.map(toArr),
        pillar_material: pillar,
        pillar_height: h,
        water_feature: h === 1 ? "fountain" : "waterfall",
        fall_seconds: fallSeconds,
        aquatic_plant: plant,
        sea_pickles: pickles,
        middle_cell: useSponge ? "sponge" : midSlab,
        moat_waterlogged_block: moatBlock,
      },
    };
  }

  buildSteps(ctx) {
    const { bot, plan, isActor, frame } = ctx;
    const myU = isActor ? 1 : -1;
    const [t1, t2, t3] = plan.trench;
    const trenchFocus = () => t2.offset(0.5, 0.6, 0.5);
    const moatFocus = () => plan.pillarCol.offset(0.5, 0.8, 0.5);
    const floorPoint = (cell) => new Vec3(cell.x + 0.5, cell.y + 0.05, cell.z + 0.5);
    const isWater = (pos) => {
      const n = blockName(bot, pos);
      return n === "water";
    };

    const actorAct = (name, focus, fn, timeoutMs = 30000) => ({
      name,
      timeoutMs,
      run: async function () {
        if (!isActor) {
          await observeUntilSignal(this, focus, name, timeoutMs - 3000);
          return {};
        }
        let res = {};
        try {
          res = (await fn.call(this)) || {};
        } finally {
          signalDone(this, name);
        }
        return res;
      },
    });

    return [
      introStep(),
      {
        name: "dig",
        timeoutMs: 32000,
        run: async function () {
          const cells = isActor ? plan.trench : plan.moat;
          await walkTo(bot, isActor ? plan.actorWork : plan.partnerWorkMoat, 0.7, 6000);
          let dug = 0;
          for (const cell of cells) {
            if (this.timeUp()) break;
            if (await digAt(bot, cell, 6000)) dug++;
            await sleep(localJitterMs(200, 400));
            if (Math.random() < 0.3) await glance(this, localJitterMs(400, 700));
          }
          await this.note("dig", { what: isActor ? "trench" : "moat", dug });
          await observe(this, isActor ? trenchFocus : moatFocus, localJitterMs(600, 1000), { strafe: false });
          return {};
        },
      },
      {
        name: "pour_ends",
        timeoutMs: 22000,
        run: async function () {
          // actor -> t1 (one end), partner -> t3 (other end). Stand right
          // next to the cell: from further away the server's bucket ray hits
          // the rim and the water lands on the ground above the trench.
          await walkTo(bot, isActor ? plan.actorPour : plan.partnerPour, 0.45, 7000);
          const cell = isActor ? t1 : t3;
          await useItemTowards(bot, "water_bucket", floorPoint(cell));
          await sleep(1200);
          await this.note("pour_end", {
            cell,
            block: blockName(bot, cell),
            above: blockName(bot, cell.offset(0, 1, 0)),
            source: isFluidSource(bot.blockAt(cell)),
          });
          await observe(this, trenchFocus, localJitterMs(1500, 2200), { strafe: false });
          return {};
        },
      },
      actorAct("infinite", trenchFocus, async function () {
        const res = { before: isFluidSource(bot.blockAt(t2)) && isWater(t2) };
        await useItemTowards(bot, "bucket", t2.offset(0.5, 0.5, 0.5));
        await sleep(600);
        res.scooped_to = blockName(bot, t2);
        await observe(this, trenchFocus, localJitterMs(1600, 2200), { strafe: false });
        const b = bot.blockAt(t2);
        res.refilled = !!b && b.name === "water" && isFluidSource(b);
        res.bucket_full = !!bot.inventory.items().find((i) => i.name === "water_bucket");
        await this.note("infinite", res);
        await glance(this, localJitterMs(600, 900));
        return {};
      }),
      {
        name: "fountain",
        timeoutMs: 34000,
        run: async function () {
          if (isActor) {
            await observeUntilSignal(this, moatFocus, "fountain", 31000, { strafe: false });
            return {};
          }
          const res = { pillar: plan.pillar, h: plan.h };
          try {
            await walkTo(bot, plan.partnerWorkMoat, 0.7, 7000);
            let built = 0;
            for (let k = 0; k < plan.h; k++) {
              const cell = plan.pillarCol.offset(0, k, 0);
              if (await placeItemAt(bot, cell, plan.pillar, { face: FACES.up, refPos: cell.offset(0, -1, 0), expect: [plan.pillar] })) built++;
              else break;
              await sleep(localJitterMs(250, 450));
            }
            res.built = built;
            if (built > 0) {
              const top = plan.pillarCol.offset(0, built - 1, 0);
              let aim;
              let src;
              if (built === 1) {
                aim = new Vec3(top.x + 0.5, top.y + 1.0, top.z + 0.5); // top face
                src = top.offset(0, 1, 0);
              } else {
                const f = frame.u.scaled(myU); // side facing us
                aim = new Vec3(top.x + 0.5 + f.x * 0.5, top.y + 0.5, top.z + 0.5 + f.z * 0.5);
                src = top.plus(f);
              }
              await useItemTowards(bot, "water_bucket", aim);
              await sleep(800);
              res.source = toArr(src);
              res.source_block = blockName(bot, src);
              await glance(this, localJitterMs(500, 800));
              await observe(this, moatFocus, plan.fallSeconds * 1000, { strafe: false });
              // scoop the source back (look at the source block itself)
              for (let k = 0; k < 2; k++) {
                if (!isWater(src)) break;
                await useItemTowards(bot, "bucket", src.offset(0.5, 0.5, 0.5));
                await sleep(700);
              }
              res.scooped = !isWater(src);
              await observe(this, moatFocus, localJitterMs(2500, 3200), { strafe: false });
            }
            await this.note("fountain", res);
          } finally {
            signalDone(this, "fountain");
          }
          return {};
        },
      },
      {
        name: "aquatic",
        timeoutMs: 34000,
        run: async function () {
          if (isActor) {
            const res = { plant: plan.plant };
            await walkTo(bot, plan.actorWork, 0.7, 5000);
            // plant into the far end source (click the floor through the water)
            if (isWater(t1)) {
              await clickBlock(bot, t1.offset(0, -1, 0), { item: plan.plant, face: FACES.up });
              await sleep(500);
            }
            res.plant_block = blockName(bot, t1);
            for (let k = 0; k < plan.pickles; k++) {
              const n = blockName(bot, t3);
              if (k === 0 && n !== "water") break;
              await clickBlock(bot, k === 0 ? t3.offset(0, -1, 0) : t3, { item: "sea_pickle", face: FACES.up });
              await sleep(500);
            }
            res.pickles = Number(propsOf(bot.blockAt(t3)).pickles) || 0;
            await glance(this, localJitterMs(500, 800));
            const mid = plan.useSponge ? "sponge" : plan.midSlab;
            if (isWater(t2)) {
              await clickBlock(bot, t2.offset(0, -1, 0), { item: mid, face: FACES.up });
              await sleep(900);
            }
            res.middle = blockName(bot, t2);
            res.middle_waterlogged = propsOf(bot.blockAt(t2)).waterlogged;
            await this.note("aquatic", res);
            await observe(this, trenchFocus, localJitterMs(1800, 2600), { strafe: false });
            return {};
          }
          // partner: waterlog a slab / stairs inside a dry moat cell, then scoop it back
          const res = { block: plan.moatBlock };
          const cell = plan.moat[3]; // (-1, 2): next to the partner's work spot
          await walkTo(bot, plan.partnerWorkMoat, 0.7, 6000);
          res.placed = await placeItemAt(bot, cell, plan.moatBlock, {
            face: FACES.up,
            refPos: cell.offset(0, -1, 0),
            expect: [plan.moatBlock],
          });
          if (res.placed) {
            await sleep(400);
            await useItemTowards(bot, "water_bucket", cell.offset(0.5, 0.4, 0.5));
            await sleep(900);
            res.waterlogged = propsOf(bot.blockAt(cell)).waterlogged;
            await observe(this, () => cell.offset(0.5, 0.5, 0.5), localJitterMs(1200, 1700), { strafe: false });
            if (String(propsOf(bot.blockAt(cell)).waterlogged) === "true") {
              await useItemTowards(bot, "bucket", cell.offset(0.5, 0.4, 0.5));
              await sleep(700);
            }
            res.after = propsOf(bot.blockAt(cell)).waterlogged;
          }
          await this.note("moat_waterlog", res);
          await walkTo(bot, plan.observerStand, 0.8, 6000);
          await observe(this, trenchFocus, localJitterMs(1500, 2200));
          return {};
        },
      },
      outroStep(),
    ];
  }
}

module.exports = { WaterWorksEpisode };
