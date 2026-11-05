// Modified by Ziyang Ye for MineEngine, 2026 (from SolarisEngine 78f23e7); see capture/MODIFICATIONS.md
// build-house-episode.js - Collaborative 5x5 house building episode
const { Vec3 } = require("vec3");

const {
  sleep,
  initializePathfinder,
  stopPathfinder,
} = require("../primitives/movement");
const {
  makeHouseBlueprint5x5,
  rotateLocalToWorld,
  splitWorkByXAxis,
  buildPhase,
  admireHouse,
  calculateMaterialCounts,
} = require("../primitives/building");
const { BaseEpisode } = require("./base-episode");
const { ensureBotHasEnough } = require("../primitives/items");
const { paletteMetadata, pickPalette } = require("../utils/materials");
const { buildPhaseWithGlances, watchPartnerUntil } = require("./partner-gaze");
const { exchange } = require("../utils/handshake");

// R2 fixer (03 section 5 / T8): the bots build their halves in chunks of
// GLANCE_EVERY_BLOCKS blocks and glance at the partner between chunks; the
// bot that finishes a phase first watches the other until it is done too
// (barrier per phase). The upstream version had the partner in view in only
// 13-26 % of the frames; with glances every 3 blocks for 1.0-1.6 s it was
// 26-35 % but the episode grew from ~125 s to 165-185 s, so the glances are
// kept short (every 4 blocks, 0.8-1.2 s) and buildHouse is down-weighted
// (episode-weights.js DEFAULT_TYPE_MULTIPLIERS). BUILD_HOUSE_GAZE=0 restores
// the upstream behaviour.
const GLANCE_EVERY_BLOCKS = 4;
const GLANCE_MS = [800, 1200];
const PHASE_BARRIER_TIMEOUT_MS = 40000;



// Constants for house building behavior
const INITIAL_EYE_CONTACT_MS = 1500; // Initial look duration
const FINAL_EYE_CONTACT_MS = 2000; // Final admiration duration
const BLOCK_PLACE_DELAY_MS = 200; // Delay between placing blocks
const ORIENTATION = 0; // Only 0° supported for now (south-facing door)

// T7: the material set is drawn per episode from the materials.js palette
// (floor / walls / roof; doors and windows are not placed by this episode).

/**
 * Get the setup phase function for house building episodes
 * @param {*} bot - Mineflayer bot instance
 * @param {Object} rcon - RCON connection
 * @param {Function} sharedBotRng - Shared random number generator
 * @param {*} coordinator - Bot coordinator instance
 * @param {number} iterationID - Iteration ID
 * @param {number} episodeNum - Episode number
 * @param {Object} episodeInstance - Episode instance
 * @param {Object} args - Configuration arguments
 * @returns {Function} Setup phase function
 */
function getOnBuildHouseSetupPhaseFn(
  bot,
  rcon,
  sharedBotRng,
  coordinator,
  iterationID,
  episodeNum,
  episodeInstance,
  args,
) {
  return async (phaseDataOther) => {
    coordinator.sendToOtherBot(
      `buildHouseSetupPhase_${iterationID}`,
      { position: bot.entity.position.clone() },
      episodeNum,
      `buildHouseSetupPhase_${iterationID} beginning`,
    );

    // Capture our position for the build phase
    const buildPhaseDataOur = {
      position: bot.entity.position.clone(),
    };

    // Transition to build phase
    coordinator.onceEvent(
      `buildHousePhase_${iterationID}`,
      episodeNum,
      getOnBuildHousePhaseFn(
        bot,
        rcon,
        sharedBotRng,
        coordinator,
        iterationID,
        episodeNum,
        episodeInstance,
        args,
        buildPhaseDataOur,
      ),
    );
    coordinator.sendToOtherBot(
      `buildHousePhase_${iterationID}`,
      buildPhaseDataOur,
      episodeNum,
      `buildHouseSetupPhase_${iterationID} end`,
    );
  };
}

