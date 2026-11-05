#!/usr/bin/env python3
# Modified by Ziyang Ye for MineEngine, 2026 (from SolarisEngine 78f23e7); see capture/MODIFICATIONS.md
import hashlib
import json
import os
import subprocess
import sys
import uuid
from pathlib import Path

import minecraft_launcher_lib


def env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except ValueError:
        return default


def ensure_version(version: str, minecraft_dir: Path) -> None:
    minecraft_dir.mkdir(parents=True, exist_ok=True)
    version_dir = minecraft_dir / "versions" / version
    jar_file = version_dir / f"{version}.jar"
    json_file = version_dir / f"{version}.json"
    if jar_file.exists() and json_file.exists():
        print(
            f"[launcher] found cached Minecraft {version} at {version_dir}, skipping install",
            flush=True,
        )
        return
    minecraft_launcher_lib.install.install_minecraft_version(version, minecraft_dir)


def ensure_option(minecraft_dir: Path, key: str, value: str) -> None:
    options_path = minecraft_dir / "options.txt"
    if options_path.exists():
        lines = options_path.read_text().splitlines()
    else:
        options_path.parent.mkdir(parents=True, exist_ok=True)
        lines = []

    entry = f"{key}:{value}"
    for idx, line in enumerate(lines):
        if line.startswith(f"{key}:"):
            if line == entry:
                return
            lines[idx] = entry
            break
    else:
        lines.append(entry)

    options_path.write_text("\n".join(lines) + "\n")


def camera_option_values() -> dict[str, str]:
    """Fixed camera settings (T2, T13) as options.txt strings."""
    return {
        # options.txt stores fov as (deg - 70) / 40, so 0.0 = 70 degrees
        "fov": "0.0",
        # No FOV change while flying (x1.1), sprinting or drawing a bow
        "fovEffectScale": "0.0",
        "renderDistance": str(env_int("RENDER_DISTANCE", 12)),
        "simulationDistance": str(env_int("SIMULATION_DISTANCE", 12)),
        "bobView": "false",
        "damageTiltStrength": "0.0",
        "guiScale": str(env_int("GUI_SCALE", 2)),
        "pauseOnLostFocus": "false",
        # Clean frames: no chat lines (0 = full, 1 = system only, 2 = hidden; CHAT_VISIBILITY env).
        # The action bar / held-item name / hotbar stay. Toast suppression needs the
        # solaris-capture mod (Windows-native camera only).
        "chatVisibility": str({"full": 0, "system": 1, "hidden": 2}.get(
            os.environ.get("CHAT_VISIBILITY", "hidden").strip().lower(), 2)),
    }


def write_camera_options(minecraft_dir: Path) -> dict[str, str]:
    values = camera_option_values()
    for key, value in values.items():
        ensure_option(minecraft_dir, key, value)
    return values


def record_camera_options(values: dict[str, str], width: int, height: int) -> None:
    """Write <recording dir>/camera_options.json with the options.txt values."""
    recording_path = os.environ.get("RECORDING_PATH", "/output/camera.mkv")
    out_path = Path(recording_path).parent / "camera_options.json"
    record = {
        "options_txt": values,
        "fov_deg": 70.0 + 40.0 * float(values["fov"]),
        "fov_effect_scale": float(values["fovEffectScale"]),
        "render_distance": int(values["renderDistance"]),
        "simulation_distance": int(values["simulationDistance"]),
        "bob_view": values["bobView"] == "true",
        "damage_tilt_strength": float(values["damageTiltStrength"]),
        "gui_scale": int(values["guiScale"]),
        "pause_on_lost_focus": values["pauseOnLostFocus"] == "true",
        "chat_visibility": {"0": "full", "1": "system", "2": "hidden"}.get(values["chatVisibility"], "hidden"),
        # toasts are only suppressed by the solaris-capture mod (not loaded on this path)
        "toasts_suppressed": False,
        # Smooth camera is a keybind toggle that starts off; HUD and name tags are untouched
        "smooth_camera": False,
        "hud_visible": True,
        "name_tags_visible": True,
        "width": width,
        "height": height,
    }
    try:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(record, indent=2) + "\n")
        print(f"[launcher] camera options recorded in {out_path}", flush=True)
    except OSError as exc:
        print(f"[launcher] warning: cannot write {out_path}: {exc}", flush=True)


