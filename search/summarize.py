"""Write an English thematic summary for each line, for embedding (CLAUDE.md §6).

    python summarize.py --list                       # what models are available
    python summarize.py --themes set.json --model X --out out/X.json

To index the library itself, use index_library.py -- it writes per model, in
checkpoints, and resumes. This file is the model-comparison side of the work and
its SYSTEM prompt, which index_library.py imports.

Everything goes through OpenRouter, so four candidate models need one API key and
one bill instead of four of each. Put it in a file called `.env` next to this
script:

    OPENROUTER_API_KEY=sk-or-...

`.env` must never be committed -- anything in the repo is readable by anyone with
repo access, and a leaked key is someone else spending your money.

CLAUDE.md §12: the model is given Gurbani and asked ONLY for an English summary
of its meaning. It never produces, corrects or transliterates Gurmukhi, and its
output only ever lands in `line_summaries`, which is the derived layer (§5) and
can be deleted and regenerated at will. Nothing it writes is shown as scripture.
"""

import argparse
import io
import json
import os
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor

sys.stdout.reconfigure(encoding="utf-8")

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)          # scripts live in a subfolder; data lives at the root
API_URL = "https://openrouter.ai/api/v1/chat/completions"

# §6: 40-80 words, meaning first then themes, deliberately restating the core
# idea several ways so that different phrasings of one concept land near each
# other in vector space. Written to be embedded, not to be read.
SYSTEM = """You write dense English summaries of single lines of Sikh scripture. \
Each summary is converted into a vector for semantic search and is never shown \
to a reader. Optimise for retrieval, not for reading.

You are given one line in Gurmukhi, an English translation, and usually a \
Punjabi teeka (traditional commentary). The teeka carries grammar, implied \
subject and reasoning that English translations flatten -- prefer it where the \
two disagree.

Write 40-80 words in two parts.

FIRST, what the line means AND what it is doing. Not only its subject but its \
stance: praising, pleading, warning, instructing, admonishing one's own mind, \
describing a state that results, declaring what human life is for. Most of \
Gurbani is about the Divine, so the subject alone separates almost nothing -- \
what the line DOES with that subject is the part that distinguishes it.

THEN a new line beginning "Themes:" followed by 4-8 comma-separated concepts.

Rules:
- Name the concrete image AND the idea beneath it. Lotus feet are both an image \
of the Divine presence and a statement of refuge and surrender; write both, so \
the line is findable from either direction.
- Restate the central idea two or three times in genuinely different words. \
Gurbani reaches one concept through many images, and varied phrasing is what \
lets those images match each other.
- Gloss Sikh terms inline on first use: naam (the divine Name), simran (loving \
remembrance), sangat (the company of the devout), maya (worldly attachment).
- Plain declarative English. No preamble, no "This line says", no "The verse \
describes", no hedging.
- Never name the author, the raag, the ang or the scripture, and don't use the \
words line, verse, hymn or shabad. Every entry shares those, so they carry no \
signal and only dilute the vector.
- Never reproduce, transliterate or correct Gurmukhi. English prose only.
- If the translation and the teeka genuinely conflict, or the meaning is \
uncertain, say so in one short clause rather than inventing a reading."""


def load_key():
    path = os.path.join(ROOT, ".env")
    if os.path.exists(path):
        for line in io.open(path, encoding="utf-8"):
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))
    key = os.environ.get("OPENROUTER_API_KEY")
    if not key:
        sys.exit("No OPENROUTER_API_KEY. Put it in a .env file next to this script.")
    return key


def prompt_for(row):
    parts = [f"Gurmukhi: {row['gurmukhi']}"]
    if row.get("translation_en"):
        parts.append(f"English translation: {row['translation_en']}")
    if row.get("teeka_pa"):
        parts.append(f"Punjabi teeka: {row['teeka_pa']}")
    else:
        parts.append("Punjabi teeka: (none available for this line)")
    return "\n".join(parts)


class Fatal(Exception):
    """Something retrying cannot fix -- no credit, bad key, unknown model."""


