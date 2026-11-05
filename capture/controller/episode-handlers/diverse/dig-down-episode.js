/**
 * digDown - dig a rectangular pit (2x2..5x5, depth 1-4), then refill it
 * (50%) or climb out. Underground block changes are the point.
 *
 * Plan (shared RNG): pit size s_u x s_p, depth D (volume <= 40), refill
 * (50%), refill mode ("layer": blocks placed from the bottom up while the
 * digger pillars up in the middle column; "gravity": sand/gravel dropped
 * from the rim, only for pits whose columns all touch the rim), refill
 * material, observer side.
 *
 * Actor: walks onto the middle column of the pit and digs layer by layer
 * "in place": all other cells of the layer under its feet, then the block
 * under itself (it drops one level). At the bottom it either refills
 * (layer mode: places the layer around it, then jump-places under itself),
 * or leaves the pit (walk / pathfinder-dug staircase / pillar) and drops
 * sand or gravel into every column from the rim (gravity mode), or just
 * leaves the pit (no refill).
 * Observer (T8): walks around the rim (outside the pit), mostly looking at
 * the digger.
 *
 * Logged changes: block_break (+ dig_start/progress/finish), entity_spawn /
 * entity_remove (item drops / pickups), block_place, falling_block
 * entity_change_block (gravity refill, sand above dug cells), block_state.
 */

const { Vec3 } = require("vec3");

const dc = require("./diverse-common");
const { pickMaterial } = require("../../utils/materials");

const { randInt, note, sleep } = dc;

class DigDownEpisode extends dc.DiverseEpisodeBase {
  static INIT_MIN_BOTS_DISTANCE = 4;
  static INIT_MAX_BOTS_DISTANCE = 9;
  static WORKS_IN_NON_FLAT_WORLD = true;
  static SITE_HALF_SIZE = 3;

  planEpisode(bot, rng, args, frame, isActor) {
    // fixed number of shared draws
    const su = randInt(rng, 2, 5);
    const sp = randInt(rng, 2, 5);
    let depth = randInt(rng, 1, 4);
    const refill = rng() < 0.5;
    const gravityWanted = rng() < 0.5;
    const uMat = rng();
    const observerOpposite = rng() < 0.5;
    while (su * sp * depth > 40 && depth > 1) depth--;

    const u0 = -Math.floor((su - 1) / 2);
    const p0 = -Math.floor((sp - 1) / 2);
    const u1 = u0 + su - 1;
    const p1 = p0 + sp - 1;
    const allEdge = Math.min(su, sp) <= 2;
    let refillMode = "none";
    let refillMaterial = null;
    if (refill) {
      if (gravityWanted && allEdge) {
        refillMode = "gravity";
        refillMaterial = ["sand", "gravel"][Math.min(1, Math.floor(uMat * 2))];
      } else {
        refillMode = "layer";
        refillMaterial = ["dirt", "cobblestone", "stone", "dirt", "cobblestone"][Math.min(4, Math.floor(uMat * 5))];
      }
    }

    const layers = [];
    for (let k = 1; k <= depth; k++) {
      const cells = [];
      for (let du = u0; du <= u1; du++) for (let dp = p0; dp <= p1; dp++) cells.push(frame.at(du, dp, -k));
      layers.push(cells);
    }
    const middle = frame.at(0, 0, 0);
    const pitCols = new Set();
    for (const c of layers[0]) pitCols.add(dc.key2(c.x, c.z));
    // way out (no refill / gravity refill): staircase cut into the +u wall
    const stairs = { from: frame.at(u1, 0, -depth), dir: [frame.u.x, frame.u.z], steps: depth };
    const stairCols = new Set(dc.stairColumns(stairs.from, stairs.dir, stairs.steps));
    const g = frame.site.y;
    const volume = su * sp * depth;

    const actorRim = frame.at(u1 + 2, 0);
    const observerStand = observerOpposite ? frame.at(u0 - 3, 0) : frame.at(0, p1 + 3);
    const all = layers.flat();
    const xs = all.map((p) => p.x);
    const zs = all.map((p) => p.z);
    const box = [Math.min(...xs), g - depth + 1, Math.min(...zs), Math.max(...xs), g, Math.max(...zs)];

    const leaderSetupCommands = [];
    if (String(args.world_type || "flat").toLowerCase() !== "flat") {
      // no fluid may flow into the pit: replace fluids around the dig box (before the snapshot)
      const [x1, y1, z1, x2, y2, z2] = box;
      for (const fl of ["water", "lava"]) {
        leaderSetupCommands.push(`fill ${x1 - 1} ${y1 - 1} ${z1 - 1} ${x2 + 1} ${y2} ${z2 + 1} minecraft:stone replace minecraft:${fl}`);
      }
    }

    const actorItems = [["dirt", depth + 2]];
    if (refillMaterial) actorItems.push([refillMaterial, volume + 4]);
    return {
      actorStand: actorRim,
      observerStand,
      items: { actor: actorItems, observer: [] },
      layers,
      middle,
      pitCols,
      stairs,
      stairCols,
      depth,
      su,
      sp,
      volume,
      refillMode,
      refillMaterial,
      actorRim,
      clearBox: { x1: box[0] - 4, z1: box[2] - 4, x2: box[3] + 4, z2: box[5] + 4, y1: frame.level, y2: frame.level + 2 },
      leaderSetupCommands,
      metadata: {
        pit: {
          size: [su, sp],
          depth,
          volume,
          box,
          top_y: g,
          bottom_y: g - depth + 1,
        },
        dig_method: "layer_by_layer_in_place",
        exit_stairs: refillMode === "layer" ? null : { from: [stairs.from.x, stairs.from.y, stairs.from.z], dir: stairs.dir, steps: depth },
        refill: refillMode !== "none",
        refill_mode: refillMode,
        refill_material: refillMaterial,
        materials: refillMaterial ? { refill: refillMaterial } : {},
        observer_view: observerOpposite ? "opposite" : "side",
        observer_policy: "rim_ring_T8",
      },
    };
  }

