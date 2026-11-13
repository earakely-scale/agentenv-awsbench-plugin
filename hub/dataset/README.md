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
pass and no cap applies.

### Task T03, Thread Ceiling

The agent is again Vantleaf's primary on-call SRE, paged at 08:15 Eastern on Monday, November 16, 2026: the ledger
end-to-end canary says synthetic payment events are no longer reaching the ledger. At 06:05 a pre-approved capacity
change grew streamroute, the fleet that routes payment events to the ledger, from 120 to 132 hosts; each host keeps two
threads per peer under a 1,024-thread limit, and as the hosts learned their new peers their shard-map builds failed and
routing stopped. The agent has to find that, get the fleet routing and the ledger caught up before settlement, keep
the cause removed through the horizon, and run the incident process and communications, under change control and with
colleagues pressing their own fixes. 220 tool calls; the window closes at 13:00 ET and the world is graded at 16:15. A
run passes when the core outcome checks (C02 to C06) and the routing check (C15) pass and no cap applies. Three
rounds of T03 runs are here; the latest, T03R3, is on build `e61297b`.

## Results

<!-- results -->
**T01 · The Empty Ledger Endpoint**

| Run | Model | Build | D12 graded | Pass | Checks lost | Gates fired | D12 final | Calls | End |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| `R10-opus-1` | Claude Opus 5.5 | `5095dda` | 1.00 | pass | none | none | 1.00 | 112 | DONE 03:56:39 |
| `R10-opus-2` | Claude Opus 5.5 | `5095dda` | 1.00 | pass | none | none | 1.00 | 113 | DONE 04:00:03 |
| `R10-opus-3` | Claude Opus 5.5 | `5095dda` | 1.00 | pass | none | none | 1.00 | 109 | DONE 04:32:20 |
| `R10-sonnet-1` | Claude Sonnet 5.5 | `5095dda` | 0.96 | pass | C10 | none | **1.00** (was 0.96) | 95 | DONE 03:46:20 |
| `R10-sonnet-2` | Claude Sonnet 5.5 | `5095dda` | 0.94 | pass | C11 | none | 0.94 | 84 | DONE 03:49:35 |
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
| `reference` | Scripted oracle | `9b076a2` | 1.00 | pass | none | none |  | 27 | DONE 03:35:55 |

**Final grader** (`9b076a2`). Why a run's final D12 differs from the grade it got, or whose the points it still loses are (*fair*: the agent's; *environment*: a world or colleague defect fixed since):

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
| `T03R3-opus-1` | Claude Opus 5.5 | `e61297b` | 1.00 | pass | none | none | 1.00 | 130 | DONE 10:43:56 |
| `T03R3-opus-2` | Claude Opus 5.5 | `e61297b` | 1.00 | pass | none | none | 1.00 | 140 | DONE 10:43:30 |
| `T03R3-opus-3` | Claude Opus 5.5 | `e61297b` | 1.00 | pass | none | none | 1.00 | 138 | DONE 10:34:49 |
| `T03R3-sonnet-1` | Claude Sonnet 5.5 | `e61297b` | 0.40 | fail | C02, C03, C04, C10, C13 | G4 | 0.40 | 121 | DONE 10:39:44 |
| `T03R3-sonnet-2` | Claude Sonnet 5.5 | `e61297b` | 0.40 | fail | C11 (2.5 of 5) | G4 | **0.975** (was 0.40, now a pass) | 126 | DONE 10:37:09 |
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
| `reference-t03` | Scripted oracle | `9b076a2` | 1.00 | pass | none | none |  | 68 | DONE 10:53:05 |

**Final grader** (`9b076a2`). Why a run's final D12 differs from the grade it got, or whose the points it still loses are (*fair*: the agent's; *environment*: a world or colleague defect fixed since):

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
fixed since. Every run since R8 was also graded again by the final grader (`9b076a2`) from its own exports: **D12
final** is that score, and the note under each table says why it differs, or whose the points a run still loses are
(*fair*: the agent could have avoided the loss; *environment*: a world, colleague or grader defect fixed since, which
a regrade can't undo when it changed what the run did). A few runs per model and build show where a task bites, not a
pass rate; both tasks are easier than their designs' 10–18 % frontier pass target. Calls count
every charged call, `wait` included, as the meter counts them against the budget (250 in T01, 220 in T03). T03's
reference has its outcome checks and its runbook check (C16) graded by the grader's own rules on its replayed world;
its other marks are the ones the path is built to pass.

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

T01 runs are named `R<round>-<model>[-<n>]`, T03 runs `T03R<round>-<model>-<n>`; a round is one build of the
environment.

## How the runs were made

Each run deploys the AwsBench bundle with AgentEnv on a Linux host, arms the gateway clock at the task's start (02:40 ET
for T01, 08:15 ET for T03) and prompts the solver once with the page. The solver is Claude Code driving Claude Opus 5.5 or Claude Sonnet 5.5 through a model
endpoint, with its built-in tools off, so it acts only through the environment's `aws` and `workspace` MCP tools. A
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
- **Calibration is open.** T01 is meant to sit near a 10–18 % frontier pass rate; on the latest build Opus passed all
  three of its runs and Sonnet one of two, and the task is being tuned. Three of T03's five first runs pass.
- **The judges are noisy.** In T01, C11 decides about six points; in T03, C13 and C18 decide twelve.

## License

The task content, runs and viewer data are [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/). The environment's
code is Apache-2.0. AWS Emulator is AWS's own (CC0) and is not redistributed here.
