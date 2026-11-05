/**
 * igniteAndExtinguish - fire as an explicit world-state change.
 *
 * Flow (both bots record the whole time):
 *   approach    both face the work site
 *   build       actor places 2-4 flammable blocks (planks / log / hay / wool)
 *   ignite      actor lights one of them with flint and steel
 *   burn        3-7 s of fire (spread, burn-out); if the observer is the
 *               extinguisher it walks up to the site meanwhile
 *   extinguish  extinguisher pours a water bucket on the fire; optionally
 *               scoops the water source back up with the empty bucket
 *   outro       eye contact
 *
 * Logged state changes: block_place, block_ignite, block_spread, block_burn,
 * block_fade (fire out), bucket_empty / liquid_flow, bucket_fill,
 * fluid_level_change, plus block_state readbacks.
 */

const { Vec3 } = require("vec3");

const {
  StateEpisodeBase,
  approachStep,
  bindSteps,
  centerOf,
  equip,
  findBlocksNear,
  isFluidSource,
  localJitterMs,
  lookAtPoint,
  note,
  outroStep,
  pick,
  placeFullBlock,
  randInt,
  sleep,
  startWatching,
  useHeldItemTowards,
  useItemOnBlock,
  walkNear,
  watchFor,
} = require("./state-common");

const FLAMMABLE_BLOCKS = ["oak_planks", "oak_log", "hay_block", "white_wool", "spruce_planks"];
const LAYOUTS = {
  line2: [[0, 0], [1, 0]],
  line3: [[-1, 0], [0, 0], [1, 0]],
  pile3: [[0, 0], [1, 0], [0, 1]],
  pile4: [[-1, 0], [0, 0], [1, 0], [0, 1]],
};

class IgniteAndExtinguishEpisode extends StateEpisodeBase {
  static INIT_MIN_BOTS_DISTANCE = 5;
  static INIT_MAX_BOTS_DISTANCE = 9;
  static WORKS_IN_NON_FLAT_WORLD = false;

  planEpisode(bot, rng, args, frame, isActor) {
    const material = pick(rng, FLAMMABLE_BLOCKS);
    const layoutName = pick(rng, Object.keys(LAYOUTS));
    const burnSeconds = randInt(rng, 3, 7);
    const observerExtinguishes = rng() < 0.5;
    const pickupWater = rng() < 0.5;
    const observerOpposite = rng() < 0.5;
    const observerDist = randInt(rng, 6, 8);

    const blocks = LAYOUTS[layoutName].map(([dp, dy]) => frame.at(0, dp, dy));
    const actorStand = frame.at(2, 0);
    const observerStand = observerOpposite
      ? frame.at(-observerDist, 0)
      : frame.at(0, observerDist);
    // where the observer stands when it comes to put the fire out
    const extinguishStand = observerOpposite ? frame.at(-3, 0) : frame.at(0, 3);

    const actorItems = [
      [material, blocks.length + 2],
      ["flint_and_steel", 1],
    ];
    if (!observerExtinguishes) actorItems.push(["water_bucket", 1]);
    const observerItems = observerExtinguishes ? [["water_bucket", 1]] : [];

    return {
      actorStand,
      observerStand,
      items: { actor: actorItems, observer: observerItems },
      blocks,
      material,
      burnSeconds,
      observerExtinguishes,
      pickupWater,
      extinguishStand,
      metadata: {
        material,
        layout: layoutName,
        blocks: blocks.map((b) => [b.x, b.y, b.z]),
        burn_seconds: burnSeconds,
        extinguisher: observerExtinguishes ? "observer" : "actor",
        pickup_water: pickupWater,
        observer_view: observerOpposite ? "opposite" : "side",
        observer_distance: observerDist,
      },
    };
  }

