"""Local server for Resume Tailor. Run with:  python server/app.py

Listens on 127.0.0.1 only. Websites you visit can't use it:
  * the Host header must be 127.0.0.1/localhost (blocks DNS-rebinding tricks)
  * cross-site requests are refused unless they come from a Chrome extension
  * anything that changes data or starts work needs the X-RT-Token header (a random secret
    in data/.token). The web page gets it inlined; the extension gets it once via /api/pair,
    and only the first extension that pairs is trusted (reset on the Settings tab).
"""
from __future__ import annotations

import base64
import html
import json
import os
import random
import re
import secrets
import sys
import time
from pathlib import Path
from typing import Literal

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, PlainTextResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

sys.path.insert(0, str(Path(__file__).resolve().parent))
ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")

import analyzer  # noqa: E402
import bank as bankmod  # noqa: E402
import drafts  # noqa: E402
import importer  # noqa: E402
import layout  # noqa: E402
import linefit  # noqa: E402
import llm  # noqa: E402
import picks  # noqa: E402
import pipeline  # noqa: E402
import render  # noqa: E402

BANK_PATH = Path(os.getenv("BANK_PATH", ROOT / "data" / "experiences.yaml"))
OUTPUT_DIR = Path(os.getenv("OUTPUT_DIR", ROOT / "output"))
PORT = int(os.getenv("PORT", 8765))
DATA_DIR = BANK_PATH.parent
TOKEN_FILE = DATA_DIR / ".token"
PAIR_FILE = DATA_DIR / ".paired-extension"
MAX_JD = 60_000
EXT_ORIGIN = re.compile(r"^chrome-extension://[a-p]{32}$")


def _token() -> str:
    if TOKEN_FILE.exists():
        t = TOKEN_FILE.read_text(encoding="utf-8").strip()
        if len(t) >= 32:
            return t
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    t = secrets.token_urlsafe(32)
    TOKEN_FILE.write_text(t, encoding="utf-8")
    return t


TOKEN = _token()
store = pipeline.JobStore(OUTPUT_DIR, BANK_PATH)
app = FastAPI(title="Resume Tailor", docs_url=None, redoc_url=None, openapi_url=None)


# ---------------------------------------------------------------- security

def _allowed_hosts() -> set[str]:
    return {f"127.0.0.1:{PORT}", f"localhost:{PORT}", "127.0.0.1", "localhost"}


def _same_origins() -> set[str]:
    return {f"http://127.0.0.1:{PORT}", f"http://localhost:{PORT}"}


def _paired() -> str:
    return PAIR_FILE.read_text(encoding="utf-8").strip() if PAIR_FILE.exists() else ""


OPEN_PATHS = {"/api/health", "/api/pair"}
NAV_GET = re.compile(r"^/api/(pdf|overleaf)/")  # opened as browser tabs (no custom headers possible)


@app.middleware("http")
async def guard(request: Request, call_next):
    host = (request.headers.get("host") or "").lower()
    if host not in _allowed_hosts():
        return PlainTextResponse("Wrong host", status_code=421)
    origin = request.headers.get("origin", "")
    site = request.headers.get("sec-fetch-site", "")
    from_ext = bool(EXT_ORIGIN.match(origin))
    if origin and origin not in _same_origins() and not from_ext:
        return PlainTextResponse("Cross-site request refused", status_code=403)
    token_ok = secrets.compare_digest(request.headers.get("x-rt-token", ""), TOKEN)
    # Cross-site without a token = a website (link, <img>, form post). Websites can't send the
    # custom token header without a CORS preflight, which only extensions pass.
    if site == "cross-site" and not from_ext and not token_ok:
        return PlainTextResponse("Cross-site request refused", status_code=403)
    if from_ext and _paired() and origin.removeprefix("chrome-extension://") != _paired() \
            and request.url.path != "/api/pair" and request.method != "OPTIONS":
        return PlainTextResponse("This extension isn't paired. Reset pairing on the Settings tab.", status_code=403)
    path = request.url.path
    if path.startswith("/api/") and path not in OPEN_PATHS and request.method != "OPTIONS":
        is_nav = request.method == "GET" and NAV_GET.match(path) and site in ("", "none", "same-origin")
        if not is_nav and not token_ok:
            return JSONResponse({"detail": "Missing or wrong token. Reload the page or re-pair the extension.",
                                 "code": "auth"}, status_code=401)
    resp: Response = await call_next(request)
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["Referrer-Policy"] = "no-referrer"
    resp.headers.setdefault("Cache-Control", "no-store")
    return resp


# Only Chrome extensions get CORS (the web UI is same-origin and needs none).
app.add_middleware(CORSMiddleware, allow_origin_regex=EXT_ORIGIN.pattern, allow_methods=["GET", "POST", "PUT"],
                   allow_headers=["Content-Type", "X-RT-Token"], max_age=600)


# ---------------------------------------------------------------- helpers

def _run_dir(run_id: str) -> Path:
    if not re.fullmatch(r"[\w.-]{1,80}", run_id):
        raise HTTPException(404, "Not found")
    d = (OUTPUT_DIR / run_id).resolve()
    if OUTPUT_DIR.resolve() not in d.parents or not (d / "run.json").exists():
        raise HTTPException(404, "Not found")
    return d


def _meta(run_id: str) -> dict:
    return json.loads((_run_dir(run_id) / "run.json").read_text(encoding="utf-8"))


def _bank_or_400() -> dict:
    try:
        return bankmod.load_dict(BANK_PATH)
    except bankmod.ValidationError as e:
        raise HTTPException(422, {"message": "Your experience bank has errors.", "errors": bankmod.format_errors(e)})
    except Exception as e:
        raise HTTPException(422, {"message": f"Couldn't read experiences.yaml: {e}", "errors": []})


