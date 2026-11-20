"""Measure a naive "just ask the LLM" resume tailor against Resumax, on the same postings.

    python tests/naive_baseline.py                  # 6 sample postings + 4 of your saved ones
    python tests/naive_baseline.py --limit 1        # one posting (a cheap dry run)
    python tests/naive_baseline.py --edits 0        # skip the simulated edits

The naive version is the obvious first build: every call sends the whole experience bank, the LaTeX
template and the posting, and the model writes the full LaTeX resume. If the page doesn't compile or
isn't exactly one page, it's sent back to fix (up to 2 times). Each edit ("swap a project", "swap a
bullet") is another full call that rewrites the whole document, and is fixed the same way.

Resumax on the same posting: one ranking call (top 40 bullet ids), a deterministic plan and layout loop,
and edits that re-plan without any model call (see tests/test_drafts.py).

Both use the engine and model in .env, so the same Claude account and model. Answers are cached by
prompt, so a rerun costs nothing; token counts are those of the original call either way. Your name,
email, phone and links are replaced with placeholders before anything is sent for the naive version.

Results: a table here, and output/baseline/<time>.json.
"""
from __future__ import annotations

import argparse
import copy
import glob
import json
import os
import re
import statistics as st
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "server"))
sys.path.insert(0, str(ROOT / "tests"))
from dotenv import load_dotenv  # noqa: E402

load_dotenv(ROOT / ".env")

import yaml  # noqa: E402

import bank as bankmod  # noqa: E402
import layout  # noqa: E402
import llm  # noqa: E402
import render  # noqa: E402
import tailor  # noqa: E402

OUT = ROOT / "output"
BANK = ROOT / "data" / "experiences.yaml"
FIXTURES = ["backend", "cv", "embedded", "fullstack", "hardware", "ml"]
MAX_FIXES = 2
EDITS = [
    "Replace the last project on the page with a different project from the experience bank that fits the "
    "posting better.",
    "In the first experience on the page, swap its weakest bullet for a different bullet of that same "
    "experience from the experience bank.",
]
SCHEMA = {"type": "object", "properties": {"latex": {"type": "string", "description": "The complete LaTeX document."}},
          "required": ["latex"]}
NAIVE_RULES = ("Use the LaTeX resume in <template> as the template: keep its preamble, header and section layout "
               "exactly, and replace the Education, Experience, Projects and Technical Skills content. Use only "
               "facts from <experience_bank>: never invent employers, titles, dates, numbers or skills. It must "
               "compile and fit on exactly one page.")


# ---------------------------------------------------------------- inputs

def redacted(bank: dict) -> dict:
    """The bank with your contact details replaced by placeholders (same rough length)."""
    b = copy.deepcopy(bank)
    p = b.get("profile") or {}
    p.update(first_name="Alex", last_name="Morgan", email="alex.morgan@example.com", phone="555-555-0100",
             location="City, ST")
    links = []
    for i, ln in enumerate(p.get("links") or []):
        if isinstance(ln, dict):
            links.append({k: (f"example.com/link{i}" if isinstance(v, str) else v) for k, v in ln.items()})
        else:
            links.append(f"example.com/link{i}")
    p["links"] = links
    return b


def template_tex(bank: dict, tpl: dict) -> str:
    """An example resume in the real template (from your latest saved run), with placeholder contact details."""
    runs = sorted(glob.glob(str(OUT / "*" / "run.json")), key=os.path.getmtime)
    sel = json.loads(Path(runs[-1]).read_text(encoding="utf-8"))["selection"] if runs else \
        {"experiences": [], "projects": [], "skills": []}
    plan = {k: sel.get(k, []) for k in ("experiences", "projects", "skills")}
    return render.render_source(bank, plan, render.default_layout(tpl), None, tpl)


def bank_yaml(bank: dict) -> str:
    keep = {k: bank[k] for k in ("education", "experiences", "projects", "skills") if k in bank}
    return yaml.safe_dump(keep, sort_keys=False, allow_unicode=True, width=1000)


def postings(limit: int, saved: int) -> list[tuple[str, str]]:
    out = [(f"sample:{n}", (ROOT / "tests" / "fixtures" / "jds" / f"{n}.txt").read_text(encoding="utf-8"))
           for n in FIXTURES]
    seen = {jd[:300] for _, jd in out}
    for f in sorted(glob.glob(str(OUT / "*" / "run.json")), key=os.path.getmtime, reverse=True):
        if saved <= 0:
            break
        r = json.loads(Path(f).read_text(encoding="utf-8"))
        jd = r.get("jd") or ""
        if len(jd) < 400 or jd[:300] in seen:
            continue
        seen.add(jd[:300])
        out.append((f"saved:{(r.get('company') or '?')[:24]}", jd))
        saved -= 1
    return out[:limit] if limit else out


