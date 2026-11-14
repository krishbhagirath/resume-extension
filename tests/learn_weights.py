"""Learn ranking weights from your A/B picks (output/picks.jsonl).

    python tests/learn_weights.py                 # report, and write weights only if they beat the current ones
    python tests/learn_weights.py --dry-run       # report only
    python tests/learn_weights.py --picks other.jsonl --out some.json --min-picks 10

How it works
  * One feature vector per resume: each score part summed over its printed bullets
    (coverage, role, keywords, evidence, recency, llm, semantic) plus how many printed bullets
    have a short last line.
  * One example per pick: features(A) - features(B), label = "A won". A logistic regression
    with no intercept learns w so that w . (A - B) > 0 when A wins. Each pick is also added
    mirrored (B - A, "B won") so the model isn't skewed by which side tends to win (swaps
    always log the new run as B).
  * Leave-one-out accuracy: fit on all picks but one, predict the one left out, repeat.
  * Baseline: the current hand-set weights from server/planner.py, scored the same way on
    the same features. Their accuracy needs no fitting, so it is computed on every pick.
  * If the learned weights win (and there are at least --min-picks usable picks), they are
    scaled so their total matches the current QUALITY_WEIGHTS total and written to
    data/learned_weights.json. server/planner.py uses that file when it's present.

Needs scikit-learn (requirements-dev.txt). Never calls Claude, never touches your bank.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "server"))

import planner  # noqa: E402

PARTS = ["coverage", "role", "keywords", "evidence", "recency", "llm", "semantic"]
FEATURES = PARTS + ["short_last_line"]
QUALITY_KEYS = list(planner.QUALITY_WEIGHTS)       # what the planner can actually take


# ---------------------------------------------------------------- data

def resume_features(bullets: list[dict]) -> np.ndarray:
    f = np.zeros(len(FEATURES))
    for b in bullets:
        p = b.get("parts") or {}
        for i, k in enumerate(PARTS):
            f[i] += float(p.get(k, 0.0) or 0.0)
        f[-1] += 1.0 if p.get("short_last_line") else 0.0
    return f


def load_picks(path: Path) -> tuple[list[dict], dict]:
    rows, skipped = [], {"unreadable": 0, "no_bullet_scores": 0, "identical": 0}
    if not path.exists():
        return rows, skipped
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            r = json.loads(line)
            ba, bb = r["a"]["bullets"], r["b"]["bullets"]
            winner = r["winner"]
        except (json.JSONDecodeError, KeyError, TypeError):
            skipped["unreadable"] += 1
            continue
        if not ba or not bb or winner not in ("a", "b"):
            skipped["no_bullet_scores"] += 1     # runs made before per-bullet scores were saved
            continue
        x = resume_features(ba) - resume_features(bb)
        if not np.any(np.abs(x) > 1e-9):
            skipped["identical"] += 1            # same content on both sides: says nothing about weights
            continue
        rows.append({"x": x, "y": 1 if winner == "a" else 0, "source": r.get("source", "?"),
                     "bullets": [b for side in (ba, bb) for b in side]})
    return rows, skipped


# ---------------------------------------------------------------- current (hand-set) weights as a linear model

def mean_bullet_quality(rows: list[dict]) -> float:
    """Average planner.quality of a printed bullet in your picks, without the short-line factor."""
    qs = []
    w = planner.QUALITY_WEIGHTS
    for r in rows:
        for b in r["bullets"]:
            p = b.get("parts") or {}
            qs.append(sum(w[k] * float(p.get(k, 0) or 0) for k in QUALITY_KEYS))
    return float(np.mean(qs)) if qs else 1.0


def hand_weights(mean_q: float) -> np.ndarray:
    """Coverage counts 1 (the planner's unit). A short last line multiplies a bullet's quality by
    SHORT_LINE_FACTOR, i.e. costs (1 - factor) x a typical bullet's quality. Evidence and recency
    are counted ungated (the planner scales them by relevance), so this is a close linear stand-in."""
    w = {"coverage": 1.0, **planner.QUALITY_WEIGHTS, "short_last_line": -(1 - planner.SHORT_LINE_FACTOR) * mean_q}
    return np.array([w.get(k, 0.0) for k in FEATURES])


def accuracy(w: np.ndarray, X: np.ndarray, y: np.ndarray) -> float:
    s = X @ w
    return float(np.mean(np.where(np.abs(s) < 1e-12, 0.5, (s > 0) == (y == 1))))


# ---------------------------------------------------------------- learning

def fit(X: np.ndarray, y: np.ndarray, scale: np.ndarray, C: float) -> np.ndarray:
    from sklearn.linear_model import LogisticRegression
    Xs = X / scale
    Xm, ym = np.vstack([Xs, -Xs]), np.concatenate([y, 1 - y])   # mirrored: no side bias
    m = LogisticRegression(fit_intercept=False, C=C, max_iter=5000)
    m.fit(Xm, ym)
    return m.coef_[0]                                             # in standardized units


def loo_accuracy(X: np.ndarray, y: np.ndarray, scale: np.ndarray, C: float) -> float:
    hits = 0.0
    for i in range(len(y)):
        keep = np.arange(len(y)) != i
        w = fit(X[keep], y[keep], scale, C) / scale
        hits += accuracy(w, X[i:i + 1], y[i:i + 1])
    return hits / len(y)


def to_planner(w_raw: np.ndarray, mean_q: float) -> tuple[dict, float, list[str]]:
    """Learned weights in the planner's terms. Scaled so the QUALITY_WEIGHTS total stays the same
    (the balance between coverage and the other signals that the page budget was tuned with),
    only redistributed between signals. Negative weights are clamped to 0."""
    notes = []
    idx = {k: FEATURES.index(k) for k in QUALITY_KEYS}
    raw = {k: float(w_raw[i]) for k, i in idx.items()}
    for k, v in raw.items():
        if v < 0:
            notes.append(f"{k} came out negative ({v:.3f}); set to 0")
    pos = {k: max(0.0, v) for k, v in raw.items()}
    total_now = sum(planner.QUALITY_WEIGHTS.values())
    total_new = sum(pos.values())
    if total_new <= 0:
        raise ValueError("every quality weight came out zero or negative")
    k_scale = total_now / total_new
    weights = {k: round(v * k_scale, 4) for k, v in pos.items()}
    # short last line: undo the linear stand-in -> multiplicative factor, kept in a sane range
    w_short = float(w_raw[FEATURES.index("short_last_line")]) * k_scale
    factor = min(1.0, max(0.5, 1.0 + w_short / max(mean_q, 1e-6)))
    if w_short > 0:
        notes.append("short last lines came out as a plus; factor kept at 1.0 (no penalty)")
    return weights, round(factor, 3), notes


# ---------------------------------------------------------------- main

def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--picks", type=Path, default=ROOT / "output" / "picks.jsonl")
    ap.add_argument("--out", type=Path, default=ROOT / "data" / "learned_weights.json")
    ap.add_argument("--min-picks", type=int, default=15,
                    help="don't write weights from fewer usable picks than this (default 15)")
    ap.add_argument("--C", type=float, default=1.0, help="inverse regularization strength (default 1.0)")
    ap.add_argument("--dry-run", action="store_true", help="report only, never write")
    a = ap.parse_args(argv)

    rows, skipped = load_picks(a.picks)
    print(f"Picks file: {a.picks}")
    print(f"Usable picks: {len(rows)}  (skipped: {skipped['identical']} identical, "
          f"{skipped['no_bullet_scores']} without per-bullet scores, {skipped['unreadable']} unreadable)")
    if len(rows) < 2:
        print("Not enough picks to learn anything yet. Use Make 2 versions / Swap and pick a few dozen times.")
        return 1
    X = np.array([r["x"] for r in rows])
    y = np.array([r["y"] for r in rows])
    by_source = {}
    for r in rows:
        by_source[r["source"]] = by_source.get(r["source"], 0) + 1
    print("By source: " + ", ".join(f"{k} {v}" for k, v in sorted(by_source.items())))

    scale = X.std(axis=0)
    unused = [FEATURES[i] for i in range(len(FEATURES)) if scale[i] < 1e-9]
    scale[scale < 1e-9] = 1.0      # a feature that never differs gets weight 0 anyway
    mean_q = mean_bullet_quality(rows)
    w_hand = hand_weights(mean_q)
    acc_hand = accuracy(w_hand, X, y)
    acc_loo = loo_accuracy(X, y, scale, a.C)
    w_std = fit(X, y, scale, a.C)
    w_raw = w_std / scale

    print()
    print(f"Current hand-set weights: {acc_hand:.1%} of picks predicted")
    print(f"Learned (leave-one-out):  {acc_loo:.1%} of picks predicted  ({len(rows)} folds)")
    print()
    print("What matters most (standardized weight: + favors the winner, - the loser):")
    order = np.argsort(-np.abs(w_std))
    for i in order:
        k = FEATURES[i]
        flag = "  (never differed between A and B)" if k in unused else ""
        print(f"  {k:<16} {w_std[i]:+.3f}{flag}")

    weights, factor, notes = to_planner(w_raw, mean_q)
    print()
    print("In planner terms (same total as now):")
    for k in QUALITY_KEYS:
        print(f"  {k:<10} now {planner.QUALITY_WEIGHTS[k]:<5} -> learned {weights[k]}")
    print(f"  short-line factor now {planner.SHORT_LINE_FACTOR} -> learned {factor}")
    for n in notes:
        print(f"  note: {n}")

    print()
    if acc_loo <= acc_hand:
        print("Learned weights don't beat the current ones. Nothing written.")
        return 0
    if len(rows) < a.min_picks:
        print(f"Learned weights look better, but {len(rows)} picks is too few to trust "
              f"(--min-picks {a.min_picks}). Nothing written.")
        return 0
    if a.dry_run:
        print(f"Learned weights win. --dry-run: would write {a.out}")
        return 0
    payload = {"quality_weights": weights, "short_line_factor": factor,
               "meta": {"created": datetime.now().isoformat(timespec="seconds"), "picks": len(rows),
                        "loo_accuracy": round(acc_loo, 3), "hand_accuracy": round(acc_hand, 3),
                        "hand_weights": planner.QUALITY_WEIGHTS, "hand_short_line_factor": planner.SHORT_LINE_FACTOR,
                        "C": a.C}}
    a.out.parent.mkdir(parents=True, exist_ok=True)
    tmp = a.out.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    tmp.replace(a.out)
    print(f"Learned weights win. Wrote {a.out}.")
    if a.out.resolve() == planner.LEARNED_FILE.resolve():
        print("The server uses them from its next resume on (run.json stats show weights: learned). "
              "Delete the file to go back to the hand-set weights.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
