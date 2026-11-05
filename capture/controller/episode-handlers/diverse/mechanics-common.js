/**
 * mechanics-common.js - shared base class and helpers for the round-2
 * world-mechanics episodes in this directory:
 *   farming, redstoneDoors, fallingBlocks, lightAndFire, waterWorks, snowIce
 *
 * Builds on ../state/state-common.js (same rules as the stateful episodes):
 *  - every /fill /setblock /time edit happens in setupEpisode, i.e. BEFORE
 *    the leader opens the world log and takes the start snapshot; during
 *    recording only the bots' own actions and natural game mechanics change
 *    the world;
 *  - both bots consume sharedBotRng identically (only in setupEpisode /
 *    planEpisode, never in the steps), so they compute the same plan;
 *  - the episode body is a barrier-synchronised phase chain.
 *
 * Additions over StateEpisodeBase:
 *  - site agreement: both bots compute a site candidate, then the follower
 *    adopts the leader's candidate (coordinator handshake). Works in normal
 *    terrain: when no flat patch exists the leader flattens one (pre-snapshot)
 *    -> every mechanics episode is WORKS_IN_NON_FLAT_WORLD;
 *  - a second handshake after the leader's setup commands, so the follower
 *    never teleports onto terrain it has not seen updated yet;
 *  - optional time of day / weather preference (TIME_PREFERENCE,
 *    PREFERRED_WEATHER), applied by the leader in setup when env-setup chose
 *    them at random;
 *  - per-step timeouts (a stuck step never blocks the barrier chain);
 *  - "observer" helpers that keep the partner AND the work spot in view;
 *  - readback helpers (inspectBlocks) for state changes the MirrorBot logger
 *    has no event for (redstone-driven lamp / door / piston updates): an
 *    empty-hand right click on a block without a use action makes the logger
 *    read back that block, its clicked-face neighbour and the blocks above
 *    and below it (WorldStateLogger.onInteract -> block_state records).
 */

const { Vec3 } = require("vec3");

const S = require("../state/state-common");
const { decidePrimaryBot, rconTp } = require("../../utils/coordination");
const { isWorldStateLeader } = require("../../utils/world-state");
const envSetup = require("../../utils/env-setup");
const { unequipHand } = require("../../primitives/items");
const { stopAll } = require("../../primitives/movement");
const { digBlock } = require("../../primitives/digging");

const {
  StateEpisodeBase,
  centerOf,
  equip,
  giveItems,
  groundAt,
  localJitterMs,
  lookAtPoint,
  makeFrame,
  note,
  sleep,
  startPhaseChain,
} = S;

const TIME_DUSK = Number.isFinite(envSetup.TIME_DUSK) ? envSetup.TIME_DUSK : 12500;
const TIME_NIGHT = Number.isFinite(envSetup.TIME_NIGHT) ? envSetup.TIME_NIGHT : 18000;
const timeLabel =
  typeof envSetup.timeOfDayLabel === "function"
    ? envSetup.timeOfDayLabel
    : (t) => (t < 12000 ? "day" : t < 13800 ? "dusk" : t < 22200 ? "night" : "dawn");

// ----------------------------------------------------------------------------
// Small utilities
// ----------------------------------------------------------------------------

function toVec(a) {
  if (!a) return null;
  if (a instanceof Vec3) return a;
  if (Array.isArray(a)) return new Vec3(a[0], a[1], a[2]);
  return new Vec3(a.x, a.y, a.z);
}

function toArr(v) {
  return v ? [v.x, v.y, v.z] : null;
}

/**
 * Sum the counts of duplicate item names ([[name, n], ...]); giveItems tops
 * an item up to a TARGET count, so duplicates would otherwise not add up.
 */
function mergeItems(list) {
  const m = new Map();
  for (const [name, n] of list || []) m.set(name, (m.get(name) || 0) + n);
  return [...m.entries()];
}

/** Resolve with `fallback` after `ms` (the promise keeps running). Never rejects. */
function withTimeout(promise, ms, fallback = null, label = "") {
  let timer;
  return Promise.race([
    Promise.resolve(promise).catch((err) => {
      console.log(`[mechanics] ${label} failed: ${err && err.message ? err.message : err}`);
      return fallback;
    }),
    new Promise((resolve) => {
      timer = setTimeout(() => {
        if (label) console.log(`[mechanics] ${label} timed out after ${ms} ms`);
        resolve(fallback);
      }, ms);
    }),
  ]).finally(() => clearTimeout(timer));
}

/**
 * Exchange one payload with the other bot (barrier semantics: resolves once
 * both bots reached this call). Same handshake as the phase chain: the
 * handler re-sends our payload, so whichever bot arrives last still releases
 * the other one. Resolves with the other bot's payload, or null on timeout.
 */
function exchange(coordinator, episodeNum, name, payload, timeoutMs = 20000) {
  return new Promise((resolve) => {
    let done = false;
    const timer = setTimeout(() => {
      if (!done) {
        done = true;
        console.log(`[mechanics] exchange ${name} timed out`);
        resolve(null);
      }
    }, timeoutMs);
    coordinator.onceEvent(name, episodeNum, async (other) => {
      coordinator.sendToOtherBot(name, payload, episodeNum, `${name} reply`);
      if (done) return;
      done = true;
      clearTimeout(timer);
      resolve(other || {});
    });
    coordinator.sendToOtherBot(name, payload, episodeNum, `${name} offer`);
  });
}

