"""Semantic embedding index over the master catalogue.

Every product gets a meaning-vector once (BAAI/bge-small via fastembed —
ONNX, CPU-only, ~65MB, runs entirely on our own server); queries are embedded
at ask-time (~10-20ms) and matched by cosine similarity. The resolver fuses
this as a SCORING SIGNAL under the existing hierarchy — corrections, model
codes and exact names all stay above it. Everything here fails soft: if the
model or index is missing, matching simply proceeds without semantics.
"""
import os
import threading

import numpy as np

from app.config import DATA_DIR

MODEL_NAME = "BAAI/bge-small-en-v1.5"
INDEX_PATH = DATA_DIR / "semantic_index.npz"

_lock = threading.Lock()
_model = None
_index = None          # (ids: int64[n], vecs: float32[n,384] L2-normalised)
_index_mtime = None
_build_running = False


def _get_model():
    global _model
    if _model is None:
        from fastembed import TextEmbedding
        _model = TextEmbedding(MODEL_NAME)
    return _model


def _embed(texts):
    m = _get_model()
    vecs = np.array(list(m.embed(list(texts))), dtype=np.float32)
    norms = np.linalg.norm(vecs, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    return vecs / norms


def build_semantic_index(conn, batch=256):
    """Embed the whole catalogue into DATA_DIR/semantic_index.npz.
    ~1-3 minutes of CPU for 45k rows; called from a background thread or the
    build script, never inline in a request."""
    rows = conn.execute(
        "SELECT id, product, COALESCE(brand,''), COALESCE(category,'') "
        "FROM master_products").fetchall()
    if not rows:
        try:
            os.remove(INDEX_PATH)
        except OSError:
            pass
        return 0
    ids = np.array([r[0] for r in rows], dtype=np.int64)
    texts = [f"{r[1]} {r[2]} {r[3]}".strip() for r in rows]
    parts = []
    for i in range(0, len(texts), batch):
        parts.append(_embed(texts[i:i + batch]))
    vecs = np.vstack(parts)
    tmp = str(INDEX_PATH) + ".tmp.npz"
    np.savez_compressed(tmp, ids=ids, vecs=vecs)
    os.replace(tmp, INDEX_PATH)
    global _index, _index_mtime
    with _lock:
        _index = (ids, vecs)
        _index_mtime = os.path.getmtime(INDEX_PATH)
    return len(ids)


def _load_index():
    global _index, _index_mtime
    try:
        mtime = os.path.getmtime(INDEX_PATH)
    except OSError:
        return None
    with _lock:
        if _index is None or _index_mtime != mtime:
            d = np.load(INDEX_PATH)
            _index = (d["ids"], d["vecs"])
            _index_mtime = mtime
        return _index


def semantic_topk(query, k=50):
    """[(product_id, similarity)] for the k semantically closest products,
    or None when the index/model is unavailable (callers proceed without)."""
    try:
        idx = _load_index()
        if idx is None:
            return None
        q = _embed([query])[0]
        sims = idx[1] @ q
        k = min(k, len(sims))
        top = np.argpartition(-sims, k - 1)[:k]
        top = top[np.argsort(-sims[top])]
        return [(int(idx[0][i]), float(sims[i])) for i in top]
    except Exception as e:
        print(f"semantic lookup skipped (non-fatal): {e}")
        return None


def index_info():
    idx = _load_index()
    return {"exists": idx is not None,
            "products": int(len(idx[0])) if idx else 0,
            "building": _build_running}


def ensure_index_async(force=False):
    """Kick a background (re)build if the index is missing/stale — used at
    app startup and by the admin rebuild endpoint. No-op if already running."""
    global _build_running
    if _build_running:
        return False

    def _worker():
        global _build_running
        _build_running = True
        try:
            from app.db import get_db
            conn = get_db()
            try:
                n_db = conn.execute("SELECT COUNT(*) FROM master_products").fetchone()[0]
                idx = _load_index()
                if force or idx is None or len(idx[0]) != n_db:
                    built = build_semantic_index(conn)
                    print(f"semantic index built: {built} products")
            finally:
                conn.close()
        except Exception as e:
            print(f"semantic index build failed (non-fatal): {e}")
        finally:
            _build_running = False

    threading.Thread(target=_worker, daemon=True).start()
    return True
