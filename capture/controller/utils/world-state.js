/**
 * world-state.js - Controller side of the per-episode world-state logging.
 *
 * The MirrorBot Paper plugin exposes a `worldstate` command (see
 * server/episode-manager-plugin/.../WorldStateLogger.java). The leader bot
 * (the one whose name sorts first, i.e. Alpha) drives it over RCON:
 *
 *   begin  right before both bots start recording an episode
 *          -> opens <stem>_world_events.jsonl and writes the start snapshot
 *   end    after the episode is torn down (optionally restoring the box)
 *          -> writes the end snapshot and closes the log
 *
 * Both bots may add free-text annotations with `note` and both query `info`
 * when writing their episode_info JSON.
 *
 * Nothing here is allowed to break an episode: every call is wrapped and
 * returns null on failure.
 */

/**
 * Left-pad a value with zeros.
 * @param {number|string} value
 * @param {number} length
 * @returns {string}
 */
function pad(value, length = 2) {
  return String(value).padStart(length, "0");
}

/**
 * Same timestamp format as the other per-episode files (YYYYMMDD_HHMMSS).
 * @param {Date} date
 * @returns {string}
 */
function formatDateForFilename(date) {
  return `${date.getFullYear()}${pad(date.getMonth() + 1)}${pad(
    date.getDate(),
  )}_${pad(date.getHours())}${pad(date.getMinutes())}${pad(date.getSeconds())}`;
}

/**
 * The leader drives world-state logging and teleports (same rule as teleport()).
 * @param {*} bot
 * @param {Object} args
 * @returns {boolean}
 */
function isWorldStateLeader(bot, args) {
  return bot.username < args.other_bot_name;
}

/**
 * File stem shared by all world files of one episode:
 *   <YYYYMMDD_HHMMSS>_<episode:06d>_world_instance_<instance:03d>
 * It matches the (episode, instance) parsing used by postprocess/*.py.
 * @param {Object} args
 * @param {number} episodeNum
 * @returns {string}
 */
function buildWorldStem(args, episodeNum) {
  const instanceId = args.instance_id ?? 0;
  return `${formatDateForFilename(new Date())}_${pad(episodeNum, 6)}_world_instance_${pad(instanceId, 3)}`;
}

/**
 * Extract the first JSON object from an RCON response.
 * @param {string} res
 * @returns {Object|null}
 */
function parseJsonResponse(res) {
  if (res === undefined || res === null) return null;
  const text = String(res);
  const start = text.indexOf("{");
  const end = text.lastIndexOf("}");
  if (start < 0 || end <= start) return null;
  try {
    return JSON.parse(text.slice(start, end + 1));
  } catch (err) {
    return null;
  }
}

/**
 * Parse WORLD_GEN_INFO (JSON produced by generate_compose.py).
 * @param {Object} args
 * @returns {Object}
 */
function parseWorldGenInfo(args) {
  if (!args.world_gen_info) return {};
  try {
    return JSON.parse(String(args.world_gen_info));
  } catch (err) {
    console.warn(`[world-state] could not parse WORLD_GEN_INFO: ${err.message}`);
    return {};
  }
}

/**
 * Ask the server for the real world seed (`/seed` -> "Seed: [123]").
 * @param {*} rcon
 * @returns {Promise<string|null>} seed as a decimal string (64-bit safe) or null
 */
async function queryWorldSeed(rcon) {
  try {
    const res = String(await rcon.send("seed"));
    const m = res.match(/-?\d+/);
    return m ? m[0] : null;
  } catch (err) {
    console.warn(`[world-state] seed query failed: ${err.message}`);
    return null;
  }
}

/**
 * Open the per-episode world log and take the start snapshot.
 * @param {*} bot
 * @param {*} rcon
 * @param {Object} args
 * @param {number} episodeNum
 * @param {string} episodeType
 * @param {Object|null} center - optional {x,y,z} snapshot centre (episode site)
 * @returns {Promise<Object|null>} plugin reply (stem, files, seed, game_time, ...)
 */
