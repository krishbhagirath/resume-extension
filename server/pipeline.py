"""Durable resume jobs: a small state machine that runs on the local server.

States: queued -> analyzing -> matching -> selecting -> writing -> rendering -> optimizing
        -> completed | failed | cancelled          ("downloading" is done by the extension)

* Jobs run in a background thread on the server, so they don't depend on the Chrome
  extension's service worker staying alive. The extension just polls GET /api/jobs/{id}.
* Each state change is written to output/jobs/<id>.json and appended to output/logs/pipeline.jsonl.
* Idempotent: the same posting + bank + template + settings + variant (+ swaps) returns the running
  job, or the finished result (unless force=True). Duplicate clicks can't start duplicate work.
* Variants: "claude" (default) re-ranks with Claude; "matcher" skips that call (A/B comparisons).
* A finished manual swap (request["swap"]) logs a pick: the new run beats the one it came from.
* Jobs left unfinished by a server restart are marked failed (retryable) on startup.
* Errors carry a code, a plain message, and whether retrying could help.
"""
from __future__ import annotations

import hashlib
import json
import re
import threading
import time
import traceback
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path

import bank as bankmod
import layout
import llm
import picks
import render
import tailor

VARIANTS = ("claude", "matcher")
# manual changes carried from run to run: bullets swapped in/out, whole items replaced
OVERRIDE_KEYS = ("include", "exclude", "include_items", "exclude_items")

TERMINAL = {"completed", "failed", "cancelled"}
TIMEOUT_S = 420


@dataclass
class Job:
    id: str
    key: str
    state: str = "queued"
    message: str = "Waiting to start…"
    created: float = field(default_factory=time.time)
    updated: float = field(default_factory=time.time)
    request: dict = field(default_factory=dict)
    result: dict | None = None
    error: dict | None = None
    progress: list = field(default_factory=list)

    def public(self) -> dict:
        d = asdict(self)
        d["request"] = {k: v for k, v in self.request.items() if k != "jd"}
        return d


