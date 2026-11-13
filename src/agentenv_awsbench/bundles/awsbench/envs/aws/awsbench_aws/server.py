"""The `aws` MCP server: the AWS Emulator behind aws_cli/aws_api, the fleet, the world and the meter that moves time.

Every tool answers one JSON text block carrying `_clock`; a call by a charged role is metered (CONTRACTS.md, "The
meter"). The workspace server meters its own calls through the internal /awsbench/meter routes."""

import asyncio
import base64
import fnmatch
import json
import logging
import os
import socket
import threading
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Annotated, Any

import yaml
from agentenv_protocol import (
    AgentEnvEnvironment,
    DataPart,
    ToolContext,
    add_data,
    environment_card,
    extension,
    get_data,
    reset_data,
    tool,
)
from boto3.dynamodb.types import TypeDeserializer
from pydantic import Field
from starlette.requests import Request
from starlette.responses import JSONResponse

from .actionlog import ActionLog
from .bridge import Bridge, CommandError, Faults, WallTimes, WorldPort, parse_command, split_globals
from .cloudtrail import seed_trail
from .emulator import Emulator
from .errors import AwsError
from .meter import ET, GatewayClock, Inbox, LocalClock, Meter, MeterConfig, parse_time, rfc3339, whole
from .ports import WorkspacePort
from .world.fleet import ONE_COMMAND, SHELL_OPS

log = logging.getLogger("awsbench.aws")

SCENARIOS = Path(__file__).resolve().parent / "scenarios"
CLOCK_URI = "urn:agentenv:clock/v1"
SCENARIO_URI = "urn:scale:awsbench-scenario/v1"
HORIZON_URI = "urn:scale:awsbench-horizon/v1"
HARNESS = "harness"
NO_SCENARIO = ("AwsBench has no scenario loaded: the aws server restarted or was reset and lost the episode's state. "
               "Nothing more can be done in this environment; reply DONE.")
SWEEP_TABLES = ("endpointd-plans",)
FLEET_COMMANDS = ("fleet_exec", "fleet_restart")
_deser = TypeDeserializer()


def _port() -> int:
    return int(os.environ.get("MCP_PORT", "18765"))


