"""T03 payment events: demand on vl-payment-events, shard leases, per-shard backlog and cell lag (payments-graph,
sr-backlog), the ledger-writer capacity and the canary (docs/T03-build.md §1.7-1.8). Times are float epoch seconds.

Shards carry seeded demand weights (mean 1) and a seeded lease map, so cells differ. `P` and `cons` are in weight-1 units
(records / weight); the record backlog of shard s is `w[s] * (P - cons[s])`."""

from bisect import bisect_left

from .t03_fleet import ROUTING, SERVING
from .util import unit

DEMAND = [["06:00", 900], ["07:00", 1500], ["07:30", 1850], ["08:00", 2250], ["09:00", 2600], ["10:00", 2900],
          ["11:00", 3150], ["12:00", 3350], ["13:00", 3300], ["16:15", 2400]]
PARAMS = {"shards": 240, "cells": 8, "drain_per_host": 62, "lw_capacity": 6500, "lease_move_s": 90,
          "redeploy_factor": 0.7, "redeploy_s": 360, "shed_factor": 0.33, "presim_factor": 1.03,
          "jitter": [0.95, 1.05], "blend_s": 2700, "canary_shard": 6, "canary_lag_s": 120, "weights": [0.9, 1.1],
          "floor_s": [0.6, 2.6], "map_seed": "t03-presim"}


def _mins(hhmm: str) -> float:
    h, m = hhmm.split(":")[:2]
    return int(h) * 60 + int(m)


