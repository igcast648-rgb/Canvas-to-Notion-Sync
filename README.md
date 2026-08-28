# Canvas → Notion assignment sync + deadline reminders

Automatically pulls your Canvas assignments into a Notion database, and emails
you 4, 3, 2, and 1 hour before each one is due. Runs on GitHub's
servers for free. **No Canvas API key, no webhooks, and nothing that depends on
Claude** — once it's set up, it runs on its own.

- New assignments appear in Notion automatically.
- If a due date changes in Canvas, it updates in Notion.
- **Your Status column is never overwritten**, so anything you check off stays
  checked off.
- Office hours, live streams, holidays, advising slots and other calendar
  clutter are filtered out before they ever reach Notion.
- Email reminders at the 4h / 3h / 2h / 1h marks. Anything you've marked done
  stops notifying. (Phone push via ntfy is supported too — see below.)

---

## How it works (the short version)

Canvas gives every student a private **Calendar Feed** — a read-only `.ics`
link, the same kind you'd subscribe to from a phone calendar. This script reads
that feed, throws away everything that isn't a gradeable item, and writes the
rest into your Notion database. A GitHub Actions schedule runs it hourly; each
run syncs Canvas and then checks whether anything has crossed a reminder mark.

### Why the filter is reliable

Canvas stamps the item type into each calendar entry's ID, so the script sorts
by structure rather than guessing from titles:

| Canvas entry ID looks like | What it is | Kept? |
|---|---|---|
| `event-assignment-9876543` | Assignment | ✅ |
| `event-assignment-override-778` | Assignment w/ extended deadline | ✅ |
| `event-quiz-112233` | Quiz | ✅ |
| `event-discussion-topic-4455` | Graded discussion | ✅ |
| `event-calendar-event-5001` | Office hours, live streams, holidays | ❌ |
| `event-planner-note-6001` | Personal to-do note | ❌ |
| `event-appointment-group-7001` | Advising / scheduler slot | ❌ |

The filter is strict by design: if an item's type isn't on the gradeable list,
it's dropped. An instructor who posts a real deadline as a plain calendar event
rather than a proper assignment will **not** show up in Notion.

---

## Setup

Roughly 15 minutes, once.

### Step 1 — Prepare your Notion database

Open your existing assignments database and make sure it has these columns.
Names must match exactly, or you'll override them in Step 5.

| Column | Type | Required | Notes |
|---|---|---|---|
| `Name` | Title | ✅ | The assignment title |
| `Canvas ID` | **Text** | ✅ | **Add this if you don't have it.** How the sync recognizes assignments it already added, so nothing duplicates. You can hide it from your views. |
| `Due Date` | Date | optional | |
| `Course` | Select or Text | optional | |
| `Link` | URL | optional | Direct link to the assignment |
| `Type` | Select or Text | optional | Assignment / Quiz / Discussion |
| `Status` | Status or Select | optional | Yours to control — the sync only sets it on brand-new rows |
| `Reminders Sent` | **Text** | ✅ for reminders | **Add this too.** Records which of the 4/3/2/1-hour alerts have already gone out. Hide it from your views. |

> **`Canvas ID` and `Reminders Sent` are the two non-negotiable pieces.** Both
> are plain Text properties, and both exist because GitHub Actions starts each
> run with no memory of the last one — these columns *are* the memory. Without
> `Canvas ID` you'd get duplicate rows every run; without `Reminders Sent`
> you'd get the same reminder over and over. Add both, then hide them from
> your views.

### Step 2 — Create a Notion integration

1. Go to **https://www.notion.so/my-integrations**
2. Click **New integration**. Name it something like `Canvas Sync`.
3. Set the type to **Internal** and pick your workspace.
4. Under Capabilities, make sure it can **Read**, **Update**, and **Insert** content.
5. Click **Show** next to the *Internal Integration Secret* and copy it.
   It starts with `ntn_` (older ones start with `secret_`). **Save this — it's
   your `NOTION_TOKEN`.**

