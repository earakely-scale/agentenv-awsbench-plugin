"""T03 streamroute hosts: threads against limit_nproc (sr-threads), gossip membership (sr-membership), the srcoord
shard-map lifecycle and warm-up ejection (rules.py), the heap rule (sr-memory) and cellmap's BootstrapSnapshot limit
(cellmap-snapshot). Times are float epoch seconds; docs/T03-build.md §1.1-1.5."""

import fnmatch
import math
import re
from datetime import datetime

from .fleet import instance_id
from .util import unit

ROUTING, STALE, BROKEN, WARMING, EJECTED, STOPPED = "ROUTING", "STALE", "BROKEN", "WARMING", "EJECTED", "STOPPED"
SERVING = (ROUTING, STALE)
STATES = (ROUTING, STALE, BROKEN, WARMING, EJECTED, STOPPED)
AZ = {"a": "us-east-1a", "b": "us-east-1b", "c": "us-east-1c"}
INF = float("inf")
ADMIN = {"cellmap-admin-01": {"fleet": "cellmap-admin", "az": "us-east-1a", "ip": "10.40.0.21"}}
BUILD_FAIL = ("ERROR [shardmap-manager] c.v.streamroute.shardmap.ShardMapBuilder - build epoch={epoch} failed after "
              "{started}/8 workers started: java.lang.OutOfMemoryError: unable to create native thread: possibly out "
              "of memory or process/resource limits reached")
NATIVE_THREAD = "OutOfMemoryError: unable to create native thread"
PARAMS = {
    "jvm_fixed": 364, "sidecar": 22, "pool_min": 256, "pool_max": 384, "pool_per_2000": 128, "build_workers": 8,
    "link_retry_s": 300, "learn_after_s": 240, "learn_spread_s": 3600, "gossip_drop_s": 90,
    "rebuild_s": 1800, "ttl_s": 2700, "build_s": 40, "build_jitter_s": 10, "stop_s": 5, "jvm_s": 45,
    "jvm_jitter_s": 10, "auth_s": 90, "auth_jitter_s": 20, "peer_boot_s": 300, "peer_slots": 2, "retry_s": 30,
    "quarantine_s": 900, "boot_s": 240, "oom_after_s": 720, "oom_xmx_gib": 8,
    "cellmap_capacity": 12, "cellmap_collapse_above": 24, "cellmap_collapse_for_s": 120, "cellmap_shed_s": 600,
    "epoch0": 204400, "rebuild_gap": [900, 1321], "pinned_offsets": {"sr-b-017": 1321}, "fixed_offsets": {"sr-a-017": 1210},
}


def xmx_gib(value) -> float:
    s = str(value or "6g").strip().lower()
    n = float(s.rstrip("gmk") or 0)
    return n / 1024 if s.endswith("m") else n / 1048576 if s.endswith("k") else n


def host_ip(name: str) -> str:
    az, n = name[3], int(name[-3:])
    return f"10.40.{16 * 'abc'.index(az) + 1 + n // 200}.{10 + n % 200}"


