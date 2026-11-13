"""epctl 2.3.0, endpointd's operator CLI as run on the endpointd hosts. Writes go to AWS as the caller."""

import shlex
from datetime import datetime

from .endpointd import COMPONENTS, ENACTORS, SWITCH, Endpointd, plan_ledger
from .util import iso, zulu

HELP = """epctl 2.3.0 - endpointd operator CLI (prod, zone internal.vantleaf.io)

usage:
  epctl status
  epctl plans list [--since GEN]
  epctl plan show GEN [--endpoint FQDN] [--from-archive]
  epctl plan diff GEN1 GEN2 [--endpoint FQDN] [--from-archive]
  epctl plan diff GEN --live-fleet [--endpoint FQDN] [--from-archive]
  epctl plan restore --gen GEN --from-archive
  epctl verify [--endpoint FQDN] [--resolve]
  epctl pause|resume planner|enactor-a|enactor-b|enactor-c|enactors|all
  epctl apply --gen GEN (--endpoint FQDN | --all) --manual --reason TEXT
  epctl set-applied --endpoint FQDN --gen GEN --reason TEXT

Writes use your own AWS credentials and are recorded in CloudTrail. apply retries Route 53 Throttling and
PriorRequestNotComplete up to 3 times, backing off 2, 4 and 8 s; the backoff is part of the command's time.
"""
RETRIED = ("Throttling", "PriorRequestNotComplete")
BACKOFF_S = (2, 4, 8)


def _usages() -> dict[str, list[str]]:
    """Each subcommand's usage lines from HELP: `pause|resume` is two, `plan show` and `plans list` take two words."""
    out: dict[str, list[str]] = {}
    for line in HELP.splitlines():
        if line.startswith("  epctl "):
            words = line.split()[1:]
            names, rest = (([f"{words[0]} {words[1]}"], words[2:]) if words[0] in ("plan", "plans")
                           else (words[0].split("|"), words[1:]))
            for name in names:
                out.setdefault(name, []).append(" ".join(["epctl", name, *rest]))
    return out


USAGES = _usages()


def usage(argv: list[str]) -> tuple[str, int]:
    """`epctl [<subcommand>] --help`: that subcommand's usage (every `plan` one for `epctl plan --help`), else HELP."""
    words = [a for a in argv if not a.startswith("-") and a != "help"]
    if not words:
        return HELP, 0
    lines = USAGES.get(" ".join(words[:2])) or [u for k, v in USAGES.items() if k.split()[0] == words[0] for u in v]
    if not lines:
        return f"epctl: unknown command {' '.join(words[:2])!r}\nRun 'epctl --help' for usage.\n", 2
    return "usage:\n" + "".join(f"  {u}\n" for u in lines), 0


class Usage(Exception):
    pass


def _opts(args: list[str], flags=(), values=()) -> tuple[list[str], dict]:
    pos, out, i = [], {}, 0
    while i < len(args):
        a = args[i]
        if a in flags:
            out[a] = True
        elif a in values:
            if i + 1 >= len(args):
                raise Usage(f"epctl: option {a} needs a value")
            out[a] = args[i + 1]
            i += 1
        elif a.startswith("--") and "=" in a and a.split("=", 1)[0] in values:
            k, v = a.split("=", 1)
            out[k] = v
        elif a.startswith("--"):
            raise Usage(f"epctl: unknown option {a}")
        else:
            pos.append(a)
        i += 1
    return pos, out


def _archive_url(gen: int) -> str:
    return f"s3://vl-endpointd-plan-archive/prod/plans/gen={gen:010d}.json"


def _z(value) -> str:
    return zulu(datetime.fromisoformat(value)) if value else "-"


def _gen(value) -> int:
    try:
        return int(str(value).lstrip("g"))
    except ValueError:
        raise Usage(f"epctl: bad generation {value!r}")


