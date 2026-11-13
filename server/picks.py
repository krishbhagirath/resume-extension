"""Your A/B picks, kept for learning what you prefer: output/picks.jsonl (one JSON object per line).

A pick says which of two resumes for the same posting you liked better and why. Each row keeps,
for both runs, the scorecard and every printed bullet's score broken into its parts (coverage,
keywords, role, evidence, recency, llm), so ranking weights can later be fitted to your choices.

Sources:
  compare  you picked between the two versions of "Make 2 versions" (one ranked by Claude, one by
           the built-in matcher; which is A or B is random)
  swap     you swapped a bullet by hand: the new run is logged as the winner over the run it came from
"""
from __future__ import annotations

import json
import threading
from datetime import datetime
from pathlib import Path

REASONS = ("projects", "bullets", "skills", "layout")
_lock = threading.Lock()


def run_summary(meta: dict) -> dict:
    sel = meta.get("selection") or {}
    return {
        "run_id": meta.get("id"),
        "variant": meta.get("variant", "claude"),
        "mode": meta.get("mode"),               # "offline" when Claude wasn't actually used
        "bank_version": meta.get("bank_version"),
        "template_version": meta.get("template_version"),
        "overrides": meta.get("overrides") or {"include": [], "exclude": []},
        "items": {s: [e["id"] for e in sel.get(s, [])] for s in ("experiences", "projects")},
        "skills": sel.get("skills", []),
        "scorecard": meta.get("scorecard") or {},
        "bullets": meta.get("bullet_scores") or [],
    }


def append(out_dir: Path, run_a: dict, run_b: dict, winner: str, reasons: list[str], source: str,
           **extra) -> dict:
    """run_a / run_b are run.json dicts. winner is "a" or "b"."""
    if winner not in ("a", "b"):
        raise ValueError("winner must be a or b")
    bad = [r for r in reasons if r not in REASONS]
    if bad:
        raise ValueError(f"unknown reasons: {bad}")
    row = {
        "ts": datetime.now().isoformat(timespec="seconds"), "source": source, "winner": winner,
        "reasons": list(dict.fromkeys(reasons)),
        "company": run_a.get("company") or run_b.get("company") or "",
        "role": run_a.get("role") or run_b.get("role") or "",
        "posting_hash": (run_a.get("analysis") or {}).get("text_hash"),
        **extra,
        "a": run_summary(run_a), "b": run_summary(run_b),
    }
    path = out_dir / "picks.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    with _lock, path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(row) + "\n")
    return row


def read(out_dir: Path, limit: int = 200) -> list[dict]:
    path = out_dir / "picks.jsonl"
    if not path.exists():
        return []
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines()[-limit:]:
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return rows
