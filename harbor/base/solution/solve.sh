#!/bin/sh
# The canonical solution: the reference path through the env's MCP tools, as the agent calls them.
# AWSBENCH_SOLUTION_MODE picks another path (alt, or a QC variant: nothing, trap, rollback, early, scale, weakshed, noshed).
set -e
exec python3 /solution/play.py "${AWSBENCH_SOLUTION_MODE:-oracle}"
