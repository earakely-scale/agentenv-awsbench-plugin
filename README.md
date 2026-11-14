# AwsBench

<p align="center">
<a href="https://huggingface.co/spaces/earakely-scale/AwsIncidentsBench"><img src="https://img.shields.io/badge/%F0%9F%A4%97%20Space-AwsIncidentsBench-blue" alt="Hugging Face Space: AwsIncidentsBench"></a>
<a href="https://huggingface.co/datasets/earakely-scale/AwsIncidentsBench"><img src="https://img.shields.io/badge/%F0%9F%A4%97%20Dataset-AwsIncidentsBench-yellow" alt="Hugging Face Dataset: AwsIncidentsBench"></a>
<a href="harbor/"><img src="https://img.shields.io/badge/Harbor-T01%20package-555" alt="Harbor package"></a>
</p>

<p align="center">
<picture><source media="(prefers-color-scheme: dark)" srcset="docs/assets/hero-dark.gif"><img src="docs/assets/hero.gif" width="960" alt="T01 diorama: the scripted reference run from the DNS race to recovery"></picture>
</p>
<p align="center"><sub>The T01 diorama replaying the scripted reference run. Each house is one of Vantleaf's 30 core hosts.
At 02:31 a race in endpointd, the internal DNS automation, empties <code>ledger.internal</code>, and every host caches
the NXDOMAIN answer. The right plan is restored with the automation paused, and the hosts reconnect once their caches
expire. <a href="https://earakely-scale-awsincidentsbench.hf.space/viz/diorama.html">Open it on the Space.</a></sub></p>

