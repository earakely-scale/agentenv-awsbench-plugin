"""The World: ticks the physics, runs the timeline and NPCs, serves the fleet and physics-owned AWS reads."""

from datetime import datetime, timedelta

from .archive import PlanArchive
from .asg import IngressAsg, LedgerWriterAsg, serve_groups
from .ecs import Ecs
from .endpointd import COMPONENTS, Endpointd, host_of
from .epctl import Epctl
from .fleet import Fleet
from .mirror import AwsMirror
from .physics import Dns, Storm, checkout_success
from .telemetry import Telemetry
from .timeline import Timeline
from .util import iso, parse_time

READ_PREFIXES = ("Describe", "Get", "List", "Lookup", "Test", "Filter", "Scan", "Query", "Search", "BatchGet",
                 "StartQuery", "Head")
WAKE_KINDS = {"dm", "mention", "page", "email", "job_done"}
LEVERS = {
    "max_concurrency": ("/vl/prod/webhooks-dispatcher/max_concurrency", 25.0),
    "consumer_paused": ("/vl/prod/webhooks-dispatcher/consumer_paused", "false"),
    "admission": ("/vl/prod/ledger-writer/admission/enabled", "false"),
    "dispatcher_quota": ("/vl/prod/ledger-writer/admission/quota/webhooks-dispatcher", 300.0),
    "replay_rate": ("/vl/prod/checkout-api/outbox/replay_rate", 400.0),
}


