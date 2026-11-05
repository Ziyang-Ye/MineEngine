/**
 * tunnel - dig a horizontal tunnel, either into a hillside (normal worlds,
 * when a hillside is found next to the bots) or from the bottom of a 2x2
 * access shaft (flat worlds and fallback). 50%: refill everything again.
 * Most changed voxels are underground and never visible from the surface.
 *
 * Plan (shared RNG; 8 draws): length L (4-8), width (1 or 2), shaft depth
 * (3-4; 4 when the ceiling would be a gravity block), refill (50%), refill
 * material, torch in the tunnel (when not refilled), observer side.
 *
 * Actor (pit mode): digs the shaft layer by layer standing in it (as
 * digDown), then digs the tunnel 2 high (head block, then feet block) and
 * walks in. Hill mode: digs straight into the hillside from the foot of the
 * slope. Refill: walks back towards the mouth placing blocks into each
 * tunnel slice (feet then head), then refills the shaft from the bottom up
 * (pillar). Without refill: optional torch, then leaves (walk / dug
 * staircase / pillar).
 * Observer (T8): at the shaft rim behind the tunnel mouth (pit mode) or
 * behind / beside the mouth (hill mode), mostly looking at the digger.
 *
 * Logged changes: block_break, block_place, entity_spawn/remove (drops),
 * falling blocks (gravity ceilings), block_state.
 */

const { Vec3 } = require("vec3");

const dc = require("./diverse-common");
const { isGravityBlock } = require("../../utils/materials");
const sc = require("../state/state-common");

const { randInt, note, sleep } = dc;

const DIRS = [
  [1, 0],
  [-1, 0],
  [0, 1],
  [0, -1],
];

class TunnelEpisode extends dc.DiverseEpisodeBase {
  static INIT_MIN_BOTS_DISTANCE = 4;
  static INIT_MAX_BOTS_DISTANCE = 9;
  static WORKS_IN_NON_FLAT_WORLD = true;
  static SITE_HALF_SIZE = 3;

  /** Normal worlds: look for a hillside first (deterministic), else a flat site. */
  chooseSite(bot, args, mid) {
    if (!this.isFlatWorld(args)) {
      const hill = this.findHillside(bot, mid, 12);
      if (hill) return hill;
    }
    return super.chooseSite(bot, args, mid);
  }

  /**
   * Mouth stand (ground block) + direction d such that the 1x2 tunnel
   * cells of the next 4 columns (feet + head) and their ceiling are solid,
   * diggable, non-gravity blocks.
   */
  findHillside(bot, mid, radius) {
    const cx = Math.floor(mid.x);
    const cz = Math.floor(mid.z);
    const solidDiggable = (p) => {
      const b = bot.blockAt(p);
      return !!b && dc.isSolid(b) && !dc.isTreeBlock(b) && b.name !== "bedrock" && !b.name.includes("ore") && !isGravityBlock(b.name);
    };
    for (let r = 0; r <= radius; r++) {
      for (let dx = -r; dx <= r; dx++) {
        for (let dz = -r; dz <= r; dz++) {
          if (Math.max(Math.abs(dx), Math.abs(dz)) !== r) continue;
          const x = cx + dx;
          const z = cz + dz;
          const feet = dc.surfaceAt(bot, x, z, Math.floor(mid.y), { up: 8, down: 8 });
          if (!feet) continue;
          // room around the mouth for both bots
          if (!dc.standable(bot, x, feet.y, z)) continue;
          for (const [ddx, ddz] of DIRS) {
            let ok = true;
            let len = 0;
            for (let k = 1; k <= 8; k++) {
              const fx = x + ddx * k;
              const fz = z + ddz * k;
              const cells = [new Vec3(fx, feet.y, fz), new Vec3(fx, feet.y + 1, fz), new Vec3(fx, feet.y + 2, fz)];
              if (cells.every(solidDiggable)) len = k;
              else {
                if (k <= 4) ok = false;
                break;
              }
            }
            if (!ok || len < 4) continue;
            // standing room behind the mouth for the observer
            const bx = x - ddx * 3;
            const bz = z - ddz * 3;
            if (!dc.surfaceAt(bot, bx, bz, feet.y, { up: 3, down: 4 })) continue;
            // frame: tunnel runs towards -u  =>  u = -d
            const axis = ddx !== 0 ? "x" : "z";
            const side = ddx !== 0 ? -ddx : -ddz;
            return { site: new Vec3(x, feet.y - 1, z), prepared: false, commands: [], axis, side, hill: { dir: [ddx, ddz], maxLen: len } };
          }
        }
      }
    }
    return null;
  }

