/**
 * tntBlast - explosion as a large, sudden world-state change.
 *
 * The actor places 1-3 TNT blocks at the site, primes one with flint and
 * steel (chain reaction ignites the others) and sprints 8-10 blocks away
 * during the 4 s fuse. The observer watches from 10-13 blocks. Optionally
 * the actor walks back and looks into the crater.
 *
 * Bots carry Resistance 255 (set once per bot by the episode loop), so the
 * blast cannot kill them; cameras are protected by the server plugin.
 *
 * Logged state changes: block_place, tnt_prime, entity_spawn (minecraft:tnt,
 * minecraft:item drops), entity_explode (with every destroyed block and its
 * old state), entity_remove, block_state readbacks for every crater voxel.
 */

const { Vec3 } = require("vec3");

const {
  StateEpisodeBase,
  approachStep,
  bindSteps,
  equip,
  localJitterMs,
  lookAtPoint,
  note,
  outroStep,
  pick,
  placeFullBlock,
  randInt,
  sleep,
  useItemOnBlock,
  walkNear,
  watchFor,
} = require("./state-common");

const LAYOUTS = {
  single: [[0, 0]],
  pair: [[0, 0], [1, 0]],
  row3: [[-1, 0], [0, 0], [1, 0]],
  stack2: [[0, 0], [0, 1]],
};

class TntBlastEpisode extends StateEpisodeBase {
  static INIT_MIN_BOTS_DISTANCE = 8;
  static INIT_MAX_BOTS_DISTANCE = 14;
  static WORKS_IN_NON_FLAT_WORLD = false;

  planEpisode(bot, rng, args, frame, isActor) {
    const layoutName = pick(rng, ["single", "single", "pair", "row3", "stack2"]);
    const retreatDist = randInt(rng, 8, 10);
    const observerOpposite = rng() < 0.5;
    const observerDist = randInt(rng, 10, 13);
    const inspect = rng() < 0.5;
    const tnt = LAYOUTS[layoutName].map(([dp, dy]) => frame.at(0, dp, dy));
    return {
      actorStand: frame.at(2, 0),
      observerStand: observerOpposite ? frame.at(-observerDist, 0) : frame.at(0, -observerDist),
      retreat: frame.at(2 + retreatDist, 0),
      inspectStand: frame.at(4, 0),
      items: {
        actor: [
          ["tnt", tnt.length + 1],
          ["flint_and_steel", 1],
        ],
        observer: [],
      },
      tnt,
      inspect,
      metadata: {
        layout: layoutName,
        tnt: tnt.map((p) => [p.x, p.y, p.z]),
        retreat_distance: retreatDist,
        inspect_crater: inspect,
        observer_view: observerOpposite ? "opposite" : "side",
        observer_distance: observerDist,
      },
    };
  }

  buildSteps(ctx) {
    const { bot, rcon, args, episodeNum, plan, isActor } = ctx;
    const blastCenter = ctx.siteCenter;

    return bindSteps(ctx, [
      approachStep(),
      {
        name: "place_tnt",
        run: async function () {
          if (!isActor) {
            await watchFor(bot, () => blastCenter, 600);
            return {};
          }
          const placed = [];
          for (const pos of plan.tnt) {
            if (await placeFullBlock(bot, pos, "tnt")) placed.push(pos);
            await sleep(localJitterMs(250, 500));
          }
          await note(rcon, args, bot, episodeNum, "place_tnt", { placed });
          return {};
        },
      },
      {
        name: "ignite_and_run",
        run: async function () {
          if (!isActor) {
            await watchFor(bot, () => blastCenter, 500);
            return {};
          }
          let primed = false;
          if (await equip(bot, "flint_and_steel")) {
            for (const pos of plan.tnt.slice(0, 2)) {
              const b = bot.blockAt(pos);
              if (!b || b.name !== "tnt") continue;
              await useItemOnBlock(bot, b, new Vec3(0, 1, 0));
              await sleep(300);
              const after = bot.blockAt(pos);
              if (!after || after.name !== "tnt") {
                primed = true;
                break;
              }
            }
          }
          await note(rcon, args, bot, episodeNum, "ignite_tnt", { primed });
          const r = plan.retreat;
          await walkNear(bot, new Vec3(r.x + 0.5, r.y, r.z + 0.5), 1.0, 3800);
          await lookAtPoint(bot, blastCenter, 140);
          return { primed };
        },
      },
      {
        name: "blast",
        run: async function () {
          // the fuse is 80 ticks; part of it elapsed while the actor ran away
          await watchFor(bot, () => blastCenter, localJitterMs(4500, 5500));
          return {};
        },
      },
      {
        name: "inspect",
        run: async function () {
          if (isActor && plan.inspect) {
            const s = plan.inspectStand;
            await walkNear(bot, new Vec3(s.x + 0.5, s.y, s.z + 0.5), 1.0, 6000);
            const crater = ctx.frame.site;
            await watchFor(bot, () => new Vec3(crater.x + 0.5, crater.y - 0.5, crater.z + 0.5), localJitterMs(2000, 3000));
            await note(rcon, args, bot, episodeNum, "inspect_crater", {});
          } else {
            await watchFor(bot, () => blastCenter, localJitterMs(1500, 2500));
          }
          return {};
        },
      },
      outroStep(),
    ]);
  }
}

module.exports = { TntBlastEpisode };
