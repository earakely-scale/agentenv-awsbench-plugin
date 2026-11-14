"""T05 vaultstore (docs/T05-build.md §1): pools and floors, membership from EC2 state and the capctl:state tag, the
index cold start (manifests, replica check, 512 partitions with the slow partition 311, rebuild-routing), placement and
its crash loop against a non-FULL index, the billing-usage batch with its coordinators and 14:00 lock, capctl's
operations and schedules, and the EC2 instances of the fleet, served to the agent. Times are epoch seconds."""

import fnmatch
import hashlib
import re
from datetime import UTC, datetime

from ..account import ACCOUNT
from ..errors import AwsError
from .util import unit

HALT, COLDSTART, READ, FULL, FAILED = "PROTECTIVE_HALT", "COLDSTART", "SERVING_READ", "SERVING_FULL", "FAILED"
SERVING, CRASHLOOP = "SERVING", "CRASHLOOP"
POOLS = {  # name: (subsystem, floor, members)
    "vs-front": ("frontend", 6, [f"vsfe-{n:04d}" for n in range(301, 313)]),
    "vs-blobindex": ("index", 16, [f"vsbi-{n:04d}" for n in range(101, 119)]),
    "vs-blobplace": ("placement", 10, [f"vsbp-{n:04d}" for n in range(201, 213)]),
    "vs-billing-aux": ("billing-aux", 4, ["vsba-0411", "vsba-0412", "vsba-0414", "vsba-0415", "vsba-0417", "vsba-0419",
                                          "vsba-0420", "vsba-0422", "vsba-0423", "vsba-0424"]),
    "vs-store-a": ("storage", 36, [f"vsst-{n:04d}" for n in range(1001, 1037)]),
    "vs-store-b": ("storage", 36, [f"vsst-{n:04d}" for n in range(2001, 2037)]),
    "vs-store-c": ("storage", 36, [f"vsst-{n:04d}" for n in range(3001, 3037)]),
    "vs-gc": ("gc", 2, [f"vsgc-{n:04d}" for n in range(801, 805)]),
    "vs-repl": ("repl", 3, [f"vsrp-{n:04d}" for n in range(901, 907)]),
}
COORDINATORS = ("vsba-0411", "vsba-0417")
SMART = {"vsba-0412": "FAILED", "vsba-0415": "FAILED", "vsba-0419": "FAILED", "vsba-0423": "FAILED",
         "vsba-0420": "PRE-FAIL", "vsba-0424": "PRE-FAIL"}
MEDIA_ERRORS = {"vsba-0412": 3962, "vsba-0415": 3518, "vsba-0419": 4120, "vsba-0423": 3307, "vsba-0420": 212,
                "vsba-0424": 188}
P99 = {"vsba-0411": 1840, "vsba-0417": 1610, "vsba-0420": 640, "vsba-0424": 610, "vsba-0414": 420, "vsba-0422": 400,
       "vsba-0412": 3410, "vsba-0415": 2980, "vsba-0419": 2440, "vsba-0423": 2120,
       "vsbi-0104": 2210, "vsbi-0109": 2190, "vsbi-0113": 2050, "vsbi-0117": 1980, "vsbi-0102": 1410, "vsbi-0111": 1390,
       "vsbp-0203": 1720, "vsbp-0206": 1690, "vsbp-0208": 1600, "vsbp-0211": 1580, "vsbp-0201": 1210, "vsbp-0210": 1190}
BASTION = "vl-ops-bastion-01"
OP_0007 = {"op": "rm-20261120-0007", "kind": "remove", "ts": "10:24:51", "user": "tomas.ilyin", "reason": "OPS-4471",
           "command": "remove --pool vs-b* --select slowest --count 4 --reason OPS-4471", "glob": "vs-b*",
           "matched": ["vs-billing-aux", "vs-blobindex", "vs-blobplace"],
           "hosts": {"vs-billing-aux": ["vsba-0412", "vsba-0415", "vsba-0419", "vsba-0423"],
                     "vs-blobindex": ["vsbi-0104", "vsbi-0109", "vsbi-0113", "vsbi-0117"],
                     "vs-blobplace": ["vsbp-0203", "vsbp-0206", "vsbp-0208", "vsbp-0211"]},
           "cause": "persona:tomas.ilyin"}
PARAMS = {"verify_manifests_min": 8.0, "rate_per_min": 7.0, "slow_partition": 311, "slow_partition_min": 9.0,
          "partitions": 512, "rebuild_routing_min": 14.0, "placement_to_serving_min": 20.0, "placement_fail_min": 3.0,
          "crashloop_retry_s": 90, "crashloop_slowdown": 0.6, "fresh_per_hosts": 4, "fresh_add_min": 25.0,
          "join_s": 45, "stop_s": 30, "work_left_h": 4.5, "slow_keys": 139.7e6}
JITTER = {"verify_manifests_min": (7.5, 8.5), "rate_per_min": (6.8, 7.2), "slow_partition_min": (8.0, 10.0),
          "rebuild_routing_min": (13.0, 15.0), "placement_to_serving_min": (19.0, 21.0)}
STATE_CODE = {HALT: 0, FAILED: 0, COLDSTART: 1, READ: 2, FULL: 3, SERVING: 3, CRASHLOOP: 4}
AZS = ("us-east-1a", "us-east-1b", "us-east-1c")
SUBNETS = {"us-east-1a": "subnet-0a41c7e2d9b3f6a10", "us-east-1b": "subnet-0b52d8f3eac4071b2",
           "us-east-1c": "subnet-0c63e904fbd5182c3"}
VPC = "vpc-0d7e2a91c4b6f3e58"
TYPES = {"index": "i4i.4xlarge", "placement": "i4i.2xlarge", "billing-aux": "i4i.2xlarge", "frontend": "c7i.2xlarge",
         "storage": "i4i.8xlarge", "gc": "m7i.xlarge", "repl": "m7i.2xlarge", "ops": "t3.medium"}


def instance_id(name: str) -> str:
    return "i-0" + hashlib.sha256(f"t05|{name}".encode()).hexdigest()[:16]


def private_ip(name: str) -> str:
    h = hashlib.sha256(f"t05-ip|{name}".encode()).digest()
    return f"10.42.{16 + h[0] % 48}.{4 + h[1] % 250}"


