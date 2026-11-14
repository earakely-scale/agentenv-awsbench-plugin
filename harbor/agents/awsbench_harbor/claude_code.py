"""AgentEnvClaudeCode: the AgentEnv claude-code agent, turn by turn, in the task's solver container.

Each turn runs /opt/awsbench/turn.py in `main` (built FROM the published agentenv-claude-code image), which calls the
image's own claude_bridge.run_claude() with the arguments the AgentEnv A2A server passes, so the CLI's argv,
environment, resumes and timeouts are AgentEnv's code. The model id is Harbor's -m, passed verbatim, as agent-env run
--model passes it. The endpoint and key come from this process's LITELLM_BASE_URL and LITELLM_API_KEY and reach the
turn in a 0600 file that turn.py reads and deletes, never an argument or the exec's environment; a loopback endpoint
becomes host.docker.internal, as AgentEnv hands a loopback URL to a local container.

    PYTHONPATH=harbor/agents harbor run -p harbor/t01-empty-ledger-endpoint \\
        -a awsbench_harbor.claude_code:AgentEnvClaudeCode -m anthropic/claude-sonnet-5-5

`--ak effort=<level>` and `--ak max_thinking_tokens=<n>` are AgentEnv's per-run overrides agent_effort and
agent_max_thinking_tokens; unset, as in the task, they are None.

Every turn's `init` event must show both MCP servers connected with the task's tools (the env's, under the server name
the gateway drew, and the peer-agents server's); otherwise the turn raises InfraError and the trial is not scored.
"""
import hashlib
import json
import os
import tempfile
import uuid
from urllib.parse import urlsplit, urlunsplit

from .conversation import HookConversation, InfraError

CLAUDE_CODE_VERSION = "2.1.322"
PEER_AGENTS = {"url": "http://127.0.0.1:8000/ext/peer-agents/mcp", "headers": None}
PEER_SERVER = "peer_agents"
CONTAINER_TURNS = "/tmp/awsbench/turns"
LOOPBACK = {"localhost", "127.0.0.1", "::1", "0.0.0.0"}


def container_url(url):
    """A loopback endpoint as a container reaches it (AgentEnv's local sandbox does the same)."""
    parts = urlsplit(url)
    if parts.hostname in LOOPBACK:
        netloc = "host.docker.internal" + (f":{parts.port}" if parts.port else "")
        return urlunsplit(parts._replace(netloc=netloc))
    return url


def init_check(stream_path, server_name, env_tools):
    """The turn's init event: both servers connected, the env's tools all there; a digest of the tool names."""
    init = None
    if stream_path.exists():
        for line in stream_path.read_text(encoding="utf-8").splitlines():
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if event.get("type") == "system" and event.get("subtype") == "init":
                init = event
                break
    if init is None:
        return None
    status = {s.get("name"): s.get("status") for s in init.get("mcp_servers") or []}
    tools = sorted(init.get("tools") or [])
    ours = [t for t in tools if t.startswith(f"mcp__{server_name}__")]
    peers = [t for t in tools if t.startswith(f"mcp__{PEER_SERVER}__")]
    check = {"mcp_servers": status, "tools": len(tools), "env_tools": len(ours), "peer_tools": len(peers),
             "tools_sha256": hashlib.sha256("\n".join(tools).encode()).hexdigest()}
    problems = [f"{name} is {status.get(name)}" for name in (server_name, PEER_SERVER) if status.get(name) != "connected"]
    if len(ours) != env_tools:
        problems.append(f"{len(ours)} env tools, not {env_tools}")
    if not peers:
        problems.append("no peer-agents tools")
    check["problems"] = problems
    return check


