/**
 * buildAndBreak - persistent, multi-material structure edits.
 *
 * The builder places a small structure of mixed blocks (wall, pillar, cube,
 * arch, L, steps), both bots look at it, then the breaker (the other bot with
 * probability 0.6, otherwise the builder) mines 30-80% of it, top-down.
 * Gravity blocks (sand / gravel) are only used where they are supported, so
 * breaking the block below them makes them fall (entity_spawn of a
 * minecraft:falling_block + entity_change_block when it lands).
 *
 * Works in normal worlds as well (falls back to the ground at the midpoint
 * when no flat patch is found).
 *
 * Logged state changes: block_place, block_break, entity_spawn (item drops,
 * falling blocks), entity_change_block, entity_remove (pickups),
 * block_state readbacks.
 */

const { Vec3 } = require("vec3");

const {
  StateEpisodeBase,
  approachStep,
  bindSteps,
  centerOf,
  digAt,
  localJitterMs,
  note,
  outroStep,
  pick,
  placeFullBlock,
  randInt,
  shuffle,
  sleep,
  walkNear,
  watchFor,
} = require("./state-common");

// [dp, dy, dq] with q pointing away from the builder (q = -u)
const TEMPLATES = {
  wall3x2: [[-1, 0, 0], [0, 0, 0], [1, 0, 0], [-1, 1, 0], [0, 1, 0], [1, 1, 0]],
  pillar3: [[0, 0, 0], [0, 1, 0], [0, 2, 0]],
  cube2: [[0, 0, 0], [1, 0, 0], [0, 0, 1], [1, 0, 1], [0, 1, 0], [1, 1, 0], [0, 1, 1], [1, 1, 1]],
  arch5: [[-1, 0, 0], [1, 0, 0], [-1, 1, 0], [1, 1, 0], [0, 1, 0]],
  l4: [[-1, 0, 0], [0, 0, 0], [1, 0, 0], [1, 1, 0]],
  steps6: [[-1, 0, 0], [0, 0, 0], [1, 0, 0], [0, 1, 0], [1, 1, 0], [1, 2, 0]],
};
const SOLID_MATERIALS = [
  "stone",
  "cobblestone",
  "oak_planks",
  "bricks",
  "glass",
  "white_wool",
  "sandstone",
  "oak_log",
  "stone_bricks",
  "glowstone",
];
const GRAVITY_MATERIALS = ["sand", "gravel"];

class BuildAndBreakEpisode extends StateEpisodeBase {
  static INIT_MIN_BOTS_DISTANCE = 4;
  static INIT_MAX_BOTS_DISTANCE = 8;
  static WORKS_IN_NON_FLAT_WORLD = true;
  static SITE_HALF_SIZE = 2;
  static ALLOW_UNFLAT_FALLBACK = true;

  planEpisode(bot, rng, args, frame, isActor) {
    const templateName = pick(rng, Object.keys(TEMPLATES));
    const tpl = TEMPLATES[templateName];
    const keys = new Set(tpl.map(([p, y, q]) => `${p},${y},${q}`));
    const blocks = tpl.map(([dp, dy, dq]) => {
      const supported = dy === 0 || keys.has(`${dp},${dy - 1},${dq}`);
      const useGravity = supported && rng() < 0.15;
      const material = useGravity ? pick(rng, GRAVITY_MATERIALS) : pick(rng, SOLID_MATERIALS);
      return { pos: frame.at(-dq, dp, dy), material, local: [dp, dy, dq] };
    });
    const frac = 0.3 + rng() * 0.5;
    const nBreak = Math.max(1, Math.round(frac * blocks.length));
    // top-down; random order within a layer
    const order = shuffle(rng, blocks.map((_, i) => i)).sort(
      (a, b) => blocks[b].pos.y - blocks[a].pos.y,
    );
    const toBreak = order.slice(0, nBreak);
    const observerBreaks = rng() < 0.6;
    const observerOpposite = rng() < 0.5;
    const observerDist = randInt(rng, 5, 7);

    const counts = {};
    for (const b of blocks) counts[b.material] = (counts[b.material] || 0) + 1;
    return {
      actorStand: frame.at(2, 0),
      observerStand: observerOpposite ? frame.at(-observerDist - 1, 0) : frame.at(0, observerDist),
      // where the observer goes to break the structure (on its own side)
      breakStand: observerOpposite ? frame.at(-3, 0) : frame.at(0, 3),
      items: {
        actor: Object.entries(counts).map(([m, c]) => [m, c + 1]),
        observer: [],
      },
      blocks,
      toBreak,
      observerBreaks,
      metadata: {
        template: templateName,
        blocks: blocks.map((b) => ({ pos: [b.pos.x, b.pos.y, b.pos.z], material: b.material })),
        break_indices: toBreak,
        breaker: observerBreaks ? "observer" : "actor",
        observer_view: observerOpposite ? "opposite" : "side",
        observer_distance: observerDist,
      },
    };
  }

  buildSteps(ctx) {
    const { bot, rcon, args, episodeNum, plan, isActor } = ctx;
    const amBreaker = plan.observerBreaks ? !isActor : isActor;
    const structureCenter = () => {
      let x = 0, y = 0, z = 0;
      for (const b of plan.blocks) { x += b.pos.x; y += b.pos.y; z += b.pos.z; }
      const n = plan.blocks.length;
      return new Vec3(x / n + 0.5, y / n + 0.5, z / n + 0.5);
    };

    return bindSteps(ctx, [
      approachStep(),
      {
        name: "build",
        run: async function () {
          if (!isActor) {
            await watchFor(bot, structureCenter, 600);
            return {};
          }
          // bottom-up, then far-to-near
          const order = plan.blocks
            .map((b, i) => ({ ...b, i }))
            .sort((a, b) => a.pos.y - b.pos.y || b.local[2] - a.local[2]);
          const placed = [];
          for (const b of order) {
            if (await placeFullBlock(bot, b.pos, b.material)) placed.push(b.i);
            await sleep(localJitterMs(200, 500));
          }
          await note(rcon, args, bot, episodeNum, "build", { placed_indices: placed });
          return {};
        },
      },
      {
        name: "admire",
        run: async function () {
          await watchFor(bot, structureCenter, localJitterMs(1200, 2000));
          return {};
        },
      },
      {
        name: "break",
        run: async function () {
          if (!amBreaker) {
            await watchFor(bot, structureCenter, 800);
            return {};
          }
          if (!isActor) {
            const s = plan.breakStand;
            await walkNear(bot, new Vec3(s.x + 0.5, s.y, s.z + 0.5), 1.0, 6000);
          }
          const broken = [];
          for (const i of plan.toBreak) {
            const pos = plan.blocks[i].pos;
            const b = bot.blockAt(pos);
            if (!b || b.name === "air") continue;
            if (await digAt(bot, pos)) broken.push(i);
            await sleep(localJitterMs(250, 600));
          }
          await note(rcon, args, bot, episodeNum, "break", { broken_indices: broken });
          return {};
        },
      },
      {
        name: "admire_remains",
        run: async function () {
          await watchFor(bot, structureCenter, localJitterMs(1500, 2500));
          return {};
        },
      },
      outroStep(),
    ]);
  }
}

module.exports = { BuildAndBreakEpisode };
