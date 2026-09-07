"""Prove two accounts cannot see each other's library.

    python tools/test_isolation.py            against a throwaway copy
    python tools/test_isolation.py --port 8000  against a server already running

This is the half of the defence that the GuardedConnection in api.py cannot
provide. The guard catches a query that forgets user_id; this catches a query
that HAS user_id and still returns the wrong rows -- a join through the wrong
table, a filter on the wrong alias, an endpoint nobody remembered to scope.

It signs in as two accounts, gives each its own private data, and then asks
every read endpoint whether either can see the other's anything. Anything it
finds is a leak, and the exit code says so.

SAFE: it works on a COPY of the database by default and never touches the real
one. The copy is thrown away afterwards.
"""

import argparse
import io
import os
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time

sys.stdout.reconfigure(encoding="utf-8")

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

PASS, FAIL = [], []

# Used only on the throwaway copy. Never touches the real database.
ADMIN_PASSWORD = "isolation-test-admin-pw"


def check(name, ok, detail=""):
    (PASS if ok else FAIL).append(name)
    mark = "  ok  " if ok else " LEAK "
    print(f"  [{mark}] {name}" + (f"   {detail}" if detail and not ok else ""))


def read_env():
    env = {}
    path = os.path.join(ROOT, ".env")
    if os.path.exists(path):
        for line in io.open(path, encoding="utf-8"):
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip().strip('"').strip("'")
    return env


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, help="test a server already running")
    ap.add_argument("--keep", action="store_true", help="keep the scratch database")
    args = ap.parse_args()

    import requests

    proc = tmpdir = None
    if args.port:
        base = f"http://127.0.0.1:{args.port}"
    else:
        # Throwaway copy, its own port, its own process. Nothing here can reach
        # the real library.
        tmpdir = tempfile.mkdtemp(prefix="shabad-iso-")
        db = os.path.join(tmpdir, "shabads.db")
        shutil.copy(os.path.join(ROOT, "shabads.db"), db)
        print(f"  copied the library to {db}")

        # The copy may still be single-user, and the app refuses to run on one.
        # Migrating here means this test also exercises the migration.
        env_file = read_env()
        mig = subprocess.run(
            [sys.executable, os.path.join(HERE, "migrate_multiuser.py"),
             "--db", db, "--write",
             "--admin", env_file.get("APP_USER", "admin"),
             "--password", env_file.get("APP_PASSWORD", "changeme12345")],
            cwd=ROOT, capture_output=True, text=True, encoding="utf-8",
            errors="replace")
        if mig.returncode != 0:
            print(mig.stdout, mig.stderr)
            sys.exit("  could not migrate the scratch copy")
        print("  migrated the copy")

        # Force a known admin password ON THE COPY. The real database's password
        # lives in its users table and may have been changed in the app long ago,
        # so .env cannot be relied on to open it -- and this test must not need
        # to know anybody's actual password.
        import sqlite3
        from api import hash_password
        c = sqlite3.connect(db)
        with c:
            c.execute("UPDATE users SET password_hash = ? WHERE is_admin = 1",
                      (hash_password(ADMIN_PASSWORD),))
            admin_name = c.execute(
                "SELECT username FROM users WHERE is_admin = 1 ORDER BY id"
            ).fetchone()[0]
        c.close()
        os.environ["ISO_ADMIN"] = admin_name
        print(f"  test admin: {admin_name}")

        port = free_port()
        base = f"http://127.0.0.1:{port}"
        env = dict(os.environ, SHABAD_DB=db)
        log = os.path.join(tmpdir, "server.log")
        proc = subprocess.Popen(
            [sys.executable, "-m", "uvicorn", "api:app", "--port", str(port)],
            cwd=ROOT, env=env,
            stdout=io.open(log, "w", encoding="utf-8"), stderr=subprocess.STDOUT)
        for _ in range(40):
            try:
                requests.get(base + "/health", timeout=2)
                break
            except Exception:
                time.sleep(0.5)
        else:
            proc.kill()
            print(io.open(log, encoding="utf-8", errors="replace").read()[-2500:])
            sys.exit("  the test server did not start")

    try:
        run(base, requests)
    finally:
        if proc:
            proc.send_signal(signal.SIGTERM)
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
        if tmpdir and not args.keep:
            shutil.rmtree(tmpdir, ignore_errors=True)

    print(f"\n  {len(PASS)} passed, {len(FAIL)} leaked")
    if FAIL:
        print("\n  LEAKS:")
        for f in FAIL:
            print(f"    - {f}")
        sys.exit(1)
    print("  no account can see another's library.")


