/**
 * creature-common.js - Shared base class and helpers for the combat / creature
 * episodes (pvpMelee, pvpRanged, pveMobs, animalCare, rideMount, itemExchange).
 *
 * Same design rules as the stateful episodes (state/state-common.js):
 *  - Every server-command edit (/fill to level the arena, water pools, rails,
 *    /summon of mobs, animals, vehicles) happens in setupEpisode, i.e. BEFORE
 *    the leader opens the world log (worldStateBegin in the start-recording
 *    phase). Summons are done last, after both bots got their items, so the
 *    mobs have as little time as possible before recording starts.
 *    env-setup.js kills leftover hostile mobs in the TELEPORT phase (before
 *    setupEpisode), so it never removes the mobs summoned here.
 *  - During recording only player actions (attack, use, place, mount, toss,
 *    open containers) and game mechanics (AI, breeding, explosions, arrows)
 *    change the world.
 *  - Both bots consume sharedBotRng identically in planEpisode (fixed draw
 *    counts); runtime jitter uses Math.random only.
 *  - Every wait has a timeout; a failing step is logged and the phase chain
 *    continues, so an episode always reaches the stop phase.
 */

const { Vec3 } = require("vec3");
const {
  goals: { GoalFollow, GoalNear },
} = require("mineflayer-pathfinder");

const {
  bindSteps,
  centerOf,
  equip,
  findFlatSite,
  giveItems,
  groundAt,
  localJitterMs,
  lookAtOtherBot,
  lookAtPoint,
  makeFrame,
  note,
  safePathfinder,
  sleep,
  startPhaseChain,
  startWatching,
  walkNear,
  watchFor,
} = require("../state/state-common");
const { BaseEpisode } = require("../base-episode");
const { unequipHand } = require("../../primitives/items");
const { gotoWithTimeout, stopAll } = require("../../primitives/movement");
const { decidePrimaryBot, rconTp } = require("../../utils/coordination");
const { isWorldStateLeader } = require("../../utils/world-state");
const { SUMMON_TAG, biomeAt, biomeGroup } = require("../../utils/creatures");
const { resolveBiome } = require("../../utils/biomes");
const { exchange } = require("../../utils/handshake");

// ----------------------------------------------------------------------------
// Small utilities
// ----------------------------------------------------------------------------

/** Race a promise against a timeout; resolves to `fallback` on timeout/error. */
async function withTimeout(promise, ms, fallback = null, label = "") {
  let timer;
  try {
    return await Promise.race([
      promise,
      new Promise((resolve) => {
        timer = setTimeout(() => {
          if (label) console.log(`[timeout] ${label} after ${ms} ms`);
          resolve(fallback);
        }, ms);
      }),
    ]);
  } catch (err) {
    if (label) console.log(`[error] ${label}: ${err && err.message ? err.message : err}`);
    return fallback;
  } finally {
    clearTimeout(timer);
  }
}

async function rconSend(bot, rcon, cmd) {
  try {
    const res = await rcon.send(cmd);
    console.log(`[${bot.username}] rcon '${cmd.length > 220 ? cmd.slice(0, 220) + "..." : cmd}' -> ${String(res).slice(0, 200)}`);
    return res;
  } catch (err) {
    console.log(`[${bot.username}] rcon '${cmd.slice(0, 120)}' failed: ${err.message}`);
    return null;
  }
}

function v3(p) {
  return p ? [Number(p.x.toFixed(2)), Number(p.y.toFixed(2)), Number(p.z.toFixed(2))] : null;
}

function isAlive(bot) {
  return Boolean(bot.entity) && bot.health > 0;
}

function stopping(bot) {
  return Boolean(bot._episodeStopping);
}

function clearControls(bot) {
  for (const c of ["forward", "back", "left", "right", "jump", "sprint", "sneak"]) {
    try {
      bot.setControlState(c, false);
    } catch (_) {}
  }
}

/** Minecraft-degree yaw/pitch of the bot's current look. */
function mcYawPitch(bot) {
  const yaw = (((Math.PI - bot.entity.yaw) * 180) / Math.PI + 540) % 360 - 180;
  const pitch = (-bot.entity.pitch * 180) / Math.PI;
  return { yaw, pitch };
}

/** Mineflayer yaw (radians) facing from `from` to `to`. */
function yawTo(from, to) {
  return Math.atan2(-(to.x - from.x), -(to.z - from.z));
}

/** Minecraft yaw degrees for a horizontal direction (dx, dz). */
function mcYawOfDir(dx, dz) {
  return (Math.atan2(-dx, dz) * 180) / Math.PI;
}

function wrapPi(a) {
  while (a > Math.PI) a -= 2 * Math.PI;
  while (a < -Math.PI) a += 2 * Math.PI;
  return a;
}

function eyePos(bot) {
  return bot.entity.position.offset(0, bot.entity.eyeHeight || 1.62, 0);
}

/** Aim point on an entity (chest height). */
function entityAim(e, frac = 0.7) {
  const h = e.height || 1.8;
  return e.position.offset(0, h * frac, 0);
}

function playerEntity(bot, name) {
  const p = bot.players[name];
  return p && p.entity ? p.entity : null;
}

/**
 * Head position of another player. Mineflayer does not move passengers with
 * their vehicle, so for a riding player the vehicle position is used.
 */
function headOf(e) {
  if (!e || !e.position) return null;
  if (e.vehicle && e.vehicle.position) return e.vehicle.position.offset(0, riderOffset(e.vehicle) + 1.5, 0);
  return e.position.offset(0, 1.5, 0);
}

function otherHead(bot, name) {
  return headOf(playerEntity(bot, name));
}

/**
 * Turn towards a point by at most maxStepDeg (yaw and pitch each), instantly
 * (no smoothing inside one step; call it every physics tick for a smooth turn).
 * @returns {number} remaining yaw error (radians, absolute)
 */
function turnToward(bot, point, maxStepDeg = 14, pitchOverride = null) {
  if (!bot.entity) return 0;
  const eye = eyePos(bot);
  const dx = point.x - eye.x;
  const dy = point.y - eye.y;
  const dz = point.z - eye.z;
  const wantYaw = Math.atan2(-dx, -dz);
  const wantPitch = pitchOverride !== null ? pitchOverride : Math.atan2(dy, Math.sqrt(dx * dx + dz * dz));
  const max = (maxStepDeg * Math.PI) / 180;
  const dYaw = wrapPi(wantYaw - bot.entity.yaw);
  const dPitch = wantPitch - bot.entity.pitch;
  const yaw = bot.entity.yaw + Math.max(-max, Math.min(max, dYaw));
  const pitch = bot.entity.pitch + Math.max(-max, Math.min(max, dPitch));
  try {
    bot.look(yaw, pitch, true);
  } catch (_) {}
  return Math.abs(wrapPi(wantYaw - yaw));
}

/** Smoothly turn to a point over ~durationMs using turnToward each tick. */
async function turnTo(bot, getPoint, durationMs = 600, stepDeg = 12) {
  const until = Date.now() + durationMs;
  while (Date.now() < until && !stopping(bot)) {
    const p = typeof getPoint === "function" ? getPoint() : getPoint;
    if (!p || !bot.entity) break;
    const err = turnToward(bot, p, stepDeg);
    if (err < 0.02) break;
    await sleep(50);
  }
}

/** Look at the other bot for a while (eye contact / checking on the partner). */
async function glanceAtOther(bot, otherName, ms = 800) {
  const until = Date.now() + ms;
  while (Date.now() < until && !stopping(bot)) {
    const h = otherHead(bot, otherName);
    if (!h) break;
    if (bot.vehicle) {
      // mounted: Mineflayer does not send the look, do it ourselves
      const eye = bot.entity.position.offset(0, 1.62, 0);
      const wantYaw = Math.atan2(-(h.x - eye.x), -(h.z - eye.z));
      const wantPitch = Math.atan2(h.y - eye.y, Math.hypot(h.x - eye.x, h.z - eye.z));
      const max = (10 * Math.PI) / 180;
      sendRiderLook(
        bot,
        bot.entity.yaw + Math.max(-max, Math.min(max, wrapPi(wantYaw - bot.entity.yaw))),
        bot.entity.pitch + Math.max(-max, Math.min(max, wantPitch - bot.entity.pitch)),
      );
    } else {
      turnToward(bot, h, 10);
    }
    await sleep(50);
  }
}