class JobStore:
    def __init__(self, output_dir: Path, bank_path: Path):
        self.out = output_dir
        self.bank_path = bank_path
        self.dir = output_dir / "jobs"
        self.log = output_dir / "logs" / "pipeline.jsonl"
        self.dir.mkdir(parents=True, exist_ok=True)
        self.log.parent.mkdir(parents=True, exist_ok=True)
        self.jobs: dict[str, Job] = {}
        self.cancel_flags: set[str] = set()
        self.lock = threading.Lock()
        self.pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="resume-job")
        self._recover()

    # ------------------------------------------------------------ persistence
    def _recover(self):
        for f in sorted(self.dir.glob("*.json"))[-200:]:
            try:
                j = Job(**json.loads(f.read_text(encoding="utf-8")))
            except Exception:
                continue
            if j.state not in TERMINAL:
                j.state, j.message = "failed", "Interrupted (the server restarted). Try again."
                j.error = {"code": "interrupted", "message": j.message, "retryable": True}
                self._save(j)
            self.jobs[j.id] = j

    def _save(self, job: Job):
        job.updated = time.time()
        tmp = self.dir / f"{job.id}.tmp"
        tmp.write_text(json.dumps(asdict(job)), encoding="utf-8")
        tmp.replace(self.dir / f"{job.id}.json")

    def _log(self, job: Job, event: str, **extra):
        row = {"ts": datetime.now().isoformat(timespec="seconds"), "job": job.id, "event": event, "state": job.state, **extra}
        with self.log.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row) + "\n")

    def set_state(self, job: Job, state: str, message: str):
        with self.lock:
            job.state, job.message = state, message
            job.progress.append({"t": round(time.time() - job.created, 1), "state": state, "message": message})
            job.progress = job.progress[-30:]
            self._save(job)
        self._log(job, "state", message=message)

    # ------------------------------------------------------------ API
    def get(self, job_id: str) -> Job | None:
        return self.jobs.get(job_id)

    def start(self, req: dict) -> Job:
        bank_dict = bankmod.load_dict(self.bank_path)
        tpl = render.template_info(bank_dict)
        settings = bank_dict.get("settings", {})
        req = {**req, "variant": req.get("variant") or "claude",
               **{k: sorted(set(req.get(k) or [])) for k in OVERRIDE_KEYS}}
        if req["variant"] not in VARIANTS:
            raise ValueError(f"variant must be one of {VARIANTS}")
        key_src = json.dumps([re.sub(r"\s+", " ", req["jd"]).strip(), req.get("company", ""), req.get("role", ""),
                              bankmod.version_hash(bank_dict), tpl["version"], llm.engine(), llm.model_name(),
                              settings.get("rephrase"), req.get("rephrase"), req["variant"],
                              *[req[k] for k in OVERRIDE_KEYS]], sort_keys=True)
        key = hashlib.sha256(key_src.encode()).hexdigest()[:20]
        with self.lock:
            for j in self.jobs.values():
                if j.key != key:
                    continue
                if j.state not in TERMINAL:
                    return j  # already running: duplicate click
                if (j.state == "completed" and not req.get("force") and j.result
                        and (self.out / j.result["run_id"] / j.result["filename"]).exists()):
                    return j  # same inputs, same answer
            job = Job(id=uuid.uuid4().hex[:12], key=key, request=req)
            self.jobs[job.id] = job
            self._save(job)
        self._log(job, "created", url=req.get("url", ""))
        self.pool.submit(self._run, job, bank_dict, tpl)
        return job

    def cancel(self, job_id: str) -> Job | None:
        job = self.jobs.get(job_id)
        if job and job.state not in TERMINAL:
            self.cancel_flags.add(job_id)
            llm.cancel(job_id)
            self.set_state(job, job.state, "Cancelling…")
        return job

    # ------------------------------------------------------------ worker
    def _run(self, job: Job, bank_dict: dict, tpl: dict):
        started = time.time()
        cancelled = lambda: job.id in self.cancel_flags or time.time() - started > TIMEOUT_S
        try:
            req = job.request
            variant = req.get("variant") or "claude"
            res = tailor.run(bank_dict, req["jd"], title=req.get("role", ""), company=req.get("company", ""),
                             force=req.get("force_text", False), rephrase=req.get("rephrase"), job_id=job.id,
                             progress=lambda s, m: self.set_state(job, s, m), cancelled=cancelled,
                             use_llm=variant != "matcher", **{k: req.get(k) for k in OVERRIDE_KEYS})
            if cancelled():
                raise layout.LayoutError("Cancelled.")
            company = (req.get("company") or res.company or "").strip()
            role = (req.get("role") or res.role or "").strip()
            filename = pdf_filename(bank_dict, company, role)
            run_id, run_dir = self._new_run_dir(clean_name(company) or "Company")
            (run_dir / filename).write_bytes(res.pdf)
            (run_dir / filename).with_suffix(".tex").write_text(res.tex, encoding="utf-8")
            meta = {
                "id": run_id, "job_id": job.id, "filename": filename, "company": company, "role": role,
                "url": req.get("url", ""), "mode": res.stats.get("mode"), "variant": variant,
                "overrides": {k: req.get(k) or [] for k in OVERRIDE_KEYS},
                "created": datetime.now().isoformat(timespec="seconds"),
                "bank_version": bankmod.version_hash(bank_dict), "template": tpl["name"],
                "template_version": tpl["version"], "selection": {**res.plan, "template": tpl["name"],
                                                                  "pages": res.measure.get("pages")},
                "validation": {"one_page": res.measure.get("pages") == 1, **res.measure},
                "layout": res.layout, "scorecard": res.scorecard, "provenance": res.provenance,
                "analysis": res.analysis, "stats": res.stats, "warnings": res.warnings, "jd": req["jd"],
                "bullet_scores": res.bullet_scores,
            }
            (run_dir / "run.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")
            job.result = {"run_id": run_id, "filename": filename, "company": company, "role": role,
                          "pdf_url": f"/api/pdf/{run_id}", "scorecard": res.scorecard, "stats": res.stats,
                          "warnings": res.warnings, "variant": variant}
            if req.get("swap"):
                self._log_swap(req["swap"], meta)
            self.set_state(job, "completed", "Resume ready.")
            self._log(job, "completed", seconds=round(time.time() - started, 1), compiles=res.stats.get("compiles"),
                      llm_requests=res.stats.get("llm_requests"), tokens=res.stats.get("input_tokens", 0)
                      + res.stats.get("output_tokens", 0))
        except tailor.ExtractionError as e:
            self._fail(job, "low_confidence", str(e), retryable=False, extra={"quality": e.quality})
        except llm.LLMError as e:
            if e.code == "cancelled" or job.id in self.cancel_flags:
                self._cancelled(job)
            else:
                self._fail(job, e.code, str(e), e.retryable)
        except layout.LayoutError as e:
            if job.id in self.cancel_flags:
                self._cancelled(job)
            elif time.time() - started > TIMEOUT_S:
                self._fail(job, "timeout", f"Took longer than {TIMEOUT_S // 60} minutes.", True)
            else:
                self._fail(job, "layout", str(e), False)
        except render.CompileError as e:
            self._fail(job, "compile", str(e), False, extra={"log_tail": e.log[-1500:]})
        except bankmod.ValidationError as e:
            self._fail(job, "bank_invalid", "Your experience bank has errors. Fix them on the Experience bank page.",
                       False, extra={"errors": bankmod.format_errors(e)})
        except Exception as e:  # unexpected: keep details in the log, show a short message
            self._log(job, "crash", trace=traceback.format_exc()[-3000:])
            self._fail(job, "internal", f"Something went wrong: {e}", True)
        finally:
            self.cancel_flags.discard(job.id)

    def _new_run_dir(self, company: str) -> tuple[str, Path]:
        """output/<time>_<Company>, with -2, -3… when two jobs (A/B versions) finish in the same second."""
        base = datetime.now().strftime("%Y%m%d-%H%M%S") + "_" + company
        run_id, n = base, 2
        while True:
            try:
                (self.out / run_id).mkdir(parents=True)
                return run_id, self.out / run_id
            except FileExistsError:
                run_id, n = f"{base}-{n}", n + 1

    def _log_swap(self, swap: dict, new_meta: dict):
        """A manual swap is a pick: you preferred the new bullet, so the new run wins over its base."""
        base = str(swap.get("base_run", ""))
        f = self.out / base / "run.json"
        if not re.fullmatch(r"[\w.-]{1,80}", base) or not f.exists():
            return
        try:
            old = json.loads(f.read_text(encoding="utf-8"))
            if swap.get("kind") == "batch":   # several changes applied at once (preview editor)
                sel = new_meta.get("selection") or {}
                items_now = {e["id"] for s in ("experiences", "projects") for e in sel.get(s, []) if e.get("bullet_ids")}
                bullets_now = {b["id"] for b in new_meta.get("bullet_scores", [])}
                done = []
                for c in swap.get("changes", []):
                    now = items_now if c["kind"] == "item" else bullets_now
                    done.append({**c, "applied": (c["in"] in now) if c.get("in") else (c["out"] not in now)})
                reasons = [r for r, k in (("projects", "item"), ("bullets", "bullet")) if any(c["kind"] == k for c in done)]
                picks.append(self.out, old, new_meta, "b", reasons, "swap",
                             swap={"kind": "batch", "changes": done, "applied": all(c["applied"] for c in done)})
                return
            if swap.get("kind") == "item":   # a whole experience/project replaced
                sel = new_meta.get("selection") or {}
                printed = {e["id"] for s in ("experiences", "projects") for e in sel.get(s, []) if e.get("bullet_ids")}
                reasons = ["projects"]
            else:
                printed = {b["id"] for b in new_meta.get("bullet_scores", [])}
                reasons = ["bullets"]
            # applied: the change really shows (new item printed, or a removed item really gone)
            applied = swap.get("in") in printed if swap.get("in") else swap.get("out") not in printed
            picks.append(self.out, old, new_meta, "b", reasons, "swap",
                         swap={"kind": swap.get("kind", "bullet"), "out": swap.get("out"), "in": swap.get("in"),
                               "applied": applied, **{k: swap[k] for k in ("action", "chosen") if k in swap}})
        except Exception:
            self._log(Job(id="-", key="-"), "pick_log_failed", trace=traceback.format_exc()[-1500:])

    def _fail(self, job: Job, code: str, message: str, retryable: bool, extra: dict | None = None):
        job.error = {"code": code, "message": message, "retryable": retryable, **(extra or {})}
        self.set_state(job, "failed", message)

    def _cancelled(self, job: Job):
        job.error = {"code": "cancelled", "message": "Cancelled.", "retryable": True}
        self.set_state(job, "cancelled", "Cancelled.")


