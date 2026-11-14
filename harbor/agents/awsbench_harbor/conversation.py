"""prompt_agent's conversation loop for a task with a post-turn hook (agent_env prompt_agent.py, 0.9.1521):

    message = the prompt
    for turn 1..max_conversation_turns:
        run the turn; a failed turn ends the conversation without the hook
        the hook decides: end, or the next message (its text parts, joined as the A2A server joins them)

Any error once the first turn has started is recorded, never raised: the AgentEnv step runs with fail_task_on_error
false, so the run goes on to the horizon and the grade either way.
"""
import hashlib
import json
import time
import traceback

from harbor.agents.base import BaseAgent

from . import harness_client


class HookConversation(BaseAgent):
    """A Harbor agent whose conversation the task's own post-turn hook drives; subclasses run one turn."""

    async def setup(self, environment):
        self.params = await harness_client.params(environment)

    async def turn(self, environment, number, message):
        """Run one turn; {"response": text, "error": str | None, ...}."""
        raise NotImplementedError

    async def run(self, instruction, environment, context):
        prompt = self.params["prompt"]
        if hashlib.sha256(instruction.encode("utf-8")).hexdigest() != prompt["sha256"]:
            raise RuntimeError("the instruction differs from the AgentEnv task's prompt (an --extra-instruction?); "
                               "this agent runs the task's prompt unchanged")
        turns, end, message, started = [], None, instruction, time.time()
        try:
            for number in range(1, prompt["max_conversation_turns"] + 1):
                result = await self.turn(environment, number, message)
                entry = {"turn": number, "error": result.get("error"),
                         "reply_tail": (result.get("response") or "")[-300:], **result.get("metrics", {})}
                turns.append(entry)
                if result.get("error"):
                    end = "failed_turn"
                    break
                last = number == prompt["max_conversation_turns"]
                answer = await harness_client.post_turn(environment, number, last, result.get("response") or "",
                                                        prompt["hook_timeout_s"])
                entry["decision"] = answer
                if answer is None:
                    raise RuntimeError("the hook returned no decision; T01's hook always decides")
                if "end" in answer or last:
                    end = answer.get("end", "max_conversation_turns")
                    break
                message = "\n".join(p.get("text") or "" for p in answer["message"] if p.get("kind") == "text")
        except Exception as e:
            if not turns:
                raise
            end = "agent_error"
            turns.append({"error": repr(e)[:2000], "traceback": traceback.format_exc()[-4000:]})
        cost = [t.get("cost_usd") for t in turns if t.get("cost_usd") is not None]
        context.cost_usd = sum(cost) if cost else None
        context.metadata = {"turns": len([t for t in turns if "turn" in t]), "end": end,
                            "wall_s": round(time.time() - started, 1)}
        (self.logs_dir / "conversation.json").write_text(json.dumps({"end": end, "turns": turns}, indent=1,
                                                                    ensure_ascii=False))