/** Biome name at a position as the client knows it (null if unknown). */
function biomeAt(bot, pos) {
  try {
    const b = bot.blockAt(pos);
    if (b && b.biome && b.biome.name) return b.biome.name;
    let id = b && b.biome ? b.biome.id : undefined;
    if ((id === undefined || id === null) && bot.world && typeof bot.world.getBiome === "function") {
      id = bot.world.getBiome(pos);
    }
    const reg = bot.registry && bot.registry.biomes;
    if (reg && id !== undefined && id !== null && reg[id]) return reg[id].name || null;
  } catch (_) {}
  return null;
}

function blockName(bot, pos) {
  const b = pos ? bot.blockAt(pos) : null;
  return b ? b.name : null;
}

function isAirName(name) {
  return name === "air" || name === "cave_air" || name === "void_air";
}

function isSolid(block) {
  return !!block && block.boundingBox === "block" && !block.name.includes("water") && !block.name.includes("lava");
}

/** Block properties (prismarine-block getProperties) or {}. */
function propsOf(block) {
  try {
    return (block && block.getProperties && block.getProperties()) || {};
  } catch (_) {
    return {};
  }
}

const FLAMMABLE_HINTS = ["log", "wood", "leaves", "planks", "wool", "hay", "bookshelf", "vine", "grass", "fern", "bush", "carpet", "scaffolding"];

/** Count flammable blocks in a box around `center` (leader-side safety checks). */
function countFlammable(bot, center, radius = 4, dyMin = -1, dyMax = 4) {
  let n = 0;
  for (let dx = -radius; dx <= radius; dx++) {
    for (let dz = -radius; dz <= radius; dz++) {
      for (let dy = dyMin; dy <= dyMax; dy++) {
        const b = bot.blockAt(center.offset(dx, dy, dz));
        if (b && FLAMMABLE_HINTS.some((h) => b.name.includes(h)) && b.name !== "grass_block") n++;
      }
    }
  }
  return n;
}

// ----------------------------------------------------------------------------
// Looking / observing (T8: keep the partner AND the work spot in view)
// ----------------------------------------------------------------------------

function otherEntity(ctx) {
  const p = ctx.bot.players[ctx.args.other_bot_name];
  return p && p.entity ? p.entity : null;
}

/** Point on the partner's body (chest height). */
function otherChest(ctx) {
  const e = otherEntity(ctx);
  return e ? e.position.offset(0, 1.1, 0) : null;
}

function eyeOf(bot) {
  return bot.entity.position.offset(0, bot.entity.eyeHeight || 1.62, 0);
}

/**
 * Look target that keeps both `focus` and the partner in the frame when the
 * angle between them (seen from our eyes) is small enough; otherwise
 * alternate between them (partner slightly preferred).
 */
function blendTarget(ctx, focus, w = 0.45) {
  const o = otherChest(ctx);
  const f = typeof focus === "function" ? focus() : focus;
  if (!f) return o;
  if (!o) return f;
  const eye = eyeOf(ctx.bot);
  const a = f.minus(eye);
  const b = o.minus(eye);
  const la = Math.sqrt(a.x * a.x + a.y * a.y + a.z * a.z) || 1e-6;
  const lb = Math.sqrt(b.x * b.x + b.y * b.y + b.z * b.z) || 1e-6;
  const cos = (a.x * b.x + a.y * b.y + a.z * b.z) / (la * lb);
  if (cos > Math.cos((62 * Math.PI) / 180)) {
    // direction-weighted blend (independent of the distances)
    const d = new Vec3(
      (a.x / la) * (1 - w) + (b.x / lb) * w,
      (a.y / la) * (1 - w) + (b.y / lb) * w,
      (a.z / la) * (1 - w) + (b.z / lb) * w,
    );
    const r = (la + lb) / 2;
    const ld = Math.sqrt(d.x * d.x + d.y * d.y + d.z * d.z) || 1e-6;
    return eye.plus(d.scaled(r / ld));
  }
  return Math.random() < 0.55 ? o : f;
}

/** Is the column next to us (sideways, 0.9 block) safe to strafe onto? */
function strafeIsSafe(bot, dirName, avoid = []) {
  const yaw = bot.entity.yaw;
  // mineflayer: forward = (-sin(yaw), -cos(yaw)); right = (-cos(yaw), +sin(yaw))
  const rx = -Math.cos(yaw);
  const rz = Math.sin(yaw);
  const s = dirName === "right" ? 1 : -1;
  const p = bot.entity.position;
  const dest = new Vec3(p.x + rx * s * 0.9, p.y, p.z + rz * s * 0.9);
  const feet = bot.blockAt(dest.floored());
  const head = bot.blockAt(dest.floored().offset(0, 1, 0));
  const below = bot.blockAt(dest.floored().offset(0, -1, 0));
  if (!feet || !head || !below) return false;
  // only plain air: never step onto pressure plates, snow layers, crops, fluids ...
  if (!isAirName(feet.name)) return false;
  if (!isAirName(head.name)) return false;
  if (!isSolid(below) || below.name === "farmland" || below.name.includes("ice")) return false;
  for (const a of avoid) {
    if (!a) continue;
    const dx = dest.x - (a.x + 0.5);
    const dz = dest.z - (a.z + 0.5);
    if (dx * dx + dz * dz < 2.6 * 2.6) return false;
  }
  return true;
}

/**
 * Keep looking at blendTarget(focus) with natural jitter and, optionally,
 * small sidesteps ("change the angle") until the returned stop() is awaited.
 */
