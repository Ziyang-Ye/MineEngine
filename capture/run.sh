# Modified by Ziyang Ye for MineEngine, 2026 (from SolarisEngine 78f23e7); see capture/MODIFICATIONS.md
set -e

# Defaults
BASE_DATA_DIR="output"
NUM_BATCHES=2
NUM_FLAT_WORLD=1
NUM_NORMAL_WORLD=1
NUM_EPISODES=2
DATASET_NAME="duet"
FILTER_WATER_EPISODES=true
# ---- stateful-capture options (see STATEFUL_CAPTURE.md) ----
EPISODE_TYPES="all"
WORLD_SEED=""
FLAT_NO_STRUCTURES=0
RESET_WORLD=0
WORLD_STATE_LOGGING=1
SNAPSHOT_MODE="blocks"
SNAPSHOT_RADIUS=32
TELEPORT_RADIUS=50000
WEIGHT_MULTS=""
RANDOM_TICK_SPEED=100
SERVER_MEMORY=""
EULA_VALUE="FALSE"
POSTPROCESS_WORKERS=32
SKIP_DATASET=false
NVIDIA_DISABLE_REQUIRE=0

# Parse CLI args
while [[ $# -gt 0 ]]; do
  case $1 in
    --output-dir)
      BASE_DATA_DIR="$2"
      shift 2
      ;;
    --num-batches)
      NUM_BATCHES="$2"
      shift 2
      ;;
    --num-flat-world)
      NUM_FLAT_WORLD="$2"
      shift 2
      ;;
    --num-normal-world)
      NUM_NORMAL_WORLD="$2"
      shift 2
      ;;
    --num-episodes)
      NUM_EPISODES="$2"
      shift 2
      ;;
    --dataset-name)
      DATASET_NAME="$2"
      shift 2
      ;;
    --filter-water-episodes)
      FILTER_WATER_EPISODES="$2"
      shift 2
      ;;
    --episode-types)
      EPISODE_TYPES="$2"
      shift 2
      ;;
    --world-seed)
      WORLD_SEED="$2"
      shift 2
      ;;
    --flat-no-structures)
      FLAT_NO_STRUCTURES=1
      shift
      ;;
    --reset-world)
      RESET_WORLD=1
      shift
      ;;
    --world-state)
      if [[ "$2" == "off" || "$2" == "0" ]]; then WORLD_STATE_LOGGING=0; else WORLD_STATE_LOGGING=1; fi
      shift 2
      ;;
    --snapshot-mode)
      SNAPSHOT_MODE="$2"
      shift 2
      ;;
    --snapshot-radius)
      SNAPSHOT_RADIUS="$2"
      shift 2
      ;;
    --teleport-radius)
      TELEPORT_RADIUS="$2"
      shift 2
      ;;
    --weight-mults)
      WEIGHT_MULTS="$2"
      shift 2
      ;;
    --random-tick-speed)
      RANDOM_TICK_SPEED="$2"
      shift 2
      ;;
    --server-memory)
      SERVER_MEMORY="$2"
      shift 2
      ;;
    --accept-minecraft-eula)
      EULA_VALUE="TRUE"
      shift
      ;;
    --eula)
      EULA_VALUE="$2"
      shift 2
      ;;
    --workers)
      POSTPROCESS_WORKERS="$2"
      shift 2
      ;;
    --skip-dataset)
      SKIP_DATASET=true
      shift
      ;;
    --nvidia-disable-require)
      NVIDIA_DISABLE_REQUIRE=1
      shift
      ;;
    -h|--help)
      echo "Usage: $0 --accept-minecraft-eula [OPTIONS]"
      echo "  --output-dir DIR       Base data directory (default: output)"
      echo "  --num-batches N        Number of batches (default: 2)"
      echo "  --num-flat-world N     Number of flat worlds per batch (default: 1)"
      echo "  --num-normal-world N   Number of normal worlds per batch (default: 1)"
      echo "  --num-episodes N       Number of episodes (default: 2)"
      echo "  --dataset-name NAME    Dataset name (default: duet)"
      echo "  --filter-water-episodes true|false  Filter water episodes (default: true)"
      echo "  --episode-types LIST   all | stateful | upstream | comma list of types (default: all)"
      echo "  --world-seed SEED      Fix the world seed (default: <instance><unix time>)"
      echo "  --flat-no-structures   Disable structure generation in flat worlds"
      echo "  --reset-world          Restore the episode box to its initial blocks after each episode"
      echo "  --world-state on|off   Per-episode world_events.jsonl + snapshots (default: on)"
      echo "  --snapshot-mode M      blocks | region | both | none (default: blocks)"
      echo "  --snapshot-radius R    Half-width of the snapshot box in blocks (default: 32)"
      echo "  --teleport-radius R    Radius for episode locations (default: 50000)"
      echo "  --weight-mults SPEC    Per-type sampling multipliers, e.g. tntBlast=2,lavaWater=2"
      echo "  --random-tick-speed N  randomTickSpeed during iceNearFire (default: 100)"
      echo "  --server-memory M      Paper server heap, e.g. 2G (default: image default)"
      echo "  --accept-minecraft-eula  You accept the Minecraft EULA (https://aka.ms/MinecraftEULA); required"
      echo "  --eula VALUE           Server EULA variable (default: FALSE; TRUE = same as --accept-minecraft-eula)"
      echo "  --workers N            Parallel postprocess workers (default: 32)"
      echo "  --skip-dataset         Stop after alignment (skip dataset preparation)"
      echo "  --nvidia-disable-require  Let camera containers start on drivers older than CUDA 13 (R580)"
      echo "  -h, --help             Show this help"
      exit 0
      ;;
    *)
      echo "Unknown option: $1" >&2
      echo "Use --help for usage"
      exit 1
      ;;
  esac
