/**
 * diverse-common.js - Shared helpers of the build & destroy episodes
 * (coBuild, buildVsBreak, digDown, tunnel, crater, tntDemolish, forestBuild).
 *
 * Same rules as state/state-common.js (which this module builds on):
 *  - every /fill or /gamerule command runs in setupEpisode (leader only),
 *    i.e. BEFORE the start snapshot; afterwards the world only changes by the
 *    bots' own place / break / use actions and by game mechanics;
 *  - both bots consume sharedBotRng identically (pickers draw a fixed number
 *    of values); Math.random is only used for local timing / gaze jitter /
 *    observer vantage points, never for anything the other bot must know;
 *  - every wait has a timeout; helpers never throw (they return false).
 *
 * Additions over state-common:
 *  - reach-aware placeBlockAt / digBlockAt that pick a stand spot, walk there
 *    with a no-dig / no-scaffold pathfinder and then act;
 *  - a T8 observer loop (observeUntil): keeps 3-20 blocks, looks mostly at
 *    the other player (else the work site), occasionally changes vantage;
 *  - actor glances at the observer (glance) so both views see the partner;
 *  - a signal channel between the bots (ctx.signal / ctx.signals) so the
 *    observer watches exactly until the worker reports "done";
 *  - a house blueprint (makeHouse) shared by coBuild / tntDemolish /
 *    forestBuild, and buildPlacements (bottom-up, ring order, retry pass);
 *  - site handling for normal worlds: flat-site search, else a levelled pad
 *    made with /fill before the snapshot (state_metadata.site_prepared).
 */

const { Vec3 } = require("vec3");
const {
  goals: { GoalBlock, GoalNear },
} = require("mineflayer-pathfinder");

const sc = require("../state/state-common");
const { buildTowerUnderneath } = require("../../primitives/building");
const { ensureItemInHand, unequipHand } = require("../../primitives/items");
const {
  gotoWithTimeout,
  initializePathfinder,
  stopAll,
} = require("../../primitives/movement");
const { decidePrimaryBot, rconTp } = require("../../utils/coordination");
const { sleep } = require("../../utils/helpers");
const { isWorldStateLeader } = require("../../utils/world-state");
const { giveMaterials, isGravityBlock } = require("../../utils/materials");
const { exchange, reviveVec3, toPlain } = require("../../utils/handshake");

// Reach limits measured from the eye to the block centre (the server allows
// 4.5 + 1 to the block's bounding box).
const REACH = 4.3;
const REACH_HIGH = 5.0;
// head turn speed while working (deg/s; the gaze / observer looks stay slower)
const WORK_LOOK_DPS = 260;

// ----------------------------------------------------------------------------
// Block classification
// ----------------------------------------------------------------------------

function isAirName(name) {
  return name === "air" || name === "cave_air" || name === "void_air";
}

function isLiquidName(name) {
  return name === "water" || name === "lava" || name === "bubble_column";
}

/** Can a player stand in / walk through this block? */
function isPassable(block) {
  if (!block) return false;
  if (isAirName(block.name)) return true;
  if (isLiquidName(block.name)) return false;
  return block.boundingBox === "empty";
}

/** Full solid block a player can stand on (leaves only if allowed). */
function isSolid(block, { allowLeaves = false } = {}) {
  if (!block || block.boundingBox !== "block") return false;
  if (!allowLeaves && block.name.includes("leaves")) return false;
  if (block.name.includes("water") || block.name.includes("lava")) return false;
  if (block.name === "snow") return false;
  return true;
}

function isTreeBlock(block) {
  return !!block && (block.name.endsWith("_log") || block.name.endsWith("_leaves") ||
    block.name.endsWith("_wood") || block.name.includes("mushroom_block"));
}

// Blocks that open a GUI / toggle when right-clicked: never place against them.
const INTERACTABLE_RE = /(chest|barrel|furnace|smoker|crafting_table|_door|_trapdoor|fence_gate|_bed|lever|_button|anvil|enchanting_table|brewing_stand|hopper|dispenser|dropper|shulker|loom|grindstone|stonecutter|cartography|smithing|lectern|beacon|note_block|jukebox|repeater|comparator|daylight|cake|composter|cauldron|bell|sign)/;

function isInteractable(block) {
  return !!block && INTERACTABLE_RE.test(block.name);
}

const key3 = (p) => `${p.x},${p.y},${p.z}`;
const key2 = (x, z) => `${x},${z}`;

// ----------------------------------------------------------------------------
// Geometry
// ----------------------------------------------------------------------------

function eyePos(bot) {
  return bot.entity.position.offset(0, bot.entity.eyeHeight || 1.62, 0);
}

function blockCenter(pos) {
  return new Vec3(pos.x + 0.5, pos.y + 0.5, pos.z + 0.5);
}

function distEyeTo(bot, pos) {
  return eyePos(bot).distanceTo(blockCenter(pos));
}

function hdist(a, b) {
  const dx = a.x - b.x;
  const dz = a.z - b.z;
  return Math.sqrt(dx * dx + dz * dz);
}

/** Can a player stand with feet in block (x, y, z)? */
function standable(bot, x, y, z, { allowLeaves = false } = {}) {
  const below = bot.blockAt(new Vec3(x, y - 1, z));
  if (!isSolid(below, { allowLeaves })) return false;
  return isPassable(bot.blockAt(new Vec3(x, y, z))) && isPassable(bot.blockAt(new Vec3(x, y + 1, z)));
}

/**
 * Feet position of the standable spot of column (x, z) closest to nearY
 * (scans nearY+up .. nearY-down). Leaves / logs never count as ground.
 * @returns {Vec3|null}
 */
function surfaceAt(bot, x, z, nearY, { up = 6, down = 10 } = {}) {
  let best = null;
  for (let y = Math.floor(nearY) + up; y >= Math.floor(nearY) - down; y--) {
    if (standable(bot, x, y, z)) {
      const below = bot.blockAt(new Vec3(x, y - 1, z));
      if (isTreeBlock(below)) continue;
      if (!best || Math.abs(y - nearY) < Math.abs(best.y - nearY)) best = new Vec3(x, y, z);
    }
  }
  return best;
}

/** Highest solid non-tree, non-cover ground block of a column (scan from top). */
function groundTop(bot, x, z, fromY = 140, toY = -60) {
  for (let y = fromY; y >= toY; y--) {
    const b = bot.blockAt(new Vec3(x, y, z));
    if (b === null) return null;
    if (isAirName(b.name) || b.boundingBox === "empty") continue;
    if (isTreeBlock(b)) continue;
    if (isLiquidName(b.name) || b.name.includes("water")) return { y, liquid: true, name: b.name };
    return { y, liquid: false, name: b.name };
  }
  return null;
}

/** Positions of the two bots (self first). */
function playerEntities(bot, otherName) {
  const out = [bot.entity];
  const o = bot.players[otherName]?.entity;
  if (o) out.push(o);
  return out;
}

/** Does a player bounding box intersect block cell `pos`? */
function cellOccupied(entity, pos, margin = 0.0) {
  if (!entity) return false;
  const p = entity.position;
  const hw = 0.3 + margin;
  const h = entity.height || 1.8;
  return (
    p.x + hw > pos.x && p.x - hw < pos.x + 1 &&
    p.z + hw > pos.z && p.z - hw < pos.z + 1 &&
    p.y + h > pos.y && p.y < pos.y + 1
  );
}

// ----------------------------------------------------------------------------
// Movement
// ----------------------------------------------------------------------------

/**
 * Pathfinder movements: by default no digging, no scaffolding, no towers,
 * short drops; `avoid(x, z)` columns are excluded from stepping.
 */
function setMoves(bot, { canDig = false, avoid = null, maxDrop = 3, sprint = true } = {}) {
  try {
    const m = initializePathfinder(bot, {
      allowSprinting: sprint,
      allowParkour: false,
      canDig,
      canPlaceOn: false,
      scafoldingBlocks: [],
      maxDropDown: maxDrop,
    });
    m.canDig = canDig;
    m.allow1by1towers = false;
    m.scafoldingBlocks = [];
    m.maxDropDown = maxDrop;
    m.allowParkour = false;
    m.dontCreateFlow = true;
    if (avoid) {
      m.exclusionAreasStep = [(block) => (block && block.position && avoid(block.position.x, block.position.z) ? 1000 : 0)];
    }
    return m;
  } catch (err) {
    console.log(`[${bot.username}] setMoves failed: ${err.message}`);
    return null;
  }
}

