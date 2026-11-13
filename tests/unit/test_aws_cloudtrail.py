import json
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from awsbench_aws.actionlog import ActionLog
from awsbench_aws.bridge import Bridge
from awsbench_aws.cloudtrail import Trail, seed_trail
from awsbench_aws.emulator import Emulator
from awsbench_aws.errors import AwsError
from awsbench_aws.server import load_scenario_file, seed_rows
from awsbench_aws.world import asg
from awsbench_aws.world.telemetry import Telemetry

ET = ZoneInfo("America/New_York")
T0 = datetime(2026, 11, 17, 2, 50, tzinfo=ET)
PLAN = {"TableName": "endpointd-plans", "Item": {"pk": {"S": "plan#0000048220"}, "generation": {"N": "48220"}}}


def log() -> ActionLog:
    al = ActionLog()
    al.record(t=T0, service="dynamodb", operation="GetItem", principal="assumed-role/SRE-OnCall/sre-primary",
              params={"TableName": "endpointd-plans", "Key": {"pk": {"S": "x"}}})
    al.record(t=T0 + timedelta(minutes=5), service="ssm", operation="PutParameter", principal="sre-primary",
              role="sre-primary", call_no=7, params={"Name": "/platform/endpointd/prod/enactor-a/enabled", "Value": "false"})
    al.record(t=T0 + timedelta(minutes=10), service="dynamodb", operation="PutItem", principal="sre-primary",
              role="sre-primary", call_no=9, params=PLAN)
    al.record(t=T0 + timedelta(minutes=11), service="dynamodb", operation="UpdateItem", world_made=True,
              principal="role:vl-endpointd-enactor-role", role="world",
              params={"TableName": "endpointd-plans", "Key": {"pk": {"S": "applied#ledger.internal.vantleaf.io"}}})
    al.record(t=T0 + timedelta(minutes=12), service="route53", operation="ChangeResourceRecordSets",
              principal="assumed-role/SRE-OnCall/sre-primary", error={"Code": "AccessDenied", "Message": "no"},
              params={"HostedZoneId": "Z1", "ChangeBatch": {"Changes": [
                  {"Action": "UPSERT", "ResourceRecordSet": {"Name": "ledger.internal.vantleaf.io.", "Type": "A"}}]}})
    al.record(service=None, tool="chat_post", t=T0)
    return al


def names(out: dict) -> list[str]:
    return [e["EventName"] for e in out["Events"]]


def test_writes_newest_first_with_cloudtrail_shape():
    out = Trail(log()).lookup({})
    assert names(out) == ["ChangeResourceRecordSets", "UpdateItem", "PutItem", "PutParameter"]
    put = out["Events"][2]
    assert put["EventTime"] == T0 + timedelta(minutes=10) and put["Username"] == "sre-primary"
    assert put["EventSource"] == "dynamodb.amazonaws.com" and put["ReadOnly"] == "false"
    assert {"ResourceType": "AWS::DynamoDB::Table", "ResourceName": "endpointd-plans"} in put["Resources"]
    detail = json.loads(put["CloudTrailEvent"])
    assert detail["eventTime"] == "2026-11-17T08:00:00Z" and detail["requestParameters"]["item"]["pk"]["S"] == \
        "plan#0000048220"
    denied = json.loads(out["Events"][0]["CloudTrailEvent"])
    assert denied["errorCode"] == "AccessDenied"
    assert out["Events"][1]["Username"] == "vl-endpointd-enactor-role"


