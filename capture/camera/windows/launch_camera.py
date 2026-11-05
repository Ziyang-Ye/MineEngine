#!/usr/bin/env python
"""Windows-native camera client launcher (windows/CONTRACT.md section 5).

Installs Minecraft 1.21 + its Mojang Java runtime + the Fabric loader into a
shared --mc-home, prepares a per-camera --game-dir (options.txt, mods/,
config/solaris-capture.json) and starts the client with quick-play
multiplayer. The solaris-capture mod records the client's main render target
in the background (the window may be hidden or covered); this script only
launches the client, streams its log to stdout and returns its exit code.

Works from cmd.exe, PowerShell and Git Bash (pathlib everywhere, no shell).
"""

import argparse
import contextlib
import glob
import hashlib
import json
import os
import shutil
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path

import minecraft_launcher_lib
import minecraft_launcher_lib._helper

IS_WINDOWS = os.name == "nt"
REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_MC_HOME = REPO_ROOT / ".cache" / "windows" / "minecraft"
MOD_DIR = REPO_ROOT / "camera" / "capture-mod"
KNOWN_FFMPEG = [Path.home() / "tools" / "ffmpeg-9.0.2-essentials_build" / "bin" / "ffmpeg.exe"]

MC_VERSION = "1.21"
# Data version of 1.21; written to a fresh options.txt so Minecraft does not run
# its option datafixers from version 0 over our keys.
MC_DATA_VERSION = 3953
FABRIC_LOADER_VERSION = "0.16.14"
INSTALL_MARKER = ".solaris_install_ok"
MOD_CONFIG_NAME = "solaris-capture.json"
# options.txt chatVisibility ids (net.minecraft.world.entity.player.ChatVisiblity)
CHAT_VISIBILITY = {"full": 0, "system": 1, "hidden": 2}
# Local skin pool: the mod renders unsigned "solaris.local" textures from <skins_dir>/<skin_id>.png
DEFAULT_SKINS_DIR = REPO_ROOT / "server" / "skins" / "pool"


def log(msg: str) -> None:
    print(f"[launcher] {msg}", flush=True)


# ---------------------------------------------------------------------------
# Installation (Minecraft + Java runtime + Fabric)
# ---------------------------------------------------------------------------

def fabric_version_id(mc_version: str, loader: str) -> str:
    return f"fabric-loader-{loader}-{mc_version}"


LOCK_HEARTBEAT_S = 10.0
LOCK_STALE_S = 120.0


