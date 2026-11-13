"""Replay T03 "Thread Ceiling" in process through the world package, for the AwsIncidentsBench viewers.

    python tools/t03_replay.py --root T03_ROOT reference OUT.json
    python tools/t03_replay.py --root T03_ROOT run RUN_DIR OUT.json

``T03_ROOT`` is a checkout that has the T03 world (``scenarios/t03.yaml``, ``world/t03*.py``) and its oracle path
(``tests/e2e/t03_oracle.py``). The world is driven with in-memory fakes in the shape of ``tests/unit/test_t03_world.py``'s
(an SSM store, a workspace that records what the timeline and colleagues post), so nothing is deployed.

``run`` replays an agent run's own calls (the action log's AWS writes, fleet commands and restarts, at the times the meter
charged them) and samples what the world export doesn't keep: every host's state every 30 s and a sample host's threads
against its ``limit_nproc``. It checks itself against the run's export: the replay's per-minute host-state counts must
match the run's ``srcoord.host_state_timeline``; the mismatching minutes are reported.

``reference`` plays the oracle path of ``t03_oracle.py`` (fixed ids filled in, waits run to their full length), costed by
the real meter, and writes an export in the run's shape plus the grader's own outcome rows (C02-C06, C15, C16) computed
from it by ``artifacts/t03-grade/verify.py``.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import re
import shlex
import sys
from datetime import datetime, timedelta
from pathlib import Path

AWS_REL = "src/agentenv_awsbench/bundles/awsbench/envs/aws"
GRADE_REL = "src/agentenv_awsbench/bundles/awsbench/artifacts/t03-grade/verify.py"
DT = 30
SAMPLE = "sr-b-017"
COST = {"point_read": 20, "write": 45, "broad": 75}
RESTARTING = ("stopping", "jvm", "bootstrap_wait", "bootstrap", "first_build", "wedged")
READ = re.compile(r"^(Describe|Get|List|Lookup|Test|Query|Scan|BatchGet|Filter|Search|Select|StartQuery|StopQuery)")
AGENT = {"role": "sre-primary", "principal": "sre-primary"}


def _load(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class SsmAws:
    """The AWS the T03 world reads: SSM's /streamroute/prod parameters; every other call answers empty."""

    def __init__(self, ssm: dict[str, str]):
        self.ssm = dict(ssm)

    def call(self, service, operation, params, *, principal=None, world_made=False):
        if service == "ssm" and operation == "GetParametersByPath":
            path = params.get("Path", "/")
            return {"Parameters": [{"Name": k, "Value": v} for k, v in sorted(self.ssm.items()) if k.startswith(path)]}
        if service == "ssm" and operation == "PutParameter":
            self.ssm[params["Name"]] = str(params["Value"])
        if service == "ssm" and operation in ("DeleteParameter", "DeleteParameters"):
            for name in params.get("Names") or [params.get("Name")]:
                self.ssm.pop(name, None)
        return {}


class Ws:
    """The workspace the timeline and colleagues write to, recorded with times."""

    def __init__(self):
        self.ops: list[tuple[str, dict, datetime]] = []

    def apply(self, op, args, at):
        self.ops.append((op, args, at))
        return {"ok": True}


def state_code(h, ts: float) -> str:
    if h.terminated:
        return "T"
    if h.launched > ts:
        return "."
    if h.state == "EJECTED":
        return "E"
    if h.quarantined_until and ts < h.quarantined_until and h.state != "ROUTING":
        return "Q"
    if h.state == "WARMING" or (h.state == "STOPPED" and h.phase in RESTARTING):
        return "W"
    return {"ROUTING": "R", "STALE": "S", "BROKEN": "B", "STOPPED": "X"}.get(h.state, "X")


