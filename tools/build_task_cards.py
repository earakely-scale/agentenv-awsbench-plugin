"""Write AwsIncidentsBench's task cards from the bundle's own files, so a rerun after a task changes updates them.

    python tools/build_task_cards.py [--out DIR]

For every task (T01, T03, T04, T05), writes ``<DIR>/<task>/README.md`` (the card) and ``<DIR>/<task>/task.json`` (the same, machine-readable)
into the dataset build output (``hub/dataset/tasks`` by default). Every part is read from where it is defined: the
prompt, tools, turn hook and grader from the task json; start, window, horizon, budget and colleagues' decisions from the
aws scenario; who the colleagues are from the workspace scenario; tool descriptions from the two servers' ``@tool``
definitions; the meter's costs from ``meter.py``; every check, gate, cap, judge fact list and the pass rule from the
grader module itself; the incident and the world from the article's tasks chapter and ``docs/<task>.md``; what makes a
failure fair from the article's results chapter. Both files are scrubbed and checked as ``build_hub_data.py`` scrubs runs.
"""
from __future__ import annotations

import argparse
import ast
import importlib.util
import inspect
import json
import re
import subprocess
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
import export_run as X

BUNDLE = ROOT / "src/agentenv_awsbench/bundles/awsbench"
AWS_PKG = BUNDLE / "envs/aws/awsbench_aws"
WS_PKG = BUNDLE / "envs/workspace/awsbench_workspace"
CHAPTERS = ROOT / "hub/space/app/src/content/chapters"
DATASET_CARD = ROOT / "hub/dataset/README.md"
OUT = ROOT / "hub/dataset/tasks"
REPO = "earakely-scale/AwsIncidentsBench"
GITHUB = "https://github.com/earakely-scale/agentenv-awsbench-plugin"
SPACE = f"https://huggingface.co/spaces/{REPO}"
DATASET = f"https://huggingface.co/datasets/{REPO}"
TASKS = {"T01": "t01-draft", "T03": "t03-draft", "T04": "t04-purged-routes-draft", "T05": "t05-capacity-glob-draft"}
SCHEMA = "awsincidentsbench.task/v1"
BUCKETS = {"outcome": "Outcome", "process": "Process", "comms": "Communication"}
# Served by AgentEnv's gateway while its clock is armed, not by either server here.
GATEWAY_TOOLS = {"get_time": {"server": "gateway", "params": ["time_zone"],
                              "description": "The gateway clock's current time, in UTC and, given an IANA time zone, in "
                                             "that zone. Free: it costs no virtual time."}}


def card_url(task: str) -> str:
    return f"{DATASET}/blob/main/tasks/{task}/README.md"


# ---------------------------------------------------------------- sources

