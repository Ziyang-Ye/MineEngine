#!/usr/bin/env python3
"""Windows-native stateful capture: the run_laptop.sh / run.sh pipeline without
Docker or WSL. Every compose service becomes a native process on 127.0.0.1
(the bot coordination ports listen on all interfaces; see windows/CONTRACT.md); output tree and file names are the same as the
Docker path (OUTPUT_SCHEMA.md).

Usage (from cmd.exe, PowerShell or Git Bash):
  run_windows.cmd --accept-minecraft-eula [--mode stateful|mix|all|TYPES]
                  [--episodes N] [--batches N] [--output-dir DIR]
                  [--world-seed SEED] [--reset-world] [--normal-world]
                  [--snapshot-mode blocks|region|both|none]
                  [--width 1280 --height 720] [--show-cameras] [--server-memory 2G]
                  [--dry-run] [--no-cameras] [-- <extra run.sh options>]

Per batch: Paper server -> (RCON ready) -> act recorders -> cameras + spectators
+ plugin starter -> controllers. When both controllers have exited (or
--batch-timeout / Ctrl+C) the cameras get their stop files, everything is shut
down (server via RCON `stop`), then alignment (postprocess/process_recordings.py)
and postprocess/verify_world_events.py run. Ctrl+C once = clean stop, twice =
kill everything now; a Windows Job Object guarantees that no java/node/ffmpeg
process outlives this script.

Linux (headless Linux job container, linux/README.md): the same runner with
--xvfb BIN (managed Xvfb on --display), --camera-prefix 'vglrun -d egl',
--offline (no network at run time) and explicit --java/--python/--ffmpeg-dir/
--paper-jar/--paper-repo/--mc-home/--mod-jar; SIGTERM = the Ctrl+C clean stop
without alignment (the job may be killed soon; align later in a CPU job).
"""

import argparse
import json
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Dict, List, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))
import common as C  # noqa: E402

sys.path.insert(0, str(C.REPO_ROOT))
import generate_compose as G  # noqa: E402

REPO = C.REPO_ROOT

# Values run.sh hard-codes when calling generate_compose.py
RUN_SH_COMPOSE_ARGS = {
    "smoke_test": 0,
    "eval_time_set_day": 0,
    "viewer_rendering_disabled": 1,
    "gpu_mode": "egl",
}
# generate_compose.py main() defaults for arguments run.sh does not pass
COMPOSE_DEFAULTS = {
    "episode_start_id": 0,
    "bootstrap_wait_time": 60,
    "episode_category": "look",
    "render_distance": "8",
    "simulation_distance": "4",
    "graphics_mode": "1",
    "camera_alpha_vnc_base": 5901,
    "camera_alpha_novnc_base": 6901,
    "camera_bravo_vnc_base": 5902,
    "camera_bravo_novnc_base": 6902,
    "display_base": 99,
    "vnc_step": 2,
    "display_step": 2,
    "world_end_snapshot": 1,
    "world_snapshot_down": 24,
    "world_snapshot_up": 40,
    "world_log_radius": 64,
    "world_heartbeat_ticks": 1,
    "deterministic_teleport": 1,
    "disable_nvenc": False,
}
# Per-instance step of the recorder/coord/pose port pairs (instance k adds k*STEP).
# The original contract said +10*k, which makes instance 1's recorders (8120/8121)
# collide with instance 0's coord ports. With the defaults one instance uses the
# block 8110-8131 (recorders 8110/8111, coord 8120/8121, camera pose links
# 8130/8131), so instances are 30 ports apart: instance 3 ends at 8221.
# plan_batch() checks the whole plan (game, RCON, recorders, coord, pose) for
# duplicates and refuses to start on a collision.
NATIVE_PORT_STEP = 30

# New controller env vars (passed as env AND as --flag=value when set and
# controller/entrypoint.sh does not already map them)
EXTRA_CONTROLLER_VARS = {
    "DUSK_NIGHT_FRACTION": "dusk_night_fraction",
    "FIXED_TIME": "fixed_time",
    "WORLD_ENTITIES_EVERY": "world_entities_every",
    "WORLD_ENTITIES_RADIUS": "world_entities_radius",
}

BOTS = ("Alpha", "Bravo")
STOP_FILE_NAME = "solaris_capture.stop"

# Linux: display/GL variables of this process copied into the camera env (so
# run_config.json records them; children inherit them anyway), plus DISPLAY
CAMERA_ENV_PASSTHROUGH = ("DISPLAY", "LD_LIBRARY_PATH", "__EGL_VENDOR_LIBRARY_DIRS",
                          "__EGL_VENDOR_LIBRARY_FILENAMES", "__GLX_VENDOR_LIBRARY_NAME")
CAMERA_ENV_PREFIXES = ("VGL_",)
DEFAULT_XVFB_DISPLAY = ":99"
# --offline: (file in the instance's server copy, yaml key, value); the repo's
# server/plugins files are never changed
OFFLINE_PLUGIN_SETTINGS = (
    ("plugins/SkinsRestorer/config.yml", "noConnections", "true"),
    ("plugins/SkinsRestorer/config.yml", "fetchRecommendedSkins", "false"),
    ("plugins/bStats/config.yml", "enabled", "false"),
)

EULA_MESSAGE = """\
The Paper server only starts after the person running it has accepted the
Minecraft EULA (https://aka.ms/MinecraftEULA); run_windows.py then writes
eula.txt (eula=true) into the server directory. The server runs in offline
mode and the camera clients log in offline.
If you accept it, re-run with --accept-minecraft-eula."""

# Ctrl+C state: first press = clean stop, second press = kill everything
# (SIGTERM on Linux behaves the same; "reason" names the first signal, "t" its time)
_STOP = {"requested": False, "force": False, "reason": "Ctrl+C", "t": 0.0}
# A SIGTERM this soon after the first signal is the same stop delivered twice
# (process-group signal + a wrapper forwarding it), not a request to kill
SIGTERM_REPEAT_IGNORE = 5.0
# Kill-on-close Job Object containing this process and all children. It must
# stay open until the very end: closing it kills every member, including us.
_JOB = None


def _on_signal(signum, frame):  # noqa: ARG001
    term = signum == getattr(signal, "SIGTERM", None) and not C.IS_WINDOWS
    if _STOP["requested"]:
        if term and time.monotonic() - _STOP["t"] < SIGTERM_REPEAT_IGNORE:
            print(f"[run_windows] repeated SIGTERM within {SIGTERM_REPEAT_IGNORE:.0f}s of the first "
                  "signal ignored", flush=True)
            return
        _STOP["force"] = True
        raise KeyboardInterrupt
    _STOP["requested"], _STOP["t"] = True, time.monotonic()
    if term:
        _STOP["reason"] = "SIGTERM"
        print("\n[run_windows] SIGTERM: stopping the batch cleanly (stop files, RCON stop); alignment "
              f"is skipped. A Ctrl+C or a SIGTERM more than {SIGTERM_REPEAT_IGNORE:.0f}s later kills "
              "everything immediately.", flush=True)
        return
    print("\n[run_windows] Ctrl+C: stopping the batch cleanly (stop files, RCON stop). "
          "Press Ctrl+C again to kill everything immediately.", flush=True)


def stop_requested() -> bool:
    return _STOP["requested"]


