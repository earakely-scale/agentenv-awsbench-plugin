"""Export an AwsBench AgentEnv task as a self-contained Harbor task package that runs the same artifacts.

    python tools/export_harbor.py                    # (re)write harbor/t01-empty-ledger-endpoint from the bundle
    python tools/export_harbor.py --check            # regenerate into a temp dir and diff; exit 1 on drift
    python tools/export_harbor.py --ref <commit> --out <dir>   # export the bundle as it was at another commit

The package holds byte copies of the bundle's aws and workspace server folders, its post-turn hook and its grader; the
gateway, agent and service-db images AgentEnv runs (by digest, from agentenv-framework's published_images.json); and a
docker-compose.yaml whose gateway, server and service-db blocks are rendered by agentenv-framework's own
EnvironmentGatewayProvider.create_docker_compose, then adapted for Harbor (no host ports, a front for the agent, the
harness sidecar). Every setup value comes from the task file; a task whose steps this exporter can't express is refused,
so a change to the AgentEnv task fails the export instead of drifting from it.
"""
import argparse
import difflib
import filecmp
import fnmatch
import hashlib
import importlib.util
import inspect
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
BUNDLE = Path("src/agentenv_awsbench/bundles/awsbench")
HARBOR = ROOT / "harbor"
BASE = HARBOR / "base"
SLUG = "t01-empty-ledger-endpoint"
TASK = "t01-draft"
TASK_NAME = "awsbench/t01-empty-ledger-endpoint"
AGENTENV_TASK_ID = "@local/~/awsbench/src/agentenv_awsbench/bundles/awsbench/t01-draft"
PORT = 18765
GATEWAY_URL = f"http://gateway:{PORT}"
FRONT_URL = f"http://mcp:{PORT}/mcp"
PYTHON_IMAGE = "public.ecr.aws/docker/library/python@sha256:5e2bdb1b0d542d5039cffb8181c65930093949194a65c365dfb1b0fe13a4fa5b"
NGINX_IMAGE = "public.ecr.aws/docker/library/nginx@sha256:65645c7bb6a0661892a8b03b89d0743208a18dd2f3f17a54ef4b76fb8e2f2a10"
SHAPE = [("deploy", "deploy_env"), ("configure", "apply_server_config"), ("clock", "sync_env_clock"),
         ("lock-all", "modify_env_tool_access"), ("allow-sre", "modify_env_tool_access"),
         ("allow-harness", "modify_env_tool_access"), ("deploy-sre", "deploy_agent"), ("incident", "prompt_agent"),
         ("horizon", "apply_server_config"), ("grade", "env_outcome_verifier"), ("horizon-snapshot", "snapshot_env")]
PROMPT_KEYS = {"id", "type", "agent_name", "prompt_id", "prompt", "timeout_seconds", "max_conversation_turns",
               "max_turns", "fail_task_on_error", "post_turn_hooks"}
AGENT_KEYS = {"id", "type", "agent_name", "env_ids", "role", "builtin_tools", "ttl_seconds"}
JUDGE_MODEL = "claude-sonnet-5-5"  # verify.py's default for AWSBENCH_JUDGE_MODEL, which AgentEnv's runs used
SOLUTION_MODES = ("oracle", "alt", "nothing", "trap", "noshed", "early", "rollback", "scale", "weakshed")
FORBIDDEN_FILE = ROOT / ".harbor-forbidden"  # untracked: one regex per line the public package must not contain


class ExportError(ValueError):
    pass


def published(repository):
    from agent_env.env import published_images
    path = Path(published_images.__file__).with_name("published_images.json")
    for image in json.loads(path.read_text())["images"]:
        if image["repository"] == repository:
            return f"{repository}@{image['digest']}"
    raise ExportError(f"agentenv-framework publishes no {repository}")


