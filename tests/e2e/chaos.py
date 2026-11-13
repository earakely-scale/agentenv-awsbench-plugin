"""Chaos checks against a deployed AwsBench env (tasks/e2e-env.json with --keep), each with the outcome it expects.

    python tests/e2e/chaos.py --gateway http://127.0.0.1:<port>                 # all but budget, in order
    python tests/e2e/chaos.py --gateway ... --only budget                       # needs a fresh env: spends all calls

Scenarios: concurrent (calls serialize, the clock advances by their summed cost), malformed (bad args give an error
result, the server stays up), unknown (an unknown tool or one hidden from the role is refused, uncharged), restart
(docker restart of the workspace container mid-episode; calls work again within 90 s), window (waits past window_end
clamp to it, then charged calls are refused), horizon (running it twice is idempotent), budget (call 251 is refused
with the exhaustion message and is not charged).
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


async def status(gw):
    return await gw.call("awsbench_control", {"op": "status"}, role="harness")


async def concurrent(gw):
    before, used = await gw.now(), (await status(gw)).get("calls_used", 0)
    results = await asyncio.gather(*(gw.call(*READ) for _ in range(8)))
    after, used_after = await gw.now(), (await status(gw)).get("calls_used", 0)
    calls = sorted((r.get("_clock") or {}).get("call") or 0 for r in results)
    ok = all("_error" not in r for r in results) and len(set(calls)) == 8 and used_after - used == 8 \
        and after - before >= timedelta(seconds=8 * 20)
    return ok, f"call numbers {calls}; clock +{after - before}; calls_used +{used_after - used}"


async def malformed(gw):
    bad = [await gw.call("fleet_exec", {}), await gw.call("aws_cli", {"cmd": 42}),
           await gw.call("wait", {"until": "not-a-time"}), await gw.call("chat_post", {"channel": "#nope", "text": ""})]
    healthy = "_error" not in await gw.call(*READ)
    return all("_error" in r for r in bad) and healthy, f"errors {[bool(r.get('_error')) for r in bad]}; healthy {healthy}"


async def unknown(gw):
    used = (await status(gw)).get("calls_used", 0)
    missing = await gw.raw_call("no_such_tool", {})
    hidden = await gw.raw_call("awsbench_control", {"op": "end"})
    used_after = (await status(gw)).get("calls_used", 0)
    ok = missing.status_code >= 400 and hidden.status_code == 403 and used_after == used
    return ok, f"unknown -> {missing.status_code}, hidden -> {hidden.status_code}, charged {used_after - used}"


async def restart(gw, container):
    name = container or (await asyncio.to_thread(
        subprocess.run, ["docker", "ps", "--format", "{{.Names}}", "--filter", "name=workspace"],
        capture_output=True, text=True, check=True)).stdout.split()[0]
    before = await gw.call("chat_read", {"channel": "#inc-4452"})
    await asyncio.to_thread(subprocess.run, ["docker", "restart", name], check=True, capture_output=True)
    deadline, result = time.monotonic() + 90, {"_error": "never answered"}
    while time.monotonic() < deadline:
        result = await gw.call("chat_read", {"channel": "#inc-4452"})
        if "_error" not in result:
            break
        await asyncio.sleep(3)
    kept = len(str(result)) >= len(str(before)) * 0.5
    return "_error" not in result, f"{name} answered again; workspace state kept: {kept} (in-memory state is lost on restart)"


async def window(gw):
    last = {}
    for _ in range(12):
        last = await gw.call("wait", {"minutes": 30})
        if last.get("_error") or last.get("reason") == "window_end":
            break
    refused = await gw.call(*READ)
    st = await status(gw)
    ok = last.get("reason") == "window_end" and "_error" in refused and st.get("now", "")[:16] == \
        (st.get("window_end") or "")[:16]
    return ok, f"last wait {last.get('reason')} at {last.get('now')}; after it: {refused.get('_error', 'not refused')[:80]}"


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
        if "budget exhausted" in str(result.get("_error", "")).lower():
            refusal = result["_error"]
            break
    t1 = await gw.now()
    again = await gw.call(*READ)
    t2 = await gw.now()
    used = (await status(gw)).get("calls_used")
    ok = refusal is not None and used == 250 and t1 == t2 and "_error" in again
    return ok, f"refused after {n} calls ({st.get('calls_used')} used before): {refusal!r:.90}; calls_used {used}"


SCENARIOS = ("concurrent", "malformed", "unknown", "restart", "window", "horizon")


async def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--gateway", required=True)
    parser.add_argument("--only", action="append", choices=SCENARIOS + ("budget",))
    parser.add_argument("--container", help="the workspace container's name (default: found with docker ps)")
    args = parser.parse_args(argv)
    gw = Gateway(args.gateway)
    failed = []
    try:
        await gw.tools("sre-primary")
        for name in args.only or SCENARIOS:
            fn = globals()[name]
            try:
                ok, detail = await (fn(gw, args.container) if name == "restart" else fn(gw))
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
