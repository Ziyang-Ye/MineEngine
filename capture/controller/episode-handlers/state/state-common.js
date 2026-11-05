/**
 * state-common.js - Shared helpers for the stateful-capture episodes
 * (igniteAndExtinguish, iceNearFire, tntBlast, lavaWater, buildAndBreak).
 *
 * Design rules shared by all of these episodes:
 *  - All world edits done with server commands (/fill to clear snow or grass
 *    on the work site) happen in setupEpisode, i.e. BEFORE the leader opens
 *    the world-state log and takes the start snapshot. During recording the
 *    world is only changed by the bots' own actions (place/break/use) and by
 *    game physics, so every change is visible in world_events.jsonl.
 *  - Both bots consume sharedBotRng in exactly the same order, so they agree
 *    on the site, the roles and every random choice without messaging.
 *  - The episode body is a chain of barrier-synchronised phases (same
 *    handshake as the upstream episodes): in each phase the actor acts and
 *    the other bot keeps its camera on the site, so both views record the
 *    same state change.
 */

const { Vec3 } = require("vec3");
const {
  goals: { GoalNear },
} = require("mineflayer-pathfinder");

const { placeAt } = require("../../primitives/building");
const { digBlock } = require("../../primitives/digging");
const {
  ensureBotHasEnough,
  ensureItemInHand,
  unequipHand,
} = require("../../primitives/items");
const {
  gotoWithTimeout,
  initializePathfinder,
  land_pos,
  lookSmooth,
  stopAll,
} = require("../../primitives/movement");
const { decidePrimaryBot, rconTp } = require("../../utils/coordination");
const { sleep } = require("../../utils/helpers");
const {
  isWorldStateLeader,
  worldStateNote,
} = require("../../utils/world-state");

// ----------------------------------------------------------------------------
// RNG helpers (sharedBotRng: identical on both bots; Math.random: local timing)
// ----------------------------------------------------------------------------

function randInt(rng, lo, hi) {
  return lo + Math.floor(rng() * (hi - lo + 1));
}

function pick(rng, arr) {
  return arr[Math.floor(rng() * arr.length)];
}

function shuffle(rng, arr) {
  const a = arr.slice();
  for (let i = a.length - 1; i > 0; i--) {
    const j = Math.floor(rng() * (i + 1));
    [a[i], a[j]] = [a[j], a[i]];
  }
  return a;
}

function localJitterMs(lo, hi) {
  return lo + Math.random() * (hi - lo);
}

// ----------------------------------------------------------------------------
// Geometry / site selection
// ----------------------------------------------------------------------------

const COVER_NAMES = new Set([
  "snow",
  "short_grass",
  "grass",
  "tall_grass",
  "fern",
  "large_fern",
  "dead_bush",
  "dandelion",
  "poppy",
]);

function isAirName(name) {
  return name === "air" || name === "cave_air" || name === "void_air";
}

/** Replaceable surface cover (snow layers, grass, flowers). */
function isCover(block) {
  if (!block) return false;
  if (COVER_NAMES.has(block.name)) return true;
  return !isAirName(block.name) && block.boundingBox === "empty" && !block.name.includes("water") && !block.name.includes("lava");
}

function isAirOrCover(block) {
  return !block || isAirName(block.name) || isCover(block);
}

function isSolidGround(block) {
  if (!block) return false;
  if (block.boundingBox !== "block") return false;
  if (block.name.includes("leaves") || block.name.includes("water") || block.name.includes("lava")) return false;
  if (block.name === "snow") return false; // snow layer
  return true;
}

/**
 * Highest solid, non-cover block of a column (Vec3) or null if unloaded.
 */
function groundAt(bot, x, z) {
  let g = land_pos(bot, x, z);
  if (!g) return null;
  for (let i = 0; i < 4; i++) {
    const b = bot.blockAt(g);
    if (b && isCover(b)) g = g.offset(0, -1, 0);
    else break;
  }
  return g;
}

function squareIsFlat(bot, cx, gy, cz, half) {
  for (let dx = -half; dx <= half; dx++) {
    for (let dz = -half; dz <= half; dz++) {
      const base = new Vec3(cx + dx, gy, cz + dz);
      if (!isSolidGround(bot.blockAt(base))) return false;
      for (let dy = 1; dy <= 3; dy++) {
        if (!isAirOrCover(bot.blockAt(base.offset(0, dy, 0)))) return false;
      }
    }
  }
  return true;
}

/**
 * Find a flat (2*half+1)^2 patch of solid ground with 3 free blocks above it,
 * scanning rings outward from `center`. Deterministic, so both bots agree.
 * @returns {Vec3|null} ground block position of the site centre
 */
