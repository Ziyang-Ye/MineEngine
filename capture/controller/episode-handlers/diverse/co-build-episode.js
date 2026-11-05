/**
 * coBuild - both bots build ONE small house from a shared blueprint with
 * split roles: the "walls" builder (actor) raises the corner posts and the
 * walls from outside, the "interior" builder (other bot) lays the floor
 * (replacing the ground inside), goes in through the door gap, sets the
 * windows and a torch, builds the roof from the inside, walks out and
 * (50%) hangs a door in the gap. They work side by side, so each is often in
 * the other's view.
 *
 * Plan (shared RNG): palette (materials.js: walls, pillar, window, roof,
 * floor, door), footprint W x D in 4..7 (area <= 35), wall height 3, door
 * side, 2-4 windows, roof style (flat / rim of logs), door (50%), torch (50%).
 * Non-full blocks (door, torch) stay far below 10% of the placed blocks.
 *
 * Logged changes: block_break (floor cells dug), block_place (every part;
 * the door as a two-block multi place), item drops / pickups, block_state.
 */

const { Vec3 } = require("vec3");

const dc = require("./diverse-common");
const sc = require("../state/state-common");
const { countMaterials, nonFullFraction, paletteMetadata, pickPalette } = require("../../utils/materials");

const { randInt, note, sleep } = dc;

const SIDES = ["u+", "u-", "p+", "p-"];

class CoBuildEpisode extends dc.DiverseEpisodeBase {
  static INIT_MIN_BOTS_DISTANCE = 4;
  static INIT_MAX_BOTS_DISTANCE = 10;
  static WORKS_IN_NON_FLAT_WORLD = true;
  static SITE_HALF_SIZE = 4;
  static SITE_CLEAR_HEIGHT = 5;

  planEpisode(bot, rng, args, frame, isActor) {
    const palette = pickPalette(rng);
    let W = randInt(rng, 4, 7);
    let D = randInt(rng, 4, 7);
    const doorSide = SIDES[Math.min(3, Math.floor(rng() * 4))];
    const windowCount = randInt(rng, 2, 4);
    const windowSeed = rng();
    const roofStyle = rng() < 0.4 ? "rim" : "flat";
    const door = rng() < 0.5;
    const torch = rng() < 0.5;
    while (W * D > 35) {
      if (W >= D) W--;
      else D--;
    }
    const H = 3;
    const house = dc.makeHouse(frame, {
      width: W,
      depth: D,
      height: H,
      palette,
      floor: true,
      pillars: true,
      doorSide,
      windowCount,
      windowSeed,
      roofStyle,
      door,
      torches: torch ? 1 : 0,
    });
    const parts = (name) => house.placements.filter((p) => p.part === name);
    const pillars = parts("pillar");
    const walls = parts("wall");
    const windows = parts("window");
    const roof = parts("roof");
    const doors = parts("door");
    const torches = parts("torch");
    const floor = house.floorCells;
    const all = [...floor, ...house.placements];
    const wallsBuilder = [...pillars, ...walls];
    const interiorBuilder = [...floor, ...windows, ...roof, ...doors, ...torches];
    const toItems = (list) => Object.entries(countMaterials(list)).map(([m, c]) => [m, c + 3]);

    // stands: walls builder outside the door-less side, interior builder outside the opposite side
    const actorStand = frame.at(house.u1 + 2, 0);
    const observerStand = frame.at(house.u0 - 2, 0);
    const used = ["walls", "pillar", "floor", "roof", ...(windows.length ? ["window"] : []), ...(door ? ["door"] : [])];
    return {
      actorStand,
      observerStand,
      items: { actor: toItems(wallsBuilder), observer: toItems(interiorBuilder) },
      house,
      pillars,
      walls,
      windows,
      roof,
      doors,
      torches,
      floor,
      clearBox: { x1: frame.site.x - 6, z1: frame.site.z - 6, x2: frame.site.x + 6, z2: frame.site.z + 6, y1: frame.level, y2: frame.level + 4 },
      metadata: {
        house: {
          width: W,
          depth: D,
          wall_height: H,
          door_side: doorSide,
          door_gap: [house.door.x, house.door.y, house.door.z],
          door_placed: door,
          windows: house.windows.map((p) => [p.x, p.y, p.z]),
          roof_style: roofStyle,
          floor: "interior ground replaced",
          torches: torches.map((t) => [t.pos.x, t.pos.y, t.pos.z]),
          blueprint: dc.placementsMetadata(all),
        },
        materials: paletteMetadata(palette, used),
        material_counts: countMaterials(all),
        non_full_fraction: nonFullFraction(all),
        roles: {
          walls: isActor ? bot.username : args.other_bot_name,
          interior: isActor ? args.other_bot_name : bot.username,
        },
        observer_policy: "co-workers; idle bot watches the other (T8)",
      },
    };
  }

