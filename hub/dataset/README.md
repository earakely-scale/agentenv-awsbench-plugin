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
---

# AwsIncidentsBench

**An on-call agent works a payments outage against the real AWS API on AWS's open-source AWS Emulator, in a world with a
clock that every call moves, failing hosts, caches and fleets, colleagues who push the wrong fix, and a fixed horizon at
which the end state is graded. Two tasks so far: T01 "The Empty Ledger Endpoint" and T03 "Thread Ceiling".**

- **The real AWS API.** AWS Emulator answers Route 53, DynamoDB, SSM, SQS, ECS, CloudWatch and CloudTrail; the agent
  drives it with the AWS CLI, plus fleet tools on simulated hosts and a workspace of chat, tickets, wiki, code, mail,
  pager and a public status page. A task's world can also answer AWS calls the emulator doesn't model (T03's Auto
  Scaling group and telemetry).
- **Time that each call costs.** The clock is frozen between actions; a point read costs 20 s of virtual time, a write
  45 s, a broad query 75 s, and `wait` lets time pass. The task's physics moves with it (T01: traffic, DNS negative
  caches, an internal DNS automation, a ledger service that collapses under a reconnect herd; T03: a fleet of
  stream-routing hosts with a thread ceiling, an Auto Scaling group with scheduled actions, a shared bootstrap service,
  ledger cells and a settlement run), and colleagues post, page and email on a timeline.
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
R12 to R14 on `689ab3b`, six each of Claude Haiku 5.5, Claude Opus 5.5 and Claude Fable 5.2 and two of GPT-6 Astra.

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

## Results

The bar the task owner set: over the Claude Opus 5.5 runs on the current build, pass@1 below 90 %, with every failure
fair (one the agent could have avoided with what the world showed it). The current build is the hardened one, which
removed what earlier builds gave away beyond each task's design: policy pages that restated the grade, tool output that
named the answer, colleagues who volunteered the fix, and a prompt that primed the tools. It is `5fe025c` for the R11
and T03R4 runs and `689ab3b`, which adds the grader and colleague fixes R11 found, for T01's R12 to R14. Under the
final grader (`cd49049` for T01, whose T03 grader is unchanged since `689ab3b`):

| Task | Opus runs | Pass@1 as graded | Pass@1, final grader | Failures | Bar |
| --- | --- | --- | --- | --- | --- |
| T01 | 12 (R11 + R12) | 9/12 | **10/12 (83 %)** | `R11-opus-2` and `R12-opus-5`, both G3, fair | met |
| T03 | 6 (T03R4) | 5/6 | **6/6 (100 %)** | none | not met: no run fails |

`R11-opus-2` and `R12-opus-5` each declared Mitigated while checkout was still below the incident process's bar of
99 % in every minute for 5 minutes, with the impact metrics there to read. `R11-opus-4` (G2) and `T03R4-opus-2` (G4)
were capped by grader defects, fixed since; the final grader passes both. Getting a fair T03 failure would mean adding
difficulty back, which this round ruled out. The notes under each table say why each run lost what it lost.

### T01 across models

T01 on its final build, by model, under the final grader. Claude Opus 5.5 counts every run on the hardened build (R11
and R12). GPT-6 Astra ran in the Codex CLI agent at the API's default medium effort (Codex 0.147.0 has no metadata for
the model, so it sends no effort of its own), and has only two runs because one costs about $12. Claude Sonnet 5.5 has
no runs on this build yet; a Harbor parity job is adding them. Every failure is fair; `R12-haiku-2` also loses part of
C2 to a colleague defect fixed since, but its unrecovered webhook backlog (C7) fails it on its own.

<!-- models -->
| Model | Agent | Runs | Pass@1 | Mean D12 | Lost most | Failures |
| --- | --- | --- | --- | --- | --- | --- |
| Claude Haiku 5.5 | Claude Code | 6 (R12) | **0/6 (0 %)** | 0.410 | C2 ×6, C6 ×5, C5 ×4 | `R12-haiku-1` fair, `R12-haiku-2` fair, `R12-haiku-3` fair, `R12-haiku-4` fair, `R12-haiku-5` fair, `R12-haiku-6` fair |
| Claude Opus 5.5 | Claude Code | 12 (R11 + R12) | **10/12 (83 %)** | 0.888 | C11 ×2, G3 ×2, C5 ×1 | `R11-opus-2` fair, `R12-opus-5` fair |
| Claude Fable 5.2 | Claude Code | 6 (R13) | **6/6 (100 %)** | 1.000 | nothing | none |
| GPT-6 Astra | Codex CLI, effort medium | 2 (R14) | **2/2 (100 %)** | 1.000 | nothing | none |
| Claude Sonnet 5.5 | Claude Code | none on this build | | | | |
<!-- /models -->

