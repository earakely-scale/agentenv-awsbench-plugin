"""Build the AwsIncidentsBench Space's and dataset's data from run folders, scrub it, check it, and optionally publish.

    python tools/build_hub_data.py --runs RUNS_DIR [--publish]

RUNS_DIR holds one folder per run (``aws-export.json``, ``workspace-export.json``, ``instance-context.json``,
``agent-session.jsonl``, ``transcript.md``, ``BUILD``), named ``<round>-<model>`` (``R5-opus``). ``hub/runs.json``
labels them: which are development runs on older builds, and a note for each. A folder it doesn't list is exported as a
current run. The reference is the scripted oracle path of ``tests/e2e/driver.py``, replayed here through the world
package in process with the unit tests' in-memory AWS and workspace fakes, so its world series are the same physics,
timeline and colleagues an agent meets.

Writes ``hub/space/app/public/data/`` (the viewers' ``index.json`` and ``runs/<id>.json``) and ``hub/dataset/`` (per run:
``viewer.json``, the scrubbed ``aws-export.json`` and ``workspace-export.json``, ``grade.json`` and ``transcript.md``;
plus ``index.json`` and the dataset viewer's ``tables/{runs,calls,checks}.jsonl``), then fails if any written file still holds a key, an internal host, a local path or endpoint, a
foreign IP or a non-emulator account id. ``--publish`` uploads both folders to the private Hugging Face repos
(``earakely-scale/AwsIncidentsBench``), one commit each.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import re
import shutil
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
import export_run as X

AWS_ENV = ROOT / "src/agentenv_awsbench/bundles/awsbench/envs/aws"
SPACE_DATA = ROOT / "hub/space/app/public/data"
DATASET = ROOT / "hub/dataset"
REPO = "earakely-scale/AwsIncidentsBench"
KEEP_IN_DATASET = ("README.md", ".gitattributes")


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


def _head() -> str:
    """The last commit that changed what the reference replays: the env, the driver or the fakes."""
    try:
        return subprocess.run(["git", "-C", str(ROOT), "log", "-1", "--format=%h", "--", "src", "tests/e2e/driver.py",
                               "tests/unit/test_world_t01.py"], capture_output=True, text=True, check=True).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return ""


# ---------------------------------------------------------------- runs

def run_folders(runs_dir: Path) -> list[Path]:
    return sorted(p for p in runs_dir.iterdir() if (p / "aws-export.json").exists() and (p / "instance-context.json").exists())


def order_key(run_id: str):
    m = re.match(r"R(\d+)-(\w+)", run_id)
    return (-int(m.group(1)), m.group(2)) if m else (0, run_id)


def index_row(v: dict) -> dict:
    fails = [c["id"] for c in v["checks"] if not c["gate"] and not c["result"]]
    gates = [c["id"] for c in v["checks"] if c["gate"] and c["fired"]]
    return {"id": v["id"], "label": v["label"], "kind": v["kind"], "model": v["model"], "build": v["build"], "dev": v["dev"],
            "note": v["note"], "d12": v["score"]["d12"], "raw": v["score"]["raw"], "pass": v["score"]["pass"],
            "caps": v["score"]["caps"], "failed": fails, "gates": gates,
            "core_failed": [c for c in fails if c in ("C1", "C2", "C3", "C4", "C6", "C7")],
            "calls": v["clock"]["calls_used"], "done": X.hms(v["clock"]["done"]), "end_reason": v["clock"]["end_reason"],
            "collapsed_min": v["summary"]["collapsed_min"], "stable_from": v["summary"]["stable_from"]}


def results_table(index: dict) -> str:
    """The dataset card's results table, from the index."""
    rows = ["| Run | Model | Build | D12 | Pass | Checks lost | Gates fired | Calls | DONE |",
            "| --- | --- | --- | --- | --- | --- | --- | --- | --- |"]
    for r in index["runs"] + [index["reference"]]:
        d12 = "" if r["d12"] is None else f"{r['d12']:.2f}"
        dev = " (dev)" if r["dev"] else ""
        rows.append(f"| `{r['id']}` | {r['model']} | `{r['build']}`{dev} | {d12} | {'pass' if r['pass'] else 'fail'} | "
                    f"{', '.join(r['failed']) or 'none'} | {', '.join(r['gates']) or 'none'} | {r['calls']} | "
                    f"{r['done'] or r['end_reason']} |")
    return "\n".join(rows)


def fill_card(index: dict) -> Path:
    card = DATASET / "README.md"
    text = card.read_text()
    start, end = "<!-- results -->", "<!-- /results -->"
    head, rest = text.split(start, 1)
    card.write_text(head + start + "\n" + results_table(index) + "\n" + end + rest.split(end, 1)[1])
    return card


def _hms(t) -> str:
    return X.hms(t) if t is not None else ""


