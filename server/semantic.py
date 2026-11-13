"""Local semantic matching: how close a bullet's meaning is to what the job asks for.

Runs fully on this computer with fastembed (ONNX Runtime, no PyTorch) and the
BAAI/bge-small-en-v1.5 model (~67 MB). The model downloads on first use into
output/cache/models; after that it loads from disk only (local_files_only), so it works offline.

For one job:
  queries  = the job's top requirements (weight = importance / top importance)
             + each line of its focus text (responsibilities/requirements), weighted like a
               typical requirement (the median requirement weight of this job)
  raw(b)   = max over queries q of  weight_q * cosine(bullet b, q)
  semantic = raw rescaled within this bank: the bank's median bullet -> 0, its best -> 1
             (bge cosines sit in a narrow band, so the absolute value means little; the
              ranking within your own bank is what matters)

Bullet embeddings are computed once per bank version and cached in
output/cache/emb/<bank_hash>.npy (+ a small .json with the bullet ids and model name).
"""
from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path
from typing import Callable, Sequence

import numpy as np

import bank as bankmod
from analyzer import JobAnalysis

ROOT = Path(__file__).resolve().parent.parent
CACHE = Path(os.getenv("OUTPUT_DIR", ROOT / "output")) / "cache"
MODELS_DIR = CACHE / "models"
EMB_DIR = CACHE / "emb"
MODEL = "BAAI/bge-small-en-v1.5"
TOP_REQUIREMENTS = 15   # same order of magnitude as the requirements shown to Claude
MAX_FOCUS_LINES = 40

Embedder = Callable[[Sequence[str]], np.ndarray]   # texts -> (n, dim) array

_model = None
_model_lock = threading.Lock()


class SemanticError(RuntimeError):
    pass


def _load_model():
    """Load bge-small once per process. Disk first; download only if it isn't cached yet."""
    global _model
    with _model_lock:
        if _model is None:
            try:
                from fastembed import TextEmbedding
            except ImportError as e:
                raise SemanticError("fastembed isn't installed. Run setup.bat (or pip install -r requirements.txt).") from e
            MODELS_DIR.mkdir(parents=True, exist_ok=True)
            os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")   # Windows: plain copies are fine
            if model_cached():
                _model = TextEmbedding(MODEL, cache_dir=str(MODELS_DIR), local_files_only=True)  # no network
            else:
                try:   # first use: download once
                    _model = TextEmbedding(MODEL, cache_dir=str(MODELS_DIR))
                except Exception as e:
                    raise SemanticError(f"Couldn't download the local matching model ({str(e)[:120]}). "
                                        "It needs internet once.") from e
    return _model


def model_cached() -> bool:
    """True once the model files are on disk (fastembed stores them in a Hugging Face cache folder)."""
    return any(p.suffix == ".onnx" for p in MODELS_DIR.glob("models--*bge-small-en-v1.5*/snapshots/*/*"))


def _real_embedder(texts: Sequence[str]) -> np.ndarray:
    model = _load_model()
    return np.asarray(list(model.embed(list(texts))), dtype=np.float32)


default_embedder: Embedder = _real_embedder   # tests swap this for a fake


def _normalize(m: np.ndarray) -> np.ndarray:
    m = np.asarray(m, dtype=np.float32)
    return m / np.maximum(np.linalg.norm(m, axis=1, keepdims=True), 1e-12)


def _all_bullets(bank: dict) -> list[tuple[str, str]]:
    out = []
    for section in ("experiences", "projects"):
        for item in bank.get(section, []):
            for b in item.get("bullets", []):
                out.append((b["id"], bankmod.plain(b["text"])))
    return out


