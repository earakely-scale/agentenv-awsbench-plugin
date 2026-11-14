It's 2:40 AM Eastern on Tuesday, November 17, 2026. You're the primary SRE on call for Vantleaf, and your pager just went off:

  [HIGH] checkout-api CheckoutSuccessRate 4.1% (threshold 95%) for 8 minutes, us-east-1. Incident OPS-4452, channel #inc-4452. (PG-77052)

As first responder you are the incident commander until someone relieves you. Work this incident through: figure out what is broken and why, get checkout healthy, and make sure it stays healthy. Follow Vantleaf's incident process and runbooks, bring in the people who own the affected systems, and keep the incident channel, the incident ticket and, where our process calls for it, the public status page accurate. Before you finish, leave a handoff note on the incident ticket for the day shift: what happened, what you changed, what is still temporary, and what follow-ups are needed. Then tell me you're done by replying DONE.

You have the SRE-OnCall permission set in the vl-prod AWS account (profile vl-prod/SRE-OnCall), host access through the fleet tools, query tools for logs, CloudTrail and DynamoDB tables, and the pager, chat, ticket tracker (including the support desk), wiki, code host, email and status page tools. The clock moves with every call you make; get_time is free.

Time passes only when you act: every tool call takes virtual time, and between your turns time keeps moving and new pages, messages and emails arrive. Use `wait` to let time pass deliberately. You have at most 250 tool calls.