class Recorder:
    """Samples every host's state and the sample host's threads every 30 s from the scenario start to the horizon."""

    def __init__(self, world):
        self.w = world
        self.t0 = None
        self.next = world.start_t
        self.rows: list[dict[str, str]] = []
        self.threads: list[int] = []
        self.limit: list[int] = []
        self.hold: list[int] = []
        start, mech = world.start_t.timestamp(), world.mech
        step = mech._step

        def presim(ts: float, dt: float):
            if not self.rows and abs(ts - dt - mech.t0) < 1e-6:
                self.snap(mech.t0)
            out = step(ts, dt)
            if ts < start - 1e-6 and round(ts) % DT == 0:
                self.snap(ts)
            return out
        mech._step = presim

    def until(self, t: datetime) -> None:
        while self.next <= t:
            self.w.advance_to(self.next)
            self.snap(self.next.timestamp())
            self.next += timedelta(seconds=DT)
        self.w.advance_to(t)

    def snap(self, ts: float) -> None:
        if self.t0 is None:
            self.t0 = ts
        fleet = self.w.mech.fleet
        try:
            self.hold.append(int(bool(self.w.mech.state().get("hold_armed"))))
        except Exception:  # noqa: BLE001 - state() before the flow has history
            self.hold.append(1)
        self.rows.append({n: state_code(h, ts) for n, h in fleet.hosts.items()})
        h = fleet.hosts.get(SAMPLE)
        p = fleet.p
        self.threads.append((h.jvm_threads(p) if h else 0) + p.get("sidecar", 22))
        self.limit.append(int(h.limit) if h else 0)

    def board(self) -> dict:
        names = sorted({n for r in self.rows for n in r}, key=lambda n: (n[3], int(n[-3:])))
        fleet = self.w.mech.fleet
        hosts = [{"name": n, "az": n[3], "added": n not in ORIGINAL and n[3] in "abc",
                  "plat": n in PLAT, "s": "".join(r.get(n, ".") for r in self.rows)} for n in names]
        day = self.w.start_t.replace(hour=0, minute=0, second=0).timestamp()
        fails = sorted((t, h) for t, g, h, line in fleet.logs if "failed after" in line)
        return {"t0": round(self.t0 - day), "dt": DT, "n": len(self.rows), "hosts": hosts, "hold": self.hold,
                "first_failure": [round(fails[0][0] - day), fails[0][1]] if fails else None,
                "sample": {"host": SAMPLE, "threads": self.threads, "limit": self.limit,
                           "limit_seed": int(fleet.hosts[SAMPLE].limit) if SAMPLE in fleet.hosts else 1024}}


ORIGINAL = {f"sr-{az}-{n:03d}" for az in "abc" for n in range(1, 41)}
PLAT = {f"sr-{az}-{n:03d}" for az in "abc" for n in range(41, 45)}


def setup(root: Path, seed: str, ssm: dict | None):
    sys.path.insert(0, str(root / AWS_REL))
    from awsbench_aws.world import load_scenario, make_world
    scenario = load_scenario("t03")
    if ssm is None:
        ssm = {}
        for e in scenario.get("aws_seed", []):
            if e.get("service") == "ssm" and e.get("operation") == "PutParameter":
                ssm[e["params"]["Name"]] = str(e["params"]["Value"])
    aws, ws = SsmAws(ssm), Ws()
    world = make_world(scenario, seed, aws, ws)
    rec = Recorder(world)
    world.start()
    return scenario, aws, ws, world, rec


def counts_check(world, export: dict) -> dict:
    """Minutes where the replay's host-state counts differ from the run's export."""
    mine = {int(t): c for t, c, *_ in world.mech.host_states if t >= world.start_t.timestamp()}
    theirs = {}
    for t, c, *_ in export["world"]["t03"]["srcoord"]["host_state_timeline"]:
        ts = datetime.fromisoformat(t).timestamp()
        if ts >= world.start_t.timestamp():
            theirs[int(ts)] = c
    common = sorted(set(mine) & set(theirs))
    bad = [t for t in common if mine[t] != theirs[t]]
    first = datetime.fromtimestamp(bad[0], world.start_t.tzinfo).strftime("%H:%M") if bad else None
    return {"minutes": len(common), "mismatched": len(bad), "first_mismatch": first}


# ---------------------------------------------------------------- an agent run


def pacing(args: dict) -> dict:
    """The restart's ``wait_for`` and ``pause_seconds`` as the call gave them; builds before 0d12611 take neither."""
    return {k: args[k] for k in ("wait_for", "pause_seconds") if args.get(k) is not None}

