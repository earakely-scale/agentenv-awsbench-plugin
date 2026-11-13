"""The `workspace` MCP server: chat, tickets, wiki, repo, mail, pager and status page for one incident.

Every tool answers one JSON text block with a `_clock` banner. A call by a charged role is metered by the `aws`
server (POST {aws}/awsbench/meter/begin before, /end after) so the workspace and AWS share one virtual clock; with
AWSBENCH_STANDALONE=1, or when the meter can't be reached, the call runs uncharged on the workspace's own clock and
the banner says so. The world writes through plain HTTP routes under /awsbench/."""

import base64
import json
import os
from collections.abc import Callable
from typing import Any

import httpx
import yaml
from agentenv_protocol import (
    AgentEnvEnvironment,
    DataPart,
    ToolContext,
    add_data,
    environment_card,
    extension,
    get_data,
    reset_data,
    tool,
)
from fastmcp import FastMCP
from starlette.requests import Request
from starlette.responses import JSONResponse

from .modules import apply as world_apply
from .modules import chat, load_docs, load_scenario, mail, pager, repo, status, tickets, wiki
from .modules.base import ToolError, Workspace, as_list
from .store import NotFound

COSTS = {"free": 0, "point_read": 20, "write": 45, "broad": 75}
READ_SUFFIXES = ("_get", "_read", "_search", "_history", "_log", "_oncall", "_list", "_list_channels", "_get_user")
CLOCK_URI = "urn:agentenv:clock/v1"


