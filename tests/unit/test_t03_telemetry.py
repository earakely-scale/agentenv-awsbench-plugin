"""T03 telemetry served by the world (docs/T03-build.md §1.9): alarms that agree with #sre and the page, metrics and logs
from the physics in virtual time, the canary Lambda and the streamroute-leases table."""

import json
from datetime import timedelta

import pytest
from awsbench_aws.errors import AwsError
from awsbench_aws.world import load_scenario, make_world
from test_t03_world import AGENT, SRC, Ws, at, revert

S = load_scenario("t03")
PAGED = {"streamroute-mem-used-high": "07:06", "streamroute-route-errors-high": "07:51",
         "kinesis-vl-payment-events-iterator-age-high": "08:02", "vl-ledger-e2e-canary-failing": "08:09"}
PAYMENTS = ["payments-volume-drop", "auth-success-rate-low", "ledger-freshness-p99-high", "webhook-delivery-lag-high"]


class Aws:
    """The emulator's side: alarm definitions as seeded, and the world's writes."""

    def __init__(self):
        self.alarms = {r["params"]["AlarmName"]: dict(r["params"]) for r in S["aws_seed"]
                       if r.get("operation") == "PutMetricAlarm"}
        self.writes = []

    def call(self, service, operation, params, *, principal, world_made, log_call=None):
        if operation == "DescribeAlarms":
            return {"MetricAlarms": [dict(a) for a in self.alarms.values()]}
        if operation == "BatchWriteItem":
            self.writes.append((params, world_made, log_call))
        return {}


def world(seed="1101"):
    w = make_world({**S, "timeline": [], "npcs": {}}, seed, Aws(), Ws())
    w.start()
    return w


def cw(w, op, **p):
    return w.serve_aws("cloudwatch", op, p)


def stats(w, ns, metric, dims=None, minutes=30, stat="Average"):
    end = w.now
    out = cw(w, "GetMetricStatistics", Namespace=ns, MetricName=metric, Dimensions=dims or [], Period=60,
             StartTime=end - timedelta(minutes=minutes), EndTime=end, Statistics=[stat])
    return sorted((d["Timestamp"], d[stat]) for d in out["Datapoints"])


def alarms(w):
    return {a["AlarmName"]: a for a in cw(w, "DescribeAlarms")["MetricAlarms"]}


@pytest.fixture(scope="module")
def opening():
    return world()


@pytest.fixture(scope="module")
def oracle():
    w = world()
    w.advance_to(at(w, "08:30:00"))
    revert(w)
    w.mech.ssm[SRC] = "authoritative"
    w.advance_to(at(w, "08:32:00"))
    w.fleet_restart("streamroute", 12, AGENT)
    w.advance_to(at(w, "11:05:00"))
    return w


def test_alarms_at_the_start_are_the_ones_sre_and_the_page_show(opening):
    a, m = alarms(opening), opening.mech
    for name, hm in PAGED.items():
        assert a[name]["StateValue"] == "ALARM"
        assert a[name]["StateUpdatedTimestamp"].astimezone(m.tz).strftime("%H:%M") == hm, name
    assert {a[n]["StateValue"] for n in PAYMENTS} == {"INSUFFICIENT_DATA"}
    assert a["checkout-alb-5xx-high"]["StateValue"] == a["api-alb-5xx-high"]["StateValue"] == "OK"
    assert "[Breaching]" not in a["vl-ledger-e2e-canary-failing"]["StateReason"]
    first_false = next(t for t, ok in m.canary if not ok)
    assert m.iso(first_false + 180)[11:16] == PAGED["vl-ledger-e2e-canary-failing"]


def test_each_paged_time_is_when_its_own_evaluation_alarms(opening):
    tel = opening.mech.tel
    for name, hm in PAGED.items():
        d = tel.defs[name]
        first = next(ts for ts in tel.ts if tel._eval(d, ts)["state"] == "ALARM")
        paged = opening.mech.at(hm)
        assert first <= paged <= first + 300, (name, opening.mech.iso(first))