/** Walk to feet position `feet` (block coords). Never throws. */
async function moveTo(bot, feet, { timeoutMs = 6000, avoid = null, canDig = false, range = 0 } = {}) {
  if (!feet) return false;
  const here = bot.entity.position;
  if (Math.floor(here.x) === feet.x && Math.floor(here.z) === feet.z && Math.abs(here.y - feet.y) < 0.6) {
    return true;
  }
  try {
    setMoves(bot, { avoid, canDig });
    const goal = range > 0 ? new GoalNear(feet.x, feet.y, feet.z, range) : new GoalBlock(feet.x, feet.y, feet.z);
    await gotoWithTimeout(bot, goal, { timeoutMs });
  } catch (err) {
    console.log(`[${bot.username}] moveTo(${feet}) did not finish: ${err.message}`);
  } finally {
    if (bot.pathfinder) bot.pathfinder.setGoal(null);
    stopAll(bot);
  }
  const p = bot.entity.position;
  return hdist(p, new Vec3(feet.x + 0.5, 0, feet.z + 0.5)) < 1.2 + range && Math.abs(p.y - feet.y) < 1.2;
}

/**
 * Stand spot (feet block) from which `target` is in reach.
 * @param {*} bot
 * @param {Vec3} target
 * @param {Object} opts
 * @param {Function} [opts.forbid] - (x, z, y) => true for columns/cells not to stand in
 * @param {Function} [opts.only]   - (x, z) => true for the only allowed columns
 * @param {number} [opts.minH=0.9] - min horizontal distance (cell centres)
 * @param {number} [opts.maxH=3.8]
 * @param {number} [opts.reach=REACH]
 * @param {boolean} [opts.allowBelow=false] - may stand right on top of target
 * @param {string} [opts.otherName]
 * @returns {Vec3|null}
 */
function findStandSpot(bot, target, opts = {}) {
  const {
    forbid = null,
    only = null,
    minH = 0.9,
    maxH = 3.8,
    reach = REACH,
    allowBelow = false,
    otherName = null,
    yUp = 2,
    yDown = 4,
  } = opts;
  const me = bot.entity.position;
  const other = otherName ? bot.players[otherName]?.entity : null;
  const tc = blockCenter(target);
  let best = null;
  let bestScore = Infinity;
  const R = Math.ceil(maxH);
  for (let dx = -R; dx <= R; dx++) {
    for (let dz = -R; dz <= R; dz++) {
      const x = target.x + dx;
      const z = target.z + dz;
      const h = Math.sqrt(dx * dx + dz * dz);
      if (h > maxH + 1e-6) continue;
      if (h < minH && !(allowBelow && h === 0)) continue;
      if (only && !only(x, z)) continue;
      for (let y = target.y + yUp; y >= target.y - yDown; y--) {
        if (forbid && forbid(x, z, y)) continue;
        if (!standable(bot, x, y, z, { allowLeaves: false })) continue;
        if (dx === 0 && dz === 0 && (target.y === y || target.y === y + 1 || (!allowBelow && target.y === y - 1))) continue;
        const eye = new Vec3(x + 0.5, y + 1.62, z + 0.5);
        const d = eye.distanceTo(tc);
        if (d > reach) continue;
        if (other && hdist(other.position, new Vec3(x + 0.5, 0, z + 0.5)) < 1.0 && Math.abs(other.position.y - y) < 2) continue;
        const walk = me.distanceTo(new Vec3(x + 0.5, y, z + 0.5));
        const score = walk + 0.6 * Math.abs(y - me.y) + 0.3 * d;
        if (score < bestScore) {
          bestScore = score;
          best = new Vec3(x, y, z);
        }
      }
    }
  }
  return best;
}

/** Make sure `target` is within reach; walk to a stand spot if needed. */
async function ensureReach(bot, target, opts = {}) {
  const reach = opts.reach || REACH;
  if (distEyeTo(bot, target) <= reach) return true;
  const spot = findStandSpot(bot, target, opts);
  if (!spot) {
    console.log(`[${bot.username}] no stand spot for ${target}`);
    return distEyeTo(bot, target) <= REACH_HIGH;
  }
  await moveTo(bot, spot, { timeoutMs: opts.walkTimeoutMs || 6000, avoid: opts.avoid || null });
  return distEyeTo(bot, target) <= Math.max(reach, REACH_HIGH);
}

// ----------------------------------------------------------------------------
// Items, placing, digging
// ----------------------------------------------------------------------------

async function holdItem(bot, name) {
  if (bot.heldItem && bot.heldItem.name === name) return true;
  try {
    await ensureItemInHand(bot, name);
    await sleep(60);
    return true;
  } catch (err) {
    console.log(`[${bot.username}] cannot hold ${name}: ${err.message}`);
    return false;
  }
}

const FACES = [
  new Vec3(0, -1, 0),
  new Vec3(1, 0, 0),
  new Vec3(-1, 0, 0),
  new Vec3(0, 0, 1),
  new Vec3(0, 0, -1),
  new Vec3(0, 1, 0),
];

/**
 * Reference block + face for placing at `pos`: any solid, non-interactable
 * neighbour; faces turned towards the eye and close ones are preferred.
 * @returns {{block, face: Vec3, point: Vec3}|null}
 */
function chooseRef(bot, pos, { exclude = null } = {}) {
  const eye = eyePos(bot);
  let best = null;
  let bestScore = -Infinity;
  for (const off of FACES) {
    const rp = pos.plus(off);
    if (exclude && exclude(rp)) continue;
    const rb = bot.blockAt(rp);
    if (!rb || rb.boundingBox !== "block" || isInteractable(rb)) continue;
    if (rb.name === "tnt") continue;
    const face = off.scaled(-1); // from ref towards pos
    const point = new Vec3(rp.x + 0.5 + face.x * 0.5, rp.y + 0.5 + face.y * 0.5, rp.z + 0.5 + face.z * 0.5);
    const toEye = eye.minus(point);
    const visible = face.x * toEye.x + face.y * toEye.y + face.z * toEye.z > 0;
    const score = (visible ? 10 : 0) - point.distanceTo(eye) + (off.y === -1 ? 0.5 : 0);
    if (score > bestScore) {
      bestScore = score;
      best = { block: rb, face, point };
    }
  }
  return best;
}

/**
 * Place `item` at `pos` (reach-aware). Returns true if the block is there
 * afterwards (for gravity blocks: if the server accepted the placement).
 * @param {*} bot
 * @param {Vec3} pos
 * @param {string} item - item (= block) name
 * @param {Object} [opts] - findStandSpot options + {otherName, timeoutMs, expect}
 */
async function placeBlockAt(bot, pos, item, opts = {}) {
  const { timeoutMs = 9000, expect = item, otherName = null } = opts;
  const t0 = Date.now();
  const gravity = isGravityBlock(item);
  for (let attempt = 0; attempt < 3; attempt++) {
    if (Date.now() - t0 > timeoutMs || bot._episodeStopping) return false;
    const cur = bot.blockAt(pos);
    if (!cur) return false;
    if (cur.name === expect) return true;
    if (!isAirName(cur.name) && !isLiquidName(cur.name)) {
      if (cur.boundingBox === "empty" && !cur.name.includes("water")) {
        // grass / flowers / torch in the way: break it first (instant)
        await digBlockAt(bot, pos, { ...opts, timeoutMs: 3000 });
        continue;
      }
      return false;
    }
    // somebody standing in the cell?
    if (cellOccupied(bot.entity, pos, 0.02)) {
      const spot = findStandSpot(bot, pos, { ...opts, minH: 1.4 });
      if (spot) await moveTo(bot, spot, { timeoutMs: 4000, avoid: opts.avoid || null });
      if (cellOccupied(bot.entity, pos, 0.0)) return false;
    }
    const other = otherName ? bot.players[otherName]?.entity : null;
    if (other && cellOccupied(other, pos, 0.0)) {
      await sleep(600);
      continue;
    }
    if (!(await ensureReach(bot, pos, opts))) {
      if (attempt >= 1) return false;
      continue;
    }
    const ref = chooseRef(bot, pos);
    if (!ref) return false;
    if (!(await holdItem(bot, item))) return false;
    await sc.lookAtPoint(bot, ref.point, WORK_LOOK_DPS, { randomized: false });
    let threw = false;
    try {
      await bot.placeBlock(ref.block, ref.face);
    } catch (err) {
      threw = true;
      console.log(`[${bot.username}] place ${item} at ${pos} failed: ${err.message}`);
    }
    await sleep(60);
    const nb = bot.blockAt(pos);
    if (nb && nb.name === expect) return true;
    if (gravity && !threw) return true;
    await sleep(150);
  }
  return false;
}