// ----------------------------------------------------------------------------
// Item use (1.21 use_item carries the rotation; Mineflayer's activateItem
// sends (0, 0), which snaps the server-side look - so we write it ourselves)
// ----------------------------------------------------------------------------

let useSeq = 1000;

/** Start using the held item (bow draw, shield raise, eating, throwing). */
function useItem(bot, offHand = false) {
  const { yaw, pitch } = mcYawPitch(bot);
  bot.emit("action_use");
  try {
    bot._client.write("use_item", {
      hand: offHand ? 1 : 0,
      sequence: useSeq++,
      rotation: { x: yaw, y: pitch },
    });
    bot.usingHeldItem = true;
    return true;
  } catch (err) {
    console.log(`[${bot.username}] use_item failed: ${err.message}`);
    return false;
  }
}

/** Release the used item (shoot the bow, lower the shield). */
function releaseItem(bot) {
  try {
    bot.deactivateItem();
  } catch (err) {
    console.log(`[${bot.username}] release failed: ${err.message}`);
  }
}

/** Drop the held item like pressing Q (all=true: Ctrl+Q, whole stack). */
function dropHeld(bot, all = false) {
  try {
    bot._client.write("block_dig", {
      status: all ? 3 : 4,
      location: new Vec3(0, 0, 0),
      face: 0,
      sequence: 0,
    });
    bot.swingArm("right");
    return true;
  } catch (err) {
    console.log(`[${bot.username}] drop failed: ${err.message}`);
    return false;
  }
}

/** Right click an entity with the held item (feed, shear, milk, leash, ...). */
async function interactEntity(bot, entity) {
  if (!entity || !bot.entity) return false;
  await turnTo(bot, () => entityAim(entity, 0.6), 500, 18);
  try {
    bot.useOn(entity); // emits action_use (patched Mineflayer)
    bot.swingArm("right");
    return true;
  } catch (err) {
    console.log(`[${bot.username}] interact failed: ${err.message}`);
    return false;
  }
}

function hasItem(bot, name) {
  return bot.inventory.items().some((i) => i.name === name);
}

function countItem(bot, name) {
  return bot.inventory.items().filter((i) => i.name === name).reduce((a, i) => a + i.count, 0);
}

/** Equip `name` in the main hand; returns false if not in the inventory. */
async function equipItem(bot, name) {
  const it = bot.inventory.items().find((i) => i.name === name);
  if (!it) return false;
  if (bot.heldItem && bot.heldItem.name === name) return true;
  try {
    await withTimeout(bot.equip(it, "hand"), 2000, null, `${bot.username} equip ${name}`);
    await sleep(80);
    return Boolean(bot.heldItem && bot.heldItem.name === name);
  } catch (err) {
    console.log(`[${bot.username}] equip ${name} failed: ${err.message}`);
    return false;
  }
}

/** First inventory item whose name matches a predicate. */
function findItem(bot, pred) {
  return bot.inventory.items().find((i) => pred(i.name));
}

// ----------------------------------------------------------------------------
// Entities
// ----------------------------------------------------------------------------

function entitiesWhere(bot, pred) {
  return Object.values(bot.entities).filter((e) => e && e !== bot.entity && e.isValid !== false && pred(e));
}

function nearestWhere(bot, pred, from = null) {
  const origin = from || (bot.entity && bot.entity.position);
  if (!origin) return null;
  let best = null;
  let bestD = Infinity;
  for (const e of entitiesWhere(bot, pred)) {
    const d = e.position.distanceTo(origin);
    if (d < bestD) {
      bestD = d;
      best = e;
    }
  }
  return best;
}

/** Entities of a type name (Mineflayer `entity.name`) near a point, sorted by id. */
function entitiesOfType(bot, name, center, radius = 24) {
  return entitiesWhere(bot, (e) => e.name === name && e.position.distanceTo(center) <= radius).sort((a, b) => a.id - b.id);
}

/** Dropped item entities near a point. */
function itemEntitiesNear(bot, center, radius = 8) {
  return entitiesWhere(bot, (e) => (e.name === "item" || e.displayName === "Item") && e.position.distanceTo(center) <= radius);
}

/** Walk over dropped items near `center` to pick them up (bounded). */
async function collectItems(bot, center, { radius = 8, max = 4, timeoutMs = 8000 } = {}) {
  const until = Date.now() + timeoutMs;
  let walked = 0;
  while (walked < max && Date.now() < until && !stopping(bot)) {
    const items = itemEntitiesNear(bot, center, radius).filter((e) => e.onGround !== false || true);
    if (!items.length) break;
    items.sort((a, b) => a.position.distanceTo(bot.entity.position) - b.position.distanceTo(bot.entity.position));
    const it = items[0];
    const left = until - Date.now();
    await walkNear(bot, it.position, 0.3, Math.max(500, Math.min(4000, left)));
    await sleep(350);
    walked++;
  }
  return walked;
}

// ----------------------------------------------------------------------------
// Combat
// ----------------------------------------------------------------------------

/** Attack cooldown of a held weapon (ms), vanilla attack speed. */
function weaponCooldownMs(itemName) {
  const n = itemName || "";
  if (n.endsWith("_sword")) return 625;
  if (n === "wooden_axe" || n === "stone_axe") return 1250;
  if (n === "iron_axe") return 1110;
  if (n.endsWith("_axe")) return 1000;
  if (n === "trident") return 910;
  if (n.endsWith("_shovel")) return 1000;
  if (n.endsWith("_pickaxe")) return 830;
  return 400; // fist
}

/**
 * Melee engagement against a (possibly changing) target.
 *
 * Far: pathfinder GoalFollow (no digging / placing). Near: manual control,
 * look at the target every tick, attack on the weapon cooldown when the
 * crosshair is roughly on the target, jump crits, strafing, optional shield
 * blocking windows and sprint hits.
 *
 * @param {*} bot
 * @param {Function} getTarget - () => entity|null (re-evaluated every tick)
 * @param {Object} o
 * @param {number} o.durationMs
 * @param {boolean} [o.shield=false] - shield in the off hand
 * @param {number} [o.blockChance=0.25] - chance per second to raise the shield
 * @param {number} [o.critChance=0.3] - chance to jump before a hit
 * @param {number} [o.strafe=0.5] - strafing intensity 0..1
 * @param {number} [o.reach=3.0]
 * @param {Function} [o.stopWhen] - () => bool, ends the engagement early
 * @param {Function} [o.onTick] - async hook called every ~1 s
 * @param {number[]} [o.cooldownScale=[1.0, 1.35]] - attack interval = weapon cooldown x U(lo, hi)
 * @param {boolean} [o.returnWhenNoTarget=true]
 * @returns {Promise<{attacks: number, blocks: number, ms: number, reason: string}>}
 */
