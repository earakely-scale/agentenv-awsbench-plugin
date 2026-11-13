"""A small client for a deployed AwsBench env's gateway: /step tool calls as a role, the clock, the aws server's
extensions through /svc/mcp-aws, and the exports the grade reads."""
import importlib.util
import json
from datetime import datetime
from pathlib import Path

import httpx
from agentenv_protocol import client as protocol

ARTIFACTS = Path(__file__).resolve().parents[2] / "src/agentenv_awsbench/bundles/awsbench/artifacts"
SYNONYMS = {
    "cmd": ("cmd", "command", "args", "argv"), "command": ("command", "cmd"),
    "host": ("selector", "host", "hosts", "target"), "id": ("id", "page_id", "message_id", "incident_id"),
    "key": ("key", "ticket", "ticket_key", "id"), "page": ("page", "title", "page_id", "slug", "path"),
    "channel": ("channel", "channel_id", "channel_name"), "text": ("text", "message", "body"),
    "to": ("to", "recipients", "user", "to_user"), "schedule": ("schedule", "to", "target", "service"),
    "title": ("title", "summary", "subject"), "details": ("details", "body", "description"),
    "body": ("body", "text", "comment", "message"), "query": ("query", "q", "text"),
}


def load_grade():
    spec = importlib.util.spec_from_file_location("t01_grade_e2e", ARTIFACTS / "t01-grade" / "verify.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class Gateway:
    def __init__(self, url, headers=None, timeout=900):
        self.url = url.rstrip("/")
        self.headers = headers or {}
        self.http = httpx.AsyncClient(timeout=timeout, headers=self.headers)
        self.schemas = {}

    async def close(self):
        await self.http.aclose()

    async def card(self):
        resp = await self.http.get(f"{self.url}/.well-known/agent-env.json")
        resp.raise_for_status()
        return resp.json()

    async def tools(self, role):
        resp = await self.http.post(f"{self.url}/step", json={"action": "list_tools"},
                                    headers={"AgentEnv-Role": role})
        resp.raise_for_status()
        tools = resp.json()["tools"]
        self.schemas.update({t["name"]: t.get("parameters") or {} for t in tools})
        return tools

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

    async def raw_call(self, tool, args, role="sre-primary"):
        return await self.http.post(f"{self.url}/step", headers={"AgentEnv-Role": role},
                                    json={"action": "call_tool", "tool_name": tool, "arguments": args})

    async def call(self, tool, args=None, role="sre-primary"):
        """A tool call's result: the parsed JSON of its single text block, with ``_error`` set for an error result."""
        resp = await self.raw_call(tool, self.adapt(tool, args or {}), role)
        if resp.status_code >= 400:
            return {"_error": f"HTTP {resp.status_code}: {resp.text[:300]}", "_status": resp.status_code}
        body = resp.json()
        text = "".join(b.get("text", "") for b in body.get("content") or [] if isinstance(b, dict))
        try:
            result = json.loads(text) if text else {}
        except json.JSONDecodeError:
            result = {"text": text}
        if not isinstance(result, dict):
            result = {"result": result}
        if body.get("isError"):
            result["_error"] = text[:500]
        return result

    async def now(self):
        resp = await self.http.get(f"{self.url}/clock/time")
        resp.raise_for_status()
        return datetime.fromisoformat(resp.json()["virtual_time"].replace("Z", "+00:00"))

    async def aws_extension(self, uri, params=None, method=None, timeout=900):
        base = f"{self.url}/svc/mcp-aws"
        card = await protocol.get_card(base, timeout=60)
        return await protocol.invoke_extension(base, card, uri, params or {}, timeout=timeout, method=method,
                                               headers=self.headers)

    async def exports(self):
        grade = load_grade()
        return await grade.fetch(self.url)
