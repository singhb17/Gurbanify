"""Shabad Library API + static file server.

    python -m uvicorn api:app --port 8000
    then open http://localhost:8000

Don't add --reload: its workers outlive the parent and keep serving stale code.

Two SQLite files, deliberately separate (CLAUDE.md §5):
    shabads.db  my library. read-write. irreplaceable -- back this up.
    banidb.db   the BaniDB corpus for search. read-only. regenerable.

Docker is NOT needed to run this; it's only needed to rebuild banidb.db.
"""

import base64
import datetime
import io
import os
import random
import re
import secrets
import sqlite3
import subprocess
import sys
import time
from typing import List, Optional

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

HERE = os.path.dirname(os.path.abspath(__file__))
LIBRARY_DB = os.path.join(HERE, "shabads.db")
CORPUS_DB = os.path.join(HERE, "banidb.db")
STATIC_DIR = os.path.join(HERE, "static")
# Where a detached indexing child writes. Without this its output goes nowhere
# and a crash on startup is invisible from both the app and the terminal.
INDEX_LOG = os.path.join(HERE, "search", "index.log")


def load_env():
    """Read .env into the environment. Never committed -- see .gitignore."""
    path = os.path.join(HERE, ".env")
    if not os.path.exists(path):
        return
    for line in io.open(path, encoding="utf-8"):
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


load_env()

# Set APP_PASSWORD in .env and every request needs it. Left unset, there is no
# auth at all -- which is right on a laptop and wrong the moment the tunnel is
# up, so the tunnel scripts refuse to start without one.
APP_USER = os.environ.get("APP_USER", "keertan")
APP_PASSWORD = os.environ.get("APP_PASSWORD", "")

app = FastAPI(title="Shabad Library")


@app.middleware("http")
async def require_password(request, call_next):
    """Basic Auth over the whole app.

    NOT exempt for localhost, and that is the important part: cloudflared runs
    on this machine and proxies to http://localhost:8000, so EVERY request from
    the public tunnel arrives looking like 127.0.0.1. An exemption for local
    addresses would therefore exempt the entire internet.

    /health is the one open path. It returns a bare ok and no data, so it gives
    an unauthenticated caller nothing, and it lets the watchdog check liveness
    without carrying credentials around.
    """
    if not APP_PASSWORD or request.url.path == "/health":
        return await call_next(request)

    header = request.headers.get("authorization", "")
    if header.startswith("Basic "):
        try:
            user, _, pw = base64.b64decode(header[6:]).decode("utf-8").partition(":")
            # compare_digest, not ==: a plain comparison returns as soon as two
            # characters differ, and the time it takes leaks how much of the
            # password was right.
            if (secrets.compare_digest(user, APP_USER)
                    and secrets.compare_digest(pw, APP_PASSWORD)):
                return await call_next(request)
        except Exception:
            pass                        # malformed header -- treat as no header
    return Response(status_code=401, content="Authentication required",
                    headers={"WWW-Authenticate": 'Basic realm="Shabad Library"'})


@app.get("/health")
def health():
    """Liveness for the watchdog: is the process up AND is the database usable?

    A port that accepts connections proves only that something is listening --
    a process wedged on a locked database passes that test and fails every real
    request. So this touches the database. It returns no data: an open endpoint
    should tell an unauthenticated caller nothing beyond yes or no.
    """
    try:
        conn = library()
        try:
            conn.execute("SELECT 1 FROM shabads LIMIT 1").fetchone()
        finally:
            conn.close()
    except Exception as e:
        return JSONResponse({"ok": False}, status_code=503,
                            headers={"X-Error": type(e).__name__})
    return {"ok": True}

TAG_KINDS = ("genre", "speed")

# The two summariser models the bench in search/ settled on: gemini37 led every
# topic set, and glm47nt is the best non-Google option once its reasoning is
# turned off (which made it both cheaper AND better). Two, not three -- a third
# means ~40 results to judge per query instead of ~25, which is the difference
# between a habit and a chore. Adding one later is an INSERT, not a migration.
#
# glm47nt replaced deepseeknt on 2026-08-19, and NOT on quality: they benched a
# single point apart, which is noise across 108 lines. On price. DeepSeek v4 Pro
# roughly doubled mid-project -- every one of its twelve providers moved together,
# so it was an upstream rise and not a routing accident -- taking a full index
# from $3.75 to $6.67 while GLM 4.7 does the same work for $3.37.
#
# This is the churn §3 keys the derived layer by model to survive. The swap was
# an UPDATE and a regenerate; no schema changed, and every vote already cast
# stayed valid because line_relations records the judgement, not the model.
DEFAULT_MODELS = (
    ("gemini37", "Gemini 3.7 Flash"),
    ("glm47nt", "GLM 4.7"),
)

