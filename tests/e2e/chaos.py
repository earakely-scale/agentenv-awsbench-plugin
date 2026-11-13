"""Chaos checks against a deployed AwsBench env (tasks/e2e-env.json with --keep), each with the outcome it expects.

    python tests/e2e/chaos.py --gateway http://127.0.0.1:<port>                 # all but budget and aws_restart
    python tests/e2e/chaos.py --gateway ... --only budget --only aws_restart    # a fresh env: spends it

Scenarios: concurrent (calls serialize, the clock advances by their summed cost), malformed (bad args give an error
result, the server stays up), unknown (an unknown tool or one hidden from the role is refused, uncharged), restart
(docker restart of the workspace container mid-episode; its calls are then refused, uncharged, naming the lost
records), window (waits past window_end clamp to it, then charged calls are refused), horizon (running it twice is
idempotent), budget (call 251 is refused with the exhaustion message and is not charged), inject (aws_cli refuses shell operators, file:// values and
`aws configure`, keeps its own endpoint and the role's profiles, and stores http:// values without fetching them),
aws_restart (docker restart of the aws container: charged calls are refused, uncharged, naming the lost state).

Tool errors come back either as MCP errors or as a JSON body with "error"; both count. The containers are found from
the gateway's port, so other envs on the host are never touched. Run aws_restart last: it ends the env's usefulness.
"""
import argparse
import asyncio
import subprocess
import sys
import time
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from gw import Gateway

HORIZON_URI = "urn:scale:awsbench-horizon/v1"
READ = ("tickets_get", {"key": "OPS-4452"})


def err(result):
    return str(result.get("_error") or result.get("error") or "")


def container(gateway, service):
    """The compose container of `service` in the env whose gateway publishes this URL's port."""
    port = gateway.rstrip("/").rsplit(":", 1)[-1]
    project = subprocess.run(["docker", "ps", "--filter", f"publish={port}", "--format",
                              '{{.Label "com.docker.compose.project"}}'], capture_output=True, text=True,
                             check=True).stdout.split()[0]
    return f"{project}-{service}-1"


async def status(gw):
    return await gw.call("awsbench_control", {"op": "status"}, role="harness")


async def concurrent(gw):
    before, used = await gw.now(), (await status(gw)).get("calls_used", 0)
    results = await asyncio.gather(*(gw.call(*READ) for _ in range(8)))
    after, used_after = await gw.now(), (await status(gw)).get("calls_used", 0)
    calls = sorted((r.get("_clock") or {}).get("call") or 0 for r in results)
    ok = not any(err(r) for r in results) and len(set(calls)) == 8 and used_after - used == 8 \
        and after - before >= timedelta(seconds=8 * 20)
    return ok, f"call numbers {calls}; clock +{after - before}; calls_used +{used_after - used}"


async def malformed(gw):
    bad = [await gw.call("fleet_exec", {}), await gw.call("aws_cli", {"cmd": 42}),
           await gw.call("wait", {"until": "not-a-time"}), await gw.call("chat_post", {"channel": "#nope", "text": ""})]
    healthy = not err(await gw.call(*READ))
    return all(err(r) for r in bad) and healthy, f"errors {[bool(err(r)) for r in bad]}; healthy {healthy}"


async def unknown(gw):
    used = (await status(gw)).get("calls_used", 0)
    missing = await gw.raw_call("no_such_tool", {})
    hidden = await gw.raw_call("awsbench_control", {"op": "end"})
    used_after = (await status(gw)).get("calls_used", 0)
    ok = missing.status_code >= 400 and hidden.status_code == 403 and used_after == used
    return ok, f"unknown -> {missing.status_code}, hidden -> {hidden.status_code}, charged {used_after - used}"


async def restart(gw):
    """The workspace keeps its records in memory, so a restart loses them: calls must say so, uncharged."""
    return await _restart(gw, "workspace", ("chat_read", {"channel": "#inc-4452"}))


async def aws_restart(gw):
    return await _restart(gw, "aws", ("aws_cli", {"command": "aws route53 list-hosted-zones"}))