function findFlatSite(bot, center, half = 3, searchRadius = 12) {
  const cx = Math.floor(center.x);
  const cz = Math.floor(center.z);
  for (let r = 0; r <= searchRadius; r++) {
    for (let dx = -r; dx <= r; dx++) {
      for (let dz = -r; dz <= r; dz++) {
        if (Math.max(Math.abs(dx), Math.abs(dz)) !== r) continue;
        const g = groundAt(bot, cx + dx, cz + dz);
        if (!g) continue;
        if (squareIsFlat(bot, cx + dx, g.y, cz + dz, half)) {
          return new Vec3(cx + dx, g.y, cz + dz);
        }
      }
    }
  }
  return null;
}

/**
 * Layout frame around a site: u = axis from the site towards the actor,
 * p = perpendicular axis. Positions are integer block coordinates.
 */
function makeFrame(site, axis, side) {
  const u = axis === "x" ? new Vec3(side, 0, 0) : new Vec3(0, 0, side);
  const p = axis === "x" ? new Vec3(0, 0, 1) : new Vec3(1, 0, 0);
  const level = site.y + 1; // feet / placement level
  const at = (du, dp, dy = 0) =>
    new Vec3(site.x + u.x * du + p.x * dp, level + dy, site.z + u.z * du + p.z * dp);
  return { site, axis, side, u, p, level, at };
}

/** Standing position (block centre) on the ground of column (x, z). */
function standAt(bot, col, fallbackY) {
  const g = groundAt(bot, col.x, col.z);
  const y = g ? g.y + 1 : fallbackY;
  return new Vec3(col.x + 0.5, y, col.z + 0.5);
}

/** Centre of a block. */
function centerOf(pos) {
  return new Vec3(pos.x + 0.5, pos.y + 0.5, pos.z + 0.5);
}

// ----------------------------------------------------------------------------
// Looking / watching
// ----------------------------------------------------------------------------

/** Smooth look from the eyes (not the feet) at a world point. */
async function lookAtPoint(bot, point, dps = 90, options = {}) {
  const eye = bot.entity.position.offset(0, bot.entity.eyeHeight || 1.62, 0);
  const dx = point.x - eye.x;
  const dy = point.y - eye.y;
  const dz = point.z - eye.z;
  const yaw = Math.atan2(-dx, -dz);
  const pitch = Math.atan2(dy, Math.sqrt(dx * dx + dz * dz));
  try {
    await lookSmooth(bot, yaw, pitch, dps, { randomized: true, ...options });
  } catch (err) {
    console.log(`[${bot.username}] look failed: ${err.message}`);
  }
}

async function lookAtOtherBot(bot, otherName, dps = 90) {
  const other = bot.players[otherName]?.entity;
  if (other) {
    await lookAtPoint(bot, other.position.offset(0, 1.5, 0), dps);
  }
}

/**
 * Keep the camera on a (possibly moving) target with small natural jitter
 * until the returned stop() is awaited.
 * @param {*} bot
 * @param {Function} getTarget - () => Vec3|null
 * @returns {Function} async stop()
 */
function startWatching(bot, getTarget, { minMs = 700, maxMs = 1600, jitter = 0.7, dps = 70 } = {}) {
  let active = true;
  const loop = (async () => {
    while (active) {
      const t = getTarget();
      if (t) {
        const j = new Vec3(
          (Math.random() - 0.5) * jitter,
          (Math.random() - 0.5) * jitter * 0.5,
          (Math.random() - 0.5) * jitter,
        );
        await lookAtPoint(bot, t.plus(j), dps);
      }
      const until = Date.now() + localJitterMs(minMs, maxMs);
      while (active && Date.now() < until) await sleep(50);
    }
  })();
  return async () => {
    active = false;
    await loop;
  };
}

/** Watch a target for a fixed duration. */
async function watchFor(bot, getTarget, ms) {
  const stop = startWatching(bot, getTarget);
  await sleep(ms);
  await stop();
}

// ----------------------------------------------------------------------------
// Movement
// ----------------------------------------------------------------------------

/**
 * Pathfinder movements that never change the world: no digging, no
 * scaffolding / towering. Used for every walk in the state episodes (and for
 * the reach "nudges" inside the upstream placeAt helper).
 */
function safePathfinder(bot) {
  try {
    const m = initializePathfinder(bot, {
      allowSprinting: true,
      allowParkour: false,
      canDig: false,
      canPlaceOn: false,
      scafoldingBlocks: [],
    });
    m.canDig = false;
    m.allow1by1towers = false;
    m.scafoldingBlocks = [];
    return m;
  } catch (err) {
    console.log(`[${bot.username}] pathfinder init failed: ${err.message}`);
    return null;
  }
}

