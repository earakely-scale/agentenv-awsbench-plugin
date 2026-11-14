"""Load a bundled scenario and replay its `aws_seed` list against an AwsPort."""

import re
import time
from pathlib import Path

import yaml

SCENARIOS = Path(__file__).resolve().parent.parent / "scenarios"
_REF = re.compile(r"\$\{zone_id:([^}]+)\}")


def load_scenario(name_or_path: str) -> dict:
    p = Path(name_or_path)
    if not p.exists():
        p = SCENARIOS / f"{name_or_path}.yaml"
    return yaml.safe_load(p.read_text())


def _subst(value, refs: dict):
    if isinstance(value, str):
        return _REF.sub(lambda m: refs[m.group(1)], value)
    if isinstance(value, dict):
        return {k: _subst(v, refs) for k, v in value.items()}
    if isinstance(value, list):
        return [_subst(v, refs) for v in value]
    return value


def replay_seed(scenario: dict, aws, principal: str = "world:seed", sleep=time.sleep) -> dict:
    """Run every seed entry in order. `${zone_id:<name>}` takes the Id CreateHostedZone returned for that name;
    `{wait: dynamodb_table_active, table}` polls DescribeTable until ACTIVE. Returns the zone refs."""
    refs: dict[str, str] = {}
    for entry in scenario.get("aws_seed", []):
        if "wait" in entry:
            if entry["wait"] == "dynamodb_table_active":
                for _ in range(100):
                    out = aws.call("dynamodb", "DescribeTable", {"TableName": entry["table"]}, principal=principal,
                                   world_made=True)
                    if out.get("Table", {}).get("TableStatus") == "ACTIVE":
                        break
                    sleep(0.1)
            continue
        out = aws.call(entry["service"], entry["operation"], _subst(entry.get("params", {}), refs),
                       principal=principal, world_made=True)
        if entry["operation"] == "CreateHostedZone":
            refs[entry["params"]["Name"].rstrip(".")] = out["HostedZone"]["Id"].split("/")[-1]
    return refs
