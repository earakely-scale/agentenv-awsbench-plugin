"""ScriptedSolution: turn 1 plays a scripted T01 path (the task's solution/play.py MODE) through the env's MCP tools from
the solver container and replies DONE, so the task's post-turn hook ends the episode as the driver's finish() does.

    PYTHONPATH=harbor/agents harbor run -p harbor/t01-empty-ledger-endpoint \\
        -a awsbench_harbor.scripted:ScriptedSolution --ak mode=trap
"""
import json
from pathlib import Path

from .conversation import HookConversation

TASK = Path(__file__).resolve().parents[2] / "t01-empty-ledger-endpoint"


class ScriptedSolution(HookConversation):
    def __init__(self, *args, mode="oracle", solution_dir=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.mode = mode
        self.solution_dir = Path(solution_dir) if solution_dir else TASK / "solution"

    @staticmethod
    def name():
        return "awsbench-scripted"

    def version(self):
        return "1"

    async def turn(self, environment, number, message):
        if number > 1:
            return {"response": "DONE", "error": None}
        await environment.upload_dir(self.solution_dir, "/solution")
        done = await environment.exec(f"python3 /solution/play.py {self.mode}", user="agent", timeout_sec=3600)
        (self.logs_dir / "play.txt").write_text((done.stdout or "") + (done.stderr or ""))
        (self.logs_dir / "play.json").write_text(json.dumps({"mode": self.mode, "exit_code": done.return_code}))
        return {"response": "DONE", "error": None, "metrics": {"mode": self.mode, "play_exit": done.return_code}}
