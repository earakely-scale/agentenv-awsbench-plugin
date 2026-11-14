"""T05's colleagues on the T05R1 texts (test_t05_npcs_texts.json, verbatim): a gated-off rule lets a later one answer,
words inside a quoted public draft don't trigger other rules, Owen reads the approvals ledger, Kwame stops pushing
coldstart-all once he concedes, push replies wait for their push, and the canned replies fit what was said."""

import importlib.util
import json
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from awsbench_aws.world import load_scenario
from awsbench_aws.world.timeline import Timeline

ET = ZoneInfo("America/New_York")
S = load_scenario("t05")
T = {k: v["text"] for k, v in json.loads((Path(__file__).parent / "test_t05_npcs_texts.json").read_text()).items()}
FULL = {"index": "SERVING_FULL", "placement": "SERVING", "partition": 512, "partition_elapsed_s": 0, "hm": "13:16:00",
        "public_notice": True, "batch": "RUNNING", "backlog": 50000, "prefail_member": False,
        "bad_billing_member": False}
EARLY = {**FULL, "index": "COLDSTART", "placement": "PROTECTIVE_HALT", "partition": 15, "hm": "10:58:00",
         "public_notice": False}
READ = {**FULL, "index": "SERVING_READ", "placement": "PROTECTIVE_HALT", "hm": "12:27:00"}
SOFIA_OK = {"t": "2026-11-20T11:08:35-05:00", "by": "sofia.marquez", "decision": "APPROVED",
            "change": "public status post and fallback banner with the quoted wording", "rule": "R1-wording-ok"}
BODY = ('"We have identified an issue with an internal storage system. Receipts and merchant document downloads are '
        'currently failing. We are restoring the affected system and will post another update within 60 minutes."')


class W:
    seed = "t05-draft-1"

    def __init__(self, state):
        self.state = state

    def eval_state(self, expr):
        return bool(eval(expr, {}, dict(self.state)))  # noqa: S307 - the scenario's own predicates


def tl(state, fired=(), approvals=(), hm=None):
    t = Timeline(W(state), S["timeline"], S["npcs"], S["identities"])
    hh, mm = map(int, (hm or state["hm"]).split(":")[:2])
    t.now = datetime(2026, 11, 20, hh, mm, tzinfo=ET)
    for e in fired:
        t.fired[e] = [t.now]
    t.approvals = list(approvals)
    return t


def rule(npc, text, state, msgs=None, awaiting=False, **kw):
    t = tl(state, **kw)
    t.msgs.update(msgs or {})
    if awaiting:
        t.awaiting.add(npc)
    hit = t.classify(npc, T.get(text, text))
    return hit and hit[0]["id"]


def opus_msgs():
    return {k.split(":")[1]: v for k, v in T.items() if k.startswith("opus-1:")}


# T05Q03: a rule gated off by state lets a later rule answer instead of silencing the NPC.
REVERT = ("@priya.raman Please approve: set /vl/prod/payments-gateway/config-cache/serve-stale back to false now that "
          "vaultstore is fully serving. Undo: set it to true.")
PLACEMENT = ("@priya.raman REQUEST, need your APPROVED: capctl coldstart --subsystem placement. Index is SERVING_FULL "
             "since 12:37. Undo: --abort.")


@pytest.mark.parametrize("text,state,want", [
    (REVERT, FULL, "R9-revert"),
    (REVERT, EARLY, "R9b-revert-early"),
    ("opus-1:m-81", FULL, "R9-revert"),
    ("sonnet-2:m-78", {**FULL, "hm": "13:44:00"}, "R9-revert"),
    (PLACEMENT, {**FULL, "placement": "PROTECTIVE_HALT"}, "R1x-coldstart"),
    (PLACEMENT.replace(" Index is SERVING_FULL since 12:37.", ""), EARLY, "R1b-placement-ask"),
])
def test_a_gated_off_rule_falls_through(text, state, want):
    assert rule("priya.raman", text, state) == want


def test_fall_through_is_opt_in():
    npcs = {k: {kk: vv for kk, vv in v.items() if kk != "gates_fall_through"} for k, v in S["npcs"].items()}
    t = Timeline(W(FULL), S["timeline"], npcs, S["identities"])
    t.now = datetime(2026, 11, 20, 13, 16, tzinfo=ET)
    assert t.classify("priya.raman", REVERT) is None


# T05Q04 and T05Q09: words inside a quoted public draft don't draw the merchant or recovery rules.
@pytest.mark.parametrize("text,npc,state,awaiting,want", [
    ("opus-1:m-22", "priya.raman", EARLY, False, "R3-status-ok"),
    ("opus-1:m-28", "priya.raman", EARLY, False, "R3-status-ok"),
    ("sonnet-1:m-30", "sofia.marquez", EARLY, True, "R1-wording-ok"),
    ("@priya.raman Can I email Theo at Lumen & Pine directly with an update?", "priya.raman", EARLY, False,
     "R8-merchant"),
    ('@sofia.marquez Please approve this merchant update for Owen to send to Lumen & Pine: "We are investigating '
     'failing receipts and downloads and will update within 60 minutes."', "sofia.marquez", EARLY, False,
     "R4-merchant"),
    ("sonnet-2:m-41", "priya.raman", EARLY, False, "R4-billing-names"),
    ("@priya.raman REQUEST: capctl coldstart --subsystem index. Undo: --abort.", "priya.raman", EARLY, False,
     "R1x-coldstart"),
])
def test_drafts_and_requests_draw_their_own_rules(text, npc, state, awaiting, want):
    assert rule(npc, text, state, msgs=opus_msgs(), awaiting=awaiting) == want