def _json(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"), default=str)


def result_summary(result: dict) -> dict:
    """What the world reads of a result (`{call.result.id}` in timeline actions): the record's id, else a ticket's
    key, at the top level or one level down (`{"page": {"id": ...}}`, `{"created": {"key": ...}}`), its error, and
    the start of its text."""
    def ident(d: dict):
        return d["id"] if d.get("id") is not None else d.get("key")

    rid = ident(result)
    if rid is None:
        rid = next((ident(v) for v in result.values() if isinstance(v, dict) and ident(v) is not None), None)
    return {"id": rid, "error": result.get("error"), "text": _json(result)[:500]}


def as_fields(value: dict[str, Any] | str) -> dict[str, Any] | str:
    """Ticket fields from the JSON text of an object, which models also send; anything else is left for
    tickets.update to refuse."""
    if not isinstance(value, str):
        return value
    try:
        parsed = json.loads(value)
    except ValueError:
        return value
    return parsed if isinstance(parsed, dict) else value


def cost_class(tool_name: str) -> str:
    return "point_read" if tool_name.endswith(READ_SUFFIXES) else "write"


NO_SCENARIO = ("The workspace has no scenario loaded: it restarted or was reset and lost its records. Nothing more "
               "can be done in this environment; reply DONE.")


def _port() -> int:
    return int(os.environ.get("MCP_PORT", "18765"))


@environment_card(name="workspace")
class WorkspaceEnv(AgentEnvEnvironment):
    def __init__(self, aws_url: str | None = None, standalone: bool | None = None,
                 transport: httpx.AsyncBaseTransport | None = None):
        self.ws = Workspace()
        self.aws_url = (aws_url or os.environ.get("AWSBENCH_AWS_URL") or f"http://aws:{_port()}").rstrip("/")
        self.standalone = os.environ.get("AWSBENCH_STANDALONE") == "1" if standalone is None else standalone
        self.http = httpx.AsyncClient(timeout=httpx.Timeout(300, connect=5), transport=transport)
        self.gateway: str | None = None
        self.banner_seq = 0
        self.local_calls = 0

    def create_app(self) -> FastMCP:
        app = FastMCP("workspace")
        app.custom_route("/awsbench/world/apply", methods=["POST"])(self.apply_route)
        app.custom_route("/awsbench/inbox", methods=["GET"])(self.inbox_route)
        app.custom_route("/awsbench/health", methods=["GET"])(self.health_route)
        return self.mount(app)

    # internal routes
    async def apply_route(self, request: Request) -> JSONResponse:
        try:
            body = await request.json()
            return JSONResponse(self.apply(body.get("op", ""), body.get("args") or {}, body.get("at")))
        except (ToolError, NotFound, KeyError, ValueError, TypeError) as e:
            return JSONResponse({"ok": False, "error": f"{type(e).__name__}: {e}"}, status_code=400)

    async def inbox_route(self, request: Request) -> JSONResponse:
        since = int(request.query_params.get("since", "0") or 0)
        return JSONResponse({"seq": self.ws.store.seq, "items": self.ws.store.inbox_since(since)})

    async def health_route(self, request: Request) -> JSONResponse:
        return JSONResponse({"ok": True, "scenario": self.ws.scenario})

    def apply(self, op: str, args: dict, at=None) -> dict:
        if op == "scenario.load":
            info = load_scenario(self.ws, args["scenario"], args.get("charged_roles"))
            self.banner_seq = self.local_calls = 0
            return {"ok": True, "id": args["scenario"], "deliveries": [], **info}
        return world_apply(self.ws, op, args, at)

    # protocol surface
    @extension(CLOCK_URI, op="sync_time", description="Remember the gateway clock's base URL.")
    def sync_time(self, env_get_time_url: str, timezone: str | None = None) -> dict:
        self.gateway = env_get_time_url.removesuffix("/clock/time").rstrip("/")
        return {"ok": True, "gateway": self.gateway}

    @reset_data
    def reset(self) -> None:
        self.ws.reset()
        self.banner_seq = self.local_calls = 0

    @add_data
    def add(self, parts: list) -> None:
        """A DataPart {scenario, docs?, charged_roles?} loads a bundled scenario, or the given docs by app; YAML file
        parts named <app>.yaml are loaded together as one scenario."""
        files = {}
        for part in parts:
            if isinstance(part, DataPart):
                d = part.data
                if d.get("docs"):
                    load_docs(self.ws, d.get("scenario", "custom"), d["docs"], d.get("charged_roles"))
                else:
                    load_scenario(self.ws, d["scenario"], d.get("charged_roles"))
            elif getattr(part, "kind", "") == "file" and getattr(part.file, "bytes", None):
                name = (part.file.name or "custom.yaml").rsplit("/", 1)[-1]
                files[name.rsplit(".", 1)[0]] = yaml.safe_load(base64.b64decode(part.file.bytes))
        if files:
            load_docs(self.ws, files.get("world", {}).get("scenario", "custom"), files)

    @get_data
    def get(self) -> list[DataPart]:
        return [DataPart(data={"scenario": self.ws.scenario, "now": self.ws.iso(self.ws.now),
                               "collab": self.ws.store.export(), "action_log": self.ws.store.action_log,
                               "inbox": self.ws.store.inbox})]

    # the call path
    async def _meter(self, phase: str, payload: dict) -> tuple[dict | None, str | None]:
        try:
            r = await self.http.post(f"{self.aws_url}/awsbench/meter/{phase}", json=payload)
            r.raise_for_status()
            return r.json(), None
        except (httpx.HTTPError, ValueError) as e:
            return None, f"meter {phase} unreachable ({type(e).__name__}); uncharged, local clock"

    def _local_banner(self, t, note: str | None) -> dict:
        new = self.ws.store.inbox_since(self.banner_seq)
        self.banner_seq = self.ws.store.seq
        return {"now": self.ws.stamp(t), "call": self.local_calls, "calls_left": None,
                "new": [{k: v for k, v in i.items() if k != "seq"} for i in new],
                "mode": note or ("standalone: uncharged, local clock" if self.standalone else "uncharged")}

    async def _call(self, ctx: ToolContext | None, name: str, args: dict, fn: Callable) -> str:
        args = {k: v for k, v in args.items() if k not in ("self", "ctx")}
        role = ctx.role if ctx else None
        app = name.split("_", 1)[0]
        if self.ws.scenario is None and not self.standalone:
            return _json({"_clock": self._local_banner(self.ws.clock(), None), "error": NO_SCENARIO})
        if self.ws.identities and not self.ws.role_known(role):
            return _json({"error": f"role {role!r} has no identity in this workspace "
                                   f"(roles: {', '.join(sorted(self.ws.identities))})"})
        role = role if self.ws.role_known(role) else "sre-primary"
        actor = self.ws.ident(role, app)
        charged = role in self.ws.charged_roles and role != "harness"
        begin, note = (None, None)
        if charged and not self.standalone:
            begin, note = await self._meter("begin", {"server": "workspace", "tool": name, "args": args,
                                                      "role": role})
            if begin is not None and not begin.get("ok", False):
                return _json({"_clock": begin.get("banner") or self._local_banner(self.ws.clock(), None),
                              "error": begin.get("refuse") or "refused by the meter"})
        if begin is not None:
            t = self.ws.at(self.ws.when(begin["t_exec"]))
        else:
            if charged:
                self.local_calls += 1
            t = self.ws.advance(COSTS[cost_class(name)]) if charged else self.ws.clock()
        error = None
        try:
            out = fn(actor, t)
            result, resolved = out if isinstance(out, tuple) else (out, None)
        except (ToolError, NotFound) as e:
            error, result, resolved = str(e), {"error": str(e)}, None
        except Exception as e:  # noqa: BLE001
            error, result, resolved = f"internal error: {type(e).__name__}: {e}", {"error": "internal error"}, None
        resolved = resolved or {"app": app, "recipients": [], "mentions": [], "external": False}
        self.ws.store.log({"t": self.ws.iso(t), "kind": "tool", "call_no": begin.get("call_no") if begin else None,
                           "role": role, "actor": actor, "tool": name, "args": args, "error": error,
                           "resolved": resolved, "charged": begin is not None})
        banner = None
        if begin is not None:
            end, note = await self._meter("end", {"call_no": begin.get("call_no"), "tool": name, "args": args,
                                                  "result_summary": result_summary(result), "resolved": resolved})
            banner = end.get("banner") if end else None
            if isinstance(banner, dict) and "_clock" in banner:
                banner = banner["_clock"]
            self.banner_seq = self.ws.store.seq
        return _json({"_clock": banner or self._local_banner(t, note), **result})

    # chat
    @tool(structured_output=False)
    async def chat_list_channels(self, ctx: ToolContext) -> str:
        """List chat channels and your DMs, with topic, member count and last activity."""
        return await self._call(ctx, "chat_list_channels", {}, lambda a, t: chat.list_channels(self.ws, a, t))

    @tool(structured_output=False)
    async def chat_read(self, ctx: ToolContext, channel: str, thread_id: str | None = None, limit: int = 50) -> str:
        """Read a channel ('#name') or a DM ('@user'): its latest top-level messages with reply counts, or one thread
        with thread_id (the root message's id)."""
        return await self._call(ctx, "chat_read", locals(),
                                lambda a, t: chat.read(self.ws, a, t, channel, thread_id, limit))

    @tool(structured_output=False)
    async def chat_search(self, ctx: ToolContext, query: str, channel: str | None = None, user: str | None = None,
                          limit: int = 20) -> str:
        """Search messages you can see for all the words of `query`, optionally in one channel or from one user."""
        return await self._call(ctx, "chat_search", locals(),
                                lambda a, t: chat.search(self.ws, a, t, query, channel, user, limit))

    @tool(structured_output=False)
    async def chat_get_user(self, ctx: ToolContext, user: str) -> str:
        """A person's profile (name, title, team, email) and current status."""
        return await self._call(ctx, "chat_get_user", locals(), lambda a, t: chat.get_user(self.ws, a, t, user))

    @tool(structured_output=False)
    async def chat_post(self, ctx: ToolContext, channel: str, text: str, thread_id: str | None = None) -> str:
        """Post to a channel, or reply in a thread with thread_id. '@handle' mentions notify that person."""
        return await self._call(ctx, "chat_post", locals(),
                                lambda a, t: chat.post(self.ws, a, t, channel, text, thread_id))

    @tool(structured_output=False)
    async def chat_dm(self, ctx: ToolContext, user: str, text: str) -> str:
        """Send a direct message to one person."""
        return await self._call(ctx, "chat_dm", locals(), lambda a, t: chat.dm(self.ws, a, t, user, text))

    @tool(structured_output=False)
    async def chat_create_channel(self, ctx: ToolContext, name: str, topic: str = "",
                                  members: list[str] | str | None = None) -> str:
        """Create a channel; you join it, with the listed members (a list, or comma-separated handles)."""
        members = as_list(members)
        return await self._call(ctx, "chat_create_channel", locals(),
                                lambda a, t: chat.create_channel(self.ws, a, t, name, topic, members))

    # tickets
    @tool(structured_output=False)
    async def tickets_search(self, ctx: ToolContext, query: str | None = None, project: str | None = None,
                             status: str | None = None, assignee: str | None = None, limit: int = 20) -> str:
        """Search tickets (projects OPS, PLAT, PAY, SUP, ...) by words, project, status or assignee."""
        return await self._call(ctx, "tickets_search", locals(),
                                lambda a, t: tickets.search(self.ws, a, t, query, project, status, assignee, limit))

    @tool(structured_output=False)
    async def tickets_get(self, ctx: ToolContext, key: str) -> str:
        """A ticket with its fields, description, comments, links and the statuses it can move to."""
        return await self._call(ctx, "tickets_get", locals(), lambda a, t: tickets.get(self.ws, a, t, key))

    @tool(structured_output=False)
    async def tickets_create(self, ctx: ToolContext, project: str, summary: str, description: str = "",
                             type: str = "Task", priority: str | None = None, severity: str | None = None,
                             assignee: str | None = None, labels: list[str] | str | None = None) -> str:
        """Create a ticket in a project; you are the reporter."""
        labels = as_list(labels)
        return await self._call(ctx, "tickets_create", locals(), lambda a, t: tickets.create(
            self.ws, a, t, project, summary, description, type, priority, severity, assignee, labels))

    @tool(structured_output=False)
    async def tickets_comment(self, ctx: ToolContext, key: str, body: str, visibility: str = "internal") -> str:
        """Comment on a ticket; visibility 'internal' (staff) or 'public' (also the requester, on SUP tickets)."""
        return await self._call(ctx, "tickets_comment", locals(),
                                lambda a, t: tickets.comment(self.ws, a, t, key, body, visibility))

    @tool(structured_output=False)
    async def tickets_update(self, ctx: ToolContext, key: str, fields: dict[str, Any] | str) -> str:
        """Set ticket fields: summary, description, priority, severity (SEV1-SEV4), assignee, labels, or custom
        fields. Status changes go through tickets_transition."""
        fields = as_fields(fields)
        return await self._call(ctx, "tickets_update", locals(),
                                lambda a, t: tickets.update(self.ws, a, t, key, fields))

    @tool(structured_output=False)
    async def tickets_transition(self, ctx: ToolContext, key: str, status: str, comment: str | None = None) -> str:
        """Move a ticket to another status of its project's workflow, optionally with a comment."""
        return await self._call(ctx, "tickets_transition", locals(),
                                lambda a, t: tickets.transition(self.ws, a, t, key, status, comment))

    @tool(structured_output=False)
    async def tickets_link(self, ctx: ToolContext, key: str, target: str, type: str = "relates to") -> str:
        """Link a ticket to another ticket key or to a URL/reference."""
        return await self._call(ctx, "tickets_link", locals(),
                                lambda a, t: tickets.link(self.ws, a, t, key, target, type))

    # wiki
    @tool(structured_output=False)
    async def wiki_search(self, ctx: ToolContext, query: str, space: str | None = None, limit: int = 10) -> str:
        """Search wiki pages by words in their title, body or labels."""
        return await self._call(ctx, "wiki_search", locals(),
                                lambda a, t: wiki.search(self.ws, a, t, query, space, limit))

    @tool(structured_output=False)
    async def wiki_get(self, ctx: ToolContext, page: str, version: int | None = None) -> str:
        """A wiki page by id, 'SPACE/Title' or title (a unique prefix is enough); the current version unless
        `version` is given."""
        return await self._call(ctx, "wiki_get", locals(), lambda a, t: wiki.get(self.ws, a, t, page, version))

    @tool(structured_output=False)
    async def wiki_history(self, ctx: ToolContext, page: str) -> str:
        """A page's versions: who edited it, when and why."""
        return await self._call(ctx, "wiki_history", locals(), lambda a, t: wiki.history(self.ws, a, t, page))

    @tool(structured_output=False)
    async def wiki_update(self, ctx: ToolContext, page: str, body: str, comment: str = "", title: str | None = None,
                          space: str | None = None) -> str:
        """Save a new version of a page's body; with `space`, creates the page when it doesn't exist."""
        return await self._call(ctx, "wiki_update", locals(),
                                lambda a, t: wiki.update(self.ws, a, t, page, body, comment, title, space))

    @tool(structured_output=False)
    async def wiki_restore(self, ctx: ToolContext, page: str, version: int, comment: str = "") -> str:
        """Make an earlier version current again, as a new version."""
        return await self._call(ctx, "wiki_restore", locals(),
                                lambda a, t: wiki.restore(self.ws, a, t, page, version, comment))

    @tool(structured_output=False)
    async def wiki_comment(self, ctx: ToolContext, page: str, body: str) -> str:
        """Comment on a wiki page."""
        return await self._call(ctx, "wiki_comment", locals(), lambda a, t: wiki.comment(self.ws, a, t, page, body))

    # repo
    @tool(structured_output=False)
    async def repo_list(self, ctx: ToolContext) -> str:
        """List the code host's repositories."""
        return await self._call(ctx, "repo_list", {}, lambda a, t: repo.list_(self.ws, a, t))

    @tool(structured_output=False)
    async def repo_get(self, ctx: ToolContext, repo: str, pr: int | None = None) -> str:
        """A repository's files, pull requests and deploys, or one pull request with `pr`."""
        return await self._call(ctx, "repo_get", locals(), lambda a, t: _repo_get(self.ws, a, t, repo, pr))

    @tool(structured_output=False)
    async def repo_read(self, ctx: ToolContext, repo: str, path: str) -> str:
        """A file's content on the default branch."""
        return await self._call(ctx, "repo_read", locals(), lambda a, t: _repo_read(self.ws, a, t, repo, path))

    @tool(structured_output=False)
    async def repo_search(self, ctx: ToolContext, query: str, repo: str | None = None, limit: int = 30) -> str:
        """Search file paths and lines for all the words of `query`."""
        return await self._call(ctx, "repo_search", locals(),
                                lambda a, t: _repo_search(self.ws, a, t, query, repo, limit))

    @tool(structured_output=False)
    async def repo_log(self, ctx: ToolContext, repo: str, path: str | None = None, limit: int = 20) -> str:
        """Commits, merged pull requests and deploys of a repository, optionally touching one path."""
        return await self._call(ctx, "repo_log", locals(), lambda a, t: _repo_log(self.ws, a, t, repo, path, limit))

    # mail
    @tool(structured_output=False)
    async def mail_search(self, ctx: ToolContext, query: str | None = None, folder: str | None = None,
                          sender: str | None = None, unread: bool | None = None, limit: int = 20) -> str:
        """Search your mailbox (folder 'inbox' or 'sent'), newest first."""
        return await self._call(ctx, "mail_search", locals(),
                                lambda a, t: mail.search(self.ws, a, t, query, folder, sender, unread, limit))

    @tool(structured_output=False)
    async def mail_read(self, ctx: ToolContext, id: str) -> str:
        """Read a message (marks it read) with a list of its thread."""
        return await self._call(ctx, "mail_read", locals(), lambda a, t: mail.read(self.ws, a, t, id))

    @tool(structured_output=False)
    async def mail_send(self, ctx: ToolContext, to: list[str] | str, subject: str, body: str,
                        cc: list[str] | str | None = None) -> str:
        """Send a new email from your mailbox. `to` and `cc` take a list or comma-separated addresses."""
        to, cc = as_list(to), as_list(cc)
        return await self._call(ctx, "mail_send", locals(), lambda a, t: mail.send(self.ws, a, t, to, subject, body, cc))

    @tool(structured_output=False)
    async def mail_reply(self, ctx: ToolContext, id: str, body: str, reply_all: bool = False,
                         cc: list[str] | str | None = None) -> str:
        """Reply to a message (to its sender, or everyone with reply_all)."""
        cc = as_list(cc)
        return await self._call(ctx, "mail_reply", locals(),
                                lambda a, t: mail.reply(self.ws, a, t, id, body, reply_all, cc))

    @tool(structured_output=False)
    async def mail_forward(self, ctx: ToolContext, id: str, to: list[str] | str, note: str = "") -> str:
        """Forward a message to other addresses (a list, or comma-separated) with an optional note above it."""
        to = as_list(to)
        return await self._call(ctx, "mail_forward", locals(), lambda a, t: mail.forward(self.ws, a, t, id, to, note))

    # pager
    @tool(structured_output=False)
    async def pager_list(self, ctx: ToolContext, status: str | None = None, mine: bool = False) -> str:
        """List pages, newest first; filter by status (triggered, acknowledged, resolved) or to your own."""
        return await self._call(ctx, "pager_list", locals(), lambda a, t: pager.list_(self.ws, a, t, status, mine))

    @tool(structured_output=False)
    async def pager_get(self, ctx: ToolContext, id: str) -> str:
        """A page with its acks, notes and escalation."""
        return await self._call(ctx, "pager_get", locals(), lambda a, t: pager.get(self.ws, a, t, id))

    @tool(structured_output=False)
    async def pager_oncall(self, ctx: ToolContext, schedule: str | None = None) -> str:
        """Who is on call now for each schedule (or one), with overrides and escalation."""
        return await self._call(ctx, "pager_oncall", locals(), lambda a, t: pager.oncall(self.ws, a, t, schedule))

    @tool(structured_output=False)
    async def pager_ack(self, ctx: ToolContext, id: str) -> str:
        """Acknowledge a page."""
        return await self._call(ctx, "pager_ack", locals(), lambda a, t: pager.ack(self.ws, a, t, id))

    @tool(structured_output=False)
    async def pager_page(self, ctx: ToolContext, title: str, schedule: str | None = None, user: str | None = None,
                         urgency: str = "high", details: str = "", incident: str | None = None) -> str:
        """Page a schedule's on-call (or one user) at 'high' urgency (wakes them) or 'low' (does not)."""
        return await self._call(ctx, "pager_page", locals(), lambda a, t: pager.page(
            self.ws, a, t, title, schedule, user, urgency, details, incident))

    @tool(structured_output=False)
    async def pager_note(self, ctx: ToolContext, id: str, note: str) -> str:
        """Add a note to a page."""
        return await self._call(ctx, "pager_note", locals(), lambda a, t: pager.note(self.ws, a, t, id, note))

    @tool(structured_output=False)
    async def pager_resolve(self, ctx: ToolContext, id: str, note: str | None = None) -> str:
        """Resolve a page."""
        return await self._call(ctx, "pager_resolve", locals(), lambda a, t: pager.resolve(self.ws, a, t, id, note))

    # status
    @tool(structured_output=False)
    async def status_get(self, ctx: ToolContext, id: str | None = None) -> str:
        """The public status page: components and incidents, or one incident with its updates."""
        return await self._call(ctx, "status_get", locals(), lambda a, t: status.get(self.ws, a, t, id))

    @tool(structured_output=False)
    async def status_create(self, ctx: ToolContext, name: str, status: str, body: str, impact: str = "minor",
                            components: dict[str, str] | None = None) -> str:
        """Open a PUBLIC status-page incident (status investigating|identified|monitoring|resolved; impact
        none|minor|major|critical; components {id: operational|degraded_performance|partial_outage|major_outage})."""
        return await self._call(ctx, "status_create", locals(), lambda a, t: _status_create(
            self.ws, a, t, name, status, body, impact, components))

    @tool(structured_output=False)
    async def status_update(self, ctx: ToolContext, id: str, body: str, status: str | None = None,
                            components: dict[str, str] | None = None, impact: str | None = None) -> str:
        """Post a PUBLIC update on a status-page incident, optionally changing its status, impact or components."""
        return await self._call(ctx, "status_update", locals(), lambda a, t: _status_update(
            self.ws, a, t, id, body, status, components, impact))


# module functions under names the tool parameters don't shadow
_repo_get, _repo_read, _repo_search, _repo_log = repo.get, repo.read, repo.search, repo.log
_status_create, _status_update = status.create, status.update


def main() -> None:
    WorkspaceEnv().create_app().run(transport="http", host=os.environ.get("MCP_HOST", "0.0.0.0"), port=_port(),
                                    show_banner=False)


if __name__ == "__main__":
    main()
