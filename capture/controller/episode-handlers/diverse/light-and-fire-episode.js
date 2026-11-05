/**
 * lightAndFire - light sources, candles, campfires and fire.
 *
 * Prefers dusk / night: TIME_PREFERENCE switches a randomly chosen DAY
 * episode to night (p=0.4) or dusk (p=0.3); the leader applies `time set`
 * before the start snapshot. PREFERRED_WEATHER = "clear" (env-setup.js
 * format; if env-setup chose rain at random the leader clears it before the
 * snapshot). state_metadata.time_of_day / time_policy="episode_preferred"
 * record the values.
 *
 * Layout (u = towards the actor, p = perpendicular), all on the ground:
 *   actor lights   (u=+1,p=-2) (u=+2,p=+2) (u=+2,p=-3)    actor home (u=+3,p=0)
 *   partner lights (u=-1,p=-2) (u=-2,p=+2) (u=-2,p=+3)    partner home (u=-3,p=0)
 *   candles (0,-1), fire base (0,-3), cake (0,+1), campfire (0,+2),
 *   pumpkin (+-2,-1) on Y's side
 * Bot X (actor or partner, shared RNG) does candles + fire, bot Y does the
 * campfire, the candle cake and the pumpkin carving.
 *
 * Flow:
 *   intro
 *   lights     both place 2-3 light sources on their side at the same time:
 *              torch, soul / redstone torch, lantern, soul lantern, jack
 *              o'lantern, glowstone, sea lantern, shroomlight, end rod,
 *              froglights, wall torch on a stone block, lantern on a wall
 *   candles    X places 1-3 candles (one colour) on one block, lights them
 *              with flint and steel, maybe blows them out (empty-hand click)
 *              and relights them
 *   campfire   Y places a campfire / soul campfire, puts it out with a
 *              shovel or a water bucket (then scoops the water back), and
 *              relights it with flint and steel or a fire charge; optional
 *              candle cake (cake + candle, lit)
 *   fire       X places netherrack / soul sand / soul soil, sets it on fire,
 *              both watch, X punches the fire out (always removed before the
 *              end: fire age changes are not logged)
 *   carve      (p=0.4) Y places a pumpkin and carves it with shears
 *   outro
 *
 * Logged: block_place, player_interact + block_state (candles=N, lit,
 * waterlogged, carved_pumpkin, candle_cake), block_ignite (flint and steel /
 * fire charge), bucket_empty / bucket_fill, block_break (fire),
 * entity_spawn (pumpkin seeds).
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

const GROUND_LIGHTS = [
  ["torch", ["torch"]],
  ["soul_torch", ["soul_torch"]],
  ["redstone_torch", ["redstone_torch"]],
  ["lantern", ["lantern"]],
  ["soul_lantern", ["soul_lantern"]],
  ["jack_o_lantern", ["jack_o_lantern"]],
  ["glowstone", ["glowstone"]],
  ["sea_lantern", ["sea_lantern"]],
  ["shroomlight", ["shroomlight"]],
  ["end_rod", ["end_rod"]],
  ["ochre_froglight", ["ochre_froglight"]],
  ["verdant_froglight", ["verdant_froglight"]],
  ["pearlescent_froglight", ["pearlescent_froglight"]],
];
const SUPPORTS = ["cobblestone", "stone_bricks", "deepslate_bricks", "mossy_cobblestone", "polished_blackstone", "sandstone"];
const WALLS = ["cobblestone_wall", "stone_brick_wall", "blackstone_wall", "andesite_wall", "sandstone_wall"];
const CANDLE_COLORS = ["", "white_", "orange_", "magenta_", "light_blue_", "yellow_", "lime_", "pink_", "gray_", "cyan_", "purple_", "blue_", "brown_", "green_", "red_", "black_"];
const FIRE_BASES = [
  ["netherrack", "fire"],
  ["netherrack", "fire"],
  ["soul_sand", "soul_fire"],
  ["soul_soil", "soul_fire"],
];

/** One light "recipe": ground item, wall torch on a block, or lantern on a wall. */
function pickLight(rng) {
  const u = rng();
  const g = pick(rng, GROUND_LIGHTS);
  const support = pick(rng, SUPPORTS);
  const wall = pick(rng, WALLS);
  const soul = rng() < 0.35;
  if (u < 0.18) {
    const torch = soul ? "soul_torch" : "torch";
    return { kind: "wall_torch", support, item: torch, expect: [soul ? "soul_wall_torch" : "wall_torch"], items: [[support, 1], [torch, 1]] };
  }
  if (u < 0.32) {
    const lantern = soul ? "soul_lantern" : "lantern";
    return { kind: "wall_lantern", support: wall, item: lantern, expect: [lantern], items: [[wall, 1], [lantern, 1]] };
  }
  return { kind: "ground", item: g[0], expect: g[1], items: [[g[0], 1]] };
}