/**
 * Get the build phase function for house building episodes
 * @param {*} bot - Mineflayer bot instance
 * @param {Object} rcon - RCON connection
 * @param {Function} sharedBotRng - Shared random number generator
 * @param {*} coordinator - Bot coordinator instance
 * @param {number} iterationID - Iteration ID
 * @param {number} episodeNum - Episode number
 * @param {Object} episodeInstance - Episode instance
 * @param {Object} args - Configuration arguments
 * @param {Object} phaseDataOur - Phase data with captured position
 * @returns {Function} Phase function
 */
function getOnBuildHousePhaseFn(
  bot,
  rcon,
  sharedBotRng,
  coordinator,
  iterationID,
  episodeNum,
  episodeInstance,
  args,
  phaseDataOur,
) {
  return async function onBuildHousePhase(phaseDataOther) {
    coordinator.sendToOtherBot(
      `buildHousePhase_${iterationID}`,
      phaseDataOur,
      episodeNum,
      `buildHousePhase_${iterationID} beginning`,
    );

    console.log(
      `[${bot.username}] 🏠 Starting BUILD HOUSE phase ${iterationID}`,
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

    // STEP 3: Determine world origin for house (midpoint between bots)
    console.log(`[${bot.username}] 📐 STEP 3: Planning house location...`);

    // Reconstruct Vec3 from received position data (positions are serialized when sent between bots)
    const botPos = new Vec3(
      phaseDataOur.position.x,
      phaseDataOur.position.y,
      phaseDataOur.position.z,
    ).floored();
    const otherBotPos = new Vec3(
      phaseDataOther.position.x,
      phaseDataOther.position.y,
      phaseDataOther.position.z,
    ).floored();

    // Use the higher Y level between bots to ensure consistent elevation
    const maxBotY = Math.max(botPos.y, otherBotPos.y);
    const worldOrigin = new Vec3(
      Math.floor((botPos.x + otherBotPos.x) / 2),
      Math.floor(maxBotY), // Use higher Y level for consistency
      Math.floor((botPos.z + otherBotPos.z) / 2),
    );

    console.log(
      `[${bot.username}] 🏗️ House origin: (${worldOrigin.x}, ${worldOrigin.y}, ${worldOrigin.z})`,
    );

    // STEP 4: Generate blueprint and convert to world coordinates
    console.log(`[${bot.username}] 📋 STEP 4: Generating blueprint...`);
    const blueprint = makeHouseBlueprint5x5({
      materials: episodeInstance.materials,
    });

    // Convert all local coords to world coords
    const worldTargets = blueprint.map((target) => ({
      ...target,
      worldPos: rotateLocalToWorld(target, worldOrigin, ORIENTATION),
    }));

    console.log(`[${bot.username}]    Total blocks: ${worldTargets.length}`);

    // STEP 5: Initialize pathfinder for building
    console.log(`[${bot.username}] 🚶 STEP 5: Initializing pathfinder...`);
    initializePathfinder(bot, {
      allowSprinting: false,
      allowParkour: true,
      canDig: true,
      allowEntityDetection: true,
    });

    // STEP 6: Build in phases (floor → walls → windows → roof)
    console.log(`[${bot.username}] 🏗️ STEP 6: Building house in phases...`);
    const phases = ["floor", "walls", "roof"]; // windows are not placed for better performance
    let phaseAborted = false;

    try {
      for (const phaseName of phases) {
        if (bot._episodeStopping) {
          phaseAborted = true;
          console.log(
            `[${bot.username}] 🛑 Stop requested before ${phaseName} phase, aborting build loop`,
          );
          break;
        }

        console.log(
          `[${bot.username}] ═══════════════════════════════════════`,
        );
        console.log(
          `[${bot.username}] 🔨 Building phase: ${phaseName.toUpperCase()}`,
        );
        console.log(
          `[${bot.username}] 🕐 Phase start time: ${new Date().toISOString()}`,
        );

        // Get all targets for this phase
        const phaseTargets = worldTargets.filter((t) => t.phase === phaseName);

        // Split work between bots using X-axis split
        const { alphaTargets, bravoTargets } = splitWorkByXAxis(
          phaseTargets,
          args.bot_name,
          args.other_bot_name,
        );

        // Determine which half based on proximity to house origin
        const botIsOnWestSide = botPos.x < worldOrigin.x;
        const otherBotIsOnWestSide = otherBotPos.x < worldOrigin.x;

        // If conflict (both bots on same side), use bot identity as tie-breaker
        let myTargets;
        if (botIsOnWestSide === otherBotIsOnWestSide) {
          // Tie! Use alphabetical order: Alpha gets west, Bravo gets east
          const isAlphaBot = bot.username < args.other_bot_name;
          myTargets = isAlphaBot ? alphaTargets : bravoTargets;
          console.log(
            `[${bot.username}] ⚠️ Both bots on ${botIsOnWestSide ? "WEST" : "EAST"} side - using tie-breaker (${isAlphaBot ? "WEST" : "EAST"} half)`,
          );
        } else {
          // No conflict - use proximity-based assignment
          myTargets = botIsOnWestSide ? alphaTargets : bravoTargets;
          console.log(
            `[${bot.username}] ✅ Using proximity-based assignment (${botIsOnWestSide ? "WEST" : "EAST"} half)`,
          );
        }

        console.log(
          `[${bot.username}] 📍 Spawn position: (${botPos.x}, ${botPos.z}), House origin: (${worldOrigin.x}, ${worldOrigin.z})`,
        );
        console.log(
          `[${bot.username}] 🏗️ Assigned ${myTargets.length}/${phaseTargets.length} blocks`,
        );

        // Build this bot's assigned blocks
        if (myTargets.length > 0) {
          console.log(
            `[${bot.username}] 🏗️ Starting to build my ${myTargets.length} blocks...`,
          );
          const phaseOptions = {
            args: args,
            delayMs: BLOCK_PLACE_DELAY_MS,
            shouldAbort: () => bot._episodeStopping,
          };
          const result =
            process.env.BUILD_HOUSE_GAZE === "0"
              ? await buildPhase(bot, myTargets, phaseOptions)
              : await buildPhaseWithGlances(
                  bot,
                  myTargets,
                  args.other_bot_name,
                  phaseOptions,
                  { every: GLANCE_EVERY_BLOCKS, glanceMs: GLANCE_MS },
                );
          console.log(`[${bot.username}] ✅ Finished building my blocks`);

          if (result.aborted) {
            phaseAborted = true;
            console.log(
              `[${bot.username}] 🛑 ${phaseName} phase aborted due to stop request`,
            );
            break;
          }

          // Check for catastrophic failure (more than 50% failed)
          if (result.failed > myTargets.length * 0.5) {
            console.log(
              `[${bot.username}] ❌ Phase ${phaseName} failed significantly: ${result.failed}/${myTargets.length} blocks failed`,
            );
            throw new Error(
              `Phase ${phaseName} failed: ${result.failed}/${myTargets.length} blocks failed`,
            );
          }
        } else {
          console.log(
            `[${bot.username}] ⏭️ No blocks assigned to me in this phase, skipping...`,
          );
        }

        // Wait for other bot to finish this phase
        console.log(
          `[${bot.username}]    Waiting for ${args.other_bot_name}...`,
        );
        if (process.env.BUILD_HOUSE_GAZE === "0") {
          await sleep(1000); // Give other bot time to catch up
        } else {
          // barrier: watch the partner until it has finished this phase too
          await watchPartnerUntil(
            bot,
            args.other_bot_name,
            exchange(
              coordinator,
              episodeNum,
              `buildHouse_${iterationID}_${phaseName}_done`,
              {},
              PHASE_BARRIER_TIMEOUT_MS,
            ),
            PHASE_BARRIER_TIMEOUT_MS + 1000,
          );
        }
        console.log(
          `[${bot.username}] ✅ Phase ${phaseName} complete at ${new Date().toISOString()}`,
        );
      }

      if (!phaseAborted) {
        console.log(`[${bot.username}] ✅ All phases complete!`);
      }
    } catch (buildError) {
      console.error(
        `[${bot.username}] ❌ Building failed: ${buildError.message}`,
      );

      // Stop pathfinder immediately using setGoal(null)
      if (bot.pathfinder) {
        bot.pathfinder.setGoal(null);
      }

      // Re-throw the error so episode system handles it properly
      throw buildError;
    }

    // STEP 7: Stop pathfinder
    stopPathfinder(bot);

    if (bot._episodeStopping) {
      console.log(
        `[${bot.username}] 🛑 Stop phase already in progress, skipping admiration/stop transition`,
      );
      return;
    }

    // STEP 8: Exit through door and admire the house
    console.log(`[${bot.username}] 🚪 STEP 8: Exiting and admiring house...`);

    // Re-initialize pathfinder for admiration movement
    initializePathfinder(bot, {
      allowSprinting: false,
      allowParkour: true,
      canDig: true,
      canPlaceOn: true, // Disable scaffolding - let bots jump down from roof
      allowEntityDetection: true,
    });

    const doorWorldPos = rotateLocalToWorld(
      { x: 2, y: 1, z: 0 },
      worldOrigin,
      ORIENTATION,
    );

    // STEP 9: Exit house and admire from 15 blocks away
    await admireHouse(bot, doorWorldPos, ORIENTATION, { backOff: 15 });

    stopPathfinder(bot);

    console.log(`[${bot.username}] ✅ BUILD HOUSE phase complete!`);
    // Transition to stop phase
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
      phaseDataOur,
      episodeNum,
      `buildHousePhase_${iterationID} end`,
    );
  };
}

