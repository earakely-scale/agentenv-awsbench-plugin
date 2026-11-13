"""The seven apps over one Workspace, the scenario loader and the world ops the aws server applies."""

from collections.abc import Callable
from pathlib import Path
from zoneinfo import ZoneInfo

import yaml

from . import chat, mail, pager, repo, status, tickets, wiki
from .base import ToolError, Workspace

SCENARIOS = Path(__file__).resolve().parent.parent / "scenarios"
APPS = {"chat": chat, "tickets": tickets, "wiki": wiki, "repo": repo, "mail": mail, "pager": pager, "status": status}

WORLD_OPS: dict[str, Callable] = {
    "chat.post_as": chat.post_as, "chat.dm_as": chat.dm_as, "mail.deliver": mail.deliver,
    "pager.page_as": pager.page_as, "pager.ack_as": pager.ack_as, "tickets.comment_as": tickets.comment_as,
    "tickets.update_as": tickets.update_as, "tickets.create_as": tickets.create_as,
}


def _resolve_refs(node, folder: Path):
    """Inline every `<key>_ref: <file>` as `<key>: <file's text>`."""
    if isinstance(node, list):
        return [_resolve_refs(n, folder) for n in node]
    if not isinstance(node, dict):
        return node
    out = {}
    for k, v in node.items():
        if k.endswith("_ref") and isinstance(v, str):
            out[k[:-4]] = (folder / v).read_text(encoding="utf-8")
        else:
            out[k] = _resolve_refs(v, folder)
    return out


def read_scenario(folder: Path) -> dict[str, dict]:
    """The scenario's documents by app: <app>.yaml, plus one repos list from repo/*.yaml."""
    if not folder.is_dir():
        raise ToolError(f"no scenario folder {folder}")
    docs = {p.stem: _resolve_refs(yaml.safe_load(p.read_text(encoding="utf-8")) or {}, folder)
            for p in sorted(folder.glob("*.yaml"))}
    repos = [_resolve_refs(yaml.safe_load(p.read_text(encoding="utf-8")), folder)
             for p in sorted((folder / "repo").glob("*.yaml"))]
    if repos:
        docs.setdefault("repo", {}).setdefault("repos", []).extend(repos)
    return docs


def load_docs(ws: Workspace, name: str, docs: dict[str, dict], charged_roles: list[str] | None = None) -> dict:
    ws.reset()
    world = docs.get("world", {})
    ws.scenario = name
    if world.get("timezone"):
        ws.tz = ZoneInfo(world["timezone"])
    ws.start = ws.when(world["start"]) if world.get("start") else None
    ws.now = ws.start
    ws.identities = {role: dict(ids) for role, ids in (world.get("identities") or {}).items()}
    ws.charged_roles = list(charged_roles or world.get("charged_roles") or ["sre-primary"])
    t0 = ws.clock()
    chat.load(ws, {"users": world.get("users", []), **docs.get("chat", {})}, t0)
    for app, module in APPS.items():
        if app != "chat":
            module.load(ws, docs.get(app, {}), t0)
    return {"scenario": name, "start": ws.iso(ws.start), "records": sum(len(v) for a in ws.store.export().values()
                                                                       for v in a.values())}


def load_scenario(ws: Workspace, name: str, charged_roles: list[str] | None = None,
                  root: Path | None = None) -> dict:
    return load_docs(ws, name, read_scenario((root or SCENARIOS) / name), charged_roles)


def apply(ws: Workspace, op: str, args: dict, at) -> dict:
    """A world op at virtual time `at`: {ok, id, deliveries}."""
    if op not in WORLD_OPS:
        raise ToolError(f"unknown op {op!r}; ops: scenario.load, {', '.join(WORLD_OPS)}")
    t = ws.at(ws.when(at) if at else ws.clock())
    rid, deliveries = WORLD_OPS[op](ws, dict(args or {}), t)
    ws.store.log({"t": ws.iso(t), "kind": "world", "op": op, "args": args, "id": rid,
                  "deliveries": [d["seq"] for d in deliveries]})
    return {"ok": True, "id": rid, "deliveries": deliveries}
