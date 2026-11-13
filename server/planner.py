"""Choose which experiences, projects and bullets go on the page.

Budgeted greedy coverage (a standard approach for monotone submodular objectives):
  value(S) = sum over requirements r of  importance_r * (1 - prod_{b in S} (1 - 0.7 * cov_{b,r}))
             + quality of each bullet (role fit, keywords, evidence, recency, model rank)
             - redundancy (near-duplicate wording)
  cost(b)  = estimated rendered lines (+ the item's header when it's the item's first bullet)
At each step the unit with the best value gain per line is added, until the line budget is
used. A new item enters with its top `min_bullets` together, so items never show one lonely
bullet. Hard rules: alternate wordings are exclusive, overlapping items are exclusive,
section and per-item limits hold, pinned things go in first.

The layout optimizer then calls trim_once / add_once / swap_shorter with real PDF feedback.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import bank as bankmod
import lexicon
from analyzer import JobAnalysis
from retrieval import Candidate

HEADER_LINES = {"experiences": 2.0, "projects": 1.2}
DIMINISH = 0.7
# How much each non-coverage signal counts next to requirement coverage (coverage weight = 1).
SHORT_LINE_FACTOR = 0.85
# semantic (ENGINE=local) stands in for Claude's ranking, so its best bullet gets the same pull as
# Claude's top pick: llm parts top out at tailor.RANK_BONUS (1.5) x weight 1.0; semantic is 0..1.
QUALITY_WEIGHTS = {"role": 0.5, "keywords": 0.4, "evidence": 0.25, "recency": 0.15, "llm": 1.0, "semantic": 1.5}


def quality(c: Candidate) -> float:
    p = c.parts
    relevance = (p.get("coverage", 0) + p.get("role", 0) + p.get("keywords", 0) + p.get("llm", 0)
                 + p.get("semantic", 0))
    # Evidence and recency only count in proportion to relevance: a strong number on an
    # unrelated bullet shouldn't beat a relevant bullet.
    gate = min(1.0, relevance / 0.6)
    q = (QUALITY_WEIGHTS["role"] * p.get("role", 0) + QUALITY_WEIGHTS["keywords"] * p.get("keywords", 0)
         + QUALITY_WEIGHTS["llm"] * p.get("llm", 0) + QUALITY_WEIGHTS["semantic"] * p.get("semantic", 0)
         + gate * (QUALITY_WEIGHTS["evidence"] * p.get("evidence", 0) + QUALITY_WEIGHTS["recency"] * p.get("recency", 0)))
    if p.get("short_last_line"):
        q *= SHORT_LINE_FACTOR  # a word or two dangling on its own line: prefer a cleaner bullet
    if c.bullet_id.endswith("-alt") and not p.get("llm") and not p.get("promoted"):
        q *= 0.75  # older wordings you set aside: only preferred when the model picks them
    return q


def default_budget(bank: dict) -> float:
    """Lines available for experience + project content on one page (calibrated on Jake's template)."""
    edu_lines = sum(2 + len(e.get("bullets") or []) for e in bank.get("education", []))
    skills_lines = len(bank.get("skills", [])) + 1
    return 54.0 - edu_lines - skills_lines - 4.0   # 4 = section headings + header block slack


@dataclass
class Selection:
    bank: dict
    job: JobAnalysis
    cands: dict[str, Candidate]
    items: dict[str, dict]
    budget: float
    chosen: dict[str, list[str]] = field(default_factory=dict)   # item id -> bullet ids (in add order)
    log: list[str] = field(default_factory=list)

    # ------------------------------------------------------------ settings
    @property
    def s(self) -> dict:
        return self.bank.get("settings", {})

    def limit(self, section: str) -> int:
        return self.s.get("max_experiences", 3) if section == "experiences" else self.s.get("max_projects", 3)

    @property
    def min_b(self) -> int:
        return self.s.get("min_bullets_per_item", 2)

    @property
    def max_b(self) -> int:
        return self.s.get("max_bullets_per_item", 4)

    # ------------------------------------------------------------ value
    def bullets(self) -> list[Candidate]:
        return [self.cands[b] for ids in self.chosen.values() for b in ids]

    def coverage_state(self, exclude: str | None = None) -> dict[str, float]:
        miss: dict[str, float] = {}
        for c in self.bullets():
            if c.bullet_id == exclude:
                continue
            for r, v in c.coverage.items():
                miss[r] = miss.get(r, 1.0) * (1 - DIMINISH * v)
        return miss

    def gain(self, c: Candidate, miss: dict[str, float] | None = None) -> float:
        miss = self.coverage_state() if miss is None else miss
        imp = {r.skill: r.importance for r in self.job.requirements}
        top = max(imp.values(), default=1.0)
        cov = sum(imp[r] * miss.get(r, 1.0) * DIMINISH * v for r, v in c.coverage.items()) / top
        q = quality(c)
        redundancy = 0.0
        for other in self.bullets():
            if other.bullet_id == c.bullet_id or not c.words or not other.words:
                continue
            j = len(c.words & other.words) / len(c.words | other.words)
            if j > 0.45:
                redundancy = max(redundancy, j)
        value = (cov + q) * self.items[c.item_id]["kind_factor"] * (1 - redundancy)
        return value + (10.0 if c.pinned else 0.0)

    def cost(self, c: Candidate, new_item: bool) -> float:
        return c.lines + (HEADER_LINES[c.section] if new_item else 0.0)

    def used(self) -> float:
        total = 0.0
        for item_id, ids in self.chosen.items():
            if ids:
                total += HEADER_LINES[self.items[item_id]["section"]] + sum(self.cands[b].lines for b in ids)
        return total

    def total_value(self) -> float:
        imp = {r.skill: r.importance for r in self.job.requirements}
        top = max(imp.values(), default=1.0)
        miss = self.coverage_state()
        cov = sum(imp[r] * (1 - m) for r, m in miss.items()) / top
        qual = sum(quality(c) for c in self.bullets())
        return cov + qual

    # ------------------------------------------------------------ feasibility
    def _section_count(self, section: str) -> int:
        return sum(1 for i, ids in self.chosen.items() if ids and self.items[i]["section"] == section)

    def _blocked_item(self, item_id: str) -> bool:
        info = self.items[item_id]
        for other, ids in self.chosen.items():
            if not ids or other == item_id:
                continue
            if other in info["overlaps"] or item_id in self.items[other]["overlaps"]:
                return True
        return False

    def _groups(self, item_id: str) -> set[str]:
        return {self.cands[b].group for b in self.chosen.get(item_id, [])}

    def options(self) -> list[tuple[float, float, str, list[str]]]:
        """Feasible units: (gain per line, gain, item id, bullet ids)."""
        miss = self.coverage_state()
        by_item: dict[str, list[Candidate]] = {}
        for c in self.cands.values():
            by_item.setdefault(c.item_id, []).append(c)
        out = []
        for item_id, cs in by_item.items():
            section = self.items[item_id]["section"]
            ids = self.chosen.get(item_id, [])
            groups = self._groups(item_id)
            free = [c for c in cs if c.bullet_id not in ids and c.group not in groups]
            if not free:
                continue
            if ids:  # item already on the page: add one bullet
                if len(ids) >= self.max_b:
                    continue
                for c in free:
                    g = self.gain(c, miss)
                    out.append((g / self.cost(c, False), g, item_id, [c.bullet_id]))
            else:   # new item: its best min_b bullets as a bundle
                if self._section_count(section) >= self.limit(section) or self._blocked_item(item_id):
                    continue
                ranked = sorted(free, key=lambda c: -self.gain(c, miss))
                bundle, seen = [], set()
                for c in ranked:
                    if c.group in seen:
                        continue
                    bundle.append(c)
                    seen.add(c.group)
                    if len(bundle) >= self.min_b:
                        break
                g = sum(self.gain(c, miss) for c in bundle) + self.items[item_id].get("anchor", 0.0)
                g *= 1.15 if self._section_count(section) < (2 if section == "experiences" else 1) else 1.0
                cost = sum(c.lines for c in bundle) + HEADER_LINES[section]
                out.append((g / cost, g, item_id, [c.bullet_id for c in bundle]))
        out.sort(key=lambda o: -o[0])
        return out

    # ------------------------------------------------------------ operations
    def add_once(self, slack: float = 0.0) -> bool:
        """Add the best feasible unit that fits the budget (+slack lines). False if none."""
        room = self.budget + slack - self.used()
        for ratio, g, item_id, ids in self.options():
            if g <= 0.05:
                break
            cost = sum(self.cands[b].lines for b in ids) + (0 if self.chosen.get(item_id) else
                                                              HEADER_LINES[self.items[item_id]["section"]])
            if cost <= room + 1e-9:
                self.chosen.setdefault(item_id, []).extend(ids)
                self.log.append(f"add {','.join(ids)} (+{g:.2f})")
                return True
        return False

    def removal_options(self) -> list[tuple[float, str, str | None]]:
        """(value lost per line, item id, bullet id or None for the whole item); cheapest loss first."""
        out = []
        for item_id, ids in self.chosen.items():
            if not ids:
                continue
            info = self.items[item_id]
            if len(ids) > self.min_b:
                for b in ids:
                    c = self.cands[b]
                    if c.pinned:
                        continue
                    miss = self.coverage_state(exclude=b)
                    loss = self.gain(c, miss)
                    # keep items at 3 bullets before going lower: discount losses above 3
                    if len(ids) > 3:
                        loss *= 0.6
                    out.append((loss / c.lines, item_id, b))
            elif not info["pinned"] and self._section_count(info["section"]) > (1 if info["section"] == "projects" else 2):
                loss = sum(self.gain(self.cands[b], self.coverage_state(exclude=b)) for b in ids)
                lines = sum(self.cands[b].lines for b in ids) + HEADER_LINES[info["section"]]
                out.append((loss / lines * 1.3, item_id, None))
        out.sort(key=lambda o: o[0])
        return out

    def trim_once(self) -> bool:
        opts = self.removal_options()
        if not opts:
            # last resort: allow dropping below the soft section minimums
            for item_id in reversed(list(self.chosen)):
                if self.chosen[item_id] and not self.items[item_id]["pinned"] and len(
                        [i for i in self.chosen if self.chosen[i]]) > 1:
                    self.log.append(f"drop item {item_id} (last resort)")
                    self.chosen[item_id] = []
                    return True
            return False
        _, item_id, b = opts[0]
        if b:
            self.chosen[item_id].remove(b)
            self.log.append(f"trim {b}")
        else:
            self.log.append(f"drop item {item_id}")
            self.chosen[item_id] = []
        return True

    def swap_shorter(self) -> bool:
        """Swap a picked bullet for a shorter wording of the same achievement, if one exists."""
        best = None
        for item_id, ids in self.chosen.items():
            for b in ids:
                c = self.cands[b]
                for alt in self.cands.values():
                    if alt.group == c.group and alt.bullet_id != b and alt.lines < c.lines:
                        loss = self.gain(c) - self.gain(alt)
                        if best is None or loss < best[0]:
                            best = (loss, item_id, b, alt.bullet_id)
        if not best or best[0] > 0.6:
            return False
        _, item_id, old, new = best
        ids = self.chosen[item_id]
        ids[ids.index(old)] = new
        self.log.append(f"swap {old} -> shorter {new}")
        return True

    # ------------------------------------------------------------ output
    def ordered(self) -> dict:
        """Selection in render order: dict with experiences/projects/skills (+ explain)."""
        keep = self.s.get("keep_bank_order", True)
        out = {}
        for section in ("experiences", "projects"):
            entries = []
            for item_id, ids in self.chosen.items():
                if not ids or self.items[item_id]["section"] != section:
                    continue
                # bullets: pinned first, then by value
                miss = self.coverage_state()
                ordered_ids = sorted(ids, key=lambda b: (-int(self.cands[b].pinned), -self.cands[b].score))
                value = sum(self.cands[b].score for b in ids)
                entries.append((self.items[item_id]["pos"], -value, {"id": item_id, "bullet_ids": ordered_ids}))
            if keep is True or (keep == "experiences" and section == "experiences"):
                entries.sort(key=lambda e: e[0])
            else:
                entries.sort(key=lambda e: e[1])
            out[section] = [e[2] for e in entries]
        out["skills"] = pick_skills(self.bank, self.job, self.bullets())
        return out


def pick_skills(bank: dict, job: JobAnalysis, picked: list[Candidate]) -> list[dict]:
    """Per category: skills the job asks for first, then ones shown in the picked bullets, then the
    rest, until the line is full. Only skills already in the bank are ever listed."""
    imp = {r.skill: r.importance for r in job.requirements}
    shown = set().union(*[c.skills | c.context_skills for c in picked]) if picked else set()
    max_chars = bank.get("settings", {}).get("max_skill_line_chars", 100)
    out = []
    for group in bank.get("skills", []):
        scored = []
        for pos, name in enumerate(group["items"]):
            canon = lexicon.canonical(name) or name
            score = imp.get(canon, 0.0) * 2 + (1.0 if canon in shown else 0.0)
            scored.append((-score, pos, name))
        scored.sort()
        items, width = [], len(group["category"]) + 2
        for _, _, name in scored:
            if width + len(name) + 2 > max_chars:
                continue
            items.append(name)
            width += len(name) + 2
        if items:
            out.append({"category": group["category"], "items": items})
    return out


def plan(bank: dict, job: JobAnalysis, cands: list[Candidate], items: dict, budget: float | None = None) -> Selection:
    sel = Selection(bank=bank, job=job, cands={c.bullet_id: c for c in cands}, items=items,
                    budget=budget if budget is not None else default_budget(bank))
    # pinned items first (with their pinned bullets + best others up to the minimum)
    for item_id, info in items.items():
        if info["pinned"] and not sel._blocked_item(item_id):
            mine = sorted([c for c in cands if c.item_id == item_id], key=lambda c: (-int(c.pinned), -c.score))
            ids, groups = [], set()
            for c in mine:
                if c.group in groups:
                    continue
                if len(ids) >= sel.min_b and not c.pinned:
                    break
                ids.append(c.bullet_id)
                groups.add(c.group)
            sel.chosen[item_id] = ids
            sel.log.append(f"pinned item {item_id}")
    for c in cands:  # pinned bullets in unpinned items pull their item in
        if c.pinned and not sel.chosen.get(c.item_id) and not sel._blocked_item(c.item_id):
            sel.chosen[c.item_id] = [c.bullet_id]
            sel.log.append(f"pinned bullet {c.bullet_id}")
    while sel.add_once():
        pass
    return sel
