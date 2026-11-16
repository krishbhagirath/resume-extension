"""Preview first, compile once: plan jobs, instant draft edits, one build. Mocked model (no Claude usage)."""
import json
import time

import pytest

from conftest import JDS, needs_tex
from test_compare import bigger_bank, client, fake, finish, picks_rows  # noqa: F401  (fixtures + helpers)
from test_pipeline import wait


def plan(client, **extra):
    r = client.post("/api/jobs", json={"jd": JDS["backend"], "company": "X", "role": "Y", "mode": "plan", **extra})
    assert r.status_code == 200, r.text
    j = wait(client.mod.store, r.json()["id"])
    assert j.state == "completed", j.error
    assert "run_id" not in j.result                        # nothing compiled yet
    return client.get(f"/api/drafts/{j.result['draft_id']}").json()


def items(d, sec):
    return [it["id"] for it in d["preview"][sec]]


def bullets(d):
    return {b["id"]: b for s in ("experiences", "projects") for it in d["preview"][s] for b in it["bullets"]}


@needs_tex
def test_plan_edit_build(fake, client, small_bank):
    bigger_bank(client, small_bank)
    m = fake()
    d = plan(client)
    assert len(m.calls) == 1 and not list(client.out.glob("*/run.json"))     # one ranking call, no PDF
    assert d["preview"]["experiences"] and d["options"]["experiences"]["on_page"]
    exp = items(d, "experiences")

    # several edits; each re-plans immediately from the saved ranking: no new model call, no compile
    out = next(i for i in exp if i != "lab")
    keep = next(i for i in exp if i != out)
    first_b = d["preview"]["experiences"][exp.index(keep)]["bullets"]
    t = time.time()
    r = client.post(f"/api/drafts/{d['id']}/edit", json={"items": [{"out": out, "in": None}]})
    assert r.status_code == 200, r.text
    d2 = r.json()
    assert time.time() - t < 10 and len(m.calls) == 1
    assert out not in items(d2, "experiences") and d2["rev"] == d["rev"] + 1

    new_exp = items(d2, "experiences")
    edited_bid = first_b[0]["id"]
    my_words = "Built a Python FastAPI service on PostgreSQL that cut p95 latency 40% for the billing team."
    edit = {"order": {"experiences": list(reversed(new_exp))},
            "texts": {edited_bid: my_words}}
    if len(first_b) > 1:
        edit["bullet_order"] = {keep: [b["id"] for b in reversed(first_b)]}
    d3 = client.post(f"/api/drafts/{d['id']}/edit", json=edit).json()
    assert items(d3, "experiences") == list(reversed(new_exp))
    assert bullets(d3)[edited_bid]["text"] == my_words and bullets(d3)[edited_bid]["edited"]
    if len(first_b) > 1:
        kb = [b["id"] for it in d3["preview"]["experiences"] if it["id"] == keep for b in it["bullets"]]
        assert kb[0] == first_b[-1]["id"]
    assert len(m.calls) == 1 and not list(client.out.glob("*/run.json"))

    # refusals
    assert client.post(f"/api/drafts/{d['id']}/edit", json={}).status_code == 400
    assert client.post(f"/api/drafts/{d['id']}/edit", json={"texts": {"web-1": "x"}}).status_code == 400
    assert client.get("/api/drafts/000000000000").status_code == 404
    assert client.get("/api/drafts/../etc").status_code == 404

    # build once: your order and wording are printed; the fit step's changes (if any) are reported
    b = client.post(f"/api/drafts/{d['id']}/build")
    assert b.status_code == 200, b.text
    run = finish(client, b.json())
    assert len(m.calls) == 1                                           # still the one ranking call
    assert run["validation"]["one_page"] and run["draft_id"] == d["id"]
    prov = {p["bullet_id"]: p for p in run["provenance"]}
    if edited_bid in prov:
        assert prov[edited_bid]["printed"] == "edited" and prov[edited_bid]["text"] == my_words
    assert run["scorecard"]["unsupported_claims"] == 0
    built_exp = [e["id"] for e in run["selection"]["experiences"] if e["bullet_ids"]]
    assert [i for i in reversed(new_exp) if i in built_exp] == built_exp
    fc = run["fit_changes"]
    assert set(fc) == {"dropped", "added"}
    tex = next(client.out.glob(f"{run['id']}/*.tex")).read_text(encoding="utf-8")
    if edited_bid in prov:
        assert "billing team" in tex
    [row] = [r for r in picks_rows(client.out) if r["source"] == "edit"]
    assert row["winner"] == "b" and row["a"]["run_id"] == f"draft:{d['id']}"
    assert {"projects", "bullets", "layout"} <= set(row["reasons"])
    # building the same revision again reuses the finished PDF
    assert client.post(f"/api/drafts/{d['id']}/build").json()["id"] == b.json()["id"]

    # reset: back to the plan you started from
    d4 = client.post(f"/api/drafts/{d['id']}/reset").json()
    assert items(d4, "experiences") == exp and not d4["changed"]
    assert not any(x["edited"] for x in bullets(d4).values())


@needs_tex
def test_matcher_draft_makes_no_model_call(fake, client, small_bank):
    m = fake()
    d = plan(client, variant="matcher")
    assert m.calls == [] and d["mode"] == "offline"
    exp = items(d, "experiences")
    d2 = client.post(f"/api/drafts/{d['id']}/edit", json={"order": {"experiences": list(reversed(exp))}}).json()
    assert items(d2, "experiences") == list(reversed(exp)) and m.calls == []
