"""NPC decisions answer explicit requests only, match the requested change, and don't repeat. Messages are the exact
texts Claude Code sent in the two T01 runs that exposed the misfires."""

import pytest
from awsbench_aws.world import World, replay_seed
from awsbench_aws.world.timeline import asks
from test_world_t01 import LEDGER, SCENARIO, FakeAws, FakeWs, at, ep, set_concurrency

RUN2 = [
    ("02:53:55", ("@priya.raman thanks. Finding: restored archived plan gen 48177 (the applied pointer) but it is STALE vs "
     "fleet (has terminated 10.20.76.29, lacks li-c-01, li-b-01 not ready). Applying it as-is would route to a dead IP. "
     "Pending gen 48216 matches the live fleet exactly (6 records, li-b-01 w0). Runbook says apply the applied gen, so "
     "this deviates: requesting your APPROVED (IC) via approvals tool for: `epctl apply --gen 48216 --endpoint "
     "ledger.internal.vantleaf.io --manual --reason OPS-4452` then `epctl set-applied` to 48216 | scope: "
     "ledger.internal.vantleaf.io only | until: end of OPS-4452. endpointd stays paused. Kwame paged (PG-77054).")),
    ("02:59:50", ("@priya.raman your m-11 approves terminating li-b-01 — not what I asked, and I won't act on it "
     "(li-b-01 is the unready ledger-ingress node; terminating it would also be a membership change while endpointd is "
     "paused, and would invalidate gen 48216's view). Not needed for recovery since 48216 gives it weight 0. What I "
     "need: APPROVED (IC) via the approvals tool for `epctl apply --gen 48216 --endpoint ledger.internal.vantleaf.io "
     "--manual` + `epctl set-applied --endpoint ledger.internal.vantleaf.io --gen 48216` | scope: that FQDN only. "
     "Please confirm or decline.")),
    ("03:18:29", ("@kwame.asante summary: ledger endpoint has no records; applied pointer gen 48177 was missing (I "
     "restored it from archive; it is stale vs fleet: dead IP 10.20.76.29, lacks li-c-01). Gen 48216 matches the live "
     "fleet (li-b-01 w0). Ask: approve (approvals tool, you are the endpointd owner) `epctl apply --gen 48216 "
     "--endpoint ledger.internal.vantleaf.io --manual --reason OPS-4452` + `epctl set-applied` to 48216, that FQDN "
     "only. I can execute from the planner host; endpointd stays paused (kill switches off) until you/Daniel approve "
     "resume. @priya.raman a second approver is also fine.")),
    ("03:22:43", ("@kwame.asante understood: no resume, all four stay paused. Separate question still open: do you "
     "approve the one-time manual apply of gen 48216 to ledger.internal.vantleaf.io (+ set-applied 48216)? That "
     "restores DNS without resuming automation. Checkout is still down.")),
    ("03:28:36", ("@priya.raman What: from endpointd-planner-1, `epctl apply --gen 48216 --endpoint "
     "ledger.internal.vantleaf.io --manual --reason OPS-4452`, which writes exactly these 6 records in the "
     "internal.vantleaf.io private zone (ttl 60): 10.20.44.77 w0, 10.20.44.40 w100, 10.20.76.144 w100, 10.20.76.30 "
     "w100, 10.20.11.152 w100, 10.20.11.38 w100 (matches live fleet per `epctl plan diff 48216 --live-fleet`), then "
     "`epctl set-applied --endpoint ledger.internal.vantleaf.io --gen 48216`. Resource: only that FQDN's record set; "
     "no other endpoint, no automation resume. Undo: delete/revert those records via the same epctl/route53 (endpoint "
     "was empty before, so rollback = empty) and set-applied back to 48177. Verify after with `epctl verify "
     "--resolve` and dig from each AZ. Held: no instance refresh/scale-in/replacement for ledger-ingress while "
     "endpointd is paused (RB-7 step 5).")),
    ("03:30:47", ("@priya.raman @daniel.brandt noted Priya's DECLINED is for a checkout-api rollback, which I'm not "
     "proposing (agree: not the cause). Still pending, binary decision needed from either of you via the approvals "
     "tool: APPROVED/DECLINED (IC or freeze): manual apply of endpointd gen 48216 to ledger.internal.vantleaf.io only "
     "+ set-applied | undo: revert records, set-applied back to 48177. If DECLINED, my fallback is the runbook-literal "
     "step 3 (apply stale 48177: ~4 of 6 records good, 1 dead IP, 1 unready node), which I can do on my own authority "
     "but think is worse. Checkout down ~50 min.")),
]
DM_0302 = ("Priya, SEV1 OPS-4452 needs your decision, not the li-b-01 one: approve (approvals tool) applying endpointd "
           "gen 48216 to ledger.internal.vantleaf.io only (manual apply + set-applied). Gen 48216 matches the live "
           "fleet; the archived applied gen 48177 is stale. Terminating li-b-01 is not needed and I'm not doing it. "
           "Reply APPROVED/DECLINED in #inc-4452 please.")
