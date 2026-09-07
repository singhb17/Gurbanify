"""Turn a single-user library into a multi-account one.

    python tools/migrate_multiuser.py --check      what it would do
    python tools/migrate_multiuser.py --write      do it

THE SHAPE OF THE CHANGE (CLAUDE.md §16)

Before, `shabads` mixed two different things: WHICH shabad this is (banidb id,
gurmukhi, ang, raag) and WHAT I THINK OF IT (status, rarity, notes). The first
is the same for everybody; the second is mine alone.

Splitting them is the whole migration:

    shabads        shared catalogue. one row per shabad in existence.
    user_shabads   one row per (person, shabad) -- their status, rarity, notes.

Everything derived hangs off the catalogue, so `lines` and `line_summaries` are
untouched and stay shared. That is what makes a second account nearly free: if
Dad adds a shabad I already have, its lines are already summarised and embedded,
so it costs nothing and is searchable immediately -- while my notes on it stay
invisible to him.

The other personal tables (tags, shortlist, history, learning, line_relations)
just gain a user_id. Most need rebuilding rather than ALTER because their
primary keys change.

SAFE TO RE-RUN. Every step checks whether it has already happened.
"""

import argparse
import io
import os
import secrets
import sqlite3
import sys

sys.stdout.reconfigure(encoding="utf-8")

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
DB_PATH = os.path.join(ROOT, "shabads.db")

sys.path.insert(0, ROOT)


def cols(conn, table):
    return {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}


def has_table(conn, table):
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                        (table,)).fetchone() is not None


def load_env():
    env = {}
    path = os.path.join(ROOT, ".env")
    if os.path.exists(path):
        for line in io.open(path, encoding="utf-8"):
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip().strip('"').strip("'")
    return env


def plan(conn):
    """What still needs doing."""
    steps = []
    if not has_table(conn, "users"):
        steps.append("create users + sessions")
    if not has_table(conn, "user_shabads"):
        n = conn.execute("SELECT COUNT(*) FROM shabads").fetchone()[0]
        steps.append(f"create user_shabads and move {n} shabads' metadata into it")
    for t in ("tags", "shortlist", "learning", "line_relations", "history"):
        if has_table(conn, t) and "user_id" not in cols(conn, t):
            n = conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
            steps.append(f"add user_id to {t} ({n} rows -> the admin account)")
    if "status" in cols(conn, "shabads"):
        steps.append("drop the moved columns from shabads")
    return steps


