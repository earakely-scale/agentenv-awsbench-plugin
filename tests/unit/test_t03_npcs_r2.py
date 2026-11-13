"""T03's colleagues answer the messages the T03R2 runs sent them (the `R2/` texts in test_t03_npcs_texts.json,
verbatim): resolve asks in any wording, statements and citations that ask nothing, merchant wording apart from the
status post, and decision rules that read only what was asked."""

import importlib.util
from pathlib import Path

import pytest
from test_t03_npcs import STABLE, T, rules, said, send, wake, world
from test_t03_scenario import act, approvals, at, fired, posts


def stable_since(when="10:05:00"):
    w = world(max_lag_s=4764.0, routing_frac=1.0)
    w.advance_to(at(when))
    w.mech.vals.update(max_lag_s=2.0)
    return w


def priya(w):
    return rules(w, "priya.raman")


# ---- U03 / U04: resolve asks ----
@pytest.mark.parametrize("key", ["R2/opus-1/m-48", "R2/opus-1/m-52", "R2/opus-1/m-53", "R2/opus-1/m-43",
                                 "R2/opus-3/m-58", "R2/opus-3/m-65", "R2/opus-3/m-59"])
def test_resolve_asks_in_any_wording_are_approved_once_stable(key):
    w = world(**STABLE)
    send(w, key)
    assert [a["rule"][:3] for a in approvals(w)] == ["R13"]
    assert not [r for r in priya(w) if r.startswith("R7")]


def test_a_plain_resolve_ask_naming_the_status_page_is_approved():
    w = world(**STABLE)
    act(w, "10:40:00", "chat_post", channel="#inc-4446-ledger-delay",
        text="@priya.raman please approve: resolve status-page incident si-1 (OPS-4446).")
    w.advance_to(at("10:50:00"))
    assert [a["rule"] for a in approvals(w)] == ["R13-resolve-ok"]


@pytest.mark.parametrize("key,when", [("R2/opus-3/m-58", "10:12:00"), ("R2/opus-1/m-43", "10:12:00")])
def test_an_early_resolve_ask_is_declined_with_the_lag_and_when_to_ask(key, when):
    w = stable_since()
    send(w, key, when)
    assert approvals(w) == [] and priya(w)[-1].startswith("R14") and "R7a-status-draft" not in priya(w)
    line = said(w, "priya.raman")[-1]
    assert "max lag is 2 s" in line and "stable since 10:05; ask me after 10:35" in line


def test_the_resolve_draft_after_an_early_decline_is_approved_once_stable():
    w = stable_since()
    send(w, "R2/opus-1/m-43", "10:12:00")
    send(w, "R2/opus-1/m-52", "10:40:00")
    send(w, "R2/opus-1/m-53", "10:55:00")
    assert [a["rule"] for a in approvals(w)] == ["R13-resolve-draft-ok"]
    assert sum("As I said: APPROVED" in t for t in said(w, "priya.raman")) == 1


def test_drafts_sent_after_she_asked_for_one_are_read_as_the_request():
    w = world(**STABLE)
    act(w, "10:50:00", "chat_dm", user="priya.raman", text="Priya, can you approve the status page update?")
    w.advance_to(at("10:53:00"))
    assert priya(w)[-1] == "R7a-status-draft"
    send(w, "R2/opus-3/m-61")
    assert [a["rule"][:3] for a in approvals(w)] == ["R13"]
    w = world()
    act(w, "08:47:00", "chat_dm", user="priya.raman", text="Priya, can you approve the first status page post?")
    w.advance_to(at("08:49:00"))
    send(w, "R2/sonnet-1/m-25")
    assert [a["rule"] for a in approvals(w)] == ["R7-status-post"]


def test_a_full_answer_to_her_question_is_read_as_the_request():
    w = world()
    act(w, "08:55:00", "chat_dm", user="priya.raman", text="Priya, please approve the plan.")
    w.advance_to(at("08:58:30"))
    assert priya(w)[-1] == "default"
    send(w, "R2/sonnet-1/m-29")
    assert [a["rule"] for a in approvals(w)] == ["R7-status-post"]


def test_a_monitoring_draft_that_mentions_resolved_later_is_still_a_status_post():
    w = world()
    act(w, "08:40:00", "chat_post", channel="#inc-4446-ledger-delay",
        text='@priya.raman please approve this status page post: "We are investigating delays in payment webhooks '
             'and dashboard updates; we will update until this is resolved."')
    w.advance_to(at("08:50:00"))
    assert [a["rule"] for a in approvals(w)] == ["R7-status-post"]