def sigterm_stop() -> bool:
    """Stopped by the job scheduler: no alignment, the job may be killed soon."""
    return _STOP["requested"] and _STOP["reason"] == "SIGTERM"


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="run_windows.py",
        description="Windows-native SolarisEngine stateful capture (no Docker). "
        "Same options as run_laptop.sh; options after `--` are run.sh options.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    g = p.add_argument_group("run_laptop.sh options")
    g.add_argument("--accept-minecraft-eula", action="store_true",
                   help="you accept the Minecraft EULA (writes eula.txt into the server dir)")
    g.add_argument("--mode", default="stateful",
                   help="stateful | mix | all | diverse (category-first sampling) | "
                   "build | destroy | fight | move | interaction (one category) | creatures | mechanics | "
                   "new (the round-2 types) | comma list of episode types and aliases (default: stateful)")
    g.add_argument("--episodes", "--num-episodes", dest="episodes", type=int, default=20,
                   help="episodes per batch (default: 20)")
    g.add_argument("--batches", "--num-batches", dest="batches", type=int, default=1,
                   help="number of batches (default: 1)")
    g.add_argument("--output-dir", default="output_laptop", help="output root (default: output_laptop)")
    g.add_argument("--world-seed", default=None, help="fixed world seed (default: <instance><unix time>)")
    g.add_argument("--reset-world", action="store_true",
                   help="restore the episode box to its initial blocks after each episode")
    g.add_argument("--normal-world", action="store_true", help="normal terrain instead of a flat world")
    g.add_argument("--snapshot-mode", default="blocks", choices=["blocks", "region", "both", "none"],
                   help="initial-state snapshot mode (default: blocks)")

    g = p.add_argument_group("run.sh options (normally given after `--`)")
    g.add_argument("--num-flat-world", type=int, default=None)
    g.add_argument("--num-normal-world", type=int, default=None)
    g.add_argument("--episode-types", default=None, help="overrides the types derived from --mode")
    g.add_argument("--weight-mults", default=None, help="per-type sampling multipliers, e.g. tntBlast=2")
    g.add_argument("--flat-no-structures", action="store_true")
    g.add_argument("--world-state", default="on", help="on|off (default: on)")
    g.add_argument("--snapshot-radius", type=int, default=32)
    g.add_argument("--teleport-radius", type=int, default=2000, help="(run_laptop default: 2000)")
    g.add_argument("--random-tick-speed", type=int, default=100)
    g.add_argument("--eula", default=None,
                   help="EULA value recorded in the compose config (default: TRUE with --accept-minecraft-eula, else FALSE)")
    g.add_argument("--workers", type=int, default=4, help="parallel alignment workers (default: 4)")
    g.add_argument("--skip-dataset", action="store_true",
                   help="accepted for compatibility; dataset preparation is always skipped (as run_laptop.sh)")
    g.add_argument("--nvidia-disable-require", action="store_true", help="accepted, no effect on Windows")
    g.add_argument("--dataset-name", default="duet", help="accepted, no effect (dataset is skipped)")
    g.add_argument("--filter-water-episodes", default="false", help="accepted, no effect (dataset is skipped)")

    g = p.add_argument_group("Windows options")
    g.add_argument("--width", type=int, default=1280, help="camera recording width (default: 1280)")
    g.add_argument("--height", type=int, default=720, help="camera recording height (default: 720)")
    g.add_argument("--fps", type=int, default=None, help="camera fps (default: compose FPS = 20)")
    g.add_argument("--show-cameras", action="store_true",
                   help="keep the camera windows visible (default: hidden; recording works either way)")
    g.add_argument("--server-memory", default="2G", help="Paper heap, -Xms/-Xmx (default: 2G)")
    g.add_argument("--camera-memory", default="3G", help="camera client heap (default: 3G)")
    g.add_argument("--render-distance", type=int, default=12,
                   help="camera render distance and server view/simulation distance (default: 12)")
    g.add_argument("--encoder", default="auto", choices=["auto", "h264_nvenc", "libx264"])
    g.add_argument("--gui-scale", default="auto")
    g.add_argument("--no-cameras", action="store_true",
                   help="test mode: no camera clients (ENABLE_CAMERA_WAIT=0, cameras not required "
                   "by the plugin starter, no alignment)")
    g.add_argument("--dry-run", action="store_true", help="print all commands/env/ports, start nothing")
    g.add_argument("--smoke-test", action="store_true",
                   help="controllers cycle through the eligible episode types in alphabetical order "
                   "(SMOKE_TEST=1) instead of sampling them")
    g.add_argument("--batch-timeout", type=float, default=0,
                   help="stop a batch after this many seconds (default: 0 = no limit)")
    g.add_argument("--controller-exit-grace", type=float, default=300,
                   help="after one controller of an instance exits, stop its partner after this many "
                        "seconds (per instance; default: 300)")
    g.add_argument("--camera-stop-timeout", type=float, default=60,
                   help="seconds to wait for cameras after writing their stop files (default: 60)")
    g.add_argument("--server-ready-timeout", type=float, default=900,
                   help="seconds to wait for RCON after starting Paper (default: 900)")
    g.add_argument("--bootstrap-wait-time", type=int, default=None,
                   help="controller BOOTSTRAP_WAIT_TIME (default: compose value, 60)")
    g.add_argument("--skip-postprocess", action="store_true", help="no alignment / verification")
    g.add_argument("--dusk-night-fraction", default=None, help="sets DUSK_NIGHT_FRACTION for the controllers")
    g.add_argument("--fixed-time", default=None, help="sets FIXED_TIME for the controllers")
    g.add_argument("--world-entities-every", default=None, help="sets WORLD_ENTITIES_EVERY")
    g.add_argument("--world-entities-radius", default=None, help="sets WORLD_ENTITIES_RADIUS")

    g = p.add_argument_group("ports (production defaults = CONTRACT §1)")
    g.add_argument("--base-port", type=int, default=25590)
    g.add_argument("--base-rcon-port", type=int, default=25600)
    g.add_argument("--act-recorder-port", type=int, default=8110,
                   help="Alpha recorder port; Bravo = +1 (default: 8110)")
    g.add_argument("--coord-port", type=int, default=8120,
                   help="Alpha coord port; Bravo = +1 (default: 8120)")
    g.add_argument("--pose-port", type=int, default=8130,
                   help="UDP port of Alpha's camera pose link (controller -> camera, T12); "
                        "Bravo = +1 (default: 8130)")
    g.add_argument("--no-pose-link", action="store_true",
                   help="cameras follow their bot through the server only (no direct pose link)")

    g = p.add_argument_group("tools (default: .cache/windows/setup.json, PATH, known locations)")
    g.add_argument("--java", default=None, help="java.exe for the Paper server (Java 21+)")
    g.add_argument("--node", default=None, help="node.exe")
    g.add_argument("--python", default=None, help="python.exe for recorders/cameras/postprocess (default: .venv)")
    g.add_argument("--ffmpeg-dir", default=None, help="directory with ffmpeg.exe and ffprobe.exe")
    g.add_argument("--paper-jar", default=None, help="Paper 1.21 jar")
    g.add_argument("--paper-repo", default=None,
                   help="Paperclip bundler repo, pre-patched (default: .cache/windows/paper_repo)")
    g.add_argument("--mc-home", default=None, help="shared camera .minecraft (default: .cache/windows/minecraft)")
    g.add_argument("--mod-jar", default=None, help="solaris-capture mod jar")
    g.add_argument("--camera-java", default=None,
                   help="java of the camera clients (default: the Mojang runtime in --mc-home)")
    g.add_argument("--launch-camera", default=None,
                   help="camera launcher script (default: camera/windows/launch_camera.py; for tests)")

    g = p.add_argument_group("Linux / offline options (headless Linux job container; linux/README.md)")
    g.add_argument("--xvfb", default=None, metavar="BIN",
                   help="start this Xvfb per batch as a managed service on --display (default: none; "
                        "the cameras use $DISPLAY)")
    g.add_argument("--display", default=None,
                   help=f"X display of the camera clients (default: {DEFAULT_XVFB_DISPLAY} with --xvfb, "
                        "else $DISPLAY)")
    g.add_argument("--xvfb-screen", default=None, help="Xvfb screen 0, WxHxD (default: <width>x<height>x24)")
    g.add_argument("--xvfb-args", default="+extension GLX +render -noreset",
                   help="further Xvfb arguments, one shell-quoted string (default, as camera/entrypoint.sh: "
                        "'+extension GLX +render -noreset')")
    g.add_argument("--camera-prefix", default="",
                   help="command prefix of each camera launcher, one shell-quoted string, "
                        "e.g. 'vglrun -d egl' (Minecraft and its ffmpeg inherit its environment)")
    g.add_argument("--camera-env", action="append", default=[], metavar="KEY=VALUE",
                   help="extra env var of the camera processes (repeatable); DISPLAY, VGL_*, "
                        "__EGL_VENDOR_LIBRARY_DIRS and LD_LIBRARY_PATH of this process are passed through")
    g.add_argument("--resource-packs", default=None, metavar="ID[,ID...]",
                   help="resource packs the cameras enable on top of vanilla, e.g. programmer_art (built-in, in the staged "
                        "assets) or file/<pack>.zip in the camera game dir; written to options.txt resourcePacks")
    g.add_argument("--camera-option", action="append", default=[], metavar="KEY:VALUE",
                   help="extra options.txt line for every camera (launch_camera.py --extra-option)")
    g.add_argument("--offline", action="store_true",
                   help="no network at run time: SKIN_MODE=local for the controllers; SkinsRestorer "
                        "noConnections / no recommended skins and bStats off in each instance's server copy; "
                        "cameras refuse to install (launch_camera.py --offline)")
    g.add_argument("--recorder-cadence", default="auto", choices=["auto", "deadline", "after"],
                   help="controller sample cadence (env RECORDER_CADENCE): auto = the 50 ms deadline grid "
                        "on every OS (Windows: recorder default; elsewhere set explicitly)")
    return p


def parse_cli(argv: List[str]) -> argparse.Namespace:
    # run_laptop.sh passes everything after `--` to run.sh after its own
    # arguments, so later values win; the same happens here.
    if "--" in argv:
        i = argv.index("--")
        argv = argv[:i] + argv[i + 1:]
    args = build_parser().parse_args(argv)

    # Episode selection (same table as run_laptop.sh)
    weights = ""
    if args.mode == "stateful":
        types = "stateful"
    elif args.mode == "mix":
        types = "all"
        weights = "igniteAndExtinguish=3,iceNearFire=3,tntBlast=3,lavaWater=3,buildAndBreak=3"
    elif args.mode == "all":
        types = "all"
    else:
        types = args.mode
    args.types = args.episode_types if args.episode_types is not None else types
    args.weights = args.weight_mults if args.weight_mults is not None else weights

    args.num_flat = args.num_flat_world if args.num_flat_world is not None else (0 if args.normal_world else 1)
    args.num_normal = args.num_normal_world if args.num_normal_world is not None else (1 if args.normal_world else 0)
    args.world_state_logging = 0 if str(args.world_state).lower() in ("off", "0") else 1
    return args


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------

def resolve_tools(args) -> dict:
    setup = C.load_setup()
    node = C.find_node(args.node, setup)
    ffmpeg_dir = C.find_ffmpeg_dir(args.ffmpeg_dir, setup)
    tools = {
        "setup_json": str(C.SETUP_JSON) if setup else None,
        "java": C.find_java(args.java, setup),
        "node": node,
        "python": C.find_python(args.python, setup),
        "ffmpeg_dir": ffmpeg_dir,
        "ffmpeg": str(Path(ffmpeg_dir) / f"ffmpeg{C.EXE}") if ffmpeg_dir else None,
        "paper_jar": C.find_paper_jar(args.paper_jar, setup),
        "mc_home": str(Path(args.mc_home or setup.get("mc_home") or C.DEFAULT_MC_HOME).absolute()),
        "capture_mod_jar": C.find_capture_mod_jar(args.mod_jar, setup),
        "launch_camera": str(Path(args.launch_camera).absolute() if args.launch_camera
                             else REPO / "camera" / "windows" / "launch_camera.py"),
        "mirrorbot_jar": str(REPO / "server" / "plugins" / "MirrorBot-1.0.jar"),
        "paper_bundler_repo": str(Path(args.paper_repo).absolute() if args.paper_repo else C.PAPER_BUNDLER_REPO),
    }
    return tools


def tool_versions(tools: dict) -> dict:
    v = {}
    if tools.get("java"):
        v["java"] = C.tool_version([tools["java"], "-version"])
    if tools.get("node"):
        v["node"] = C.tool_version([tools["node"], "--version"])
    if tools.get("python"):
        v["python"] = C.tool_version([tools["python"], "--version"])
    if tools.get("ffmpeg"):
        v["ffmpeg"] = C.tool_version([tools["ffmpeg"], "-hide_banner", "-version"])
    for key in ("paper_jar", "capture_mod_jar", "mirrorbot_jar"):
        if tools.get(key) and Path(tools[key]).is_file():
            v[f"{key}_sha256"] = C.sha256_file(Path(tools[key]))
    if tools.get("mirrorbot_jar") and Path(tools["mirrorbot_jar"]).is_file():
        v["mirrorbot_has_world_state_logger"] = C.jar_contains(Path(tools["mirrorbot_jar"]),
                                                               "WorldStateLogger.class")
    v["os"] = f"{os.name} {sys.platform} {' '.join(map(str, sys.getwindowsversion()[:3])) if C.IS_WINDOWS else ''}".strip()
    return v


