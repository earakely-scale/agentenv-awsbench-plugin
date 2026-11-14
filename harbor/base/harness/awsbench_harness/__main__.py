"""The harness sidecar: what AgentEnv's runner does to the env around the agent, with the same protocol-client calls.

    python -m awsbench_harness serve        setup steps 2-6 of the task (configure, clock, tool access), then idle
    python -m awsbench_harness params       what the host-side agent needs (server name, limits, hook params)
    python -m awsbench_harness reply-chunk N B64     append a base64 chunk of turn N's reply
    python -m awsbench_harness post-turn N LAST      run the task's post-turn hook on turn N's reply; print the decision
    python -m awsbench_harness collect      horizon step, then the grader's own fetch(); exports to /export

Every value comes from setup.json, which tools/export_harbor.py writes from the AgentEnv task file. The control token
and the MCP server name are the gateway's (the `ctl` volume): this container and the gateway hold them, the agent never.
"""
import asyncio
import base64
import copy
import hashlib
import importlib.util
import inspect
import json
import sys
import time
import uuid
from pathlib import Path

import httpx
from agentenv_protocol import client as protocol

HERE = Path(__file__).resolve().parent.parent
SETUP = json.loads((HERE / "setup.json").read_text())
GW = SETUP["gateway_url"]
CTL = Path("/ctl")
EXPORT = Path("/export")
RUN = Path("/run/awsbench")
READY = RUN / "ready"


def log(*parts):
    print(time.strftime("%H:%M:%S"), *parts, file=sys.stderr, flush=True)


def ctl(name):
    return (CTL / name).read_text().strip()


