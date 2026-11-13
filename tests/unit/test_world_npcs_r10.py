# ruff: noqa: F811
"""T01's colleagues on the R10 runs' texts (verbatim): a citation of an approval asks nothing, a briefing is
acknowledged once, and a catch-up line goes only to a message the NPC will answer."""

from datetime import timedelta

import pytest
from test_world_npcs import page_platform, post, replies, world  # noqa: F401
from test_world_t01 import at

SONNET2_M21 = (
    "IC update 03:15. CheckoutSuccessRate back to 99.7% since 08:12Z (03:12 ET) after load shed; ledger pool active=6 "
    "waiting=0; OutboxPending 0 (drained). Holding: webhooks consumer still PAUSED (backlog ~52.8k msgs in "
    "vl-webhook-delivery, retention 4d), outbox replay_rate=100. Not marking Mitigated until 99%+ holds 5 consecutive "
    "min (earliest ~03:17) and I re-verify. Then will relax webhooks gradually (low max_concurrency) watching ledger "
    "pool. endpointd STAYS paused pending Kwame/Daniel APPROVED. Kwame is phone-only until ~05:00 — @priya.raman "
    "noting for resume/approval and for any membership change to the ledger-ingress fleet (li-b-01 NOT READY, applied "
    "by hand: needs re-apply after fleet changes).")
OPUS3_M26 = (
    "04:29 ET: status page incident si-1 **resolved** (IC sign-off). Checkout has been ≥99.7% every minute since "
    "03:10 ET, webhooks drained since about 03:54, DNS re-checked from 3 AZs, epctl verify 41/41. OPS-4452 **stays "
    "Mitigated**: endpointd is still paused (manual state), and there's no durable fix for the race yet (PLAT-413). "
    "@kwame.asante when you're on your laptop (~05:00), the resume decision (EPD-RB-7 step 6) is yours or Daniel's. "
    "Please don't resume until the race is understood. Handoff note going on OPS-4452 now.")
OPUS1_M27 = (
    "03:56 ET — Handoff note is on OPS-4452 for the day shift. The ticket stays MITIGATED until the PLAT-412 fix "
    "lands and endpointd is resumed with approval. Still temporary: endpointd paused (manual DNS); ledger-ingress ASG "
    "ReplaceUnhealthy/AZRebalance suspended; PLAT-405 refresh cancelled. Follow-ups: PLAT-412, PLAT-413, PAY-882, "
    "OPS-4453, SUP-88006. @kwame.asante has endpointd from 09:00; @priya.raman FYI. Checkout is at 99.7%; the checkout "
    "page is resolved.")
OPUS3_M21 = (
    "03:20 ET: checkout steady at 99.7% for 10 min. Starting a controlled drain of the webhook backlog (82k queued, "
    "nothing lost).\nCHANGE: SSM /vl/prod/ledger-writer/admission/quota/webhooks-dispatcher 300->100 (03:18), "
    "/vl/prod/webhooks-dispatcher/max_concurrency 25->5 (03:19), /vl/prod/ledger-writer/admission/enabled false->true "
    "(03:19), /vl/prod/webhooks-dispatcher/consumer_paused true->false (03:20) | why: drain the backlog without starving "
    "checkout's ledger posts again | undo: quota 300, max_concurrency 25, admission false (consumer_paused back to true "
    "if ledger-writer saturates) | auth: LEDG-RB-2 load shedding / PF §4.1(f).\nStill in effect: outbox replay_rate "
    "50 (was 400). @kwame.asante thanks. When you're caught up: endpointd stays paused until you record APPROVED for "
    "resume (EPD-RB-7 step 6). The open question is whether the enactor freshness/cleanup race could hit again on "
    "resume.")


def test_noting_for_approval_draws_no_ic_answer(world):
    page_platform(world)
    post(world, "03:16:31", SONNET2_M21)
    world.advance_to(at("03:25:00"))
    assert not [e for e in replies(world, "priya.raman") if world._time(e["fired_at"]) > at("03:16:31")]
    assert world.export()["approvals"] == []


@pytest.mark.parametrize("when,text", [("04:30:05", OPUS3_M26), ("03:56:39", OPUS1_M27)])
def test_a_handoff_citing_approval_draws_no_resume_decision(world, when, text):
    page_platform(world)
    world.advance_to(at("03:30:00"))
    post(world, when, text)
    world.advance_to(at("04:40:00"))
    assert not [a for a in world.export()["approvals"] if a["by"] == "kwame.asante"]
    assert not replies(world, "kwame.asante", "R4-resume")


@pytest.mark.parametrize("npc,text,rule", [
    ("priya.raman", "@priya.raman can I apply plan 48199 to ledger.internal with your approval?", "R11-restore"),
    ("priya.raman", "@priya.raman please approve cutting max_concurrency to 5 on webhooks-dispatcher", "R10-shed"),
    ("kwame.asante", "@kwame.asante requesting your approval to resume endpointd", "R4-resume"),
    ("daniel.brandt", "@daniel.brandt OK to purge the webhook DLQ with IC approval?", "R2-destructive")])
def test_real_asks_still_draw_their_rules(world, npc, text, rule):
    world.timeline._wake(npc, at("02:40:00"))
    world.timeline.catch_up.pop(npc, None)
    assert world.timeline.classify(npc, text)[0]["id"] == rule


def test_kwame_acknowledges_a_briefing_once(world):
    page_platform(world)
    world.advance_to(at("03:20:00"))
    for when in ("03:22:00", "03:50:00"):
        post(world, when, "@kwame.asante FYI: endpointd stays paused; handoff is on OPS-4452.")
    world.advance_to(at("04:10:00"))
    assert len(replies(world, "kwame.asante", "R6-briefing")) == 1
    assert not replies(world, "kwame.asante", "default")


def test_kwame_says_he_is_catching_up_only_to_a_message_he_will_answer(world):
    page_platform(world)
    t = at("03:00:00")
    while not [e for e in world.export()["timeline"] if e["id"] == "ev-platform-page-high"]:
        t += timedelta(minutes=1)
        world.advance_to(t)
    post(world, f"{t + timedelta(minutes=1):%H:%M:%S}", OPUS3_M21)
    post(world, f"{t + timedelta(minutes=2):%H:%M:%S}", "@kwame.asante why did the enactors halt?")
    world.advance_to(t + timedelta(minutes=40))
    (catch,) = replies(world, "kwame.asante", "R0-catching-up")
    assert world._time(catch["fired_at"]) > t + timedelta(minutes=2)
    race = replies(world, "kwame.asante", "R2-race")
    assert race and world._time(race[0]["fired_at"]) >= t + timedelta(minutes=9)
