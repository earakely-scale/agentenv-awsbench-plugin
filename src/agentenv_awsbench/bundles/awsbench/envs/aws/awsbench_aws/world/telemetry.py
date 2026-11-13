"""AWS reads that physics owns: CloudWatch metrics and alarms, CloudWatch Logs, SQS queue depth and the outbox table."""

import hashlib
import math
import operator
import re
from bisect import bisect_right
from datetime import UTC, datetime, timedelta

from ..account import ACCOUNT
from ..errors import AwsError
from .physics import FAILURE
from .util import iso, parse_time

# name, namespace, metric, comparison, threshold, evaluation periods
ALARMS = [
    ("checkout-api-CheckoutSuccessRate-critical", "Vantleaf/Checkout", "CheckoutSuccessRate", "LessThanThreshold",
     0.95, 1),
    ("ledger-writer-p99-high", "Vantleaf/LedgerWriter", "P99LatencyMs", "GreaterThanThreshold", 1000, 3),
    ("webhooks-backlog-high", "AWS/SQS", "ApproximateNumberOfMessagesVisible", "GreaterThanThreshold", 30000, 1),
    ("endpointd-ApplyLagSeconds-high", "Vantleaf/Endpointd", "ApplyLagSeconds", "GreaterThanThreshold", 300, 1),
    ("ledger-ingress-EnvoyNotReady", "Vantleaf/LedgerIngress", "EnvoyNotReady", "GreaterThanThreshold", 0, 1),
]
APP_GROUPS = {"/vl/prod/checkout-api": "checkout", "/vl/prod/webhooks-dispatcher": "dispatcher",
              "/vl/prod/ledger-writer": "ledger"}
OUTBOX = "vl-ledger-outbox"
LEDGER = "ledger.internal.vantleaf.io"
BREAKER_REVISION = 412
SUPPORTED = "supported: fields, filter @message like/=~, sort @timestamp, limit, stats count() by bin()/@log"
_FIELDS = ("@timestamp", "@message", "@log", "@logStream")
ACME_LOG = "/vl/platform/acme-renewer"
_TERM = re.compile(r"\s*(?:(and|or)\s+)?@message\s+(not\s+like|like|=~)\s+(/(?:\\.|[^/\\])*/|\"[^\"]*\"|'[^']*')\s*",
                   re.IGNORECASE)
_BIN = re.compile(r"bin\(\s*(\d+)\s*(s|m|h)\s*\)$")
_STATS = re.compile(r"(count\(\s*\*?\s*\))\s*(?:as\s+(\w+))?\s*(?:by\s+(.+))?")


_DDB_TOKEN = re.compile(r"\s*(<>|<=|>=|[=<>(),]|[#:]?[A-Za-z_][\w.-]*)")
_DDB_CMP = {"=": operator.eq, "<>": operator.ne, "<": operator.lt, "<=": operator.le, ">": operator.gt,
            ">=": operator.ge}
_DDB_FUNCS = {"attribute_exists": (1, lambda a: a is not None), "attribute_not_exists": (1, lambda a: a is None),
              "begins_with": (2, lambda a, b: _ddb_same(a, b) and a[1].startswith(b[1])),
              "contains": (2, lambda a, b: _ddb_same(a, b) and b[1] in a[1])}
_DDB_RESERVED = {"status", "state", "name", "date", "time", "timestamp", "type", "value", "data", "count", "key"}
DDB_SUPPORTED = ("supported: = <> < <= > >=, begins_with, contains, attribute_exists, attribute_not_exists, AND, OR, "
                 "NOT, parentheses")


def _ddb_same(a, b) -> bool:
    return a is not None and b is not None and a[0] == b[0]


class DdbCondition:
    """A DynamoDB condition expression over an item's string attributes, as a predicate (`test`); anything outside
    the subset is a ValidationException naming it, so no read is answered unfiltered."""

    def __init__(self, text: str, names: dict | None, values: dict | None, what: str):
        self.names, self.values, self.what, self.attrs = names or {}, values or {}, what, set()
        self.toks, pos = [], 0
        while text[pos:].strip():
            m = _DDB_TOKEN.match(text, pos)
            if not m:
                self._bad(text[pos:].split()[0])
            self.toks.append(m.group(1))
            pos = m.end()
        self.i = 0
        self.test = self._join("OR", any)
        if self.i < len(self.toks):
            self._bad(self.toks[self.i])

    def _bad(self, tok):
        raise AwsError("ValidationException", f"Invalid {self.what}: unsupported in this environment: {tok}; "
                                              f"{DDB_SUPPORTED}")

    def _err(self, msg):
        raise AwsError("ValidationException", f"Invalid {self.what}: {msg}")

    def _peek(self, k: int = 0) -> str:
        return self.toks[self.i + k] if self.i + k < len(self.toks) else ""

    def _take(self, want: str | None = None) -> str:
        tok = self._peek()
        if not tok or want and tok != want:
            self._bad(tok or "end of expression")
        self.i += 1
        return tok

    def _join(self, word: str, fold):
        parts = [self._join("AND", all) if word == "OR" else self._not()]
        while self._peek().upper() == word:
            self.i += 1
            parts.append(self._join("AND", all) if word == "OR" else self._not())
        return parts[0] if len(parts) == 1 else lambda it: fold(p(it) for p in parts)

    def _not(self):
        if self._peek().upper() == "NOT":
            self.i += 1
            inner = self._not()
            return lambda it: not inner(it)
        if self._peek() == "(":
            self.i += 1
            inner = self._join("OR", any)
            self._take(")")
            return inner
        if self._peek(1) == "(":
            name = self._take()
            arity, fn = _DDB_FUNCS.get(name.lower()) or self._bad(name)
            self.i += 1
            args = [self._operand()]
            while self._peek() == ",":
                self.i += 1
                args.append(self._operand())
            self._take(")")
            if len(args) != arity:
                self._bad(name)
            return lambda it: fn(*(a(it) for a in args))
        left, op = self._operand(), self._take()
        if op not in _DDB_CMP:
            self._bad(op)
        right = self._operand()
        if op == "<>":
            return lambda it: not (_ddb_same(a := left(it), b := right(it)) and a[1] == b[1])
        return lambda it: _ddb_same(a := left(it), b := right(it)) and _DDB_CMP[op](a[1], b[1])

    def _operand(self):
        tok = self._take()
        if tok.startswith(":"):
            if tok not in self.values:
                self._err(f"An expression attribute value used in expression is not defined; attribute value: {tok}")
            kind, raw = next(iter(self.values[tok].items()))
            val = (kind, float(raw) if kind == "N" else raw)
            return lambda it: val
        if tok.startswith("#"):
            if tok not in self.names:
                self._err(f"An expression attribute name used in the document path is not defined; attribute name: "
                          f"{tok}")
            name = self.names[tok]
        elif tok.lower() in _DDB_RESERVED:
            self._err(f"Attribute name is a reserved keyword; reserved keyword: {tok}")
        elif not tok[0].isalpha() and tok[0] != "_" or tok.upper() in ("AND", "OR", "NOT", "BETWEEN", "IN"):
            self._bad(tok)
        else:
            name = tok
        self.attrs.add(name)
        return lambda it: ("S", it[name]["S"]) if name in it else None


