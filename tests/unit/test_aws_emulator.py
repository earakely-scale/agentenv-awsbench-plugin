"""Against a real AWS Emulator in a container; skipped when Docker or the image is unavailable."""

import json
import shutil
import subprocess
import time
import uuid

import pytest
from awsbench_aws.actionlog import ActionLog
from awsbench_aws.bridge import Bridge

IMAGE = "awsbench-emulator-base:dev"
PORT = 19297
ZONE = "internal.vantleaf.io"
SEED = [
    {"kind": "call", "service": "route53", "operation": "CreateHostedZone",
     "params": {"Name": ZONE, "CallerReference": "seed-1"}},
    {"kind": "call", "service": "route53", "operation": "ChangeResourceRecordSets", "params": {
        "HostedZoneId": f"${{ref:route53.zone/{ZONE}.Id}}", "ChangeBatch": {"Changes": [{
            "Action": "UPSERT", "ResourceRecordSet": {"Name": f"ledger.{ZONE}", "Type": "A", "SetIdentifier": "g1-a",
                                                      "Weight": 100, "TTL": 60,
                                                      "ResourceRecords": [{"Value": "10.20.4.11"}]}}]}}},
    {"kind": "call", "service": "dynamodb", "operation": "CreateTable", "params": {
        "TableName": "endpointd-plans", "BillingMode": "PAY_PER_REQUEST",
        "AttributeDefinitions": [{"AttributeName": "pk", "AttributeType": "S"}],
        "KeySchema": [{"AttributeName": "pk", "KeyType": "HASH"}]}},
    {"wait": "dynamodb_table_active"},
    {"kind": "call", "service": "dynamodb", "operation": "PutItem", "params": {
        "TableName": "endpointd-plans", "Item": {"pk": {"S": f"applied#ledger.{ZONE}"}, "generation": {"N": "48177"}}}},
    {"kind": "call", "service": "ssm", "operation": "PutParameter", "params": {
        "Name": "/platform/endpointd/prod/planner/enabled", "Type": "String", "Value": "true"}},
    {"kind": "inject", "ext_action": "cloudtrail", "args": {}},
]


@pytest.fixture(scope="module")
def emulator_url():
    if shutil.which("docker") is None:
        pytest.skip("docker is not installed")
    if subprocess.run(["docker", "image", "inspect", IMAGE], capture_output=True, check=False).returncode != 0:
        pytest.skip(f"{IMAGE} is not built")
    name = f"awsbench-test-emulator-{uuid.uuid4().hex[:8]}"
    subprocess.run(["docker", "run", "-d", "--rm", "--name", name, "-p", f"127.0.0.1:{PORT}:9297", IMAGE,
                    "aws-emulator", "-a", "off", "--telemetry", "off", "-b", "0.0.0.0", "-p", "9297"],
                   check=True, capture_output=True)
    try:
        yield f"https://127.0.0.1:{PORT}"
    finally:
        subprocess.run(["docker", "rm", "-f", name], capture_output=True, check=False)


@pytest.fixture
def server(emulator_url, monkeypatch):
    monkeypatch.setenv("AWSBENCH_EMULATOR_URL", emulator_url)
    monkeypatch.setenv("AWSBENCH_EMULATOR_VERIFY", "0")
    from awsbench_aws.server import AwsServer
    s = AwsServer()
    s.emulator.wait(30)
    s.ready.set()
    return s


def test_seed_replay_sweep_and_cli(server):
    seeded = server._replay_seed({"aws_seed": SEED})
    assert seeded == {"ok": 5, "failed": 0, "errors": []}
    state = server.sweep()
    assert state["route53"][ZONE][f"ledger.{ZONE}"][0]["SetIdentifier"] == "g1-a"
    assert state["dynamodb"]["endpointd-plans"][f"applied#ledger.{ZONE}"]["generation"] == 48177
    assert state["ssm"]["/platform/endpointd/prod/planner/enabled"] == "true"

    log = ActionLog()
    bridge = Bridge(server.emulator, log, profiles={"sre-primary": [{"profile": "vl-prod/SRE-OnCall",
                                                                       "permission_set": "SRE-OnCall"}]})
    out = bridge.run_cli("aws route53 list-hosted-zones", role="sre-primary")
    assert out["exit_code"] == 0, out
    zones = json.loads(out["stdout"])["HostedZones"]
    assert any(z["Name"] == f"{ZONE}." for z in zones)
    zone_id = next(z["Id"] for z in zones if z["Name"] == f"{ZONE}.").rsplit("/", 1)[-1]
    out = bridge.run_cli(f"aws route53 list-resource-record-sets --hosted-zone-id {zone_id}", role="sre-primary")
    assert "10.20.4.11" in out["stdout"]
    out = bridge.run_cli("aws ssm put-parameter --name /platform/endpointd/prod/planner/enabled --value false "
                         "--type String --overwrite", role="sre-primary")
    assert out["exit_code"] == 0, out
    out = bridge.call_api("ssm", "GetParameter", {"Name": "/platform/endpointd/prod/planner/enabled"},
                          role="sre-primary")
    assert out["Parameter"]["Value"] == "false"
    out = bridge.run_cli("aws dynamodb put-item --table-name other --item '{\"pk\": {\"S\": \"x\"}}'",
                         role="sre-primary")
    assert out["exit_code"] != 0 and "AccessDenied" in out["stderr"]
    out = bridge.run_cli("aws dynamodb get-item --table-name nope --key '{\"pk\": {\"S\": \"x\"}}'", role="sre-primary")
    assert out["exit_code"] != 0 and "ResourceNotFoundException" in out["stderr"] and "127.0.0.1" not in out["stderr"]
    ops = [(r["operation"], (r["error"] or {}).get("Code")) for r in log.export()]
    assert ops == [("ListHostedZones", None), ("ListResourceRecordSets", None), ("PutParameter", None),
                   ("GetParameter", None), ("PutItem", "AccessDenied"), ("GetItem", "ResourceNotFoundException")]
    started = time.monotonic()
    bridge.run_cli("aws sts get-caller-identity", role="sre-primary")
    assert time.monotonic() - started < 5