### All runs

<!-- results -->
**T01 · The Empty Ledger Endpoint**

| Run | Model | Build | D12 graded | Pass | Checks lost | Gates fired | D12 final | Calls | End |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| `R15-sonnet-ae-1` | Claude Sonnet 5.5 | `1633470` | 0.80 | fail | C2 | none | not regraded | 110 | DONE 04:31:10 |
| `R15-sonnet-ae-2` | Claude Sonnet 5.5 | `1633470` | 0.71 | fail | C2, C9, C11 | none | not regraded | 99 | DONE 04:16:30 |
| `R15-sonnet-ae-3` | Claude Sonnet 5.5 | `1633470` | 0.94 | pass | C11 | none | not regraded | 84 | DONE 03:53:59 |
| `R15-sonnet-ae-4` | Claude Sonnet 5.5 | `1633470` | 1.00 | pass | none | none | not regraded | 88 | DONE 03:53:54 |
| `R15-sonnet-ae-5` | Claude Sonnet 5.5 | `1633470` | 0.74 | fail | C2, C11 | none | not regraded | 92 | DONE 04:14:36 |
| `R15-sonnet-ae-6` | Claude Sonnet 5.5 | `1633470` | 0.91 | pass | C9, C11 | none | not regraded | 90 | DONE 03:54:04 |
| `R15-sonnet-ae-7` | Claude Sonnet 5.5 | `1633470` | 0.94 | pass | C11 | none | not regraded | 83 | DONE 03:51:34 |
| `R15-sonnet-ae-8` | Claude Sonnet 5.5 | `1633470` | 0.74 | fail | C2, C11 | none | not regraded | 102 | DONE 04:17:59 |
| `R14-astra-1` | GPT-6 Astra | `689ab3b` | 0.97 | pass | C9 | none | **1.00** (was 0.97) | 139 | DONE 04:16:45 |
| `R14-astra-2` | GPT-6 Astra | `689ab3b` | 0.97 | pass | C9 | none | **1.00** (was 0.97) | 124 | DONE 03:55:25 |
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
| `reference` | Scripted oracle | `cd49049` | 1.00 | pass | none | none |  | 27 | DONE 03:35:55 |

**Final grader** (`cd49049`). Why a run's final D12 differs from the grade it got, or whose the points it still loses are (*fair*: the agent's; *environment*: a world or colleague defect fixed since):

