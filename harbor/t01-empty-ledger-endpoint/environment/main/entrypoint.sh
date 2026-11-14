#!/bin/sh
# The solver container: the published agentenv-claude-code image with the SDK's peer-agents MCP server on loopback,
# where the AgentEnv agent serves it, then Harbor's command.
set -e
mkdir -p /tmp/awsbench
python3 /opt/awsbench/peers.py > /tmp/awsbench/peers.log 2>&1 &
exec "$@"
