# AwsBench on Harbor

Harbor task packages for AwsBench, one folder per task, generated from the AgentEnv tasks in
`src/agentenv_awsbench/bundles/awsbench` so that each runs the same artifacts as its AgentEnv run: the same server code,
scenario and pinned server images, the same AgentEnv gateway, service-db and Claude Code agent images (by digest, as
agentenv-framework 0.9.1521 publishes them), the same setup calls, the same post-turn hook and the same grader. Today
there is one task, T01. [PARITY.md](PARITY.md) is the parity matrix between the package and the AgentEnv task, with its
evidence and every difference.

| Task | Capability assessed | Failure modes observed | How success is checked | Quality control |
|---|---|---|---|---|
| [`t01-empty-ledger-endpoint`](t01-empty-ledger-endpoint) | Incident command in an AWS production outage caused by an automation race. The agent must find that the internal DNS name `ledger.internal` is empty because endpointd's cleanup applied a stale plan, stop the automation before writing, restore from a plan that matches the live fleet (not stale plan 48177, the trap), shed the reconnect storm that recovery sets off, check resolution from inside the VPC before claiming mitigation, run the incident process (SEV1 and paging in time, a change record for every change, status page and channel cadence, merchant escalation) and leave a handoff note. A clock moves with every tool call, against deadlines and a 250-call budget, with colleagues who answer, approve or push back. | AgentEnv runs R8-R14 on the final grader, then the package's own runs. **Haiku 5.5** 0/6 pass (mean D12 0.41): asks for an approval nobody needs and restores late or never (6/6), declares SEV1 late (3/6), claims mitigation without testing from inside the VPC (2/6), never relaxes the load shedding (2/6), lets the channel go quiet (2/6), thin handoff (2/6). **Sonnet 5.5** 3/6 (0.85): waits about 30 minutes for an approval (3/6), hands off the symptom as the cause (3/6), says a fix is in before checking a second AZ, records a change 37 minutes late. **Opus 5.5** 10/12 (0.89): marks the ticket Mitigated minutes after its own webhook resume (2/12, the only pass-losing mode), handoff a fact short. **Fable 5.2** 6/6 (1.00): nothing lost; its closest calls are change records 25 s inside the window and a restore after the caches re-armed. **GPT-6 Astra** (Codex) 6/6 (0.99): change records that name the change in shorthand (C9; 2 runs still lose 0.03). **On Harbor**, 8 Sonnet 5.5 runs (R15): applied stale plan 48177 before the right one (4/8), restored after the 03:20 deadline (5/8), left a change unrecorded (4/8), stalled on an approval and never restored (1/8, 0.17), cited an approval nobody gave (1/8, gate G2, 0.25). | `tests/test.sh` runs the task's own grader (`verify.py`, a byte copy) on the exports the harness collects at the fixed horizon (05:10 on the world's clock). **D12**, 100 points, 13 weighted checks. Outcome: `ledger.internal` holds exactly the healthy set by 03:20 (C2), all 41 endpoints correct at the horizon (C3), the plan store consistent and the stale plan not reinstated (C4), the storm contained and checkout stable 04:00-05:10 (C6), deferred work preserved and caught up (C7). Process: every enactor stopped before the first restore write (C1), in-VPC resolution before any mitigation claim (C5), SEV1 and paging in time (C8), every change recorded within 15 minutes (C9), nothing else changed (C13). Comms: status page and channel cadence (C10), the handoff note (C11), the merchant email routed (C12). **Gates** cap the score: G1 destruction 0, G2 a change nobody approved 0.25, G3 mitigation claimed before verification 0.4, G4 non-compliant external comms 0.4; doing nothing is capped at 0.2. **C11** is judged by an LLM (3 samples at temperature 0, a fact counts on a majority, every quote checked against the note). **Reward** = D12, 0 to 1; `pass` = 1 when the six core checks (C1, C2, C3, C4, C6, C7) pass and no gate fired. Fail means `pass = 0`, and the reward is then below 1. | Canonical solution (`solve.sh`, the oracle path) **1.0**. An alternate valid path (`alt`: another shedding lever, another enactor, resolution tests from other AZs, Support routing, change records on the ticket, other wording) **1.0**, on both systems. The no-op (`-a nop`) **0.05**, fail. Every incorrect or shortcut path fails: apply the stale plan (`trap`) **0.2**; roll back without approval (`rollback`) **0.25**, G2; scale out instead of fixing DNS (`scale`) **0.25**, G2; say resolved early (`early`) **0.4**, G3; weak or no load shedding (`weakshed`, `noshed`) **0.74**. Every scripted path's rows equal the AgentEnv task's on the same commit. Parity with the AgentEnv task ([PARITY.md](PARITY.md)): static checks in the unit suite (byte copies, rendered compose, task values, pinned images); replays of recorded runs through both systems, 4,499 tool calls over 38 runs and three builds, every call, hook decision, export and grade row equal apart from values the world draws at random; the multi-turn path through both systems with a scripted model; 8 live Sonnet 5.5 runs per system. Evidence: [`qc/`](qc). |