const SHOVEL_RE = /^(dirt|grass_block|sand|red_sand|gravel|coarse_dirt|podzol|mycelium|clay|snow|snow_block|soul_sand|soul_soil|mud|muddy_mangrove_roots|farmland|dirt_path|rooted_dirt|suspicious_sand|suspicious_gravel)$|_concrete_powder$/;
const AXE_RE = /(_log|_planks|_wood|_stem|_hyphae|bookshelf|crafting_table|chest|barrel|_fence|_door|_trapdoor|ladder|pumpkin|melon)$/;
const PICK_RE = /(stone|cobble|brick|_concrete$|deepslate|andesite|diorite|granite|_ore$|sandstone|terracotta|obsidian|basalt|blackstone|tuff|calcite|netherrack|prismarine|quartz|purpur|end_stone|ice)/;

/** Tool for a block: diamond shovel / axe / pickaxe, or null (hand). */
function toolFor(name) {
  if (SHOVEL_RE.test(name)) return "diamond_shovel";
  if (AXE_RE.test(name)) return "diamond_axe";
  if (PICK_RE.test(name)) return "diamond_pickaxe";
  return null;
}

/** Equip the right tool quickly (no-op when already held). */
async function equipToolFor(bot, block) {
  const tool = toolFor(block.name);
  if (tool) {
    const kind = tool.replace("diamond_", "_");
    if (bot.heldItem && bot.heldItem.name.endsWith(kind)) return;
    if (bot.inventory.items().some((i) => i.name === tool)) {
      await holdItem(bot, tool);
      return;
    }
  }
  if (bot.tool) {
    try {
      await bot.tool.equipForBlock(block, { requireHarvest: false });
    } catch (_) {}
  }
}

/**
 * Dig the block at `pos` (reach-aware, right tool). True if air afterwards.
 * The view is turned smoothly first; mineflayer's own instant look is skipped.
 * @param {*} bot
 * @param {Vec3} pos
 * @param {Object} [opts] - findStandSpot options + {timeoutMs}
 */
async function digBlockAt(bot, pos, opts = {}) {
  const { timeoutMs = 7000 } = opts;
  for (let attempt = 0; attempt < 2; attempt++) {
    if (bot._episodeStopping) return false;
    const b = bot.blockAt(pos);
    if (!b) return false;
    if (isAirName(b.name)) return true;
    if (isLiquidName(b.name) || b.name === "bedrock") return false;
    if (!(await ensureReach(bot, pos, opts))) {
      if (attempt >= 1) return false;
      continue;
    }
    const block = bot.blockAt(pos);
    if (!block || isAirName(block.name)) return true;
    const t0 = Date.now();
    await equipToolFor(bot, block);
    const t1 = Date.now();
    await sc.lookAtPoint(bot, blockCenter(pos), WORK_LOOK_DPS, { randomized: false });
    const t2 = Date.now();
    let timer = null;
    try {
      await Promise.race([
        bot.dig(block, "ignore"),
        new Promise((_, rej) => {
          timer = setTimeout(() => {
            try {
              bot.stopDigging();
            } catch (_) {}
            rej(new Error(`dig timed out after ${timeoutMs} ms`));
          }, timeoutMs);
        }),
      ]);
    } catch (err) {
      console.log(`[${bot.username}] dig ${block.name} at ${pos} failed: ${err.message}`);
    } finally {
      if (timer) clearTimeout(timer);
    }
    await sleep(40);
    const a = bot.blockAt(pos);
    const okDig = !!a && (isAirName(a.name) || isLiquidName(a.name));
    console.log(`[${bot.username}] [dig] ${block.name} ${pos} ok=${okDig} equip=${t1 - t0} look=${t2 - t1} dig=${Date.now() - t2}ms`);
    if (okDig) return true;
  }
  return false;
}

/** Jump-place `n` blocks of `material` under the bot (pillar up). */
async function towerUp(bot, n, material, args) {
  if (n <= 0) return { success: 0, failed: 0, heightGained: 0 };
  try {
    return await buildTowerUnderneath(bot, n, args, {
      blockType: material,
      enableRetry: true,
      breakOnFailure: true,
      maxPlaceAttempts: 8,
    });
  } catch (err) {
    console.log(`[${bot.username}] towerUp failed: ${err.message}`);
    return { success: 0, failed: n, heightGained: 0 };
  }
}

/**
 * Walk to the centre of the block column the bot is in (small sneak nudge
 * with the movement keys relative to the current yaw; the view does not turn).
 */
async function centerInCell(bot) {
  const p0 = bot.entity.position;
  const cx = Math.floor(p0.x) + 0.5;
  const cz = Math.floor(p0.z) + 0.5;
  const t0 = Date.now();
  const keys = ["forward", "back", "left", "right"];
  try {
    while (Date.now() - t0 < 1200) {
      const q = bot.entity.position;
      const dx = cx - q.x;
      const dz = cz - q.z;
      if (Math.abs(dx) < 0.15 && Math.abs(dz) < 0.15) break;
      const yaw = bot.entity.yaw;
      const fwd = dx * -Math.sin(yaw) + dz * -Math.cos(yaw);
      const right = dx * Math.cos(yaw) + dz * -Math.sin(yaw);
      bot.setControlState("sneak", true);
      bot.setControlState("forward", fwd > 0.08);
      bot.setControlState("back", fwd < -0.08);
      bot.setControlState("right", right > 0.08);
      bot.setControlState("left", right < -0.08);
      await sleep(50);
    }
  } finally {
    for (const k of keys) bot.setControlState(k, false);
    bot.setControlState("sneak", false);
  }
  await sleep(100);
}

// ----------------------------------------------------------------------------
// Signals between the two bots
// ----------------------------------------------------------------------------

/** Persistent per-episode listener: ctx.signals holds what the other bot sent. */
function installSignals(ctx) {
  const { coordinator, episodeNum } = ctx;
  const eventName = `episode_${episodeNum}_diverseSignal`;
  const signals = new Set();
  const data = {};
  const handler = (payload) => {
    if (payload && payload.sig) {
      signals.add(payload.sig);
      if (payload.data !== undefined) data[payload.sig] = payload.data;
    }
  };
  coordinator.on(eventName, handler);
  ctx.signals = signals;
  ctx.signalData = data;
  ctx.signal = (sig, payload) => {
    coordinator.sendToOtherBot("diverseSignal", { sig, data: payload }, episodeNum, `signal ${sig}`);
  };
  ctx.removeSignals = () => coordinator.removeListener(eventName, handler);
  return ctx;
}

// ----------------------------------------------------------------------------
// Gaze: actor glances, T8 observer
// ----------------------------------------------------------------------------

function rand(lo, hi) {
  return lo + Math.random() * (hi - lo);
}

/** Is the other bot's body centre inside this bot's view (approx. camera frustum)? */
function otherInView(bot, otherName, halfV = 30, halfH = 45) {
  const o = bot.players[otherName]?.entity;
  if (!o) return false;
  const eye = eyePos(bot);
  const t = o.position.offset(0, 0.9, 0);
  const dx = t.x - eye.x;
  const dy = t.y - eye.y;
  const dz = t.z - eye.z;
  const yaw = Math.atan2(-dx, -dz);
  const pitch = Math.atan2(dy, Math.sqrt(dx * dx + dz * dz));
  let dyaw = Math.abs(yaw - bot.entity.yaw) % (2 * Math.PI);
  if (dyaw > Math.PI) dyaw = 2 * Math.PI - dyaw;
  return (dyaw * 180) / Math.PI < halfH && (Math.abs(pitch - bot.entity.pitch) * 180) / Math.PI < halfV;
}

