"""T04 "Purged Routes" data: the merchant registry, the merchant sets and the routemap-state write history from Nov 16
(docs/T04-build.md §1.1). The design resolves its sets against a base universe this repo doesn't have, so they come from
its documented fallback rules (overlay/data/merchant_sets.yaml), deterministic and seed-independent: every run and every
check sees the same keys. Times are epoch seconds; items are plain dicts (DynamoDB-typed only at the emulator port)."""

import copy
import hashlib
import random
from bisect import bisect_right
from datetime import datetime
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
PURGE_JOB = "pj-20261121-004208"
STATE = "routemap-state"
ROUTE, LIMITS = "ROUTE", "LIMITS"
SALT = "T04-mirror"
TOUCH_SEED = "4471"
SAVES_SEED = "4472"
HOT = "cell-7"
GENERAL_CELLS = ["cell-1", "cell-2", "cell-4", "cell-5", "cell-6", "cell-7", "cell-8", "cell-10", "cell-11", "cell-12"]
DEFAULT_SPEC = {"cells": [], "weights": {}, "pool_id": "pool-default", "fallback_cell": "cell-drain", "pinned": False,
                "residency": "US"}
NAMES = {
    "mch_10422": "Brightfield Outfitters", "mch_30988": "Corvid Supply Co.", "mch_41207": "Maple Ridge Mercantile",
    "mch_37755": "Tidewater Ceramics", "mch_11872": "Lumen & Pine", "mch_17336": "Ottoline Cycles",
    "mch_23518": "Fernhill Tea Co.", "mch_29047": "Kestrel Home", "mch_20871": "Harbor & Pine Goods",
    "mch_22764": "Northwind Cycle Co.", "mch_15110": "Saltmarsh Provisions", "mch_31156": "Copperline Studio",
    "mch_16233": "Halvorsen Home", "mch_19390": "Tallgrass Outfitters", "mch_12904": "Wildacre Farm Supply",
    "mch_26631": "Blue Heron Books", "mch_38140": "Larkspur Apothecary", "mch_27415": "Granite Peak Gear",
    "mch_44419": "Wrenfield Apparel", "mch_44420": "Quillstone Goods",
}
MUST_INCLUDE = ["mch_10422", "mch_30988", "mch_41207", "mch_37755", "mch_17336", "mch_23518", "mch_29047"]
MUST_EXCLUDE = ["mch_11872", "mch_12904", "mch_26631", "mch_38140", "mch_20871", "mch_22764", "mch_15110", "mch_31156",
                "mch_16233", "mch_19390"] + [f"mch_{n}" for n in range(44412, 44421)]
NEVER_TOUCHED = ["mch_30988", "mch_41207", "mch_37755"] + [f"mch_{n}" for n in range(44403, 44412)]
FRIDAY = [f"mch_{n}" for n in range(44403, 44412)]
SATURDAY = [f"mch_{n}" for n in range(44412, 44419)]
DAILY = {"mch_37755", "mch_12904", "mch_26631", "mch_38140"}  # their payout edits change daily to something else
KEY_MERCHANTS = {"mch_10422": 36, "mch_20871": 22, "mch_22764": 14, "mch_11872": 18}
NAMED_REWRITES = [("mch_17336", "routemap-scaler", "2026-11-21T10:20:04-05:00"),
                  ("mch_23518", "routemap-scaler", "2026-11-21T11:40:05-05:00"),
                  ("mch_29047", "routemap-scaler", "2026-11-21T12:20:03-05:00"),
                  ("mch_10422", "routemap-scaler", "2026-11-21T12:50:03.441-05:00")]
ADJ = ["Amber", "Birch", "Cedar", "Driftwood", "Elm", "Fern", "Golden", "Harbor", "Iron", "Juniper", "Kettle", "Linden",
       "Meadow", "North", "Oak", "Pine", "Quarry", "River", "Stone", "Thistle", "Upland", "Valley", "Willow", "Yarrow"]
