"""T03 payment events: demand on vl-payment-events, shard leases, per-shard backlog and cell lag (payments-graph,
sr-backlog), the ledger-writer capacity and the canary (docs/T03-build.md §1.7-1.8). Times are float epoch seconds."""

from bisect import bisect_left

from .t03_fleet import ROUTING, SERVING
from .util import unit

DEMAND = [["06:00", 900], ["07:00", 1500], ["07:30", 1850], ["08:00", 2250], ["09:00", 2600], ["10:00", 2900],
          ["11:00", 3150], ["12:00", 3350], ["13:00", 3300], ["16:15", 2400]]
PARAMS = {"shards": 240, "cells": 8, "drain_per_host": 62, "lw_capacity": 6500, "lease_move_s": 90,
          "redeploy_factor": 0.7, "redeploy_s": 360, "shed_factor": 0.33, "presim_factor": 1.03,
          "jitter": [0.95, 1.05], "blend_s": 2700, "canary_shard": 17, "canary_lag_s": 120}


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
        self.owner = [names[i % len(names)] for i in range(self.p["shards"])]

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
            shards.sort(key=lambda s: self.P - self.cons[s])
            for i, s in enumerate(shards):
                w = min(self.P - self.cons[s], left / (len(shards) - i))
                want[s], left = w, left - w
        total = sum(want.values())
        scale = min(1.0, self.capacity(t, shed) * dt / total) if total > 0 else 1.0
        for s, w in want.items():
            self.cons[s] += w * scale
        self.lag_t = None

    # ---- lag ----
    def shard_lag(self, s: int, t: float) -> float:
        c = self.cons[s]
        if self.P - c < 1:
            return 0.0
        i = bisect_left(self.hist_p, c)
        if i == 0:
            return t - self.hist_t[0]
        (t0, p0), (t1, p1) = (self.hist_t[i - 1], self.hist_p[i - 1]), (self.hist_t[i], self.hist_p[i])
        tc = t0 + (t1 - t0) * ((c - p0) / (p1 - p0) if p1 > p0 else 0)
        return max(0.0, t - tc)

    def cell_lags(self, t: float) -> list[float]:
        if self.lag_t != t:
            per = self.p["shards"] // self.p["cells"]
            self.lags = [self.shard_lag(min(range(c * per, (c + 1) * per), key=self.cons.__getitem__), t)
                         for c in range(self.p["cells"])]
            self.lag_t = t
        return self.lags

    def backlog(self) -> float:
        return sum(self.P - c for c in self.cons)

    def canary_ok(self, fleet, t: float) -> bool:
        s = self.p["canary_shard"]
        h = fleet.hosts.get(self.owner[s])
        cell = s // (self.p["shards"] // self.p["cells"])
        return bool(h and h.proc and h.state == ROUTING) and self.cell_lags(t)[cell] < self.p["canary_lag_s"]
