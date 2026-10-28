#!/usr/bin/env python3.11
"""
todo_sync.py — turn Slack signals into cards for the Todo board.

What it does, every run:
  1. finds every message I reacted to with :todo:            -> a card
  2. finds messages that @-mention me and I have not answered -> a card at P4
  3. if anyone put a checkmark on that message, the card is done
  4. stamps :todo_added: back on the message so I can see it registered
  5. writes board/slack.json, which the board fetches at load

Config (in /Users/joris/Documents/scripts/ts-compliance/.env):
  SLACK_TODO_TOKEN=xoxp-...   user token, scopes:
                              search:read, reactions:read, reactions:write,
                              channels:history, groups:history, im:history, mpim:history

Run it:
  python3.11 todo_sync.py --dry-run    # print what would happen, touch nothing
  python3.11 todo_sync.py              # write slack.json + stamp reactions
  python3.11 todo_sync.py --deploy     # ...and push the board to Disco

Must run under launchd, not cron: cron lives outside the GUI session. Same
lesson as disco_sync.py.

Failure is loud on purpose. The 2026-07-28 sync died silently for eight days;
this exits non-zero and says why, so the LaunchAgent log shows it.
"""

import hashlib
import json
import os
import re
import subprocess
import sys
import urllib.parse
import time
import urllib.request
from datetime import datetime, timedelta, timezone

from dotenv import load_dotenv

ENV_PATH = "/Users/joris/Documents/scripts/ts-compliance/.env"
load_dotenv(ENV_PATH, override=True)

ME = "U07KAU0MYFN"
# Everything this job writes lives inside this repo: the board files it deploys, and its
# state in logs/ (never committed).
ROOT = os.path.dirname(os.path.abspath(__file__))
LOGS = os.path.join(ROOT, "logs")
os.makedirs(LOGS, exist_ok=True)
BOARD_DIR = os.path.join(ROOT, "board")
OUT_PATH = os.path.join(BOARD_DIR, "slack.json")
HEALTH_PATH = os.path.join(BOARD_DIR, "health.json")
LEDGER_PATH = os.path.join(LOGS, "todo_sync_ledger.json")
BRIEF_CACHE = os.path.join(LOGS, "todo_sync_briefings.json")
PLIST_PATH = "/Users/joris/Library/LaunchAgents/com.ticketswap.todo.sync.plist"
BRIEF_MIN_SECONDS = 30 * 60   # never re-answer the standing questions faster than this
STARTED = time.time()         # set at import, so health reports this run's own duration

# Once something has been done for this long, stop emitting it. Without this the
# board only ever grows: a :todo: reaction stays on the message forever, so the
# card would keep coming back every five minutes for the rest of the year.
RETIRE_AFTER_DAYS = 7        # after this, a finished card leaves the board
ARCHIVE_KEEP_DAYS = 180      # ...and lives in the Archive tab until this

# Year log of finished work — one row per item, written once. The ledger's
# "sheeted" flag is what stops a completed card being re-appended every 5 min.
SHEET_ID = "1BbDd0wOEsKc-exO3mCntA9QNwX9HdOfpwvhv4wzrolU"
SHEET_TAB = "Sheet1"
SHEET_KEY = "/Users/joris/Documents/scripts/ts-compliance/todo-board-service-account.json"
SHEET_HEADER = ["Closed", "What", "Source", "Priority", "Days open", "Days waiting", "Link"]

# Meeting notes. Action items may live in the "Action items" column or as
# checkbox blocks in the page body — different note-takers fill different ones,
# so read both.
NOTION_DB = "11c529ad-f59f-4452-9649-3831be682535"
NOTION_DAYS = 30
NOTION_HEADINGS = ("action", "todo", "to do", "to-do", "next step", "follow up", "follow-up")

# Agenda strip. Same service-account key as the sheet; the calendar is shared
# with it read-only. Recurring filler is dropped or the strip is all lunches.
CALENDAR_ID = "joris.dekoning@ticketswap.com"
CALENDAR_DAYS = 7
CALENDAR_SKIP = ("lunch", "office", "out of office", "ooo", "focus", "travel", "commute")
DISCO = "/Users/joris/.local/bin/disco"
DISCO_BASE = "https://disco.ticketswap.dev"
BOARD_APP = "todo-board"
# Cloudflare rejects a bare urllib signature with error 1010 — any normal
# User-Agent gets through. This cost an afternoon of "403, must be permissions".
UA = "todo-sync/1.0"

# Any of these from anyone means the request is finished.
DONE_EMOJI = {
    "white_check_mark", "heavy_check_mark", "ballot_box_with_check",
    "todo_done", "done", "heavy_tick_mark",
}
CAPTURED_EMOJI = "todo_added"   # stamped back so I can see it landed

# Mentions are noisy: a mention means someone wants attention, not that I took
# something on. Off by default — hit :todo: on anything that is really mine.
MENTIONS = "--mentions" in sys.argv
MENTION_DAYS = 10

STATS = {"stamped": 0}

DRY = "--dry-run" in sys.argv
DEPLOY = "--deploy" in sys.argv


class SlackError(RuntimeError):
    pass


def token():
    t = (os.getenv("SLACK_TODO_TOKEN") or "").strip().strip('"').strip("'")
    if not t:
        raise SlackError(
            "SLACK_TODO_TOKEN is empty in %s — paste the xoxp- user token there." % ENV_PATH
        )
    if not t.startswith("xoxp-"):
        raise SlackError(
            "SLACK_TODO_TOKEN does not start with xoxp-. A bot token (xoxb-) cannot "
            "use search.messages; reinstall the app and copy the *User* OAuth Token."
        )
    return t