NOUN = ["Goods", "Supply", "Outfitters", "Kitchen", "Studio", "Mercantile", "Provisions", "Workshop", "Trading Co.",
        "Botanicals", "Ceramics", "Apparel", "Books", "Coffee", "Gear", "Home", "Leather", "Paper Co.", "Tea Co.", "Toys"]
FRIDAY_ITEMS = {  # aws_seed rows 13-33: (time, route_id, idem_ns, cells, name)
    "mch_44403": ("2026-11-20T09:02:00-05:00", "rt-30a29ae9", "ins-d706be", ["cell-2", "cell-5"], "Alderbrook Goods"),
    "mch_44404": ("2026-11-20T10:14:31-05:00", "rt-488ae763", "ins-74ff36", ["cell-4", "cell-8"], "Bramblewood Kitchen"),
    "mch_44405": ("2026-11-20T11:03:12-05:00", "rt-977438b6", "ins-77b57c", ["cell-6", "cell-10"], "Cinderhill Outdoor"),
    "mch_44406": ("2026-11-20T12:47:55-05:00", "rt-6734a69e", "ins-04e694", ["cell-2", "cell-5"], "Dovetail Paper Co."),
    "mch_44407": ("2026-11-20T13:30:08-05:00", "rt-8e1cc58b", "ins-e9c5eb", ["cell-4", "cell-8"], "Elmstead Pottery"),
    "mch_44408": ("2026-11-20T14:52:40-05:00", "rt-5d72e283", "ins-236dc0", ["cell-6", "cell-10"], "Foxglove Botanicals"),
    "mch_44409": ("2026-11-20T16:21:19-05:00", "rt-f1088ab9", "ins-146a8c", ["cell-2", "cell-5"], "Greyfriar Leather"),
    "mch_44410": ("2026-11-20T17:44:02-05:00", "rt-b320b41f", "ins-e48510", ["cell-4", "cell-8"], "Hollowbrook Toys"),
    "mch_44411": ("2026-11-20T18:29:40-05:00", "rt-0570e392", "ins-2dc9f3", ["cell-6", "cell-10"], "Ironvale Supply"),
}
SATURDAY_ITEMS = {  # aws_seed rows 59-91
    "mch_44412": ("2026-11-21T09:05:12-05:00", "rt-08e9af24", "ins-3485fd", ["cell-4", "cell-8"], "Pinecrest Running"),
    "mch_44413": ("2026-11-21T09:41:50-05:00", "rt-7231a838", "ins-9a1f45", ["cell-6", "cell-10"], "Quarry Lane Books"),
    "mch_44414": ("2026-11-21T10:22:33-05:00", "rt-50c1d0b3", "ins-a93a80", ["cell-2", "cell-5"], "Rosewater Linen"),
    "mch_44415": ("2026-11-21T10:57:04-05:00", "rt-77ff8015", "ins-032aa0", ["cell-4", "cell-8"], "Stonebridge Tackle"),
    "mch_44416": ("2026-11-21T11:29:46-05:00", "rt-a9c4b795", "ins-62046d", ["cell-6", "cell-10"], "Thistle & Thorn"),
    "mch_44417": ("2026-11-21T12:06:18-05:00", "rt-97f5835a", "ins-eab4ff", ["cell-2", "cell-5"], "Umberfield Coffee"),
    "mch_44418": ("2026-11-21T12:40:27-05:00", "rt-9a744fa8", "ins-41a845", ["cell-4", "cell-8"], "Vesper Glassworks"),
}


def h(*parts) -> int:
    return int(hashlib.sha256(":".join(map(str, parts)).encode()).hexdigest(), 16)


def u(*parts) -> float:
    return (h(*parts) % (1 << 53)) / float(1 << 53)


def ts(text: str) -> float:
    return datetime.fromisoformat(text).timestamp()