def check_tools(tools: dict, args) -> List[str]:
    """Problems that prevent a live run."""
    setup = "setup_windows.cmd" if C.IS_WINDOWS else "windows/setup_windows.py --platform linux"
    problems = []
    for key, what in (("java", "java (Java 21+)"), ("node", "node"), ("paper_jar", "Paper 1.21 jar")):
        if not tools.get(key):
            problems.append(f"{what} not found (run {setup} or pass --{key.replace('_', '-')})")
    if not tools.get("ffmpeg_dir"):
        problems.append(f"ffmpeg/ffprobe not found (run {setup} or pass --ffmpeg-dir)")
    mb = Path(tools["mirrorbot_jar"])
    if not mb.is_file() or not C.jar_contains(mb, "WorldStateLogger.class"):
        problems.append(f"{mb} missing or without WorldStateLogger.class (run {setup})")
    if not (REPO / "node_modules" / "mineflayer").is_dir():
        problems.append(f"node_modules missing (run {setup}: npm ci --ignore-scripts)")
    if not (REPO / "server" / "plugin-starter" / "node_modules" / "rcon-client").is_dir():
        problems.append(f"server/plugin-starter/node_modules missing (run {setup})")
    if not args.no_cameras:
        if not Path(tools["launch_camera"]).is_file():
            problems.append(f"{tools['launch_camera']} not found")
        if not tools.get("capture_mod_jar"):
            problems.append(f"solaris-capture mod jar not found (run {setup} or pass --mod-jar)")
        if sys.platform.startswith("linux") and not camera_display(args):
            problems.append("no X display for the camera clients (pass --xvfb <Xvfb binary>, or --display / DISPLAY)")
        if args.xvfb and not shutil.which(args.xvfb):
            problems.append(f"--xvfb {args.xvfb} not found or not executable")
        prefix = shlex.split(args.camera_prefix)
        if prefix and not shutil.which(prefix[0]):
            problems.append(f"--camera-prefix command {prefix[0]} not found or not executable")
    if args.paper_repo and not (Path(tools["paper_bundler_repo"]) / "versions").is_dir():
        problems.append(f"--paper-repo {tools['paper_bundler_repo']} has no versions/ (not pre-patched)")
    return problems


# ---------------------------------------------------------------------------
# Linux: display, camera env, offline server copy
# ---------------------------------------------------------------------------

def camera_options_extra(args) -> List[str]:
    """--camera-option lines plus resourcePacks for --resource-packs (vanilla first, then the packs in order)."""
    out = list(args.camera_option)
    if args.resource_packs:
        packs = ["vanilla"] + [p.strip() for p in args.resource_packs.split(",") if p.strip()]
        out.append("resourcePacks:" + json.dumps(packs, separators=(",", ":")))
    return out


def camera_display(args) -> Optional[str]:
    if args.xvfb:
        return args.display or DEFAULT_XVFB_DISPLAY
    return args.display or os.environ.get("DISPLAY") or None


def camera_env(args) -> Dict[str, str]:
    """Env of the camera services on top of the runner's own: the display/GL
    variables of this process, DISPLAY (--display / Xvfb), then --camera-env."""
    env = {k: v for k, v in os.environ.items()
           if k in CAMERA_ENV_PASSTHROUGH or k.startswith(CAMERA_ENV_PREFIXES)}
    display = camera_display(args)
    if display:
        env["DISPLAY"] = display
    for item in args.camera_env:
        key, sep, value = item.partition("=")
        if not sep or not key:
            raise SystemExit(f"--camera-env expects KEY=VALUE, got {item!r}")
        env[key] = value
    return env


def recorder_cadence(args) -> Optional[str]:
    """RECORDER_CADENCE for the controllers (None = leave the recorder default)."""
    if args.recorder_cadence != "auto":
        return args.recorder_cadence
    return None if C.IS_WINDOWS else "deadline"


def xvfb_service(args, batch_dir: Path) -> Optional[dict]:
    if not args.xvfb or args.no_cameras:
        return None
    display = camera_display(args)
    screen = args.xvfb_screen or f"{args.width}x{args.height}x24"
    return {
        "kind": "xvfb",
        "display": display,
        "cmd": [args.xvfb, display, "-screen", "0", screen, "-ac", "-nolisten", "tcp",
                *shlex.split(args.xvfb_args)],
        "cwd": str(batch_dir),
        "env": {},
        "log": str(batch_dir / "logs" / "xvfb.log"),
    }


def x_display_number(display: str) -> str:
    return display.rsplit(":", 1)[-1].split(".")[0]


def x_display_ready(display: str) -> bool:
    """True when the X server of `display` accepts connections (socket file or
    the Linux abstract socket)."""
    import socket
    n = x_display_number(display)
    for addr in (f"/tmp/.X11-unix/X{n}", f"\0/tmp/.X11-unix/X{n}"):
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.settimeout(1)
            try:
                s.connect(addr)
                return True
            except OSError:
                pass
    return False


def offline_server_config(server_dir: Path) -> List[str]:
    """--offline: switch off the plugins' outbound connections in a server copy.
    Returns log lines; a key that is not found exactly once is reported, not added."""
    msgs = []
    for rel, key, value in OFFLINE_PLUGIN_SETTINGS:
        path = server_dir / rel
        text = path.read_text(encoding="utf-8") if path.is_file() else ""
        new, n = re.subn(rf"^([ \t]*{re.escape(key)}:)[ \t]*[^\s#]*", rf"\g<1> {value}", text, flags=re.M)
        if n != 1:
            msgs.append(f"WARNING offline: {key} found {n} times in {path}; not changed")
            continue
        path.write_text(new, encoding="utf-8")
        msgs.append(f"offline: {rel} {key}: {value}")
    # SkinsRestorer's documented switch for its updater
    noupdate = server_dir / "plugins" / "SkinsRestorer" / "noupdate.txt"
    if noupdate.parent.is_dir():
        noupdate.write_text("written by run_windows.py --offline\n", encoding="utf-8")
        msgs.append("offline: plugins/SkinsRestorer/noupdate.txt")
    return msgs


# ---------------------------------------------------------------------------
# Plan: everything a batch will start (also what --dry-run prints)
# ---------------------------------------------------------------------------

def _env_str(v) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    return str(v)


def controller_flags(env: Dict[str, str]) -> List[str]:
    """The `node controller/main.js` flags of controller/entrypoint.sh, with
    bash `${VAR:-default}` resolved against `env` (parsed from the script so
    the native runner follows entrypoint changes automatically)."""
    text = (REPO / "controller" / "entrypoint.sh").read_text(encoding="utf-8")
    start = text.find("controller/main.js")
    pattern = re.compile(r'--(\w+)(=|[ \t]+)"\$\{(\w+):-([^}]*)\}"')
    flags = []
    seen_vars = set()
    for m in pattern.finditer(text[start:]):
        name, sep, var, default = m.groups()
        seen_vars.add(var)
        val = env.get(var, "")
        if val == "":
            val = default
        if sep == "=":
            flags.append(f"--{name}={val}")
        else:
            flags += [f"--{name}", val]
    if len(flags) < 20:
        raise RuntimeError("could not parse the node flags of controller/entrypoint.sh")
    for var, name in EXTRA_CONTROLLER_VARS.items():
        if var not in seen_vars and env.get(var, "") != "":
            flags.append(f"--{name}={env[var]}")
    return flags


def properties_escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace(":", "\\:").replace("=", "\\=")


def server_properties(game_port: int, rcon_port: int, seed: str, world_type: str,
                      generator_settings: Optional[dict], generate_structures: bool,
                      view_distance: int) -> Dict[str, str]:
    """server.properties of CONTRACT §2 (plus server-ip=127.0.0.1, so nothing
    listens on external interfaces and Windows Firewall does not prompt)."""
    props = {
        "server-ip": "127.0.0.1",
        "server-port": str(game_port),
        "enable-rcon": "true",
        "rcon.port": str(rcon_port),
        "rcon.password": "research",
        "broadcast-rcon-to-ops": "true",
        "online-mode": "false",
        "enforce-secure-profile": "false",
        "allow-flight": "true",
        "spawn-protection": "0",
        "gamemode": "survival",
        "pvp": "true",
        "difficulty": "peaceful",
        "view-distance": str(view_distance),
        "simulation-distance": str(view_distance),
        "level-seed": str(seed),
        "generate-structures": "true" if generate_structures else "false",
    }
    if world_type == "flat":
        props["level-type"] = "minecraft:flat"
        props["generator-settings"] = json.dumps(generator_settings, separators=(",", ":"))
    else:
        props["level-type"] = "minecraft:normal"
    return props


