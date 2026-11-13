import json

import httpx
import pytest
from agentenv_protocol import DataPart
from awsbench_workspace.server import WorkspaceEnv, cost_class
from fastmcp import Client

ROLE = {"agentenv.io/role": "sre-primary"}


class FakeAws:
    """The aws server's meter routes: records what the workspace sends and answers like the meter would."""

    def __init__(self, begin: dict | None = None, fail: bool = False):
        self.calls: list[tuple[str, dict]] = []
        self.begin = begin or {"ok": True, "call_no": 7, "t_exec": "2026-11-17T02:41:05-05:00", "calls_left": 243}
        self.fail = fail

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if self.fail:
            raise httpx.ConnectError("no route to aws", request=request)
        phase = request.url.path.rsplit("/", 1)[-1]
        self.calls.append((phase, json.loads(request.content)))
        if phase == "begin":
            return httpx.Response(200, json=self.begin)
        return httpx.Response(200, json={"banner": {"now": "02:41:05 ET", "call": 7, "calls_left": 243,
                                                    "new": [{"kind": "dm", "from": "priya.raman"}]}})


def make(fake: FakeAws | None = None, standalone: bool = False) -> tuple[WorkspaceEnv, object]:
    env = WorkspaceEnv(aws_url="http://aws.test", standalone=standalone,
                       transport=httpx.MockTransport(fake or FakeAws()))
    app = env.create_app()
    env.apply("scenario.load", {"scenario": "t01"})
    return env, app


async def call(app, name: str, args: dict, meta: dict | None = ROLE) -> dict:
    async with Client(app) as c:
        r = await c.call_tool(name, args, meta=meta)
    assert len(r.content) == 1
    return json.loads(r.content[0].text)


def test_cost_classes():
    assert cost_class("wiki_get") == "point_read" and cost_class("chat_list_channels") == "point_read"
    assert cost_class("pager_oncall") == "point_read" and cost_class("chat_get_user") == "point_read"
    assert cost_class("chat_post") == "write" and cost_class("pager_ack") == "write"


async def test_serves_exactly_the_contract_tools():
    _, app = make()
    async with Client(app) as c:
        names = {t.name for t in await c.list_tools()}
    expected = {f"chat_{n}" for n in ("list_channels", "read", "search", "get_user", "post", "dm", "create_channel")} | \
        {f"tickets_{n}" for n in ("search", "get", "create", "comment", "update", "transition", "link")} | \
        {f"wiki_{n}" for n in ("search", "get", "history", "update", "restore", "comment")} | \
        {f"repo_{n}" for n in ("list", "get", "read", "search", "log")} | \
        {f"mail_{n}" for n in ("search", "read", "send", "reply", "forward")} | \
        {f"pager_{n}" for n in ("list", "get", "oncall", "ack", "page", "note", "resolve")} | \
        {f"status_{n}" for n in ("get", "create", "update")}
    assert names == expected


async def test_charged_call_is_metered_and_stamped_at_t_exec():
    fake = FakeAws()
    env, app = make(fake)
    out = await call(app, "tickets_update", {"key": "OPS-4452", "fields": {"severity": "SEV1"}})
    assert out["changed"] == ["severity"]
    assert out["_clock"] == {"now": "02:41:05 ET", "call": 7, "calls_left": 243,
                             "new": [{"kind": "dm", "from": "priya.raman"}]}
    (b, begin), (e, end) = fake.calls
    assert b == "begin" and begin == {"server": "workspace", "tool": "tickets_update", "role": "sre-primary",
                                      "args": {"key": "OPS-4452", "fields": {"severity": "SEV1"}}}
    assert e == "end" and end["call_no"] == 7 and end["tool"] == "tickets_update"
    assert end["resolved"]["ticket"] == "OPS-4452" and end["resolved"]["fields_changed"] == ["severity"]
    assert end["resolved"]["external"] is False
    hist = env.ws.store.history("tickets.issue", "OPS-4452")[-2]
    assert hist["t"] == "2026-11-17T02:41:05-05:00" and hist["by"] == "sre-primary"
    log = env.ws.store.action_log[-1]
    assert log["tool"] == "tickets_update" and log["call_no"] == 7 and log["charged"] is True


async def test_end_carries_the_record_id_the_world_acks():
    fake = FakeAws()
    _, app = make(fake)
    out = await call(app, "pager_page", {"schedule": "platform", "urgency": "high", "title": "ledger empty"})
    summary = fake.calls[-1][1]["result_summary"]
    assert summary["id"] == out["page"]["id"] and summary["error"] is None


async def test_status_post_is_external_in_resolved():
    fake = FakeAws()
    _, app = make(fake)
    await call(app, "status_create", {"name": "Checkout errors", "status": "investigating", "body": "Investigating."})
    assert fake.calls[-1][1]["resolved"]["external"] is True


async def test_meter_refusal_is_not_executed():
    fake = FakeAws(begin={"ok": False, "refuse": "Call budget exhausted (250/250); write nothing more and reply DONE"})
    env, app = make(fake)
    out = await call(app, "chat_post", {"channel": "#inc-4452", "text": "hello"})
    assert out["error"].startswith("Call budget exhausted") and "_clock" in out
    assert [p for p, _ in fake.calls] == ["begin"]
    assert not any(m["user"] == "sre-primary" for m in env.ws.store.items("chat.message"))


