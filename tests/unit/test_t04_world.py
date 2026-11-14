"""T04 mechanics: the merchant sets the design's checks key on, the calibration lines of world/mechanisms.yaml (do-nothing
N = 125, an early full resume N = 18, an API rollback N in the half tier), point-in-time restores served from the write
history, and rmctl on rm-admin-01."""

from datetime import UTC, datetime

import pytest
from awsbench_aws.errors import AwsError
from awsbench_aws.world import t04_data as D
from t04_fake import agent_call, at, world

CALLER = {"role": "sre-primary", "principal": "assumed-role/SRE-OnCall/sre-primary", "call_no": 7}


def sh(w, cmd, hms=None):
    if hms:
        w.advance_to(at(w, hms))
    out = w.fleet_exec("rm-admin-01", cmd, dict(CALLER))
    h = out["hosts"][0]
    return h["stdout"], h["exit_code"]


def pause(w, aws, hms="13:17:00", which=("scaler", "rebalancer")):
    w.advance_to(at(w, hms))
    for wf in which:
        aws.ssm[f"/routemap/prod/workflows/{wf}/enabled"] = "false"
    w.mech.refresh_ssm()


@pytest.fixture(scope="module")
def sets():
    return D.merchant_sets()


def test_sets_match_the_design(sets):
    assert len(sets["established"]) + len(sets["recent"]) == 4103
    assert (len(sets["mirror_opt_out"]), len(sets["onboarded_since_oct6"]), len(sets["friday_onboarded"])) == (236, 126, 9)
    assert len(sets["purged"]) == 371
    assert len(sets["rewritten_before_t0"]) == 29 and len(sets["latent_tombstones_at_t0"]) == 342
    assert set(D.MUST_INCLUDE) <= set(sets["purged"])
    assert not set(D.MUST_EXCLUDE) & set(sets["purged"])
    assert {"mch_17336", "mch_23518", "mch_29047", "mch_10422"} <= set(sets["rewritten_before_t0"])
    assert not set(D.NEVER_TOUCHED) & set(sets["rewritten_before_t0"])
    assert len(sets["dashboard_retry_backlog"]) == 14
    assert set(sets["dashboard_retry_backlog"]) <= set(sets["latent_tombstones_at_t0"]) - set(D.NEVER_TOUCHED)
    writers = [w for _, w, _ in sets["rewrites"]]
    assert writers.count("routemap-scaler") == 25 and writers.count("routemap-rebalancer") == 4


def test_the_table_at_the_page():
    w, aws = world()
    items = aws.items()
    west = aws.items(region="us-west-2")
    assert len(items) == 8238 and items == west
    m = w.mech
    bf = items[("mch_10422", "ROUTE")]
    assert (bf["lifecycle"], bf["route_version"], bf["route_spec"]["cells"]) == ("ACTIVE", 89, [])
    assert sum(1 for k, v in items.items() if v.get("lifecycle") == "PURGED") == 342
    assert m.dp["mch_10422"]["cells"] == [] and m.dp["mch_41207"]["cells"] == ["cell-ca-2"]
    assert items[("mch_30988", "LIMITS")]["daily_cap_usd"] == 2500
    assert items[("mch_11872", "ROUTE")]["route_spec"]["cells"] == ["cell-9"]


def test_do_nothing_breaks_125():
    w, _ = world()
    w.advance_to(at(w, "16:12:00"))
    assert len(w.mech.first_broken) == 125


def test_containment_before_1320_breaks_none():
    w, aws = world()
    pause(w, aws)
    w.advance_to(at(w, "16:12:00"))
    assert len(w.mech.first_broken) == 0
    assert w.mech.tel.cell7 > 800  # scaler paused for hours: cell-7 drifts (rules.py)


def test_early_full_resume_breaks_18():
    w, aws = world()
    pause(w, aws)
    w.advance_to(at(w, "13:38:00"))
    for wf in ("scaler", "rebalancer"):
        aws.ssm[f"/routemap/prod/workflows/{wf}/enabled"] = "true"
    w.mech.refresh_ssm()
    w.advance_to(at(w, "14:04:00"))
    assert len(w.mech.first_broken) == 18


def test_api_rollback_breaks_through_dashboard_saves():
    w, aws = world()
    pause(w, aws)
    w.advance_to(at(w, "13:31:00"))
    aws.service_td = "arn:aws:ecs:us-east-1:000000000000:task-definition/routemap-api:57"
    agent_call(w, "aws_cli", at(w, "13:31:00"), 30, [{"service": "ecs", "operation": "UpdateService", "params": {
        "cluster": "vl-prod-core", "service": "routemap-api", "taskDefinition": "routemap-api:57"}, "error": None}])
    w.advance_to(at(w, "13:48:00"))
    n = len(w.mech.first_broken)
    assert 12 <= n <= 23, n
    assert all(v["cause"].startswith("agent:") for v in w.mech.first_broken.values())


