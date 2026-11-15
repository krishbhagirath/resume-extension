"""A/B comparison ("Make 2 versions"), picks, and manual bullet swaps. Mocked model: no Claude usage."""
import importlib
import json
import sys
import time

import pytest
from fastapi.testclient import TestClient

import bank as bankmod
import llm
import pipeline
import tailor
from conftest import JDS, needs_tex
from test_pipeline import FakeModel, wait

PARTS = {"coverage", "keywords", "role", "evidence", "recency", "llm"}


@pytest.fixture
def fake(monkeypatch):
    def install(**kw):
        m = FakeModel(**kw)
        m.cancelled = set()
        monkeypatch.setattr(llm, "available", lambda: True)
        monkeypatch.setattr(llm, "engine", lambda: "claude-code")
        monkeypatch.setattr(llm, "complete_json", m)
        monkeypatch.setattr(llm, "cancel", lambda jid: m.cancelled.add(jid))
        return m
    return install


@pytest.fixture
def client(tmp_path, monkeypatch, small_bank):
    """The real app on a temp copy of a small synthetic bank (your bank files are never touched)."""
    data = tmp_path / "data"
    data.mkdir()
    bankmod.save(data / "experiences.yaml", small_bank)
    monkeypatch.setenv("BANK_PATH", str(data / "experiences.yaml"))
    monkeypatch.setenv("OUTPUT_DIR", str(tmp_path / "output"))
    monkeypatch.setenv("PORT", "8765")
    sys.modules.pop("app", None)
    app = importlib.import_module("app")
    c = TestClient(app.app, base_url="http://127.0.0.1:8765", headers={"X-RT-Token": app.TOKEN})
    c.mod = app
    c.out = tmp_path / "output"
    return c


def picks_rows(out):
    f = out / "picks.jsonl"
    return [json.loads(x) for x in f.read_text(encoding="utf-8").splitlines()] if f.exists() else []


def finish(c, job):
    j = wait(c.mod.store, job["id"])
    assert j.state == "completed", j.error
    return c.get(f"/api/run/{j.result['run_id']}").json()


# ---------------------------------------------------------------- tailor / pipeline

@needs_tex
def test_matcher_variant_skips_the_model(fake, small_bank):
    m = fake()
    res = tailor.run(small_bank, JDS["backend"], use_llm=False)
    assert m.calls == [] and res.stats["mode"] == "offline"
    assert res.bullet_scores and all(set(b["parts"]) == PARTS and b["parts"]["llm"] == 0 for b in res.bullet_scores)
    printed = {p["bullet_id"] for p in res.provenance}
    assert {b["id"] for b in res.bullet_scores} == printed


@needs_tex
def test_include_and_exclude(small_bank):
    small_bank["settings"]["max_bullets_per_item"] = 2   # so some bullets are left over
    base = tailor.run(small_bank, JDS["backend"])
    printed = [p["bullet_id"] for p in base.provenance]
    unused = [b["id"] for s in ("experiences", "projects") for it in small_bank[s] if not it.get("hidden")
              for b in it["bullets"] if b["id"] not in printed and not b.get("alt_of")]
    assert unused, "the small bank should have a bullet left over"
    out, inc = printed[-1], unused[0]
    res = tailor.run(small_bank, JDS["backend"], exclude={out}, include={inc})
    now = {p["bullet_id"] for p in res.provenance}
    assert out not in now and inc in now
    assert res.measure["pages"] == 1


@needs_tex
def test_variant_is_part_of_the_idempotency_key(fake, tmp_path, small_bank):
    m = fake()
    bank = tmp_path / "data" / "experiences.yaml"
    bank.parent.mkdir()
    bankmod.save(bank, small_bank)
    store = pipeline.JobStore(tmp_path / "output", bank)
    req = {"jd": JDS["backend"], "company": "Example", "role": "Backend Intern"}
    a = store.start({**req, "variant": "claude"})
    b = store.start({**req, "variant": "matcher"})
    assert a.id != b.id and a.key != b.key
    assert store.start({**req, "variant": "matcher"}).id == b.id      # duplicate click: same job
    assert store.start(dict(req)).id == a.id                          # no variant = claude
    a, b = wait(store, a.id), wait(store, b.id)
    assert a.state == b.state == "completed"
    assert len(m.calls) == 1                                          # only the claude version asked the model
    ra = json.loads((store.out / a.result["run_id"] / "run.json").read_text())
    rb = json.loads((store.out / b.result["run_id"] / "run.json").read_text())
    assert a.result["run_id"] != b.result["run_id"]                   # same company, same second: separate folders
    assert (ra["variant"], ra["mode"]) == ("claude", "claude-code")
    assert (rb["variant"], rb["mode"]) == ("matcher", "offline")
    assert any(x["parts"]["llm"] > 0 for x in ra["bullet_scores"])
    assert all(x["parts"]["llm"] == 0 for x in rb["bullet_scores"])
    with pytest.raises(ValueError):
        store.start({**req, "variant": "gpt"})


