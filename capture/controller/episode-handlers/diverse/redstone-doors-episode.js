/**
 * redstoneDoors - doors, trapdoors, fence gates, levers, buttons, pressure
 * plates, redstone lamps, pistons, note blocks and bells.
 *
 * Layout: four "stations" on the line u=0 between the bots, at p = -4, -2,
 * +2, +4 (the centre column p=0 stays free so the bots see each other). The
 * actor's home is (u=+3, p=0), the partner's (u=-3, p=0). Stations are
 * operated alternately by the actor and the partner (operators walk up to
 * (u=+-2, p) on their own side when a station is out of reach); the other
 * bot watches the station and the operator.
 *
 * Station mix (shared RNG): two redstone stations, one manual station and
 * one station whose block the operator places during the episode.
 *   redstone: lamp_lever (lever on a redstone lamp), lamp_button (button on
 *             a lamp), plate_lamp (pressure plate on a lamp sunk into the
 *             ground; the operator walks over it), iron_door (iron door or
 *             iron trapdoor driven by a floor lever / button next to it),
 *             piston (piston / sticky piston facing up with a block on top,
 *             floor lever next to it), note_block (instrument block below,
 *             played / tuned by right clicks)
 *   manual:   wood_door, wood_trapdoor, fence_gate (between two fence posts),
 *             bell
 *   placed:   place_door, place_trapdoor, place_gate (placed by the
 *             operator, then opened / closed)
 * Pre-built station blocks are set with /setblock in setup (before the start
 * snapshot).
 *
 * Logging / replay: hand interactions (door, trapdoor, gate, lever, button,
 * note block) are read back by the logger after player_interact. Redstone
 * consequences that have no logged event (lamp lit/unlit, iron door open,
 * button release, plate release, piston head / moved block after the
 * movement finished) are made visible with an empty-hand right click on a
 * block without a use action ("inspect": lamp, iron door, pressure plate,
 * piston, moved block) after the circuit settled, plus a final sweep.
 * See windows/schema_parts/r2-mechanics.md.
 */

const {
  FACES,
  MechanicsEpisodeBase,
  blockName,
  clickBlock,
  glance,
  inspectBlocks,
  introStep,
  localJitterMs,
  observeUntilSignal,
  outroStep,
  pick,
  placeItemAt,
  propsOf,
  shuffle,
  signalDone,
  sleep,
  toArr,
  walkTo,
} = require("./mechanics-common");

const WOODS = ["oak", "spruce", "birch", "jungle", "acacia", "dark_oak", "mangrove", "cherry", "bamboo", "crimson", "warped"];
const BUTTONS = ["stone_button", "polished_blackstone_button", "oak_button", "birch_button", "cherry_button", "warped_button"];
const PLATES = [
  "stone_pressure_plate",
  "oak_pressure_plate",
  "light_weighted_pressure_plate",
  "heavy_weighted_pressure_plate",
  "polished_blackstone_pressure_plate",
  "spruce_pressure_plate",
];
const PISTON_TOPS = ["white_wool", "oak_planks", "hay_block", "pumpkin", "cobblestone", "slime_block", "glass", "honey_block"];
const INSTRUMENTS = [
  ["gold_block", "bell"],
  ["clay", "flute"],
  ["packed_ice", "chime"],
  ["bone_block", "xylophone"],
  ["white_wool", "guitar"],
  ["sand", "snare"],
  ["glass", "hat"],
  ["stone", "basedrum"],
  ["oak_planks", "bass"],
  ["iron_block", "iron_xylophone"],
  ["soul_sand", "cow_bell"],
  ["emerald_block", "bit"],
  ["hay_block", "banjo"],
  ["glowstone", "pling"],
];
const REDSTONE_TYPES = ["lamp_lever", "lamp_button", "plate_lamp", "iron_door", "piston", "note_block"];
const MANUAL_TYPES = ["wood_door", "wood_trapdoor", "fence_gate", "bell"];
const PLACED_TYPES = ["place_door", "place_trapdoor", "place_gate"];
const SLOTS = [-4, -2, 2, 4];

function dirName(v) {
  if (v.x > 0) return "east";
  if (v.x < 0) return "west";
  if (v.z > 0) return "south";
  return "north";
}

function bs(p) {
  return `${p.x} ${p.y} ${p.z}`;
}

class RedstoneDoorsEpisode extends MechanicsEpisodeBase {
  static INIT_MIN_BOTS_DISTANCE = 5;
  static INIT_MAX_BOTS_DISTANCE = 9;
  static WORKS_IN_NON_FLAT_WORLD = true;
  static SITE_HALF_SIZE = 5;
  static CLEAR_HEIGHT = 4;
  static STEP_TIMEOUT_MS = 26000;
  // lit lamps read better in low light
  static TIME_PREFERENCE = { dusk: 0.15, night: 0.1 };

