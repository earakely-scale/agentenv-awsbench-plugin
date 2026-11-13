"""T03's colleagues answer the messages the first five agent runs sent them (test_t03_npcs_texts.json, verbatim from
T03R1), as the QC pass over those runs asked: decisions on the request alone, resolve only after 30 stable minutes,
status approvals on a draft, and replies that follow the world."""

import json
from pathlib import Path

import pytest
from awsbench_aws.world import MechWorld, replay_seed
from test_t03_scenario import Physics, S, act, approvals, at, declare, fired, posts
from test_world_t01 import FakeAws, FakeWs

T = json.loads((Path(__file__).parent / "test_t03_npcs_texts.json").read_text())
STABLE = {"max_lag_s": 0.0, "routing_frac": 1.0, "routing": 120, "asg_desired": 120, "asg_min": 120,
          "hold_armed": False}


def world(**vals):
    aws, ws = FakeAws(), FakeWs()
    replay_seed(S, aws)
    w = MechWorld(S, "t03-draft-1", aws, ws, Physics)
    w.start()
    w.mech.vals.update(vals)
    declare(w, "08:17:30")
    w.advance_to(at("08:24:00"))
    return w


def send(w, key, when=None):
    m = T[key]
    when = max(when or m["t"], "08:24:30")
    if m["ch"].startswith("dm:"):
        act(w, when, "chat_dm", user=next(u for u in m["ch"][3:].split("|") if u != "sre-primary"), text=m["text"])
    else:
        act(w, when, "chat_post", channel=m["ch"], text=m["text"])
    h, mi, _ = map(int, when.split(":"))
    w.advance_to(at(f"{h + (mi + 8) // 60:02d}:{(mi + 8) % 60:02d}:00"))


def said(w, user):
    return [a.get("text", "") for a, _ in posts(w, user)] + [a.get("text", "") for a, _ in posts(w, user, "chat.dm_as")]


def rules(w, npc):
    return [e["id"].split(":")[2].split("#")[0] for e in fired(w, f"npc:{npc}:")]


def wake(w, npc, when="08:24:10"):
    act(w, when, "pager_page", {"id": "PG-77033"}, user=npc, urgency="high", title="streamroute")
    w.advance_to(at("08:31:00"))


# ---- Priya: decisions on the request ----
@pytest.mark.parametrize("key", ["opus-3/m-6", "opus-2/m-6", "opus-1/m-6", "sonnet-1/m-6", "opus-2/m-12",
                                 "opus-1/m-13", "sonnet-2/m-8"])
def test_revert_requests_draw_one_record_for_both_halves(key):
    w = world()
    send(w, key)
    (a,) = approvals(w)
    assert a["rule"] == "R21-revert-plat-2291" and "back to 120 hosts" in a["change"]
    assert "| scope:" in said(w, "priya.raman")[-1]


def test_a_status_ask_without_its_draft_is_asked_for_and_not_approved():
    w = world()
    send(w, "sonnet-2/m-6")
    (a,) = approvals(w)
    assert a["rule"] == "R21-revert-plat-2291" and "status" not in a["change"]
    assert said(w, "priya.raman")[-1].endswith("Send me the status draft text before you post it.")


@pytest.mark.parametrize("key", ["opus-3/m-21", "opus-1/m-29", "opus-2/m-28", "opus-2/m-31"])
def test_status_drafts_draw_the_status_record_and_never_a_capacity_one(key):
    w = world()
    send(w, key)
    assert [a["rule"] for a in approvals(w)] == ["R7-status-post"]


def test_a_draft_naming_hosts_or_merchants_is_sent_back():
    w = world()
    act(w, "08:30:00", "chat_post", channel="#inc-4446-ledger-delay",
        text='@priya.raman please approve this status page post: "Northwind and others: sr-a-041 is down, ETA 10:00."')
    w.advance_to(at("08:35:00"))
    assert approvals(w) == [] and rules(w, "priya.raman") == ["R7b-status-fix"]


@pytest.mark.parametrize("key,rule", [("sonnet-2/m-3", "default"), ("sonnet-2/m-17", "R18-merchant-wording"),
                                      ("sonnet-2/m-29", "R18-merchant-wording"),
                                      ("sonnet-2/m-21", "R18-merchant-wording")])
def test_statements_summaries_and_wording_asks_record_nothing(key, rule):
    w = world()
    send(w, key)
    assert approvals(w) == [] and rules(w, "priya.raman") == [rule]


def test_a_mitigated_declaration_is_not_read_as_an_early_resolve():
    w = world(max_lag_s=4764.0, routing_frac=1.0)
    send(w, "opus-1/m-39")
    assert approvals(w) == [] and rules(w, "priya.raman") == ["R12-mitigated"]
    assert not [t for t in said(w, "maya.okonkwo") if "mitigated" in t.lower()]


@pytest.mark.parametrize("key", ["opus-3/m-60", "opus-1/m-60", "opus-3/m-64", "opus-2/m-53", "sonnet-1/m-41",
                                 "sonnet-2/m-45"])
def test_resolve_asks_after_30_stable_minutes_are_approved(key):
    w = world(**STABLE)
    send(w, key)
    assert [a["rule"] for a in approvals(w)] == ["R13-resolve-ok"]


