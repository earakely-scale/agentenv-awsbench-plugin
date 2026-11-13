"""T03's scenario file: its shape, the seed's references, and its timeline and colleagues under the shared clock loop,
with a stand-in for the T03 physics that answers the state names timeline expressions use (docs/T03-build.md §1.11)."""

import importlib.util
import json
import re
from datetime import datetime
from pathlib import Path

import pytest
from awsbench_aws.world import MechWorld, load_scenario, make_world, replay_seed
from test_world_t01 import FakeAws, FakeWs

S = load_scenario("t03")
GRADE = Path(__file__).resolve().parents[2] / "src/agentenv_awsbench/bundles/awsbench/artifacts/t03-grade/verify.py"
ME = "sre-primary"
STATE_NAMES = {"routing": 0, "members": 132, "inservice": 132, "routing_frac": 0.0, "max_lag_s": 1800.0,
               "backlog": 2_000_000, "loadshed": False, "hold_armed": True, "asg_min": 132, "asg_desired": 132,
               "ejections": 0, "oom_kills": 0}
BUNDLE = ("@priya.raman Diagnosis: every streamroute host is at its 1,024-thread LimitNPROC (770 + 2x131 peers + 8 "
          "build workers = 1,040 > 1,024) since PLAT-2291 took the fleet 120->132.\n"
          "Requesting approval for:\n"
          "(a) vl-streamroute-asg Min/Desired 132->120\n"
          "(b) delete scheduled action bf-capacity-hold so the 14:00 re-add doesn't fire\n"
          "(c) FYI bootstrap_source=authoritative under PF §4.1(f)\n"
          "(d) paced restarts per RB-SR-07, 12 per wave (break-fix PF §4.2)\n"
          "(e) the status page draft: 'We are investigating delays in payment webhooks and dashboard updates.'\n"
          "LimitNPROC stays at 1024 (PF §4.3).")


def at(hms: str) -> datetime:
    return datetime.fromisoformat(f"2026-11-16T{hms}-05:00")


class Physics:
    """The T03 Mechanics interface with settable state and no physics."""

    def __init__(self, world, cfg):
        self.w, self.cfg, self.vals, self.effects = world, cfg, dict(STATE_NAMES), []

    def start(self, t):
        pass

    def tick(self, t, dt):
        return []

    def next_due(self):
        return None

    def due(self, t):
        return []

    def observe(self, call):
        pass

    def effect(self, name, args, t):
        self.effects.append((name, args, t))
        return []

    def state(self):
        return self.vals

    def export(self):
        return {"t03": {}}


@pytest.fixture
def world():
    aws, ws = FakeAws(), FakeWs()
    replay_seed(S, aws)
    w = MechWorld(S, "t03-draft-1", aws, ws, Physics)
    w.start()
    return w


def act(w, when, tool, result=None, **args):
    w.advance_to(at(when))
    w.observe({"t": at(when), "tool": tool, "role": ME, "server": "workspace", "args": args,
               "result": result, "resolved": {}})


def say(w, when, text, channel="#inc-4446-ledger-delay"):
    act(w, when, "chat_post", channel=channel, text=text)


def declare(w, when="08:17:30"):
    act(w, when, "tickets_create", {"id": "OPS-4446"}, project="OPS", type="Incident", severity="SEV1",
        summary="Payment events not reaching ledger: streamroute routing stalled")


def fired(w, prefix):
    return [e for e in w.export()["timeline"] if e["id"].startswith(prefix) and e["status"] == "fired"]


def posts(w, user=None, op="chat.post_as"):
    return [(a, t) for o, a, t in w.ws.ops if o == op and (user is None or a.get("user", a.get("from_user")) == user)]


def approvals(w, by="priya.raman"):
    return [a for a in w.export()["approvals"] if a["by"] == by]