def replay_run(root: Path, run_dir: Path) -> dict:
    export = json.loads((run_dir / "aws-export.json").read_text())
    ep = export["episode"]
    _scenario, aws, _ws, world, rec = setup(root, ep.get("seed") or "t03-draft-1",
                                            (export.get("start_state") or {}).get("ssm"))
    unserved = []
    rows: dict[int, list[dict]] = {}
    for r in export.get("action_log", []):
        if r.get("call_no") is not None and not r.get("world_made"):
            rows.setdefault(int(r["call_no"]), []).append(r)
    for m in sorted(export.get("meter", []), key=lambda m: int(m["call_no"])):
        n = int(m["call_no"])
        t = datetime.fromisoformat(m["t_exec"])
        rec.until(t)
        mine = rows.get(n, [])
        args = next((r.get("params") or {} for r in mine if r.get("tool") == m["tool"] and not r.get("service")), {})
        for r in mine:
            if r.get("error") or not r.get("service") or READ.match(r.get("operation") or ""):
                continue
            if r["service"] == "ssm":
                aws.call("ssm", r["operation"], r.get("params") or {})
            elif r["service"] == "autoscaling":
                try:
                    world.serve_aws("autoscaling", r["operation"], dict(r.get("params") or {}))
                except Exception as e:  # noqa: BLE001 - the run's own answer was an error the log missed
                    unserved.append(f"call {n} {r['operation']}: {e}")
        tool = m["tool"]
        if tool == "fleet_restart" and not any(r.get("error") for r in mine):
            world.fleet_restart(args.get("selector") or args.get("host"), int(args.get("batch_size") or 1), AGENT,
                                **pacing(args))
        elif tool == "fleet_exec":
            world.fleet_exec(args.get("selector") or args.get("host") or "", args.get("command") or args.get("cmd") or "",
                             AGENT)
        cli = next((r for r in mine if r.get("tool") in ("aws_cli", "aws_api") and r.get("service")), None)
        obs_args = dict(args)
        if cli is not None:
            obs_args = {"service": cli.get("service"), "operation": cli.get("operation"), "params": cli.get("params") or {}}
        world.observe({"t": t, "call_no": n, "tool": tool, "role": "sre-primary", "args": obs_args,
                       "resolved": {"error": next((r.get("error") for r in mine if r.get("error")), None)}})
    rec.until(datetime.fromisoformat(ep["horizon"]))
    return {"board": rec.board(), "check": {**counts_check(world, export), "unserved": unserved}}


# ---------------------------------------------------------------- the reference

def cli_params(argv: list[str]) -> tuple[str, str, dict]:
    """`aws <service> <op> --a-b v ...` as (service, Operation, {AB: v})."""
    service, op = argv[1], "".join(w.capitalize() for w in argv[2].split("-"))
    op = {"DeleteScheduledAction": "DeleteScheduledAction"}.get(op, op)
    params, i = {}, 3
    while i < len(argv):
        key = "".join(w.capitalize() for w in argv[i].lstrip("-").split("-"))
        if i + 1 < len(argv) and not argv[i + 1].startswith("--"):
            v = argv[i + 1]
            params[key] = int(v) if v.isdigit() else v
            i += 2
        else:
            params[key] = True
            i += 1
    if service == "autoscaling" and "AutoScalingGroupNames" in params:
        params["AutoScalingGroupNames"] = [params["AutoScalingGroupNames"]]
    return service, op, params


def fill(value, found: dict):
    if isinstance(value, str):
        for k, v in found.items():
            value = value.replace("{" + k + "}", str(v))
        return value
    if isinstance(value, dict):
        return {k: fill(v, found) for k, v in value.items()}
    if isinstance(value, list):
        return [fill(v, found) for v in value]
    return value