@pytest.mark.parametrize("attr,value,want", [
    ("ResourceName", "endpointd-plans", ["UpdateItem", "PutItem"]),
    ("ResourceName", "ledger.internal.vantleaf.io", ["ChangeResourceRecordSets"]),
    ("ResourceName", "/platform/endpointd/prod/enactor-a/enabled", ["PutParameter"]),
    ("EventSource", "dynamodb.amazonaws.com", ["UpdateItem", "PutItem"]),
    ("EventName", "PutItem", ["PutItem"]),
    ("Username", "sre-primary", ["ChangeResourceRecordSets", "PutItem", "PutParameter"]),
    ("ReadOnly", "true", []),
    ("ReadOnly", "false", ["ChangeResourceRecordSets", "UpdateItem", "PutItem", "PutParameter"]),
])
def test_lookup_attributes(attr, value, want):
    assert names(Trail(log()).lookup({"LookupAttributes": [{"AttributeKey": attr, "AttributeValue": value}]})) == want


def test_time_window_and_pages():
    trail = Trail(log())
    window = {"StartTime": "2026-11-17T07:55:00Z", "EndTime": (T0 + timedelta(minutes=11)).timestamp()}
    assert names(trail.lookup(window)) == ["UpdateItem", "PutItem", "PutParameter"]
    first = trail.lookup({**window, "MaxResults": 2})
    second = trail.lookup({**window, "MaxResults": 2, "NextToken": first["NextToken"]})
    assert names(first) + names(second) == ["UpdateItem", "PutItem", "PutParameter"] and "NextToken" not in second
    with pytest.raises(AwsError):
        trail.lookup({"StartTime": T0 + timedelta(hours=1), "EndTime": T0})
    with pytest.raises(AwsError):
        trail.lookup({"LookupAttributes": [{"AttributeKey": "Colour", "AttributeValue": "x"}]})


def test_the_seed_leaves_the_race_in_the_trail():
    scenario = load_scenario_file("t01", {})
    history = seed_trail(scenario, seed_rows(scenario), {"internal.vantleaf.io": "Z1"})
    trail = Trail(ActionLog(), history)
    out = trail.lookup({"StartTime": "2026-11-17T02:31:30-05:00", "EndTime": "2026-11-17T02:31:45-05:00",
                        "LookupAttributes": [{"AttributeKey": "ResourceName", "AttributeValue": "endpointd-plans"}]})
    rows = [(e["EventTime"].astimezone(ET).strftime("%H:%M:%S"), e["EventName"], e["Username"],
             json.loads(e["CloudTrailEvent"])["requestParameters"]) for e in out["Events"]]
    deletes = [r for r in rows if r[1] == "DeleteItem"]
    assert {r[3]["key"]["pk"]["S"] for r in deletes} >= {"plan#0000048177"}
    assert all(r[0] == "02:31:41" and r[2] == "enactor-c" for r in deletes)
    ledger = [r for r in rows if r[1] == "PutItem" and r[3]["item"]["pk"]["S"] == "applied#ledger.internal.vantleaf.io"]
    assert ledger and ledger[0][:3] == ("02:31:31", "PutItem", "enactor-b")
    dns = trail.lookup({"LookupAttributes": [{"AttributeKey": "ResourceName",
                                              "AttributeValue": "ledger.internal.vantleaf.io"}]})["Events"]
    assert any("enactor-b gen=48177" in e["CloudTrailEvent"] for e in dns)
    batches = {(e["EventTime"].astimezone(ET).strftime("%H:%M:%S"), e["Username"]):
               json.loads(e["CloudTrailEvent"])["requestParameters"]["changeBatch"]["Changes"]
               for e in dns if e["EventName"] == "ChangeResourceRecordSets"}
    cleanup, upsert = batches[("02:31:41", "enactor-c")], batches[("02:31:31", "enactor-b")]
    assert [c["Action"] for c in cleanup] == ["DELETE"] * 6
    assert {c["ResourceRecordSet"]["SetIdentifier"][:7] for c in cleanup} == {"g48177-"}
    assert [c["ResourceRecordSet"] for c in cleanup] == [c["ResourceRecordSet"] for c in upsert]
    assert cleanup[0]["ResourceRecordSet"] == {"Name": "ledger.internal.vantleaf.io.", "Type": "A",
                                               "SetIdentifier": "g48177-use1-az6-1", "Weight": 100, "TTL": 60,
                                               "ResourceRecords": [{"Value": "10.20.11.152"}]}
    assert all(e["EventTime"] <= datetime.fromisoformat(scenario["start"]) for e in history)
    changes = [(e, c) for e in history if e["EventName"] == "ChangeResourceRecordSets"
               for c in json.loads(e["CloudTrailEvent"])["requestParameters"]["changeBatch"]["Changes"]]
    assert len({c["ResourceRecordSet"]["Name"] for _, c in changes}) == 41
    assert all("TTL" in c["ResourceRecordSet"] and ("ResourceRecords" in c["ResourceRecordSet"]
                                                    or "AliasTarget" in c["ResourceRecordSet"]) for _, c in changes)
    name = "checkout-int.internal.vantleaf.io."
    checkout = [c["ResourceRecordSet"] for e, c in changes
                if c["ResourceRecordSet"]["Name"] == name and e["Username"] == "enactor-c"]
    assert {s["SetIdentifier"] for s in checkout} == {"g48199-use1-az6-1", "g48199-use1-az1-1", "g48199-use1-az4-1",
                                                      "g48199-use1-az6-2"}
    seeded = [c["ResourceRecordSet"] for r in seed_rows(scenario) if r.get("operation") == "ChangeResourceRecordSets"
              for c in r["params"]["ChangeBatch"]["Changes"] if c["ResourceRecordSet"]["Name"].rstrip(".") == name[:-1]]
    assert sorted(s["ResourceRecords"][0]["Value"] for s in checkout) == \
        sorted(s["ResourceRecords"][0]["Value"] for s in seeded)


