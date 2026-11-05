"""Shared helpers for the Windows-native runtime (windows/run_windows.py,
windows/setup_windows.py; also used on Linux, see linux/README.md): repo
paths, tool discovery, a minimal RCON client, Windows Job Objects and
log-capturing child processes.

Standard library only, except the optional pywin32 import (win32job) used for
Job Objects; without pywin32 the code falls back to `taskkill /T /F`.
"""

import datetime
import hashlib
import json
import os
import shutil
import socket
import struct
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Dict, List, Optional, Sequence

IS_WINDOWS = os.name == "nt"

REPO_ROOT = Path(__file__).resolve().parent.parent
CACHE_DIR = REPO_ROOT / ".cache" / "windows"
TOOLS_DIR = CACHE_DIR / "tools"
SETUP_JSON = CACHE_DIR / "setup.json"
VENV_PYTHON = REPO_ROOT / ".venv" / ("Scripts/python.exe" if IS_WINDOWS else "bin/python")

# Paper 1.21 build 130 (the version the Docker image runs)
PAPER_JAR_NAME = "paper-1.21-130.jar"
PAPER_URL = (
    "https://fill-data.papermc.io/v1/objects/"
    "ab9bb1afc3cea6978a0c03ce8448aa654fe8a9c4dddf341e7cbda1b0edaa73f5/paper-1.21-130.jar"
)
PAPER_SHA256 = "ab9bb1afc3cea6978a0c03ce8448aa654fe8a9c4dddf341e7cbda1b0edaa73f5"
# Paperclip extracts the patched server + libraries here (shared by all batches,
# so the Mojang jar is downloaded only once)
PAPER_BUNDLER_REPO = CACHE_DIR / "paper_repo"
DEFAULT_MC_HOME = CACHE_DIR / "minecraft"

# Locations named in windows/CONTRACT.md (header); searched after PATH
HOME_TOOLS = Path.home() / "tools"
KNOWN_FFMPEG_DIRS = [HOME_TOOLS / "ffmpeg-9.0.2-essentials_build" / "bin"]
KNOWN_MAVEN = [HOME_TOOLS / "apache-maven-3.9.9" / "bin" / "mvn.cmd"]
KNOWN_M2_REPO = HOME_TOOLS / "m2repo"
KNOWN_PAPER_JARS = [HOME_TOOLS / "paper" / PAPER_JAR_NAME]
KNOWN_NODE = [Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "nodejs" / "node.exe"]

EXE = ".exe" if IS_WINDOWS else ""


# ---------------------------------------------------------------------------
# Small utilities
# ---------------------------------------------------------------------------

