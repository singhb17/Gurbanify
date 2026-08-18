"""Import the Notion CSV export into SQLite, enriching each row from BaniDB.

Reads BaniDB's MySQL directly (the local Docker container) rather than going
through their HTTP API. Two reasons:

  1. api.banidb.com rate limits by returning HTTP 200 with zero results -- not a
     429 -- so a throttled request is indistinguishable from "line not found".
     That silently writes off good shabads as failures.
  2. The local API reads the `Verse` table, which in the dev image holds only
     every 3rd verse (47,526 rows). The COMPLETE text lives in `VerseNoBisram`
     (142,428 rows, 60,403 of them SGGS). Querying SQL directly gets the whole
     thing, instantly, with no rate limit.

Prerequisites: ./start_local_banidb.sh  (only the container is needed, not the
Node API), and `pip install pymysql`.

Usage:
    python import_shabads.py                # dry run, first 20 rows
    python import_shabads.py --limit 20 --write
    python import_shabads.py --limit 0 --write     # everything
"""

import argparse
import csv
import io
import os
import sqlite3
import sys
from collections import OrderedDict

import pymysql

sys.stdout.reconfigure(encoding="utf-8")

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)          # scripts live in a subfolder; data lives at the root
CSV_PATH = os.path.join(ROOT, "data", "notion-export.csv")
DB_PATH = os.path.join(ROOT, "shabads.db")
SCHEMA_PATH = os.path.join(ROOT, "schema.sql")
FAILURES_PATH = os.path.join(ROOT, "data", "import_failures.csv")
OVERRIDES_PATH = os.path.join(ROOT, "data", "manual_matches.csv")

MYSQL = dict(host="127.0.0.1", port=3002, user="root", password="root",
             database="khajana_dev_khajana", charset="utf8mb4")

UNTAGGED = "Not chosen"   # explicit tag for fields left blank on purpose

# VerseNoBisram carries one English translation and one Punjabi teeka per line,
# rather than the multi-source JSON blob the modern `Verse` table has. That's a
# real trade-off: complete coverage, but no choice of translator. See §13 --
# A/B testing translation sources means re-fetching from the live API later.
VERSE_TABLE = "VerseNoBisram"


# The same pairin haha is encoded two ways: the Notion export uses U+0A51
# (UDAAT), BaniDB stores VIRAMA + HA. Identical letter, different codepoints, so
# an exact match misses. Used ONLY to look a line up -- every character stored in
# the database still comes from BaniDB verbatim, nothing is rewritten (§12).
UDAAT = "ੑ"
VIRAMA_HA = "੍ਹ"


def match_key(text):
    """Canonical form for comparing two spellings of the same line."""
    return norm(text).replace(UDAAT, VIRAMA_HA)


def query_variants(line):
    """The spellings worth trying against BaniDB, most faithful first."""
    base = norm(line)
    out = [base]
    for alt in (base.replace(UDAAT, VIRAMA_HA), base.replace(VIRAMA_HA, UDAAT)):
        if alt not in out:
            out.append(alt)
    return out


class MatchError(Exception):
    def __init__(self, reason, candidates=()):
        super().__init__(reason)
        self.reason = reason
        self.candidates = list(candidates)


def decode_letters(codes):
    """',103,107,066,066,' -> 'gkBB'. See extract_corpus.decode_letters."""
    return "".join(chr(int(p)) for p in (codes or "").split(",") if p.strip().isdigit())


def norm(text):
    """Normalise whitespace.

    The Notion export separates words with NON-BREAKING SPACES (U+00A0), not
    plain spaces. They look identical and .strip() won't touch them, but an
    exact SQL match against BaniDB fails on every one. str.split() treats U+00A0
    as whitespace, so splitting and rejoining rewrites them to U+0020.

    This only ever touches the spaces between words -- no Gurmukhi character is
    added, removed or altered (CLAUDE.md §12).
    """
    return " ".join((text or "").split())


# --- BaniDB (MySQL) ---------------------------------------------------------

def load_overrides():
    """Lines I've resolved by hand, keyed by the CSV text.

    Two kinds, both recorded in manual_matches.csv:
      Ang     - the line genuinely appears in more than one shabad, and I picked
                which one by its ang.
      VerseID - BaniDB spells or splits the line differently to my note, so
                there's nothing to match on. Points straight at the verse.

    Keyed by text rather than CSV row number so it survives a fresh Notion export.
    """
    if not os.path.exists(OVERRIDES_PATH):
        return {}
    with io.open(OVERRIDES_PATH, encoding="utf-8") as fh:
        return {
            match_key(r["SourceLine"]): {
                "ang": int(r["Ang"]) if r.get("Ang") else None,
                "verse_id": int(r["VerseID"]) if r.get("VerseID") else None,
            }
            for r in csv.DictReader(fh)
        }


