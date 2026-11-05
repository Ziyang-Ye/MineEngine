/**
 * lavaWater - fluid interaction that creates new blocks.
 *
 * The actor digs a 1x1 hole at the site and pours a lava source into it.
 * Then the pourer (actor or observer) empties a water bucket on the ground
 * next to the hole; the water flows over the hole and turns the lava source
 * into obsidian (flowing lava touched by water becomes cobblestone). With
 * probability 0.6 the pourer scoops the water source back up with the empty
 * bucket and both watch the flowing water recede.
 *
 * Logged state changes: block_break (dig), bucket_empty (lava, water),
 * liquid_flow, block_form (obsidian / cobblestone), bucket_fill,
 * fluid_level_change, block_state readbacks.
 */

const { Vec3 } = require("vec3");

const {
  StateEpisodeBase,
  approachStep,
  bindSteps,
  digAt,
  equip,
  isFluidSource,
  localJitterMs,
  note,
  outroStep,
  randInt,
  sleep,
  useHeldItemTowards,
  walkNear,
  watchFor,
} = require("./state-common");

class LavaWaterEpisode extends StateEpisodeBase {
  static INIT_MIN_BOTS_DISTANCE = 4;
  static INIT_MAX_BOTS_DISTANCE = 8;
  static WORKS_IN_NON_FLAT_WORLD = false;

  planEpisode(bot, rng, args, frame, isActor) {
    const observerPours = rng() < 0.5;
    const pickup = rng() < 0.6;
    const observerOpposite = rng() < 0.5;
    const observerDist = randInt(rng, 5, 7);
    const watchSeconds = randInt(rng, 3, 5);

    const hole = frame.site; // ground block that gets dug out
    // ground block whose top receives the water: opposite the actor, or
    // on the observer's side when the observer pours
    const waterGround = observerPours && !observerOpposite
      ? frame.at(0, 1, -1) // (u=0, p=+1) column, ground level
      : frame.at(-1, 0, -1); // (u=-1, p=0) column, ground level
    const pourerStandObserver = observerOpposite ? frame.at(-3, 0) : frame.at(0, 3);

    const actorItems = [["lava_bucket", 1]];
    if (!observerPours) actorItems.push(["water_bucket", 1]);
    const observerItems = observerPours ? [["water_bucket", 1]] : [];
    return {
      actorStand: frame.at(1, 0),
      actorBackStand: frame.at(3, 0),
      observerStand: observerOpposite ? frame.at(-observerDist, 0) : frame.at(0, observerDist),
      pourerStandObserver,
      items: { actor: actorItems, observer: observerItems },
      hole,
      waterGround,
      observerPours,
      pickup,
      watchSeconds,
      metadata: {
        hole: [hole.x, hole.y, hole.z],
        water_ground: [waterGround.x, waterGround.y, waterGround.z],
        pourer: observerPours ? "observer" : "actor",
        pickup_water: pickup,
        observer_view: observerOpposite ? "opposite" : "side",
        observer_distance: observerDist,
      },
    };
  }

  buildSteps(ctx) {
    const { bot, rcon, args, episodeNum, plan, isActor } = ctx;
    const amPourer = plan.observerPours ? !isActor : isActor;
    const holeView = () => new Vec3(plan.hole.x + 0.5, plan.hole.y + 0.5, plan.hole.z + 0.5);
    const W = plan.waterGround;
    const waterSource = W.offset(0, 1, 0);

    return bindSteps(ctx, [
      approachStep(),
      {
        name: "dig",
        run: async function () {
          if (!isActor) {
            await watchFor(bot, holeView, 600);
            return {};
          }
          const ok = await digAt(bot, plan.hole);
          await sleep(300);
          await note(rcon, args, bot, episodeNum, "dig_hole", { hole: plan.hole, ok });
          return {};
        },
      },
      {
        name: "pour_lava",
        run: async function () {
          if (!isActor) {
            if (amPourer) {
              // walk up while the lava goes in
              const s = plan.pourerStandObserver;
              await walkNear(bot, new Vec3(s.x + 0.5, s.y, s.z + 0.5), 0.8, 6000);
            }
            await watchFor(bot, holeView, 800);
            return {};
          }
          let ok = false;
          if (await equip(bot, "lava_bucket")) {
            // top face of the block under the hole
            const h = plan.hole;
            await useHeldItemTowards(bot, new Vec3(h.x + 0.5, h.y + 0.02, h.z + 0.5));
            await sleep(600);
            const b = bot.blockAt(h);
            ok = !!b && b.name === "lava";
          }
          await note(rcon, args, bot, episodeNum, "pour_lava", { pos: plan.hole, ok });
          if (!amPourer) {
            const s = plan.actorBackStand;
            await walkNear(bot, new Vec3(s.x + 0.5, s.y, s.z + 0.5), 0.8, 4000);
          }
          await watchFor(bot, holeView, localJitterMs(1000, 1600));
          return {};
        },
      },
      {
        name: "pour_water",
        run: async function () {
          if (!amPourer) {
            await watchFor(bot, holeView, plan.watchSeconds * 1000 + 1200);
            return {};
          }
          let ok = false;
          if (await equip(bot, "water_bucket")) {
            // top face of the ground block next to the hole
            ok = await useHeldItemTowards(bot, new Vec3(W.x + 0.5, W.y + 1.0, W.z + 0.5));
          }
          await sleep(800);
          const formed = bot.blockAt(plan.hole);
          await note(rcon, args, bot, episodeNum, "pour_water", {
            ground: W,
            ok,
            hole_block: formed ? formed.name : null,
          });
          await watchFor(bot, holeView, plan.watchSeconds * 1000);
          return {};
        },
      },
      {
        name: "pickup_water",
        run: async function () {
          if (!amPourer || !plan.pickup) {
            await watchFor(bot, holeView, localJitterMs(2500, 3500));
            return {};
          }
          const src = bot.blockAt(waterSource);
          if (src && src.name === "water" && isFluidSource(src) && (await equip(bot, "bucket"))) {
            await useHeldItemTowards(
              bot,
              new Vec3(waterSource.x + 0.5, waterSource.y + 0.45, waterSource.z + 0.5),
            );
            await note(rcon, args, bot, episodeNum, "pickup_water", { source: waterSource });
          }
          await watchFor(bot, holeView, localJitterMs(2500, 3500));
          return {};
        },
      },
      outroStep(),
    ]);
  }
}

module.exports = { LavaWaterEpisode };
