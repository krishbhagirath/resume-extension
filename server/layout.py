"""Closed-loop one-page layout: render, measure the real PDF, adjust, repeat (bounded).

Measurement (pdfplumber, deterministic): page count, page size, text bounds, clipping,
margin violations, smallest font, bottom whitespace, line height, selectable text.

Fit objective (lower is better):
  10000 * extra pages + 2000 * clipped + 500 * margin violations + 300 * tiny fonts
  + whitespace penalty above the target gap + spacing drift from the template defaults
  - content value (from the planner)

Adjustments, in order of preference:
  overflow:   shorter wording of the same bullet -> drop least valuable content -> tighten spacing
  underfill:  add the next most valuable content -> relax spacing back toward the defaults
A result is only accepted if it is exactly one page with no clipping or margin violations.
"""
from __future__ import annotations

import io
import re
import statistics
import time
from dataclasses import dataclass, field

import pdfplumber

import render
from planner import Selection


@dataclass
class Measure:
    pages: int
    width: float
    height: float
    text_top: float
    text_bottom: float
    bottom_gap: float
    min_font: float
    clipped: bool
    margin_violation: bool
    line_height: float
    overflow_lines: float
    chars: int
    issues: list[str] = field(default_factory=list)
    edge_text: str = ""   # the line that runs into the page edge, if any

    def ok(self, page: dict) -> bool:
        return (self.pages == 1 and not self.clipped and not self.margin_violation
                and abs(self.width - page["width_pt"]) < 2 and abs(self.height - page["height_pt"]) < 2
                and self.min_font >= page["min_font_pt"] - 0.01 and self.chars > 300)

    def to_json(self) -> dict:
        return {k: (round(v, 1) if isinstance(v, float) else v) for k, v in self.__dict__.items()}


def measure(pdf_bytes: bytes) -> Measure:
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        first = pdf.pages[0]
        w, h = float(first.width), float(first.height)
        chars = [c for c in first.chars if c["text"].strip()]
        tops = sorted({round(c["top"], 0) for c in chars})
        gaps = [b - a for a, b in zip(tops, tops[1:]) if 8 <= b - a <= 16]
        line_h = statistics.median(gaps) if gaps else 11.5
        clipped = any(c["x0"] < 0 or c["x1"] > w + 0.5 or c["top"] < 0 or c["bottom"] > h + 0.5 for c in chars)
        margin = 0.25 * 72
        bad = [c for c in chars if c["x1"] > w - margin or c["x0"] < margin]
        violation = bool(bad)
        edge_text = ""
        if bad:
            row = sorted((c for c in chars if abs(c["top"] - bad[0]["top"]) < 2), key=lambda c: c["x0"])
            edge_text = "".join(c["text"] for c in row)[:90]
        # body text only: bullet glyphs and icons are meant to be small
        min_font = min((c["size"] for c in chars if c["text"].isalnum()), default=0.0)
        bottom = max((c["bottom"] for c in chars), default=0.0)
        top = min((c["top"] for c in chars), default=0.0)
        overflow = 0.0
        for extra in pdf.pages[1:]:
            ec = [c for c in extra.chars if c["text"].strip()]
            if ec:
                overflow += (max(c["bottom"] for c in ec) - min(c["top"] for c in ec)) / line_h + 1
        n_chars = sum(len(p.chars) for p in pdf.pages)
        issues = []
        if len(pdf.pages) > 1:
            issues.append(f"{len(pdf.pages)} pages")
        if clipped:
            issues.append("text outside the page")
        if violation:
            issues.append("text inside the 0.25in edge")
        return Measure(pages=len(pdf.pages), width=w, height=h, text_top=top, text_bottom=bottom,
                       bottom_gap=h - bottom, min_font=min_font, clipped=clipped, margin_violation=violation,
                       line_height=line_h, overflow_lines=overflow, chars=n_chars, issues=issues,
                       edge_text=edge_text)


def _norm(t: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (t or "").lower())