  planEpisode(bot, rng, args, frame, isActor, chosen) {
    let L = randInt(rng, 4, 8);
    const width = rng() < 0.3 ? 2 : 1;
    let shaftDepth = randInt(rng, 3, 4);
    const refill = rng() < 0.5;
    const uMat = rng();
    const torch = rng() < 0.5;
    const observerLeft = rng() < 0.5;
    const uExtra = rng();
    void uExtra;

    const hill = chosen && chosen.hill ? chosen.hill : null;
    const mode = hill ? "hill" : "pit";
    const g = frame.site.y;
    const refillMaterial = refill ? ["dirt", "cobblestone", "stone", "dirt", "gravel"][Math.min(4, Math.floor(uMat * 5))] : null;

    let shaftLayers = [];
    let floorDy;
    if (mode === "pit") {
      // ceiling of the tunnel (dy = -shaftDepth + 2) must not be a gravity block
      const ceilingGravity = (sd) => {
        for (let i = 1; i <= L; i++) {
          const b = bot.blockAt(frame.at(-i, 0, -sd + 2));
          if (b && isGravityBlock(b.name)) return true;
        }
        return false;
      };
      if (shaftDepth === 3 && ceilingGravity(3)) shaftDepth = 4;
      for (let k = 1; k <= shaftDepth; k++) {
        const cells = [];
        for (const du of [0, 1]) for (const dp of [0, 1]) cells.push(frame.at(du, dp, -k));
        shaftLayers.push(cells);
      }
      floorDy = -shaftDepth;
    } else {
      L = Math.min(L, hill.maxLen);
      floorDy = 0;
    }
    const dps = width === 2 ? [0, 1] : [0];
    // tunnel slices i = 1..L at du = -i (head block first, then feet)
    const slices = [];
    for (let i = 1; i <= L; i++) {
      const s = [];
      for (const dp of dps) {
        s.push({ pos: frame.at(-i, dp, floorDy + 1), part: "head" });
        s.push({ pos: frame.at(-i, dp, floorDy), part: "feet" });
      }
      slices.push(s);
    }
    const shaftCols = new Set();
    for (const c of shaftLayers[0] || []) shaftCols.add(dc.key2(c.x, c.z));
    // way out of the shaft (no refill): staircase cut into the +u wall
    const stairs = mode === "pit" ? { from: frame.at(1, 0, floorDy), dir: [frame.u.x, frame.u.z], steps: shaftDepth } : null;
    if (stairs) for (const k of dc.stairColumns(stairs.from, stairs.dir, stairs.steps)) shaftCols.add(k);
    const middle = frame.at(0, 0, 0);

    let actorStand;
    let observerStand;
    if (mode === "pit") {
      actorStand = frame.at(1, observerLeft ? 3 : -2);
      observerStand = frame.at(3, observerLeft ? -1 : 2);
    } else {
      actorStand = frame.at(1, 0);
      observerStand = frame.at(4, observerLeft ? 2 : -2);
    }
    const all = [...shaftLayers.flat(), ...slices.flat().map((s) => s.pos)];
    const xs = all.map((p) => p.x);
    const ys = all.map((p) => p.y);
    const zs = all.map((p) => p.z);
    const box = [Math.min(...xs), Math.min(...ys), Math.min(...zs), Math.max(...xs), Math.max(...ys), Math.max(...zs)];
    const leaderSetupCommands = [];
    if (!this.isFlatWorld(args)) {
      for (const fl of ["water", "lava"]) {
        leaderSetupCommands.push(`fill ${box[0] - 1} ${box[1] - 1} ${box[2] - 1} ${box[3] + 1} ${box[4] + 1} ${box[5] + 1} minecraft:stone replace minecraft:${fl}`);
      }
    }
    const volume = all.length;
    const actorItems = [["dirt", shaftDepth + 3]];
    if (refillMaterial) actorItems.push([refillMaterial, volume + 4]);
    if (torch && !refill) actorItems.push(["torch", 2]);

    return {
      actorStand,
      observerStand,
      items: { actor: actorItems, observer: [] },
      mode,
      L,
      width,
      shaftDepth: mode === "pit" ? shaftDepth : 0,
      shaftLayers,
      shaftCols,
      stairs,
      slices,
      middle,
      floorDy,
      refillMaterial,
      torch: torch && !refill,
      clearBox: { x1: box[0] - 4, z1: box[2] - 4, x2: box[3] + 4, z2: box[5] + 4, y1: frame.level, y2: frame.level + 2 },
      leaderSetupCommands,
      metadata: {
        tunnel: {
          mode,
          length: L,
          width,
          height: 2,
          direction: [-frame.u.x, -frame.u.z],
          floor_y: frame.level + floorDy,
          box,
          cells: slices.flat().map((s) => [s.pos.x, s.pos.y, s.pos.z]),
        },
        shaft: mode === "pit" ? { size: [2, 2], depth: shaftDepth, top_y: g, cols: shaftLayers[0].map((p) => [p.x, p.z]) } : null,
        refill,
        refill_material: refillMaterial,
        torch: torch && !refill,
        materials: refillMaterial ? { refill: refillMaterial } : {},
        observer_view: mode === "pit" ? "shaft_rim_behind_mouth" : "behind_mouth",
        observer_policy: "rim_ring_T8",
      },
    };
  }