def control_headers():
    return protocol.control_headers(ctl("token"))


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(value, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(path)


def read_json(path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return default


async def child_card(service, timeout):
    base = f"{GW}/svc/mcp-{service}"
    return base, await protocol.get_card(base, timeout=timeout)


async def wait_for_env(timeout=900):
    """The gateway's card and both servers' cards, as the gateway provider's deploy waits for them."""
    deadline, last = time.monotonic() + timeout, None
    while time.monotonic() < deadline:
        try:
            if (CTL / "token").exists() and (CTL / "name").exists():
                card = await protocol.get_card(GW, timeout=10)
                for service in SETUP["services"]:
                    await child_card(service, 10)
                return card
        except Exception as e:  # not up yet
            last = e
        await asyncio.sleep(2)
    raise RuntimeError(f"the env did not come up in {timeout}s: {last!r}")


async def list_tools(role):
    async with httpx.AsyncClient(timeout=60) as http:
        resp = await http.post(f"{GW}/step", json={"action": "list_tools"}, headers={"AgentEnv-Role": role})
        resp.raise_for_status()
        return sorted(t["name"] for t in resp.json()["tools"])


async def setup():
    """Steps 2-6 of the task, in its order, as apply_server_config, sync_env_clock and modify_env_tool_access make
    them; the record is shaped like the run's metadata."""
    card = await wait_for_env()
    headers = control_headers()
    record = {"server_config_changes": [], "clock_configurations": [], "tool_access_changes": []}
    for step in SETUP["setup_steps"]:
        kind, timeout = step["type"], step.get("timeout_seconds", 30)
        log("setup", step["id"], kind)
        if kind == "apply_server_config":
            for d in step["directives"]:
                base, child = await child_card(d["service"], timeout)
                result = await protocol.invoke_extension(base, child, d["uri"], params=d["args"], timeout=timeout,
                                                         headers=headers)
                record["server_config_changes"].append({"step_id": step["id"], "service": d["service"],
                                                        "uri": d["uri"], "args": d["args"], "result": result})
        elif kind == "sync_env_clock":
            uri = SETUP["uris"]["clock"]
            await protocol.invoke_extension(GW, card, uri, {"virtual_time": step["armed_virtual_time"],
                                                            "virtual_seconds_per_real_second":
                                                                step["virtual_seconds_per_real_second"]},
                                            timeout, method="set_time", headers=headers)
            state = await protocol.invoke_extension(GW, card, uri, None, timeout, method="state", headers=headers)
            url = state.get("env_get_time_url")
            if not url:
                raise RuntimeError("gateway did not expose env_get_time_url")
            synced = []
            for service in SETUP["services"]:
                base, child = await child_card(service, timeout)
                method = protocol.find_extension_method(child, uri, "sync_time") or {}
                if "timezone" not in ((method.get("request") or {}).get("properties") or {}):
                    raise RuntimeError(f"{service}'s sync_time takes no timezone")
                result = await protocol.invoke_extension(base, child, uri, params={"env_get_time_url": url,
                                                                                   "timezone": step["timezone"]},
                                                         timeout=timeout, headers=headers)
                synced.append({"service": service, "environment": service, "synced": True, "result": result})
            record["clock_configurations"].append({
                "step_id": step["id"], "virtual_time": step["virtual_time"],
                "virtual_seconds_per_real_second": step["virtual_seconds_per_real_second"],
                "env_get_time_url": url, "synced": synced, "skipped": [], "timezone": step["timezone"],
                "armed_at": step["armed_virtual_time"]})
        elif kind == "modify_env_tool_access":
            uri = SETUP["uris"][step["action"]]
            body = await protocol.invoke_extension(GW, card, uri, {"role": step["role"], "tools": step["tools"]},
                                                   method=step["action"], headers=headers)
            record["tool_access_changes"].append({"step_id": step["id"], "action": step["action"],
                                                  "role": step["role"], "tools": step["tools"], "result": body})
        else:
            raise RuntimeError(f"no harness for setup step type {kind!r}")
    sre, harness = await list_tools(SETUP["role"]), await list_tools(SETUP["hook"]["params"]["role"])
    record["tools"] = {SETUP["role"]: sre, SETUP["hook"]["params"]["role"]: harness}
    expected = sorted(SETUP["sre_tools"])
    if sre != expected:
        raise RuntimeError(f"{SETUP['role']} sees {len(sre)} tools, not the {len(expected)} the task enables: "
                           f"missing {sorted(set(expected) - set(sre))}, extra {sorted(set(sre) - set(expected))}")
    if "awsbench_control" not in harness:
        raise RuntimeError("the harness role cannot see awsbench_control")
    write_json(EXPORT / "setup-record.json", record)


async def serve():
    RUN.mkdir(parents=True, exist_ok=True)
    EXPORT.mkdir(parents=True, exist_ok=True)
    try:
        await setup()
    except Exception as e:
        log("setup failed:", repr(e))
        raise SystemExit(1)
    READY.touch()
    log("ready")
    while True:
        await asyncio.sleep(3600)


def params():
    """What the host-side agent needs; no secrets."""
    print(json.dumps({"server_name": ctl("name"), "mcp_url": SETUP["front_url"], "role": SETUP["role"],
                      "prompt": SETUP["prompt"], "hook": SETUP["hook"]["artifact_id"]}))


# --- the post-turn hook, as agent_env.task_step.task_steps.post_turn_hooks runs it ------------------------------------

class HookEnv:
    """An env as a hook reaches it: tool calls through /step as the hook's, with the role header and control token."""

    def __init__(self, env_id, card, artifact_id):
        self.env_id = env_id
        self._card = card
        self._artifact_id = artifact_id

    async def call_tool(self, tool, args, role):
        if not isinstance(role, str) or not role:
            raise ValueError("call_tool needs the AgentEnv-Role to call as")
        uris = SETUP["uris"]
        op = protocol.find_extension_method(self._card, uris["step"], "step")
        if op is None:
            raise RuntimeError(f"env '{self.env_id}' serves no {uris['step']} step")
        if uris["step_source"] not in (protocol.extension_params(self._card, uris["step"]).get("features") or []):
            raise RuntimeError(f"env '{self.env_id}' can't record a post-turn hook's tool calls as the hook's; "
                               "rebuild its gateway image")
        body = {"action": "call_tool", "tool_name": tool, "arguments": args or {},
                "source": uris["step_source_post_turn_hook"], "hook_artifact_id": self._artifact_id}
        async with httpx.AsyncClient() as client:
            resp = await client.request(op["method"], f"{GW}{op['endpoint']}", json=body,
                                        headers={"AgentEnv-Role": role, **control_headers()},
                                        timeout=SETUP["hook"]["tool_call_timeout_s"])
        if resp.status_code >= 400:
            raise RuntimeError(f"env '{self.env_id}': /step call_tool {tool!r} failed (HTTP {resp.status_code}): "
                               f"{resp.text[:300]}")
        return resp.json()


class Turn:
    def __init__(self, number, last, reply, envs, records):
        self.number = number
        self.last = last
        self.agent_reply = reply
        self.agent_parts = [{"kind": "text", "text": reply}]
        self.seed = {}
        self.envs = envs
        self._records = records

    def record(self, entry):
        if not isinstance(entry, dict) or set(entry) & {"hook", "turn"}:
            raise ValueError("a hook records an object, without the keys ['hook', 'turn'] it is tagged with")
        self._records.append({"hook": SETUP["hook"]["artifact_id"], "turn": self.number, **copy.deepcopy(entry)})

    def records(self):
        return [copy.deepcopy(e) for e in self._records if e.get("hook") == SETUP["hook"]["artifact_id"]]

    def heartbeat(self):
        pass

    async def ask_user(self):
        raise RuntimeError("T01's hook never asks the user side")

    async def user_triggers(self):
        raise RuntimeError("T01's hook never asks the user side")


def load_hook():
    module_name = f"post_turn_hook_{SETUP['hook']['step_id']}_0_{uuid.uuid4().hex[:8]}"
    spec = importlib.util.spec_from_file_location(module_name, HERE / "hook.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    if not inspect.iscoroutinefunction(getattr(module, "post_turn", None)):
        raise ValueError("the post-turn hook must define `async def post_turn(turn, params)`")
    return module


def decision(value):
    """post_turn_hooks._decision, for a text message (T01's hook returns text or an end)."""
    artifact = SETUP["hook"]["artifact_id"]
    if value is None:
        return None
    if not isinstance(value, dict) or not value or set(value) - {"message", "end"}:
        raise RuntimeError(f"post-turn hook '{artifact}' returned {value!r}")
    if "end" in value and (not isinstance(value["end"], str) or not value["end"]):
        raise RuntimeError(f"post-turn hook '{artifact}' ended with {value['end']!r}; the reason is a string")
    answer = {"end": value["end"]} if "end" in value else {}
    if "message" in value:
        message = value["message"]
        if not isinstance(message, str):
            raise RuntimeError(f"post-turn hook '{artifact}': T01's hook messages are text")
        parts = [{"kind": "text", "text": message}]
        if parts or "end" not in answer:
            answer["message"] = parts
    return answer


async def post_turn(number, last):
    reply_path = RUN / f"reply-{number}.b64"
    reply = base64.b64decode(reply_path.read_bytes()).decode("utf-8") if reply_path.exists() else ""
    records_path = EXPORT / "post_turn_hooks.json"
    records = read_json(records_path, [])
    card = await protocol.get_card(GW, timeout=60)
    envs = [HookEnv(SETUP["hook"]["env_id"], card, SETUP["hook"]["artifact_id"])]
    turn = Turn(number, last, reply, envs, records)
    returned = await load_hook().post_turn(turn, copy.deepcopy(SETUP["hook"]["params"]))
    answer = decision(returned)
    write_json(records_path, records)
    log(f"turn {number}: reply sha256 {hashlib.sha256(reply.encode()).hexdigest()[:12]}, decision "
        f"{json.dumps(answer)[:200]}")
    print(json.dumps({"decision": answer}))


# --- after the agent phase ------------------------------------------------------------------------------------------

async def collect():
    """The horizon step (apply_server_config, failure tolerated), then the grader's own fetch() and the exports."""
    headers = control_headers()
    horizon = SETUP["horizon"]
    note = None
    try:
        for d in horizon["directives"]:
            base, child = await child_card(d["service"], horizon["timeout_seconds"])
            await protocol.invoke_extension(base, child, d["uri"], params=d["args"],
                                            timeout=horizon["timeout_seconds"], headers=headers)
    except Exception as e:  # fail_task_on_error false: the grade's own fetch() runs the horizon again
        note = repr(e)[:500]
        log("horizon step failed:", note)
    spec = importlib.util.spec_from_file_location("t01_grade_fetch", HERE / "verify.py")
    grade = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(grade)
    aws, workspace = await grade.fetch(GW)
    write_json(EXPORT / "aws-export.json", aws)
    write_json(EXPORT / "workspace-export.json", workspace)
    async with httpx.AsyncClient(timeout=120) as http:
        for name, path in (("gateway-state.json", "/state"), ("clock-state.json", "/clock/state")):
            try:
                resp = await http.get(f"{GW}{path}", headers=headers)
                write_json(EXPORT / name, resp.json() if resp.status_code < 400 else {"status": resp.status_code})
            except Exception as e:
                write_json(EXPORT / name, {"error": repr(e)})
        try:
            resp = await http.get(f"{GW}/trajectory", headers=headers)
            (EXPORT / "gateway-trajectory.jsonl").write_text(resp.text, encoding="utf-8")
        except Exception as e:
            write_json(EXPORT / "gateway-trajectory.error.json", {"error": repr(e)})
    write_json(EXPORT / "collect.json", {"horizon_step_error": note, "episode": aws.get("episode")})
    log("collected:", json.dumps(aws.get("episode") or {})[:300])


def main(argv):
    cmd = argv[0] if argv else "serve"
    if cmd == "serve":
        asyncio.run(serve())
    elif cmd == "params":
        params()
    elif cmd == "reply-chunk":
        RUN.mkdir(parents=True, exist_ok=True)
        with open(RUN / f"reply-{int(argv[1])}.b64", "ab") as f:
            f.write(argv[2].encode())
    elif cmd == "post-turn":
        asyncio.run(post_turn(int(argv[1]), argv[2] in ("1", "true")))
    elif cmd == "collect":
        asyncio.run(collect())
    else:
        raise SystemExit(f"unknown command {cmd!r}")


if __name__ == "__main__":
    main(sys.argv[1:])