# ---------------------------------------------------------------- file names

def clean_name(s: str, limit: int = 40) -> str:
    """'Jane Street Capital, LLC' -> 'JaneStreetCapitalLLC' (letters and digits only)."""
    import unicodedata
    s = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode()  # Café -> Cafe
    s = s.replace("C++", "Cpp").replace("c++", "cpp").replace("C#", "CSharp").replace("&", " and ")
    words = re.findall(r"[A-Za-z0-9]+", s)
    return "".join(w[:1].upper() + w[1:] for w in words)[:limit]


_COMPANY_SUFFIX = re.compile(r"[,\s]+(inc|llc|ltd|limited|corp|corporation|co|plc|gmbh|ulc|s\.?a\.?)\.?\s*$", re.I)
_ROLE_NOISE = re.compile(r"\(.*?\)|\[.*?\]|\b(summer|fall|winter|spring|autumn)\b\s*'?\d{0,4}|\b20\d\d\b|\s[-–—|:]\s.*$|\s/\s.*$", re.I)


def short_role(role: str) -> str:
    r = _ROLE_NOISE.sub(" ", role or "")
    return clean_name(r, 40)


def pdf_filename(bank_dict: dict, company: str, role: str) -> str:
    """Krish_Bhagirath_Shopify_SoftwareEngineerIntern_Resume.pdf (pattern from settings)."""
    p = bank_dict["profile"]
    pattern = bank_dict.get("settings", {}).get("filename_pattern") or "{first}_{last}_{company}_{role}_Resume"
    parts = {"first": clean_name(p.get("first_name", "First")), "last": clean_name(p.get("last_name", "Last")),
             "company": clean_name(_COMPANY_SUFFIX.sub("", company or "")) or "Company", "role": short_role(role)}
    name = pattern.format(**parts)
    name = re.sub(r"_+", "_", name).strip("_")
    name = re.sub(r"[^A-Za-z0-9_-]", "", name)[:120]
    return name + ".pdf"