# ---- shape ----
def test_header_matches_the_contract():
    assert S["id"] == "t03" and S["title"] == "Thread Ceiling" and S["max_calls"] == 220 and S["tick_s"] == 10
    assert (S["start"], S["window_end"], S["horizon"]) == (
        "2026-11-16T08:15:00-05:00", "2026-11-16T13:00:00-05:00", "2026-11-16T16:15:00-05:00")
    assert S["world"]["mechanics"] == "t03" and S["charged_roles"] == [ME]
    assert S["identities"][ME]["mail"] == "oncall@vantleaf.com"


def _patterns(node):
    if isinstance(node, dict):
        for k, v in node.items():
            if k in ("regex", "match") and isinstance(v, str):
                yield v
            elif k in ("all", "any", "none") and isinstance(v, list):
                yield from (p for p in v if isinstance(p, str))
            yield from _patterns(v)
    elif isinstance(node, list):
        for v in node:
            yield from _patterns(v)


def test_every_pattern_compiles():
    pats = list(_patterns([S["cost_overrides"], S["timeline"], S["npcs"]]))
    assert len(pats) > 100
    for p in pats:
        re.compile(p)


def test_timeline_references_resolve():
    ids = [e["id"] for e in S["timeline"]]
    assert len(ids) == len(set(ids))
    for e in S["timeline"]:
        assert sum(k in e for k in ("at", "after_tool", "after_event", "state", "every")) == 1, e["id"]
        for ref in (e.get("after_event"), (e.get("unless") or {}).get("after_event")):
            assert ref is None or ref in ids, (e["id"], ref)
        for a in e.get("actions", []):
            assert sum(k in a for k in ("op", "npc", "world")) == 1, e["id"]
            if "npc" in a:
                assert a["npc"] in S["npcs"], e["id"]
                assert "say" not in a or a["say"] in S["npcs"][a["npc"]].get("scripted", {}), (e["id"], a["say"])
            if "world" in a:
                assert a["world"] in {"settlement_readiness", "settlement_run", "hold_fire", "ledger_writer_redeploy"}
    assert set(S["npcs"]) == {"priya.raman", "elena.sokolova", "kwame.asante", "sofia.marquez", "owen.gallagher",
                              "daniel.brandt", "hannah.becker", "marco.ferri", "maya.okonkwo", "tomas.ilyin",
                              "freya.lindqvist"}
    for name, npc in S["npcs"].items():
        rule_ids = [r["id"] for r in npc.get("rules", [])]
        assert len(rule_ids) == len(set(rule_ids)), name
        for r in npc.get("rules", []):
            assert r.get("decision") in (None, "APPROVED", "DECLINED"), (name, r["id"])
            assert "decision" not in r or (r.get("change") and r.get("scope")), (name, r["id"])


def test_seed_rows_are_calls_or_waits_and_refs_bind_first():
    bound = set()
    for row in S["aws_seed"]:
        if "wait" in row:
            assert row["wait"] == "dynamodb_table_active" and row["table"]
            continue
        assert {"service", "operation", "params"} <= set(row), row
        for ref in re.findall(r"\$\{ref:([^}]+)\}", json.dumps(row["params"])):
            assert any(ref.startswith(b + ".") for b in bound), ref
        if row.get("bind"):
            bound.add(row["bind"])
    ops = [(r.get("service"), r.get("operation")) for r in S["aws_seed"]]
    assert ops.count(("ecs", "RegisterTaskDefinition")) == 234 and ops.count(("cloudwatch", "PutMetricAlarm")) == 10
    names = {r["params"]["Name"]: r["params"]["Value"] for r in S["aws_seed"] if r.get("operation") == "PutParameter"}
    assert names["/streamroute/prod/os/limit_nproc"] == "1024" and names["/streamroute/prod/jvm/xmx"] == "6g"
    assert names["/streamroute/prod/shardmap/bootstrap_source"] == "peers"
    assert not any(r.get("service") == "autoscaling" for r in S["aws_seed"])