/**
 * With probability `prob`, look at the other bot for a moment (skipped when
 * the other bot is already in view).
 */
async function glance(ctx, prob = 0.35, ms = [500, 1000]) {
  if (Math.random() >= prob) return;
  if (otherInView(ctx.bot, ctx.otherName)) return;
  const o = ctx.bot.players[ctx.otherName]?.entity;
  if (!o) return;
  await sc.lookAtPoint(ctx.bot, o.position.offset(0, 1.5, 0), 170);
  await sleep(rand(ms[0], ms[1]));
}

/** A random standable vantage point on a ring around `center`. */
function pickVantage(bot, center, { minR = 4, maxR = 10, avoid = null, otherName = null, near = null } = {}) {
  const other = otherName ? bot.players[otherName]?.entity : null;
  const base = near ? Math.atan2(near.z - center.z, near.x - center.x) : Math.random() * 2 * Math.PI;
  for (let i = 0; i < 16; i++) {
    const ang = base + (near ? (Math.random() - 0.5) * 2.2 : Math.random() * 2 * Math.PI);
    const r = rand(minR, maxR);
    const x = Math.floor(center.x + r * Math.cos(ang));
    const z = Math.floor(center.z + r * Math.sin(ang));
    if (avoid && avoid(x, z)) continue;
    const s = surfaceAt(bot, x, z, center.y, { up: 4, down: 6 });
    if (!s) continue;
    if (avoid && avoid(s.x, s.z)) continue;
    if (other && hdist(other.position, new Vec3(s.x + 0.5, 0, s.z + 0.5)) < 2.5) continue;
    return s;
  }
  return null;
}

/**
 * T8 observer behaviour until `until()` is true (or maxMs): keep 3-20
 * blocks from the work, look at the other player (actorProb) or at the work
 * focus, change vantage now and then; never closer than ~2 blocks.
 */
async function observeUntil(ctx, opts = {}) {
  const {
    until = () => false,
    maxMs = 30000,
    focus = null,
    minR = 4,
    maxR = 9,
    avoid = null,
    actorProb = 0.7,
    moveProb = 0.5,
    canMove = true,
  } = opts;
  const { bot } = ctx;
  const t0 = Date.now();
  const otherEnt = () => bot.players[ctx.otherName]?.entity;
  let nextMove = Date.now() + rand(4000, 8000);
  while (!until() && Date.now() - t0 < maxMs && !bot._episodeStopping) {
    const center = (focus && focus()) || ctx.siteCenter;
    const o = otherEnt();
    const dOther = o ? o.position.distanceTo(bot.entity.position) : 99;
    const dCenter = hdist(bot.entity.position, center);
    const tooClose = dOther < 2.0;
    const tooFar = dCenter > Math.max(maxR + 5, 20) || dCenter < Math.min(minR, 3) - 1.5;
    if (canMove && (tooClose || tooFar || (Date.now() > nextMove && Math.random() < moveProb))) {
      const v = pickVantage(bot, center, { minR, maxR, avoid, otherName: ctx.otherName, near: bot.entity.position });
      if (v) await moveTo(bot, v, { timeoutMs: 5000, avoid });
      nextMove = Date.now() + rand(6000, 11000);
      continue;
    }
    if (Date.now() > nextMove) nextMove = Date.now() + rand(5000, 9000);
    const lookActor = Math.random() < actorProb;
    const getT = () => {
      const e = otherEnt();
      if (lookActor && e) return e.position.offset(0, 1.2, 0);
      return (focus && focus()) || ctx.siteCenter;
    };
    const stop = sc.startWatching(bot, getT, { minMs: 400, maxMs: 1100, jitter: 0.5, dps: 90 });
    const end = Date.now() + rand(1500, 3200);
    while (Date.now() < end && !until() && !bot._episodeStopping) await sleep(100);
    await stop();
  }
}

/**
 * Step in which each bot runs its own work function (or none), signals
 * "<name>_done" and then observes until the other bot is done too.
 * @param {string} name
 * @param {Function|null} actorFn - async function(ctx) for the actor
 * @param {Function|null} otherFn - async function(ctx) for the other bot
 * @param {Object} [opts] - {maxMs, observe: observeUntil opts (function of ctx)}
 */
function dualStep(name, actorFn, otherFn, opts = {}) {
  const { maxMs = 60000, observe = null } = opts;
  return {
    name,
    run: async function (otherData, ourPrev) {
      const ctx = this;
      ctx.otherData = otherData;
      ctx.ourPrev = ourPrev;
      const t0 = Date.now();
      const fn = ctx.isActor ? actorFn : otherFn;
      let out = {};
      if (fn) {
        try {
          out = (await fn(ctx, otherData, ourPrev)) || {};
        } catch (err) {
          console.error(`[${ctx.bot.username}] ${name} work failed: ${err.stack || err}`);
        }
      }
      stopAll(ctx.bot);
      const sig = `${name}_done`;
      ctx.signal(sig);
      const obsOpts = observe ? observe(ctx) : {};
      await observeUntil(ctx, {
        ...obsOpts,
        until: () => ctx.signals.has(sig),
        maxMs: Math.max(1500, maxMs - (Date.now() - t0)),
      });
      return out;
    },
  };
}

/** Both bots look at the work, then at each other (admire / pause). */
function lookStep(name, msRange = [1500, 2500], focus = null) {
  return {
    name,
    run: async function () {
      const ctx = this;
      const f = focus ? () => focus(ctx) : () => ctx.siteCenter;
      const total = rand(msRange[0], msRange[1]);
      await sc.watchFor(ctx.bot, f, total * 0.4);
      await sc.lookAtOtherBot(ctx.bot, ctx.otherName, 120);
      await sleep(total * 0.6);
      return {};
    },
  };
}

/** First step: eye contact, then a look at the work site. */
function greetStep(name = "greet") {
  return {
    name,
    run: async function () {
      const ctx = this;
      setMoves(ctx.bot);
      await sc.lookAtOtherBot(ctx.bot, ctx.otherName, 120);
      await sleep(rand(900, 1500));
      await sc.lookAtPoint(ctx.bot, ctx.siteCenter, 90);
      await sleep(rand(300, 600));
      return {};
    },
  };
}

// ----------------------------------------------------------------------------
// Placement lists
// ----------------------------------------------------------------------------

/**
 * Place a list of {pos, material} (in the given order) with a retry pass for
 * cells that had no support yet. Optional glance between blocks.
 * @returns {Promise<{placed: number[], failed: number[]}>} indices into list
 */
async function buildPlacements(ctx, list, opts = {}) {
  const { deadline = Infinity, glanceProb = 0.35, standOpts = {}, delayMs = [80, 220], onPlaced = null } = opts;
  const { bot } = ctx;
  const placed = [];
  let pending = list.map((_, i) => i);
  for (let pass = 0; pass < 2 && pending.length > 0; pass++) {
    const failed = [];
    for (const i of pending) {
      if (bot._episodeStopping || Date.now() > deadline) {
        failed.push(i);
        continue;
      }
      const item = list[i];
      const ok = await placeBlockAt(bot, item.pos, item.material, {
        otherName: ctx.otherName,
        expect: item.expect || item.material,
        ...standOpts,
      });
      if (ok) {
        placed.push(i);
        if (onPlaced) onPlaced(i);
        await glance(ctx, glanceProb);
      } else failed.push(i);
      await sleep(rand(delayMs[0], delayMs[1]));
    }
    pending = failed;
  }
  return { placed, failed: pending };
}

/** Yaw/pitch change (deg) needed to look from the current view at `pos`. */
function turnCost(bot, pos) {
  const eye = eyePos(bot);
  const c = blockCenter(pos);
  const dx = c.x - eye.x;
  const dy = c.y - eye.y;
  const dz = c.z - eye.z;
  const yaw = Math.atan2(-dx, -dz);
  const pitch = Math.atan2(dy, Math.sqrt(dx * dx + dz * dz));
  let dyaw = Math.abs(yaw - bot.entity.yaw) % (2 * Math.PI);
  if (dyaw > Math.PI) dyaw = 2 * Math.PI - dyaw;
  return ((dyaw + Math.abs(pitch - bot.entity.pitch)) * 180) / Math.PI;
}

