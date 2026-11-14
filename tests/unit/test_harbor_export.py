"""The Harbor package under harbor/ is what tools/export_harbor.py makes from the AgentEnv task, and it carries the
task's own artifacts, values and paths."""
import hashlib
import importlib.util
import json
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("export_harbor", ROOT / "tools/export_harbor.py")
export_harbor = importlib.util.module_from_spec(spec)
spec.loader.exec_module(export_harbor)
PKG = ROOT / "harbor" / export_harbor.SLUG
BUNDLE = ROOT / export_harbor.BUNDLE
TASK = json.loads((BUNDLE / "tasks/t01-draft.json").read_text())
BY_ID = {s["id"]: s for s in TASK}


def test_package_is_current():
    assert export_harbor.main(["--check"]) == 0, "harbor/ is stale: run python tools/export_harbor.py"


def test_instruction_is_the_prompt_byte_for_byte():
    assert (PKG / "instruction.md").read_bytes() == BY_ID["incident"]["prompt"].encode("utf-8")


@pytest.mark.parametrize("copy, source", [
    ("environment/harness/hook.py", "artifacts/awsbench-turn/hook.py"),
    ("environment/harness/verify.py", "artifacts/t01-grade/verify.py"),
    ("tests/verify.py", "artifacts/t01-grade/verify.py"),
])
def test_hook_and_grader_are_byte_copies(copy, source):
    assert (PKG / copy).read_bytes() == (BUNDLE / source).read_bytes()


@pytest.mark.parametrize("server", ["aws", "workspace"])
def test_server_folders_are_byte_copies(server):
    src = BUNDLE / "envs" / server
    rules = export_harbor.dockerignore(src)
    want = {p.relative_to(src).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest() for p in src.rglob("*")
            if p.is_file() and (not export_harbor.ignored(p.relative_to(src).as_posix(), rules)
                                or p.name == ".dockerignore")}
    dst = PKG / "environment" / server
    got = {p.relative_to(dst).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest() for p in dst.rglob("*")
           if p.is_file()}
    assert got == want


def test_scoring_is_the_frameworks():
    from agent_env.task_step.task_steps.verifiers import scoring
    assert (PKG / "tests/scoring.py").read_bytes() == Path(scoring.__file__).read_bytes()


def test_compose_keeps_the_rendered_blocks():
    """gateway, aws, workspace and servicedb as the framework renders them, apart from the documented adaptations."""
    _, rendered, _ = export_harbor.render_compose(export_harbor.published("ghcr.io/scaleapi/agentenv-gateway"),
                                                  export_harbor.published("ghcr.io/scaleapi/agentenv-service-db"))
    text = (PKG / "environment/docker-compose.yaml").read_text()
    ours = yaml.safe_load(text)["services"]
    for name in ("aws", "workspace"):
        assert {k: v for k, v in ours[name].items() if k not in ("build", "image")} == \
               {k: v for k, v in rendered["services"][name].items() if k != "image"}
    gateway = dict(ours["gateway"])
    assert gateway.pop("entrypoint")[:2] == ["sh", "-c"] and gateway.pop("volumes") == ["ctl:/ctl"]
    want = dict(rendered["services"]["gateway"])
    want["environment"] = [e for e in want["environment"] if not e.startswith("MCP_SERVER_NAME=")]
    assert gateway == want
    servicedb = dict(ours["servicedb"])
    assert servicedb.pop("volumes") == ["./servicedb/init-schemas.sql:/docker-entrypoint-initdb.d/init-schemas.sql:ro"]
    assert servicedb == {k: v for k, v in rendered["services"]["servicedb"].items() if k != "volumes"}
    assert "ports" not in json.dumps(ours)
    assert ours["main"]["depends_on"]["harness"] == {"condition": "service_healthy"}
    assert set(ours["mcp"]["networks"]) == {"env-network", "default"}
    assert all(ours[s]["networks"] == ["env-network"] for s in ("gateway", "aws", "workspace", "servicedb", "harness"))


def test_gateway_wrapper_draws_the_server_name_as_agentenv_does():
    assert 'f"env{secrets.randbelow(10000):04d}"' in export_harbor.GATEWAY_WRAPPER
    assert "exec python -m gateway.entrypoint" in export_harbor.GATEWAY_WRAPPER


def test_setup_carries_the_task_values():
    setup = json.loads((PKG / "environment/harness/setup.json").read_text())
    steps = {s["id"]: s for s in setup["setup_steps"]}
    for sid in ("configure", "lock-all", "allow-sre", "allow-harness"):
        assert steps[sid] == BY_ID[sid]
    assert {k: v for k, v in steps["clock"].items() if k != "armed_virtual_time"} == BY_ID["clock"]
    assert steps["clock"]["armed_virtual_time"].startswith("2026-11-17T07:40:00")
    assert setup["horizon"] == BY_ID["horizon"]
    prompt = BY_ID["incident"]
    assert setup["hook"]["params"] == prompt["post_turn_hooks"][0]["params"]
    assert setup["prompt"]["sha256"] == hashlib.sha256(prompt["prompt"].encode()).hexdigest()
    for key in ("timeout_seconds", "max_conversation_turns", "max_turns"):
        assert setup["prompt"][key] == prompt[key]
    assert setup["prompt"]["builtin_tools"] == BY_ID["deploy-sre"]["builtin_tools"] == {"disable": "*"}
    assert setup["role"] == BY_ID["deploy-sre"]["role"]
    assert sorted(setup["sre_tools"]) == sorted(BY_ID["allow-sre"]["tools"])


def test_solution_paths_are_the_drivers():
    driver, _ = export_harbor.driver_module(ROOT)
    paths = json.loads((PKG / "solution/paths.json").read_text())
    for mode in export_harbor.SOLUTION_MODES:
        want = [[tool, args, check[0].__name__ if check else None] for tool, args, *check in driver.steps_for(mode)]
        assert paths["modes"][mode] == json.loads(json.dumps(want)), mode
    assert paths["modes"]["nothing"] == []


def test_task_toml_runs_the_grader_separately_with_the_judge_keys_from_the_host():
    import tomllib
    toml = tomllib.loads((PKG / "task.toml").read_text())
    assert toml["verifier"]["environment_mode"] == "separate"
    assert toml["verifier"]["env"] == {"LITELLM_API_KEY": "${LITELLM_API_KEY:-}",
                                       "LITELLM_BASE_URL": "${LITELLM_BASE_URL:-}"}
    assert toml["verifier"]["collect"][0]["service"] == "harness"
    assert toml["artifacts"][0] == {"source": "/export", "destination": "export", "service": "harness"}
    assert toml["agent"] == {"timeout_sec": BY_ID["deploy-sre"]["ttl_seconds"], "user": "agent"}


def test_shape_change_is_refused():
    steps = [dict(s) for s in TASK]
    steps[7] = {**steps[7], "system_prompt": "be brief"}
    with pytest.raises(export_harbor.ExportError):
        export_harbor.check_shape(steps)
    with pytest.raises(export_harbor.ExportError):
        export_harbor.check_shape(TASK[:-1])
