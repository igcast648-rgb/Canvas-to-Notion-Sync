#!/usr/bin/env python3
"""
Deadline reminders for the Canvas -> Notion sync.

Runs hourly. For each assignment in your Notion database, checks whether it has
crossed the 4h / 3h / 2h / 1h marks before its due date, and sends a phone push
notification the first time each mark is crossed.

Which reminders have already gone out is recorded in a Notion text property
(default: "Reminders Sent"), so nothing is ever sent twice - even though each
GitHub Actions run starts with no memory of the last one.

Anything you've marked Done in Notion is skipped.

Reminders can go out by email, by phone push (ntfy), or both.

Environment variables:

  Required:
    NOTION_TOKEN        Same token the sync uses
    NOTION_DATABASE_ID  Same database id the sync uses

  Choose at least one delivery channel:

    Email (SMTP):
      SMTP_USER         The mailbox you send from, e.g. you@gmail.com
      SMTP_PASSWORD     App password - NOT your normal account password
      EMAIL_TO=         Where reminders go (defaults to SMTP_USER)
      SMTP_HOST=smtp.gmail.com
      SMTP_PORT=587     587 = STARTTLS, 465 = implicit TLS
      EMAIL_FROM=       Defaults to SMTP_USER

    Phone push (ntfy):
      NTFY_TOPIC        Your private ntfy topic name
      NTFY_SERVER=https://ntfy.sh
      NTFY_TOKEN=       (only for protected/self-hosted topics)

  Optional (defaults shown):
    REMINDER_CHANNEL=auto        auto | email | ntfy | "email,ntfy"
    REMINDER_TIERS=4,3,2,1       Hours before the deadline to notify
    PROP_REMINDERS=Reminders Sent
    PROP_STATUS=Status
    DONE_STATUSES=Done,Complete,Completed,Submitted,Turned in
    PROP_TITLE / PROP_DUE / PROP_COURSE / PROP_URL   (same as the sync)
    TIMEZONE=America/Chicago
    DRY_RUN=false                true = log what would be sent, send nothing
"""

import os
import sys
import json
import smtplib
import ssl
from email.message import EmailMessage
from email.utils import formataddr
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import requests

from canvas_notion_sync import (
    Notion, env, env_bool, read_plain, build_value, die,
)


CFG = {
    "token": env("NOTION_TOKEN"),
    "database_id": env("NOTION_DATABASE_ID"),
    "ntfy_server": env("NTFY_SERVER", "https://ntfy.sh").rstrip("/"),
    "ntfy_topic": env("NTFY_TOPIC"),
    "ntfy_token": env("NTFY_TOKEN"),
    "smtp_host": env("SMTP_HOST", "smtp.gmail.com"),
    "smtp_port": int(env("SMTP_PORT", "587") or 587),
    "smtp_user": env("SMTP_USER"),
    "smtp_password": env("SMTP_PASSWORD"),
    "email_to": env("EMAIL_TO"),
    "email_from": env("EMAIL_FROM"),
    "channel": env("REMINDER_CHANNEL", "auto").lower(),
    "p_title": env("PROP_TITLE", "Name"),
    "p_due": env("PROP_DUE", "Due Date"),
    "p_course": env("PROP_COURSE", "Course"),
    "p_url": env("PROP_URL", "Link"),
    "p_reminders": env("PROP_REMINDERS", "Reminders Sent"),
    "p_status": env("PROP_STATUS", "Status"),
    "tz": env("TIMEZONE", "America/Chicago"),
    "dry_run": env_bool("DRY_RUN", False),
}

TIERS = [
    int(x) for x in env("REMINDER_TIERS", "4,3,2,1").replace(" ", "").split(",")
    if x.strip().isdigit()
] or [4, 3, 2, 1]

DONE_STATUSES = {
    s.strip().lower()
    for s in env("DONE_STATUSES", "Done,Complete,Completed,Submitted,Turned in").split(",")
    if s.strip()
}


def parse_due(value, tzname):
    """Notion hands back either '2026-09-07' or a full ISO timestamp."""
    if not value:
        return None
    try:
        tz = ZoneInfo(tzname)
    except Exception:
        tz = timezone.utc

    if len(value) == 10:  # date only - treat the deadline as end of that day
        try:
            d = datetime.strptime(value, "%Y-%m-%d")
        except ValueError:
            return None
        return d.replace(hour=23, minute=59, second=0, tzinfo=tz)

    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=tz)
    return dt


def parse_sent(value):
    if not value:
        return set()
    return {int(x) for x in str(value).replace(" ", "").split(",") if x.strip().isdigit()}


def format_sent(tiers):
    return ",".join(str(t) for t in sorted(tiers, reverse=True))


def human_delta(delta):
    total = int(delta.total_seconds())
    if total < 0:
        return "overdue"
    hours, remainder = divmod(total, 3600)
    minutes = remainder // 60
    if hours and minutes:
        return f"{hours}h {minutes}m"
    if hours:
        return f"{hours}h"
    return f"{minutes}m"


