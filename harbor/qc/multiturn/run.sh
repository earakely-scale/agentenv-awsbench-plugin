#!/bin/bash
# The multi-turn path on both systems, at no model cost: one AgentEnv t01-draft run and one Harbor AgentEnvClaudeCode
# trial against recorder.py (tests/e2e/fake_model.py: turn 1 answers without DONE, so the task's hook wakes the agent
# with its digest; turn 2 answers DONE), each on its own port so the two request logs stay apart. Then compare.py.
#   harbor/qc/multiturn/run.sh OUT_DIR
# Run from the repo root on a Linux host with Docker, agent-env and Harbor; containers reach the recorder on the docker
# bridge. The judge is off on the Harbor side (the fake model is no judge); AgentEnv's grade is not compared.
set -u
OUT=${1:?out dir}; mkdir -p "$OUT"
REPO=${REPO:-$(cd "$(dirname "$0")/../../.." && pwd)}
AGENTENV=${AGENTENV:-agent-env}; HARBOR=${HARBOR:-harbor}; PY=${PY:-python3}
BRIDGE=${BRIDGE:-172.17.0.1}; MODEL=${MODEL:-anthropic/claude-sonnet-5-5}
cd "$REPO" || exit 1
"$PY" harbor/qc/multiturn/recorder.py --host "$BRIDGE" --port 18998 --out "$OUT/ae-requests.jsonl" &
ae_rec=$!
"$PY" harbor/qc/multiturn/recorder.py --host "$BRIDGE" --port 18997 --out "$OUT/hb-requests.jsonl" &
hb_rec=$!
sleep 2
(LITELLM_BASE_URL="http://$BRIDGE:18998" LITELLM_API_KEY=fake PYTHONUNBUFFERED=1 \
  "$AGENTENV" run src/agentenv_awsbench/bundles/awsbench --task t01-draft --model "$MODEL" \
  > "$OUT/agentenv-run.log" 2>&1 < /dev/null; echo "agentenv exit $?" >> "$OUT/status.txt") &
ae=$!
(LITELLM_BASE_URL="http://$BRIDGE:18997" LITELLM_API_KEY=fake PYTHONPATH="$REPO/harbor/agents" \
  "$HARBOR" run -p harbor/t01-empty-ledger-endpoint -a awsbench_harbor.claude_code:AgentEnvClaudeCode -m "$MODEL" \
  --ve AWSBENCH_JUDGE=off -o "$OUT/jobs" --job-name multiturn -y > "$OUT/harbor-run.log" 2>&1 < /dev/null
  echo "harbor exit $?" >> "$OUT/status.txt") &
hb=$!
wait $ae $hb
kill $ae_rec $hb_rec 2>/dev/null
trial=$(ls -d "$OUT"/jobs/multiturn/t01*/ 2>/dev/null | head -1)
"$PY" harbor/qc/multiturn/compare.py "$OUT/ae-requests.jsonl" "$OUT/hb-requests.jsonl" "$trial" > "$OUT/multiturn.json"
"$PY" -c "import json,sys; print(json.dumps(json.load(open(sys.argv[1]))['verdict']))" "$OUT/multiturn.json"