def migrate(conn, admin_user, admin_hash):
    conn.execute("PRAGMA foreign_keys = OFF")     # rebuilding tables they point at

    # --- accounts ------------------------------------------------------------
    conn.execute("""
        CREATE TABLE IF NOT EXISTS users (
          id            INTEGER PRIMARY KEY,
          username      TEXT NOT NULL UNIQUE COLLATE NOCASE,
          password_hash TEXT NOT NULL,
          is_admin      INTEGER NOT NULL DEFAULT 0,
          created_at    TEXT NOT NULL DEFAULT (datetime('now'))
        )""")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS sessions (
          token      TEXT PRIMARY KEY,
          user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
          created_at TEXT NOT NULL DEFAULT (datetime('now')),
          expires_at TEXT NOT NULL
        )""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions(user_id)")

    row = conn.execute("SELECT id FROM users WHERE is_admin = 1 ORDER BY id LIMIT 1"
                       ).fetchone()
    if row:
        admin_id = row[0]
        print(f"  admin account already exists (id {admin_id})")
    else:
        cur = conn.execute(
            "INSERT INTO users (username, password_hash, is_admin) VALUES (?,?,1)",
            (admin_user, admin_hash))
        admin_id = cur.lastrowid
        print(f"  created admin account '{admin_user}' (id {admin_id})")

    # --- the catalogue / library split ---------------------------------------
    if not has_table(conn, "user_shabads"):
        conn.execute("""
            CREATE TABLE user_shabads (
              user_id       INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
              shabad_id     INTEGER NOT NULL REFERENCES shabads(id) ON DELETE CASCADE,
              rarity        TEXT,
              status        TEXT,
              notes         TEXT,
              last_surfaced TEXT,
              -- Which line I know this shabad by. Personal: two people can file
              -- the same shabad under different tuks, and the list view shows
              -- this one. NULL means "whatever the catalogue says", which is
              -- what whoever added it first chose.
              source_line_no INTEGER,
              added_at      TEXT NOT NULL DEFAULT (datetime('now')),
              PRIMARY KEY (user_id, shabad_id)
            )""")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_user_shabads_user "
                     "ON user_shabads(user_id)")
        have = cols(conn, "shabads")
        conn.execute(f"""
            INSERT INTO user_shabads
                  (user_id, shabad_id, rarity, status, notes, last_surfaced, added_at)
            SELECT ?, id,
                   {'rarity' if 'rarity' in have else 'NULL'},
                   {'status' if 'status' in have else 'NULL'},
                   {'notes' if 'notes' in have else 'NULL'},
                   {'last_surfaced' if 'last_surfaced' in have else 'NULL'},
                   COALESCE({'imported_at' if 'imported_at' in have else 'NULL'},
                            datetime('now'))
            FROM shabads""", (admin_id,))
        n = conn.execute("SELECT COUNT(*) FROM user_shabads").fetchone()[0]
        print(f"  moved {n} shabads into user_shabads for the admin")

    # --- the personal tables -------------------------------------------------
    # Rebuilt rather than ALTERed: their primary keys change, and a PK is not
    # something ALTER TABLE can add.
    if "user_id" not in cols(conn, "tags"):
        conn.execute("""
            CREATE TABLE tags_new (
              user_id   INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
              shabad_id INTEGER NOT NULL REFERENCES shabads(id) ON DELETE CASCADE,
              kind      TEXT NOT NULL,
              value     TEXT NOT NULL,
              PRIMARY KEY (user_id, shabad_id, kind, value)
            )""")
        conn.execute("INSERT INTO tags_new (user_id, shabad_id, kind, value) "
                     "SELECT ?, shabad_id, kind, value FROM tags", (admin_id,))
        conn.execute("DROP TABLE tags")
        conn.execute("ALTER TABLE tags_new RENAME TO tags")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_tags_lookup ON tags(kind, value)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_tags_user ON tags(user_id)")
        print(f"  tags: {conn.execute('SELECT COUNT(*) FROM tags').fetchone()[0]} rows")

    if "user_id" not in cols(conn, "shortlist"):
        conn.execute("""
            CREATE TABLE shortlist_new (
              user_id   INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
              shabad_id INTEGER NOT NULL REFERENCES shabads(id) ON DELETE CASCADE,
              list      TEXT NOT NULL DEFAULT 'Interested',
              added_at  TEXT NOT NULL DEFAULT (datetime('now')),
              PRIMARY KEY (user_id, shabad_id, list)
            )""")
        conn.execute("INSERT INTO shortlist_new (user_id, shabad_id, list, added_at) "
                     "SELECT ?, shabad_id, list, added_at FROM shortlist", (admin_id,))
        conn.execute("DROP TABLE shortlist")
        conn.execute("ALTER TABLE shortlist_new RENAME TO shortlist")
        print(f"  shortlist: "
              f"{conn.execute('SELECT COUNT(*) FROM shortlist').fetchone()[0]} rows")

    if "user_id" not in cols(conn, "learning"):
        conn.execute("""
            CREATE TABLE learning_new (
              user_id        INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
              shabad_id      INTEGER NOT NULL REFERENCES shabads(id) ON DELETE CASCADE,
              added_at       TEXT NOT NULL DEFAULT (datetime('now')),
              status         TEXT NOT NULL DEFAULT 'not_started',
              last_practised TEXT,
              PRIMARY KEY (user_id, shabad_id)
            )""")
        have = cols(conn, "learning")
        conn.execute(f"""
            INSERT INTO learning_new (user_id, shabad_id, added_at, status, last_practised)
            SELECT ?, shabad_id, added_at,
                   {'status' if 'status' in have else "'not_started'"},
                   {'last_practised' if 'last_practised' in have else 'NULL'}
            FROM learning""", (admin_id,))
        conn.execute("DROP TABLE learning")
        conn.execute("ALTER TABLE learning_new RENAME TO learning")
        print(f"  learning: "
              f"{conn.execute('SELECT COUNT(*) FROM learning').fetchone()[0]} rows")

    if has_table(conn, "line_relations") and "user_id" not in cols(conn, "line_relations"):
        conn.execute("""
            CREATE TABLE line_relations_new (
              user_id        INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
              query_line_id  INTEGER NOT NULL REFERENCES lines(id) ON DELETE CASCADE,
              result_line_id INTEGER NOT NULL REFERENCES lines(id) ON DELETE CASCADE,
              verdict        INTEGER NOT NULL,
              judged_at      TEXT NOT NULL DEFAULT (datetime('now')),
              PRIMARY KEY (user_id, query_line_id, result_line_id)
            )""")
        conn.execute("""INSERT INTO line_relations_new
                          (user_id, query_line_id, result_line_id, verdict, judged_at)
                        SELECT ?, query_line_id, result_line_id, verdict, judged_at
                        FROM line_relations""", (admin_id,))
        conn.execute("DROP TABLE line_relations")
        conn.execute("ALTER TABLE line_relations_new RENAME TO line_relations")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_relations_result "
                     "ON line_relations(result_line_id)")
        print(f"  line_relations: "
              f"{conn.execute('SELECT COUNT(*) FROM line_relations').fetchone()[0]} rows")

    # history has its own surrogate key, so a plain column will do
    if "user_id" not in cols(conn, "history"):
        conn.execute("ALTER TABLE history ADD COLUMN user_id INTEGER "
                     "NOT NULL DEFAULT 0 REFERENCES users(id)")
        conn.execute("UPDATE history SET user_id = ?", (admin_id,))
        conn.execute("CREATE INDEX IF NOT EXISTS idx_history_user ON history(user_id)")
        print(f"  history: "
              f"{conn.execute('SELECT COUNT(*) FROM history').fetchone()[0]} rows")

    # --- shabads becomes a shared catalogue ----------------------------------
    # Done LAST, so nothing above has to worry about whether the columns are
    # still there. Anything failing before this leaves the originals intact.
    for c in ("rarity", "status", "notes", "last_surfaced"):
        if c in cols(conn, "shabads"):
            conn.execute(f"ALTER TABLE shabads DROP COLUMN {c}")
            print(f"  dropped shabads.{c} (now in user_shabads)")

    conn.execute("PRAGMA foreign_keys = ON")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--write", action="store_true", help="actually do it")
    ap.add_argument("--db", default=DB_PATH)
    ap.add_argument("--admin", help="username for the admin account")
    ap.add_argument("--password", help="password for it (default: a random one)")
    args = ap.parse_args()

    if not os.path.exists(args.db):
        sys.exit(f"no database at {args.db}")

    conn = sqlite3.connect(args.db)
    steps = plan(conn)
    if not steps:
        print("  already migrated; nothing to do")
        conn.close()
        return
    print("  this will:")
    for s in steps:
        print(f"    - {s}")

    if not args.write:
        print("\n  dry run. add --write to do it (back up first).")
        conn.close()
        return

    from api import hash_password
    env = load_env()
    admin = args.admin or env.get("APP_USER") or "admin"
    password = args.password or env.get("APP_PASSWORD")
    generated = False
    if not password:
        password = secrets.token_urlsafe(12)
        generated = True

    print()
    try:
        with conn:
            migrate(conn, admin, hash_password(password))
    except Exception as e:
        print(f"\n  FAILED, nothing committed: {type(e).__name__}: {e}")
        conn.close()
        raise
    conn.close()

    print(f"\n  done. sign in as '{admin}'")
    if generated:
        print(f"  GENERATED PASSWORD (save it now): {password}")
    else:
        print("  password: the APP_PASSWORD already in your .env")


if __name__ == "__main__":
    main()
