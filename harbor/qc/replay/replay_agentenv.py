"""The AgentEnv side of a replay: on an env that `agent-env run BUNDLE --task e2e-env --keep` deployed (t01-draft's steps
1-6, by the framework), play the script's turns as the deployed agent reaches the env (the gateway's /mcp with
AgentEnv-Role: sre-primary, the registration deploy_agent writes), run the task's post-turn hook after each turn through
the framework's own runner (agent_env.task_step.task_steps.post_turn_hooks.run_post_turn_hooks, the hook loaded from the
bundle's file as PostTurnHooks._load loads its artifact), then the horizon step's call and the grade without the judge.

    AGENTENV_CONTROL_TOKEN=... python replay_agentenv.py SCRIPT.json GATEWAY_URL BUNDLE_DIR OUT_DIR
"""
import asyncio
import copy
import importlib.util
import inspect
import json
import os
import sys
import uuid
from pathlib import Path
from types import SimpleNamespace

from agent_env.env.env import DeployedEnv
from agent_env.task_step.task_steps.post_turn_hooks import PostTurnHook, PostTurnHooks, run_post_turn_hooks
from agent_env.task_step.task_steps.verifiers.scoring import ScoreAggregator, aggregate_score
from agentenv_protocol import client as protocol

sys.path.insert(0, str(Path(__file__).resolve().parent))
import replay  # noqa: E402

HORIZON_URI = "urn:scale:awsbench-horizon/v1"


def load_module(path, name):
    spec = importlib.util.spec_from_file_location(f"{name}_{uuid.uuid4().hex[:8]}", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


async def main(script_path, gw, bundle, out):
    script = json.loads(Path(script_path).read_text())
    bundle, out = Path(bundle), Path(out)
    out.mkdir(parents=True, exist_ok=True)
    task = json.loads((bundle / "tasks/t01-draft.json").read_text())
    step = next(s for s in task if s["id"] == "incident")
    (spec,) = step["post_turn_hooks"]
    hook_file = bundle / "artifacts" / spec["file_artifact_id"] / "hook.py"
    module = load_module(hook_file, f"post_turn_hook_{step['id']}_0")
    assert inspect.iscoroutinefunction(module.post_turn)
    artifact_id = f"@local/replay/{spec['file_artifact_id']}"
    hooks = PostTurnHooks()
    hooks.hooks.append(PostTurnHook(artifact_id, None, spec.get("params") or {}, module.post_turn))
    card = await protocol.get_card(gw, timeout=60)
    token = os.environ.get("AGENTENV_CONTROL_TOKEN") or None
    deployed = DeployedEnv(env_id="awsbench", env_version=1, environment_card_url=f"{gw}/.well-known/agent-env.json",
                           environment_card=card, control_token=token, instance_id="replay")
    context = SimpleNamespace(metadata={}, deployed_envs=[deployed], heartbeat=lambda: None)
    max_turns = step["max_conversation_turns"]
    decisions, end = [], None

    async def no_user():
        raise RuntimeError("T01's hook never asks the user side")

    for number in range(1, max_turns + 1):
        if number > len(script["turns"]):
            end = "script_exhausted"
            break
        await replay.play(script, number, f"{gw}/mcp", out / f"turn-{number}.jsonl", {"AgentEnv-Role": "sre-primary"})
        reply = script["turns"][number - 1]["reply"]
        last = number == max_turns
        answer = await run_post_turn_hooks(hooks, context=context, step_id=step["id"], number=number, last=last,
                                           agent_reply=reply, agent_parts=[{"kind": "text", "text": reply}],
                                           ask_user=no_user, user_triggers=no_user)
        decisions.append({"turn": number, "decision": answer})
        print(f"turn {number}: decision {json.dumps(answer)[:200]}", flush=True)
        if answer is None or "end" in answer or last:
            end = (answer or {}).get("end", "max_conversation_turns")
            break
    (out / "conversation.json").write_text(json.dumps({"end": end, "turns": decisions,
                                                       "post_turn_hooks": context.metadata.get("post_turn_hooks")},
                                                      indent=1, ensure_ascii=False))
    note = None
    try:
        base = f"{gw}/svc/mcp-aws"
        child = await protocol.get_card(base, timeout=900)
        await protocol.invoke_extension(base, child, HORIZON_URI, params={}, timeout=900,
                                        headers=deployed.control_headers)
    except Exception as e:  # the task's horizon step tolerates a failure; fetch() runs it again
        note = repr(e)[:500]
    os.environ.pop("LITELLM_API_KEY", None)  # judge off on both sides: C11 is skipped and its weight redistributed
    grade = load_module(bundle / "artifacts/t01-grade/verify.py", "t01_grade_replay")
    aws, ws = await grade.fetch(gw)
    (out / "aws-export.json").write_text(json.dumps(aws, ensure_ascii=False, indent=1))
    (out / "workspace-export.json").write_text(json.dumps(ws, ensure_ascii=False, indent=1))

    async def fetch(env_url):
        return aws, ws
    grade.fetch = fetch
    results = await grade.verify(f"{gw}/mcp", env_url=gw)
    score = aggregate_score(results, ScoreAggregator.WEIGHTED_AVERAGE)
    (out / "grade.json").write_text(json.dumps({"results": results, "score": score}, ensure_ascii=False, indent=1))
    (out / "done.json").write_text(json.dumps({"horizon_error": note, "end": end, "score": score}))
    print("D12", score, flush=True)


if __name__ == "__main__":
    asyncio.run(main(*sys.argv[1:5]))