# ---------------------------------------------------------------- status

@app.get("/api/health")
def health():
    """No secrets here: this endpoint is readable without a token."""
    eng = llm.engine()
    info = {"ok": True, "engine": eng, "model": llm.model_name(),
            "compiler": Path((render.find_compiler() or ["none"])[0]).stem, "paired": bool(_paired())}
    problem = ""
    if eng == "claude-code":
        st = llm.claude_code_status()
        info["mode"] = "claude-code" if st["installed"] and st["logged_in"] else "offline"
        if not st["installed"]:
            problem = "Claude Code isn't installed. Run setup.bat."
        elif not st["logged_in"]:
            problem = "Resume Tailor's Claude account isn't signed in. Double-click claude-login.bat."
        elif st["auth"] in ("api_key", "api_key_helper"):
            problem = "Claude Code is signed in with an API key, which bills the API. Run claude-login.bat again."
    elif eng == "api":
        info["mode"] = "api" if os.getenv("ANTHROPIC_API_KEY") else "offline"
    elif eng == "local":
        import importlib.util
        if importlib.util.find_spec("fastembed"):
            info["mode"] = "local"
        else:
            info["mode"] = "offline"
            problem = "ENGINE=local needs fastembed. Run setup.bat (or pip install -r requirements.txt)."
    else:
        info["mode"] = "offline"
    if info["compiler"] == "none":
        problem = problem or "No LaTeX compiler found. Run setup.bat (it downloads Tectonic)."
    info["problem"] = problem
    return info


@app.post("/api/pair")
def pair(request: Request):
    """Gives the token to the Chrome extension. Websites can't fake a chrome-extension:// origin.
    (POST because Chrome only sends the extension's Origin header on non-GET requests.)"""
    origin = request.headers.get("origin", "")
    if not EXT_ORIGIN.match(origin):
        raise HTTPException(403, "Only the Resume Tailor extension can pair.")
    ext_id = origin.removeprefix("chrome-extension://")
    current = _paired()
    if current and current != ext_id:
        raise HTTPException(403, "Another extension is already paired. Reset pairing on the Settings tab first.")
    if not current:
        PAIR_FILE.write_text(ext_id, encoding="utf-8")
    return {"token": TOKEN}


@app.post("/api/pair/reset")
def pair_reset():
    PAIR_FILE.unlink(missing_ok=True)
    return {"ok": True}


@app.get("/api/diagnostics")
def diagnostics():
    st = llm.claude_code_status() if llm.engine() == "claude-code" else {}
    jobs = sorted(store.jobs.values(), key=lambda j: -j.created)[:15]
    try:
        bank = bankmod.load_dict(BANK_PATH)
        bank_info = {"ok": True, "version": bankmod.version_hash(bank),
                     "items": len(bank["experiences"]) + len(bank["projects"]),
                     "bullets": sum(len(i["bullets"]) for s in ("experiences", "projects") for i in bank[s])}
        tpl = render.template_info(bank)
        tpl_info = {"name": tpl["name"], "version": tpl["version"]}
    except Exception as e:
        bank_info, tpl_info = {"ok": False, "error": str(e)[:300]}, {}
    log_tail = []
    log = OUTPUT_DIR / "logs" / "pipeline.jsonl"
    if log.exists():
        log_tail = [json.loads(x) for x in log.read_text(encoding="utf-8").splitlines()[-25:] if x.strip()]
    return {"health": health(), "claude_account": st.get("account", ""), "claude_auth": st.get("auth", ""),
            "bank": bank_info, "template": tpl_info, "templates": render.list_templates(),
            "paired_extension": _paired(), "jobs": [j.public() for j in jobs], "log": log_tail,
            "paths": {"bank": str(BANK_PATH), "output": str(OUTPUT_DIR)}}


@app.get("/api/templates")
def templates():
    return render.list_templates()


# ---------------------------------------------------------------- jobs

class JobRequest(BaseModel):
    jd: str = Field(max_length=MAX_JD)
    company: str = Field("", max_length=120)
    role: str = Field("", max_length=200)
    url: str = Field("", max_length=2000)
    force: bool = False          # ignore a cached identical result
    force_text: bool = False     # go ahead even if the text doesn't look like a full posting
    rephrase: bool | None = None  # None = use the setting
    source: str = Field("", max_length=40)   # e.g. "greenhouse", "jsonld", "manual"
    confidence: float | None = None
    variant: Literal["claude", "matcher"] = "claude"   # matcher = skip the Claude re-rank
    mode: Literal["full", "plan"] = "full"   # plan = choose content only, preview before compiling


@app.post("/api/jobs")
def create_job(req: JobRequest):
    if len(req.jd.strip()) < 50:
        raise HTTPException(400, "The job description looks too short. Paste the full posting.")
    _bank_or_400()
    job = store.start(req.model_dump())
    return job.public()


@app.post("/api/compare")
def compare(req: JobRequest):
    """Starts two versions for one posting: ranked by Claude, and by the built-in matcher only.
    Which one is A and which is B is random, so you judge the resumes, not the label."""
    if len(req.jd.strip()) < 50:
        raise HTTPException(400, "The job description looks too short. Paste the full posting.")
    if not llm.available():
        raise HTTPException(409, "Claude isn't available, so both versions would be the built-in matcher. "
                                 "Check the status at the top of the page.")
    _bank_or_400()
    base = req.model_dump()
    jobs = {v: store.start({**base, "variant": v}) for v in ("claude", "matcher")}
    a, b = random.sample(["claude", "matcher"], 2)
    return {"a": jobs[a].public(), "b": jobs[b].public()}


