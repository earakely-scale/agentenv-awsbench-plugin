"""The meter: every charged call costs virtual time, counts against the call budget and runs alone in the agent lane.

Only the meter moves time: it advances the world, then the gateway clock to the time the world reached."""

import asyncio
import hashlib
import logging
import re
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import httpx

log = logging.getLogger(__name__)

ET = ZoneInfo("America/New_York")
COSTS = {"free": 0, "point_read": 20, "write": 45, "broad": 75}
RANK = {"free": 0, "point_read": 1, "write": 2, "broad": 3}
FREE_TOOLS = {"get_time", "awsbench_control", "wait"}
BROAD_TOOLS = {"aws_query", "aws_table_export", "aws_table_diff"}
READ_TOOLS = {"fleet_list_hosts", "fleet_inspect_host", "fleet_job_status", "chat_get_user"}
READ_SUFFIXES = ("_get", "_read", "_search", "_history", "_log", "_oncall")
READ_OP = re.compile(r"^(Describe|Get|List|Lookup|BatchGet|Scan|Query|Search|Filter|Test|Head|Ls)")
BROAD_OPS = {("logs", "StartQuery"), ("logs", "GetQueryResults")}
WAKE_KINDS = frozenset({"dm", "mention", "page", "email", "job_done"})
WORLD_KINDS = frozenset({"job_done"})  # inbound items the world makes itself; workspace ones reach the inbox directly
DEFAULT_ROLES = ("sre-primary",)
CLI_VALUED = {"--profile", "--region", "--endpoint-url", "--output", "--query", "--ca-bundle", "--color",
              "--cli-read-timeout", "--cli-connect-timeout"}


def parse_time(value: Any) -> datetime:
    t = value if isinstance(value, datetime) else datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if t.tzinfo is None:
        raise ValueError(f"time {value!r} has no offset")
    return t


def rfc3339(t: datetime) -> str:
    return t.isoformat()


def whole(t: datetime) -> datetime:
    """`t` rounded up to a whole second: NPC reply delays are fractional, and a wake at one would otherwise put
    microseconds on every later time an agent sees."""
    return t + timedelta(microseconds=1_000_000 - t.microsecond) if t.microsecond else t


def clock_text(t: datetime) -> str:
    return t.astimezone(ET).strftime("%H:%M:%S ET")


def camel(subcommand: str) -> str:
    """An aws-cli subcommand's API operation name: list-hosted-zones -> ListHostedZones."""
    return "".join(part[:1].upper() + part[1:] for part in subcommand.split("-"))


def cli_words(command: str) -> list[str]:
    """An `aws ...` command's service and subcommand, skipping global options and their values."""
    words = command.split()
    if words[:1] != ["aws"]:
        return []
    out, skip = [], False
    for w in words[1:]:
        if skip:
            skip = False
        elif w.startswith("-"):
            skip = w in CLI_VALUED and "=" not in w
        else:
            out.append(w)
            if len(out) == 2:
                break
    return out


def s3_download(command: str) -> bool:
    """`aws s3 cp s3://bucket/key -`: an object printed to stdout, which reads it (HeadObject and GetObject)."""
    words, paths, skip = command.split(), [], False
    for w in words[words.index("cp") + 1 if "cp" in words else len(words):]:
        if skip:
            skip = False
        elif w.startswith("--"):
            skip = w in CLI_VALUED
        else:
            paths.append(w)
    return len(paths) == 2 and paths[0].startswith("s3://") and paths[1] == "-"


def is_read(operation: str | None) -> bool:
    return bool(operation) and (bool(READ_OP.match(operation)) or operation == "TestDNSAnswer")


def op_class(service: str | None, operation: str | None) -> str:
    if not operation:
        return "point_read"
    if (service, operation) in BROAD_OPS:
        return "broad"
    return "point_read" if is_read(operation) else "write"


def default_class(tool: str, args: dict) -> str:
    if tool in FREE_TOOLS:
        return "free"
    if tool in BROAD_TOOLS:
        return "broad"
    if tool in READ_TOOLS or tool.endswith(READ_SUFFIXES) or "_list" in tool:
        return "point_read"
    if tool == "aws_api":
        return op_class(args.get("service"), args.get("operation"))
    if tool == "aws_cli":
        command = str(args.get("command", ""))
        words = cli_words(command)
        if words == ["s3", "presign"] or words == ["s3", "cp"] and s3_download(command):
            return "point_read"
        return op_class(words[0] if words else None, camel(words[1]) if len(words) > 1 else None)
    if tool == "fleet_exec":
        return "point_read"
    return "write"


