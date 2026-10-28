# Todo board — runbook

**Board:** https://disco.ticketswap.dev/todo-board/ (TicketSwap login)
**Diagnostics:** add `?debug` to that URL
**Year log:** the "Joris Todo Board data" sheet — `Sheet1` and `How long things take`

Everything below is on this Mac. No part of it runs in the cloud.

---

## What runs, and when

| Piece | Where | When |
|---|---|---|
| `todo_sync.py` | `~/todo-board/` | every 5 min via launchd |
| LaunchAgent | `~/Library/LaunchAgents/com.ticketswap.todo.sync.plist` | loads at login |
| Board page | `~/todo-board/board/` → Disco app `todo-board` | redeployed each run |
| Log | `~/todo-board/logs/todo_sync.log` | appended each run |

One run: read Slack `:todo:` reactions → read Notion meeting notes → read the
calendar → send new items to Claude for triage → write `slack.json` and
`health.json` → stamp `:todo_added:` in Slack → tick finished items back in
Notion → append finished work to the sheet → `disco deploy`.

```bash
cd ~/todo-board
python3.11 todo_sync.py --dry-run     # show what it would do, change nothing
python3.11 todo_sync.py --deploy      # full run
python3.11 todo_sync.py --deploy --mentions   # also capture unanswered @-mentions
```

Stop or restart the schedule:

```bash
launchctl bootout gui/$(id -u)/com.ticketswap.todo.sync
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.ticketswap.todo.sync.plist
```

---

## Credentials — what exists and where

All secrets live in `~/Documents/scripts/ts-compliance/`. That folder **is a git
repo with a GitHub remote**; `.env` and `*service-account*.json` are gitignored
and mode 600. Check before adding anything new there.

| Credential | Stored as | Renew by |
|---|---|---|
| Slack user token | `SLACK_TODO_TOKEN` in `.env` | reinstall the app at api.slack.com/apps |
| Notion integration | `NOTION_TOKEN` in `.env` | it doesn't expire; re-share pages if access changes |
| Google service account | `todo-board-service-account.json` | new key in Cloud console, same path, `chmod 600` |
| Disco CLI token | login Keychain, service `disco-cli` | `disco login` — **expires roughly weekly** |

The Google service account (`todoboardjoris@…`) needs **two** grants per
resource: the API enabled in the Cloud project, *and* the sheet/calendar shared
with its email address. Enabling the API is not access to your data.

Check Disco token expiry: `disco whoami`. It reports only — it does not renew.

---

## When something looks wrong

**Start with the health strip on the board.** Green = a good run recently.
Amber = no successful run in 20 minutes. Red = the last run failed, with the
reason. A dying job writes nothing at all, so a stale timestamp *is* the alarm.

| Symptom | Cause | Fix |
|---|---|---|
| Red: `disco deploy failed … sign in required` | Disco token lapsed | `disco login` |
| Red: `disco deploy failed (exit 1)` with other text | usually two deploys colliding | it retries once; ignore unless it repeats |
| Red: `SLACK_TODO_TOKEN …` | token missing or wrong type | must start `xoxp-`, not `xoxb-` |
| Red: `missing_scope` | app reinstalled without a scope | reinstall from `slack-app/manifest.json` |
| Amber, never goes green | job not running | `launchctl print gui/$(id -u)/com.ticketswap.todo.sync` |
| No Notion items | database no longer shared | Notion → the database → ••• → Connections |
| Agenda empty | calendar sharing revoked, or API disabled | see the two-grants note above |
| Nothing appended to the sheet | only Slack-closed items append | close with ✅ in Slack, not by ticking on the board |

**"Cards have disappeared."** Almost certainly filters, not data loss. The amber
bar above the tabs says how many are hidden and why; **Show everything** clears
the lot. `?debug` gives the real counts. The ledger records every id it has ever
seen, so it can prove whether a card ever existed.

**"My edits are gone."** Press **Undo** — the last 20 states are kept locally.
Shared state also snapshots to `board_state_history` every 10 minutes, newest 24
kept. Press **Export** any time you want your own copy.

---

## Behaviour worth remembering

- A card only leaves for good when you **remove the `:todo:` reaction in Slack**.
  The `×` on the board hides it for you only.
- A ✅ from *anyone* on the original message finishes the card.
- Finished cards stay in Shipped for 7 days, then move to the **Archive** tab —
  they are not deleted. They drop out of the data after 180 days; the sheet is
  the permanent record.
- Triage rewrites titles using the surrounding conversation, so the card says
  what to do rather than quoting the message. The original text is the first
  checklist item, and every card links back to the source.
- **Merging duplicates.** The same job often arrives twice — once from a Slack
  `:todo:`, once from the action items in the meeting where it was agreed. Click
  **⧉** on the duplicate, then **⧉** on the card you want to keep. The survivor
  carries *both* source links, both sets of people, the combined checklist, the
  more urgent priority and the earlier due date. Nothing is deleted: the card
  shows "Merged in 1 card" with a **↩** to split it back out.
  - When the merged card is finished, **both** Slack messages get `:todo_done:`.
  - The year log gets **one** row, not two — the folded duplicate is marked as
    already written so the same work is never counted twice.
  - The board flags a pair as a possible duplicate only when the titles share
    rare words. It finds nothing in the current board because nothing there is
    actually a duplicate; it stays quiet until something really does arrive twice.
  - Splitting back out leaves the absorbed checklist items on the survivor — they
    may have been ticked, so they are not removed. Delete them with `×` if unwanted.
- Mentions capture is **off**. Turn it on with `--mentions` if you want it.
- Triage costs a few cents a month — verdicts are cached per message id, so a run
  with nothing new makes no API call at all.

---

## Known bug

**Editing a card's Finished/Due date.** Dates are set through a prompt dialog
because three attempts at an inline date picker failed — the card's drag
handling appears to swallow the interaction. If the prompt ever stops appearing,
the diagnosis needs a console error from the page; do not fix it blind.

---

## Files

| Path | What |
|---|---|
| `todo_sync.py` | the whole sync |
| `guide/index.html` | the one-page guide (Disco app `todo-board-guide`) |
| `docs/BUILD_YOUR_OWN.md` | how to build your own board |
| `board/index.html` | the board (single file) |
| `board/slack.json` | cards + agenda, rewritten each run |
| `board/health.json` | last run status |
| `logs/todo_sync_ledger.json` | first-seen / finished / waiting-days / written-to-sheet, per card |
| `logs/todo_sync_cache.json` | triage verdicts, cached by message id |
| `slack-app/manifest.json` | recreate the Slack app from this |
| `BUILD_YOUR_OWN_TODO_BOARD.md` | the brief to hand a colleague |
