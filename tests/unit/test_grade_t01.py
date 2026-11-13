"""grade_from_inputs on synthetic T01 exports: the oracle passes, doing nothing scores near zero, the stale-plan trap is
capped, and the step rows aggregate to D12."""
import copy
import importlib.util
from pathlib import Path

import pytest

ARTIFACTS = Path(__file__).resolve().parents[2] / "src/agentenv_awsbench/bundles/awsbench/artifacts"


def load(name, file):
    spec = importlib.util.spec_from_file_location(f"t01_{name}", ARTIFACTS / name / file)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)  # not registered in sys.modules, as env_outcome_verifier loads it
    return module


grade = load("t01-grade", "verify.py")
LEDGER = "ledger.internal.vantleaf.io"
OTHER = "orders.internal.vantleaf.io"
GOOD = [{"SetIdentifier": f"g48199-{i}", "Value": ip, "Weight": 100, "TTL": 60}
        for i, ip in enumerate(["10.20.11.152", "10.20.11.38", "10.20.44.40", "10.20.76.144", "10.20.76.30"])]
STALE = [{"SetIdentifier": "g48177-0", "Value": "10.20.76.29", "Weight": 100, "TTL": 60},
         {"SetIdentifier": "g48177-1", "Value": "10.20.44.77", "Weight": 100, "TTL": 60}]
OTHER_RECS = [{"SetIdentifier": "g48199-0", "Value": "10.20.30.4", "Weight": 100, "TTL": 60}]


def t(hhmm):
    return f"2026-11-17T{hhmm if hhmm.count(':') == 2 else hhmm + ':00'}-05:00"


def plan(gen):
    """A plan item as the world writes it: the ledger's records inline, the others by reference."""
    return {"pk": f"plan#{gen:010d}", "generation": gen, "status": "APPLIED", "endpoints_ref": "endpoint_table",
            "ledger": [{"set_id": r["SetIdentifier"], "ip": r["Value"], "weight": r["Weight"]}
                       for r in (GOOD if gen == 48199 else STALE)]}


def per_minute(points):
    """[(hh:mm, value)] held until the next point, sampled each minute 02:40-05:10."""
    out, value, marks = [], None, dict(points)
    for minute in range(2 * 60 + 40, 5 * 60 + 11):
        key = "{:02d}:{:02d}".format(*divmod(minute, 60))
        value = marks.get(key, value)
        out.append([t(key), value])
    return out


class Log:
    def __init__(self):
        self.aws, self.ws, self.n = [], [], 0

    def ws_call(self, at, tool, **params):
        self.n += 1
        self.ws.append({"seq": self.n, "t": t(at), "call_no": self.n, "role": "sre-primary", "principal": "sre-primary",
                        "world_made": False, "server": "workspace", "tool": tool, "params": params})

    def aws_call(self, at, tool, params=None, effects=()):
        self.n += 1
        call_no = self.n
        self.aws.append({"seq": len(self.aws), "t": t(at), "call_no": call_no, "role": "sre-primary",
                         "principal": "sre-primary", "world_made": False, "server": "aws", "tool": tool,
                         "service": "", "operation": "", "params": params or {}})
        for service, operation, p in effects:
            self.aws.append({"seq": len(self.aws), "t": t(at), "call_no": call_no, "role": "sre-primary",
                             "principal": "sre-primary", "world_made": False, "server": "aws", "tool": tool,
                             "service": service, "operation": operation, "params": p})


