"""Telemetry reads against the T01 world: outbox, logs, Logs Insights, metric statistics, alarms and SQS."""

import json
import re
from datetime import UTC, datetime, timedelta
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
        start = (p["Timestamp"] + timedelta(minutes=1)).astimezone(at("00:00:00").tzinfo).strftime("%H:%M:%S")
        end = (p["Timestamp"] + timedelta(minutes=6)).astimezone(at("00:00:00").tzinfo).strftime("%H:%M:%S")
        assert p["Minimum"] == min(round(v, 4) for v in minutes(world, "checkout", start, end))
    pts = stats(world, "Vantleaf/Checkout", "CheckoutSuccessRate", "03:00:00", "03:02:00", 120, ["Average"])
    assert pts[0]["Average"] == round(sum(round(v, 4) for v in minutes(world, "checkout", "03:01:00", "03:03:00"))
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



def test_queue_attributes_show_the_queue_configuration_and_its_redrive_policy(world):
    """R11-opus-3 CALLs 79 and 83 and R11-opus-5 CALL 80: 'All' showed 5 attributes with VisibilityTimeout '0' and
    RedrivePolicy came back {}, so the DLQ's 40 seeded messages read as tonight's failures."""
    url = "https://sqs.us-east-1.amazonaws.com/000000000000/vl-webhook-delivery"
    world.advance_to(at("03:35:35"))
    attrs = world.serve_aws("sqs", "GetQueueAttributes", {"QueueUrl": url, "AttributeNames": ["All"]})["Attributes"]
    assert attrs["VisibilityTimeout"] == "30" and attrs["QueueArn"].endswith(":000000000000:vl-webhook-delivery")
    assert {"CreatedTimestamp", "LastModifiedTimestamp", "DelaySeconds", "MaximumMessageSize"} <= set(attrs)
    redrive = json.loads(attrs["RedrivePolicy"])
    assert redrive == {"deadLetterTargetArn": "arn:aws:sqs:us-east-1:000000000000:vl-webhook-delivery-dlq",
                       "maxReceiveCount": 1000}
    one = world.serve_aws("sqs", "GetQueueAttributes", {"QueueUrl": url, "AttributeNames": ["RedrivePolicy"]})
    assert list(one["Attributes"]) == ["RedrivePolicy"]
    dlq = world.serve_aws("sqs", "GetQueueAttributes", {"QueueUrl": url + "-dlq", "AttributeNames": ["All"]})
    dlq = dlq["Attributes"]
    assert "RedrivePolicy" not in dlq and dlq["QueueArn"].endswith(":vl-webhook-delivery-dlq")
    assert dlq["CreatedTimestamp"] == dlq["LastModifiedTimestamp"]


def test_outbox_table_description_has_the_fields_dynamodb_returns(world):
    """R11-opus-6 CALL 80: DescribeTable vl-ledger-outbox had no CreationDateTime, ProvisionedThroughput or TableId."""
    world.advance_to(at("03:30:00"))
    table = world.serve_aws("dynamodb", "DescribeTable", {"TableName": "vl-ledger-outbox"})["Table"]
    assert table["CreationDateTime"] == datetime.fromisoformat(SCENARIO["seeded_at"])
    assert table["BillingModeSummary"]["LastUpdateToPayPerRequestDateTime"] == table["CreationDateTime"]
    assert table["ProvisionedThroughput"] == {"NumberOfDecreasesToday": 0, "ReadCapacityUnits": 0,
                                              "WriteCapacityUnits": 0}
    assert table["ItemCount"] == round(world.storm.outbox)
    again = world.serve_aws("dynamodb", "DescribeTable", {"TableName": "vl-ledger-outbox"})["Table"]
    assert table["TableId"] == again["TableId"] and len(table["TableId"]) == 36

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
    outbox = [p["Average"] for p in sorted(stats(world, "Vantleaf/Checkout", "OutboxPending", "02:30:00", "02:42:00",
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


# ---- admission ----
def test_rejected_429_is_a_metric_and_a_log_line_only_under_admission(world):
    ep(world, "epctl pause all", "02:49:00")
    set_param(world, "/vl/prod/ledger-writer/admission/enabled", "true", "02:50:00")
    set_param(world, "/vl/prod/ledger-writer/admission/quota/webhooks-dispatcher", "60", "02:50:30")
    ep(world, f"epctl apply --gen 48199 --endpoint {LEDGER} --manual --reason OPS-4452", "02:52:30")
    world.advance_to(at("03:10:00"))
    pts = stats(world, "Vantleaf/LedgerWriter", "Rejected429", "03:05:00", "03:08:00", 60, ["Sum"])
    assert len(pts) == 3 and all(p["Sum"] > 0 for p in pts)
    assert stats(world, "Vantleaf/LedgerWriter", "Rejected429", "02:00:00", "02:50:00", 60, ["Sum"])[0]["Sum"] == 0
    assert any("429 Retry-After: 1" in m for m in messages(world, "/vl/prod/ledger-writer", "03:05:00", "03:06:00"))
    assert any("429" in m for m in messages(world, "/vl/prod/webhooks-dispatcher", "03:05:00", "03:06:00"))
    listed = world.serve_aws("cloudwatch", "ListMetrics", {"Namespace": "Vantleaf/LedgerWriter"})["Metrics"]
    assert "Rejected429" in {m["MetricName"] for m in listed}


def test_no_rejections_without_admission(world):
    oracle(world)
    world.advance_to(at("03:30:00"))
    assert all(m["rejected"] == 0 for m in world.telemetry.minutes)
    assert not any("429" in m for m in messages(world, "/vl/prod/ledger-writer", "02:40:00", "03:30:00"))


# ---- outbox table ----
def outbox(w, op, **p):
    return w.serve_aws("dynamodb", op, {"TableName": "vl-ledger-outbox", **p})


def test_outbox_reads_apply_their_filter_and_key_condition(world):
    world.advance_to(at("02:50:00"))
    n = round(world.storm.outbox)
    by = {"ExpressionAttributeNames": {"#s": "status"}}
    for status, count in (("PENDING", 0), ("PENDING_POSTING", n), ("POSTED", 0)):
        out = outbox(world, "Scan", Select="COUNT", FilterExpression="#s = :p",
                     ExpressionAttributeValues={":p": {"S": status}}, **by)
        assert (out["Count"], out["ScannedCount"]) == (count, n)
    page = outbox(world, "Scan", Limit=3, FilterExpression="#s = :p", ExpressionAttributeValues={
        ":p": {"S": "POSTED"}}, **by)
    assert (page["Items"], page["Count"], page["ScannedCount"]) == ([], 0, 3) and page["LastEvaluatedKey"]
    item = world.telemetry._outbox_item(5)
    pid = {":id": item["payment_id"]}
    got = outbox(world, "Query", KeyConditionExpression="payment_id = :id", ExpressionAttributeValues=pid)
    assert (got["Items"], got["Count"]) == ([item], 1)
    assert outbox(world, "GetItem", Key={"payment_id": item["payment_id"]})["Item"] == item
    missing = {":id": {"S": "pi_ffffff000000000000000000"}}
    assert outbox(world, "Query", Select="COUNT", KeyConditionExpression="payment_id = :id",
                  ExpressionAttributeValues=missing)["Count"] == 0
    early = outbox(world, "Scan", Limit=50, FilterExpression="begins_with(payment_id, :p) AND NOT (#s <> :s)",
                   ExpressionAttributeValues={":p": {"S": "pi_00000"}, ":s": {"S": "PENDING_POSTING"}}, **by)
    assert early["Count"] == 16 and early["ScannedCount"] == 50


@pytest.mark.parametrize("p, says", [
    ({"FilterExpression": "status = :p", "ExpressionAttributeValues": {":p": {"S": "x"}}}, "reserved keyword: status"),
    ({"FilterExpression": "#s = :p", "ExpressionAttributeValues": {":p": {"S": "x"}}}, "attribute name: #s"),
    ({"FilterExpression": "authorized_at BETWEEN :a AND :b", "ExpressionAttributeValues": {
        ":a": {"S": "a"}, ":b": {"S": "b"}}}, "unsupported in this environment: BETWEEN"),
    ({"FilterExpression": "size(payment_id) > :a", "ExpressionAttributeValues": {":a": {"N": "3"}}},
     "unsupported in this environment: size"),
    ({"ScanFilter": {"status": {"ComparisonOperator": "EQ"}}}, "ScanFilter is unsupported"),
])
def test_outbox_filters_outside_the_subset_are_refused(world, p, says):
    world.advance_to(at("02:50:00"))
    with pytest.raises(AwsError, match=says) as e:
        outbox(world, "Scan", Select="COUNT", **p)
    assert e.value.code == "ValidationException"
    with pytest.raises(AwsError, match="missed key schema element: payment_id"):
        outbox(world, "Query", KeyConditionExpression="authorized_at = :a", ExpressionAttributeValues={
            ":a": {"S": "x"}})


# ---- Insights statistics ----
def test_insights_counts_every_record_it_scans(world):
    oracle(world)
    world.advance_to(at("03:58:00"))
    lw = "/vl/prod/ledger-writer"
    events = world.telemetry.log_events(lw, at("03:25:00"), at("03:58:00"), None, world.now, 10000)
    r = query(world, "filter @message like /NO_SUCH_TEXT/", [lw], "03:25:00", "03:58:00")
    assert r["statistics"] == {"recordsMatched": 0.0, "recordsScanned": float(len(events)),
                               "bytesScanned": float(sum(len(e["message"].encode()) for e in events))}
    assert len(events) > 0
    r = query(world, "fields @message", [lw], "03:25:00", "03:58:00")
    assert r["statistics"]["recordsScanned"] == r["statistics"]["recordsMatched"] == len(events)


# ---- the breaker decoy ----
def test_breaker_errors_outnumber_the_cause_but_the_first_error_is_the_cause(world):
    world.advance_to(at("03:00:00"))
    events = world.telemetry.log_events(CK, at("02:31:00"), at("03:00:00"), None, world.now, 10000)
    breaker = sum("CircuitBreakerOpenException" in e["message"] for e in events)
    cause = [e for e in events if "UnknownHostException" in e["message"]]
    assert 10 <= breaker / len(cause) <= 12
    assert cause[0]["timestamp"] == int(at("02:31:42").timestamp() * 1000) == events[0]["timestamp"]
    newest = query(world, "filter @message like /Exception/ | sort @timestamp desc | limit 24", [CK], "02:40:00",
                   "03:00:00")["results"]
    assert sum("UnknownHostException" in r[1]["value"] for r in newest) == 2
    assert not any("hosts)" in e["message"] for e in events)


def test_a_rollback_to_411_has_no_breaker_lines(world):
    world.advance_to(at("02:45:00"))
    world.serve_aws("ecs", "UpdateService", {"cluster": "vl-prod-core", "service": "checkout-api",
                                             "taskDefinition": "checkout-api:411"})
    world.ecs.apply({"service": "ecs", "operation": "UpdateService", "params": {
        "cluster": "vl-prod-core", "service": "checkout-api", "taskDefinition": "checkout-api:411"}}, at("02:45:00"))
    world.advance_to(at("02:55:00"))
    late = messages(world, CK, "02:50:00", "02:55:00")
    assert late and not any("CircuitBreaker" in m for m in late) and all("UnknownHost" in m for m in late
                                                                          if m.startswith("ERROR"))


def test_an_overloaded_pool_logs_its_queue(world):
    """R10-opus-2 CALL 47: '08:03:42 INFO inbound 144.5 req/s per task ...; pool active=25 idle=0 waiting=0' in the
    minute the ledger was degraded, offered 1734 against capacity 1500."""
    ep(world, "epctl pause all", "02:51:00")
    set_param(world, "/vl/prod/checkout-api/outbox/replay_rate", "50", "02:58:15")
    ep(world, f"epctl apply --gen 48219 --endpoint {LEDGER} --manual --reason OPS-4452", "03:02:05")
    world.advance_to(at("03:12:00"))
    lines = [x for x in messages(world, "/vl/prod/ledger-writer", "02:40:00", "03:12:00") if "inbound" in x]
    over = [m for m in world.telemetry.minutes if m["regime"] != "collapsed" and m["offered"] > m["capacity"]]
    assert [m["t"].strftime("%H:%M") for m in over] == ["03:03"]
    waits = [int(re.search(r"waiting=(\d+)", x)[1]) for x in lines]
    hot = [x for x in lines if "waiting=0" not in x]
    assert len(hot) == 1 and hot[0].startswith("WARN inbound 144.5 req/s per task") and max(waits) > 0
    assert waits.count(0) == len(lines) - 1


def test_insights_runs_the_design_oracles_query_and_names_what_it_supports(world):
    """parse with named groups, stats count/min/max by a parsed field, and filters on @timestamp and level."""
    world.advance_to(at("03:20:00"))
    qs = ("filter @timestamp >= '2026-11-17T07:20:00Z' and level in ['ERROR','WARN'] "
          "| parse @message /(?<cls>[A-Za-z]+Exception)/ | stats count(*), min(@timestamp)\n  by cls")
    r = query(world, qs, [CK], "02:40:00", "03:15:00")
    rows = {x[0]["value"]: {f["field"]: f["value"] for f in x} for x in r["results"]}
    assert "CircuitBreakerOpenException" in rows and rows["CircuitBreakerOpenException"]["count(*)"].isdigit()
    assert all(v["min(@timestamp)"] >= "2026-11-17 07:20:00" for v in rows.values())
    r = query(world, "fields @timestamp, @message | filter @message like /ERROR/ and @timestamp < 1795421400000 "
                     "| sort @timestamp asc | limit 3", [CK], "02:40:00", "03:15:00")
    assert len(r["results"]) == 3 and r["results"][0][0]["value"] < r["results"][2][0]["value"]
    with pytest.raises(AwsError) as e:
        query(world, "fields @message | dedup @message", [CK], "03:00:00", "03:15:00")
    assert e.value.code == "MalformedQueryException" and "parse @message /(?<name>re)/" in e.value.message


def test_an_unsupported_query_is_refused_uncharged(world):
    from awsbench_aws.server import unrunnable
    assert unrunnable("aws_query", {"source": "logs", "query": "fields @message | dedup @message"}, world)["charged"] \
        is False
    assert unrunnable("aws_query", {"source": "logs", "query": "parse @message /(?<c>\\w+)/ | stats count(*) by c"},
                      world) is None


def test_an_extended_query_with_a_limit_keeps_the_newest_rows(world):
    """R11-opus-5 CALL 44: `filter @timestamp > 0 | limit 25` kept 07:58-08:00 and dropped the 08:03 collapse lines."""
    ep(world, "epctl pause all", "02:49:00")
    ep(world, f"epctl apply --gen 48199 --endpoint {LEDGER} --manual --reason OPS-4452", "02:52:30")
    world.advance_to(at("03:04:30"))

    def rows(qs, groups=(CK,)):
        return [[f["value"] for f in r] for r in query(world, qs, list(groups), "02:58:00", "03:04:30")["results"]]
    ext = rows("fields @timestamp, @message | filter @timestamp > 0 | limit 25")
    assert ext == rows("fields @timestamp, @message | limit 25") and ext[0][0] >= "2026-11-17 08:03"
    both = (CK, "/vl/prod/ledger-writer")
    five = rows("fields @timestamp, @log | filter @timestamp > 0 | limit 5", both)
    assert five == rows("fields @timestamp, @log | sort @timestamp desc | limit 5", both)
    assert rows("filter @timestamp > 0 | limit 10 | stats max(@timestamp)")[0][0] == ext[0][0]


def test_ledger_writer_lines_carry_no_computed_baseline(world):
    oracle(world)
    world.advance_to(at("03:20:00"))
    lines = messages(world, "/vl/prod/ledger-writer", "02:40:00", "03:20:00")
    assert lines and not [m for m in lines if "baseline" in m]


# ---- a minute's samples and lines (R11-opus-3) ----
def test_a_datapoint_is_labelled_at_the_start_of_its_minute(world):
    """opus-3 read 08:02:00 = 0.1251, the minute that ended at 08:02; the minute in progress has no datapoint."""
    oracle(world)
    world.advance_to(at("03:10:30"))
    pts = stats(world, "Vantleaf/Checkout", "CheckoutSuccessRate", "02:50:00", "03:11:00", 60, ["Average"])
    export = dict(world.checkout_1m)
    tz = at("00:00:00").tzinfo
    assert max(p["Timestamp"] for p in pts) == at("03:09:00")
    for p in pts:
        assert p["Average"] == export[(p["Timestamp"] + timedelta(minutes=1)).astimezone(tz).isoformat()]


def test_a_host_recovered_in_the_minute_logs_no_failure_after(world):
    """opus-3: core-a-07 recovered at 03:02:18 and still logged UnknownHostException at 03:02:42."""
    oracle(world)
    world.advance_to(at("03:05:00"))
    assert world.dns.hosts["core-a-07"]["recovered_at"] == at("03:02:18")
    assert world.telemetry.lines(CK, at("03:02:42"), host="core-a-07") == []
    named = re.findall(r"host=([\w-]+)", " ".join(world.telemetry.lines(CK, at("03:02:42"))))
    assert all(world.dns.hosts[h]["recovered_at"] > at("03:02:42") for h in named)


def test_a_minute_that_collapses_after_its_record_logs_the_collapse(world):
    """opus-3: ledger-writer logged 'waiting=0' at 08:02:42 though the pool collapsed at 08:02:20."""
    collapse(world)
    assert ["2026-11-17T03:02:20-05:00", "collapsed"] in world.storm.regime_timeline
    assert world.telemetry.lines("/vl/prod/ledger-writer", at("03:02:42"))[0].startswith("WARN db pool exhausted")


def test_planner_lines_keep_the_apply_lag_while_plans_wait(world):
    """R11-opus-1: the planner logged 'pending_unapplied=9 WARN apply lag 480s' at 02:40, then no lag while pending grew;
    and gens 48200-48207 had no lines."""
    world.advance_to(at("02:43:30"))
    planner = messages(world, "/vl/platform/endpointd/planner", "02:31:30", "02:44:00")
    assert [int(re.search(r"gen=(\d+)", x).group(1)) for x in planner] == list(range(48200, 48212))
    assert planner[-3:] == [f"INFO planner gen={g} endpoints=41 changes=0 pending_unapplied={n} WARN apply lag {s}s"
                            for g, n, s in ((48209, 10, 540), (48210, 11, 600), (48211, 12, 660))]
    ep(world, "epctl pause all", "02:44:00")
    world.advance_to(at("02:50:00"))
    assert messages(world, "/vl/platform/endpointd/planner", "02:44:30", "02:50:00") == []
