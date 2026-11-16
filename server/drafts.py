"""Drafts: a planned resume you can look at and change before anything is compiled.

  Make resume (plan job)  -> analyze, rank (one model call, or none), choose content. No LaTeX.
  Edit the draft          -> remove/replace items or bullets, reorder, reword: re-planned in about a
                             second from the saved ranking, so edits never call the model again.
  Build PDF (build job)   -> compile and fit exactly one page once, then download.

Stored as output/drafts/<id>.json. The shape mirrors run.json where the API needs it ("selection",
"provenance") so the same option and validation code works for drafts and finished runs.
"""
from __future__ import annotations

import json
import re
import time
import uuid
from datetime import datetime
from pathlib import Path

import bank as bankmod
import evaluate
import tailor

KEYS = ("include", "exclude", "include_items", "exclude_items")
ID = re.compile(r"[0-9a-f]{12}")
MAX_TEXT = 400


def path(out: Path, draft_id: str) -> Path | None:
    return out / "drafts" / f"{draft_id}.json" if ID.fullmatch(draft_id or "") else None


def load(out: Path, draft_id: str) -> dict | None:
    p = path(out, draft_id)
    if not p or not p.exists():
        return None
    return json.loads(p.read_text(encoding="utf-8"))


def save(out: Path, d: dict) -> None:
    p = path(out, d["id"])
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(d, indent=1), encoding="utf-8")
    tmp.replace(p)


def prepare(bank: dict, d: dict, **kw) -> tailor.Prepared:
    """Re-plan a draft from its saved ranking: no model call."""
    return tailor.prepare(bank, d["jd"], title=d.get("role", ""), company=d.get("company", ""), force=True,
                          use_llm=d.get("variant") != "matcher", llm_rank=d.get("llm_rank") or {},
                          mode_hint=d.get("mode"), texts=d.get("texts"), order=d.get("order"),
                          **{k: d["overrides"].get(k) or [] for k in KEYS}, **kw)


def refresh(bank: dict, d: dict, prep: tailor.Prepared) -> None:
    """Store what the plan now puts on the page (preview + run.json-like fields)."""
    d["preview"] = tailor.preview(bank, prep)
    plan = prep.sel.ordered()
    d["selection"] = {"experiences": plan["experiences"], "projects": plan["projects"], "skills": plan["skills"]}
    d["provenance"] = [{"bullet_id": b["id"], "item_id": it["id"], "source": b["text"]}
                       for s in ("experiences", "projects") for it in d["preview"][s] for b in it["bullets"]]
    d["bullet_scores"] = tailor.bullet_scores(prep)
    d["bank_version"] = bankmod.version_hash(bank)
    d["updated"] = datetime.now().isoformat(timespec="seconds")


def create(out: Path, bank: dict, req: dict, prep: tailor.Prepared, stats: dict) -> dict:
    d = {"id": uuid.uuid4().hex[:12], "created": datetime.now().isoformat(timespec="seconds"),
         "jd": req["jd"], "company": (req.get("company") or prep.job.company or "").strip(),
         "role": (req.get("role") or prep.job.title or "").strip(), "url": req.get("url", ""),
         "variant": req.get("variant") or "claude", "mode": prep.mode, "llm_rank": prep.llm_rank,
         "overrides": {k: list(req.get(k) or []) for k in KEYS}, "order": {}, "texts": {},
         "rev": 0, "changes": [], "logged_rev": 0, "built": None, "stats": stats,
         "text_hash": prep.job.text_hash}
    refresh(bank, d, prep)
    plan = prep.sel.ordered()
    prov = [{"bullet_id": r["bullet_id"], "item_id": r["item_id"], "printed": "bank"} for r in d["provenance"]]
    d["initial"] = {   # the plan before your changes, kept so a build can log "you preferred this"
        "id": f"draft:{d['id']}", "variant": d["variant"], "mode": d["mode"], "company": d["company"],
        "role": d["role"], "analysis": {"text_hash": prep.job.text_hash}, "overrides": d["overrides"],
        "selection": dict(d["selection"]), "bullet_scores": d["bullet_scores"],
        "scorecard": evaluate.scorecard(prep.job, prep.sel, plan, None, prov)}
    save(out, d)
    return d


def clean_text(t: str | None) -> str:
    return re.sub(r"\s+", " ", (t or "")).strip()[:MAX_TEXT]


def fit_changes(d: dict, built_plan: dict) -> dict:
    """What the one-page fitting changed compared with the draft you approved."""
    want = {b["id"]: b["text"] for s in ("experiences", "projects") for it in d["preview"][s] for b in it["bullets"]}
    got = {b for s in ("experiences", "projects") for e in built_plan[s] for b in e["bullet_ids"]}
    return {"dropped": [{"id": b, "text": t} for b, t in want.items() if b not in got],
            "added": sorted(got - set(want))}
