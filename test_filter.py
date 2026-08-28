"""Sanity check for the gradeable-item filter, using a synthetic Canvas feed
that mixes real assignments with the office-hours / live-stream clutter."""

from canvas_notion_sync import collect_assignments

SAMPLE = """BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//Instructure//Canvas//EN
BEGIN:VEVENT
UID:event-assignment-9876543@school.instructure.com
DTSTART:20260902T235900Z
SUMMARY:Problem Set 3 [MATH 2413]
URL:https://school.instructure.com/courses/11/assignments/9876543
END:VEVENT
BEGIN:VEVENT
UID:event-quiz-112233@school.instructure.com
DTSTART:20260904T170000Z
SUMMARY:Chapter 4 Quiz [BIOL 1406]
URL:https://school.instructure.com/courses/12/quizzes/112233
END:VEVENT
BEGIN:VEVENT
UID:event-discussion-topic-4455@school.instructure.com
DTSTART:20260905T045900Z
SUMMARY:Week 2 Discussion [HIST 1301]
URL:https://school.instructure.com/courses/13/discussion_topics/4455
END:VEVENT
BEGIN:VEVENT
UID:event-assignment-override-778@school.instructure.com
DTSTART:20260907T235900Z
SUMMARY:Lab Report 1 [CHEM 1411]
URL:https://school.instructure.com/courses/14/assignments/778
END:VEVENT
BEGIN:VEVENT
UID:event-calendar-event-5001@school.instructure.com
DTSTART:20260903T160000Z
SUMMARY:Office Hours [MATH 2413]
URL:https://school.instructure.com/calendar
END:VEVENT
BEGIN:VEVENT
UID:event-calendar-event-5002@school.instructure.com
DTSTART:20260903T180000Z
SUMMARY:Guest Lecture Live Stream [BIOL 1406]
URL:https://school.instructure.com/calendar
END:VEVENT
BEGIN:VEVENT
UID:event-calendar-event-5003@school.instructure.com
DTSTART;VALUE=DATE:20260907
SUMMARY:Labor Day - No Class [HIST 1301]
URL:https://school.instructure.com/calendar
END:VEVENT
BEGIN:VEVENT
UID:event-planner-note-6001@school.instructure.com
DTSTART;VALUE=DATE:20260906
SUMMARY:Remember to buy the textbook
END:VEVENT
BEGIN:VEVENT
UID:event-appointment-group-7001@school.instructure.com
DTSTART:20260908T150000Z
SUMMARY:Advising Slot [MATH 2413]
END:VEVENT
END:VCALENDAR
"""

EXPECTED_KEPT = {
    "event-assignment-9876543",
    "event-quiz-112233",
    "event-discussion-topic-4455",
    "event-assignment-override-778",
}


def test():
    items, dropped = collect_assignments(SAMPLE, "America/Chicago")
    kept = set(items)

    print(f"Kept    ({len(kept)}):")
    for k in sorted(kept, key=lambda x: items[x]["due"] or ""):
        i = items[k]
        print(f"   - {i['title']:<28} course={i['course']:<12} "
              f"type={i['type']:<11} due={i['due']}")
    print(f"Dropped ({dropped}) non-gradeable entries.\n")

    assert kept == EXPECTED_KEPT, f"unexpected result: {kept ^ EXPECTED_KEPT}"
    assert dropped == 5, f"expected 5 dropped, got {dropped}"

    # Titles must be split away from the [COURSE] suffix.
    assert items["event-assignment-9876543"]["title"] == "Problem Set 3"
    assert items["event-assignment-9876543"]["course"] == "MATH 2413"
    # Quizzes and discussions must be labeled distinctly.
    assert items["event-quiz-112233"]["type"] == "Quiz"
    assert items["event-discussion-topic-4455"]["type"] == "Discussion"
    assert items["event-assignment-override-778"]["type"] == "Assignment"
    # Timezone conversion: 23:59 UTC -> 18:59 Central.
    assert items["event-assignment-9876543"]["due"].startswith("2026-09-02T18:59")

    print("PASS - office hours, live stream, holiday, planner note and "
          "advising slot were all filtered out.")


if __name__ == "__main__":
    test()
