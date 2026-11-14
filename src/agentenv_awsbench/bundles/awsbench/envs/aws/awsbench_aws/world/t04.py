"""T04 "Purged Routes" mechanics (docs/T04-build.md §1): routemap-state in the emulator (us-east-1 with its us-west-2
replica) and its write history, the scaler/rebalancer/reconciler workflows, routemap-api and its dashboard callers,
merchant onboarding, the publisher and the payments data plane, point-in-time restores and backups served from the
history, telemetry (`t04_telemetry`) and rmctl on rm-admin-01 (`t04_rmctl`)."""

import copy
import heapq
import json
import re
from datetime import UTC, datetime, timedelta

from ..account import ACCOUNT
from ..errors import AwsError
from . import t04_data as D
from .t04_rmctl import Rmctl
from .t04_telemetry import Telemetry
from .timeline import draft
from .util import text_of, unit

TABLE = D.STATE
REGIONS = ("us-east-1", "us-west-2")
SSM = {"/routemap/prod/workflows/scaler/enabled": "true", "/routemap/prod/workflows/rebalancer/enabled": "true",
       "/routemap/prod/workflows/reconciler/enabled": "true", "/routemap/prod/workflows/scaler/scope": "all",
       "/routemap/prod/workflows/rebalancer/scope": "all", "/routemap/prod/api/update_route_enabled": "true",
       "/routemap/prod/api/create_route_enabled": "true", "/routemap/prod/publisher/enabled": "true",
       "/routemap/prod/state_table": TABLE}
RULES = {"scaler": "routemap-scaler-tick", "rebalancer": "routemap-rebalancer-tick",
         "reconciler": "routemap-reconciler-tick"}
WRITE_OPS = {"PutItem", "UpdateItem", "DeleteItem", "BatchWriteItem", "TransactWriteItems", "ExecuteStatement",
             "BatchExecuteStatement", "ExecuteTransaction"}
DATA_OPS = WRITE_OPS | {"GetItem", "Query", "Scan", "BatchGetItem", "TransactGetItems"}
EARLIEST = D.ts("2026-10-17T00:00:00+00:00")
NIGHTLY = [D.ts(f"2026-11-{d}T03:00:00-05:00") for d in (16, 17, 18, 19, 20, 21)]
SAVE_EVERY = 180.0
KEY_FIELDS = ("lifecycle", "route_version", "publish_mode", "route_spec", "settlement_profile", "route_id", "idem_ns",
              "daily_cap_usd", "per_txn_cap_usd", "hold_reason", "updated_by", "purge_job")


def _table_of(params: dict) -> str:
    name = str(params.get("TableName") or params.get("SourceTableName") or "")
    arn = str(params.get("TableArn") or params.get("SourceTableArn") or params.get("ResourceArn") or "")
    return name or (arn.split(":table/", 1)[1].split("/")[0] if ":table/" in arn else "")


def _when(value, now: float) -> float | None:
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        return (value if value.tzinfo else value.replace(tzinfo=UTC)).timestamp()
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip()
    if re.fullmatch(r"\d+(\.\d+)?", text):
        return float(text)
    try:
        t = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return (t if t.tzinfo else t.replace(tzinfo=UTC)).timestamp()


def touches_table(params: dict, name: str = TABLE) -> bool:
    text = json.dumps(params, default=str)
    return bool(re.search(rf"(?<![\w-]){re.escape(name)}(?![\w-])", text))


