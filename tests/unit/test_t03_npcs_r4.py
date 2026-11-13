"""Priya answers the asks T03R4-opus-2 sent her (the `R4/` texts in test_t03_npcs_texts.json, verbatim): a list a
markdown-wrapped colon or "Two things need you:" opens, "blocked on your APPROVED", a draft quoted under "Proposed
post, for your OK:", and "the draft above (m-18)" read as that message's draft."""

from test_t03_npcs import T, rules, world
from test_t03_npcs_r3 import post
from test_t03_scenario import approvals


def test_an_ask_whose_colon_sits_in_markdown_opens_its_list():
    w = world()
    post(w, "R4/opus-2/m-5")
    assert [a["rule"] for a in approvals(w)] == ["R21-revert-plat-2291"]


def test_blocked_on_your_approved_is_an_ask():
    w = world()
    post(w, "R4/opus-2/m-7")
    assert [a["rule"] for a in approvals(w)] == ["R21-revert-plat-2291"]


def test_the_changes_and_the_post_for_your_ok_draw_one_record():
    w = world()
    post(w, "R4/opus-2/m-18", "m-18")
    (a,) = approvals(w)
    assert a["rule"] == "R20-revert-hold-and-status" and "back to 120" in a["change"]
    assert "R8-os-limit" not in rules(w, "priya.raman")


def test_can_i_post_the_draft_above_reads_that_message():
    w = world()
    w.timeline.msgs["m-18"] = T["R4/opus-2/m-18"]["text"]  # sent, not yet answered
    post(w, "R4/opus-2/m-25")
    assert [a["rule"] for a in approvals(w)] == ["R7-status-post"]
    w = world()
    post(w, "R4/opus-2/m-18", "m-18")
    post(w, "R4/opus-2/m-25")
    assert [a["rule"] for a in approvals(w)] == ["R20-revert-hold-and-status", "R7-status-post"]
    assert "R7a-status-draft" not in rules(w, "priya.raman")
