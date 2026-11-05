/**
 * crater - dig a wide, shallow, roughly round crater (diameter 5-9, depth 1
 * at the rim, 1-2 in the middle), then (50%) refill it with another
 * material, so the ground surface changes over a large area.
 *
 * Plan (shared RNG; 7 draws): radius R in {2.5, 3, 3.5, 4}, centre depth
 * (1 or 2), refill (50%), refill material (dirt, cobblestone, stone,
 * gravel, sand), dig start (centre-out or rim-in), observer side.
 *
 * Actor: digs the top layer (centre-out or rim-in rings), then the inner
 * layer, walking inside the crater (steps are 1 block high); refill: lower
 * layer first, then the top layer from the rim inwards (sand / gravel stay
 * put because every cell is supported). Without refill it walks along the
 * rim and inspects the crater.
 * Observer (T8): on a ring 2-6 blocks outside the crater rim, mostly
 * looking at the digger.
 *
 * Logged changes: block_break (+ dig records), item entity_spawn/remove,
 * block_place, block_state.
 */

const { Vec3 } = require("vec3");

const dc = require("./diverse-common");

const { note, sleep } = dc;

const RADII = [2.5, 3, 3.5, 4];

class CraterEpisode extends dc.DiverseEpisodeBase {
  static INIT_MIN_BOTS_DISTANCE = 5;
  static INIT_MAX_BOTS_DISTANCE = 11;
  static WORKS_IN_NON_FLAT_WORLD = true;
  static SITE_HALF_SIZE = 4;

  planEpisode(bot, rng, args, frame, isActor) {
    let rIdx = Math.min(RADII.length - 1, Math.floor(rng() * RADII.length));
    const centerDepth = rng() < 0.6 ? 2 : 1;
    const refill = rng() < 0.5;
    const uMat = rng();
    const rimIn = rng() < 0.4;
    const observerOpposite = rng() < 0.5;
    const uExtra = rng();
    void uExtra;

    // keep the episode within ~2 minutes: dug volume <= 55 (<= 36 when refilled)
    const maxVolume = refill ? 36 : 55;
    const layout = (R) => {
      const top = [];
      const inner = [];
      const cols = new Set();
      const cells = [];
      const n = Math.ceil(R);
      for (let du = -n; du <= n; du++) {
        for (let dp = -n; dp <= n; dp++) {
          const d = Math.sqrt(du * du + dp * dp);
          if (d > R + 1e-6) continue;
          const depth = centerDepth === 2 && d <= R * 0.5 + 1e-6 ? 2 : 1;
          const p1 = frame.at(du, dp, -1);
          top.push({ pos: p1, d });
          cols.add(dc.key2(p1.x, p1.z));
          if (depth === 2) inner.push({ pos: frame.at(du, dp, -2), d });
          cells.push([p1.x, p1.z, depth]);
        }
      }
      return { top, inner, cols, cells };
    };
    let L = layout(RADII[rIdx]);
    while (rIdx > 0 && L.top.length + L.inner.length > maxVolume) L = layout(RADII[--rIdx]);
    const R = RADII[rIdx];
    const { top, inner, cols, cells } = L;
    const refillMaterial = refill ? ["dirt", "cobblestone", "stone", "gravel", "sand"][Math.min(4, Math.floor(uMat * 5))] : null;
    const volume = top.length + inner.length;
    const g = frame.site.y;
    const all = [...top, ...inner].map((c) => c.pos);
    const xs = all.map((p) => p.x);
    const zs = all.map((p) => p.z);
    const box = [Math.min(...xs), g - (inner.length ? 1 : 0), Math.min(...zs), Math.max(...xs), g, Math.max(...zs)];
    const leaderSetupCommands = [];
    if (!this.isFlatWorld(args)) {
      for (const fl of ["water", "lava"]) {
        leaderSetupCommands.push(`fill ${box[0] - 1} ${box[1] - 1} ${box[2] - 1} ${box[3] + 1} ${box[4]} ${box[5] + 1} minecraft:stone replace minecraft:${fl}`);
      }
    }
    const obsR = Math.ceil(R) + 3;
    return {
      actorStand: rimIn ? frame.at(Math.ceil(R) + 1, 0) : frame.at(0, 0),
      observerStand: observerOpposite ? frame.at(-obsR, 0) : frame.at(0, obsR),
      items: { actor: refillMaterial ? [[refillMaterial, volume + 4]] : [], observer: [] },
      R,
      top,
      inner,
      cols,
      rimIn,
      refillMaterial,
      clearBox: { x1: box[0] - 5, z1: box[2] - 5, x2: box[3] + 5, z2: box[5] + 5, y1: frame.level, y2: frame.level + 2 },
      leaderSetupCommands,
      metadata: {
        crater: {
          radius: R,
          diameter: 2 * Math.ceil(R) + 1,
          center_depth: centerDepth,
          volume,
          top_y: g,
          box,
          cells,
        },
        dig_order: rimIn ? "rim_in" : "centre_out",
        refill,
        refill_material: refillMaterial,
        materials: refillMaterial ? { refill: refillMaterial } : {},
        observer_view: observerOpposite ? "opposite" : "side",
        observer_policy: "rim_ring_T8",
      },
    };
  }

