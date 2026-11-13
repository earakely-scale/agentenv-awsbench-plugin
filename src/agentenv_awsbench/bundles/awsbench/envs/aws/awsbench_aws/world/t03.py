"""T03 "Thread Ceiling" mechanics: the streamroute fleet, its thread ceiling, srcoord, cellmap, the ASG and its 14:00
hold, the per-cell backlog, telemetry and settlement (docs/T03-build.md). This is the interface stub the builders fill
in; until then it serves no AWS reads and its hosts run no commands."""

from datetime import datetime

NOT_AVAILABLE = "sh: {cmd}: not available in this environment"


class Mechanics:
    def __init__(self, world, cfg: dict):
        self.w = world
        self.cfg = cfg
        self.hosts = {h["name"]: dict(h) for h in cfg.get("hosts", [])}

    def start(self, t: datetime) -> None:
        pass

    def tick(self, t: datetime, dt: float) -> list[dict]:
        return []

    def next_due(self) -> datetime | None:
        return None

    def due(self, t: datetime) -> list[dict]:
        return []

    def observe(self, call: dict) -> None:
        pass

    def effect(self, name: str, args: dict, t: datetime) -> list[dict]:
        return []

    def state(self) -> dict:
        return {}

    def serve_aws(self, service: str, operation: str, params: dict, now: datetime) -> dict | None:
        return None

    def fleet_list_hosts(self, fleet: str | None = None, az: str | None = None) -> list[dict]:
        return [h for h in self.hosts.values() if (fleet is None or h.get("fleet") == fleet)
                and (az is None or h.get("az") == az)]

    def fleet_inspect_host(self, host: str, now: datetime) -> dict:
        return self.hosts.get(host) or {"error": f"no host {host!r}"}

    def fleet_exec(self, selector: str, command: str, caller: dict, now: datetime) -> dict:
        names = [n for n in self.hosts if n == selector]
        if not names:
            return {"hosts": [], "error": f"no hosts match {selector!r}"}
        cmd = (command or "").split()[0] if (command or "").split() else ""
        return {"hosts": [{"host": n, "stdout": NOT_AVAILABLE.format(cmd=cmd) + "\n", "exit_code": 127} for n in names]}

    def fleet_restart(self, selector: str, batch_size: int, caller: dict, now: datetime) -> dict:
        return {"error": "restarts are not modelled yet"}

    def fleet_job_status(self, job_id: str, now: datetime) -> dict:
        return {"error": f"no job {job_id!r}"}

    def export(self) -> dict:
        return {"t03": {}}
