"""Grade a trial's exports with the AgentEnv task's own grader, as its env_outcome_verifier step does.

verify.py is loaded the way env_outcome_verifier loads it (spec_from_file_location, not registered in sys.modules) and
called the same way (verify(mcp_url, env_url=...)); only its fetch() is swapped for one that returns the exports the
harness collected through the grader's own fetch() after the agent phase. The score is the framework's weighted_average
of its rows (scoring.py, a byte copy), which equals the grader's D12.

Writes <out>/grade.json (the shape of the run's metadata.verifications.grade, plus the judge's settings and requests and
how the conversation ended) and <out>/reward.json (flat numbers: reward = D12, pass, raw, calls_used, each check's
score, each gate as 1 when fired, judge_on, judge_answered, conversation, turns).

It writes no reward, so Harbor records the trial as an error and leaves it out of the job's mean, when the score would
not measure the model:
- the judge's key is missing and AWSBENCH_JUDGE is not "off" (C11 would be skipped and its weight moved);
- fewer than 2 of the judge's 3 samples answered (C11 would fail for want of a judge);
- a hook-driven conversation began and did not record its end, or ended on a Harbor-side failure (agent_error,
  infra_error) before the task's hook had ended the episode;
- its first turn failed (failed_turn) before the agent made any tool call: that measured the model endpoint.
"""
import argparse
import asyncio
import hashlib
import importlib.util
import inspect
import json
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from scoring import ScoreAggregator, aggregate_score  # noqa: E402

ENV_URL = "http://gateway:18765"
JUDGE_MODEL_DEFAULT = "claude-sonnet-5-5"
HARBOR_FAILURES = ("agent_error", "infra_error")


class Refused(Exception):
    pass


