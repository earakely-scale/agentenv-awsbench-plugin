from datetime import timedelta

import pytest
from awsbench_workspace.modules import apply, chat, load_scenario, mail, pager, repo, status, tickets, wiki
from awsbench_workspace.modules.base import ToolError, Workspace

ME = "sre-primary"
BOX = "oncall@vantleaf.com"


@pytest.fixture
def ws():
    w = Workspace()
    load_scenario(w, "t01")
    return w


def later(ws, s=60):
    return ws.advance(s)


def test_t01_loads_the_facts_the_oracle_reads(ws):
    t = ws.clock()
    assert ws.iso(t) == "2026-11-17T02:40:00-05:00"
    issue = tickets.get(ws, ME, t, "OPS-4452")["issue"]
    assert issue["severity"] is None and "breaching since 2026-11-17T07:32:00Z" in issue["description"]
    rb = wiki.get(ws, ME, t, "OPS/EPD-RB-7 endpointd: endpoint empty or enactors halted")["page"]
    assert rb["version"] == 6 and "high" in rb["body"] and "APPROVED" in rb["body"]
    assert "--gen <applied gen>" in wiki.get(ws, ME, t, "EPD-RB-7", version=5)["page"]["body"]
    assert "verify=ok" in wiki.get(ws, ME, t, "PLAT/endpointd design")["page"]["body"]
    assert "Do **not** fail over" in wiki.get(ws, ME, t, "OPS/LEDG-RB-2 ledger-writer: elevated latency or 503s")[
        "page"]["body"]
    assert "every 30 min" in wiki.get(ws, ME, t, "Incident Process 2026")["page"]["body"]
    assert "the rotation IC or Daniel" in wiki.get(ws, ME, t, "Peak Freeze 2026")["page"]["body"]
    assert "ROLLBACK WARNING" in repo.read(ws, ME, t, "checkout-api", "RELEASE.md")["content"]
    assert "Cap the scalable target" in repo.read(ws, ME, t, "vantleaf/payments-gateway",
                                                  "services/webhooks-dispatcher/README.md")["content"]
    assert "900 s" in repo.read(ws, ME, t, "infra", "terraform/modules/node-dns/README.md")["content"]
    pg = pager.get(ws, ME, t, "PG-77052")["page"]
    assert pg["status"] == "triggered" and pg["acks"] == []
    assert pager.oncall(ws, ME, t, "sre-secondary")["schedules"][0]["oncall"] == "grace.liu"
    assert all(c["status"] == "operational" for c in status.get(ws, ME, t)["components"])
    assert chat.get_user(ws, ME, t, "kwame.asante")["user"]["status"]["text"].startswith("Do not disturb")


def test_chat_post_thread_dm_search_and_channels(ws):
    t = later(ws)
    posted, resolved = chat.post(ws, ME, t, "inc-4452", "I'm IC. @hannah.becker no rollback yet")
    m = posted["posted"]
    assert m["channel"] == "#inc-4452" and m["mentions"] == ["hannah.becker"]
    assert resolved["app"] == "chat" and resolved["mentions"] == ["hannah.becker"] and not resolved["external"]
    reply, _ = chat.post(ws, ME, later(ws), "#inc-4452", "follow-up", thread_id=m["id"])
    assert reply["posted"]["thread_id"] == m["id"]
    top = chat.read(ws, ME, ws.clock(), "#inc-4452")
    assert [x["replies"] for x in top["messages"] if x["id"] == m["id"]] == [1]
    assert len(chat.read(ws, ME, ws.clock(), "#inc-4452", thread_id=m["id"])["messages"]) == 2
    _, res = chat.dm(ws, ME, later(ws), "@kwame.asante", "are you up?")
    assert res["recipients"] == ["kwame.asante"]
    assert chat.read(ws, ME, ws.clock(), "@kwame.asante")["messages"][-1]["text"] == "are you up?"
    assert chat.search(ws, ME, ws.clock(), "ledger.internal resolves")["messages"][0]["id"] == "m-sre-1110"
    created, _ = chat.create_channel(ws, ME, later(ws), "inc-4452-comms", "comms", ["priya.raman"])
    assert created["created"]["members"] == [ME, "priya.raman"]
    with pytest.raises(ToolError):
        chat.post(ws, ME, ws.clock(), "#nope", "x")
    with pytest.raises(ToolError):
        chat.dm(ws, ME, ws.clock(), "nobody.here", "x")


