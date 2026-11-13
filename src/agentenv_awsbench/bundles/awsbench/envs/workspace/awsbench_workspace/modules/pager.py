"""Pager: schedules with overrides, who is on call, and pages (urgency, acks, notes, resolve) with PG ids."""

from datetime import datetime

from .base import ToolError, Workspace

K_SCHED, K_PAGE = "pager.schedule", "pager.incident"
URGENCIES = ("high", "low")


def load(ws: Workspace, doc: dict, t0: datetime) -> None:
    for name, s in doc.get("schedules", {}).items():
        ws.store.create(K_SCHED, name, {"name": name, "primary": s["primary"], "secondary": s.get("secondary"),
                                        "escalation": s.get("escalation"), "hours": s.get("hours", "24/7"),
                                        "overrides": []}, by="seed", t=t0)
    for o in doc.get("overrides", []):
        sched = ws.store.require(K_SCHED, o["schedule"])
        ws.store.update(K_SCHED, o["schedule"], {"overrides": sched["overrides"] + [
            {"user": o["user"], "from": ws.iso(ws.when(o["from"])), "to": ws.iso(ws.when(o["to"])),
             "note": o.get("note", "")}]}, by="seed", t=t0)
    for p in doc.get("incidents", []):
        ws.store.create(K_PAGE, p["id"], {
            "id": p["id"], "title": p["title"], "urgency": p.get("urgency", "high"),
            "schedules": p.get("schedules", []), "assigned": p.get("assigned", []), "status": p.get("status", "triggered"),
            "triggered_at": ws.iso(ws.when(p["triggered_at"])), "created_by": p.get("created_by", "alert-integration"),
            "details": p.get("details", ""), "incident": p.get("incident"),
            "acks": [dict(a, at=ws.iso(ws.when(a["at"]))) for a in p.get("acks", [])],
            "notes": [], "resolved_at": None, "escalation": p.get("escalation")}, by="seed", t=t0)
    ws.config["pager_next"] = int(doc.get("next_id", 77053))


def oncall_user(ws: Workspace, schedule: str, t: datetime) -> str:
    s = ws.store.require(K_SCHED, schedule)
    for o in reversed(s["overrides"]):
        if ws.when(o["from"]) <= t < ws.when(o["to"]):
            return o["user"]
    return s["primary"]


def _page(ws: Workspace, pid: str) -> dict:
    if (p := ws.store.get(K_PAGE, pid.strip().upper())) is None:
        raise ToolError(f"no page {pid}; pager_list lists them")
    return p


def _resolved(p: dict, fields: list[str]) -> dict:
    return {"app": "pager", "page": p["id"], "recipients": p["assigned"], "schedules": p["schedules"],
            "urgency": p["urgency"], "fields_changed": fields, "mentions": [], "external": False}


def _summary(p: dict) -> dict:
    return {k: p.get(k) for k in ("id", "title", "urgency", "status", "schedules", "assigned", "triggered_at",
                                  "incident")} | {"acked_by": [a["user"] for a in p["acks"]]}


def _create(ws: Workspace, by: str, t: datetime, target: str, urgency: str, title: str, details: str,
            incident: str | None) -> dict:
    if urgency not in URGENCIES:
        raise ToolError("urgency is high or low")
    if ws.store.get(K_SCHED, target) is not None:
        schedules, assigned = [target], [oncall_user(ws, target, t)]
    elif (u := ws.user(target)) is not None:
        schedules, assigned = [], [u["id"]]
    else:
        raise ToolError(f"{target!r} is neither a schedule nor a user; pager_oncall lists the schedules")
    pid = f"PG-{ws.next_id('pager', ws.config.get('pager_next', 77053))}"
    return ws.store.create(K_PAGE, pid, {
        "id": pid, "title": title, "urgency": urgency, "schedules": schedules, "assigned": assigned,
        "status": "triggered", "triggered_at": ws.iso(t), "created_by": by, "details": details or "",
        "incident": incident, "acks": [], "notes": [], "resolved_at": None, "escalation": None}, by=by, t=t)