  buildSteps(ctx) {
    const { bot, rcon, args, episodeNum, plan, frame } = ctx;
    const h = plan.house;
    const houseCenter = () => h.center;
    const inHouse = (x, z) => h.inFootprint(x, z);
    const inInterior = (x, z) => h.inInterior(x, z);
    const isCornerCol = (x, z) => plan.pillars.some((p) => p.pos.x === x && p.pos.z === z);
    const observeOut = () => ({ focus: houseCenter, avoid: inHouse, minR: Math.max(h.W, h.D) / 2 + 2, maxR: Math.max(h.W, h.D) / 2 + 6 });
    const outside = { forbid: (x, z) => inHouse(x, z), otherName: ctx.otherName };
    const inside = { only: (x, z) => inInterior(x, z), otherName: ctx.otherName, maxH: 3.2 };
    const door = h.door;

    // ---- walls builder ---------------------------------------------------
    const buildPillars = async () => {
      const list = plan.pillars.slice().sort((a, b) => a.pos.y - b.pos.y);
      const res = await dc.buildPlacements(ctx, dc.startNearest(bot, list).sort((a, b) => a.pos.y - b.pos.y), {
        deadline: Date.now() + 30000,
        standOpts: outside,
      });
      await note(rcon, args, bot, episodeNum, "pillars", { placed: res.placed.length, failed: res.failed.length });
      return {};
    };
    const buildWalls = async () => {
      const deadline = Date.now() + 60000;
      let placed = 0;
      for (const dy of [0, 1, 2]) {
        const row = dc.startNearest(bot, plan.walls.filter((p) => p.local[2] === dy));
        const res = await dc.buildPlacements(ctx, row, { deadline, standOpts: outside });
        placed += res.placed.length;
        if (dy === 0) ctx.signal("walls_row0");
      }
      await note(rcon, args, bot, episodeNum, "walls", { placed, total: plan.walls.length });
      return {};
    };

    // ---- interior builder ------------------------------------------------
    const layFloor = async () => {
      const deadline = Date.now() + 45000;
      const forbid = (x, z) => inInterior(x, z) || isCornerCol(x, z);
      const cells = dc.startNearest(bot, plan.floor);
      let done = 0;
      for (const c of cells) {
        if (Date.now() > deadline || bot._episodeStopping) break;
        await dc.digBlockAt(bot, c.pos, { forbid, otherName: ctx.otherName });
        if (await dc.placeBlockAt(bot, c.pos, c.material, { forbid, otherName: ctx.otherName })) done++;
        await dc.glance(ctx, 0.35);
      }
      await note(rcon, args, bot, episodeNum, "floor", { placed: done, total: plan.floor.length });
      return {};
    };
    const goInside = async () => {
      const inFeet = new Vec3(h.doorInside.x, h.doorInside.y, h.doorInside.z);
      const ok = await dc.moveTo(bot, inFeet, { timeoutMs: 9000 });
      return ok;
    };
    const windowsAndTorch = async () => {
      await goInside();
      // wait (watching the walls go up) until the first wall row is done
      await dc.observeUntil(ctx, { until: () => ctx.signals.has("walls_row0"), maxMs: 35000, canMove: false, focus: houseCenter });
      const res = await dc.buildPlacements(ctx, plan.windows, { deadline: Date.now() + 20000, standOpts: inside });
      let torches = 0;
      for (const t of plan.torches) {
        if (await dc.ensureReach(bot, t.pos, inside)) {
          if (await sc.placeOnTop(bot, t.pos, "torch")) torches++;
        }
      }
      await note(rcon, args, bot, episodeNum, "windows", { placed: res.placed.length, torches });
      // stay inside, watching through the gaps
      await dc.observeUntil(ctx, { until: () => ctx.signals.has("walls_done"), maxMs: 40000, canMove: false, focus: houseCenter });
      return {};
    };
    const buildRoof = async () => {
      if (!h.inInterior(Math.floor(bot.entity.position.x), Math.floor(bot.entity.position.z))) await goInside();
      const res = await dc.buildPlacements(ctx, plan.roof, { deadline: Date.now() + 50000, standOpts: inside, glanceProb: 0.3 });
      await note(rcon, args, bot, episodeNum, "roof", { placed: res.placed.length, failed: res.failed.length });
      return {};
    };
    const exitAndDoor = async () => {
      const out = new Vec3(h.doorOutside.x, h.doorOutside.y, h.doorOutside.z);
      const outDir = out.minus(door);
      const away = dc.surfaceAt(bot, out.x + outDir.x, out.z + outDir.z, frame.level) || out;
      await dc.moveTo(bot, away, { timeoutMs: 9000 });
      let doorOk = null;
      if (plan.doors.length) {
        const d = plan.doors[0];
        await dc.lookAtPoint(bot, dc.blockCenter(door), 120);
        doorOk = await dc.placeBlockAt(bot, d.pos, d.material, { expect: d.material, forbid: (x, z) => inHouse(x, z), otherName: ctx.otherName });
      }
      await note(rcon, args, bot, episodeNum, "exit", { door: doorOk });
      return {};
    };
    const toVantage = async () => {
      const v = dc.pickVantage(bot, houseCenter(), { minR: Math.max(h.W, h.D) / 2 + 3, maxR: Math.max(h.W, h.D) / 2 + 6, avoid: inHouse, otherName: ctx.otherName, near: bot.entity.position });
      if (v) await dc.moveTo(bot, v, { timeoutMs: 6000, avoid: inHouse });
      return {};
    };

    return dc.bindSteps(ctx, [
      dc.greetStep(),
      dc.dualStep("foundation", buildPillars, layFloor, { maxMs: 55000, observe: observeOut }),
      // walls (actor) while the interior builder goes in and sets windows
      dc.dualStep("walls", buildWalls, windowsAndTorch, { maxMs: 80000, observe: observeOut }),
      dc.dualStep("roof", null, buildRoof, { maxMs: 60000, observe: observeOut }),
      dc.dualStep("exit", null, exitAndDoor, { maxMs: 25000, observe: observeOut }),
      dc.dualStep("admire_walk", toVantage, toVantage, { maxMs: 12000, observe: observeOut }),
      dc.lookStep("admire", [2000, 3000], houseCenter),
      dc.outroStep(),
    ]);
  }
}

module.exports = { CoBuildEpisode };
