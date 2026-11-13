"""Fleets of hosts: listing, inspection, allowlisted exec (dig, getent, unbound-control, systemctl, journalctl, ps,
curl, epctl, ...) and rolling restart jobs."""

import fnmatch
import hashlib
import json
import re
import shlex
from datetime import datetime, timedelta

from .endpointd import COMPONENTS, host_of
from .util import hms, iso, unit

NOT_AVAILABLE = "{cmd}: not available in this environment"
SUDO = re.compile(r"^\s*sudo\s+")  # the hosts run fleet commands as root already
SHELL_OPS = re.compile(r"[;|<>`]|&&|\$\(")
ONE_COMMAND = "one command per call: shell operators are not supported"
UNBOUND = {"pid": 612, "user": "unbound", "cmd": "/usr/sbin/unbound -d -p",
           "desc": "Validating, recursive, and caching DNS resolver"}
BOOT = "Mon 2026-10-06 23:28:11 EDT"


def instance_id(host: str) -> str:
    return "i-0" + hashlib.sha256(host.encode()).hexdigest()[:16]


class Fleet:
    def __init__(self, world, cfg: dict):
        self.w = world
        self.cfg = cfg
        self.hosts: dict[str, dict] = {}
        for fleet, spec in cfg.items():
            for h in spec["hosts"]:
                h = dict(h) if isinstance(h, dict) else {"name": h}
                h.setdefault("az", spec.get("az_of", {}).get(h["name"][-4:-3], "use1-az6"))
                h["fleet"] = fleet
                h.setdefault("state", "running")
                self.hosts[h["name"]] = h
        self.jobs: dict[str, dict] = {}
        self.files = {k: v for spec in cfg.values() for k, v in (spec.get("files") or {}).items()}

    def ingress(self) -> list[dict]:
        return [h for h in self.hosts.values() if h["fleet"] == "ledger-ingress"]

    def list_hosts(self, fleet=None, az=None) -> list[dict]:
        out = []
        for h in self.hosts.values():
            if fleet and h["fleet"] != fleet or az and h["az"] != az:
                continue
            row = {"host": h["name"], "fleet": h["fleet"], "az": h["az"], "ip": h.get("ip"), "state": h["state"]}
            if h["fleet"] == "ledger-ingress":
                row["ready"] = bool(h.get("ready"))
            out.append(row)
        return out

    def select(self, selector: str) -> list[str]:
        sel = (selector or "").strip()
        if sel in self.cfg:
            return [h for h, v in self.hosts.items() if v["fleet"] == sel]
        if ":" in sel:
            fleet, az = sel.split(":", 1)
            return [h for h, v in self.hosts.items() if v["fleet"] == fleet and fnmatch.fnmatchcase(v["az"], az)]
        names = [s.strip() for s in sel.split(",") if s.strip()]
        return [h for h in self.hosts if any(fnmatch.fnmatchcase(h, n) for n in names)]

    def inspect(self, host: str, now: datetime) -> dict:
        h = self.hosts.get(host)
        if h is None:
            return {"error": f"unknown host {host}"}
        out = {"host": host, "instance_id": instance_id(host), "fleet": h["fleet"], "az": h["az"], "ip": h.get("ip"),
               "state": h["state"],
               "os": h.get("os", self.cfg[h["fleet"]].get("os", "Ubuntu 22.04.4 LTS")),
               "instance_type": h.get("type", self.cfg[h["fleet"]].get("instance_type"))}
        if host in self.w.dns.hosts:
            self.w.dns.update(host, now)
            out["resolver"] = {"local": "unbound 1.13.1 on 127.0.0.1", "forwards_to": self.w.dns.resolver}
        if h["fleet"] == "endpointd":
            comp = next(c for c in COMPONENTS if host_of(c) == host)
            ep = self.w.endpointd
            out["services"] = {f"endpointd-{comp}": "active (running)" if ep.processes[comp] else "inactive (dead)"}
            out["kill_switch"] = "on" if ep.switch(comp) else "off"
        if h["fleet"] == "ledger-ingress":
            out["envoy_ready"] = bool(h.get("ready"))
        return out

    # ---- exec ----
    def exec(self, selector: str, command: str, caller: dict, now: datetime) -> dict:
        if SHELL_OPS.search(command or ""):
            return {"hosts": [], "exit_code": 2, "error": ONE_COMMAND}
        hosts = self.select(selector)
        if not hosts:
            return {"hosts": [], "error": f"no hosts match {selector!r}"}
        out = []
        for host in hosts:
            stdout, code = self.run(host, command, caller, now)
            out.append({"host": host, "stdout": stdout, "exit_code": code})
        return {"hosts": out}

    def run(self, host: str, command: str, caller: dict, now: datetime) -> tuple[str, int]:
        h = self.hosts[host]
        command = SUDO.sub("", command or "")
        try:
            argv = shlex.split(command)
        except ValueError as e:
            return f"sh: {e}\n", 2
        if not argv:
            return "", 0
        cmd = argv[0]
        if cmd == "dig":
            return self.dig(host, argv[1:], now)
        if cmd == "getent" and argv[1:2] == ["hosts"] and len(argv) == 3:
            return self.getent(host, argv[2], now)
        if cmd == "unbound-control" and len(argv) >= 2 and argv[1] in ("flush", "flush_zone", "flush_negative"):
            return self.flush(host, argv, now)
        if cmd == "systemctl" and len(argv) == 3 and h["fleet"] == "endpointd" and argv[1] != "status":
            return self.systemctl(host, argv[1], argv[2], now)
        if cmd == "systemctl" and len(argv) >= 3 and argv[1] == "status":
            return self.status(host, argv[-1], now)
        if cmd == "curl":
            return self.curl(host, argv[1:], now)
        if cmd == "epctl" and h["fleet"] == "endpointd":
            return self.w.epctl.run(command, host, caller, now)
        if cmd == "cat" and len(argv) == 2:
            body = self.files.get(argv[1])
            return (body if body.endswith("\n") else body + "\n", 0) if body is not None else (
                f"cat: {argv[1]}: No such file or directory\n", 1)
        if cmd == "journalctl":
            return self.journal(host, argv, now)
        if cmd == "ps":
            return self.ps(host, argv[1:]), 0
        if cmd in ("hostname", "uptime", "date"):
            return {"hostname": host, "date": now.strftime("%a %b %d %H:%M:%S %Z %Y"),
                    "uptime": f" {hms(now)} up 41 days,  3:12,  0 users,  load average: 0.31, 0.27, 0.22"}[cmd] + "\n", 0
        if cmd == "envoy-certcheck" and h["fleet"] == "ledger-ingress":
            if h.get("ready"):
                return "OK sds secret ingress-tls loaded; listener ledger_https ready\n", 0
            return ("ERROR sds secret ingress-tls not loaded: certsync bootstrap path missing on arm64 "
                    "(since 02:11:20); listener ledger_https NOT READY\n"), 1
        return NOT_AVAILABLE.format(cmd=cmd) + "\n", 127

    def _ns_answer(self, host, name, now, bypass):
        """(status, ips, ttl) for `name` as `host` sees it."""
        live = [r for r in self.w.mirror.live(name) if r["Weight"] > 0]
        dns = self.w.dns
        if name == dns.name and not bypass and host in dns.hosts:
            hs = dns.update(host, now)
            if hs["state"] == "neg":
                return "NXDOMAIN", [], int((hs["neg_until"] - now).total_seconds())
        if not live:
            return "NXDOMAIN", [], int(dns.neg_ttl.total_seconds())
        total = sum(r["Weight"] for r in live)
        u = unit(self.w.seed, "dig", host, name, int(now.timestamp()) // 60) * total
        acc = 0
        for r in sorted(live, key=lambda r: r["SetIdentifier"] or r["Value"]):
            acc += r["Weight"]
            if u < acc:
                return "NOERROR", [r["Value"]], int(r["TTL"])
        return "NOERROR", [live[-1]["Value"]], int(live[-1]["TTL"])

    def dig(self, host, args, now):
        short = "+short" in args
        names = [a for a in args if not a.startswith("+") and not a.startswith("@") and a not in ("A", "IN", "-t")]
        server = next((a[1:] for a in args if a.startswith("@")), None)
        if not names:
            return "dig: no name given\n", 1
        name = names[0].rstrip(".")
        bypass = server is not None and server not in ("127.0.0.1", "127.0.0.53", "localhost")
        status, ips, ttl = self._ns_answer(host, name, now, bypass)
        if short:
            return "".join(ip + "\n" for ip in ips), 0
        qid = int(unit(self.w.seed, "qid", host, now.timestamp()) * 65535)
        used = server or "127.0.0.1"
        lines = [f"; <<>> DiG 9.18.28-0ubuntu0.22.04.1-Ubuntu <<>> {' '.join(args)}", ";; global options: +cmd",
                 ";; Got answer:",
                 f";; ->>HEADER<<- opcode: QUERY, status: {status}, id: {qid}",
                 f";; flags: qr rd ra; QUERY: 1, ANSWER: {len(ips)}, AUTHORITY: {0 if ips else 1}, ADDITIONAL: 1", "",
                 ";; QUESTION SECTION:", f";{name}.\t\t\tIN\tA", ""]
        if ips:
            lines += [";; ANSWER SECTION:"] + [f"{name}.\t{ttl}\tIN\tA\t{ip}" for ip in ips] + [""]
        else:
            zone = self.w.mirror.zone
            lines += [";; AUTHORITY SECTION:",
                      (f"{zone}.\t{max(ttl, 0)}\tIN\tSOA\tns-1536.awsdns-00.co.uk. awsdns-hostmaster.amazon.com. "
                       "1 7200 900 1209600 86400"), ""]
        lines += [";; Query time: 0 msec" if not bypass else ";; Query time: 2 msec",
                  f";; SERVER: {used}#53({used}) (UDP)", f";; WHEN: {now.strftime('%a %b %d %H:%M:%S %Z %Y')}",
                  ";; MSG SIZE  rcvd: 112", ""]
        return "\n".join(lines), 0

    def getent(self, host, name, now):
        _, ips, _ = self._ns_answer(host, name.rstrip("."), now, False)
        if not ips:
            return "", 2
        return f"{ips[0]:<15} {name}\n", 0

    def flush(self, host, argv, now):
        if host not in self.w.dns.hosts:
            return "ok\n", 0
        target = argv[2].rstrip(".") if len(argv) > 2 else ""
        if argv[1] == "flush_negative" or target in (self.w.dns.name, self.w.mirror.zone):
            self.w.dns.flush(host, now)
        return "ok\n", 0

    def systemctl(self, host, verb, unit_name, now):
        comp = next((c for c in COMPONENTS if host_of(c) == host), None)
        if unit_name.removesuffix(".service") != f"endpointd-{comp}":
            return f"Unit {unit_name} could not be found.\n", 4
        if verb in ("stop", "start", "restart"):
            self.w.set_process(comp, verb != "stop", now)
            return "", 0
        return NOT_AVAILABLE.format(cmd=f"systemctl {verb}") + "\n", 127

    # ---- units: systemctl status, journalctl, ps, curl ----
    def units(self, host: str) -> dict[str, dict]:
        """The host's systemd units: unbound everywhere, endpointd's component on its host, the fleet's `units`."""
        h = self.hosts[host]
        out = {"unbound": UNBOUND}
        comp = next((c for c in COMPONENTS if host_of(c) == host), None)
        if comp:
            out[f"endpointd-{comp}"] = {"pid": 2147, "cmd": f"/usr/local/bin/endpointd --component {comp}",
                                        "desc": f"endpointd {comp}", "port": 9100, "health": "endpointd"}
        out.update(self.cfg[h["fleet"]].get("units") or {})
        return out

    def _running(self, host: str, name: str) -> bool:
        return not name.startswith("endpointd-") or self.w.endpointd.processes[name.removeprefix("endpointd-")]

    def status(self, host, unit_name, now):
        name = unit_name.removesuffix(".service")
        u = self.units(host).get(name)
        if u is None:
            return f"Unit {name}.service could not be found.\n", 4
        running = self._running(host, name)
        lines = [f"\u25cf {name}.service - {u.get('desc', name)}",
                 f"     Loaded: loaded (/etc/systemd/system/{name}.service; enabled; vendor preset: enabled)",
                 f"     Active: {'active (running) since ' + BOOT + '; 41 days ago' if running else 'inactive (dead)'}",
                 f"   Main PID: {str(u['pid']) + ' (' + u['cmd'].split()[0].rsplit('/', 1)[-1] + ')' if running else '-'}"]
        if name.startswith("endpointd-"):
            comp, ep = name.removeprefix("endpointd-"), self.w.endpointd
            halted = running and comp != "planner" and ep.engaged()
            lines.append(f"     Status: \"kill switch {'on' if ep.switch(comp) else 'off'}"
                         f"{'; HALTED: ' + ep.latch_reasons()[0] if halted else ''}\"")
        elif name == "envoy" and not self.hosts[host].get("ready"):
            lines.append('     Status: "listener ledger_https NOT READY (sds secret ingress-tls not loaded)"')
        lines += [""] + self.journal_lines(host, name, now)[-5:]
        return "\n".join(lines) + "\n", (0 if running else 3)

    def journal_lines(self, host: str, unit_name: str | None, now: datetime) -> list[str]:
        """journalctl lines up to `now`: the scenario's own, endpointd's halt and apply events, and per minute the app
        log lines the unit's `log_group` writes as this host sees the outage."""
        h, units = self.hosts[host], self.units(host)
        rows: list[tuple[str, str]] = []
        for line in h.get("journal") or self.cfg[h["fleet"]].get("journal") or []:
            ts, rest = line.split(" ", 1)
            rows.append((ts, rest))
        comp = next((c for c in COMPONENTS if host_of(c) == host), None)
        if comp:
            ep = self.w.endpointd
            if comp != "planner" and ep.engaged() and ep.processes[comp]:
                why = ep.latch_reasons()[0]
                rows.append((hms(now), f"endpointd[2147]: WARN halt latch engaged: {why}; writing nothing"))
            rows += [(ev["t"][11:19], f"endpointd[2147]: INFO batch gen {ev['gen']} {ev['event']}")
                     for ev in ep.apply_events[-5:] if ev["enactor"] == comp]
        tz = self.w.start_t.tzinfo
        off = int(unit(self.w.seed, "journal", host) * 15)
        for name, u in units.items():
            if not u.get("log_group") or unit_name and name != unit_name:
                continue
            t = (now - timedelta(minutes=30)).replace(second=42 - off, microsecond=0)
            while t <= now:
                rows += [(hms(t.astimezone(tz)), f"{name}[{u['pid']}]: {line}")
                         for line in self.w.telemetry.lines(u["log_group"], t, host)]
                t += timedelta(minutes=1)

        def owner(rest: str) -> str:
            proc = rest.split("[", 1)[0]
            return proc if proc in units else next((n for n in units if n.startswith(proc)), proc)
        day = now.strftime("%b %d")
        return [f"{day} {ts} {host} {rest}" for ts, rest in sorted(rows, key=lambda r: r[0])
                if ts <= hms(now) and (not unit_name or owner(rest) == unit_name)]

    def journal(self, host, argv, now):
        unit_name, n = None, 20
        for i, a in enumerate(argv):
            nxt = argv[i + 1] if i + 1 < len(argv) else ""
            if a in ("-u", "--unit"):
                unit_name = nxt
            elif a.startswith("--unit="):
                unit_name = a.split("=", 1)[1]
            elif a in ("-n", "--lines") and nxt.isdigit():
                n = int(nxt)
            elif a.startswith("--lines=") and a[8:].isdigit():
                n = int(a[8:])
        if unit_name is not None:
            unit_name = unit_name.removesuffix(".service")
            if unit_name not in self.units(host):
                return "-- No entries --\n", 0
        lines = self.journal_lines(host, unit_name, now)
        if not lines:
            return "-- No entries --\n", 0
        return "\n".join(lines[-n:]) + "\n", 0

    def ps(self, host, args=()):
        procs = [(1, "root", "/sbin/init")] + [(u["pid"], u.get("user", "root"), u["cmd"])
                                              for name, u in self.units(host).items() if self._running(host, name)]
        procs.sort()
        if not any(set(a.lstrip("-")) & set("aexuf") for a in args):
            return "\n".join(["    PID TTY          TIME CMD"] +
                             [f"{pid:>7} ?        00:{pid % 50:02d}:{pid % 60:02d} {cmd.split()[0].rsplit('/', 1)[-1]}"
                              for pid, _, cmd in procs]) + "\n"
        rows = ["USER         PID %CPU %MEM     VSZ    RSS TTY      STAT START   TIME COMMAND"]
        for pid, user, cmd in procs:
            cpu = round(unit(self.w.seed, "cpu", host, pid) * 12, 1) if pid > 1 else 0.0
            rows.append(f"{user[:8]:<8} {pid:>7} {cpu:>4} {1.0 + pid % 7:>4.1f} {pid * 97 % 900000 + 20000:>7} "
                        f"{pid * 31 % 200000 + 9000:>6} ?        Ssl  Oct06  {pid % 90:>3}:{pid % 60:02d} {cmd}")
        return "\n".join(rows) + "\n"

    def curl(self, host, args, now):
        flags = {a for a in args if a.startswith("-")}
        skip = {"-o", "-H", "-X", "-w", "-m", "-d", "--max-time", "--connect-timeout", "--output", "--header"}
        url = next((a for i, a in enumerate(args) if not a.startswith("-") and (i == 0 or args[i - 1] not in skip)),
                   None)
        if url is None:
            return "curl: no URL specified!\n", 2
        m = re.match(r"^(?:\w+://)?([^/:]+)(?::(\d+))?(/.*)?$", url)
        if not m:
            return f"curl: (3) URL using bad/illegal format or missing URL: {url}\n", 3
        name, port, path = m.group(1), int(m.group(2) or (443 if url.startswith("https") else 80)), m.group(3) or "/"
        if name in ("localhost", "127.0.0.1"):
            u = next(((n, u) for n, u in self.units(host).items() if u.get("port") == port and self._running(host, n)),
                     None)
            if u is None:
                return (f"curl: (7) Failed to connect to {name} port {port} after 0 ms: Couldn't connect to server\n",
                        7)
            code, body = self.health(host, u[0], u[1], path, now)
        else:
            _, ips, _ = self._ns_answer(host, name.rstrip("."), now, False)
            if not ips:
                return f"curl: (6) Could not resolve host: {name}\n", 6
            code, body = self.health(host, "ledger-writer", {"health": "ledger-writer"}, path, now) \
                if name == self.w.dns.name else (200, {"status": "UP"})
        if code >= 400 and flags & {"-f", "-sf", "-fs", "--fail", "-fsS", "-sSf"}:
            return f"curl: (22) The requested URL returned error: {code}\n", 22
        text = body if isinstance(body, str) else json.dumps(body, separators=(",", ":"))
        return text + "\n", 0

    def health(self, host, name, u, path, now) -> tuple[int, dict | str]:
        """What a unit's local health endpoint answers right now."""
        kind, w = u.get("health"), self.w
        regime = w.storm.regime
        if kind == "envoy":
            return (200, "LIVE") if self.hosts[host].get("ready") else (503, "PRE_INITIALIZING")
        if kind == "endpointd":
            comp, ep = name.removeprefix("endpointd-"), w.endpointd
            reasons = ep.latch_reasons()
            state = "PAUSED" if not ep.switch(comp) else ("HALTED: " + reasons[0] if reasons and comp != "planner"
                                                          else "RUNNING")
            return 200, {"component": comp, "kill_switch": "on" if ep.switch(comp) else "off", "state": state}
        if kind == "ledger-writer":
            if regime == "collapsed":
                return 503, {"status": "DEGRADED", "db_pool": {"active": 25, "idle": 0, "waiting": 212}}
            busy = 18 if regime == "degraded" else 4
            return 200, {"status": "UP", "db_pool": {"active": busy, "idle": 25 - busy, "waiting": 0}}
        resolves = host not in w.dns.hosts or w.dns.resolves(host, now)
        ledger = ({"status": "DOWN", "error": "java.net.UnknownHostException: ledger.internal.vantleaf.io"}
                  if not resolves else {"status": "DOWN", "error": "SocketTimeoutException: Read timed out after 2000ms"}
                  if regime == "collapsed" else {"status": "UP"})
        if kind == "checkout":
            up = ledger["status"] == "UP"
            return (200 if up else 503), {"status": "UP" if up else "DOWN", "components": {
                "ledger": ledger, "ledgerClientBreaker": {"status": "CLOSED" if up else "OPEN"}}}
        if kind == "dispatcher":
            lev = w.levers()
            return 200, {"status": "UP", "consumer_paused": lev["consumer_paused"],
                         "max_concurrency": int(lev["max_concurrency"]), "ledger": ledger["status"]}
        return 200, {"status": "UP"}

    # ---- restart jobs ----
    def restart(self, selector, batch_size, caller, now):
        hosts = self.select(selector)
        if not hosts:
            return {"error": f"no hosts match {selector!r}"}
        jid = f"job-{len(self.jobs) + 1:04d}"
        size = max(1, int(batch_size or 1))
        batches = [hosts[i:i + size] for i in range(0, len(hosts), size)]
        per = timedelta(seconds=float(self.cfg.get("restart_batch_s", 90)) if isinstance(self.cfg.get(
            "restart_batch_s"), (int, float)) else 90)
        sched = [(now + per * (i + 1), b) for i, b in enumerate(batches)]
        self.jobs[jid] = {"job_id": jid, "selector": selector, "hosts": hosts, "batch_size": size,
                          "started_at": now, "schedule": sched, "done": [], "status": "running",
                          "by": caller.get("role")}
        return {"job_id": jid, "hosts": len(hosts), "batches": len(batches), "eta": iso(sched[-1][0])}

    def next_due(self):
        dues = [s[0] for j in self.jobs.values() if j["status"] == "running" for s in j["schedule"]
                if s[1][0] not in j["done"]]
        return min(dues, default=None)

    def complete_jobs(self, now):
        out = []
        for j in self.jobs.values():
            if j["status"] != "running":
                continue
            for due, batch in j["schedule"]:
                if due <= now and batch[0] not in j["done"]:
                    for host in batch:
                        j["done"].append(host)
                        if host in self.w.dns.hosts:
                            self.w.dns.flush(host, due)
                        comp = next((c for c in COMPONENTS if host_of(c) == host), None)
                        if comp:
                            self.w.set_process(comp, True, due)
            if len(j["done"]) >= len(j["hosts"]):
                j["status"] = "succeeded"
                j["finished_at"] = max(s[0] for s in j["schedule"])
                out.append({"t": iso(j["finished_at"]), "kind": "job_done", "from": "fleet", "where": j["job_id"],
                            "preview": f"restart {j['selector']}: {len(j['hosts'])} hosts restarted"})
        return out

    def job_status(self, jid, now):
        j = self.jobs.get(jid)
        if j is None:
            return {"error": f"unknown job {jid}"}
        return {"job_id": jid, "status": j["status"], "hosts": len(j["hosts"]), "done": len(j["done"]),
                "started_at": iso(j["started_at"]), "finished_at": iso(j.get("finished_at"))}

    def export_jobs(self):
        return [{"job_id": j["job_id"], "selector": j["selector"], "status": j["status"], "by": j["by"],
                 "started_at": iso(j["started_at"]), "finished_at": iso(j.get("finished_at")), "hosts": j["hosts"]}
                for j in self.jobs.values()]
