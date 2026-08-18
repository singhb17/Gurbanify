"""Compare summariser models on your own hand-grouped topics.

    python search/bench.py --list
    python search/bench.py --models gemini37 gpt5mini
    python search/bench.py --models gemini37 --topics search/topics/recall.txt
    python search/bench.py --show gemini37

WHAT IT DOES, in one pass:
  read your topic files -> summarise each line with each model -> embed every
  summary -> score how well each model's summaries group your topics -> print a
  leaderboard.

WHY IT'S NOT JUST THE OLD SCRIPTS CHAINED:
  - BGE-M3 takes ~30s to load and the old flow reloaded it every single run.
    Here it loads once, and only if something actually needs embedding.
  - Summaries are cached per model+line. Re-running a model you've already run
    costs nothing, so adding one new model to a comparison only pays for that
    one.
  - Vectors are cached by text. Re-scoring is instant.
  - It estimates the spend and asks before calling anything.

TOPIC FILES are plain text, far easier to paste into than json:

    # Lotus Feet
    ਚਰਨ ਕਮਲ ਆਧਾਰੁ ਜਨ ਕਾ ਆਸਰਾ ॥
    ਬੋਹਿਥੜਾ ਹਰਿ ਚਰਣ ਮਨ ਚੜਿ ਲੰਘੀਐ ॥

    # Amrit
    ਹਰਿ ਰਸੁ ਪੀਵਹੁ ਭਾਈ ॥

Lines are looked up in your library first, then in the whole SGGS, so you can
group anything in Gurbani -- not only shabads you've saved.
"""

import argparse
import glob
import hashlib
import io
import json
import os
import pickle
import sqlite3
import sys

sys.stdout.reconfigure(encoding="utf-8")
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)

TOPICS_DIR = os.path.join(HERE, "topics")
CACHE_DIR = os.path.join(HERE, "cache")
SUM_DIR = os.path.join(CACHE_DIR, "summaries")
VEC_PATH = os.path.join(CACHE_DIR, "vectors.pkl")
MODELS_PATH = os.path.join(HERE, "models.json")

DEFAULT_TOKENS_PER_LINE = 1000       # for a model we've never measured

TEMPLATE = """// One topic per heading. Paste full Gurmukhi lines underneath.
// Lines are looked up in your library first, then in all of SGGS.
//
// What makes a GOOD test set:
//   - group lines by MEANING, using lines whose wording differs
//   - keep topics far enough apart that you'd be annoyed if the app confused them
//   - 4-8 lines per topic, 3-4 topics
//   - recall them from memory rather than searching for a keyword. A set found
//     by searching shares vocabulary, every model scores well, and it tells you
//     nothing. The bench will warn you when a set is weak this way.

# First topic name
ਪਹਿਲੀ ਪੰਗਤੀ ਇਥੇ ॥

# Second topic name

# Third topic name
"""


# ---------- topics ----------

def parse_topic_file(path):
    """`# Heading` starts a topic; every non-blank line under it is a member."""
    topics, current = {}, None
    for raw in io.open(path, encoding="utf-8"):
        line = raw.strip()
        if not line or line.startswith("//"):
            continue
        if line.startswith("#"):
            current = line.lstrip("#").strip()
            topics.setdefault(current, [])
        elif current:
            topics[current].append(line)
    return {k: v for k, v in topics.items() if v}


def line_index():
    """Every line we can look up: the library first, then all of Gurbani."""
    from test_clustering import match_key
    index = {}
    corpus = sqlite3.connect(f"file:{os.path.join(ROOT, 'banidb.db')}?mode=ro", uri=True)
    corpus.row_factory = sqlite3.Row
    for r in corpus.execute("SELECT gurmukhi, english, teeka FROM verses"):
        index.setdefault(match_key(r["gurmukhi"]), {
            "gurmukhi": r["gurmukhi"], "translation_en": r["english"],
            "teeka_pa": r["teeka"], "src": "sggs"})
    corpus.close()
    lib = sqlite3.connect(f"file:{os.path.join(ROOT, 'shabads.db')}?mode=ro", uri=True)
    lib.row_factory = sqlite3.Row
    for r in lib.execute("SELECT gurmukhi, translation_en, teeka_pa FROM lines"):
        index[match_key(r["gurmukhi"])] = {
            "gurmukhi": r["gurmukhi"], "translation_en": r["translation_en"],
            "teeka_pa": r["teeka_pa"], "src": "library"}
    lib.close()
    return index


