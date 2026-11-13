"""The awsbench-turn hook: DONE detection, the end branches, and a digest that is never empty."""
import importlib.util
import json
from pathlib import Path

ARTIFACTS = Path(__file__).resolve().parents[2] / "src/agentenv_awsbench/bundles/awsbench/artifacts"
spec = importlib.util.spec_from_file_location("awsbench_turn_hook", ARTIFACTS / "awsbench-turn" / "hook.py")
hook = importlib.util.module_from_spec(spec)
spec.loader.exec_module(hook)


class FakeEnv:
    def __init__(self, status=None, turn_end=None, env_id="awsbench"):
        self.env_id = env_id
        self.calls = []
        self.status = status or {"now": "2026-11-17T03:00:00-05:00", "ended": False, "calls_left": 100,
                                 "window_end": "2026-11-17T05:10:00-05:00"}
        self.turn_end = list(turn_end or [])

    async def call_tool(self, tool, args, role):
        assert tool == "awsbench_control" and role == "harness"
        self.calls.append(args)
        result = {"status": self.status, "end": {"ended": True}}.get(args["op"])
        if args["op"] == "turn_end":
            result = self.turn_end.pop(0)
        return {"content": [{"type": "text", "text": json.dumps(result)}]}


class FakeTurn:
    def __init__(self, reply, env, last=False):
        self.agent_reply, self.envs, self.last, self.recorded = reply, env if isinstance(env, list) else [env], last, []

    def record(self, entry):
        self.recorded.append(entry)

    def heartbeat(self):
        pass


def test_said_done():
    assert hook.said_done("All handed off.\n\nDONE")
    assert hook.said_done("handoff left on OPS-4452\n**DONE.**")
    assert hook.said_done("I'm DONE!")
    assert not hook.said_done("DONE with step 1, now checking the plan store")
    assert not hook.said_done("not done yet")
    assert not hook.said_done("")


async def test_done_ends_the_episode():
    env = FakeEnv()
    assert await hook.post_turn(FakeTurn("Handoff written.\nDONE", env), {}) == {"end": "done"}
    assert env.calls == [{"op": "end", "reason": "done"}]


async def test_finds_the_env_a_bundle_run_deploys():
    bundled = FakeEnv(env_id="@local/~/awsbench/src/agentenv_awsbench/bundles/awsbench/awsbench")
    assert await hook.post_turn(FakeTurn("Handoff written.\nDONE", bundled), {"env_id": "awsbench"}) == {"end": "done"}
    assert bundled.calls == [{"op": "end", "reason": "done"}]

    other, mine = FakeEnv(env_id="@local/x/mail"), FakeEnv(env_id="@local/x/awsbench")
    assert hook.find_env([other, mine], "awsbench") is mine
    assert hook.find_env([FakeEnv(env_id="@local/x/renamed")], "awsbench").env_id == "@local/x/renamed"
    assert hook.find_env([other, FakeEnv(env_id="@local/x/renamed")], "awsbench") is None

    turn = FakeTurn("DONE", [other, FakeEnv(env_id="@local/x/renamed")])
    assert await hook.post_turn(turn, {}) == {"end": "no_awsbench_env"}
    assert turn.recorded[-1]["envs"] == ["@local/x/mail", "@local/x/renamed"]


async def test_budget_and_window_end():
    env = FakeEnv(status={"now": "2026-11-17T04:00:00-05:00", "calls_left": 0, "window_end": "2026-11-17T05:10:00-05:00"})
    assert await hook.post_turn(FakeTurn("still working", env), {}) == {"end": "budget_exhausted"}
    env = FakeEnv(status={"now": "2026-11-17T05:10:00-05:00", "calls_left": 9, "window_end": "2026-11-17T05:10:00-05:00"})
    assert await hook.post_turn(FakeTurn("still working", env), {}) == {"end": "window_end"}


async def test_turn_end_digest_lists_what_arrived():
    item = {"t": "2026-11-17T03:02:10-05:00", "kind": "mention", "from": "hannah.becker", "where": "#inc-4452",
            "preview": "should we roll back checkout?"}
    env = FakeEnv(turn_end=[{"waited_s": 130, "now": "2026-11-17T03:02:10-05:00", "inbound": [item]}])
    answer = await hook.post_turn(FakeTurn("Looking at DNS next.", env), {})
    assert answer == {"message": "[03:02 ET] #inc-4452 hannah.becker: should we roll back checkout?"}
    assert env.calls[-1] == {"op": "turn_end", "max_s": 600.0}


async def test_quiet_turn_end_is_never_empty_and_chunks():
    env = FakeEnv(turn_end=[{"waited_s": 300, "now": "2026-11-17T03:05:00-05:00", "inbound": []},
                            {"waited_s": 300, "now": "2026-11-17T03:10:00-05:00", "inbound": []}])
    answer = await hook.post_turn(FakeTurn("waiting", env), {"chunk_s": 300})
    assert answer == {"message": "[03:10 ET] 10 minutes passed; nothing new."}
    assert [c["op"] for c in env.calls] == ["status", "turn_end", "turn_end"]


async def test_last_turn_and_errors_end():
    env = FakeEnv()
    assert await hook.post_turn(FakeTurn("working", env, last=True), {}) == {"end": "max_conversation_turns"}

    class Broken(FakeEnv):
        async def call_tool(self, tool, args, role):
            if args["op"] == "status":
                raise RuntimeError("gateway down")
            return await super().call_tool(tool, args, role)
    env = Broken()
    assert await hook.post_turn(FakeTurn("working", env), {}) == {"end": "hook_error"}
    assert env.calls[-1] == {"op": "end", "reason": "hook_error"}