def run(base, requests):
    from api import hash_password
    import sqlite3

    # --- two accounts, one of them not the admin ---------------------------
    admin = requests.Session()
    other = requests.Session()

    # Against a copy we set the password ourselves; against --port we fall back
    # to .env, which only works if it has not been changed in the app since.
    env = read_env()
    name = os.environ.get("ISO_ADMIN") or env.get("APP_USER", "admin")
    pw = ADMIN_PASSWORD if os.environ.get("ISO_ADMIN") else env.get("APP_PASSWORD", "")
    r = admin.post(base + "/api/login", timeout=20,
                   json={"username": name, "password": pw})
    check("admin can sign in", r.status_code == 200, r.text[:120])
    if r.status_code != 200:
        return

    # create the second account through the admin api
    r = admin.post(base + "/api/admin/users", timeout=20,
                   json={"username": "testdad", "password": "dadpass12345"})
    check("admin can create an account", r.status_code in (200, 409), r.text[:160])

    r = other.post(base + "/api/login", timeout=20,
                   json={"username": "testdad", "password": "dadpass12345"})
    check("second account can sign in", r.status_code == 200, r.text[:120])
    if r.status_code != 200:
        return

    # --- what the admin has, the other must not see ------------------------
    mine = admin.get(base + "/api/shabads", timeout=60).json()
    theirs = other.get(base + "/api/shabads", timeout=60).json()
    check("library is not shared",
          theirs["count"] == 0 and mine["count"] > 0,
          f"admin {mine['count']}, other {theirs['count']}")

    my_ids = {s["id"] for s in mine["shabads"]}
    their_ids = {s["id"] for s in theirs["shabads"]}
    check("no overlapping shabads", not (my_ids & their_ids))

    # one of the admin's shabads, poked at directly by the other account
    sid = next(iter(my_ids))
    check("cannot open another's shabad",
          other.get(f"{base}/api/shabads/{sid}", timeout=20).status_code == 404)
    check("cannot edit another's shabad",
          other.patch(f"{base}/api/shabads/{sid}", json={"notes": "x"},
                      timeout=20).status_code == 404)
    check("cannot delete another's shabad",
          other.delete(f"{base}/api/shabads/{sid}", timeout=20).status_code == 404)
    check("cannot shortlist another's shabad",
          other.post(f"{base}/api/shortlist/{sid}", timeout=20).status_code == 404)
    check("cannot add another's shabad to learning",
          other.post(f"{base}/api/learning/{sid}", timeout=20).status_code == 404)
    check("cannot log a visit to another's shabad",
          other.post(f"{base}/api/history", json={"shabad_id": sid},
                     timeout=20).status_code == 404)

    # --- list endpoints ----------------------------------------------------
    for path, key in (("/api/shortlist", "shabads"), ("/api/history", "shabads"),
                      ("/api/learning", "shabads"), ("/api/deck?limit=100", "shabads")):
        a = admin.get(base + path, timeout=60).json()
        b = other.get(base + path, timeout=60).json()
        ids_a = {s["id"] for s in a.get(key, [])}
        ids_b = {s["id"] for s in b.get(key, [])}
        check(f"{path} is not shared", not (ids_a & ids_b),
              f"{len(ids_a & ids_b)} shared rows")

    # --- filters must not reveal the other's vocabulary --------------------
    fa = admin.get(base + "/api/filters", timeout=30).json()
    fb = other.get(base + "/api/filters", timeout=30).json()
    check("filters show nothing for an empty library",
          fb["total"] == 0 and not fb["raag"] and not fb["writer"] and not fb["status"],
          f"other sees total={fb['total']} raags={len(fb['raag'])}")
    check("admin's filters are populated", fa["total"] > 0)

    # --- similar search must not surface another's lines -------------------
    detail = admin.get(f"{base}/api/shabads/{sid}", timeout=30).json()
    line_id = detail["lines"][0]["id"]
    check("cannot run similar on another's line",
          other.get(f"{base}/api/similar/{line_id}", timeout=60).status_code == 404)
    check("cannot quiz on another's line",
          other.get(f"{base}/api/quiz/{line_id}", timeout=30).status_code == 404)

    sim = admin.get(f"{base}/api/similar/{line_id}", timeout=60).json()
    check("admin's own similar search works", sim.get("results") is not None)

    # --- scope=all widens to the catalogue, never to anyone's opinions -----
    # It is meant to show Gurbani nobody owns. What it must never carry is the
    # personal layer: status, rarity, notes, tags, shortlisted.
    PERSONAL = {"status", "rarity", "notes", "tags", "shortlisted", "last_surfaced"}
    wide = admin.get(f"{base}/api/similar/{line_id}",
                     params={"scope": "all"}, timeout=60).json()
    leaked = set()
    for row in wide.get("results", []):
        leaked |= PERSONAL & set(row)
    check("scope=all carries no personal fields", not leaked, f"{sorted(leaked)}")
    check("scope=all returns results", len(wide.get("results", [])) > 0)
    check("scope=all marks what is already mine",
          all("in_library" in r for r in wide.get("results", [])))
    check("an unknown scope is refused",
          admin.get(f"{base}/api/similar/{line_id}", params={"scope": "sneaky"},
                    timeout=30).status_code == 400)
    # and it is still gated on the QUERY line being mine
    check("scope=all cannot be used from another's line",
          other.get(f"{base}/api/similar/{line_id}", params={"scope": "all"},
                    timeout=30).status_code == 404)

    # --- votes are private -------------------------------------------------
    if sim.get("results"):
        rid = sim["results"][0]["id"]
        admin.post(base + "/api/relations", timeout=20,
                   json={"query_line_id": line_id, "result_line_id": rid, "verdict": 1})
        sa = admin.get(base + "/api/scores", timeout=30).json()
        sb = other.get(base + "/api/scores", timeout=30).json()
        check("votes are not shared", sb["votes"] == 0 and sa["votes"] > 0,
              f"admin {sa['votes']}, other {sb['votes']}")

    # --- admin-only surfaces ----------------------------------------------
    for path in ("/api/status", "/api/indexing", "/api/models/gemini37/estimate"):
        check(f"{path} is admin only",
              other.get(base + path, timeout=60).status_code == 403)
    check("/api/backup is admin only",
          other.post(base + "/api/backup", timeout=60).status_code == 403)
    check("account management is admin only",
          other.get(base + "/api/admin/users", timeout=20).status_code == 403)

    # --- signed out is locked out -----------------------------------------
    anon = requests.Session()
    check("anonymous api is refused",
          anon.get(base + "/api/shabads", timeout=20).status_code == 401)
    check("anonymous page redirects to login",
          anon.get(base + "/", timeout=20, allow_redirects=False).status_code == 303)
    check("health stays open", anon.get(base + "/health", timeout=20).status_code == 200)

    # --- the second account can still USE the app --------------------------
    with sqlite3.connect(os.path.join(ROOT, "shabads.db")) as _:
        pass                                   # (real db untouched; just asserting import)
    cat = other.get(base + "/api/search?q=gkbvv&mode=firstletter", timeout=60)
    check("shared catalogue search still works for a new account",
          cat.status_code == 200)


if __name__ == "__main__":
    main()
