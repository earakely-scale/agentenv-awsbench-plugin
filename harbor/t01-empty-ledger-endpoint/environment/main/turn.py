"""One conversation turn, run as the AgentEnv claude-code agent runs it: the image's own claude_bridge.run_claude()
with the arguments its A2A server passes (a2a_server.py `_turn`), in /app, with the agent container's environment.

    python3 /opt/awsbench/turn.py SPEC.json      # writes <out>.result.json and <out>.stream.jsonl

SPEC holds prompt_file, env_file, session_id, model, mcp_servers, timeout_s, task_id, role, max_turns, effort,
max_thinking_tokens, builtin_tools, out (a path prefix). The prompt and the results go through files, so no text rides a
command line; env_file (0600, the model endpoint and key) is read and deleted before anything else runs.
"""
import asyncio
import json
import os
import sys
from pathlib import Path

AGENT = Path("/opt/agent")
ENV_NAMES = Path(__file__).with_name("agentenv-env.json")


def agent_environment(environ):
    """The variables the AgentEnv agent container's CLI process has: the image's own, the ones its sandbox sets, and
    the model endpoint's (deploy_agent hands it LITELLM_API_KEY and LITELLM_BASE_URL). Harbor's exec adds others."""
    names = json.loads(ENV_NAMES.read_text())
    env = {k: v for k, v in environ.items() if k in names["keep"]}
    env.update(names["set"])
    return env


def take_env_file(path):
    if not path:
        return {}
    path = Path(path)
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    finally:
        path.unlink(missing_ok=True)


async def main(spec_path):
    spec = json.loads(Path(spec_path).read_text())
    kept = agent_environment({**os.environ, **take_env_file(spec.get("env_file"))})
    os.environ.clear()
    os.environ.update(kept)
    os.chdir("/app")
    sys.path.insert(0, str(AGENT))
    from claude_bridge import ClaudeCodeTools, run_claude, session_jsonl_path

    out = Path(spec["out"])
    stream = open(f"{out}.stream.jsonl", "a", encoding="utf-8")

    def on_event(event):
        stream.write(json.dumps(event, ensure_ascii=False) + "\n")
        stream.flush()

    prompt = Path(spec["prompt_file"]).read_text(encoding="utf-8")
    session_id = spec["session_id"]
    result = await run_claude(
        prompt=prompt,
        session_id=session_id,
        resume=session_jsonl_path(session_id).exists(),
        model=spec["model"],
        allowed_tools=None,
        mcp_servers=spec["mcp_servers"],
        system_prompt=None,
        output_format=None,
        timeout_s=spec["timeout_s"],
        project_id=None,
        task_id=spec["task_id"],
        role=spec["role"],
        effort=spec.get("effort"),
        max_turns=spec["max_turns"],
        max_thinking_tokens=spec.get("max_thinking_tokens"),
        builtin_tools=ClaudeCodeTools(**spec["builtin_tools"]) if spec.get("builtin_tools") else None,
        event_callback=on_event,
    )
    stream.close()
    record = {"response": result.response, "error": result.error, "error_detail": result.error_detail,
              "session_id": result.session_id, "tool_call_count": result.tool_call_count, "attempts": result.attempts,
              "session_file": str(session_jsonl_path(result.session_id or session_id))}
    Path(f"{out}.result.json").write_text(json.dumps(record, ensure_ascii=False, indent=1), encoding="utf-8")


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1]))
