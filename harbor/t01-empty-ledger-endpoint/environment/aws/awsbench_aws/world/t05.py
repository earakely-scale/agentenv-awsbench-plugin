"""T05 "Capacity Glob Cold Start" mechanics: vaultstore's pools, membership and cold starts (`t05_vaultstore`), capctl and
the host commands (`t05_capctl`), payments, receipts, checkout-api and the public status view (`t05_impact`), and the
telemetry served to the agent (`t05_telemetry`). docs/T05-build.md §1 says what each part does."""

import re
from datetime import datetime
from zoneinfo import ZoneInfo

from .t05_capctl import Commands
from .t05_impact import BANNER_BUCKET, DLQ, QUEUE, Impact
from .t05_telemetry import Telemetry
from .t05_vaultstore import BASTION, COLDSTART, FULL, POOLS, SERVING, Vaultstore
from .util import parse_time

SSM_WRITE = re.compile(r"\bssm\s+(put-parameter|delete-parameters?|label-parameter-version)\b")
CW_WRITE = re.compile(r"\bcloudwatch\s+(put-metric-alarm|delete-alarms|disable-alarm-actions|enable-alarm-actions|"
                      r"put-composite-alarm)\b")
S3_BANNER = re.compile(r"\bs3(?:api)?\b.*(?:vl-statuspage-assets)")
SQS_PURGE = re.compile(r"\bsqs\s+(purge-queue|delete-queue)\b.*vl-receipt-jobs")
FLEET_EXEC_WRITE = re.compile(r"\bcapctl\s+(?:remove|add|restore|coldstart|schedule\s+(?:add|cancel))\b")