async function meleeEngage(bot, getTarget, o = {}) {
  const durationMs = o.durationMs || 15000;
  const reach = o.reach || 3.0;
  const shield = Boolean(o.shield);
  const blockChance = o.blockChance === undefined ? 0.25 : o.blockChance;
  const critChance = o.critChance === undefined ? 0.3 : o.critChance;
  const strafe = o.strafe === undefined ? 0.5 : o.strafe;
  const returnWhenNoTarget = o.returnWhenNoTarget !== false;
  const start = Date.now();
  const until = start + durationMs;
  let attacks = 0;
  let blocks = 0;
  let nextAttack = Date.now() + 200 + Math.random() * 300;
  let following = false;
  let strafeDir = null;
  let strafeUntil = 0;
  let blockingUntil = 0;
  let nextHook = Date.now() + 1000;
  let reason = "timeout";
  let noTargetSince = null;
  safePathfinder(bot);
  try {
    while (Date.now() < until) {
      if (stopping(bot)) {
        reason = "stopping";
        break;
      }
      if (o.stopWhen && o.stopWhen()) {
        reason = "stop_condition";
        break;
      }
      if (!isAlive(bot)) {
        clearControls(bot);
        if (following && bot.pathfinder) bot.pathfinder.setGoal(null);
        following = false;
        await sleep(200);
        continue;
      }
      const now = Date.now();
      if (o.onTick && now >= nextHook) {
        nextHook = now + 1000;
        try {
          await o.onTick();
        } catch (_) {}
      }
      const target = getTarget();
      if (!target || !target.position) {
        clearControls(bot);
        if (following && bot.pathfinder) bot.pathfinder.setGoal(null);
        following = false;
        if (blockingUntil) {
          releaseItem(bot);
          blockingUntil = 0;
        }
        if (noTargetSince === null) noTargetSince = now;
        if (returnWhenNoTarget && now - noTargetSince > 600) {
          reason = "no_target";
          break;
        }
        await sleep(100);
        continue;
      }
      noTargetSince = null;
      const dist = bot.entity.position.distanceTo(target.position);
      if (dist > 5.5) {
        if (blockingUntil) {
          releaseItem(bot);
          blockingUntil = 0;
        }
        if (!following) {
          clearControls(bot);
          try {
            bot.pathfinder.setGoal(new GoalFollow(target, 2.2), true);
            following = true;
          } catch (_) {}
        }
        await sleep(100);
        continue;
      }
      if (following) {
        try {
          bot.pathfinder.setGoal(null);
        } catch (_) {}
        following = false;
      }
      const yawErr = turnToward(bot, entityAim(target, 0.75), 16);
      // shield window
      if (shield) {
        if (blockingUntil && now >= blockingUntil) {
          releaseItem(bot);
          blockingUntil = 0;
          nextAttack = Math.max(nextAttack, now + 150);
        } else if (!blockingUntil && dist < 4.5 && Math.random() < blockChance * 0.05) {
          useItem(bot, true);
          blocks++;
          blockingUntil = now + 500 + Math.random() * 900;
        }
      }
      // movement
      if (now >= strafeUntil) {
        const r = Math.random();
        strafeDir = r < strafe * 0.5 ? "left" : r < strafe ? "right" : null;
        strafeUntil = now + 600 + Math.random() * 1400;
      }
      const wantForward = dist > 2.4;
      const wantBack = dist < 1.3;
      bot.setControlState("forward", wantForward);
      bot.setControlState("back", !wantForward && wantBack);
      bot.setControlState("sprint", wantForward && dist > 3.2 && !blockingUntil);
      bot.setControlState("left", strafeDir === "left");
      bot.setControlState("right", strafeDir === "right");
      if (bot.entity.isCollidedHorizontally && wantForward) bot.setControlState("jump", true);
      else bot.setControlState("jump", false);
      // occasional swing at air just out of reach (a miss, like a human)
      if (!blockingUntil && dist > reach && dist < reach + 1.5 && now >= nextAttack && Math.random() < 0.04) {
        bot.swingArm("right");
        nextAttack = now + 300 + Math.random() * 300;
      }
      // attack
      if (!blockingUntil && dist <= reach && now >= nextAttack && yawErr < 0.45) {
        if (bot.entity.onGround && Math.random() < critChance) {
          bot.setControlState("jump", true);
          await sleep(300); // falling: critical hit
          bot.setControlState("jump", false);
        }
        const t2 = getTarget();
        if (t2 && t2.position && bot.entity.position.distanceTo(t2.position) <= reach + 0.3) {
          try {
            bot.attack(t2);
            attacks++;
          } catch (_) {}
        }
        const held = bot.heldItem ? bot.heldItem.name : null;
        const [cdLo, cdHi] = o.cooldownScale || [1.0, 1.35];
        nextAttack = Date.now() + weaponCooldownMs(held) * (cdLo + Math.random() * (cdHi - cdLo));
      }
      await sleep(50);
    }
  } finally {
    if (blockingUntil) releaseItem(bot);
    if (bot.pathfinder) {
      try {
        bot.pathfinder.setGoal(null);
      } catch (_) {}
    }
    clearControls(bot);
  }
  return { attacks, blocks, ms: Date.now() - start, reason };
}

/** Arrow power (0..1) for a bow drawn for `ticks` ticks (vanilla BowItem.getPowerForTime). */
function bowPower(ticks) {
  const f = ticks / 20;
  return Math.min(1, (f * f + f * 2) / 3);
}

/**
 * Pitch (Mineflayer radians, positive = up) so that a projectile launched with
 * `speed` blocks/tick reaches horizontal distance `dist` at height `dh`
 * (relative to the launch point). Simulates vanilla arrow physics
 * (drag 0.99, gravity 0.05 per tick).
 */
function ballisticPitch(dist, dh, speed, gravity = 0.05, drag = 0.99) {
  const heightAt = (pitch) => {
    let vx = Math.cos(pitch) * speed;
    let vy = Math.sin(pitch) * speed;
    let x = 0;
    let y = 0;
    for (let t = 0; t < 200; t++) {
      if (x + vx >= dist) {
        const f = (dist - x) / vx;
        return y + vy * f;
      }
      x += vx;
      y += vy;
      vx *= drag;
      vy = vy * drag - gravity;
      if (vx < 1e-3) return -1e9;
    }
    return -1e9;
  };
  let lo = -0.6;
  let hi = 0.75;
  if (heightAt(hi) < dh) return hi; // out of range: max useful angle
  for (let i = 0; i < 30; i++) {
    const mid = (lo + hi) / 2;
    if (heightAt(mid) < dh) lo = mid;
    else hi = mid;
  }
  return (lo + hi) / 2;
}

/** Velocity tracker (blocks/tick) for entities whose velocity Mineflayer does not update. */
function makeVelocityTracker() {
  const last = new Map();
  return (e) => {
    const now = Date.now();
    const prev = last.get(e.id);
    last.set(e.id, { t: now, p: e.position.clone() });
    if (!prev || now - prev.t < 30 || now - prev.t > 1500) return new Vec3(0, 0, 0);
    const dt = (now - prev.t) / 50;
    return e.position.minus(prev.p).scaled(1 / dt);
  };
}

/**
 * Ranged engagement with a bow or crossbow: draw (strafing), track the target
 * with arrow-drop and lead compensation, release; repeat.
 *
 * @param {*} bot
 * @param {Function} getTarget
 * @param {Object} o
 * @param {number} o.durationMs
 * @param {boolean} [o.crossbow=false]
 * @param {number} [o.minDist=5] - returns 'close' when the target gets closer
 * @param {number} [o.strafe=0.6]
 * @param {boolean} [o.shield=false] - raise an off-hand shield between shots
 * @param {Function} [o.stopWhen]
 * @returns {Promise<{shots: number, blocks: number, reason: string}>}
 */
