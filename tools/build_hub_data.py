"""Build the AwsIncidentsBench Space's and dataset's data from run folders, scrub it, check it, and optionally publish.

    python tools/build_hub_data.py --runs RUNS_DIR [--publish]

RUNS_DIR holds one folder per run (``aws-export.json``, ``workspace-export.json``, ``instance-context.json``,
``agent-session.jsonl``, ``transcript.md``, ``BUILD``), named ``<round>-<model>`` (``R8-opus-1``, ``T03R1-sonnet-2``).
The task (T01 or T03) is the scenario the aws export's episode names. ``hub/runs.json`` labels them: which are
development runs on older builds, a note for each, and a ``build`` where the folder's BUILD file lacks the commit. A
folder it doesn't list is exported as a current run. Each task has a reference: the scripted oracle path
(``tests/e2e/driver.py`` for T01, ``tests/e2e/t03_oracle.py`` for T03), replayed here through the world package in
process with in-memory AWS and workspace fakes, so its world series are the same physics, timeline and colleagues an
agent meets. T03 runs are also replayed in process from their own calls (``tools/t03_replay.py``), for the per-host
states and thread counts their exports don't keep; the T03 world comes from ``--t03-root`` (this checkout once it has
the T03 world, else the sibling ``agentenv-awsbench-plugin-t03`` worktree).

Writes ``hub/space/app/public/data/`` (the viewers' ``index.json`` and ``runs/<id>.json``) and ``hub/dataset/`` (per run:
``viewer.json``, the scrubbed ``aws-export.json`` and ``workspace-export.json``, ``grade.json`` and ``transcript.md``;
plus ``index.json``, the dataset viewer's ``tables/<task>/{runs,calls,checks}.jsonl`` and each task's card,
``tasks/<task>/{README.md,task.json}`` from ``tools/build_task_cards.py``, which the Space reads as ``tasks.json``), then fails if any written file still holds a key, an internal host, a local path or endpoint, a
foreign IP or a non-emulator account id. ``--publish`` uploads both folders to the public Hugging Face repos
(``earakely-scale/AwsIncidentsBench``), one commit each.

Films: every script in ``hub/films/`` is rendered by ``tools/render_films.mjs`` over the run data just written (node,
playwright-core, Chrome and ffmpeg; ``--playwright DIR`` or ``PLAYWRIGHT_CORE`` names the playwright-core folder) into
``hub/space/app/public/films/`` (``<id>.mp4``, ``.poster.png``, ``.vtt``, with the scripts and their stage under
``scripts/`` so film.html's interactive links resolve) and into the dataset's ``films/<task>/``. ``--no-films`` keeps the
MP4s already in public/films instead of rendering. Each ``hub/showcase/failures-<task>.json`` (the failure showcase a
chapter reads) is written to ``public/data/`` and beside the task's films in the dataset.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import re
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
import build_task_cards as TC
import export_run as X
import export_t03 as X3

AWS_ENV = ROOT / "src/agentenv_awsbench/bundles/awsbench/envs/aws"
SPACE_DATA = ROOT / "hub/space/app/public/data"
DATASET = ROOT / "hub/dataset"
REPO = "earakely-scale/AwsIncidentsBench"
KEEP_IN_DATASET = ("README.md", ".gitattributes")
FILM_SCRIPTS = ROOT / "hub/films"
SHOWCASE = ROOT / "hub/showcase"
SPACE_FILMS = ROOT / "hub/space/app/public/films"
FILM_MEDIA = (".mp4", ".poster.png", ".vtt")
FILM_MAX_BYTES = 8 * 1024 * 1024
TASKS = {"T01": "The Empty Ledger Endpoint", "T03": "Thread Ceiling"}
REF_ID = {"T01": "reference", "T03": "reference-t03"}
CORE = {"T01": ("C1", "C2", "C3", "C4", "C6", "C7"), "T03": ("C02", "C03", "C04", "C05", "C06", "C15")}


def default_t03_root() -> Path:
    if (AWS_ENV / "awsbench_aws/scenarios/t03.yaml").exists():
        return ROOT
    return ROOT.parent / (ROOT.name + "-t03")


def _load(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# ---------------------------------------------------------------- the reference

def reference_view(people: dict | None = None) -> dict:
    """The oracle path played through the world in process: the meter's costs, the world's physics and colleagues."""
    sys.path.insert(0, str(AWS_ENV))
    from awsbench_aws import meter
    from awsbench_aws.world import World, load_scenario, replay_seed
    fakes = _load(ROOT / "tests/unit/test_world_t01.py", "awsbench_world_fakes")
    driver = _load(ROOT / "tests/e2e/driver.py", "awsbench_driver")
    scenario = load_scenario("t01")
    aws, ws = fakes.FakeAws(), fakes.FakeWs()
    replay_seed(scenario, aws)
    start_ssm = dict(aws.ssm)
    world = World(scenario, "reference", aws, ws)
    world.start()
    agent = {"role": "sre-primary", "principal": "sre-primary"}
    now = datetime.fromisoformat(scenario["start"])
    steps, rows = [], []
    found = {"zone_id": "Z00001", "callum_mail": "mail-callum-1", "status_id": "si-1"}
    for n, (tool, raw) in enumerate(driver.oracle_steps(48199), start=1):
        args = driver.fill(raw, found)
        t_issue = now
        result = "scripted"
        if tool == "wait":
            target = (datetime.fromisoformat(f"{now.date()}T{args['until']}-05:00") if args.get("until")
                      else now + timedelta(minutes=float(args["minutes"])))
            world.advance_to(target)
            now = target
            cls, cost = "wait", 0
            result = f"waited until {now.strftime('%H:%M:%S')}"
        else:
            margs = {"command": args.get("cmd") or args.get("command") or ""}
            cls = meter.classify(tool, margs if tool in ("fleet_exec", "aws_cli") else args,
                                 scenario.get("cost_overrides"), 1 if tool == "fleet_exec" else None)
            cost = {"point_read": 20, "write": 45, "broad": 75}.get(cls, 0)
            now = now + timedelta(seconds=cost)
            world.advance_to(now)
            call = {"t": now, "tool": tool, "role": "sre-primary", "server": "aws", "args": args}
            if tool == "fleet_exec":
                out = world.fleet_exec(args["host"], args["cmd"], agent)
                host = out["hosts"][0]
                result = f"{host['host']} [{host['exit_code']}]: " + " / ".join(str(host.get("stdout", "")).splitlines()[:4])
                call["args"] = {"selector": args["host"], "command": args["cmd"]}
            elif tool == "aws_cli":
                m = re.search(r"put-parameter --name (\S+) --value (\S+)", args["command"])
                if m:
                    params = {"Name": m.group(1), "Value": m.group(2), "Type": "String", "Overwrite": True}
                    aws.call("ssm", "PutParameter", params, principal="sre-primary", world_made=False)
                    call.update(service="ssm", operation="PutParameter", params=params)
                    rows.append({"call_no": n, "t": now.isoformat(), "service": "ssm", "operation": "PutParameter",
                                 "params": params, "world_made": False, "role": "sre-primary"})
            else:
                call["server"] = "workspace"
            world.observe(call)
        detail = X.detail_of(tool, {"selector": args.get("host"), "command": args.get("cmd")} if tool == "fleet_exec" else args)
        text = f"{tool} {json.dumps(args)}"
        prod = cls != "wait" and (bool(re.search(r"put-parameter", args.get("command", ""))) or bool(
            re.search(r"epctl (pause|apply)", args.get("cmd", ""))))
        steps.append({"n": n, "at": X.sec(t_issue.isoformat()), "end": X.sec(now.isoformat()), "tool": tool,
                      "server": "workspace" if tool.split("_")[0] in ("chat", "tickets", "wiki", "mail", "pager", "status") else "aws",
                      "cls": cls, "cost": cost, "detail": detail, "args": {k: X.short(v, 160) for k, v in args.items()},
                      "result": X.short(result, 420), "error": None, "exit": 0 if tool == "fleet_exec" else None,
                      "prod": prod, "label": X.prod_label(tool, detail) if prod else None, "sat": X.sat_of(tool, args, text),
                      "zone": X.zone_of(tool, text), "purpose": X.PURPOSE.get(tool, tool), "where": X.where_of(tool, args)})
    done = now
    world.advance_to(datetime.fromisoformat(scenario["horizon"]))
    exp = world.export()
    fake_export = {"episode": {"start": scenario["start"], "window_end": scenario["window_end"], "horizon": scenario["horizon"]},
                   "world": exp, "action_log": rows, "start_state": {"ssm": start_ssm}}
    series, hosts, levers = X.build_series(fake_export)
    summary = X.build_summary(fake_export, series, steps)
    events = _reference_events(ws.ops, fake_export, summary, people or {})
    board = {"ticket": [[s["end"], "SEV1"] for s in steps if s["tool"] == "tickets_update"],
             "status": [[s["end"], s["args"].get("status", "")] for s in steps if s["tool"].startswith("status_")]}
    checks = _reference_checks(steps, series, summary)
    return X.scrub_obj({
        "schema": X.SCHEMA, "id": "reference", "label": "Reference · scripted oracle", "kind": "reference",
        "task": "T01", "task_title": "The Empty Ledger Endpoint", "model": "Scripted oracle", "model_id": "",
        "harness": "tests/e2e/driver.py --mode oracle, replayed in process", "build": _head(), "dev": False,
        "note": ("The scripted oracle path, played through the world package in process with the unit tests' in-memory "
                 "AWS and workspace fakes: the same physics, timeline and colleagues. On a deployed env the same path "
                 "grades D12 1.0; the check marks here are the ones it is built to pass."),
        "clock": {"start": X.S0, "window_end": X.HZ, "horizon": X.HZ, "done": X.sec(done.isoformat()), "end_reason": "done",
                  "calls_used": len(steps), "max_calls": 250},
        "score": {"d12": 1.0, "raw": 1.0, "pass": True, "caps": [], "buckets": {"outcome": 68, "process": 20, "comms": 12},
                  "why": "raw 100 · no cap", "measured": "D12 1.0 when the same path runs on a deployed env"},
        "checks": checks, "steps": steps, "series": series, "hosts": hosts, "levers": levers, "board": board,
        "dns": X.build_dns(fake_export, series), "summary": summary, "events": events,
        "messages": X.agent_messages(steps), "final": "DONE", "handoff": next(
            (s["args"].get("body", "") for s in steps if s["tool"] == "tickets_comment"), ""),
    })


NPC = {"hannah.becker": ("Hannah Becker", "Payments engineering lead"), "priya.raman": ("Priya Raman", "Rotation IC"),
       "kwame.asante": ("Kwame Asante", "endpointd owner"), "owen.gallagher": ("Owen Gallagher", "Merchant Success")}


def _reference_events(ops, export: dict, summary: dict, people: dict) -> list[dict]:
    ev = []
    for op, args, at in ops:
        t = X.sec(at.isoformat())
        if not X.S0 <= t <= X.HZ:
            continue
        if op in ("chat.post_as", "chat.dm_as"):
            user = args.get("user") or args.get("from_user") or ""
            if user == "support-desk":
                continue  # its tickets carry the fakes' ids, not the workspace's SUP- keys
            name, role = people.get(user) or NPC.get(user, (user, ""))
            kind = "bot" if user in X.BOTS or user.endswith("-bot") else "npc"
            ev.append({"t": t, "kind": kind, "who": name, "role": role, "where": args.get("channel", "dm"),
                       "text": X.short(args.get("text", ""), 600)})
        elif op == "mail.deliver":
            sender = args.get("from", "")
            name, role = people.get(sender) or (sender.split("@")[0].replace(".", " ").title(), sender.split("@")[-1])
            ev.append({"t": t, "kind": "email", "who": name, "role": role, "where": "email",
                       "text": X.short(f"{args.get('subject', '')}: {args.get('body', '')}", 500)})
        elif op == "pager.page_as":
            ev.append({"t": t, "kind": "page", "who": "pager", "role": str(args.get("to", "")), "where": "",
                       "text": X.short(args.get("summary", ""), 300)})
    base = X.build_events(export, {}, summary)
    return sorted(base + ev, key=lambda e: e["t"])


def _reference_checks(steps: list[dict], series: dict, summary: dict) -> list[dict]:
    first_ok = next((X.S0 + X.DT * i for i, v in enumerate(series["ok"]) if v), None)
    served = lambda cid: [s for s in steps if cid in s["sat"]]
    out = []
    for cid, (name, short_name, core, deadline, window) in X.CHECK_META.items():
        gate = {"G1": "A", "G2": "B", "G3": "C", "G4": "C"}.get(cid)
        if gate:
            at = None
        elif cid in X.END_STATE:
            at = X.HZ
        elif cid == "C2":
            at = first_ok
        else:
            s = served(cid)
            at = s[-1]["end"] if s else X.HZ
        points = {"C1": 6, "C2": 20, "C3": 12, "C4": 10, "C5": 3, "C6": 18, "C7": 8, "C8": 3, "C9": 3, "C10": 4,
                  "C11": 6, "C12": 2, "C13": 5}.get(cid, 0)
        out.append({"id": cid, "name": name, "title": name, "short": short_name, "bucket": None, "w": points, "core": core,
                    "gate": gate, "M": cid == "C3", "result": True, "fired": False if gate else None,
                    "detail": "the path is built to pass this check", "at": at, "deadline": deadline, "window": window})
    return out


def _head(root: Path = ROOT, paths=("src", "tests/e2e/driver.py", "tests/unit/test_world_t01.py")) -> str:
    """The last commit that changed what the reference replays: the env, the driver or the fakes."""
    try:
        return subprocess.run(["git", "-C", str(root), "log", "-1", "--format=%h", "--", *paths],
                              capture_output=True, text=True, check=True).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return ""


# ---------------------------------------------------------------- runs

def build_of(folder: Path) -> str:
    """The commit in a run folder's BUILD file: a bare sha, or a ``commit <sha>`` first line, shortened to 7."""
    text = (folder / "BUILD").read_text().strip() if (folder / "BUILD").exists() else ""
    m = re.match(r"(?:commit\s+)?([0-9a-f]{7,40})\b", text)
    return m.group(1)[:7] if m else text.splitlines()[0] if text else ""