function startObserving(ctx, focus, opts = {}) {
  const { w = 0.45, strafe = true, minMs = 650, maxMs = 1500, dps = 75, avoid = [] } = opts;
  const bot = ctx.bot;
  let active = true;
  const loop = (async () => {
    let lastStrafe = Date.now();
    while (active && !bot._episodeStopping) {
      const t = blendTarget(ctx, focus, w);
      if (t) {
        const j = new Vec3((Math.random() - 0.5) * 0.5, (Math.random() - 0.5) * 0.3, (Math.random() - 0.5) * 0.5);
        await lookAtPoint(bot, t.plus(j), dps);
      }
      if (strafe && Date.now() - lastStrafe > 3500 && Math.random() < 0.3) {
        const dir = Math.random() < 0.5 ? "left" : "right";
        const focusPos = typeof focus === "function" ? focus() : focus;
        if (strafeIsSafe(bot, dir, [focusPos, ...avoid])) {
          bot.setControlState(dir, true);
          await sleep(220 + Math.random() * 300);
          bot.setControlState(dir, false);
        }
        lastStrafe = Date.now();
      }
      const until = Date.now() + localJitterMs(minMs, maxMs);
      while (active && Date.now() < until) await sleep(50);
    }
  })();
  return async () => {
    active = false;
    await withTimeout(loop, 4000, null, "observe-stop");
    for (const c of ["left", "right", "forward", "back", "sprint", "jump"]) bot.setControlState(c, false);
  };
}

/** Observe for a fixed time. */
async function observe(ctx, focus, ms, opts = {}) {
  const stop = startObserving(ctx, focus, opts);
  await sleep(ms);
  await stop();
}

/**
 * Partner-side half of an "act / watch" step: observe `focus` until the
 * acting bot calls signalDone(ctx, name) (or maxMs elapses).
 */
async function observeUntilSignal(ctx, focus, name, maxMs = 20000, opts = {}) {
  let got = false;
  ctx.coordinator.onceEvent(`mech_sig_${name}`, ctx.episodeNum, async () => {
    got = true;
  });
  const stop = startObserving(ctx, focus, opts);
  const end = Date.now() + maxMs;
  while (!got && Date.now() < end && !ctx.bot._episodeStopping) await sleep(100);
  await stop();
  return got;
}

/**
 * Acting-side half of observeUntilSignal. Sent twice (the second copy covers
 * a watcher that registered late; duplicates are dropped by the once-handler).
 */
function signalDone(ctx, name) {
  ctx.coordinator.sendToOtherBot(`mech_sig_${name}`, {}, ctx.episodeNum, `signal ${name}`);
  setTimeout(() => {
    try {
      ctx.coordinator.sendToOtherBot(`mech_sig_${name}`, {}, ctx.episodeNum, `signal ${name} (repeat)`);
    } catch (_) {}
  }, 900);
}

/** Short eye contact with the partner. */
async function glance(ctx, ms = 900) {
  const o = otherChest(ctx);
  if (o) await lookAtPoint(ctx.bot, o.offset(0, 0.4, 0), 100);
  await sleep(ms);
}

// ----------------------------------------------------------------------------
// Movement
// ----------------------------------------------------------------------------

/** Walk (no digging / placing) next to a block column; never throws. */
async function walkTo(bot, pos, range = 0.8, timeoutMs = 7000) {
  const target = new Vec3(Math.floor(pos.x) + 0.5, pos.y, Math.floor(pos.z) + 0.5);
  const ok = await withTimeout(S.walkNear(bot, target, range, timeoutMs), timeoutMs + 1500, false, "walkTo");
  return ok;
}

// ----------------------------------------------------------------------------
// Block interactions
// ----------------------------------------------------------------------------

const FACES = {
  up: new Vec3(0, 1, 0),
  down: new Vec3(0, -1, 0),
  north: new Vec3(0, 0, -1),
  south: new Vec3(0, 0, 1),
  west: new Vec3(-1, 0, 0),
  east: new Vec3(1, 0, 0),
};

/** Click position (0..1) on a face, used as the cursor of use_item_on. */
function cursorFor(face, yOnSide = 0.3) {
  return new Vec3(
    face.x === 0 ? 0.5 : face.x > 0 ? 1 : 0,
    face.y === 0 ? yOnSide : face.y > 0 ? 1 : 0,
    face.z === 0 ? 0.5 : face.z > 0 ? 1 : 0,
  );
}

/** Face of block `pos` that points most towards the bot's eyes. */
function faceToward(bot, pos) {
  const eye = eyeOf(bot);
  const d = eye.minus(new Vec3(pos.x + 0.5, pos.y + 0.5, pos.z + 0.5));
  const ax = Math.abs(d.x);
  const ay = Math.abs(d.y);
  const az = Math.abs(d.z);
  if (ay >= ax && ay >= az) return d.y > 0 ? FACES.up : FACES.down;
  if (ax >= az) return d.x > 0 ? FACES.east : FACES.west;
  return d.z > 0 ? FACES.south : FACES.north;
}

async function holdItem(bot, item) {
  if (!item) {
    try {
      await unequipHand(bot);
    } catch (_) {}
    return true;
  }
  return equip(bot, item);
}

/**
 * Right click a block face with `item` in hand (null = empty hand). Looks at
 * the block smoothly first so mineflayer's own lookAt is a no-op (no snap).
 * @returns {Promise<boolean>} packet sent
 */
