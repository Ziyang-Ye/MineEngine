#!/usr/bin/env bash
# Single-laptop preset for stateful capture: ONE docker-compose instance
# (2 players: Alpha + Bravo, each = controller + camera + spectator),
# flat world, world-state logging on. See STATEFUL_CAPTURE.md.
#
# Usage:
#   ./run_laptop.sh --accept-minecraft-eula [--mode stateful|mix|all|diverse|TYPES]
#                   [--episodes N] [--batches N] [--output-dir DIR]
#                   [--world-seed SEED] [--reset-world] [--normal-world]
#                   [--snapshot-mode blocks|region|both|none] [-- <extra run.sh args>]
# Modes: diverse = all training types, category first (03 section 4 shares);
#        build | destroy | fight | move | interaction = one category;
#        creatures | mechanics | new = round-2 groups; else a comma list.
set -euo pipefail
cd "$(dirname "$0")"

MODE="stateful"
EPISODES=20
BATCHES=1
OUTPUT_DIR="output_laptop"
WORLD_SEED=""
RESET_FLAG=()
WORLD_FLAGS=(--num-flat-world 1 --num-normal-world 0)
SNAPSHOT_MODE="blocks"
ACCEPT_EULA=0
EXTRA=()

while [[ $# -gt 0 ]]; do
  case $1 in
    --mode) MODE="$2"; shift 2 ;;
    --episodes) EPISODES="$2"; shift 2 ;;
    --batches) BATCHES="$2"; shift 2 ;;
    --output-dir) OUTPUT_DIR="$2"; shift 2 ;;
    --world-seed) WORLD_SEED="$2"; shift 2 ;;
    --reset-world) RESET_FLAG=(--reset-world); shift ;;
    --normal-world) WORLD_FLAGS=(--num-flat-world 0 --num-normal-world 1); shift ;;
    --snapshot-mode) SNAPSHOT_MODE="$2"; shift 2 ;;
    --accept-minecraft-eula) ACCEPT_EULA=1; shift ;;
    --) shift; EXTRA=("$@"); break ;;
    -h|--help)
      sed -n '2,13p' "$0"; exit 0 ;;
    *) echo "Unknown option: $1" >&2; exit 1 ;;
  esac
done

if [[ "$ACCEPT_EULA" != "1" ]]; then
  cat >&2 <<'MSG'
The Paper server container only starts with EULA=TRUE, i.e. after the person
running it has accepted the Minecraft EULA (https://aka.ms/MinecraftEULA).
The server runs in offline mode and the camera clients log in offline.
If you accept it, re-run with --accept-minecraft-eula.
MSG
  exit 1
fi

# Episode selection
WEIGHTS=""
case "$MODE" in
  stateful) TYPES="stateful" ;;
  # all upstream training episodes + the 5 state episodes, state ones x3 (~half of the episodes)
  mix) TYPES="all"; WEIGHTS="igniteAndExtinguish=3,iceNearFire=3,tntBlast=3,lavaWater=3,buildAndBreak=3" ;;
  all) TYPES="all" ;;
  # round 2: aliases expanded by controller/episodes-loop.js
  diverse|build|destroy|fight|move|interaction|physics|creatures|mechanics|new) TYPES="$MODE" ;;
  *) TYPES="$MODE" ;;  # explicit comma-separated list, e.g. tntBlast,lavaWater
esac

# The server plugin must contain the world-state logger (rebuild once).
JAR="server/plugins/MirrorBot-1.0.jar"
if ! python3 - "$JAR" <<'EOF'
import sys, zipfile
sys.exit(0 if any(n.endswith("WorldStateLogger.class") for n in zipfile.ZipFile(sys.argv[1]).namelist()) else 1)
EOF
then
  echo "Building the server plugin (first run only)..."
  server/build_plugin.sh
fi

ARGS=(
  --output-dir "$OUTPUT_DIR"
  --num-batches "$BATCHES"
  "${WORLD_FLAGS[@]}"
  --num-episodes "$EPISODES"
  --episode-types "$TYPES"
  --snapshot-mode "$SNAPSHOT_MODE"
  --teleport-radius 2000
  --server-memory 2G
  --workers 4
  --filter-water-episodes false
  --skip-dataset
  --nvidia-disable-require
  --eula TRUE
)
[[ -n "$WEIGHTS" ]] && ARGS+=(--weight-mults "$WEIGHTS")
[[ -n "$WORLD_SEED" ]] && ARGS+=(--world-seed "$WORLD_SEED")
ARGS+=("${RESET_FLAG[@]}")
ARGS+=("${EXTRA[@]}")

echo "./run.sh ${ARGS[*]}"
./run.sh "${ARGS[@]}"

echo
echo "Done. Copy the whole output directory: $(cd "$OUTPUT_DIR" && pwd)"
