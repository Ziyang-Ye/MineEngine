// Modified by Ziyang Ye for MineEngine, 2026 (from SolarisEngine 78f23e7); see capture/MODIFICATIONS.md
const fs = require("fs/promises");
const path = require("path");

const { Rcon } = require("rcon-client");
const seedrandom = require("seedrandom");

const { sleep } = require("./utils/helpers");
// Per-frame recorder; loads prismarine-viewer-colalab only when rendering.
const { startFrameRecorder } = require("./recorder/frame-recorder");
const {
  addEnvMetadata,
  applyDamagePolicy,
  applyEpisodeEnvironment,
  chooseEpisodeEnvironment,
  ensureBotPersistentState,
  isDamageEpisode,
  randomTickSpeedFor,
  resolvePrecipitation,
  setFallDamageMultiplierZero,
  weatherObservation,
} = require("./utils/env-setup");
// Round 2 (world diversity, skins): biome per episode on normal worlds,
// optional levelled pad, per-episode character skins.
const {
  biomeSiteMetadata,
  chooseEpisodeSite,
  resolveBiome,
  teleportToSite,
} = require("./utils/biomes");
const { prepareSite, sitePrepMetadata } = require("./utils/site-prep");
const { assignEpisodeSkins, noteEpisodeSkins } = require("./utils/skins");
const { SUMMON_TAG } = require("./utils/creatures");
const { planAmbientFauna, summonAmbientFauna } = require("./utils/ambient-fauna");
const { applyFlatBiome, planFlatBiome } = require("./utils/flat-biome");
const { groundAt } = require("./episode-handlers/state/state-common");
const { waitForCameras } = require("./utils/camera-ready");
const { DEFAULT_CAMERA_SPEED_DEGREES_PER_SEC } = require("./utils/constants");
const {
  directTeleport,
  lookAtSmooth,
  stopAll,
} = require("./primitives/movement");
const { ensureBotHasEnough, unequipHand } = require("./primitives/items");
const {
  CATEGORY_ALIASES,
  EPISODE_CATEGORIES,
  categoryOf,
  categoryPlan,
  parseCategoryWeights,
  parseWeightMultipliers,
  selectCategoryEpisodeType,
  selectWeightedEpisodeType,
} = require("./utils/episode-weights");
const {
  isWorldStateLeader,
  parseWorldGenInfo,
  queryWorldSeed,
  worldStateBegin,
  worldStateEnd,
  worldStateInfo,
} = require("./utils/world-state");

// Import episode classes
const {
  StraightLineEpisode,
} = require("./episode-handlers/straight-line-episode");
const { ChaseEpisode } = require("./episode-handlers/chase-episode");
const { OrbitEpisode } = require("./episode-handlers/orbit-episode");
const { WalkLookEpisode } = require("./episode-handlers/walk-look-episode");
const {
  WalkLookAwayEpisode,
} = require("./episode-handlers/walk-look-away-episode");
const { PvpEpisode } = require("./episode-handlers/pvp-episode");
const {
  BuildStructureEpisode,
} = require("./episode-handlers/build-structure-episode");
const { BuildTowerEpisode } = require("./episode-handlers/build-tower-episode");
const { MineEpisode } = require("./episode-handlers/mine-episode");
const { PveEpisode } = require("./episode-handlers/pve-episode");
const {
  TowerBridgeEpisode,
} = require("./episode-handlers/tower-bridge-episode");
const { BuildHouseEpisode } = require("./episode-handlers/build-house-episode");
const { CollectorEpisode } = require("./episode-handlers/collector-episode");
const {
  PlaceAndMineEpisode,
} = require("./episode-handlers/place-and-mine-episode");
const {
  StructureEvalEpisode,
} = require("./episode-handlers/eval/structure-eval-episode");
const {
  TranslationEvalEpisode,
} = require("./episode-handlers/eval/translation-eval-episode");
const {
  BothLookAwayEvalEpisode,
} = require("./episode-handlers/eval/both-look-away-eval-episode");
const {
  OneLooksAwayEvalEpisode,
} = require("./episode-handlers/eval/one-looks-away-eval-episode");
const {
  RotationEvalEpisode,
} = require("./episode-handlers/eval/rotation-eval-episode");
const {
  TurnToLookEvalEpisode,
} = require("./episode-handlers/eval/turn-to-look-eval-episode");
const {
  TurnToLookOppositeEvalEpisode,
} = require("./episode-handlers/eval/turn-to-look-opposite-eval-episode");
const turnToLookEvalTpPoints = require("./episode-handlers/eval/turn-to-look-eval-episode-tp-points.json");
// Stateful-capture episodes (world-state interactions; see STATEFUL_CAPTURE.md)
const {
  IgniteAndExtinguishEpisode,
} = require("./episode-handlers/state/ignite-and-extinguish-episode");
const {
  IceNearFireEpisode,
} = require("./episode-handlers/state/ice-near-fire-episode");
const { TntBlastEpisode } = require("./episode-handlers/state/tnt-blast-episode");
const { LavaWaterEpisode } = require("./episode-handlers/state/lava-water-episode");
const {
  BuildAndBreakEpisode,
} = require("./episode-handlers/state/build-and-break-episode");
// Round 2 (diverse/): build / destroy
const { CoBuildEpisode } = require("./episode-handlers/diverse/co-build-episode");
const { BuildVsBreakEpisode } = require("./episode-handlers/diverse/build-vs-break-episode");
const { DigDownEpisode } = require("./episode-handlers/diverse/dig-down-episode");
const { TunnelEpisode } = require("./episode-handlers/diverse/tunnel-episode");
const { CraterEpisode } = require("./episode-handlers/diverse/crater-episode");
const { TntDemolishEpisode } = require("./episode-handlers/diverse/tnt-demolish-episode");
const { ForestBuildEpisode } = require("./episode-handlers/diverse/forest-build-episode");
// Round 2 (diverse/): mechanics
const { FarmingEpisode } = require("./episode-handlers/diverse/farming-episode");
const { RedstoneDoorsEpisode } = require("./episode-handlers/diverse/redstone-doors-episode");
const { FallingBlocksEpisode } = require("./episode-handlers/diverse/falling-blocks-episode");
const { LightAndFireEpisode } = require("./episode-handlers/diverse/light-and-fire-episode");
const { WaterWorksEpisode } = require("./episode-handlers/diverse/water-works-episode");
const { SnowIceEpisode } = require("./episode-handlers/diverse/snow-ice-episode");
// Round 2 (diverse/): combat / creatures
const { PvpMeleeEpisode } = require("./episode-handlers/diverse/pvp-melee-episode");
const { PvpRangedEpisode } = require("./episode-handlers/diverse/pvp-ranged-episode");
const { PveMobsEpisode } = require("./episode-handlers/diverse/pve-mobs-episode");
const { AnimalCareEpisode } = require("./episode-handlers/diverse/animal-care-episode");
const { RideMountEpisode } = require("./episode-handlers/diverse/ride-mount-episode");
const { ItemExchangeEpisode } = require("./episode-handlers/diverse/item-exchange-episode");

/**
 * Map of episode type string keys to their episode class constructors.
 * Used to instantiate episodes by type name (e.g. from config or env).
 * @type {Object<string, typeof import('./episode-handlers/base-episode').BaseEpisode>}
 */
const episodeClassMap = {
  straightLineWalk: StraightLineEpisode,
  chase: ChaseEpisode,
  orbit: OrbitEpisode,
  walkLook: WalkLookEpisode,
  walkLookAway: WalkLookAwayEpisode,
  pvp: PvpEpisode,
  pve: PveEpisode,
  buildStructure: BuildStructureEpisode,
  buildTower: BuildTowerEpisode,
  mine: MineEpisode,
  towerBridge: TowerBridgeEpisode,
  buildHouse: BuildHouseEpisode,
  collector: CollectorEpisode,
  placeAndMine: PlaceAndMineEpisode,
  // Stateful-capture episodes:
  igniteAndExtinguish: IgniteAndExtinguishEpisode,
  iceNearFire: IceNearFireEpisode,
  tntBlast: TntBlastEpisode,
  lavaWater: LavaWaterEpisode,
  buildAndBreak: BuildAndBreakEpisode,
  // Round 2: build / destroy
  coBuild: CoBuildEpisode,
  buildVsBreak: BuildVsBreakEpisode,
  digDown: DigDownEpisode,
  tunnel: TunnelEpisode,
  crater: CraterEpisode,
  tntDemolish: TntDemolishEpisode,
  forestBuild: ForestBuildEpisode,
  // Round 2: mechanics
  farming: FarmingEpisode,
  redstoneDoors: RedstoneDoorsEpisode,
  fallingBlocks: FallingBlocksEpisode,
  lightAndFire: LightAndFireEpisode,
  waterWorks: WaterWorksEpisode,
  snowIce: SnowIceEpisode,
  // Round 2: combat / creatures
  pvpMelee: PvpMeleeEpisode,
  pvpRanged: PvpRangedEpisode,
  pveMobs: PveMobsEpisode,
  animalCare: AnimalCareEpisode,
  rideMount: RideMountEpisode,
  itemExchange: ItemExchangeEpisode,
  // Eval episodes:
  structureEval: StructureEvalEpisode,
  translationEval: TranslationEvalEpisode,
  bothLookAwayEval: BothLookAwayEvalEpisode,
  oneLooksAwayEval: OneLooksAwayEvalEpisode,
  rotationEval: RotationEvalEpisode,
  turnToLookEval: TurnToLookEvalEpisode,
  turnToLookOppositeEval: TurnToLookOppositeEvalEpisode,
};

/**
 * Array of eval episode classes used to detect whether an episode instance
 * is an eval episode (e.g. in {@link isEvalEpisode}).
 * @type {Array<typeof import('./episode-handlers/base-episode').BaseEpisode>}
 */
const evalEpisodeClasses = [
  StructureEvalEpisode,
  TranslationEvalEpisode,
  BothLookAwayEvalEpisode,
  OneLooksAwayEvalEpisode,
  RotationEvalEpisode,
  TurnToLookEvalEpisode,
  TurnToLookOppositeEvalEpisode,
];

/**
 * Check if an episode instance is an eval episode
 * @param {Object} episodeInstance - Episode instance to check
 * @returns {boolean} True if the episode is an eval episode
 */
