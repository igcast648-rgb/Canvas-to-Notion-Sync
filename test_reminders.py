"""Checks the reminder rules against the situations that actually come up:

  1. Nothing fires while an assignment is still far out.
  2. Each tier fires once and only once as the deadline approaches.
  3. A tier that already fired never fires again.
  4. An assignment marked Done stops notifying.
  5. A past-due assignment retires quietly instead of blasting four alerts.
  6. Several tiers coming due at once collapse into ONE notification.
  7. Extending a deadline re-arms the reminders.
"""

import json
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import reminders
import canvas_notion_sync as sync

TZ = ZoneInfo("America/Chicago")
NOW = datetime(2026, 9, 15, 18, 0, tzinfo=TZ)   # 6:00 PM

sent_pushes = []
notion_writes = []


def make_page(pid, title, due, sent="", status="Not started"):
    return {
        "id": pid,
        "properties": {
            "Name": {"type": "title", "title": [{"plain_text": title}]},
            "Due Date": {"type": "date", "date": {"start": due.isoformat()}},
            "Course": {"type": "select", "select": {"name": "TEST 101"}},
            "Link": {"type": "url", "url": "https://example.test/a"},
            "Status": {"type": "status", "status": {"name": status}},
            "Reminders Sent": {"type": "rich_text",
                               "rich_text": ([{"plain_text": sent}] if sent else [])},
        },
    }


SCHEMA = {"properties": {
    "Name": {"type": "title"}, "Due Date": {"type": "date"},
    "Course": {"type": "select"}, "Link": {"type": "url"},
    "Status": {"type": "status"}, "Reminders Sent": {"type": "rich_text"},
}}


class FakeNotion:
    pages = []

    def __init__(self, token):
        pass

    def get_database(self, db_id):
        return SCHEMA

    def all_pages(self, db_id):
        return FakeNotion.pages

    def update_page(self, page_id, props):
        notion_writes.append((page_id, props))


def fake_notify(channels, heading, title, course, due_text, left,
                hours_left, link):
    sent_pushes.append({"title": heading, "message": title,
                        "channels": sorted(channels),
                        "priority": reminders.priority_for(hours_left)})
    return True


def run(pages, now=NOW):
    sent_pushes.clear()
    notion_writes.clear()
    FakeNotion.pages = pages
    reminders.Notion = FakeNotion
    reminders.send_notification = fake_notify
    reminders.CFG.update({"token": "x", "database_id": "db", "ntfy_topic": "t",
                          "channel": "auto", "dry_run": False})

    class FrozenDT(datetime):
        @classmethod
        def now(cls, tz=None):
            return now
    reminders.datetime = FrozenDT
    try:
        reminders.main()
    finally:
        reminders.datetime = datetime
    return list(sent_pushes), list(notion_writes)


def written_tiers(writes, page_id):
    for pid, props in writes:
        if pid == page_id:
            rt = props["Reminders Sent"].get("rich_text", [])
            return rt[0]["text"]["content"] if rt else ""
    return None


# ---------------------------------------------------------------------------

def test_far_out_is_silent():
    pushes, _ = run([make_page("p1", "Far Away", NOW + timedelta(hours=30))])
    assert not pushes, "should not notify 30 hours out"
    print("PASS 1 - silent while the deadline is far off")


def test_tiers_fire_in_sequence():
    # 3h50m left -> only the 4h tier should fire.
    pushes, writes = run([make_page("p2", "Essay", NOW + timedelta(hours=3, minutes=50))])
    assert len(pushes) == 1, f"expected one push, got {len(pushes)}"
    assert pushes[0]["title"] == "Due in 3h 50m"
    assert written_tiers(writes, "p2") == "4"

    # Next hour: 2h50m left, 4h already sent -> only the 3h tier fires.
    pushes, writes = run([make_page("p2", "Essay",
                                    NOW + timedelta(hours=2, minutes=50), sent="4")])
    assert len(pushes) == 1
    assert written_tiers(writes, "p2") == "4,3"

    # 50 minutes left, 4/3/2 already sent -> the final 1h tier.
    pushes, writes = run([make_page("p2", "Essay",
                                    NOW + timedelta(minutes=50), sent="4,3,2")])
    assert len(pushes) == 1
    assert pushes[0]["priority"] == 5, "the 1-hour warning should be max priority"
    assert written_tiers(writes, "p2") == "4,3,2,1"
    print("PASS 2 - each tier fires once, in order, escalating priority")


def test_no_repeats():
    pushes, writes = run([make_page("p3", "Lab", NOW + timedelta(hours=3, minutes=30),
                                    sent="4,3")])
    assert not pushes, "3h tier was already sent; must not repeat"
    print("PASS 3 - an already-sent tier never fires again")


def test_done_is_skipped():
    pushes, writes = run([make_page("p4", "Finished Early",
                                    NOW + timedelta(minutes=30), status="Done")])
    assert not pushes, "assignments marked Done must not notify"
    assert written_tiers(writes, "p4") == "4,3,2,1", "should retire quietly"
    print("PASS 4 - checked-off assignments stop notifying")


def test_past_due_retires_quietly():
    pushes, writes = run([make_page("p5", "Missed It", NOW - timedelta(hours=2))])
    assert not pushes, "must not fire a burst of alerts after the deadline"
    assert written_tiers(writes, "p5") == "4,3,2,1"
    print("PASS 5 - past-deadline items retire without a late burst")