def world_at(root: Path, build: str, work: Path) -> Path:
    """The T03 world as the run's own build had it (``git archive`` of ``src`` and ``tests``), so a run's calls are
    replayed through the physics it met; ``root`` itself when that build is its head or isn't in its history."""
    git = ["git", "-C", str(root)]
    try:
        sha = subprocess.run(git + ["rev-parse", "--verify", build + "^{commit}"], capture_output=True, text=True,
                             check=True).stdout.strip()
        head = subprocess.run(git + ["rev-parse", "HEAD"], capture_output=True, text=True, check=True).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return root
    if sha == head:
        return root
    out = work / "worlds" / sha[:12]
    if not out.exists():
        out.mkdir(parents=True)
        tar = subprocess.run(git + ["archive", sha, "src", "tests"], capture_output=True, check=True).stdout
        subprocess.run(["tar", "-x", "-C", str(out)], input=tar, check=True)
    return out


def run_folders(runs_dir: Path) -> list[Path]:
    return sorted(p for p in runs_dir.iterdir() if (p / "aws-export.json").exists() and (p / "instance-context.json").exists())


def order_key(run_id: str):
    m = re.match(r"(?:T\d+)?R(\d+)-([\w-]+)", run_id)
    return (-int(m.group(1)), m.group(2)) if m else (0, run_id)


