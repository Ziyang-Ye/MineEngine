#!/usr/bin/env bash
# Rebuild server/plugins/MirrorBot-1.0.jar (the episode-manager plugin, now
# including the world-state logger) inside a Maven + JDK 21 container, so no
# Java toolchain is needed on the host. Dependencies are cached in .cache/m2.
#
# Usage: server/build_plugin.sh
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
CACHE="$ROOT/.cache/m2"
mkdir -p "$CACHE"

docker run --rm \
  -u "$(id -u):$(id -g)" \
  -e HOME=/tmp \
  -v "$ROOT/server:/work/server" \
  -v "$CACHE:/tmp/m2" \
  -w /work/server/episode-manager-plugin \
  maven:3.9.9-eclipse-temurin-21 \
  mvn -B -q -Duser.home=/tmp -Dmaven.repo.local=/tmp/m2/repository package

JAR="$ROOT/server/plugins/MirrorBot-1.0.jar"
python3 - "$JAR" <<'EOF'
import sys, zipfile
jar = sys.argv[1]
names = zipfile.ZipFile(jar).namelist()
ok = any(n.endswith("WorldStateLogger.class") for n in names)
print(f"{jar}: {'contains' if ok else 'MISSING'} WorldStateLogger")
sys.exit(0 if ok else 1)
EOF
