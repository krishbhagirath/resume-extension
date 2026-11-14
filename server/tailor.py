"""The tailoring pipeline (no I/O besides the model and the compiler):

  posting text -> JobAnalysis (deterministic) -> scored accomplishments (deterministic)
  -> optional model re-rank of a small candidate pool (ids only) -> budgeted selection
  -> closed-loop one-page layout -> optional rewording + fact check -> scorecard

Every printed bullet has provenance: the bank bullet id it came from, and whether the
printed words are your bank wording or a fact-checked rewording.
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass, field

import analyzer
import bank as bankmod
import evaluate
import factcheck
import lexicon
import layout
import linefit
import llm
import planner
import retrieval

POOL_SIZE = 40          # candidates the model sees (not the whole bank)
RANK_BONUS = 1.5        # how much the model's ranking can add to a bullet's value


class ExtractionError(RuntimeError):
    def __init__(self, msg: str, quality: dict):
        super().__init__(msg)
        self.quality = quality


@dataclass
class Result:
    plan: dict
    pdf: bytes
    tex: str
    company: str
    role: str
    analysis: dict
    measure: dict
    layout: dict
    scorecard: dict
    provenance: list[dict]
    stats: dict
    warnings: list[str] = field(default_factory=list)
    bullet_scores: list[dict] = field(default_factory=list)   # printed bullets: score and its parts


RANK_SCHEMA = {
    "type": "object",
    "properties": {
        "company": {"type": "string", "description": "Hiring company from the posting, or empty."},
        "role": {"type": "string", "description": "Exact job title from the posting, or empty."},
        "ranked_ids": {"type": "array", "items": {"type": "string"},
                       "description": "Bullet ids, most convincing for this job first. Up to 24."},
        "note": {"type": "string", "description": "One sentence on what the job needs most."},
    },
    "required": ["company", "role", "ranked_ids", "note"],
}

REPHRASE_SCHEMA = {
    "type": "object",
    "properties": {"bullets": {"type": "array", "items": {
        "type": "object", "properties": {"id": {"type": "string"}, "text": {"type": "string"}},
        "required": ["id", "text"]}}},
    "required": ["bullets"],
}


def _rank_prompt(job: analyzer.JobAnalysis, pool: list[retrieval.Candidate], items: dict) -> str:
    reqs = "; ".join(f"{r.skill} ({'required' if r.section == 'required' or r.mandatory else r.section}, {r.importance:.1f})"
                     for r in job.requirements[:15])
    lines = []
    for c in pool:
        it = items[c.item_id]["item"]
        lines.append(f"{c.bullet_id} [{it.get('title')}, {it.get('org') or it.get('stack', '')}]: {c.plain}")
    return f"""Rank an applicant's verified resume bullets for one job.

Job title: {job.title or 'unknown'}   Company: {job.company or 'unknown'}
Role type: {', '.join(job.primary_roles)}
Top requirements (importance): {reqs}

<job_posting>
{job.focus_text[:3500]}
</job_posting>

Candidate bullets (id [title, org]: text):
{chr(10).join(lines)}

Rank the bullets that best prove this applicant fits this job, most convincing first (up to 24).
Prefer direct evidence of required skills, then strong measured results in closely related work.
Use only ids from the list. Also return the hiring company and exact job title from the posting."""


def _rephrase_prompt(job: analyzer.JobAnalysis, targets: list[dict]) -> str:
    body = []
    for t in targets:
        lim = f"at most {t['one_line']} characters (one line)"
        if t.get("two_lines"):
            lim += f", OR {t['two_lines'][0]}-{t['two_lines'][1]} characters (two nearly full lines)"
        why = "; ".join(t["why"])
        body.append(f'- id: {t["id"]}\n  source: "{t["text"]}"\n  length: {lim}\n  goal: {why}')
    terms = ", ".join(r.skill for r in job.requirements[:12])
    return f"""Lightly edit a few resume bullets. Change as little as possible.

Every bullet must stay in XYZ form: "Accomplished [X] as measured by [Y], by doing [Z]":
- Start with the same kind of strong past-tense verb.
- Keep the measurable result: every number exactly as written, with its unit. Add no new numbers.
- Keep how it was done (the "by / using / via ..." part).

Hard rules:
- Use ONLY facts in the source. Do not add technologies, employers, team sizes, leadership, users,
  awards, production or deployment claims. Only name a technology listed in that bullet's goal.
- Respect the length exactly. A bullet that spills a word or two onto a new line is not allowed.
- Plain text only. You may mark 1-2 key terms with **double asterisks**.
- If you can't meet every rule, return the source unchanged.

Job: {job.title or ''} at {job.company or ''}. Important terms: {terms}

