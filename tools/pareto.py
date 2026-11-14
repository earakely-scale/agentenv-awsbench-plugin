"""Cost against score for every task: one point per run, a marker per model at its means, and the Pareto frontier.

    python tools/pareto.py [--regrade REGRADE.json] [--out hub/pareto] [--readme-assets docs/assets]

x is the solver's model spend per run in US dollars (``hub/run_costs.json``: Claude Code's total_cost_usd, or the list
price of a Codex run's token usage; judge calls are not counted), y the final grader's D12 (``hub/runs.json``'s
``final``, the same numbers as the regrade record ``--regrade`` names, which the tool checks when it is given). Every
run with a final grade is plotted; a run without a known cost is left out and named in ``notes``. A model's marker sits
at its mean cost and mean D12; the frontier joins the models no other model beats on both (cheaper or as cheap, and
scoring as well or better). x is logarithmic when a task's costs span more than 10x.

Writes ``pareto.json`` (the Space's chart data), ``pareto.csv`` (every point), ``table.md`` (the dataset card's section)
and, with matplotlib, ``pareto-<task>.png`` and ``pareto-<task>-dark.png``; ``--readme-assets`` also copies T01's two
PNGs there for the GitHub README.
"""
from __future__ import annotations

import argparse
import csv
import json
import re
import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TITLES = {"T01": "The Empty Ledger Endpoint", "T03": "Thread Ceiling", "T04": "Purged Routes",
          "T05": "Capacity Glob Cold Start"}
# Entity-fixed identity: the three Claude 5.5 models take the first three categorical slots (validated all-pairs in
# both modes); Fable and Astra, T01 only, are neutral ink told apart by shape and direct labels.
MODELS = {
    "haiku": {"name": "Claude Haiku 5.5", "short": "Haiku 5.5", "shape": "triangle", "light": "#1baf7a", "dark": "#199e70"},
    "sonnet": {"name": "Claude Sonnet 5.5", "short": "Sonnet 5.5", "shape": "circle", "light": "#eb6834", "dark": "#d95926"},
    "opus": {"name": "Claude Opus 5.5", "short": "Opus 5.5", "shape": "square", "light": "#2a78d6", "dark": "#3987e5"},
    "fable": {"name": "Claude Fable 5.2", "short": "Fable 5.2", "shape": "diamond", "light": "#0b0b0b", "dark": "#f0efec"},
    "astra": {"name": "GPT-6 Astra", "short": "GPT-6 Astra", "shape": "cross", "light": "#52514e", "dark": "#c3c2b7"},
}
SURFACE = {"light": {"bg": "#fcfcfb", "ink": "#0b0b0b", "ink2": "#52514e", "grid": "#e6e5e1"},
           "dark": {"bg": "#1a1a19", "ink": "#f0efec", "ink2": "#c3c2b7", "grid": "#3a3a37"}}


def task_of(run: str) -> str:
    m = re.match(r"(T\d\d)R", run)
    return m.group(1) if m else "T01"


def model_of(run: str) -> str:
    return next(k for k in MODELS if f"-{k}" in run)


def frontier(models: list[dict]) -> list[str]:
    """The models no other model dominates: none is as cheap or cheaper and scores as well or better, and better in one."""
    out = []
    for m in models:
        if not any(o is not m and o["mean_cost"] <= m["mean_cost"] and o["mean_d12"] >= m["mean_d12"]
                   and (o["mean_cost"] < m["mean_cost"] or o["mean_d12"] > m["mean_d12"]) for o in models):
            out.append(m["key"])
    return sorted(out, key=lambda k: next(m["mean_cost"] for m in models if m["key"] == k))


