"""Diff one run's AgentEnv replay against its Harbor replay: every tool result, every hook decision and record, both
exports and the grade. Raw equality first; every difference is listed with its JSON path, unnormalized.

    python3 compare.py AE_DIR HARBOR_TRIAL_DIR > RUN.json
"""
import glob
import json
import re
import sys
from pathlib import Path


ZONE_ID = re.compile(r'(?:/hostedzone/|route53:zone/|\\?"HostedZoneId\\?": ?\\?"|\\?"ResourceName\\?": ?\\?")(Z[A-Z0-9]{12,16})\b')
ZONE = "<zone:internal.vantleaf.io>"


def zone_ids(*texts):
    """The Route 53 hosted zone ids a side saw: the AWS Emulator draws one per deployment (random by design)."""
    return {m for t in texts for m in ZONE_ID.findall(t or "")}


def norm(text, ids):
    for z in ids:
        text = text.replace(z, ZONE)
    return text


IDS = {"ae": set(), "hb": set()}


def load(path, side=None):
    text = Path(path).read_text(encoding="utf-8")
    return json.loads(norm(text, IDS[side]) if side else text)


def jsonl(path, side=None):
    text = Path(path).read_text(encoding="utf-8")
    text = norm(text, IDS[side]) if side else text
    return [json.loads(line) for line in text.splitlines() if line.strip()]


def deep_diff(a, b, path="", out=None, limit=200):
    out = [] if out is None else out
    if len(out) >= limit:
        return out
    if type(a) is not type(b):
        out.append({"path": path, "ae": a, "hb": b})
    elif isinstance(a, dict):
        for k in sorted(set(a) | set(b), key=str):
            if k not in a or k not in b:
                out.append({"path": f"{path}/{k}", "ae": a.get(k, "<absent>"), "hb": b.get(k, "<absent>")})
            else:
                deep_diff(a[k], b[k], f"{path}/{k}", out, limit)
    elif isinstance(a, list):
        if len(a) != len(b):
            out.append({"path": path, "ae_len": len(a), "hb_len": len(b)})
        for i, (x, y) in enumerate(zip(a, b)):
            deep_diff(x, y, f"{path}[{i}]", out, limit)
    elif a != b:
        out.append({"path": path, "ae": a, "hb": b})
    return out


def parse_text(row):
    text = "".join(b.get("text", "") for b in row.get("content") or [] if b.get("type") == "text")
    try:
        return json.loads(text)
    except Exception:
        return text


def short(v, n=300):
    s = v if isinstance(v, str) else json.dumps(v, ensure_ascii=False)
    return s if len(s) <= n else s[:n] + "..."


def vs_recorded(turn_files, rec, side):
    """Each replayed call's text against the text the recorded agent got; the hook digests against its messages."""
    n = same = 0
    first = []
    for t, path in enumerate(turn_files, 1):
        for row, r in zip(jsonl(path, side), rec["turns"][t - 1] if t <= len(rec["turns"]) else []):
            n += 1
            text = "".join(b.get("text", "") for b in row.get("content") or [] if b.get("type") == "text")
            r = {**r, "recorded": norm(r["recorded"] or "", rec_ids)}
            if bool(row.get("is_error")) == bool(r["is_error"]) and (
                    text == r["recorded"] or not deep_diff(parse_text({"content": [{"type": "text",
                                                                                     "text": r["recorded"]}]}),
                                                           parse_text(row))):
                same += 1
            elif len(first) < 5:
                first.append({"turn": t, "i": row["i"], "tool": row["tool"],
                              "diff": [{k: short(v) for k, v in d.items()} for d in
                                       deep_diff(parse_text({"content": [{"type": "text", "text": r["recorded"] or ""}]}),
                                                 parse_text(row))[:5]]})
    return {"calls": n, "identical": same, "first_diffs": first}


rec_ids = set()


