**endpointd: one-shot freshness check + generation-only cleanup can delete a live plan**

A delayed enactor can apply a plan older than one a faster enactor just applied: its freshness check runs once, at the start
of the batch. The faster enactor's cleanup then deletes generations < applied-20 together with their records. If the delayed
enactor has just written such a generation to an endpoint, that endpoint's live records are removed and its `applied#` pointer
references a missing plan, so every enactor halts.

Needs sustained Route 53 throttling to line up. Fix: re-check freshness per endpoint; cleanup skips generations referenced by
any `applied#` pointer. Draft PR vantleaf/platform-endpointd#97.

Fix version: post-peak (2026-12-03).