done

case "$EULA_VALUE" in
  TRUE|true|True) ;;
  *)
    cat >&2 <<'MSG'
The Paper server container only starts with EULA=TRUE, i.e. after the person
running it has accepted the Minecraft EULA (https://aka.ms/MinecraftEULA).
The server runs in offline mode and the camera clients log in offline.
If you accept it, re-run with --accept-minecraft-eula.
MSG
    exit 1
    ;;
esac

TOTAL_INSTANCES=$((NUM_FLAT_WORLD + NUM_NORMAL_WORLD))
if [[ $TOTAL_INSTANCES -gt 4 ]]; then
  echo "Error: total instances ($TOTAL_INSTANCES) exceeds the NVENC limit of 8 simultaneous encoding sessions per GPU." >&2
  echo "  NUM_FLAT_WORLD=$NUM_FLAT_WORLD + NUM_NORMAL_WORLD=$NUM_NORMAL_WORLD = $TOTAL_INSTANCES" >&2
  echo "  Please reduce the number of total instances to 4 or fewer." >&2
  exit 1
fi

BASE_DATA_COLLECTION_DIR=$BASE_DATA_DIR/data_collection/train
for ((i=0; i<NUM_BATCHES; i++)); do
    BATCH_NAME="batch_$i"
    BATCH_DIR="$BASE_DATA_COLLECTION_DIR/$BATCH_NAME"
    COMPOSE_DIR="$BATCH_DIR/compose_configs"

    python3 generate_compose.py \
        --compose_dir "$COMPOSE_DIR" \
        --base_port 25590 \
        --base_rcon_port 25600 \
        --act_recorder_port 8110 \
        --coord_port 8120 \
        --data_dir "$BATCH_DIR/data" \
        --output_dir "$BATCH_DIR/output" \
        --camera_output_alpha_base "$BATCH_DIR/camera/output_alpha" \
        --camera_output_bravo_base "$BATCH_DIR/camera/output_bravo" \
        --camera_data_alpha_base "$BATCH_DIR/camera/data_alpha" \
        --camera_data_bravo_base "$BATCH_DIR/camera/data_bravo" \
        --smoke_test 0 \
        --num_flatland_world $NUM_FLAT_WORLD \
        --num_normal_world $NUM_NORMAL_WORLD \
        --num_episodes $NUM_EPISODES \
        --eval_time_set_day 0 \
        --viewer_rendering_disabled 1 \
        --gpu_mode egl \
        --episode_types "$EPISODE_TYPES" \
        ${WORLD_SEED:+--world_seed "$WORLD_SEED"} \
        --flatland_world_disable_structures "$FLAT_NO_STRUCTURES" \
        --reset_world_between_episodes "$RESET_WORLD" \
        --world_state_logging "$WORLD_STATE_LOGGING" \
        --world_snapshot_mode "$SNAPSHOT_MODE" \
        --world_snapshot_radius "$SNAPSHOT_RADIUS" \
        --teleport_radius "$TELEPORT_RADIUS" \
        --episode_weight_multipliers "$WEIGHT_MULTS" \
        --state_random_tick_speed "$RANDOM_TICK_SPEED" \
        --server_memory "$SERVER_MEMORY" \
        --nvidia_disable_require "$NVIDIA_DISABLE_REQUIRE" \
        --eula "$EULA_VALUE"

    python3 orchestrate.py start --build --compose-dir "$COMPOSE_DIR" --logs-dir "$BATCH_DIR/logs"
    python3 orchestrate.py status --compose-dir "$COMPOSE_DIR" --logs-dir "$BATCH_DIR/logs"
    python3 orchestrate.py logs --compose-dir "$COMPOSE_DIR" --tail 20 --logs-dir "$BATCH_DIR/logs"
    python3 orchestrate.py stop --compose-dir "$COMPOSE_DIR"
    # Fix Docker root-owned file permissions so re-runs and postprocessing work
    ABS_BATCH_DIR="$(cd "$BATCH_DIR" && pwd)"
    docker run --rm -v "$ABS_BATCH_DIR:/workspace" alpine chown -R "$(id -u):$(id -g)" /workspace
    python3 orchestrate.py postprocess --compose-dir "$COMPOSE_DIR" --workers "$POSTPROCESS_WORKERS" --output-dir "$BATCH_DIR/aligned"
    if [[ "$WORLD_STATE_LOGGING" == "1" ]]; then
      python3 postprocess/verify_world_events.py "$BATCH_DIR" --report "$BATCH_DIR/world_events_report.json" || \
        echo "verify_world_events.py reported problems for $BATCH_DIR (see above)"
    fi
done

if [[ "$SKIP_DATASET" == "true" ]]; then
  echo "Skipping dataset preparation (--skip-dataset). Raw + aligned data is under $BASE_DATA_COLLECTION_DIR"
  exit 0
fi

if [[ "$FILTER_WATER_EPISODES" == "true" ]]; then
  echo "Detecting water episodes"
  python3 postprocess/detect_water_episodes_batch.py --base-path $BASE_DATA_COLLECTION_DIR --num-workers 8 --out-path $BASE_DATA_COLLECTION_DIR/water_episodes.json
fi

echo "Preparing train dataset"
python3 postprocess/prepare_train_dataset.py --source-dir $BASE_DATA_COLLECTION_DIR --destination-dir $BASE_DATA_DIR/datasets/$DATASET_NAME

if [[ "$FILTER_WATER_EPISODES" == "true" ]]; then
  echo "Filtering train dataset"
  python3 postprocess/filter_dataset.py --episodes-json $BASE_DATA_COLLECTION_DIR/water_episodes.json $BASE_DATA_DIR/datasets/$DATASET_NAME
fi

echo "Splitting train dataset"
python3 postprocess/split_train_test.py $BASE_DATA_DIR/datasets/$DATASET_NAME 

echo "Annotating some of the test split videos"
python3 postprocess/annotate_video_batch.py $BASE_DATA_DIR/datasets/$DATASET_NAME
