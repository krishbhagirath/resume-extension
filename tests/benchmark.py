"""Measures the pipeline on the synthetic role postings (no Claude usage).

    python tests/benchmark.py            # built-in matcher: time / compiles / coverage / fit per role
    python tests/benchmark.py --local    # ENGINE=local: adds the on-device semantic step's timing
"""
import itertools
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tests"))
import conftest  # noqa: E402,F401  (sets up paths, offline engine, LaTeX)
if "--local" in sys.argv:
    os.environ["ENGINE"] = "local"
import bank as bankmod  # noqa: E402
import evaluate  # noqa: E402
import tailor  # noqa: E402

bank = bankmod.load_dict(conftest.BANK_FILE)
roles = ["backend", "fullstack", "ml", "cv", "embedded", "hardware"]
plans = {}
print(f"engine: {os.environ['ENGINE']}")
print(f"{'role':10} {'sec':>5} {'sem s':>5} {'compiles':>8} {'bullets':>7} {'supp.cov':>8} {'coverage':>8} {'quant':>6} "
      f"{'gap pt':>6}  projects")
for r in roles:
    jd = (ROOT / "tests" / "fixtures" / "jds" / f"{r}.txt").read_text(encoding="utf-8")
    t = time.time()
    res = tailor.run(bank, jd)
    sc = res.scorecard
    plans[r] = res.plan
    sem = res.stats["timings"].get("semantic")
    print(f"{r:10} {time.time() - t:5.1f} {'-' if sem is None else f'{sem:.2f}':>5} {res.stats['compiles']:8} "
          f"{sc['bullets']:7} {sc['supported_coverage']:8.2f} {sc['coverage']:8.2f} "
          f"{sc['quantified']:6.2f} {res.measure['bottom_gap']:6.1f}  {', '.join(p['id'] for p in res.plan['projects'])}")
print("\nbullet-set overlap between roles (Jaccard, lower = more tailored):")
for a, b in itertools.combinations(roles, 2):
    print(f"  {a:10} {b:10} {evaluate.selection_overlap(plans[a], plans[b]):.2f}")
