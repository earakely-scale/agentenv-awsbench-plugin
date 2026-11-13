---
license: cc-by-4.0
pretty_name: 'AwsIncidentsBench: AWS incident response on the AWS Emulator'
language:
- en
task_categories:
- reinforcement-learning
- text-generation
size_categories:
- n<1K
tags:
- agentenv
- rl-environment
- agents
- tool-use
- incident-response
- sre
- aws
- aws-emulator
- simulation
- trajectories
configs:
- config_name: runs
  default: true
  data_files:
  - split: t01
    path: tables/runs.jsonl
- config_name: calls
  data_files:
  - split: t01
    path: tables/calls.jsonl
- config_name: checks
  data_files:
  - split: t01
    path: tables/checks.jsonl
---

# AwsIncidentsBench

**An on-call agent works a payments outage against the real AWS API on AWS's open-source AWS Emulator, in a world with a
clock that every call moves, failing hosts and DNS caches, colleagues who push the wrong fix, and a fixed horizon at
which the end state is graded.**

- **The real AWS API.** AWS Emulator answers Route 53, DynamoDB, SSM, SQS, ECS, CloudWatch and CloudTrail; the agent
  drives it with the AWS CLI, plus fleet tools on simulated hosts and a workspace of chat, tickets, wiki, code, mail,
  pager and a public status page.
- **Time that each call costs.** The clock is frozen between actions; a point read costs 20 s of virtual time, a write
  45 s, a broad query 75 s, and `wait` lets time pass. Traffic, DNS negative caches, an internal DNS automation and a
  ledger service that collapses under a reconnect herd move with it, and colleagues post, page and email on a timeline.
- **Graded at a horizon.** After the agent says DONE the world is fast-forwarded to 05:10 and both servers' end state
  and the world's time series are graded: thirteen checks, four gates that cap the score, one D12 score.

