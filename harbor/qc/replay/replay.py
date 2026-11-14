"""Play one turn of a replay script (extract.py) against an env's MCP endpoint, call by call with the recorded
arguments, and record every result exactly as the endpoint returned it.

    python3 replay.py SCRIPT.json TURN MCP_URL OUT.jsonl [HEADER=VALUE ...]

Each line of OUT is {"i", "tool", "args", "is_error", "content": [the MCP content blocks]}. Calls run one at a time in
the script's order; a failed call is recorded and the replay goes on, as the agent's turn would.

The one id random by design that an agent carries from a result into later arguments is the Route 53 hosted zone id
(the AWS Emulator draws it when the scenario creates internal.vantleaf.io, the world's only zone). With the script's
`recorded_zone_ids`, once a result shows this env's zone id (`/hostedzone/<id>`), later arguments get it in place of the
recorded one (`sent_args` in OUT); the id seen is kept in zones.json beside OUT, for the next turn.
"""
import re
from pathlib import Path
import asyncio
import json
import sys

try:
    from mcp.client.streamable_http import streamablehttp_client as _http_client
except ImportError:
    from mcp.client.streamable_http import streamable_http_client as _http_client
from mcp import ClientSession


def block(b):
    d = b.model_dump(mode="json", exclude_none=True) if hasattr(b, "model_dump") else dict(b)
    d.pop("meta", None)
    d.pop("_meta", None)
    return d


ZONE = re.compile(r"/hostedzone/(Z[A-Z0-9]{8,32})")


async def play(script, turn, url, out, headers):
    calls = script["turns"][turn - 1]["calls"]
    recorded_zones = script.get("recorded_zone_ids") or []
    state = Path(out).parent / "zones.json"
    live = json.loads(state.read_text()).get("live") if state.exists() else None
    async with _http_client(url, headers=headers or None, timeout=600, sse_read_timeout=1800) as (read, write, *_):
        async with ClientSession(read, write) as session:
            await session.initialize()
            names = {t.name for t in (await session.list_tools()).tools}
            with open(out, "w") as f:
                for i, call in enumerate(calls, 1):
                    args = call["args"]
                    if live and recorded_zones:
                        text = json.dumps(args, ensure_ascii=False)
                        for z in recorded_zones:
                            text = text.replace(z, live)
                        args = json.loads(text)
                    if call["tool"] not in names:
                        row = {"i": i, "tool": call["tool"], "args": call["args"], "is_error": True,
                               "content": [{"type": "text", "text": f"<not among the {len(names)} tools listed>"}]}
                    else:
                        try:
                            r = await session.call_tool(call["tool"], args)
                            row = {"i": i, "tool": call["tool"], "args": call["args"], "is_error": bool(r.isError),
                                   "content": [block(b) for b in r.content or []]}
                        except Exception as e:  # a transport-level failure: recorded, the turn goes on
                            row = {"i": i, "tool": call["tool"], "args": call["args"], "is_error": True,
                                   "exception": repr(e)[:2000], "content": []}
                    if args != call["args"]:
                        row["sent_args"] = args
                    if not live:
                        found = ZONE.search(json.dumps(row.get("content"), ensure_ascii=False))
                        if found:
                            live = found.group(1)
                            state.write_text(json.dumps({"live": live}))
                    f.write(json.dumps(row, ensure_ascii=False) + "\n")
                    f.flush()
    print(f"turn {turn}: {len(calls)} calls played, {len(names)} tools listed", flush=True)


def main(argv):
    script, turn, url, out = json.loads(open(argv[0]).read()), int(argv[1]), argv[2], argv[3]
    headers = dict(h.split("=", 1) for h in argv[4:])
    asyncio.run(play(script, turn, url, out, headers))


if __name__ == "__main__":
    main(sys.argv[1:])
