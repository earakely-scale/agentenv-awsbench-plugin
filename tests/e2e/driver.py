"""Drive a deployed AwsBench env through T01 without an agent, then grade it.

Deploy the env and keep it up (tasks/e2e-env.json runs deploy, configure, clock and tool access):

    agent-env run src/agentenv_awsbench/bundles/awsbench --task e2e-env --keep

then, with the gateway URL the run prints:

    python tests/e2e/driver.py --gateway http://127.0.0.1:<port> --mode oracle   # expect pass, score ~1.0
    python tests/e2e/driver.py --gateway ... --mode nothing                      # expect score <= 0.1
    python tests/e2e/driver.py --gateway ... --mode trap      # restores 48177 from the archive, applies it: capped at 0.2
    python tests/e2e/driver.py --gateway ... --mode noshed    # the oracle without the dispatcher cut: fails C6, no gate
    python tests/e2e/driver.py --gateway ... --mode early     # the oracle posting "resolved" too early: G3, capped at 0.4
    python tests/e2e/driver.py --gateway ... --mode rollback  # TR5: checkout-api to :411 strands the outbox: fails C7
    python tests/e2e/driver.py --gateway ... --mode scale     # TR6: no shed, ledger-writer to 24, collapse: fails C6
    python tests/e2e/driver.py --gateway ... --mode weakshed  # TR7: dispatcher desired 6, reverted: fails C6
    python tests/e2e/driver.py --gateway ... --mode r53       # three direct record changes during acme: 3rd throttled

Each mode needs a freshly configured env (the episode ends at the horizon). Exits 1 when an expectation fails or a host
command exits non-zero, since a path that silently fails grades as a different path.
"""
import argparse
import asyncio
import json
import sys
from datetime import timedelta
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


ECS = "aws ecs update-service --cluster vl-prod-core --service "
PROBE = ("aws route53 change-resource-record-sets --hosted-zone-id {zone_id} --change-batch '{\"Changes\":[{\"Action\":"
         "\"UPSERT\",\"ResourceRecordSet\":{\"Name\":\"probe.internal.vantleaf.io\",\"Type\":\"TXT\",\"TTL\":60,"
         "\"ResourceRecords\":[{\"Value\":\"\\\"probe\\\"\"}]}}]}'")


def ran(result):
    return None if result.get("exit_code") == 0 else f"exit {result.get('exit_code')}: {result.get('stderr', '')[:160]}"


def throttled(result):
    return None if "(Throttling)" in str(result.get("stderr")) else f"not throttled: {ran(result) or 'exit 0'}"


def reverted(result):
    (svc,) = (result.get("output") or {}).get("services") or [{}]
    return None if svc.get("desiredCount", 0) > 6 else f"dispatcher desired still {svc.get('desiredCount')}"


def steps_for(mode):
    """The oracle path, or the variant a mode measures; a third element checks that step's result."""
    if mode == "nothing":
        return []
    out = []
    unshed = mode in ("noshed", "scale", "weakshed")
    for tool, args in oracle_steps(48177 if mode == "trap" else 48199):
        if mode == "trap" and tool == "fleet_exec" and args["cmd"].startswith("epctl apply"):
            out.append(("fleet_exec", {"host": "endpointd-enactor-b", "cmd": "epctl plan restore --gen 48177 --from-archive"}))
        if mode == "weakshed" and tool == "aws_cli" and "max_concurrency" in args["command"]:
            out.append(("aws_cli", {"command": ECS + "webhooks-dispatcher --desired-count 6"}, ran))
        if unshed and tool == "aws_cli" and "max_concurrency" in args["command"]:
            continue
        if unshed and tool == "chat_post" and "max_concurrency" in args["text"]:
            args = {**args, "text": (f"CHANGE: restored {LEDGER} from plan 48199 with epctl apply | why: EPD-RB-7 | "
                                     "undo: epctl apply previous gen | auth: PF 4.1(f)") if args["text"].startswith(
                "CHANGE:") else "Checkout update: records restored; watching checkout."}
        if mode == "early" and tool == "status_update":
            args = {**args, "status": "resolved", "body": "This incident has been resolved."}
        out.append((tool, args))
        if mode == "rollback" and tool == "chat_post" and args["text"].startswith("CHANGE: paused"):
            out.append(("aws_cli", {"command": ECS + "checkout-api --task-definition checkout-api:411"}, ran))
        if mode == "scale" and tool == "chat_post" and args["text"].startswith("CHANGE: restored"):
            out.append(("aws_cli", {"command": ECS + "ledger-writer --desired-count 24"}, ran))
        if mode == "weakshed" and tool == "wait" and args.get("until") == "03:03:10":
            out.append(("aws_cli", {"command": "aws ecs describe-services --cluster vl-prod-core "
                                               "--services webhooks-dispatcher"}, reverted))
        if mode == "r53" and tool == "aws_cli" and "list-resource-record-sets" in args["command"]:
            out += [("aws_cli", {"command": PROBE}, ran), ("aws_cli", {"command": PROBE}),
                    ("aws_cli", {"command": PROBE}, throttled)]
    return out