def find_shabad_id(cur, line, overrides):
    """Resolve a Gurmukhi line to exactly one BaniDB ShabadID."""
    ov = overrides.get(match_key(line))

    if ov and ov["verse_id"]:
        cur.execute("SELECT ShabadID FROM Shabad WHERE VerseID = %s", (ov["verse_id"],))
        row = cur.fetchone()
        if not row:
            raise MatchError(f"override verse {ov['verse_id']} maps to no shabad")
        return row["ShabadID"]

    ids = []
    for variant in query_variants(line):
        cur.execute(
            f"""SELECT DISTINCT s.ShabadID
                FROM {VERSE_TABLE} v JOIN Shabad s ON s.VerseID = v.ID
                WHERE v.GurmukhiUni = %s""",
            (variant,),
        )
        ids = [r["ShabadID"] for r in cur.fetchall()]
        if ids:
            break
    if not ids:
        raise MatchError("line not found in BaniDB")

    if len(ids) > 1:
        if not (ov and ov["ang"]):
            raise MatchError(f"line appears in {len(ids)} different shabads", ids[:5])
        # disambiguate by the ang I chose
        fmt = ",".join(["%s"] * len(ids))
        cur.execute(
            f"""SELECT DISTINCT s.ShabadID FROM Shabad s
                JOIN {VERSE_TABLE} v ON v.ID = s.VerseID
                WHERE s.ShabadID IN ({fmt}) AND v.PageNo = %s""",
            (*ids, ov["ang"]),
        )
        picked = [r["ShabadID"] for r in cur.fetchall()]
        if len(picked) != 1:
            raise MatchError(f"ang {ov['ang']} matched {len(picked)} of the candidates", ids)
        return picked[0]

    return ids[0]


def fetch_shabad(cur, shabad_id, source_line, verse_id=None):
    """Pull every verse of a shabad, plus its raag/writer/source metadata."""
    cur.execute(
        f"""SELECT v.ID, v.GurmukhiUni, v.English, v.PunjabiUni, v.Transliteration,
                   v.FirstLetterStr, v.PageNo, v.SourceID,
                   r.RaagEnglish, r.RaagUnicode,
                   w.WriterEnglish,
                   src.SourceEnglish, src.SourceUnicode
            FROM Shabad s
            JOIN {VERSE_TABLE} v ON v.ID = s.VerseID
            LEFT JOIN Raag   r   ON r.RaagID   = v.RaagID
            LEFT JOIN Writer w   ON w.WriterID = v.WriterID
            LEFT JOIN Source src ON src.SourceID = v.SourceID
            WHERE s.ShabadID = %s
            ORDER BY v.ID""",
        (shabad_id,),
    )
    rows = cur.fetchall()
    if not rows:
        raise MatchError(f"shabad {shabad_id} has no verses")

    verses = [
        {
            "line_no": i,
            "banidb_verse_id": r["ID"],
            "gurmukhi": r["GurmukhiUni"] or "",
            "transliteration_en": r["Transliteration"] or "",
            "translation_en": r["English"] or "",
            "teeka_pa": r["PunjabiUni"] or "",
            # decoded so case survives: B is ਭ, b is ਬ (see extract_corpus.py)
            "first_letters": decode_letters(r["FirstLetterStr"]),
        }
        for i, r in enumerate(rows, start=1)
    ]

    # Don't read metadata off rows[0]: verse 1 is the raag/mahalla header, which
    # carries no WriterID (and sometimes no raag). Take the first verse that
    # actually has a value for each field.
    def first(field):
        return next((r[field] for r in rows if r[field] not in (None, "")), None)

    # Which verse is the line I know this shabad by? Usually not verse 1, since
    # that's typically the raag/mahalla header. An override points at it
    # directly; otherwise match on text.
    if verse_id is not None:
        mine = next((v for v in verses if v["banidb_verse_id"] == verse_id), None)
    else:
        wanted = match_key(source_line)
        mine = next((v for v in verses if match_key(v["gurmukhi"]) == wanted), None)

    return {
        "banidb_shabad_id": shabad_id,
        "source_line_no": mine["line_no"] if mine else None,
        # BaniDB is the source of truth: store the line as they spell it, not as
        # I typed it into Notion. Falls back to my text only if nothing matched.
        "source_line": mine["gurmukhi"] if mine else norm(source_line),
        "ang": first("PageNo"),
        "raag_en": first("RaagEnglish"),
        "raag_pa": first("RaagUnicode"),
        "writer": first("WriterEnglish"),
        "source_en": first("SourceEnglish"),
        "source_pa": first("SourceUnicode"),
        "verses": verses,
    }


