"""Index the library: write a summary and a vector for every line, per model.

    python search/index_library.py --estimate            what it would cost
    python search/index_library.py --verify              one line, checks the endpoint
    python search/index_library.py --model gemini37 --batch
    python search/index_library.py --embed-only          vectors for existing summaries
    python search/index_library.py --model gemini37 --limit 50

This is the pass that turns an empty derived layer into a working similarity
search (CLAUDE.md §4). Two phases:

    summarise   OpenRouter writes an English thematic summary per line. Costs
                money, so everything here is built around not losing paid-for
                work: it checkpoints to the database continuously and only ever
                asks for lines that have no summary yet.
    embed       BGE-M3 turns those summaries into vectors, locally and free.

RESUMABLE BY DEFAULT. Stop it whenever -- Ctrl-C, a reboot, a dead network --
and run it again; it picks up exactly where it stopped. That is not a
convenience, it is the whole design: a single run is thousands of API calls over
several hours, and any script that could lose that on one bad line is unusable
at this size.

§7: the web app never loads BGE-M3. It lives here, in a script that runs and
exits, so the ~3 GB it wants is only in use while indexing.

§12: the model is only ever asked for an English summary. It never produces,
corrects or transliterates Gurmukhi, and what it writes goes only into the
derived layer, which can be deleted and regenerated at will.
"""

import argparse
import hashlib
import io
import json
import os
import sqlite3
import sys
import time

sys.stdout.reconfigure(encoding="utf-8")

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
DB_PATH = os.path.join(ROOT, "shabads.db")
MODELS_PATH = os.path.join(HERE, "models.json")

# How many lines to summarise before writing to the database. Small enough that
# a crash costs pennies, large enough that the commit overhead is invisible.
CHUNK = 25
EMBED_BATCH = 16
LOCK_PATH = os.path.join(HERE, ".index.lock")

# A cooperative stop, written by the app when a model is switched off mid-run.
#
# Deliberately NOT "kill the pid in the lock file". take_lock already explains
# why pids are not trusted here -- they get recycled, and on Windows there is no
# cheap portable way to ask whether one is still alive -- so killing by pid risks
# killing something else entirely. A flag the indexer looks at has none of that
# risk, drops its own lock on the way out, marks its own job row, and finishes
# the chunk it already paid for rather than throwing it away.
STOP_PATH = os.path.join(HERE, ".index.stop")


def stop_requested():
    return os.path.exists(STOP_PATH)


def clear_stop():
    try:
        os.unlink(STOP_PATH)
    except OSError:
        pass


class AlreadyRunning(Exception):
    pass