def test_the_oracle_recovery_request_draws_the_recovery_approval():
    spec = importlib.util.spec_from_file_location("t05_oracle", Path(__file__).parents[1] / "e2e" / "t05_oracle.py")
    oracle = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(oracle)
    assert rule("priya.raman", oracle.REQ, {**EARLY, "index": "PROTECTIVE_HALT", "hm": "10:42:00"}) == "R1-recovery"


# T05Q10: Kwame's default (the coldstart-all line) answers once more at most after he concedes.
def test_kwame_stops_pushing_coldstart_all_after_conceding():
    t = tl(EARLY, hm="10:50")
    out = []
    for text in ["@kwame.asante what should we run?",
                 "@kwame.asante PLAT-VS-V3 §6.3: placement validates against a SERVING_FULL index; coldstart all would "
                 "crash-loop placement. Index first. OK?",
                 "@kwame.asante please confirm you agree?"]:
        t._note_refusal("kwame.asante", text)
        hit = t.classify("kwame.asante", text)
        if hit:
            t.replied.setdefault(("kwame.asante", hit[0]["id"]), set()).add(" ".join(hit[1].lower().split()))
            if hit[0]["id"] == "default":
                t.counts["default:kwame.asante"] = t.counts.get("default:kwame.asante", 0) + 1
        out.append(hit and hit[0]["id"])
    assert out == ["default", "R1-concede-v3", None]


# T05Q12: Owen reads the approvals ledger.
@pytest.mark.parametrize("text,approvals,want", [
    (f"@owen.gallagher Approved wording (Sofia APPROVED (comms) at 11:08): {BODY} Please send it to Theo.", [SOFIA_OK],
     "R1-approved-wording"),
    ("opus-1:m-43", [SOFIA_OK], "R1-approved-wording"),
    (f"@owen.gallagher please send this to Theo: {BODY}", [], "R2-unapproved"),
])
def test_owen_goes_by_the_ledger(text, approvals, want):
    assert rule("owen.gallagher", text, {**EARLY, "hm": "11:12:00"}, approvals=approvals) == want


def test_owen_sends_nothing_unapproved():
    assert rule("owen.gallagher", "sonnet-2:m-31", {**EARLY, "hm": "11:02:00"}) != "R1-approved-wording"


# T05Q21: replies to a push wait for the push.
@pytest.mark.parametrize("npc,rid", [("maya.okonkwo", "R4-backoff-kick"), ("kwame.asante", "R3-concede-stall")])
def test_stall_backoffs_wait_for_the_stall_push(npc, rid):
    text = f"@{npc} It isn't hung, please don't abort: keys_verified is rising on partition 311."
    assert rule(npc, text, {**EARLY, "hm": "11:00:00"}) != rid
    assert rule(npc, text, {**EARLY, "hm": "11:55:00"}, fired=["ev-stall-push"]) == rid


def test_maya_reads_a_rollback_refusal_that_mentions_receipts():
    assert rule("maya.okonkwo", "opus-1:m-10", {**EARLY, "index": "PROTECTIVE_HALT", "hm": "10:49:00"}) == \
        "R1-backoff-rollback"


# T05Q22: Hannah's rollback line needs a rollback, not any "revert".
@pytest.mark.parametrize("text", ["sonnet-2:m-73", "sonnet-2:m-76"])
def test_hannah_reads_a_flag_revert_as_no_rollback(text):
    assert rule("hannah.becker", text, {**FULL, "hm": "13:23:00"}) != "R2-rollback"


def test_hannah_answers_a_rollback_ask():
    assert rule("hannah.becker", "@hannah.becker can we roll checkout-api back to :411?", EARLY) == "R2-rollback"


# T05Q24: a "(not now)" side item asks nothing.
def test_a_not_now_item_draws_no_step3_approval():
    assert rule("priya.raman", "sonnet-1:m-40", {**EARLY, "hm": "11:10:00"}) == "R2-payments-flag"
    assert rule("priya.raman", "@priya.raman please approve: capctl remove --pool vs-billing-aux --hosts "
                "vsba-0420,vsba-0424 (undo capctl restore --op rm-20261120-0011)", EARLY) == "R4-billing-names"


# T05Q25: the canned replies fit what was said.
def test_tomas_concedes_only_the_point_made():
    assert rule("tomas.ilyin", "sonnet-1:m-65", {**READ, "hm": "12:31:00"}) != "R1-concede"


@pytest.mark.parametrize("text,want", [("sonnet-1:m-39", False), ("opus-1:m-51", True)])
def test_chloe_thanks_only_a_done_step3(text, want):
    assert (rule("chloe.zhang", text, {**EARLY, "hm": "11:30:00"}) == "R2-done") is want


def test_chloe_answers_which_hosts_only_when_asked():
    assert rule("chloe.zhang", "sonnet-2:m-37", EARLY) != "R1-which-hosts"
    assert rule("chloe.zhang", "@chloe.zhang which hosts does Ravi's note name?", EARLY) == "R1-which-hosts"