def api(method, params=None, post=False):
    """Call Slack. Raises on transport failure or ok:false, never returns junk."""
    url = "https://slack.com/api/" + method
    data = None
    headers = {"Authorization": "Bearer " + token()}
    if post:
        data = urllib.parse.urlencode(params or {}).encode()
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    elif params:
        url += "?" + urllib.parse.urlencode(params)

    req = urllib.request.Request(url, data=data, headers=headers)
    with urllib.request.urlopen(req, timeout=30) as r:
        payload = json.load(r)

    if not payload.get("ok"):
        err = payload.get("error", "unknown")
        if err == "missing_scope":
            raise SlackError(
                "%s needs a scope the token does not have (%s). Reinstall the app "
                "with the manifest in slack_todo_app.manifest.json." % (method, payload.get("needed"))
            )
        raise SlackError("%s failed: %s" % (method, err))
    return payload


def notion(method, path, body=None):
    key = (os.getenv("NOTION_TODO_TOKEN") or os.getenv("NOTION_TOKEN") or "").strip()
    if not key:
        return None
    req = urllib.request.Request(
        "https://api.notion.com/v1" + path,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Authorization": "Bearer " + key,
                 "Notion-Version": "2022-06-28",
                 "Content-Type": "application/json"},
        method=method)
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)


def _plain(prop):
    t = (prop or {}).get("type")
    if t in ("rich_text", "title"):
        return "".join(x.get("plain_text", "") for x in prop.get(t, []))
    return ""


def notion_cards():
    """One card per open action item found in a recent meeting note."""
    since = (datetime.now(timezone.utc) - timedelta(days=NOTION_DAYS)).strftime("%Y-%m-%d")
    try:
        res, pages, cursor = None, [], None
        while True:
            body = {"page_size": 100}
            if cursor:
                body["start_cursor"] = cursor
            res = notion("POST", "/databases/%s/query" % NOTION_DB, body)
            if not res:
                break
            pages += res.get("results", [])
            cursor = res.get("next_cursor")
            if not res.get("has_more"):
                break
        if res is not None:
            res = {"results": pages}
    except Exception as e:
        print("  ! Notion query failed: %s" % e)
        return []
    if not res:
        return []

    cards = []
    for page in res.get("results", []):
        props = page.get("properties", {})
        when_raw = ((props.get("Date") or {}).get("date") or {}).get("start", "")[:10]
        if when_raw and when_raw < since:
            continue                      # filtered here, not by the API
        meeting = _plain(props.get("Meeting")) or "Meeting"
        when = ((props.get("Date") or {}).get("date") or {}).get("start", "")[:10]
        url = page.get("url", "")
        items = []

        # 1. the Action items column
        for line in _plain(props.get("Action items")).splitlines():
            line = line.strip(" -•\t")
            if line:
                items.append(line)

        # 2. unchecked checkboxes, and bullets under an action-ish heading
        state = {"under_actions": False, "seen": 0}
        item_blocks = []

        def walk(block_id, depth=0):
            if depth > 3 or state["seen"] > 400:
                return
            try:
                kids = notion("GET", "/blocks/%s/children?page_size=100" % block_id) or {}
            except Exception:
                return
            for b in kids.get("results", []):
                state["seen"] += 1
                t = b.get("type")
                body = b.get(t) or {}
                text = "".join(x.get("plain_text", "") for x in body.get("rich_text", [])) \
                    if isinstance(body, dict) else ""
                if t.startswith("heading"):
                    state["under_actions"] = any(h in text.lower() for h in NOTION_HEADINGS)
                elif t == "to_do" and not body.get("checked") and text.strip():
                    items.append(text.strip())
                    item_blocks.append((text.strip(), b["id"]))
                elif state["under_actions"] and t in ("bulleted_list_item", "numbered_list_item") \
                        and text.strip():
                    items.append(text.strip())
                if b.get("has_children"):
                    walk(b["id"], depth + 1)

        walk(page["id"])

        blocks_by_text = {t: b for t, b in item_blocks}
        for n, text in enumerate(dict.fromkeys(items)):      # de-dupe, keep order
            cards.append({
                # Hash the text, never the position. With an index, adding one
                # item to the top of a note re-ids every card below it and
                # orphans its priority, ticks and dates.
                "id": "notion-%s-%s" % (
                    page["id"].replace("-", "")[:12],
                    hashlib.sha1(" ".join(text.lower().split()).encode()).hexdigest()[:10]),
                "col": "todo",
                "tag": "Notion \u00b7 %s" % meeting[:26],
                "due": when or "",
                "p": 3,
                "title": text[:110],
                "checks": [{"t": text[:400], "ok": False}],
                "why": "Action item from <b>%s</b>%s." % (meeting, " on " + when if when else ""),
                "who": [],
                "refs": [{"t": "Notion", "u": url}],
                "_channel": None, "_ts": None, "_kind": "notion", "_text": text,
                "_block": blocks_by_text.get(text),
                "_captured": True,          # nothing to stamp back in Notion
            })
    return cards


def agenda(days_back=0):
    """Real meetings either side of now, for the strip and the notes check."""
    try:
        import urllib.error
        from google.oauth2 import service_account
        import google.auth.transport.requests as gt
        creds = service_account.Credentials.from_service_account_file(
            SHEET_KEY, scopes=["https://www.googleapis.com/auth/calendar.readonly"])
        creds.refresh(gt.Request())
        now = datetime.now(timezone.utc)
        q = urllib.parse.urlencode({
            "timeMin": (now - timedelta(days=days_back)).isoformat(
                timespec="seconds").replace("+00:00", "Z"),
            "timeMax": (now + timedelta(days=CALENDAR_DAYS)).isoformat(
                timespec="seconds").replace("+00:00", "Z"),
            "singleEvents": "true", "orderBy": "startTime", "maxResults": "50",
        })
        req = urllib.request.Request(
            "https://www.googleapis.com/calendar/v3/calendars/%s/events?%s"
            % (urllib.parse.quote(CALENDAR_ID), q),
            headers={"Authorization": "Bearer " + creds.token})
        with urllib.request.urlopen(req, timeout=30) as r:
            items = json.load(r).get("items", [])
    except Exception as e:
        print("  ! calendar read failed: %s" % e)
        return []

    out = []
    for e in items:
        start = (e.get("start") or {}).get("dateTime")
        if not start:
            continue                                  # all-day: Office, OOO, birthdays
        name = (e.get("summary") or "").strip()
        if not name or any(w in name.lower() for w in CALENDAR_SKIP):
            continue
        mine = next((a for a in e.get("attendees", [])
                     if a.get("self") or a.get("email") == CALENDAR_ID), None)
        when = datetime.fromisoformat(start)
        out.append({
            "day": when.strftime("%a"),
            "time": when.strftime("%H:%M"),
            "iso": when.isoformat(timespec="minutes"),
            "title": name[:52],
            "url": e.get("htmlLink", ""),
            "declined": bool(mine and mine.get("responseStatus") == "declined"),
        })
    return out[:9]