/**
 * Episode for collaborative 5x5 house building. Both bots coordinate to build a shared house
 * at the midpoint between them, splitting work by X-axis.
 * @extends BaseEpisode
 */
class BuildHouseEpisode extends BaseEpisode {
  static INIT_MIN_BOTS_DISTANCE = 10;
  static INIT_MAX_BOTS_DISTANCE = 20;
  static WORKS_IN_NON_FLAT_WORLD = true; // Auto-scaffolding enabled

  constructor(sharedBotRng) {
    super();
    // Pick the material set for this episode (shared RNG, same on both bots)
    this.palette = pickPalette(sharedBotRng);
    this.materials = {
      floor: this.palette.floor,
      walls: this.palette.walls,
      door: this.palette.door,
      windows: this.palette.window,
      roof: this.palette.roof,
    };
    this._stateMetadata = {
      materials: paletteMetadata(this.palette, ["floor", "walls", "roof"]),
      material_source: "materials.js palette",
      gaze_layer:
        process.env.BUILD_HOUSE_GAZE === "0"
          ? null
          : { glance_every_blocks: GLANCE_EVERY_BLOCKS, glance_ms: GLANCE_MS, phase_barrier: true },
    };
    console.log(`[BuildHouseEpisode] Selected materials:`, this.materials);
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
    console.log(`[${bot.username}] 🏠 Setting up house building episode...`);

    // Generate blueprint to calculate material needs
    const blueprint = makeHouseBlueprint5x5({
      materials: this.materials,
    });

    // Calculate material counts
    const materialCounts = calculateMaterialCounts(blueprint);

    // Add extra blocks for scaffolding (100% more = 2x total)
    // Pathfinder now uses correct block types for scaffolding, so we need more materials
    const safetyMaterials = {};
    for (const [block, count] of Object.entries(materialCounts)) {
      safetyMaterials[block] = Math.ceil(count * 2.0); // 2x for scaffolding consumption
    }

    console.log(
      `[${bot.username}] 📦 Giving building materials (2x for scaffolding):`,
      safetyMaterials,
    );

    // placeScaffold() in primitives/building.js uses cobblestone
    if (!safetyMaterials.cobblestone) safetyMaterials.cobblestone = 16;

    // Use ensureBotHasEnough for each material (matches working episodes)
    for (const [blockType, count] of Object.entries(safetyMaterials)) {
      await ensureBotHasEnough(bot, rcon, blockType, count);
    }

    // Return unchanged positions since we don't move bots during setup
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
    const phaseDataOur = {
      position: bot.entity.position.clone(),
    };

    coordinator.onceEvent(
      `buildHouseSetupPhase_${iterationID}`,
      episodeNum,
      getOnBuildHouseSetupPhaseFn(
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
      `buildHouseSetupPhase_${iterationID}`,
      phaseDataOur,
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
    console.log(`[${bot.username}] 🧹 Cleaning up house building episode...`);
    // Clean up pathfinder if still active
    if (bot.pathfinder) {
      stopPathfinder(bot);
    }
  }
}

module.exports = {
  BuildHouseEpisode,
};
