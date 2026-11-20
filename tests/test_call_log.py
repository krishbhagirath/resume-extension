"""The short debugging log of model calls: one line each, old lines dropped, never breaks a call."""
import time

import pytest

import llm


@pytest.fixture
def log(tmp_path, monkeypatch):
    monkeypatch.setattr(llm, "LOG_FILE", tmp_path / "logs" / "claude-calls.log")
    monkeypatch.setattr(llm, "CACHE_DIR", tmp_path / "cache")
    monkeypatch.setattr(llm, "engine", lambda: "api")            # account = "API key", no subprocess
    return llm.LOG_FILE


def lines(path):
    return path.read_text(encoding="utf-8").splitlines()


def test_live_cached_and_failed_calls_are_logged(log, monkeypatch):
    calls = []

    def fake_api(prompt, schema, system, timeout):
        calls.append(prompt)
        if prompt == "bad":
            raise llm.LLMError("boom")
        return {"ranked_ids": ["a"]}, {"input_tokens": 6000, "output_tokens": 1700}

    monkeypatch.setattr(llm, "_api", fake_api)
    schema = {"required": ["ranked_ids"]}
    llm.complete_json("rank these", schema, job_id="job1")
    llm.complete_json("rank these", schema, job_id="job2")            # same prompt: answered from the cache
    with pytest.raises(llm.LLMError):
        llm.complete_json("bad", schema, job_id="job3")
    got = lines(log)
    assert len(calls) == 2 and len(got) == 3
    assert " live " in got[0] and "API key" in got[0] and "in=6000 out=1700" in got[0] and "job=job1" in got[0]
    assert " cached " in got[1] and "in=0 out=0" in got[1] and "job=job2" in got[1]
    assert " failed " in got[2] and "error=boom" in got[2]


def test_old_lines_disappear(log, monkeypatch):
    now = time.time()
    llm.log_call("live", now=now - 5 * 86400)                       # older than LOG_KEEP_DAYS
    llm.log_call("live", now=now - 1 * 86400)
    llm.log_call("live", now=now)
    assert len(lines(log)) == 2
    monkeypatch.setattr(llm, "LOG_MAX_LINES", 3)
    for _ in range(5):
        llm.log_call("cached", now=now)
    assert len(lines(log)) == 3                                     # capped at the newest LOG_MAX_LINES


def test_logging_never_breaks_a_call(log, monkeypatch):
    monkeypatch.setattr(llm, "LOG_FILE", log.parent)                 # a directory: writing fails
    log.parent.mkdir(parents=True, exist_ok=True)
    llm.log_call("live")                                            # no exception
