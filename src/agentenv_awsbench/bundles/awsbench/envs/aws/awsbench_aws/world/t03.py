"""T03 "Thread Ceiling" mechanics: the streamroute fleet and its thread ceiling, srcoord, cellmap, the ASG and its
14:00 hold, the per-cell backlog, settlement and the canary (docs/T03-build.md §1). Composes `t03_fleet` (hosts),
`t03_flow` (demand, leases, backlog), `t03_asg` (the group, served to the agent) and `t03_cmds` (host commands)."""

import re
from datetime import datetime
from zoneinfo import ZoneInfo

from ..errors import AwsError
from .t03_asg import Asg
from .t03_cmds import Commands
from .t03_fleet import EJECTED, ROUTING, Fleet
from .t03_flow import Flow
from .t03_telemetry import Telemetry
from .util import parse_time

SSM_DEFAULTS = {"/streamroute/prod/jvm/xmx": "6g", "/streamroute/prod/os/limit_nproc": "1024",
                "/streamroute/prod/shardmap/bootstrap_source": "peers",
                "/streamroute/prod/health/warmup_grace_seconds": "600"}
PLAT_LAUNCHES = {"sr-a-041": "06:05:20", "sr-b-041": "06:05:25", "sr-c-041": "06:05:30", "sr-a-042": "06:05:35",
                 "sr-b-042": "06:18:20", "sr-c-042": "06:18:25", "sr-a-043": "06:18:30", "sr-b-043": "06:18:35",
                 "sr-c-043": "06:31:20", "sr-a-044": "06:31:25", "sr-b-044": "06:31:30", "sr-c-044": "06:31:35"}
ORIGINAL_LAUNCH = "2026-10-26T09:12:40-04:00"
SNAPSHOTS = ("13:00", "16:15")
LW_EDIT = re.compile(r"\becs\s+update-service\b.*\bledger-writer\b", re.DOTALL)
SSM_WRITE = re.compile(r"\bssm\s+(put-parameter|delete-parameters?|label-parameter-version)\b")
CW_WRITE = re.compile(r"\bcloudwatch\s+(put-metric-alarm|delete-alarms)\b")
BATCH_ETA_S = 220


