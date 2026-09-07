"""Back up the library, and export it in a form Notion can import.

    python backup.py            # snapshot + csv, prune old ones
    python backup.py --export-only out.csv

Writes TWO things per run, on purpose:

  .db   an exact snapshot. restore = copy it back over shabads.db.
  .csv  the same data as plain text, with the columns the Notion table uses.

The csv is the more important of the two. A binary snapshot is worthless if
SQLite, this app, or Python ever stop working for you; a csv is readable in
thirty years by anything, and can be dragged straight back into Notion.

What's actually irreplaceable here is small (CLAUDE.md §5): status, rarity,
genre, speed, notes. Gurbani text and vectors are re-fetchable, so it doesn't
matter that the csv omits them.
"""

import argparse
import csv
import io
import os
import sqlite3
import sys
from datetime import datetime

sys.stdout.reconfigure(encoding="utf-8")

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)          # scripts live in a subfolder; data lives at the root
DB_PATH = os.path.join(ROOT, "shabads.db")
BACKUP_DIR = os.path.join(ROOT, "backups")

# Column order matches the original Notion export, so a re-import needs no
# fiddling. Extras are appended after those.
#
# Account leads, because every account's library is in the one csv. Splitting
# into a file each would mean a restore had to find and reassemble them, and
# the whole point of the csv is that it survives everything -- including this
# program.
NOTION_COLUMNS = ["Account", "Shabad", "Genre", "Rarity", "Speed", "Status",
                  "Notes", "Ang", "Raag", "Writer", "BaniDbShabadId", "Interested"]

UNTAGGED = "Not chosen"


def rows_for_export(conn):
    users = {r["id"]: r["username"] for r in conn.execute(
        "SELECT id, username FROM users")} or {None: ""}

    tags = {}
    for t in conn.execute(
            "SELECT user_id, shabad_id, kind, value FROM tags ORDER BY value"):
        tags.setdefault((t["user_id"], t["shabad_id"]), {}) \
            .setdefault(t["kind"], []).append(t["value"])

    # Shortlists are working state rather than description, but they're still
    # something only I can produce -- a shortlist built the night before a
    # program is worth a column. Missing on databases older than the deck.
    try:
        shortlisted = {(r["user_id"], r["shabad_id"]) for r in conn.execute(
            "SELECT user_id, shabad_id FROM shortlist WHERE list = 'Interested'")}
    except sqlite3.OperationalError:
        shortlisted = set()

    out = []
    for s in conn.execute(
            """SELECT us.user_id, s.id, s.source_line, s.ang, s.raag_en, s.writer,
                      s.banidb_shabad_id, us.rarity, us.status, us.notes
               FROM user_shabads us JOIN shabads s ON s.id = us.shabad_id
               ORDER BY us.user_id, s.id"""):
        key = (s["user_id"], s["id"])
        mine = tags.get(key, {})

        def joined(kind):
            vals = [v for v in mine.get(kind, []) if v != UNTAGGED]
            return ", ".join(vals)      # "Not chosen" was a blank cell in Notion

        out.append({
            "Account": users.get(s["user_id"], s["user_id"]),
            "Shabad": s["source_line"],
            "Genre": joined("genre"),
            "Rarity": s["rarity"] or "",
            "Speed": joined("speed"),
            "Status": s["status"] or "",
            "Notes": s["notes"] or "",
            "Ang": s["ang"] or "",
            "Raag": s["raag_en"] or "",
            "Writer": s["writer"] or "",
            "BaniDbShabadId": s["banidb_shabad_id"] or "",
            "Interested": "Yes" if key in shortlisted else "",
        })
    return out


# What I'm memorising: one row per shabad. Keyed on BaniDbShabadId rather than
# the text -- that id is stable in BaniDB, so the list survives even a full
# rebuild of the library, which matching on wording would not.
#
# This used to be one row per LINE with SM-2 state -- level, ease, interval,
# due date. That scheduling layer was removed (it went unused), so there is far
# less to preserve: which shabads, and when each was last practised.
LEARNING_COLUMNS = ["Account", "BaniDbShabadId", "Shabad", "Lines", "Status",
                    "AddedAt", "LastPractised"]