def _ack(ws: Workspace, p: dict, user: str, t: datetime) -> None:
    if p["status"] == "resolved":
        raise ToolError(f"{p['id']} is resolved")
    if any(a["user"] == user for a in p["acks"]):
        raise ToolError(f"{p['id']} is already acknowledged by {user}")
    ws.store.update(K_PAGE, p["id"], {"acks": p["acks"] + [{"user": user, "at": ws.iso(t)}],
                                      "status": "acknowledged"}, by=user, t=t, op="ack")


# tools
def list_(ws: Workspace, actor: str, t: datetime, status: str | None = None, mine: bool = False) -> dict:
    pages = [p for p in ws.store.items(K_PAGE) if ws.when(p["triggered_at"]) <= t
             and (status is None or p["status"] == status) and (not mine or actor in p["assigned"])]
    return {"pages": [_summary(p) for p in sorted(pages, key=lambda p: p["triggered_at"], reverse=True)]}


def get(ws: Workspace, actor: str, t: datetime, id: str) -> dict:
    return {"page": _page(ws, id)}


def oncall(ws: Workspace, actor: str, t: datetime, schedule: str | None = None) -> dict:
    names = [schedule] if schedule else [s["name"] for s in ws.store.items(K_SCHED)]
    out = []
    for name in names:
        if ws.store.get(K_SCHED, name) is None:
            raise ToolError(f"no schedule {name!r}")
        s = ws.store.get(K_SCHED, name)
        out.append({"schedule": name, "oncall": oncall_user(ws, name, t), "primary": s["primary"],
                    "secondary": s["secondary"], "escalation": s["escalation"], "hours": s["hours"],
                    "overrides": [o for o in s["overrides"] if ws.when(o["to"]) > t]})
    return {"now": ws.iso(t), "schedules": out}


def ack(ws: Workspace, actor: str, t: datetime, id: str):
    p = _page(ws, id)
    _ack(ws, p, actor, t)
    return {"page": _summary(p)}, _resolved(p, ["acks"])


def page(ws: Workspace, actor: str, t: datetime, title: str, schedule: str | None = None, user: str | None = None,
         urgency: str = "high", details: str = "", incident: str | None = None):
    target = schedule or user
    if not target or (schedule and user):
        raise ToolError("give exactly one of schedule or user")
    p = _create(ws, actor, t, target, urgency, title, details, incident)
    return {"page": _summary(p)}, _resolved(p, ["created"])


def note(ws: Workspace, actor: str, t: datetime, id: str, note: str):
    p = _page(ws, id)
    ws.store.append(K_PAGE, p["id"], "notes", {"by": actor, "at": ws.iso(t), "text": note}, by=actor, t=t, op="note")
    return {"page": p["id"], "notes": p["notes"]}, _resolved(p, ["notes"])


def resolve(ws: Workspace, actor: str, t: datetime, id: str, note: str | None = None):
    p = _page(ws, id)
    if p["status"] == "resolved":
        raise ToolError(f"{p['id']} is already resolved")
    if note:
        ws.store.append(K_PAGE, p["id"], "notes", {"by": actor, "at": ws.iso(t), "text": note}, by=actor, t=t,
                        op="note")
    ws.store.update(K_PAGE, p["id"], {"status": "resolved", "resolved_at": ws.iso(t), "resolved_by": actor},
                    by=actor, t=t, op="resolve")
    return {"page": _summary(p)}, _resolved(p, ["status"])


# world ops
def page_as(ws: Workspace, args: dict, t: datetime) -> tuple[str, list[dict]]:
    p = _create(ws, args["from_user"], t, args["to"], args.get("urgency", "high"), args["summary"],
                args.get("details", ""), args.get("incident"))
    out = []
    if ws.agent_ids("pager") & set(p["assigned"]):
        tag = f"[{p['urgency'].upper()}]"
        text = p["title"] if p["title"].lstrip().upper().startswith(tag) else f"{tag} {p['title']}"
        out.append(ws.deliver("page", t, args["from_user"], p["id"], text, id=p["id"]))
    return p["id"], out


def ack_as(ws: Workspace, args: dict, t: datetime) -> tuple[str, list[dict]]:
    p = _page(ws, args["page_id"])
    if args.get("if_open") and (p["status"] == "resolved" or any(a["user"] == args["user"] for a in p["acks"])):
        return p["id"], []
    _ack(ws, p, args["user"], t)
    return p["id"], []