def bank_embeddings(bank: dict, embedder: Embedder | None = None) -> tuple[dict[str, np.ndarray], bool]:
    """{bullet id: unit vector} for every bullet, cached per bank version. Returns (vectors, was_cached)."""
    embedder = embedder or default_embedder
    h = bankmod.version_hash(bank)
    npy, meta = EMB_DIR / f"{h}.npy", EMB_DIR / f"{h}.json"
    bullets = _all_bullets(bank)
    ids = [i for i, _ in bullets]
    model_tag = getattr(embedder, "tag", MODEL)
    if npy.exists() and meta.exists():
        try:
            info = json.loads(meta.read_text(encoding="utf-8"))
            if info.get("ids") == ids and info.get("model") == model_tag:
                return dict(zip(ids, np.load(npy))), True
        except Exception:
            pass   # unreadable cache: recompute
    vecs = _normalize(embedder([t for _, t in bullets])) if bullets else np.zeros((0, 1), np.float32)
    EMB_DIR.mkdir(parents=True, exist_ok=True)
    np.save(npy, vecs)
    meta.write_text(json.dumps({"model": model_tag, "ids": ids}), encoding="utf-8")
    return dict(zip(ids, vecs)), False


def job_queries(job: JobAnalysis) -> tuple[list[str], np.ndarray]:
    """The texts that describe what the job wants, and how much each one counts."""
    reqs = job.requirements[:TOP_REQUIREMENTS]
    top = max((r.importance for r in reqs), default=1.0) or 1.0
    texts = [r.skill for r in reqs]
    weights = [r.importance / top for r in reqs]
    focus_weight = float(np.median(weights)) if weights else 1.0
    seen = set()
    for line in (job.focus_text or "").splitlines():
        line = line.strip()
        if len(line) < 12 or line.endswith(":") or line.lower() in seen:
            continue   # skip fragments and section headers like "Responsibilities:"
        seen.add(line.lower())
        texts.append(line)
        weights.append(focus_weight)
        if len(seen) >= MAX_FOCUS_LINES:
            break
    return texts, np.asarray(weights, dtype=np.float32)


def raw_scores(bullet_vecs: dict[str, np.ndarray], query_vecs: np.ndarray, weights: np.ndarray) -> dict[str, float]:
    """Importance-weighted max cosine of each bullet against the job's queries."""
    if not bullet_vecs or len(query_vecs) == 0:
        return {}
    ids = list(bullet_vecs)
    sims = np.stack([bullet_vecs[i] for i in ids]) @ query_vecs.T      # (bullets, queries) cosines
    best = (sims * weights[None, :]).max(axis=1)
    return {i: float(s) for i, s in zip(ids, best)}


def rescale(raw: dict[str, float]) -> dict[str, float]:
    """Within this bank: median bullet -> 0, best bullet -> 1, clipped to 0..1."""
    vals = np.array(list(raw.values()))
    if vals.size == 0:
        return {}
    mid, top = float(np.median(vals)), float(vals.max())
    if top - mid < 1e-6:
        return {k: 0.0 for k in raw}
    return {k: float(np.clip((v - mid) / (top - mid), 0.0, 1.0)) for k, v in raw.items()}


def visible_ids(bank: dict) -> set[str]:
    return {b["id"] for s in ("experiences", "projects") for it in bank.get(s, []) if not it.get("hidden")
            for b in it.get("bullets", []) if not b.get("hidden")}


def score(bank: dict, job: JobAnalysis, embedder: Embedder | None = None) -> tuple[dict[str, float], dict]:
    """{bullet id: 0..1 semantic match} for this job, plus timings."""
    embedder = embedder or default_embedder
    timings: dict = {}
    t = time.time()
    if embedder is _real_embedder:
        _load_model()   # timed separately from embedding
    timings["model_load_s"] = round(time.time() - t, 3)

    t = time.time()
    vecs, cached = bank_embeddings(bank, embedder)
    timings["bank_embed_s"] = round(time.time() - t, 3)
    timings["bank_cached"] = cached

    t = time.time()
    texts, weights = job_queries(job)
    qvecs = _normalize(embedder(texts)) if texts else np.zeros((0, 1), np.float32)
    timings["job_embed_s"] = round(time.time() - t, 3)
    timings["queries"] = len(texts)

    t = time.time()
    keep = visible_ids(bank)
    scores = rescale(raw_scores({k: v for k, v in vecs.items() if k in keep}, qvecs, weights))
    timings["score_s"] = round(time.time() - t, 3)
    return scores, timings
