/**
 * fallingBlocks - gravity blocks (sand, red sand, gravel, concrete powder),
 * concrete powder hardening in water and an optional anvil drop.
 *
 * Layout (frame u = towards the actor, p = perpendicular, level = feet):
 *   tower T      column (u=0,  p=+2)
 *   water hole H column (u=+1, p=+2), ground block (dug by the actor)
 *   side pile    column (u=-1, p=+2) (partner drops blocks there)
 *   anvil        column (u=0,  p=+3)
 *   actor works at (u=+2, p=+1); partner home (u=-4, p=0), works at (u=-2, p=+1)
 *
 * Flow:
 *   intro
 *   tower      actor stacks h (3-4) gravity blocks, optionally on a
 *              non-gravity support block (planks, cobblestone, ...)
 *   drop       actor breaks the support (or the bottom gravity block): the
 *              whole column becomes falling_block entities and lands 1 lower
 *   side_drop  partner walks up and places 1-3 gravity blocks of another
 *              material against the side of the tower top: each one falls
 *              to the ground immediately (pile)
 *   concrete   actor digs the hole H, pours water into it and places
 *              concrete powder against the tower above the hole: it falls
 *              into the water and hardens to concrete (block_form); a second
 *              powder lands on top and stays powder
 *   anvil      (p=0.45) actor places an anvil against the outer side of the
 *              tower top; it falls next to the tower (may chip on landing)
 *   outro
 *
 * Logged: block_place, block_break, entity_spawn (falling_block, item),
 * entity_change_block (block -> falling entity, entity -> block),
 * bucket_empty, block_form (concrete), block_state readbacks.
 */

const {
  FACES,
  MechanicsEpisodeBase,
  blockName,
  clickBlock,
  digAt,
  glance,
  introStep,
  isAirName,
  localJitterMs,
  observe,
  observeUntilSignal,
  outroStep,
  pick,
  placeItemAt,
  randInt,
  shuffle,
  signalDone,
  sleep,
  toArr,
  useItemTowards,
  walkTo,
} = require("./mechanics-common");

const { Vec3 } = require("vec3");

const COLORS = ["white", "orange", "magenta", "light_blue", "yellow", "lime", "pink", "gray", "light_gray", "cyan", "purple", "blue", "brown", "green", "red", "black"];
const TOWER_MATERIALS = ["sand", "red_sand", "gravel", "sand", "gravel", "white_concrete_powder", "yellow_concrete_powder", "lime_concrete_powder"];
const SIDE_MATERIALS = ["sand", "red_sand", "gravel", "light_blue_concrete_powder", "pink_concrete_powder", "orange_concrete_powder"];
const SUPPORTS = ["oak_planks", "cobblestone", "stone_bricks", "dirt", "spruce_log", "glass", "bricks", "birch_planks"];
const ANVILS = ["anvil", "anvil", "chipped_anvil", "damaged_anvil"];

function faceName(v) {
  if (v.x > 0) return "east";
  if (v.x < 0) return "west";
  if (v.z > 0) return "south";
  return "north";
}

class FallingBlocksEpisode extends MechanicsEpisodeBase {
  static INIT_MIN_BOTS_DISTANCE = 5;
  static INIT_MAX_BOTS_DISTANCE = 9;
  static WORKS_IN_NON_FLAT_WORLD = true;
  static SITE_HALF_SIZE = 4;
  static CLEAR_HEIGHT = 6;

  planEpisode(bot, rng, args, frame, isActor) {
    const tower = pick(rng, TOWER_MATERIALS);
    const side = pick(rng, SIDE_MATERIALS.filter((m) => m !== tower));
    const useSupport = rng() < 0.55;
    const support = pick(rng, SUPPORTS);
    const h = randInt(rng, 3, 4);
    const nSide = randInt(rng, 1, 3);
    const colors = shuffle(rng, COLORS);
    const powderA = `${colors[0]}_concrete_powder`;
    const powderB = `${colors[1]}_concrete_powder`;
    const doAnvil = rng() < 0.45;
    const anvil = pick(rng, ANVILS);

    const T = (dy) => frame.at(0, 2, dy);
    const base = useSupport ? 1 : 0;
    const towerCells = [];
    for (let k = 0; k < h; k++) towerCells.push(T(base + k));
    const hole = frame.at(1, 2, -1);

    return {
      actorStand: frame.at(2, 1),
      observerStand: frame.at(-4, 0),
      partnerWork: frame.at(-2, 1),
      items: {
        actor: [
          [tower, h + 1],
          ...(useSupport ? [[support, 1]] : []),
          ["water_bucket", 1],
          [powderA, 1],
          [powderB, 1],
          ...(doAnvil ? [[anvil, 1]] : []),
        ],
        observer: [[side, nSide + 1]],
      },
      T,
      towerCells,
      hole,
      useSupport,
      support,
      tower,
      side,
      h,
      nSide,
      powderA,
      powderB,
      doAnvil,
      anvil,
      metadata: {
        tower_material: tower,
        tower_height: h,
        tower_column: toArr(T(0)),
        support_block: useSupport ? support : null,
        side_material: side,
        side_count: nSide,
        side_column: toArr(frame.at(-1, 2, 0)),
        water_hole: toArr(hole),
        concrete_powders: [powderA, powderB],
        anvil: doAnvil ? anvil : null,
        anvil_column: doAnvil ? toArr(frame.at(0, 3, 0)) : null,
      },
    };
  }