def _utc(t) -> datetime:
    if isinstance(t, (int, float)):
        return datetime.fromtimestamp(t / 1000 if t > 1e11 else t, UTC)
    if isinstance(t, str):
        return datetime.fromisoformat(t.replace("Z", "+00:00"))
    return t.astimezone(UTC)


def _group(g) -> str:
    g = str(g or "")
    return g.split("log-group:", 1)[1].removesuffix(":*") if "log-group:" in g else g


def _stat(vals: list[float], s: str) -> float:
    if s == "Average":
        return round(sum(vals) / len(vals), 4)
    if s == "Sum":
        return round(sum(vals), 4)
    if s == "SampleCount":
        return float(len(vals))
    if s in ("Minimum", "Maximum"):
        return (min if s == "Minimum" else max)(vals)
    return sorted(vals)[max(0, math.ceil(float(s[1:]) / 100 * len(vals)) - 1)]


def _fmt(v: float) -> str:
    return f"{v:.4f}".rstrip("0").rstrip(".")


def _split(qs: str) -> list[str]:
    """Top-level `|` commands, keeping pipes inside /regex/ and quotes."""
    out, buf, quote, i = [], "", None, 0
    while i < len(qs):
        ch = qs[i]
        if quote and ch == "\\":
            buf, i = buf + qs[i:i + 2], i + 2
            continue
        if quote:
            quote = None if ch == quote else quote
        elif ch in "/'\"":
            quote = ch
        elif ch == "|":
            out, buf, i = out + [buf.strip()], "", i + 1
            continue
        buf, i = buf + ch, i + 1
    return [c for c in out + [buf.strip()] if c]


def _filter(expr: str):
    """`@message like|=~|not like /re/ or "s"` terms joined by and/or, as a predicate (an or of ands)."""
    pos, ors = 0, [[]]
    while pos < len(expr):
        m = _TERM.match(expr, pos)
        if not m or (m.group(1) is None) != (pos == 0):
            raise ValueError(expr)
        lit, neg = m.group(3), m.group(2).lower().startswith("not")
        test = re.compile(lit[1:-1]).search if lit.startswith("/") else (lambda s, n=lit[1:-1]: n in s)
        if (m.group(1) or "").lower() == "or":
            ors.append([])
        ors[-1].append(lambda s, f=test, n=neg: bool(f(s)) != n)
        pos = m.end()
    return lambda s: any(all(p(s) for p in ands) for ands in ors)


SUPPORTED_X = ("supported: fields/display, filter on @message, @timestamp, @log, level or a parsed field (like, not like, "
               "=~, =, !=, <, <=, >, >=, in [...], not in [...]) joined by and/or, parse @message /(?<name>re)/, "
               "stats count(*)/count(f)/min(f)/max(f) [as name] [by bin(5m), @log or fields], sort <field> asc|desc, "
               "limit")
_XTERM = re.compile(r"\s*(?:(and|or)\s+)?(@?\w+)\s+(not\s+like|like|=~|not\s+in|in|!=|<=|>=|==|=|<|>)\s*"
                    r"(/(?:\\.|[^/\\])*/|\"[^\"]*\"|'[^']*'|\[[^\]]*\]|[-\w.:]+)\s*", re.IGNORECASE)
_AGG = re.compile(r"(count|min|max)\(\s*(\*|@?\w*)\s*\)(?:\s+as\s+(\w+))?$", re.IGNORECASE)
_LEVELS = ("ERROR", "WARN", "WARNING", "INFO", "DEBUG", "FATAL")


def _top(text: str, sep: str = ",") -> list[str]:
    """`text` split on `sep` outside brackets and quotes."""
    out, buf, depth, quote = [], "", 0, None
    for ch in text:
        if quote:
            quote = None if ch == quote else quote
        elif ch in "'\"":
            quote = ch
        elif ch in "([":
            depth += 1
        elif ch in ")]":
            depth -= 1
        elif ch == sep and depth == 0:
            out, buf = out + [buf.strip()], ""
            continue
        buf += ch
    return [x for x in out + [buf.strip()] if x]


def _lit(tok: str):
    if tok[0] in "'\"":
        return tok[1:-1]
    try:
        return float(tok)
    except ValueError:
        return tok


def _ms(v):
    """A record's or a literal's time as epoch ms: numbers above 1e11 are ms, smaller ones seconds, text ISO 8601."""
    if isinstance(v, (int, float)):
        return v if v > 1e11 else v * 1000
    return _utc(str(v)).timestamp() * 1000


def _xfilter(expr: str):
    """`<field> <op> <literal>` terms joined by and/or, as a predicate over a record (an or of ands)."""
    pos, ors = 0, [[]]
    while pos < len(expr):
        m = _XTERM.match(expr, pos)
        if not m or (m.group(1) is None) != (pos == 0):
            raise ValueError(expr)
        field, op, raw = m.group(2), re.sub(r"\s+", " ", m.group(3).lower()), m.group(4)
        if op in ("like", "not like", "=~"):
            needle = _lit(raw)
            find = re.compile(raw[1:-1]).search if raw.startswith("/") else (lambda s, n=needle: n in s)
            test = (lambda v, f=find, neg=op == "not like": bool(f(str(v))) != neg)
        elif op in ("in", "not in"):
            if not raw.startswith("["):
                raise ValueError(raw)
            vals = {_lit(x) for x in _top(raw[1:-1])}
            test = (lambda v, s=vals, neg=op == "not in": (v in s) != neg)
        else:
            want, cmp = _lit(raw), {"=": operator.eq, "==": operator.eq, "!=": operator.ne, "<": operator.lt,
                                    "<=": operator.le, ">": operator.gt, ">=": operator.ge}[op]
            if field == "@timestamp":
                want = _ms(want)
            test = (lambda v, w=want, c=cmp: c(float(v), w) if isinstance(w, float) and _num(v) else c(str(v), str(w)))
        if (m.group(1) or "").lower() == "or":
            ors.append([])
        ors[-1].append(lambda r, f=field, t=test: r.get(f) is not None and t(r[f]))
        pos = m.end()
    return lambda r: any(all(p(r) for p in ands) for ands in ors)


