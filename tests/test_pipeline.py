"""The job state machine with a mocked model (no Claude usage)."""
import shutil
import time

import pytest

import llm
import pipeline
import render
import tailor
from conftest import JDS, ROOT, needs_tex, BANK_FILE


class FakeModel:
    def __init__(self, rephrase=None, fail=None, delay=0.0):
        self.calls = []
        self.rephrase = rephrase or {}
        self.fail = fail
        self.delay = delay

    def __call__(self, prompt, schema, **kw):
        self.calls.append(prompt)
        if self.delay:
            end = time.time() + self.delay
            while time.time() < end:
                if kw.get("job_id") in getattr(self, "cancelled", set()):
                    raise llm.LLMError("Cancelled.", False, "cancelled")
                time.sleep(0.05)
        if self.fail:
            raise llm.LLMError("You've hit your Claude usage limit.", True, self.fail)
        st = {"requests": 1, "input_tokens": len(prompt) // 4, "output_tokens": 200}
        if "ranked_ids" in schema["properties"]:
            ids = [ln.split(" ", 1)[0] for ln in prompt.splitlines() if ln[:1].isalnum() and " [" in ln]
            return {"company": "Example", "role": "Machine Learning Intern",
                    "ranked_ids": ["not-a-real-id"] + ids[:20], "note": ""}, st
        return {"bullets": [{"id": k, "text": v} for k, v in self.rephrase.items()]}, st


@pytest.fixture
def fake(monkeypatch):
    def install(**kw):
        m = FakeModel(**kw)
        monkeypatch.setattr(llm, "available", lambda: True)
        monkeypatch.setattr(llm, "engine", lambda: "claude-code")
        monkeypatch.setattr(llm, "complete_json", m)
        m.cancelled = set()
        monkeypatch.setattr(llm, "cancel", lambda jid: m.cancelled.add(jid))
        return m
    return install


@pytest.fixture
def store(tmp_path):
    bank = tmp_path / "data" / "experiences.yaml"
    bank.parent.mkdir()
    shutil.copy(BANK_FILE, bank)
    return pipeline.JobStore(tmp_path / "output", bank)


def wait(store, jid, timeout=120):
    end = time.time() + timeout
    while time.time() < end:
        j = store.get(jid)
        if j.state in pipeline.TERMINAL:
            return j
        time.sleep(0.1)
    raise AssertionError("job didn't finish")


@needs_tex
def test_model_ranking_ignores_unknown_ids(fake, real_bank):
    m = fake()
    res = tailor.run(real_bank, JDS["ml"])
    assert len(m.calls) == 1
    printed = {p["bullet_id"] for p in res.provenance}
    assert "not-a-real-id" not in printed
    assert res.stats["mode"] == "claude-code" and res.role == "Machine Learning Intern"
    # the pool sent to the model is limited (not the whole bank)
    import re
    assert sum(1 for ln in m.calls[0].splitlines() if re.match(r"^[\w-]+ \[", ln)) <= tailor.POOL_SIZE


@needs_tex
def test_rephrase_only_few_targets_and_line_checked(fake, small_bank):
    """Only bullets with a reason are sent (short last line / tagged skill), at most max_rephrase;
    bad rewordings are rejected; accepted ones keep XYZ shape and fit their line target."""
    b = small_bank
    b["settings"]["rephrase"] = True
    b["experiences"][0]["bullets"][0]["text"] = (
        "Built a Python FastAPI service backed by PostgreSQL for the internal billing and invoicing team, "
        "cutting p95 latency by 40\\% across all regions.")
    b["experiences"][1]["pinned"] = True
    b["experiences"][1]["bullets"][0]["skills"] = ["Docker"]
    good = "Built a Python FastAPI service on PostgreSQL for the billing team, cutting p95 latency by 40% across regions."
    bad = "Trained PyTorch CNN models in Docker for 2 million users, reaching 94% accuracy."
    m = fake(rephrase={"acme-api": good, "lab-cv": bad, "acme-ci": "Added tests."})
    res = tailor.run(b, JDS["backend"])
    assert len(m.calls) == 2
    sent = [ln.split("id: ")[1] for ln in m.calls[1].splitlines() if ln.startswith("- id: ")]
    assert set(sent) <= {"acme-api", "lab-cv"} and len(sent) <= b["settings"]["max_rephrase"]
    assert "acme-ci" not in sent  # no reason to touch it
    prov = {p["bullet_id"]: p for p in res.provenance}
    assert prov["acme-api"]["printed"] == "rephrased" and prov["acme-api"]["lines"] == 1
    assert prov["lab-cv"]["printed"] == "bank" and prov["lab-cv"].get("rejected")
    assert "2 million" not in res.tex
    assert res.scorecard["unsupported_claims"] == 0 and res.stats["rephrase_targets"] == len(sent)


@needs_tex
def test_no_rephrase_call_when_nothing_needs_it(fake, small_bank):
    small_bank["settings"]["rephrase"] = True
    m = fake()
    tailor.run(small_bank, JDS["backend"])
    assert len(m.calls) == 1  # ranking only


@needs_tex
def test_usage_limit_falls_back_offline(fake, real_bank):
    fake(fail="usage_limit")
    res = tailor.run(real_bank, JDS["backend"])
    assert res.measure["pages"] == 1
    assert res.stats["mode"] == "offline"
    assert any("usage limit" in w for w in res.warnings)


@needs_tex
def test_job_lifecycle_and_idempotency(store):
    req = {"jd": JDS["backend"], "company": "Example", "role": "Backend Intern"}
    j = store.start(req)
    again = store.start(dict(req))
    assert again.id == j.id  # duplicate click while running
    done = wait(store, j.id)
    assert done.state == "completed", done.error
    states = [p["state"] for p in done.progress]
    assert states[0] == "analyzing" and states[-1] == "completed"
    assert (store.out / done.result["run_id"] / done.result["filename"]).exists()
    assert store.start(dict(req)).id == j.id  # same inputs -> same finished result
    forced = store.start({**req, "force": True})
    assert forced.id != j.id
    wait(store, forced.id)
    log = (store.out / "logs" / "pipeline.jsonl").read_text()
    assert '"event": "completed"' in log and JDS["backend"][:40] not in log  # no posting text in logs


def test_low_confidence_fails_clearly(store):
    j = wait(store, store.start({"jd": JDS["not_a_job"]}).id)
    assert j.state == "failed" and j.error["code"] == "low_confidence" and not j.error["retryable"]


def test_cancel(fake, store):
    fake(delay=5)
    j = store.start({"jd": JDS["ml"], "company": "X", "role": "Y"})
    time.sleep(0.4)
    store.cancel(j.id)
    j = wait(store, j.id, 30)
    assert j.state == "cancelled"


def test_compile_error_is_reported(store, monkeypatch):
    def boom(src, tpl, timeout=180):
        raise render.CompileError("A bullet has an unbalanced { or }.", "! Runaway argument")
    monkeypatch.setattr(render, "compile_pdf", boom)
    import layout
    monkeypatch.setattr(layout.render, "compile_pdf", boom)
    j = wait(store, store.start({"jd": JDS["backend"], "company": "X", "role": "Y"}).id)
    assert j.state == "failed" and j.error["code"] == "compile"


def test_interrupted_jobs_recovered(tmp_path):
    bank = BANK_FILE
    s1 = pipeline.JobStore(tmp_path, bank)
    job = pipeline.Job(id="abc123", key="k", state="rendering")
    s1._save(job)
    s2 = pipeline.JobStore(tmp_path, bank)
    assert s2.get("abc123").state == "failed" and s2.get("abc123").error["code"] == "interrupted"
