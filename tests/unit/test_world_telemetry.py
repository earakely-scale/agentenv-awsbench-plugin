"""Telemetry reads against the T01 world: outbox, logs, Logs Insights, metric statistics, alarms and SQS."""

import re
from datetime import UTC, timedelta
from pathlib import Path

import pytest
from awsbench_aws.errors import AwsError
from awsbench_aws.world import World, replay_seed
from test_world_t01 import AGENT, LEDGER, SCENARIO, FakeAws, FakeWs, at, ep, set_concurrency

CK = "/vl/prod/checkout-api"


@pytest.fixture
def world():
    aws, ws = FakeAws(), FakeWs()
    replay_seed(SCENARIO, aws)
    w = World(SCENARIO, "eval-seed-1", aws, ws)
    w.start()
    return w


def set_param(w, name, value, when):
    w.advance_to(at(when))
    w.aws.call("ssm", "PutParameter", {"Name": name, "Value": value, "Type": "String", "Overwrite": True},
               principal="sre-primary", world_made=False)
    w.observe({"t": at(when), "tool": "aws_cli", "role": "sre-primary", "server": "aws", "service": "ssm",
               "operation": "PutParameter", "args": {"cmd": "ssm put-parameter"}, "params": {"Name": name}})


def oracle(w):
    ep(w, "epctl pause all", "02:49:00")
    set_concurrency(w, "4", "02:51:45")
    ep(w, f"epctl apply --gen 48199 --endpoint {LEDGER} --manual --reason OPS-4452", "02:52:30")


def collapse(w):
    ep(w, "epctl pause all", "02:49:00")
    ep(w, f"epctl apply --gen 48199 --endpoint {LEDGER} --manual --reason OPS-4452", "02:52:30")
    w.advance_to(at("03:40:00"))


def stats(w, ns, metric, start, end, period, statistics, dims=()):
    return w.serve_aws("cloudwatch", "GetMetricStatistics", {
        "Namespace": ns, "MetricName": metric, "StartTime": at(start), "EndTime": at(end), "Period": period,
        "Statistics": statistics, "Dimensions": list(dims)})["Datapoints"]


def query(w, qs, groups, start, end, limit=100):
    qid = w.serve_aws("logs", "StartQuery", {"logGroupNames": groups, "queryString": qs, "limit": limit,
                                             "startTime": int(at(start).timestamp()),
                                             "endTime": int(at(end).timestamp())})["queryId"]
    return w.serve_aws("logs", "GetQueryResults", {"queryId": qid})


def messages(w, group, start, end):
    return [e["message"] for e in w.serve_aws("logs", "FilterLogEvents", {
        "logGroupName": group, "startTime": int(at(start).timestamp() * 1000),
        "endTime": int(at(end).timestamp() * 1000)})["events"]]


def minutes(w, key, start, end):
    return [m[key] for m in w.telemetry.minutes if at(start) <= m["t"] < at(end)]


# ---- outbox ----
def test_outbox_metric_and_table_show_the_physics(world):
    world.advance_to(at("02:50:00"))
    pts = stats(world, "Vantleaf/Checkout", "OutboxPending", "02:45:00", "02:50:00", 60, ["Average"])
    assert pts and min(p["Average"] for p in pts) > 7500
    names = world.serve_aws("cloudwatch", "ListMetrics", {"Namespace": "Vantleaf/Checkout"})["Metrics"]
    assert "OutboxPending" in {m["MetricName"] for m in names}
    n = round(world.storm.outbox)
    assert world.serve_aws("dynamodb", "DescribeTable", {"TableName": "vl-ledger-outbox"})["Table"]["ItemCount"] == n
    assert world.serve_aws("dynamodb", "Scan", {"TableName": "vl-ledger-outbox", "Select": "COUNT"})["Count"] == n
    seen, params = [], {"TableName": "vl-ledger-outbox"}
    while True:
        page = world.serve_aws("dynamodb", "Scan", params)
        seen += [i["payment_id"]["S"] for i in page["Items"]]
        if "LastEvaluatedKey" not in page:
            break
        params["ExclusiveStartKey"] = page["LastEvaluatedKey"]
    assert len(seen) == len(set(seen)) == n
    assert world.serve_aws("dynamodb", "DescribeTable", {"TableName": "endpointd-plans"}) is None


