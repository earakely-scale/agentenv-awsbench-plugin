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
colleagues on a timeline, and grading of the end state at a fixed horizon. The first task is T01, "The Empty Ledger
Endpoint".

- **Dataset:** [earakely-scale/AwsIncidentsBench](https://huggingface.co/datasets/earakely-scale/AwsIncidentsBench),
  every run's viewer JSON, scrubbed exports, grade and transcript.
- **Code:** [agentenv-awsbench-plugin](https://github.com/earakely-scale/agentenv-awsbench-plugin), Apache-2.0.

AwsIncidentsBench is not affiliated with or endorsed by Amazon Web Services.

Built with the [research-article-template](https://huggingface.co/spaces/tfrere/research-article-template) by Thibaud
Frere, licensed CC BY 4.0.

## Where the content lives

The source is `hub/space/` in [agentenv-awsbench-plugin](https://github.com/earakely-scale/agentenv-awsbench-plugin).
`python tools/build_hub_data.py --runs <runs folder> --publish` rebuilds the data and publishes this Space and the
dataset.

| Path | What |
| --- | --- |
| `app/src/content/article.mdx` | Front matter and the chapter registry, in running order |
| `app/src/content/chapters/` | One `.mdx` per section; the results table reads `public/data/index.json` |
| `app/public/viz/diorama.html` | The T01 diorama: one run replayed on its own exported world (three.js) |
| `app/public/viz/flight-recorder.html` | The flight recorder: every run as time-locked lanes, with the reference to compare |
| `app/public/data/` | `index.json` and `runs/<id>.json`, written by `tools/export_run.py` from each run's exports |