# --- CSV reading and de-duplication ----------------------------------------

def split_multi(value):
    return [v.strip() for v in (value or "").split(",") if v.strip()]


def load_rows(csv_path=None):
    """Read the CSV and merge duplicate lines, returning (rows, conflicts)."""
    with io.open(csv_path or CSV_PATH, encoding="utf-8-sig") as fh:
        raw = list(csv.DictReader(fh))

    merged = OrderedDict()
    conflicts = []

    for n, row in enumerate(raw, start=2):   # start=2: line 1 is the header
        key = norm(row["Shabad"])
        entry = merged.get(key)
        if entry is None:
            merged[key] = {
                "shabad": row["Shabad"],
                "genre": split_multi(row["Genre"]),
                "speed": split_multi(row["Speed"]),
                "rarity": row["Rarity"].strip(),
                "status": row["Status"].strip(),
                "rows": [n],
            }
            continue

        # duplicate: union the multi-valued tags, blank loses to populated
        entry["rows"].append(n)
        for field in ("genre", "speed"):
            for v in split_multi(row[field.capitalize()]):
                if v not in entry[field]:
                    entry[field].append(v)

        for field, col in (("rarity", "Rarity"), ("status", "Status")):
            new = row[col].strip()
            if not new:
                continue
            if not entry[field]:
                entry[field] = new
            elif entry[field] != new:
                conflicts.append((entry["shabad"], field, entry[field], new, entry["rows"]))

    return list(merged.values()), conflicts


# --- SQLite -----------------------------------------------------------------

def connect(write):
    conn = sqlite3.connect(DB_PATH if write else ":memory:")
    conn.execute("PRAGMA foreign_keys = ON")
    with io.open(SCHEMA_PATH, encoding="utf-8") as fh:
        conn.executescript(fh.read())
    migrate(conn)
    return conn


def migrate(conn):
    """Add columns introduced after a database was first created.

    CREATE TABLE IF NOT EXISTS silently does nothing on an existing table, so new
    columns would otherwise never appear. Once real metadata lives in here,
    rebuilding from scratch stops being an option -- so add, never recreate.
    """
    wanted = {
        "shabads": {"source_line_no": "INTEGER", "last_surfaced": "TEXT"},
        "lines": {"first_letters": "TEXT"},
    }
    added = []
    for table, columns in wanted.items():
        have = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
        for name, decl in columns.items():
            if name not in have:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")
                added.append(f"{table}.{name}")
                print(f"migrated: added {table}.{name}")
    # after the column is guaranteed to exist, on both fresh and migrated databases
    conn.execute("CREATE INDEX IF NOT EXISTS idx_lines_letters ON lines(first_letters)")
    conn.commit()

    if "lines.first_letters" in added:
        backfill_first_letters(conn)


def backfill_first_letters(conn):
    """Fill first_letters for lines imported before the column existed.

    Comes from banidb.db rather than the network -- every line already carries
    banidb_verse_id, so it's a straight lookup.
    """
    corpus = os.path.join(HERE, "banidb.db")
    if not os.path.exists(corpus):
        print("  (skipping first_letters backfill -- run extract_corpus.py, then re-run)")
        return
    conn.execute("ATTACH DATABASE ? AS corpus", (corpus,))
    with conn:
        conn.execute("""UPDATE lines SET first_letters = (
                          SELECT v.first_letters FROM corpus.verses v
                          WHERE v.verse_id = lines.banidb_verse_id)
                        WHERE first_letters IS NULL""")
    n = conn.execute("SELECT COUNT(*) FROM lines WHERE first_letters IS NOT NULL").fetchone()[0]
    total = conn.execute("SELECT COUNT(*) FROM lines").fetchone()[0]
    conn.execute("DETACH DATABASE corpus")
    print(f"  backfilled first_letters for {n}/{total} lines")


def already_imported(conn, banidb_shabad_id):
    return conn.execute(
        "SELECT 1 FROM shabads WHERE banidb_shabad_id = ?", (banidb_shabad_id,)
    ).fetchone() is not None


