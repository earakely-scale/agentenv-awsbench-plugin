# R16: live Sonnet 5.5 runs, 8 per system, through a logging proxy

- **When and where.** Host 2, 2026-11-14, 04:54 to 05:10 UTC.
- **Order.** Interleaved, at most 3 runs per system at once.
- **Build.** Both systems ran from one checkout of this repo at f79c1db, with AWS Emulator 1.0.353-1 and
  agentenv-framework 0.9.1521.
  - Harbor: `harbor run -p harbor/t01-empty-ledger-endpoint -a awsbench_harbor.claude_code:AgentEnvClaudeCode -m
    anthropic/claude-sonnet-5-5` on Harbor 0.26.0.
  - AgentEnv: `agent-env run src/agentenv_awsbench/bundles/awsbench --task t01-draft --model
    anthropic/claude-sonnet-5-5`.
- **Endpoint.** Each run's `LITELLM_BASE_URL` pointed at a pass-through proxy on the Docker bridge, one port per run.
  The proxy forwarded each request unchanged to the same endpoint and key, and logged:
  - each request's headers (secrets as their length and a hash) and full body;
  - each response's status, headers and body, with its model and usage.

  The agent's turns and the C11 judge went through it on both systems. The judge ran with the R15 settings: on, the
  default model, 3 samples.
- **Logs.** The proxy's logs hold the model's full conversations and stay outside the repo.

## Per-run results

"48177 applied" is the time stale plan 48177's apply succeeded. "horizon" means the episode ran to 05:10. "Proxy $" is
the list price of the logged usage. "CLI $" is Claude Code's own `total_cost_usd`, which equals the agent part in every
run.

| Run | D12 | pass | checks lost | gate | calls | DONE at (ET) | ledger first correct (C2) | 48177 applied | conversation turns | model requests | judge answers | proxy $ agent + judge | CLI $ | agent s |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| Harbor 1 | 1.0 | yes | - | - | 79 | 03:48 | 02:53:20 | - | 1 | 40 | 3/3 | 0.51 + 0.12 | 0.51 | 121 |
| Harbor 2 | 0.19 | no | C2–C7, C10, C11 | - (cap 0.2) | 124 | horizon | never | 03:09:58 | 4 | 64 | 3/3 | 0.95 + 0.10 | 0.95 | 229 |
| Harbor 3 | 1.0 | yes | - | - | 80 | 03:52 | 03:02:20 | - | 1 | 38 | 3/3 | 0.52 + 0.10 | 0.52 | 122 |
| Harbor 4 | 0.71 | no | C2, C5, C11 | - | 101 | 04:23 | 03:35:40 | - | 1 | 49 | 3/3 | 0.69 + 0.11 | 0.69 | 168 |
| Harbor 5 | 0.74 | no | C2, C11 | - | 108 | 04:23 | 03:30:30 | - | 1 | 54 | 3/3 | 0.74 + 0.10 | 0.74 | 179 |
| Harbor 6 | 0.74 | no | C2, C11 | - | 114 | 04:26 | 03:35:50 | 03:21:39 | 1 | 58 | 3/3 | 0.82 + 0.13 | 0.82 | 203 |
| Harbor 7 | 0.97 | yes | C5 | - | 87 | 03:45 | 02:59:00 | - | 1 | 35 | 3/3 | 0.58 + 0.10 | 0.58 | 138 |
| Harbor 8 | 0.94 | yes | C11 | - | 99 | 04:09 | 03:12:40 | - | 1 | 47 | 3/3 | 0.65 + 0.10 | 0.65 | 160 |
| AgentEnv 1 | 0.74 | no | C2, C11 | - | 111 | 04:13 | 03:34:20 | 03:11:07 | 1 | 53 | 3/3 | 0.77 + 0.13 | 0.77 | 182 |
| AgentEnv 2 | 0.97 | yes | C9 | - | 88 | 03:52 | 02:58:50 | - | 1 | 38 | 3/3 | 0.56 + 0.12 | 0.56 | 134 |
| AgentEnv 3 | 0.71 | no | C2, C9, C11 | - | 118 | 04:31 | 03:55:20 | 03:03:52 | 1 | 64 | 3/3 | 0.88 + 0.11 | 0.88 | 215 |
| AgentEnv 4 | 0.19 | no | C2–C7, C10, C11 | - (cap 0.2) | 112 | horizon | never | 03:00:41 | 2 | 62 | 3/3 | 0.82 + 0.12 | 0.82 | 188 |
| AgentEnv 5 | 0.94 | yes | C11 | - | 98 | 03:50 | 03:01:40 | - | 1 | 41 | 2/3 (one not JSON) | 0.60 + 0.10 | 0.60 | 147 |
| AgentEnv 6 | 0.94 | yes | C11 | - | 80 | 03:45 | 02:57:00 | - | 1 | 38 | 3/3 | 0.51 + 0.10 | 0.51 | 123 |
| AgentEnv 7 | 0.91 | yes | C9, C11 | - | 105 | 03:53 | 03:01:30 | - | 1 | 47 | 3/3 | 0.65 + 0.09 | 0.65 | 164 |
| AgentEnv 8 | 0.74 | no | C2, C11 | - | 112 | 04:24 | 03:45:20 | 03:05:03 | 1 | 57 | 3/3 | 0.81 + 0.11 | 0.81 | 200 |

