#!/bin/sh
# The canonical solution: the reference path through the env's MCP tools, as the agent calls them. It always plays
# "oracle"; AWSBENCH_QC_SOLUTION_MODE (QC only) plays another path: alt, or an incorrect one (nothing, trap, rollback,
# early, scale, weakshed, noshed). The mode is the first line of /logs/agent/oracle.txt.
set -e
mode="${AWSBENCH_QC_SOLUTION_MODE:-oracle}"
echo "awsbench solution mode: $mode"
exec python3 /solution/play.py "$mode"
