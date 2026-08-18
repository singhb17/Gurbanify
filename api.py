"""Shabad Library API + static file server.

    python -m uvicorn api:app --port 8000
    then open http://localhost:8000

Don't add --reload: its workers outlive the parent and keep serving stale code.

Two SQLite files, deliberately separate (CLAUDE.md §5):
    shabads.db  my library. read-write. irreplaceable -- back this up.
    banidb.db   the BaniDB corpus for search. read-only. regenerable.

Docker is NOT needed to run this; it's only needed to rebuild banidb.db.
"""

import os
import random
import re
import sqlite3
from typing import List, Optional

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

HERE = os.path.dirname(os.path.abspath(__file__))
LIBRARY_DB = os.path.join(HERE, "shabads.db")
CORPUS_DB = os.path.join(HERE, "banidb.db")
STATIC_DIR = os.path.join(HERE, "static")

app = FastAPI(title="Shabad Library")

TAG_KINDS = ("genre", "speed")


@app.on_event("startup")
def ensure_deck_schema():
    """Bring an existing database up to date: deck storage, and the open history.

    schema.sql has the same definitions, but CREATE TABLE IF NOT EXISTS does
    nothing to a table that already exists, so a column added later would never
    appear. The other migration path (import_shabads.py) needs Docker running --
    the app has to be able to migrate itself. Keep the two definitions in step.
    """
    if not os.path.exists(LIBRARY_DB):
        return
    conn = sqlite3.connect(LIBRARY_DB)
    try:
        with conn:
            have = {r[1] for r in conn.execute("PRAGMA table_info(shabads)")}
            if "last_surfaced" not in have:
                conn.execute("ALTER TABLE shabads ADD COLUMN last_surfaced TEXT")
                print("migrated: added shabads.last_surfaced")
            conn.execute("""
                CREATE TABLE IF NOT EXISTS shortlist (
                  shabad_id  INTEGER NOT NULL REFERENCES shabads(id) ON DELETE CASCADE,
                  list       TEXT NOT NULL DEFAULT 'Interested',
                  added_at   TEXT NOT NULL DEFAULT (datetime('now')),
                  PRIMARY KEY (shabad_id, list)
                )""")
            conn.execute("""
                CREATE TABLE IF NOT EXISTS history (
                  id         INTEGER PRIMARY KEY,
                  shabad_id  INTEGER NOT NULL REFERENCES shabads(id) ON DELETE CASCADE,
                  opened_at  TEXT NOT NULL DEFAULT (datetime('now'))
                )""")
            conn.execute("""
                CREATE TABLE IF NOT EXISTS learning (
                  shabad_id  INTEGER PRIMARY KEY REFERENCES shabads(id) ON DELETE CASCADE,
                  added_at   TEXT NOT NULL DEFAULT (datetime('now')),
                  rahao_ok   INTEGER NOT NULL DEFAULT 0
                )""")
            conn.execute("""
                CREATE TABLE IF NOT EXISTS learning_lines (
                  line_id     INTEGER PRIMARY KEY REFERENCES lines(id) ON DELETE CASCADE,
                  shabad_id   INTEGER NOT NULL REFERENCES shabads(id) ON DELETE CASCADE,
                  level       INTEGER NOT NULL DEFAULT 0,
                  ease        REAL    NOT NULL DEFAULT 2.5,
                  interval_d  REAL    NOT NULL DEFAULT 0,
                  reps        INTEGER NOT NULL DEFAULT 0,
                  lapses      INTEGER NOT NULL DEFAULT 0,
                  due         TEXT,
                  last_review TEXT
                )""")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_learning_due ON learning_lines(due)")
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_learning_shabad ON learning_lines(shabad_id)")
    finally:
        conn.close()


@app.on_event("startup")
def heal_missing_first_letters():
    """Repair any line missing first_letters, using banidb.db.

    A row without it is invisible to First Letter Gurbani search while still
    turning up in English search -- a silent, confusing half-failure. Cheap to
    check on every boot, so check.
    """
    if not (os.path.exists(LIBRARY_DB) and os.path.exists(CORPUS_DB)):
        return
    conn = sqlite3.connect(LIBRARY_DB)
    try:
        missing = conn.execute(
            "SELECT COUNT(*) FROM lines WHERE first_letters IS NULL").fetchone()[0]
        if not missing:
            return
        conn.execute("ATTACH DATABASE ? AS corpus", (CORPUS_DB,))
        with conn:
            conn.execute("""UPDATE lines SET first_letters = (
                              SELECT v.first_letters FROM corpus.verses v
                              WHERE v.verse_id = lines.banidb_verse_id)
                            WHERE first_letters IS NULL""")
        still = conn.execute(
            "SELECT COUNT(*) FROM lines WHERE first_letters IS NULL").fetchone()[0]
        conn.execute("DETACH DATABASE corpus")
        print(f"first_letters: repaired {missing - still} lines"
              + (f", {still} still missing" if still else ""))
    finally:
        conn.close()


# --- db helpers -------------------------------------------------------------

