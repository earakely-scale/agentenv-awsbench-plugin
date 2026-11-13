"""The scenario's timeline and scripted NPCs, lowered to timed items over a seeded clock.

Event kinds: `at` (+ jitter), `after_tool` (an agent call that matches, + delay), `after_event` (+ offset),
`every` (recurring, skipped while `unless_state` holds) and `state` (a physics predicate, `first_time` or `transition`).
An `unless` (`after_tool` or `after_event`) cancels a pending event when it matches before the event is due.
Every delivery goes through `WorkspacePort.apply`.
"""

import re
from datetime import datetime, timedelta

from .util import dig, glob_match, iso, jitter, parse_time, pick, seconds, text_of, unit

APPROVAL_ASK = re.compile(r"(?i)\bapprov|sign[- ]?off|\bok(ay)? to\b|go ahead|permission|second approver|\+1|\bconfirm")


def match_call(spec: dict, call: dict) -> bool:
    if not glob_match(spec.get("tool", "*"), call.get("tool", "")):
        return False
    role = spec.get("role", "sre-primary")
    if role and call.get("role") not in (role, None):
        return False
    return _where(spec.get("where") or {}, call)


def _where(where: dict, call: dict) -> bool:
    for path, cond in where.items():
        if path == "any":
            if not any(_where(w, call) for w in cond):
                return False
            continue
        value = dig(call, path)
        text = text_of(value)
        if "equals" in cond and text != str(cond["equals"]):
            return False
        if "regex" in cond and not re.search(cond["regex"], text):
            return False
        if "in" in cond and text not in [str(c) for c in cond["in"]]:
            return False
    return True


class Item:
    __slots__ = ("ctx", "due", "ev", "kind", "n", "trigger_t")

    def __init__(self, due, kind, ev=None, n=0, ctx=None, trigger_t=None):
        self.due, self.kind, self.ev, self.n, self.ctx, self.trigger_t = due, kind, ev, n, ctx or {}, trigger_t