def _hosts_match(rule: Any, hosts: int | None) -> bool:
    if rule is None:
        return True
    if hosts is None:
        return False
    text = str(rule).strip()
    if text.startswith(">"):
        return hosts > int(text[1:])
    return hosts == int(text)


def classify(tool: str, args: dict, overrides: list[dict] | None = None, hosts: int | None = None) -> str:
    """The default class, else the first override whose tool, `match` (re.search on the command) and `hosts` fit;
    a fleet_exec on more than one host is never cheaper than broad."""
    cls = default_class(tool, args)
    command = str(args.get("command", ""))
    for rule in overrides or []:
        if rule.get("tool") not in (None, tool):
            continue
        if rule.get("match") is not None and not re.search(rule["match"], command):
            continue
        if not _hosts_match(rule.get("hosts"), hosts):
            continue
        cls = rule["class"]
        break
    if tool == "fleet_exec" and hosts is not None and hosts > 1 and RANK[cls] < RANK["broad"]:
        cls = "broad"
    return cls


def jitter(seed: str, call_no: int, mode: str) -> float:
    if mode != "train":
        return 1.0
    u = int(hashlib.sha256(f"{seed}|cost|{call_no}".encode()).hexdigest()[:16], 16) / 2**64
    return 1 + 0.25 * (2 * u - 1)


class LocalClock:
    """The clock without a gateway: whatever the meter last set."""

    def __init__(self, now: datetime | None = None):
        self.now = now

    async def advance(self, t: datetime) -> None:
        self.now = t

    async def time(self) -> datetime | None:
        return self.now


class GatewayClock:
    """The gateway's clock, moved forward only."""

    def __init__(self, base: str, timeout: float = 30.0):
        self.base = base.rstrip("/")
        self.timeout = timeout
        self.errors: list[str] = []

    async def advance(self, t: datetime) -> None:
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                r = await client.put(f"{self.base}/clock/advance", json={"virtual_time": rfc3339(t)})
            if r.status_code >= 400:
                self.errors.append(f"advance {rfc3339(t)}: {r.status_code} {r.text[:200]}")
                log.warning("gateway clock advance refused: %s", r.text[:200])
        except httpx.HTTPError as e:
            self.errors.append(f"advance {rfc3339(t)}: {e}")
            log.warning("gateway clock unreachable: %s", e)

    async def time(self) -> datetime | None:
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                r = await client.get(f"{self.base}/clock/time")
            return parse_time(r.json()["virtual_time"]) if r.status_code == 200 else None
        except (httpx.HTTPError, KeyError, ValueError):
            return None