function isEvalEpisode(episodeInstance) {
  return evalEpisodeClasses.some(
    (EvalClass) => episodeInstance instanceof EvalClass,
  );
}

// Import episode-specific handlers

/**
 * Default list of episode type keys to run when EPISODE_TYPES is not set.
 * Each string must be a key of {@link episodeClassMap}.
 * @type {string[]}
 */
const defaultEpisodeTypes = [
  "straightLineWalk",
  "chase",
  "orbit",
  "walkLook",
  "buildHouse",
  "walkLookAway",
  "pvp",
  "pve",
  "buildStructure",
  "buildTower",
  "mine",
  "towerBridge",
  "collector",
  "placeAndMine",
  "structureEval",
  "translationEval",
  "bothLookAwayEval",
  "oneLooksAwayEval",
  "rotationEval",
  "turnToLookEval",
  "turnToLookOppositeEval",
];

/**
 * Stateful-capture episode types (world-state interactions).
 * @type {string[]}
 */
const statefulEpisodeTypes = [
  "igniteAndExtinguish",
  "iceNearFire",
  "tntBlast",
  "lavaWater",
  "buildAndBreak",
];
defaultEpisodeTypes.push(...statefulEpisodeTypes);

/**
 * Round-2 episode types (episode-handlers/diverse/), grouped by theme.
 * All of them are part of "all" and "diverse".
 */
const buildDestroyEpisodeTypes = [
  "coBuild",
  "buildVsBreak",
  "digDown",
  "tunnel",
  "crater",
  "tntDemolish",
  "forestBuild",
];
const mechanicsEpisodeTypes = [
  "farming",
  "redstoneDoors",
  "fallingBlocks",
  "lightAndFire",
  "waterWorks",
  "snowIce",
];
const combatCreatureEpisodeTypes = [
  "pvpMelee",
  "pvpRanged",
  "pveMobs",
  "animalCare",
  "rideMount",
  "itemExchange",
];
const round2EpisodeTypes = [
  ...buildDestroyEpisodeTypes,
  ...mechanicsEpisodeTypes,
  ...combatCreatureEpisodeTypes,
];
defaultEpisodeTypes.push(...round2EpisodeTypes);

/**
 * Thematic aliases (plain weighted sampling, no category step).
 * @type {Object<string, string[]>}
 */
const thematicAliases = {
  stateful: statefulEpisodeTypes,
  new: round2EpisodeTypes,
  round2: round2EpisodeTypes,
  mechanics: mechanicsEpisodeTypes,
  // episodes whose subject is non-player creatures (hostile, passive,
  // neutral mobs, mounts, villagers / wandering traders)
  creatures: ["pveMobs", "animalCare", "rideMount", "itemExchange"],
  combat: ["pvpMelee", "pvpRanged", "pveMobs"],
  buildDestroy: buildDestroyEpisodeTypes,
};

/**
 * Expand EPISODE_TYPES tokens. Besides concrete type keys it accepts the
 * aliases
 *  - "stateful" (the 5 round-1 world-state episodes), "upstream" (the
 *    original training episode types, without evals), "new" / "round2"
 *    (the 19 round-2 types), "mechanics", "creatures", "combat",
 *    "buildDestroy";
 *  - "diverse": every training type, sampled category first (03 section 4
 *    shares, see utils/episode-weights.js);
 *  - category names "build", "destroy" (or "dig"), "fight", "move" (or
 *    "observe"), "interaction" (or "physics"): the types of that category,
 *    also sampled category first when several categories are given.
 * @param {string} raw
 * @returns {{types: string[], categoryMode: boolean}}
 */
function expandEpisodeTypes(raw) {
  const out = [];
  let categoryMode = false;
  const trainingTypes = defaultEpisodeTypes.filter(
    (t) => !t.toLowerCase().endsWith("eval"),
  );
  for (const token of raw.split(",").map((t) => t.trim()).filter(Boolean)) {
    if (token === "all") {
      // inside a list: every training type (EPISODE_TYPES=all alone keeps
      // the old default list, whose evals are skipped when sampling)
      out.push(...trainingTypes);
    } else if (token === "upstream") {
      out.push(
        ...trainingTypes.filter(
          (t) =>
            !statefulEpisodeTypes.includes(t) && !round2EpisodeTypes.includes(t),
        ),
      );
    } else if (token === "diverse") {
      categoryMode = true;
      out.push(...trainingTypes);
    } else if (CATEGORY_ALIASES[token]) {
      categoryMode = true;
      out.push(...EPISODE_CATEGORIES[CATEGORY_ALIASES[token]].types);
    } else if (thematicAliases[token]) {
      out.push(...thematicAliases[token]);
    } else {
      out.push(token);
    }
  }
  return { types: [...new Set(out)], categoryMode };
}

const isCustomEpisodeTypes =
  process.env.EPISODE_TYPES && process.env.EPISODE_TYPES !== "all";
// Load episode types from environment variable or use default
const expandedEpisodeTypes = isCustomEpisodeTypes
  ? expandEpisodeTypes(process.env.EPISODE_TYPES)
  : { types: defaultEpisodeTypes, categoryMode: false };
const episodeTypes = expandedEpisodeTypes.types;

/**
 * Biome preferences of the integration for types whose class declares none
 * (biomes.js reads PREFERRED_BIOMES / AVOID_BIOMES; a class static wins).
 *  - forestBuild fells simple 3-7 log trunks: oak / birch forests, and the
 *    smaller spruces of taiga (snowy taiga canopies carry snow layers; dark oak, jungle, acacia,
 *    mangrove, cherry, tall spruce and old-growth birch trees have branches
 *    or 2x2 trunks). Plains were dropped (too few trees: near a border the
 *    bots landed on a beach / in savanna and the episode aborted);
 *  - snowIce builds a snow golem, which melts in the hot dry biomes;
 *  - tntDemolish needs a levelled tree-free site.
 */
const TYPE_BIOME_PREFERENCES = {
  forestBuild: {
    PREFERRED_BIOMES: [
      "forest",
      "flower_forest",
      "birch_forest",
      "windswept_forest",
      "taiga",
    ],
  },
  snowIce: { AVOID_BIOMES: ["@dry"] },
  tntDemolish: { AVOID_BIOMES: ["@trees", "@mountain"] },
};

/**
 * The object biomes.js reads PREFERRED_BIOMES / AVOID_BIOMES from: the class
 * itself, or a view of it with the integration defaults above.
 * @param {*} episodeInstance
 * @returns {Function|Object}
 */
function biomePreferenceSource(episodeInstance) {
  const C = episodeInstance.constructor;
  const pref = TYPE_BIOME_PREFERENCES[episodeInstance._episodeType];
  if (!pref) return C;
  return {
    name: C.name,
    PREFERRED_BIOMES:
      C.PREFERRED_BIOMES !== undefined ? C.PREFERRED_BIOMES : pref.PREFERRED_BIOMES,
    AVOID_BIOMES: C.AVOID_BIOMES !== undefined ? C.AVOID_BIOMES : pref.AVOID_BIOMES,
  };
}

/**
 * CLI value if given (non-empty), else the environment variable, else the
 * default. The entrypoint passes the round-2 integration options as empty
 * strings when unset, so the native Windows runner can still set them via
 * the process environment.
 */
function argOrEnv(args, key, envKey, dflt) {
  const v = args ? args[key] : undefined;
  if (v !== undefined && v !== null && String(v) !== "") return v;
  const e = process.env[envKey];
  if (e !== undefined && e !== "") return e;
  return dflt;
}

/**
 * Base randomTickSpeed for an episode (leader sets it before the start
 * snapshot): env-setup.js randomTickSpeedFor. Default "vanilla" (3): the
 * plugin's periodic box diff records the random-tick changes that fire no
 * Bukkit event (source `box_diff`), so natural dynamics stay on.
 * EPISODE_RANDOM_TICK_SPEED="auto" restores the old policy (0 in clear
 * weather, 3 in rain / snow); a number sets that value; -1 leaves the
 * gamerule alone. Episodes that need fast random ticks (iceNearFire,
 * forestBuild) raise it in their own setup and reset it in teardown; a class
 * may also set `static RANDOM_TICK_SPEED`.
 * @returns {number|null} value to set, or null to leave the gamerule alone
 */
function episodeRandomTickSpeed(args, episodeInstance, env) {
  return randomTickSpeedFor(args, episodeInstance.constructor, env ? env.weather : null);
}

/**
 * state_metadata written to episode_info: environment / biome / skin keys
 * (`_envMetadata`) merged with the handler's `_stateMetadata`. Handler values
 * win on conflicts, and the environment value it replaced is kept under
 * `env_shadowed.<key>`, so nothing is lost.
 * @param {*} episodeInstance
 * @returns {Object}
 */
function mergeStateMetadata(episodeInstance) {
  const env = episodeInstance?._envMetadata || {};
  const handler = episodeInstance?._stateMetadata || {};
  const out = { ...env };
  const shadowed = {};
  for (const [k, v] of Object.entries(handler)) {
    if (
      Object.prototype.hasOwnProperty.call(env, k) &&
      JSON.stringify(env[k]) !== JSON.stringify(v)
    ) {
      shadowed[k] = env[k];
    }
    out[k] = v;
  }
  if (Object.keys(shadowed).length > 0) out.env_shadowed = shadowed;
  return out;
}

function formatDateForFilename(date) {
  /**
   * Left-pad a value with zeros.
   * @param {number|string} value - Value to pad.
   * @param {number} [length=2] - Target width.
   * @returns {string} Padded string.
   */
  const pad = (value, length = 2) => String(value).padStart(length, "0");
  return `${date.getFullYear()}${pad(date.getMonth() + 1)}${pad(
    date.getDate(),
  )}_${pad(date.getHours())}${pad(date.getMinutes())}${pad(date.getSeconds())}`;
}

/**
 * Persist per-episode metadata to disk (one JSON file per episode).
 *
 * @param {Object} params - Parameters bag.
 * @param {Object} params.args - CLI/config args. Must include `output_dir` and `bot_name`.
 * @param {*} params.bot - Mineflayer bot instance.
 * @param {*} params.episodeInstance - Episode instance (used for error/eval flags).
 * @param {number} params.episodeNum - Episode number.
 * @param {string} params.episodeType - Episode type key.
 * @returns {Promise<void>} Resolves once the file is written.
 */
