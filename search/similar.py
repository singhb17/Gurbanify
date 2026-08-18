"""Find lines with similar meaning. This is the actual §3 feature, on the CLI.

    python search/similar.py --demo search/bakeoff/gemini.json   # the 21 test lines
    python search/similar.py --line 305                          # needs a full index
    python search/similar.py --text "the fear of death is gone"

All it does is compare vectors that already exist -- a dot product over numbers
in memory (CLAUDE.md §7). No API calls, no model at query time, unless you pass
--text, which has to embed your words first.

Read the scores the way §7 says: RANK, never threshold. In 1024 dimensions
unrelated vectors sit near perpendicular, so real scores bunch in a narrow band
(measured here: about 0.30 to 0.82). A cutoff like `if sim > 0.8` returns
nothing at all.
"""

import argparse
import io
import json
import os
import sqlite3
import struct
import sys

sys.stdout.reconfigure(encoding="utf-8")

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
DB_PATH = os.path.join(ROOT, "shabads.db")


def unpack(blob):
    return list(struct.unpack(f"<{len(blob) // 4}f", blob))


def from_db():
    """Vectors already written into the library by embed.py."""
    conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    rows = [dict(r) for r in conn.execute(
        """SELECT l.id, l.shabad_id, l.line_no, l.gurmukhi, l.translation_en,
                  l.summary, l.embedding, s.source_line
           FROM lines l JOIN shabads s ON s.id = l.shabad_id
           WHERE l.embedding IS NOT NULL ORDER BY l.id""")]
    conn.close()
    if not rows:
        sys.exit("No embeddings in the database yet. Run: python search/embed.py")
    import numpy as np
    vecs = np.array([unpack(r.pop("embedding")) for r in rows], dtype="float32")
    return rows, vecs


def from_bakeoff(path):
    """Embed one model's summaries on the fly, so the whole thing can be seen
    working before committing to a full indexing run."""
    from test_clustering import load_lines, bge_vectors, match_key
    import numpy as np

    data = json.load(io.open(path, encoding="utf-8"))["summaries"]
    themes = os.path.join(HERE, "themes_recall.json")
    rows, labels, _ = load_lines(themes)
    keep = [(r, l, data[r["gurmukhi"]]) for r, l in zip(rows, labels)
            if r["gurmukhi"] in data]
    out = []
    for r, label, summary in keep:
        out.append({**r, "theme": label, "summary": summary,
                    "source_line": r["gurmukhi"], "line_no": "", "shabad_id": ""})
    return out, np.array(bge_vectors([r["summary"] for r in out]), dtype="float32")


args_gurmukhi_only = False       # --gurmukhi hides the English under each hit


def show(rows, vecs, idx, top, query_label=None):
    import numpy as np
    v = vecs / np.linalg.norm(vecs, axis=1, keepdims=True)
    sims = v @ v[idx] if isinstance(idx, int) else v @ idx

    order = np.argsort(-sims)
    if isinstance(idx, int):
        q = rows[idx]
        print(f"\nQUERY  {q['gurmukhi']}")
        print(f"       {(q['translation_en'] or '')[:96]}")
        if q.get("theme"):
            print(f"       theme: {q['theme']}")
        order = [i for i in order if i != idx]
    else:
        print(f"\nQUERY  \"{query_label}\"")

    print(f"\n{'score':>7}  {'':<3} line")
    for rank, i in enumerate(order[:top], 1):
        r = rows[i]
        mark = ""
        if isinstance(idx, int) and r.get("theme"):
            mark = "OK " if r["theme"] == rows[idx]["theme"] else "-- "
        print(f"{sims[i]:>7.3f}  {mark:<3} {r['gurmukhi']}")
        if not args_gurmukhi_only:
            print(f"{'':>7}  {'':<3} {(r['translation_en'] or '')[:88]}")
    lo, hi = float(sims[order].min()), float(sims[order].max())
    print(f"\n  scores span {lo:.3f} .. {hi:.3f}  "
          f"-- narrow band is normal, rank rather than threshold")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--demo", help="a bakeoff json; embeds those summaries live")
    ap.add_argument("--line", type=int, help="line id to search from (needs the db)")
    ap.add_argument("--text", help="free text query (loads the model)")
    ap.add_argument("--top", type=int, default=8)
    ap.add_argument("--all", action="store_true", help="demo: query from every line")
    ap.add_argument("--gurmukhi", action="store_true", help="Gurmukhi only, no English")
    args = ap.parse_args()
    global args_gurmukhi_only
    args_gurmukhi_only = args.gurmukhi

    if args.demo:
        sys.path.insert(0, HERE)
        rows, vecs = from_bakeoff(args.demo)
    else:
        rows, vecs = from_db()
    print(f"{len(rows)} lines in the index, {vecs.shape[1]} dims each")

    if args.text:
        sys.path.insert(0, HERE)
        from embed import embed_texts, load_model
        import numpy as np
        q = np.array(embed_texts(load_model(), [args.text])[0], dtype="float32")
        show(rows, vecs, q / np.linalg.norm(q), args.top, args.text)
        return

    if args.all:
        for i in range(len(rows)):
            show(rows, vecs, i, 3)
        return

    if args.line is not None:
        idx = next((i for i, r in enumerate(rows) if r["id"] == args.line), None)
        if idx is None:
            sys.exit(f"line {args.line} has no embedding")
    else:
        idx = 0
    show(rows, vecs, idx, args.top)


if __name__ == "__main__":
    main()
