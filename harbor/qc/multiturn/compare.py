"""Compare the model requests of one AgentEnv run and one Harbor trial against the recording fake model.

    python harbor/qc/multiturn/compare.py AE_REQUESTS.jsonl HB_REQUESTS.jsonl [HB_TRIAL_DIR] > multiturn.json

A main-loop request is a /messages request that offers tools; a turn starts at each one whose last user message is
text rather than a tool result. For each system: the turns, each turn's session id (metadata.user_id's session_id),
whether turn 2 carries turn 1's messages (the CLI resumed the session) and turn 2's new user message (the hook's
digest). Across the two: every main-loop request's body equal after one normalization, the env's MCP server name, which
the gateway draws at random per deploy (envNNNN) on both systems; metadata (device and session ids, random per run) is
reported apart.
"""
import hashlib
import json
import re
import sys
from pathlib import Path

ENV = re.compile(r"(?<![A-Za-z0-9])env\d{4}(?!\d)")
SESSION = re.compile(r'"session_id":\s*"([0-9a-f-]{36})"')


def norm(value):
    return json.loads(ENV.sub("envNNNN", json.dumps(value, ensure_ascii=False)))


def texts(message):
    content = message.get("content")
    if isinstance(content, str):
        return [content]
    return [b.get("text") for b in content or [] if b.get("type") == "text"]


def is_tool_result(message):
    content = message.get("content")
    return isinstance(content, list) and any(b.get("type") == "tool_result" for b in content)


def main_loop(path):
    out = []
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        entry = json.loads(line)
        body = entry["body"]
        if not entry["route"].endswith("/messages") or not body.get("tools"):
            continue
        out.append(body)
    return out


def turns(requests):
    found, seen = [], set()
    for body in requests:
        messages = body.get("messages") or []
        users = [m for m in messages if m.get("role") == "user"]
        if not users or is_tool_result(users[-1]):
            continue
        key = hashlib.sha256(json.dumps(norm(messages), sort_keys=True).encode()).hexdigest()
        if key in seen:  # a retried request
            continue
        seen.add(key)
        user_id = (body.get("metadata") or {}).get("user_id") or ""
        match = SESSION.search(user_id)
        found.append({"messages": len(messages), "roles": [m.get("role") for m in messages],
                      "session": match.group(1) if match else None, "last_user_texts": texts(users[-1]),
                      "first_user_texts": texts(messages[0]), "body": body})
    return found


def summary(path):
    found = turns(main_loop(path))
    out = {"requests": len(main_loop(path)), "turns": len(found),
           "sessions": [t["session"] for t in found]}
    if len(found) >= 2:
        one, two = found[0]["body"]["messages"], found[1]["body"]["messages"]
        kept = [m for m in one if m.get("role") != "system"]
        out["turn2_resumes_turn1"] = norm([m for m in two if m.get("role") != "system"][:len(kept)]) == norm(kept) \
            and any(m.get("role") == "assistant" for m in two)
        out["turn2_digest"] = found[1]["last_user_texts"]
        out["same_session"] = found[0]["session"] == found[1]["session"] is not None
    return out, found


def main():
    ae_path, hb_path = sys.argv[1], sys.argv[2]
    ae, ae_turns = summary(ae_path)
    hb, hb_turns = summary(hb_path)
    report = {"agentenv": ae, "harbor": hb, "per_turn": []}
    for i, (a, h) in enumerate(zip(ae_turns, hb_turns), 1):
        a_body, h_body = norm(a["body"]), norm(h["body"])
        diff = sorted(k for k in set(a_body) | set(h_body) if k != "metadata" and a_body.get(k) != h_body.get(k))
        report["per_turn"].append({"turn": i, "differing_keys": diff,
                                   "messages_equal": a_body.get("messages") == h_body.get("messages"),
                                   "system_equal": a_body.get("system") == h_body.get("system"),
                                   "tools_equal": a_body.get("tools") == h_body.get("tools"),
                                   "tools": len(h_body.get("tools") or [])})
    if len(sys.argv) > 3:
        trial = Path(sys.argv[3])
        decisions = trial / "artifacts/export/hook-decisions.json"
        if decisions.exists() and len(hb_turns) >= 2:
            first = json.loads(decisions.read_text())[0]["decision"]
            sent = "\n".join(p.get("text") or "" for p in first.get("message") or [] if p.get("kind") == "text")
            report["harbor"]["digest_is_the_hooks_message"] = sent in hb_turns[1]["last_user_texts"]
        conversation = trial / "agent/conversation.json"
        if conversation.exists():
            report["harbor"]["end"] = json.loads(conversation.read_text()).get("end")
    report["verdict"] = {
        "both_two_turns": ae.get("turns") == hb.get("turns") == 2,
        "both_resumed": bool(ae.get("turn2_resumes_turn1")) and bool(hb.get("turn2_resumes_turn1")),
        "both_same_session": bool(ae.get("same_session")) and bool(hb.get("same_session")),
        "digests_equal": ae.get("turn2_digest") == hb.get("turn2_digest"),
        "requests_equal": all(not t["differing_keys"] for t in report["per_turn"]),
    }
    print(json.dumps(report, indent=1, ensure_ascii=False))


if __name__ == "__main__":
    main()
