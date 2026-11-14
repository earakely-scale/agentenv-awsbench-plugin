"""Public status page: components and incidents with updates. Every write here is public, so it counts as external."""

from datetime import datetime

from .base import ToolError, Workspace

K_COMP, K_INC = "status.component", "status.incident"
COMPONENT_STATES = ("operational", "degraded_performance", "partial_outage", "major_outage", "under_maintenance")
INCIDENT_STATES = ("investigating", "identified", "monitoring", "resolved")
IMPACTS = ("none", "minor", "major", "critical")


def load(ws: Workspace, doc: dict, t0: datetime) -> None:
    for c in doc.get("components", []):
        ws.store.create(K_COMP, c["id"], {"id": c["id"], "name": c["name"], "status": c.get("status", "operational")},
                        by="seed", t=t0)
    for i in doc.get("incidents", []):
        ws.store.create(K_INC, i["id"], i, by="seed", t=t0)
    ws.config["status_url"] = doc.get("url", "")


def _resolved(inc: dict, fields: list[str]) -> dict:
    return {"app": "status", "status_incident": inc["id"], "fields_changed": fields, "status": inc["status"],
            "components": inc["components"], "recipients": ["public"], "mentions": [], "external": True}


def _components(ws: Workspace, components: dict | None) -> dict:
    out = {}
    for cid, state in (components or {}).items():
        if ws.store.get(K_COMP, cid) is None:
            raise ToolError(f"no component {cid!r}; components are {[c['id'] for c in ws.store.items(K_COMP)]}")
        if state not in COMPONENT_STATES:
            raise ToolError(f"component state is one of {', '.join(COMPONENT_STATES)}")
        out[cid] = state
    return out


def _set_components(ws: Workspace, comps: dict, by: str, t: datetime) -> None:
    for cid, state in comps.items():
        ws.store.update(K_COMP, cid, {"status": state}, by=by, t=t)


def _check(status: str | None, impact: str | None, body: str) -> None:
    if status is not None and status not in INCIDENT_STATES:
        raise ToolError(f"status is one of {', '.join(INCIDENT_STATES)}")
    if impact is not None and impact not in IMPACTS:
        raise ToolError(f"impact is one of {', '.join(IMPACTS)}")
    if not body or not body.strip():
        raise ToolError("body is empty: a public update needs text")


# tools
def get(ws: Workspace, actor: str, t: datetime, id: str | None = None) -> dict:
    if id:
        if (inc := ws.store.get(K_INC, id)) is None:
            raise ToolError(f"no status incident {id!r}")
        return {"incident": inc}
    return {"url": ws.config.get("status_url", ""), "components": ws.store.items(K_COMP),
            "incidents": [i for i in ws.store.items(K_INC)]}


def create(ws: Workspace, actor: str, t: datetime, name: str, status: str, body: str, impact: str = "minor",
           components: dict | None = None):
    _check(status, impact, body)
    comps = _components(ws, components)
    sid = f"si-{ws.next_id('status', 1)}"
    inc = ws.store.create(K_INC, sid, {
        "id": sid, "name": name, "status": status, "impact": impact, "components": comps, "created": ws.iso(t),
        "updated": ws.iso(t), "resolved_at": ws.iso(t) if status == "resolved" else None,
        "updates": [{"t": ws.iso(t), "status": status, "body": body, "by": actor, "components": comps}]},
        by=actor, t=t)
    _set_components(ws, comps, actor, t)
    return {"incident": inc}, _resolved(inc, ["created"])


def update(ws: Workspace, actor: str, t: datetime, id: str, body: str, status: str | None = None,
           components: dict | None = None, impact: str | None = None):
    if (inc := ws.store.get(K_INC, id)) is None:
        raise ToolError(f"no status incident {id!r}; status_get lists them")
    _check(status, impact, body)
    comps = _components(ws, components)
    status = status or inc["status"]
    fields = {"status": status, "updated": ws.iso(t), "components": {**inc["components"], **comps},
              "updates": inc["updates"] + [{"t": ws.iso(t), "status": status, "body": body, "by": actor,
                                            "components": comps}]}
    if impact:
        fields["impact"] = impact
    if status == "resolved" and not inc.get("resolved_at"):
        fields["resolved_at"] = ws.iso(t)
    changed = list(ws.store.update(K_INC, id, fields, by=actor, t=t))
    _set_components(ws, comps, actor, t)
    return {"incident": inc}, _resolved(inc, changed)
