"""Ticket tracker: projects with a workflow, issues with fields, comments (internal or public), links and the full
field history the store keeps."""

import re
from datetime import datetime

from .base import ToolError, Workspace, snippet, terms_match

K_ISSUE = "tickets.issue"
SEVERITIES = ("SEV1", "SEV2", "SEV3", "SEV4")
DEFAULT_WORKFLOW = ["Open", "In Progress", "In Review", "Done", "Closed"]
CORE_FIELDS = ("summary", "description", "priority", "severity", "assignee", "labels", "type")


def load(ws: Workspace, doc: dict, t0: datetime) -> None:
    ws.config["workflows"] = dict(doc.get("workflows", {}))
    for i in doc.get("issues", []):
        data = {"key": i["key"], "project": i.get("project", i["key"].split("-")[0]), "type": i.get("type", "Task"),
                "summary": i["summary"], "description": i.get("description", ""), "status": i.get("status", "Open"),
                "priority": i.get("priority"), "severity": i.get("severity"), "assignee": i.get("assignee"),
                "reporter": i.get("reporter"), "labels": i.get("labels", []), "fields": i.get("fields", {}),
                "created": ws.iso(ws.when(i.get("created"))), "updated": ws.iso(ws.when(i.get("updated") or i.get("created"))),
                "comments": [dict(c, id=c.get("id") or f"c-{n}", ts=ws.iso(ws.when(c["ts"])),
                                  visibility=c.get("visibility", "internal"))
                             for n, c in enumerate(i.get("comments", []), 1)],
                "links": i.get("links", []), "untrusted": i.get("untrusted", False)}
        ws.store.create(K_ISSUE, i["key"], data, by="seed", t=t0)


def workflow(ws: Workspace, project: str) -> list[str]:
    return ws.config.get("workflows", {}).get(project, DEFAULT_WORKFLOW)


def _issue(ws: Workspace, key: str) -> dict:
    key = key.strip().upper()
    if (issue := ws.store.get(K_ISSUE, key)) is None:
        raise ToolError(f"no ticket {key}")
    return issue


def _summary(i: dict) -> dict:
    return {k: i.get(k) for k in ("key", "type", "summary", "status", "priority", "severity", "assignee", "updated")}


def _resolved(issue: dict, fields_changed: list[str], mentions: list[str] | None = None,
              external: bool = False) -> dict:
    recipients = [u for u in (issue.get("assignee"), issue.get("reporter")) if u]
    return {"app": "tickets", "ticket": issue["key"], "fields_changed": fields_changed, "mentions": mentions or [],
            "recipients": recipients, "external": external}


def _check_fields(ws: Workspace, fields: dict) -> dict:
    out = {}
    for k, v in fields.items():
        if k == "status":
            raise ToolError("status changes go through tickets_transition")
        if k == "severity" and v is not None:
            v = str(v).upper().replace(" ", "")
            if v not in SEVERITIES:
                raise ToolError(f"severity must be one of {', '.join(SEVERITIES)}")
        if k == "assignee" and v:
            v = ws.require_user(v)["id"]
        if k == "labels" and not isinstance(v, list):
            raise ToolError("labels must be a list")
        out[k] = v
    return out


def _apply_fields(ws: Workspace, issue: dict, fields: dict, by: str, t: datetime) -> list[str]:
    core = {k: v for k, v in fields.items() if k in CORE_FIELDS}
    custom = {k: v for k, v in fields.items() if k not in CORE_FIELDS}
    changed = list(ws.store.update(K_ISSUE, issue["key"], core, by=by, t=t))
    if custom:
        merged = {**issue["fields"], **custom}
        if ws.store.update(K_ISSUE, issue["key"], {"fields": merged}, by=by, t=t):
            changed += [k for k in custom]
    if changed:
        ws.store.update(K_ISSUE, issue["key"], {"updated": ws.iso(t)}, by=by, t=t, op="touch")
    return changed


