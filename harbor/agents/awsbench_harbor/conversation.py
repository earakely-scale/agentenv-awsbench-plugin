"""prompt_agent's conversation loop for a task with a post-turn hook (agent_env prompt_agent.py, 0.9.1521):

    message = the prompt
    for turn 1..max_conversation_turns:
        run the turn; a failed turn ends the conversation without the hook
        the hook decides: end, or the next message (its text parts, joined as the A2A server joins them)

Nothing is raised once the conversation starts: the AgentEnv step runs with fail_task_on_error false, so the run goes on
to the horizon either way. How the conversation ended goes to the harness (`end`), and the verifier refuses to score a
trial that a Harbor-side failure cut short (`agent_error`), whose turns could not reach both MCP servers
(`infra_error`), or whose first turn failed before any tool call, since none of those measured the model.
"""
import hashlib
import json
import time
import traceback

from harbor.agents.base import BaseAgent
from harbor.agents.capabilities import AgentCapabilities

from . import harness_client


class InfraError(RuntimeError):
    """The turn ran without the task's tool surface (an MCP server was not connected)."""


class HookConversation(BaseAgent):
    """A Harbor agent whose conversation the task's own post-turn hook drives; subclasses run one turn. It takes the
    task's MCP server (the front) from the harness, under the server name the gateway drew, as AgentEnv does."""

    capabilities = AgentCapabilities(mcp_servers=True)

    async def setup(self, environment):
        self.params = await harness_client.params(environment)
        await harness_client.begin(environment, self.name())

    async def turn(self, environment, number, message):
        """Run one turn; {"response": text, "error": str | None, "metrics": {...}}; raise InfraError when the turn
        could not reach the task's tools."""
        raise NotImplementedError

    async def run(self, instruction, environment, context):
        prompt = self.params["prompt"]
        turns, end, message, started, failure = [], None, instruction, time.time(), None
        try:
            if hashlib.sha256(instruction.encode("utf-8")).hexdigest() != prompt["sha256"]:
                raise InfraError("the instruction differs from the AgentEnv task's prompt (an --extra-instruction?); "
                                 "this agent runs the task's prompt unchanged")
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
                    raise harness_client.HarnessError("the hook returned no decision; T01's hook always decides")
                if "end" in answer or last:
                    end = answer.get("end", "max_conversation_turns")
                    break
                message = "\n".join(p.get("text") or "" for p in answer["message"] if p.get("kind") == "text")
        except Exception as e:
            end = "infra_error" if isinstance(e, InfraError) else "agent_error"
            failure = {"error": repr(e)[:2000], "traceback": traceback.format_exc()[-4000:]}
            turns.append(failure)
        played = [t for t in turns if "turn" in t]
        record = {"agent": self.name(), "end": end, "turns": len(played),
                  "tool_calls": sum(t.get("tool_call_count") or 0 for t in played),
                  "turn_errors": [t["error"] for t in played if t.get("error")][:5],
                  "error": failure["error"] if failure else None}
        try:
            await harness_client.end(environment, record)
        except Exception as e:  # the verifier then finds no end record and refuses to score
            turns.append({"error": f"could not record the end: {e!r}"[:2000]})
        cost = [t.get("cost_usd") for t in played if t.get("cost_usd") is not None]
        context.cost_usd = sum(cost) if cost else None
        context.metadata = {"turns": len(played), "end": end, "wall_s": round(time.time() - started, 1)}
        (self.logs_dir / "conversation.json").write_text(json.dumps({**record, "detail": turns}, indent=1,
                                                                    ensure_ascii=False))