  buildSteps(ctx) {
    const { bot, rcon, args, episodeNum, plan, frame } = ctx;
    const center = () => new Vec3(frame.site.x + 0.5, frame.site.y + 0.3, frame.site.z + 0.5);
    const inCrater = (x, z) => plan.cols.has(dc.key2(x, z));
    const observe = () => ({
      focus: center,
      avoid: inCrater,
      minR: plan.R + 2,
      maxR: plan.R + 6,
    });

    const dig = async () => {
      const deadline = Date.now() + 55000;
      await note(rcon, args, bot, episodeNum, "crater_dig_start", { radius: plan.R });
      const ringOf = new Map(plan.top.concat(plan.inner).map((c) => [dc.key3(c.pos), Math.round(c.d)]));
      let dug = 0;
      for (const layer of [plan.top, plan.inner]) {
        if (layer.length === 0) continue;
        // ring by ring (centre-out or rim-in), sweeping around each ring
        const res = await dc.digPositions(
          ctx,
          layer.map((c) => c.pos),
          {
            deadline,
            glanceProb: 0.2,
            standOpts: { maxH: 3.2 },
            group: (p) => (plan.rimIn ? -1 : 1) * ringOf.get(dc.key3(p)),
          },
        );
        dug += res.dug.length;
        await note(rcon, args, bot, episodeNum, "crater_layer", { dug: res.dug.length, failed: res.failed.length });
        await dc.glance(ctx, 0.7, [700, 1200]);
      }
      return { dug };
    };

    const after = async () => {
      if (plan.refillMaterial) {
        const deadline = Date.now() + 40000;
        const list = [
          ...plan.inner.slice().sort((a, b) => a.d - b.d),
          ...plan.top.slice().sort((a, b) => b.d - a.d),
        ].map((c) => ({ pos: c.pos, material: plan.refillMaterial }));
        const res = await dc.buildPlacements(ctx, list, { deadline, glanceProb: 0.3, standOpts: { maxH: 3.2 } });
        await note(rcon, args, bot, episodeNum, "crater_refilled", { placed: res.placed.length, failed: res.failed.length });
        const out = dc.surfaceAt(bot, frame.at(Math.ceil(plan.R) + 2, 0).x, frame.at(Math.ceil(plan.R) + 2, 0).z, frame.level);
        if (out) await dc.moveTo(bot, out, { timeoutMs: 5000 });
        return { placed: res.placed.length };
      }
      // inspect: walk around the rim, looking into the crater
      const t0 = Date.now();
      for (let k = 0; k < 3 && Date.now() - t0 < 15000; k++) {
        const v = dc.pickVantage(bot, center(), { minR: plan.R + 1, maxR: plan.R + 3, avoid: inCrater, otherName: ctx.otherName, near: bot.entity.position });
        if (v) await dc.moveTo(bot, v, { timeoutMs: 4000, avoid: inCrater });
        await dc.watchFor(bot, center, dc.rand(900, 1600));
        await dc.glance(ctx, 0.6, [600, 1000]);
      }
      await note(rcon, args, bot, episodeNum, "crater_inspected", {});
      return {};
    };

    return dc.bindSteps(ctx, [
      dc.approachStep(),
      dc.dualStep("dig", dig, null, { maxMs: 80000, observe }),
      dc.lookStep("pause", [1200, 2000], center),
      dc.dualStep("after", after, null, { maxMs: 70000, observe }),
      dc.lookStep("admire", [1500, 2500], center),
      dc.outroStep(),
    ]);
  }
}

module.exports = { CraterEpisode };