# tools
def search(ws: Workspace, actor: str, t: datetime, query: str | None = None, project: str | None = None,
           status: str | None = None, assignee: str | None = None, limit: int = 20) -> dict:
    hits = [i for i in ws.store.items(K_ISSUE)
            if (project is None or i["project"] == project.upper())
            and (status is None or i["status"].lower() == status.lower())
            and (assignee is None or i.get("assignee") == assignee.lstrip("@"))
            and terms_match(query, i["key"], i["summary"], i["description"],
                            *(c["body"] for c in i["comments"]))]
    hits.sort(key=lambda i: i["updated"] or "", reverse=True)
    return {"total": len(hits), "issues": [dict(_summary(i), match=snippet(i["description"], query) if query else None)
                                           for i in hits[:max(1, limit)]]}


def get(ws: Workspace, actor: str, t: datetime, key: str) -> dict:
    issue = _issue(ws, key)
    return {"issue": issue, "transitions": [s for s in workflow(ws, issue["project"]) if s != issue["status"]]}


def create(ws: Workspace, actor: str, t: datetime, project: str, summary: str, description: str = "",
           type: str = "Task", priority: str | None = None, severity: str | None = None,
           assignee: str | None = None, labels: list[str] | None = None):
    project = project.strip().upper()
    if not re.fullmatch(r"[A-Z]+", project):
        raise ToolError("project is a key such as OPS, PLAT, PAY, SUP")
    issue = _create(ws, project, actor, t, summary=summary, description=description or "", type=type,
                    **_check_fields(ws, {"priority": priority, "severity": severity, "assignee": assignee,
                                         "labels": labels or []}))
    return {"created": _summary(issue)}, _resolved(issue, ["created"])


def _create(ws: Workspace, project: str, by: str, t: datetime, **fields) -> dict:
    taken = [int(i["key"].split("-")[1]) for i in ws.store.items(K_ISSUE) if i["project"] == project]
    key = f"{project}-{ws.next_id('tickets:' + project, max(taken, default=0) + 1)}"
    data = {"key": key, "project": project, "type": "Task", "summary": "", "description": "",
            "status": workflow(ws, project)[0], "priority": None, "severity": None, "assignee": None, "reporter": by,
            "labels": [], "fields": {}, "created": ws.iso(t), "updated": ws.iso(t), "comments": [], "links": [],
            "untrusted": False, **fields}
    return ws.store.create(K_ISSUE, key, data, by=by, t=t)


def comment(ws: Workspace, actor: str, t: datetime, key: str, body: str, visibility: str = "internal"):
    issue = _issue(ws, key)
    if visibility not in ("internal", "public"):
        raise ToolError("visibility is internal or public")
    if not body or not body.strip():
        raise ToolError("body is empty")
    c = _comment(ws, issue, actor, body, t, visibility)
    external = visibility == "public" and issue["project"] == "SUP"
    return {"comment": c, "ticket": issue["key"]}, _resolved(issue, ["comment"], c["mentions"], external)


def _comment(ws: Workspace, issue: dict, by: str, body: str, t: datetime, visibility: str) -> dict:
    c = {"id": f"c-{ws.next_id('comment', 1000)}", "author": by, "ts": ws.iso(t), "body": body,
         "visibility": visibility, "mentions": ws.mentions(body)}
    ws.store.append(K_ISSUE, issue["key"], "comments", c, by=by, t=t, op="comment")
    ws.store.update(K_ISSUE, issue["key"], {"updated": ws.iso(t)}, by=by, t=t, op="touch")
    return c


def update(ws: Workspace, actor: str, t: datetime, key: str, fields: dict):
    issue = _issue(ws, key)
    if not isinstance(fields, dict):
        raise ToolError('fields must be an object, e.g. {"severity": "SEV1"}')
    if not fields:
        raise ToolError("fields is empty")
    changed = _apply_fields(ws, issue, _check_fields(ws, fields), actor, t)
    values = {k: issue.get(k, issue["fields"].get(k)) for k in changed}
    return ({"ticket": issue["key"], "changed": changed, "issue": _summary(issue)},
            {**_resolved(issue, changed), "values": values})