def test_pitr_restores_history_into_a_new_table_after_15_minutes():
    w, aws = world()
    m = w.mech
    w.advance_to(at(w, "13:25:00"))
    out = w.serve_aws("dynamodb", "RestoreTableToPointInTime", {
        "SourceTableName": "routemap-state", "TargetTableName": "side",
        "RestoreDateTime": datetime(2026, 11, 21, 0, 41, 30, tzinfo=UTC)}, region="us-east-1")
    assert out["TableDescription"]["TableStatus"] == "CREATING"
    with pytest.raises(AwsError) as e:
        w.serve_aws("dynamodb", "Scan", {"TableName": "side"}, region="us-east-1")
    assert e.value.code == "ResourceNotFoundException"
    assert w.serve_aws("dynamodb", "DescribeTable", {"TableName": "side"}, region="us-east-1")["Table"][
        "TableStatus"] == "CREATING"
    with pytest.raises(AwsError) as e:
        w.serve_aws("dynamodb", "RestoreTableToPointInTime", {
            "SourceTableName": "routemap-state", "TargetTableName": "side2",
            "RestoreDateTime": datetime(2026, 10, 1, tzinfo=UTC)}, region="us-east-1")
    assert e.value.code == "InvalidRestoreTimeException"
    w.advance_to(at(w, "13:43:00"))
    assert m.restores["side"]["status"] == "ACTIVE"
    side = aws.items("side")
    assert side[("mch_10422", "ROUTE")]["route_spec"]["cells"] == ["cell-7", "cell-11"]
    assert side[("mch_37755", "ROUTE")]["settlement_profile"]["payout_schedule"] == "weekly"
    assert side[("mch_30988", "LIMITS")]["daily_cap_usd"] == 250000
    assert ("mch_44412", "ROUTE") not in side
    desc = w.serve_aws("dynamodb", "DescribeTable", {"TableName": "side"}, region="us-east-1")["Table"]
    assert desc["TableStatus"] == "ACTIVE" and desc["TableId"] == m.restores["side"]["table_id"]
    assert desc["RestoreSummary"]["RestoreInProgress"] is False and desc["ItemCount"] == len(side)
    live = w.serve_aws("dynamodb", "DescribeTable", {"TableName": "routemap-state"}, region="us-east-1")["Table"]
    assert live["TableId"] == "6f2c7a1e-91b4-4d0e-8c3a-5b7e2d9f4a10" and live["CreationDateTime"].year == 2024
    assert live["ItemCount"] == len(m.live)


def test_backups_list_the_nightlies_and_snapshot_at_call_time():
    w, _ = world()
    rows = w.serve_aws("dynamodb", "ListBackups", {"TableName": "routemap-state"}, region="us-east-1")["BackupSummaries"]
    assert [r["BackupName"] for r in rows][-1] == "routemap-state-nightly-20261121T0800Z"
    assert len(rows) == 6


def test_rmctl_restore_is_denied_and_help_lists_commands():
    w, _ = world()
    out, code = sh(w, "rmctl state restore --pitr 2026-11-21T00:41:30Z")
    assert code == 1 and "explicit deny" in out
    out, code = sh(w, "rmctl help")
    assert code == 0 and "state merge" in out
    out, code = sh(w, "rmctl route describe mch_30988")
    assert code == 1 and "KeyError: route_spec" in out


def test_rmctl_oracle_repair():
    w, aws = world()
    m = w.mech
    pause(w, aws)
    w.serve_aws("dynamodb", "RestoreTableToPointInTime", {
        "SourceTableName": "routemap-state", "TargetTableName": "pitr", "RestoreDateTime": "2026-11-21T00:41:30Z"},
        region="us-east-1")
    w.advance_to(at(w, "13:42:00"))
    assert sh(w, "rmctl route list --filter purge_job=pj-20261121-004208 --out tomb.keys")[0].startswith("342")
    assert sh(w, "rmctl route list --filter cells_empty=true --out empty.keys")[0].startswith("29")
    assert sh(w, "rmctl keys union tomb.keys empty.keys --out purged.keys")[0].startswith("371")
    out, _ = sh(w, "rmctl state merge --source pitr --keys /var/tmp/rmctl/purged.keys --whole-item", "13:47:00")
    assert "updated 371" in out
    assert sh(w, "rmctl dataplane status mch_10422")[0].split()[1] == "EMPTY_CELLS"
    out, _ = sh(w, "rmctl publish --keys /var/tmp/rmctl/purged.keys", "13:49:00")
    assert "ACCEPTED 371" in out and "STALE 0" in out
    assert sh(w, "rmctl dataplane status --keys /var/tmp/rmctl/purged.keys")[0].startswith("371 keys: OK 371")
    items = aws.items()
    assert items[("mch_30988", "LIMITS")]["daily_cap_usd"] == 2500
    assert items[("mch_11872", "ROUTE")]["route_spec"]["pinned"] is True
    assert all(not x["world_made"] for x in m.writes if x["writer"].startswith("rmctl"))