def index_row(v: dict) -> dict:
    fails = [c["id"] for c in v["checks"] if not c["gate"] and c["result"] is False]
    gates = [c["id"] for c in v["checks"] if c["gate"] and c["fired"]]
    row = {"id": v["id"], "task": v.get("task", "T01"), "label": v["label"], "kind": v["kind"], "model": v["model"],
           "build": v["build"], "dev": v["dev"], "note": v["note"], "d12": v["score"]["d12"], "raw": v["score"]["raw"],
           "pass": v["score"]["pass"], "caps": v["score"]["caps"], "failed": fails, "gates": gates,
           "core_failed": [c for c in fails if c in CORE[v.get("task", "T01")]],
           "calls": v["clock"]["calls_used"], "done": X.hms(v["clock"]["done"]), "end_reason": v["clock"]["end_reason"],
           "final": v.get("regrade")}
    sm = v["summary"]
    if v.get("task") == "T03":
        row.update(said_done=X.hms(v["clock"].get("said_done")) if v["clock"].get("said_done") else None,
                   partial={c["id"]: [c["earned"], c["w"]] for c in v["checks"]
                            if not c["gate"] and c["result"] is False and c.get("earned")},
                   routing_from=X.hms(sm["routing_from"]), lag_clear_at=X.hms(sm["lag_clear_at"]),
                   settlement=sm["settlement"].get("readiness_1230"), agent_restarts=sm["agent_restarts"],
                   peak_conc=sm["peak_conc"], ejections=sm["ejections"], hold=sm["hold"])
    else:
        row.update(collapsed_min=sm["collapsed_min"], stable_from=sm["stable_from"])
    return row