def test_run_folders_never_collide(tmp_path):
    from conftest import BANK_FILE
    store = pipeline.JobStore(tmp_path, BANK_FILE)
    ids = {store._new_run_dir("Acme")[0] for _ in range(3)}
    assert len(ids) == 3 and all((tmp_path / i).is_dir() for i in ids)


# ---------------------------------------------------------------- API

def test_compare_needs_claude(client):
    r = client.post("/api/compare", json={"jd": JDS["backend"]})
    assert r.status_code == 409 and "Claude" in r.json()["detail"]


def test_compare_and_picks_need_the_token(client):
    no_token = {"X-RT-Token": ""}
    assert client.post("/api/compare", json={"jd": JDS["backend"]}, headers=no_token).status_code == 401
    assert client.post("/api/picks", json={"run_a": "x", "run_b": "y", "winner": "a"}, headers=no_token).status_code == 401


@needs_tex
def test_compare_then_pick(fake, client):
    m = fake()
    r = client.post("/api/compare", json={"jd": JDS["backend"], "company": "Example", "role": "Backend Intern"})
    assert r.status_code == 200, r.text
    ja, jb = r.json()["a"], r.json()["b"]
    assert {ja["request"]["variant"], jb["request"]["variant"]} == {"claude", "matcher"}
    assert "jd" not in ja["request"]
    ra, rb = finish(client, ja), finish(client, jb)
    assert len(m.calls) == 1

    bad = client.post("/api/picks", json={"run_a": ra["id"], "run_b": rb["id"], "winner": "a", "reasons": ["vibes"]})
    assert bad.status_code == 422
    assert client.post("/api/picks", json={"run_a": ra["id"], "run_b": ra["id"], "winner": "a"}).status_code == 400
    assert client.post("/api/picks", json={"run_a": ra["id"], "run_b": "../etc", "winner": "a"}).status_code == 404
    assert client.post("/api/picks", json={"run_a": ra["id"], "run_b": rb["id"], "winner": "c"}).status_code == 422
    assert picks_rows(client.out) == []

    r = client.post("/api/picks", json={"run_a": ra["id"], "run_b": rb["id"], "winner": "b",
                                        "reasons": ["bullets", "skills", "bullets"]})
    assert r.status_code == 200, r.text
    assert r.json()["variants"] == {"a": ra["variant"], "b": rb["variant"]}
    [row] = picks_rows(client.out)
    assert row["source"] == "compare" and row["winner"] == "b" and row["reasons"] == ["bullets", "skills"]
    assert row["company"] == "Example" and JDS["backend"][:40] not in json.dumps(row)   # no posting text
    for side, run in (("a", ra), ("b", rb)):
        s = row[side]
        assert s["run_id"] == run["id"] and s["variant"] == run["variant"]
        assert s["scorecard"]["supported_coverage"] == run["scorecard"]["supported_coverage"]
        assert {b["id"] for b in s["bullets"]} == {p["bullet_id"] for p in run["provenance"]}
        assert all(set(b["parts"]) == PARTS for b in s["bullets"])


@needs_tex
def test_jobs_endpoint_accepts_variant(client):
    assert client.post("/api/jobs", json={"jd": JDS["backend"], "variant": "nope"}).status_code == 422
    run = finish(client, client.post("/api/jobs", json={"jd": JDS["backend"], "company": "X", "role": "Y",
                                                         "variant": "matcher"}).json())
    assert run["variant"] == "matcher" and run["mode"] == "offline"