def test_every_time_is_virtual(opening):
    a = alarms(opening)["vl-ledger-e2e-canary-failing"]
    data = json.loads(a["StateReasonData"])
    assert data["queryDate"].startswith("2026-11-16T13:09") and data["recentDatapoints"] == [0.0, 0.0, 0.0]
    assert a["AlarmConfigurationUpdatedTimestamp"].date().isoformat() == "2026-09-01"
    hist = cw(opening, "DescribeAlarmHistory", AlarmName="vl-ledger-e2e-canary-failing")["AlarmHistoryItems"]
    assert [h["HistorySummary"] for h in hist][:2] == ["Alarm updated from OK to ALARM",
                                                       "Alarm updated from INSUFFICIENT_DATA to OK"]
    now = opening.now
    assert all(h["Timestamp"] <= now for h in hist)
    for ns, metric, dims in (("Vantleaf/Canary", "LedgerE2ESuccess", []),
                             ("AWS/ApplicationELB", "RequestCount",
                              [{"Name": "LoadBalancer", "Value": "app/vl-checkout-alb/5c8e2f1a9d3b7e40"}])):
        pts = stats(opening, ns, metric, dims)
        assert pts and all(t + timedelta(minutes=1) <= now for t, _ in pts)


def test_metrics_follow_the_physics(opening):
    m = opening.mech
    lag = stats(opening, "Vantleaf/Streamroute", "CellLagSeconds", [{"Name": "Cell", "Value": "lc-03"}], 5)
    want = dict(m.cell_lag)[m.at("08:14")]["lc-03"]
    assert abs(lag[-1][1] - want) <= 1
    assert stats(opening, "Vantleaf/Canary", "LedgerE2ESuccess", minutes=15, stat="Minimum")[-1][1] == 0.0
    assert stats(opening, "Vantleaf/Payments", "PaymentVolumePerMin", minutes=120)[-1][0].astimezone(m.tz).strftime(
        "%H:%M") < "07:40"
    five = stats(opening, "AWS/ApplicationELB", "HTTPCode_Target_5XX_Count",
                 [{"Name": "LoadBalancer", "Value": "app/vl-checkout-alb/5c8e2f1a9d3b7e40"}], stat="Sum")
    assert five and max(v for _, v in five) < 50
    assert stats(opening, "AWS/ApplicationELB", "RequestCount",
                 [{"Name": "LoadBalancer", "Value": "app/vl-nope/1"}]) == []
    names = {x["MetricName"] for x in cw(opening, "ListMetrics", Namespace="Vantleaf/Streamroute")["Metrics"]}
    assert {"RecordsRouted", "RouteErrors", "CellLagSeconds", "EjectedHosts", "PeerCountKnown"} <= names
    assert not any("Thread" in x["MetricName"] for x in cw(opening, "ListMetrics")["Metrics"])


def test_the_canary_alarm_clears_after_the_fix(oracle):
    m = oracle.mech
    back = next(t for t, ok in m.canary if t > m.t_start and ok)
    a = alarms(oracle)["vl-ledger-e2e-canary-failing"]
    assert a["StateValue"] == "OK" and 0 < a["StateUpdatedTimestamp"].timestamp() - back <= 240
    assert {v for _, v in stats(oracle, "Vantleaf/Canary", "LedgerE2ESuccess", minutes=20)} == {1.0}
    ex = oracle.export()["t03"]["telemetry"]["alarm_state_timeline"]
    assert ex[0][1]["vl-ledger-e2e-canary-failing"] == "ALARM" and ex[-1][1]["vl-ledger-e2e-canary-failing"] == "OK"


def test_cells_differ_and_never_read_exactly_zero(opening, oracle):
    rows = [lags for t, lags in opening.mech.cell_lag if t >= opening.mech.at("07:50")]
    cells = sorted(rows[0])
    same = max(sum(r[a] == r[b] for r in rows) for i, a in enumerate(cells) for b in cells[i + 1:])
    assert same < len(rows) / 2
    peak = dict(oracle.mech.cell_lag)[oracle.mech.at("11:00")]
    assert all(0 < v < 5 for v in peak.values())
    assert 3.4e6 < opening.mech.flow.backlog() < 3.8e6