def results_table(index: dict) -> str:
    """The dataset card's results tables, one per task, from the index."""
    out = []
    for task, t in index["tasks"].items():
        out += ["", f"**{task} · {t['title']}**", "",
                "| Run | Model | Build | D12 graded | Pass | Checks lost | Gates fired | D12 final | Calls | End |",
                "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |"]
        for r in t["runs"] + [t["reference"]]:
            d12 = "" if r["d12"] is None else d12_text(r["d12"])
            dev = " (dev)" if r["dev"] else ""
            out.append(f"| `{r['id']}` | {r['model']} | `{r['build']}`{dev} | {d12} | {'pass' if r['pass'] else 'fail'} | "
                       f"{lost_text(r)} | {', '.join(r['gates']) or 'none'} | {final_text(r)} | {r['calls']} | {end_text(r)} |")
        notes = [r for r in t["runs"] if r.get("final") and (r["final"]["losses"] or not same_grade(r))]
        if notes:
            out += ["", f"**Final grader** (`{index['final_grader']['build']}`). Why a run's final D12 differs from the "
                        "grade it got, or whose the points it still loses are (*fair*: the agent's; *environment*: a "
                        "world or colleague defect fixed since):", ""]
            out += [f"- `{r['id']}`: {final_text(r)}{' · ' + LOSSES[r['final']['losses']] if r['final']['losses'] else ''}. "
                    f"{r['final']['why']}" for r in notes]
        if any(r.get("partial") for r in t["runs"]):
            out += ["", "A check lost with points in brackets was given part of them."]
        said = [r for r in t["runs"] if r.get("said_done")]
        if said:
            builds = sorted({r["build"] for r in said})
            out += ["", f"The runs on build{'s' if len(builds) > 1 else ''} {', '.join(f'`{b}`' for b in builds)} replied "
                        "`RESULT: DONE` before the window closed, at the time in brackets, but that build's turn hook looked "
                        "for DONE on a reply's last line and T03's prompt puts a two-line summary after it, so it missed "
                        "the reply and the episode ran on to the window's end (fixed since). Calls after it count against "
                        "the budget and are graded like any other."]
    return "\n".join(out[1:])


LOSSES = {"fair": "fair", "env": "environment", "mixed": "partly environment"}
MODELS = (("Claude Haiku 5.5", "Claude Code"), ("Claude Sonnet 5.5", "Claude Code, build 1633470"),
          ("Claude Opus 5.5", "Claude Code"), ("Claude Fable 5.2", "Claude Code"), ("GPT-6 Astra", "Codex CLI, effort medium"))