class PickBody(BaseModel):
    run_a: str = Field(max_length=80)
    run_b: str = Field(max_length=80)
    winner: Literal["a", "b"]
    reasons: list[Literal["projects", "bullets", "skills", "layout"]] = Field(default_factory=list, max_length=4)
    source: Literal["compare"] = "compare"


@app.post("/api/picks")
def add_pick(body: PickBody):
    """Saves which of two resumes you liked better (output/picks.jsonl) with both runs' scorecards
    and per-bullet score parts."""
    if body.run_a == body.run_b:
        raise HTTPException(400, "Pick between two different resumes.")
    row = picks.append(OUTPUT_DIR, _meta(body.run_a), _meta(body.run_b), body.winner, list(body.reasons), body.source)
    return {"ok": True, "winner": body.winner, "variants": {"a": row["a"]["variant"], "b": row["b"]["variant"]},
            "modes": {"a": row["a"]["mode"], "b": row["b"]["mode"]}}


@app.get("/api/jobs")
def list_jobs(limit: int = 20):
    jobs = sorted(store.jobs.values(), key=lambda j: -j.created)[:max(1, min(limit, 100))]
    return [j.public() for j in jobs]


@app.get("/api/jobs/{job_id}")
def get_job(job_id: str):
    job = store.get(job_id)
    if not job:
        raise HTTPException(404, "No such job (the server may have been reset).")
    return job.public()


@app.post("/api/jobs/{job_id}/cancel")
def cancel_job(job_id: str):
    job = store.cancel(job_id)
    if not job:
        raise HTTPException(404, "No such job.")
    return job.public()


class AnalyzeRequest(BaseModel):
    jd: str = Field(max_length=MAX_JD)
    title: str = Field("", max_length=200)
    company: str = Field("", max_length=120)


@app.post("/api/analyze")
def analyze(req: AnalyzeRequest):
    """Fast, deterministic: what the posting asks for, and whether the text looks complete."""
    job = analyzer.analyze(req.jd, title=req.title, company=req.company)
    return {"title": job.title, "company": job.company, "roles": job.roles, "primary_roles": job.primary_roles,
            "seniority": job.seniority, "years_required": job.years_required, "quality": job.quality,
            "requirements": [r.model_dump() for r in job.requirements[:25]]}


@app.post("/api/generate")
def generate_compat(req: JobRequest):
    """Older one-shot call: starts a job and waits for it (kept for compatibility)."""
    job = create_job(req)
    jid = job["id"]
    deadline = time.time() + pipeline.TIMEOUT_S + 30
    while time.time() < deadline:
        j = store.get(jid)
        if j.state in pipeline.TERMINAL:
            if j.state != "completed":
                raise HTTPException(400 if not (j.error or {}).get("retryable") else 503,
                                    (j.error or {}).get("message", j.message))
            return {**j.result, "id": j.result["run_id"], "job_id": jid}
        time.sleep(0.4)
    raise HTTPException(504, "Timed out.")


# ---------------------------------------------------------------- bank

@app.get("/api/bank", response_class=PlainTextResponse)
def get_bank_yaml():
    return BANK_PATH.read_text(encoding="utf-8")


class YamlBody(BaseModel):
    yaml: str = Field(max_length=2_000_000)
    version: str | None = None


@app.put("/api/bank")
def put_bank_yaml(body: YamlBody):
    _check_version(body.version)
    try:
        b = bankmod.save_text(BANK_PATH, body.yaml)
    except bankmod.ValidationError as e:
        raise HTTPException(422, {"message": "Not saved: fix the highlighted problems.", "errors": bankmod.format_errors(e)})
    except Exception as e:
        raise HTTPException(422, {"message": f"Not saved: {e}", "errors": []})
    return {"ok": True, "version": bankmod.version_hash(bankmod.to_dict(b))}


@app.get("/api/bank.json")
def get_bank_json():
    try:
        data = bankmod.load_dict(BANK_PATH)
        return {"bank": data, "version": bankmod.version_hash(data), "errors": []}
    except Exception as e:
        # still hand back what's there so it can be fixed in the editor
        import yaml
        try:
            raw = bankmod.migrate(yaml.safe_load(BANK_PATH.read_text(encoding="utf-8")) or {})
        except Exception:
            raw = {}
        return {"bank": raw, "version": None, "errors": bankmod.format_errors(e)}


class BankBody(BaseModel):
    bank: dict
    version: str | None = None


def _check_version(version: str | None):
    if not version:
        return
    try:
        current = bankmod.version_hash(bankmod.load_dict(BANK_PATH))
    except Exception:
        return
    if current != version:
        raise HTTPException(409, {"message": "The bank changed since you opened it (another tab or the YAML file). "
                                             "Reload to get the latest, then redo your edit.", "errors": []})


@app.put("/api/bank.json")
def put_bank_json(body: BankBody):
    _check_version(body.version)
    try:
        b = bankmod.save(BANK_PATH, body.bank)
    except bankmod.ValidationError as e:
        raise HTTPException(422, {"message": "Not saved: fix the highlighted problems.", "errors": bankmod.format_errors(e)})
    data = bankmod.to_dict(b)
    return {"ok": True, "version": bankmod.version_hash(data)}


@app.post("/api/bank/validate")
def validate_bank(body: BankBody):
    try:
        bankmod.parse(body.bank)
        return {"ok": True, "errors": []}
    except Exception as e:
        return {"ok": False, "errors": bankmod.format_errors(e)}