class LightAndFireEpisode extends MechanicsEpisodeBase {
  static INIT_MIN_BOTS_DISTANCE = 5;
  static INIT_MAX_BOTS_DISTANCE = 9;
  static WORKS_IN_NON_FLAT_WORLD = true;
  static SITE_HALF_SIZE = 4;
  static TIME_PREFERENCE = { night: 0.4, dusk: 0.3 };
  // rain would put the fire out under the open sky
  static PREFERRED_WEATHER = "clear";

  planEpisode(bot, rng, args, frame, isActor) {
    const nA = randInt(rng, 2, 3);
    const nB = randInt(rng, 2, 3);
    const lightsA = [pickLight(rng), pickLight(rng), pickLight(rng)].slice(0, nA);
    const lightsB = [pickLight(rng), pickLight(rng), pickLight(rng)].slice(0, nB);
    const xIsActor = rng() < 0.5;
    const candle = `${pick(rng, CANDLE_COLORS)}candle`;
    const nCandles = randInt(rng, 1, 3);
    const blowOut = rng() < 0.55;
    const campfire = rng() < 0.65 ? "campfire" : "soul_campfire";
    const douse = rng() < 0.5 ? "shovel" : "water";
    const igniter = rng() < 0.7 ? "flint_and_steel" : "fire_charge";
    const [fireBase, fireName] = pick(rng, FIRE_BASES);
    const burnMs = randInt(rng, 3000, 5000);
    const doCake = rng() < 0.35;
    const cakeCandle = `${pick(rng, CANDLE_COLORS)}candle`;
    const doCarve = rng() < 0.4;

    // (u=+-1, p=+2) stays free: it is next to the campfire and would block
    // the bucket ray of the bot that stands at (u=+-1, p=+1)
    const cellsA = [frame.at(1, -2), frame.at(2, 2), frame.at(2, -3)];
    // pumpkin on Y's side (Y = the bot that is not X)
    const pumpkinPos = frame.at(xIsActor ? -2 : 2, -1);
    const cellsB = [frame.at(-1, -2), frame.at(-2, 2), frame.at(-2, 3)];
    const itemsA = lightsA.flatMap((l) => l.items);
    const itemsB = lightsB.flatMap((l) => l.items);
    const xItems = [[candle, nCandles + 1], ["flint_and_steel", 1], [fireBase, 1]];
    const yItems = [[campfire, 1]];
    if (douse === "water") yItems.push(["water_bucket", 1]);
    yItems.push([igniter, 1]);
    if (doCake) yItems.push(["cake", 1], [cakeCandle, 1], ["flint_and_steel", 1]);
    if (doCarve) yItems.push(["pumpkin", 1], ["shears", 1]);

    return {
      actorStand: frame.at(3, 0),
      observerStand: frame.at(-3, 0),
      items: {
        actor: [...itemsA, ...(xIsActor ? xItems : yItems)],
        observer: [...itemsB, ...(xIsActor ? yItems : xItems)],
      },
      lightsA,
      lightsB,
      cellsA,
      cellsB,
      xIsActor,
      candle,
      nCandles,
      blowOut,
      campfire,
      douse,
      igniter,
      fireBase,
      fireName,
      burnMs,
      doCake,
      cakeCandle,
      doCarve,
      candlePos: frame.at(0, -1),
      firePos: frame.at(0, -3),
      cakePos: frame.at(0, 1),
      campfirePos: frame.at(0, 2),
      pumpkinPos,
      metadata: {
        actor_lights: lightsA.map((l, i) => ({ kind: l.kind, item: l.item, support: l.support || null, pos: toArr(cellsA[i]) })),
        observer_lights: lightsB.map((l, i) => ({ kind: l.kind, item: l.item, support: l.support || null, pos: toArr(cellsB[i]) })),
        fire_and_candles_by: xIsActor ? "actor" : "observer",
        campfire_by: xIsActor ? "observer" : "actor",
        candle,
        candle_count: nCandles,
        candle_blow_out: blowOut,
        candle_pos: toArr(frame.at(0, -1)),
        campfire,
        campfire_pos: toArr(frame.at(0, 2)),
        campfire_douse: douse,
        campfire_igniter: igniter,
        fire_base: fireBase,
        fire_block: fireName,
        fire_pos: toArr(frame.at(0, -3)),
        burn_ms: burnMs,
        candle_cake: doCake ? cakeCandle : null,
        pumpkin_carving: doCarve ? toArr(pumpkinPos) : null,
      },
    };
  }

