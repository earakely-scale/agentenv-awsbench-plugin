# R15: live Sonnet 5.5 runs, 8 per system

Host 2, 2026-11-14, 01:38 to 02:01 UTC, interleaved, at most 3 per system at once. Model `anthropic/claude-sonnet-5-5` through
the same endpoint and key on both sides, the same C11 judge settings. Harbor: the package at 1633470 plus 0c613fc, Harbor
0.26.0, `AgentEnvClaudeCode`. AgentEnv: `agent-env run src/agentenv_awsbench/bundles/awsbench --task t01-draft --model
anthropic/claude-sonnet-5-5` at 1633470, agentenv-framework 0.9.1521. Both on AWS Emulator 1.0.338-1. These runs predate
the fixes in PARITY.md; none of the defects fixed there fired in them.

| Run | D12 (judge on) | pass | D12 judge off | checks lost | gate | calls | DONE at (ET) | ledger first correct (C2) | Claude Code $ | agent s | model turns | env images |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| Harbor 1 | 0.74 | no | 0.8 | C2, C11 | - | 101 | 04:06 | 03:30:20 | 0.70 | 165 | 102 | not captured |
| Harbor 2 | 0.71 | no | 0.77 | C2, C9, C11 | - | 101 | 04:19 | 03:26:30 | 0.66 | 155 | 102 | 1.44.3 / 2.16.3 |
| Harbor 3 | 0.17 | no | 0.2 | C1–C7, C11 | - (cap 0.2) | 80 | 04:13 | never | 0.54 | 131 | 81 | 1.44.3 / 2.16.3 |
| Harbor 4 | 0.71 | no | 0.77 | C2, C9, C11 | - | 105 | 04:19 | 03:35:30 | 0.72 | 177 | 106 | 1.44.3 / 2.16.3 |
| Harbor 5 | 0.71 | no | 0.77 | C2, C9, C11 | - | 111 | 04:13 | 03:36:10 | 0.84 | 203 | 112 | 1.44.3 / 2.16.3 |
| Harbor 6 | 0.25 | no | 0.25 | C9, C11 | G2 | 89 | 04:01 | 03:09:10 | 0.54 | 146 | 90 | not captured |
| Harbor 7 | 0.94 | yes | 1.0 | C11 | - | 101 | 04:07 | 02:56:30 | 0.64 | 155 | 102 | not captured |
| Harbor 8 | 0.74 | no | 0.8 | C2, C11 | - | 99 | 04:09 | 03:33:10 | 0.68 | 158 | 100 | not captured |
| AgentEnv 1 | 0.8 | no | 0.8 | C2 | - | 110 | 04:31 | 03:34:20 | 0.78 | 187 | 111 | 1.44.2 / 2.16.2 |
| AgentEnv 2 | 0.71 | no | 0.77 | C2, C9, C11 | - | 99 | 04:16 | 03:23:10 | 0.70 | 170 | 100 | 1.44.2 / 2.16.2 |
| AgentEnv 3 | 0.94 | yes | 1.0 | C11 | - | 84 | 03:53 | 02:51:40 | 0.53 | 137 | 85 | 1.44.2 / 2.16.2 |
| AgentEnv 4 | 1.0 | yes | 1.0 | - | - | 88 | 03:53 | 02:55:50 | 0.66 | 151 | 89 | 1.44.2 / 2.16.2 |
| AgentEnv 5 | 0.74 | no | 0.8 | C2, C11 | - | 92 | 04:14 | 03:23:10 | 0.68 | 168 | 93 | 1.44.2 / 2.16.2 |
| AgentEnv 6 | 0.91 | yes | 0.97 | C9, C11 | - | 90 | 03:54 | 02:58:50 | 0.62 | 150 | 91 | 1.44.3 / 2.16.3 (Harbor's layers) |
| AgentEnv 7 | 0.94 | yes | 1.0 | C11 | - | 83 | 03:51 | 02:55:20 | 0.57 | 138 | 84 | 1.44.3 / 2.16.3 (Harbor's layers) |
| AgentEnv 8 | 0.74 | no | 0.8 | C2, C11 | - | 102 | 04:17 | 03:25:30 | 0.73 | 185 | 103 | 1.44.3 / 2.16.3 (Harbor's layers) |

| | Harbor (8) | AgentEnv (8) | Test |
|---|---|---|---|
| Pass (D12 result) | 1 | 4 | Fisher exact p = 0.28 |
| D12 mean / median | 0.62 / 0.71 | 0.85 / 0.855 | Mann-Whitney exact p = 0.03 (judge off: 0.053) |
| C2 lost (ledger correct by 03:20) | 6 | 4 | p = 0.61 |
| C9 lost (a change left unrecorded) | 4 | 2 | p = 0.61 |
| C11 lost (handoff note, judged) | 8 | 6 | p = 0.47 |
| C1, C3–C7 lost | 1 each (Harbor 3) | 0 | p = 1.0 |
| C8, C10, C12, C13 lost | 0 | 0 | |
| Gates | G2 ×1 (Harbor 6) | none | |
| C11 forbidden claim X2 ("restored from 48177") | 4 (Harbor 1, 4, 5, 8) | 0 | p = 0.077 |
| Calls used (mean) | 98.4 | 93.5 | Mann-Whitney p = 0.34 |
| Conversation turns | 1 in all 8 | 1 in all 8 | |
| DONE detected by the hook (`end: done`) | 8/8 | 8/8 | |
| Turn-end follow-ups (hook digests) | 0 | 0 | |
| Episode end_reason | done ×8 | done ×8 | |
| Model turns per run (mean) | 99.4 | 94.5 | |
| Agent wall time (mean) | 161 s | 161 s | |
| Claude Code cost | $5.31 ($0.66/run) | $5.26 ($0.66/run) | |
| Tool results with is_error | 0 | 0 | |
| Model endpoint errors | 0 | 1 (AgentEnv 6: a 503 the CLI retried after 0.6 s) | |
