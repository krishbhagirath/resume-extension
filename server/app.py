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
import re
import secrets
import sys
import time
from pathlib import Path

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
import importer  # noqa: E402
import linefit  # noqa: E402
import llm  # noqa: E402
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


@app.post("/api/jobs")
def create_job(req: JobRequest):
    if len(req.jd.strip()) < 50:
        raise HTTPException(400, "The job description looks too short. Paste the full posting.")
    _bank_or_400()
    job = store.start(req.model_dump())
    return job.public()


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
                     force=True, force_text=True)
    return create_job(req)


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
