"""CLAUDE.md §15 — does BGE-M3 actually cluster thematically-linked Gurbani?

    python test_clustering.py
    python test_clustering.py --fields translation_en gurmukhi teeka_pa

Takes the hand-labelled themes in themes.json and measures whether lines from
the same theme end up near each other in vector space.

This is the go/no-go on the whole §7 design, and it is meant to be run BEFORE
spending anything on summaries. If related lines don't cluster, better to find
out here than after paying to index 5,223 lines.

TWO BASELINES, both of which matter:

  TF-IDF   plain word overlap, no semantics at all. The themes were found using
           the app's English keyword search, so lines within a theme tend to
           share a keyword -- three of the four have one word present in every
           line. If BGE-M3 only matches TF-IDF, it is doing keyword search with
           extra steps and the embedding approach is not earning its place.

  the best-scoring FIELD is the number the eventual LLM summaries must beat. A
  summary scoring below its own source text is a failing summary.
"""

import argparse
import io
import json
import os
import sqlite3
import sys

sys.stdout.reconfigure(encoding="utf-8")

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)          # scripts live in a subfolder; data lives at the root
DB_PATH = os.path.join(ROOT, "shabads.db")
CORPUS_PATH = os.path.join(ROOT, "banidb.db")
THEMES = os.path.join(HERE, "themes.json")

UDAAT = "ੑ"
VIRAMA_HA = "੍ਹ"
_MODEL = None


def norm(t):
    return " ".join((t or "").split())


def match_key(t):
    """Same normalisation the importer uses -- non-breaking spaces and the two
    orthographic variants BaniDB spells inconsistently."""
    return norm(t).replace(UDAAT, VIRAMA_HA)


def load_lines(path=THEMES):
    """Look in the personal library first, then fall back to the full SGGS.

    A theme set can name any line in Gurbani, not just the 410 shabads I've
    saved -- lines recalled from memory generally won't be in the library.
    Column names differ between the two databases, so they're normalised here.
    """
    themes = json.load(io.open(path, encoding="utf-8"))["themes"]

    index = {}
    corpus = sqlite3.connect(f"file:{CORPUS_PATH}?mode=ro", uri=True)
    corpus.row_factory = sqlite3.Row
    for r in corpus.execute("SELECT gurmukhi, english, teeka FROM verses"):
        index.setdefault(match_key(r["gurmukhi"]), {
            "gurmukhi": r["gurmukhi"], "translation_en": r["english"],
            "teeka_pa": r["teeka"], "summary": None, "src": "sggs"})
    corpus.close()

    lib = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    lib.row_factory = sqlite3.Row
    # summary comes from line_summaries now -- lines.summary was the old
    # single-model column and has been dropped. Any model's summary will do
    # here; this script only ever compares fields against each other.
    for r in lib.execute(
            """SELECT l.gurmukhi, l.translation_en, l.teeka_pa,
                      (SELECT ls.summary FROM line_summaries ls
                        WHERE ls.line_id = l.id LIMIT 1) AS summary
               FROM lines l"""):
        index[match_key(r["gurmukhi"])] = {                # library wins
            "gurmukhi": r["gurmukhi"], "translation_en": r["translation_en"],
            "teeka_pa": r["teeka_pa"], "summary": r["summary"], "src": "library"}
    lib.close()

    rows, labels, missing = [], [], []
    for theme, lines in themes.items():
        for g in lines:
            hit = index.get(match_key(g))
            if hit:
                rows.append(hit)
                labels.append(theme)
            else:
                missing.append(g)
    return rows, labels, missing


def score(vecs, labels):
    """Cosine-similarity metrics against the hand-labelled themes."""
    import numpy as np
    from sklearn.metrics import silhouette_score
    from sklearn.preprocessing import normalize

    v = normalize(vecs)
    sim = v @ v.T
    n = len(labels)
    same = np.array([[a == b for b in labels] for a in labels])
    off = ~np.eye(n, dtype=bool)

    order = np.argsort(-sim + np.eye(n) * 9)      # never rank a line against itself
    hits = tot = 0
    per_theme = {}
    rr, hit3 = [], []
    for i in range(n):
        m = labels.count(labels[i]) - 1
        if m <= 0:
            continue
        got = sum(1 for j in order[i][:m] if labels[j] == labels[i])
        hits += got
        tot += m
        h, t = per_theme.get(labels[i], (0, 0))
        per_theme[labels[i]] = (h + got, t + m)

        # R-precision above is all-or-nothing per slot: a correct line at rank
        # m+1 scores exactly the same as one at rank n. That is far stricter
        # than the way the search is used -- tap a line, read 20 ranked results
        # -- so measure the softer questions too.
        nb = [j for j in order[i] if j != i]
        rr.append(next((1 / k for k, j in enumerate(nb, 1)
                        if labels[j] == labels[i]), 0.0))
        hit3.append(float(any(labels[j] == labels[i] for j in nb[:3])))

    return {
        "within": sim[same & off].mean(),
        "between": sim[~same].mean(),
        "gap": sim[same & off].mean() - sim[~same].mean(),
        "silhouette": silhouette_score(v, labels, metric="cosine"),
        "r_precision": hits / tot if tot else 0,
        "mrr": sum(rr) / len(rr) if rr else 0,          # 1/rank of the first hit
        "hit_at_3": sum(hit3) / len(hit3) if hit3 else 0,
        "per_theme": {k: h / t for k, (h, t) in per_theme.items()},
        "lo": sim[off].min(), "hi": sim[off].max(),
        "order": order, "labels": labels,
    }