def test_chat_world_ops_deliver_mentions_dms_and_channel_posts(ws):
    t = later(ws)
    r = apply(ws, "chat.post_as", {"channel": "#inc-4452", "user": "hannah.becker",
                                   "text": "@sre-primary roll back to :411 now"}, ws.iso(t))
    assert r["ok"] and [d["kind"] for d in r["deliveries"]] == ["mention"]
    assert r["deliveries"][0]["from"] == "hannah.becker" and r["deliveries"][0]["where"] == "#inc-4452"
    assert apply(ws, "chat.post_as", {"channel": "#payments", "user": "marco.ferri", "text": "fyi"},
                 None)["deliveries"][0]["kind"] == "channel"
    assert apply(ws, "chat.post_as", {"channel": "#exec", "user": "maya.okonkwo", "text": "status?"},
                 None)["deliveries"] == []
    d = apply(ws, "chat.dm_as", {"from_user": "priya.raman", "to_user": "sre-primary", "text": "you keep IC"}, None)
    assert d["deliveries"][0]["kind"] == "dm" and d["deliveries"][0]["preview"] == "you keep IC"
    mine, _ = chat.post(ws, ME, later(ws), "#inc-4452", "root")
    reply = apply(ws, "chat.post_as", {"channel": "#inc-4452", "user": "hannah.becker", "text": "ok",
                                       "thread_id": mine["posted"]["id"]}, None)
    assert reply["deliveries"][0]["kind"] == "mention"


def test_tickets_fields_workflow_comments_links_and_history(ws):
    t = later(ws)
    out, res = tickets.update(ws, ME, t, "ops-4452", {"severity": "sev1", "assignee": "sre-primary"})
    assert out["changed"] == ["severity", "assignee"] and res["fields_changed"] == ["severity", "assignee"]
    assert res["values"] == {"severity": "SEV1", "assignee": "sre-primary"}
    assert res["ticket"] == "OPS-4452"
    with pytest.raises(ToolError):
        tickets.update(ws, ME, t, "OPS-4452", {"severity": "P1"})
    with pytest.raises(ToolError):
        tickets.update(ws, ME, t, "OPS-4452", {"status": "Resolved"})
    c, res = tickets.comment(ws, ME, later(ws), "OPS-4452", "CHANGE: epctl pause all | why: EPD-RB-7 step 1")
    assert c["comment"]["visibility"] == "internal" and not res["external"]
    assert tickets.transition(ws, ME, later(ws), "OPS-4452", "mitigated")[0]["status"] == "Mitigated"
    with pytest.raises(ToolError):
        tickets.transition(ws, ME, ws.clock(), "OPS-4452", "Done")
    tickets.link(ws, ME, later(ws), "OPS-4452", "PLAT-412", "is caused by")
    issue = tickets.get(ws, ME, ws.clock(), "OPS-4452")
    assert issue["issue"]["links"][-1]["target"] == "PLAT-412" and "Resolved" in issue["transitions"]
    back = tickets.get(ws, ME, ws.clock(), "PLAT-412")["issue"]["links"]
    assert [(x["type"], x["target"]) for x in back][-1] == ("causes", "OPS-4452")
    tickets.link(ws, ME, later(ws), "SUP-88000", "OPS-4452")
    tickets.link(ws, ME, later(ws), "OPS-4452", "SUP-88000")
    pairs = [(x["type"], x["target"]) for x in tickets.get(ws, ME, ws.clock(), "OPS-4452")["issue"]["links"]]
    assert pairs.count(("relates to", "SUP-88000")) == 1
    before = {i["key"]: len(i.get("links") or []) for i in ws.store.items("tickets.issue")}
    tickets.link(ws, ME, later(ws), "OPS-4452", "vantleaf/infra#1858")
    after = {i["key"]: len(i.get("links") or []) for i in ws.store.items("tickets.issue")}
    assert {k for k in after if after[k] != before[k]} == {"OPS-4452"}
    hist = ws.store.history("tickets.issue", "OPS-4452")
    sev = next(h for h in hist if h["op"] == "update" and "severity" in h["changes"])
    assert sev["by"] == ME and sev["changes"]["severity"] == {"from": None, "to": "SEV1"}
    created, _ = tickets.create(ws, ME, later(ws), "PLAT", "Alarm on empty endpoints", "follow-up")
    assert created["created"]["key"] == "PLAT-413" and created["created"]["status"] == "Open"
    assert tickets.search(ws, ME, ws.clock(), "freshness", project="PLAT")["issues"][0]["key"] == "PLAT-412"