/**
 * Dig a list of positions. Groups (opts.group(pos) -> number, ascending) are
 * processed in order; inside a group the next block is the one needing the
 * smallest head turn (plus a little walking), like a player sweeping a row.
 */
async function digPositions(ctx, positions, opts = {}) {
  const { deadline = Infinity, glanceProb = 0.35, standOpts = {}, delayMs = [60, 180], group = null, greedy = true } = opts;
  const dug = [];
  const failed = [];
  const groups = new Map();
  for (const p of positions) {
    const g = group ? group(p) : 0;
    if (!groups.has(g)) groups.set(g, []);
    groups.get(g).push(p);
  }
  const keys = [...groups.keys()].sort((a, b) => a - b);
  for (const k of keys) {
    const pending = groups.get(k).slice();
    while (pending.length) {
      let idx = 0;
      if (greedy) {
        let best = Infinity;
        pending.forEach((p, i) => {
          const c = turnCost(ctx.bot, p) + 25 * Math.max(0, distEyeTo(ctx.bot, p) - REACH);
          if (c < best) {
            best = c;
            idx = i;
          }
        });
      }
      const pos = pending.splice(idx, 1)[0];
      if (ctx.bot._episodeStopping || Date.now() > deadline) {
        failed.push(pos);
        continue;
      }
      if (await digBlockAt(ctx.bot, pos, { otherName: ctx.otherName, ...standOpts })) {
        dug.push(pos);
        await glance(ctx, glanceProb);
      } else failed.push(pos);
      await sleep(rand(delayMs[0], delayMs[1]));
    }
  }
  return { dug, failed };
}

/** Rotate a ring-ordered list so it starts at the element nearest the bot. */
function startNearest(bot, list) {
  if (list.length === 0) return list;
  let bi = 0;
  let bd = Infinity;
  list.forEach((it, i) => {
    const d = bot.entity.position.distanceTo(blockCenter(it.pos));
    if (d < bd) {
      bd = d;
      bi = i;
    }
  });
  return list.slice(bi).concat(list.slice(0, bi));
}

// ----------------------------------------------------------------------------
// House blueprint (coBuild, tntDemolish, forestBuild)
// ----------------------------------------------------------------------------

/**
 * Small house in a layout frame (state-common makeFrame): cells du in
 * [u0, u0+D-1] (axis u), dp in [p0, p0+W-1] (axis p); dy = 0 is the first
 * block above the ground (frame.level).
 *
 * @param {Object} frame
 * @param {Object} o
 * @param {number} o.width  - W (along p), 3..7
 * @param {number} o.depth  - D (along u), 3..7
 * @param {number} o.height - wall height H (roof at dy = H)
 * @param {Object} o.palette - materials (walls, pillar, window, roof, floor, door)
 * @param {boolean} [o.floor=false] - replace the interior ground (dy=-1) with floor blocks
 * @param {boolean} [o.pillars=true] - corner posts in the pillar material
 * @param {string} [o.doorSide="u+"] - wall with the door gap: u+, u-, p+, p-
 * @param {number} [o.windowCount=2]
 * @param {number} [o.windowSeed=0] - [0,1) chooses window cells deterministically
 * @param {string} [o.roofStyle="flat"] - flat | rim (perimeter roof cells in pillar wood)
 * @param {boolean} [o.door=false] - place a door item in the gap
 * @param {number} [o.torches=0] - torches on the floor inside
 * @returns {Object} house description
 */
function makeHouse(frame, o) {
  const W = o.width;
  const D = o.depth;
  const H = o.height;
  const pal = o.palette;
  const u0 = -Math.floor((D - 1) / 2);
  const p0 = -Math.floor((W - 1) / 2);
  const u1 = u0 + D - 1;
  const p1 = p0 + W - 1;
  const isPerim = (du, dp) => du === u0 || du === u1 || dp === p0 || dp === p1;
  const isCorner = (du, dp) => (du === u0 || du === u1) && (dp === p0 || dp === p1);

  // perimeter ring in walking order (continuous loop)
  const ring = [];
  for (let dp = p0; dp <= p1; dp++) ring.push([u0, dp]);
  for (let du = u0 + 1; du <= u1; du++) ring.push([du, p1]);
  for (let dp = p1 - 1; dp >= p0; dp--) ring.push([u1, dp]);
  for (let du = u1 - 1; du > u0; du--) ring.push([du, p0]);

  // door: middle of the chosen wall
  const side = o.doorSide || "u+";
  let door;
  if (side === "u+") door = [u1, p0 + Math.floor((W - 1) / 2)];
  else if (side === "u-") door = [u0, p0 + Math.floor(W / 2)];
  else if (side === "p+") door = [u0 + Math.floor((D - 1) / 2), p1];
  else door = [u0 + Math.floor(D / 2), p0];
  const isDoor = (du, dp) => du === door[0] && dp === door[1];
  const adjDoor = (du, dp) => Math.abs(du - door[0]) + Math.abs(dp - door[1]) <= 1;

  // windows: non-corner, non-door, not adjacent to the door, dy = 1
  const winCandidates = ring.filter(([du, dp]) => !isCorner(du, dp) && !adjDoor(du, dp));
  const windows = [];
  const nWin = Math.min(o.windowCount || 0, winCandidates.length);
  if (nWin > 0) {
    const step = winCandidates.length / nWin;
    const offset = Math.floor((o.windowSeed || 0) * step);
    for (let k = 0; k < nWin; k++) windows.push(winCandidates[(offset + Math.floor(k * step)) % winCandidates.length]);
  }
  const isWindow = (du, dp) => windows.some(([a, b]) => a === du && b === dp);
  const winY = H >= 3 ? 1 : 1;

  const P = (du, dp, dy) => frame.at(du, dp, dy);
  const placements = [];
  const add = (du, dp, dy, material, part) => placements.push({ pos: P(du, dp, dy), material, part, local: [du, dp, dy] });

  const interior = [];
  for (let du = u0 + 1; du < u1; du++) for (let dp = p0 + 1; dp < p1; dp++) interior.push([du, dp]);

  // floor: replaces the ground of the interior (dig first, then place)
  const floorCells = o.floor ? interior.map(([du, dp]) => ({ pos: P(du, dp, -1), material: pal.floor, part: "floor", local: [du, dp, -1] })) : [];

  // corner pillars
  if (o.pillars !== false) {
    for (const [du, dp] of ring.filter(([a, b]) => isCorner(a, b))) {
      for (let dy = 0; dy < H; dy++) add(du, dp, dy, pal.pillar, "pillar");
    }
  }
  // walls, row by row along the ring
  for (let dy = 0; dy < H; dy++) {
    for (const [du, dp] of ring) {
      if (o.pillars !== false && isCorner(du, dp)) continue;
      if (isDoor(du, dp) && dy <= 1) continue;
      if (isWindow(du, dp) && dy === winY) continue;
      add(du, dp, dy, pal.walls, "wall");
    }
  }
  for (const [du, dp] of windows) add(du, dp, winY, pal.window, "window");
  // roof: perimeter first, then inwards
  const roofCells = [];
  for (let du = u0; du <= u1; du++) for (let dp = p0; dp <= p1; dp++) roofCells.push([du, dp]);
  const ringDist = (du, dp) => Math.min(du - u0, u1 - du, dp - p0, p1 - dp);
  roofCells.sort((a, b) => ringDist(a[0], a[1]) - ringDist(b[0], b[1]));
  for (const [du, dp] of roofCells) {
    const mat = o.roofStyle === "rim" && isPerim(du, dp) ? pal.pillar : pal.roof;
    add(du, dp, H, mat, "roof");
  }
  // optional non-full blocks
  if (o.door) {
    placements.push({ pos: P(door[0], door[1], 0), material: pal.door, part: "door", local: [door[0], door[1], 0], expect: pal.door });
  }
  const torches = [];
  for (let k = 0; k < (o.torches || 0) && k < interior.length; k++) {
    const [du, dp] = interior[(k * 2 + 1) % interior.length];
    if (torches.some(([a, b]) => a === du && b === dp)) continue;
    torches.push([du, dp]);
    placements.push({ pos: P(du, dp, 0), material: "torch", part: "torch", local: [du, dp, 0], expect: "torch" });
  }

  const footprint = new Set();
  for (let du = u0; du <= u1; du++) for (let dp = p0; dp <= p1; dp++) {
    const w = P(du, dp, 0);
    footprint.add(key2(w.x, w.z));
  }
  const interiorSet = new Set(interior.map(([du, dp]) => {
    const w = P(du, dp, 0);
    return key2(w.x, w.z);
  }));
  const doorPos = P(door[0], door[1], 0);
  // the cell right outside the door
  const outDir = side === "u+" ? [1, 0] : side === "u-" ? [-1, 0] : side === "p+" ? [0, 1] : [0, -1];
  const doorOutside = P(door[0] + outDir[0], door[1] + outDir[1], 0);
  const doorInside = P(door[0] - outDir[0], door[1] - outDir[1], 0);
  const center = frame.at((u0 + u1) / 2, (p0 + p1) / 2, Math.floor(H / 2));

  return {
    W, D, H, u0, u1, p0, p1,
    placements,
    floorCells,
    footprint,
    interiorSet,
    door: doorPos,
    doorOutside,
    doorInside,
    doorSide: side,
    windows: windows.map(([du, dp]) => P(du, dp, winY)),
    center: new Vec3(center.x + 0.5, center.y, center.z + 0.5),
    inFootprint: (x, z) => footprint.has(key2(x, z)),
    inInterior: (x, z) => interiorSet.has(key2(x, z)),
  };
}

