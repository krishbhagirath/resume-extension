"""tests/learn_weights.py on synthetic picks, and the planner picking up learned weights."""
import json
import random

import pytest

pytest.importorskip("sklearn")

import learn_weights as lw  # noqa: E402
import planner  # noqa: E402
from retrieval import Candidate  # noqa: E402


def fake_bullets(rng, n=12):
    out = []
    for i in range(n):
        parts = {"coverage": rng.uniform(0, 1.5), "role": rng.uniform(0, 1), "keywords": rng.uniform(0, 0.6),
                 "evidence": rng.choice([0.0, 0.5]) + rng.uniform(0, 0.4), "recency": 0.3 * rng.uniform(0, 1),
                 "llm": rng.choice([0.0, rng.uniform(0, 1.5)]), "semantic": rng.uniform(0, 1)}
        if rng.random() < 0.15:
            parts["short_last_line"] = 1.0
        out.append({"id": f"b{i}", "parts": parts})
    return out


def write_picks(path, truth: dict, n: int, seed=0):
    """Picks made by someone whose taste is `truth` (weights over lw.FEATURES), with some noise."""
    import math
    rng = random.Random(seed)
    w = [truth.get(k, 0.0) for k in lw.FEATURES]
    with path.open("w", encoding="utf-8") as fh:
        for _ in range(n):
            a, b = fake_bullets(rng, rng.randint(9, 14)), fake_bullets(rng, rng.randint(9, 14))
            d = lw.resume_features(a) - lw.resume_features(b)
            p_a = 1 / (1 + math.exp(-sum(x * y for x, y in zip(w, d))))
            fh.write(json.dumps({"source": rng.choice(["compare", "swap"]), "winner": "a" if rng.random() < p_a else "b",
                                 "reasons": [], "a": {"bullets": a}, "b": {"bullets": b}}) + "\n")


def test_learns_a_different_taste_and_writes_weights(tmp_path, capsys):
    picks, out = tmp_path / "picks.jsonl", tmp_path / "learned.json"
    # cares about measured results and penalizes short last lines; doesn't care about Claude's rank
    write_picks(picks, {"evidence": 3.0, "coverage": 0.3, "short_last_line": -2.0, "llm": -0.5}, 120)
    assert lw.main(["--picks", str(picks), "--out", str(out)]) == 0
    text = capsys.readouterr().out
    assert "Learned weights win" in text, text
    d = json.loads(out.read_text())
    w = d["quality_weights"]
    assert set(w) == set(planner.QUALITY_WEIGHTS)
    assert abs(sum(w.values()) - sum(planner.QUALITY_WEIGHTS.values())) < 0.01   # same total as now
    assert w["evidence"] == max(w.values()) and w["llm"] == 0                     # negative -> clamped to 0
    assert d["short_line_factor"] < planner.SHORT_LINE_FACTOR                      # stronger penalty
    assert d["meta"]["loo_accuracy"] > d["meta"]["hand_accuracy"]
    assert text.index("evidence") < text.index("recency")                          # importance order printed


def test_nothing_written_when_hand_weights_already_fit(tmp_path, capsys):
    picks, out = tmp_path / "picks.jsonl", tmp_path / "learned.json"
    truth = {"coverage": 1.0, **planner.QUALITY_WEIGHTS, "short_last_line": -0.15}
    write_picks(picks, {k: 8 * v for k, v in truth.items()}, 60, seed=3)   # near-deterministic, current taste
    lw.main(["--picks", str(picks), "--out", str(out)])
    assert not out.exists(), capsys.readouterr().out


def test_too_few_picks_and_bad_rows(tmp_path, capsys):
    picks, out = tmp_path / "picks.jsonl", tmp_path / "learned.json"
    write_picks(picks, {"evidence": 3.0}, 8)
    same = fake_bullets(random.Random(1))
    with picks.open("a", encoding="utf-8") as fh:
        fh.write("not json\n")
        fh.write(json.dumps({"winner": "a", "a": {"bullets": same}, "b": {"bullets": same}}) + "\n")
        fh.write(json.dumps({"winner": "b", "a": {"bullets": []}, "b": {"bullets": same}}) + "\n")
    lw.main(["--picks", str(picks), "--out", str(out), "--min-picks", "15"])
    text = capsys.readouterr().out
    assert "Usable picks: 8" in text and "1 identical, 1 without per-bullet scores, 1 unreadable" in text
    assert not out.exists()
    assert lw.main(["--picks", str(tmp_path / "missing.jsonl"), "--out", str(out)]) == 1


def test_planner_uses_learned_file_when_present(tmp_path, monkeypatch):
    f = tmp_path / "learned.json"
    c = Candidate(bullet_id="x", item_id="i", section="experiences", text="t", plain="t", group="x", is_alt=False,
                  pinned=False, skills=set(), context_skills=set(), numbers=set(), words=set(), lines=1,
                  parts={"coverage": 1.0, "role": 0.5, "keywords": 0.2, "evidence": 0.5, "recency": 0.3,
                         "llm": 0.0, "semantic": 0.0, "short_last_line": 1.0})
    monkeypatch.setenv("LEARNED_WEIGHTS", str(f))
    planner._learned.update(checked=0.0, key=None, value=None)
    hand = planner.quality(c)
    assert planner.active_weights()[2] == "hand-set"            # no file yet

    f.write_text(json.dumps({"quality_weights": {**planner.QUALITY_WEIGHTS, "evidence": 2.0}, "short_line_factor": 0.6}))
    planner._learned.update(checked=0.0)
    w, factor, source = planner.active_weights()
    assert source == "learned" and w["evidence"] == 2.0 and factor == 0.6
    assert planner.quality(c) != hand

    f.write_text(json.dumps({"quality_weights": {"evidence": -1}, "short_line_factor": 0.6}))   # out of range
    planner._learned.update(checked=0.0, key=None)
    assert planner.active_weights()[2] == "hand-set"

    monkeypatch.setenv("LEARNED_WEIGHTS", "off")
    planner._learned.update(checked=0.0, key=None)
    assert planner.active_weights()[2] == "hand-set" and planner.quality(c) == hand