<job_posting>
{job.focus_text[:1500]}
</job_posting>

Bullets:
{chr(10).join(body)}"""


def run(bank: dict, jd: str, *, title: str = "", company: str = "", force: bool = False,
        rephrase: bool | None = None, job_id: str | None = None, progress=lambda s, m: None,
        cancelled=lambda: False, use_llm: bool = True, include=(), exclude=()) -> Result:
    """use_llm=False skips the Claude re-rank (the "matcher" variant of an A/B comparison).
    include / exclude are manual swaps: bullet ids forced onto the page / kept off it."""
    t0 = time.time()
    include, exclude = set(include or ()), set(exclude or ())
    stats = {"llm_requests": 0, "llm_cached": 0, "input_tokens": 0, "output_tokens": 0, "timings": {}}
    warnings: list[str] = []

    def tick(name: str, t: float):
        stats["timings"][name] = round(time.time() - t, 2)

    def check_cancel():
        if cancelled():
            raise layout.LayoutError("Cancelled.")

    # 1. analysis
    progress("analyzing", "Analyzing the job description…")
    t = time.time()
    job = analyzer.analyze(jd, title=title, company=company)
    tick("analyze", t)
    if job.quality["confidence"] < 0.45 and not force:
        raise ExtractionError("This doesn't look like a complete job description. "
                              + " ".join(job.quality["issues"][:2]), job.quality)

    # 2. deterministic scoring
    check_cancel()
    t = time.time()
    cands, items = retrieval.score_bank(bank, job)
    progress("matching", f"Matching {len(cands)} accomplishments…")
    tick("retrieve", t)

    # 3. re-rank: on-device semantic match (ENGINE=local) or Claude on a small pool (ids only)
    mode = "offline"
    if use_llm and llm.engine() == "local":
        check_cancel()
        progress("matching", "Matching meaning on this computer…")
        t = time.time()
        try:
            import semantic  # needs fastembed + numpy, so only loaded for ENGINE=local
            sem, sem_timing = semantic.score(bank, job)
            cands, items = retrieval.score_bank(bank, job, semantic=sem)
            stats["semantic"] = sem_timing
            mode = "local"
        except (ImportError, RuntimeError) as e:   # SemanticError is a RuntimeError
            warnings.append(f"Local matching wasn't used ({str(e).rstrip('.')}). "
                            "Picked with the built-in matcher instead.")
        tick("semantic", t)
    elif use_llm and llm.available():
        check_cancel()
        progress("matching", "Ranking your best evidence with Claude…")
        t = time.time()
        pool = cands[:POOL_SIZE]
        try:
            data, st = llm.complete_json(_rank_prompt(job, pool, items), RANK_SCHEMA, job_id=job_id)
            _add_stats(stats, st)
            valid = {c.bullet_id for c in pool}
            ranked = [i for i in dict.fromkeys(data.get("ranked_ids", [])) if i in valid][:24]
            n = max(1, len(ranked))
            llm_rank = {bid: RANK_BONUS * (1 - i / n) for i, bid in enumerate(ranked)}
            cands, items = retrieval.score_bank(bank, job, llm_rank)
            job.company = job.company or (data.get("company") or "").strip()[:80]
            job.title = job.title or (data.get("role") or "").strip()[:120]
            mode = llm.engine()
        except llm.LLMError as e:
            if e.code == "cancelled":
                raise
            warnings.append(f"Claude wasn't used ({e}). Picked with the built-in matcher instead.")
        tick("rank", t)

    # 3a. manual swaps: excluded bullets never print, included ones are pinned
    if include or exclude:
        cands = [c for c in cands if c.bullet_id not in exclude]
        for c in cands:
            if c.bullet_id in include and not c.pinned:
                c.pinned, c.score = True, c.score + 10.0
        cands.sort(key=lambda c: -c.score)

    # 3b. real line counts for every bullet in this template (cached per bank version)
    fit = {}
    try:
        t = time.time()
        fit = linefit.bank_fit(bank)
        tick("line_fit", t)
    except Exception as e:  # never block a resume on this
        warnings.append(f"Couldn't measure bullet line lengths ({str(e)[:80]}). Used estimates.")
    min_fill = bank.get("settings", {}).get("min_last_line_fill", 0.6)
    for c in cands:
        f = fit.get(c.bullet_id)
        if f:
            c.lines = f["lines"]
            if layout.short_last_line(f, min_fill):
                c.parts["short_last_line"] = 1.0
    if bank.get("settings", {}).get("strict_lines") and fit:
        # never print a bullet whose last line is a word or two; a clean wording of it takes its place
        dropped = {c.group for c in cands if c.parts.get("short_last_line") and not c.pinned and c.bullet_id == c.group}
        cands = [c for c in cands if not (c.parts.get("short_last_line") and not c.pinned)]
        for c in cands:
            if c.group in dropped:
                c.parts["promoted"] = 1.0
        stats["strict_excluded"] = len(dropped)

    # 4. selection under a page budget
    check_cancel()
    progress("selecting", "Choosing the strongest content for one page…")
    t = time.time()
    sel = planner.plan(bank, job, cands, items)
    tick("select", t)

    # 5. layout loop (your exact wording)
    progress("rendering", "Building and checking the PDF…")
    t = time.time()
    best, lay = layout.optimize(sel, bank, progress=lambda m: progress("optimizing", m), cancelled=cancelled)
    tick("layout", t)
    texts: dict[str, str] = {}

    # 6. optional rewording of a few bullets, fact-checked and line-checked, then re-fit
    settings = bank.get("settings", {})
    do_rephrase = settings.get("rephrase", False) if rephrase is None else rephrase
    rejected: dict = {}
    if do_rephrase and llm.available() and settings.get("max_rephrase", 3) > 0:
        check_cancel()
        t = time.time()
        targets = _rephrase_targets(bank, job, best, sel, fit, settings)
        stats["rephrase_targets"] = len(targets)
        if targets:
            progress("writing", f"Tightening {len(targets)} bullet{'s' if len(targets) > 1 else ''}…")
            try:
                texts, rejected = _rephrase(bank, job, targets, stats, job_id)
                for _round in range(2):
                    if not texts:
                        break
                    best2, lay2 = layout.optimize(sel, bank, texts=texts, progress=lambda m: progress("optimizing", m),
                                                  cancelled=cancelled)
                    # check each rewording in the real PDF: no word or two dangling on a new line
                    printed = linefit.printed_texts(bank, set(texts), texts)
                    measured = layout.bullet_fit(best2.pdf, printed)
                    bad = {b: m for b, m in measured.items() if layout.short_last_line(m, min_fill)}
                    if not bad:
                        best, lay = best2, {**lay2, "compiles": lay["compiles"] + lay2["compiles"]}
                        break
                    for b, m in bad.items():
                        rejected[b] = {"text": texts.pop(b), "reasons": [
                            f"printed with a short last line ({int(m['last_fill'] * 100)}% full)"]}
                    lay = {**lay, "compiles": lay["compiles"] + lay2["compiles"]}
                else:
                    texts = {}
            except llm.LLMError as e:
                if e.code == "cancelled":
                    raise
                warnings.append(f"Rewording skipped ({e}). Used your exact wording.")
                texts = {}
            except layout.LayoutError:
                warnings.append("Reworded bullets didn't fit one page. Used your exact wording.")
                texts = {}
        tick("rephrase", t)
    provenance = _provenance(bank, best.plan, texts, rejected)

    # how every printed bullet actually laid out
    picked = [b for e in best.plan["experiences"] + best.plan["projects"] for b in e["bullet_ids"]]
    final_fit = {}
    try:
        final_fit = layout.bullet_fit(best.pdf, linefit.printed_texts(bank, set(picked), texts))
    except Exception:
        pass
    for p in provenance:
        f = final_fit.get(p["bullet_id"])
        if f:
            p.update(lines=f["lines"], last_fill=f["last_fill"], short_last_line=layout.short_last_line(f, min_fill))

    card = evaluate.scorecard(job, sel, best.plan, best.measure.to_json(), provenance)
    missing = sorted(include - set(picked))
    if missing:
        warnings.append(f"Couldn't fit {', '.join(missing)} on the page.")
    # score includes +10 for pinned bullets (bank pins and manual swaps); parts never do
    scores = [sel.cands[b].to_json() | {"lines": sel.cands[b].lines, "pinned": sel.cands[b].pinned}
              for b in picked if b in sel.cands]
    stats["seconds"] = round(time.time() - t0, 1)
    stats["compiles"] = lay["compiles"]
    stats["mode"] = mode
    stats["weights"] = planner.active_weights()[2]   # "learned" when data/learned_weights.json is in use
    stats["pool_size"] = min(POOL_SIZE, len(cands)) if mode not in ("offline", "local") else 0
    return Result(plan=best.plan, pdf=best.pdf, tex=best.source, company=job.company, role=job.title,
                  analysis={"roles": job.roles, "primary_roles": job.primary_roles, "seniority": job.seniority,
                            "requirements": [r.model_dump() for r in job.requirements[:20]],
                            "quality": job.quality, "text_hash": job.text_hash},
                  measure=best.measure.to_json(), layout={**lay, "history": lay["history"][-12:]},
                  scorecard=card, provenance=provenance, stats=stats, warnings=warnings, bullet_scores=scores)


def _add_stats(stats: dict, st: dict):
    stats["llm_requests"] += st.get("requests", 0)
    stats["llm_cached"] += 1 if st.get("cached") else 0
    stats["input_tokens"] += st.get("input_tokens", 0) or 0
    stats["output_tokens"] += st.get("output_tokens", 0) or 0
    stats["est_cost_usd"] = round(stats.get("est_cost_usd", 0) + (st.get("est_cost_usd") or 0 if not st.get("cached") else 0), 5)
    stats["llm_seconds"] = round(stats.get("llm_seconds", 0) + (st.get("seconds") or 0 if not st.get("cached") else 0), 1)


def _lookup(bank: dict) -> dict:
    out = {}
    for section in ("experiences", "projects"):
        for item in bank.get(section, []):
            for b in item.get("bullets", []):
                out[b["id"]] = (b, item)
    return out


def _rephrase_targets(bank: dict, job: analyzer.JobAnalysis, best, sel, fit: dict, settings: dict) -> list[dict]:
    """Only bullets with a reason to change, at most settings.max_rephrase:
      * it prints with a short last line (tighten to one line, or fill two)
      * the job asks for a skill this bullet is tagged with but doesn't name"""
    lk = _lookup(bank)
    cpl = linefit.chars_per_line(fit) if fit else retrieval.CPL
    min_fill = settings.get("min_last_line_fill", 0.6)
    imp = {r.skill: r.importance for r in job.requirements}
    out = []
    for bid in [b for e in best.plan["experiences"] + best.plan["projects"] for b in e["bullet_ids"]]:
        b, _item = lk[bid]
        text = bankmod.plain(b["text"])
        f = fit.get(bid) or {"lines": retrieval.est_lines(text), "last_fill": 1.0}
        c = sel.cands.get(bid)
        why, priority = [], 0.0
        if layout.short_last_line(f, min_fill):
            why.append(f"its last line is only {int(f['last_fill'] * 100)}% full: fit it on one line, or fill two lines")
            priority += 3
        tagged = {lexicon.canonical(x) or x for x in b.get("skills") or []}
        named = set(lexicon.find_skills(text))
        missing = [s for s in sorted(tagged - named, key=lambda s: -imp.get(s, 0)) if imp.get(s, 0) >= 1.5]
        if missing:
            why.append(f"may name {', '.join(missing[:2])} (this bullet is tagged with it)")
            priority += sum(imp[s] for s in missing[:2])
        if not why or (c and c.pinned and not missing):
            continue
        one = int(cpl * 0.95)
        two = [int(cpl * (1 + min_fill + 0.05)), int(cpl * 1.93)] if f["lines"] >= 2 or len(text) > one else None
        out.append({"id": bid, "text": text, "one_line": one, "two_lines": two, "why": why, "priority": priority})
    out.sort(key=lambda t: -t["priority"])
    return out[: settings.get("max_rephrase", 3)]