  buildSteps(ctx) {
    const { bot, plan, isActor, frame } = ctx;
    const towerFocus = () => plan.T(1).offset(0.5, 0.5, 0.5);
    const uFace = frame.u; // tower face pointing to the actor side (+u)
    const minusU = frame.u.scaled(-1);
    const plusP = frame.p;
    // highest non-air block of the tower column (dy 0..6)
    const towerTop = () => {
      let top = null;
      for (let dy = 0; dy <= 6; dy++) {
        const n = blockName(bot, plan.T(dy));
        if (n && !isAirName(n)) top = dy;
      }
      return top;
    };
    // place a gravity block against `refPos` (face) - it may fall at once, so
    // success is not verified by polling the target
    const dropPlace = async (refPos, face, item) => {
      const ok = await clickBlock(bot, refPos, { item, face });
      await sleep(localJitterMs(700, 1100));
      return ok;
    };

    const actorAct = (name, fn) => ({
      name,
      timeoutMs: 30000,
      run: async function () {
        if (!isActor) {
          await observeUntilSignal(this, towerFocus, name, 27000, { avoid: [plan.T(0), plan.hole] });
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
      actorAct("tower", async function () {
        const placed = [];
        if (plan.useSupport) {
          if (await placeItemAt(bot, plan.T(0), plan.support, { face: FACES.up, refPos: plan.T(-1), expect: [plan.support] })) {
            placed.push(plan.support);
          }
          await sleep(localJitterMs(250, 450));
        }
        for (const cell of plan.towerCells) {
          if (this.timeUp()) break;
          const ok = await placeItemAt(bot, cell, plan.tower, {
            face: FACES.up,
            refPos: cell.offset(0, -1, 0),
            expect: [plan.tower],
          });
          if (ok) placed.push(plan.tower);
          else break;
          await sleep(localJitterMs(250, 500));
          if (Math.random() < 0.3) await glance(this, localJitterMs(400, 700));
        }
        await this.note("tower", { material: plan.tower, support: plan.useSupport ? plan.support : null, placed: placed.length });
        await observe(this, towerFocus, localJitterMs(700, 1100), { strafe: false });
        return { placed: placed.length };
      }),
      actorAct("drop", async function () {
        const bottom = plan.T(0);
        const name = blockName(bot, bottom);
        const ok = name && !isAirName(name) ? await digAt(bot, bottom, 9000) : false;
        await this.note("drop", { broke: name, ok });
        await observe(this, towerFocus, localJitterMs(2200, 3000), { strafe: false });
        return {};
      }),
      {
        name: "side_drop",
        timeoutMs: 30000,
        run: async function () {
          if (isActor) {
            await observeUntilSignal(this, towerFocus, "side_drop", 27000, { strafe: false });
            return {};
          }
          let dropped = 0;
          try {
            await walkTo(bot, plan.partnerWork, 0.7, 7000);
            for (let k = 0; k < plan.nSide; k++) {
              if (this.timeUp()) break;
              const top = towerTop();
              if (top === null || top < 1) break;
              const dy = Math.max(1, top - (k % 2));
              if (await dropPlace(plan.T(dy), minusU, plan.side)) dropped++;
              if (Math.random() < 0.4) await glance(this, localJitterMs(400, 700));
            }
            await this.note("side_drop", { material: plan.side, dropped });
            await observe(this, towerFocus, localJitterMs(1200, 1800), { strafe: false });
          } finally {
            signalDone(this, "side_drop");
          }
          return { dropped };
        },
      },
      actorAct("concrete", async function () {
        const H = plan.hole;
        const res = { hole: toArr(H) };
        res.dug = await digAt(bot, H, 8000);
        await sleep(300);
        if (res.dug) {
          await useItemTowards(bot, "water_bucket", new Vec3(H.x + 0.5, H.y + 0.05, H.z + 0.5));
          await sleep(900);
          res.water = blockName(bot, H);
        }
        await glance(this, localJitterMs(400, 700));
        // powder against the tower face above the hole: falls into the water
        const top = towerTop();
        const dy = top === null ? null : Math.min(top, 2);
        if (dy !== null && dy >= 1) {
          await dropPlace(plan.T(dy), uFace, plan.powderA);
          await sleep(900);
          res.first = blockName(bot, H);
          await dropPlace(plan.T(dy), uFace, plan.powderB);
          await sleep(900);
          res.second = blockName(bot, H.offset(0, 1, 0));
        }
        await this.note("concrete", res);
        await observe(this, () => H.offset(0.5, 0.8, 0.5), localJitterMs(1200, 1800), { strafe: false });
        return {};
      }),
      actorAct("anvil", async function () {
        if (!plan.doAnvil) {
          await glance(this, localJitterMs(600, 900));
          return {};
        }
        const top = towerTop();
        if (top !== null && top >= 1) {
          await dropPlace(plan.T(top), plusP, plan.anvil);
          await sleep(1200);
          const landed = blockName(bot, frame.at(0, 3, 0));
          await this.note("anvil", { anvil: plan.anvil, from_dy: top, landed });
        }
        await observe(this, () => frame.at(0, 3, 0).offset(0.5, 0.5, 0.5), localJitterMs(1000, 1500), { strafe: false });
        return {};
      }),
      outroStep(),
    ];
  }
}

module.exports = { FallingBlocksEpisode, faceName };