def models_table(index: dict) -> str:
    """The dataset card's cross-model view of T01's current (non-development) runs, under the final grader."""
    fin = lambda r: r["final"] if r.get("final") else {"d12": r["d12"], "pass": r["pass"], "failed": r["failed"], "gates": r["gates"]}
    rows = [r for r in index["tasks"]["T01"]["runs"] if not r["dev"]]
    out = ["| Model | Agent | Runs | Pass@1 | Mean D12 | Lost most | Failures |", "| --- | --- | --- | --- | --- | --- | --- |"]
    for name, via in MODELS:
        mine = sorted((r for r in rows if r["model"] == name), key=lambda r: [int(x) if x.isdigit() else x for x in re.split(r"(\d+)", r["id"])])
        if not mine:
            continue
        n = sum(fin(r)["pass"] for r in mine)
        mean = sum(fin(r)["d12"] for r in mine) / len(mine)
        lost: dict[str, int] = {}
        for r in mine:
            for c in [*fin(r)["failed"], *fin(r)["gates"]]:
                lost[c] = lost.get(c, 0) + 1
        top = sorted(lost.items(), key=lambda kv: (-kv[1], [int(x) if x.isdigit() else x for x in re.split(r"(\d+)", kv[0])]))[:3]
        rounds = " + ".join(sorted({r["id"].split("-")[0] for r in mine}, key=lambda x: int(x[1:])))
        fails = [f"`{r['id']}` {LOSSES.get((r.get('final') or {}).get('failure') or (r.get('final') or {}).get('losses'), 'unlabelled')}"
                 for r in mine if not fin(r)["pass"]]
        out.append(f"| {name} | {via} | {len(mine)} ({rounds}) | **{n}/{len(mine)} ({round(100 * n / len(mine))} %)** | "
                   f"{mean:.3f} | {', '.join(f'{c} ×{k}' for c, k in top) or 'nothing'} | {', '.join(fails) or 'none'} |")
    return "\n".join(out)


def same_grade(r: dict) -> bool:
    f = r.get("final") or {}
    return f.get("d12") is not None and abs(f["d12"] - r["d12"]) < 1e-6 and f["pass"] == r["pass"]


def final_text(r: dict) -> str:
    """The final grader's D12, marked when it changes the grade ("**1.00** (was 0.40, now a pass)")."""
    f = r.get("final")
    if not f:
        return "" if r.get("kind") == "reference" else "not regraded"
    flip = "" if f["pass"] == r["pass"] else ", now a pass" if f["pass"] else ", now a fail"
    return d12_text(f["d12"]) if same_grade(r) else f"**{d12_text(f['d12'])}** (was {d12_text(r['d12'])}{flip})"


def d12_text(d12: float) -> str:
    """Two decimals, or as many as the grade has, up to four (0.935 is not 0.94, 0.9875 is not 0.988)."""
    return next(f"{d12:.{n}f}" for n in (2, 3, 4, 6) if n == 6 or abs(d12 * 10 ** n - round(d12 * 10 ** n)) < 1e-6)


def lost_text(r: dict) -> str:
    """The checks a run didn't get in full, with the points of a partial one ("C11 (2.5 of 5)")."""
    part = r.get("partial") or {}
    return ", ".join(f"{c} ({part[c][0]:g} of {part[c][1]:g})" if c in part else c for c in r["failed"]) or "none"


def end_text(r: dict) -> str:
    if r["end_reason"] == "done" and r["done"]:
        return f"DONE {r['done']}"
    if r["end_reason"] == "window_end":
        window = "13:00" if r.get("task") == "T03" else "05:10"
        return f"{window} window" + (f" (said DONE {r['said_done']})" if r.get("said_done") else "")
    return r["end_reason"] or ""


def render_films(playwright: str | None) -> None:
    """Render every film script into SPACE_FILMS with tools/render_films.mjs, over the run data already written."""
    out = Path(tempfile.mkdtemp(prefix="awsbench-films-"))
    cmd = ["node", str(ROOT / "tools/render_films.mjs"), "--all", "--out", str(out), "--no-stills", "--check", "2"]
    if playwright:
        cmd += ["--playwright", playwright]
    for attempt in range(3):  # --check can trip on a fresh page's text antialiasing alone; it doesn't repeat
        r = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True)
        if r.returncode == 0:
            break
        print(f"  render_films.mjs failed (attempt {attempt + 1}): {r.stderr.strip().splitlines()[-1:]}")
    else:
        raise SystemExit("tools/render_films.mjs failed three times:\n" + r.stderr[-3000:])
    if SPACE_FILMS.exists():
        shutil.rmtree(SPACE_FILMS)
    SPACE_FILMS.mkdir(parents=True)
    for script in sorted(FILM_SCRIPTS.glob("*.json")):
        fid = json.loads(script.read_text()).get("id") or script.stem
        for ext in FILM_MEDIA:
            shutil.copy2(out / f"{fid}{ext}", SPACE_FILMS / f"{fid}{ext}")
    shutil.rmtree(out, ignore_errors=True)