class Timeline:
    def __init__(self, world, events: list[dict], npcs: dict, identities: dict):
        self.w = world
        self.seed = world.seed
        self.events = {e["id"]: e for e in events}
        self.order = [e["id"] for e in events]
        self.npcs = npcs or {}
        self.me = identities
        self.items: list[Item] = []
        self.log: list[dict] = []
        self.fired: dict[str, list[datetime]] = {}
        self.calls: list[dict] = []
        self.counts: dict[str, int] = {}
        self.page_ids: dict[str, str] = {}
        self.state_seen: dict[str, bool] = {}
        self.state_done: set[str] = set()
        self.approvals: list[dict] = []
        self.awake = {n: bool(c.get("awake", True)) for n, c in self.npcs.items()}
        self.waiting: dict[str, list[dict]] = {n: [] for n in self.npcs}
        self.world_flags: dict[str, bool] = {}

    # ---- scheduling ----
    def arm(self, start: datetime, horizon: datetime) -> None:
        self.horizon = horizon
        for eid in self.order:
            ev = self.events[eid]
            if "at" in ev:
                due = parse_time(ev["at"], start) + jitter(self.seed, eid, ev.get("jitter"))
                self.items.append(Item(due, "event", eid))
            elif "every" in ev:
                self._next_recurring(eid, 0, start)

    def _next_recurring(self, eid: str, n: int, start: datetime) -> None:
        ev = self.events[eid]
        base = parse_time(ev.get("from", start), start) + timedelta(seconds=seconds(ev["every"]) * n)
        due = base + jitter(self.seed, f"{eid}#{n}", ev.get("jitter"))
        until = self.horizon if ev.get("until", "horizon") == "horizon" else parse_time(ev["until"], start)
        if base <= until:
            self.items.append(Item(max(due, start), "recurring", eid, n))

    def next_due(self) -> datetime | None:
        return min((i.due for i in self.items), default=None)

    def pop_due(self, t: datetime) -> list[Item]:
        due = sorted((i for i in self.items if i.due <= t), key=lambda i: (i.due, i.kind))
        for i in due:
            self.items.remove(i)
        return due

    # ---- agent calls ----
    def observe(self, call: dict) -> None:
        t = call["t"]
        self.calls.append(call)
        for eid in self.order:
            ev = self.events[eid]
            spec = ev.get("after_tool")
            if spec and match_call(spec, call):
                if ev.get("once", True) and (self.fired.get(eid) or any(i.ev == eid for i in self.items)):
                    continue
                n = self.counts[eid] = self.counts.get(eid, 0) + 1
                due = t + timedelta(seconds=seconds(ev.get("delay"))) + jitter(self.seed, f"{eid}#{n}", ev.get("jitter"))
                self.items.append(Item(due, "event", eid, n, {"call": call}, t))
        self.route(call)

    def _cancelled(self, ev: dict, item: Item) -> bool:
        un = ev.get("unless")
        if not un:
            return False
        since = item.trigger_t or datetime.min.replace(tzinfo=item.due.tzinfo)
        if "after_tool" in un:
            if any(match_call(un["after_tool"], c) and c["t"] <= item.due for c in self.calls if c["t"] >= since):
                return True
        if "after_event" in un and self.fired.get(un["after_event"]):
            return True
        return False

    # ---- firing ----
    def run(self, item: Item, t: datetime) -> list[dict]:
        if item.kind == "npc_reply":
            return self._npc_reply(item, t)
        ev = self.events[item.ev]
        entry = {"id": item.ev if item.n <= 1 else f"{item.ev}#{item.n}",
                 "due_at": iso(item.due), "fired_at": None, "status": "fired", "deliveries": []}
        if item.kind == "recurring":
            entry["id"] = f"{item.ev}#{item.n}"
            self._next_recurring(item.ev, item.n + 1, self.w.start_t)
            if ev.get("unless_state") and self.w.eval_state(ev["unless_state"]):
                entry["status"] = "skipped"
                self.log.append(entry)
                return []
        if self._cancelled(ev, item):
            entry["status"] = "cancelled"
            self.log.append(entry)
            return []
        deliveries = self.fire(item.ev, ev.get("actions", []), t, item)
        entry.update(fired_at=iso(t), deliveries=deliveries)
        self.log.append(entry)
        return deliveries

    def fire(self, eid: str, actions: list[dict], t: datetime, item: Item | None = None) -> list[dict]:
        self.fired.setdefault(eid, []).append(t)
        ctx = {"n": item.n if item else 0, **((item.ctx if item else None) or {})}
        out = []
        for k, a in enumerate(actions):
            if "op" in a:
                args = self._render(a.get("args", {}), ctx, f"{eid}#{ctx['n']}#{k}")
                res = self.w.ws_apply(a["op"], args, t)
                if a["op"] == "pager.page_as" and res.get("id"):
                    self.page_ids.setdefault(eid, res["id"])
                out += res.get("deliveries", [])
            elif "npc" in a:
                if a.get("wake"):
                    self._wake(a["npc"], t)
                if a.get("say"):
                    out += self._say(a["npc"], a["say"], t, ctx)
            elif "world" in a:
                self.w.world_effect(a["world"], a.get("args", {}), t)
        for other in self.order:
            o = self.events[other]
            if o.get("after_event") == eid:
                due = t + timedelta(seconds=seconds(o.get("offset"))) + jitter(self.seed, other, o.get("jitter"))
                self.items.append(Item(due, "event", other, 0, {}, t))
        return out

    def _render(self, value, ctx: dict, key: str):
        if isinstance(value, dict):
            if "text_variants" in value:
                value = {**{k: v for k, v in value.items() if k != "text_variants"},
                         "text": pick(self.seed, key, value["text_variants"])}
            return {k: self._render(v, ctx, key) for k, v in value.items()}
        if isinstance(value, list):
            return [self._render(v, ctx, key) for v in value]
        if isinstance(value, str) and "{" in value:
            call = ctx.get("call") or {}

            def sub(m):
                name = m.group(1)
                if name.startswith("page:"):
                    return self.page_ids.get(name[5:], "")
                if name == "severity":
                    return text_of(dig(call, "args.fields.severity")) or "SEV1"
                if name == "n":
                    return str(ctx.get("n", 0))
                if name.startswith("call."):
                    return text_of(dig(call, name[5:]))
                return m.group(0)
            return re.sub(r"\{([a-z_]+(?::[\w.-]+|\.[\w.]+)?)\}", sub, value)
        return value

    # ---- state predicates ----
    def check_state(self, t: datetime) -> list[dict]:
        out = []
        for eid in self.order:
            ev = self.events[eid]
            if "state" not in ev:
                continue
            now = bool(self.w.eval_state(ev["state"]))
            before = self.state_seen.get(eid, False)
            self.state_seen[eid] = now
            if ev.get("trigger", "first_time") == "first_time":
                hit = now and eid not in self.state_done
            else:
                hit = now and not before
            if not hit:
                continue
            self.state_done.add(eid)
            item = Item(t, "event", eid, 0, {}, None)
            if self._cancelled(ev, item):
                self.log.append({"id": eid, "due_at": iso(t), "fired_at": None, "status": "cancelled",
                                 "deliveries": []})
                continue
            d = self.fire(eid, ev.get("actions", []), t, item)
            self.log.append({"id": eid, "due_at": iso(t), "fired_at": iso(t), "status": "fired", "deliveries": d})
            out += d
        return out

    # ---- NPCs ----
    def _npc_for(self, token: str) -> str | None:
        tok = (token or "").lower().lstrip("@").strip()
        for name, c in self.npcs.items():
            keys = {name.lower(), str(c.get("chat_user", name)).lower(), str(c.get("email", "")).lower()}
            keys |= {a.lower() for a in c.get("aliases", [])}
            if tok in keys or (tok and tok.split("@")[0] in keys):
                return name
        return None

    def route(self, call: dict) -> None:
        tool, args = call.get("tool", ""), call.get("args") or {}
        resolved = call.get("resolved") or {}
        text = text_of(args.get("text") or args.get("body") or args.get("message"))
        targets: list[tuple[str, dict]] = []
        if tool == "chat_dm":
            for to in [args.get("user"), args.get("to"), args.get("to_user"), *resolved.get("recipients", [])]:
                if isinstance(to, str) and self._npc_for(to):
                    targets.append((self._npc_for(to), {"via": "dm"}))
        elif tool == "chat_post":
            mentions = list(resolved.get("mentions") or []) + re.findall(r"@([\w.-]+)", text)
            for m in mentions:
                if self._npc_for(m):
                    targets.append((self._npc_for(m), {"via": "channel", "channel": args.get("channel"),
                                                       "thread_id": args.get("thread_id")}))
        elif tool.startswith("mail_") and tool != "mail_search" and tool != "mail_read":
            to = args.get("to") or []
            to = [to] if isinstance(to, str) else list(to)
            for addr in to + list(resolved.get("recipients") or []):
                if isinstance(addr, str) and self._npc_for(addr):
                    targets.append((self._npc_for(addr), {"via": "mail", "subject": args.get("subject", "")}))
        seen = set()
        for npc, how in targets:
            if npc in seen:
                continue
            seen.add(npc)
            msg = {"t": call["t"], "text": text, **how}
            if self.awake.get(npc, True):
                self._schedule_reply(npc, msg, call["t"])
            else:
                self.waiting[npc].append(msg)

    def _schedule_reply(self, npc: str, msg: dict, t: datetime) -> None:
        c = self.npcs[npc]
        lo, hi = (seconds(x) for x in c.get("reply_delay", ["PT1M", "PT2M"]))
        k = self.counts[f"npc:{npc}"] = self.counts.get(f"npc:{npc}", 0) + 1
        due = t + timedelta(seconds=lo + (hi - lo) * unit(self.seed, "reply", npc, k))
        self.items.append(Item(due, "npc_reply", npc, k, {"msg": msg}, t))

    def _wake(self, npc: str, t: datetime) -> None:
        if self.awake.get(npc):
            return
        self.awake[npc] = True
        for msg in self.waiting.pop(npc, []):
            self._schedule_reply(npc, msg, t)
        self.waiting[npc] = []

    def classify(self, npc: str, text: str) -> dict:
        for rule in self.npcs[npc].get("rules", []):
            m = rule.get("match", {})
            if m.get("all") and not all(re.search(p, text, re.IGNORECASE) for p in m["all"]):
                continue
            if m.get("any") and not any(re.search(p, text, re.IGNORECASE) for p in m["any"]):
                continue
            if m.get("none") and any(re.search(p, text, re.IGNORECASE) for p in m["none"]):
                continue
            return rule
        return {"id": "default", "reply": self.npcs[npc].get("default", "Noted.")}

    def _npc_reply(self, item: Item, t: datetime) -> list[dict]:
        npc, msg = item.ev, item.ctx["msg"]
        rule = self.classify(npc, msg["text"])
        text = pick(self.seed, f"{npc}#{item.n}", rule.get("reply_variants") or rule.get("reply", "Noted."))
        decision = rule.get("decision")
        if decision in ("APPROVED", "DECLINED") and (rule.get("always_record") or APPROVAL_ASK.search(msg["text"])):
            self.approvals.append({"t": iso(t), "by": npc, "decision": decision, "change": rule.get("change", ""),
                                   "scope": rule.get("scope", ""), "rule": rule["id"], "request": msg["text"][:300]})
        entry = {"id": f"npc:{npc}:{rule['id']}#{item.n}", "due_at": iso(item.due), "fired_at": iso(t),
                 "status": "fired", "deliveries": self._deliver(npc, msg, text, t)}
        self.log.append(entry)
        return entry["deliveries"]

    def _say(self, npc: str, line_id: str, t: datetime, ctx: dict) -> list[dict]:
        c = self.npcs[npc]
        line = c.get("scripted", {}).get(line_id, {})
        text = pick(self.seed, f"{npc}:{line_id}", line.get("text_variants") or line.get("text", ""))
        where = line.get("where", "#inc-4452")
        if where == "dm":
            msg = {"via": "dm"}
        elif where == "mail":
            call = ctx.get("call") or {}
            msg = {"via": "mail", "subject": text_of(dig(call, "args.subject")) or line.get("subject", "")}
        else:
            msg = {"via": "channel", "channel": where}
        return self._deliver(npc, msg, text, t, mention=line.get("mention", False))

    def _deliver(self, npc: str, msg: dict, text: str, t: datetime, mention: bool = True) -> list[dict]:
        c = self.npcs[npc]
        user = c.get("chat_user", npc)
        if msg["via"] == "dm":
            res = self.w.ws_apply("chat.dm_as", {"from_user": user, "to_user": self.me.get("chat", "sre-primary"),
                                                 "text": text}, t)
        elif msg["via"] == "mail":
            subj = msg.get("subject") or ""
            res = self.w.ws_apply("mail.deliver", {"from": c.get("email", f"{npc}@vantleaf.com"),
                                                   "to": [self.me.get("mail", "oncall@vantleaf.com")],
                                                   "subject": subj if subj.lower().startswith("re:") else f"Re: {subj}",
                                                   "body": text, "untrusted": bool(c.get("external"))}, t)
        else:
            body = f"@{self.me.get('chat', 'sre-primary')} {text}" if mention and "@" not in text[:1] else text
            args = {"channel": msg.get("channel") or "#inc-4452", "user": user, "text": body}
            if msg.get("thread_id"):
                args["thread_id"] = msg["thread_id"]
            res = self.w.ws_apply("chat.post_as", args, t)
        return res.get("deliveries", [])

    def export(self) -> list[dict]:
        return self.log