@app.get("/api/bank/export")
def export_bank():
    data = bankmod.load_dict(BANK_PATH)
    return Response(bankmod.export_json(data), media_type="application/json",
                    headers={"Content-Disposition": 'attachment; filename="experience-bank.json"'})


class JsonText(BaseModel):
    text: str = Field(max_length=4_000_000)


@app.post("/api/bank/import")
def import_bank(body: JsonText):
    """Checks an exported JSON bank. Returns it for review; nothing is saved until you press Save."""
    try:
        return {"ok": True, "bank": bankmod.import_json(body.text), "errors": []}
    except json.JSONDecodeError as e:
        return {"ok": False, "bank": None, "errors": [{"path": "", "message": f"Not valid JSON: {e}"}]}
    except Exception as e:
        return {"ok": False, "bank": None, "errors": bankmod.format_errors(e)}


class PdfBody(BaseModel):
    pdf_base64: str = Field(max_length=14_000_000)


@app.post("/api/parse-resume")
def parse_resume(body: PdfBody):
    """Reads an existing resume PDF into drafts for you to review. Nothing is saved."""
    try:
        data = base64.b64decode(body.pdf_base64.split(",")[-1], validate=False)
    except Exception:
        raise HTTPException(400, "That file couldn't be read.")
    if not data.startswith(b"%PDF"):
        raise HTTPException(400, "That isn't a PDF.")
    try:
        return importer.parse_resume(data)
    except Exception as e:
        raise HTTPException(400, f"Couldn't read that PDF: {e}")


@app.get("/api/bank/lines")
def bank_lines():
    """How each bullet lays out in your template: lines used and how full the last line is."""
    bank = _bank_or_400()
    try:
        fit = linefit.bank_fit(bank)
    except render.CompileError as e:
        raise HTTPException(422, {"message": str(e), "errors": []})
    s = bank.get("settings", {})
    return {"bullets": fit, "chars_per_line": linefit.chars_per_line(fit),
            "min_last_line_fill": s.get("min_last_line_fill", 0.6)}


@app.get("/api/usage")
def usage():
    """Claude usage by Resume Tailor, from saved runs (tokens as reported by Claude Code)."""
    from datetime import datetime, timedelta
    now = datetime.now()
    windows = {"today": now.replace(hour=0, minute=0, second=0, microsecond=0), "7d": now - timedelta(days=7),
               "30d": now - timedelta(days=30), "all": datetime.min}
    out = {k: {"resumes": 0, "claude_calls": 0, "cached": 0, "input_tokens": 0, "output_tokens": 0,
               "est_cost_usd": 0.0, "claude_seconds": 0.0} for k in windows}
    if OUTPUT_DIR.exists():
        for f in OUTPUT_DIR.glob("*/run.json"):
            try:
                m = json.loads(f.read_text(encoding="utf-8"))
                created = datetime.fromisoformat(m.get("created"))
            except Exception:
                continue
            st = m.get("stats") or {}
            for k, since in windows.items():
                if created >= since:
                    o = out[k]
                    o["resumes"] += 1
                    o["claude_calls"] += st.get("llm_requests", 0) or 0
                    o["cached"] += st.get("llm_cached", 0) or 0
                    o["input_tokens"] += st.get("input_tokens", 0) or 0
                    o["output_tokens"] += st.get("output_tokens", 0) or 0
                    o["est_cost_usd"] += st.get("est_cost_usd", 0) or 0
                    o["claude_seconds"] += st.get("llm_seconds", 0) or st.get("timings", {}).get("rank", 0) or 0
    for o in out.values():
        o["est_cost_usd"] = round(o["est_cost_usd"], 4)
        o["claude_seconds"] = round(o["claude_seconds"], 1)
        n = max(1, o["resumes"])
        o["per_resume"] = {"input_tokens": round(o["input_tokens"] / n), "output_tokens": round(o["output_tokens"] / n),
                           "claude_calls": round(o["claude_calls"] / n, 2)}
    return out


# ---------------------------------------------------------------- runs

@app.get("/api/run/{run_id}")
def get_run(run_id: str):
    return _meta(run_id) | {"pdf_url": f"/api/pdf/{run_id}"}


@app.post("/api/run/{run_id}/regenerate")
def regenerate(run_id: str):
    m = _meta(run_id)
    if not m.get("jd"):
        raise HTTPException(400, "This run has no saved job description.")
    req = JobRequest(jd=m["jd"], company=m.get("company", ""), role=m.get("role", ""), url=m.get("url", ""),
                     force=True, force_text=True, variant=m.get("variant") or "claude")
    return create_job(req)


def _printed(meta: dict) -> dict[str, str]:
    """bullet id -> item id for every bullet printed in a run."""
    return {p["bullet_id"]: p["item_id"] for p in meta.get("provenance") or []}


def _strict_fit(bank: dict) -> tuple[bool, dict]:
    """(strict_lines on?, measured line fit when it is). Fit failures just mean nothing is blocked."""
    strict = bool(bank.get("settings", {}).get("strict_lines"))
    if not strict:
        return False, {}
    try:
        return True, linefit.bank_fit(bank)
    except Exception:
        return True, {}


