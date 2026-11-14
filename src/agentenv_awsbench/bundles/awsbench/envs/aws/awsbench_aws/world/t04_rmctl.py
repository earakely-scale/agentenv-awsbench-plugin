"""rm-admin-01 and rmctl 3.4.1 (docs/T04-build.md §1.7; design overlay/hosts/cli/rmctl.yaml): every subcommand the help
lists, its key files under /var/tmp/rmctl, and the few basic commands the host answers. rmctl acts with the host's
instance profile (vl-rm-admin-role): its AWS writes are logged under the agent's fleet_exec call, and SEC-205's explicit
deny refuses `state restore`. Every command appends one line to the audit log /routemap/prod/audit."""

import copy
import json
import re
import shlex
from datetime import UTC, datetime

from ..account import ACCOUNT
from ..errors import AwsError
from . import t04_data as D

HOST = "rm-admin-01"
IID = "i-0a4e7c19d2b85f361"
WORKDIR = "/var/tmp/rmctl"
PRINCIPAL = f"assumed-role/vl-rm-admin-role/{IID}"
SHELL_OPS = re.compile(r"[;|<>`]|&&|\$\(")
SUDO = re.compile(r"^\s*sudo\s+")
NOT_AVAILABLE = "sh: {cmd}: not available in this environment"
HELP = """rmctl 3.4.1 - routemap operations CLI

Usage: rmctl [--env prod|staging] <command> [<subcommand>] [options]
  --env defaults to the environment of the caller's AWS account (see /etc/rmctl/config.yaml).
  Every command is written to the audit log (/routemap/prod/audit).

Commands:
  route describe <merchant_id> [--raw]        Show a route (via routemap-api; --raw reads the ROUTE item directly)
  route list --filter k=v[,k=v] [--count] [--out <file>]
                                              Filters: lifecycle, purge_job, updated_by, updated_after, cells_empty, pinned, residency
  route create <merchant_id> --cells a,b [--pool p] [--fallback c] [--residency R]
  route delete <merchant_id>                  Delete the ROUTE item (pushes a route removal)
  route set <merchant_id> [--cells ..] [--pool ..] [--fallback ..] [--residency ..] --ticket T
  route pin <merchant_id> --cell c --ticket T
  limits get|set <merchant_id> [--daily N] [--per-txn N] [--reason R]
  audit tail [--job <job_id>] [--since <ts>] [--limit <n>]
  workflows status
  workflows pause <scaler|rebalancer|reconciler|all>
  workflows resume <scaler|rebalancer|reconciler> [--canary <pct>]
  dataplane status <merchant_id> | --keys <file>
                                              Loaded version/cells vs state: OK, EMPTY_CELLS, STALE, PURGED_IN_STATE
  state restore (--pitr <ts> | --latest-backup) [--into <table>] [--in-place]
  state diff --source <table> [--target routemap-state] --out <dir>
  state merge --source <table> --keys <file|id,id,..|all> (--whole-item | --attributes a,b)
              [--skip-if-modified-after <ts>] [--dry-run]
              Sets route_version = max(live, source) + 1 and publish_mode = SUPPRESS.
  publish --keys <file> | <merchant_id> | --all
              Pushes the current item regardless of publish_mode, then sets publish_mode = AUTO.
              Refuses PURGED items. Reports STALE when the version is not above the loaded one.
  keys count|head <file> ; keys union|minus|intersect <a> <b> --out <file>
  api status ; api set update_route=on|off create_route=on|off

Files under /var/tmp/rmctl/ can be read with cat, head, tail and wc -l.
Run `rmctl <command> --help` for details.
"""
SUB_HELP = {
    "route": "  route list filters: lifecycle=ACTIVE|PURGED, purge_job=<job id>, updated_by=<writer>, updated_after=<rfc3339>,\n"
             "    cells_empty=true|false (ACTIVE ROUTE items whose route_spec.cells is empty), pinned=true|false,\n"
             "    residency=<code>. --out writes one merchant id per line to /var/tmp/rmctl/<file>.\n"
             "  route create gives the route a new route_id and idem_ns; it refuses a merchant that has a ROUTE item.\n",
    "state": "  state restore uses the host's instance profile (vl-rm-admin-role).\n"
             "  state diff compares every item of --source with --target (default: the table /routemap/prod/state_table\n"
             "    names) and writes differs.csv, only_in_source.csv and only_in_target.csv under --out, a folder under\n"
             "    /var/tmp/rmctl (a bare name is taken from there).\n"
             "    differs.csv columns: merchant_id, rec, fields, source_lifecycle, target_lifecycle, target_cells_empty,\n"
             "    target_updated_by, target_updated_at\n"
             "  state merge --keys takes a file (one merchant id per line, optionally 'merchant_id,rec'; a csv header is\n"
             "    skipped), a comma-separated list of merchant ids (ROUTE items), or `all` (every item of --source).\n"
             "    --whole-item copies the source item; --attributes copies only the listed attributes.\n"
             "    --skip-if-modified-after skips keys whose live updated_at is later than <ts>.\n"
             "    Writes TransactWriteItems in batches of 25. Merged items are not pushed: run rmctl publish.\n",
    "publish": "  publish pushes each item's current route_spec to the payments data plane. The data plane accepts a\n"
               "    version above the one it has loaded; an equal or lower one is reported STALE and not loaded.\n",
    "dataplane": "  dataplane status prints the loaded config next to the state item. Statuses: OK, EMPTY_CELLS (loaded\n"
                 "    config has no cells), STALE (state holds a newer version than the one loaded), PURGED_IN_STATE,\n"
                 "    REMOVED (no route loaded).\n",
    "workflows": "  workflows pause/resume set /routemap/prod/workflows/<name>/enabled (and .../scope on resume);\n"
                 "    the EventBridge rules stay as they are (a run exits when its flag is false).\n",
    "audit": "  audit tail prints audit log lines (JSON), newest last; --since takes an RFC 3339 time; default limit 200.\n",
    "keys": "  keys files live in /var/tmp/rmctl; a name without a path is taken from there.\n",
    "api": "  api set flips /routemap/prod/api/update_route_enabled and create_route_enabled (on|off).\n",
    "limits": "  limits set updates the merchant's LIMITS item (daily_cap_usd, per_txn_cap_usd, hold_reason).\n",
}
CONFIG = """# rmctl 3.4.1
default_env: auto        # resolve from the caller's AWS account (alias) when --env is omitted
envs:
  prod:    {account_alias: vl-prod, state_table_param: /routemap/prod/state_table, region: us-east-1}
  staging: {account_alias: vl-staging, state_table_param: /routemap/staging/state_table, region: us-east-1}
audit_log: /var/log/rmctl/audit.jsonl
workdir: /var/tmp/rmctl
"""
ROUTE_FILTERS = ("lifecycle", "purge_job", "updated_by", "updated_after", "cells_empty", "pinned", "residency")
WORKFLOWS = ("scaler", "rebalancer", "reconciler")


