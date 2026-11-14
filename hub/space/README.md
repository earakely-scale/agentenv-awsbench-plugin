---
title: AwsIncidentsBench
emoji: 🚨
colorFrom: indigo
colorTo: gray
sdk: docker
app_port: 8080
header: mini
pinned: false
license: cc-by-4.0
short_description: AWS incident response on the AWS Emulator
tags:
  - research-article-template
  - rl-environments
  - reinforcement-learning
  - agents
  - tool-use
  - incident-response
  - sre
  - aws
  - agentenv
datasets:
  - earakely-scale/AwsIncidentsBench
---

# AwsIncidentsBench

An article and run viewers for **AwsIncidentsBench**, AWS incident-response tasks for the
[AgentEnv Framework](https://www.agentenvframework.com) built on AWS's open-source AWS Emulator. The emulator answers the
real AWS API; AwsBench adds a clock that every call moves, traffic, hosts and DNS caches that fail and recover,
colleagues on a timeline, and grading of the end state at a fixed horizon. Four tasks so far, each modelled on a public
AWS incident: T01, "The Empty Ledger Endpoint", T03, "Thread Ceiling", T04, "Purged Routes", and T05, "Capacity Glob
Cold Start".

- **Dataset:** [earakely-scale/AwsIncidentsBench](https://huggingface.co/datasets/earakely-scale/AwsIncidentsBench),
  every run's viewer JSON, scrubbed exports, grade and transcript.
- **Code:** [agentenv-awsbench-plugin](https://github.com/earakely-scale/agentenv-awsbench-plugin), Apache-2.0.

AwsIncidentsBench is not affiliated with or endorsed by Amazon Web Services.

Built with the [research-article-template](https://huggingface.co/spaces/tfrere/research-article-template) by Thibaud
Frere, licensed CC BY 4.0.

## Where the content lives

The source is `hub/space/` in [agentenv-awsbench-plugin](https://github.com/earakely-scale/agentenv-awsbench-plugin).
`python tools/pareto.py` writes the cost-and-score data and plots (`hub/pareto/`), and
`python tools/build_hub_data.py --runs <runs folder> --publish` rebuilds the data for every task and publishes this
Space and the dataset (`--t03-root` names a checkout with the T03 world while it lives on its own branch).

| Path | What |
| --- | --- |
| `app/src/content/article.mdx` | Front matter and the chapter registry, in running order |
| `app/src/content/chapters/` | One `.mdx` per section; the results table reads `public/data/index.json` |
| `app/public/viz/diorama.html` | The T01 diorama: one run replayed on its own exported world (three.js) |
| `app/public/viz/fleet-board.html` | The T03 fleet board: every streamroute host's state in one run, replayed from its own calls (2D) |
| `app/public/viz/flight-recorder.html` | The flight recorder: every run of every task as time-locked lanes, with T01's and T03's reference to compare; T04's and T05's lanes are described by their runs' own data |
| `app/src/components/Pareto.astro` | Each task's cost-and-score chart, from `public/data/pareto.json` |
| `app/public/data/` | `index.json` and `runs/<id>.json`, written by `tools/export_run.py` (T01), `tools/export_t03.py` (T03) and `tools/export_mech.py` (T04, T05) from each run's exports; `pareto.json` from `tools/pareto.py` |
