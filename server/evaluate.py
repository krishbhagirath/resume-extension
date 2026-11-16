"""Resume quality scorecard. Deterministic metrics only; nothing here is an "ATS score".

  coverage            share of the job's weighted requirements shown on the resume
  supported_coverage  same, but only counting requirements your bank can support at all
                      (the fair measure: it doesn't punish skills you don't have)
  unsupported         requirements the bank has no evidence for (never added to the resume)
  quantified          share of bullets with a measurable result
  specificity         named technologies per bullet
  redundancy          highest word overlap between any two bullets (lower is better)
  rephrased / rejected  rewordings used vs. rejected by the fact checker
  one_page / fill     layout checks from the measured PDF
  short_last_lines    printed bullets whose last line is only a word or two (edit them in the bank)
"""
from __future__ import annotations

import itertools

import lexicon
from analyzer import JobAnalysis
from planner import Selection


def scorecard(job: JobAnalysis, sel: Selection, plan: dict, measure: dict | None, provenance: list[dict]) -> dict:
    imp = {r.skill: r.importance for r in job.requirements}
    total = sum(imp.values()) or 1.0
    bullets = [sel.cands[b] for e in plan["experiences"] + plan["projects"] for b in e["bullet_ids"]]
    on_page = set().union(*[c.skills for c in bullets]) if bullets else set()
    context = set().union(*[c.context_skills for c in bullets]) if bullets else set()
    skills_line = {lexicon.canonical(i) or i for g in plan.get("skills", []) for i in g["items"]}
    shown = on_page | context | skills_line
    supportable = set()
    for c in sel.cands.values():
        supportable |= c.skills | c.context_skills
    for g in sel.bank.get("skills", []):
        supportable |= {lexicon.canonical(i) or i for i in g["items"]}
    covered = {r for r in imp if r in shown}
    support = {r for r in imp if r in supportable}
    redundancy = 0.0
    for a, b in itertools.combinations(bullets, 2):
        if a.words and b.words:
            redundancy = max(redundancy, len(a.words & b.words) / len(a.words | b.words))
    rephrased = sum(1 for p in provenance if p.get("rephrased"))
    rejected = sum(1 for p in provenance if p.get("rejected"))
    m = measure or {}
    return {
        "coverage": round(sum(imp[r] for r in covered) / total, 3),
        "supported_coverage": round(sum(imp[r] for r in covered) / (sum(imp[r] for r in support) or 1.0), 3),
        "covered": sorted(covered, key=lambda r: -imp[r]),
        "evidenced_in_bullets": sorted({r for r in imp if r in on_page}, key=lambda r: -imp[r]),
        "unsupported": sorted(set(imp) - support, key=lambda r: -imp[r])[:12],
        "bullets": len(bullets),
        "quantified": round(sum(1 for c in bullets if c.numbers) / max(1, len(bullets)), 2),
        "specificity": round(sum(len(c.skills) for c in bullets) / max(1, len(bullets)), 2),
        "redundancy": round(redundancy, 2),
        "rephrased": rephrased,
        "rephrase_rejected": rejected,
        # printed bullets that are neither your exact bank wording nor a fact-checked rewording
        "unsupported_claims": sum(1 for p in provenance if p.get("printed") not in ("bank", "rephrased", "edited")),
        "short_last_lines": [p["bullet_id"] for p in provenance if p.get("short_last_line")],
        "one_page": m.get("pages") == 1,
        "bottom_gap_pt": m.get("bottom_gap"),
    }


def selection_overlap(a: dict, b: dict) -> float:
    """Jaccard overlap of two plans' bullet sets (used to catch near-identical resumes)."""
    sa = {x for e in a["experiences"] + a["projects"] for x in e["bullet_ids"]}
    sb = {x for e in b["experiences"] + b["projects"] for x in e["bullet_ids"]}
    return len(sa & sb) / max(1, len(sa | sb))