async function saveEpisodeInfo({
  args,
  bot,
  rcon,
  episodeInstance,
  episodeNum,
  episodeType,
}) {
  const now = new Date();
  const formattedTimestamp = formatDateForFilename(now);
  const episodeNumStr = String(episodeNum).padStart(6, "0");
  const instanceId = args.instance_id ?? 0;
  const instanceIdStr = String(instanceId).padStart(3, "0");
  const botName = args.bot_name;
  const outputDir = args.output_dir;

  await fs.mkdir(outputDir, { recursive: true });

  const baseFileName = `${formattedTimestamp}_${episodeNumStr}_${botName}_instance_${instanceIdStr}_episode_info`;
  const filePath = path.join(outputDir, `${baseFileName}.json`);

  const payload = {
    timestamp: now.toISOString(),
    bot_name: botName,
    world_type: args.world_type,
    episode_number: episodeNum,
    episode_type: episodeType,
    instance_id: instanceId,
    encountered_error: Boolean(episodeInstance?._encounteredError),
    peer_encountered_error: Boolean(episodeInstance?._peerError),
    bot_died: Boolean(episodeInstance?._botDied),
    episode_recording_started: Boolean(
      episodeInstance?._episodeRecordingStarted,
    ),
    eval_metadata: episodeInstance?._evalMetadata || {},
    // ---- stateful capture additions (see OUTPUT_SCHEMA.md) ----
    world: {
      // Real seed reported by the server (`/seed`), as a decimal string.
      seed: args._worldSeed ?? null,
      // Value passed to the server as level-seed (generate_compose.py).
      seed_config: args.world_seed_config || null,
      ...parseWorldGenInfo(args),
    },
    world_state_leader: isWorldStateLeader(bot, args),
    world_state: null,
    world_state_end: episodeInstance?._worldStateEnd || null,
    reset_world_between_episodes: Boolean(
      Number(args.reset_world_between_episodes),
    ),
    // Handler keys merged with the time/weather/difficulty keys (env-setup.js).
    state_metadata: mergeStateMetadata(episodeInstance),
    angle_convention: {
      yaw: "mineflayer radians in [0, 2pi): yaw = (pi - radians(mc_yaw)) mod 2pi; 0 faces -Z (north), pi/2 faces -X (west)",
      pitch: "mineflayer radians: pitch = -radians(mc_pitch); positive looks up",
    },
  };
  if (episodeInstance?._worldState) {
    payload.world_state = episodeInstance._worldState;
  } else if (rcon && Number(args.world_state_logging)) {
    // Non-leader bot: ask the plugin what the leader opened for this episode.
    const info = await worldStateInfo(rcon, args, episodeNum);
    if (info) {
      payload.world_state = info.begin || null;
      if (!payload.world_state_end) payload.world_state_end = info.end || null;
    }
  }

  await fs.writeFile(filePath, JSON.stringify(payload, null, 2));
  console.log(
    `[${bot.username}] Saved episode info to ${filePath} (encountered_error=${payload.encountered_error}, peer_encountered_error=${payload.peer_encountered_error}, bot_died=${payload.bot_died})`,
  );
}

/**
 * Run a single episode
 * @param {*} bot - Mineflayer bot instance
 * @param {*} rcon - RCON connection instance
 * @param {Function} sharedBotRng - Shared random number generator
 * @param {*} coordinator - Bot coordinator instance
 * @param {number} episodeNum - Episode number
 * @param {*} episodeInstance - Episode instance for this run
 * @param {Object} args - Configuration arguments
 * @returns {Promise<Function>} Resolves with a cleanup function for episode-scoped handlers.
 */
async function runSingleEpisode(
  bot,
  rcon,
  sharedBotRng,
  coordinator,
  episodeNum,
  episodeInstance,
  args,
) {
  console.log(`[${bot.username}] Starting episode ${episodeNum}`);

  episodeInstance._botDied = false;
  episodeInstance._episodeRecordingStarted = false;

  return new Promise((resolve) => {
    // Reset episode stopping guard at the start of each episode
    bot._episodeStopping = false;

    // Episode-scoped error handler that captures this episode number
    let episodeErrorHandled = false;

    /**
     * Handle any episode-scoped error (unhandled rejection/exception).
     *
     * Captures the episode number and ensures we only perform the stop/notify
     * sequence once per episode.
     *
     * @param {unknown} err - Error value.
     * @returns {Promise<void>} Resolves once peer has been notified and stop initiated.
     */
    const handleAnyError = async (err) => {
      if (episodeErrorHandled) {
        console.log(
          `[${bot.username}] Episode ${episodeNum} error already handled, skipping.`,
        );
        return;
      }
      episodeErrorHandled = true;
      episodeInstance._encounteredError = true;
      await notifyPeerErrorAndStop(
        bot,
        rcon,
        sharedBotRng,
        coordinator,
        episodeNum,
        episodeInstance,
        args,
        err,
      );
    };

    /**
     * Mark that the bot died during this episode.
     * @returns {void}
     */
    const handleBotDeath = () => {
      console.warn(
        `[${bot.username}] Episode ${episodeNum} detected bot death`,
      );
      episodeInstance._botDied = true;
    };

    /**
     * Remove all episode-scoped handlers/listeners.
     * @returns {void}
     */
    const cleanupEpisodeScopedHandlers = () => {
      process.removeListener("unhandledRejection", handleAnyError);
      process.removeListener("uncaughtException", handleAnyError);
      bot.removeListener("death", handleBotDeath);
    };
    process.on("unhandledRejection", handleAnyError);
    process.on("uncaughtException", handleAnyError);
    bot.once("death", handleBotDeath);

    // Ensure we clean up episode-scoped handlers when the episode resolves
    // Return the cleanup function to the caller so it can be invoked
    // after all pending phase handlers finish.
    /**
     * Resolve the episode promise with the cleanup function.
     * @returns {void}
     */
    bot._currentEpisodeResolve = () => {
      resolve(cleanupEpisodeScopedHandlers);
    };

    const { x, y, z } = bot.entity.position;
    console.log(
      `[${bot.username}] episode ${episodeNum} at (${x.toFixed(2)}, ${y.toFixed(
        2,
      )}, ${z.toFixed(2)})`,
    );

    coordinator.onceEvent(
      `peerErrorPhase_${episodeNum}`,
      episodeNum,
      getOnPeerErrorPhaseFn(
        bot,
        rcon,
        sharedBotRng,
        coordinator,
        episodeNum,
        episodeInstance,
        args,
      ),
    );
    const phaseDataOur = {
      position: bot.entity.position.clone(),
    };

    coordinator.onceEvent(
      "teleportPhase",
      episodeNum,
      getOnTeleportPhaseFn(
        bot,
        rcon,
        sharedBotRng,
        coordinator,
        episodeNum,
        episodeInstance,
        args,
        phaseDataOur,
      ),
    );
    coordinator.sendToOtherBot(
      "teleportPhase",
      phaseDataOur,
      episodeNum,
      "spawnPhase end",
    );
  });
}

/**
 * Notify the peer bot of an episode error and initiate the stop phase locally.
 *
 * @param {*} bot - Mineflayer bot instance.
 * @param {*} rcon - RCON connection instance.
 * @param {Function} sharedBotRng - Shared RNG instance.
 * @param {*} coordinator - Bot coordinator instance.
 * @param {number} episodeNum - Episode number.
 * @param {*} episodeInstance - Episode instance.
 * @param {Object} args - Configuration args.
 * @param {unknown} error - Error value.
 * @returns {Promise<void>} Resolves when peer is notified and stop is scheduled.
 */
async function notifyPeerErrorAndStop(
  bot,
  rcon,
  sharedBotRng,
  coordinator,
  episodeNum,
  episodeInstance,
  args,
  error,
) {
  const reason = error && error.message ? error.message : String(error);
  console.error(
    `[${bot.username}] Episode ${episodeNum} encountered an error:`,
    error,
  );
  coordinator.sendToOtherBot(
    `peerErrorPhase_${episodeNum}`,
    { reason },
    episodeNum,
    "error notifier",
  );
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
    `error notifier end`,
  );
  // Initiate our own stop sequence
}

/**
 * Setup bot protection effects and world rules (called once per bot)
 * @param {*} bot - Mineflayer bot instance
 * @param {*} rcon - RCON connection instance
 */
async function setupBotAndWorldOnce(bot, rcon) {
  // Resistance and difficulty are re-applied per episode by applyDamagePolicy
  // (env-setup.js): damage episodes clear the resistance and use normal.
  const resistEffectRes = await rcon.send(
    `effect give ${bot.username} minecraft:resistance 999999 255 true`,
  );
  console.log(`[${bot.username}] resistEffectRes=${resistEffectRes}`);
  const waterBreathingEffectRes = await rcon.send(
    `effect give ${bot.username} minecraft:water_breathing 999999 0 true`,
  );
  console.log(
    `[${bot.username}] waterBreathingEffectRes=${waterBreathingEffectRes}`,
  );
  const fallDamageRes = await setFallDamageMultiplierZero(bot, rcon, bot.username);
  console.log(`[${bot.username}] fallDamageRes=${fallDamageRes}`);
  const difficultyRes = await rcon.send("difficulty peaceful"); // or hard
  console.log(
    `[${bot.username}] set difficulty to peaceful, difficultyRes=${difficultyRes}`,
  );
  const fallDamageGameruleRes = await rcon.send("gamerule fallDamage false");
  console.log(
    `[${bot.username}] set fallDamage gamerule to false, fallDamageGameruleRes=${fallDamageGameruleRes}`,
  );
  const doImmediateRespawnRes = await rcon.send(
    "gamerule doImmediateRespawn true",
  );
  console.log(
    `[${bot.username}] set doImmediateRespawn gamerule to true, doImmediateRespawnRes=${doImmediateRespawnRes}`,
  );
  const keepInventoryRes = await rcon.send("gamerule keepInventory true");
  console.log(
    `[${bot.username}] set keepInventory gamerule to true, keepInventoryRes=${keepInventoryRes}`,
  );
  const showDeathMessagesRes = await rcon.send(
    "gamerule showDeathMessages false",
  );
  console.log(
    `[${bot.username}] set showDeathMessages gamerule to false, showDeathMessagesRes=${showDeathMessagesRes}`,
  );
  const tagResult = await rcon.send(`tag ${bot.username} add minebot`);
  console.log(
    `[${bot.username}] tag ${bot.username} add minebot result: ${tagResult}`,
  );
}