def zulu(t: float, ms: bool = False) -> str:
    s = datetime.fromtimestamp(t, ET).astimezone(ZoneInfo("UTC")).strftime("%Y-%m-%dT%H:%M:%S.%f")
    return (s[:-3] if ms else s[:-7]) + "Z"


def key(mid: str, rec: str = ROUTE) -> tuple[str, str]:
    return (mid, rec)


def name_of(mid: str) -> str:
    if mid in NAMES:
        return NAMES[mid]
    n = h("T04-name", mid)
    return f"{ADJ[n % len(ADJ)]} {NOUN[(n // 97) % len(NOUN)]}"


def registry() -> tuple[list[str], list[str]]:
    """The 4,103 base merchants: 3,700 established (sparse in mch_10000..mch_43999, the named ones included) and the 403
    recent mch_44000..mch_44402. Returns (established, recent)."""
    named = sorted({m for m in NAMES if int(m[4:]) < 44000})
    pool = sorted((h("T04-registry", n), f"mch_{n}") for n in range(10000, 44000))
    est = list(named)
    for _, m in pool:
        if len(est) >= 3700:
            break
        if m not in est:
            est.append(m)
    return sorted(est), [f"mch_{n}" for n in range(44000, 44403)]


def merchant_sets() -> dict:
    """overlay/data/merchant_sets.yaml resolved by its fallback rules (docs/T04-build.md §1.1)."""
    est, recent = registry()
    excl = set(MUST_EXCLUDE)
    opt_out = list(MUST_INCLUDE)
    for _, m in sorted((h(f"{SALT}:{m}"), m) for m in est if m not in excl and m not in MUST_INCLUDE):
        if len(opt_out) >= 236:
            break
        opt_out.append(m)
    since_oct6 = [f"mch_{n}" for n in range(44277, 44403)]
    purged = sorted(set(opt_out) | set(since_oct6) | set(FRIDAY))
    named = [m for m, _, _ in NAMED_REWRITES]
    drawable = [m for m in purged if m not in set(FRIDAY) | set(NEVER_TOUCHED) | set(named)]
    drawn = sorted(drawable, key=lambda m: h(TOUCH_SEED, "rewrite", m))[:25]
    ticks = [ts(f"2026-11-21T{hh:02d}:{mm:02d}:00-05:00") for hh in range(7, 13) for mm in range(0, 60, 10)
             if (7, 10) <= (hh, mm) <= (12, 40)]
    named_ticks = {ts("2026-11-21T10:20:00-05:00"), ts("2026-11-21T11:40:00-05:00"), ts("2026-11-21T12:20:00-05:00")}
    free = sorted((t for t in ticks if t not in named_ticks), key=lambda t: h(TOUCH_SEED, "tick", t))[:21]
    reb = [ts(f"2026-11-21T{hh:02d}:35:00-05:00") for hh in (8, 9, 10, 11)]
    rewrites = [(m, "routemap-scaler", t + 5 + (h(TOUCH_SEED, "off", m) % 4)) for m, t in zip(drawn[:21], sorted(free))]
    rewrites += [(m, "routemap-rebalancer", t + 6 + (h(TOUCH_SEED, "off", m) % 4)) for m, t in zip(drawn[21:], reb)]
    rewrites += [(m, w, ts(t)) for m, w, t in NAMED_REWRITES]
    rewrites.sort(key=lambda x: x[2])
    rewritten = sorted(m for m, _, _ in rewrites)
    latent = sorted(set(purged) - set(rewritten))
    backlog = sorted((m for m in latent if m not in NEVER_TOUCHED), key=lambda m: h(SAVES_SEED, "backlog", m))[:14]
    return {"established": est, "recent": recent, "mirror_opt_out": sorted(opt_out), "onboarded_since_oct6": since_oct6,
            "friday_onboarded": FRIDAY, "purged": purged, "rewritten_before_t0": rewritten, "rewrites": rewrites,
            "latent_tombstones_at_t0": latent, "never_touched_before_horizon": NEVER_TOUCHED,
            "dashboard_retry_backlog": sorted(backlog), "saturday_onboarded": SATURDAY}


