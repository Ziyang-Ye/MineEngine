// Modified by Ziyang Ye for MineEngine, 2026 (from SolarisEngine 78f23e7); see capture/MODIFICATIONS.md
// build-tower-episode.js - Individual tower building episode
const { ensureBotHasEnough, unequipHand } = require("../primitives/items");

const { buildTowerUnderneath } = require("../primitives/building");
const { sleep } = require("../primitives/movement");
const { BaseEpisode } = require("./base-episode");
const { pickPalette } = require("../utils/materials");
const { takeTurns } = require("./partner-gaze");

// Constants for tower building behavior
const INITIAL_EYE_CONTACT_MS = 1500; // Initial look duration
const FINAL_EYE_CONTACT_MS = 1500; // Final look duration
const MIN_TOWER_HEIGHT = 8; // Minimum tower height
const MAX_TOWER_HEIGHT = 12; // Maximum tower height
// T7: tower material per episode / per bot from the materials.js palette
// (bot whose name sorts first: palette.walls, the other bot: palette.accent)
const JUMP_DURATION_MS = 50; // How long to hold jump
const PLACE_RETRY_DELAY_MS = 20; // Delay between place attempts
const MAX_PLACE_ATTEMPTS = 10; // Max attempts to place a block
const SETTLE_DELAY_MS = 200; // Delay to settle after placing

/**
 * Get the phase function for tower building episodes
 * @param {*} bot - Mineflayer bot instance
 * @param {Object} rcon - RCON connection
 * @param {Function} sharedBotRng - Shared random number generator
 * @param {*} coordinator - Bot coordinator instance
 * @param {number} iterationID - Iteration ID
 * @param {number} episodeNum - Episode number
 * @param {Object} episodeInstance - Episode instance
 * @param {Object} args - Configuration arguments
 * @returns {Function} Phase function
 */
function getOnBuildTowerPhaseFn(
  bot,
  rcon,
  sharedBotRng,
  coordinator,
  iterationID,
  episodeNum,
  episodeInstance,
  args,
) {
  return async function onBuildTowerPhase(otherBotPosition) {
    coordinator.sendToOtherBot(
      `buildTowerPhase_${iterationID}`,
      bot.entity.position.clone(),
      episodeNum,
      `buildTowerPhase_${iterationID} beginning`,
    );

    console.log(
      `[${bot.username}] 🚀 Starting BUILD TOWER phase ${iterationID}`,
    );

    // STEP 1: Bots spawn (already done by teleport phase)
    console.log(`[${bot.username}] ✅ STEP 1: Bot spawned`);
    // STEP 2: Initial eye contact
    console.log(
      `[${bot.username}] 👀 STEP 2: Making eye contact with ${args.other_bot_name}...`,
    );
    try {
      const otherEntity = bot.players[args.other_bot_name]?.entity;
      if (otherEntity) {
        const targetPos = otherEntity.position.offset(0, otherEntity.height, 0);
        await bot.lookAt(targetPos, false);
        await sleep(INITIAL_EYE_CONTACT_MS);
      }
    } catch (lookError) {
      console.log(
        `[${bot.username}] ⚠️ Could not look at other bot: ${lookError.message}`,
      );
    }

    // STEP 3: Prepare to place blocks
    console.log(`[${bot.username}] 📐 STEP 3: Preparing to build tower...`);

    // STEP 4: Determine tower height and position
    const towerHeight =
      MIN_TOWER_HEIGHT +
      Math.floor(sharedBotRng() * (MAX_TOWER_HEIGHT - MIN_TOWER_HEIGHT + 1));
    console.log(`[${bot.username}] 📏 Tower height: ${towerHeight} blocks`);

    // STEP 5: Build the tower
    const towerBlockType = episodeInstance.towerMaterialFor(bot.username, args);
    console.log(
      `[${bot.username}] 🗼 STEP 5: Building ${towerHeight}-block tower with ${towerBlockType}...`,
    );
    const buildTower = () =>
      buildTowerUnderneath(bot, towerHeight, args, {
        blockType: towerBlockType,
        breakOnFailure: true, // build-tower-episode breaks immediately on failure
        enableRetry: false, // simpler version without retry logic
      });
    // R2 fixer (03 section 5 / T8): turn-based by default - one bot builds
    // while the other watches it, then they swap (partner in view; the
    // simultaneous variant had the partner in only ~1/3 of the frames).
    // BUILD_TOWER_MODE=simultaneous restores the upstream behaviour.
    // One shared draw (both bots) decides who builds first.
    const uFirst = sharedBotRng();
    const turnBased = process.env.BUILD_TOWER_MODE !== "simultaneous";
    const names = [bot.username, args.other_bot_name].sort();
    const firstBuilder = names[uFirst < 0.5 ? 0 : 1];
    episodeInstance._stateMetadata = {
      ...(episodeInstance._stateMetadata || {}),
      tower_height: towerHeight,
      build_mode: turnBased ? "turns" : "simultaneous",
      first_builder: turnBased ? firstBuilder : null,
      observer_distance: Number(
        (bot.players[args.other_bot_name]?.entity?.position?.distanceTo(bot.entity.position) || 0).toFixed(1),
      ) || null,
    };
    const buildResult = turnBased
      ? await takeTurns({
          bot,
          coordinator,
          episodeNum,
          otherName: args.other_bot_name,
          name: `buildTower_${iterationID}`,
          iAmFirst: bot.username === firstBuilder,
          work: buildTower,
          turnTimeoutMs: 30000,
        })
      : await buildTower();

    // STEP 6: Final eye contact
    console.log(`[${bot.username}] 👀 STEP 6: Final eye contact...`);
    try {
      const otherEntity = bot.players[args.other_bot_name]?.entity;
      if (otherEntity) {
        const targetPos = otherEntity.position.offset(0, otherEntity.height, 0);
        await bot.lookAt(targetPos, false);
        await sleep(FINAL_EYE_CONTACT_MS);
      }
    } catch (lookError) {
      console.log(
        `[${bot.username}] ⚠️ Could not look at other bot: ${lookError.message}`,
      );
    }

    console.log(`[${bot.username}] ✅ BUILD TOWER phase complete!`);
    console.log(
      `[${bot.username}] 📊 Final stats: ${buildResult.success}/${towerHeight} blocks placed`,
    );

    // STEP 7: Transition to stop phase (end episode)
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
      `buildTowerPhase_${iterationID} end`,
    );

    return buildResult;
  };
}