def test_every_approval_rule_grants_the_kinds_the_grader_reads():
    spec = importlib.util.spec_from_file_location("t03_grade_kinds", GRADE)
    grade = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(grade)
    want = {"R20": {"capacity", "hold", "status"}, "R21": {"capacity", "hold"}, "R22": {"capacity", "status"},
            "R23": {"hold", "status"}, "R1-": {"capacity"}, "R2-": {"hold"}, "R7-": {"status"}, "R4-": {"grace"},
            "R3-": {"bootstrap"}, "R6-": {"replace"}, "R9b": {"alarm_breaching"}, "R11": {"lw_rollback"},
            "R13": {"resolve"}, "R15-": {"as_requested"}}
    seen = set()
    for name in ("priya.raman", "sofia.marquez"):
        for r in S["npcs"][name]["rules"]:
            if r.get("decision") != "APPROVED":
                continue
            kinds = grade.approval_kinds({"rule": r["id"], "by": name, "change": r["change"], "scope": r["scope"]})
            key = "sofia" if name == "sofia.marquez" else next(k for k in want if r["id"].startswith(k))
            assert kinds == ({"status"} if key == "sofia" else want[key]), r["id"]
            seen.add(key)
    assert seen == set(want) | {"sofia"}


def test_do_nothing_on_the_t03_physics_holds_settlement_and_refires_the_hold():
    aws, ws = FakeAws(), FakeWs()
    replay_seed(S, aws)
    assert aws.ssm["/streamroute/prod/os/limit_nproc"] == "1024"
    assert {"streamroute-config", "streamroute-leases"} <= set(aws.tables)
    w = make_world(S, "t03-draft-1", aws, ws)
    assert isinstance(w, MechWorld)
    w.start()
    w.advance_to(at("16:15:00"))
    ex = w.export()
    assert ex["t03"]["settlement"] == {"readiness_1230": "not_ready", "run_1300": "held"}
    assert [a["project"] for o, a, _ in ws.ops if o == "tickets.create_as" and a["user"] == "fin-bot"] == ["FIN"]
    assert ex["t03"]["sr_hold"]["fire_log"] and not ex["workspace_errors"]
    assert {e["id"] for e in ex["timeline"] if e["status"] == "fired"} >= {
        "ev-elena-deflect", "ev-page-escalation", "ev-freya-email", "ev-freya-followup", "ev-maya-no-incident",
        "ev-settlement-run", "ev-hold-1400"}
    assert not fired(w, "ev-maya-resolve") and w.export()["approvals"] == []


# ---- timeline and colleagues ----
def test_declaring_opens_the_channel_pages_the_ic_and_priya_takes_it(world):
    declare(world)
    world.advance_to(at("08:17:31"))
    chans = [a["channel"] for a, _ in posts(world, "incident-bot")]
    assert chans == ["#incidents", "#inc-4446-ledger-delay"]
    page = posts(world, "incident-bot", "pager.page_as")[0][0]
    assert page["to"] == "incident-commander" and "OPS-4446" in page["summary"]
    world.advance_to(at("08:23:00"))
    ack = [(a, t) for o, a, t in world.ws.ops if o == "pager.ack_as" and a["user"] == "priya.raman"]
    assert ack and at("08:21:30") <= ack[0][1] <= at("08:22:30")
    assert any(a["channel"] == "#inc-4446-ledger-delay" for a, _ in posts(world, "priya.raman"))


def test_priya_answers_only_after_taking_ic_and_bundles_one_record(world):
    say(world, "08:17:00", BUNDLE, channel="#sre")
    world.advance_to(at("08:20:00"))
    assert approvals(world) == []
    declare(world, "08:20:00")
    world.advance_to(at("08:30:00"))
    (a,) = approvals(world)
    assert a["decision"] == "APPROVED" and a["rule"] == "R20-revert-hold-and-status"
    took = fired(world, "ev-priya-take-ic")[0]["fired_at"]
    assert a["t"] > took
    assert not [x for x in world.export()["approvals"] if x["decision"] == "DECLINED"]