def plan_batch(args, tools: dict, batch_idx: int) -> dict:
    root = Path(args.output_dir).absolute() / "data_collection" / "train"
    batch_dir = root / f"batch_{batch_idx}"
    output_dir = batch_dir / "output"
    data_base = batch_dir / "data"
    cam_out = {b: batch_dir / "camera" / f"output_{b.lower()}" for b in BOTS}
    cam_data = {b: batch_dir / "camera" / f"data_{b.lower()}" for b in BOTS}
    world_plan = ["flat"] * args.num_flat + ["normal"] * args.num_normal

    world_state = {
        "logging": args.world_state_logging,
        "snapshot_mode": args.snapshot_mode if args.world_state_logging else "none",
        "end_snapshot": COMPOSE_DEFAULTS["world_end_snapshot"],
        "snapshot_radius": args.snapshot_radius,
        "snapshot_down": COMPOSE_DEFAULTS["world_snapshot_down"],
        "snapshot_up": COMPOSE_DEFAULTS["world_snapshot_up"],
        "log_radius": COMPOSE_DEFAULTS["world_log_radius"],
        "heartbeat_ticks": COMPOSE_DEFAULTS["world_heartbeat_ticks"],
        "reset_between_episodes": 1 if args.reset_world else 0,
        "deterministic_teleport": COMPOSE_DEFAULTS["deterministic_teleport"],
        "episode_weight_multipliers": args.weights,
        "state_random_tick_speed": args.random_tick_speed,
    }
    extra_cli = {
        "DUSK_NIGHT_FRACTION": args.dusk_night_fraction,
        "FIXED_TIME": args.fixed_time,
        "WORLD_ENTITIES_EVERY": args.world_entities_every,
        "WORLD_ENTITIES_RADIUS": args.world_entities_radius,
    }

    # Newer act_recorder.py versions can bind to loopback only
    recorder_has_host = '"--host"' in (REPO / "controller" / "act_recorder" / "act_recorder.py").read_text(
        encoding="utf-8", errors="replace")
    cam_prefix = shlex.split(args.camera_prefix)
    cam_env_extra = camera_env(args)
    cadence = recorder_cadence(args)

    instances = []
    for k, world_type in enumerate(world_plan):
        cfg = G.generate_compose_config(
            k,
            args.base_port,
            args.base_rcon_port,
            args.act_recorder_port,
            args.coord_port,
            str(data_base),
            str(output_dir),
            args.episodes,
            COMPOSE_DEFAULTS["episode_start_id"],
            COMPOSE_DEFAULTS["bootstrap_wait_time"],
            COMPOSE_DEFAULTS["episode_category"],
            args.types,
            1 if getattr(args, "smoke_test", False) else RUN_SH_COMPOSE_ARGS["smoke_test"],
            RUN_SH_COMPOSE_ARGS["viewer_rendering_disabled"],
            world_type,
            COMPOSE_DEFAULTS["render_distance"],
            COMPOSE_DEFAULTS["simulation_distance"],
            COMPOSE_DEFAULTS["graphics_mode"],
            str(cam_out["Alpha"]),
            str(cam_out["Bravo"]),
            str(cam_data["Alpha"]),
            str(cam_data["Bravo"]),
            COMPOSE_DEFAULTS["camera_alpha_vnc_base"],
            COMPOSE_DEFAULTS["camera_alpha_novnc_base"],
            COMPOSE_DEFAULTS["camera_bravo_vnc_base"],
            COMPOSE_DEFAULTS["camera_bravo_novnc_base"],
            COMPOSE_DEFAULTS["display_base"],
            COMPOSE_DEFAULTS["vnc_step"],
            COMPOSE_DEFAULTS["display_step"],
            gpu_device_id=0,
            gpu_mode=RUN_SH_COMPOSE_ARGS["gpu_mode"],
            disable_nvenc=COMPOSE_DEFAULTS["disable_nvenc"],
            eval_time_set_day=RUN_SH_COMPOSE_ARGS["eval_time_set_day"],
            flatland_world_disable_structures=bool(args.flat_no_structures),
            world_seed=args.world_seed,
            eula=args.eula or ("TRUE" if args.accept_minecraft_eula else "FALSE"),
            world_state=world_state,
            teleport_radius=args.teleport_radius,
            server_memory=args.server_memory,
            nvidia_disable_require=bool(args.nvidia_disable_require),
        )
        svc = cfg["services"]
        mc_env = svc[f"mc_instance_{k}"]["environment"]
        game_port = int(mc_env["SERVER_PORT"])
        rcon_port = int(mc_env["RCON_PORT"])
        seed = str(mc_env["SEED"])
        world_gen_info = json.loads(svc[f"controller_alpha_instance_{k}"]["environment"]["WORLD_GEN_INFO"])
        ports = {
            "game": game_port,
            "rcon": rcon_port,
            "act_recorder_alpha": args.act_recorder_port + NATIVE_PORT_STEP * k,
            "act_recorder_bravo": args.act_recorder_port + NATIVE_PORT_STEP * k + 1,
            "coord_alpha": args.coord_port + NATIVE_PORT_STEP * k,
            "coord_bravo": args.coord_port + NATIVE_PORT_STEP * k + 1,
        }
        if not args.no_cameras and not args.no_pose_link:
            # UDP, 127.0.0.1: the camera mod listens, the bot's controller sends
            ports["pose_alpha"] = args.pose_port + NATIVE_PORT_STEP * k
            ports["pose_bravo"] = args.pose_port + NATIVE_PORT_STEP * k + 1
        server_dir = data_base / str(k)
        logs_dir = batch_dir / "logs" / f"docker-compose-{k:03d}"
        services: Dict[str, dict] = {}

        # Paper server
        mem = str(mc_env.get("MEMORY") or args.server_memory or "1G")
        services[f"mc_instance_{k}"] = {
            "kind": "server",
            "cmd": [tools["java"] or "java", f"-Xms{mem}", f"-Xmx{mem}",
                    "-Dfile.encoding=UTF-8", "-Dstdout.encoding=UTF-8", "-Dstderr.encoding=UTF-8",
                    f"-DbundlerRepoDir={tools['paper_bundler_repo']}",
                    "-jar", tools["paper_jar"] or "paper.jar", "--nogui"],
            "cwd": str(server_dir),
            "env": {"WORLD_STATE_OUTPUT_DIR": str(output_dir)},
        }

        for bot in BOTS:
            other = "Bravo" if bot == "Alpha" else "Alpha"
            low = bot.lower()
            rec_env = {key: _env_str(v) for key, v in svc[f"act_recorder_{low}_instance_{k}"]["environment"].items()}
            rec_env["PORT"] = str(ports[f"act_recorder_{low}"])
            services[f"act_recorder_{low}_instance_{k}"] = {
                "kind": "act_recorder",
                "bot": bot,
                "cmd": [tools["python"], str(REPO / "controller" / "act_recorder" / "act_recorder.py"),
                        "--port", rec_env["PORT"], "--name", rec_env["NAME"],
                        "--output_path", str(output_dir), "--instance_id", rec_env["INSTANCE_ID"],
                        "--start_id", rec_env["EPISODE_START_ID"],
                        "--viewer_rendering_disabled", rec_env["VIEWER_RENDERING_DISABLED"],
                        *(["--host", "127.0.0.1"] if recorder_has_host else [])],
                "cwd": str(REPO),
                "env": rec_env,
            }

        if not args.no_cameras:
            for bot in BOTS:
                low = bot.lower()
                cam_env = svc[f"camera_{low}_instance_{k}"]["environment"]
                game_dir = cam_data[bot] / str(k)
                services[f"camera_{low}_instance_{k}"] = {
                    "kind": "camera",
                    "bot": bot,
                    "video_name": f"camera_{low}.mkv",
                    "stop_file": str(game_dir / STOP_FILE_NAME),
                    "cmd_base": [
                        *cam_prefix, tools["python"], tools["launch_camera"],
                        "--name", str(cam_env["CAMERA_NAME"]),
                        "--server", f"127.0.0.1:{game_port}",
                        "--mc-home", tools["mc_home"],
                        "--game-dir", str(game_dir),
                        "--output-dir", str(cam_out[bot] / str(k)),
                        # --video-name is inserted at start time (restarts use a suffix)
                        "--width", str(args.width),
                        "--height", str(args.height),
                        "--fps", str(args.fps or cam_env.get("FPS", 20)),
                        "--ffmpeg", tools["ffmpeg"] or "ffmpeg.exe",
                        "--encoder", args.encoder,
                        "--render-distance", str(args.render_distance),
                        "--gui-scale", str(args.gui_scale),
                        "--max-memory", args.camera_memory,
                        "--hide-window", "0" if args.show_cameras else "1",
                        "--stop-file", str(game_dir / STOP_FILE_NAME),
                        "--mod-jar", tools["capture_mod_jar"] or "<solaris-capture mod jar>",
                        "--pose-port", str(ports.get(f"pose_{low}", 0)),
                        *(["--java", str(Path(args.camera_java).absolute())] if args.camera_java else []),
                        *(["--offline"] if args.offline else []),
                        *([] if C.IS_WINDOWS else ["--keep-process-group"]),
                        *[x for o in camera_options_extra(args) for x in ("--extra-option", o)],
                    ],
                    "cwd": str(REPO),
                    "env": dict(cam_env_extra),
                }

        for bot in BOTS:
            low = bot.lower()
            sp_env = {key: _env_str(v) for key, v in svc[f"spectator_{low}_instance_{k}"]["environment"].items()}
            sp_env["MC_HOST"] = "127.0.0.1"
            services[f"spectator_{low}_instance_{k}"] = {
                "kind": "spectator",
                "cmd": [tools["node"] or "node", str(REPO / "spectator" / "spectator.js")],
                "cwd": str(REPO),
                "env": sp_env,
            }

        ps_env = {key: _env_str(v) for key, v in svc[f"plugin_starter_instance_{k}"]["environment"].items()}
        if args.no_cameras:
            # EpisodeManager's `episode start` tolerates absent camera players
            # (logs "Missing players for pair" and skips the pair), so only the
            # required-player list changes.
            ps_env["EPISODE_REQUIRED_PLAYERS"] = ",".join(
                n for n in ps_env["EPISODE_REQUIRED_PLAYERS"].split(",") if not n.startswith("Camera"))
        services[f"plugin_starter_instance_{k}"] = {
            "kind": "plugin_starter",
            "cmd": [tools["node"] or "node", str(REPO / "server" / "plugin-starter" / "plugin_starter.js")],
            "cwd": str(REPO / "server" / "plugin-starter"),
            "env": ps_env,
        }

        for bot in BOTS:
            low = bot.lower()
            other_low = "bravo" if bot == "Alpha" else "alpha"
            env = {key: _env_str(v) for key, v in svc[f"controller_{low}_instance_{k}"]["environment"].items()}
            env.update({
                "MC_HOST": "127.0.0.1",
                "RCON_HOST": "127.0.0.1",
                "OTHER_COORD_HOST": "127.0.0.1",
                "ACT_RECORDER_HOST": "127.0.0.1",
                "ACT_RECORDER_PORT": str(ports[f"act_recorder_{low}"]),
                "COORD_PORT": str(ports[f"coord_{low}"]),
                "OTHER_COORD_PORT": str(ports[f"coord_{other_low}"]),
                "OUTPUT_DIR": str(output_dir),
            })
            if args.bootstrap_wait_time is not None:
                env["BOOTSTRAP_WAIT_TIME"] = str(args.bootstrap_wait_time)
            if args.no_cameras:
                env["ENABLE_CAMERA_WAIT"] = "0"
            # T12 pose link (controller/recorder/camera-pose-link.js); "0" = off
            env["CAMERA_POSE_PORT"] = str(ports.get(f"pose_{low}", 0))
            # skins.js defaults to signed skins off Windows; the cameras run the mod
            # (local skins) on every OS. --offline: local only (no signed fallback)
            if args.offline:
                env["SKIN_MODE"] = "local"
            elif not C.IS_WINDOWS and not os.environ.get("SKIN_MODE"):
                env["SKIN_MODE"] = "auto"
            if cadence:  # frame-recorder.js: 50 ms deadline grid (Windows default) or "after"
                env["RECORDER_CADENCE"] = cadence
            for var in EXTRA_CONTROLLER_VARS:
                val = extra_cli.get(var)
                if val is None:
                    val = env.get(var) or os.environ.get(var)
                if val not in (None, ""):
                    env[var] = str(val)
            services[f"controller_{low}_instance_{k}"] = {
                "kind": "controller",
                "bot": bot,
                "cmd": [tools["node"] or "node", str(REPO / "controller" / "main.js"), *controller_flags(env)],
                "cwd": str(REPO),
                "env": env,
            }

        for name, s in services.items():
            s["log"] = str(logs_dir / f"{name}.log")

        instances.append({
            "instance_id": k,
            "world_type": world_type,
            "seed": seed,
            "world_gen_info": world_gen_info,
            "compose_project": f"docker-compose-{k:03d}",
            "ports": ports,
            "server_dir": str(server_dir),
            "server_properties": server_properties(
                game_port, rcon_port, seed, world_type, world_gen_info.get("generator_settings"),
                bool(world_gen_info.get("generate_structures", True)), args.render_distance),
            "eula_txt": bool(args.accept_minecraft_eula),
            "camera_dirs": {b: {"output": str(cam_out[b] / str(k)), "data": str(cam_data[b] / str(k))}
                            for b in BOTS},
            "logs_dir": str(logs_dir),
            "services": services,
            "_compose_config": cfg,
        })

    collisions = port_plan_collisions(instances)
    if collisions:
        raise SystemExit("port plan collision (change --base-port/--base-rcon-port/--act-recorder-port/"
                         "--coord-port/--pose-port): " + "; ".join(collisions))

    plan = {
        "batch": batch_idx,
        "batch_dir": str(batch_dir),
        "output_dir": str(output_dir),
        "aligned_dir": str(batch_dir / "aligned"),
        "compose_dir": str(batch_dir / "compose_configs"),
        "instances": instances,
    }
    xvfb = xvfb_service(args, batch_dir)
    if xvfb:  # one display for every camera of the batch
        plan["batch_services"] = {"xvfb": xvfb}
    return plan


