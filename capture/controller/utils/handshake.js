/**
 * handshake.js - Small leader/follower handshake helpers for episode setup
 * (round 2 fixer).
 *
 * Both bots run setupEpisode at slightly different times. Anything that is
 * read from the world (site search, flatness, biome, water nearby) must be
 * read BEFORE the leader starts its /fill edits, and both bots must use the
 * same result, otherwise the plan (and the shared RNG consumption that may
 * depend on it) differs between the bots. The pattern is:
 *
 *   1. both compute a candidate on unedited terrain,
 *      `exchange(..., "x_site", mine)` - barrier; the follower adopts the
 *      leader's payload (`reviveVec3` restores Vec3 objects);
 *   2. both plan (shared RNG), `exchange(..., "x_plan", ...)` - barrier, so
 *      the follower has finished all its world reads before any edit;
 *   3. the leader edits the world, `exchange(..., "x_ready", {})` - barrier,
 *      so the follower reads the edited terrain only after the edits.
 */

const { Vec3 } = require("vec3");

/**
 * Exchange one payload with the other bot (barrier semantics: resolves once
 * both bots reached this call). The handler re-sends our payload, so
 * whichever bot arrives last still releases the other one. Resolves with the
 * other bot's payload, or null on timeout.
 * @param {*} coordinator
 * @param {number} episodeNum
 * @param {string} name - unique per episode
 * @param {*} payload - JSON-serialisable
 * @param {number} [timeoutMs=20000]
 * @returns {Promise<*>}
 */
function exchange(coordinator, episodeNum, name, payload, timeoutMs = 20000) {
  return new Promise((resolve) => {
    let done = false;
    const timer = setTimeout(() => {
      if (!done) {
        done = true;
        console.log(`[handshake] exchange ${name} timed out after ${timeoutMs} ms`);
        resolve(null);
      }
    }, timeoutMs);
    coordinator.onceEvent(name, episodeNum, async (other) => {
      coordinator.sendToOtherBot(name, payload, episodeNum, `${name} reply`);
      if (done) return;
      done = true;
      clearTimeout(timer);
      resolve(other === undefined || other === null ? {} : other);
    });
    coordinator.sendToOtherBot(name, payload, episodeNum, `${name} offer`);
  });
}

/** Deep copy through JSON (Vec3 -> {x, y, z}). */
function toPlain(obj) {
  return obj === undefined ? undefined : JSON.parse(JSON.stringify(obj));
}

/**
 * Restore Vec3 objects in a JSON-decoded structure: every plain object whose
 * only keys are numeric x, y, z becomes a Vec3. Returns a new structure.
 * @param {*} obj
 * @returns {*}
 */
function reviveVec3(obj) {
  if (Array.isArray(obj)) return obj.map(reviveVec3);
  if (obj && typeof obj === "object") {
    const keys = Object.keys(obj);
    if (
      keys.length === 3 &&
      typeof obj.x === "number" &&
      typeof obj.y === "number" &&
      typeof obj.z === "number"
    ) {
      return new Vec3(obj.x, obj.y, obj.z);
    }
    const out = {};
    for (const k of keys) out[k] = reviveVec3(obj[k]);
    return out;
  }
  return obj;
}

module.exports = { exchange, reviveVec3, toPlain };
