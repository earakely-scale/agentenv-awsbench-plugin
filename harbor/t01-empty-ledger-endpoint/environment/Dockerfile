# The solver container: the image AgentEnv deploys the claude-code agent from (published by agentenv-framework, by
# digest), plus the per-turn runner and the peer-agents server. Same Claude Code, Python, node, uid and cwd.
FROM ghcr.io/scaleapi/agentenv-claude-code@sha256:d07cb7fadc6a83c0f7f1e55ecfa806f7b8cecdd5fe27d940d548b61febb4dba6
USER root
COPY main/ /opt/awsbench/
RUN chmod 0755 /opt/awsbench/entrypoint.sh
USER agent
WORKDIR /app
ENTRYPOINT ["/opt/awsbench/entrypoint.sh"]
CMD ["sleep", "infinity"]
