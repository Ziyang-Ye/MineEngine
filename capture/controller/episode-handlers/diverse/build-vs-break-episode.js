/**
 * buildVsBreak - one bot builds a small wall / tower / pillar / arch while
 * the other one tears it down, for 3-5 rounds, so the same voxels appear
 * and disappear again and again (often in a different material).
 *
 * Plan (shared RNG; 15 draws): shape (wall 3-5 x 2-3, tower 1 x 3-5,
 * pillar 2x2 x 2-3, arch 3 wide x 2-3), rounds 3-5, per round a mode
 * ("turns": build everything, then break everything; "contest": the breaker
 * removes blocks while the builder is still placing) and a wall material
 * (materials.js walls pool), and whether the last structure is kept.
 *
 * The builder (actor) stands on one side of the structure, the breaker on
 * the other side, facing each other through it; whoever is not acting
 * watches the structure / the other player (T8, standing still).
 *
 * Logged changes: block_place, block_break (+ dig_start/progress/finish),
 * entity_spawn / entity_remove (drops, pickups), block_state.
 */

const { Vec3 } = require("vec3");

const dc = require("./diverse-common");
const { countMaterials, pickMaterial } = require("../../utils/materials");

const { randInt, note, sleep } = dc;

const SHAPES = ["wall", "tower", "pillar2", "arch"];
const MAX_ROUNDS = 5;

/** Voxels as [dp, dy, du] (du <= 0 is behind the plane, away from the builder). */
function shapeVoxels(shape, a, b) {
  const out = [];
  if (shape === "wall") {
    const w = 3 + Math.min(2, Math.floor(a * 3));
    const h = 2 + Math.min(1, Math.floor(b * 2));
    const p0 = -Math.floor((w - 1) / 2);
    for (let dy = 0; dy < h; dy++) for (let dp = p0; dp < p0 + w; dp++) out.push([dp, dy, 0]);
  } else if (shape === "tower") {
    const h = 3 + Math.min(2, Math.floor(a * 3));
    for (let dy = 0; dy < h; dy++) out.push([0, dy, 0]);
  } else if (shape === "pillar2") {
    const h = 2 + Math.min(1, Math.floor(b * 2));
    for (let dy = 0; dy < h; dy++) for (const dp of [0, 1]) for (const du of [0, -1]) out.push([dp, dy, du]);
  } else {
    const h = 2 + Math.min(1, Math.floor(b * 2));
    for (let dy = 0; dy < h; dy++) {
      out.push([-1, dy, 0]);
      out.push([1, dy, 0]);
    }
    out.push([0, h - 1, 0]);
  }
  return out;
}

class BuildVsBreakEpisode extends dc.DiverseEpisodeBase {
  static INIT_MIN_BOTS_DISTANCE = 4;
  static INIT_MAX_BOTS_DISTANCE = 9;
  static WORKS_IN_NON_FLAT_WORLD = true;
  static SITE_HALF_SIZE = 2;

  planEpisode(bot, rng, args, frame, isActor) {
    const shape = SHAPES[Math.min(SHAPES.length - 1, Math.floor(rng() * SHAPES.length))];
    const a = rng();
    const b = rng();
    const rounds = randInt(rng, 3, MAX_ROUNDS);
    const modes = [];
    const materials = [];
    for (let i = 0; i < MAX_ROUNDS; i++) modes.push(rng() < 0.6 ? "turns" : "contest");
    for (let i = 0; i < MAX_ROUNDS; i++) materials.push(pickMaterial(rng, "walls", i > 0 ? [materials[i - 1]] : []));
    const keepLast = rng() < 0.4;

    const local = shapeVoxels(shape, a, b);
    // bottom-up, then left to right / front to back
    local.sort((x, y) => x[1] - y[1] || x[0] - y[0] || y[2] - x[2]);
    const voxels = local.map(([dp, dy, du]) => frame.at(du, dp, dy));
    const plan = [];
    for (let r = 0; r < rounds; r++) plan.push({ mode: modes[r], material: materials[r] });
    const minDu = Math.min(...local.map((v) => v[2]));
    const placements = [];
    for (const r of plan) for (const v of voxels) placements.push({ material: r.material });
    const counts = countMaterials(placements);
    const center = (() => {
      let x = 0, y = 0, z = 0;
      for (const v of voxels) { x += v.x; y += v.y; z += v.z; }
      const n = voxels.length;
      return new Vec3(x / n + 0.5, y / n + 0.5, z / n + 0.5);
    })();
    const breakerTools = plan.some((r) => r.material.endsWith("_wool")) ? [["shears", 1]] : [];
    return {
      actorStand: frame.at(2, 0),
      observerStand: frame.at(minDu - 2, 0),
      items: {
        actor: Object.entries(counts).map(([m, c]) => [m, c + 2]),
        observer: breakerTools,
      },
      voxels,
      rounds: plan,
      keepLast,
      center,
      clearBox: { x1: frame.site.x - 4, z1: frame.site.z - 4, x2: frame.site.x + 4, z2: frame.site.z + 4, y1: frame.level, y2: frame.level + 2 },
      metadata: {
        shape,
        voxels: voxels.map((p) => [p.x, p.y, p.z]),
        rounds: plan,
        keep_last_structure: keepLast,
        materials: { walls: [...new Set(plan.map((r) => r.material))] },
        material_counts: counts,
        roles: {
          builder: isActor ? bot.username : args.other_bot_name,
          breaker: isActor ? args.other_bot_name : bot.username,
        },
        observer_policy: "opposite sides of the structure, idle bot watches (T8)",
      },
    };
  }

