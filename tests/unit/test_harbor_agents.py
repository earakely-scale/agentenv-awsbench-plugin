"""The Harbor package's agent side and verifier wrapper: what makes a trial count, and how answers cross Harbor's exec."""
import asyncio
import base64
import importlib.util
import json
import sys
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
PKG = ROOT / "harbor/t01-empty-ledger-endpoint"


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


sys.path.insert(0, str(PKG / "tests"))
run_grade = load("awsbench_run_grade", PKG / "tests/run_grade.py")
sys.path.insert(0, str(ROOT / "harbor/agents"))
harbor_stub = types.ModuleType("harbor")
for name in ("harbor.agents", "harbor.agents.base", "harbor.agents.capabilities"):
    sys.modules.setdefault(name, types.ModuleType(name))
sys.modules.setdefault("harbor", harbor_stub)
sys.modules["harbor.agents.base"].BaseAgent = getattr(sys.modules["harbor.agents.base"], "BaseAgent", object)
sys.modules["harbor.agents.capabilities"].AgentCapabilities = getattr(
    sys.modules["harbor.agents.capabilities"], "AgentCapabilities", lambda **kw: kw)
from awsbench_harbor import claude_code, harness_client  # noqa: E402


def write(folder, name, value):
    (folder / name).write_text(json.dumps(value))


def test_a_trial_without_a_hook_conversation_counts(tmp_path):
    assert run_grade.conversation_end(tmp_path, 0) is None


def test_a_conversation_that_recorded_no_end_is_refused(tmp_path):
    write(tmp_path, "agent-begin.json", {"agent": "x"})
    with pytest.raises(run_grade.Refused):
        run_grade.conversation_end(tmp_path, 12)


@pytest.mark.parametrize("end", ["agent_error", "infra_error"])
def test_a_harbor_side_failure_is_refused_unless_the_hook_had_ended_the_episode(tmp_path, end):
    write(tmp_path, "agent-begin.json", {"agent": "x"})
    write(tmp_path, "agent-end.json", {"end": end, "turns": 2, "tool_calls": 40, "error": "boom"})
    write(tmp_path, "hook-decisions.json", [{"turn": 1, "decision": {"message": [{"kind": "text", "text": "wake"}]}}])
    with pytest.raises(run_grade.Refused):
        run_grade.conversation_end(tmp_path, 40)
    write(tmp_path, "hook-decisions.json", [{"turn": 1, "decision": {"end": "done"}}])
    assert run_grade.conversation_end(tmp_path, 40)["end"] == end


def test_a_first_turn_that_failed_before_any_tool_call_is_refused(tmp_path):
    write(tmp_path, "agent-begin.json", {"agent": "x"})
    write(tmp_path, "agent-end.json", {"end": "failed_turn", "turns": 1, "tool_calls": 0, "turn_errors": ["401"]})
    with pytest.raises(run_grade.Refused):
        run_grade.conversation_end(tmp_path, 0)
    write(tmp_path, "agent-end.json", {"end": "failed_turn", "turns": 1, "tool_calls": 7, "turn_errors": ["529"]})
    assert run_grade.conversation_end(tmp_path, 7)["end"] == "failed_turn"


def test_the_judge_is_on_with_a_key_off_on_purpose_and_refused_without_one(monkeypatch):
    monkeypatch.delenv("AWSBENCH_JUDGE", raising=False)
    monkeypatch.delenv("LITELLM_API_KEY", raising=False)
    with pytest.raises(run_grade.Refused):
        run_grade.judge_mode()
    monkeypatch.setenv("AWSBENCH_JUDGE", "off")
    monkeypatch.setenv("LITELLM_API_KEY", "k")
    assert run_grade.judge_mode() == "off"
    import os
    assert "LITELLM_API_KEY" not in os.environ
    monkeypatch.setenv("AWSBENCH_JUDGE", "on")
    monkeypatch.setenv("LITELLM_API_KEY", "k")
    assert run_grade.judge_mode() == "on"


class FakeHarness:
    """service_exec as Harbor's docker environment gives it: stderr folded into stdout, in any order."""

    def __init__(self):
        self.files, self.commands = {}, []

    async def service_exec(self, command, service, user, timeout_sec):
        self.commands.append(command)
        out = "03:01:02 turn 1: reply sha256 abc, decision {...}\n"  # a stray log line on the merged stream
        if command.startswith("cat "):
            out = self.files[command.split()[1]]
        elif "post-turn" in command:
            self.files["/run/awsbench/out/decision-1.json"] = json.dumps({"decision": {"end": "done"}})
            out = '03:01:02{"decision": {"message": []}}'
        return types.SimpleNamespace(return_code=0, stdout=out, stderr=None)


def test_the_decision_is_read_from_its_file_not_from_the_merged_output():
    env = FakeHarness()
    decision = asyncio.run(harness_client.post_turn(env, 1, False, "DONE", 60))
    assert decision == {"end": "done"}
    assert env.commands[-1] == "cat /run/awsbench/out/decision-1.json"
    chunk = next(c for c in env.commands if "reply-chunk" in c)
    assert base64.b64decode(chunk.split()[-1]) == b"DONE"


def test_init_check_wants_both_servers_and_every_env_tool(tmp_path):
    tools = [f"mcp__env0042__t{i}" for i in range(51)] + [f"mcp__peer_agents__p{i}" for i in range(6)]
    event = {"type": "system", "subtype": "init", "tools": tools,
             "mcp_servers": [{"name": "env0042", "status": "connected"}, {"name": "peer_agents", "status": "connected"}]}
    stream = tmp_path / "s.jsonl"
    stream.write_text(json.dumps(event) + "\n")
    good = claude_code.init_check(stream, "env0042", 51)
    assert good["problems"] == [] and good["tools"] == 57
    event["mcp_servers"][1]["status"] = "failed"
    event["tools"] = tools[:51]
    stream.write_text(json.dumps(event) + "\n")
    assert len(claude_code.init_check(stream, "env0042", 51)["problems"]) == 2
    assert claude_code.init_check(tmp_path / "missing.jsonl", "env0042", 51) is None


def test_a_loopback_endpoint_becomes_the_docker_host():
    assert claude_code.container_url("http://127.0.0.1:4000/v1") == "http://host.docker.internal:4000/v1"
    assert claude_code.container_url("https://models.example.org") == "https://models.example.org"