def load_topics(paths):
    from test_clustering import match_key
    index = line_index()
    sets = {}
    for path in paths:
        rows, labels, missing = [], [], []
        for topic, lines in parse_topic_file(path).items():
            for g in lines:
                hit = index.get(match_key(g))
                if hit:
                    rows.append(dict(hit))
                    labels.append(topic)
                else:
                    missing.append(g)
        sets[os.path.basename(path)] = (rows, labels, missing)
    return sets


# ---------- caches ----------

def sum_cache_path(key):
    os.makedirs(SUM_DIR, exist_ok=True)
    return os.path.join(SUM_DIR, f"{key}.json")


def load_summaries(key):
    p = sum_cache_path(key)
    return json.load(io.open(p, encoding="utf-8")) if os.path.exists(p) else {}


def save_summaries(key, data):
    json.dump(data, io.open(sum_cache_path(key), "w", encoding="utf-8"),
              ensure_ascii=False, indent=1)


def load_vectors():
    if os.path.exists(VEC_PATH):
        with open(VEC_PATH, "rb") as fh:
            return pickle.load(fh)
    return {}


def save_vectors(cache):
    os.makedirs(CACHE_DIR, exist_ok=True)
    with open(VEC_PATH, "wb") as fh:
        pickle.dump(cache, fh)


def text_key(t):
    return hashlib.sha1(t.encode("utf-8")).hexdigest()


# ---------- the run ----------

# Input is the system prompt plus one line, so it barely varies between models
# -- measured at ~557 tokens. Everything above that is output, which is where
# models differ wildly: a reasoning model writes thousands of tokens of thinking
# before its 60-word answer. Splitting this way is far more accurate than
# assuming a fixed input/output ratio.
INPUT_PER_LINE = 557
LIBRARY_LINES = 5223                 # what a full indexing run would cover


def cost_for(n, tpl, inp, outp):
    """Cost of n lines at `tpl` measured tokens per line.

    Every caller must go through here. The projection printed after a run used
    to do its own 55/45 input/output split, which quietly disagreed with the
    estimate above it: at kimi26's 3,703 tokens/line the split claimed 1,666
    output tokens where the real figure is 3,146, roughly halving the projected
    cost of the model with the most output to pay for. Two copies of one
    calculation is how that happens, so there is now only one.
    """
    out_tokens = max(40, tpl - INPUT_PER_LINE)
    return n * (INPUT_PER_LINE * inp + out_tokens * outp) / 1e6


def estimate(models, registry, n_missing, key_prices):
    total = 0.0
    print(f"\n{'model':<12}{'lines':>7}{'in/line':>9}{'out/line':>10}{'cost':>10}")
    for m in models:
        n = n_missing[m]
        if not n:
            print(f"{m:<12}{'0':>7}{'':>9}{'':>10}{'cached':>10}")
            continue
        tpl = registry[m].get("tokens_per_line", DEFAULT_TOKENS_PER_LINE)
        inp, outp = key_prices.get(registry[m]["id"], (0.0, 0.0))
        cost = cost_for(n, tpl, inp, outp)
        total += cost
        print(f"{m:<12}{n:>7}{INPUT_PER_LINE:>9}{max(40, tpl - INPUT_PER_LINE):>10}"
              f"{'$%.3f' % cost:>10}")
    print(f"{'TOTAL':<12}{'':>7}{'':>9}{'':>10}{'$%.3f' % total:>10}")
    return total


def fetch_prices(ids):
    import requests
    from summarize import load_key
    r = requests.get("https://openrouter.ai/api/v1/models",
                     headers={"Authorization": f"Bearer {load_key()}"}, timeout=60)
    out = {}
    for m in r.json()["data"]:
        if m["id"] in ids:
            p = m.get("pricing", {})
            out[m["id"]] = (float(p.get("prompt", 0)) * 1e6,
                            float(p.get("completion", 0)) * 1e6)
    return out


