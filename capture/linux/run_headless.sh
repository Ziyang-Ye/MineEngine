#!/usr/bin/env bash
[ -z "${BASH_VERSION:-}" ] && exec bash "$0" "$@"
# One offline capture instance (Paper + Alpha/Bravo + 2 camera clients with the solaris-capture mod)
# on a headless Linux GPU machine or inside one scheduler job (Slurm, Kubernetes, ...): Xvfb + vglrun -d egl, no network at
# run time. Several instances can share one GPU and one host network: PORTS=auto (default) takes a free port block and an X
# display per instance (linux/pick_ports.py).
#   bash linux/run_headless.sh STAGE=<staging dir> PY=<python 3.10+> OUT=<output dir> \
#        ACCEPT_MINECRAFT_EULA=1 [EPISODES=20 MODE=stateful BATCHES=1 TAG=<name> PORTS=auto|fixed EXTRA=--opt_SP_value
#        MINEENGINE_LOCAL_ROOT=/tmp]
# ACCEPT_MINECRAFT_EULA=1 states that YOU accept the Minecraft EULA (https://aka.ms/MinecraftEULA).
# Work files (worlds, MKVs, caches) go to a new directory mineengine_<TAG>.XXXXXX that the script creates under
# MINEENGINE_LOCAL_ROOT (node-local disk, default /tmp); it deletes only that directory, after the output is copied to OUT.
# Staged layout: linux/README.md.
set -uo pipefail
for kv in "$@"; do
  [[ "$kv" =~ ^[A-Za-z_][A-Za-z0-9_]*= ]] || { echo "run_headless.sh: arguments are KEY=VALUE (got: $kv)" >&2; exit 2; }
  export "$kv"; done
STAGE=${STAGE:?STAGE=<staging dir>}; OUT=${OUT:?OUT=<output dir>}; PY=${PY:?PY=<path of python 3.10+>}
REPO=${REPO:-$STAGE/repo/capture}
TAG=${TAG:-$(hostname)_$(date +%Y%m%d_%H%M%S)}
LOCAL_ROOT=${MINEENGINE_LOCAL_ROOT:-/tmp}                   # node-local disk; the work dir LOCAL is created below it
JAVA=${JAVA:-$STAGE/jdk-21/bin/java}                  # Paper server (Java 21+)
NODE=${NODE:-$(command -v node || echo node)}
FFMPEG_DIR=${FFMPEG_DIR:-$STAGE/ffmpeg}               # ffmpeg + ffprobe >= 5.1, libx264
PAPER_JAR=${PAPER_JAR:-$REPO/.cache/windows/paper-1.21-130.jar}
PAPER_REPO=${PAPER_REPO:-$REPO/.cache/windows/paper_repo}
MC_HOME=${MC_HOME:-$STAGE/minecraft}                  # Linux mc_home (launch_camera.py --install-only)
MOD_JAR=${MOD_JAR:-$(ls -t "$STAGE"/mod/solaris-capture*.jar 2>/dev/null | grep -Ev -- "-(sources|dev|javadoc)\.jar$" | head -1)}
XPREFIX=${XPREFIX:-$STAGE/xprefix}                    # Xvfb + VirtualGL unpacked with dpkg -x
XVFB=${XVFB:-$XPREFIX/usr/bin/Xvfb}
VGLRUN=${VGLRUN:-$XPREFIX/opt/VirtualGL/bin/vglrun}
X_LD=${X_LD-$XPREFIX/usr/lib/x86_64-linux-gnu:$XPREFIX/usr/lib:$XPREFIX/opt/VirtualGL/lib64:$XPREFIX/opt/VirtualGL/lib}
XVFB_ARGS=${XVFB_ARGS:-+extension GLX +render -noreset}
DISPLAY_NUM=${DISPLAY_NUM:-:99}
EPISODES=${EPISODES:-20}; MODE=${MODE:-stateful}; BATCHES=${BATCHES:-1}
EXTRA=${EXTRA:-}                                      # more run_windows.py options, spaces as _SP_
PORTS=${PORTS:-auto}                                  # auto: free port block + display per instance; fixed: the defaults
export VGL_READBACK=${VGL_READBACK:-none}             # the mod reads its own FBO; to verify (README)

mkdir -p "$OUT/$TAG"
exec > >(trap '' INT TERM; exec tee -a "$OUT/$TAG/job.log") 2>&1   # tee outlives a group-wide signal
echo "[job] $(hostname) $(date) TAG=$TAG"
nvidia-smi --query-gpu=name,driver_version,memory.total --format=csv,noheader || true
miss=0
for f in "$PY" "$JAVA" "$FFMPEG_DIR/ffmpeg" "$FFMPEG_DIR/ffprobe" "$PAPER_JAR" "$XVFB" "$VGLRUN" "${MOD_JAR:-<mod jar>}"; do
  [ -e "$f" ] || { echo "[job] missing: $f"; miss=1; }; done
command -v setsid >/dev/null || { echo "[job] missing: setsid (util-linux)"; miss=1; }
for d in "$REPO/node_modules/mineflayer" "$REPO/server/plugin-starter/node_modules/rcon-client" "$PAPER_REPO/versions" "$MC_HOME/versions"; do
  [ -d "$d" ] || { echo "[job] missing: $d"; miss=1; }; done