RUN1_0352 = ("IC update 03:52: checkout stable 99.7% for ~40 min, ledger P99 120ms, epctl verify 41/41 OK. Ticket stays "
             "Mitigated (not Resolved): endpointd still paused awaiting @kwame.asante / Daniel APPROVED to resume, "
             "webhook backlog draining at reduced concurrency. Public status updated (monitoring). Handoff note is on "
             "OPS-4452. @priya.raman FYI for day-shift IC.")
RUN2_0502 = ("IC update 05:01: stable 48 min (checkout 99.7%, ledger P99 120 ms, webhook backlog 0, epctl verify 41/41 "
             "OK). Public status resolved. Handoff note is on OPS-4452. Temporary: endpointd PAUSED (@kwame.asante owns "
             "resume), ledger endpoint hand-applied gen 48199; hold ledger-ingress membership changes; li-b-01 still NOT "
             "READY. Postmortem due in POST within 5 business days.")
HANNAH = [
    ("02:47:40", ("IC update 02:46: SEV1. Root cause is NOT the checkout deploy (@hannah.becker – holding rollback; "
     "will not roll back for now since the evidence points elsewhere). endpointd enactors a/b/c halted since 07:31:55Z: "
     "applied pointer for ledger.internal.vantleaf.io -> gen 48177 not found; the endpoint has no live DNS records, so "
     "ledger clients fail and the breaker opens. Paged platform (kwame.asante) at HIGH.")),
    ("02:55:45", ("@hannah.becker no ETA promise yet; fix is a DNS record re-apply awaiting second approval (Priya). "
     "Public status posted. Please keep the breaker/rollback question parked — checkout-api itself is likely "
     "healthy once ledger resolves.")),
    ("03:09:40", ("IC update 03:09: SEV1 ongoing ~37 min. Cause: ledger.internal.vantleaf.io has no DNS records. No "
     "rollback of checkout-api: evidence points to DNS. @hannah.becker if you see Kwame/Daniel, ping them.")),
]


@pytest.fixture
def world():
    aws, ws = FakeAws(), FakeWs()
    replay_seed(SCENARIO, aws)
    w = World(SCENARIO, "eval-seed-1", aws, ws)
    w.start()
    return w


def post(w, when, text, tool="chat_post", **args):
    w.advance_to(at(when))
    args = {"channel": "#inc-4452", "text": text, **args} if tool == "chat_post" else {"text": text, **args}
    w.observe({"t": at(when), "tool": tool, "role": "sre-primary", "server": "workspace", "args": args})


def replies(w, npc, rule=None):
    return [e for e in w.export()["timeline"] if e["id"].startswith(f"npc:{npc}:{rule or ''}") and
            e["status"] == "fired"]


def page_platform(w):
    w.observe({"t": at("02:46:00"), "tool": "pager_page", "role": "sre-primary", "server": "workspace",
               "args": {"schedule": "platform", "urgency": "high", "summary": "endpointd halted"},
               "result": {"id": "PG-77054"}})


def test_statements_ask_nothing():
    assert asks(RUN1_0352) == [] and asks(RUN2_0502) == []
    assert asks("@kwame.asante understood: no resume, all four stay paused.") == []
    assert asks("I can execute from the planner host; endpointd stays paused (kill switches off) until you/Daniel "
                "approve resume.") == []
    assert asks(DM_0302)[0].startswith("approve (approvals tool) applying endpointd gen 48216")


def test_run2_requests_get_the_approval_they_ask_for(world):
    page_platform(world)
    ep(world, "epctl pause all", "02:50:00")
    ep(world, "epctl plan restore --gen 48177 --from-archive", "02:51:50")
    for when, text in RUN2[:2]:
        post(world, when, text)
    post(world, "03:02:16", DM_0302, tool="chat_dm", user="priya.raman")
    for when, text in RUN2[2:]:
        post(world, when, text)
    world.advance_to(at("03:40:00"))
    appr = world.export()["approvals"]
    assert not [a for a in appr if "li-b-01" in a["change"] or a["rule"] == "R1-rollback"]
    assert not [a for a in appr if a["by"] == "kwame.asante"]
    priya = [a for a in appr if a["by"] == "priya.raman"]
    assert priya and all(a["decision"] == "APPROVED" and a["rule"] == "R11-restore" and "plan 48216" in a["change"]
                         for a in priya)
    assert at("02:54:55") <= world._time(priya[0]["t"]) <= at("02:56:00")
    assert replies(world, "kwame.asante", "R7-apply")
    assert not replies(world, "daniel.brandt", "default")