/**
 * Setup camera player protection effects (called once per camera)
 * @param {*} bot - Mineflayer bot instance (used to derive camera username)
 * @param {*} rcon - RCON connection instance
 */
async function setupCameraPlayerOnce(bot, rcon) {
  const cameraUsername = `Camera${bot.username}`;
  const resistEffectResCamera = await rcon.send(
    `effect give ${cameraUsername} minecraft:resistance 999999 255 true`,
  );
  console.log(`[${cameraUsername}] resistEffectRes=${resistEffectResCamera}`);
  const waterBreathingEffectResCamera = await rcon.send(
    `effect give ${cameraUsername} minecraft:water_breathing 999999 0 true`,
  );
  console.log(
    `[${cameraUsername}] waterBreathingEffectRes=${waterBreathingEffectResCamera}`,
  );
  const fallDamageResCamera = await setFallDamageMultiplierZero(
    bot,
    rcon,
    cameraUsername,
  );
  console.log(`[${cameraUsername}] fallDamageRes=${fallDamageResCamera}`);
}

/**
 * Setup bot and camera for each episode: damage policy (T14), camera
 * saturation, inventory.
 * @param {*} bot - Mineflayer bot instance
 * @param {*} rcon - RCON connection instance
 * @param {Object} args - Configuration arguments
 * @param {*} [episodeInstance] - Episode instance (type and metadata)
 */
async function setupBotAndCameraForEpisode(bot, rcon, args, episodeInstance = null) {
  // Damage episodes: difficulty normal, no resistance/saturation; others stay
  // protected (peaceful, resistance 255, saturation). Same value on both bots.
  const damageEnabled = isDamageEpisode(
    episodeInstance?._episodeType,
    episodeInstance,
  );
  const damagePolicy = await applyDamagePolicy(bot, rcon, damageEnabled);
  if (episodeInstance) addEnvMetadata(episodeInstance, damagePolicy);
  if (args.enable_camera_wait) {
    const camRes = await rcon.send(
      `effect give Camera${bot.username} minecraft:saturation 999999 255 true`,
    );
    console.log(`[${bot.username}] Camera saturationEffectRes=${camRes}`);
  }
  await sleep(1000);
  console.log(`[${bot.username}] unequipping hand before episode`);
  await clearBotInventory(bot, rcon);
  await sleep(500);
  await ensureBotHasEnough(bot, rcon, "stone", 64);
  const givePickaxeRes = await rcon.send(
    `give ${bot.username} minecraft:diamond_pickaxe 1`,
  );
  console.log(`[${bot.username}] givePickaxeRes=${givePickaxeRes}`);
  const giveShovelRes = await rcon.send(
    `give ${bot.username} minecraft:diamond_shovel 1`,
  );
  console.log(`[${bot.username}] giveShovelRes=${giveShovelRes}`);
  const giveAxeRes = await rcon.send(
    `give ${bot.username} minecraft:diamond_axe 1`,
  );
  console.log(`[${bot.username}] giveAxeRes=${giveAxeRes}`);
  await unequipHand(bot);
}

/**
 * Clear all items from the bot's inventory.
 *
 * @param {*} bot - Mineflayer bot instance.
 * @param {*} rcon - RCON connection instance.
 * @returns {Promise<void>} Resolves when the `/clear` command completes.
 */
async function clearBotInventory(bot, rcon) {
  // /clear <name> with no item argument deletes ALL items
  const cmd = `clear ${bot.username}`;
  const response = await rcon.send(cmd);
  console.log(`[${bot.username}] clearBotInventory response: ${response}`);
}

/**
 * Get spawn phase handler function
 * @param {*} bot - Mineflayer bot instance
 * @param {string} host - Server host
 * @param {number} actRecorderPort - Act recorder port
 * @param {*} coordinator - Bot coordinator instance
 * @param {Object} args - Configuration arguments
 * @returns {Function} Spawn phase handler
 */