async function rangedEngage(bot, getTarget, o = {}) {
  const durationMs = o.durationMs || 15000;
  const crossbow = Boolean(o.crossbow);
  const weapon = crossbow ? "crossbow" : "bow";
  const minDist = o.minDist === undefined ? 5 : o.minDist;
  const strafe = o.strafe === undefined ? 0.6 : o.strafe;
  const until = Date.now() + durationMs;
  const vel = makeVelocityTracker();
  let shots = 0;
  let blocks = 0;
  let reason = "timeout";
  let strafeDir = null;
  let strafeUntil = 0;

  const aimAt = (target, chargeTicks) => {
    const eye = eyePos(bot).offset(0, -0.1, 0);
    const v = vel(target);
    const aim = entityAim(target, 0.6);
    const speed = crossbow ? 3.15 : Math.max(0.6, bowPower(chargeTicks) * 3);
    const flat = Math.hypot(aim.x - eye.x, aim.z - eye.z);
    const tFlight = Math.min(40, flat / Math.max(0.5, speed * 0.9));
    const lead = aim.plus(new Vec3(v.x * tFlight, 0, v.z * tFlight));
    const dist = Math.hypot(lead.x - eye.x, lead.z - eye.z);
    const pitch = ballisticPitch(dist, lead.y - eye.y, speed);
    return { point: lead, pitch };
  };
  const moveTick = (dist) => {
    const now = Date.now();
    if (now >= strafeUntil) {
      const r = Math.random();
      strafeDir = r < strafe * 0.5 ? "left" : r < strafe ? "right" : null;
      strafeUntil = now + 700 + Math.random() * 1500;
    }
    bot.setControlState("left", strafeDir === "left");
    bot.setControlState("right", strafeDir === "right");
    bot.setControlState("back", dist < minDist + 2 && Math.random() < 0.5);
    bot.setControlState("forward", false);
    bot.setControlState("sprint", false);
    bot.setControlState("jump", Boolean(bot.entity && bot.entity.isCollidedHorizontally));
  };

  try {
    while (Date.now() < until) {
      if (stopping(bot)) {
        reason = "stopping";
        break;
      }
      if (o.stopWhen && o.stopWhen()) {
        reason = "stop_condition";
        break;
      }
      if (!isAlive(bot)) {
        clearControls(bot);
        await sleep(250);
        continue;
      }
      let target = getTarget();
      if (!target) {
        clearControls(bot);
        reason = "no_target";
        break;
      }
      let dist = bot.entity.position.distanceTo(target.position);
      if (dist < minDist) {
        reason = "close";
        break;
      }
      if (dist > 40) {
        reason = "far";
        break;
      }
      if (!(await equipItem(bot, weapon))) {
        reason = "no_weapon";
        break;
      }
      if (!hasItem(bot, "arrow")) {
        reason = "no_arrows";
        break;
      }
      // Shield between shots
      if (o.shield && Math.random() < 0.3) {
        await turnTo(bot, () => entityAim(target, 0.7), 300, 16);
        useItem(bot, true);
        blocks++;
        const bUntil = Date.now() + 600 + Math.random() * 900;
        while (Date.now() < bUntil && !stopping(bot)) {
          const t = getTarget();
          if (t) turnToward(bot, entityAim(t, 0.7), 14);
          await sleep(50);
        }
        releaseItem(bot);
        await sleep(100);
      }
      // Draw / load
      const chargeMs = crossbow ? 1350 + Math.random() * 300 : 650 + Math.random() * 700;
      const chargeStart = Date.now();
      await turnTo(bot, () => {
        const t = getTarget();
        return t ? aimAt(t, 20).point : null;
      }, 250, 18);
      useItem(bot, false);
      while (Date.now() - chargeStart < chargeMs && !stopping(bot)) {
        target = getTarget();
        if (!target || !isAlive(bot)) break;
        dist = bot.entity.position.distanceTo(target.position);
        moveTick(dist);
        const ticks = (Date.now() - chargeStart) / 50;
        const a = aimAt(target, crossbow ? 20 : Math.max(4, ticks + 2));
        turnToward(bot, a.point, 14, a.pitch);
        await sleep(50);
      }
      clearControls(bot);
      if (crossbow) {
        // finish loading, then aim and fire with a second use
        releaseItem(bot);
        const aimUntil = Date.now() + 250 + Math.random() * 350;
        while (Date.now() < aimUntil && !stopping(bot)) {
          const t = getTarget();
          if (!t) break;
          const a = aimAt(t, 20);
          turnToward(bot, a.point, 14, a.pitch);
          await sleep(50);
        }
        useItem(bot, false);
        await sleep(60);
        releaseItem(bot);
      } else {
        const t = getTarget();
        if (t) {
          const a = aimAt(t, (Date.now() - chargeStart) / 50);
          turnToward(bot, a.point, 20, a.pitch);
          await sleep(50);
        }
        releaseItem(bot);
      }
      shots++;
      await sleep(250 + Math.random() * 450);
    }
  } finally {
    if (bot.usingHeldItem) releaseItem(bot);
    clearControls(bot);
  }
  return { shots, blocks, reason };
}

/** Eat / drink the held item (golden apple, bread, milk): use, hold, release. */
async function consumeHeld(bot, holdMs = 1800) {
  useItem(bot, false);
  const until = Date.now() + holdMs;
  while (Date.now() < until && !stopping(bot) && isAlive(bot)) await sleep(50);
  releaseItem(bot);
  await sleep(150);
}

/** Back off from a point for `ms` while looking at it. */
async function backOff(bot, getPoint, ms = 1200, sprintAway = false) {
  const until = Date.now() + ms;
  try {
    while (Date.now() < until && !stopping(bot) && isAlive(bot)) {
      const p = getPoint();
      if (!p) break;
      turnToward(bot, p, 14);
      bot.setControlState("back", true);
      bot.setControlState("sprint", sprintAway);
      bot.setControlState("jump", Boolean(bot.entity.isCollidedHorizontally));
      await sleep(50);
    }
  } finally {
    clearControls(bot);
  }
}

// ----------------------------------------------------------------------------
// Equipment (RCON, setup only)
// ----------------------------------------------------------------------------

const TIERS = ["wooden", "stone", "iron", "golden", "diamond", "netherite"];
const TIER_WEIGHTS = [2, 3, 3, 1.2, 2, 1];
const ARMOR_TIERS = ["none", "leather", "chainmail", "iron", "golden", "diamond", "netherite"];
const ARMOR_WEIGHTS = [1.5, 2, 1.5, 2.5, 1, 1.5, 0.8];
const ARMOR_SLOTS = [
  ["head", "helmet"],
  ["chest", "chestplate"],
  ["legs", "leggings"],
  ["feet", "boots"],
];

/**
 * Round-2 fixer (03 section 5: no long close-ups under 1.5 blocks): when the
 * partner is closer than `minDist`, walk backwards while facing it until the
 * distance is `minDist + 1` (or `maxMs` passed). Not while mounted.
 * @returns {Promise<boolean>} whether the bot stepped back
 */
async function keepApart(bot, otherName, minDist = 2.5, maxMs = 1800) {
  if (!bot.entity || bot.vehicle || stopping(bot)) return false;
  const o = playerEntity(bot, otherName);
  if (!o || !o.position || o.position.distanceTo(bot.entity.position) >= minDist) return false;
  const until = Date.now() + maxMs;
  try {
    while (Date.now() < until && !stopping(bot) && isAlive(bot)) {
      const e = playerEntity(bot, otherName);
      if (!e || e.position.distanceTo(bot.entity.position) >= minDist + 1) break;
      turnToward(bot, headOf(e) || e.position.offset(0, 1.5, 0), 14);
      bot.setControlState("back", true);
      bot.setControlState("jump", Boolean(bot.entity.isCollidedHorizontally));
      await sleep(50);
    }
  } finally {
    clearControls(bot);
  }
  return true;
}

function weightedIndex(u, weights) {
  const total = weights.reduce((a, b) => a + b, 0);
  let acc = 0;
  for (let i = 0; i < weights.length; i++) {
    acc += weights[i];
    if (u * total < acc) return i;
  }
  return weights.length - 1;
}

/**
 * Random loadout for one fighter. Fixed 9 draws.
 * @param {Function} rng
 * @param {Object} o
 * @param {number} [o.axeChance=0.35]
 * @param {number} [o.shieldChance=0.35]
 * @param {number} [o.armorScale=1] - multiplies the "none" weight inverse
 * @returns {{weapon: string, tier: string, kind: string, armor: Object<string,string|null>, armor_tier: string, shield: boolean, golden_apple: boolean}}
 */
function chooseLoadout(rng, o = {}) {
  const uKind = rng();
  const uTier = rng();
  const uArmor = rng();
  const uPieces = [rng(), rng(), rng(), rng()];
  const uShield = rng();
  const uApple = rng();
  const kind = uKind < (o.axeChance === undefined ? 0.35 : o.axeChance) ? "axe" : "sword";
  const tier = TIERS[weightedIndex(uTier, TIER_WEIGHTS)];
  const armorWeights = ARMOR_WEIGHTS.slice();
  if (o.noArmorWeight !== undefined) armorWeights[0] = o.noArmorWeight;
  const armorTier = ARMOR_TIERS[weightedIndex(uArmor, armorWeights)];
  const armor = {};
  ARMOR_SLOTS.forEach(([slot, piece], i) => {
    armor[slot] = armorTier !== "none" && uPieces[i] < 0.78 ? `${armorTier === "golden" ? "golden" : armorTier}_${piece}` : null;
  });
  return {
    weapon: `${tier}_${kind}`,
    tier,
    kind,
    armor,
    armor_tier: armorTier,
    shield: uShield < (o.shieldChance === undefined ? 0.35 : o.shieldChance),
    golden_apple: uApple < 0.5,
  };
}

/** RCON commands that put armour / off-hand items on a player. */
function equipmentCommands(name, loadout) {
  const cmds = [];
  for (const [slot] of ARMOR_SLOTS) {
    const it = loadout.armor && loadout.armor[slot];
    cmds.push(`item replace entity ${name} armor.${slot} with ${it ? "minecraft:" + it : "minecraft:air"}`);
  }
  cmds.push(`item replace entity ${name} weapon.offhand with ${loadout.shield ? "minecraft:shield" : "minecraft:air"}`);
  return cmds;
}

/** Commands that remove armour / off-hand items again (teardown). */
function unequipCommands(name) {
  const cmds = ARMOR_SLOTS.map(([slot]) => `item replace entity ${name} armor.${slot} with minecraft:air`);
  cmds.push(`item replace entity ${name} weapon.offhand with minecraft:air`);
  return cmds;
}

// ----------------------------------------------------------------------------
// Vehicles
// ----------------------------------------------------------------------------