class Inbox:
    """Inbound items for the agent, each handed out once, in arrival order."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._items: list[dict] = []
        self._seen: set[str] = set()
        self._read = 0

    @staticmethod
    def _key(item: dict) -> str:
        return "|".join(str(item.get(k)) for k in ("t", "kind", "from", "where", "preview", "id"))

    def extend(self, items: list[dict] | None) -> None:
        with self._lock:
            for item in items or []:
                key = self._key(item)
                if key not in self._seen:
                    self._seen.add(key)
                    self._items.append(item)

    def drain(self) -> list[dict]:
        with self._lock:
            new, self._read = self._items[self._read:], len(self._items)
            return new

    def all(self) -> list[dict]:
        with self._lock:
            return list(self._items)


@dataclass
class MeterConfig:
    start: datetime
    window_end: datetime
    horizon: datetime
    max_calls: int = 250
    seed: str = "0"
    mode: str = "eval"
    charged_roles: tuple[str, ...] = DEFAULT_ROLES
    overrides: list[dict] = field(default_factory=list)
    wait_max_s: float = 1800.0
    wake: frozenset = WAKE_KINDS
    lease_s: float = 300.0

    @classmethod
    def from_scenario(cls, scenario: dict, *, seed: str, mode: str, charged_roles: list[str] | None = None):
        clock = scenario.get("clock") or {}
        budget = scenario.get("budget") or {}
        wait = clock.get("wait") or {}
        start = parse_time(scenario["start"])
        window_end = parse_time(scenario.get("window_end") or scenario.get("horizon"))
        wake = frozenset("email" if k == "email_to_role" else k for k in (wait.get("early_wake") or WAKE_KINDS))
        return cls(start=start, window_end=window_end, horizon=parse_time(scenario.get("horizon") or window_end),
                   max_calls=int(budget.get("max_calls", scenario.get("max_calls", 250))), seed=str(seed), mode=mode,
                   charged_roles=tuple(charged_roles or scenario.get("charged_roles") or DEFAULT_ROLES),
                   overrides=list(clock.get("overrides") or scenario.get("cost_overrides") or []),
                   wait_max_s=float(wait.get("max_minutes", 30)) * 60, wake=wake)


class Lane:
    """One asyncio lock for the agent's charged calls; a holder past its lease is evicted so a lost `end` can't
    wedge the episode."""

    def __init__(self, lease_s: float):
        self.lease_s = lease_s
        self._lock = asyncio.Lock()
        self._since = 0.0

    async def acquire(self) -> None:
        while True:
            remaining = self.lease_s - (time.monotonic() - self._since) if self._lock.locked() else self.lease_s
            try:
                await asyncio.wait_for(self._lock.acquire(), timeout=max(remaining, 0.01))
                self._since = time.monotonic()
                return
            except TimeoutError:
                if self._lock.locked() and time.monotonic() - self._since >= self.lease_s:
                    log.warning("agent lane lease expired; evicting the holder")
                    self._lock.release()

    def release(self) -> None:
        if self._lock.locked():
            self._lock.release()


class Meter:
    def __init__(self, cfg: MeterConfig, *, world: Any = None, clock: Any = None, inbox: Inbox | None = None,
                 actionlog: Any = None, world_lock: Any = None):
        self.cfg = cfg
        self.world = world
        self.clock = clock or LocalClock(cfg.start)
        self.inbox = inbox or Inbox()
        self.actionlog = actionlog
        self.world_lock = world_lock or threading.RLock()
        self.lane = Lane(cfg.lease_s)
        self.now = cfg.start
        self.calls_used = 0
        self.ended = False
        self.end_reason: str | None = None
        self.done_at: datetime | None = None
        self.rows: list[dict] = []
        self.pending: dict[int, dict] = {}

    def charged(self, role: str | None) -> bool:
        return role in self.cfg.charged_roles

    @property
    def calls_left(self) -> int:
        return max(self.cfg.max_calls - self.calls_used, 0)

    def _world(self, fn, *args, **kwargs):
        with self.world_lock:
            return fn(*args, **kwargs)

    async def advance(self, t: datetime, wake: set[str] | frozenset | None = None) -> dict:
        """Move the world, then the gateway clock, forward to `t` (or to the wake item that stopped it), always to a
        whole second; `early` says a wake item stopped it short of `t`."""
        t = whole(t)
        if t <= self.now:
            return {"now": self.now, "woke": [], "fired": [], "early": False}
        if self.world is not None:
            res = await asyncio.to_thread(self._world, self.world.advance_to, t, set(wake) if wake else None)
            reached = parse_time(res.get("now") or t)
            if reached.microsecond:
                rest = await asyncio.to_thread(self._world, self.world.advance_to, whole(reached), None)
                res = {**res, "fired": list(res.get("fired") or []) + list(rest.get("fired") or []),
                       "delivered": list(res.get("delivered") or []) + list(rest.get("delivered") or [])}
            res["early"] = reached < t
            reached = whole(reached)
        else:
            res, reached = {"now": t, "woke": [], "fired": [], "early": False}, t
        self.inbox.extend(res.get("woke"))
        self.inbox.extend([d for d in res.get("delivered") or [] if d.get("kind") in WORLD_KINDS])
        if reached > self.now:
            self.now = reached
            await self.clock.advance(reached)
        return {**res, "now": reached}

    def refuse(self, role: str | None) -> str | None:
        if self.ended:
            return "The incident window is over for you: the episode has ended; write nothing more and reply DONE"
        if self.calls_used >= self.cfg.max_calls:
            return f"Call budget exhausted ({self.cfg.max_calls}/{self.cfg.max_calls}); write nothing more and reply DONE"
        if self.now >= self.cfg.window_end:
            return f"The window closed at {clock_text(self.cfg.window_end)}; write nothing more and reply DONE"
        return None

    async def begin(self, server: str, tool: str, args: dict, role: str | None, *, hosts: int | None = None,
                    cost_class: str | None = None) -> dict:
        if not self.charged(role):
            return {"ok": True, "charged": False, "call_no": None, "t_exec": rfc3339(self.now),
                    "t_issue": self.now, "calls_left": self.calls_left}
        await self.lane.acquire()
        try:
            if reason := self.refuse(role):
                self.lane.release()
                return {"ok": False, "refuse": reason}
            self.calls_used += 1
            call_no = self.calls_used
            cls = cost_class or classify(tool, args, self.cfg.overrides, hosts)
            cost = round(COSTS[cls] * jitter(self.cfg.seed, call_no, self.cfg.mode))
            t_issue = self.now
            t_exec = min(t_issue + timedelta(seconds=cost), self.cfg.window_end)
            await self.advance(t_exec)
            row = {"call_no": call_no, "tool": tool, "cost_class": cls, "cost_s": cost,
                   "t_issue": rfc3339(t_issue), "t_exec": rfc3339(self.now)}
            self.rows.append(row)
            self.pending[call_no] = {**row, "server": server, "role": role, "args": dict(args)}
            return {"ok": True, "charged": True, "call_no": call_no, "t_exec": rfc3339(self.now),
                    "t_issue": t_issue, "calls_left": self.calls_left}
        except BaseException:
            self.lane.release()
            raise

    async def end(self, call_no: int | None, tool: str, args: dict, result_summary: Any = None,
                  resolved: dict | None = None, *, drain: bool = True) -> dict:
        try:
            call = self.pending.pop(call_no, None) if call_no is not None else None
            if call is not None:
                record = {"t": rfc3339(self.now), "t_issue": call["t_issue"], "call_no": call_no, "role": call["role"],
                          "server": call["server"], "tool": tool, "args": dict(args), "result": result_summary,
                          "resolved": resolved or {}, "ops": self.ops(call_no)}
                if self.actionlog is not None and not (resolved or {}).get("logged"):
                    self.actionlog.record(t=self.now, t_issue=call["t_issue"], call_no=call_no, role=call["role"],
                                          principal=call["role"], world_made=False, server=call["server"], tool=tool,
                                          params=dict(args), error=(resolved or {}).get("error"),
                                          exit_code=(resolved or {}).get("exit_code"), resources=[])
                if self.world is not None:
                    await asyncio.to_thread(self._world, self.world.observe, record)
        finally:
            if call_no is not None:
                self.lane.release()
        return {"banner": self.banner(drain=drain)}

    def ops(self, call_no: int) -> list[dict]:
        """The AWS operations the call made, from the action log rows it owns, in issue order."""
        out = []
        for r in reversed(self.actionlog.rows[:] if self.actionlog is not None else []):
            if r["call_no"] == call_no and r["service"] and not r["world_made"]:
                out.append({k: r[k] for k in ("service", "operation", "params", "error")})
            elif isinstance(r["call_no"], int) and r["call_no"] < call_no:
                break
        return out[::-1]

    def banner(self, *, drain: bool = True) -> dict:
        return {"now": clock_text(self.now), "call": self.calls_used, "calls_left": self.calls_left,
                "new": self.inbox.drain() if drain else []}

    async def wait(self, target: datetime) -> dict:
        """Advance toward `target` (clamped to the wait cap and window_end), stopping at the first wake item. The
        timeline ids that fired stay internal: they name scripted beats, cancelled ones included."""
        start = self.now
        cap = min(target, start + timedelta(seconds=self.cfg.wait_max_s), self.cfg.window_end)
        before = len(self.inbox.all())
        res = await self.advance(cap, wake=self.cfg.wake) if cap > start else {"now": start, "woke": []}
        inbound = self.inbox.drain()
        woke = any(i.get("kind") in self.cfg.wake for i in self.inbox.all()[before:])
        reason = "inbound" if woke and res.get("early") else (
            "window_end" if self.now >= self.cfg.window_end else "until_reached")
        return {"waited_s": (self.now - start).total_seconds(), "now": rfc3339(self.now), "woke_early": reason ==
                "inbound", "reason": reason, "inbound": inbound}

    def end_episode(self, reason: str = "done") -> None:
        if not self.ended:
            self.ended, self.end_reason, self.done_at = True, reason, self.now

    def status(self) -> dict:
        return {"now": rfc3339(self.now), "ended": self.ended, "end_reason": self.end_reason,
                "calls_used": self.calls_used, "calls_left": self.calls_left,
                "window_end": rfc3339(self.cfg.window_end), "horizon": rfc3339(self.cfg.horizon)}
