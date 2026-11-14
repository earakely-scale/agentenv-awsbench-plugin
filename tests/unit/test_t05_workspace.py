"""T05's workspace: the scenario loads, and the status page's origin mode (off elsewhere) queues posts while the origin
is down, shows the world's public view, and publishes the queue on recovery."""

from datetime import datetime

from awsbench_workspace.modules import apply, load_scenario, status
from awsbench_workspace.modules.base import Workspace

T = datetime.fromisoformat("2026-11-20T10:46:10-05:00")


def ws():
    w = Workspace()
    load_scenario(w, "t05")
    return w


def test_t05_loads_with_its_records():
    w = ws()
    keys = {i["key"] for i in w.store.items("tickets.issue")}
    assert {"OPS-4471", "OPS-4470", "FRZ-103", "PLAT-1290"} <= keys
    assert w.config["status_origin"] == {"healthy": False}


def test_posts_queue_while_the_origin_is_down_and_publish_on_recovery():
    w = ws()
    out, _ = status.create(w, "sre-primary", T, name="Receipts affected", status="identified", body="We are seeing errors.",
                           components={"receipts": "major_outage"})
    assert out["state"] == "accepted" and out["publish"] == "pending"
    apply(w, "status.set_view", {"view": {"source": "fallback", "headline": "Receipts affected"}}, T.isoformat())
    assert status.get(w, "sre-primary", T)["public_view"]["source"] == "fallback"
    apply(w, "status.set_origin", {"healthy": True}, T.isoformat())
    assert status.get(w, "sre-primary", T)["incidents"][-1]["publish"] == "published"


def test_origin_mode_is_off_elsewhere():
    w = Workspace()
    load_scenario(w, "t03")
    out, _ = status.create(w, "sre-primary", T, name="x", status="investigating", body="y")
    assert "publish" not in out and "status_origin" not in w.config