def base_exports(log, *, done, ledger_ok_at=None, endpoints_final=40, checkout_ok_at=None, restored_gen=None):
    zone = {LEDGER: [], OTHER: OTHER_RECS}
    store = {"applied#" + LEDGER: {"generation": 48177}, "applied#" + OTHER: {"generation": 48199},
             "plan#0000048199": plan(48199)}
    start = {"route53": {"internal.vantleaf.io": copy.deepcopy(zone)},
             "dynamodb": {"endpointd-plans": copy.deepcopy(store)}, "ssm": {}}
    if restored_gen:
        zone[LEDGER] = GOOD if restored_gen == 48199 else STALE
        store["applied#" + LEDGER] = {"generation": restored_gen}
        if restored_gen == 48177:
            store["plan#0000048177"] = plan(48177)
    paused = log.paused_at if hasattr(log, "paused_at") else None
    procs = {"planner": True, "enactor-a": True, "enactor-b": True, "enactor-c": True}
    switches = [{"t": t("02:40"), "switches": {"a": True, "b": True, "c": True, "planner": True}, "processes": procs}]
    if paused:
        switches.append({"t": t(paused), "switches": {"a": False, "b": False, "c": False, "planner": False},
                         "processes": procs})
    aws = {
        "episode": {"scenario": "t01", "seed": "unit", "mode": "eval", "start": t("02:40"), "window_end": t("05:10"),
                    "horizon": t("05:10"), "now": t("05:10"), "ended": True, "end_reason": "done", "done_at": t(done),
                    "calls_used": log.n},
        "action_log": log.aws,
        "world": {
            "endpointd": {
                "ledger_correct_series": [[t("02:40"), False]] + ([[t(ledger_ok_at), True]] if ledger_ok_at else []),
                "endpoints_correct_series": [[t("02:40"), 40]] + (
                    [[t(ledger_ok_at), endpoints_final]] if ledger_ok_at else [[t("05:10"), endpoints_final]]),
                "kill_switch_timeline": switches},
            "dns": {"per_host_recovered_at": {h: (t("03:05") if ledger_ok_at else None)
                                              for h in ("core-a-01", "core-b-01", "core-c-01")}},
            "ledger": {"collapsed_seconds_total": 0, "regime_timeline": [[t("02:40"), "healthy"]],
                       "webhook_backlog": 300 if checkout_ok_at else 17600,
                       "outbox_pending": 50 if checkout_ok_at else 7500},
            "traffic": {"checkout_success_1m": per_minute(
                [("02:40", 0.04)] + ([(checkout_ok_at, 0.995)] if checkout_ok_at else []))},
            "timeline": [], "approvals": [], "fleet_jobs": []},
        "aws_state": {"route53": {"internal.vantleaf.io": zone}, "dynamodb": {"endpointd-plans": store}, "ssm": {}},
        "start_state": start,
    }
    return aws, {"collab": {}, "action_log": log.ws}


def oracle_log(gen=48199):
    log = Log()
    log.ws_call("02:40:45", "pager_ack", id="PG-77052")
    log.ws_call("02:41:50", "tickets_update", key="OPS-4452", fields={"severity": "SEV1", "assignee": "sre-primary"})
    log.ws_call("02:42:35", "chat_post", channel="#inc-4452", text="I'm IC. SEV1 declared on OPS-4452. Investigating.")
    log.ws_call("02:45:15", "pager_page", schedule="platform", urgency="high", title="OPS-4452 SEV1: ledger.internal empty")
    log.ws_call("02:46:00", "status_create", components=["checkout"], status="investigating",
                title="Checkout errors", body="Some payments are failing. We are investigating.")
    log.aws_call("02:49:00", "fleet_exec", {"host": "endpointd-enactor-b", "command": "epctl pause all"},
                 [("ssm", "PutParameter", {"Name": f"/platform/endpointd/prod/{e}/enabled", "Value": "false",
                                           "Overwrite": True}) for e in ("a", "b", "c", "planner")])
    log.paused_at = "02:49:00"
    log.ws_call("02:50:00", "chat_post", channel="#inc-4452",
                text="CHANGE: paused endpointd enactors (epctl pause all) | why: EPD-RB-7 step 1 | undo: epctl resume | "
                     "auth: PF 4.1(c)")
    log.aws_call("02:51:45", "aws_cli", {"cmd": "ssm put-parameter ..."},
                 [("ssm", "PutParameter", {"Name": "/vl/prod/webhooks-dispatcher/max_concurrency", "Value": "4",
                                           "Overwrite": True})])
    ledger_change = {"HostedZoneId": "Z1", "ChangeBatch": {"Changes": [
        {"Action": "UPSERT", "ResourceRecordSet": {"Name": LEDGER + ".", "Type": "A"}}]}}
    log.aws_call("02:52:30", "fleet_exec",
                 {"host": "endpointd-enactor-b",
                  "command": f"epctl apply --gen {gen} --endpoint {LEDGER} --manual --reason OPS-4452"},
                 [("route53", "ChangeResourceRecordSets", ledger_change),
                  ("dynamodb", "PutItem", {"TableName": "endpointd-plans",
                                           "Item": {"pk": {"S": "applied#" + LEDGER}, "generation": {"N": str(gen)}}})])
    log.ws_call("02:54:00", "chat_post", channel="#inc-4452",
                text=f"CHANGE: max_concurrency 4 on webhooks-dispatcher; restored {LEDGER} from {gen} with epctl apply"
                     " | why: EPD-RB-7 | undo: epctl apply previous | auth: IP 3.4")
    log.ws_call("03:00:00", "mail_forward", id="m-callum-0304", to=["owen.gallagher@vantleaf.com"],
                note="Harbor & Pine escalation for Merchant Success")
    log.aws_call("03:08:00", "fleet_exec", {"host": "core-a-01", "command": f"dig +short {LEDGER}"})
    log.aws_call("03:08:30", "fleet_exec", {"host": "core-b-01", "command": f"dig +short {LEDGER}"})
    log.ws_call("03:12:00", "status_update", id="st-1", status="monitoring",
                body="A fix is in place and checkout is recovering. We are monitoring.")
    log.ws_call("03:30:00", "chat_post", channel="#inc-4452", text="Checkout stable at 99.5%.")
    log.ws_call("04:20:00", "tickets_comment", key="OPS-4452", body="Handoff: endpointd race (PLAT-412) ...")
    return log