/** Send the rider's head rotation (Mineflayer stops sending it while mounted). */
function sendRiderLook(bot, yawRad, pitchRad) {
  // bot.look(..., force=true) sets bot.entity.yaw/pitch AND the physics
  // plugin's "last sent" look, which the per-frame recorder reports
  // (getLastCameraAction); while mounted the physics plugin sends nothing,
  // so the packet is written here.
  try {
    bot.look(yawRad, pitchRad, true);
  } catch (_) {
    bot.entity.yaw = yawRad;
    bot.entity.pitch = pitchRad;
  }
  const { yaw, pitch } = mcYawPitch(bot);
  try {
    bot._client.write("look", { yaw, pitch, onGround: false, flags: { onGround: false, hasHorizontalCollision: undefined } });
  } catch (_) {}
}

/** Approximate rider (feet) height above the vehicle origin. */
function riderOffset(vehicle) {
  const n = vehicle ? vehicle.name : "";
  const table = {
    horse: 1.1,
    donkey: 0.95,
    mule: 1.0,
    skeleton_horse: 1.1,
    zombie_horse: 1.1,
    camel: 1.85,
    pig: 0.65,
    boat: 0.0,
    chest_boat: 0.0,
    minecart: 0.0,
    llama: 1.15,
  };
  if (table[n] !== undefined) return table[n];
  return (vehicle && vehicle.height ? vehicle.height * 0.75 : 0.8) - 0.1;
}

/** Keep bot.entity.position at the rider seat (Mineflayer does not while mounted). */
function syncRiderPosition(bot) {
  const v = bot.vehicle;
  if (!v || !v.position || !bot.entity) return;
  const p = v.position.offset(0, riderOffset(v), 0);
  bot.entity.position.x = p.x;
  bot.entity.position.y = p.y;
  bot.entity.position.z = p.z;
}

/**
 * Mineflayer 4.33 only handles set_passengers packets that LIST the bot, so a
 * dismount (the vehicle's passenger list no longer contains the bot) never
 * clears bot.vehicle / entity.vehicle. Install a listener that does.
 * Idempotent (once per bot).
 */
function ensurePassengerPatch(bot) {
  if (bot._r2PassengerPatch || !bot._client) return;
  bot._r2PassengerPatch = true;
  bot._client.on("set_passengers", ({ entityId, passengers }) => {
    try {
      const list = passengers || [];
      for (const e of Object.values(bot.entities)) {
        if (e && e.vehicle && e.vehicle.id === entityId && !list.includes(e.id)) {
          const v = e.vehicle;
          if (v.passengers) {
            const i = v.passengers.indexOf(e);
            if (i !== -1) v.passengers.splice(i, 1);
          }
          e.vehicle = null;
        }
      }
      if (bot.vehicle && bot.vehicle.id === entityId && bot.entity && !list.includes(bot.entity.id)) {
        const v = bot.vehicle;
        bot.vehicle = null;
        bot.emit("dismount", v);
      }
    } catch (err) {
      console.log(`[${bot.username}] passenger patch: ${err.message}`);
    }
  });
}

/**
 * Mount an entity (empty hand right click) and wait for the server to confirm.
 * @returns {Promise<boolean>}
 */
async function mountEntity(bot, entity, timeoutMs = 2500) {
  if (!entity || !bot.entity) return false;
  if (bot.vehicle && bot.vehicle.id === entity.id) return true;
  await turnTo(bot, () => entityAim(entity, 0.5), 500, 18);
  const mounted = new Promise((resolve) => {
    const onMount = () => resolve(true);
    bot.once("mount", onMount);
    setTimeout(() => {
      bot.removeListener("mount", onMount);
      resolve(Boolean(bot.vehicle));
    }, timeoutMs);
  });
  try {
    bot.mount(entity); // emits action_mount (patched Mineflayer)
  } catch (err) {
    console.log(`[${bot.username}] mount failed: ${err.message}`);
    return false;
  }
  const ok = await mounted;
  if (ok) syncRiderPosition(bot);
  return ok;
}

/** Dismount (sends the unmount input) and wait for the server. */
async function dismountVehicle(bot, timeoutMs = 2500) {
  if (!bot.vehicle) return true;
  const done = new Promise((resolve) => {
    const onDis = () => resolve(true);
    bot.once("dismount", onDis);
    setTimeout(() => {
      bot.removeListener("dismount", onDis);
      resolve(!bot.vehicle);
    }, timeoutMs);
  });
  try {
    bot.dismount(); // emits action_dismount (patched Mineflayer)
  } catch (err) {
    console.log(`[${bot.username}] dismount failed: ${err.message}`);
  }
  let ok = await done;
  if (!ok) {
    // fallback: sneak (vanilla shift-dismount)
    bot.setControlState("sneak", true);
    await sleep(400);
    bot.setControlState("sneak", false);
    ok = !bot.vehicle;
  }
  return ok;
}

/**
 * Drive a client-controlled vehicle (saddled horse / pig / camel, boat) along
 * waypoints by sending vehicle_move packets, like a vanilla client does while
 * it controls a vehicle. The rider's head looks at `getLookTarget()` (e.g. the
 * other bot) when given, otherwise ahead.
 *
 * @param {*} bot
 * @param {Vec3[]} waypoints - feet-level points (y ignored for boats)
 * @param {Object} o
 * @param {number} o.speed - blocks per tick
 * @param {string} o.kind - "land" | "boat"
 * @param {number} o.durationMs - max duration
 * @param {Function} [o.groundY] - (x, z) => feet y of the vehicle at that column, or null
 * @param {Function} [o.getLookTarget] - () => Vec3|null
 * @param {Function} [o.partnerHead] - () => Vec3|null: when getLookTarget gives
 *   nothing, the rider looks at the partner whenever its bearing is within
 *   +-60 degrees of the travel direction (instead of straight ahead)
 * @param {boolean} [o.loop=true]
 * @param {Function} [o.onBoost] - called every ~4 s (carrot on a stick)
 * @returns {Promise<{ticks: number, distance: number, corrections: number, wall_skips: number, ground_check_off: boolean}>}
 */