def films(render: bool = True, playwright: str | None = None) -> list[Path]:
    """Put each film's media, its script and the stage it names beside film.html and in the dataset's films/<task>/,
    and each task's failure showcase in public/data and the dataset. Returns the text files written, for the scrub."""
    if render:
        render_films(playwright)
    written: list[Path] = []
    for script in sorted(FILM_SCRIPTS.glob("*.json")):
        doc = json.loads(script.read_text())
        fid, task = doc.get("id") or script.stem, doc.get("task", "T01")
        for ext in FILM_MEDIA:
            src = SPACE_FILMS / f"{fid}{ext}"
            if not src.exists():
                raise SystemExit(f"{src} is missing: run without --no-films to render the films")
            if ext == ".mp4" and src.stat().st_size > FILM_MAX_BYTES:
                raise SystemExit(f"{src} is {src.stat().st_size / 1048576:.1f} MB, over the 8 MB a film may take")
            dst = DATASET / "films" / task / f"{fid}{ext}"
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
            if ext == ".vtt":
                written += [src, dst]
        for base in (SPACE_FILMS / "scripts", DATASET / "films" / task / "scripts"):
            written.append(write_json(base / script.name, doc))
            if isinstance(doc.get("stage"), str):
                stage = (script.parent / doc["stage"]).resolve()
                written.append(write_json(base / doc["stage"], json.loads(stage.read_text())))
    for show in sorted(SHOWCASE.glob("failures-*.json")):
        doc = json.loads(show.read_text())
        task = doc.get("task", "T01")
        written.append(write_json(SPACE_DATA / show.name, doc))
        written.append(write_json(DATASET / "films" / task / show.name, doc))
    return written


def films_table() -> str:
    """The dataset card's films section: one row per film in each task's failure showcase."""
    rows = ["| Film | Failure family | Model | Run | Compared with | Length | MP4 |", "| --- | --- | --- | --- | --- | --- | --- |"]
    for show in sorted(SHOWCASE.glob("failures-*.json")):
        doc = json.loads(show.read_text())
        task = doc.get("task", "T01")
        fams = {f["id"]: f for f in doc.get("families", [])}
        models = {m["id"]: m for m in doc.get("models", [])}
        order = {f["id"]: f["n"] for f in doc.get("families", [])}
        for film in sorted(doc.get("films", []), key=lambda f: (order.get(f["family"], 9), f["id"])):
            mp4 = SPACE_FILMS / f"{film['id']}.mp4"
            size = f"{mp4.stat().st_size / 1048576:.1f} MB" if mp4.exists() else ""
            fam = fams.get(film["family"], {})
            shown = ", ".join(models[m]["short"] for m in film.get("models_shown", [film["model"]]) if m in models)
            rows.append(f"| [`{film['id']}`](films/{task}/{film['id']}.mp4) · {film['title']} | {fam.get('n', '')}. "
                        f"{fam.get('name', film['family'])} | {shown} | `{film['run']}` | "
                        f"{('`' + film['compare'] + '`') if film.get('compare') else 'none (montage)'} | "
                        f"{film['duration_s']} s | {size} |")
    return "\n".join(rows)


def fill_card(index: dict, cards: dict[str, dict]) -> Path:
    card = DATASET / "README.md"
    text = card.read_text()
    for name, body in (("results", results_table(index)), ("models", models_table(index)), ("tasks", TC.tasks_table(cards)),
                       ("films", films_table())):
        start, end = f"<!-- {name} -->", f"<!-- /{name} -->"
        head, rest = text.split(start, 1)
        text = head + start + "\n" + body + "\n" + end + rest.split(end, 1)[1]
    card.write_text(text)
    return card


def _hms(t) -> str:
    return X.hms(t) if t is not None else ""


