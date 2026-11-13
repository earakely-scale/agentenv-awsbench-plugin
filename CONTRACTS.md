# AwsBench build contracts

This file is the source of truth for the interfaces between the parts of AwsBench. Every part is built against it.
Change it only in the same change as every part that depends on what you change.

## What AwsBench is

An AgentEnv environment for AWS incident response. The AWS API comes from AWS's open-source AWS Emulator (CC0, the
Linux package from `https://downloads.aws.com/aws-emulator/packages/aws-emulator/LTS/latest/aws-emulator_<arch>.deb`,
arch `amd64` or `arm64`). Around it, AwsBench adds what an incident needs: time that passes, traffic, failing hosts,
colleagues, a timeline of world events and grading at a fixed horizon. The first task is T01 "The Empty Ledger
Endpoint" (see `docs/T01.md`).

Hard rules:
- **No agentenv-framework change.** Only existing steps, gateway routes and protocol-SDK features.
- **No Scale-internal code or servers.** Depend only on public PyPI packages (`agentenv-framework-protocol`, `awscli`,
  `boto3`, ...). Everything here is Apache-2.0.
- **No secrets in the repo.** Model keys come from the environment (`LITELLM_API_KEY`, `LITELLM_BASE_URL`) or from
  agent-env config, never from a file in git.

## Layout

```
src/agentenv_awsbench/bundles/awsbench/        the bundle (agent-env run <path>)
  envs/aws/                                    MCP server `aws` (build context = this folder)
    Dockerfile, requirements.txt
    awsbench_aws/
      server.py        protocol-SDK server: tools, extensions, data ops, internal routes   [aws-server]
      meter.py         per-call charge, wait, budget, gateway clock client                [aws-server]
      emulator.py      start/stop the emulator process, its endpoint and credentials       [aws-server]
      bridge.py        aws_cli / aws_api: awscli+botocore in process, hooks, faults        [aws-server]
      actionlog.py     the action log                                                      [aws-server]
      cloudtrail.py    CloudTrail LookupEvents from the action log                          [aws-server]
      ports.py         AwsPort and WorkspacePort implementations                           [aws-server]
      world/           physics, timeline, NPCs, hosts, epctl, dig; pure Python             [world]
      scenarios/t01.yaml   the T01 scenario data (seed calls, world config, timeline, NPCs) [world]
  envs/workspace/                              MCP server `workspace`
    Dockerfile, requirements.txt
    awsbench_workspace/
      server.py, store.py, modules/*.py                                                    [workspace]
      scenarios/t01/*.yaml   T01 workspace records (channels, tickets, wiki, mail, pager)  [workspace]
  envs/awsbench/env.toml                       the MultiEnv over `aws` and `workspace`     [bundle]
  artifacts/awsbench-turn/hook.py              post-turn hook                              [bundle]
  artifacts/t01-grade/verify.py                grade module                                [bundle]
  tasks/*.json                                 smoke, e2e, chaos and the T01 draft         [bundle]
tests/unit/                                    pure-Python tests per part
tests/e2e/                                     deploy-and-drive tests                      [bundle]
```

Each image's build context is its own env folder, so an image never imports from another folder. Tests import the
packages by putting the env folders on `sys.path` (`tests/conftest.py`).

Pins, every image: `python:3.12-slim`, `agentenv-framework-protocol==0.1.536` (let it choose its own `mcp`/`fastmcp`),
`awscli==1.46.3` and its `botocore`/`boto3` (aws image only), `pyyaml`, `httpx`.

## Time

- All virtual times are timezone-aware `datetime`. On the wire they are RFC 3339 strings with an offset. The task's
  zone is `America/New_York`.
- Every time an agent or the grader sees is a whole second: the meter rounds the times it moves to up to the next second
  (a wake can land between seconds), and the workspace stamps records in whole seconds.