# ---------------------------------------------------------------- checks

def check_pdf(src: str, tpl: dict) -> tuple[int | None, str]:
    """(pages, problem). pages is None when it doesn't compile."""
    try:
        m = layout.measure(render.compile_pdf(src, tpl))
    except Exception as e:  # compile error
        return None, f"It doesn't compile: {str(e)[:300]}"
    if m.pages != 1:
        return m.pages, f"It compiles to {m.pages} pages; it must be exactly one page."
    return 1, ""


def invented_numbers(src: str, allowed: set[str]) -> int:
    """Printed bullets that contain a number found nowhere in the bank or the posting."""
    body = src.split("\\begin{document}", 1)[-1]                      # not the template's own macros
    items = re.findall(r"\\resumeItem\{(.*)\}", body)
    return sum(1 for it in items if bankmod.numbers(it) - allowed)


def invented_examples(src: str, allowed: set[str], n: int = 3) -> list[str]:
    body = src.split("\\begin{document}", 1)[-1]
    out = []
    for it in re.findall(r"\\resumeItem\{(.*)\}", body):
        bad = bankmod.numbers(it) - allowed
        if bad:
            out.append(f"{sorted(bad)}: {bankmod.plain(it)[:140]}")
    return out[:n]


# ---------------------------------------------------------------- naive

def naive_call(context: str, task: str, stats: dict, job: str) -> str:
    data, s = llm.complete_json(f"{task}\n\n{context}", SCHEMA, job_id=job, timeout=400)
    stats["calls"] += 1
    stats["cached"] += bool(s.get("cached"))
    stats["in"] += s.get("input_tokens") or 0
    stats["out"] += s.get("output_tokens") or 0
    stats["model_seconds"] += s.get("seconds") or 0
    return str(data.get("latex") or "")


def naive_fit(context: str, src: str, tpl: dict, stats: dict, job: str) -> tuple[str, int | None]:
    """Compile; while it isn't one page, send it back to fix (up to MAX_FIXES)."""
    pages, problem = check_pdf(src, tpl)
    stats["compiles"] += 1
    for _ in range(MAX_FIXES):
        if not problem:
            break
        stats["fixes"] += 1
        src = naive_call(context, f"{NAIVE_RULES}\n\nThis resume has a problem. {problem} Fix it and return the "
                                  f"complete document.\n\n<resume>\n{src}\n</resume>", stats, job)
        pages, problem = check_pdf(src, tpl)
        stats["compiles"] += 1
    return src, pages


def run_naive(jd: str, tpl: dict, ctx_bank: str, template: str, edits: int, job: str, allowed: set[str]) -> dict:
    stats = {"calls": 0, "cached": 0, "in": 0, "out": 0, "model_seconds": 0.0, "compiles": 0, "fixes": 0}
    t0 = time.time()
    context = (f"<template>\n{template}\n</template>\n\n<experience_bank>\n{ctx_bank}\n</experience_bank>\n\n"
               f"<job_posting>\n{jd}\n</job_posting>")
    src = naive_call(context, f"Write a one-page resume tailored to the job posting. {NAIVE_RULES} Pick the "
                              "experiences, projects and bullet points that best fit the posting.", stats, job)
    src, pages = naive_fit(context, src, tpl, stats, job)
    first = {"one_page": pages == 1, "calls": stats["calls"], "tokens": stats["in"] + stats["out"],
             "in": stats["in"], "out": stats["out"]}
    edit_ok = []
    for e in EDITS[:edits]:
        src = naive_call(context, f"{NAIVE_RULES}\n\nEdit this resume: {e} Return the complete document.\n\n"
                                  f"<resume>\n{src}\n</resume>", stats, job)
        src, pages = naive_fit(context, src, tpl, stats, job)
        edit_ok.append(pages == 1)
    return {**stats, "seconds": round(time.time() - t0, 1), "first": first, "edits_one_page": edit_ok,
            "final_one_page": pages == 1, "invented_number_bullets": invented_numbers(src, allowed),
            "invented_examples": invented_examples(src, allowed)}