async function clickBlock(bot, pos, { item = null, face = null, cursor = null, dps = 120, swing = true } = {}) {
  const block = bot.blockAt(pos);
  if (!block || isAirName(block.name)) return false;
  if (!(await holdItem(bot, item))) return false;
  const f = face || faceToward(bot, pos);
  await lookAtPoint(bot, new Vec3(pos.x + 0.5, pos.y + 0.5, pos.z + 0.5), dps);
  bot.emit("action_use");
  const ok = await withTimeout(
    bot.activateBlock(block, f, cursor || cursorFor(f)).then(() => true),
    3000,
    false,
    `activateBlock ${block.name}`,
  );
  if (swing) {
    try {
      bot.swingArm("right");
    } catch (_) {}
  }
  return ok;
}

/** Wait (poll) until predicate(block at pos) is true. */
async function waitForBlock(bot, pos, predicate, timeoutMs = 1500) {
  const end = Date.now() + timeoutMs;
  while (Date.now() < end) {
    const b = bot.blockAt(pos);
    if (b && predicate(b)) return true;
    await sleep(60);
  }
  const b = bot.blockAt(pos);
  return !!b && predicate(b);
}

/** Candidate (reference block, face) pairs for placing at `target`. */
function placementRefs(bot, target, preferred = null) {
  const order = preferred
    ? [preferred]
    : [FACES.up, FACES.east, FACES.west, FACES.south, FACES.north, FACES.down];
  const out = [];
  for (const face of order) {
    const refPos = target.minus(face);
    const ref = bot.blockAt(refPos);
    if (ref && isSolid(ref)) out.push({ refPos, face });
  }
  // prefer side faces that look towards the bot when not placing on top
  return out;
}

/**
 * Place `item` so that it ends up at `target`, clicking a neighbour face.
 * @param {Object} opts - face: force the face of the reference block
 *   (e.g. FACES.down = hang under the block above), expect: block names that
 *   count as success, cursorY: click height on side faces
 */
async function placeItemAt(bot, target, item, { face = null, expect = null, cursorY = 0.3, refPos = null } = {}) {
  const want = expect ? (Array.isArray(expect) ? expect : [expect]) : null;
  const okName = (b) => (want ? want.includes(b.name) : !isAirName(b.name) && !b.name.includes("water"));
  let refs;
  if (refPos && face) refs = [{ refPos, face }];
  else refs = placementRefs(bot, target, face);
  if (!refs.length) {
    console.log(`[${bot.username}] placeItemAt(${item}) no reference block for ${target}`);
    return false;
  }
  for (const { refPos: rp, face: f } of refs.slice(0, 2)) {
    const before = bot.blockAt(target);
    const beforeKey = before ? `${before.name}:${before.stateId}` : "";
    if (!(await holdItem(bot, item))) return false;
    await clickBlock(bot, rp, { item, face: f, cursor: cursorFor(f, cursorY), swing: true });
    const changed = await waitForBlock(
      bot,
      target,
      (b) => `${b.name}:${b.stateId}` !== beforeKey && okName(b),
      1300,
    );
    if (changed) return true;
  }
  return false;
}

/** Dig a block (smooth look first); bounded in time. */
async function digAt(bot, pos, timeoutMs = 9000) {
  const b = bot.blockAt(pos);
  if (!b || isAirName(b.name)) return true;
  await lookAtPoint(bot, new Vec3(pos.x + 0.5, pos.y + 0.5, pos.z + 0.5), 120);
  const ok = await withTimeout(digBlock(bot, pos), timeoutMs, false, `dig ${b.name}`);
  return !!ok && isAirName(blockName(bot, pos) || "air");
}

/** Mineflayer yaw / pitch (radians) from the eyes towards a point. */
function anglesTo(bot, point) {
  const eye = eyeOf(bot);
  const dx = point.x - eye.x;
  const dy = point.y - eye.y;
  const dz = point.z - eye.z;
  return { yaw: Math.atan2(-dx, -dz), pitch: Math.atan2(dy, Math.sqrt(dx * dx + dz * dz)) };
}

function yawDiff(a, b) {
  let d = (a - b) % (2 * Math.PI);
  if (d > Math.PI) d -= 2 * Math.PI;
  if (d < -Math.PI) d += 2 * Math.PI;
  return Math.abs(d);
}

/**
 * Right click "in the air" (use_item) with `item` while looking at `point`:
 * buckets and throwables, the server ray-traces from the eyes with the
 * rotation of the packet. Unlike state-common.useHeldItemTowards the packet
 * carries the exact rotation towards `point` (the smooth look may end a
 * little short of it), and a remaining difference is closed first so the
 * camera shows what the server used.
 */
async function useItemTowards(bot, item, point) {
  if (item && !(await equip(bot, item))) return false;
  await lookAtPoint(bot, point, 110);
  await sleep(120);
  const { yaw, pitch } = anglesTo(bot, point);
  if (yawDiff(bot.entity.yaw, yaw) > 0.015 || Math.abs(bot.entity.pitch - pitch) > 0.015) {
    try {
      await withTimeout(bot.look(yaw, pitch, true), 600, null, "look");
    } catch (_) {}
    await sleep(60);
  }
  const mcYaw = ((((Math.PI - yaw) * 180) / Math.PI + 540) % 360) - 180;
  const mcPitch = (-pitch * 180) / Math.PI;
  bot.emit("action_use");
  try {
    bot._client.write("use_item", { hand: 0, sequence: 0, rotation: { x: mcYaw, y: mcPitch } });
    bot.swingArm("right");
    return true;
  } catch (err) {
    console.log(`[${bot.username}] use_item failed: ${err.message}`);
    return false;
  }
}