def main(ae, hb, run=None, recorded=None):
    ae, hb = Path(ae), Path(hb)
    rep = {"run": run, "ae": str(ae), "hb": str(hb)}
    # tool results, turn by turn
    calls, same, diffs, order_only = 0, 0, [], []
    ae_turns = sorted(glob.glob(str(ae / "turn-*.jsonl")), key=lambda p: int(p.rsplit("-", 1)[1].split(".")[0]))
    hb_turns = sorted(glob.glob(str(hb / "agent/turn-*.jsonl")), key=lambda p: int(p.rsplit("-", 1)[1].split(".")[0]))
    rep["turns"] = {"ae": len(ae_turns), "hb": len(hb_turns)}
    IDS["ae"] = zone_ids(*(Path(p).read_text() for p in ae_turns + [ae / "aws-export.json"]))
    IDS["hb"] = zone_ids(*(Path(p).read_text() for p in hb_turns + [hb / "artifacts/export/aws-export.json"]))
    rep["zone_ids"] = {k: sorted(v) for k, v in IDS.items()}
    raw_same = 0
    for pa, pb in zip(ae_turns, hb_turns):
        raw_same += sum(x.get("content") == y.get("content") and x.get("is_error") == y.get("is_error")
                        for x, y in zip(jsonl(pa), jsonl(pb)))
    rep["identical_raw"] = raw_same
    for n, (pa, pb) in enumerate(zip(ae_turns, hb_turns), 1):
        ra, rb = jsonl(pa, "ae"), jsonl(pb, "hb")
        if len(ra) != len(rb):
            diffs.append({"turn": n, "count": [len(ra), len(rb)]})
        for x, y in zip(ra, rb):
            calls += 1
            if x["tool"] != y["tool"] or x["args"] != y["args"] or \
                    (x.get("sent_args") is None) != (y.get("sent_args") is None):
                diffs.append({"turn": n, "i": x["i"], "input_mismatch": [x["tool"], y["tool"]]})
                continue
            if x.get("is_error") == y.get("is_error") and x.get("content") == y.get("content") \
                    and "exception" not in x and "exception" not in y:
                same += 1
                continue
            dd = deep_diff(parse_text(x), parse_text(y))
            if not dd and x.get("is_error") == y.get("is_error") and "exception" not in x and "exception" not in y:
                same += 1
                order_only.append({"turn": n, "i": x["i"], "tool": x["tool"]})
                continue
            diffs.append({"turn": n, "i": x["i"], "tool": x["tool"], "args": short(x["args"], 200),
                          "is_error": [x.get("is_error"), y.get("is_error")],
                          "exception": [x.get("exception"), y.get("exception")],
                          "diff": [{k: short(v) for k, v in d.items()} for d in dd[:20]]})
    rep["calls"] = calls
    rep["identical"] = same
    rep["call_diffs"] = diffs
    rep["key_order_only"] = order_only
    # hook decisions and records
    ca, cb = load(ae / "conversation.json"), load(hb / "agent/conversation.json")
    da = [t.get("decision") for t in ca["turns"]]
    hb_turns_detail = cb["detail"] if isinstance(cb.get("turns"), int) else cb["turns"]  # the end record's shape
    db = [t.get("decision") for t in hb_turns_detail if "turn" in t]
    rep["end"] = {"ae": ca.get("end"), "hb": cb.get("end")}
    rep["decisions_equal"] = da == db
    rep["decisions"] = {"ae": da, "hb": db} if da != db else len(da)
    strip = lambda rs: [{k: v for k, v in r.items() if k != "hook"} for r in rs or []]  # noqa: E731
    ha = strip((ca.get("post_turn_hooks") or {}).get("incident"))
    hb_path = hb / "artifacts/export/post_turn_hooks.json"
    hbr = strip(load(hb_path)) if hb_path.exists() else None
    rep["hook_records_equal"] = ha == hbr
    if ha != hbr:
        rep["hook_records"] = {"ae": ha, "hb": hbr}
    # exports
    for name in ("aws-export.json", "workspace-export.json"):
        a, b = load(ae / name, "ae"), load(hb / "artifacts/export" / name, "hb")
        dd = deep_diff(a, b)
        rep[name] = {"equal": not dd, "diffs": [{k: short(v) for k, v in d.items()} for d in dd[:50]],
                     "n_diffs": len(dd)}
    # grade
    ga, gb = load(ae / "grade.json", "ae"), load(hb / "verifier/grade.json", "hb")
    rows = lambda g: {r.get("id"): {k: r.get(k) for k in ("result", "score", "detail", "fired", "skipped", "raw",  # noqa: E731
                                                          "description")} for r in g["results"]}
    rep["score"] = {"ae": ga["score"], "hb": gb["score"]}
    rep["grade_rows_equal"] = rows(ga) == rows(gb)
    rep["grade_results_equal"] = ga["results"] == gb["results"]
    if not rep["grade_results_equal"]:
        rep["grade_diffs"] = [{k: short(v) for k, v in d.items()} for d in deep_diff(ga["results"], gb["results"])]
    rep["reward_json"] = load(hb / "verifier/reward.json")
    rep["d12"] = {r["id"]: r.get("result") for r in ga["results"] if r.get("id") == "D12"}
    rep["gates_fired"] = sorted(r["id"] for r in ga["results"] if str(r.get("id")).startswith("G") and r.get("fired"))
    if recorded and run:
        rec = load(recorded)[run]
        global rec_ids
        rec_ids = zone_ids(*(c["recorded"] for t in rec["turns"] for c in t))
        rep["ae_vs_recorded"] = vs_recorded(ae_turns, rec, "ae")
        rep["hb_vs_recorded"] = vs_recorded(hb_turns, rec, "hb")
        msgs = lambda ds: [" ".join(p.get("text", "") for p in (d or {}).get("message") or []) for d in ds]  # noqa: E731
        rep["digests_vs_recorded"] = {"ae": msgs(da)[:len(rec["messages"]) - 1] == rec["messages"][1:],
                                      "hb": msgs(db)[:len(rec["messages"]) - 1] == rec["messages"][1:]}
        rh = strip(rec["hooks"])
        rep["hook_records_vs_recorded"] = {"ae": ha == rh, "hb": hbr == rh}
    print(json.dumps(rep, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main(*sys.argv[1:5])