- The gateway clock is armed by `sync_env_clock` at the task start with rate 0 (frozen). From then on, **only the `aws`
  server moves it**, with `PUT {gw}/clock/advance {"virtual_time": "<rfc3339>"}` (forward only).
- `{gw}` comes from the `env_get_time_url` argument of the `urn:agentenv:clock/v1` `sync_time` call, with the
  `/clock/time` suffix removed. If `getent hosts gateway` resolves, prefer `http://gateway:$MCP_PORT`.
- Without a gateway (unit tests, a standalone server), the meter keeps its own clock that starts at the scenario start.
- AWS times the emulator stamps read as virtual time: up to the end of seeding the scenario's `seeded_at`, later the
  virtual time of the call that made them. World-served times are UTC already.
- The scenario's `faults` (`bridge.Faults`) apply to the agent's AWS calls, direct or through epctl, and count them all;
  the world's own calls are its physics. T01 throttles Route 53 while acme-renewer runs, as OPS-4398 says.
- Both servers serve `@extension("urn:agentenv:clock/v1", op="sync_time")` with the signature
  `sync_time(env_get_time_url: str, timezone: str | None = None) -> dict`, as the only method of that URI.

## The meter (in `aws`)

A tool call is **charged** when the caller's role (`ToolContext`, from `_meta agentenv.io/role` or the
`AgentEnv-Role` header) is in `charged_roles` (default `["sre-primary"]`). The role `harness` and internal routes are
never charged.

Each charged call:
1. `begin(server, tool, args, role)`. It takes the agent lane (one asyncio lock) and refuses when the episode has
   ended, the budget is spent, or the clock is at `window_end`. It counts the call (`call_no`), picks its cost class,
   draws jitter, and advances the world and the gateway clock to `t_exec = min(now + cost, window_end)`, the cost
   rounded to whole seconds. It returns
   `{ok, call_no, t_exec, calls_left}`, or `{ok: false, refuse: "<message>"}`.
2. The tool executes at `t_exec` (effect time = done time).
3. `end(call_no, tool, args, result_summary, resolved)`. It records the call, lets the world observe it (action
   events, NPC routing, unless-cancels), releases the lane, and returns `{banner}`.

**Cost classes** (seconds): `free 0`, `point_read 20`, `write 45`, `broad 75`.
- Defaults per tool: every `*_get`, `*_read`, `*_list*`, `*_search`, `*_history`, `*_log`, `fleet_list_hosts`,
  `fleet_inspect_host`, `fleet_job_status` and read AWS operations (`Describe*`, `Get*`, `List*`, `Lookup*`,
  `TestDNSAnswer`) are `point_read`. Writes are `write`. `aws_query`, `aws_table_export`, Logs Insights queries and
  `fleet_exec` on more than one host are `broad`. `get_time` and `awsbench_control` are `free`. `wait` is charged as
  `free` plus the waited time.
- An ordered list of regex overrides on `fleet_exec` commands comes from the scenario; the first match wins.
- Jitter: `mode: eval` uses none. `mode: train` multiplies the cost by `1 + 0.25*(2u-1)`, with
  `u = int(sha256(f"{seed}|cost|{call_no}").hexdigest()[:16], 16) / 2**64`.

**Budget.** `max_calls` (T01: 250) counts every admitted charged call. Later calls get an error result
`"Call budget exhausted (250/250); write nothing more and reply DONE"` and are neither executed nor charged.
A call whose text can't run (an `aws_cli` command the CLI won't parse, a `fleet_exec` with shell operators or nothing to
run) is refused before the meter the same way, uncharged and uncounted; a command that runs and fails is charged.

**Banner.** Every tool result of either server is a single JSON text block (`structured_output=False`). The top-level
object carries, as its first key, `"_clock": {"now": "<HH:MM:SS ET>", "call": n, "calls_left": m, "new": [<inbound items since the last
call>]}`. An inbound item is `{"t", "kind": "dm|mention|page|email|channel|job_done", "from", "where", "preview"}`.
Only items addressed to the agent's identities appear.

