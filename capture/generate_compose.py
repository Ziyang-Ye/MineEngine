#!/usr/bin/env python3
# Modified by Ziyang Ye for MineEngine, 2026 (from SolarisEngine 78f23e7); see capture/MODIFICATIONS.md
"""
Generate multiple Docker Compose configurations for parallel Minecraft data collection.
Each instance will have its own ports and output directories to avoid conflicts.

Enhancements:
- Support generating a mix of flatland and normal worlds via
  `--num_flatland_world` and `--num_normal_world`.
- Add `--viewer_rendering_disabled` (default: 1) applied to act_recorder/controller.
- Keep `--bootstrap_wait_time` unchanged as an argument.
"""

import argparse
import json
import os
import re
import time
from pathlib import Path
from typing import Optional

import yaml


def absdir(path: str) -> str:
    """Return an absolute path.

    If `path` is relative, it's resolved against the current working directory.
    """
    if os.path.isabs(path):
        return path
    return os.path.abspath(os.path.join(os.getcwd(), path))


def camera_paths(
    instance_id: int,
    alpha_base: str,
    bravo_base: str,
    data_alpha_base: str,
    data_bravo_base: str,
) -> dict:
    return {
        "alpha_output_host": os.path.join(alpha_base, str(instance_id)),
        "bravo_output_host": os.path.join(bravo_base, str(instance_id)),
        "alpha_data_host": os.path.join(data_alpha_base, str(instance_id)),
        "bravo_data_host": os.path.join(data_bravo_base, str(instance_id)),
    }


def camera_ports(
    instance_id: int,
    alpha_vnc_base: int,
    alpha_novnc_base: int,
    bravo_vnc_base: int,
    bravo_novnc_base: int,
    display_base: int,
    vnc_step: int,
    display_step: int,
) -> dict:
    return {
        "alpha_vnc": alpha_vnc_base + vnc_step * instance_id,
        "alpha_novnc": alpha_novnc_base + vnc_step * instance_id,
        "bravo_vnc": bravo_vnc_base + vnc_step * instance_id,
        "bravo_novnc": bravo_novnc_base + vnc_step * instance_id,
        "alpha_display": f":{display_base + display_step * instance_id}",
        "bravo_display": f":{display_base + display_step * instance_id + 1}",
    }


# Flat-world presets (round 2, world diversity). Each preset is
# (biome, surface_block): the biome sets grass/foliage/water/sky colours and
# what falls when it rains (rain, snow or nothing); the surface and the
# layers under it come from FLAT_TERRAIN_LAYERS. Every preset keeps the
# original total height (bedrock 1 + 127 = 128 layers, surface at y = 63), so
# flat worlds stand at the same height as before.
FLAT_TERRAIN_OPTIONS = [
    ("plains", "grass_block"),
    ("sunflower_plains", "grass_block"),
    ("meadow", "grass_block"),
    ("flower_forest", "grass_block"),
    ("cherry_grove", "grass_block"),
    ("birch_forest", "grass_block"),
    ("dark_forest", "grass_block"),
    ("old_growth_pine_taiga", "podzol"),
    ("jungle", "grass_block"),
    ("swamp", "grass_block"),
    ("mangrove_swamp", "grass_block"),
    ("windswept_hills", "grass_block"),
    ("savanna", "grass_block"),
    ("savanna_plateau", "coarse_dirt"),
    ("desert", "sand"),
    ("beach", "sand"),
    ("badlands", "red_sand"),
    ("wooded_badlands", "coarse_dirt"),
    ("eroded_badlands", "orange_terracotta"),
    ("mushroom_fields", "mycelium"),
    ("snowy_plains", "snow"),
    ("snowy_taiga", "snow"),
    ("ice_spikes", "snow_block"),
    ("stony_shore", "stone"),
    ("windswept_gravelly_hills", "gravel"),
]

# Sampling weight per preset (default 1.0).
FLAT_TERRAIN_WEIGHTS = {
    ("plains", "grass_block"): 3.0,
    ("desert", "sand"): 2.0,
    ("snowy_plains", "snow"): 2.0,
    ("savanna", "grass_block"): 1.5,
    ("swamp", "grass_block"): 1.5,
    ("badlands", "red_sand"): 1.5,
    ("stony_shore", "stone"): 0.5,
    ("windswept_gravelly_hills", "gravel"): 0.5,
}

# Layers above the bedrock, top first: (block, height). The stone filler
# below them is added by generate_terrain_settings (total 127).
FLAT_TERRAIN_LAYERS = {
    ("old_growth_pine_taiga", "podzol"): [("podzol", 1), ("dirt", 2)],
    ("mangrove_swamp", "grass_block"): [("grass_block", 1), ("mud", 2), ("dirt", 1)],
    ("savanna", "grass_block"): [("grass_block", 1), ("coarse_dirt", 1), ("dirt", 2)],
    ("savanna_plateau", "coarse_dirt"): [("coarse_dirt", 1), ("dirt", 2)],
    ("desert", "sand"): [("sand", 2), ("sandstone", 3)],
    ("beach", "sand"): [("sand", 3), ("sandstone", 2)],
    ("badlands", "red_sand"): [("red_sand", 1), ("orange_terracotta", 1), ("terracotta", 3)],
    ("wooded_badlands", "coarse_dirt"): [("coarse_dirt", 1), ("terracotta", 2), ("yellow_terracotta", 1), ("terracotta", 1)],
    ("eroded_badlands", "orange_terracotta"): [
        ("orange_terracotta", 1),
        ("white_terracotta", 1),
        ("terracotta", 1),
        ("yellow_terracotta", 1),
        ("red_terracotta", 1),
    ],
    ("mushroom_fields", "mycelium"): [("mycelium", 1), ("dirt", 3)],
    ("snowy_plains", "snow"): [("snow", 1), ("snow_block", 2), ("dirt", 2)],
    ("snowy_taiga", "snow"): [("snow", 1), ("grass_block", 1), ("dirt", 2)],
    ("ice_spikes", "snow_block"): [("snow_block", 2), ("packed_ice", 1), ("dirt", 2)],
    ("stony_shore", "stone"): [("stone", 1), ("andesite", 1), ("gravel", 1), ("stone", 1)],
    ("windswept_gravelly_hills", "gravel"): [("gravel", 1), ("stone", 2)],
}

FLAT_TOTAL_LAYERS = 128  # bedrock + everything above it

# Choices made by generate_compose_config (with the instance seed) so that a
# later call for the same instance without a seed returns the same preset.
_FLAT_CHOICE_CACHE = {}


