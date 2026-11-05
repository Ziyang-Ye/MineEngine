// Modified by Ziyang Ye for MineEngine, 2026 (from SolarisEngine 78f23e7); see capture/MODIFICATIONS.md
const path = require("path");
const minimist = require("minimist");

// Default output directory: /output in the containers, ./output elsewhere on Windows.
const DEFAULT_OUTPUT_DIR =
  process.platform === "win32" ? path.resolve("output") : "/output";

/**
 * Parse command line arguments with default values
 * @returns {Object} Parsed arguments object
 */
function parseArgs() {
  return minimist(process.argv.slice(2), {
    default: {
      host: "127.0.0.1",
      port: 25565,
      rcon_host: "127.0.0.1",
      rcon_port: 25575,
      act_recorder_host: "127.0.0.1",
      act_recorder_port: 8091,
      bot_name: "Alpha",
      other_bot_name: "Bravo",
      coord_port: 8093,
      other_coord_host: "127.0.0.1",
      other_coord_port: 8094,
      bot_rng_seed: "12345",
      episodes_num: 1,
      start_episode_id: 0,
      run_id: 1,
      instance_id: 0,
      output_dir: process.env.OUTPUT_DIR || DEFAULT_OUTPUT_DIR,
      bootstrap_wait_time: 0,
      enable_camera_wait: 1,
      teleport_center_x: 0,
      teleport_center_z: 0,
      teleport_radius: 5,
      walk_timeout: 5, // walk timeout in seconds
      mc_version: process.env.MC_VERSION || "1.21",
      camera_ready_retries: 30,
      camera_ready_check_interval: 2000, // milliseconds
      rcon_password: "research",
      teleport: 1,
      viewer_rendering_disabled: 0,
      viewer_recording_interval: 50,
      smoke_test: 0,
      world_type: "flat",
      eval_time_set_day: 0,
      // ---- stateful capture (see STATEFUL_CAPTURE.md) ----
      world_seed_config: process.env.WORLD_SEED_CONFIG || "",
      world_gen_info: process.env.WORLD_GEN_INFO || "",
      world_state_logging: 1,
      world_snapshot_mode: "blocks",
      world_end_snapshot: 1,
      world_snapshot_radius: 32,
      world_snapshot_down: 24,
      world_snapshot_up: 40,
      world_log_radius: 64,
      world_heartbeat_ticks: 1,
      reset_world_between_episodes: 0,
      deterministic_teleport: 1,
      episode_weight_multipliers: "",
      state_random_tick_speed: 100,
      // ---- per-episode environment (T1) ----
      // fraction of episodes at dusk (12500) or night (18000), else 6000
      dusk_night_fraction: 0.15,
      // -1 = use the random policy above; >= 0 = force this /time set value
      fixed_time: -1,
      // ---- per-tick entity records in the world log (T3) ----
      world_entities_every: 1, // server ticks between `entities` records
      world_entities_radius: 64, // blocks around the tracked players
      // ---- round 2 integration (episodes-loop.js); "" = use the env var
      // of the same name (upper case) or the built-in default ----
      // 1 (default): bot RNG seed = `<bot_rng_seed>_<world_seed_config>`
      bot_rng_seed_salt: "",
      // category shares for the category-first sampler, "build=0.35,..."
      category_weights: "",
      // "" = auto (EPISODE_TYPES=diverse or a category alias), 1 / 0 = force
      category_sampling: "",
      // "auto" (default: 0 in clear weather, 3 when it rains / snows), a
      // number, or -1 = leave the gamerule alone
      episode_random_tick_speed: "",
    },
    string: [
      "world_seed_config",
      "world_gen_info",
      "episode_weight_multipliers",
      "world_snapshot_mode",
      "bot_rng_seed",
      "bot_rng_seed_salt",
      "category_weights",
      "category_sampling",
      "episode_random_tick_speed",
    ],
  });
}

module.exports = {
  parseArgs,
};
