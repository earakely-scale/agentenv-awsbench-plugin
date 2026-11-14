# ruff: noqa: F811
"""T01's colleagues on the R12 and R13 runs' texts (verbatim, test_t01_r12_texts.json): Priya answers a restore ask
with her restore approval, not a resume decline drawn from a context sentence (H02); "e.g." ends no clause, so an
example in an "if DECLINED" aside draws no rollback decline (H13); Kwame reads "resume is yours/Daniel's to approve"
as a status line, not an ask (J18)."""

import json
from pathlib import Path

import pytest
from awsbench_aws.world import timeline as TL
from test_world_npcs import page_platform, post, replies, world  # noqa: F401
from test_world_t01 import at, ep

TEXTS = json.loads((Path(__file__).parent / "test_t01_r12_texts.json").read_text())


def awake(world, npc):
    world.timeline._wake(npc, at("02:40:00"))
    world.timeline.catch_up.pop(npc, None)
    return world.timeline


def approvals(world, by):
    return [a for a in world.export()["approvals"] if a["by"] == by]


def test_a_restore_ask_with_a_resume_context_sentence_draws_the_restore_answer(world):
    page_platform(world)
    ep(world, "epctl pause all", "02:58:00")
    world.advance_to(at("03:13:05"))
    assert awake(world, "priya.raman").classify("priya.raman", TEXTS["haiku2_m24"])[0]["id"] == "R11-restore"


def test_an_example_after_eg_draws_no_rollback_decline(world):
    assert not [c for c in TL.clauses(
        "Reply APPROVED or DECLINED. If DECLINED, tell me what you want instead (e.g. wait for Kwame, or roll checkout "
        "back to :411).") if ":411" in c]
    page_platform(world)
    ep(world, "epctl pause all", "02:58:00")
    post(world, "03:23:11", TEXTS["haiku6_c65"], tool="chat_dm", user="priya.raman")
    world.advance_to(at("03:30:00"))
    assert not approvals(world, "priya.raman")
    assert replies(world, "priya.raman", "R11-restore")


@pytest.mark.parametrize("text", [
    "@priya.raman can we resume endpointd?",
    "@priya.raman Kwame is asleep. Please approve resuming the enactors.",
    "@priya.raman OK to re-enable the planner now that ledger is restored?"])
def test_a_resume_ask_still_draws_priyas_resume_answer(world, text):
    assert awake(world, "priya.raman").classify("priya.raman", text)[0]["id"] == "R7-resume"


def test_resume_is_yours_to_approve_is_a_status_line_to_kwame(world):
    world.observe({"t": at("02:45:00"), "tool": "pager_page", "role": "sre-primary", "server": "workspace",
                   "args": {"schedule": "platform", "urgency": "high", "summary": "endpointd halted"},
                   "result": {"id": "PG-77054"}})
    post(world, "03:50:40", TEXTS["fable1_m22"], resolved={"mentions": ["hannah.becker", "kwame.asante"]})
    world.advance_to(at("04:05:00"))
    assert not approvals(world, "kwame.asante") and not replies(world, "kwame.asante", "R4-resume")


def test_a_real_resume_ask_to_kwame_still_draws_his_decline(world):
    page_platform(world)
    post(world, "03:50:40", "@kwame.asante resume is yours to approve: do you approve resuming endpointd now?")
    world.advance_to(at("04:05:00"))
    assert [a["rule"] for a in approvals(world, "kwame.asante")] == ["R4-resume"]