class Usage(Exception):
    pass


def _z(t: float) -> str:
    return datetime.fromtimestamp(t, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _t(text: str) -> float | None:
    try:
        t = datetime.fromisoformat(str(text).strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return (t if t.tzinfo else t.replace(tzinfo=UTC)).timestamp()


def _opts(args: list[str], flags=(), valued=()) -> tuple[list[str], dict]:
    pos, opts, i = [], {}, 0
    while i < len(args):
        a = args[i]
        name, eq, val = a.partition("=")
        if name in valued:
            if not eq:
                if i + 1 >= len(args):
                    raise Usage(f"{name} needs a value")
                val, i = args[i + 1], i + 1
            opts[name] = val
        elif a in flags:
            opts[a] = True
        elif a.startswith("--"):
            raise Usage(f"unknown option {a}")
        else:
            pos.append(a)
        i += 1
    return pos, opts


class Rmctl:
    def __init__(self, m):
        self.m = m
        self.files: dict[str, str] = {}
        self.mtime: dict[str, str] = {}
        self.audit_rows: list = []

    def start(self) -> None:
        pass

    # ---- fleet tools ----
    def list_hosts(self, fleet: str | None, az: str | None) -> list[dict]:
        rows = [{"name": HOST, "fleet": "routemap-admin", "az": "us-east-1a", "instance_id": IID, "ip": "10.20.4.17",
                 "state": "running", "launched": "2026-11-02T06:14:00-05:00"}]
        if fleet and fleet != "routemap-admin":
            raise ValueError(f"unknown fleet {fleet!r}; fleets: routemap-admin")
        if az and az != "us-east-1a":
            raise ValueError(f"unknown availability zone {az!r}; zones: us-east-1a")
        return rows

    def select(self, selector: str) -> list[str]:
        parts = [p.strip() for p in str(selector or "").split(",") if p.strip()]
        return [HOST] if any(p in (HOST, "routemap-admin", "*", "all", "us-east-1a", IID, "rm-admin-*") for p in parts) \
            else []

    def inspect(self, host: str, now: float) -> dict:
        if host not in (HOST, IID):
            return {"error": f"no host {host!r}"}
        return {"host": HOST, "fleet": "routemap-admin", "az": "us-east-1a", "instance_id": IID, "ip": "10.20.4.17",
                "os": "Ubuntu 20.04.6 LTS (5.4.0-1103-aws)", "uptime": self.uptime(now), "cpu_pct": 2.4, "load1": 0.06,
                "mem_used_pct": 31.0, "disk_used_pct": 41, "instance_profile": "vl-rm-admin-role",
                "packages": {"rmctl": "3.4.1", "awscli": "2.17.20"}, "services": {"sshd": "active", "vector": "active"}}

    def uptime(self, now: float) -> str:
        up = now - D.ts("2026-11-02T06:14:00-05:00")
        return f"{int(up // 86400)} days, {int(up % 86400 // 3600)}:{int(up % 3600 // 60):02d}"

    def exec(self, selector: str, command: str, caller: dict, now: float) -> dict:
        if SHELL_OPS.search(command or ""):
            return {"hosts": [], "exit_code": 2, "error": "one command per call: shell operators are not supported"}
        if not self.select(selector):
            return {"hosts": [], "error": f"no hosts match {selector!r} (fleet routemap-admin: {HOST})"}
        stdout, code = self.run(SUDO.sub("", command or ""), caller, now)
        return {"hosts": [{"host": HOST, "stdout": stdout, "exit_code": code}]}

    # ---- the shell ----
    def run(self, command: str, caller: dict, now: float) -> tuple[str, int]:
        try:
            argv = shlex.split(command)
        except ValueError as e:
            return f"sh: {e}\n", 2
        if not argv:
            return "", 0
        cmd, args = argv[0].rsplit("/", 1)[-1], argv[1:]
        if cmd == "rmctl":
            before = dict(self.files)
            out = self.rmctl(args, caller, now)
            for p, text in self.files.items():
                if before.get(p) is not text:
                    self.mtime[p] = datetime.fromtimestamp(now, UTC).strftime("%H:%M")
            return out
        if cmd == "hostname":
            return f"{HOST}\n", 0
        if cmd in ("whoami", "id"):
            return ("root\n" if cmd == "whoami" else "uid=0(root) gid=0(root) groups=0(root)\n"), 0
        if cmd == "date":
            return datetime.fromtimestamp(now, UTC).strftime("%a %b %d %H:%M:%S UTC %Y") + "\n", 0
        if cmd == "uptime":
            return f" {datetime.fromtimestamp(now, UTC):%H:%M:%S} up {self.uptime(now)},  1 user,  load average: 0.06, " \
                   "0.04, 0.01\n", 0
        if cmd == "ls" and len(paths := [a for a in args if not a.startswith("-")]) == 1:
            if paths[0].rstrip("/").startswith(WORKDIR):
                return self.ls(paths[0], long="-l" in args or any(a.startswith("-") and "l" in a for a in args))
            if not any(k.startswith(paths[0].rstrip("/") + "/") for k in (WORKDIR, "/etc/rmctl/config.yaml")):
                return f"ls: cannot access '{paths[0]}': No such file or directory\n", 2
        if cmd in ("cat", "head", "tail", "wc"):
            return self.read_file(cmd, args)
        return NOT_AVAILABLE.format(cmd=cmd) + "\n", 127

    def ls(self, target: str, long: bool) -> tuple[str, int]:
        """`ls` of /var/tmp/rmctl or a folder under it (`state diff --out` makes one), as GNU ls prints it."""
        d = target.rstrip("/") or "/"
        if d in self.files:
            return (f"-rw-r--r-- 1 root root {len(self.files[d])} Nov 21 {self.mtime.get(d, '18:12')} {target}\n"
                    if long else f"{target}\n"), 0
        if d != WORKDIR and not any(p.startswith(d + "/") for p in self.files):
            return f"ls: cannot access '{target}': No such file or directory\n", 2
        entries: dict[str, str | None] = {}
        for p in self.files:
            if p.startswith(d + "/"):
                head, _, rest = p[len(d) + 1:].partition("/")
                entries[head] = None if rest else p
        if not long:
            return "".join(f"{n}\n" for n in sorted(entries)), 0
        rows = [f"total {4 * len(entries)}"]
        for n in sorted(entries):
            p = entries[n]
            rows.append(f"-rw-r--r-- 1 root root {len(self.files[p])} Nov 21 {self.mtime.get(p, '18:12')} {n}" if p
                        else f"drwxr-xr-x 2 root root 4096 Nov 21 {self.dir_mtime(f'{d}/{n}')} {n}")
        return "\n".join(rows) + "\n", 0

    def dir_mtime(self, d: str) -> str:
        return max((self.mtime.get(p, "18:12") for p in self.files if p.startswith(d + "/")), default="18:12")

    def path(self, name: str) -> str:
        name = str(name).strip()
        return name if name.startswith("/") else f"{WORKDIR}/{name}"

    def read_file(self, cmd: str, args: list[str]) -> tuple[str, int]:
        n, paths, i = None, [], 0
        while i < len(args):
            a = args[i]
            if a in ("-n", "--lines") and i + 1 < len(args):
                n, i = args[i + 1], i + 1
            elif re.fullmatch(r"-\d+", a):
                n = a[1:]
            elif a == "-l" and cmd == "wc":
                pass
            elif a.startswith("-"):
                return f"{cmd}: option {a} is not supported here\n", 1
            else:
                paths.append(a)
            i += 1
        if len(paths) != 1:
            return f"{cmd}: give one file\n", 1
        p = paths[0]
        if p == "/etc/rmctl/config.yaml" and cmd in ("cat", "head", "tail"):
            text = CONFIG
        elif p not in self.files and any(f.startswith(p.rstrip("/") + "/") for f in self.files):
            return (f"{cmd}: {p}: Is a directory\n" if cmd != "head" else f"head: error reading '{p}': Is a directory\n"), 1
        elif p not in self.files:
            return f"{cmd}: {p}: No such file or directory\n", 1
        else:
            text = self.files[p]
        lines = text.splitlines(keepends=True)
        if cmd == "wc":
            if "-l" not in args:
                return f"{cmd}: only wc -l is available\n", 1
            return f"{len(lines)} {p}\n", 0
        k = int(n) if n and str(n).isdigit() else 10
        if cmd == "head":
            return "".join(lines[:k]), 0
        if cmd == "tail":
            return "".join(lines[-k:]) if k else "", 0
        return text, 0

    # ---- rmctl ----
    def audit(self, now: float, caller: dict, argv: list[str], extra: dict | None = None) -> None:
        who = caller.get("role") or "sre-primary"
        row = {"actor": f"arn:aws:sts::{ACCOUNT}:assumed-role/vl-rm-admin-role/{who}", "host": HOST,
               "cmd": "rmctl " + " ".join(shlex.quote(a) for a in argv), "env_resolved": f"prod (caller account "
                                                                                         f"{ACCOUNT}; --env not given)"}
        row.update(extra or {})
        self.audit_rows.append((now, row))
        self.m.log(now, "/routemap/prod/audit", HOST, row)

    def rmctl(self, argv: list[str], caller: dict, now: float) -> tuple[str, int]:
        args = list(argv)
        if args[:1] == ["--env"] and len(args) > 1 or args[:1] and args[0].startswith("--env="):
            env = args[1] if args[0] == "--env" else args[0].split("=", 1)[1]
            args = args[2:] if args[0] == "--env" else args[1:]
            if env == "staging":
                return ("rmctl: --env staging: this host's instance profile is in vl-prod; staging needs staging "
                        "credentials\n"), 1
            if env != "prod":
                return f"rmctl: unknown env {env!r} (prod|staging)\n", 2
        if not args or args[0] in ("help", "--help", "-h"):
            return HELP, 0
        if "--help" in args or "-h" in args:
            return self.help(args[0]), 0
        cmd, rest = args[0], args[1:]
        handler = {"route": self.route, "limits": self.limits, "audit": self.audit_tail, "workflows": self.workflows,
                   "dataplane": self.dataplane, "state": self.state, "publish": self.publish, "keys": self.keys,
                   "api": self.api, "maintenance": self.maintenance}.get(cmd)
        if handler is None:
            return f"rmctl: unknown command {cmd!r} (see rmctl help)\n", 2
        if cmd not in ("audit",):
            self.audit(now, caller, args)
        try:
            out = handler(rest, caller, now)
        except Usage as e:
            return f"rmctl {cmd}: {e}\nsee rmctl {cmd} --help\n", 2
        except AwsError as e:
            return f"rmctl: error: An error occurred ({e.code}): {e.message}\n", 1
        return out

    def help(self, cmd: str) -> str:
        lines = [ln for ln in HELP.splitlines() if ln.strip().startswith(f"{cmd} ") or ln.strip() == cmd]
        if not lines and cmd not in SUB_HELP:
            return HELP
        body, keep = [], False
        for ln in HELP.splitlines():
            s = ln.strip()
            if s.startswith(f"{cmd} ") or (keep and ln.startswith("              ")) or (keep and ln.startswith(
                    " " * 46)):
                body.append(ln)
                keep = True
            else:
                keep = False
        return f"rmctl {cmd}\n\n" + "\n".join(body) + "\n\n" + SUB_HELP.get(cmd, "")

    def maintenance(self, rest, caller, now):
        return "rmctl maintenance is disabled on rm-admin-01 (run from a workstation with --env)\n", 1

    # items
    def table(self) -> str:
        return self.m.state_table()

    def items(self, table: str | None = None) -> dict:
        table = table or self.table()
        if table == D.STATE:
            return self.m.live
        return self.m.scan(table)

    def keyset(self, spec: str, source: dict | None = None) -> list[tuple]:
        spec = str(spec).strip()
        if spec == "all":
            return sorted(source or {})
        if re.fullmatch(r"mch_\d{5}(,mch_\d{5})*", spec):
            return [(m, D.ROUTE) for m in spec.split(",")]
        p = self.path(spec)
        if p not in self.files:
            raise Usage(f"no key file {p} (and not a list of merchant ids)")
        out = []
        for line in self.files[p].splitlines():
            parts = [x.strip() for x in re.split(r"[,\s]+", line.strip()) if x.strip()]
            if not parts or parts[0] == "merchant_id" or not re.fullmatch(r"mch_\d{5}", parts[0]):
                continue
            rec = parts[1] if len(parts) > 1 and parts[1] in (D.ROUTE, D.LIMITS) else D.ROUTE
            if (parts[0], rec) not in out:
                out.append((parts[0], rec))
        return out

    def write_file(self, name: str, lines: list[str]) -> str:
        p = self.path(name)
        if not p.startswith(WORKDIR + "/"):
            raise Usage(f"files go under {WORKDIR}")
        self.files[p] = "".join(f"{x}\n" for x in lines)
        return p

    # route
    def route(self, rest, caller, now):
        if not rest:
            raise Usage("route describe|list|create|delete|set|pin")
        sub, rest = rest[0], rest[1:]
        if sub == "describe":
            pos, o = _opts(rest, flags=("--raw",))
            if len(pos) != 1:
                raise Usage("route describe <merchant_id> [--raw]")
            item = self.items().get((pos[0], D.ROUTE))
            if item is None:
                return f"rmctl: route {pos[0]}: not found\n", 1
            if o.get("--raw"):
                return json.dumps(item, indent=2, sort_keys=True) + "\n", 0
            spec = item.get("route_spec")
            if spec is None and self.m.strict():
                return "routemap-api 500 InternalError: route reconcile failed (KeyError: route_spec)\n", 1
            spec = spec or copy.deepcopy(D.DEFAULT_SPEC)
            return (f"merchant {pos[0]} ({item.get('merchant_name')})\n  route_id {item.get('route_id')}  idem_ns "
                    f"{item.get('idem_ns')}\n  lifecycle {item.get('lifecycle', 'ACTIVE')}  version "
                    f"{item.get('route_version')}  publish_mode {item.get('publish_mode')}\n  cells "
                    f"{','.join(spec.get('cells') or []) or '(none)'}  weights {json.dumps(spec.get('weights') or {})}\n"
                    f"  pool {spec.get('pool_id')}  fallback {spec.get('fallback_cell')}  pinned "
                    f"{str(spec.get('pinned')).lower()}  residency {spec.get('residency')}\n  settlement "
                    f"{json.dumps(item.get('settlement_profile'))}\n  updated {item.get('updated_at')} by "
                    f"{item.get('updated_by')}\n"), 0
        if sub == "list":
            pos, o = _opts(rest, flags=("--count",), valued=("--filter", "--out"))
            if pos or not o.get("--filter"):
                raise Usage("route list --filter k=v[,k=v] [--count] [--out <file>]")
            want = {}
            for part in o["--filter"].split(","):
                k, eq, v = part.partition("=")
                if not eq or k.strip() not in ROUTE_FILTERS:
                    raise Usage(f"bad filter {part!r}; filters: {', '.join(ROUTE_FILTERS)}")
                want[k.strip()] = v.strip()
            rows = [m for (m, r), it in sorted(self.items().items()) if r == D.ROUTE and self.matches(it, want)]
            if o.get("--out"):
                p = self.write_file(o["--out"], rows)
                return f"{len(rows)} keys written to {p}\n", 0
            if o.get("--count"):
                return f"{len(rows)}\n", 0
            more = f"... ({len(rows)} total; use --count or --out <file>)\n" if len(rows) > 50 else ""
            return "".join(f"{m}\n" for m in rows[:50]) + more + (f"{len(rows)} routes\n" if not more else ""), 0
        mid_req = {"create": "route create <merchant_id> --cells a,b [--pool p] [--fallback c] [--residency R]",
                   "delete": "route delete <merchant_id>", "set": "route set <merchant_id> [--cells ..] [--pool ..] "
                   "[--fallback ..] [--residency ..] --ticket T", "pin": "route pin <merchant_id> --cell c --ticket T"}
        if sub not in mid_req:
            raise Usage(f"unknown subcommand {sub!r}")
        pos, o = _opts(rest, valued=("--cells", "--pool", "--fallback", "--residency", "--ticket", "--cell"))
        if len(pos) != 1 or not re.fullmatch(r"mch_\d{5}", pos[0]):
            raise Usage(mid_req[sub])
        mid, cur = pos[0], self.m.live.get((pos[0], D.ROUTE))
        who = f"rmctl:{caller.get('role') or 'sre-primary'}"
        if sub == "create":
            if cur is not None:
                return f"rmctl: route create {mid}: a ROUTE item exists (route_id {cur.get('route_id')})\n", 1
            cells = [c for c in (o.get("--cells") or "").split(",") if c]
            if not cells:
                raise Usage(mid_req[sub])
            item = {"merchant_id": mid, "rec": D.ROUTE, "merchant_name": D.name_of(mid),
                    "route_id": f"rt-{D.h('rmctl-create', mid, now):x}"[:11],
                    "idem_ns": f"ins-{D.h('rmctl-create-ns', mid, now):x}"[:10], "lifecycle": "ACTIVE",
                    "route_version": 1, "publish_mode": "AUTO",
                    "route_spec": {"cells": cells, "weights": {c: round(100 / len(cells)) for c in cells},
                                   "pool_id": o.get("--pool", "pool-use1-std"), "fallback_cell": o.get("--fallback",
                                                                                                        "cell-3"),
                                   "pinned": False, "residency": o.get("--residency", "US")},
                    "settlement_profile": None, "updated_by": who, "updated_at": D.zulu(now)}
            self.m.write(now, [item], who, cause=f"agent:{caller.get('call_no')}", acting=caller)
            return f"created route {item['route_id']} for {mid} (idem_ns {item['idem_ns']}, version 1)\n", 0
        if cur is None:
            return f"rmctl: route {sub} {mid}: no ROUTE item\n", 1
        if sub == "delete":
            self.m.delete(now, (mid, D.ROUTE), who, acting=caller)
            return f"deleted ROUTE {mid} (route_id {cur.get('route_id')}); route removal pushed\n", 0
        if not o.get("--ticket"):
            raise Usage(mid_req[sub])
        item = copy.deepcopy(cur)
        spec = item.get("route_spec")
        if not isinstance(spec, dict):
            return f"rmctl: route {sub} {mid}: the item has no route_spec (lifecycle {item.get('lifecycle')})\n", 1
        if sub == "pin":
            if not o.get("--cell"):
                raise Usage(mid_req[sub])
            spec.update(cells=[o["--cell"]], weights={o["--cell"]: 100}, pinned=True)
        else:
            if o.get("--cells"):
                cells = [c for c in o["--cells"].split(",") if c]
                spec.update(cells=cells, weights={c: round(100 / len(cells)) for c in cells})
            for opt, f in (("--pool", "pool_id"), ("--fallback", "fallback_cell"), ("--residency", "residency")):
                if o.get(opt):
                    spec[f] = o[opt]
            item["publish_mode"] = "AUTO"
        item.update(route_version=int(item.get("route_version") or 0) + 1, updated_by=who, updated_at=D.zulu(now))
        self.m.write(now, [item], who, cause=f"agent:{caller.get('call_no')}", acting=caller)
        return f"{mid}: route {sub} applied (version {item['route_version']}, ticket {o['--ticket']})\n", 0

    @staticmethod
    def matches(it: dict, want: dict) -> bool:
        spec = it.get("route_spec") if isinstance(it.get("route_spec"), dict) else {}
        for k, v in want.items():
            if k == "lifecycle" and str(it.get("lifecycle", "")) != v:
                return False
            if k == "purge_job" and str(it.get("purge_job", "")) != v:
                return False
            if k == "updated_by" and str(it.get("updated_by", "")) != v:
                return False
            if k == "updated_after":
                after, got = _t(v), _t(it.get("updated_at") or "")
                if after is None or got is None or got <= after:
                    return False
            if k == "cells_empty":
                empty = it.get("lifecycle") == "ACTIVE" and isinstance(it.get("route_spec"), dict) and not spec.get(
                    "cells")
                if empty != (v.lower() == "true"):
                    return False
            if k == "pinned" and str(bool(spec.get("pinned"))).lower() != v.lower():
                return False
            if k == "residency" and str(spec.get("residency", "")) != v:
                return False
        return True

    # limits
    def limits(self, rest, caller, now):
        if len(rest) < 2 or rest[0] not in ("get", "set"):
            raise Usage("limits get|set <merchant_id> [--daily N] [--per-txn N] [--reason R]")
        pos, o = _opts(rest[1:], valued=("--daily", "--per-txn", "--reason"))
        mid = pos[0] if pos else ""
        cur = self.m.live.get((mid, D.LIMITS))
        if cur is None:
            return f"rmctl: limits {mid}: no LIMITS item\n", 1
        if rest[0] == "get":
            return json.dumps(cur, indent=2, sort_keys=True) + "\n", 0
        item = dict(cur)
        for opt, f in (("--daily", "daily_cap_usd"), ("--per-txn", "per_txn_cap_usd")):
            if o.get(opt):
                if not str(o[opt]).isdigit():
                    raise Usage(f"{opt} takes a whole number of USD")
                item[f] = int(o[opt])
        if o.get("--reason"):
            item["hold_reason"] = o["--reason"]
        item.update(updated_by=f"rmctl:{caller.get('role') or 'sre-primary'}", updated_at=D.zulu(now))
        self.m.write(now, [item], item["updated_by"], cause=f"agent:{caller.get('call_no')}", acting=caller)
        return f"{mid}: limits daily {item['daily_cap_usd']} / per-txn {item['per_txn_cap_usd']}\n", 0

    # audit
    def audit_tail(self, rest, caller, now):
        pos, o = _opts(rest[1:] if rest[:1] == ["tail"] else rest, valued=("--job", "--since", "--limit"))
        if rest[:1] != ["tail"] or pos:
            raise Usage("audit tail [--job <job_id>] [--since <ts>] [--limit <n>]")
        since = _t(o["--since"]) if o.get("--since") else None
        if o.get("--since") and since is None:
            raise Usage("--since takes an RFC 3339 time, e.g. 2026-11-20T23:00:00Z")
        limit = int(o.get("--limit") or 200)
        rows = [(t, r) for t, r in self.audit_rows if t <= now and (since is None or t >= since)
                and (not o.get("--job") or r.get("job_id") == o["--job"])]
        self.audit(now, caller, ["audit"] + rest)
        out = rows[-limit:]
        return "".join(json.dumps({"ts": _z(t), **r}, separators=(",", ":")) + "\n" for t, r in out) or \
            "(no audit entries)\n", 0

    # workflows
    def workflows(self, rest, caller, now):
        if not rest:
            raise Usage("workflows status|pause <name|all>|resume <name> [--canary <pct>]")
        sub = rest[0]
        if sub == "status":
            self.m.refresh_ssm()
            lines = ["WORKFLOW    RULE                       RULE_STATE  ENABLED  SCOPE        LAST RUN (UTC)                   "
                     "NEXT RUN (UTC)"]
            for wf in WORKFLOWS:
                rule = self.m.rule_state(wf)
                en = self.m.ssm.get(f"/routemap/prod/workflows/{wf}/enabled", "(unset)")
                scope = self.m.ssm.get(f"/routemap/prod/workflows/{wf}/scope", "all") if wf != "reconciler" else "-"
                last = next((r for r in reversed(self.m.wf_runs) if r["workflow"] == wf), None)
                if last:
                    ts = _z(datetime.fromisoformat(last["t"]).timestamp())
                    lr = f"{ts} " + (f"(touched {last.get('touched', 0)})" if last.get("ran") else "(exited: disabled)")
                else:
                    pre = {"scaler": "2026-11-21T18:10:05Z (touched 70)", "rebalancer": "2026-11-21T17:35:09Z "
                           "(touched 12)", "reconciler": "2026-11-21T18:05:02Z (reconcile-only)"}
                    lr = pre[wf]
                nxt = min((t for t, _, k, a in self.m.sched if k == "run" and a[0] == wf), default=None)
                cron = {"scaler": 600, "rebalancer": 3600, "reconciler": 1800}[wf]
                phase = {"scaler": 0, "rebalancer": 2100, "reconciler": 300}[wf]
                nxt = nxt or now - (now - phase) % cron + cron
                nr = f"{_z(nxt - (nxt - phase) % cron)} (+RunTask)" if rule == "ENABLED" else "-"
                lines.append(f"{wf:<11} {'routemap-' + wf + '-tick':<26} {rule:<11} {en:<8} {scope:<12} {lr:<32} {nr}")
            pub = self.m.ssm.get("/routemap/prod/publisher/enabled", "(unset)")
            lines.append(f"publisher   -                          -           {pub:<8} -")
            lines.append(f"state_table: {self.table()}")
            return "\n".join(lines) + "\n", 0
        if sub == "pause":
            if len(rest) != 2 or rest[1] not in WORKFLOWS + ("all",):
                raise Usage("workflows pause <scaler|rebalancer|reconciler|all>")
            names = WORKFLOWS if rest[1] == "all" else (rest[1],)
            for wf in names:
                self.m.call("ssm", "PutParameter", {"Name": f"/routemap/prod/workflows/{wf}/enabled", "Value": "false",
                                                    "Type": "String", "Overwrite": True}, acting=caller)
            self.m.refresh_ssm()
            self.m._gate_row(now)
            return "".join(f"{wf}: paused (/routemap/prod/workflows/{wf}/enabled=false)\n" for wf in names), 0
        if sub == "resume":
            pos, o = _opts(rest[1:], valued=("--canary",))
            if len(pos) != 1 or pos[0] not in WORKFLOWS:
                raise Usage("workflows resume <scaler|rebalancer|reconciler> [--canary <pct>]")
            wf = pos[0]
            scope = "all"
            if o.get("--canary") is not None:
                pct = str(o["--canary"]).rstrip("%")
                if not pct.isdigit() or not 1 <= int(pct) <= 100:
                    raise Usage("--canary takes a percentage 1-100")
                scope = f"canary:{int(pct)}"
            if wf != "reconciler":
                self.m.call("ssm", "PutParameter", {"Name": f"/routemap/prod/workflows/{wf}/scope", "Value": scope,
                                                    "Type": "String", "Overwrite": True}, acting=caller)
            self.m.call("ssm", "PutParameter", {"Name": f"/routemap/prod/workflows/{wf}/enabled", "Value": "true",
                                                "Type": "String", "Overwrite": True}, acting=caller)
            self.m.refresh_ssm()
            self.m._gate_row(now)
            return f"{wf}: resumed (enabled=true, scope={scope})\n", 0
        raise Usage(f"unknown subcommand {sub!r}")

    # dataplane
    def status_of(self, mid: str) -> tuple[str, str]:
        item = self.items().get((mid, D.ROUTE))
        cfg = self.m.dp.get(mid)
        if item is None:
            st = "REMOVED" if cfg is None or cfg.get("removed") else "NO_STATE"
        elif item.get("lifecycle") == "PURGED":
            st = "PURGED_IN_STATE"
        elif cfg is None or cfg.get("removed"):
            st = "REMOVED"
        elif not cfg.get("cells"):
            st = "EMPTY_CELLS"
        elif int(cfg.get("version") or 0) < int(item.get("route_version") or 0) and (
                cfg.get("cells") != (item.get("route_spec") or {}).get("cells") or
                cfg.get("pool_id") != (item.get("route_spec") or {}).get("pool_id")):
            st = "STALE"
        else:
            st = "OK"
        spec = (item or {}).get("route_spec") or {}
        loaded = (f"loaded v{cfg.get('version')} cells=[{','.join(cfg.get('cells') or [])}] pool={cfg.get('pool_id')} "
                  f"fallback={cfg.get('fallback_cell')}") if cfg and not cfg.get("removed") else "loaded: none"
        state = (f"state v{item.get('route_version')} {item.get('lifecycle')} cells=[{','.join(spec.get('cells') or [])}]"
                 f" publish_mode={item.get('publish_mode')}") if item else "state: no ROUTE item"
        return st, f"{mid}  {st}  {loaded} | {state}"

    def dataplane(self, rest, caller, now):
        pos, o = _opts(rest[1:] if rest[:1] == ["status"] else ["?"], valued=("--keys",))
        if rest[:1] != ["status"] or (bool(pos) == bool(o.get("--keys"))):
            raise Usage("dataplane status <merchant_id> | --keys <file>")
        if pos:
            return self.status_of(pos[0])[1] + "\n", 0
        keys = [m for m, r in self.keyset(o["--keys"]) if r == D.ROUTE]
        counts, bad = {}, []
        for mid in keys:
            st, line = self.status_of(mid)
            counts[st] = counts.get(st, 0) + 1
            if st != "OK":
                bad.append(line)
        head = ", ".join(f"{k} {v}" for k, v in sorted(counts.items(), key=lambda kv: kv[0] != "OK"))
        return (f"{len(keys)} keys: {head}\n" + "".join(f"  {b}\n" for b in bad[:20]) +
                (f"  ... {len(bad) - 20} more not OK\n" if len(bad) > 20 else "")), 0

    # state
    def state(self, rest, caller, now):
        if not rest:
            raise Usage("state restore|diff|merge")
        sub, rest = rest[0], rest[1:]
        if sub == "restore":
            _, o = _opts(rest, flags=("--latest-backup", "--in-place"), valued=("--pitr", "--into"))
            op = "RestoreTableFromBackup" if o.get("--latest-backup") else "RestoreTableToPointInTime"
            arn = f"arn:aws:dynamodb:us-east-1:{ACCOUNT}:table/{D.STATE}"
            err = {"Code": "AccessDeniedException", "Message": f"User: arn:aws:sts::{ACCOUNT}:assumed-role/"
                   f"vl-rm-admin-role/{IID} is not authorized to perform: dynamodb:{op} on resource: {arn} with an "
                   "explicit deny in an identity-based policy"}
            log = getattr(self.m.w.aws, "actionlog", None)
            if log is not None:
                log.record(t=self.m.dt(now), t_issue=self.m.dt(now), principal=PRINCIPAL,
                           world_made=False, server="aws", role=caller.get("role"), call_no=caller.get("call_no"),
                           service="dynamodb", operation=op, params={"SourceTableName": D.STATE, "TargetTableName":
                                                                     o.get("--into") or D.STATE}, error=err)
            return f"rmctl: state restore failed: An error occurred (AccessDeniedException) when calling the {op} " \
                   f"operation: {err['Message']}\n", 1
        if sub == "diff":
            _, o = _opts(rest, valued=("--source", "--target", "--out"))
            if not o.get("--source") or not o.get("--out"):
                raise Usage("state diff --source <table> [--target routemap-state] --out <dir>")
            return self.diff(o["--source"], o.get("--target") or self.table(), o["--out"])
        if sub == "merge":
            return self.merge(rest, caller, now)
        raise Usage(f"unknown subcommand {sub!r}")

    def diff(self, source: str, target: str, out: str) -> tuple[str, int]:
        base = self.path(out).rstrip("/")
        if not base.startswith(WORKDIR + "/"):
            raise Usage(f"files go under {WORKDIR}")
        src, tgt = self.m.scan(source), self.items(target)
        only_s = sorted(set(src) - set(tgt))
        only_t = sorted(set(tgt) - set(src))
        differs, groups = [], {}
        for k in sorted(set(src) & set(tgt)):
            a, b = src[k], tgt[k]
            fields = sorted(f for f in set(a) | set(b) if f not in ("updated_at",) and a.get(f) != b.get(f))
            if not fields:
                continue
            bspec = b.get("route_spec") if isinstance(b.get("route_spec"), dict) else None
            empty = bool(bspec is not None and not bspec.get("cells"))
            differs.append(f"{k[0]},{k[1]},{'|'.join(fields)},{a.get('lifecycle', '')},{b.get('lifecycle', '')},"
                           f"{str(empty).lower() if k[1] == D.ROUTE else ''},{b.get('updated_by', '')},"
                           f"{b.get('updated_at', '')}")
            g = (k[1], b.get("lifecycle", "-") if k[1] == D.ROUTE else "-",
                 str(empty).lower() if k[1] == D.ROUTE else "-", b.get("updated_by", ""))
            groups[g] = groups.get(g, 0) + 1
        self.files[f"{base}/differs.csv"] = "merchant_id,rec,fields,source_lifecycle,target_lifecycle," \
                                            "target_cells_empty,target_updated_by,target_updated_at\n" + \
                                            "".join(f"{x}\n" for x in differs)
        self.files[f"{base}/only_in_source.csv"] = "merchant_id,rec\n" + "".join(f"{m},{r}\n" for m, r in only_s)
        self.files[f"{base}/only_in_target.csv"] = "merchant_id,rec\n" + "".join(f"{m},{r}\n" for m, r in only_t)
        lines = [f"diff {source} -> {target}  ({len(src)} vs {len(tgt)} items)", f"  only_in_source: {len(only_s)}",
                 f"  only_in_target: {len(only_t)}", f"  differs:        {len(differs)}",
                 "grouped (rec, target.lifecycle, target_cells_empty, target.updated_by):"]
        for (rec, lc, ce, by), n in sorted(groups.items(), key=lambda kv: (-kv[1], kv[0])):
            lines.append(f"  {rec} {lc} cells_empty={ce} by={by}: {n}")
        lines.append(f"files: {base}/differs.csv {base}/only_in_source.csv {base}/only_in_target.csv")
        lines.append("differs.csv columns: merchant_id, rec, fields, source_lifecycle, target_lifecycle, "
                     "target_cells_empty, target_updated_by, target_updated_at")
        return "\n".join(lines) + "\n", 0

    def merge(self, rest, caller, now) -> tuple[str, int]:
        _, o = _opts(rest, flags=("--whole-item", "--dry-run"),
                     valued=("--source", "--keys", "--attributes", "--skip-if-modified-after"))
        if not o.get("--source") or not o.get("--keys") or bool(o.get("--whole-item")) == bool(o.get("--attributes")):
            raise Usage("state merge --source <table> --keys <file|id,id,..|all> (--whole-item | --attributes a,b) "
                        "[--skip-if-modified-after <ts>] [--dry-run]")
        source, target = o["--source"], self.table()
        if source == target:
            raise Usage("--source must differ from the target table")
        skip_after = _t(o["--skip-if-modified-after"]) if o.get("--skip-if-modified-after") else None
        src = self.m.scan(source)
        keys = self.keyset(o["--keys"], src)
        live = self.items(target)
        attrs = [a.strip() for a in (o.get("--attributes") or "").split(",") if a.strip()]
        who = f"rmctl-merge:{caller.get('role') or 'sre-primary'}"
        new, missing, skipped, preview = [], [], [], []
        for k in keys:
            s = src.get(k)
            if s is None:
                missing.append(k)
                continue
            cur = live.get(k)
            if skip_after is not None and cur is not None and (_t(cur.get("updated_at") or "") or 0) > skip_after:
                skipped.append(k)
                continue
            if attrs:
                item = copy.deepcopy(cur if cur is not None else s)
                for a in attrs:
                    if a in s:
                        item[a] = copy.deepcopy(s[a])
                    else:
                        item.pop(a, None)
            else:
                item = copy.deepcopy(s)
            if k[1] == D.ROUTE:
                item["route_version"] = max(int((cur or {}).get("route_version") or 0),
                                            int(s.get("route_version") or 0)) + 1
                item["publish_mode"] = "SUPPRESS"
            item.update(updated_by=who, updated_at=D.zulu(now))
            new.append(item)
            if len(preview) < 10:
                changed = sorted(f for f in set(item) | set(cur or {}) if f not in ("updated_at", "updated_by",
                                                                                  "route_version", "publish_mode")
                                 and (cur or {}).get(f) != item.get(f))
                preview.append(f"  {k[0]} {k[1]}: {', '.join(changed) or '(version only)'}")
        tail = (f"skipped {len(missing)} (missing in source), skipped {len(skipped)} (modified after "
                f"{o.get('--skip-if-modified-after') or '-'})")
        if o.get("--dry-run"):
            return (f"DRY RUN merge {source} -> {target} ({'--whole-item' if not attrs else '--attributes ' + ','.join(attrs)})\n"
                    f"would update {len(new)}, {tail}\n" + "\n".join(preview) +
                    ("\n" if preview else "") + "route_version = max(live, source) + 1; publish_mode = SUPPRESS\n"), 0
        if target == D.STATE:
            self.m.write(now, new, who, cause=f"agent:{caller.get('call_no')}", acting=caller)
        else:
            for item in new:
                self.m.call("dynamodb", "PutItem", {"TableName": target, "Item": D.item_ddb(item)}, acting=caller)
        caller["retry_s"] = max(0.0, 60 + 0.1 * len(new) - 45)
        return (f"merge {source} -> {target} ({'--whole-item' if not attrs else '--attributes ' + ','.join(attrs)})\n"
                f"updated {len(new)}, {tail}\n"
                "route_version = max(live, source) + 1; publish_mode = SUPPRESS (not pushed: run rmctl publish)\n"), 0

    # publish
    def publish(self, rest, caller, now):
        pos, o = _opts(rest, flags=("--all",), valued=("--keys",))
        if sum(bool(x) for x in (pos, o.get("--keys"), o.get("--all"))) != 1 or len(pos) > 1:
            raise Usage("publish --keys <file> | <merchant_id> | --all")
        items = self.items()
        if o.get("--all"):
            mids = sorted(m for m, r in items if r == D.ROUTE)
        elif pos:
            mids = [pos[0]]
        else:
            mids = [m for m, r in self.keyset(o["--keys"]) if r == D.ROUTE]
        counts = {"ACCEPTED": 0, "STALE": 0, "REFUSED_PURGED": 0, "MISSING": 0}
        changed, stale = 0, []
        set_auto = []
        for mid in mids:
            item = items.get((mid, D.ROUTE))
            if item is None:
                counts["MISSING"] += 1
                continue
            if item.get("lifecycle") == "PURGED" or not isinstance(item.get("route_spec"), dict):
                counts["REFUSED_PURGED"] += 1
                continue
            before = copy.deepcopy(self.m.dp.get(mid))
            res = self.m.push(now, mid, item, cause=f"agent:{caller.get('call_no')}", by="rmctl")
            if res == "ACCEPTED":
                counts["ACCEPTED"] += 1
                after = self.m.dp.get(mid) or {}
                if not before or before.get("cells") != after.get("cells") or before.get("pool_id") != after.get(
                        "pool_id") or before.get("fallback_cell") != after.get("fallback_cell"):
                    changed += 1
            else:
                counts["STALE"] += 1
                if len(stale) < 10:
                    stale.append(f"  STALE {mid}: version {item.get('route_version')} not above loaded "
                                 f"{(before or {}).get('version')}")
            if item.get("publish_mode") != "AUTO":
                set_auto.append(dict(copy.deepcopy(item), publish_mode="AUTO"))
        if set_auto:
            if self.table() == D.STATE:
                self.m.write(now, set_auto, f"rmctl:{caller.get('role') or 'sre-primary'}",
                             cause=f"agent:{caller.get('call_no')}", acting=caller)
            else:
                for item in set_auto:
                    self.m.call("dynamodb", "PutItem", {"TableName": self.table(), "Item": D.item_ddb(item)},
                                acting=caller)
        caller["retry_s"] = max(0.0, 30 + 0.1 * len(mids) - 45)
        out = (f"publish {len(mids)} keys from {self.table()}: ACCEPTED {counts['ACCEPTED']} (config changed {changed}, "
               f"unchanged {counts['ACCEPTED'] - changed}), STALE {counts['STALE']}, REFUSED_PURGED "
               f"{counts['REFUSED_PURGED']}" + (f", MISSING {counts['MISSING']}" if counts["MISSING"] else "") + "\n")
        return out + "".join(f"{s}\n" for s in stale), 0

    # keys
    def keys(self, rest, caller, now):
        if not rest:
            raise Usage("keys count|head <file> ; keys union|minus|intersect <a> <b> --out <file>")
        sub = rest[0]
        if sub in ("count", "head"):
            pos, _ = _opts(rest[1:])
            if not pos:
                raise Usage(f"keys {sub} <file>")
            ks = self.keyset(pos[0])
            if sub == "count":
                return f"{len(ks)}\n", 0
            n = int(pos[1]) if len(pos) > 1 and pos[1].isdigit() else 10
            return "".join(f"{m}\n" if r == D.ROUTE else f"{m},{r}\n" for m, r in ks[:n]), 0
        if sub in ("union", "minus", "intersect"):
            pos, o = _opts(rest[1:], valued=("--out",))
            if len(pos) != 2 or not o.get("--out"):
                raise Usage(f"keys {sub} <a> <b> --out <file>")
            a, b = self.keyset(pos[0]), self.keyset(pos[1])
            if sub == "union":
                res = sorted(set(a) | set(b))
            elif sub == "minus":
                res = sorted(set(a) - set(b))
            else:
                res = sorted(set(a) & set(b))
            p = self.write_file(o["--out"], [m if r == D.ROUTE else f"{m},{r}" for m, r in res])
            return f"{len(res)} keys written to {p}\n", 0
        raise Usage(f"unknown subcommand {sub!r}")

    # api
    def api(self, rest, caller, now):
        if rest[:1] == ["status"]:
            self.m.refresh_ssm()
            rev = self.m.api["rev"]
            ver = "v2.31.0" if rev.endswith(":58") else "v2.30.4" if rev.endswith(":57") else "?"
            pending = [a for t, _, k, a in self.m.sched if k == "api_switch"]
            dep = f"\ndeploying {pending[0][0]} (started {_z(self.m.api['rollover'])})" if pending else ""
            on = lambda k: "on" if self.m.flag(f"/routemap/prod/api/{k}_enabled") else "off"
            return (f"routemap-api: task definition {rev} ({ver}), 6/6 running{dep}\nupdate_route={on('update_route')} "
                    f"create_route={on('create_route')}\n"), 0
        if rest[:1] == ["set"] and len(rest) > 1:
            names = []
            for part in rest[1:]:
                k, eq, v = part.partition("=")
                if k not in ("update_route", "create_route") or v not in ("on", "off"):
                    raise Usage("api set update_route=on|off create_route=on|off")
                self.m.call("ssm", "PutParameter", {"Name": f"/routemap/prod/api/{k}_enabled", "Value":
                                                    "true" if v == "on" else "false", "Type": "String",
                                                    "Overwrite": True}, acting=caller)
                names.append(f"{k}={v}")
            self.m.refresh_ssm()
            self.m.api_timeline.append({"t": self.m.iso(now), "revision": self.m.api["rev"],
                                        "update_route": self.m.ssm.get("/routemap/prod/api/update_route_enabled"),
                                        "create_route": self.m.ssm.get("/routemap/prod/api/create_route_enabled"),
                                        "by_call": caller.get("call_no")})
            return "routemap-api flags: " + " ".join(names) + "\n", 0
        raise Usage("api status ; api set update_route=on|off create_route=on|off")