def _swap_options(meta: dict, bank: dict) -> dict[str, list[dict]]:
    printed = _printed(meta)
    strict, fit = _strict_fit(bank)
    min_fill = bank.get("settings", {}).get("min_last_line_fill", 0.6)
    group = {b["id"]: b.get("alt_of") or b["id"] for s in ("experiences", "projects")
             for it in bank.get(s, []) for b in it.get("bullets", [])}
    printed_group = {group[b]: b for b in printed if b in group}
    out = {}
    for section in ("experiences", "projects"):
        for item in bank.get(section, []):
            if item["id"] not in printed.values():
                continue
            rows = []
            for b in item.get("bullets", []):
                if b["id"] in printed or b.get("hidden"):
                    continue
                f = fit.get(b["id"])
                short = bool(f and layout.short_last_line(f, min_fill))
                # same_as: the printed bullet this is another wording of. It can only replace that one.
                rows.append({"id": b["id"], "text": bankmod.plain(b["text"]), "alt_of": b.get("alt_of"),
                             "same_as": printed_group.get(group[b["id"]]),
                             "short_last_line": short, "blocked": bool(strict and short)})
            out[item["id"]] = rows
    return out


@app.get("/api/run/{run_id}/swap-options")
def swap_options(run_id: str):
    """For each item on the page: its bullets that weren't printed (what you could swap in)."""
    return _swap_options(_meta(run_id), _bank_or_400())


class SwapBody(BaseModel):
    out: str = Field(max_length=120)
    in_: str = Field(alias="in", max_length=120)


@app.post("/api/run/{run_id}/swap")
def swap_bullet(run_id: str, body: SwapBody):
    """Rebuilds this resume with one printed bullet replaced by another bullet of the same item.
    Logs a pick when it finishes: the new resume is preferred over this one."""
    m = _meta(run_id)
    if not m.get("jd"):
        raise HTTPException(400, "This run has no saved job description.")
    printed = _printed(m)
    if body.out not in printed:
        raise HTTPException(400, "That bullet isn't on this resume.")
    options = _swap_options(m, _bank_or_400()).get(printed[body.out], [])
    choice = next((o for o in options if o["id"] == body.in_), None)
    if not choice:
        raise HTTPException(409, "That bullet isn't an unused bullet of the same item in your current bank.")
    if choice["blocked"]:
        raise HTTPException(409, "That bullet leaves a word or two on its last line, and strict lines is on, "
                                 "so it's never printed. Fix it in the bank or pick another.")
    if choice["same_as"] and choice["same_as"] != body.out:
        raise HTTPException(409, f"That's another wording of {choice['same_as']}, which is already on the page. "
                                 "Swap that bullet instead.")
    ov = _overrides(m)
    ov["include"] = (ov["include"] - {body.out}) | {body.in_}
    ov["exclude"] = (ov["exclude"] - {body.in_}) | {body.out}
    return _rebuild(run_id, m, ov, {"kind": "bullet", "out": body.out, "in": body.in_})


def _overrides(meta: dict) -> dict[str, set]:
    ov = meta.get("overrides") or {}
    return {k: set(ov.get(k) or []) for k in pipeline.OVERRIDE_KEYS}


def _rebuild(run_id: str, m: dict, ov: dict[str, set], swap: dict) -> dict:
    """Start a job for the same posting with these manual changes. Logs a pick when it finishes."""
    req = {"jd": m["jd"], "company": m.get("company", ""), "role": m.get("role", ""), "url": m.get("url", ""),
           "force_text": True, "source": "swap", "variant": m.get("variant") or "claude",
           **{k: sorted(v) for k, v in ov.items()}, "swap": {"base_run": run_id, **swap}}
    return store.start(req).public()


def _item_info(item: dict, section: str) -> dict:
    return {"id": item["id"], "title": item.get("title", item["id"]), "org": item.get("org") or "",
            "section": section, "kind": item.get("kind") or ""}


def _item_options(meta: dict, bank: dict) -> dict:
    """What's on the page per section, and which other items could replace one of them."""
    sel = meta.get("selection") or {}
    strict, fit = _strict_fit(bank)
    min_fill = bank.get("settings", {}).get("min_last_line_fill", 0.6)
    by_id = {it["id"]: (it, s) for s in ("experiences", "projects") for it in bank.get(s, [])}
    printed = {e["id"] for s in ("experiences", "projects") for e in sel.get(s, []) if e.get("bullet_ids")}
    out = {}
    for section in ("experiences", "projects"):
        on_page = []
        for e in sel.get(section, []):
            if e.get("bullet_ids") and e["id"] in by_id:
                on_page.append(_item_info(by_id[e["id"]][0], section) | {"bullets": len(e["bullet_ids"])})
        unused = []
        for it in bank.get(section, []):
            if it["id"] in printed or it.get("hidden"):
                continue
            visible = [b for b in it.get("bullets", []) if not b.get("hidden")]
            if not visible:
                continue
            clean = [b for b in visible if not (fit.get(b["id"]) and layout.short_last_line(fit[b["id"]], min_fill))]
            # items that describe the same work as one on the page can only replace that one
            overlaps = sorted(p for p in printed if p in set(it.get("overlaps") or [])
                              or it["id"] in set(by_id[p][0].get("overlaps") or []))
            unused.append(_item_info(it, section) | {"bullets": len(visible), "overlaps": overlaps,
                                                     "blocked": bool(strict and not clean)})
        out[section] = {"on_page": on_page, "unused": unused}
    return out


@app.get("/api/run/{run_id}/items")
def run_items(run_id: str):
    """The experiences and projects on this resume, and what could replace each one."""
    return _item_options(_meta(run_id), _bank_or_400())


class ReplaceBody(BaseModel):
    out: str = Field(max_length=120)
    in_: str | None = Field(None, alias="in", max_length=120)   # an item id, "any", or empty/None to remove


def _rank_unused(m: dict, bank: dict, candidates: list[dict]) -> list[dict]:
    """Candidate items, best fit for this run's posting first, by the built-in matcher's bullet scores
    (deterministic, no Claude call): the sum of each item's 3 best bullets."""
    import retrieval
    job = analyzer.analyze(m["jd"], title=m.get("role", ""), company=m.get("company", ""))
    cands, _ = retrieval.score_bank(bank, job)
    by_item: dict[str, list[float]] = {}
    for c in cands:
        by_item.setdefault(c.item_id, []).append(c.score)
    return sorted(candidates, key=lambda i: -sum(sorted(by_item.get(i["id"], [0.0]), reverse=True)[:3]))


