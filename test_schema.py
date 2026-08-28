"""Covers the three schema-specific behaviors:

  1. All-day Canvas items get the real 11:59 PM deadline, not a bare date.
  2. Assessment Type maps to existing options - and "Final Project" is NOT
     mistaken for a Final exam.
  3. The Course relation resolves Canvas course names to Courses pages.
"""

import canvas_notion_sync as sync


# --- 1. All-day due times ----------------------------------------------------

def test_all_day_gets_deadline_time():
    ics = (
        "BEGIN:VCALENDAR\nBEGIN:VEVENT\n"
        "UID:event-assignment-311@school.instructure.com\n"
        "DTSTART;VALUE=DATE:20260830\n"
        "SUMMARY:Welcome to 320E Survey [CH 320E]\n"
        "END:VEVENT\nEND:VCALENDAR\n"
    )
    items, _, undated = sync.collect_assignments(ics, "America/Chicago")
    due = items["event-assignment-311"]["due"]
    assert due.startswith("2026-08-30T23:59"), f"got {due}"
    assert undated == 0
    print(f"PASS 1 - all-day item became {due} (was showing as a bare date)")


def test_missing_date_is_counted():
    ics = (
        "BEGIN:VCALENDAR\nBEGIN:VEVENT\n"
        "UID:event-assignment-312@school.instructure.com\n"
        "SUMMARY:Undated Reading [HIST 1301]\n"
        "END:VEVENT\nEND:VCALENDAR\n"
    )
    items, _, undated = sync.collect_assignments(ics, "America/Chicago")
    assert items["event-assignment-312"]["due"] is None
    assert undated == 1, "an item with no due date should be reported, not hidden"
    print("PASS 2 - undated items sync with a blank date and are reported")


# --- 2. Assessment Type ------------------------------------------------------

def test_type_inference():
    tmap = {"assignment": "Assignment", "quiz": "Quiz",
            "discussion-topic": "Assignment"}
    cases = [
        # (title, canvas kind, expected Notion option)
        ("Homework 7", "assignment", "Assignment"),
        ("Quiz 20: Chapter 20", "quiz", "Quiz"),
        ("Week 2 Discussion", "discussion-topic", "Assignment"),
        ("Examen #2", "assignment", "Exam"),
        ("Midterm Exam", "assignment", "Exam"),
        ("Exam 3", "quiz", "Exam"),
        ("Final Exam", "quiz", "Final"),
        # The traps: these are projects, not exams.
        ("Final Project: Part 2", "assignment", "Assignment"),
        ("Final Project: Parts 1 & 3", "assignment", "Assignment"),
        ("Final Project Video and Peer Review", "assignment", "Assignment"),
        ("Final Project Slideshow", "assignment", "Assignment"),
    ]
    for title, kind, expected in cases:
        got = sync.infer_type(title, kind, tmap)
        assert got == expected, f"{title!r} -> {got}, expected {expected}"
    print(f"PASS 3 - all {len(cases)} type cases correct, "
          "including 4 'Final Project' traps")


# --- 3. Course relation ------------------------------------------------------

COURSE_PAGES = [
    {"id": "pg-orgo", "properties": {"Name": {"type": "title",
     "title": [{"plain_text": "Organic Chemistry"}]}}},
    {"id": "pg-stat", "properties": {"Name": {"type": "title",
     "title": [{"plain_text": "Statistics"}]}}},
    {"id": "pg-gene", "properties": {"Name": {"type": "title",
     "title": [{"plain_text": "Genetics"}]}}},
]


class FakeNotion:
    def all_pages(self, db_id):
        return COURSE_PAGES


def test_course_resolution():
    r = sync.CourseResolver(FakeNotion(), "db", {})

    assert r.resolve("Statistics") == "pg-stat", "exact match"
    assert r.resolve("statistics") == "pg-stat", "case-insensitive"
    assert r.resolve("Genetics 301") == "pg-gene", "containment match"
    # A Canvas course code shares nothing with the Notion title, so it can't
    # be guessed - it must be reported rather than mislinked.
    assert r.resolve("CH 320E") is None
    assert "CH 320E" in r.unmatched
    print("PASS 4 - exact, case-insensitive and partial course names resolve; "
          "unmatchable ones are reported, not guessed")


def test_course_aliases():
    r = sync.CourseResolver(FakeNotion(), "db",
                            {"CH 320E": "Organic Chemistry",
                             "M 358K": "Statistics"})
    assert r.resolve("CH 320E") == "pg-orgo"
    assert r.resolve("M 358K") == "pg-stat"
    assert not r.unmatched, "aliased names shouldn't be reported as unmatched"
    print("PASS 5 - COURSE_ALIASES links course codes to their Notion pages")


def test_relation_value_shape():
    v = sync.build_value("relation", ["pg-orgo"])
    assert v == {"relation": [{"id": "pg-orgo"}]}, v
    assert sync.read_plain({"type": "relation",
                            "relation": [{"id": "pg-orgo"}]}) == ["pg-orgo"]
    print("PASS 6 - relation values are written and read back correctly")


if __name__ == "__main__":
    test_all_day_gets_deadline_time()
    test_missing_date_is_counted()
    test_type_inference()
    test_course_resolution()
    test_course_aliases()
    test_relation_value_shape()
    print("\nAll schema checks passed.")