def build(regrade: Path | None) -> dict:
    labels = json.loads((ROOT / "hub/runs.json").read_text())["runs"]
    costs = json.loads((ROOT / "hub/run_costs.json").read_text())["runs"]
    check = {}
    if regrade:
        check = {r["run"]: r["final"] for r in json.loads(regrade.read_text())["runs"]}
    tasks: dict[str, dict] = {}
    for run, lab in labels.items():
        fin = lab.get("final")
        if not fin:
            continue
        if check and run in check and (abs(check[run]["d12"] - fin["d12"]) > 1e-6 or check[run]["pass"] != fin["pass"]):
            raise SystemExit(f"{run}: hub/runs.json's final D12 {fin['d12']} differs from the regrade's {check[run]['d12']}")
        t = tasks.setdefault(task_of(run), {"points": [], "notes": []})
        cost = (costs.get(run) or {}).get("usd")
        if cost is None:
            t["notes"].append(f"{run} has no known cost and is not plotted")
            continue
        t["points"].append({"run": run, "model": model_of(run), "cost": round(cost, 4), "d12": round(fin["d12"], 6),
                            "pass": bool(fin["pass"]), "dev": bool(lab.get("dev")),
                            "source": costs[run]["source"]})
    out = {"schema": "awsincidentsbench.pareto/v1",
           "about": ("x: the solver's model spend per run in US dollars (Claude Code's total_cost_usd, or the list price "
                     "of a Codex run's token usage; judge calls not counted). y: the final grader's D12. One point per "
                     "run the final grader graded; a model's marker at its mean cost and mean D12; the frontier joins "
                     "the models no other model beats on both."),
           "models": MODELS, "tasks": {}}
    for task in sorted(tasks):
        pts = sorted(tasks[task]["points"], key=lambda p: (p["model"], p["run"]))
        models = []
        for key in MODELS:
            mine = [p for p in pts if p["model"] == key]
            if not mine:
                continue
            models.append({"key": key, "runs": len(mine), "passed": sum(p["pass"] for p in mine),
                           "mean_cost": round(sum(p["cost"] for p in mine) / len(mine), 4),
                           "mean_d12": round(sum(p["d12"] for p in mine) / len(mine), 4),
                           "min_cost": min(p["cost"] for p in mine), "max_cost": max(p["cost"] for p in mine)})
        front = frontier(models)
        for m in models:
            m["frontier"] = m["key"] in front
        lo, hi = min(p["cost"] for p in pts), max(p["cost"] for p in pts)
        out["tasks"][task] = {"title": TITLES.get(task, task), "points": pts, "models": models, "frontier": front,
                              "log_x": hi / lo > 10, "cost_span": round(hi / lo, 1), "notes": tasks[task]["notes"]}
    return out


def write_csv(doc: dict, path: Path) -> None:
    with path.open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["task", "run", "model", "cost_usd", "final_d12", "final_pass", "development_run", "cost_source"])
        for task, t in doc["tasks"].items():
            for p in t["points"]:
                w.writerow([task, p["run"], MODELS[p["model"]]["name"], f"{p['cost']:.4f}", f"{p['d12']:.6g}",
                            str(p["pass"]).lower(), str(p["dev"]).lower(), p["source"]])


def table_md(doc: dict) -> str:
    rows = ["| Task | Model | Runs | Pass@1 | Mean cost | Cost range | Mean D12 | On the frontier |",
            "| --- | --- | --- | --- | --- | --- | --- | --- |"]
    for task, t in doc["tasks"].items():
        for m in t["models"]:
            rows.append(f"| {task} | {MODELS[m['key']]['name']} | {m['runs']} | {m['passed']}/{m['runs']} | "
                        f"${m['mean_cost']:.2f} | ${m['min_cost']:.2f}–${m['max_cost']:.2f} | {m['mean_d12']:.3f} | "
                        f"{'yes' if m['frontier'] else 'no'} |")
    plots = " · ".join(f"[{task}](pareto/pareto-{task.lower()}.png) ([dark](pareto/pareto-{task.lower()}-dark.png))"
                       for task in doc["tasks"])
    notes = [n for t in doc["tasks"].values() for n in t["notes"]]
    return "\n".join(rows + ["", f"Plots: {plots}. Every point: [`pareto/pareto.csv`](pareto/pareto.csv)."]
                     + ([""] + [f"- {n}" for n in notes] if notes else []))


