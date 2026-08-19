"""Turn text into vectors with BGE-M3.

    python embed.py --stats        # how much of the library is indexed

This is the model-loading half of the pipeline: load_model, embed_texts and
pack/unpack live here and are imported by index_library.py, bench.py and
test_clustering.py. Writing vectors into the database is index_library.py's
job -- it is per-model, checkpointed and resumable, which the old --field CLI
in this file was not.

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
    """Coverage only. Everything that writes is in index_library.py."""
    ap = argparse.ArgumentParser()
    ap.add_argument("--stats", action="store_true", help="report coverage")
    ap.parse_args()

    conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    total = conn.execute("SELECT COUNT(*) FROM lines").fetchone()[0]
    print(f"lines       {total}")
    for r in conn.execute(
            """SELECT model, COUNT(*) n,
                      SUM(embedding IS NOT NULL) e
               FROM line_summaries GROUP BY model ORDER BY model"""):
        print(f"  {r['model']:<14}{r['n']:>6} summarised {r['e']:>6} embedded")
    if not conn.execute("SELECT COUNT(*) FROM line_summaries").fetchone()[0]:
        print("  (nothing indexed yet -- run index_library.py)")
    conn.close()


if __name__ == "__main__":
    main()
