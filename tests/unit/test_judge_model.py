"""Every task grader in the bundle asks the same judge model by default, so the eval's judged checks are scored by one
model; AWSBENCH_JUDGE_MODEL overrides it for all of them, and the Harbor package forwards the same default."""
import importlib.util
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
ARTIFACTS = ROOT / "src/agentenv_awsbench/bundles/awsbench/artifacts"
DEFAULT = re.compile(r'os\.environ\.get\("AWSBENCH_JUDGE_MODEL", "([^"]+)"\)')
spec = importlib.util.spec_from_file_location("export_harbor", ROOT / "tools/export_harbor.py")
export_harbor = importlib.util.module_from_spec(spec)
spec.loader.exec_module(export_harbor)


def test_every_task_grader_defaults_to_one_judge_model():
    defaults = {grader.parent.name: DEFAULT.findall(grader.read_text())
                for grader in sorted(ARTIFACTS.glob("*-grade/verify.py"))}
    assert {"t01-grade", "t03-grade"} <= set(defaults)
    assert all(len(found) == 1 for found in defaults.values()), defaults
    assert {found[0] for found in defaults.values()} == {export_harbor.JUDGE_MODEL}, defaults