@needs_tex
def test_manual_swap_rebuilds_and_logs_a_pick(client):
    base = finish(client, client.post("/api/jobs", json={"jd": JDS["backend"], "company": "X", "role": "Y"}).json())
    printed = {p["bullet_id"]: p["item_id"] for p in base["provenance"]}
    opts = client.get(f"/api/run/{base['id']}/swap-options").json()
    assert set(opts) == set(printed.values())
    assert all(o["id"] not in printed for rows in opts.values() for o in rows)
    pairs = [(b, i, o["id"]) for b, i in printed.items() for o in opts.get(i, [])
             if not o["blocked"] and o["same_as"] in (None, b)]
    out, item, new_in = pairs[0]
    other_item = next((o["id"] for i, rows in opts.items() if i != item for o in rows), "fw-power")
    # another wording of a printed bullet can only replace that bullet, not a different one
    clash = [(b, o["id"]) for b, i in printed.items() for o in opts.get(i, []) if o["same_as"] not in (None, b)]
    assert clash, "small bank: acme-api-alt is another wording of a printed bullet"
    r = client.post(f"/api/run/{base['id']}/swap", json={"out": clash[0][0], "in": clash[0][1]})
    assert r.status_code == 409 and "another wording" in r.json()["detail"]

    # refused: bullet not on the page, bullet from another item, unknown bullet
    assert client.post(f"/api/run/{base['id']}/swap", json={"out": "web-1", "in": new_in}).status_code == 400
    assert client.post(f"/api/run/{base['id']}/swap", json={"out": out, "in": other_item}).status_code == 409
    assert client.post(f"/api/run/{base['id']}/swap", json={"out": out, "in": "nope"}).status_code == 409

    r = client.post(f"/api/run/{base['id']}/swap", json={"out": out, "in": new_in})
    assert r.status_code == 200, r.text
    new = finish(client, r.json())
    now = {p["bullet_id"] for p in new["provenance"]}
    assert out not in now and new_in in now and new["validation"]["one_page"]
    assert new["overrides"] == {"include": [new_in], "exclude": [out], "include_items": [], "exclude_items": []}
    [row] = picks_rows(client.out)
    assert row["source"] == "swap" and row["winner"] == "b" and row["reasons"] == ["bullets"]
    assert row["a"]["run_id"] == base["id"] and row["b"]["run_id"] == new["id"]
    assert row["swap"] == {"kind": "bullet", "out": out, "in": new_in, "applied": True}

    # swapping back undoes the override instead of piling up contradictions
    back = finish(client, client.post(f"/api/run/{new['id']}/swap", json={"out": new_in, "in": out}).json())
    assert back["overrides"] == {"include": [out], "exclude": [new_in], "include_items": [], "exclude_items": []}
    assert len(picks_rows(client.out)) == 2


def test_strict_lines_blocks_short_bullets_in_swaps(client, monkeypatch):
    """With strict_lines on, a bullet that leaves a word or two on its last line can't be swapped in."""
    import linefit
    from pathlib import Path
    b = bankmod.load_dict(client.mod.BANK_PATH)
    b["settings"]["strict_lines"] = True
    bankmod.save(client.mod.BANK_PATH, b)
    run_id = "20260101-000000_X"
    d = client.out / run_id
    d.mkdir(parents=True)
    (d / "run.json").write_text(json.dumps({
        "id": run_id, "filename": "x.pdf", "jd": JDS["backend"], "variant": "matcher",
        "provenance": [{"bullet_id": "acme-api", "item_id": "acme"}, {"bullet_id": "acme-ci", "item_id": "acme"}]}))
    monkeypatch.setattr(linefit, "bank_fit", lambda bank: {
        "acme-ui": {"lines": 2, "last_fill": 0.1, "first_fill": 1.0, "chars_per_line": 110},
        "acme-api-alt": {"lines": 1, "last_fill": 0.9, "first_fill": 0.9, "chars_per_line": 110}})
    opts = {o["id"]: o for o in client.get(f"/api/run/{run_id}/swap-options").json()["acme"]}
    assert opts["acme-ui"]["blocked"] and not opts["acme-api-alt"]["blocked"]
    r = client.post(f"/api/run/{run_id}/swap", json={"out": "acme-api", "in": "acme-ui"})
    assert r.status_code == 409 and "strict" in r.json()["detail"]
    assert not Path(client.out / "picks.jsonl").exists()


# ---------------------------------------------------------------- replacing a whole item

