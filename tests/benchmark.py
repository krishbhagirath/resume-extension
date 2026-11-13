"""Measures the offline pipeline on the synthetic role postings (no Claude usage).

    python tests/benchmark.py            # table of time / compiles / coverage / fit per role
"""
import itertools
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tests"))
import conftest  # noqa: E402,F401  (sets up paths, offline engine, LaTeX)
import bank as bankmod  # noqa: E402
import evaluate  # noqa: E402
import tailor  # noqa: E402

bank = bankmod.load_dict(conftest.BANK_FILE)
roles = ["backend", "fullstack", "ml", "cv", "embedded", "hardware"]
plans = {}
print(f"{'role':10} {'sec':>5} {'compiles':>8} {'bullets':>7} {'supp.cov':>8} {'coverage':>8} {'quant':>6} {'gap pt':>6}  projects")
for r in roles:
    jd = (ROOT / "tests" / "fixtures" / "jds" / f"{r}.txt").read_text(encoding="utf-8")
    t = time.time()
    res = tailor.run(bank, jd)
    sc = res.scorecard
    plans[r] = res.plan
    print(f"{r:10} {time.time() - t:5.1f} {res.stats['compiles']:8} {sc['bullets']:7} {sc['supported_coverage']:8.2f} {sc['coverage']:8.2f} "
          f"{sc['quantified']:6.2f} {res.measure['bottom_gap']:6.1f}  {', '.join(p['id'] for p in res.plan['projects'])}")
print("\nbullet-set overlap between roles (Jaccard, lower = more tailored):")
for a, b in itertools.combinations(roles, 2):
    print(f"  {a:10} {b:10} {evaluate.selection_overlap(plans[a], plans[b]):.2f}")
