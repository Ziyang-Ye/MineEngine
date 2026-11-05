#!/usr/bin/env python3
"""One-time (idempotent) setup of the Windows-native runtime.

Steps (each is skipped when already done; see --help for the switches):
  1. check java (21+), node (18+), python (3.10+), ffmpeg/ffprobe
  2. create .venv if missing and pip install the Python packages
  3. npm ci --ignore-scripts at the repo root; npm install in server/plugin-starter
  4. Paper 1.21 build 130 into .cache/windows/ (sha256-checked) + pre-patch it
  5. ffmpeg (gyan.dev essentials build) into .cache/windows/tools/ if not found
  6. build the MirrorBot plugin with Maven (downloads Maven if not found) and
     check the jar contains WorldStateLogger.class
  7. build the solaris-capture mod (camera/capture-mod/gradlew.bat build)
  8. python camera/windows/launch_camera.py --install-only (Minecraft 1.21 +
     Fabric for the camera clients)
  9. write .cache/windows/setup.json (tool paths read by run_windows.py)

--platform linux (default on Linux; staging machine with network, see
linux/README.md): no pywin32, never downloads the Windows ffmpeg/Maven
zips (pass --ffmpeg-dir; an existing MirrorBot jar with WorldStateLogger is
kept when Maven is missing), --no-venv uses this interpreter (e.g. a conda env).
"""

import argparse
import hashlib
import os
import re
import shutil
import subprocess
import sys
import urllib.request
import zipfile
from pathlib import Path
from typing import List, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))
import common as C  # noqa: E402

REPO = C.REPO_ROOT

PIP_PACKAGES = [
    "numpy", "opencv-python-headless", "minecraft-launcher-lib==6.5",
    "pywin32", "pyyaml", "tqdm", "pillow",
]
# import name -> required version (None = any)
PIP_IMPORTS = {"numpy": None, "cv2": None, "minecraft_launcher_lib": "6.5", "win32job": None,
               "yaml": None, "tqdm": None, "PIL": None}
# --platform linux: no Job Objects (run_windows.py uses process groups there)
LINUX_DROP = {"pywin32", "win32job"}

FFMPEG_URL = "https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip"
MAVEN_VERSION = "3.9.9"
MAVEN_URL = (f"https://archive.apache.org/dist/maven/maven-3/{MAVEN_VERSION}/binaries/"
             f"apache-maven-{MAVEN_VERSION}-bin.zip")
MAVEN_SHA512 = ("8beac8d11ef208f1e2a8df0682b9448a9a363d2ad13ca74af43705549e72e74c"   # apache-maven-3.9.9-bin.zip, as published
                "9378823bf689287801cbbfc2f6ea9596201d19ccacfdfb682ee8a2ff4c4418ba")   # by Apache and on Maven Central

PLUGIN_DIR = REPO / "server" / "episode-manager-plugin"
PLUGIN_JAR = REPO / "server" / "plugins" / "MirrorBot-1.0.jar"
MOD_DIR = REPO / "camera" / "capture-mod"
LAUNCH_CAMERA = REPO / "camera" / "windows" / "launch_camera.py"


class SetupError(Exception):
    pass


def say(msg: str) -> None:
    C.log(msg)


def run(cmd: List[str], cwd: Optional[Path] = None, env: Optional[dict] = None, check: bool = True) -> int:
    say(f"$ {subprocess.list2cmdline([str(c) for c in cmd])}" + (f"   (in {cwd})" if cwd else ""))
    r = subprocess.run([str(c) for c in cmd], cwd=str(cwd) if cwd else None, env=env)
    if check and r.returncode != 0:
        raise SetupError(f"command failed with exit code {r.returncode}: {cmd[0]}")
    return r.returncode


