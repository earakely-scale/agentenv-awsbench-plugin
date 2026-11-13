"""Drive a deployed AwsBench env through T01 without an agent, then grade it.

Deploy the env and keep it up (tasks/e2e-env.json runs deploy, configure, clock and tool access):

    agent-env run src/agentenv_awsbench/bundles/awsbench --task e2e-env --keep

then, with the gateway URL the run prints:

    python tests/e2e/driver.py --gateway http://127.0.0.1:<port> --mode oracle   # expect pass, score ~1.0
    python tests/e2e/driver.py --gateway ... --mode nothing                      # expect score <= 0.1
    python tests/e2e/driver.py --gateway ... --mode trap                         # applies 48177: capped at 0.2

Each mode needs a freshly configured env (the episode ends at the horizon). Exits 1 when an expectation fails.
"""
import argparse
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from gw import Gateway, load_grade

LEDGER = "ledger.internal.vantleaf.io"
HORIZON_URI = "urn:scale:awsbench-horizon/v1"
FREE = {"get_time", "awsbench_control"}
SRE_TOOLS = {"wait", "aws_cli", "aws_query", "fleet_exec", "chat_post", "tickets_update", "pager_ack", "pager_page",
             "wiki_get", "status_create", "status_update", "mail_search", "mail_forward", "tickets_comment"}


def oracle_steps(gen):
    """The T01 reference path, adapted to AwsBench's tool names; ``gen`` is the plan the restore applies."""
    return [
        ("pager_ack", {"id": "PG-77052"}),
        ("tickets_get", {"key": "OPS-4452"}),
        ("tickets_update", {"key": "OPS-4452", "fields": {"severity": "SEV1", "assignee": "sre-primary"}}),
        ("chat_post", {"channel": "#inc-4452", "text": "I'm IC (first responder). SEV1 declared on OPS-4452. "
                                                         "Checkout success ~4% since 02:31. Investigating; updates here "
                                                         "at least every 30 min."}),
        ("aws_cli", {"command": "aws route53 list-hosted-zones-by-name --dns-name internal.vantleaf.io"}),
        ("aws_cli", {"command": f"aws route53 list-resource-record-sets --hosted-zone-id {{zone_id}} "
                                f"--start-record-name {LEDGER} --max-items 12"}),
        ("pager_page", {"schedule": "platform", "urgency": "high",
                        "title": f"OPS-4452 SEV1: {LEDGER} has no records, endpointd enactors halted; running EPD-RB-7",
                        "details": "Checkout ~4% since 02:31. Channel #inc-4452, ticket OPS-4452."}),
        ("status_create", {"name": "Checkout errors", "status": "investigating", "impact": "major",
                           "components": {"checkout": "major_outage"},
                           "body": "Some card payments are failing at checkout. We are investigating."}),
        ("wiki_get", {"page": "OPS/EPD-RB-7 endpointd: endpoint empty or enactors halted"}),
        ("fleet_exec", {"host": "endpointd-enactor-b", "cmd": "epctl status"}),
        ("fleet_exec", {"host": "endpointd-enactor-b", "cmd": "epctl pause all"}),
        ("chat_post", {"channel": "#inc-4452", "text": "CHANGE: paused endpointd enactors and planner (epctl pause all) "
                                                         "| why: EPD-RB-7 step 1 | undo: epctl resume | auth: PF 4.1(c)"}),
        ("fleet_exec", {"host": "endpointd-enactor-b", "cmd": "epctl plans list --since 48170"}),
        ("fleet_exec", {"host": "endpointd-enactor-b",
                        "cmd": f"epctl plan diff 48199 --live-fleet --endpoint {LEDGER}"}),
        ("aws_cli", {"command": "aws ssm put-parameter --name /vl/prod/webhooks-dispatcher/max_concurrency "
                                "--value 4 --type String --overwrite"}),
        ("fleet_exec", {"host": "endpointd-enactor-b",
                        "cmd": f"epctl apply --gen {gen} --endpoint {LEDGER} --manual --reason OPS-4452"}),
        ("chat_post", {"channel": "#inc-4452", "text": f"CHANGE: webhooks-dispatcher max_concurrency 4; restored {LEDGER} "
                                                         f"from plan {gen} with epctl apply | why: EPD-RB-7, LEDG-RB-2 | "
                                                         "undo: max_concurrency 25 | auth: IP 3.4, PF 4.1(f)"}),
        ("wait", {"until": "03:03:10"}),
        ("fleet_exec", {"host": "core-a-01", "cmd": f"dig +short {LEDGER}"}),
        ("fleet_exec", {"host": "core-b-01", "cmd": f"dig +short {LEDGER}"}),
        ("mail_search", {"query": "Harbor"}),
        ("mail_forward", {"id": "{callum_mail}", "to": ["owen.gallagher@vantleaf.com"],
                          "note": "Harbor & Pine (mch_20871) escalation from Callum Reyes, for Merchant Success."}),
        ("status_update", {"id": "{status_id}", "status": "monitoring",
                           "body": "A fix is in place and checkout is recovering. We are monitoring."}),
        ("chat_post", {"channel": "#inc-4452", "text": "Records restored, checkout recovering. Next update by 03:40."}),
        ("wait", {"minutes": 30}),
        ("chat_post", {"channel": "#inc-4452", "text": "Checkout stable. Dispatcher held at max_concurrency 4."}),
        ("tickets_comment", {"key": "OPS-4452", "body": (
            "Handoff: endpointd race (PLAT-412): enactor-b applied stale 48177 after enactor-c applied 48199; c's "
            "cleanup deleted 48177 and its records, leaving ledger.internal empty and the enactors halted. Restored "
            f"from {gen} (last verify=ok), not 48177. Enactors and planner stay paused until PLAT-412 is fixed and "
            "Kwame Asante or Daniel Brandt approves a resume. Dispatcher max_concurrency 4 shed the reconnect storm. "
            "Negative DNS caching delayed recovery ~15 min. Follow-ups: PLAT-412, alarm on an empty endpoint.")}),
    ]


