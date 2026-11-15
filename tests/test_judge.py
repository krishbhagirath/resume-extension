"""tests/judge_pairs.py with a fake judge: position swap, ties, logging. No Claude usage."""
import json

import pytest

import judge_pairs as jp
import llm
import picks


def write_run(out, run_id, bullets, skills=("Python",), variant="claude"):
    d = out / run_id
    d.mkdir(parents=True)
    meta = {"id": run_id, "variant": variant, "mode": "claude-code" if variant == "claude" else "offline",
            "company": "Acme", "role": "Intern", "jd": "Backend intern. Python, PostgreSQL, Kafka. " * 5,
            "provenance": [{"bullet_id": b, "item_id": "acme", "source": f"text of {b}"} for b in bullets],
            "selection": {"experiences": [{"id": "acme", "bullet_ids": list(bullets)}], "projects": [],
                          "skills": [{"category": "Languages", "items": list(skills)}]},
            "scorecard": {"supported_coverage": 0.9},
            "bullet_scores": [{"id": b, "parts": {"coverage": 1.0, "role": 0.5}} for b in bullets]}
    (d / "run.json").write_text(json.dumps(meta))
    return meta


@pytest.fixture
def env(tmp_path, monkeypatch, small_bank):
    monkeypatch.setattr(jp, "OUT", tmp_path)
    monkeypatch.setattr(jp, "BANK", tmp_path / "bank.yaml")
    import bank as bankmod
    bankmod.save(tmp_path / "bank.yaml", small_bank)
    monkeypatch.setattr(llm, "engine", lambda: "claude-code")
    monkeypatch.setenv("CLAUDE_CODE_MODEL", "haiku")   # main() switches it to the judge model; restored after
    write_run(tmp_path, "r-claude", ["acme-api", "acme-ci"], variant="claude")
    write_run(tmp_path, "r-matcher", ["acme-api", "acme-ui"], variant="matcher")
    (tmp_path / "pairs.json").write_text(json.dumps([{"label": "Acme", "a": "r-claude", "b": "r-matcher"}]))
    return tmp_path


def fake_judge(monkeypatch, pick):
    """pick(prompt) -> 'A' | 'B' | 'tie', given the prompt with RESUME A / RESUME B in it."""
    calls = []

    def complete_json(prompt, schema, **kw):
        calls.append(prompt)
        assert "<job_posting>" in prompt and kw.get("system") == jp.JUDGE_SYSTEM
        assert "claude" not in prompt.lower().replace("claude-code", "")   # judge never told which side is which
        return {"winner": pick(prompt), "confidence": "high", "reasons": ["bullets"], "why": "x"}, \
            {"input_tokens": 100, "output_tokens": 20}
    monkeypatch.setattr(llm, "complete_json", complete_json)
    return calls


def prefers(bullet):
    """A judge that always picks whichever side shows `bullet` (consistent under the swap)."""
    def pick(prompt):
        a = prompt.split("RESUME A")[1].split("RESUME B")[0]
        return "A" if bullet in a else "B"
    return pick


def test_consistent_verdict_is_logged(env, monkeypatch, capsys):
    calls = fake_judge(monkeypatch, prefers("text of acme-ui"))
    assert jp.main(["--pairs", str(env / "pairs.json")]) == 0
    assert len(calls) == 2                                      # judged twice, swapped
    [row] = picks.read(env)
    assert row["source"] == "judge" and row["winner"] == "b"   # b = the matcher run, which has acme-ui
    assert row["judge"]["model"] == "sonnet" and row["reasons"] == ["bullets"]
    out = capsys.readouterr().out
    assert "Claude 0, built-in matcher 1" in out
    # rerun: already judged by this model, not logged twice
    jp.main(["--pairs", str(env / "pairs.json")])
    assert len(picks.read(env)) == 1


def test_position_bias_becomes_a_tie(env, monkeypatch):
    fake_judge(monkeypatch, lambda prompt: "A")                 # always picks whatever is shown first
    jp.main(["--pairs", str(env / "pairs.json")])
    assert picks.read(env) == []


def test_dry_run_and_identical(env, monkeypatch):
    fake_judge(monkeypatch, prefers("text of acme-ci"))
    jp.main(["--pairs", str(env / "pairs.json"), "--dry-run"])
    assert picks.read(env) == []
    write_run(env, "r-same", ["acme-api", "acme-ci"])
    (env / "same.json").write_text(json.dumps([{"a": "r-claude", "b": "r-same"}]))
    calls = fake_judge(monkeypatch, lambda p: "A")
    jp.main(["--pairs", str(env / "same.json")])
    assert calls == [] and picks.read(env) == []
