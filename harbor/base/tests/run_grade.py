"""Grade a trial's exports with the AgentEnv task's own grader, as its env_outcome_verifier step does.

verify.py is loaded the way env_outcome_verifier loads it (spec_from_file_location, not registered in sys.modules) and
called the same way (verify(mcp_url, env_url=...)); only its fetch() is swapped for one that returns the exports the
harness collected through the grader's own fetch() after the agent phase. The score is the framework's weighted_average
of its rows (scoring.py, a byte copy), which equals the grader's D12.

Writes <out>/grade.json (the shape of the run's metadata.verifications.grade, plus the judge requests) and
<out>/reward.json (flat numbers: reward = D12, pass, raw, calls_used, each check's score, each gate as 1 when fired).
"""
import argparse
import asyncio
import hashlib
import importlib.util
import inspect
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from scoring import ScoreAggregator, aggregate_score  # noqa: E402

ENV_URL = "http://gateway:18765"


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
        requests.append({"model": kwargs.get("model"), "temperature": kwargs.get("temperature"),
                         "response_format": kwargs.get("response_format"),
                         "prompt_sha256": hashlib.sha256(prompt.encode()).hexdigest()})
        return await original(*args, **kwargs)
    litellm.acompletion = acompletion


async def grade(exports, out):
    aws = json.loads((exports / "aws-export.json").read_text(encoding="utf-8"))
    workspace = json.loads((exports / "workspace-export.json").read_text(encoding="utf-8"))
    module = load_verifier()

    async def fetch(env_url):
        return aws, workspace
    module.fetch = fetch
    requests = []
    record_judge(module, requests)
    keywords = {"env_url": ENV_URL} if "env_url" in inspect.signature(module.verify).parameters else {}
    results = await module.verify(f"{ENV_URL}/mcp", **keywords)
    score = aggregate_score(results, ScoreAggregator.WEIGHTED_AVERAGE)
    out.mkdir(parents=True, exist_ok=True)
    (out / "grade.json").write_text(json.dumps({"results": results, "score": score, "judge_requests": requests},
                                               ensure_ascii=False, indent=1), encoding="utf-8")
    d12 = next(r for r in results if r.get("id") == "D12")
    reward = {"reward": score, "pass": 1 if d12.get("result") else 0, "raw": d12.get("raw") or 0,
              "calls_used": d12.get("calls_used") or 0}
    for r in results:
        rid = str(r.get("id"))
        if rid.startswith("C") and not r.get("skipped"):
            reward[rid] = float(r.get("score") or 0)
        elif rid.startswith("G"):
            reward[rid] = 1 if r.get("fired") else 0
    (out / "reward.json").write_text(json.dumps(reward), encoding="utf-8")
    print(f"D12 {score} pass={bool(d12.get('result'))}: {d12.get('description', '')[:300]}")
    for r in results[1:]:
        print(f"  {r.get('id'):4} {r.get('result')!s:5} {str(r.get('detail'))[:160]}")


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--exports", type=Path, default=Path("/export"))
    parser.add_argument("--out", type=Path, default=Path("/logs/verifier"))
    args = parser.parse_args()
    asyncio.run(grade(args.exports, args.out))


if __name__ == "__main__":
    main()
