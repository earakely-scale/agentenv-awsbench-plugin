# T01 QC

Everything here runs on one Linux host with Docker, Harbor 0.26.0 and agentenv-framework 0.9.1521, at one commit of
this repo, on both systems. Model keys come from the environment (`LITELLM_BASE_URL`, `LITELLM_API_KEY`, read from a file
outside the repo with `set -a`), never from the repo.

| Script | What it runs |
|---|---|
| `run_qc.sh JOBS [PARALLEL] [FILTER]` | Harbor trials. Set A: `ScriptedSolution --ak mode=<m>` (a scripted path through the env's MCP tools, then DONE through the task's hook, as `tests/e2e/driver.py`'s `finish()`), judge off (`AWSBENCH_JUDGE=off`), so D12 is comparable with the driver's. Set B, the owner's table, judge on: `-a oracle` (`solve.sh`; `AWSBENCH_QC_SOLUTION_MODE` = oracle, alt, trap, rollback, early) and `-a nop` |
| `agentenv_modes.sh OUT MODE...` | AgentEnv: per mode, `agent-env run --task t01-setup --keep` (t01-draft.json's own steps 1-6, seed `t01-draft-1`), `tests/e2e/driver.py --mode MODE` against it (the same path, DONE, the horizon, the grade without the judge), teardown |
| `replay/run.sh SCRIPTS OUT RUN...` | a recorded run's tool calls and replies through both systems (AgentEnv: `replay_agentenv.py` with the framework's own post-turn hook runner; Harbor: `ReplaySolution`), then `replay/compare.py`: every tool result, hook decision and record, both exports, every grade row. `replay/extract.py` makes the scripts from a run's Claude Code session |
| `multiturn/run.sh OUT` | one AgentEnv `t01-draft` run and one Harbor `AgentEnvClaudeCode` trial against `multiturn/recorder.py`, a recording scripted model (turn 1 without DONE, so the hook wakes the agent; turn 2 DONE); `multiturn/compare.py` checks both resumed the session and diffs every model request |
| `summarize.py` | `results/t01/summary.md` and `summary.json`; copies each job's graded trial and the AgentEnv driver logs |

Acceptance: set A's rows (every check and gate, result and detail) and D12 equal the AgentEnv driver's for the same
mode; the canonical solution and `alt` earn 1.0 with the judge on; the no-op and every incorrect or shortcut path fail
(`pass = 0`); replays equal on both systems up to the ids the world draws at random; the multi-turn requests equal.

## Results

Host 2, 2026-11-14, this commit (AWS Emulator 1.0.353-1, pinned). [`results/t01/summary.md`](results/t01/summary.md) is the
scripted table; each job's graded trial is under [`results/t01/trials/`](results/t01/trials) and the AgentEnv driver logs under
[`results/t01/agentenv/`](results/t01/agentenv).

| Harbor trial | agent | judge | Harbor reward | pass | gates fired | AgentEnv driver D12 | rows equal |
|---|---|---|---|---|---|---|---|
| A-scripted-oracle | ScriptedSolution (DONE) | off | 1.0 | 1 | none | 1.0 | yes |
| A-scripted-alt | ScriptedSolution (DONE) | off | 1.0 | 1 | none | 1.0 | yes |
| A-scripted-nothing | ScriptedSolution (DONE) | off | 0.05 | 0 | none | 0.05 | yes |
| A-scripted-trap | ScriptedSolution (DONE) | off | 0.2 | 0 | none | 0.2 | yes |
| A-scripted-rollback | ScriptedSolution (DONE) | off | 0.25 | 0 | G2 | 0.25 | yes |
| A-scripted-scale | ScriptedSolution (DONE) | off | 0.25 | 0 | G2 | 0.25 | yes |
| A-scripted-early | ScriptedSolution (DONE) | off | 0.4 | 0 | G3 | 0.4 | yes |
| A-scripted-weakshed | ScriptedSolution (DONE) | off | 0.74 | 0 | none | 0.74 | yes |
| A-scripted-noshed | ScriptedSolution (DONE) | off | 0.74 | 0 | none | 0.74 | yes |
| B-oracle | oracle (solve.sh) | on | 1.0 | 1 | none | - | - |
| B-alt | oracle (solve.sh) | on | 1.0 | 1 | none | - | - |
| B-nop | nop | on | 0.05 | 0 | none | - | - |
| B-trap | oracle (solve.sh) | on | 0.2 | 0 | none | - | - |
| B-rollback | oracle (solve.sh) | on | 0.25 | 0 | G2 | - | - |
| B-early | oracle (solve.sh) | on | 0.4 | 0 | G3 | - | - |

**Replays** ([`results/t01/replay/`](results/t01/replay)), this commit: R12-haiku-6 (8 hook turns), R12-haiku-4 (6),
R12-haiku-3 and R13-fable-6, 585 calls through both systems: 583 equal (556 byte-identical, 27 after the hosted zone
id), 2 differ by values the emulator draws at random (R12-haiku-3 calls 40 and 96); exports 0 differences; hook
decisions and records equal; grade rows equal (D12 0.15, 0.15, 0.48, 1.0 on both). Earlier rounds (PARITY.md): 18
recorded runs, 2,379 calls (689ab3b), and the 16 live R15 runs, 1,535 calls (1633470).

**Multi-turn** ([`results/t01/multiturn/`](results/t01/multiturn)): with the recording scripted model, both systems
played 2 turns, resumed the same session for turn 2, sent the hook's digest byte for byte, and made byte-identical model
requests once the env's random server name is normalized.

**Live** ([`results/t01/live-r15.md`](results/t01/live-r15.md)): 8 Sonnet 5.5 runs per system before these fixes;
identical model input and env answers; Harbor 1/8 pass, mean D12 0.62; AgentEnv 4/8, 0.85 (PARITY.md has the
intervals and what they do and do not show).

20 Harbor trials ran at this commit (15 QC, 4 replays, 1 multi-turn) with no harness error and no trial left
ungraded.