async def test_meter_unreachable_runs_uncharged_on_local_clock():
    _, app = make(FakeAws(fail=True))
    out = await call(app, "pager_ack", {"id": "PG-77052"})
    assert out["page"]["status"] == "acknowledged"
    assert "unreachable" in out["_clock"]["mode"] and out["_clock"]["now"] == "02:40:45 ET"


async def test_standalone_banner_carries_new_inbound_items():
    fake = FakeAws()
    env, app = make(fake, standalone=True)
    env.apply("chat.dm_as", {"from_user": "priya.raman", "to_user": "sre-primary", "text": "you keep IC"},
              "2026-11-17T02:40:30-05:00")
    out = await call(app, "chat_list_channels", {})
    assert out["_clock"]["mode"].startswith("standalone") and out["_clock"]["now"] == "02:40:50 ET"
    assert [i["kind"] for i in out["_clock"]["new"]] == ["dm"]
    assert (await call(app, "chat_list_channels", {}))["_clock"]["new"] == []
    assert fake.calls == []


async def test_roles():
    _, app = make()
    assert "no identity" in (await call(app, "wiki_get", {"page": "EPD-RB-7"}, meta=None))["error"]
    assert "no identity" in (await call(app, "wiki_get", {"page": "EPD-RB-7"},
                                        meta={"agentenv.io/role": "npc"}))["error"]
    bare = WorkspaceEnv(standalone=True)
    bare_app = bare.create_app()
    out = await call(bare_app, "chat_list_channels", {}, meta=None)
    assert out["channels"] == [] and bare.ws.store.action_log[-1]["actor"] == "sre-primary"


async def test_tool_errors_come_back_as_json():
    _, app = make(standalone=True)
    out = await call(app, "tickets_get", {"key": "OPS-1"})
    assert out["error"] == "no ticket OPS-1" and "_clock" in out


async def test_internal_routes():
    env = WorkspaceEnv(standalone=True)
    asgi = env.create_app().http_app()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=asgi), base_url="http://ws") as c:
        assert (await c.get("/awsbench/health")).json()["ok"] is True
        r = (await c.post("/awsbench/world/apply", json={"op": "scenario.load", "args": {"scenario": "t01"}})).json()
        assert r["ok"] and r["id"] == "t01" and r["records"] > 50
        seq = (await c.get("/awsbench/inbox")).json()["seq"]
        r = (await c.post("/awsbench/world/apply", json={
            "op": "mail.deliver", "at": "2026-11-17T03:04:00-05:00",
            "args": {"id": "g-callum-0304", "from": "callum.reyes@harborpinegoods.example",
                     "to": ["oncall@vantleaf.com"], "subject": "Is Vantleaf down?", "body": "ETA?", "untrusted": True}}
        )).json()
        assert r["id"] == "g-callum-0304" and r["deliveries"][0]["kind"] == "email"
        assert r["deliveries"][0]["t"] == "2026-11-17T03:04:00-05:00"
        inbox = (await c.get(f"/awsbench/inbox?since={seq}")).json()
        assert [i["kind"] for i in inbox["items"]] == ["email"]
        bad = await c.post("/awsbench/world/apply", json={"op": "chat.nuke", "args": {}})
        assert bad.status_code == 400 and bad.json()["ok"] is False
        missing = await c.post("/awsbench/world/apply", json={"op": "chat.post_as", "args": {"channel": "#sre"}})
        assert missing.status_code == 400
    assert env.ws.now.isoformat() == "2026-11-17T03:04:00-05:00"


def test_data_ops_and_clock_sync():
    env, _ = make(standalone=True)
    env.apply("chat.post_as", {"channel": "#inc-4452", "user": "hannah.becker", "text": "rollback?"}, None)
    data = env.get()[0].data
    assert set(data["collab"]) == {"chat", "tickets", "wiki", "repo", "mail", "pager", "status"}
    assert data["action_log"][-1]["op"] == "chat.post_as" and data["scenario"] == "t01"
    env.reset()
    assert env.get()[0].data["collab"] == {} and env.ws.identities == {}
    env.add([DataPart(data={"scenario": "t01"})])
    assert env.ws.store.get("tickets.issue", "OPS-4452") is not None
    assert env.sync_time("http://gateway:8080/clock/time", "America/New_York")["gateway"] == "http://gateway:8080"


def test_add_data_accepts_docs():
    env = WorkspaceEnv(standalone=True)
    env.add([DataPart(data={"scenario": "mini", "docs": {
        "world": {"start": "2026-11-17T02:40:00-05:00", "identities": {"sre-primary": {"chat": "me"}},
                  "users": [{"id": "me", "name": "Me"}]},
        "chat": {"channels": [{"name": "#x", "members": ["me"]}]}}})])
    assert env.ws.scenario == "mini" and env.ws.ident("sre-primary", "chat") == "me"
    with pytest.raises(ValueError, match="no scenario folder"):
        env.add([DataPart(data={"scenario": "does-not-exist"})])