**`wait`** (an `aws` tool): `wait(minutes: float | None = None, until: str | None = None)`. `until` takes `HH:MM`,
`HH:MM:SS` (ET, on the episode's date) or RFC 3339. It is clamped to 30 minutes and to `window_end`. It advances event
by event and returns early when a wake item arrives (`dm`, `mention`, `page`, `email` to the agent, `job_done`). It
returns exactly `{waited_s, now, woke_early, reason: "inbound"|"until_reached"|"window_end", inbound: [...]}`: the
timeline ids the world fired stay internal. Its description says which zone each part shows: banners and workspace
records Eastern, AWS and epctl UTC, and any tool that takes a time takes RFC 3339 with an offset.

## The world (in `aws`, package `awsbench_aws.world`, pure Python)

```python
class World:
    def __init__(self, scenario: dict, seed: str, aws: "AwsPort", ws: "WorkspacePort"): ...
    now: datetime                                      # the world's virtual time
    def start(self) -> None                            # arm at scenario["start"]; run seed-time physics state
    def next_due(self) -> datetime | None              # earliest timeline event or physics change
    def advance_to(self, t: datetime, wake: set[str] | None = None) -> dict
        # simulate in ticks of scenario["tick_s"] (10 s), firing timeline events, NPC replies and physics-driven
        # events in time order; stop early at the first delivered wake item whose kind is in `wake`;
        # returns {"now": t_reached, "woke": [items], "fired": [event ids]}
    def observe(self, call: dict) -> None              # an agent call record (see action log) at call["t"]
    def fleet_list_hosts(self, fleet: str | None = None, az: str | None = None) -> list[dict]
    def fleet_inspect_host(self, host: str) -> dict
    def fleet_exec(self, selector: str, command: str, caller: dict) -> dict   # {"hosts": [{host, stdout, exit_code}]}
    def fleet_restart(self, selector: str, batch_size: int, caller: dict) -> dict   # {"job_id"}
    def fleet_job_status(self, job_id: str) -> dict
    def serve_aws(self, service: str, operation: str, params: dict) -> dict | None
        # physics-served AWS reads (CloudWatch metrics and alarm states, Logs, SQS queue attributes, ...);
        # None means "let the emulator answer"
    def export(self) -> dict                           # see "Exports"
```

`configure` builds the world with `make_world(scenario, seed, aws, ws)`: T01's `World` above unless the scenario sets
`world.mechanics: <name>`, in which case `MechWorld` (`world/mech.py`, same public methods) runs the timeline and clock
loop over `awsbench_aws.world.<name>.Mechanics` (interface in `world/mech.py`; T03's in `docs/T03-build.md`).

`AwsPort` (implemented in `ports.py` over the emulator, faked in unit tests):
```python
class AwsPort(Protocol):
    def call(self, service: str, operation: str, params: dict, *, principal: str, world_made: bool) -> dict
        # a botocore-shaped call against the emulator, logged in the action log; raises AwsError(code, message)
```
The world reads and writes all AWS state it shares with the agent through this port: Route 53 records, the
`endpointd-plans` DynamoDB table, SSM parameters (kill switches, dispatcher concurrency), SQS. `epctl` writes made by
the agent use `principal` = the agent's role and `world_made=False`, so they are attributed to the agent; their
action-log rows carry the `call_no` and `role` of the `fleet_exec` that ran them, so a command and its writes are one
change.

`WorkspacePort` (implemented in `ports.py` over the workspace server's internal route, faked in unit tests):
```python
class WorkspacePort(Protocol):
    def apply(self, op: str, args: dict, at: datetime) -> dict    # see "workspace internal routes"
```

## The workspace (server `workspace`)

Seven apps, all state in memory, every record with full history. Tools (exact names):
- chat: `chat_list_channels`, `chat_read`, `chat_search`, `chat_get_user`, `chat_post`, `chat_dm`, `chat_create_channel`
- tickets: `tickets_search`, `tickets_get`, `tickets_create`, `tickets_comment`, `tickets_update`, `tickets_transition`, `tickets_link`
- wiki: `wiki_search`, `wiki_get`, `wiki_history`, `wiki_update`, `wiki_restore`, `wiki_comment`
- repo (read-only in T01): `repo_list`, `repo_get`, `repo_read`, `repo_search`, `repo_log`
- mail: `mail_search`, `mail_read`, `mail_send`, `mail_reply`, `mail_forward`
- pager: `pager_list`, `pager_get`, `pager_oncall`, `pager_ack`, `pager_page`, `pager_note`, `pager_resolve`
- status: `status_get`, `status_create`, `status_update`

List arguments (`to`, `cc`, `members`, `labels`) also take one comma-separated string, and `tickets_update` `fields` the
JSON text of an object, so a model's loose types come back as tool results, not schema errors.

The acting identity of a role comes from the scenario's `identities` (T01: role `sre-primary` is chat user
`sre-primary`, mail `oncall@vantleaf.com`, pager user `sre-primary`).

**Internal routes** (plain HTTP on the server's port, not MCP, not on the card):
- `POST /awsbench/world/apply {op, args, at}` → `{ok, id, deliveries: [inbound item, ...]}`. Ops:
  `chat.post_as {channel, user, text, thread_id?}`, `chat.dm_as {from_user, to_user, text}`,
  `mail.deliver {from, to: [..], cc?, subject, body, untrusted?}`, `pager.page_as {from_user, to, urgency, summary,
  incident?}`, `pager.ack_as {page_id, user}`, `tickets.comment_as {key, user, body}`,
  `tickets.update_as {key, user, fields}`. `deliveries` lists the inbound items this op created for the agent.
- `GET /awsbench/inbox?since=<seq>` → `{seq, items: [inbound item, ...]}`.
- `GET /awsbench/health` → `{ok: true}`.

**Meter delegation.** For a charged call, the workspace POSTs `{aws}/awsbench/meter/begin` before executing and
`{aws}/awsbench/meter/end` after, with `resolved` = `{app, recipients, channel, mentions, ticket, fields_changed,
external: bool}` and `result_summary` = `{id, error, text}` (the record's id, top level or one level down, which
timeline actions read as `{call.result.id}`); it stamps the record with `t_exec` and puts the returned banner into the result. `{aws}` is
`http://aws:$MCP_PORT` (override: `AWSBENCH_AWS_URL`). The `aws` server finds the workspace at
`http://workspace:$MCP_PORT` (override: `AWSBENCH_WORKSPACE_URL`).

## Setup and control

Extensions on `aws` (one op per URI, because `apply_server_config` calls the first method):
- `urn:scale:awsbench-scenario/v1` op `configure` `{scenario: "t01", seed: str, mode: "eval"|"train",
  npc_mode: "scripted", charged_roles?: [...]}`: loads the scenario (bundled in the image, or the file `data/add`
  staged), starts the emulator if needed, replays the seed calls, builds the world, takes the start baseline. Also
  calls workspace `/awsbench/world/apply` op `scenario.load {scenario}` so both servers load the same scenario.
- `urn:scale:awsbench-horizon/v1` op `run` `{until?: rfc3339}`: ends the agent's episode and fast-forwards the world
  and the gateway clock to the horizon (default: the scenario's). Idempotent.

Hidden tool on `aws`: `awsbench_control(op: str, max_s: float = 600)`, for the role `harness` only. Ops: `status`
(`{now, ended, calls_used, calls_left, window_end, horizon}`), `end` (marks the episode ended, records `done_at`),
`turn_end` (advance up to `max_s` or the next wake item; returns the inbound digest).

Data ops: both servers implement `@reset_data` (clear everything) and `@get_data` (the exports). `@add_data` accepts
scenario files (optional; scenarios also ship in the images).

`configure` refuses (an error, nothing changed) when the gateway clock is already past the scenario's start, as after a
`data/reset` of a used env: the gateway clock only moves forward, so the world would never agree with it. Deploy a fresh
env instead.

## Exports (`data/get`)

`aws` returns one `DataPart` with:
```
{"episode": {scenario, seed, mode, start, window_end, horizon, now, ended, end_reason, done_at, calls_used},
 "action_log": [ {seq, t, t_issue, call_no, role, principal, world_made, server, tool, service, operation, params,
                  error, exit_code, resources} ],
 "meter": [ {call_no, tool, cost_class, cost_s, t_issue, t_exec} ],
 "world": {"endpointd": {...}, "dns": {...}, "ledger": {...}, "traffic": {...}, "timeline": [...],
           "approvals": [...], "fleet_jobs": [...]},
 "aws_state": {"route53": {"<zone>": {"<fqdn>": [{SetIdentifier, Value, Weight, TTL}]}},
               "dynamodb": {"endpointd-plans": {"<pk>": item}}, "ssm": {"<name>": value}},
 "start_state": <the same aws_state shape, taken at configure>}
```
`workspace` returns one `DataPart` with `{"collab": {"chat": ..., "tickets": ..., "wiki": ..., "mail": ...,
"pager": ..., "status": ...}, "action_log": [...]}`, every record with its history and timestamps.

**CloudTrail.** `LookupEvents` (through `aws_cli`, `aws_api` and `aws_query source=cloudtrail`) is answered from the
action log, not the emulator: every logged write (not a read) by the agent or the world, including epctl's writes, newest
first, at its virtual time, with `EventName`, `EventSource` (`<service>.amazonaws.com`), `Username`, `Resources`,
`ReadOnly: "false"` and a `CloudTrailEvent` JSON carrying `requestParameters`. It honours `StartTime`/`EndTime`, one
`LookupAttributes` entry, `MaxResults` (at most 50) and `NextToken`. The seed's pre-incident writes are in it too, at the
times their items record (`applied_at` by `applied_by`, else `created_at` by the planner), and a seeded delete one
enactor step after the last apply, by the enactor of the newest generation. That cleanup first deletes, in Route 53, the
records the deleted plan carries for a name an `applied#` pointer still names (T01: the six `g48177-*` ledger records
enactor-b had upserted). A world write is logged at the world's time. Every account id, in CloudTrail and in the world's
ARNs, is `awsbench_aws.account.ACCOUNT`, the emulator's own, so they agree with the ARNs the emulator returns. A scenario
may declare its own `account` (T03: vl-prod's 444444444444); the aws server then shows that id in place of `ACCOUNT` in
every tool result and maps it back to `ACCOUNT` in the arguments of `aws_cli`, `aws_api` and `aws_query`, so the agent
sees one account and the exports keep `ACCOUNT`. T01 declares none.

`exit_code` is set on the rows of `fleet_exec` and `fleet_restart`: the highest exit code over the hosts (1 when the
selector named no host), and `error` says which hosts failed when it is non-zero, so a failed command is never read as a
change. Other rows carry `null`.

The world fills `world.*` with series the grader reads; each series is a list of `[rfc3339, value]`:
- `endpointd.ledger_correct_series` (bool per tick), `endpointd.endpoints_correct_series` (int per tick, out of 41),
  `endpointd.kill_switch_timeline` (`[{t, switches: {a,b,c,planner}, processes: {...}}]`)
- `dns.per_host_recovered_at` (`{host: rfc3339 | null}`)
- `ledger.collapsed_seconds_total`, `ledger.regime_timeline`
- `traffic.checkout_success_1m`
- `timeline`: `[{id, due_at, fired_at, status: fired|cancelled|skipped, deliveries}]`
- `approvals`: `[{t, by, decision: APPROVED|DECLINED, change, scope}]`
