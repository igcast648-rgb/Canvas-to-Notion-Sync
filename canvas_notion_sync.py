#!/usr/bin/env python3
"""
Canvas -> Notion assignment sync.

Reads your Canvas Calendar Feed (.ics), keeps ONLY gradeable items
(assignments, quizzes, graded discussions), and upserts them into a Notion
database. Your Status column is never overwritten on rows that already exist,
so checking things off in Notion sticks.

No Canvas API key. No webhooks. Just the private calendar feed URL.

Environment variables (all set as GitHub Actions secrets / repo variables):

  Required:
    CANVAS_ICS_URL      Your Canvas Calendar Feed URL (webcal:// or https://)
    NOTION_TOKEN        Notion internal integration token (starts with ntn_ or secret_)
    NOTION_DATABASE_ID  The 32-char id of your assignments database

  Optional (defaults shown) - only set these if your column names differ:
    PROP_TITLE=Name
    PROP_DUE=Due Date
    PROP_COURSE=Course
    PROP_CANVAS_ID=Canvas ID
    PROP_URL=Link
    PROP_TYPE=Type
    STATUS_DEFAULT=            (blank = leave Status unset on new rows)
    TIMEZONE=America/Chicago
    DRY_RUN=false              (true = print what would happen, change nothing)
"""

import os
import re
import sys
import time
import json
from datetime import datetime, date, timezone
from zoneinfo import ZoneInfo

import requests

NOTION_VERSION = "2022-06-28"
NOTION_API = "https://api.notion.com/v1"

# ---------------------------------------------------------------------------
# The filter. This is the part that keeps office hours / live streams out.
#
# Canvas encodes the item type directly in each event's UID, e.g.
#   event-assignment-9876543@school.instructure.com        <- real assignment
#   event-quiz-112233@school.instructure.com               <- quiz
#   event-discussion-topic-4455@school.instructure.com     <- graded discussion
#   event-calendar-event-778899@school.instructure.com     <- office hours, streams, etc.
#
# We match on that structure rather than guessing from the title text, and we
# are strict: anything whose type we don't explicitly recognize as gradeable
# is dropped. No safety net, no fuzzy keyword rescue.
# ---------------------------------------------------------------------------
GRADEABLE_UID_RE = re.compile(
    r"^event-("
    r"assignment-override"
    r"|assignment"
    r"|quiz"
    r"|discussion-topic"
    r"|sub-assignment"           # checkpointed / sub-assignments
    r")-\d+",
    re.IGNORECASE,
)

TYPE_LABELS = {
    "assignment": "Assignment",
    "assignment-override": "Assignment",
    "sub-assignment": "Assignment",
    "quiz": "Quiz",
    "discussion-topic": "Discussion",
}


def env(name, default=""):
    return os.environ.get(name, default).strip()


def env_bool(name, default=False):
    v = env(name).lower()
    if not v:
        return default
    return v in ("1", "true", "yes", "on")


CFG = {
    "ics_url": env("CANVAS_ICS_URL"),
    "token": env("NOTION_TOKEN"),
    "database_id": env("NOTION_DATABASE_ID"),
    "p_title": env("PROP_TITLE", "Name"),
    "p_due": env("PROP_DUE", "Due Date"),
    "p_course": env("PROP_COURSE", "Course"),
    "p_canvas_id": env("PROP_CANVAS_ID", "Canvas ID"),
    "p_url": env("PROP_URL", "Link"),
    "p_type": env("PROP_TYPE", "Type"),
    "p_reminders": env("PROP_REMINDERS", "Reminders Sent"),
    "status_default": env("STATUS_DEFAULT", ""),
    "tz": env("TIMEZONE", "America/Chicago"),
    "dry_run": env_bool("DRY_RUN", False),
}


def die(msg):
    print(f"ERROR: {msg}", file=sys.stderr)
    sys.exit(1)


# ---------------------------------------------------------------------------
# Canvas side
# ---------------------------------------------------------------------------