### Step 3 — Give the integration access to your database

This is the step people miss, and it produces a confusing "not found" error.

1. Open your assignments database in Notion as a full page.
2. Click the **`•••`** menu in the top-right.
3. Choose **Connections** (older versions: *Add connections*).
4. Select your `Canvas Sync` integration and confirm.

### Step 4 — Get your two remaining values

**Notion database ID** — with the database open as a full page, look at the URL:

```
https://www.notion.so/myworkspace/8f4c2b1e9d7a4f3b8c5e2a1d6b9f0c3e?v=...
                                 └────────── this 32-char chunk ──────────┘
```

Copy that 32-character string (the part *before* `?v=`). That's your
`NOTION_DATABASE_ID`.

**Canvas calendar feed** —

1. In Canvas, go to **Account → Settings**.
2. Scroll to the bottom of the right-hand sidebar and find **Calendar Feed**.
3. **Right-click it → Copy Link Address.** Don't left-click — it opens as a
   `webcal://` link and you'll lose it.
4. That's your `CANVAS_ICS_URL`.

> Treat this URL like a password. It needs no login, so anyone holding it can
> read your assignment schedule. Keeping it in GitHub Secrets (below) is the
> right place for it. If it ever leaks, click **Reset** next to the feed in
> Canvas settings to invalidate the old link.

### Step 5 — Set up email reminders

Reminders are sent through your own Gmail account using an **app password** —
a separate 16-character password that only this script uses, and that you can
revoke at any time without touching your real password.

1. Go to **https://myaccount.google.com/security**.
2. Turn on **2-Step Verification** if it isn't already. App passwords don't
   exist without it — this is the step people get stuck on.
3. Go to **https://myaccount.google.com/apppasswords**.
4. Type a name like `Canvas Reminders` and click **Create**.
5. Copy the 16-character password it shows you. Spaces don't matter; you can
   include or strip them. **This is your `SMTP_PASSWORD`.**

> **Never put your regular Google password in here.** It won't work — Google
> blocks plain-password SMTP — and an app password is safer anyway: it's
> scoped to this one use and revocable from that same page.

Using a different provider? Set `SMTP_HOST` and `SMTP_PORT` to theirs. Any
standard SMTP server works (port 587 for STARTTLS, 465 for implicit TLS).
Note that Outlook/Hotmail personal accounts have largely dropped basic SMTP
auth, so Gmail is the smoother path.

### Step 6 — Put it on GitHub

1. Create a **new private repository** on GitHub.
2. Upload these files, preserving the folder structure:

   ```
   canvas_notion_sync.py
   reminders.py
   requirements.txt
   .github/workflows/sync.yml
   ```

3. In the repo, go to **Settings → Secrets and variables → Actions**.
4. Click **New repository secret** and add these five:

   | Name | Value |
   |---|---|
   | `CANVAS_ICS_URL` | your Canvas feed link |
   | `NOTION_TOKEN` | the `ntn_…` secret from Step 2 |
   | `NOTION_DATABASE_ID` | the 32-character ID from Step 4 |
   | `SMTP_USER` | your Gmail address |
   | `SMTP_PASSWORD` | the 16-character app password from Step 5 |

   Optional: `EMAIL_TO` if reminders should go to a different address than the
   one sending them (defaults to `SMTP_USER`), plus `SMTP_HOST` / `SMTP_PORT`
   for a non-Gmail provider.

5. If your Notion column names differ from the defaults, open
   `.github/workflows/sync.yml` and uncomment/edit the matching `PROP_*` lines.
   To have new assignments land on a specific Status, uncomment `STATUS_DEFAULT`
   and set it to the exact option name (e.g. `"Not started"`).

6. **Check `DONE_STATUSES` matches your Status options.** In `sync.yml` it's
   set to `Done,Complete,Completed,Submitted,Turned in`. If your "finished"
   option is called something else, add it — otherwise you'll keep getting
   reminders for work you've already turned in.

### Step 7 — Run it