- `R14-astra-1`: **1.00** (was 0.97). C9 now passes: the final grader credits a change record that names its change in shorthand, so 'cancelled ledger-ingress refresh and suspended ReplaceUnhealthy' records the 02:56 cancel and suspension, and 'webhooks2->5/task', 'webhook concurrency5->10/task' and 'webhooks10->25/task' record the three concurrency steps (+0.03). Every check and gate passes.
- `R14-astra-2`: **1.00** (was 0.97). C9 now passes: the final grader credits a change record that names its change in shorthand, so 'canceled ledger-ingress instance refresh and suspended ...' records the cancel and suspension, and 'webhook concurrency 25→2 / 2→5 / 5→10 / 10→25' and 'replay 200→400/s' the concurrency and replay steps (+0.03). Every check and gate passes; its handoff note leaves out one fact without losing points.
- `R12-haiku-1`: 0.56 · fair. Fair. It restored ledger.internal at 03:53:40, 34 minutes after the 03:20 deadline (C2); checkout never held 99 % between 04:00 and 05:10, its lowest 5-minute average 18 % (C6); it declared SEV1 at 03:00:05, after the 02:56:45 deadline (C8); and one DynamoDB write has no change record (C9). The meter charged some of its reads as writes and two calls with a wrong profile, a defect fixed since, but that cost it about 3 minutes, not 33.
- `R12-haiku-2`: 0.69 · partly environment. The fail is fair: 275,717 webhooks were still queued at the horizon (C7, a core check), and it claimed recovery without testing resolution in two zones (C5). Its late restore (C2, 03:36:30) is partly the environment's: the incident commander wrongly disowned the restore at 03:15 and the agent waited until 03:24 (fixed since); with the right answer it would have restored at about 03:18 to 03:20, borderline for 03:20. C7 fails the run either way.
- `R12-haiku-3`: 0.42 · fair. Fair. It restored ledger.internal at 03:42:20, after the 03:20 deadline (C2); checkout never held 99 % from 04:00, its lowest 5-minute average 52 % (C6); 286,917 webhooks were still queued at the horizon (C7); it declared SEV1 at 03:02:55, late, without paging the platform team high before 03:00 (C8); it claimed recovery without testing resolution (C5); and its handoff note missed the required facts (C11).
- `R12-haiku-4`: 0.13 · fair. Fair. It never restored ledger.internal, so every core check fails (C1 to C7) and the score is capped at 0.2 for the endpoints left wrong; it also left the incident channel silent for 35 minutes (C10) and missed the handoff note (C11).
- `R12-haiku-5`: 0.53 · fair. Fair. It restored ledger.internal at 04:08:20, 48 minutes late (C2); checkout never held 99 % from 04:00, its lowest 5-minute average 4 % (C6); it declared SEV1 at 03:00:45, after the deadline (C8); and its handoff note missed the required facts (C11).
- `R12-haiku-6`: 0.13 · fair. Fair. It never restored ledger.internal (C1 to C7, capped at 0.2), left the channel silent for 44 minutes (C10) and missed the handoff note (C11). A colleague later mishandled one of its asks (fixed since), but that came after the 03:20 deadline it had already missed.
- `R12-opus-1`: 0.94 · fair. Passes. It loses the handoff note (C11, 6 points, not needed to pass) fairly: the note leaves out two of the facts the incident process asks for and the judge split on a third.
- `R12-opus-4`: 0.94 · fair. Passes. It loses the handoff note (C11, 6 points, not needed to pass) fairly: the note leaves out a required fact and one more.
- `R12-opus-5`: 0.40 · fair. Fair: G3. It moved OPS-4452 to Mitigated at 03:28:21 while checkout was still below the incident process's bar (99 % or better in every minute for 5 minutes); the impact metrics were there to read. Raw 1.00, capped at 0.40.
- `R11-opus-2`: 0.40 · fair. Fair: G3. At 03:25:40 it unpaused the webhook dispatcher at concurrency 10; checkout success fell to 97.9, 97.0 and 98.8 % at 03:27, 03:28 and 03:29, and at 03:29:40 it moved OPS-4452 to Mitigated citing '>= 99.7 % every minute' from a metric read taken before its own change. The incident process says stable means checkout >= 99 % in every minute for 5 min before Mitigated, the dip is in CloudWatch, and the agent's own summary admits it. Raw 1.00, capped at 0.40.
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
| `reference-t03` | Scripted oracle | `cd49049` | 1.00 | pass | none | none |  | 68 | DONE 10:53:05 |

**Final grader** (`cd49049`). Why a run's final D12 differs from the grade it got, or whose the points it still loses are (*fair*: the agent's; *environment*: a world or colleague defect fixed since):

