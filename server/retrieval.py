"""Score every accomplishment (bullet) in the bank against a job analysis.

Deterministic and explainable. Each bullet gets a breakdown:
  coverage   - which job requirements it shows evidence for, and how strongly
               (named in the bullet = full credit, only in the item's stack/tags = half credit)
  keywords   - overlap with important non-skill words in the posting (domain terms)
  role       - how well the bullet's skills fit the role categories of the job
  evidence   - quantified results (numbers) and technical specificity
  recency    - current or recent work
  llm        - optional bonus from the model's ranking (semantic relevance)
"""
from __future__ import annotations

import re
from datetime import date
from dataclasses import dataclass, field

import bank as bankmod
import lexicon
from analyzer import JobAnalysis

CPL = 118          # characters per rendered bullet line (Jake's template, \small, ~7.6in text width)


@dataclass
class Candidate:
    bullet_id: str
    item_id: str
    section: str                    # experiences | projects
    text: str                       # as stored (LaTeX allowed)
    plain: str
    group: str                      # alt group: bullets sharing it are alternate wordings
    is_alt: bool
    pinned: bool
    skills: set[str]
    context_skills: set[str]
    numbers: set[str]
    words: set[str]
    lines: int
    coverage: dict[str, float] = field(default_factory=dict)   # requirement -> 0..1
    parts: dict[str, float] = field(default_factory=dict)
    score: float = 0.0

    def to_json(self) -> dict:
        return {"id": self.bullet_id, "item": self.item_id, "score": round(self.score, 2),
                "parts": {k: round(v, 2) for k, v in self.parts.items()},
                "covers": sorted(self.coverage, key=lambda r: -self.coverage[r])}


def est_lines(plain_text: str) -> int:
    return max(1, -(-len(plain_text) // CPL))


def _recency(dates: str) -> float:
    d = (dates or "").lower()
    if "present" in d or "current" in d:
        return 1.0
    years = [int(y) for y in re.findall(r"20\d\d", d)]
    if not years:
        return 0.3
    newest = max(years)
    return {0: 1.0, 1: 0.8, 2: 0.55, 3: 0.35}.get(max(0, date.today().year - newest), 0.15)


def score_bank(bank: dict, job: JobAnalysis, llm_rank: dict[str, float] | None = None) -> tuple[list[Candidate], dict]:
    """Return (candidates sorted by score, item info). Hidden items and bullets are skipped."""
    reqs = {r.skill: r.importance for r in job.requirements}
    top_imp = max(reqs.values(), default=1.0)
    llm_rank = llm_rank or {}
    leadership_job = bool(re.search(r"lead|mentor|leadership|team player|communicat", job.focus_text, re.I))
    cands: list[Candidate] = []
    items: dict[str, dict] = {}
    for section in ("experiences", "projects"):
        for pos, item in enumerate(bank.get(section, [])):
            if item.get("hidden"):
                continue
            ctx = bankmod.item_skills(item)
            kind = item.get("kind", "work" if section == "experiences" else "project")
            kind_factor = 1.0
            if kind in ("leadership", "volunteer", "other") and not leadership_job:
                kind_factor = 0.55
            if kind == "school":
                kind_factor = 0.8
            recency = _recency(item.get("dates", "")) if section == "experiences" else 0.6
            # Anchor: a current or recent internship/job belongs on almost any resume (with at least
            # its minimum bullets) unless it's truly unrelated. Applied when the item first enters.
            anchor = 0.6 if section == "experiences" and kind in ("internship", "research") and recency >= 0.8 else 0.0
            items[item["id"]] = {"section": section, "pos": pos, "kind": kind, "item": item, "anchor": anchor,
                                 "context_skills": ctx, "pinned": bool(item.get("pinned")),
                                 "overlaps": set(item.get("overlaps") or []), "kind_factor": kind_factor}
            for b in item.get("bullets", []):
                if b.get("hidden"):
                    continue
                p = bankmod.plain(b["text"])
                skills = bankmod.bullet_skills(b, item)
                c = Candidate(
                    bullet_id=b["id"], item_id=item["id"], section=section, text=b["text"], plain=p,
                    group=b.get("alt_of") or b["id"], is_alt=bool(b.get("alt_of")) or b["id"].endswith("-alt"),
                    pinned=bool(b.get("pinned")), skills=skills, context_skills=ctx - skills,
                    numbers=bankmod.numbers(b["text"]), words=set(lexicon.content_words(p)), lines=est_lines(p))
                # coverage of each requirement
                for skill, imp in reqs.items():
                    if skill in skills:
                        c.coverage[skill] = 1.0
                    elif skill in ctx:
                        c.coverage[skill] = 0.35
                cov = sum(reqs[s] * v for s, v in c.coverage.items()) / top_imp
                kw = sum(job.keywords.get(w, 0) for w in c.words) / 25.0
                roles: dict[str, float] = {}
                for s in skills | ctx:
                    for r in lexicon.roles_for(s):
                        roles[r] = roles.get(r, 0) + (1.0 if s in skills else 0.4)
                norm = max(1.0, sum(roles.values()))
                role = sum(job.roles.get(r, 0) * v for r, v in roles.items()) / norm
                evidence = (0.5 if c.numbers else 0.0) + min(0.4, 0.1 * len(skills))
                c.parts = {"coverage": cov, "keywords": min(kw, 1.0), "role": role, "evidence": evidence,
                           "recency": 0.3 * recency, "llm": llm_rank.get(b["id"], 0.0)}
                c.score = kind_factor * sum(c.parts.values()) + (10.0 if c.pinned else 0.0)
                cands.append(c)
    cands.sort(key=lambda c: -c.score)
    return cands, items