  buildSteps(ctx) {
    const { bot, plan, isActor, frame } = ctx;
    const amX = plan.xIsActor === isActor;
    const myU = isActor ? 1 : -1;
    const myLights = isActor ? plan.lightsA : plan.lightsB;
    const myCells = isActor ? plan.cellsA : plan.cellsB;
    const c = (p) => () => p.offset(0.5, 0.6, 0.5);

    const placeLight = async (cell, light) => {
      if (light.kind === "ground") {
        return placeItemAt(bot, cell, light.item, { face: FACES.up, refPos: cell.offset(0, -1, 0), expect: light.expect });
      }
      const okSupport = await placeItemAt(bot, cell, light.support, {
        face: FACES.up,
        refPos: cell.offset(0, -1, 0),
        expect: [light.support],
      });
      if (!okSupport) return false;
      await sleep(250);
      if (light.kind === "wall_torch") {
        const face = frame.u.scaled(myU); // side of the block that faces us
        return placeItemAt(bot, cell.plus(face), light.item, { face, refPos: cell, expect: light.expect, cursorY: 0.5 });
      }
      return placeItemAt(bot, cell.offset(0, 1, 0), light.item, { face: FACES.up, refPos: cell, expect: light.expect });
    };

    // X / Y acting step: the other bot watches until signalled
    const roleStep = (name, actorIsX, focus, fn, timeoutMs = 32000) => ({
      name,
      timeoutMs,
      run: async function () {
        const acting = actorIsX ? amX : !amX;
        if (!acting) {
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
        name: "lights",
        timeoutMs: 30000,
        run: async function () {
          const placed = [];
          for (let i = 0; i < myLights.length; i++) {
            if (this.timeUp()) break;
            const ok = await placeLight(myCells[i], myLights[i]);
            placed.push({ item: myLights[i].item, ok });
            await sleep(localJitterMs(300, 600));
            await glance(this, localJitterMs(500, 900));
          }
          await this.note("lights", { placed });
          await observe(this, () => frame.at(0, 0).offset(0.5, 0.5, 0.5), localJitterMs(1200, 1800));
          return {};
        },
      },
      roleStep("candles", true, c(plan.candlePos), async function () {
        const P = plan.candlePos;
        const res = { candle: plan.candle };
        let n = 0;
        for (let k = 0; k < plan.nCandles; k++) {
          if (this.timeUp()) break;
          const ok =
            k === 0
              ? await placeItemAt(bot, P, plan.candle, { face: FACES.up, refPos: P.offset(0, -1, 0), expect: [plan.candle] })
              : (await clickBlock(bot, P, { item: plan.candle, face: FACES.up }), true);
          if (ok) n++;
          await sleep(localJitterMs(350, 600));
        }
        res.candles = Number(propsOf(bot.blockAt(P)).candles) || 0;
        await clickBlock(bot, P, { item: "flint_and_steel" });
        await sleep(700);
        res.lit = String(propsOf(bot.blockAt(P)).lit);
        await glance(this, localJitterMs(800, 1300));
        if (plan.blowOut && res.lit === "true") {
          await clickBlock(bot, P, { item: null });
          await sleep(localJitterMs(1000, 1500));
          res.blown_out = String(propsOf(bot.blockAt(P)).lit) === "false";
          await clickBlock(bot, P, { item: "flint_and_steel" });
          await sleep(500);
          res.relit = String(propsOf(bot.blockAt(P)).lit);
        }
        await this.note("candles", res);
        await observe(this, c(P), localJitterMs(1000, 1600));
        return {};
      }),
      roleStep("campfire", false, c(plan.campfirePos), async function () {
        const P = plan.campfirePos;
        const res = { campfire: plan.campfire };
        // stand diagonally next to the campfire (bucket ray traces are
        // unreliable from further away); also next to the cake spot
        await walkTo(bot, frame.at(myU, 1), 0.45, 6000);
        res.placed = await placeItemAt(bot, P, plan.campfire, { face: FACES.up, refPos: P.offset(0, -1, 0), expect: [plan.campfire] });
        await observe(this, c(P), localJitterMs(1800, 2600));
        if (res.placed) {
          if (plan.douse === "shovel") {
            await clickBlock(bot, P, { item: "diamond_shovel" });
            await sleep(900);
          } else {
            await useItemTowards(bot, "water_bucket", P.offset(0.5, 0.3, 0.5));
            await sleep(350);
          }
          const pr = propsOf(bot.blockAt(P));
          res.doused = { lit: pr.lit, waterlogged: pr.waterlogged };
          if (String(pr.waterlogged) === "true") {
            // scoop at once: water leaking out of the campfire pushes us away
            for (let k = 0; k < 3; k++) {
              if (k > 0) await walkTo(bot, frame.at(myU, 1), 0.45, 4000);
              await useItemTowards(bot, "bucket", P.offset(0.5, 0.5, 0.5));
              await sleep(500);
              if (String(propsOf(bot.blockAt(P)).waterlogged) !== "true") break;
            }
            res.scooped = String(propsOf(bot.blockAt(P)).waterlogged) === "false";
          }
          await glance(this, localJitterMs(800, 1200));
          await walkTo(bot, frame.at(myU, 1), 0.45, 4000);
          await clickBlock(bot, P, { item: plan.igniter });
          await sleep(700);
          res.relit = String(propsOf(bot.blockAt(P)).lit);
        }
        if (plan.doCake && !this.timeUp()) {
          const K = plan.cakePos;
          if (await placeItemAt(bot, K, "cake", { face: FACES.up, refPos: K.offset(0, -1, 0), expect: ["cake"] })) {
            await sleep(400);
            await clickBlock(bot, K, { item: plan.cakeCandle });
            await sleep(500);
            await clickBlock(bot, K, { item: "flint_and_steel" });
            await sleep(500);
            res.cake = blockName(bot, K);
          }
        }
        await this.note("campfire", res);
        await observe(this, c(P), localJitterMs(1000, 1500));
        await walkTo(bot, isActor ? plan.actorStand : plan.observerStand, 0.8, 5000);
        return {};
      }),
      roleStep("fire", true, c(plan.firePos), async function () {
        const B = plan.firePos;
        const F = B.offset(0, 1, 0);
        const res = { base: plan.fireBase };
        // stand a bit closer to the fire spot
        await walkTo(bot, frame.at(2 * myU, -2), 0.8, 5000);
        res.placed = await placeItemAt(bot, B, plan.fireBase, { face: FACES.up, refPos: B.offset(0, -1, 0), expect: [plan.fireBase] });
        if (res.placed) {
          await clickBlock(bot, B, { item: "flint_and_steel", face: FACES.up });
          await sleep(600);
          res.fire = blockName(bot, F);
          await observe(this, c(F), plan.burnMs);
        }
        // put it out (fire must be gone before the end: fire age is not logged)
        for (let k = 0; k < 4; k++) {
          const n = blockName(bot, F);
          if (!n || !n.includes("fire")) break;
          await digAt(bot, F, 2500);
          await sleep(400);
        }
        res.after = blockName(bot, F);
        await this.note("fire", res);
        await walkTo(bot, isActor ? plan.actorStand : plan.observerStand, 0.8, 5000);
        await glance(this, localJitterMs(700, 1000));
        return {};
      }),
      roleStep("carve", false, c(plan.pumpkinPos), async function () {
        if (!plan.doCarve) {
          await glance(this, localJitterMs(600, 900));
          return {};
        }
        const P = plan.pumpkinPos;
        const res = {};
        res.placed = await placeItemAt(bot, P, "pumpkin", { face: FACES.up, refPos: P.offset(0, -1, 0), expect: ["pumpkin"] });
        if (res.placed) {
          await sleep(400);
          await clickBlock(bot, P, { item: "shears" });
          await sleep(600);
          res.block = blockName(bot, P);
        }
        await this.note("carve", res);
        await observe(this, c(P), localJitterMs(900, 1400));
        return {};
      }, 20000),
      {
        name: "safety",
        timeoutMs: 12000,
        run: async function () {
          // whoever is X double-checks that no fire is left on the base block
          if (amX) {
            const F = plan.firePos.offset(0, 1, 0);
            for (let k = 0; k < 3; k++) {
              const n = blockName(bot, F);
              if (!n || !n.includes("fire")) break;
              await walkTo(bot, frame.at(2 * myU, -2), 0.8, 4000);
              await digAt(bot, F, 2500);
            }
          }
          await observe(this, () => frame.at(0, 0).offset(0.5, 0.6, 0.5), localJitterMs(1200, 1800));
          return {};
        },
      },
      outroStep(),
    ];
  }
}

module.exports = { LightAndFireEpisode };