def check_shape(steps):
    got = [(s.get("id"), s.get("type")) for s in steps]
    if got != SHAPE:
        raise ExportError(f"the task's steps changed shape; this exporter expresses {SHAPE}, the task has {got}")
    by_id = {s["id"]: s for s in steps}
    prompt, agent = by_id["incident"], by_id["deploy-sre"]
    if set(prompt) - PROMPT_KEYS:
        raise ExportError(f"prompt_agent sets {sorted(set(prompt) - PROMPT_KEYS)}, which the Harbor agent can't express")
    if set(agent) - AGENT_KEYS:
        raise ExportError(f"deploy_agent sets {sorted(set(agent) - AGENT_KEYS)}, which the Harbor package can't express")
    if len(prompt["post_turn_hooks"]) != 1 or "{" in prompt["prompt"]:
        raise ExportError("the exporter expresses one post-turn hook and a prompt without placeholders")
    for s in steps:
        if s["type"] in ("apply_server_config", "sync_env_clock", "modify_env_tool_access", "env_outcome_verifier",
                         "snapshot_env", "deploy_agent", "prompt_agent") and s.get("env_id", "awsbench") != "awsbench":
            raise ExportError(f"step {s['id']} targets {s.get('env_id')}, not awsbench")
    if by_id["grade"].get("score_aggregator") != "weighted_average":
        raise ExportError("the grade step's aggregator is not weighted_average")
    return by_id


def dockerignore(folder):
    path = folder / ".dockerignore"
    rules = [line.strip() for line in path.read_text().splitlines()] if path.exists() else []
    return [r for r in rules if r and not r.startswith("#")] + ["__pycache__/", "*.pyc"]


def ignored(rel, rules):
    parts = rel.split("/")
    for rule in rules:
        pattern = rule.rstrip("/")
        if any(fnmatch.fnmatch(p, pattern) for p in parts) or fnmatch.fnmatch(rel, pattern):
            return True
    return False


def copy_tree(src, dst, files):
    rules = dockerignore(src)
    for path in sorted(src.rglob("*")):
        rel = path.relative_to(src).as_posix()
        if path.is_dir() or (ignored(rel, rules) and rel != ".dockerignore"):
            continue
        write_bytes(dst / rel, path.read_bytes(), files, mode=path.stat().st_mode & 0o777)


def write_bytes(path, data, files, mode=0o644):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    os.chmod(path, 0o755 if mode & 0o100 else 0o644)
    files.append(path)


def write_text(path, text, files, mode=0o644):
    write_bytes(path, text.encode("utf-8"), files, mode)


def template(name, **values):
    text = (BASE / name).read_text()
    for key, value in values.items():
        text = text.replace("{" + key + "}", value)
    return text


def constraints():
    """The AgentEnv runner's package versions (pip freeze of the host-2 runner venv), as pip constraints."""
    lines = (BASE / "runner-freeze.txt").read_text().splitlines()
    return "\n".join(line for line in lines if re.fullmatch(r"[a-z0-9][a-z0-9._-]*==[^\s]+", line)) + "\n"


def render_compose(gateway_image, servicedb_image):
    """The framework's own compose for the MultiEnv's two servers, then adapted for Harbor."""
    from agent_env.env.envs.service_db import ServiceDBConfig
    from agent_env.providers.env_providers.env_gateway_provider import EnvironmentGatewayProvider, MCPServerConfig
    from agent_env.providers.env_state.env_state_provider import EnvStateInstance
    from agent_env.providers.env_state.local_postgres import LocalPostgresStateProvider

    state = LocalPostgresStateProvider(ServiceDBConfig(db_image=servicedb_image))
    instance = EnvStateInstance(state_type="local_postgres", _db_url_base=state._pg_url("servicedb"))
    provider = EnvironmentGatewayProvider.__new__(EnvironmentGatewayProvider)
    rendered = provider.create_docker_compose(
        [MCPServerConfig(image="awsbench-aws", environment_name="aws"),
         MCPServerConfig(image="awsbench-workspace", environment_name="workspace")],
        gateway_image=gateway_image, gateway_port=PORT, expose_gateway_port=False,
        state_provider=state, state_instance=instance, mcp_server_name="env0000", control_token=None)
    init_sql = state.get_init_script(["aws", "workspace"])
    return rendered, yaml.safe_load(rendered), init_sql