  buildSteps(ctx) {
    const { bot, rcon, args, episodeNum, plan, frame } = ctx;
    const mouth = () => {
      const p = frame.at(-1, 0, plan.floorDy + 1);
      return new Vec3(p.x + 0.5, p.y, p.z + 0.5);
    };
    const avoidShaft = (x, z) => plan.shaftCols.has(dc.key2(x, z));
    const observe = () => ({
      focus: () => {
        const e = bot.players[ctx.otherName]?.entity;
        return e ? e.position.offset(0, 1, 0) : mouth();
      },
      avoid: avoidShaft,
      minR: 3,
      maxR: 7,
      actorProb: 0.75,
      moveProb: 0.35,
    });
    const rimFeet = () => dc.surfaceAt(bot, plan.actorStand.x, plan.actorStand.z, frame.level) || plan.actorStand;
    const tunnelFeet = (i) => frame.at(-i, 0, plan.floorDy);

    const digTunnel = async () => {
      const deadline = Date.now() + 75000;
      await note(rcon, args, bot, episodeNum, "tunnel_start", { mode: plan.mode });
      if (plan.mode === "pit") {
        const r = await dc.digPitInPlace(ctx, plan.shaftLayers, plan.middle, { deadline: Date.now() + 35000, level: frame.level, label: "shaft_layer" });
        await note(rcon, args, bot, episodeNum, "shaft_done", { dug: r.dug });
      } else {
        await dc.moveTo(bot, frame.at(0, 0, 0), { timeoutMs: 6000 });
      }
      let dug = 0;
      let reached = 0;
      for (let i = 1; i <= plan.L; i++) {
        if (Date.now() > deadline || bot._episodeStopping) break;
        for (const cell of plan.slices[i - 1]) {
          if (await dc.digBlockAt(bot, cell.pos, { otherName: ctx.otherName, only: (x, z) => false })) dug++;
          else if (await dc.digBlockAt(bot, cell.pos, { otherName: ctx.otherName })) dug++;
          await sleep(dc.rand(60, 160));
        }
        const f = tunnelFeet(i);
        if (await dc.moveTo(bot, f, { timeoutMs: 3000 })) reached = i;
        await dc.glance(ctx, 0.5, [500, 900]);
      }
      if (plan.torch) {
        const ti = Math.max(1, Math.ceil(reached / 2));
        const tpos = tunnelFeet(ti);
        if (await dc.ensureReach(bot, tpos, { otherName: ctx.otherName })) {
          const ok = await sc.placeOnTop(bot, tpos, "torch");
          await note(rcon, args, bot, episodeNum, "torch", { pos: tpos, ok });
        }
      }
      // turn around: look back out of the tunnel
      await dc.lookAtPoint(bot, mouth().offset(0, 0.8, 0), 120);
      await dc.glance(ctx, 0.9, [800, 1300]);
      await note(rcon, args, bot, episodeNum, "tunnel_done", { dug, reached });
      return { reached };
    };

    const finish = async () => {
      if (plan.refillMaterial) {
        const deadline = Date.now() + 55000;
        let placed = 0;
        // walk back slice by slice, filling the slice behind
        for (let i = plan.L; i >= 1; i--) {
          if (Date.now() > deadline || bot._episodeStopping) break;
          const standFeet = i - 1 >= 1 ? tunnelFeet(i - 1) : plan.mode === "pit" ? frame.at(0, 0, plan.floorDy) : frame.at(0, 0, 0);
          await dc.moveTo(bot, standFeet, { timeoutMs: 3500 });
          const cells = plan.slices[i - 1]
            .slice()
            .sort((a, b) => (a.part === "feet" ? 0 : 1) - (b.part === "feet" ? 0 : 1))
            .map((c) => ({ pos: c.pos, material: plan.refillMaterial }));
          const res = await dc.buildPlacements(ctx, cells, { deadline, glanceProb: 0.3 });
          placed += res.placed.length;
        }
        await note(rcon, args, bot, episodeNum, "tunnel_refilled", { placed });
        if (plan.mode === "pit") {
          await dc.moveTo(bot, frame.at(0, 0, plan.floorDy), { timeoutMs: 3000 });
          const r = await dc.refillPitInPlace(ctx, plan.shaftLayers, plan.middle, plan.refillMaterial, { deadline: deadline + 10000 });
          placed += r.placed;
          await note(rcon, args, bot, episodeNum, "shaft_refilled", { placed: r.placed });
        }
        await dc.moveTo(bot, rimFeet(), { timeoutMs: 5000 });
        return { placed };
      }
      if (plan.mode === "pit") {
        await dc.moveTo(bot, frame.at(0, 0, plan.floorDy), { timeoutMs: 6000 });
        const method = await dc.exitPit(ctx, rimFeet(), { towerMaterial: "dirt", rimY: frame.level, avoid: null, stairs: plan.stairs });
        await note(rcon, args, bot, episodeNum, "exit_shaft", { method });
        return { exit: method };
      }
      await dc.moveTo(bot, rimFeet(), { timeoutMs: 8000 });
      return {};
    };

    return dc.bindSteps(ctx, [
      dc.approachStep(),
      dc.dualStep("dig", digTunnel, null, { maxMs: 85000, observe }),
      dc.lookStep("pause", [1000, 1800], () => mouth()),
      dc.dualStep("finish", finish, null, { maxMs: 70000, observe }),
      dc.lookStep("admire", [1500, 2500], () => mouth()),
      dc.outroStep(),
    ]);
  }
}

module.exports = { TunnelEpisode };