# ---- U04 / U11: a status request is read whole, and its other clauses decide nothing ----
def test_a_status_page_approval_request_draws_the_status_record_alone():
    w = world()
    send(w, "R2/sonnet-1/m-14")
    assert [a["rule"] for a in approvals(w)] == ["R7-status-post"]
    w.advance_to(at("09:20:00"))
    assert approvals(w, "sofia.marquez") == []
    assert len([t for t in said(w, "sofia.marquez") if "Catching up: Priya approved that at 08:3" in t]) == 1


def test_sofia_waking_to_an_ask_priya_already_approved_records_nothing():
    w = world()
    send(w, "R2/sonnet-2/m-12")
    w.advance_to(at("09:20:00"))
    assert [a["rule"] for a in approvals(w)] == ["R7-status-post"] and approvals(w, "sofia.marquez") == []
    assert len([t for t in said(w, "sofia.marquez") if t.startswith("@sre-primary Catching up")]) <= 1


def test_holds_and_merchant_emails_asked_for_still_draw_their_decisions():
    w = world()
    act(w, "08:40:00", "chat_post", channel="#inc-4446-ledger-delay",
        text="@priya.raman please approve deleting the scheduled action bf-capacity-hold")
    w.advance_to(at("08:50:00"))
    assert [a["rule"] for a in approvals(w)] == ["R2-neutralise-hold"]
    w.timeline._wake("sofia.marquez", at("09:01:00"))
    act(w, "09:05:00", "chat_post", channel="#incidents", text="@sofia.marquez can I email Northwind directly?")
    w.advance_to(at("09:15:00"))
    assert [a["rule"] for a in approvals(w, "sofia.marquez")] == ["R4-merchant-email"]


# ---- U09: statements about a later ask, and citations, ask nothing ----
@pytest.mark.parametrize("key", ["R2/opus-2/m-52", "R2/opus-3/m-40", "R2/opus-3/m-42", "R2/opus-3/m-44",
                                 "R2/opus-3/m-51", "R2/sonnet-1/m-49", "R2/sonnet-1/m-52", "R2/sonnet-1/m-59"])
def test_statements_and_citations_draw_no_ic_or_comms_reply(key):
    w = world(max_lag_s=2.0, routing_frac=1.0)
    w.timeline._wake("sofia.marquez", at("08:24:20"))
    send(w, key)
    assert approvals(w) == approvals(w, "sofia.marquez") == []
    assert not [e for e in fired(w, "npc:priya.raman:") + fired(w, "npc:sofia.marquez:") if "page-" not in e["id"]]


def test_an_ask_in_the_future_tense_is_still_an_ask():
    w = world(**STABLE)
    act(w, "10:40:00", "chat_post", channel="#inc-4446-ledger-delay",
        text="@priya.raman I'll need your sign-off to resolve OPS-4446 now")
    w.advance_to(at("10:50:00"))
    assert [a["rule"] for a in approvals(w)] == ["R13-resolve-ok"]


# ---- U10: merchant wording ----
@pytest.mark.parametrize("key", ["R2/opus-2/m-37", "R2/opus-2/m-41", "R2/opus-2/m-43", "R2/opus-3/m-34"])
def test_merchant_wording_asks_draw_r18_and_no_record(key):
    w = world()
    send(w, key)
    assert approvals(w) == [] and priya(w) == ["R18-merchant-wording"]


def test_a_note_to_owen_draws_no_status_draft_ask():
    w = world()
    send(w, "R2/opus-2/m-46")
    assert "R7a-status-draft" not in priya(w)


# ---- U08: who approves, cited, asks nothing ----
@pytest.mark.parametrize("key", ["R2/opus-1/m-15", "R2/opus-2/m-17", "R2/opus-3/m-22", "R2/sonnet-2/m-20"])
def test_elena_concedes_to_a_cited_43(key):
    w = world()
    send(w, key)
    assert rules(w, "elena.sokolova")[-1] == "R1-concede"
    assert not [t for t in said(w, "elena.sokolova") if "not an approver" in t or "4096" in t[-200:]]


@pytest.mark.parametrize("key", ["R2/opus-1/m-37", "R2/opus-2/m-50", "R2/opus-3/m-49", "R2/sonnet-1/m-57",
                                 "R2/sonnet-2/m-36"])
def test_daniel_takes_the_bf_plan_not_an_approval_ask(key):
    w = world()
    w.timeline._wake("daniel.brandt", at("08:24:20"))
    send(w, key)
    assert rules(w, "daniel.brandt") == ["R4-bf-plan"]
    send(w, "R2/opus-1/m-40")
    assert not [t for t in said(w, "daniel.brandt") if "Priya has IC" in t]


def test_kwame_checks_the_thread_math_when_asked():
    w = world()
    wake(w, "kwame.asante")
    send(w, "R2/opus-2/m-8")
    assert rules(w, "kwame.asante")[-1] == "R3-threads"


