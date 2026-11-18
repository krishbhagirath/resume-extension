import itertools

import pytest

import analyzer
import evaluate
import planner
import retrieval
from conftest import JDS

ROLE_JDS = ["backend", "ml", "embedded", "hardware", "cv", "fullstack"]


def _plan(bank, name):
    job = analyzer.analyze(JDS[name])
    cands, items = retrieval.score_bank(bank, job)
    sel = planner.plan(bank, job, cands, items)
    return job, sel, sel.ordered()


def _ids(p):
    return [b for e in p["experiences"] + p["projects"] for b in e["bullet_ids"]]


@pytest.mark.parametrize("name", ROLE_JDS)
def test_plan_respects_rules(real_bank, name):
    job, sel, p = _plan(real_bank, name)
    s = real_bank["settings"]
    assert 1 <= len(p["experiences"]) <= s["max_experiences"]
    assert len(p["projects"]) <= s["max_projects"]
    by_id = {b["id"]: b for sec in ("experiences", "projects") for i in real_bank[sec] for b in i["bullets"]}
    items = {i["id"]: i for sec in ("experiences", "projects") for i in real_bank[sec]}
    ids = _ids(p)
    assert len(ids) == len(set(ids))
    groups = [by_id[b].get("alt_of") or b for b in ids]
    assert len(groups) == len(set(groups)), "two wordings of one accomplishment"
    chosen = {e["id"] for e in p["experiences"] + p["projects"]}
    for iid in chosen:
        assert not items[iid].get("hidden")
        assert not (set(items[iid].get("overlaps") or []) & chosen), "overlapping items both used"
        n = len(next(e for e in p["experiences"] + p["projects"] if e["id"] == iid)["bullet_ids"])
        assert n <= s["max_bullets_per_item"]
    assert sel.used() <= planner.default_budget(real_bank) + 1e-6


@pytest.mark.parametrize("name", ROLE_JDS)
def test_coverage_is_reasonable(real_bank, name):
    job, sel, p = _plan(real_bank, name)
    card = evaluate.scorecard(job, sel, p, None, [])
    assert card["supported_coverage"] >= 0.6, card
    # never adds skills the bank can't support
    assert not set(card["covered"]) & set(card["unsupported"])


def test_different_roles_get_different_resumes(real_bank):
    plans = {n: _plan(real_bank, n)[2] for n in ROLE_JDS}
    pairs = {(a, b): evaluate.selection_overlap(plans[a], plans[b]) for a, b in itertools.combinations(ROLE_JDS, 2)}
    assert pairs[("backend", "hardware")] < 0.5, pairs
    assert pairs[("ml", "embedded")] < 0.6, pairs
    assert sum(pairs.values()) / len(pairs) < 0.6, pairs


def test_role_specific_content(real_bank):
    def items(name):
        return {e["id"] for e in _plan(real_bank, name)[2]["projects"]}
    assert "neuracore" in items("hardware")
    assert "fridgemind" in items("embedded")


def test_pinned_and_hidden(small_bank):
    small_bank["projects"][0]["pinned"] = True
    job, sel, p = _plan(small_bank, "ml")
    assert "fw" in {e["id"] for e in p["projects"]}
    assert "web" not in {e["id"] for e in p["projects"]}


def test_alt_wordings_exclusive(small_bank):
    job, sel, p = _plan(small_bank, "backend")
    ids = set(_ids(p))
    assert not {"acme-api", "acme-api-alt"} <= ids


@pytest.mark.parametrize("name", ["backend", "ml"])
def test_lead_point_prints_first(small_bank, name):
    """A point marked lead (it sums up the item) goes first under its item whenever it's chosen,
    even when it scores lowest."""
    _, _, before = _plan(small_bank, name)
    entry = next(e for e in before["experiences"] + before["projects"] if len(e["bullet_ids"]) >= 2)
    last = entry["bullet_ids"][-1]                      # the lowest-ranked point shown
    for sec in ("experiences", "projects"):
        for it in small_bank[sec]:
            for b in it["bullets"]:
                if b["id"] == last:
                    b["lead"] = True
    _, _, after = _plan(small_bank, name)
    now = next(e for e in after["experiences"] + after["projects"] if e["id"] == entry["id"])
    assert now["bullet_ids"][0] == last and set(now["bullet_ids"]) == set(entry["bullet_ids"])


@pytest.mark.parametrize("name", ["backend", "ml"])
def test_item_enters_through_its_lead_point(small_bank, name):
    """An item on the page always shows its intro point, even one that would never be picked on score,
    and squeezing the page never trims it (the whole item may still go)."""
    _, _, before = _plan(small_bank, name)
    sec = "experiences"
    item_id = before[sec][0]["id"]
    item = next(i for i in small_bank[sec] if i["id"] == item_id)
    item["bullets"].append({"id": "intro-x", "text": "Joined the team and attended weekly planning meetings.",
                            "lead": True})
    _, sel, after = _plan(small_bank, name)
    entry = next((e for e in after[sec] if e["id"] == item_id), None)
    assert entry and entry["bullet_ids"][0] == "intro-x"
    sel.budget = sel.used() - 2.5
    while sel.used() > sel.budget and sel.trim_once():
        pass
    ids = sel.chosen.get(item_id) or []
    assert not ids or "intro-x" in ids