function getOnSpawnFn(bot, host, actRecorderPort, coordinator, args) {
  return async () => {
    // Round 2: salt the shared bot RNG seed with the world seed (per batch
    // and instance: `<instance><unix time>` unless --world-seed is fixed), so
    // batches do not replay the same episode plans, materials, biomes, times
    // and skins. Both bots get the same WORLD_SEED_CONFIG. BOT_RNG_SEED_SALT=0
    // restores the plain BOT_RNG_SEED.
    if (args.bot_rng_seed_base === undefined) {
      args.bot_rng_seed_base = String(args.bot_rng_seed);
      const salt = String(argOrEnv(args, "bot_rng_seed_salt", "BOT_RNG_SEED_SALT", "1"));
      if (salt !== "0" && args.world_seed_config) {
        args.bot_rng_seed = `${args.bot_rng_seed_base}_${args.world_seed_config}`;
      }
      console.log(
        `[${bot.username}] bot RNG seed: ${args.bot_rng_seed} (base ${args.bot_rng_seed_base}, salt=${salt})`,
      );
    }
    bot.pathfinder.thinkTimeout = 7500; // max total planning time per path (ms)
    bot.pathfinder.tickTimeout = 15; // max CPU per tick spent "thinking" (ms)
    bot.pathfinder.searchRadius = 96; // don’t search beyond ~6 chunks from the bot
    bot.pathfinder.maxDropDown = 15;
    const rcon = await Rcon.connect({
      host: args.rcon_host,
      port: args.rcon_port,
      password: args.rcon_password,
      timeout: 10000, // increased from the 2000ms default
    });
    await setupBotAndWorldOnce(bot, rcon);
    args._worldSeed = await queryWorldSeed(rcon);
    console.log(
      `[${bot.username}] world seed reported by server: ${args._worldSeed} (configured: ${args.world_seed_config})`,
    );

    // Wait for both connections to be established
    console.log("Setting up coordinator connections...");
    await coordinator.setupConnections();
    console.log(
      "All coordinator connections ready, proceeding with bot spawn...",
    );

    const { x, y, z } = bot.entity.position;
    console.log(
      `[${bot.username}] spawned at (${x.toFixed(2)}, ${y.toFixed(
        2,
      )}, ${z.toFixed(2)})`,
    );

    // Wait for both cameras to join before starting recording
    if (args.enable_camera_wait) {
      console.log(`[${bot.username}] Waiting for cameras to join server...`);
      const camerasReady = await waitForCameras(
        args.rcon_host,
        args.rcon_port,
        args.rcon_password,
        args.camera_ready_retries,
        args.camera_ready_check_interval,
      );

      if (!camerasReady) {
        console.error(
          `[${bot.username}] Cameras failed to join within timeout. Exiting.`,
        );
        process.exit(1);
      }
      // Give resistance to the camera bot paired with this bot, e.g., if Alpha then AlphaCamera
      await setupCameraPlayerOnce(bot, rcon);

      console.log(
        `[${bot.username}] Cameras detected, waiting ${args.bootstrap_wait_time}s for popups to clear...`,
      );
      await sleep(args.bootstrap_wait_time * 1000);
    }

    // Initialize the per-frame recorder once for the entire program
    startFrameRecorder(bot, {
      output: `${host}:${actRecorderPort}`,
      width: 640,
      height: 360,
      frames: 400,
      disableRendering: args.viewer_rendering_disabled,
      interval: args.viewer_recording_interval,
      peerName: args.other_bot_name,
    });
    // Run multiple episodes
    // Respect world type for eligible episode filtering
    const worldType = (args.world_type || "flat").toLowerCase();
    const isFlatWorld = worldType === "flat";
    const allEpisodeTypes = episodeTypes;
    const unknownTypes = allEpisodeTypes.filter((t) => !episodeClassMap[t]);
    if (unknownTypes.length > 0) {
      throw new Error(
        `Invalid episode type(s): ${unknownTypes.join(", ")}; allowed types are: ${Object.keys(
          episodeClassMap,
        )
          .sort()
          .join(", ")}; aliases: all, upstream, diverse, ${Object.keys(CATEGORY_ALIASES).join(", ")}, ${Object.keys(thematicAliases).join(", ")}`,
      );
    }
    // Flat worlds: everything except types that need terrain (forestBuild,
    // REQUIRES_NON_FLAT_WORLD). Normal worlds: WORKS_IN_NON_FLAT_WORLD types.
    const eligibleEpisodeTypesForWorld = isFlatWorld
      ? allEpisodeTypes.filter(
          (type) => episodeClassMap[type].REQUIRES_NON_FLAT_WORLD !== true,
        )
      : allEpisodeTypes.filter(
          (type) => episodeClassMap[type].WORKS_IN_NON_FLAT_WORLD === true,
        );

    if (eligibleEpisodeTypesForWorld.length === 0) {
      throw new Error(
        isFlatWorld
          ? `No episode types are eligible for a flat world (${allEpisodeTypes.join(", ")} need normal terrain).`
          : "No episodes are eligible for normal world. Mark episode classes with WORKS_IN_NON_FLAT_WORLD = true.",
      );
    }
    const sortedEligible = eligibleEpisodeTypesForWorld.slice().sort();
    // Category-first sampling (EPISODE_TYPES=diverse or a category alias;
    // CATEGORY_SAMPLING=1/0 forces it on/off).
    const categorySamplingSpec = String(
      argOrEnv(args, "category_sampling", "CATEGORY_SAMPLING", ""),
    );
    const categoryMode =
      categorySamplingSpec === ""
        ? expandedEpisodeTypes.categoryMode
        : categorySamplingSpec !== "0";
    const samplingOpts = {
      uniform: Boolean(Number(args.uniform_weights || 0)),
      multipliers: parseWeightMultipliers(args.episode_weight_multipliers),
      categoryWeights: parseCategoryWeights(
        argOrEnv(args, "category_weights", "CATEGORY_WEIGHTS", ""),
      ),
      // R2 fixer: "episodes" (default) or "time" (category shares are shares
      // of the recorded time, see episode-weights.js categoryPlan)
      shareMode: String(argOrEnv(args, "category_share_mode", "CATEGORY_SHARE_MODE", "episodes")),
    };
    if (categoryMode) {
      const plan = categoryPlan(sortedEligible, samplingOpts);
      console.log(
        `[${bot.username}] category sampling (world_type=${worldType}): ` +
          plan.categories
            .map(
              (c) =>
                `${c.name}=${(c.share * 100).toFixed(1)}% (time ${(c.time_share * 100).toFixed(1)}%) [${c.types
                  .map((t) => `${t} ${(plan.probabilities[t] * 100).toFixed(1)}%`)
                  .join(", ")}]`,
            )
            .join(" | "),
      );
    } else {
      console.log(
        `[${bot.username}] eligible episode types (world_type=${worldType}): ${sortedEligible.join(", ")}`,
      );
    }

    // In smoke test mode, iterate over all eligible episode types in alphabetical order
    let episodesToRun = [];
    if (args.smoke_test === 1) {
      // Cycle through eligible episode types until we reach episodes_num
      for (let i = 0; i < args.episodes_num; i++) {
        const episodeType = sortedEligible[i % sortedEligible.length];
        episodesToRun.push({
          episodeNum: args.start_episode_id + i,
          episodeType: episodeType,
        });
      }
      console.log(
        `[${bot.username}] SMOKE TEST MODE: Running ${episodesToRun.length} episodes cycling through ${sortedEligible.length} eligible episode types (world_type=${worldType}) in alphabetical order`,
      );
    } else {
      // Normal mode: use the configured episodes_num, episode type picked at random from eligible
      for (let i = 0; i < args.episodes_num; i++) {
        episodesToRun.push({
          episodeNum: args.start_episode_id + i,
          episodeType: null, // Will be randomly selected
        });
      }
    }

    for (const episodeConfig of episodesToRun) {
      const episodeNum = episodeConfig.episodeNum;
      const botsRngBaseSeed = args.bot_rng_seed;
      // Concatenate episodeNum to the seed string to get a unique, reproducible seed per episode
      const botsRngSeedWithEpisode = `${botsRngBaseSeed}_${episodeNum}`;
      const sharedBotRng = seedrandom(botsRngSeedWithEpisode);

      // Select episode type: smoke-test cycling, category first (diverse),
      // or weighted by inverse sqrt of typical length.
      let selectedEpisodeType;
      let samplingMeta;
      if (args.smoke_test === 1) {
        selectedEpisodeType = episodeConfig.episodeType;
        samplingMeta = { episode_sampling: "smoke_test" };
      } else if (categoryMode) {
        const pick = selectCategoryEpisodeType(
          sortedEligible,
          sharedBotRng,
          samplingOpts,
        );
        selectedEpisodeType = pick.type;
        samplingMeta = {
          episode_sampling: "category",
          episode_category_share: pick.category_share,
          episode_type_probability: pick.type_probability,
        };
      } else {
        selectedEpisodeType = selectWeightedEpisodeType(
          sortedEligible,
          sharedBotRng,
          args.uniform_weights,
          !isCustomEpisodeTypes,
          samplingOpts.multipliers,
        );
        samplingMeta = { episode_sampling: "weighted" };
      }

      console.log(
        `[${bot.username}] Selected episode type: ${selectedEpisodeType}`,
      );

      // Get the episode class for the selected type
      const EpisodeClass = episodeClassMap[selectedEpisodeType];

      if (!EpisodeClass) {
        throw new Error(
          `Invalid episode type: ${selectedEpisodeType}, allowed types are: ${Object.keys(
            episodeClassMap,
          )
            .sort()
            .join(", ")}`,
        );
      }

      // Create an instance of the episode class
      const episodeInstance = new EpisodeClass(sharedBotRng);
      episodeInstance._episodeType = selectedEpisodeType;
      addEnvMetadata(episodeInstance, {
        episode_category: categoryOf(selectedEpisodeType),
        ...samplingMeta,
        bot_rng_seed: String(args.bot_rng_seed),
      });

      console.log(
        `[${bot.username}] Created ${EpisodeClass.name} instance for episode ${episodeNum}`,
      );
      // Round 2: per-episode character skins (separate seeded RNG; the
      // leader applies both, each bot waits for its texture). Never throws.
      try {
        episodeInstance._skinMetadata = await assignEpisodeSkins(
          bot,
          rcon,
          args,
          episodeNum,
        );
        if (episodeInstance._skinMetadata) {
          addEnvMetadata(episodeInstance, episodeInstance._skinMetadata);
        }
      } catch (err) {
        console.warn(`[${bot.username}] skin assignment failed: ${err.message}`);
      }
      // A death in a damage episode resets effects/attributes; restore them
      // before the teleport (spreadplayers selects @a[tag=minebot]).
      await ensureBotPersistentState(bot, rcon);
      await sleep(1000);
      const episodeCleanup = await runSingleEpisode(
        bot,
        rcon,
        sharedBotRng,
        coordinator,
        episodeNum,
        episodeInstance,
        args,
      );
      await coordinator.waitForAllPhasesToFinish();
      episodeCleanup();

      // Force stop bot.pvp and pathfinder navigation
      if (bot.pvp) {
        bot.pvp.forceStop();
        console.log(`[${bot.username}] Stopped PVP for episode ${episodeNum}`);
      }
      if (bot.pathfinder) {
        bot.pathfinder.setGoal(null);
        console.log(
          `[${bot.username}] Stopped pathfinder navigation for episode ${episodeNum}`,
        );
      }
      stopAll(bot);

      console.log(`[${bot.username}] tearing down episode ${episodeNum}`);
      try {
        await episodeInstance.tearDownEpisode(
          bot,
          rcon,
          sharedBotRng,
          coordinator,
          episodeNum,
          args,
        );
      } catch (err) {
        console.error(
          `[${bot.username}] Error during tearDownEpisode, continuing:`,
          err,
        );
      }
      // Close the world-state log (end snapshot, optional box restore).
      if (
        isWorldStateLeader(bot, args) &&
        episodeInstance._worldState &&
        episodeInstance._worldState.stem
      ) {
        episodeInstance._worldStateEnd = await worldStateEnd(
          bot,
          rcon,
          args,
          episodeInstance._worldState.stem,
        );
      }
      // R2 fixer: summoned creatures, vehicles, babies, drops and arrows of
      // this episode must not outlive it (the world log is closed now).
      if (isWorldStateLeader(bot, args)) {
        await cleanupEpisodeEntities(bot, rcon, episodeInstance);
      }
      console.log(`[${bot.username}] Episode ${episodeNum} completed`);
      if (episodeInstance._biomeMetaPromise) {
        await Promise.race([episodeInstance._biomeMetaPromise, sleep(5000)]);
      }
      finalizeEpisodeBiome(episodeInstance);
      await saveEpisodeInfo({
        args,
        bot,
        rcon,
        episodeInstance,
        episodeNum,
        episodeType: selectedEpisodeType,
      });
      console.log(`[${bot.username}] Syncing bots for episode ${episodeNum}`);
      await coordinator.syncBots(episodeNum);
      console.log(`[${bot.username}] Synced bots for episode ${episodeNum}`);
    }
    if (bot._locateRcon) {
      try {
        await bot._locateRcon.end();
      } catch (_) {}
    }
    await rcon.end();

    const totalEpisodesRun = episodesToRun.length;
    console.log(`[${bot.username}] All ${totalEpisodesRun} episodes completed`);
    process.exit(0);
  };
}

/**
 * Get teleport phase handler function
 * @param {*} bot - Mineflayer bot instance
 * @param {*} rcon - RCON connection instance
 * @param {Function} sharedBotRng - Shared random number generator
 * @param {*} coordinator - Bot coordinator instance
 * @param {number} episodeNum - Episode number
 * @param {*} episodeInstance - Episode instance
 * @param {Object} args - Configuration arguments
 * @param {Object} phaseDataOur - Our phase data payload
 * @returns {Function} Teleport phase handler
 */
