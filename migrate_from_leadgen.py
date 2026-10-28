#!/opt/homebrew/bin/python3.11
"""
One-off, on Joris's Mac: move the running todo sync from ~/ticketswap-leadgen to this repo.

Until 2026-10-28 the board lived inside the leadgen folder. Its state did too: the ledger
(first seen / finished / written-to-sheet per card) and the triage caches in
~/ticketswap-leadgen/logs/. Without them the next run would re-add every finished card to the
year sheet and re-triage everything with Claude. So this:

  1. copies todo_sync_* state from ~/ticketswap-leadgen/logs/ into ./logs/
     (and the last slack.json / health.json into ./board/), never overwriting
  2. points ~/Library/LaunchAgents/com.ticketswap.todo.sync.plist at this folder
  3. reloads the job

    ./migrate_from_leadgen.py           show what would happen
    ./migrate_from_leadgen.py --apply   do it (the plist is backed up first)
"""

import datetime
import glob
import os
import shutil
import subprocess
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
OLD = os.path.expanduser("~/ticketswap-leadgen")
PLIST = os.path.expanduser("~/Library/LaunchAgents/com.ticketswap.todo.sync.plist")
LABEL = "com.ticketswap.todo.sync"


def main():
    apply = "--apply" in sys.argv
    copies = [(p, os.path.join(ROOT, "logs", os.path.basename(p)))
              for p in sorted(glob.glob(os.path.join(OLD, "logs", "todo_sync*")))]
    copies += [(os.path.join(OLD, "todo-board", f), os.path.join(ROOT, "board", f))
               for f in ("slack.json", "health.json")
               if os.path.exists(os.path.join(OLD, "todo-board", f))]

    print("state to copy:")
    for src, dst in copies:
        print(f"  {src}  →  {dst}" + ("   (exists, kept)" if os.path.exists(dst) else ""))
    if not copies:
        print("  nothing found")

    if not os.path.exists(PLIST):
        print(f"\nSTOP — {PLIST} not found, nothing changed")
        return 1
    old = open(PLIST, encoding="utf-8").read()
    new = old.replace(f"{OLD}/todo_sync.py", f"{ROOT}/todo_sync.py").replace(OLD, ROOT)
    print("\nlaunchd:")
    if new == old:
        print("  nothing to change")
    for a, b in zip(old.splitlines(), new.splitlines()):
        if a != b:
            print(f"  - {a.strip()}\n  + {b.strip()}")

    if not apply:
        print("\nniets gewijzigd — draai met --apply om dit door te voeren")
        return 0

    os.makedirs(os.path.join(ROOT, "logs"), exist_ok=True)
    for src, dst in copies:
        if not os.path.exists(dst):
            shutil.copy2(src, dst)
    print(f"\n  ✓ {len(copies)} state files copied")
    if new != old:
        backup = os.path.join(ROOT, "logs", f"todo.sync.plist.{datetime.datetime.now():%Y%m%d_%H%M%S}.bak")
        shutil.copy2(PLIST, backup)
        open(PLIST, "w", encoding="utf-8").write(new)
        uid = os.getuid()
        subprocess.run(["launchctl", "bootout", f"gui/{uid}/{LABEL}"], capture_output=True)
        r = subprocess.run(["launchctl", "bootstrap", f"gui/{uid}", PLIST],
                           capture_output=True, text=True)
        print(f"  {'✓' if r.returncode == 0 else '✗'} {LABEL} reloaded"
              + (f": {r.stderr.strip()}" if r.returncode else ""))
        print(f"  backup: {backup}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
