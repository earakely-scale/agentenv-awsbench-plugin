#!/bin/bash
# The AgentEnv side of the scripted-mode comparison, on the same host and commit as the Harbor trials:
#   harbor/qc/agentenv_modes.sh OUT_DIR MODE...
# For each mode: a fresh `agent-env run <bundle> --task e2e-env --keep` (deploy, configure, clock, tool access: the
# T01 task's steps 1-6), tests/e2e/driver.py --mode MODE against it (the path, then DONE through awsbench_control, the
# horizon twice, the grade without the judge), then the env's compose project torn down.
set -u
OUT=${1:?out dir}; shift
REPO=${REPO:-$(cd "$(dirname "$0")/../.." && pwd)}
AGENTENV=${AGENTENV:-agent-env}; PY=${PY:-python3}
mkdir -p "$OUT"
for m in "$@"; do
  cd "$REPO" || exit 1
  "$AGENTENV" run src/agentenv_awsbench/bundles/awsbench --task e2e-env --keep > "$OUT/keep-$m.log" 2>&1 < /dev/null
  port=$(grep -oE 'gateway  http://127.0.0.1:[0-9]+' "$OUT/keep-$m.log" | grep -oE '[0-9]+$' | head -1)
  if [ -z "$port" ]; then echo "$m: no gateway (see keep-$m.log)"; continue; fi
  "$PY" tests/e2e/driver.py --gateway "http://127.0.0.1:$port" --mode "$m" > "$OUT/drive-$m.log" 2>&1
  echo "$m driver exit $?"
  proj=$(docker ps --filter "publish=$port" --format '{{.Label "com.docker.compose.project"}}' | head -1)
  [ -n "$proj" ] && docker compose -p "$proj" down -v --remove-orphans > /dev/null 2>&1
done
