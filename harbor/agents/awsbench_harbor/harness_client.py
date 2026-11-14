"""Calls into the task's `harness` sidecar (docker compose exec through Harbor's service_exec)."""
import base64
import json

SERVICE = "harness"
CHUNK = 60000  # base64 characters per exec argument, well under Linux's 128 KiB single-argument limit


class HarnessError(RuntimeError):
    pass


async def _exec(environment, command, timeout_sec):
    result = await environment.service_exec(command, service=SERVICE, user="root", timeout_sec=timeout_sec)
    if result.return_code != 0:
        raise HarnessError(f"harness `{command[:80]}` exited {result.return_code}: "
                           f"{(result.stderr or '')[-1500:]} {(result.stdout or '')[-500:]}")
    return result.stdout or ""


def _last_json(text):
    for line in reversed(text.strip().splitlines()):
        line = line.strip()
        if line.startswith("{"):
            return json.loads(line)
    raise HarnessError(f"no JSON in the harness output: {text[-500:]}")


async def params(environment):
    return _last_json(await _exec(environment, "python -m awsbench_harness params", 120))


async def post_turn(environment, number, last, reply, timeout_sec):
    """Run the task's post-turn hook on this turn's reply; the decision: {"end": reason} or {"message": parts}."""
    encoded = base64.b64encode((reply or "").encode("utf-8")).decode("ascii")
    for start in range(0, len(encoded), CHUNK):
        await _exec(environment, f"python -m awsbench_harness reply-chunk {int(number)} {encoded[start:start + CHUNK]}",
                    120)
    out = await _exec(environment, f"python -m awsbench_harness post-turn {int(number)} {1 if last else 0}",
                      timeout_sec)
    return _last_json(out)["decision"]