def offline_login(username: str) -> dict[str, str]:
    data = ("OfflinePlayer:" + username).encode("utf-8")
    offline_uuid = uuid.UUID(hashlib.md5(data).hexdigest())
    return {
        "name": username,
        "uuid": str(offline_uuid),
        "token": "0",
    }


def build_launch_command(version: str, minecraft_dir: Path) -> list[str]:
    username = os.environ.get("CAMERA_NAME", "CameraAlpha")
    login = offline_login(username)

    width = env_int("WIDTH", 1280)
    height = env_int("HEIGHT", 720)
    host = os.environ.get("MC_HOST", "mc")
    port = os.environ.get("MC_PORT", "25565")

    quickplay_path = minecraft_dir / "quickplay" / "multiplayer.json"
    quickplay_path.parent.mkdir(parents=True, exist_ok=True)
    quickplay_entry = {
        "address": f"{host}:{port}",
        "name": host or "server",
        "icon": None,
    }
    quickplay_path.write_text(json.dumps(quickplay_entry, separators=(",", ":")))
    ensure_option(minecraft_dir, "skipQuickPlayFirstLaunchPrompt", "true")
    ensure_option(minecraft_dir, "accessibilityOnboarded", "true")
    ensure_option(minecraft_dir, "tutorialStep", "none")
    camera_options = write_camera_options(minecraft_dir)
    record_camera_options(camera_options, width, height)

    options = {
        "username": login["name"],
        "uuid": login["uuid"],
        "token": login["token"],
        "userType": "legacy",
        "launcherName": "mc-multiplayer-camera",
        "launcherVersion": "0.1",
        "gameDirectory": str(minecraft_dir),
        "customResolution": True,
        "resolutionWidth": str(width),
        "resolutionHeight": str(height),
        "jvmArguments": [
            "-Xmx" + os.environ.get("MC_MAX_MEMORY", "4G"),
            "-Xms" + os.environ.get("MC_MIN_MEMORY", "2G"),
            "-XX:+UnlockExperimentalVMOptions",
            "-XX:+UseG1GC",
            "-XX:G1NewSizePercent=20",
            "-XX:G1ReservePercent=20",
            "-XX:MaxGCPauseMillis=50",
            "-XX:G1HeapRegionSize=32M",
            "-Dsun.net.client.defaultConnectTimeout=2000",
            "-Dsun.net.client.defaultReadTimeout=2000",
        ],
        "javaExecutable": os.environ.get("JAVA_BIN", "/usr/bin/java"),
    }

    command = minecraft_launcher_lib.command.get_minecraft_command(
        version, minecraft_dir, options
    )

    java_bin = os.environ.get("JAVA_BIN")
    if java_bin:
        command[0] = java_bin

    quickplay_target = f"{host}:{port}"
    command.extend(["--quickPlayPath", str(quickplay_path)])
    command.extend(["--quickPlayMultiplayer", quickplay_target])

    return command


def main() -> int:
    minecraft_dir = Path(os.environ.get("MINECRAFT_HOME", "/root/.minecraft"))
    version = os.environ.get("MC_VERSION", "1.21")

    print(f"[launcher] ensuring Minecraft {version} in {minecraft_dir}", flush=True)
    ensure_version(version, minecraft_dir)

    command = build_launch_command(version, minecraft_dir)
    print(
        f"[launcher] starting Minecraft with command: {' '.join(command)}", flush=True
    )

    env = os.environ.copy()
    process = subprocess.Popen(command, env=env)

    try:
        return process.wait()
    except KeyboardInterrupt:
        process.terminate()
        return process.wait()


if __name__ == "__main__":
    sys.exit(main())
