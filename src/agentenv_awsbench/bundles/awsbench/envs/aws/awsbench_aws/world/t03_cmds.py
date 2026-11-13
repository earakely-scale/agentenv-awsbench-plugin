"""T03 host tools: fleet_list_hosts, fleet_inspect_host and the allowlisted commands of fleet_exec on streamroute hosts
and cellmap-admin-01 (docs/T03-build.md §1.1, §1.10), rendered from live mechanics; no command output is invented."""

import re
import shlex
from datetime import datetime

from .t03_fleet import ADMIN, EJECTED, ROUTING, SERVING, STALE, STATES, STOPPED, WARMING, xmx_gib
from .util import unit

NOT_AVAILABLE = "sh: {cmd}: not available in this environment"
NOT_FOUND = "bash: {cmd}: command not found"
SR_ONLY, ADMIN_ONLY = {"srctl", "sr-apply-limits", "sr-metrics-agent"}, {"cmctl"}
SYSCTL = {"kernel.threads-max": 125520, "kernel.pid_max": 4194304}
NPROC = SYSCTL["kernel.threads-max"] // 2  # the kernel's default RLIMIT_NPROC, root's `ulimit -u`
TASKS_MAX = 18828
CGROUP = "/sys/fs/cgroup/system.slice/{unit}.service/pids.{what}"
DIRS = {"/etc/streamroute": "env  streamroute.conf", "/etc/systemd/system/streamroute.service.d": "10-limits.conf",
        "/opt/streamroute": "bin  streamroute-2.3.9.jar", "/opt/streamroute/bin": "streamroute-start",
        "/usr/local/bin": "sr-apply-limits  sr-metrics-agent  srctl",
        "/var/log": "amazon  apt  btmp  cloud-init-output.log  cloud-init.log  dist-upgrade  journal  lastlog  private  "
                    "unattended-upgrades  wtmp",
        "/sys/fs/cgroup/system.slice/streamroute.service": "cgroup.procs  cgroup.threads  memory.current  memory.max  "
                                                           "pids.current  pids.max"}
SHELL_OPS = re.compile(r"[;|<>`]|&&|\$\(")
SUDO = re.compile(r"^\s*sudo\s+")
ONE_COMMAND = "one command per call: shell operators are not supported"
FILES = {
    "/etc/streamroute/streamroute.conf": (
        "# /etc/streamroute/streamroute.conf (static; runtime values come from SSM /streamroute/prod/* at process "
        "start)\nstream = vl-payment-events\nlease_table = streamroute-leases\nconfig_table = streamroute-config\n"
        "cells = lc-01,lc-02,lc-03,lc-04,lc-05,lc-06,lc-07,lc-08\ncell_client_threads_per_cell = 24\n"
        "kinesis_consumer_threads = 64\nnetty_event_loop_threads = 32\nroute_worker_core = 256\n"
        "route_worker_max = 384\npeer_seed_list_size = 32\nadmin_port = 8081\n"),
    "/usr/local/bin/sr-apply-limits": (
        "#!/bin/bash\n# Renders the streamroute limits drop-in from SSM and reloads systemd. Runs at boot and inside "
        "`srctl restart`.\nset -euo pipefail\nNPROC=$(aws ssm get-parameter --name /streamroute/prod/os/limit_nproc "
        "--query Parameter.Value --output text || echo 1024)\nmkdir -p /etc/systemd/system/streamroute.service.d\n"
        "printf '[Service]\\nLimitNPROC=%s\\nLimitNOFILE=65536\\n' \"$NPROC\" > "
        "/etc/systemd/system/streamroute.service.d/10-limits.conf\nsystemctl daemon-reload\n"),
    "/etc/sr-metrics-agent.yaml": (
        "namespace: Vantleaf/Streamroute\ninterval: 60s\nmetrics: [RecordsRouted, RouteErrors, ShardMapValid, "
        "PeerCountKnown, CellLagSeconds, EjectedHosts]\n"),
}
LIMITS_CONF = "/etc/systemd/system/streamroute.service.d/10-limits.conf"
SRCTL_HELP = """srctl 2.3.9 - streamroute operations
  srctl fleet status          members and host states (from srcoord)
  srctl host status           this host: shard map state, epoch, last success, last error
  srctl lag                   per-cell backlog_records, lag_seconds, last_ack
  srctl leases --summary      lease owners by host state, checkpoint movement
  srctl membership            srcoord members (running processes), known peers on this host
  srctl config                effective config and the SSM values read at process start
  srctl restart               sr-apply-limits + daemon-reload + systemctl restart streamroute (use fleet_restart for waves)
"""
ULIMIT_A = """core file size          (blocks, -c) 0
data seg size           (kbytes, -d) unlimited
scheduling priority             (-e) 0
file size               (blocks, -f) unlimited
pending signals                 (-i) 62760
max locked memory       (kbytes, -l) 65536
max memory size         (kbytes, -m) unlimited
open files                      (-n) 1024
pipe size            (512 bytes, -p) 8
POSIX message queues     (bytes, -q) 819200
real-time priority              (-r) 0
stack size              (kbytes, -s) 8192
cpu time               (seconds, -t) unlimited
max user processes              (-u) 62760
virtual memory          (kbytes, -v) unlimited
file locks                      (-x) unlimited
"""
PROC_LIMITS = """Limit                     Soft Limit           Hard Limit           Units
Max cpu time              unlimited            unlimited            seconds
Max file size             unlimited            unlimited            bytes
Max data size             unlimited            unlimited            bytes
Max stack size            8388608              unlimited            bytes
Max core file size        0                    unlimited            bytes
Max resident set          unlimited            unlimited            bytes
Max processes             {n:<20} {n:<20} processes
Max open files            {files:<20} {files_max:<20} files
Max locked memory         {lock:<20} {lock:<20} bytes
Max address space         unlimited            unlimited            bytes
Max file locks            unlimited            unlimited            locks
Max pending signals       62760                62760                signals
Max msgqueue size         819200               819200               bytes
Max nice priority         0                    0
Max realtime priority     0                    0
Max realtime timeout      unlimited            unlimited            us
"""
DMESG = """[    2.114301] ena 0000:00:05.0: Elastic Network Adapter (ENA) v2.8.1g
[    2.120774] ena 0000:00:05.0 ens5: renamed from eth0
[    4.802217] EXT4-fs (nvme0n1p1): re-mounted. Opts: discard
[    6.330120] systemd[1]: Started streamroute (vl-payment-events -> ledger cells).
"""
UNIT = """[Unit]
Description=streamroute (vl-payment-events -> ledger cells)
After=network-online.target sr-apply-limits.service
Wants=network-online.target

[Service]
User=streamroute
Group=streamroute
EnvironmentFile=/etc/streamroute/env
ExecStart=/opt/streamroute/bin/streamroute-start
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
"""
MEM_TOTAL = 15690
PS_FIELDS = {"pid": "PID", "user": "USER", "nlwp": "NLWP", "rss": "RSS", "comm": "COMMAND", "cmd": "CMD",
             "args": "COMMAND", "lwp": "LWP", "tid": "TID", "spid": "SPID"}
