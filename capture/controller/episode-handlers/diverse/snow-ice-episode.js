/**
 * snowIce - snow blocks and layers, a snow golem built from blocks, powder
 * snow, ice placement, sliding on ice and a snowball exchange.
 *
 * Works in any biome (flat or normal terrain); in cold biomes the golem
 * leaves a snow trail (block_form). The episode cannot choose the biome
 * (teleport is done by episodes-loop); state_metadata.biome records it.
 *
 * Layout (u = towards the actor, p = perpendicular, ground at dy=-1):
 *   golem pen    fence ring u=0..4, p=-5..-1 (explicit fence states), floor
 *                u=1..3, p=-4..-2 of dirt / coarse dirt / moss / packed mud /
 *                rooted dirt (no `snowy` property), set before the snapshot;
 *                the golem stays inside and its snow trail never changes a
 *                block silently
 *   golem        (u=+2, p=-3) pen centre: 2 snow blocks + carved pumpkin / jack o'lantern
 *   snow layers  (u=-2, p=-2), snow block (u=-2, p=-3), powder snow (u=-2, p=-1)
 *   ice blocks   (u=+1, p=+2), (u=+2, p=+2) placed by the actor
 *   ice rink     ground cells u=-3..+3 at p=+4 (ice / packed ice / blue ice,
 *                set before the start snapshot)
 *   actor home (3,0), partner home (-3,0)
 *
 * Flow:
 *   intro
 *   build      actor stacks two snow blocks and puts the pumpkin on top ->
 *              a snow golem spawns (the pattern blocks vanish); meanwhile the
 *              partner places a snow layer and stacks it to 2-4 layers, plus
 *              a snow block
 *   golem      both watch the golem walk around
 *   powder_ice partner places powder snow with a bucket (and maybe scoops it
 *              back); actor places 1-2 ice blocks (ice / packed / blue)
 *   slide      the slider (shared RNG) sprints along the ice rink and slides
 *              out, then walks back
 *   snowballs  partner shovels its snow block (drops snowballs), then both
 *              throw 2-4 snowballs at each other
 *   outro
 *
 * Logged: block_place, player_interact + block_state (snow layers, golem
 * pattern removal: the pumpkin is placed by clicking the top snow block, so
 * the logger re-reads both snow blocks and the head position),
 * entity_spawn (snow_golem, snowball, item), block_form (golem trail),
 * bucket_fill (powder snow), block_break.
 */

const { Vec3 } = require("vec3");

