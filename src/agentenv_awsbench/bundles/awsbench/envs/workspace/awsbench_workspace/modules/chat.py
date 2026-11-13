"""Chat: channels with threads and @mentions, DMs, user profiles and statuses."""

from datetime import datetime

from .base import ToolError, Workspace, terms_match

K_USER, K_CHANNEL, K_MSG = "chat.user", "chat.channel", "chat.message"


def channel_name(name: str) -> str:
    name = name.strip()
    return name if name.startswith(("#", "dm:")) else "#" + name


def dm_name(a: str, b: str) -> str:
    return "dm:" + "|".join(sorted((a, b)))


def load(ws: Workspace, doc: dict, t0: datetime) -> None:
    for u in doc.get("users", []):
        ws.store.create(K_USER, u["id"], {"status": None, **u}, by="seed", t=t0)
    for s in doc.get("statuses", []):
        ws.store.update(K_USER, s["user"], {"status": {**s, "until": ws.iso(ws.when(s.get("until")))}}, by="seed", t=t0)
    for c in doc.get("channels", []):
        name = channel_name(c["name"])
        ws.store.create(K_CHANNEL, name, {"name": name, "topic": c.get("topic", ""), "members": c.get("members", []),
                                          "created": ws.iso(ws.when(c.get("created"))), "created_by": "seed"},
                        by="seed", t=t0)
    for m in doc.get("messages", []):
        name = channel_name(m["channel"])
        if ws.store.get(K_CHANNEL, name) is None:
            ws.store.create(K_CHANNEL, name, {"name": name, "topic": "", "members": [], "created": None,
                                              "created_by": "seed"}, by="seed", t=t0)
        _store_msg(ws, m["id"], name, m["user"], m["text"], ws.when(m["ts"]), m.get("thread_id"), "seed")


def _store_msg(ws: Workspace, mid: str, channel: str, user: str, text: str, t: datetime, thread_id: str | None,
               by: str) -> dict:
    if thread_id and (root := ws.store.get(K_MSG, thread_id)) is None:
        raise ToolError(f"no message {thread_id!r} to reply to")
    if thread_id and root["channel"] != channel:
        raise ToolError(f"message {thread_id!r} is in {root['channel']}, not {channel}")
    if thread_id and root.get("thread_id"):
        thread_id = root["thread_id"]
    return ws.store.create(K_MSG, mid, {"id": mid, "channel": channel, "ts": ws.iso(t), "user": user, "text": text,
                                        "thread_id": thread_id, "mentions": ws.mentions(text)}, by=by, t=t)


def _msg_id(ws: Workspace) -> str:
    return f"m-{ws.next_id('chat', 1)}"


def _channel(ws: Workspace, name: str, actor: str) -> dict:
    if name.strip().startswith("@"):
        return ws.store.get(K_CHANNEL, dm_name(actor, ws.require_user(name)["id"])) or {
            "name": dm_name(actor, ws.require_user(name)["id"]), "members": [], "topic": ""}
    ch = ws.store.get(K_CHANNEL, channel_name(name))
    if ch is None:
        raise ToolError(f"no channel {channel_name(name)}; chat_list_channels lists them")
    if ch["name"].startswith("dm:") and actor not in ch["members"]:
        raise ToolError("that DM is not yours")
    return ch


def _visible(ws: Workspace, actor: str, t: datetime) -> list[dict]:
    names = {c["name"] for c in ws.store.items(K_CHANNEL) if not c["name"].startswith("dm:") or actor in c["members"]}
    return [m for m in ws.store.items(K_MSG) if m["channel"] in names and ws.when(m["ts"]) <= t]


def _sorted(msgs: list[dict]) -> list[dict]:
    return sorted(msgs, key=lambda m: m["ts"])


def _resolved(channel: dict, mentions: list[str], recipients: list[str] | None = None) -> dict:
    return {"app": "chat", "channel": channel["name"], "mentions": mentions,
            "recipients": recipients if recipients is not None else [m for m in channel["members"]],
            "external": False}


# tools
def list_channels(ws: Workspace, actor: str, t: datetime) -> dict:
    msgs = _visible(ws, actor, t)
    out = []
    for c in ws.store.items(K_CHANNEL):
        if c["name"].startswith("dm:") and actor not in c["members"]:
            continue
        mine = [m for m in msgs if m["channel"] == c["name"]]
        out.append({"name": c["name"] if not c["name"].startswith("dm:") else
                    "@" + next((u for u in c["members"] if u != actor), actor),
                    "topic": c["topic"], "members": len(c["members"]), "member": actor in c["members"],
                    "messages": len(mine), "last": max((m["ts"] for m in mine), default=None)})
    return {"channels": out}


def read(ws: Workspace, actor: str, t: datetime, channel: str, thread_id: str | None = None,
         limit: int = 50) -> dict:
    ch = _channel(ws, channel, actor)
    msgs = [m for m in _visible(ws, actor, t) if m["channel"] == ch["name"]]
    if thread_id:
        thread = [m for m in msgs if m["id"] == thread_id or m["thread_id"] == thread_id]
        if not thread:
            raise ToolError(f"no thread {thread_id!r} in {ch['name']}")
        return {"channel": channel, "thread_id": thread_id, "messages": _sorted(thread)}
    top = [dict(m, replies=sum(1 for r in msgs if r["thread_id"] == m["id"])) for m in msgs if not m["thread_id"]]
    top = _sorted(top)
    return {"channel": channel, "topic": ch.get("topic", ""), "total": len(top), "messages": top[-max(1, limit):],
            "note": "replies are in threads: chat_read with thread_id" if any(m["replies"] for m in top) else None}