def test_multiple_tiers_collapse():
    # Appears with only 45 minutes left: all four tiers are due at once.
    pushes, writes = run([make_page("p6", "Surprise Quiz", NOW + timedelta(minutes=45))])
    assert len(pushes) == 1, f"four tiers at once must collapse to ONE push, got {len(pushes)}"
    assert pushes[0]["title"] == "Due in 45m", pushes[0]["title"]
    assert written_tiers(writes, "p6") == "4,3,2,1"
    print("PASS 6 - simultaneous tiers collapse into a single notification")


def test_extension_rearms_reminders():
    """The sync should clear Reminders Sent when Canvas moves a due date."""
    feed = (
        "BEGIN:VCALENDAR\nBEGIN:VEVENT\n"
        "UID:event-assignment-900@school.instructure.com\n"
        "DTSTART:20260930T235900Z\n"
        "SUMMARY:Extended Paper [TEST 101]\n"
        "END:VEVENT\nEND:VCALENDAR\n"
    )
    page = make_page("p7", "Extended Paper", NOW, sent="4,3,2,1")
    page["properties"]["Canvas ID"] = {
        "type": "rich_text", "rich_text": [{"plain_text": "event-assignment-900"}]}

    schema = {"properties": dict(SCHEMA["properties"],
                                 **{"Canvas ID": {"type": "rich_text"}})}
    captured = []

    class N:
        def __init__(self, token): pass
        def get_database(self, db): return schema
        def all_pages(self, db): return [page]
        def create_page(self, db, props): captured.append(("create", props))
        def update_page(self, pid, props): captured.append(("update", props))

    sync.Notion = N
    sync.fetch_ics = lambda url: feed
    sync.CFG.update({"ics_url": "u", "token": "t", "database_id": "db",
                     "dry_run": False, "status_default": ""})
    sync.main()

    assert captured, "the moved due date should have produced an update"
    _, props = captured[0]
    assert "Due Date" in props
    assert props.get("Reminders Sent") == {"rich_text": []}, (
        "extending a deadline must clear Reminders Sent so the warnings re-arm"
    )
    print("PASS 7 - extending a deadline re-arms the reminders")


def test_channel_selection():
    """Which channel(s) get used, based on what's configured."""
    base = {"smtp_user": "", "smtp_password": "", "ntfy_topic": "",
            "channel": "auto"}

    def chans(**over):
        reminders.CFG.update({**base, **over})
        return reminders.active_channels()

    assert chans(smtp_user="a@b.com", smtp_password="pw") == {"email"}
    assert chans(ntfy_topic="t") == {"ntfy"}
    assert chans(smtp_user="a@b.com", smtp_password="pw",
                 ntfy_topic="t") == {"email", "ntfy"}
    # An explicit choice wins over auto-detection.
    assert chans(smtp_user="a@b.com", smtp_password="pw", ntfy_topic="t",
                 channel="email") == {"email"}
    assert chans(smtp_user="a@b.com", smtp_password="pw", ntfy_topic="t",
                 channel="ntfy") == {"ntfy"}
    print("PASS 8 - channel selection resolves correctly")


def test_email_is_built_correctly():
    """The email itself: subject, recipient, and both text and HTML parts."""
    captured = {}

    class FakeSMTP:
        def send_message(self, msg):
            captured["msg"] = msg

    reminders.CFG.update({
        "smtp_user": "isaiah@example.com", "smtp_password": "pw",
        "email_to": "", "email_from": "",
    })
    mailer = reminders.Mailer()
    mailer.conn = FakeSMTP()

    ok = mailer.send(subject="Due in 2h 15m - Problem Set 3",
                     title="Problem Set 3", course="MATH 2413",
                     due_text="Tue Sep 15, 8:15 PM", left="2h 15m",
                     link="https://example.test/courses/1/assignments/2")
    assert ok
    msg = captured["msg"]

    assert msg["Subject"] == "Due in 2h 15m - Problem Set 3"
    # EMAIL_TO defaults to the sending mailbox.
    assert msg["To"] == "isaiah@example.com"

    text = msg.get_body(preferencelist=("plain",)).get_content()
    html = msg.get_body(preferencelist=("html",)).get_content()
    for part, label in ((text, "text"), (html, "html")):
        assert "Problem Set 3" in part, f"{label} part missing title"
        assert "MATH 2413" in part, f"{label} part missing course"
        assert "example.test" in part, f"{label} part missing Canvas link"
    assert "2h 15m" in text and "2h 15m" in html
    print("PASS 9 - email carries subject, both body parts, and the link")


def test_failed_send_leaves_tier_unsent():
    """If delivery fails, the tier must NOT be marked sent, so the next
    hourly run retries instead of the reminder vanishing."""
    FakeNotion.pages = [make_page("p8", "Flaky", NOW + timedelta(hours=3, minutes=30))]
    reminders.Notion = FakeNotion
    reminders.send_notification = lambda *a, **k: False
    reminders.CFG.update({"token": "x", "database_id": "db", "ntfy_topic": "t",
                          "channel": "auto", "dry_run": False})
    notion_writes.clear()

    class FrozenDT(datetime):
        @classmethod
        def now(cls, tz=None):
            return NOW
    reminders.datetime = FrozenDT
    try:
        reminders.main()
    finally:
        reminders.datetime = datetime

    assert written_tiers(notion_writes, "p8") is None, (
        "a failed send must not mark the tier as sent"
    )
    print("PASS 10 - a failed send is retried next run, not lost")


if __name__ == "__main__":
    test_far_out_is_silent()
    test_tiers_fire_in_sequence()
    test_no_repeats()
    test_done_is_skipped()
    test_past_due_retires_quietly()
    test_multiple_tiers_collapse()
    test_extension_rearms_reminders()
    test_channel_selection()
    test_email_is_built_correctly()
    test_failed_send_leaves_tier_unsent()
    print("\nAll reminder checks passed.")