class Host:
    __slots__ = (
        "az",
        "boot_peer",
        "bootstrap_source",
        "build_epoch",
        "build_until",
        "build_workers",
        "detached",
        "epoch",
        "grace_s",
        "iid",
        "ip",
        "joined",
        "known",
        "kver",
        "last_error",
        "last_success",
        "last_wait_log",
        "launched",
        "learn",
        "limit",
        "link_failures",
        "link_retry",
        "links",
        "n_restarts",
        "name",
        "next_learn",
        "next_rebuild",
        "oom_at",
        "phase",
        "phase_until",
        "pool",
        "proc",
        "process_start",
        "protected",
        "quarantined_until",
        "restart_cause",
        "routing_since",
        "standby",
        "state",
        "stopped_at",
        "stuck",
        "terminated",
        "valid_until",
        "workers_started",
        "xmx",
    )

    def __init__(self, name: str, launched: float):
        self.name, self.az, self.iid, self.ip = name, AZ[name[3]], instance_id(name), host_ip(name)
        self.launched, self.terminated, self.proc, self.joined, self.stopped_at = launched, False, False, False, None
        self.state, self.phase, self.phase_until = STOPPED, "boot", None
        self.valid_until = self.last_success = self.build_until = self.quarantined_until = None
        self.next_rebuild, self.stuck, self.build_workers, self.workers_started = INF, False, 0, "8/8"
        self.process_start, self.bootstrap_source, self.grace_s, self.restart_cause = launched, "peers", 600, "launch"
        self.xmx, self.limit, self.pool, self.links, self.known, self.kver, self.next_learn = "6g", 1024, 0, 0, 0, -1, INF
        self.learn: dict[str, float] = {}
        self.link_retry, self.link_failures, self.boot_peer, self.epoch, self.last_error = 0.0, 0, None, None, None
        self.routing_since = self.oom_at = self.build_epoch = None
        self.n_restarts, self.last_wait_log = 0, -INF
        self.standby = self.detached = self.protected = False

    def jvm_threads(self, p: dict) -> int:
        return p["jvm_fixed"] + self.pool + 2 * self.links + self.build_workers if self.proc else 0


class Cellmap:
    """BootstrapSnapshot: 12 concurrent; above that 429 and a 30 s client retry; more than 24 in flight + retrying for
    120 s collapses it into a 600 s load-shed that answers 503 to every bootstrap."""

    def __init__(self, p: dict):
        self.p = p
        self.in_flight: set[str] = set()
        self.retrying: dict[str, float] = {}
        self.shed_until: float | None = None
        self.over_since: float | None = None
        self.intervals: list[list[float]] = []
        self.throttled = 0
        self.rejected_503 = 0

    def shed(self, t: float) -> bool:
        return self.shed_until is not None and t < self.shed_until

    def request(self, host: str, t: float) -> bool:
        if host in self.in_flight:
            return True
        if t < self.retrying.get(host, -INF):
            return False
        if self.shed(t):
            self.retrying[host] = t + self.p["retry_s"]
            self.rejected_503 += 1
            return False
        if len(self.in_flight) < self.p["cellmap_capacity"]:
            self.in_flight.add(host)
            self.retrying.pop(host, None)
            return True
        self.retrying[host] = t + self.p["retry_s"]
        self.throttled += 1
        return False

    def release(self, host: str) -> None:
        self.in_flight.discard(host)
        self.retrying.pop(host, None)

    def tick(self, t: float) -> list[str]:
        """Hosts whose in-flight bootstrap the collapse aborted."""
        if self.shed(t):
            return []
        if len(self.in_flight) + len(self.retrying) > self.p["cellmap_collapse_above"]:
            self.over_since = t if self.over_since is None else self.over_since
            if t - self.over_since >= self.p["cellmap_collapse_for_s"]:
                self.shed_until, self.over_since = t + self.p["cellmap_shed_s"], None
                self.intervals.append([t, self.shed_until])
                aborted = sorted(self.in_flight)
                for h in aborted:
                    self.retrying[h] = t + self.p["retry_s"]
                self.in_flight.clear()
                return aborted
        else:
            self.over_since = None
        return []

    def shed_seconds(self, t: float) -> int:
        return int(sum(min(b, t) - a for a, b in self.intervals if a < t))