def plot(doc: dict, task: str, mode: str, path: Path) -> None:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.ticker import FuncFormatter, LogLocator, NullFormatter

    t, s = doc["tasks"][task], SURFACE[mode]
    marker = {"triangle": "^", "circle": "o", "square": "s", "diamond": "D", "cross": "P"}
    fig, ax = plt.subplots(figsize=(8, 5), dpi=150)
    fig.patch.set_facecolor(s["bg"])
    ax.set_facecolor(s["bg"])
    for side in ("top", "right"):
        ax.spines[side].set_visible(False)
    for side in ("left", "bottom"):
        ax.spines[side].set_color(s["grid"])
    ax.tick_params(colors=s["ink2"], labelsize=9)
    ax.grid(True, color=s["grid"], linewidth=0.8)
    ax.set_axisbelow(True)
    if t["log_x"]:
        ax.set_xscale("log")
        ax.xaxis.set_major_locator(LogLocator(base=10, subs=(1.0, 2.0, 5.0)))
        ax.xaxis.set_minor_formatter(NullFormatter())
    ax.xaxis.set_major_formatter(FuncFormatter(lambda v, _: f"${v:g}"))
    by = {m["key"]: m for m in t["models"]}
    fr = [by[k] for k in t["frontier"]]
    if fr:
        xs, ys = [], []
        for i, m in enumerate(fr):
            if i:
                xs.append(m["mean_cost"])
                ys.append(fr[i - 1]["mean_d12"])
            xs.append(m["mean_cost"])
            ys.append(m["mean_d12"])
        ax.plot(xs, ys, color=s["ink2"], linewidth=1.5, linestyle=(0, (4, 3)), zorder=2, label="Pareto frontier")
    for m in t["models"]:
        spec = MODELS[m["key"]]
        pts = [p for p in t["points"] if p["model"] == m["key"]]
        ax.scatter([p["cost"] for p in pts], [p["d12"] for p in pts], s=34, marker=marker[spec["shape"]],
                   color=spec[mode], alpha=0.55, linewidths=0, zorder=3)
        ax.scatter([m["mean_cost"]], [m["mean_d12"]], s=150, marker=marker[spec["shape"]], color=spec[mode],
                   edgecolors=s["bg"], linewidths=2, zorder=4, label=f"{spec['short']} mean")
        ax.annotate(f"{spec['short']}\n${m['mean_cost']:.2f} · {m['mean_d12']:.3f}", (m["mean_cost"], m["mean_d12"]),
                    xytext=(9, -4), textcoords="offset points", fontsize=8.5, color=s["ink"], va="top", zorder=5)
    ax.set_ylim(min(0.0, min(p["d12"] for p in t["points"]) - 0.05), 1.08)
    ax.set_xlabel("Cost per run (USD, solver only" + (", log scale)" if t["log_x"] else ")"), color=s["ink2"], fontsize=9.5)
    ax.set_ylabel("Final D12", color=s["ink2"], fontsize=9.5)
    ax.set_title(f"{task} · {t['title']}: cost and score per run", color=s["ink"], fontsize=11, loc="left")
    leg = ax.legend(loc="lower right", fontsize=8, frameon=False, labelcolor=s["ink2"])
    for h in leg.legend_handles:
        h.set_alpha(1)
    fig.text(0.01, 0.01, "Small marks: one run each. Large marks: the model's mean cost and mean D12. "
             "Dashed: the Pareto frontier over the means.", fontsize=7.5, color=s["ink2"])
    fig.tight_layout(rect=(0, 0.03, 1, 1))
    fig.savefig(path, facecolor=s["bg"])
    plt.close(fig)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--regrade", type=Path, help="the regrade record to check hub/runs.json's final grades against")
    ap.add_argument("--out", type=Path, default=ROOT / "hub/pareto")
    ap.add_argument("--readme-assets", type=Path, help="copy T01's light and dark PNGs here for the GitHub README")
    a = ap.parse_args(argv)
    doc = build(a.regrade)
    a.out.mkdir(parents=True, exist_ok=True)
    (a.out / "pareto.json").write_text(json.dumps(doc, indent=1) + "\n")
    write_csv(doc, a.out / "pareto.csv")
    (a.out / "table.md").write_text(table_md(doc) + "\n")
    try:
        import matplotlib  # noqa: F401
        have_mpl = True
    except ImportError:
        have_mpl = False
        print("matplotlib is not installed: no PNGs written")
    for task in doc["tasks"]:
        if have_mpl:
            for mode in ("light", "dark"):
                plot(doc, task, mode, a.out / f"pareto-{task.lower()}{'-dark' if mode == 'dark' else ''}.png")
        t = doc["tasks"][task]
        print(f"{task}: {len(t['points'])} runs, cost span {t['cost_span']}x{' (log x)' if t['log_x'] else ''}, frontier "
              + " → ".join(t["frontier"]) + "; " + "; ".join(
                  f"{m['key']} {m['passed']}/{m['runs']} ${m['mean_cost']:.2f} {m['mean_d12']:.3f}" for m in t["models"]))
    if a.readme_assets and have_mpl:
        a.readme_assets.mkdir(parents=True, exist_ok=True)
        for name in ("pareto-t01.png", "pareto-t01-dark.png"):
            shutil.copy2(a.out / name, a.readme_assets / name)


if __name__ == "__main__":
    main()