  planEpisode(bot, rng, args, frame, isActor) {
    const r1 = pick(rng, REDSTONE_TYPES);
    const r2 = pick(rng, REDSTONE_TYPES.filter((t) => t !== r1));
    const m = pick(rng, MANUAL_TYPES);
    const pl = pick(rng, PLACED_TYPES);
    let types = shuffle(rng, [r1, r2, m, pl]);
    // test hook (same env on both bots; the rng was consumed as usual):
    // MECH_FORCE_STATIONS=plate_lamp,iron_door,fence_gate,place_gate
    const forced = String(process.env.MECH_FORCE_STATIONS || "")
      .split(",")
      .map((t) => t.trim())
      .filter((t) => [...REDSTONE_TYPES, ...MANUAL_TYPES, ...PLACED_TYPES].includes(t));
    if (forced.length === 4) types = forced;
    const firstOp = rng() < 0.5 ? "actor" : "observer";
    const uDir = dirName(frame.u);
    const uBack = dirName(frame.u.scaled(-1));
    const pDir = dirName(frame.p);

    const stations = [];
    const setup = [];
    const items = { actor: [], observer: [] };
    types.forEach((type, i) => {
      // every station draws the same number of rng values (5) => stable plans
      const r = [rng(), rng(), rng(), rng(), rng()];
      const dp = SLOTS[i];
      const op = i % 2 === 0 ? firstOp : firstOp === "actor" ? "observer" : "actor";
      const opU = op === "actor" ? 1 : -1;
      const at = (du, dy) => frame.at(du, dp, dy);
      const st = { i, type, dp, op, opU, pos: at(0, 0), inspect: [], params: {} };
      const wood = WOODS[Math.floor(r[0] * WOODS.length)];
      const pickR = (arr, u) => arr[Math.floor(u * arr.length)];
      switch (type) {
        case "lamp_lever": {
          st.params = { lamp: toArr(at(0, 0)), lever: toArr(at(0, 1)), toggles: 2 + Math.floor(r[1] * 2) };
          setup.push(`setblock ${bs(at(0, 0))} minecraft:redstone_lamp`);
          setup.push(`setblock ${bs(at(0, 1))} minecraft:lever[face=floor,facing=${uDir}]`);
          st.inspect = [at(0, 0)];
          break;
        }
        case "lamp_button": {
          const button = pickR(BUTTONS, r[1]);
          st.params = { lamp: toArr(at(0, 0)), button, presses: 1 + Math.floor(r[2] * 2) };
          setup.push(`setblock ${bs(at(0, 0))} minecraft:redstone_lamp`);
          setup.push(`setblock ${bs(at(0, 1))} minecraft:${button}[face=floor,facing=${uDir}]`);
          st.inspect = [at(0, 0)];
          break;
        }
        case "plate_lamp": {
          const plate = pickR(PLATES, r[1]);
          st.params = { lamp: toArr(at(0, -1)), plate, plate_pos: toArr(at(0, 0)) };
          setup.push(`setblock ${bs(at(0, -1))} minecraft:redstone_lamp`);
          setup.push(`setblock ${bs(at(0, 0))} minecraft:${plate}`);
          st.inspect = [at(0, 0)];
          break;
        }
        case "iron_door": {
          const trap = r[1] < 0.35;
          const useLever = r[2] < 0.55;
          const trigger = useLever ? "lever" : pickR(BUTTONS, r[3]);
          const facing = r[4] < 0.5 ? uDir : uBack;
          st.params = { block: trap ? "iron_trapdoor" : "iron_door", trigger, trigger_pos: toArr(at(opU, 0)), facing, toggles: useLever ? 1 + Math.floor(r[3] * 2) : 1 };
          if (trap) {
            setup.push(`setblock ${bs(at(0, 0))} minecraft:iron_trapdoor[facing=${facing},half=bottom]`);
          } else {
            setup.push(`setblock ${bs(at(0, 0))} minecraft:iron_door[half=lower,facing=${facing},hinge=left]`);
            setup.push(`setblock ${bs(at(0, 1))} minecraft:iron_door[half=upper,facing=${facing},hinge=left]`);
          }
          setup.push(`setblock ${bs(at(opU, 0))} minecraft:${trigger}[face=floor,facing=${uDir}]`);
          st.inspect = [at(0, 0)];
          break;
        }
        case "piston": {
          const sticky = r[1] < 0.5;
          const top = pickR(PISTON_TOPS, r[2]);
          st.params = { piston: sticky ? "sticky_piston" : "piston", top_block: top, lever_pos: toArr(at(opU, 0)), cycles: 1 + Math.floor(r[3] * 2) };
          setup.push(`setblock ${bs(at(0, 0))} minecraft:${sticky ? "sticky_piston" : "piston"}[facing=up]`);
          setup.push(`setblock ${bs(at(0, 1))} minecraft:${top}`);
          setup.push(`setblock ${bs(at(opU, 0))} minecraft:lever[face=floor,facing=${uDir}]`);
          st.inspect = [at(0, 0), at(0, 2), at(0, 1)];
          break;
        }
        case "note_block": {
          const [inst, instrument] = pickR(INSTRUMENTS, r[1]);
          st.params = { instrument_block: inst, instrument, clicks: 2 + Math.floor(r[2] * 4) };
          // note block first, then the block below (its update sets the instrument)
          setup.push(`setblock ${bs(at(0, 0))} minecraft:note_block`);
          setup.push(`setblock ${bs(at(0, -1))} minecraft:${inst}`);
          break;
        }
        case "wood_door": {
          const facing = r[1] < 0.5 ? uDir : uBack;
          const hinge = r[2] < 0.5 ? "left" : "right";
          st.params = { block: `${wood}_door`, facing, hinge, toggles: 2 + Math.floor(r[3] * 2) };
          setup.push(`setblock ${bs(at(0, 0))} minecraft:${wood}_door[half=lower,facing=${facing},hinge=${hinge}]`);
          setup.push(`setblock ${bs(at(0, 1))} minecraft:${wood}_door[half=upper,facing=${facing},hinge=${hinge}]`);
          break;
        }
        case "wood_trapdoor": {
          const half = r[1] < 0.6 ? "bottom" : "top";
          st.params = { block: `${wood}_trapdoor`, half, toggles: 2 + Math.floor(r[2] * 2) };
          setup.push(`setblock ${bs(at(0, 0))} minecraft:${wood}_trapdoor[facing=${uDir},half=${half}]`);
          break;
        }
        case "fence_gate": {
          st.params = { block: `${wood}_fence_gate`, fence: `${wood}_fence`, toggles: 2 + Math.floor(r[1] * 2) };
          // posts along u, gate in between (facing along p)
          setup.push(`setblock ${bs(at(1, 0))} minecraft:${wood}_fence`);
          setup.push(`setblock ${bs(at(-1, 0))} minecraft:${wood}_fence`);
          setup.push(`setblock ${bs(at(0, 0))} minecraft:${wood}_fence_gate[facing=${pDir}]`);
          break;
        }
        case "bell": {
          st.params = { block: "bell", rings: 1 + Math.floor(r[1] * 2) };
          setup.push(`setblock ${bs(at(0, 0))} minecraft:bell[attachment=floor,facing=${uDir}]`);
          break;
        }
        case "place_door":
        case "place_trapdoor":
        case "place_gate": {
          const item = type === "place_door" ? `${wood}_door` : type === "place_trapdoor" ? `${wood}_trapdoor` : `${wood}_fence_gate`;
          st.params = { item, toggles: 1 + Math.floor(r[1] * 2) };
          items[op].push([item, 2]);
          break;
        }
        default:
          break;
      }
      stations.push(st);
    });

    return {
      actorStand: frame.at(3, 0),
      observerStand: frame.at(-3, 0),
      items,
      leaderSetupCommands: setup,
      stations,
      metadata: {
        stations: stations.map((s) => ({
          index: s.i,
          type: s.type,
          pos: toArr(s.pos),
          operator: s.op,
          ...s.params,
        })),
        readback_inspection: true,
      },
    };
  }