GATEWAY_WRAPPER = r"""set -eu
if [ ! -s /ctl/name ]; then
  python3 -c 'import os, secrets
open("/ctl/token.tmp", "w").write(secrets.token_urlsafe(32))
open("/ctl/name.tmp", "w").write(f"env{secrets.randbelow(10000):04d}")
os.replace("/ctl/token.tmp", "/ctl/token")
os.replace("/ctl/name.tmp", "/ctl/name")'
fi
AGENTENV_CONTROL_TOKEN="$(cat /ctl/token)"
MCP_SERVER_NAME="$(cat /ctl/name)"
export AGENTENV_CONTROL_TOKEN MCP_SERVER_NAME
exec python -m gateway.entrypoint
"""


FRONT_HEALTH = ("code=$$(curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:18765/mcp); "
                "case $$code in 000|502|503|504) exit 1;; esac")  # nginx up, and the gateway behind it answering


def adapt_compose(doc, contexts):
    """Harbor's compose: the rendered services, built from the copied folders, plus the front, the harness and main."""
    services = doc["services"]
    for name in contexts:  # built per trial (BuildKit's cache keeps it fast): a shared tag races between jobs
        if name in services:
            services[name].pop("image", None)
            services[name]["build"] = {"context": f"./{name}"}
    servicedb = services["servicedb"]
    servicedb["volumes"] = ["./servicedb/init-schemas.sql:/docker-entrypoint-initdb.d/init-schemas.sql:ro"]
    gateway = services["gateway"]
    gateway["environment"] = [e for e in gateway["environment"] if not e.startswith("MCP_SERVER_NAME=")]
    gateway["entrypoint"] = ["sh", "-c", GATEWAY_WRAPPER.replace("$", "$$")]  # compose interpolates a bare $
    gateway["volumes"] = ["ctl:/ctl"]
    services["mcp"] = {"build": {"context": "./mcp"},
                       "depends_on": {"gateway": {"condition": "service_healthy"}},
                       "healthcheck": {"test": ["CMD-SHELL", FRONT_HEALTH], "interval": "5s", "timeout": "5s",
                                       "retries": 3, "start_period": "60s", "start_interval": "2s"},
                       "networks": ["env-network", "default"]}
    services["harness"] = {
        "build": {"context": "./harness"},
        "depends_on": {"gateway": {"condition": "service_healthy"}},
        "volumes": ["ctl:/ctl:ro"],
        "healthcheck": {"test": ["CMD", "test", "-f", "/run/awsbench/ready"], "interval": "5s", "timeout": "5s",
                        "retries": 3, "start_period": "900s", "start_interval": "2s"},
        "networks": ["env-network"]}
    services["main"] = {"depends_on": {"harness": {"condition": "service_healthy"},
                                       "mcp": {"condition": "service_healthy"}},
                        "extra_hosts": ["host.docker.internal:host-gateway"]}
    doc["volumes"] = {"ctl": {}}
    return doc


def context_digest(folder):
    h = hashlib.sha256()
    for path in sorted(folder.rglob("*")):
        if path.is_file():
            h.update(path.relative_to(folder).as_posix().encode() + b"\0" + path.read_bytes() + b"\0")
    return h.hexdigest()[:12]


def driver_module(root):
    sys.path.insert(0, str(root / "tests/e2e"))
    try:
        spec = importlib.util.spec_from_file_location("awsbench_e2e_driver", root / "tests/e2e/driver.py")
        driver = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(driver)
        gw = sys.modules["gw"]
    finally:
        sys.path.pop(0)
    return driver, gw


