from datetime import UTC, datetime, timedelta

import pytest
from awsbench_workspace.store import NotFound, Store

T0 = datetime(2026, 11, 17, 7, 40, tzinfo=UTC)


def test_records_keep_full_history_and_one_sequence():
    s = Store()
    s.create("tickets.issue", "OPS-1", {"key": "OPS-1", "status": "Open", "severity": None}, by="seed", t=T0)
    changed = s.update("tickets.issue", "OPS-1", {"severity": "SEV1", "status": "Open"}, by="sre-primary",
                       t=T0 + timedelta(seconds=45))
    assert changed == {"severity": {"from": None, "to": "SEV1"}}
    assert s.update("tickets.issue", "OPS-1", {"severity": "SEV1"}, by="x", t=T0) == {}
    s.append("tickets.issue", "OPS-1", "comments", {"body": "hi"}, by="hannah.becker", t=T0, op="comment")
    hist = s.history("tickets.issue", "OPS-1")
    assert [h["op"] for h in hist] == ["create", "update", "comment"]
    assert [h["seq"] for h in hist] == [1, 2, 3]
    assert hist[1]["by"] == "sre-primary" and hist[1]["t"].startswith("2026-11-17T07:40:45")
    assert s.get("tickets.issue", "OPS-1")["comments"] == [{"body": "hi"}]


def test_history_is_a_copy_and_missing_records_raise():
    s = Store()
    s.create("wiki.page", "P", {"body": "a"}, by="seed", t=T0)
    s.history("wiki.page", "P")[0]["changes"]["body"] = "mutated"
    assert s.history("wiki.page", "P")[0]["changes"]["body"] == "a"
    with pytest.raises(NotFound):
        s.require("wiki.page", "nope")
    with pytest.raises(ValueError):
        s.create("wiki.page", "P", {}, by="seed", t=T0)


def test_export_groups_by_app_and_kind_in_creation_order():
    s = Store()
    s.create("chat.channel", "#b", {"name": "#b"}, by="seed", t=T0)
    s.create("chat.channel", "#a", {"name": "#a"}, by="seed", t=T0)
    s.create("mail.message", "m1", {"id": "m1"}, by="seed", t=T0)
    out = s.export()
    assert [r["id"] for r in out["chat"]["channel"]] == ["#b", "#a"]
    assert out["mail"]["message"][0]["history"][0]["op"] == "create"


def test_inbox_since():
    s = Store()
    a = s.deliver({"kind": "dm"})
    b = s.deliver({"kind": "page"})
    assert s.inbox_since(0) == [a, b] and s.inbox_since(a["seq"]) == [b]
