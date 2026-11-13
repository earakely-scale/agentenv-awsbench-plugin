"""Node DNS caches (the negative-cache herd), the ledger-writer reconnect storm and checkout traffic."""

from datetime import datetime, timedelta

from .util import iso, parse_time, unit


class Dns:
    """Each host cached NXDOMAIN for the ledger name and re-queries only when that entry expires.

    A host's boundaries are `first_negative_cache + offset + k * negative_ttl`; at a boundary it recovers if the record
    was non-empty then. `unbound-control flush` re-queries at once, which re-caches NXDOMAIN for a full TTL if the
    record is still empty.
    """

    def __init__(self, cfg: dict, start: datetime, seed: str, mode: str):
        self.name = cfg.get("name", "ledger.internal.vantleaf.io")
        self.resolver = cfg.get("resolver_ip", "10.20.0.2")
        self.neg_ttl = timedelta(seconds=float(cfg.get("negative_ttl_s", 900)))
        self.pos_ttl = timedelta(seconds=float(cfg.get("positive_ttl_s", 60)))
        first = parse_time(cfg["first_negative_cache"], start)
        self.history: list[tuple[datetime, bool]] = [(first - timedelta(seconds=1), False)]
        self.hosts: dict[str, dict] = {}
        for h in cfg["hosts"]:
            off = float(h.get("offset_s", 0))
            if mode == "train":
                off = 60 * unit(seed, "dns-offset", h["name"])
            until = first + timedelta(seconds=off)
            while until <= start:
                until += self.neg_ttl
            self.hosts[h["name"]] = {"state": "neg", "neg_until": until, "pos_since": None, "recovered_at": None,
                                     "offset_s": off, "flushes": []}

    def record_change(self, t: datetime, nonempty: bool) -> None:
        if self.history[-1][1] != nonempty:
            self.history.append((t, nonempty))

    def nonempty_at(self, t: datetime) -> bool:
        val = False
        for ts, ne in self.history:
            if ts <= t:
                val = ne
            else:
                break
        return val

    def _first_empty_after(self, since: datetime, until: datetime) -> datetime | None:
        for ts, ne in self.history:
            if since < ts <= until and not ne:
                return ts
        return None

    def update(self, host: str, t: datetime) -> dict:
        h = self.hosts[host]
        while True:
            if h["state"] == "neg" and h["neg_until"] <= t:
                b = h["neg_until"]
                if self.nonempty_at(b):
                    h.update(state="pos", pos_since=b)
                    h["recovered_at"] = h["recovered_at"] or b
                else:
                    h["neg_until"] = b + self.neg_ttl
            elif h["state"] == "pos":
                e = self._first_empty_after(h["pos_since"], t)
                if e is None:
                    break
                cached = e + self.pos_ttl
                h.update(state="neg", neg_until=cached + self.neg_ttl, pos_since=None)
            else:
                break
        return h

    def update_all(self, t: datetime) -> None:
        for host in self.hosts:
            self.update(host, t)

    def flush(self, host: str, t: datetime) -> None:
        h = self.update(host, t)
        h["flushes"].append(iso(t))
        if self.nonempty_at(t):
            if h["state"] != "pos":
                h.update(state="pos", pos_since=t)
            h["recovered_at"] = h["recovered_at"] or t
        else:
            h.update(state="neg", neg_until=t + self.neg_ttl, pos_since=None)

    def resolves(self, host: str, t: datetime) -> bool:
        return self.update(host, t)["state"] == "pos"

    def recovered_share(self, hosts: list[str], t: datetime) -> float:
        return sum(1 for h in hosts if self.resolves(h, t)) / len(hosts) if hosts else 0.0

    def short_answer(self, host: str, fqdn: str, t: datetime) -> str:
        world_live = self.live_fn(fqdn)
        if fqdn == self.name and host in self.hosts and not self.resolves(host, t):
            left = int((self.hosts[host]["neg_until"] - t).total_seconds())
            return f"NXDOMAIN (cached negative answer, {left}s left)"
        ips = [r["Value"] for r in world_live if r["Weight"] > 0]
        return ", ".join(sorted(ips)) if ips else "NXDOMAIN"

    live_fn = staticmethod(lambda fqdn: [])

    def export(self) -> dict:
        return {"per_host_recovered_at": {h: iso(v["recovered_at"]) for h, v in self.hosts.items()},
                "flushes": {h: v["flushes"] for h, v in self.hosts.items() if v["flushes"]},
                "ledger_nonempty_history": [[iso(t), ne] for t, ne in self.history]}