/** Blocks with a right-click action: never "inspect" these with an empty hand. */
const INTERACTIVE_HINTS = [
  "door",
  "trapdoor",
  "fence_gate",
  "lever",
  "button",
  "note_block",
  "repeater",
  "comparator",
  "redstone_wire",
  "bell",
  "candle",
  "cake",
  "chest",
  "barrel",
  "furnace",
  "crafting_table",
  "anvil",
  "bed",
  "jukebox",
  "daylight_detector",
  "composter",
  "lectern",
  "campfire",
  "respawn_anchor",
  "flower_pot",
  "cauldron",
];

function isInteractive(name) {
  if (!name) return true;
  if (name === "iron_door" || name === "iron_trapdoor") return false; // no hand use
  return INTERACTIVE_HINTS.some((h) => name.includes(h));
}

/**
 * Logger readback: right click (empty hand) each position that holds a
 * block without a use action. The MirrorBot logger then re-reads the block,
 * its clicked-face neighbour and the blocks directly above and below
 * (block_state records), which covers redstone-driven changes (lamp lit,
 * iron door open, piston head) that have no Bukkit event of their own in the
 * logger. Positions with interactive blocks / air are skipped.
 * @returns {Promise<string[]>} names of the inspected blocks
 */
async function inspectBlocks(ctx, positions) {
  const { bot } = ctx;
  const done = [];
  await holdItem(bot, null);
  for (const p of positions) {
    if (bot._episodeStopping) break;
    const pos = toVec(p);
    const name = blockName(bot, pos);
    if (!name || isAirName(name) || isInteractive(name) || name.includes("water") || name.includes("lava")) continue;
    await clickBlock(bot, pos, { item: null, swing: false, dps: 140 });
    done.push(name);
    await sleep(localJitterMs(200, 380));
  }
  return done;
}

// ----------------------------------------------------------------------------
// Site search (tree-free) and leaf persistence
// ----------------------------------------------------------------------------

const LEAF_TYPES = [
  "oak_leaves",
  "spruce_leaves",
  "birch_leaves",
  "jungle_leaves",
  "acacia_leaves",
  "dark_oak_leaves",
  "mangrove_leaves",
  "cherry_leaves",
  "azalea_leaves",
  "flowering_azalea_leaves",
];

function isTreeName(name) {
  return !!name && (name.endsWith("_log") || name.endsWith("_leaves") || name.endsWith("_wood") || name === "mangrove_roots");
}

function boxHasTree(bot, x1, y1, z1, x2, y2, z2) {
  for (let x = x1; x <= x2; x++) {
    for (let z = z1; z <= z2; z++) {
      for (let y = y1; y <= y2; y++) {
        const b = bot.blockAt(new Vec3(x, y, z));
        if (b && isTreeName(b.name)) return true;
      }
    }
  }
  return false;
}

function coverLike(b) {
  if (!b) return true;
  if (isAirName(b.name)) return true;
  if (b.name.includes("water") || b.name.includes("lava")) return false;
  return b.boundingBox === "empty";
}

function solidGround(b) {
  return !!b && b.boundingBox === "block" && !b.name.includes("leaves") && !b.name.includes("water") && !b.name.includes("lava") && b.name !== "snow";
}

function squareFlat(bot, cx, gy, cz, half) {
  for (let dx = -half; dx <= half; dx++) {
    for (let dz = -half; dz <= half; dz++) {
      if (!solidGround(bot.blockAt(new Vec3(cx + dx, gy, cz + dz)))) return false;
      for (let dy = 1; dy <= 3; dy++) {
        if (!coverLike(bot.blockAt(new Vec3(cx + dx, gy + dy, cz + dz)))) return false;
      }
    }
  }
  return true;
}

/**
 * Like state-common.findFlatSite, but the (2*half+3)^2 x clearHeight volume
 * above the site must not contain logs or leaves: clearing it would cut
 * trees, and leaves that later decay drop whatever rests on them (snow
 * layers) without a logged event.
 */
function findTreeFreeSite(bot, center, half, radius, clearHeight) {
  const cx = Math.floor(center.x);
  const cz = Math.floor(center.z);
  for (let r = 0; r <= radius; r++) {
    for (let dx = -r; dx <= r; dx++) {
      for (let dz = -r; dz <= r; dz++) {
        if (Math.max(Math.abs(dx), Math.abs(dz)) !== r) continue;
        const g = groundAt(bot, cx + dx, cz + dz);
        if (!g) continue;
        if (!squareFlat(bot, cx + dx, g.y, cz + dz, half)) continue;
        const h = half + 1;
        if (boxHasTree(bot, cx + dx - h, g.y + 1, cz + dz - h, cx + dx + h, g.y + clearHeight + 1, cz + dz + h)) continue;
        return new Vec3(cx + dx, g.y, cz + dz);
      }
    }
  }
  return null;
}

/**
 * Pre-snapshot: make the natural leaves around a flattened pad persistent,
 * so leaves whose logs were cleared never decay during the episode (their
 * decay is logged, but snow layers etc. resting on them would vanish
 * silently). Their `distance` is recomputed within a few ticks of the fill,
 * long before the snapshot.
 */
function leafPersistenceCommands(site, reach) {
  const x1 = site.x - reach;
  const x2 = site.x + reach;
  const z1 = site.z - reach;
  const z2 = site.z + reach;
  const y1 = site.y - 3;
  const y2 = site.y + 24;
  return LEAF_TYPES.map(
    (l) => `fill ${x1} ${y1} ${z1} ${x2} ${y2} ${z2} minecraft:${l}[persistent=true] replace minecraft:${l}[persistent=false]`,
  );
}