[ $miss = 0 ] || exit 2
if [ "$PORTS" = auto ]; then                         # game P+k, rcon P+10+k, recorders P+40 / P+50, pose link (UDP) P+60
  read P D < <("$PY" -I "$REPO/linux/pick_ports.py" "$TAG$(date +%s%N)") || true
  [ -n "${P:-}" ] || { echo "[job] pick_ports.py found no free port block"; exit 4; }
  EXTRA="$EXTRA _SP_--base-port_SP_${P}_SP_--base-rcon-port_SP_$((P + 10))_SP_--act-recorder-port_SP_$((P + 40))_SP_--coord-port_SP_$((P + 50))_SP_--pose-port_SP_$((P + 60))"
  EXTRA=${EXTRA// /}; DISPLAY_NUM=:$D; echo "[job] ports from $P, display $DISPLAY_NUM"
fi

# Work dir: always a new directory of our own (never an existing one), the only one the end of the script deletes
LOCAL=$(mktemp -d "$LOCAL_ROOT/mineengine_$TAG.XXXXXX") || { echo "[job] cannot create a work dir in $LOCAL_ROOT"; exit 2; }
echo "[job] work dir $LOCAL"
fail() { echo "[job] $1"; rm -rf "$LOCAL"; exit 2; }   # an early stop removes the work dir too

# NVIDIA EGL vendor file (the runtime mounts libEGL_nvidia but no glvnd json)
VD=$LOCAL/egl_vendor.d; mkdir -p "$VD"
for d in /usr/lib/x86_64-linux-gnu /usr/lib64 /usr/local/nvidia/lib64; do
  [ -e $d/libEGL_nvidia.so.0 ] && { printf '{"file_format_version":"1.0.0","ICD":{"library_path":"%s"}}\n' \
    $d/libEGL_nvidia.so.0 > "$VD/10_nvidia.json"; break; }; done
[ -s "$VD/10_nvidia.json" ] || fail "libEGL_nvidia.so.0 not found: no NVIDIA graphics userspace"
export __EGL_VENDOR_LIBRARY_DIRS=$VD
[ -n "$X_LD" ] && export LD_LIBRARY_PATH=$X_LD${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}
"$FFMPEG_DIR/ffmpeg" -hide_banner -encoders 2>/dev/null | grep -E " (h264_nvenc|libx264) " || true

# Writable copies on local disk (Minecraft extracts natives into mc_home; Paperclip checks its repo)
echo "[job] copying mc_home and paper repo to $LOCAL"
cp -a "$MC_HOME" "$LOCAL/minecraft" && cp -a "$PAPER_REPO" "$LOCAL/paper_repo" || fail "copy to $LOCAL failed"

EULA=(); [ "${ACCEPT_MINECRAFT_EULA:-}" = 1 ] && EULA=(--accept-minecraft-eula)
cd "$REPO" || fail "cannot cd to $REPO"
# Own session: only the SIGTERM forwarded below reaches the runner, not a terminal Ctrl+C or a signal to this
# script's process group (that plus the forwarded one would be a second signal = kill everything)
setsid "$PY" -u windows/run_windows.py ${EULA[@]+"${EULA[@]}"} --offline \
  --output-dir "$LOCAL/out" --mode "$MODE" --episodes "$EPISODES" --batches "$BATCHES" \
  --java "$JAVA" --node "$NODE" --python "$PY" --ffmpeg-dir "$FFMPEG_DIR" \
  --paper-jar "$PAPER_JAR" --paper-repo "$LOCAL/paper_repo" --mc-home "$LOCAL/minecraft" --mod-jar "$MOD_JAR" \
  --xvfb "$XVFB" --display "$DISPLAY_NUM" --xvfb-args="$XVFB_ARGS" --camera-prefix="$VGLRUN -d egl" \
  ${EXTRA//_SP_/ } < /dev/null &
PID=$!
# Schedulers stop a job with SIGTERM: pass it on (run_windows.py then stops cleanly, no alignment) and keep waiting
trap 'echo "[job] signal: forwarding SIGTERM to run_windows.py"; kill -TERM $PID 2>/dev/null' TERM INT
# batch_<i> is complete (aligned too) once batch_<i+1> exists: copy it to OUT while the next one runs,
# so a SIGKILL after the grace period loses at most the batch in progress
TRAIN=data_collection/train
( trap '' INT TERM; i=0
  while kill -0 $PID 2>/dev/null; do
    while [ -d "$LOCAL/out/$TRAIN/batch_$((i + 1))" ]; do
      mkdir -p "$OUT/$TAG/$TRAIN" && cp -a "$LOCAL/out/$TRAIN/batch_$i" "$OUT/$TAG/$TRAIN/" &&
        rm -rf "$LOCAL/out/$TRAIN/batch_$i" && echo "[job] batch_$i copied to $OUT/$TAG/$TRAIN" ||
        echo "[job] copy of batch_$i failed; retried at the end"
      i=$((i + 1)); done
    sleep 5; done ) &
COPIER=$!
wait $PID; RC=$?
while kill -0 $PID 2>/dev/null; do wait $PID; RC=$?; done
while kill -0 $COPIER 2>/dev/null; do wait $COPIER; done
echo "[job] run_windows.py exit $RC; copying the rest of $LOCAL/out -> $OUT/$TAG"
if cp -a "$LOCAL/out/." "$OUT/$TAG/"; then rm -rf "$LOCAL"; else echo "[job] copy failed: the local output stays in $LOCAL"; fi
echo "[job] done $(date)"
exit $RC