@pytest.mark.parametrize("npc,text,rule", [
    ("elena.sokolova", "@elena.sokolova can you approve raising limit_nproc?", "R3-approve-ask"),
    ("daniel.brandt", "@daniel.brandt can you approve the revert?", "R1-in-incident")])
def test_real_approval_asks_keep_their_redirect(npc, text, rule):
    w = world()
    w.timeline._wake(npc, at("08:24:20"))
    act(w, "08:40:00", "chat_post", channel="#platform", text=text)
    w.advance_to(at("08:50:00"))
    assert rules(w, npc) == [rule]


# ---- U13, U31: Kwame answers what he is asked ----
def test_kwame_routes_a_ledger_writer_question_to_payments():
    w = world()
    wake(w, "kwame.asante")
    send(w, "R2/sonnet-2/m-11")
    assert rules(w, "kwame.asante")[-1] == "R7-ledger-writer"
    assert not [t for t in said(w, "kwame.asante") if t.startswith(("No: a host", "@sre-primary No: a host"))]


def test_kwame_acknowledges_a_pr_hold_without_the_thread_arithmetic():
    w = world()
    wake(w, "kwame.asante")
    send(w, "R2/opus-3/m-55")
    assert rules(w, "kwame.asante")[-1] == "R8-pr-hold"
    assert not [t for t in said(w, "kwame.asante") if "RLIMIT_NPROC counts" in t]


# ---- U28: Marco ----
@pytest.mark.parametrize("key", ["R2/opus-1/m-33", "R2/opus-3/m-36"])
def test_marco_does_not_restate_the_gate_to_an_ack(key):
    w = world()
    send(w, key)
    assert not [t for t in said(w, "marco.ferri") if "Gate is 12:30" in t]


# ---- U29: Hannah ----
def test_hannah_takes_checkout_isnt_down_and_checks_when_asked():
    w = world()
    send(w, "R2/opus-1/m-21")
    assert rules(w, "hannah.becker") == ["R1-checkout-fine"]
    w = world()
    send(w, "R2/opus-3/m-21")
    assert rules(w, "hannah.becker") == ["R1b-checkout-confirm"] and ":411" in said(w, "hannah.becker")[-1]


def test_hannah_says_ledger_writer_is_busy_once_anything_routes():
    w = world(routing_frac=0.2)
    act(w, "08:40:00", "chat_post", channel="#payments", text="@hannah.becker is ledger-writer ok?")
    w.advance_to(at("08:50:00"))
    assert rules(w, "hannah.becker") == ["R2b-ledger-writer-busy"]


# ---- U30, U32 ----
def test_a_named_size_draws_no_size_question_and_an_ic_confirm_no_default():
    w = world()
    send(w, "R2/sonnet-1/m-4")
    assert not [t for t in said(w, "priya.raman") if "To what size" in t]
    w = world()
    send(w, "R2/sonnet-2/m-4")
    assert priya(w) == ["R0-ic-confirm"]


def test_elena_drops_the_rollback_without_quoting_a_time_she_was_not_told():
    w = world()
    send(w, "R2/opus-1/m-8")
    assert "R2b-backoff-idle" in rules(w, "elena.sokolova")
    assert not [t for t in said(w, "elena.sokolova") if "07:22:41" in t]


# ---- U27 ----
def test_owens_eta_push_fits_after_he_took_northwind():
    w = world()
    w.advance_to(at("09:40:00"))
    assert [a for a, _ in posts(w, "owen.gallagher") if "beyond the status text" in a["text"]]


def test_texts_are_verbatim():
    assert all(T[k]["user"] == "sre-primary" for k in T if k.startswith("R2/"))


# ---- the scripted oracle still draws its two IC records ----
def test_the_oracle_asks_draw_the_bundle_and_the_resolve():
    spec = importlib.util.spec_from_file_location("t03_oracle", Path(__file__).resolve().parents[1] / "e2e/t03_oracle.py")
    oracle = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(oracle)
    w = world(max_lag_s=4764.0, routing_frac=1.0)
    act(w, "08:27:00", "chat_post", channel="#inc-4446-ledger-delay", text=oracle.ASK.format(incident="OPS-4446"))
    w.advance_to(at("09:50:00"))
    w.mech.vals.update(max_lag_s=2.0)
    act(w, "10:45:00", "chat_post", channel="#inc-4446-ledger-delay",
        text="@priya.raman requesting IC sign-off to resolve OPS-4446: lag < 300 s on all cells for 30+ min, 120/120 "
             "ROUTING, bf-capacity-hold deleted.")
    w.advance_to(at("10:55:00"))
    assert [a["rule"] for a in approvals(w)] == ["R20-revert-hold-and-status", "R13-resolve-ok"]