  buildSteps(ctx) {
    const { bot, rcon, args, episodeNum, plan } = ctx;
    const voxSet = new Set(plan.voxels.map(dc.key3));
    const center = () => plan.center;
    const watchStill = () => ({ focus: center, canMove: false, actorProb: 0.6 });
    const builderSide = { forbid: (x, z, y) => voxSet.has(`${x},${y},${z}`) || voxSet.has(`${x},${y + 1},${z}`), otherName: ctx.otherName, maxH: 3.2 };

    const build = (r) => async () => {
      const list = plan.voxels.map((pos) => ({ pos, material: plan.rounds[r].material }));
      const res = await dc.buildPlacements(ctx, list, { deadline: Date.now() + 30000, glanceProb: 0.2, standOpts: builderSide, delayMs: [120, 300] });
      await note(rcon, args, bot, episodeNum, "build_round", { round: r, material: plan.rounds[r].material, placed: res.placed.length });
      return {};
    };
    const present = () => plan.voxels.filter((p) => {
      const b = bot.blockAt(p);
      return b && !dc.isAirName(b.name);
    });
    const breakAll = (r) => async () => {
      let broken = 0;
      const deadline = Date.now() + 30000;
      for (let guard = 0; guard < 40 && Date.now() < deadline; guard++) {
        const left = present().sort((p, q) => q.y - p.y);
        if (left.length === 0) break;
        if (await dc.digBlockAt(bot, left[0], builderSide)) broken++;
        await dc.glance(ctx, 0.35);
        await sleep(dc.rand(100, 250));
      }
      await note(rcon, args, bot, episodeNum, "break_round", { round: r, broken });
      return {};
    };
    const contestBreak = (r, sig) => async () => {
      let broken = 0;
      const deadline = Date.now() + 35000;
      while (Date.now() < deadline && !bot._episodeStopping) {
        const left = present().sort((p, q) => q.y - p.y);
        if (left.length === 0) {
          if (ctx.signals.has(sig)) break;
          await dc.watchFor(bot, () => {
            const e = bot.players[ctx.otherName]?.entity;
            return e ? e.position.offset(0, 1.2, 0) : center();
          }, 500);
          continue;
        }
        if (await dc.digBlockAt(bot, left[0], builderSide)) broken++;
        await sleep(dc.rand(150, 350));
      }
      await note(rcon, args, bot, episodeNum, "contest_round", { round: r, broken });
      return {};
    };

    const steps = [dc.greetStep()];
    plan.rounds.forEach((round, r) => {
      const last = r === plan.rounds.length - 1;
      if (round.mode === "turns") {
        steps.push(dc.dualStep(`build_${r}`, build(r), null, { maxMs: 35000, observe: watchStill }));
        if (!(last && plan.keepLast)) steps.push(dc.dualStep(`break_${r}`, null, breakAll(r), { maxMs: 35000, observe: watchStill }));
      } else {
        const name = `contest_${r}`;
        steps.push(dc.dualStep(name, build(r), contestBreak(r, `${name}_done`), { maxMs: 40000, observe: watchStill }));
        if (!(last && plan.keepLast)) steps.push(dc.dualStep(`clear_${r}`, null, breakAll(r), { maxMs: 25000, observe: watchStill }));
      }
    });
    steps.push(dc.lookStep("admire", [1500, 2500], center));
    steps.push(dc.outroStep());
    return dc.bindSteps(ctx, steps);
  }
}

module.exports = { BuildVsBreakEpisode };