def take_lock():
    """One indexer at a time, machine-wide.

    Adding five shabads in a row spawns five of these, and each would load its
    own ~3 GB copy of BGE-M3 -- enough to bring a home PC to its knees. Because
    the run is resumable and asks the database what still needs doing, the one
    process that wins the lock also picks up everything the losers would have
    done, including shabads added while it runs. So the losers can simply exit.

    O_EXCL is atomic in the filesystem, which two processes starting in the same
    second need it to be. A stale lock from a killed process is detected by age
    rather than by PID: pids are recycled, and on Windows there is no cheap
    portable way to ask whether one is still alive.
    """
    import errno
    try:
        fd = os.open(LOCK_PATH, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except OSError as e:
        if e.errno != errno.EEXIST:
            raise
        age = time.time() - os.path.getmtime(LOCK_PATH)
        if age < 6 * 3600:
            raise AlreadyRunning(f"another indexer started {age / 60:.0f} min ago")
        os.unlink(LOCK_PATH)                       # stale; the process died
        fd = os.open(LOCK_PATH, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    os.write(fd, str(os.getpid()).encode())
    os.close(fd)


def drop_lock():
    try:
        os.unlink(LOCK_PATH)
    except OSError:
        pass


class Job:
    """A row in index_jobs, kept current so the app can say what is happening.

    The app cannot otherwise distinguish "stopped half way" from "working on it
    right now" -- both are just a partial count. Every update also refreshes
    `updated_at`, which is the heartbeat a reader uses to spot a job whose
    process died without ever writing a final state.
    """

    def __init__(self, conn, model, total, phase="summarise"):
        self.conn, self.model = conn, model
        with conn:
            cur = conn.execute(
                "INSERT INTO index_jobs (model, state, phase, total) "
                "VALUES (?, 'running', ?, ?)", (model, phase, total))
        self.id = cur.lastrowid

    def tick(self, done=None, spent=None, phase=None, total=None):
        sets, args = ["updated_at = datetime('now')"], []
        for col, val in (("done", done), ("spent", spent),
                         ("phase", phase), ("total", total)):
            if val is not None:
                sets.append(f"{col} = ?")
                args.append(val)
        with self.conn:
            self.conn.execute(f"UPDATE index_jobs SET {', '.join(sets)} WHERE id = ?",
                              args + [self.id])

    def finish(self, state="done", error=None):
        with self.conn:
            self.conn.execute(
                "UPDATE index_jobs SET state = ?, error = ?, "
                "updated_at = datetime('now') WHERE id = ?",
                (state, (error or "")[:300] or None, self.id))


def sweep_dead_jobs(conn):
    """Any job still 'running' but silent for ten minutes lost its process.

    A killed script never gets to write a final state, so without this the app
    would show "indexing..." forever after one reboot.
    """
    with conn:
        conn.execute(
            "UPDATE index_jobs SET state = 'stopped', "
            "error = COALESCE(error, 'process ended without finishing') "
            "WHERE state = 'running' "
            "AND updated_at < datetime('now', '-10 minutes')")


def prompt_version():
    """A short hash of the system prompt, stored beside every summary.

    §6 says the derived layer gets regenerated when the prompt improves. Without
    this you end up with a silent mix of old and new summaries and no way to
    tell them apart; with it, `WHERE prompt_ver != ?` finds the stale ones.
    """
    from summarize import SYSTEM
    return hashlib.sha256(SYSTEM.encode("utf-8")).hexdigest()[:12]


def db(write=False):
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    if write:
        # WAL lets the web app keep reading while a long index run writes.
        conn.execute("PRAGMA journal_mode = WAL")
    return conn


def registry():
    return {k: v for k, v in json.load(io.open(MODELS_PATH, encoding="utf-8")).items()
            if not k.startswith("_")}


def target_models(conn, only):
    """Which models to index for: the ones switched on in the app, unless told."""
    reg = registry()
    if only:
        names = [only]
    else:
        names = [r["name"] for r in conn.execute(
            "SELECT name FROM models WHERE enabled = 1 ORDER BY sort_order")]
    missing = [n for n in names if n not in reg]
    if missing:
        sys.exit(f"not in models.json: {', '.join(missing)}")
    if not names:
        sys.exit("no models enabled -- switch one on in Settings, or pass --model")
    return names


def pending(conn, model, ver, limit=0):
    """Lines with no current summary for this model, deduplicated by text.

    303 of the library's 5,290 lines are tuks that appear in more than one
    shabad. The same line means the same thing wherever it sits, so it is
    summarised once and the result written to every line that shares it --
    about 6% of the bill, saved for free.
    """
    sql = """SELECT l.id, l.gurmukhi, l.translation_en, l.teeka_pa
             FROM lines l
             LEFT JOIN line_summaries ls
                    ON ls.line_id = l.id AND ls.model = ? AND ls.prompt_ver = ?
             WHERE ls.line_id IS NULL
             ORDER BY l.id"""
    rows = [dict(r) for r in conn.execute(sql, (model, ver))]

    groups, order = {}, []
    for r in rows:
        if r["gurmukhi"] not in groups:
            groups[r["gurmukhi"]] = {"row": r, "ids": []}
            order.append(r["gurmukhi"])
        groups[r["gurmukhi"]]["ids"].append(r["id"])
    if limit:
        order = order[:limit]
    sel = [groups[g] for g in order]
    # `covered` counts only the lines this call will actually write, so a
    # --limit run is costed as what it will spend. Returning the whole
    # library's total here instead had the estimator asking to approve $4.59
    # for a slice that spends 18 cents -- a spend prompt you cannot trust is
    # worse than no spend prompt at all.
    return sel, sum(len(g["ids"]) for g in sel), len(rows)


def bar(frac, width=22):
    filled = int(round(max(0.0, min(1.0, frac)) * width))
    return "█" * filled + "░" * (width - filled)


def fmt_eta(secs):
    if secs <= 0 or secs != secs or secs == float("inf"):
        return "--"
    h, rem = divmod(int(secs), 3600)
    m, s = divmod(rem, 60)
    return f"{h}h{m:02d}m" if h else f"{m}m{s:02d}s"


QUIET = False


def say(*a):
    if not QUIET:
        print(*a)


def progress(tag, done, total, started, spent):
    """One line, rewritten in place. Explicit counts because a bar alone does
    not answer "how far in am I" on a run measured in hours."""
    if QUIET:
        return
    el = time.time() - started
    rate = done / el * 60 if el > 0 else 0
    eta = (total - done) / (done / el) if done and el > 0 else 0
    sys.stdout.write(
        f"\r  {tag:<11}{done:>5}/{total:<5} {bar(done / total if total else 1)} "
        f"{(done / total if total else 1):>4.0%}  {rate:>4.0f}/min  "
        f"eta {fmt_eta(eta):<7} ${spent:.2f}   ")
    sys.stdout.flush()


def save_chunk(conn, model, ver, results):
    """Write summaries for a chunk. Clears any embedding at the same time: a
    vector made from the previous summary would silently mismatch the new text,
    which is worse than having no vector at all."""
    rows = [(model, lid, txt, ver)
            for grp, txt in results for lid in grp["ids"]]
    with conn:
        conn.executemany(
            """INSERT INTO line_summaries (model, line_id, summary, prompt_ver)
               VALUES (?,?,?,?)
               ON CONFLICT(model, line_id) DO UPDATE SET
                 summary = excluded.summary,
                 prompt_ver = excluded.prompt_ver,
                 embedding = NULL,
                 created_at = datetime('now')""", rows)
    return len(rows)


def summarise(conn, key, name, reg, ver, use_batch, limit):
    from summarize import summarise_rows, Fatal
    from bench import fetch_prices, INPUT_PER_LINE

    model_id = reg[name]["id"] + (":batch" if use_batch else "")
    groups, covered, all_pending = pending(conn, name, ver, limit)
    if not groups:
        say(f"  {name}: nothing to summarise")
        return

    prices = fetch_prices({model_id})
    inp, outp = prices.get(model_id, (0.0, 0.0))
    say(f"\n  {name} -> {model_id}")
    say(f"  {len(groups)} unique texts covering {covered} lines"
          + (f", of {all_pending} lines outstanding" if limit else ""))

    started, spent, done = time.time(), 0.0, 0
    written = 0
    job = Job(conn, name, len(groups))
    try:
        for i in range(0, len(groups), CHUNK):
            chunk = groups[i:i + CHUNK]
            out, usage = summarise_rows(
                key, model_id, [g["row"] for g in chunk],
                workers=reg[name].get("workers", 6),
                extra=reg[name].get("options"),
                max_tpl=reg[name].get("max_tokens_per_line"),
                should_stop=stop_requested)
            got = [(g, out[g["row"]["gurmukhi"]]) for g in chunk
                   if g["row"]["gurmukhi"] in out]
            written += save_chunk(conn, name, ver, got)
            spent += (usage["prompt_tokens"] * inp
                      + usage["completion_tokens"] * outp) / 1e6
            done += len(chunk)
            job.tick(done=min(done, len(groups)), spent=spent)
            progress(name, done, len(groups), started, spent)
            # Save first, THEN check. Whatever came back is already paid for, so
            # it is written before honouring the stop -- a stop must cost nothing
            # beyond the calls already in flight.
            if stop_requested():
                job.finish("stopped", "stopped from the app")
                say(f"\n  stopped from the app. {written} lines saved "
                    "-- switch the model back on to carry on.")
                # Embedding still runs after this returns, and that is deliberate:
                # it is local, free, and takes seconds, while skipping it would
                # leave summaries that were paid for but cannot be searched --
                # the one state the control panel calls out as a fault. Stopping
                # is about not spending more, not about abandoning what is bought.
                return
    except KeyboardInterrupt:
        job.finish("stopped", "interrupted")
        say(f"\n  stopped. {written} lines saved -- run again to carry on.")
        return
    except Fatal as e:
        job.finish("failed", str(e))
        say(f"\n  stopped: {e}")
        say(f"  {written} lines saved -- fix that, then run again.")
        return
    except Exception as e:
        job.finish("failed", str(e))
        raise
    job.finish("done")
    say(f"\n  {name}: wrote {written} lines, ${spent:.2f}")


def embed_all(conn):
    """Vectors for every summary that hasn't got one. Local, free, resumable."""
    from embed import load_model, pack

    rows = conn.execute(
        "SELECT model, line_id, summary FROM line_summaries "
        "WHERE embedding IS NULL AND summary <> '' ORDER BY model, line_id").fetchall()
    if not rows:
        say("  nothing to embed")
        return

    say(f"\n  embedding {len(rows)} summaries (loading BGE-M3, ~30s)")
    # The job row is opened BEFORE the model loads, because that load is ~30
    # seconds of apparent silence and is exactly when someone looks at the badge
    # and concludes nothing is happening.
    job = Job(conn, ",".join(sorted({r["model"] for r in rows})), len(rows), "embed")
    model = load_model()
    started = time.time()
    try:
        for i in range(0, len(rows), EMBED_BATCH):
            batch = rows[i:i + EMBED_BATCH]
            vecs = model.encode([r["summary"] for r in batch],
                                batch_size=EMBED_BATCH, normalize_embeddings=True,
                                show_progress_bar=False, convert_to_numpy=True)
            with conn:
                conn.executemany(
                    "UPDATE line_summaries SET embedding = ? "
                    "WHERE model = ? AND line_id = ?",
                    [(pack(v.tolist()), r["model"], r["line_id"])
                     for r, v in zip(batch, vecs)])
            n = min(i + EMBED_BATCH, len(rows))
            job.tick(done=n)
            progress("embed", n, len(rows), started, 0.0)
    except KeyboardInterrupt:
        job.finish("stopped", "interrupted")
        say("\n  stopped. Everything embedded so far is saved.")
        return
    except Exception as e:
        job.finish("failed", str(e))
        raise
    job.finish("done")
    say(f"\n  embedded {len(rows)}")


def estimate(conn, names, reg, ver, use_batch, limit=0):
    from bench import fetch_prices, cost_for
    ids = {n: reg[n]["id"] + (":batch" if use_batch else "") for n in names}
    prices = fetch_prices(set(ids.values()))
    say(f"\n  {'model':<12}{'unique':>8}{'lines':>8}{'tok/line':>10}{'cost':>9}")
    total = 0.0
    for n in names:
        groups, covered, all_pending = pending(conn, n, ver, limit)
        tpl = reg[n].get("tokens_per_line", 1000)
        i, o = prices.get(ids[n], (0.0, 0.0))
        c = cost_for(len(groups), tpl, i, o)
        total += c
        say(f"  {n:<12}{len(groups):>8}{covered:>8}{tpl:>10}{'$%.2f' % c:>9}"
              + (f"   (of {all_pending} outstanding)" if limit else ""))
    say(f"  {'TOTAL':<12}{'':>8}{'':>8}{'':>10}{'$%.2f' % total:>9}")
    if limit:
        say(f"\n  --limit {limit} is in force: this run stops after {limit} "
              "unique texts.")
    if use_batch:
        say("\n  (:batch pricing -- run --verify first to confirm the endpoint works)")
    return total


def verify(key, name, reg, use_batch):
    """One real call, to prove the endpoint answers before committing hours.

    The :batch variants are half price, which is the difference between this
    run fitting the budget and not. But half price is worthless if the endpoint
    is asynchronous and our synchronous client can't talk to it, so find out
    now, for a fraction of a penny, rather than after a failed four-hour run.
    """
    from summarize import call, prompt_for, Fatal
    conn = db()
    row = dict(conn.execute(
        "SELECT id, gurmukhi, translation_en, teeka_pa FROM lines "
        "WHERE translation_en IS NOT NULL LIMIT 1").fetchone())
    conn.close()

    model_id = reg[name]["id"] + (":batch" if use_batch else "")
    say(f"  calling {model_id} with one line...")
    t0 = time.time()
    try:
        txt, usage = call(key, model_id, prompt_for(row),
                          extra=reg[name].get("options"))
    except Fatal as e:
        say(f"  FAILED: {e}")
        say("  the endpoint rejected the request -- do not rely on this pricing.")
        return False
    except Exception as e:
        say(f"  FAILED: {e}")
        return False
    say(f"  OK in {time.time() - t0:.1f}s, "
          f"{usage.get('prompt_tokens', 0)} in / {usage.get('completion_tokens', 0)} out")
    say(f"\n  --- what it wrote ---\n  {txt[:400]}\n")
    return True


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", help="one model name; default is every enabled model")
    # Half price, and NOT usable from here: OpenRouter serves :batch models only
    # through /api/beta/batches, which is submit-a-job-and-poll rather than a
    # normal chat completion. Verified by --verify, which gets a 404 telling you
    # exactly that. Left in place because --estimate --batch still prices the
    # work honestly, and because implementing the async path later is a real
    # option worth about $2.30 on a full run.
    ap.add_argument("--batch", action="store_true",
                    help="price against :batch (NOT runnable yet -- needs the async Batch API)")
    ap.add_argument("--limit", type=int, default=0, help="only N unique texts")
    ap.add_argument("--estimate", action="store_true", help="cost only, spend nothing")
    ap.add_argument("--verify", action="store_true", help="one call, then stop")
    ap.add_argument("--summarise-only", action="store_true")
    ap.add_argument("--embed-only", action="store_true")
    ap.add_argument("--yes", action="store_true", help="skip the spend prompt")
    ap.add_argument("--quiet", action="store_true",
                    help="no progress output; for scheduled and background runs")
    ap.add_argument("--max-spend", type=float, default=0.0, metavar="USD",
                    help="hard ceiling; indexes only as many texts as fit")
    args = ap.parse_args()

    global QUIET
    QUIET = args.quiet

    if not os.path.exists(DB_PATH):
        sys.exit(f"no database at {DB_PATH}")

    # --estimate and --verify spend nothing and load no model, so they need no
    # lock; everything else does.
    if not (args.estimate or args.verify):
        try:
            take_lock()
        except AlreadyRunning as e:
            say(f"skipping: {e}")
            return
        import atexit
        atexit.register(drop_lock)
        # Clear the flag on the way out too, whichever way we leave. A flag that
        # outlives the run it stopped would silently halt the NEXT run before it
        # asked for a single line.
        atexit.register(clear_stop)
        # And clear one left by a run that died before its atexit could fire.
        # Only after the lock is held, so this cannot wipe a stop aimed at a
        # different process that is still shutting down.
        clear_stop()

    conn = db(write=True)
    sweep_dead_jobs(conn)          # tidy up after any run that was killed
    reg = registry()
    ver = prompt_version()

    if args.embed_only:
        embed_all(conn)
        conn.close()
        return

    names = target_models(conn, args.model)
    say(f"models: {', '.join(names)}    prompt version: {ver}")

    from summarize import load_key
    key = load_key()

    # A ceiling, translated into a count of texts. This is what makes the
    # automatic run safe to trigger from the app: without it, adding one shabad
    # would start a pass over every unindexed line in the library and quietly
    # spend several dollars. A background job may only ever spend small change;
    # clearing a real backlog stays a deliberate, manual act.
    if args.max_spend:
        from bench import fetch_prices, cost_for
        ids = {n: reg[n]["id"] for n in names}
        prices = fetch_prices(set(ids.values()))
        per = max(cost_for(1, reg[n].get("tokens_per_line", 1000),
                           *prices.get(ids[n], (0.0, 0.0))) for n in names)
        fit = int(args.max_spend / per / max(1, len(names))) if per > 0 else 0
        if fit <= 0:
            say(f"--max-spend ${args.max_spend:.2f} does not cover a single line")
            conn.close()
            return
        args.limit = min(args.limit, fit) if args.limit else fit
        say(f"--max-spend ${args.max_spend:.2f}: at most {args.limit} texts per model")

    if args.verify:
        verify(key, names[0], reg, args.batch)
        conn.close()
        return

    total = estimate(conn, names, reg, ver, args.batch, args.limit)
    if args.estimate:
        conn.close()
        return
    if args.batch:
        conn.close()
        sys.exit("\n--batch prices the work but cannot run it: those models are served\n"
                 "only through OpenRouter's async /api/beta/batches endpoint.\n"
                 "Drop --batch to run at full price.")
    if total and not args.yes:
        if input(f"\nspend about ${total:.2f}? [y/N] ").strip().lower() != "y":
            conn.close()
            sys.exit("cancelled")

    say("\nSummarising. Safe to stop at any point -- it resumes where it left off.")
    for n in names:
        summarise(conn, key, n, reg, ver, args.batch, args.limit)

    if not args.summarise_only:
        embed_all(conn)

    left = conn.execute(
        "SELECT COUNT(*) FROM line_summaries WHERE embedding IS NULL").fetchone()[0]
    say(f"\ndone. {left} summaries still without a vector"
          + ("" if not left else "  -- run again with --embed-only"))
    conn.close()


if __name__ == "__main__":
    main()