def bigger_bank(client, small_bank):
    """Two more items, and limits low enough that some items stay off the page."""
    b = small_bank
    b["settings"].update(max_experiences=2, max_projects=1)
    b["experiences"].append({"id": "cafe", "title": "Barista Lead", "org": "Corner Cafe", "kind": "work",
                             "dates": "2023 – 2024", "bullets": [
                                 {"id": "cafe-1", "text": "Trained 6 new baristas and cut average wait time by 2 minutes."},
                                 {"id": "cafe-2", "text": "Built a Python script that tracked inventory for 40 items."}]})
    b["experiences"].append({"id": "lab2", "title": "Lab Volunteer", "org": "Example Lab", "kind": "research",
                             "dates": "2024", "overlaps": ["lab"], "bullets": [
                                 {"id": "lab2-1", "text": "Labeled 2k images for the defect-detection dataset."},
                                 {"id": "lab2-2", "text": "Reviewed OpenCV preprocessing output with the team."}]})
    b["projects"].append({"id": "bot", "title": "Chat Bot", "stack": "Python, FastAPI", "bullets": [
        {"id": "bot-1", "text": "Built a FastAPI chat bot answering 200 FAQ questions for a student club."},
        {"id": "bot-2", "text": "Added pytest coverage for 30 bot intents in GitHub Actions CI."}]})
    bankmod.save(client.mod.BANK_PATH, b)


@needs_tex
def test_replace_whole_item_before_download(client, small_bank):
    bigger_bank(client, small_bank)
    base = finish(client, client.post("/api/jobs", json={"jd": JDS["backend"], "company": "X", "role": "Y"}).json())
    items = client.get(f"/api/run/{base['id']}/items").json()
    on_exp = [i["id"] for i in items["experiences"]["on_page"]]
    unused_exp = {i["id"]: i for i in items["experiences"]["unused"]}
    assert len(on_exp) == 2 and all(i["bullets"] >= 1 and i["title"] for i in items["experiences"]["on_page"])
    assert "web" not in [i["id"] for i in items["projects"]["unused"]]          # hidden items never offered
    assert set(on_exp) | set(unused_exp) == {"acme", "lab", "cafe", "lab2"}

    out = next(i for i in on_exp if i != "lab")                                  # replace something that isn't lab
    new_in = "cafe" if "cafe" in unused_exp else next(iter(unused_exp))
    # refusals: not on the page, other section, same work as another printed item
    assert client.post(f"/api/run/{base['id']}/replace", json={"out": "web", "in": new_in}).status_code == 400
    assert client.post(f"/api/run/{base['id']}/replace", json={"out": out, "in": "bot"}).status_code == 409
    if "lab" in on_exp and "lab2" in unused_exp:
        r = client.post(f"/api/run/{base['id']}/replace", json={"out": out, "in": "lab2"})
        assert r.status_code == 409 and "same work" in r.json()["detail"]

    new = finish(client, client.post(f"/api/run/{base['id']}/replace", json={"out": out, "in": new_in}).json())
    now = [e["id"] for e in new["selection"]["experiences"] if e["bullet_ids"]]
    assert out not in now and new_in in now and new["validation"]["one_page"]
    assert new["overrides"]["exclude_items"] == [out] and new["overrides"]["include_items"] == [new_in]
    [row] = picks_rows(client.out)
    assert row["source"] == "swap" and row["reasons"] == ["projects"] and row["winner"] == "b"
    assert row["swap"] == {"kind": "item", "out": out, "in": new_in, "applied": True, "action": "replace", "chosen": "you"}

    # a later bullet swap keeps the replacement
    opts = client.get(f"/api/run/{new['id']}/swap-options").json()
    printed = {p["bullet_id"]: p["item_id"] for p in new["provenance"]}
    pair = next(((b, o["id"]) for b, i in printed.items() for o in opts.get(i, [])
                 if not o["blocked"] and o["same_as"] in (None, b)), None)
    if pair:
        after = finish(client, client.post(f"/api/run/{new['id']}/swap", json={"out": pair[0], "in": pair[1]}).json())
        assert after["overrides"]["exclude_items"] == [out] and after["overrides"]["include_items"] == [new_in]
        assert out not in [e["id"] for e in after["selection"]["experiences"] if e["bullet_ids"]]


