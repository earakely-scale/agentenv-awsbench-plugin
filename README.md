# AwsBench for AgentEnv

AWS incident-response environments for [AgentEnv](https://github.com/scaleapi/agentenv-framework), built on AWS's
open-source [AWS Emulator](https://github.com/aws/aws-emulator). The emulator answers the AWS API; AwsBench adds what an
incident needs around it: virtual time charged per call, traffic and failing hosts, colleagues and a timeline of world
events, and grading at a fixed horizon. It needs no change to agentenv-framework.

Work in progress (tracked in FD-5735). The first task is T01, "The Empty Ledger Endpoint". See `CONTRACTS.md` for how
the parts fit together.

AwsBench is not affiliated with or endorsed by Amazon Web Services.