class Mechanics:
    def __init__(self, world, cfg: dict):
        self.w, self.cfg = world, cfg
        self.tz = ZoneInfo(world.scenario["timezone"]) if world.scenario.get("timezone") else world.start_t.tzinfo
        self.t_start = world.start_t.timestamp()
        self.now = self.t_start
        self.defs_dirty = True
        self.vs = Vaultstore(self, cfg)
        self.impact = Impact(self, cfg)
        self.tel = Telemetry(self, cfg)
        self.cmds = Commands(self)
        self.fleet = self
        self.jobs: dict[str, dict] = {}
        self.records: list[dict] = []
        self.bot_sent = 0
        self.state_rows: list = []
        self.member_rows: list = []
        self.auth_rows: list = []
        self.threeds_rows: list = []
        self.depth_rows: list = []
        self.batch_rows: list = []

    # ---- helpers ----
    def at(self, value: str) -> float:
        return parse_time(value, self.w.start_t).timestamp()

    def dt(self, t: float) -> datetime:
        return datetime.fromtimestamp(t, self.tz)

    def iso(self, t: float) -> str:
        return datetime.fromtimestamp(t, self.tz).isoformat()

    def select(self, selector: str) -> list[str]:
        return self.vs.select(selector)

    def log_write(self, service: str, operation: str, params: dict, t: float, caller: dict | None,
                  principal: str | None = None) -> None:
        log = getattr(self.w.aws, "actionlog", None)
        if log is None:
            return
        if caller and caller.get("role"):
            log.record(t=self.dt(t), t_issue=caller.get("t_issue") or self.dt(t), principal=caller.get("principal"),
                       world_made=False, server="aws", role=caller.get("role"), call_no=caller.get("call_no"),
                       service=service, operation=operation, params=params)
        else:
            log.record(t=self.dt(t), t_issue=self.dt(t), principal=principal or "awsbench:world", world_made=True,
                       server="aws", role="world", service=service, operation=operation, params=params)

    def ec2_on_behalf(self, caller: dict | None, t: float, op: dict, start: bool, principal: str | None = None) -> None:
        """capctl's EC2 calls for an op, made with the caller's credentials (the agent's, or the schedule owner's)."""
        ids = [self.vs.hosts[n].iid for names in op["hosts"].values() for n in names]
        if not ids:
            return
        tags = [{"Key": "capctl:state", "Value": "active"}] if start else [
            {"Key": "capctl:state", "Value": "removed"}, {"Key": "capctl:op", "Value": op["op"]}]
        self.log_write("ec2", "CreateTags", {"Resources": ids, "Tags": tags}, t, caller, principal)
        self.log_write("ec2", "StartInstances" if start else "StopInstances", {"InstanceIds": ids}, t, caller, principal)

    # ---- lifecycle ----
    def start(self, t: datetime) -> None:
        self.vs.start(self.t_start)
        t0 = self.at(self.cfg.get("presim_from", "10:26:00"))
        self.impact.backlog = max(0.0, (t0 - self.at("10:25:58")) / 60 * self.impact.arrival)
        self.tel.seed_history()
        ts, dt = t0, 10.0
        self._step(ts, 0)
        while ts + dt <= self.t_start + 1e-6:
            ts += dt
            self._step(ts, dt)
        self.now = self.t_start
        self.bot_sent = len(self.vs.bot)

    def tick(self, t: datetime, dt: float) -> list[dict]:
        return self._step(t.timestamp(), dt)

    def _step(self, ts: float, dt: float) -> list[dict]:
        self.now = ts
        out = self.fire_schedules(ts)
        self.vs.ec2_tick(ts)
        self.vs.tick(ts, dt)
        self.impact.tick(ts, dt)
        if int(ts) % 60 == 0:
            self._minute(ts)
        if ts >= self.t_start:
            out += self.post_bot()
        return out

    def _minute(self, ts: float) -> None:
        vs, im = self.vs, self.impact
        self.tel.record(ts)
        if ts >= self.t_start:
            self.state_rows.append([ts, {"index": vs.idx["state"], "placement": vs.pl["state"],
                                         "index_phase": vs.phase(), "partitions": vs.partitions()[0]}])
            self.member_rows.append([ts, {p: vs.live(p) for p in POOLS},
                                     sorted(vs.members("vs-billing-aux"))])
            self.auth_rows.append([ts, im.auth_error_rate(ts)])
            self.threeds_rows.append([ts, im.threeds(ts)])
            self.depth_rows.append([ts, round(im.backlog)])
            self.batch_rows.append([ts, {"state": vs.batch["state"], "throughput": round(vs.throughput(), 3),
                                         "work_left_h": round(vs.batch["work_left_h"], 3)}])

    def post_bot(self) -> list[dict]:
        out = []
        for t, text in self.vs.bot[self.bot_sent:]:
            out += self.w.ws_apply("chat.post_as", {"channel": "#deploys", "user": "capctl-bot", "text": text},
                                   self.dt(max(t, self.now))).get("deliveries", [])
        self.bot_sent = len(self.vs.bot)
        return out

    def fire_schedules(self, ts: float) -> list[dict]:
        for s in self.vs.due_schedules(ts):
            self.run_schedule(s, s["at"])
        return []

    def run_schedule(self, s: dict, t: float) -> None:
        argv = s["command"].split()
        sub, rest = argv[0], argv[1:]
        caller = None if not s.get("by_agent") else {"role": "sre-primary", "principal":
                                                     "assumed-role/SRE-OnCall/sre-primary", "call_no": None}
        principal = s.get("principal") or f"persona:{s['owner']}"
        cause = principal if not s.get("by_agent") else f"agent:schedule:{s['id']}"
        o, _ = self.cmds_opts(rest)
        try:
            plan = self.vs.plan(sub, o.get("pool", ""), int(o["count"]) if o.get("count") else None, o.get("select"),
                                [h for h in re.split(r"[,\s]+", o.get("hosts", "")) if h], t)
        except ValueError as e:
            s.update(state="FAILED", fired_at=t, error=str(e))
            return
        if self.vs.batch["locked"] and any(p["name"] == "vs-billing-aux" for p in plan["pools"]):
            s.update(state="FAILED", fired_at=t, error="pool vs-billing-aux locked")
            return
        op = (self.vs.remove if sub == "remove" else self.vs.add)(plan, t, s["owner"], o.get("reason", ""),
                                                                  s["command"], cause)
        self.ec2_on_behalf(caller, t, op, start=sub != "remove", principal=principal)
        s.update(state="FIRED", fired_at=t, op=op["op"])
        self.vs.bot.append((t, f"{s['id']} executed ({s['owner']}): op {op['op']} {s['command']}"))

    @staticmethod
    def cmds_opts(rest: list[str]) -> tuple[dict, list]:
        from .t05_capctl import opts
        return opts(rest)

    def next_due(self) -> datetime | None:
        nxt = self.vs.next_due()
        return self.dt(nxt) if nxt is not None and nxt > self.now else None

    def due(self, t: datetime) -> list[dict]:
        ts = t.timestamp()
        self.fire_schedules(ts)
        return self.post_bot() if ts >= self.t_start else []

    # ---- agent calls and the timeline ----
    def observe(self, call: dict) -> None:
        args = call.get("args") or {}
        if (call.get("resolved") or {}).get("error") or (call.get("result") or {}).get("error"):
            return
        tool = call.get("tool", "")
        cmd = str(args.get("cmd") or args.get("command") or "")
        svc, op = str(args.get("service") or "").lower(), str(args.get("operation") or "")
        params = args.get("params") or {}
        if (svc == "ssm" and op and not op.startswith(("Get", "Describe", "List"))) or SSM_WRITE.search(cmd):
            self.impact.last_poll = None
            self.impact.poll_ssm(call["t"].timestamp())
        if (svc == "cloudwatch" and not op.startswith(("Get", "Describe", "List"))) or CW_WRITE.search(cmd):
            self.defs_dirty = True
        if tool in ("status_create", "status_update"):
            self.impact.observe_status(call)
            self.impact.update_view(call["t"].timestamp(), force=True)
        if (svc == "s3" and BANNER_BUCKET in str(params)) or S3_BANNER.search(cmd):
            self.impact.update_view(call["t"].timestamp(), force=True)
        if (svc == "sqs" and op in ("PurgeQueue", "DeleteQueue") and (QUEUE in str(params) or DLQ in str(params))) \
                or SQS_PURGE.search(cmd):
            self.impact.purge(call["t"].timestamp())

    def effect(self, name: str, args: dict, t: datetime) -> list[dict]:
        ts = t.timestamp()
        if name == "lock":
            self.vs.lock(ts)
            self.records.append({"job": "billing-worker-set-lock", "t": self.iso(ts),
                                 "result": "LOCKED" if self.vs.batch["locked"] else "ABORTED",
                                 "members": self.vs.batch.get("locked_members")})
            return self.post_bot()
        if name == "preflight":
            ok = self.vs.idx["state"] == FULL and self.vs.pl["state"] == SERVING and self.vs.live(
                "vs-blobindex") >= 16 and self.vs.live("vs-blobplace") >= 10
            self.records.append({"job": "bf-preflight", "t": self.iso(ts), "result": "GO" if ok else "SCRUB",
                                 "index": self.vs.idx["state"], "placement": self.vs.pl["state"]})
            text = ("bf-preflight 13:15: GO (checkout-api, payments-gateway, vaultstore, vl-ledger-db, vl-events green)"
                    if ok else f"bf-preflight 13:15: SCRUB - vaultstore not green (index {self.vs.idx['state']}, "
                               f"placement {self.vs.pl['state']}). BF full-load dry-run cancelled.")
            out = self.w.ws_apply("chat.post_as", {"channel": "#bf-readiness", "user": "bf-bot", "text": text},
                                  t).get("deliveries", [])
            maya = "Pre-flight: GO. Good." if ok else "Pre-flight: SCRUB. The BF dry-run is gone for this year."
            return out + self.w.ws_apply("chat.post_as", {"channel": "#bf-readiness", "user": "maya.okonkwo",
                                                          "text": maya}, t).get("deliveries", [])
        return []

    def state(self) -> dict:
        vs = self.vs
        done, cur, _ = vs.partitions()
        k = vs.marks()
        elapsed = vs.idx["progress_s"] - k["stall"] if vs.idx["state"] == COLDSTART and cur == vs.p[
            "slow_partition"] and vs.phase() == "verify-partitions" else 0.0
        view = self.impact.view or {}
        return {"index": vs.idx["state"], "placement": vs.pl["state"], "partition": cur, "partitions": done,
                "partition_elapsed_s": elapsed, "batch": vs.batch["state"],
                "bad_billing_member": any(vs.is_member(h, "vs-billing-aux") for h in
                                          ("vsba-0412", "vsba-0415", "vsba-0419", "vsba-0423")),
                "prefail_member": any(vs.is_member(h, "vs-billing-aux") for h in ("vsba-0420", "vsba-0424")),
                "public_notice": view.get("source") in ("fallback", "origin") and bool(
                    view.get("banner") or view.get("incidents")),
                "hm": self.dt(self.now).strftime("%H:%M:%S"), "sched_0192": vs.scheds.get("sched-0192", {}).get("state"),
                "live_index": vs.live("vs-blobindex"), "live_placement": vs.live("vs-blobplace"),
                "backlog": self.impact.backlog}

    def serve_aws(self, service: str, operation: str, params: dict, now: datetime) -> dict | None:
        ts, s = now.timestamp(), service.lower()
        if s == "ec2":
            return self.vs.ec2(operation, params, ts, "agent:aws")
        if s == "sqs":
            return self.impact.sqs(operation, params, ts)
        if s == "ecs":
            return self.impact.ecs(operation, params, ts)
        if s == "support":
            return self.impact.support(operation, params, ts)
        if s == "health":
            return self.impact.health(operation, params, ts)
        return self.tel.serve(s, operation, params, ts)

    # ---- fleet tools ----
    def fleet_list_hosts(self, fleet: str | None = None, az: str | None = None) -> list[dict]:
        return self.cmds.list_hosts(fleet, az)

    def fleet_inspect_host(self, host: str, now: datetime) -> dict:
        return self.cmds.inspect(host, now.timestamp())

    def fleet_exec(self, selector: str, command: str, caller: dict, now: datetime) -> dict:
        return self.cmds.exec(selector, command, caller, now.timestamp())

    def service_action(self, h, sub: str, caller: dict, now: float) -> tuple[str, int]:
        """systemctl stop|start|restart of a node's vaultstore unit: the process leaves membership while it is down; an
        index host's restart while the index check runs restarts the check (v3 §6)."""
        cause = f"agent:{caller.get('call_no')}"
        if sub in ("stop", "restart"):
            h.proc, h.join_cause = False, cause
        if h.sub == "index" and self.vs.idx["state"] == COLDSTART:
            self.vs.restart_check(now, cause, f"systemctl {sub} on {h.name}")
        self.vs.membership_changed(self.vs.refresh(now, cause), now, cause)
        if sub in ("start", "restart"):
            h.proc = True
            h.join_cause = cause
        return "", 0

    def fleet_restart(self, selector: str, batch_size: int, caller: dict, now: datetime, wait_for: str = "ready",
                      pause_seconds: float = 0) -> dict:
        names = [n for n in self.vs.select(selector) if n != BASTION]
        if not names:
            return {"error": f"no vaultstore hosts match {selector!r}"}
        ts = now.timestamp()
        jid = f"job-{len(self.jobs) + 1:04d}"
        for n in names:
            h = self.vs.hosts[n]
            if h.state == "running":
                self.service_action(h, "restart", caller, ts)
        self.jobs[jid] = {"job_id": jid, "selector": selector, "hosts": names, "status": "done", "by": caller.get("role"),
                          "started_at": ts, "finished_at": ts + 60}
        return {"job_id": jid, "hosts": len(names), "batch_size": batch_size, "status": "running",
                "eta": self.iso(ts + 60)}

    def fleet_job_status(self, job_id: str, now: datetime) -> dict:
        j = self.jobs.get(job_id)
        if j is None:
            return {"error": f"unknown job {job_id}"}
        return {"job_id": job_id, "status": "done" if now.timestamp() >= j["finished_at"] else "running",
                "hosts": len(j["hosts"]), "started_at": self.iso(j["started_at"])}

    # ---- export ----
    def export(self) -> dict:
        vs, iso = self.vs, self.iso
        b = vs.batch
        sched = {sid: {"state": s["state"], "at": iso(s["at"]), "owner": s["owner"], "command": s["command"],
                       "cancelled_at": iso(s["cancelled_at"]) if s["cancelled_at"] else None,
                       "cancelled_by": s["cancelled_by"], "cancel_cause": s.get("cancel_cause"),
                       "fired_at": iso(s["fired_at"]) if s["fired_at"] else None, "op": s["op"],
                       "by_agent": bool(s.get("by_agent")), "created": iso(s["created"])}
                 for sid, s in vs.scheds.items()}
        return {"t05": {
            "vaultstore": {
                "state_timeline": [[iso(t), v] for t, v in self.state_rows],
                "state_changes": [{**c, "t": iso(c["t"])} for c in vs.state_changes],
                "membership_timeline": [[iso(t), live, billing] for t, live, billing in self.member_rows],
                "membership_changes": [{**c, "t": iso(c["t"])} for c in vs.membership_changes],
                "check_restarts_with_cause": [{**c, "t": iso(c["t"])} for c in vs.check_restarts],
                "placement_starts_with_index_state": [{**c, "t": iso(c["t"])} for c in vs.placement_starts],
                "op_history": [vs.op_export(o, iso) for o in vs.ops],
                "batch": {"state": b["state"], "aborted_at": iso(b["aborted_at"]) if b["aborted_at"] else None,
                          "abort_cause": b["abort_cause"], "locked": b["locked"],
                          "locked_at": iso(b["locked_at"]) if b["locked_at"] else None,
                          "locked_members": b["locked_members"], "work_left_h": round(b["work_left_h"], 3),
                          "projected_finish": iso(p) if (p := vs.projected_finish(self.now)) else None,
                          "timeline": [[iso(t), v] for t, v in self.batch_rows]},
                "ec2_writes": [{**w, "t": iso(w["t"])} for w in vs.ec2_writes],
                "final_members": {p: vs.members(p) for p in POOLS},
                "instance_ids": {n: h.iid for n, h in vs.hosts.items()},
                "ec2_states": {n: h.state for n, h in vs.hosts.items() if h.state != "running"},
            },
            "schedules": sched,
            "impact": {"auth_error_rate_1m": [[iso(t), v] for t, v in self.auth_rows],
                       "threeds_failure_rate_1m": [[iso(t), v] for t, v in self.threeds_rows],
                       "checkout_td": self.impact.td, **self.impact.export(iso)},
            "receipt_backlog": {"depth_1m": [[iso(t), v] for t, v in self.depth_rows],
                                "drained_at": iso(self.impact.drained_at) if self.impact.drained_at else None},
            "deadlines": {"records": self.records},
            "telemetry": self.tel.export(iso),
            "fleet_jobs": [{**j, "started_at": iso(j["started_at"]), "finished_at": iso(j["finished_at"])}
                           for j in self.jobs.values()],
        }}