  buildSteps(ctx) {
    const { bot, rcon, args, episodeNum, plan, isActor } = ctx;
    const amExtinguisher = plan.observerExtinguishes ? !isActor : isActor;
    const fireTarget = () => {
      const fires = findBlocksNear(bot, ctx.frame.site.offset(0, 1, 0), ["fire"], 3, -1, 3);
      if (fires.length > 0) return centerOf(fires[0].pos);
      return ctx.siteCenter;
    };

    return bindSteps(ctx, [
      approachStep(),
      {
        name: "build",
        run: async function () {
          if (!isActor) {
            await watchFor(bot, () => ctx.siteCenter, 600);
            return {};
          }
          const placed = [];
          if (await equip(bot, plan.material)) {
            for (const pos of plan.blocks) {
              if (await placeFullBlock(bot, pos, plan.material)) placed.push(pos);
              await sleep(localJitterMs(250, 550));
            }
          }
          await note(rcon, args, bot, episodeNum, "build", { material: plan.material, placed });
          return { placed: placed.map((p) => [p.x, p.y, p.z]) };
        },
      },
      {
        name: "ignite",
        run: async function (otherData, ourPrev) {
          if (!isActor) {
            await watchFor(bot, () => ctx.siteCenter, 500);
            return {};
          }
          const placed = (ourPrev.placed || []).map((a) => new Vec3(a[0], a[1], a[2]));
          // light the highest placed block (or the site ground if nothing was placed)
          placed.sort((a, b) => b.y - a.y);
          const candidates = placed.length ? placed : [ctx.frame.site];
          let lit = null;
          if (await equip(bot, "flint_and_steel")) {
            for (const pos of candidates.slice(0, 2)) {
              await useItemOnBlock(bot, bot.blockAt(pos), new Vec3(0, 1, 0));
              await sleep(500);
              const above = bot.blockAt(pos.offset(0, 1, 0));
              if (above && above.name === "fire") {
                lit = pos.offset(0, 1, 0);
                break;
              }
            }
          }
          await note(rcon, args, bot, episodeNum, "ignite", { fire: lit });
          return { fire: lit ? [lit.x, lit.y, lit.z] : null };
        },
      },
      {
        name: "burn",
        run: async function () {
          const burnMs = plan.burnSeconds * 1000;
          const t0 = Date.now();
          if (amExtinguisher && !isActor) {
            // observer walks up to the fire while it burns
            const stop = startWatching(bot, fireTarget, { minMs: 600, maxMs: 1200 });
            await sleep(Math.min(1500, burnMs / 3));
            await stop();
            const s = plan.extinguishStand;
            await walkNear(bot, new Vec3(s.x + 0.5, s.y, s.z + 0.5), 0.8, 6000);
          }
          const left = burnMs - (Date.now() - t0);
          if (left > 0) await watchFor(bot, fireTarget, left);
          return {};
        },
      },
      {
        name: "extinguish",
        run: async function () {
          if (!amExtinguisher) {
            await watchFor(bot, fireTarget, localJitterMs(2500, 3500));
            return {};
          }
          // aim at the bottom of a burning block (or the site ground if burnt out)
          const fires = findBlocksNear(bot, ctx.frame.site.offset(0, 1, 0), ["fire"], 3, -1, 3);
          let aim;
          if (fires.length) {
            const f = fires[0].pos;
            aim = new Vec3(f.x + 0.5, f.y + 0.05, f.z + 0.5);
          } else {
            const s = ctx.frame.site;
            aim = new Vec3(s.x + 0.5, s.y + 1.0, s.z + 0.5);
          }
          let poured = false;
          if (await equip(bot, "water_bucket")) {
            poured = await useHeldItemTowards(bot, aim);
          }
          await note(rcon, args, bot, episodeNum, "extinguish", { aim, poured });
          await watchFor(bot, () => aim, localJitterMs(2500, 3500));

          if (plan.pickupWater && poured) {
            const sources = findBlocksNear(bot, ctx.frame.site.offset(0, 1, 0), ["water"], 3, -1, 3)
              .filter((w) => isFluidSource(w.block));
            if (sources.length && (await equip(bot, "bucket"))) {
              const w = sources[0].pos;
              const target = new Vec3(w.x + 0.5, w.y + 0.45, w.z + 0.5);
              await useHeldItemTowards(bot, target);
              await note(rcon, args, bot, episodeNum, "pickup_water", { source: w });
              await watchFor(bot, () => target, localJitterMs(2000, 3000));
            }
          }
          return {};
        },
      },
      {
        name: "aftermath",
        run: async function () {
          await lookAtPoint(bot, ctx.siteCenter, 80);
          await watchFor(bot, () => ctx.siteCenter, localJitterMs(1200, 2000));
          return {};
        },
      },
      outroStep(),
    ]);
  }
}

module.exports = { IgniteAndExtinguishEpisode };