def test_one_account_in_the_trail_and_the_world():
    detail = json.loads(Trail(log()).lookup({})["Events"][0]["CloudTrailEvent"])
    ident = detail["userIdentity"]
    assert ident["accountId"] == detail["recipientAccountId"] == asg.ACCOUNT
    assert {ident["arn"].split(":")[4], ident["sessionContext"]["sessionIssuer"]["arn"].split(":")[4]} == {asg.ACCOUNT}
    alarms = Telemetry(None, {}).serve("cloudwatch", "DescribeAlarms", {}, T0)["MetricAlarms"]
    assert alarms and {a["AlarmArn"].split(":")[4] for a in alarms} == {asg.ACCOUNT}


def test_the_cli_and_the_api_both_read_the_trail():
    al = log()
    bridge = Bridge(Emulator(port=1), al, world=lambda: None)
    out = bridge.run_cli("aws cloudtrail lookup-events --lookup-attributes "
                         "AttributeKey=ResourceName,AttributeValue=endpointd-plans --start-time 2026-11-17T07:00:00Z",
                         role="sre-primary")
    assert out["exit_code"] == 0, out["stderr"]
    assert [e["EventName"] for e in json.loads(out["stdout"])["Events"]] == ["UpdateItem", "PutItem"]
    api = bridge.call_api("cloudtrail", "LookupEvents", {"MaxResults": 1}, role="sre-primary")
    assert names(api) == ["ChangeResourceRecordSets"] and api["NextToken"] == "1"


