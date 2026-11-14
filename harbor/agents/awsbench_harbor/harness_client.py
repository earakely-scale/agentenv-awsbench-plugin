"""Calls into the task's `harness` sidecar (docker compose exec through Harbor's service_exec).

Harbor runs every exec with stderr folded into stdout, so no answer is parsed from a command's output: the harness writes
it whole to /run/awsbench/out/<name>.json and a second exec reads that file.
"""
import base64
import json

SERVICE = "harness"
CHUNK = 60000  # base64 characters per exec argument, well under Linux's 128 KiB single-argument limit
OUT = "/run/awsbench/out"


class HarnessError(RuntimeError):
    pass


async def _exec(environment, command, timeout_sec):
    result = await environment.service_exec(command, service=SERVICE, user="root", timeout_sec=timeout_sec)
    if result.return_code != 0:
        raise HarnessError(f"harness `{command[:80]}` exited {result.return_code}: "
                           f"{(result.stderr or '')[-1500:]} {(result.stdout or '')[-500:]}")
    return result.stdout or ""


async def _answer(environment, name):
    text = await _exec(environment, f"cat {OUT}/{name}.json", 60)
    try:
        return json.loads(text)
    except json.JSONDecodeError as e:
        raise HarnessError(f"the harness answer {name} is not JSON: {text[-500:]}") from e


async def params(environment):
    await _exec(environment, f"rm -f {OUT}/params.json && python -m awsbench_harness params", 120)
    return await _answer(environment, "params")


async def begin(environment, agent):
    await _exec(environment, f"python -m awsbench_harness begin {agent}", 60)


async def end(environment, record):
    encoded = base64.b64encode(json.dumps(record, ensure_ascii=False).encode("utf-8")).decode("ascii")
    await _exec(environment, f"python -m awsbench_harness end {encoded}", 60)


async def post_turn(environment, number, last, reply, timeout_sec):
    """Run the task's post-turn hook on this turn's reply; the decision: {"end": reason} or {"message": parts}."""
    number = int(number)
    encoded = base64.b64encode((reply or "").encode("utf-8")).decode("ascii")
    await _exec(environment, f"rm -f /run/awsbench/reply-{number}.b64 {OUT}/decision-{number}.json", 60)
    for start in range(0, len(encoded), CHUNK):
        await _exec(environment, f"python -m awsbench_harness reply-chunk {number} {encoded[start:start + CHUNK]}",
                    120)
    await _exec(environment, f"python -m awsbench_harness post-turn {number} {1 if last else 0}", timeout_sec)
    return (await _answer(environment, f"decision-{number}"))["decision"]