/** Compact metadata for a placement list. */
function placementsMetadata(list) {
  return list.map((p) => ({ pos: [p.pos.x, p.pos.y, p.pos.z], material: p.material, part: p.part }));
}

// ----------------------------------------------------------------------------
// Site handling
// ----------------------------------------------------------------------------

const COVER_REPLACE = [
  "minecraft:short_grass",
  "minecraft:tall_grass",
  "minecraft:fern",
  "minecraft:large_fern",
  "minecraft:snow",
  "minecraft:dead_bush",
  "#minecraft:small_flowers",
  "#minecraft:tall_flowers",
  "minecraft:sweet_berry_bush",
  "minecraft:brown_mushroom",
  "minecraft:red_mushroom",
];

/** /fill commands that remove plant / snow cover in a box (nothing else). */
function clearCoverCommands(x1, y1, z1, x2, y2, z2) {
  return COVER_REPLACE.map((b) => `fill ${x1} ${y1} ${z1} ${x2} ${y2} ${z2} minecraft:air replace ${b}`);
}

/** Is a (2*half+1)^2 square flat with `clear` free blocks above (cover ok)? */
function squareFlat(bot, cx, gy, cz, half, clear = 3) {
  for (let dx = -half; dx <= half; dx++) {
    for (let dz = -half; dz <= half; dz++) {
      const g = bot.blockAt(new Vec3(cx + dx, gy, cz + dz));
      if (!isSolid(g) || isTreeBlock(g)) return false;
      for (let dy = 1; dy <= clear; dy++) {
        const b = bot.blockAt(new Vec3(cx + dx, gy + dy, cz + dz));
        if (!b) return false;
        if (!(isAirName(b.name) || (b.boundingBox === "empty" && !isLiquidName(b.name) && !b.name.includes("water")))) return false;
      }
    }
  }
  return true;
}

/**
 * Find a flat site (ground block of its centre) near `center`. Rings
 * outward, deterministic. `accept(site)` can veto candidates.
 */
function findSite(bot, center, half, searchRadius = 14, { clear = 3, accept = null } = {}) {
  const cx = Math.floor(center.x);
  const cz = Math.floor(center.z);
  for (let r = 0; r <= searchRadius; r++) {
    for (let dx = -r; dx <= r; dx++) {
      for (let dz = -r; dz <= r; dz++) {
        if (Math.max(Math.abs(dx), Math.abs(dz)) !== r) continue;
        const gt = groundTop(bot, cx + dx, cz + dz, Math.floor(center.y) + 24, Math.floor(center.y) - 24);
        if (!gt || gt.liquid) continue;
        if (squareFlat(bot, cx + dx, gt.y, cz + dz, half, clear)) {
          const s = new Vec3(cx + dx, gt.y, cz + dz);
          if (!accept || accept(s)) return s;
        }
      }
    }
  }
  return null;
}

/**
 * /fill commands levelling a square pad (before the snapshot): fill holes
 * below with dirt, set the top layer, clear the space above.
 */
function levelPadCommands(bot, cx, gy, cz, half, clearUp = 6) {
  const x1 = cx - half;
  const x2 = cx + half;
  const z1 = cz - half;
  const z2 = cz + half;
  const topBlock = bot.blockAt(new Vec3(cx, gy, cz));
  let top = topBlock && isSolid(topBlock) && !isGravityBlock(topBlock.name) ? topBlock.name : "grass_block";
  if (!["grass_block", "dirt", "stone", "podzol", "coarse_dirt", "sandstone", "terracotta", "mycelium", "moss_block", "snow_block"].includes(top)) top = "grass_block";
  return [
    `fill ${x1} ${gy - 3} ${z1} ${x2} ${gy - 1} ${z2} minecraft:dirt replace air`,
    `fill ${x1} ${gy - 3} ${z1} ${x2} ${gy - 1} ${z2} minecraft:dirt replace water`,
    `fill ${x1} ${gy} ${z1} ${x2} ${gy} ${z2} minecraft:${top}`,
    `fill ${x1} ${gy + 1} ${z1} ${x2} ${gy + clearUp} ${z2} minecraft:air`,
  ];
}

// ----------------------------------------------------------------------------
// Pit exit
// ----------------------------------------------------------------------------

/**
 * Stair cells to climb out of a pit: from feet position `from` (bottom cell
 * at the pit edge) `steps` steps along `dir` ([dx, dz]); step i needs its
 * feet + head cell (column i) and the head-room cell above the previous
 * column dug out.
 * @returns {Array<{feet: Vec3, dig: Vec3[]}>}
 */
function stairPlan(from, dir, steps) {
  const out = [];
  for (let i = 1; i <= steps; i++) {
    const feet = new Vec3(from.x + dir[0] * i, from.y + i, from.z + dir[1] * i);
    const prev = new Vec3(from.x + dir[0] * (i - 1), from.y + i - 1, from.z + dir[1] * (i - 1));
    out.push({ feet, dig: [prev.offset(0, 2, 0), feet.offset(0, 1, 0), feet.clone()] });
  }
  return out;
}

/** All cells a stair plan may remove (for observers to avoid). */
function stairColumns(from, dir, steps) {
  return stairPlan(from, dir, steps).map((s) => key2(s.feet.x, s.feet.z));
}

/** Climb out by cutting a staircase into the pit wall. */
async function digStairsOut(ctx, from, dir, steps) {
  const { bot } = ctx;
  if (!(await moveTo(bot, from, { timeoutMs: 5000 }))) return false;
  for (const st of stairPlan(from, dir, steps)) {
    for (const c of st.dig) {
      const b = bot.blockAt(c);
      if (b && !isPassable(b)) await digBlockAt(bot, c, { only: () => false, otherName: ctx.otherName });
    }
    if (!(await moveTo(bot, st.feet, { timeoutMs: 3000 }))) return false;
  }
  return true;
}

/**
 * Leave a pit/tunnel and stand on `rim` (feet position at ground level).
 * 1) plain walk (1-block steps), 2) cut a staircase (opts.stairs =
 * {from, dir, steps}), 3) pillar up with `towerMaterial` (open sky only),
 * 4) pathfinder allowed to dig, 5) RCON teleport (last resort).
 * @returns {Promise<string>} method used
 */
