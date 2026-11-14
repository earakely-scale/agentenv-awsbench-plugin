#!/bin/bash
# Replay recorded T01 runs through both systems, judge off, and compare them call by call:
#   harbor/qc/replay/run.sh SCRIPTS_DIR OUT_DIR RUN...
# SCRIPTS_DIR holds extract.py's replay scripts (RUN.json, made from each run's Claude Code session). For each RUN:
# - AgentEnv: `agent-env run --task t01-setup --keep` (t01-draft.json's own steps 1-6, written into a scratch copy of
#   the bundle), then replay_agentenv.py (the calls through the gateway's /mcp as sre-primary, the framework's own
#   post-turn hook runner after each turn, the horizon, the grade), then teardown;
# - Harbor: the package with ReplaySolution (the same calls from the solver container through the front, the package's
#   hook in the harness after each turn, the verifier's collect and grade);
# - compare.py: every tool result, hook decision and record, both exports and every grade row.
set -u
SCRIPTS=${1:?scripts dir}; OUT=${2:?out dir}; shift 2
REPO=${REPO:-$(cd "$(dirname "$0")/../../.." && pwd)}; Q=$REPO/harbor/qc/replay
AGENTENV=${AGENTENV:-agent-env}; HARBOR=${HARBOR:-harbor}; PY=${PY:-python3}
mkdir -p "$OUT/ae" "$OUT/jobs"
COPY=$(mktemp -d); cp -a "$REPO/src" "$COPY/src"
"$PY" - "$COPY/src/agentenv_awsbench/bundles/awsbench/tasks" <<'PYEOF'
import json, sys
from pathlib import Path
tasks = Path(sys.argv[1])
steps = json.loads((tasks / "t01-draft.json").read_text())
assert [s["id"] for s in steps[:6]] == ["deploy", "configure", "clock", "lock-all", "allow-sre", "allow-harness"]
(tasks / "t01-setup.json").write_text(json.dumps(steps[:6], indent=2) + "\n")
PYEOF
for run in "$@"; do
  ae=$OUT/ae/$run; mkdir -p "$ae"; cd "$COPY" || exit 1
  PYTHONUNBUFFERED=1 "$AGENTENV" run src/agentenv_awsbench/bundles/awsbench --task t01-setup --keep \
    > "$ae/keep.log" 2>&1 < /dev/null &
  pid=$!; port=
  for _ in $(seq 400); do
    port=$(grep -oE 'gateway  http://127.0.0.1:[0-9]+' "$ae/keep.log" | grep -oE '[0-9]+$' | head -1)
    [ -n "$port" ] && break; kill -0 $pid 2>/dev/null || break; sleep 3
  done
  if [ -n "$port" ]; then
    proj=$(docker ps --filter "publish=$port" --format '{{.Label "com.docker.compose.project"}}' | head -1)
    gwc=$(docker ps --filter "publish=$port" --format '{{.Names}}' | head -1)
    tok=$(docker inspect --format '{{range .Config.Env}}{{println .}}{{end}}' "$gwc" | sed -n 's/^AGENTENV_CONTROL_TOKEN=//p')
    env -u LITELLM_API_KEY AGENTENV_CONTROL_TOKEN="$tok" "$PY" "$Q/replay_agentenv.py" "$SCRIPTS/$run.json" \
      "http://127.0.0.1:$port" "$COPY/src/agentenv_awsbench/bundles/awsbench" "$ae" > "$ae/replay.log" 2>&1
    kill -INT $pid 2>/dev/null; sleep 20; kill $pid 2>/dev/null
    [ -n "$proj" ] && docker compose -p "$proj" down -v --remove-orphans > /dev/null 2>&1
  else
    echo "$run: AgentEnv deploy failed (see $ae/keep.log)"; kill -INT $pid 2>/dev/null
  fi
  cd "$REPO" || exit 1
  env -u LITELLM_API_KEY -u LITELLM_BASE_URL PYTHONPATH="$REPO/harbor/agents:$Q/agents" "$HARBOR" run \
    -p harbor/t01-empty-ledger-endpoint -a awsbench_replay:ReplaySolution --ak script="$SCRIPTS/$run.json" \
    --ve AWSBENCH_JUDGE=off -o "$OUT/jobs" --job-name "hb-$run" -y > "$OUT/hb-$run.log" 2>&1
  trial=$(ls -d "$OUT/jobs/hb-$run"/t01*/ 2>/dev/null | head -1)
  "$PY" "$Q/compare.py" "$ae" "$trial" "$run" > "$OUT/cmp-$run.json" 2> "$OUT/cmp-$run.err"
  echo "$run compared: $OUT/cmp-$run.json"
done