/**
 * Pre-snapshot (normal worlds only): set every kelp head in the snapshot box
 * to its maximum age, so it cannot grow during the episode. Kelp growth is
 * logged for the new head only; the old head turning into kelp_plant has no
 * event and would show up as a replay mismatch (seen in live tests next to
 * rivers / oceans). Same look, the plant just stops growing.
 */
function kelpFreezeCommands(args, site) {
  if (String(args.world_type || "flat").toLowerCase() === "flat") return [];
  const r = (Number(args.world_snapshot_radius) || 32) + 1;
  const y1 = site.y - (Number(args.world_snapshot_down) || 24) - 1;
  const y2 = Math.min(site.y + (Number(args.world_snapshot_up) || 40) + 1, 62);
  if (y2 < y1) return [];
  const cmds = [];
  const step = 32;
  const dyMax = Math.max(1, Math.floor(32768 / (step * step)));
  for (let x = site.x - r; x <= site.x + r; x += step) {
    for (let z = site.z - r; z <= site.z + r; z += step) {
      for (let y = y1; y <= y2; y += dyMax) {
        const xe = Math.min(x + step - 1, site.x + r);
        const ze = Math.min(z + step - 1, site.z + r);
        const ye = Math.min(y + dyMax - 1, y2);
        cmds.push(`fill ${x} ${y} ${z} ${xe} ${ye} ${ze} minecraft:kelp[age=25] replace minecraft:kelp`);
      }
    }
  }
  return cmds;
}

// ----------------------------------------------------------------------------
// Base class
// ----------------------------------------------------------------------------

/**
 * Site-based mechanics episode. Subclasses implement
 *   planEpisode(bot, rng, args, frame, isActor, siteInfo) -> {
 *     actorStand, observerStand (Vec3 columns), items: {actor:[], observer:[]},
 *     leaderSetupCommands?: string[], metadata: {...}, ...
 *   }
 * (must consume `rng` identically regardless of isActor) and
 *   buildSteps(ctx) -> [{name, run, timeoutMs?}] (run is called with this = ctx).
 * Optional: siteInfo(bot, site, flatten) -> JSON (computed by the leader and
 * shared with the follower).
 */
class MechanicsEpisodeBase extends StateEpisodeBase {
  static INIT_MIN_BOTS_DISTANCE = 5;
  static INIT_MAX_BOTS_DISTANCE = 9;
  static WORKS_IN_NON_FLAT_WORLD = true;
  static SITE_HALF_SIZE = 4;
  static SITE_SEARCH_RADIUS = 16;
  static CLEAR_HEIGHT = 4;
  // { night: p, dusk: q }: probability of switching a randomly-chosen DAY
  // episode to night / dusk (leader applies `time set` before the snapshot).
  // (Not named PREFERRED_TIME: env-setup.js reads that static as a label list.)
  static TIME_PREFERENCE = null;
  // env-setup.js format ("clear", ["clear", "snow"], ...). When env-setup did
  // not apply it (policy "random") and the chosen weather is not allowed,
  // the leader switches to the first allowed weather before the snapshot.
  static PREFERRED_WEATHER = undefined;
  static STEP_TIMEOUT_MS = 30000;

  siteInfo(bot, site, flatten) {
    return {};
  }

  _siteCandidate(bot, mid, half) {
    const C = this.constructor;
    const site = findTreeFreeSite(bot, mid, half, C.SITE_SEARCH_RADIUS, C.CLEAR_HEIGHT);
    if (site) return { site, flatten: false };
    const g = groundAt(bot, Math.floor(mid.x), Math.floor(mid.z));
    if (!g) return null;
    return { site: g, flatten: true };
  }

  /** Pre-snapshot commands that turn the area around `site` into a flat pad. */
  _flattenCommands(bot, site, half) {
    const x1 = site.x - half;
    const x2 = site.x + half;
    const z1 = site.z - half;
    const z2 = site.z + half;
    const gb = bot.blockAt(site);
    let top = "grass_block";
    if (gb && isSolid(gb) && /^(grass_block|dirt|podzol|sand|red_sand|snow_block|stone|mud|moss_block|coarse_dirt|terracotta|sandstone|mycelium)$/.test(gb.name)) {
      top = gb.name;
    }
    const sub = top === "sand" || top === "red_sand" ? "sandstone" : "dirt";
    return [
      `fill ${x1} ${site.y - 3} ${z1} ${x2} ${site.y - 1} ${z2} minecraft:${sub}`,
      `fill ${x1} ${site.y} ${z1} ${x2} ${site.y} ${z2} minecraft:${top}`,
      `fill ${x1} ${site.y + 1} ${z1} ${x2} ${site.y + 6} ${z2} minecraft:air`,
    ];
  }