def priority_for(hours_left):
    if hours_left <= 1:
        return 5   # max - breaks through Do Not Disturb on most setups
    if hours_left <= 2:
        return 4   # high
    return 3       # default


def active_channels():
    """Work out where reminders should go, from config or from what's set up."""
    have_email = bool(CFG["smtp_user"] and CFG["smtp_password"])
    have_ntfy = bool(CFG["ntfy_topic"])

    if CFG["channel"] in ("", "auto"):
        chosen = set()
        if have_email:
            chosen.add("email")
        if have_ntfy:
            chosen.add("ntfy")
        return chosen

    chosen = {c.strip() for c in CFG["channel"].split(",") if c.strip()}
    unknown = chosen - {"email", "ntfy"}
    if unknown:
        die(f"Unknown REMINDER_CHANNEL value(s): {', '.join(sorted(unknown))}. "
            "Use 'email', 'ntfy', 'email,ntfy', or 'auto'.")
    if "email" in chosen and not have_email:
        die("REMINDER_CHANNEL includes email, but SMTP_USER/SMTP_PASSWORD "
            "aren't set. See README.")
    if "ntfy" in chosen and not have_ntfy:
        die("REMINDER_CHANNEL includes ntfy, but NTFY_TOPIC isn't set. "
            "See README.")
    return chosen


def send_ntfy(title, message, priority, tags, click_url):
    payload = {
        "topic": CFG["ntfy_topic"],
        "title": title,
        "message": message,
        "priority": priority,
        "tags": tags,
    }
    if click_url:
        payload["click"] = click_url

    headers = {"Content-Type": "application/json"}
    if CFG["ntfy_token"]:
        headers["Authorization"] = f"Bearer {CFG['ntfy_token']}"

    r = requests.post(CFG["ntfy_server"], data=json.dumps(payload),
                      headers=headers, timeout=30)
    if r.status_code >= 400:
        print(f"    ! push failed ({r.status_code}): {r.text[:200]}",
              file=sys.stderr)
        return False
    return True


class Mailer:
    """One SMTP connection reused for every reminder in a run, rather than
    reconnecting per message (which providers treat as suspicious)."""

    def __init__(self):
        self.conn = None

    def _connect(self):
        if self.conn is not None:
            return self.conn
        host, port = CFG["smtp_host"], CFG["smtp_port"]
        context = ssl.create_default_context()
        try:
            if port == 465:
                conn = smtplib.SMTP_SSL(host, port, timeout=30, context=context)
            else:
                conn = smtplib.SMTP(host, port, timeout=30)
                conn.ehlo()
                conn.starttls(context=context)
                conn.ehlo()
            conn.login(CFG["smtp_user"], CFG["smtp_password"])
        except smtplib.SMTPAuthenticationError as e:
            die("SMTP login was rejected. For Gmail this almost always means "
                "you used your normal password instead of a 16-character app "
                "password, or 2-Step Verification isn't enabled on the "
                f"account. ({e.smtp_code})")
        except Exception as e:
            die(f"Couldn't connect to {host}:{port} - {e}")
        self.conn = conn
        return conn

    def send(self, subject, title, course, due_text, left, link):
        to_addr = CFG["email_to"] or CFG["smtp_user"]
        from_addr = CFG["email_from"] or CFG["smtp_user"]

        msg = EmailMessage()
        msg["Subject"] = subject
        msg["From"] = formataddr(("Canvas Reminders", from_addr))
        msg["To"] = to_addr

        lines = [title]
        if course:
            lines.append(course)
        lines.append("")
        lines.append(f"Due {due_text}")
        lines.append(f"That's {left} from now.")
        if link:
            lines.append("")
            lines.append(f"Open in Canvas: {link}")
        msg.set_content("\n".join(lines))

        safe_link = (link or "").replace('"', "%22")
        link_html = (f'<p><a href="{safe_link}">Open in Canvas</a></p>'
                     if link else "")
        course_html = (f'<p style="margin:0;color:#666">{course}</p>'
                       if course else "")
        msg.add_alternative(
            f"""<html><body style="font-family:-apple-system,Segoe UI,sans-serif">
<h2 style="margin:0 0 4px">{title}</h2>
{course_html}
<p style="font-size:18px"><strong>Due in {left}</strong></p>
<p style="color:#666">{due_text}</p>
{link_html}
</body></html>""", subtype="html")

        try:
            self._connect().send_message(msg)
            return True
        except Exception as e:
            print(f"    ! email failed: {e}", file=sys.stderr)
            # Drop the connection so the next message reconnects cleanly.
            self.close()
            return False

    def close(self):
        if self.conn is not None:
            try:
                self.conn.quit()
            except Exception:
                pass
            self.conn = None


MAILER = Mailer()


