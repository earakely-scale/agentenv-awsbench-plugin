"""Summarise T01 QC trials: Harbor job folders and the AgentEnv driver logs of the same modes.

    python harbor/qc/summarize.py --harbor JOBS_DIR... --agentenv DRIVE_DIR... --out OUT_DIR

Writes OUT_DIR/summary.json and OUT_DIR/summary.md, and copies the kept trial of each job (the one Harbor graded; a job
retries a trial whose environment failed to start) with its verifier output (reward.json, grade.json, test-stdout.txt),
its solution or conversation log and the harness records under OUT_DIR/trials/<job>/, and each AgentEnv driver log as
OUT_DIR/agentenv/drive-<mode>.txt. A Harbor scripted trial (ScriptedSolution, judge off) and the AgentEnv driver run of
the same mode match when D12, pass and every row's (id, result, detail) agree.
"""
import argparse
import json
import re
import shutil
from pathlib import Path

ROW = re.compile(r"^  (C\d+|G\d)\s+(True|False|None)\s+(.*)$")
D12_DRIVER = re.compile(r"^  D12: score ([\d.]+) .*pass=(True|False)")


def kept_trial(job):
    trials = [p for p in job.iterdir() if p.is_dir() and (p / "result.json").exists()]
    graded = [p for p in trials if (p / "verifier").is_dir()]
    pool = graded or trials
    return max(pool, key=lambda p: (p / "result.json").stat().st_mtime) if pool else None


def harbor_trials(job_dirs, out):
    trials = {}
    for jobs in job_dirs:
        for job in sorted(p for p in Path(jobs).iterdir() if p.is_dir()):
            for trial in [kept_trial(job)] if kept_trial(job) else []:
                rec = {"job": job.name, "trial": trial.name,
                       "retried": len([p for p in job.iterdir() if p.is_dir()]) - 1}
                reward = trial / "verifier/reward.json"
                if reward.exists():
                    rec["reward"] = json.loads(reward.read_text())
                stdout = trial / "verifier/test-stdout.txt"
                if stdout.exists():
                    rec["rows"] = {m.group(1): [m.group(2), m.group(3)[:140]] for m in
                                   (ROW.match(line) for line in stdout.read_text().splitlines()) if m}
                exc = trial / "exception.txt"
                if exc.exists():
                    rec["exception"] = exc.read_text().strip().splitlines()[-1][:300]
                play = trial / "agent/play.json"
                if play.exists():
                    rec["play"] = json.loads(play.read_text())
                oracle = trial / "agent/oracle.txt"
                if oracle.exists():
                    rec["solution_mode"] = oracle.read_text().splitlines()[0][len("awsbench solution mode: "):]
                dest = out / "trials" / job.name
                if dest.exists():
                    shutil.rmtree(dest)
                dest.mkdir(parents=True)
                for src in ("verifier/reward.json", "verifier/grade.json", "verifier/test-stdout.txt",
                            "agent/play.txt", "agent/oracle.txt", "agent/conversation.json",
                            "artifacts/export/setup-record.json", "artifacts/export/post_turn_hooks.json",
                            "artifacts/export/hook-decisions.json", "artifacts/export/agent-end.json",
                            "artifacts/export/collect.json", "exception.txt"):
                    if (trial / src).exists():
                        shutil.copy(trial / src, dest / Path(src).name)
                trials[job.name] = rec
    return trials


def agentenv_runs(dirs, out):
    runs = {}
    for d in dirs:
        for log in sorted(Path(d).glob("drive-*.log")):
            mode = log.stem[len("drive-"):]
            text = log.read_text()
            rows = {m.group(1): [m.group(2), m.group(3)[:140]] for m in (ROW.match(l) for l in text.splitlines()) if m}
            d12 = next((m for m in (D12_DRIVER.match(l) for l in text.splitlines()) if m), None)
            runs[mode] = {"rows": rows, "d12": float(d12.group(1)) if d12 else None,
                          "pass": d12.group(2) == "True" if d12 else None,
                          "problems": [l for l in text.splitlines() if l.startswith("PROBLEM:")]}
            dest = out / "agentenv"
            dest.mkdir(parents=True, exist_ok=True)
            shutil.copy(log, dest / f"{log.stem}.txt")
    return runs


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--harbor", nargs="*", default=[])
    parser.add_argument("--agentenv", nargs="*", default=[])
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)
    trials = harbor_trials(args.harbor, args.out)
    runs = agentenv_runs(args.agentenv, args.out)
    lines = ["| Harbor trial | agent | judge | Harbor reward | pass | gates fired | AgentEnv driver D12 | rows equal |",
             "|---|---|---|---|---|---|---|---|"]
    for name, rec in sorted(trials.items()):
        reward = rec.get("reward") or {}
        mode = name.split("-", 2)[-1] if name.startswith("A-scripted-") else None
        ae = runs.get(mode) if mode else None
        match = "-"
        if ae and rec.get("rows"):
            diff = [k for k in sorted(set(ae["rows"]) | set(rec["rows"])) if k != "C11" and
                    ae["rows"].get(k) != rec["rows"].get(k)]
            match = "yes" if not diff and ae["d12"] == reward.get("reward") else f"no: {diff}"
        gates = ",".join(g for g in ("G1", "G2", "G3", "G4") if reward.get(g)) or "none"
        agent = "ScriptedSolution (DONE)" if name.startswith("A-") else ("nop" if "nop" in name else "oracle (solve.sh)")
        judge = {1: "on", 0: "off"}.get(reward.get("judge_on"), "-")
        lines.append(f"| {name} | {agent} | {judge} | "
                     f"{reward.get('reward', rec.get('exception', 'none'))} | {reward.get('pass', '-')} | {gates} | "
                     f"{ae['d12'] if ae else '-'} | {match} |")
    (args.out / "summary.md").write_text("\n".join(lines) + "\n")
    (args.out / "summary.json").write_text(json.dumps({"harbor": trials, "agentenv": runs}, indent=1) + "\n")
    print("\n".join(lines))


if __name__ == "__main__":
    main()
