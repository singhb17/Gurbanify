"""Turn text into vectors with BGE-M3, and write them into shabads.db.

    python embed.py --field translation_en --limit 50   # dry-ish: embed a few
    python embed.py --field summary                     # the real one, later
    python embed.py --stats

A one-time script, deliberately NOT a service (CLAUDE.md §7). The web app never
loads the model; it only ever reads vectors that are already in the database.

The model is ~2.3 GB on disk and wants ~3 GB of RAM while running. It is loaded
once per run and released when the process exits, so that memory is only in use
for the couple of minutes an indexing pass takes.
"""

import argparse
import os
import sqlite3
import struct
import sys

sys.stdout.reconfigure(encoding="utf-8")

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)          # scripts live in a subfolder; data lives at the root
DB_PATH = os.path.join(ROOT, "shabads.db")

MODEL_NAME = "BAAI/bge-m3"
DIMS = 1024

# Which column feeds the embedding. summary is the eventual answer (§6), the
# others exist so the §15 sanity check can compare them against each other.
FIELDS = ("summary", "translation_en", "gurmukhi", "teeka_pa")


def pack(vec):
    """float32 little-endian, so it round-trips identically anywhere."""
    return struct.pack(f"<{len(vec)}f", *vec)


def unpack(blob):
    return list(struct.unpack(f"<{len(blob) // 4}f", blob))


_MODEL = None


def load_model():
    """Cached per process. Loading takes ~30s, so anything that embeds more than
    once in a run must not pay that repeatedly."""
    global _MODEL
    if _MODEL is None:
        from sentence_transformers import SentenceTransformer
        print(f"loading {MODEL_NAME} (first run downloads ~2.3 GB)...", flush=True)
        _MODEL = SentenceTransformer(MODEL_NAME, device="cpu")
    return _MODEL


def embed_texts(model, texts, batch=8):
    # normalised so cosine similarity is a plain dot product later
    return model.encode(texts, batch_size=batch, show_progress_bar=True,
                        normalize_embeddings=True, convert_to_numpy=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--field", default="summary", choices=FIELDS,
                    help="which column to embed")
    ap.add_argument("--limit", type=int, default=0, help="0 = everything")
    ap.add_argument("--redo", action="store_true",
                    help="re-embed rows that already have a vector")
    ap.add_argument("--stats", action="store_true", help="just report coverage")
    args = ap.parse_args()

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row

    if args.stats:
        total = conn.execute("SELECT COUNT(*) FROM lines").fetchone()[0]
        done = conn.execute(
            "SELECT COUNT(*) FROM lines WHERE embedding IS NOT NULL").fetchone()[0]
        summ = conn.execute(
            "SELECT COUNT(*) FROM lines WHERE summary IS NOT NULL AND summary <> ''"
        ).fetchone()[0]
        print(f"lines      {total}")
        print(f"summarised {summ}")
        print(f"embedded   {done}")
        conn.close()
        return

    where = f"{args.field} IS NOT NULL AND {args.field} <> ''"
    if not args.redo:
        where += " AND embedding IS NULL"
    sql = f"SELECT id, {args.field} AS txt FROM lines WHERE {where} ORDER BY id"
    if args.limit:
        sql += f" LIMIT {args.limit}"

    rows = conn.execute(sql).fetchall()
    if not rows:
        print("nothing to embed")
        conn.close()
        return

    print(f"embedding {len(rows)} lines from `{args.field}`")
    model = load_model()
    vecs = embed_texts(model, [r["txt"] for r in rows])

    with conn:
        conn.executemany("UPDATE lines SET embedding = ? WHERE id = ?",
                         [(pack(v.tolist()), r["id"]) for r, v in zip(rows, vecs)])
    print(f"wrote {len(rows)} vectors of {len(vecs[0])} dims")
    conn.close()


if __name__ == "__main__":
    main()