async function driveVehicle(bot, waypoints, o) {
  const v = bot.vehicle;
  if (!v || !v.position) return { ticks: 0, distance: 0, corrections: 0, wall_skips: 0, ground_check_off: false };
  // A rider whose every waypoint looks like a wall (stale chunk data, wrong
  // vehicle y) would never move: after one full round of skips the ground
  // check is switched off (keep the current y).
  let wallSkips = 0;
  let wallSkipsInARow = 0;
  let groundCheckOff = false;
  let pos = v.position.clone();
  let wp = 0;
  let ticks = 0;
  let distance = 0;
  let corrections = 0;
  const until = Date.now() + o.durationMs;
  let headYaw = bot.entity.yaw;
  let headPitch = 0;
  let travelYawDeg = 0;
  let nextBoost = Date.now() + 2500;
  const onCorrection = (packet) => {
    pos = new Vec3(packet.x, packet.y, packet.z);
    corrections++;
  };
  bot._client.on("vehicle_move", onCorrection);
  // find the nearest waypoint to start from
  let best = 0;
  let bestD = Infinity;
  waypoints.forEach((w, i) => {
    const d = Math.hypot(w.x - pos.x, w.z - pos.z);
    if (d < bestD) {
      bestD = d;
      best = i;
    }
  });
  wp = o.startAtFirst ? 0 : (best + 1) % waypoints.length;
  let recentCorrections = 0;
  let lastCorrectionCheck = Date.now();
  let correctionsAtCheck = 0;
  try {
    while (Date.now() < until && bot.vehicle && !stopping(bot)) {
      // stuck (every move rejected, e.g. boat against boat / wall): next waypoint
      if (Date.now() - lastCorrectionCheck > 600) {
        recentCorrections = corrections - correctionsAtCheck;
        correctionsAtCheck = corrections;
        lastCorrectionCheck = Date.now();
        if (recentCorrections >= 10) {
          wp = (wp + 1) % waypoints.length;
        }
      }
      const target = waypoints[wp];
      const dx = target.x - pos.x;
      const dz = target.z - pos.z;
      const d = Math.hypot(dx, dz);
      if (d < 0.6) {
        wp++;
        if (wp >= waypoints.length) {
          if (o.loop === false) break;
          wp = 0;
        }
        continue;
      }
      const step = Math.min(o.getSpeed ? o.getSpeed() : o.speed, d);
      const nx = pos.x + (dx / d) * step;
      const nz = pos.z + (dz / d) * step;
      let ny = pos.y;
      if (o.kind !== "boat" && o.groundY && !groundCheckOff) {
        const gy = o.groundY(nx, nz);
        if (gy !== null && gy !== undefined && Math.abs(gy - pos.y) <= 1.05) ny = gy;
        else if (gy !== null && gy !== undefined && gy - pos.y > 1.05) {
          // wall: skip this waypoint
          wallSkips++;
          wallSkipsInARow++;
          if (wallSkipsInARow >= waypoints.length) {
            groundCheckOff = true;
            console.log(`[${bot.username}] driveVehicle: every waypoint looked like a wall (vehicle y ${pos.y.toFixed(2)}, ground ${gy}); ground check off`);
          }
          wp = (wp + 1) % waypoints.length;
          await sleep(50);
          continue;
        }
      }
      wallSkipsInARow = 0;
      // smooth turning of the vehicle
      const wantDeg = mcYawOfDir(dx, dz);
      let diff = ((wantDeg - travelYawDeg + 540) % 360) - 180;
      if (ticks === 0) diff = wantDeg - travelYawDeg;
      travelYawDeg += Math.max(-12, Math.min(12, diff));
      pos = new Vec3(nx, ny, nz);
      distance += step;
      try {
        bot._client.write("vehicle_move", { x: pos.x, y: pos.y, z: pos.z, yaw: travelYawDeg, pitch: 0 });
        if (o.kind === "boat") bot._client.write("steer_boat", { leftPaddle: true, rightPaddle: true });
        bot._client.write("steer_vehicle", { sideways: 0, forward: 0.98, jump: 0 });
      } catch (err) {
        console.log(`[${bot.username}] vehicle packet failed: ${err.message}`);
        break;
      }
      if (bot.vehicle && bot.vehicle.position) {
        bot.vehicle.position.x = pos.x;
        bot.vehicle.position.y = pos.y;
        bot.vehicle.position.z = pos.z;
      }
      syncRiderPosition(bot);
      // head: look at target (other bot) or ahead
      let lt = o.getLookTarget ? o.getLookTarget() : null;
      if (!lt && o.partnerHead) {
        // partner roughly ahead (+-60 deg of the travel direction): look at it
        const ph = o.partnerHead();
        if (ph) {
          const bearingDeg = mcYawOfDir(ph.x - pos.x, ph.z - pos.z);
          const off = Math.abs(((bearingDeg - travelYawDeg + 540) % 360) - 180);
          if (off <= 60) lt = ph;
        }
      }
      let wantYaw;
      let wantPitch;
      if (lt) {
        const eye = bot.entity.position.offset(0, 1.62, 0);
        wantYaw = Math.atan2(-(lt.x - eye.x), -(lt.z - eye.z));
        wantPitch = Math.atan2(lt.y - eye.y, Math.hypot(lt.x - eye.x, lt.z - eye.z));
      } else {
        wantYaw = Math.PI - (travelYawDeg * Math.PI) / 180;
        wantPitch = -0.08;
      }
      const maxStep = (10 * Math.PI) / 180;
      headYaw += Math.max(-maxStep, Math.min(maxStep, wrapPi(wantYaw - headYaw)));
      headPitch += Math.max(-maxStep, Math.min(maxStep, wantPitch - headPitch));
      sendRiderLook(bot, headYaw, headPitch);
      if (process.env.RIDE_DEBUG === "1" && ticks % 40 === 0) {
        console.log(
          `[${bot.username}] [ride-debug] tick ${ticks} vehicle=${bot.vehicle && bot.vehicle.name} headYaw=${headYaw.toFixed(3)} entityYaw=${bot.entity.yaw.toFixed(3)} lt=${Boolean(lt)} pos=${pos.x.toFixed(1)},${pos.z.toFixed(1)}`,
        );
      }
      if (o.onBoost && Date.now() >= nextBoost) {
        nextBoost = Date.now() + 4000 + Math.random() * 3000;
        try {
          o.onBoost();
        } catch (_) {}
      }
      ticks++;
      await sleep(50);
    }
  } finally {
    bot._client.removeListener("vehicle_move", onCorrection);
    try {
      bot._client.write("steer_vehicle", { sideways: 0, forward: 0, jump: 0 });
      if (o.kind === "boat") bot._client.write("steer_boat", { leftPaddle: false, rightPaddle: false });
    } catch (_) {}
  }
  return { ticks, distance: Number(distance.toFixed(2)), corrections, wall_skips: wallSkips, ground_check_off: groundCheckOff };
}

/**
 * Ride a server-controlled vehicle (minecart, untamed horse, another bot's
 * camel): keep the rider position in sync and look at `getLookTarget()`.
 */
async function rideAlong(bot, durationMs, getLookTarget = null, stopWhen = null) {
  const until = Date.now() + durationMs;
  let headYaw = bot.entity.yaw;
  let headPitch = bot.entity.pitch;
  while (Date.now() < until && bot.vehicle && !stopping(bot)) {
    if (stopWhen && stopWhen()) break;
    syncRiderPosition(bot);
    const lt = getLookTarget ? getLookTarget() : null;
    if (lt) {
      const eye = bot.entity.position.offset(0, 1.62, 0);
      const wantYaw = Math.atan2(-(lt.x - eye.x), -(lt.z - eye.z));
      const wantPitch = Math.atan2(lt.y - eye.y, Math.hypot(lt.x - eye.x, lt.z - eye.z));
      const maxStep = (10 * Math.PI) / 180;
      headYaw += Math.max(-maxStep, Math.min(maxStep, wrapPi(wantYaw - headYaw)));
      headPitch += Math.max(-maxStep, Math.min(maxStep, wantPitch - headPitch));
    }
    sendRiderLook(bot, headYaw, headPitch);
    await sleep(50);
  }
  return !bot.vehicle;
}

/** Waypoints of a closed rectangle loop around `center` (frame axes u/p). */
function loopWaypoints(frame, halfU, halfP, y) {
  const c = frame.site;
  const pts = [];
  const corner = (su, sp) =>
    new Vec3(c.x + 0.5 + frame.u.x * su * halfU + frame.p.x * sp * halfP, y, c.z + 0.5 + frame.u.z * su * halfU + frame.p.z * sp * halfP);
  const corners = [corner(1, 1), corner(1, -1), corner(-1, -1), corner(-1, 1)];
  // add mid points on edges for smoother turns
  for (let i = 0; i < 4; i++) {
    const a = corners[i];
    const b = corners[(i + 1) % 4];
    pts.push(a);
    pts.push(new Vec3((a.x + b.x) / 2, y, (a.z + b.z) / 2));
  }
  return pts;
}

/**
 * Give items: entries are [name, count] (topped up like the other episodes)
 * or {raw, name, count} where raw is an item with components, e.g.
 * 'bow[minecraft:enchantments={levels:{"minecraft:power":2}}]'.
 */
async function giveItemsExt(bot, rcon, list) {
  const plain = [];
  for (const it of list || []) {
    if (Array.isArray(it)) plain.push(it);
    else if (it && it.raw) await rconSend(bot, rcon, `give ${bot.username} minecraft:${it.raw} ${it.count || 1}`);
  }
  await giveItems(bot, rcon, plain);
  await sleep(300);
}

// ----------------------------------------------------------------------------
// Arena (setup)
// ----------------------------------------------------------------------------

const FALLBACK_GROUND = "grass_block";

// Plants removed from a flat site (block ids / tags for `fill ... replace`).
const CLEAR_PLANTS = [
  "minecraft:short_grass",
  "minecraft:tall_grass",
  "minecraft:fern",
  "minecraft:large_fern",
  "minecraft:dead_bush",
  "#minecraft:flowers",
  "minecraft:sweet_berry_bush",
];

/** A ground block name that is fine to use as the levelled surface. */
function groundBlockName(bot, pos) {
  const b = bot.blockAt(pos);
  const n = b ? b.name : null;
  if (!n) return FALLBACK_GROUND;
  if (n.includes("leaves") || n.includes("log") || n.includes("water") || n.includes("lava") || n.includes("ice") || n === "snow" || n.includes("glass")) {
    return FALLBACK_GROUND;
  }
  if (b.boundingBox !== "block") return FALLBACK_GROUND;
  return n;
}

/**
 * RCON commands that level a square (ground at y = groundY, `clearUp` air
 * blocks above it). Used only in setup, before the start snapshot.
 */