async function exitPit(ctx, rim, { towerMaterial = "dirt", rimY = null, avoid = null, stairs = null } = {}) {
  const { bot, rcon, args } = ctx;
  const topY = rimY ?? rim.y;
  const up = () => bot.entity.position.y >= topY - 0.2;
  if (topY - bot.entity.position.y <= 1.2 && (await moveTo(bot, rim, { timeoutMs: 4000, avoid }))) return "walk";
  if (up()) {
    await moveTo(bot, rim, { timeoutMs: 4000, avoid });
    return "walk";
  }
  if (stairs) {
    await digStairsOut(ctx, stairs.from, stairs.dir, stairs.steps);
    if (up()) {
      await moveTo(bot, rim, { timeoutMs: 5000, avoid });
      return "stairs";
    }
  }
  // pillar up where the sky is open
  const need = Math.ceil(topY - bot.entity.position.y);
  const p = bot.entity.position;
  let open = true;
  for (let dy = 2; dy <= need + 2; dy++) {
    if (!isPassable(bot.blockAt(new Vec3(Math.floor(p.x), Math.floor(p.y) + dy, Math.floor(p.z))))) open = false;
  }
  if (need > 0 && open) {
    await centerInCell(bot);
    await towerUp(bot, need, towerMaterial, args);
    if (up()) {
      await moveTo(bot, rim, { timeoutMs: 5000, avoid });
      return "pillar";
    }
  }
  await moveTo(bot, rim, { timeoutMs: 12000, canDig: true, range: 1 });
  if (up()) {
    await moveTo(bot, rim, { timeoutMs: 4000, avoid });
    return "pathfinder_dig";
  }
  await rconTp(rcon, bot.username, rim.x + 0.5, rim.y, rim.z + 0.5);
  await sleep(800);
  return "teleport";
}

// ----------------------------------------------------------------------------
// Pit digging / refilling "in place" (digDown, tunnel shaft)
// ----------------------------------------------------------------------------

/**
 * Dig `layers` (top layer first, each a list of block positions) standing on
 * the `middle` column: every other cell of the layer under the feet, then the
 * block under the bot (it drops one level). Ends at the bottom of the pit.
 * @returns {Promise<{dug: number, failedLayers: number}>}
 */
async function digPitInPlace(ctx, layers, middle, { deadline = Date.now() + 55000, level, label = "dig_layer" } = {}) {
  const { bot, rcon, args, episodeNum } = ctx;
  const onSelf = (x, z) => x === middle.x && z === middle.z;
  await moveTo(bot, new Vec3(middle.x, level, middle.z), { timeoutMs: 6000 });
  let dug = 0;
  let failedLayers = 0;
  for (let k = 0; k < layers.length; k++) {
    if (Date.now() > deadline || bot._episodeStopping) break;
    const cells = layers[k].filter((p) => !onSelf(p.x, p.z));
    const res = await digPositions(ctx, cells, {
      deadline,
      glanceProb: 0.35,
      standOpts: { only: onSelf, allowBelow: true },
    });
    dug += res.dug.length;
    if (res.failed.length) failedLayers++;
    const own = layers[k].find((p) => onSelf(p.x, p.z));
    if (own && (await digBlockAt(bot, own, { allowBelow: true, minH: 0, only: onSelf }))) dug++;
    await sleep(450);
    await sc.note(rcon, args, bot, episodeNum, label, { layer: k + 1, y: own ? own.y : null, failed: res.failed.length });
    await glance(ctx, 1.0, [800, 1300]);
  }
  return { dug, failedLayers };
}

/**
 * Refill `layers` (top layer first) from the bottom up: the bot stands in the
 * `middle` column at the bottom, places every other cell of its feet layer,
 * then jump-places `material` under itself; repeat up to the surface.
 * @returns {Promise<{placed: number}>}
 */
async function refillPitInPlace(ctx, layers, middle, material, { deadline = Date.now() + 50000 } = {}) {
  const { bot, args } = ctx;
  const onSelf = (x, z) => x === middle.x && z === middle.z;
  let placed = 0;
  for (let k = layers.length - 1; k >= 0; k--) {
    if (Date.now() > deadline || bot._episodeStopping) break;
    await centerInCell(bot);
    const me = bot.entity.position;
    const cells = layers[k]
      .filter((p) => !onSelf(p.x, p.z))
      .sort((a, b) => me.distanceTo(blockCenter(b)) - me.distanceTo(blockCenter(a)))
      .map((pos) => ({ pos, material }));
    const res = await buildPlacements(ctx, cells, {
      deadline,
      glanceProb: 0.35,
      standOpts: { only: onSelf, minH: 0, allowBelow: true },
    });
    placed += res.placed.length;
    const own = layers[k].find((p) => onSelf(p.x, p.z));
    const feetY = Math.floor(bot.entity.position.y + 0.01);
    if (own && feetY <= own.y) {
      await centerInCell(bot);
      const t = await towerUp(bot, 1, material, args);
      placed += t.success;
    }
    await glance(ctx, 1.0, [700, 1100]);
  }
  return { placed };
}

// ----------------------------------------------------------------------------
// Base class
// ----------------------------------------------------------------------------

/**
 * Base of the build/destroy episodes. Subclasses implement
 *   planEpisode(bot, rng, args, frame, isActor, info) -> {
 *     actorStand: Vec3 col, observerStand: Vec3 col,
 *     items: {actor: [[name,n]], observer: [[name,n]]},
 *     metadata: {...}, leaderSetupCommands?: [], clearBox?: {x1,y1,z1,x2,y2,z2}
 *   }
 *   buildSteps(ctx) -> steps (use bindSteps)
 * and may override chooseSite(bot, args, mid) -> {site, prepared, commands}.
 */
class DiverseEpisodeBase extends sc.StateEpisodeBase {
  static INIT_MIN_BOTS_DISTANCE = 5;
  static INIT_MAX_BOTS_DISTANCE = 10;
  static WORKS_IN_NON_FLAT_WORLD = true;
  static SITE_HALF_SIZE = 3;
  static SITE_SEARCH_RADIUS = 14;
  static SITE_CLEAR_HEIGHT = 3;
  // normal worlds: level a pad with /fill (before the snapshot) if no flat site
  static ALLOW_LEVEL_SITE = true;

  isFlatWorld(args) {
    return String(args.world_type || "flat").toLowerCase() === "flat";
  }

  /** Default site choice: flat site, else (normal world) a levelled pad. */
  chooseSite(bot, args, mid) {
    const half = this.constructor.SITE_HALF_SIZE;
    const site = findSite(bot, mid, half, this.constructor.SITE_SEARCH_RADIUS, {
      clear: this.constructor.SITE_CLEAR_HEIGHT,
      accept: (s) => this.acceptSite(bot, s),
    });
    if (site) return { site, prepared: false, commands: [] };
    if (!this.constructor.ALLOW_LEVEL_SITE || this.isFlatWorld(args)) return null;
    // level a pad: the midpoint first, then rings of candidates (step 2)
    // until one is on dry ground and accepted by acceptSite()
    const mx = Math.floor(mid.x);
    const mz = Math.floor(mid.z);
    for (let r = 0; r <= 16; r += 2) {
      for (let dx = -r; dx <= r; dx += 2) {
        for (let dz = -r; dz <= r; dz += 2) {
          if (Math.max(Math.abs(dx), Math.abs(dz)) !== r) continue;
          const cx = mx + dx;
          const cz = mz + dz;
          const gt = groundTop(bot, cx, cz, Math.floor(mid.y) + 24, Math.floor(mid.y) - 24);
          if (!gt || gt.liquid) continue; // never build a pad in water
          const s = new Vec3(cx, gt.y, cz);
          if (!this.acceptSite(bot, s)) continue;
          const commands = levelPadCommands(bot, cx, gt.y, cz, half + 1, Math.max(6, this.constructor.SITE_CLEAR_HEIGHT + 3));
          return { site: s, prepared: true, commands };
        }
      }
    }
    return null;
  }

  /** Subclasses can veto a flat site (e.g. trees nearby). */
  acceptSite(bot, site) {
    return true;
  }

