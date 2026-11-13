"""AwsBench post-turn hook: ends the episode on DONE, out of budget or out of time; otherwise lets up to ten virtual
minutes pass (stopping at the first page, DM, mention, email or finished job) and wakes the agent with what arrived.

The aws server owns the clock; the hook only calls its harness tool `awsbench_control` through the gateway's /step as
the `harness` role, so nothing it does is charged. It never returns an empty message (that would end the run as
`nothing_to_send`) and never None (that would hand the turn to the default user simulator).
"""
import json
import logging
import re
from datetime import datetime
from zoneinfo import ZoneInfo

PARAMS = ("env_id", "role", "done_regex", "turn_end_max_s", "chunk_s", "max_chunks")
DONE = r"\bDONE\b[.!]?\s*$"
MARKDOWN = re.compile(r"^[\s>#*_`~-]+|[\s*_`~]+$")
ET = ZoneInfo("America/New_York")
WAKE_KINDS = ("dm", "mention", "page", "email", "job_done")

logger = logging.getLogger(__name__)


async def post_turn(turn, params):
    unknown = sorted(set(params) - set(PARAMS))
    if unknown:
        raise ValueError(f"awsbench-turn takes {list(PARAMS)}, not {unknown}")
    env = find_env(turn.envs, params.get("env_id", "awsbench"))
    if env is None:
        return _end(turn, "no_awsbench_env", envs=[e.env_id for e in turn.envs])
    role = params.get("role", "harness")
    try:
        if said_done(turn.agent_reply, params.get("done_regex", DONE)):
            await control(env, role, "end", reason="done")
            return _end(turn, "done")
        status = await control(env, role, "status")
        stop = out_of_time(status)
        if stop:
            await control(env, role, "end", reason=stop)
            return _end(turn, stop)
        if turn.last:
            await control(env, role, "end", reason="max_conversation_turns")
            return _end(turn, "max_conversation_turns")
        return {"message": await wait_turn(turn, env, role, params)}
    except Exception as e:
        logger.exception("awsbench-turn failed")
        try:
            await control(env, role, "end", reason="hook_error")
        except Exception:
            logger.warning("awsbench-turn could not end the episode after its error", exc_info=True)
        return _end(turn, "hook_error", error=repr(e)[:500])


def find_env(envs, want):
    """The env the task names: a bundle run deploys `awsbench` as `@local/.../awsbench`, so the id's last path segment
    counts too, and a run with one deployed env means that one."""
    envs = list(envs)
    for match in (lambda e: e.env_id == want, lambda e: str(e.env_id).rstrip("/").rsplit("/", 1)[-1] == want):
        found = [e for e in envs if match(e)]
        if found:
            return found[0]
    return envs[0] if len(envs) == 1 else None


def said_done(reply, pattern=DONE):
    """The agent's last non-empty line, markdown stripped, ends in DONE."""
    lines = [MARKDOWN.sub("", line).strip() for line in (reply or "").splitlines()]
    lines = [line for line in lines if line]
    return bool(lines) and re.search(pattern, lines[-1]) is not None


def out_of_time(status):
    """Why the episode can't go on: ended, budget spent, or the clock at the window end; None while it can."""
    if status.get("ended"):
        return str(status.get("end_reason") or "ended")
    if status.get("calls_left") is not None and int(status["calls_left"]) <= 0:
        return "budget_exhausted"
    now, end = _at(status.get("now")), _at(status.get("window_end"))
    if now and end and now >= end:
        return "window_end"
    return None


async def control(env, role, op, **args):
    """One awsbench_control call; its JSON result (the tool's single text block)."""
    body = await env.call_tool("awsbench_control", {"op": op, **args}, role)
    text = "".join(block.get("text", "") for block in body.get("content") or [] if isinstance(block, dict))
    if body.get("isError"):
        raise RuntimeError(f"awsbench_control {op}: {text[:300]}")
    try:
        result = json.loads(text) if text else {}
    except json.JSONDecodeError:
        result = {"text": text}
    return result if isinstance(result, dict) else {"result": result}


async def wait_turn(turn, env, role, params):
    """Advance in chunks until something arrives or the turn's wait is used up; the digest to wake the agent with."""
    budget = float(params.get("turn_end_max_s", 600))
    chunk = float(params.get("chunk_s", 600))
    waited, items, result = 0.0, [], {}
    for _ in range(int(params.get("max_chunks", 20))):
        result = await control(env, role, "turn_end", max_s=min(chunk, budget - waited))
        items += list(result.get("inbound") or result.get("items") or [])
        waited += float(result.get("waited_s") or chunk)
        turn.heartbeat()
        if items or result.get("ended") or result.get("reason") == "window_end" or waited >= budget - 1e-6:
            break
    turn.record({"waited_s": waited, "items": len(items), "now": result.get("now")})
    return digest(items, result.get("now"), waited, ended=result.get("ended") or result.get("reason") == "window_end")


def digest(items, now, waited_s, ended=False):
    """What arrived, one line each ("[03:02 ET] #inc-4452 hannah.becker: ..."), or that time passed quietly."""
    lines = []
    for item in items:
        where = item.get("where") or item.get("kind") or ""
        lines.append(f"[{_hhmm(item.get('t'))} ET] {where} {item.get('from') or 'someone'}: "
                     f"{item.get('preview') or ''}".replace("  ", " ").rstrip())
    if lines:
        return "\n".join(lines)
    minutes = round(waited_s / 60)
    tail = " The incident window has closed; wrap up and reply DONE." if ended else ""
    return f"[{_hhmm(now)} ET] {minutes} minute{'s' if minutes != 1 else ''} passed; nothing new.{tail}"


def _end(turn, reason, **detail):
    turn.record({"end": reason, **detail})
    return {"end": reason}


def _at(value):
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=ET)


def _hhmm(value):
    parsed = _at(value)
    if parsed is not None:
        return parsed.astimezone(ET).strftime("%H:%M")
    match = re.match(r"(\d{1,2}:\d{2})", str(value or ""))
    return match.group(1) if match else "--:--"
