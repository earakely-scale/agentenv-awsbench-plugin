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
- config_name: t01-runs
  default: true
  data_files:
  - split: train
    path: tables/t01/runs.jsonl
- config_name: t01-calls
  data_files:
  - split: train
    path: tables/t01/calls.jsonl
- config_name: t01-checks
  data_files:
  - split: train
    path: tables/t01/checks.jsonl
- config_name: t03-runs
  data_files:
  - split: train
    path: tables/t03/runs.jsonl
- config_name: t03-calls
  data_files:
  - split: train
    path: tables/t03/calls.jsonl
- config_name: t03-checks
  data_files:
  - split: train
    path: tables/t03/checks.jsonl
- config_name: t04-runs
  data_files:
  - split: train
    path: tables/t04/runs.jsonl
- config_name: t04-calls
  data_files:
  - split: train
    path: tables/t04/calls.jsonl
- config_name: t04-checks
  data_files:
  - split: train
    path: tables/t04/checks.jsonl
- config_name: t05-runs
  data_files:
  - split: train
    path: tables/t05/runs.jsonl
- config_name: t05-calls
  data_files:
  - split: train
    path: tables/t05/calls.jsonl
- config_name: t05-checks
  data_files:
  - split: train
    path: tables/t05/checks.jsonl
- config_name: pareto
  data_files:
  - split: train
    path: pareto/pareto.csv
---

# AwsIncidentsBench

**An on-call agent works a payments outage against the real AWS API on AWS's open-source AWS Emulator, in a world with a
clock that every call moves, failing hosts, caches and fleets, colleagues who push the wrong fix, and a fixed horizon at
which the end state is graded. Four tasks so far, each modelled on a public AWS incident: T01 "The Empty Ledger
Endpoint", T03 "Thread Ceiling", T04 "Purged Routes" and T05 "Capacity Glob Cold Start".**

- **The real AWS API.** AWS Emulator answers Route 53, DynamoDB, SSM, SQS, ECS, CloudWatch and CloudTrail; the agent
  drives it with the AWS CLI, plus fleet tools on simulated hosts and a workspace of chat, tickets, wiki, code, mail,
  pager and a public status page. A task's world can also answer AWS calls the emulator doesn't model (T03's Auto
  Scaling group and telemetry, T04's table restores).
- **Time that each call costs.** The clock is frozen between actions; a point read costs 20 s of virtual time, a write
  45 s, a broad query 75 s, and `wait` lets time pass. The task's physics moves with it (T01: traffic, DNS negative
  caches, an internal DNS automation, a ledger service that collapses under a reconnect herd; T03: a fleet of
  stream-routing hosts with a thread ceiling, an Auto Scaling group with scheduled actions, a shared bootstrap service,
  ledger cells and a settlement run; T04: a routing control plane whose scheduled workflows rewrite purged routes, a
  data plane that loads them, point-in-time restores; T05: an object store's index and placement subsystems with
  floors, cold starts and a long integrity check, a scheduled capacity command, and a status page that depends on the
  store), and colleagues post, page and email on a timeline.
- **Graded at a horizon.** When the agent stops, the world is fast-forwarded to the task's horizon and both servers'
  end state and the world's time series are graded: per-task checks, gates that cap the score, one D12 score.