def fetch_ics(url):
    if url.startswith("webcal://"):
        url = "https://" + url[len("webcal://"):]
    if not url.startswith("http"):
        die("CANVAS_ICS_URL doesn't look like a URL. Copy the link address of "
            "'Calendar Feed' at the bottom of Canvas > Account > Settings.")
    r = requests.get(url, timeout=60)
    r.raise_for_status()
    if "BEGIN:VCALENDAR" not in r.text:
        die("That URL didn't return a calendar feed. Make sure you copied the "
            "link address rather than the page you land on.")
    return r.text


def unfold(ics_text):
    """iCalendar wraps long lines, continuing them with a leading space or tab.
    Stitch those back together before anything tries to read them."""
    lines = ics_text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    out = []
    for line in lines:
        if line[:1] in (" ", "\t") and out:
            out[-1] += line[1:]
        else:
            out.append(line)
    return out


def unescape(v):
    return (v.replace("\\n", "\n").replace("\\N", "\n")
             .replace("\\,", ",").replace("\\;", ";").replace("\\\\", "\\"))


def parse_events(ics_text):
    """Yield each VEVENT as {PROPNAME: (params_dict, value)}."""
    event = None
    for line in unfold(ics_text):
        stripped = line.strip()
        if stripped == "BEGIN:VEVENT":
            event = {}
            continue
        if stripped == "END:VEVENT":
            if event is not None:
                yield event
            event = None
            continue
        if event is None or ":" not in line:
            continue

        head, _, value = line.partition(":")
        parts = head.split(";")
        name = parts[0].strip().upper()
        params = {}
        for p in parts[1:]:
            if "=" in p:
                k, _, v = p.partition("=")
                params[k.strip().upper()] = v.strip().strip('"')
        event[name] = (params, unescape(value.strip()))


def parse_ics_datetime(params, value, tzname):
    """Turn an iCalendar date/date-time into an ISO string Notion accepts."""
    if not value:
        return None
    value = value.strip()

    # All-day form: 20260907
    if params.get("VALUE") == "DATE" or re.fullmatch(r"\d{8}", value):
        try:
            return datetime.strptime(value[:8], "%Y%m%d").date().isoformat()
        except ValueError:
            return None

    m = re.fullmatch(r"(\d{8})T(\d{6})(Z?)", value)
    if not m:
        return None
    try:
        dt = datetime.strptime(m.group(1) + m.group(2), "%Y%m%d%H%M%S")
    except ValueError:
        return None

    if m.group(3) == "Z":
        dt = dt.replace(tzinfo=timezone.utc)
    elif "TZID" in params:
        try:
            dt = dt.replace(tzinfo=ZoneInfo(params["TZID"]))
        except Exception:
            dt = dt.replace(tzinfo=timezone.utc)
    else:
        dt = dt.replace(tzinfo=timezone.utc)

    try:
        dt = dt.astimezone(ZoneInfo(tzname))
    except Exception:
        dt = dt.astimezone(timezone.utc)
    return dt.isoformat()


def parse_uid_type(uid):
    """Return the canvas item type from a UID, or None if not gradeable."""
    uid = str(uid or "").strip()
    m = GRADEABLE_UID_RE.match(uid)
    if not m:
        return None
    return m.group(1).lower()


def clean_summary(summary):
    """Canvas formats titles as 'Homework 3 [MATH 101]'. Split them apart."""
    s = str(summary or "").strip()
    m = re.match(r"^(.*?)\s*\[([^\]]+)\]\s*$", s)
    if m:
        return m.group(1).strip(), m.group(2).strip()
    return s, ""


def collect_assignments(ics_text, tzname):
    items = {}
    dropped = 0
    for event in parse_events(ics_text):
        uid = event.get("UID", ({}, ""))[1]
        kind = parse_uid_type(uid)
        if kind is None:
            dropped += 1
            continue

        title, course = clean_summary(event.get("SUMMARY", ({}, ""))[1])
        if not title:
            dropped += 1
            continue

        due = None
        for field in ("DTSTART", "DTEND", "DUE"):
            if field in event:
                params, value = event[field]
                due = parse_ics_datetime(params, value, tzname)
                if due:
                    break

        url = event.get("URL", ({}, ""))[1].strip()

        # UID is the stable identity across renames and due-date changes.
        canvas_id = uid.split("@")[0]

        items[canvas_id] = {
            "canvas_id": canvas_id,
            "title": title,
            "course": course,
            "due": due,
            "url": url,
            "type": TYPE_LABELS.get(kind, "Assignment"),
        }
    return items, dropped