def generate_terrain_settings(biome, surface_block):
    """Generate terrain settings JSON for flat world generation.

    Layers come from FLAT_TERRAIN_LAYERS for known (biome, surface) presets,
    else the original layering (stone, 2 dirt, surface). Stone fills the
    rest so the total height is always FLAT_TOTAL_LAYERS.
    """
    top = FLAT_TERRAIN_LAYERS.get((biome, surface_block))
    if top is None:
        top = [(surface_block, 1), ("dirt", 2)]
    used = 1 + sum(h for _, h in top)
    layers = [
        {"block": "minecraft:bedrock", "height": 1},
        {"block": "minecraft:stone", "height": FLAT_TOTAL_LAYERS - used},
    ]
    # Bottom to top, as the flat generator expects.
    for block, height in reversed(top):
        layers.append({"block": f"minecraft:{block}", "height": height})
    terrain_settings = {
        "layers": layers,
        "biome": f"minecraft:{biome}",
    }
    return terrain_settings


def _flat_preset_index(key: str) -> int:
    """Deterministic weighted preset index for a key (sha256, not hash())."""
    import hashlib

    digest = hashlib.sha256(key.encode("utf-8")).digest()
    u = int.from_bytes(digest[:8], "big") / float(2**64)
    weights = [FLAT_TERRAIN_WEIGHTS.get(opt, 1.0) for opt in FLAT_TERRAIN_OPTIONS]
    x = u * sum(weights)
    for i, w in enumerate(weights):
        x -= w
        if x < 0:
            return i
    return len(FLAT_TERRAIN_OPTIONS) - 1


def flat_terrain_for_instance(instance_id: int, seed=None):
    """Return (biome, surface_block) used for the flat world of this instance.

    With a seed (generate_compose_config passes the instance's level seed,
    which includes the batch time unless --world_seed is fixed) the preset is
    a weighted, deterministic function of (seed, instance_id), so it varies
    per batch and per instance. Without a seed the choice made earlier for
    this instance is returned (same process), else the old round-robin by
    instance id. FLAT_PRESET=<biome>[:<surface>] forces a preset.
    """
    forced = os.environ.get("FLAT_PRESET", "").strip()
    if forced:
        biome, _, surface = forced.partition(":")
        biome = biome.replace("minecraft:", "")
        if not surface:
            surface = next((s for b, s in FLAT_TERRAIN_OPTIONS if b == biome), "grass_block")
        choice = (biome, surface.replace("minecraft:", ""))
    elif seed is not None and str(seed) != "":
        choice = FLAT_TERRAIN_OPTIONS[_flat_preset_index(f"{seed}|{int(instance_id)}")]
    elif int(instance_id) in _FLAT_CHOICE_CACHE:
        return _FLAT_CHOICE_CACHE[int(instance_id)]
    else:
        choice = FLAT_TERRAIN_OPTIONS[instance_id % len(FLAT_TERRAIN_OPTIONS)]
    _FLAT_CHOICE_CACHE[int(instance_id)] = choice
    return choice


def java_seed_value(seed: str) -> int:
    """Numeric seed the 1.21 server derives from a level-seed string.

    Mirrors WorldOptions.parseSeed: a string that parses as a signed 64-bit
    integer is used as-is, anything else uses Java's String.hashCode().
    The authoritative value is still the one the server reports (RCON `seed`,
    episode_info.json -> world.seed, world_events.jsonl -> episode_begin).
    """
    s = str(seed).strip()
    try:
        v = int(s)
        if -(2**63) <= v < 2**63:
            return v
    except ValueError:
        pass
    h = 0
    for ch in s:
        h = (31 * h + ord(ch)) & 0xFFFFFFFF
    return h - 2**32 if h >= 2**31 else h


