import json
from datetime import UTC, datetime, timedelta

import pytest
from awsbench_aws.actionlog import ActionLog, resources_of
from awsbench_aws.bridge import DEFAULT_POLICY, Bridge, CommandError, Faults, iam_allows, parse_command, split_globals
from awsbench_aws.emulator import Emulator
from awsbench_aws.errors import AwsError
from awsbench_aws.world import World, replay_seed
from test_world_t01 import SCENARIO, FakeAws, FakeWs, at

T = datetime(2026, 11, 17, 7, 41, tzinfo=UTC)
PROFILES = {"sre-primary": [{"profile": "vl-prod/SRE-OnCall", "account": "vl-prod", "permission_set": "SRE-OnCall"}]}
ZONES = {"HostedZones": [{"Id": "/hostedzone/Z1", "Name": "internal.vantleaf.io.", "CallerReference": "r",
                          "Config": {"PrivateZone": True}, "ResourceRecordSetCount": 3}],
         "IsTruncated": False, "MaxItems": "100"}


class ServingWorld:
    """Answers every call itself, so the bridge runs end to end with no emulator."""

    def __init__(self):
        self.calls = []

    def serve_aws(self, service, operation, params):
        self.calls.append((service, operation, params))
        if operation == "ListHostedZones":
            return ZONES
        if operation == "ChangeResourceRecordSets":
            return {"ChangeInfo": {"Id": "/change/C1", "Status": "PENDING", "SubmittedAt": T}}
        if operation == "GetParameter":
            return {"Parameter": {"Name": params["Name"], "Value": "false", "Type": "String", "Version": 3}}
        return {}


@pytest.fixture
def bridge():
    world = ServingWorld()
    b = Bridge(Emulator(port=1), ActionLog(), profiles=PROFILES, world=lambda: world,
               faults=[{"service": "route53", "operation": "ChangeResourceRecordSets", "error": "Throttling",
                        "times": 1}])
    b.test_world = world
    return b


@pytest.mark.parametrize("command", [
    "aws route53 list-hosted-zones | jq .",
    "aws route53 list-hosted-zones; rm -rf /",
    "aws route53 list-hosted-zones && aws s3 ls",
    "aws route53 list-hosted-zones > out.json",
    "aws s3 ls `whoami`",
    "aws s3 ls $(whoami)",
    "route53 list-hosted-zones",
    "kubectl get pods",
    "aws",
    "aws s3 ls 'unterminated",
])
def test_parse_rejects(command):
    with pytest.raises(CommandError):
        parse_command(command)


def test_parse_keeps_quoted_operators():
    argv = parse_command("aws dynamodb scan --table-name t --filter-expression 'gen > :g' "
                         "--expression-attribute-values '{\":g\": {\"N\": \"1\"}}'")
    assert argv[:2] == ["dynamodb", "scan"] and "gen > :g" in argv
    assert json.loads(argv[-1]) == {":g": {"N": "1"}}


def test_split_globals():
    argv, profile, region = split_globals(["--profile", "vl-prod/SRE-OnCall", "route53", "list-hosted-zones",
                                           "--region=us-west-2", "--endpoint-url", "http://evil", "--debug"])
    assert argv == ["route53", "list-hosted-zones"] and profile == "vl-prod/SRE-OnCall" and region == "us-west-2"


@pytest.mark.parametrize("service,op,params,ok", [
    ("route53", "ListHostedZones", {}, True),
    ("route53", "ChangeResourceRecordSets", {}, True),
    ("dynamodb", "PutItem", {"TableName": "endpointd-plans"}, True),
    ("dynamodb", "PutItem", {"TableName": "ledger"}, False),
    ("dynamodb", "Scan", {"TableName": "ledger"}, True),
    ("ssm", "PutParameter", {"Name": "/platform/endpointd/prod/planner/enabled"}, True),
    ("ssm", "PutParameter", {"Name": "/secret/x"}, False),
    ("iam", "ListUsers", {}, False),
    ("iam", "CreateUser", {}, False),
    ("organizations", "DescribeOrganization", {}, False),
    ("ec2", "TerminateInstances", {}, False),
    ("ecs", "UpdateService", {}, True),
    ("application-autoscaling", "RegisterScalableTarget", {}, True),
    ("logs", "StartQuery", {}, True),
    ("logs", "GetQueryResults", {"queryId": "q"}, True),
    ("logs", "StopQuery", {"queryId": "q"}, True),
    ("logs", "FilterLogEvents", {"logGroupName": "g"}, True),
    ("cloudtrail", "LookupEvents", {}, True),
    ("dynamodb", "Query", {"TableName": "vl-ledger-outbox"}, True),
    ("autoscaling", "DescribeInstanceRefreshes", {"AutoScalingGroupName": "vl-ledger-ingress-asg"}, True),
    ("route53", "TestDNSAnswer", {}, True),
    ("iam", "GetRole", {}, False),
    ("logs", "DeleteLogGroup", {"logGroupName": "g"}, False),
])
def test_iam(service, op, params, ok):
    assert iam_allows(DEFAULT_POLICY, service, op, params) is ok