def _num(v) -> bool:
    try:
        float(v)
        return True
    except (TypeError, ValueError):
        return False


def _xstats(rest: str) -> dict:
    head, _, by = re.sub(r"\s+by\s+", "\0", rest, count=1, flags=re.IGNORECASE).partition("\0")
    aggs = []
    for part in _top(head):
        m = _AGG.fullmatch(part)
        if not m:
            raise ValueError(part)
        fn, arg = m.group(1).lower(), m.group(2) or "*"
        aggs.append((m.group(3) or f"{fn}({arg})", fn, arg))
    keys = []
    for b in _top(by):
        m = _BIN.match(b)
        if not m and not re.fullmatch(r"@?\w+", b):
            raise ValueError(b)
        keys.append((b, int(m.group(1)) * {"s": 1, "m": 60, "h": 3600}[m.group(2)] * 1000 if m else 0))
    return {"aggs": aggs, "keys": keys}


def _xplan(qs: str) -> list:
    ops = []
    for cmd in _split(qs):
        name, _, rest = cmd.partition(" ")
        name, rest = name.lower(), rest.strip()
        try:
            if name == "filter":
                ops.append(("filter", _xfilter(rest)))
            elif name in ("fields", "display"):
                ops.append(("fields", _top(rest)))
            elif name == "parse":
                m = re.fullmatch(r"@message\s+/((?:\\.|[^/\\])*)/", rest)
                ops.append(("parse", re.compile(re.sub(r"\(\?<(\w+)>", r"(?P<\1>", m.group(1)))))
            elif name == "sort":
                m = re.fullmatch(r"(@?\w+)(?:\s+(asc|desc))?", rest, re.IGNORECASE)
                ops.append(("sort", (m.group(1), (m.group(2) or "desc").lower())))
            elif name == "limit":
                ops.append(("limit", int(rest)))
            elif name == "stats":
                ops.append(("stats", _xstats(rest)))
            else:
                raise ValueError(name)
        except (ValueError, AttributeError, KeyError, re.error):
            raise AwsError("MalformedQueryException", f"unsupported in this environment: {cmd}; {SUPPORTED_X}",
                           400) from None
    return ops


def _groups(rx, text: str) -> dict:
    m = rx.search(text)
    return {k: v for k, v in m.groupdict().items() if v is not None} if m else {}


def _stamp(ms) -> str:
    return datetime.fromtimestamp(float(ms) / 1000, UTC).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]