def embed_all(texts, cache):
    """Only loads BGE-M3 if something isn't cached already."""
    import numpy as np
    missing = [t for t in texts if text_key(t) not in cache]
    if missing:
        print(f"  embedding {len(missing)} new texts (loading BGE-M3, ~30s)...")
        from embed import embed_texts, load_model
        vecs = embed_texts(load_model(), missing, batch=16)
        for t, v in zip(missing, vecs):
            cache[text_key(t)] = np.asarray(v, dtype="float32")
        save_vectors(cache)
    return np.array([cache[text_key(t)] for t in texts], dtype="float32")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--models", nargs="*", default=[],
                    metavar="NAME",
                    help="model names, or `all` for every registered model, "
                         "or `cached` for only ones already paid for")
    ap.add_argument("--topics", nargs="*", default=[])
    ap.add_argument("--list", action="store_true", help="show registered models")
    ap.add_argument("--show", help="print similarity results for one model")
    ap.add_argument("--top", type=int, default=5)
    ap.add_argument("--yes", action="store_true", help="skip the cost prompt")
    ap.add_argument("--new", help="create an empty topic file to fill in")
    args = ap.parse_args()

    if args.new:
        os.makedirs(TOPICS_DIR, exist_ok=True)
        path = os.path.join(TOPICS_DIR, args.new.replace(".txt", "") + ".txt")
        if os.path.exists(path):
            sys.exit(f"{path} already exists")
        io.open(path, "w", encoding="utf-8").write(TEMPLATE)
        print(f"created {path}\n\nPaste your lines under each heading, then run:")
        print(f"  python search/bench.py --models gemini37 --topics {path}")
        return

    registry = {k: v for k, v in json.load(io.open(MODELS_PATH, encoding="utf-8")).items()
                if not k.startswith("_")}

    if args.list:
        print(f"{'name':<12}{'openrouter id':<44}{'tokens/line':>12}")
        for k, v in registry.items():
            print(f"{k:<12}{v['id']:<44}{v.get('tokens_per_line', '?'):>12}")
        print("\ncached summaries:")
        for k in registry:
            n = len(load_summaries(k))
            if n:
                print(f"  {k:<12}{n} lines")
        return

    paths = args.topics or sorted(glob.glob(os.path.join(TOPICS_DIR, "*.txt")))
    if not paths:
        sys.exit(f"No topic files. Put .txt files in {TOPICS_DIR}")
    sets = load_topics(paths)

    print("topics:")
    all_rows = []
    for name, (rows, labels, missing) in sets.items():
        groups = len(set(labels))
        print(f"  {name:<20} {len(rows):>3} lines in {groups} topics"
              + (f"   [{len(missing)} NOT FOUND]" if missing else ""))
        for g in missing:
            print(f"      not found: {g[:60]}")
        all_rows += rows

    models = args.models or ([args.show] if args.show else [])
    if not models:
        sys.exit("Pass --models, or --show <model>")
    # `all` saves typing eleven names; `cached` re-scores only what's already
    # been paid for, which never costs anything
    if "all" in models:
        models = list(registry)
    elif "cached" in models:
        models = [m for m in registry if load_summaries(m)]
        print(f"using cached models: {' '.join(models)}")
    for m in models:
        if m not in registry:
            sys.exit(f"Unknown model '{m}'. See --list")

    # what still needs writing, after the cache
    caches = {m: load_summaries(m) for m in models}
    need = {m: [r for r in all_rows if r["gurmukhi"] not in caches[m]] for m in models}
    todo = {m: len(v) for m, v in need.items()}

    if any(todo.values()):
        prices = fetch_prices({registry[m]["id"] for m in models})
        total = estimate(models, registry, todo, prices)
        if not args.yes:
            if input(f"\nspend about ${total:.3f}? [y/N] ").strip().lower() != "y":
                sys.exit("cancelled")

        from summarize import summarise_rows, Fatal
        from summarize import load_key
        key = load_key()
        for m in models:
            if not need[m]:
                continue
            print(f"\n{m} ({registry[m]['id']}): {len(need[m])} lines")
            try:
                # `workers` is per model: OpenRouter applies a per-model RPM cap
                # to new accounts, and six threads against a freshly released
                # model earns nothing but 429s -- luna lost 79 of 109 lines that
                # way. Slower is faster when the alternative is a retry storm.
                out, usage = summarise_rows(key, registry[m]["id"], need[m],
                                            workers=registry[m].get("workers", 6),
                                            extra=registry[m].get("options"),
                                            max_tpl=registry[m].get("max_tokens_per_line"))
            except Fatal as e:
                print(f"  stopped: {e}")
                continue
            caches[m].update(out)
            save_summaries(m, caches[m])
            # remember what it really cost, so the next estimate is accurate
            tpl = (usage["prompt_tokens"] + usage["completion_tokens"]) / max(1, len(need[m]))
            reg = json.load(io.open(MODELS_PATH, encoding="utf-8"))
            reg[m]["tokens_per_line"] = round(tpl)
            json.dump(reg, io.open(MODELS_PATH, "w", encoding="utf-8"),
                      ensure_ascii=False, indent=2)
            inp, outp = prices[registry[m]["id"]]
            print(f"  measured {tpl:.0f} tokens/line"
                  f"  ->  ~${cost_for(LIBRARY_LINES, tpl, inp, outp):.2f}"
                  f" for all {LIBRARY_LINES:,} lines")

    # ---- score ----
    from test_clustering import score
    vec_cache = load_vectors()
    results = {}

    for setname, (rows, labels, _) in sets.items():
        for m in models:
            texts = [caches[m].get(r["gurmukhi"]) for r in rows]
            keep = [i for i, t in enumerate(texts) if t]
            if len(keep) < 4:
                continue
            vecs = embed_all([texts[i] for i in keep], vec_cache)
            results.setdefault(m, {})[setname] = score(vecs, [labels[i] for i in keep])
        # the honest control: raw English translations, no model involved
        vecs = embed_all([r["translation_en"] or "" for r in rows], vec_cache)
        results.setdefault("(raw english)", {})[setname] = score(vecs, labels)

    if args.show:
        show_similar(sets, caches[args.show], vec_cache, args.top)
        return

    report(sets, results, models)


