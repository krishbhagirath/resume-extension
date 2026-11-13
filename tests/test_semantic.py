"""Local semantic matching (ENGINE=local).

Most tests use a tiny fake embedder, so they run instantly and offline. The tests marked
`real_model` use the actual bge-small model (downloads ~67 MB once, then runs offline);
they are skipped when fastembed isn't installed.
"""
import hashlib
import importlib.util
import re

import numpy as np
import pytest

import analyzer
import bank as bankmod
import llm
import planner
import retrieval
import semantic
import tailor
from conftest import JDS, needs_tex

real_model = pytest.mark.skipif(importlib.util.find_spec("fastembed") is None, reason="fastembed not installed")


class FakeEmbedder:
    """Hashed bag of words: texts sharing words get similar vectors. Counts calls."""
    tag = "fake-bow"

    def __init__(self):
        self.calls = 0
        self.texts = 0

    def __call__(self, texts):
        self.calls += 1
        self.texts += len(texts)
        out = np.zeros((len(texts), 64), np.float32)
        for row, t in enumerate(texts):
            for w in re.findall(r"[a-z0-9]+", t.lower()):
                out[row, int(hashlib.md5(w.encode()).hexdigest(), 16) % 64] += 1.0
            out[row, 0] += 0.01   # never an all-zero vector
        return out


@pytest.fixture
def emb_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(semantic, "EMB_DIR", tmp_path / "emb")
    return tmp_path / "emb"


def with_kafka(bank: dict) -> dict:
    bank["projects"].append({"id": "stream", "title": "Event Pipeline", "stack": "Java", "bullets": [
        {"id": "stream-kafka", "text": "Built a Kafka streaming pipeline processing 2M events per day with "
                                       "exactly-once delivery and 50 ms p99 lag."}]})
    return bank


# ---------------------------------------------------------------- the real model

@real_model
def test_high_throughput_ranks_kafka_above_unrelated(small_bank):
    """A posting asking for "high-throughput systems" should prefer the Kafka bullet, even though
    the posting never says "Kafka" (the keyword matcher can't see this connection)."""
    bank = with_kafka(small_bank)
    texts = {b["id"]: bankmod.plain(b["text"]) for s in ("experiences", "projects")
             for it in bank[s] for b in it["bullets"]}
    q = semantic._normalize(semantic.default_embedder(["high-throughput systems"]))
    vecs = dict(zip(texts, semantic._normalize(semantic.default_embedder(list(texts.values())))))
    raw = semantic.raw_scores(vecs, q, np.ones(1, np.float32))
    for unrelated in ("acme-ui", "lab-cv", "fw-power"):   # React dashboards, CNN accuracy, power draw
        assert raw["stream-kafka"] > raw[unrelated], (unrelated, raw)
    assert max(raw, key=raw.get) == "stream-kafka"


@real_model
def test_high_throughput_posting_end_to_end_scores(small_bank, emb_dir):
    """Same check through the whole scoring path: posting -> analysis -> parts['semantic']."""
    bank = with_kafka(small_bank)
    jd = ("Backend Engineering Intern\nResponsibilities:\n- Design and operate high-throughput systems that "
          "move data between services\n- Keep latency low under heavy load\nRequirements:\n- Java or Python\n")
    job = analyzer.analyze(jd)
    sem, timing = semantic.score(bank, job)
    cands, _ = retrieval.score_bank(bank, job, semantic=sem)
    parts = {c.bullet_id: c.parts["semantic"] for c in cands}
    for unrelated in ("acme-ui", "lab-cv", "lab-data"):   # dashboards, CNN accuracy, image preprocessing
        assert parts["stream-kafka"] > parts[unrelated], (unrelated, parts)
    assert parts["stream-kafka"] > 0                    # above the bank's median bullet
    # (Not asserted: being the bank's #1. The "latency under heavy load" line also pulls up
    #  fw-power's "reduced power draw" wording; max-over-lines can do that on generic lines.)
    assert timing["queries"] >= 2 and timing["bank_cached"] is False
    assert not any(t.endswith(":") for t in semantic.job_queries(job)[0])   # no section headers


# ---------------------------------------------------------------- scoring math (fake embedder)

def test_importance_weighted_max():
    vecs = {"a": np.array([1.0, 0.0]), "b": np.array([0.0, 1.0])}
    queries = np.array([[1.0, 0.0], [0.0, 1.0]], np.float32)
    raw = semantic.raw_scores(vecs, queries, np.array([1.0, 0.5], np.float32))
    assert raw == pytest.approx({"a": 1.0, "b": 0.5})   # b matches only the half-important query


