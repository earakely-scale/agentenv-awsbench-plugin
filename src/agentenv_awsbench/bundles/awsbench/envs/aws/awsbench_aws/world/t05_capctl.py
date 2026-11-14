"""T05 host tools (docs/T05-build.md §1.3): capctl on vl-ops-bastion-01, the allowlisted commands on vaultstore hosts
(smartctl, systemctl, journalctl, ...), and `curl -s https://status.vantleaf.com` on the bastion, rendered from live
mechanics. capctl's EC2 writes (tags, stop, start) are made with the caller's credentials and logged under its call."""

import re
import shlex
from datetime import datetime

from .t05_vaultstore import BASTION, COLDSTART, CRASHLOOP, FULL, HALT, POOLS, READ, SERVING, parse_hosts
from .util import parse_time

NOT_AVAILABLE = "sh: {cmd}: not available in this environment"
NOT_FOUND = "bash: {cmd}: command not found"
SHELL_OPS = re.compile(r"[;|<>`]|&&|\$\(")
SUDO = re.compile(r"^\s*sudo\s+")
ONE_COMMAND = "one command per call: shell operators are not supported"
UNKNOWN = "capctl: unknown command (see capctl --help)"
HELP = """capctl 3.4.1 - vaultstore capacity control

Usage: capctl <command> [options]

Commands:
  status [--subsystem index|placement|billing-aux]   subsystem state, live/floor (from the guard), progress
  pools                                              pools with size and live members
  hosts --pool GLOB [--sort p99|name]                hosts with Role, SMART, p99 (trailing 24 h)
  ops [--since T]                                    recent capacity operations
  show-op OPID                                       matched pools, hosts, undo hint
  remove --pool GLOB (--count N --select slowest|random | --hosts H1,H2,...) --reason R [--dry-run]
  add    --pool GLOB (--count N | --hosts H1,H2,...) --reason R [--dry-run]
  restore --op OPID [--pool GLOB] [--dry-run]        re-admit hosts removed by OPID
  coldstart --subsystem index|placement|all [--abort]
  schedule list | schedule add --at T -- <remove|add ...> | schedule cancel SCHEDID [--reason R]

Notes:
  --pool GLOB is matched against pool names; --count and --select apply to EACH matched pool.
  --select slowest: highest p99 over the trailing 24 h among live members.
  --hosts selects by exact host name within the matched pool(s).
  --dry-run prints matched pools, the hosts with Role/SMART, and live before -> after per pool.
  coldstart --subsystem all starts index and placement together (if index is SERVING_FULL, only placement).
  capctl applies EC2 stop/start and capctl:* tags with your credentials.
"""
CAPCTL_CONF = ("endpoint: https://vs-control.internal.vantleaf.io\naudit_log_group: /vl/platform/vaultstore/capctl-audit\n"
               "credentials: caller\n")
OS_RELEASE = 'NAME="Ubuntu"\nVERSION="20.04.6 LTS (Focal Fossa)"\nID=ubuntu\nPRETTY_NAME="Ubuntu 20.04.6 LTS"\n'
WRITE_SUBS = ("remove", "add", "restore", "coldstart")


class Usage(Exception):
    pass


def opts(args: list[str], flags: tuple = ("dry-run", "abort")) -> tuple[dict, list[str]]:
    """--key value / --key=value options and bare flags; the rest positional."""
    out, rest, i = {}, [], 0
    while i < len(args):
        a = args[i]
        if a == "--":
            rest += args[i:]
            break
        if a.startswith("--"):
            key, eq, val = a[2:].partition("=")
            if key in flags:
                out[key] = True
            elif eq:
                out[key] = val
            elif i + 1 < len(args):
                out[key] = args[i + 1]
                i += 1
            else:
                raise Usage(f"capctl: option --{key} needs a value")
        else:
            rest.append(a)
        i += 1
    return out, rest


