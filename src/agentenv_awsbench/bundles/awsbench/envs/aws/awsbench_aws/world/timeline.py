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
                 r"request(?:ing)?|requested(?!\s+by\b)|sign[- ]?off|need your|record (?:an? |your )?approved)\b")
CLAUSE = re.compile(r"(?<=[.!?])\s+|\n+|\s\|\s|;\s+|\s+[\u2014\u2013]\s+")
ASIDE = re.compile(r"(?i)^(?:(?:undo|why|auth|rollback(?: plan)?|fallback|held|hold)\s*:|if declined|otherwise\b|"
                   r"not\b|no\b|never\b|without\b|won'?t\b|i'?m not\b|i will not\b|i won'?t\b|do not\b|don'?t\b)")
HEDGE = {"not", "no", "never", "won't", "don't", "didn't", "isn't", "until", "unless", "once", "after", "awaiting",
         "without", "nobody"}
CONTEXT_HEDGE = HEDGE - {"not", "no"} | {"pending"}
GEN = re.compile(r"\b(\d{5})\b")
LOOSE_ASK = re.compile(r"(?i)\?|\bplease\b|\b(?:could|can|would|will) you\b|\bneed (?:you|your)\b|\bapprov(?:e|al|als)\b|"
                       r"\bsign[- ]?off\b|\byour call\b|\bdecision for you\b")
NOUN_HEDGE = re.compile(r"(?i)\b(?:behind|pending|awaiting|after|without)\s+(?:(?:one|an?|the|your|her|his|their|[\w.]+'s)\s+)?$")
FYI_NEAR = r"(?i)(?:@?{n}\W{{0,3}}(?:fyi|cc)\b|\b(?:fyi|cc)\W{{0,3}}@?{n}\b)"
REJECT_BEFORE = re.compile(r"(?i)(?:\bnot\b|n't\b|\bnever\b|\brather than\b|\binstead of\b|\bstale\b)[^,.;()]{0,25}$")
REJECT_AFTER = re.compile(r"(?i)^(?:\s*\([^)]*\))?[^,.;()]{0,30}?\b(?:not|n't|stale|never)\b")
ASKED_BEFORE = re.compile(r"(?i)(?:\b(?:re-?)?appl(?:y|ying)\b|\brestore\b|--gen\b|\bset-applied\b)[^,.;()]{0,25}$")
LEAD_MENTIONS = re.compile(r"^\s*((?:@[\w.-]+[,:]?\s*)+)")
AGAIN = "As I said: "
BULLET = re.compile(r"^(?:[\u2022\u00b7*-]|\(?\d+[.)])\s+")


def clauses(text: str) -> list[str]:
    """The message's clauses without leading @mentions, minus asides (undo plans, holds, negations)."""
    out = []
    for c in CLAUSE.split((text or "").replace("\u2019", "'")):
        c = BULLET.sub("", re.sub(r"^(?:\s*@[\w.-]+[,:]?)+\s*", "", c).strip())
        if c and not ASIDE.match(c):
            out.append(c)
    return out


def asks(text: str) -> list[str]:
    """What the message asks for: each clause from its first unhedged ask word on, or a whole question. A statement
    ("endpointd stays paused until Kwame approves a resume") asks nothing, so it can never draw a decision."""
    out = []
    for c in clauses(text):
        for m in ASK.finditer(c):
            before = c[:m.start()]
            if not (HEDGE & set(re.findall(r"[\w']+", before.lower())[-4:])
                    or m.group(0).lower().startswith("approval") and NOUN_HEDGE.search(before)):
                out.append(c[m.start():])
                break
        else:
            if c.endswith("?"):
                out.append(c)
    return out


def context(text: str) -> list[str]:
    """Every clause a request is read in: the change often sits in one sentence and the ask in another. Hedged
    statements ("paused until Kwame approves a resume", "paused pending Kwame's APPROVED to resume", "which I didn't
    request") stay out; a bare "not" doesn't hedge."""
    return [c for c in clauses(text) if asks(c) or not CONTEXT_HEDGE & set(re.findall(r"[\w']+", c.lower()))]


