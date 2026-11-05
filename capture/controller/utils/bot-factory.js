// Modified by Ziyang Ye for MineEngine, 2026 (from SolarisEngine 78f23e7); see capture/MODIFICATIONS.md
const util = require("util");

const mineflayer = require("mineflayer");
const {
  pathfinder,
  Movements,
  goals: { GoalNear, GoalNearXZ, GoalXZ, GoalBlock, GoalFollow },
} = require("mineflayer-pathfinder");

const pvp = require("mineflayer-pvp").plugin;
const toolPlugin = require("mineflayer-tool").plugin;

// Log mineflayer version once to help debug protocol mismatches
const MINEFLAYER_VERSION =
  (require("mineflayer/package.json") || {}).version || "unknown";
console.log(`[bot-factory] mineflayer version: ${MINEFLAYER_VERSION}`);

/**
 * Guard bot.look against the fork's look-easing hang.
 *
 * mineflayer (georgysavva fork) finishes a look when a sent packet's yaw
 * equals bot.entity.yaw. If the server corrects the bot's rotation while an
 * eased look is running (teleport, movement correction), the easing keeps
 * driving lastSentYaw to the old target, the two never match again and the
 * look promise never resolves ("Look with easing still has not completed"),
 * which hangs the episode. Each look gets a deadline from its angle and turn
 * speed; when it passes, a forced look to the current entity rotation
 * finishes the pending task.
 *
 * @param {*} bot - Mineflayer bot (after createBot, physics plugin loaded)
 * @param {{graceMs?: number}} [options]
 */
function installLookWatchdog(bot, { graceMs = 1500 } = {}) {
  if (typeof bot.look !== "function") return;
  const originalLook = bot.look.bind(bot);
  const angle = (a, b) => {
    let d = (a - b) % (2 * Math.PI);
    if (d > Math.PI) d -= 2 * Math.PI;
    if (d < -Math.PI) d += 2 * Math.PI;
    return Math.abs(d);
  };
  bot.look = async (yaw, pitch, force, yawSpeedDPS, pitchSpeedDPS, useEasing = false) => {
    if (force) return originalLook(yaw, pitch, force, yawSpeedDPS, pitchSpeedDPS, useEasing);
    const yawSpeed = yawSpeedDPS != null ? (yawSpeedDPS * Math.PI) / 180 : bot.physics?.yawSpeed || 3;
    const pitchSpeed = pitchSpeedDPS != null ? (pitchSpeedDPS * Math.PI) / 180 : bot.physics?.pitchSpeed || 3;
    // Easing averages 80% of the turn speed; allow 50% slack plus a fixed grace.
    const turnSeconds = Math.max(
      angle(yaw, bot.entity.yaw) / (yawSpeed * 0.8),
      Math.abs(pitch - bot.entity.pitch) / (pitchSpeed * 0.8),
    );
    const deadlineMs = turnSeconds * 1500 + graceMs;
    let timer = null;
    const pending = originalLook(yaw, pitch, force, yawSpeedDPS, pitchSpeedDPS, useEasing);
    const timedOut = await Promise.race([
      pending.then(() => false),
      new Promise((resolve) => {
        timer = setTimeout(() => resolve(true), deadlineMs);
      }),
    ]);
    clearTimeout(timer);
    if (timedOut) {
      console.warn(
        `[${bot.username}] look did not finish within ${Math.round(deadlineMs)} ms; forcing it to the current rotation`,
      );
      await originalLook(bot.entity.yaw, bot.entity.pitch, true);
    }
  };
}

/**
 * Create a new Mineflayer bot instance
 * @param {Object} config - Bot configuration
 * @param {string} config.username - Bot username
 * @param {string} config.host - Server host
 * @param {number} config.port - Server port
 * @param {string} config.version - Minecraft version (defaults to 1.21)
 * @returns {Bot} Mineflayer bot instance
 */
function makeBot({ username, host, port, version = "1.21" }) {
  const bot = mineflayer.createBot({
    host,
    port,
    username,
    version,
    checkTimeoutInterval: 10 * 60 * 1000,
  });

  // Load pathfinder plugin
  bot.loadPlugin(pathfinder);

  bot.loadPlugin(pvp);

  // Load tool plugin for automatic tool selection
  bot.loadPlugin(toolPlugin);

  installLookWatchdog(bot);

  bot.on("end", () => console.log(`[${bot.username}] disconnected.`));
  bot.on("kicked", (reason) =>
    console.log(
      `[${bot.username}] kicked:`,
      util.inspect(reason, { depth: null }),
    ),
  );
  bot.on("error", (err) => console.log(`[${bot.username}] error:`, err));

  bot.on("spawn", () => {
    console.log(
      `[${bot.username}] 🎮 Spawned in world at (${bot.entity.position.x.toFixed(1)}, ${bot.entity.position.y.toFixed(1)}, ${bot.entity.position.z.toFixed(1)})`,
    );
  });

  bot.on("health", () => {
    if (bot.health <= 5 && bot.health > 0) {
      console.log(`[${bot.username}] ⚠️ Low health: ${bot.health}/20`);
    }
  });

  bot.on("death", () => {
    console.log(
      `[${bot.username}] 💀 Died at (${bot.entity.position.x.toFixed(1)}, ${bot.entity.position.y.toFixed(1)}, ${bot.entity.position.z.toFixed(1)})`,
    );
  });

  // Log when bot respawns
  bot.on("respawn", () => {
    console.log(`[${bot.username}] ♻️ Respawned`);
  });

  return bot;
}

module.exports = {
  makeBot,
  Movements,
  GoalNear,
  GoalNearXZ,
  GoalXZ,
  GoalBlock,
  GoalFollow,
};