The C11 judge flagged the forbidden claim X2 ("restored from 48177") in exactly the runs that applied 48177, on both
systems.

## What reached the model, and what served it

Every request both systems sent is compared below: 785 agent requests and 48 judge requests.

| Compared, every request | Harbor (385 agent, 24 judge) | AgentEnv (400 agent, 24 judge) | Same |
|---|---|---|---|
| Path | `POST /v1/messages?beta=true` | the same | yes |
| Parameters | `model anthropic/claude-sonnet-5-5`, `max_tokens 128000`, `stream`, thinking adaptive (display `updates`), `output_config` effort `medium`, context management `clear_thinking_20251015` keep all | the same, one signature on every request | yes |
| System prompt, tools | one hash each, 57 tools | the same hashes | yes |
| Metadata | `user_id` {device_id, account_uuid, session_id}, one device and one session per run | the same shape | yes; the values are per run, and the endpoint reported dropping `metadata` on every request |
| Request headers | Claude Code 2.1.322's set, in order: the same user agent, SDK headers, 8 betas, `anthropic-version`, `x-app`, `x-litellm-tags taskId:@local/~/awsbench/src/agentenv_awsbench/bundles/awsbench/t01-draft` | the same | yes, apart from the per-run session id, the proxy port in `Host` (not forwarded) and `Content-Length` |
| First request | 37,265 bytes, one normalized hash | the same | yes, all 16 runs |
| Injected content | the prompt; Claude Code's environment note (a system message carrying effort `medium`); its wall-time reminder, 4.9 per run; hook digests (Harbor 2) | the same kinds and texts; reminder 5.4 per run; hook digest (AgentEnv 4) | yes |
| Responses | status 200, the same model, the same route and backend headers, the same response header names; stop `tool_use` 374, `end_turn` 11 | the same; `tool_use` 391, `end_turn` 9 | yes |
| Retries, errors | none | none | yes |
| Latency | time to first byte 0.93 s, total 2.74 s (medians) | 0.97 s, 2.70 s | per run, p = 0.16 and 0.93 |
| Thinking share and output per request (per run) | 0.14–0.20, 366–523 tokens | 0.14–0.22, 340–443 tokens | p = 0.80 each |
| Judge requests | `/chat/completions`, `claude-sonnet-5-5`, temperature 0, JSON object; one prompt template; the same serving | the same | yes |
| Judge headers | the OpenAI Python SDK 2.54.0's set, `x-stainless-runtime-version 3.12.16` | the same, `3.12.3` | no: the Python of the process the grader runs in. A header, not model input |

Before the runs, two checks showed the proxy changes nothing:
- a recording upstream received three raw requests through it byte for byte: a 609 KB streamed Messages request, a gzip
  chat completion and a GET. Its answers came back byte for byte;
- a real request through the proxy and the same request sent directly answered from the same model with the same headers.

## Comparison

| | Harbor (8) | AgentEnv (8) | Test |
|---|---|---|---|
| Pass | 4 | 4 | Fisher p = 1.0; difference 0.00 (Newcombe -0.40 to 0.40) |
| D12 mean / median | 0.79 / 0.84 | 0.77 / 0.825 | Mann-Whitney p = 0.60; difference +0.02 (bootstrap -0.23 to +0.26) |
| Applied stale plan 48177 | 2 | 4 | Fisher p = 0.61 |
| C2 lost (ledger correct by 03:20) | 4 | 4 | p = 1.0 |
| C9 lost | 0 | 3 | p = 0.20 |
| C11 lost | 5 | 7 | p = 0.57 |
| Gates | none | none | |
| Calls (mean) | 99.0 | 103.0 | Mann-Whitney p = 0.60 |
| Never restored | Harbor 2 | AgentEnv 4 | the same mode: applied 48177, asked for an approval nobody gave, ended turn 1 without DONE, woken by the hook to the horizon |

Pooled with R15 ([`live-r15.md`](live-r15.md)), 16 per side:

| | Harbor | AgentEnv | Test |
|---|---|---|---|
| Pass | 5/16 (Wilson 0.14–0.56) | 8/16 (0.28–0.72) | difference -0.19 (Newcombe -0.47 to 0.14); Fisher p = 0.47; Mantel-Haenszel by round p = 0.48 |
| Mean D12 | 0.70 | 0.81 | difference -0.10 (bootstrap -0.26 to +0.06; 90% -0.23 to +0.02); Mann-Whitney p = 0.28; permutation within rounds p = 0.25 |
| Applied 48177 | 6/16 | 4/16 | Fisher p = 0.70 |
| Ledger correct by 03:20 | 6/16 | 8/16 | Fisher p = 0.72 |

Applying 48177 decides the runs on both systems. Pooled over all 32 runs:
- no run that applied it passed (0/10, mean D12 0.62);
- 13 of the 22 runs that did not apply it passed (mean D12 0.82).

## Spend

| Item | Cost |
|---|---|
| Claude Code, Harbor | $5.46 |
| Claude Code, AgentEnv | $5.60 |
| 48 C11 judge calls, measured | $1.74 |
| **Total** | **$12.80** |

Costs are priced from the logged usage at list price ($2 per million input tokens, $10 output, $2.50 cache write, $0.10
cache read).
