"""The AWS Emulator process: started at boot, restarted for a clean state.

With AWSBENCH_EMULATOR_URL set, an emulator someone else runs is used instead (tests, a sidecar)."""

import logging
import os
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

import httpx

log = logging.getLogger(__name__)

ACCESS_KEY = "AKIAIOSFODNN7EXAMPLE"
SECRET_KEY = "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"
DEFAULT_PORT = 9297


class EmulatorError(RuntimeError):
    pass


class Emulator:
    def __init__(self, port: int | None = None, home: str | None = None, binary: str = "aws-emulator"):
        self.port = int(port or os.environ.get("AWSBENCH_EMULATOR_PORT", DEFAULT_PORT))
        self.external = os.environ.get("AWSBENCH_EMULATOR_URL")
        self.home = Path(home or os.environ.get("AWSBENCH_EMULATOR_HOME") or tempfile.mkdtemp(prefix="aws-emulator-"))
        self.binary = binary
        self.proc: subprocess.Popen | None = None

    @property
    def endpoint(self) -> str:
        return (self.external or f"https://127.0.0.1:{self.port}").rstrip("/")

    @property
    def ca_path(self) -> str | None:
        path = self.home / ".aws" / "emulator" / str(self.port) / "ca.pem"
        return str(path) if path.is_file() else None

    @property
    def verify(self) -> str | bool:
        """What clients pass as `verify`: the emulator's CA, else no verification (self-signed, loopback only)."""
        if os.environ.get("AWSBENCH_EMULATOR_VERIFY", "1") == "0":
            return False
        return self.ca_path or False

    def healthy(self) -> bool:
        try:
            return httpx.get(f"{self.endpoint}/__admin/health", verify=False, timeout=2).status_code == 200
        except httpx.HTTPError:
            return False

    def start(self, timeout: float = 30.0) -> None:
        if self.external or (self.proc and self.proc.poll() is None):
            return self.wait(timeout)
        if shutil.which(self.binary) is None:
            raise EmulatorError(f"{self.binary} is not installed")
        self.home.mkdir(parents=True, exist_ok=True)
        env = {**os.environ, "HOME": str(self.home)}
        self.proc = subprocess.Popen([self.binary, "-a", "off", "--telemetry", "off", "-p", str(self.port)], env=env,
                                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
        self.wait(timeout)

    def wait(self, timeout: float = 30.0) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.healthy():
                return
            if self.proc is not None and self.proc.poll() is not None:
                raise EmulatorError(f"aws-emulator exited with {self.proc.returncode}")
            time.sleep(0.1)
        raise EmulatorError(f"aws-emulator not healthy after {timeout:.0f}s at {self.endpoint}")

    def stop(self) -> None:
        if self.proc is None:
            return
        if self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(10)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(5)
        self.proc = None

    def restart(self, timeout: float = 30.0) -> None:
        """A clean emulator: a new process over an empty home. An external emulator is kept as it is."""
        if self.external:
            return self.wait(timeout)
        self.stop()
        shutil.rmtree(self.home / ".aws" / "emulator", ignore_errors=True)
        self.start(timeout)

    def apply_client_env(self) -> None:
        """Point this process's aws-cli and boto3 at fixed keys and no user profile or endpoint."""
        for key in [k for k in os.environ if k.startswith(("AWS_PROFILE", "AWS_DEFAULT_PROFILE", "AWS_SESSION_TOKEN",
                                                           "AWS_ENDPOINT_URL", "AWS_CA_BUNDLE", "AWS_ROLE_"))]:
            os.environ.pop(key, None)
        os.environ.update(self.client_env())

    def client_env(self) -> dict:
        """Process environment for in-process aws-cli and boto3: fixed keys, no user config."""
        return {"AWS_ACCESS_KEY_ID": ACCESS_KEY, "AWS_SECRET_ACCESS_KEY": SECRET_KEY, "AWS_DEFAULT_REGION": "us-east-1",
                "AWS_CONFIG_FILE": os.devnull, "AWS_SHARED_CREDENTIALS_FILE": os.devnull, "AWS_PAGER": "",
                "AWS_EC2_METADATA_DISABLED": "true", "AWS_MAX_ATTEMPTS": "2"}