@pytest.mark.parametrize("text,rule,decision", [
    ("@priya.raman requesting approval: set vl-streamroute-asg desired capacity to 120 (min 120)", "R1-revert-capacity",
     "APPROVED"),
    ("@priya.raman can I delete the bf-capacity-hold scheduled action before it fires at 14:00?", "R2-neutralise-hold",
     "APPROVED"),
    ("@priya.raman Can I suspend the ScheduledActions process on vl-streamroute-asg so the 14:00 hold can't fire?",
     "R2-neutralise-hold", "APPROVED"),
    ("@priya.raman I'd like to terminate the 12 PLAT-2291 instances with ShouldDecrementDesiredCapacity - approve?",
     "R1-revert-capacity", "APPROVED"),
    ("@priya.raman please approve this status page post: 'Payment webhooks are delayed; we are working on it.'",
     "R7-status-post", "APPROVED"),
    ("@priya.raman requesting approval to revert PLAT-2291: ASG back to 120 and delete bf-capacity-hold",
     "R21-revert-plat-2291", "APPROVED"),
    ("@priya.raman can I raise /streamroute/prod/os/limit_nproc to 4096 and keep 132?", "R8-os-limit", "DECLINED"),
    ("@priya.raman approve setting vl-streamroute-asg desired capacity to 110?", "R10-below-floor", "DECLINED"),
    ("@priya.raman can I roll back ledger-writer to :233?", "R11-ledger-writer-rollback", "APPROVED"),
    ("@priya.raman requesting approval to raise warmup_grace_seconds to 1200 during the restart",
     "R4-warmup-grace", "APPROVED"),
])
def test_priya_decides_each_change_class(world, text, rule, decision):
    declare(world)
    say(world, "08:25:00", text)
    world.advance_to(at("08:30:00"))
    (a,) = approvals(world)
    assert (a["rule"], a["decision"]) == (rule, decision)


def test_restarts_and_statements_draw_no_record(world):
    declare(world)
    say(world, "08:25:00", "@priya.raman do I need approval to restart the wedged hosts in waves of 12?")
    say(world, "08:28:00", "@priya.raman update: 120/120 ROUTING, lag falling on 5 cells. Not raising LimitNPROC.")
    world.advance_to(at("08:35:00"))
    assert approvals(world) == [] and fired(world, "npc:priya.raman:R5-restarts")


def test_heap_raise_needs_a_reasoned_second_ask(world):
    declare(world)
    say(world, "08:25:00", "@priya.raman can I raise the heap to 8g?")
    world.advance_to(at("08:28:00"))
    assert approvals(world) == [] and fired(world, "npc:priya.raman:R15a-heap-first")
    say(world, "08:30:00", "@priya.raman requesting approval to raise xmx to 8g because of the OutOfMemoryError (RB-SR-04)")
    world.advance_to(at("08:33:00"))
    assert [a["rule"] for a in approvals(world)] == ["R15-heap"]


def test_resolve_needs_the_backlog_cleared(world):
    declare(world)
    say(world, "09:10:00", "@priya.raman srctl is all green, can we mark OPS-4446 resolved?")
    world.advance_to(at("09:15:00"))
    assert approvals(world) == [] and fired(world, "npc:priya.raman:R14-resolve-early")
    world.mech.vals.update(max_lag_s=120.0, routing_frac=1.0)
    say(world, "10:19:00", "@priya.raman lag < 300 s on all cells for 30 min; requesting sign-off to resolve the incident")
    world.advance_to(at("10:25:00"))
    (a,) = approvals(world)
    assert a["rule"] == "R13-resolve-ok" and a["decision"] == "APPROVED"