SIMILAR_LIMIT = 20          # §3: about twenty results, ranked, never thresholded


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
                  shabad_id      INTEGER PRIMARY KEY REFERENCES shabads(id) ON DELETE CASCADE,
                  added_at       TEXT NOT NULL DEFAULT (datetime('now')),
                  status         TEXT NOT NULL DEFAULT 'not_started',
                  last_practised TEXT
                )""")
            # The SM-2 layer that used to live here -- per-line ease, six levels,
            # due dates, a rahao gate, daily caps -- is gone. It worked and went
            # unused: being told what to practise and when turns something you
            # want to do into something you are behind on. Dropped rather than
            # tuned. See the memorization section below.
            have = {r[1] for r in conn.execute("PRAGMA table_info(learning)")}
            if "last_practised" not in have:
                conn.execute("ALTER TABLE learning ADD COLUMN last_practised TEXT")
                print("migrated: added learning.last_practised")
            if "status" not in have:
                conn.execute("ALTER TABLE learning ADD COLUMN status TEXT "
                             "NOT NULL DEFAULT 'not_started'")
                print("migrated: added learning.status")
            if "rahao_ok" in have:
                # Left behind by the meaning gate. Harmless, but a dead column
                # invites someone to wonder later whether it still means anything.
                try:
                    conn.execute("ALTER TABLE learning DROP COLUMN rahao_ok")
                    print("migrated: dropped learning.rahao_ok")
                except sqlite3.OperationalError:
                    pass                      # sqlite too old for DROP COLUMN
            conn.execute("DROP TABLE IF EXISTS learning_lines")
            conn.execute("DROP INDEX IF EXISTS idx_learning_due")
            conn.execute("DROP INDEX IF EXISTS idx_learning_shabad")

            # --- similarity search + model comparison (CLAUDE.md §3) ---
            conn.execute("""
                CREATE TABLE IF NOT EXISTS models (
                  name        TEXT PRIMARY KEY,
                  label       TEXT NOT NULL,
                  enabled     INTEGER NOT NULL DEFAULT 1,
                  sort_order  INTEGER NOT NULL DEFAULT 0
                )""")
            conn.execute("""
                CREATE TABLE IF NOT EXISTS line_summaries (
                  model       TEXT NOT NULL,
                  line_id     INTEGER NOT NULL REFERENCES lines(id) ON DELETE CASCADE,
                  summary     TEXT NOT NULL,
                  embedding   BLOB,
                  prompt_ver  TEXT NOT NULL DEFAULT '',
                  created_at  TEXT NOT NULL DEFAULT (datetime('now')),
                  PRIMARY KEY (model, line_id)
                ) WITHOUT ROWID""")
            conn.execute("""
                CREATE TABLE IF NOT EXISTS line_relations (
                  query_line_id   INTEGER NOT NULL REFERENCES lines(id) ON DELETE CASCADE,
                  result_line_id  INTEGER NOT NULL REFERENCES lines(id) ON DELETE CASCADE,
                  verdict         INTEGER NOT NULL,
                  judged_at       TEXT NOT NULL DEFAULT (datetime('now')),
                  PRIMARY KEY (query_line_id, result_line_id)
                )""")
            conn.execute("""
                CREATE TABLE IF NOT EXISTS model_results (
                  query_line_id   INTEGER NOT NULL REFERENCES lines(id) ON DELETE CASCADE,
                  result_line_id  INTEGER NOT NULL REFERENCES lines(id) ON DELETE CASCADE,
                  model           TEXT NOT NULL,
                  rank            INTEGER NOT NULL,
                  prompt_ver      TEXT NOT NULL DEFAULT '',
                  PRIMARY KEY (query_line_id, result_line_id, model)
                )""")
            conn.execute("""
                CREATE TABLE IF NOT EXISTS settings (
                  key    TEXT PRIMARY KEY,
                  value  TEXT NOT NULL
                )""")
            conn.execute("""
                CREATE TABLE IF NOT EXISTS index_jobs (
                  id          INTEGER PRIMARY KEY,
                  model       TEXT NOT NULL,
                  state       TEXT NOT NULL,
                  phase       TEXT,
                  total       INTEGER NOT NULL DEFAULT 0,
                  done        INTEGER NOT NULL DEFAULT 0,
                  spent       REAL    NOT NULL DEFAULT 0,
                  error       TEXT,
                  started_at  TEXT NOT NULL DEFAULT (datetime('now')),
                  updated_at  TEXT NOT NULL DEFAULT (datetime('now'))
                )""")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_index_jobs_state "
                         "ON index_jobs(state, updated_at)")
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_relations_result ON line_relations(result_line_id)")
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_model_results_model ON model_results(model)")

            # Seed the two models chosen by the bench in search/. INSERT OR IGNORE
            # so re-running never clobbers an `enabled` flag toggled in Settings.
            for i, (name, label) in enumerate(DEFAULT_MODELS):
                conn.execute("INSERT OR IGNORE INTO models (name, label, sort_order) "
                             "VALUES (?, ?, ?)", (name, label, i))
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


def model_rows(conn):
    return [dict(r) for r in conn.execute(
        "SELECT name, label, enabled, sort_order FROM models ORDER BY sort_order, name")]


DEFAULT_SETTINGS = {"auto_index": "1", "wake_lock": "1"}

# The most an automatic background pass may spend in one go. A shabad averages
# ten lines, about a cent, so this covers a normal add many times over while
# making it impossible for adding one shabad to start a multi-dollar run over a
# backlog. Clearing a backlog is a manual decision, taken with the price on
# screen.
AUTO_INDEX_BUDGET = 0.25


def get_setting(conn, key):
    row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else DEFAULT_SETTINGS.get(key)


def spawn_indexer(model=None, budget=AUTO_INDEX_BUDGET):
    """Kick off an indexing pass in a DETACHED child process.

    `model` limits the run to one model; the default of None means "every model
    switched on in Settings", which is what the automatic pass wants.

    `budget` is the ceiling in dollars. The default is deliberately small (see
    AUTO_INDEX_BUDGET); a catch-up run started from the enable dialog passes the
    figure the user was actually shown, so the run can never quietly exceed the
    number they agreed to.

    Deliberately a subprocess and not a thread: BGE-M3 wants ~3 GB while it
    runs, and CLAUDE.md §7 is explicit that the web app never loads the model.
    A child process honours that -- the memory belongs to something that exits,
    so the server goes back to its normal footprint the moment indexing ends.

    Fire and forget. Adding a shabad must never wait on OpenRouter, and must
    never fail because indexing did: the shabad is already saved by this point,
    and an unindexed one is merely un-searchable, which the badge says out loud.

    Running several at once is prevented by the lock inside the script, not
    here. That lock is also what makes this safe to call on every add: the
    process that wins it asks the database what still needs doing, so it picks
    up the shabads that arrived while it was working.

    --max-spend is the other half of that safety. The script's normal job is
    "index everything outstanding", so without a ceiling, adding one shabad
    while a backlog exists would quietly spend several dollars. A background run
    is allowed small change and no more; clearing a backlog stays something you
    choose to do, having seen the price.

    Output goes to a log file, NOT to DEVNULL. A detached child that dies on
    startup -- a missing key, a bad import, a locked database -- leaves no trace
    otherwise, and "I added a shabad and nothing happened" becomes impossible to
    diagnose from inside the app. The log is the only place that failure can be
    seen, so it has to exist.
    """
    script = os.path.join(HERE, "search", "index_library.py")
    if not os.path.exists(script):
        return
    kw = {}
    if os.name == "nt":
        # no console window, and survives the server being closed
        kw["creationflags"] = (subprocess.CREATE_NO_WINDOW
                               | subprocess.DETACHED_PROCESS)
    else:
        kw["start_new_session"] = True
    try:
        log = open(INDEX_LOG, "a", encoding="utf-8", errors="replace")
        log.write(f"\n=== spawn {datetime.datetime.now():%Y-%m-%d %H:%M:%S} "
                  f"(python: {sys.executable}) ===\n")
        log.flush()
        cmd = [sys.executable, "-u", script, "--yes",
               "--max-spend", "%.4f" % budget]
        if model:
            cmd += ["--model", model]
        subprocess.Popen(cmd, cwd=HERE, stdout=log, stderr=subprocess.STDOUT,
                         stdin=subprocess.DEVNULL, **kw)
    except Exception as e:                 # never let this break adding a shabad
        print(f"auto-index could not start: {e}")
        try:
            with open(INDEX_LOG, "a", encoding="utf-8") as f:
                f.write(f"spawn failed: {type(e).__name__}: {e}\n")
        except OSError:
            pass


def index_estimate(conn, name):
    """What switching this model on would cost, without spending anything.

    Everything here is imported from search/ rather than reimplemented, and that
    matters more than it looks: if this screen priced the work with its own copy
    of the arithmetic, the number shown before agreeing and the number actually
    spent could drift apart silently. One implementation, two callers.

    Prices are fetched live because they move -- a figure cached at the time the
    model was registered is the wrong thing to put in front of a spend decision.
    If that call fails, `cost` comes back None and the dialog says so instead of
    guessing; a made-up price is worse than an honest blank.
    """
    search_dir = os.path.join(HERE, "search")
    if search_dir not in sys.path:
        sys.path.insert(0, search_dir)
    import index_library as ix
    from bench import fetch_prices, cost_for

    reg = ix.registry()
    if name not in reg:
        raise HTTPException(404, f"{name} is not in models.json")

    groups, covered, _ = ix.pending(conn, name, ix.prompt_version())
    tpl = reg[name].get("tokens_per_line", 1000)

    cost = None
    try:
        i, o = fetch_prices({reg[name]["id"]}).get(reg[name]["id"], (0.0, 0.0))
        if i or o:
            cost = cost_for(len(groups), tpl, i, o)
    except Exception as e:
        print(f"could not price {name}: {e}")

    total = conn.execute("SELECT COUNT(*) FROM lines").fetchone()[0]
    return {"model": name, "unique": len(groups), "lines": covered,
            "total": total, "tokens_per_line": tpl, "cost": cost,
            "balance": credit_balance(),
            # A run already in flight holds the lock, so a second one would exit
            # immediately without doing anything. Say that up front rather than
            # letting the catch-up silently not happen.
            "busy": bool(live_jobs(conn))}


def credit_info():
    """What OpenRouter says the account holds, or None if it can't be read.

    Shown next to an estimate because the two together answer the only question
    that matters at that moment -- can this run actually finish? A run that stops
    two thirds of the way through wastes nothing already paid for, but it is
    still better known in advance than discovered in the log.

    Never raises. This is a nice-to-have on every screen that shows it, and a
    network wobble at OpenRouter must not take out a page that is otherwise
    working entirely from local data.
    """
    try:
        search_dir = os.path.join(HERE, "search")
        if search_dir not in sys.path:
            sys.path.insert(0, search_dir)
        import requests
        from summarize import load_key
        r = requests.get("https://openrouter.ai/api/v1/credits", timeout=8,
                         headers={"Authorization": f"Bearer {load_key()}"})
        d = r.json().get("data", {})
        top, used = d.get("total_credits", 0), d.get("total_usage", 0)
        return {"purchased": round(top, 2), "spent": round(used, 2),
                "remaining": round(top - used, 2)}
    except Exception:
        return None                    # not worth surfacing; the estimate stands


def credit_balance():
    return (credit_info() or {}).get("remaining")


# How long a job may go without a heartbeat before it is presumed dead. Defined
# once because live_jobs and recent_jobs must agree: if one calls a job alive and
# the other calls it stalled, the badge and the control panel contradict each
# other over the same row.
JOB_SILENT_S = 600

EMBED_DIMS = 1024                      # BGE-M3 (CLAUDE.md §7)
EMBED_BYTES = EMBED_DIMS * 4           # float32
BACKUP_STALE_DAYS = 7
LOW_BALANCE = 1.00


def model_coverage(conn):
    """How much of the library each model has actually finished.

    Three numbers, not one, because the two phases fail independently and the
    difference is diagnostic. Summaries are bought from an API and cost money;
    embeddings are computed locally and are free. So `summarised > embedded`
    means the expensive half succeeded and the free half was interrupted -- a
    reassuring problem, fixed by --embed-only with no further spend. The reverse
    cannot happen.

    `stale` counts rows written by an older prompt. This is the query §5 says
    prompt_ver exists to make possible: without it, an improved prompt leaves a
    silent mix of old and new summaries and no way to tell them apart.
    """
    search_dir = os.path.join(HERE, "search")
    if search_dir not in sys.path:
        sys.path.insert(0, search_dir)
    try:
        import index_library as ix
        ver = ix.prompt_version()
    except Exception:
        ver = None

    total = conn.execute("SELECT COUNT(*) FROM lines").fetchone()[0]
    stats = {r["model"]: dict(r) for r in conn.execute(
        f"""SELECT model,
                   COUNT(*)                                            AS summarised,
                   SUM(embedding IS NOT NULL)                          AS embedded,
                   SUM(embedding IS NOT NULL
                       AND LENGTH(embedding) <> {EMBED_BYTES})         AS malformed,
                   SUM(prompt_ver <> ?)                                AS stale,
                   SUM(LENGTH(embedding))                              AS bytes
            FROM line_summaries GROUP BY model""", (ver or "",))}

    out = []
    for m in model_rows(conn):
        s = stats.pop(m["name"], {})
        done = min(s.get("summarised", 0) or 0, s.get("embedded", 0) or 0)
        out.append({**m, "total": total,
                    "summarised": s.get("summarised", 0) or 0,
                    "embedded": s.get("embedded", 0) or 0,
                    "malformed": s.get("malformed", 0) or 0,
                    "stale": (s.get("stale", 0) or 0) if ver else None,
                    "bytes": s.get("bytes", 0) or 0,
                    "coverage": (done / total) if total else 0})
    # Rows for models no longer in `models` -- summaries paid for and still on
    # disk, but invisible everywhere else in the app. Worth seeing here.
    for name, s in stats.items():
        out.append({"name": name, "label": name, "enabled": 0, "orphan": True,
                    "total": total, "summarised": s["summarised"] or 0,
                    "embedded": s["embedded"] or 0, "malformed": s["malformed"] or 0,
                    "stale": None, "bytes": s["bytes"] or 0, "coverage": 0})
    return out, ver


def lock_state():
    """Whether an indexer holds the machine-wide lock, and for how long.

    A lock with no live job behind it is the fingerprint of a killed process --
    a reboot mid-run, a closed terminal. It blocks every later run silently,
    which is exactly the kind of failure that is invisible from the app and
    obvious here.
    """
    path = os.path.join(HERE, "search", ".index.lock")
    if not os.path.exists(path):
        return {"held": False}
    age = time.time() - os.path.getmtime(path)
    return {"held": True, "age_s": int(age), "stale": age > 6 * 3600}


def backup_state():
    """Newest file in backups/, because §8 says backups are not optional.

    Nothing else in the app ever mentions backups, so if the nightly job was
    never set up -- or set up and quietly failing -- there is currently no
    surface anywhere that would say so. This is that surface.
    """
    d = os.path.join(HERE, "backups")
    try:
        files = [os.path.join(d, f) for f in os.listdir(d) if f.endswith(".db")]
    except OSError:
        return {"count": 0, "newest": None, "age_days": None}
    if not files:
        return {"count": 0, "newest": None, "age_days": None}
    newest = max(files, key=os.path.getmtime)
    age = (time.time() - os.path.getmtime(newest)) / 86400
    return {"count": len(files), "newest": os.path.basename(newest),
            "age_days": round(age, 1), "bytes": os.path.getsize(newest)}


def log_tail(n=40, cap=16384):
    """The end of the indexer's log.

    A detached child writes here and nowhere else, so when "I enabled a model
    and nothing happened" happens again, this is the only place the reason
    exists. Read from the end so the file can grow without bound.
    """
    try:
        size = os.path.getsize(INDEX_LOG)
        with open(INDEX_LOG, "rb") as f:
            f.seek(max(0, size - cap))
            text = f.read().decode("utf-8", "replace")
        lines = [l.rstrip() for l in text.splitlines() if l.strip()]
        return lines[-n:], size
    except OSError:
        return [], 0


def build_alerts(conn, models, jobs, lock, backups, credit, ver):
    """Everything currently wrong, worst first.

    Deliberately derived here rather than in the browser: these are judgements
    about the system ("a job that stopped is a problem, a job you stopped is
    not"), and they belong next to the data they judge. The page then only has
    to render a list, which is why it stays simple as the checks multiply.
    """
    a = []
    def add(level, title, detail):
        a.append({"level": level, "title": title, "detail": detail})

    if credit is None:
        add("warn", "Cannot reach OpenRouter",
            "Balance unknown. Indexing may fail; the key or the network is the "
            "usual cause.")
    elif credit["remaining"] <= 0:
        add("error", "Out of credit",
            f"${credit['remaining']:.2f} remaining. Indexing will fail until "
            "you top up.")
    elif credit["remaining"] < LOW_BALANCE:
        add("warn", "Low balance",
            f"${credit['remaining']:.2f} remaining.")

    for j in jobs:
        if j["state"] == "failed":
            add("error", f"Indexing failed — {j['model']}",
                (j.get("error") or "no reason recorded")[:300])
        elif j["state"] == "stalled":
            add("error", f"Indexing stopped without finishing — {j['model']}",
                f"{j['done']}/{j['total']} done, ${j['spent']:.2f} spent, then "
                "silent. Nothing paid for was lost; run it again to carry on.")

    for m in models:
        if m.get("orphan") and m["summarised"]:
            add("info", f"Summaries with no model — {m['name']}",
                f"{m['summarised']:,} rows for a model no longer registered. "
                "They cost real money, so they are kept, not deleted.")
            continue
        gap = m["summarised"] - m["embedded"]
        if gap > 0:
            add("warn", f"Summaries without vectors — {m['label']}",
                f"{gap:,} lines are summarised but not embedded, so they cannot "
                "be searched. Free to fix: run index_library.py --embed-only.")
        if m["malformed"]:
            add("error", f"Malformed vectors — {m['label']}",
                f"{m['malformed']:,} embeddings are not {EMBED_BYTES} bytes. "
                "Those rows are corrupt and should be re-embedded.")
        if m["stale"]:
            add("warn", f"Summaries from an older prompt — {m['label']}",
                f"{m['stale']:,} rows predate the current prompt ({ver}). They "
                "still work; regenerating them costs the usual per-line price.")
        if m["enabled"] and not m["summarised"]:
            add("warn", f"Switched on but not indexed — {m['label']}",
                "It contributes nothing to search until it has summaries.")

    if lock.get("stale"):
        add("error", "Stale indexing lock",
            f"Held for {lock['age_s'] // 3600}h with nothing running — a killed "
            "process. It blocks new runs until removed.")

    if backups["age_days"] is None:
        add("error", "No backups",
            "Nothing in backups/. §5's 'mine' layer — status, notes, tags, "
            "votes — can be regenerated by nothing.")
    elif backups["age_days"] > BACKUP_STALE_DAYS:
        add("warn", "Backup is old",
            f"Newest is {backups['age_days']:.0f} days old.")

    orphans = conn.execute(
        "SELECT COUNT(*) FROM shabads s WHERE NOT EXISTS "
        "(SELECT 1 FROM lines l WHERE l.shabad_id = s.id)").fetchone()[0]
    if orphans:
        add("warn", "Shabads with no lines",
            f"{orphans} saved shabad(s) have no verses — a BaniDB fetch that "
            "did not complete. §12: the gap is surfaced, not filled in.")
    return a


def recent_jobs(conn, limit=5):
    """The last few indexing runs, with 'running' reinterpreted honestly.

    A row says `running` because that is what the process wrote when it started;
    it says nothing about whether the process still exists. A killed run leaves
    that row claiming to run forever. So a heartbeat older than the cutoff is
    reported as `stalled` -- a distinct state, because "still going" and "died
    without saying so" need different responses from the reader, and the raw
    column cannot tell them apart.
    """
    rows = [dict(r) for r in conn.execute(
        """SELECT id, model, state, phase, total, done, spent, error,
                  started_at, updated_at,
                  CAST((julianday('now') - julianday(updated_at)) * 86400 AS INT)
                    AS quiet_s
           FROM index_jobs ORDER BY id DESC LIMIT ?""", (limit,))]
    for r in rows:
        if r["state"] == "running" and (r["quiet_s"] or 0) > JOB_SILENT_S:
            r["state"] = "stalled"
    return rows


@app.get("/api/status")
def get_status():
    """One call, everything the control panel shows.

    Deliberately a single endpoint rather than six. The page is a snapshot of
    one moment; assembled from six requests it could show a job as running in
    one panel and finished in another, and a diagnostics screen that contradicts
    itself is worse than no diagnostics screen.

    Read-only throughout. Nothing here starts, stops, or repairs anything --
    diagnosing and acting are separate, so that opening this page is always safe.
    """
    conn = library()
    try:
        models, ver = model_coverage(conn)
        jobs = recent_jobs(conn, 12)
        lock = lock_state()
        backups = backup_state()
        credit = credit_info()
        log, log_bytes = log_tail()

        counts = {}
        for key, sql in (
                ("shabads",   "SELECT COUNT(*) FROM shabads"),
                ("lines",     "SELECT COUNT(*) FROM lines"),
                ("tags",      "SELECT COUNT(*) FROM tags"),
                ("shortlist", "SELECT COUNT(*) FROM shortlist"),
                ("history",   "SELECT COUNT(*) FROM history"),
                ("learning",  "SELECT COUNT(*) FROM learning"),
                ("votes",     "SELECT COUNT(*) FROM line_relations"),
                ("user_added", "SELECT COUNT(*) FROM shabads WHERE is_user_added = 1"),
                ("no_teeka",  "SELECT COUNT(*) FROM lines WHERE teeka_pa IS NULL OR teeka_pa = ''"),
                ("no_english", "SELECT COUNT(*) FROM lines WHERE translation_en IS NULL OR translation_en = ''"),
        ):
            try:
                counts[key] = conn.execute(sql).fetchone()[0]
            except sqlite3.Error:
                counts[key] = None      # table not migrated yet; not fatal here

        return {
            "alerts": build_alerts(conn, models, jobs, lock, backups, credit, ver),
            "account": credit,
            "models": models,
            "jobs": jobs,
            "lock": lock,
            "backups": backups,
            "library": counts,
            "prompt_ver": ver,
            "auto_index": get_setting(conn, "auto_index") == "1",
            "storage": {
                "library_db": file_size(LIBRARY_DB),
                "corpus_db": file_size(CORPUS_DB),
                "vectors": sum(m["bytes"] for m in models),
                "log": log_bytes,
            },
            "log": log,
            "now": datetime.datetime.now().isoformat(timespec="seconds"),
        }
    finally:
        conn.close()


@app.post("/api/backup")
def run_backup():
    """Run tools/backup.py and hand back exactly what it printed.

    The one action allowed on an otherwise read-only page, because it is the one
    that cannot make anything worse: it only ever adds files, running it twice is
    harmless, and it is precisely what the "no backups" alert tells you to do.
    Everything else there stays diagnosis-only.

    SYNCHRONOUS, unlike spawn_indexer. That is not an inconsistency -- indexing
    runs for hours and must not block a page load, while this takes a couple of
    seconds, and waiting means the answer can be the real filenames and sizes
    rather than a hopeful "started". A backup you were told about but that
    silently failed is worse than no button at all.

    The script's own stdout is returned verbatim rather than being summarised.
    It already reports what it wrote and what it pruned; re-wording that here
    would be a second description of the same event, free to drift from the first.
    """
    script = os.path.join(HERE, "tools", "backup.py")
    if not os.path.exists(script):
        raise HTTPException(500, "tools/backup.py is missing")
    try:
        p = subprocess.run([sys.executable, "-u", script], cwd=HERE,
                           capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=300)
    except subprocess.TimeoutExpired:
        raise HTTPException(504, "backup timed out after 5 minutes")
    except OSError as e:
        raise HTTPException(500, f"could not run backup.py: {e}")
    return {"ok": p.returncode == 0, "code": p.returncode,
            "output": (p.stdout + p.stderr).strip() or "(no output)"}


def file_size(path):
    try:
        return os.path.getsize(path)
    except OSError:
        return None


def live_jobs(conn):
    """Jobs genuinely in flight right now.

    `state = 'running'` alone is not enough: a killed process never writes a
    final state, so the row would claim to be indexing forever. The heartbeat in
    updated_at is the real signal, and anything silent for ten minutes is
    treated as gone. The indexer sweeps those rows on its next start; this only
    has to avoid believing them meanwhile.
    """
    return [dict(r) for r in conn.execute(
        """SELECT id, model, state, phase, total, done, spent, started_at, updated_at
           FROM index_jobs
           WHERE state = 'running'
             AND updated_at >= datetime('now', ?)
           ORDER BY id""", (f"-{JOB_SILENT_S} seconds",))]


def indexing_status(conn, shabad_id):
    """How far the derived layer (CLAUDE.md §5) has got, per model.

    Three states, per model and overall:
        none  no line indexed at all
        part  some lines done, or some models done and others not
        done  every line summarised AND embedded

    Only ENABLED models decide the overall state. A model switched off in
    Settings would otherwise hold the badge amber forever, which reads as "still
    working" when the truth is "deliberately not doing that one". Disabled
    models are still listed, so the popup shows the whole picture.

    A line counts as done only when it has both a summary and a vector: a
    summary with no embedding is invisible to search, so calling it indexed
    would be a lie in exactly the case that matters.
    """
    total = conn.execute("SELECT COUNT(*) FROM lines WHERE shabad_id = ?",
                         (shabad_id,)).fetchone()[0]
    if not total:
        return None

    counts = {r["model"]: r for r in conn.execute(
        """SELECT ls.model,
                  SUM(CASE WHEN ls.summary <> '' THEN 1 ELSE 0 END)     AS summarised,
                  SUM(CASE WHEN ls.embedding IS NOT NULL THEN 1 ELSE 0 END) AS embedded
           FROM line_summaries ls
           JOIN lines l ON l.id = ls.line_id
           WHERE l.shabad_id = ?
           GROUP BY ls.model""", (shabad_id,))}

    models = []
    for m in model_rows(conn):
        c = counts.get(m["name"])
        summarised = (c["summarised"] if c else 0) or 0
        embedded = (c["embedded"] if c else 0) or 0
        done = min(summarised, embedded)
        models.append({**m, "summarised": summarised, "embedded": embedded,
                       "total": total,
                       "state": "done" if done >= total else "part" if done else "none"})

    enabled = [m for m in models if m["enabled"]]
    if enabled and all(m["state"] == "done" for m in enabled):
        overall = "done"
    elif any(m["state"] != "none" for m in enabled):
        overall = "part"
    else:
        overall = "none"

    # A run in flight outranks the count. "Half done and working on it" and
    # "half done and abandoned" are the same number but not the same situation,
    # and only one of them is worth waiting for.
    jobs = live_jobs(conn)
    if jobs and overall != "done":
        overall = "running"
    return {"total": total, "state": overall, "models": models, "jobs": jobs}


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


# How long a shabad is, in bands rather than an exact number.
#
# Three, not a slider. The question being asked is "is this short enough to
# fit the programme", which does not deserve single-line precision, and bands
# render as ordinary chips so they need no new control on any of the three
# panels that show filters.
#
# Multi-select is what makes one control cover every case: short+medium is a
# maximum of 16, medium+long is a minimum of 9, medium alone is a band. So the
# "I might want a minimum later" case is already built, with nothing to add.
#
# Boundaries from the real distribution -- median 11, p90 25 -- which splits the
# library roughly 29 / 46 / 25.
LENGTH_BANDS = (
    ("short",  "Short",  1,  8),
    ("medium", "Medium", 9,  16),
    ("long",   "Long",   17, 10_000),
)
LENGTH_BY_ID = {b[0]: b for b in LENGTH_BANDS}

# Counting lines per shabad is a correlated subquery; naming it once keeps the
# library, the deck and the similar page using the identical expression.
LINE_COUNT_SQL = "(SELECT COUNT(*) FROM lines l2 WHERE l2.shabad_id = s.id)"


def filter_clauses(status, rarity, genre, speed, raag, writer, length=None):
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

    # Bands are OR'd with each other and AND'd with everything else: picking
    # Short and Long means either of those, not neither.
    bands = [LENGTH_BY_ID[v] for v in (length or []) if v in LENGTH_BY_ID]
    if bands:
        parts = []
        for _, _, lo, hi in bands:
            parts.append(f"{LINE_COUNT_SQL} BETWEEN ? AND ?")
            args += [lo, hi]
        where.append("(" + " OR ".join(parts) + ")")
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
    length: Optional[List[str]] = Query(None),
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

    fwhere, fargs = filter_clauses(status, rarity, genre, speed, raag, writer, length)
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
                # id comes along so the card can open the shabad ON the matched
                # line rather than dropping you at the line it is filed under
                f"""SELECT id AS line_id, shabad_id, line_no, gurmukhi,
                           translation_en, first_letters
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
    lrn = conn.execute("SELECT 1 FROM learning WHERE shabad_id = ?",
                       (shabad_id,)).fetchone() is not None
    # How far the derived layer (CLAUDE.md §5) has got for this shabad. Reads
    # zero for everything until the summary/embedding pipeline exists, which is
    # exactly what makes it a useful progress indicator for that work.
    ix = indexing_status(conn, shabad_id)
    conn.close()
    out = dict(row)
    out["lines"] = [{k: v for k, v in l.items() if k != "embedding"} for l in lines]
    out["tags"] = tags
    out["shortlisted"] = shortlisted
    out["learning"] = lrn
    out["indexing"] = ix
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
        auto = get_setting(conn, "auto_index") == "1"
    finally:
        conn.close()
    # after the connection is closed, so the child never contends for the write
    if auto:
        spawn_indexer()
    return {"id": new_id, "lines": len(verses), "indexing_started": auto}


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

    # Bands come from the server with their boundaries and live counts, so the
    # UI renders whatever is defined here and the two can never disagree about
    # where "Short" ends. Objects rather than bare strings: the chip needs a
    # label ("Short 1-8") that is not the value it posts back ("short").
    out["length"] = [
        {"value": vid,
         "label": f"{label} {lo}+" if hi > 999 else f"{label} {lo}–{hi}",
         "count": conn.execute(
             f"SELECT COUNT(*) FROM shabads s WHERE {LINE_COUNT_SQL} BETWEEN ? AND ?",
             (lo, hi)).fetchone()[0]}
        for vid, label, lo, hi in LENGTH_BANDS]

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
    length: Optional[List[str]] = Query(None),
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

    fwhere, fargs = filter_clauses(status, rarity, genre, speed, raag, writer, length)
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
# A LIST, and tools to test yourself with. Nothing more.
#
# This replaced a full SM-2 implementation: per-line ease factors, six scaffold
# levels, a rahao meaning gate, daily new-material caps, interval scheduling.
# It was carefully built and never used once. The scheduling was the problem --
# being told what to practise and when turns a thing you want to do into a thing
# you are behind on, and the honest response to that is to delete it rather than
# to tune it.
#
# So: no due dates, no levels, no gates, no caps, no streaks. Add a shabad, and
# test yourself on it whenever you feel like it, in whichever mode you feel like.
# The only thing recorded is when you last practised, and that is information,
# never a schedule.


# Where a shabad is in your head, set by hand. Three, not five: the whole point
# of removing the scheduler was to stop the app having opinions, and a scale fine
# enough to agonise over is an opinion by another route.
#
# Ordered worst-first so the list opens on what still needs work rather than on
# what is already done.
LEARN_STATES = ("not_started", "in_progress", "memorized")


@app.get("/api/learning")
def list_learning(status: Optional[List[str]] = Query(None)):
    """Everything being memorised. Unstarted first, then newest."""
    where, args = [], []
    if status:
        keep = [s for s in status if s in LEARN_STATES]
        if keep:
            where.append(f"lg.status IN ({','.join('?' * len(keep))})")
            args += keep
    clause = f"WHERE {' AND '.join(where)}" if where else ""
    conn = library()
    try:
        rows = decorate(conn, [dict(r) for r in conn.execute(
            f"""SELECT s.*, lg.added_at, lg.last_practised, lg.status AS learn_status,
                       (SELECT COUNT(*) FROM lines l WHERE l.shabad_id = s.id) line_count
                FROM learning lg JOIN shabads s ON s.id = lg.shabad_id
                {clause}
                ORDER BY CASE lg.status WHEN 'not_started' THEN 0
                                        WHEN 'in_progress' THEN 1 ELSE 2 END,
                         lg.added_at DESC""", args)])
        counts = {s: 0 for s in LEARN_STATES}
        for r in conn.execute("SELECT status, COUNT(*) FROM learning GROUP BY status"):
            counts[r[0]] = r[1]
    finally:
        conn.close()
    return {"count": len(rows), "shabads": rows, "counts": counts,
            "total": sum(counts.values())}


class LearnStatus(BaseModel):
    status: str


@app.patch("/api/learning/{shabad_id}")
def set_learn_status(shabad_id: int, body: LearnStatus):
    if body.status not in LEARN_STATES:
        raise HTTPException(400, f"status must be one of {', '.join(LEARN_STATES)}")
    conn = library(write=True)
    try:
        with conn:
            cur = conn.execute("UPDATE learning SET status = ? WHERE shabad_id = ?",
                               (body.status, shabad_id))
        if not cur.rowcount:
            raise HTTPException(404, "not being learned")
    finally:
        conn.close()
    return {"shabad_id": shabad_id, "status": body.status}


@app.post("/api/learning/{shabad_id}")
def add_to_learning(shabad_id: int):
    conn = library(write=True)
    try:
        if not conn.execute("SELECT 1 FROM shabads WHERE id = ?", (shabad_id,)).fetchone():
            raise HTTPException(404, "no such shabad")
        with conn:
            conn.execute("INSERT OR IGNORE INTO learning (shabad_id) VALUES (?)",
                         (shabad_id,))
        total = conn.execute("SELECT COUNT(*) FROM learning").fetchone()[0]
    finally:
        conn.close()
    return {"added": shabad_id, "learning_count": total}


@app.delete("/api/learning/{shabad_id}")
def remove_from_learning(shabad_id: int):
    """Just removes it from the list. There is no progress to lose."""
    conn = library(write=True)
    try:
        with conn:
            cur = conn.execute("DELETE FROM learning WHERE shabad_id = ?", (shabad_id,))
        if not cur.rowcount:
            raise HTTPException(404, "not being learned")
        total = conn.execute("SELECT COUNT(*) FROM learning").fetchone()[0]
    finally:
        conn.close()
    return {"removed": shabad_id, "learning_count": total}


@app.get("/api/learning/{shabad_id}/lines")
def learning_lines_for(shabad_id: int):
    """Every line of one shabad, with what each practice mode needs.

    ALL lines, headers included (CLAUDE.md §3). Line 1 is usually the raag and
    mahalla rather than a real tuk, but not always -- and a heuristic that
    skipped it would misfire exactly on the shabads that open on a true tuk.
    One extra line costs nothing to page past.
    """
    conn = library()
    try:
        sh = conn.execute("SELECT * FROM shabads WHERE id = ?", (shabad_id,)).fetchone()
        if not sh:
            raise HTTPException(404, "no such shabad")
        lines = [dict(r) for r in conn.execute(
            """SELECT id, line_no, gurmukhi, transliteration_en, translation_en,
                      teeka_pa, first_letters
               FROM lines WHERE shabad_id = ? ORDER BY line_no""", (shabad_id,))]
        learning = conn.execute("SELECT 1 FROM learning WHERE shabad_id = ?",
                                (shabad_id,)).fetchone() is not None
    finally:
        conn.close()
    return {"shabad": dict(sh), "lines": lines, "learning": learning}


@app.post("/api/learning/{shabad_id}/practised")
def mark_practised(shabad_id: int):
    """Stamp the last-practised date. Informational -- nothing schedules on it."""
    conn = library(write=True)
    try:
        with conn:
            cur = conn.execute(
                "UPDATE learning SET last_practised = datetime('now') WHERE shabad_id = ?",
                (shabad_id,))
        if not cur.rowcount:
            raise HTTPException(404, "not being learned")
    finally:
        conn.close()
    return {"shabad_id": shabad_id, "practised": True}


@app.get("/api/quiz/{line_id}")
def line_quiz(line_id: int):
    """Multiple choice: which English meaning belongs to this line?

    Distractors are real translations of other lines, never invented ones -- so
    a wrong answer is still Gurbani, and picking the right one means genuinely
    telling two meanings apart.

    Pulled from the most SIMILAR lines where vectors exist (§3): near-misses
    force real discrimination, where four random lines from across the library
    can usually be dismissed on subject alone.
    """
    conn = library()
    try:
        line = conn.execute("SELECT * FROM lines WHERE id = ?", (line_id,)).fetchone()
        if not line:
            raise HTTPException(404, "no such line")

        others = []
        models = models_enabled(conn)
        if models:
            near = similar_for_model(conn, models[0]["name"], line_id, 40)
            ids = [rid for rid, _, _ in near]
            if ids:
                ph = ",".join("?" * len(ids))
                others = [r[0] for r in conn.execute(
                    f"""SELECT translation_en FROM lines
                        WHERE id IN ({ph}) AND shabad_id <> ?
                          AND translation_en IS NOT NULL AND translation_en <> ''
                        LIMIT 3""", (*ids, line["shabad_id"]))]

        if len(others) < 3:                      # not indexed yet, or too few
            others += [r[0] for r in conn.execute(
                """SELECT translation_en FROM lines
                   WHERE shabad_id <> ? AND translation_en IS NOT NULL
                     AND translation_en <> '' ORDER BY RANDOM() LIMIT ?""",
                (line["shabad_id"], 3 - len(others)))]
    finally:
        conn.close()

    options = [line["translation_en"], *others[:3]]
    random.shuffle(options)
    return {"line_id": line_id, "gurmukhi": line["gurmukhi"],
            "teeka_pa": line["teeka_pa"], "options": options,
            "answer": line["translation_en"]}


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

# --- similarity search + model comparison ------------------------------------
#
# Nothing here calls a model. Every vector it reads was written by the indexing
# pass in search/, so a query is arithmetic over numbers already on disk
# (CLAUDE.md §7). Until that pass has run, these endpoints correctly return
# nothing, and say why rather than returning a bare empty list.

class IndexRequest(BaseModel):
    # The ceiling the caller agreed to, in dollars. None means "use the server's
    # own estimate" -- the client is never trusted to invent a larger number, it
    # only ever echoes back the figure it was shown.
    budget: Optional[float] = None


class Relation(BaseModel):
    query_line_id: int
    result_line_id: int
    verdict: int                       # +1 connected, -1 unrelated, 0 clears


class ModelPatch(BaseModel):
    enabled: bool


# model -> (count_at_load, [line_id], matrix, checked_at). 5,530 x 1024 float32
# is ~23 MB per model, far too much to read per request, so it is held in memory.
# Invalidated by row count, which catches new rows; the indexing pass must call
# clear_vector_cache() itself if it ever REPLACES vectors in place.
_VECTORS = {}

# How often the row count is actually re-checked.
#
# Measured: the count itself takes 15.5 ms while the similarity maths it guards
# takes 0.7 ms -- twenty times the cost of the work. line_summaries is WITHOUT
# ROWID, so the row IS the index entry, embedding included; there is no way to
# count rows without paging 23 MB of vectors past the disk. No index helps.
#
# So it is checked on a timer instead. The only writer of vectors is the indexer,
# a separate process, so the exposure is: for up to a minute after a run
# finishes, similarity may not yet see the newest lines. That is invisible in
# practice -- the page has to be reloaded to show them anyway -- and it makes
# changing a filter cost a fraction of a millisecond instead of 16.
VECTOR_RECHECK_S = 60


def clear_vector_cache():
    _VECTORS.clear()


def _vectors_for(conn, model):
    hit = _VECTORS.get(model)
    now = time.monotonic()
    if hit and now - hit[3] < VECTOR_RECHECK_S:
        return hit[1], hit[2]

    n = conn.execute("SELECT COUNT(*) FROM line_summaries "
                     "WHERE model = ? AND embedding IS NOT NULL", (model,)).fetchone()[0]
    if hit and hit[0] == n:
        _VECTORS[model] = (n, hit[1], hit[2], now)      # unchanged; reset the clock
        return hit[1], hit[2]
    if not n:
        return [], None

    import numpy as np
    rows = conn.execute("SELECT line_id, embedding FROM line_summaries "
                        "WHERE model = ? AND embedding IS NOT NULL "
                        "ORDER BY line_id", (model,)).fetchall()
    ids = [r["line_id"] for r in rows]
    mat = np.frombuffer(b"".join(r["embedding"] for r in rows),
                        dtype="<f4").reshape(len(ids), -1)
    # normalised once here, so every query is a plain dot product
    mat = mat / np.linalg.norm(mat, axis=1, keepdims=True)
    _VECTORS[model] = (n, ids, mat, now)
    return ids, mat


def similar_for_model(conn, model, line_id, limit, allowed=None):
    """The nearest lines to `line_id` under one model, best first.

    §7: RANK, never threshold. In 1024 dimensions unrelated vectors sit near
    perpendicular, so real scores bunch in a narrow band -- a hardcoded cutoff
    like `sim > 0.8` returns nothing at all. Sort and take the top N.

    `allowed` is a set of line ids the filters permit. Filtering happens BEFORE
    the slice, not after: the whole library is scored and ordered either way, so
    taking the best `limit` OF THE MATCHING ONES costs nothing extra and always
    returns a full page. Filtering the top 20 after the fact would instead return
    however few of those 20 happened to match, and "show more" would be a
    workaround for a self-inflicted problem.

    Deliberately NO offset here. Paging belongs to the merged list, not to one
    model: with an offset per model, a line that model A puts on page 2 can be
    one model B already showed on page 1, and the same result appears twice.
    Measured on a real query before this was moved -- page 2 repeated one row,
    page 3 repeated three. The caller takes each model's top N and pages the
    merge instead.

    Returns (line_id, score, true_rank). `true_rank` is the position in the
    UNFILTERED ordering, which is what model_results must record: a rank that
    moved because a filter was on would make the model comparison depend on how
    the reader happened to have the UI configured.

    A full argsort of 5,530 costs 0.75 ms measured -- argpartition shaves 0.1 ms
    and loses the true ranks, so the sort stays.
    """
    ids, mat = _vectors_for(conn, model)
    if mat is None or line_id not in ids:
        return []
    import numpy as np
    i = ids.index(line_id)
    sims = mat @ mat[i]
    sims[i] = -9                                   # never match a line to itself

    order = np.argsort(-sims)
    ranks = np.empty(len(sims), dtype=np.int32)
    ranks[order] = np.arange(len(sims))            # ranks[j] = j's place overall

    if allowed is not None:
        keep = np.fromiter((lid in allowed for lid in ids), bool, len(ids))
        order = order[keep[order]]                 # drops rows, keeps the order
    return [(ids[j], float(sims[j]), int(ranks[j])) for j in order[:limit]]


class SettingPatch(BaseModel):
    value: str


@app.get("/api/indexing")
def get_indexing():
    """What the indexer is doing, for the badge to poll while a run is live."""
    conn = library()
    try:
        return {"running": live_jobs(conn), "recent": recent_jobs(conn)}
    finally:
        conn.close()


@app.get("/api/settings")
def list_settings():
    conn = library()
    try:
        stored = {r["key"]: r["value"] for r in conn.execute("SELECT key, value FROM settings")}
        return {"settings": {**DEFAULT_SETTINGS, **stored}}
    finally:
        conn.close()


@app.patch("/api/settings/{key}")
def patch_setting(key: str, body: SettingPatch):
    if key not in DEFAULT_SETTINGS:
        raise HTTPException(404, f"no setting named {key}")
    conn = library(write=True)
    try:
        with conn:
            conn.execute("INSERT INTO settings (key, value) VALUES (?,?) "
                         "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                         (key, body.value))
        return {"key": key, "value": body.value}
    finally:
        conn.close()


@app.get("/api/models")
def list_models():
    conn = library()
    try:
        return {"models": model_rows(conn)}
    finally:
        conn.close()


def request_stop():
    """Ask any running indexer to stop, cooperatively.

    Writes the flag file search/.index.stop. The indexer checks it after every
    completed line and again after every saved chunk, so it stops within
    seconds, having kept everything already bought and released its own lock.

    Deliberately not a kill. The lock file holds the process id, but that id is
    not trustworthy for this -- pids are recycled, and killing a recycled one
    means killing something unrelated. A flag also stops it at a point of its own
    choosing, which is the difference between losing the current chunk and
    keeping it.
    """
    path = os.path.join(HERE, "search", ".index.stop")
    try:
        with open(path, "w", encoding="utf-8") as f:
            f.write(datetime.datetime.now().isoformat(timespec="seconds"))
        return True
    except OSError as e:
        print(f"could not write stop flag: {e}")
        return False


@app.patch("/api/models/{name}")
def patch_model(name: str, body: ModelPatch):
    """Switching a model off also stops any run indexing it.

    These were separate actions and should not have been. Switching off already
    means "stop spending on this"; leaving a run going that is doing exactly
    that makes the switch a lie, and the only way to act on it was a terminal.
    The reverse does not apply -- switching ON never starts anything by itself,
    because that spends money and needs the priced confirmation first.
    """
    conn = library(write=True)
    try:
        with conn:
            n = conn.execute("UPDATE models SET enabled = ? WHERE name = ?",
                             (1 if body.enabled else 0, name)).rowcount
        if not n:
            raise HTTPException(404, f"no model named {name}")

        stopped = None
        if not body.enabled:
            job = next((j for j in live_jobs(conn) if j["model"] == name), None)
            if job and request_stop():
                stopped = {"done": job["done"], "total": job["total"],
                           "spent": job["spent"]}
        return {"name": name, "enabled": body.enabled, "stopped": stopped}
    finally:
        conn.close()


@app.get("/api/models/{name}/estimate")
def model_estimate(name: str):
    """Priced before the switch is flipped, not after.

    Switching a model on is the one action in this app that spends real money,
    and the amount is not guessable from the UI -- it depends on how much of the
    library that particular model has already done, which could be all of it or
    none. So the number goes on screen before the decision, never after.
    """
    conn = library()
    try:
        return index_estimate(conn, name)
    finally:
        conn.close()


@app.post("/api/models/{name}/index")
def index_model(name: str, body: IndexRequest):
    """Start a catch-up run for one model.

    The ceiling is the estimate the user just approved plus 10%. The margin
    exists so the run isn't cut off a few lines short by rounding or a small
    price move; the ceiling itself exists so that a LARGE price move cannot turn
    an agreed $3.75 into something else entirely. Consent was given to a number,
    and the number is enforced.
    """
    conn = library()
    try:
        est = index_estimate(conn, name)
    finally:
        conn.close()
    if not est["unique"]:
        return {**est, "started": False, "reason": "nothing outstanding"}
    if est["busy"]:
        return {**est, "started": False, "reason": "indexing already running"}

    # The server's own estimate is the cap. A caller may ask for LESS -- index a
    # dollar's worth and see how it goes -- but never for more, so the ceiling
    # can't be widened by editing the request. With no live price to work from,
    # fall back to the small automatic allowance rather than running uncapped:
    # spending too little is recoverable, spending too much is not.
    ceiling = est["cost"] or AUTO_INDEX_BUDGET
    budget = min(body.budget, ceiling) if body.budget else ceiling
    budget = round(budget * 1.10, 4)
    spawn_indexer(model=name, budget=budget)
    return {**est, "started": True, "budget": budget}


def models_enabled(conn):
    return [m for m in model_rows(conn) if m["enabled"]]


def allowed_line_ids(conn, status, rarity, genre, speed, raag, writer, length):
    """Line ids whose shabad passes the filters, or None if none are set.

    Built with filter_clauses, the same function the library list and the deck
    use, so a filter cannot mean one thing on one screen and something else on
    another. None rather than "every id" on purpose: it lets the caller skip
    building a 5,530-element mask in the common case where nothing is filtered.
    """
    where, args = filter_clauses(status, rarity, genre, speed, raag, writer, length)
    if not where:
        return None
    return {r[0] for r in conn.execute(
        f"""SELECT l.id FROM lines l JOIN shabads s ON s.id = l.shabad_id
            WHERE {' AND '.join(where)}""", args)}


@app.get("/api/similar/{line_id}")
def get_similar(
    line_id: int,
    limit: int = SIMILAR_LIMIT,
    offset: int = 0,
    status: Optional[List[str]] = Query(None),
    rarity: Optional[List[str]] = Query(None),
    genre: Optional[List[str]] = Query(None),
    speed: Optional[List[str]] = Query(None),
    raag: Optional[List[str]] = Query(None),
    writer: Optional[List[str]] = Query(None),
    length: Optional[List[str]] = Query(None),
):
    """Lines related in meaning to this one, merged across enabled models.

    One row per RESULT LINE, never one per model: a line three models returned
    is shown once and judged once, and every model that offered it shares the
    verdict. `by` carries each model's rank so credit can be weighted by how
    prominently it was offered.

    Sorted by score, descending (§3) -- best match at the top, where it belongs.
    Blindness is kept by hiding WHICH model said what, not by scrambling the
    order: a list in random order is useless for the thing this page is actually
    for, which is finding the related line.

    Caveat worth knowing: scores from different models are not strictly
    comparable. Every vector comes from BGE-M3, but each model's summaries have
    their own style, and a model that writes more uniformly will score its own
    matches higher across the board. So 0.99 from one model and 0.89 from
    another does not reliably mean the first match is better. If that starts to
    skew the list, sort on best RANK instead -- a #1 is a #1 whoever said it.
    """
    conn = library(write=True)      # writes model_results: a record of what was shown
    try:
        q = conn.execute(
            """SELECT l.id, l.gurmukhi, l.translation_en, l.teeka_pa, l.line_no,
                      s.id AS shabad_id, s.source_line, s.raag_en, s.writer, s.ang
               FROM lines l JOIN shabads s ON s.id = l.shabad_id
               WHERE l.id = ?""", (line_id,)).fetchone()
        if not q:
            raise HTTPException(404, "no such line")

        allowed = allowed_line_ids(conn, status, rarity, genre, speed,
                                   raag, writer, length)
        # A filter combination nothing matches is not the same as an unindexed
        # line, and saying "not indexed yet" there would send you off to run the
        # indexer over a library that is already complete.
        if allowed is not None and not allowed:
            return {"query": dict(q), "results": [], "models": models_enabled(conn),
                    "reason": "no match", "more": False}

        models = models_enabled(conn)
        # Each model's top (offset+limit+1), then page the MERGE. Any line in the
        # true merged top-N must be in some model's own top-N, so this prefix is
        # complete; the +1 is what makes "is there a next page" answerable
        # without counting the whole matching set.
        need = offset + limit + 1
        merged = {}
        for m in models:
            for rid, score, rank in similar_for_model(
                    conn, m["name"], line_id, need, allowed):
                merged.setdefault(rid, {})[m["name"]] = {"rank": rank, "score": score}

        ordered = sorted(merged.items(),
                         key=lambda kv: (-max(v["score"] for v in kv[1].values()),
                                         min(v["rank"] for v in kv[1].values())))
        more = len(ordered) > offset + limit
        page = ordered[offset:offset + limit]

        if not page:
            # Past the end of the list is an ordinary outcome of Show more, not
            # a fault. Only an empty FIRST page means nothing has been indexed.
            return {"query": dict(q), "results": [], "models": models,
                    "reason": "not indexed yet" if not offset else "no more",
                    "more": False}

        rows = {r["id"]: dict(r) for r in conn.execute(
            f"""SELECT l.id, l.gurmukhi, l.translation_en, l.teeka_pa, l.line_no,
                       s.id AS shabad_id, s.source_line, s.source_line_no,
                       s.raag_en, s.writer, s.ang,
                       (SELECT COUNT(*) FROM lines x WHERE x.shabad_id = s.id)
                         AS line_count
                FROM lines l JOIN shabads s ON s.id = l.shabad_id
                WHERE l.id IN ({','.join('?' * len(page))})""",
            tuple(rid for rid, _ in page))}

        verdicts = {r["result_line_id"]: r["verdict"] for r in conn.execute(
            "SELECT result_line_id, verdict FROM line_relations WHERE query_line_id = ?",
            (line_id,))}

        # Only what was actually put in front of me, at its true unfiltered rank.
        # Recording the whole prefix would credit models for results this page
        # never showed.
        with conn:
            conn.executemany(
                "INSERT OR REPLACE INTO model_results "
                "(query_line_id, result_line_id, model, rank) VALUES (?,?,?,?)",
                [(line_id, rid, name, d["rank"])
                 for rid, by in page for name, d in by.items()])

        # `page` is already in merged order; do not re-sort.
        results = [{**rows[rid], "by": by, "verdict": verdicts.get(rid, 0),
                    "score": max(v["score"] for v in by.values()),
                    "best_rank": min(v["rank"] for v in by.values())}
                   for rid, by in page if rid in rows]
        return {"query": dict(q), "results": results, "models": models,
                "reason": None, "more": more, "offset": offset}
    finally:
        conn.close()


@app.post("/api/relations")
def post_relation(body: Relation):
    """Record a judgement. verdict 0 deletes it, so a mis-tap is undoable."""
    conn = library(write=True)
    try:
        with conn:
            if body.verdict == 0:
                conn.execute("DELETE FROM line_relations "
                             "WHERE query_line_id = ? AND result_line_id = ?",
                             (body.query_line_id, body.result_line_id))
            else:
                conn.execute(
                    """INSERT INTO line_relations (query_line_id, result_line_id, verdict)
                       VALUES (?,?,?)
                       ON CONFLICT(query_line_id, result_line_id)
                       DO UPDATE SET verdict = excluded.verdict,
                                     judged_at = datetime('now')""",
                    (body.query_line_id, body.result_line_id,
                     1 if body.verdict > 0 else -1))
        return {"ok": True, "verdict": body.verdict}
    finally:
        conn.close()


@app.get("/api/scores")
def get_scores():
    """Per-model tallies from my votes, discounted by how prominently each
    model offered the line.

    credit = verdict / log2(rank + 2). A hit at #2 is worth about 2.7x one at
    #19 -- a plain 1/rank would say 9.5x, which over-punishes a result that is
    merely a bit further down a list I read all of anyway.

    `unique` counts only results no other enabled model offered. A line every
    model returned separates nobody; the uniques are the entire signal.
    """
    import math
    conn = library()
    try:
        models = model_rows(conn)
        offers = {}
        for r in conn.execute("SELECT query_line_id, result_line_id, model, rank "
                              "FROM model_results"):
            offers.setdefault((r["query_line_id"], r["result_line_id"]), []).append(
                (r["model"], r["rank"]))
        votes = {(r["query_line_id"], r["result_line_id"]): r["verdict"]
                 for r in conn.execute(
                     "SELECT query_line_id, result_line_id, verdict FROM line_relations")}

        stats = {m["name"]: {**m, "up": 0, "down": 0, "dcg": 0.0, "ideal": 0.0,
                             "unique_up": 0, "unique_down": 0} for m in models}
        for key, verdict in votes.items():
            for name, rank in offers.get(key, []):
                s = stats.get(name)
                if not s:
                    continue                      # a model dropped from the registry
                w = 1.0 / math.log2(rank + 2)
                s["dcg"] += verdict * w
                s["ideal"] += w
                s["up" if verdict > 0 else "down"] += 1
                if len(offers[key]) == 1:
                    s["unique_up" if verdict > 0 else "unique_down"] += 1

        out = []
        for s in stats.values():
            out.append({**s, "score": (s["dcg"] / s["ideal"]) if s["ideal"] else None})
        out.sort(key=lambda s: (s["score"] is None, -(s["score"] or 0)))
        return {"models": out, "votes": len(votes)}
    finally:
        conn.close()


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