class AgentEnvClaudeCode(HookConversation):
    def __init__(self, *args, effort=None, max_thinking_tokens=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.effort = effort or None
        self.max_thinking_tokens = int(max_thinking_tokens) if max_thinking_tokens else None

    @staticmethod
    def name():
        return "agentenv-claude-code"

    def version(self):
        return CLAUDE_CODE_VERSION

    async def setup(self, environment):
        await super().setup(environment)
        probe = await environment.exec("claude --version; sha256sum /usr/local/bin/claude", user="agent",
                                       timeout_sec=120)
        if CLAUDE_CODE_VERSION not in (probe.stdout or ""):
            raise RuntimeError(f"the solver container's Claude Code is not {CLAUDE_CODE_VERSION}: {probe.stdout}")
        (self.logs_dir / "claude-version.txt").write_text(probe.stdout or "")
        self.session_id = str(uuid.uuid4())
        self.turns_dir = self.logs_dir / "turns"
        self.turns_dir.mkdir(parents=True, exist_ok=True)

    async def _send_secrets(self, environment, target):
        values = {k: os.environ[k] for k in ("LITELLM_API_KEY", "LITELLM_BASE_URL") if os.environ.get(k)}
        if "LITELLM_BASE_URL" in values:
            values["LITELLM_BASE_URL"] = container_url(values["LITELLM_BASE_URL"])
        fd, local = tempfile.mkstemp(prefix="awsbench-turn-", suffix=".json")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(values, f)
            os.chmod(local, 0o600)
            await environment.upload_file(local, target)
        finally:
            os.unlink(local)
        await environment.exec(f"chown agent:agent {target} && chmod 0600 {target}", user="root", timeout_sec=60)

    async def turn(self, environment, number, message):
        """Files go in and out with Harbor's upload and download (docker cp): the host's trial folder is not mounted in
        the solver container, so nothing is shared through /logs/agent."""
        prompt = self.params["prompt"]
        stem = f"{number:03d}"
        box = f"{CONTAINER_TURNS}/{stem}"
        (self.turns_dir / f"{stem}.prompt.txt").write_text(message, encoding="utf-8")
        spec = {
            "prompt_file": f"{box}.prompt.txt",
            "env_file": f"{box}.env.json",
            "session_id": self.session_id,
            "model": self.model_name,
            "mcp_servers": {self.params["server_name"]: {"url": self.params["mcp_url"],
                                                         "headers": {"AgentEnv-Role": self.params["role"]}},
                            PEER_SERVER: PEER_AGENTS},
            "timeout_s": prompt["timeout_seconds"],
            "task_id": prompt["task_id"],
            "role": self.params["role"],
            "max_turns": prompt["max_turns"],
            "effort": self.effort,
            "max_thinking_tokens": self.max_thinking_tokens,
            "builtin_tools": prompt["builtin_tools"],
            "out": box,
        }
        (self.turns_dir / f"{stem}.spec.json").write_text(json.dumps(spec, indent=1), encoding="utf-8")
        await environment.exec(f"mkdir -p {CONTAINER_TURNS}", user="agent", timeout_sec=60)
        for kind in ("prompt.txt", "spec.json"):
            await environment.upload_file(self.turns_dir / f"{stem}.{kind}", f"{box}.{kind}")
        await self._send_secrets(environment, spec["env_file"])
        done = await environment.exec(f"python3 /opt/awsbench/turn.py {box}.spec.json", cwd="/app", user="agent",
                                      timeout_sec=prompt["timeout_seconds"] + 600)
        for kind in ("stream.jsonl", "result.json"):
            try:
                await environment.download_file(f"{box}.{kind}", self.turns_dir / f"{stem}.{kind}")
            except Exception:
                pass
        check = init_check(self.turns_dir / f"{stem}.stream.jsonl", self.params["server_name"],
                           self.params["sre_tools"])
        result_path = self.turns_dir / f"{stem}.result.json"
        if not result_path.exists():  # run_claude reports every CLI failure in its result; this is turn.py's own
            output = (done.stdout or "") + (done.stderr or "")  # Harbor's docker exec folds stderr into stdout
            raise InfraError(f"turn.py exited {done.return_code} without a result: {output[-1500:]}")
        result = json.loads(result_path.read_text(encoding="utf-8"))
        if check is not None and check["problems"]:
            raise InfraError(f"turn {number} ran without the task's tools: {check['problems']}")
        try:
            await environment.download_file(result["session_file"], self.logs_dir / "agent-session.jsonl")
        except Exception as e:
            if not result.get("error"):
                raise InfraError(f"turn {number}'s session file could not be read back: {e!r}") from e
        result["metrics"] = {**self._metrics(stem, result), "init": check}
        if check is None and not result.get("error"):
            raise InfraError(f"turn {number} has no init event to check its MCP servers by")
        return result

    def _metrics(self, stem, result):
        out = {"tool_call_count": result.get("tool_call_count"), "attempts": result.get("attempts")}
        stream = self.turns_dir / f"{stem}.stream.jsonl"
        if stream.exists():
            for line in stream.read_text(encoding="utf-8").splitlines():
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if event.get("type") == "result" and event.get("total_cost_usd") is not None:
                    out["cost_usd"] = event["total_cost_usd"]
        return out