def utc_now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def log(msg: str) -> None:
    """Console message with a local time prefix (flushed immediately)."""
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def load_setup() -> dict:
    """Tool paths resolved by windows/setup_windows.py ({} if setup never ran)."""
    try:
        with open(SETUP_JSON, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {}


def save_setup(data: dict) -> None:
    SETUP_JSON.parent.mkdir(parents=True, exist_ok=True)
    tmp = SETUP_JSON.with_suffix(".json.tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2)
    os.replace(tmp, SETUP_JSON)


def git_head(repo: Path = REPO_ROOT) -> Dict[str, Optional[str]]:
    """{'commit', 'branch', 'dirty'} of the repo (None values if git is unavailable)."""
    def run(args):
        try:
            r = subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True, timeout=20)
            return r.stdout.strip() if r.returncode == 0 else None
        except (OSError, subprocess.SubprocessError):
            return None

    status = run(["status", "--porcelain", "--untracked-files=no"])
    return {
        "commit": run(["rev-parse", "HEAD"]),
        "branch": run(["rev-parse", "--abbrev-ref", "HEAD"]),
        "dirty": None if status is None else bool(status),
    }


# ---------------------------------------------------------------------------
# Tool discovery
# ---------------------------------------------------------------------------

def _existing(path) -> Optional[str]:
    if path and Path(path).is_file():
        return str(Path(path).absolute())
    return None


def find_java(explicit: Optional[str] = None, setup: Optional[dict] = None) -> Optional[str]:
    setup = setup or {}
    for cand in (explicit, setup.get("java"), shutil.which("java")):
        p = _existing(cand)
        if p:
            return p
    java_home = os.environ.get("JAVA_HOME")
    if java_home:
        return _existing(Path(java_home) / "bin" / f"java{EXE}")
    return None


def find_node(explicit: Optional[str] = None, setup: Optional[dict] = None) -> Optional[str]:
    setup = setup or {}
    for cand in (explicit, setup.get("node"), shutil.which("node"), *KNOWN_NODE):
        p = _existing(cand)
        if p:
            return p
    return None


def find_npm(node: Optional[str]) -> Optional[str]:
    """npm next to node (npm.cmd on Windows), else npm on PATH."""
    if node:
        for name in (["npm.cmd", "npm.exe"] if IS_WINDOWS else ["npm"]):
            p = _existing(Path(node).parent / name)
            if p:
                return p
    return shutil.which("npm")


def find_python(explicit: Optional[str] = None, setup: Optional[dict] = None) -> str:
    """Python used for children (act recorders, camera launcher, postprocess):
    --python, setup.json, the repo venv, else the running interpreter."""
    setup = setup or {}
    for cand in (explicit, setup.get("python"), VENV_PYTHON):
        p = _existing(cand)
        if p:
            return p
    return sys.executable


def find_ffmpeg_dir(explicit: Optional[str] = None, setup: Optional[dict] = None) -> Optional[str]:
    """Directory containing ffmpeg(.exe) and ffprobe(.exe)."""
    setup = setup or {}
    cands = [explicit, setup.get("ffmpeg_dir")]
    on_path = shutil.which("ffmpeg")
    if on_path:
        cands.append(str(Path(on_path).parent))
    cands += [str(d) for d in KNOWN_FFMPEG_DIRS]
    if TOOLS_DIR.is_dir():
        cands += [str(p.parent) for p in sorted(TOOLS_DIR.glob(f"ffmpeg*/**/bin/ffmpeg{EXE}"))]
    for d in cands:
        if d and (Path(d) / f"ffmpeg{EXE}").is_file() and (Path(d) / f"ffprobe{EXE}").is_file():
            return str(Path(d).absolute())
    return None


def find_maven(explicit: Optional[str] = None, setup: Optional[dict] = None) -> Optional[str]:
    setup = setup or {}
    cands = [explicit, setup.get("maven"), shutil.which("mvn.cmd") if IS_WINDOWS else None,
             shutil.which("mvn"), *KNOWN_MAVEN]
    if TOOLS_DIR.is_dir():
        cands += sorted(TOOLS_DIR.glob("apache-maven-*/bin/mvn.cmd" if IS_WINDOWS else "apache-maven-*/bin/mvn"))
    for cand in cands:
        p = _existing(cand)
        if p:
            return p
    return None


def find_paper_jar(explicit: Optional[str] = None, setup: Optional[dict] = None) -> Optional[str]:
    setup = setup or {}
    for cand in (explicit, setup.get("paper_jar"), CACHE_DIR / PAPER_JAR_NAME, *KNOWN_PAPER_JARS):
        p = _existing(cand)
        if p:
            return p
    return None


def find_capture_mod_jar(explicit: Optional[str] = None, setup: Optional[dict] = None) -> Optional[str]:
    """The solaris-capture mod jar: --mod-jar, else the newest
    camera/capture-mod/build/libs/*.jar that is not a -sources/-dev jar (so a
    rebuild with a new version is picked up), else setup.json's."""
    setup = setup or {}
    for cand in (explicit, newest_mod_jar(REPO_ROOT / "camera" / "capture-mod"), setup.get("capture_mod_jar")):
        p = _existing(cand)
        if p:
            return p
    return None


def newest_mod_jar(mod_dir: Path) -> Optional[str]:
    libs = mod_dir / "build" / "libs"
    if not libs.is_dir():
        return None
    jars = [p for p in libs.glob("*.jar")
            if not p.stem.endswith(("-sources", "-dev", "-javadoc"))]
    if not jars:
        return None
    return str(max(jars, key=lambda p: p.stat().st_mtime).resolve())


def tool_version(cmd: Sequence[str], timeout: float = 30) -> Optional[str]:
    """First non-empty output line of `cmd` (stdout+stderr), or None."""
    try:
        r = subprocess.run(list(cmd), capture_output=True, text=True, timeout=timeout,
                           encoding="utf-8", errors="replace")
    except (OSError, subprocess.SubprocessError):
        return None
    for line in (r.stdout + "\n" + r.stderr).splitlines():
        if line.strip():
            return line.strip()
    return None


def jar_contains(jar: Path, suffix: str) -> bool:
    import zipfile
    try:
        with zipfile.ZipFile(jar) as zf:
            return any(n.endswith(suffix) for n in zf.namelist())
    except (OSError, zipfile.BadZipFile):
        return False


def port_in_use(port: int, host: str = "127.0.0.1") -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.5)
        return s.connect_ex((host, port)) == 0