def run_insights(ops: list, records: list[dict], limit: int, default_fields: list[str]) -> tuple[list, int]:
    """A parsed query (`_xplan`) over records keyed by field (`@timestamp` in epoch ms): (result rows, matched)."""
    rows = sorted(records, key=lambda r: r["@timestamp"], reverse=True)
    shown, stamps, matched = None, {"@timestamp"}, len(records)
    for op, arg in ops:
        if op == "parse":
            rows = [{**r, **_groups(arg, r["@message"])} for r in rows]
        elif op == "filter":
            rows = [r for r in rows if arg(r)]
            matched = len(rows)
        elif op == "fields":
            shown = arg
        elif op == "sort":
            have = [r for r in rows if r.get(arg[0]) is not None]
            have.sort(key=lambda r: (0, float(r[arg[0]])) if _num(r[arg[0]]) else (1, str(r[arg[0]])),
                      reverse=arg[1] == "desc")
            rows = have + [r for r in rows if r.get(arg[0]) is None]
        elif op == "limit":
            rows = rows[:arg]
        elif op == "stats":
            groups: dict[tuple, list] = {}
            for r in rows:
                key = tuple(int(_ms(r["@timestamp"]) // size * size) if size else r.get(k) for k, size in arg["keys"])
                if None not in key:
                    groups.setdefault(key, []).append(r)
            out = []
            for key, members in sorted(groups.items(), key=lambda kv: [str(k) for k in kv[0]]):
                row = {k: v for (k, _), v in zip(arg["keys"], key)}
                for name, fn, f in arg["aggs"]:
                    vals = [m.get(f, 1) for m in members if f == "*" or m.get(f) is not None]
                    nums = [float(v) for v in vals] if vals and all(_num(v) for v in vals) else None
                    row[name] = len(vals) if fn == "count" else (min if fn == "min" else max)(nums or vals) if vals \
                        else None
                    if f == "@timestamp" and fn != "count":
                        stamps.add(name)
                out.append(row)
            stamps |= {k for k, size in arg["keys"] if size}
            rows, shown = out, [k for k, _ in arg["keys"]] + [a[0] for a in arg["aggs"]]
    if not any(op == "stats" for op, _ in ops) and not any(op == "sort" for op, _ in ops):
        rows = sorted(rows, key=lambda r: r["@timestamp"], reverse=True)

    def show(f, v):
        return None if v is None else _stamp(v) if f in stamps else (
            str(int(v)) if isinstance(v, float) and v.is_integer() else str(v))
    return [[{"field": f, "value": show(f, r.get(f))} for f in (shown or default_fields) if r.get(f) is not None]
            for r in rows[:limit]], matched


def parse_query(qs: str, extended: bool = False) -> dict:
    """The Logs Insights subset this world serves; anything else is a MalformedQueryException, so no query is
    silently half-answered. `extended` also takes parse, stats by fields and filters on fields, as `ops`."""
    try:
        return _parse_basic(qs)
    except AwsError:
        if not extended:
            raise
    return {"filters": [], "sort": None, "limit": None, "fields": None, "stats": None, "ops": _xplan(qs)}


def _parse_basic(qs: str) -> dict:
    plan = {"filters": [], "sort": None, "limit": None, "fields": None, "stats": None}
    for cmd in _split(qs):
        name, _, rest = cmd.partition(" ")
        name, rest = name.lower(), rest.strip()
        try:
            if name == "filter":
                plan["filters"].append(_filter(rest))
            elif name == "fields":
                plan["fields"] = [f.strip() for f in rest.split(",") if f.strip() in _FIELDS]
            elif name == "sort":
                m = re.fullmatch(r"@timestamp(?:\s+(asc|desc))?", rest, re.IGNORECASE)
                plan["sort"] = (m.group(1) or "asc").lower()
            elif name == "limit":
                plan["limit"] = int(rest)
            elif name == "stats":
                m = _STATS.fullmatch(rest)
                by = [b.strip() for b in (m.group(3) or "").split(",") if b.strip()]
                bins = [_BIN.match(b) for b in by if b != "@log"]
                if any(b is None for b in bins) or len(bins) > 1:
                    raise ValueError(rest)
                plan["stats"] = {"name": m.group(2) or re.sub(r"\s", "", m.group(1)), "by": by, "bin_s":
                                 int(bins[0].group(1)) * {"s": 1, "m": 60, "h": 3600}[bins[0].group(2)] if bins else 0}
            else:
                raise ValueError(name)
        except (ValueError, AttributeError, re.error):
            raise AwsError("MalformedQueryException", f"unsupported in this environment: {cmd}; {SUPPORTED}",
                           400) from None
    return plan


class Telemetry:
    def __init__(self, world, cfg: dict):
        self.w = world
        self.cfg = cfg
        self.minutes: list[dict] = []
        self._mts: list[datetime] = []
        self.alarms: dict[str, dict] = {}
        self.queries: dict[str, dict] = {}
        self.outage_start = datetime.fromisoformat(cfg.get("outage_start", "2026-11-17T02:31:42-05:00"))
        self.extra_logs: list[tuple] = []
        self.alarm_since: dict[str, datetime] = {}
        self.groups: set[str] = set()
        self._gen = 0
        if world is None:
            return
        self.extra_logs = [(parse_time(e["t"], world.start_t), e["group"], e["message"]) for e in cfg.get("logs", [])]
        self.alarm_since = {k: parse_time(v, world.start_t) for k, v in (cfg.get("alarm_since") or {}).items()}
        self._gen = world.endpointd.next_gen
        self.groups = {r["params"]["logGroupName"] for r in world.scenario.get("aws_seed", [])
                       if r.get("service") == "logs" and r.get("operation") == "CreateLogGroup"}
        self.groups |= set(re.findall(r"'log_group': '([^']+)'", str(world.scenario["world"].get("fleets"))))

    def tick(self, t: datetime) -> None:
        self._planner_lines(t)
        if t.second >= self.w.tick_s and self.minutes:
            return
        s, ep, lev = self.w.storm, self.w.endpointd, self.w.levers()
        m = {"t": t.replace(second=0, microsecond=0), "checkout": self.w.checkout_success_1m(),
             "p99": s.latency_ms(), "backlog": s.backlog, "offered": s.offered, "capacity": s.capacity,
             "regime": s.regime, "lw_tasks": s.lw_running, "conns": min(400, s.lw_running * 25),
             "outbox": s.outbox, "records": len(self.w.mirror.live(ep.ledger)),
             "ledger_correct": ep.correct(ep.ledger), "latch": (ep.latch_reasons() or [None])[0],
             "paused": lev["consumer_paused"], "rejected": s.rejected, "quota": lev["dispatcher_quota"],
             "rev": s.checkout_rev,
             "envoy": sum(1 for h in self.w.fleet.ingress() if not h.get("ready")),
             "recovered": [h for h in self.w.core_hosts if self.w.dns.resolves(h, t)]}
        if self.minutes and self.minutes[-1]["t"] == m["t"]:
            self.minutes[-1] = m
        else:
            self.minutes.append(m)
            self._mts.append(m["t"])
        p99_hot = len(self.minutes) >= 3 and all(x["p99"] > 1000 for x in self.minutes[-3:])
        states = {"checkout-api-CheckoutSuccessRate-critical": (m["checkout"] < 0.95, round(m["checkout"], 4)),
                  "ledger-writer-p99-high": (p99_hot, m["p99"]),
                  "webhooks-backlog-high": (m["backlog"] > 30000, round(m["backlog"])),
                  "endpointd-ApplyLagSeconds-high": (not m["ledger_correct"],
                                                     self.value("Vantleaf/Endpointd", "ApplyLagSeconds", {}, t)),
                  "ledger-ingress-EnvoyNotReady": (m["envoy"] > 0, m["envoy"])}
        for name, (on, dp) in states.items():
            v = "ALARM" if on else "OK"
            a = self.alarms.setdefault(name, {"state": None, "since": t, "dps": []})
            a["dps"] = (a["dps"] + [dp])[-3:]
            if a["state"] != v:
                since = self.alarm_since.get(name) if a["state"] is None and on else None
                a.update(state=v, since=since or t)

    def backfill(self, start: datetime) -> None:
        """Minute records from the outage to the episode start, which the world never ticks: the start's own state, with
        the outbox and webhook backlog growing from nothing at the outage as physics grows them."""
        span = (start - self.outage_start).total_seconds()
        if not self.minutes or span <= 0:
            return
        m0, t = self.minutes[0], self.outage_start.astimezone(start.tzinfo).replace(second=0, microsecond=0)
        rows = []
        while t < m0["t"]:
            f = max(0.0, (t - self.outage_start).total_seconds()) / span
            rows.append({**m0, "t": t, "outbox": m0["outbox"] * f, "backlog": m0["backlog"] * f})
            t += timedelta(minutes=1)
        self.minutes[:0], self._mts[:0] = rows, [r["t"] for r in rows]

    def _planner_lines(self, t: datetime) -> None:
        """The planner logs each plan it writes, with how many ledger records changed from the plan before."""
        ep = self.w.endpointd
        while self._gen < ep.next_gen:
            prev, cur = ep.archive.get(self._gen - 1), ep.archive.get(self._gen) or []
            recs = [{(r["ip"], r["weight"]) for r in x or []} for x in (prev, cur)]
            changes = len(recs[0] ^ recs[1]) if prev is not None else 0
            self.extra_logs.append((t, "/vl/platform/endpointd/planner",
                                    f"INFO planner gen={self._gen} endpoints={len(ep.order)} changes={changes}"))
            self._gen += 1

    def alarm_state(self, name: str) -> str:
        return (self.alarms.get(name) or {}).get("state") or "INSUFFICIENT_DATA"

    # ---- values ----
    def baseline_req_s(self) -> float:
        """Healthy ledger-writer load from the same physics: checkout, webhook arrivals and everything else."""
        c = self.w.storm.cfg
        return sum(float(c.get(k, d)) for k, d in (("checkout_req_s", 90), ("webhook_arrivals_per_s", 35),
                                                    ("other_req_s", 150)))

    def value(self, ns: str, metric: str, dims: dict, t: datetime) -> float | None:
        m = self._minute(t)
        pre = t < self.outage_start
        if m is None and not pre:
            return None
        key = (ns, metric)
        if key == ("Vantleaf/Checkout", "CheckoutSuccessRate"):
            return 0.997 if pre else round(m["checkout"], 4)
        if key == ("Vantleaf/Checkout", "OutboxPending"):
            return 0.0 if pre else float(round(m["outbox"]))
        if key == ("Vantleaf/LedgerWriter", "P99LatencyMs"):
            return 120.0 if pre else m["p99"]
        if key == ("Vantleaf/LedgerWriter", "Requests"):
            return round(self.baseline_req_s() * 60) if pre else round(m["offered"] * 60)
        if key == ("Vantleaf/LedgerWriter", "Rejected429"):
            return 0.0 if pre else float(round(m["rejected"] * 60))
        if key == ("AWS/SQS", "ApproximateNumberOfMessagesVisible"):
            return 0.0 if pre else round(m["backlog"])
        if key == ("Vantleaf/Webhooks", "BacklogPerTask"):
            return 0.0 if pre else round(m["backlog"] / max(1, self.w.storm.dispatcher_running()), 1)
        if key == ("AWS/RDS", "DatabaseConnections"):
            return float(min(400, int(self.w.storm.cfg.get("ledger_writer_tasks", 12)) * 25) if pre else m["conns"])
        if key == ("Vantleaf/Endpointd", "EndpointRecordCount"):
            return 6.0 if pre else float(m["records"])
        if key == ("Vantleaf/Endpointd", "ApplyLagSeconds"):
            return 4.0 if pre or m["ledger_correct"] else (t - self.outage_start).total_seconds()
        if key == ("AWS/ECS", "RunningTaskCount"):
            svc = dims.get("ServiceName", "")
            if self.w.ecs is not None:
                return float(self.w.ecs.counts(svc)[1]) if svc in self.w.ecs.services else None
            return float(self.w.storm.lw_running if "ledger-writer" in svc else
                         self.w.storm.dispatcher_running() if "webhooks" in svc else 18)
        return None

    def _minute(self, t: datetime):
        i = bisect_right(self._mts, t.astimezone(self.w.start_t.tzinfo).replace(second=0, microsecond=0)) - 1
        return self.minutes[i] if i >= 0 else None

    def aggregate(self, ns, metric, dims, start, end, period, stats) -> list[tuple[datetime, dict]]:
        """Each statistic over every minute sample in [t, t+period), as CloudWatch aggregates a period."""
        start, end = _utc(start), min(_utc(end), self.w.now.astimezone(UTC))
        period = max(60, int(period or 60))
        out = []
        t = start.replace(second=0, microsecond=0)
        while t < end and len(out) < 1440:
            stop, x, vals = min(t + timedelta(seconds=period), end), t, []
            while x < stop:
                if (v := self.value(ns, metric, dims, x)) is not None:
                    vals.append(v)
                x += timedelta(minutes=1)
            if vals:
                out.append((t, {s: _stat(vals, s) for s in stats}))
            t += timedelta(seconds=period)
        return out

    # ---- dispatch ----
    def serve(self, service: str, op: str, p: dict, now: datetime):
        service = service.lower()
        if service in ("cloudwatch", "monitoring"):
            return self._cloudwatch(op, p, now)
        if service == "logs":
            return self._logs(op, p, now)
        if service == "dynamodb" and p.get("TableName") == OUTBOX:
            return self._outbox(op, p, now)
        if service == "sqs" and op == "GetQueueAttributes" and "vl-webhook-delivery" in str(p.get("QueueUrl")):
            dlq = str(p.get("QueueUrl")).endswith("-dlq")
            visible = self.w.storm.dlq if dlq else round(self.w.storm.backlog)
            busy = 0 if dlq or self.w.levers()["consumer_paused"] else min(visible,
                                                                            self.w.storm.dispatcher_running() * 10)
            attrs = {"ApproximateNumberOfMessages": str(visible), "ApproximateNumberOfMessagesNotVisible": str(busy),
                     "ApproximateNumberOfMessagesDelayed": "0", "VisibilityTimeout": "0" if not dlq else "30",
                     "MessageRetentionPeriod": "345600"}
            want = p.get("AttributeNames") or ["All"]
            return {"Attributes": attrs if "All" in want else {k: v for k, v in attrs.items() if k in want}}
        return None

    def _cloudwatch(self, op: str, p: dict, now: datetime):
        if op == "GetMetricData":
            res = []
            for q in p.get("MetricDataQueries", []):
                ms = (q.get("MetricStat") or {})
                met = ms.get("Metric") or {}
                dims = {d["Name"]: d["Value"] for d in met.get("Dimensions", [])}
                stat = ms.get("Stat") or "Average"
                pts = self.aggregate(met.get("Namespace"), met.get("MetricName"), dims, p["StartTime"], p["EndTime"],
                                     ms.get("Period", 60), [stat])
                pts = pts if p.get("ScanBy") == "TimestampAscending" else pts[::-1]
                res.append({"Id": q.get("Id"), "Label": q.get("Label") or met.get("MetricName"),
                            "Timestamps": [t for t, _ in pts], "Values": [v[stat] for _, v in pts],
                            "StatusCode": "Complete"})
            return {"MetricDataResults": res, "Messages": []}
        if op == "GetMetricStatistics":
            dims = {d["Name"]: d["Value"] for d in p.get("Dimensions", [])}
            if self.value(p.get("Namespace"), p.get("MetricName"), dims, now) is None:
                return None
            ext = p.get("ExtendedStatistics") or []
            stats = p.get("Statistics") or ([] if ext else ["Average"])
            pts = self.aggregate(p["Namespace"], p["MetricName"], dims, p["StartTime"], p["EndTime"],
                                 p.get("Period", 60), stats + ext)
            return {"Label": p["MetricName"], "Datapoints": [
                {"Timestamp": t, **{s: v[s] for s in stats},
                 **({"ExtendedStatistics": {e: v[e] for e in ext}} if ext else {}), "Unit": "None"}
                for t, v in pts[::-1]]}
        if op == "ListMetrics":
            names = [("Vantleaf/Checkout", "CheckoutSuccessRate", [{"Name": "Service", "Value": "checkout-api"}]),
                     ("Vantleaf/Checkout", "OutboxPending", [{"Name": "Service", "Value": "checkout-api"}]),
                     ("Vantleaf/LedgerWriter", "P99LatencyMs", [{"Name": "Service", "Value": "ledger-writer"}]),
                     ("Vantleaf/LedgerWriter", "Requests", [{"Name": "Service", "Value": "ledger-writer"}]),
                     ("Vantleaf/LedgerWriter", "Rejected429", [{"Name": "Service", "Value": "ledger-writer"}]),
                     ("Vantleaf/Webhooks", "BacklogPerTask", [{"Name": "Service", "Value": "webhooks-dispatcher"}]),
                     ("Vantleaf/Endpointd", "EndpointRecordCount",
                      [{"Name": "Endpoint", "Value": "ledger.internal.vantleaf.io"}]),
                     ("Vantleaf/Endpointd", "ApplyLagSeconds", [{"Name": "Environment", "Value": "prod"}]),
                     ("AWS/SQS", "ApproximateNumberOfMessagesVisible",
                      [{"Name": "QueueName", "Value": "vl-webhook-delivery"}]),
                     ("AWS/RDS", "DatabaseConnections", [{"Name": "DBClusterIdentifier", "Value": "vl-ledger-db"}])]
            ns = p.get("Namespace")
            return {"Metrics": [{"Namespace": n, "MetricName": m, "Dimensions": d} for n, m, d in names
                                if not ns or ns == n and (not p.get("MetricName") or p["MetricName"] == m)]}
        if op == "DescribeAlarms":
            want = set(p.get("AlarmNames") or [])
            prefix = p.get("AlarmNamePrefix", "")
            out = []
            for name, ns, metric, cmp, thr, periods in ALARMS:
                if want and name not in want or not name.startswith(prefix):
                    continue
                a = self.alarms.get(name) or {"state": "INSUFFICIENT_DATA", "since": now, "dps": []}
                if p.get("StateValue") and p["StateValue"] != a["state"]:
                    continue
                dps = a["dps"][-periods:]
                reason = (f"Threshold Crossed: {len(dps)} datapoint{'' if len(dps) == 1 else 's'} "
                          f"[{', '.join(_fmt(v) for v in dps)}] was {'' if a['state'] == 'ALARM' else 'not '}"
                          f"{'less' if cmp.startswith('Less') else 'greater'} than the threshold ({_fmt(thr)}).")
                out.append({"AlarmName": name, "AlarmArn": f"arn:aws:cloudwatch:us-east-1:{ACCOUNT}:alarm:{name}",
                            "Namespace": ns, "MetricName": metric, "ComparisonOperator": cmp, "Threshold": thr,
                            "StateValue": a["state"], "StateUpdatedTimestamp": a["since"], "StateReason": reason,
                            "Period": 60, "EvaluationPeriods": periods, "DatapointsToAlarm": periods,
                            "Statistic": "Average", "ActionsEnabled": True})
            return {"MetricAlarms": out, "CompositeAlarms": []}
        return None

    def _logs(self, op: str, p: dict, now: datetime):
        if op == "FilterLogEvents":
            group = self._known(p.get("logGroupName") or p.get("logGroupIdentifier"))
            evs = self.log_events(group, p.get("startTime"), p.get("endTime"), p.get("filterPattern"), now,
                                  int(p.get("limit", 200)))
            return {"events": evs, "searchedLogStreams": []}
        if op == "StartQuery":
            groups = [self._known(g) for g in p.get("logGroupNames") or p.get("logGroupIdentifiers") or
                      ([p["logGroupName"]] if p.get("logGroupName") else [])]
            if not groups:
                raise AwsError("InvalidParameterException", "Specify logGroupName, logGroupNames or "
                                                            "logGroupIdentifiers.", 400)
            qid = f"q-{len(self.queries) + 1:06d}"
            q = {**p, "groups": groups, "plan": parse_query(p.get("queryString", ""), extended=True), "at": now}
            self.queries[qid] = {**q, "result": self.run_query(q, now)}
            return {"queryId": qid}
        if op == "GetQueryResults":
            q = self.queries.get(p.get("queryId"))
            return None if q is None else q["result"]
        return None

    def _known(self, g) -> str:
        """Only the log groups the scenario created answer, as in CloudWatch Logs."""
        g = _group(g)
        if g not in self.groups and not any(x[1] == g for x in self.extra_logs):
            raise AwsError("ResourceNotFoundException", "The specified log group does not exist.", 400)
        return g

    def _outbox(self, op: str, p: dict, now: datetime):
        """vl-ledger-outbox reads show the checkout outbox that physics holds; writes stay with the emulator."""
        if now < self.outage_start - timedelta(minutes=9):
            return None
        n = round(self.w.storm.outbox)
        if op == "DescribeTable":
            return {"Table": {"TableName": OUTBOX, "TableStatus": "ACTIVE", "ItemCount": n, "TableSizeBytes": n * 350,
                              "KeySchema": [{"AttributeName": "payment_id", "KeyType": "HASH"}],
                              "AttributeDefinitions": [{"AttributeName": "payment_id", "AttributeType": "S"}],
                              "BillingModeSummary": {"BillingMode": "PAY_PER_REQUEST"},
                              "TableArn": f"arn:aws:dynamodb:us-east-1:{ACCOUNT}:table/{OUTBOX}"}}
        if op == "GetItem":
            item = self._outbox_find(((p.get("Key") or {}).get("payment_id") or {}).get("S"), n)
            return {"Item": item} if item else {}
        if op not in ("Scan", "Query"):
            return None
        if legacy := next((k for k in ("ScanFilter", "QueryFilter", "KeyConditions") if p.get(k)), None):
            raise AwsError("ValidationException", f"{legacy} is unsupported in this environment; use FilterExpression "
                                                  "and KeyConditionExpression")
        names, values = p.get("ExpressionAttributeNames"), p.get("ExpressionAttributeValues")
        flt = DdbCondition(p["FilterExpression"], names, values, "FilterExpression") if p.get(
            "FilterExpression") else None
        count, hi = p.get("Select") == "COUNT", 0
        if op == "Query":
            m = re.fullmatch(r"\s*(#\w+|payment_id)\s*=\s*(:\w+)\s*", p.get("KeyConditionExpression") or "")
            if not m or (names or {}).get(m.group(1), m.group(1)) != "payment_id":
                raise AwsError("ValidationException", "Query condition missed key schema element: payment_id")
            read = [x for x in [self._outbox_find(((values or {}).get(m.group(2)) or {}).get("S"), n)] if x]
        else:
            start = (p.get("ExclusiveStartKey") or {}).get("payment_id", {}).get("S")
            lo = int(start[3:9], 16) + 1 if start else 0
            hi = min(n, lo + (int(p["Limit"]) if p.get("Limit") else n if count else 1000))
            if not count:
                hi = min(hi, lo + 1000)
            if count and (flt is None or flt.attrs <= {"status"}):
                read = range(lo, hi)
                hits = len(read) if flt is None or flt.test(self._outbox_item(0)) else 0
                return {"Count": hits, "ScannedCount": len(read),
                        **({"LastEvaluatedKey": {"payment_id": self._outbox_item(hi - 1)["payment_id"]}} if hi < n
                           else {})}
            read = [self._outbox_item(i) for i in range(lo, hi)]
        items = [x for x in read if flt is None or flt.test(x)]
        out = {"Count": len(items), "ScannedCount": len(read), **({} if count else {"Items": items})}
        if op == "Scan" and hi < n:
            out["LastEvaluatedKey"] = {"payment_id": self._outbox_item(hi - 1)["payment_id"]}
        return out

    def _outbox_find(self, pid: str | None, n: int) -> dict | None:
        i = int(pid[3:9], 16) if re.fullmatch(r"pi_[0-9a-f]{24}", pid or "") else n
        item = self._outbox_item(i) if i < n else None
        return item if item and item["payment_id"]["S"] == pid else None

    def _outbox_item(self, i: int) -> dict:
        h = hashlib.sha256(f"{self.w.seed}|outbox|{i}".encode()).hexdigest()
        at = min(self.outage_start + timedelta(seconds=i / 15), self.w.now)
        return {"payment_id": {"S": f"pi_{i:06x}{h[:18]}"}, "status": {"S": "PENDING_POSTING"},
                "authorized_at": {"S": iso(at)}}

    # ---- logs ----
    def lines(self, group: str, t: datetime, host: str | None = None) -> list[str]:
        """The app log lines `group` writes in the minute of `t`, across the fleet or, given `host`, on that host. The
        outage's own minute is the scenario's seeded lines.

        They read only that minute's record, never live state, so log history never changes after the fact."""
        w = self.w
        if t < self.outage_start.replace(second=0, microsecond=0) + timedelta(minutes=1) or not self.minutes:
            return []
        m = self._minute(t) or self.minutes[0]
        unrec = [h for h in w.core_hosts if h not in m["recovered"]]
        if host is not None and host in w.core_hosts:
            unrec = [host] if host in unrec else []
        reaches = host is None or host in m["recovered"]
        kind = APP_GROUPS.get(group) or ("endpointd" if group.startswith("/vl/platform/endpointd/") else None)
        if kind == "checkout":
            out = self._checkout_lines(m, unrec[int(m["t"].timestamp()) // 60 % len(unrec)]) if unrec else []
            if m["regime"] == "collapsed" and reaches:
                out += [f"ERROR [ledger-client] POST https://{LEDGER}/v1/postings failed: "
                        + "java.net.SocketTimeoutException: Read timed out after 2000ms",
                        "ERROR [ledger-client] HTTP 503 from ledger-writer: db pool exhausted"]
                if m["rev"] >= BREAKER_REVISION:
                    out.append(f"WARN [ledger-client] circuit ledger OPEN (failure rate {100 * FAILURE['collapsed']:.1f}% "
                               "> 50.0% over 20 calls); short-circuiting for 10s")
            return out
        if kind == "dispatcher":
            out = []
            if unrec and not m["paused"]:
                out.append(f"ERROR dispatcher - deliver failed: getaddrinfo ENOTFOUND {LEDGER}; nack visibility=0")
            if m["regime"] == "collapsed" and not m["paused"]:
                out.append("ERROR dispatcher - ledger post timeout after 2000ms; retrying immediately")
            if m["rejected"] >= 0.5 and not m["paused"]:
                out.append("WARN dispatcher - ledger lookup 429 Too Many Requests (Retry-After: 1); nack visibility=0")
            return out
        if kind == "ledger":
            tasks = max(1, m["lw_tasks"])
            if m["regime"] == "collapsed":
                waiting = round(1.5 * m["offered"] / tasks)
                out = [f"WARN db pool exhausted: waited 1500ms for connection (active=25 idle=0 waiting={waiting})",
                       "WARN client cancelled after 2000ms; query still running (no context propagation, PAY-655)"]
            else:
                active = min(25, math.ceil(25 * m["offered"] / max(1.0, m["capacity"])))
                waiting = round(m["p99"] / 1000 * max(0.0, m["offered"] - m["capacity"]) / tasks)
                out = [f"{'WARN' if waiting else 'INFO'} inbound {m['offered'] / tasks:.1f} req/s per task (1m avg); "
                       f"pool active={active} idle={25 - active} "
                       f"waiting={waiting}"
                       + ("" if m["recovered"] else " (callers: checkout-api 0, webhooks-dispatcher 0)")]
            if m["rejected"] >= 0.5:
                out.append(f"WARN admission: 429 Retry-After: 1 to webhooks-dispatcher for {m['rejected']:.0f} req/s "
                           f"over its quota of {m['quota']:.0f} req/s")
            return out
        if kind == "endpointd" and not group.endswith("/planner") and m["latch"]:
            return [f"WARN halt latch engaged: {m['latch']}; writing nothing"]
        return []

    def _checkout_lines(self, m: dict, host: str) -> list[str]:
        """One unrecovered host's checkout-api lines in the minute: from :412 on, a failed half-open probe re-opens the
        ledger-client breaker and the next 11 payments short-circuit behind it, so the breaker's exception outnumbers
        the cause 11:1; before :412 each posting fails after its retries."""
        cause = f"java.net.UnknownHostException: {LEDGER}: Name or service not known"
        if m["rev"] < BREAKER_REVISION:
            return [f"ERROR [ledger-client] POST https://{LEDGER}/v1/postings failed after 5 attempts: {cause} "
                    + f"(host={host})"]
        out = [f"ERROR [ledger-client] POST https://{LEDGER}/v1/postings failed: {cause} (half-open probe, host={host})"]
        for i in range(11):
            h = hashlib.sha256(f"{self.w.seed}|payment|{m['t'].isoformat()}|{i}".encode()).hexdigest()
            out.append(f"ERROR [checkout] payment pi_{h[:8]} authorize ok, posting deferred: CircuitBreakerOpenException: "
                       f"ledger-client circuit OPEN -> 503 PAYMENT_NOT_COMPLETED (outbox ob_{h[8:14]}, host={host})")
        return out

    def log_events(self, group, start, end, pattern, now, limit) -> list[dict]:
        start_t = _utc(start) if start else (now - timedelta(minutes=30)).astimezone(UTC)
        end_t = min(_utc(end) if end else now.astimezone(UTC), now.astimezone(UTC))
        gen_t = max(start_t, self.outage_start.astimezone(UTC) - timedelta(minutes=5))
        needle = (pattern or "").strip().strip('"').strip("'").lower()
        out = []
        t = gen_t.replace(second=42, microsecond=0)
        if t < gen_t:
            t += timedelta(minutes=1)
        stream = f"{group.rsplit('/', 1)[-1]}/{hashlib.sha256(group.encode()).hexdigest()[:16]}"
        while t <= end_t and len(out) < limit:
            lines = self.lines(group, t)
            for i, line in enumerate(lines):
                ms = int(t.timestamp() * 1000) + i * (17000 // len(lines))
                if needle and needle not in line.lower() or ms > end_t.timestamp() * 1000:
                    continue
                out.append({"logStreamName": stream, "timestamp": ms, "message": line, "ingestionTime": ms + 400,
                            "eventId": f"{ms}{i}"})
            t += timedelta(minutes=1)
        for ts, g, msg in self.extra_logs + self.acme_lines(now):
            if g == group and start_t <= ts.astimezone(UTC) <= end_t and (not needle or needle in msg.lower()):
                ms = int(ts.timestamp() * 1000)
                out.append({"logStreamName": "run", "timestamp": ms, "message": msg, "ingestionTime": ms,
                            "eventId": str(ms)})
        return sorted(out, key=lambda e: e["timestamp"])[:limit]

    def acme_lines(self, now: datetime) -> list[tuple]:
        """The nightly acme-renewer task's progress every 5 minutes from its start until its run completes or is
        stopped: its batch, the TXT changes since the last line, domains renewed (`done_at_start` at the episode
        start, all by `completes_at`) and how many of its Route 53 calls were throttled."""
        a = ((self.w.scenario.get("world") or {}).get("ecs") or {}).get("tasks", {}).get("acme-renewer")
        if not a:
            return []
        t0, start, total = parse_time(a["started"], self.w.start_t), self.w.start_t, int(a["total"])
        t1 = parse_time(a["completes_at"], start)
        batch = int(((a.get("revisions") or {}).get(a.get("revision")) or {}).get("env", {}).get("BATCH_SIZE", 50))
        done_at = self.w.world_flags.get("acme_complete")
        end = min(now, parse_time(done_at, start)) if done_at else now

        def renewed(t: datetime) -> int:
            if t <= start:
                return round(a["done_at_start"] * (t - t0) / (start - t0))
            return min(total, round(a["done_at_start"] + (total - a["done_at_start"]) * (t - start) / (t1 - start)))

        out = [(t0, ACME_LOG, (f"INFO run started: {total:,} domains, batches of {batch} domains ({2 * batch} TXT "
                               "changes, route53 ChangeResourceRecordSets)"))]
        k = 1
        while (t := t0 + timedelta(seconds=300 * k)) <= end:
            done, changes = renewed(t), 2 * (renewed(t) - renewed(t - timedelta(seconds=300)))
            h = int(hashlib.sha256(f"{self.w.seed}|acme|{k}".encode()).hexdigest()[:8], 16)
            throttled = changes // 3 + h % 7 if k > 1 else 0
            out.append((t, ACME_LOG, (f"INFO batch {math.ceil(done / batch)}/{math.ceil(total / batch)}: {done:,}/"
                                      f"{total:,} domains renewed; {changes} TXT changes in the last 5 min; Route 53 "
                                      f"throttled {throttled} calls (Throttling, PriorRequestNotComplete)"
                                      + (", retried with backoff" if throttled else ""))))
            k += 1
        return out if t0 <= end else []

    def run_query(self, q: dict, now: datetime) -> dict:
        plan, groups = q["plan"], q["groups"]
        scanned = [(g, e) for g in groups for e in self.log_events(g, q.get("startTime"), q.get("endTime"), None, now,
                                                                     10000)]
        rows = [(g, e) for g, e in scanned if all(f(e["message"]) for f in plan["filters"])]
        limit = min(plan["limit"] or 10000, int(q.get("limit") or 10000))
        if plan.get("ops"):
            recs = [{"@timestamp": e["timestamp"], "@message": e["message"], "@log": g, "@logStream": e["logStreamName"],
                     **({"level": w.removesuffix("ING")} if (w := e["message"].split(" ", 1)[0]) in _LEVELS else {})}
                    for g, e in scanned]
            results, matched = run_insights(plan["ops"], recs, limit,
                                            ["@timestamp", "@message"] + (["@log"] if len(groups) > 1 else []))
            return {"status": "Complete", "results": results,
                    "statistics": {"recordsMatched": float(matched), "recordsScanned": float(len(scanned)),
                                   "bytesScanned": float(sum(len(e["message"].encode()) for _, e in scanned))}}
        if plan["stats"]:
            st, buckets = plan["stats"], {}
            for g, e in rows:
                k = (e["timestamp"] // 1000 // st["bin_s"] * st["bin_s"] if st["bin_s"] else 0,
                     g if "@log" in st["by"] else "")
                buckets[k] = buckets.get(k, 0) + 1
            results = [[{"field": f, "value": g if f == "@log" else
                         datetime.fromtimestamp(b, UTC).strftime("%Y-%m-%d %H:%M:%S.000")} for f in st["by"]]
                       + [{"field": st["name"], "value": str(n)}] for (b, g), n in sorted(buckets.items())][:limit]
        else:
            rows.sort(key=lambda r: r[1]["timestamp"], reverse=plan["sort"] != "asc")
            fields = plan["fields"] or ["@timestamp", "@message"] + (["@log"] if len(groups) > 1 else [])
            val = {"@timestamp": lambda g, e: datetime.fromtimestamp(e["timestamp"] / 1000, UTC)
                   .strftime("%Y-%m-%d %H:%M:%S.%f")[:-3], "@message": lambda g, e: e["message"],
                   "@log": lambda g, e: g, "@logStream": lambda g, e: e["logStreamName"]}
            results = [[{"field": f, "value": val[f](g, e)} for f in fields] for g, e in rows[:limit]]
        return {"status": "Complete", "results": results,
                "statistics": {"recordsMatched": float(len(rows)), "recordsScanned": float(len(scanned)),
                               "bytesScanned": float(sum(len(e["message"].encode()) for _, e in scanned))}}

    def export(self) -> dict:
        return {"alarms": {k: {"state": v["state"], "since": iso(v["since"])} for k, v in self.alarms.items()}}