def test_rescale_median_to_zero_best_to_one():
    out = semantic.rescale({"a": 0.5, "b": 0.6, "c": 0.7, "d": 0.9})
    assert out["d"] == 1.0 and out["a"] == 0.0 and out["b"] == 0.0
    assert out["c"] == pytest.approx(0.05 / 0.25)
    assert semantic.rescale({"a": 0.4, "b": 0.4}) == {"a": 0.0, "b": 0.0}


def test_job_queries_weights(small_bank):
    job = analyzer.analyze(JDS["backend"])
    texts, weights = semantic.job_queries(job)
    n = min(semantic.TOP_REQUIREMENTS, len(job.requirements))
    assert texts[:n] == [r.skill for r in job.requirements[:n]]
    assert weights[0] == pytest.approx(1.0) and all(0 < w <= 1 for w in weights)
    assert len(texts) > n   # focus-text lines are included too


def test_bank_embeddings_cached_per_version(small_bank, emb_dir):
    fake = FakeEmbedder()
    _, cached = semantic.bank_embeddings(small_bank, fake)
    assert not cached and fake.calls == 1
    files = sorted(p.suffix for p in emb_dir.iterdir())
    assert files == [".json", ".npy"]
    _, cached = semantic.bank_embeddings(small_bank, fake)
    assert cached and fake.calls == 1                    # same bank version: read from disk
    small_bank["projects"][0]["bullets"][0]["text"] = "Rewrote the sensor firmware in Rust."
    _, cached = semantic.bank_embeddings(small_bank, fake)
    assert not cached and fake.calls == 2                # edited bank: embedded again


def test_hidden_bullets_get_no_score(small_bank, emb_dir):
    job = analyzer.analyze(JDS["fullstack"])
    sem, _ = semantic.score(small_bank, job, FakeEmbedder())
    assert "web-1" not in sem                            # its project is hidden
    assert set(sem) == semantic.visible_ids(small_bank)
    assert all(0.0 <= v <= 1.0 for v in sem.values()) and max(sem.values()) == 1.0


def test_semantic_part_only_when_given(small_bank):
    job = analyzer.analyze(JDS["backend"])
    plain, _ = retrieval.score_bank(small_bank, job)
    assert all("semantic" not in c.parts for c in plain)   # other engines: unchanged parts
    sem = {c.bullet_id: 0.5 for c in plain}
    with_sem, _ = retrieval.score_bank(small_bank, job, semantic=sem)
    assert all(c.parts["semantic"] == 0.5 for c in with_sem)


def test_semantic_weight_matches_claude_rank_bonus():
    """The local signal stands in for Claude's rank: same maximum pull on the planner."""
    assert planner.QUALITY_WEIGHTS["semantic"] * 1.0 == planner.QUALITY_WEIGHTS["llm"] * tailor.RANK_BONUS


def test_semantic_raises_quality(small_bank):
    job = analyzer.analyze(JDS["backend"])
    c = retrieval.score_bank(small_bank, job)[0][0]
    before = planner.quality(c)
    c.parts["semantic"] = 1.0
    assert planner.quality(c) > before


# ---------------------------------------------------------------- the engine

def test_engine_local_never_calls_claude(monkeypatch):
    monkeypatch.setenv("ENGINE", "local")
    assert llm.engine() == "local" and not llm.available()
    with pytest.raises(llm.LLMError):
        llm.complete_json("x", {"type": "object", "properties": {}, "required": []})


@needs_tex
def test_local_engine_end_to_end(small_bank, emb_dir, monkeypatch):
    monkeypatch.setenv("ENGINE", "local")
    fake = FakeEmbedder()
    monkeypatch.setattr(semantic, "default_embedder", fake)
    monkeypatch.setattr(llm, "complete_json", lambda *a, **k: pytest.fail("ENGINE=local called Claude"))
    res = tailor.run(small_bank, JDS["backend"], force=True)
    assert res.stats["mode"] == "local" and res.stats["llm_requests"] == 0 and res.stats["pool_size"] == 0
    assert "semantic" in res.stats["timings"] and res.stats["semantic"]["queries"] > 0
    assert all("semantic" in b["parts"] for b in res.bullet_scores)
    assert res.pdf[:4] == b"%PDF" and not res.warnings
    # second resume for the same bank reuses the cached bullet embeddings
    res2 = tailor.run(small_bank, JDS["ml"], force=True)
    assert res2.stats["semantic"]["bank_cached"] is True


@needs_tex
def test_local_engine_falls_back_when_model_unavailable(small_bank, emb_dir, monkeypatch):
    monkeypatch.setenv("ENGINE", "local")

    def broken(texts):
        raise semantic.SemanticError("Couldn't download the local matching model (no internet).")
    monkeypatch.setattr(semantic, "default_embedder", broken)
    res = tailor.run(small_bank, JDS["backend"], force=True)
    assert res.stats["mode"] == "offline" and res.pdf[:4] == b"%PDF"
    assert any("Local matching wasn't used" in w for w in res.warnings)