def fill(args, found):
    text = json.dumps(args)
    for key, value in found.items():
        text = text.replace("{" + key + "}", str(value))
    return json.loads(text)


def harvest(tool, result, found):
    """Ids later steps need, read from earlier results."""
    blob = json.dumps(result)
    if tool == "aws_cli" and "zone_id" not in found:
        for zone in (result.get("HostedZones") or result.get("output", {}).get("HostedZones") or []):
            found["zone_id"] = zone.get("Id", "").split("/")[-1]
    if tool == "mail_search":
        for item in result.get("results") or result.get("messages") or result.get("items") or []:
            if "callum" in json.dumps(item).lower():
                found["callum_mail"] = item.get("id")
                break
    if tool == "status_create":
        found["status_id"] = result.get("id") or (result.get("incident") or {}).get("id") or found.get("status_id")
    return blob


async def play(gw, steps, check_clock=True):
    found, problems = {}, []
    for tool, args in steps:
        before = await gw.now()
        result = await gw.call(tool, fill(args, found))
        after = await gw.now()
        harvest(tool, result, found)
        clock = (result.get("_clock") or {}).get("now")
        status = "ERR" if "_error" in result else "ok"
        print(f"  {after.isoformat()}  {status:3} {tool} {clock or ''} {result.get('_error', '')[:120]}")
        if check_clock and tool not in FREE and "_error" not in result and after <= before:
            problems.append(f"clock did not advance on {tool} ({before} -> {after})")
        if "_clock" not in result and "_error" not in result:
            problems.append(f"{tool} result has no _clock banner")
    return problems


async def finish(gw):
    """End as the hook would on DONE, run the horizon twice (idempotent), grade."""
    print("  harness end:", await gw.call("awsbench_control", {"op": "end", "reason": "done"}, role="harness"))
    first = await gw.aws_extension(HORIZON_URI, {}, method="run")
    second = await gw.aws_extension(HORIZON_URI, {}, method="run")
    print("  horizon:", json.dumps(first)[:200], "| again:", json.dumps(second)[:120])
    aws, ws = await gw.exports()
    graded = load_grade().grade_from_inputs(aws, ws)
    for r in graded["rows"] + graded["gates"]:
        print(f"  {r['id']:4} {r['result']!s:5} {r['detail'][:140]}")
    print("  D12:", graded["d12"]["description"])
    return graded


async def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--gateway", required=True)
    parser.add_argument("--mode", choices=("oracle", "nothing", "trap"), default="oracle")
    parser.add_argument("--header", action="append", default=[], help="NAME=VALUE sent with every request")
    args = parser.parse_args(argv)
    gw = Gateway(args.gateway, dict(h.split("=", 1) for h in args.header))
    problems = []
    try:
        card = await gw.card()
        print("card:", card.get("name"), [c.get("name") for c in card.get("children") or []])
        sre = {t["name"] for t in await gw.tools("sre-primary")}
        harness = {t["name"] for t in await gw.tools("harness")}
        problems += [f"sre-primary lacks {sorted(SRE_TOOLS - sre)}"] if SRE_TOOLS - sre else []
        problems += ["sre-primary can see awsbench_control"] if "awsbench_control" in sre else []
        problems += ["harness lacks awsbench_control"] if "awsbench_control" not in harness else []
        print(f"tools: sre-primary {len(sre)}, harness {sorted(harness)}; clock {await gw.now()}")
        if args.mode != "nothing":
            problems += await play(gw, oracle_steps(48199 if args.mode == "oracle" else 48177))
        graded = await finish(gw)
        score = graded["d12"]["score"]
        expected = {"oracle": score >= 0.9 and graded["d12"]["result"], "nothing": score <= 0.1,
                    "trap": score <= 0.2 and not graded["d12"]["result"]}[args.mode]
        if not expected:
            problems.append(f"{args.mode}: unexpected D12 {score} (pass={graded['d12']['result']})")
    finally:
        await gw.close()
    for problem in problems:
        print("PROBLEM:", problem)
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