def test_outbox_drains_to_zero_on_the_oracle_path(world):
    oracle(world)
    world.advance_to(at("05:10:00"))
    pts = stats(world, "Vantleaf/Checkout", "OutboxPending", "02:50:00", "05:10:00", 600, ["Maximum"])
    vals = [p["Maximum"] for p in sorted(pts, key=lambda p: p["Timestamp"])]
    assert vals[0] > 7500 and vals[-1] == 0
    assert world.serve_aws("dynamodb", "Scan", {"TableName": "vl-ledger-outbox", "Select": "COUNT"})["Count"] == 0


# ---- logs ----
def test_log_history_does_not_change_after_a_restore(world):
    world.advance_to(at("02:46:00"))
    before = messages(world, "/vl/platform/endpointd/enactor-b", "02:35:00", "02:45:00")
    assert before and all("halt latch engaged" in m for m in before)
    oracle(world)
    world.advance_to(at("03:00:00"))
    assert messages(world, "/vl/platform/endpointd/enactor-b", "02:35:00", "02:45:00") == before


def test_no_dispatcher_lines_while_the_consumer_is_paused(world):
    set_param(world, "/vl/prod/webhooks-dispatcher/consumer_paused", "true", "02:47:30")
    world.advance_to(at("02:55:00"))
    assert messages(world, "/vl/prod/webhooks-dispatcher", "02:42:00", "02:47:00")
    assert messages(world, "/vl/prod/webhooks-dispatcher", "02:49:00", "02:55:00") == []


def test_race_and_planner_lines_reach_cloudwatch(world):
    world.advance_to(at("02:45:00"))
    cleanup = world.serve_aws("logs", "FilterLogEvents", {
        "logGroupName": "/vl/platform/endpointd/enactor-c", "startTime": int(at("02:00:00").timestamp() * 1000),
        "filterPattern": "cleanup"})["events"]
    assert any("[48177, 48178]" in e["message"] and e["timestamp"] == int(at("02:31:41").timestamp() * 1000)
               for e in cleanup)
    planner = messages(world, "/vl/platform/endpointd/planner", "02:00:00", "02:45:00")
    assert any("gen=48177" in m for m in planner) and any("gen=48209" in m for m in planner)


def test_ledger_writer_log_rate_matches_requests(world):
    oracle(world)
    world.advance_to(at("04:00:00"))
    lw = world.storm.lw_running
    for when in ("02:45:42", "03:30:42"):
        line = world.telemetry.lines("/vl/prod/ledger-writer", at(when))[0]
        per_task = float(line.split("inbound ", 1)[1].split(" ", 1)[0])
        req = world.telemetry.value("Vantleaf/LedgerWriter", "Requests", {}, at(when))
        assert abs(per_task * lw - req / 60) <= 0.05 * req / 60, (when, line, req)
    set_param(world, "/vl/prod/checkout-api/outbox/replay_rate", "0", "04:00:00")
    world.advance_to(at("04:10:00"))
    pre = world.telemetry.value("Vantleaf/LedgerWriter", "Requests", {}, at("02:20:00"))
    assert pre == world.telemetry.value("Vantleaf/LedgerWriter", "Requests", {}, at("04:09:00"))
    conns = stats(world, "AWS/RDS", "DatabaseConnections", "02:00:00", "04:10:00", 60, ["Maximum"])
    assert max(p["Maximum"] for p in conns) <= lw * 25


# ---- Logs Insights ----
def test_insights_regex_sort_limit_and_stats(world):
    world.advance_to(at("03:20:00"))
    r = query(world, "fields @message | filter @message like /UnknownHost|Circuit/", [CK], "03:00:00", "03:15:00")
    msgs = [x[0]["value"] for x in r["results"]]
    assert any("UnknownHost" in m for m in msgs) and any("CircuitBreaker" in m for m in msgs)
    two = [CK, "/vl/prod/webhooks-dispatcher"]
    r = query(world, "fields @timestamp, @log | sort @timestamp desc | limit 4", two, "02:40:00", "03:15:00", 50)
    newest = sorted((e["timestamp"] for g in two for e in world.telemetry.log_events(
        g, at("02:40:00"), at("03:15:00"), None, world.now, 10000)), reverse=True)[:4]
    assert [x[0]["value"] for x in r["results"]] == [
        at("00:00:00").fromtimestamp(t / 1000, UTC).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3] for t in newest]
    r = query(world, "filter @message like /ERROR|Exception/ | stats count() by bin(5m)", [CK], "03:00:00", "03:15:00")
    assert len(r["results"]) == 3
    assert sum(int(row[1]["value"]) for row in r["results"]) == r["statistics"]["recordsMatched"]
    assert r["results"][0][0]["field"] == "bin(5m)" and r["results"][0][1]["field"] == "count()"
    with pytest.raises(AwsError) as e:
        query(world, "parse @message 'x *' as y", [CK], "03:00:00", "03:15:00")
    assert e.value.code == "MalformedQueryException"