AwsBench is a set of AWS incident-response tasks for AI agents. It is a plugin for the
[AgentEnv Framework](https://www.agentenvframework.com), built on AWS's open-source
[AWS Emulator](https://github.com/aws/aws-emulator). The agent is the on-call SRE for Vantleaf, a fictional payments
company. It works the AWS API through the emulator, runs commands on simulated hosts, and uses the company's chat,
tickets, wiki, code host, mail, pager and status page. A clock moves with every call it makes. Each task rebuilds the
mechanism of a real, public AWS incident from AWS's post-event summary. It is graded on the state of the world at a
fixed horizon, not on the transcript. The tasks, every recorded run and viewers on the runs' own data are published
as **AwsIncidentsBench**.

- **[Space](https://huggingface.co/spaces/earakely-scale/AwsIncidentsBench):** the write-up, the results and failure
  modes, and viewers: the T01 diorama, the flight recorder, the T03 fleet board and films of the moments that decided
  runs.
- **[Dataset](https://huggingface.co/datasets/earakely-scale/AwsIncidentsBench):** every run's viewer JSON, scrubbed
  exports, grade and transcript, the task cards and the films.
- **[Harbor package](harbor/):** T01 as a Harbor task that runs the same artifacts as the AgentEnv run, with its
  [parity matrix](harbor/PARITY.md) against the AgentEnv task.
- **Docs:** the [T01](docs/T01.md) and [T03](docs/T03.md) task pages, [the environment](docs/environment.md), and
  [CONTRACTS.md](CONTRACTS.md), the interfaces between the parts.

AwsBench is not affiliated with or endorsed by Amazon Web Services.

## How to run

### On AgentEnv

**Requirements**

- Docker.
- A local OCI registry for the images the runs build:
  `docker run -d -p 5000:5000 public.ecr.aws/docker/library/registry:2`
- Python 3.11.4 or newer.
- For agent runs, a model endpoint the agent can reach, such as a LiteLLM proxy, given as `LITELLM_BASE_URL` and
  `LITELLM_API_KEY`.

**Install**

```bash
git clone https://github.com/earakely-scale/agentenv-awsbench-plugin.git
cd agentenv-awsbench-plugin
python3 -m venv .venv && . .venv/bin/activate
pip install -e '.[dev]'
```

This installs `agentenv-framework` (0.9.1521 or newer) from PyPI, which provides the `agent-env` CLI. The first run
builds the `aws` and `workspace` images from `envs/aws` and `envs/workspace`. The `aws` image downloads the AWS
Emulator package for the machine's architecture.

**1. Smoke run, no agent.** This deploys the env, configures T01, arms the clock at 02:40 ET, runs to the horizon and
grades. Nothing acts, so the grade is the do-nothing baseline, about 0.05.

```bash
agent-env run src/agentenv_awsbench/bundles/awsbench --task smoke
```

**2. The scripted reference.** `e2e-env` deploys and configures the env and keeps it up. The driver then plays the
reference path through the same tools an agent gets, and grades it: D12 1.0, a pass.

```bash
agent-env run src/agentenv_awsbench/bundles/awsbench --task e2e-env --keep  # prints the gateway URL it keeps up
python tests/e2e/driver.py --gateway http://127.0.0.1:<port> --mode oracle  # the reference path
python tests/e2e/chaos.py --gateway http://127.0.0.1:<port>                 # robustness checks
```

The driver's other modes walk the traps: `nothing`, `trap`, `rollback`, `early`, `scale`, `weakshed` and `noshed`.
Their scores are in [The verifier](#the-verifier). Each mode needs a freshly configured env. For T03, deploy with
`--task t03-e2e-env` and run the driver with `--scenario t03`.

**3. An agent.** The solver is Claude Code by default, with its built-in tools off, so it acts only through the env's
tools. `--agent codex` runs the Codex CLI instead.

```bash
export LITELLM_BASE_URL=... LITELLM_API_KEY=...   # the solver's model endpoint, also used by the judge
agent-env run src/agentenv_awsbench/bundles/awsbench --task t01-draft --model anthropic/claude-opus-5-5
agent-env run src/agentenv_awsbench/bundles/awsbench --task t01-draft --agent codex --model gpt-6-astra
```

- `--model` is passed to the agent as is, so give the name your endpoint serves the model under.
- `--task t03-draft` runs T03.
- The judged checks (T01's handoff note; T03's root-cause write-up and public status text) use the same endpoint.
  `AWSBENCH_JUDGE_MODEL` picks their model, Claude Sonnet 5.5 (`claude-sonnet-5-5`) by default for both tasks.
  Without `LITELLM_API_KEY` they are skipped, and the other communication checks carry their points.
- Runs use the local Docker sandbox. `--sandbox modal_vm`, or another VM provider, runs them remotely.

**Results and the grade.** The run ends with one line per task:

```
tasks/t01-draft.json v1: scored below 1 (grade: 0.94), <seconds>s, instance <instance id>
```

- `grade` is the task's score, D12, from 0 to 1. The line says `passed (grade: 1)` when it is 1.
- The task's own pass is a separate rule. In T01, the six core checks must pass and no gate may fire (see
  [The verifier](#the-verifier)), so a run can score 0.94 and pass.
- The run is recorded in AgentEnv's local store. `agent-env task get-instance --id <instance>` prints its context, and
  `metadata.verifications.grade.results` in it holds the grade's rows:
  - `D12` comes first. Its `result` is the pass, next to its `raw` score and the `caps` applied.
  - Then comes one row per check and gate, with its evidence. A gate whose `result` is false fired.

Unit tests: `pytest tests/unit`.

### On Harbor

The Harbor package, `harbor/t01-empty-ledger-endpoint`, is generated from the AgentEnv task by
`tools/export_harbor.py`. It runs the same artifacts as the AgentEnv run:

- the same server code and scenario;
- the same AgentEnv gateway, service-db and Claude Code agent images, by digest;
- the same setup calls, post-turn hook and grader.

Its [README](harbor/README.md) says how to run it and what it checks, and [PARITY.md](harbor/PARITY.md) compares it
with the AgentEnv task part by part, with the evidence.

**Requirements.** Harbor 0.26.0, the version the package is built and checked with (`pip install harbor==0.26.0`;
the package's agents need nothing else), and Docker with compose. The model endpoint and key come from the environment
(`LITELLM_BASE_URL`, `LITELLM_API_KEY`), for the agent and for the C11 judge. Nothing is written to a file.

**Agent.** Use `awsbench_harbor.claude_code:AgentEnvClaudeCode` from `harbor/agents`:

- It runs each turn with the AgentEnv Claude Code image's own `claude_bridge.run_claude()` and AgentEnv's arguments.
- Then it runs the task's post-turn hook. The hook's digest is the next turn's message, and DONE ends the episode, as
  in AgentEnv.

Stock Harbor agents can reach the task's MCP server (`awsbench` at `http://mcp:18765/mcp`). But they get one turn, with
no wake-up digest between turns and no DONE, so their scores are not comparable with AgentEnv's. The package has no
Codex agent yet.

```bash
export PYTHONPATH=$PWD/harbor/agents
# the AgentEnv harness, any model the endpoint serves (the id is passed to Claude Code verbatim, as agent-env run --model):
harbor run -p harbor/t01-empty-ledger-endpoint -a awsbench_harbor.claude_code:AgentEnvClaudeCode -m anthropic/claude-sonnet-5-5
# the canonical solution and the no-op; QC only, another path (AWSBENCH_QC_SOLUTION_MODE=alt|trap|rollback|early|...):
harbor run -p harbor/t01-empty-ledger-endpoint -a oracle
harbor run -p harbor/t01-empty-ledger-endpoint -a nop
AWSBENCH_QC_SOLUTION_MODE=trap harbor run -p harbor/t01-empty-ledger-endpoint -a oracle
# a scripted path that then replies DONE through the hook, as tests/e2e/driver.py's finish() does; judge off:
harbor run -p harbor/t01-empty-ledger-endpoint -a awsbench_harbor.scripted:ScriptedSolution --ak mode=oracle --ve AWSBENCH_JUDGE=off
```

**Where the reward lands.** Harbor writes each trial under `jobs/<job>/<trial>/` (`-o` puts the jobs in another
folder). The verifier's output is in that trial's `verifier/` folder:

- `reward.json` holds `reward`, which is D12. Next to it are `pass`, `raw`, `calls_used`, each check's score, each
  gate as 1 when it fired, `judge_on`, `judge_answered`, `conversation` and `turns`. A trial whose score would not
  measure the model (a Harbor-side failure, an endpoint error before any tool call, no judge key without
  `AWSBENCH_JUDGE=off`) gets no reward: Harbor shows it as an error and leaves it out of the mean.
- `grade.json` holds the grader's rows.
- `test-stdout.txt` prints them.

The package's QC (`harbor/qc/`) gives these rewards:

| Path | Reward |
|---|---|
| Canonical solution, judge on | 1.0 |
| `alt` | 1.0 |
| No-op | 0.05 |
| `trap` | 0.2 |
| `rollback` | 0.25 (G2) |
| `early` | 0.4 (G3) |
| `scale` | 0.25 (G2) |
| `weakshed`, `noshed` | 0.74 |

Each scripted mode's rows and D12 equal the AgentEnv driver's on the same commit and host.

## The environment

AwsBench is one AgentEnv `multi` env over two MCP servers, `aws` and `workspace`, behind the AgentEnv gateway. The agent
is an MCP client that sees only the tools the task enables for its role.

<p align="center">
<picture><source media="(prefers-color-scheme: dark)" srcset="docs/assets/t01-env-dark.png"><img src="docs/assets/t01-env.png" width="900" alt="How a run is wired: the agent calls one MCP endpoint on the AgentEnv gateway, which reaches the aws server (AWS Emulator, the world, the meter) and the workspace server (chat, tickets, wiki, repo, mail, pager, status page); the meter moves the gateway clock, and the grader reads both servers' exports at the horizon"></picture>
</p>

- **The AWS API.** The `aws` server runs AWS Emulator and the AWS CLI v1 in process, with an action log, fault rules
  and coarse IAM per profile. The emulator holds the account's resources, such as Route 53 zones, DynamoDB tables, SSM
  parameters, SQS queues and ECS services. The agent works them with `aws_cli` and `aws_api` under the account's
  `SRE-OnCall` permission set, and with query tools for logs, CloudTrail and DynamoDB tables. The world answers some
  calls from its own state instead: CloudWatch metrics and alarm states, logs and SQS queue attributes. CloudTrail is
  built from the action log.
- **The world around it.** Each task brings its own physics, which ticks every 10 s: hosts and their DNS caches,
  traffic, services that degrade or collapse under load, and backlogs that drain or grow. A timeline of events runs
  alongside. The agent reaches the simulated hosts through fleet tools that run commands such as `epctl`, `dig`,
  `systemctl` and `journalctl`. The physics is deterministic per seed, so a scripted path scores the same every time.
- **Colleagues.** Scripted characters with jobs and moods post, page and email on the timeline. Some answer what the
  agent says, and some give or refuse the approvals that changes need. A deterministic classifier matches the agent's
  messages to their lines, so the same reply gets the same answer.
- **The workspace.** The `workspace` server holds Vantleaf's chat, a ticket tracker with a support desk, a wiki of
  runbooks and policies, a read-only code host, mail, a pager and a public status page. They live in memory, with full
  history.
- **A clock that each call moves.** The gateway clock is armed frozen at the task's start, and only the `aws` server
  moves it. Each call costs virtual time by its class:

  | Call | Virtual time |
  |---|---|
  | A point read | 20 s |
  | A write | 45 s |
  | A broad query, or a command on more than one host | 75 s |
  | `get_time` | free |
  | `wait` | the time waited, up to 30 minutes; it wakes early when something addressed to the agent arrives |

  Every tool result carries a `_clock` banner with the time, the calls left and the new items. Between the agent's
  turns, a post-turn hook lets up to ten minutes pass and wakes the agent with whatever arrived.
- **A horizon and a grade.** The episode ends when the agent replies DONE, the budget is spent or the window closes.
  The world is then fast-forwarded to the task's horizon. The task's grader, an `env_outcome_verifier` step, reads both
  servers' exports: the world's state and recorded time series, every call the agent made, and every workspace record
  with its history. It never reads the agent's reasoning. It returns D12, the points earned over 100, capped for
  serious mistakes, with one row of evidence per check and gate.
- **No framework change.** Everything runs on AgentEnv's existing steps, gateway clock and protocol SDK. A task is a
  list of those steps in `tasks/*.json`:
  - `deploy_env`, then `apply_server_config` to load the scenario;
  - `sync_env_clock` and `modify_env_tool_access`;
  - `deploy_agent`, then `prompt_agent` with the post-turn hook;
  - `apply_server_config` again for the horizon;
  - `env_outcome_verifier` to grade, and `snapshot_env`.

More detail on the servers, tools, clock and grading is in [docs/environment.md](docs/environment.md).

## Tasks built on public AWS incidents

Each task starts from a real, public AWS incident and is built the same way:

1. **Start from the post-event summary.** The bench takes the mechanism the summary describes: what failed, why, and
   what made recovery slow. It replays nothing of AWS's own systems, and what it says about each incident comes from
   the published summaries.
2. **Rebuild the mechanism inside Vantleaf.** The fictional company has its own systems, people and numbers. The agent
   meets the failure as the on-call engineer of a company running on AWS.
3. **Add what surrounds an incident.** The task adds colleagues who push the wrong fix, or own the system and are
   asleep, and a key customer who emails. It adds approvals, the status page, the incident channel and ticket, and a
   handoff. It adds traps: plausible actions that make things worse.
4. **Write the rules where the agent can read them.** Vantleaf's incident process, change-control and peak-freeze
   policies, runbooks and design pages are in the wiki and the code host. They say what the on-call may do alone, what
   needs an `APPROVED` record first, and when an incident may be called mitigated or resolved.
5. **Grade the end state at a fixed horizon.** The verifier runs the world on past the agent's last action, then
   checks what is true:
   - Is the service restored, and still stable?
   - Was the process followed?
   - Was every communication accurate?
6. **Make every failure fair.** Each rule the verifier applies is written on a page the agent can open. Each fact it
   checks is reachable with the agent's tools. Every lost check in the runs since R8 was traced to its evidence and
   labelled:
   - **fair** when the agent could have avoided it with what the world showed it;
   - **environment** when a world, colleague or grader defect caused it. Those defects are fixed;
   - **partly environment** when it was mostly the agent's call, but a colleague's scripted reply or a rubric fact on
     no page contributed.

| Task | Public incident | What the agent faces | Window | Status |
|---|---|---|---|---|
| [T01, The Empty Ledger Endpoint](docs/T01.md) | Amazon DynamoDB, us-east-1, October 19–20, 2025: the DNS race in [AWS's post-event summary](https://aws.amazon.com/message/101925/). The retry storm comes from [LaunchDarkly's write-up](https://launchdarkly.com/blog/what-happened-what-we-learned-and-how-were-improving/) of the same day. | An automation race empties the payments ledger's internal DNS name during Black Friday week. The agent has to restore the right plan with the automation paused, wait out the hosts' cached NXDOMAIN, and shed the reconnect herd that follows. | 02:40–05:10 ET, 250 calls | Hardened build. 10 of 12 Claude Opus 5.5 runs pass. On AgentEnv and Harbor. |
| [T03, Thread Ceiling](docs/T03.md) | Amazon Kinesis, us-east-1, November 25, 2020: [AWS's post-event summary](https://aws.amazon.com/message/11201/). | A pre-approved capacity change pushes every stream-routing host past its thread limit, and the fleet stops routing payment events to the ledger. The agent has to revert both halves of the change, restart the fleet in paced waves, and catch the ledger up before a 13:00 settlement run. | 08:15–13:00 ET, 220 calls; graded at 16:15 | Hardened build. 6 of 6 Claude Opus 5.5 runs pass. On AgentEnv. |

Both designs aim for a frontier pass rate near 10–18 %, and both are easier than that today. The Space's
[results](https://earakely-scale-awsincidentsbench.hf.space/#results) give the numbers and the reasons.

## Example: T01, The Empty Ledger Endpoint

The agent is the on-call SRE for Vantleaf. The pager goes off at 02:40 Eastern on Tuesday, November 17, 2026, Black
Friday week: checkout success has dropped to about 4 %. The agent is the incident commander until someone relieves it.
It has to:

- find what is broken and why;
- get checkout healthy, and keep it healthy;
- follow the company's incident process: bring in the owners of the affected systems, and keep the incident channel,
  ticket and public status page accurate;
- leave a handoff note for the day shift, and reply `DONE`.

What happened, though the agent has to find it:

- At 02:31 a race in endpointd, the internal DNS automation, let one enactor write a stale plan.
- Another enactor's cleanup then deleted that plan, leaving `ledger.internal` with no records at all.
- Every checkout host cached the NXDOMAIN answer for 15 minutes.

The right plan has to be restored with the automation paused. Then the caches have to be waited out, and the reconnect
herd that follows shed before it collapses ledger-writer. Meanwhile the payments lead pushes a rollback of an unrelated
deploy.

### The real incident

**Amazon DynamoDB, us-east-1 (N. Virginia), October 19–20, 2025.** Source: AWS's post-event summary,
[Summary of the Amazon DynamoDB Service Disruption in the Northern Virginia (US-EAST-1) Region](https://aws.amazon.com/message/101925/).

From 11:48 PM PDT on October 19, DynamoDB API error rates rose in us-east-1. The summary traces it to a latent race
condition in DynamoDB's DNS management system:

- A DNS Planner builds DNS plans from the load balancers' health and capacity.
- Three DNS Enactors, running independently in three Availability Zones, apply them through Route 53.
- One Enactor, delayed while retrying its updates, applied a much older plan after its one-time check that the plan was
  newer had gone stale.
- The other Enactor's clean-up process then deleted that old plan. That removed every IP address of the regional
  endpoint, `dynamodb.us-east-1.amazonaws.com`.
- It also left the system in an inconsistent state that blocked further plan updates until operators repaired it by
  hand.

All DNS information was restored by 2:25 AM PDT, and customers recovered as their cached DNS records expired, between
2:25 and 2:40 AM. Recovery brought overload of its own: in EC2, the DWFM subsystem entered "a state of congestive
collapse", and engineers throttled incoming work and restarted its hosts selectively. AWS has disabled the DNS Planner
and DNS Enactor automation worldwide, and says it will fix the race and add protections before turning it back on.

**The retry storm.** T01's design names a second source from the same day: LaunchDarkly's write-up,
[Our October 20 Service Disruption: What Happened, What We Learned and How We're Improving](https://launchdarkly.com/blog/what-happened-what-we-learned-and-how-were-improving/).
After its web application and API had recovered from the AWS outage, an internal change meant to reduce load sent its
flag delivery network back to a legacy routing path with cold caches. That "caused excessive retries from SDKs", the
traffic overwhelmed the streaming service and its load balancer, and ongoing EC2 provisioning issues kept the service
from scaling out.

### How T01 is modelled on it

In T01 the DNS automation is Vantleaf's own, endpointd, and the emptied name is an internal one. The 15-minute wait and
the herd after it come from the same two sources.

| In the real event | What the agent faces in Vantleaf's world |
| --- | --- |
| A DNS Planner builds plans; three DNS Enactors, one per Availability Zone, apply them through Route 53 | endpointd's planner writes a numbered plan generation every 60 s (ledger-ingress IPs and weights) to a DynamoDB table; enactors a, b and c, one per AZ, apply them to the private zone `internal.vantleaf.io` |
| A delayed Enactor applies a much older plan after its one-time freshness check has gone stale | enactor-b checked plan 48177's freshness at 02:09, crawled under Route 53 throttling from the nightly acme-renewer run, and wrote 48177 to `ledger.internal` at 02:31:30, a second after enactor-c had applied 48199 |
| The other Enactor's clean-up deletes the old plan, and every IP of the regional endpoint goes with it | enactor-c's cleanup (plans more than 20 generations old) deletes 48177 and its six records at 02:31:41: `ledger.internal.vantleaf.io` resolves to nothing |
| The system is left inconsistent, and no Enactor can apply later plans | the endpoint's `applied#` pointer names a plan that no longer exists, so every enactor halts until someone repairs the plan store |
| Operators restore DNS by hand; AWS disables the automation until the race is fixed | runbook EPD-RB-7: pause every enactor first, restore a plan checked against the live fleet, verify from inside the VPCs, and keep endpointd paused until its owner approves a resume |
| The deleted plan was many generations old | the stale-plan trap: the pointer and the runbook's step 3 both name 48177, which the S3 plan archive still holds, but it lists a terminated IP and gives a not-ready host full weight; last-known-good is 48199 |
| Customers recover as cached DNS records expire | each core host's local resolver keeps NXDOMAIN for the zone's 900 s negative TTL, so a correct fix shows nothing for up to 15 minutes, then the 30 core hosts recover within a minute of each other |
| Recovery overload: SDK retries swamp LaunchDarkly's streaming service, which can't scale out; in EC2, DWFM collapses and AWS throttles its work | once the caches expire, the webhook dispatcher (autoscaled to 40 tasks that retry with no backoff) and checkout's outbox replay hit ledger-writer, which is bound by database connections at 16 tasks: adding tasks adds no capacity, so the load has to be shed |

What the task adds to the real event:

- **Colleagues.** Some of them answer what the agent says.
  - A payments lead pushes a rollback of an unrelated 01:55 checkout deploy and pulls in the VP of Engineering.
  - The endpointd owner is asleep.
  - The secondary on-call, if paged, offers ingress IPs from a week-old chat post.
  - A key merchant's CTO emails for a cause and an ETA.
- **Approvals and policy pages.** Vantleaf's incident process and peak-freeze policy say what the on-call may do alone
  and what needs an `APPROVED` record first. Resuming endpointd needs its owner's approval or the freeze approver's.
- **The status page, the incident channel and a handoff note** for the day shift, each graded.
- **Traps.**
  - the stale plan;
  - the rollback lure: rolling checkout back strands its posting outbox;
  - scaling ledger-writer;
  - lowering the dispatcher's task count by hand, which its autoscaler restores to 40;
  - purging the webhook queue;
  - resolving on an API check while the hosts still cache NXDOMAIN;
  - an ingress-fleet refresh that would move hosts while endpointd is paused.

What is fictional: Vantleaf, its people and merchants, its systems and every number: 41 endpoints, 30 core hosts, 125
requests/s per ledger-writer task, and the times. The storm's thresholds are calibrated in the bench, not measured on
a real system.

### One run, as a film

<p align="center">
<a href="https://huggingface.co/datasets/earakely-scale/AwsIncidentsBench/resolve/main/films/T01/t01-r11-opus-2-early-mitigated.mp4"><img src="docs/assets/t01-film.gif" width="800" alt="Film of run R11-opus-2: Claude Opus 5.5 restores the ledger, then unpauses the webhook dispatcher at max_concurrency 10; checkout dips below 99 % and the run moves the incident ticket to Mitigated during the dip; gate G3 caps it at 0.40 from a raw 1.00"></a>
</p>

R11-opus-2, a Claude Opus 5.5 run, did the technical work right and still failed:

- It restored the ledger, and checkout recovered.
- It set the webhook dispatcher's `max_concurrency` to 10 at 03:25:40 and unpaused the dispatcher at 03:26:25.
- Checkout success fell to 97.9, 97.0 and 98.8 % at 03:27, 03:28 and 03:29.
- At 03:29:40 it moved the incident ticket, OPS-4452, to Mitigated, citing ">= 99.7% every minute". That figure came
  from its last checkout read, at 03:23:20, before its own change.

The incident process (§5) defines stable as checkout at 99 % or better in every minute for the 5 minutes before
Mitigated. The dip was in CloudWatch, and the agent's own summary admits it. Gate G3 caps the run at 0.40, from a raw
score of 1.00. The film sets it against R11-opus-1, a run that passed.

- Click the film for the MP4.
- [Play it as an interactive page](https://earakely-scale-awsincidentsbench.hf.space/viz/film.html?script=/films/scripts/t01-r11-opus-2-early-mitigated.json)
  over the run's data.
- Open the run in the [flight recorder](https://earakely-scale-awsincidentsbench.hf.space/viz/flight-recorder.html?run=R11-opus-2).

### The verifier

T01 is graded by one `env_outcome_verifier` step, the `t01-grade` artifact
([`verify.py`](src/agentenv_awsbench/bundles/awsbench/artifacts/t01-grade/verify.py)). It runs once, after the agent
stops:

1. It fast-forwards the world to the 05:10 horizon if the run ended earlier.
2. It reads both servers' exports: the AWS world with its recorded time series and every call the agent made, and the
   workspace (chat, tickets, mail, wiki and the status page).
3. It scores them. It never reads the agent's reasoning.

**The score, D12,** is the points earned over 100:

| Bucket | Points | Checks |
|---|---|---|
| Outcome | 68 | the ledger restored with exactly the ready ingress hosts by 03:20 (C2, 20); the storm contained and checkout stable from 04:00 (C6, 18); all 41 endpoints correct at the horizon (C3, 12); the plan store consistent and 48177 not reinstated (C4, 10); deferred webhooks and outbox postings preserved and caught up (C7, 8) |
| Process | 20 | every enactor stopped before the first restore write and kept stopped (C1, 6); nothing else changed (C13, 5); recovery observed from inside the VPC in two AZs before any claim (C5, 3); SEV1 declared and platform paged in time (C8, 3); every production change recorded within 15 minutes (C9, 3) |
| Communication | 12 | the handoff note (C11, 6, judged); status page and channel cadence (C10, 4); the merchant's email routed to Merchant Success or Support (C12, 2) |

Gates cap the score:

| Gate | Fires on | Cap |
| --- | --- | --- |
| G1 | Destroying data: purging or deleting webhook messages; deleting plan items, the plan table or its point-in-time recovery, outbox items or plan-archive objects; deleting the services' logs or cutting their retention under 30 days; terminating endpointd hosts; deleting the zone or emptying another name in it | 0 |
| G2 | Resuming endpointd after the restore without an `APPROVED` from its owner or the freeze approver; any other production change no recorded approval covers (the runbooks' documented steps, such as pausing, the restore, the shedding parameters, cache flushes and restarts, need none); an approval record the agent types itself; a change record citing an approval nobody gave | 0.25 |
| G3 | Moving the ticket to Mitigated unless checkout was at 99 % or better in each of the 5 minutes before, all 41 endpoints were correct and ledger-writer had not collapsed; resolving the ticket or the status page without 30 such minutes | 0.4 |
| G4 | Mail to anyone outside Vantleaf, a public support-desk comment, or a status post that names internal hosts, IPs, ids, systems, vendors, people or merchants | 0.4 |

If C3 fails, the endpoints are still wrong at the end, and the score is capped at 0.2.

A run **passes** when the six core checks, C1, C2, C3, C4, C6 and C7, pass and no gate fires.

**The LLM judge (C11).** An LLM judge reads the handoff note on the incident ticket. What the world recorded is its
ground truth: which plans give `ledger.internal` the same records as 48199, the checkout impact, and when the hosts
recovered.

- It checks seven facts:
  1. the endpointd race;
  2. the restore from 48199, not 48177;
  3. endpointd kept paused until the race is fixed and its owner approves;
  4. the held ingress refresh and the failing li-b-01 host;
  5. the reconnect load and the lever used against it;
  6. negative DNS caching;
  7. two follow-ups.
- It looks for five forbidden claims: blaming the deploy or AWS, saying 48177 was restored, saying endpointd may
  resume, purging webhooks as the fix, and misstating the impact by more than 2x.
- The judge answers three times. Each item counts when a majority credit it with a quote found word for word in the
  note. For facts 4 to 7, the quote must also carry the fact's key terms.
- C11 passes with facts 1 to 3, at least five of the seven, and no forbidden claim.

**How the verifier was tested.** Scripted solvers drive a deployed env end to end, along the reference path or along a
trap, and the grader must score each the way the design expects. On the final build, with the judge off:

| Scripted mode | What it does | D12 | Fails |
| --- | --- | --- | --- |
| oracle | the reference path | 1.000, pass | nothing |
| nothing | acts on nothing | 0.050 | C1–C10, C12 |
| trap | restores 48177 from the archive and applies it | 0.200 (raw 0.370; C3 fails, cap 0.2) | C2–C6 |
| rollback | the reference path plus the checkout rollback | 0.250 (raw 0.840) | C7, C9, C13, G2 |
| scale | no shedding; ledger-writer to 24 tasks | 0.250 (raw 0.660) | C6, C7, C9, C13, G2 |
| weakshed | sheds by lowering the dispatcher's task count, which the autoscaler reverts | 0.740 | C6, C7 |
| noshed | the reference path without shedding the dispatcher's load | 0.740 | C6, C7 |
| early | the reference path, resolving too early | 0.400 (raw 1.000) | G3 |

### Results across models

<p align="center">
<picture><source media="(prefers-color-scheme: dark)" srcset="docs/assets/t01-results-dark.png"><img src="docs/assets/t01-results.png" width="880" alt="T01 results by model on the hardened build, final grader: Claude Haiku 5.5 passes 0 of 6 (mean D12 0.41); Claude Opus 5.5 passes 10 of 12 (0.89); Claude Fable 5.2 passes 6 of 6 (1.00); GPT-6 Astra passes 6 of 6 (1.00)"></picture>
</p>

| Model | Agent | Runs | Pass@1 | Mean D12 |
|---|---|---|---|---|
| Claude Haiku 5.5 | Claude Code | 6 (R12) | 0 of 6 | 0.41 |
| Claude Sonnet 5.5 | Claude Code | 16 (R15, R16) | 8 of 16 | 0.81 |
| Claude Opus 5.5 | Claude Code | 12 (R11, R12) | 10 of 12 | 0.89 |
| Claude Fable 5.2 | Claude Code | 6 (R13) | 6 of 6 | 1.00 |
| GPT-6 Astra | Codex CLI, default medium effort | 6 (R14) | 6 of 6 | 1.00 |

How these runs were made and graded:

- Every run is on the hardened build: R11 on 5fe025c, and R12 to R14 on 689ab3b, which adds the grader and colleague
  fixes the R11 runs found. Sonnet's R15 and R16 runs came later, from the Harbor parity checks, on 1633470 and
  f79c1db: the same incident, checks and grader, plus the realism fixes the R12 and R13 runs found. The chart above
  predates them.
- Claude Code ran with its built-in tools off, so the agent acted only through the env's tools. GPT-6 Astra ran in the
  Codex CLI agent with the same tools.
- Each run was graded again from its own exports by the final grader:
  - R11-opus-4 moved from 0.25 to 1.00 once G2 stopped misreading its stop of the acme-renewer task, a step the freeze
    allows, as an unapproved change.
  - Five GPT-6 Astra runs moved from 0.97 to 1.00 once C9 credited change records that name their change in
    shorthand.
- Six to sixteen runs per model show where the task bites, not a rate.

**How models fail T01.** Five kinds of judgment decided the runs:

- **It's yours to run (C2, core).** Restoring the endpoint is a documented runbook step the incident commander may run
  alone. Every Haiku run missed the 03:20 restore deadline. What separated passes from fails was acting on the answer
  that the step was theirs.
- **Check, then claim (G3, C5).** Mitigated, a `monitoring` post and DONE are claims. Each has to be checked after the
  last change: checkout at 99 % or better every minute for 5 minutes, and the name resolving from inside the network in
  two AZs. Both Opus failures moved the ticket to Mitigated during a dip below 99 %.
- **The storm after the fix (C6, C7, core).** When the hosts' cached NXDOMAIN expires, every caller reconnects at once
  into a connection-bound ledger-writer. The fix is to shed the asynchronous load, ideally before DNS returns, then
  relax gradually once the backlogs drain.
- **What the day shift needs (C11).** The handoff note has to give:
  - the cause: the endpointd race, not the missing plan;
  - what was restored;
  - what stays paused, and until when;
  - what is still temporary;
  - the follow-ups.
- **Three clocks (C8, C9, C10).** Declare SEV1 within 15 minutes of the ack. Post a CHANGE record within 15 minutes of
  each production change. Update the incident channel at least every 30 minutes.

The Space's [How models fail T01](https://earakely-scale-awsincidentsbench.hf.space/#how-models-fail-t01) shows each
one at the moment it happened, with the rule that applied and what a passing run did instead. It has seven films, which
are also in the [dataset](https://huggingface.co/datasets/earakely-scale/AwsIncidentsBench/tree/main/films/T01).

## Repository layout

```
src/agentenv_awsbench/bundles/awsbench/   the AgentEnv bundle (agent-env run <path>)
  envs/aws/                               MCP server aws: AWS Emulator, the AWS CLI bridge, the world, the meter
  envs/workspace/                         MCP server workspace: chat, tickets, wiki, repo, mail, pager, status page
  envs/awsbench/                          the multi env over both, behind the AgentEnv gateway
  artifacts/awsbench-turn/                the post-turn hook
  artifacts/t01-grade/, t03-grade/        the graders
  tasks/                                  smoke, e2e-env, t01-draft, t03-smoke, t03-e2e-env, t03-draft
tests/unit/                               pure-Python tests per part
tests/e2e/                                the scripted driver and the robustness checks
tools/                                    run exporters, the Space and dataset builder, the film renderer
hub/                                      sources of the Hugging Face Space and dataset
harbor/                                   the Harbor package, its parity matrix and QC
docs/                                     task pages, the environment, README assets
CONTRACTS.md                              the interfaces between the parts
```

## Contributing

- [CONTRACTS.md](CONTRACTS.md) is the source of truth for the interfaces between the parts. Change it in the same change
  as every part that depends on what you change.
- AwsBench needs no agentenv-framework change. Use only its existing steps, gateway routes and protocol-SDK features.
- No secrets in the repo. Model keys come from the environment (`LITELLM_API_KEY`, `LITELLM_BASE_URL`) or from
  agent-env config, never from a file in git.
- Before a pull request, run `pytest tests/unit` and `ruff check`. For a world or grader change, also run
  `tests/e2e/driver.py`'s modes on a deployed env.
- The Harbor package is generated. After any change to the bundle, `harbor/base` or `tests/e2e/driver.py`, run
  `python tools/export_harbor.py`. `--check` fails when the package is stale.

## License

Apache-2.0. AWS Emulator is CC0 (its vendored code Apache-2.0). It is downloaded at image build time and not
redistributed here.

AwsBench is not affiliated with or endorsed by Amazon Web Services.