class Storm:
    """Ledger-writer load versus a connection-bound capacity, with hysteresis between regimes."""

    def __init__(self, cfg: dict, start: datetime):
        self.cfg = cfg
        self.per_task = float(cfg.get("capacity_per_task", 125))
        self.task_budget = int(cfg.get("task_budget", 16))
        self.over_budget_capacity = float(cfg.get("over_budget_capacity", 1000))
        self.max_tasks = int(cfg.get("max_tasks", 18))
        self.lw_running = int(cfg.get("ledger_writer_tasks", 12))
        self.lw_pending: tuple[datetime, int] | None = None
        self.disp_desired = int(cfg.get("dispatcher_tasks", 40))
        self.disp_max = int(cfg.get("dispatcher_max", 40))
        self.disp_base = self.disp_desired
        self.disp_revert_at: datetime | None = None
        self.disp_suspended = False
        self.backlog = float(cfg.get("webhook_backlog", 17600))
        self.dlq = int(cfg.get("webhook_dlq", 40))
        self.outbox = float(cfg.get("outbox_pending", 7500))
        self.regime = "healthy"
        self.above_s = 0.0
        self.below_s = 0.0
        self.collapsed_total = 0.0
        self.regime_timeline: list[list] = [[iso(start), "healthy"]]
        self.offered = 0.0
        self.capacity = self.capacity_now()
        self.series: list[dict] = []

    def capacity_now(self) -> float:
        n = self.lw_running
        return self.per_task * n if n <= self.task_budget else self.over_budget_capacity

    def dispatcher_running(self) -> int:
        return min(self.disp_desired, self.disp_max)

    def set_dispatcher_desired(self, n: int, t: datetime) -> None:
        self.disp_desired = n
        self.disp_revert_at = None if self.disp_suspended else t + timedelta(
            seconds=float(self.cfg.get("manual_desired_reverted_after_s", 120)))

    def set_ledger_writer_desired(self, n: int, t: datetime) -> None:
        self.lw_pending = (t + timedelta(seconds=float(self.cfg.get("task_start_s", 90))), min(n, self.max_tasks))

    def tick(self, t: datetime, dt: float, rs: float, lev: dict) -> None:
        if self.disp_revert_at and t >= self.disp_revert_at:
            self.disp_desired, self.disp_revert_at = self.disp_base, None
        if self.lw_pending and t >= self.lw_pending[0]:
            self.lw_running, self.lw_pending = self.lw_pending[1], None
        cap = self.capacity = self.capacity_now()
        mc = lev["max_concurrency"]
        running = self.dispatcher_running()
        arrivals = float(self.cfg.get("webhook_arrivals_per_s", 35))
        replay = lev["replay_rate"] if self.outbox > 0 else 0.0
        checkout = rs * (float(self.cfg.get("checkout_req_s", 90)) + replay)
        if lev["consumer_paused"]:
            dispatcher = 0.0
        else:
            dispatcher = rs * (running * mc / 0.4 if self.backlog > 0 else arrivals)
        if lev["admission"]:
            quota = lev["dispatcher_quota"]
            dispatcher = min(dispatcher, quota) + 0.02 * max(0.0, dispatcher - quota)
        load = checkout + dispatcher + float(self.cfg.get("other_req_s", 150))
        self.offered = load
        prev = self.regime
        if load > 1.3 * cap:
            self.above_s += dt
        else:
            self.above_s = 0.0
        self.below_s = self.below_s + dt if load < 0.8 * cap else 0.0
        if self.regime == "collapsed":
            if self.below_s >= float(self.cfg.get("exit_below_s", 180)):
                self.regime = "healthy"
        elif self.above_s >= float(self.cfg.get("collapse_after_s", 10)):
            self.regime = "collapsed"
        else:
            self.regime = "degraded" if load >= 0.8 * cap else "healthy"
        if self.regime == "collapsed":
            self.collapsed_total += dt
        if self.regime != prev:
            self.regime_timeline.append([iso(t), self.regime])
        if lev["consumer_paused"] or rs == 0:
            delivered = 0.0
        elif self.regime == "collapsed":
            delivered = rs * float(self.cfg.get("collapsed_webhook_per_s", 40))
        else:
            delivered = rs * running * mc / 0.4 if self.backlog > 0 else arrivals
        self.backlog = max(0.0, self.backlog + (arrivals - delivered) * dt)
        inflow = float(self.cfg.get("outbox_inflow_per_s", 15)) * (1 - rs)
        drain = lev["replay_rate"] * rs if self.regime != "collapsed" else 0.0
        self.outbox = max(0.0, self.outbox + (inflow - drain) * dt)

    def latency_ms(self) -> float:
        return {"healthy": 120.0, "degraded": 900.0, "collapsed": 2000.0}[self.regime]

    def export(self) -> dict:
        return {"collapsed_seconds_total": self.collapsed_total, "regime_timeline": self.regime_timeline,
                "regime": self.regime, "ledger_writer_tasks": self.lw_running,
                "dispatcher_desired": self.disp_desired, "webhook_backlog": round(self.backlog),
                "outbox_pending": round(self.outbox), "series": self.series}


def checkout_success(recovered: list[bool], regime: str, bad_share: float, cfg: dict) -> float:
    if not recovered:
        return 0.0
    rec_val = {"healthy": float(cfg.get("recovered_healthy", 0.997)) * (1 - 0.9 * bad_share),
               "degraded": float(cfg.get("recovered_degraded", 0.97)) * (1 - 0.9 * bad_share),
               "collapsed": float(cfg.get("recovered_collapsed", 0.35))}[regime]
    unrec = float(cfg.get("unrecovered_host", 0.04))
    return sum(rec_val if r else unrec for r in recovered) / len(recovered)