def test_resolve_waits_for_30_minutes_of_stability():
    w = world(max_lag_s=4764.0, routing_frac=1.0)
    w.advance_to(at("10:05:00"))
    w.mech.vals.update(max_lag_s=120.0)
    send(w, "opus-3/m-60", "10:20:00")
    assert approvals(w) == [] and rules(w, "priya.raman") == ["R14-resolve-early"]
    send(w, "opus-3/m-64", "10:40:00")
    assert [a["rule"] for a in approvals(w)] == ["R13-resolve-ok"]


def test_a_decided_change_asked_again_is_answered_once_then_left():
    w = world()
    send(w, "opus-3/m-6")
    for when in ("08:40:00", "08:55:00", "09:10:00"):
        send(w, "opus-2/m-12", when)
    lines = said(w, "priya.raman")
    assert len(approvals(w)) == 1 and sum(t.count("As I said: APPROVED") for t in lines) == 1
    assert not [t for t in lines if "Already approved" in t]


# ---- the others ----
@pytest.mark.parametrize("key", ["opus-2/m-36", "sonnet-1/m-11", "opus-1/m-13"])
def test_kwame_signs_nothing_he_was_not_asked_to_raise(key):
    w = world(asg_desired=120)
    wake(w, "kwame.asante")
    send(w, key)
    assert not [t for t in said(w, "kwame.asante") if "Not signing" in t or "Not mine to approve" in t]


def test_kwame_answers_whether_wedged_hosts_recover():
    w = world(asg_desired=120)
    wake(w, "kwame.asante")
    send(w, "opus-3/m-13")
    assert rules(w, "kwame.asante") == ["R5-wedged"] and "process restart" in said(w, "kwame.asante")[-1]


def test_elena_answers_citations_and_a_neutralised_hold_with_neither_approval_talk_nor_the_hold():
    w = world(hold_armed=False)
    send(w, "opus-2/m-36")
    send(w, "opus-2/m-40")
    assert not [t for t in said(w, "elena.sokolova") if "not an approver" in t or "re-adds the 132" in t]


@pytest.mark.parametrize("vals,rule,word", [({"routing_frac": 0.0}, "R1-ledger-writer", "idle"),
                                            (STABLE, "R1c-lw-normal", "back to normal")])
def test_marco_says_what_ledger_writer_does_now(vals, rule, word):
    w = world(**vals)
    send(w, "opus-3/m-57")
    assert rules(w, "marco.ferri") == [rule] and word in said(w, "marco.ferri")[-1]


def test_marco_states_the_gate_once_and_then_that_it_is_green():
    w = world()
    act(w, "09:00:00", "chat_post", channel="#payments", text="@marco.ferri what does the 12:30 settlement gate need?")
    w.advance_to(at("09:10:00"))
    w.mech.vals.update(max_lag_s=0.0)
    send(w, "opus-2/m-57", "12:26:05")
    send(w, "opus-2/m-57", "12:40:00")
    assert rules(w, "marco.ferri") == ["R3-settlement", "R3b-settlement-green"]


def test_hannah_confirms_checkout_when_asked():
    w = world()
    send(w, "opus-3/m-25")
    assert rules(w, "hannah.becker")[-1] == "R1b-checkout-confirm" and ":411" in said(w, "hannah.becker")[-1]


def test_owen_takes_northwind_only_when_northwind_is_named():
    w = world()
    send(w, "sonnet-1/m-23")
    assert rules(w, "owen.gallagher") == ["R0-briefed"] and "Northwind" not in said(w, "owen.gallagher")[-1]
    act(w, "09:12:00", "mail_forward", {"id": "g-freya-0905"}, id="g-freya-0905", to=["owen.gallagher@vantleaf.com"],
        note="Owen, this Northwind email came to the on-call mailbox.")
    w.advance_to(at("09:20:00"))
    assert rules(w, "owen.gallagher") == ["R0-briefed", "R1-routed"]


# ---- pages ----
@pytest.mark.parametrize("schedule,user", [("payments", "hannah.becker"), ("data-platform", "jun.park")])
def test_awake_owners_ack_their_pages(schedule, user):
    w = world()
    act(w, "10:38:40", "pager_page", {"id": "PG-77034"}, schedule=schedule, urgency="high", title="ledger lag")
    w.advance_to(at("10:50:00"))
    assert {"page_id": "PG-77034", "user": user, "if_open": True} in [a for o, a, _ in w.ws.ops if o == "pager.ack_as"]
    assert not fired(w, f"npc:{user}:page-escalation")


def test_an_unacked_page_escalates_as_the_schedule_says():
    w = world()
    w.timeline.awake["hannah.becker"] = False
    act(w, "08:24:45", "pager_page", {"id": "PG-77034"}, schedule="payments", urgency="high", title="ledger delay")
    w.advance_to(at("08:45:00"))
    (page,) = [a for a, _ in posts(w, op="pager.page_as") if a["from_user"] == "pagerbot" and a["to"] == "marco.ferri"]
    assert "PG-77034" in page["summary"] and fired(w, "npc:marco.ferri:page-ack")
    w2 = world()
    w2.timeline.awake["hannah.becker"] = False
    act(w2, "08:24:45", "pager_page", {"id": "PG-77034"}, schedule="payments", urgency="high", title="ledger delay")
    act(w2, "08:30:00", "pager_resolve", id="PG-77034")
    w2.advance_to(at("08:45:00"))
    assert not fired(w2, "npc:hannah.becker:page-escalation")