def solution_files(root):
    driver, gw = driver_module(root)
    modes = {}
    for mode in SOLUTION_MODES:
        modes[mode] = [[tool, args, check[0].__name__ if check else None] for tool, args, *check in driver.steps_for(mode)]
    paths = {"mcp_url": FRONT_URL, "ledger": driver.LEDGER, "free": sorted(driver.FREE), "needed": sorted(driver.SRE_TOOLS),
             "synonyms": {k: list(v) for k, v in gw.SYNONYMS.items()}, "modes": modes}
    functions = "\n\n".join(inspect.getsource(getattr(driver, name)) for name in
                            ("ran", "throttled", "reverted", "failed_hosts", "fill", "harvest", "wait_through", "play"))
    play = (BASE / "solution/play.py").read_text()
    play = play.replace("# @ADAPT@\n", inspect.getsource(gw.Gateway.adapt))
    play = play.replace("# @DRIVER@\n", "# --- copied from tests/e2e/driver.py by tools/export_harbor.py ---\n\n"
                        + functions)
    return json.dumps(paths, indent=1, sort_keys=True) + "\n", play


def setup_json(by_id, prompt_sha):
    from agent_env.env.gateway.constants import (EXT_CLOCK_URI, EXT_DISABLE_TOOL_URI, EXT_ENABLE_TOOL_URI,
                                                 EXT_STEP_URI, STEP_SOURCE, STEP_SOURCE_POST_TURN_HOOK)
    from agent_env.task_step.task_steps.sync_env_clock import resolve_virtual_time

    clock = dict(by_id["clock"])
    clock["armed_virtual_time"] = resolve_virtual_time(clock["virtual_time"], clock.get("timezone"))
    prompt, hook = by_id["incident"], by_id["incident"]["post_turn_hooks"][0]
    return {
        "gateway_url": GATEWAY_URL, "front_url": FRONT_URL, "services": ["aws", "workspace"],
        "role": by_id["deploy-sre"]["role"],
        "uris": {"clock": EXT_CLOCK_URI, "disable": EXT_DISABLE_TOOL_URI, "enable": EXT_ENABLE_TOOL_URI,
                 "step": EXT_STEP_URI, "step_source": STEP_SOURCE, "step_source_post_turn_hook": STEP_SOURCE_POST_TURN_HOOK},
        "setup_steps": [by_id["configure"], clock, by_id["lock-all"], by_id["allow-sre"], by_id["allow-harness"]],
        "sre_tools": by_id["allow-sre"]["tools"],
        "horizon": by_id["horizon"],
        "hook": {"artifact_id": hook["file_artifact_id"], "params": hook["params"], "env_id": "awsbench",
                 "step_id": prompt["id"], "tool_call_timeout_s": 600},
        "prompt": {"sha256": prompt_sha, "prompt_id": prompt["prompt_id"], "timeout_seconds": prompt["timeout_seconds"],
                   "max_conversation_turns": prompt["max_conversation_turns"], "max_turns": prompt["max_turns"],
                   "builtin_tools": by_id["deploy-sre"]["builtin_tools"], "task_id": AGENTENV_TASK_ID,
                   "hook_timeout_s": 600 * 22},
    }


