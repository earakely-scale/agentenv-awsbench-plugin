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

# @ADAPT@

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


# @DRIVER@


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