def newest_mtime(paths) -> float:
    newest = 0.0
    for p in paths:
        p = Path(p)
        if p.is_file():
            newest = max(newest, p.stat().st_mtime)
        elif p.is_dir():
            for f in p.rglob("*"):
                if f.is_file():
                    newest = max(newest, f.stat().st_mtime)
    return newest


def parse_version(text: Optional[str]) -> tuple:
    m = re.search(r"(\d+)(?:\.(\d+))?(?:\.(\d+))?", text or "")
    return tuple(int(x or 0) for x in m.groups()) if m else (0, 0, 0)


def download(url: str, dest: Path) -> Path:
    """Download into `dest` (a file inside its own directory) via a .part file."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_name(dest.name + ".part")
    say(f"downloading {url}")
    req = urllib.request.Request(url, headers={"User-Agent": "mineengine-setup/0.1"})
    with urllib.request.urlopen(req, timeout=60) as resp, open(part, "wb") as fh:
        total = int(resp.headers.get("Content-Length") or 0)
        done, last = 0, -1
        while True:
            chunk = resp.read(1 << 20)
            if not chunk:
                break
            fh.write(chunk)
            done += len(chunk)
            pct = int(done * 100 / total) if total else -1
            if pct // 10 != last // 10:
                last = pct
                print(f"  {done >> 20} MiB" + (f" ({pct}%)" if total else ""), flush=True)
    os.replace(part, dest)
    return dest


def safe_extract(zip_path: Path, dest: Path) -> None:
    """Extract a zip refusing absolute paths / '..' members."""
    dest.mkdir(parents=True, exist_ok=True)
    root = dest.resolve()
    with zipfile.ZipFile(zip_path) as zf:
        for member in zf.infolist():
            target = (dest / member.filename).resolve()
            if root not in target.parents and target != root:
                raise SetupError(f"unsafe path in {zip_path.name}: {member.filename}")
        zf.extractall(dest)


def java_home_of(java: str) -> Optional[str]:
    try:
        r = subprocess.run([java, "-XshowSettings:properties", "-version"], capture_output=True,
                           text=True, timeout=30, errors="replace")
    except (OSError, subprocess.SubprocessError):
        return None
    m = re.search(r"java\.home\s*=\s*(.+)", r.stderr + r.stdout)
    return m.group(1).strip() if m else None


# ---------------------------------------------------------------------------
# Steps
# ---------------------------------------------------------------------------

def step_check_tools(args, st: dict) -> None:
    java = C.find_java(args.java, st)
    if not java:
        raise SetupError("java not found: install a JDK 21+ (e.g. Temurin 21) or pass --java")
    jv = C.tool_version([java, "-version"])
    if parse_version(jv.split('"')[1] if jv and '"' in jv else jv)[0] < 21:
        raise SetupError(f"Paper 1.21 needs Java 21+, found: {jv}")
    st.update(java=java, java_version=jv, java_home=java_home_of(java))
    say(f"java: {java} ({jv})")

    node = C.find_node(args.node, st)
    if not node:
        raise SetupError("node not found: install Node.js 20+ or pass --node")
    nv = C.tool_version([node, "--version"])
    if parse_version(nv)[0] < 18:
        raise SetupError(f"Node 18+ required, found {nv}")
    npm = C.find_npm(node)
    if not npm:
        raise SetupError("npm not found next to node")
    st.update(node=node, node_version=nv, npm=npm)
    say(f"node: {node} ({nv}); npm: {npm}")

    if sys.version_info < (3, 10):
        raise SetupError(f"Python 3.10+ required, running {sys.version.split()[0]}")


def pip_lists(args):
    """(pip packages, {import name: version}) for the target platform."""
    if args.platform != "linux":
        return PIP_PACKAGES, PIP_IMPORTS
    return ([p for p in PIP_PACKAGES if p not in LINUX_DROP],
            {k: v for k, v in PIP_IMPORTS.items() if k not in LINUX_DROP})


def step_venv(args, st: dict) -> None:
    if args.no_venv:
        py = Path(args.python or sys.executable).absolute()
        if not py.is_file():
            raise SetupError(f"--no-venv: python {py} not found")
        say(f"python: {py} (--no-venv)")
    else:
        py = C.VENV_PYTHON
    if not py.is_file():
        base = args.python or sys.executable
        say(f"creating {py.parent.parent} with {base}")
        run([base, "-m", "venv", str(py.parent.parent)])
    st["python"] = str(py)
    st["python_version"] = C.tool_version([str(py), "--version"])
    if args.skip_pip:
        say("pip: skipped (--skip-pip)")
        return "skipped"
    pip_packages, pip_imports = pip_lists(args)
    check = ("import importlib, importlib.metadata as m, sys\n"
             f"need = {pip_imports!r}\n"
             "missing = []\n"
             "for mod, ver in need.items():\n"
             "    try:\n"
             "        importlib.import_module(mod)\n"
             "        if ver and m.version(mod.replace('_', '-')) != ver: missing.append(mod)\n"
             "    except Exception: missing.append(mod)\n"
             "print(','.join(missing)); sys.exit(1 if missing else 0)\n")
    r = subprocess.run([str(py), "-c", check], capture_output=True, text=True)
    if r.returncode == 0 and not args.force_pip:
        say("python packages: all present")
        return
    say(f"python packages missing/outdated: {r.stdout.strip() or '?'}; installing")
    run([str(py), "-m", "pip", "install", "--disable-pip-version-check", *pip_packages])


def step_npm(args, st: dict) -> None:
    if args.skip_npm:
        say("npm: skipped (--skip-npm)")
        return "skipped"
    npm = st["npm"]
    lock_marker = REPO / "node_modules" / ".package-lock.json"
    fresh = (lock_marker.is_file() and (REPO / "node_modules" / "mineflayer").is_dir()
             and lock_marker.stat().st_mtime >= (REPO / "package-lock.json").stat().st_mtime)
    if fresh and not args.force_npm:
        say("node_modules: up to date (npm ci skipped; --force-npm to redo)")
    else:
        # --ignore-scripts: the native gl/canvas builds are not needed (viewer rendering is off)
        run([npm, "ci", "--ignore-scripts", "--no-audit", "--no-fund"], cwd=REPO)
    ps = REPO / "server" / "plugin-starter"
    if (ps / "node_modules" / "rcon-client").is_dir() and not args.force_npm:
        say("server/plugin-starter/node_modules: present")
    else:
        run([npm, "install", "--omit=dev", "--no-progress", "--no-package-lock", "--no-audit", "--no-fund"],
            cwd=ps)


def step_paper(args, st: dict) -> None:
    if args.paper_jar:
        jar = Path(args.paper_jar).absolute()
        if not jar.is_file():
            raise SetupError(f"--paper-jar {jar} does not exist")
        digest = C.sha256_file(jar)
        if digest != C.PAPER_SHA256:
            say(f"warning: {jar} is not Paper 1.21 build 130 (sha256 {digest})")
    else:
        jar = C.CACHE_DIR / C.PAPER_JAR_NAME
        if jar.is_file() and C.sha256_file(jar) == C.PAPER_SHA256:
            say(f"Paper: {jar} (sha256 ok)")
        else:
            local = next((p for p in C.KNOWN_PAPER_JARS if p.is_file() and C.sha256_file(p) == C.PAPER_SHA256),
                         None)
            if local:
                say(f"Paper: copying {local}")
                jar.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(local, jar)
            else:
                download(C.PAPER_URL, jar)
            digest = C.sha256_file(jar)
            if digest != C.PAPER_SHA256:
                jar.unlink()
                raise SetupError(f"Paper download sha256 mismatch ({digest})")
        digest = C.PAPER_SHA256
    st.update(paper_jar=str(jar), paper_sha256=digest, paper_bundler_repo=str(C.PAPER_BUNDLER_REPO))
    # Pre-patch once (downloads the Mojang server jar + libraries into the
    # shared bundler repo, so the first batch does not wait for it)
    patched = list((C.PAPER_BUNDLER_REPO / "versions").glob("*/paper-*.jar"))
    if patched:
        say(f"Paper bundler repo ready: {C.PAPER_BUNDLER_REPO}")
    elif args.skip_paper_patch:
        say("Paper pre-patch skipped (--skip-paper-patch)")
    else:
        C.PAPER_BUNDLER_REPO.mkdir(parents=True, exist_ok=True)
        run([st["java"], f"-DbundlerRepoDir={C.PAPER_BUNDLER_REPO}", "-Dpaperclip.patchonly=true",
             "-jar", str(jar)], cwd=C.PAPER_BUNDLER_REPO)


def step_ffmpeg(args, st: dict) -> None:
    d = C.find_ffmpeg_dir(args.ffmpeg_dir, st)
    if not d:
        if args.no_download:
            why = "--platform linux (only the Windows build is downloaded)" if args.platform == "linux" \
                else "--no-download given"
            raise SetupError(f"ffmpeg not found and {why} (pass --ffmpeg-dir)")
        dl_dir = C.TOOLS_DIR / "ffmpeg_download"
        zip_path = download(FFMPEG_URL, dl_dir / "ffmpeg-release-essentials.zip")
        safe_extract(zip_path, C.TOOLS_DIR / "ffmpeg")
        d = C.find_ffmpeg_dir(None, {})
        if not d:
            hits = list((C.TOOLS_DIR / "ffmpeg").glob("*/bin/ffmpeg.exe"))
            d = str(hits[0].parent) if hits else None
        if not d:
            raise SetupError("ffmpeg.exe not found in the downloaded archive")
    ffmpeg = str(Path(d) / f"ffmpeg{C.EXE}")
    st.update(ffmpeg_dir=d, ffmpeg_version=C.tool_version([ffmpeg, "-hide_banner", "-version"]))
    try:
        enc = subprocess.run([ffmpeg, "-hide_banner", "-encoders"], capture_output=True, text=True,
                             timeout=30, errors="replace").stdout
    except (OSError, subprocess.SubprocessError):
        enc = ""
    st["ffmpeg_has_h264_nvenc"] = "h264_nvenc" in enc
    st["ffmpeg_has_libx264"] = "libx264" in enc
    say(f"ffmpeg: {d} ({st['ffmpeg_version']}); h264_nvenc={st['ffmpeg_has_h264_nvenc']} "
        f"libx264={st['ffmpeg_has_libx264']}")
    if parse_version(st["ffmpeg_version"]) < (5, 1, 0):
        say("warning: the capture mod passes -fps_mode / -enc_time_base, which need ffmpeg 5.1+")


def java_env(st: dict) -> dict:
    env = dict(os.environ)
    if st.get("java_home") and not env.get("JAVA_HOME"):
        env["JAVA_HOME"] = st["java_home"]
    if st.get("java_home"):
        env["PATH"] = os.pathsep.join([str(Path(st["java_home"]) / "bin"), env.get("PATH", "")])
    return env


def step_plugin(args, st: dict) -> None:
    st["mirrorbot_jar"] = str(PLUGIN_JAR)
    ok = C.jar_contains(PLUGIN_JAR, "WorldStateLogger.class")
    stale = (not PLUGIN_JAR.is_file()
             or newest_mtime([PLUGIN_DIR / "src", PLUGIN_DIR / "pom.xml"]) > PLUGIN_JAR.stat().st_mtime)
    if args.skip_plugin_build:
        say(f"MirrorBot build skipped (--skip-plugin-build); jar has WorldStateLogger: {ok}")
        st["mirrorbot_ok"] = ok
        return "skipped"
    if ok and not stale and not args.rebuild_plugin:
        say("MirrorBot jar is up to date (contains WorldStateLogger)")
        st["mirrorbot_ok"] = True
        return
    mvn = C.find_maven(args.maven, st)
    if not mvn and args.platform == "linux" and ok and not args.rebuild_plugin:
        # mtimes after a copy say nothing; an existing jar with WorldStateLogger is the built one
        say("Maven not found; keeping the existing MirrorBot jar (contains WorldStateLogger)")
        st["mirrorbot_ok"] = True
        return "skipped"
    if not mvn:
        if args.no_download:
            why = "--platform linux" if args.platform == "linux" else "--no-download given"
            raise SetupError(f"Maven not found and {why} (pass --maven)")
        dl = C.TOOLS_DIR / "maven_download" / f"apache-maven-{MAVEN_VERSION}-bin.zip"
        download(MAVEN_URL, dl)
        if hashlib.sha512(dl.read_bytes()).hexdigest() != MAVEN_SHA512:
            dl.unlink()
            raise SetupError("Maven download sha512 mismatch")
        safe_extract(dl, C.TOOLS_DIR)
        mvn = C.find_maven(None, {})
        if not mvn:
            raise SetupError("mvn not found after extracting Maven")
    m2 = Path(args.m2_repo or st.get("m2_repo") or (C.KNOWN_M2_REPO if C.KNOWN_M2_REPO.is_dir()
                                                     else C.CACHE_DIR / "m2"))
    st.update(maven=mvn, m2_repo=str(m2))
    run([mvn, "-B", "-q", f"-Dmaven.repo.local={m2}", "package"], cwd=PLUGIN_DIR, env=java_env(st))
    ok = C.jar_contains(PLUGIN_JAR, "WorldStateLogger.class")
    st["mirrorbot_ok"] = ok
    if not ok:
        raise SetupError(f"{PLUGIN_JAR} does not contain WorldStateLogger.class after the build")
    say(f"{PLUGIN_JAR}: contains WorldStateLogger")


def step_mod(args, st: dict) -> None:
    gradlew = MOD_DIR / ("gradlew.bat" if C.IS_WINDOWS else "gradlew")
    jar = C.newest_mod_jar(MOD_DIR)
    if args.skip_mod_build:
        say(f"capture mod build skipped (--skip-mod-build); jar: {jar}")
    elif not gradlew.is_file():
        say(f"warning: {gradlew} not found; the capture mod cannot be built yet")
    else:
        sources = [MOD_DIR / "src", *MOD_DIR.glob("*.gradle*"), MOD_DIR / "gradle.properties"]
        stale = jar is None or newest_mtime(sources) > Path(jar).stat().st_mtime
        if stale or args.rebuild_mod:
            cmd = [str(gradlew)] if C.IS_WINDOWS else ["sh", str(gradlew)]   # works without the exec bit (zip downloads)
            run(cmd + ["build", "--no-daemon"], cwd=MOD_DIR, env=java_env(st))
            jar = C.newest_mod_jar(MOD_DIR)
            if not jar:
                raise SetupError(f"no jar in {MOD_DIR / 'build' / 'libs'} after the Gradle build")
        else:
            say(f"capture mod jar is up to date: {jar}")
    st["capture_mod_jar"] = jar
    if not jar:
        return "unavailable"
    return "skipped" if args.skip_mod_build else None


def step_camera_install(args, st: dict) -> None:
    mc_home = Path(args.mc_home or st.get("mc_home") or C.DEFAULT_MC_HOME).absolute()
    st["mc_home"] = str(mc_home)
    if args.skip_camera_install:
        say("camera install skipped (--skip-camera-install)")
        return "skipped"
    if not LAUNCH_CAMERA.is_file():
        say(f"warning: {LAUNCH_CAMERA} not found; camera clients not installed")
        st["camera_installed"] = False
        return "unavailable"
    mc_home.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ, PYTHONUNBUFFERED="1", PYTHONIOENCODING="utf-8")
    run([st.get("python") or sys.executable, str(LAUNCH_CAMERA), "--install-only", "--mc-home", str(mc_home)],
        cwd=REPO, env=env)
    st["camera_installed"] = True


STEPS = [
    ("check_tools", step_check_tools),
    ("venv", step_venv),
    ("npm", step_npm),
    ("paper", step_paper),
    ("ffmpeg", step_ffmpeg),
    ("plugin", step_plugin),
    ("capture_mod", step_mod),
    ("camera_install", step_camera_install),
]


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--java", help="java.exe (21+); default: PATH / JAVA_HOME")
    ap.add_argument("--node", help="node.exe; default: PATH")
    ap.add_argument("--python", help="base Python used to create .venv (default: this interpreter)")
    ap.add_argument("--ffmpeg-dir", help="directory with ffmpeg.exe + ffprobe.exe")
    ap.add_argument("--maven", help="mvn(.cmd); default: PATH, known location, else downloaded")
    ap.add_argument("--m2-repo", help="Maven local repository (default: ~/tools/m2repo if present, "
                    "else .cache/windows/m2)")
    ap.add_argument("--paper-jar", help="use this Paper jar instead of downloading build 130")
    ap.add_argument("--mc-home", help=f"shared camera .minecraft (default: {C.DEFAULT_MC_HOME})")
    ap.add_argument("--no-download", action="store_true", help="never download ffmpeg/Maven")
    ap.add_argument("--skip-pip", action="store_true")
    ap.add_argument("--force-pip", action="store_true", help="pip install even if all imports work")
    ap.add_argument("--skip-npm", action="store_true")
    ap.add_argument("--force-npm", action="store_true", help="re-run npm ci / npm install")
    ap.add_argument("--skip-paper-patch", action="store_true", help="do not pre-patch Paper")
    ap.add_argument("--skip-plugin-build", action="store_true")
    ap.add_argument("--rebuild-plugin", action="store_true")
    ap.add_argument("--skip-mod-build", action="store_true")
    ap.add_argument("--rebuild-mod", action="store_true")
    ap.add_argument("--skip-camera-install", action="store_true")
    ap.add_argument("--platform", default="auto", choices=["auto", "windows", "linux"],
                    help="linux: no pywin32, no Windows ffmpeg/Maven downloads (default: this OS)")
    ap.add_argument("--no-venv", action="store_true",
                    help="use --python / this interpreter (e.g. a conda env) instead of creating .venv")
    args = ap.parse_args(argv)
    if args.platform == "auto":
        args.platform = "windows" if C.IS_WINDOWS else "linux"
    if args.platform == "linux":
        args.no_download = True
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="backslashreplace")
        except (AttributeError, ValueError):
            pass

    st = C.load_setup()
    st["repo"] = str(REPO)
    steps = st.setdefault("steps", {})
    failed = None
    for name, fn in STEPS:
        say(f"--- {name} ---")
        try:
            res = fn(args, st)
            steps[name] = {"status": res or "ok", "utc": C.utc_now_iso()}
        except (SetupError, OSError, subprocess.SubprocessError) as exc:
            steps[name] = {"status": "failed", "error": str(exc), "utc": C.utc_now_iso()}
            failed = (name, exc)
            break
    st["written_utc"] = C.utc_now_iso()
    C.save_setup(st)
    say(f"wrote {C.SETUP_JSON}")
    if failed:
        print(f"\nSetup FAILED at step '{failed[0]}': {failed[1]}", file=sys.stderr)
        return 1
    missing = [k for k in ("capture_mod_jar",) if not st.get(k)]
    if not st.get("camera_installed"):
        missing.append("camera client install")
    print("\nSetup complete." + (f" Not available yet: {', '.join(missing)} "
                                 "(run_windows.cmd --no-cameras still works)." if missing else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