class Host:
    __slots__ = (
        "az",
        "fresh",
        "iid",
        "ip",
        "join_at",
        "join_cause",
        "launched",
        "member",
        "name",
        "next_state",
        "p99",
        "pool",
        "proc",
        "role",
        "smart",
        "state",
        "state_until",
        "sub",
        "tags",
    )

    def __init__(self, name: str, pool: str | None, sub: str, role: str, az: str, launched: float):
        self.name, self.pool, self.sub, self.role, self.az, self.launched = name, pool, sub, role, az, launched
        self.iid, self.ip = instance_id(name), private_ip(name)
        self.state, self.state_until, self.next_state = "running", None, None
        self.proc, self.member, self.join_at, self.fresh, self.join_cause = True, True, None, False, "world"
        self.smart = SMART.get(name, "ok")
        self.p99 = P99.get(name, 0)
        self.tags = {"Name": name, "vl:system": "vaultstore" if pool else "ops-bastion", "Environment": "prod"}
        if pool:
            self.tags.update({"Pool": pool, "Subsystem": sub, "Role": role, "capctl:state": "active"})


class Vaultstore:
    def __init__(self, mech, cfg: dict):
        self.m = mech
        self.p = {**PARAMS, **(cfg.get("vaultstore") or {})}
        if mech.w.mode == "train":
            for k, (lo, hi) in JITTER.items():
                self.p[k] = lo + (hi - lo) * unit(mech.w.seed, "t05-physics", k)
        self.hosts: dict[str, Host] = {}
        self.pool_hosts = {pool: list(names) for pool, (_, _, names) in POOLS.items()}
        for pool, (sub, _, names) in POOLS.items():
            for i, n in enumerate(names):
                role = "coordinator" if n in COORDINATORS else "worker" if sub == "billing-aux" else "member"
                h = Host(n, pool, sub, role, AZS[i % 3], mech.at("2026-11-02T04:11:20-05:00"))
                if not h.p99:
                    h.p99 = int((880 + 420 * unit("t05-p99", n)) if sub == "index" else
                                (700 + 450 * unit("t05-p99", n)) if sub == "placement" else 300 + 500 * unit("t05-p99", n))
                self.hosts[n] = h
        self.hosts[BASTION] = Host(BASTION, None, "ops", "bastion", "us-east-1a", mech.at("2026-10-28T03:00:00-04:00"))
        self.idx = {"state": HALT, "since": mech.at("10:25:40"), "progress_s": 0.0, "started": None, "fresh": 0,
                    "checked": False, "last": None}
        self.pl = {"state": HALT, "since": mech.at("10:25:58"), "boot_until": None, "fail_at": None, "next_retry": None,
                   "restarts": 0, "started": None}
        self.batch = {"state": "PAUSED", "work_left_h": float(self.p["work_left_h"]), "aborted_at": None,
                      "abort_cause": None, "locked": False, "locked_at": None, "locked_members": None,
                      "throughput": None}
        self.ops: list[dict] = []
        self.op_seq = 7
        self.scheds: dict[str, dict] = {}
        self.check_restarts: list[dict] = []
        self.placement_starts: list[dict] = []
        self.state_changes: list[dict] = []
        self.membership_changes: list[dict] = []
        self.restart_counts: list[float] = []
        self.bot: list[tuple[float, str]] = []
        self.audit: list[tuple[float, str, str]] = []
        self.idx_log: list[tuple[float, str, str]] = []
        self.pl_log: list[tuple[float, str, str]] = []
        self.billing_log: list[tuple[float, str, str]] = []
        self.journal: list[tuple[float, str, str]] = []
        self.ec2_writes: list[dict] = []

    # ---- names and selection ----
    def pool_names(self, glob: str) -> list[str]:
        return [p for p in POOLS if fnmatch.fnmatchcase(p, glob)]

    def members(self, pool: str) -> list[str]:
        return [n for n in self.pool_hosts[pool] if self.hosts[n].member]

    def live(self, pool: str) -> int:
        return len(self.members(pool))

    def is_member(self, host: str, pool: str | None = None) -> bool:
        h = self.hosts.get(host)
        return bool(h and h.member and (pool is None or h.pool == pool))

    def select(self, selector: str) -> list[str]:
        out = []
        for part in [s.strip() for s in str(selector or "").split(",") if s.strip()]:
            if part in ("vaultstore", "all", "*"):
                out += [n for n in self.hosts if n != BASTION]
            elif part in ("ops-bastion", "bastion"):
                out.append(BASTION)
            elif part in POOLS:
                out += self.pool_hosts[part]
            elif part in AZS:
                out += [n for n, h in self.hosts.items() if h.az == part]
            else:
                out += [n for n in self.hosts if fnmatch.fnmatchcase(n, part)]
        return list(dict.fromkeys(out))

    # ---- the start ----
    def start(self, t0: float) -> None:
        """10:24:51 rm-20261120-0007 (world-made, as Tomas ran it), sched-0192 created 10:25:12, the guards' halts."""
        op = dict(OP_0007, ts=self.m.at(OP_0007["ts"]))
        for pool, names in op["hosts"].items():
            for n in names:
                h = self.hosts[n]
                h.tags.update({"capctl:state": "removed", "capctl:op": op["op"]})
                h.state, h.proc, h.member = "stopped", False, False
        op["live_before"] = {"vs-billing-aux": 10, "vs-blobindex": 18, "vs-blobplace": 12}
        op["live_after"] = {"vs-billing-aux": 6, "vs-blobindex": 14, "vs-blobplace": 8}
        self.ops.append(op)
        for sid, row in (self.m.cfg.get("schedules") or {}).items():
            self.scheds[sid] = {"id": sid, "at": self.m.at(row["at"]), "owner": row["owner"], "command": row["command"],
                                "state": "PENDING", "created": self.m.at(row.get("created", "2026-11-19T12:00:00-05:00")),
                                "cancelled_at": None, "cancelled_by": None, "fired_at": None, "op": None,
                                "principal": row.get("principal", f"persona:{row['owner']}")}
        s192 = self.scheds.get("sched-0192")
        if s192 and self.m.w.mode == "train":
            s192["at"] += -600 * unit(self.m.w.seed, "t05-sched-0192")
        self.audit += [(self.m.at("10:24:51"), BASTION, 'user=tomas.ilyin cmd="capctl remove --pool vs-b* --select '
                        'slowest --count 4 --reason OPS-4471" matched_pools=vs-billing-aux,vs-blobindex,vs-blobplace '
                        'per_pool_count=4 op=rm-20261120-0007'),
                       (self.m.at("10:24:51") + 0.4, BASTION, "op=rm-20261120-0007 selected vs-billing-aux: vsba-0412,"
                        "vsba-0415,vsba-0419,vsba-0423 | vs-blobindex: vsbi-0104,vsbi-0109,vsbi-0113,vsbi-0117 | "
                        "vs-blobplace: vsbp-0203,vsbp-0206,vsbp-0208,vsbp-0211"),
                       (self.m.at("10:24:53"), BASTION, 'op=rm-20261120-0007 completed removed=12 (vs-billing-aux 4, '
                        'vs-blobindex 4, vs-blobplace 4) undo="capctl restore --op rm-20261120-0007"'),
                       (self.m.at("10:25:12"), BASTION, 'user=tomas.ilyin cmd="capctl schedule add --at 2026-11-20T'
                        f'{self.hms(s192["at"] if s192 else self.m.at("11:40"))[:5]} -- remove --pool vs-b* --select '
                        'slowest --count 2 --reason OPS-4471-step2" schedule=sched-0192 state=PENDING')]
        self.idx_log += [(self.m.at("10:25:39"), "guard", "WARN membership: vsbi-0104,vsbi-0109,vsbi-0113,vsbi-0117 left "
                          "(capctl op rm-20261120-0007)"),
                         (self.m.at("10:25:40"), "guard", "ERROR guard: live members 14 below serving floor 16 -> "
                          "PROTECTIVE_HALT. Serving stopped. Recovery requires cold start (see PLAT vaultstore v3 "
                          "architecture s6)")]
        self.pl_log += [(self.m.at("10:25:58"), "guard", "ERROR guard: live 8 below floor 10; index unavailable -> "
                         "PROTECTIVE_HALT")]
        self.billing_log += [(self.m.at("10:25:58"), "billing-usage", "WARN billing-usage PAUSED: index unavailable; "
                              "work_left=4.5h@full; throughput=0.67; worker_set_lock=2026-11-20T14:00; invoice_run=18:00")]
        for n in [x for names in op["hosts"].values() for x in names]:
            unit_name = f"vaultstore-{self.hosts[n].sub}"
            self.journal.append((self.m.at("10:24:52"), n, f"{unit_name}: capctl: removed by op rm-20261120-0007; "
                                 + ("replicas retained for 24 h; " if self.hosts[n].sub != "billing-aux" else "")
                                 + "shutting down"))
        self.journal += [(self.m.at("09:58:12"), "vsba-0412", "kernel: nvme0: I/O error, dev nvme0n1, sector 88172032 op "
                          "0x0:(READ)"),
                         (self.m.at("10:02:41"), "vsba-0420", "kernel: nvme0: corrected media error, dev nvme0n1 (count "
                          "212)"),
                         (self.m.at("10:25:58"), "vsba-0411", "vaultstore-billing-aux: coordinator: checkpoint 41/127 "
                          "held; batch PAUSED (index unavailable)"),
                         (self.m.at("10:25:58"), "vsba-0417", "vaultstore-billing-aux: coordinator: checkpoint 41/127 "
                          "held; batch PAUSED (index unavailable)")]

    # ---- helpers ----
    def hms(self, t: float) -> str:
        return datetime.fromtimestamp(t, self.m.tz).strftime("%H:%M:%S")

    def next_op(self, kind: str) -> str:
        self.op_seq += 1
        return f"{kind}-20261120-{self.op_seq:04d}"

    def throughput(self) -> float:
        mem = self.members("vs-billing-aux")
        failed = sum(1 for n in mem if self.hosts[n].smart == "FAILED")
        pre = sum(1 for n in mem if self.hosts[n].smart == "PRE-FAIL")
        return 1.0 / (1 + 0.5 * failed + 0.25 * pre)

    def manifests_s(self) -> float:
        return 60 * (self.p["verify_manifests_min"] + self.p["fresh_add_min"] * (self.idx["fresh"] // self.p[
            "fresh_per_hosts"]))

    def marks(self) -> dict:
        """The index check's phase boundaries in seconds of check progress."""
        m = self.manifests_s()
        stall = m + 60 * self.p["slow_partition"] / self.p["rate_per_min"]
        stall_end = stall + 60 * self.p["slow_partition_min"]
        read = stall_end + 60 * (self.p["partitions"] - self.p["slow_partition"] - 1) / self.p["rate_per_min"]
        return {"manifests": m, "stall": stall, "stall_end": stall_end, "read": read,
                "full": read + 60 * self.p["rebuild_routing_min"]}

    def partitions(self) -> tuple[int, int, float]:
        """(partitions verified, current partition, keys verified of partition 311) from the check's progress."""
        p, k = self.idx["progress_s"], self.marks()
        if p < k["manifests"]:
            return 0, 0, 0.0
        if p < k["stall"]:
            done = int((p - k["manifests"]) / 60 * self.p["rate_per_min"])
            return done, done, 0.0
        if p < k["stall_end"]:
            frac = (p - k["stall"]) / (k["stall_end"] - k["stall"])
            return self.p["slow_partition"], self.p["slow_partition"], frac * self.p["slow_keys"]
        done = min(self.p["partitions"], self.p["slow_partition"] + 1 + int((p - k["stall_end"]) / 60 *
                                                                            self.p["rate_per_min"]))
        return done, min(done, self.p["partitions"] - 1), self.p["slow_keys"]

    def phase(self) -> str | None:
        st = self.idx["state"]
        if st not in (COLDSTART, READ):
            return None
        p, k = self.idx["progress_s"], self.marks()
        return ("verify-manifests" if p < k["manifests"] else "verify-partitions" if p < k["read"] else
                "rebuild-routing")

    def set_index(self, state: str, t: float, why: str = "") -> None:
        if self.idx["state"] != state:
            self.state_changes.append({"t": t, "subsystem": "index", "from": self.idx["state"], "to": state, "why": why})
            self.idx.update(state=state, since=t)
            self.bot.append((t, f"vaultstore index {state}" + (f" ({why})" if why else "")))

    def set_placement(self, state: str, t: float, why: str = "") -> None:
        if self.pl["state"] != state:
            self.state_changes.append({"t": t, "subsystem": "placement", "from": self.pl["state"], "to": state,
                                       "why": why})
            self.pl.update(state=state, since=t)
            if state != CRASHLOOP or why != "retry":
                self.bot.append((t, f"vaultstore placement {state}" + (f" ({why})" if why else "")))

    # ---- membership ----
    def eligible(self, h: Host) -> bool:
        return h.pool is not None and h.state == "running" and h.proc and h.tags.get("capctl:state") != "removed"

    def refresh(self, t: float, cause: str) -> list[str]:
        """Apply membership after a change at `t`: hosts that stopped being eligible leave at once, eligible ones join
        `join_s` after they became eligible. Returns the pools whose membership changed."""
        changed = []
        for h in self.hosts.values():
            if h.pool is None:
                continue
            if h.member and not self.eligible(h):
                h.member, h.join_at = False, None
                changed.append(h.pool)
                self.membership_changes.append({"t": t, "host": h.name, "pool": h.pool, "change": "left",
                                                "cause": cause})
                self.journal.append((t, h.name, f"vaultstore-{h.sub}: membership: left pool {h.pool}"))
            elif not h.member and self.eligible(h) and h.join_at is None:
                h.join_at = t + self.p["join_s"]
        return changed

    def joins(self, t: float) -> tuple[list[str], str]:
        """Hosts whose join time came: (the pools that changed, the cause of the first vs-blobindex join or "world")."""
        changed, cause = [], "world"
        for h in self.hosts.values():
            if not h.member and h.join_at is not None and t >= h.join_at:
                if self.eligible(h):
                    h.member = True
                    changed.append(h.pool)
                    if h.pool == "vs-blobindex" and cause == "world":
                        cause = h.join_cause
                    self.membership_changes.append({"t": h.join_at, "host": h.name, "pool": h.pool, "change": "joined",
                                                    "cause": h.join_cause})
                    self.journal.append((h.join_at, h.name, f"vaultstore-{h.sub}: membership: joined pool {h.pool}"))
                h.join_at = None
        return changed, cause

    def membership_changed(self, pools: list[str], t: float, cause: str) -> None:
        """A vs-blobindex membership change restarts a running index check (v3 §6); floors are re-checked."""
        if "vs-blobindex" in pools and self.idx["state"] == COLDSTART:
            self.restart_check(t, cause, "vs-blobindex membership changed")
        self.check_floors(t)

    def check_floors(self, t: float) -> None:
        if self.live("vs-blobindex") < 16 and self.idx["state"] in (READ, FULL):
            self.set_index(HALT, t, f"live {self.live('vs-blobindex')} below serving floor 16")
            self.idx_log.append((t, "guard", f"ERROR guard: live members {self.live('vs-blobindex')} below serving floor "
                                 "16 -> PROTECTIVE_HALT"))
        if self.live("vs-blobplace") < 10 and self.pl["state"] != HALT:
            self.set_placement(HALT, t, f"live {self.live('vs-blobplace')} below floor 10")
            self.pl.update(boot_until=None, fail_at=None, next_retry=None)
            self.pl_log.append((t, "guard", f"ERROR guard: live {self.live('vs-blobplace')} below floor 10 -> "
                                "PROTECTIVE_HALT"))

    # ---- the index check ----
    def restart_check(self, t: float, cause: str, reason: str) -> None:
        prior = self.idx["state"]
        done, cur, _ = self.partitions()
        self.check_restarts.append({"t": t, "subsystem": "index", "cause": cause, "prior_state": prior,
                                    "phase": self.phase(), "partitions_done": done, "reason": reason})
        self.idx.update(progress_s=0.0, started=t, checked=False,
                        fresh=sum(1 for n in self.members("vs-blobindex") if self.hosts[n].fresh))
        self.set_index(COLDSTART, t, "check restarted")
        self.idx_log.append((t, "coldstart", f"WARN coldstart: index check restarted ({reason}); previous check at "
                             f"{self.phase() or 'verify-manifests'} {done}/{self.p['partitions']} discarded; phase "
                             "verify-manifests"))

    def start_index(self, t: float, cause: str) -> str:
        if self.idx["state"] == COLDSTART:
            self.restart_check(t, cause, "second coldstart while the check was running")
            return "restarted"
        if self.idx["state"] in (READ, FULL):
            return "serving"
        self.idx.update(progress_s=0.0, started=t, checked=False,
                        fresh=sum(1 for n in self.members("vs-blobindex") if self.hosts[n].fresh))
        self.set_index(COLDSTART, t, "cold start")
        self.idx_log.append((t, "coldstart", f"INFO coldstart: index cold start begun (live "
                             f"{self.live('vs-blobindex')}/16); phase verify-manifests"))
        return "started"

    def abort_index(self, t: float, cause: str) -> str:
        if self.idx["state"] != COLDSTART:
            return "idle"
        done, _, _ = self.partitions()
        self.check_restarts.append({"t": t, "subsystem": "index", "cause": cause, "prior_state": COLDSTART,
                                    "phase": self.phase(), "partitions_done": done, "reason": "coldstart --abort"})
        self.idx.update(progress_s=0.0, started=None)
        self.set_index(HALT, t, "check aborted")
        self.idx_log.append((t, "coldstart", f"WARN coldstart: index check aborted at {done}/{self.p['partitions']}; "
                             "PROTECTIVE_HALT"))
        return "aborted"

    def start_placement(self, t: float, cause: str) -> str:
        if self.live("vs-blobplace") < 10:
            return "below_floor"
        if self.pl["state"] == SERVING:
            return "serving"
        self.placement_starts.append({"t": t, "cause": cause, "index_state": self.idx["state"],
                                      "placement_state": self.pl["state"]})
        self.pl.update(started=t, next_retry=None)
        if self.idx["state"] == FULL:
            self.pl.update(boot_until=t + 60 * self.p["placement_to_serving_min"], fail_at=None)
            self.pl_log.append((t, "coldstart", "INFO coldstart: placement starting; allocation-map validation against "
                                "index SERVING_FULL"))
        else:
            self.pl.update(boot_until=None, fail_at=t + 60 * self.p["placement_fail_min"])
            self.pl_log.append((t, "coldstart", "INFO coldstart: placement starting; allocation-map validation against "
                                f"index {self.idx['state']}"))
        self.set_placement(COLDSTART, t, "cold start")
        return "started"

    def abort_placement(self, t: float) -> str:
        if self.pl["state"] not in (COLDSTART, CRASHLOOP):
            return "idle"
        self.pl.update(boot_until=None, fail_at=None, next_retry=None)
        self.set_placement(HALT, t, "aborted")
        self.pl_log.append((t, "coldstart", "WARN coldstart: placement aborted; PROTECTIVE_HALT"))
        return "aborted"

    def tick(self, t: float, dt: float) -> None:
        self.membership_changed(self.refresh(t, "world"), t, "world")
        changed, cause = self.joins(t)
        self.membership_changed(changed, t, cause)
        if self.idx["state"] in (COLDSTART, READ):
            before = self.partitions()[0]
            self.idx["progress_s"] += dt * (self.p["crashloop_slowdown"] if self.pl["state"] == CRASHLOOP else 1.0)
            k = self.marks()
            p = self.idx["progress_s"]
            if not self.idx["checked"] and p >= k["manifests"]:
                self.idx["checked"] = True
                if self.live("vs-blobindex") < 16:
                    self.idx.update(progress_s=0.0, started=None, last="FAILED")
                    self.set_index(HALT, t, "replica check FAILED")
                    self.idx_log.append((t, "coldstart", "ERROR coldstart: index cold start FAILED at replica check: "
                                         f"41/512 partitions under-replicated (live {self.live('vs-blobindex')} < 16); "
                                         "still PROTECTIVE_HALT"))
                    return self._placement(t)
                self.idx_log.append((t, "coldstart", "INFO coldstart: verify-manifests done; replica check passed "
                                     f"(live {self.live('vs-blobindex')}/16); phase verify-partitions"))
            if self.idx["state"] == COLDSTART and p >= k["read"]:
                self.set_index(READ, t, "partitions verified")
                self.idx_log.append((t, "coldstart", "INFO coldstart: verify-partitions 512/512 done; SERVING_READ; "
                                     "phase rebuild-routing"))
            if self.idx["state"] == READ and p >= k["full"]:
                self.set_index(FULL, t, "routing rebuilt")
                self.idx_log.append((t, "coldstart", "INFO coldstart: rebuild-routing done; SERVING_FULL"))
            after = self.partitions()[0]
            if after == self.p["partitions"] and before < after:
                pass
        self._placement(t)
        self._batch(t, dt)

    def _placement(self, t: float) -> None:
        pl = self.pl
        if pl["state"] == COLDSTART and pl["fail_at"] is not None and t >= pl["fail_at"]:
            pl.update(fail_at=None, next_retry=t + self.p["crashloop_retry_s"])
            self.restart_counts.append(t)
            self.set_placement(CRASHLOOP, t, "allocation-map validation failed: index not SERVING_FULL")
            self.pl_log.append((t, "coldstart", f"ERROR placement: allocation-map validation failed: index is "
                                f"{self.idx['state']}, need SERVING_FULL; restarting in {self.p['crashloop_retry_s']}s"))
        elif pl["state"] == CRASHLOOP and pl["next_retry"] is not None and t >= pl["next_retry"]:
            self.restart_counts.append(t)
            if self.idx["state"] == FULL:
                pl.update(next_retry=None, boot_until=t + 60 * self.p["placement_to_serving_min"])
                self.set_placement(COLDSTART, t, "index SERVING_FULL; validation passed on restart")
                self.pl_log.append((t, "coldstart", "INFO placement: restart; allocation-map validation passed "
                                    "(index SERVING_FULL)"))
            else:
                pl["next_retry"] = t + self.p["crashloop_retry_s"]
                self.pl_log.append((t, "coldstart", f"ERROR placement: allocation-map validation failed: index is "
                                    f"{self.idx['state']}, need SERVING_FULL; restarting in "
                                    f"{self.p['crashloop_retry_s']}s"))
        elif pl["state"] == COLDSTART and pl["boot_until"] is not None and t >= pl["boot_until"]:
            pl["boot_until"] = None
            self.set_placement(SERVING, t, "cold start complete")
            self.pl_log.append((t, "coldstart", "INFO coldstart: placement SERVING"))

    # ---- billing-usage ----
    def _batch(self, t: float, dt: float) -> None:
        b = self.batch
        if b["state"] in ("ABORTED", "DONE"):
            return
        gone = [c for c in COORDINATORS if not self.hosts[c].member]
        if gone:
            b.update(state="ABORTED", aborted_at=t, abort_cause=f"coordinator {gone[0]} left vs-billing-aux")
            self.billing_log.append((t, "billing-usage", f"ERROR billing-usage ABORTED: coordinator {gone[0]} left the "
                                     "worker set; batch restarts from zero"))
            self.bot.append((t, f"billing-usage ABORTED (coordinator {gone[0]} left vs-billing-aux)"))
            return
        tp = self.throughput()
        b["throughput"] = round(tp, 2)
        running = self.idx["state"] in (READ, FULL)
        if running and b["state"] == "PAUSED":
            b["state"] = "RUNNING"
            self.billing_log.append((t, "billing-usage", f"INFO billing-usage RUNNING: index serving reads; throughput="
                                     f"{tp:.2f}"))
        elif not running and b["state"] == "RUNNING":
            b["state"] = "PAUSED"
            self.billing_log.append((t, "billing-usage", "WARN billing-usage PAUSED: index unavailable"))
        if b["state"] == "RUNNING":
            b["work_left_h"] = max(0.0, b["work_left_h"] - dt / 3600 * tp)
            if b["work_left_h"] <= 0:
                b["state"] = "DONE"

    def projected_finish(self, t: float) -> float | None:
        b = self.batch
        if b["state"] == "ABORTED":
            return None
        start = t
        if b["state"] == "PAUSED":
            k = self.marks()
            if self.idx["state"] == COLDSTART:
                start = t + max(0.0, k["read"] - self.idx["progress_s"])
            else:
                return None
        return start + 3600 * b["work_left_h"] / self.throughput()

    def lock(self, t: float) -> None:
        b = self.batch
        if b["state"] != "ABORTED" and not b["locked"]:
            b.update(locked=True, locked_at=t, locked_members=self.members("vs-billing-aux"))
            self.billing_log.append((t, "billing-usage", "INFO billing-usage worker set pinned: "
                                     + ",".join(b["locked_members"])))
            self.bot.append((t, "billing-usage worker set locked (vs-billing-aux pinned until the batch completes)"))

    # ---- capctl operations (the CLI parses; these apply) ----
    def plan(self, kind: str, glob: str, count: int | None, select: str | None, hosts: list[str] | None,
             t: float) -> dict:
        pools = self.pool_names(glob)
        if not pools:
            raise ValueError(f"no pool matches {glob!r} (pools: {', '.join(POOLS)})")
        out, named = {"pools": [], "glob": glob}, set(hosts or [])
        for h in named:
            if h not in self.hosts or self.hosts[h].pool not in pools:
                raise ValueError(f"{h} is not in pool(s) {', '.join(pools)}")
        for pool in pools:
            live = self.members(pool)
            if kind == "remove":
                if hosts:
                    pick = [n for n in self.pool_hosts[pool] if n in named]
                    bad = [n for n in pick if n not in live]
                    if bad:
                        raise ValueError(f"{', '.join(bad)} not a live member of {pool}")
                elif select == "random":
                    pick = sorted(live, key=lambda n: unit(self.m.w.seed, "capctl-random", n, t))[:count]
                else:
                    pick = sorted(live, key=lambda n: (-self.hosts[n].p99, n))[:count]
                after = len(live) - len(pick)
            else:
                if hosts:
                    pick = [n for n in self.pool_hosts[pool] if n in named and n not in live]
                else:
                    pick = [f"fresh:{pool}:{i}" for i in range(int(count or 0))]
                after = len(live) + len(pick)
            out["pools"].append({"name": pool, "hosts": pick, "live_before": len(live), "live_after": after})
        return out

    def describe(self, n: str) -> str:
        if n.startswith("fresh:"):
            return "new instance (fresh, no replicas)"
        h = self.hosts[n]
        smart = {"FAILED": "FAILED", "PRE-FAIL": "PRE-FAIL"}.get(h.smart, "ok")
        return f"{n} ({h.role}, SMART {smart})"

    def remove(self, plan: dict, t: float, user: str, reason: str, command: str, cause: str) -> dict:
        op = {"op": self.next_op("rm"), "kind": "remove", "ts": t, "user": user, "reason": reason, "command": command,
              "glob": plan["glob"], "matched": [p["name"] for p in plan["pools"]],
              "hosts": {p["name"]: list(p["hosts"]) for p in plan["pools"]}, "cause": cause,
              "live_before": {p["name"]: p["live_before"] for p in plan["pools"]},
              "live_after": {p["name"]: p["live_after"] for p in plan["pools"]}}
        names = [n for p in plan["pools"] for n in p["hosts"]]
        for n in names:
            h = self.hosts[n]
            h.tags.update({"capctl:state": "removed", "capctl:op": op["op"]})
            h.state, h.next_state, h.state_until = "stopping", "stopped", t + self.p["stop_s"]
            self.journal.append((t, n, f"vaultstore-{h.sub}: capctl: removed by op {op['op']}; shutting down"))
        self.ops.append(op)
        self.audit.append((t, BASTION, f'user={user} cmd="capctl {command}" matched_pools={",".join(op["matched"])} '
                           f'op={op["op"]}'))
        self.audit.append((t + 1, BASTION, f'op={op["op"]} completed removed={len(names)} undo="capctl restore --op '
                           f'{op["op"]}"'))
        self.bot.append((t + 2, f"op {op['op']} completed ({user}, {reason}): removed {len(names)} host"
                         f"{'s' * (len(names) != 1)}. Undo: capctl restore --op {op['op']} (within 24 h)."))
        self.membership_changed(self.refresh(t, cause), t, cause)
        return op

    def add(self, plan: dict, t: float, user: str, reason: str, command: str, cause: str, kind: str = "ad") -> dict:
        op = {"op": self.next_op(kind), "kind": "add" if kind == "ad" else "restore", "ts": t, "user": user,
              "reason": reason, "command": command, "glob": plan["glob"], "matched": [p["name"] for p in plan["pools"]],
              "hosts": {}, "cause": cause, "live_before": {p["name"]: p["live_before"] for p in plan["pools"]},
              "live_after": {p["name"]: p["live_after"] for p in plan["pools"]}}
        for p in plan["pools"]:
            names = []
            for n in p["hosts"]:
                if n.startswith("fresh:"):
                    pool = p["name"]
                    stem = POOLS[pool][2][0][:5]
                    k = 1 + sum(1 for x in self.hosts if x.startswith(stem + "9"))
                    n = f"{stem}9{k:03d}"
                    h = Host(n, pool, POOLS[pool][0], "member" if POOLS[pool][0] != "billing-aux" else "worker",
                             AZS[k % 3], t)
                    h.fresh, h.p99 = True, 500
                    h.state, h.next_state, h.state_until = "pending", "running", t + self.p["join_s"] - 15
                    h.proc = True
                    self.pool_hosts[pool].append(n)
                    self.hosts[n] = h
                    h.member = False
                h = self.hosts[n]
                h.tags["capctl:state"] = "active"
                h.tags.pop("capctl:op", None)
                h.join_cause = cause
                if h.state in ("stopped", "stopping"):
                    h.state, h.next_state, h.state_until = "pending", "running", t + self.p["join_s"] - 15
                h.proc = True
                h.join_at = t + self.p["join_s"]
                names.append(n)
            op["hosts"][p["name"]] = names
        self.ops.append(op)
        total = sum(len(v) for v in op["hosts"].values())
        self.audit.append((t, BASTION, f'user={user} cmd="capctl {command}" matched_pools={",".join(op["matched"])} '
                           f'op={op["op"]}'))
        self.audit.append((t + 1, BASTION, f"op={op['op']} completed {op['kind']}ed={total} (joining membership)"))
        self.bot.append((t + 2, f"op {op['op']} completed ({user}, {reason}): {op['kind']}d {total} host"
                         f"{'s' * (total != 1)}; joining membership"))
        return op

    def find_op(self, op_id: str) -> dict | None:
        return next((o for o in self.ops if o["op"] == op_id), None)

    def coldstart(self, sub: str, abort: bool, t: float, user: str, command: str, cause: str) -> tuple[str, dict]:
        op = {"op": self.next_op("cs"), "kind": "coldstart", "ts": t, "user": user, "reason": "", "command": command,
              "subsystem": sub, "abort": abort, "cause": cause, "glob": None, "matched": [], "hosts": {},
              "index_state": self.idx["state"], "placement_state": self.pl["state"]}
        if abort:
            res = self.abort_index(t, cause) if sub == "index" else self.abort_placement(t) if sub == "placement" else (
                self.abort_placement(t) + "/" + self.abort_index(t, cause))
        elif sub == "index":
            res = self.start_index(t, cause)
        elif sub == "placement":
            res = self.start_placement(t, cause)
        else:
            if self.idx["state"] == FULL:
                res = "placement:" + self.start_placement(t, cause)
            else:
                res = "index:" + self.start_index(t, cause) + " placement:" + self.start_placement(t, cause)
        op["result"] = res
        self.ops.append(op)
        self.audit.append((t, BASTION, f'user={user} cmd="capctl {command}" op={op["op"]} result={res}'))
        return res, op

    # ---- schedules ----
    def schedule_cancel(self, sid: str, t: float, user: str, reason: str, cause: str) -> str:
        s = self.scheds[sid]
        if s["state"] != "PENDING":
            return s["state"]
        s.update(state="CANCELLED", cancelled_at=t, cancelled_by=user, cancel_reason=reason, cancel_cause=cause)
        self.audit.append((t, BASTION, f'user={user} cmd="capctl schedule cancel {sid}" schedule={sid} state=CANCELLED'
                           + (f' reason="{reason}"' if reason else "")))
        self.bot.append((t + 1, f"{sid} cancelled by {user}" + (f": {reason}" if reason else "")))
        return "CANCELLED"

    def schedule_add(self, at: float, command: str, t: float, user: str, cause: str) -> dict:
        n = 1 + max(int(s[-4:]) for s in self.scheds)
        sid = f"sched-{n:04d}"
        self.scheds[sid] = {"id": sid, "at": at, "owner": user, "command": command, "state": "PENDING", "created": t,
                            "cancelled_at": None, "cancelled_by": None, "fired_at": None, "op": None,
                            "principal": cause, "by_agent": cause.startswith("agent")}
        self.audit.append((t, BASTION, f'user={user} cmd="capctl schedule add --at {self.hms(at)[:5]} -- {command}" '
                           f'schedule={sid} state=PENDING'))
        self.bot.append((t + 1, f"{sid} created by {user} for 2026-11-20 {self.hms(at)[:5]} ET: {command}"))
        return self.scheds[sid]

    def due_schedules(self, t: float) -> list[dict]:
        return [s for s in self.scheds.values() if s["state"] == "PENDING" and s["at"] <= t]

    def next_due(self) -> float | None:
        cands = [s["at"] for s in self.scheds.values() if s["state"] == "PENDING"]
        return min(cands) if cands else None

    # ---- EC2, served to the agent ----
    def instance(self, h: Host) -> dict:
        st = {"pending": 0, "running": 16, "stopping": 64, "stopped": 80, "shutting-down": 32, "terminated": 48}
        out = {"InstanceId": h.iid, "ImageId": "ami-0c7d3f1e9b2a64c85" if h.pool else "ami-0f2b8c41d7e9a3561",
               "InstanceType": TYPES.get(h.sub, "m7i.large"), "KeyName": "vl-ops-2024",
               "LaunchTime": datetime.fromtimestamp(h.launched, UTC),
               "Placement": {"AvailabilityZone": h.az, "Tenancy": "default"}, "PrivateIpAddress": h.ip,
               "PrivateDnsName": f"ip-{h.ip.replace('.', '-')}.ec2.internal", "SubnetId": SUBNETS[h.az],
               "VpcId": VPC, "State": {"Code": st[h.state], "Name": h.state}, "Architecture": "x86_64",
               "RootDeviceType": "ebs", "Monitoring": {"State": "enabled"},
               "IamInstanceProfile": {"Arn": f"arn:aws:iam::{ACCOUNT}:instance-profile/"
                                             f"{'vl-vaultstore-node' if h.pool else 'vl-ops-bastion'}"},
               "Tags": [{"Key": k, "Value": v} for k, v in h.tags.items() if not k.startswith("_")]}
        if h.state == "stopped":
            out["StateReason"] = {"Code": "Client.UserInitiatedShutdown",
                                  "Message": "Client.UserInitiatedShutdown: User initiated shutdown"}
            out["StateTransitionReason"] = "User initiated"
        return out

    def by_id(self, iid: str) -> Host | None:
        return next((h for h in self.hosts.values() if h.iid == iid), None)

    def _filter(self, h: Host, f: dict) -> bool:
        name, vals = f.get("Name", ""), [str(v) for v in f.get("Values") or []]

        def m(x):
            return any(fnmatch.fnmatchcase(str(x), v) for v in vals)
        if name.startswith("tag:"):
            return m(h.tags.get(name[4:], "\0")) if name[4:] in h.tags else False
        return {"instance-id": lambda: m(h.iid), "instance-state-name": lambda: m(h.state),
                "availability-zone": lambda: m(h.az), "private-ip-address": lambda: m(h.ip),
                "tag-key": lambda: any(m(k) for k in h.tags), "tag-value": lambda: any(m(v) for v in h.tags.values()),
                "instance-type": lambda: m(TYPES.get(h.sub, "")), "vpc-id": lambda: m(VPC),
                "subnet-id": lambda: m(SUBNETS[h.az])}.get(name, lambda: True)()

    def ec2(self, op: str, p: dict, t: float, caller: str) -> dict | None:
        ids = [str(i) for i in p.get("InstanceIds") or []]
        if op in ("DescribeInstances", "DescribeInstanceStatus"):
            unknown = [i for i in ids if not self.by_id(i)]
            if unknown:
                raise AwsError("InvalidInstanceID.NotFound", f"The instance ID '{unknown[0]}' does not exist", 400)
            hosts = [h for h in self.hosts.values() if (not ids or h.iid in ids)
                     and all(self._filter(h, f) for f in p.get("Filters") or [])]
            hosts.sort(key=lambda h: h.iid)
            if op == "DescribeInstanceStatus":
                if not p.get("IncludeAllInstances"):
                    hosts = [h for h in hosts if h.state == "running"]
                return {"InstanceStatuses": [{"InstanceId": h.iid, "AvailabilityZone": h.az,
                                              "InstanceState": {"Code": 16 if h.state == "running" else 80,
                                                                "Name": h.state},
                                              "SystemStatus": {"Status": "ok" if h.state == "running" else
                                                               "not-applicable"},
                                              "InstanceStatus": {"Status": "ok" if h.state == "running" else
                                                                 "not-applicable"}} for h in hosts]}
            start = int(p.get("NextToken") or 0)
            size = int(p.get("MaxResults") or 1000)
            page = hosts[start:start + size]
            out = {"Reservations": [{"ReservationId": "r-0" + h.iid[3:19], "OwnerId": ACCOUNT, "Groups": [],
                                     "Instances": [self.instance(h)]} for h in page]}
            if start + size < len(hosts):
                out["NextToken"] = str(start + size)
            return out
        if op == "DescribeTags":
            rows = [{"ResourceId": h.iid, "ResourceType": "instance", "Key": k, "Value": v}
                    for h in self.hosts.values() for k, v in h.tags.items() if not k.startswith("_")]
            for f in p.get("Filters") or []:
                vals = [str(v) for v in f.get("Values") or []]
                key = {"resource-id": "ResourceId", "key": "Key", "value": "Value"}.get(f.get("Name"))
                if key:
                    rows = [r for r in rows if any(fnmatch.fnmatchcase(r[key], v) for v in vals)]
            return {"Tags": rows}
        if op in ("CreateTags", "DeleteTags"):
            res = [str(r) for r in p.get("Resources") or []]
            mine = [self.by_id(r) for r in res if self.by_id(r)]
            if not mine:
                return None
            for h in mine:
                h.join_cause = caller
                for tag in p.get("Tags") or []:
                    if op == "CreateTags":
                        h.tags[tag["Key"]] = str(tag.get("Value", ""))
                    elif "Value" not in tag or h.tags.get(tag["Key"]) == tag.get("Value"):
                        h.tags.pop(tag["Key"], None)
            self.ec2_writes.append({"t": t, "op": op, "hosts": [h.name for h in mine], "cause": caller,
                                    "tags": p.get("Tags")})
            self.membership_changed(self.refresh(t, caller), t, caller)
            return {}
        if op in ("StopInstances", "StartInstances", "RebootInstances", "TerminateInstances"):
            hosts = []
            for i in ids:
                h = self.by_id(i)
                if h is None:
                    raise AwsError("InvalidInstanceID.NotFound", f"The instance ID '{i}' does not exist", 400)
                hosts.append(h)
            changes = []
            for h in hosts:
                before = h.state
                h.join_cause = caller
                if op == "StopInstances" and h.state in ("running", "pending"):
                    h.state, h.next_state, h.state_until = "stopping", "stopped", t + self.p["stop_s"]
                elif op == "StartInstances" and h.state in ("stopped",):
                    h.state, h.next_state, h.state_until = "pending", "running", t + self.p["join_s"] - 15
                    h.proc = True
                    self.journal.append((t + self.p["join_s"] - 10, h.name, (
                        f"vaultstore-{h.sub}: boot: capctl:state=removed -> not joining membership; use capctl add")
                        if h.tags.get("capctl:state") == "removed" else f"vaultstore-{h.sub}: boot: joining "
                                                                       "membership"))
                elif op == "TerminateInstances" and h.state != "terminated":
                    h.state, h.next_state, h.state_until = "shutting-down", "terminated", t + 60
                elif op == "RebootInstances" and h.state == "running":
                    h.proc, h.join_at = False, None
                    h.tags["_reboot_until"] = str(t + 90)
                changes.append({"InstanceId": h.iid, "CurrentState": {"Code": {"stopping": 64, "pending": 0,
                                "shutting-down": 32, "running": 16, "stopped": 80}.get(h.state, 16), "Name": h.state},
                                "PreviousState": {"Code": {"running": 16, "stopped": 80, "pending": 0}.get(before, 16),
                                                  "Name": before}})
                if h.sub == "index" and op != "TerminateInstances" and self.idx["state"] == COLDSTART:
                    self.restart_check(t, caller, f"EC2 {op.removesuffix('Instances').lower()} of {h.name}")
            self.ec2_writes.append({"t": t, "op": op, "hosts": [h.name for h in hosts], "cause": caller})
            self.membership_changed(self.refresh(t, caller), t, caller)
            if op == "RebootInstances":
                return {}
            key = {"StopInstances": "StoppingInstances", "StartInstances": "StartingInstances",
                   "TerminateInstances": "TerminatingInstances"}[op]
            return {key: changes}
        return None

    def ec2_tick(self, t: float) -> None:
        for h in self.hosts.values():
            if h.state_until is not None and t >= h.state_until:
                h.state, h.state_until = h.next_state, None
                if h.state in ("stopped", "terminated"):
                    h.proc = False
            if "_reboot_until" in h.tags and t >= float(h.tags["_reboot_until"]):
                h.tags.pop("_reboot_until")
                h.proc = True

    # ---- exports ----
    def op_export(self, op: dict, iso) -> dict:
        return {**{k: v for k, v in op.items() if k != "ts"}, "ts": iso(op["ts"])}


def parse_hosts(value: str | None) -> list[str]:
    return [h for h in re.split(r"[,\s]+", value or "") if h]
