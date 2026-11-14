"""aws_cli and aws_api: the AWS CLI v1 and botocore in process, against the emulator.

Every API call passes botocore hooks that log it, apply the caller's coarse IAM and the scenario's faults, and let the
world answer the reads it serves (metrics, logs, queue depths) before the emulator sees the call."""

import copy
import fnmatch
import functools
import hashlib
import io
import math
import re
import shlex
import threading
import time
from bisect import bisect_right
from collections.abc import Callable
from contextlib import redirect_stderr, redirect_stdout
from datetime import UTC, datetime, timedelta
from email.utils import format_datetime
from types import SimpleNamespace
from typing import Any

import botocore.session
import urllib3
from awscli.clidriver import create_clidriver
from awscli.paramfile import register_uri_param_handler
from botocore import xform_name
from botocore.exceptions import BotoCoreError, ClientError, ParamValidationError
from botocore.utils import parse_timestamp

from .actionlog import ActionLog
from .cloudtrail import Trail
from .emulator import ACCESS_KEY, SECRET_KEY, Emulator
from .errors import AwsError
from .meter import is_read
from .ports import AwsPort

urllib3.disable_warnings()

OPERATORS = set("|;&<>()")
LOCAL_FILES = ("file://", "fileb://")
NOT_HERE = {"configure", "history"}
DROPPED_FLAGS = {"--debug", "--no-verify-ssl"}
VALUED_GLOBALS = {"--profile", "--region", "--endpoint-url", "--ca-bundle"}
READ_EXTRA = {"StartQuery", "StopQuery"}
DEFAULT_POLICY = {
    "deny": ["iam:*", "organizations:*", "sts:AssumeRole*", "account:*"],
    "allow": [
        {"action": "route53:ChangeResourceRecordSets"},
        {"action": ["dynamodb:PutItem", "dynamodb:UpdateItem", "dynamodb:DeleteItem", "dynamodb:BatchWriteItem"],
         "params": {"TableName": "endpointd-plans"}},
        {"action": ["ssm:PutParameter", "ssm:DeleteParameter"], "params": {"Name": ["/platform/*", "/vl/prod/*"]}},
        {"action": ["sqs:SetQueueAttributes", "sqs:PurgeQueue", "sqs:ChangeMessageVisibility*"]},
        {"action": ["ecs:UpdateService", "ecs:StopTask"]},
        {"action": "application-autoscaling:*"},
        {"action": ["autoscaling:CancelInstanceRefresh", "autoscaling:SuspendProcesses",
                    "autoscaling:ResumeProcesses", "autoscaling:SetDesiredCapacity", "autoscaling:UpdateAutoScalingGroup"]},
        {"action": ["cloudwatch:SetAlarmState", "cloudwatch:DisableAlarmActions", "cloudwatch:EnableAlarmActions"]},
    ],
}
FAULT_MESSAGES = {
    "Throttling": "Rate exceeded",
    "ThrottlingException": "Rate exceeded",
    "PriorRequestNotComplete": "The request was rejected because Route 53 was still processing a prior request.",
    "ServiceUnavailable": "Service is unavailable. Please try again later.",
    "InternalError": "We encountered an internal error. Please try again.",
}
TIME_LONG = re.compile(r"(?i)(time|timestamp)$")


class CommandError(ValueError):
    pass


def parse_command(command: str) -> list[str]:
    """The arguments after `aws`, refusing anything a shell would read as more than one command, and file:// values,
    which the CLI would read from this server's own disk."""
    if "`" in command or "$(" in command:
        raise CommandError("command substitution is not supported: run one aws command")
    lex = shlex.shlex(command, posix=True, punctuation_chars=True)
    lex.whitespace_split = True
    try:
        tokens = list(lex)
    except ValueError as e:
        raise CommandError(f"cannot parse the command: {e}") from None
    if any(tok and set(tok) <= OPERATORS for tok in tokens):
        raise CommandError("pipes, redirection and command chaining are not supported: run one aws command")
    if not tokens or tokens[0] != "aws":
        raise CommandError("the command must start with 'aws'")
    if len(tokens) == 1:
        raise CommandError("name a service and an operation, e.g. 'aws route53 list-hosted-zones'")
    if any(tok.startswith(LOCAL_FILES) or tok.partition("=")[2].startswith(LOCAL_FILES) for tok in tokens):
        raise CommandError("file:// and fileb:// values are not supported: pass the value inline")
    if tokens[1] in NOT_HERE:
        raise CommandError(f"'aws {tokens[1]}' is not available: your profile and region are set for you")
    return tokens[1:]


