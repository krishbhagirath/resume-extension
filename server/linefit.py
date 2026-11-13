"""How every bank bullet actually lays out in your template.

Renders all bullets once (cached per bank + template version), measures each in the PDF and
records: lines used, how full the last line is, and characters per full line. Used to
  * budget the page with real line counts instead of a character estimate
  * steer away from bullets whose last line is just a word or two
  * give rewording a hard character target (one line, or nearly two full lines)
  * show "short last line" warnings in the bank editor
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import bank as bankmod
import layout
import render

CACHE = Path(__file__).resolve().parent.parent / "output" / "cache" / "lines"


def _all_plan(bank: dict) -> dict:
    plan = {}
    for section in ("experiences", "projects"):
        plan[section] = [{"id": it["id"], "bullet_ids": [b["id"] for b in it["bullets"]]} for it in bank.get(section, [])]
    plan["skills"] = []
    return plan


def printed_texts(bank: dict, ids=None, texts: dict | None = None) -> dict[str, str]:
    texts = texts or {}
    out = {}
    for section in ("experiences", "projects"):
        for it in bank.get(section, []):
            for b in it["bullets"]:
                if ids is None or b["id"] in ids:
                    out[b["id"]] = texts.get(b["id"]) or bankmod.plain(b["text"])
    return out


def bank_fit(bank: dict) -> dict[str, dict]:
    """{bullet_id: {lines, last_fill, first_fill, chars_per_line}} for every bullet in the bank."""
    tpl = render.template_info(bank)
    key = hashlib.sha256(json.dumps([bankmod.version_hash(bank), tpl["version"]]).encode()).hexdigest()[:20]
    f = CACHE / f"{key}.json"
    if f.exists():
        return json.loads(f.read_text(encoding="utf-8"))
    src = render.render_source(bank, _all_plan(bank), render.default_layout(tpl), None, tpl)
    pdf = render.compile_pdf(src, tpl)
    printed = printed_texts(bank)
    measured = layout.bullet_lines(pdf, all_pages=True)
    out = {}
    i = 0
    for bid, text in printed.items():
        key_t = layout._norm(text)[:28]
        for j in range(i, len(measured)):
            if layout._norm(measured[j]["text"]).startswith(key_t):
                m = measured[j]
                full = (m["lines"] - 1) + m["last_fill"]
                out[bid] = {"lines": m["lines"], "last_fill": m["last_fill"], "first_fill": m["first_fill"],
                            "chars_per_line": round(len(text) / max(0.3, full))}
                i = j + 1
                break
    CACHE.mkdir(parents=True, exist_ok=True)
    f.write_text(json.dumps(out), encoding="utf-8")
    return out


def chars_per_line(fit: dict[str, dict]) -> int:
    """Typical characters in one full bullet line for this template (median over measured bullets)."""
    vals = sorted(v["chars_per_line"] for v in fit.values() if v["lines"] >= 2 or v["last_fill"] > 0.6)
    return vals[len(vals) // 2] if vals else 118
