"""AgentEnvClaudeCode: the AgentEnv claude-code agent, turn by turn, in the task's solver container.

Each turn runs /opt/awsbench/turn.py in `main` (built FROM the published agentenv-claude-code image), which calls the
image's own claude_bridge.run_claude() with the arguments the AgentEnv A2A server passes, so the CLI's argv,
environment, resumes and timeouts are AgentEnv's code. The model id is Harbor's -m, passed verbatim, as agent-env run
--model passes it; the endpoint and key ride the exec's environment from this process's LITELLM_BASE_URL and
LITELLM_API_KEY, never a file or an argument.

    PYTHONPATH=harbor/agents harbor run -p harbor/t01-empty-ledger-endpoint \\
        -a awsbench_harbor.claude_code:AgentEnvClaudeCode -m anthropic/claude-sonnet-5-5
"""
import json
import os
import uuid

from .conversation import HookConversation

CLAUDE_CODE_VERSION = "2.1.322"
PEER_AGENTS = {"url": "http://127.0.0.1:8000/ext/peer-agents/mcp", "headers": None}
CONTAINER_TURNS = "/tmp/awsbench/turns"


class AgentEnvClaudeCode(HookConversation):
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

    async def turn(self, environment, number, message):
        """Files go in and out with Harbor's upload and download (docker cp): the host's trial folder is not mounted in
        the solver container, so nothing is shared through /logs/agent."""
        prompt = self.params["prompt"]
        stem = f"{number:03d}"
        box = f"{CONTAINER_TURNS}/{stem}"
        (self.turns_dir / f"{stem}.prompt.txt").write_text(message, encoding="utf-8")
        spec = {
            "prompt_file": f"{box}.prompt.txt",
            "session_id": self.session_id,
            "model": self.model_name,
            "mcp_servers": {self.params["server_name"]: {"url": self.params["mcp_url"],
                                                         "headers": {"AgentEnv-Role": self.params["role"]}},
                            "peer_agents": PEER_AGENTS},
            "timeout_s": prompt["timeout_seconds"],
            "task_id": prompt["task_id"],
            "role": self.params["role"],
            "max_turns": prompt["max_turns"],
            "builtin_tools": prompt["builtin_tools"],
            "out": box,
        }
        (self.turns_dir / f"{stem}.spec.json").write_text(json.dumps(spec, indent=1), encoding="utf-8")
        await environment.exec(f"mkdir -p {CONTAINER_TURNS}", user="agent", timeout_sec=60)
        for kind in ("prompt.txt", "spec.json"):
            await environment.upload_file(self.turns_dir / f"{stem}.{kind}", f"{box}.{kind}")
        env = {k: os.environ[k] for k in ("LITELLM_API_KEY", "LITELLM_BASE_URL") if os.environ.get(k)}
        done = await environment.exec(f"python3 /opt/awsbench/turn.py {box}.spec.json", cwd="/app", env=env,
                                      user="agent", timeout_sec=prompt["timeout_seconds"] + 600)
        for kind in ("stream.jsonl", "result.json"):
            try:
                await environment.download_file(f"{box}.{kind}", self.turns_dir / f"{stem}.{kind}")
            except Exception:
                pass
        result_path = self.turns_dir / f"{stem}.result.json"
        if not result_path.exists():
            output = (done.stdout or "") + (done.stderr or "")  # Harbor's docker exec folds stderr into stdout
            return {"response": "", "error": f"turn.py exited {done.return_code}: {output[-1500:]}"}
        result = json.loads(result_path.read_text(encoding="utf-8"))
        await environment.download_file(result["session_file"], self.logs_dir / "agent-session.jsonl")
        result["metrics"] = self._metrics(stem, result)
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