# ---------------------------------------------------------------------------
# Minimal Source-RCON client (the protocol Minecraft uses)
# ---------------------------------------------------------------------------

class RconError(Exception):
    pass


class Rcon:
    """Blocking RCON client: `with Rcon(host, port, pw) as r: r.command("list")`."""

    AUTH, AUTH_RESPONSE, EXECCOMMAND, RESPONSE_VALUE = 3, 2, 2, 0

    def __init__(self, host: str, port: int, password: str, timeout: float = 10.0):
        self.host, self.port, self.password, self.timeout = host, int(port), password, timeout
        self.sock: Optional[socket.socket] = None
        self._next_id = 1

    def __enter__(self):
        self.connect()
        return self

    def __exit__(self, *exc):
        self.close()

    def connect(self) -> None:
        self.sock = socket.create_connection((self.host, self.port), timeout=self.timeout)
        req_id = self._send(self.AUTH, self.password)
        while True:
            rid, rtype, _ = self._recv()
            if rtype == self.AUTH_RESPONSE:
                if rid == -1:
                    raise RconError("RCON authentication failed")
                if rid == req_id:
                    return

    def command(self, cmd: str) -> str:
        if self.sock is None:
            self.connect()
        req_id = self._send(self.EXECCOMMAND, cmd)
        while True:
            rid, rtype, body = self._recv()
            if rid == req_id and rtype == self.RESPONSE_VALUE:
                return body

    def close(self) -> None:
        if self.sock is not None:
            try:
                self.sock.close()
            except OSError:
                pass
            self.sock = None

    def _send(self, ptype: int, body: str) -> int:
        req_id = self._next_id
        self._next_id += 1
        payload = struct.pack("<ii", req_id, ptype) + body.encode("utf-8") + b"\x00\x00"
        self.sock.sendall(struct.pack("<i", len(payload)) + payload)
        return req_id

    def _recv_exact(self, n: int) -> bytes:
        buf = b""
        while len(buf) < n:
            chunk = self.sock.recv(n - len(buf))
            if not chunk:
                raise RconError("RCON connection closed")
            buf += chunk
        return buf

    def _recv(self):
        (length,) = struct.unpack("<i", self._recv_exact(4))
        data = self._recv_exact(length)
        rid, rtype = struct.unpack("<ii", data[:8])
        return rid, rtype, data[8:-2].decode("utf-8", errors="replace")


def rcon_command(host: str, port: int, password: str, cmd: str, timeout: float = 10.0) -> str:
    with Rcon(host, port, password, timeout=timeout) as r:
        return r.command(cmd)


# ---------------------------------------------------------------------------
# Windows Job Objects
# ---------------------------------------------------------------------------

try:  # pywin32 (in the repo venv); optional
    import win32api
    import win32con
    import win32job
    HAVE_WIN32JOB = IS_WINDOWS
except ImportError:  # pragma: no cover - non-Windows or pywin32 missing
    win32api = win32con = win32job = None
    HAVE_WIN32JOB = False


def create_kill_on_close_job():
    """A Job Object whose processes are all killed when its last handle closes
    (i.e. at the latest when this Python process exits or crashes). None if
    Job Objects are unavailable."""
    if not HAVE_WIN32JOB:
        return None
    job = win32job.CreateJobObject(None, "")
    info = win32job.QueryInformationJobObject(job, win32job.JobObjectExtendedLimitInformation)
    info["BasicLimitInformation"]["LimitFlags"] |= win32job.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    win32job.SetInformationJobObject(job, win32job.JobObjectExtendedLimitInformation, info)
    return job