def bullet_lines(pdf_bytes: bytes, all_pages: bool = False) -> list[dict]:
    """Each printed bullet on page 1: its text, how many lines it takes, and how full its last line is
    (0..1 of the text width). A last line that is only a word or two looks unfinished."""
    out: list[dict] = []
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        for page in pdf.pages[: None if all_pages else 1]:
            out += _page_bullets(page)
    return out


def _page_bullets(page) -> list[dict]:
    if True:
        chars = [c for c in page.chars if c["text"].strip()]
        glyphs = [(c["top"] + c["bottom"]) / 2 for c in chars if c["text"] in "•∙·" and c["size"] < 7.5]
        body = [c for c in chars if not (c["text"] in "•∙·" and c["size"] < 7.5)]
        rows: list[list] = []
        for c in sorted(body, key=lambda c: (c["top"] + c["bottom"]) / 2):
            mid = (c["top"] + c["bottom"]) / 2
            if rows and abs(mid - rows[-1][0]) <= 3.5:
                rows[-1][1].append(c)
            else:
                rows.append([mid, [c]])
        right = max((c["x1"] for c in body), default=float(page.width))
    bullets: list[dict] = []
    current, last_mid, indent = None, None, None
    for mid, row in rows:
        row.sort(key=lambda c: c["x0"])
        x0, x1 = row[0]["x0"], row[-1]["x1"]
        text = "".join(c["text"] for c in row)
        starts = any(abs(g - mid) < 6 for g in glyphs)
        if starts:
            indent = x0
            current = {"text": text, "fills": [], "x0": x0}
            bullets.append(current)
        elif current is not None and indent is not None and abs(x0 - indent) < 2.5 and last_mid is not None \
                and mid - last_mid < 16:
            current["text"] += text
        else:
            current = None
            last_mid = mid
            continue
        current["fills"].append((x1 - indent) / max(1.0, right - indent))
        last_mid = mid
    return [{"text": b["text"], "lines": len(b["fills"]), "last_fill": round(b["fills"][-1], 2),
             "first_fill": round(b["fills"][0], 2)} for b in bullets]


def bullet_fit(pdf_bytes: bytes, printed: dict[str, str]) -> dict[str, dict]:
    """Matches measured bullets to bullet ids by their printed text: {id: {lines, last_fill, ...}}."""
    measured = bullet_lines(pdf_bytes)
    out = {}
    for bid, text in printed.items():
        key = _norm(text)[:28]
        for m in measured:
            if key and _norm(m["text"]).startswith(key):
                out[bid] = {k: m[k] for k in ("lines", "last_fill", "first_fill")}
                break
    return out


def short_last_line(fit: dict, min_fill: float) -> bool:
    return fit.get("lines", 1) > 1 and fit.get("last_fill", 1.0) < min_fill


def fit_score(m: Measure, layout: dict, tpl: dict, value: float) -> float:
    page = tpl["page"]
    lo, hi = page["target_bottom_gap_pt"]
    s = 10000 * max(0, m.pages - 1) + 2000 * m.clipped + 500 * m.margin_violation
    s += 300 * (m.min_font < page["min_font_pt"] - 0.01)
    s += max(0.0, m.bottom_gap - hi) / 4           # unused space at the bottom
    s += max(0.0, lo - m.bottom_gap) * 2           # too close to the bottom edge
    for k, spec in tpl["layout"].items():
        s += abs(layout.get(k, spec["default"]) - spec["default"]) * 3
    return s - value * 5


@dataclass
class Attempt:
    source: str
    pdf: bytes
    measure: Measure
    layout: dict
    plan: dict
    score: float


class LayoutError(RuntimeError):
    pass