def test_merge_without_publish_leaves_the_data_plane_empty():
    w, aws = world()
    pause(w, aws)
    w.serve_aws("dynamodb", "RestoreTableToPointInTime", {
        "SourceTableName": "routemap-state", "TargetTableName": "pitr", "RestoreDateTime": "2026-11-21T00:41:30Z"},
        region="us-east-1")
    w.advance_to(at(w, "13:42:00"))
    sh(w, "rmctl route list --filter cells_empty=true --out empty.keys")
    sh(w, "rmctl state merge --source pitr --keys /var/tmp/rmctl/empty.keys --whole-item")
    w.advance_to(at(w, "14:30:00"))
    assert w.mech.broken(w.mech.dp["mch_10422"])


def test_agent_dynamodb_writes_are_resynced_and_published():
    w, aws = world()
    m = w.mech
    item = dict(aws.tables[("routemap-state", "us-east-1")][("mch_10422", "ROUTE")])
    ref = D.item_ddb({**m.hist.at(("mch_10422", "ROUTE"), D.ts("2026-11-21T00:42:00+00:00")), "route_version": 95,
                      "publish_mode": "AUTO"})
    aws.tables[("routemap-state", "us-east-1")][("mch_10422", "ROUTE")] = ref
    agent_call(w, "aws_api", at(w, "13:30:00"), 12, [{"service": "dynamodb", "operation": "PutItem", "params": {
        "TableName": "routemap-state", "Item": ref}, "error": None}])
    assert item["route_version"] != ref["route_version"]
    assert m.dp["mch_10422"]["cells"] == ["cell-7", "cell-11"] and m.dp["mch_10422"]["version"] == 95
    assert any(x["writer"] == "agent" and x["key"] == ["mch_10422", "ROUTE"] for x in m.writes)


def test_the_bundled_scenario_loads_and_runs():
    from pathlib import Path

    import yaml
    from awsbench_aws.world import make_world
    from t04_fake import FakeAws, Ws
    path = Path(D.__file__).resolve().parents[1] / "scenarios" / "t04.yaml"
    sc = yaml.safe_load(path.read_text())
    w = make_world(sc, "t04-unit", FakeAws(), Ws())
    w.start()
    w.advance_to(at(w, "16:12:00"))
    fired = {e["id"] for e in w.timeline.export() if e["status"] == "fired"}
    assert {"ev-owen-first", "ev-hannah-rollback", "ev-daniel-cutover", "ev-jordan-email", "ev-onboard-44419",
            "ev-page-escalation"} <= fired
    assert len(w.mech.first_broken) == 125


def test_rm_admin_ls_lists_the_workdir_and_diff_folders():
    w, _ = world()
    w.serve_aws("dynamodb", "RestoreTableToPointInTime", {
        "SourceTableName": "routemap-state", "TargetTableName": "pitr", "RestoreDateTime": "2026-11-21T00:41:30Z"},
        region="us-east-1")
    w.advance_to(at(w, "13:42:00"))
    sh(w, "rmctl route list --filter cells_empty=true --out empty.keys")
    sh(w, "rmctl state diff --source pitr --out /var/tmp/rmctl/d1")
    assert sh(w, "ls /var/tmp/rmctl") == ("d1\nempty.keys\n", 0)
    assert sh(w, "ls /var/tmp/rmctl/d1/") == ("differs.csv\nonly_in_source.csv\nonly_in_target.csv\n", 0)
    out, code = sh(w, "ls -l /var/tmp/rmctl")
    assert code == 0 and out.startswith("total 8\ndrwxr-xr-x 2 root root 4096 Nov 21 ") and out.rstrip().endswith(
        " empty.keys")
    assert sh(w, "ls /var/tmp/rmctl/nope") == ("ls: cannot access '/var/tmp/rmctl/nope': No such file or directory\n", 2)
    assert sh(w, "cat /var/tmp/rmctl/d1") == ("cat: /var/tmp/rmctl/d1: Is a directory\n", 1)


@pytest.mark.parametrize("text,rule", [
    ("@kwame which keys did the purge touch?", "K7-which-keys"),
    ("@kwame how many routes need restoring?", "K7-which-keys"),
    ("@kwame how do I find purged routes that no longer look purged?", "K2-find-rewritten"),
    ("@kwame I count 342 tombstones but the audit says 371, where are the rest?", "K2-find-rewritten"),
    ("@kwame how do I find the purged routes the scaler rebuilt from defaults?", "K2-find-rewritten"),
])
def test_kwame_suggests_cells_empty_only_when_asked_about_rewritten_routes(text, rule):
    from pathlib import Path

    import yaml
    from awsbench_aws.world import make_world
    from t04_fake import FakeAws, Ws
    sc = yaml.safe_load((Path(D.__file__).resolve().parents[1] / "scenarios" / "t04.yaml").read_text())
    w = make_world(sc, "t04-unit", FakeAws(), Ws())
    hit = w.timeline.classify("kwame.asante", text)
    assert hit and hit[0]["id"] == rule
    assert ("cells_empty" in hit[0]["reply"]) == (rule == "K2-find-rewritten")
    assert "stop looking purged" not in hit[0]["reply"]
