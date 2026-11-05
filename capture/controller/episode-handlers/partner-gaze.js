/**
 * partner-gaze.js - Observer layer for the upstream build episodes
 * (buildTower, towerBridge, buildHouse; round-2 fixer, 03 section 5 / T8).
 *
 * The upstream build types had no observer role: both bots looked at their
 * own placements (down) most of the time, so the partner was in view in only
 * 13-39 % of the frames. These helpers let the idle bot watch the working
 * one (turn-based building) and let a working bot glance at its partner
 * between placements.
 *
 * Runtime jitter uses Math.random (it never has to match across bots);
 * every wait has a timeout.
 */

const { startWatching } = require("./state/state-common");
const { buildPhase } = require("../primitives/building");
const { exchange } = require("../utils/handshake");

function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

/** Head position of the other player (null if not visible to this client). */
function partnerHead(bot, otherName) {
  const e = bot.players[otherName]?.entity;
  if (!e || !e.position) return null;
  return e.position.offset(0, (e.height || 1.8) * 0.85, 0);
}

/**
 * Keep the camera on the partner until `promise` settles (or `maxMs`
 * elapses, or the episode stops). Resolves with the promise's value (or
 * null on timeout).
 */
async function watchPartnerUntil(bot, otherName, promise, maxMs = 60000) {
  let settled = false;
  let value = null;
  const p = Promise.resolve(promise).then(
    (v) => {
      settled = true;
      value = v;
    },
    () => {
      settled = true;
    },
  );
  const stop = startWatching(bot, () => partnerHead(bot, otherName), {
    minMs: 500,
    maxMs: 1100,
    jitter: 0.5,
    dps: 120,
  });
  const t0 = Date.now();
  try {
    while (!settled && Date.now() - t0 < maxMs && !bot._episodeStopping) {
      await Promise.race([p, sleep(150)]);
    }
  } finally {
    await stop();
  }
  return value;
}

/** Watch the partner for `ms` (natural small jitter). */
async function watchPartnerFor(bot, otherName, ms) {
  return watchPartnerUntil(bot, otherName, sleep(ms), ms + 500);
}

/**
 * Glance at the partner for ~`ms` (only when the partner entity is known).
 * @returns {Promise<boolean>} whether a glance happened
 */
async function glanceAtPartner(bot, otherName, ms = 800) {
  if (!partnerHead(bot, otherName) || bot._episodeStopping) return false;
  await watchPartnerFor(bot, otherName, ms);
  return true;
}

/**
 * Turn-based work: `first` works while the other bot watches it, then the
 * other works while `first` watches. Each bot calls this with its own role.
 * Both resolve after both turns (or the timeouts).
 * @param {Object} o
 * @param {*} o.bot
 * @param {*} o.coordinator
 * @param {number} o.episodeNum
 * @param {string} o.otherName
 * @param {string} o.name - unique event prefix per episode
 * @param {boolean} o.iAmFirst
 * @param {Function} o.work - async () => result (our turn)
 * @param {number} [o.turnTimeoutMs=45000] - max wait for the other's turn
 * @returns {Promise<*>} result of our work
 */
async function takeTurns({ bot, coordinator, episodeNum, otherName, name, iAmFirst, work, turnTimeoutMs = 45000 }) {
  let result;
  if (iAmFirst) {
    result = await work();
    await exchange(coordinator, episodeNum, `${name}_turn1`, { done: true }, turnTimeoutMs);
    await watchPartnerUntil(
      bot,
      otherName,
      exchange(coordinator, episodeNum, `${name}_turn2`, { waiting: true }, turnTimeoutMs),
      turnTimeoutMs + 1000,
    );
  } else {
    await watchPartnerUntil(
      bot,
      otherName,
      exchange(coordinator, episodeNum, `${name}_turn1`, { waiting: true }, turnTimeoutMs),
      turnTimeoutMs + 1000,
    );
    result = await work();
    await exchange(coordinator, episodeNum, `${name}_turn2`, { done: true }, turnTimeoutMs);
  }
  return result;
}

/** Sort like buildPhase does (placementOrder, else bottom-up then near-to-far). */
function sortTargets(bot, targets) {
  const botPos = bot.entity.position;
  return targets.slice().sort((a, b) => {
    if (a.placementOrder !== undefined && b.placementOrder !== undefined) {
      return a.placementOrder - b.placementOrder;
    }
    if (a.worldPos.y !== b.worldPos.y) return a.worldPos.y - b.worldPos.y;
    return botPos.distanceTo(a.worldPos) - botPos.distanceTo(b.worldPos);
  });
}

/**
 * primitives/building buildPhase in chunks of `every` blocks with a partner
 * glance (`glanceMs` [min, max]) between the chunks. Same return shape as
 * buildPhase ({success, failed, aborted}); same placement order.
 */
async function buildPhaseWithGlances(bot, targets, otherName, options, { every = 3, glanceMs = [700, 1200] } = {}) {
  const sorted = sortTargets(bot, targets).map((t, i) =>
    t.placementOrder !== undefined ? t : { ...t, placementOrder: i },
  );
  const total = { success: 0, failed: 0, aborted: false };
  for (let i = 0; i < sorted.length; i += every) {
    const chunk = sorted.slice(i, i + every);
    const r = await buildPhase(bot, chunk, options);
    total.success += r.success || 0;
    total.failed += r.failed || 0;
    if (r.aborted) {
      total.aborted = true;
      break;
    }
    if (i + every < sorted.length) {
      const ms = glanceMs[0] + Math.random() * (glanceMs[1] - glanceMs[0]);
      await glanceAtPartner(bot, otherName, ms);
    }
  }
  return total;
}

module.exports = {
  buildPhaseWithGlances,
  glanceAtPartner,
  partnerHead,
  takeTurns,
  watchPartnerFor,
  watchPartnerUntil,
};
