"""The scenario's timeline and scripted NPCs, lowered to timed items over a seeded clock.

Event kinds: `at` (+ jitter), `after_tool` (an agent call that matches, + delay), `after_event` (+ offset),
`every` (recurring, skipped while `unless_state` holds) and `state` (a physics predicate, `first_time` or `transition`).
An `unless` (`after_tool` or `after_event`) cancels a pending event when it matches before the event is due.
Every delivery goes through `WorkspacePort.apply`.
"""

import re
from datetime import datetime, timedelta

from .util import dig, glob_match, iso, jitter, parse_time, pick, seconds, text_of, unit

ASK = re.compile(r"(?i)\b(?:ask(?:ing)?|approve|approvals?|permission|ok(?:ay)? to|can i|may i|please confirm|"
                 r"requesting|request (?:your |an? )?(?:approval|sign[- ]?off|decision)|sign[- ]?off|need your)\b")
CLAUSE = re.compile(r"(?<=[.!?])\s+|\n+|\s\|\s|;\s+|\s+[\u2014\u2013]\s+")
ASIDE = re.compile(r"(?i)^(?:(?:undo|rollback(?: plan)?|fallback|held|hold)\s*:|if declined|otherwise\b|not\b|no\b|"
                   r"never\b|without\b|won'?t\b|i'?m not\b|i will not\b|i won'?t\b|do not\b|don'?t\b)")
HEDGE = {"not", "no", "never", "won't", "don't", "didn't", "isn't", "until", "unless", "once", "after", "awaiting",
         "without", "nobody"}
GEN = re.compile(r"\b(\d{5})\b")


def clauses(text: str) -> list[str]:
    """The message's clauses without leading @mentions, minus asides (undo plans, holds, negations)."""
    out = []
    for c in CLAUSE.split((text or "").replace("\u2019", "'")):
        c = re.sub(r"^(?:\s*@[\w.-]+[,:]?)+\s*", "", c).strip()
        if c and not ASIDE.match(c):
            out.append(c)
    return out


def asks(text: str) -> list[str]:
    """What the message asks for: each clause from its first unhedged ask word on, or a whole question. A statement
    ("endpointd stays paused until Kwame approves a resume") asks nothing, so it can never draw a decision."""
    out = []
    for c in clauses(text):
        for m in ASK.finditer(c):
            if not HEDGE & set(re.findall(r"[\w']+", c[:m.start()].lower())[-4:]):
                out.append(c[m.start():])
                break
        else:
            if c.endswith("?"):
                out.append(c)
    return out


def _matches(m: dict, text: str) -> bool:
    return not (m.get("all") and not all(re.search(p, text, re.IGNORECASE) for p in m["all"])
                or m.get("any") and not any(re.search(p, text, re.IGNORECASE) for p in m["any"])
                or m.get("none") and any(re.search(p, text, re.IGNORECASE) for p in m["none"]))


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
        self.replied: dict[tuple[str, str], set[str]] = {}
        self.awaiting: set[str] = set()
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

    def classify(self, npc: str, text: str) -> tuple[dict, str, dict] | None:
        """The rule that answers `text`, the request it answers and its template values, or None for silence.

        Rules with a `decision` or `on_request` read only what the message asks for; the others read the whole text.
        Each (npc, rule) answers once, and again only for a request it has not answered (`once` rules never repeat);
        a rule whose `unless_state` holds is skipped. The default answers requests only."""
        c = self.npcs[npc]
        ask_list = asks(text)
        if npc in self.awaiting:
            self.awaiting.discard(npc)
            ask_list = ask_list or clauses(text)
        ask = " ".join(ask_list)
        key = " ".join(ask.lower().split())
        for rule in c.get("rules", []):
            on_request = bool(rule.get("on_request")) or "decision" in rule
            if on_request and not ask or not _matches(rule.get("match", {}), ask if on_request else text):
                continue
            values = {}
            if "plan_gen" in rule:
                values = self._plan_gen(rule["plan_gen"], ask if on_request else text)
                if values is None:
                    continue
            if self._spent(npc, rule, key):
                if on_request:
                    return None
                continue
            return rule, ask, values
        if not ask or c.get("default_unless_state") and self.w.eval_state(c["default_unless_state"]):
            return None
        rule = {"id": "default", "reply": c.get("default", "Noted."), "asks_details": True}
        return None if self._spent(npc, rule, key) else (rule, ask, {})

    def _spent(self, npc: str, rule: dict, key: str) -> bool:
        if rule.get("unless_state") and self.w.eval_state(rule["unless_state"]):
            return True
        seen = self.replied.get((npc, rule["id"]))
        return seen is not None and (bool(rule.get("once")) or not key or key in seen)

    def _plan_gen(self, spec: dict, text: str) -> dict | None:
        """`fits_fleet: true` matches a request naming a plan gen at or above `min` whose ledger records match the
        live fleet (or naming none); `fits_fleet: false` matches one naming only gens that don't."""
        ep = self.w.endpointd
        gens = [g for g in map(int, GEN.findall(text)) if ep.plan_fits_fleet(g) is not None]
        good = [g for g in gens if g >= int(spec.get("min", 0)) and ep.plan_fits_fleet(g)]
        if spec.get("fits_fleet", True):
            if good:
                return {"plan": f"plan {max(good)}", "gen": str(max(good))}
            return None if gens else {"plan": "the last-known-good plan", "gen": "last-known-good"}
        return {"gen": str(gens[0])} if gens and not good else None

    def _npc_reply(self, item: Item, t: datetime) -> list[dict]:
        npc, msg = item.ev, item.ctx["msg"]
        hit = self.classify(npc, msg["text"])
        if hit is None:
            self.log.append({"id": f"npc:{npc}:silent#{item.n}", "due_at": iso(item.due), "fired_at": None,
                             "status": "skipped", "deliveries": []})
            return []
        rule, ask, values = hit
        self.replied.setdefault((npc, rule["id"]), set()).add(" ".join(ask.lower().split()))
        if rule.get("asks_details"):
            self.awaiting.add(npc)

        def fill(s: str) -> str:
            for k, v in values.items():
                s = s.replace("{" + k + "}", v)
            return s
        text = fill(pick(self.seed, f"{npc}#{item.n}", rule.get("reply_variants") or rule.get("reply", "Noted.")))
        if rule.get("decision") in ("APPROVED", "DECLINED"):
            self.approvals.append({"t": iso(t), "by": npc, "decision": rule["decision"],
                                   "change": fill(rule.get("change", "")), "scope": fill(rule.get("scope", "")),
                                   "rule": rule["id"], "request": ask[:300]})
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
