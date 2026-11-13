# The awsbench bundle

AWS incident-response tasks on AWS Emulator. `envs/awsbench` is a `multi` env over two MCP servers behind one gateway:

- `aws`: the emulator, the simulated world (hosts, traffic, timeline, colleagues) and the meter that moves the clock;
- `workspace`: chat, tickets, wiki, repo, mail, pager and status page.

Inside the env the servers reach each other at `http://aws:$MCP_PORT` and `http://workspace:$MCP_PORT`. These are
the compose service names, which come from each server's `environment_name`, so their `@environment_card` names must
stay `aws` and `workspace`. The gateway proxies them at `/svc/mcp-aws/` and `/svc/mcp-workspace/`.

## Tasks

| Task | What it does |
|---|---|
| `smoke` | Deploys, configures T01 (seed `smoke-1`), arms the clock at 02:40 ET, sets tool access, runs the horizon and grades. No agent: the score is the do-nothing baseline (about 0.05). |
| `e2e-env` | The same setup and nothing else. Run it with `--keep` and drive it with `tests/e2e/driver.py` or `tests/e2e/chaos.py`. |
| `t01-draft` | The full T01 task: the solver (`sre-primary`, built-in tools off), the post-turn hook, the horizon, the grade and a snapshot. |

## Artifacts

- `awsbench-turn/hook.py`: the `prompt_agent` post-turn hook. It ends the episode on `DONE`, when the budget is spent,
  or at the window end. Otherwise it lets up to ten minutes pass through `awsbench_control turn_end` and wakes the
  agent with what arrived. Its params are `env_id`, `role` (`harness`), `done_regex`, `turn_end_max_s`, `chunk_s` and
  `max_chunks`.
- `t01-grade/verify.py`: the T01 grade. Run it with `env_outcome_verifier` and `score_aggregator: weighted_average`.
  It returns one weight-1 `D12` row, so the step's score is the capped total, plus weight-0 rows with the evidence for
  each check and gate. The C11 handoff judge runs only when `LITELLM_API_KEY` is set. It uses `LITELLM_BASE_URL` and
  `AWSBENCH_JUDGE_MODEL` (default `claude-sonnet-4-5`).

## Running

```bash
export LITELLM_BASE_URL=...  LITELLM_API_KEY=...       # the solver's model endpoint (and the C11 judge)
agent-env run src/agentenv_awsbench/bundles/awsbench --dry-run
agent-env run src/agentenv_awsbench/bundles/awsbench --task smoke
agent-env run src/agentenv_awsbench/bundles/awsbench --task e2e-env --keep   # then tests/e2e/driver.py --gateway <url>
agent-env run src/agentenv_awsbench/bundles/awsbench --task t01-draft --model <model>
```

Runs use the local sandbox by default. `--sandbox modal_vm` (or another VM provider) runs them remotely.