def test_treat_missing_data_edits_take_effect():
    w = world()
    w.mech.w.aws.alarms["payments-volume-drop"]["TreatMissingData"] = "breaching"
    w.observe({"t": w.now, "args": {"service": "cloudwatch", "operation": "PutMetricAlarm", "params": {}}})
    w.advance_to(w.now + timedelta(minutes=1))
    a = alarms(w)["payments-volume-drop"]
    assert a["StateValue"] == "ALARM" and "treated as [Breaching]" in a["StateReason"]


def test_logs_come_from_the_fleet(opening):
    def events(group, **p):
        return opening.serve_aws("logs", "FilterLogEvents", {"logGroupName": group, **p})["events"]
    start = int(opening.mech.at("07:20") * 1000)
    fails = events("/vl/prod/streamroute", startTime=start, filterPattern='"unable to create native thread"')
    assert fails[0]["logStreamName"] == "sr-b-017" and opening.mech.iso(fails[0]["timestamp"] / 1000)[11:19] == \
        "07:22:41"
    assert events("/vl/prod/streamroute", filterPattern="route.no_shardmap")
    lw = [e["message"] for e in events("/vl/prod/ledger-writer", startTime=int(opening.mech.at("07:30") * 1000))]
    assert any("3.41.0 started" in x for x in lw) and all("errors 0" in x or "idle" in x or "started" in x for x in lw)
    assert "not visible in ledger after 120s" in events("/aws/lambda/ledger-e2e-canary")[-1]["message"]
    q = opening.serve_aws("logs", "StartQuery", {"logGroupNames": ["/vl/prod/streamroute"], "queryString":
                                                 "filter @message like /route.no_shardmap/ | stats count() by bin(5m)",
                                                 "startTime": int(opening.mech.at("07:15")),
                                                 "endTime": int(opening.mech.at("08:15"))})
    res = opening.serve_aws("logs", "GetQueryResults", q)
    assert res["results"] and 0 < res["statistics"]["recordsMatched"] < res["statistics"]["recordsScanned"]
    with pytest.raises(AwsError, match="does not exist"):
        events("/vl/prod/nope")
    fn = opening.serve_aws("lambda", "GetFunctionConfiguration", {"FunctionName": "ledger-e2e-canary"})
    assert fn["LoggingConfig"]["LogGroup"] == "/aws/lambda/ledger-e2e-canary"


def test_the_lease_table_follows_the_world(oracle):
    w, flow = oracle, oracle.mech.flow
    aws = w.mech.w.aws
    aws.writes.clear()
    assert w.serve_aws("dynamodb", "Scan", {"TableName": "streamroute-leases"}) is None
    items = [r["PutRequest"]["Item"] for p, made, logged in aws.writes for r in p["RequestItems"]["streamroute-leases"]]
    assert len(items) == 240 and all(made and logged is False for _, made, logged in aws.writes)
    assert [i["leaseOwner"]["S"] for i in items] == flow.owner
    hosts = w.mech.fleet.hosts
    assert not any(hosts[i["leaseOwner"]["S"]].terminated for i in items)
    before = int(items[0]["leaseCounter"]["N"])
    w.advance_to(w.now + timedelta(minutes=5))
    aws.writes.clear()
    w.serve_aws("dynamodb", "Scan", {"TableName": "streamroute-leases"})
    assert int(aws.writes[0][0]["RequestItems"]["streamroute-leases"][0]["PutRequest"]["Item"]["leaseCounter"]["N"]) \
        == before + 30


def test_times_render_in_new_york(opening):
    hosts = {h["name"]: h for h in opening.fleet_list_hosts("streamroute")}
    assert hosts["sr-a-001"]["launched"] == "2026-10-26T09:12:40-04:00"
    assert opening.mech.iso(opening.mech.t_start) == "2026-11-16T08:15:00-05:00"