def _load(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _git(*args: str) -> str:
    try:
        return subprocess.run(["git", "-C", str(ROOT), *args], capture_output=True, text=True, check=True).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return ""


def _section(text: str, heading: str, level: int = 2) -> str:
    """The body under a markdown heading, up to the next heading of the same or a higher level."""
    marks = "#" * level
    m = re.search(rf"(?m)^{marks} {re.escape(heading)}\s*$", text)
    if not m:
        raise SystemExit(f"no '{marks} {heading}' section")
    rest = text[m.end():]
    end = re.search(rf"(?m)^#{{1,{level}}} ", rest)
    return (rest[:end.start()] if end else rest).strip()


def _paragraphs(text: str) -> list[str]:
    return [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]


def tool_docs() -> dict[str, dict]:
    """Every agent tool's description and parameters, read from the servers' ``@tool`` methods."""
    out = {}
    for server, path in (("aws", AWS_PKG / "server.py"), ("workspace", WS_PKG / "server.py")):
        for node in ast.walk(ast.parse(path.read_text())):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            deco = next((d for d in node.decorator_list if isinstance(d, ast.Call) and getattr(d.func, "id", "") == "tool"),
                        None)
            if deco is None:
                continue
            desc = next((ast.literal_eval(k.value) for k in deco.keywords if k.arg == "description"), None)
            desc = desc or ast.get_docstring(node) or ""
            args = node.args.args + node.args.kwonlyargs
            params = [a.arg for a in args if a.arg not in ("self", "ctx")]
            out[node.name] = {"server": server, "description": " ".join(desc.split()), "params": params}
    return out


def meter_costs() -> dict[str, int]:
    for node in ast.parse((AWS_PKG / "meter.py").read_text()).body:
        if isinstance(node, ast.Assign) and getattr(node.targets[0], "id", "") == "COSTS":
            return ast.literal_eval(node.value)
    raise SystemExit("meter.py defines no COSTS")


def disclaimer() -> str:
    """The card's own not-affiliated line, so every task card carries the same words."""
    for line in re.split(r"(?<=\.)\s+", " ".join(DATASET_CARD.read_text().split())):
        if "not affiliated with or endorsed by Amazon Web Services" in line:
            return line
    raise SystemExit("the dataset card lost its line saying it is not affiliated with or endorsed by Amazon Web Services")


def _run_tasks(text: str) -> set[str]:
    """The tasks of the run ids a text names: ``T03R4-opus-2`` is T03's, a bare ``R11-opus-2`` T01's."""
    return {m.group(1) or "T01" for m in re.finditer(r"\b(T\d\d)?R\d+-(?:opus|sonnet|haiku|fable|astra)", text)}


def fair_failure(task: str) -> list[str]:
    """The article's section, with an example drawn from another task's runs left out."""
    out = []
    for p in _paragraphs(_section((CHAPTERS / "results.mdx").read_text(), "What makes a failure fair", 3)):
        p = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", " ".join(p.split()))
        head, _, example = p.partition(". ")
        if _run_tasks(head) and task not in _run_tasks(head):
            continue
        if _run_tasks(example) and task not in _run_tasks(example):
            p = head + "."
        out.append(p)
    return out


def claim_rules(task: str) -> list[str]:
    """docs/<task>.md's paragraphs that say when a mitigated or resolved claim is false."""
    body = _section((ROOT / "docs" / f"{task}.md").read_text(), "How it is graded")
    return [" ".join(p.split()) for p in _paragraphs(body) if re.match(r"A (?:mitigated )?claim\b", p)]


def chapter_text(task: str) -> dict:
    """The tasks chapter's prose for a task: its paragraphs and its "why it is hard" list, without the viewer, the card
    link or the generated tables."""
    text = (CHAPTERS / "tasks.mdx").read_text()
    m = re.search(rf"(?m)^### {task}, .*$", text)
    if not m:
        raise SystemExit(f"tasks.mdx has no '### {task}, ...' section")
    rest = text[m.end():]
    end = re.search(r"(?m)^#{1,3} ", rest)
    body = rest[:end.start()] if end else rest
    paras, hard = [], []
    for p in _paragraphs(body):
        if p[0] in "<{" or p.startswith(("**Task card", "import ", "export ")) or "replays one recorded run" in p:
            continue
        if p.startswith("- "):
            hard += [" ".join(item.split()) for item in re.split(r"(?m)^- ", p) if item.strip()]
        elif p.rstrip().endswith(":") and "hard" in p.lower():
            continue
        else:
            paras.append(" ".join(p.split()))
    return {"paragraphs": paras, "why_hard": hard}


def doc_bullets(task: str, heading: str) -> list[str]:
    body = _section((ROOT / "docs" / f"{task}.md").read_text(), heading)
    return [" ".join(item.split()) for item in re.split(r"(?m)^- ", body) if item.strip()]


# ---------------------------------------------------------------- the card's parts

def wiki_pages(scenario: str, users: dict[str, dict]) -> list[dict]:
    """The wiki's policy and runbook pages as the agent finds them: each page's latest version, verbatim."""
    wiki = WS_PKG / "scenarios" / scenario
    out = []
    for page in yaml.safe_load((wiki / "wiki.yaml").read_text()).get("pages") or []:
        kind = next((k for k in ("policy", "runbook") if k in (page.get("labels") or [])), None)
        if not kind:
            continue
        last = page["versions"][-1]
        out.append({"id": page["id"], "title": page["title"], "kind": kind, "version": last["version"],
                    "author": (users.get(last["author"]) or {}).get("name", last["author"]),
                    "body": X.scrub((wiki / last["body_ref"]).read_text()).strip()})
    return out


def colleagues(scenario: dict, users: dict[str, dict]) -> list[dict]:
    """The colleagues the world plays, with what each approves or declines when asked (the scenario's decision rules)."""
    out = []
    for uid, spec in (scenario.get("npcs") or {}).items():
        user = users.get(uid, {})
        approves, declines = [], []
        for rule in spec.get("rules") or []:
            decision, change = rule.get("decision"), rule.get("change")
            if not decision or not change:
                continue
            change = change.replace("{desired}", "N")
            bucket = approves if decision == "APPROVED" else declines
            if change not in [c["change"] for c in bucket]:
                bucket.append({"change": change, "scope": (rule.get("scope") or "").replace("{desired}", "N"),
                               "rule": rule.get("id")})
        escalates = (spec.get("page_escalation") or {}).get("to")
        out.append({"id": uid, "name": user.get("name", uid), "title": user.get("title", ""), "team": user.get("team", ""),
                    "external": bool(user.get("external") or spec.get("external")),
                    "awake_at_start": spec.get("awake") is not False,
                    "escalates_to": users.get(escalates, {}).get("name", escalates) if escalates else None,
                    "approves": approves, "declines": declines})
    return out


def grader_rules(task: str, grade_id: str) -> dict:
    """Checks, gates, caps, judges and the pass rule, read from the grader module the task's grade step runs."""
    path = BUNDLE / "artifacts" / grade_id / "verify.py"
    g = _load(path, f"awsbench_{grade_id.replace('-', '_')}_cards")
    depends = getattr(g, "DEPENDS", {})
    judged = set(getattr(g, "JUDGED", ()) or ())
    if not judged and "C11" in g.CHECKS and hasattr(g, "c11_from"):
        judged = {"C11"}
    checks = []
    for cid, (bucket, points, title) in g.CHECKS.items():
        fn = getattr(g, cid.lower(), None)
        doc = " ".join((inspect.getdoc(fn) or "").split()) if fn else ""
        checks.append({"id": cid, "bucket": bucket, "points": points, "rule": title, "core": cid in g.CORE,
                       "mitigation": cid == getattr(g, "MITIGATION", None), "judged": cid in judged, "depends_on": depends.get(cid),
                       "detail": doc})
    gates = []
    for gid, (klass, title) in g.GATES.items():
        fn = getattr(g, gid.lower(), None)
        doc = " ".join((inspect.getdoc(fn) or "").split()) if fn else ""
        gates.append({"id": gid, "class": klass, "cap": g.CAPS[klass], "rule": title, "detail": doc})
    judges = []
    for cid in sorted(judged):
        facts = g.JUDGE_FACTS[cid] if cid in getattr(g, "JUDGED", ()) else g.JUDGE_FACTS
        forbidden = g.JUDGE_FORBIDDEN[cid] if cid in getattr(g, "JUDGED", ()) else g.JUDGE_FORBIDDEN
        if hasattr(g, "JUDGE_RULES"):
            required, least = g.JUDGE_RULES[cid]
        else:
            m = re.search(r"ok = \{([^}]*)\} <= set\(got\) and len\(got\) >= (\d+) and not bad", inspect.getsource(g.c11_from))
            if not m:
                raise SystemExit(f"{grade_id}: can't read C11's pass rule from c11_from")
            required, least = set(re.findall(r'"(F\d+)"', m.group(1))), int(m.group(2))
        terms = {k: v for k, v in (getattr(g, "KEY_TERMS", {}) or {}).items() if k in facts}
        judges.append({"check": cid, "samples": g.JUDGE_SAMPLES, "facts": facts, "forbidden": forbidden,
                       "required": sorted(required, key=lambda k: int(k[1:])), "at_least": least,
                       "key_terms": sorted(terms)})
    totals = {}
    for c in checks:
        totals[c["bucket"]] = totals.get(c["bucket"], 0) + c["points"]
    mitigation = getattr(g, "MITIGATION", None)
    inaction = (f"if {mitigation} fails, the score is capped at {g.INACTION_CAP}" if mitigation else
                INACTION.get(task, f"the score is capped at {g.INACTION_CAP} when the mitigation fails"))
    passes = (f"every core check ({', '.join(g.CORE)}) and {mitigation} pass and no gate fires" if mitigation and
              mitigation not in g.CORE else f"every core check ({', '.join(g.CORE)}) passes and no gate fires")
    return {"grader": f"artifacts/{grade_id}/verify.py", "checks": checks, "gates": gates, "judges": judges,
            "buckets": totals, "core": list(g.CORE), "mitigation": mitigation, "inaction_cap": g.INACTION_CAP,
            "inaction": inaction, "caps": g.CAPS,
            "score": "D12 = min(1, raw, every cap that applies); raw = the points earned over 100", "pass": passes}


# T04's grader has no single mitigation check: C1 scores in tiers, and under half of it caps the score (verify.py).
INACTION = {"T04": "if C1 earns under half its points (more than 23 purged merchants newly broken after 13:12), the "
                   "score is capped at 0.2"}


def build_card(task: str) -> dict:
    name = TASKS[task]
    steps = json.loads((BUNDLE / "tasks" / f"{name}.json").read_text())
    step = {s["id"]: s for s in steps}
    prompt_step = next(s for s in steps if s["type"] == "prompt_agent")
    configure = next(d for d in step["configure"]["directives"] if d["uri"].endswith("awsbench-scenario/v1"))["args"]
    scenario = yaml.safe_load((AWS_PKG / "scenarios" / f"{configure['scenario']}.yaml").read_text())
    ws_world = yaml.safe_load((WS_PKG / "scenarios" / configure["scenario"] / "world.yaml").read_text())
    users = {u["id"]: u for u in ws_world.get("users") or []}
    allowed = next(s["tools"] for s in steps if s["type"] == "modify_env_tool_access" and s["action"] == "enable"
                   and s.get("role") == prompt_step.get("role", "sre-primary"))
    docs = tool_docs()
    tools = [{"name": t, **docs.get(t, GATEWAY_TOOLS.get(t) or {"server": "?", "description": "", "params": []})}
             for t in allowed]
    missing = [t["name"] for t in tools if t["server"] == "?"]
    if missing:
        raise SystemExit(f"{name}: no definition found for tools {missing}")
    hook = (prompt_step.get("post_turn_hooks") or [{}])[0].get("params", {})
    grade_step = next(s for s in steps if s["type"] == "env_outcome_verifier")
    chapter = chapter_text(task)
    page = next((" ".join(p.split()) for p in prompt_step["prompt"].split("\n\n") if re.search(r"\bPG-\d+", p)), "")
    lead = " ".join(prompt_step["prompt"].split("\n\n")[0].split())
    start = step["clock"]["virtual_time"]
    task_paths = [f"src/agentenv_awsbench/bundles/awsbench/tasks/{name}.json",
                  f"src/agentenv_awsbench/bundles/awsbench/envs/aws/awsbench_aws/scenarios/{configure['scenario']}.yaml",
                  f"src/agentenv_awsbench/bundles/awsbench/envs/workspace/awsbench_workspace/scenarios/{configure['scenario']}",
                  f"src/agentenv_awsbench/bundles/awsbench/artifacts/{grade_step['file_artifact_id']}"]
    from datetime import datetime
    when = datetime.fromisoformat(start)
    story = (f"Vantleaf's on-call SRE is paged at {when.strftime('%H:%M')} ET on {when.strftime('%A, %B')} "
             f"{when.day}, {when.year}: \"{page}\"" if page else
             f"Vantleaf's on-call SRE starts a shift at {when.strftime('%H:%M')} ET on {when.strftime('%A, %B')} "
             f"{when.day}, {when.year}: \"{lead}\"")
    return {
        "schema": SCHEMA, "id": task, "title": scenario["title"], "story": story, "task_file": f"tasks/{name}.json",
        "scenario": configure["scenario"], "seed": configure.get("seed"), "npc_mode": configure.get("npc_mode"),
        "disclaimer": disclaimer(),
        "links": {"card": card_url(task), "dataset": DATASET, "space": SPACE, "github": GITHUB,
                  "bundle": f"{GITHUB}/tree/main/src/agentenv_awsbench/bundles/awsbench"},
        "incident": chapter, "world": doc_bullets(task, "The world"),
        "colleagues": colleagues(scenario, users),
        "wiki": wiki_pages(configure["scenario"], users),
        "agent": {"role": prompt_step.get("role", "sre-primary"), "agent_name": prompt_step["agent_name"],
                  "builtin_tools": step["deploy-sre"].get("builtin_tools")},
        "prompt": prompt_step["prompt"], "tools": tools,
        "time": {"start": scenario["start"], "window_end": scenario["window_end"], "horizon": scenario["horizon"],
                 "timezone": scenario["timezone"], "max_calls": scenario["max_calls"], "tick_s": scenario["tick_s"],
                 "costs_s": meter_costs(), "turn_end_max_s": hook.get("turn_end_max_s"),
                 "done": hook.get("done_text") or "DONE", "max_turns": prompt_step.get("max_turns"),
                 "max_conversation_turns": prompt_step.get("max_conversation_turns"),
                 "timeout_s": prompt_step.get("timeout_seconds"), "notes": doc_bullets(task, "Time")},
        "grading": {**grader_rules(task, grade_step["file_artifact_id"]), "claims": claim_rules(task)},
        "fair_failure": fair_failure(task),
        "run": {"task": name, "bundle": "src/agentenv_awsbench/bundles/awsbench"},
        "build": {"commit": _git("rev-parse", "--short=7", "HEAD"),
                  "task_commit": _git("log", "-1", "--format=%h", "--", *task_paths),
                  "date": _git("log", "-1", "--format=%cs", "HEAD")},
    }


# ---------------------------------------------------------------- rendering

def _cell(text) -> str:
    return str(text if text is not None else "").replace("|", "\\|").replace("\n", " ")


def _hm(iso: str) -> str:
    return iso[11:16]


def render(c: dict) -> str:
    t, g = c["time"], c["grading"]
    out = [f"# {c['id']}: {c['title']}", "", f"**{c['story']}**", "",
           f"A task of [AwsIncidentsBench]({c['links']['dataset']}): an on-call agent works a payments outage against the "
           f"real AWS API on AWS's open-source AWS Emulator, in a world with a clock that every call moves, and is graded "
           f"at a fixed horizon. Runs and viewers: the [Space]({c['links']['space']}); code: "
           f"[agentenv-awsbench-plugin]({c['links']['github']}). Generated by `tools/build_task_cards.py` from the "
           f"bundle at build `{c['build']['commit']}`.", "", c["disclaimer"], ""]
    out += ["## At a glance", "", "| | |", "| --- | --- |",
            f"| Task file | `{c['task_file']}` (scenario `{c['scenario']}`, seed `{c['seed']}`, colleagues {c['npc_mode']}) |",
            f"| Agent | `{c['agent']['role']}`, its built-in tools off; only the tools below |",
            f"| Start | {_hm(t['start'])} ET, {t['start'][:10]} |",
            f"| Window closes | {_hm(t['window_end'])} ET |",
            f"| Graded at the horizon | {_hm(t['horizon'])} ET |",
            f"| Budget | {t['max_calls']} tool calls |",
            f"| Ends | at `{t['done']}`, the call budget or the window's end |",
            f"| Score | {g['score']} |",
            f"| Pass | {g['pass']} |",
            f"| Grader | `{g['grader']}` |", ""]
    inc = c["incident"]
    out += ["## The incident", ""]
    for p in inc["paragraphs"]:
        out += [p, ""]
    if inc["why_hard"]:
        out += ["Why it is hard:", ""] + [f"- {h}" for h in inc["why_hard"]] + [""]
    out += ["## The world", ""] + [f"- {w}" for w in c["world"]] + [""]
    out += ["## Colleagues", "",
            "The colleagues the world plays. Each answers on a timeline and when asked; an approval or a decline is "
            "recorded as the colleague's own `APPROVED` or `DECLINED` message, and the grader checks governed changes "
            "against them. `N` is the size the request names; each approval's scope, and the requests that combine "
            "several of them, are in `task.json`.", "",
            "| Person | Role | Approves when asked | Declines |", "| --- | --- | --- | --- |"]
    for p in c["colleagues"]:
        role = p["title"] + (" (answers only once paged or woken by the timeline"
                             + (f"; an unacked page escalates to {p['escalates_to']}" if p["escalates_to"] else "")
                             + ")" if not p["awake_at_start"] else "")
        single = [a for a in p["approves"] if not re.search(r"; status page post$|^PLAT-2291 revert: ", a["change"])]
        appr = "<br>".join(a["change"] for a in single)
        decl = "<br>".join(d["change"] for d in p["declines"])
        out.append(f"| {_cell(p['name'])} | {_cell(role)} | {_cell(appr) or 'none'} | {_cell(decl) or 'none'} |")
    out += ["", "## Policies and runbooks", "",
            "The company's policies and the runbooks on the wiki, each at the version the agent finds, verbatim. The "
            "graded rules live in these pages and the runbooks; the grader's own wording is under Checks.", "",
            "| Page | Title | Kind | Version | Author |", "| --- | --- | --- | --- | --- |"]
    out += [f"| `{w['id']}` | {_cell(w['title'])} | {w['kind']} | v{w['version']} | {_cell(w['author'])} |" for w in c["wiki"]]
    for w in c["wiki"]:
        out += ["", f"<details><summary><code>{w['id']}</code> v{w['version']}: {w['title']}</summary>", "",
                "````markdown", w["body"], "````", "", "</details>"]
    out += ["", "## The prompt", "",
            f"The agent gets this prompt once, verbatim from `{c['task_file']}` (the scrub shows the scenario's fictional "
            "account as the emulator's `000000000000`):", "", "```text", c["prompt"], "```", ""]
    out += ["## Tools", "",
            f"The `{c['agent']['role']}` role is given exactly these {len(c['tools'])} tools (`aws` server: AWS, hosts, "
            "time; `workspace` server: chat, tickets, wiki, code, mail, pager, status page). Descriptions are the "
            "servers' own.", "", "| Tool | Server | Parameters | What it does |", "| --- | --- | --- | --- |"]
    for tool in c["tools"]:
        out.append(f"| `{tool['name']}` | {tool['server']} | {_cell(', '.join(tool['params']))} | "
                   f"{_cell(tool['description'])} |")
    costs = t["costs_s"]
    out += ["", "## Time and budget", ""] + [f"- {n}" for n in t["notes"]] + [
        "", f"The meter's costs (`meter.py`): " + ", ".join(f"{k.replace('_', ' ')} {v} s" for k, v in costs.items())
        + f". The world steps every {t['tick_s']} s of virtual time. The turn hook lets up to {t['turn_end_max_s']} s "
        f"pass between turns; the episode allows at most {t['max_turns']} turns and {t['timeout_s']} s of real time.", ""]
    out += ["## Checks", "",
            f"From `{g['grader']}`: {len(g['checks'])} checks in three buckets ("
            + ", ".join(f"{BUCKETS[b].lower()} {p}" for b, p in g["buckets"].items()) + " points). **Core** checks "
            f"decide a pass; {g['inaction']}.", "",
            "| Check | Bucket | Points | Core | Rule |", "| --- | --- | --- | --- | --- |"]
    for ch in g["checks"]:
        rule = ch["rule"]
        if ch["depends_on"]:
            rule += f"; scores 0 unless {ch['depends_on']} passes"
        if ch["mitigation"]:
            rule += f"; failing it caps the score at {g['inaction_cap']}"
        if ch["judged"]:
            rule += " (LLM judge, below)"
        if ch["detail"]:
            rule += f". {ch['detail']}"
        out.append(f"| {ch['id']} | {BUCKETS[ch['bucket']]} | {ch['points']} | {'core' if ch['core'] else ''} | "
                   f"{_cell(rule)} |")
    for j in g["judges"]:
        req = ", ".join(j["required"])
        out += ["", f"### {j['check']}: the judge's fact list", "",
                f"An LLM judge reads the text against these facts and forbidden claims, {j['samples']} times; an item "
                "counts when most answers credit it with a quote found in the text"
                + (f" that carries its key terms ({', '.join(j['key_terms'])})" if j["key_terms"] else "")
                + f". {j['check']} passes with " + (f"{req} and " if req else "") + f"at least {j['at_least']} facts in "
                "all, and no forbidden claim. Without a model key the judged rows are skipped and the bucket's other "
                "checks carry their points.", ""]
        out += [f"- **{k}** {v}" for k, v in j["facts"].items()]
        out += ["", "Forbidden:", ""] + [f"- **{k}** {v}" for k, v in j["forbidden"].items()]
    out += ["", "## Gates and caps", "",
            "A gate fires on a serious mistake and caps the score, whatever the points.", "",
            "| Gate | Cap | Rule |", "| --- | --- | --- |"]
    for gate in g["gates"]:
        rule = gate["rule"] + (f". {gate['detail']}" if gate["detail"] else "")
        out.append(f"| {gate['id']} | {gate['cap']} | {_cell(rule)} |")
    out += [(f"| {g['mitigation']} fails | {g['inaction_cap']} | the mitigation check: "
             + _cell(next(ch["rule"] for ch in g["checks"] if ch["mitigation"])) + " |") if g["mitigation"] else
            f"| C1 under half | {g['inaction_cap']} | {_cell(g['inaction'])} |", ""] + [f"{c_} (`docs/{c['id']}.md`)" for c_ in g["claims"]] + [
            "", f"**Score.** {g['score']}. **Pass.** A run passes when {g['pass']}.", ""]
    out += ["## What a fair failure looks like", "",
            "From the article's *What makes a failure fair*: every lost check in the recorded runs was traced to its "
            "evidence and labelled this way.", ""]
    for p in c["fair_failure"]:
        out += [" ".join(p.split()), ""]
    run = c["run"]
    out += ["## Run it", "",
            "With [AgentEnv](https://www.agentenvframework.com), Docker and a local OCI registry, from the GitHub "
            "bundle at this card's build:", "", "```bash",
            f"git clone {c['links']['github']}.git",
            "cd agentenv-awsbench-plugin",
            f"git checkout {c['build']['commit']}",
            "pip install -e '.[dev]'",
            "docker run -d -p 5000:5000 public.ecr.aws/docker/library/registry:2",
            "# set the model endpoint for the solver and the judges: [model] base_url and api_key",
            "# in .agentenv/config.toml (agent-env config show prints the file in effect)",
            f"agent-env run {run['bundle']} --task {run['task']} --model anthropic/claude-sonnet-5-5", "```", "",
            "The images build from `envs/aws` and `envs/workspace` on the first run; the aws image downloads AWS "
            "Emulator for the machine's architecture. The run deploys the env, arms the clock, prompts the agent "
            "once, fast-forwards to the horizon and grades.", ""]
    b = c["build"]
    out += ["## Build", "",
            f"Generated at commit `{b['commit']}` ({b['date']}) of [agentenv-awsbench-plugin]({c['links']['github']}); "
            f"the task's own files (its task json, scenario, workspace and grader) last changed in `{b['task_commit']}`. "
            f"The same content, machine-readable: [`task.json`](task.json) (schema `{SCHEMA}`).", ""]
    return "\n".join(out)


def cards() -> dict[str, dict]:
    """Every task's card, scrubbed."""
    return {task: X.scrub_obj(build_card(task)) for task in TASKS}


def write_cards(out: Path = OUT, built: dict[str, dict] | None = None) -> list[Path]:
    written = []
    for task, card in (built or cards()).items():
        folder = out / task
        folder.mkdir(parents=True, exist_ok=True)
        readme = folder / "README.md"
        readme.write_text(X.scrub(render(card)))
        js = folder / "task.json"
        js.write_text(json.dumps(card, ensure_ascii=False, indent=1))
        written += [readme, js]
    check(written)
    return written


def check(paths: list[Path]) -> None:
    """build_hub_data's scrub check, plus the card's own: the not-affiliated line is there."""
    X.check_clean(paths)
    for p in paths:
        text = p.read_text()
        if X.KEY_FRAGMENT in text:
            raise SystemExit(f"key fragment in {p}")
        if p.suffix == ".md" and "not affiliated with or endorsed by Amazon Web Services" not in text:
            raise SystemExit(f"{p} lost the not-affiliated line")


def tasks_table(cards: dict[str, dict]) -> str:
    """The dataset card's Tasks section body."""
    rows = ["| Task | Title | Start | Window | Horizon | Calls | Checks | Gates | Card |",
            "| --- | --- | --- | --- | --- | --- | --- | --- | --- |"]
    for task, c in cards.items():
        t, g = c["time"], c["grading"]
        rows.append(f"| {task} | {c['title']} | {_hm(t['start'])} ET {t['start'][:10]} | {_hm(t['window_end'])} | "
                    f"{_hm(t['horizon'])} | {t['max_calls']} | {len(g['checks'])} | {len(g['gates'])} | "
                    f"[tasks/{task}/README.md](tasks/{task}/README.md) |")
    return "\n".join(rows)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--out", type=Path, default=OUT, help="folder to write <task>/README.md and task.json into")
    a = ap.parse_args(argv)
    for p in write_cards(a.out):
        print(f"wrote {p}")


if __name__ == "__main__":
    main()
