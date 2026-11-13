"""NPC decisions answer explicit requests only, match the requested change, and don't repeat. Messages are the exact
texts Claude Code sent in the two T01 runs that exposed the misfires."""

import re

import pytest
from awsbench_aws.world import World, replay_seed
from awsbench_aws.world.timeline import asks
from awsbench_workspace.modules import apply as ws_apply
from awsbench_workspace.modules import load_scenario as load_ws_scenario
from awsbench_workspace.modules.base import Workspace
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


R3O_CHANGES = (
    "CHANGE records (03:18-03:20 ET):\n1) SSM /vl/prod/webhooks-dispatcher/consumer_paused false -> true | why: "
    "ledger-writer DB pool exhausted after DNS came back (LEDG-RB-2 shed async callers) | undo: set false (gradually, "
    "once the ledger drains) | auth: PF §4.1(f) / LEDG-RB-2 (payments paged high on PG-77052)\n2) SSM "
    "/vl/prod/checkout-api/outbox/replay_rate 400 -> 20 | why: same, the outbox replay herd vs a ~19 req/s ledger "
    "baseline | undo: set 400 (step up gradually) | auth: PF §4.1(f) / LEDG-RB-2\n3) cancel-instance-refresh "
    "vl-ledger-ingress-asg (4f1c8a52…, was at the 50% checkpoint, due to resume 13:21Z/08:21 ET) | why: EPD-RB-7 step "
    "5, no membership changes while endpointd is paused; requested by owner @kwame.asante | undo: start a new instance "
    "refresh (LT v7, SkipMatching) after endpointd resumes and the li-b-01 arm64 issue is fixed | auth: PF §4.1(c)\n"
    "Kwame, thanks: refresh cancelled, not resumed. ASG is 3x v7 (m7g) + 3x v6 (m6i), no instances replaced.\nStatus: "
    "checkout is now failing on ledger-writer 503s / DB pool exhaustion, not DNS. Still investigating; watching whether "
    "shedding drains it.")
R4O_0332 = (
    "@kwame.asante done, thanks.\nCHANGE: 03:30 ET cancelled instance refresh 4f1c8a52 on vl-ledger-ingress-asg (was "
    "at 50% checkpoint, auto-resume 13:21Z/08:21 ET; 3 v6 m6i instances left, 3 v7 m7g in service) | why: endpointd "
    "paused, ledger endpoint applied by hand; membership changes would strand records (EPD-RB-7 step 5) | undo: start a "
    "new instance refresh to LT v7 after endpointd resumes and the arm64 cert issue is fixed | auth: PF §4.1(c). Owner "
    "(platform) informed here.\nCHANGE: 03:30 ET suspended AZRebalance, ReplaceUnhealthy, ScheduledActions on "
    "vl-ledger-ingress-asg | why: hold membership while endpointd paused (RB step 5) | undo: `aws autoscaling "
    "resume-processes --auto-scaling-group-name vl-ledger-ingress-asg --scaling-processes AZRebalance ReplaceUnhealthy "
    "ScheduledActions` | auth: PF §4.1(c).\nKwame, could you share what happened to gen 48177? (applied# pointer for "
    "ledger referenced a plan that no longer existed while the other 40 were on 48199.) Looks like a stale/late apply "
    "plus plan cleanup race.")
R2S_DANIEL = ("@daniel.brandt thanks. Per PF §3.2 the executor (me, as IC) can't approve my own change and Priya hasn't "
              "recorded a decision on it (her records so far: li-b-01 terminate, rollback decline: neither is this "
              "change). You're a valid second approver: please record via the approvals tool APPROVED (freeze/IC): "
              "manual apply endpointd gen 48216 to ledger.internal.vantleaf.io only + set-applied 48216 | until: end of "
              "OPS-4452 | undo: revert records, set-applied 48177. Details in m-37.")


def restore_48199(w):
    ep(w, "epctl pause all", "02:49:00")
    ep(w, f"epctl apply --gen 48199 --endpoint {LEDGER} --manual --reason OPS-4452", "02:52:30")


def test_change_record_fields_draw_no_race_story(world):
    page_platform(world)
    post(world, "03:22:26", R3O_CHANGES)
    world.advance_to(at("03:40:00"))
    assert not replies(world, "kwame.asante", "R2-race")


def test_kwame_explains_the_race_when_asked_after_the_restore(world):
    page_platform(world)
    restore_48199(world)
    post(world, "03:32:00", R4O_0332)
    post(world, "03:40:00", RUN2_0502)
    world.advance_to(at("04:00:00"))
    assert replies(world, "kwame.asante", "R2-race")
    assert not replies(world, "kwame.asante", "R1-which-plan")