function levelCommands(x1, z1, x2, z2, groundY, topBlock, clearUp = 5) {
  return [
    `fill ${x1} ${groundY + 1} ${z1} ${x2} ${groundY + clearUp} ${z2} minecraft:air`,
    `fill ${x1} ${groundY} ${z1} ${x2} ${groundY} ${z2} minecraft:${topBlock} replace minecraft:air`,
    `fill ${x1} ${groundY} ${z1} ${x2} ${groundY} ${z2} minecraft:${topBlock} replace minecraft:water`,
    `fill ${x1} ${groundY - 1} ${z1} ${x2} ${groundY - 1} ${z2} minecraft:dirt replace minecraft:air`,
    `fill ${x1} ${groundY - 1} ${z1} ${x2} ${groundY - 1} ${z2} minecraft:dirt replace minecraft:water`,
  ];
}

/** Fraction of the (2*half+1)^2 columns around `site` whose ground is at site.y with 2 free blocks above. */
function areaFlatFraction(bot, site, half) {
  let ok = 0;
  let n = 0;
  for (let dx = -half; dx <= half; dx++) {
    for (let dz = -half; dz <= half; dz++) {
      n++;
      const g = groundAt(bot, site.x + dx, site.z + dz);
      if (!g || g.y !== site.y) continue;
      const b = bot.blockAt(g);
      if (!b || b.boundingBox !== "block" || b.name.includes("leaves") || b.name.includes("water")) continue;
      const a1 = bot.blockAt(g.offset(0, 1, 0));
      const a2 = bot.blockAt(g.offset(0, 2, 0));
      const free = (x) => !x || x.boundingBox === "empty";
      if (free(a1) && free(a2)) ok++;
    }
  }
  return n ? ok / n : 0;
}

/** Water source blocks within `radius` (horizontal) of `center`, at most `limit` found. */
function waterNear(bot, center, radius = 16, limit = 3) {
  let n = 0;
  for (let dx = -radius; dx <= radius; dx += 2) {
    for (let dz = -radius; dz <= radius; dz += 2) {
      const g = groundAt(bot, Math.floor(center.x) + dx, Math.floor(center.z) + dz);
      if (!g) continue;
      for (let dy = 0; dy <= 2; dy++) {
        const b = bot.blockAt(g.offset(0, dy, 0));
        if (b && b.name === "water") {
          n++;
          break;
        }
      }
      if (n >= limit) return true;
    }
  }
  return n > 0;
}

// ----------------------------------------------------------------------------
// Base class
// ----------------------------------------------------------------------------

/**
 * Site-based base class. Subclasses implement
 *   planEpisode(bot, rng, args, frame, isActor, info) -> {
 *     actorStand: Vec3 (block column), observerStand: Vec3,
 *     items: {actor: [[name, count]], observer: [[name, count]]},
 *     equipment: {actor: loadout|null, observer: loadout|null} (optional),
 *     terrainCommands: string[] (leader, before the teleport),
 *     summonCommands: () => string[] (leader, last step of setup),
 *     metadata: {...}
 *   }
 * and buildSteps(ctx) -> steps[] (see state-common startPhaseChain).
 *
 * `info` = {biome, biome_group, flat_site, water_near, is_day, time_of_day, leader}.
 */
class CreatureEpisodeBase extends BaseEpisode {
  static INIT_MIN_BOTS_DISTANCE = 5;
  static INIT_MAX_BOTS_DISTANCE = 10;
  static WORKS_IN_NON_FLAT_WORLD = true;
  static SITE_HALF_SIZE = 3;
  // Half size of the square levelled with /fill when no naturally flat site
  // is found (normal worlds) - must cover all walking / riding paths.
  static LEVEL_HALF_SIZE = 6;
  static CLEAR_HEIGHT = 5;
  // After the episode (world log closed) the leader removes every non-player
  // entity within this radius of the site: bred babies, chicks, vexes and
  // anything the episode summoned (episodes-loop cleanupEpisodeEntities).
  static CLEANUP_ENTITIES_RADIUS = 32;

