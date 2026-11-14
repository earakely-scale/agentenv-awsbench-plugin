"""Play one T01 path through the env's MCP tools, as the agent reaches them (http://mcp:18765/mcp as sre-primary).

    python3 /solution/play.py MODE          # oracle (canonical), alt (another valid path), or a QC variant

The paths are tests/e2e/driver.py's steps_for(MODE), serialised to paths.json by tools/export_harbor.py, and the
functions below the marker are driver.py's and gw.py's own source, copied by the generator: the same play loop, the
same argument adaptation, the same checks. Only the transport differs: MCP tools/call instead of the gateway's /step
(both reach the same proxied tools as the same role), and "now" from the free get_time tool instead of /clock/time
(the same clock). Exits 1 when a step's check fails or a host command exits non-zero, as the driver does.
"""
import asyncio
import json
import sys
from datetime import datetime, timedelta
from pathlib import Path

try:
    from mcp.client.streamable_http import streamablehttp_client as _http_client
except ImportError:  # newer mcp names it without the underscore-free alias
    from mcp.client.streamable_http import streamable_http_client as _http_client
from mcp import ClientSession

HERE = Path(__file__).resolve().parent
PATHS = json.loads((HERE / "paths.json").read_text())
URL = PATHS["mcp_url"]
SYNONYMS = PATHS["synonyms"]
LEDGER = PATHS["ledger"]
FREE = set(PATHS["free"])


class McpGateway:
    """The driver's Gateway, over MCP: call() returns the parsed JSON of a tool's single text block, `_error` set for
    an error result; now() is the env clock."""

    def __init__(self, session, tools):
        self.session = session
        self.schemas = {t.name: t.inputSchema or {} for t in tools}

    def adapt(self, tool, args):
        """Rename an oracle argument to the name the tool's schema uses, when they differ."""
        props = (self.schemas.get(tool) or {}).get("properties") or {}
        if not props:
            return args
        out = {}
        for key, value in args.items():
            name = key if key in props else next((s for s in SYNONYMS.get(key, ()) if s in props and s not in out), None)
            if name is not None:
                out[name] = value
        return out

    async def call(self, tool, args=None, role=None):
        result = await self.session.call_tool(tool, self.adapt(tool, args or {}))
        text = "".join(getattr(block, "text", "") or "" for block in result.content or [])
        try:
            parsed = json.loads(text) if text else {}
        except json.JSONDecodeError:
            parsed = {"text": text}
        if not isinstance(parsed, dict):
            parsed = {"result": parsed}
        if result.isError:
            parsed["_error"] = text[:500]
        return parsed

    async def now(self):
        result = await self.session.call_tool("get_time", {})
        text = "".join(getattr(block, "text", "") or "" for block in result.content or [])
        return datetime.fromisoformat(json.loads(text)["current_time"].replace("Z", "+00:00"))


# --- copied from tests/e2e/driver.py by tools/export_harbor.py ---

def ran(result):
    return None if result.get("exit_code") == 0 else f"exit {result.get('exit_code')}: {result.get('stderr', '')[:160]}"


def throttled(result):
    return None if "(Throttling)" in str(result.get("stderr")) else f"not throttled: {ran(result) or 'exit 0'}"


def reverted(result):
    (svc,) = (result.get("output") or {}).get("services") or [{}]
    return None if svc.get("desiredCount", 0) > 6 else f"dispatcher desired still {svc.get('desiredCount')}"


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
    for _ in range(60):
        result = await gw.call("wait", args)
        if not result.get("woke_early"):
            return result
    return result


async def play(gw, steps, check_clock=True, harvest=harvest, skip_past=False):
    found, problems = {}, []
    for tool, args, *check in steps:
        if tool == "REPAIR":
            await t04_oracle.repair_by_hand(gw, found)
            continue
        before = await gw.now()
        args = fill(args, found)
        if skip_past and tool == "wait" and args.get("until") and len(args["until"]) <= 8 and before.astimezone(ET).strftime(
                "%H:%M:%S") >= args["until"].rjust(8, "0"):
            print(f"  {before.isoformat()}  skip wait until {args['until']} (already past)")
            continue
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


CHECKS = {"ran": ran, "throttled": throttled, "reverted": reverted}


async def main(mode):
    if mode not in PATHS["modes"]:
        raise SystemExit(f"unknown mode {mode!r}; one of {sorted(PATHS['modes'])}")
    steps = [(tool, args, *([CHECKS[check]] if check else [])) for tool, args, check in PATHS["modes"][mode]]
    async with _http_client(URL, timeout=600, sse_read_timeout=1800) as (read, write, *_):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = (await session.list_tools()).tools
            names = {t.name for t in tools}
            problems = [f"the agent's tools lack {sorted(set(PATHS['needed']) - names)}"] \
                if set(PATHS["needed"]) - names else []
            problems += ["the agent can see awsbench_control"] if "awsbench_control" in names else []
            gw = McpGateway(session, tools)
            print(f"mode {mode}: {len(steps)} steps, {len(names)} tools; clock {await gw.now()}", flush=True)
            problems += await play(gw, steps)
    for problem in problems:
        print("PROBLEM:", problem, flush=True)
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main(sys.argv[1] if len(sys.argv) > 1 else "oracle")))
