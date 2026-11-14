"""The SDK's own peer-agents MCP server, served as the AgentEnv claude-code agent serves it: PeerAgents() from the
image's agentenv-framework-protocol, mounted at PEER_MCP_PATH on 127.0.0.1:8000 (its A2A_PORT), with no peers set
(T01 deploys one agent)."""
import uvicorn
from agentenv_protocol.a2a_agent.peer_agents import PEER_MCP_PATH, PeerAgents
from starlette.applications import Starlette
from starlette.routing import Route

peers = PeerAgents()
app = Starlette(routes=[Route(PEER_MCP_PATH, peers.app)], lifespan=lambda app: peers.serving())

if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=8000, log_level="warning")