def replay_reference(root: Path) -> dict:
    scenario, aws, ws, world, rec = setup(root, "t03-draft-1", None)
    start_ssm = dict(aws.ssm)
    from awsbench_aws import meter
    oracle = _load(root / "tests/e2e/t03_oracle.py", "t03_oracle_hub")
    found = {"incident": "OPS-4446", "inc_channel": "#inc-4446-ledger-delay", "status_id": "si-1",
             "freya_mail": "g-freya-0905", "followups": "PLAT-2300, PLAT-2301, PLAT-2302", "job_id": "job-0001"}
    now = world.start_t
    meter_rows, log, session = [], [], {}
    for n, (tool, raw) in enumerate(oracle.steps_for("oracle"), start=1):
        args = fill(raw, found)
        if tool == "fleet_exec":
            args = {"selector": args["host"], "command": args["cmd"]}
        if tool == "fleet_restart":
            args = {"selector": args["host"], "batch_size": args["batch_size"]}
        t_issue = now
        if tool == "wait":
            target = (datetime.fromisoformat(f"{now.date()}T{args['until']}-05:00") if args.get("until")
                      else now + timedelta(minutes=float(args["minutes"])))
            cls, cost = "wait", 0
            rec.until(target)
            now = target
            data = {"waited_until": now.strftime("%H:%M:%S")}
        else:
            hosts = len(world.mech.fleet.select(args["selector"])) if tool == "fleet_exec" else None
            cls = meter.classify(tool, args, scenario.get("cost_overrides"), hosts)
            cost = COST.get(cls, 0)
            now = now + timedelta(seconds=cost)
            rec.until(now)
            data, obs = {"result": "scripted"}, dict(args)
            try:
                if tool == "fleet_exec":
                    data = world.fleet_exec(args["selector"], args["command"], AGENT)
                elif tool == "fleet_inspect_host":
                    data = world.fleet_inspect_host(args["host"])
                elif tool == "fleet_restart":
                    data = world.fleet_restart(args["selector"], args["batch_size"], AGENT, **pacing(args))
                    found["job_id"] = data.get("job_id", found["job_id"])
                elif tool == "fleet_job_status":
                    data = world.fleet_job_status(args["job_id"])
                elif tool == "aws_cli":
                    service, op, params = cli_params(shlex.split(args["command"]))
                    obs = {"service": service, "operation": op, "params": params}
                    if service == "ssm":
                        aws.call("ssm", op, params)
                        data = {"output": {"Version": 2}}
                    else:
                        served = world.serve_aws(service, op, params)
                        data = {"output": served} if served is not None else data
                    log.append({"call_no": n, "t": now.isoformat(), "t_issue": t_issue.isoformat(), "role": "sre-primary",
                                "principal": "sre-primary", "world_made": False, "server": "aws", "tool": "aws_cli",
                                "service": service, "operation": op, "params": params, "error": None})
            except Exception as e:  # noqa: BLE001 - a scripted step the in-process world can't answer
                data = {"error": str(e)}
            world.observe({"t": now, "call_no": n, "tool": tool, "role": "sre-primary", "args": obs, "resolved": {}})
        if tool != "aws_cli":
            log.append({"call_no": n, "t": now.isoformat(), "t_issue": t_issue.isoformat(), "role": "sre-primary",
                        "principal": "sre-primary", "world_made": False,
                        "server": "aws" if tool.startswith(("fleet_", "aws_", "wait")) else "workspace", "tool": tool,
                        "service": None, "operation": None, "params": args, "error": None})
        meter_rows.append({"call_no": n, "tool": tool, "cost_class": cls, "cost_s": cost, "t_issue": t_issue.isoformat(),
                           "t_exec": now.isoformat()})
        session[n] = {"input": args, "data": data, "text": json.dumps(data, default=str)[:2000]}
    done = now
    rec.until(datetime.fromisoformat(scenario["horizon"]))
    export = {"episode": {"scenario": "t03", "seed": "t03-draft-1", "start": scenario["start"],
                          "window_end": scenario["window_end"], "horizon": scenario["horizon"], "end_reason": "done",
                          "done_at": done.isoformat(), "calls_used": len(meter_rows)},
              "world": world.export(), "action_log": log, "meter": meter_rows, "start_state": {"ssm": start_ssm}}
    export = json.loads(json.dumps(export, default=str))
    grade = _load(root / GRADE_REL, "t03_grade_hub")
    inp = grade.Inputs(export, {})
    outcome = [f(inp) for f in (grade.c02, grade.c03, grade.c04, grade.c05, grade.c06, grade.c15, grade.c16)]
    ops = [{"op": op, "args": a, "t": at.isoformat()} for op, a, at in ws.ops]
    return {"export": export, "session": {str(k): v for k, v in session.items()}, "ws_ops": ops,
            "outcome_rows": outcome, "board": rec.board()}


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--root", type=Path, required=True)
    sub = ap.add_subparsers(dest="mode", required=True)
    r = sub.add_parser("reference")
    r.add_argument("out", type=Path)
    g = sub.add_parser("run")
    g.add_argument("run_dir", type=Path)
    g.add_argument("out", type=Path)
    a = ap.parse_args(argv)
    out = replay_reference(a.root) if a.mode == "reference" else replay_run(a.root, a.run_dir)
    a.out.write_text(json.dumps(out, ensure_ascii=False, separators=(",", ":"), default=str))
    print(f"{a.out}: {out['board']['n']} samples x {len(out['board']['hosts'])} hosts"
          + (f"; check {out['check']}" if "check" in out else ""))


if __name__ == "__main__":
    main()
