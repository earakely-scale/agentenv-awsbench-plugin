# The AwsBench environment

The parts of an AwsBench run in more detail than the [README](../README.md) gives. [CONTRACTS.md](../CONTRACTS.md) is the
source of truth for every interface named here.

## Servers

One AgentEnv `multi` env (`envs/awsbench`) over two MCP servers behind the AgentEnv gateway:

| Server | What it holds |
|---|---|
| `aws` | AWS Emulator (the Linux package, telemetry off, auth off, loopback only); the AWS CLI v1 in process with an action log, fault rules and coarse IAM per profile; the task's world (its physics, the event timeline, scripted colleagues and their approvals), which also answers AWS calls the emulator doesn't model; fleet tools on simulated hosts; the meter; `wait`; the horizon. |
| `workspace` | Chat, tickets, wiki, repo, mail, pager and status page, in memory with full history. Every call is charged through the `aws` meter. |

Inside the env the servers reach each other at `http://aws:$MCP_PORT` and `http://workspace:$MCP_PORT`. The gateway
proxies them at `/svc/mcp-aws/` and `/svc/mcp-workspace/`.

## The agent's tools

T01's solver (role `sre-primary`) gets these tools and no others; its agent's built-in tools are off:

| Group | Tools |
|---|---|
| Time | `get_time` (free), `wait` |
| AWS | `aws_cli`, `aws_api`, `aws_query` (logs, CloudTrail, DynamoDB), `aws_table_export` |
| Hosts | `fleet_list_hosts`, `fleet_inspect_host`, `fleet_exec`, `fleet_restart`, `fleet_job_status` |
| Chat | `chat_list_channels`, `chat_read`, `chat_search`, `chat_get_user`, `chat_post`, `chat_dm`, `chat_create_channel` |
| Tickets | `tickets_search`, `tickets_get`, `tickets_create`, `tickets_comment`, `tickets_update`, `tickets_transition`, `tickets_link` |
| Wiki | `wiki_search`, `wiki_get`, `wiki_history`, `wiki_update`, `wiki_restore`, `wiki_comment` |
| Repo (read-only) | `repo_list`, `repo_get`, `repo_read`, `repo_search`, `repo_log` |
| Mail | `mail_search`, `mail_read`, `mail_send`, `mail_reply`, `mail_forward` |
| Pager | `pager_list`, `pager_get`, `pager_oncall`, `pager_ack`, `pager_page`, `pager_note`, `pager_resolve` |
| Status page | `status_get`, `status_create`, `status_update` |

The AWS calls run under the `SRE-OnCall` permission set of the account (`--profile vl-prod/SRE-OnCall`). `fleet_exec`
runs commands on the simulated hosts, including the internal tools they carry (`epctl` for endpointd in T01) and
`dig`, `systemctl` and `journalctl`. The harness role gets one hidden tool, `awsbench_control`, which the post-turn
hook uses.

## Time

- The gateway clock is armed frozen at the task's start (`sync_env_clock`, rate 0). From then on only the `aws` server
  moves it, with the gateway's `PUT /clock/advance`.
- The meter charges every agent call by its class: a point read costs 20 s, a write 45 s, a broad query (or a command on
  more than one host) 75 s. `get_time` is free. `wait` jumps to the next event, up to 30 minutes, and wakes early when
  something addressed to the agent arrives.
- Every tool result carries a `_clock` banner with the time, the calls left and the new items addressed to the agent.
- Between the agent's turns the post-turn hook (`artifacts/awsbench-turn/hook.py`) lets up to ten minutes pass and wakes
  the agent with whatever arrived. It ends the episode on `DONE`, when the budget is spent, or at the window's end.
- After the agent stops, the `urn:scale:awsbench-horizon/v1` extension fast-forwards the world and the gateway clock to
  the horizon: 05:10 ET in T01, 16:15 ET in T03.

## The world

The world ticks every 10 s, and each scenario brings its own mechanics. In T01, checkout success follows which hosts can
resolve the ledger; ledger-writer's regime (healthy, degraded, collapsed) follows offered load against capacity; the
webhook backlog and the checkout outbox drain or grow with the dispatcher's concurrency and the replay rate the agent
can set. In T03, each of about 130 stream-routing hosts holds threads against its user's process limit, learns its
peers over time, rebuilds its shard map every 30 minutes and is ejected by its coordinator if it stays warming too long;
the Auto Scaling group, its scheduled actions and its scaling activities are served from the world's state, a shared
bootstrap service can be overloaded, and eight ledger cells fall behind or catch up with the hosts that route. The
physics is deterministic per seed, so a scripted path scores the same every time.

Colleagues are scripted characters with jobs and moods. Their lines and the agent's replies are matched by a
deterministic classifier, so the same reply gets the same answer. Some of them give or refuse approvals, which the
grader reads.

CloudTrail `LookupEvents` is answered from the action log, not the emulator: every write by the agent or the world, at
its virtual time.

## Grading

Each task has its own grader, run once by an `env_outcome_verifier` step with `score_aggregator: weighted_average`:
`artifacts/t01-grade/verify.py` and `artifacts/t03-grade/verify.py`. The grader reads both servers' `data/get` exports
(the world's state and recorded time series, the action log and the meter from `aws`; every workspace record with its
history) and never the agent's reasoning. It scores the task's checks and gates and returns one weight-1 `D12` row, so
the step's score is the capped total, plus weight-0 rows with the evidence for each check and gate.

The judged rows (T01's C11 handoff note; T03's C13 root-cause write-up and C18 public status text) run only when
`LITELLM_API_KEY` is set. They call the OpenAI-compatible endpoint at `LITELLM_BASE_URL` with the model
`AWSBENCH_JUDGE_MODEL` names (both tasks' graders default to `claude-sonnet-5-5`, Claude Sonnet 5.5), ask three
times and take the majority per item, and credit a fact only when the judge quotes it from the text. Without a key the
judged rows are skipped and the task's other communication checks carry their points.

The checks, gates and pass rule of each task are in [T01.md](T01.md) and [T03.md](T03.md).