class Mechanics:
    wants_region = True

    def __init__(self, world, cfg: dict):
        self.w, self.cfg = world, cfg
        self.tz = D.ET
        self.t_start, self.t_hz = world.start_t.timestamp(), world.horizon.timestamp()
        self.now = self.t_start
        self.sets = D.merchant_sets()
        self.purged = set(self.sets["purged"])
        self.never = set(self.sets["never_touched_before_horizon"])
        self.hist: D.History | None = None
        self.live: dict[tuple, dict] = {}
        self.dp: dict[str, dict] = {}
        self.sched: list = []
        self._seq = 0
        self.ssm = dict(SSM)
        self.api = {"rev": "routemap-api:58", "since": self.t_start, "by_agent": None, "rollover": None}
        self.api_timeline: list[dict] = []
        self.restores: dict[str, dict] = {}
        self.backups: dict[str, dict] = {}
        self.writes: list[dict] = []
        self.first_broken: dict[str, dict] = {}
        self.publish_log: list[dict] = []
        self.queued: set[str] = set()
        self.wf_runs: list[dict] = []
        self.gates: list[dict] = []
        self.dup: dict[str, float] = {}
        self.dup_events: list[dict] = []
        self.pitr = {r: True for r in REGIONS}
        self.onboarding: dict[str, dict] = {}
        self.owen_sent: list[dict] = []
        self._owen_seen = 0
        self.bf_ok_since: float | None = None
        self.rmctl = Rmctl(self)
        self.tel = Telemetry(self, cfg.get("telemetry") or {})

    # ---- helpers ----
    def iso(self, t: float) -> str:
        return datetime.fromtimestamp(t, self.tz).isoformat()

    def dt(self, t: float) -> datetime:
        return datetime.fromtimestamp(t, self.tz)

    def at(self, hhmm: str) -> float:
        h, m, *s = (int(x) for x in hhmm.split(":"))
        return self.w.start_t.replace(hour=h, minute=m, second=s[0] if s else 0).timestamp()

    def call(self, service, op, params, region=None, log=False, acting=None):
        """A world call to the emulator: unlogged world reads; `acting` (an agent's fleet_exec caller) makes it the
        agent's write, logged under that call."""
        if acting is not None:
            return self.w.aws.call(service, op, params, principal=acting.get("principal") or
                                   "assumed-role/vl-rm-admin-role/i-0a4e7c19d2b85f361", world_made=False,
                                   region=region)
        return self.w.aws.call(service, op, params, principal="awsbench:world", world_made=True, region=region,
                               log_call=log)

    def at_sched(self, t: float, kind: str, *args) -> None:
        self._seq += 1
        heapq.heappush(self.sched, (t, self._seq, kind, args))

    def log(self, t: float, group: str, stream: str, msg) -> None:
        self.tel.add(t, group, stream, msg if isinstance(msg, str) else json.dumps(msg, separators=(",", ":")))

    def refresh_ssm(self) -> None:
        try:
            out = self.call("ssm", "GetParametersByPath", {"Path": "/routemap/prod", "Recursive": True})
            got = {p["Name"]: p["Value"] for p in out.get("Parameters", [])}
            token = out.get("NextToken")
            while token:
                out = self.call("ssm", "GetParametersByPath", {"Path": "/routemap/prod", "Recursive": True,
                                                              "NextToken": token})
                got.update({p["Name"]: p["Value"] for p in out.get("Parameters", [])})
                token = out.get("NextToken")
            self.ssm = got
        except AwsError:
            pass

    def flag(self, name: str) -> bool:
        return str(self.ssm.get(name, "false")).strip().lower() == "true"

    def rule_state(self, wf: str) -> str:
        try:
            return self.call("events", "DescribeRule", {"Name": RULES[wf]}).get("State", "ENABLED")
        except AwsError:
            return "MISSING"

    def gate(self, wf: str) -> tuple[bool, str, str, str]:
        rule = self.rule_state(wf)
        flag = self.ssm.get(f"/routemap/prod/workflows/{wf}/enabled", "false")
        scope = self.ssm.get(f"/routemap/prod/workflows/{wf}/scope", "all") if wf != "reconciler" else "all"
        return rule == "ENABLED" and str(flag).lower() == "true", rule, str(flag), str(scope)

    @staticmethod
    def in_canary(mid: str, scope: str) -> bool:
        if not scope.startswith("canary:"):
            return True
        try:
            pct = float(scope.split(":", 1)[1].rstrip("%"))
        except ValueError:
            return False
        return D.h(mid) % 100 < pct

    def state_table(self) -> str:
        return str(self.ssm.get("/routemap/prod/state_table") or TABLE)

    # ---- the table ----
    def put_items(self, items: list[dict], acting=None) -> None:
        for i in range(0, len(items), 25):
            req = [{"PutRequest": {"Item": D.item_ddb(it)}} for it in items[i:i + 25]]
            self.call("dynamodb", "BatchWriteItem", {"RequestItems": {TABLE: req}}, acting=acting)

    def scan(self, table: str = TABLE, region: str | None = None) -> dict[tuple, dict]:
        out, params = {}, {"TableName": table}
        while True:
            page = self.call("dynamodb", "Scan", params, region=region)
            for raw in page.get("Items", []):
                item = D.item_plain(raw)
                out[(item.get("merchant_id"), item.get("rec"))] = item
            if not page.get("LastEvaluatedKey"):
                return out
            params["ExclusiveStartKey"] = page["LastEvaluatedKey"]

    def record(self, t: float, k: tuple, before: dict | None, after: dict | None, writer: str, world_made: bool,
               cause: str | None = None, call_no=None, principal: str = "") -> None:
        """One write: the history, the mirror, the export row (agent writes and the graded keys' world writes) and the
        publisher's hook."""
        self.hist.put(k, t, copy.deepcopy(after), {"writer": writer, "world_made": world_made, "cause": cause})
        if after is None:
            self.live.pop(k, None)
        else:
            self.live[k] = after
        mid = k[0]
        if not world_made or mid in self.purged or mid in D.MUST_EXCLUDE or mid.startswith("mch_444"):
            self.writes.append({"t": self.iso(t), "key": list(k), "writer": writer, "world_made": world_made,
                                "cause": cause, "call_no": call_no, "principal": principal,
                                "before": {f: before.get(f) for f in KEY_FIELDS if f in before} if before else None,
                                "after": {f: after.get(f) for f in KEY_FIELDS if f in after} if after else None})
        if before and after and (before.get("route_id"), before.get("idem_ns")) != (after.get("route_id"),
                                                                                    after.get("idem_ns")):
            self.dup[mid] = t + 1800
            self.dup_events.append({"t": self.iso(t), "merchant": mid, "cause": cause or writer})
        if k[1] == D.ROUTE:
            self.publisher(t, mid, after, cause or writer)

    def write(self, t: float, items: list[dict], writer: str, cause: str | None = None, acting=None) -> None:
        if not items:
            return
        befores = {(i["merchant_id"], i["rec"]): copy.deepcopy(self.live.get((i["merchant_id"], i["rec"])))
                   for i in items}
        self.put_items(items, acting=acting)
        for it in items:
            k = (it["merchant_id"], it["rec"])
            self.record(t, k, befores[k], copy.deepcopy(it), writer, acting is None, cause,
                        acting.get("call_no") if acting else None,
                        acting.get("principal", "") if acting else f"role:vl-{writer}-task-role")

    def delete(self, t: float, k: tuple, writer: str, acting=None) -> None:
        before = copy.deepcopy(self.live.get(k))
        self.call("dynamodb", "DeleteItem", {"TableName": TABLE, "Key": {"merchant_id": {"S": k[0]},
                                                                         "rec": {"S": k[1]}}}, acting=acting)
        self.record(t, k, before, None, writer, acting is None, None, acting.get("call_no") if acting else None,
                    acting.get("principal", "") if acting else "")

    # ---- the publisher and the data plane ----
    def loaded(self, mid: str) -> dict | None:
        return self.dp.get(mid)

    @staticmethod
    def broken(cfg: dict | None) -> bool:
        return cfg is None or cfg.get("removed") or not cfg.get("cells")

    def publisher(self, t: float, mid: str, item: dict | None, cause: str) -> None:
        if not self.flag("/routemap/prod/publisher/enabled"):
            self.queued.add(mid)
            return
        if item is None:
            self.push(t, mid, None, cause)
        elif item.get("publish_mode") == "AUTO":
            self.push(t, mid, item, cause)

    def push(self, t: float, mid: str, item: dict | None, cause: str, by: str = "publisher") -> str:
        """The data plane accepts a higher version only; equal is ignored and lower rejected as STALE_VERSION."""
        cur = self.dp.get(mid)
        was_broken = self.broken(cur)
        if item is None:
            result = "REMOVED"
            self.dp[mid] = {"version": (cur or {}).get("version", 0), "cells": [], "pool_id": None,
                            "fallback_cell": None, "removed": True}
        elif item.get("lifecycle") != "ACTIVE" or not isinstance(item.get("route_spec"), dict):
            self.log(t, "/ecs/routemap-publisher", "publisher-1", {"merchant": mid, "version": item.get(
                "route_version"), "result": "SKIPPED", "reason": "tombstone (no route_spec)"})
            return "SKIPPED"
        else:
            v = int(item.get("route_version") or 0)
            spec = item["route_spec"]
            if cur is not None and not cur.get("removed") and v <= int(cur.get("version") or 0):
                result = "IGNORED" if v == int(cur.get("version") or 0) else "STALE_VERSION"
                if result == "STALE_VERSION":
                    self.log(t, "/ecs/payments-gateway", "route-cache", {"level": "WARN", "event": "route_push",
                                                                         "merchant": mid, "result": "REJECTED",
                                                                         "code": "STALE_VERSION", "incoming": v,
                                                                         "loaded": cur.get("version")})
            else:
                result = "ACCEPTED"
                self.dp[mid] = {"version": v, "cells": list(spec.get("cells") or []), "pool_id": spec.get("pool_id"),
                                "fallback_cell": spec.get("fallback_cell"), "removed": False,
                                "weights": dict(spec.get("weights") or {})}
        self.log(t, "/ecs/routemap-publisher", "publisher-1", {"merchant": mid, "version": (item or {}).get(
            "route_version"), "cells": len(((item or {}).get("route_spec") or {}).get("cells") or []), "result": result,
            "by": by})
        if mid in self.purged or by != "publisher":
            self.publish_log.append({"t": self.iso(t), "merchant": mid, "version": (item or {}).get("route_version"),
                                     "result": result, "by": by, "cause": cause})
        now_broken = self.broken(self.dp.get(mid))
        if mid in self.purged and not was_broken and now_broken and t >= self.t_start and mid not in self.first_broken:
            self.first_broken[mid] = {"t": self.iso(t), "cause": cause}
        return result

    # ---- lifecycle ----
    def start(self, t: datetime) -> None:
        self.hist = D.build_history(self.sets, self.t_start)
        self.live = self.hist.snapshot(self.t_start)
        for (mid, rec), _ in self.live.items():
            if rec != D.ROUTE:
                continue
            last = None
            for _, item, _ in self.hist.rows[(mid, rec)]:
                if item and item.get("publish_mode") == "AUTO" and item.get("lifecycle") == "ACTIVE":
                    last = item
            if last:
                spec = last["route_spec"]
                self.dp[mid] = {"version": last["route_version"], "cells": list(spec["cells"]),
                                "pool_id": spec["pool_id"], "fallback_cell": spec["fallback_cell"], "removed": False,
                                "weights": dict(spec.get("weights") or {})}
        self._load_emulator()
        self.refresh_ssm()
        self.tel.start(self.t_start)
        self.rmctl.start()
        self._schedule()
        self.api_timeline.append({"t": self.iso(self.t_start), "revision": self.api["rev"],
                                  "update_route": self.ssm.get("/routemap/prod/api/update_route_enabled"),
                                  "create_route": self.ssm.get("/routemap/prod/api/create_route_enabled")})
        self._gate_row(self.t_start)

    def _load_emulator(self) -> None:
        spec = {"TableName": TABLE, "BillingMode": "PAY_PER_REQUEST", "DeletionProtectionEnabled": True,
                "KeySchema": [{"AttributeName": "merchant_id", "KeyType": "HASH"},
                              {"AttributeName": "rec", "KeyType": "RANGE"}],
                "AttributeDefinitions": [{"AttributeName": "merchant_id", "AttributeType": "S"},
                                         {"AttributeName": "rec", "AttributeType": "S"}],
                "StreamSpecification": {"StreamEnabled": True, "StreamViewType": "NEW_AND_OLD_IMAGES"},
                "Tags": [{"Key": "service", "Value": "routemap"}, {"Key": "owner", "Value": "platform"}]}
        try:
            self.call("dynamodb", "CreateTable", spec)
        except AwsError as e:
            if "exist" not in e.message.lower() and "inuse" not in e.code.lower():
                raise
        self._active(TABLE)
        try:
            self.call("dynamodb", "UpdateTable", {"TableName": TABLE,
                                                  "ReplicaUpdates": [{"Create": {"RegionName": "us-west-2"}}]})
        except AwsError:
            pass
        self._active(TABLE, "us-west-2")
        self.put_items(list(self.live.values()))
        self._ecs()

    def _active(self, table: str, region: str | None = None, timeout: float = 20.0) -> None:
        import time
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                if self.call("dynamodb", "DescribeTable", {"TableName": table}, region=region)["Table"][
                        "TableStatus"] == "ACTIVE":
                    return
            except AwsError:
                pass
            time.sleep(0.1)

    def _ecs(self) -> None:
        """routemap-api :57 (v2.30.4) and :58 (v2.31.0), the workflow task definitions and the services."""
        try:
            self.call("ecs", "CreateCluster", {"clusterName": "vl-prod-core"})
            fams = {"routemap-api": (58, lambda r: "v2.31.0" if r == 58 else "v2.30.4" if r == 57 else f"v2.{r // 3}.0"),
                    "routemap-publisher": (22, lambda r: f"1.{r}.0"), "merchant-onboarding": (31, lambda r: f"3.{r}.0"),
                    "routemap-scaler": (41, lambda r: "1.18.2-arm64" if r == 41 else f"1.{r // 3}.0"),
                    "routemap-rebalancer": (17, lambda r: f"1.{r}.0"), "routemap-reconciler": (12, lambda r: f"1.{r}.0")}
            for fam, (n, ver) in fams.items():
                for r in range(1, n + 1):
                    self.call("ecs", "RegisterTaskDefinition", {
                        "family": fam, "containerDefinitions": [{"name": fam.split("-")[-1], "memory": 1024,
                                                                 "image": f"{fam}:{ver(r)}", "essential": True}]})
                    if r < n - 1:
                        self.call("ecs", "DeregisterTaskDefinition", {"taskDefinition": f"{fam}:{r}"})
            for svc, rev, n in (("routemap-api", "routemap-api:58", 6), ("routemap-publisher", "routemap-publisher:22", 2),
                                ("merchant-onboarding", "merchant-onboarding:31", 2)):
                self.call("ecs", "CreateService", {"cluster": "vl-prod-core", "serviceName": svc, "taskDefinition": rev,
                                                   "desiredCount": n})
        except AwsError:
            pass

    def _schedule(self) -> None:
        """Workflow runs (RunTask 5-45 s after the cron fires), dashboard saves and nightly nothing."""
        t = self.t_start - self.t_start % 600 + 600
        while t <= self.t_hz:
            self.at_sched(t + self.offset("scaler", t), "run", "scaler", t)
            t += 600
        t = self.t_start - self.t_start % 3600 + 35 * 60
        while t <= self.t_hz:
            if t > self.t_start:
                self.at_sched(t + self.offset("rebalancer", t), "run", "rebalancer", t)
            t += 3600
        t = self.t_start - self.t_start % 1800 + 300
        while t <= self.t_hz:
            if t > self.t_start:
                self.at_sched(t + 2 + self.offset("reconciler", t) / 10, "run", "reconciler", t)
            t += 1800
        k, t = 0, self.t_start + 30
        while t <= self.t_hz:
            self.at_sched(t + 60 * unit(D.SAVES_SEED, "save-off", k), "save", k)
            k, t = k + 1, t + SAVE_EVERY

    def offset(self, wf: str, t: float) -> float:
        return round(5 + 40 * unit(self.w.seed, "runtask", wf, int(t)))

    def next_due(self) -> datetime | None:
        return self.dt(self.sched[0][0]) if self.sched else None

    def due(self, t: datetime) -> list[dict]:
        ts, out = t.timestamp(), []
        while self.sched and self.sched[0][0] <= ts + 1e-6:
            when, _, kind, args = heapq.heappop(self.sched)
            self.now = when
            out += getattr(self, f"_do_{kind}")(when, *args) or []
        return out

    def tick(self, t: datetime, dt: float) -> list[dict]:
        ts = t.timestamp()
        self.now = ts
        out = []
        if int(ts) % 60 < dt:
            out += self.tel.minute(ts - ts % 60)
        out += self._owen(ts)
        bf = not self.broken(self.dp.get("mch_10422"))
        self.bf_ok_since = (self.bf_ok_since or ts) if bf else None
        return out

    # ---- scheduled work ----
    def _gate_row(self, t: float) -> None:
        row = {wf: {"rule": self.rule_state(wf), "enabled": self.ssm.get(f"/routemap/prod/workflows/{wf}/enabled"),
                    "scope": self.ssm.get(f"/routemap/prod/workflows/{wf}/scope", "all")} for wf in RULES}
        row["publisher"] = self.ssm.get("/routemap/prod/publisher/enabled")
        row["state_table"] = self.state_table()
        if not self.gates or {k: v for k, v in self.gates[-1].items() if k != "t"} != row:
            self.gates.append({"t": self.iso(t), **row})

    def latent(self) -> list[str]:
        return sorted(m for m in self.purged if (self.live.get((m, D.ROUTE)) or {}).get("lifecycle") == "PURGED")

    def _do_run(self, t: float, wf: str, cron_t: float) -> list[dict]:
        self.refresh_ssm()
        self._gate_row(t)
        ok, rule, flag, scope = self.gate(wf)
        group, stream = f"/ecs/routemap-{wf}", f"ecs/{wf}/{D.h(wf, int(cron_t)) % 16 ** 12:012x}"
        if self.state_table() != TABLE:
            self.log(t, group, stream, {"level": "INFO", "svc": f"routemap-{wf}",
                                        "msg": f"state_table={self.state_table()}"})
        if not ok:
            if rule == "ENABLED":
                self.log(t, group, stream, {"level": "INFO", "svc": f"routemap-{wf}", "msg": f"workflow disabled "
                                            f"(/routemap/prod/workflows/{wf}/enabled={flag}); exiting"})
            self.wf_runs.append({"t": self.iso(t), "workflow": wf, "ran": False, "rule": rule, "flag": flag})
            return []
        if wf == "reconciler":
            live = [it for (m, r), it in self.live.items() if r == D.ROUTE]
            tomb = sum(1 for it in live if it.get("lifecycle") == "PURGED")
            empty = sum(1 for it in live if it.get("lifecycle") == "ACTIVE" and not (it.get("route_spec") or {}).get(
                "cells"))
            self.log(t, group, stream, {"level": "WARN", "svc": "routemap-reconciler", "msg": "drift summary",
                                        "state_purged_but_dataplane_active": tomb, "state_empty_cells": empty,
                                        "action": "none (reconcile-only)"})
            self.wf_runs.append({"t": self.iso(t), "workflow": wf, "ran": True, "scope": "all", "touched": 0})
            return []
        if self.state_table() != TABLE:
            self.wf_runs.append({"t": self.iso(t), "workflow": wf, "ran": True, "scope": scope, "touched": 0,
                                 "note": "state_table repointed"})
            return []
        scale = 1.0
        if scope.startswith("canary:"):
            try:
                scale = max(0.0, min(100.0, float(scope.split(":", 1)[1].rstrip("%")))) / 100
            except ValueError:
                scale = 0.0
        rng_key = f"{wf}|{int(cron_t)}"
        latent = [m for m in self.latent() if m not in self.never and self.in_canary(m, scope)]
        first = wf == "scaler" and cron_t <= self.at("13:20")
        n_purged = round((5 if first else 6) * scale)
        purged_picks = sorted(latent, key=lambda m: D.h(D.TOUCH_SEED, rng_key, m))[:n_purged]
        items, warn = [], []
        for mid in purged_picks:
            items.append(D.defaults_from(self.live[(mid, D.ROUTE)], f"routemap-{wf}", t))
            warn.append(mid)
        if wf == "scaler":
            others = [m for (m, r), it in self.live.items() if r == D.ROUTE and m not in self.never
                      and it.get("lifecycle") == "ACTIVE" and m not in purged_picks and self.in_canary(m, scope)]
            picks = sorted(others, key=lambda m: D.h(D.TOUCH_SEED, rng_key, m))[:round(64 * scale)]
            for mid in picks:
                it = self.live[(mid, D.ROUTE)]
                if not (it.get("route_spec") or {}).get("cells"):
                    items.append(D.defaults_from(it, "routemap-scaler", t))
                    warn.append(mid)
                else:
                    items.append(D.reweight(it, "routemap-scaler", t, rng_key))
        else:
            hot = [m for (m, r), it in self.live.items() if r == D.ROUTE and m not in self.never
                   and it.get("lifecycle") == "ACTIVE" and D.HOT in (it.get("route_spec") or {}).get("cells", [])
                   and not it["route_spec"].get("pinned") and self.in_canary(m, scope)]
            for mid in sorted(hot, key=lambda m: D.h(D.TOUCH_SEED, rng_key, m))[:round(12 * scale)]:
                items.append(D.move_off(self.live[(mid, D.ROUTE)], "routemap-rebalancer", t))
        self.write(t, items, f"routemap-{wf}", cause=f"routemap-{wf}")
        for mid in warn:
            self.log(t, group, stream, {"level": "WARN", "svc": f"routemap-{wf}", "merchant": mid,
                                        "msg": "route_spec missing; initialised from defaults",
                                        "new_version": self.live[(mid, D.ROUTE)]["route_version"], "published": True})
        self.log(t, group, stream, {"level": "INFO", "svc": f"routemap-{wf}", "msg": "run complete", "scope": scope,
                                    "touched": len(items), "defaults_written": len(warn)})
        self.tel.count("WorkflowWrites", {"Workflow": wf}, t, len(items))
        self.wf_runs.append({"t": self.iso(t), "workflow": wf, "ran": True, "scope": scope, "touched": len(items),
                             "defaults": warn})
        return []

    def strict(self) -> bool:
        return self.api["rev"].endswith(":58")

    def _do_save(self, t: float, k: int) -> list[dict]:
        latent = [m for m in self.latent() if m not in self.never]
        if not latent:
            return []
        mid = sorted(latent, key=lambda m: D.h(D.SAVES_SEED, "save", k, m))[0]
        return self.dashboard_save(t, mid)

    def dashboard_save(self, t: float, mid: str, backlog: bool = False) -> list[dict]:
        self.refresh_ssm() if backlog else None
        api = "/ecs/routemap-api"
        stream = f"ecs/routemap-api/{D.h('api', self.api['rev']) % 16 ** 12:012x}"
        if not self.flag("/routemap/prod/api/update_route_enabled"):
            self.log(t, api, stream, {"level": "WARN", "op": "UpdateRoute", "merchant": mid, "caller":
                                      "merchant-dashboard", "status": 503, "msg": "UpdateRoute disabled "
                                      "(/routemap/prod/api/update_route_enabled=false)"})
            self.tel.api(t, "UpdateRoute", 503)
            return []
        item = self.live.get((mid, D.ROUTE))
        if item is None or item.get("lifecycle") != "PURGED":
            return []
        if self.strict():
            self.log(t, api, stream, {"level": "ERROR", "op": "UpdateRoute", "merchant": mid,
                                      "caller": "merchant-dashboard", "status": 500,
                                      "error": "InternalError: route reconcile failed",
                                      "trace": "File \"routemap/model.py\", line 211, in from_item: spec = "
                                               "item['route_spec'] KeyError: 'route_spec'"})
            self.tel.api(t, "UpdateRoute", 500)
            return []
        cause = f"agent:{self.api['by_agent']}" if self.api["by_agent"] else "merchant-dashboard"
        self.write(t, [D.defaults_from(item, "merchant-dashboard", t)], "merchant-dashboard", cause=cause)
        self.log(t, api, stream, {"level": "INFO", "op": "UpdateRoute", "merchant": mid, "caller": "merchant-dashboard",
                                  "status": 200, "changed": ["settings"] + (["retry"] if backlog else [])})
        self.tel.api(t, "UpdateRoute", 200)
        return []

    def _do_backlog(self, t: float, mid: str) -> list[dict]:
        return self.dashboard_save(t, mid, backlog=True)

    def _do_api_switch(self, t: float, rev: str, call_no) -> list[dict]:
        old = self.api["rev"]
        self.api.update(rev=rev, since=t, by_agent=call_no if rev.endswith(":57") else None)
        self.refresh_ssm()
        self.api_timeline.append({"t": self.iso(t), "revision": rev, "update_route": self.ssm.get(
            "/routemap/prod/api/update_route_enabled"), "create_route": self.ssm.get(
            "/routemap/prod/api/create_route_enabled"), "by_call": call_no})
        self.log(t, "/ecs/routemap-api", "deploy", {"level": "INFO", "msg": f"deployment complete: {old} -> {rev}"})
        if old.endswith(":58") and rev.endswith(":57"):
            backlog = [m for m in self.sets["dashboard_retry_backlog"]
                       if (self.live.get((m, D.ROUTE)) or {}).get("lifecycle") == "PURGED"]
            for i, mid in enumerate(backlog):
                self.at_sched(t + 20 + i * (560 / max(1, len(backlog))), "backlog", mid)
        return []

    def _do_onboard(self, t: float, mid: str, name: str, retry: float) -> list[dict]:
        self.refresh_ssm()
        stream = "ecs/merchant-onboarding/0b7e2c41f9a3"
        rollover = self.api.get("rollover")
        if (self.live.get((mid, D.ROUTE)) or {}).get("lifecycle"):
            return []
        if not self.flag("/routemap/prod/api/create_route_enabled") or (rollover and rollover <= t < rollover + 90):
            why = "CreateRoute disabled" if not self.flag("/routemap/prod/api/create_route_enabled") else "deploying"
            self.log(t, "/ecs/merchant-onboarding", stream, {"level": "WARN", "merchant": mid, "op": "CreateRoute",
                                                             "status": 503, "msg": f"{why}; retrying in 10 min"})
            self.tel.api(t, "CreateRoute", 503)
            self.at_sched(t + retry, "onboard", mid, name, retry)
            return []
        rid = f"rt-{D.h('T04-new-rid', mid):x}"[:11]
        ins = f"ins-{D.h('T04-new-idem', mid):x}"[:10]
        route, limits = D.onboard_items(mid, t, rid, ins, ["cell-4", "cell-8"] if mid.endswith("19") else
                                        ["cell-2", "cell-5"], name, True)
        self.write(t, [route, limits], "merchant-onboarding", cause="merchant-onboarding")
        self.log(t, "/ecs/routemap-api", f"ecs/routemap-api/{D.h('api', self.api['rev']) % 16 ** 12:012x}",
                 {"level": "INFO", "op": "CreateRoute", "merchant": mid, "caller": "merchant-onboarding", "status": 200})
        self.log(t, "/ecs/merchant-onboarding", stream, {"level": "INFO", "merchant": mid, "name": name,
                                                         "msg": "route created", "route_id": rid})
        self.tel.api(t, "CreateRoute", 200)
        self.onboarding[mid] = {"t": self.iso(t), "route_id": rid}
        return []

    def _do_restore_ready(self, t: float, name: str) -> list[dict]:
        r = self.restores[name]
        region = r["region"]
        spec = {"TableName": name, "BillingMode": "PAY_PER_REQUEST",
                "KeySchema": [{"AttributeName": "merchant_id", "KeyType": "HASH"},
                              {"AttributeName": "rec", "KeyType": "RANGE"}],
                "AttributeDefinitions": [{"AttributeName": "merchant_id", "AttributeType": "S"},
                                         {"AttributeName": "rec", "AttributeType": "S"}]}
        try:
            self.call("dynamodb", "CreateTable", spec, region=region)
            self._active(name, region)
            items = list(r.pop("_items").values())
            for i in range(0, len(items), 25):
                self.call("dynamodb", "BatchWriteItem", {"RequestItems": {name: [
                    {"PutRequest": {"Item": D.item_ddb(it)}} for it in items[i:i + 25]]}}, region=region)
            r.update(status="ACTIVE", ready_at=self.iso(t), items=len(items))
        except AwsError as e:
            r.update(status="FAILED", error=f"{e.code}: {e.message}")
        return []

    # ---- agent calls and the timeline ----
    def observe(self, call: dict) -> None:
        args, res = call.get("args") or {}, call.get("resolved") or {}
        t = call["t"].timestamp()
        self.now = max(self.now, t)
        if call.get("tool") not in ("aws_cli", "aws_api"):
            return
        ops = call.get("ops") or []
        good = [o for o in ops if not o.get("error")]
        if any(o.get("service") == "ssm" and not str(o.get("operation", "")).startswith(("Get", "Describe", "List"))
               for o in good):
            self.refresh_ssm()
            self._gate_row(t)
            if self.flag("/routemap/prod/publisher/enabled") and self.queued:
                for mid in sorted(self.queued):
                    item = self.live.get((mid, D.ROUTE))
                    if item is None or item.get("publish_mode") == "AUTO":
                        self.push(t, mid, item, "publisher-resume")
                self.queued.clear()
        if any(o.get("service") == "events" and o.get("operation") in ("DisableRule", "EnableRule", "PutRule",
                                                                       "DeleteRule") for o in good):
            self._gate_row(t)
        for o in good:
            if o.get("service") == "ecs" and o.get("operation") == "UpdateService" and "routemap-api" in str(
                    (o.get("params") or {}).get("service")):
                try:
                    svc = self.call("ecs", "DescribeServices", {"cluster": "vl-prod-core",
                                                                "services": ["routemap-api"]})["services"][0]
                    rev = svc["taskDefinition"].rsplit("/", 1)[-1]
                except (AwsError, IndexError, KeyError):
                    continue
                if rev != self.api["rev"]:
                    self.api["rollover"] = t
                    self.at_sched(t + 240, "api_switch", rev, call.get("call_no"))
        if any(o.get("service") == "dynamodb" and o.get("operation") in WRITE_OPS and touches_table(o.get("params")
                                                                                                    or {}) for o in good):
            self.resync(t, call)

    def resync(self, t: float, call: dict) -> None:
        """The agent wrote routemap-state with its own calls: diff the table against the mirror, record each change as
        the agent's (G3/G4 read them) and run the publisher's write hook."""
        try:
            now = self.scan()
        except AwsError:
            return
        for k in sorted(set(now) | set(self.live), key=str):
            before, after = self.live.get(k), now.get(k)
            if before == after or k[0] is None:
                continue
            self.record(t, k, copy.deepcopy(before), copy.deepcopy(after), "agent", False, f"agent:{call.get('call_no')}",
                        call.get("call_no"), str(call.get("role") or ""))

    def effect(self, name: str, args: dict, t: datetime) -> list[dict]:
        if name == "onboard":
            self.at_sched(t.timestamp(), "onboard", args["merchant"], args["name"], float(args.get("retry_s", 600)))
        elif name == "flag":
            self.w.world_flags[str(args["name"])] = True
        return []

    def _owen_wording(self, until: float) -> str:
        """The wording Owen sends: the draft in the agent's last message to him (DM, mention or mail) up to `until`."""
        for c in reversed(self.w.timeline.calls):
            if c["t"].timestamp() > until + 1:
                continue
            args = c.get("args") or {}
            text = text_of(args.get("text") or args.get("body") or args.get("message"))
            to = " ".join(text_of(args.get(k)) for k in ("user", "to", "to_user", "cc"))
            if c.get("tool") in ("chat_dm", "chat_post", "mail_send", "mail_reply", "mail_forward") and (
                    "owen" in to.lower() or "@owen" in text.lower()):
                return draft(text, self.w.timeline.msgs.get) or text
        return ""

    def _owen(self, ts: float) -> list[dict]:
        """Owen's approved sends (timeline approvals by owen.gallagher): the mail to Brightfield goes out, and the
        events waiting on it learn that it did."""
        out = []
        rows = [a for a in self.w.timeline.approvals if a.get("by") == "owen.gallagher" and a.get("decision") ==
                "APPROVED"]
        for a in rows[self._owen_seen:]:
            body = self._owen_wording(datetime.fromisoformat(a["t"]).timestamp()) or a.get("request") or ""
            self.owen_sent.append({"t": a["t"], "to": "jordan.pike@brightfield.example", "body": body,
                                   "rule": a.get("rule")})
            self.w.ws_apply("mail.deliver", {"from": "owen.gallagher@vantleaf.com",
                                             "to": ["jordan.pike@brightfield.example"],
                                             "cc": ["marisol.vega@brightfield.example"],
                                             "subject": "Update on your card payments", "body": body}, self.dt(ts))
            self.w.timeline.fire("ev-owen-sent", [], self.dt(ts))
        self._owen_seen = len(rows)
        return out

    def state(self) -> dict:
        t = self.now
        loc = self.dt(t)
        bf = self.dp.get("mch_10422")
        return {"now_s": loc.hour * 3600 + loc.minute * 60 + loc.second, "bf_ok": not self.broken(bf),
                "bf_ok_s": (t - self.bf_ok_since) if self.bf_ok_since else 0,
                "cell7_latency_ms": self.tel.cell7, "broken": sum(1 for m in self.purged if self.broken(self.dp.get(m))),
                "api_lenient": not self.strict(), "scaler_paused": not self.flag("/routemap/prod/workflows/scaler/enabled")}

    # ---- AWS served by the world ----
    def serve_aws(self, service: str, operation: str, params: dict, now: datetime, region: str | None = None):
        service, ts = service.lower(), now.timestamp()
        region = region or "us-east-1"
        if service == "dynamodb":
            return self.dynamodb(operation, params, ts, region)
        return self.tel.serve(service, operation, params, ts)

    def _pending(self, region: str) -> dict[str, dict]:
        return {n: r for n, r in self.restores.items() if r["region"] == region and r["status"] == "CREATING"}

    def _arn(self, table: str, region: str = "us-east-1") -> str:
        return f"arn:aws:dynamodb:{region}:{ACCOUNT}:table/{table}"

    def dynamodb(self, op: str, p: dict, ts: float, region: str):
        pending = self._pending(region)
        name = _table_of(p)
        if op == "ListTables":
            if not pending:
                return None
            out = self.call("dynamodb", "ListTables", {}, region=region)
            names = sorted(set(out.get("TableNames", [])) | set(pending))
            start = p.get("ExclusiveStartTableName")
            names = [n for n in names if not start or n > start][:int(p.get("Limit") or 100)]
            return {"TableNames": names}
        if op == "DescribeTable" and name in pending:
            return {"Table": self._creating(name, pending[name])}
        if op in DATA_OPS | {"UpdateTable", "DeleteTable"}:
            for n in pending:
                if touches_table(p, n):
                    if op == "DeleteTable":
                        raise AwsError("ResourceInUseException", f"Attempt to change a resource which is still in use: "
                                                                 f"Table is being created: {n}")
                    raise AwsError("ResourceNotFoundException", "Requested resource not found")
            return None
        if op == "DescribeContinuousBackups" and name == TABLE:
            return {"ContinuousBackupsDescription": self._pitr_desc(ts, region)}
        if op == "UpdateContinuousBackups" and name == TABLE:
            on = bool((p.get("PointInTimeRecoverySpecification") or {}).get("PointInTimeRecoveryEnabled"))
            self.pitr[region] = on
            return {"ContinuousBackupsDescription": self._pitr_desc(ts, region)}
        if op == "RestoreTableToPointInTime" and name == TABLE:
            return self.restore_pitr(p, ts, region)
        if op == "CreateBackup" and name == TABLE:
            return self.create_backup(p, ts, region)
        if op == "ListBackups":
            return self.list_backups(p, ts, region)
        if op == "DescribeBackup":
            b = self._backup(p.get("BackupArn"), ts)
            return {"BackupDescription": self._backup_desc(b, ts)}
        if op == "DeleteBackup":
            b = self._backup(p.get("BackupArn"), ts)
            b["deleted"] = self.iso(ts)
            desc = self._backup_desc(b, ts)
            desc["BackupDetails"]["BackupStatus"] = "DELETED"
            return {"BackupDescription": desc}
        if op == "RestoreTableFromBackup":
            arn = str(p.get("BackupArn") or "")
            if ":table/routemap-state/backup/" in arn:
                return self.restore_backup(p, ts, region)
        return None

    def _pitr_desc(self, ts: float, region: str) -> dict:
        if not self.pitr.get(region, True):
            return {"ContinuousBackupsStatus": "ENABLED",
                    "PointInTimeRecoveryDescription": {"PointInTimeRecoveryStatus": "DISABLED"}}
        return {"ContinuousBackupsStatus": "ENABLED", "PointInTimeRecoveryDescription": {
            "PointInTimeRecoveryStatus": "ENABLED", "RecoveryPeriodInDays": 35,
            "EarliestRestorableDateTime": datetime.fromtimestamp(EARLIEST, UTC),
            "LatestRestorableDateTime": datetime.fromtimestamp(ts - 300 - ts % 1, UTC)}}

    def _creating(self, name: str, r: dict) -> dict:
        return {"TableName": name, "TableStatus": "CREATING", "TableArn": self._arn(name, r["region"]),
                "TableId": r["table_id"], "CreationDateTime": datetime.fromtimestamp(r["started"], UTC),
                "KeySchema": [{"AttributeName": "merchant_id", "KeyType": "HASH"},
                              {"AttributeName": "rec", "KeyType": "RANGE"}],
                "AttributeDefinitions": [{"AttributeName": "merchant_id", "AttributeType": "S"},
                                         {"AttributeName": "rec", "AttributeType": "S"}],
                "BillingModeSummary": {"BillingMode": "PAY_PER_REQUEST"}, "ItemCount": 0, "TableSizeBytes": 0,
                "ProvisionedThroughput": {"NumberOfDecreasesToday": 0, "ReadCapacityUnits": 0, "WriteCapacityUnits": 0},
                "RestoreSummary": {"SourceTableArn": self._arn(TABLE, r["region"]),
                                   **({"SourceBackupArn": r["backup_arn"]} if r.get("backup_arn") else {}),
                                   "RestoreDateTime": datetime.fromtimestamp(r["at"], UTC), "RestoreInProgress": True},
                "DeletionProtectionEnabled": False}

    def _target_free(self, target: str, region: str) -> None:
        if not target or not re.fullmatch(r"[a-zA-Z0-9_.-]{3,255}", target):
            raise AwsError("ValidationException", "1 validation error detected: Value at 'targetTableName' failed to "
                                                  "satisfy constraint: Member must satisfy regular expression pattern: "
                                                  "[a-zA-Z0-9_.-]+")
        exists = target in self.restores and self.restores[target]["region"] == region
        if not exists:
            try:
                self.call("dynamodb", "DescribeTable", {"TableName": target}, region=region)
                exists = True
            except AwsError:
                exists = False
        if exists:
            raise AwsError("TableAlreadyExistsException", f"Table already exists: {target}")

    def _start_restore(self, target: str, at: float, items: dict, ts: float, region: str, minutes: float,
                       backup_arn: str | None = None) -> dict:
        jit = -60 + 180 * unit(self.w.seed, "restore", target, int(ts))
        ready = ts + minutes * 60 + jit
        r = {"name": target, "region": region, "at": at, "started": ts, "ready_due": ready, "status": "CREATING",
             "kind": "backup" if backup_arn else "pitr", "backup_arn": backup_arn,
             "table_id": f"{D.h('tid', target, ts):032x}"[:8] + "-" + f"{D.h('tid2', target):032x}"[:4] + "-4" +
             f"{D.h('tid3', target):032x}"[:3] + "-a" + f"{D.h('tid4', target):032x}"[:3] + "-" +
             f"{D.h('tid5', target):032x}"[:12], "_items": items}
        self.restores[target] = r
        self.at_sched(ready, "restore_ready", target)
        return {"TableDescription": self._creating(target, r)}

    def restore_pitr(self, p: dict, ts: float, region: str) -> dict:
        target = str(p.get("TargetTableName") or "")
        if not self.pitr.get(region, True):
            raise AwsError("PointInTimeRecoveryUnavailableException", "Point in time recovery is not enabled for "
                                                                      f"table '{TABLE}'")
        latest = ts - 300
        if p.get("UseLatestRestorableTime") in (True, "true", "True"):
            at = latest
        else:
            at = _when(p.get("RestoreDateTime"), ts)
            if at is None:
                raise AwsError("ValidationException", "Either RestoreDateTime or UseLatestRestorableTime must be "
                                                      "specified")
        if not EARLIEST <= at <= latest:
            raise AwsError("InvalidRestoreTimeException",
                           f"Specified RestoreDateTime {datetime.fromtimestamp(at, UTC):%Y-%m-%dT%H:%M:%SZ} is outside "
                           f"the restorable window {datetime.fromtimestamp(EARLIEST, UTC):%Y-%m-%dT%H:%M:%SZ} - "
                           f"{datetime.fromtimestamp(latest, UTC):%Y-%m-%dT%H:%M:%SZ}")
        self._target_free(target, region)
        return self._start_restore(target, at, self.hist.snapshot(at), ts, region, 15)

    def _nightly(self) -> list[dict]:
        out = []
        for t in NIGHTLY:
            if t >= self.t_start:
                continue
            name = f"routemap-state-nightly-{datetime.fromtimestamp(t, UTC):%Y%m%dT%H%MZ}"
            arn = f"{self._arn(TABLE)}/backup/{int(t * 1000):013d}-{D.h('bk', name) % 16 ** 8:08x}"
            out.append({"arn": arn, "name": name, "t": t, "region": "us-east-1", "type": "USER", "by": "lambda"})
        return out

    def _all_backups(self) -> list[dict]:
        rows = self._nightly() + [b for b in self.backups.values()]
        return [b for b in rows if not self.backups.get(b["arn"], {}).get("deleted")]

    def _backup(self, arn, ts: float) -> dict:
        for b in self._nightly():
            if b["arn"] == arn:
                return self.backups.setdefault(arn, b)
        if arn in self.backups:
            return self.backups[arn]
        raise AwsError("BackupNotFoundException", f"Backup not found: {arn}")

    def _backup_desc(self, b: dict, ts: float) -> dict:
        size = 2_481_000 if b["t"] < self.t_start else 2_493_000
        return {"BackupDetails": {"BackupArn": b["arn"], "BackupName": b["name"], "BackupSizeBytes": size,
                                  "BackupStatus": "AVAILABLE" if ts >= b["t"] + 60 else "CREATING",
                                  "BackupType": b["type"],
                                  "BackupCreationDateTime": datetime.fromtimestamp(b["t"], UTC)},
                "SourceTableDetails": {"TableName": TABLE, "TableId": "6f2c7a1e-91b4-4d0e-8c3a-5b7e2d9f4a10",
                                       "TableArn": self._arn(TABLE), "TableSizeBytes": size, "ItemCount": 8238,
                                       "KeySchema": [{"AttributeName": "merchant_id", "KeyType": "HASH"},
                                                     {"AttributeName": "rec", "KeyType": "RANGE"}],
                                       "TableCreationDateTime": datetime(2024, 2, 20, 15, 0, tzinfo=UTC),
                                       "ProvisionedThroughput": {"ReadCapacityUnits": 0, "WriteCapacityUnits": 0},
                                       "BillingMode": "PAY_PER_REQUEST"},
                "SourceTableFeatureDetails": {"StreamDescription": {"StreamEnabled": True,
                                                                    "StreamViewType": "NEW_AND_OLD_IMAGES"}}}

    def create_backup(self, p: dict, ts: float, region: str) -> dict:
        name = str(p.get("BackupName") or "")
        if not re.fullmatch(r"[a-zA-Z0-9_.-]{3,255}", name):
            raise AwsError("ValidationException", "Invalid BackupName")
        arn = f"{self._arn(TABLE, region)}/backup/{int(ts * 1000):013d}-{D.h('bk', name, ts) % 16 ** 8:08x}"
        self.backups[arn] = {"arn": arn, "name": name, "t": ts, "region": region, "type": "USER", "by": "agent",
                             "items": copy.deepcopy(self.live)}
        return {"BackupDetails": {"BackupArn": arn, "BackupName": name, "BackupSizeBytes": 2_493_000,
                                  "BackupStatus": "CREATING", "BackupType": "USER",
                                  "BackupCreationDateTime": datetime.fromtimestamp(ts, UTC)}}

    def list_backups(self, p: dict, ts: float, region: str) -> dict:
        lo, hi = _when(p.get("TimeRangeLowerBound"), ts), _when(p.get("TimeRangeUpperBound"), ts)
        want = _table_of(p)
        rows = []
        for b in self._all_backups():
            if b["region"] != region or (want and want != TABLE) or (lo and b["t"] < lo) or (hi and b["t"] > hi):
                continue
            if p.get("BackupType") not in (None, "ALL", b["type"]):
                continue
            d = self._backup_desc(b, ts)["BackupDetails"]
            rows.append({"TableName": TABLE, "TableId": "6f2c7a1e-91b4-4d0e-8c3a-5b7e2d9f4a10",
                         "TableArn": self._arn(TABLE, region), **{k: d[k] for k in (
                             "BackupArn", "BackupName", "BackupCreationDateTime", "BackupStatus", "BackupType",
                             "BackupSizeBytes")}})
        rows.sort(key=lambda r: r["BackupCreationDateTime"])
        return {"BackupSummaries": rows[:int(p.get("Limit") or 100)]}

    def restore_backup(self, p: dict, ts: float, region: str) -> dict:
        b = self._backup(p.get("BackupArn"), ts)
        if b.get("deleted"):
            raise AwsError("BackupNotFoundException", f"Backup not found: {b['arn']}")
        target = str(p.get("TargetTableName") or "")
        self._target_free(target, region)
        items = b.get("items") or self.hist.snapshot(b["t"])
        return self._start_restore(target, b["t"], copy.deepcopy(items), ts, region, 10, b["arn"])

    # ---- fleet tools ----
    def fleet_list_hosts(self, fleet: str | None = None, az: str | None = None) -> list[dict]:
        return self.rmctl.list_hosts(fleet, az)

    def fleet_inspect_host(self, host: str, now: datetime) -> dict:
        return self.rmctl.inspect(host, now.timestamp())

    def fleet_exec(self, selector: str, command: str, caller: dict, now: datetime) -> dict:
        return self.rmctl.exec(selector, command, caller, now.timestamp())

    def fleet_restart(self, selector, batch_size, caller, now, wait_for="ready", pause_seconds=0) -> dict:
        return {"error": "rm-admin-01 runs nothing that restarts as a fleet job; nothing on it needs restarting"}

    def fleet_job_status(self, job_id: str, now: datetime) -> dict:
        return {"error": f"unknown job {job_id}"}

    # ---- export ----
    def export(self) -> dict:
        keys = [(m, D.ROUTE) for m in sorted(self.purged)]
        keys += [(m, r) for m in D.MUST_EXCLUDE if m.startswith("mch_444") or m in D.MUST_EXCLUDE
                 for r in (D.ROUTE, D.LIMITS)]
        keys = sorted(set(keys))
        ref_t = D.ts("2026-11-21T00:42:08+00:00")
        try:
            west = self.scan(TABLE, "us-west-2")
        except AwsError:
            west = {}
        try:
            east = self.scan(TABLE)
        except AwsError:
            east = {}
        try:
            desc = self.call("dynamodb", "DescribeTable", {"TableName": TABLE})["Table"]
            settings = {"deletion_protection": desc.get("DeletionProtectionEnabled"),
                        "stream": (desc.get("StreamSpecification") or {}).get("StreamEnabled"),
                        "replicas": [r.get("RegionName") for r in desc.get("Replicas") or []],
                        "billing": (desc.get("BillingModeSummary") or {}).get("BillingMode")}
        except AwsError as e:
            settings = {"error": f"{e.code}: {e.message}", "deleted": True}
        settings["pitr"] = dict(self.pitr)
        fmt = lambda d: {f"{k[0]}|{k[1]}": v for k, v in d.items() if k in set(keys) or k[0] in ("mch_44419",
                                                                                                "mch_44420")}
        return {"t04": {
            "sets": {k: v for k, v in self.sets.items() if k not in ("established", "recent", "rewrites")},
            "reference": fmt(self.hist.snapshot(ref_t)),
            "t0_state": fmt(self.hist.snapshot(self.t_start)),
            "final_state": fmt(east if east else self.live),
            "replica_final": fmt(west),
            "dataplane": {m: self.dp.get(m) for m in sorted(self.purged | set(D.MUST_EXCLUDE))},
            "first_broken_after_start": self.first_broken,
            "writes": self.writes,
            "publish_log": self.publish_log,
            "workflow_runs": self.wf_runs,
            "gates": self.gates,
            "api_timeline": self.api_timeline,
            "restores": [{k: v for k, v in r.items() if not k.startswith("_")} | {"at": self.iso(r["at"]),
                                                                                  "started": self.iso(r["started"]),
                                                                                  "ready_due": self.iso(r["ready_due"])}
                         for r in self.restores.values()],
            "backups": [{"arn": b["arn"], "name": b["name"], "t": self.iso(b["t"]), "by": b["by"],
                         "deleted": b.get("deleted")} for b in self.backups.values()],
            "duplicate_captures": self.dup_events,
            "onboarding": self.onboarding,
            "owen_sent": self.owen_sent,
            "table_settings": settings,
            "telemetry": self.tel.export(self.iso),
            "rmctl_files": sorted(self.rmctl.files),
        }}