def bge_vectors(texts):
    global _MODEL
    from embed import embed_texts, load_model
    if _MODEL is None:
        _MODEL = load_model()
    return embed_texts(_MODEL, texts)


def tfidf_vectors(texts):
    """Deliberately dumb: word overlap, no meaning. The control group."""
    from sklearn.feature_extraction.text import TfidfVectorizer
    return TfidfVectorizer(sublinear_tf=True).fit_transform(texts).toarray()


def show(name, res, rows=None):
    print(f"\n=== {name}")
    print(f"  within-theme similarity   {res['within']:.3f}")
    print(f"  between-theme similarity  {res['between']:.3f}")
    print(f"  gap                       {res['gap']:+.3f}   <- bigger is better")
    print(f"  silhouette                {res['silhouette']:+.3f}   <- >0 means themes separate")
    print(f"  R-precision               {res['r_precision']:.0%}   <- strictest")
    print(f"  Hit@3                     {res['hit_at_3']:.0%}   <- closest to real use")
    print(f"  MRR                       {res['mrr']:.2f}")
    print(f"  raw score range           {res['lo']:.2f} .. {res['hi']:.2f}"
          f"   <- §7: rank, never threshold")
    print("  per theme: " + "  ".join(
        f"{k}={v:.0%}" for k, v in sorted(res["per_theme"].items())))
    if rows is not None:
        wrong = [(rows[i], res["labels"][i], res["labels"][res["order"][i][0]])
                 for i in range(len(rows))
                 if res["labels"][res["order"][i][0]] != res["labels"][i]]
        if wrong:
            print(f"  nearest neighbour off-theme for {len(wrong)}/{len(rows)}:")
            for r, want, got in wrong[:6]:
                print(f"     {r['gurmukhi'][:42]:<44} {want} -> {got}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--fields", nargs="+",
                    default=["translation_en", "gurmukhi", "teeka_pa"],
                    help="columns to compare; add `summary` once it exists")
    ap.add_argument("--themes", default=THEMES, help="which ground-truth file")
    ap.add_argument("--summaries", nargs="*", default=[],
                    help="bake-off json files from summarize.py; scored as extra fields")
    args = ap.parse_args()

    rows, labels, missing = load_lines(args.themes)

    # graft each model's summaries on as an extra scoreable column
    for path in args.summaries:
        d = json.load(io.open(path, encoding="utf-8"))
        name = "sum:" + os.path.basename(path).replace(".json", "")
        for r in rows:
            r[name] = d["summaries"].get(r["gurmukhi"])
        args.fields.append(name)
    themes = sorted(set(labels))
    print(f"{os.path.basename(args.themes)}: {len(rows)} lines across {len(themes)} themes")
    for t in themes:
        n = labels.count(t)
        lib = sum(1 for r, l in zip(rows, labels) if l == t and r["src"] == "library")
        print(f"   {t:<24} {n} lines  ({lib} from your library, {n - lib} from SGGS)")
    if missing:
        print(f"\n{len(missing)} NOT FOUND in library or corpus:")
        for g in missing:
            print("   " + g[:64])

    results = {}

    # the control: pure keyword overlap on the English translations
    texts = [r["translation_en"] or "" for r in rows]
    results["TF-IDF (keywords only)"] = score(tfidf_vectors(texts), labels)
    show("TF-IDF on translation_en  [CONTROL — no semantics]",
         results["TF-IDF (keywords only)"])

    for f in args.fields:
        txt = [r[f] or "" for r in rows]
        keep = [i for i, t in enumerate(txt) if t.strip()]
        if len(keep) < 4:
            print(f"\n=== BGE-M3 on {f}: only {len(keep)} lines have it, skipping")
            continue
        res = score(bge_vectors([txt[i] for i in keep]), [labels[i] for i in keep])
        results[f"BGE-M3 / {f}"] = res
        show(f"BGE-M3 on {f}", res, [rows[i] for i in keep])

    print("\n" + "=" * 64)
    print(f"{'':<34}{'gap':>8}{'silhouette':>13}{'R-prec':>9}")
    for k, r in results.items():
        print(f"{k:<34}{r['gap']:>+8.3f}{r['silhouette']:>+13.3f}{r['r_precision']:>8.0%}")

    ctrl = results["TF-IDF (keywords only)"]["r_precision"]
    best = max((r["r_precision"], k) for k, r in results.items() if k.startswith("BGE"))
    print(f"""
How to read this:
  silhouette <= 0            -> STOP. Themes don't separate; §7 is wrong.
  BGE-M3 <= TF-IDF ({ctrl:.0%})     -> embeddings are just keyword search with extra
                                steps, and the whole approach is unproven.
  BGE-M3 >> TF-IDF           -> embeddings genuinely add meaning. Proceed.

  Best field: {best[1]} at {best[0]:.0%} — that is the baseline the LLM
  summaries have to beat.

  Watch `amrit` specifically: it is the one theme with NO word common to every
  line, so it wasn't found by keyword search. It is the cleanest evidence here.""")


if __name__ == "__main__":
    main()
