#!/usr/bin/env bash
# CPU self-tests: no GPU, no Minecraft. Module tests, then the whole pipeline on synthetic episodes (mineengine.cap_synth writes
# captures in the real output format; cap_reader / cap2proxy are checked against what the generator logged; some checks feed
# broken inputs on purpose, so the logs contain expected tracebacks).
# The rendered run needs the block registry (python scripts/setup_assets.py, or MINEENGINE_HOME pointing at one).
set -uo pipefail
cd "$(dirname "$0")/.."
PY=${PY:-python}; WORK=${WORK:-$(mktemp -d)}; mkdir -p "$WORK"
REG=${MINEENGINE_REGISTRY:-${MINEENGINE_HOME:-$HOME/.cache/mineengine}/mc_registry_1.21.json}
t() {   # name, command...: run, keep the log, print its last line; stop at the first failure
  local n=$1; shift
  if "$@" > "$WORK/$n.log" 2>&1; then echo "ok   $n: $(tail -1 "$WORK/$n.log")"; else echo "FAIL $n (log $WORK/$n.log):"
    grep -n -E "FAIL|Error|Traceback" "$WORK/$n.log" | head -40; echo "--- last lines:"; tail -20 "$WORK/$n.log"; exit 1; fi
}
for m in cap_palette cap_reader cap_synth state_proxy mc_proxy_style; do t "$m" $PY -m mineengine.$m --test; done
t cap_selftest $PY -m mineengine.cap_selftest --work "$WORK/st"
t entity_refs $PY -m mineengine.entity_refs --test
if [ -f "$REG" ]; then
  t cap_selftest_cpu_render $PY -m mineengine.cap_selftest --work "$WORK/st_render" --cpu-render --registry "$REG"
  t block_stats $PY -m mineengine.block_stats --test --reg "$REG"
else
  echo "skip rendered run: no registry at $REG (python scripts/setup_assets.py)"
fi
echo "all CPU self-tests passed (logs in $WORK)"
