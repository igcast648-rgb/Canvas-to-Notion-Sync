"""Verifies the two behaviors that matter most for day-to-day use:

  1. An assignment you've already checked off in Notion keeps its Status
     even when Canvas changes the due date.
  2. Long folded lines in the .ics feed are stitched back together, so
     assignments with long titles don't get mangled or dropped.
"""

import json
import canvas_notion_sync as sync

# --- 1. Status preservation --------------------------------------------------

FEED = """BEGIN:VCALENDAR
BEGIN:VEVENT
UID:event-assignment-555@school.instructure.com
DTSTART:20261001T235900Z
SUMMARY:Essay Draft [ENGL 1301]
URL:https://school.instructure.com/courses/9/assignments/555
END:VEVENT
END:VCALENDAR
"""

# Canvas pushed the deadline back a week; the student already marked it Done.
EXISTING_PAGE = {
    "id": "page-abc",
    "properties": {
        "Name": {"type": "title",
                 "title": [{"plain_text": "Essay Draft"}]},
        "Due Date": {"type": "date", "date": {"start": "2026-09-24T18:59:00-05:00"}},
        "Course": {"type": "select", "select": {"name": "ENGL 1301"}},
        "Canvas ID": {"type": "rich_text",
                      "rich_text": [{"plain_text": "event-assignment-555"}]},
        "Status": {"type": "status", "status": {"name": "Done"}},
    },
}

SCHEMA = {
    "properties": {
        "Name": {"type": "title"},
        "Due Date": {"type": "date"},
        "Course": {"type": "select"},
        "Canvas ID": {"type": "rich_text"},
        "Link": {"type": "url"},
        "Status": {"type": "status"},
    }
}

captured = {"updates": [], "creates": []}


class FakeNotion:
    def __init__(self, token):
        pass

    def get_database(self, db_id):
        return SCHEMA

    def all_pages(self, db_id):
        return [EXISTING_PAGE]

    def create_page(self, db_id, props):
        captured["creates"].append(props)

    def update_page(self, page_id, props):
        captured["updates"].append((page_id, props))


def test_status_preserved(monkeypatched_env):
    sync.Notion = FakeNotion
    sync.fetch_ics = lambda url: FEED
    sync.CFG.update({
        "ics_url": "https://example.test/feed.ics",
        "token": "fake",
        "database_id": "db123",
        "status_default": "Not started",
        "dry_run": False,
    })
    sync.main()

    assert not captured["creates"], "should have matched the existing row, not added one"
    assert len(captured["updates"]) == 1, "the due-date change should produce one update"

    page_id, props = captured["updates"][0]
    assert page_id == "page-abc"
    assert "Status" not in props, (
        "REGRESSION: the sync tried to write Status on an existing row - "
        "that would wipe out a checked-off assignment"
    )
    assert "Due Date" in props, "the new due date should have been written"
    assert props["Due Date"]["date"]["start"].startswith("2026-10-01T18:59")
    print("PASS - due date updated, Status left untouched:", json.dumps(props))


# --- 2. Line folding ---------------------------------------------------------

FOLDED = (
    "BEGIN:VCALENDAR\n"
    "BEGIN:VEVENT\n"
    "UID:event-assignment-777@school.instructure.com\n"
    "DTSTART:20261015T235900Z\n"
    "SUMMARY:Comparative Analysis of Renaissance and Baroque Compositio\n"
    " nal Techniques in Sacred Choral Music [MUSI 3301]\n"
    "END:VEVENT\n"
    "END:VCALENDAR\n"
)


def test_folding():
    items, dropped, _ = sync.collect_assignments(FOLDED, "America/Chicago")
    assert len(items) == 1 and dropped == 0
    item = items["event-assignment-777"]
    assert item["title"] == (
        "Comparative Analysis of Renaissance and Baroque "
        "Compositional Techniques in Sacred Choral Music"
    ), f"folded title came through wrong: {item['title']!r}"
    assert item["course"] == "MUSI 3301"
    print("PASS - folded long title reassembled correctly.")


if __name__ == "__main__":
    test_folding()
    test_status_preserved(None)
    print("\nAll upsert checks passed.")