_SESSION = botocore.session.get_session()


@functools.lru_cache(maxsize=256)
def operation_name(service: str, operation: str) -> str:
    """The API name of an operation given as the API names it or in snake_case (`describe_alarms`), so the meter, the
    action log and the world see one name; unchanged when the service or operation is unknown."""
    try:
        model = _SESSION.get_service_model(service)
    except BotoCoreError:
        return operation
    want = xform_name(operation)
    return next((name for name in model.operation_names if xform_name(name) == want), operation)


def split_globals(argv: list[str]) -> tuple[list[str], str | None, str | None]:
    """The argv without the options this bridge decides itself, and the profile and region asked for."""
    out, profile, region, i = [], None, None, 0
    while i < len(argv):
        arg = argv[i]
        name, eq, value = arg.partition("=")
        if name in VALUED_GLOBALS:
            if not eq:
                if i + 1 >= len(argv):
                    raise CommandError(f"{name} needs a value")
                value, i = argv[i + 1], i + 1
            profile = value if name == "--profile" else profile
            region = value if name == "--region" else region
        elif arg not in DROPPED_FLAGS:
            out.append(arg)
        i += 1
    return out, profile, region


def _globs(value: Any) -> list[str]:
    return [value] if isinstance(value, str) else list(value or [])


def _matches(patterns: Any, value: str) -> bool:
    return any(fnmatch.fnmatchcase(value, p) for p in _globs(patterns))


def iam_allows(policy: dict, service: str, operation: str, params: dict) -> bool:
    """Deny rules first; then every read, whatever it costs (a Logs Insights query is broad, but still a read); then
    the allow rules for writes."""
    action = f"{service}:{operation}"
    if _matches(policy.get("deny"), action):
        return False
    if is_read(operation) or operation in READ_EXTRA:
        return True
    for rule in policy.get("allow", []):
        if not _matches(rule.get("action"), action):
            continue
        if all(_matches(want, str(params.get(key, ""))) for key, want in (rule.get("params") or {}).items()):
            return True
    return False


class Faults:
    """Scenario fault rules: the first rule matching a call by service, operation, params and window (`from`, `until`,
    and `until_flag`, the world flag that ends it) raises its error, on a seeded `rate` of calls, on every
    `every_nth` one, or, with `min_interval_s`, on one that follows the last accepted call with the same `per`
    parameter (a zone) by less than that. Counters cover every caller and draws are seeded, so a replay sees the
    same faults."""

    def __init__(self, rules: list[dict] | None, seed: str, flags: Callable[[], dict] = dict):
        self.rules = list(rules or [])
        self.seed = seed
        self.flags = flags
        self.seen = [0] * len(self.rules)
        self.fired = [0] * len(self.rules)
        self.last: list[dict[str, datetime]] = [{} for _ in self.rules]

    def check(self, service: str, operation: str, params: dict, t: datetime | None, who: str = "agent") -> None:
        accepted = []
        for i, rule in enumerate(self.rules):
            if not _matches(rule.get("service", "*"), service) or not _matches(rule.get("operation", "*"), operation):
                continue
            if rule.get("who", "agent") not in (who, "*"):
                continue
            if any(not _matches(want, str(params.get(k, ""))) for k, want in (rule.get("params") or {}).items()):
                continue
            if t is not None and ((rule.get("from") and t < _time(rule["from"])) or
                                  (rule.get("until") and t >= _time(rule["until"]))):
                continue
            if rule.get("until_flag") and self.flags().get(rule["until_flag"]):
                continue
            if rule.get("times") is not None and self.fired[i] >= int(rule["times"]):
                continue
            if rule.get("min_interval_s") is not None:
                key = str(params.get(rule.get("per", "HostedZoneId"), "")).rsplit("/", 1)[-1]
                last = self.last[i].get(key)
                if t is None or last is None or (t - last).total_seconds() >= float(rule["min_interval_s"]):
                    accepted.append((i, key))
                    continue
            else:
                self.seen[i] += 1
                if rule.get("every_nth"):
                    if self.seen[i] % int(rule["every_nth"]):
                        continue
                elif int(hashlib.sha256(f"{self.seed}|fault|{i}|{self.seen[i]}".encode()).hexdigest()[:16],
                         16) / 2**64 >= float(rule.get("rate", 1.0)):
                    continue
            self.fired[i] += 1
            err = rule.get("error", "Throttling")
            code = err if isinstance(err, str) else err.get("code", "Throttling")
            message = FAULT_MESSAGES.get(code, "") if isinstance(err, str) else err.get("message", "")
            raise AwsError(code, message, int(rule.get("status", 400)))
        for i, key in accepted:
            self.last[i][key] = t


