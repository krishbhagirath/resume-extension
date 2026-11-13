"""Real compiles (Tectonic, or the XeLaTeX stand-in on Linux) and real PDF measurements."""
import copy

import pytest

import analyzer
import layout
import planner
import render
import retrieval
import tailor
from conftest import JDS, needs_tex

pytestmark = needs_tex


def _sel(bank, jd):
    job = analyzer.analyze(jd)
    cands, items = retrieval.score_bank(bank, job)
    return planner.plan(bank, job, cands, items)


@pytest.mark.parametrize("name", ["backend", "ml", "embedded", "hardware", "cv", "fullstack"])
def test_exactly_one_page(real_bank, name):
    best, lay = layout.optimize(_sel(real_bank, JDS[name]), real_bank)
    m = best.measure
    tpl = render.template_info(real_bank)
    assert m.pages == 1
    assert m.min_font >= tpl["page"]["min_font_pt"] - 0.05
    assert not m.clipped
    lo, hi = tpl["page"]["target_bottom_gap_pt"]
    assert m.bottom_gap <= hi + 12, f"page looks underfilled: {m.bottom_gap}pt"
    assert lay["compiles"] <= 10


def test_overflow_is_recovered(real_bank):
    """Start from a plan that is far too long (wrong line estimate): measuring the real PDF
    must drive trimming / swapping / tightening until it fits."""
    job = analyzer.analyze(JDS["backend"])
    cands, items = retrieval.score_bank(real_bank, job)
    sel = planner.plan(real_bank, job, cands, items, budget=planner.default_budget(real_bank) + 25)
    best, lay = layout.optimize(sel, real_bank)
    assert best.measure.pages == 1 and not best.measure.margin_violation
    assert lay["compiles"] >= 2
    assert lay["history"][0]["pages"] >= 2


def test_long_heading_gives_clear_error(small_bank):
    b = copy.deepcopy(small_bank)
    b["experiences"][0]["title"] = "Senior Staff Principal Distinguished Software and Infrastructure Engineering Intern"
    b["experiences"][0]["org"] = "Extremely Long Organization Name Research and Development Laboratories International"
    with pytest.raises(layout.LayoutError, match="page edge"):
        layout.optimize(_sel(b, JDS["backend"]), b)


def test_sparse_bank_still_valid(small_bank):
    best, _ = layout.optimize(_sel(small_bank, JDS["backend"]), small_bank)
    assert best.measure.pages == 1   # sparse is allowed: the bank has nothing more to add


def test_special_characters_and_long_titles(small_bank):
    b = copy.deepcopy(small_bank)
    b["experiences"][0]["title"] = "Software & Infra Intern #1"
    b["experiences"][0]["org"] = "R&D_Labs 100% {Inc} ~ ^ $ \\ ok"
    b["experiences"][0]["bullets"][2]["text"] = "Handled 50% of C# & F# _builds_ for $5 at ~3 sites #fun \\input{x}"
    best, _ = layout.optimize(_sel(b, JDS["backend"]), b)
    assert best.measure.pages == 1
    assert "\\input{x}" not in best.source


def test_offline_pipeline_end_to_end(real_bank):
    res = tailor.run(real_bank, JDS["ml"], title="Machine Learning Intern", company="Example AI")
    assert res.pdf.startswith(b"%PDF")
    assert res.measure["pages"] == 1
    assert res.scorecard["unsupported_claims"] == 0
    assert all(p["printed"] == "bank" for p in res.provenance)
    assert res.stats["mode"] == "offline" and res.stats["llm_requests"] == 0


def test_bullet_line_measurement(real_bank):
    import linefit
    fit = linefit.bank_fit(real_bank)
    n = sum(len(i["bullets"]) for s in ("experiences", "projects") for i in real_bank[s])
    assert len(fit) == n  # every bullet measured
    assert 90 <= linefit.chars_per_line(fit) <= 140
    for f in fit.values():
        assert 1 <= f["lines"] <= 4 and 0 < f["last_fill"] <= 1.01


def test_short_last_lines_reported(real_bank):
    res = tailor.run(real_bank, JDS["fullstack"])
    printed = res.provenance
    short = [p for p in printed if p.get("short_last_line")]
    assert all("lines" in p for p in printed)  # every printed bullet was measured
    assert res.scorecard["short_last_lines"] == [p["bullet_id"] for p in short]


def test_strict_lines_never_prints_short_last_line(real_bank):
    real_bank["settings"]["strict_lines"] = True
    for role in ("fullstack", "hardware", "ml"):
        res = tailor.run(real_bank, JDS[role])
        assert res.measure["pages"] == 1
        assert res.scorecard["short_last_lines"] == [], (role, res.scorecard["short_last_lines"])