def _best_unused(m: dict, bank: dict, candidates: list[dict]) -> dict:
    return _rank_unused(m, bank, candidates)[0]


@app.post("/api/run/{run_id}/replace")
def replace_item(run_id: str, body: ReplaceBody):
    """Rebuilds this resume without one experience/project. `in` picks what takes its place: an item id
    from the same section, "any" for the unused one that best fits the posting, or nothing to just remove
    it (the freed space goes to whatever fits best). Earlier changes carry over. Logs a pick when done."""
    m = _meta(run_id)
    if not m.get("jd"):
        raise HTTPException(400, "This run has no saved job description.")
    bank = _bank_or_400()
    opts = _item_options(m, bank)
    section = next((s for s, v in opts.items() if any(i["id"] == body.out for i in v["on_page"])), None)
    if not section:
        raise HTTPException(400, "That experience or project isn't on this resume.")
    ov = _overrides(m)
    ov["exclude_items"] = ov["exclude_items"] | {body.out}
    ov["include_items"] = ov["include_items"] - {body.out}
    want = (body.in_ or "").strip()
    if not want:   # just remove it
        return _rebuild(run_id, m, ov, {"kind": "item", "action": "remove", "out": body.out, "in": None})
    usable = [i for i in opts[section]["unused"] if not i["blocked"] and not set(i["overlaps"]) - {body.out}]
    if want == "any":
        if not usable:
            raise HTTPException(409, "Nothing else in this section can take its place. Remove it instead.")
        choice = _best_unused(m, bank, usable)
    else:
        choice = next((i for i in opts[section]["unused"] if i["id"] == want), None)
        if not choice:
            raise HTTPException(409, "Pick an unused, visible item from the same section of your current bank.")
        if choice["blocked"]:
            raise HTTPException(409, "Every bullet of that item leaves a word or two on its last line, and strict "
                                     "lines is on, so it can't be printed. Fix it in the bank first.")
        if set(choice["overlaps"]) - {body.out}:
            raise HTTPException(409, f"That describes the same work as {', '.join(choice['overlaps'])}, which is "
                                     "already on the page. Replace that one instead.")
    ov["exclude_items"] -= {choice["id"]}
    ov["include_items"] |= {choice["id"]}
    job = _rebuild(run_id, m, ov, {"kind": "item", "action": "replace", "out": body.out, "in": choice["id"],
                                   "chosen": "auto" if want == "any" else "you"})
    return job | {"replaced_with": choice}


# ---------------------------------------------------------------- batch edits (preview before download)

@app.get("/api/run/{run_id}/edit-options")
def edit_options(run_id: str):
    """Everything the preview editor needs in one call: per section, the items on the page with their
    printed bullets and spare bullets (unused ones of that item), plus unused items that could come in."""
    return _editor_options(_meta(run_id), _bank_or_400())


def _editor_options(m: dict, bank: dict) -> dict:
    items = _item_options(m, bank)
    spare = _swap_options(m, bank)
    printed: dict[str, list[dict]] = {}
    for p in m.get("provenance") or []:
        printed.setdefault(p["item_id"], []).append(
            {"id": p["bullet_id"], "text": p.get("text") if p.get("rephrased") else p["source"]})
    for sec in items.values():
        for it in sec["on_page"]:
            it["printed"] = printed.get(it["id"], [])
            it["spare"] = spare.get(it["id"], [])
    return items


class ItemChange(BaseModel):
    out: str = Field(max_length=120)
    in_: str | None = Field(None, alias="in", max_length=120)   # item id, "any", or None to remove


class BulletChange(BaseModel):
    out: str = Field(max_length=120)
    in_: str | None = Field(None, alias="in", max_length=120)   # bullet id of the same item, or None to remove


class EditBody(BaseModel):
    items: list[ItemChange] = Field(default_factory=list, max_length=20)
    bullets: list[BulletChange] = Field(default_factory=list, max_length=40)


@app.post("/api/run/{run_id}/edit")
def edit_run(run_id: str, body: EditBody):
    """Apply several changes at once and rebuild once: remove or replace any number of experiences or
    projects ("any" = best unused fit for the posting, kept distinct within the batch) and remove or
    swap any number of bullets inside items that stay. Earlier changes carry over. Logs one pick."""
    m = _meta(run_id)
    if not m.get("jd"):
        raise HTTPException(400, "This run has no saved job description.")
    if not body.items and not body.bullets:
        raise HTTPException(400, "No changes to apply.")
    bank = _bank_or_400()
    ov, changes = _apply_changes(m, bank, body)
    job = _rebuild(run_id, m, ov, {"kind": "batch", "changes": changes})
    return job | {"changes": changes}