def learning_rows(conn):
    """Empty list on databases predating the memorization feature."""
    try:
        cur = conn.execute("""
            SELECT u.username, s.banidb_shabad_id, s.source_line,
                   (SELECT COUNT(*) FROM lines l WHERE l.shabad_id = s.id),
                   lg.status, lg.added_at, lg.last_practised
            FROM learning lg
            JOIN shabads s ON s.id = lg.shabad_id
            LEFT JOIN users u ON u.id = lg.user_id
            ORDER BY lg.user_id, lg.added_at""")
    except sqlite3.OperationalError:
        return []
    return [{
        "Account": r[0] or "", "BaniDbShabadId": r[1] or "", "Shabad": r[2],
        "Lines": r[3], "Status": r[4] or "", "AddedAt": r[5] or "",
        "LastPractised": r[6] or "",
    } for r in cur]


def write_csv(rows, path, columns=NOTION_COLUMNS):
    # utf-8-sig: Excel shows Gurmukhi as mojibake without the BOM
    with io.open(path, "w", encoding="utf-8-sig", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=columns)
        w.writeheader()
        w.writerows(rows)


def snapshot(conn, path):
    """Use SQLite's backup API, not a file copy.

    A copy taken while something is mid-write can land a torn database on disk.
    .backup() takes a consistent snapshot even with the app running.
    """
    dest = sqlite3.connect(path)
    with dest:
        conn.backup(dest)
    dest.close()


def prune(keep):
    files = sorted(f for f in os.listdir(BACKUP_DIR) if f.endswith(".db"))
    for old in files[:-keep] if keep else []:
        stem = old[:-3]
        for suffix in (".db", ".csv", "-learning.csv"):
            p = os.path.join(BACKUP_DIR, stem + suffix)
            if os.path.exists(p):
                os.remove(p)
        print(f"  pruned {stem}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--keep", type=int, default=30, help="how many backups to retain")
    ap.add_argument("--export-only", metavar="PATH",
                    help="just write a Notion-ready csv here, no snapshot")
    args = ap.parse_args()

    if not os.path.exists(DB_PATH):
        sys.exit("shabads.db not found")

    conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    rows = rows_for_export(conn)
    learning = learning_rows(conn)

    if args.export_only:
        write_csv(rows, args.export_only)
        conn.close()
        print(f"exported {len(rows)} shabads -> {args.export_only}")
        return

    os.makedirs(BACKUP_DIR, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    db_out = os.path.join(BACKUP_DIR, f"shabads-{stamp}.db")
    csv_out = os.path.join(BACKUP_DIR, f"shabads-{stamp}.csv")
    learn_out = os.path.join(BACKUP_DIR, f"shabads-{stamp}-learning.csv")

    snapshot(conn, db_out)
    write_csv(rows, csv_out)
    # Months of daily practice live here and nothing can regenerate them, so they
    # get a plain-text copy of their own rather than only the binary snapshot.
    write_csv(learning, learn_out, LEARNING_COLUMNS)
    conn.close()

    edited = sum(1 for r in rows if r["Notes"])
    print(f"backed up {len(rows)} shabads ({edited} with notes)")
    print(f"  {os.path.relpath(db_out, ROOT)}   {os.path.getsize(db_out)/1e6:.1f} MB")
    print(f"  {os.path.relpath(csv_out, ROOT)}   {os.path.getsize(csv_out)/1e3:.0f} KB")
    print(f"  {os.path.relpath(learn_out, ROOT)}   "
          f"{os.path.getsize(learn_out)/1e3:.0f} KB   {len(learning)} lines in learning")
    prune(args.keep)
    print(f"\nrestore:  copy the .db back over shabads.db")
    print(f"to Notion: import the .csv as a new database")


if __name__ == "__main__":
    main()