def meetings_without_notes(events):
    """Calendar entries in the last fortnight with no matching note in the Hub.

    Nothing tells you when a meeting leaves no record; this makes the gap
    visible instead of something you find weeks later.
    """
    try:
        res, pages, cursor = None, [], None
        while True:
            body = {"page_size": 100}
            if cursor:
                body["start_cursor"] = cursor
            res = notion("POST", "/databases/%s/query" % NOTION_DB, body)
            if not res:
                return []
            pages += res.get("results", [])
            cursor = res.get("next_cursor")
            if not res.get("has_more"):
                break
    except Exception:
        return []

    noted = set()
    for pg in pages:
        t = _plain(pg["properties"].get("Meeting")).lower()
        d = ((pg["properties"].get("Date") or {}).get("date") or {}).get("start", "")[:10]
        noted.add((d, " ".join(t.split())[:28]))

    missing = []
    for e in events:
        day = e["iso"][:10]
        name = " ".join(e["title"].lower().split())[:28]
        if e.get("declined") or (day, name) in noted:
            continue
        if any((day, n) == (day, name) for (dd, n) in noted if dd == day):
            continue
        missing.append(e)
    return missing


def search(query, count=100):
    return api("search.messages", {"query": query, "count": count, "sort": "timestamp"}) \
        .get("messages", {}).get("matches", [])


def reactions_on(channel, ts):
    """Reaction names on a message. Missing message is not fatal — it may have been deleted."""
    try:
        msg = api("reactions.get", {"channel": channel, "timestamp": ts}).get("message", {})
    except SlackError as e:
        if "message_not_found" in str(e) or "channel_not_found" in str(e):
            return None
        raise
    return {r["name"]: r.get("users", []) for r in msg.get("reactions", [])}


def i_reacted(reacts):
    """Any reaction from me means I have already engaged with it."""
    return any(ME in users for users in (reacts or {}).values())


def conversation_around(channel, ts, thread_ts=None, span=5):
    """The few messages either side, so a task reads as a task.

    A flagged message is usually the end of an exchange — "oke dat kan denk ik
    wel" means nothing on its own. The surrounding turns are what carry the ask.
    """
    msgs = []
    try:
        if thread_ts and thread_ts != ts:
            msgs = api("conversations.replies", {
                "channel": channel, "ts": thread_ts, "limit": span * 2,
            }).get("messages", [])
        else:
            before = api("conversations.history", {
                "channel": channel, "latest": ts, "limit": span, "inclusive": "true",
            }).get("messages", [])
            after = api("conversations.history", {
                "channel": channel, "oldest": ts, "limit": span, "inclusive": "false",
            }).get("messages", [])
            msgs = list(reversed(before)) + list(reversed(after))
    except SlackError:
        return ""

    lines = []
    for m in msgs:
        text = clean(m.get("text", ""), 220)
        if not text:
            continue
        who = "me" if m.get("user") == ME else (m.get("user_profile", {}) or {}).get(
            "display_name") or m.get("username") or "them"
        mark = "  <-- flagged" if m.get("ts") == ts else ""
        lines.append("%s: %s%s" % (who, text, mark))
    return "\n".join(lines[:12])[:1400]


def answered_by_me(channel, ts, thread_ts=None):
    """True if I replied after this message — an answered mention is not a todo."""
    try:
        if thread_ts and thread_ts != ts:
            msgs = api("conversations.replies", {
                "channel": channel, "ts": thread_ts, "limit": 50,
            }).get("messages", [])
            return any(m.get("user") == ME and float(m.get("ts", 0)) > float(ts) for m in msgs)
        msgs = api("conversations.history", {
            "channel": channel, "oldest": ts, "limit": 30, "inclusive": "false",
        }).get("messages", [])
        return any(m.get("user") == ME for m in msgs)
    except SlackError:
        # No history access to this conversation: fail open, keep the card,
        # rather than silently dropping something that might matter.
        return False


def clean(text, limit=110):
    """Slack text -> a readable one-liner."""
    import re
    t = text or ""
    t = re.sub(r"<@%s(\|[^>]+)?>" % ME, "@me", t)
    t = re.sub(r"<@[UW][A-Z0-9]+\|([^>]+)>", r"@\1", t)
    t = re.sub(r"<@([UW][A-Z0-9]+)>", "@someone", t)
    t = re.sub(r"<(https?://[^|>]+)\|([^>]+)>", r"\2", t)
    t = re.sub(r"<(https?://[^>]+)>", r"\1", t)
    t = t.replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")
    t = " ".join(t.split())
    return t[:limit].rstrip() + ("…" if len(t) > limit else "")


QUALIFY_CACHE = os.path.join(LOGS, "todo_sync_cache.json")
MODEL = "claude-sonnet-5"