# ---------------------------------------------------------------------------
# Notion side
# ---------------------------------------------------------------------------

class Notion:
    def __init__(self, token):
        self.s = requests.Session()
        self.s.headers.update({
            "Authorization": f"Bearer {token}",
            "Notion-Version": NOTION_VERSION,
            "Content-Type": "application/json",
        })

    def _req(self, method, path, **kw):
        for attempt in range(5):
            r = self.s.request(method, f"{NOTION_API}{path}", timeout=60, **kw)
            if r.status_code == 429:
                wait = float(r.headers.get("Retry-After", 2))
                time.sleep(wait)
                continue
            if r.status_code >= 400:
                try:
                    detail = r.json().get("message", r.text)
                except Exception:
                    detail = r.text
                die(f"Notion API {r.status_code} on {method} {path}: {detail}")
            time.sleep(0.34)  # stay under Notion's ~3 req/sec limit
            return r.json()
        die("Notion kept rate-limiting the request; try again later.")

    def get_database(self, db_id):
        return self._req("GET", f"/databases/{db_id}")

    def all_pages(self, db_id):
        pages, cursor = [], None
        while True:
            body = {"page_size": 100}
            if cursor:
                body["start_cursor"] = cursor
            data = self._req("POST", f"/databases/{db_id}/query", data=json.dumps(body))
            pages.extend(data.get("results", []))
            if not data.get("has_more"):
                return pages
            cursor = data.get("next_cursor")

    def create_page(self, db_id, props):
        return self._req("POST", "/pages", data=json.dumps({
            "parent": {"database_id": db_id},
            "properties": props,
        }))

    def update_page(self, page_id, props):
        return self._req("PATCH", f"/pages/{page_id}",
                         data=json.dumps({"properties": props}))


def build_value(prop_type, value):
    """Shape a plain value into whatever Notion column type is actually there."""
    if value in (None, ""):
        return None
    if prop_type == "title":
        return {"title": [{"text": {"content": str(value)[:2000]}}]}
    if prop_type == "rich_text":
        return {"rich_text": [{"text": {"content": str(value)[:2000]}}]}
    if prop_type == "date":
        return {"date": {"start": value}}
    if prop_type == "url":
        return {"url": str(value)}
    if prop_type == "select":
        return {"select": {"name": str(value)[:100]}}
    if prop_type == "multi_select":
        return {"multi_select": [{"name": str(value)[:100]}]}
    if prop_type == "status":
        return {"status": {"name": str(value)[:100]}}
    if prop_type == "number":
        try:
            return {"number": float(value)}
        except (TypeError, ValueError):
            return None
    return None


def read_plain(prop):
    """Read a Notion property back out as a comparable plain value."""
    if not prop:
        return None
    t = prop.get("type")
    if t == "title":
        return "".join(x.get("plain_text", "") for x in prop.get("title", [])) or None
    if t == "rich_text":
        return "".join(x.get("plain_text", "") for x in prop.get("rich_text", [])) or None
    if t == "date":
        d = prop.get("date")
        return d.get("start") if d else None
    if t == "url":
        return prop.get("url")
    if t == "select":
        s = prop.get("select")
        return s.get("name") if s else None
    if t == "multi_select":
        vals = [x.get("name") for x in prop.get("multi_select", [])]
        return vals[0] if vals else None
    if t == "status":
        s = prop.get("status")
        return s.get("name") if s else None
    return None


def same_date(a, b):
    """Compare dates tolerantly - Notion may normalize the offset it echoes back."""
    if a == b:
        return True
    if not a or not b:
        return False
    try:
        da = datetime.fromisoformat(a)
        db = datetime.fromisoformat(b)
        if da.tzinfo and db.tzinfo:
            return da == db
        return da.replace(tzinfo=None) == db.replace(tzinfo=None)
    except ValueError:
        return False


# ---------------------------------------------------------------------------