def load_verifier():
    spec = importlib.util.spec_from_file_location("outcome_verifier_grade", HERE / "verify.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def record_judge(module, requests):
    """Wrap litellm.acompletion to keep each judge request's settings and the sha256 of its prompt."""
    litellm = getattr(module, "litellm", None)
    if litellm is None:
        return
    original = litellm.acompletion

    async def acompletion(*args, **kwargs):
        prompt = json.dumps(kwargs.get("messages"), ensure_ascii=False, sort_keys=True)
        entry = {"model": kwargs.get("model"), "temperature": kwargs.get("temperature"),
                 "response_format": kwargs.get("response_format"),
                 "prompt_sha256": hashlib.sha256(prompt.encode()).hexdigest()}
        requests.append(entry)
        try:
            response = await original(*args, **kwargs)
        except BaseException as e:
            entry["error"] = type(e).__name__
            raise
        entry["answered"] = True
        return response
    litellm.acompletion = acompletion


def judge_mode():
    """on (the key reaches the grader) or off (AWSBENCH_JUDGE=off: C11 skipped, as AgentEnv grades without a key)."""
    if os.environ.get("AWSBENCH_JUDGE", "").strip().lower() == "off":
        os.environ.pop("LITELLM_API_KEY", None)
        return "off"
    if not os.environ.get("LITELLM_API_KEY"):
        raise Refused("no LITELLM_API_KEY reached the verifier, so C11 would be skipped and its weight moved; pass the "
                      "key, or set AWSBENCH_JUDGE=off to grade without the judge on purpose")
    os.environ.setdefault("AWSBENCH_JUDGE_MODEL", JUDGE_MODEL_DEFAULT)
    return "on"


def conversation_end(exports, calls_used):
    """How a hook-driven conversation ended, or None for an agent without one (oracle, nop, stock agents)."""
    begin = exports / "agent-begin.json"
    if not begin.exists():
        return None
    path = exports / "agent-end.json"
    if not path.exists():
        raise Refused("the conversation began and recorded no end (a Harbor-side failure)")
    end = json.loads(path.read_text(encoding="utf-8"))
    decisions = json.loads((exports / "hook-decisions.json").read_text()) if (exports / "hook-decisions.json").exists() \
        else []
    ended = any("end" in (d.get("decision") or {}) for d in decisions)
    if end.get("end") in HARBOR_FAILURES and not ended:
        raise Refused(f"the conversation ended on a Harbor-side failure ({end.get('end')}: "
                      f"{str(end.get('error'))[:300]}) before the task's hook ended the episode")
    if end.get("end") == "failed_turn" and end.get("turns") == 1 and not end.get("tool_calls") and not calls_used:
        raise Refused(f"the first turn failed before any tool call ({str(end.get('turn_errors'))[:300]}): that "
                      "measured the model endpoint, not the model")
    return end


async def grade(exports, out):
    aws = json.loads((exports / "aws-export.json").read_text(encoding="utf-8"))
    workspace = json.loads((exports / "workspace-export.json").read_text(encoding="utf-8"))
    mode = judge_mode()
    module = load_verifier()

    async def fetch(env_url):
        return aws, workspace
    module.fetch = fetch
    requests = []
    record_judge(module, requests)
    keywords = {"env_url": ENV_URL} if "env_url" in inspect.signature(module.verify).parameters else {}
    results = await module.verify(f"{ENV_URL}/mcp", **keywords)
    score = aggregate_score(results, ScoreAggregator.WEIGHTED_AVERAGE)
    d12 = next(r for r in results if r.get("id") == "D12")
    c11 = next((r for r in results if r.get("id") == "C11"), None)
    judged = c11 is not None and not c11.get("skipped") and "judge" in c11
    answered = (c11["judge"].get("answered") or 0) if judged else 0
    judge = {"mode": mode, "model": os.environ.get("AWSBENCH_JUDGE_MODEL") if mode == "on" else None,
             "called": judged, "answered": answered, "samples": len(requests)}
    out.mkdir(parents=True, exist_ok=True)
    record = {"results": results, "score": score, "judge": judge, "judge_requests": requests}
    try:
        if judged and answered < 2:
            raise Refused(f"only {answered} of the judge's samples answered, so C11 would fail for want of a judge")
        end = conversation_end(exports, d12.get("calls_used") or 0)
    except Refused as e:
        (out / "grade.json").write_text(json.dumps({**record, "refused": str(e)}, ensure_ascii=False, indent=1),
                                        encoding="utf-8")
        print(f"NO REWARD: {e}")
        print(f"(the grade it would have been: D12 {score})")
        return 3
    (out / "grade.json").write_text(json.dumps({**record, "agent_end": end}, ensure_ascii=False, indent=1),
                                    encoding="utf-8")
    reward = {"reward": score, "pass": 1 if d12.get("result") else 0, "raw": d12.get("raw") or 0,
              "calls_used": d12.get("calls_used") or 0}
    for r in results:
        rid = str(r.get("id"))
        if rid.startswith("C") and not r.get("skipped"):
            reward[rid] = float(r.get("score") or 0)
        elif rid.startswith("G"):
            reward[rid] = 1 if r.get("fired") else 0
    reward.update({"judge_on": 1 if mode == "on" else 0, "judge_answered": answered,
                   "conversation": 1 if end else 0, "turns": (end or {}).get("turns") or 0})
    (out / "reward.json").write_text(json.dumps(reward), encoding="utf-8")
    print(f"D12 {score} pass={bool(d12.get('result'))}: {d12.get('description', '')[:300]}")
    print(f"judge {mode}" + (f" ({judge['model']}, {answered}/{len(requests)} answered)" if mode == "on" else "")
          + (f"; conversation ended {end.get('end')} after {end.get('turns')} turn(s)" if end else ""))
    for r in results[1:]:
        print(f"  {r.get('id'):4} {r.get('result')!s:5} {str(r.get('detail'))[:160]}")
    return 0


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--exports", type=Path, default=Path("/export"))
    parser.add_argument("--out", type=Path, default=Path("/logs/verifier"))
    args = parser.parse_args()
    try:
        code = asyncio.run(grade(args.exports, args.out))
    except Refused as e:
        print(f"NO REWARD: {e}")
        code = 3
    sys.exit(code)


if __name__ == "__main__":
    main()
