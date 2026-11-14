#!/bin/sh
# The solver container: the published agentenv-claude-code image with the SDK's peer-agents MCP server on loopback,
# where the AgentEnv agent serves it, then Harbor's command. The container starts only once that server answers; every
# turn then checks both MCP servers in Claude Code's init event.
set -e
mkdir -p /tmp/awsbench
python3 /opt/awsbench/peers.py > /tmp/awsbench/peers.log 2>&1 &
if ! python3 /opt/awsbench/wait_port.py 127.0.0.1 8000 60; then
  cat /tmp/awsbench/peers.log >&2
  exit 1
fi
exec "$@"