class Fleet:
    def __init__(self, cfg: dict, seed: str, presim_seed: str, tz):
        self.p = {**PARAMS, **(cfg.get("hosts_params") or {})}
        self.tz = tz
        self.seed, self.pseed = seed, presim_seed
        self.rseed = presim_seed  # draws use the fixed presimulation seed until the scenario starts
        self.hosts: dict[str, Host] = {}
        self.cell = Cellmap(self.p)
        self.epoch = self.p["epoch0"]
        self.logs: list[tuple[float, str, str, str]] = []  # (t, group, host, line)
        self.ejections: list[dict] = []
        self.restarts: list[dict] = []
        self.oom_kills: list[dict] = []
        self.became_routing: list[str] = []
        self.mver, self._visible = 0, frozenset()
        self.peer_boots: dict[str, int] = {}

    # ---- roster ----
    def add_host(self, name: str, launched: float, *, presim: bool, t: float | None = None) -> Host:
        h = Host(name, launched)
        self.hosts[name] = h
        rs = self.pseed if presim else self.rseed
        for other in self.hosts.values():
            if other is not h:
                spread = self.p["learn_spread_s"] / (2 if other.name in self.p["pinned_offsets"] else 1)
                other.learn[name] = launched + self.p["learn_after_s"] + spread * unit(rs, "learn", other.name, name)
                other.kver = -1
        if presim and t is not None:     # an original member: routing since before the presimulation
            h.learn = {o: launched for o in self.hosts if o != name}
            h.proc = h.joined = True
            h.state, h.phase, h.restart_cause = ROUTING, "routing", None
            h.process_start = launched
            h.valid_until, h.last_success = t + self.p["ttl_s"], t
            h.next_rebuild = t + self.offset(name)
            h.routing_since, h.epoch = t, self.epoch
        else:
            h.phase_until = launched + self.p["boot_s"]
        return h

    def offset(self, name: str) -> float:
        pinned = self.p["pinned_offsets"].get(name, self.p["fixed_offsets"].get(name))
        if pinned is not None:
            return float(pinned)
        lo, hi = self.p["rebuild_gap"]
        u = unit(self.pseed, "offset", name) * (self.p["rebuild_s"] - (hi - lo))
        return u if u < lo else u + (hi - lo)

    def learn_all(self, h: Host, t: float, ssm: dict) -> None:
        h.learn = {o.name: t for o in self.hosts.values() if o is not h and not o.terminated and o.proc}
        h.kver = -1

    def terminate(self, name: str, t: float) -> None:
        h = self.hosts[name]
        if h.terminated:
            return
        self._stop(h, t)
        h.terminated, h.state, h.phase = True, STOPPED, "terminated"
        self.epoch += 1

    # ---- membership ----
    def visible(self, h: Host, t: float) -> bool:
        return h.joined and not h.terminated and (
            h.proc or (h.stopped_at is not None and t - h.stopped_at < self.p["gossip_drop_s"]))

    def members(self) -> list[str]:
        return [n for n, h in self.hosts.items() if not h.terminated and h.proc]

    def known_set(self, h: Host, t: float) -> list[str]:
        return [n for n in self._visible if n != h.name and h.learn.get(n, INF) <= t]

    def _refresh_known(self, h: Host, t: float) -> None:
        if h.kver == self.mver and t < h.next_learn:
            return
        known, nxt = 0, INF
        for n in self._visible:
            if n == h.name:
                continue
            lt = h.learn.get(n, INF)
            if lt <= t:
                known += 1
            elif lt < nxt:
                nxt = lt
        h.known, h.next_learn, h.kver = known, nxt, self.mver

    # ---- threads ----
    def want_pool(self, h: Host, demand: float, lagging: bool) -> int:
        p = self.p
        if h.state in (STALE, BROKEN) or lagging:
            return p["pool_max"]
        return min(p["pool_max"], math.ceil(p["pool_min"] + p["pool_per_2000"] * demand / 2000))

    def alloc(self, h: Host, t: float, demand: float, lagging: bool) -> int:
        """Priority jvm_fixed, sidecar, route_worker, existing_links, shardmap-build, new_links; returns free threads."""
        p = self.p
        cap = h.limit - p["jvm_fixed"] - p["sidecar"]
        h.pool = max(0, min(self.want_pool(h, demand, lagging), cap))
        rem = cap - h.pool
        h.links = max(0, min(h.links, h.known, rem // 2))
        rem -= 2 * h.links + h.build_workers
        if h.known > h.links and t >= h.link_retry and h.phase not in ("jvm", "bootstrap_wait", "bootstrap"):
            add = min(h.known - h.links, max(rem, 0) // 2)
            h.links += add
            rem -= 2 * add
            if h.links < h.known:
                h.link_failures += h.known - h.links
                h.link_retry = t + p["link_retry_s"]
        return rem

    # ---- lifecycle ----
    def jit(self, h: Host, key: str, spread: float) -> float:
        return spread * (2 * unit(self.rseed, key, h.name, h.n_restarts) - 1)

    def log(self, t: float, group: str, host: str, line: str) -> None:
        self.logs.append((t, group, host, line))

    def _stop(self, h: Host, t: float) -> None:
        if h.proc:
            h.stopped_at = t
        h.proc = False
        h.pool = h.links = h.build_workers = 0
        h.build_until = h.valid_until = h.routing_since = h.oom_at = None
        self.cell.release(h.name)
        if h.boot_peer:
            self.peer_boots[h.boot_peer] = max(0, self.peer_boots.get(h.boot_peer, 1) - 1)
            h.boot_peer = None

    def restart(self, name: str, t: float, cause: str, ssm: dict, routing_frac: float) -> str | None:
        """srctl restart / a fleet_restart step: the error text, or None once the host is stopping."""
        h = self.hosts.get(name)
        if h is None or h.terminated:
            return f"srctl: no such host {name}"
        if h.quarantined_until is not None and t < h.quarantined_until:
            return f"srctl: restart refused: host quarantined by srcoord until {self.hms(h.quarantined_until)}"
        if h.phase == "boot":
            return f"srctl: {name}: instance is still booting"
        members = len(self.members())
        self._stop(h, t)
        h.n_restarts += 1
        h.state, h.phase, h.phase_until, h.stuck, h.quarantined_until = STOPPED, "stopping", t + self.p["stop_s"], False, None
        self._read_config(h, ssm)
        h.restart_cause = cause
        self.restarts.append({"t": t, "host": name, "cause": cause, "bootstrap_source": h.bootstrap_source,
                              "members_at_start": members, "routing_frac_at_start": round(routing_frac, 3),
                              "xmx": h.xmx, "limit_nproc": h.limit, "warmup_grace": h.grace_s})
        self.log(t, "/vl/prod/srcoord", name, f"{name} process stopping (srctl restart)")
        return None

    def _read_config(self, h: Host, ssm: dict) -> None:
        h.bootstrap_source = str(ssm.get("/streamroute/prod/shardmap/bootstrap_source") or "peers").strip().lower()
        h.grace_s = int(float(ssm.get("/streamroute/prod/health/warmup_grace_seconds") or 600))
        h.xmx = str(ssm.get("/streamroute/prod/jvm/xmx") or "6g").strip()
        try:
            h.limit = int(float(ssm.get("/streamroute/prod/os/limit_nproc") or 1024))
        except ValueError:
            h.limit = 1024

    def _start_jvm(self, h: Host, at: float) -> None:
        h.proc, h.state, h.phase, h.process_start = True, WARMING, "jvm", at
        h.phase_until = at + self.p["jvm_s"] + self.jit(h, "jvm", self.p["jvm_jitter_s"])
        h.learn, h.kver, h.link_retry, h.last_wait_log = {}, -1, 0.0, -INF
        if not h.joined:
            h.joined = True
            self.epoch += 1

    def _eject(self, h: Host, t: float, cause: str, line: str) -> None:
        self._stop(h, t)
        h.state, h.phase, h.quarantined_until = EJECTED, "ejected", t + self.p["quarantine_s"]
        self.ejections.append({"t": t, "host": h.name, "cause": cause})
        self.log(t, "/vl/prod/srcoord", h.name, line)

    # ---- one tick ----
    def tick(self, t: float, demand: float, lagging: bool, ssm: dict) -> None:
        self.became_routing = []
        for h in self.hosts.values():
            if h.terminated:
                continue
            if h.state == EJECTED and t >= h.quarantined_until:
                h.state, h.phase, h.quarantined_until = STOPPED, "fenced", None
            if h.state == STOPPED and h.phase in ("stopping", "boot") and t >= h.phase_until:
                if h.phase == "boot":
                    self._read_config(h, ssm)
                self._start_jvm(h, h.phase_until)
        vis = frozenset(n for n, h in self.hosts.items() if self.visible(h, t))
        if vis != self._visible:
            self._visible, self.mver = vis, self.mver + 1
        routing = [n for n, h in self.hosts.items() if h.state == ROUTING]
        for h in self.cell_aborts(t):
            h.phase, h.phase_until = "bootstrap_wait", None
        for h in self.hosts.values():
            if not h.proc:
                continue
            self._refresh_known(h, t)
            rem = self.alloc(h, t, demand, lagging)
            if h.state == WARMING:
                self._warming(h, t, rem, routing, ssm, lagging)
            else:
                self._serving(h, t, rem, lagging)
            if h.oom_at is not None and t >= h.oom_at and h.state in SERVING:
                self.oom_kills.append({"t": t, "host": h.name})
                self._eject(h, t, "oom_kill", f"ejected {h.name}: unclean exit while ROUTING (oom-kill); quarantined 15m")
                h.restart_cause = "world"

    def cell_aborts(self, t: float) -> list[Host]:
        return [self.hosts[n] for n in self.cell.tick(t)]

    def _serving(self, h: Host, t: float, rem: int, lagging: bool) -> None:
        p = self.p
        if h.build_until is not None and t >= h.build_until:
            te, h.build_until = h.build_until, None
            if h.build_workers >= p["build_workers"]:
                h.valid_until, h.last_success, h.epoch, h.last_error = te + p["ttl_s"], te, self.epoch, None
                if h.state != ROUTING:
                    self.became_routing.append(h.name)
                h.state, h.workers_started = ROUTING, "8/8"
            else:
                h.stuck, h.workers_started, h.last_error = True, f"{h.build_workers}/8", NATIVE_THREAD
                h.build_epoch = self.epoch
                self.log(te, "/vl/prod/streamroute", h.name, BUILD_FAIL.format(epoch=self.epoch, started=h.build_workers))
            h.build_workers = 0
        if t >= h.next_rebuild:
            te = h.next_rebuild
            h.next_rebuild += p["rebuild_s"]
            if h.stuck:
                self.log(te, "/vl/prod/streamroute", h.name, "INFO [shardmap-manager] ShardMapManager - rebuild "
                                                             f"skipped: build already in progress (epoch {h.build_epoch})")
            else:
                h.build_until, h.build_workers = te + p["build_s"], max(0, min(p["build_workers"], rem))
        if h.stuck or h.state != ROUTING:
            h.state = STALE if (h.valid_until or 0) > t else BROKEN
            h.routing_since = None

    def _warming(self, h: Host, t: float, rem: int, routing: list[str], ssm: dict, lagging: bool) -> None:
        p = self.p
        if h.phase == "jvm" and t >= h.phase_until:
            h.phase, h.phase_until = "bootstrap_wait", None
        if h.phase == "bootstrap_wait":
            if h.bootstrap_source == "authoritative":
                if self.cell.request(h.name, t):
                    h.phase, h.phase_until = "bootstrap", t + p["auth_s"] + self.jit(h, "auth", p["auth_jitter_s"])
            else:
                peer = next((n for n in routing if self.peer_boots.get(n, 0) < p["peer_slots"]), None)
                if peer is not None:
                    self.peer_boots[peer] = self.peer_boots.get(peer, 0) + 1
                    h.boot_peer, h.phase, h.phase_until = peer, "bootstrap", t + p["peer_boot_s"]
                elif t - h.last_wait_log >= p["retry_s"]:
                    h.last_wait_log = t
                    self.log(t, "/vl/prod/streamroute", h.name, "WARN [bootstrap] no ROUTING peers available for "
                             f"bootstrap (0/{len(routing)} eligible); retrying in 30s")
        if h.phase == "bootstrap" and t >= h.phase_until:
            te = h.phase_until
            if h.bootstrap_source == "authoritative" or h.boot_peer is None or h.boot_peer not in self.hosts:
                self.learn_all(h, te, ssm)
            else:
                q = self.hosts[h.boot_peer]
                h.learn = {n: te for n in [*self.known_set(q, t), q.name] if n != h.name}
                h.kver = -1
                self.peer_boots[q.name] = max(0, self.peer_boots.get(q.name, 1) - 1)
            h.boot_peer = None
            self.cell.release(h.name)
            self._refresh_known(h, t)
            h.links, h.link_retry = 0, 0.0
            h.phase = "first_build"
            rem = self.alloc(h, t, 0, lagging)
            h.phase_until = te + p["build_s"] + self.jit(h, "build", p["build_jitter_s"])
            h.build_workers = max(0, min(p["build_workers"], rem))
        if h.phase == "first_build" and t >= h.phase_until:
            te, workers = h.phase_until, h.build_workers
            ok, h.build_workers = workers >= p["build_workers"], 0
            if ok:
                h.state, h.phase, h.stuck, h.workers_started, h.last_error = ROUTING, "routing", False, "8/8", None
                h.valid_until, h.last_success, h.next_rebuild, h.epoch = te + p["ttl_s"], te, te + p["rebuild_s"], self.epoch
                h.routing_since = te
                self.became_routing.append(h.name)
                self.log(te, "/vl/prod/srcoord", h.name, f"{h.name} ROUTING (bootstrap={h.bootstrap_source})")
                if xmx_gib(h.xmx) >= p["oom_xmx_gib"] and lagging:
                    h.oom_at = te + p["oom_after_s"]
            else:
                h.stuck, h.phase, h.workers_started, h.last_error = True, "wedged", f"{workers}/8", NATIVE_THREAD
                h.build_epoch = self.epoch
                self.log(te, "/vl/prod/streamroute", h.name, BUILD_FAIL.format(epoch=self.epoch, started=workers))
        if h.state == WARMING and t - h.process_start > h.grace_s:
            self._eject(h, t, h.restart_cause or "world",
                        f"ejected {h.name}: not ROUTING within warmup grace {h.grace_s}s; quarantined 15m")

    # ---- views ----
    def fleet_of(self, name: str) -> str:
        return ADMIN[name]["fleet"] if name in ADMIN else "streamroute"

    def select(self, selector: str) -> list[str]:
        """Hosts a selector names: `*`/`all`, a fleet, an AZ glob or host globs, comma- or space-separated."""
        names = [n for n, h in self.hosts.items() if not h.terminated] + list(ADMIN)
        chosen: set[str] = set()
        for tok in re.split(r"[,\s]+", (selector or "").strip()):
            if tok in ("*", "all"):
                chosen |= set(names)
            elif tok:
                chosen |= {n for n in names if self.fleet_of(n) == tok or fnmatch.fnmatchcase(n, tok) or
                           fnmatch.fnmatchcase(ADMIN[n]["az"] if n in ADMIN else self.hosts[n].az, tok)}
        return [n for n in names if n in chosen]

    def hms(self, t: float) -> str:
        return datetime.fromtimestamp(t, self.tz).strftime("%H:%M:%S")

    def counts(self) -> dict:
        c = dict.fromkeys(STATES, 0)
        for h in self.hosts.values():
            if not h.terminated:
                c[h.state] += 1
        return c