def _apply_changes(m: dict, bank: dict, body: "EditBody") -> tuple[dict[str, set], list[dict]]:
    """Check item and bullet changes against what's on the page (a run or a draft) and fold them into its
    overrides. Raises HTTPException with a plain reason when something can't be done."""
    opts = _item_options(m, bank)
    section_of = {i["id"]: s for s, v in opts.items() for i in v["on_page"]}
    outs = [c.out for c in body.items]
    if len(set(outs)) != len(outs):
        raise HTTPException(400, "Each experience or project can only be changed once per batch.")
    for o in outs:
        if o not in section_of:
            raise HTTPException(400, f"{o} isn't on this resume.")
    ov = _overrides(m)
    changes, taken = [], set()
    # named picks first, so "any" never takes something you asked for by name
    for c in sorted(body.items, key=lambda c: (c.in_ or "") in ("", "any")):
        want, sec = (c.in_ or "").strip(), section_of[c.out]
        ov["exclude_items"] |= {c.out}
        ov["include_items"] -= {c.out}
        if not want:
            changes.append({"kind": "item", "action": "remove", "out": c.out, "in": None})
            continue
        usable = [i for i in opts[sec]["unused"] if not i["blocked"] and i["id"] not in taken
                  and not set(i["overlaps"]) - set(outs)]
        if want == "any":
            if not usable:
                raise HTTPException(409, f"Nothing else in {sec} can take the place of {c.out}. Remove it instead.")
            choice, chosen = _rank_unused(m, bank, usable)[0], "auto"
        else:
            choice, chosen = next((i for i in usable if i["id"] == want), None), "you"
            if not choice:
                raise HTTPException(409, f"{want} can't replace {c.out}. It must be an unused, printable item from "
                                         "the same section, not already picked in this batch, and not the same work "
                                         "as something staying on the page.")
        taken.add(choice["id"])
        ov["exclude_items"] -= {choice["id"]}
        ov["include_items"] |= {choice["id"]}
        changes.append({"kind": "item", "action": "replace", "out": c.out, "in": choice["id"],
                        "chosen": chosen, "title": choice["title"]})
    if body.bullets:
        printed = _printed(m)
        spare = _swap_options(m, bank)
        b_outs = [b.out for b in body.bullets]
        if len(set(b_outs)) != len(b_outs):
            raise HTTPException(400, "Each bullet can only be changed once per batch.")
        used: set[str] = set()
        for b in body.bullets:
            item = printed.get(b.out)
            if not item:
                raise HTTPException(400, f"Bullet {b.out} isn't on this resume.")
            if item in outs:
                raise HTTPException(409, f"Bullet {b.out} belongs to an item you're removing or replacing.")
            ov["exclude"] |= {b.out}
            ov["include"] -= {b.out}
            if not b.in_:
                changes.append({"kind": "bullet", "action": "remove", "out": b.out, "in": None})
                continue
            choice = next((o for o in spare.get(item, []) if o["id"] == b.in_), None)
            if not choice or choice["blocked"] or b.in_ in used or choice["same_as"] not in (None, b.out):
                raise HTTPException(409, f"{b.in_} can't replace {b.out}. Pick an unused, printable bullet of the "
                                         "same item, once, that isn't another wording of a different printed bullet.")
            used.add(b.in_)
            ov["exclude"] -= {b.in_}
            ov["include"] |= {b.in_}
            changes.append({"kind": "bullet", "action": "replace", "out": b.out, "in": b.in_})
    return ov, changes


# ---------------------------------------------------------------- drafts: preview and edit before compiling

def _draft_or_404(draft_id: str) -> dict:
    d = drafts.load(OUTPUT_DIR, draft_id)
    if not d:
        raise HTTPException(404, "That preview no longer exists. Make the resume again.")
    return d


def _draft_payload(d: dict, bank: dict) -> dict:
    return {"id": d["id"], "rev": d["rev"], "company": d["company"], "role": d["role"], "variant": d["variant"],
            "mode": d["mode"], "preview": d["preview"], "options": _editor_options(d, bank),
            "changed": bool(d.get("changes")), "built": d.get("built"), "stats": d.get("stats") or {}}


@app.get("/api/drafts/{draft_id}")
def get_draft(draft_id: str):
    return _draft_payload(_draft_or_404(draft_id), _bank_or_400())


class DraftEdit(EditBody):
    order: dict[str, list[str]] | None = None             # {"experiences": [ids], "projects": [ids]}
    bullet_order: dict[str, list[str]] | None = None      # {item id: [bullet ids]}
    texts: dict[str, str | None] | None = None            # {bullet id: your wording, or null to undo}


@app.post("/api/drafts/{draft_id}/edit")
def edit_draft(draft_id: str, body: DraftEdit):
    """Change a draft and re-plan it right away (no model call, no compile). Returns the new preview."""
    d = _draft_or_404(draft_id)
    bank = _bank_or_400()
    changes = []
    if body.items or body.bullets:
        ov, changes = _apply_changes(d, bank, body)
        d["overrides"] = {k: sorted(v) for k, v in ov.items()}
    printed = {p["bullet_id"] for p in d["provenance"]}
    if body.texts:
        for bid, text in body.texts.items():
            text = drafts.clean_text(text)
            if bid not in printed and bid not in d["texts"]:
                raise HTTPException(400, f"Bullet {bid} isn't on this resume.")
            if text and text != next((p["source"] for p in d["provenance"] if p["bullet_id"] == bid), None):
                d["texts"][bid] = text
                changes.append({"kind": "text", "action": "edit", "out": bid, "in": bid})
            else:
                d["texts"].pop(bid, None)
    if body.order is not None:
        for sec in ("experiences", "projects"):
            if sec in body.order:
                d["order"][sec] = [str(i)[:120] for i in body.order[sec]][:30]
        changes.append({"kind": "order", "action": "items"})
    if body.bullet_order is not None:
        d["order"].setdefault("bullets", {}).update({str(k)[:120]: [str(b)[:120] for b in v][:20]
                                                     for k, v in body.bullet_order.items()})
        changes.append({"kind": "order", "action": "bullets"})
    if not changes:
        raise HTTPException(400, "No changes to apply.")
    for b in d["overrides"].get("exclude", []):   # wording for a bullet you removed is dropped too
        d["texts"].pop(b, None)
    prep = drafts.prepare(bank, d)
    drafts.refresh(bank, d, prep)
    d["rev"] += 1
    d["changes"] = (d.get("changes") or []) + changes
    drafts.save(OUTPUT_DIR, d)
    return _draft_payload(d, bank)