  buildSteps(ctx) {
    const { bot, rcon, args, episodeNum, plan } = ctx;
    const pitFocus = () => {
      const m = plan.middle;
      return new Vec3(m.x + 0.5, m.y - plan.depth / 2, m.z + 0.5);
    };
    const avoidPit = (x, z) => plan.pitCols.has(dc.key2(x, z)) || plan.stairCols.has(dc.key2(x, z));
    const observe = () => ({
      focus: pitFocus,
      avoid: avoidPit,
      minR: Math.max(plan.su, plan.sp) / 2 + 1.5,
      maxR: Math.max(plan.su, plan.sp) / 2 + 6,
    });
    const rimFeet = () => dc.surfaceAt(bot, plan.actorRim.x, plan.actorRim.z, ctx.frame.level) || plan.actorRim;

    const digPit = async () => {
      await note(rcon, args, bot, episodeNum, "dig_start", { pit: plan.metadata.pit.box });
      const res = await dc.digPitInPlace(ctx, plan.layers, plan.middle, {
        deadline: Date.now() + 55000,
        level: ctx.frame.level,
      });
      await note(rcon, args, bot, episodeNum, "dig_done", { dug: res.dug, feet_y: Math.floor(bot.entity.position.y) });
      return res;
    };

    const refillLayers = async () => {
      const res = await dc.refillPitInPlace(ctx, plan.layers, plan.middle, plan.refillMaterial, {
        deadline: Date.now() + 50000,
      });
      await note(rcon, args, bot, episodeNum, "refill_done", { mode: "layer", placed: res.placed });
      await dc.moveTo(bot, rimFeet(), { timeoutMs: 5000 });
      return res;
    };

    const gravityRefill = async () => {
      const exit = await dc.exitPit(ctx, rimFeet(), { towerMaterial: "dirt", rimY: ctx.frame.level, stairs: plan.stairs });
      await note(rcon, args, bot, episodeNum, "exit_pit", { method: exit });
      const deadline = Date.now() + 45000;
      const top = plan.layers[0];
      const count = new Map(top.map((p) => [dc.key3(p), 0]));
      let placed = 0;
      for (let round = 0; round < plan.depth; round++) {
        // nearest-first ring around the pit
        const order = dc.startNearest(bot, top.map((pos) => ({ pos }))).map((x) => x.pos);
        for (const pos of order) {
          if (Date.now() > deadline || bot._episodeStopping) break;
          const b = bot.blockAt(pos);
          if (!b || !dc.isAirName(b.name)) continue; // column full
          const ok = await dc.placeBlockAt(bot, pos, plan.refillMaterial, {
            otherName: ctx.otherName,
            forbid: (x, z) => plan.pitCols.has(dc.key2(x, z)),
            avoid: avoidPit,
          });
          if (ok) {
            placed++;
            count.set(dc.key3(pos), count.get(dc.key3(pos)) + 1);
          }
          await dc.glance(ctx, 0.35);
          await sleep(dc.rand(150, 300));
        }
        await sleep(500);
      }
      await sleep(1200);
      await note(rcon, args, bot, episodeNum, "refill_done", { mode: "gravity", placed });
      return { placed, exit };
    };

    const after = async () => {
      if (plan.refillMode === "layer") return refillLayers();
      if (plan.refillMode === "gravity") return gravityRefill();
      const exit = await dc.exitPit(ctx, rimFeet(), { towerMaterial: "dirt", rimY: ctx.frame.level, stairs: plan.stairs });
      await note(rcon, args, bot, episodeNum, "exit_pit", { method: exit });
      return { exit };
    };

    return dc.bindSteps(ctx, [
      dc.approachStep(),
      dc.dualStep("dig", digPit, null, { maxMs: 65000, observe }),
      dc.lookStep("pause", [1200, 2000], () => pitFocus()),
      dc.dualStep("after", after, null, { maxMs: 60000, observe }),
      dc.lookStep("admire", [1500, 2500], () => pitFocus()),
      dc.outroStep(),
    ]);
  }
}

module.exports = { DigDownEpisode };