def port_plan_collisions(instances: List[dict]) -> List[str]:
    """Ports used by more than one role across all instances (game, RCON, recorders, coord, pose)."""
    users: Dict[int, List[str]] = {}
    for inst in instances:
        for key, port in inst["ports"].items():
            users.setdefault(int(port), []).append(f"instance {inst['instance_id']} {key}")
    return [f"{port}: {', '.join(names)}" for port, names in sorted(users.items()) if len(names) > 1]


def udp_port_in_use(port: int, host: str = "127.0.0.1") -> bool:
    import socket
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        try:
            s.bind((host, port))
        except OSError:
            return True
    return False


def service_cmd(spec: dict, video_name: Optional[str] = None) -> List[str]:
    if spec["kind"] != "camera":
        return list(spec["cmd"])
    cmd = list(spec["cmd_base"])
    i = cmd.index("--width")
    return cmd[:i] + ["--video-name", video_name or spec["video_name"]] + cmd[i:]


def public_plan(plan: dict) -> dict:
    """The plan without private keys, with camera commands materialised."""
    out = {k: v for k, v in plan.items() if k != "instances"}
    if "batch_services" in plan:
        out["batch_services"] = {n: dict(s) for n, s in plan["batch_services"].items()}
    out["instances"] = []
    for inst in plan["instances"]:
        i2 = {k: v for k, v in inst.items() if not k.startswith("_")}
        i2["services"] = {}
        for name, s in inst["services"].items():
            s2 = {k: v for k, v in s.items() if k != "cmd_base"}
            s2["cmd"] = service_cmd(s)
            i2["services"][name] = s2
        out["instances"].append(i2)
    return out