def task_toml(by_id, source):
    prompt = by_id["incident"]
    return f"""schema_version = "1.4"
source = "{source}"
artifacts = [{{ source = "/export", destination = "export", service = "harness" }}]

[task]
name = "{TASK_NAME}"
description = "AwsBench T01 \\"The Empty Ledger Endpoint\\": incident command for an empty internal DNS endpoint left by an automation race, in an AWS Emulator world with colleagues, a clock and a grader at a fixed horizon. Generated from the AgentEnv task {TASK} by tools/export_harbor.py; do not edit."
keywords = ["sre", "incident-response", "aws", "dns", "mcp", "long-horizon"]

[metadata]
agentenv_task = "{TASK}"
agentenv_task_id = "{AGENTENV_TASK_ID}"
prompt_id = "{prompt["prompt_id"]}"
parity_agent = "awsbench_harbor.claude_code:AgentEnvClaudeCode (PYTHONPATH=harbor/agents)"
scripted_agent = "awsbench_harbor.scripted:ScriptedSolution --ak mode=<oracle|alt|trap|...>"

[agent]
timeout_sec = {by_id["deploy-sre"]["ttl_seconds"]}
user = "agent"

[environment]
build_timeout_sec = 1800

[[environment.mcp_servers]]
name = "awsbench"
transport = "streamable-http"
url = "{FRONT_URL}"

[verifier]
environment_mode = "separate"
timeout_sec = 1800

[verifier.env]
LITELLM_API_KEY = "${{LITELLM_API_KEY:-}}"
LITELLM_BASE_URL = "${{LITELLM_BASE_URL:-}}"
AWSBENCH_JUDGE = "${{AWSBENCH_JUDGE:-on}}"
AWSBENCH_JUDGE_MODEL = "${{AWSBENCH_JUDGE_MODEL:-{JUDGE_MODEL}}}"

[[verifier.collect]]
service = "harness"
command = "python -m awsbench_harness collect"
timeout_sec = {by_id["horizon"]["timeout_seconds"] + 900}

[solution.env]
AWSBENCH_QC_SOLUTION_MODE = "${{AWSBENCH_QC_SOLUTION_MODE:-oracle}}"
"""


TREES = {"bundle": BUNDLE.as_posix(), "harbor_base": "harbor/base", "driver": "tests/e2e/driver.py"}


def git(*args, env=None):
    return subprocess.run(["git", *args], cwd=ROOT, check=True, capture_output=True, text=True,
                          env={**os.environ, **(env or {})}).stdout.strip()


def source_trees(ref):
    """The git object ids of what the package is generated from: the bundle and harbor/base as trees, the driver as a
    blob. Any commit C with `git rev-parse C:<path>` equal to an id holds exactly that source, so the ids name the task
    version without naming a commit the package itself would have to be regenerated for. Without --ref they are
    computed from the working tree (a throwaway index), as the next commit will record them."""
    if ref:
        return {name: git("rev-parse", f"{ref}:{path}") for name, path in TREES.items()}
    with tempfile.TemporaryDirectory() as tmp:
        env = {"GIT_INDEX_FILE": str(Path(tmp) / "index")}
        git("add", "-A", "--", *TREES.values(), env=env)
        out = {name: git("write-tree", f"--prefix={path}/", env=env) for name, path in TREES.items()
               if name != "driver"}
    out["driver"] = git("hash-object", TREES["driver"])
    return out


def server_pins(bundle):
    """What the bundle's server images pin (both systems build them from the same Dockerfiles)."""
    aws = (bundle / "envs/aws/Dockerfile").read_text()
    pins = {"base": re.search(r"^FROM (\S+)", aws, re.M).group(1)}
    for key in ("AWS_EMULATOR_VERSION", "AWS_EMULATOR_SHA256_AMD64", "AWS_EMULATOR_SHA256_ARM64"):
        pins[key.lower()] = re.search(rf"^ARG {key}=(\S+)", aws, re.M).group(1)
    for server in ("aws", "workspace"):
        lock = (bundle / f"envs/{server}/requirements.txt").read_text()
        pins[f"{server}_requirements_sha256"] = hashlib.sha256(lock.encode()).hexdigest()
        if not re.search(r"--hash=sha256:", lock) or re.search(r"^[A-Za-z0-9._-]+(?:[<>~!]=|>|<)", lock, re.M):
            raise ExportError(f"envs/{server}/requirements.txt is not a hashed lock of exact versions")
    return pins


def bundle_root(ref, tmp):
    if not ref:
        return ROOT
    out = Path(tmp) / "src"
    out.mkdir()
    archive = subprocess.run(["git", "archive", ref, "src", "tests/e2e"], cwd=ROOT, check=True, capture_output=True)
    subprocess.run(["tar", "-x", "-C", str(out)], input=archive.stdout, check=True)
    return out