It is the public face of AwsBench, a plugin for the [AgentEnv Framework](https://www.agentenvframework.com); no change
to AgentEnv is needed. Code: [agentenv-awsbench-plugin](https://github.com/earakely-scale/agentenv-awsbench-plugin)
(Apache-2.0). Article and viewers: the [AwsIncidentsBench Space](https://huggingface.co/spaces/earakely-scale/AwsIncidentsBench).

AwsIncidentsBench is not affiliated with or endorsed by Amazon Web Services. Vantleaf, its people and its systems are
fictional.

## Task T01, The Empty Ledger Endpoint

The agent is the on-call SRE for Vantleaf, a fictional payments company, paged at 02:40 Eastern on Tuesday, November
17, 2026: checkout success is about 4 %. At 02:31 a race in endpointd, the internal DNS automation, left
`ledger.internal` with no records, and every checkout host cached the NXDOMAIN answer for 15 minutes. The agent has to
find that, restore the right plan with the automation paused, wait out the caches, shed the reconnect herd before it
collapses ledger-writer, and run the incident process (declare, page the owners, keep the channel, ticket and status
page accurate, route a key merchant's email, leave a handoff note) while the payments lead pushes an unrelated rollback.
250 tool calls; the window and the horizon are 05:10 ET. A run passes when the six core checks (C1, C2, C3, C4, C6, C7)
pass and no cap applies. Task T03, "Thread Ceiling", will be added here.

## Results

<!-- results -->
| Run | Model | Build | D12 | Pass | Checks lost | Gates fired | Calls | DONE |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| `R5-opus` | Claude Opus 5.5 | `8b0f533` | 0.94 | pass | C11 | none | 128 | 04:52:38 |
| `R5-sonnet` | Claude Sonnet 5.5 | `8b0f533` | 1.00 | pass | none | none | 93 | 03:47:37 |
| `R4-opus` | Claude Opus 5.5 | `4f1813f` (dev) | 0.97 | pass | C9 | none | 145 | 05:10:00 |
| `R4-sonnet` | Claude Sonnet 5.5 | `4f1813f` (dev) | 0.91 | pass | C9, C11 | none | 115 | 04:31:44 |
| `R3-opus` | Claude Opus 5.5 | `4aa8a25` (dev) | 0.97 | pass | C9 | none | 115 | 04:22:26 |
| `R3-sonnet` | Claude Sonnet 5.5 | `4aa8a25` (dev) | 0.74 | fail | C2, C5, C9 | none | 115 | 04:20:48 |
| `R2-sonnet` | Claude Sonnet 5.5 | `e712bd7` (dev) | 0.40 | fail | C2, C6, C9 | G3 | 139 | 05:02:54 |
| `R1-sonnet` | Claude Sonnet 5.5 | `e712bd7` (dev) | 0.40 | fail | C5, C11 | G3 | 81 | 03:52:48 |
| `reference` | Scripted oracle | `8bc9c6a` | 1.00 | pass | none | none | 27 | 03:37:55 |
<!-- /results -->

Runs on older builds are **development runs**: they met env and grader bugs that the runs themselves found and that are
fixed since. The final runs will replace them. One run per model and build is an example, not a pass rate. Calls count
every charged call, `wait` included, as the meter counts them against the 250-call budget.

## Files

| Path | What |
| --- | --- |
| `tables/runs.jsonl` | The dataset viewer's `runs` table: one row per run and the reference, with its score, checks lost, gates fired, calls, DONE time, restore time, minutes collapsed, final message and handoff note |
| `tables/calls.jsonl` | The `calls` table: one row per charged call of every run, with its virtual start and end, tool, cost class, command or arguments, result, error, and whether it changed production |
| `tables/checks.jsonl` | The `checks` table: one row per check and gate of every run, with its points, whether it passed or fired, and the grader's evidence |
| `index.json` | One row per run: model, build, development flag, D12, pass, checks lost, gates fired, calls, DONE time; plus the reference |
| `runs/<run>/viewer.json` | The viewers' JSON (schema `awsincidentsbench.viewer/v1`, documented at the top of `tools/export_run.py`): every call with its virtual times, cost class, arguments and result; the world's series every 30 s; per-host DNS recovery; levers; the ticket and status-page history; events; the agent's messages; the grade's checks; the final message and the handoff note |
| `runs/<run>/aws-export.json` | The `aws` server's horizon export: `episode`, the `action_log` of every call and world write, the `meter`, the `world` (endpointd, DNS, ledger, traffic, timeline, approvals, ...), `aws_state` and `start_state` |
| `runs/<run>/workspace-export.json` | The `workspace` server's export: chat, tickets, wiki, repo, mail, pager and status records, each with its full history, and its action log |
| `runs/<run>/grade.json` | The grader's rows: D12 with its caps and buckets, every check and gate with its evidence |
| `runs/<run>/transcript.md` | A rendered view of the agent's session: the prompt, every tool call with its arguments and result, the agent's messages |
| `reference/viewer.json` | The scripted reference path in the same schema |

Runs are named `R<round>-<model>`; a round is one build of the environment.

## How the runs were made

Each run deploys the AwsBench bundle with AgentEnv on a Linux host, arms the gateway clock at 02:40 ET and prompts the
solver once with the page. The solver is Claude Code driving Claude Opus 5.5 or Claude Sonnet 5.5 through a model
endpoint, with its built-in tools off, so it acts only through the environment's `aws` and `workspace` MCP tools. A
post-turn hook lets up to ten minutes of virtual time pass between turns and wakes the agent with what arrived. The
episode ends at DONE, at 250 calls or at 05:10; the world is then fast-forwarded to the 05:10 horizon and graded by
`artifacts/t01-grade/verify.py`, whose C11 handoff judge is an LLM given a fixed fact list.

The reference is the scripted oracle path of `tests/e2e/driver.py`, replayed by `tools/build_hub_data.py` through the
world package in process with the unit tests' in-memory AWS and workspace fakes: the same physics, timeline and
colleagues, timed by the same meter. On a deployed env the same path grades D12 1.0; its check marks here are the ones
the path is built to pass, and its colleagues' support-desk tickets are left out because the fakes give them their own
ids.

## Caveats

- **Scrubbed.** Every file passed `tools/export_run.py`'s scrub and its check: no keys, model-endpoint or internal
  hostnames, local paths or endpoints, IP addresses outside the scenario's own fictional ones, or AWS account ids other
  than the emulator's `000000000000` (earlier builds stamped the AWS documentation's example account in world ARNs; it is
  rewritten). Loopback addresses in simulated host output read `localhost`.
- **Heuristics in the viewer JSON.** Which checks a call "bears on" is read from its tool and arguments, not from the
  grader; the grade's own evidence is in `grade.json`.
- **Calibration is open.** T01 is meant to sit near a 10–18 % frontier pass rate; on the latest build both models pass
  it, and the task is being tuned.
- **The judge is noisy.** C11 decides about six points.

## License

The task content, runs and viewer data are [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/). The environment's
code is Apache-2.0. AWS Emulator is AWS's own (CC0) and is not redistributed here.