  async setupEpisode(bot, rcon, sharedBotRng, coordinator, episodeNum, args, botPosition, otherBotPosition) {
    const mid = new Vec3(
      (botPosition.x + otherBotPosition.x) / 2,
      (botPosition.y + otherBotPosition.y) / 2,
      (botPosition.z + otherBotPosition.z) / 2,
    );
    // Shared draws first (fixed count), world-dependent choices afterwards.
    const isActor = decidePrimaryBot(bot, sharedBotRng, args);
    let axis = sharedBotRng() < 0.5 ? "x" : "z";
    let side = sharedBotRng() < 0.5 ? -1 : 1;
    this.preSetup(bot, args);
    const leader = isWorldStateLeader(bot, args);
    // Site search on UNEDITED terrain by both bots; the follower adopts the
    // leader's choice (handshake "div_site" is a barrier: the leader edits
    // nothing before both bots passed it, see utils/handshake.js).
    let chosen = this.chooseSite(bot, args, mid);
    const theirs = await exchange(
      coordinator,
      episodeNum,
      "div_site",
      chosen ? toPlain(chosen) : { none: true },
      30000,
    );
    let handshake = leader ? "leader" : "own_timeout";
    if (!leader && theirs && (theirs.site || theirs.none)) {
      chosen = theirs.none ? null : reviveVec3(theirs);
      handshake = "adopted";
    }
    if (!chosen) {
      throw new Error(`[${this.constructor.name}] no usable site near ${mid}`);
    }
    const { site } = chosen;
    // a site may impose the layout orientation (e.g. a hillside tunnel)
    if (chosen.axis) axis = chosen.axis;
    if (chosen.side) side = chosen.side;
    const frame = sc.makeFrame(site, axis, side);
    const plan = this.planEpisode(bot, sharedBotRng, args, frame, isActor, chosen);
    // barrier: planEpisode may read the world; the follower must be done
    // before the leader's /fill commands. The stands are compared (log only).
    const standKey = (p) => (p ? `${p.x},${p.y},${p.z}` : "null");
    const planSig = `${standKey(plan.actorStand)}|${standKey(plan.observerStand)}`;
    const planTheirs = await exchange(coordinator, episodeNum, "div_plan", { sig: planSig }, 30000);
    const planAgree = planTheirs && planTheirs.sig !== undefined ? planTheirs.sig === planSig : null;
    if (planAgree === false) {
      console.warn(`[${bot.username}] ${this.constructor.name}: plan differs from the other bot (${planSig} vs ${planTheirs.sig})`);
    }

    const half = this.constructor.SITE_HALF_SIZE;
    const cb = plan.clearBox || {
      x1: site.x - half - 1,
      z1: site.z - half - 1,
      x2: site.x + half + 1,
      z2: site.z + half + 1,
      y1: frame.level,
      y2: frame.level + 2,
    };
    const commands = [
      ...(chosen.commands || []),
      ...clearCoverCommands(cb.x1, cb.y1, cb.z1, cb.x2, cb.y2, cb.z2),
      ...(plan.leaderSetupCommands || []),
    ];
    if (leader) {
      for (const cmd of commands) {
        try {
          const r = await rcon.send(cmd);
          console.log(`[${bot.username}] setup '${cmd}' -> ${String(r).slice(0, 120)}`);
        } catch (err) {
          console.log(`[${bot.username}] setup '${cmd}' failed: ${err.message}`);
        }
      }
    }
    // barrier: the follower reads stand heights only after the leader's edits
    await exchange(coordinator, episodeNum, "div_ready", {}, 30000);
    await sleep(chosen.prepared ? 600 : 300);

    const myCol = isActor ? plan.actorStand : plan.observerStand;
    const otherCol = isActor ? plan.observerStand : plan.actorStand;
    const me = this.feetFor(bot, myCol, frame);
    const other = this.feetFor(bot, otherCol, frame);
    await rconTp(rcon, bot.username, me.x + 0.5, me.y, me.z + 0.5);
    await sleep(1200);

    await giveMaterials(bot, rcon, isActor ? plan.items.actor : plan.items.observer, { extra: 0 });
    await unequipHand(bot);

    this._frame = frame;
    this._isActor = isActor;
    this._plan = plan;
    this._worldStateCenter = sc.centerOf(site);
    this._stateMetadata = {
      episode: this.constructor.name,
      site: [site.x, site.y, site.z],
      axis,
      side,
      actor: isActor ? bot.username : args.other_bot_name,
      observer: isActor ? args.other_bot_name : bot.username,
      site_prepared: Boolean(chosen.prepared),
      site_handshake: handshake,
      plan_agrees_with_other_bot: planAgree,
      ...(chosen.prepared ? { site_prepare_commands: chosen.commands } : {}),
      ...plan.metadata,
    };
    console.log(`[${bot.username}] ${this.constructor.name} setup: ${JSON.stringify(this._stateMetadata).slice(0, 1500)}`);
    return {
      botPositionNew: new Vec3(me.x + 0.5, me.y, me.z + 0.5),
      otherBotPositionNew: new Vec3(other.x + 0.5, other.y, other.z + 0.5),
    };
  }

  /** Hook before the site search (e.g. reject flat worlds). */
  preSetup(bot, args) {}

  /**
   * Feet position for a stand column: the standable spot of that column (or
   * of the nearest column within 4 blocks) closest to the site level; last
   * resort: on top of the highest block of the column (never inside terrain).
   */
  feetFor(bot, col, frame) {
    for (let r = 0; r <= 4; r++) {
      for (let dx = -r; dx <= r; dx++) {
        for (let dz = -r; dz <= r; dz++) {
          if (Math.max(Math.abs(dx), Math.abs(dz)) !== r) continue;
          const s = surfaceAt(bot, col.x + dx, col.z + dz, frame.level, { up: 8, down: 10 });
          if (s) return s;
        }
      }
    }
    let y = frame.level + 30;
    while (y > frame.level - 20 && isPassable(bot.blockAt(new Vec3(col.x, y - 1, col.z)))) y--;
    return new Vec3(col.x, y, col.z);
  }

  async entryPoint(bot, rcon, sharedBotRng, coordinator, iterationID, episodeNum, args) {
    const ctx = {
      bot,
      rcon,
      sharedBotRng,
      coordinator,
      episodeNum,
      args,
      frame: this._frame,
      plan: this._plan,
      isActor: this._isActor,
      episode: this,
      otherName: args.other_bot_name,
      siteCenter: sc.centerOf(this._frame.site).offset(0, 0.6, 0),
    };
    installSignals(ctx);
    this._ctx = ctx;
    sc.startPhaseChain({
      bot,
      rcon,
      sharedBotRng,
      coordinator,
      episodeNum,
      episodeInstance: this,
      args,
      prefix: `${this.constructor.name}_${iterationID}`,
      steps: this.buildSteps(ctx),
    });
  }

  async tearDownEpisode(bot, rcon, sharedBotRng, coordinator, episodeNum, args) {
    if (this._ctx && this._ctx.removeSignals) this._ctx.removeSignals();
    for (const c of ["forward", "back", "left", "right", "jump", "sneak", "sprint"]) bot.setControlState(c, false);
    await super.tearDownEpisode(bot, rcon, sharedBotRng, coordinator, episodeNum, args);
  }
}

module.exports = {
  DiverseEpisodeBase,
  REACH,
  REACH_HIGH,
  blockCenter,
  buildPlacements,
  cellOccupied,
  centerInCell,
  chooseRef,
  clearCoverCommands,
  digBlockAt,
  digPitInPlace,
  digPositions,
  distEyeTo,
  dualStep,
  ensureReach,
  digStairsOut,
  exitPit,
  stairColumns,
  stairPlan,
  eyePos,
  findSite,
  findStandSpot,
  glance,
  groundTop,
  hdist,
  holdItem,
  installSignals,
  isAirName,
  isInteractable,
  isLiquidName,
  isPassable,
  isSolid,
  otherInView,
  isTreeBlock,
  key2,
  key3,
  levelPadCommands,
  lookStep,
  makeHouse,
  moveTo,
  observeUntil,
  pickVantage,
  placeBlockAt,
  placementsMetadata,
  playerEntities,
  rand,
  refillPitInPlace,
  setMoves,
  squareFlat,
  standable,
  startNearest,
  surfaceAt,
  toolFor,
  towerUp,
  turnCost,
  // re-exports from state-common
  bindSteps: sc.bindSteps,
  approachStep: greetStep,
  greetStep,
  outroStep: sc.outroStep,
  lookAtPoint: sc.lookAtPoint,
  lookAtOtherBot: sc.lookAtOtherBot,
  note: sc.note,
  pick: sc.pick,
  randInt: sc.randInt,
  shuffle: sc.shuffle,
  sleep,
  startWatching: sc.startWatching,
  watchFor: sc.watchFor,
  useItemOnBlock: sc.useItemOnBlock,
  equip: sc.equip,
};