/**
 * Walk (pathfinder, no digging / no block placing) to within `range` of pos.
 * Never throws.
 */
async function walkNear(bot, pos, range = 1, timeoutMs = 6000) {
  try {
    safePathfinder(bot);
    await gotoWithTimeout(bot, new GoalNear(pos.x, pos.y, pos.z, range), {
      timeoutMs,
    });
    return true;
  } catch (err) {
    console.log(`[${bot.username}] walkNear(${pos}) did not finish: ${err.message}`);
    return false;
  } finally {
    if (bot.pathfinder) bot.pathfinder.setGoal(null);
    stopAll(bot);
  }
}

// ----------------------------------------------------------------------------
// Items / interactions
// ----------------------------------------------------------------------------

/** Give each [itemName, count] via RCON (same mechanism as other episodes). */
async function giveItems(bot, rcon, items) {
  for (const [name, count] of items) {
    if (count > 0) await ensureBotHasEnough(bot, rcon, name, count);
  }
}

async function equip(bot, itemName) {
  try {
    await ensureItemInHand(bot, itemName);
    await sleep(150);
    return true;
  } catch (err) {
    console.log(`[${bot.username}] cannot equip ${itemName}: ${err.message}`);
    return false;
  }
}

/** Place a full block (uses the robust upstream placeAt). */
async function placeFullBlock(bot, pos, itemName) {
  try {
    return await placeAt(bot, pos, itemName, { useSneak: false, tries: 3, args: null });
  } catch (err) {
    console.log(`[${bot.username}] placeFullBlock failed: ${err.message}`);
    return false;
  }
}

/**
 * Place a non-full block (torch, ...) on the top face of the ground block below `pos`.
 * @returns {Promise<boolean>}
 */
async function placeOnTop(bot, pos, itemName, expectedName = itemName) {
  const ref = bot.blockAt(pos.offset(0, -1, 0));
  if (!ref) return false;
  if (!(await equip(bot, itemName))) return false;
  await lookAtPoint(bot, new Vec3(pos.x + 0.5, pos.y, pos.z + 0.5), 110);
  try {
    await bot.placeBlock(ref, new Vec3(0, 1, 0));
  } catch (err) {
    console.log(`[${bot.username}] placeOnTop(${itemName}) error: ${err.message}`);
  }
  await sleep(300);
  const b = bot.blockAt(pos);
  return !!b && b.name === expectedName;
}

/**
 * Use the held item on a block face (flint and steel on a block / TNT).
 * Mirrors a right click on the block and records the `use` action flag.
 */
async function useItemOnBlock(bot, block, face = new Vec3(0, 1, 0)) {
  if (!block) return false;
  bot.emit("action_use");
  try {
    await bot.activateBlock(block, face);
    return true;
  } catch (err) {
    console.log(`[${bot.username}] activateBlock failed: ${err.message}`);
    return false;
  }
}

/**
 * Right click "in the air" with the held item while looking at `point`
 * (buckets: the server ray-traces from the player's eyes).
 *
 * mineflayer's activateItem() sends rotation (0, 0) in the 1.21 use_item
 * packet, which can snap the server-side view; we send the real rotation.
 */
async function useHeldItemTowards(bot, point) {
  await lookAtPoint(bot, point, 110);
  await sleep(150);
  const mcYaw = (((Math.PI - bot.entity.yaw) * 180) / Math.PI + 540) % 360 - 180;
  const mcPitch = (-bot.entity.pitch * 180) / Math.PI;
  bot.emit("action_use");
  try {
    bot._client.write("use_item", {
      hand: 0,
      sequence: 0,
      rotation: { x: mcYaw, y: mcPitch },
    });
    bot.swingArm("right");
    return true;
  } catch (err) {
    console.log(`[${bot.username}] use_item failed: ${err.message}`);
    return false;
  }
}

async function digAt(bot, pos) {
  return digBlock(bot, pos);
}

/** Positions of blocks with one of `names` in a cube around `center`. */
function findBlocksNear(bot, center, names, radius = 2, dyMin = -1, dyMax = 3) {
  const out = [];
  for (let dx = -radius; dx <= radius; dx++) {
    for (let dz = -radius; dz <= radius; dz++) {
      for (let dy = dyMin; dy <= dyMax; dy++) {
        const pos = center.offset(dx, dy, dz);
        const b = bot.blockAt(pos);
        if (b && names.includes(b.name)) out.push({ pos, block: b });
      }
    }
  }
  return out;
}