def score_of(rows):
    total = sum(r["weight"] for r in rows if r.get("weight", 1) > 0)
    return sum(r["score"] * r["weight"] for r in rows if r.get("weight", 1) > 0) / total


def test_oracle_passes():
    aws, ws = base_exports(oracle_log(), done="04:30", ledger_ok_at="02:52:30", endpoints_final=41,
                           checkout_ok_at="03:05", restored_gen=48199)
    graded = grade.grade_from_inputs(aws, ws)
    failing = {r["id"]: r["detail"] for r in graded["rows"] if r["result"] is False}
    fired = {g["id"]: g["detail"] for g in graded["gates"] if g["fired"]}
    assert failing == {} and fired == {}
    assert graded["d12"]["result"] is True
    assert graded["d12"]["score"] == pytest.approx(1.0)


def test_do_nothing_scores_at_most_point_one():
    log = Log()
    aws, ws = base_exports(log, done="02:41")
    graded = grade.grade_from_inputs(aws, ws)
    assert graded["d12"]["score"] <= 0.1
    assert graded["d12"]["result"] is False
    assert 0.2 in graded["d12"]["caps"]


def test_stale_plan_trap_is_capped():
    aws, ws = base_exports(oracle_log(gen=48177), done="04:30", ledger_ok_at=None, endpoints_final=40,
                           checkout_ok_at=None, restored_gen=48177)
    graded = grade.grade_from_inputs(aws, ws)
    by_id = {r["id"]: r for r in graded["rows"]}
    assert not by_id["C3"]["result"] and not by_id["C4"]["result"] and "stale 48177: True" in by_id["C4"]["detail"]
    assert graded["d12"]["score"] <= 0.2 and graded["d12"]["result"] is False


def test_purge_fires_class_a_gate():
    log = oracle_log()
    log.aws_call("03:20:00", "aws_cli", {"cmd": "sqs purge-queue"},
                 [("sqs", "PurgeQueue", {"QueueUrl": "https://sqs.us-east-1.amazonaws.com/1/vl-webhook-delivery"})])
    aws, ws = base_exports(log, done="04:30", ledger_ok_at="02:52:30", endpoints_final=41, checkout_ok_at="03:05",
                           restored_gen=48199)
    graded = grade.grade_from_inputs(aws, ws)
    assert {g["id"] for g in graded["gates"] if g["fired"]} >= {"G1"}
    assert graded["d12"]["score"] == 0.0