def main():
    for key in ("ics_url", "token", "database_id"):
        if not CFG[key]:
            die(f"Missing required setting: {key.upper()}. See README.")

    db_id = CFG["database_id"].replace("-", "")

    print("Fetching Canvas calendar feed...")
    ics = fetch_ics(CFG["ics_url"])
    items, dropped = collect_assignments(ics, CFG["tz"])
    print(f"  {len(items)} gradeable item(s) found; "
          f"{dropped} non-gradeable calendar entr(ies) filtered out.")

    notion = Notion(CFG["token"])
    schema = notion.get_database(db_id).get("properties", {})

    # Resolve which of our fields actually exist in this database.
    wanted = {
        "title": CFG["p_title"],
        "due": CFG["p_due"],
        "course": CFG["p_course"],
        "canvas_id": CFG["p_canvas_id"],
        "url": CFG["p_url"],
        "type": CFG["p_type"],
    }
    resolved = {}
    for field, name in wanted.items():
        if name and name in schema:
            resolved[field] = (name, schema[name]["type"])

    # The title column is mandatory - fall back to whichever column IS the title.
    if "title" not in resolved:
        for name, meta in schema.items():
            if meta.get("type") == "title":
                resolved["title"] = (name, "title")
                break
    if "title" not in resolved:
        die("Couldn't find a title property in that Notion database.")

    if "canvas_id" not in resolved:
        die(f"Your database needs a text property named '{CFG['p_canvas_id']}' "
            "so the sync can recognize assignments it has already added. "
            "Add it in Notion (type: Text) and re-run.")

    status_prop = None
    for name, meta in schema.items():
        if meta.get("type") in ("status", "select") and name == "Status":
            status_prop = (name, meta["type"])
            break

    print("Loading existing Notion rows...")
    existing = {}
    for page in notion.all_pages(db_id):
        cid = read_plain(page.get("properties", {}).get(resolved["canvas_id"][0]))
        if cid:
            existing[cid] = page
    print(f"  {len(existing)} row(s) already tracked.")

    created = updated = unchanged = 0

    for cid, item in sorted(items.items(), key=lambda kv: kv[1]["due"] or ""):
        values = {
            "title": item["title"],
            "due": item["due"],
            "course": item["course"],
            "canvas_id": item["canvas_id"],
            "url": item["url"],
            "type": item["type"],
        }
        page = existing.get(cid)

        if page is None:
            props = {}
            for field, (name, ptype) in resolved.items():
                v = build_value(ptype, values.get(field))
                if v:
                    props[name] = v
            # Status is only ever set here, on brand new rows.
            if status_prop and CFG["status_default"]:
                v = build_value(status_prop[1], CFG["status_default"])
                if v:
                    props[status_prop[0]] = v

            if CFG["dry_run"]:
                print(f"  + would add: {item['title']}  ({item['due'] or 'no due date'})")
            else:
                notion.create_page(db_id, props)
                print(f"  + added: {item['title']}")
            created += 1
            continue

        # Existing row: refresh the Canvas-owned fields only.
        # Status is deliberately absent here so your checkmarks survive.
        changes = {}
        current = page.get("properties", {})
        for field in ("title", "due", "course", "url", "type"):
            if field not in resolved:
                continue
            name, ptype = resolved[field]
            new_val = values.get(field)
            if new_val in (None, ""):
                continue
            old_val = read_plain(current.get(name))
            if field == "due":
                if same_date(old_val, new_val):
                    continue
            elif old_val == new_val:
                continue
            shaped = build_value(ptype, new_val)
            if shaped:
                changes[name] = shaped

        # If the deadline moved, the reminders already sent no longer apply -
        # clear them so the 4/3/2/1-hour warnings re-arm against the new date.
        due_name = resolved.get("due", (None, None))[0]
        if due_name and due_name in changes:
            rem_name = CFG["p_reminders"]
            if rem_name in schema and read_plain(current.get(rem_name)):
                changes[rem_name] = {"rich_text": []}

        if not changes:
            unchanged += 1
            continue

        if CFG["dry_run"]:
            print(f"  ~ would update: {item['title']} -> {list(changes)}")
        else:
            notion.update_page(page["id"], changes)
            print(f"  ~ updated: {item['title']} ({', '.join(changes)})")
        updated += 1

    print("\nDone. "
          f"{created} added, {updated} updated, {unchanged} already current.")
    if CFG["dry_run"]:
        print("(DRY_RUN was on - nothing was actually written to Notion.)")


if __name__ == "__main__":
    main()