/** Fluid source test for a mineflayer block (level 0 == source). */
function isFluidSource(block) {
  if (!block) return false;
  try {
    const props = block.getProperties ? block.getProperties() : {};
    if (props && props.level !== undefined) return Number(props.level) === 0;
  } catch (_) {}
  return block.metadata === 0;
}

// ----------------------------------------------------------------------------
// Logging
// ----------------------------------------------------------------------------

/** Annotate world_events.jsonl (no spaces: the plugin stores text verbatim). */
async function note(rcon, args, bot, episodeNum, phase, extra = {}) {
  const v = (p) => (p && p.x !== undefined ? [p.x, p.y, p.z] : p);
  const payload = { ep: episodeNum, bot: bot.username, phase };
  for (const [k, val] of Object.entries(extra)) {
    payload[k] = Array.isArray(val) ? val.map(v) : v(val);
  }
  await worldStateNote(rcon, args, payload);
}

// ----------------------------------------------------------------------------
// Phase chain (barrier-synchronised steps)
// ----------------------------------------------------------------------------

/**
 * Run `steps` as barrier-synchronised phases, then trigger the stop phase.
 *
 * Each step is { name, run: async (otherData, ourPrevData) => ourData }.
 * Barrier i is entered by registering a once-handler and sending the phase
 * name; the handler re-sends the name first (the upstream handshake), so
 * whichever bot arrives last still releases the other one. `ourData` from
 * step i is delivered to the other bot as `otherData` of step i+1.
 */
function startPhaseChain({
  bot,
  rcon,
  sharedBotRng,
  coordinator,
  episodeNum,
  episodeInstance,
  args,
  prefix,
  steps,
}) {
  const enter = (i, ourData) => {
    if (i >= steps.length) {
      coordinator.onceEvent(
        "stopPhase",
        episodeNum,
        episodeInstance.getOnStopPhaseFn(
          bot,
          rcon,
          sharedBotRng,
          coordinator,
          args.other_bot_name,
          episodeNum,
          args,
        ),
      );
      coordinator.sendToOtherBot(
        "stopPhase",
        bot.entity.position.clone(),
        episodeNum,
        `${prefix} end`,
      );
      return;
    }
    const name = `${prefix}_${i}_${steps[i].name}`;
    coordinator.onceEvent(name, episodeNum, async (otherData) => {
      coordinator.sendToOtherBot(name, ourData, episodeNum, `${name} beginning`);
      if (bot._episodeStopping) {
        console.log(`[${bot.username}] episode stopping, abandoning ${name}`);
        return;
      }
      let next = {};
      try {
        console.log(`[${bot.username}] ▶ ${name}`);
        next = (await steps[i].run(otherData || {}, ourData || {})) || {};
      } catch (err) {
        console.error(`[${bot.username}] step ${name} failed: ${err.stack || err}`);
        next = { error: String(err && err.message ? err.message : err) };
      }
      enter(i + 1, next);
    });
    coordinator.sendToOtherBot(name, ourData, episodeNum, `${name} enter`);
  };
  enter(0, {});
}

// ----------------------------------------------------------------------------
// Base class
// ----------------------------------------------------------------------------

const { BaseEpisode } = require("../base-episode");

/**
 * Common setup for site-based state episodes. Subclasses implement
 *   planEpisode(bot, rng, args, frame, isActor) -> {
 *     actorStand: Vec3 col, observerStand: Vec3 col, items: {actor:[], observer:[]},
 *     metadata: {...}
 *   }
 * and buildSteps(ctx) -> steps[].
 */
class StateEpisodeBase extends BaseEpisode {
  static INIT_MIN_BOTS_DISTANCE = 5;
  static INIT_MAX_BOTS_DISTANCE = 9;
  static WORKS_IN_NON_FLAT_WORLD = false;
  static SITE_HALF_SIZE = 3;
  static ALLOW_UNFLAT_FALLBACK = false;