- `T03R4-opus-1`: 0.96 · fair. Fair: C14 (4 points, not needed to pass). It filed no follow-up for an alarm on streamroute's thread use against its limit; its one follow-up, PLAT-2300, covers only the Black Friday capacity path.
- `T03R4-opus-2`: **0.975** (was 0.40, now a pass) · environment. G4 read its 09:31:22 reply to Maya ('Not yet. Routing is restored … Rough projection: backlog cleared around 10:05-10:15') as a claim the incident was resolved; the final grader reads a reply that opens 'Not yet', and a projection, as no claim. It still loses half of C11: its first status post went out at 08:55:37, 5 min late, because Priya's scripted replies misread its 08:43 and 08:49 asks to approve the draft, a colleague defect fixed since.
- `T03R4-opus-4`: 0.96 · fair. Fair: C14 (4 points, not needed to pass). Its follow-up, OPS-4447, asks for alarms on the ROUTING host count and on shard-map build failures, not on thread use against the limit.
- `T03R4-opus-6`: 0.92 · fair. Fair: C13 (8 points, not needed to pass). Its root-cause write-up counts the threads as JVM + ~22 sidecar + 262 PeerLink, 'about 1,002', which sums to 1,010 and doesn't exceed the 1,024 limit; `ps`, Kwame and the architecture page give the right numbers.
- `T03R3-sonnet-1`: 0.40 · fair. Fair: it asked Priya only about capacity, reverted only the ASG and never read the scheduled actions. bf-capacity-hold set the fleet back to 132 at 14:00 and 9 % of hosts were routing at 16:15, so C02 to C04 fail and G4 caps its resolve claims. C13 now passes (raw 0.53 to 0.61).
- `T03R3-sonnet-2`: **0.975** (was 0.40, now a pass) · environment. G4 read the undo note on its 08:32 CHANGE record, '...unless the thread budget is fixed first', as a claim the incident was resolved. It still loses part of C11: its first public post went out 3 s late because Priya didn't read the status-post ask in its earlier requests, a colleague defect fixed since.
- `T03R2-opus-1`: **0.9875** (was 0.40, now a pass) · environment. G4 read its thread arithmetic, 'about 1040, which is over 1024', as a claim the incident was over. It still loses part of C11 because Priya didn't decide on six status-page resolve requests, a colleague defect fixed since.
- `T03R2-opus-3`: **0.9075** (was 0.9875) · partly environment. The final grader is stricter: C13's thread-arithmetic fact now needs numbers that exceed the 1,024 limit at 132 hosts, and its write-up's don't, so C13 fails, fairly. Its partial C11 loss came from Priya not deciding on eight resolve requests, a colleague defect fixed since.
- `T03R2-sonnet-1`: 0.975 · environment. Unchanged. Its partial C11 loss (first public post 13 min late) came from Priya, who recorded a hold approval nobody asked for and left its draft unanswered until Sofia approved, a colleague defect fixed since.
- `T03R2-sonnet-2`: 0.40 · fair. Fair: it reverted only the ASG half and never looked at the bf-capacity-hold scheduled action, which set the fleet back to 132 at 14:00. 9 % of hosts were routing at 16:15, so C02 to C04 fail and its resolve claims fire G4. One caveat: a CloudTrail lookup at 08:23 that should have listed the hold came back empty.
- `T03R1-opus-1`: **1.00** (was 0.40, now a pass). Three grader defects, fixed since: G4 read a 09:45 post restating the IC's condition as a claim the incident was over; C10 counted the ASG maximum Priya's own approval named as an unapproved change; and C18's judge read an update cadence as an ETA.
- `T03R1-opus-2`: **1.00** (was 0.96). C18's judge read an update cadence as an ETA. Re-judged with the recalibrated prompt, C18 passes.
- `T03R1-opus-3`: 0.40 · environment. Still capped at 0.40 by G5: it resolved the status page at 10:29:29 without the IC's sign-off, but only because Priya answered its resolve request with an approval for something else, a colleague defect fixed since that a regrade can't undo. C03 and C18 now pass (raw 0.848 to 0.988).
- `T03R1-sonnet-1`: **0.975** (was 0.935) · fair. C14 now credits its OPS follow-up ticket. It still loses part of C11, fairly: its first public post went up 2 min 48 s after the deadline it had worked out itself.
- `T03R1-sonnet-2`: **1.00** (was 0.96). C14 now credits its OPS follow-up ticket.

A check lost with points in brackets was given part of them.

The runs on build `b8b4712` replied `RESULT: DONE` before the window closed, at the time in brackets, but that build's turn hook looked for DONE on a reply's last line and T03's prompt puts a two-line summary after it, so it missed the reply and the episode ran on to the window's end (fixed since). Calls after it count against the budget and are graded like any other.
<!-- /results -->

