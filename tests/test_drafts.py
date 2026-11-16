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


@needs_tex
def test_add_an_item_past_the_section_limit(fake, client, small_bank):
    """Only 2 experiences show (limit 2); you add a third: it goes in, the limit rises for this resume only,
    other items lose bullets as needed, and it still fits one page. No model call."""
    bigger_bank(client, small_bank)                       # max_experiences=2, plus "cafe" and "lab2" (overlaps lab)
    m = fake()
    d = plan(client)
    exp = items(d, "experiences")
    assert len(exp) == 2
    unused = {u["id"]: u for u in d["options"]["experiences"]["unused"]}
    target = "cafe" if "cafe" in unused else next(i for i, u in unused.items() if not u["overlaps"])
    url = f"/api/drafts/{d['id']}/edit"

    # refusals: already on the page; same work as a printed item
    assert client.post(url, json={"add": [{"section": "experiences", "in": exp[0]}]}).status_code == 409
    if "lab" in exp and "lab2" in unused:
        assert client.post(url, json={"add": [{"section": "experiences", "in": "lab2"}]}).status_code == 409

    d2 = client.post(url, json={"add": [{"section": "experiences", "in": target}]}).json()
    exp2 = items(d2, "experiences")
    assert target in exp2 and len(exp2) == 3, exp2
    assert len(m.calls) == 1
    assert d2["preview"]["lines_used"] <= d2["preview"]["budget"] + 0.01

    # build: the added experience is printed, one page
    run = finish(client, client.post(f"/api/drafts/{d['id']}/build").json())
    assert run["validation"]["one_page"]
    assert target in [e["id"] for e in run["selection"]["experiences"] if e["bullet_ids"]]

    # reset drops the added item and the raised limit
    d3 = client.post(f"/api/drafts/{d['id']}/reset").json()
    assert items(d3, "experiences") == exp


@needs_tex
def test_add_best_fit_and_swap_a_project_for_an_experience(fake, client, small_bank):
    bigger_bank(client, small_bank)
    fake()
    d = plan(client)
    proj = items(d, "projects")
    url = f"/api/drafts/{d['id']}/edit"
    # remove a project and add an experience in the same change ("best fit" picks which)
    r = client.post(url, json={"items": [{"out": proj[0], "in": None}], "add": [{"section": "experiences", "in": "any"}]})
    assert r.status_code == 200, r.text
    d2 = r.json()
    assert proj[0] not in items(d2, "projects")
    assert len(items(d2, "experiences")) == len(items(d, "experiences")) + 1


@needs_tex
def test_best_fit_never_brings_back_what_you_removed(fake, client, small_bank):
    bigger_bank(client, small_bank)
    fake()
    d = plan(client)
    url = f"/api/drafts/{d['id']}/edit"
    removed = items(d, "experiences")[0]
    d2 = client.post(url, json={"items": [{"out": removed, "in": None}]}).json()
    for _ in range(2):                                            # add "best fit" twice
        r = client.post(url, json={"add": [{"section": "experiences", "in": "any"}]})
        if r.status_code == 409:                                  # ran out of other experiences: fine
            break
        d2 = r.json()
        assert removed not in items(d2, "experiences")
    # picking it by name still works
    d3 = client.post(url, json={"add": [{"section": "experiences", "in": removed}]})
    assert d3.status_code == 200 and removed in items(d3.json(), "experiences")


@needs_tex
def test_add_a_point_past_the_per_item_limit(fake, client, small_bank):
    """An item shows 3 points (the limit); you add a 4th: its 3 stay, the limit rises for that item only,
    and the room comes from elsewhere. Best fit never re-adds a point you removed."""
    import bank as bankmod
    bigger_bank(client, small_bank)
    b = bankmod.load_dict(client.mod.BANK_PATH)
    b["settings"]["max_bullets_per_item"] = 3
    acme = next(i for i in b["experiences"] if i["id"] == "acme")
    acme["bullets"] += [{"id": "acme-x1", "text": "Wrote Python scripts that cut nightly batch jobs from 40 to 12 minutes."},
                        {"id": "acme-x2", "text": "Documented 15 internal REST endpoints in OpenAPI for the support team."}]
    bankmod.save(client.mod.BANK_PATH, b)
    m = fake()
    d = plan(client)
    pv = {it["id"]: [x["id"] for x in it["bullets"]] for s in ("experiences", "projects") for it in d["preview"][s]}
    if "acme" not in pv:
        pytest.skip("acme not planned for this posting")
    before = pv["acme"]
    assert len(before) == 3
    url = f"/api/drafts/{d['id']}/edit"
    assert client.post(url, json={"add_bullets": [{"item": "web", "in": "any"}]}).status_code == 400   # not on page
    other = next(x for i, xs in pv.items() if i != "acme" for x in xs)
    assert client.post(url, json={"add_bullets": [{"item": "acme", "in": other}]}).status_code == 409

    d2 = client.post(url, json={"add_bullets": [{"item": "acme", "in": "any"}]}).json()
    now = [x["id"] for it in d2["preview"]["experiences"] if it["id"] == "acme" for x in it["bullets"]]
    assert len(now) == 4 and set(before) <= set(now), now
    assert len(m.calls) == 1

    # remove the new point, then best fit again: it picks a different one, not the removed point
    added = (set(now) - set(before)).pop()
    d3 = client.post(url, json={"bullets": [{"out": added, "in": None}]}).json()
    r = client.post(url, json={"add_bullets": [{"item": "acme", "in": "any"}]})
    if r.status_code == 200:
        again = [x["id"] for it in r.json()["preview"]["experiences"] if it["id"] == "acme" for x in it["bullets"]]
        assert added not in again

    run = finish(client, client.post(f"/api/drafts/{d['id']}/build").json())
    assert run["validation"]["one_page"]