  async setupEpisode(
    bot,
    rcon,
    sharedBotRng,
    coordinator,
    episodeNum,
    args,
    botPosition,
    otherBotPosition,
  ) {
    const mid = new Vec3(
      (botPosition.x + otherBotPosition.x) / 2,
      (botPosition.y + otherBotPosition.y) / 2,
      (botPosition.z + otherBotPosition.z) / 2,
    );
    const half = this.constructor.SITE_HALF_SIZE;
    let site = findFlatSite(bot, mid, half, 12);
    if (!site && this.constructor.ALLOW_UNFLAT_FALLBACK) {
      site = groundAt(bot, Math.floor(mid.x), Math.floor(mid.z));
    }
    if (!site) {
      throw new Error(`[${this.constructor.name}] no flat site near ${mid}`);
    }
    // Shared random choices (same order on both bots)
    const isActor = decidePrimaryBot(bot, sharedBotRng, args);
    const axis = sharedBotRng() < 0.5 ? "x" : "z";
    const side = sharedBotRng() < 0.5 ? -1 : 1;
    const frame = makeFrame(site, axis, side);
    const plan = this.planEpisode(bot, sharedBotRng, args, frame, isActor);

    // Leader clears snow layers / grass on the site BEFORE the start snapshot
    if (isWorldStateLeader(bot, args)) {
      const x1 = site.x - half;
      const x2 = site.x + half;
      const z1 = site.z - half;
      const z2 = site.z + half;
      const res = await rcon.send(
        `fill ${x1} ${frame.level} ${z1} ${x2} ${frame.level + 2} ${z2} minecraft:air`,
      );
      console.log(`[${bot.username}] cleared site cover: ${res}`);
      if (plan.leaderSetupCommands) {
        for (const cmd of plan.leaderSetupCommands) {
          const r = await rcon.send(cmd);
          console.log(`[${bot.username}] setup '${cmd}' -> ${r}`);
        }
      }
    }

    const myCol = isActor ? plan.actorStand : plan.observerStand;
    const otherCol = isActor ? plan.observerStand : plan.actorStand;
    const me = standAt(bot, myCol, frame.level);
    const other = standAt(bot, otherCol, frame.level);
    await rconTp(rcon, bot.username, me.x, me.y, me.z);
    await sleep(1200);

    await giveItems(bot, rcon, isActor ? plan.items.actor : plan.items.observer);
    await unequipHand(bot);

    this._frame = frame;
    this._isActor = isActor;
    this._plan = plan;
    this._worldStateCenter = centerOf(site);
    this._stateMetadata = {
      episode: this.constructor.name,
      site: [site.x, site.y, site.z],
      axis,
      side,
      actor: isActor ? bot.username : args.other_bot_name,
      observer: isActor ? args.other_bot_name : bot.username,
      ...plan.metadata,
    };
    console.log(
      `[${bot.username}] ${this.constructor.name} setup: ${JSON.stringify(this._stateMetadata)}`,
    );
    return { botPositionNew: me, otherBotPositionNew: other };
  }

  /** Subclasses override. */
  planEpisode(bot, rng, args, frame, isActor) {
    throw new Error("planEpisode() must be implemented");
  }

  /** Subclasses override; returns the list of phase steps. */
  buildSteps(ctx) {
    throw new Error("buildSteps() must be implemented");
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
      siteCenter: centerOf(this._frame.site).offset(0, 0.6, 0),
    };
    startPhaseChain({
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
    stopAll(bot);
    if (bot.pathfinder) bot.pathfinder.setGoal(null);
    try {
      await unequipHand(bot);
    } catch (_) {}
  }
}

/** Common first step: both bots face the site (observer) / look around (actor). */
function approachStep(name = "approach") {
  return {
    name,
    run: async function () {
      const ctx = this;
      // Movements used by any pathfinder nudge during this episode: never dig
      // or place scaffolding (that would add unintended world changes).
      safePathfinder(ctx.bot);
      await lookAtPoint(ctx.bot, ctx.siteCenter, 80);
      await sleep(localJitterMs(500, 900));
      return {};
    },
  };
}

/** Common last step: short eye contact, then the chain triggers the stop phase. */
function outroStep(name = "outro") {
  return {
    name,
    run: async function () {
      const ctx = this;
      await lookAtOtherBot(ctx.bot, ctx.args.other_bot_name, 90);
      await sleep(localJitterMs(1000, 1600));
      return {};
    },
  };
}

/** Bind step.run to ctx so steps can use `this`. */
function bindSteps(ctx, steps) {
  return steps.map((s) => ({ name: s.name, run: s.run.bind(ctx) }));
}

module.exports = {
  StateEpisodeBase,
  approachStep,
  bindSteps,
  centerOf,
  digAt,
  equip,
  findBlocksNear,
  findFlatSite,
  giveItems,
  groundAt,
  isFluidSource,
  localJitterMs,
  lookAtOtherBot,
  lookAtPoint,
  makeFrame,
  note,
  outroStep,
  pick,
  placeFullBlock,
  safePathfinder,
  placeOnTop,
  randInt,
  shuffle,
  sleep,
  standAt,
  startPhaseChain,
  startWatching,
  useHeldItemTowards,
  useItemOnBlock,
  walkNear,
  watchFor,
};