  async setupEpisode(bot, rcon, sharedBotRng, coordinator, episodeNum, args, botPosition, otherBotPosition) {
    const C = this.constructor;
    const leader = isWorldStateLeader(bot, args);
    const mid = new Vec3(
      (botPosition.x + otherBotPosition.x) / 2,
      (botPosition.y + otherBotPosition.y) / 2,
      (botPosition.z + otherBotPosition.z) / 2,
    );
    const half = C.SITE_HALF_SIZE;

    // 1) site: both compute a candidate, the follower adopts the leader's
    const cand = this._siteCandidate(bot, mid, half);
    const mine = cand
      ? { site: toArr(cand.site), flatten: cand.flatten, info: this.siteInfo(bot, cand.site, cand.flatten) || {} }
      : { none: true };
    const theirs = await exchange(coordinator, episodeNum, "mech_site", mine, 25000);
    let chosen = mine;
    if (!leader && theirs && theirs.site) chosen = theirs;
    if (!chosen || !chosen.site) throw new Error(`[${C.name}] no site near ${mid}`);
    const site = toVec(chosen.site);
    const siteInfo = chosen.info || {};

    // 2) shared random choices (identical order on both bots)
    const isActor = decidePrimaryBot(bot, sharedBotRng, args);
    const axis = sharedBotRng() < 0.5 ? "x" : "z";
    const side = sharedBotRng() < 0.5 ? -1 : 1;
    const uTime = sharedBotRng();
    const frame = makeFrame(site, axis, side);
    const plan = this.planEpisode(bot, sharedBotRng, args, frame, isActor, siteInfo);

    // preferred time of day (only when env-setup picked a random DAY time)
    let timeChoice = null;
    const envMeta = this._envMetadata || {};
    const pref = C.TIME_PREFERENCE;
    if (pref && envMeta.time_policy === "random" && timeLabel(Number(envMeta.time_of_day)) === "day") {
      if (uTime < (pref.night || 0)) timeChoice = TIME_NIGHT;
      else if (uTime < (pref.night || 0) + (pref.dusk || 0)) timeChoice = TIME_DUSK;
    }
    // preferred weather (e.g. farming: rain would hydrate farmland, which the
    // logger cannot see; lightAndFire: rain puts the fire out)
    let weatherChoice = null;
    const wantWeather = C.PREFERRED_WEATHER
      ? (Array.isArray(C.PREFERRED_WEATHER) ? C.PREFERRED_WEATHER : String(C.PREFERRED_WEATHER).split(","))
          .map((w) => String(w).trim().toLowerCase())
          .filter(Boolean)
      : null;
    const curWeather = String(envMeta.weather_requested || envMeta.weather || "clear").toLowerCase();
    if (wantWeather && wantWeather.length && !wantWeather.includes(curWeather) && envMeta.weather_policy !== "fixed") {
      weatherChoice = wantWeather[0];
    }

    // 3) leader: world edits BEFORE the start snapshot
    const pad = half + 1;
    if (leader) {
      const cmds = [];
      if (chosen.flatten) {
        cmds.push(...this._flattenCommands(bot, site, pad));
        // the flatten fill may cut trees: keep the remaining leaves from decaying
        if (boxHasTree(bot, site.x - pad, site.y + 1, site.z - pad, site.x + pad, site.y + 8, site.z + pad)) {
          cmds.push(...leafPersistenceCommands(site, pad + 7));
        }
      }
      cmds.push(
        `fill ${site.x - half} ${frame.level} ${site.z - half} ${site.x + half} ${frame.level + C.CLEAR_HEIGHT - 1} ${site.z + half} minecraft:air`,
      );
      cmds.push(...(plan.leaderSetupCommands || []));
      cmds.push(...kelpFreezeCommands(args, site));
      if (timeChoice !== null) cmds.push(`time set ${timeChoice}`);
      if (weatherChoice !== null) cmds.push(`weather ${weatherChoice === "snow" ? "rain" : weatherChoice}`);
      for (const cmd of cmds) {
        try {
          const r = await rcon.send(cmd);
          console.log(`[${bot.username}] setup '${cmd}' -> ${String(r).slice(0, 160)}`);
        } catch (err) {
          console.log(`[${bot.username}] setup '${cmd}' failed: ${err.message}`);
        }
      }
      // the rain level ramps down 0.01 / tick: wait until it is below the
      // "raining" threshold before the start snapshot
      if (weatherChoice !== null) await sleep(4500);
    }
    // The weather override replaces the one applied in the teleport phase:
    // remember it (applyEpisodeEnvironment waits for the rain ramp only when
    // the weather changes) and re-apply the base randomTickSpeed policy for
    // the new weather (it may depend on the weather, EPISODE_RANDOM_TICK_SPEED=auto).
    let tickOverride = null;
    if (weatherChoice !== null) {
      const newWeather = weatherChoice === "snow" ? "rain" : weatherChoice;
      bot._lastAppliedWeather = newWeather;
      if (typeof envSetup.randomTickSpeedFor === "function") {
        const t = envSetup.randomTickSpeedFor(args, C, newWeather);
        if (t !== null && t !== envMeta.random_tick_speed_base) {
          tickOverride = t;
          if (leader) {
            try {
              const r = await rcon.send(`gamerule randomTickSpeed ${t}`);
              console.log(`[${bot.username}] setup 'gamerule randomTickSpeed ${t}' -> ${String(r).slice(0, 120)}`);
            } catch (err) {
              console.log(`[${bot.username}] gamerule randomTickSpeed ${t} failed: ${err.message}`);
            }
          }
        }
      }
    }
    // 4) barrier: the follower waits until the leader's edits are done
    await exchange(coordinator, episodeNum, "mech_ready", {}, 30000);
    await sleep(700);

    if (weatherChoice !== null && typeof envSetup.addEnvMetadata === "function") {
      envSetup.addEnvMetadata(this, {
        weather: weatherChoice === "snow" ? "rain" : weatherChoice,
        weather_requested: weatherChoice,
        weather_policy: "episode_preferred",
        weather_base: curWeather,
        ...(weatherChoice === "clear" ? { precipitation: "none", rain_level: 0, thunder_level: 0 } : {}),
        ...(tickOverride !== null
          ? { random_tick_speed_base: tickOverride, random_tick_speed_base_before_weather: envMeta.random_tick_speed_base ?? null }
          : {}),
      });
    }
    if (timeChoice !== null && typeof envSetup.addEnvMetadata === "function") {
      envSetup.addEnvMetadata(this, {
        time_of_day: timeChoice,
        time_of_day_label: timeLabel(timeChoice),
        time_policy: "episode_preferred",
        time_policy_base: envMeta.time_policy,
        time_of_day_base: envMeta.time_of_day,
      });
    }

    // 5) teleport onto the stand columns, items
    const myCol = isActor ? plan.actorStand : plan.observerStand;
    const otherCol = isActor ? plan.observerStand : plan.actorStand;
    const inPad = (c) => chosen.flatten && Math.abs(c.x - site.x) <= pad && Math.abs(c.z - site.z) <= pad;
    const standOf = (c) => (inPad(c) ? new Vec3(c.x + 0.5, frame.level, c.z + 0.5) : S.standAt(bot, c, frame.level));
    const me = standOf(myCol);
    const other = standOf(otherCol);
    await rconTp(rcon, bot.username, me.x, me.y, me.z);
    await sleep(1200);
    await giveItems(bot, rcon, mergeItems(isActor ? plan.items.actor : plan.items.observer));
    await unequipHand(bot);

    this._frame = frame;
    this._isActor = isActor;
    this._plan = plan;
    this._worldStateCenter = centerOf(site);
    this._stateMetadata = {
      episode: C.name,
      site: [site.x, site.y, site.z],
      site_flattened: Boolean(chosen.flatten),
      // client-side biome at the site (the episode-level `biome` key is the
      // server-resolved one, written by episodes-loop for both bots)
      biome_client: siteInfo.biome || biomeAt(bot, site.offset(0, 1, 0)),
      axis,
      side,
      actor: isActor ? bot.username : args.other_bot_name,
      observer: isActor ? args.other_bot_name : bot.username,
      actor_stand: toArr(plan.actorStand),
      observer_stand: toArr(plan.observerStand),
      observer_distance: Number(
        Math.sqrt(
          (plan.actorStand.x - plan.observerStand.x) ** 2 + (plan.actorStand.z - plan.observerStand.z) ** 2,
        ).toFixed(2),
      ),
      preferred_time: timeChoice !== null ? timeLabel(timeChoice) : null,
      preferred_weather: weatherChoice,
      ...plan.metadata,
    };
    console.log(`[${bot.username}] ${C.name} setup: ${JSON.stringify(this._stateMetadata)}`);
    return { botPositionNew: me, otherBotPositionNew: other };
  }