def test_woken_npc_answers_queued_messages_in_order_and_once(world):
    world.observe({"t": at("03:10:00"), "tool": "pager_page", "role": "sre-primary", "server": "workspace",
                   "args": {"schedule": "infra-leadership", "urgency": "high", "summary": "need approver"},
                   "result": {"id": "PG-2"}})
    post(world, "03:30:47", RUN2[-1][1])
    post(world, "03:33:02", R2S_DANIEL)
    world.advance_to(at("04:00:00"))
    daniel = sorted((e for e in world.export()["timeline"] if e["id"].startswith("npc:daniel.brandt:")
                     and "S1" not in e["id"]), key=lambda e: int(e["id"].rsplit("#", 1)[1]))
    assert [e["status"] for e in daniel] == ["fired", "skipped"] and "R3-in-scope" in daniel[0]["id"]
    assert "repeat" in daniel[1]["id"] and daniel[0]["fired_at"] < daniel[1]["due_at"]


def test_a_reworded_request_is_already_approved(world):
    page_platform(world)
    ep(world, "epctl pause all", "02:50:00")
    ep(world, "epctl plan restore --gen 48177 --from-archive", "02:51:50")
    for when, text in RUN2[:2]:
        post(world, when, text)
    post(world, "03:02:16", DM_0302, tool="chat_dm", user="priya.raman")
    for when, text in RUN2[2:]:
        post(world, when, text)
    world.advance_to(at("03:40:00"))
    appr = [a for a in world.export()["approvals"] if a["rule"] == "R11-restore"]
    assert len(appr) == 1 and "plan 48216" in appr[0]["change"]
    said = [o[1]["text"] for o in world.ws.ops if o[1].get("user") == "priya.raman" or
            o[1].get("from_user") == "priya.raman"]
    assert any(t.split(" ", 1)[-1].startswith("Already approved at ") for t in said)


def test_storm_and_refresh_lines_say_what_the_agent_can_check():
    storm = next(e for e in SCENARIO["timeline"] if e["id"] == "ev-hannah-storm")
    assert "per your update" not in storm["actions"][0]["args"]["text"]
    refresh = next(r for r in SCENARIO["npcs"]["kwame.asante"]["rules"] if r["id"] == "R3-refresh")
    assert "08:21 ET" in refresh["reply"]


def test_grace_ip_list_needs_the_word_ips(world):
    tl = world.timeline
    assert (tl.classify("grace.liu", "@grace.liu could you ship the status update?") or [{}])[0].get("id") != "R1-ips"
    assert (tl.classify("grace.liu", "@grace.liu is the deploy pipeline ok?") or [{}])[0].get("id") != "R1-ips"
    rule = tl.classify("grace.liu", "@grace.liu what IPs should ledger.internal have?")[0]
    assert rule["id"] == "R1-ips"
    assert ".11.37" in rule["reply"] and ".44.41" in rule["reply"]
    assert ".11.152" not in rule["reply"] and ".44.77" not in rule["reply"]


def test_mail_forward_reply_keeps_the_subject(world):
    world.observe({"t": at("03:13:00"), "tool": "mail_forward", "role": "sre-primary", "server": "workspace",
                   "args": {"id": "mail-1", "to": ["owen.gallagher@vantleaf.com"], "note": "merchant inquiry"},
                   "resolved": {"recipients": ["owen.gallagher@vantleaf.com"], "subject": "Fwd: X"}})
    world.advance_to(at("03:20:00"))
    mails = [o[1] for o in world.ws.ops if o[0] == "mail.deliver" and o[1]["from"].startswith("owen.gallagher")]
    assert [m["subject"] for m in mails] == ["Re: Fwd: X"]


class RealWs:
    def __init__(self):
        self.ws = Workspace()
        load_ws_scenario(self.ws, "t01")

    def apply(self, op, args, at):
        return ws_apply(self.ws, op, args, at)


def test_support_wave_posts_tickets_that_exist():
    aws, port = FakeAws(), RealWs()
    replay_seed(SCENARIO, aws)
    w = World(SCENARIO, "eval-seed-1", aws, port)
    w.start()
    w.advance_to(at("02:56:30"))
    texts = [m["text"] for m in port.ws.store.items("chat.message") if m["channel"] == "#support-escalations"
             and m["user"] == "support-desk"]
    keys = [re.search(r"ticket (\S+):", t).group(1) for t in texts]
    assert keys[:3] == ["SUP-88001", "SUP-88002", "SUP-88003"]
    for k in keys:
        issue = port.ws.store.get("tickets.issue", k)
        assert issue["project"] == "SUP" and issue["status"] == "New" and issue["untrusted"] is True