async def _restart(gw, service, probe):
    name = await asyncio.to_thread(container, gw.url, service)
    used = (await status(gw)).get("calls_used", 0)
    await asyncio.to_thread(subprocess.run, ["docker", "restart", name], check=True, capture_output=True)
    deadline, result = time.monotonic() + 90, {"_status": 0}
    while time.monotonic() < deadline:
        result = await gw.call(*probe)
        if "_status" not in result:
            break
        await asyncio.sleep(3)
    used_after = (await status(gw)).get("calls_used", 0)
    ok = "no scenario loaded" in err(result) and (service == "aws" or used_after == used)
    return ok, f"{name} after restart: {err(result)[:90] or str(result)[:90]}; charged {used_after - used}"


async def inject(gw):
    refused = ["aws route53 list-hosted-zones | cat /etc/passwd", "aws route53 list-hosted-zones; id",
               "aws route53 list-hosted-zones --max-items `id`", "aws route53 list-hosted-zones --max-items $(id)",
               "aws ssm put-parameter --name /vl/prod/chaos --type String --overwrite --value file:///etc/hostname",
               "aws route53 list-hosted-zones --cli-input-json file:///etc/hostname", "aws configure list"]
    codes = [(await gw.call("aws_cli", {"command": c})).get("exit_code") for c in refused]
    zones = await gw.call("aws_cli", {"command": "aws route53 list-hosted-zones --endpoint-url http://169.254.169.254"})
    other = await gw.call("aws_cli", {"command": "aws route53 list-hosted-zones --profile vl-corp/Admin"})
    url = "http://workspace:18765/awsbench/inbox"
    await gw.call("aws_cli", {"command": f"aws ssm put-parameter --name /vl/prod/chaos --type String --overwrite "
                                         f"--value {url}"})
    stored = ((await gw.call("aws_cli", {"command": "aws ssm get-parameter --name /vl/prod/chaos"})).get("output")
              or {}).get("Parameter", {}).get("Value")
    ok = codes == [2] * len(refused) and bool((zones.get("output") or {}).get("HostedZones")) \
        and "AccessDenied" in other.get("stderr", "") and stored == url
    return ok, f"refused exit codes {codes}; zones listed {bool((zones.get('output') or {}).get('HostedZones'))}; other profile denied; http value stored {stored!r:.60}"


async def window(gw):
    last = {}
    for _ in range(12):
        last = await gw.call("wait", {"minutes": 30})
        if err(last) or last.get("reason") == "window_end":
            break
    refused = await gw.call("aws_cli", {"command": "aws route53 list-hosted-zones"})
    st = await status(gw)
    ok = last.get("reason") == "window_end" and "window closed" in err(refused) and st.get("now", "")[:16] == \
        (st.get("window_end") or "")[:16]
    return ok, f"last wait {last.get('reason')} at {last.get('now')}; after it: {err(refused)[:80] or 'not refused'}"


async def horizon(gw):
    first = await gw.aws_extension(HORIZON_URI, {}, method="run")
    t1 = await gw.now()
    second = await gw.aws_extension(HORIZON_URI, {}, method="run")
    t2 = await gw.now()
    return t1 == t2, f"clock {t1} then {t2}; second answer {str(second)[:120]} (first {str(first)[:80]})"


async def budget(gw):
    st, n, refusal = await status(gw), 0, None
    while n < 300:
        result = await gw.call(*READ)
        n += 1
        if "budget exhausted" in err(result).lower():
            refusal = err(result)
            break
    t1 = await gw.now()
    again = await gw.call(*READ)
    t2 = await gw.now()
    used = (await status(gw)).get("calls_used")
    ok = refusal is not None and used == 250 and t1 == t2 and bool(err(again))
    return ok, f"refused after {n} calls ({st.get('calls_used')} used before): {refusal!r:.90}; calls_used {used}"


SCENARIOS = ("concurrent", "malformed", "unknown", "inject", "window", "restart", "horizon")


async def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--gateway", required=True)
    parser.add_argument("--only", action="append", choices=SCENARIOS + ("budget", "aws_restart"))
    args = parser.parse_args(argv)
    gw = Gateway(args.gateway)
    failed = []
    try:
        await gw.tools("sre-primary")
        for name in args.only or SCENARIOS:
            fn = globals()[name]
            try:
                ok, detail = await fn(gw)
            except Exception as e:
                ok, detail = False, f"raised {e!r}"
            print(f"{'PASS' if ok else 'FAIL'} {name}: {detail}")
            if not ok:
                failed.append(name)
    finally:
        await gw.close()
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