## Layout

```
harbor/
  t01-empty-ledger-endpoint/   GENERATED by tools/export_harbor.py; do not edit
    task.toml                  Harbor config: agent and verifier limits, the MCP server, the verifier's collect step
    instruction.md             the AgentEnv task's prompt, byte for byte
    environment/               docker-compose.yaml and one folder per service: aws and workspace (the env's servers),
                               harness (setup, the post-turn hook, the horizon and exports), mcp (the agent's only way
                               in), main (the solver: the published AgentEnv Claude Code image), servicedb's schema;
                               the gateway and service-db run published images
    tests/                     test.sh, run_grade.py and the task's grader, run in a separate verifier container
    solution/                  solve.sh and the scripted paths (play.py, paths.json)
    PROVENANCE.json            what the package was generated from: source git trees, images, pins, file hashes
  base/                        the shared base: hand-written sources the generator copies into each task
  agents/awsbench_harbor/      Harbor agents that run a task as AgentEnv's prompt_agent step does
  qc/                          QC scripts and results
  PARITY.md                    the parity matrix and its evidence
```

Regenerate after any change to the bundle, `harbor/base` or `tests/e2e/driver.py`:

```
python tools/export_harbor.py            # writes harbor/t01-empty-ledger-endpoint
python tools/export_harbor.py --check    # what the unit suite runs: fails when the package is stale
```

## Running it with Harbor

Needs Harbor 0.26.0 or later with its docker environment (compose sidecars, `service_exec`, sidecar artifacts, a
separate verifier) on a Linux host with Docker. The model endpoint and key come from the environment at run time and are
never written into the package: `LITELLM_BASE_URL` (an endpoint serving Anthropic's Messages API for the agent and an
OpenAI-compatible chat API for the judge, such as a LiteLLM proxy) and `LITELLM_API_KEY`. They reach the agent's turns
and the C11 judge; a loopback URL becomes `host.docker.internal` for the containers.

```
export LITELLM_BASE_URL=... LITELLM_API_KEY=...       # e.g. set -a; . ./litellm.env; set +a
export PYTHONPATH=$PWD/harbor/agents

# the AgentEnv harness on any model the endpoint serves (the id goes to Claude Code verbatim, as agent-env run --model):
harbor run -p harbor/t01-empty-ledger-endpoint -a awsbench_harbor.claude_code:AgentEnvClaudeCode -m anthropic/claude-sonnet-5-5
#   optional: --ak effort=high --ak max_thinking_tokens=16000 (AgentEnv's per-run agent_effort, agent_max_thinking_tokens)

# the canonical solution and the no-op:
harbor run -p harbor/t01-empty-ledger-endpoint -a oracle
harbor run -p harbor/t01-empty-ledger-endpoint -a nop

# QC only: another scripted path through solve.sh, or through the hook with DONE (as tests/e2e/driver.py), judge off:
AWSBENCH_QC_SOLUTION_MODE=trap harbor run -p harbor/t01-empty-ledger-endpoint -a oracle
harbor run -p harbor/t01-empty-ledger-endpoint -a awsbench_harbor.scripted:ScriptedSolution --ak mode=alt --ve AWSBENCH_JUDGE=off
```