def transition(ws: Workspace, actor: str, t: datetime, key: str, status: str, comment: str | None = None):
    issue = _issue(ws, key)
    flow = workflow(ws, issue["project"])
    target = next((s for s in flow if s.lower() == status.strip().lower()), None)
    if target is None:
        raise ToolError(f"{issue['project']} statuses are {', '.join(flow)}")
    if issue["status"] == "Closed" and target != "Closed":
        raise ToolError(f"{issue['key']} is Closed")
    if target == issue["status"]:
        raise ToolError(f"{issue['key']} is already {target}")
    ws.store.update(K_ISSUE, issue["key"], {"status": target, "updated": ws.iso(t)}, by=actor, t=t, op="transition")
    if comment:
        _comment(ws, issue, actor, comment, t, "internal")
    return {"ticket": issue["key"], "status": target}, {**_resolved(issue, ["status"]), "values": {"status": target}}


INVERSE = {"relates to": "relates to", "blocks": "is blocked by", "causes": "is caused by", "fixes": "is fixed by",
           "duplicates": "is duplicated by", "clones": "is cloned by", "implements": "is implemented by"}
INVERSE |= {v: k for k, v in INVERSE.items()}


def _add_link(ws: Workspace, issue: dict, type: str, target: str, actor: str, t: datetime) -> None:
    if not any(x.get("type") == type and x.get("target") == target for x in issue.get("links") or []):
        ws.store.append(K_ISSUE, issue["key"], "links", {"type": type, "target": target, "by": actor, "ts": ws.iso(t)},
                        by=actor, t=t, op="link")


def link(ws: Workspace, actor: str, t: datetime, key: str, target: str, type: str = "relates to"):
    """Links an issue to a ticket, both ways as a tracker shows them (the target gets the inverse type), or to a URL
    or reference, on the issue only."""
    issue = _issue(ws, key)
    other = _issue(ws, target) if re.fullmatch(r"[A-Za-z]+-\d+", target.strip()) else None
    _add_link(ws, issue, type, other["key"] if other else target, actor, t)
    if other and other["key"] != issue["key"]:
        _add_link(ws, other, INVERSE.get(type.strip().lower(), type), issue["key"], actor, t)
    return {"ticket": issue["key"], "links": issue["links"]}, _resolved(issue, ["links"])


# world ops
def comment_as(ws: Workspace, args: dict, t: datetime) -> tuple[str, list[dict]]:
    issue = _issue(ws, args["key"])
    c = _comment(ws, issue, args["user"], args["body"], t, args.get("visibility", "internal"))
    agents = ws.agent_ids("tickets")
    out = [ws.deliver("mention", t, args["user"], issue["key"], c["body"], id=c["id"])] if agents & set(
        c["mentions"]) else []
    return c["id"], out


def update_as(ws: Workspace, args: dict, t: datetime) -> tuple[str, list[dict]]:
    issue = _issue(ws, args["key"])
    fields = dict(args.get("fields") or {})
    if (status := fields.pop("status", None)) and status != issue["status"]:
        ws.store.update(K_ISSUE, issue["key"], {"status": status, "updated": ws.iso(t)}, by=args["user"], t=t,
                        op="transition")
    _apply_fields(ws, issue, fields, args["user"], t)
    return issue["key"], []


def create_as(ws: Workspace, args: dict, t: datetime) -> tuple[str, list[dict]]:
    fields = {k: args[k] for k in ("summary", "description", "type", "priority", "severity", "assignee", "labels",
                                   "fields", "untrusted") if k in args}
    issue = _create(ws, args["project"].upper(), args["user"], t, **fields)
    return issue["key"], []