/**
 * Episode for individual tower building. Each bot builds a tower of random height (8–12 blocks)
 * at their spawn position, with initial and final eye contact.
 * @extends BaseEpisode
 */
class BuildTowerEpisode extends BaseEpisode {
  static INIT_MIN_BOTS_DISTANCE = 8;
  static INIT_MAX_BOTS_DISTANCE = 15;
  static WORKS_IN_NON_FLAT_WORLD = true;

  constructor(sharedBotRng) {
    super(sharedBotRng);
    // T7: shared palette (same on both bots; fixed number of RNG draws)
    this.palette = pickPalette(sharedBotRng);
  }

  /** Tower material of a bot (first name: walls, other: accent). */
  towerMaterialFor(botName, args) {
    const first = [args.bot_name, args.other_bot_name].sort()[0];
    return botName === first ? this.palette.walls : this.palette.accent;
  }

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
    // Give tower building blocks via RCON
    const towerBlockType = this.towerMaterialFor(bot.username, args);
    await ensureBotHasEnough(bot, rcon, towerBlockType, 64);
    this._stateMetadata = {
      ...(this._stateMetadata || {}),
      materials: {
        [bot.username]: towerBlockType,
        [args.other_bot_name]: this.towerMaterialFor(args.other_bot_name, args),
      },
      material_source: "materials.js palette (walls / accent)",
    };
    await unequipHand(bot);
    return {
      botPositionNew: botPosition,
      otherBotPositionNew: otherBotPosition,
    };
  }

  async entryPoint(
    bot,
    rcon,
    sharedBotRng,
    coordinator,
    iterationID,
    episodeNum,
    args,
  ) {
    coordinator.onceEvent(
      `buildTowerPhase_${iterationID}`,
      episodeNum,
      getOnBuildTowerPhaseFn(
        bot,
        rcon,
        sharedBotRng,
        coordinator,
        iterationID,
        episodeNum,
        this,
        args,
      ),
    );
    coordinator.sendToOtherBot(
      `buildTowerPhase_${iterationID}`,
      bot.entity.position.clone(),
      episodeNum,
      "entryPoint end",
    );
  }

  async tearDownEpisode(
    bot,
    rcon,
    sharedBotRng,
    coordinator,
    episodeNum,
    args,
  ) {
    // Clean up any remaining blocks from inventory
  }
}

module.exports = { BuildTowerEpisode };
