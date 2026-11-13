"""endpointd: the planner, three enactors and the halt latch that write internal.vantleaf.io from plans in DynamoDB.

All shared state lives in the emulator (through the mirror), so the agent sees the same plans, pointers, kill switches
and records with the aws CLI as epctl and the physics do. Plans carry the ledger records; the other 40 endpoints are the
endpoint table's, unchanged in every generation (the plan's `endpoints_ref`).
"""

import json
from datetime import datetime, timedelta

from .mirror import AwsMirror
from .util import iso

COMPONENTS = ("planner", "enactor-a", "enactor-b", "enactor-c")
ENACTORS = COMPONENTS[1:]
SWITCH = "/platform/endpointd/prod/{}/enabled"
ENDPOINTS_REF = "endpoint-table/v1"


def host_of(component: str) -> str:
    return "endpointd-planner-1" if component == "planner" else "endpointd-" + component


def plan_ledger(item: dict) -> list[dict]:
    led = item.get("ledger") or []
    return json.loads(led) if isinstance(led, str) else led


class Endpointd:
    def __init__(self, cfg: dict, mirror: AwsMirror, fleet_ready: callable, start: datetime):
        self.cfg = cfg
        self.m = mirror
        self.table = cfg["endpoint_table"]
        self.ledger = cfg.get("ledger_fqdn", "ledger.internal.vantleaf.io")
        self.order = self.table["apply_order"]
        self.ttl = int(self.table.get("ttl", 60))
        self.fleet_ready = fleet_ready
        self.apply_s = float(cfg.get("enactor_step_s", 10))
        self.next_gen = int(cfg.get("next_gen", 48209))
        self.next_plan_at = start + timedelta(seconds=float(cfg.get("plan_every_s", 60)))
        self.plan_every = timedelta(seconds=float(cfg.get("plan_every_s", 60)))
        self.batches: dict[str, dict | None] = {e: None for e in ENACTORS}
        self.processes = {c: True for c in COMPONENTS}
        self.halted_since: datetime | None = None
        if cfg.get("halted_since"):
            from .util import parse_time
            self.halted_since = parse_time(cfg["halted_since"], start)
        self.archive: dict[int, list[dict]] = {}
        for seg in self.table.get("ledger_history", []):
            for gen in range(seg["from_gen"], seg["to_gen"] + 1):
                self.archive[gen] = [{"set_id": f"g{gen}-{r['set_id_suffix']}", "ip": r["ip"], "weight": r["weight"]}
                                     for r in seg["records"]]
        self.kill_switch_timeline: list[dict] = []
        self.apply_events: list[dict] = []
        self.ledger_correct: list[list] = []
        self.endpoints_correct: list[list] = []
        self._last_status = None

    # ---- state reads ----
    def switch(self, c: str) -> bool:
        return self.m.ssm.get(SWITCH.format(c), "true") == "true"

    def active(self, c: str) -> bool:
        return self.switch(c) and self.processes[c]

    def plan_records(self, gen: int, fqdn: str, item: dict | None = None) -> list[dict] | None:
        if fqdn == self.ledger:
            item = item or self.m.plan(gen)
            return plan_ledger(item) if item else None
        return [{"set_id": f"g{gen}-{r['set_id_suffix']}", "ip": r["ip"], "weight": r["weight"]}
                for r in self.table["non_ledger"][fqdn]]

    def healthy(self, fqdn: str) -> set[str]:
        if fqdn == self.ledger:
            return {i["ip"] for i in self.fleet_ready() if i.get("state") == "running" and i.get("ready")}
        return {r["ip"] for r in self.table["non_ledger"][fqdn] if r["weight"] > 0}

    def correct(self, fqdn: str) -> bool:
        live = self.m.live(fqdn)
        if not live:
            return False
        return {r["Value"] for r in live if r["Weight"] > 0} == self.healthy(fqdn) and all(r["TTL"] <= 300 for r in live)

    def latch_reasons(self) -> list[str]:
        out = []
        for fqdn, ptr in sorted(self.m.pointers().items()):
            gen = int(ptr.get("generation", 0))
            item = self.m.plan(gen)
            if fqdn == self.ledger and item is None:
                out.append(f"applied#{fqdn} -> gen {gen} not found")
                continue
            want = self.plan_records(gen, fqdn, item)
            if want is None:
                out.append(f"applied#{fqdn} -> gen {gen} not found")
                continue
            have = {(r["Value"], int(r["Weight"])) for r in self.m.live(fqdn)}
            if {(r["ip"], int(r["weight"])) for r in want} != have:
                out.append(f"applied#{fqdn} -> gen {gen} does not match live records")
        return out

    def engaged(self) -> bool:
        return bool(self.latch_reasons())

    # ---- physics tick ----
    def tick(self, t: datetime) -> None:
        switches = {c: self.switch(c) for c in COMPONENTS}
        status = (switches, dict(self.processes))
        if status != self._last_status:
            self.kill_switch_timeline.append({"t": iso(t), "switches": {k.replace("enactor-", ""): v for k, v in
                                                                         switches.items()},
                                              "processes": dict(self.processes)})
            self._last_status = status
        if self.active("planner") and t >= self.next_plan_at:
            self.write_plan(t)
        engaged = self.engaged()
        if engaged and self.halted_since is None:
            self.halted_since = t
        if not engaged:
            self.halted_since = None
        for e in ENACTORS:
            self.step_enactor(e, t, engaged)
        self.ledger_correct.append([iso(t), self.correct(self.ledger)])
        self.endpoints_correct.append([iso(t), sum(1 for f in self.order if self.correct(f))])

    def write_plan(self, t: datetime) -> None:
        gen = self.next_gen
        recs, per_az = [], {}
        for inst in sorted(self.fleet_ready(), key=lambda i: (i["az"], i["name"])):
            if inst.get("state") != "running":
                continue
            per_az[inst["az"]] = per_az.get(inst["az"], 0) + 1
            recs.append({"set_id": f"g{gen}-{inst['az']}-{per_az[inst['az']]}", "ip": inst["ip"],
                         "weight": 100 if inst.get("ready") else 0})
        self.m.put_item({"pk": f"plan#{gen:010d}", "generation": gen, "status": "PENDING", "created_at": iso(t),
                         "ledger": recs, "endpoints_ref": ENDPOINTS_REF, "endpoint_count": len(self.order)},
                        principal="role:vl-endpointd-planner-role")
        self.archive[gen] = recs
        self.next_gen += 1
        self.next_plan_at = t + self.plan_every

    def step_enactor(self, e: str, t: datetime, engaged: bool) -> None:
        batch = self.batches[e]
        if not self.active(e):
            if batch is not None:
                self.apply_events.append({"t": iso(t), "enactor": e, "event": "abandoned", "gen": batch["gen"]})
            self.batches[e] = None
            return
        if engaged:
            return
        if batch is None:
            gens = self.m.plan_gens()
            applied = [int(p.get("generation", 0)) for p in self.m.pointers().values()]
            taken = {b["gen"] for b in self.batches.values() if b}
            if not gens or gens[-1] <= max(applied, default=0) or gens[-1] in taken:
                return
            batch = self.batches[e] = {"gen": gens[-1], "idx": 0, "next_at": t}
            self.apply_events.append({"t": iso(t), "enactor": e, "event": "picked", "gen": batch["gen"]})
        if t < batch["next_at"]:
            return
        item = self.m.plan(batch["gen"])
        if item is None:
            self.apply_events.append({"t": iso(t), "enactor": e, "event": "plan_missing", "gen": batch["gen"]})
            self.batches[e] = None
            return
        fqdn = self.order[batch["idx"]]
        role = "role:vl-endpointd-enactor-role"
        self.m.put_item({"pk": "applied#" + fqdn, "generation": batch["gen"], "state": "APPLYING", "applied_by": e},
                        principal=role)
        self.m.replace_records(fqdn, self.ttl, self.plan_records(batch["gen"], fqdn, item),
                               f"endpointd {e} gen={batch['gen']}", principal=role)
        self.m.put_item({"pk": "applied#" + fqdn, "generation": batch["gen"], "state": "APPLIED", "applied_by": e,
                         "applied_at": iso(t)}, principal=role)
        batch["idx"] += 1
        batch["next_at"] = t + timedelta(seconds=self.apply_s)
        if batch["idx"] >= len(self.order):
            gen = batch["gen"]
            self.m.update_item(f"plan#{gen:010d}", {"status": "APPLIED", "applied_by": e, "verified_at": iso(t),
                                                    "verify": f"ok {len(self.order)}/{len(self.order)}"},
                               principal=role)
            referenced = {int(p.get("generation", 0)) for p in self.m.pointers().values()}
            for old in self.m.plan_gens():
                if old < gen - 20 and old not in referenced:
                    self.m.delete_item(f"plan#{old:010d}", principal=role)
            self.apply_events.append({"t": iso(t), "enactor": e, "event": "applied", "gen": gen})
            self.batches[e] = None

    def export(self) -> dict:
        return {
            "ledger_correct_series": self.ledger_correct,
            "endpoints_correct_series": self.endpoints_correct,
            "kill_switch_timeline": self.kill_switch_timeline,
            "enactor_apply_events": self.apply_events,
            "next_gen": self.next_gen,
            "halt_engaged": self.engaged(),
            "processes": dict(self.processes),
        }