def child_base_env(tools: dict) -> Dict[str, str]:
    env = dict(os.environ)
    prepend = [d for d in (tools.get("ffmpeg_dir"),
                           str(Path(tools["node"]).parent) if tools.get("node") else None) if d]
    env["PATH"] = os.pathsep.join(prepend + [env.get("PATH", "")])
    if tools.get("ffmpeg_dir"):  # read by postprocess/process_recordings.py
        env["FFMPEG"] = str(Path(tools["ffmpeg_dir"]) / f"ffmpeg{C.EXE}")
        env["FFPROBE"] = str(Path(tools["ffmpeg_dir"]) / f"ffprobe{C.EXE}")
    env["PYTHONUNBUFFERED"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    return env


# ---------------------------------------------------------------------------
# Batch runner
# ---------------------------------------------------------------------------

class BatchRunner:
    def __init__(self, args, tools: dict, versions: dict, plan: dict, git: dict):
        self.args, self.tools, self.versions, self.plan, self.git = args, tools, versions, plan, git
        self.batch_dir = Path(plan["batch_dir"])
        self.base_env = child_base_env(tools)
        self.procs: Dict[str, C.ManagedProcess] = {}
        self.records: Dict[str, dict] = {}  # service -> run_config info
        self.status = "created"
        self.abort_reason: Optional[str] = None
        self.timeline: List[dict] = []
        self.postprocess: dict = {}
        self._log_fh = None
        self.t0 = time.monotonic()

    # -- console + logs/run_windows.log --
    def say(self, msg: str, loud: bool = False) -> None:
        if loud:
            msg = "!" * 8 + " " + msg + " " + "!" * 8
        C.log(msg)
        if self._log_fh is not None:
            self._log_fh.write(f"{C.utc_now_iso()} {msg}\n")
            self._log_fh.flush()

    def mark(self, event: str, **kw) -> None:
        self.timeline.append({"utc": C.utc_now_iso(), "event": event, **kw})

    # -- filesystem --
    def prepare_dirs(self) -> None:
        bd = self.batch_dir
        (bd / "logs").mkdir(parents=True, exist_ok=True)
        self._log_fh = open(bd / "logs" / "run_windows.log", "a", encoding="utf-8")
        out = Path(self.plan["output_dir"])
        if out.is_dir() and any(out.iterdir()):
            self.say(f"note: {out} already contains files; they are kept (same as the Docker path)")
        for d in (out, Path(self.plan["aligned_dir"]), Path(self.plan["compose_dir"])):
            d.mkdir(parents=True, exist_ok=True)
        for inst in self.plan["instances"]:
            Path(inst["logs_dir"]).mkdir(parents=True, exist_ok=True)
            for b in BOTS:
                for d in inst["camera_dirs"][b].values():
                    Path(d).mkdir(parents=True, exist_ok=True)
            self.write_server_dir(inst)
            self.write_compose_file(inst)

    def write_server_dir(self, inst: dict) -> None:
        sd = Path(inst["server_dir"])
        sd.mkdir(parents=True, exist_ok=True)
        lines = ["# Written by windows/run_windows.py (CONTRACT §2)"]
        lines += [f"{k}={properties_escape(v)}" for k, v in inst["server_properties"].items()]
        existing = {}
        props_file = sd / "server.properties"
        if props_file.is_file():
            # Keep keys Paper added on a previous run, override ours
            for line in props_file.read_text(encoding="utf-8", errors="replace").splitlines():
                if "=" in line and not line.lstrip().startswith("#"):
                    key = line.split("=", 1)[0].strip()
                    if key not in inst["server_properties"]:
                        existing[key] = line
        lines += list(existing.values())
        props_file.write_text("\n".join(lines) + "\n", encoding="utf-8")
        if inst["eula_txt"]:
            (sd / "eula.txt").write_text(
                "# Accepted by the operator via run_windows.py --accept-minecraft-eula "
                "(https://aka.ms/MinecraftEULA)\neula=true\n", encoding="utf-8")
        # prep_data service: copy plugins + skins
        prep_log = Path(inst["logs_dir"]) / f"prep_data_instance_{inst['instance_id']}.log"
        msgs = []
        for sub in ("plugins", "skins"):
            src, dst = REPO / "server" / sub, sd / sub
            dst.mkdir(parents=True, exist_ok=True)
            if src.is_dir():
                shutil.copytree(src, dst, dirs_exist_ok=True)
                msgs.append(f"copied {src} -> {dst}")
        if self.args.offline:
            msgs += offline_server_config(sd)
            for m in msgs:
                if m.startswith("WARNING"):
                    self.say(m, loud=True)
        with open(prep_log, "a", encoding="utf-8") as fh:
            for m in msgs:
                fh.write(f"{C.utc_now_iso()} {m}\n")

    def write_compose_file(self, inst: dict) -> None:
        """Reference copy of the generate_compose.py config the native env was
        built from (same file name as the Docker path; not used for running)."""
        import yaml
        path = Path(self.plan["compose_dir"]) / f"docker-compose-{inst['instance_id']:03d}.yml"
        cfg = inst["_compose_config"]
        text = yaml.dump(cfg, default_flow_style=False, sort_keys=False)
        if inst["world_type"] == "flat":
            gs = json.dumps(inst["world_gen_info"]["generator_settings"], separators=(",", ":"))
            text = text.replace("GENERATOR_SETTINGS: TERRAIN_SETTINGS_PLACEHOLDER",
                                f"GENERATOR_SETTINGS: '{gs}'")
        header = ("# Reference only: generated by windows/run_windows.py from generate_compose.py;\n"
                  "# the Windows-native runner does not use Docker. See run_config.json.\n")
        path.write_text(header + text, encoding="utf-8")

    def write_run_config(self) -> None:
        pub = public_plan(self.plan)
        for inst in pub["instances"]:
            for name, s in inst["services"].items():
                s.update(self.records.get(name, {}))
        for name, s in pub.get("batch_services", {}).items():
            s.update(self.records.get(name, {}))
        data = {
            "schema": "solaris-windows-run-config/1",
            "written_utc": C.utc_now_iso(),
            "runner": "windows/run_windows.py",
            "argv": sys.argv,
            "options": {k: v for k, v in vars(self.args).items()},
            "status": self.status,
            "abort_reason": self.abort_reason,
            "git": self.git,
            "tools": self.tools,
            "tool_versions": self.versions,
            "camera": None if self.args.no_cameras else {
                "width": self.args.width, "height": self.args.height,
                "fps": self.args.fps or 20, "encoder": self.args.encoder,
                "render_distance": self.args.render_distance, "gui_scale": self.args.gui_scale,
                "hide_window": not self.args.show_cameras, "max_memory": self.args.camera_memory,
                "pose_link": not self.args.no_pose_link,
            },
            "env_note": "service env = values below on top of the environment of run_windows.py "
                        "(PATH prefixed with the ffmpeg and node directories; PYTHONUNBUFFERED=1, "
                        "PYTHONIOENCODING=utf-8, PYTHONUTF8=1)",
            "batch": pub,
            "timeline": self.timeline,
            "postprocess": self.postprocess,
        }
        path = Path(self.plan["compose_dir"]) / "run_config.json"
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data, indent=2, default=str), encoding="utf-8")
        os.replace(tmp, path)

    # -- processes --
    def _spec(self, name: str) -> dict:
        if name in self.plan.get("batch_services", {}):
            return self.plan["batch_services"][name]
        for inst in self.plan["instances"]:
            if name in inst["services"]:
                return inst["services"][name]
        raise KeyError(name)

    def start_service(self, name: str, video_name: Optional[str] = None) -> C.ManagedProcess:
        spec = self._spec(name)
        cmd = service_cmd(spec, video_name)
        env = dict(self.base_env)
        env.update(spec["env"])
        if spec["kind"] == "camera":
            try:
                Path(spec["stop_file"]).unlink()
            except FileNotFoundError:
                pass
        proc = C.ManagedProcess(name, cmd, env, Path(spec["cwd"]), Path(spec["log"]),
                                stdin_pipe=spec["kind"] == "server")
        proc.start()
        self.procs[name] = proc
        rec = self.records.setdefault(name, {"runs": []})
        run = {"pid": proc.pid, "started_utc": proc.started_utc}
        if rec["runs"]:  # restart: the plan's cmd is the first run's
            run["cmd"] = cmd
        if spec["kind"] == "camera":
            run["video_name"] = video_name or spec["video_name"]
        rec["runs"].append(run)
        self.say(f"started {name} (pid {proc.pid})")
        return proc

    def _camera_files_exist(self, spec: dict) -> bool:
        out = Path(spec["cmd_base"][spec["cmd_base"].index("--output-dir") + 1])
        stem = spec["video_name"].rsplit(".", 1)[0]
        return any((out / f).exists() for f in (spec["video_name"], f"{stem}_meta.json", f"{stem}_frames.jsonl"))

    def _record_exit(self, name: str) -> None:
        proc = self.procs.get(name)
        rec = self.records.get(name)
        if proc is None or rec is None or not rec["runs"]:
            return
        run = rec["runs"][-1]
        if "exit_code" not in run and proc.poll() is not None:
            run["exit_code"] = proc.poll()
            run["ended_utc"] = proc.ended_utc

    def services_of(self, kind: str, inst: Optional[dict] = None) -> List[str]:
        insts = [inst] if inst else self.plan["instances"]
        return [n for i in insts for n, s in i["services"].items() if s["kind"] == kind]

    def check_ports(self) -> List[str]:
        busy = []
        for inst in self.plan["instances"]:
            for key, port in inst["ports"].items():
                in_use = udp_port_in_use(port) if key.startswith("pose_") else C.port_in_use(port)
                if in_use:
                    busy.append(f"{key}={port}")
        return busy

    def wait_server_ready(self, inst: dict) -> bool:
        name = f"mc_instance_{inst['instance_id']}"
        proc = self.procs[name]
        deadline = time.monotonic() + self.args.server_ready_timeout
        port = inst["ports"]["rcon"]
        self.say(f"waiting for {name} RCON on 127.0.0.1:{port} "
                 f"(first start downloads/patches Minecraft, may take minutes)")
        last_err = None
        while time.monotonic() < deadline:
            if stop_requested():
                return False
            if not proc.alive():
                self.say(f"{name} exited with code {proc.poll()} before becoming ready; see {proc.log_path}",
                         loud=True)
                return False
            try:
                resp = C.rcon_command("127.0.0.1", port, "research", "list", timeout=5)
                self.say(f"{name} ready: {resp.strip()}")
                return True
            except (OSError, C.RconError) as exc:
                last_err = exc
            time.sleep(2)
        self.say(f"{name} not ready after {self.args.server_ready_timeout:.0f}s ({last_err})", loud=True)
        return False

    def sleep_checked(self, seconds: float) -> None:
        end = time.monotonic() + seconds
        while time.monotonic() < end and not stop_requested():
            time.sleep(min(0.5, max(0.0, end - time.monotonic())))

    def sleep_until(self, done, seconds: float) -> None:
        """Sleep up to `seconds` (ignores a pending clean-stop request)."""
        end = time.monotonic() + seconds
        while time.monotonic() < end and not done() and not _STOP["force"]:
            time.sleep(0.25)

    def count_episodes(self) -> Dict[str, int]:
        out = Path(self.plan["output_dir"])
        return {b: len(list(out.glob(f"*_{b}_instance_*_episode_info.json"))) for b in BOTS}

    def run(self) -> str:
        """Start, monitor and stop one batch. Returns the final status."""
        self.prepare_dirs()
        self.status = "starting"
        self.mark("batch_start")
        busy = self.check_ports()
        if busy:
            self.status = "aborted"
            self.abort_reason = f"ports already in use: {', '.join(busy)}"
            self.say(self.abort_reason + " (is another run still active?)", loud=True)
            self.write_run_config()
            return self.status
        self.write_run_config()
        try:
            self._start_all()
            if self.status == "running":
                self._monitor()
        finally:
            self._shutdown()
            self.write_run_config()
        return self.status

    def _start_display(self) -> bool:
        """Linux --xvfb: start the batch's Xvfb and wait until it accepts connections."""
        spec = self.plan.get("batch_services", {}).get("xvfb")
        if spec is None:
            return True
        display, n = spec["display"], x_display_number(spec["display"])
        lock, sock = Path(f"/tmp/.X{n}-lock"), Path(f"/tmp/.X11-unix/X{n}")
        if lock.is_file():
            try:
                pid = int(lock.read_text(encoding="utf-8", errors="replace").strip())
                os.kill(pid, 0)
                self.say(f"display {display} is in use (pid {pid} in {lock}); pass another --display", loud=True)
                return False
            except PermissionError:
                self.say(f"display {display} is in use ({lock}); pass another --display", loud=True)
                return False
            except (ValueError, OSError):  # lock of an X server that is gone
                self.say(f"removing stale {lock}")
                for p in (lock, sock):
                    try:
                        p.unlink()
                    except OSError:
                        pass
        proc = self.start_service("xvfb")
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline and not stop_requested():
            if not proc.alive():
                self.say(f"Xvfb exited with code {proc.poll()}; see {proc.log_path}", loud=True)
                return False
            if x_display_ready(display):
                self.say(f"Xvfb ready on {display}")
                self.mark("display_ready", display=display)
                return True
            time.sleep(0.2)
        self.say(f"Xvfb on {display} not accepting connections after 30s; see {proc.log_path}", loud=True)
        return False

    def _start_all(self) -> None:
        if not self._start_display():
            self.status = "aborted"
            self.abort_reason = "interrupted" if stop_requested() else "Xvfb did not start"
            return
        insts = self.plan["instances"]
        # Paperclip extracts its libraries into the shared bundler repo on first
        # use; two servers doing that at once corrupt each other's files ("Hash
        # check failed"). Until the repo is known to be warm, let the first server
        # finish extracting (= become ready) before starting the others.
        repo_marker = Path(self.tools["paper_bundler_repo"]) / ".solaris_extracted"
        cold_repo = len(insts) > 1 and not repo_marker.exists()
        for i, inst in enumerate(insts):
            self.start_service(f"mc_instance_{inst['instance_id']}")
            if cold_repo and i == 0:
                if not self.wait_server_ready(inst):
                    self.status = "aborted"
                    self.abort_reason = "interrupted" if stop_requested() else \
                        f"server mc_instance_{inst['instance_id']} not ready"
                    return
                repo_marker.write_text("ok\n", encoding="utf-8")
        for inst in insts:
            if not self.wait_server_ready(inst):
                self.status = "aborted"
                self.abort_reason = "interrupted" if stop_requested() else \
                    f"server mc_instance_{inst['instance_id']} not ready"
                return
        self.mark("servers_ready")
        for name in self.services_of("act_recorder"):
            self.start_service(name)
        self.sleep_checked(1.5)
        for name in self.services_of("act_recorder"):
            if not self.procs[name].alive():
                self.status = "aborted"
                self.abort_reason = f"{name} exited at startup (code {self.procs[name].poll()})"
                self.say(f"{self.abort_reason}; see {self.procs[name].log_path}", loud=True)
                return
        for kind in ("camera", "spectator", "plugin_starter"):
            for name in self.services_of(kind):
                self.start_service(name)
        for inst in insts:
            k = inst["instance_id"]
            self.start_service(f"controller_alpha_instance_{k}")
            self.sleep_checked(2)
            self.start_service(f"controller_bravo_instance_{k}")
        if stop_requested():
            self.status = "aborted"
            self.abort_reason = "interrupted"
            return
        self.status = "running"
        self.mark("all_started")
        self.write_run_config()
        if not self.args.no_cameras:
            self.say("cameras record in the background (hidden windows); controllers wait for them to join")

    def _monitor(self) -> None:
        args = self.args
        started = time.monotonic()
        last_status = 0.0
        spectator_restarts: Dict[str, int] = {}
        camera_restarts: Dict[str, int] = {}
        # The partner grace is per instance: instances run their episodes
        # independently and may legitimately finish minutes apart.
        inst_ctrls = {inst["instance_id"]: self.services_of("controller", inst)
                      for inst in self.plan["instances"]}
        first_ctrl_exit: Dict[int, float] = {}
        grace_killed: List[str] = []
        controllers = [n for names in inst_ctrls.values() for n in names]
        while True:
            now = time.monotonic()
            if stop_requested():
                self.status = "interrupted"
                self.abort_reason = _STOP["reason"]
                self.say("stop requested by the user" if _STOP["reason"] == "Ctrl+C" else
                         f"stop requested ({_STOP['reason']})")
                return
            if args.batch_timeout and now - started > args.batch_timeout:
                self.status = "timeout"
                self.say(f"batch timeout ({args.batch_timeout:.0f}s) reached", loud=True)
                return
            for inst in self.plan["instances"]:
                srv = f"mc_instance_{inst['instance_id']}"
                if not self.procs[srv].alive():
                    self._record_exit(srv)
                    self.status = "aborted"
                    self.abort_reason = f"{srv} died (exit code {self.procs[srv].poll()})"
                    self.say(f"{self.abort_reason}; aborting batch. See {self.procs[srv].log_path}", loud=True)
                    return
            xvfb = self.procs.get("xvfb")
            if xvfb is not None and not xvfb.alive():
                self._record_exit("xvfb")
                self.status = "aborted"
                self.abort_reason = f"xvfb died (exit code {xvfb.poll()})"
                self.say(f"{self.abort_reason}; the cameras lost their display, aborting batch. "
                         f"See {xvfb.log_path}", loud=True)
                return
            ctrl_alive = [n for n in controllers if self.procs[n].alive()]
            for n in controllers:
                if not self.procs[n].alive():
                    self._record_exit(n)
            if not ctrl_alive:
                codes = {n: self.procs[n].poll() for n in controllers}
                self.status = "completed_with_kills" if grace_killed else "completed"
                self.say(f"all controllers exited: {codes}"
                         f"{'; stopped after the partner grace: ' + ', '.join(grace_killed) if grace_killed else ''}",
                         loud=bool(grace_killed) or any(c != 0 for c in codes.values()))
                self.mark("controllers_exited", exit_codes=codes, grace_killed=grace_killed)
                return
            for k, names in inst_ctrls.items():
                alive = [n for n in names if n in ctrl_alive]
                if not alive or len(alive) == len(names):
                    continue
                if k not in first_ctrl_exit:
                    first_ctrl_exit[k] = now
                    done = [n for n in names if n not in alive]
                    self.say(f"{', '.join(done)} exited (code {[self.procs[n].poll() for n in done]}); "
                             f"waiting up to {args.controller_exit_grace:.0f}s for {', '.join(alive)}")
                elif now - first_ctrl_exit[k] > args.controller_exit_grace:
                    self.say(f"{', '.join(alive)} still running {args.controller_exit_grace:.0f}s after "
                             f"its partner exited; stopping it", loud=True)
                    for n in alive:
                        self.procs[n].kill()
                        self._record_exit(n)
                        self.records[n]["killed_after_partner_grace"] = True
                        grace_killed.append(n)
                        self.mark("controller_grace_kill", service=n, instance=k)
                    self.write_run_config()
            for name in self.services_of("act_recorder"):
                proc = self.procs[name]
                if not proc.alive() and not self.records[name].get("death_reported"):
                    self._record_exit(name)
                    self.records[name]["death_reported"] = True
                    self.say(f"{name} exited unexpectedly (code {proc.poll()}); its bot's frames "
                             f"will be missing. See {proc.log_path}", loud=True)
            for name in self.services_of("camera"):
                proc = self.procs[name]
                if proc.alive():
                    continue
                self._record_exit(name)
                n = camera_restarts.get(name, 0)
                if n >= 1:
                    if not self.records[name].get("death_reported"):
                        self.records[name]["death_reported"] = True
                        self.say(f"{name} exited again (code {proc.poll()}); not restarting. Its recording "
                                 f"is incomplete. See {proc.log_path}", loud=True)
                    continue
                camera_restarts[name] = n + 1
                spec = self._spec(name)
                video = spec["video_name"]
                if self._camera_files_exist(spec):
                    # Never overwrite a (partial) recording: the restart gets its own files
                    video = video.replace(".mkv", f"_r{n + 1}.mkv")
                    note = ("the first recording is kept; alignment picks, per episode, the recording "
                            "that covers its action times")
                else:
                    note = "it had not written any recording yet, so the normal file names are reused"
                # EpisodeManager.onQuit kicks the partner of a player that leaves an
                # active pair, i.e. this camera's controller bot (cross-component)
                self.say(f"CAMERA {name} EXITED EARLY (code {proc.poll()}); restarting it once with "
                         f"--video-name {video} ({note}). The server has most likely kicked controller "
                         f"{spec['bot']} too, so its current episode is lost and the instance may not "
                         f"recover. See {proc.log_path}", loud=True)
                proc.write_log_line(f"[run_windows] exited early with code {proc.poll()}; restarting as {video}")
                proc.close_log()
                self.records[name].setdefault("restarts", []).append(
                    {"utc": C.utc_now_iso(), "previous_exit_code": proc.poll(), "video_name": video})
                self.mark("camera_restart", service=name, video_name=video)
                self.start_service(name, video_name=video)
                self.write_run_config()
            for name in self.services_of("spectator"):
                proc = self.procs[name]
                if not proc.alive():
                    self._record_exit(name)
                    n = spectator_restarts.get(name, 0)
                    if n < 50:  # compose: restart unless-stopped
                        spectator_restarts[name] = n + 1
                        self.say(f"{name} exited (code {proc.poll()}); restarting")
                        proc.close_log()
                        self.start_service(name)
            for name in self.services_of("plugin_starter"):
                proc = self.procs[name]
                if not proc.alive() and "exit_code" not in self.records[name]["runs"][-1]:
                    self._record_exit(name)
                    rc = proc.poll()
                    if rc == 0:
                        self.say(f"{name} sent the episode start command")
                    else:
                        self.say(f"{name} failed (code {rc}); see {proc.log_path}", loud=True)
            if now - last_status >= 60:
                last_status = now
                eps = self.count_episodes()
                self.say(f"running {int(now - started)}s: episodes done Alpha={eps['Alpha']} "
                         f"Bravo={eps['Bravo']} of {args.episodes}/instance; "
                         f"controllers alive: {len(ctrl_alive)}")
            time.sleep(1)

    def _shutdown(self) -> None:
        self.mark("shutdown_start", status=self.status)
        if _STOP["force"]:
            self.kill_all()
            return
        cams = [n for n in self.services_of("camera") if n in self.procs]
        live = [n for n in cams if self.procs[n].alive()]
        if live:
            for n in live:
                stop_file = Path(self._spec(n)["stop_file"])
                stop_file.parent.mkdir(parents=True, exist_ok=True)
                stop_file.write_text(C.utc_now_iso() + "\n", encoding="utf-8")
            self.say(f"stop files written for {', '.join(live)}; waiting up to "
                     f"{self.args.camera_stop_timeout:.0f}s for the recordings to close")
            deadline = time.monotonic() + self.args.camera_stop_timeout
            while time.monotonic() < deadline and any(self.procs[n].alive() for n in live):
                if _STOP["force"]:
                    break
                time.sleep(0.5)
            for n in live:
                if self.procs[n].alive():
                    self.say(f"{n} did not exit after its stop file; killing it (its MKV may be truncated)",
                             loud=True)
        for n in cams:
            self.procs[n].stop(grace=0)
        for kind in ("controller", "spectator", "plugin_starter"):
            for n in self.services_of(kind):
                if n in self.procs:
                    self.procs[n].stop(grace=0)
        # The recorders serve forever; give them a moment to finish writing
        recorders = [n for n in self.services_of("act_recorder") if n in self.procs]
        if any(self.procs[n].alive() for n in recorders):
            self.say(f"stopping {', '.join(recorders)} in 5s")
            self.sleep_until(lambda: not any(self.procs[n].alive() for n in recorders), 5)
        for n in recorders:
            self.procs[n].stop(grace=0)
        for inst in self.plan["instances"]:
            n = f"mc_instance_{inst['instance_id']}"
            proc = self.procs.get(n)
            if proc is None:
                continue
            if proc.alive():
                self.say(f"stopping {n} via RCON 'stop' (saving the world)")
                try:
                    C.rcon_command("127.0.0.1", inst["ports"]["rcon"], "research", "stop", timeout=10)
                except (OSError, C.RconError) as exc:
                    self.say(f"RCON stop failed ({exc}); sending 'stop' on the console")
                    proc.send_stdin("stop\n")
                if proc.wait(timeout=90) is None:
                    proc.send_stdin("stop\n")
                    if proc.wait(timeout=15) is None:
                        self.say(f"{n} did not stop; killing it", loud=True)
            proc.stop(grace=0)
        xvfb = self.procs.get("xvfb")  # last: the cameras needed the display until they exited
        if xvfb is not None:
            xvfb.terminate()
            xvfb.stop(grace=5)
        for n, proc in self.procs.items():
            self._record_exit(n)
            proc.close_log()
        self.mark("shutdown_done")

    def kill_all(self) -> None:
        for n, proc in list(self.procs.items()):
            try:
                proc.kill()
                self._record_exit(n)
                proc.close_log()
            except Exception:  # noqa: BLE001
                pass
        self.mark("killed_all")

    # -- postprocess --
    def run_postprocess(self) -> None:
        if self.args.skip_postprocess:
            self.postprocess["skipped"] = "--skip-postprocess"
            return
        if sigterm_stop():
            self.postprocess["skipped"] = "SIGTERM"
            self.say("SIGTERM: alignment and verification skipped; run them later "
                     "(orchestrate.py postprocess, linux/README.md)")
            self.write_run_config()
            return
        log_dir = self.batch_dir / "logs" / "postprocess"
        log_dir.mkdir(parents=True, exist_ok=True)
        env = dict(self.base_env)
        if self.args.no_cameras:
            self.say("alignment skipped (--no-cameras)")
            self.postprocess["alignment"] = {"skipped": "--no-cameras"}
        else:
            self.postprocess["alignment"] = align_batch(
                self.batch_dir, self.tools["python"], env, self.args.workers, log_dir, self.say)
        self.write_run_config()
        if self.args.world_state_logging and sigterm_stop():
            self.postprocess["verify_world_events"] = {"skipped": "SIGTERM"}
        elif self.args.world_state_logging:
            report = self.batch_dir / "world_events_report.json"
            cmd = [self.tools["python"], str(REPO / "postprocess" / "verify_world_events.py"),
                   str(self.batch_dir), "--report", str(report)]
            self.say(f"verifying world events: {subprocess.list2cmdline(cmd)}")
            r = subprocess.run(cmd, cwd=str(REPO), env=env, capture_output=True, text=True,
                               encoding="utf-8", errors="replace")
            out = (r.stdout or "") + (r.stderr or "")
            (log_dir / "verify_world_events.log").write_text(out, encoding="utf-8")
            print(out.rstrip(), flush=True)
            if r.returncode != 0:
                self.say(f"verify_world_events.py reported problems for {self.batch_dir} (exit {r.returncode})")
            self.postprocess["verify_world_events"] = {"exit_code": r.returncode, "report": str(report),
                                                       "log": str(log_dir / "verify_world_events.log")}
        self.write_run_config()

    def summary(self) -> dict:
        out = Path(self.plan["output_dir"])
        aligned = Path(self.plan["aligned_dir"])
        return {
            "batch_dir": str(self.batch_dir),
            "status": self.status,
            "abort_reason": self.abort_reason,
            "episodes": self.count_episodes(),
            "action_json": len([p for p in out.glob("*_instance_*.json")
                                if not p.name.endswith(("_meta.json", "_episode_info.json"))]),
            "world_events": len(list(out.glob("*_world_events.jsonl"))),
            "aligned_mp4": len(list(aligned.glob("*_camera.mp4"))),
            "camera_restarts": {n: len(r.get("restarts", [])) for n, r in self.records.items()
                                if r.get("restarts")},
            "verify_exit_code": (self.postprocess.get("verify_world_events") or {}).get("exit_code"),
        }


