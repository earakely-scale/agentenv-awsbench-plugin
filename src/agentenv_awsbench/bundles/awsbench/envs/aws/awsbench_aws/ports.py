"""The world's two ports: AWS state through boto3 against the emulator, and the workspace through its internal route.

Both are blocking; the server calls the world from a worker thread, so they never block the event loop."""

import logging
import threading
from collections.abc import Callable
from datetime import datetime
from typing import Any

import boto3
import httpx
from botocore import xform_name
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError

from .actionlog import ActionLog
from .emulator import ACCESS_KEY, SECRET_KEY, Emulator
from .errors import AwsError
from .meter import Inbox, op_class

log = logging.getLogger(__name__)

__all__ = ["AwsError", "AwsPort", "WorkspacePort"]


class AwsPort:
    """Calls against the emulator. World reads are not logged (the world polls every tick); writes are, and every
    call made on the agent's behalf (`world_made=False`) is, under `acting` ({call_no, role}), the agent call it serves
    (an epctl command's writes and the fleet_exec that ran it are one change)."""

    def __init__(self, emulator: Emulator, actionlog: ActionLog | None = None,
                 now: Callable[[], datetime | None] = lambda: None):
        self.emulator = emulator
        self.actionlog = actionlog
        self.now = now
        self._clients: dict[tuple[str, str], Any] = {}
        self._lock = threading.Lock()
        self.acting: dict | None = None
        emulator.apply_client_env()

    def client(self, service: str, region: str = "us-east-1") -> Any:
        key = (service, region)
        with self._lock:
            if key not in self._clients:
                self._clients[key] = boto3.client(
                    service, region_name=region, endpoint_url=self.emulator.endpoint, verify=self.emulator.verify,
                    aws_access_key_id=ACCESS_KEY, aws_secret_access_key=SECRET_KEY,
                    config=Config(retries={"max_attempts": 3, "mode": "standard"}, connect_timeout=5, read_timeout=30))
            return self._clients[key]

    def reset(self) -> None:
        with self._lock:
            self._clients.clear()

    def call(self, service: str, operation: str, params: dict, *, principal: str, world_made: bool,
             region: str | None = None, log_call: bool | None = None) -> dict:
        region = region if region and region != "global" else "us-east-1"
        error, out = None, {}
        try:
            out = getattr(self.client(service, region), xform_name(operation))(**(params or {}))
            out.pop("ResponseMetadata", None)
            return out
        except ClientError as e:
            err = e.response.get("Error", {})
            error = {"Code": err.get("Code", "Unknown"), "Message": err.get("Message", "")}
            raise AwsError(error["Code"], error["Message"],
                           e.response.get("ResponseMetadata", {}).get("HTTPStatusCode", 400)) from None
        except BotoCoreError as e:
            error = {"Code": type(e).__name__, "Message": str(e)}
            raise AwsError(error["Code"], error["Message"], 500) from None
        finally:
            should_log = log_call if log_call is not None else (not world_made or op_class(service, operation) != "point_read")
            if self.actionlog is not None and should_log:
                t = self.now()
                acting = {} if world_made else (self.acting or {})
                self.actionlog.record(t=t, t_issue=t, principal=principal, world_made=world_made, server="aws",
                                      role="world" if world_made else acting.get("role"), call_no=acting.get("call_no"),
                                      service=service, operation=operation, params=params, error=error)


class WorkspacePort:
    """POST {workspace}/awsbench/world/apply. An unreachable workspace never fails the world: the op is kept in
    `dropped` and answered with no deliveries."""

    def __init__(self, url: str, inbox: Inbox | None = None, timeout: float = 30.0, headers: dict | None = None):
        self.url = url.rstrip("/")
        self.inbox = inbox
        self.timeout = timeout
        self.headers = headers or {}
        self.dropped: list[dict] = []
        self._client = httpx.Client(timeout=timeout)

    def apply(self, op: str, args: dict, at: datetime) -> dict:
        body = {"op": op, "args": args, "at": at.isoformat() if isinstance(at, datetime) else at}
        try:
            r = self._client.post(f"{self.url}/awsbench/world/apply", json=body, headers=self.headers)
            out = r.json() if r.content else {}
            if r.status_code >= 400:
                raise httpx.HTTPStatusError(f"{r.status_code}: {r.text[:300]}", request=r.request, response=r)
        except (httpx.HTTPError, ValueError) as e:
            log.warning("workspace apply %s failed: %s", op, e)
            self.dropped.append({**body, "error": str(e)[:300]})
            return {"ok": False, "error": str(e)[:300], "deliveries": []}
        if self.inbox is not None:
            self.inbox.extend(out.get("deliveries"))
        return out

    def health(self) -> bool:
        try:
            return self._client.get(f"{self.url}/awsbench/health", timeout=3).status_code == 200
        except httpx.HTTPError:
            return False