1. Go to the **Actions** tab. If prompted, click to enable workflows.
2. Select **Canvas to Notion sync** on the left → **Run workflow**.
3. Watch the log. You should see something like:

   ```
   Fetching Canvas calendar feed...
     23 gradeable item(s) found; 41 non-gradeable calendar entr(ies) filtered out.
   Loading existing Notion rows...
     0 row(s) already tracked.
     + added: Problem Set 3
     ...
   Done. 23 added, 0 updated, 0 already current.

   Checking reminders at 2026-09-15 18:10 CDT (tiers: 4h, 3h, 2h, 1h)
   Done. 0 reminder(s) sent; 4 past-deadline item(s) retired; 23 row(s) checked.
   ```

Check Notion. From here it runs itself every hour.

**Tip for the first run:** if you want to preview without writing anything or
sending pushes, add a temporary secret `DRY_RUN` = `true`, run it, review the
log, then delete the secret.

---

## How the reminders behave

| Situation | What happens |
|---|---|
| Assignment due in 3h 50m, nothing sent yet | One email: *"Due in 3h 50m"*. The 4h tier is marked sent. |
| An hour later, 2h 50m left | One email: *"Due in 2h 50m"*. The 3h tier is marked sent. |
| Under an hour left | Subject reads *"Due in 45m — Problem Set 3"*. (On push, this one goes out at max priority.) |
| Assignment appears with only 45m left | **One** message saying *"Due in 45m"* — not a burst of four. All tiers are retired at once. |
| You mark it Done at the 3h mark | No further reminders for it. |
| The deadline has already passed | Retired silently. No late-notification pile-up. |
| Instructor extends the deadline | Sent reminders are cleared, so the 4/3/2/1 warnings re-arm against the new date. |

**On timing precision.** Reminders fire on the first hourly run *after* a mark
is crossed, so the "4-hour" reminder actually lands somewhere between 3h00m and
4h00m out. If you want them tighter, change the cron in `sync.yml` to
`"*/30 * * * *"` for half-hourly (roughly 960 of your 2,000 free minutes a
month) — but note GitHub's scheduler can itself run late during busy periods,
so treat these as approximate by nature. Don't rely on the 1-hour ping as your
only line of defense on something important.

**Changing the tiers.** Edit `REMINDER_TIERS` in `sync.yml` — e.g. `"24,4,1"`
for a day-before warning plus two on the day. Any whole numbers of hours work.

**Taming your inbox.** Four emails per assignment adds up during a busy week.
Worth setting up a Gmail filter: search `from:(your@gmail.com) subject:(Due in)`,
then **Create filter** → apply a `Canvas` label and **skip the inbox** if you'd
rather check them in one place. Or drop to two tiers (`REMINDER_TIERS: "4,1"`)
for roughly half the volume.

**A delivery failure is retried, not lost.** If Gmail is briefly unreachable,
the tier isn't marked as sent, so the next hourly run tries again.

### Want phone push as well, or instead?