BASE = "(raw english)"

# Four ways of asking the same question, from harshest to most forgiving.
#
# R-precision is all-or-nothing: every one of a line's R same-topic neighbours
# must land inside its top R, and a correct line one rank late scores zero.
# That is much stricter than how the search is actually used -- tap a line,
# read 20 ranked results, judge them yourself -- so it is reported next to
# softer measures. If all four order the models the same way, the strictness
# never mattered and the leader is simply the better model. Where they
# disagree, Hit@3 and MRR are the ones that match real use.
METRICS = [
    ("r_precision", "R-prec", "{:>5.0%}",  "all R correct lines inside the top R"),
    ("hit_at_3",    "Hit@3",  "{:>5.0%}",  "at least one correct line in the top 3"),
    ("mrr",         "MRR",    "{:>5.2f}",  "1 / rank of the first correct line"),
    ("silhouette",  "Silh",   "{:>+6.3f}", "topic separation overall, no rank cutoff"),
]
PRIMARY = "r_precision"


def bar(frac, width=20):
    filled = int(round(max(0.0, min(1.0, frac)) * width))
    return "#" * filled + "." * (width - filled)


def mean_over(results, model, key, names):
    got = [results[model][s][key] for s in names if s in results.get(model, {})]
    return sum(got) / len(got) if got else 0.0