function getOnTeleportPhaseFn(
  bot,
  rcon,
  sharedBotRng,
  coordinator,
  episodeNum,
  episodeInstance,
  args,
  phaseDataOur,
) {
  return async (phaseDataOther) => {
    coordinator.sendToOtherBot(
      "teleportPhase",
      phaseDataOur,
      episodeNum,
      "teleportPhase beginning",
    );
    const otherBotPosition = phaseDataOther.position;
    const ourPosition = phaseDataOur.position;
    // T1 + R2: time of day and weather, decided by a separate seeded RNG
    // (both bots get the same value), applied by the leader before the start
    // snapshot. Episode classes may set PREFERRED_TIME / PREFERRED_WEATHER.
    // Chosen before the teleport: a "snow" request sends normal-world
    // episodes to a cold biome.
    const env = chooseEpisodeEnvironment(args, episodeNum, {
      evalDay: Boolean(args.eval_time_set_day) && isEvalEpisode(episodeInstance),
      isEval: isEvalEpisode(episodeInstance),
      episodeClass: episodeInstance.constructor,
      episodeType: episodeInstance._episodeType,
    });
    addEnvMetadata(episodeInstance, env);
    episodeInstance._biomeSite = null;
    episodeInstance._biomeSiteFailure = null;
    episodeInstance._sitePrep = null;
    episodeInstance._flatBiome = null;
    if (args.teleport && bot.username < args.other_bot_name) {
      console.log(`[${bot.username}] performs bots teleporting`);
      // R2 fixer: leftovers of earlier episodes (tagged creatures / vehicles,
      // drops, arrows) around the old site, before we leave it.
      await cleanupEpisodeEntities(bot, rcon, null, "loaded");
      // R2: normal worlds go to a located target biome (biomes.js); the
      // random spreadplayers teleport stays as the fallback.
      let siteTp = null;
      if (!isEvalEpisode(episodeInstance)) {
        try {
          bot._lastBiomeSiteFailure = null;
          const site = await chooseEpisodeSite(
            bot,
            await getLocateRcon(bot, args, rcon),
            args,
            episodeNum,
            biomePreferenceSource(episodeInstance),
            { weatherRequested: env.weather_requested },
          );
          if (site) {
            siteTp = await teleportToSite(bot, rcon, args, site, episodeInstance);
            if (siteTp.ok) {
              episodeInstance._biomeSite = {
                ...site,
                teleport_center: siteTp.center,
                biome_match: siteTp.biome_match,
                recentered: siteTp.recentered,
              };
            } else {
              episodeInstance._biomeSiteFailure = {
                biome_target: site.biome_target,
                reason: "spread_failed",
              };
            }
          } else if (bot._lastBiomeSiteFailure) {
            episodeInstance._biomeSiteFailure = {
              biome_target: bot._lastBiomeSiteFailure.biome_target,
              reason: "not_located",
            };
          }
        } catch (err) {
          console.warn(`[${bot.username}] biome site failed: ${err.message}`);
        }
      }
      if (!siteTp || !siteTp.ok) {
        await teleport(
          bot,
          rcon,
          args,
          ourPosition,
          otherBotPosition,
          episodeInstance,
          sharedBotRng,
          episodeNum,
        );
      }
      // ... and around the new site (chunks loaded by the teleport).
      await sleep(300);
      await cleanupEpisodeEntities(bot, rcon, null, bot.entity.position, 64);
      // R2 fixer: flat worlds get a per-episode biome around the bots
      // (utils/flat-biome.js; chunk data, not blocks; before the snapshot).
      if (
        String(args.world_type || "flat").toLowerCase() === "flat" &&
        !isEvalEpisode(episodeInstance)
      ) {
        try {
          const plan = planFlatBiome(
            args,
            episodeNum,
            biomePreferenceSource(episodeInstance),
            env.weather_requested,
          );
          const other = bot.players[args.other_bot_name]?.entity;
          const centre = other
            ? bot.entity.position.plus(other.position).scaled(0.5)
            : bot.entity.position;
          episodeInstance._flatBiome = await applyFlatBiome(bot, rcon, plan, centre);
        } catch (err) {
          console.warn(`[${bot.username}] flat biome failed: ${err.message}`);
        }
      }
      // R2: optional levelled pad (static SITE_PREP = {level: true, radius: 3})
      // on normal worlds, before any setup and before the start snapshot.
      const prepOpts = episodeInstance.constructor.SITE_PREP;
      if (prepOpts && String(args.world_type || "flat").toLowerCase() !== "flat") {
        const other = bot.players[args.other_bot_name]?.entity;
        const me = bot.entity.position;
        const centre = other ? me.plus(other.position).scaled(0.5) : me;
        try {
          episodeInstance._sitePrep = sitePrepMetadata(
            await prepareSite(bot, rcon, args, centre, {
              ...prepOpts,
              episodeInstance,
            }),
          );
        } catch (err) {
          console.warn(`[${bot.username}] site prep failed: ${err.message}`);
        }
      }
    }
    if (isWorldStateLeader(bot, args)) {
      const applied = await applyEpisodeEnvironment(bot, rcon, env, {
        damageEnabled: isDamageEpisode(
          episodeInstance?._episodeType,
          episodeInstance,
        ),
      });
      if (!applied) {
        console.warn(
          `[${bot.username}] episode ${episodeNum}: time/weather commands failed`,
        );
      }
    }
    // R2: base randomTickSpeed (see episodeRandomTickSpeed); both bots
    // compute the value, the leader sets it. Handlers that need random ticks
    // raise it later in their setup.
    const tickSpeed = episodeRandomTickSpeed(args, episodeInstance, env);
    addEnvMetadata(episodeInstance, { random_tick_speed_base: tickSpeed });
    if (tickSpeed !== null && isWorldStateLeader(bot, args)) {
      try {
        const res = await rcon.send(`gamerule randomTickSpeed ${tickSpeed}`);
        console.log(
          `[${bot.username}] gamerule randomTickSpeed ${tickSpeed} -> ${res}`,
        );
      } catch (err) {
        console.warn(
          `[${bot.username}] gamerule randomTickSpeed ${tickSpeed} failed: ${err.message}`,
        );
      }
    }

    coordinator.onceEvent(
      "postTeleportPhase",
      episodeNum,
      getOnPostTeleportPhaseFn(
        bot,
        rcon,
        sharedBotRng,
        coordinator,
        episodeNum,
        episodeInstance,
        args,
      ),
    );
    coordinator.sendToOtherBot(
      "postTeleportPhase",
      {
        biomeSite: episodeInstance._biomeSite || null,
        biomeSiteFailure: episodeInstance._biomeSiteFailure || null,
        sitePrep: episodeInstance._sitePrep || null,
        flatBiome: episodeInstance._flatBiome || null,
      },
      episodeNum,
      "teleportPhase end",
    );
  };
}

/**
 * Get post-teleport phase handler.
 *
 * @param {*} bot - Mineflayer bot instance
 * @param {*} rcon - RCON connection instance
 * @param {Function} sharedBotRng - Shared RNG
 * @param {*} coordinator - Bot coordinator
 * @param {number} episodeNum - Episode number
 * @param {*} episodeInstance - Episode instance
 * @param {Object} args - Configuration args
 * @returns {Function} Post-teleport phase handler
 */
function getOnPostTeleportPhaseFn(
  bot,
  rcon,
  sharedBotRng,
  coordinator,
  episodeNum,
  episodeInstance,
  args,
) {
  return async (phaseDataOther) => {
    // The peer is triggered either by our "teleportPhase end" or by this
    // "beginning" message (whichever it was listening for), so both carry the
    // leader's biome site.
    coordinator.sendToOtherBot(
      "postTeleportPhase",
      {
        biomeSite: episodeInstance._biomeSite || null,
        biomeSiteFailure: episodeInstance._biomeSiteFailure || null,
        sitePrep: episodeInstance._sitePrep || null,
        flatBiome: episodeInstance._flatBiome || null,
      },
      episodeNum,
      "postTeleportPhase beginning",
    );
    // R2: the leader's biome site / site prep (or ours), the real biome at
    // our feet and what actually falls there. Background task: nothing may
    // be awaited before the setupEpisodePhase listener below is registered
    // (the peer's setupEpisodePhase message would arrive with no listener).
    // The episode loop awaits it (max 5 s) before saveEpisodeInfo.
    const biomeSite =
      episodeInstance._biomeSite || phaseDataOther?.biomeSite || null;
    const biomeSiteFailure =
      episodeInstance._biomeSiteFailure || phaseDataOther?.biomeSiteFailure || null;
    const sitePrep = episodeInstance._sitePrep || phaseDataOther?.sitePrep || null;
    const flatBiome = episodeInstance._flatBiome || phaseDataOther?.flatBiome || null;
    if (flatBiome) {
      episodeInstance._flatBiome = flatBiome;
      addEnvMetadata(episodeInstance, { flat_biome_swap: flatBiome });
    }
    const feet = bot.entity.position.clone();
    episodeInstance._biomeMetaPromise = (async () => {
      const isFlat = String(args.world_type || "flat").toLowerCase() === "flat";
      const worldGen = parseWorldGenInfo(args);
      const hint = isFlat
        ? flatBiome && flatBiome.ok
          ? flatBiome.biome
          : worldGen.flat_biome
        : biomeSite?.biome_located;
      const rb = await resolveBiome(bot, rcon, feet, hint ? [hint] : []);
      // Per-bot value at this bot's feet right after the teleport. The
      // episode-level `biome` (same on both bots) is resolved once by the
      // leader at the episode site after setup (finalizeEpisodeBiome).
      episodeInstance._feetBiome = {
        biome: rb.biome || (isFlat ? worldGen.flat_biome || null : null),
        source: rb.biome ? rb.source : isFlat && worldGen.flat_biome ? "flat_world" : null,
        y: feet.y,
      };
      const siteMeta = biomeSiteMetadata(isFlat ? null : biomeSite, {
        biome_at_feet: rb.biome,
        biome_at_feet_source: rb.source,
        biome_at_feet_client: rb.client_biome,
      });
      if (!isFlat && !biomeSite && biomeSiteFailure) {
        siteMeta.biome_target = biomeSiteFailure.biome_target || null;
        siteMeta.biome_site_failure = biomeSiteFailure.reason || null;
      }
      if (biomeSite) {
        siteMeta.biome_match = biomeSite.biome_match ?? null;
        siteMeta.biome_recentered = Boolean(biomeSite.recentered);
      }
      addEnvMetadata(episodeInstance, {
        ...siteMeta,
        ...(isFlat ? { biome_site_source: "flat_world" } : {}),
        ...(sitePrep || {}),
      });
    })().catch((err) => {
      console.warn(`[${bot.username}] biome metadata failed: ${err.message}`);
    });
    const phaseDataOur = {
      position: bot.entity.position.clone(),
    };
    console.log(
      `[${bot.username}] our position after teleport: ${JSON.stringify(
        phaseDataOur,
      )}`,
    );

    coordinator.onceEvent(
      "setupEpisodePhase",
      episodeNum,
      getOnSetupEpisodeFn(
        bot,
        rcon,
        sharedBotRng,
        coordinator,
        episodeNum,
        episodeInstance,
        args,
        phaseDataOur,
      ),
    );
    coordinator.sendToOtherBot(
      "setupEpisodePhase",
      phaseDataOur,
      episodeNum,
      "postTeleportPhase end",
    );
  };
}

/**
 * Get setup-episode phase handler.
 *
 * @param {*} bot - Mineflayer bot instance
 * @param {*} rcon - RCON connection instance
 * @param {Function} sharedBotRng - Shared RNG
 * @param {*} coordinator - Bot coordinator
 * @param {number} episodeNum - Episode number
 * @param {*} episodeInstance - Episode instance
 * @param {Object} args - Configuration args
 * @param {Object} phaseDataOur - Our phase payload (expects `position`)
 * @returns {Function} Setup-episode phase handler
 */
