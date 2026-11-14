"""ReplaySolution: each turn plays a recorded run's calls for that turn through the env's MCP tools from the solver
container (the front, as AgentEnvClaudeCode's Claude Code reaches it) and answers with that turn's recorded reply, so
the task's post-turn hook (in the harness) decides the conversation as it did in the recorded run.

    PYTHONPATH=harbor/agents:<this dir's parent> harbor run -p <task> -a awsbench_replay:ReplaySolution \\
        --ak script=/abs/path/R12-opus-1.json --ve LITELLM_API_KEY=
"""
import json
import shutil
import tempfile
from pathlib import Path

from awsbench_harbor.conversation import HookConversation

PLAYER = Path(__file__).resolve().parents[2] / "replay.py"


class ReplaySolution(HookConversation):
    def __init__(self, *args, script=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.script_path = Path(script)
        self.script = json.loads(self.script_path.read_text())
        self.uploaded = False

    @staticmethod
    def name():
        return "awsbench-replay"

    def version(self):
        return "1"

    async def turn(self, environment, number, message):
        if number > len(self.script["turns"]):
            return {"response": "", "error": "script_exhausted"}
        if not self.uploaded:
            tmp = Path(tempfile.mkdtemp())
            tmp.chmod(0o755)
            shutil.copy(PLAYER, tmp / "replay.py")
            shutil.copy(self.script_path, tmp / "script.json")
            for f in tmp.iterdir():
                f.chmod(0o644)
            await environment.upload_dir(tmp, "/replay")
            shutil.rmtree(tmp)
            await environment.exec("mkdir -p /tmp/replay-out && chmod 777 /tmp/replay-out", user="root")
            self.uploaded = True
        (self.logs_dir / f"message-{number}.txt").write_text(message)
        done = await environment.exec(
            f"python3 /replay/replay.py /replay/script.json {number} http://mcp:18765/mcp "
            f"/tmp/replay-out/turn-{number}.jsonl", user="agent", timeout_sec=7200)
        (self.logs_dir / f"play-{number}.txt").write_text((done.stdout or "") + (done.stderr or ""))
        await environment.download_file(f"/tmp/replay-out/turn-{number}.jsonl", self.logs_dir / f"turn-{number}.jsonl")
        if done.return_code != 0:
            return {"response": "", "error": f"replay exited {done.return_code}"}
        return {"response": self.script["turns"][number - 1]["reply"], "error": None,
                "metrics": {"calls": len(self.script["turns"][number - 1]["calls"])}}