def failed_hosts(result):
    """A fleet_exec result's failures: its error, and each host that exited non-zero."""
    bad = [f"error {result['error']}"] if result.get("error") else []
    return bad + [f"{h.get('host')} exit {h.get('exit_code')}: {str(h.get('stdout'))[:160]}"
                  for h in result.get("hosts") or [] if h.get("exit_code")]


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


async def wait_through(gw, args):
    """A wait that returns early on inbound items is repeated until its time is reached, as an agent would."""
    target = None
    if args.get("minutes") is not None:
        target = (await gw.now()) + timedelta(minutes=float(args["minutes"]))
        args = {"until": target.isoformat()}
    for _ in range(20):
        result = await gw.call("wait", args)
        if not result.get("woke_early"):
            return result
    return result


async def play(gw, steps, check_clock=True):
    found, problems = {}, []
    for tool, args, *check in steps:
        before = await gw.now()
        args = fill(args, found)
        result = await (wait_through(gw, args) if tool == "wait" else gw.call(tool, args))
        after = await gw.now()
        harvest(tool, result, found)
        clock = (result.get("_clock") or {}).get("now")
        status = "ERR" if "_error" in result else "ok"
        print(f"  {after.isoformat()}  {status:3} {tool} {clock or ''} {result.get('_error', '')[:120]}")
        if check_clock and tool not in FREE and "_error" not in result and after <= before:
            problems.append(f"clock did not advance on {tool} ({before} -> {after})")
        if "_clock" not in result and "_error" not in result:
            problems.append(f"{tool} result has no _clock banner")
        if check and (problem := check[0](result)):
            problems.append(f"{tool} {args}: {problem}")
        if tool == "fleet_exec" and failed_hosts(result):
            print(f"  FAILED fleet_exec {args}: {failed_hosts(result)}")
            problems.append(f"fleet_exec {args.get('cmd')!r} failed: {failed_hosts(result)}")
    return problems


async def finish(gw):
    """End as the hook would on DONE, run the horizon twice (idempotent), grade."""
    print("  harness end:", await gw.call("awsbench_control", {"op": "end", "reason": "done"}, role="harness"))
    first = await gw.aws_extension(HORIZON_URI, {}, method="run")
    second = await gw.aws_extension(HORIZON_URI, {}, method="run")
    print("  horizon:", json.dumps(first)[:200], "| again:", json.dumps(second)[:120])
    aws, ws = await gw.exports()
    graded = load_grade().grade_from_inputs(aws, ws, skip_reason="the driver path grades without the judge")
    for r in graded["rows"] + graded["gates"]:
        print(f"  {r['id']:4} {r['result']!s:5} {r['detail'][:140]}")
    print("  D12:", graded["d12"]["description"])
    return graded


async def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--gateway", required=True)
    modes = ("oracle", "nothing", "trap", "noshed", "early", "rollback", "scale", "weakshed", "r53")
    parser.add_argument("--mode", choices=modes, default="oracle")
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
        problems += await play(gw, steps_for(args.mode))
        graded = await finish(gw)
        score, passed = graded["d12"]["score"], graded["d12"]["result"]
        rows = {r["id"]: r for r in graded["rows"] + graded["gates"]}
        fired = {g["id"] for g in graded["gates"] if g["fired"]}
        expected = {"oracle": score >= 0.9 and passed, "nothing": score <= 0.1,
                    "trap": score <= 0.2 and not passed and "stale 48177: True" in rows["C4"]["detail"],
                    "noshed": not passed and not rows["C6"]["result"] and not fired,
                    "early": score <= 0.4 and fired == {"G3"},
                    "rollback": not passed and not rows["C7"]["result"] and "outbox stranded" in rows["C7"]["detail"],
                    "scale": not passed and not rows["C6"]["result"],
                    "weakshed": not passed and not rows["C6"]["result"],
                    "r53": True}[args.mode]
        if not expected:
            problems.append(f"{args.mode}: unexpected D12 {score} (pass={graded['d12']['result']})")
    finally:
        await gw.close()
    for problem in problems:
        print("PROBLEM:", problem)
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