async function worldStateBegin(bot, rcon, args, episodeNum, episodeType, center = null) {
  if (!Number(args.world_state_logging)) return null;
  const stem = buildWorldStem(args, episodeNum);
  const parts = [
    "worldstate begin",
    `stem=${stem}`,
    `episode=${episodeNum}`,
    `instance=${args.instance_id ?? 0}`,
    `type=${episodeType || "unknown"}`,
    `players=${bot.username},${args.other_bot_name}`,
    `radius=${Number(args.world_snapshot_radius)}`,
    `down=${Number(args.world_snapshot_down)}`,
    `up=${Number(args.world_snapshot_up)}`,
    `log_radius=${Number(args.world_log_radius)}`,
    `snapshot=${args.world_snapshot_mode || "blocks"}`,
    `heartbeat=${Number(args.world_heartbeat_ticks) || 1}`,
    `entities_every=${Number(args.world_entities_every) || 1}`,
    `entities_radius=${Number(args.world_entities_radius) || 64}`,
  ];
  if (center && Number.isFinite(center.x) && Number.isFinite(center.z)) {
    parts.push(
      `center=${center.x.toFixed(2)},${Number(center.y).toFixed(2)},${center.z.toFixed(2)}`,
    );
  }
  const cmd = parts.join(" ");
  try {
    const res = await rcon.send(cmd);
    const info = parseJsonResponse(res);
    console.log(`[${bot.username}] [world-state] ${cmd} -> ${String(res).slice(0, 400)}`);
    if (!info || !info.ok) return null;
    return info;
  } catch (err) {
    console.warn(`[${bot.username}] [world-state] begin failed: ${err.message}`);
    return null;
  }
}

/**
 * Close the per-episode world log, write the end snapshot and optionally
 * restore the snapshot box to its initial state.
 * @param {*} bot
 * @param {*} rcon
 * @param {Object} args
 * @param {string} stem
 * @returns {Promise<Object|null>}
 */
async function worldStateEnd(bot, rcon, args, stem) {
  if (!Number(args.world_state_logging) || !stem) return null;
  const restore = Number(args.reset_world_between_episodes) ? 1 : 0;
  const snapshot = Number(args.world_end_snapshot) ? "blocks" : "none";
  const cmd = `worldstate end stem=${stem} snapshot=${snapshot} restore=${restore}`;
  try {
    const res = await rcon.send(cmd);
    console.log(`[${bot.username}] [world-state] ${cmd} -> ${String(res).slice(0, 400)}`);
    return parseJsonResponse(res);
  } catch (err) {
    console.warn(`[${bot.username}] [world-state] end failed: ${err.message}`);
    return null;
  }
}

/**
 * Query the begin/end summary of an episode (used by the non-leader bot).
 * @param {*} rcon
 * @param {Object} args
 * @param {number} episodeNum
 * @returns {Promise<Object|null>}
 */
async function worldStateInfo(rcon, args, episodeNum) {
  if (!Number(args.world_state_logging)) return null;
  try {
    const res = await rcon.send(`worldstate info episode=${episodeNum}`);
    const info = parseJsonResponse(res);
    return info && info.ok ? info : null;
  } catch (err) {
    console.warn(`[world-state] info failed: ${err.message}`);
    return null;
  }
}

/**
 * Append a free-text annotation (stored as a string; JSON is recommended)
 * to the active world log. Keep it short and free of spaces-sensitive syntax.
 * @param {*} rcon
 * @param {Object} args
 * @param {Object|string} note
 * @returns {Promise<void>}
 */
async function worldStateNote(rcon, args, note) {
  if (!Number(args.world_state_logging)) return;
  const text = typeof note === "string" ? note : JSON.stringify(note);
  try {
    await rcon.send(`worldstate note ${text.slice(0, 900)}`);
  } catch (err) {
    console.warn(`[world-state] note failed: ${err.message}`);
  }
}

module.exports = {
  buildWorldStem,
  formatDateForFilename,
  isWorldStateLeader,
  parseJsonResponse,
  parseWorldGenInfo,
  queryWorldSeed,
  worldStateBegin,
  worldStateEnd,
  worldStateInfo,
  worldStateNote,
};