def tables(views: list[dict], grades: dict[str, list[dict]]) -> dict[str, list[dict]]:
    """Flat rows for the dataset viewer: one per run, one per call, one per check."""
    runs, calls, checks = [], [], []
    for v in views:
        row = index_row(v)
        sm, t03 = v["summary"], v.get("task") == "T03"
        runs.append({"run": v["id"], "task": v.get("task", "T01"), "kind": v["kind"], "model": v["model"], "build": v["build"],
                     "development_run": v["dev"],
                     "d12": v["score"]["d12"], "raw": v["score"]["raw"], "passed": v["score"]["pass"],
                     "final_d12": (v.get("regrade") or {}).get("d12"), "final_passed": (v.get("regrade") or {}).get("pass"),
                     "final_why": (v.get("regrade") or {}).get("why"), "final_losses": (v.get("regrade") or {}).get("losses"),
                     "caps": ", ".join(str(c) for c in v["score"]["caps"]), "checks_lost": ", ".join(row["failed"]),
                     "gates_fired": ", ".join(row["gates"]), "calls": v["clock"]["calls_used"], "done_at": _hms(v["clock"]["done"]),
                     "end_reason": v["clock"]["end_reason"],
                     "said_done_at": _hms(v["clock"].get("said_done")) if t03 else None,
                     "restore_at": None if t03 else _hms(sm.get("restore_at")),
                     "collapsed_min": None if t03 else float(sm["collapsed_min"] or 0),
                     "checkout_stable_from": None if t03 else (sm["stable_from"] or ""),
                     "routing_95_from": _hms(sm.get("routing_from")) if t03 else None,
                     "lag_under_5min_at": _hms(sm.get("lag_clear_at")) if t03 else None,
                     "settlement_1230": sm["settlement"].get("readiness_1230") if t03 else None,
                     "agent_restarts": sm.get("agent_restarts") if t03 else None,
                     "ejections": sm.get("ejections") if t03 else None,
                     "note": v["note"], "final_message": v["final"] or "",
                     "handoff_note": "" if t03 else (v["handoff"] or ""), "rca_comment": (v["handoff"] or "") if t03 else ""})
        for st in v["steps"]:
            calls.append({"run": v["id"], "task": v.get("task", "T01"), "call": st["n"], "start": _hms(st["at"]), "end": _hms(st["end"]), "tool": st["tool"],
                          "server": st["server"], "cost_class": st["cls"], "cost_s": st["cost"], "detail": st["detail"] or "",
                          "result": st["result"] or "", "error": st["error"] or "", "production_change": bool(st["prod"]),
                          "label": st["label"] or "", "bears_on": ", ".join(st["sat"])})
        rows = {r.get("id"): r for r in grades.get(v["id"], [])}
        for c in v["checks"]:
            g = rows.get(c["id"], {})
            checks.append({"run": v["id"], "task": v.get("task", "T01"), "check": c["id"], "name": c["name"], "bucket": g.get("bucket") or c.get("bucket") or "",
                           "points": c["w"], "earned": c.get("earned"), "core": bool(c["core"]), "gate_cap": X.GATE_CAP.get(c["gate"]) if c["gate"] else None,
                           "passed": bool(c["result"]), "fired": bool(c["fired"]), "detail": g.get("detail") or c.get("detail") or "",
                           "graded": "grader" if g else ("grader rule on the replay" if c.get("graded") else "scripted")})
    return {"runs": runs, "calls": calls, "checks": checks}


def write_jsonl(path: Path, rows: list[dict]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows))
    return path


def grade_doc(ctx: dict, view: dict) -> dict:
    grade = ctx["metadata"]["verifications"]["grade"]
    return {"run": view["id"], "model": view["model_id"], "build": view["build"], "dev": view["dev"],
            "score": grade.get("score"), "rows": grade.get("results", [])}


def write_json(path: Path, obj) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, ensure_ascii=False, separators=(",", ":")))
    return path