function getOnSetupEpisodeFn(
  bot,
  rcon,
  sharedBotRng,
  coordinator,
  episodeNum,
  episodeInstance,
  args,
  phaseDataOur,
) {
  return async (phaseDataOther) => {
    console.log(
      `[${bot.username}] other position after teleport: ${JSON.stringify(
        phaseDataOther,
      )}`,
    );
    try {
      await setupBotAndCameraForEpisode(bot, rcon, args, episodeInstance);
    } catch (error) {
      console.error(
        `[${bot.username}] Failed to setup bot and camera for episode:`,
        error,
      );
    }
    console.log(`[${bot.username}] setting up episode ${episodeNum}`);
    const { botPositionNew, otherBotPositionNew } =
      await episodeInstance.setupEpisode(
        bot,
        rcon,
        sharedBotRng,
        coordinator,
        episodeNum,
        args,
        phaseDataOur.position,
        phaseDataOther.position,
      );
    await lookAtSmooth(
      bot,
      otherBotPositionNew,
      DEFAULT_CAMERA_SPEED_DEGREES_PER_SEC,
      { randomized: false, useEasing: false },
    );
    // R2 fixer: respawn point = where this episode starts (a death in a
    // damage episode must not send the bot to an old arena far away).
    try {
      const p = bot.entity.position;
      await rcon.send(
        `spawnpoint ${bot.username} ${Math.floor(p.x)} ${Math.floor(p.y)} ${Math.floor(p.z)}`,
      );
    } catch (err) {
      console.warn(`[${bot.username}] spawnpoint failed: ${err.message}`);
    }
    // R2 fixer: the leader resolves the episode biome once (server side, at
    // the episode site) and sends it to the follower with the
    // start-recording message, so both episode_info files carry the same
    // `biome`.
    if (isWorldStateLeader(bot, args)) {
      try {
        episodeInstance._episodeBiome = await resolveEpisodeBiome(
          bot,
          rcon,
          args,
          episodeInstance,
        );
      } catch (err) {
        console.warn(`[${bot.username}] episode biome failed: ${err.message}`);
      }
      // R2 fixer: optional background animals (utils/ambient-fauna.js),
      // summoned before the start snapshot.
      try {
        const C = episodeInstance.constructor;
        const eligible =
          !isEvalEpisode(episodeInstance) &&
          !isDamageEpisode(episodeInstance._episodeType, episodeInstance) &&
          !(Number(C.CLEANUP_ENTITIES_RADIUS) > 0) &&
          C.AMBIENT_FAUNA !== false;
        const plan = planAmbientFauna(args, episodeNum, {
          episodeType: episodeInstance._episodeType,
          biome: episodeInstance._episodeBiome ? episodeInstance._episodeBiome.biome : null,
          eligible,
        });
        if (plan) {
          const other = bot.players[args.other_bot_name]?.entity;
          const centre =
            episodeInstance._worldStateCenter ||
            (other ? bot.entity.position.plus(other.position).scaled(0.5) : bot.entity.position);
          episodeInstance._ambientFauna = await summonAmbientFauna(bot, rcon, plan, centre, groundAt);
          addEnvMetadata(episodeInstance, { ambient_fauna: episodeInstance._ambientFauna });
        }
      } catch (err) {
        console.warn(`[${bot.username}] ambient fauna failed: ${err.message}`);
      }
    }

    await sleep(1000);

    // Call the entry point method
    coordinator.onceEvent(
      "startRecordingPhase",
      episodeNum,
      getOnStartRecordingFn(
        bot,
        rcon,
        sharedBotRng,
        coordinator,
        episodeNum,
        episodeInstance,
        args,
      ),
    );
    coordinator.sendToOtherBot(
      "startRecordingPhase",
      startRecordingPayload(bot, episodeInstance),
      episodeNum,
      "teleportPhase end",
    );
  };
}

/**
 * Leader: a dedicated RCON connection for /locate biome (normal worlds) with
 * a long packet timeout (60 s). A slow locate then neither times out on the
 * shared connection (10 s packet timeout) nor makes the following commands
 * of the episode loop queue behind it. Falls back to `fallback` when the
 * connection cannot be opened. Closed at the end of the run.
 */
async function getLocateRcon(bot, args, fallback) {
  if (bot._locateRcon) return bot._locateRcon;
  if (bot._locateRconFailed) return fallback;
  try {
    bot._locateRcon = await Rcon.connect({
      host: args.rcon_host,
      port: args.rcon_port,
      password: args.rcon_password,
      timeout: 60000,
    });
    bot._locateRcon.on("end", () => {
      bot._locateRcon = null;
    });
    bot._locateRcon.on("error", (err) => {
      console.warn(`[${bot.username}] locate rcon error: ${err && err.message}`);
    });
    console.log(`[${bot.username}] opened the dedicated locate RCON connection`);
    return bot._locateRcon;
  } catch (err) {
    console.warn(
      `[${bot.username}] locate rcon connect failed (${err.message}); using the shared connection`,
    );
    bot._locateRconFailed = true;
    return fallback;
  }
}

/**
 * Entity types never removed by the clean-up below (players, cameras and
 * hanging / decorative entities of natural structures).
 */
const CLEANUP_KEEP_TYPES = [
  "player",
  "item_frame",
  "glow_item_frame",
  "painting",
  "armor_stand",
  "marker",
  "item_display",
  "block_display",
  "text_display",
  "interaction",
];

/**
 * Leader: remove what earlier episodes left behind (summoned creatures and
 * vehicles carry the tag `solaris_r2`, utils/creatures.js SUMMON_TAG).
 *  - always: every loaded entity with that tag;
 *  - `where = "loaded"`: also every loaded dropped item and arrow (teleport
 *    phase, before the start snapshot of the next episode);
 *  - `where = {x, y, z}` + radius: also items / arrows / XP orbs around it;
 *  - episodes with `static CLEANUP_ENTITIES_RADIUS` (creature episodes):
 *    every non-player entity (except CLEANUP_KEEP_TYPES) within that radius
 *    of the episode site after the episode (bred babies, vexes, untagged
 *    chicks, leftover hostiles).
 * Never throws.
 */
async function cleanupEpisodeEntities(bot, rcon, episodeInstance = null, where = null, radius = 64) {
  const cmds = [`kill @e[tag=${SUMMON_TAG}]`];
  if (where === "loaded") {
    cmds.push("kill @e[type=minecraft:item]");
    cmds.push("kill @e[type=minecraft:arrow]");
    cmds.push("kill @e[type=minecraft:spectral_arrow]");
  } else if (where && Number.isFinite(where.x)) {
    const at = `execute positioned ${Math.floor(where.x)} ${Math.floor(where.y)} ${Math.floor(where.z)} run`;
    for (const t of ["item", "arrow", "spectral_arrow", "experience_orb"]) {
      cmds.push(`${at} kill @e[type=minecraft:${t},distance=..${radius}]`);
    }
  }
  const C = episodeInstance ? episodeInstance.constructor : null;
  const c = episodeInstance ? episodeInstance._worldStateCenter : null;
  const r = C ? Number(C.CLEANUP_ENTITIES_RADIUS) : NaN;
  if (c && Number.isFinite(r) && r > 0) {
    const keep = CLEANUP_KEEP_TYPES.map((t) => `type=!minecraft:${t}`).join(",");
    cmds.push(
      `execute positioned ${Math.floor(c.x)} ${Math.floor(c.y)} ${Math.floor(c.z)} run kill @e[${keep},distance=..${r}]`,
    );
  }
  for (const cmd of cmds) {
    try {
      const res = await rcon.send(cmd);
      console.log(`[${bot.username}] cleanup '${cmd}' -> ${String(res).slice(0, 120)}`);
    } catch (err) {
      console.warn(`[${bot.username}] cleanup '${cmd}' failed: ${err.message}`);
    }
  }
}

/**
 * Payload of the start-recording messages: our position (x, y, z, as
 * before) plus the leader's episode biome.
 */
function startRecordingPayload(bot, episodeInstance) {
  const p = bot.entity.position;
  return {
    x: p.x,
    y: p.y,
    z: p.z,
    episodeBiome: episodeInstance._episodeBiome || null,
    ambientFauna: episodeInstance._ambientFauna || null,
  };
}

/**
 * Leader: the episode biome, resolved once at the episode site (the
 * handler's server-resolved site biome `_siteBiome` if it has one, else
 * `execute if biome` one block above the world-state centre, else at the
 * leader's feet).
 * @returns {Promise<{biome, source, client, pos, scope}|null>}
 */
async function resolveEpisodeBiome(bot, rcon, args, episodeInstance) {
  const isFlat = String(args.world_type || "flat").toLowerCase() === "flat";
  const worldGen = parseWorldGenInfo(args);
  const sb = episodeInstance._siteBiome;
  if (sb && sb.biome) {
    return {
      biome: sb.biome,
      source: sb.source || null,
      client: sb.client || null,
      pos: sb.pos || null,
      scope: "episode_site",
    };
  }
  const c = episodeInstance._worldStateCenter;
  const scope = c ? "episode_site" : "leader_feet";
  const base = c || bot.entity.position;
  const pos = {
    x: Math.floor(base.x),
    y: Math.floor(base.y) + (c ? 1 : 0),
    z: Math.floor(base.z),
  };
  const hints = [];
  if (isFlat && worldGen.flat_biome) hints.push(worldGen.flat_biome);
  if (episodeInstance._feetBiome && episodeInstance._feetBiome.biome) {
    hints.push(episodeInstance._feetBiome.biome);
  }
  const rb = await Promise.race([
    resolveBiome(bot, rcon, pos, hints),
    sleep(9000).then(() => null),
  ]);
  if (!rb || !rb.biome) {
    if (isFlat && worldGen.flat_biome) {
      return {
        biome: worldGen.flat_biome,
        source: "flat_world",
        client: null,
        pos: [pos.x, pos.y, pos.z],
        scope,
      };
    }
    return null;
  }
  return {
    biome: rb.biome,
    source: rb.source,
    client: rb.client_biome,
    pos: [pos.x, pos.y, pos.z],
    scope,
  };
}

/**
 * Write the episode-level biome keys (`biome`, `biome_source`,
 * `biome_client`, `biome_pos`, `biome_scope`, `precipitation`,
 * `precipitation_biome`): the leader's episode biome when known (same on
 * both bots), else this bot's feet biome (`biome_scope` = "bot_feet").
 */
