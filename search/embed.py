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


class ModelUnavailable(RuntimeError):
    """torch is installed but this machine cannot load it.

    Its own exception type because it is the one failure in this file that is
    about the machine rather than the data: nothing in the library is wrong,
    nothing was spent, and the fix is an installer rather than a rerun. Callers
    catch it to say that, instead of letting forty lines of DLL traceback be the
    only explanation an indexing log ever offers.
    """


def _load_hint(e):
    """What to actually do about a failed import.

    WinError 1114 is 'the DLL loaded and its initialisation routine failed',
    which is not the same as 'a file is missing' and does not read like the
    version problem it usually is. torch's DLLs need the Visual C++ runtime at
    14.20 or newer -- that is where vcruntime140_1.dll first appears -- and a
    machine can carry a 2017-era 14.11 for years with nothing else complaining,
    because Python itself only ever needs the half that is already there.
    """
    if os.name == "nt" and "1114" in str(e):
        return ("torch's DLLs will not load. The Visual C++ runtime is almost "
                "certainly too old -- install the current one from "
                "https://aka.ms/vs/17/release/vc_redist.x64.exe, open a NEW "
                "terminal, and run this again.")
    return ("torch could not be imported. Run tools/setup.ps1 -- it checks for "
            "exactly this and names the fix.")


def load_model():
    """Cached per process. Loading takes ~30s, so anything that embeds more than
    once in a run must not pay that repeatedly."""
    global _MODEL
    if _MODEL is None:
        try:
            from sentence_transformers import SentenceTransformer
        except ImportError as e:
            raise ModelUnavailable(
                f"sentence-transformers is not installed ({e}). "
                "Run tools/setup.ps1, or: pip install -r requirements.txt") from e
        except OSError as e:
            # An OSError from an *import* is a native library that would not
            # load, never a missing .py -- so it gets the installer advice.
            raise ModelUnavailable(f"{e}\n  {_load_hint(e)}") from e
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