class Epctl:
    def __init__(self, ep: Endpointd, world):
        self.ep = ep
        self.world = world

    def run(self, command: str, host: str, caller: dict, now: datetime) -> tuple[str, int]:
        try:
            argv = shlex.split(command)[1:]
        except ValueError as e:
            return f"epctl: {e}\n", 2
        if not argv or "-h" in argv or "--help" in argv or argv[0] == "help":
            return usage(argv)
        try:
            return self._dispatch(argv, host, caller, now)
        except Usage as e:
            return f"{e}\nRun 'epctl --help' for usage.\n", 2
        except Exception as e:  # noqa: BLE001 - any AWS error becomes epctl output
            code = getattr(e, "code", type(e).__name__)
            return f"epctl: AWS error {code}: {getattr(e, 'message', str(e))}\n", 1

    def _dispatch(self, argv, host, caller, now):
        cmd, rest = argv[0], argv[1:]
        if cmd == "status":
            return self.status(now), 0
        if cmd == "plans" and rest[:1] == ["list"]:
            _, o = _opts(rest[1:], values=("--since",))
            return self.plans_list(_gen(o["--since"]) if "--since" in o else None), 0
        if cmd == "plan" and rest[:1] == ["show"]:
            pos, o = _opts(rest[1:], ("--from-archive",), ("--endpoint",))
            if len(pos) != 1:
                raise Usage("usage: epctl plan show GEN [--endpoint FQDN] [--from-archive]")
            return self.plan_show(_gen(pos[0]), o.get("--endpoint"), "--from-archive" in o)
        if cmd == "plan" and rest[:1] == ["diff"]:
            pos, o = _opts(rest[1:], ("--from-archive", "--live-fleet"), ("--endpoint",))
            return self.plan_diff(pos, o)
        if cmd == "plan" and rest[:1] == ["restore"]:
            pos, o = _opts(rest[1:], ("--from-archive",), ("--gen",))
            gen = o.get("--gen") or (pos[0] if pos else None)
            if gen is None:
                raise Usage("usage: epctl plan restore --gen GEN --from-archive")
            return self.plan_restore(_gen(gen), caller, now)
        if cmd == "verify":
            _, o = _opts(rest, ("--resolve",), ("--endpoint",))
            return self.verify(o.get("--endpoint"), "--resolve" in o, now), 0
        if cmd in ("pause", "resume"):
            if len(rest) != 1:
                raise Usage(f"usage: epctl {cmd} planner|enactor-a|enactor-b|enactor-c|enactors|all")
            return self.switch(cmd, rest[0], caller)
        if cmd == "apply":
            _, o = _opts(rest, ("--all", "--manual"), ("--gen", "--endpoint", "--reason"))
            return self.apply(o, caller, now)
        if cmd == "set-applied":
            _, o = _opts(rest, (), ("--endpoint", "--gen", "--reason"))
            if "--endpoint" not in o or "--gen" not in o:
                raise Usage("usage: epctl set-applied --endpoint FQDN --gen GEN --reason TEXT")
            self.ep.m.put_item({"pk": "applied#" + o["--endpoint"], "generation": _gen(o["--gen"]),
                                "state": "APPLIED", "applied_by": f"manual:{caller.get('role', 'unknown')}",
                                "applied_at": iso(now), "reason": o.get("--reason", "")}, **self._as(caller))
            return f"applied#{o['--endpoint']} -> gen {_gen(o['--gen'])}\n", 0
        raise Usage(f"epctl: unknown command {' '.join(argv[:2])!r}")

    def _as(self, caller):
        return {"principal": caller.get("principal") or caller.get("role") or "sre-primary", "world_made": False}

    # ---- reads ----
    def status(self, now: datetime) -> str:
        ep = self.ep
        gens = ep.m.plan_gens()
        latest = ep.m.plan(gens[-1]) if gens else None
        pending = sum(1 for g in gens if ep.m.plan(g).get("status") == "PENDING")
        reasons = ep.latch_reasons()
        lines = [f"endpointd prod (zone {ep.m.zone}, {len(ep.order)} endpoints)"]
        pstate = "RUNNING" if ep.active("planner") else ("STOPPED" if not ep.processes["planner"] else "PAUSED")
        lines.append(f"planner {pstate} latest gen {gens[-1] if gens else '-'} "
                     f"({zulu(datetime.fromisoformat(latest['created_at'])) if latest else '-'}) pending {pending}")
        for e in ENACTORS:
            if not ep.processes[e]:
                state = "STOPPED (process not running)"
            elif not ep.switch(e):
                state = "PAUSED (kill switch off)"
            elif reasons:
                since = zulu(ep.halted_since) if ep.halted_since else "-"
                state = f"HALTED since {since} {reasons[0]}"
            elif ep.batches[e]:
                b = ep.batches[e]
                state = f"APPLYING gen {b['gen']} ({b['idx']}/{len(ep.order)})"
            else:
                state = "IDLE"
            lines.append(f"{e} {state}")
        lines.append("kill switches: " + " ".join(
            f"{c}={'on' if ep.switch(c) else 'off'}" for c in COMPONENTS))
        return "\n".join(lines) + "\n"

    def plans_list(self, since: int | None) -> str:
        ep = self.ep
        rows = ["GEN    CREATED    STATUS      APPLIED_BY  VERIFIED"]
        for g in ep.m.plan_gens():
            if since is not None and g < since:
                continue
            it = ep.m.plan(g)
            created = _z(it.get("created_at"))
            verified = (f"{zulu(datetime.fromisoformat(it['verified_at']))} verify={it.get('verify', '-')}"
                        if it.get("verified_at") else "-")
            rows.append(f"{g:<6} {created:<10} {it.get('status', '-'):<11} {it.get('applied_by', '-') or '-':<11} {verified}")
        by_gen: dict[int, list[str]] = {}
        for fqdn, p in ep.m.pointers().items():
            by_gen.setdefault(int(p.get("generation", 0)), []).append(fqdn)
        parts = []
        for g, names in sorted(by_gen.items(), key=lambda kv: -len(kv[1])):
            missing = " [MISSING PLAN]" if ep.m.plan(g) is None else ""
            parts.append(f"{len(names)} endpoints -> {g}{missing}" if len(names) > 1 else f"{names[0]} -> {g}{missing}")
        rows.append("applied# pointers: " + "; ".join(parts))
        return "\n".join(rows) + "\n"

    def _records(self, gen: int, fqdn: str, archive: bool):
        ep = self.ep
        item = ep.m.plan(gen)
        if fqdn == ep.ledger:
            if item is not None:
                return plan_ledger(item)
            if archive and gen in ep.archive:
                return ep.archive[gen]
            return None
        if item is None and not (archive and gen in ep.archive):
            return None
        return ep.plan_records(gen, fqdn)

    def plan_show(self, gen: int, endpoint: str | None, archive: bool):
        ep = self.ep
        item = ep.m.plan(gen)
        if item is None and not archive:
            return f"plan gen={gen} not found in endpointd-plans (try --from-archive)\n", 1
        if item is None and gen not in ep.archive:
            return f"plan gen={gen} not found in endpointd-plans or s3://vl-endpointd-plan-archive\n", 1
        src = "endpointd-plans" if item else _archive_url(gen)
        head = f"plan gen={gen} source={src}"
        if item:
            head += f" status={item.get('status')} created={_z(item.get('created_at'))}"
            if item.get("restored_at"):
                head += f" restored={_z(item['restored_at'])} by {item.get('restored_by', '-')}"
        lines = [head]
        for fqdn in [endpoint] if endpoint else ep.order:
            recs = self._records(gen, fqdn, archive)
            if recs is None:
                lines.append(f"{fqdn}: unknown endpoint")
                continue
            lines.append(f"{fqdn} ttl={ep.ttl}")
            lines += [f"  {r['set_id']:<26} {r['ip']:<15} w{r['weight']}" for r in recs]
        return "\n".join(lines) + "\n", 0

    def plan_diff(self, pos, o):
        ep = self.ep
        fqdn = o.get("--endpoint", ep.ledger)
        archive = "--from-archive" in o
        if "--live-fleet" in o:
            if len(pos) != 1:
                raise Usage("usage: epctl plan diff GEN --live-fleet [--endpoint FQDN]")
            gen = _gen(pos[0])
            recs = self._records(gen, fqdn, archive)
            if recs is None:
                return f"plan gen={gen} not found in endpointd-plans (try --from-archive)\n", 1
            if fqdn != ep.ledger:
                return f"{fqdn}: plan {gen} matches the endpoint table ({len(recs)} records) OK\n", 0
            fleet = {i["ip"]: i for i in ep.fleet_ready()}
            lines, seen = [], set()
            for r in recs:
                inst = fleet.get(r["ip"])
                seen.add(r["ip"])
                if inst is None:
                    lines.append(f"{r['ip']:<13} w{r['weight']:<4} (no instance; terminated)  MISMATCH")
                    continue
                ready = inst.get("ready")
                ok = (r["weight"] > 0) == bool(ready)
                desc = f"{inst['name']} running {'ready' if ready else 'NOT READY'}"
                note = "OK" if ok else ("MISMATCH (planner weight now 0)" if not ready else "MISMATCH")
                lines.append(f"{r['ip']:<13} w{r['weight']:<4} {desc:<26} {note}")
            for ip, inst in fleet.items():
                if ip not in seen and inst.get("state") == "running":
                    lines.append(f"missing from plan: {ip} ({inst['name']} running "
                                 f"{'ready' if inst.get('ready') else 'NOT READY'})")
            return "\n".join(lines) + "\n", 0
        if len(pos) != 2:
            raise Usage("usage: epctl plan diff GEN1 GEN2 [--endpoint FQDN] [--from-archive]")
        g1, g2 = _gen(pos[0]), _gen(pos[1])
        a, b = self._records(g1, fqdn, archive), self._records(g2, fqdn, archive)
        for g, r in ((g1, a), (g2, b)):
            if r is None:
                return f"plan gen={g} not found in endpointd-plans (try --from-archive)\n", 1
        wa, wb = {r["ip"]: r["weight"] for r in a}, {r["ip"]: r["weight"] for r in b}
        lines = [f"{fqdn}: {g1} -> {g2}"]
        for ip in sorted(set(wa) | set(wb)):
            x, y = wa.get(ip), wb.get(ip)
            if x == y:
                lines.append(f"  {ip:<15} w{x} (same)")
            else:
                lines.append(f"  {ip:<15} {'-' if x is None else f'w{x}'} -> {'-' if y is None else f'w{y}'}")
        return "\n".join(lines) + "\n", 0

    def verify(self, endpoint: str | None, resolve: bool, now: datetime) -> str:
        ep = self.ep
        if resolve:
            fqdn = endpoint or ep.ledger
            hosts = ("endpointd-enactor-a", "endpointd-enactor-b", "endpointd-enactor-c")
            return "\n".join([f"resolving {fqdn} from the enactor hosts:"]
                             + self.world.fleet.resolve_from(hosts, fqdn, now)) + "\n"
        ok, bad = 0, []
        for fqdn in [endpoint] if endpoint else ep.order:
            p = ep.m.pointers().get(fqdn)
            live = ep.m.live(fqdn)
            if p is None:
                bad.append(f"{fqdn}: no applied# pointer")
                continue
            gen = int(p.get("generation", 0))
            want = ep.plan_records(gen, fqdn)
            if want is None:
                live_txt = ", ".join(f"{r['Value']} w{r['Weight']}" for r in live) or "none"
                bad.append(f"{fqdn}: applied gen {gen} MISSING PLAN; live records: {live_txt}")
            elif {(r["ip"], r["weight"]) for r in want} != {(r["Value"], r["Weight"]) for r in live}:
                bad.append(f"{fqdn}: applied gen {gen} differs from live records "
                           f"({len(live)} live, {len(want)} planned)")
            else:
                ok += 1
        total = 1 if endpoint else len(ep.order)
        return f"{ok}/{total} OK" + ("; " + "; ".join(bad) if bad else "") + "\n"

    # ---- writes ----
    def switch(self, verb: str, target: str, caller: dict):
        comps = {"all": COMPONENTS, "enactors": ENACTORS}.get(target, (target,))
        if any(c not in COMPONENTS for c in comps):
            raise Usage(f"epctl: unknown component {target!r}")
        value = "false" if verb == "pause" else "true"
        out = []
        for c in comps:
            self.ep.m.put_parameter(SWITCH.format(c), value, **self._as(caller))
            out.append(f"{c}: kill switch {'off' if verb == 'pause' else 'on'} (takes effect within 10 s)")
        return "\n".join(out) + "\n", 0

    def apply(self, o: dict, caller: dict, now: datetime):
        ep = self.ep
        if "--gen" not in o or not ("--endpoint" in o or "--all" in o):
            raise Usage("usage: epctl apply --gen GEN (--endpoint FQDN | --all) --manual --reason TEXT")
        if "--manual" not in o:
            return "error: manual applies need --manual (enactors own automatic applies)\n", 2
        gen = _gen(o["--gen"])
        item = ep.m.plan(gen)
        if item is None:
            return f"error: plan gen={gen} not found; run epctl plan restore --gen {gen} --from-archive\n", 1
        targets = ep.order if "--all" in o else [o["--endpoint"]]
        if any(t not in ep.order for t in targets):
            return f"error: {o['--endpoint']} is not an endpointd-managed endpoint\n", 1
        lines = []
        enabled = [e for e in ENACTORS if ep.active(e)]
        if enabled:
            lines.append(f"WARNING: enactor(s) {', '.join(enabled)} are enabled and will overwrite manual records "
                         "with the newest plan")
        who = caller.get("role", "unknown")
        for fqdn in targets:
            recs = ep.plan_records(gen, fqdn, item)
            self._retrying(caller, lines, ep.m.replace_records, fqdn, ep.ttl, recs,
                           f"epctl manual apply gen={gen} by {who}", **self._as(caller))
            ep.m.put_item({"pk": "applied#" + fqdn, "generation": gen, "state": "APPLIED",
                           "applied_by": f"manual:{who}", "applied_at": iso(now),
                           "reason": o.get("--reason", "")}, **self._as(caller))
            lines.append(f"{fqdn}: UPSERT {len(recs)} records from gen {gen}; pointer applied#{fqdn} -> {gen}")
        lines.append(f"applied gen {gen} to {len(targets)} endpoint(s)")
        self.world.on_records_changed(now)
        return "\n".join(lines) + "\n", 0

    @staticmethod
    def _retrying(caller: dict, lines: list[str], fn, *args, **kwargs):
        """A Route 53 change, retried as HELP says; each backoff adds to the caller's `retry_s`, which the call is
        charged."""
        for attempt, wait in enumerate((*BACKOFF_S, None), 1):
            try:
                return fn(*args, **kwargs)
            except Exception as e:
                if getattr(e, "code", None) not in RETRIED or wait is None:
                    raise
                caller["retry_s"] = caller.get("retry_s", 0) + wait
                lines.append(f"route53: {e.code}: {getattr(e, 'message', '')} (retry {attempt}/3 in {wait}s)")

    def plan_restore(self, gen: int, caller: dict, now: datetime):
        ep = self.ep
        if ep.m.plan(gen) is not None:
            return f"plan gen={gen} is already in endpointd-plans (status {ep.m.plan(gen).get('status')})\n", 0
        if gen not in ep.archive:
            return f"error: {_archive_url(gen)} not found\n", 1
        ep.m.put_item({"pk": f"plan#{gen:010d}", "generation": gen, "status": "RESTORED",
                       "created_at": iso(ep.created[gen]), "ledger": ep.archive[gen], "endpoints_ref": "endpoint-table/v1",
                       "endpoint_count": len(ep.order), "restored_at": iso(now), "restored_from": _archive_url(gen),
                       "restored_by": caller.get("role", "unknown")}, **self._as(caller))
        return f"restored plan gen={gen} from {_archive_url(gen)} (status RESTORED)\n", 0
