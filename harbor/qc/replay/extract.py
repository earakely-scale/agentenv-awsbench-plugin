"""Turn a recorded AgentEnv T01 run into a replay script: its turns, each turn's MCP tool calls (exact arguments, in the
order the env served them) and the agent's reply that the post-turn hook read.

    python3 extract.py RUN_DIR... --out DIR

Source: the run's Claude Code session (agent-session.jsonl): every tool_use block with its input, and its tool_result.
A turn starts at each user message that is not a tool result (the prompt, then each hook digest). The reply of a turn is
the text of its last assistant message, which claude_bridge returns as the A2A reply. Parallel tool_use blocks of one
assistant message are put in the order the env charged them (the `_clock.call` number in each recorded result);
calls that carry no number keep their slot. A tool_use naming no MCP tool never reached the env and is left out.
"""
import argparse
import json
import re
from pathlib import Path

CALL = re.compile(r'"_clock":\{"now":"[^"]*","call":(\d+)')


def text_of(content):
    if isinstance(content, str):
        return content
    return "".join(b.get("text", "") for b in content or [] if isinstance(b, dict) and b.get("type") == "text")


def extract(run_dir):
    rows = [json.loads(line) for line in (run_dir / "agent-session.jsonl").read_text().splitlines() if line.strip()]
    results = {}
    for r in rows:
        if r.get("type") == "user" and isinstance(r["message"]["content"], list):
            for b in r["message"]["content"]:
                if b.get("type") == "tool_result":
                    results[b["tool_use_id"]] = {"text": text_of(b.get("content")), "is_error": bool(b.get("is_error"))}
    turns, dropped, server = [], [], None
    last_text = {}
    for r in rows:
        if r.get("isSidechain"):
            raise SystemExit(f"{run_dir}: sidechain rows (a subagent?) are not replayable")
        if r.get("type") == "user":
            c = r["message"]["content"]
            if isinstance(c, list) and any(b.get("type") == "tool_result" for b in c):
                continue
            turns.append({"message": text_of(c), "groups": [], "reply_by_msg": {}})
        elif r.get("type") == "assistant" and turns:
            m = r["message"]
            turn = turns[-1]
            for b in m.get("content") or []:
                if b.get("type") == "text":
                    turn["reply_by_msg"].setdefault(m.get("id"), []).append(b["text"])
                    turn["last_msg"] = m.get("id")
                elif b.get("type") == "tool_use":
                    name = b["name"]
                    if not name.startswith("mcp__"):
                        dropped.append({"name": name, "input": b.get("input"), "result": results.get(b["id"])})
                        continue
                    _, srv, tool = name.split("__", 2)
                    server = server or srv
                    rec = results.get(b["id"]) or {}
                    num = CALL.search(rec.get("text") or "")
                    call = {"tool": tool, "args": b.get("input") or {}, "recorded": rec.get("text"),
                            "recorded_is_error": rec.get("is_error"), "call_no": int(num.group(1)) if num else None}
                    if turn["groups"] and turn["groups"][-1]["msg"] == m.get("id"):
                        turn["groups"][-1]["calls"].append(call)
                    else:
                        turn["groups"].append({"msg": m.get("id"), "calls": [call]})
                    turn["last_msg"] = m.get("id")
    out_turns, reordered = [], 0
    for t in turns:
        calls = []
        for g in t["groups"]:
            numbered = [c for c in g["calls"] if c["call_no"] is not None]
            ordered = sorted(numbered, key=lambda c: c["call_no"])
            reordered += ordered != numbered
            it = iter(ordered)
            calls += [next(it) if c["call_no"] is not None else c for c in g["calls"]]
        reply = "".join(t["reply_by_msg"].get(t.get("last_msg"), []))
        out_turns.append({"message": t["message"], "calls": calls, "reply": reply})
    ctx = json.loads((run_dir / "instance-context.json").read_text())
    pr = ctx["prompt_responses"][0]
    final = pr.get("response") or ""
    hooks = ((ctx.get("metadata") or {}).get("post_turn_hooks") or {}).get("incident") or []
    return {"run": run_dir.name, "build": (run_dir / "BUILD").read_text().split()[1] if (run_dir / "BUILD").exists()
            else None, "server_name": server, "turns": out_turns, "dropped_tool_uses": dropped,
            "reordered_groups": reordered, "recorded_final_reply": final,
            "recorded_final_reply_matches": out_turns[-1]["reply"] == final if out_turns else None,
            "recorded_hook_records": hooks,
            "recorded_messages": [t["message"] for t in out_turns]}


def main():
    p = argparse.ArgumentParser()
    p.add_argument("runs", nargs="+")
    p.add_argument("--out", required=True)
    a = p.parse_args()
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    for run in a.runs:
        s = extract(Path(run))
        (out / f"{s['run']}.json").write_text(json.dumps(s, indent=1, ensure_ascii=False))
        print(s["run"], "turns", len(s["turns"]), "calls", sum(len(t["calls"]) for t in s["turns"]),
              "dropped", [d["name"] for d in s["dropped_tool_uses"]], "reordered groups", s["reordered_groups"],
              "final reply matches", s["recorded_final_reply_matches"], "hook records", len(s["recorded_hook_records"]))


if __name__ == "__main__":
    main()