def report(sets, results, models):
    """One row per model per topic set, scored four different ways.

    The headline is R-precision: of the nearest neighbours a line has, how many
    come from the topic you put it in. 100% would mean every line's closest
    matches are exactly the ones you grouped it with. See METRICS for why the
    other three are printed beside it.
    """
    print("\n\nTOPIC SETS")
    weak = set()
    for name, (rows, labels, _) in sets.items():
        base = results[BASE][name]["r_precision"]
        # If plain English translations already score high, the lines in this
        # set share vocabulary -- usually because they were found by searching
        # for a word. Every model will look good and none can be told apart.
        flag = ""
        if base >= 0.75:
            weak.add(name)
            flag = f"   WEAK TEST - plain English already scores {base:.0%}"
        print(f"  {name:<18} {len(rows):>3} lines, {len(set(labels))} topics"
              f"   baseline {base:.0%}{flag}")
    if weak:
        print("\n  A weak set can't separate models. Topics recalled from memory,")
        print("  rather than found by searching, make much better tests.")

    print("\n\nHOW EACH COLUMN SCORES  (harshest first)")
    for _, label, _, blurb in METRICS:
        print(f"  {label:<8} {blurb}")

    head = "  " + f"{'model':<12}" + " " * 14 + "".join(f"{l:>7}" for _, l, _, _ in METRICS)
    for name in sets:
        base = results[BASE][name][PRIMARY]
        tag = "  (weak - ignore)" if name in weak else "  (meaningful)"
        print(f"\n\n{name}{tag}\n")
        print(head)
        ranked = sorted([m for m in models if name in results.get(m, {})],
                        key=lambda m: -results[m][name][PRIMARY])
        for m in ranked + [BASE]:
            r = results[m][name]
            cells = "".join(f.format(r[k]) + " " for k, _, f, _ in METRICS)
            note = "  <- plain English, no model" if m == BASE else \
                   f"  {r[PRIMARY] - base:+.0%} vs baseline"
            label = "baseline" if m == BASE else m
            print(f"  {label:<12} {bar(r[PRIMARY], 12)} {cells}{note}")

    strong = [s for s in sets if s not in weak]
    if not strong:
        print("\n\nNo meaningful topic set to rank on. Add one built from memory.")
        return

    print("\n\nOVERALL" + (f"  (averaged over: {', '.join(strong)})" if strong else ""))
    avg = {k: {m: mean_over(results, m, k, strong)
               for m in list(models) + [BASE] if m in results}
           for k, _, _, _ in METRICS}
    order = sorted([m for m in models if m in results], key=lambda m: -avg[PRIMARY][m])
    print()
    print("  " + f"{'#  model':<15}" + " " * 11 + "".join(f"{l:>7}" for _, l, _, _ in METRICS))
    for i, m in enumerate(order, 1):
        cells = "".join(f.format(avg[k][m]) + " " for k, _, f, _ in METRICS)
        print(f"  {i}. {m:<12} {bar(avg[PRIMARY][m], 12)} {cells}")
    cells = "".join(f.format(avg[k][BASE]) + " " for k, _, f, _ in METRICS)
    print(f"     {'baseline':<12} {bar(avg[PRIMARY][BASE], 12)} {cells}")

    if len(order) < 2:
        return

    # The point of four metrics is not four leaderboards -- it is whether the
    # strictness of R-precision is quietly picking a different winner.
    print("\n\nDO THE METRICS AGREE?\n")
    tops = {}
    for k, label, _, _ in METRICS:
        o = sorted(order, key=lambda m: -avg[k][m])
        tops[label] = o[0]
        print(f"  {label:<8} " + "   ".join(f"{i}.{m}" for i, m in enumerate(o[:3], 1)))

    leaders = sorted(set(tops.values()))
    print()
    if len(leaders) == 1:
        print(f"  All four agree: {leaders[0]} leads. R-precision being harsh is")
        print("  not changing the answer, so take it at face value.")
    else:
        print(f"  They disagree on the leader ({', '.join(leaders)}).")
        print("  Prefer Hit@3 and MRR -- reading 20 ranked results is forgiving of")
        print("  a correct line sitting one or two places lower than it should.")

    gap = avg[PRIMARY][order[0]] - avg[PRIMARY][order[1]]
    print(f"\n  On {METRICS[0][1]}: {order[0]} at {avg[PRIMARY][order[0]]:.0%}, "
          f"{'ahead of' if gap else 'level with'} {order[1]} by {gap:.0%}.")
    if gap < 0.05:
        print("  At this sample size that is noise. Add another topic file.")


def show_similar(sets, summaries, vec_cache, top):
    import numpy as np
    rows, labels = [], []
    for r, l, _ in sets.values():
        rows += r
        labels += l
    texts = [summaries.get(r["gurmukhi"]) for r in rows]
    keep = [i for i, t in enumerate(texts) if t]
    rows = [rows[i] for i in keep]
    labels = [labels[i] for i in keep]
    v = embed_all([texts[i] for i in keep], vec_cache)
    v = v / np.linalg.norm(v, axis=1, keepdims=True)
    sim = v @ v.T

    seen = set()
    for i, lab in enumerate(labels):
        if lab in seen:
            continue
        seen.add(lab)
        print(f"\nQUERY  {rows[i]['gurmukhi']}   [{lab}]")
        nearest = [j for j in np.argsort(-sim[i]) if j != i][:top]
        for j in nearest:
            # OK/-- is agreement with YOUR grouping, not correctness -- topics
            # genuinely overlap and a "--" is often a perfectly good match
            mark = "OK" if labels[j] == lab else "--"
            print(f"  {sim[i][j]:.3f}  {mark}  {rows[j]['gurmukhi']}")


if __name__ == "__main__":
    main()