def test_ticket_world_ops(ws):
    r = apply(ws, "tickets.comment_as", {"key": "OPS-4452", "user": "priya.raman", "body": "@sre-primary you keep IC"},
              None)
    assert r["deliveries"][0]["kind"] == "mention"
    assert "Investigating" in tickets.get(ws, ME, ws.clock(), "OPS-4452")["transitions"]
    with pytest.raises(ToolError, match="OPS statuses are Open, Investigating, Identified"):
        tickets.transition(ws, ME, ws.clock(), "OPS-4452", "In Progress")
    apply(ws, "tickets.update_as", {"key": "OPS-4452", "user": "priya.raman", "fields": {"status": "Investigating"}},
          None)
    assert ws.store.get("tickets.issue", "OPS-4452")["status"] == "Investigating"
    sup = apply(ws, "tickets.create_as", {"project": "SUP", "user": "hana.mori@tidewaterceramics.example",
                                          "summary": "Checkout failing", "untrusted": True}, None)
    assert sup["id"].startswith("SUP-") and ws.store.get("tickets.issue", sup["id"])["status"] == "New"


def test_wiki_versions_history_restore_comment(ws):
    t = later(ws)
    out, res = wiki.update(ws, ME, t, "EPD-RB-7", "new body", comment="fix step 3")
    assert out["version"] == 7 and res["page"] == "OPS-RB-EPD-7"
    assert [v["version"] for v in wiki.history(ws, ME, ws.clock(), "OPS-RB-EPD-7")["versions"]] == [7, 6, 5]
    restored, _ = wiki.restore(ws, ME, later(ws), "OPS-RB-EPD-7", 6)
    assert restored == {"id": "OPS-RB-EPD-7", "version": 8, "restored_from": 6}
    assert wiki.get(ws, ME, ws.clock(), "OPS-RB-EPD-7")["page"]["body"] == wiki.get(
        ws, ME, ws.clock(), "OPS-RB-EPD-7", version=6)["page"]["body"]
    wiki.comment(ws, ME, later(ws), "PLAT/endpointd design", "step 3 should say last-known-good")
    assert wiki.get(ws, ME, ws.clock(), "PLAT-DOC-ENDPOINTD")["page"]["comments"][0]["author"] == ME
    created, _ = wiki.update(ws, ME, later(ws), "OPS-4452 timeline", "02:31 race", space="OPS")
    assert created["created"]["version"] == 1
    assert "PLAT-DOC-ENDPOINTD" in [p["id"] for p in wiki.search(ws, ME, ws.clock(), "last-known-good")["pages"]]
    with pytest.raises(ToolError):
        wiki.get(ws, ME, ws.clock(), "no such page at all")


def test_repo_is_read_only_views(ws):
    t = ws.clock()
    assert {r["name"] for r in repo.list_(ws, ME, t)["repos"]} >= {"vantleaf/infra", "vantleaf/platform-endpointd"}
    pr = repo.get(ws, ME, t, "platform-endpointd", pr=97)["pr"]
    assert pr["state"] == "draft" and pr["checks"] == "failing"
    assert repo.search(ws, ME, t, "freshness ONCE")["matches"][0]["path"] == "enactor/apply.go"
    log = repo.log(ws, ME, t, "checkout-api")
    assert log["deploys"][0]["revision"] == "checkout-api:412" and log["merged_prs"][0]["pr"] == 1840
    assert "terraform/ledger/aurora.tf" in repo.read(ws, ME, t, "infra", "terraform/ledger")["directory"]


def test_mail_send_reply_forward_and_external_flag(ws):
    d = apply(ws, "mail.deliver", {"id": "g-callum-0304", "from": "callum.reyes@harborpinegoods.example",
                                   "to": [BOX], "subject": "Checkout down?", "body": "Need an ETA", "untrusted": True},
              "03:04:00")
    assert d["deliveries"][0]["kind"] == "email" and d["deliveries"][0]["where"] == BOX
    found = mail.search(ws, BOX, ws.clock(), unread=True)["messages"]
    assert found[0]["id"] == "g-callum-0304" and found[0]["external_sender"]
    msg = mail.read(ws, BOX, later(ws), "g-callum-0304")
    assert msg["message"]["notice"].startswith("external sender")
    assert all(m["id"] != "g-callum-0304" for m in mail.search(ws, BOX, ws.clock(), unread=True)["messages"])
    fwd, res = mail.forward(ws, BOX, later(ws), "g-callum-0304", ["owen.gallagher@vantleaf.com", "support@vantleaf.com"],
                            "key account, please take it")
    assert res["external"] is False and fwd["sent"]["subject"] == "Fwd: Checkout down?"
    assert res["forwarded"] == "g-callum-0304"
    rep, res = mail.reply(ws, BOX, later(ws), "g-callum-0304", "we are on it")
    assert res["external"] is True and rep["sent"]["to"] == ["callum.reyes@harborpinegoods.example"]
    _, res = mail.send(ws, BOX, later(ws), ["rkim@aws-tam.example", "priya.raman@vantleaf.com"], "hi", "x")
    assert res["external"] is True and set(res["recipients"]) == {"rkim@aws-tam.example", "priya.raman@vantleaf.com"}
    with pytest.raises(ToolError):
        mail.send(ws, BOX, ws.clock(), ["not-an-address"], "s", "b")
    reply = apply(ws, "mail.deliver", {"from": "callum.reyes@harborpinegoods.example", "to": [BOX], "subject": "Re: x",
                                       "body": "RCA please", "in_reply_to": "g-callum-0304"}, None)
    assert ws.store.get("mail.message", reply["id"])["thread"] == "g-callum-0304"