QUALIFY_PROMPT = """You triage Slack messages for Joris de Koning, who runs \
partnerships and lead generation at TicketSwap (HubSpot, Apollo, leadgen \
dashboards, partner deals).

For each message decide whether it is a REAL PIECE OF WORK FOR JORIS: something \
he has to go and do, or answer, or decide. A message is NOT work if it is \
banter, praise, a celebration, an FYI, a broadcast/announcement, someone \
thinking out loud, a bare mention with no request, or a request clearly aimed \
at someone else.

Messages marked source=todo were deliberately flagged by Joris himself, so \
always keep them (keep=true) — just write a good title.

Some messages come with the surrounding conversation. Use it: the flagged line
is often a reply ("sure, I can do that") and the actual request is a turn or two
earlier. Write the title as the thing Joris has to DO, not as a quote of the
message. Where the conversation makes the ask concrete — a name, a system, a
number, a wording someone proposed — put that in the title or the ask.

Return ONLY a JSON array, one object per input, same order:
[{"id": "<id>", "keep": true|false, "title": "<imperative, <=70 chars, English>", \
  "ask": "<one sentence: what is actually being asked and by whom, from the \
conversation. Empty string if there is no context.>", \
  "priority": 1-5, "reason": "<max 12 words>"}]

Priority: 1 someone is blocked or it is due now, 2 clear task with a real \
deadline, 3 normal, 4 minor or vague, 5 barely worth keeping."""