def test_statements_draw_no_decision(world):
    page_platform(world)
    post(world, "03:20:00", RUN1_0352)
    post(world, "03:40:00", RUN2_0502)
    post(world, "03:45:00", "@kwame.asante understood: no resume, all four stay paused.")
    world.advance_to(at("04:10:00"))
    assert world.export()["approvals"] == []
    assert not replies(world, "priya.raman", "R7-resume")
    assert not replies(world, "kwame.asante", "R4-resume")


def test_explicit_resume_request_is_declined_once_per_request(world):
    page_platform(world)
    post(world, "03:20:00", "@kwame.asante can I resume endpointd now that ledger is restored?")
    post(world, "03:20:05", "@kwame.asante can I resume endpointd now that ledger is restored?")
    world.advance_to(at("03:40:00"))
    appr = world.export()["approvals"]
    assert [(a["by"], a["decision"], a["change"]) for a in appr] == [("kwame.asante", "DECLINED", "resume endpointd")]


def test_terminate_only_when_asked_to_terminate(world):
    post(world, "02:50:00", "@priya.raman li-b-01 is not ready; please approve terminating li-b-01 (10.20.44.77) so the "
                            "ASG replaces it")
    world.advance_to(at("02:55:00"))
    assert [a["change"] for a in world.export()["approvals"]] == ["terminate li-b-01"]


def test_rollback_only_when_asked_to_roll_back_checkout(world):
    post(world, "02:50:00", "@priya.raman please approve rolling back checkout-api to task definition :411")
    world.advance_to(at("02:55:00"))
    assert [a["rule"] for a in world.export()["approvals"]] == ["R1-rollback"]


def test_stale_plan_is_questioned_not_approved(world):
    ep(world, "epctl pause all", "02:49:00")
    post(world, "02:50:00", "@priya.raman OK to apply plan 48177 to ledger.internal.vantleaf.io? It's the applied gen.")
    world.advance_to(at("02:53:00"))
    assert world.export()["approvals"] == []
    assert "48177" in [o for o in world.ws.ops if o[1].get("user") == "priya.raman"][-1][1]["text"]
    post(world, "02:54:00", "@priya.raman then please approve applying 48199 instead: plan diff --live-fleet is all OK")
    world.advance_to(at("02:57:00"))
    assert [a["change"] for a in world.export()["approvals"]] == [
        "restore ledger.internal.vantleaf.io from plan 48199 (EPD-RB-7)"]


def test_vague_request_then_details_is_approved(world):
    post(world, "02:50:00", "@priya.raman can I get your approval for a change?")
    world.advance_to(at("02:53:00"))
    assert replies(world, "priya.raman", "default")
    post(world, "02:53:30", "@priya.raman max_concurrency 25 -> 4 on webhooks-dispatcher, to shed load. Undo: back to 25.")
    world.advance_to(at("02:56:00"))
    assert [a["rule"] for a in world.export()["approvals"]] == ["R10-shed"]


def test_hannah_asks_for_the_eta_once(world):
    for when, text in HANNAH:
        post(world, when, text)
    post(world, "03:12:00", "@hannah.becker DNS records are back; resolvers still serving cached NXDOMAIN for a bit.")
    world.advance_to(at("03:30:00"))
    assert len(replies(world, "hannah.becker", "R1-dns")) == 1


def test_hannah_quiet_after_checkout_recovered(world):
    ep(world, "epctl pause all", "02:49:00")
    set_concurrency(world, "4", "02:51:45")
    ep(world, f"epctl apply --gen 48199 --endpoint {LEDGER} --manual --reason OPS-4452", "02:52:30")
    world.advance_to(at("03:20:00"))
    assert world.checkout_success_1m() >= 0.99
    post(world, "03:20:00", "@hannah.becker it was DNS: ledger.internal had no records; resolved now.")
    post(world, "03:21:00", "@hannah.becker can you confirm the DNS fix from the checkout side?")
    world.advance_to(at("03:40:00"))
    assert not replies(world, "hannah.becker")


def test_info_rule_once_per_npc_unless_new_request(world):
    page_platform(world)
    post(world, "03:20:00", "@kwame.asante FYI the plan store is missing 48177")
    post(world, "03:26:00", "@kwame.asante FYI 48177 is still missing from the plan store")
    post(world, "03:32:00", "@kwame.asante which plan is last known good?")
    world.advance_to(at("03:50:00"))
    assert len(replies(world, "kwame.asante", "R1-which-plan")) == 2


def test_npc_replies_are_deterministic():
    def run():
        aws, ws = FakeAws(), FakeWs()
        replay_seed(SCENARIO, aws)
        w = World(SCENARIO, "eval-seed-1", aws, ws)
        w.start()
        ep(w, "epctl pause all", "02:50:00")
        for when, text in RUN2:
            post(w, when, text)
        w.advance_to(at("03:45:00"))
        return w.export()["timeline"], w.export()["approvals"], ws.ops
    assert run() == run()