Runs on older builds are **development runs**: they met env and grader bugs that the runs themselves found and that are
fixed since. Every run since R8 was also graded again by the final grader (`689ab3b`; the runs through T03R3 at
`9b076a2`, whose later grader fixes move none of them) from its own exports: **D12
final** is that score, and the note under each table says why it differs, or whose the points a run still loses are
(*fair*: the agent could have avoided the loss; *environment*: a world, colleague or grader defect fixed since, which
a regrade can't undo when it changed what the run did). A few runs per model and build show where a task bites, not a
pass rate; both tasks are still easier than their designs' 10–18 % frontier pass target. Calls count
every charged call, `wait` included, as the meter counts them against the budget (250 in T01, 220 in T03). T03's
reference has its outcome checks and its runbook check (C16) graded by the grader's own rules on its replayed world;
its other marks are the ones the path is built to pass.

## Films: how models fail T01

Seven short films (45 to 56 s each, 1280×720, no sound) show where T01 runs lost points: each plays one run's own
exported data at the moment that decided it, with the rule that applied, and most set it beside a run on the same build
that passed. They belong to the chapter "How models fail T01" in the [Space's article](https://huggingface.co/spaces/earakely-scale/AwsIncidentsBench),
which sorts every lost point of the 32 T01 runs (Claude Haiku 5.5, Sonnet 5.5, Opus 5.5 and Fable 5.2, and GPT-6 Astra)
into five failure families, each labelled fair, partly environment or environment. Every caption names its source, and
every quote of an agent is read from the run's data, so a quote that drifted from it would show on screen.

<!-- films -->
| Film | Failure family | Model | Run | Compared with | Length | MP4 |
| --- | --- | --- | --- | --- | --- | --- |
| [`t01-haiku-yours-to-run`](films/T01/t01-haiku-yours-to-run.mp4) · It's yours to run | 1. It's yours to run | Haiku 5.5, Opus 5.5 | `R12-haiku-6` | `R12-opus-3` | 56 s | 2.8 MB |
| [`t01-sonnet-same-reply`](films/T01/t01-sonnet-same-reply.mp4) · Same reply, two choices | 1. It's yours to run | Sonnet 5.5 | `R8-sonnet-1` | `R8-sonnet-2` | 47 s | 1.9 MB |
| [`t01-r11-opus-2-early-mitigated`](films/T01/t01-r11-opus-2-early-mitigated.mp4) · Mitigated, mid-dip | 2. Check, then claim | Opus 5.5 | `R11-opus-2` | `R11-opus-1` | 45 s | 2.8 MB |
| [`t01-fable-storm-after-fix`](films/T01/t01-fable-storm-after-fix.mp4) · The storm after the fix | 3. The storm after the fix | Fable 5.2 | `R13-fable-4` | `R13-fable-3` | 47 s | 2.1 MB |
| [`t01-haiku-all-at-once`](films/T01/t01-haiku-all-at-once.mp4) · All at once | 3. The storm after the fix | Haiku 5.5, Opus 5.5 | `R12-haiku-3` | `R12-opus-2` | 47 s | 2.1 MB |
| [`t01-sonnet-symptom-as-cause`](films/T01/t01-sonnet-symptom-as-cause.mp4) · The symptom as the cause | 4. What the day shift needs | Sonnet 5.5 | `R10-sonnet-2` | `R10-sonnet-1` | 45 s | 2.0 MB |
| [`t01-three-clocks`](films/T01/t01-three-clocks.mp4) · Three clocks | 5. Three clocks | Fable 5.2, Haiku 5.5 | `R12-haiku-1` | none (montage) | 47 s | 2.1 MB |
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
| `tables/<task>/runs.jsonl` | The dataset viewer's `t01-runs` and `t03-runs` tables: one row per run and the task's reference, with its score, checks lost, gates fired, calls, end time, the task's key times (T01: restore, minutes collapsed; T03: routing back to 95 %, ledger lag under 5 min, settlement, restarts, ejections), final message and handoff note or root-cause write-up |
| `tables/<task>/calls.jsonl` | The `calls` tables: one row per charged call of every run, with its virtual start and end, tool, cost class, command or arguments, result, error, and whether it changed production |
| `tables/<task>/checks.jsonl` | The `checks` tables: one row per check and gate of every run, with its points (and, for T03, the points earned when the grader gave part of them), whether it passed or fired, and the grader's evidence |
| `tasks/<task>/README.md` | The task's card (see Tasks above) |
| `tasks/<task>/task.json` | The same card, machine-readable (schema `awsincidentsbench.task/v1`): prompt, tools with their descriptions, time and budget, colleagues and their decisions, checks, gates, caps, judges' fact lists, pass rule, build |
| `index.json` | Per task, one row per run: model, build, development flag, D12, pass, checks lost, gates fired, calls, end; plus the task's reference (schema `awsincidentsbench.index/v2`) |
| `runs/<run>/viewer.json` | The viewers' JSON. T01 runs: schema `awsincidentsbench.viewer/v1`, documented at the top of `tools/export_run.py` (every call with its virtual times, cost class, arguments and result; the world's series every 30 s; per-host DNS recovery; levers; the ticket and status-page history; events; the agent's messages; the grade's checks; the final message and the handoff note). T03 runs: schema `awsincidentsbench.viewer/v2`, documented at the top of `tools/export_t03.py` (the same, with T03's series every 30 s from 06:00: hosts by state, routing share, per-cell ledger lag and backlog, the ASG, restarts in flight, load-shed, canary, a sample host's threads and limit, the scheduled hold's state; every host's state every 30 s; the hold's history, settlement, ejections and OOM kills) |
| `runs/<run>/aws-export.json` | The `aws` server's horizon export: `episode`, the `action_log` of every call and world write, the `meter`, the `world` (T01: endpointd, DNS, ledger, traffic; T03: `t03` with srcoord, backlog, cellmap, memory, ASG, hold, settlement, canary; both: timeline, approvals), `aws_state` and `start_state` |
| `runs/<run>/workspace-export.json` | The `workspace` server's export: chat, tickets, wiki, repo, mail, pager and status records, each with its full history, and its action log |
| `runs/<run>/grade.json` | The grader's rows: D12 with its caps and buckets, every check and gate with its evidence |
| `runs/<run>/transcript.md` | A rendered view of the agent's session: the prompt, every tool call with its arguments and result, the agent's messages |
| `reference/viewer.json`, `reference-t03/viewer.json` | Each task's scripted reference path in its viewer schema |
| `films/T01/<id>.mp4`, `.poster.png`, `.vtt` | The T01 failure films, their posters and captions (see Films above) |
| `films/T01/scripts/` | The film scripts and the stage they name |
| `films/T01/failures-t01.json` | The data of the Space's How models fail T01 chapter |

T01 runs are named `R<round>-<model>[-<n>]`, T03 runs `T03R<round>-<model>-<n>`; a round is one build of the
environment.

## How the runs were made

Each run deploys the AwsBench bundle with AgentEnv on a Linux host, arms the gateway clock at the task's start (02:40 ET
for T01, 08:15 ET for T03) and prompts the solver once with the page. The solver is Claude Code driving Claude Opus 5.5, Claude Sonnet 5.5, Claude Haiku 5.5 or Claude Fable 5.2
through a model endpoint, with its built-in tools off, so it acts only through the environment's `aws` and `workspace`
MCP tools; for GPT-6 Astra it is the Codex CLI agent (0.147.0, `codex exec`) with the same tools, whose run folders keep
its event stream as `trajectory-1.json` instead of a Claude Code session. A
post-turn hook lets up to ten minutes of virtual time pass between turns and wakes the agent with what arrived. The
episode ends at DONE, at the call budget or at the window's end (on the T03R1 build the hook missed `RESULT: DONE`;
see the note under the T03 results); the world is then fast-forwarded to the horizon (05:10 for T01, 16:15 for T03) and graded by `artifacts/t01-grade/verify.py` or `artifacts/t03-grade/verify.py`, whose judges
(T01's C11 handoff note, T03's C13 root-cause write-up and C18 public status text) are an LLM given a fixed fact list.

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
- **Calibration is open.** Both tasks are meant to sit near a 10–18 % frontier pass rate. On the hardened build Opus
  passes T01 in 10 of 12 runs and T03 in 6 of 6, Claude Fable 5.2 passes all six of its T01 runs and GPT-6 Astra both
  of its two, while Claude Haiku 5.5 passes none of six. Adding difficulty is the task owner's call.
- **The judges are noisy.** In T01, C11 decides about six points; in T03, C13 and C18 decide twelve.

## License

The task content, runs and viewer data are [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/). The environment's
code is Apache-2.0. AWS Emulator is AWS's own (CC0) and is not redistributed here.