def library(write=False):
    conn = sqlite3.connect(LIBRARY_DB if write else f"file:{LIBRARY_DB}?mode=ro",
                           uri=not write)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def corpus():
    if not os.path.exists(CORPUS_DB):
        raise HTTPException(503, "banidb.db missing -- run: python extract_corpus.py")
    conn = sqlite3.connect(f"file:{CORPUS_DB}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def letters_clause(column, q):
    """Match first letters, case-sensitively only if the query uses capitals.

    In the GurbaniAkhar encoding ਭ is 'B' and ਬ is 'b', so case is real
    information. But typing it exactly is fiddly, so:
        gkbbj  -> forgiving, matches ਬ and ਭ alike (LIKE ignores ASCII case)
        gkBBj  -> precise, only ਭ (GLOB is case-sensitive)
    Same idea as smart-case in ripgrep: you opt into strictness by using capitals.
    """
    if any(c.isupper() for c in q):
        safe = q.replace("[", "[[]").replace("*", "[*]").replace("?", "[?]")
        return f"{column} GLOB ?", f"*{safe}*"
    return f"{column} LIKE ?", f"%{q}%"


# A bare word, or anything inside double quotes.
TERM_RE = re.compile(r'"([^"]*)"|(\S+)')


def keywords(q):
    """Split a search box into terms.

        love name        -> ['love', 'name']       two independent keywords
        "love name"      -> ['love name']          one literal phrase
        "of the" name    -> ['of the', 'name']     mixed

    An unclosed quote is dropped rather than searched for literally -- typing
    one by accident should not silently return nothing.
    """
    terms = []
    for quoted, bare in TERM_RE.findall(q or ""):
        term = (quoted if quoted else bare.replace('"', "")).strip()
        if term:
            terms.append(term)
    return terms


def like_arg(term):
    r"""Wrap a term for LIKE, escaping the wildcards % and _ .

    Without this, searching '100%' matches everything -- % is LIKE's wildcard.
    Pairs with `ESCAPE '\'` in the SQL below.
    """
    safe = term.replace("\\", r"\\").replace("%", r"\%").replace("_", r"\_")
    return f"%{safe}%"


def keywords_clause(column, q):
    """AND every keyword against ONE column value. Returns (sql, args).

    'love name' finds "Perfect is the Love of the Lord's Name" -- both words
    present, order and position irrelevant.

    The caller must apply this to a single row, never spread across a shabad's
    lines: 'love' and 'name' are common enough in the translations that allowing
    them in different lines would match almost the whole library and there would
    be no one line to show or highlight.
    """
    terms = keywords(q)
    if not terms:
        return None, []
    sql = " AND ".join(f"{column} LIKE ? ESCAPE '\\'" for _ in terms)
    return sql, [like_arg(t) for t in terms]


def filter_clauses(status, rarity, genre, speed, raag, writer):
    """The tag/metadata WHERE clauses shared by the library list and the deck.

    Shared so the deck can never disagree with the library about what "Status:
    Heard" means. Returns (list_of_conditions, args) to be ANDed by the caller.
    """
    where, args = [], []
    for col, vals in (("s.status", status), ("s.rarity", rarity),
                      ("s.raag_en", raag), ("s.writer", writer)):
        if vals:
            where.append(f"{col} IN ({','.join('?' * len(vals))})")
            args += vals

    # tags are rows, not columns -- a shabad matches if it has ANY of the
    # requested values for that kind
    for kind, vals in (("genre", genre), ("speed", speed)):
        if vals:
            where.append(f"""EXISTS(SELECT 1 FROM tags t WHERE t.shabad_id = s.id
                             AND t.kind = ? AND t.value IN ({','.join('?' * len(vals))}))""")
            args += [kind] + vals
    return where, args


def decorate(conn, rows):
    """Attach each shabad's tags and the English of its own line.

    Every list view needs both -- without source_translation the list is bare
    Gurmukhi whenever you aren't searching, which is most of the time.
    """
    if not rows:
        return rows
    ids = [r["id"] for r in rows]
    ph = ",".join("?" * len(ids))

    tags = {}
    for t in conn.execute(
            f"SELECT shabad_id, kind, value FROM tags WHERE shabad_id IN ({ph})", ids):
        tags.setdefault(t["shabad_id"], {}).setdefault(t["kind"], []).append(t["value"])

    trans = {
        l["shabad_id"]: l["translation_en"]
        for l in conn.execute(
            f"""SELECT l.shabad_id, l.translation_en
                FROM lines l JOIN shabads s
                  ON s.id = l.shabad_id AND l.line_no = s.source_line_no
                WHERE l.shabad_id IN ({ph})""", ids)
    }

    # so any list view can show whether a shabad is already shortlisted
    shortlisted = {r[0] for r in conn.execute(
        f"SELECT shabad_id FROM shortlist WHERE shabad_id IN ({ph})", ids)}

    for r in rows:
        r["tags"] = tags.get(r["id"], {})
        r["source_translation"] = trans.get(r["id"])
        r["shortlisted"] = r["id"] in shortlisted
    return rows


def attach_lines(conn, rows):
    """Add every verse to each shabad. Used by the deck, where the whole shabad
    is on the card rather than just the line I know it by."""
    if not rows:
        return rows
    ids = [r["id"] for r in rows]
    ph = ",".join("?" * len(ids))
    by_shabad = {}
    for l in conn.execute(
            f"""SELECT shabad_id, line_no, gurmukhi, translation_en
                FROM lines WHERE shabad_id IN ({ph})
                ORDER BY shabad_id, line_no""", ids):
        by_shabad.setdefault(l["shabad_id"], []).append(dict(l))
    for r in rows:
        r["lines"] = by_shabad.get(r["id"], [])
    return rows


def first(rows, field):
    """First non-empty value across a shabad's verses.

    Verse 1 is the raag/mahalla header and carries no writer, so reading
    metadata off it gives NULLs.
    """
    return next((r[field] for r in rows if r[field] not in (None, "")), None)


# --- request bodies ---------------------------------------------------------

class ShabadCreate(BaseModel):
    banidb_shabad_id: int
    source_line_no: int
    rarity: Optional[str] = None
    status: Optional[str] = None
    notes: Optional[str] = None
    genre: List[str] = []
    speed: List[str] = []


class ShabadUpdate(BaseModel):
    rarity: Optional[str] = None
    status: Optional[str] = None
    notes: Optional[str] = None
    genre: Optional[List[str]] = None
    speed: Optional[List[str]] = None


# --- my library -------------------------------------------------------------

@app.get("/api/shabads")
def list_shabads(
    q: Optional[str] = None,
    mode: str = "text",
    status: Optional[List[str]] = Query(None),
    rarity: Optional[List[str]] = Query(None),
    genre: Optional[List[str]] = Query(None),
    speed: Optional[List[str]] = Query(None),
    raag: Optional[List[str]] = Query(None),
    writer: Optional[List[str]] = Query(None),
    sort: str = "id",
):
    where, args = [], []

    if q:
        if mode == "firstletter":
            # STTM-style: find my own shabads by 'gkBBj', matching ANY of their lines
            cond, arg = letters_clause("l.first_letters", q)
            where.append(f"EXISTS(SELECT 1 FROM lines l WHERE l.shabad_id = s.id AND {cond})")
            args.append(arg)
        else:
            # "English Translation" mode. Searches every line (§3), not just the
            # one I saved the shabad by. Notes are included because they're my
            # own English text; Gurmukhi and the Punjabi teeka are not, so the
            # label means what it says.
            #
            # Notes and lines are separate containers: all the keywords have to
            # land in one line, or all of them in the notes. Half in each is not
            # a match -- see keywords_clause.
            line_sql, line_args = keywords_clause("l.translation_en", q)
            note_sql, note_args = keywords_clause("s.notes", q)
            if line_sql:
                where.append(f"""(({note_sql}) OR EXISTS(
                                   SELECT 1 FROM lines l WHERE l.shabad_id = s.id
                                   AND ({line_sql})))""")
                args += note_args + line_args

    fwhere, fargs = filter_clauses(status, rarity, genre, speed, raag, writer)
    where += fwhere
    args += fargs

    order = {
        "id": "s.id", "ang": "s.ang", "raag": "s.raag_en",
        "writer": "s.writer", "status": "s.status", "rarity": "s.rarity",
        "line": "s.source_line",
    }.get(sort, "s.id")

    sql = f"""SELECT s.*, (SELECT COUNT(*) FROM lines l WHERE l.shabad_id = s.id) line_count
              FROM shabads s
              {'WHERE ' + ' AND '.join(where) if where else ''}
              ORDER BY {order}"""

    conn = library()
    rows = decorate(conn, [dict(r) for r in conn.execute(sql, args)])

    # Show WHY each shabad matched. Without this a hit on an inner line looks
    # like a false positive, because the card shows source_line -- a different
    # line entirely.
    if q and rows:
        ids = [r["id"] for r in rows]
        placeholders = ",".join("?" * len(ids))
        # Must use the SAME condition as the filter above, or the card shows a
        # line that doesn't actually contain what was searched for.
        if mode == "firstletter":
            c, a = letters_clause("l.first_letters", q)
            cond, cargs = c, [a]
        else:
            cond, cargs = keywords_clause("l.translation_en", q)
        hits = {}
        if cond:
            for l in conn.execute(
                f"""SELECT shabad_id, line_no, gurmukhi, translation_en, first_letters
                    FROM lines l WHERE l.shabad_id IN ({placeholders}) AND ({cond})
                    ORDER BY shabad_id, line_no""", [*ids, *cargs]):
                hits.setdefault(l["shabad_id"], dict(l))
        for r in rows:
            hit = hits.get(r["id"])
            if hit:
                # always returned, even when it IS my own line -- the frontend
                # still needs first_letters to highlight the matched words
                r["match"] = hit
    conn.close()
    return {"count": len(rows), "shabads": rows}


@app.get("/api/shabads/{shabad_id}")
def get_shabad(shabad_id: int):
    conn = library()
    row = conn.execute("SELECT * FROM shabads WHERE id = ?", (shabad_id,)).fetchone()
    if not row:
        conn.close()
        raise HTTPException(404, "no such shabad")
    lines = [dict(r) for r in conn.execute(
        "SELECT * FROM lines WHERE shabad_id = ? ORDER BY line_no", (shabad_id,))]
    tags = {}
    for t in conn.execute("SELECT kind, value FROM tags WHERE shabad_id = ?", (shabad_id,)):
        tags.setdefault(t["kind"], []).append(t["value"])
    # the detail view has its own heart, so it needs this too -- without it the
    # heart reads as empty on a shabad that IS shortlisted
    shortlisted = conn.execute(
        "SELECT 1 FROM shortlist WHERE shabad_id = ?", (shabad_id,)).fetchone() is not None
    lrn = conn.execute("SELECT rahao_ok FROM learning WHERE shabad_id = ?",
                       (shabad_id,)).fetchone()
    prog = learning_progress(conn, [shabad_id]).get(shabad_id) if lrn else None
    # How far the derived layer (CLAUDE.md §5) has got for this shabad. Reads
    # zero for everything until the summary/embedding pipeline exists, which is
    # exactly what makes it a useful progress indicator for that work.
    ix = conn.execute(
        """SELECT COUNT(*) total,
                  SUM(CASE WHEN summary IS NOT NULL AND summary <> '' THEN 1 ELSE 0 END) summarised,
                  SUM(CASE WHEN embedding IS NOT NULL THEN 1 ELSE 0 END) embedded
           FROM lines WHERE shabad_id = ?""", (shabad_id,)).fetchone()
    conn.close()
    out = dict(row)
    out["lines"] = [{k: v for k, v in l.items() if k != "embedding"} for l in lines]
    out["tags"] = tags
    out["shortlisted"] = shortlisted
    out["learning"] = lrn is not None
    out["learning_progress"] = prog
    out["learning_stage"] = stage_name(prog, lrn["rahao_ok"]) if lrn else None
    out["indexing"] = {"total": ix["total"], "summarised": ix["summarised"] or 0,
                       "embedded": ix["embedded"] or 0}
    return out


@app.post("/api/shabads")
def add_shabad(body: ShabadCreate):
    conn = library(write=True)
    existing = conn.execute("SELECT id, source_line FROM shabads WHERE banidb_shabad_id = ?",
                            (body.banidb_shabad_id,)).fetchone()
    if existing:
        conn.close()
        # not an error worth shouting about -- tell the UI which one it already is
        raise HTTPException(409, {
            "message": "already in your library",
            "id": existing["id"], "source_line": existing["source_line"],
        })

    cor = corpus()
    verses = cor.execute(
        "SELECT * FROM verses WHERE shabad_id = ? ORDER BY line_no",
        (body.banidb_shabad_id,)).fetchall()
    cor.close()
    if not verses:
        conn.close()
        raise HTTPException(404, "no such shabad in banidb.db")

    mine = next((v for v in verses if v["line_no"] == body.source_line_no), None)
    if not mine:
        conn.close()
        raise HTTPException(400, f"line {body.source_line_no} not in this shabad")

    try:
        with conn:
            cur = conn.execute(
                """INSERT INTO shabads
                     (banidb_shabad_id, source_line, source_line_no, ang, raag_en,
                      raag_pa, writer, source_en, source_pa, rarity, status, notes,
                      is_user_added, imported_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,0,datetime('now'))""",
                (body.banidb_shabad_id, mine["gurmukhi"], body.source_line_no,
                 first(verses, "ang"), first(verses, "raag_en"), first(verses, "raag_pa"),
                 first(verses, "writer"), first(verses, "source_en"),
                 first(verses, "source_pa"),
                 body.rarity or None, body.status or None, body.notes or None))
            new_id = cur.lastrowid

            # first_letters is what "First Letter Gurbani" searches. Leaving it
            # out made every shabad added through the app invisible to that
            # search while English search still worked.
            conn.executemany(
                """INSERT INTO lines (shabad_id, line_no, banidb_verse_id, gurmukhi,
                                      transliteration_en, translation_en, teeka_pa,
                                      first_letters)
                   VALUES (?,?,?,?,?,?,?,?)""",
                [(new_id, v["line_no"], v["verse_id"], v["gurmukhi"],
                  v["transliteration"], v["english"], v["teeka"], v["first_letters"])
                 for v in verses])

            conn.executemany(
                "INSERT OR IGNORE INTO tags (shabad_id, kind, value) VALUES (?,?,?)",
                [(new_id, kind, val)
                 for kind, vals in (("genre", body.genre), ("speed", body.speed))
                 for val in (vals or ["Not chosen"])])
    finally:
        conn.close()
    return {"id": new_id, "lines": len(verses)}


@app.patch("/api/shabads/{shabad_id}")
def update_shabad(shabad_id: int, body: ShabadUpdate):
    conn = library(write=True)
    if not conn.execute("SELECT 1 FROM shabads WHERE id = ?", (shabad_id,)).fetchone():
        conn.close()
        raise HTTPException(404, "no such shabad")

    fields = {k: v for k, v in (("rarity", body.rarity), ("status", body.status),
                                ("notes", body.notes)) if v is not None}
    try:
        with conn:
            if fields:
                conn.execute(
                    f"UPDATE shabads SET {','.join(f'{k}=?' for k in fields)} WHERE id=?",
                    [*fields.values(), shabad_id])
            # tags are replaced wholesale per kind, not merged
            for kind, vals in (("genre", body.genre), ("speed", body.speed)):
                if vals is None:
                    continue
                conn.execute("DELETE FROM tags WHERE shabad_id=? AND kind=?", (shabad_id, kind))
                conn.executemany(
                    "INSERT OR IGNORE INTO tags (shabad_id, kind, value) VALUES (?,?,?)",
                    [(shabad_id, kind, v) for v in (vals or ["Not chosen"])])
    finally:
        conn.close()
    return get_shabad(shabad_id)


@app.delete("/api/shabads/{shabad_id}")
def delete_shabad(shabad_id: int):
    conn = library(write=True)
    try:
        with conn:
            cur = conn.execute("DELETE FROM shabads WHERE id = ?", (shabad_id,))
        if not cur.rowcount:
            raise HTTPException(404, "no such shabad")
    finally:
        conn.close()
    return {"deleted": shabad_id}


@app.get("/api/filters")
def filters():
    """Every value actually present, so the filter UI never offers a dead option."""
    conn = library()
    out = {}
    for name, sql in (
        ("status", "SELECT DISTINCT status FROM shabads WHERE status IS NOT NULL ORDER BY 1"),
        ("rarity", "SELECT DISTINCT rarity FROM shabads WHERE rarity IS NOT NULL ORDER BY 1"),
        ("raag", "SELECT DISTINCT raag_en FROM shabads WHERE raag_en IS NOT NULL ORDER BY 1"),
        ("writer", "SELECT DISTINCT writer FROM shabads WHERE writer IS NOT NULL ORDER BY 1"),
    ):
        out[name] = [r[0] for r in conn.execute(sql)]
    for kind in TAG_KINDS:
        out[kind] = [r[0] for r in conn.execute(
            "SELECT DISTINCT value FROM tags WHERE kind = ? ORDER BY 1", (kind,))]
    out["total"] = conn.execute("SELECT COUNT(*) FROM shabads").fetchone()[0]
    conn.close()
    return out


# --- swipe deck --------------------------------------------------------------

DEFAULT_LIST = "Interested"


@app.get("/api/deck")
def deck(
    limit: int = 30,
    list_name: str = DEFAULT_LIST,
    status: Optional[List[str]] = Query(None),
    rarity: Optional[List[str]] = Query(None),
    genre: Optional[List[str]] = Query(None),
    speed: Optional[List[str]] = Query(None),
    raag: Optional[List[str]] = Query(None),
    writer: Optional[List[str]] = Query(None),
    include_shortlisted: bool = False,
):
    """A shuffled deck of shabads not already shortlisted.

    Plain random ordering. There was recency weighting here (surface what you
    haven't seen in a while, per CLAUDE.md §3); it was removed on request as
    unnecessary for now. `last_surfaced` is still recorded on every swipe, so
    the data to turn it back on keeps accumulating and nothing is lost.

    Filters use the same clauses as the library list, so "Status: Heard" cannot
    mean two different things in two places.
    """
    where, args = [], []
    if not include_shortlisted:
        where.append("""NOT EXISTS(SELECT 1 FROM shortlist sl
                                   WHERE sl.shabad_id = s.id AND sl.list = ?)""")
        args.append(list_name)

    fwhere, fargs = filter_clauses(status, rarity, genre, speed, raag, writer)
    where += fwhere
    args += fargs

    clause = f"WHERE {' AND '.join(where)}" if where else ""
    conn = library()
    try:
        total = conn.execute(
            f"SELECT COUNT(*) FROM shabads s {clause}", args).fetchone()[0]
        rows = decorate(conn, [dict(r) for r in conn.execute(
            f"""SELECT s.*, (SELECT COUNT(*) FROM lines l WHERE l.shabad_id = s.id) line_count
                FROM shabads s
                {clause}
                ORDER BY RANDOM() LIMIT ?""", [*args, max(1, limit)])])
        attach_lines(conn, rows)
    finally:
        conn.close()
    return {"count": len(rows), "remaining": total, "shabads": rows}


class Swipe(BaseModel):
    direction: str                          # 'left' = pass, 'right' = shortlist
    list_name: str = DEFAULT_LIST


@app.post("/api/deck/{shabad_id}/swipe")
def swipe(shabad_id: int, body: Swipe):
    """Record a decision, and stamp last_surfaced either way.

    Both directions count as surfaced: the point of the timestamp is "I have
    seen this recently", which is equally true of one I passed on. Writing it on
    the swipe rather than when the card renders means it only counts once I have
    actually looked and judged, not when a card is preloaded behind three others.
    """
    if body.direction not in ("left", "right"):
        raise HTTPException(400, "direction must be 'left' or 'right'")

    conn = library(write=True)
    try:
        row = conn.execute("SELECT last_surfaced FROM shabads WHERE id = ?",
                           (shabad_id,)).fetchone()
        if not row:
            raise HTTPException(404, "no such shabad")
        previous = row[0]           # handed back so Undo can restore it exactly

        with conn:
            conn.execute(
                "UPDATE shabads SET last_surfaced = datetime('now') WHERE id = ?",
                (shabad_id,))
            if body.direction == "right":
                conn.execute(
                    "INSERT OR IGNORE INTO shortlist (shabad_id, list) VALUES (?, ?)",
                    (shabad_id, body.list_name))
        total = conn.execute("SELECT COUNT(*) FROM shortlist WHERE list = ?",
                             (body.list_name,)).fetchone()[0]
    finally:
        conn.close()
    return {"id": shabad_id, "direction": body.direction,
            "shortlist_count": total, "previous_surfaced": previous}


class UndoSwipe(BaseModel):
    direction: str                          # the swipe being reversed
    previous_surfaced: Optional[str] = None  # what last_surfaced was before it
    list_name: str = DEFAULT_LIST


@app.post("/api/deck/{shabad_id}/undo")
def undo_swipe(shabad_id: int, body: UndoSwipe):
    """Reverse a swipe completely: un-shortlist it and put last_surfaced back.

    Restoring the timestamp matters even though nothing reads it today. The deck
    keeps that history in case recency weighting comes back, and a card you took
    back was never really surfaced -- leaving the stamp would quietly poison it.

    Removing from the shortlist is tolerant of it already being gone: you may
    have deleted it by hand from the Interested list before hitting undo.
    """
    conn = library(write=True)
    try:
        if not conn.execute("SELECT 1 FROM shabads WHERE id = ?", (shabad_id,)).fetchone():
            raise HTTPException(404, "no such shabad")
        with conn:
            conn.execute("UPDATE shabads SET last_surfaced = ? WHERE id = ?",
                         (body.previous_surfaced, shabad_id))
            if body.direction == "right":
                conn.execute("DELETE FROM shortlist WHERE shabad_id = ? AND list = ?",
                             (shabad_id, body.list_name))
        total = conn.execute("SELECT COUNT(*) FROM shortlist WHERE list = ?",
                             (body.list_name,)).fetchone()[0]
    finally:
        conn.close()
    return {"undone": shabad_id, "shortlist_count": total}


@app.get("/api/shortlist")
def get_shortlist(list_name: str = DEFAULT_LIST):
    conn = library()
    try:
        rows = decorate(conn, [dict(r) for r in conn.execute(
            """SELECT s.*, sl.added_at,
                      (SELECT COUNT(*) FROM lines l WHERE l.shabad_id = s.id) line_count
               FROM shortlist sl JOIN shabads s ON s.id = sl.shabad_id
               WHERE sl.list = ?
               ORDER BY sl.added_at DESC, s.id DESC""", (list_name,))])
    finally:
        conn.close()
    return {"count": len(rows), "list": list_name, "shabads": rows}


@app.post("/api/shortlist/{shabad_id}")
def add_to_shortlist(shabad_id: int, list_name: str = DEFAULT_LIST):
    """Add straight from the library, without going through the deck.

    Deliberately does NOT touch last_surfaced: deciding from the list is not the
    same event as being shown a card, and conflating them would corrupt the
    recency history the deck keeps for later.
    """
    conn = library(write=True)
    try:
        if not conn.execute("SELECT 1 FROM shabads WHERE id = ?", (shabad_id,)).fetchone():
            raise HTTPException(404, "no such shabad")
        with conn:
            conn.execute("INSERT OR IGNORE INTO shortlist (shabad_id, list) VALUES (?, ?)",
                         (shabad_id, list_name))
        total = conn.execute("SELECT COUNT(*) FROM shortlist WHERE list = ?",
                             (list_name,)).fetchone()[0]
    finally:
        conn.close()
    return {"added": shabad_id, "shortlist_count": total}


@app.delete("/api/shortlist/{shabad_id}")
def remove_from_shortlist(shabad_id: int, list_name: str = DEFAULT_LIST):
    conn = library(write=True)
    try:
        with conn:
            cur = conn.execute("DELETE FROM shortlist WHERE shabad_id = ? AND list = ?",
                               (shabad_id, list_name))
        if not cur.rowcount:
            raise HTTPException(404, "not in that list")
        total = conn.execute("SELECT COUNT(*) FROM shortlist WHERE list = ?",
                             (list_name,)).fetchone()[0]
    finally:
        conn.close()
    return {"removed": shabad_id, "shortlist_count": total}


@app.delete("/api/shortlist")
def clear_shortlist(list_name: str = DEFAULT_LIST):
    """Empty the whole folder. Only touches shortlist rows -- the shabads, their
    tags and their notes are untouched, so this is not a destructive edit."""
    conn = library(write=True)
    try:
        with conn:
            cur = conn.execute("DELETE FROM shortlist WHERE list = ?", (list_name,))
        removed = cur.rowcount
    finally:
        conn.close()
    return {"cleared": removed, "list": list_name}


# --- history -----------------------------------------------------------------

HISTORY_LIMIT = 80


class OpenedShabad(BaseModel):
    shabad_id: int


@app.post("/api/history")
def record_open(body: OpenedShabad):
    """Log that a shabad was opened.

    Repeats are deliberate -- this is a log of what I actually looked at, not a
    set of what I've seen. Opening the same shabad five times is five rows,
    because "I keep coming back to this one" is the signal worth keeping.

    Takes the shabad in the body rather than the path so that the path can mean
    exactly one thing: /api/history/{id} is always a history ENTRY, never a
    shabad. The two id spaces are different and mixing them in one url shape is
    how you end up deleting the wrong row.

    Trimmed on every insert rather than by a cleanup job: the table can never
    grow past the cap, so there's nothing to remember to run.
    """
    conn = library(write=True)
    try:
        if not conn.execute("SELECT 1 FROM shabads WHERE id = ?",
                            (body.shabad_id,)).fetchone():
            raise HTTPException(404, "no such shabad")
        with conn:
            conn.execute("INSERT INTO history (shabad_id) VALUES (?)", (body.shabad_id,))
            conn.execute("""DELETE FROM history WHERE id NOT IN
                            (SELECT id FROM history ORDER BY id DESC LIMIT ?)""",
                         (HISTORY_LIMIT,))
        kept = conn.execute("SELECT COUNT(*) FROM history").fetchone()[0]
    finally:
        conn.close()
    return {"recorded": body.shabad_id, "kept": kept}


@app.delete("/api/history/{history_id}")
def remove_history_entry(history_id: int):
    """Drop ONE entry -- the row you tapped, not every visit to that shabad.

    A shabad can sit in here several times over. Removing all of them from a tap
    on one row would delete things that aren't on screen, so the visible row is
    the only thing that goes.
    """
    conn = library(write=True)
    try:
        with conn:
            cur = conn.execute("DELETE FROM history WHERE id = ?", (history_id,))
        if not cur.rowcount:
            raise HTTPException(404, "no such history entry")
        left = conn.execute("SELECT COUNT(*) FROM history").fetchone()[0]
    finally:
        conn.close()
    return {"removed": history_id, "count": left}


@app.get("/api/history")
def get_history(limit: int = HISTORY_LIMIT):
    """Newest first. A shabad appears once per time it was opened."""
    conn = library()
    try:
        rows = decorate(conn, [dict(r) for r in conn.execute(
            """SELECT s.*, h.opened_at, h.id AS history_id,
                      (SELECT COUNT(*) FROM lines l WHERE l.shabad_id = s.id) line_count
               FROM history h JOIN shabads s ON s.id = h.shabad_id
               ORDER BY h.id DESC LIMIT ?""", (max(1, limit),))])
    finally:
        conn.close()
    return {"count": len(rows), "limit": HISTORY_LIMIT, "shabads": rows}


@app.delete("/api/history")
def clear_history():
    conn = library(write=True)
    try:
        with conn:
            cur = conn.execute("DELETE FROM history")
        removed = cur.rowcount
    finally:
        conn.close()
    return {"cleared": removed}


# --- memorization -------------------------------------------------------------
#
# The scaffold. Each rung removes more of the cue; levels 1-4 are checked
# objectively by typing first letters, which is the same muscle memory used to
# search on STTM. Only level 5 is self-graded, because typing full Gurmukhi to
# prove a point would be miserable.
LEVELS = [
    "Learn",        # 0 - read it with the meaning in front of you
    "Meaning",      # 1 - given the English, pick the right tuk
    "First letters",# 2 - given the English, type the first letters
    "Chained",      # 3 - given the PREVIOUS tuk, type this one's first letters
    "From memory",  # 4 - given only the position, type the first letters
    "Full recall",  # 5 - recite it whole, self-graded
]
MAX_LEVEL = len(LEVELS) - 1

# Nothing ever graduates (CLAUDE.md §3). Vanilla SM-2 grows intervals without
# limit, which is exactly how a shabad memorised once quietly disappears.
MAX_INTERVAL_DAYS = 60.0

NEW_LINES_PER_DAY = 12          # about one shabad; stops enthusiasm outrunning recall
SESSION_LINES = 40              # a short daily burst, not a due-list dump


def sm2(ease, interval, reps, grade):
    """One SM-2 step. grade: 0 blank, 1 nearly, 2 got it.

    Three-way rather than pass/fail because the target sits between word-perfect
    and good-enough: "nearly" shortens the gap without throwing away the history.
    """
    if grade == 0:
        return max(1.3, ease - 0.20), 1.0, reps + 1, True      # lapse
    if grade == 1:
        ease = max(1.3, ease - 0.15)
        interval = 1.0 if reps == 0 else max(1.0, interval * 1.2)
    else:
        ease = min(2.8, ease + 0.10)
        interval = 1.0 if reps == 0 else (6.0 if reps == 1 else interval * ease)
    return ease, min(interval, MAX_INTERVAL_DAYS), reps + 1, False


def rahao_line(conn, shabad_id):
    """The line the meaning gate asks about.

    Traditionally the rahao holds the shabad's central idea, so understanding it
    is understanding what you're about to memorise. Only ~67% of shabads have a
    detectable ਰਹਾਉ, so fall back to the line I saved the shabad by -- my own
    choice of its key line, which is the next best thing.
    """
    row = conn.execute(
        """SELECT * FROM lines WHERE shabad_id = ? AND gurmukhi LIKE '%' || ? || '%'
           ORDER BY line_no LIMIT 1""", (shabad_id, "ਰਹਾਉ")).fetchone()
    if row:
        return dict(row)
    row = conn.execute(
        """SELECT l.* FROM lines l JOIN shabads s ON s.id = l.shabad_id
           WHERE l.shabad_id = ? AND l.line_no = COALESCE(s.source_line_no, 1)""",
        (shabad_id,)).fetchone()
    return dict(row) if row else None


def learning_progress(conn, shabad_ids):
    """Per-shabad stage, derived from its lines -- never set by hand."""
    if not shabad_ids:
        return {}
    ph = ",".join("?" * len(shabad_ids))
    out = {}
    for r in conn.execute(
            f"""SELECT shabad_id, COUNT(*) n, AVG(level) avg_level, SUM(reps) reps,
                       MIN(due) next_due, MIN(interval_d) min_interval
                FROM learning_lines WHERE shabad_id IN ({ph})
                GROUP BY shabad_id""", shabad_ids):
        out[r["shabad_id"]] = {
            "lines": r["n"],
            "avg_level": round(r["avg_level"] or 0, 2),
            "percent": round(100 * (r["avg_level"] or 0) / MAX_LEVEL),
            "reviews": r["reps"] or 0,
            "next_due": r["next_due"],
            "matured": (r["min_interval"] or 0) >= MAX_INTERVAL_DAYS,
        }
    return out


def stage_name(p, rahao_ok):
    if not p or not p["reviews"]:
        return "Not started"
    if not rahao_ok:
        return "Understanding"
    if p["matured"] and p["avg_level"] >= MAX_LEVEL:
        return "Maintenance"
    if p["avg_level"] >= MAX_LEVEL - 0.5:
        return "Memorised"
    if p["avg_level"] >= 2:
        return "Consolidating"
    return "Learning"


@app.get("/api/learning")
def list_learning():
    conn = library()
    try:
        rows = decorate(conn, [dict(r) for r in conn.execute(
            """SELECT s.*, lg.added_at, lg.rahao_ok,
                      (SELECT COUNT(*) FROM lines l WHERE l.shabad_id = s.id) line_count
               FROM learning lg JOIN shabads s ON s.id = lg.shabad_id
               ORDER BY lg.added_at DESC""")])
        prog = learning_progress(conn, [r["id"] for r in rows])
        # same builder the session uses, so the tab can never promise practice
        # that Start refuses to give
        queue, allowance = build_queue(conn, 10_000)
        due_now = {q["shabad_id"] for q in queue}
        for r in rows:
            p = prog.get(r["id"])
            r["progress"] = p
            r["stage"] = stage_name(p, r["rahao_ok"])
            r["due"] = r["id"] in due_now
        # so the ui can say WHY there's nothing to do: held back by the daily
        # new-material cap reads very differently from genuinely all caught up
        waiting = conn.execute(
            "SELECT COUNT(*) FROM learning_lines WHERE due IS NULL").fetchone()[0]
        next_due = conn.execute(
            "SELECT MIN(due) FROM learning_lines WHERE due > date('now')").fetchone()[0]
    finally:
        conn.close()
    return {"count": len(rows), "due_lines": len(queue), "new_waiting": waiting,
            "new_allowance": allowance, "next_due": next_due, "shabads": rows}


class Review(BaseModel):
    line_id: int
    grade: int                                  # 0 blank, 1 nearly, 2 got it


# MUST be declared before /api/learning/{shabad_id}: FastAPI matches routes in
# declaration order, so with the parameterised one first, "review" is handed to
# it as a shabad id, fails to parse as an int, and every review 422s.
@app.post("/api/learning/review")
def post_review(body: Review):
    return review_line(body)


@app.post("/api/learning/{shabad_id}")
def add_to_learning(shabad_id: int):
    """Start memorising a shabad. Every line gets a progress row -- that set IS
    the scope, so a line selector later needs no schema change."""
    conn = library(write=True)
    try:
        if not conn.execute("SELECT 1 FROM shabads WHERE id = ?", (shabad_id,)).fetchone():
            raise HTTPException(404, "no such shabad")
        with conn:
            conn.execute("INSERT OR IGNORE INTO learning (shabad_id) VALUES (?)", (shabad_id,))
            conn.execute(
                """INSERT OR IGNORE INTO learning_lines (line_id, shabad_id)
                   SELECT id, shabad_id FROM lines WHERE shabad_id = ?""", (shabad_id,))
        n = conn.execute("SELECT COUNT(*) FROM learning_lines WHERE shabad_id = ?",
                         (shabad_id,)).fetchone()[0]
        total = conn.execute("SELECT COUNT(*) FROM learning").fetchone()[0]
    finally:
        conn.close()
    return {"added": shabad_id, "lines": n, "learning_count": total}


@app.delete("/api/learning/{shabad_id}")
def remove_from_learning(shabad_id: int):
    """Drops all memorization progress for this shabad. Irreversible."""
    conn = library(write=True)
    try:
        with conn:
            cur = conn.execute("DELETE FROM learning WHERE shabad_id = ?", (shabad_id,))
            conn.execute("DELETE FROM learning_lines WHERE shabad_id = ?", (shabad_id,))
        if not cur.rowcount:
            raise HTTPException(404, "not being learned")
        total = conn.execute("SELECT COUNT(*) FROM learning").fetchone()[0]
    finally:
        conn.close()
    return {"removed": shabad_id, "learning_count": total}


@app.get("/api/learning/gate/{shabad_id}")
def meaning_gate(shabad_id: int):
    """The rahao meaning check: the tuk, and four English meanings to choose from.

    Distractors come from the library's own other shabads -- no LLM, and they're
    real Gurbani meanings rather than invented wrong answers. Once §7's vectors
    exist these should come from the most SIMILAR lines instead of random ones,
    which turns a recognition test into real discrimination.
    """
    conn = library()
    try:
        line = rahao_line(conn, shabad_id)
        if not line:
            raise HTTPException(404, "no lines for that shabad")
        others = [r[0] for r in conn.execute(
            """SELECT translation_en FROM lines
               WHERE shabad_id <> ? AND translation_en IS NOT NULL AND translation_en <> ''
               ORDER BY RANDOM() LIMIT 3""", (shabad_id,))]
    finally:
        conn.close()
    options = [line["translation_en"], *others]
    random.shuffle(options)
    return {
        "shabad_id": shabad_id,
        "line": {k: line[k] for k in ("id", "line_no", "gurmukhi", "teeka_pa")},
        "options": options,
        "answer": line["translation_en"],
    }


@app.get("/api/learning/quiz/{line_id}")
def line_quiz(line_id: int):
    """Meaning check for any single line -- same idea as the rahao gate."""
    conn = library()
    try:
        line = conn.execute("SELECT * FROM lines WHERE id = ?", (line_id,)).fetchone()
        if not line:
            raise HTTPException(404, "no such line")
        others = [r[0] for r in conn.execute(
            """SELECT translation_en FROM lines
               WHERE shabad_id <> ? AND translation_en IS NOT NULL AND translation_en <> ''
               ORDER BY RANDOM() LIMIT 3""", (line["shabad_id"],))]
    finally:
        conn.close()
    options = [line["translation_en"], *others]
    random.shuffle(options)
    return {"line_id": line_id, "options": options, "answer": line["translation_en"]}


@app.post("/api/learning/{shabad_id}/gate")
def pass_gate(shabad_id: int):
    conn = library(write=True)
    try:
        with conn:
            cur = conn.execute("UPDATE learning SET rahao_ok = 1 WHERE shabad_id = ?",
                               (shabad_id,))
        if not cur.rowcount:
            raise HTTPException(404, "not being learned")
    finally:
        conn.close()
    return {"shabad_id": shabad_id, "rahao_ok": True}


def build_queue(conn, budget):
    """Everything practisable right now, in the order it should be drilled.

    THE single source of truth for "is anything due". The Learn tab used to
    answer that with its own query, which counted brand-new lines the session
    would then refuse once the daily new-material cap was spent -- so the tab
    said "due" and pressing Start said "nothing due". Both now call this.

    Scheduling is per SHABAD even though ease is tracked per line: when anything
    in a shabad falls due the whole shabad surfaces and runs top to bottom. Pure
    per-line scheduling scatters one shabad across five days, which is useless
    for keertan where the flow is the thing.
    """
    used_today = conn.execute(
        "SELECT COUNT(*) FROM learning_lines WHERE reps = 1 AND last_review = date('now')"
    ).fetchone()[0]
    allowance = max(0, NEW_LINES_PER_DAY - used_today)

    rows = [dict(r) for r in conn.execute(
        """SELECT ll.line_id, ll.shabad_id, ll.level, ll.due, ll.reps,
                  l.line_no, l.gurmukhi, l.translation_en, l.teeka_pa, l.first_letters,
                  s.source_line, s.source_line_no, lg.rahao_ok,
                  (SELECT MIN(COALESCE(x.due, '9999')) FROM learning_lines x
                   WHERE x.shabad_id = ll.shabad_id) shabad_due,
                  (SELECT p.gurmukhi FROM lines p
                   WHERE p.shabad_id = ll.shabad_id AND p.line_no = l.line_no - 1) prev_gurmukhi
           FROM learning_lines ll
           JOIN lines l ON l.id = ll.line_id
           JOIN shabads s ON s.id = ll.shabad_id
           JOIN learning lg ON lg.shabad_id = ll.shabad_id
           WHERE ll.due IS NULL OR ll.due <= date('now')
           ORDER BY shabad_due, ll.shabad_id, l.line_no""")]

    queue, used_new = [], 0
    for r in rows:
        if r["due"] is None:                       # brand new line
            if used_new >= allowance:
                continue
            used_new += 1
        r["level_name"] = LEVELS[min(r["level"], MAX_LEVEL)]
        queue.append(r)
        if len(queue) >= budget:
            break
    return queue, allowance


@app.get("/api/learning/session")
def learning_session(budget: int = SESSION_LINES):
    """Today's practice queue, capped at `budget` lines for a short daily burst."""
    conn = library()
    try:
        full, allowance = build_queue(conn, 10_000)
        queue = full[:max(1, budget)]
    finally:
        conn.close()
    return {
        "count": len(queue), "total": len(full),
        "new_allowance": allowance, "levels": LEVELS,
        "queue": queue,
    }


def review_line(body):
    """Record one answer: reschedule it, and move it up or down the scaffold."""
    if body.grade not in (0, 1, 2):
        raise HTTPException(400, "grade must be 0, 1 or 2")

    conn = library(write=True)
    try:
        row = conn.execute("SELECT * FROM learning_lines WHERE line_id = ?",
                           (body.line_id,)).fetchone()
        if not row:
            raise HTTPException(404, "line is not being learned")

        ease, interval, reps, lapsed = sm2(
            row["ease"], row["interval_d"], row["reps"], body.grade)

        # the scaffold moves separately from the schedule: getting it right earns
        # a harder cue next time, blanking drops back to an easier one
        level = row["level"]
        if body.grade == 2:
            level = min(MAX_LEVEL, level + 1)
        elif body.grade == 0:
            level = max(0, level - 1)

        with conn:
            conn.execute(
                """UPDATE learning_lines
                   SET level = ?, ease = ?, interval_d = ?, reps = ?,
                       lapses = lapses + ?, last_review = date('now'),
                       due = date('now', '+' || CAST(ROUND(?) AS INTEGER) || ' days')
                   WHERE line_id = ?""",
                (level, ease, interval, reps, 1 if lapsed else 0, interval, body.line_id))
    finally:
        conn.close()
    return {"line_id": body.line_id, "level": level, "level_name": LEVELS[level],
            "interval_days": round(interval, 1), "ease": round(ease, 2)}


# --- searching BaniDB to add something new ----------------------------------

@app.get("/api/search")
def search(q: str, mode: str = "firstletter", source: Optional[str] = None, limit: int = 40):
    """STTM-style. mode=firstletter matches 'gkbvv'; mode=fullword matches Gurmukhi."""
    q = q.strip()
    if len(q) < 2:
        return {"results": [], "note": "type at least 2 characters"}

    if mode == "firstletter":
        # prefix first (indexed, instant), then anywhere if that finds little
        if any(c.isupper() for c in q):
            safe = q.replace("[", "[[]").replace("*", "[*]").replace("?", "[?]")
            clause, args = "first_letters GLOB ?", [safe + "*"]
        else:
            clause, args = "first_letters LIKE ?", [q + "%"]
    else:
        clause, args = "gurmukhi LIKE ?", [f"%{q}%"]

    sql = f"SELECT * FROM verses WHERE {clause}"
    if source:
        sql += " AND source_id = ?"
        args.append(source)
    sql += " ORDER BY verse_id LIMIT ?"
    args.append(limit)

    conn = corpus()
    rows = [dict(r) for r in conn.execute(sql, args)]

    if mode == "firstletter" and len(rows) < limit:
        anywhere, arg = letters_clause("first_letters", q)
        prefix_col = "first_letters GLOB ?" if any(c.isupper() for c in q) else "first_letters LIKE ?"
        sql2 = f"SELECT * FROM verses WHERE {anywhere} AND NOT ({prefix_col})"
        args2 = [arg, (q + "*") if any(c.isupper() for c in q) else (q + "%")]
        if source:
            sql2 += " AND source_id = ?"
            args2.append(source)
        sql2 += " ORDER BY verse_id LIMIT ?"
        args2.append(limit - len(rows))
        rows += [dict(r) for r in conn.execute(sql2, args2)]
    conn.close()

    have = set()
    lib = library()
    for r in lib.execute("SELECT banidb_shabad_id FROM shabads"):
        have.add(r[0])
    lib.close()
    for r in rows:
        r["already_have"] = r["shabad_id"] in have
    return {"count": len(rows), "results": rows}


@app.get("/api/preview/{banidb_shabad_id}")
def preview(banidb_shabad_id: int):
    """Every verse of a shabad, to confirm before adding."""
    conn = corpus()
    verses = [dict(r) for r in conn.execute(
        "SELECT * FROM verses WHERE shabad_id = ? ORDER BY line_no", (banidb_shabad_id,))]
    conn.close()
    if not verses:
        raise HTTPException(404, "no such shabad")

    lib = library()
    existing = lib.execute("SELECT id FROM shabads WHERE banidb_shabad_id = ?",
                           (banidb_shabad_id,)).fetchone()
    lib.close()
    return {
        "banidb_shabad_id": banidb_shabad_id,
        "ang": first(verses, "ang"),
        "raag_en": first(verses, "raag_en"),
        "writer": first(verses, "writer"),
        "source_en": first(verses, "source_en"),
        "already_have_id": existing["id"] if existing else None,
        "verses": verses,
    }


# --- static -----------------------------------------------------------------

ASSET_RE = re.compile(r"/static/([\w.\-]+\.(?:js|css))")


@app.get("/")
def index():
    """Serve index.html with a version stamp on every css/js it references.

    StaticFiles sends no Cache-Control, so the browser falls back to heuristic
    caching and will serve app.js from disk without even revalidating. The
    symptom is horrible to debug: the API returns the new behaviour while the
    page runs last week's JavaScript, so a working feature looks broken.

    Stamping each asset with its mtime means the URL changes whenever the file
    does, so a stale hit is impossible and no hard refresh is ever needed.
    """
    with open(os.path.join(STATIC_DIR, "index.html"), encoding="utf-8") as f:
        html = f.read()

    def stamp(m):
        try:
            v = int(os.path.getmtime(os.path.join(STATIC_DIR, m.group(1))))
        except OSError:
            return m.group(0)          # referenced file is gone; leave it alone
        return f"{m.group(0)}?v={v}"

    # the html itself must never be cached, or the new stamps are never seen
    return HTMLResponse(ASSET_RE.sub(stamp, html),
                        headers={"Cache-Control": "no-cache"})


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


# Declared AFTER the mount on purpose: Starlette matches in order, so a catch-all
# above it would swallow /static. Every app path (/deck, /shabad/123, ...) is
# handled by the client router, so they all need to serve the same page -- that's
# what makes real urls work instead of #fragments, including on a hard refresh.
@app.get("/{full_path:path}")
def spa_fallback(full_path: str):
    if full_path.startswith(("api/", "static/")):
        raise HTTPException(404, "not found")
    return index()