def test_unknown_log_groups_are_not_found(world):
    world.advance_to(at("02:50:00"))
    for op, p in (("StartQuery", {"logGroupNames": ["/vl/checkout-api"], "queryString": "fields @message",
                                  "startTime": 0, "endTime": 1}),
                  ("FilterLogEvents", {"logGroupName": "/vl/checkout-api"})):
        with pytest.raises(AwsError) as e:
            world.serve_aws("logs", op, p)
        assert e.value.code == "ResourceNotFoundException"
    assert any("UnknownHostException" in m for m in messages(world, CK, "02:40:00", "02:50:00"))
    out = world.fleet_exec("core-a-01", "journalctl -u checkout-api -n 20", AGENT)["hosts"][0]["stdout"]
    assert "UnknownHostException" in out


# ---- metric statistics ----
def test_statistics_aggregate_each_period(world):
    collapse(world)
    first = next(m["t"] for m in world.telemetry.minutes if m["p99"] == 2000.0)
    t0 = first.replace(minute=first.minute // 5 * 5)
    assert t0 < first
    pts = stats(world, "Vantleaf/LedgerWriter", "P99LatencyMs", t0.strftime("%H:%M:%S"),
                (t0 + timedelta(minutes=5)).strftime("%H:%M:%S"), 300, ["Average", "Maximum"])
    assert len(pts) == 1 and pts[0]["Maximum"] == 2000.0 and 120.0 < pts[0]["Average"] < 2000.0
    pts = stats(world, "Vantleaf/Checkout", "CheckoutSuccessRate", "02:40:00", "03:40:00", 300, ["Minimum"])
    for p in pts:
        start = p["Timestamp"].astimezone(at("00:00:00").tzinfo).strftime("%H:%M:%S")
        end = (p["Timestamp"] + timedelta(minutes=5)).astimezone(at("00:00:00").tzinfo).strftime("%H:%M:%S")
        assert p["Minimum"] == min(round(v, 4) for v in minutes(world, "checkout", start, end))
    pts = stats(world, "Vantleaf/Checkout", "CheckoutSuccessRate", "03:00:00", "03:02:00", 120, ["Average"])
    assert pts[0]["Average"] == round(sum(round(v, 4) for v in minutes(world, "checkout", "03:00:00", "03:02:00"))
                                      / 2, 4)


# ---- alarms ----
def test_alarms_keep_their_real_times_names_and_periods(world):
    world.advance_to(at("02:41:00"))
    alarms = {a["AlarmName"]: a for a in world.serve_aws("cloudwatch", "DescribeAlarms", {})["MetricAlarms"]}
    assert alarms["ledger-ingress-EnvoyNotReady"]["StateUpdatedTimestamp"] == at("02:13:00")
    assert alarms["endpointd-ApplyLagSeconds-high"]["StateUpdatedTimestamp"] == at("02:37:00")
    assert alarms["checkout-api-CheckoutSuccessRate-critical"]["StateUpdatedTimestamp"] == at("02:32:00")
    one = world.serve_aws("cloudwatch", "DescribeAlarms", {
        "AlarmNames": ["checkout-api-CheckoutSuccessRate-critical"]})["MetricAlarms"]
    assert len(one) == 1 and one[0]["StateValue"] == "ALARM" and one[0]["Threshold"] == 0.95
    assert one[0]["EvaluationPeriods"] == 1 and "was less than the threshold (0.95)" in one[0]["StateReason"]
    assert alarms["ledger-writer-p99-high"]["EvaluationPeriods"] == 3


# ---- SQS ----
def test_queue_attributes_follow_the_consumer_and_the_request(world):
    url = "https://sqs.us-east-1.amazonaws.com/000000000000/vl-webhook-delivery"
    set_param(world, "/vl/prod/webhooks-dispatcher/consumer_paused", "true", "02:48:00")
    world.advance_to(at("02:50:00"))
    attrs = world.serve_aws("sqs", "GetQueueAttributes", {"QueueUrl": url, "AttributeNames": ["All"]})["Attributes"]
    assert attrs["ApproximateNumberOfMessagesNotVisible"] == "0"
    one = world.serve_aws("sqs", "GetQueueAttributes", {"QueueUrl": url,
                                                         "AttributeNames": ["ApproximateNumberOfMessages"]})
    assert list(one["Attributes"]) == ["ApproximateNumberOfMessages"]


# ---- the minutes before the episode ----
@pytest.mark.parametrize("ns,metric", [("Vantleaf/Checkout", "CheckoutSuccessRate"),
                                       ("Vantleaf/Checkout", "OutboxPending"),
                                       ("AWS/SQS", "ApproximateNumberOfMessagesVisible"),
                                       ("Vantleaf/Endpointd", "EndpointRecordCount")])
def test_metrics_have_every_minute_from_before_the_outage(world, ns, metric):
    world.advance_to(at("02:45:00"))
    pts = sorted(stats(world, ns, metric, "02:20:00", "02:45:00", 60, ["Average"]), key=lambda p: p["Timestamp"])
    assert [p["Timestamp"] for p in pts] == [at("02:20:00") + timedelta(minutes=i) for i in range(25)]


def test_backfilled_minutes_follow_the_outage_physics(world):
    world.advance_to(at("02:45:00"))
    outbox = [p["Average"] for p in sorted(stats(world, "Vantleaf/Checkout", "OutboxPending", "02:31:00", "02:42:00",
                                                 60, ["Average"]), key=lambda p: p["Timestamp"])]
    assert outbox[0] == 0 and outbox == sorted(outbox) and 6000 < outbox[-3] < outbox[-2]
    rate = stats(world, "Vantleaf/Checkout", "CheckoutSuccessRate", "02:33:00", "02:40:00", 60, ["Maximum"])
    assert len(rate) == 7 and all(p["Maximum"] < 0.95 for p in rate)


# ---- Logs Insights ----
def test_insights_answers_as_of_start_query(world):
    world.advance_to(at("02:44:00"))
    qid = world.serve_aws("logs", "StartQuery", {"logGroupNames": [CK], "queryString": "fields @timestamp, @message",
                                                 "startTime": int(at("02:30:00").timestamp()),
                                                 "endTime": int(at("02:50:00").timestamp())})["queryId"]
    world.advance_to(at("02:50:00"))
    rows = world.serve_aws("logs", "GetQueryResults", {"queryId": qid})["results"]
    newest = max(r[0]["value"] for r in rows)
    assert rows and newest <= at("02:44:00").astimezone(UTC).strftime("%Y-%m-%d %H:%M:%S.999")


# ---- ledger-writer pool ----
def test_collapsed_pool_lines_always_have_waiters(world):
    ep(world, "epctl pause all", "02:49:00")
    ep(world, f"epctl apply --gen 48199 --endpoint {LEDGER} --manual --reason x", "02:58:30")
    set_param(world, "/vl/prod/webhooks-dispatcher/consumer_paused", "true", "03:06:00")
    world.advance_to(at("03:20:00"))
    tail = [m for m in world.telemetry.minutes if m["regime"] == "collapsed" and m["offered"] < m["capacity"]]
    assert tail
    for m in world.telemetry.minutes:
        if m["regime"] == "collapsed":
            line = world.telemetry.lines("/vl/prod/ledger-writer", m["t"] + timedelta(seconds=42))[0]
            assert int(re.search(r"waiting=(\d+)", line).group(1)) > 0, line


# ---- seeded log lines ----
def test_seeded_logs_name_real_versions_and_hosts(world):
    msgs = " ".join(e["message"] for e in SCENARIO["world"]["telemetry"]["logs"])
    repo = (Path(__file__).resolve().parents[2] / "src/agentenv_awsbench/bundles/awsbench/envs/workspace/"
            "awsbench_workspace/scenarios/t01/repo/checkout-api.yaml").read_text()
    tags = re.findall(r"v\d{4}\.\d{2}\.\d{2}-\d+", msgs)
    assert tags and all(t in repo for t in tags)
    names = re.findall(r"\b(?:li|lw|core)-[abc]-\d{2}\b", msgs)
    assert names and all(n in world.fleet.hosts for n in names)