def qualify(items):
    """Ask Claude which candidates are genuinely work. Cached per message id so
    the 5-minute loop costs nothing when nothing changed."""
    key = os.getenv("ANTHROPIC_API_KEY_LEADGEN") or os.getenv("ANTHROPIC_API_KEY")
    if not key or not items:
        return {}

    try:
        with open(QUALIFY_CACHE, encoding="utf-8") as f:
            cache = json.load(f)
    except Exception:
        cache = {}

    todo = [i for i in items if i["id"] not in cache]
    got = False
    # Sending everything at once outgrew the reply budget once context was
    # added, and one oversized batch silently lost every verdict. Small batches
    # fail alone; the rest still get triaged.
    for i in range(0, len(todo), 5):
        batch = todo[i:i + 5]
        payload = json.dumps({
            "model": MODEL,
            "max_tokens": 4000,
            "system": QUALIFY_PROMPT,
            "messages": [{"role": "user", "content": json.dumps(batch, ensure_ascii=False)}],
        }).encode()
        req = urllib.request.Request(
            "https://api.anthropic.com/v1/messages", data=payload,
            headers={"x-api-key": key, "anthropic-version": "2023-06-01",
                     "content-type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=90) as r:
                body = json.load(r)
            text = "".join(b.get("text", "") for b in body.get("content", []))
            start, end = text.find("["), text.rfind("]")
            if start < 0 or end < start:
                raise ValueError("no JSON array in reply (stop: %s)" % body.get("stop_reason"))
            for row in json.loads(text[start:end + 1]):
                cache[row["id"]] = row
            got = True
        except Exception as e:
            print("  ! triage batch %d failed (%s) — those keep their raw text" % (i // 5 + 1, e))
    if got:
        try:
            os.makedirs(os.path.dirname(QUALIFY_CACHE), exist_ok=True)
            with open(QUALIFY_CACHE, "w", encoding="utf-8") as f:
                json.dump(cache, f, indent=1, ensure_ascii=False)
        except Exception as e:
            print("  ! could not write triage cache: %s" % e)
    return cache


def card_from(match, kind):
    """Build a board card. Shape matches the CARDS entries in index.html."""
    channel = match.get("channel", {}).get("id", "")
    ts = match.get("ts", "")
    author = match.get("username") or "someone"
    when = datetime.fromtimestamp(float(ts), tz=timezone.utc).astimezone()

    reacts = reactions_on(channel, ts)
    if reacts is None:
        return None                        # message gone; drop the card
    finished = bool(DONE_EMOJI & set(reacts))

    text = clean(match.get("text", ""), 110)
    full = clean(match.get("text", ""), 400)

    if kind == "todo":
        tag, pri = "Slack · %s" % author, 2
        why = "Tagged <b>:todo:</b> on %s's message, %s." % (author, when.strftime("%-d %b %H:%M"))
    else:
        tag, pri = "Mention · %s" % author, 4
        why = ("%s mentioned me on %s and I have not replied yet. "
               "Dismiss with × if it needs nothing." % (author, when.strftime("%-d %b %H:%M")))

    if finished:
        why += " Closed by a checkmark in Slack."

    return {
        "id": "slack-%s-%s" % (channel, ts.replace(".", "")),
        "col": "done" if finished else "todo",
        "tag": tag,
        "due": when.strftime("%-d %b"),
        "p": pri,
        "title": text or "(no text)",
        "checks": [{"t": full, "ok": finished}],
        "why": why,
        "who": [author],
        "refs": [{"t": "Slack", "u": match.get("permalink", "")}],
        "_channel": channel,
        "_ts": ts,
        "_ctx": conversation_around(channel, ts, match.get("thread_ts")) if kind == "todo" else "",
        "_kind": kind,
        "_text": full,
        "_captured": CAPTURED_EMOJI in reacts,
    }


def stamp(card):
    """Add :todo_added: so the capture is visible in Slack. Never fatal."""
    if card["_captured"] or DRY:
        return
    try:
        api("reactions.add", {
            "channel": card["_channel"], "timestamp": card["_ts"], "name": CAPTURED_EMOJI,
        }, post=True)
    except SlackError as e:
        if "already_reacted" not in str(e):
            print("  ! could not stamp %s: %s" % (card["id"], e))


def load_ledger():
    """What we have already seen, completed, and written to the sheet.

    Durable state lives here and in Slack's own reactions — never in the browser.
    A card the viewer dismisses with x is local to that browser; the ledger is
    what stops the sync itself from re-adding or double-reporting anything.
    """
    try:
        with open(LEDGER_PATH, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_ledger(ledger):
    try:
        os.makedirs(os.path.dirname(LEDGER_PATH), exist_ok=True)
        with open(LEDGER_PATH, "w", encoding="utf-8") as f:
            json.dump(ledger, f, indent=1)
    except Exception as e:
        print("  ! could not write ledger: %s" % e)


# Standing questions, answered on the Mac with the fast key and shipped as data.
# The board never sees an API key: the thinking is already done by the time the
# page loads, which also makes it instant rather than 45 seconds.
BRIEFINGS = [
    ("hubspot", "Group every HubSpot-related item together and structure it as a "
                "message Joris can paste into Slack."),
    ("late",    "What is late or due within three days? Say which card, how late, "
                "and what to do first."),
    ("waiting", "What is Joris waiting on other people for? Name the person per "
                "item and how long it has been waiting."),
    ("streams", "Group everything by stream or system, one line of status per group."),
]

BRIEF_SYSTEM = (
    "You summarise Joris's work board. Use ONLY the snapshot given. Never invent a "
    "card, date or name; if the snapshot does not say, say so. Write something he "
    "can paste into Slack: *single asterisks* for bold, plain hyphen bullets, no "
    "markdown headings, no tables. Reference cards by title, name the people and "
    "systems the data names, under 180 words, no preamble.\n"
    # Every early answer listed the items and then said the same thing again in a
    # closing line ("that's 6 open HubSpot items, all P3"), which reads as the
    # board showing everything twice. The list is the answer; stop after it.
    "Mention each card exactly once. Never restate, recap or count up items you "
    "have already listed, and do not end with a summary sentence about the list.\n"
    # The priority belongs on the line itself. It used to appear only in the
    # closing recap ("all are P3"), which is the line that read as a repeat.
    "Start every bullet with the card's priority — '- P2 Update Lookerboards…' — "
    "and order each list P1 first, P5 last."
)


def board_snapshot(cards):
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    lines = []
    for c in cards:
        bits = ["- " + c["title"], "col=" + c["col"], "P%d" % c.get("p", 3)]
        if c.get("tag"):
            bits.append("source=" + c["tag"])
        if c.get("dueISO"):
            bits.append("due=" + c["dueISO"])
        if c.get("doneISO"):
            bits.append("finished=" + c["doneISO"])
        if c.get("blockedDays"):
            bits.append("waiting %dd" % c["blockedDays"])
        for w in (c.get("who") or [])[:2]:
            bits.append("with=" + w)
        lines.append(" | ".join(bits))
    return "Today is %s.\n\n%s" % (today, "\n".join(lines))


def briefings(cards):
    """Answer the standing questions now, so the board can show them instantly."""
    key = os.getenv("ANTHROPIC_API_KEY_LEADGEN") or os.getenv("ANTHROPIC_API_KEY")
    if not key or DRY or not cards:
        return []
    snap = board_snapshot(cards)

    # These answers only change when the board changes, but this runs every five
    # minutes — which quietly spent four Claude calls a run, ~24,000 of them,
    # re-answering identical questions about an identical board. Key the cache on
    # the snapshot itself: unchanged board, zero API calls.
    # Hash the lines SORTED. Slack search hands cards back in a different order
    # from run to run, so hashing the snapshot as-written made an unchanged board
    # look different nearly every time and the cache almost never hit — 2 hits to
    # 87 paid runs. Order is not content.
    sig = hashlib.sha1(
        ("\n".join(sorted(snap.splitlines())) + "||" +
         "|".join(q for _, q in BRIEFINGS) + "||" + BRIEF_SYSTEM)
        .encode("utf-8")).hexdigest()
    cached = {}
    try:
        with open(BRIEF_CACHE, encoding="utf-8") as f:
            cached = json.load(f)
    except Exception:
        pass
    if cached.get("sig") == sig and cached.get("briefings"):
        print("  briefings unchanged — reused %d (no API calls)" % len(cached["briefings"]))
        return cached["briefings"]

    # Even when the board really has changed, this runs every five minutes and
    # these are standing questions, not alerts. A floor puts a hard ceiling on
    # the spend: at worst 2 refreshes an hour instead of 12.
    if cached.get("briefings") and cached.get("at"):
        try:
            age = (datetime.now(timezone.utc) -
                   datetime.fromisoformat(cached["at"])).total_seconds()
            if age < BRIEF_MIN_SECONDS:
                print("  briefings %dm old — reused %d (no API calls)" %
                      (age // 60, len(cached["briefings"])))
                return cached["briefings"]
        except Exception:
            pass

    out = []
    for name, question in BRIEFINGS:
        text, why = "", ""
        # Two of four answers went missing on the first real run and `if text:`
        # dropped them without a word. The logging added here found the cause on
        # the very next run: stop_reason=max_tokens with blocks=['thinking'] —
        # the model spent the whole budget reasoning and never got to the answer.
        # So the budget has to cover thinking *and* ~250 words of output, and the
        # retry doubles it rather than repeating an attempt that cannot fit.
        # Thinking off, and a small cap. These are 180-word summaries of a short
        # snapshot — the reasoning was adding ~1-2k billed tokens a call for no
        # gain, and it was the reason answers came back empty: it ate the whole
        # budget before writing anything. With it off, 1200 is ample.
        for cap in (1200, 2400):
            payload = json.dumps({
                "model": MODEL, "max_tokens": cap, "system": BRIEF_SYSTEM,
                "thinking": {"type": "disabled"},
                "messages": [{"role": "user",
                              "content": "SNAPSHOT:\n%s\n\nQUESTION: %s" % (snap, question)}],
            }).encode()
            req = urllib.request.Request(
                "https://api.anthropic.com/v1/messages", data=payload,
                headers={"x-api-key": key, "anthropic-version": "2023-06-01",
                         "content-type": "application/json"})
            try:
                with urllib.request.urlopen(req, timeout=120) as r:
                    body = json.load(r)
                text = "".join(b.get("text", "") for b in body.get("content", [])).strip()
                cut = body.get("stop_reason") == "max_tokens"
                if text and not cut:
                    break
                if text and cut:
                    # A truncated answer is still text, so the old code published
                    # it mid-sentence. Keep it only as a fallback and try again
                    # with room to finish.
                    if cap == 2400:
                        print("  ! briefing '%s' cut off even at 2400 tokens" % name)
                        break
                    why = "cut off at %d tokens" % cap
                    time.sleep(2)
                    continue
                why = "empty reply (stop_reason=%s, blocks=%s, cap=%d)" % (
                    body.get("stop_reason"),
                    [b.get("type") for b in body.get("content", [])], cap)
            except Exception as e:
                why = str(e)
            if cap == 1200:
                time.sleep(2)
        if text:
            out.append({"id": name, "question": question, "answer": text})
        else:
            print("  ! briefing '%s' produced nothing: %s" % (name, why))
    print("  wrote %d of %d briefing(s)" % (len(out), len(BRIEFINGS)))
    # Only cache a complete set: caching a partial one would pin a missing
    # answer in place until the board happens to change again.
    if len(out) == len(BRIEFINGS):
        try:
            os.makedirs(os.path.dirname(BRIEF_CACHE), exist_ok=True)
            with open(BRIEF_CACHE, "w", encoding="utf-8") as f:
                json.dump({"sig": sig,
                           "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                           "briefings": out}, f)
        except Exception as e:
            print("  ! could not cache briefings: %s" % e)
    return out


def board_state():
    """What the board itself says — the ticks and moves made in the browser.

    Shared state lives in the Disco app DB, so the sync can see completions that
    happened on the board rather than in Slack. Returns {} if nothing is stored,
    which is also what a board running on browser-only state looks like.
    """
    try:
        # Keychain on the Mac, DISCO_TOKEN where there is none (a cloud session).
        try:
            tok = subprocess.check_output(
                ["security", "find-generic-password", "-s", "disco-cli", "-a", DISCO_BASE, "-w"],
                stderr=subprocess.DEVNULL, text=True).strip()
        except Exception:
            tok = ""
        tok = tok or os.getenv("DISCO_TOKEN", "").strip()
        if not tok:
            return {}
        req = urllib.request.Request(
            "%s/%s/__api/db/board_state?limit=5" % (DISCO_BASE, BOARD_APP),
            headers={"Authorization": "Bearer " + tok, "User-Agent": UA,
                     "Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=25) as r:
            d = json.load(r)
        # Disco returns {"docs": [...]}. Guessing at "items"/"results" is what
        # made me report "0 rows" and wrongly conclude shared state was broken.
        rows = d if isinstance(d, list) else (
            d.get("docs") or d.get("items") or d.get("results") or [])
        rows = [x for x in rows if x and x.get("blob")]
        if not rows:
            return {}
        rows.sort(key=lambda x: x.get("updatedAt") or "", reverse=True)
        blob = rows[0]["blob"]
        return json.loads(blob) if isinstance(blob, str) else (blob or {})
    except Exception as e:
        print("  ! could not read board state: %s" % e)
        return {}


def finished_ids(st):
    """Card ids that count as finished on the board, following any merges.

    A duplicate folded into another card never gets a move of its own, so without
    following the merge map its Slack message would keep its :todo: for ever and
    the loop would look broken from Slack's side — which is the whole reason the
    board writes back at all.
    """
    moves = (st or {}).get("moves") or {}
    merged = (st or {}).get("merged") or {}
    done = set(i for i, col in moves.items() if col == "done")

    def root(i):
        seen = set()
        while i in merged and i not in seen:
            seen.add(i)
            i = merged[i]
        return i

    for child in merged:
        if root(child) in done:
            done.add(child)
    return done


def slack_mark_done(cards, st):
    """Tick the Slack message when the card was finished on the board.

    Closes the loop the other way: a checkmark in Slack already finishes a card,
    but finishing one on the board used to leave Slack looking untouched.
    """
    done_ids = finished_ids(st)
    if not done_ids:
        return 0
    if DRY:
        would = [c for c in cards
                 if c["id"] in done_ids and c.get("_ts") and c.get("_channel")]
        print("  would mark %d Slack message(s) done:" % len(would))
        for c in would:
            print("     %s" % c["title"][:66])
        return 0
    ledger = load_ledger()
    marked = 0
    for c in cards:
        if c["id"] not in done_ids or not c.get("_ts") or not c.get("_channel"):
            continue
        e = ledger.setdefault(c["id"], {})
        if e.get("slackClosed"):
            continue
        reacts = reactions_on(c["_channel"], c["_ts"]) or {}
        if DONE_EMOJI & set(reacts):
            e["slackClosed"] = True            # someone already marked it
            continue
        try:
            api("reactions.add", {"channel": c["_channel"], "timestamp": c["_ts"],
                                  "name": "todo_done"}, post=True)
            e["slackClosed"] = True
            marked += 1
        except SlackError as err:
            if "already_reacted" in str(err):
                e["slackClosed"] = True
            else:
                print("  ! could not mark %s done in Slack: %s" % (c["id"], err))
    if marked:
        save_ledger(ledger)
        print("  marked %d item(s) done in Slack" % marked)
    return marked


def notion_check_back(cards):
    """Tick the matching checkbox in the meeting note when a card is finished.

    Without this a note slowly drifts out of date: the work is done but the
    page still shows it open. Only touches blocks this script created cards
    from, and only ever sets checked=true.
    """
    if DRY:
        return 0
    done = 0
    for c in cards:
        if c.get("col") != "done" or not c.get("_block"):
            continue
        try:
            notion("PATCH", "/blocks/%s" % c["_block"], {"to_do": {"checked": True}})
            done += 1
        except Exception as e:
            print("  ! could not tick Notion block for %s: %s" % (c["id"], e))
    if done:
        print("  ticked %d item(s) back in Notion" % done)
    return done


def sheet_rows(rows):
    """Append to the year log. Never fatal — a sheet outage must not stop the board."""
    if not rows or DRY:
        return False
    try:
        import gspread
        from google.oauth2.service_account import Credentials
        creds = Credentials.from_service_account_file(
            SHEET_KEY, scopes=["https://www.googleapis.com/auth/spreadsheets"])
        sh = gspread.authorize(creds).open_by_key(SHEET_ID)
        try:
            ws = sh.worksheet(SHEET_TAB)
        except Exception:
            ws = sh.add_worksheet(title=SHEET_TAB, rows=2000, cols=len(SHEET_HEADER))
        if not ws.get_all_values():
            ws.append_row(SHEET_HEADER, value_input_option="USER_ENTERED")
        ws.append_rows(rows, value_input_option="USER_ENTERED")
        print("  logged %d finished item(s) to the sheet" % len(rows))
        return True
    except Exception as e:
        print("  ! sheet append failed (will retry next run): %s" % e)
        return False


def retire_finished(cards):
    """Drop cards that have been done long enough, and remember the rest."""
    ledger = load_ledger()
    now = datetime.now(timezone.utc)
    stamp = now.isoformat(timespec="seconds")
    kept, retired = [], 0

    for c in cards:
        e = ledger.setdefault(c["id"], {"firstSeen": stamp, "doneAt": None, "sheeted": False})
        if c["col"] == "done":
            if not e.get("doneAt"):
                e["doneAt"] = stamp
            done_at = datetime.fromisoformat(e["doneAt"])
            c["doneISO"] = done_at.astimezone().strftime("%Y-%m-%d")
            age = (now - done_at).days
            if age >= ARCHIVE_KEEP_DAYS:
                retired += 1
                continue                    # old enough to drop entirely
            if age >= RETIRE_AFTER_DAYS:
                retired += 1                # off the board, still in the Archive
        else:
            e["doneAt"] = None          # reopened in Slack — treat it as live again
        # Time spent waiting on someone else, accumulated a run at a time.
        # This is the number worth watching: the three items with real durations
        # were all slow because of waiting, not because the work was hard.
        was_blocked = e.get("blockedSince")
        if c["col"] == "blocked":
            if not was_blocked:
                e["blockedSince"] = stamp
        elif was_blocked:
            since = datetime.fromisoformat(was_blocked)
            e["blockedDays"] = e.get("blockedDays", 0) + max(0, (now - since).days)
            e["blockedSince"] = None

        blocked = e.get("blockedDays", 0)
        if c["col"] == "blocked" and e.get("blockedSince"):
            blocked += max(0, (now - datetime.fromisoformat(e["blockedSince"])).days)
        if blocked:
            c["blockedDays"] = blocked

        c["firstSeen"] = e["firstSeen"]
        kept.append(c)

    # Anything newly finished goes to the year log exactly once. Only mark it
    # written after the append succeeds, so a failure simply retries next run.
    pending, pending_ids = [], []
    for c in cards:
        e = ledger.get(c["id"], {})
        if c["col"] != "done" or e.get("sheeted"):
            continue
        opened = datetime.fromisoformat(e.get("firstSeen") or stamp)
        closed = datetime.fromisoformat(e.get("doneAt") or stamp)
        pending.append([
            closed.astimezone().strftime("%Y-%m-%d"),
            c["title"],
            c.get("tag", ""),
            "P%d" % c.get("p", 3),
            max(0, (closed - opened).days),
            e.get("blockedDays", 0),
            (c.get("refs") or [{}])[0].get("u", ""),
        ])
        pending_ids.append(c["id"])
    if pending and sheet_rows(pending):
        for cid in pending_ids:
            ledger[cid]["sheeted"] = True

    save_ledger(ledger)
    if retired:
        print("  %d finished card(s) moved to the Archive tab" % retired)
    return kept


def schedule_minutes():
    """How often this job is actually scheduled, read from the LaunchAgent.

    This used to be hardcoded to 5. When the schedule was changed to hourly the
    board kept expecting a run every 5 minutes, so it sat on amber "stale" for
    about 40 minutes of every hour while the sync was in fact perfectly healthy.
    Reading the real number means the two can never disagree again.
    """
    try:
        with open(PLIST_PATH, encoding="utf-8") as f:
            txt = f.read()
        m = re.search(r"<key>StartInterval</key>\s*<integer>(\d+)</integer>", txt)
        if m:
            return max(1, round(int(m.group(1)) / 60))
    except Exception:
        pass
    return 5


def write_health(ok, started, cards=0, open_n=0, done_n=0, stamped=0, error=None):
    """Record every run, successful or not, so the board can show what happened.

    A run that dies before this writes nothing at all — the board treats a stale
    timestamp as a failure, which is what catches the sync silently not running.
    """
    if DRY:
        return
    try:
        os.makedirs(BOARD_DIR, exist_ok=True)
        with open(HEALTH_PATH, "w", encoding="utf-8") as f:
            json.dump({
                "ranAt": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "ok": bool(ok),
                "error": error,
                "cards": cards,
                "open": open_n,
                "done": done_n,
                "stamped": stamped,
                "seconds": round(time.time() - started, 1),
                "everyMinutes": schedule_minutes(),
            }, f, indent=1)
    except Exception as e:
        print("  ! could not write health file: %s" % e)


def main():
    cards, seen = [], set()

    for m in search("hasmy::todo:"):
        c = card_from(m, "todo")
        if c and c["id"] not in seen:
            seen.add(c["id"])
            cards.append(c)

    if MENTIONS:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=MENTION_DAYS)).strftime("%Y-%m-%d")
        for m in search("<@%s> after:%s" % (ME, cutoff)):
            channel = m.get("channel", {}).get("id", "")
            ts = m.get("ts", "")
            if m.get("user") == ME:
                continue                                # my own messages
            if "slack-%s-%s" % (channel, ts.replace(".", "")) in seen:
                continue                                # already captured via :todo:
            reacts = reactions_on(channel, ts)
            if reacts is None or i_reacted(reacts):
                continue                                # I already engaged with it
            if answered_by_me(channel, ts, m.get("thread_ts")):
                continue                                # I already replied
            c = card_from(m, "mention")
            if c and c["id"] not in seen:
                seen.add(c["id"])
                cards.append(c)

    notion_found = notion_cards()
    for c in notion_found:
        if c["id"] not in seen:
            seen.add(c["id"])
            cards.append(c)
    if notion_found:
        print("  %d action item(s) from Notion meeting notes" % len(notion_found))

    # Read each candidate and decide whether it is genuinely work. Crude filters
    # cannot tell a high-five from a request; this can.
    verdicts = qualify([
        {"id": c["id"], "source": c["_kind"],
         "from": (c.get("who") or ["meeting notes"])[0], "text": c["_text"],
         "conversation": c.get("_ctx", "")}
        for c in cards
    ])
    if verdicts:
        kept = []
        for c in cards:
            v = verdicts.get(c["id"])
            if v is None:
                kept.append(c)
                continue
            if not v.get("keep", True):
                print("  dropped: %s — %s" % (c["title"][:48], v.get("reason", "")))
                continue
            if v.get("title"):
                c["title"] = v["title"]
            if v.get("priority"):
                c["p"] = int(v["priority"])
            if v.get("ask"):
                c["why"] = "<b>The ask:</b> %s" % v["ask"]
            elif v.get("reason"):
                c["why"] += " <b>Read as:</b> %s." % v["reason"]
            kept.append(c)
        cards = kept

    before = sum(1 for c in cards if c["_captured"])
    # Completions made on the board itself, mirrored into Slack and honoured here.
    st = board_state()
    if st:
        slack_mark_done(cards, st)
        done_on_board = finished_ids(st)
        for c in cards:
            if c["id"] in done_on_board:
                c["col"] = "done"
        # A duplicate folded into another card is the same piece of work, so the
        # year log must not gain a second row for it. Mark it as already written
        # rather than skipping it, so it also stops being counted as open.
        merged = st.get("merged") or {}
        if merged:
            led = load_ledger()
            touched = False
            for child in merged:
                if child in done_on_board and not led.setdefault(child, {}).get("sheeted"):
                    led[child]["sheeted"] = True
                    led[child]["mergedInto"] = merged[child]
                    touched = True
            if touched:
                save_ledger(led)

    cards = retire_finished(cards)

    for c in cards:
        stamp(c)
    notion_check_back(cards)
    STATS["stamped"] = len(cards) - before

    public = [{k: v for k, v in c.items() if not k.startswith("_")} for c in cards]
    week = agenda()
    payload = {
        "generatedAt": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "cards": public,
        "agenda": week,
        "briefings": briefings(public),
        "meetingsWithoutNotes": [
            {"day": m["day"], "time": m["time"], "title": m["title"], "url": m["url"]}
            for m in meetings_without_notes(agenda(days_back=14))
        ],
    }

    todo = sum(1 for c in public if c["col"] == "todo")
    done = sum(1 for c in public if c["col"] == "done")
    print("%d cards — %d open, %d closed by checkmark" % (len(public), todo, done))
    for c in public:
        print("  [%s] P%d %s" % (c["col"], c["p"], c["title"][:64]))

    if DRY:
        print("\n--dry-run: nothing written, nothing stamped")
        return

    os.makedirs(BOARD_DIR, exist_ok=True)
    with open(OUT_PATH, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=1, ensure_ascii=False)
    print("wrote %s" % OUT_PATH)

    # Write health BEFORE deploying, or the copy that goes up is the previous
    # run's and the board's clock is permanently one run behind — which is what
    # made a healthy board read as stale. If the deploy then fails, the live
    # timestamp simply stops advancing, which is exactly the staleness signal
    # the strip is built on.
    write_health(True, STARTED, cards=len(public),
                 open_n=todo, done_n=done, stamped=STATS["stamped"])

    if DEPLOY:
        # Deploys collide when a manual run overlaps the scheduled one, so try
        # twice before calling it a failure. Report what disco actually said
        # rather than guessing the cause — the first version of this blamed an
        # expired token for what was really a transient clash.
        r = None
        for attempt in (1, 2):
            r = subprocess.run([DISCO, "deploy", BOARD_DIR], capture_output=True, text=True)
            out = "\n".join(x for x in (r.stdout.strip(), r.stderr.strip()) if x)
            print(out)
            if r.returncode == 0:
                break
            if attempt == 1:
                print("  deploy failed, retrying once…")
                time.sleep(6)
        if r.returncode != 0:
            said = "\n".join(x for x in (r.stdout.strip(), r.stderr.strip()) if x)
            tail = " / ".join(said.splitlines()[-3:])[:220] or "no output"
            hint = " — run: disco login" if "sign in" in said.lower() else ""
            raise RuntimeError("disco deploy failed (exit %d): %s%s" % (r.returncode, tail, hint))

    return {"cards": len(public), "open_n": todo, "done_n": done}


if __name__ == "__main__":
    started = time.time()
    try:
        counts = main() or {}
        # main() already wrote health just before deploying, so the deployed copy
        # is this run's. Only write here for a dry run, which never deploys.
        if DRY:
            write_health(True, started, stamped=STATS["stamped"], **counts)
    except SlackError as e:
        write_health(False, started, error=str(e))
        print("todo_sync: %s" % e, file=sys.stderr)
        raise SystemExit(1)
    except Exception as e:
        write_health(False, started, error="%s: %s" % (type(e).__name__, e))
        raise
