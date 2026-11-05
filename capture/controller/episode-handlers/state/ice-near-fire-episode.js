/**
 * iceNearFire - light-driven melting.
 *
 * The actor places a short row of ice blocks and then a light source at the
 * end of the row: a torch, a burning netherrack block (eternal fire) or a
 * lava source. Ice melts when the block light reaching it is above 11, which
 * happens on random ticks; the leader raises randomTickSpeed to
 * STATE_RANDOM_TICK_SPEED (default 100, vanilla 3) during setup so that
 * melting happens within the episode, and resets it to 3 in teardown. The
 * value in force is recorded in the episode_begin record (world.gamerules).
 *
 * Melting turns ice into water sources, which then flow; with the lava
 * variant, water meeting lava forms obsidian / cobblestone.
 *
 * Logged state changes: block_place, block_ignite (netherrack variant),
 * bucket_empty (lava variant), block_fade (ice -> water), liquid_flow,
 * block_form (obsidian / cobblestone), fluid_level_change.
 */

const { Vec3 } = require("vec3");

const {
  StateEpisodeBase,
  approachStep,
  bindSteps,
  equip,
  localJitterMs,
  note,
  outroStep,
  pick,
  placeFullBlock,
  placeOnTop,
  randInt,
  sleep,
  useHeldItemTowards,
  useItemOnBlock,
  walkNear,
  watchFor,
} = require("./state-common");
const { isWorldStateLeader } = require("../../utils/world-state");

const VARIANTS = ["torch", "netherrack_fire", "lava"];

class IceNearFireEpisode extends StateEpisodeBase {
  static INIT_MIN_BOTS_DISTANCE = 4;
  static INIT_MAX_BOTS_DISTANCE = 8;
  static WORKS_IN_NON_FLAT_WORLD = false;

  planEpisode(bot, rng, args, frame, isActor) {
    const variant = pick(rng, VARIANTS);
    // torch light (14) only reaches 3 ice blocks with light > 11
    const nIce = variant === "torch" ? randInt(rng, 2, 3) : randInt(rng, 2, 4);
    const meltSeconds = randInt(rng, 14, 22);
    const observerOpposite = rng() < 0.5;
    const observerDist = randInt(rng, 4, 6);

    // ice row along p at the site, light source right after the last ice block
    const ice = [];
    for (let k = 0; k < nIce; k++) ice.push(frame.at(0, k));
    const light = frame.at(0, nIce);
    // actor stands diagonally so both the row and the light spot are in plain view
    // (diagonal to the light spot: clear line of sight, every ice block in reach)
    const actorStand = frame.at(2, nIce - 1);
    const retreatStand = frame.at(4, nIce - 1);
    const observerStand = observerOpposite
      ? frame.at(-observerDist, Math.floor(nIce / 2))
      : frame.at(0, -observerDist);

    const actorItems = [["ice", nIce + 1]];
    if (variant === "torch") actorItems.push(["torch", 2]);
    if (variant === "netherrack_fire") actorItems.push(["netherrack", 2], ["flint_and_steel", 1]);
    if (variant === "lava") actorItems.push(["lava_bucket", 1]);

    const tickSpeed = Number(args.state_random_tick_speed) || 100;
    return {
      actorStand,
      observerStand,
      retreatStand,
      items: { actor: actorItems, observer: [] },
      ice,
      light,
      variant,
      meltSeconds,
      leaderSetupCommands: [`gamerule randomTickSpeed ${tickSpeed}`],
      metadata: {
        variant,
        ice: ice.map((p) => [p.x, p.y, p.z]),
        light: [light.x, light.y, light.z],
        melt_seconds: meltSeconds,
        random_tick_speed: tickSpeed,
        observer_view: observerOpposite ? "opposite" : "side",
        observer_distance: observerDist,
      },
    };
  }

  buildSteps(ctx) {
    const { bot, rcon, args, episodeNum, plan, isActor } = ctx;
    const rowCenter = () => {
      const a = plan.ice[0];
      const b = plan.light;
      return new Vec3((a.x + b.x) / 2 + 0.5, a.y + 0.5, (a.z + b.z) / 2 + 0.5);
    };

    return bindSteps(ctx, [
      approachStep(),
      {
        name: "place_ice",
        run: async function () {
          if (!isActor) {
            await watchFor(bot, rowCenter, 600);
            return {};
          }
          const placed = [];
          // far -> near (the actor stands next to the light end of the row)
          for (const pos of plan.ice) {
            if (await placeFullBlock(bot, pos, "ice")) placed.push(pos);
            await sleep(localJitterMs(250, 500));
          }
          await note(rcon, args, bot, episodeNum, "place_ice", { placed });
          return {};
        },
      },
      {
        name: "place_light",
        run: async function () {
          if (!isActor) {
            await watchFor(bot, rowCenter, 600);
            return {};
          }
          const L = plan.light;
          let ok = false;
          if (plan.variant === "torch") {
            ok = await placeOnTop(bot, L, "torch", "torch");
          } else if (plan.variant === "netherrack_fire") {
            if (await placeFullBlock(bot, L, "netherrack")) {
              if (await equip(bot, "flint_and_steel")) {
                await useItemOnBlock(bot, bot.blockAt(L), new Vec3(0, 1, 0));
                await sleep(500);
                const above = bot.blockAt(L.offset(0, 1, 0));
                ok = !!above && above.name === "fire";
              }
            }
          } else if (plan.variant === "lava") {
            if (await equip(bot, "lava_bucket")) {
              // top face of the ground block under the light spot
              await useHeldItemTowards(bot, new Vec3(L.x + 0.5, L.y + 0.0, L.z + 0.5));
              await sleep(500);
              const b = bot.blockAt(L);
              ok = !!b && b.name === "lava";
            }
          }
          await note(rcon, args, bot, episodeNum, "place_light", { variant: plan.variant, pos: L, ok });
          // step back so the actor does not stand in the light / lava spread
          const r = plan.retreatStand;
          await walkNear(bot, new Vec3(r.x + 0.5, r.y, r.z + 0.5), 0.8, 4000);
          return {};
        },
      },
      {
        name: "melt",
        run: async function () {
          await watchFor(bot, rowCenter, plan.meltSeconds * 1000);
          return {};
        },
      },
      outroStep(),
    ]);
  }

  async tearDownEpisode(bot, rcon, sharedBotRng, coordinator, episodeNum, args) {
    await super.tearDownEpisode(bot, rcon, sharedBotRng, coordinator, episodeNum, args);
    if (isWorldStateLeader(bot, args)) {
      const res = await rcon.send("gamerule randomTickSpeed 3");
      console.log(`[${bot.username}] reset randomTickSpeed: ${res}`);
    }
  }
}

module.exports = { IceNearFireEpisode };