def base_route(mid: str) -> dict:
    n = h("T04-route", mid)
    a = GENERAL_CELLS[n % len(GENERAL_CELLS)]
    b = GENERAL_CELLS[(n // 11 + 1 + n % len(GENERAL_CELLS)) % len(GENERAL_CELLS)]
    if b == a:
        b = GENERAL_CELLS[(GENERAL_CELLS.index(a) + 3) % len(GENERAL_CELLS)]
    cells = sorted([a, b], key=lambda c: int(c.split("-")[1]))
    split = [50, 60, 70][(n // 7) % 3]
    weights = {cells[0]: split, cells[1]: 100 - split}
    sched = "daily" if mid in DAILY else ["daily", "daily", "weekly", "biweekly"][(n // 13) % 4]
    num = mid.split("_")[1]
    item = {"merchant_id": mid, "rec": ROUTE, "merchant_name": name_of(mid),
            "route_id": f"rt-{h('T04-rid', mid):x}"[:11], "idem_ns": f"ins-{h('T04-idem', mid):x}"[:10],
            "lifecycle": "ACTIVE", "route_version": 20 + n % 60, "publish_mode": "AUTO",
            "route_spec": {"cells": cells, "weights": weights,
                           "pool_id": "pool-use1-keyacct" if mid in KEY_MERCHANTS else "pool-use1-std",
                           "fallback_cell": "cell-3", "pinned": False, "residency": "US"},
            "settlement_profile": {"payout_schedule": sched, "currency": "USD", "bank_ref": f"sp-{num}-01"},
            "updated_by": "routemap-scaler", "updated_at": "2026-11-15T21:40:02Z"}
    if mid == "mch_10422":
        item["route_version"] = 86
        item["route_spec"].update(cells=["cell-7", "cell-11"], weights={"cell-7": 50, "cell-11": 50})
    return item


def base_limits(mid: str, at: str = "2026-10-01T14:00:00Z") -> dict:
    return {"merchant_id": mid, "rec": LIMITS, "daily_cap_usd": 250000, "per_txn_cap_usd": 10000, "hold_reason": None,
            "updated_by": "merchant-onboarding", "updated_at": at}


def onboard_items(mid: str, t: float, rid: str, ins: str, cells: list, name: str, labels: bool) -> tuple[dict, dict]:
    num = mid.split("_")[1]
    route = {"merchant_id": mid, "rec": ROUTE, "merchant_name": name, "route_id": rid, "idem_ns": ins,
             "lifecycle": "ACTIVE", "route_version": 1, "publish_mode": "AUTO",
             "route_spec": {"cells": list(cells), "weights": {cells[0]: 50, cells[1]: 50}, "pool_id": "pool-use1-std",
                            "fallback_cell": "cell-3", "pinned": False, "residency": "US"},
             "settlement_profile": {"payout_schedule": "daily", "currency": "USD", "bank_ref": f"sp-{num}-01"},
             "updated_by": "merchant-onboarding", "updated_at": zulu(t, ms=True)}
    if labels:
        route["labels"] = {}
    return route, base_limits(mid, zulu(t, ms=True))


def defaults_from(item: dict, writer: str, t: float) -> dict:
    """A workflow's (or the lenient API's) write of a purged or empty-celled route: the pre-#612 model's defaults."""
    out = {k: v for k, v in item.items() if k not in ("purge_job", "tombstoned_at")}
    out.update(lifecycle="ACTIVE", route_version=int(item.get("route_version", 0)) + 1, publish_mode="AUTO",
               route_spec=copy.deepcopy(DEFAULT_SPEC), settlement_profile=None, updated_by=writer,
               updated_at=zulu(t, ms=True))
    return out


def reweight(item: dict, writer: str, t: float, salt: str) -> dict:
    out = copy.deepcopy(item)
    cells = out["route_spec"]["cells"]
    if len(cells) == 2:
        split = [40, 45, 55, 60, 65, 70][h(salt, item["merchant_id"]) % 6]
        out["route_spec"]["weights"] = {cells[0]: split, cells[1]: 100 - split}
    out.update(route_version=int(item["route_version"]) + 1, updated_by=writer, updated_at=zulu(t, ms=True))
    return out


def move_off(item: dict, writer: str, t: float) -> dict:
    out = copy.deepcopy(item)
    spec = out["route_spec"]
    taken = set(spec["cells"])
    new = next(c for c in ("cell-12", "cell-8", "cell-10", "cell-4") if c not in taken)
    spec["cells"] = [new if c == HOT else c for c in spec["cells"]]
    spec["weights"] = {(new if c == HOT else c): w for c, w in spec["weights"].items()}
    out.update(route_version=int(item["route_version"]) + 1, updated_by=writer, updated_at=zulu(t, ms=True))
    return out


class History:
    """Every write to routemap-state as (t, item or None, meta), per key, in time order: what PITR and backups read."""

    def __init__(self):
        self.rows: dict[tuple, list] = {}
        self.times: dict[tuple, list] = {}

    def put(self, k: tuple, t: float, item: dict | None, meta: dict) -> None:
        rows, times = self.rows.setdefault(k, []), self.times.setdefault(k, [])
        i = bisect_right(times, t)
        rows.insert(i, (t, item, meta))
        times.insert(i, t)

    def at(self, k: tuple, t: float) -> dict | None:
        times = self.times.get(k) or []
        i = bisect_right(times, t)
        return self.rows[k][i - 1][1] if i else None

    def snapshot(self, t: float) -> dict[tuple, dict]:
        out = {}
        for k in self.rows:
            item = self.at(k, t)
            if item is not None:
                out[k] = item
        return out

    def last(self, k: tuple) -> dict | None:
        rows = self.rows.get(k) or []
        return rows[-1][1] if rows else None


def build_history(sets: dict, until: float) -> History:
    """Nov 16 00:00 -> `until` (13:12): the base items, routine workflow writes on healthy merchants, the seeded Friday
    and Saturday writes, the purge and the 29 rewrites. Deterministic (seed 4471 for touches)."""
    hist = History()
    t0 = ts("2026-11-16T00:00:00-05:00")
    purged, protected = set(sets["purged"]), set(MUST_EXCLUDE)
    for mid in sets["established"] + sets["recent"]:
        route = base_route(mid)
        if mid in sets["recent"]:
            route["updated_by"] = "merchant-onboarding"
        hist.put((mid, ROUTE), t0, route, {"writer": "base", "world_made": True})
        hist.put((mid, LIMITS), t0, base_limits(mid), {"writer": "base", "world_made": True})
    events = []  # (t, kind, args)
    for mid, (t, rid, ins, cells, name) in FRIDAY_ITEMS.items():
        events.append((ts(t), "onboard", (mid, rid, ins, cells, name, False)))
    for mid, (t, rid, ins, cells, name) in SATURDAY_ITEMS.items():
        events.append((ts(t), "onboard", (mid, rid, ins, cells, name, True)))
    events += [
        (ts("2026-11-20T17:20:15-05:00"), "set", ("mch_41207", {"route_spec": {
            "cells": ["cell-ca-2"], "weights": {"cell-ca-2": 100}, "pool_id": "pool-ca-1", "fallback_cell": "cell-ca-1",
            "pinned": False, "residency": "CA"}, "publish_mode": "AUTO", "updated_by": "rmctl:ravi.menon@vantleaf.com"},
            "rmctl:ravi.menon", "role:vl-rm-admin-role")),
        (ts("2026-11-20T19:30:02-05:00"), "set", ("mch_10422", {"weights": {"cell-7": 60, "cell-11": 40},
                                                                 "updated_by": "routemap-scaler"},
                                                   "routemap-scaler", "role:vl-routemap-scaler-task-role")),
        (ts("2026-11-20T19:31:12.640-05:00"), "settle", ("mch_37755", "weekly")),
        (ts("2026-11-21T10:02:31-05:00"), "settle", ("mch_12904", "weekly")),
        (ts("2026-11-21T10:15:44-05:00"), "pin", ("mch_11872", "cell-9")),
        (ts("2026-11-21T10:48:09-05:00"), "settle", ("mch_26631", "biweekly")),
        (ts("2026-11-21T11:20:41-05:00"), "hold", ("mch_30988",)),
        (ts("2026-11-21T11:37:52-05:00"), "settle", ("mch_38140", "weekly")),
    ]
    batch_t = [ts("2026-11-20T19:42:08.402-05:00") + 33.0667 * i for i in range(15)]
    batch_t[-1] = ts("2026-11-20T19:49:51.337-05:00")
    for i, mid in enumerate(sets["purged"]):
        events.append((batch_t[i // 25], "purge", (mid,)))
    for mid, writer, t in sets["rewrites"]:
        events.append((t, "rewrite", (mid, writer)))
    # routine workflow touches on healthy merchants (not purged, not protected, not Brightfield)
    eligible = [m for m in sets["established"] + sets["recent"] if m not in purged and m not in protected
                and m != "mch_10422"]
    tick, noon13 = t0 + 600, ts("2026-11-21T13:00:00-05:00")
    while tick < until:
        rng = random.Random(h(TOUCH_SEED, "routine", tick) % (1 << 63))
        for j, mid in enumerate(rng.sample(eligible, 70 if tick >= noon13 else 10)):
            events.append((tick + 5 + j * 0.05, "reweight", (mid, "routemap-scaler", tick)))
        tick += 600
    reb, busy = t0 + 2100, ts("2026-11-20T19:35:00-05:00")
    while reb < until:
        events.append((reb + 6, "rebalance", (12 if reb >= busy else 2, reb)))
        reb += 3600
    events.sort(key=lambda e: e[0])
    live = {k: hist.last(k) for k in hist.rows}
    for t, kind, args in events:
        if t >= until:
            continue
        _apply(hist, live, sets, t, kind, args)
    return hist


def _apply(hist: History, live: dict, sets: dict, t: float, kind: str, args: tuple) -> None:
    def put(k, item, writer, principal):
        live[k] = item
        hist.put(k, t, item, {"writer": writer, "principal": principal, "world_made": True})
    if kind == "onboard":
        mid, rid, ins, cells, name, labels = args
        route, limits = onboard_items(mid, t, rid, ins, cells, name, labels)
        put((mid, ROUTE), route, "merchant-onboarding", "role:vl-routemap-api-task-role")
        put((mid, LIMITS), limits, "merchant-onboarding", "role:vl-routemap-api-task-role")
    elif kind == "set":
        mid, fields, writer, principal = args
        item = copy.deepcopy(live[(mid, ROUTE)])
        if "weights" in fields:
            item["route_spec"]["weights"] = fields["weights"]
        if "route_spec" in fields:
            item["route_spec"] = fields["route_spec"]
        item.update({k: v for k, v in fields.items() if k in ("publish_mode", "updated_by")})
        item.update(route_version=int(item["route_version"]) + 1, updated_at=zulu(t, ms=True))
        put((mid, ROUTE), item, writer, principal)
    elif kind == "settle":
        mid, sched = args
        item = copy.deepcopy(live[(mid, ROUTE)])
        item["settlement_profile"]["payout_schedule"] = sched
        item.update(route_version=int(item["route_version"]) + 1, updated_by="merchant-dashboard",
                    updated_at=zulu(t, ms=True))
        put((mid, ROUTE), item, "merchant-dashboard", "role:vl-routemap-api-task-role")
    elif kind == "pin":
        mid, cell = args
        item = copy.deepcopy(live[(mid, ROUTE)])
        item["route_spec"].update(cells=[cell], weights={cell: 100}, pinned=True)
        item.update(route_version=int(item["route_version"]) + 1, updated_by="rmctl:grace.liu@vantleaf.com",
                    updated_at=zulu(t))
        put((mid, ROUTE), item, "rmctl:grace.liu", "role:vl-rm-admin-role")
    elif kind == "hold":
        mid, = args
        item = dict(live[(mid, LIMITS)], daily_cap_usd=2500, per_txn_cap_usd=500,
                    hold_reason="PAY-1911 card-testing pattern", updated_by="marco.ferri@vantleaf.com",
                    updated_at=zulu(t))
        put((mid, LIMITS), item, "rmctl:marco.ferri", "role:vl-rm-admin-role")
    elif kind == "purge":
        mid, = args
        item = {k: v for k, v in live[(mid, ROUTE)].items() if k not in ("route_spec", "settlement_profile")}
        item.update(lifecycle="PURGED", route_version=int(item["route_version"]) + 1, publish_mode="SUPPRESS",
                    purge_job=PURGE_JOB, tombstoned_at=zulu(t, ms=True), updated_by="rmctl:ravi.menon@vantleaf.com",
                    updated_at=zulu(t, ms=True))
        put((mid, ROUTE), item, "rmctl:ravi.menon", "persona:ravi.menon@PlatformEng")
    elif kind == "rewrite":
        mid, writer = args
        put((mid, ROUTE), defaults_from(live[(mid, ROUTE)], writer, t), writer, f"role:vl-{writer}-task-role")
    elif kind == "reweight":
        mid, writer, tick = args
        put((mid, ROUTE), reweight(live[(mid, ROUTE)], writer, t, f"routine:{tick}"), writer,
            "role:vl-routemap-scaler-task-role")
    elif kind == "rebalance":
        moves, tick = args
        purged, protected = set(sets["purged"]), set(MUST_EXCLUDE)
        on_hot = sorted((m for (m, r), it in live.items() if r == ROUTE and m not in purged and m not in protected
                         and m != "mch_10422" and HOT in (it.get("route_spec") or {}).get("cells", [])
                         and not it["route_spec"].get("pinned")), key=lambda m: h(TOUCH_SEED, "move", tick, m))
        for mid in on_hot[:moves]:
            put((mid, ROUTE), move_off(live[(mid, ROUTE)], "routemap-rebalancer", t), "routemap-rebalancer",
                "role:vl-routemap-rebalancer-task-role")


def to_ddb(value):
    """A plain value as a DynamoDB attribute value."""
    if value is None:
        return {"NULL": True}
    if isinstance(value, bool):
        return {"BOOL": value}
    if isinstance(value, (int, float)):
        return {"N": str(value)}
    if isinstance(value, str):
        return {"S": value}
    if isinstance(value, list):
        return {"L": [to_ddb(v) for v in value]}
    if isinstance(value, dict):
        return {"M": {k: to_ddb(v) for k, v in value.items()}}
    return {"S": str(value)}


def from_ddb(av):
    (kind, v), = av.items()
    if kind == "NULL":
        return None
    if kind == "BOOL":
        return bool(v)
    if kind == "N":
        f = float(v)
        return int(f) if f == int(f) else f
    if kind in ("S", "B"):
        return v
    if kind == "L":
        return [from_ddb(x) for x in v]
    if kind == "M":
        return {k: from_ddb(x) for k, x in v.items()}
    if kind in ("SS", "NS", "BS"):
        return sorted(v)
    return v


def item_ddb(item: dict) -> dict:
    return {k: to_ddb(v) for k, v in item.items()}


def item_plain(raw: dict) -> dict:
    return {k: from_ddb(v) for k, v in raw.items()}