def test_faults_are_seeded_and_counted():
    rules = [{"service": "route53", "operation": "Change*", "error": "Throttling", "rate": 0.5}]
    draws = []
    for seed in ("a", "a", "b"):
        f, fired = Faults(rules, seed), []
        for _ in range(40):
            try:
                f.check("route53", "ChangeResourceRecordSets", {}, None)
                fired.append(0)
            except AwsError as e:
                assert e.code == "Throttling" and e.message == "Rate exceeded"
                fired.append(1)
        draws.append(fired)
    assert draws[0] == draws[1] != draws[2] and 5 < sum(draws[0]) < 35
    f = Faults([{"service": "ssm", "error": {"code": "InternalError", "message": "boom"}, "times": 1,
                 "from": "2026-11-17T07:40:00+00:00"}], "s")
    f.check("ssm", "GetParameter", {}, datetime(2026, 11, 17, 7, 39, tzinfo=UTC))
    with pytest.raises(AwsError):
        f.check("ssm", "GetParameter", {}, T)
    f.check("ssm", "GetParameter", {}, T)


def test_cli_served_by_world_and_logged(bridge):
    call = {"call_no": 7, "t": T, "t_issue": T, "tool": "aws_cli"}
    out = bridge.run_cli("aws --profile vl-prod/SRE-OnCall route53 list-hosted-zones", role="sre-primary", call=call)
    assert out["exit_code"] == 0, out
    assert json.loads(out["stdout"])["HostedZones"][0]["Name"] == "internal.vantleaf.io."
    (row,) = bridge.actionlog.export()
    assert row["service"] == "route53" and row["operation"] == "ListHostedZones" and row["call_no"] == 7
    assert row["principal"] == "assumed-role/SRE-OnCall/sre-primary" and row["error"] is None
    assert row["t"] == T.isoformat() and not row["world_made"] and row["tool"] == "aws_cli"


def test_cli_unknown_profile_is_access_denied(bridge):
    out = bridge.run_cli("aws --profile vl-prod/Admin route53 list-hosted-zones", role="sre-primary")
    assert out["exit_code"] == 254 and "AccessDenied" in out["stderr"] and "vl-prod/SRE-OnCall" in out["stderr"]
    assert bridge.actionlog.export() == []


def test_cli_iam_denies_and_logs(bridge):
    out = bridge.run_cli("aws iam list-users", role="sre-primary")
    assert out["exit_code"] != 0 and "AccessDenied" in out["stderr"] and "iam:ListUsers" in out["stderr"]
    (row,) = bridge.actionlog.export()
    assert row["error"]["Code"] == "AccessDenied"
    assert not any(c[1] == "ListUsers" for c in bridge.test_world.calls)


def test_cli_fault_then_success(bridge):
    cmd = ("aws route53 change-resource-record-sets --hosted-zone-id Z1 --change-batch "
           "'{\"Changes\":[{\"Action\":\"UPSERT\",\"ResourceRecordSet\":{\"Name\":\"ledger.internal.vantleaf.io\","
           "\"Type\":\"A\",\"SetIdentifier\":\"a\",\"Weight\":100,\"TTL\":60,"
           "\"ResourceRecords\":[{\"Value\":\"10.0.0.1\"}]}}]}'")
    first = bridge.run_cli(cmd, role="sre-primary")
    assert first["exit_code"] != 0 and "Throttling" in first["stderr"]
    second = bridge.run_cli(cmd, role="sre-primary")
    assert second["exit_code"] == 0, second
    rows = bridge.actionlog.export()
    assert [r["error"] and r["error"]["Code"] for r in rows] == ["Throttling", None]
    assert "route53:record/ledger.internal.vantleaf.io/A#a" in rows[1]["resources"]
    assert rows[1]["params"]["HostedZoneId"] == "Z1"