def _rephrase(bank: dict, job: analyzer.JobAnalysis, targets: list[dict], stats: dict, job_id: str | None):
    lk = _lookup(bank)
    data, st = llm.complete_json(_rephrase_prompt(job, targets), REPHRASE_SCHEMA, job_id=job_id)
    _add_stats(stats, st)
    limits = {t["id"]: {"one_line": t["one_line"], "two_lines": t["two_lines"]} for t in targets}
    accepted, rejected = {}, {}
    for row in data.get("bullets", []):
        bid, text = row.get("id"), (row.get("text") or "").strip()
        if bid not in limits or not text:
            continue
        b, item = lk[bid]
        if re.sub(r"\*\*", "", text) == bankmod.plain(b["text"]):
            continue
        ok, reasons = factcheck.check(text, b, item, limits[bid])
        if ok:
            accepted[bid] = text
        else:
            rejected[bid] = {"text": text, "reasons": reasons}
    return accepted, rejected


def _provenance(bank: dict, plan: dict, texts: dict, rejected: dict) -> list[dict]:
    lk = _lookup(bank)
    out = []
    for section in ("experiences", "projects"):
        for e in plan[section]:
            for bid in e["bullet_ids"]:
                b, item = lk[bid]
                row = {"bullet_id": bid, "item_id": item["id"], "source": bankmod.plain(b["text"]),
                       "printed": "rephrased" if bid in texts else "bank"}
                if bid in texts:
                    row.update(rephrased=True, text=texts[bid])
                if bid in rejected:
                    row.update(rejected=True, rejected_text=rejected[bid]["text"], reasons=rejected[bid]["reasons"])
                out.append(row)
    return out