def test_t03_trail_has_the_pre_incident_writes():
    """sonnet-2 CALL 20: LookupEvents with EventSource autoscaling.amazonaws.com returned [] although
    describe-scaling-activities showed PLAT-2291's three resizes and the bf-capacity-hold was created at 06:07."""
    t03 = load_scenario_file("t03", {})
    trail = Trail(ActionLog(), seed_trail(t03, seed_rows(t03), {}))
    end = datetime(2026, 11, 16, 8, 20, tzinfo=ET)
    out = trail.lookup({"LookupAttributes": [{"AttributeKey": "EventSource",
                                              "AttributeValue": "autoscaling.amazonaws.com"}], "EndTime": end})
    rows = [(e["EventName"], e["EventTime"].astimezone(ET).strftime("%H:%M")) for e in out["Events"]]
    assert rows == [("UpdateAutoScalingGroup", "06:31"), ("UpdateAutoScalingGroup", "06:18"),
                    ("PutScheduledUpdateGroupAction", "06:07"), ("UpdateAutoScalingGroup", "06:05")]
    assert all(e["Username"] == "elena.sokolova" for e in out["Events"])
    assert all({"ResourceType": "AWS::AutoScaling::AutoScalingGroup", "ResourceName": "vl-streamroute-asg"}
               in e["Resources"] for e in out["Events"])
    hold = json.loads(out["Events"][2]["CloudTrailEvent"])["requestParameters"]
    assert hold["scheduledActionName"] == "bf-capacity-hold" and hold["recurrence"] == "0 14 * * 1-5"
    ecs = trail.lookup({"LookupAttributes": [{"AttributeKey": "EventSource", "AttributeValue": "ecs.amazonaws.com"}],
                        "EndTime": end})["Events"]
    assert [e["EventName"] for e in ecs] == ["UpdateService", "RegisterTaskDefinition"]
    assert ecs[0]["EventTime"].astimezone(ET).strftime("%H:%M:%S") == "07:31:02" and ecs[0]["Username"] == \
        "vl-ci-deployer"
    assert json.loads(ecs[0]["CloudTrailEvent"])["requestParameters"]["taskDefinition"] == "ledger-writer:234"


def test_t01_trail_has_the_ingress_refresh_and_tonights_deploys():
    """R11-opus-6 CALL 106: LookupEvents EventName=StartInstanceRefresh returned [] while DescribeInstanceRefreshes
    showed 4f1c8a52 InProgress since 06:40Z, and the agent wrote into PLAT-413 that who started it was unknown."""
    t01 = load_scenario_file("t01", {})
    trail = Trail(ActionLog(), seed_trail(t01, seed_rows(t01), {}))
    refresh = trail.lookup({"LookupAttributes": [{"AttributeKey": "EventName",
                                                  "AttributeValue": "StartInstanceRefresh"}]})["Events"]
    assert len(refresh) == 1 and refresh[0]["EventTime"] == datetime(2026, 11, 17, 6, 40, tzinfo=UTC)
    assert refresh[0]["Username"] == "graviton-bot"
    assert json.loads(refresh[0]["CloudTrailEvent"])["userIdentity"]["sessionContext"]["sessionIssuer"][
        "userName"] == "vl-deploy-pipeline"
    assert {"ResourceType": "AWS::AutoScaling::AutoScalingGroup", "ResourceName": "vl-ledger-ingress-asg"} in \
        refresh[0]["Resources"]
    by_asg = trail.lookup({"LookupAttributes": [{"AttributeKey": "ResourceName",
                                                 "AttributeValue": "vl-ledger-ingress-asg"}]})["Events"]
    assert [e["EventId"] for e in by_asg] == [refresh[0]["EventId"]]
    ec2 = trail.lookup({"LookupAttributes": [{"AttributeKey": "EventSource", "AttributeValue": "ec2.amazonaws.com"}]})
    gone = [json.loads(e["CloudTrailEvent"])["requestParameters"]["instanceIds"] for e in ec2["Events"]
            if e["EventName"] == "TerminateInstances"]
    assert gone == [["i-04a81c5eba5dd76f6"], ["i-089a804412af9e917"], ["i-044927a475d00213d"]]
    assert all(e["Username"] == "AutoScaling" for e in ec2["Events"] if e["EventName"] != "CreateLaunchTemplateVersion")
    deploy = trail.lookup({"LookupAttributes": [{"AttributeKey": "EventName", "AttributeValue": "UpdateService"}]})
    assert [(e["EventTime"], json.loads(e["CloudTrailEvent"])["requestParameters"]["taskDefinition"])
            for e in deploy["Events"]] == [(datetime(2026, 11, 17, 6, 55, tzinfo=UTC), "checkout-api:412")]