  async entryPoint(bot, rcon, sharedBotRng, coordinator, iterationID, episodeNum, args) {
    const C = this.constructor;
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
      siteCenter: centerOf(this._frame.site).offset(0, 0.6, 0),
      deadline: 0,
      note: (phase, extra = {}) => note(rcon, args, bot, episodeNum, phase, extra),
      timeUp() {
        return bot._episodeStopping || (this.deadline && Date.now() > this.deadline);
      },
    };
    const steps = this.buildSteps(ctx).map((s) => ({
      name: s.name,
      run: async (otherData, ourPrev) => {
        if (bot._episodeStopping) return {};
        const ms = s.timeoutMs || C.STEP_TIMEOUT_MS;
        ctx.deadline = Date.now() + ms - 1500;
        const res = await withTimeout(
          Promise.resolve().then(() => s.run.call(ctx, otherData, ourPrev)),
          ms,
          { timeout: true },
          `step ${s.name}`,
        );
        stopAll(bot);
        return res || {};
      },
    }));
    startPhaseChain({
      bot,
      rcon,
      sharedBotRng,
      coordinator,
      episodeNum,
      episodeInstance: this,
      args,
      prefix: `${C.name}_${iterationID}`,
      steps,
    });
  }

  async tearDownEpisode(bot, rcon, sharedBotRng, coordinator, episodeNum, args) {
    for (const c of ["left", "right", "forward", "back", "sprint", "jump", "sneak"]) {
      try {
        bot.setControlState(c, false);
      } catch (_) {}
    }
    await super.tearDownEpisode(bot, rcon, sharedBotRng, coordinator, episodeNum, args);
  }
}

/** Common first step: both face the site, short eye contact. */
function introStep(name = "intro") {
  return {
    name,
    timeoutMs: 8000,
    run: async function () {
      S.safePathfinder(this.bot);
      await lookAtPoint(this.bot, this.siteCenter, 80);
      await sleep(localJitterMs(400, 800));
      await glance(this, localJitterMs(500, 900));
      return {};
    },
  };
}

/** Common last step: eye contact. */
function outroStep(name = "outro") {
  return {
    name,
    timeoutMs: 8000,
    run: async function () {
      await holdItem(this.bot, null);
      await glance(this, localJitterMs(1200, 1800));
      return {};
    },
  };
}

module.exports = {
  FACES,
  MechanicsEpisodeBase,
  TIME_DUSK,
  TIME_NIGHT,
  biomeAt,
  blendTarget,
  blockName,
  clickBlock,
  countFlammable,
  cursorFor,
  digAt,
  exchange,
  faceToward,
  glance,
  holdItem,
  inspectBlocks,
  introStep,
  isAirName,
  isInteractive,
  isSolid,
  observe,
  observeUntilSignal,
  otherChest,
  otherEntity,
  outroStep,
  placeItemAt,
  propsOf,
  signalDone,
  startObserving,
  toArr,
  toVec,
  useItemTowards,
  waitForBlock,
  walkTo,
  withTimeout,
  // re-exports from state-common
  centerOf,
  equip,
  localJitterMs,
  lookAtPoint,
  mergeItems,
  pick: S.pick,
  randInt: S.randInt,
  shuffle: S.shuffle,
  sleep,
  isFluidSource: S.isFluidSource,
};