function finalizeEpisodeBiome(episodeInstance) {
  const eb = episodeInstance._episodeBiome;
  const fb = episodeInstance._feetBiome;
  let meta;
  let y = 64;
  if (eb && eb.biome) {
    meta = {
      biome: eb.biome,
      biome_source: eb.source || null,
      biome_client: eb.client || null,
      biome_pos: eb.pos || null,
      biome_scope: eb.scope || "episode_site",
    };
    if (Array.isArray(eb.pos)) y = eb.pos[1];
  } else if (fb) {
    meta = {
      biome: fb.biome || null,
      biome_source: fb.source || null,
      biome_client: null,
      biome_pos: null,
      biome_scope: "bot_feet",
    };
    if (Number.isFinite(fb.y)) y = fb.y;
  } else {
    return;
  }
  addEnvMetadata(episodeInstance, {
    ...meta,
    ...resolvePrecipitation(episodeInstance._envMetadata, meta.biome, y),
  });
}

/**
 * Get start-recording phase handler.
 *
 * @param {*} bot - Mineflayer bot instance
 * @param {*} rcon - RCON connection instance
 * @param {Function} sharedBotRng - Shared RNG
 * @param {*} coordinator - Bot coordinator
 * @param {number} episodeNum - Episode number
 * @param {*} episodeInstance - Episode instance
 * @param {Object} args - Configuration args
 * @returns {Function} Start-recording phase handler
 */
function getOnStartRecordingFn(
  bot,
  rcon,
  sharedBotRng,
  coordinator,
  episodeNum,
  episodeInstance,
  args,
) {
  return async (otherData) => {
    if (
      !isWorldStateLeader(bot, args) &&
      otherData &&
      otherData.episodeBiome &&
      otherData.episodeBiome.biome
    ) {
      episodeInstance._episodeBiome = otherData.episodeBiome;
    }
    if (!isWorldStateLeader(bot, args) && otherData && otherData.ambientFauna) {
      episodeInstance._ambientFauna = otherData.ambientFauna;
      addEnvMetadata(episodeInstance, { ambient_fauna: otherData.ambientFauna });
    }
    coordinator.sendToOtherBot(
      "startRecordingPhase",
      startRecordingPayload(bot, episodeInstance),
      episodeNum,
      "startRecordingPhase end",
    );
    if (bot._episodeStopping) {
      console.log(
        `[${bot.username}] episode already stopping, skipping start recording`,
      );
    } else {
      if (isWorldStateLeader(bot, args) && Number(args.world_state_logging)) {
        // Open world_events.jsonl and capture the initial state of the box
        // before any episode action can change the world (all episode
        // actions happen after the first phase barrier in entryPoint).
        episodeInstance._worldState = await worldStateBegin(
          bot,
          rcon,
          args,
          episodeNum,
          episodeInstance._episodeType,
          episodeInstance._worldStateCenter || null,
        );
        if (episodeInstance._worldState && episodeInstance._skinMetadata) {
          await noteEpisodeSkins(
            rcon,
            bot,
            episodeNum,
            episodeInstance._skinMetadata,
          );
        }
      }
      addEnvMetadata(episodeInstance, weatherObservation(bot));
      console.log(`[${bot.username}] starting episode recording`);
      bot.emit("startepisode", episodeNum);
      episodeInstance._episodeRecordingStarted = true;
      await sleep(1000);
    }

    const iterationID = 0;
    episodeInstance.entryPoint(
      bot,
      rcon,
      sharedBotRng,
      coordinator,
      iterationID,
      episodeNum,
      args,
    );
  };
}

/**
 * Teleport both bots to a randomized location (or episode-specific fixed points).
 *
 * Uses `spreadplayers` to place both bots within the configured distance bounds.
 * For some eval episodes, applies special-case teleporting.
 *
 * @param {*} bot - Mineflayer bot instance.
 * @param {*} rcon - RCON connection instance.
 * @param {Object} args - Configuration args.
 * @param {*} ourPosition - Our current position (vec3-like).
 * @param {*} otherBotPosition - Peer current position (vec3-like).
 * @param {*} episodeInstance - Episode instance (used for min/max distances and eval detection).
 * @param {Function} sharedBotRng - Shared RNG (currently unused; reserved for future determinism).
 * @param {number} episodeNum - Episode number (for logging/commands).
 * @returns {Promise<void>} Resolves when teleport attempt finishes.
 */
async function teleport(
  bot,
  rcon,
  args,
  ourPosition,
  otherBotPosition,
  episodeInstance,
  sharedBotRng,
  episodeNum,
) {
  // Set time to day for eval episodes if enabled
  if (args.eval_time_set_day && isEvalEpisode(episodeInstance)) {
    const timeSetRes = await rcon.send("time set day");
    console.log(
      `[${bot.username}] time set to day for eval episode, result=${timeSetRes}`,
    );
  }

  // Custom TP logic for TurnToLookEpisode
  if (
    episodeInstance instanceof TurnToLookEvalEpisode ||
    episodeInstance instanceof TurnToLookOppositeEvalEpisode
  ) {
    if (turnToLookEvalTpPoints && turnToLookEvalTpPoints.length > 0) {
      await directTeleport(
        bot,
        rcon,
        args.other_bot_name,
        episodeNum,
        turnToLookEvalTpPoints,
      );
      return;
    }
  }

  // Initialize teleport center once as the midpoint between this bot and the other bot
  if (!bot._teleport_center) {
    bot._teleport_center = {
      x: (ourPosition.x + otherBotPosition.x) / 2,
      z: (ourPosition.z + otherBotPosition.z) / 2,
    };
    console.log(
      `[${bot.username}] initializing teleport center: ${JSON.stringify(bot._teleport_center)}`,
    );
  }
  if (!bot._teleport_radius) {
    bot._teleport_radius = args.teleport_radius;
  }
  const teleportCenter = bot._teleport_center;
  // Seeded RNG so that (seed, bot_rng_seed, episode) reproduces the location.
  // Only the leader teleports, so this does not consume the shared bot RNG.
  const tpRandom = Number(args.deterministic_teleport)
    ? seedrandom(`${args.bot_rng_seed}_teleport_${episodeNum}`)
    : Math.random;
  // Pick a random point in the world within the specified radius from center
  const MAX_ATTEMPTS_WITH_THIS_RADIUS = 10;
  const TOTAL_ATTEMPTS = 100;
  const MAX_RCON_TIMEOUTS = 3;
  let attemptsWithThisRadius = 0;
  let rconTimeoutCount = 0;
  let success = false;
  for (let i = 0; i < TOTAL_ATTEMPTS; i++) {
    console.log(
      `[${bot.username}] teleporting with radius: ${bot._teleport_radius}`,
    );
    const randomAngle = tpRandom() * 2 * Math.PI;
    const randomDistance = tpRandom() * bot._teleport_radius;

    const randomPointX =
      teleportCenter.x + randomDistance * Math.cos(randomAngle);
    const randomPointZ =
      teleportCenter.z + randomDistance * Math.sin(randomAngle);

    console.log(
      `[${bot.username}] picked random center at (${randomPointX.toFixed(
        2,
      )}, ${randomPointZ.toFixed(2)})`,
    );
    // Use spreadplayers to place both bots around the chosen center
    const centerX = Math.floor(randomPointX);
    const centerZ = Math.floor(randomPointZ);
    const minDistance = episodeInstance.constructor.INIT_MIN_BOTS_DISTANCE;
    const maxRange = Math.floor(
      episodeInstance.constructor.INIT_MAX_BOTS_DISTANCE / 2,
    );
    const targets = `${bot.username} ${args.other_bot_name}`;
    const cmd = `spreadplayers ${centerX} ${centerZ} ${minDistance} ${maxRange} false @a[tag=minebot]`;
    console.log(`[${bot.username}] spreadplayers command: ${cmd}`);
    let result;
    try {
      result = await rcon.send(cmd);
    } catch (err) {
      if (
        err.message &&
        err.message.includes("Timeout for packet id") &&
        rconTimeoutCount < MAX_RCON_TIMEOUTS
      ) {
        rconTimeoutCount++;
        console.log(
          `[${bot.username}] RCON timeout, retrying (${rconTimeoutCount}/${MAX_RCON_TIMEOUTS})...`,
        );
        await sleep(2500);
        continue;
      } else {
        throw err;
      }
    }
    console.log(`[${bot.username}] spreadplayers result: ${result}`);
    attemptsWithThisRadius++;
    if (!result.startsWith("Spread 2 player")) {
      if (attemptsWithThisRadius >= MAX_ATTEMPTS_WITH_THIS_RADIUS) {
        console.log(
          `[${bot.username}] spreadplayers failed after ${attemptsWithThisRadius} attempts with radius ${bot._teleport_radius}, halving the radius and trying again`,
        );
        bot._teleport_radius /= 2;
        attemptsWithThisRadius = 0;
      } else {
        console.log(`[${bot.username}] spreadplayers failed, trying again`);
      }
      await sleep(1000);
    } else {
      success = true;
      await sleep(5000);
      break;
    }
  }
  if (!success) {
    console.log(
      `[${bot.username}] spreadplayers failed after ${TOTAL_ATTEMPTS} attempts, skipping teleport`,
    );
  }
}

/**
 * Get peer-error phase handler for a specific episode.
 *
 * When the peer reports an error, mark `_peerError` and schedule the stop phase.
 *
 * @param {*} bot - Mineflayer bot instance
 * @param {*} rcon - RCON connection instance
 * @param {Function} sharedBotRng - Shared RNG
 * @param {*} coordinator - Bot coordinator
 * @param {number} episodeNum - Episode number
 * @param {*} episodeInstance - Episode instance
 * @param {Object} args - Configuration args
 * @returns {Function} Peer-error phase handler
 */
function getOnPeerErrorPhaseFn(
  bot,
  rcon,
  sharedBotRng,
  coordinator,
  episodeNum,
  episodeInstance,
  args,
) {
  return async (phaseDataOther) => {
    console.error(
      `[${bot.username}] Received peerErrorPhase_${episodeNum} from peer, stopping.`,
      phaseDataOther["reason"],
    );
    episodeInstance._peerError = true;
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
      `peerErrorPhase_${episodeNum} end`,
    );
  };
}

module.exports = {
  runSingleEpisode,
  getOnSpawnFn,
  getOnTeleportPhaseFn,
  setupBotAndWorldOnce,
  setupCameraPlayerOnce,
  setupBotAndCameraForEpisode,
  // exported for tests / tools
  episodeClassMap,
  episodeRandomTickSpeed,
  expandEpisodeTypes,
  mergeStateMetadata,
};