@app.post("/api/drafts/{draft_id}/reset")
def reset_draft(draft_id: str):
    """Undo every change: back to the plan you started from (still no model call)."""
    d = _draft_or_404(draft_id)
    bank = _bank_or_400()
    d["overrides"] = {k: list(d["initial"]["overrides"].get(k) or []) for k in drafts.KEYS}
    d["order"], d["texts"], d["changes"] = {}, {}, []
    prep = drafts.prepare(bank, d)
    drafts.refresh(bank, d, prep)
    d["rev"] += 1
    drafts.save(OUTPUT_DIR, d)
    return _draft_payload(d, bank)


@app.post("/api/drafts/{draft_id}/build")
def build_draft(draft_id: str):
    """Compile the draft as it is now and fit it to one page (one LaTeX build, no model call)."""
    d = _draft_or_404(draft_id)
    _bank_or_400()
    job = store.start({"mode": "build", "draft_id": d["id"], "rev": d["rev"], "jd": d["jd"], "company": d["company"],
                       "role": d["role"], "url": d.get("url", ""), "variant": d.get("variant") or "claude",
                       "force_text": True, "source": "draft"})
    return job.public()


@app.get("/api/pdf/{run_id}")
def get_pdf(run_id: str, download: bool = False):
    d = _run_dir(run_id)
    meta = _meta(run_id)
    f = (d / meta["filename"]).resolve()
    if d not in f.parents or not f.exists():
        raise HTTPException(404, "Not found")
    return FileResponse(f, media_type="application/pdf", filename=meta["filename"],
                        content_disposition_type="attachment" if download else "inline")


@app.get("/api/overleaf/{run_id}", response_class=HTMLResponse)
def open_in_overleaf(run_id: str):
    """Opens this resume as a new Overleaf project (Overleaf's official 'Open in Overleaf' form)."""
    d = _run_dir(run_id)
    meta = _meta(run_id)
    tex = (d / meta["filename"]).with_suffix(".tex").read_text(encoding="utf-8")
    tpl = meta.get("template") or meta.get("selection", {}).get("template", "jakes")
    engine = "pdflatex"
    tj = render.TEMPLATE_DIR / tpl / "template.json"
    if tj.exists():
        engine = json.loads(tj.read_text(encoding="utf-8")).get("overleaf_engine", engine)
    data = base64.b64encode(render.overleaf_zip(tex, tpl)).decode()
    name = html.escape(meta["filename"].removesuffix(".pdf"))
    return f"""<!doctype html><meta charset="utf-8"><title>Opening in Overleaf</title>
<body style="font:15px system-ui;padding:40px;color:#18202E">
<form id="f" action="https://www.overleaf.com/docs" method="post">
  <input type="hidden" name="snip_uri" value="data:application/zip;base64,{data}">
  <input type="hidden" name="engine" value="{html.escape(engine)}">
  <input type="hidden" name="main_document" value="resume.tex">
  <p>Opening <b>{name}</b> in Overleaf as a new project…</p>
  <button type="submit">Open in Overleaf</button>
</form>
<script>document.getElementById("f").submit()</script>"""


@app.get("/api/history")
def history(limit: int = 100):
    runs = []
    if OUTPUT_DIR.exists():
        for d in sorted(OUTPUT_DIR.iterdir(), reverse=True):
            f = d / "run.json"
            if d.is_dir() and f.exists():
                try:
                    m = json.loads(f.read_text(encoding="utf-8"))
                except Exception:
                    continue
                sc = m.get("scorecard") or {}
                runs.append({k: m.get(k) for k in ("id", "filename", "company", "role", "url", "mode", "created")}
                            | {"coverage": sc.get("supported_coverage"), "one_page": sc.get("one_page"),
                               "compiles": (m.get("stats") or {}).get("compiles"),
                               "seconds": (m.get("stats") or {}).get("seconds")})
            if len(runs) >= limit:
                break
    return runs


# ---------------------------------------------------------------- web page

@app.get("/", response_class=HTMLResponse)
@app.get("/index.html", response_class=HTMLResponse)
def index():
    page = (ROOT / "ui" / "index.html").read_text(encoding="utf-8")
    return page.replace("__RT_TOKEN__", TOKEN)


app.mount("/", StaticFiles(directory=ROOT / "ui", html=False), name="ui")


def already_running(port: int) -> bool:
    import urllib.request
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/health", timeout=1.5) as r:
            return b'"ok":true' in r.read()
    except Exception:
        return False


if __name__ == "__main__":
    import uvicorn
    import webbrowser

    url = f"http://127.0.0.1:{PORT}"
    headless = sys.stdout is None  # started hidden at login (pythonw)
    if headless:
        OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        log = open(OUTPUT_DIR / "server.log", "a", encoding="utf-8", buffering=1)
        sys.stdout = sys.stderr = log

    if already_running(PORT):
        print(f"Resume Tailor is already running at {url}")
        if "--open" in sys.argv:
            webbrowser.open(url)
        sys.exit(0)

    print(f"Resume Tailor running at {url}  (keep this window open, Ctrl+C to stop)")
    if "--open" in sys.argv:
        import threading
        threading.Timer(1.5, lambda: webbrowser.open(url)).start()
    uvicorn.run(app, host="127.0.0.1", port=PORT, log_config=None if headless else uvicorn.config.LOGGING_CONFIG)