# A 429 means "later", not "no". OpenRouter's per-minute limit for an account
# scales with how much credit has been bought, so a new account hits it with
# settings that are fine for an older one -- and the failure is total for the
# lines it touches while costing nothing, which makes it pure waste.
#
# Given its own retry budget, separate from `tries`. Mixing the two spends the
# whole allowance on being told to wait, and then reports the line as broken
# when nothing was ever wrong with it.
RATE_TRIES = 6
RATE_BACKOFF = 8.0                      # seconds, x how many times we've been told
RATE_JITTER = 3.0


def call(key, model, text, tries=3, timeout=75, extra=None):
    """75s, 3 tries. The old 120s x 4 meant one stuck line could hold the run
    for eight minutes before anyone found out something was wrong.

    `extra` is merged into the request body, for per-model settings that belong
    to the model rather than to this script -- chiefly turning reasoning off.
    A hybrid reasoning model writes a thousand tokens of thinking before its
    60-word answer, and output is billed at 4-5x input, so the switch is worth
    far more than the sticker price difference between models.
    """
    import random
    import requests
    body = {"model": model, "temperature": 0.2,
            "messages": [{"role": "system", "content": SYSTEM},
                         {"role": "user", "content": text}]}
    if extra:
        body.update(extra)
    last = None
    rate_hits = 0
    attempt = 0
    while attempt < tries:
        try:
            r = requests.post(API_URL, timeout=timeout, json=body, headers={
                "Authorization": f"Bearer {key}",
                "HTTP-Referer": "http://localhost:8000",
                "X-Title": "Shabad Library",
            })
            if r.status_code == 200:
                d = r.json()
                return d["choices"][0]["message"]["content"].strip(), d.get("usage", {})
            # 402 no credit, 401 bad key, 400 bad model -- retrying just burns
            # time and, where a call half-succeeds, money. Stop immediately.
            if r.status_code in (400, 401, 402, 403, 404):
                raise Fatal(f"{r.status_code}: {r.json().get('error', {}).get('message', r.text)[:180]}")
            if r.status_code == 429:
                rate_hits += 1
                last = f"429 {r.text[:160]}"
                if rate_hits > RATE_TRIES:
                    break
                # Honour Retry-After when the server sends one; otherwise back
                # off further each time. The jitter is the part that matters:
                # without it every worker sleeps the same duration and wakes in
                # lockstep, so they trip the same limit together and the pool
                # converges on failing in unison rather than spreading out.
                wait = float(r.headers.get("Retry-After") or 0) or RATE_BACKOFF * rate_hits
                time.sleep(wait + random.uniform(0, RATE_JITTER))
                continue                            # not one of the `tries`
            last = f"{r.status_code} {r.text[:160]}"
        except Fatal:
            raise
        except Exception as e:                      # network flake, not a bug
            last = str(e)
        attempt += 1
        time.sleep(1.5 * attempt)                   # back off, then try again
    raise RuntimeError(f"failed after {attempt} tries"
                       + (f" and {rate_hits} rate limits" if rate_hits else "")
                       + f": {last}")


def list_models(key):
    import requests
    r = requests.get("https://openrouter.ai/api/v1/models",
                     headers={"Authorization": f"Bearer {key}"}, timeout=60)
    r.raise_for_status()
    want = ("gemini", "deepseek", "glm", "claude", "sonnet", "kimi", "qwen")
    for m in sorted(r.json()["data"], key=lambda m: m["id"]):
        if any(w in m["id"].lower() for w in want):
            p = m.get("pricing", {})
            print(f"  {m['id']:<52} in ${float(p.get('prompt', 0))*1e6:.2f}"
                  f"  out ${float(p.get('completion', 0))*1e6:.2f} /M")


