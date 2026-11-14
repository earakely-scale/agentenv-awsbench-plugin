#!/bin/bash
# Run the T01 QC trials on a Linux host with Docker, Harbor 0.26.0 and this repo checked out at REPO:
#   harbor/qc/run_qc.sh JOBS_DIR [PARALLEL] [TRIAL NAME REGEX]
# Model keys come from ~/.config/awsbench/litellm.env (LITELLM_BASE_URL, LITELLM_API_KEY), read into the
# environment, never echoed. Set A grades without the C11 judge (AWSBENCH_JUDGE=off, as tests/e2e/driver.py grades), so
# its D12 is comparable with the AgentEnv driver modes; set B is the owner's table with the judge on.
set -u
JOBS=${1:?jobs dir}; PAR=${2:-4}; ONLY=${3:-.}
REPO=${REPO:-$(cd "$(dirname "$0")/../.." && pwd)}
HARBOR=${HARBOR:-harbor}
cat > /tmp/awsbench-qc-trials.txt <<LIST
A-scripted-oracle|-|-a awsbench_harbor.scripted:ScriptedSolution --ak mode=oracle --ve AWSBENCH_JUDGE=off
A-scripted-alt|-|-a awsbench_harbor.scripted:ScriptedSolution --ak mode=alt --ve AWSBENCH_JUDGE=off
A-scripted-nothing|-|-a awsbench_harbor.scripted:ScriptedSolution --ak mode=nothing --ve AWSBENCH_JUDGE=off
A-scripted-trap|-|-a awsbench_harbor.scripted:ScriptedSolution --ak mode=trap --ve AWSBENCH_JUDGE=off
A-scripted-rollback|-|-a awsbench_harbor.scripted:ScriptedSolution --ak mode=rollback --ve AWSBENCH_JUDGE=off
A-scripted-early|-|-a awsbench_harbor.scripted:ScriptedSolution --ak mode=early --ve AWSBENCH_JUDGE=off
A-scripted-scale|-|-a awsbench_harbor.scripted:ScriptedSolution --ak mode=scale --ve AWSBENCH_JUDGE=off
A-scripted-weakshed|-|-a awsbench_harbor.scripted:ScriptedSolution --ak mode=weakshed --ve AWSBENCH_JUDGE=off
A-scripted-noshed|-|-a awsbench_harbor.scripted:ScriptedSolution --ak mode=noshed --ve AWSBENCH_JUDGE=off
B-oracle|oracle|-a oracle
B-alt|alt|-a oracle
B-nop|-|-a nop
B-trap|trap|-a oracle
B-rollback|rollback|-a oracle
B-early|early|-a oracle
LIST
run_one() {
  IFS='|' read -r name mode args <<< "$1"
  cd "$REPO" || exit 1
  set -a; . ~/.config/awsbench/litellm.env; set +a
  export PYTHONPATH="$REPO/harbor/agents"
  unset AWSBENCH_QC_SOLUTION_MODE
  [ "$mode" != "-" ] && export AWSBENCH_QC_SOLUTION_MODE="$mode"
  # shellcheck disable=SC2086
  "$HARBOR" run -p harbor/t01-empty-ledger-endpoint -o "$JOBS" --job-name "$name" -y $args > "$JOBS/$name.log" 2>&1
  echo "$name exit $?"
}
export -f run_one; export REPO JOBS HARBOR
mkdir -p "$JOBS"
grep -v '^$' /tmp/awsbench-qc-trials.txt | grep -E "$ONLY" | xargs -P "$PAR" -I{} bash -c 'run_one "$@"' _ {}
