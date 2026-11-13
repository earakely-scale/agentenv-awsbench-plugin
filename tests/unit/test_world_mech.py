"""`make_world`: T01 keeps its `World`; `world.mechanics` runs a scenario's own Mechanics under the shared clock loop."""

from datetime import datetime, timedelta

import pytest
from awsbench_aws.world import MechWorld, World, load_scenario, make_world


class NoAws:
    def call(self, service, operation, params, *, principal, world_made):
        return {}


class Ws:
    def __init__(self):
        self.ops = []

    def apply(self, op, args, at):
        self.ops.append((op, args, at))
        return {"ok": True, "id": f"r{len(self.ops)}", "deliveries": [{"kind": "dm", "t": at.isoformat()}]
                if op == "chat.dm_as" else []}


def scenario(**world):
    return {"id": "x", "start": "2026-11-16T08:15:00-05:00", "window_end": "2026-11-16T13:00:00-05:00",
            "horizon": "2026-11-16T16:15:00-05:00", "tick_s": 10, "world": world, "timeline": [], "npcs": {}}


def test_t01_keeps_its_world():
    s = load_scenario("t01")
    assert not (s.get("world") or {}).get("mechanics")
    world = make_world(s, "0", NoAws(), Ws())
    assert type(world) is World


def test_mechanics_name_is_checked():
    with pytest.raises(ValueError):
        make_world(scenario(mechanics="../evil"), "0", NoAws(), Ws())
    with pytest.raises(ModuleNotFoundError):
        make_world(scenario(mechanics="nope"), "0", NoAws(), Ws())


def test_t03_runs_under_the_clock_loop():
    world = make_world(scenario(mechanics="t03"), "1101", NoAws(), Ws())
    assert isinstance(world, MechWorld)
    world.start()
    out = world.advance_to(world.start_t + timedelta(minutes=5))
    assert out["now"] == world.start_t + timedelta(minutes=5)
    assert len(world.fleet_list_hosts("streamroute")) == 132
    assert world.fleet_exec("sr-a-001", "srctl fleet status", {"role": "sre-primary"})["hosts"][0]["exit_code"] == 0
    assert world.serve_aws("autoscaling", "DescribeAutoScalingGroups", {})["AutoScalingGroups"]
    ex = world.export()
    assert {"t03", "timeline", "approvals", "world_flags", "workspace_errors"} <= set(ex)


class Ticker:
    """A Mechanics that counts ticks, runs one due event, answers state and turns an effect into a wake item."""

    def __init__(self, world, cfg):
        self.w, self.ticks, self.due_at, self.ran, self.seen = world, 0, world.start_t + timedelta(seconds=25), [], []

    def start(self, t):
        self.started = t

    def tick(self, t, dt):
        self.ticks += 1
        return []

    def next_due(self):
        return self.due_at

    def due(self, t):
        if self.due_at is not None and t >= self.due_at:
            self.ran.append(t)
            self.due_at = None
            return [{"kind": "job_done", "t": t.isoformat()}]
        return []

    def observe(self, call):
        self.seen.append(call)

    def effect(self, name, args, t):
        return [{"kind": "page", "t": t.isoformat(), "from": name}]

    def state(self):
        return {"routing": 0.97}

    def export(self):
        return {"ticker": {"ticks": self.ticks}}


def test_loop_runs_ticks_due_events_effects_and_state():
    s = scenario()
    s["timeline"] = [{"id": "hold", "at": "08:20:00", "actions": [{"world": "hold_fire"}]}]
    world = MechWorld(s, "1", NoAws(), Ws(), Ticker)
    world.start()
    assert world.mech.started == world.start_t
    out = world.advance_to(world.start_t + timedelta(minutes=1), wake={"job_done"})
    assert out["now"] == world.start_t + timedelta(seconds=25) and out["woke"][0]["kind"] == "job_done"
    out = world.advance_to(world.start_t + timedelta(minutes=10))
    assert world.mech.ticks == 60
    assert "hold" in out["fired"] and any(d.get("from") == "hold_fire" for d in out["delivered"])
    assert world.eval_state("routing >= 0.95") and not world.eval_state("flag('x')")
    world.observe({"t": "2026-11-16T08:30:00-05:00", "tool": "chat_post", "args": {"text": "hi"}})
    assert isinstance(world.mech.seen[0]["t"], datetime)
    assert world.export()["ticker"]["ticks"] == 60