const {
  FACES,
  MechanicsEpisodeBase,
  blockName,
  clickBlock,
  digAt,
  glance,
  inspectBlocks,
  introStep,
  localJitterMs,
  lookAtPoint,
  observe,
  observeUntilSignal,
  otherChest,
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

const RINKS = ["ice", "packed_ice", "blue_ice"];
const PEN_FENCES = ["oak_fence", "spruce_fence", "birch_fence", "dark_oak_fence", "cherry_fence", "mangrove_fence", "bamboo_fence", "crimson_fence", "nether_brick_fence"];
// pen floor: blocks WITHOUT a `snowy` property (grass / podzol / mycelium turn
// snowy=true under the golem's snow trail and that change has no event)
const PEN_FLOORS = ["dirt", "coarse_dirt", "moss_block", "packed_mud", "rooted_dirt"];

function dirKey(dx, dz) {
  if (dx > 0) return "east";
  if (dx < 0) return "west";
  if (dz > 0) return "south";
  return "north";
}

/**
 * /setblock commands for a fence ring with explicit connection states (a
 * plain /setblock would leave one-sided connections that a later neighbour
 * update could fix silently, i.e. without a logged event).
 */
function penCommands(cells, fence) {
  const key = (c) => `${c.x},${c.z}`;
  const set = new Set(cells.map(key));
  return cells.map((c) => {
    const props = { north: false, south: false, west: false, east: false };
    for (const [dx, dz] of [[1, 0], [-1, 0], [0, 1], [0, -1]]) {
      if (set.has(`${c.x + dx},${c.z + dz}`)) props[dirKey(dx, dz)] = true;
    }
    const st = Object.entries(props).map(([k, v]) => `${k}=${v}`).join(",");
    return `setblock ${c.x} ${c.y} ${c.z} minecraft:${fence}[${st},waterlogged=false]`;
  });
}
const ICE_ITEMS = ["ice", "packed_ice", "blue_ice"];

function findGolem(bot, near, radius = 16) {
  let best = null;
  let bestD = Infinity;
  for (const e of Object.values(bot.entities)) {
    if (!e || !e.position) continue;
    const n = e.name || e.displayName || "";
    if (n !== "snow_golem" && n !== "Snow Golem") continue;
    const d = e.position.distanceTo(near);
    if (d < radius && d < bestD) {
      best = e;
      bestD = d;
    }
  }
  return best;
}

class SnowIceEpisode extends MechanicsEpisodeBase {
  static INIT_MIN_BOTS_DISTANCE = 5;
  static INIT_MAX_BOTS_DISTANCE = 9;
  static WORKS_IN_NON_FLAT_WORLD = true;
  static SITE_HALF_SIZE = 5;

  planEpisode(bot, rng, args, frame, isActor) {
    const head = rng() < 0.75 ? "carved_pumpkin" : "jack_o_lantern";
    const layers = randInt(rng, 2, 4);
    const rink = pick(rng, RINKS);
    const iceA = pick(rng, ICE_ITEMS);
    const iceB = pick(rng, ICE_ITEMS);
    const nIce = randInt(rng, 1, 2);
    const scoopPowder = rng() < 0.6;
    const actorSlides = rng() < 0.5;
    const throwsA = randInt(rng, 2, 4);
    const throwsB = randInt(rng, 2, 4);

    const fence = pick(rng, PEN_FENCES);
    const floor = pick(rng, PEN_FLOORS);

    // golem pen: interior u=1..3, p=-4..-2 (centre G = (2,-3)), fence ring around it
    const golem = [frame.at(2, -3, 0), frame.at(2, -3, 1), frame.at(2, -3, 2)];
    const penRing = [];
    const penFloor = [];
    for (let du = 0; du <= 4; du++) {
      for (let dp = -5; dp <= -1; dp++) {
        if (du === 0 || du === 4 || dp === -5 || dp === -1) penRing.push(frame.at(du, dp, 0));
        else penFloor.push(frame.at(du, dp, -1));
      }
    }
    const fa = frame.at(1, -4, -1);
    const fb = frame.at(3, -2, -1);
    const rinkA = frame.at(-3, 4, -1);
    const rinkB = frame.at(3, 4, -1);
    return {
      actorStand: frame.at(3, 0),
      observerStand: frame.at(-3, 0),
      items: {
        actor: [
          ["snow_block", 2],
          [head, 1],
          [iceA, 1],
          [iceB, 1],
          ["snowball", throwsA + 2],
        ],
        observer: [
          ["snow", layers + 1],
          ["snow_block", 1],
          ["powder_snow_bucket", 1],
          ["snowball", throwsB + 2],
        ],
      },
      leaderSetupCommands: [
        `fill ${Math.min(rinkA.x, rinkB.x)} ${rinkA.y} ${Math.min(rinkA.z, rinkB.z)} ${Math.max(rinkA.x, rinkB.x)} ${rinkA.y} ${Math.max(rinkA.z, rinkB.z)} minecraft:${rink}`,
        `fill ${Math.min(fa.x, fb.x)} ${fa.y} ${Math.min(fa.z, fb.z)} ${Math.max(fa.x, fb.x)} ${fa.y} ${Math.max(fa.z, fb.z)} minecraft:${floor}`,
        ...penCommands(penRing, fence),
      ],
      golem,
      head,
      layers,
      rink,
      iceCells: [frame.at(1, 2, 0), frame.at(2, 2, 0)].slice(0, nIce),
      iceItems: [iceA, iceB],
      layerPos: frame.at(-2, -2, 0),
      snowBlockPos: frame.at(-2, -3, 0),
      powderPos: frame.at(-2, -1, 0),
      scoopPowder,
      actorSlides,
      throwsA,
      throwsB,
      metadata: {
        golem_pattern: golem.map(toArr),
        golem_head: head,
        golem_pen: { fence, floor, ring: [toArr(frame.at(0, -5, 0)), toArr(frame.at(4, -1, 0))] },
        snow_layers: layers,
        snow_layer_pos: toArr(frame.at(-2, -2, 0)),
        snow_block_pos: toArr(frame.at(-2, -3, 0)),
        powder_snow_pos: toArr(frame.at(-2, -1, 0)),
        powder_snow_scooped: scoopPowder,
        ice_blocks: [iceA, iceB].slice(0, nIce),
        rink_material: rink,
        rink: [toArr(rinkA), toArr(rinkB)],
        slider: actorSlides ? "actor" : "observer",
        snowballs: { actor: throwsA, observer: throwsB },
      },
    };
  }

  buildSteps(ctx) {
    const { bot, plan, isActor, frame } = ctx;
    const golemFocus = () => {
      const g = findGolem(bot, plan.golem[0]);
      return g ? g.position.offset(0, 1.0, 0) : plan.golem[1].offset(0.5, 0.5, 0.5);
    };
    const amSlider = plan.actorSlides === isActor;
    const home = isActor ? plan.actorStand : plan.observerStand;

    return [
      introStep(),
      {
        name: "build",
        timeoutMs: 30000,
        run: async function () {
          if (isActor) {
            const [g0, g1, g2] = plan.golem;
            const res = {};
            res.snow0 = await placeItemAt(bot, g0, "snow_block", { face: FACES.up, refPos: g0.offset(0, -1, 0), expect: ["snow_block"] });
            await sleep(localJitterMs(250, 450));
            res.snow1 = res.snow0 && (await placeItemAt(bot, g1, "snow_block", { face: FACES.up, refPos: g0, expect: ["snow_block"] }));
            await glance(this, localJitterMs(500, 800));
            if (res.snow1) {
              // click the TOP face of the upper snow block: the logger re-reads
              // it, the head position and the lower snow block (DOWN)
              await clickBlock(bot, g1, { item: plan.head, face: FACES.up });
              await sleep(900);
            }
            res.after = [blockName(bot, g0), blockName(bot, g1), blockName(bot, g2)];
            res.golem = !!findGolem(bot, g0, 6);
            await this.note("snow_golem", res);
            await observe(this, golemFocus, localJitterMs(800, 1200), { strafe: false });
            return {};
          }
          const res = {};
          const L = plan.layerPos;
          res.first = await placeItemAt(bot, L, "snow", { face: FACES.up, refPos: L.offset(0, -1, 0), expect: ["snow"] });
          for (let k = 1; k < plan.layers && res.first; k++) {
            if (this.timeUp()) break;
            await sleep(localJitterMs(300, 500));
            await clickBlock(bot, L, { item: "snow", face: FACES.up });
          }
          await sleep(300);
          res.layers = Number(propsOf(bot.blockAt(L)).layers) || 0;
          await glance(this, localJitterMs(500, 800));
          const B = plan.snowBlockPos;
          res.block = await placeItemAt(bot, B, "snow_block", { face: FACES.up, refPos: B.offset(0, -1, 0), expect: ["snow_block"] });
          await this.note("snow_layers", res);
          await observe(this, golemFocus, localJitterMs(800, 1200), { strafe: false });
          return {};
        },
      },
      {
        name: "golem",
        timeoutMs: 10000,
        run: async function () {
          await observe(this, golemFocus, localJitterMs(3000, 4200), { strafe: false });
          return {};
        },
      },
      {
        name: "powder_ice",
        timeoutMs: 26000,
        run: async function () {
          if (isActor) {
            const placed = [];
            for (let i = 0; i < plan.iceCells.length; i++) {
              const cell = plan.iceCells[i];
              const item = plan.iceItems[i];
              const ok = await placeItemAt(bot, cell, item, { face: FACES.up, refPos: cell.offset(0, -1, 0), expect: [item] });
              placed.push({ item, ok });
              await sleep(localJitterMs(300, 500));
            }
            await this.note("ice_blocks", { placed });
            await observe(this, () => plan.powderPos.offset(0.5, 0.5, 0.5), localJitterMs(3500, 4500));
            return {};
          }
          const P = plan.powderPos;
          const res = {};
          await clickBlock(bot, P.offset(0, -1, 0), { item: "powder_snow_bucket", face: FACES.up });
          await sleep(700);
          res.placed = blockName(bot, P);
          await observe(this, () => P.offset(0.5, 0.5, 0.5), localJitterMs(1500, 2200), { strafe: false });
          if (plan.scoopPowder && res.placed === "powder_snow") {
            await useItemTowards(bot, "bucket", P.offset(0.5, 0.5, 0.5));
            await sleep(700);
            res.scooped = blockName(bot, P) !== "powder_snow";
          }
          await this.note("powder_snow", res);
          await glance(this, localJitterMs(600, 1000));
          return {};
        },
      },
      {
        name: "slide",
        timeoutMs: 26000,
        run: async function () {
          if (!amSlider) {
            await observeUntilSignal(this, () => frame.at(0, 4, 0).offset(0.5, 0.5, 0.5), "slide", 23000, { strafe: false });
            return {};
          }
          const res = {};
          try {
            // start at the partner-side end of the rink, run towards +u
            const start = frame.at(-3, 4, 0);
            const end = frame.at(3, 4, 0);
            res.reached_start = await walkTo(bot, start, 0.6, 8000);
            await lookAtPoint(bot, end.offset(0.5, 1.4, 0.5), 140);
            await glance(this, localJitterMs(300, 500));
            await lookAtPoint(bot, end.offset(0.5, 1.4, 0.5), 160);
            const p0 = bot.entity.position.clone();
            bot.setControlState("forward", true);
            bot.setControlState("sprint", true);
            await sleep(localJitterMs(1100, 1500));
            bot.setControlState("sprint", false);
            bot.setControlState("forward", false);
            await sleep(1200); // sliding out
            res.distance = Number(bot.entity.position.distanceTo(p0).toFixed(2));
            res.on = blockName(bot, bot.entity.position.floored().offset(0, -1, 0));
            await this.note("slide", res);
            await glance(this, localJitterMs(500, 800));
            await walkTo(bot, home, 0.8, 7000);
          } finally {
            for (const c of ["forward", "sprint"]) bot.setControlState(c, false);
            signalDone(this, "slide");
          }
          return {};
        },
      },
      {
        name: "snowballs",
        timeoutMs: 26000,
        run: async function () {
          const res = {};
          if (!isActor) {
            const B = plan.snowBlockPos;
            if (blockName(bot, B) === "snow_block") {
              res.shoveled = await digAt(bot, B, 5000);
              await sleep(400);
              // grass under it turns snowy=false (no event): logger readback
              await inspectBlocks(this, [B.offset(0, -1, 0)]);
            }
          } else {
            await sleep(localJitterMs(800, 1300));
          }
          const n = isActor ? plan.throwsA : plan.throwsB;
          let thrown = 0;
          for (let k = 0; k < n; k++) {
            if (this.timeUp()) break;
            const o = otherChest(this);
            if (!o) break;
            const d = bot.entity.position.distanceTo(o);
            // small lead upwards for the arc (snowballs drop ~0.03/tick^2)
            const aim = o.offset((Math.random() - 0.5) * 0.4, 0.15 + d * 0.06, (Math.random() - 0.5) * 0.4);
            if (await useItemTowards(bot, "snowball", aim)) thrown++;
            await sleep(localJitterMs(700, 1300));
          }
          res.thrown = thrown;
          await this.note("snowballs", res);
          await glance(this, localJitterMs(800, 1200));
          return {};
        },
      },
      outroStep(),
    ];
  }
}

module.exports = { SnowIceEpisode };