def _json(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"), default=_default)


def _default(o: Any) -> Any:
    if isinstance(o, datetime):
        return o.isoformat()
    if isinstance(o, (set, frozenset)):
        return sorted(o)
    if isinstance(o, bytes):
        return base64.b64encode(o).decode()
    return str(o)


def _plain(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: _plain(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_plain(v) for v in value]
    if hasattr(value, "is_integer") and not isinstance(value, float):
        return int(value) if value == value.to_integral_value() else float(value)
    if isinstance(value, (set, frozenset)):
        return sorted(_plain(v) for v in value)
    return value


def untyped(item: dict) -> dict:
    return {k: _plain(_deser.deserialize(v)) for k, v in item.items()}


def _resolves(host: str) -> bool:
    try:
        socket.gethostbyname(host)
        return True
    except OSError:
        return False


def _role(ctx: ToolContext | None) -> str | None:
    return ctx.role if ctx is not None else None


def _ref_lookup(obj: Any, key: str) -> Any:
    if isinstance(obj, dict):
        if key in obj:
            return obj[key]
        for v in obj.values():
            if (found := _ref_lookup(v, key)) is not None:
                return found
    elif isinstance(obj, list):
        for v in obj:
            if (found := _ref_lookup(v, key)) is not None:
                return found
    return None


class SeedRefs:
    """`${zone_id:<zone name>}` and `${ref:<binding>.<Field>}` values in seed calls, bound from earlier responses
    (CreateHostedZone, CreateTable, and any row with `bind`)."""

    def __init__(self) -> None:
        self.bound: dict[str, Any] = {}

    def bind(self, row: dict, out: dict) -> None:
        op, params = row.get("operation"), row.get("params") or {}
        if op == "CreateHostedZone":
            zone = dict(out.get("HostedZone") or {})
            zone["Id"] = str(zone.get("Id", "")).rsplit("/", 1)[-1]
            self.bound[f"route53.zone/{str(params.get('Name', '')).rstrip('.')}"] = zone
        elif op == "CreateTable":
            self.bound[f"dynamodb.table/{params.get('TableName')}"] = out.get("TableDescription") or out
        if row.get("bind"):
            self.bound[str(row["bind"])] = out

    def resolve(self, value: Any) -> Any:
        if isinstance(value, dict):
            return {k: self.resolve(v) for k, v in value.items()}
        if isinstance(value, list):
            return [self.resolve(v) for v in value]
        if isinstance(value, str) and "${" in value:
            return self._substitute(value)
        return value

    def _substitute(self, text: str) -> Any:
        out, rest = "", text
        while "${" in rest:
            head, _, tail = rest.partition("${")
            ref, _, rest = tail.partition("}")
            value = self._one(ref)
            out += head + (str(value) if value is not None else "${" + ref + "}")
        return out + rest

    def _one(self, ref: str) -> Any:
        kind, _, arg = ref.partition(":")
        if kind == "zone_id":
            return (self.bound.get(f"route53.zone/{arg.rstrip('.')}") or {}).get("Id")
        if kind != "ref":
            return None
        for name in sorted(self.bound, key=len, reverse=True):
            if arg.startswith(name + "."):
                return _ref_lookup(self.bound[name], arg[len(name) + 1:])
        return None


def load_scenario_file(name: str, staged: dict[str, dict]) -> dict:
    if name in staged:
        return staged[name]
    path = SCENARIOS / f"{name}.yaml"
    if not path.is_file():
        raise ValueError(f"unknown scenario {name!r}: no {path.name} in the image and none staged by data/add")
    return yaml.safe_load(path.read_text()) or {}


def seed_rows(scenario: dict) -> list[dict]:
    rows = scenario.get("aws_seed") or []
    if isinstance(rows, str):
        path = SCENARIOS / rows
        rows = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
    return list(rows)


def profiles_of(scenario: dict) -> dict[str, list[dict]]:
    out = {}
    for role in scenario.get("roles") or []:
        if isinstance(role, dict) and role.get("aws_profiles"):
            out[role["role"]] = list(role["aws_profiles"])
    return out or {"sre-primary": [{"profile": "vl-prod/SRE-OnCall", "account": "vl-prod",
                                    "permission_set": "SRE-OnCall", "regions": ["us-east-1", "us-west-2"]}]}


def lookup_attributes_of(value: Any) -> list[dict]:
    """CloudTrail LookupAttributes from {"EventName": "X"}, {"AttributeKey": "EventName", "AttributeValue": "X"}, or a
    list of either: models write all three."""
    out = []
    for item in value if isinstance(value, list) else [value] if value else []:
        if not isinstance(item, dict):
            raise TypeError(f"a lookup attribute is an object like {{\"EventName\": \"PutParameter\"}}, not {item!r}")
        if "AttributeKey" in item:
            out.append({"AttributeKey": str(item["AttributeKey"]), "AttributeValue": str(item.get("AttributeValue", ""))})
        else:
            out += [{"AttributeKey": str(k), "AttributeValue": str(v)} for k, v in item.items()]
    if len(out) > 1:
        raise ValueError("CloudTrail looks events up by one attribute at a time; pass one, e.g. "
                         "{\"EventName\": \"ChangeResourceRecordSets\"}")
    return out


def fleet_outcome(result: Any) -> tuple[int | None, str | None]:
    """A fleet call's exit code (the worst host's) and, when it is non-zero, the error the action log records, so a
    failed command is never read as a change."""
    if not isinstance(result, dict):
        return None, None
    hosts = result.get("hosts")
    hosts = [h for h in hosts if isinstance(h, dict)] if isinstance(hosts, list) else []
    code = max((int(h.get("exit_code") or 0) for h in hosts), default=0)
    if result.get("error"):
        return code or 1, str(result["error"])
    if code:
        failed = [str(h.get("host")) for h in hosts if int(h.get("exit_code") or 0)]
        return code, f"exit code {code} on {', '.join(failed)}"
    return 0, None


def unrunnable(tool: str, args: dict) -> dict | None:
    """The answer to a call whose text can't run at all (shell operators, an empty command, an aws command the CLI
    would not parse): refused before the meter, uncharged and uncounted. A command that runs and fails is charged."""
    command = str(args.get("command") or "")
    if tool == "aws_cli":
        try:
            split_globals(parse_command(command))
        except CommandError as e:
            return {"exit_code": 2, "stderr": f"aws: error: {e}\n", "charged": False}
    elif tool == "fleet_exec" and (not command.strip() or SHELL_OPS.search(command)):
        return {"hosts": [], "exit_code": 2, "charged": False,
                "error": ONE_COMMAND if command.strip() else "empty command: name one command to run"}
    return None


def count_hosts(world: Any, selector: str) -> int | None:
    """How many hosts a fleet selector names: the world's own resolver when it has a fleet, else a match over its
    host list."""
    if getattr(world, "fleet", None) is not None:
        return len(world.fleet.select(selector))
    try:
        hosts = world.fleet_list_hosts() if world is not None else []
    except Exception:
        return None
    parts = [p.strip() for p in str(selector).split(",") if p.strip()]
    chosen = set()
    for h in hosts:
        name = str(h.get("host") or h.get("name") or h.get("id") or "")
        tags = {str(h.get(k)) for k in ("fleet", "group", "az", "role") if h.get(k)}
        if any(p in ("*", "all") or fnmatch.fnmatchcase(name, p) or p in tags for p in parts):
            chosen.add(name)
    return len(chosen) if hosts else None


@environment_card(name="aws")
class AwsServer(AgentEnvEnvironment):
    def __init__(self) -> None:
        self.emulator = Emulator()
        self.ready = threading.Event()
        self.boot_error: str | None = None
        self.world_lock = threading.RLock()
        self.staged: dict[str, dict] = {}
        self.clock: Any = None
        self.gateway: str | None = None
        self._clear()

    def _clear(self) -> None:
        self.actionlog = ActionLog()
        self.inbox = Inbox()
        self.world: Any = None
        self.scenario: dict | None = None
        self.scenario_name: str | None = None
        self.seed, self.mode = "0", "eval"
        self.start_state: dict | None = None
        self.warnings: list[str] = []
        now = datetime.now(UTC).replace(microsecond=0)
        self.meter = Meter(MeterConfig(start=now, window_end=now + timedelta(days=1), horizon=now + timedelta(days=1)),
                           clock=self.clock or LocalClock(now), inbox=self.inbox, actionlog=self.actionlog,
                           world_lock=self.world_lock)
        self.aws_port = WorldPort(self.emulator, self.actionlog, now=self._now)
        self.ws_port = WorkspacePort(os.environ.get("AWSBENCH_WORKSPACE_URL") or f"http://workspace:{_port()}",
                                     self.inbox)
        self.bridge = Bridge(self.emulator, self.actionlog, profiles=profiles_of({}), world=lambda: self.world,
                             world_lock=self.world_lock)

    def _now(self) -> datetime:
        """When an AWS call the world makes happens: the world's time, which runs ahead of the meter's while it
        advances."""
        world = self.world
        return max(self.meter.now, whole(world.now)) if world is not None and getattr(world, "now", None) else self.meter.now

    def boot(self) -> None:
        """Start the emulator off the main thread, so the port binds at once."""
        def run() -> None:
            try:
                self.emulator.start(60)
            except Exception as e:
                self.boot_error = str(e)
                log.error("emulator failed to start: %s", e)
            finally:
                self.ready.set()
        threading.Thread(target=run, daemon=True, name="emulator-boot").start()

    async def _ready(self) -> None:
        if not self.ready.is_set():
            await asyncio.to_thread(self.ready.wait, 90)
        if self.boot_error:
            raise RuntimeError(f"the AWS emulator is not running: {self.boot_error}")

    def _w(self, fn, *args, **kwargs):
        with self.world_lock:
            return fn(*args, **kwargs)

    async def _world_call(self, fn, *args, **kwargs):
        return await asyncio.to_thread(self._w, fn, *args, **kwargs)

    def _acting(self, caller: dict, fn, *args):
        """Run a world call made for an agent call, so the AWS writes it makes are logged under that call and their
        faults see the backoff its retries took (`retry_s`)."""
        self.aws_port.acting = caller
        try:
            return fn(*args)
        finally:
            self.aws_port.acting = None

    def create_app(self) -> Any:
        app = super().create_app()
        app.custom_route("/awsbench/meter/begin", methods=["POST"])(self._route_begin)
        app.custom_route("/awsbench/meter/end", methods=["POST"])(self._route_end)
        app.custom_route("/awsbench/meter/status", methods=["GET"])(self._route_status)
        app.custom_route("/awsbench/health", methods=["GET"])(self._route_health)
        return app

    # metering

    def _unconfigured(self, role: str | None) -> dict | None:
        """A charged call with no scenario loaded is refused, uncharged: after a restart the AWS account is empty and
        the clock is the host's, which an agent must not mistake for the incident."""
        if self.scenario is not None or not self.meter.charged(role):
            return None
        return {"error": NO_SCENARIO, "_clock": self.meter.banner(drain=False)}

    async def _metered(self, ctx: ToolContext | None, tool_name: str, args: dict, work, *, hosts: int | None = None,
                       cost_class: str | None = None, logged: bool = False) -> str:
        role = _role(ctx)
        if refused := self._unconfigured(role) or unrunnable(tool_name, args):
            return _json({**refused, "_clock": self.meter.banner(drain=self.meter.charged(role))})
        meter = self.meter
        began = await meter.begin("aws", tool_name, args, role, hosts=hosts, cost_class=cost_class)
        if not began["ok"]:
            return _json({"error": began["refuse"], "_clock": meter.banner(drain=meter.charged(role))})
        call = {"call_no": began["call_no"], "t": meter.now, "t_issue": meter.now, "tool": tool_name, "role": role}
        result: Any = None
        try:
            await self._ready()
            result = await work(call)
        except AwsError as e:
            result = {"error": e.as_dict()}
        except Exception as e:
            log.exception("%s failed", tool_name)
            result = {"error": str(e)}
        finally:
            summary = result.get("error") if isinstance(result, dict) else None
            exit_code = None
            if tool_name in FLEET_COMMANDS:
                exit_code, summary = fleet_outcome(result)
            ended = await meter.end(began["call_no"], tool_name, args,
                                    {"error": summary, "exit_code": exit_code} if summary else None,
                                    {"logged": logged, "error": summary, "exit_code": exit_code},
                                    drain=meter.charged(role))
        out = result if isinstance(result, dict) else {"result": result}
        return _json({**out, "_clock": ended["banner"]})

    def _need_world(self) -> Any:
        if self.world is None:
            raise RuntimeError("no scenario is loaded: the fleet comes with the scenario (urn:scale:awsbench-scenario/v1)")
        return self.world

    # agent tools

    @tool(description="Run one AWS CLI v1 command, e.g. `aws route53 list-hosted-zones` or `aws dynamodb get-item "
                      "--table-name t --key '{...}'`. One command only: no pipes, redirection or chaining. "
                      "`--profile` picks one of your AWS profiles; the region defaults to us-east-1.",
          structured_output=False)
    async def aws_cli(self, command: Annotated[str, Field(description="The full command, starting with `aws`.")],
                      ctx: ToolContext) -> str:
        async def work(call: dict) -> dict:
            out = await asyncio.to_thread(self.bridge.run_cli, command, role=_role(ctx), call=call)
            try:
                out["output"] = json.loads(out["stdout"]) if out["stdout"].strip() else None
                out.pop("stdout")
            except ValueError:
                pass
            return out
        return await self._metered(ctx, "aws_cli", {"command": command}, work, logged=True)

    @tool(description="Call one AWS API operation through botocore: service (e.g. `route53`), operation (e.g. "
                      "`ListResourceRecordSets`) and its parameters as the API names them.", structured_output=False)
    async def aws_api(self, service: str, operation: str, ctx: ToolContext, params: dict | None = None,
                      profile: str | None = None, region: str | None = None) -> str:
        args = {"service": service, "operation": operation, "params": params or {}}

        async def work(call: dict) -> dict:
            return await asyncio.to_thread(self.bridge.call_api, service, operation, params or {}, role=_role(ctx),
                                           profile=profile, region=region, call=call)
        return await self._metered(ctx, "aws_api", args, work, logged=True)

    @tool(description="A broad query. `source=logs` runs the CloudWatch Logs Insights `query` over `log_groups`, e.g. "
                      "query `fields @timestamp, @message | filter @message like /NXDOMAIN/ | limit 20`. Supported "
                      "Logs Insights subset: `fields`, `filter` with `like`/`not like` (/regex/ or \"text\") and "
                      "`=~`, joined by and/or; `sort @timestamp asc|desc` (default newest first); `limit`; and "
                      "`stats count()` by `bin(<n>m)` and/or `@log`. Log group names must exist "
                      "(`aws logs describe-log-groups`). "
                      "`source=cloudtrail` looks up CloudTrail management events, filtered on one attribute: "
                      "`lookup_attributes` {\"EventName\": \"ChangeResourceRecordSets\"} (or {\"AttributeKey\": "
                      "\"Username\", \"AttributeValue\": \"kwame.asante\"}); keys EventName, Username, EventSource, "
                      "ResourceType, ResourceName, EventId, ReadOnly, AccessKeyId; newest first. `start`/`end` are "
                      "RFC 3339; the default window is the last hour for logs, everything until now for cloudtrail.",
          structured_output=False)
    async def aws_query(self, ctx: ToolContext, source: str = "logs", query: str = "",
                        log_groups: list[str] | None = None, start: str | None = None, end: str | None = None,
                        limit: int = 100, lookup_attributes: dict | list[dict] | None = None,
                        region: str | None = None) -> str:
        args = {"source": source, "query": query, "log_groups": log_groups, "start": start, "end": end,
                "limit": limit, "lookup_attributes": lookup_attributes}

        async def work(call: dict) -> dict:
            t_end = parse_time(end) if end else self.meter.now
            t_start = parse_time(start) if start else None if source == "cloudtrail" else t_end - timedelta(hours=1)
            api = self.bridge.call_api
            kw = {"role": _role(ctx), "region": region, "call": call}
            if source == "cloudtrail":
                try:
                    attrs = lookup_attributes_of(lookup_attributes)
                except (TypeError, ValueError) as e:
                    return {"error": str(e)}
                params = {**({"StartTime": t_start} if t_start else {}), "EndTime": t_end, "MaxResults": min(int(limit), 50),
                          **({"LookupAttributes": attrs} if attrs else {})}
                events: list = []
                while len(events) < limit:
                    out = await asyncio.to_thread(api, "cloudtrail", "LookupEvents", params, **kw)
                    if "error" in out:
                        return out
                    events += out.get("Events", [])
                    if not out.get("NextToken"):
                        break
                    params["NextToken"] = out["NextToken"]
                return {"source": "cloudtrail", "events": events[:limit]}
            if source != "logs":
                return {"error": f"unknown source {source!r}: logs or cloudtrail"}
            params = {"logGroupNames": log_groups or [], "queryString": query, "limit": int(limit),
                      "startTime": int(t_start.timestamp()), "endTime": int(t_end.timestamp())}
            started = await asyncio.to_thread(api, "logs", "StartQuery", params, **kw)
            if "error" in started or "queryId" not in started:
                return started
            for _ in range(20):
                out = await asyncio.to_thread(api, "logs", "GetQueryResults", {"queryId": started["queryId"]}, **kw)
                if "error" in out or out.get("status") not in ("Scheduled", "Running"):
                    break
                await asyncio.sleep(0.2)
            if "error" in out:
                return out
            rows = [{f["field"]: f["value"] for f in r} for r in out.get("results", [])]
            return {"source": "logs", "status": out.get("status"), "results": rows,
                    "statistics": out.get("statistics")}
        return await self._metered(ctx, "aws_query", args, work, logged=True)

    @tool(description="Export a DynamoDB table: every item (a scan), as plain JSON.", structured_output=False)
    async def aws_table_export(self, table: str, ctx: ToolContext, limit: int = 1000, region: str | None = None) -> str:
        async def work(call: dict) -> dict:
            items: list = []
            params: dict = {"TableName": table}
            while len(items) < limit:
                out = await asyncio.to_thread(self.bridge.call_api, "dynamodb", "Scan", params, role=_role(ctx),
                                              region=region, call=call)
                if "error" in out:
                    return out
                items += [untyped(i) for i in out.get("Items", [])]
                if not out.get("LastEvaluatedKey"):
                    break
                params["ExclusiveStartKey"] = out["LastEvaluatedKey"]
            return {"table": table, "count": len(items[:limit]), "items": items[:limit]}
        return await self._metered(ctx, "aws_table_export", {"table": table, "limit": limit}, work, logged=True)

    @tool(description="List the hosts of the fleet, optionally of one fleet group or availability zone.",
          structured_output=False)
    async def fleet_list_hosts(self, ctx: ToolContext, fleet: str | None = None, az: str | None = None) -> str:
        async def work(call: dict) -> dict:
            return {"hosts": await self._world_call(self._need_world().fleet_list_hosts, fleet, az)}
        return await self._metered(ctx, "fleet_list_hosts", {"fleet": fleet, "az": az}, work)

    @tool(description="Inspect one host: its state, services, resolver cache and recent log lines.",
          structured_output=False)
    async def fleet_inspect_host(self, host: str, ctx: ToolContext) -> str:
        async def work(call: dict) -> dict:
            return await self._world_call(self._need_world().fleet_inspect_host, host)
        return await self._metered(ctx, "fleet_inspect_host", {"host": host}, work)

    def _fleet_caller(self, ctx: ToolContext, call: dict) -> dict:
        """Who a fleet command acts as: the role, and for the AWS writes it makes (epctl) the principal the role's own
        AWS credentials give, as aws_cli's writes have."""
        role = _role(ctx)
        return {"role": role, "principal": self.bridge.principal(role, self.bridge.profile(role, None)),
                "call_no": call["call_no"], "t": call["t"]}

    @tool(description="Run a shell command on the hosts a selector names (a host, a glob, a fleet group or an AZ; "
                      "comma-separated). Each host answers with its stdout and exit code.", structured_output=False)
    async def fleet_exec(self, selector: str, command: str, ctx: ToolContext) -> str:
        world = self.world
        hosts = await self._world_call(count_hosts, world, selector) if world is not None else None

        async def work(call: dict) -> dict:
            caller = self._fleet_caller(ctx, call)
            world = self._need_world()
            out = await self._world_call(self._acting, caller, world.fleet_exec, selector, command, caller)
            if caller.get("retry_s"):
                await self.meter.advance(self.meter.now + timedelta(seconds=caller["retry_s"]))
            return out
        return await self._metered(ctx, "fleet_exec", {"selector": selector, "command": command}, work, hosts=hosts)

    @tool(description="Restart the hosts a selector names, `batch_size` at a time, as a background job; "
                      "fleet_job_status follows it.", structured_output=False)
    async def fleet_restart(self, selector: str, ctx: ToolContext, batch_size: int = 1) -> str:
        async def work(call: dict) -> dict:
            caller = self._fleet_caller(ctx, call)
            world = self._need_world()
            return await self._world_call(self._acting, caller, world.fleet_restart, selector, batch_size, caller)
        return await self._metered(ctx, "fleet_restart", {"selector": selector, "batch_size": batch_size}, work)

    @tool(description="The state of a fleet job (a restart).", structured_output=False)
    async def fleet_job_status(self, job_id: str, ctx: ToolContext) -> str:
        async def work(call: dict) -> dict:
            return await self._world_call(self._need_world().fleet_job_status, job_id)
        return await self._metered(ctx, "fleet_job_status", {"job_id": job_id}, work)

    @tool(description="Let time pass: `minutes` (at most 30), or `until` a time (HH:MM or HH:MM:SS Eastern, or RFC "
                      "3339). Returns early when a DM, mention, page or email reaches you, or a fleet job finishes. "
                      "Banners and workspace records are in Eastern time, AWS and epctl print UTC, and any tool that "
                      "takes a time takes RFC 3339 with an offset.",
          structured_output=False)
    async def wait(self, ctx: ToolContext, minutes: float | None = None, until: str | None = None) -> str:
        role, meter = _role(ctx), self.meter
        if refused := self._unconfigured(role):
            return _json(refused)
        args = {"minutes": minutes, "until": until}
        began = await meter.begin("aws", "wait", args, role, cost_class="free")
        if not began["ok"]:
            return _json({"error": began["refuse"], "_clock": meter.banner(drain=meter.charged(role))})
        result: dict
        try:
            target = self._wait_target(minutes, until)
            result = await meter.wait(target) if meter.charged(role) else {
                "error": "wait is only for the responder: time is not yours to move"}
        except ValueError as e:
            result = {"error": str(e)}
        finally:
            ended = await meter.end(began["call_no"], "wait", args, None, {"error": None}, drain=False)
        return _json({**result, "_clock": ended["banner"]})

    def _wait_target(self, minutes: float | None, until: str | None) -> datetime:
        now = self.meter.now
        if until:
            text = until.strip()
            if len(text) <= 8 and ":" in text:
                parts = [int(p) for p in text.split(":")] + [0]
                local = now.astimezone(ET)
                return local.replace(hour=parts[0], minute=parts[1], second=parts[2], microsecond=0)
            return parse_time(text)
        if minutes is None:
            raise ValueError("give minutes or until")
        if not minutes > 0:
            raise ValueError("minutes must be positive")
        return now + timedelta(seconds=min(float(minutes) * 60, self.meter.cfg.wait_max_s))

    @tool(description="Harness control of the episode: status, end, turn_end.", hidden_by_default=True,
          structured_output=False)
    async def awsbench_control(self, op: str, ctx: ToolContext, max_s: float = 600, reason: str | None = None) -> str:
        if _role(ctx) not in (HARNESS, None):
            return _json({"error": "awsbench_control is for the harness only", "_clock": self.meter.banner(drain=False)})
        meter = self.meter
        if op == "status":
            out: dict = meter.status()
        elif op == "end":
            meter.end_episode(reason or "done")
            out = meter.status()
        elif op == "turn_end":
            if meter.ended:
                out = {**meter.status(), "inbound": [], "reason": "ended"}
            else:
                target = min(meter.now + timedelta(seconds=float(max_s)), meter.cfg.window_end)
                start = meter.now
                await meter.advance(target, wake=meter.cfg.wake)
                inbound = meter.inbox.drain()
                reason_ = "inbound" if inbound else ("window_end" if meter.now >= meter.cfg.window_end else "max_s")
                out = {**meter.status(), "waited_s": (meter.now - start).total_seconds(), "inbound": inbound,
                       "reason": reason_}
        else:
            out = {"error": f"unknown op {op!r}: status, end or turn_end"}
        return _json({**out, "_clock": meter.banner(drain=False)})

    # extensions

    @extension(CLOCK_URI, op="sync_time", description="Follow the gateway clock: only this server moves it.")
    async def sync_time(self, env_get_time_url: str, timezone: str | None = None) -> dict:
        port = os.environ.get("MCP_PORT", "18765")
        base = (f"http://gateway:{port}" if await asyncio.to_thread(_resolves, "gateway")
                else env_get_time_url.removesuffix("/").removesuffix("/clock/time"))
        self.gateway = base
        self.clock = GatewayClock(base)
        self.meter.clock = self.clock
        gw_now = await self.clock.time()
        if gw_now is not None and gw_now < self.meter.now:
            await self.clock.advance(self.meter.now)
        return {"ok": True, "gateway": base, "now": rfc3339(self.meter.now),
                "gateway_time": rfc3339(gw_now) if gw_now else None}

    @extension(SCENARIO_URI, op="configure", description="Load a scenario: seed AWS, build the world, start the meter.")
    async def configure(self, scenario: str = "t01", seed: str = "0", mode: str = "eval", npc_mode: str = "scripted",
                        charged_roles: list[str] | None = None) -> dict:
        if mode not in ("eval", "train"):
            raise ValueError(f"mode must be eval or train, not {mode!r}")
        seed = os.urandom(8).hex() if seed == "random" else str(seed)
        if seed.startswith("<") and seed.endswith(">"):
            raise ValueError(f"the seed {seed!r} is an unsubstituted placeholder")
        await self._ready()
        data = load_scenario_file(scenario, self.staged)
        data = {**data, "npc_mode": npc_mode}
        cfg = MeterConfig.from_scenario(data, seed=seed, mode=mode, charged_roles=charged_roles)
        if self.clock is not None and (gw_now := await self.clock.time()) is not None and gw_now > cfg.start:
            # The gateway clock moves forward only, so a world started behind it would never agree with it.
            raise ValueError(f"the gateway clock ({rfc3339(gw_now)}) is past the scenario start ({rfc3339(cfg.start)}); "
                             "deploy a fresh env")
        if self.scenario is not None:
            await asyncio.to_thread(self.emulator.restart)
        clock = self.clock
        self._clear()
        self.scenario, self.scenario_name, self.seed, self.mode = data, scenario, seed, mode
        self.meter = Meter(cfg, clock=clock or LocalClock(), inbox=self.inbox, actionlog=self.actionlog,
                           world_lock=self.world_lock)
        self.bridge.profiles = profiles_of(data)
        self.bridge.policies = data.get("iam") or {}
        seeded = await asyncio.to_thread(self._replay_seed, data)
        self.bridge.faults = self.aws_port.faults = Faults(data.get("faults"), seed, flags=self._flags)
        self.bridge.times = self.aws_port.times = WallTimes(parse_time(data["seeded_at"]) if data.get("seeded_at")
                                                            else None)
        self.bridge.times.mark(cfg.start)
        try:
            from .world import World
            self.world = await asyncio.to_thread(self._w, lambda: World(data, seed, self.aws_port, self.ws_port))
            await self._world_call(self.world.start)
            self.meter.world = self.world
        except Exception as e:
            log.exception("the world did not start")
            self.warnings.append(f"world: {type(e).__name__}: {e}")
        if not await asyncio.to_thread(self.ws_port.health):
            self.warnings.append("workspace unreachable: deliveries are dropped and logged")
        loaded = await asyncio.to_thread(self.ws_port.apply, "scenario.load", {"scenario": scenario},
                                         self.meter.cfg.start)
        if not loaded.get("ok", False):
            self.warnings.append(f"workspace scenario.load: {loaded.get('error', loaded)}")
        self.start_state = await asyncio.to_thread(self.sweep)
        if clock is not None:
            gw_now = await clock.time()
            if gw_now is None or gw_now < self.meter.now:
                await clock.advance(self.meter.now)
        return {"ok": True, "scenario": scenario, "seed": seed, "mode": mode, "start": rfc3339(self.meter.cfg.start),
                "window_end": rfc3339(self.meter.cfg.window_end), "horizon": rfc3339(self.meter.cfg.horizon),
                "seed_calls": seeded, "world": self.world is not None, "warnings": self.warnings}

    def _flags(self) -> dict:
        return getattr(self.world, "world_flags", None) or {}

    def _replay_seed(self, data: dict) -> dict:
        refs, done, errors = SeedRefs(), 0, []
        for row in seed_rows(data):
            if row.get("wait") == "dynamodb_table_active":
                self._wait_tables(row.get("table"))
                continue
            if row.get("kind", "call") != "call" or not row.get("service") or not row.get("operation"):
                continue
            params = refs.resolve(row.get("params") or {})
            try:
                out = self.aws_port.call(row["service"], row["operation"], params,
                                         principal=row.get("principal", "world:seed"), world_made=True,
                                         region=row.get("region"), log_call=False)
                refs.bind(row, out)
                if row["operation"] == "CreateTable":
                    self._wait_tables(params.get("TableName"))
                done += 1
            except AwsError as e:
                errors.append(f"seq {row.get('seq')}: {row['service']}:{row['operation']}: {e.code} {e.message}"[:300])
        if errors:
            self.warnings.append(f"{len(errors)} seed calls failed; first: {errors[0]}")
        zones = {k.split("/", 1)[1]: v.get("Id") for k, v in refs.bound.items() if k.startswith("route53.zone/")}
        self.bridge.trail.history = seed_trail(data, seed_rows(data), zones)
        return {"ok": done, "failed": len(errors), "errors": errors[:20]}

    def _wait_tables(self, table: str | None, timeout: float = 30.0) -> None:
        tables = [table] if table else self.aws_port.call(
            "dynamodb", "ListTables", {}, principal="world:seed", world_made=True, log_call=False).get("TableNames", [])
        deadline = time.monotonic() + timeout
        for name in tables:
            while time.monotonic() < deadline:
                try:
                    status = self.aws_port.call("dynamodb", "DescribeTable", {"TableName": name}, principal="world:seed",
                                                world_made=True, log_call=False)["Table"]["TableStatus"]
                except AwsError:
                    status = "MISSING"
                if status == "ACTIVE":
                    break
                time.sleep(0.1)

    @extension(HORIZON_URI, op="run", description="End the agent's episode and run the world to the horizon.")
    async def run(self, until: str | None = None) -> dict:
        meter = self.meter
        meter.end_episode(meter.end_reason or "horizon")
        target = parse_time(until) if until else meter.cfg.horizon
        before = meter.now
        res = await meter.advance(target) if target > meter.now else {"fired": []}
        return {"done": meter.now >= target, "now": rfc3339(meter.now), "from": rfc3339(before),
                "fired": res.get("fired", [])}

    # data plane

    @reset_data
    async def reset(self) -> None:
        await self._ready()
        await asyncio.to_thread(self.emulator.restart)
        self.aws_port.reset()
        self._clear()

    @add_data
    async def add(self, parts: list) -> None:
        """Stage scenario files: a DataPart {"name", "scenario": {...}}, or YAML text or a file named <name>.yaml."""
        for part in parts:
            kind = getattr(part, "kind", None)
            meta = getattr(part, "metadata", None) or {}
            if kind == "data":
                data = part.data
                name = data.get("name") or meta.get("name")
                body = data.get("scenario") if isinstance(data.get("scenario"), dict) else data
                if not name:
                    raise ValueError("a scenario DataPart needs a name")
                self.staged[str(name)] = body
            elif kind == "text":
                name = meta.get("name")
                if not name:
                    raise ValueError("a scenario TextPart needs metadata.name")
                self.staged[str(name)] = yaml.safe_load(part.text) or {}
            elif kind == "file":
                f = part.file
                raw = getattr(f, "bytes", None)
                if raw is None:
                    raise ValueError("scenario files must carry their bytes")
                name = Path(getattr(f, "name", None) or meta.get("name") or "scenario").stem
                self.staged[name] = yaml.safe_load(base64.b64decode(raw)) or {}

    @get_data
    async def get(self) -> list[DataPart]:
        return [DataPart(data=json.loads(_json(await self.export())))]

    async def export(self) -> dict:
        meter = self.meter
        world = await self._world_call(self.world.export) if self.world is not None else {}
        try:
            state = await asyncio.to_thread(self.sweep) if self.ready.is_set() and not self.boot_error else {}
        except Exception as e:
            state = {"error": str(e)}
        return {"episode": {"scenario": self.scenario_name, "seed": self.seed, "mode": self.mode,
                            "start": rfc3339(meter.cfg.start), "window_end": rfc3339(meter.cfg.window_end),
                            "horizon": rfc3339(meter.cfg.horizon), "now": rfc3339(meter.now), "ended": meter.ended,
                            "end_reason": meter.end_reason,
                            "done_at": rfc3339(meter.done_at) if meter.done_at else None,
                            "calls_used": meter.calls_used, "warnings": self.warnings,
                            "workspace_dropped": len(self.ws_port.dropped)},
                "action_log": self.actionlog.export(), "meter": list(meter.rows), "world": world,
                "aws_state": state, "start_state": self.start_state}

    def sweep(self) -> dict:
        """Route 53 records by zone, the endpointd-plans items and every SSM parameter, read without logging."""
        def call(service: str, op: str, params: dict) -> dict:
            return self.aws_port.call(service, op, params, principal="awsbench:sweep", world_made=True, log_call=False)

        route53: dict = {}
        zones = call("route53", "ListHostedZones", {}).get("HostedZones", [])
        for zone in zones:
            records: dict = {}
            params = {"HostedZoneId": zone["Id"].rsplit("/", 1)[-1]}
            while True:
                out = call("route53", "ListResourceRecordSets", params)
                for rr in out.get("ResourceRecordSets", []):
                    if rr.get("Type") in ("SOA", "NS"):
                        continue
                    name = rr["Name"].rstrip(".")
                    for value in rr.get("ResourceRecords", []) or [{"Value": (rr.get("AliasTarget") or {}).get("DNSName")}]:
                        records.setdefault(name, []).append({"SetIdentifier": rr.get("SetIdentifier"),
                                                             "Value": value.get("Value"), "Weight": rr.get("Weight"),
                                                             "TTL": rr.get("TTL"), "Type": rr.get("Type")})
                if not out.get("IsTruncated"):
                    break
                params = {**params, "StartRecordName": out["NextRecordName"], "StartRecordType": out["NextRecordType"],
                          **({"StartRecordIdentifier": out["NextRecordIdentifier"]}
                             if out.get("NextRecordIdentifier") else {})}
            route53[zone["Name"].rstrip(".")] = records
        dynamodb: dict = {}
        tables = call("dynamodb", "ListTables", {}).get("TableNames", [])
        for table in SWEEP_TABLES:
            if table not in tables:
                continue
            items: dict = {}
            params = {"TableName": table}
            while True:
                out = call("dynamodb", "Scan", params)
                for raw in out.get("Items", []):
                    item = untyped(raw)
                    items[str(item.get("pk", len(items)))] = item
                if not out.get("LastEvaluatedKey"):
                    break
                params = {**params, "ExclusiveStartKey": out["LastEvaluatedKey"]}
            dynamodb[table] = items
        ssm: dict = {}
        params = {"Path": "/", "Recursive": True, "WithDecryption": True}
        while True:
            out = call("ssm", "GetParametersByPath", params)
            ssm.update({p["Name"]: p.get("Value") for p in out.get("Parameters", [])})
            if not out.get("NextToken"):
                break
            params = {**params, "NextToken": out["NextToken"]}
        return {"route53": route53, "dynamodb": dynamodb, "ssm": ssm}

    # internal routes (not on the card)

    async def _body(self, request: Request) -> dict:
        try:
            body = await request.json()
            return body if isinstance(body, dict) else {}
        except ValueError:
            return {}

    async def _route_begin(self, request: Request) -> JSONResponse:
        b = await self._body(request)
        role = b.get("role")
        if refused := self._unconfigured(role):
            return JSONResponse(json.loads(_json({"ok": False, "refuse": refused["error"], "banner": refused["_clock"]})))
        out = await self.meter.begin(b.get("server", "workspace"), b.get("tool", ""), b.get("args") or {},
                                     role, hosts=b.get("hosts"), cost_class=b.get("cost_class"))
        if not out.get("ok"):
            out["banner"] = self.meter.banner(drain=self.meter.charged(role))
        return JSONResponse(json.loads(_json(out)))

    async def _route_end(self, request: Request) -> JSONResponse:
        b = await self._body(request)
        out = await self.meter.end(b.get("call_no"), b.get("tool", ""), b.get("args") or {}, b.get("result_summary"),
                                   b.get("resolved") or {}, drain=b.get("call_no") is not None)
        return JSONResponse(json.loads(_json(out)))

    async def _route_status(self, request: Request) -> JSONResponse:
        return JSONResponse(json.loads(_json({**self.meter.status(), "banner": self.meter.banner(drain=False)})))

    async def _route_health(self, request: Request) -> JSONResponse:
        return JSONResponse({"ok": True, "emulator": self.ready.is_set() and not self.boot_error,
                             "boot_error": self.boot_error, "scenario": self.scenario_name,
                             "world": self.world is not None})


def main() -> None:
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))
    server = AwsServer()
    server.boot()
    server.create_app().run(transport="streamable-http")


if __name__ == "__main__":
    main()