class WallTimes:
    """Wall-clock times the emulator stamps, as virtual times: a time up to the end of seeding reads `seeded_at`;
    a later one reads the virtual time of the last call made at or before it, as each call marks."""

    def __init__(self, seeded_at: datetime | None):
        self.seeded_at = seeded_at
        self.marks: list[tuple[float, datetime]] = []

    def mark(self, virtual: datetime) -> None:
        if not self.marks or virtual >= self.marks[-1][1]:
            self.marks.append((math.floor(time.time() * 100) / 100, virtual))

    def begin(self, virtual: datetime) -> None:
        """Mark the episode start once the 10 ms bucket holding the last seed stamp has passed, so every seed stamp
        reads seeded_at."""
        edge = time.time()
        while math.floor(time.time() * 100) / 100 <= edge:
            time.sleep(0.001)
        self.mark(virtual)

    def virtual(self, wall: float) -> datetime | None:
        i = bisect_right(self.marks, wall, key=lambda m: m[0])
        return self.marks[i - 1][1] if i else self.seeded_at


def retime(value: Any, shape: Any, times: WallTimes, name: str = "") -> Any:
    """An emulator response with each time it stamped moved to virtual time, in the form it came: a datetime, epoch
    seconds (the CLI prints JSON protocols' times raw), an ISO 8601 or RFC 822 string, or epoch milliseconds in a
    long named like a time (CloudWatch Logs' creationTime). A time later than the wall clock is already virtual."""
    kind = getattr(shape, "type_name", None)
    if value is None or kind is None:
        return value
    if kind == "structure" and isinstance(value, dict):
        return {k: retime(v, shape.members.get(k), times, k) for k, v in value.items()}
    if kind == "list" and isinstance(value, list):
        return [retime(v, shape.member, times, name) for v in value]
    if kind == "map" and isinstance(value, dict):
        return {k: retime(v, shape.value, times, k) for k, v in value.items()}
    ms = kind == "long" and isinstance(value, int) and not isinstance(value, bool) and TIME_LONG.search(name) \
        and value > 10**11
    if kind != "timestamp" and not ms:
        return value
    try:
        wall = value / 1000 if ms else (value if isinstance(value, datetime) else parse_timestamp(value)).timestamp()
    except (TypeError, ValueError):
        return value
    v = times.virtual(wall) if wall <= time.time() + 1 else None
    if v is None:
        return value
    if ms:
        return int(v.timestamp() * 1000)
    if isinstance(value, datetime):
        return v.astimezone(UTC)
    if isinstance(value, (int, float)):
        return type(value)(v.timestamp())
    if "," in value:
        return format_datetime(v.astimezone(UTC), usegmt=True)
    return v.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.000Z" if "." in value else "%Y-%m-%dT%H:%M:%SZ")


class WorldPort(AwsPort):
    """The world's AwsPort, marking each call's virtual time for the emulator's stamps and applying the scenario's
    faults to the calls made for the agent (epctl's writes), at the call's time plus the backoff its retries took."""

    faults: Faults | None = None
    times: WallTimes | None = None

    def call(self, service: str, operation: str, params: dict, *, principal: str, world_made: bool,
             region: str | None = None, log_call: bool | None = None) -> dict:
        t = self.now()
        if self.times is not None and t is not None:
            self.times.mark(t)
        if not world_made and self.faults is not None:
            acting = self.acting or {}
            try:
                self.faults.check(service, operation, params,
                                  t and t + timedelta(seconds=float(acting.get("retry_s") or 0)))
            except AwsError as e:
                if self.actionlog is not None:
                    self.actionlog.record(t=t, t_issue=acting.get("t_issue") or t, principal=principal,
                                          world_made=False, server="aws",
                                          role=acting.get("role"), call_no=acting.get("call_no"), service=service,
                                          operation=operation, params=params, error=e.as_dict())
                raise
        return super().call(service, operation, params, principal=principal, world_made=world_made, region=region,
                            log_call=log_call)


def _time(value: Any) -> datetime:
    return value if isinstance(value, datetime) else datetime.fromisoformat(str(value).replace("Z", "+00:00"))