def test_pager_ack_page_note_resolve(ws):
    out, res = pager.ack(ws, ME, later(ws), "pg-77052")
    assert out["page"]["status"] == "acknowledged" and out["page"]["acked_by"] == [ME]
    with pytest.raises(ToolError):
        pager.ack(ws, ME, ws.clock(), "PG-77052")
    page, res = pager.page(ws, ME, later(ws), "OPS-4452 SEV1: ledger.internal empty", schedule="platform",
                           urgency="high")
    assert page["page"]["id"] == "PG-77053" and page["page"]["assigned"] == ["kwame.asante"]
    assert res["urgency"] == "high" and res["recipients"] == ["kwame.asante"]
    with pytest.raises(ToolError):
        pager.page(ws, ME, ws.clock(), "x", schedule="platform", urgency="urgent")
    pager.note(ws, ME, later(ws), "PG-77053", "running EPD-RB-7 under IP §3.4")
    done, _ = pager.resolve(ws, ME, later(ws), "PG-77052")
    assert done["page"]["status"] == "resolved"
    assert [p["id"] for p in pager.list_(ws, ME, ws.clock(), status="triggered")["pages"]] == [
        "PG-77053", "PG-77051", "PG-77050"]


def test_pager_world_ops(ws):
    t = ws.clock()
    apply(ws, "pager.ack_as", {"page_id": "PG-77052", "user": "hannah.becker"}, ws.iso(t + timedelta(minutes=1)))
    assert ws.store.get("pager.incident", "PG-77052")["acks"][0]["user"] == "hannah.becker"
    r = apply(ws, "pager.page_as", {"from_user": "grace.liu", "to": "sre-primary", "urgency": "high",
                                    "summary": "can you look at #inc-4452?"}, None)
    assert r["deliveries"][0]["kind"] == "page" and r["id"] == "PG-77053"
    assert apply(ws, "pager.page_as", {"from_user": "incident-bot", "to": "incident-commander", "urgency": "high",
                                       "summary": "SEV1 OPS-4452"}, None)["deliveries"] == []


def test_status_page_create_update_is_external(ws):
    out, res = status.create(ws, ME, later(ws), "Checkout errors", "identified", "Many checkouts fail.", "major",
                             {"checkout": "major_outage"})
    sid = out["incident"]["id"]
    assert res["external"] is True and res["app"] == "status"
    assert ws.store.get("status.component", "checkout")["status"] == "major_outage"
    up, res = status.update(ws, ME, later(ws), sid, "Recovering.", "monitoring", {"checkout": "operational"})
    assert up["incident"]["status"] == "monitoring" and len(up["incident"]["updates"]) == 2
    assert ws.store.get("status.component", "checkout")["status"] == "operational"
    with pytest.raises(ToolError):
        status.update(ws, ME, ws.clock(), sid, "x", "fixed")
    with pytest.raises(ToolError):
        status.create(ws, ME, ws.clock(), "x", "investigating", "b", components={"ledger": "major_outage"})


def test_unknown_world_op_is_refused(ws):
    with pytest.raises(ToolError):
        apply(ws, "chat.delete_everything", {}, None)


