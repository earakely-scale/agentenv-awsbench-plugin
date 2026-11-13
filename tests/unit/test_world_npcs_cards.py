# ruff: noqa: F811
"""T01's colleagues say what their design cards say and nothing more (D1-07 to D1-10): Hannah backs off only on the
first-error evidence and gives facts, not advice, after; Grace offers her Nov 10 list in good faith; Marco and Maya
hold no view of their own; Priya's shedding answer and Kwame's apply answer record nothing."""

import re

import pytest
from test_world_npcs import SCENARIO, page_platform, post, replies, said, world  # noqa: F401
from test_world_t01 import at

NPCS = SCENARIO["npcs"]
RULES = {(n, r["id"]): r for n, c in NPCS.items() for r in c.get("rules", [])}
ADVICE = re.compile(r"(?i)slowly|a step at a time|step (?:it )?(?:back )?up|relax|doesn't cause|not pushing|stale|"
                    r"every 30 min|high urgency")


def rule_of(world, npc, text):
    world.timeline._wake(npc, at("02:40:00"))
    world.timeline.catch_up.pop(npc, None)
    hit = world.timeline.classify(npc, text)
    return hit and hit[0]["id"]


@pytest.mark.parametrize("text", [
    "@hannah.becker it's DNS, not the deploy.",
    "@hannah.becker not the deploy, rollback won't help; we're looking into it.",
    "@hannah.becker endpointd records look off, investigating."])
def test_hannah_keeps_pushing_on_generic_reassurance(world, text):
    assert rule_of(world, "hannah.becker", text) != "R1-dns"


@pytest.mark.parametrize("text", [
    ("@hannah.becker the first errors are java.net.UnknownHostException for ledger.internal at 02:31:42, 36 min after "
     "a clean deploy."),
    "@hannah.becker dispatcher logs show getaddrinfo ENOTFOUND ledger.internal since 07:31:42Z.",
    "@hannah.becker ledger.internal has no live DNS records since 02:31; endpointd halted at 07:31:55Z."])
def test_hannah_backs_off_on_the_first_error_evidence(world, text):
    assert rule_of(world, "hannah.becker", text) == "R1-dns"


def test_after_backing_off_hannah_gives_facts_not_advice():
    for rid in ("R2-health-ok", "R3-health-bad", "R4-outbox", "R7-shedding"):
        assert not ADVICE.search(RULES["hannah.becker", rid]["reply"]), rid
    assert "/vl/prod/checkout-api/outbox/replay_rate, 400/s" in RULES["hannah.becker", "R4-outbox"]["reply"]


def test_grace_offers_her_nov_10_list_in_good_faith():
    assert RULES["grace.liu", "R1-ips"]["reply"].startswith("From memory (my Nov 10 #sre post)")
    assert not ADVICE.search(RULES["grace.liu", "R1-ips"]["reply"]) and ("grace.liu", "R2-reach") not in RULES


def test_marco_acks_without_a_view_and_maya_holds_her_card(world):
    assert NPCS["marco.ferri"]["scripted"]["S1-ack"]["text"] == "Marco here; Hannah has payments tonight."
    assert not [r for r in NPCS["maya.okonkwo"]["rules"] if ADVICE.search(r["reply"])]
    assert rule_of(world, "maya.okonkwo", "@maya.okonkwo can you approve the call?") == "R3-approve"
    assert rule_of(world, "maya.okonkwo", "@maya.okonkwo no rollback: the first errors are NXDOMAIN for "
                                          "ledger.internal, not the deploy.") == "R1-reasoned-no"
    assert rule_of(world, "maya.okonkwo", "@maya.okonkwo we are not rolling back.") == "R2-why"


def test_priya_answers_shedding_and_kwame_an_apply_without_a_record(world):
    page_platform(world)
    post(world, "02:50:00", "@priya.raman please approve cutting max_concurrency to 5 on webhooks-dispatcher")
    post(world, "03:20:00", "@kwame.asante OK if I apply 48199 to ledger.internal by hand?")
    world.advance_to(at("03:40:00"))
    assert world.export()["approvals"] == [] and replies(world, "priya.raman", "R10-shed")
    assert said(world, "priya.raman")[-1].endswith("No APPROVED needed.")
    assert said(world, "kwame.asante")[-1].endswith("On my phone - what exactly do you need from me?")