def export(out, ref=None):
    with tempfile.TemporaryDirectory() as tmp:
        root = bundle_root(ref, tmp)
        bundle = root / BUNDLE
        steps = json.loads((bundle / "tasks" / f"{TASK}.json").read_text())
        by_id = check_shape(steps)
        target = out / SLUG
        if target.exists():
            shutil.rmtree(target)
        files = []
        prompt = by_id["incident"]["prompt"]
        write_text(target / "instruction.md", prompt, files)
        gateway_image = published("ghcr.io/scaleapi/agentenv-gateway")
        agent_image = published("ghcr.io/scaleapi/agentenv-claude-code")
        servicedb_image = published("ghcr.io/scaleapi/agentenv-service-db")
        env_dir = target / "environment"
        for name in ("aws", "workspace"):
            copy_tree(bundle / "envs" / name, env_dir / name, files)
        for name in ("entrypoint.sh", "peers.py", "turn.py", "wait_port.py", "agentenv-env.json"):
            write_bytes(env_dir / "main" / name, (BASE / "main" / name).read_bytes(), files,
                        mode=0o755 if name.endswith(".sh") else 0o644)
        write_text(env_dir / "Dockerfile", template("main/Dockerfile", agent_image=agent_image), files)
        cons = constraints()
        harness = env_dir / "harness"
        write_text(harness / "Dockerfile", template("harness/Dockerfile", python_image=PYTHON_IMAGE), files)
        write_bytes(harness / "requirements.txt", (BASE / "harness/requirements.txt").read_bytes(), files)
        write_text(harness / "constraints.txt", cons, files)
        for path in sorted((BASE / "harness/awsbench_harness").glob("*.py")):
            write_bytes(harness / "awsbench_harness" / path.name, path.read_bytes(), files)
        hook = bundle / "artifacts/awsbench-turn/hook.py"
        grader = bundle / "artifacts/t01-grade/verify.py"
        write_bytes(harness / "hook.py", hook.read_bytes(), files)
        write_bytes(harness / "verify.py", grader.read_bytes(), files)
        prompt_sha = hashlib.sha256(prompt.encode()).hexdigest()
        write_text(harness / "setup.json", json.dumps(setup_json(by_id, prompt_sha), indent=1, ensure_ascii=False) + "\n",
                   files)
        role = by_id["deploy-sre"]["role"]
        write_text(env_dir / "mcp/Dockerfile", template("mcp/Dockerfile", nginx_image=NGINX_IMAGE), files)
        write_text(env_dir / "mcp/nginx.conf", template("mcp/nginx.conf", role=role), files)
        rendered, doc, init_sql = render_compose(gateway_image, servicedb_image)
        write_text(env_dir / "servicedb/init-schemas.sql", init_sql, files)
        contexts = {name: context_digest(env_dir / name) for name in ("aws", "workspace", "mcp", "harness")}
        compose = adapt_compose(doc, contexts)
        header = ("# Generated by tools/export_harbor.py from agentenv-framework's EnvironmentGatewayProvider."
                  "create_docker_compose\n# (gateway, aws, workspace, servicedb as AgentEnv renders them), adapted "
                  "for Harbor: no host ports, the\n# control token and server name drawn per stack by the gateway "
                  "wrapper, the agent's front (mcp), the harness.\n")
        write_text(env_dir / "docker-compose.yaml", header + yaml.safe_dump(compose, sort_keys=False, width=200), files)
        tests = target / "tests"
        write_text(tests / "Dockerfile", template("tests/Dockerfile", python_image=PYTHON_IMAGE), files)
        write_bytes(tests / "requirements.txt", (BASE / "tests/requirements.txt").read_bytes(), files)
        write_text(tests / "constraints.txt", cons, files)
        write_bytes(tests / "test.sh", (BASE / "tests/test.sh").read_bytes(), files, mode=0o755)
        write_bytes(tests / "run_grade.py", (BASE / "tests/run_grade.py").read_bytes(), files)
        from agent_env.task_step.task_steps.verifiers import scoring
        write_bytes(tests / "scoring.py", Path(scoring.__file__).read_bytes(), files)
        write_bytes(tests / "verify.py", grader.read_bytes(), files)
        paths, play = solution_files(root)
        write_bytes(target / "solution/solve.sh", (BASE / "solution/solve.sh").read_bytes(), files, mode=0o755)
        write_text(target / "solution/play.py", play, files)
        write_text(target / "solution/paths.json", paths, files)
        source = f"{BUNDLE.as_posix()}/tasks/{TASK}.json"
        write_text(target / "task.toml", task_toml(by_id, source), files)
        import agent_env
        from importlib.metadata import version
        provenance = {
            "agentenv_task": source, "agentenv_task_id": AGENTENV_TASK_ID,
            "source_trees": source_trees(ref), "server_pins": server_pins(bundle),
            "agentenv_framework": version("agentenv-framework"),
            "agentenv_framework_protocol": version("agentenv-framework-protocol"),
            "images": {"gateway": gateway_image, "agent": agent_image, "servicedb": servicedb_image,
                       "python": PYTHON_IMAGE, "nginx": NGINX_IMAGE},
            "rendered_compose_sha256": hashlib.sha256(rendered.encode()).hexdigest(),
            "sources_sha256": {name: hashlib.sha256((bundle / rel).read_bytes()).hexdigest() for name, rel in (
                ("task", f"tasks/{TASK}.json"), ("hook", "artifacts/awsbench-turn/hook.py"),
                ("grader", "artifacts/t01-grade/verify.py"))},
            "files": {p.relative_to(target).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
                      for p in sorted(files)},
        }
        del agent_env
        write_text(target / "PROVENANCE.json", json.dumps(provenance, indent=1, sort_keys=True) + "\n", files)
    scan(target)
    return target


