# The solver container: the image AgentEnv deploys the claude-code agent from (published by agentenv-framework, by
# digest), plus the per-turn runner and the peer-agents server. Same Claude Code, Python, node, uid and cwd.
FROM {agent_image}
USER root
COPY main/ /opt/awsbench/
RUN chmod 0755 /opt/awsbench/entrypoint.sh
USER agent
WORKDIR /app
ENTRYPOINT ["/opt/awsbench/entrypoint.sh"]
CMD ["sleep", "infinity"]