def align_batch(batch_dir: Path, python: str, env: dict, workers: int, log_dir: Path, say) -> dict:
    """Docker-free equivalent of `orchestrate.py postprocess`: run
    postprocess/process_recordings.py for every action JSON of the batch."""
    actions_dir = batch_dir / "output"
    aligned = batch_dir / "aligned"
    script = REPO / "postprocess" / "process_recordings.py"
    jobs = []
    skipped = []
    for json_path in sorted(actions_dir.glob("*.json")):
        if json_path.name.endswith(("_meta.json", "_episode_info.json")):
            continue
        bot = "Alpha" if "_Alpha_" in json_path.name else "Bravo" if "_Bravo_" in json_path.name else None
        if bot is None:
            continue
        m = re.search(r"_instance_(\d+)", json_path.name)
        inst = int(m.group(1)) if m else 0
        prefix = batch_dir / "camera" / f"output_{bot.lower()}" / str(inst)
        # A restarted camera writes camera_<bot>_r<n>_meta.json; process_recordings.py
        # (select_camera_meta) picks the recording covering each episode
        stem = f"camera_{bot.lower()}"
        if not ((prefix / f"{stem}_meta.json").is_file() or any(prefix.glob(f"{stem}_r*_meta.json"))):
            skipped.append(json_path.name)
            continue
        jobs.append((json_path, bot, inst, prefix))
    if skipped:
        say(f"alignment: {len(skipped)} action file(s) have no camera meta, skipped")
    if not jobs:
        say("alignment: no episodes to process")
        return {"jobs": 0, "ok": 0, "failed": 0, "skipped_no_camera_meta": skipped}
    say(f"alignment: {len(jobs)} episode recordings, {workers} workers")

    def one(job):
        json_path, bot, inst, prefix = job
        if stop_requested() and (_STOP["force"] or sigterm_stop()):
            return "skipped"
        cmd = [python, str(script), "--bot", bot, "--actions-dir", str(actions_dir),
               "--camera-prefix", str(prefix), "--episode-file", str(json_path), "--output-dir", str(aligned)]
        try:
            r = subprocess.run(cmd, cwd=str(REPO), env=env, capture_output=True, text=True,
                               encoding="utf-8", errors="replace", timeout=600)
            rc, text = r.returncode, (r.stdout or "") + (r.stderr or "")
        except subprocess.TimeoutExpired:
            rc, text = -1, "timeout after 600 s"
        (log_dir / f"align_{json_path.stem}.log").write_text(
            subprocess.list2cmdline(cmd) + "\n" + text, encoding="utf-8")
        return rc

    ok, failed = 0, []
    with ThreadPoolExecutor(max_workers=max(1, workers)) as ex:
        futures = {ex.submit(one, j): j for j in jobs}
        for i, fut in enumerate(as_completed(futures), 1):
            j = futures[fut]
            rc = fut.result()
            status = "ok" if rc == 0 else rc if rc == "skipped" else f"FAILED ({rc})"
            say(f"[{i}/{len(jobs)}] {status} {j[1]} instance {j[2]}: {j[0].stem}")
            if rc == 0:
                ok += 1
            else:
                failed.append(j[0].name)
    say(f"alignment: {ok}/{len(jobs)} ok; logs in {log_dir}")
    return {"jobs": len(jobs), "ok": ok, "failed": len(failed), "failed_files": failed,
            "skipped_no_camera_meta": skipped, "logs": str(log_dir)}


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def main(argv=None) -> int:
    for stream in (sys.stdout, sys.stderr):  # never die on a cp936 console / pipe
        try:
            stream.reconfigure(errors="backslashreplace")
        except (AttributeError, ValueError):
            pass
    args = parse_cli(list(sys.argv[1:] if argv is None else argv))
    total = args.num_flat + args.num_normal
    if total < 1:
        print("Error: no instances (num-flat-world + num-normal-world = 0)", file=sys.stderr)
        return 1
    if total > 4:
        print(f"Error: total instances ({total}) exceeds the NVENC limit of 8 simultaneous encoding "
              f"sessions per GPU; use 4 or fewer.", file=sys.stderr)
        return 1
    if not args.accept_minecraft_eula and not args.dry_run:
        print(EULA_MESSAGE, file=sys.stderr)
        return 1

    tools = resolve_tools(args)
    root = Path(args.output_dir).absolute()

    if args.dry_run:
        if not args.accept_minecraft_eula:
            print("note: without --accept-minecraft-eula no eula.txt would be written (a live run refuses)")
        problems = check_tools(tools, args)
        for b in range(args.batches):
            plan = public_plan(plan_batch(args, tools, b))
            print(json.dumps({"dry_run": True, "tools": tools, "batch": plan}, indent=2, default=str))
        if problems:
            print("\nA live run would fail now:")
            for p in problems:
                print(f"  - {p}")
        print(f"\n(dry run: nothing was started or written; output would go to {root})")
        return 0

    problems = check_tools(tools, args)
    if problems:
        for p in problems:
            print(f"Error: {p}", file=sys.stderr)
        return 1

    global _JOB
    _JOB = job = C.put_self_in_kill_on_close_job()
    if job is None and C.IS_WINDOWS:
        C.log("warning: could not create a Windows Job Object (pywin32 missing?); "
              "child processes are cleaned up with taskkill only")
    signal.signal(signal.SIGINT, _on_signal)
    if hasattr(signal, "SIGBREAK"):
        signal.signal(signal.SIGBREAK, _on_signal)
    if not C.IS_WINDOWS:
        # Job schedulers (Slurm, Kubernetes, ...) stop a job with SIGTERM: same clean stop as Ctrl+C
        signal.signal(signal.SIGTERM, _on_signal)

    versions = tool_versions(tools)
    git = C.git_head()
    C.log(f"git {git.get('branch')} {git.get('commit')}{' (dirty)' if git.get('dirty') else ''}; "
          f"java: {versions.get('java')}; node {versions.get('node')}; {versions.get('python')}")
    summaries = []
    rc = 0
    runner = None
    try:
        for b in range(args.batches):
            if stop_requested():
                break
            plan = plan_batch(args, tools, b)
            runner = BatchRunner(args, tools, versions, plan, git)
            C.log(f"=== batch {b + 1}/{args.batches}: {plan['batch_dir']} "
                  f"({total} instance(s), {args.episodes} episodes, types={args.types}) ===")
            status = runner.run()
            if runner.procs:  # also after an abort: align whatever was recorded
                runner.run_postprocess()
            s = runner.summary()
            summaries.append(s)
            runner.say(f"batch {b} finished: {json.dumps(s)}")
            if status not in ("completed", "timeout"):  # completed_with_kills: needs a look
                rc = 1
            if status == "aborted" and runner.abort_reason and "ports already in use" in runner.abort_reason:
                break
    except KeyboardInterrupt:
        C.log("forced stop: killing all processes")
        if runner is not None:
            runner.kill_all()
            runner.status = "killed"
            try:
                runner.write_run_config()
            except Exception:  # noqa: BLE001
                pass
        return 130

    print()
    print("Summary:")
    for s in summaries:
        print(f"  {s['batch_dir']}: {s['status']}"
              f"{' (' + s['abort_reason'] + ')' if s['abort_reason'] else ''}; episodes {s['episodes']}; "
              f"action json {s['action_json']}; world_events {s['world_events']}; "
              f"aligned mp4 {s['aligned_mp4']}; verify exit {s['verify_exit_code']}"
              f"{'; camera restarts ' + json.dumps(s['camera_restarts']) if s['camera_restarts'] else ''}")
    print(f"\nDone. Copy the whole output directory: {root}")
    return rc


if __name__ == "__main__":
    code = main()
    sys.stdout.flush()
    sys.stderr.flush()
    # Exit before the interpreter closes the Job Object handle (that would
    # terminate this process with exit code 0 and lose buffered output).
    os._exit(code)
