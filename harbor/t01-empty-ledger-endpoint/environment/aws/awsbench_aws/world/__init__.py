"""The worlds: T01's endpointd, DNS, ledger storm, traffic, timeline and NPCs (`World`, the default), or a scenario's
own mechanics (`world.mechanics`, run by `MechWorld`), in pure Python."""

import importlib
import re

from .core import World
from .mech import MechWorld
from .seed import load_scenario, replay_seed

__all__ = ["MechWorld", "World", "load_scenario", "make_world", "replay_seed"]


def make_world(scenario: dict, seed: str, aws, ws):
    """T01's `World` unless the scenario's `world.mechanics` names a module of this package with a `Mechanics` class."""
    name = (scenario.get("world") or {}).get("mechanics")
    if not name or name == "t01":
        return World(scenario, seed, aws, ws)
    if not re.fullmatch(r"[a-z][a-z0-9_]*", str(name)):
        raise ValueError(f"world.mechanics must name a module of awsbench_aws.world, not {name!r}")
    return MechWorld(scenario, seed, aws, ws, importlib.import_module(f"{__name__}.{name}").Mechanics)