# ---------------------------------------------------------------- Resumax

def run_resumax(bank: dict, jd: str, edits: int, allowed: set[str]) -> dict:
    t0 = time.time()
    r = tailor.run(bank, jd, rephrase=False)
    s = r.stats
    return {"calls": s.get("llm_requests") or 0, "cached": bool(s.get("llm_cached")),
            "in": s.get("input_tokens") or 0, "out": s.get("output_tokens") or 0,
            "model_seconds": s.get("llm_seconds") or 0, "compiles": s.get("compiles") or 0,
            "seconds": round(time.time() - t0, 1), "mode": s.get("mode"),
            "final_one_page": r.measure.get("pages") == 1 if isinstance(r.measure, dict) else None,
            "edit_calls": 0, "edits": edits,
            "invented_number_bullets": invented_numbers(r.tex, allowed),
            "invented_examples": invented_examples(r.tex, allowed)}


# ---------------------------------------------------------------- main

def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--limit", type=int, default=0, help="only the first N postings")
    ap.add_argument("--saved", type=int, default=4, help="how many of your saved postings to add to the 6 samples")
    ap.add_argument("--edits", type=int, default=2, choices=range(0, len(EDITS) + 1), help="simulated edits")
    args = ap.parse_args()
    if llm.engine() in ("offline", "local"):
        sys.exit("ENGINE in .env makes no model calls; set ENGINE=claude-code or api to measure.")

    bank = bankmod.load_dict(BANK)
    tpl = render.template_info(bank)
    red = redacted(bank)
    template = template_tex(red, tpl)
    ctx_bank = bank_yaml(red)
    bank_numbers = bankmod.numbers(BANK.read_text(encoding="utf-8"))   # any number anywhere in your bank
    rows = []
    for name, jd in postings(args.limit, args.saved):
        allowed = bank_numbers | bankmod.numbers(jd)
        print(f"\n== {name}")
        try:
            n = run_naive(jd, tpl, ctx_bank, template, args.edits, f"naive-{name}", allowed)
        except llm.LLMError as e:
            n = {"error": str(e)}
        print("  naive  :", {k: v for k, v in n.items() if k != "first"})
        x = run_resumax(bank, jd, args.edits, allowed)
        print("  resumax:", x)
        rows.append({"posting": name, "naive": n, "resumax": x})

    ok = [r for r in rows if "error" not in r["naive"]]
    if not ok:
        sys.exit("No naive runs finished.")

    def med(f):
        return st.median([f(r) for r in ok])

    summary = {
        "postings": len(ok),
        "naive_tokens_first_resume": med(lambda r: r["naive"]["first"]["tokens"]),
        "naive_calls_first_resume": med(lambda r: r["naive"]["first"]["calls"]),
        "naive_tokens_total_with_edits": med(lambda r: r["naive"]["in"] + r["naive"]["out"]),
        "naive_calls_total_with_edits": med(lambda r: r["naive"]["calls"]),
        "naive_first_try_one_page": sum(r["naive"]["first"]["one_page"] and r["naive"]["first"]["calls"] == 1
                                        for r in ok),
        "naive_final_one_page": sum(bool(r["naive"]["final_one_page"]) for r in ok),
        "naive_invented_number_bullets": sum(r["naive"]["invented_number_bullets"] for r in ok),
        "naive_seconds_total": med(lambda r: r["naive"]["seconds"]),
        "resumax_tokens_per_resume": med(lambda r: r["resumax"]["in"] + r["resumax"]["out"]),
        "resumax_calls_per_resume": med(lambda r: r["resumax"]["calls"] or (1 if r["resumax"]["cached"] else 0)),
        "resumax_tokens_per_edit": 0,
        "resumax_final_one_page": sum(bool(r["resumax"]["final_one_page"]) for r in ok),
        "resumax_invented_number_bullets": sum(r["resumax"]["invented_number_bullets"] for r in ok),
        "edits_per_posting": args.edits,
        "model": llm.model_name(), "engine": llm.engine(),
    }
    print("\n== summary (medians per posting)")
    for k, v in summary.items():
        print(f"  {k:36} {v}")
    dest = OUT / "baseline" / f"{datetime.now():%Y%m%d-%H%M%S}.json"
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(json.dumps({"summary": summary, "rows": rows}, indent=2), encoding="utf-8")
    print(f"\nSaved {dest}")


if __name__ == "__main__":
    main()
