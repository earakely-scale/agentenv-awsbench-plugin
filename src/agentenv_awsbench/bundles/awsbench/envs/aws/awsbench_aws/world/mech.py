"""A world whose physics a scenario picks: `world.mechanics: <name>` runs `awsbench_aws.world.<name>.Mechanics` under
the shared clock loop, timeline and NPCs. T01's `World` stays the default (see `make_world`).

A Mechanics class is built as `Mechanics(world, cfg)` (`cfg` = the scenario's `world` table) and provides:
  start(t) -> None                                  seed-time state (presimulation up to the scenario start)
  tick(t, dt) -> list[inbound item]                 one physics tick of `dt` seconds ending at `t`
  next_due() -> datetime | None                     its next event between ticks (a restart step, a job batch)
  due(t) -> list[inbound item]                      run everything due at or before `t`; never leaves it due
  observe(call) -> None                             an agent call record (see World.observe), before the timeline
  effect(name, args, t) -> list[inbound item]       a timeline `world:` action
  state() -> dict                                   names for timeline `state`/`unless_state` expressions
  serve_aws(service, operation, params, now) -> dict | None
  fleet_list_hosts(fleet, az) / fleet_inspect_host(host, now) / fleet_exec(selector, command, caller, now) /
  fleet_restart(selector, batch_size, caller, now, wait_for, pause_seconds) / fleet_job_status(job_id, now)
  export() -> dict                                  merged into the world export
and may expose `fleet` with `select(selector) -> list[str]`, which the meter uses to count a command's hosts.
"""

from datetime import datetime, timedelta

from .timeline import Timeline
from .util import parse_time

WAKE_KINDS = {"dm", "mention", "page", "email", "job_done"}


class MechWorld:
    def __init__(self, scenario: dict, seed: str, aws, ws, mechanics):
        self.scenario = scenario
        self.seed = str(seed)
        self.mode = scenario.get("mode", "eval")
        self.aws = aws
        self.ws = ws
        self.start_t = datetime.fromisoformat(scenario["start"])
        self.horizon = datetime.fromisoformat(scenario.get("horizon", scenario["window_end"]))
        self.tick_s = float(scenario.get("tick_s", 10))
        self.now = self.start_t
        self.inbox: list[dict] = []
        self.world_flags: dict = {}
        self.ws_errors: list[dict] = []
        self._effects: list[dict] = []
        self._next_tick = self.start_t
        self.timeline = Timeline(self, scenario.get("timeline", []), scenario.get("npcs", {}),
                                 scenario.get("identities", {}).get("sre-primary", {}))
        self.mech = mechanics(self, scenario.get("world") or {})
        self.fleet = getattr(self.mech, "fleet", None)

    # ---- lifecycle ----
    def start(self) -> None:
        self.mech.start(self.start_t)
        self.timeline.arm(self.start_t, self.horizon)
        self._next_tick = self.start_t + timedelta(seconds=self.tick_s)

    def next_due(self) -> datetime | None:
        cands = [c for c in (self.timeline.next_due(), self.mech.next_due()) if c is not None]
        return min(cands) if cands else None

    def advance_to(self, t: datetime, wake: set[str] | None = None) -> dict:
        t = t.astimezone(self.start_t.tzinfo)
        woke, fired, delivered = [], [], []
        while self.now < t:
            step = max(min(x for x in (t, self._next_tick, self.next_due()) if x is not None), self.now)
            self.now = step
            if step >= self._next_tick:
                delivered += self.mech.tick(step, self.tick_s) or []
                self._next_tick = step + timedelta(seconds=self.tick_s)
                delivered += self.timeline.check_state(step)
            for item in self.timeline.pop_due(step):
                delivered += self.timeline.run(item, step)
                fired.append(item.ev)
            delivered += self.mech.due(step) or []
            delivered, self._effects = delivered + self._effects, []
            if wake and any(d.get("kind") in wake for d in delivered):
                woke = [d for d in delivered if d.get("kind") in wake]
                break
        self.inbox += delivered
        return {"now": self.now, "woke": woke, "fired": fired, "delivered": delivered}

    def observe(self, call: dict) -> None:
        call = dict(call)
        t = call.get("t") or self.now
        call["t"] = t if isinstance(t, datetime) else parse_time(t, self.start_t)
        call.setdefault("args", {})
        self.mech.observe(call)
        self.timeline.observe(call)

    # ---- what the timeline needs ----
    def eval_state(self, expr: str) -> bool:
        ns = {**self.mech.state(), "flag": lambda k: bool(self.world_flags.get(k))}
        return bool(eval(expr, {"__builtins__": {}}, ns))

    def world_effect(self, name: str, args: dict, t: datetime) -> None:
        self._effects += self.mech.effect(name, args, t) or []

    def ws_apply(self, op: str, args: dict, t: datetime) -> dict:
        try:
            return self.ws.apply(op, args, t) or {}
        except Exception as e:  # noqa: BLE001 - a failed delivery must not stop the world
            self.ws_errors.append({"t": t.isoformat(), "op": op, "error": str(e)})
            return {}

    # ---- fleet and AWS reads ----
    def fleet_list_hosts(self, fleet: str | None = None, az: str | None = None) -> list[dict]:
        return self.mech.fleet_list_hosts(fleet, az)

    def fleet_inspect_host(self, host: str) -> dict:
        return self.mech.fleet_inspect_host(host, self.now)

    def fleet_exec(self, selector: str, command: str, caller: dict) -> dict:
        return self.mech.fleet_exec(selector, command, caller, self.now)

    def fleet_restart(self, selector: str, batch_size: int, caller: dict, wait_for: str = "ready",
                      pause_seconds: float = 0) -> dict:
        return self.mech.fleet_restart(selector, batch_size, caller, self.now, wait_for, pause_seconds)

    def fleet_job_status(self, job_id: str) -> dict:
        return self.mech.fleet_job_status(job_id, self.now)

    @property
    def wants_region(self) -> bool:
        """Mechanics that serve more than one region (T04's replica) take the call's region."""
        return bool(getattr(self.mech, "wants_region", False))

    def serve_aws(self, service: str, operation: str, params: dict, region: str | None = None) -> dict | None:
        if self.wants_region:
            return self.mech.serve_aws(service, operation, params or {}, self.now, region=region)
        return self.mech.serve_aws(service, operation, params or {}, self.now)

    def export(self) -> dict:
        return {**self.mech.export(), "timeline": self.timeline.export(), "approvals": self.timeline.approvals,
                "world_flags": self.world_flags, "workspace_errors": self.ws_errors}