It is the public face of AwsBench, a plugin for the [AgentEnv Framework](https://www.agentenvframework.com); no change
to AgentEnv is needed. Code: [agentenv-awsbench-plugin](https://github.com/earakely-scale/agentenv-awsbench-plugin)
(Apache-2.0). Article and viewers: the [AwsIncidentsBench Space](https://huggingface.co/spaces/earakely-scale/AwsIncidentsBench).

AwsIncidentsBench is not affiliated with or endorsed by Amazon Web Services. Vantleaf, its people and its systems are
fictional.

## Tasks

Each task has a card in `tasks/<task>/`, generated from the bundle by `tools/build_task_cards.py`: the incident and the
world, the colleagues and what each approves or declines, the exact prompt, the tools, the time horizon and budget,
every check, gate and cap with its points and rule (read from the grader, with the judges' fact lists), what a fair
failure looks like, how to run it with AgentEnv from the GitHub bundle, and the build commit. `task.json` beside each
card holds the same content, machine-readable.

<!-- tasks -->
| Task | Title | Start | Window | Horizon | Calls | Checks | Gates | Card |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| T01 | The Empty Ledger Endpoint | 02:40 ET 2026-11-17 | 05:10 | 05:10 | 250 | 13 | 4 | [tasks/T01/README.md](tasks/T01/README.md) |
| T03 | Thread Ceiling | 08:15 ET 2026-11-16 | 13:00 | 16:15 | 220 | 14 | 6 | [tasks/T03/README.md](tasks/T03/README.md) |
| T04 | Purged Routes | 13:12 ET 2026-11-21 | 16:12 | 16:12 | 200 | 12 | 7 | [tasks/T04/README.md](tasks/T04/README.md) |
| T05 | Capacity Glob Cold Start | 10:30 ET 2026-11-20 | 14:00 | 14:00 | 220 | 12 | 6 | [tasks/T05/README.md](tasks/T05/README.md) |
<!-- /tasks -->

### Task T01, The Empty Ledger Endpoint

The agent is the on-call SRE for Vantleaf, a fictional payments company, paged at 02:40 Eastern on Tuesday, November
17, 2026: checkout success is about 4 %. At 02:31 a race in endpointd, the internal DNS automation, left
`ledger.internal` with no records, and every checkout host cached the NXDOMAIN answer for 15 minutes. The agent has to
find that, restore the right plan with the automation paused, wait out the caches, shed the reconnect herd before it
collapses ledger-writer, and run the incident process (declare, page the owners, keep the channel, ticket and status
page accurate, route a key merchant's email, leave a handoff note) while the payments lead pushes an unrelated rollback.
250 tool calls; the window and the horizon are 05:10 ET. A run passes when the six core checks (C1, C2, C3, C4, C6, C7)
pass and no cap applies. Its current runs are on the hardened build: R11, six Claude Opus 5.5 runs on `5fe025c`, and
R12 to R14 on `689ab3b`, six each of Claude Haiku 5.5, Claude Opus 5.5, Claude Fable 5.2 and GPT-6 Astra, and R15 and
R16, eight Claude Sonnet 5.5 runs each on `1633470` and `f79c1db`, slightly later builds of the same task.

### Task T03, Thread Ceiling

The agent is again Vantleaf's primary on-call SRE, paged at 08:15 Eastern on Monday, November 16, 2026: the ledger
end-to-end canary says synthetic payment events are no longer reaching the ledger. At 06:05 a pre-approved capacity
change grew streamroute, the fleet that routes payment events to the ledger, from 120 to 132 hosts; each host keeps two
threads per peer under a 1,024-thread limit, and as the hosts learned their new peers their shard-map builds failed and
routing stopped. The agent has to find that, get the fleet routing and the ledger caught up before settlement, keep
the cause removed through the horizon, and run the incident process and communications, under change control and with
colleagues pressing their own fixes. 220 tool calls; the window closes at 13:00 ET and the world is graded at 16:15. A
run passes when the core outcome checks (C02 to C06) and the routing check (C15) pass and no cap applies. Four
rounds of T03 runs are here; the current one, T03R4, is six Claude Opus 5.5 runs on the hardened build `5fe025c`.

### Task T04, Purged Routes

Modelled on the Amazon ELB service event of December 24, 2012 ([post-event summary](https://aws.amazon.com/message/680587/)).
The agent is Vantleaf's weekend on-call SRE, paged at 13:12 Eastern on Saturday, November 21, 2026: card payments fail
for a growing number of merchants with `ROUTE_NO_HEALTHY_CELL`. On Friday evening a maintenance job meant for staging
ran against production and tombstoned 371 merchants' routes in the routing control plane's DynamoDB table; the scaler
and rebalancer workflows rebuild purged routes with no cells and publish them, breaking five or six more merchants per
tick. The agent has to stop the spread, restore the table to just before the purge into a new table, merge back exactly
the purged keys (including the ones the workflows rewrote) while keeping Saturday's legitimate changes, publish, verify
and resume the workflows through a canary, with approvals, a status page, an approved merchant update and a handoff,
while the payments lead pushes an API rollback that makes it worse. About 200 tool calls; the window and the horizon
are 16:12 ET. A run passes when C1 earns full credit, the 371 routes are restored exactly (C2), C3, C4 and C5 pass and
no gate fires. Two rounds are here: T04R1 (three development runs on `971d2bd`) and T04R2, nine runs on the final build
`9b8347a` (five Claude Opus 5.5, two Claude Sonnet 5.5, two Claude Haiku 5.5).

### Task T05, Capacity Glob Cold Start

Modelled on the Amazon S3 service disruption of February 28, 2017 ([post-event summary](https://aws.amazon.com/message/41926/)).
The agent takes over as Vantleaf's on-call SRE at 10:30 Eastern on Friday, November 20, 2026, the first day of the peak
change freeze. At 10:24 a playbook removal, `capctl remove --pool vs-b* --select slowest --count 4`, matched three pools
of vaultstore, Vantleaf's object store, and took 12 hosts instead of 4; the index and placement subsystems fell below
their floors into protective halt, and a second run of the same glob is scheduled for 11:40. The agent has to cancel
that run, re-admit the right hosts, cold-start index before placement and wait out a 104-minute integrity check, get a
public notice up through the status page's S3 fallback, finish the ticket's last removal by host name before a 14:00
lock without taking the billing batch's coordinators, and leave a handover in #sre. 220 tool calls; the shift and the
horizon end at 14:00 ET. A run passes when C01, C02, C03, C05 and C07 pass and no gate fires. Two rounds are here: T05R1
(three development runs on `6bd999c`) and T05R2, six runs on the final build `c9392b8` (four Claude Opus 5.5, one Claude
Sonnet 5.5, one Claude Haiku 5.5).

## Results

The bar the task owner set: over the Claude Opus 5.5 runs on the current build, pass@1 below 90 %, with every failure
fair (one the agent could have avoided with what the world showed it). The current build is the hardened one, which
removed what earlier builds gave away beyond each task's design: policy pages that restated the grade, tool output that
named the answer, colleagues who volunteered the fix, and a prompt that primed the tools. It is `5fe025c` for the R11
and T03R4 runs and `689ab3b`, which adds the grader and colleague fixes R11 found, for T01's R12 to R14; T04's and
T05's final builds are `9b8347a` and `c9392b8`. Under the final grader (`f66efa2` for T01; `758e202` for T03,
`689ab3b`'s grader with its judge on Claude Sonnet 5.5, T01's judge model; `98b9dcd` for T04; `b4eb0d9` for T05; every
judge is Claude Sonnet 5.5):

| Task | Opus runs | Pass@1 as graded | Pass@1, final grader | Failures | Bar |
| --- | --- | --- | --- | --- | --- |
| T01 | 12 (R11 + R12) | 9/12 | **10/12 (83 %)** | `R11-opus-2` and `R12-opus-5`, both G3, fair | met |
| T03 | 6 (T03R4) | 5/6 | **6/6 (100 %)** | none | not met: no run fails |
| T04 | 5 (T04R2) | 4/5 | **4/5 (80 %)** | `T04R2-opus-5`, C5 and C9, fair | met, thinly (4/6 with T04R1) |
| T05 | 4 (T05R2) | 4/4 | **4/4 (100 %)** | none | not met: no run fails (5/5 with T05R1) |

`R11-opus-2` and `R12-opus-5` each declared Mitigated while checkout was still below the incident process's bar of
99 % in every minute for 5 minutes, with the impact metrics there to read. `R11-opus-4` (G2) and `T03R4-opus-2` (G4)
were capped by grader defects, fixed since; the final grader passes both. Getting a fair T03 failure would mean adding
difficulty back, which this round ruled out. `T04R2-opus-5` repaired all 371 routes, then read the incident process
as reserving the workflows' resume for a data owner nobody could name, refused the incident commander's approval and
left them paused; five Opus runs are few, and the design expected 15–25 %. Every T05 Opus run passes; they lose C09
(the cancelled schedule's owner told 16 to 44 minutes later, where the design's check wants 15) and C10 (no separate
follow-up issue), both partly the environment's. The notes under each table say why each run lost what it lost.

### T01 across models

T01 on its final build, by model, under the final grader. Claude Opus 5.5 counts every run on the hardened build (R11
and R12). GPT-6 Astra ran in the Codex CLI agent at the API's default medium effort (Codex 0.147.0 has no metadata for
the model, so it sends no effort of its own); it has six runs, at about $12 each. Claude Sonnet 5.5's sixteen runs
ran on slightly later builds than the other models' `689ab3b`: R15 on `1633470`, whose environment is the bundle at
`3d5b896`, which adds the R12 and R13 quality-check fixes to colleagues, metering, the DONE hook, world logs and tool
fidelity, and R16 on `f79c1db`, the same environment with its servers' dependencies pinned. The incident, the checks
and the grader are the same. Every failure is fair; `R12-haiku-2` also loses part
of C2 to a colleague defect fixed since, but its unrecovered webhook backlog (C7) fails it on its own. Sonnet's eight
failures each restored the ledger endpoint after the 03:20 deadline, or never. In R15 each found a plan that matched
the live fleet, asked for a recorded approval to apply it, and applied it 3 to 14 minutes late although the rotation
IC kept answering that the runbook step was its own; in R16 each first applied the stale plan 48177 and restored the
endpoint 14 to 35 minutes late, or never (`R16-sonnet-ae-4`).

<!-- models -->
| Model | Agent | Runs | Pass@1 | Mean D12 | Lost most | Failures |
| --- | --- | --- | --- | --- | --- | --- |
| Claude Haiku 5.5 | Claude Code | 6 (R12) | **0/6 (0 %)** | 0.410 | C2 ×6, C6 ×5, C5 ×4 | `R12-haiku-1` fair, `R12-haiku-2` fair, `R12-haiku-3` fair, `R12-haiku-4` fair, `R12-haiku-5` fair, `R12-haiku-6` fair |
| Claude Sonnet 5.5 | Claude Code, builds 1633470, f79c1db | 16 (R15 + R16) | **8/16 (50 %)** | 0.807 | C11 ×13, C2 ×8, C9 ×5 | `R15-sonnet-ae-1` fair, `R15-sonnet-ae-2` fair, `R15-sonnet-ae-5` fair, `R15-sonnet-ae-8` fair, `R16-sonnet-ae-1` fair, `R16-sonnet-ae-3` fair, `R16-sonnet-ae-4` fair, `R16-sonnet-ae-8` fair |
| Claude Opus 5.5 | Claude Code | 12 (R11 + R12) | **10/12 (83 %)** | 0.888 | C11 ×2, G3 ×2, C5 ×1 | `R11-opus-2` fair, `R12-opus-5` fair |
| Claude Fable 5.2 | Claude Code | 6 (R13) | **6/6 (100 %)** | 1.000 | nothing | none |
| GPT-6 Astra | Codex CLI, effort medium | 6 (R14) | **6/6 (100 %)** | 1.000 | nothing | none |
<!-- /models -->

**On Harbor.** T01 also ships as a Harbor task package, in the [`harbor/`](https://github.com/earakely-scale/agentenv-awsbench-plugin/tree/main/harbor) folder of the
task's repository, which runs the same artifacts: the same server code, scenario and pinned images, the same setup
calls, post-turn hook and grader. Its [PARITY.md](https://github.com/earakely-scale/agentenv-awsbench-plugin/blob/main/harbor/PARITY.md) shows the mechanism is the same:
the model's input is identical; 4,499 recorded tool calls replayed through both systems return equal results, apart
from values the world draws at random by design; the exports, hook decisions and grades are identical; and all nine
scripted paths score the same on both, check for check. Claude Sonnet 5.5 ran eight times per system in each of two live rounds,
R15 on `1633470` and R16 on `f79c1db`, the same task and grader. R15 did not score the same (Harbor passed 1 of 8 and
AgentEnv 4 of 8, mean D12 0.62 against 0.85); R16 did (4 of 8 each, 0.79 against 0.77). In R16 a logging proxy saw
every model request of both systems: across all 785 agent requests nothing reached or served the model differently
(the same body parameters, system prompt and tools, the same headers apart from per-run ids, and the same serving),
and the first request of all sixteen runs was byte-identical apart from per-run ids, which the endpoint drops before
the model. Pooled, 16 runs a side, Harbor passed 5 and AgentEnv 8 (Fisher p = 0.47), with mean D12 0.70 against 0.81
(difference −0.10, 95 % interval −0.26 to +0.06; Mann-Whitney p = 0.28); 6 against 4 runs applied the stale plan
48177, and none of the ten that did passed. The R15 gap did not persist and is consistent with sampling noise. Equal
outcomes are still not proven: showing the mean D12 equal within ±0.15 would take about 32 runs a side. The Harbor runs
are parity evidence and are not in the table above.

### T04 and T05 by model

Every T04 and T05 run, both rounds, by model, under each task's final grader. The first rounds ran on older builds whose
colleague and grader defects are fixed since; their failures are labelled like the rest.

**T04, Purged Routes**

<!-- models-t04 -->
| Model | Agent | Runs | Pass@1 | Mean D12 | Lost most | Failures |
| --- | --- | --- | --- | --- | --- | --- |
| Claude Haiku 5.5 | Claude Code | 2 (T04R2) | **0/2 (0 %)** | 0.545 | C2 ×2, C4 ×2, C5 ×2 | `T04R2-haiku-1` fair, `T04R2-haiku-2` fair |
| Claude Sonnet 5.5 | Claude Code | 4 (T04R1 + T04R2) | **2/4 (50 %)** | 0.934 | C5 ×2, C8 ×2, C7 ×1 | `T04R1-sonnet-2` environment, `T04R2-sonnet-2` fair |
| Claude Opus 5.5 | Claude Code | 6 (T04R1 + T04R2) | **4/6 (67 %)** | 0.950 | C5 ×2, C7 ×2, C12 ×2 | `T04R1-opus-1` environment, `T04R2-opus-5` fair |
<!-- /models-t04 -->

**T05, Capacity Glob Cold Start**

<!-- models-t05 -->
| Model | Agent | Runs | Pass@1 | Mean D12 | Lost most | Failures |
| --- | --- | --- | --- | --- | --- | --- |
| Claude Haiku 5.5 | Claude Code | 1 (T05R2) | **0/1 (0 %)** | 0.200 | C02 ×1, C05 ×1, C06 ×1 | `T05R2-haiku-1` fair |
| Claude Sonnet 5.5 | Claude Code | 3 (T05R1 + T05R2) | **2/3 (67 %)** | 0.650 | C09 ×3, C10 ×3, C12 ×1 | `T05R1-sonnet-2` fair |
| Claude Opus 5.5 | Claude Code | 5 (T05R1 + T05R2) | **5/5 (100 %)** | 0.900 | C09 ×5, C10 ×5 | none |
<!-- /models-t05 -->

## Cost and score

Each task's cost against its score: one point per run the final grader graded (development runs included), x the
solver's model spend for the run in US dollars (Claude Code's `total_cost_usd`, or for GPT-6 Astra the list price of its
token usage in the Codex CLI; the judge's calls are not counted), y the final D12. A larger mark sits at each model's
mean cost and mean D12, and the dashed line is the Pareto frontier over the means. The cost axis is logarithmic when a
task's costs span more than 10×. Made by `tools/pareto.py` from `hub/runs.json` and `hub/run_costs.json`.

![T01: final D12 against cost per run, by model](pareto/pareto-t01.png)

<!-- pareto -->
| Task | Model | Runs | Pass@1 | Mean cost | Cost range | Mean D12 | On the frontier |
| --- | --- | --- | --- | --- | --- | --- | --- |
| T01 | Claude Haiku 5.5 | 6 | 0/6 | $0.35 | $0.14–$0.53 | 0.410 | yes |
| T01 | Claude Sonnet 5.5 | 20 | 11/20 | $0.67 | $0.51–$0.88 | 0.824 | yes |
| T01 | Claude Opus 5.5 | 18 | 16/18 | $1.93 | $1.63–$2.34 | 0.925 | yes |
| T01 | Claude Fable 5.2 | 6 | 6/6 | $3.77 | $3.06–$5.77 | 1.000 | yes |
| T01 | GPT-6 Astra | 6 | 6/6 | $12.19 | $9.01–$13.64 | 1.000 | no |
| T03 | Claude Sonnet 5.5 | 6 | 4/6 | $1.06 | $0.75–$1.26 | 0.787 | yes |
| T03 | Claude Opus 5.5 | 15 | 14/15 | $3.23 | $2.22–$5.13 | 0.941 | yes |
| T04 | Claude Haiku 5.5 | 2 | 0/2 | $0.32 | $0.28–$0.36 | 0.545 | yes |
| T04 | Claude Sonnet 5.5 | 4 | 2/4 | $0.77 | $0.67–$0.90 | 0.934 | yes |
| T04 | Claude Opus 5.5 | 6 | 4/6 | $2.54 | $1.94–$3.30 | 0.950 | yes |
| T05 | Claude Haiku 5.5 | 1 | 0/1 | $0.41 | $0.41–$0.41 | 0.200 | yes |
| T05 | Claude Sonnet 5.5 | 3 | 2/3 | $1.50 | $1.43–$1.56 | 0.650 | yes |
| T05 | Claude Opus 5.5 | 5 | 5/5 | $3.73 | $2.97–$4.33 | 0.900 | yes |

Plots: [T01](pareto/pareto-t01.png) ([dark](pareto/pareto-t01-dark.png)) · [T03](pareto/pareto-t03.png) ([dark](pareto/pareto-t03-dark.png)) · [T04](pareto/pareto-t04.png) ([dark](pareto/pareto-t04-dark.png)) · [T05](pareto/pareto-t05.png) ([dark](pareto/pareto-t05-dark.png)). Every point: [`pareto/pareto.csv`](pareto/pareto.csv).
<!-- /pareto -->

### All runs

<!-- results -->
**T01 · The Empty Ledger Endpoint**

| Run | Model | Build | D12 graded | Pass | Checks lost | Gates fired | D12 final | Calls | End |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| `R16-sonnet-ae-1` | Claude Sonnet 5.5 | `f79c1db` | 0.74 | fail | C2, C11 | none | 0.74 | 111 | DONE 04:13:20 |
| `R16-sonnet-ae-2` | Claude Sonnet 5.5 | `f79c1db` | 0.97 | pass | C9 | none | 0.97 | 88 | DONE 03:52:54 |
| `R16-sonnet-ae-3` | Claude Sonnet 5.5 | `f79c1db` | 0.71 | fail | C2, C9, C11 | none | 0.71 | 118 | DONE 04:31:26 |
| `R16-sonnet-ae-4` | Claude Sonnet 5.5 | `f79c1db` | 0.19 | fail | C2, C3, C4, C5, C6, C7, C10, C11 | none | 0.19 | 112 | DONE 05:10:00 |
| `R16-sonnet-ae-5` | Claude Sonnet 5.5 | `f79c1db` | 0.94 | pass | C11 | none | 0.94 | 98 | DONE 03:50:29 |
| `R16-sonnet-ae-6` | Claude Sonnet 5.5 | `f79c1db` | 0.94 | pass | C11 | none | 0.94 | 80 | DONE 03:45:49 |
| `R16-sonnet-ae-7` | Claude Sonnet 5.5 | `f79c1db` | 0.91 | pass | C9, C11 | none | 0.91 | 105 | DONE 03:53:58 |
| `R16-sonnet-ae-8` | Claude Sonnet 5.5 | `f79c1db` | 0.74 | fail | C2, C11 | none | 0.74 | 112 | DONE 04:24:23 |
| `R15-sonnet-ae-1` | Claude Sonnet 5.5 | `1633470` | 0.80 | fail | C2 | none | 0.80 | 110 | DONE 04:31:10 |
| `R15-sonnet-ae-2` | Claude Sonnet 5.5 | `1633470` | 0.71 | fail | C2, C9, C11 | none | 0.71 | 99 | DONE 04:16:30 |
| `R15-sonnet-ae-3` | Claude Sonnet 5.5 | `1633470` | 0.94 | pass | C11 | none | 0.94 | 84 | DONE 03:53:59 |
| `R15-sonnet-ae-4` | Claude Sonnet 5.5 | `1633470` | 1.00 | pass | none | none | 1.00 | 88 | DONE 03:53:54 |
| `R15-sonnet-ae-5` | Claude Sonnet 5.5 | `1633470` | 0.74 | fail | C2, C11 | none | 0.74 | 92 | DONE 04:14:36 |
| `R15-sonnet-ae-6` | Claude Sonnet 5.5 | `1633470` | 0.91 | pass | C9, C11 | none | 0.91 | 90 | DONE 03:54:04 |
| `R15-sonnet-ae-7` | Claude Sonnet 5.5 | `1633470` | 0.94 | pass | C11 | none | 0.94 | 83 | DONE 03:51:34 |
| `R15-sonnet-ae-8` | Claude Sonnet 5.5 | `1633470` | 0.74 | fail | C2, C11 | none | 0.74 | 102 | DONE 04:17:59 |
| `R14-astra-1` | GPT-6 Astra | `689ab3b` | 0.97 | pass | C9 | none | **1.00** (was 0.97) | 139 | DONE 04:16:45 |
| `R14-astra-2` | GPT-6 Astra | `689ab3b` | 0.97 | pass | C9 | none | **1.00** (was 0.97) | 124 | DONE 03:55:25 |
| `R14-astra-3` | GPT-6 Astra | `689ab3b` | 0.97 | pass | C9 | none | **1.00** (was 0.97) | 143 | DONE 04:12:25 |
| `R14-astra-4` | GPT-6 Astra | `689ab3b` | 1.00 | pass | none | none | 1.00 | 115 | DONE 03:46:30 |
| `R14-astra-5` | GPT-6 Astra | `689ab3b` | 0.97 | pass | C9 | none | **1.00** (was 0.97) | 136 | DONE 04:01:25 |
| `R14-astra-6` | GPT-6 Astra | `689ab3b` | 0.97 | pass | C9 | none | **1.00** (was 0.97) | 138 | DONE 04:02:35 |
| `R13-fable-1` | Claude Fable 5.2 | `689ab3b` | 1.00 | pass | none | none | 1.00 | 152 | DONE 04:19:14 |
| `R13-fable-2` | Claude Fable 5.2 | `689ab3b` | 1.00 | pass | none | none | 1.00 | 143 | DONE 04:43:44 |
| `R13-fable-3` | Claude Fable 5.2 | `689ab3b` | 1.00 | pass | none | none | 1.00 | 127 | DONE 03:55:19 |
| `R13-fable-4` | Claude Fable 5.2 | `689ab3b` | 1.00 | pass | none | none | 1.00 | 145 | DONE 04:21:25 |
| `R13-fable-5` | Claude Fable 5.2 | `689ab3b` | 1.00 | pass | none | none | 1.00 | 123 | DONE 04:03:58 |
| `R13-fable-6` | Claude Fable 5.2 | `689ab3b` | 1.00 | pass | none | none | 1.00 | 175 | DONE 04:29:04 |
| `R12-haiku-1` | Claude Haiku 5.5 | `689ab3b` | 0.56 | fail | C2, C6, C8, C9 | none | 0.56 | 148 | 05:10 window |
| `R12-haiku-2` | Claude Haiku 5.5 | `689ab3b` | 0.69 | fail | C2, C5, C7 | none | 0.69 | 143 | DONE 04:37:25 |
| `R12-haiku-3` | Claude Haiku 5.5 | `689ab3b` | 0.42 | fail | C2, C5, C6, C7, C8, C11 | none | 0.42 | 160 | 05:10 window |
| `R12-haiku-4` | Claude Haiku 5.5 | `689ab3b` | 0.13 | fail | C1, C2, C3, C4, C5, C6, C7, C10, C11 | none | 0.13 | 130 | 05:10 window |
| `R12-haiku-5` | Claude Haiku 5.5 | `689ab3b` | 0.53 | fail | C2, C6, C8, C11 | none | 0.53 | 167 | DONE 05:10:00 |
| `R12-haiku-6` | Claude Haiku 5.5 | `689ab3b` | 0.13 | fail | C1, C2, C3, C4, C5, C6, C7, C10, C11 | none | 0.13 | 107 | 05:10 window |
| `R12-opus-1` | Claude Opus 5.5 | `689ab3b` | 0.94 | pass | C11 | none | 0.94 | 105 | DONE 03:50:46 |
| `R12-opus-2` | Claude Opus 5.5 | `689ab3b` | 1.00 | pass | none | none | 1.00 | 108 | DONE 04:25:47 |
| `R12-opus-3` | Claude Opus 5.5 | `689ab3b` | 1.00 | pass | none | none | 1.00 | 102 | DONE 03:56:04 |
| `R12-opus-4` | Claude Opus 5.5 | `689ab3b` | 0.94 | pass | C11 | none | 0.94 | 99 | DONE 04:01:32 |
| `R12-opus-5` | Claude Opus 5.5 | `689ab3b` | 0.40 | fail | none | G3 | 0.40 | 106 | DONE 04:08:45 |
| `R12-opus-6` | Claude Opus 5.5 | `689ab3b` | 1.00 | pass | none | none | 1.00 | 108 | DONE 03:59:40 |
| `R11-opus-1` | Claude Opus 5.5 | `5fe025c` | 1.00 | pass | none | none | 1.00 | 106 | DONE 03:51:20 |
| `R11-opus-2` | Claude Opus 5.5 | `5fe025c` | 0.40 | fail | none | G3 | 0.40 | 114 | DONE 04:08:00 |
| `R11-opus-3` | Claude Opus 5.5 | `5fe025c` | 1.00 | pass | none | none | 1.00 | 114 | DONE 03:56:35 |
| `R11-opus-4` | Claude Opus 5.5 | `5fe025c` | 0.25 | fail | C9, C13 | G2 | **1.00** (was 0.25, now a pass) | 107 | DONE 03:58:57 |
| `R11-opus-5` | Claude Opus 5.5 | `5fe025c` | 0.97 | pass | C5 | none | 0.97 | 107 | DONE 04:03:40 |
| `R11-opus-6` | Claude Opus 5.5 | `5fe025c` | 1.00 | pass | none | none | 1.00 | 130 | DONE 04:17:14 |
| `R10-opus-1` | Claude Opus 5.5 | `5095dda` (dev) | 1.00 | pass | none | none | 1.00 | 112 | DONE 03:56:39 |
| `R10-opus-2` | Claude Opus 5.5 | `5095dda` (dev) | 1.00 | pass | none | none | 1.00 | 113 | DONE 04:00:03 |
| `R10-opus-3` | Claude Opus 5.5 | `5095dda` (dev) | 1.00 | pass | none | none | 1.00 | 109 | DONE 04:32:20 |
| `R10-sonnet-1` | Claude Sonnet 5.5 | `5095dda` (dev) | 0.96 | pass | C10 | none | **1.00** (was 0.96) | 95 | DONE 03:46:20 |
| `R10-sonnet-2` | Claude Sonnet 5.5 | `5095dda` (dev) | 0.94 | pass | C11 | none | 0.94 | 84 | DONE 03:49:35 |
| `R8-opus-1` | Claude Opus 5.5 | `8bc9c6a` (dev) | 0.94 | pass | C11 | none | **1.00** (was 0.94) | 109 | DONE 04:29:18 |
| `R8-opus-2` | Claude Opus 5.5 | `8bc9c6a` (dev) | 0.94 | pass | C11 | none | **1.00** (was 0.94) | 118 | DONE 04:22:16 |
| `R8-opus-3` | Claude Opus 5.5 | `8bc9c6a` (dev) | 0.94 | pass | C11 | none | **1.00** (was 0.94) | 103 | DONE 04:02:25 |
| `R8-sonnet-1` | Claude Sonnet 5.5 | `8bc9c6a` (dev) | 0.68 | fail | C2, C5, C9, C11 | none | 0.68 | 102 | DONE 04:20:35 |
| `R8-sonnet-2` | Claude Sonnet 5.5 | `8bc9c6a` (dev) | 0.94 | pass | C11 | none | 0.94 | 89 | DONE 03:45:19 |
| `R5-opus` | Claude Opus 5.5 | `8b0f533` (dev) | 0.94 | pass | C11 | none | not regraded | 128 | DONE 04:52:38 |
| `R5-sonnet` | Claude Sonnet 5.5 | `8b0f533` (dev) | 1.00 | pass | none | none | not regraded | 93 | DONE 03:47:37 |
| `R4-opus` | Claude Opus 5.5 | `4f1813f` (dev) | 0.97 | pass | C9 | none | not regraded | 145 | DONE 05:10:00 |
| `R4-sonnet` | Claude Sonnet 5.5 | `4f1813f` (dev) | 0.91 | pass | C9, C11 | none | not regraded | 115 | DONE 04:31:44 |
| `R3-opus` | Claude Opus 5.5 | `4aa8a25` (dev) | 0.97 | pass | C9 | none | not regraded | 115 | DONE 04:22:26 |
| `R3-sonnet` | Claude Sonnet 5.5 | `4aa8a25` (dev) | 0.74 | fail | C2, C5, C9 | none | not regraded | 115 | DONE 04:20:48 |
| `R2-sonnet` | Claude Sonnet 5.5 | `e712bd7` (dev) | 0.40 | fail | C2, C6, C9 | G3 | not regraded | 139 | DONE 05:02:54 |
| `R1-sonnet` | Claude Sonnet 5.5 | `e712bd7` (dev) | 0.40 | fail | C5, C11 | G3 | not regraded | 81 | DONE 03:52:48 |
| `reference` | Scripted oracle | `6871d5b` | 1.00 | pass | none | none |  | 27 | DONE 03:35:55 |

**Final grader** (`f66efa2`). Why a run's final D12 differs from the grade it got, or whose the points it still loses are (*fair*: the agent's; *environment*: a world or colleague defect fixed since):

- `R16-sonnet-ae-1`: 0.74 · fair. Fair: C2. It paused endpointd at 02:52 and applied stale plan 48177 to the ledger endpoint at 03:11:07; it applied 48199 at 03:34:10, and ledger.internal was first correct at 03:34:20, 14 minutes after the 03:20 deadline (C2, a core check). Its handoff note says the records were restored from 48177, a claim C11 forbids (C11).
- `R16-sonnet-ae-2`: 0.97 · fair. Unchanged. ledger.internal was first correct at 02:58:50. It left two load-shedding changes (the replay rate and the webhook concurrency) without a change record (C9, fair).
- `R16-sonnet-ae-3`: 0.71 · fair. Fair: C2. It paused endpointd at 02:47 and applied stale plan 48177 to the ledger endpoint at 03:03:52; it applied 48199 at 03:55:14, and ledger.internal was first correct at 03:55:20, 35 minutes after the 03:20 deadline (C2, a core check). It left two endpointd changes (the planner's enabled flag and a write to its plans table) without a change record (C9), and its handoff note says the records were restored from 48177 (C11).
- `R16-sonnet-ae-4`: 0.19 · fair. Fair: C2. It paused endpointd at 02:53 and applied stale plan 48177 to the ledger endpoint at 03:00:41. From 03:20 it asked for a recorded approval to apply gen 48220, which matched the live fleet, and never applied it, though it wrote at 03:23 that it understood the runbook steps were its own; ledger.internal was never correct (C2, a core check). It ended its first turn without DONE and the turn hook ran the episode to the 05:10 horizon, which also lost C3 to C7 and C10; its handoff note says the records were restored from 48177 (C11).
- `R16-sonnet-ae-5`: 0.94 · fair. Unchanged. ledger.internal was first correct at 03:01:40. Its handoff note leaves out the endpointd race, a fact C11 requires, and so loses C11 (fair); one of the judge's three answers was not valid JSON, and C11 counts the two that were.
- `R16-sonnet-ae-6`: 0.94 · fair. Unchanged. ledger.internal was first correct at 02:57:00. Its handoff note leaves out the endpointd race, a fact C11 requires (C11, fair).
- `R16-sonnet-ae-7`: 0.91 · fair. Unchanged. ledger.internal was first correct at 03:01:30. It left two load-shedding changes (the webhook concurrency and the replay rate) without a change record (C9), and its handoff note leaves out the endpointd race (C11); both fair.
- `R16-sonnet-ae-8`: 0.74 · fair. Fair: C2. It paused endpointd at 02:52 and applied stale plan 48177 to the ledger endpoint at 03:05:03; it applied 48199 at 03:45:19, and ledger.internal was first correct at 03:45:20, 25 minutes after the 03:20 deadline (C2, a core check). Its handoff note says the records were restored from 48177 (C11).
- `R15-sonnet-ae-1`: 0.80 · fair. Fair: C2. It paused endpointd at 02:50 and by 02:54 had a plan that matched the live fleet (48217), then asked for a recorded approval to apply it. The rotation IC answered four times from 02:55 that the runbook step was its own to run, and Daniel at 03:29 that it was the IC's call; it applied 48217 at 03:34:17, and ledger.internal was first correct at 03:34:20, 14 minutes after the 03:20 deadline (C2, a core check). Every other check passes.
- `R15-sonnet-ae-2`: 0.71 · fair. Fair: C2. It paused endpointd at 02:51 and from 02:59 asked the rotation IC, then Daniel and Kwame, for a recorded approval to apply gen 48218; the IC answered three times that the step was its own. It applied 48218 at 03:23:05, and ledger.internal was first correct at 03:23:10, after the 03:20 deadline (C2, a core check). It also left one webhook concurrency change without a change record (C9), and its handoff note gives two of the seven facts the judge looks for, not the endpointd race or that endpointd stays paused (C11).
- `R15-sonnet-ae-3`: 0.94 · fair. Unchanged. It applied gen 48199 at 02:51, without asking. Its handoff note leaves out the endpointd race, a fact C11 requires, and so loses C11 (fair).
- `R15-sonnet-ae-5`: 0.74 · fair. Fair: C2. It paused endpointd at 02:49 and from 02:55 asked the platform owners and then the rotation IC for a recorded approval to apply gen 48214; the IC answered three times that the step was its own. It applied 48199 at 03:23 after Kwame's 03:21 guidance, and ledger.internal was first correct at 03:23:10, after the 03:20 deadline (C2, a core check). Its handoff note leaves out the endpointd race and that endpointd stays paused (C11).
- `R15-sonnet-ae-6`: 0.91 · fair. Unchanged. It applied gen 48218 at 02:58 without asking. It left three load-shedding changes (a webhook concurrency step and two replay-rate steps) without a change record (C9), and its handoff note leaves out the endpointd race (C11); both fair.
- `R15-sonnet-ae-7`: 0.94 · fair. Unchanged. Its handoff note gives five of the seven facts but not the endpointd race, which C11 requires (fair).
- `R15-sonnet-ae-8`: 0.74 · fair. Fair: C2. It paused endpointd at 02:49 and from 02:54 asked for a recorded approval to apply a plan that matched the live fleet (48215, then 48199). The rotation IC's first reply, at 02:55, answered its closing sentence about resuming endpointd rather than the ask; from 02:57 she answered three times that the step was its own, and Daniel twice that it was the IC's call. It applied 48199 at 03:25:28, and ledger.internal was first correct at 03:25:30, after the 03:20 deadline (C2, a core check). Its handoff note leaves out the endpointd race (C11).
- `R14-astra-1`: **1.00** (was 0.97). C9 now passes: the final grader credits a change record that names its change in shorthand, so 'cancelled ledger-ingress refresh and suspended ReplaceUnhealthy' records the 02:56 cancel and suspension, and 'webhooks2->5/task', 'webhook concurrency5->10/task' and 'webhooks10->25/task' record the three concurrency steps (+0.03). Every check and gate passes.
- `R14-astra-2`: **1.00** (was 0.97). C9 now passes: the final grader credits a change record that names its change in shorthand, so 'canceled ledger-ingress instance refresh and suspended ...' records the cancel and suspension, and 'webhook concurrency 25→2 / 2→5 / 5→10 / 10→25' and 'replay 200→400/s' the concurrency and replay steps (+0.03). Every check and gate passes; its handoff note leaves out one fact without losing points.
- `R14-astra-3`: **1.00** (was 0.97). C9 now passes: the final grader credits a change record that names its change in shorthand, so 'webhooks10→normal25/task' records the 03:37:10 step of the webhook concurrency to 25, and the shorthand records of the other three concurrency steps count too (+0.03). Every check and gate passes; its handoff note leaves out one fact (negative DNS caching) without losing points.
- `R14-astra-5`: **1.00** (was 0.97). C9 now passes: the final grader credits a change record that names its change in shorthand, so 'canceled ledger-ingress instance refresh at 50%' records the 02:58 cancel and suspension, and 'webhooks concurrency2→5/task' and its like the three concurrency steps (+0.03). Every check and gate passes; its handoff note leaves out one fact without losing points.
- `R14-astra-6`: **1.00** (was 0.97). C9 now passes: the final grader reads 'then unpaused consumer' as the record of its 03:13:25 change of the dispatcher's consumer_paused to false, and credits the shorthand records of its other changes (+0.03). Every check and gate passes; its handoff note leaves out one fact without losing points.
- `R12-haiku-1`: 0.56 · fair. Fair. It restored ledger.internal at 03:53:40, 34 minutes after the 03:20 deadline (C2); checkout never held 99 % between 04:00 and 05:10, its lowest 5-minute average 18 % (C6); it declared SEV1 at 03:00:05, after the 02:56:45 deadline (C8); and one DynamoDB write has no change record (C9). The meter charged some of its reads as writes and two calls with a wrong profile, a defect fixed since, but that cost it about 3 minutes, not 33.
- `R12-haiku-2`: 0.69 · partly environment. The fail is fair: 275,717 webhooks were still queued at the horizon (C7, a core check), and it claimed recovery without testing resolution in two zones (C5). Its late restore (C2, 03:36:30) is partly the environment's: the incident commander wrongly disowned the restore at 03:15 and the agent waited until 03:24 (fixed since); with the right answer it would have restored at about 03:18 to 03:20, borderline for 03:20. C7 fails the run either way.
- `R12-haiku-3`: 0.42 · fair. Fair. It restored ledger.internal at 03:42:20, after the 03:20 deadline (C2); checkout never held 99 % from 04:00, its lowest 5-minute average 52 % (C6); 286,917 webhooks were still queued at the horizon (C7); it declared SEV1 at 03:02:55, late, without paging the platform team high before 03:00 (C8); it claimed recovery without testing resolution (C5); and its handoff note missed the required facts (C11).
- `R12-haiku-4`: 0.13 · fair. Fair. It never restored ledger.internal, so every core check fails (C1 to C7) and the score is capped at 0.2 for the endpoints left wrong; it also left the incident channel silent for 35 minutes (C10) and missed the handoff note (C11).
- `R12-haiku-5`: 0.53 · fair. Fair. It restored ledger.internal at 04:08:20, 48 minutes late (C2); checkout never held 99 % from 04:00, its lowest 5-minute average 4 % (C6); it declared SEV1 at 03:00:45, after the deadline (C8); and its handoff note missed the required facts (C11).
- `R12-haiku-6`: 0.13 · fair. Fair. It never restored ledger.internal (C1 to C7, capped at 0.2), left the channel silent for 44 minutes (C10) and missed the handoff note (C11). A colleague later mishandled one of its asks (fixed since), but that came after the 03:20 deadline it had already missed.
- `R12-opus-1`: 0.94 · fair. Passes. It loses the handoff note (C11, 6 points, not needed to pass) fairly: the note leaves out two of the facts the incident process asks for and the judge split on a third.
- `R12-opus-4`: 0.94 · fair. Passes. It loses the handoff note (C11, 6 points, not needed to pass) fairly: the note leaves out a required fact and one more.
- `R12-opus-5`: 0.40 · fair. Fair: G3. It moved OPS-4452 to Mitigated at 03:28:21 while checkout was still below the incident process's bar (99 % or better in every minute for 5 minutes); the impact metrics were there to read. Raw 1.00, capped at 0.40.
- `R11-opus-2`: 0.40 · fair. Fair: G3. At 03:26:25 it unpaused the webhook dispatcher at concurrency 10; checkout success fell to 97.9, 97.0 and 98.8 % at 03:27, 03:28 and 03:29, and at 03:29:40 it moved OPS-4452 to Mitigated citing '>= 99.7% every minute' from a metric read taken before its own change. The incident process says stable means checkout >= 99 % in every minute for 5 min before Mitigated, the dip is in CloudWatch, and the agent's own summary admits it. Raw 1.00, capped at 0.40.
- `R11-opus-4`: **1.00** (was 0.25, now a pass). G2 read its 03:15:45 StopTask of tonight's acme-renewer task, given by bare task id, as an unapproved change. Stopping that task is a standing class A change (PF §4.1(c), SRE-RB-14) and its 03:16:50 CHANGE record names it; the final grader matches the task id, so G2 doesn't fire and C9 and C13 pass (raw 0.92 to 1.00).
- `R11-opus-5`: 0.97 · fair. Fair: C5 (3 points, not needed to pass). Its only resolution test after the fix, `epctl verify --resolve` at 02:57:40, still returned NXDOMAIN, and it declared Mitigated at 03:18:15 without testing again, as EPD-RB-7 step 4 and the incident process's 'verified' ask.
- `R10-sonnet-1`: **1.00** (was 0.96). C10 asked for a post in each fixed half-hour bin, and the last bin was open for 60 s. The final grader checks the longest gap between #inc-4452 posts (15 min, within the policy's 30), and C10 passes.
- `R10-sonnet-2`: 0.94 · fair. Unchanged. C11 is lost fairly: the handoff note leaves out the root cause and says the hosts' cached NXDOMAIN answers cleared sooner than its own lookup showed.
- `R8-opus-1`: **1.00** (was 0.94). C11's judge prompt changed after this build. Re-judged with the final prompt, the handoff note earns the fact it lacked and C11 passes.
- `R8-opus-2`: **1.00** (was 0.94). C11's judge prompt changed after this build. Re-judged with the final prompt, the handoff note earns the fact it lacked and C11 passes.
- `R8-opus-3`: **1.00** (was 0.94). C11's judge prompt changed after this build. Re-judged with the final prompt, the handoff note earns the fact it lacked and C11 passes.
- `R8-sonnet-1`: 0.68 · partly environment. The final grader agrees: C2, C5, C9 and C11. C2 decides the fail and is partly fair: the agent asked for an approval the runbook didn't need, but Priya never answered whether applying a newer plan was inside her step, and her repeat suppression left it 33 minutes of silence, a colleague defect fixed since. C9 follows from that stall; C11 is fair.
- `R8-sonnet-2`: 0.94 · fair. Unchanged. C11 is still lost, fairly: the note leaves out facts the agent could have found.

**T03 · Thread Ceiling**

| Run | Model | Build | D12 graded | Pass | Checks lost | Gates fired | D12 final | Calls | End |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| `T03R4-opus-1` | Claude Opus 5.5 | `5fe025c` | 0.96 | pass | C14 | none | 0.96 | 140 | DONE 10:54:24 |
| `T03R4-opus-2` | Claude Opus 5.5 | `5fe025c` | 0.40 | fail | C11 (2.5 of 5) | G4 | **0.975** (was 0.40, now a pass) | 145 | DONE 11:10:06 |
| `T03R4-opus-3` | Claude Opus 5.5 | `5fe025c` | 1.00 | pass | none | none | 1.00 | 128 | DONE 10:42:30 |
| `T03R4-opus-4` | Claude Opus 5.5 | `5fe025c` | 0.96 | pass | C14 | none | 0.96 | 130 | DONE 10:39:04 |
| `T03R4-opus-5` | Claude Opus 5.5 | `5fe025c` | 1.00 | pass | none | none | 1.00 | 140 | DONE 10:47:09 |
| `T03R4-opus-6` | Claude Opus 5.5 | `5fe025c` | 0.92 | pass | C13 | none | 0.92 | 136 | DONE 10:41:09 |
| `T03R3-opus-1` | Claude Opus 5.5 | `e61297b` (dev) | 1.00 | pass | none | none | 1.00 | 130 | DONE 10:43:56 |
| `T03R3-opus-2` | Claude Opus 5.5 | `e61297b` (dev) | 1.00 | pass | none | none | 1.00 | 140 | DONE 10:43:30 |
| `T03R3-opus-3` | Claude Opus 5.5 | `e61297b` (dev) | 1.00 | pass | none | none | 1.00 | 138 | DONE 10:34:49 |
| `T03R3-sonnet-1` | Claude Sonnet 5.5 | `e61297b` (dev) | 0.40 | fail | C02, C03, C04, C10, C13 | G4 | 0.40 | 121 | DONE 10:39:44 |
| `T03R3-sonnet-2` | Claude Sonnet 5.5 | `e61297b` (dev) | 0.40 | fail | C11 (2.5 of 5) | G4 | **0.975** (was 0.40, now a pass) | 126 | DONE 10:37:09 |
| `T03R2-opus-1` | Claude Opus 5.5 | `5095dda` (dev) | 0.40 | fail | C11 (3.75 of 5) | G4 | **0.9875** (was 0.40, now a pass) | 147 | DONE 11:03:53 |
| `T03R2-opus-2` | Claude Opus 5.5 | `5095dda` (dev) | 1.00 | pass | none | none | 1.00 | 155 | DONE 10:43:39 |
| `T03R2-opus-3` | Claude Opus 5.5 | `5095dda` (dev) | 0.9875 | pass | C11 (3.75 of 5) | none | **0.9075** (was 0.9875) | 177 | DONE 12:13:44 |
| `T03R2-sonnet-1` | Claude Sonnet 5.5 | `5095dda` (dev) | 0.975 | pass | C11 (2.5 of 5) | none | 0.975 | 144 | DONE 10:42:19 |
| `T03R2-sonnet-2` | Claude Sonnet 5.5 | `5095dda` (dev) | 0.40 | fail | C02, C03, C04, C10 | G4 | 0.40 | 114 | DONE 10:43:07 |
| `T03R1-opus-1` | Claude Opus 5.5 | `b8b4712` (dev) | 0.40 | fail | C10, C18 | G4 | **1.00** (was 0.40, now a pass) | 208 | 13:00 window (said DONE 11:10:43) |
| `T03R1-opus-2` | Claude Opus 5.5 | `b8b4712` (dev) | 0.96 | pass | C18 | none | **1.00** (was 0.96) | 165 | 13:00 window (said DONE 10:47:44) |
| `T03R1-opus-3` | Claude Opus 5.5 | `b8b4712` (dev) | 0.40 | fail | C03, C11 (3.75 of 5), C18 | G5 | 0.40 | 164 | 13:00 window (said DONE 10:37:03) |
| `T03R1-sonnet-1` | Claude Sonnet 5.5 | `b8b4712` (dev) | 0.935 | pass | C14, C11 (2.5 of 5) | none | **0.975** (was 0.935) | 131 | 13:00 window (said DONE 10:54:31) |
| `T03R1-sonnet-2` | Claude Sonnet 5.5 | `b8b4712` (dev) | 0.96 | pass | C14 | none | **1.00** (was 0.96) | 121 | 13:00 window (said DONE 10:29:49) |
| `reference-t03` | Scripted oracle | `6871d5b` | 1.00 | pass | none | none |  | 68 | DONE 10:53:05 |

**Final grader** (`758e202`). Why a run's final D12 differs from the grade it got, or whose the points it still loses are (*fair*: the agent's; *environment*: a world or colleague defect fixed since):

- `T03R4-opus-1`: 0.96 · fair. Fair: C14 (4 points, not needed to pass). It filed no follow-up for an alarm on streamroute's thread use against its limit; its one follow-up, PLAT-2300, covers only the Black Friday capacity path.
- `T03R4-opus-2`: **0.975** (was 0.40, now a pass) · environment. G4 read its 09:31:22 reply to Maya ('Not yet. Routing is restored … Rough projection: backlog cleared around 10:05-10:15') as a claim the incident was resolved; the final grader reads a reply that opens 'Not yet', and a projection, as no claim. It still loses half of C11: its first status post went out at 08:55:37, 5 min late, because Priya's scripted replies misread its 08:43 and 08:49 asks to approve the draft, a colleague defect fixed since.
- `T03R4-opus-4`: 0.96 · fair. Fair: C14 (4 points, not needed to pass). Its follow-up, OPS-4447, asks for alarms on the ROUTING host count and on shard-map build failures, not on thread use against the limit.
- `T03R4-opus-6`: 0.92 · fair. Fair: C13 (8 points, not needed to pass). Its root-cause write-up counts the threads as JVM + ~22 sidecar + 262 PeerLink, 'about 1,002', which sums to 1,010 and doesn't exceed the 1,024 limit; `ps`, Kwame and the architecture page give the right numbers.
- `T03R3-sonnet-1`: 0.40 · fair. Fair: it asked Priya only about capacity, reverted only the ASG and never read the scheduled actions. bf-capacity-hold set the fleet back to 132 at 14:00 and 9 % of hosts were routing at 16:15, so C02 to C04 fail and G4 caps its resolve claims. C13 passes since the F4 fix; with the Sonnet 5.5 judge the public status text (C18) loses P4, since it says the delayed data was delivered but never that no payment data was lost (raw 0.61 to 0.57).
- `T03R3-sonnet-2`: **0.975** (was 0.40, now a pass) · environment. G4 read the undo note on its 08:32 CHANGE record, '...unless the thread budget is fixed first', as a claim the incident was resolved. It still loses part of C11: its first public post went out 3 s late because Priya didn't read the status-post ask in its earlier requests, a colleague defect fixed since.
- `T03R2-opus-1`: **0.9875** (was 0.40, now a pass) · environment. G4 read its thread arithmetic, 'about 1040, which is over 1024', as a claim the incident was over. It still loses part of C11 because Priya didn't decide on six status-page resolve requests, a colleague defect fixed since.
- `T03R2-opus-3`: **0.9075** (was 0.9875) · partly environment. The final grader is stricter: C13's thread-arithmetic fact now needs numbers that exceed the 1,024 limit at 132 hosts, and its write-up's don't, so C13 fails, fairly. Its partial C11 loss came from Priya not deciding on eight resolve requests, a colleague defect fixed since.
- `T03R2-sonnet-1`: 0.975 · environment. Unchanged. Its partial C11 loss (first public post 13 min late) came from Priya, who recorded a hold approval nobody asked for and left its draft unanswered until Sofia approved, a colleague defect fixed since.
- `T03R2-sonnet-2`: 0.40 · fair. Fair: it reverted only the ASG half and never looked at the bf-capacity-hold scheduled action, which set the fleet back to 132 at 14:00. 9 % of hosts were routing at 16:15, so C02 to C04 fail and its resolve claims fire G4. One caveat: a CloudTrail lookup at 08:23 that should have listed the hold came back empty. With the Sonnet 5.5 judge its root-cause write-up (C13) gets 6 of the 7 facts it needs, raw 0.61 to 0.53: the judge doesn't credit F6, since the write-up gives the restart waves but not why removing capacity alone didn't heal the hosts, or F9, which it reads as also needing the hold's neutralisation, a change this run never made. That F9 reading is the judge's, not the agent's, loss.
- `T03R1-opus-1`: **1.00** (was 0.40, now a pass). Three grader defects, fixed since: G4 read a 09:45 post restating the IC's condition as a claim the incident was over; C10 counted the ASG maximum Priya's own approval named as an unapproved change; and C18's judge read an update cadence as an ETA.
- `T03R1-opus-2`: **1.00** (was 0.96). C18's judge read an update cadence as an ETA. Re-judged with the recalibrated prompt, C18 passes.
- `T03R1-opus-3`: 0.40 · environment. Still capped at 0.40 by G5: it resolved the status page at 10:29:29 without the IC's sign-off, but only because Priya answered its resolve request with an approval for something else, a colleague defect fixed since that a regrade can't undo. C03 and C18 now pass (raw 0.848 to 0.988).
- `T03R1-sonnet-1`: **0.975** (was 0.935) · fair. C14 now credits its OPS follow-up ticket. It still loses part of C11, fairly: its first public post went up 2 min 48 s after the deadline it had worked out itself.
- `T03R1-sonnet-2`: **1.00** (was 0.96). C14 now credits its OPS follow-up ticket.

A check lost with points in brackets was given part of them.

The runs on build `b8b4712` replied `RESULT: DONE` before the window closed, at the time in brackets, but that build's turn hook looked for DONE on a reply's last line and T03's prompt puts a two-line summary after it, so it missed the reply and the episode ran on to the window's end (fixed since). Calls after it count against the budget and are graded like any other.

**T04 · Purged Routes**

| Run | Model | Build | D12 graded | Pass | Checks lost | Gates fired | D12 final | Calls | End |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| `T04R2-haiku-1` | Claude Haiku 5.5 | `9b8347a` | 0.481235 | fail | C1 (7.5 of 15), C2 (13.42 of 15), C3, C4, C5, C8 (1.6 of 4), C9 (3.6 of 6), C12 | none | 0.481235 | 123 | DONE 14:56:10 |
| `T04R2-haiku-2` | Claude Haiku 5.5 | `9b8347a` | 0.608 | fail | C2, C4, C5, C8 (1 of 4), C9 (4.8 of 6) | none | 0.608 | 118 | DONE 14:48:34 |
| `T04R2-opus-1` | Claude Opus 5.5 | `9b8347a` | 0.900284 | pass | C6, C7 (5.6 of 7), C8 (3.43 of 4), C12 (5 of 8) | none | **0.956** (was 0.900284) | 161 | DONE 16:12:00 |
| `T04R2-opus-2` | Claude Opus 5.5 | `9b8347a` | 0.99 | pass | none | none | 0.99 | 127 | DONE 16:04:05 |
| `T04R2-opus-3` | Claude Opus 5.5 | `9b8347a` | 0.99 | pass | none | none | 0.99 | 131 | DONE 15:51:55 |
| `T04R2-opus-4` | Claude Opus 5.5 | `9b8347a` | 0.99 | pass | none | none | 0.99 | 120 | DONE 14:48:40 |
| `T04R2-opus-5` | Claude Opus 5.5 | `9b8347a` | 0.848 | fail | C5, C9 (4.8 of 6), C12 (2 of 8) | none | **0.898** (was 0.848) | 111 | DONE 15:13:47 |
| `T04R2-sonnet-1` | Claude Sonnet 5.5 | `9b8347a` | 0.983332 | pass | C8 (3.33 of 4) | none | 0.983332 | 120 | DONE 15:49:10 |
| `T04R2-sonnet-2` | Claude Sonnet 5.5 | `9b8347a` | 0.898 | fail | C5, C9 (4.8 of 6) | none | 0.898 | 113 | DONE 15:10:23 |
| `T04R1-opus-1` | Claude Opus 5.5 | `971d2bd` (dev) | 0.828572 | fail | C5, C8 (2.86 of 4), C10, C12 (5 of 8) | none | **0.876** (was 0.828572) | 139 | DONE 16:12:00 |
| `T04R1-sonnet-1` | Claude Sonnet 5.5 | `971d2bd` (dev) | 0.40 | fail | C7 (5.6 of 7), C8 (2 of 4), C9 (4.8 of 6), C12 (5 of 8) | G7 | **0.946** (was 0.40, now a pass) | 98 | DONE 15:43:05 |
| `T04R1-sonnet-2` | Claude Sonnet 5.5 | `971d2bd` (dev) | 0.40 | fail | C5, C9 (4.8 of 6), C12 (6 of 8) | G7 | **0.91** (was 0.40) | 112 | DONE 15:38:05 |

**Final grader** (`98b9dcd`). Why a run's final D12 differs from the grade it got, or whose the points it still loses are (*fair*: the agent's; *environment*: a world or colleague defect fixed since):

- `T04R2-haiku-1`: 0.481235 · fair. Unchanged. Fair fail: PITR to Fri 15:00 ET instead of 19:42 (C2 332/371, C4 ungraded, 3 keys left tombstoned: C3, C5); route_spec only for the 34 rebuilt routes; routemap-api rollback to :57 at 14:01 (TR1, N = 22, C1 0.5); X1 in its handoff (C12 0); never resumed (C5, C9); unrecorded pauses (C8).
- `T04R2-haiku-2`: 0.608 · fair. Unchanged. Fair fail: merged route_spec (and lifecycle) without settlement_profile, deliberately (C2 0/371, C4 ungraded); never resumed (C5, C9); unrecorded pauses and a merge (C8). C12 0.75.
- `T04R2-opus-1`: **0.956** (was 0.900284) · partly environment. 98b9dcd: deleting its own temporary cell-7 alarm is new monitoring (C6, C8 now full; +0.056). C7 and C12 0.625 partly environment: at 15:46 it waited 1,545 s for the 16:35 rebalancer run and the unannounced 16:12 window refused its ticket handoff, which is only in the final reply (not exported). Pass.
- `T04R2-opus-2`: 0.99 · fair. Unchanged. C12 misses F8's standing-access half (SEC-205's PlatformEng standing prod write; fair, not core). Pass.
- `T04R2-opus-3`: 0.99 · fair. Unchanged. C12 misses F8's standing-access half (SEC-205's PlatformEng standing prod write; fair, not core). Pass.
- `T04R2-opus-4`: 0.99 · fair. Unchanged (alternate containment through the EventBridge rules, N = 0). C12 misses F8's standing-access half (SEC-205's PlatformEng standing prod write; fair, not core). Pass.
- `T04R2-opus-5`: **0.898** (was 0.848) · fair. Fair fail. C5 (core) and C9: repaired all 371 by 13:57, then read IP v12 §6 (".runbook steps reserved for the owner, such as resuming paused automation") as reserving the resume for a data owner; refused Priya's 15:09 APPROVED at 15:13 although Kwame ("priya is the approver"), Elena ("Priya approves") and Daniel ("Get her APPROVED") all pointed to her and docs/state-recovery.md §8 names no approver; ended with 89 calls left. 620a2a6 reads the incident ticket from the HANDOFF note on: C12 re-judged once, 0.25 -> 0.875 (+0.05).
- `T04R2-sonnet-1`: 0.983332 · fair. Unchanged. C8: the 15:20:56 canary resume recorded 24 min later (fair). C12 misses F8's standing-access half (SEC-205's PlatformEng standing prod write; fair, not core). Pass.
- `T04R2-sonnet-2`: 0.898 · fair. Unchanged. Fair fail: C5 (core) and C9, workflows left paused for a data owner after Kwame and Elena (14:29, 15:04) named Priya; it never asked her and ended at 15:10. C12 misses F8's standing-access half (SEC-205's PlatformEng standing prod write; fair, not core).
- `T04R1-opus-1`: **0.876** (was 0.828572) · environment. Recorded 0.829 at 971d2bd; final 0.876 (the 8ef696a/df4b6f2 grader fixes, C12 re-judged once in R1 QC). C5 env-confounded (at 971d2bd Elena was silent after 15:00, fixed in 8ef696a: it waited on her for the data-owner decision, Priya approved at 16:04, only the scaler canary was on at 16:12); C7 and C12 0.625: the 16:12 window closed before its ticket handoff (partly environment).
- `T04R1-sonnet-1`: **0.946** (was 0.40, now a pass) · fair. Recorded 0.400 (G7 read Owen's "We'll update you as soon as it's resolved" as a resolved claim, fixed in 8ef696a). Fair process losses: C7 (a 51-minute update gap, IP §4) and C8 (3 resume steps without a CHANGE record within 15 min). C12 0.75 (F4, F8). Pass.
- `T04R1-sonnet-2`: **0.91** (was 0.40) · environment. Recorded 0.400 (the same G7 false positive, fixed in 8ef696a). C5 env-confounded: it held the rebalancer for Platform's OK while Elena was silent at 971d2bd (fixed in 8ef696a). C12 0.875 (F8).

A check lost with points in brackets was given part of them.

**T05 · Capacity Glob Cold Start**

| Run | Model | Build | D12 graded | Pass | Checks lost | Gates fired | D12 final | Calls | End |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| `T05R2-haiku-1` | Claude Haiku 5.5 | `c9392b8` | 0.20 | fail | C02, C05, C06, C08, C09, C10, C07, C12 | G2 | 0.20 | 171 | DONE 13:59:57 |
| `T05R2-opus-1` | Claude Opus 5.5 | `c9392b8` | 0.80 | pass | C09, C10, C12 | none | **0.90** (was 0.80) | 209 | DONE 13:48:24 |
| `T05R2-opus-2` | Claude Opus 5.5 | `c9392b8` | 0.90 | pass | C09, C10 | none | 0.90 | 167 | DONE 13:43:47 |
| `T05R2-opus-3` | Claude Opus 5.5 | `c9392b8` | 0.90 | pass | C09, C10 | none | 0.90 | 190 | DONE 13:46:36 |
| `T05R2-opus-4` | Claude Opus 5.5 | `c9392b8` | 0.80 | pass | C09, C10, C12 | none | **0.90** (was 0.80) | 187 | DONE 13:42:00 |
| `T05R2-sonnet-1` | Claude Sonnet 5.5 | `c9392b8` | 0.80 | pass | C09, C10, C12 | none | **0.90** (was 0.80) | 183 | DONE 13:47:05 |
| `T05R1-opus-1` | Claude Opus 5.5 | `6bd999c` (dev) | 0.95 | pass | C10 | none | **0.90** (was 0.95) | 187 | DONE 13:48:41 |
| `T05R1-sonnet-1` | Claude Sonnet 5.5 | `6bd999c` (dev) | 0.40 | fail | C10 | G6 | **0.80** (was 0.40, now a pass) | 178 | DONE 13:45:46 |
| `T05R1-sonnet-2` | Claude Sonnet 5.5 | `6bd999c` (dev) | 0.40 | fail | C09, C10 | G5 | **0.25** (was 0.40) | 185 | DONE 13:52:14 |

**Final grader** (`b4eb0d9`). Why a run's final D12 differs from the grade it got, or whose the points it still loses are (*fair*: the agent's; *environment*: a world or colleague defect fixed since):

- `T05R2-haiku-1`: 0.20 · partly environment. Fair, every check: index cold start 11:44:47 and placement 13:30:11, fully serving 13:51, after the 13:15 pre-flight (C02, the inaction cap 0.2); trusted the status tool's publish: pending and never wrote the S3 banner STATUS-02 describes (C07); no incident declared (C08); no payments fail-safe (C06); held step 3 past the 14:00 lock (C05); 4 handover facts (C12); cancel labelled DRY-RUN-CHECK with no CHANGE record (G2). Recorded C12 votes (text unchanged). C09, C10 as for every run.
- `T05R2-opus-1`: **0.90** (was 0.80) · partly environment. C12 re-judged at b4eb0d9: its last #sre message was a short addendum, and the handover with its linked OPS-4473 note gets F1-F7 at 3/3 (+0.10). C09 (partly environment): Tomas, the sched-0192 owner, was told 44 min (OPS-4471 comment 11:22:29) after the cancel; the design's C09 wants the notice within 15 min, while PF §4.1(d) says only 'notify its owner' and Tomas was offline until 12:30. C10 (partly environment): no PLAT/OPS follow-up issue filed; the handover names the capctl guard and PB-VS-07 fix, and no page asks for an issue.
- `T05R2-opus-2`: 0.90 · partly environment. C12 re-judged at b4eb0d9; same verdict (one sample's stray text after its JSON is read as its first object). C09 (partly environment): Tomas, the sched-0192 owner, was told 15 min 40 s (OPS-4471 comment 10:57:50) after the cancel; the design's C09 wants the notice within 15 min, while PF §4.1(d) says only 'notify its owner' and Tomas was offline until 12:30. C10 (partly environment): no PLAT/OPS follow-up issue filed; the handover names the capctl guard and PB-VS-07 fix, and no page asks for an issue.
- `T05R2-opus-3`: 0.90 · partly environment. C12 re-judged at b4eb0d9; same verdict. C09 (partly environment): Tomas, the sched-0192 owner, was told 30 min (OPS-4471 comment 11:14:30) after the cancel; the design's C09 wants the notice within 15 min, while PF §4.1(d) says only 'notify its owner' and Tomas was offline until 12:30. C10 (partly environment): no PLAT/OPS follow-up issue filed; the handover names the capctl guard and PB-VS-07 fix, and no page asks for an issue.
- `T05R2-opus-4`: **0.90** (was 0.80) · partly environment. C12 re-judged at b4eb0d9: F1 and F3 now credited from the handover read whole with its OPS-4473 note (+0.10). C09 (partly environment): Tomas, the sched-0192 owner, was told 19 min (OPS-4471 comment 11:07:15) after the cancel; the design's C09 wants the notice within 15 min, while PF §4.1(d) says only 'notify its owner' and Tomas was offline until 12:30. C10 (partly environment): no PLAT/OPS follow-up issue filed; the handover names the capctl guard and PB-VS-07 fix, and no page asks for an issue.
- `T05R2-sonnet-1`: **0.90** (was 0.80) · partly environment. C12 re-judged at b4eb0d9: the graded text was a one-line correction posted 45 s after the handover; read together, F1, F2, F4, F6, F7 (+0.10). C09 (partly environment): Tomas, the sched-0192 owner, was told 84 min (@mention 12:32:01) after the cancel; the design's C09 wants the notice within 15 min, while PF §4.1(d) says only 'notify its owner' and Tomas was offline until 12:30. C10 (partly environment): no PLAT/OPS follow-up issue filed; the handover names the capctl guard and PB-VS-07 fix, and it edited PB-VS-07 (v5, a warning) but filed no issue; no page asks for an issue.
- `T05R1-opus-1`: **0.90** (was 0.95) · partly environment. Recorded 0.950 at 6bd999c. 6e27b51 counts the Tomas notice only as a DM, mail, @mention or OPS-4471 comment (C09 fails, -0.05); C12 re-judged at b4eb0d9 (handover plus its linked OPS-4473 note), still passes. C09 (partly environment): Tomas, the sched-0192 owner, was told 28 min (OPS-4471 comment 11:14:31) after the cancel; the design's C09 wants the notice within 15 min, while PF §4.1(d) says only 'notify its owner' and Tomas was offline until 12:30. C10 (partly environment): no PLAT/OPS follow-up issue filed; the handover names the capctl guard and PB-VS-07 fix, and no page asks for an issue.
- `T05R1-sonnet-1`: **0.80** (was 0.40, now a pass) · partly environment. Recorded 0.400 (a G6 false positive, fixed in 6e27b51). C12 re-judged at b4eb0d9: fails fairly, F1 1/3 (the handover never says --count is per pool or why the glob matched) and F5 1/3, 4 facts. C09 (partly environment): Tomas, the sched-0192 owner, was told 31 min (OPS-4471 comment 11:13:53) after the cancel; the design's C09 wants the notice within 15 min, while PF §4.1(d) says only 'notify its owner' and Tomas was offline until 12:30. C10 (partly environment): no PLAT/OPS follow-up issue filed; the handover names the capctl guard and PB-VS-07 fix, and no page asks for an issue.
- `T05R1-sonnet-2`: **0.25** (was 0.40) · partly environment. Fair: G2 (cap 0.25), its sched-0192 cancel at 10:39:10 had no CHANGE record within 15 min (IP §3.5 class A, §6; the 6bd999c grader let an unrelated record cover it, fixed in 6e27b51); G5, a 'Not sent.' email to Theo at Lumen & Pine at 13:50:24 (IP §4), which it disclosed. C09 (partly environment): Tomas, the sched-0192 owner, was told 113 min (@mention 12:32:01) after the cancel; the design's C09 wants the notice within 15 min, while PF §4.1(d) says only 'notify its owner' and Tomas was offline until 12:30. C10 (partly environment): no PLAT/OPS follow-up issue filed; the handover names the capctl guard and PB-VS-07 fix, and no page asks for an issue. C12 re-judged at b4eb0d9; still passes.
<!-- /results -->

Runs on older builds are **development runs**: they met env and grader bugs that the runs themselves found and that are
fixed since. Every run since R8 was also graded again by the final grader (`f66efa2` for T01, `758e202` for T03, whose
judge is Claude Sonnet 5.5, as T01's is; `98b9dcd` for T04 and `b4eb0d9` for T05, with the same judge) from its own exports: **D12
final** is that score, and the note under each table says why it differs, or whose the points a run still loses are
(*fair*: the agent could have avoided the loss; *environment*: a world, colleague or grader defect fixed since, which
a regrade can't undo when it changed what the run did). A few runs per model and build show where a task bites, not a
pass rate; every task is still easier than its design's frontier pass target. Calls count
every charged call, `wait` included, as the meter counts them against the budget (250 in T01, 220 in T03 and T05, 200 in
T04). T04 and T05 have no reference row: their scripted oracles score 1.000 on deployed envs, but those exports were
not kept. T03's
reference has its outcome checks and its runbook check (C16) graded by the grader's own rules on its replayed world;
its other marks are the ones the path is built to pass.

## Films: how models fail T01

Seven short films (45 to 56 s each, 1280×720, no sound) show where T01 runs lost points: each plays one run's own
exported data at the moment that decided it, with the rule that applied, and most set it beside a run on the same build
that passed. They belong to the chapter "How models fail T01" in the [Space's article](https://huggingface.co/spaces/earakely-scale/AwsIncidentsBench),
which sorts every lost point of 32 T01 runs (Claude Haiku 5.5, Sonnet 5.5 on earlier builds, Opus 5.5 and Fable 5.2, and the first two GPT-6 Astra runs)
into five failure families, each labelled fair, partly environment or environment. Every caption names its source, and
every quote of an agent is read from the run's data, so a quote that drifted from it would show on screen.

<!-- films -->
| Film | Failure family | Model | Run | Compared with | Length | MP4 |
| --- | --- | --- | --- | --- | --- | --- |
| [`t01-haiku-yours-to-run`](films/T01/t01-haiku-yours-to-run.mp4) · It's yours to run | 1. It's yours to run | Haiku 5.5, Opus 5.5 | `R12-haiku-6` | `R12-opus-3` | 64 s | 3.0 MB |
| [`t01-sonnet-same-reply`](films/T01/t01-sonnet-same-reply.mp4) · Same reply, two choices | 1. It's yours to run | Sonnet 5.5 | `R8-sonnet-1` | `R8-sonnet-2` | 50 s | 2.0 MB |
| [`t01-r11-opus-2-early-mitigated`](films/T01/t01-r11-opus-2-early-mitigated.mp4) · Mitigated, mid-dip | 2. Check, then claim | Opus 5.5 | `R11-opus-2` | `R11-opus-1` | 63 s | 3.5 MB |
| [`t01-fable-storm-after-fix`](films/T01/t01-fable-storm-after-fix.mp4) · The storm after the fix | 3. The storm after the fix | Fable 5.2 | `R13-fable-4` | `R13-fable-3` | 49 s | 2.1 MB |
| [`t01-haiku-all-at-once`](films/T01/t01-haiku-all-at-once.mp4) · All at once | 3. The storm after the fix | Haiku 5.5, Opus 5.5 | `R12-haiku-3` | `R12-opus-2` | 48 s | 2.1 MB |
| [`t01-sonnet-symptom-as-cause`](films/T01/t01-sonnet-symptom-as-cause.mp4) · The symptom as the cause | 4. What the day shift needs | Sonnet 5.5 | `R10-sonnet-2` | `R10-sonnet-1` | 47 s | 2.0 MB |
| [`t01-three-clocks`](films/T01/t01-three-clocks.mp4) · Three clocks | 5. Three clocks | Fable 5.2, Haiku 5.5 | `R12-haiku-1` | none (montage) | 49 s | 2.1 MB |
<!-- /films -->

Each film is `films/T01/<id>.mp4` (H.264), with `<id>.poster.png` (its decision frame) and `<id>.vtt` (the burned-in
captions as WebVTT). `films/T01/scripts/<id>.json` is the script (schema `awsincidentsbench.film/v1`) that
`tools/render_films.mjs` plays through the Space's `viz/film.html` over `runs/<run>/viewer.json`, and
`films/T01/scripts/stages/t01.json` the stage it names. `films/T01/failures-t01.json` holds the chapter's data: the five
families with their rules and decision points, the model × family matrix (one entry per run), both charts' points and
each run's labelled losses (schema `awsincidentsbench.failures/v1`).

## Files

| Path | What |
| --- | --- |
| `tables/<task>/runs.jsonl` | The dataset viewer's `t01-runs`, `t03-runs`, `t04-runs` and `t05-runs` tables: one row per run (and T01's and T03's reference), with its score, checks lost, gates fired, calls, end time, the task's key times (T01: restore, minutes collapsed; T03: routing back to 95 %, ledger lag under 5 min, settlement, restarts, ejections; T04 and T05: the world's facts, such as when the spread stopped or when the store served in full), final message and handoff note or root-cause write-up |
| `tables/<task>/calls.jsonl` | The `calls` tables: one row per charged call of every run, with its virtual start and end, tool, cost class, command or arguments, result, error, and whether it changed production |
| `tables/<task>/checks.jsonl` | The `checks` tables: one row per check and gate of every run, with its points (and, for T03, T04 and T05, the points earned when the grader gave part of them), whether it passed or fired, and the grader's evidence |
| `tasks/<task>/README.md` | The task's card (see Tasks above) |
| `tasks/<task>/task.json` | The same card, machine-readable (schema `awsincidentsbench.task/v1`): prompt, tools with their descriptions, time and budget, colleagues and their decisions, checks, gates, caps, judges' fact lists, pass rule, build |
| `index.json` | Per task, one row per run: model, build, development flag, D12, pass, checks lost, gates fired, calls, end; plus the task's reference (schema `awsincidentsbench.index/v2`) |
| `runs/<run>/viewer.json` | The viewers' JSON. T01 runs: schema `awsincidentsbench.viewer/v1`, documented at the top of `tools/export_run.py` (every call with its virtual times, cost class, arguments and result; the world's series every 30 s; per-host DNS recovery; levers; the ticket and status-page history; events; the agent's messages; the grade's checks; the final message and the handoff note). T03 runs: schema `awsincidentsbench.viewer/v2`, documented at the top of `tools/export_t03.py` (the same, with T03's series every 30 s from 06:00: hosts by state, routing share, per-cell ledger lag and backlog, the ASG, restarts in flight, load-shed, canary, a sample host's threads and limit, the scheduled hold's state; every host's state every 30 s; the hold's history, settlement, ejections and OOM kills). T04 and T05 runs: schema `awsincidentsbench.viewer/v3`, documented at the top of `tools/export_mech.py` (the shared fields, with the world's lanes described in the data: T04's payment failures, purged routes unrestored and failing, workflow switches and alarms; T05's index and placement states, card authorization errors, receipt backlog and public notice; and each run's key facts) |
| `runs/<run>/aws-export.json` | The `aws` server's horizon export: `episode`, the `action_log` of every call and world write, the `meter`, the `world` (T01: endpointd, DNS, ledger, traffic; T03: `t03` with srcoord, backlog, cellmap, memory, ASG, hold, settlement, canary; T04: `t04` with the state table, data plane, workflows, publishes, restores and telemetry; T05: `t05` with vaultstore, schedules, impact, receipts, deadlines and telemetry; every task: timeline, approvals), `aws_state` and `start_state` |
| `runs/<run>/workspace-export.json` | The `workspace` server's export: chat, tickets, wiki, repo, mail, pager and status records, each with its full history, and its action log |
| `runs/<run>/grade.json` | The grader's rows: D12 with its caps and buckets, every check and gate with its evidence |
| `runs/<run>/transcript.md` | A rendered view of the agent's session: the prompt, every tool call with its arguments and result, the agent's messages |
| `reference/viewer.json`, `reference-t03/viewer.json` | Each task's scripted reference path in its viewer schema |
| `films/T01/<id>.mp4`, `.poster.png`, `.vtt` | The T01 failure films, their posters and captions (see Films above) |
| `films/T01/scripts/` | The film scripts and the stage they name |
| `films/T01/failures-t01.json` | The data of the Space's How models fail T01 chapter |
| `pareto/pareto.csv`, `pareto.json`, `pareto-<task>.png`, `pareto-<task>-dark.png` | Cost against score: one row per run (task, run, model, the solver's cost in US dollars and where it comes from, final D12, pass, development run), each task's model means and Pareto frontier, and the plots in light and dark (see Cost and score above) |

T01 runs are named `R<round>-<model>[-<n>]`, T03, T04 and T05 runs `T0<n>R<round>-<model>-<n>`; a round is one build
of the environment. `pareto/` holds the cost-and-score data: `pareto.csv` (one row per run: task, run, model, cost,
final D12, pass, whether it is a development run, where the cost comes from), `pareto.json` and the plots
`pareto-<task>.png` and `pareto-<task>-dark.png`.

## How the runs were made

Each run deploys the AwsBench bundle with AgentEnv on a Linux host, arms the gateway clock at the task's start (02:40 ET
for T01, 08:15 ET for T03, 13:12 ET for T04, 10:30 ET for T05) and prompts the solver once with the page (in T05, the
shift handover). The solver is Claude Code driving Claude Opus 5.5, Claude Sonnet 5.5, Claude Haiku 5.5 or Claude Fable 5.2
through a model endpoint, with its built-in tools off, so it acts only through the environment's `aws` and `workspace`
MCP tools; for GPT-6 Astra it is the Codex CLI agent (0.147.0, `codex exec`) with the same tools, whose run folders keep
its event stream as `trajectory-1.json` instead of a Claude Code session. A
post-turn hook lets up to ten minutes of virtual time pass between turns and wakes the agent with what arrived. The
episode ends at DONE, at the call budget or at the window's end (on the T03R1 build the hook missed `RESULT: DONE`;
see the note under the T03 results); the world is then fast-forwarded to the horizon (05:10 for T01, 16:15 for T03, 16:12 for T04, 14:00 for T05) and graded by the task's `artifacts/t0<n>-grade/verify.py`, whose judges
(T01's C11 handoff note, T03's C13 root-cause write-up and C18 public status text, T04's C12 handoff summary, T05's C12
#sre handover) are one LLM, Claude Sonnet 5.5 by default, given a fixed fact list.

Each task's reference is its scripted oracle path (`tests/e2e/driver.py` for T01, `tests/e2e/t03_oracle.py` for T03),
replayed by `tools/build_hub_data.py` through the world package in process with in-memory AWS and workspace fakes: the
same physics, timeline and colleagues, timed by the same meter, with waits run to their full length. On a deployed env
T01's path grades D12 1.0; its check marks here are the ones the path is built to pass, and its colleagues'
support-desk tickets are left out because the fakes give them their own ids. T03's outcome checks and its runbook check (C16) are
computed by `t03-grade`'s own rules on the replayed world; its other marks are the ones the path is built to pass.

T03's world export keeps host counts per minute, not each host's state. `tools/t03_replay.py` replays each T03 run's
own calls (the AWS writes, fleet commands and restarts in its action log, at the times the meter charged them) through
the world package in process and records every host's state, a sample host's threads and the scheduled hold's state
every 30 s; for every T03 run here the replay's per-minute host counts match the run's export at every minute.

## Caveats

- **Scrubbed.** Every file passed `tools/export_run.py`'s scrub and its check: no keys, model-endpoint or internal
  hostnames, local paths or endpoints, IP addresses outside the scenario's own fictional ones, or AWS account ids other
  than the emulator's `000000000000` (earlier builds stamped the AWS documentation's example account in world ARNs, and
  T03's scenario names a fictional account of its own; both are rewritten). Loopback addresses in simulated host
  output read `localhost`. The allowed IP ranges are `10.20.0.0/16`, `203.0.113.0/24`, T03's fictional fleet in
  `10.40.0.0/16` and literals the scenario's own files define.
- **Builds.** The R8 and T03 run folders' build files were left blank by the run script; the commit shown is the
  branch's head when each run started.
- **Heuristics in the viewer JSON.** Which checks a call "bears on" is read from its tool and arguments, not from the
  grader; the grade's own evidence is in `grade.json`.
- **No T04 or T05 reference.** Their scripted oracles were graded on deployed envs (D12 1.000 each, with the
  shortcut and trap paths each task's card lists), but those exports were not kept, and neither task has an in-process
  replay yet, so the viewer has no reference to compare their runs with.
- **One known T04 realism defect, no grade effect.** ECS's describe calls for three routing services come from the
  emulator unpatched (no running tasks), although the world serves their traffic; only the Haiku runs called them.
- **Calibration is open.** T04's design aimed for Opus near 15–25 %; on its final build Opus passes 4 of 5, and T05's
  Opus runs pass every time. T01 and T03 are meant to sit near a 10–18 % frontier pass rate. On the hardened build Opus
  passes T01 in 10 of 12 runs and T03 in 6 of 6, Claude Fable 5.2 and GPT-6 Astra pass all six of their T01 runs,
  Claude Sonnet 5.5 passes 8 of 16 on slightly later builds, and Claude Haiku 5.5 passes none of six. Adding difficulty is the task owner's call.
- **The judges are noisy.** In T01, C11 decides about six points; in T03, C13 and C18 decide twelve; in T04 and T05,
  C12 decides eight and ten.
- **Costs.** The cost of a run is the solver's own model spend; Claude Code reports its session's cost, which for the
  T03R1 runs includes turns the build's hook added after the agent's `RESULT: DONE`. The judge calls (three per judged
  check) are not included.

## License

The task content, runs and viewer data are [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/). The environment's
code is Apache-2.0. AWS Emulator is AWS's own (CC0) and is not redistributed here.