def assign_pid_to_job(job, pid: int) -> bool:
    if job is None:
        return False
    try:
        h = win32api.OpenProcess(win32con.PROCESS_SET_QUOTA | win32con.PROCESS_TERMINATE, False, pid)
        try:
            win32job.AssignProcessToJobObject(job, h)
        finally:
            win32api.CloseHandle(h)
        return True
    except Exception:  # noqa: BLE001 - pywintypes.error, process already gone, ...
        return False


CREATE_SUSPENDED = 0x00000004


def resume_process(handle) -> bool:
    """Resume every thread of a process created with CREATE_SUSPENDED
    (subprocess closes the main thread handle, so use NtResumeProcess)."""
    import ctypes
    ntdll = ctypes.WinDLL("ntdll")
    ntdll.NtResumeProcess.argtypes = [ctypes.c_void_p]
    ntdll.NtResumeProcess.restype = ctypes.c_long
    return ntdll.NtResumeProcess(ctypes.c_void_p(int(handle))) == 0


def put_self_in_kill_on_close_job():
    """Put the current process (and therefore every child it starts later) in a
    kill-on-close job, so Ctrl+C, a crash or closing the console never leaves
    orphan java/node/ffmpeg processes. Returns the job handle (keep it alive)."""
    job = create_kill_on_close_job()
    if job is None:
        return None
    try:
        win32job.AssignProcessToJobObject(job, win32api.GetCurrentProcess())
        return job
    except Exception:  # noqa: BLE001
        return None


def _descendants(pid: int) -> List[int]:
    """Linux: all descendants of `pid` from /proc (empty without /proc)."""
    kids: Dict[int, List[int]] = {}
    try:
        entries = [e for e in os.listdir("/proc") if e.isdigit()]
    except OSError:
        return []
    for e in entries:
        try:
            with open(f"/proc/{e}/stat", "rb") as fh:
                ppid = int(fh.read().rsplit(b")", 1)[1].split()[1])
        except (OSError, ValueError, IndexError):
            continue
        kids.setdefault(ppid, []).append(int(e))
    out, todo = [], [pid]
    while todo:
        for c in kids.get(todo.pop(), []):
            out.append(c)
            todo.append(c)
    return out


def kill_tree(pid: int) -> None:
    """Force-kill a process and its descendants."""
    if IS_WINDOWS:
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    else:
        # No Job Object here: the process group, plus descendants that left it
        # (setsid) found before the kill, since they are reparented after it
        rest = _descendants(pid)
        try:
            os.killpg(pid, 9)
        except OSError:
            try:
                os.kill(pid, 9)
            except OSError:
                pass
        for c in rest:
            try:
                os.kill(c, 9)
            except OSError:
                pass


# ---------------------------------------------------------------------------
# Child processes with timestamped log capture
# ---------------------------------------------------------------------------