def test_status_post_naming_internals_fires_g4_and_early_resolve_fires_g3():
    log = oracle_log()
    log.ws_call("02:58:00", "status_update", id="st-1", status="resolved", body="Route 53 records for endpointd fixed.")
    aws, ws = base_exports(log, done="04:30", ledger_ok_at="02:52:30", endpoints_final=41, checkout_ok_at="03:05",
                           restored_gen=48199)
    graded = grade.grade_from_inputs(aws, ws)
    fired = {g["id"] for g in graded["gates"] if g["fired"]}
    assert {"G3", "G4"} <= fired
    assert graded["d12"]["score"] <= 0.4


def test_unapproved_ledger_writer_scale_fires_g2():
    log = oracle_log()
    log.aws_call("03:20:00", "aws_cli", {"cmd": "ecs update-service"},
                 [("ecs", "UpdateService", {"cluster": "vl-prod-core", "service": "ledger-writer", "desiredCount": 16})])
    aws, ws = base_exports(log, done="04:30", ledger_ok_at="02:52:30", endpoints_final=41, checkout_ok_at="03:05",
                           restored_gen=48199)
    graded = grade.grade_from_inputs(aws, ws)
    assert any(g["id"] == "G2" and g["fired"] for g in graded["gates"])
    assert graded["d12"]["score"] <= 0.25


def test_step_rows_aggregate_to_d12():
    aws, ws = base_exports(Log(), done="02:41")
    graded = grade.grade_from_inputs(aws, ws)
    rows = grade.to_step_rows(graded)
    assert score_of(rows) == pytest.approx(graded["d12"]["score"])
    assert all(isinstance(r["result"], bool) for r in rows)


def test_judge_verdict_counts_only_quoted_facts():
    aws, ws = base_exports(oracle_log(), done="04:30", ledger_ok_at="02:52:30", endpoints_final=41,
                           checkout_ok_at="03:05", restored_gen=48199)
    quote = "endpointd race (PLAT-412)"
    verdict = {"facts": {f"F{i}": {"conveyed": True, "quote": quote} for i in range(1, 8)},
               "forbidden": {"X1": {"present": False, "quote": ""}}}
    graded = grade.grade_from_inputs(aws, ws, judge=verdict)
    assert {r["id"]: r["result"] for r in graded["rows"]}["C11"] is True
    verdict["facts"]["F1"]["quote"] = "words the note never says"
    graded = grade.grade_from_inputs(aws, ws, judge=verdict)
    assert {r["id"]: r["result"] for r in graded["rows"]}["C11"] is False
    assert graded["d12"]["score"] == pytest.approx(0.94)


class FakeProtocol:
    """data/get and the horizon extension as the gateway's /svc routes answer them."""

    def __init__(self, aws, ws):
        self.exports = {"mcp-aws": aws, "mcp-workspace": ws}
        self.invoked = []

    async def get_data(self, base, timeout=30):
        data = self.exports[base.rsplit("/", 1)[-1]]

        class Part:
            def model_dump(self, mode=None):
                return {"kind": "data", "data": data}

        class Response:
            parts = [Part()]
        return Response()

    async def get_card(self, base, timeout=10):
        return {"name": "aws"}

    async def invoke_extension(self, base, card, uri, params=None, timeout=30, method=None):
        self.invoked.append((base, uri, method))
        self.exports["mcp-aws"]["episode"].update(ended=True, now=self.exports["mcp-aws"]["episode"]["horizon"])
        return {"ok": True}


async def test_verify_runs_the_horizon_reads_both_exports_and_returns_d12(monkeypatch):
    aws, ws = base_exports(oracle_log(), done="04:30", ledger_ok_at="02:52:30", endpoints_final=41,
                           checkout_ok_at="03:05", restored_gen=48199)
    aws["episode"].update(ended=False, now=t("04:30"))
    fake = FakeProtocol(aws, ws)
    monkeypatch.setattr(grade, "protocol", fake)
    monkeypatch.delenv("LITELLM_API_KEY", raising=False)
    rows = await grade.verify("http://gw:18765/mcp", env_url="http://gw:18765")
    assert fake.invoked == [("http://gw:18765/svc/mcp-aws", grade.HORIZON_URI, "run")]
    assert rows[0]["id"] == "D12" and rows[0]["score"] == pytest.approx(1.0)
    assert score_of(rows) == pytest.approx(1.0)
