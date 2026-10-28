# Build me a personal Todo board

Paste this whole file to Claude Code as your first message. It contains no
credentials — every account-specific value is something Claude will ask you for
or find for you.

---

## What I want

A single web page that shows everything I'm working on, so my manager can open a
link instead of asking me. Cards move between columns, carry a priority, and each
one links back to wherever it came from.

The important part is that it **fills itself in**. I should not maintain it by
hand. Specifically:

- I react to a Slack message with a **`:todo:`** emoji → it becomes a card
- Anyone puts a **✅** on that same message → the card is finished
- **Action items in my meeting notes** become cards
- My **calendar** fills a small agenda strip
- Finished work is appended to a **spreadsheet**, one row each, so I have a record
  of what I got done this year

A scheduled job does all of this every few minutes, unattended.

## Build it in this order

Each phase is useful on its own — get one working and visible before starting the
next. Do not build the whole thing before showing me anything.

1. **The board, with my real work on it.** Static data is fine at first. Ask me
   what I'm working on, or better: find it. Look for a message where I've written
   out my projects to my manager, my meeting notes, my calendar. Read those and
   populate the board from what you find, then show me and let me correct it.
2. **Host it somewhere I can send a link.** Anything that serves a static file
   behind my company login. Ask me what my company uses before assuming.
3. **Slack capture** — the `:todo:` and ✅ loop.
4. **The scheduled job**, so it runs without me.
5. **Meeting notes**, then **calendar**, then the **spreadsheet log**.

## Things to work out with me before writing code

Ask me for these; do not guess, and do not put any of them in a file that a git
repo can see:

- Where my secrets already live (I probably have a `.env` or a keychain entry
  already — reuse the pattern rather than inventing a new one)
- Where static sites get hosted at my company
- My Slack user ID, the exact emoji name I'll use, and whether my workspace lets
  people install Slack apps without an admin approving it
- Where my meeting notes live, and whether action items are checkboxes, a
  database column, or just prose
- Whether I want a dedicated spreadsheet or an existing one

## Traps — these all cost real time to discover

