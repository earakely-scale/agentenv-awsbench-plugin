import sys
from pathlib import Path

BUNDLE = Path(__file__).resolve().parents[1] / "src" / "agentenv_awsbench" / "bundles" / "awsbench"
for env in ("aws", "workspace"):
    sys.path.insert(0, str(BUNDLE / "envs" / env))