def build(runs_dir: Path, t03_root: Path | None = None, render: bool = True, playwright: str | None = None) -> list[Path]:
    manifest = json.loads((ROOT / "hub/runs.json").read_text())
    t03_root = t03_root or default_t03_root()
    folders = run_folders(runs_dir)
    builds = {}
    for folder in folders:
        meta = manifest.get("runs", {}).get(folder.name, {})
        build_id = meta.get("build") or build_of(folder)
        if not re.fullmatch(r"[0-9a-f]{7,40}", build_id):
            raise SystemExit(f"{folder.name}: BUILD holds {build_id!r}, not the commit the env was built from; write it "
                             f"there or as \"build\" in hub/runs.json")
        builds[folder.name] = build_id
    tasks = {f.name: X.task_of(f) for f in folders}
    if "T03" in tasks.values() and not (t03_root / AWS_ENV.relative_to(ROOT) / "awsbench_aws/scenarios/t03.yaml").exists():
        raise SystemExit(f"T03 runs need a checkout with the T03 world: {t03_root} has none (--t03-root)")
    if SPACE_DATA.exists():
        shutil.rmtree(SPACE_DATA)
    for child in DATASET.iterdir() if DATASET.exists() else []:
        if child.name not in KEEP_IN_DATASET:
            shutil.rmtree(child) if child.is_dir() else child.unlink()
    work = Path(tempfile.mkdtemp(prefix="awsbench-hub-"))
    written: list[Path] = []
    views = []
    grades: dict[str, list[dict]] = {}
    for folder in sorted(folders, key=lambda p: (tasks[p.name], order_key(p.name))):
        meta = manifest.get("runs", {}).get(folder.name, {})
        root = world_at(t03_root, builds[folder.name], work) if tasks[folder.name] == "T03" else t03_root
        view = X.export_run(folder, folder.name, meta.get("label"), bool(meta.get("dev")), meta.get("note", ""),
                            build=builds[folder.name], t03_root=root, work=work)
        if meta.get("final"):
            view["regrade"] = dict(meta["final"], grader=manifest.get("final_grader", {}).get("build"))
        views.append(view)
        written.append(write_json(SPACE_DATA / "runs" / f"{view['id']}.json", view))
        out = DATASET / "runs" / view["id"]
        written.append(write_json(out / "viewer.json", view))
        for name in ("aws-export.json", "workspace-export.json"):
            if (folder / name).exists():
                written.append(write_json(out / name, X.scrub_obj(json.loads((folder / name).read_text()))))
        ctx = json.loads((folder / "instance-context.json").read_text())
        grade = X.scrub_obj(grade_doc(ctx, view))
        grades[view["id"]] = grade["rows"]
        written.append(write_json(out / "grade.json", grade))
        if (folder / "transcript.md").exists():
            p = out / "transcript.md"
            p.write_text(X.scrub((folder / "transcript.md").read_text()))
            written.append(p)
        print(f"  {view['task']} {view['id']:15} {view['model']:18} build {view['build']}  D12 {view['score']['d12']}  "
              f"{'pass' if view['score']['pass'] else 'FAIL'}  calls {view['clock']['calls_used']}")
    refs = {}
    for task in sorted(set(tasks.values()) | {"T01"}):
        same = [f for f in folders if tasks[f.name] == task]
        ws0 = json.loads((same[-1] / "workspace-export.json").read_text()) if same else {}
        if task == "T01":
            ref = reference_view(X.people(ws0))
        else:
            ref = X3.reference_t03(t03_root, work, X.people(ws0),
                                   _head(t03_root, ("src", "tests/e2e/t03_oracle.py")))
        refs[task] = ref
        written.append(write_json(SPACE_DATA / "runs" / f"{ref['id']}.json", ref))
        written.append(write_json(DATASET / ref["id"] / "viewer.json", ref))
        print(f"  {task} {ref['id']:15} scripted oracle    build {ref['build']}  calls {ref['clock']['calls_used']}  "
              f"done {X.hms(ref['clock']['done'])}")
    by_task = {task: {"title": TASKS[task], "reference": index_row(refs[task]),
                      "runs": [index_row(v) for v in views if v.get("task", "T01") == task]} for task in sorted(refs)}
    index = {"schema": "awsincidentsbench.index/v2", "generated_from": [v["id"] for v in views], "tasks": by_task,
             "final_grader": manifest.get("final_grader"),
             "task": "T01", "reference": by_task["T01"]["reference"], "runs": by_task["T01"]["runs"]}
    written.append(write_json(SPACE_DATA / "index.json", index))
    written.append(write_json(DATASET / "index.json", index))
    for name, rows in tables(views + list(refs.values()), grades).items():
        for task in sorted(refs):
            mine = [r for r in rows if r["task"] == task]
            keep = [k for k in (mine[0] if mine else {}) if any(r.get(k) is not None for r in mine)]
            written.append(write_jsonl(DATASET / "tables" / task.lower() / f"{name}.jsonl",
                                       [{k: r.get(k) for k in keep} for r in mine]))
    cards = TC.cards()
    written += TC.write_cards(DATASET / "tasks", cards)
    written.append(write_json(SPACE_DATA / "tasks.json", {"schema": TC.SCHEMA, "tasks": cards}))
    written += films(render, playwright)
    written.append(fill_card(index, cards))
    X.check_clean(written)
    for p in written:
        if X.KEY_FRAGMENT in p.read_text(errors="ignore"):
            raise SystemExit(f"key fragment in {p}")
    shutil.rmtree(work, ignore_errors=True)
    print(f"wrote {len(written)} files; scrub check clean")
    return written


def publish() -> None:
    from huggingface_hub import HfApi
    api = HfApi()
    for path, kind in ((ROOT / "hub/space", "space"), (DATASET, "dataset")):
        for f in path.rglob("*"):
            if (f.is_file() and "node_modules" not in f.parts and f.suffix not in (".mp4", ".png")
                    and X.KEY_FRAGMENT in f.read_text(errors="ignore")):
                raise SystemExit(f"key fragment in {f}")
        api.create_repo(REPO, repo_type=kind, private=True, exist_ok=True,
                        **({"space_sdk": "docker"} if kind == "space" else {}))
        api.upload_folder(repo_id=REPO, repo_type=kind, folder_path=str(path), commit_message="Update AwsIncidentsBench data",
                          ignore_patterns=["app/node_modules/**", "app/dist/**", "app/.astro/**", "**/.DS_Store"],
                          delete_patterns=["runs/**", "reference/**", "reference-*/**", "tables/**", "tasks/**",
                                           "films/**", "app/public/data/**", "app/public/films/**"])
        print(f"published {kind} {REPO}")


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--runs", type=Path, required=True, help="folder of run folders")
    ap.add_argument("--publish", action="store_true", help="upload the Space and the dataset, one commit each")
    ap.add_argument("--t03-root", type=Path, help="a checkout with the T03 world (default: this one if it has it, else "
                                                   "the sibling agentenv-awsbench-plugin-t03 worktree)")
    ap.add_argument("--no-films", action="store_true", help="keep the films already in hub/space/app/public/films "
                                                             "instead of rendering them again")
    ap.add_argument("--playwright", help="a folder holding node_modules/playwright-core, for rendering the films "
                                         "(default: PLAYWRIGHT_CORE, then this checkout)")
    a = ap.parse_args(argv)
    build(a.runs, a.t03_root, render=not a.no_films, playwright=a.playwright)
    if a.publish:
        publish()


if __name__ == "__main__":
    main()