**Slack needs a *user* token, not a bot token.** `search:read` only exists as a
user scope, and personal todos are mostly in DMs, which a bot user cannot see at
all. Create an app from a manifest (not a template — the AI-agent and starter
templates scaffold a bot, events and a server you don't need), pick **internal
integration secret** rather than OAuth, and copy the token beginning `xoxp-`, not
`xoxb-`. Minimum scopes: `search:read`, `reactions:read`, `reactions:write`. Add
history scopes only if you want the "did I already reply" filter.

**Do not capture @-mentions naively.** A first attempt at this produced 67 cards
from 4 real tasks. Mentions mean "someone wants your attention", not "you took
this on". If you do capture them: bound them by date, skip anything I've already
reacted or replied to, and default them off.

**Add a triage step, and make it the thing that earns its place.** Send each
candidate to Claude and ask a single question: is this genuinely a piece of work
for this person? Drop banter, praise, FYIs, broadcasts and bare mentions; rewrite
what survives into a short imperative title; suggest a priority. On real data this
went from 67 candidates to 6. Cache the verdict per message id so a job running
every five minutes costs nothing when nothing has changed — that keeps it to a
few cents a month.

**Give triage the surrounding conversation, not just the flagged message.** The
message someone flags is usually the *end* of an exchange — "sure, I can do
that" — and the actual request is a turn or two earlier. Fetch a few messages
either side (or the thread) and pass them in. The difference is between a card
titled *"Ik wou een uitdraai maken van alle aa/a…"* and one titled *"Pull HubSpot
data for AA/A/B partners for Hidde"*. Ask the model for a one-line "what is
actually being asked, and by whom" alongside the title.

**Batch the triage calls, five or so at a time.** Sending everything in one
request outgrew the reply budget the moment context was added, and the truncated
reply meant *every* verdict was silently lost — the whole board fell back to raw
message text. Small batches fail alone.

**Keep the source link prominent.** Triage rewrites titles for readability, which
can lose the sender's exact words. Every card needs an obvious way back to the
original message, not a small chip in a footer.

**Give every card a stable id, derived from content and never from position.**
Numbering items by their index in a meeting note seems fine until someone adds a
line at the top: every card below it gets a new id, and its priority, ticks and
dates orphan. Hash the text instead.

**Notion has two traps, both silent.** Action items are often nested *inside* a
child block, so reading a page's top-level children returns one block and finds
nothing — you must recurse. And Notion's filter and search indexes lag freshly
shared content, so a server-side date filter will silently skip pages that an
unfiltered fetch returns. Fetch everything and filter client-side. Also: a Notion
integration only ever sees pages explicitly connected to it, and access cascades
from a parent — so connect it to the *parent database*, not to individual notes.

**Google service accounts need two separate grants.** Enabling the API in the
cloud project is not access to my data. The calendar or sheet also has to be
shared with the service account's own email address. The error for the first is
`accessNotConfigured`; the second is a plain 403. They are different fixes.

**Use launchd, not cron, on a Mac.** cron runs outside the GUI login session, so
it cannot read the login keychain and cannot post notifications. A job that
depends on either will fail every single run and tell nobody — that is exactly how
a previous sync of mine died silently for eight days.

**Make silent failure impossible.** Have every run write a small health file with
a timestamp, an ok flag and the error text, and show it on the board: green when
fresh, amber when the last good run is older than a few intervals, red with the
reason when it failed. A job that dies writes nothing at all, so *staleness is
the signal* — that is the failure you are actually defending against.

**Stop it re-adding things.** A `:todo:` reaction stays on a message forever, so
a naive job re-creates that card every run for the rest of the year. Keep a small
ledger keyed by message id recording first-seen, finished-at, and whether it has
been written to the spreadsheet. Retire finished items from the board after about
a week — but move them to an archive view rather than deleting them. Never mark
something as written to the sheet until the write actually succeeded, so a failure
retries instead of losing the row.

**Decide where state lives, early.** If card moves are kept in browser storage,
my manager opening the link sees a different board from mine, and nothing I tick
can reach the spreadsheet. If the host offers any shared store, use it, and keep
browser storage only as an offline cache.

**Shared state must merge, never replace — and this one actually cost me a day's
edits.** The obvious implementation loads the shared blob and assigns it over
local state. Then any browser that opens the board holding an emptier copy adopts
it, writes it back as the shared truth, and everyone's work is gone. Merge: take
the union, let local win ties, and refuse to publish an empty state over a
populated one. Keep timestamped snapshots too, so the *next* unforeseen bug is
recoverable rather than final.

**Never let a filter hide cards silently.** A "top priorities only" toggle, a
"hide finished" toggle and an archive rule are each reasonable; together they can
hide most of the board with nothing on screen saying so, and the user reasonably
concludes their data was deleted. Always show "showing 12 of 41 — here is why",
with one button that clears everything.

**Build undo and export before you build anything clever.** Twenty local
snapshots and a "copy my state as JSON" button are an hour's work and turn any
future data-loss bug from unrecoverable into an inconvenience.

**Add a `?debug` view.** A block showing card counts by source and column, which
filters are on, how many edits exist, and whether shared state is connected. When
something looks wrong the user pastes one block instead of describing symptoms —
and you stop guessing at a screen you cannot see.

**Watch out for `draggable`.** If cards are draggable, the browser's drag gesture
swallows clicks and caret placement on controls inside them — text you meant to
edit, date pickers that never open. Put the drag on a small grip handle instead of
the whole card. (I still have an unfixed date-picker bug from exactly this; a
plain prompt dialog was the reliable workaround.)

**Check my secrets are actually ignored.** Two real findings while building mine:
a `.env` holding seven live tokens sat in a repo with a GitHub remote and was not
gitignored, and a service-account key was world-readable at mode 644. Neither had
been committed, but one `git add -A` would have published them. Check `git
check-ignore`, check the file mode, and check `git log` to see whether anything is
already in history — if it is, gitignore is not enough and the credential needs
rotating.

## Design choices worth keeping

- **Warn before something is late, not after.** A red "4 days late" badge is
  useless; amber "due tomorrow" is not.
- **Track waiting separately from working.** The most interesting thing in my own
  data: every item that took a long time was slow because it was waiting on
  someone, not because the work was hard. "Median 6 days, most of it waiting" is
  a far better thing to bring to a review than a count of tickets closed.
- **Show me meetings that produced no notes.** Nothing otherwise tells you when a
  meeting left no record, and you find out weeks later.
- **Resist adding more capture sources.** Email, ticketing systems, task
  managers — the board's value is that it is short enough to look at. More inputs
  means more for triage to reject, and the failure mode is a board nobody opens.

## How I want you to work

- Show me something on screen early, then iterate. Don't disappear for twenty
  steps.
- **Check the page, not just the data.** The most expensive mistakes here came
  from verifying that the JSON was correct and concluding the board was fine.
  Those are different claims. If you cannot load the page, say so every time you
  make a change you have not seen.
- **Don't fix a UI bug you cannot reproduce.** Two blind attempts at the same
  symptom is the limit; after that, ask for a console error or use a cruder
  mechanism that cannot fail the same way.
- Verify things rather than asserting them. Run the script, load the page, read
  the response — and if you can't verify something, say so plainly instead of
  implying you did.
- When you find a real problem in my setup that I didn't ask about — an exposed
  key, a job that has been failing, a stale link — tell me.
- Use my own words on the cards where you have them. My phrasing of my projects
  is better than your summary of them.