  async setupEpisode(bot, rcon, sharedBotRng, coordinator, episodeNum, args, botPosition, otherBotPosition) {
    const C = this.constructor;
    ensurePassengerPatch(bot);
    const leader = isWorldStateLeader(bot, args);
    const mid = new Vec3(
      (botPosition.x + otherBotPosition.x) / 2,
      (botPosition.y + otherBotPosition.y) / 2,
      (botPosition.z + otherBotPosition.z) / 2,
    );
    // 1) Site and world facts, read on UNEDITED terrain by both bots; the
    //    follower adopts the leader's (handshake "cre_site" is a barrier: the
    //    leader edits nothing before both bots passed it). The leader also
    //    resolves the real (server-side) biome at the site.
    let site = findFlatSite(bot, mid, C.SITE_HALF_SIZE, 14);
    let flat = Boolean(site);
    if (!site) site = groundAt(bot, Math.floor(mid.x), Math.floor(mid.z));
    let facts = { none: true };
    if (site) {
      const clientBiome = biomeAt(bot, site.offset(0, 1, 0)) || biomeAt(bot, site) || null;
      facts = {
        site: [site.x, site.y, site.z],
        flat,
        biome_client: clientBiome,
        biome: clientBiome,
        biome_source: clientBiome ? "client" : null,
        water_near: waterNear(bot, site, 16),
      };
      if (leader) {
        const rb = await withTimeout(
          resolveBiome(bot, rcon, site.offset(0, 1, 0), clientBiome ? [clientBiome] : []),
          9000,
          null,
          "resolveBiome",
        );
        if (rb && rb.biome) {
          facts.biome = rb.biome;
          facts.biome_source = rb.source;
        }
      }
    }
    const theirs = await exchange(coordinator, episodeNum, "cre_site", facts, 25000);
    let handshake = leader ? "leader" : "own_timeout";
    if (!leader && theirs && (theirs.site || theirs.none)) {
      facts = theirs;
      handshake = "adopted";
    }
    if (!facts.site) throw new Error(`[${C.name}] no ground near ${mid}`);
    site = new Vec3(facts.site[0], facts.site[1], facts.site[2]);
    flat = Boolean(facts.flat);
    const biome = facts.biome || null;
    if (leader) {
      this._siteBiome = { biome, source: facts.biome_source || null, client: facts.biome_client || null, pos: [site.x, site.y + 1, site.z] };
    }

    // 2) Shared random choices (same order on both bots)
    const isActor = decidePrimaryBot(bot, sharedBotRng, args);
    const axis = sharedBotRng() < 0.5 ? "x" : "z";
    const side = sharedBotRng() < 0.5 ? -1 : 1;
    const frame = makeFrame(site, axis, side);
    const env = this._envMetadata || {};
    const tod = Number(env.time_of_day);
    const isDay = !Number.isFinite(tod) || (tod % 24000) < 12300 || (tod % 24000) > 23500;
    const info = {
      biome,
      biome_group: biomeGroup(biome),
      flat_site: flat,
      water_near: Boolean(facts.water_near),
      is_day: isDay,
      time_of_day: Number.isFinite(tod) ? tod : null,
      leader,
      episodeNum,
    };
    const plan = this.planEpisode(bot, sharedBotRng, args, frame, isActor, info);
    const half = flat ? C.SITE_HALF_SIZE : C.LEVEL_HALF_SIZE;
    const levelHalf = Math.max(half, plan.levelHalf || 0);
    // A flat 7x7 core does not mean the whole arena is walkable (normal
    // worlds): level it when less than 85 % of the arena columns are flat.
    // Measured before any edit; the follower adopts the leader's value
    // (handshake "cre_plan", a barrier before the leader's /fill commands).
    let flatFraction = areaFlatFraction(bot, site, levelHalf);
    const planTheirs = await exchange(coordinator, episodeNum, "cre_plan", { flat_fraction: flatFraction }, 25000);
    if (!leader && planTheirs && Number.isFinite(Number(planTheirs.flat_fraction))) {
      flatFraction = Number(planTheirs.flat_fraction);
    }
    if (flat && flatFraction < 0.85) plan.forceLevel = true;
    info.arena_flat_fraction = Number(flatFraction.toFixed(3));

    if (leader) {
      // Remove leftovers of earlier creature episodes near this site.
      await rconSend(bot, rcon, `execute positioned ${site.x} ${site.y} ${site.z} run kill @e[tag=${SUMMON_TAG},distance=..96]`);
      await rconSend(bot, rcon, `execute positioned ${site.x} ${site.y} ${site.z} run kill @e[type=minecraft:item,distance=..48]`);
      await rconSend(bot, rcon, `execute positioned ${site.x} ${site.y} ${site.z} run kill @e[type=minecraft:arrow,distance=..48]`);
      const x1 = site.x - levelHalf;
      const x2 = site.x + levelHalf;
      const z1 = site.z - levelHalf;
      const z2 = site.z + levelHalf;
      // Snow layers are kept (or put back after levelling): removing them in a
      // snowy biome lets snow re-form during the episode when it snows, and
      // the world log does not re-read the grass_block[snowy] change below a
      // newly formed snow layer (replay mismatch).
      const snowTop = (() => {
        const b = bot.blockAt(site.offset(0, 1, 0));
        return Boolean(b && b.name === "snow");
      })();
      if (flat && !plan.forceLevel) {
        // clear grass / flowers on the site (not snow layers)
        for (const plant of CLEAR_PLANTS) {
          await rconSend(bot, rcon, `fill ${x1} ${frame.level} ${z1} ${x2} ${frame.level + 2} ${z2} minecraft:air replace ${plant}`);
        }
      } else {
        const top = groundBlockName(bot, site);
        for (const cmd of levelCommands(x1, z1, x2, z2, site.y, top, C.CLEAR_HEIGHT)) {
          await rconSend(bot, rcon, cmd);
        }
        if (snowTop) await rconSend(bot, rcon, `fill ${x1} ${frame.level} ${z1} ${x2} ${frame.level} ${z2} minecraft:snow replace minecraft:air`);
      }
      for (const cmd of plan.terrainCommands || []) await rconSend(bot, rcon, cmd);
    }
    // 3) barrier: the follower reads the terrain only after the leader's edits
    await exchange(coordinator, episodeNum, "cre_ready", {}, 30000);

    // Stand positions: inside the levelled square the feet level is known
    // (frame.level) - do not read the ground (the leader may still be filling).
    const standY = (col) => {
      if (Math.abs(col.x - site.x) <= levelHalf && Math.abs(col.z - site.z) <= levelHalf) return frame.level;
      const g = groundAt(bot, col.x, col.z);
      return g ? g.y + 1 : frame.level;
    };
    const myCol = isActor ? plan.actorStand : plan.observerStand;
    const otherCol = isActor ? plan.observerStand : plan.actorStand;
    const me = new Vec3(myCol.x + 0.5, standY(myCol), myCol.z + 0.5);
    const other = new Vec3(otherCol.x + 0.5, standY(otherCol), otherCol.z + 0.5);
    await sleep(300);
    await rconTp(rcon, bot.username, me.x, me.y, me.z);
    await sleep(1000);

    await giveItemsExt(bot, rcon, isActor ? plan.items.actor : plan.items.observer);
    const loadout = plan.equipment ? (isActor ? plan.equipment.actor : plan.equipment.observer) : null;
    if (loadout) {
      for (const cmd of equipmentCommands(bot.username, loadout)) await rconSend(bot, rcon, cmd);
    }
    if (C.DAMAGE_ENABLED) {
      // Respawn in the arena after a death (doImmediateRespawn is on).
      await rconSend(bot, rcon, `spawnpoint ${bot.username} ${Math.floor(me.x)} ${Math.floor(me.y)} ${Math.floor(me.z)}`);
    }
    await unequipHand(bot);

    if (leader && plan.summonCommands) {
      await sleep(400);
      const cmds = typeof plan.summonCommands === "function" ? plan.summonCommands(bot) : plan.summonCommands;
      const results = [];
      for (const cmd of cmds) {
        const r = await rconSend(bot, rcon, cmd);
        results.push(r !== null && /Summoned/i.test(String(r)));
      }
      this._summonResults = results;
    }

    this._frame = frame;
    this._isActor = isActor;
    this._plan = plan;
    this._info = info;
    this._worldStateCenter = centerOf(site);
    this._stateMetadata = {
      episode: C.name,
      site: [site.x, site.y, site.z],
      axis,
      side,
      actor: isActor ? bot.username : args.other_bot_name,
      observer: isActor ? args.other_bot_name : bot.username,
      biome: biome || null,
      biome_source: facts.biome_source || null,
      biome_client: facts.biome_client || null,
      biome_group: info.biome_group,
      site_handshake: handshake,
      flat_site: flat,
      levelled_half_size: flat && !plan.forceLevel ? null : levelHalf,
      arena_flat_fraction: info.arena_flat_fraction,
      water_near: info.water_near,
      ...plan.metadata,
    };
    if (leader && this._summonResults) {
      this._stateMetadata.summon_ok = this._summonResults.filter(Boolean).length;
      this._stateMetadata.summon_failed = this._summonResults.filter((x) => !x).length;
    }
    console.log(`[${bot.username}] ${C.name} setup: ${JSON.stringify(this._stateMetadata)}`);
    return { botPositionNew: me, otherBotPositionNew: other };
  }

  planEpisode() {
    throw new Error("planEpisode() must be implemented");
  }

  buildSteps() {
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
      info: this._info,
      isActor: this._isActor,
      episode: this,
      other: args.other_bot_name,
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
      steps: bindSteps(ctx, this.buildSteps(ctx)),
    });
  }

  /** Register a bot listener that is removed in tearDownEpisode. */
  listen(bot, event, fn) {
    if (!this._listeners) this._listeners = [];
    bot.on(event, fn);
    this._listeners.push([event, fn]);
  }

  async tearDownEpisode(bot, rcon) {
    for (const [event, fn] of this._listeners || []) bot.removeListener(event, fn);
    this._listeners = [];
    try {
      if (bot.usingHeldItem) releaseItem(bot);
    } catch (_) {}
    if (bot.vehicle) await withTimeout(dismountVehicle(bot, 1500), 2500, false);
    clearControls(bot);
    stopAll(bot);
    if (bot.pathfinder) bot.pathfinder.setGoal(null);
    if (this.constructor.REMOVE_EQUIPMENT_AFTER) {
      for (const cmd of unequipCommands(bot.username)) await rconSend(bot, rcon, cmd);
    }
    try {
      await unequipHand(bot);
    } catch (_) {}
  }
}

/** First step: both face the site / the other bot. */
function readyStep(name = "ready", lookAtOtherFirst = true) {
  return {
    name,
    run: async function () {
      const ctx = this;
      safePathfinder(ctx.bot);
      if (lookAtOtherFirst) await lookAtOtherBot(ctx.bot, ctx.other, 90);
      else await lookAtPoint(ctx.bot, ctx.siteCenter, 80);
      await sleep(localJitterMs(500, 900));
      return {};
    },
  };
}

/** Last step: eye contact (stepping back first when the partner is too close). */
function outroStep(name = "outro") {
  return {
    name,
    run: async function () {
      const ctx = this;
      clearControls(ctx.bot);
      if (ctx.bot.vehicle) await dismountVehicle(ctx.bot);
      await keepApart(ctx.bot, ctx.other, 2.5);
      await glanceAtOther(ctx.bot, ctx.other, localJitterMs(1200, 1800));
      return {};
    },
  };
}

module.exports = {
  ARMOR_TIERS,
  CreatureEpisodeBase,
  TIERS,
  backOff,
  ballisticPitch,
  bowPower,
  chooseLoadout,
  clearControls,
  collectItems,
  consumeHeld,
  countItem,
  dismountVehicle,
  driveVehicle,
  dropHeld,
  ensurePassengerPatch,
  entitiesOfType,
  entitiesWhere,
  entityAim,
  equip,
  equipItem,
  equipmentCommands,
  eyePos,
  findItem,
  giveItemsExt,
  glanceAtOther,
  groundBlockName,
  headOf,
  hasItem,
  interactEntity,
  isAlive,
  itemEntitiesNear,
  keepApart,
  levelCommands,
  localJitterMs,
  lookAtOtherBot,
  lookAtPoint,
  loopWaypoints,
  makeVelocityTracker,
  mcYawOfDir,
  mcYawPitch,
  meleeEngage,
  mountEntity,
  nearestWhere,
  note,
  otherHead,
  outroStep,
  playerEntity,
  rangedEngage,
  rconSend,
  readyStep,
  releaseItem,
  rideAlong,
  safePathfinder,
  sendRiderLook,
  sleep,
  startWatching,
  stopping,
  syncRiderPosition,
  turnTo,
  turnToward,
  unequipCommands,
  useItem,
  v3,
  walkNear,
  watchFor,
  weaponCooldownMs,
  withTimeout,
  yawTo,
};