def scan(target):
    """Refuse an export holding a string the public package must not hold; the patterns live in an untracked file."""
    if not FORBIDDEN_FILE.exists():
        return
    patterns = [re.compile(p) for p in FORBIDDEN_FILE.read_text().splitlines() if p.strip()]
    for path in target.rglob("*"):
        if path.is_file():
            text = path.read_bytes().decode("utf-8", "replace")
            for pattern in patterns:
                if pattern.search(text):
                    raise ExportError(f"{path.relative_to(target)} matches a forbidden pattern")


def diff_trees(a, b):
    out = []
    cmp = filecmp.dircmp(a, b)

    def walk(c, rel):
        out.extend(f"only in generated: {rel}{n}" for n in c.left_only)
        out.extend(f"only in committed: {rel}{n}" for n in c.right_only)
        for name in c.diff_files:
            left = (Path(c.left) / name).read_text(errors="replace").splitlines()
            right = (Path(c.right) / name).read_text(errors="replace").splitlines()
            out.append(f"differs: {rel}{name}\n" + "\n".join(list(difflib.unified_diff(right, left, lineterm=""))[:40]))
        for name, sub in c.subdirs.items():
            walk(sub, f"{rel}{name}/")
    walk(cmp, "")
    return out


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", type=Path, default=HARBOR)
    parser.add_argument("--ref", help="export the bundle as it was at this commit")
    parser.add_argument("--check", action="store_true", help="regenerate into a temp dir and diff against --out")
    args = parser.parse_args(argv)
    if args.check:
        with tempfile.TemporaryDirectory() as tmp:
            fresh = export(Path(tmp), args.ref)
            problems = diff_trees(fresh, args.out / SLUG)
        for problem in problems:
            print(problem)
        return 1 if problems else 0
    print(export(args.out, args.ref))
    return 0


if __name__ == "__main__":
    sys.exit(main())