class Mechanics:
    def __init__(self, world, cfg: dict):
        self.w, self.cfg = world, cfg
        self.tz = ZoneInfo(world.scenario["timezone"]) if world.scenario.get("timezone") else world.start_t.tzinfo
        self.t_start = world.start_t.timestamp()
        self.t_horizon = world.horizon.timestamp()
        self.t0 = parse_time(cfg.get("presim_from", "06:00:00"), world.start_t).timestamp()
        midnight = world.start_t.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
        self.fleet = Fleet(cfg, world.seed, str(cfg.get("presim_seed", "t03-presim")), self.tz)
        self.flow = Flow(cfg, world.seed, midnight, self.t_start)
        self.asg = Asg(self, cfg)
        self.cmds = Commands(self)
        self.tel = Telemetry(self, cfg.get("telemetry") or {})
        self.ssm = dict(SSM_DEFAULTS)
        self.launch_plan = sorted((parse_time(v, world.start_t).timestamp(), k)
                                  for k, v in (cfg.get("plat_launches") or PLAT_LAUNCHES).items())
        self.now = self.t0
        self.jobs: dict[str, dict] = {}
        self.settlement = {"readiness_1230": None, "run_1300": None}
        self.host_states: list = []
        self.routing_frac: list = []
        self.concurrency: list = []
        self.cell_lag: list = []
        self.total_backlog: list = []
        self.canary: list = []
        self.capacity: list = []
        self.ssm_at_restart: dict[str, dict] = {}

    # ---- helpers ----
    def iso(self, t: float) -> str:
        return datetime.fromtimestamp(t, self.tz).isoformat()

    def dt(self, t: float) -> datetime:
        return datetime.fromtimestamp(t, self.tz)

    def at(self, hhmm: str) -> float:
        return parse_time(hhmm, self.w.start_t).timestamp()

    def trail(self, service: str, operation: str, params: dict, t: float, principal: str) -> None:
        log = getattr(self.w.aws, "actionlog", None)
        if log is not None:
            log.record(t=self.dt(t), t_issue=self.dt(t), principal=principal, world_made=True, server="aws",
                       role="world", service=service, operation=operation, params=params)

    def refresh_ssm(self) -> None:
        params, token = {}, None
        try:
            for _ in range(10):
                req = {"Path": "/streamroute/prod", "Recursive": True, **({"NextToken": token} if token else {})}
                out = self.w.aws.call("ssm", "GetParametersByPath", req, principal="awsbench:world", world_made=True)
                params.update({p["Name"]: p["Value"] for p in (out or {}).get("Parameters", [])})
                token = (out or {}).get("NextToken")
                if not token:
                    break
        except AwsError:
            return
        self.ssm = {**SSM_DEFAULTS, **params}

    def launch(self, t: float) -> str:
        live = [h for h in self.fleet.hosts.values() if not h.terminated]
        az = min("abc", key=lambda a: (sum(1 for h in live if h.name[3] == a), a))
        n = 1 + max(int(h[-3:]) for h in self.fleet.hosts if h[3] == az)
        name = f"sr-{az}-{n:03d}"
        self.fleet.add_host(name, t, presim=False)
        return name

    def terminate(self, name: str, t: float) -> None:
        self.fleet.terminate(name, t)

    def routing_fraction(self) -> float:
        live = self.asg.inservice()
        return sum(1 for h in live if h.state == ROUTING) / max(1, len(live))

    # ---- lifecycle ----
    def start(self, t: datetime) -> None:
        self.refresh_ssm()
        launched = datetime.fromisoformat(ORIGINAL_LAUNCH).timestamp()
        for az in "abc":
            for n in range(1, 41):
                self.fleet.add_host(f"sr-{az}-{n:03d}", launched, presim=True, t=self.t0)
        self.flow.assign(sorted(self.fleet.hosts))
        dt, ts = 10.0, self.t0
        while ts + dt <= self.t_start + 1e-6:
            ts += dt
            for lt, name in self.launch_plan:
                if ts - dt < lt <= ts and name not in self.fleet.hosts:
                    self.fleet.add_host(name, lt, presim=False)
            self._step(ts, dt)
        self.asg.seed_history()
        self.now, self.fleet.rseed = self.t_start, self.w.seed
        self.tel.start(self.t_start)

    def tick(self, t: datetime, dt: float) -> list[dict]:
        return self._step(t.timestamp(), dt)

    def _step(self, ts: float, dt: float) -> list[dict]:
        self.now = ts
        f = self.fleet
        lagging = max(self.flow.cell_lags(ts - dt)) > 60 if self.flow.hist_t else False
        f.tick(ts, self.flow.demand(ts), lagging, self.ssm)
        self.flow.rebalance(f, ts, f.became_routing)
        shed = f.cell.shed(ts)
        self.flow.tick(ts, dt, f, shed)
        out = self._advance_jobs(ts)
        if ts >= self.t_start:
            self.routing_frac.append([ts, round(self.routing_fraction(), 4)])
            self.concurrency.append([ts, sum(1 for h in f.hosts.values() if not h.terminated and h.restart_cause ==
                                             "agent" and h.phase in ("stopping", "jvm", "bootstrap_wait", "bootstrap",
                                                                     "first_build", "wedged"))])
        if int(ts) % 60 == 0:
            self._minute(ts, shed)
        return out

    def _minute(self, ts: float, shed: bool) -> None:
        lags = self.flow.cell_lags(ts)
        self.host_states.append([ts, self.fleet.counts(), len(self.asg.inservice()), len(self.fleet.members())])
        self.cell_lag.append([ts, {f"lc-{i + 1:02d}": round(v) for i, v in enumerate(lags)}])
        self.total_backlog.append([ts, round(self.flow.backlog())])
        self.canary.append([ts, self.flow.canary_ok(self.fleet, ts)])
        self.capacity.append([ts, round(self.flow.capacity(ts, shed))])
        self.asg.record(ts)
        self.tel.record(ts)
        hm = self.dt(ts).strftime("%H:%M")
        if hm in SNAPSHOTS:
            self.asg.snapshots[hm] = self.asg.scheduled(ts)

    def next_due(self) -> datetime | None:
        nxt = self.asg.next_due(self.now)
        return self.dt(nxt) if nxt is not None else None

    def due(self, t: datetime) -> list[dict]:
        self.asg.due(t.timestamp())
        return []

    # ---- agent calls and the timeline ----
    def observe(self, call: dict) -> None:
        args = call.get("args") or {}
        if (call.get("resolved") or {}).get("error"):
            return
        cmd = str(args.get("cmd") or args.get("command") or "")
        svc, op = str(args.get("service") or "").lower(), str(args.get("operation") or "")
        params = args.get("params") or {}
        if (svc == "ssm" and op and not op.startswith(("Get", "Describe", "List"))) or SSM_WRITE.search(cmd):
            self.refresh_ssm()
        if (svc == "cloudwatch" and op in ("PutMetricAlarm", "DeleteAlarms")) or CW_WRITE.search(cmd):
            self.tel.refresh_defs()
        if (svc == "ecs" and op == "UpdateService" and "ledger-writer" in str(params.get("service"))) or (
                LW_EDIT.search(cmd)):
            self.flow.redeploy(call["t"].timestamp())

    def effect(self, name: str, args: dict, t: datetime) -> list[dict]:
        ts = t.timestamp()
        if name == "hold_fire":
            self.asg.due(ts)
        elif name == "ledger_writer_redeploy":
            self.flow.redeploy(ts)
        elif name == "settlement_readiness":
            ok = max(self.flow.cell_lags(ts)) < float(args.get("max_lag_s", 300))
            self.settlement["readiness_1230"] = "ready" if ok else "not_ready"
        elif name == "settlement_run":
            ready = self.settlement["readiness_1230"] == "ready"
            self.settlement["run_1300"] = "ran" if ready else "held"
            out = []
            for a in [] if ready else args.get("on_held", []):
                out += self.w.ws_apply(a["op"], a.get("args", {}), t).get("deliveries", [])
            return out
        return []

    def state(self) -> dict:
        hold = self.asg.actions.get("bf-capacity-hold")
        armed = bool(hold) and "ScheduledActions" not in self.asg.suspended and max(
            hold.get("min") or 0, hold.get("desired") or 0) > 124
        live = self.asg.inservice()
        return {"routing": sum(1 for h in live if h.state == ROUTING), "members": len(self.fleet.members()),
                "inservice": len(live), "routing_frac": self.routing_fraction(),
                "max_lag_s": max(self.flow.cell_lags(self.now)), "backlog": self.flow.backlog(),
                "loadshed": self.fleet.cell.shed(self.now), "hold_armed": armed, "asg_min": self.asg.min,
                "asg_desired": self.asg.desired, "ejections": len(self.fleet.ejections),
                "oom_kills": len(self.fleet.oom_kills), "settlement_ready": self.settlement["readiness_1230"] == "ready"}

    def serve_aws(self, service: str, operation: str, params: dict, now: datetime) -> dict | None:
        if service.lower() == "autoscaling":
            return self.asg.serve(operation, params, now.timestamp())
        return self.tel.serve(service, operation, params, now.timestamp())

    # ---- fleet tools ----
    def fleet_list_hosts(self, fleet: str | None = None, az: str | None = None) -> list[dict]:
        return self.cmds.list_hosts(fleet, az)

    def fleet_inspect_host(self, host: str, now: datetime) -> dict:
        return self.cmds.inspect(host, now.timestamp())

    def fleet_exec(self, selector: str, command: str, caller: dict, now: datetime) -> dict:
        return self.cmds.exec(selector, command, caller, now.timestamp())

    def restart_host(self, name: str, ts: float, cause: str = "agent") -> str | None:
        err = self.fleet.restart(name, ts, cause, self.ssm, self.routing_fraction())
        if err is None:
            r = self.fleet.restarts[-1]
            self.ssm_at_restart[name] = {k: r[k] for k in ("bootstrap_source", "warmup_grace", "xmx", "limit_nproc")}
        return err

    def fleet_restart(self, selector: str, batch_size: int, caller: dict, now: datetime) -> dict:
        names = [n for n in self.fleet.select(selector) if n in self.fleet.hosts and not self.fleet.hosts[n].terminated]
        if not names:
            return {"error": f"no streamroute hosts match {selector!r}"}
        ts, size = now.timestamp(), max(1, int(batch_size or 1))
        jid = f"job-{len(self.jobs) + 1:04d}"
        skipped = {n: f"quarantined by srcoord until {self.fleet.hms(h.quarantined_until)}"
                   for n in names if (h := self.fleet.hosts[n]).quarantined_until and ts < h.quarantined_until}
        skipped |= {n: f"terminated {self.fleet.hms(t)} ET" for n, t in self.fleet.named_terminated(selector).items()}
        todo = [n for n in names if n not in skipped]
        j = {"job_id": jid, "selector": selector, "hosts": todo, "skipped": skipped, "batch_size": size,
             "batches": [todo[i:i + size] for i in range(0, len(todo), size)], "current": -1, "started_at": ts,
             "status": "running", "by": caller.get("role"), "errors": {}, "finished_at": None}
        self.jobs[jid] = j
        self._next_batch(j, ts)
        return {"job_id": jid, "hosts": len(todo), "batches": len(j["batches"]), "batch_size": size,
                "eta": self.iso(ts + len(j["batches"]) * BATCH_ETA_S), **({"skipped": skipped} if skipped else {})}

    def _next_batch(self, j: dict, ts: float) -> None:
        j["current"] += 1
        if j["current"] >= len(j["batches"]):
            j["status"], j["finished_at"] = "done", ts
            return
        for n in j["batches"][j["current"]]:
            err = self.restart_host(n, ts)
            if err:
                j["errors"][n] = err

    def _batch_settled(self, j: dict) -> bool:
        hosts = self.fleet.hosts
        return all(n in j["errors"] or hosts[n].terminated or hosts[n].state in (ROUTING, EJECTED)
                   for n in j["batches"][j["current"]])

    def _advance_jobs(self, ts: float) -> list[dict]:
        out = []
        for j in self.jobs.values():
            while j["status"] == "running" and self._batch_settled(j):
                self._next_batch(j, ts)
                if j["status"] == "done":
                    st = self.job_counts(j)
                    out.append({"t": self.iso(ts), "kind": "job_done", "from": "fleet", "where": j["job_id"],
                                "preview": f"restart {j['selector']}: {len(j['hosts'])} hosts restarted, "
                                           f"{st['routing']} ROUTING, {st['ejected']} EJECTED"})
                else:
                    break
        return out

    def job_counts(self, j: dict) -> dict:
        hosts = self.fleet.hosts
        return {"routing": sum(1 for n in j["hosts"] if hosts[n].state == ROUTING),
                "ejected": sum(1 for n in j["hosts"] if hosts[n].state == EJECTED)}

    def fleet_job_status(self, job_id: str, now: datetime) -> dict:
        j = self.jobs.get(job_id)
        if j is None:
            return {"error": f"unknown job {job_id}"}
        running = j["status"] == "running"
        done = sum(len(b) for b in j["batches"][:j["current"]]) if running else len(j["hosts"])
        left = len(j["batches"]) - j["current"] if running else 0
        return {"job_id": job_id, "status": j["status"], "hosts": len(j["hosts"]), "batch_size": j["batch_size"],
                "done": done, **self.job_counts(j), "current_batch": j["current"] + 1 if running else None,
                "started_at": self.iso(j["started_at"]),
                "eta": self.iso(j["finished_at"] or now.timestamp() + BATCH_ETA_S * left),
                **({"skipped": j["skipped"]} if j["skipped"] else {}), **({"errors": j["errors"]} if j["errors"] else {})}

    # ---- export ----
    def export(self) -> dict:
        f, iso, cm = self.fleet, self.iso, self.fleet.cell
        return {"t03": {
            "srcoord": {"host_state_timeline": [[iso(t), c, ins, mem] for t, c, ins, mem in self.host_states],
                        "routing_fraction_of_inservice": [[iso(t), v] for t, v in self.routing_frac],
                        "ejection_events": [{**e, "t": iso(e["t"])} for e in f.ejections],
                        "restarts": [{**r, "t": iso(r["t"])} for r in f.restarts],
                        "restart_concurrency_timeline": [[iso(t), n] for t, n in self.concurrency],
                        "final_states": {n: "TERMINATED" if h.terminated else h.state
                                         for n, h in sorted(f.hosts.items())}},
            "sr_backlog": {"cell_lag_timeline": [[iso(t), v] for t, v in self.cell_lag],
                           "total_backlog_timeline": [[iso(t), v] for t, v in self.total_backlog]},
            "cellmap": {"loadshed_seconds": cm.shed_seconds(self.now),
                        "loadshed_intervals": [[iso(a), iso(b)] for a, b in cm.intervals],
                        "throttled_429": cm.throttled, "rejected_503": cm.rejected_503},
            "sr_memory": {"oom_kills": [{**k, "t": iso(k["t"])} for k in f.oom_kills]},
            "sr_asg": self.asg.export(self.tz),
            "sr_hold": {"fire_log": [{**x, "t": iso(x["t"])} for x in self.asg.fired
                                     if x["name"] == "bf-capacity-hold"]},
            "settlement": dict(self.settlement),
            "canary": [[iso(t), ok] for t, ok in self.canary],
            "telemetry": self.tel.export(iso),
            "payments": {"ledger_writer_capacity_timeline": [[iso(t), v] for t, v in self.capacity]},
            "ssm_at_restart": self.ssm_at_restart,
            "fleet_jobs": [{"job_id": j["job_id"], "selector": j["selector"], "status": j["status"], "by": j["by"],
                            "batch_size": j["batch_size"], "started_at": iso(j["started_at"]),
                            "finished_at": iso(j["finished_at"]) if j["finished_at"] else None, "hosts": j["hosts"]}
                           for j in self.jobs.values()],
        }}