  buildSteps(ctx) {
    const { bot, plan, isActor, frame } = ctx;
    const home = isActor ? plan.actorStand : plan.observerStand;
    const focusOf = (st) => () => st.pos.offset(0.5, 0.8, 0.5);

    const reach = async (st) => {
      const p = bot.entity.position;
      const dx = p.x - (st.pos.x + 0.5);
      const dz = p.z - (st.pos.z + 0.5);
      if (Math.sqrt(dx * dx + dz * dz) > 3.4) {
        await walkTo(bot, frame.at(st.opU * 2, st.dp), 0.7, 6000);
      }
    };
    const toggle = async (pos, times, gapLo = 800, gapHi = 1300) => {
      let n = 0;
      for (let t = 0; t < times; t++) {
        if (ctx.timeUp()) break;
        if (await clickBlock(bot, pos, { item: null })) n++;
        await sleep(localJitterMs(gapLo, gapHi));
      }
      return n;
    };

    const operate = async (st) => {
      const at = (du, dy) => frame.at(du, st.dp, dy);
      const res = { type: st.type };
      await reach(st);
      switch (st.type) {
        case "lamp_lever":
          res.toggles = await toggle(at(0, 1), st.params.toggles, 900, 1500);
          break;
        case "lamp_button":
          for (let k = 0; k < st.params.presses && !ctx.timeUp(); k++) {
            await clickBlock(bot, at(0, 1), { item: null });
            await sleep(localJitterMs(2300, 2700)); // button (20-30 ticks) + lamp delay (4)
          }
          res.presses = st.params.presses;
          break;
        case "plate_lamp": {
          const back = bot.entity.position.clone();
          await walkTo(bot, at(0, 0), 0.25, 6000);
          await sleep(localJitterMs(1200, 1800));
          res.stepped = blockName(bot, at(0, 0));
          await walkTo(bot, back, 0.6, 6000);
          await glance(ctx, localJitterMs(700, 1100));
          await sleep(1200); // plate release (10-20 ticks) + lamp delay
          break;
        }
        case "iron_door": {
          // the trigger is next to the door, so the click readback does not
          // cover the door: inspect it while it is open, too
          const trig = at(st.opU, 0);
          const door = at(0, 0);
          res.toggles = 0;
          res.states = [];
          const n = st.params.trigger === "lever" ? st.params.toggles : 1;
          for (let k = 0; k < n && !ctx.timeUp(); k++) {
            await clickBlock(bot, trig, { item: null });
            res.toggles++;
            await sleep(350);
            await inspectBlocks(ctx, [door]);
            res.states.push(String(propsOf(bot.blockAt(door)).open));
            await sleep(localJitterMs(900, 1400));
          }
          if (st.params.trigger !== "lever") await sleep(localJitterMs(1300, 1700)); // button release
          break;
        }
        case "piston": {
          const lever = at(st.opU, 0);
          const n = st.params.cycles * 2 - (Math.random() < 0.3 ? 1 : 0); // may end extended
          res.toggles = await toggle(lever, Math.max(1, n), 1200, 1800);
          await sleep(500);
          break;
        }
        case "note_block":
          res.clicks = await toggle(at(0, 0), st.params.clicks, 350, 650);
          break;
        case "wood_door":
        case "wood_trapdoor":
        case "fence_gate":
          res.toggles = await toggle(at(0, 0), st.params.toggles, 700, 1200);
          break;
        case "bell":
          res.rings = await toggle(at(0, 0), st.params.rings, 900, 1400);
          break;
        case "place_door":
        case "place_trapdoor":
        case "place_gate": {
          const target = at(0, 0);
          const ok = await placeItemAt(bot, target, st.params.item, {
            face: FACES.up,
            refPos: at(0, -1),
            expect: [st.params.item],
          });
          res.placed = ok;
          res.block = blockName(bot, target);
          await sleep(400);
          if (ok) res.toggles = await toggle(target, st.params.toggles + 1, 700, 1200);
          break;
        }
        default:
          break;
      }
      if (st.inspect.length) {
        await sleep(400);
        res.inspected = await inspectBlocks(ctx, st.inspect);
      }
      await ctx.note(`station_${st.i}_${st.type}`, { pos: st.pos, ...res });
      await glance(ctx, localJitterMs(500, 900));
      return res;
    };

    const stationSteps = plan.stations.map((st) => ({
      name: `st${st.i}_${st.type}`,
      timeoutMs: 26000,
      run: async function () {
        const amOp = (st.op === "actor") === isActor;
        if (!amOp) {
          await observeUntilSignal(this, focusOf(st), `st${st.i}`, 23000, { avoid: [st.pos] });
          return {};
        }
        let res = {};
        try {
          res = await operate(st);
        } finally {
          signalDone(this, `st${st.i}`);
        }
        return res;
      },
    }));

    return [
      introStep(),
      ...stationSteps,
      {
        name: "final_check",
        timeoutMs: 24000,
        run: async function () {
          // operators re-inspect their redstone stations (logger readback of
          // anything that settled late), then return home
          for (const st of plan.stations) {
            if (this.timeUp()) break;
            if (!st.inspect.length || (st.op === "actor") !== isActor) continue;
            await reach(st);
            await inspectBlocks(this, st.inspect);
          }
          await walkTo(bot, home, 0.7, 6000);
          await glance(this, localJitterMs(800, 1200));
          return {};
        },
      },
      outroStep(),
    ];
  }
}

module.exports = { RedstoneDoorsEpisode };