def tables(views: list[dict], grades: dict[str, list[dict]]) -> dict[str, list[dict]]:
    """Flat rows for the dataset viewer: one per run, one per call, one per check."""
    runs, calls, checks = [], [], []
    for v in views:
        row = index_row(v)
        runs.append({"run": v["id"], "kind": v["kind"], "model": v["model"], "build": v["build"], "development_run": v["dev"],
                     "d12": v["score"]["d12"], "raw": v["score"]["raw"], "passed": v["score"]["pass"],
                     "caps": ", ".join(str(c) for c in v["score"]["caps"]), "checks_lost": ", ".join(row["failed"]),
                     "gates_fired": ", ".join(row["gates"]), "calls": v["clock"]["calls_used"], "done_at": _hms(v["clock"]["done"]),
                     "end_reason": v["clock"]["end_reason"], "restore_at": _hms(v["summary"].get("restore_at")),
                     "collapsed_min": float(v["summary"]["collapsed_min"] or 0), "checkout_stable_from": v["summary"]["stable_from"] or "",
                     "note": v["note"], "final_message": v["final"] or "", "handoff_note": v["handoff"] or ""})
        for st in v["steps"]:
            calls.append({"run": v["id"], "call": st["n"], "start": _hms(st["at"]), "end": _hms(st["end"]), "tool": st["tool"],
                          "server": st["server"], "cost_class": st["cls"], "cost_s": st["cost"], "detail": st["detail"] or "",
                          "result": st["result"] or "", "error": st["error"] or "", "production_change": bool(st["prod"]),
                          "label": st["label"] or "", "bears_on": ", ".join(st["sat"])})
        rows = {r.get("id"): r for r in grades.get(v["id"], [])}
        for c in v["checks"]:
            g = rows.get(c["id"], {})
            checks.append({"run": v["id"], "check": c["id"], "name": c["name"], "bucket": g.get("bucket") or c.get("bucket") or "",
                           "points": c["w"], "core": bool(c["core"]), "gate_cap": X.GATE_CAP.get(c["gate"]) if c["gate"] else None,
                           "passed": bool(c["result"]), "fired": bool(c["fired"]), "detail": g.get("detail") or c.get("detail") or "",
                           "graded": "grader" if g else "scripted"})
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


def build(runs_dir: Path) -> list[Path]:
    manifest = json.loads((ROOT / "hub/runs.json").read_text())
    for folder in run_folders(runs_dir):
        build_id = (folder / "BUILD").read_text().strip() if (folder / "BUILD").exists() else ""
        if not re.fullmatch(r"[0-9a-f]{7,40}", build_id):
            raise SystemExit(f"{folder.name}: BUILD holds {build_id!r}, not the commit the env was built from")
    if SPACE_DATA.exists():
        shutil.rmtree(SPACE_DATA)
    for child in DATASET.iterdir() if DATASET.exists() else []:
        if child.name not in KEEP_IN_DATASET:
            shutil.rmtree(child) if child.is_dir() else child.unlink()
    written: list[Path] = []
    views = []
    grades: dict[str, list[dict]] = {}
    for folder in sorted(run_folders(runs_dir), key=lambda p: order_key(p.name)):
        meta = manifest.get("runs", {}).get(folder.name, {})
        view = X.export_run(folder, folder.name, meta.get("label"), bool(meta.get("dev")), meta.get("note", ""))
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
        print(f"  {view['id']:10} {view['model']:18} build {view['build']}  D12 {view['score']['d12']}  "
              f"{'pass' if view['score']['pass'] else 'FAIL'}  calls {view['clock']['calls_used']}")
    folders = run_folders(runs_dir)
    ws0 = json.loads((folders[-1] / "workspace-export.json").read_text()) if folders else {}
    ref = reference_view(X.people(ws0))
    written.append(write_json(SPACE_DATA / "runs" / "reference.json", ref))
    written.append(write_json(DATASET / "reference" / "viewer.json", ref))
    print(f"  reference  scripted oracle    build {ref['build']}  calls {ref['clock']['calls_used']}  "
          f"done {X.hms(ref['clock']['done'])}  collapsed {ref['summary']['collapsed_min']} min")
    index = {"schema": "awsincidentsbench.index/v1", "task": "T01", "generated_from": [v["id"] for v in views],
             "reference": index_row(ref), "runs": [index_row(v) for v in views]}
    written.append(write_json(SPACE_DATA / "index.json", index))
    written.append(write_json(DATASET / "index.json", index))
    for name, rows in tables(views + [ref], grades).items():
        written.append(write_jsonl(DATASET / "tables" / f"{name}.jsonl", rows))
    written.append(fill_card(index))
    X.check_clean(written)
    for p in written:
        if X.KEY_FRAGMENT in p.read_text(errors="ignore"):
            raise SystemExit(f"key fragment in {p}")
    print(f"wrote {len(written)} files; scrub check clean")
    return written


def publish() -> None:
    from huggingface_hub import HfApi
    api = HfApi()
    for path, kind in ((ROOT / "hub/space", "space"), (DATASET, "dataset")):
        for f in path.rglob("*"):
            if f.is_file() and "node_modules" not in f.parts and X.KEY_FRAGMENT in f.read_text(errors="ignore"):
                raise SystemExit(f"key fragment in {f}")
        api.create_repo(REPO, repo_type=kind, private=True, exist_ok=True,
                        **({"space_sdk": "docker"} if kind == "space" else {}))
        api.upload_folder(repo_id=REPO, repo_type=kind, folder_path=str(path), commit_message="Update AwsIncidentsBench data",
                          ignore_patterns=["app/node_modules/**", "app/dist/**", "app/.astro/**", "**/.DS_Store"],
                          delete_patterns=["runs/**", "reference/**", "tables/**", "app/public/data/**"])
        print(f"published {kind} {REPO}")


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--runs", type=Path, required=True, help="folder of run folders")
    ap.add_argument("--publish", action="store_true", help="upload the Space and the dataset (private), one commit each")
    a = ap.parse_args(argv)
    build(a.runs)
    if a.publish:
        publish()


if __name__ == "__main__":
    main()