class World:
    def __init__(self, scenario: dict, seed: str, aws, ws):
        self.scenario = scenario
        self.seed = str(seed)
        self.mode = scenario.get("mode", "eval")
        self.aws = aws
        self.ws = ws
        self.start_t = datetime.fromisoformat(scenario["start"])
        self.horizon = datetime.fromisoformat(scenario.get("horizon", scenario["window_end"]))
        self.tick_s = float(scenario.get("tick_s", 10))
        self.now = self.start_t
        w = scenario["world"]
        ep = w["endpointd"]
        self.mirror = AwsMirror(aws, ep.get("zone", "internal.vantleaf.io"), ep.get("table", "endpointd-plans"),
                                ["/platform/endpointd/prod"], [p for p, _ in LEVERS.values()])
        self.fleet = Fleet(self, w["fleets"])
        self.endpointd = Endpointd(ep, self.mirror, self.fleet.ingress, self.start_t)
        self.epctl = Epctl(self.endpointd, self)
        self.archive = PlanArchive(self.endpointd)
        self.dns = Dns(w["dns"], self.start_t, self.seed, self.mode)
        self.dns.live_fn = self.mirror.live
        self.storm = Storm(w["ledger"], self.start_t)
        self.traffic_cfg = w.get("traffic", {})
        self.core_hosts = [h["name"] for h in self.fleet.hosts.values() if h["fleet"] == self.traffic_cfg.get(
            "fleet", "vl-core")]
        self.timeline = Timeline(self, scenario.get("timeline", []), scenario.get("npcs", {}),
                                 scenario.get("identities", {}).get("sre-primary", {}))
        self.telemetry = Telemetry(self, w.get("telemetry", {}))
        self.asg = IngressAsg(self, w["ingress_asg"]) if w.get("ingress_asg") else None
        self.ecs = Ecs(self, w["ecs"]) if w.get("ecs") else None
        self.lw_asg = LedgerWriterAsg(self, w["ecs"]["ledger_writer_asg"]) if self.ecs else None
        self.checkout_ticks: list[tuple[datetime, float]] = []
        self.checkout_1m: list[list] = []
        self.checkout_now = 0.0
        self.inbox: list[dict] = []
        self.world_flags: dict = {}
        self._ledger_sig = None
        self._next_tick = self.start_t
        self.ws_errors: list[dict] = []

    # ---- lifecycle ----
    def start(self) -> None:
        self.mirror.sync()
        self.on_records_changed(self.start_t)
        self.timeline.arm(self.start_t, self.horizon)
        self._tick(self.start_t)
        self.telemetry.backfill(self.start_t)
        self._next_tick = self.start_t + timedelta(seconds=self.tick_s)

    def next_due(self) -> datetime | None:
        cands = [self.timeline.next_due(), self.fleet.next_due()]
        cands = [c for c in cands if c is not None]
        return min(cands) if cands else None

    def advance_to(self, t: datetime, wake: set[str] | None = None) -> dict:
        t = t.astimezone(self.start_t.tzinfo)
        woke, fired, delivered = [], [], []
        while self.now < t:
            nd = self.next_due()
            step = min(x for x in (t, self._next_tick, nd) if x is not None)
            step = max(step, self.now)
            self.now = step
            if step >= self._next_tick:
                self._tick(step)
                self._next_tick = step + timedelta(seconds=self.tick_s)
                delivered += self.timeline.check_state(step)
            for item in self.timeline.pop_due(step):
                delivered += self.timeline.run(item, step)
                fired.append(item.ev)
            delivered += self.fleet.complete_jobs(step)
            if wake and any(d.get("kind") in wake for d in delivered):
                woke = [d for d in delivered if d.get("kind") in wake]
                break
        self.inbox += delivered
        return {"now": self.now, "woke": woke, "fired": fired, "delivered": delivered}

    def observe(self, call: dict) -> None:
        """An agent call at call["t"]. Its `ops` are the AWS operations it made ({service, operation, params, error}),
        as the meter reads them from the action log; only the ones without an error move the world."""
        call = dict(call)
        call["t"] = self._time(call.get("t") or self.now)
        call.setdefault("args", {})
        if self._writes_aws(call):
            self.mirror.sync()
            self.on_records_changed(call["t"])
        call["effects"] = [e for op in call.get("ops") or [] if not op.get("error")
                           for e in self.storm_levers_from_call(op, call["t"])]
        self.timeline.observe(call)

    def _time(self, t) -> datetime:
        return t if isinstance(t, datetime) else parse_time(t, self.start_t)

    def _writes_aws(self, call: dict) -> bool:
        if call.get("server") == "workspace" or str(call.get("tool", "")).split("_")[0] in (
                "chat", "tickets", "wiki", "repo", "mail", "pager", "status"):
            return False
        if call.get("ops") is not None and call.get("tool") in ("aws_cli", "aws_api"):
            return any(not o.get("error") and not str(o.get("operation")).startswith(READ_PREFIXES)
                       for o in call["ops"])
        op = call.get("operation") or ""
        if op:
            return not op.startswith(READ_PREFIXES)
        cmd = str((call.get("args") or {}).get("cmd") or (call.get("args") or {}).get("command") or "")
        return call.get("tool") in ("aws_cli", "aws_api") and not any(
            f" {w}" in f" {cmd}".replace("-", "") for w in ("describe", "get", "list", "lookup", "scan", "query"))

    # ---- physics ----
    def levers(self) -> dict:
        out = {}
        for k, (name, default) in LEVERS.items():
            raw = self.mirror.ssm.get(name)
            if isinstance(default, float):
                try:
                    out[k] = float(raw) if raw is not None else default
                except ValueError:
                    out[k] = default
            else:
                out[k] = str(raw if raw is not None else default).lower() == "true"
        return out

    def on_records_changed(self, t: datetime) -> None:
        live = self.mirror.live(self.endpointd.ledger)
        sig = tuple(sorted((r["Value"], r["Weight"]) for r in live))
        if sig != self._ledger_sig:
            self._ledger_sig = sig
            self.dns.record_change(t, any(r["Weight"] > 0 for r in live))

    def bad_answer_share(self) -> float:
        live = [r for r in self.mirror.live(self.endpointd.ledger) if r["Weight"] > 0]
        if not live:
            return 0.0
        ready = {i["ip"] for i in self.fleet.ingress() if i.get("state") == "running" and i.get("ready")}
        total = sum(r["Weight"] for r in live)
        return sum(r["Weight"] for r in live if r["Value"] not in ready) / total

    def _tick(self, t: datetime) -> None:
        self.endpointd.tick(t)
        self.dns.update_all(t)
        rec = [self.dns.resolves(h, t) for h in self.core_hosts]
        rs = sum(rec) / len(rec) if rec else 0.0
        self.storm.tick(t, self.tick_s, rs, self.levers())
        self.checkout_now = checkout_success(rec, self.storm.regime, self.bad_answer_share(), self.traffic_cfg)
        self.checkout_ticks.append((t, self.checkout_now))
        cutoff = t - timedelta(seconds=60)
        self.checkout_ticks = [(x, v) for x, v in self.checkout_ticks if x > cutoff]
        if t.second < self.tick_s:
            avg = sum(v for _, v in self.checkout_ticks) / len(self.checkout_ticks)
            self.checkout_1m.append([iso(t.replace(second=0, microsecond=0)), round(avg, 4)])
        self.storm.series.append({"t": iso(t), "rs": round(rs, 3), "offered": round(self.storm.offered, 1),
                                  "capacity": self.storm.capacity, "regime": self.storm.regime,
                                  "backlog": round(self.storm.backlog), "checkout": round(self.checkout_now, 4)}) \
            if t.second < self.tick_s else None
        self.telemetry.tick(t)

    def checkout_success_1m(self) -> float:
        if not self.checkout_ticks:
            return self.checkout_now
        return sum(v for _, v in self.checkout_ticks) / len(self.checkout_ticks)

    def eval_state(self, expr: str) -> bool:
        ns = {"regime": self.storm.regime, "checkout_success_1m": self.checkout_success_1m(),
              "backlog": self.storm.backlog, "visible": self.storm.backlog, "outbox": self.storm.outbox,
              "ledger_correct": self.endpointd.correct(self.endpointd.ledger),
              "alarm": self.telemetry.alarm_state, "flag": lambda k: bool(self.world_flags.get(k))}
        return bool(eval(expr, {"__builtins__": {}}, ns))

    def storm_levers_from_call(self, op: dict, t: datetime) -> list[str]:
        """ECS and Application Auto Scaling writes the world serves and physics owns; returns the effects."""
        return self.ecs.apply(op, t) if self.ecs is not None else []

    def world_effect(self, name: str, args: dict, t: datetime) -> None:
        self.world_flags[name] = iso(t)
        self.telemetry.extra_logs.append((t, args.get("log_group", "/vl/platform/acme-renewer"),
                                          args.get("message", "INFO run complete: 2,412/2,412")))

    def ws_apply(self, op: str, args: dict, t: datetime) -> dict:
        try:
            return self.ws.apply(op, args, t) or {}
        except Exception as e:  # noqa: BLE001 - a failed delivery must not stop the world
            self.ws_errors.append({"t": iso(t), "op": op, "error": str(e)})
            return {}

    # ---- fleet ----
    def fleet_list_hosts(self, fleet: str | None = None, az: str | None = None) -> list[dict]:
        return self.fleet.list_hosts(fleet, az)

    def fleet_inspect_host(self, host: str) -> dict:
        return self.fleet.inspect(host, self.now)

    def fleet_exec(self, selector: str, command: str, caller: dict) -> dict:
        return self.fleet.exec(selector, command, caller, self.now)

    def fleet_restart(self, selector: str, batch_size: int, caller: dict) -> dict:
        return self.fleet.restart(selector, batch_size, caller, self.now)

    def fleet_job_status(self, job_id: str) -> dict:
        return self.fleet.job_status(job_id, self.now)

    def set_process(self, component: str, running: bool, t: datetime) -> None:
        self.endpointd.processes[component] = running

    # ---- AWS reads served by physics ----
    def serve_aws(self, service: str, operation: str, params: dict) -> dict | None:
        if service.lower() == "autoscaling":
            return serve_groups([g for g in (self.asg, self.lw_asg) if g], operation, params or {}, self.now)
        if self.ecs is not None and (out := self.ecs.serve(service.lower(), operation, params or {},
                                                           self.now)) is not None:
            return out
        if service.lower() == "s3":
            return self.archive.serve(operation, params or {}, self.now)
        return self.telemetry.serve(service, operation, params or {}, self.now)

    # ---- exports ----
    def export(self) -> dict:
        ep = self.endpointd.export()
        return {
            "endpointd": ep,
            "dns": self.dns.export(),
            "ledger": self.storm.export(),
            "traffic": {"checkout_success_1m": self.checkout_1m},
            "timeline": self.timeline.export(),
            "approvals": self.timeline.approvals,
            "fleet_jobs": self.fleet.export_jobs(),
            "world_flags": self.world_flags,
            "ingress_asg": self.asg.export(self.now) if self.asg else None,
            "ecs": self.ecs.export(self.now) if self.ecs else None,
            "workspace_errors": self.ws_errors,
            "components": {c: {"host": host_of(c), "switch": self.endpointd.switch(c),
                               "process": self.endpointd.processes[c]} for c in COMPONENTS},
        }