def plan_gens(text: str) -> tuple[list[int], set[int], list[int]]:
    """The gens a message names, those it rejects ("48177 does NOT match", "not 48177", "rather than 48177", "stale
    48177") and those it asks to apply or restore ("apply --gen 48220", "restore 48199"), each in order."""
    named, rejected, asked = [], set(), []
    for m in GEN.finditer(text or ""):
        g, before, after = int(m.group(1)), text[max(0, m.start() - 60):m.start()], text[m.end():m.end() + 120]
        named.append(g)
        if REJECT_BEFORE.search(before) or REJECT_AFTER.search(after):
            rejected.add(g)
        elif ASKED_BEFORE.search(before):
            asked.append(g)
    return named, rejected, [g for g in asked if g not in rejected]


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
        self.default_cap: dict[str, int] = {}
        self.waiting: dict[str, list[dict]] = {n: [] for n in self.npcs}
        self.world_flags: dict[str, bool] = {}
        self.last_said: dict[str, tuple[datetime, str]] = {}
        self.catch_up: dict[str, datetime] = {}

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
        if item.kind == "page_ack":
            return self._page_ack(item, t)
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
                ctx["result"] = res.get("id", "")
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
                if name == "result":
                    return str(ctx.get("result", ""))
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
                    targets.append((self._npc_for(addr), {"via": "mail", "subject": args.get("subject") or
                                                          resolved.get("subject") or ""}))
        if tool == "pager_page" and str(args.get("urgency", "high")) == "high":
            npc = self._npc_for(str(args.get("user") or args.get("schedule") or args.get("to") or ""))
            if npc and self.awake.get(npc, True) and self.npcs[npc].get("page_ack"):
                body = " ".join(text_of(args.get(k)) for k in ("title", "summary", "details") if args.get(k))
                self.items.append(Item(call["t"] + timedelta(seconds=seconds(self.npcs[npc]["page_ack"])), "page_ack", npc,
                                       0, {"msg": {"t": call["t"], "text": body, "via": "dm"},
                                           "page_id": text_of(dig(call, "result.id") or dig(call, "result.page.id"))},
                                       call["t"]))
        seen = set()
        for npc, how in targets:
            if npc in seen:
                continue
            seen.add(npc)
            self._note_refusal(npc, text)
            msg = {"t": call["t"], "text": text, **how, "fyi": how["via"] == "channel" and self._fyi_only(npc, text)}
            if self.awake.get(npc, True):
                self._schedule_reply(npc, msg, call["t"])
            else:
                self.waiting[npc].append(msg)

    def _fyi_only(self, npc: str, text: str) -> bool:
        """An NPC the message marks FYI or cc ("@kwame.asante FYI", "cc @daniel") is told, not asked."""
        c = self.npcs[npc]
        names = {npc, str(c.get("chat_user", npc)), *c.get("aliases", [])}
        return any(re.search(FYI_NEAR.format(n=re.escape(n)), text or "") for n in names)

    def _page_ack(self, item: Item, t: datetime) -> list[dict]:
        """An awake NPC acks a high page after their `page_ack` delay and answers it like a DM."""
        npc = item.ev
        if item.ctx.get("page_id"):
            self.w.ws_apply("pager.ack_as", {"page_id": item.ctx["page_id"], "user": self.npcs[npc].get("chat_user", npc),
                                             "if_open": True}, t)
        self.log.append({"id": f"npc:{npc}:page-ack", "due_at": iso(item.due), "fired_at": iso(t), "status": "fired",
                         "deliveries": []})
        self._schedule_reply(npc, item.ctx["msg"], t)
        return []

    def _schedule_reply(self, npc: str, msg: dict, t: datetime, not_before: datetime | None = None) -> datetime:
        c = self.npcs[npc]
        lo, hi = (seconds(x) for x in c.get("reply_delay", ["PT1M", "PT2M"]))
        k = self.counts[f"npc:{npc}"] = self.counts.get(f"npc:{npc}", 0) + 1
        due = t + timedelta(seconds=lo + (hi - lo) * unit(self.seed, "reply", npc, k))
        if not_before is not None:
            due = max(due, not_before + timedelta(seconds=15))
        self.items.append(Item(due, "npc_reply", npc, k, {"msg": msg}, t))
        return due

    def _wake(self, npc: str, t: datetime) -> None:
        """A woken NPC answers what queued up in the order it arrived, never a later message first."""
        if self.awake.get(npc):
            return
        self.awake[npc] = True
        prev = None
        if self.npcs[npc].get("catch_up"):
            self.catch_up[npc] = t + timedelta(seconds=seconds(self.npcs[npc]["catch_up"]["for"]))
            prev = self.catch_up[npc] - timedelta(seconds=15)
        for msg in self.waiting.pop(npc, []):
            prev = self._schedule_reply(npc, msg, t, prev)
        self.waiting[npc] = []

    def classify(self, npc: str, text: str, skip: frozenset = frozenset(), fyi: bool = False
                 ) -> tuple[dict, str, dict] | None:
        """The rule that answers `text`, the request it answers and its template values, or None for silence.

        Rules with a `decision` or `on_request` answer only a message that asks something (or details awaited): first
        its ask clauses, then all its clauses (`context`); `ask_only` rules read only the ask clauses. An
        `answers_direct` NPC also reads a direct request without an ask word ("please take IC") with its non-decision
        request rules. Plain rules read the whole text after all of those, so an acknowledgement never answers a
        question another rule answers. An NPC told FYI answers only through plain rules.
        With `skip` (rules whose reply would repeat), only the ask clauses and the default are read.
        `after_refusal` rules wait for `default_once_after`, which caps the default at one more reply.
        Each (npc, rule) answers once, and again only for a request it has not answered (`once` rules never repeat);
        a rule whose `unless_state` holds is skipped. The default answers requests only."""
        c = self.npcs[npc]
        self._note_refusal(npc, text)
        ask_list, ask, loose = [] if fyi else asks(text), "", ""
        if not fyi and (ask_list or npc in self.awaiting):
            self.awaiting.discard(npc)
            ask = " ".join(context(text))
        if not (fyi or ask) and c.get("answers_direct"):
            loose = " ".join(cl for cl in clauses(text) if LOOSE_ASK.search(cl))
        key = " ".join((ask or loose).lower().split())
        rules = [r for r in c.get("rules", []) if r["id"] not in skip
                 and (not r.get("after_refusal") or npc in self.default_cap)]
        for i, scope in enumerate((" ".join(ask_list), ask, loose, text)):
            for rule in rules:
                decides, only = "decision" in rule, bool(rule.get("ask_only"))
                gated = bool(rule.get("on_request")) or decides or only
                if gated != (i < 3) or i == 1 and (only or skip) or i == 2 and decides or gated and not scope:
                    continue
                if not _matches(rule.get("match", {}), scope):
                    continue
                req = loose if i == 2 else ask
                values = {}
                if "plan_gen" in rule:
                    values = self._plan_gen(rule["plan_gen"], req if gated else text, text)
                    if values is None:
                        continue
                if self._spent(npc, rule, key):
                    if gated and not only and i < 2:
                        return None
                    continue
                return rule, req, values
        if fyi:
            return None
        if not ask and loose:
            ask = " ".join(context(text)) or text
            key = " ".join(ask.lower().split())
        if not ask or c.get("default_unless_state") and self.w.eval_state(c["default_unless_state"]):
            return None
        if self.counts.get(f"default:{npc}", 0) >= self.default_cap.get(npc, 1 << 30) or "default" in skip:
            return None
        rule = {"id": "default", "reply": c.get("default", "Noted."), "asks_details": True}
        return None if self._spent(npc, rule, key) else (rule, ask, {})

    def _spent(self, npc: str, rule: dict, key: str) -> bool:
        if rule.get("unless_state") and self.w.eval_state(rule["unless_state"]):
            return True
        seen = self.replied.get((npc, rule["id"]))
        return seen is not None and (bool(rule.get("once")) or not key or key in seen)

    def _note_refusal(self, npc: str, text: str) -> None:
        """`default_once_after`: once a message to the NPC matches it (the agent refused the push), the default
        answers no more."""
        c = self.npcs.get(npc) or {}
        if c.get("default_once_after") and npc not in self.default_cap and re.search(c["default_once_after"], text or "",
                                                                                    re.IGNORECASE):
            self.default_cap[npc] = self.counts.get(f"default:{npc}", 0)

    def _plan_gen(self, spec: dict, text: str, full: str | None = None) -> dict | None:
        """`fits_fleet: true` matches a request naming a plan gen at or above `min` whose ledger records match the
        live fleet (or naming none at all); `fits_fleet: false` matches one asking to apply or restore only gens that
        don't; `fits_fleet: none` matches one naming no fitting gen. Gens the message rejects never count."""
        ep = self.w.endpointd
        _, rejected, _ = plan_gens(full if full is not None else text)
        named, rej, asked = plan_gens(text)
        rejected |= rej
        known = [g for g in named if ep.plan_fits_fleet(g) is not None]
        good = [g for g in known if g not in rejected and g >= int(spec.get("min", 0)) and ep.plan_fits_fleet(g)]
        mode = spec.get("fits_fleet", True)
        if mode is None or mode == "none":
            return {} if not good else None
        if mode:
            if good:
                return {"plan": f"plan {max(good)}", "gen": str(max(good))}
            return None if known else {"plan": "the last-known-good plan", "gen": "last-known-good"}
        stale = [g for g in asked if g not in rejected and ep.plan_fits_fleet(g) is not None and not ep.plan_fits_fleet(g)]
        return {"gen": str(stale[0])} if stale and not good else None

    def _npc_reply(self, item: Item, t: datetime) -> list[dict]:
        """A reworded request for a change the NPC already decided gets "Already approved at ..." and no second record.
        A reply identical to the NPC's last line within 10 min is skipped, as a person wouldn't repeat it, unless a
        request rule's (`on_request` or `after_refusal`) answer was asked for again, directly and after that line: then
        the NPC says "As I said: " and the line."""
        npc, msg = item.ev, item.ctx["msg"]
        until = self.catch_up.get(npc)
        if until and t < until:
            return self._catching_up(npc, item, until, t)
        was_awaiting, skip, again = npc in self.awaiting, set(), False
        while True:
            hit = self.classify(npc, msg["text"], frozenset(skip), bool(msg.get("fyi")))
            last = self.last_said.get(npc)
            if hit is None or not (last and t - last[0] <= timedelta(minutes=10)) or hit[0].get("decision"):
                break
            if self._line(npc, item, hit[0], hit[2]) != last[1].removeprefix(AGAIN):
                break
            if (hit[0].get("on_request") or hit[0].get("after_refusal")) and msg["t"] > last[0] and \
                    self._asked_directly(npc, msg):
                again = True
                break
            skip |= {hit[0]["id"]} | ({"default"} if hit[0].get("on_request") else set())
            self.log.append({"id": f"npc:{npc}:repeat:{hit[0]['id']}#{item.n}", "due_at": iso(item.due),
                             "fired_at": None, "status": "skipped", "deliveries": []})
            if was_awaiting:
                self.awaiting.add(npc)
        if hit is None:
            self.log.append({"id": f"npc:{npc}:silent#{item.n}", "due_at": iso(item.due), "fired_at": None,
                             "status": "skipped", "deliveries": []})
            return []
        rule, ask, values = hit
        self.replied.setdefault((npc, rule["id"]), set()).add(" ".join(ask.lower().split()))
        decision, change, rid = rule.get("decision"), self._fill(rule.get("change", ""), values), rule["id"]
        prior = next((a for a in self.approvals if decision in ("APPROVED", "DECLINED") and a["by"] == npc
                      and a["rule"] == rule["id"] and a["change"] == change), None)
        if prior:
            hhmm = datetime.fromisoformat(prior["t"]).strftime("%H:%M")
            text = (f"Already approved at {hhmm} ET: {change}. Post the CHANGE record." if decision == "APPROVED"
                    else f"Already declined at {hhmm} ET.")
            rid += "#repeat"
        else:
            text = self._line(npc, item, rule, values)
            if again:
                text, rid = AGAIN + text, rid + "#again"
        if rule.get("asks_details"):
            self.awaiting.add(npc)
            self.counts[f"default:{npc}"] = self.counts.get(f"default:{npc}", 0) + 1
        if decision in ("APPROVED", "DECLINED") and not prior:
            self.approvals.append({"t": iso(t), "by": npc, "decision": decision, "change": change,
                                   "scope": self._fill(rule.get("scope", ""), values), "rule": rule["id"],
                                   "request": ask[:300]})
        entry = {"id": f"npc:{npc}:{rid}#{item.n}", "due_at": iso(item.due), "fired_at": iso(t),
                 "status": "fired", "deliveries": self._deliver(npc, msg, text, t)}
        self.log.append(entry)
        return entry["deliveries"]

    def _fill(self, s: str, values: dict) -> str:
        """A line's `{plan}`/`{gen}` from the request and `{outbox_pending}`/`{dlq}` from the world now."""
        if "{" not in s:
            return s
        storm = self.w.storm
        for k, v in {"outbox_pending": f"{round(storm.outbox):,}", "dlq": str(storm.dlq), **values}.items():
            s = s.replace("{" + k + "}", v)
        return s

    def _line(self, npc: str, item: Item, rule: dict, values: dict) -> str:
        return self._fill(pick(self.seed, f"{npc}#{item.n}", rule.get("reply_variants") or rule.get("reply", "Noted.")),
                          values)

    def _asked_directly(self, npc: str, msg: dict) -> bool:
        """A request sent to the NPC itself: a DM, mail or page, or a post that opens with its @mention."""
        text = msg.get("text") or ""
        lead = LEAD_MENTIONS.match(text)
        to_me = msg.get("via") != "channel" or bool(lead and any(self._npc_for(m) == npc
                                                                  for m in re.findall(r"@([\w.-]+)", lead.group(1))))
        return to_me and bool(asks(text) or self.npcs[npc].get("answers_direct") and
                              any(LOOSE_ASK.search(c) for c in clauses(text)))

    def _catching_up(self, npc: str, item: Item, until: datetime, t: datetime) -> list[dict]:
        """Before `catch_up.for` after waking, the NPC says its catch-up line once and answers after the window."""
        out = []
        if not self.counts.get(f"catch_up:{npc}"):
            self.counts[f"catch_up:{npc}"] = 1
            out = self._deliver(npc, item.ctx["msg"], self.npcs[npc]["catch_up"]["reply"], t)
            self.log.append({"id": f"npc:{npc}:R0-catching-up#{item.n}", "due_at": iso(item.due), "fired_at": iso(t),
                             "status": "fired", "deliveries": out})
        self._schedule_reply(npc, item.ctx["msg"], t, until)
        return out

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
        self.last_said[npc] = (t, text)
        if msg["via"] == "dm":
            res = self.w.ws_apply("chat.dm_as", {"from_user": user, "to_user": self.me.get("chat", "sre-primary"),
                                                 "text": text}, t)
        elif msg["via"] == "mail":
            subj = msg.get("subject") or "(no subject)"
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