def summarise_rows(key, model, rows, workers=6, extra=None, max_tpl=None, probe=12,
                   should_stop=None):
    """Summarise every row, returning whatever succeeded.

    A line that keeps failing must NOT throw away the ones that worked. The
    first version let one bad line propagate an exception out of the pool, which
    discarded every summary already paid for -- 73 good lines lost to 3 bad
    ones. Failures are collected instead, and the caller saves the rest; a later
    run picks up only what's missing, because the cache already holds the good
    ones.

    `max_tpl` is a runaway guard. A hybrid reasoning model that ignores the
    request to stop thinking writes ~1,700 tokens of reasoning before its
    60-word answer, and output bills at 4-5x input -- glm-4.7 with thinking on
    costs $16.74 to index the library against $3.49 with it off. Once `probe`
    lines have come back, the running average is checked against `max_tpl`; if
    it is over, no further lines are requested. In-flight calls still finish,
    so it overshoots by at most `workers`, which caps the damage at roughly
    a fifth of what one full run would have cost.

    Deliberately NOT done with `max_tokens` on the request: that truncates the
    answer rather than declining to ask for it, and a half-written summary gets
    cached and scored as though it were valid. Better to spend nothing more
    than to buy something misleading.
    """
    out, failed = {}, []
    usage = {"prompt_tokens": 0, "completion_tokens": 0}
    done = [0]
    stop = [None]                       # reason string, once tripped
    lock = threading.Lock()

    def one(row):
        # Two ways to stop asking for more: the runaway guard below, and the
        # app asking us to. Both land in the same place because the response is
        # identical -- request nothing further, keep everything already bought.
        if stop[0]:
            return row, None
        if should_stop and should_stop():
            stop[0] = "stopped from the app"
            return row, None
        try:
            txt, u = call(key, model, prompt_for(row), extra=extra)
            with lock:
                for k in usage:
                    usage[k] += u.get(k, 0) or 0
        except Fatal:
            raise
        except Exception as e:
            failed.append((row["gurmukhi"], str(e)[:110]))
            txt = None
        with lock:
            done[0] += 1
            n, tot = done[0], usage["prompt_tokens"] + usage["completion_tokens"]
            if max_tpl and n >= probe and not stop[0] and tot / n > max_tpl:
                stop[0] = (f"averaging {tot / n:.0f} tokens/line after {n} lines, "
                           f"over the {max_tpl} guard")
            print(f"\r  {done[0]}/{len(rows)}"
                  + (f"  ({len(failed)} failed)" if failed else ""), end="", flush=True)
        return row, txt

    with ThreadPoolExecutor(max_workers=workers) as pool:
        for row, txt in pool.map(one, rows):
            if txt:
                out[row["gurmukhi"]] = txt
    print()
    if stop[0]:
        print(f"  STOPPED EARLY: {stop[0]}")
        print(f"  kept {len(out)} lines; the rest were never requested.")
        if "token" in stop[0]:          # the runaway guard, not a deliberate stop
            print("  the reasoning switch is not being honoured for this model.")
    if failed:
        print(f"  {len(failed)} line(s) failed and were skipped:")
        for g, err in failed[:5]:
            print(f"    {g[:44]}  -> {err}")
        print("  re-run to retry just these; everything else is cached.")
    return out, usage


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", help="e.g. google/gemini-2.5-flash")
    ap.add_argument("--themes", help="score-set json; summarise only those lines")
    # (no --all: use index_library.py, which writes per model and resumes)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--out", required=False, help="write the summaries to this json")
    ap.add_argument("--list", action="store_true", help="show available models")
    args = ap.parse_args()

    key = load_key()
    if args.list:
        list_models(key)
        return
    if not args.model:
        sys.exit("--model is required (see --list)")

    if args.themes:
        from test_clustering import load_lines
        rows, _, missing = load_lines(args.themes)
        if missing:
            print(f"{len(missing)} theme lines not found, skipping them")
    else:
        sys.exit("pass --themes <file>  (to index the library use index_library.py)")

    if args.limit and args.themes:
        rows = rows[:args.limit]
    if not rows:
        print("nothing to do")
        return

    print(f"{args.model}: summarising {len(rows)} lines")
    t0 = time.time()
    try:
        out, usage = summarise_rows(key, args.model, rows)
    except Fatal as e:
        sys.exit(f"\n  stopped: {e}")             # no traceback, no retry storm
    secs = time.time() - t0

    print(f"  {secs:.0f}s   {usage['prompt_tokens']} in / {usage['completion_tokens']} out tokens")
    if usage["prompt_tokens"]:
        per = (usage["prompt_tokens"] + usage["completion_tokens"]) / len(rows)
        print(f"  ~{per:.0f} tokens per line -> ~{per * 5223 / 1e6:.2f}M for the full library")

    # Writing into the database is index_library.py's job now: it is per-model,
    # checkpointed and resumable, none of which this ever was. Here, --out only.
    os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)
    json.dump({"model": args.model, "usage": usage, "summaries": out},
              io.open(args.out, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print(f"  wrote {args.out}")


if __name__ == "__main__":
    main()