def generate_compose_config(
    instance_id,
    base_port,
    base_rcon_port,
    act_recorder_port,
    coord_port,
    data_dir_base,
    output_dir,
    num_episodes,
    episode_start_id,
    bootstrap_wait_time,
    episode_category,
    episode_types,
    smoke_test,
    viewer_rendering_disabled,
    world_type,
    render_distance,
    simulation_distance,
    graphics_mode,
    # camera specific
    camera_output_alpha_base,
    camera_output_bravo_base,
    camera_data_alpha_base,
    camera_data_bravo_base,
    camera_alpha_vnc_base,
    camera_alpha_novnc_base,
    camera_bravo_vnc_base,
    camera_bravo_novnc_base,
    display_base,
    vnc_step,
    display_step,
    # CPU pinning
    cpuset: Optional[str] = None,
    cpuset_camera_alpha: Optional[str] = None,
    cpuset_camera_bravo: Optional[str] = None,
    # GPU settings
    gpu_device_id: Optional[int] = None,
    gpu_mode: str = "egl",
    disable_nvenc: bool = False,
    # Eval options
    eval_time_set_day: int = 0,
    # Flatland options
    flatland_world_disable_structures: bool = False,
    # Stateful-capture options (see STATEFUL_CAPTURE.md)
    world_seed: Optional[str] = None,
    eula: str = "FALSE",
    world_state: Optional[dict] = None,
    teleport_radius: int = 50000,
    server_memory: str = "",
    nvidia_disable_require: bool = False,
):
    """Generate a Docker Compose configuration for a single instance."""
    world_state = dict(world_state or {})

    # Calculate ports for this instance
    mc_port = base_port + instance_id
    rcon_port = base_rcon_port + instance_id

    # Directories - each instance gets its own data subdirectory
    data_dir = f"{data_dir_base}/{instance_id}"

    cam_paths = camera_paths(
        instance_id,
        camera_output_alpha_base,
        camera_output_bravo_base,
        camera_data_alpha_base,
        camera_data_bravo_base,
    )
    cam_ports = camera_ports(
        instance_id,
        camera_alpha_vnc_base,
        camera_alpha_novnc_base,
        camera_bravo_vnc_base,
        camera_bravo_novnc_base,
        display_base,
        vnc_step,
        display_step,
    )

    project_root = str(Path(__file__).resolve().parent)

    plugin_starter_package_json_host = os.path.join(project_root, "server", "plugin-starter", "package.json")
    # If the only episode type is turnToLookEval, use the fixed seed "solaris"
    if episode_types == "turnToLookEval" or episode_types == "turnToLookOppositeEval":
        seed = "solaris"
        print(
            f"turnToLookEval episode type passsed. Using fixed seed 'solaris' for all instances."
        )
    elif world_seed is not None and str(world_seed) != "":
        # Fixed seed requested on the command line (same seed for every instance)
        seed = str(world_seed)
    else:
        seed = str(instance_id) + str(int(time.time()))

    is_flat = str(world_type).lower() == "flat"
    if is_flat:
        biome, surface_block = flat_terrain_for_instance(instance_id, seed)
        generator_settings = generate_terrain_settings(biome, surface_block)
    else:
        biome, surface_block, generator_settings = None, None, None
    world_gen_info = {
        "seed_config": seed,
        "seed_value_expected": java_seed_value(seed),
        "world_type": str(world_type).lower(),
        "level_type": "minecraft:flat" if is_flat else "minecraft:normal",
        "generator_settings": generator_settings,
        "flat_biome": biome,
        "flat_surface_block": surface_block,
        "generate_structures": not (is_flat and flatland_world_disable_structures),
        "server_version": "1.21",
        "server_type": "PAPER",
    }
    world_gen_info_json = json.dumps(world_gen_info, separators=(",", ":"))
    # Controller-side world-state settings (passed as env vars)
    world_state_env = {
        "WORLD_SEED_CONFIG": seed,
        "WORLD_GEN_INFO": world_gen_info_json,
        "WORLD_STATE_LOGGING": int(world_state.get("logging", 1)),
        "WORLD_SNAPSHOT_MODE": str(world_state.get("snapshot_mode", "blocks")),
        "WORLD_END_SNAPSHOT": int(world_state.get("end_snapshot", 1)),
        "WORLD_SNAPSHOT_RADIUS": int(world_state.get("snapshot_radius", 32)),
        "WORLD_SNAPSHOT_DOWN": int(world_state.get("snapshot_down", 24)),
        "WORLD_SNAPSHOT_UP": int(world_state.get("snapshot_up", 40)),
        "WORLD_LOG_RADIUS": int(world_state.get("log_radius", 64)),
        "WORLD_HEARTBEAT_TICKS": int(world_state.get("heartbeat_ticks", 1)),
        "RESET_WORLD_BETWEEN_EPISODES": int(world_state.get("reset_between_episodes", 0)),
        "DETERMINISTIC_TELEPORT": int(world_state.get("deterministic_teleport", 1)),
        "EPISODE_WEIGHT_MULTIPLIERS": str(world_state.get("episode_weight_multipliers", "")),
        "STATE_RANDOM_TICK_SPEED": int(world_state.get("state_random_tick_speed", 100)),
    }
    config = {
        "networks": {f"mc_network_{instance_id}": {"driver": "bridge"}},
        "services": {
            f"prep_data_instance_{instance_id}": {
                "image": "busybox:latest",
                "command": [
                    "sh",
                    "-c",
                    "mkdir -p /data /data/plugins /data/skins && "
                    "chmod 777 /data /data/plugins /data/skins && "
                    'if [ -d /source_plugins ] && [ -n "$(ls -A /source_plugins 2>/dev/null)" ]; then '
                    "  cp -r /source_plugins/. /data/plugins/; "
                    "fi; "
                    'if [ -d /source_skins ] && [ -n "$(ls -A /source_skins 2>/dev/null)" ]; then '
                    "  cp -r /source_skins/. /data/skins/; "
                    "fi; "
                    "chmod -R 777 /data/plugins /data/skins",
                ],
                "volumes": [
                    f"{data_dir}:/data",
                    f"{project_root}/server/plugins:/source_plugins:ro",
                    f"{project_root}/server/skins:/source_skins:ro",
                ],
                "restart": "no",
            },
            f"mc_instance_{instance_id}": {
                "depends_on": {
                    f"prep_data_instance_{instance_id}": {
                        "condition": "service_completed_successfully"
                    }
                },
                "image": "itzg/minecraft-server:java21",
                "tty": True,
                "network_mode": "host",
                **({"cpuset": cpuset} if cpuset else {}),
                "environment": (
                    lambda: {
                        # Base server env, common to both normal and flat worlds
                        # EULA value comes from --eula (default FALSE; run.sh passes TRUE only
                        # with --accept-minecraft-eula): TRUE means the operator accepts
                        # Mojang's EULA for this server.
                        "EULA": str(eula).upper(),
                        "VERSION": "1.21",
                        "TYPE": "PAPER",
                        "MODE": "survival",
                        "RCON_PORT": rcon_port,
                        "SERVER_PORT": mc_port,
                        "ALLOW_FLIGHT": True,
                        "ONLINE_MODE": False,
                        "SPAWN_PROTECTION": 0,
                        "SEED": seed,
                        "ENFORCE_SECURE_PROFILE": False,
                        "RCON_PASSWORD": "research",
                        "BROADCAST_RCON_TO_OPS": True,
                        # The MirrorBot plugin writes world_events.jsonl / snapshots here
                        "WORLD_STATE_OUTPUT_DIR": "/episode_output",
                        **({"MEMORY": server_memory} if server_memory else {}),
                        **(
                            {
                                "LEVEL_TYPE": "minecraft:flat",
                                "GENERATOR_SETTINGS": "TERRAIN_SETTINGS_PLACEHOLDER",
                                **(
                                    {"GENERATE_STRUCTURES": "false"}
                                    if flatland_world_disable_structures
                                    else {}
                                ),
                            }
                            if str(world_type).lower() == "flat"
                            else {}
                        ),
                    }
                )(),
                "volumes": [f"{data_dir}:/data", f"{output_dir}:/episode_output"],
                "healthcheck": {
                    "test": [
                        "CMD-SHELL",
                        f"mc-monitor status --host localhost --port {mc_port}",
                    ],
                    "interval": "10s",
                    "timeout": "5s",
                    "retries": 12,
                },
            },
            f"controller_alpha_instance_{instance_id}": {
                "image": "solaris-engine-base:latest",
                "build": {
                    "context": project_root,
                    "dockerfile": "Dockerfile",
                },
                "depends_on": {
                    f"mc_instance_{instance_id}": {"condition": "service_healthy"},
                    f"act_recorder_alpha_instance_{instance_id}": {
                        "condition": "service_started"
                    },
                },
                **({"cpuset": cpuset} if cpuset else {}),
                "volumes": [f"{output_dir}:/output"],
                "environment": {
                    "BOT_NAME": "Alpha",
                    "OTHER_BOT_NAME": "Bravo",
                    "ACT_RECORDER_HOST": f"act_recorder_alpha_instance_{instance_id}",
                    "ACT_RECORDER_PORT": act_recorder_port,
                    "COORD_PORT": coord_port,
                    "OTHER_COORD_HOST": f"controller_bravo_instance_{instance_id}",
                    "OTHER_COORD_PORT": coord_port,
                    "BOT_RNG_SEED": str(12345 + instance_id),
                    "EPISODES_NUM": num_episodes,
                    "EPISODE_START_ID": episode_start_id,
                    "EPISODE_TYPES": episode_types,
                    "MC_HOST": "host.docker.internal",
                    "MC_PORT": mc_port,
                    "RCON_HOST": "host.docker.internal",
                    "RCON_PORT": rcon_port,
                    "RCON_PASSWORD": "research",
                    "BOOTSTRAP_WAIT_TIME": bootstrap_wait_time,
                    "ENABLE_CAMERA_WAIT": 1,
                    "CAMERA_READY_RETRIES": 300,
                    "CAMERA_READY_CHECK_INTERVAL": 2000,
                    "MC_VERSION": "1.21",
                    "VIEWER_RENDERING_DISABLED": viewer_rendering_disabled,
                    "VIEWER_RECORDING_INTERVAL": 50,
                    "WALK_TIMEOUT": 5,
                    "TELEPORT": 1,
                    "TELEPORT_RADIUS": teleport_radius,
                    "WORLD_TYPE": str(world_type).lower(),
                    "SMOKE_TEST": smoke_test,
                    "INSTANCE_ID": instance_id,
                    "OUTPUT_DIR": "/output",
                    "EVAL_TIME_SET_DAY": eval_time_set_day,
                    **world_state_env,
                },
                "extra_hosts": ["host.docker.internal:host-gateway"],
                "networks": [f"mc_network_{instance_id}"],
                "command": "./controller/entrypoint.sh",
            },
            f"controller_bravo_instance_{instance_id}": {
                "image": "solaris-engine-base:latest",
                "build": {
                    "context": project_root,
                    "dockerfile": "Dockerfile",
                },
                "depends_on": {
                    f"mc_instance_{instance_id}": {"condition": "service_healthy"},
                    f"act_recorder_bravo_instance_{instance_id}": {
                        "condition": "service_started"
                    },
                    f"controller_alpha_instance_{instance_id}": {
                        "condition": "service_started"
                    },
                },
                **({"cpuset": cpuset} if cpuset else {}),
                "volumes": [f"{output_dir}:/output"],
                "environment": {
                    "BOT_NAME": "Bravo",
                    "OTHER_BOT_NAME": "Alpha",
                    "ACT_RECORDER_HOST": f"act_recorder_bravo_instance_{instance_id}",
                    "ACT_RECORDER_PORT": act_recorder_port,
                    "COORD_PORT": coord_port,
                    "OTHER_COORD_HOST": f"controller_alpha_instance_{instance_id}",
                    "OTHER_COORD_PORT": coord_port,
                    "BOT_RNG_SEED": str(12345 + instance_id),
                    "EPISODES_NUM": num_episodes,
                    "EPISODE_START_ID": episode_start_id,
                    "EPISODE_TYPES": episode_types,
                    "MC_HOST": "host.docker.internal",
                    "MC_PORT": mc_port,
                    "RCON_HOST": "host.docker.internal",
                    "RCON_PORT": rcon_port,
                    "RCON_PASSWORD": "research",
                    "BOOTSTRAP_WAIT_TIME": bootstrap_wait_time,
                    "ENABLE_CAMERA_WAIT": 1,
                    "CAMERA_READY_RETRIES": 300,
                    "CAMERA_READY_CHECK_INTERVAL": 2000,
                    "MC_VERSION": "1.21",
                    "VIEWER_RENDERING_DISABLED": viewer_rendering_disabled,
                    "VIEWER_RECORDING_INTERVAL": 50,
                    "WALK_TIMEOUT": 5,
                    "TELEPORT": 1,
                    "TELEPORT_RADIUS": 250,
                    "WORLD_TYPE": str(world_type).lower(),
                    "SMOKE_TEST": smoke_test,
                    "INSTANCE_ID": instance_id,
                    "OUTPUT_DIR": "/output",
                    "EVAL_TIME_SET_DAY": eval_time_set_day,
                    **world_state_env,
                },
                "extra_hosts": ["host.docker.internal:host-gateway"],
                "networks": [f"mc_network_{instance_id}"],
                "command": "./controller/entrypoint.sh",
            },
            f"act_recorder_alpha_instance_{instance_id}": {
                "image": "solaris-engine-base:latest",
                "environment": {
                    "PORT": act_recorder_port,
                    "NAME": "Alpha",
                    "INSTANCE_ID": instance_id,
                    "EPISODE_START_ID": episode_start_id,
                    "VIEWER_RENDERING_DISABLED": viewer_rendering_disabled,
                    "WORLD_SEED_CONFIG": seed,
                    "WORLD_GEN_INFO": world_gen_info_json,
                },
                "tty": True,
                **({"cpuset": cpuset} if cpuset else {}),
                "volumes": [f"{output_dir}:/output"],
                "networks": [f"mc_network_{instance_id}"],
                "command": "./controller/act_recorder/entrypoint.sh",
            },
            f"act_recorder_bravo_instance_{instance_id}": {
                "image": "solaris-engine-base:latest",
                "environment": {
                    "PORT": act_recorder_port,
                    "NAME": "Bravo",
                    "INSTANCE_ID": instance_id,
                    "EPISODE_START_ID": episode_start_id,
                    "VIEWER_RENDERING_DISABLED": viewer_rendering_disabled,
                    "WORLD_SEED_CONFIG": seed,
                    "WORLD_GEN_INFO": world_gen_info_json,
                },
                "tty": True,
                **({"cpuset": cpuset} if cpuset else {}),
                "volumes": [f"{output_dir}:/output"],
                "networks": [f"mc_network_{instance_id}"],
                "command": "./controller/act_recorder/entrypoint.sh",
            },
            # Camera alpha: recording client
            f"camera_alpha_instance_{instance_id}": {
                "image": "solaris-engine-camera:latest",
                "build": {
                    "context": os.path.join(project_root, "camera"),
                    "dockerfile": "Dockerfile",
                },
                "restart": "unless-stopped",
                "network_mode": "host",
                **({"cpuset": cpuset_camera_alpha} if cpuset_camera_alpha else {}),
                "depends_on": {
                    f"mc_instance_{instance_id}": {"condition": "service_healthy"}
                },
                "environment": {
                    "MC_VERSION": "1.21",
                    "MC_HOST": "127.0.0.1",
                    "MC_PORT": mc_port,
                    "CAMERA_NAME": "CameraAlpha",
                    "DISPLAY": cam_ports["alpha_display"],
                    "VNC_PORT": str(cam_ports["alpha_vnc"]),
                    "NOVNC_PORT": str(cam_ports["alpha_novnc"]),
                    "WIDTH": "1280",
                    "HEIGHT": "720",
                    "FPS": "20",
                    "VNC_PASSWORD": "research",
                    "ENABLE_RECORDING": "1",
                    "RECORDING_PATH": "/output/camera_alpha.mkv",
                    "DISABLE_NVENC": "1" if disable_nvenc else "0",
                    "RENDER_DISTANCE": render_distance,
                    "SIMULATION_DISTANCE": simulation_distance,
                    "GRAPHICS_MODE": graphics_mode,
                    **(
                        {
                            "NVIDIA_DRIVER_CAPABILITIES": "all",
                            "NVIDIA_VISIBLE_DEVICES": gpu_device_id,
                        }
                    ),
                    # The camera image is based on nvidia/cuda:13.0 but only uses
                    # EGL + NVENC; this skips the "cuda>=13.0" driver check so it
                    # also starts on older (e.g. 550/570) laptop drivers.
                    **({"NVIDIA_DISABLE_REQUIRE": "1"} if nvidia_disable_require else {}),
                },
                "runtime": "nvidia",
                "volumes": [
                    f"{cam_paths['alpha_data_host']}:/root",
                    f"{cam_paths['alpha_output_host']}:/output",
                ],
            },
            # Plugin starter: waits for all players then triggers episode start
            f"plugin_starter_instance_{instance_id}": {
                "image": "node:20",
                "network_mode": "host",
                **({"cpuset": cpuset} if cpuset else {}),
                "depends_on": {
                    f"mc_instance_{instance_id}": {"condition": "service_healthy"},
                    f"camera_alpha_instance_{instance_id}": {
                        "condition": "service_started"
                    },
                },
                "working_dir": "/app",
                "environment": {
                    "RCON_HOST": "127.0.0.1",
                    "RCON_PORT": rcon_port,
                    "RCON_PASSWORD": "research",
                    "EPISODE_START_RETRIES": "300",
                    "EPISODE_REQUIRED_PLAYERS": "Alpha,CameraAlpha,Bravo,CameraBravo,SpectatorAlpha,SpectatorBravo",
                    "EPISODE_START_COMMAND": "episode start Alpha CameraAlpha session_a.png Bravo CameraBravo session_b.png",
                },
                "volumes": [
                    f"{os.path.join(project_root, 'server', 'plugin-starter', 'plugin_starter.js')}:/app/plugin_starter.js:ro",
                    f"{plugin_starter_package_json_host}:/app/package.json:ro",
                ],
                "command": [
                    "sh",
                    "-c",
                    "npm install --omit=dev --no-progress && node plugin_starter.js",
                ],
            },
            # Camera bravo: recording client
            f"camera_bravo_instance_{instance_id}": {
                "image": "solaris-engine-camera:latest",
                "build": {
                    "context": os.path.join(project_root, "camera"),
                    "dockerfile": "Dockerfile",
                },
                "restart": "unless-stopped",
                "network_mode": "host",
                **({"cpuset": cpuset_camera_bravo} if cpuset_camera_bravo else {}),
                "depends_on": {
                    f"mc_instance_{instance_id}": {"condition": "service_healthy"}
                },
                "environment": {
                    "MC_VERSION": "1.21",
                    "MC_HOST": "127.0.0.1",
                    "MC_PORT": mc_port,
                    "CAMERA_NAME": "CameraBravo",
                    "DISPLAY": cam_ports["bravo_display"],
                    "VNC_PORT": str(cam_ports["bravo_vnc"]),
                    "NOVNC_PORT": str(cam_ports["bravo_novnc"]),
                    "WIDTH": "1280",
                    "HEIGHT": "720",
                    "FPS": "20",
                    "VNC_PASSWORD": "research",
                    "ENABLE_RECORDING": "1",
                    "RECORDING_PATH": "/output/camera_bravo.mkv",
                    "DISABLE_NVENC": "1" if disable_nvenc else "0",
                    "RENDER_DISTANCE": render_distance,
                    "SIMULATION_DISTANCE": simulation_distance,
                    "GRAPHICS_MODE": graphics_mode,
                    **(
                        {
                            "NVIDIA_DRIVER_CAPABILITIES": "all",
                            "NVIDIA_VISIBLE_DEVICES": gpu_device_id,
                        }
                    ),
                    # The camera image is based on nvidia/cuda:13.0 but only uses
                    # EGL + NVENC; this skips the "cuda>=13.0" driver check so it
                    # also starts on older (e.g. 550/570) laptop drivers.
                    **({"NVIDIA_DISABLE_REQUIRE": "1"} if nvidia_disable_require else {}),
                },
                "runtime": "nvidia",
                "volumes": [
                    f"{cam_paths['bravo_data_host']}:/root",
                    f"{cam_paths['bravo_output_host']}:/output",
                ],
            },
            # Passive spectator alpha
            f"spectator_alpha_instance_{instance_id}": {
                "image": "solaris-engine-base:latest",
                "build": {
                    "context": project_root,
                    "dockerfile": "Dockerfile",
                },
                "restart": "unless-stopped",
                **({"cpuset": cpuset} if cpuset else {}),
                "depends_on": {
                    f"mc_instance_{instance_id}": {"condition": "service_healthy"}
                },
                "working_dir": "/usr/src/app",
                "environment": {
                    "MC_HOST": "host.docker.internal",
                    "MC_PORT": mc_port,
                    "MC_USERNAME": "SpectatorAlpha",
                },
                "extra_hosts": [
                    "host.docker.internal:host-gateway",
                ],
                "networks": [f"mc_network_{instance_id}"],
                "command": ["node", "spectator/spectator.js"],
            },
            # Passive spectator bravo
            f"spectator_bravo_instance_{instance_id}": {
                "image": "solaris-engine-base:latest",
                "build": {
                    "context": project_root,
                    "dockerfile": "Dockerfile",
                },
                "restart": "unless-stopped",
                **({"cpuset": cpuset} if cpuset else {}),
                "depends_on": {
                    f"mc_instance_{instance_id}": {"condition": "service_healthy"}
                },
                "working_dir": "/usr/src/app",
                "environment": {
                    "MC_HOST": "host.docker.internal",
                    "MC_PORT": mc_port,
                    "MC_USERNAME": "SpectatorBravo",
                },
                "extra_hosts": [
                    "host.docker.internal:host-gateway",
                ],
                "networks": [f"mc_network_{instance_id}"],
                "command": ["node", "spectator/spectator.js"],
            },
        },
    }

    return config