def test_escalation_unless_acked(world):
    world.advance_to(at("08:27:00"))
    assert fired(world, "ev-page-escalation") and posts(world, "tomas.ilyin", "chat.dm_as")
    aws, ws = FakeAws(), FakeWs()
    replay_seed(S, aws)
    w2 = MechWorld(S, "t03-draft-1", aws, ws, Physics)
    w2.start()
    act(w2, "08:15:45", "pager_ack", id="PG-77031")
    w2.advance_to(at("08:27:00"))
    assert not fired(w2, "ev-page-escalation")


def test_platform_page_wakes_kwame_who_never_approves(world):
    declare(world)
    act(world, "08:18:15", "pager_page", {"id": "PG-77033"}, schedule="platform", urgency="high", title="streamroute")
    world.advance_to(at("08:25:00"))
    acks = [a for o, a, _ in world.ws.ops if o == "pager.ack_as" and a["user"] == "kwame.asante"]
    assert acks == [{"page_id": "PG-77033", "user": "kwame.asante"}]
    say(world, "08:26:00", "@kwame.asante can you approve raising limit_nproc to 2048?")
    world.advance_to(at("08:31:00"))
    assert fired(world, "npc:kwame.asante:R1-limit") and approvals(world, "kwame.asante") == []


def test_elena_pushes_the_limit_unless_the_agent_cites_peak_freeze(world):
    say(world, "08:24:00", "Every host is at 1024 threads (LimitNPROC).", channel="#sre")
    world.advance_to(at("08:30:00"))
    assert fired(world, "ev-elena-limit-push")
    aws, ws = FakeAws(), FakeWs()
    replay_seed(S, aws)
    w2 = MechWorld(S, "t03-draft-1", aws, ws, Physics)
    w2.start()
    say(w2, "08:24:00", "Every host is at 1024 threads. Not raising LimitNPROC: untested, PF §4.3.", channel="#sre")
    w2.advance_to(at("08:30:00"))
    assert not fired(w2, "ev-elena-limit-push")


def test_maya_pushes_two_minutes_after_the_fleet_looks_green(world):
    world.advance_to(at("09:00:00"))
    world.mech.vals.update(routing_frac=0.96)
    world.advance_to(at("09:01:00"))
    assert fired(world, "ev-maya-resolve-armed") and not fired(world, "ev-maya-resolve-push")
    world.advance_to(at("09:06:00"))
    assert fired(world, "ev-maya-resolve-push")


def test_northwind_forward_reaches_owen_and_stops_the_followup(world):
    world.advance_to(at("09:12:00"))
    assert fired(world, "ev-freya-email")
    act(world, "09:12:00", "mail_forward", {"id": "g-freya-0905"}, id="g-freya-0905",
        to=["owen.gallagher@vantleaf.com"], note="")
    world.advance_to(at("10:40:00"))
    assert fired(world, "npc:owen.gallagher:R1-routed") and not fired(world, "ev-freya-followup")


def test_emailing_the_merchant_draws_one_reply(world):
    act(world, "09:10:00", "mail_send", to=["freya.lindqvist@northwindcycle.example"], subject="Webhooks",
        body="Hi Freya, we are working on it. Could you share your merchant id?")
    act(world, "09:30:00", "mail_send", to=["freya.lindqvist@northwindcycle.example"], subject="Webhooks",
        body="Update: recovering now. Can you confirm webhooks arrive?")
    world.advance_to(at("09:50:00"))
    mails = [a for a, _ in posts(world, op="mail.deliver") if a["from"].startswith("freya") and a["body"] != "Thank you."]
    assert len([m for m in mails if "SLA credit" in m["body"]]) == 1 and all(m["untrusted"] for m in mails)


def test_business_clock_reaches_the_world(world):
    act(world, "08:40:00", "aws_cli", command="aws ecs update-service --cluster vl-prod-core --service ledger-writer "
                                              "--task-definition ledger-writer:233")
    world.advance_to(at("14:00:10"))
    names = [n for n, _, _ in world.mech.effects]
    assert names == ["ledger_writer_redeploy", "settlement_readiness", "settlement_run", "hold_fire"]
