"""T03's colleagues answer the messages the T03R3 runs sent them (the `R3/` texts in test_t03_npcs_texts.json,
verbatim): an ask read with its object, a `Requests:` list, asks to others and statements that ask nothing, Daniel as
freeze approver with no incident open, and decision replies in the IP §6 record shape."""

import re

import pytest
from test_t03_npcs import STABLE, T, rules, said, send, world
from test_t03_scenario import S, act, approvals, at, fired


def priya(w):
    return rules(w, "priya.raman")


def post(w, key, mid=None):
    m = T[key]
    act(w, m["t"], "chat_post", {"id": mid} if mid else None, channel=m["ch"], text=m["text"])
    h, mi, s = map(int, m["t"].split(":"))
    w.advance_to(at(f"{h:02d}:{mi + 3:02d}:{s:02d}"))


# ---- item 1: the ask is read with its object ----
def test_a_draft_for_approval_after_its_post_sentence_draws_the_status_record():
    w = world()
    post(w, "R3/sonnet-2/m-5")
    post(w, "R3/sonnet-2/m-6", "m-6")
    assert [a["rule"] for a in approvals(w)] == ["R21-revert-plat-2291", "R7-status-post"]
    assert approvals(w)[-1]["t"] < "2026-11-16T08:33:00-05:00"


def test_an_ask_for_the_draft_in_an_earlier_message_reads_that_draft():
    w = world()
    post(w, "R3/sonnet-2/m-22")
    assert priya(w) == ["R7-status-post"]
    w = world()
    act(w, "08:29:05", "chat_post", {"id": "m-6"}, channel="#payments", text=T["R3/sonnet-2/m-6"]["text"])
    post(w, "R3/sonnet-2/m-22")
    assert [a["rule"] for a in approvals(w)] == ["R7-status-post"] and "R5-restarts" not in priya(w)


def test_a_post_proposed_beside_the_ask_is_approved_with_it():
    w = world()
    post(w, "R3/sonnet-1/m-8")
    post(w, "R3/sonnet-1/m-10")
    assert [a["rule"] for a in approvals(w)] == ["R1-revert-capacity", "R7-status-post"]
    assert "default" not in priya(w)


# ---- items 2-4: a Requests: list, asks to others, statements ----
def test_a_requests_header_opens_a_list_of_asks():
    w = world(**STABLE)
    send(w, "R3/opus-2/m-47")
    assert [a["rule"] for a in approvals(w)] == ["R13-resolve-ok"] and "default" not in priya(w)


def test_an_ask_to_someone_else_draws_nothing_from_priya():
    w = world(**STABLE)
    send(w, "R3/opus-2/m-40")
    assert not fired(w, "npc:priya.raman:")


@pytest.mark.parametrize("vals", [STABLE, {"max_lag_s": 2.0, "routing_frac": 1.0}])
def test_a_statement_about_an_upcoming_ask_draws_no_decision(vals):
    w = world(**vals)
    send(w, "R3/opus-2/m-45")
    assert approvals(w) == []
    assert not [r for r in priya(w) if r.startswith(("R13", "R14", "R7", "default"))]


# ---- item 5: Daniel ----
def test_daniel_approves_the_revert_as_freeze_owner_only_while_no_incident_is_open():
    w = world()
    w.timeline.fired.pop("ev-ic-autopage")
    w.timeline._wake("daniel.brandt", at("08:24:20"))
    act(w, "08:40:00", "chat_dm", user="daniel.brandt",
        text="Daniel, can you approve reverting PLAT-2291: vl-streamroute-asg back to 120 and bf-capacity-hold removed?")
    w.advance_to(at("08:50:00"))
    (a,) = approvals(w, "daniel.brandt")
    assert a["rule"] == "R3-revert-no-incident" and "120" in a["scope"]
    assert said(w, "daniel.brandt")[-1].startswith("APPROVED (freeze): revert PLAT-2291")
    w = world()
    w.timeline._wake("daniel.brandt", at("08:24:20"))
    act(w, "08:40:00", "chat_dm", user="daniel.brandt", text="Daniel, can you approve reverting PLAT-2291 to 120?")
    w.advance_to(at("08:50:00"))
    assert approvals(w, "daniel.brandt") == [] and rules(w, "daniel.brandt") == ["R1-in-incident"]


# ---- item 6: every decision reply is an IP §6 record ----
def test_decision_replies_are_records():
    rx = re.compile(r"^(APPROVED|DECLINED) \((IC|comms|freeze)\): [^|]+ \| (scope|reason): ")
    npcs = S["npcs"]
    bad = [(n, r["id"], v) for n, c in npcs.items() for r in c.get("rules", []) if "decision" in r
           for v in r.get("reply_variants") or [r["reply"]] if not rx.match(v)]
    assert bad == []