def main():
    parser = argparse.ArgumentParser(
        description="Generate Docker Compose files for parallel Minecraft data "
        "collection"
    )
    parser.add_argument(
        "--instances",
        type=int,
        default=15,
        help="Number of instances to generate (default: 32)",
    )
    # World-split counts: if provided (>0), overrides --instances
    parser.add_argument(
        "--num_flatland_world",
        type=int,
        default=0,
        help="Number of flatland-world instances to generate (default: 0)",
    )
    parser.add_argument(
        "--num_normal_world",
        type=int,
        default=0,
        help="Number of normal-world instances to generate (default: 0)",
    )
    parser.add_argument(
        "--compose_dir",
        default="compose_configs",
        help="Directory to store generated compose files",
    )
    parser.add_argument(
        "--base_port",
        type=int,
        default=25565,
        help="Base Minecraft server port (default: 25565)",
    )
    parser.add_argument(
        "--base_rcon_port",
        type=int,
        default=25675,
        help="Base RCON port (default: 25675)",
    )
    parser.add_argument(
        "--act_recorder_port",
        type=int,
        default=8090,
        help="Act recorder port for bridge network services (default: 8090)",
    )
    parser.add_argument(
        "--coord_port",
        type=int,
        default=8100,
        help="Coordination port for bridge network services (default: 8100)",
    )
    parser.add_argument(
        "--data_dir",
        required=True,
        help="Base directory for instance data directories (default: ./data)",
    )
    parser.add_argument(
        "--output_dir",
        default="./output",
        required=True,
        help="Shared output directory for all instances (default: ./output)",
    )
    # Camera-specific bases (absolute paths)
    parser.add_argument(
        "--camera_output_alpha_base",
        required=True,
        help="Absolute base dir for per-instance Camera Alpha outputs (e.g., /abs/.../camera/output_alpha)",
    )
    parser.add_argument(
        "--camera_output_bravo_base",
        required=True,
        help="Absolute base dir for per-instance Camera Bravo outputs (e.g., /abs/.../camera/output_bravo)",
    )
    parser.add_argument(
        "--camera_data_alpha_base",
        default=None,
        help="Absolute base dir for per-instance Camera Alpha data (default: <project>/camera/data_alpha)",
    )
    parser.add_argument(
        "--camera_data_bravo_base",
        default=None,
        help="Absolute base dir for per-instance Camera Bravo data (default: <project>/camera/data_bravo)",
    )
    parser.add_argument("--camera_alpha_vnc_base", type=int, default=5901)
    parser.add_argument("--camera_alpha_novnc_base", type=int, default=6901)
    parser.add_argument("--camera_bravo_vnc_base", type=int, default=5902)
    parser.add_argument("--camera_bravo_novnc_base", type=int, default=6902)
    parser.add_argument("--display_base", type=int, default=99)
    parser.add_argument("--vnc_step", type=int, default=2)
    parser.add_argument("--display_step", type=int, default=2)
    parser.add_argument(
        "--num_episodes",
        type=int,
        default=5,
        help="Number of episodes to run (default: 5)",
    )
    parser.add_argument(
        "--episode_start_id",
        type=int,
        default=0,
        help="Starting episode ID (default: 0)",
    )
    parser.add_argument(
        "--bootstrap_wait_time",
        type=int,
        default=60,
        help="Bootstrap wait time (default: 60)",
    )
    parser.add_argument(
        "--episode_category",
        default="look",
        help="Episode category (default: look)",
    )
    parser.add_argument(
        "--episode_types",
        default="all",
        help="Comma-separated episode types to run (default: all)",
    )

    parser.add_argument(
        "--viewer_rendering_disabled",
        type=int,
        choices=[0, 1],
        default=1,
        help="Disable viewer rendering for act_recorder/controller (default: 1)",
    )
    parser.add_argument(
        "--smoke_test",
        type=int,
        default=0,
        choices=[0, 1],
        help="Enable smoke test mode to run all episode types (default: 0)",
    )
    parser.add_argument(
        "--eval_time_set_day",
        type=int,
        default=0,
        choices=[0, 1],
        help="Set time to day at the start of eval episodes (default: 0)",
    )
    parser.add_argument(
        "--flatland_world_disable_structures",
        type=int,
        default=0,
        choices=[0, 1],
        help="Disable structure generation for flatland worlds (default: 0)",
    )
    # ---- stateful-capture options (see STATEFUL_CAPTURE.md) ----------------------
    parser.add_argument(
        "--world_seed",
        type=str,
        default=None,
        help="Fix the world seed for every instance (default: <instance_id><unix_time>). "
        "Numeric strings are used as the 64-bit seed; other strings are hashed by the server.",
    )
    parser.add_argument(
        "--eula",
        type=str,
        default="FALSE",
        help="Value of the server container's EULA variable (default: FALSE). "
        "TRUE means you accept the Minecraft EULA (https://aka.ms/MinecraftEULA) for your server.",
    )
    parser.add_argument(
        "--world_state_logging",
        type=int,
        default=1,
        choices=[0, 1],
        help="Write per-episode world_events.jsonl + block snapshots via the server plugin (default: 1)",
    )
    parser.add_argument(
        "--world_snapshot_mode",
        type=str,
        default="blocks",
        choices=["blocks", "region", "both", "none"],
        help="Initial-state snapshot: compact block list (blocks), .mca region files copied "
        "after save-all flush (region), both, or none (default: blocks)",
    )
    parser.add_argument(
        "--world_end_snapshot",
        type=int,
        default=1,
        choices=[0, 1],
        help="Also write a block snapshot of the same box at episode end (default: 1)",
    )
    parser.add_argument("--world_snapshot_radius", type=int, default=32,
                        help="Half-width in blocks of the snapshot box around the episode centre (default: 32)")
    parser.add_argument("--world_snapshot_down", type=int, default=24,
                        help="Blocks below the lowest player included in the snapshot box (default: 24)")
    parser.add_argument("--world_snapshot_up", type=int, default=40,
                        help="Blocks above the highest player included in the snapshot box (default: 40)")
    parser.add_argument("--world_log_radius", type=int, default=64,
                        help="Log block/entity events within this horizontal distance of a player or the box; "
                        "-1 = whole world (default: 64)")
    parser.add_argument("--world_heartbeat_ticks", type=int, default=1,
                        help="Write a 'tick' record (game_time, epoch_ms, server-side player pose) every N ticks (default: 1)")
    parser.add_argument(
        "--reset_world_between_episodes",
        type=int,
        default=0,
        choices=[0, 1],
        help="After each episode restore the snapshot box to its initial blocks and remove entities "
        "spawned during the episode (default: 0 = upstream behaviour, builds persist)",
    )
    parser.add_argument(
        "--deterministic_teleport",
        type=int,
        default=1,
        choices=[0, 1],
        help="Draw the per-episode teleport target from a seeded RNG instead of Math.random (default: 1)",
    )
    parser.add_argument(
        "--teleport_radius",
        type=int,
        default=50000,
        help="Radius around the first spawn in which episode locations are drawn (default: 50000). "
        "Smaller values mean less chunk generation, which helps on laptops.",
    )
    parser.add_argument(
        "--episode_weight_multipliers",
        type=str,
        default="",
        help="Optional per-type sampling multipliers, e.g. 'tntBlast=2,lavaWater=2'",
    )
    parser.add_argument(
        "--state_random_tick_speed",
        type=int,
        default=100,
        help="randomTickSpeed used during iceNearFire episodes so ice melts within the episode "
        "(default: 100; vanilla is 3)",
    )
    parser.add_argument(
        "--nvidia_disable_require",
        type=int,
        default=0,
        choices=[0, 1],
        help="Set NVIDIA_DISABLE_REQUIRE=1 for the camera containers (skips the CUDA>=13 driver "
        "check of the base image; the cameras only use EGL + NVENC) (default: 0)",
    )
    parser.add_argument(
        "--server_memory",
        type=str,
        default="",
        help="Heap for the Paper server container, e.g. 2G (default: image default)",
    )
    parser.add_argument(
        "--render_distance",
        type=int,
        default=8,
        help="Minecraft render distance in chunks (2-32, lower = faster, our default: 8, MC default: 12.)",
    )
    parser.add_argument(
        "--simulation_distance",
        type=int,
        default=4,
        help="Minecraft simulation distance in chunks (5-32, lower = faster, our default: 4, MC default: 8.)",
    )
    parser.add_argument(
        "--graphics_mode",
        type=int,
        default=1,
        choices=[0, 1, 2],
        help="Minecraft graphics mode (0=Fast, 1=Fancy, 2=Fabulous, default: 1)",
    )
    parser.add_argument(
        "--gpu_mode",
        type=str,
        default="egl",
        choices=["egl", "x11", "auto"],
        help="GPU rendering mode: egl (headless), x11 (requires host X), auto (default: egl)",
    )
    parser.add_argument(
        "--disable_nvenc",
        type=int,
        default=0,
        choices=[0, 1],
        help="Force CPU (libx264) video encoding in camera containers. Normally "
        "unnecessary: the camera entrypoint probes NVENC with a test encode and "
        "falls back to CPU automatically on GPUs without NVENC silicon (e.g. "
        "A100/H100). Use this to skip the probe or force CPU encoding (default: 0)",
    )

    args = parser.parse_args()
    # Ensure required dirs are absolute
    args.output_dir = absdir(args.output_dir)
    args.data_dir = absdir(args.data_dir)
    args.camera_output_alpha_base = absdir(args.camera_output_alpha_base)
    args.camera_output_bravo_base = absdir(args.camera_output_bravo_base)

    # Defaults for camera data bases
    project_root = str(Path(__file__).resolve().parent)
    if args.camera_data_alpha_base is None:
        args.camera_data_alpha_base = absdir(
            os.path.join(project_root, "camera", "data_alpha")
        )
    else:
        args.camera_data_alpha_base = absdir(args.camera_data_alpha_base)
    if args.camera_data_bravo_base is None:
        args.camera_data_bravo_base = absdir(
            os.path.join(project_root, "camera", "data_bravo")
        )
    else:
        args.camera_data_bravo_base = absdir(args.camera_data_bravo_base)

    # Create compose directory
    compose_dir = Path(args.compose_dir)
    compose_dir.mkdir(parents=True, exist_ok=True)

    # Determine number of instances and world plan
    use_split = (args.num_flatland_world > 0) or (args.num_normal_world > 0)
    if use_split:
        total_instances = args.num_flatland_world + args.num_normal_world
        world_plan = ["flat"] * args.num_flatland_world + [
            "normal"
        ] * args.num_normal_world
    else:
        total_instances = args.instances
        world_plan = ["normal"] * total_instances

    # GPU configuration validation
    gpu_count = 1

    # GPU configuration summary
    print(f"GPU rendering enabled: {gpu_count} GPUs available, mode={args.gpu_mode}")
    print(f"  Instances will be distributed round-robin across GPUs")
    print(f"  ! Due to a known NVIDIA driver bug, only GPU 0 can be used for now. See Docs->Known Issue(s) for details")
    print(f"  ! https://solaris-wm.github.io/solaris-engine/getting_started.html#known-issue-s")
    for i in range(total_instances):
        gpu_id = i % gpu_count
        print(f"    Instance {i}: GPU {gpu_id}")

    if total_instances > 4:
        raise ValueError(
            f"total_instances={total_instances} exceeds the NVENC limit of 8 "
            f"simultaneous encoding sessions per GPU. "
            f"Please reduce the number of total instances to 4 or fewer."
        )

    # Validate that all skin PNGs have pre-cached .customskin files
    skins_dir = Path(project_root) / "server" / "skins"
    if skins_dir.is_dir():
        missing = [
            p.name for p in sorted(skins_dir.glob("*.png"))
            if not p.with_suffix(p.suffix + ".customskin").exists()
        ]
        if missing:
            raise FileNotFoundError(
                f"Missing .customskin cache files for: {', '.join(missing)}. "
                f"Run: python3 regenerate_skin_cache.py --upload {skins_dir}"
            )
    else:
        raise FileNotFoundError(f"Skins directory not found: {skins_dir}")

    print(f"Generating {total_instances} Docker Compose configurations...")

    for i in range(total_instances):
        world_type = world_plan[i]
        # Calculate cpuset string for this instance if CPU pinning is enabled
        instance_cpuset = None
        camera_alpha_cpuset = None
        camera_bravo_cpuset = None

        # Calculate GPU assignment for this instance (round-robin across available GPUs)
        gpu_device_id = i % gpu_count

        config = generate_compose_config(
            i,
            args.base_port,
            args.base_rcon_port,
            args.act_recorder_port,
            args.coord_port,
            args.data_dir,
            args.output_dir,
            args.num_episodes,
            args.episode_start_id,
            args.bootstrap_wait_time,
            args.episode_category,
            args.episode_types,
            args.smoke_test,
            args.viewer_rendering_disabled,
            world_type,
            str(args.render_distance),
            str(args.simulation_distance),
            str(args.graphics_mode),
            # camera args
            args.camera_output_alpha_base,
            args.camera_output_bravo_base,
            args.camera_data_alpha_base,
            args.camera_data_bravo_base,
            args.camera_alpha_vnc_base,
            args.camera_alpha_novnc_base,
            args.camera_bravo_vnc_base,
            args.camera_bravo_novnc_base,
            args.display_base,
            args.vnc_step,
            args.display_step,
            # CPU pinning
            cpuset=instance_cpuset,
            cpuset_camera_alpha=camera_alpha_cpuset,
            cpuset_camera_bravo=camera_bravo_cpuset,
            # GPU settings
            gpu_device_id=gpu_device_id,
            gpu_mode=args.gpu_mode,
            disable_nvenc=bool(args.disable_nvenc),
            # Eval options
            eval_time_set_day=args.eval_time_set_day,
            # Flatland options
            flatland_world_disable_structures=bool(
                args.flatland_world_disable_structures
            ),
            world_seed=args.world_seed,
            eula=args.eula,
            world_state={
                "logging": args.world_state_logging,
                "snapshot_mode": args.world_snapshot_mode if args.world_state_logging else "none",
                "end_snapshot": args.world_end_snapshot,
                "snapshot_radius": args.world_snapshot_radius,
                "snapshot_down": args.world_snapshot_down,
                "snapshot_up": args.world_snapshot_up,
                "log_radius": args.world_log_radius,
                "heartbeat_ticks": args.world_heartbeat_ticks,
                "reset_between_episodes": args.reset_world_between_episodes,
                "deterministic_teleport": args.deterministic_teleport,
                "episode_weight_multipliers": args.episode_weight_multipliers,
                "state_random_tick_speed": args.state_random_tick_speed,
            },
            teleport_radius=args.teleport_radius,
            server_memory=args.server_memory,
            nvidia_disable_require=bool(args.nvidia_disable_require),
        )

        # Write compose file
        compose_file = compose_dir / f"docker-compose-{i:03d}.yml"
        with open(compose_file, "w") as f:
            yaml.dump(config, f, default_flow_style=False, sort_keys=False)

        # For flat worlds, inject generator settings into the compose file
        if world_type == "flat":
            biome, surface_block = flat_terrain_for_instance(i)
            terrain_settings = generate_terrain_settings(biome, surface_block)

            layers_json = []
            for layer in terrain_settings["layers"]:
                layer_str = (
                    f'{{ "block": "{layer["block"]}", "height": {layer["height"]} }}'
                )
                layers_json.append(layer_str)
            layers_str = ",\n    ".join(layers_json)
            biome_val = terrain_settings["biome"]
            terrain_json = f'{{\n  "layers": [\n    {layers_str}\n  ],\n  "biome": "{biome_val}"\n}}'
            newline = "\n"
            terrain_multiline = (
                f">-\n        {terrain_json.replace(newline, newline + '        ')}"
            )

            with open(compose_file, "r") as f:
                content = f.read()
            content = re.sub(
                r"GENERATOR_SETTINGS: TERRAIN_SETTINGS_PLACEHOLDER",
                f"GENERATOR_SETTINGS: {terrain_multiline}",
                content,
            )
            with open(compose_file, "w") as f:
                f.write(content)

        # Create necessary directories
        os.makedirs(f"{args.data_dir}/{i}", exist_ok=True)
        # Camera output/data per-instance dirs
        cp = camera_paths(
            i,
            args.camera_output_alpha_base,
            args.camera_output_bravo_base,
            args.camera_data_alpha_base,
            args.camera_data_bravo_base,
        )
        os.makedirs(cp["alpha_output_host"], exist_ok=True)
        os.makedirs(cp["bravo_output_host"], exist_ok=True)
        os.makedirs(cp["alpha_data_host"], exist_ok=True)
        os.makedirs(cp["bravo_data_host"], exist_ok=True)

        print(f"Generated: {compose_file}")

    # Create shared output directory. The Paper server (non-root user inside the
    # itzg container) writes world_events.jsonl / snapshots into it, so make it
    # world-writable; run.sh chowns everything back to the host user afterwards.
    os.makedirs(args.output_dir, exist_ok=True)
    try:
        os.chmod(args.output_dir, 0o777)
    except OSError as exc:
        print(f"Warning: could not chmod {args.output_dir}: {exc}")

    print(f"\nGenerated {total_instances} configurations in {compose_dir}/")
    print("Published port ranges (host network):")
    print(
        f"  Minecraft servers: {args.base_port}-{args.base_port + total_instances - 1}"
    )
    print(
        f"  RCON ports: {args.base_rcon_port}-{args.base_rcon_port + total_instances - 1}"
    )
    # Collision validation for camera ports
    alpha_vncs = {
        args.camera_alpha_vnc_base + args.vnc_step * i for i in range(total_instances)
    }
    alpha_novncs = {
        args.camera_alpha_novnc_base + args.vnc_step * i for i in range(total_instances)
    }
    bravo_vncs = {
        args.camera_bravo_vnc_base + args.vnc_step * i for i in range(total_instances)
    }
    bravo_novncs = {
        args.camera_bravo_novnc_base + args.vnc_step * i for i in range(total_instances)
    }
    assert len(alpha_vncs) == total_instances, "alpha VNC port collisions detected"
    assert len(alpha_novncs) == total_instances, "alpha noVNC port collisions detected"
    assert len(bravo_vncs) == total_instances, "bravo VNC port collisions detected"
    assert len(bravo_novncs) == total_instances, "bravo noVNC port collisions detected"
    print(
        f"  Camera Alpha noVNC: {args.camera_alpha_novnc_base}..{args.camera_alpha_novnc_base + args.vnc_step * (total_instances - 1)}"
    )
    print(
        f"  Camera Bravo noVNC: {args.camera_bravo_novnc_base}..{args.camera_bravo_novnc_base + args.vnc_step * (total_instances - 1)}"
    )
    print(
        "Bridge network services (act_recorder, controller) use internal communication only."
    )


if __name__ == "__main__":
    main()