def test_cli_bad_command_and_argparse_errors(bridge):
    out = bridge.run_cli("aws route53 list-hosted-zones | head", role="sre-primary")
    assert out["exit_code"] == 2 and "pipes" in out["stderr"]
    out = bridge.run_cli("aws route53 no-such-thing", role="sre-primary")
    assert out["exit_code"] != 0 and "Invalid choice" in out["stderr"]


def test_api_served_and_errors(bridge):
    out = bridge.call_api("ssm", "GetParameter", {"Name": "/platform/x"}, role="sre-primary")
    assert out["Parameter"]["Value"] == "false"
    assert bridge.call_api("iam", "ListUsers", {}, role="sre-primary")["error"]["Code"] == "AccessDenied"
    assert bridge.call_api("ssm", "NoSuchOp", {}, role="sre-primary")["error"]["Code"] == "InvalidAction"
    assert bridge.call_api("ssm", "GetParameter", {}, role="sre-primary")["error"]["Code"] == "ValidationException"
    assert bridge.call_api("ssm", "GetParameter", {"Name": "x"}, role="sre-primary",
                           profile="nope")["error"]["Code"] == "AccessDenied"


def test_resources_of():
    assert resources_of("dynamodb", "PutItem", {"TableName": "endpointd-plans",
                                                "Item": {"pk": {"S": "applied#ledger"}}}) == [
        "dynamodb:table/endpointd-plans/applied#ledger"]
    assert resources_of("ssm", "PutParameter", {"Name": "/platform/a"}) == ["ssm:parameter/platform/a"]


@pytest.mark.parametrize("command", [
    "aws ssm put-parameter --name /vl/prod/x --type String --value file:///etc/hostname",
    "aws ssm put-parameter --name /vl/prod/x --type String --value=fileb:///etc/hostname",
    "aws route53 list-hosted-zones --cli-input-json file:///app/awsbench_aws/scenarios/t01.yaml",
    "aws configure set region eu-west-1",
])
def test_parse_refuses_local_files_and_configure(command):
    with pytest.raises(CommandError):
        parse_command(command)


def test_cli_passes_urls_through_without_fetching_them(bridge):
    out = bridge.run_cli("aws ssm put-parameter --name /vl/prod/x --type String --overwrite "
                         "--value http://127.0.0.1:1/awsbench/inbox", role="sre-primary")
    assert out["exit_code"] == 0, out["stderr"]
    assert bridge.test_world.calls[-1][2]["Value"] == "http://127.0.0.1:1/awsbench/inbox"


def test_logs_insights_reads_pass_the_gate(bridge):
    for op, params in (("StartQuery", {"logGroupNames": ["g"], "queryString": "fields @message", "startTime": 1,
                                       "endTime": 2}), ("GetQueryResults", {"queryId": "q"})):
        assert "error" not in bridge.call_api("logs", op, params, role="sre-primary"), op
    assert [c[1] for c in bridge.test_world.calls] == ["StartQuery", "GetQueryResults"]


def test_world_served_times_come_back_in_utc():
    world = World(SCENARIO, "eval-seed-1", aws := FakeAws(), FakeWs())
    replay_seed(SCENARIO, aws)
    world.start()
    world.advance_to(at("02:47:00"))
    b = Bridge(Emulator(port=1), ActionLog(), profiles=PROFILES, world=lambda: world)
    out = b.run_cli("aws cloudwatch describe-alarms --alarm-names checkout-api-CheckoutSuccessRate-critical",
                    role="sre-primary", call={"t": at("02:47:00")})
    assert out["exit_code"] == 0, out
    (alarm,) = json.loads(out["stdout"])["MetricAlarms"]
    assert alarm["StateUpdatedTimestamp"] == "2026-11-17T07:32:00+00:00"
    asg = b.call_api("autoscaling", "DescribeAutoScalingGroups", {}, role="sre-primary",
                     call={"t": at("02:47:00")})["AutoScalingGroups"][0]
    assert asg["CreatedTime"].utcoffset() == timedelta(0)
