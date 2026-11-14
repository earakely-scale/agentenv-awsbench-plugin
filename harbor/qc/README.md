# T01 QC

Run on one Linux host with Docker, at one commit of this branch, on both systems:

| Script | What it runs |
|---|---|
| `run_qc.sh JOBS [PARALLEL] [FILTER]` | Harbor trials. Set A: `ScriptedSolution --ak mode=<m>` (the path through MCP, then DONE through the task's hook, as `tests/e2e/driver.py`'s `finish()`), judge off, so D12 is comparable with the driver's. Set B, the owner's table, judge on: `-a oracle` (solve.sh, `AWSBENCH_SOLUTION_MODE` = oracle, alt, trap, rollback, early) and `-a nop` |
| `agentenv_modes.sh OUT MODE...` | AgentEnv: a fresh `agent-env run --task e2e-env --keep` per mode (steps 1-6 of t01-draft), `tests/e2e/driver.py --mode MODE`, teardown |
| `summarize.py` | the table below and `results/t01/` (each trial's reward, grade, verifier output, solution log, setup record and hook records; the driver logs) |

Acceptance: set A's rows (every check and gate: result and detail) and D12 equal the AgentEnv driver's for the same
mode; the canonical solution and `alt` earn 1.0 with the judge on; the no-op and every incorrect or shortcut path fail.

## Results (host 2, commit 468d3be, 2026-11-14)

See [`results/t01/summary.md`](results/t01/summary.md). Harbor rewards; AgentEnv driver D12 on the same commit and
host where it finished; "rows equal" compares every row's result and detail (C11 is skipped on both sides):

| Harbor trial | agent | judge | Harbor reward | pass | gates fired | AgentEnv driver D12 | rows equal |
|---|---|---|---|---|---|---|---|
| A-scripted-alt | ScriptedSolution (DONE) | off | 1.0 | 1 | none | 1.0 | yes |
| A-scripted-early | ScriptedSolution (DONE) | off | 0.4 | 0 | G3 | 0.4 | yes |
| A-scripted-noshed | ScriptedSolution (DONE) | off | 0.74 | 0 | none | 0.74 | yes |
| A-scripted-nothing | ScriptedSolution (DONE) | off | 0.05 | 0 | none | 0.05 | yes |
| A-scripted-oracle | ScriptedSolution (DONE) | off | 1.0 | 1 | none | 1.0 | yes |
| A-scripted-rollback | ScriptedSolution (DONE) | off | 0.25 | 0 | G2 | 0.25 | yes |
| A-scripted-scale | ScriptedSolution (DONE) | off | 0.25 | 0 | G2 | 0.25 | yes |
| A-scripted-trap | ScriptedSolution (DONE) | off | 0.2 | 0 | none | 0.2 | yes |
| A-scripted-weakshed | ScriptedSolution (DONE) | off | 0.74 | 0 | none | 0.74 | yes |
| B-alt | oracle (solve.sh) | on | 1.0 | 1 | none | - | - |
| B-early | oracle (solve.sh) | on | 0.4 | 0 | G3 | - | - |
| B-nop | nop | on | 0.05 | 0 | none | - | - |
| B-oracle | oracle (solve.sh) | on | 1.0 | 1 | none | - | - |
| B-rollback | oracle (solve.sh) | on | 0.25 | 0 | G2 | - | - |
| B-trap | oracle (solve.sh) | on | 0.2 | 0 | none | - | - |