class Flow:
    def __init__(self, cfg: dict, seed: str, midnight: float, start: float):
        self.p = {**PARAMS, **(cfg.get("flow") or {})}
        self.table = [(_mins(a), float(b)) for a, b in cfg.get("demand_rps") or DEMAND]
        lo, hi = self.p["jitter"]
        self.jitter = lo + (hi - lo) * unit(seed, "demand")
        self.midnight, self.start = midnight, start
        n = self.p["shards"]
        lo, hi = self.p["weights"]
        raw = [lo + (hi - lo) * unit(self.p["map_seed"], "weight", s) for s in range(n)]
        self.w = [x * n / sum(raw) for x in raw]
        self.order = sorted(range(n), key=lambda s: unit(self.p["map_seed"], "lease", s))
        self.seed = seed
        self.routed = 0.0
        self.owner: list[str] = [""] * n
        self.cons = [0.0] * n
        self.P = 0.0
        self.hist_t: list[float] = []
        self.hist_p: list[float] = []
        self.redeploy_until: float | None = None
        self.lags: list[float] = [0.0] * self.p["cells"]
        self.lag_t: float | None = None

    # ---- demand and capacity ----
    def factor(self, t: float) -> float:
        f0 = self.p["presim_factor"]
        if t <= self.start:
            return f0
        w = min(1.0, (t - self.start) / self.p["blend_s"])
        return f0 + (self.jitter - f0) * w

    def demand(self, t: float) -> float:
        m = (t - self.midnight) / 60
        pts = self.table
        if m <= pts[0][0]:
            base = pts[0][1]
        elif m >= pts[-1][0]:
            base = pts[-1][1]
        else:
            i = bisect_left([a for a, _ in pts], m)
            (a, x), (b, y) = pts[i - 1], pts[i]
            base = x + (y - x) * (m - a) / (b - a)
        return base * self.factor(t)

    def capacity(self, t: float, shed: bool) -> float:
        f = self.p["shed_factor"] if shed else 1.0
        if self.redeploy_until is not None and t < self.redeploy_until:
            f *= self.p["redeploy_factor"]
        return self.p["lw_capacity"] * f

    def redeploy(self, t: float) -> None:
        self.redeploy_until = t + self.p["redeploy_s"]

    # ---- leases ----
    def assign(self, names: list[str]) -> None:
        for i, s in enumerate(self.order):
            self.owner[s] = names[i % len(names)]

    def leases(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for o in self.owner:
            out[o] = out.get(o, 0) + 1
        return out

    def rebalance(self, fleet, t: float, new_routing: list[str]) -> None:
        hosts = fleet.hosts
        for s, o in enumerate(self.owner):
            h = hosts.get(o)
            gone = h is None or h.terminated or (not h.proc and h.stopped_at is not None
                                                 and t - h.stopped_at >= self.p["lease_move_s"])
            if gone:
                cands = [n for n, x in hosts.items() if x.state == ROUTING]
                if cands:
                    counts = self.leases()
                    self.owner[s] = min(cands, key=lambda n: (counts.get(n, 0), n))
        for name in new_routing:
            counts = self.leases()
            members = max(1, len(fleet.members()))
            while counts.get(name, 0) < self.p["shards"] // members:
                donor = max(counts, key=lambda n: (counts[n], n))
                if counts[donor] <= counts.get(name, 0) + 1:
                    break
                s = self.owner.index(donor)
                self.owner[s] = name
                counts[donor] -= 1
                counts[name] = counts.get(name, 0) + 1

    # ---- one tick ----
    def tick(self, t: float, dt: float, fleet, shed: bool) -> None:
        n = self.p["shards"]
        self.P += self.demand(t) * dt / n
        self.hist_t.append(t)
        self.hist_p.append(self.P)
        by_owner: dict[str, list[int]] = {}
        for s, o in enumerate(self.owner):
            h = fleet.hosts.get(o)
            if h is not None and h.proc and h.state in SERVING:
                by_owner.setdefault(o, []).append(s)
        want = {}
        per = self.p["drain_per_host"] * dt
        for shards in by_owner.values():
            left = per
            shards.sort(key=lambda s: self.w[s] * (self.P - self.cons[s]))
            for i, s in enumerate(shards):
                r = min(self.w[s] * (self.P - self.cons[s]), left / (len(shards) - i))
                want[s], left = r, left - r
        total = sum(want.values())
        scale = min(1.0, self.capacity(t, shed) * dt / total) if total > 0 else 1.0
        for s, r in want.items():
            self.cons[s] += r * scale / self.w[s]
        self.routed += total * scale
        self.lag_t = None

    # ---- lag ----
    def floor(self, s: int, t: float) -> float:
        """Every shard reads a second or two behind what it has consumed: its consumer's poll and processing time."""
        lo, hi = self.p["floor_s"]
        return lo + (hi - lo) * unit(self.p["map_seed"], "poll", s, int(t // 60))

    def shard_lag(self, s: int, t: float) -> float:
        c, fl = self.cons[s], self.floor(s, t)
        if self.P - c < 1:
            return fl
        i = bisect_left(self.hist_p, c)
        if i == 0:
            return t - self.hist_t[0] + fl
        (t0, p0), (t1, p1) = (self.hist_t[i - 1], self.hist_p[i - 1]), (self.hist_t[i], self.hist_p[i])
        tc = t0 + (t1 - t0) * ((c - p0) / (p1 - p0) if p1 > p0 else 0)
        return t - tc + fl

    def cell_lags(self, t: float) -> list[float]:
        if self.lag_t != t:
            per = self.p["shards"] // self.p["cells"]
            self.lags = [self.shard_lag(min(range(c * per, (c + 1) * per), key=self.cons.__getitem__), t)
                         for c in range(self.p["cells"])]
            self.lag_t = t
        return self.lags

    def cell_of(self, s: int) -> int:
        return s // (self.p["shards"] // self.p["cells"])

    def shard_backlog(self, s: int) -> float:
        return self.w[s] * (self.P - self.cons[s])

    def cell_backlog(self, c: int) -> float:
        per = self.p["shards"] // self.p["cells"]
        return sum(self.shard_backlog(s) for s in range(c * per, (c + 1) * per))

    def backlog(self) -> float:
        return sum(self.shard_backlog(s) for s in range(self.p["shards"]))

    def canary_ok(self, fleet, t: float) -> bool:
        """The canary's synthetic event lands on one shard: it reaches the ledger in time while that shard's owner still
        routes (a STALE map still routes) and the shard is less than `canary_lag_s` behind."""
        s = self.p["canary_shard"]
        h = fleet.hosts.get(self.owner[s])
        return bool(h and h.proc and h.state in SERVING) and self.shard_lag(s, t) < self.p["canary_lag_s"]