def send_notification(channels, heading, title, course, due_text, left,
                      hours_left, link):
    """Deliver one reminder to every configured channel.

    Returns True if at least one channel accepted it. A tier is only marked
    sent when this returns True, so a transient outage means the next hourly
    run retries rather than silently swallowing the reminder.
    """
    results = []

    if "email" in channels:
        results.append(MAILER.send(
            subject=f"{heading} - {title}",
            title=title, course=course, due_text=due_text,
            left=left, link=link,
        ))

    if "ntfy" in channels:
        body = title if not course else f"{title}\n{course}"
        body += f"\nDue {due_text}"
        tags = "rotating_light" if hours_left <= 1 else "alarm_clock"
        results.append(send_ntfy(heading, body, priority_for(hours_left),
                                 tags, link))

    return any(results)


def main():
    for key in ("token", "database_id"):
        if not CFG[key]:
            die(f"Missing required setting: {key.upper()}. See README.")

    channels = active_channels()
    if not channels:
        die("No delivery channel configured. Set SMTP_USER + SMTP_PASSWORD "
            "for email, and/or NTFY_TOPIC for phone push. See README.")

    db_id = CFG["database_id"].replace("-", "")
    notion = Notion(CFG["token"])
    schema = notion.get_database(db_id).get("properties", {})

    if CFG["p_reminders"] not in schema:
        die(f"Your database needs a text property named '{CFG['p_reminders']}' "
            "so reminders aren't sent twice. Add it in Notion (type: Text) "
            "and re-run.")
    reminders_type = schema[CFG["p_reminders"]]["type"]
    if reminders_type not in ("rich_text", "select"):
        die(f"'{CFG['p_reminders']}' must be a Text property, not {reminders_type}.")

    try:
        tz = ZoneInfo(CFG["tz"])
    except Exception:
        tz = timezone.utc
    now = datetime.now(tz)

    print(f"Checking reminders at {now:%Y-%m-%d %H:%M %Z} "
          f"(tiers: {', '.join(str(t) + 'h' for t in TIERS)}; "
          f"via {', '.join(sorted(channels))})")

    pages = notion.all_pages(db_id)
    sent_count = 0
    retired = 0

    for page in pages:
        props = page.get("properties", {})

        due_raw = read_plain(props.get(CFG["p_due"]))
        due = parse_due(due_raw, CFG["tz"])
        if not due:
            continue

        already = parse_sent(read_plain(props.get(CFG["p_reminders"])))
        if already >= set(TIERS):
            continue

        title = read_plain(props.get(CFG["p_title"])) or "Untitled assignment"
        course = read_plain(props.get(CFG["p_course"])) or ""
        link = read_plain(props.get(CFG["p_url"])) or ""
        status = (read_plain(props.get(CFG["p_status"])) or "").strip().lower()

        # Finished early? Stop pestering, and remember that we're done with it.
        if status in DONE_STATUSES:
            if already != set(TIERS) and not CFG["dry_run"]:
                notion.update_page(page["id"], {
                    CFG["p_reminders"]: build_value(reminders_type, format_sent(TIERS))
                })
            continue

        remaining = due - now
        hours_left = remaining.total_seconds() / 3600.0

        # Past the deadline: retire the remaining tiers quietly rather than
        # firing a burst of late notifications.
        if hours_left <= 0:
            if not CFG["dry_run"]:
                notion.update_page(page["id"], {
                    CFG["p_reminders"]: build_value(reminders_type, format_sent(TIERS))
                })
            retired += 1
            continue

        crossed = sorted([t for t in TIERS if hours_left <= t and t not in already],
                         reverse=True)
        if not crossed:
            continue

        # Several tiers can come due at once - a run gets delayed, or an
        # assignment appears with the deadline already close. Send one
        # notification stating the real time left, not a burst of four.
        left = human_delta(remaining)
        heading = f"Due in {left}"
        due_text = f"{due:%a %b %-d, %-I:%M %p}"

        if CFG["dry_run"]:
            print(f"  → would notify via {', '.join(sorted(channels))}: "
                  f"[{heading}] {title} (closing tiers {crossed})")
            ok = True
        else:
            ok = send_notification(channels, heading, title, course, due_text,
                                   left, hours_left, link)
            if ok:
                print(f"  → sent: [{heading}] {title}")

        if not ok:
            # Leave the tier unmarked so the next hourly run tries again.
            continue

        sent_count += 1
        if not CFG["dry_run"]:
            notion.update_page(page["id"], {
                CFG["p_reminders"]: build_value(
                    reminders_type, format_sent(already | set(crossed))
                )
            })

    MAILER.close()

    print(f"\nDone. {sent_count} reminder(s) sent; "
          f"{retired} past-deadline item(s) retired; "
          f"{len(pages)} row(s) checked.")
    if CFG["dry_run"]:
        print("(DRY_RUN was on - no notifications were sent.)")


if __name__ == "__main__":
    main()