def search(ws: Workspace, actor: str, t: datetime, query: str, channel: str | None = None, user: str | None = None,
           limit: int = 20) -> dict:
    name = _channel(ws, channel, actor)["name"] if channel else None
    hits = [m for m in _visible(ws, actor, t)
            if (name is None or m["channel"] == name) and (user is None or m["user"] == user.lstrip("@"))
            and terms_match(query, m["text"], m["user"])]
    hits = _sorted(hits)
    return {"query": query, "total": len(hits), "messages": hits[-max(1, limit):]}


def get_user(ws: Workspace, actor: str, t: datetime, user: str) -> dict:
    u = dict(ws.require_user(user))
    status = u.get("status")
    if status and status.get("until") and ws.when(status["until"]) <= t:
        u["status"] = None
    return {"user": u}


def post(ws: Workspace, actor: str, t: datetime, channel: str, text: str, thread_id: str | None = None):
    if not text or not text.strip():
        raise ToolError("text is empty")
    if channel.strip().startswith("@"):
        return dm(ws, actor, t, channel, text)
    ch = _channel(ws, channel, actor)
    if actor not in ch["members"]:
        ws.store.update(K_CHANNEL, ch["name"], {"members": ch["members"] + [actor]}, by=actor, t=t, op="join")
    m = _store_msg(ws, _msg_id(ws), ch["name"], actor, text, t, thread_id, actor)
    return {"posted": m}, _resolved(ch, m["mentions"])


def dm(ws: Workspace, actor: str, t: datetime, user: str, text: str):
    if not text or not text.strip():
        raise ToolError("text is empty")
    other = ws.require_user(user)["id"]
    ch = _ensure_dm(ws, actor, other, t, actor)
    m = _store_msg(ws, _msg_id(ws), ch["name"], actor, text, t, None, actor)
    return {"sent": m}, _resolved(ch, m["mentions"], [other])


def create_channel(ws: Workspace, actor: str, t: datetime, name: str, topic: str = "",
                   members: list[str] | None = None):
    name = channel_name(name)
    if ws.store.get(K_CHANNEL, name) is not None:
        raise ToolError(f"{name} already exists")
    ids = [actor] + [ws.require_user(m)["id"] for m in members or [] if ws.require_user(m)["id"] != actor]
    ch = ws.store.create(K_CHANNEL, name, {"name": name, "topic": topic or "", "members": ids, "created": ws.iso(t),
                                           "created_by": actor}, by=actor, t=t)
    return {"created": ch}, _resolved(ch, [], ids)


def _ensure_dm(ws: Workspace, a: str, b: str, t: datetime, by: str) -> dict:
    name = dm_name(a, b)
    return ws.store.get(K_CHANNEL, name) or ws.store.create(
        K_CHANNEL, name, {"name": name, "topic": "", "members": sorted({a, b}), "created": ws.iso(t),
                          "created_by": by}, by=by, t=t)


# world ops
def post_as(ws: Workspace, args: dict, t: datetime) -> tuple[str, list[dict]]:
    ch = ws.store.get(K_CHANNEL, channel_name(args["channel"]))
    if ch is None:
        ch = ws.store.create(K_CHANNEL, channel_name(args["channel"]),
                             {"name": channel_name(args["channel"]), "topic": "", "members": [args["user"]],
                              "created": ws.iso(t), "created_by": args["user"]}, by=args["user"], t=t)
    m = _store_msg(ws, args.get("id") or _msg_id(ws), ch["name"], args["user"], args["text"], t,
                   args.get("thread_id"), args["user"])
    agents = ws.agent_ids("chat") - {args["user"]}
    in_thread = m["thread_id"] and any(
        x["user"] in agents for x in ws.store.items(K_MSG) if m["thread_id"] in (x["id"], x["thread_id"]))
    out = []
    if agents & set(m["mentions"]) or in_thread or "@here" in m["text"] or "@channel" in m["text"]:
        out.append(ws.deliver("mention", t, args["user"], ch["name"], m["text"], id=m["id"]))
    elif agents & set(ch["members"]):
        out.append(ws.deliver("channel", t, args["user"], ch["name"], m["text"], id=m["id"]))
    return m["id"], out


def dm_as(ws: Workspace, args: dict, t: datetime) -> tuple[str, list[dict]]:
    frm, to = args["from_user"], ws.require_user(args["to_user"])["id"]
    ch = _ensure_dm(ws, frm, to, t, frm)
    m = _store_msg(ws, args.get("id") or _msg_id(ws), ch["name"], frm, args["text"], t, None, frm)
    out = [ws.deliver("dm", t, frm, "@" + frm, m["text"], id=m["id"])] if to in ws.agent_ids("chat") else []
    return m["id"], out