@needs_tex
def test_remove_item_and_replace_with_best_fit(client, small_bank):
    bigger_bank(client, small_bank)
    base = finish(client, client.post("/api/jobs", json={"jd": JDS["backend"], "company": "X", "role": "Y"}).json())
    items = client.get(f"/api/run/{base['id']}/items").json()
    on_exp = [i["id"] for i in items["experiences"]["on_page"]]
    out = next(i for i in on_exp if i != "lab")

    # remove: the item is gone and nothing is forced in; the space goes to whatever fits best
    gone = finish(client, client.post(f"/api/run/{base['id']}/replace", json={"out": out, "in": None}).json())
    assert out not in [e["id"] for e in gone["selection"]["experiences"] if e["bullet_ids"]]
    assert gone["overrides"]["exclude_items"] == [out] and gone["overrides"]["include_items"] == []
    assert gone["validation"]["one_page"]
    row = picks_rows(client.out)[-1]
    assert row["swap"]["action"] == "remove" and row["swap"]["in"] is None and row["swap"]["applied"] is True
    assert row["reasons"] == ["projects"]

    # "any": the server picks the unused item that best fits the posting, never one overlapping a different printed item
    r = client.post(f"/api/run/{base['id']}/replace", json={"out": out, "in": "any"})
    assert r.status_code == 200, r.text
    chosen = r.json()["replaced_with"]
    assert chosen["section"] == "experiences" and chosen["id"] not in on_exp
    assert not set(chosen["overlaps"]) - {out}
    new = finish(client, r.json())
    now = [e["id"] for e in new["selection"]["experiences"] if e["bullet_ids"]]
    assert out not in now and chosen["id"] in now
    row = picks_rows(client.out)[-1]
    assert row["swap"]["chosen"] == "auto" and row["swap"]["in"] == chosen["id"]

    # removing something that isn't on the page is refused
    assert client.post(f"/api/run/{base['id']}/replace", json={"out": "web", "in": None}).status_code == 400


@needs_tex
def test_batch_edit_items_and_bullets_at_once(client, small_bank):
    bigger_bank(client, small_bank)
    base = finish(client, client.post("/api/jobs", json={"jd": JDS["backend"], "company": "X", "role": "Y"}).json())
    opts = client.get(f"/api/run/{base['id']}/edit-options").json()
    exp = opts["experiences"]["on_page"]
    assert all(it["printed"] and "spare" in it for it in exp)
    assert {b["id"] for it in exp for b in it["printed"]} <= {p["bullet_id"] for p in base["provenance"]}
    out = next(it["id"] for it in exp if it["id"] != "lab")
    other = next(it for s in ("experiences", "projects") for it in opts[s]["on_page"] if it["id"] != out)
    gone_bullet = other["printed"][0]["id"]
    swap = next(((b["id"], o["id"]) for b in other["printed"][1:] for o in other["spare"]
                 if not o["blocked"] and o["same_as"] in (None, b["id"])), None)
    edit = {"items": [{"out": out, "in": "any"}],
            "bullets": [{"out": gone_bullet, "in": None}] + ([{"out": swap[0], "in": swap[1]}] if swap else [])}

    # refusals
    url = f"/api/run/{base['id']}/edit"
    assert client.post(url, json={}).status_code == 400
    assert client.post(url, json={"items": [{"out": out, "in": None}, {"out": out, "in": "any"}]}).status_code == 400
    assert client.post(url, json={"items": [{"out": out, "in": None}],
                                  "bullets": [{"out": next(p["bullet_id"] for p in base["provenance"]
                                                           if p["item_id"] == out), "in": None}]}).status_code == 409
    both = [i["id"] for i in exp]
    if len(both) == 2:
        target = next(u["id"] for u in opts["experiences"]["unused"] if not u["blocked"] and not u["overlaps"])
        r = client.post(url, json={"items": [{"out": both[0], "in": target}, {"out": both[1], "in": target}]})
        assert r.status_code == 409   # the same item can't come in twice

    r = client.post(url, json=edit)
    assert r.status_code == 200, r.text
    chosen = next(c for c in r.json()["changes"] if c["kind"] == "item")["in"]
    new = finish(client, r.json())
    items_now = [e["id"] for e in new["selection"]["experiences"] if e["bullet_ids"]]
    bullets_now = {p["bullet_id"] for p in new["provenance"]}
    assert out not in items_now and chosen in items_now
    assert gone_bullet not in bullets_now and new["validation"]["one_page"]
    if swap:
        assert swap[0] not in bullets_now and swap[1] in bullets_now
    assert out in new["overrides"]["exclude_items"] and chosen in new["overrides"]["include_items"]
    assert gone_bullet in new["overrides"]["exclude"]
    [row] = picks_rows(client.out)       # one pick for the whole batch
    assert row["swap"]["kind"] == "batch" and set(row["reasons"]) == {"projects", "bullets"}
    assert len(row["swap"]["changes"]) == len(edit["items"]) + len(edit["bullets"])
    assert row["swap"]["applied"] is True and all(c["applied"] for c in row["swap"]["changes"])