def insert_shabad(conn, row, fetched):
    """Write one shabad, its lines and its tags in a single transaction."""
    with conn:      # commits on success, rolls back on any exception
        cur = conn.execute(
            """INSERT INTO shabads
                 (banidb_shabad_id, source_line, source_line_no, ang,
                  raag_en, raag_pa, writer, source_en, source_pa, rarity, status,
                  is_user_added, imported_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,0,datetime('now'))""",
            (
                fetched["banidb_shabad_id"],
                fetched["source_line"],
                fetched["source_line_no"],
                fetched["ang"],
                fetched["raag_en"],
                fetched["raag_pa"],
                fetched["writer"],
                fetched["source_en"],
                fetched["source_pa"],
                row["rarity"] or None,
                row["status"] or None,
            ),
        )
        shabad_id = cur.lastrowid

        conn.executemany(
            """INSERT INTO lines
                 (shabad_id, line_no, banidb_verse_id, gurmukhi,
                  transliteration_en, translation_en, teeka_pa, first_letters)
               VALUES (?,?,?,?,?,?,?,?)""",
            [
                (shabad_id, v["line_no"], v["banidb_verse_id"], v["gurmukhi"],
                 v["transliteration_en"], v["translation_en"], v["teeka_pa"],
                 v["first_letters"])
                for v in fetched["verses"]
            ],
        )

        tag_rows = []
        for field, kind in (("genre", "genre"), ("speed", "speed")):
            values = row[field] or [UNTAGGED]     # blank was deliberate, tag it as such
            tag_rows += [(shabad_id, kind, v) for v in values]
        conn.executemany(
            "INSERT OR IGNORE INTO tags (shabad_id, kind, value) VALUES (?,?,?)", tag_rows
        )

    return shabad_id


# --- main -------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=20, help="0 = no limit")
    ap.add_argument("--write", action="store_true", help="write to shabads.db")
    ap.add_argument("--csv", default=None,
                    help="a Notion export to import (defaults to the original one). "
                         "Shabads already in the library are skipped, so you can "
                         "always hand it the whole export.")
    args = ap.parse_args()

    csv_path = args.csv or CSV_PATH
    if not os.path.exists(csv_path):
        sys.exit(f"csv not found: {csv_path}")

    try:
        my = pymysql.connect(**MYSQL)
    except Exception as exc:
        sys.exit(f"Can't reach BaniDB MySQL on port {MYSQL['port']}: {exc}\n"
                 f"Start it with ./start_local_banidb.sh")
    cur = my.cursor(pymysql.cursors.DictCursor)

    overrides = load_overrides()
    rows, conflicts = load_rows(csv_path)
    print(f"reading {os.path.basename(csv_path)}")
    print(f"{len(rows)} unique shabads after merging duplicates"
          f"   ({len(overrides)} manual matches loaded)")
    if conflicts:
        print(f"\n{len(conflicts)} metadata conflicts from merged duplicates -- "
              "kept the first value:")
        for line, field, kept, dropped, src in conflicts:
            print(f"  {field}: kept {kept!r}, dropped {dropped!r}  (csv rows {src})")

    todo = rows[: args.limit] if args.limit else rows
    print(f"\nprocessing {len(todo)} of them"
          f"{'' if args.write else '  (DRY RUN -- nothing will be saved)'}\n")

    conn = connect(args.write)
    failures = []
    imported = skipped = 0

    for i, row in enumerate(todo, start=1):
        # norm() here is load-bearing: it rewrites Notion's non-breaking spaces
        # to plain ones, without which the exact match below never fires.
        line = norm(row["shabad"])
        row = dict(row, shabad=line)      # store the normalised form too
        try:
            shabad_id = find_shabad_id(cur, line, overrides)
            ov = overrides.get(match_key(line)) or {}
            fetched = fetch_shabad(cur, shabad_id, line, ov.get("verse_id"))
        except MatchError as exc:
            failures.append((row["shabad"], exc.reason, row["rows"]))
            print(f"[{i:3}] SKIP  {exc.reason:32} {line}")
            skipped += 1
            continue

        if already_imported(conn, shabad_id):
            print(f"[{i:3}] have  shabad {shabad_id:<6} {line}")
            continue

        insert_shabad(conn, row, fetched)
        imported += 1
        found = fetched["source_line_no"]
        print(f"[{i:3}] ok    shabad {shabad_id:<6} {len(fetched['verses']):>2} lines  "
              f"ang {str(fetched['ang']):>4}  my line @{found}  "
              f"{(fetched['raag_en'] or '-'):<16} {line[:34]}")

    print(f"\nimported {imported}   skipped {skipped}")

    if failures:
        with io.open(FAILURES_PATH, "w", encoding="utf-8", newline="") as fh:
            w = csv.writer(fh)
            w.writerow(["Shabad", "Reason", "CsvRows"])
            for line, reason, src in failures:
                w.writerow([line, reason, " ".join(map(str, src))])
        print(f"failures written to {os.path.basename(FAILURES_PATH)} -- add these manually")

    if not args.write:
        print("\nDRY RUN -- re-run with --write to save to shabads.db")
    conn.close()
    my.close()


if __name__ == "__main__":
    main()