@contextlib.contextmanager
def install_lock(mc_home: Path, timeout_s: float = 4 * 3600.0):
    """Exclusive lock file so two cameras started at once do not install concurrently.

    The holder touches the lock every LOCK_HEARTBEAT_S while it installs, so a
    lock is only broken when its heartbeat stopped for LOCK_STALE_S (crashed or
    killed installer), never because a slow install takes long. Waiting gives up
    after timeout_s.
    """
    mc_home.mkdir(parents=True, exist_ok=True)
    lock = mc_home / ".solaris_install.lock"
    deadline = time.time() + timeout_s
    fd = None
    waited = False
    while fd is None:
        try:
            fd = os.open(str(lock), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(fd, str(os.getpid()).encode())
        except FileExistsError:
            try:
                age = time.time() - lock.stat().st_mtime
                if age > LOCK_STALE_S:
                    log(f"breaking stale install lock {lock} (no heartbeat for {age:.0f}s)")
                    lock.unlink()
                    continue
            except OSError:
                pass
            if time.time() > deadline:
                raise TimeoutError(f"install lock {lock} held for more than {timeout_s:.0f}s")
            if not waited:
                waited = True
                log(f"another launcher is installing into {mc_home}; waiting for {lock}")
            time.sleep(1.0)
    stop = threading.Event()

    def heartbeat():
        while not stop.wait(LOCK_HEARTBEAT_S):
            with contextlib.suppress(OSError):
                os.utime(lock)

    beat = threading.Thread(target=heartbeat, name="install-lock-heartbeat", daemon=True)
    beat.start()
    try:
        yield
    finally:
        stop.set()
        beat.join(timeout=5)
        os.close(fd)
        with contextlib.suppress(OSError):
            lock.unlink()


def java_runtime_path(mc_home: Path, mc_version: str) -> Path:
    """java(w).exe of the Mojang runtime that minecraft-launcher-lib installed for mc_version."""
    vjson = mc_home / "versions" / mc_version / f"{mc_version}.json"
    component = "java-runtime-delta"
    with contextlib.suppress(OSError, ValueError, KeyError):
        component = json.loads(vjson.read_text(encoding="utf-8"))["javaVersion"]["component"]
    exe = minecraft_launcher_lib.runtime.get_executable_path(component, str(mc_home))
    if not exe:
        raise FileNotFoundError(f"Mojang Java runtime '{component}' not installed in {mc_home}")
    exe = Path(exe)
    if IS_WINDOWS and exe.name.lower() == "javaw.exe" and (exe.parent / "java.exe").is_file():
        # java.exe keeps stdout/stderr attached so the game log can be streamed
        exe = exe.parent / "java.exe"
    return exe


def is_installed(mc_home: Path, mc_version: str, loader: str) -> bool:
    marker = mc_home / INSTALL_MARKER
    try:
        done = json.loads(marker.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    fid = fabric_version_id(mc_version, loader)
    return (
        fid in done.get("versions", [])
        and (mc_home / "versions" / fid / f"{fid}.json").is_file()
        and (mc_home / "versions" / mc_version / f"{mc_version}.jar").is_file()
    )


VERSION_MANIFEST_URL = "https://piston-meta.mojang.com/mc/game/version_manifest_v2.json"
JVM_MANIFEST_URL = (
    "https://launchermeta.mojang.com/v1/products/java-runtime/"
    "2ec0cc96c44e5a76b9c8b7c39df7210883d12871/all.json"
)


def _download(session, url: str, dest: Path, sha1: str, lzma_compressed: bool = False) -> int:
    """Download url to dest (atomic rename) if dest is missing or has another sha1."""
    if dest.is_file():
        h = hashlib.sha1(dest.read_bytes()).hexdigest()
        if h == sha1:
            return 0
    dest.parent.mkdir(parents=True, exist_ok=True)
    last = None
    for _attempt in range(4):
        try:
            r = session.get(url, timeout=60)
            r.raise_for_status()
            data = r.content
            if lzma_compressed:
                import lzma
                data = lzma.decompress(data)
            if hashlib.sha1(data).hexdigest() != sha1:
                raise ValueError(f"sha1 mismatch for {url}")
            tmp = dest.with_name(dest.name + f".part{os.getpid()}")
            tmp.write_bytes(data)
            os.replace(tmp, dest)
            return len(data)
        except Exception as exc:  # network errors: retry
            last = exc
            time.sleep(1.0)
    raise RuntimeError(f"download failed: {url}: {last!r}")


def prefetch(mc_home: Path, mc_version: str, workers: int = 32) -> None:
    """Download client jar, libraries, assets and the Java runtime in parallel.

    minecraft-launcher-lib downloads one file at a time, which takes hours over a
    slow link (thousands of small asset files); its install pass afterwards only
    re-hashes what this placed (same paths, sha1-verified).
    """
    import requests
    from concurrent.futures import ThreadPoolExecutor

    session = requests.Session()
    adapter = requests.adapters.HTTPAdapter(pool_connections=workers, pool_maxsize=workers)
    session.mount("https://", adapter)
    manifest = session.get(VERSION_MANIFEST_URL, timeout=60).json()
    entry = next(v for v in manifest["versions"] if v["id"] == mc_version)
    vdir = mc_home / "versions" / mc_version
    vjson_path = vdir / f"{mc_version}.json"
    _download(session, entry["url"], vjson_path, entry["sha1"])
    vjson = json.loads(vjson_path.read_text(encoding="utf-8"))

    jobs = []  # (url, dest, sha1, lzma)
    client = vjson["downloads"]["client"]
    jobs.append((client["url"], vdir / f"{mc_version}.jar", client["sha1"], False))
    for lib in vjson.get("libraries", []):
        art = lib.get("downloads", {}).get("artifact")
        if not art or not art.get("url"):
            continue
        if "rules" in lib and not minecraft_launcher_lib._helper.parse_rule_list(lib["rules"], {}):
            continue
        jobs.append((art["url"], mc_home / "libraries" / art["path"], art["sha1"], False))
    idx = vjson["assetIndex"]
    idx_path = mc_home / "assets" / "indexes" / f"{vjson['assets']}.json"
    _download(session, idx["url"], idx_path, idx["sha1"])
    for obj in json.loads(idx_path.read_text(encoding="utf-8"))["objects"].values():
        h = obj["hash"]
        jobs.append((f"https://resources.download.minecraft.net/{h[:2]}/{h}",
                     mc_home / "assets" / "objects" / h[:2] / h, h, False))
    component = vjson.get("javaVersion", {}).get("component")
    if component:
        plat = minecraft_launcher_lib.runtime._get_jvm_platform_string()
        rt = session.get(JVM_MANIFEST_URL, timeout=60).json()[plat][component]
        if rt:
            files = session.get(rt[0]["manifest"]["url"], timeout=60).json()["files"]
            base = mc_home / "runtime" / component / plat / component
            for rel, info in files.items():
                if info["type"] != "file":
                    continue
                d = info["downloads"]
                if "lzma" in d:
                    jobs.append((d["lzma"]["url"], base / rel, d["raw"]["sha1"], True))
                else:
                    jobs.append((d["raw"]["url"], base / rel, d["raw"]["sha1"], False))
    log(f"prefetch: {len(jobs)} files with {workers} parallel downloads")
    total = {"bytes": 0, "done": 0, "last": time.time()}
    lock = threading.Lock()

    def run(job):
        n = _download(session, *job)
        with lock:
            total["bytes"] += n
            total["done"] += 1
            if time.time() - total["last"] > 10:
                total["last"] = time.time()
                log(f"prefetch: {total['done']}/{len(jobs)} files, {total['bytes'] / 1e6:.0f} MB downloaded")

    with ThreadPoolExecutor(max_workers=workers) as pool:
        for _ in pool.map(run, jobs):
            pass
    log(f"prefetch: done ({total['bytes'] / 1e6:.0f} MB downloaded)")


def ensure_installed(mc_home: Path, mc_version: str, loader: str, offline: bool = False) -> str:
    """Install Minecraft + runtime + Fabric once; returns the Fabric version id.
    offline: never install (no network on the node), fail if it is not staged."""
    fid = fabric_version_id(mc_version, loader)
    if is_installed(mc_home, mc_version, loader):
        log(f"found installed {fid} in {mc_home}")
        return fid
    if offline:
        raise SystemExit(f"--offline: {fid} is not installed in {mc_home} ({INSTALL_MARKER} or version files "
                         f"missing); stage it with --install-only on a machine with network")
    with install_lock(mc_home):
        if is_installed(mc_home, mc_version, loader):
            return fid
        status = {"text": "", "max": 0, "last": 0.0}

        def set_status(text):
            status["text"] = text

        def set_progress(value):
            now = time.time()
            if now - status["last"] > 5.0:
                status["last"] = now
                log(f"install: {status['text']} ({value}/{status['max']})")

        def set_max(value):
            status["max"] = value

        callback = {"setStatus": set_status, "setProgress": set_progress, "setMax": set_max}
        try:
            prefetch(mc_home, mc_version)
        except Exception as exc:  # the sequential install below still downloads everything
            log(f"parallel prefetch failed ({exc!r}); continuing with the sequential install")
        log(f"installing Minecraft {mc_version} (+ Mojang Java runtime) into {mc_home}")
        minecraft_launcher_lib.install.install_minecraft_version(mc_version, str(mc_home), callback=callback)
        java = java_runtime_path(mc_home, mc_version)
        log(f"installing Fabric loader {loader} (installer runs on {java})")
        minecraft_launcher_lib.fabric.install_fabric(
            mc_version, str(mc_home), loader_version=loader, callback=callback, java=str(java)
        )
        marker = mc_home / INSTALL_MARKER
        done = {"versions": []}
        with contextlib.suppress(OSError, ValueError):
            done = json.loads(marker.read_text(encoding="utf-8"))
        done.setdefault("versions", [])
        if fid not in done["versions"]:
            done["versions"].append(fid)
        done["java"] = str(java)
        done["installed_utc"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        marker.write_text(json.dumps(done, indent=2), encoding="utf-8")
        log(f"installed {fid}")
    return fid


# ---------------------------------------------------------------------------
# Game directory: options.txt, mod jar, mod config
# ---------------------------------------------------------------------------

def gui_scale_value(arg: str, height: int) -> int:
    if str(arg).lower() == "auto":
        return max(1, int(round(height / 360.0)))
    return int(arg)


def camera_options(args) -> "dict[str, str]":
    """options.txt keys of windows/CONTRACT.md section 5.2 (T2, T13)."""
    return {
        "fov": "0.0",  # (value - 70) / 40 -> 70 degrees
        "fovEffectScale": "0.0",  # no FOV change while flying / sprinting / drawing a bow
        "renderDistance": str(args.render_distance),
        "simulationDistance": str(args.simulation_distance or args.render_distance),
        "bobView": "false",
        "guiScale": str(gui_scale_value(args.gui_scale, args.height)),
        "damageTiltStrength": "0.0",
        "pauseOnLostFocus": "false",
        "maxFps": str(args.max_fps),
        "enableVsync": "false",
        # Not an option in 1.21 (added in 1.21.2); kept for newer clients, ignored by 1.21
        "inactivityFpsLimit": "minimized",
        "soundCategory_master": "0.0",
        "narrator": "0",
        "tutorialStep": "none",
        "skipMultiplayerWarning": "true",
        "joinedFirstServer": "true",
        "onboardAccessibility": "false",
        "skipQuickPlayFirstLaunchPrompt": "true",
        "fullscreen": "false",
        # clean frames: no chat lines (the server then sends no chat / non-overlay system
        # messages either); the action bar, titles and the rest of the HUD stay
        "chatVisibility": str(CHAT_VISIBILITY[args.chat_visibility]),
    }


def write_options(game_dir: Path, values: "dict[str, str]") -> Path:
    """Set our keys in options.txt, keeping every other existing (user) key."""
    path = game_dir / "options.txt"
    lines = []
    if path.exists():
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    index = {}
    for i, line in enumerate(lines):
        key = line.split(":", 1)[0]
        index.setdefault(key, i)
    if "version" not in index:
        lines.insert(0, f"version:{MC_DATA_VERSION}")
        index = {k: (i + 1) for k, i in index.items()}
    for key, value in values.items():
        entry = f"{key}:{value}"
        if key in index:
            lines[index[key]] = entry
        else:
            lines.append(entry)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def find_mod_jar(explicit) -> Path:
    if explicit:
        jar = Path(explicit)
        if not jar.is_file():
            raise FileNotFoundError(f"--mod-jar {jar} does not exist")
        return jar
    cands = [
        Path(p) for p in glob.glob(str(MOD_DIR / "build" / "libs" / "*.jar"))
        if not p.endswith(("-sources.jar", "-dev.jar"))
    ]
    if not cands:
        raise FileNotFoundError(
            f"no solaris-capture jar in {MOD_DIR / 'build' / 'libs'}; build it with gradlew build or pass --mod-jar"
        )
    return max(cands, key=lambda p: p.stat().st_mtime)


def install_mod(game_dir: Path, jar: Path) -> Path:
    mods = game_dir / "mods"
    mods.mkdir(parents=True, exist_ok=True)
    # Remove other copies of our mod (older versions) so exactly one is loaded
    for old in mods.glob("solaris-capture*.jar"):
        if old.name != jar.name:
            old.unlink()
    dest = mods / jar.name
    if not dest.exists() or dest.stat().st_size != jar.stat().st_size or dest.stat().st_mtime < jar.stat().st_mtime:
        shutil.copy2(jar, dest)
    return dest


def find_ffmpeg(explicit) -> str:
    if explicit:
        return str(Path(explicit))
    found = shutil.which("ffmpeg")
    if found:
        return found
    for cand in KNOWN_FFMPEG:
        if cand.is_file():
            return str(cand)
    return "ffmpeg.exe" if IS_WINDOWS else "ffmpeg"


def write_mod_config(game_dir: Path, args, output_dir: Path, stop_file: Path) -> Path:
    stem = Path(args.video_name).stem
    cfg = {
        "camera_name": args.name,
        "output_dir": output_dir.as_posix(),
        "video_name": args.video_name,
        "meta_name": f"{stem}_meta.json",
        "frames_name": f"{stem}_frames.jsonl",
        "ffmpeg": Path(find_ffmpeg(args.ffmpeg)).as_posix(),
        "encoder": args.encoder,
        "fps": args.fps,
        "width": args.width,
        "height": args.height,
        "hide_window": bool(args.hide_window),
        "hide_at_startup": bool(args.hide_window if args.hide_at_startup is None else args.hide_at_startup),
        "stop_file": stop_file.as_posix(),
        "server": args.server,
        "timestamps": args.timestamps,
        "process_priority": args.priority,
        "x264_preset": args.x264_preset,
        # The mod stops cleanly and quits if this launcher dies without a stop file
        "launcher_pid": os.getpid(),
        "debug_fly": bool(args.debug_fly),
        "debug_spin_deg_per_s": args.debug_spin,
        # T12 pose link: the controller of the bot (camera_name minus "Camera") sends
        # its movement packets as UDP datagrams to 127.0.0.1:pose_port (0 = off)
        "pose_port": args.pose_port,
        "pose_max_age_ms": args.pose_max_age_ms,
        # clean frames (toasts never drawn; chat hidden via options.txt)
        "suppress_toasts": bool(args.suppress_toasts),
        "chat_visibility": args.chat_visibility,
        # local skins: unsigned textures naming a pool skin are loaded from this directory
        "local_skins": bool(args.local_skins),
        "skins_dir": Path(args.skins_dir).resolve().as_posix(),
    }
    path = game_dir / "config" / MOD_CONFIG_NAME
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# Launch
# ---------------------------------------------------------------------------

def offline_login(username: str) -> "dict[str, str]":
    data = ("OfflinePlayer:" + username).encode("utf-8")
    return {"name": username, "uuid": str(uuid.UUID(hashlib.md5(data).hexdigest())), "token": "0"}


def build_command(args, mc_home: Path, game_dir: Path, version_id: str) -> "list[str]":
    login = offline_login(args.name)
    java = Path(args.java) if args.java else java_runtime_path(mc_home, args.mc_version)
    options = {
        "username": login["name"],
        "uuid": login["uuid"],
        "token": login["token"],
        "userType": "legacy",
        "launcherName": "solaris-camera-windows",
        "launcherVersion": "0.1",
        "gameDirectory": str(game_dir),
        "customResolution": True,
        "resolutionWidth": str(args.width),
        "resolutionHeight": str(args.height),
        "executablePath": str(java),
        "jvmArguments": [
            "-Xmx" + args.max_memory,
            "-Xms" + args.min_memory,
            "-XX:+UnlockExperimentalVMOptions",
            "-XX:+UseG1GC",
            "-XX:G1NewSizePercent=20",
            "-XX:G1ReservePercent=20",
            "-XX:MaxGCPauseMillis=50",
            "-XX:G1HeapRegionSize=32M",
            "-Dsun.net.client.defaultConnectTimeout=2000",
            "-Dsun.net.client.defaultReadTimeout=2000",
            "-Dfile.encoding=UTF-8",
        ],
    }
    command = minecraft_launcher_lib.command.get_minecraft_command(version_id, str(mc_home), options)
    quickplay_log = game_dir / "quickPlay" / "log.json"
    quickplay_log.parent.mkdir(parents=True, exist_ok=True)
    command.extend(["--quickPlayPath", str(quickplay_log)])
    command.extend(["--quickPlayMultiplayer", args.server])
    return command


def wait_interruptible(proc: subprocess.Popen, timeout=None) -> int:
    """Popen.wait that a Ctrl+C can interrupt on Windows (a single blocking
    WaitForSingleObject call is not interrupted by the console Ctrl+C handler)."""
    deadline = None if timeout is None else time.monotonic() + timeout
    while True:
        step = 0.25 if deadline is None else max(0.0, min(0.25, deadline - time.monotonic()))
        try:
            return proc.wait(timeout=step)
        except subprocess.TimeoutExpired:
            if deadline is not None and time.monotonic() >= deadline:
                raise


def stream_output(proc: subprocess.Popen) -> None:
    out = sys.stdout
    for raw in iter(proc.stdout.readline, b""):
        try:
            out.write(raw.decode("utf-8", errors="replace"))
            out.flush()
        except (OSError, ValueError):
            pass


def parse_args(argv=None):
    p = argparse.ArgumentParser(description="Launch a Minecraft 1.21 camera client with the solaris-capture mod (Windows)")
    p.add_argument("--name", default="CameraAlpha", help="offline player name")
    p.add_argument("--server", default="127.0.0.1:25590", help="host:port for quick-play multiplayer")
    p.add_argument("--mc-home", default=str(DEFAULT_MC_HOME), help="shared .minecraft (versions, libraries, assets, runtime)")
    p.add_argument("--game-dir", default=None, help="per-camera game dir (options.txt, mods/, logs/)")
    p.add_argument("--output-dir", default=None, help="recording directory (MKV, meta, frames sidecar)")
    p.add_argument("--video-name", default=None, help="MKV file name (default camera_<bot>.mkv)")
    # With the libx264 fallback (no NVENC) 1280x720 holds 20 fps for two cameras on the
    # reference laptop (i9-13980HX) even under load; two cameras at 1920x1080 lose
    # some frames when the CPU is busy, 2560x1440 is not usable (windows/schema_parts/camera.md).
    p.add_argument("--width", type=int, default=1280,
                   help="framebuffer / video width (default 1280; above 1920x1080 needs NVENC to keep 20 fps)")
    p.add_argument("--height", type=int, default=720)
    p.add_argument("--fps", type=int, default=20)
    p.add_argument("--max-fps", type=int, default=60, help="options.txt maxFps (render rate; frames are sampled at --fps)")
    p.add_argument("--ffmpeg", default=None, help="path to ffmpeg.exe (default: PATH, then ~/tools)")
    p.add_argument("--encoder", default="auto", choices=["auto", "h264_nvenc", "libx264"])
    p.add_argument("--x264-preset", default="veryfast",
                   choices=["ultrafast", "superfast", "veryfast", "faster", "fast", "medium"],
                   help="libx264 preset when NVENC is not used (ultrafast: ~25%% less CPU, ~4x larger files)")
    p.add_argument("--priority", default="above_normal", choices=["normal", "above_normal", "high"],
                   help="Windows priority class of the client and its ffmpeg (keeps 20 fps under CPU load)")
    p.add_argument("--timestamps", default="capture", choices=["capture", "wallclock"],
                   help="MKV PTS source: exact capture time (default) or ffmpeg wall clock at pipe arrival (snapped to 1/fps)")
    p.add_argument("--render-distance", type=int, default=12)
    p.add_argument("--simulation-distance", type=int, default=None, help="default: --render-distance")
    p.add_argument("--gui-scale", default="auto", help="'auto' = round(height/360) (2 at 720p) or an integer")
    p.add_argument("--max-memory", default="3G")
    p.add_argument("--min-memory", default="1G")
    p.add_argument("--hide-window", type=int, default=1, choices=[0, 1],
                   help="1 = record without a visible window (default); 0 = keep the window visible")
    p.add_argument("--hide-at-startup", type=int, default=None, choices=[0, 1],
                   help="1 = create the window hidden (never shown, never takes focus); 0 = show it until the "
                        "player is in a world, then hide it. Default: same as --hide-window")
    p.add_argument("--stop-file", default=None, help="the mod stops recording and quits when this file appears")
    p.add_argument("--mod-jar", default=None, help="solaris-capture jar (default: newest camera/capture-mod/build/libs/*.jar)")
    p.add_argument("--mc-version", default=MC_VERSION)
    p.add_argument("--fabric-loader", default=FABRIC_LOADER_VERSION)
    p.add_argument("--java", default=None, help="override the Java executable (default: Mojang runtime for 1.21)")
    p.add_argument("--install-only", action="store_true", help="install Minecraft + runtime + Fabric into --mc-home and exit")
    p.add_argument("--offline", action="store_true",
                   help="no network: never install, fail when --mc-home is not staged (with --install-only: check only)")
    p.add_argument("--keep-process-group", action="store_true",
                   help="Linux: start Minecraft in this launcher's process group, so killing the group (run_windows.py "
                        "kill path) also ends Minecraft and its ffmpeg (default: own session)")
    p.add_argument("--extra-option", action="append", default=[], metavar="KEY:VALUE",
                   help="extra / overriding options.txt entry (repeatable; recorded live in the meta)")
    p.add_argument("--pose-port", type=int, default=0,
                   help="UDP port on 127.0.0.1 where the bot's controller sends its movement packets "
                        "(CAMERA_POSE_PORT); the camera then renders that pose directly (0 = off: follow the server)")
    p.add_argument("--pose-max-age-ms", type=float, default=500.0,
                   help="pose-link datagrams older than this fall back to the server-followed camera")
    p.add_argument("--suppress-toasts", type=int, default=1, choices=[0, 1],
                   help="1 = the mod drops every toast (advancement, recipe, tutorial, system) before it is drawn")
    p.add_argument("--chat-visibility", default="hidden", choices=sorted(CHAT_VISIBILITY),
                   help="options.txt chatVisibility (hidden = no chat lines in the frames; the action bar stays)")
    p.add_argument("--local-skins", type=int, default=1, choices=[0, 1],
                   help="1 = render unsigned local-pool textures (MirrorBot local skin mode) from --skins-dir")
    p.add_argument("--skins-dir", default=str(DEFAULT_SKINS_DIR),
                   help="skin pool directory (<skin_id>.png); legacy skins are also looked up in its parent")
    p.add_argument("--debug-fly", type=int, default=0, choices=[0, 1], help=argparse.SUPPRESS)
    p.add_argument("--debug-spin", type=float, default=0.0, help=argparse.SUPPRESS)
    return p.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    mc_home = Path(args.mc_home).resolve()
    version_id = ensure_installed(mc_home, args.mc_version, args.fabric_loader, offline=args.offline)
    if args.install_only:
        log(f"install-only: {version_id} ready in {mc_home} (java: {java_runtime_path(mc_home, args.mc_version)})")
        return 0

    bot = args.name.lower().replace("camera", "") or args.name.lower()
    if not args.video_name:
        args.video_name = f"camera_{bot}.mkv"
    game_dir = Path(args.game_dir or (mc_home.parent / "camera_game" / args.name)).resolve()
    output_dir = Path(args.output_dir or (game_dir / "recordings")).resolve()
    stop_file = Path(args.stop_file or (game_dir / "camera.stop")).resolve()
    game_dir.mkdir(parents=True, exist_ok=True)
    output_dir.mkdir(parents=True, exist_ok=True)
    if stop_file.exists():
        log(f"removing stale stop file {stop_file}")
        stop_file.unlink()

    opts = camera_options(args)
    for item in args.extra_option:
        key, sep, value = item.partition(":")
        if not sep or not key:
            raise SystemExit(f"--extra-option expects KEY:VALUE, got {item!r}")
        opts[key] = value
    log(f"options.txt: {write_options(game_dir, opts)}")
    jar = install_mod(game_dir, find_mod_jar(args.mod_jar))
    log(f"mod: {jar}")
    log(f"mod config: {write_mod_config(game_dir, args, output_dir, stop_file)}")

    command = build_command(args, mc_home, game_dir, version_id)
    log("starting Minecraft: " + subprocess.list2cmdline(command))
    # Own process group: a console Ctrl+C reaches only this launcher, which then
    # writes the stop file, so the mod finishes the recording on the render thread
    # and Minecraft exits normally (a Ctrl+C delivered to the JVM itself ends it via
    # the shutdown hook with a 0xC0000409 crash code). If this launcher is killed
    # instead, the mod notices (launcher_pid in its config) and stops the same way.
    # Under run_windows.py on Linux (--keep-process-group) the launcher already has its
    # own session, and its process-group kill must reach Minecraft too (no Job Object).
    flags = subprocess.CREATE_NEW_PROCESS_GROUP if IS_WINDOWS else 0
    proc = subprocess.Popen(
        command, cwd=str(game_dir), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        stdin=subprocess.DEVNULL, creationflags=flags,
        start_new_session=not IS_WINDOWS and not args.keep_process_group,
    )
    pump = threading.Thread(target=stream_output, args=(proc,), daemon=True)
    pump.start()
    interrupted = False
    try:
        code = wait_interruptible(proc)
    except KeyboardInterrupt:
        interrupted = True
        log("interrupted: asking Minecraft to stop via the stop file (Ctrl+C again kills it)")
        with contextlib.suppress(OSError):
            stop_file.touch()
        try:
            code = wait_interruptible(proc, timeout=90)
        except (subprocess.TimeoutExpired, KeyboardInterrupt):
            log("Minecraft did not exit in time; killing it")
            proc.kill()
            code = proc.wait()
    pump.join(timeout=5)
    log(f"Minecraft exited with code {code}")
    if interrupted:
        # Conventional exit status of a program stopped with Ctrl+C (128 + SIGINT)
        return 130
    return code


if __name__ == "__main__":
    sys.exit(main())