class ManagedProcess:
    """A child process whose stdout+stderr are copied, line by line and with a
    UTC timestamp prefix (like `docker compose logs --timestamps`), into
    `log_path`. Each service runs in its own Job Object so `kill()` takes down
    the whole tree (e.g. launch_camera.py -> Minecraft java -> ffmpeg)."""

    def __init__(self, name: str, cmd: List[str], env: Dict[str, str], cwd: Path,
                 log_path: Path, stdin_pipe: bool = False):
        self.name, self.cmd, self.env, self.cwd = name, list(cmd), env, Path(cwd)
        self.log_path = Path(log_path)
        self.stdin_pipe = stdin_pipe
        self.proc: Optional[subprocess.Popen] = None
        self.job = None
        self._pump: Optional[threading.Thread] = None
        self._log_fh = None
        self._log_lock = threading.Lock()
        self.started_utc: Optional[str] = None
        self.ended_utc: Optional[str] = None
        self.start_monotonic = 0.0

    def start(self) -> "ManagedProcess":
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        self._log_fh = open(self.log_path, "ab")
        self.write_log_line(f"[run_windows] starting {self.name}: {subprocess.list2cmdline(self.cmd)}")
        flags = 0
        if IS_WINDOWS:
            # New process group: the console's Ctrl+C reaches only run_windows.py,
            # which then shuts the children down in order (stop files, RCON stop).
            flags |= subprocess.CREATE_NEW_PROCESS_GROUP
        job = create_kill_on_close_job()
        if job is not None:
            # Start suspended and join the job before the first instruction runs:
            # launcher shims (javapath\java.exe, the venv python.exe redirector)
            # spawn the real process at once, and that child must be in the job too.
            flags |= CREATE_SUSPENDED
        self.proc = subprocess.Popen(
            self.cmd,
            cwd=str(self.cwd),
            env=self.env,
            stdin=subprocess.PIPE if self.stdin_pipe else subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            creationflags=flags,
            start_new_session=not IS_WINDOWS,
        )
        if job is not None:
            try:
                win32job.AssignProcessToJobObject(job, int(self.proc._handle))
                self.job = job
            except Exception:  # noqa: BLE001 - kill() then falls back to taskkill /T
                self.job = None
            if not resume_process(self.proc._handle):
                self.proc.kill()
                self.proc.wait()
                self.write_log_line(f"[run_windows] could not resume suspended {self.name}")
                raise OSError(f"could not resume suspended process for {self.name}")
        self.started_utc = utc_now_iso()
        self.start_monotonic = time.monotonic()
        self._pump = threading.Thread(target=self._pump_output, name=f"log-{self.name}", daemon=True)
        self._pump.start()
        return self

    def write_log_line(self, text: str) -> None:
        if self._log_fh is None:
            return
        with self._log_lock:
            try:
                self._log_fh.write(f"{utc_now_iso()} {text}\n".encode("utf-8"))
                self._log_fh.flush()
            except (OSError, ValueError):
                pass

    def _pump_output(self) -> None:
        stream = self.proc.stdout
        for raw in iter(stream.readline, b""):
            line = raw.decode("utf-8", errors="replace").rstrip("\r\n")
            with self._log_lock:
                try:
                    self._log_fh.write(f"{utc_now_iso()} {line}\n".encode("utf-8"))
                    self._log_fh.flush()
                except (OSError, ValueError):
                    pass
        try:
            stream.close()
        except OSError:
            pass

    @property
    def pid(self) -> Optional[int]:
        return self.proc.pid if self.proc else None

    def poll(self) -> Optional[int]:
        if self.proc is None:
            return None
        rc = self.proc.poll()
        if rc is not None and self.ended_utc is None:
            self.ended_utc = utc_now_iso()
        return rc

    def alive(self) -> bool:
        return self.proc is not None and self.poll() is None

    def send_stdin(self, text: str) -> bool:
        if self.proc is None or self.proc.stdin is None:
            return False
        try:
            self.proc.stdin.write(text.encode("utf-8"))
            self.proc.stdin.flush()
            return True
        except OSError:
            return False

    def wait(self, timeout: Optional[float] = None) -> Optional[int]:
        if self.proc is None:
            return None
        try:
            self.proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            return None
        return self.poll()

    def terminate(self) -> None:
        """Polite stop of the root process only (SIGTERM / TerminateProcess)."""
        if self.alive():
            try:
                self.proc.terminate()
            except OSError:
                pass

    def kill(self) -> None:
        """Force-kill the whole process tree of this service."""
        if self.proc is None:
            return
        # Tree kill first, while the root (possibly a launcher shim) is alive, so
        # its descendants go even if one somehow escaped the job; then the job
        # sweeps whatever is left (including children of exited parents).
        if self.poll() is None:
            kill_tree(self.proc.pid)
        if self.job is not None:
            try:
                win32job.TerminateJobObject(self.job, 1)
            except Exception:  # noqa: BLE001
                pass
        self.wait(timeout=10)

    def stop(self, grace: float = 5.0) -> Optional[int]:
        """Wait up to `grace` seconds for a voluntary exit, then kill the tree."""
        if self.proc is None:
            return None
        if self.wait(timeout=grace) is None:
            self.write_log_line(f"[run_windows] {self.name} still running after {grace:.0f}s; killing process tree")
            self.kill()
        else:
            # The service exited; still sweep leftovers of its tree (job)
            if self.job is not None:
                try:
                    win32job.TerminateJobObject(self.job, 1)
                except Exception:  # noqa: BLE001
                    pass
        return self.poll()

    def close_log(self) -> None:
        if self._pump is not None:
            self._pump.join(timeout=5)
        with self._log_lock:
            if self._log_fh is not None:
                try:
                    self._log_fh.close()
                except OSError:
                    pass
                self._log_fh = None