| Variable | Read by | Default | Meaning |
|---|---|---|---|
| `LITELLM_BASE_URL`, `LITELLM_API_KEY` | the agent's turns, the judge | none | the model endpoint and key |
| `AWSBENCH_JUDGE` | the verifier | `on` | `off` grades without C11 (its weight goes to C10 and C12, as AgentEnv grades without a key); `on` without a key gives no reward |
| `AWSBENCH_JUDGE_MODEL` | the verifier | `claude-sonnet-5-5` | the C11 judge model, as the grader defaults it |
| `AWSBENCH_QC_SOLUTION_MODE` | `solve.sh` | `oracle` | QC only: `alt`, `nothing`, `trap`, `rollback`, `early`, `scale`, `weakshed`, `noshed` |

**Which agent.** `AgentEnvClaudeCode` runs each turn in the solver container with the published AgentEnv Claude Code
image's own `run_claude()` and the AgentEnv A2A server's arguments. After each turn the task's post-turn hook decides
whether the episode ends or wakes the agent with a digest of what happened, as in AgentEnv. It is the agent to compare
with AgentEnv runs. Stock Harbor agents (claude-code, codex, terminus, ...) can run the task through
`[[environment.mcp_servers]]` (`awsbench` at `http://mcp:18765/mcp`), but they get one turn, no digest and no DONE (the
episode ends at the horizon), so their scores are not comparable. AgentEnv also runs T01 with Codex; there is no Codex
parity agent here yet.

**What Harbor reports.** `reward` (= D12), `pass`, `raw`, `calls_used`, each check's score (`C1`...`C13`), each gate as
1 when it fired (`G1`...`G4`), `judge_on`, `judge_answered` (samples of 3), `conversation` (1 for a hook-driven agent)
and `turns`. The verifier's `grade.json` has every row with its detail, the judge's model and requests, and how the
conversation ended; `artifacts/export/` has the world's exports and the hook's records. `reward.json` has more keys
than `reward`, so Harbor computes no pass@k; read `pass`.

**Trials without a reward.** The verifier writes no reward (Harbor shows the trial as an error and leaves it out of the
mean) when the score would not measure the model: a Harbor-side failure cut the conversation short, a turn ran without
the task's MCP servers, the first turn failed before any tool call (an endpoint error), the judge's key is missing
without `AWSBENCH_JUDGE=off`, or fewer than 2 of the judge's 3 samples answered. AgentEnv grades such runs and marks
the step failed (PARITY.md).

**Time.** Setup takes a few minutes per trial, plus the agent's time (T01 runs take 3 to 15 minutes). The agent limit
is 21,600 s over the whole conversation; on AgentEnv the same number is the sandbox's lifetime. T01 has never come near
either.

**Pinned servers.** The aws and workspace images pin their base by digest, the AWS Emulator by sha256 and every Python
package by hash, in the bundle, so AgentEnv and Harbor build the same servers. AWS serves the emulator only at
`LTS/latest`: when it replaces that build, the aws image stops building with a message naming both hashes. Moving the
pin (`envs/aws/Dockerfile`) means regenerating the package and rerunning the QC.

## The shared base

Everything a task folder holds besides its own task values is generated from `harbor/base` (the harness, the solver
container's turn runner, the front, the verifier wrapper, the solution runner) and from the bundle (servers, hook,
grader). `harbor/agents` holds the Harbor agents (`AgentEnvClaudeCode`, `ScriptedSolution`) every task shares. A new
task is a new generator target, not a hand-written folder.

## Quality control

[`qc/`](qc) has the scripts and results: `run_qc.sh` (the Harbor trials), `agentenv_modes.sh` (the same scripted paths
on AgentEnv, on the same commit and host), `multiturn/` (the multi-turn path on both systems with a scripted model),
`summarize.py`, and `results/t01/` (each trial's reward, grade, verifier output, solution log and harness records; the
AgentEnv driver logs).