class Commands:
    def __init__(self, mech):
        self.m = mech
        self.vs = mech.vs

    # ---- fleet tools ----
    def list_hosts(self, fleet: str | None, az: str | None) -> list[dict]:
        fleets = {"vaultstore", "ops-bastion"}
        if fleet and fleet not in fleets:
            raise ValueError(f"unknown fleet {fleet!r}; fleets: {', '.join(sorted(fleets))}")
        out = []
        for n, h in self.vs.hosts.items():
            f = "vaultstore" if h.pool else "ops-bastion"
            if (fleet and f != fleet) or (az and h.az != az):
                continue
            out.append({"name": n, "fleet": f, "pool": h.pool, "az": h.az, "instance_id": h.iid, "ip": h.ip,
                        "ec2_state": h.state})
        return out

    def inspect(self, name: str, now: float) -> dict:
        h = self.vs.hosts.get(name)
        if h is None:
            raise ValueError(f"unknown host {name!r}")
        if h.state != "running":
            return {"host": name, "instance_id": h.iid, "ec2_state": h.state, "reachable": False}
        if h.pool is None:
            return {"host": name, "instance_id": h.iid, "ec2_state": h.state, "reachable": True, "uptime":
                    self.uptime(h.launched, now), "cpu_pct": 3.1, "mem_used_pct": 21.4, "disk_root_used_pct": 22}
        return {"host": name, "instance_id": h.iid, "ec2_state": h.state, "reachable": True, "pool": h.pool,
                "subsystem": h.sub, "role": h.role, "uptime": self.uptime(h.launched, now),
                "service": f"vaultstore-{h.sub}: {'active (running)' if h.proc else 'inactive (dead)'}",
                "cpu_pct": 11.2 if h.member else 1.4, "mem_used_pct": 61.0 if h.member else 8.2,
                "disk_data_used_pct": 64, "nvme_smart": self.smart_word(h)}

    @staticmethod
    def smart_word(h) -> str:
        return {"FAILED": "FAILED", "PRE-FAIL": "PASSED (warnings: media errors rising)"}.get(h.smart, "PASSED")

    def uptime(self, since: float, now: float) -> str:
        d = int(now - since)
        return f"{d // 86400} days, {d % 86400 // 3600}:{d % 3600 // 60:02d}"

    def exec(self, selector: str, command: str, caller: dict, now: float) -> dict:
        if SHELL_OPS.search(command or ""):
            return {"hosts": [], "exit_code": 2, "error": ONE_COMMAND}
        names = self.vs.select(selector)
        if not names:
            return {"hosts": [], "error": f"no hosts match {selector!r}"}
        out = []
        for n in names:
            stdout, code = self.run(n, SUDO.sub("", command or ""), caller, now)
            out.append({"host": n, "stdout": stdout, "exit_code": code})
        return {"hosts": out}

    def run(self, name: str, command: str, caller: dict, now: float) -> tuple[str, int]:
        h = self.vs.hosts[name]
        if h.state != "running":
            return f"ssh: connect to host {name} port 22: Connection timed out (instance {h.iid} {h.state})\n", 255
        try:
            argv = shlex.split(command)
        except ValueError as e:
            return f"sh: {e}\n", 2
        if not argv:
            return "", 0
        cmd, args = argv[0], argv[1:]
        if cmd in ("hostname", "date", "uptime", "whoami", "id"):
            return self.basic(name, cmd, now), 0
        if name == BASTION:
            if cmd == "capctl":
                return self.capctl(args, caller, now)
            if cmd == "curl":
                return self.curl_status(args, now)
            if cmd == "cat" and args == ["/etc/capctl/config.yaml"]:
                return CAPCTL_CONF, 0
            if cmd == "cat" and args == ["/etc/os-release"]:
                return OS_RELEASE, 0
            if cmd in ("smartctl", "systemctl", "journalctl"):
                return self.node(h, cmd, args, caller, now)
            return NOT_AVAILABLE.format(cmd=cmd) + "\n", 127
        if cmd == "capctl":
            return NOT_FOUND.format(cmd=cmd) + "\n", 127
        if cmd in ("smartctl", "systemctl", "journalctl", "cat", "curl", "df", "free"):
            return self.node(h, cmd, args, caller, now)
        return NOT_AVAILABLE.format(cmd=cmd) + "\n", 127

    def basic(self, name: str, cmd: str, now: float) -> str:
        t = datetime.fromtimestamp(now, self.m.tz)
        if cmd == "hostname":
            return name + "\n"
        if cmd == "whoami":
            return "root\n"
        if cmd == "id":
            return "uid=0(root) gid=0(root) groups=0(root)\n"
        if cmd == "date":
            return t.strftime("%a %b %d %H:%M:%S EST %Y") + "\n"
        h = self.vs.hosts[name]
        return f" {t.strftime('%H:%M:%S')} up {self.uptime(h.launched, now)},  0 users,  load average: 0.42, 0.40, 0.38\n"

    # ---- vaultstore node commands ----
    def node(self, h, cmd: str, args: list[str], caller: dict, now: float) -> tuple[str, int]:
        unit = f"vaultstore-{h.sub}" if h.pool else "capctl"
        if cmd == "smartctl":
            if args[-1:] != ["/dev/nvme0n1"] or not ({"-H", "-a", "-A", "--health"} & set(args)):
                return "smartctl: only `smartctl -H /dev/nvme0n1` is available here\n", 1
            if h.pool is None:
                return "smartctl: /dev/nvme0n1: No such device\n", 2
            head = "smartctl 7.1 2019-12-30 r5022 [x86_64-linux-5.15.0-1066-aws] (local build)\n\n=== START OF SMART DATA SECTION ===\n"
            if h.smart == "FAILED":
                return head + ("SMART overall-health self-assessment test result: FAILED!\nMedia and Data Integrity "
                               f"Errors: {self.media(h, now)}\n"), 1
            if h.smart == "PRE-FAIL":
                return head + ("SMART overall-health self-assessment test result: PASSED\nWarning: Media and Data "
                               f"Integrity Errors: {self.media(h, now)} (rising)\n"), 0
            return head + "SMART overall-health self-assessment test result: PASSED\n", 0
        if cmd == "systemctl":
            sub = args[0] if args else "status"
            target = (args[1] if len(args) > 1 else unit).removesuffix(".service")
            if h.pool is None or target not in (unit, "vaultstore-agent"):
                return f"Unit {target}.service could not be found.\n", 4
            if sub in ("status", "is-active", "show"):
                up = h.proc or target == "vaultstore-agent"
                if sub == "is-active":
                    return ("active\n" if up else "inactive\n"), 0 if up else 3
                return (f"● {target}.service - vaultstore {h.sub} node\n     Loaded: loaded (/lib/systemd/system/"
                        f"{target}.service; enabled)\n     Active: " + ("active (running)" if up else "inactive (dead)")
                        + "\n"), 0 if up else 3
            if sub in ("restart", "stop", "start") and target == unit:
                return self.m.service_action(h, sub, caller, now)
            return f"systemctl: {sub} is not available here\n", 1
        if cmd == "journalctl":
            o, _ = opts([a for a in args if a not in ("--no-pager", "-q")], flags=())
            n = int(o.get("n") or o.get("lines") or 30) if str(o.get("n") or o.get("lines") or "30").isdigit() else 30
            since = parse_time(o["since"], self.m.w.start_t).timestamp() if o.get("since") and re.fullmatch(
                r"\d{1,2}:\d{2}(:\d{2})?", str(o["since"])) else 0
            rows = [(t, line) for t, host, line in self.vs.journal if host == h.name and since <= t <= now]
            if h.member and h.sub in ("index", "placement"):
                rows += self.m.tel.node_lines(h, max(since, now - 3600), now)
            rows.sort()
            fmt = [f"{datetime.fromtimestamp(t, self.m.tz):%b %d %H:%M:%S} {h.name} {line}" for t, line in rows[-n:]]
            return ("\n".join(fmt) + "\n") if fmt else "-- No entries --\n", 0
        if cmd == "cat":
            path = args[0] if args else ""
            if path == "/etc/vaultstore/node.yaml":
                return f"pool: {h.pool}\nsubsystem: {h.sub}\nrole: {h.role}\n", 0
            if path == "/etc/os-release":
                return OS_RELEASE, 0
            return f"cat: {path}: No such file or directory\n", 1
        if cmd == "curl":
            url = next((a for a in args if not a.startswith("-")), "")
            if re.fullmatch(r"(?:http://)?localhost:7070/healthz", url):
                return ('{"status":"ok","member":true}\n' if h.member else
                        '{"status":"unavailable","member":false}\n'), 0
            return f"curl: (7) Failed to connect to {url or 'host'}\n", 7
        if cmd == "df":
            return ("Filesystem      Size  Used Avail Use% Mounted on\n/dev/nvme1n1p1   50G   16G   34G  31% /\n"
                    "/dev/nvme0n1    3.8T  2.4T  1.4T  64% /var/lib/vaultstore\n"), 0
        if cmd == "free":
            return ("              total        used        free      shared  buff/cache   available\nMem:         "
                    "126976       77420        4120          12       45436       48210\nSwap:             0"
                    "           0           0\n"), 0
        return NOT_AVAILABLE.format(cmd=cmd) + "\n", 127

    def media(self, h, now: float) -> int:
        from .t05_vaultstore import MEDIA_ERRORS
        base = MEDIA_ERRORS.get(h.name, 0)
        return base + (int((now - self.m.at("10:02:41")) / 120) if h.smart == "PRE-FAIL" else 0)

    # ---- the public status page ----
    def curl_status(self, args: list[str], now: float) -> tuple[str, int]:
        url = next((a for a in args if not a.startswith("-")), "")
        if not re.fullmatch(r"(?:https?://)?status\.vantleaf\.com/?(?:index\.html)?", url):
            return f"curl: (6) Could not resolve host: {re.sub(r'^https?://', '', url).split('/')[0] or 'host'}\n", 6
        return self.m.impact.render_public(now), 0

    # ---- capctl ----
    def capctl(self, args: list[str], caller: dict, now: float) -> tuple[str, int]:
        if not args or args[0] in ("--help", "-h", "help"):
            return HELP, 0
        sub, rest = args[0], args[1:]
        try:
            if sub == "status":
                o, _ = opts(rest)
                return self.status(o.get("subsystem"), now), 0
            if sub == "pools":
                return self.pools(), 0
            if sub == "hosts":
                o, _ = opts(rest)
                return self.hosts(o.get("pool") or "*", o.get("sort") or "p99")
            if sub == "ops":
                return self.ops_list(now), 0
            if sub == "show-op":
                return self.show_op(rest[0] if rest else "")
            if sub == "schedule":
                return self.schedule(rest, caller, now)
            if sub in WRITE_SUBS:
                return self.write(sub, rest, caller, now)
        except Usage as e:
            return f"{e}\n", 2
        return UNKNOWN + "\n", 2

    def status(self, sub: str | None, now: float) -> str:
        vs, rows = self.vs, []
        if sub not in (None, "index", "placement", "billing-aux"):
            raise Usage("capctl: --subsystem is one of index, placement, billing-aux")
        hdr = f"{'subsystem':<12} {'state':<16} {'live/floor':<11} {'since':<9} detail"
        idx, pl, b = vs.idx, vs.pl, vs.batch
        if sub in (None, "index"):
            detail = ""
            if idx["state"] == HALT:
                detail = (f"live {vs.live('vs-blobindex')} below serving floor 16; cold start required"
                          if vs.live("vs-blobindex") < 16 else "cold start required")
                if idx.get("last") == "FAILED":
                    detail = ("last cold start FAILED at replica check: 41/512 partitions under-replicated; " + detail)
            elif idx["state"] == COLDSTART:
                done, cur, keys = vs.partitions()
                ph = vs.phase()
                if ph == "verify-manifests":
                    detail = f"phase verify-manifests (check started {vs.hms(idx['started'])})"
                else:
                    rate = self.m.tel.partition_rate(now)
                    detail = (f"phase verify-partitions {done}/512 ({rate:.1f}/min last 10 min) partition={cur}"
                              + (f" keys_verified {keys / 1e6:.1f}M/139.7M" if cur == vs.p["slow_partition"] else ""))
            elif idx["state"] == READ:
                detail = "reads serving; writes waiting; phase rebuild-routing"
            rows.append(f"{'index':<12} {idx['state']:<16} {str(vs.live('vs-blobindex')) + '/16':<11} "
                        f"{vs.hms(idx['since']):<9} {detail}".rstrip())
        if sub in (None, "placement"):
            detail = {HALT: (f"live {vs.live('vs-blobplace')} below floor 10; " if vs.live("vs-blobplace") < 10 else "")
                      + ("index unavailable" if idx["state"] != FULL else "cold start required"),
                      COLDSTART: "booting (allocation-map validation)" if pl.get("fail_at") else "booting",
                      CRASHLOOP: f"restarting every 90s: allocation-map validation failed (index {idx['state']}); "
                                 f"restarts {self.m.tel.placement_restarts(now, 600)} in the last 10 min",
                      SERVING: ""}.get(pl["state"], "")
            rows.append(f"{'placement':<12} {pl['state']:<16} {str(vs.live('vs-blobplace')) + '/10':<11} "
                        f"{vs.hms(pl['since']):<9} {detail}".rstrip())
        if sub in (None, "billing-aux"):
            state = "DEGRADED" if b["state"] == "PAUSED" or b["state"] == "ABORTED" else "SERVING"
            lock = "worker set locked" if b["locked"] else "worker-set lock 2026-11-20 14:00"
            detail = (f"billing-usage {b['state']}; work left {b['work_left_h']:.1f} h at full pool health; "
                      f"throughput {vs.throughput():.2f}; {lock}; invoice run 18:00")
            if b["state"] == "ABORTED":
                detail = f"billing-usage ABORTED at {vs.hms(b['aborted_at'])} ({b['abort_cause']}); invoice run 18:00"
            rows.append(f"{'billing-aux':<12} {state:<16} {str(vs.live('vs-billing-aux')) + '/4':<11} "
                        f"{'10:25:58':<9} {detail}")
        if sub is None:
            rows.append(f"{'frontend':<12} {'SERVING':<16} {str(vs.live('vs-front')) + '/6':<11}")
            rows.append(f"{'storage':<12} " + ("idle (waiting on index)" if idx["state"] not in (READ, FULL) else
                                               "SERVING"))
        return hdr + "\n" + "\n".join(rows) + "\n"

    def pool_state(self, pool: str) -> str:
        vs = self.vs
        sub = POOLS[pool][0]
        if sub == "index":
            return vs.idx["state"]
        if sub == "placement":
            return vs.pl["state"]
        if sub == "billing-aux":
            return "DEGRADED (index unavailable)" if vs.batch["state"] == "PAUSED" else "SERVING"
        if sub == "frontend":
            return "SERVING"
        return "idle" if vs.idx["state"] not in (READ, FULL) else "SERVING"

    def pools(self) -> str:
        vs = self.vs
        rows = [f"{'pool':<16} {'size':<5} {'live':<5} state"]
        for pool in POOLS:
            rows.append(f"{pool:<16} {len(vs.pool_hosts[pool]):<5} {vs.live(pool):<5} {self.pool_state(pool)}")
        return "\n".join(rows) + "\n"

    def hosts(self, glob: str, sort: str) -> tuple[str, int]:
        vs = self.vs
        pools = vs.pool_names(glob)
        if not pools:
            return f"ERROR no pool matches {glob!r}\n", 1
        out = []
        for pool in pools:
            out.append(f"pool {pool}  (p99 = trailing 24 h; static during the episode)")
            out.append(f"{'host':<11} {'state':<8} {'role':<12} {'p99_ms':>6}  smart")
            names = list(vs.pool_hosts[pool])
            names.sort(key=(lambda n: (-vs.hosts[n].p99, n)) if sort == "p99" else (lambda n: n))
            for n in names:
                h = vs.hosts[n]
                state = "live" if h.member else "joining" if h.join_at else ("removed" if h.tags.get("capctl:state") ==
                                                                            "removed" else h.state)
                smart = {"FAILED": "FAILED", "PRE-FAIL": f"PRE-FAIL (media errors {self.media(h, self.m.now)}, "
                                                         "+30/h)"}.get(h.smart, "ok")
                out.append(f"{n:<11} {state:<8} {h.role:<12} {h.p99:>6}  {smart}")
            out.append("")
        return "\n".join(out), 0

    def ops_list(self, now: float) -> str:
        rows = [f"{'op':<18} {'time (ET)':<9} {'user':<14} {'reason':<16} command"]
        for op in self.vs.ops[-20:]:
            rows.append(f"{op['op']:<18} {self.vs.hms(op['ts']):<9} {op['user']:<14} {op.get('reason') or '-':<16} "
                        f"capctl {op['command']}")
        return "\n".join(rows) + "\n"

    def show_op(self, op_id: str) -> tuple[str, int]:
        vs = self.vs
        op = vs.find_op(op_id)
        if op is None:
            return f"ERROR no such op {op_id!r}\n", 1
        stamp = datetime.fromtimestamp(op["ts"], self.m.tz).isoformat()
        out = [f"op {op['op']}  {stamp}  user {op['user']}  reason {op.get('reason') or '-'}",
               f"command: capctl {op['command']}"]
        if op["kind"] in ("remove", "add", "restore"):
            out.append(f"matched pools (glob {op['glob']}): {', '.join(op['matched'])}"
                       + ("   (count applies per pool)" if "--count" in op["command"] else ""))
            verb = {"remove": "removed", "add": "added", "restore": "restored"}[op["kind"]]
            for pool, names in op["hosts"].items():
                before, after = op["live_before"].get(pool), op["live_after"].get(pool)
                out.append(f"  {pool:<15} {verb} {' '.join(names) or '-':<40} live {before} -> {after}")
            total = sum(len(v) for v in op["hosts"].values())
            if op["kind"] == "remove":
                out.append(f"{verb}: {total} hosts (EC2 stopped, tag capctl:state=removed)")
                out.append(f"Undo with: capctl restore --op {op['op']} (within 24 h)")
            else:
                out.append(f"{verb}: {total} hosts (EC2 started, tag capctl:state=active)")
        else:
            out.append(f"subsystem {op.get('subsystem')}  result {op.get('result')}")
        return "\n".join(out) + "\n", 0

    def schedule(self, rest: list[str], caller: dict, now: float) -> tuple[str, int]:
        vs = self.vs
        if not rest or rest[0] == "list":
            rows = [f"{'id':<11} {'at (ET)':<17} {'owner':<13} {'state':<9} command"]
            for s in sorted(vs.scheds.values(), key=lambda s: s["id"]):
                at = datetime.fromtimestamp(s["at"], self.m.tz).strftime("%Y-%m-%d %H:%M")
                rows.append(f"{s['id']:<11} {at:<17} {s['owner']:<13} {s['state']:<9} {s['command']}")
            return "\n".join(rows) + "\n", 0
        if rest[0] == "cancel":
            o, pos = opts(rest[1:])
            sid = pos[0] if pos else ""
            if sid not in vs.scheds:
                return f"ERROR no such schedule {sid!r}\n", 1
            s = vs.scheds[sid]
            if s["state"] == "FIRED":
                return f"ERROR {sid} already executed at {vs.hms(s['fired_at'])[:5]} (op {s['op']})\n", 1
            if s["state"] == "CANCELLED":
                return f"ERROR {sid} already cancelled at {vs.hms(s['cancelled_at'])} by {s['cancelled_by']}\n", 1
            vs.schedule_cancel(sid, now, caller.get("role") or "sre-primary", o.get("reason") or "", self.cause(caller))
            return f"{sid} CANCELLED (owner {s['owner']} notified by capctl-bot in #deploys)\n", 0
        if rest[0] == "add":
            o, pos = opts(rest[1:])
            if "--" not in pos or not o.get("at"):
                raise Usage("capctl: schedule add --at T -- <remove|add ...>")
            cmd = pos[pos.index("--") + 1:]
            if not cmd or cmd[0] not in ("remove", "add"):
                raise Usage("capctl: schedule add runs remove or add")
            try:
                at = parse_time(o["at"].replace("T", " ").split(" ")[-1] if re.fullmatch(
                    r"(?:2026-11-20[T ])?\d{1,2}:\d{2}(:\d{2})?", o["at"]) else o["at"], self.m.w.start_t).timestamp()
            except ValueError:
                raise Usage(f"capctl: --at {o['at']!r} is not a time") from None
            if at <= now:
                return "ERROR --at is in the past\n", 1
            self.parse_change(cmd[0], cmd[1:])
            s = vs.schedule_add(at, shlex.join(cmd), now, caller.get("role") or "sre-primary", self.cause(caller))
            return f"{s['id']} created for {datetime.fromtimestamp(at, self.m.tz):%Y-%m-%d %H:%M} ET: {s['command']}\n", 0
        raise Usage("capctl: schedule list | schedule add --at T -- <...> | schedule cancel SCHEDID [--reason R]")

    @staticmethod
    def cause(caller: dict) -> str:
        return f"agent:{caller.get('call_no')}" if caller.get("role") else str(caller.get("cause") or "world")

    def parse_change(self, sub: str, rest: list[str]) -> dict:
        o, pos = opts(rest)
        if pos:
            raise Usage(f"capctl: unexpected argument {pos[0]!r} (see capctl --help)")
        if sub in ("remove", "add"):
            if not o.get("pool"):
                raise Usage(f"capctl: {sub} needs --pool GLOB")
            if bool(o.get("hosts")) == bool(o.get("count")):
                raise Usage(f"capctl: {sub} needs exactly one of --count N or --hosts H1,H2,...")
            if o.get("count") and not str(o["count"]).isdigit():
                raise Usage("capctl: --count must be a number")
            if sub == "remove" and o.get("count") and o.get("select", "slowest") not in ("slowest", "random"):
                raise Usage("capctl: --select is slowest or random")
            if sub == "remove" and o.get("count") and not o.get("select"):
                raise Usage("capctl: remove --count needs --select slowest|random")
            if not o.get("reason") and not o.get("dry-run"):
                raise Usage(f"capctl: {sub} needs --reason R")
        elif sub == "restore":
            if not o.get("op"):
                raise Usage("capctl: restore needs --op OPID")
        elif sub == "coldstart":
            if o.get("subsystem") not in ("index", "placement", "all"):
                raise Usage("capctl: coldstart --subsystem index|placement|all [--abort]")
        return o

    def write(self, sub: str, rest: list[str], caller: dict, now: float) -> tuple[str, int]:
        vs = self.vs
        o = self.parse_change(sub, rest)
        user, cause = caller.get("role") or "sre-primary", self.cause(caller)
        command = shlex.join([sub, *rest])
        if sub == "coldstart":
            res, op = vs.coldstart(o["subsystem"], bool(o.get("abort")), now, user, command, cause)
            return self.coldstart_text(o["subsystem"], bool(o.get("abort")), res, op), (
                1 if "below_floor" in res else 0)
        if sub == "restore":
            src = vs.find_op(o["op"])
            if src is None or src["kind"] != "remove":
                return f"ERROR no remove op {o['op']!r}\n", 1
            pools = vs.pool_names(o.get("pool") or "*")
            names = [n for p in src["matched"] if p in pools for n in src["hosts"].get(p, [])]
            plan = {"glob": o.get("pool") or src["glob"], "pools": []}
            for p in src["matched"]:
                if p in pools:
                    live = vs.live(p)
                    pick = [n for n in src["hosts"].get(p, []) if not vs.hosts[n].member]
                    plan["pools"].append({"name": p, "hosts": pick, "live_before": live, "live_after": live + len(pick)})
            if not names:
                return f"ERROR op {o['op']} removed no hosts from pools matching {o.get('pool')!r}\n", 1
            locked = self.locked(plan)
            if locked:
                return locked, 1
            if o.get("dry-run"):
                return self.dry_run(f"capctl {command}", plan), 0
            op = vs.add(plan, now, user, src["op"], command, cause, kind="rs")
            self.m.ec2_on_behalf(caller, now, op, start=True)
            return self.applied_text(op, "restore"), 0
        try:
            plan = vs.plan(sub, o["pool"], int(o["count"]) if o.get("count") else None, o.get("select"),
                           parse_hosts(o.get("hosts")), now)
        except ValueError as e:
            return f"ERROR {e}\n", 1
        locked = self.locked(plan)
        if locked:
            return locked, 1
        if o.get("dry-run"):
            return self.dry_run(f"capctl {command}", plan), 0
        if sub == "remove":
            op = vs.remove(plan, now, user, o.get("reason") or "", command, cause)
            self.m.ec2_on_behalf(caller, now, op, start=False)
            return self.applied_text(op, "remove"), 0
        op = vs.add(plan, now, user, o.get("reason") or "", command, cause)
        self.m.ec2_on_behalf(caller, now, op, start=True)
        return self.applied_text(op, "add"), 0

    def locked(self, plan: dict) -> str | None:
        b = self.vs.batch
        if b["locked"] and any(p["name"] == "vs-billing-aux" for p in plan["pools"]):
            return "ERROR pool vs-billing-aux locked by billing-usage (worker set pinned at 14:00)\n"
        return None

    def dry_run(self, argv: str, plan: dict) -> str:
        out = [f"DRY RUN: {argv}", f"matched pools: {', '.join(p['name'] for p in plan['pools'])}"]
        verb = "remove" if argv.split()[1] == "remove" else "add"
        for p in plan["pools"]:
            hosts = " ".join(self.vs.describe(n) for n in p["hosts"]) or "(none)"
            out.append(f"  {p['name']}: would {verb} {hosts}  live {p['live_before']} -> {p['live_after']}")
        out.append("(no changes made)")
        return "\n".join(out) + "\n"

    def applied_text(self, op: dict, kind: str) -> str:
        out = [f"op {op['op']} ({op['user']}, reason {op.get('reason') or '-'})",
               f"matched pools (glob {op['glob']}): {', '.join(op['matched'])}"]
        for pool, names in op["hosts"].items():
            out.append(f"  {pool:<15} {'removed' if kind == 'remove' else 'added'} {' '.join(names) or '-'}   live "
                       f"{op['live_before'].get(pool)} -> {op['live_after'].get(pool)}")
        if kind == "remove":
            out.append("EC2: tagged capctl:state=removed and stopped the instances")
            out.append(f"op {op['op']} completed. Undo with: capctl restore --op {op['op']} (within 24 h)")
        else:
            out.append("EC2: tagged capctl:state=active and started the stopped instances; hosts join membership in "
                       "about 45 s")
            out.append(f"op {op['op']} completed.")
        return "\n".join(out) + "\n"

    def coldstart_text(self, sub: str, abort: bool, res: str, op: dict) -> str:
        vs = self.vs
        if abort:
            return f"coldstart {sub} --abort: {res} (op {op['op']})\n"
        texts = {"started": "check started; phase verify-manifests",
                 "restarted": "check restarted from verify-manifests (the running check was discarded)",
                 "serving": "already serving; nothing to do", "below_floor": "ERROR refused: live "
                 f"{vs.live('vs-blobplace')} below floor 10"}
        parts = []
        for piece in res.split():
            name, _, r = piece.rpartition(":")
            parts.append(f"{name or sub}: {texts.get(r, r)}")
        return f"coldstart --subsystem {sub} (op {op['op']}): " + "; ".join(parts) + "\n"