def optimize(sel: Selection, bank: dict, texts: dict | None = None, max_compiles: int = 10,
             progress=None, cancelled=lambda: False) -> tuple[Attempt, dict]:
    """Returns (best valid attempt, stats). Raises LayoutError if no one-page result is possible."""
    tpl = render.template_info(bank)
    page = tpl["page"]
    lo_gap, hi_gap = page["target_bottom_gap_pt"]
    layout = render.default_layout(tpl)
    compiles, t0 = 0, time.time()
    best: Attempt | None = None
    history = []
    tried_add_fail: set[str] = set()

    last_measure = None

    def attempt() -> Attempt:
        nonlocal compiles, last_measure
        plan = sel.ordered()
        src = render.render_source(bank, plan, layout, texts, tpl)
        pdf = render.compile_pdf(src, tpl)
        compiles += 1
        m = measure(pdf)
        last_measure = m
        a = Attempt(src, pdf, m, dict(layout), plan, fit_score(m, layout, tpl, sel.total_value()))
        history.append({"pages": m.pages, "gap": round(m.bottom_gap), "score": round(a.score, 1),
                        "layout": dict(layout), "bullets": sum(len(e["bullet_ids"]) for e in plan["experiences"] + plan["projects"])})
        return a

    def snapshot():
        return {k: list(v) for k, v in sel.chosen.items()}

    def restore(snap):
        sel.chosen = {k: list(v) for k, v in snap.items()}

    def step(key: str, direction: int) -> bool:
        spec = tpl["layout"].get(key)
        if not spec:
            return False
        new = round(layout[key] + direction * spec["step"], 2)
        if new < spec["min"] - 1e-9 or new > spec["max"] + 1e-9:
            return False
        layout[key] = new
        return True

    last_good_snap = None
    while compiles < max_compiles:
        if cancelled():
            raise LayoutError("Cancelled.")
        a = attempt()
        m = a.measure
        if progress:
            progress(f"Checking page fit (pass {compiles})…")
        if m.ok(page) and (best is None or a.score < best.score):
            best = a
            last_good_snap = snapshot()
        if m.pages > 1 or m.clipped:
            # Too long. 1) shorter wordings, 2) drop least valuable content, 3) tighten spacing.
            need = max(1.0, m.overflow_lines)
            freed = 0.0
            while freed < need and sel.swap_shorter():
                freed += 1
            while freed < need:
                before = sel.used()
                if not sel.trim_once():
                    break
                freed += max(0.5, before - sel.used())
            if freed < need and not any(step(k, -1) for k in ("item_gap", "list_gap", "section_gap", "heading_gap")):
                if freed == 0:
                    break
            continue
        if not m.ok(page):
            break  # margin/font problem that content changes won't fix
        if m.bottom_gap > hi_gap:
            # Room left. Add content first, then relax spacing toward the defaults.
            free_lines = (m.bottom_gap - (lo_gap + hi_gap) / 2) / m.line_height
            snap = snapshot()
            sel.budget = sel.used() + free_lines
            added = 0
            while sel.add_once():
                added += 1
            key = repr(sorted((k, tuple(v)) for k, v in sel.chosen.items()))
            if added and key not in tried_add_fail:
                tried_add_fail.add(key)
                continue
            restore(snap)
            relaxed = False
            for k in ("item_gap", "list_gap", "section_gap", "heading_gap"):
                spec = tpl["layout"].get(k)
                if spec and layout[k] < spec["max"] and (layout[k] < spec["default"] or m.bottom_gap > hi_gap * 1.6):
                    relaxed = step(k, +1) or relaxed
            if relaxed:
                continue
        break  # fits and the page is full enough
    if best is None and last_measure is not None and last_measure.edge_text:
        raise LayoutError(f"A line runs past the page edge: \"{last_measure.edge_text}…\". "
                          "Shorten that title, company or tech-stack line in your bank.")
    if best is None:
        raise LayoutError("Couldn't fit the resume on one page without breaking the layout rules. "
                          "Try lowering max bullets per item, or shortening long bullets.")
    if last_good_snap is not None:
        restore(last_good_snap)
    stats = {"compiles": compiles, "seconds": round(time.time() - t0, 1), "history": history,
             "final": best.measure.to_json(), "layout": best.layout, "fit_score": round(best.score, 1)}
    return best, stats
