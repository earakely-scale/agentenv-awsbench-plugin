#!/bin/bash
# The AgentEnv side of the scripted-mode comparison, on the same host and commit as the Harbor trials:
#   harbor/qc/agentenv_modes.sh OUT_DIR MODE...
# For each mode: a fresh `agent-env run <bundle> --task t01-setup --keep`, where t01-setup is t01-draft.json's own steps
# 1-6 verbatim (deploy, configure with its seed, clock, tool access), written into a scratch copy of the bundle; then
# tests/e2e/driver.py --mode MODE against it (the path, then DONE through awsbench_control, the horizon twice, the grade
# without the judge); then the run is interrupted and the env's compose project torn down.
set -u
OUT=${1:?out dir}; shift
REPO=${REPO:-$(cd "$(dirname "$0")/../.." && pwd)}
AGENTENV=${AGENTENV:-agent-env}; PY=${PY:-python3}
mkdir -p "$OUT"
COPY=$(mktemp -d)
cp -a "$REPO/src" "$COPY/src"
"$PY" - "$COPY/src/agentenv_awsbench/bundles/awsbench/tasks" <<'PYEOF'
import json, sys
from pathlib import Path
tasks = Path(sys.argv[1])
steps = json.loads((tasks / "t01-draft.json").read_text())
assert [s["id"] for s in steps[:6]] == ["deploy", "configure", "clock", "lock-all", "allow-sre", "allow-harness"]
(tasks / "t01-setup.json").write_text(json.dumps(steps[:6], indent=2) + "\n")
PYEOF
for m in "$@"; do
  cd "$COPY" || exit 1
  PYTHONUNBUFFERED=1 "$AGENTENV" run src/agentenv_awsbench/bundles/awsbench --task t01-setup --keep \
    > "$OUT/keep-$m.log" 2>&1 < /dev/null &
  run=$!
  port=
  for _ in $(seq 400); do
    port=$(grep -oE 'gateway  http://127.0.0.1:[0-9]+' "$OUT/keep-$m.log" | grep -oE '[0-9]+$' | head -1)
    [ -n "$port" ] && break
    kill -0 "$run" 2>/dev/null || break
    sleep 3
  done
  if [ -z "$port" ]; then echo "$m: no gateway (see keep-$m.log)"; kill -INT "$run" 2>/dev/null; continue; fi
  cd "$REPO" || exit 1
  "$PY" tests/e2e/driver.py --gateway "http://127.0.0.1:$port" --mode "$m" > "$OUT/drive-$m.log" 2>&1
  echo "$m driver exit $?"
  proj=$(docker ps --filter "publish=$port" --format '{{.Label "com.docker.compose.project"}}' | head -1)
  kill -INT "$run" 2>/dev/null; sleep 20; kill "$run" 2>/dev/null
  [ -n "$proj" ] && docker compose -p "$proj" down -v --remove-orphans > /dev/null 2>&1
done