def test_incident_process_is_the_canonical_v12_with_the_stable_figures_once(ws):
    """IP v12 is COMMON_v2 §8.2's text (D1-01, D1-05): no checklist, no §7 prompt for the handoff's facts. §5 states once
    the figures G3 holds a Mitigated or Resolved claim to, and every other graded rule reads where the design puts it."""
    body = " ".join(wiki.get(ws, ME, ws.clock(), "Incident Process 2026")["page"]["body"].split())
    for rule in ("Declare within 15 min of the ack or of the first production change", "at **high** urgency",
                 "update the incident channel at least every 30 min", "first public post within **30 min of declaration**",
                 "at least every **60 min**", "engineers never reply to merchants",
                 "Route to Owen Gallagher (key accounts) or Nadia Petrova (support)",
                 "AWS Support and the TAM need no approval", "stable >= 30 min",
                 ("Stable means checkout success >= 99 % in every minute with every endpoint correct: for at least 5 "
                  "min before Mitigated, and for 30 min before Resolved"), "CHANGE record within 15 min",
                 "`CHANGE: <what> | why: <reason> | undo: <how> | auth:", "Only records from the approvals tool count",
                 "(impact, cause, changes and their undo, open risks, owners)"):
        assert rule in body, rule
    for hint in ("rules every responder", "delayed recovery", "caching", "load that followed", "follow-up tickets",
                 "low urgency does not wake", "support@vantleaf.com", "just applied", "NOT NEEDED"):
        assert hint not in body, hint


def test_epd_rb_7_v6_is_the_design_text(ws):
    """Step 4 says how to verify and that clients keep negative answers, not when the check must come (D1-06)."""
    body = " ".join(wiki.get(ws, ME, ws.clock(), "EPD-RB-7")["page"]["body"].split())
    step4 = body.split(" 4. ")[1].split(" 5. ")[0]
    assert step4 == ("Verify from inside the VPCs with `epctl verify --resolve` or `dig` from a host in each AZ. Route 53 "
                     "TestDNSAnswer does not work for private zones. Clients may keep a negative answer for up to the "
                     "zone's SOA negative TTL.")
    assert "Steps unchanged since v5" in body


def test_peak_freeze_is_the_canonical_v3(ws):
    """PF v3 is COMMON_v2 §8.1's text (D1-05): (g) names no DNS cache flush and §6 is communications only."""
    body = " ".join(wiki.get(ws, ME, ws.clock(), "Peak Freeze 2026")["page"]["body"].split())
    g = body.split("- (g)")[1].split("- §4.2")[0]
    assert "flush" not in g and "reboot impaired hosts" in g
    assert "Class B" not in body.split("## §6")[1] and "Priya" not in body
    assert "§1.4 Not changes: read-only work incl. `--dry-run`; creating new dashboards or new alarms" in body


def test_page_as_preview_does_not_double_the_urgency_tag(ws):
    r = apply(ws, "pager.page_as", {"from_user": "alertmanager", "to": "sre-primary", "urgency": "high",
                                    "summary": "[HIGH] x"}, None)
    assert r["deliveries"][0]["preview"] == "[HIGH] x"
    assert ws.store.get("pager.incident", r["id"])["title"] == "[HIGH] x"
    r = apply(ws, "pager.page_as", {"from_user": "alertmanager", "to": "sre-primary", "urgency": "high",
                                    "summary": "y"}, None)
    assert r["deliveries"][0]["preview"] == "[HIGH] y"


def test_post_as_makes_the_poster_a_channel_member_once(ws):
    def count():
        return next(c["members"] for c in chat.list_channels(ws, ME, ws.clock())["channels"] if c["name"] == "#inc-4452")

    assert count() == 3
    apply(ws, "chat.post_as", {"channel": "#inc-4452", "user": "priya.raman", "text": "I'm on"}, None)
    apply(ws, "chat.post_as", {"channel": "#inc-4452", "user": "priya.raman", "text": "still here"}, None)
    assert ws.store.get("chat.channel", "#inc-4452")["members"].count("priya.raman") == 1 and count() == 4
    d = apply(ws, "chat.dm_as", {"from_user": "kwame.asante", "to_user": "sre-primary", "text": "up"}, None)
    assert ws.store.get("chat.channel", "dm:kwame.asante|sre-primary")["members"] == ["kwame.asante", "sre-primary"]
    assert d["ok"]


def test_mail_resolved_carries_the_sent_subject(ws):
    apply(ws, "mail.deliver", {"id": "g-x", "from": "callum.reyes@harborpinegoods.example", "to": [BOX],
                               "subject": "Checkout failing", "body": "help"}, None)
    _, res = mail.forward(ws, BOX, later(ws), "g-x", ["owen.gallagher@vantleaf.com"])
    assert res["subject"] == "Fwd: Checkout failing"
    _, res = mail.reply(ws, BOX, later(ws), "g-x", "on it")
    assert res["subject"] == "Re: Checkout failing"
    _, res = mail.send(ws, BOX, later(ws), ["owen.gallagher@vantleaf.com"], "ETA", "x")
    assert res["subject"] == "ETA"
