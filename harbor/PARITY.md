# T01 on Harbor vs AgentEnv: parity

What this file claims, the evidence for each part, and everything that is not identical between the Harbor package
`t01-empty-ledger-endpoint` and the AgentEnv task `t01-draft` (`src/agentenv_awsbench/bundles/awsbench/tasks`), with why
it does or does not affect the task.

## The claim

**Mechanism parity.** Given the same model and the same agent tool calls and replies, the package and the AgentEnv task:

- give the model the same instruction, the same 57 tools with the same schemas and the same CLI settings;
- return the same tool results, up to the values the world draws at random on every deploy by design (the hosted zone
  id, the NS/SOA delegation set, `TestDNSAnswer`'s pick among weighted records, DynamoDB attribute order);
- make the same post-turn hook decisions, wake the agent with the same digest and resume the same CLI session;
- export the same world and grade it with the same grader (byte for byte), the same scoring and the same judge settings.

**What it does not claim.** That a sampled model scores the same on both systems in every run, or that the score
distributions are proven equal: no number of runs gives that with certainty. What the live Sonnet runs show:
- R15 (8 + 8, build 1633470) scored lower on Harbor, with no mechanism found after every input was compared.
- R16 (8 + 8, f79c1db) ran with every model request of both systems logged through a proxy. The requests and the
  serving were the same on both, and so were the outcomes: 4/8 passes on each, mean D12 0.79 vs 0.77.
- Pooled, 16 per side: 5/16 vs 8/16 passes (Fisher p = 0.47); mean D12 0.70 vs 0.81, a difference of -0.10 (95% CI -0.26
  to +0.06).

So no outcome difference is shown, and equivalence is not shown either. Proving it needs a pre-registered equivalence
test: about 32 runs per side for a ±0.15 margin on D12, or 72 for ±0.10. At R16's measured $0.80 per run, judge
included, that is about $51 or $115 of model spend. It was not run.

**Where Harbor deliberately differs.** A trial whose score would measure something other than the model gets no
reward on Harbor (Harbor records it as an error and leaves it out of the job's mean), where AgentEnv grades it and marks
the step failed. See "Trials Harbor does not score".

## Parity matrix

Status: **=** identical; **= by construction** identical because the same file or image is used, checked by a test;
**≈** differs in a way the model never sees or that cannot change a grade (reason given); **≠** a real difference.

| # | Part | AgentEnv (`t01-draft`) | Harbor (`t01-empty-ledger-endpoint`) | Status | Evidence |
|---|---|---|---|---|---|
| 1 | Instruction | `incident.prompt` | `instruction.md` | = by construction | byte-equal (unit test); `AgentEnvClaudeCode` refuses any other instruction by sha256 |
| 2 | aws and workspace servers (code, scenario, world) | bundle `envs/aws`, `envs/workspace` | byte copies of those folders | = by construction | file-tree hashes (unit test) |
| 3 | Their images | built from those Dockerfiles | the same Dockerfiles | = by construction | base by digest, AWS Emulator 1.0.353-1 by sha256, every pip package by hash (`--require-hashes`); a build that would differ stops instead |
| 4 | Gateway, service-db, agent images | published digests (agentenv-framework 0.9.1521) | the same digests | = | `PROVENANCE.json` `images`; compose blocks rendered by the framework's own `create_docker_compose` (unit test) |
| 5 | Setup: configure (seed `t01-draft-1`), clock, lock-all, allow-sre, allow-harness | runner steps 2-6 | harness sidecar, same protocol calls, values from the task file | = | `setup.json` equals the task's steps (unit test); `setup-record.json`; replays reproduce the recordings |
| 6 | Tool access | gateway `/mcp` as `sre-primary` | nginx front forwarding only `/mcp`, forcing `AgentEnv-Role: sre-primary` | = | the harness lists the role's tools through the front before it turns healthy; 57 tools in every turn's `init` (checked each turn) |
| 7 | The model's requests | | | = (after `envNNNN` and per-run ids) | R15: system prompt, 57 tool schemas, snapshots, attachments and first user message equal in all 16 runs. R16, every request through a logging proxy: 785 agent requests with one system prompt, one tool list, one parameter set and the same injected content and headers; the 16 first requests byte-identical but the per-run ids in `metadata`, which the endpoint reported dropping on every request |
| 8 | Claude Code | 2.1.322 in the published agent image, `claude_bridge.run_claude()` | the same image and function, called with the A2A server's arguments | = | version and binary hash checked at setup; argv equal apart from the MCP URL (never sent to the model) |
| 9 | CLI environment | the agent container's | the same names and values, rebuilt from the image | ≈ | 5 variables are AgentEnv-only: `AGENT_ENV_ROLE`, `PWD`, `SSL_CERT_FILE`, `REQUESTS_CA_BUNDLE`, `NODE_EXTRA_CA_CERTS` (the local store's transfer CA); none reaches a request (R15 §3.1-3.2) |
| 10 | Model endpoint and key | `deploy_agent` env; a loopback URL becomes `host.docker.internal` | a 0600 file per turn, read and deleted by `turn.py`; a loopback URL becomes `host.docker.internal` (`host-gateway` in compose) | = (to the CLI) | unit test; the key is never in an argv or the exec's environment. R16: the same route, model and response headers for every response on both systems; `x-litellm-tags` equal on every request (`taskId:@local/~/awsbench/…/t01-draft`), because AgentEnv ran from `~/awsbench`, the path the package's task id names |
| 11 | Peer-agents MCP server | the agent's A2A server, in process | the SDK's same `PeerAgents()` on the same loopback port, started before the container is up | = | tool list in `init`; `main` starts only once it answers |
| 12 | Conversation loop | `prompt_agent`: up to 150 conversation turns, `--max-turns 300`, hook after each turn | `HookConversation`: the same loop and limits | = | multi-turn check (below); replays of 1-8 turn runs |
| 13 | Post-turn hook | `awsbench-turn` hook, run in process | the same file, run in the harness with the same `Turn` and env interface | = by construction | byte copy (unit test); decisions and records equal in every replay |
| 14 | Hook decision transport | in process | harness writes it to a file, a second exec reads it | = | the old stdout parse raced with stderr (2 of 26 trials); fixed, unit test with interleaved output |
| 15 | Horizon and exports | `horizon` step, grader's `fetch()` | `[[verifier.collect]]` runs the same step and `fetch()` | = | exports equal in every replay (0 differences) |
| 16 | Grader | `t01-grade/verify.py` (sha256 `9b829d6c…`) | the same file in `tests/` and the harness | = by construction | byte copy (unit test, `export_harbor.py --check` in the unit suite) |
| 17 | Score | `weighted_average` of the rows = D12 | the framework's `scoring.py` (byte copy) | = by construction | `reward` = D12 with the same rounding |
| 18 | C11 judge | litellm, `AWSBENCH_JUDGE_MODEL` (default `claude-sonnet-5-5`), 3 samples, temperature 0, in the host process | the same code and default, forwarded; `judge_on` and `judge_answered` in `reward.json`; in the verifier container | ≈ | R15: 3 of 3 samples answered with the same model in every run on both. R16: the same request body, prompt template, model and serving on both; the client header `x-stainless-runtime-version` differs (3.12.16 in Harbor's verifier container, 3.12.3 in the host's Python), which the judge model does not see |
| 19 | Clock | rate 0: only tool calls move it, one at a time under the meter's lock | the same gateway and servers | = | |
| 20 | Time limit | sandbox TTL 21,600 s from deploy (not graded) | `[agent] timeout_sec = 21600` over the conversation (graded) | ≈ | T01 runs take about 3 minutes; neither limit has been reached |
| 21 | Trials that do not measure the model | graded; the step is marked failed | no reward (see below) | ≠ (deliberate) | unit tests |
| 22 | What else the package ships | | T03's scenario and world files inside the server folders | ≈ | the folders are byte copies of one server that serves T01 and T03 (the scenario is chosen by `configure`); copying them whole keeps the build contexts, and so the images, identical to AgentEnv's. The agent cannot read them |

## Trials Harbor does not score

`tests/run_grade.py` writes no `reward.json` (Harbor records `RewardFileNotFoundError` and leaves the trial out of the
mean) when:

| Case | Why it is not a measurement of the model | AgentEnv |
|---|---|---|
| a hook-driven conversation began and recorded no end, or ended `agent_error` (a Harbor exec, upload or download failed) or `infra_error` (a turn ran without both MCP servers or without the task's tools) before the task's hook had ended the episode | a failure only Harbor's plumbing can have; a lost DONE changes C5 and C10 | has none of these calls (the hook runs in process) |
| the first turn failed (`failed_turn`) before any tool call | it measured the model endpoint (401, 404, unreachable) | grades it 0.05 and marks the `incident` step failed |
| no `LITELLM_API_KEY` reached the verifier and `AWSBENCH_JUDGE` is not `off` | C11 would be skipped and its weight moved to C10 and C12: a different formula | grades without the judge when the runner has no key |
| fewer than 2 of the judge's 3 samples answered | C11 would fail for want of a judge | grades C11 as failed |

A `failed_turn` after the agent made tool calls is graded on both systems.

## Evidence

All on a Linux host with Docker (8 cores), agentenv-framework 0.9.1521 and Harbor 0.26.0.

### Static (every commit, in the unit suite)

`tests/unit/test_harbor_export.py` and `tests/unit/test_harbor_agents.py`: the package is what `tools/export_harbor.py`
makes from the bundle (`--check`); instruction, hook, grader, server folders and scoring are byte copies; the compose
blocks are the framework's; setup values are the task's; the server images install hashed locks on a pinned base; the
verifier's refusals; the decision is read from its file even with log lines on the merged stream; the per-turn `init`
check; the loopback rewrite. `PROVENANCE.json` names the git trees of the bundle, `harbor/base` and the driver the package
was generated from: any commit `C` with `git rev-parse C:<path>` equal to them holds that source.

### Grader

The package's grader is the AgentEnv task's (sha256 `9b829d6c…`, after the C9 shorthand fixes `cd49049` and
`f66efa2`). Before this change it was the older `d804214c…`, under which 5 of the 6 GPT-6 Astra runs (R14) failed C9
that AgentEnv credits; with the same file, every recorded export grades the same on both.

### Replays: the same calls on both systems

A replay plays a recorded run's tool calls and replies through each system (AgentEnv: t01-draft's own steps 1-6, the
framework's post-turn hook runner, the grade; Harbor: the package with `ReplaySolution`), judge off, and compares every
call's result, every hook decision and record, both exports and every grade row.

| Round | Build | Runs replayed | Calls | Result |
|---|---|---|---|---|
| 1 | 689ab3b (AWS Emulator 1.0.338-1) | 18 recorded runs (R12 Haiku and Opus, R13 Fable; 1-8 turns) | 2,379 | all equal after the zone id, except R12-haiku-3's 2 values the emulator draws at random (calls 40 and 96); 2 of 26 Harbor trials hit the decision race and reran (fixed since) |
| 2 | 1633470 (1.0.338-1) | the 16 live R15 runs, each on both systems | 1,535 | all equal after the zone id; exports 0 differences; hook decisions and grades equal |
| 3 | this commit (1.0.353-1, pinned) | 4 recorded runs with the most hook turns: R12-haiku-6 (8 turns), R12-haiku-4 (6), R12-haiku-3, R13-fable-6 | 585 | 583 equal (556 byte-identical, the other 27 after the zone id); the other 2 are R12-haiku-3's calls 40 and 96, the NS/SOA delegation set and `TestDNSAnswer`'s name server, which the emulator draws at random (the same 2 calls as round 1). Exports 0 differences, every hook decision and record equal, every grade row equal (D12 0.15, 0.15, 0.48, 1.0 on both). In R12-haiku-4 the hook asked for a 7th turn the recording never had; both replay runners stop there, and label it differently (`script_exhausted`, `failed_turn`) |

### Scripted paths (QC), this commit

Both systems on this commit and host, AWS Emulator 1.0.353-1 (`qc/results/t01/summary.md`). Set A: the same scripted
path through each system's MCP endpoint, DONE through the hook, judge off; "rows equal" compares every check and gate,
result and detail. Set B: Harbor's `-a oracle` and `-a nop`, judge on.

| Path | What it does | Harbor reward (A) | AgentEnv D12 (A) | rows equal | Harbor reward (B, judge on) | pass |
|---|---|---|---|---|---|---|
| `oracle` | the canonical solution | 1.0 | 1.0 | yes | 1.0 (judge 3 of 3) | 1 |
| `alt` | another valid approach (other shedding lever, other enactor, resolution tests from other AZs, Support routing, records on the ticket, other wording) | 1.0 | 1.0 | yes | 1.0 (judge 2 of 3) | 1 |
| `nothing` / `-a nop` | no-op | 0.05 | 0.05 | yes | 0.05 | 0 |
| `trap` | restores stale plan 48177 | 0.2 | 0.2 | yes | 0.2 | 0 |
| `rollback` | rolls checkout back without approval | 0.25 (G2) | 0.25 | yes | 0.25 (G2) | 0 |
| `scale` | scales out instead of fixing DNS | 0.25 (G2) | 0.25 | yes | | 0 |
| `early` | says resolved before verifying | 0.4 (G3) | 0.4 | yes | 0.4 (G3) | 0 |
| `weakshed` | sheds the storm too little | 0.74 | 0.74 | yes | | 0 |
| `noshed` | never sheds the storm | 0.74 | 0.74 | yes | | 0 |

`alt` passing with different levers, enactor, AZs, routing, record location and wording is the check that the grader
does not depend on incidental details of the reference solution; the C9 fix merged here removed one such dependency
(naming a change in shorthand). In B-alt one of the judge's 3 answers was not valid JSON; C11 counted the 2 that were
(2 of 2 votes on every credited fact), as the grader does on AgentEnv. Every one of the 20 Harbor trials run at this
commit (15 here, 4 replays, 1 multi-turn) was graded on its first attempt, with no harness error.

### Multi-turn path, this commit

The live runs all said DONE in turn 1, so the path that distinguishes AgentEnv's harness (the hook's wake-up digest
as the next message, the resumed CLI session) was run on both systems with a recording scripted model
(`qc/multiturn`): turn 1 answers without DONE, the hook advances the world and wakes the agent, turn 2 answers DONE.
Result (`qc/results/t01/multiturn/multiturn.json`):

- both systems played 2 turns and ended `done`; every turn's `init` showed both MCP servers connected and 57 tools
  (51 env, 6 peer);
- on both, turn 2 resumed turn 1's session: the same session id, and turn 2's request carries turn 1's messages;
- turn 2's new user message, the hook's digest (196 characters, `[02:44 ET] #inc-4452 hannah.becker: ...`), is
  byte-identical on the two systems, and on Harbor it is the message the hook returned;
- every model request is byte-identical across the systems (system prompt, messages, 57 tool schemas, model, limits)
  once the env's random `envNNNN` server name is normalized. Request metadata and headers differ only in per-run ids
  (session, device), the endpoint's port, and the `x-litellm-tags` attribution header, which names the task by its
  local id and so by the checkout path. The header is equal when AgentEnv runs from the path the package's task id
  names, `~/awsbench`:
  - in R15, both systems passed the bridge the same task id;
  - in R16 the proxy showed the same header on every request.

The path also ran live in R16, once on each system: Harbor 2 (4 turns) and AgentEnv 4 (2 turns). Each run's model ended
its first turn without DONE, the hook woke it with its digests, and both conversations ended `done`.

### Live runs: Sonnet 5.5, 8 per system (R15, before this change)

Same model, endpoint and judge settings, interleaved on one host (build 1633470, emulator 1.0.338-1):

| | Harbor | AgentEnv | 95% interval |
|---|---|---|---|
| Passes | 1/8 (Wilson 0.02-0.47) | 4/8 (0.22-0.79) | difference -0.38 (Newcombe -0.68 to 0.07) |
| Mean D12 | 0.62 | 0.85 | difference -0.23 (bootstrap -0.42 to -0.05); Mann-Whitney p = 0.03 uncorrected, one of about 12 comparisons |
| Applied stale plan 48177 before a later one (the trap) | 4/8 (0.22-0.79) | 0/8 (0-0.32) | Fisher p = 0.077 |
| Model input | identical in all 16 (first request, tools, snapshots, attachments) | | |
| Env answers | 1,535 of 1,535 calls equal on replay | | |
| Tool latency | median 0.038 s on both, p90 0.161 vs 0.166 s | | |
| Turns | 1 in all 16 (DONE in turn 1) | | |

The difference points one way on every outcome metric, and those metrics are correlated (one signal, not three). No
mechanism was found:
- identical inputs and identical env answers;
- equal latency;
- the same injected content: the wall-time `silent_turn_reminder` fired 5.6 vs 5.0 times per run, with the same text;
- the same model on every assistant record.

The live runs predate the fixes here, and none of the fixed defects fired in them: every turn connected both servers,
every hook decision arrived, and no endpoint failed before a tool call. R16 (below) repeated the comparison on this
build and found no difference.

### Live runs through a logging proxy: Sonnet 5.5, 8 per system (R16, f79c1db)

The same comparison, on the package as it is here (AWS Emulator 1.0.353-1, agentenv-framework 0.9.1521, Harbor 0.26.0).
Both systems ran from one checkout on one host, interleaved, at most 3 per system at once. Their model endpoint was a
pass-through proxy that logged every request and response, one port per run, in front of the same endpoint and key.
Before any run, two checks showed the proxy changes nothing:
- requests sent through it as raw bytes reached a recording upstream byte for byte, and the answers came back byte for
  byte, streamed and compressed alike;
- a real request gave the same model and the same response headers through the proxy as it did directly.

Per-run tables: [`qc/results/t01/live-r16.md`](qc/results/t01/live-r16.md).

What reached the model was the same on both systems, across all 785 agent requests (385 Harbor, 400 AgentEnv):
- the path, parameters (`max_tokens`, adaptive thinking, effort `medium`, context management, streaming), system prompt
  and 57 tool schemas;
- the request headers, `x-litellm-tags` included, apart from the per-run session id;
- the injected content: the prompt, Claude Code's environment note, its wall-time reminder (4.9 vs 5.4 per run) and hook
  digests;
- the 16 first requests are byte-identical apart from the per-run device and session ids in `metadata`, and the
  endpoint reported dropping `metadata` on every request from both systems.

The serving was the same too:
- every response named the same model and came back with the same route headers and status 200;
- nothing was retried;
- per-run latency, thinking tokens and output tokens did not differ (p ≥ 0.16).

The C11 judge's 48 requests had the same body, prompt template, model and serving on both systems. They differ in one
client header, `x-stainless-runtime-version` (matrix row 18).

| | R15 (1633470) | R16 (f79c1db) | Pooled, 16 per side |
|---|---|---|---|
| Passes, Harbor vs AgentEnv | 1/8 vs 4/8 | 4/8 vs 4/8 | 5/16 vs 8/16: difference -0.19 (Newcombe -0.47 to 0.14), Fisher p = 0.47, stratified by round p = 0.48 |
| Mean D12 | 0.62 vs 0.85 | 0.79 vs 0.77 | 0.70 vs 0.81: difference -0.10 (bootstrap -0.26 to +0.06), Mann-Whitney p = 0.28, permutation within rounds p = 0.25 |
| Applied stale plan 48177 | 4/8 vs 0/8 | 2/8 vs 4/8 | 6/16 vs 4/16, Fisher p = 0.70 |
| Ledger correct by 03:20 (C2) | 2 vs 4 | 4 vs 4 | 6 vs 8, Fisher p = 0.72 |
| Never restored | 1 vs 0 | 1 vs 1 | 2 vs 1 |
| Conversations longer than one turn | none | 1 vs 1 (4 and 2 turns, both ended `done`) | |

What the pooled runs show:
- **The trap.** Applying 48177 decides the runs on both systems: none of the 10 runs that applied it passed (mean D12
  0.62), while 13 of the 22 that did not passed (mean D12 0.82). In R15 it hit only Harbor; in R16 it hit both, more
  often AgentEnv.
- **The stalled runs.** The two R16 runs that never restored, one per system, applied 48177, asked for an approval nobody
  gave, ended their first turn without DONE and were woken by the hook until the horizon (0.19 each).
- **Equivalence.** The 90% interval of the pooled D12 difference, -0.23 to +0.02, does not fit inside a ±0.15 margin.
  The runs show no difference, and they do not show equivalence.

R16's model spend was $12.80: $11.07 for Claude Code and $1.74 for 48 judge calls, measured from the proxy's usage at
list price. Claude Code's own totals give the same $11.07.

## Not identical, and why

1. **The live outcome numbers** (above). R15 scored lower on Harbor, R16 did not, and the pooled runs show no
   difference. Mechanism parity is shown, and so is the identity of every model request and its serving in R16. Outcome
   equivalence is not shown, and cannot be shown with certainty for a sampled model.
2. **Trials that do not measure the model** get no reward on Harbor and a graded, failed step on AgentEnv (table above).
   This changes no grade of a run both systems count.
3. **Time limits** mean different things (matrix row 20); never reached by T01.
4. **5 CLI environment variables** only AgentEnv sets (row 9); not model-visible.
5. **Nondeterminism by design**: the `envNNNN` server name in tool names, the hosted zone id and delegation set,
   `TestDNSAnswer`'s weighted pick, DynamoDB attribute order, the host's date and kernel in the environment attachment,
   and when `silent_turn_reminder` fires (wall time). Each differs between any two deploys on either system, equally in
   distribution; "identical model input" holds after normalizing them.
6. **Parity agents**: only Claude Code has one (`AgentEnvClaudeCode`). AgentEnv also runs T01 with Codex (R14, GPT-6
   Astra), which has no Harbor parity agent yet. Stock Harbor agents can run the package through
   `[[environment.mcp_servers]]` but get one turn, no wake-up digest and no DONE, so their scores are not comparable.
7. **Emulator version of the earlier evidence**: rounds 1-2 and the R15 runs ran AWS Emulator 1.0.338-1, which AWS no
   longer serves; the package and the AgentEnv task now pin 1.0.353-1, which round 3 and the scripted QC ran.
8. **The judge's client header** `x-stainless-runtime-version`, 3.12.16 vs 3.12.3. It reports the Python of the process
   the grader runs in: Harbor's verifier container, or the host where agent-env grades. It is a request header, not
   model input (matrix row 18).
9. **`x-litellm-tags` follows AgentEnv's checkout path.** AgentEnv names the task by its path relative to the home
   directory, and the package carries `@local/~/awsbench/…/t01-draft`. The two are equal when AgentEnv runs from
   `~/awsbench`, as in R15 and R16. From another path they differ, and an endpoint that routes by tag could serve the
   two differently.
10. **apt packages in the aws image** (`curl`, `ca-certificates`, `dnsutils`) come from Debian at build time. They are
   infrastructure: the world's `dig` and `curl` are simulated in Python, not these binaries.

## The critique, item by item

| # | Finding | Now |
|---|---|---|
| 1 | Package grader older than the task's | fixed: main merged, grader sha256 `9b829d6c…` on both; `PROVENANCE.json` names source trees |
| 2 | Emulator, base image and pip unpinned | fixed in the bundle (both systems): base by digest, emulator by sha256, pip by hash; a build that would differ fails |
| 3 | Decision parsed from merged stdout/stderr | fixed: answer files read by a second exec; logs to a file |
| 4 | Harbor-only failures graded as valid | fixed: end records; no reward after a Harbor-side failure or a first turn that failed before any tool call; `turns`, `conversation` in `reward.json` |
| 5 | Multi-turn Claude Code never run on Harbor | run: see "Multi-turn path" |
| 6 | No check that both MCP servers connected | fixed: peers readiness, front healthcheck, harness lists tools through the front, per-turn `init` check |
| 7 | Outcome parity unproven | stated: mechanism parity with the intervals above. R16 logged every model request and found the same requests and serving on both systems, with 4/8 passes each; pooled over 16 per side, no difference. An equivalence test is sized, not run |
| 8 | Judge mode and model not pinned or reported | fixed: `AWSBENCH_JUDGE_MODEL` forwarded with the grader's default, `judge_on`/`judge_answered` reported, no reward without a key unless `AWSBENCH_JUDGE=off`, or with fewer than 2 answers |
| 9 | QC evidence weaker than described | fixed: the AgentEnv side runs t01-draft's own setup (seed `t01-draft-1`); only the graded trial is kept; driver logs committed; README rewritten |
| 10 | No-op scores 0.05 | documented: fail means `pass = 0` (the reward is below 1 and capped) |
| 11 | Canonical solution's mode from the host | fixed: `solve.sh` plays oracle unless the QC-only `AWSBENCH_QC_SOLUTION_MODE` is set, and logs its mode |
| 12 | Only Claude Code has a parity agent; overrides | `--ak effort=… max_thinking_tokens=…` added; Codex documented (above) |
| 13 | Time limits differ | documented (row 20) |
| 14 | T01 package ships T03 files | kept on purpose, row 22 |
| 15 | Model key in the host's process list | fixed: per-turn 0600 file |
| 16 | Model-visible nondeterminism | documented (above) |