The script also supports [ntfy](https://ntfy.sh) — free push notifications, no
account needed. Install the app, subscribe to a long unguessable topic name,
add it as an `NTFY_TOPIC` secret, and it works alongside email automatically.

`REMINDER_CHANNEL` controls this: `auto` (the default) uses whichever channels
have credentials configured. Set it to `email`, `ntfy`, or `email,ntfy` to be
explicit. Push has one advantage worth noting — the 1-hour reminder is sent at
max priority, which breaks through Do Not Disturb on most phones in a way email
never will.

---

## Things worth knowing

**GitHub pauses schedules on idle repos.** If a repository has no activity for
60 days, GitHub automatically disables its scheduled workflows and emails you.
Re-enable it from the Actions tab in one click. Since you'll likely touch this
repo rarely, expect that email roughly once a semester — it is the one piece of
upkeep this system has.

**Cost.** A run takes well under a minute. Hourly is roughly 400–500 minutes of
compute a month, comfortably inside the 2,000 free minutes private repos get.
Public repos are unlimited, but keep it private anyway — no reason to publish
your schedule.

**Timing isn't exact.** GitHub's scheduler can delay jobs during busy periods,
sometimes by minutes and occasionally longer. The tier logic is built to
tolerate this: a delayed run still sends the reminder (slightly late) rather
than skipping it, because tiers fire on "time remaining is under N hours and
this tier hasn't been sent," not on hitting an exact moment.

**Deleted assignments stay in Notion.** If an instructor removes an assignment,
the script leaves the existing Notion row alone rather than deleting your work.
Remove those by hand if you like.

**Undated assignments may not appear.** Canvas usually omits assignments with no
due date from the calendar feed entirely.

**Renames are handled.** Matching is on the Canvas ID, not the title, so an
instructor renaming an assignment updates the existing row instead of creating
a duplicate.

---

## Troubleshooting

| Symptom | Cause and fix |
|---|---|
| `Notion API 404 … Could not find database` | Step 3 was skipped, or the ID is wrong. Re-check the integration's Connections on the database. |
| `Your database needs a text property named 'Canvas ID'` | Add that Text column (Step 1). |
| `Notion API 400 … is not a property that exists` | A `PROP_*` name doesn't match your Notion column. Fix it in `sync.yml`. |
| `Notion API 400 … Invalid select option` | Your `STATUS_DEFAULT` isn't an existing option in that Status column. Match it exactly, or leave it blank. |
| Everything filtered out (`0 gradeable items`) | You likely copied the wrong link. It should contain `/feeds/calendars/` and end in `.ics`. |
| `That URL didn't return a calendar feed` | You copied the page you landed on rather than the link address. Right-click → Copy Link Address. |
| Duplicates appearing | The `Canvas ID` column is being cleared or renamed. It must persist untouched. |
| `Your database needs a text property named 'Reminders Sent'` | Add that Text column (Step 1). |
| `SMTP login was rejected` | You used your normal Google password instead of the 16-character app password, or 2-Step Verification isn't on. Redo Step 5. |
| `No delivery channel configured` | `SMTP_USER` and `SMTP_PASSWORD` aren't both set as secrets. |
| No emails arriving, but the log says "sent" | Check spam — the first one often lands there since you're emailing yourself. Mark it "not spam" once and it'll stop. |
| No push notifications arriving | Confirm the topic in the app matches `NTFY_TOPIC` exactly (case-sensitive), then test with `curl -d "test" ntfy.sh/your-topic`. |
| Same reminder repeatedly | The `Reminders Sent` column is being cleared. Check no Notion automation or template is wiping it. |
| Reminders for work you've turned in | Your "finished" Status option isn't in `DONE_STATUSES`. Add its exact name in `sync.yml`. |
| Reminders arrive at the wrong hour | `TIMEZONE` in `sync.yml` doesn't match yours. It's set to `America/Chicago`. |

---

## Running it locally instead

If you'd rather not use GitHub:

```bash
pip install -r requirements.txt

export CANVAS_ICS_URL="https://…/feeds/calendars/….ics"
export NOTION_TOKEN="ntn_…"
export NOTION_DATABASE_ID="8f4c2b1e9d7a4f3b8c5e2a1d6b9f0c3e"
export SMTP_USER="you@gmail.com"
export SMTP_PASSWORD="your-16-char-app-password"

python canvas_notion_sync.py && python reminders.py
```

Schedule it with `cron` (macOS/Linux) or Task Scheduler (Windows). The tradeoff
is that it only syncs while your computer is on and online — and for deadline
reminders that's a real problem, since a closed laptop means no notification.
This is why GitHub Actions is the better default here.

## Tests

```bash
python test_filter.py     # office hours / streams / holidays are filtered out
python test_upsert.py     # a checked-off Status survives a due-date change
python test_reminders.py  # tiers, channel selection, and email construction
```

All three run offline against sample data — no credentials, no network, nothing
written to Notion. Run them after any edit you make.
