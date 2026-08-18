"""Copy BaniDB's verse corpus out of the Docker MySQL into a local SQLite file.

Run this once (and again only when you want to refresh the corpus). After it
finishes, the app never touches Docker or the network -- search and add-shabad
both run against banidb.db.

Deliberately a SEPARATE file from shabads.db:
  banidb.db   ~85 MB, regenerable, never backed up
  shabads.db  tiny, irreplaceable, backed up nightly (CLAUDE.md §5/§8)

Usage:  ./start_local_banidb.sh   (container only, the Node API isn't needed)
        python extract_corpus.py
"""

import os
import sqlite3
import sys
import time

import pymysql

sys.stdout.reconfigure(encoding="utf-8")

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)          # scripts live in a subfolder; data lives at the root
OUT_PATH = os.path.join(ROOT, "banidb.db")

MYSQL = dict(host="127.0.0.1", port=3002, user="root", password="root",
             database="khajana_dev_khajana", charset="utf8mb4")

# The complete table. `Verse` holds only every 3rd verse in the dev image.
VERSE_TABLE = "VerseNoBisram"


def decode_letters(codes):
    """',103,107,066,066,' -> 'gkBB'

    BaniDB keeps first letters twice. FirstLetterEng is lowercased, which
    collapses ਭ and ਬ both to 'b' -- so searching can't tell them apart.
    FirstLetterStr keeps the GurbaniAkhar codes, where ਭ is 66 ('B') and ਬ is
    98 ('b'). Decoding that preserves the distinction, so 'gkBBj' means
    something different from 'gkbbj'.
    """
    return "".join(chr(int(p)) for p in (codes or "").split(",") if p.strip().isdigit())

SCHEMA = """
PRAGMA journal_mode = WAL;

CREATE TABLE verses (
  verse_id        INTEGER PRIMARY KEY,   -- BaniDB's stable id
  shabad_id       INTEGER NOT NULL,
  line_no         INTEGER NOT NULL,      -- position within the shabad, 1-based
  gurmukhi        TEXT NOT NULL,
  transliteration TEXT,
  english         TEXT,
  teeka           TEXT,
  first_letters   TEXT,                  -- 'gkBBj' -- what you type to search.
                                         -- case matters: B is ਭ, b is ਬ
  ang             INTEGER,
  source_id       TEXT,
  raag_en         TEXT,
  raag_pa         TEXT,
  writer          TEXT,
  source_en       TEXT,
  source_pa       TEXT
);
"""

INDEXES = """
CREATE INDEX idx_first_letters ON verses(first_letters);
CREATE INDEX idx_shabad        ON verses(shabad_id, line_no);
CREATE INDEX idx_gurmukhi      ON verses(gurmukhi);
"""

QUERY = f"""
SELECT s.ShabadID, v.ID, v.GurmukhiUni, v.Transliteration, v.English, v.PunjabiUni,
       v.FirstLetterStr, v.PageNo, v.SourceID,
       r.RaagEnglish, r.RaagUnicode, w.WriterEnglish,
       src.SourceEnglish, src.SourceUnicode
FROM Shabad s
JOIN {VERSE_TABLE} v ON v.ID = s.VerseID
LEFT JOIN Raag   r   ON r.RaagID    = v.RaagID
LEFT JOIN Writer w   ON w.WriterID  = v.WriterID
LEFT JOIN Source src ON src.SourceID = v.SourceID
ORDER BY s.ShabadID, v.ID
"""


def main():
    if os.path.exists(OUT_PATH):
        print(f"{os.path.basename(OUT_PATH)} exists -- deleting and rebuilding "
              "(it's fully regenerable)")
        os.remove(OUT_PATH)
    for suffix in ("-wal", "-shm"):
        stale = OUT_PATH + suffix
        if os.path.exists(stale):
            os.remove(stale)

    try:
        my = pymysql.connect(**MYSQL)
    except Exception as exc:
        sys.exit(f"Can't reach BaniDB MySQL on port {MYSQL['port']}: {exc}\n"
                 f"Start it with ./start_local_banidb.sh")

    # streaming cursor: 140k rows of text is too much to buffer in one go
    cur = my.cursor(pymysql.cursors.SSDictCursor)
    cur.execute(QUERY)

    out = sqlite3.connect(OUT_PATH)
    out.executescript(SCHEMA)

    started = time.time()
    batch, total = [], 0
    current_shabad, line_no = None, 0

    for r in cur:
        # line_no restarts at 1 for each shabad; rows arrive grouped and ordered
        if r["ShabadID"] != current_shabad:
            current_shabad, line_no = r["ShabadID"], 0
        line_no += 1

        batch.append((
            r["ID"], r["ShabadID"], line_no,
            r["GurmukhiUni"] or "", r["Transliteration"], r["English"],
            r["PunjabiUni"], decode_letters(r["FirstLetterStr"]),
            r["PageNo"], r["SourceID"],
            r["RaagEnglish"], r["RaagUnicode"], r["WriterEnglish"],
            r["SourceEnglish"], r["SourceUnicode"],
        ))

        if len(batch) >= 5000:
            out.executemany("INSERT OR IGNORE INTO verses VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", batch)
            total += len(batch)
            batch = []
            print(f"\r  {total:,} verses...", end="", flush=True)

    if batch:
        out.executemany("INSERT OR IGNORE INTO verses VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", batch)
        total += len(batch)

    out.commit()
    print(f"\r  {total:,} verses written")

    print("  building indexes...")
    out.executescript(INDEXES)
    out.commit()

    n_shabads = out.execute("SELECT COUNT(DISTINCT shabad_id) FROM verses").fetchone()[0]
    by_source = out.execute(
        "SELECT source_en, COUNT(*) FROM verses GROUP BY source_en ORDER BY 2 DESC"
    ).fetchall()
    out.close()
    cur.close()
    my.close()

    size = os.path.getsize(OUT_PATH) / 1_000_000
    print(f"\ndone in {time.time() - started:.0f}s -- {size:.0f} MB, "
          f"{total:,} verses across {n_shabads:,} shabads")
    for name, n in by_source:
        print(f"   {(name or '(unknown)'):<28} {n:>7,}")


if __name__ == "__main__":
    main()
