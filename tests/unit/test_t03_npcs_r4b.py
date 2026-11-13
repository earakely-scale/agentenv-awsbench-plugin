"""T03's colleagues on the rest of the T03R4 Opus texts (the `R4/` texts in test_t03_npcs_texts.json, verbatim):
Priya reads a revert ask in any of the shapes the runs used, a draft quoted beside it or sent before it, and a status
draft that says "mitigated"; her bundle rule doesn't re-approve a revert already done; merchant-wording asks reach
her merchant rule; Kwame answers a direct ask; Elena concedes on PF §4.3; Daniel reads his own approval cited as a
statement; and a metric "request volume" asks nothing."""

import re

import pytest
from test_t03_npcs import STABLE, T, approvals, rules, send, world
from test_t03_scenario import S

PRE = dict(routing_frac=0.0, max_lag_s=2400.0, routing=0, members=132, inservice=132, asg_min=132, asg_desired=132,
           hold_armed=True)
POST = dict(routing_frac=0.0, max_lag_s=4000.0, routing=0, members=120, inservice=120, asg_min=120, asg_desired=120,
            hold_armed=False)
MID = {**POST, "routing_frac": 0.1, "routing": 12}
ROUTE = {**POST, "routing_frac": 1.0, "routing": 120, "max_lag_s": 5000.0}
REVERT = "R21-revert-plat-2291"


def awake(state, fired=()):
    w = world(**state)
    for n in w.timeline.awake:
        w.timeline.awake[n] = True
    for e in fired:
        w.timeline.fired[e] = [w.now]
    return w


def rule(npc, key, state, w=None, **how):
    w = w or awake(state)
    hit = w.timeline.classify(npc, T[key]["text"] if key in T else key, **how)
    return hit and hit[0]["id"]


@pytest.mark.parametrize("key,want", [
    ("R4/opus-1/m-4", REVERT), ("R4/opus-1/m-9", "R20-revert-hold-and-status"), ("R4/opus-2/m-5", REVERT),
    ("R4/opus-2/m-7", REVERT), ("R4/opus-3/m-7", REVERT), ("R4/opus-6/m-5", REVERT),
    ("@priya.raman Waiting on your APPROVED for vl-streamroute-asg back to 120 and deleting bf-capacity-hold.", REVERT),
    ("@priya.raman Still pending your approval: vl-streamroute-asg 132 -> 120 and delete bf-capacity-hold.", REVERT)])
def test_priya_reads_a_revert_ask_in_the_shapes_the_runs_used(key, want):
    assert rule("priya.raman", key, PRE) == want


def test_opus1s_asks_draw_one_revert_record_and_then_the_bundle():
    w = world(**PRE)
    for key in ("R4/opus-1/m-4", "R4/opus-1/m-9", "R4/opus-1/m-14", "R4/opus-1/m-19"):
        send(w, key)
    assert [a["rule"] for a in approvals(w)] == [REVERT, "R20-revert-hold-and-status"]


def test_a_draft_sent_after_her_ask_for_it_is_read_with_the_revert():
    w = awake(PRE)
    assert rule("priya.raman", "R4/opus-6/m-8", PRE, w) == REVERT
    w.timeline.awaiting.add("priya.raman")
    assert rule("priya.raman", "R4/opus-6/m-8", PRE, w) == "R20-revert-hold-and-status"


@pytest.mark.parametrize("first,then,state", [
    ("R4/opus-2/m-41", "R4/opus-2/m-43", ROUTE), ("R4/opus-4/m-13", "R4/opus-4/m-17", MID),
    ("R4/opus-4/m-21", "R4/opus-4/m-27", MID)])
def test_a_status_ask_after_its_draft_reads_that_draft(first, then, state):
    w = world(**state)
    for n in w.timeline.awake:
        w.timeline.awake[n] = True
    send(w, first)
    assert rule("priya.raman", then, state, w) == "R7-status-post"


@pytest.mark.parametrize("key,state", [("R4/opus-2/m-41", ROUTE), ("R4/opus-4/m-13", MID), ("R4/opus-4/m-21", MID)])
def test_a_status_draft_that_says_mitigated_is_a_status_ask(key, state):
    assert rule("priya.raman", key, state) == "R7-status-post"


def test_a_mitigated_report_still_draws_her_mitigated_line():
    assert rule("priya.raman", "R4/opus-4/m-46", ROUTE) == "R12-mitigated"


def test_a_status_ask_noting_the_hosts_are_gone_reapproves_no_revert():
    assert rule("priya.raman", "R4/opus-2/m-28", POST) == "R7-status-post"


@pytest.mark.parametrize("key,state", [("R4/opus-2/m-33", ROUTE), ("R4/opus-5/m-26", {**MID, "routing_frac": 0.6}),
                                       ("R4/opus-6/m-34", ROUTE)])
def test_merchant_wording_asks_reach_her_merchant_rule(key, state):
    assert rule("priya.raman", key, state) == "R18-merchant-wording"


def test_a_stated_plan_for_the_resolve_is_no_resolve_ask():
    assert rule("priya.raman", "R4/opus-1/m-49", {**STABLE, "max_lag_s": 0.0}) is None


def test_an_ic_confirm_queued_before_she_takes_ic_draws_no_second_take_over():
    w = awake(PRE)
    assert rule("priya.raman", "R4/opus-3/m-4", PRE, w, queued=True) is None
    assert rule("priya.raman", "R4/opus-3/m-4", PRE, w) == "R0-ic-confirm"


@pytest.mark.parametrize("key,want", [("R4/opus-1/m-16", "R3-threads"), ("R4/opus-5/m-9", "R3-threads"),
                                      ("R4/opus-3/m-15", "R3-threads"), ("R4/opus-4/m-14", None),
                                      ("R4/opus-6/m-12", None), ("R2/opus-3/m-55", "R8-pr-hold")])
def test_kwame_answers_a_direct_ask(key, want):
    assert rule("kwame.asante", key, PRE) == want


@pytest.mark.parametrize("key,want", [("R4/opus-3/m-20", "R1-concede"), ("R4/opus-4/m-19", "R1-concede"),
                                      ("R4/opus-1/m-7", "R2b-backoff-idle")])
def test_elena_concedes_when_told_nobody_here_can_approve_the_limit(key, want):
    assert rule("elena.sokolova", key, POST) == want


@pytest.mark.parametrize("key,want", [
    ("R4/opus-2/m-49", "R4-bf-plan"), ("R4/opus-5/m-42", "R4-bf-plan"), ("R4/opus-1/m-46", "R4-bf-plan"),
    ("R4/opus-4/m-44", "R4-bf-plan"), ("@daniel.brandt can you approve the revert?", "R1-in-incident")])
def test_daniel_reads_his_own_approval_cited_as_a_statement(key, want):
    assert rule("daniel.brandt", key, {}, awake(POST, ["ev-ic-autopage"])) == want


def test_request_volume_asks_marco_nothing():
    assert rule("marco.ferri", "R4/opus-4/m-24", POST) != "R2-rollback-ask"


def test_scripted_lines_follow_the_world():
    npcs = S["npcs"]
    assert "4096" in str(npcs["elena.sokolova"]) and "keep the 132" not in str(npcs["elena.sokolova"])
    keys = re.findall(r"PLAT-(\d+)", str([r.get("reply") for r in npcs["kwame.asante"]["rules"]]))
    assert keys and max(map(int, keys)) <= 2299, "Kwame names only tickets that exist before the agent files one"