UNIT_DESC = "streamroute.service - streamroute (vl-payment-events -> ledger cells)"
BOOTSTRAP = "INFO [bootstrap] bootstrap source={bootstrap_source} limit_nproc={limit_nproc} xmx={xmx}"


class Commands:
    def __init__(self, mech):
        self.m = mech

    # ---- values ----
    def rss_mib(self, h) -> int:
        return round(11469 + (xmx_gib(h.xmx) - 6) * 1024) if h.proc else 0

    def mem_pct(self, h) -> float:
        return round(84.1 + 0.27 * (h.links - 119) + (xmx_gib(h.xmx) - 6) * 6.5, 1) if h.proc else 9.6

    def pid(self, h, t: float | None = None) -> int:
        """The JVM's pid at `t`: each restart starts a new process."""
        stop = self.m.fleet.p["stop_s"]
        k = sum(1 for r in self.m.fleet.restarts if r["host"] == h.name and (t is None or r["t"] + stop <= t))
        return 2214 + 97 * k

    def procs(self, h) -> list[dict]:
        out = [{"pid": 1, "user": "root", "nlwp": 1, "rss": 11264, "cmd": "/sbin/init"},
               {"pid": 742, "user": "root", "nlwp": 4, "rss": 36864, "cmd": "/usr/sbin/unbound -d"},
               {"pid": 1187, "user": "streamroute", "nlwp": 22, "rss": 49152,
                "cmd": "/usr/local/bin/sr-metrics-agent --config /etc/sr-metrics-agent.yaml"}]
        if h.proc:
            out.append({"pid": self.pid(h), "user": "streamroute", "nlwp": h.jvm_threads(self.m.fleet.p),
                        "rss": self.rss_mib(h) * 1024, "cmd": f"/usr/lib/jvm/java-17-openjdk-amd64/bin/java "
                        f"-Xmx{h.xmx} -XX:+UseG1GC -Xss1m -jar /opt/streamroute/streamroute-2.3.9.jar"})
        for p in out:
            p["args"], p["comm"] = p["cmd"], p["cmd"].split()[0].rsplit("/", 1)[-1]
        return out

    def uptime(self, since: float, now: float) -> str:
        s = int(max(0, now - since))
        d, rem = divmod(s, 86400)
        return (f"{d}d " if d else "") + f"{rem // 3600}h{rem % 3600 // 60:02d}m"

    def shard_map(self, h, now: float) -> dict:
        st = "VALID" if h.state == ROUTING else "FAILED" if h.stuck else "STALE" if h.state == STALE else (
            "BUILDING" if h.state == WARMING else "NONE")
        return {"state": st, "epoch": h.epoch, "last_success": self.m.fleet.hms(h.last_success) if h.last_success
                else None, "last_error": h.last_error}

    def leases_of(self, name: str) -> list[int]:
        return [s for s, o in enumerate(self.m.flow.owner) if o == name]

    # ---- tools ----
    def list_hosts(self, fleet: str | None, az: str | None) -> list[dict]:
        rows = [{"name": h.name, "fleet": "streamroute", "az": h.az, "instance_id": h.iid, "ip": h.ip,
                 "state": h.state, "launched": self.m.iso(h.launched)}
                for h in sorted(self.m.fleet.hosts.values(), key=lambda h: h.name) if not h.terminated]
        rows += [{"name": n, "fleet": a["fleet"], "az": a["az"], "instance_id": "i-0c3a9e2b7d14f5068", "ip": a["ip"],
                  "state": "running", "launched": "2026-08-04T10:22:07-04:00"} for n, a in ADMIN.items()]
        fleets, zones = sorted({r["fleet"] for r in rows}), sorted({r["az"] for r in rows})
        if fleet and fleet not in fleets:
            raise ValueError(f"unknown fleet {fleet!r}; fleets: {', '.join(fleets)}")
        if az and az not in zones:
            raise ValueError(f"unknown availability zone {az!r}; zones: {', '.join(zones)}")
        return [r for r in rows if (not fleet or r["fleet"] == fleet) and (not az or r["az"] == az)]

    def inspect(self, name: str, now: float) -> dict:
        if name in ADMIN:
            return {"host": name, "fleet": "cellmap-admin", "uptime": "102d 1h", "cpu_pct": 3.1, "load1": 0.08}
        h = self.m.fleet.hosts.get(name)
        if h is None or h.terminated:
            return {"error": f"no host {name!r}" + (f" (terminated {self.m.fleet.hms(h.terminated_at)} ET)"
                                                     if h is not None and h.terminated_at else "")}
        heap = 0.0 if not h.proc else 2.2 + 0.4 * unit(self.m.w.seed, "heap", name)
        return {"host": name, "fleet": "streamroute", "az": h.az, "srcoord_state": h.state,
                "uptime": self.uptime(h.process_start, now) if h.proc else "process not running",
                "cpu_pct": 0.0 if not h.proc else (38.0 if h.state == ROUTING else 11.0),
                "load1": 0.0 if not h.proc else (1.9 if h.state == ROUTING else 0.6),
                "rss": f"{self.rss_mib(h)} MiB", "heap_used_max": f"{heap:.1f} / {xmx_gib(h.xmx):.1f} GiB",
                "mem_used_pct": self.mem_pct(h),
                "mem_available_mib": round(MEM_TOTAL * (1 - self.mem_pct(h) / 100)),
                "open_fds": 1400 + 4 * h.links if h.proc else 0,
                "peers_known_connected": f"{h.known} / {h.links}", "shard_map": self.shard_map(h, now),
                "leases_held": len(self.leases_of(name))}

    def exec(self, selector: str, command: str, caller: dict, now: float) -> dict:
        if SHELL_OPS.search(command or ""):
            return {"hosts": [], "exit_code": 2, "error": ONE_COMMAND}
        names, f = self.m.fleet.select(selector), self.m.fleet
        gone = {n: f"ssh: connect to host {n} port 22: No route to host (instance {f.hosts[n].iid} terminated "
                   f"{f.hms(t)} ET)" for n, t in f.named_terminated(selector).items()}
        extra = {"unreachable": gone} if gone else {}
        if not names:
            return {"hosts": [], "error": f"no hosts match {selector!r}", **extra}
        out = []
        for n in names:
            stdout, code = self.run(n, SUDO.sub("", command or ""), caller, now)
            out.append({"host": n, "stdout": stdout, "exit_code": code})
        return {"hosts": out, **extra}

    def run(self, name: str, command: str, caller: dict, now: float) -> tuple[str, int]:
        try:
            argv = shlex.split(command)
        except ValueError as e:
            return f"sh: {e}\n", 2
        if not argv:
            return "", 0
        cmd, args = argv[0], argv[1:]
        if name in ADMIN:
            if argv[:3] == ["cmctl", "bootstrap", "status"]:
                return self.cmctl(now), 0
            if cmd in ("hostname", "date", "uptime"):
                return self.basic(name, cmd, now), 0
            return (NOT_FOUND if cmd in SR_ONLY else NOT_AVAILABLE).format(cmd=cmd) + "\n", 127
        h = self.m.fleet.hosts[name]
        if cmd in ADMIN_ONLY:
            return NOT_FOUND.format(cmd=cmd) + "\n", 127
        if cmd == "srctl":
            return self.srctl(h, args, now)
        if cmd == "ulimit" and args in (["-a"], ["-u"], []):
            return (ULIMIT_A if args == ["-a"] else "62760\n" if args == ["-u"] else "unlimited\n"), 0
        if cmd == "cat" and (paths := [a for a in args if not a.startswith("-")]):
            outs = [self.cat(h, path, now) for path in paths]
            return "".join(o for o, _ in outs), max(c for _, c in outs)
        if cmd == "systemctl":
            return self.systemctl(h, args, caller, now)
        if cmd == "journalctl":
            return self.journal(h, args, now)
        if cmd == "dmesg" or (cmd == "journalctl" and "-k" in args):
            return self.dmesg(h), 0
        if cmd in ("pgrep", "pidof"):
            return self.pgrep(h, cmd, args)
        if cmd in ("head", "tail", "grep", "ls"):
            return getattr(self, cmd)(h, args, now)
        if cmd == "ps":
            return self.ps(h, args)
        if cmd == "free":
            used = round(MEM_TOTAL * self.mem_pct(h) / 100)
            return (f"              total        used        free      shared  buff/cache   available\n"
                    f"Mem:          {MEM_TOTAL} {used:>11}         214           2        1778 "
                    f"{MEM_TOTAL - used:>11}\nSwap:             0           0           0\n"), 0
        if cmd == "sysctl":
            vals = SYSCTL
            keys = [a for a in args if not a.startswith("-")] or list(vals)
            return "".join(f"{k} = {vals[k]}\n" if k in vals else f"sysctl: cannot stat /proc/sys/{k.replace('.', '/')}"
                           ": No such file or directory\n" for k in keys), 0 if all(k in vals for k in keys) else 255
        if cmd == "curl":
            return self.curl(h, args, now)
        if cmd in ("hostname", "date", "uptime"):
            return self.basic(name, cmd, now), 0
        return NOT_AVAILABLE.format(cmd=cmd) + "\n", 127

    # ---- commands ----
    def basic(self, name: str, cmd: str, now: float) -> str:
        t = datetime.fromtimestamp(now, self.m.tz)
        if cmd == "hostname":
            return name + "\n"
        if cmd == "date":
            return t.strftime("%a %b %d %H:%M:%S EST %Y") + "\n"
        return f" {t.strftime('%H:%M:%S')} up 21 days,  2:07,  0 users,  load average: 0.61, 0.58, 0.55\n"

    def cmctl(self, now: float) -> str:
        cm = self.m.fleet.cell
        shed = f"ACTIVE until {self.m.fleet.hms(cm.shed_until)}" if cm.shed(now) else "off"
        return (f"BootstrapSnapshot: in_flight={len(cm.in_flight)}/12 retrying={len(cm.retrying)} "
                f"throttled_429_total={cm.throttled}\nload_shed={shed} (engages when in_flight+retrying > 24 for "
                "120 s; 600 s)\n")

    def srctl(self, h, args: list[str], now: float) -> tuple[str, int]:
        f, m = self.m.fleet, self.m
        sub = " ".join(args)
        if sub in ("--help", "-h", "help", ""):
            return SRCTL_HELP, 0
        if sub == "restart":
            err = m.restart_host(h.name, now)
            if err:
                return err + "\n", 1
            return (f"sr-apply-limits: LimitNPROC={h.limit}\nstreamroute restarting on {h.name} "
                    f"(bootstrap_source={h.bootstrap_source})\n"), 0
        if not h.proc and sub == "host status":
            return f"srctl: streamroute is not running on {h.name} (state {h.state})\n", 1
        if sub == "fleet status":
            c = f.counts()
            lines = [f"members={len(f.members())}  " + "  ".join(f"{s} {c[s]}" for s in STATES if s != STOPPED),
                     (f"bootstrap_source={m.ssm['/streamroute/prod/shardmap/bootstrap_source']}; "
                      f"warmup_grace={m.ssm['/streamroute/prod/health/warmup_grace_seconds']}s")]
            for x in sorted(f.hosts.values(), key=lambda x: x.name):
                if x.terminated:
                    continue
                if x.state == STALE:
                    lines.append(f"  {x.name} STALE (map expires {f.hms(x.valid_until)})")
                elif x.state == WARMING:
                    lines.append(f"  {x.name} WARMING")
                elif x.state == EJECTED:
                    lines.append(f"  {x.name} EJECTED (quarantined until {f.hms(x.quarantined_until)})")
            return "\n".join(lines) + "\n", 0
        if sub == "host status":
            sm = self.shard_map(h, now)
            skipped = "; rebuild skipped: build already in progress" if h.stuck else ""
            return (f"host {h.name} state={h.state} uptime={self.uptime(h.process_start, now)}\n"
                    f"shardmap {sm['state']} epoch={sm['epoch']}; last_success={sm['last_success']}{skipped}\n"
                    f"peers known={h.known} connected={h.links}; leases "
                    f"{','.join(str(s) for s in self.leases_of(h.name)) or '-'}\n"), 0
        if sub == "lag":
            lags, per = m.flow.cell_lags(now), m.flow.p["shards"] // m.flow.p["cells"]
            rows = ["cell   backlog_records  lag_seconds"]
            for c, lag in enumerate(lags):
                b = sum(m.flow.P - m.flow.cons[s] for s in range(c * per, (c + 1) * per))
                rows.append(f"lc-{c + 1:02d}  {round(b):>15}  {round(lag):>11}")
            rows.append(f"total backlog {round(m.flow.backlog())}; iterator age max {round(max(lags) * 1000)} ms; "
                        "retention 72h")
            return "\n".join(rows) + "\n", 0
        if sub == "leases --summary":
            by = dict.fromkeys((ROUTING, STALE, "BROKEN", WARMING), 0)
            for o in m.flow.owner:
                st = f.hosts[o].state if o in f.hosts and not f.hosts[o].terminated else "BROKEN"
                by[st if st in by else "BROKEN"] += 1
            moving = sum(1 for o in m.flow.owner if o in f.hosts and f.hosts[o].state in SERVING)
            return ("leases 240; owners: " + ", ".join(f"{k} {v}" for k, v in by.items()) +
                    f"\ncheckpoints advanced in last 5 min: {moving}\n"), 0
        if sub == "membership":
            newest = sorted((x for x in f.hosts.values() if not x.terminated and x.proc), key=lambda x: -x.launched)
            return (f"srcoord members (running streamroute processes): {len(f.members())}\n"
                    + (f"this host knows {h.known} peers, connected {h.links}\n" if h.proc else
                       f"this host: streamroute not running (state {h.state})\n")
                    + f"newest members: {', '.join(x.name for x in newest[:6])}\n"), 0
        if sub == "config":
            ssm = m.ssm
            return (f"effective (read at process start {m.iso(h.process_start)}):\n"
                    f"  shardmap.bootstrap_source={h.bootstrap_source} rebuild_interval=30m ttl=45m build_workers=8\n"
                    f"  health.warmup_grace_seconds={h.grace_s} membership.discovery_window=60m dead_after_seconds=90\n"
                    f"  jvm.xmx={h.xmx} os.limit_nproc={h.limit}\n"
                    f"current SSM: bootstrap_source={ssm['/streamroute/prod/shardmap/bootstrap_source']} "
                    f"os/limit_nproc={ssm['/streamroute/prod/os/limit_nproc']} "
                    f"jvm/xmx={ssm['/streamroute/prod/jvm/xmx']}\n"), 0
        return "srctl: unknown command (see srctl --help)\n", 2

    def cat(self, h, path: str, now: float) -> tuple[str, int]:
        if path == LIMITS_CONF:
            return f"[Service]\nLimitNPROC={h.limit}\nLimitNOFILE=65536\n", 0
        if path in FILES:
            return FILES[path], 0
        if path == "/proc/meminfo":
            used = round(MEM_TOTAL * self.mem_pct(h) / 100)
            return (f"MemTotal:       {MEM_TOTAL * 1024} kB\nMemFree:          219136 kB\n"
                    f"MemAvailable:   {(MEM_TOTAL - used) * 1024:>8} kB\nBuffers:           81920 kB\n"
                    "Cached:          1738752 kB\nSwapTotal:             0 kB\n"), 0
        if path.startswith("/proc/sys/kernel/") and (key := "kernel." + path.rsplit("/", 1)[-1]) in SYSCTL:
            return f"{SYSCTL[key]}\n", 0
        for svc, tasks in (("streamroute", h.jvm_threads(self.m.fleet.p) if h.proc else None),
                           ("sr-metrics-agent", 22)):
            if tasks is not None and path in (CGROUP.format(unit=svc, what="max"),
                                              CGROUP.format(unit=svc, what="current")):
                return f"{TASKS_MAX if path.endswith('max') else tasks}\n", 0
        mm = re.fullmatch(r"/proc/(\d+|self)/(limits|status)", path)
        comm = {str(p["pid"]): p["comm"] for p in self.procs(h)} | {"self": "cat"}
        if mm and mm.group(2) == "limits" and mm.group(1) in comm:
            jvm = comm[mm.group(1)] == "java"  # its unit's LimitNPROC/LimitNOFILE; the rest, `self` too, the defaults
            return PROC_LIMITS.format(n=h.limit if jvm else NPROC, files=65536 if jvm else 1024,
                                      files_max=65536 if jvm else 524288,
                                      lock=67108864 if mm.group(1) == "self" else 65536), 0
        pids = {str(self.pid(h)): "java", "1187": "sr-metrics-agent"} if h.proc else {}
        if mm and mm.group(1) in pids:
            pid = mm.group(1)
            java = pids[pid] == "java"
            threads = h.jvm_threads(self.m.fleet.p) if java else 22
            rss = self.rss_mib(h) * 1024 if java else 49152
            return (f"Name:\t{pids[pid]}\nState:\tS (sleeping)\nTgid:\t{pid}\nPid:\t{pid}\nPPid:\t1\n"
                    f"Uid:\t996\t996\t996\t996\nVmRSS:\t{rss} kB\nThreads:\t{threads}\n"), 0
        return f"cat: {path}: No such file or directory\n", 1

    def _file_args(self, args: list[str]) -> tuple[int, list[str]]:
        n, paths, it = 10, [], iter(args)
        for a in it:
            if a in ("-n", "--lines"):
                n = int(next(it, "10").lstrip("+-") or 10)
            elif re.fullmatch(r"-n?\d+", a):
                n = int(a.lstrip("-n"))
            elif not a.startswith("-"):
                paths.append(a)
        return n, paths

    def _read(self, h, tool: str, path: str, now: float) -> tuple[str | None, str]:
        out, code = self.cat(h, path, now)
        if code == 0:
            return out, ""
        why = "Is a directory" if path.rstrip("/") in DIRS else "No such file or directory"
        return None, (f"{tool}: cannot open '{path}' for reading: {why}\n" if tool != "grep" else
                      f"grep: {path}: {why}\n")

    def head(self, h, args: list[str], now: float, tail: bool = False) -> tuple[str, int]:
        n, paths = self._file_args(args)
        out, code, tool = [], 0, "tail" if tail else "head"
        for path in paths:
            text, err = self._read(h, tool, path, now)
            if text is None:
                out.append(err)
                code = 1
                continue
            rows = text.splitlines(keepends=True)
            out.append((f"==> {path} <==\n" if len(paths) > 1 else "") + "".join(rows[-n:] if tail else rows[:n]))
        return "".join(out), code

    def tail(self, h, args: list[str], now: float) -> tuple[str, int]:
        return self.head(h, args, now, tail=True)

    def grep(self, h, args: list[str], now: float) -> tuple[str, int]:
        flags = {c for a in args if a.startswith("-") for c in a[1:]}
        rest = [a for a in args if not a.startswith("-")]
        if not rest:
            return "Usage: grep [OPTION]... PATTERNS [FILE]...\n", 2
        pat, paths, out, found, err = rest[0], rest[1:], [], 0, False
        rx = re.compile(re.escape(pat) if "F" in flags else pat, re.IGNORECASE if "i" in flags else 0)
        for path in paths:
            text, msg = self._read(h, "grep", path, now)
            if text is None:
                out.append(msg)
                err = True
                continue
            hits = [(i, x) for i, x in enumerate(text.splitlines(), 1) if bool(rx.search(x)) != ("v" in flags)]
            found += len(hits)
            pre = f"{path}:" if len(paths) > 1 else ""
            out += [f"{pre}{len(hits)}\n"] if "c" in flags else [
                f"{pre}{f'{i}:' if 'n' in flags else ''}{x}\n" for i, x in hits]
        return "".join(out), 2 if err else 0 if found else 1

    def ls(self, h, args: list[str], now: float) -> tuple[str, int]:
        paths = [a.rstrip("/") or "/" for a in args if not a.startswith("-")] or ["/home/ssm-user"]
        out, code = [], 0
        for path in paths:
            if path in DIRS:
                out.append((f"{path}:\n" if len(paths) > 1 else "") + DIRS[path].replace("  ", "\n") + "\n")
            elif path == "/home/ssm-user":
                continue
            elif self.cat(h, path, now)[1] == 0:
                out.append(path + "\n")
            else:
                out.append(f"ls: cannot access '{path}': No such file or directory\n")
                code = 2
        return "".join(out), code

    def pgrep(self, h, cmd: str, args: list[str]) -> tuple[str, int]:
        """pgrep [-c] [-w] [-f] [-x] [-l] [-a] [-u USER] [PATTERN] and pidof NAME..., over the host's processes."""
        procs, flags, users, pats, it = self.procs(h), set(), None, [], iter(args)
        for a in it:
            if a in ("-u", "-U", "--euid", "--uid"):
                users = next(it, "").split(",")
            elif a.startswith("-") and len(a) > 1 and not a.startswith("--"):
                flags |= set(a[1:].rstrip("u"))
                if a.endswith("u") and cmd == "pgrep":
                    users = next(it, "").split(",")
            elif not a.startswith("--"):
                pats.append(a)
        if cmd == "pidof":
            pids = [str(p["pid"]) for p in reversed(procs) if p["comm"] in pats]
            return (" ".join(pids[:1] if "s" in flags else pids) + "\n", 0) if pids else ("", 1)
        if not pats and users is None:
            return "pgrep: no matching criteria specified\nTry `pgrep --help' for more information.\n", 2
        try:
            rx = re.compile(pats[0]) if pats else None
        except re.error as e:
            return f"pgrep: {e}\n", 2
        hit = [p for p in procs if (users is None or p["user"] in users) and (rx is None or (
            rx.fullmatch if "x" in flags else rx.search)(p["args"] if "f" in flags else p["comm"]))]
        if "w" in flags:
            hit = [{**p, "pid": p["pid"] + k if k else p["pid"]} for p in hit for k in range(p["nlwp"])]
        if "c" in flags:
            return f"{len(hit)}\n", 0 if hit else 1
        rows = [f"{p['pid']} {p['args']}" if "a" in flags else f"{p['pid']} {p['comm']}" if "l" in flags
                else str(p["pid"]) for p in hit]
        return "".join(r + "\n" for r in rows), 0 if hit else 1

    def ps(self, h, args: list[str]) -> tuple[str, int]:
        """ps over the host's processes: -e/-A/-f/aux, -u USER, -p PID, -C NAME, -o FIELDS and --no-headers."""
        procs, fields, header, threads, it = self.procs(h), None, True, False, iter(args)
        for a in it:
            if a in ("--no-headers", "--no-heading", "-h", "h"):
                header = False
            elif a in ("-o", "-eo", "-Lo", "o", "--format"):
                spec = [f.strip() for f in next(it, "").split(",") if f.strip()]
                fields, header = [f.rstrip("=") for f in spec], header and not all(f.endswith("=") for f in spec)
                threads |= a == "-Lo"
            elif a in ("-u", "-U", "--user", "-p", "--pid", "-C"):
                key, vals = {"-p": "pid", "--pid": "pid", "-C": "comm"}.get(a, "user"), next(it, "").split(",")
                procs = [p for p in procs if str(p[key]) in vals]
            elif a.lstrip("-") and set(a.lstrip("-")) <= set("eAfFlauxLTm"):
                threads |= bool(set(a.lstrip("-")) & set("LTm"))
            else:
                return f"ps: unsupported option {a}\n", 1
        fields = fields or (["pid", "lwp", "user", "nlwp", "cmd"] if threads else ["pid", "user", "nlwp", "rss", "cmd"])
        if bad := [f for f in fields if f not in PS_FIELDS]:
            return f'ps: unknown user-defined format specifier "{bad[0]}"\n', 1
        if threads:
            procs = [{**p, "lwp": p["pid"] + k if k else p["pid"]} for p in procs for k in range(p["nlwp"])]
        for p in procs:
            p["tid"] = p["spid"] = p.setdefault("lwp", p["pid"])
        width = {"pid": 7, "user": -11, "nlwp": 4, "rss": 5, "lwp": 7, "tid": 7, "spid": 7}

        def row(vals: dict) -> str:
            return " ".join(f"{vals[f]:>{width[f]}}" if width.get(f, 0) > 0 else f"{vals[f]:<{-width[f]}}"
                            if f in width else str(vals[f]) for f in fields).rstrip()
        rows = ([row(PS_FIELDS)] if header else []) + [row(p) for p in procs]
        return "".join(r + "\n" for r in rows), 0 if procs else 1

    def systemctl(self, h, args: list[str], caller: dict, now: float) -> tuple[str, int]:
        verb = args[0] if args else "status"
        pos = [a for i, a in enumerate(args) if i and not a.startswith("-") and args[i - 1] not in ("-p", "--property")]
        unit = (pos[0] if pos else "streamroute").removesuffix(".service")
        if unit not in ("streamroute", "sr-metrics-agent", "sr-apply-limits"):
            return f"Unit {unit}.service could not be found.\n", 4
        if verb in ("restart", "start", "stop") and unit == "streamroute":
            if verb == "stop":
                return "systemctl stop streamroute: refused by srcoord fencing\n", 1
            limit_before = h.limit
            err = self.m.restart_host(h.name, now)
            if err:
                return err + "\n", 1
            h.limit = self.m.fleet.restarts[-1]["limit_nproc"] = self.m.ssm_at_restart[h.name]["limit_nproc"] = limit_before
            return "", 0
        if verb == "daemon-reload":
            return "", 0
        if verb == "show":
            props = {"Type": "simple", "LimitNPROC": h.limit, "LimitNPROCSoft": h.limit, "LimitNOFILE": 65536,
                     "TasksMax": TASKS_MAX, "TasksCurrent": h.jvm_threads(self.m.fleet.p) if h.proc else "[not set]",
                     "MemoryCurrent": self.rss_mib(h) * 1048576 if h.proc else "[not set]", "NRestarts": 0,
                     "MemoryMax": "infinity", "MainPID": self.pid(h) if h.proc else 0,
                     "ActiveState": "active" if h.proc else "inactive", "SubState": "running" if h.proc else "dead",
                     "User": "streamroute"}
            if unit == "sr-metrics-agent":
                props |= {"LimitNPROC": 62760, "LimitNPROCSoft": 62760, "TasksCurrent": 22, "MemoryCurrent": 50331648,
                          "MainPID": 1187, "ActiveState": "active", "SubState": "running"}
            elif unit == "sr-apply-limits":
                props |= {"Type": "oneshot", "LimitNPROC": 62760, "LimitNPROCSoft": 62760, "TasksCurrent": "[not set]",
                          "MemoryCurrent": "[not set]", "MainPID": 0, "ActiveState": "inactive", "SubState": "dead",
                          "User": "root"}
            want = []
            for i, a in enumerate(args):
                if a in ("-p", "--property") and i + 1 < len(args):
                    want += args[i + 1].split(",")
                elif a.startswith("--property="):
                    want += a.split("=", 1)[1].split(",")
            return "".join(f"{k}={props[k]}\n" for k in (want or props) if k in props), 0
        if verb == "cat" and unit == "streamroute":
            return (f"# /etc/systemd/system/streamroute.service\n{UNIT}\n# {LIMITS_CONF}\n[Service]\n"
                    f"LimitNPROC={h.limit}\nLimitNOFILE=65536\n"), 0
        if verb in ("status", "is-active"):
            active = h.proc if unit == "streamroute" else unit == "sr-metrics-agent"
            if verb == "is-active":
                return ("active\n" if active else "inactive\n"), 0 if active else 3
            since = self.m.iso(h.process_start) if unit == "streamroute" else self.m.iso(h.launched)
            return (f"● {unit}.service\n     Loaded: loaded (/etc/systemd/system/{unit}.service; enabled)\n"
                    f"     Active: {'active (running)' if active else 'inactive (dead)'} since {since}\n"
                    + (f"   Main PID: {self.pid(h)} (java)\n      Tasks: {h.jvm_threads(self.m.fleet.p)} (limit: "
                       f"{TASKS_MAX})\n" if unit == "streamroute" and h.proc else "")), 0 if active else 3
        return NOT_AVAILABLE.format(cmd=f"systemctl {verb}") + "\n", 127

    def journal(self, h, args: list[str], now: float) -> tuple[str, int]:
        if "-k" in args:
            return self.dmesg(h), 0
        n = 50
        for i, a in enumerate(args):
            if a in ("-n", "--lines") and i + 1 < len(args) and args[i + 1].isdigit():
                n = int(args[i + 1])
        unit = next((args[i + 1] for i, a in enumerate(args) if a == "-u" and i + 1 < len(args)), "streamroute")
        unit = unit.removesuffix(".service")
        if unit != "streamroute":
            return "-- No entries --\n", 0
        lines = [(t, f"streamroute[{self.pid(h, t)}]:", line)
                 for t, _, line in self.m.tel.lines("/vl/prod/streamroute", h.launched, now, host=h.name)]
        lines = sorted(lines + self.lifecycle(h, now), key=lambda x: x[0])
        return "".join(f"{datetime.fromtimestamp(t, self.m.tz).strftime('%b %d %H:%M:%S')} {h.name} {who} {line}\n"
                       for t, who, line in lines[-n:]) or "-- No entries --\n", 0

    def lifecycle(self, h, now: float) -> list[tuple[float, str, str]]:
        """The unit's restarts and its process's way back to ROUTING, as systemd and streamroute log them."""
        f, out = self.m.fleet, []
        for r in (r for r in f.restarts if r["host"] == h.name and r["t"] <= now):
            out.append((r["t"], "systemd[1]:", f"Stopping {UNIT_DESC}..."))
            if (up := r["t"] + f.p["stop_s"]) <= now:
                out += [(up, "systemd[1]:", "streamroute.service: Deactivated successfully."),
                        (up, "systemd[1]:", f"Started {UNIT_DESC}."),
                        (up, f"streamroute[{self.pid(h, up)}]:", BOOTSTRAP.format(**r))]
        for t, g, host, line in f.logs:
            if host == h.name and g == "/vl/prod/srcoord" and t <= now and " ROUTING " in f" {line} ":
                out.append((t, f"streamroute[{self.pid(h, t)}]:", f"INFO [srcoord] {line.removeprefix(h.name + ' ')}"))
        return out

    def dmesg(self, h) -> str:
        oom = [k for k in self.m.fleet.oom_kills if k["host"] == h.name]
        extra = "".join(f"[{int(k['t'] - h.launched):>5}.000000] Out of memory: Killed process {self.pid(h, k['t'])} (java) "
                        "total-vm:17702156kB, anon-rss:15201320kB, file-rss:0kB\n" for k in oom)
        return DMESG + extra

    def curl(self, h, args: list[str], now: float) -> tuple[str, int]:
        url = next((a for a in args if "localhost" in a or "127.0.0.1" in a), "")
        path = "/" + url.split("/", 3)[3] if url.count("/") >= 3 else url.split(":8081", 1)[-1]
        if not h.proc:
            return "curl: (7) Failed to connect to localhost port 8081: Connection refused\n", 7
        sm = self.shard_map(h, now)
        if path.endswith("/healthz"):
            return (f'{{"state": "{h.state}", "shard_map": "{sm["state"]}", "epoch": {sm["epoch"]}, '
                    f'"leases": {len(self.leases_of(h.name))}}}\n'), 0
        if path.endswith("/admin/peers"):
            return (f'{{"known": {h.known}, "connected": {h.links}, "seed_list": 32, "discovery_window": "60m", '
                    '"dead_after_seconds": 90}\n'), 0
        if path.endswith("/admin/threadpools"):
            return ('{\n  "route-worker": {"core": 256, "max": 384, "current": ' + str(h.pool) + '},\n'
                    '  "cell-client": {"threads": 192, "per_cell": 24},\n  "kinesis-consumer": {"threads": 64},\n'
                    '  "netty-eventloop": {"threads": 32},\n'
                    f'  "peerlink": {{"in": {h.links}, "out": {h.links}, "out_failed_retrying": '
                    f'{max(0, h.known - h.links)}}},\n'
                    f'  "shardmap-build": {{"workers": 8, "last_build": {{"epoch": {sm["epoch"]}, "started": '
                    f'"{h.workers_started}", "result": "{sm["state"]}"}}}}\n}}\n'), 0
        return "curl: (22) The requested URL returned error: 404\n", 22