def _http(status: int) -> SimpleNamespace:
    return SimpleNamespace(status_code=status, headers={}, content=b"", text="", raw=None)


def _utc(value: Any) -> Any:
    """A served response's aware datetimes in UTC, as AWS answers, at any depth."""
    if isinstance(value, datetime):
        return value.astimezone(UTC) if value.tzinfo else value
    if isinstance(value, dict):
        return {k: _utc(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_utc(v) for v in value]
    return value


class Bridge:
    def __init__(self, emulator: Emulator, actionlog: ActionLog, *, profiles: dict[str, list[dict]] | None = None,
                 policies: dict[str, dict] | None = None, faults: list[dict] | None = None, seed: str = "0",
                 world: Callable[[], Any] = lambda: None, world_lock: Any = None):
        self.emulator = emulator
        self.actionlog = actionlog
        self.profiles = profiles or {}
        self.policies = policies or {}
        self.faults = Faults(faults, seed)
        self.times: WallTimes | None = None
        self.trail = Trail(actionlog)
        self.world = world
        self.world_lock = world_lock or threading.RLock()
        self.lock = threading.Lock()
        self._ctx: dict = {}
        emulator.apply_client_env()

    def profile(self, role: str | None, name: str | None) -> dict:
        """The caller's profile: the one named, which must be the role's, else the role's first."""
        options = self.profiles.get(role or "") or next(iter(self.profiles.values()), None) or [
            {"profile": "default", "permission_set": "Admin", "account": "local"}]
        if name is None:
            return options[0]
        for p in options:
            if p.get("profile") == name:
                return p
        mine = ", ".join(p.get("profile", "?") for p in options)
        raise AwsError("AccessDenied", f"The profile {name!r} is not available to you; your profiles: {mine}", 403)

    def principal(self, role: str | None, prof: dict) -> str:
        return f"assumed-role/{prof.get('permission_set', 'Admin')}/{role or 'anonymous'}"

    def _register(self, session: Any) -> None:
        session.register("before-parameter-build", self._stash)
        session.register("before-call", self._gate)
        session.register("after-call", self._after)

    def _stash(self, params: dict, model: Any, context: dict, **_: Any) -> None:
        context["awsbench"] = {"service": model.service_model.service_name, "operation": model.name,
                               "params": copy.deepcopy(params)}

    def _gate(self, model: Any, context: dict, **_: Any) -> tuple | None:
        info = context.setdefault("awsbench", {"service": model.service_model.service_name, "operation": model.name,
                                               "params": {}})
        ctx, service, operation, params = self._ctx, info["service"], info["operation"], info["params"]
        self._ctx.setdefault("open", []).append(info)
        try:
            policy = self.policies.get(ctx.get("permission_set"), DEFAULT_POLICY)
            if not iam_allows(policy, service, operation, params):
                raise AwsError("AccessDenied", f"User: {ctx.get('principal')} is not authorized to perform: "
                                               f"{service}:{operation} because no identity-based policy allows the "
                                               f"{service}:{operation} action", 403)
            self.faults.check(service, operation, params, ctx.get("t"))
            info["served"] = True
            if (service, operation) == ("cloudtrail", "LookupEvents"):
                return _http(200), {**_utc(self.trail.lookup(params)), "ResponseMetadata": {"HTTPStatusCode": 200}}
            world = self.world()
            if world is not None:
                with self.world_lock:
                    served = (world.serve_aws(service, operation, params, region=ctx.get("region"))
                              if getattr(world, "wants_region", False) else world.serve_aws(service, operation, params))
                if served is not None:
                    return _http(200), {**_utc(served), "ResponseMetadata": {"HTTPStatusCode": 200,
                                                                            "RequestId": "awsbench"}}
            info["served"] = False
        except AwsError as e:
            return _http(e.status), {"Error": e.as_dict(), "ResponseMetadata": {"HTTPStatusCode": e.status}}
        return None

    def _after(self, http_response: Any, parsed: dict, model: Any, context: dict, **_: Any) -> None:
        """Logs the call; an answer the emulator gave carries its times in virtual time."""
        info = context.get("awsbench") or {}
        error = parsed.get("Error") if getattr(http_response, "status_code", 200) >= 300 else None
        if error is None and not info.get("served") and self.times is not None:
            parsed.update(retime(parsed, model.output_shape, self.times))
        self._log(info, error)

    def _log(self, info: dict, error: dict | None) -> None:
        if info.get("logged"):
            return
        info["logged"] = True
        ctx = self._ctx
        self.actionlog.record(t=ctx.get("t"), t_issue=ctx.get("t_issue"), call_no=ctx.get("call_no"),
                              role=ctx.get("role"), principal=ctx.get("principal"), world_made=False, server="aws",
                              tool=ctx.get("tool"), service=info.get("service"), operation=info.get("operation"),
                              params=info.get("params"), error=error)

    def _begin(self, role: str | None, profile: str | None, region: str | None, call: dict | None) -> dict:
        prof = self.profile(role, profile)
        call = call or {}
        self._ctx = {"role": role, "profile": prof.get("profile"), "permission_set": prof.get("permission_set"),
                     "principal": self.principal(role, prof), "region": region or "us-east-1",
                     "call_no": call.get("call_no"), "t": call.get("t"), "t_issue": call.get("t_issue"),
                     "tool": call.get("tool"), "open": []}
        if self.times is not None and call.get("t") is not None:
            self.times.mark(call["t"])
        return self._ctx

    def _finish(self, fallback_error: str) -> None:
        for info in self._ctx.get("open", []):
            self._log(info, {"Code": "ConnectionError", "Message": fallback_error[:300]})
        self._ctx = {}

    def _scrub(self, text: str) -> str:
        return text.replace(self.emulator.endpoint, "https://aws.amazon.com")

    def run_cli(self, command: str, *, role: str | None, call: dict | None = None) -> dict:
        """One `aws ...` command as the CLI runs it: {exit_code, stdout, stderr}. Blocking; run it off the loop."""
        try:
            argv, profile, region = split_globals(parse_command(command))
        except CommandError as e:
            return {"exit_code": 2, "stdout": "", "stderr": f"aws: error: {e}\n"}
        with self.lock:
            try:
                ctx = self._begin(role, profile, region, call)
            except AwsError as e:
                return {"exit_code": 254, "stdout": "", "stderr": f"\nAn error occurred ({e.code}): {e.message}\n"}
            verify = self.emulator.verify
            args = argv + ["--endpoint-url", self.emulator.endpoint, "--region", ctx["region"]]
            args += ["--ca-bundle", verify] if verify else ["--no-verify-ssl"]
            out, err = io.StringIO(), io.StringIO()
            try:
                driver = create_clidriver()
                # CLI v1 fetches http(s):// values, which would let the agent read the env's internal routes.
                driver.session.unregister("session-initialized", register_uri_param_handler)
                self._register(driver.session)
                with redirect_stdout(out), redirect_stderr(err):
                    try:
                        rc = driver.main(args)
                    except SystemExit as e:
                        rc = e.code if isinstance(e.code, int) else 2
            finally:
                self._finish(err.getvalue())
        return {"exit_code": rc, "stdout": self._scrub(out.getvalue()), "stderr": self._scrub(err.getvalue())}

    def call_api(self, service: str, operation: str, params: dict | None = None, *, role: str | None,
                 profile: str | None = None, region: str | None = None, call: dict | None = None) -> dict:
        """One API call through botocore: the response without ResponseMetadata, or {"error": {Code, Message}}."""
        with self.lock:
            try:
                ctx = self._begin(role, profile, region, call)
            except AwsError as e:
                return {"error": e.as_dict()}
            message = ""
            try:
                session = botocore.session.get_session()
                self._register(session)
                client = session.create_client(service, region_name=ctx["region"], endpoint_url=self.emulator.endpoint,
                                               verify=self.emulator.verify, aws_access_key_id=ACCESS_KEY,
                                               aws_secret_access_key=SECRET_KEY)
                method = xform_name(operation)
                if not hasattr(client, method):
                    return {"error": {"Code": "InvalidAction", "Message": f"{service} has no operation {operation}"}}
                out = getattr(client, method)(**(params or {}))
                out.pop("ResponseMetadata", None)
                return out
            except ClientError as e:
                return {"error": {"Code": e.response.get("Error", {}).get("Code", "Unknown"),
                                  "Message": e.response.get("Error", {}).get("Message", "")}}
            except ParamValidationError as e:
                return {"error": {"Code": "ValidationException", "Message": str(e)}}
            except BotoCoreError as e:
                message = self._scrub(str(e))
                return {"error": {"Code": type(e).__name__, "Message": message}}
            except Exception as e:  # unknown service names and the like
                return {"error": {"Code": "InvalidRequest", "Message": self._scrub(str(e))}}
            finally:
                self._finish(message)
