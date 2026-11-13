"""LaTeX rendering and compilation. Pure rendering: what goes on the page is decided elsewhere.

render_source(bank, plan, layout, texts) -> LaTeX source
compile_pdf(source, template) -> PDF bytes (cached by source hash)

All bank text is escaped (or, with latex_bullets, passed through the safe-LaTeX filter) so
nothing in the bank or from a model can inject LaTeX commands.
"""
from __future__ import annotations

import hashlib
import io
import json
import re
import shutil
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

import jinja2

import bank as bankmod

ROOT = Path(__file__).resolve().parent.parent
TEMPLATE_DIR = ROOT / "templates"

_JINJA = dict(
    block_start_string=r"\BLOCK{", block_end_string="}",
    variable_start_string=r"\VAR{", variable_end_string="}",
    comment_start_string=r"\#{", comment_end_string="}",
    trim_blocks=True, lstrip_blocks=True, autoescape=False,
    undefined=jinja2.StrictUndefined,
)
_SKIP = {".j2", ".md"}
DEFAULT_PAGE = {"width_pt": 612, "height_pt": 792, "min_font_pt": 8.5, "target_bottom_gap_pt": [18, 56]}


def tex(value) -> str:
    return bankmod.escape(value)


def url(value) -> str:
    """Escape only what breaks inside \\href{}; reject anything that isn't http(s)/mailto."""
    v = str(value or "")
    if v and not re.match(r"^(https?://|mailto:)", v):
        return ""
    return v.replace("\\", "").replace("{", "").replace("}", "").replace("%", r"\%").replace("#", r"\#")


def template_info(bank: dict) -> dict:
    name = str(bank.get("settings", {}).get("template") or "default")
    folder = (TEMPLATE_DIR / name).resolve()
    if TEMPLATE_DIR.resolve() not in folder.parents or not folder.is_dir():
        raise RuntimeError(f"Template folder templates/{name} not found")
    meta = {}
    if (folder / "template.json").exists():
        meta = json.loads((folder / "template.json").read_text(encoding="utf-8"))
    main = meta.get("main") or next((p.name for p in sorted(folder.glob("*.j2"))), None)
    if not main or not (folder / main).exists():
        raise RuntimeError(f"templates/{name} has no .j2 main file")
    env = jinja2.Environment(loader=jinja2.FileSystemLoader(folder), **_JINJA)
    digest = hashlib.sha256()
    for p in sorted(folder.rglob("*")):
        if p.is_file():
            digest.update(p.name.encode())
            digest.update(p.read_bytes())
    return {"name": name, "folder": folder, "main": main, "env": env,
            "overleaf_engine": meta.get("overleaf_engine", "pdflatex"),
            "layout": meta.get("layout", {}), "page": {**DEFAULT_PAGE, **meta.get("page", {})},
            "version": digest.hexdigest()[:10]}


def default_layout(tpl: dict) -> dict:
    return {k: v["default"] for k, v in tpl["layout"].items()}


def template_assets(folder: Path):
    for p in sorted(folder.rglob("*")):
        if p.is_file() and p.suffix not in _SKIP and p.name != "template.json":
            yield p.relative_to(folder), p


def list_templates() -> list[str]:
    return sorted(d.name for d in TEMPLATE_DIR.iterdir() if d.is_dir() and any(d.glob("*.j2")))


_XETEX_SHIM = (
    "\\ifdefined\\pdfgentounicode\\else\\newcount\\pdfgentounicode\\fi\n"
    "\\ifdefined\\pdfglyphtounicode\\else\\def\\pdfglyphtounicode#1#2{}\\fi\n"
)


def build_context(bank: dict, plan: dict, layout: dict | None = None, texts: dict[str, str] | None = None) -> dict:
    """plan: {experiences: [{id, bullet_ids}], projects: [...], skills: [...]} in print order.
    texts: optional {bullet_id: plain-text rewording} (already fact-checked) to print instead."""
    prof = bank["profile"]
    latex = bool(bank.get("settings", {}).get("latex_bullets"))
    texts = texts or {}
    p = {k: tex(v) for k, v in prof.items() if isinstance(v, str)}
    p["email_raw"] = url("mailto:" + prof.get("email", "")).removeprefix("mailto:") if prof.get("email") else ""
    p["links"] = [{"label": tex(l["label"]), "url_raw": url(l["url"]),
                   "icon": "\\" + l["icon"] if re.fullmatch(r"fa[A-Za-z]+", str(l.get("icon", ""))) else ""}
                  for l in prof.get("links", [])]

    def fmt(bullet_id: str | None, text: str) -> str:
        if bullet_id and bullet_id in texts:
            return rephrased_latex(texts[bullet_id])
        return bankmod.to_latex(text, latex)

    def expand(key):
        lookup = {x["id"]: x for x in bank.get(key, [])}
        out = []
        for entry in plan.get(key, []):
            item = lookup[entry["id"]]
            btext = {b["id"]: b["text"] for b in item["bullets"]}
            out.append({
                "title": tex(item.get("title")), "org": tex(item.get("org")),
                "location": tex(item.get("location")), "dates": tex(item.get("dates")),
                "stack": tex(item.get("stack")), "url": url(item.get("url")),
                "bullets": [fmt(b, btext[b]) for b in entry["bullet_ids"]],
            })
        return out

    return {
        "p": p,
        "education": [{**{k: tex(v) for k, v in e.items() if k != "bullets" and isinstance(v, str)},
                       "bullets": [fmt(None, b) for b in e.get("bullets", [])]} for e in bank.get("education", [])],
        "experiences": expand("experiences"),
        "projects": expand("projects"),
        "skills": [{"category": tex(s["category"]), "list": tex(", ".join(s["items"]))} for s in plan.get("skills", [])],
        "layout": layout or {},
    }


def rephrased_latex(text: str) -> str:
    """Model rewording is plain text with **bold** marks; escape it fully, then add bold."""
    parts = re.split(r"\*\*(.+?)\*\*", text)
    out = []
    for i, part in enumerate(parts):
        out.append(f"\\textbf{{{tex(part)}}}" if i % 2 else tex(part))
    return "".join(out)


def render_source(bank: dict, plan: dict, layout: dict | None = None, texts: dict | None = None, tpl: dict | None = None) -> str:
    tpl = tpl or template_info(bank)
    lay = {**default_layout(tpl), **(layout or {})}
    return tpl["env"].get_template(tpl["main"]).render(**build_context(bank, plan, lay, texts))


def find_compiler() -> list[str] | None:
    local = ROOT / "bin" / ("tectonic.exe" if sys.platform == "win32" else "tectonic")
    if local.exists():
        return [str(local), "--keep-logs", "--chatter", "minimal"]
    if shutil.which("tectonic"):
        return ["tectonic", "--keep-logs", "--chatter", "minimal"]
    if shutil.which("latexmk"):
        return ["latexmk", "-pdf", "-interaction=nonstopmode", "-halt-on-error", "-quiet", "-no-shell-escape"]
    if shutil.which("pdflatex"):
        return ["pdflatex", "-interaction=nonstopmode", "-halt-on-error", "-no-shell-escape"]
    return None


class CompileError(RuntimeError):
    def __init__(self, msg: str, log: str = ""):
        super().__init__(msg)
        self.log = log


def _explain(log: str) -> str:
    """Turn a LaTeX log into one plain sentence."""
    if re.search(r"File `([^']+)' not found", log):
        f = re.search(r"File `([^']+)' not found", log).group(1)
        return f"The template needs {f}, which isn't installed. Tectonic downloads packages on first use: check your internet."
    if "Undefined control sequence" in log:
        m = re.search(r"Undefined control sequence.*?\n.*?(\\[A-Za-z]+)", log, re.S)
        return f"The template uses an unknown LaTeX command{' ' + m.group(1) if m else ''}."
    if "Missing $ inserted" in log:
        return "A bullet has a stray $ or math character. Use \\$ for a dollar sign."
    if re.search(r"Runaway argument|File ended while scanning", log):
        return "A bullet has an unbalanced { or }."
    return "LaTeX couldn't compile the resume."


_compile_cache: dict[str, bytes] = {}


def compile_pdf(source: str, tpl: dict, timeout: int = 180) -> bytes:
    key = hashlib.sha256((tpl["version"] + source).encode()).hexdigest()
    if key in _compile_cache:
        return _compile_cache[key]
    cmd = find_compiler()
    if not cmd:
        raise CompileError("No LaTeX compiler found. Run setup.bat (Windows) or install Tectonic.")
    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp)
        for rel, src in template_assets(tpl["folder"]):
            (work / rel).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy(src, work / rel)
        src_text = (_XETEX_SHIM + source) if "tectonic" in Path(cmd[0]).name.lower() else source
        (work / "resume.tex").write_text(src_text, encoding="utf-8")
        flags = 0x08000000 if sys.platform == "win32" else 0
        try:
            proc = subprocess.run(cmd + ["resume.tex"], cwd=work, capture_output=True, text=True, encoding="utf-8",
                                  errors="replace", timeout=timeout, creationflags=flags)
        except subprocess.TimeoutExpired:
            raise CompileError(f"LaTeX took longer than {timeout}s.")
        pdf = work / "resume.pdf"
        if proc.returncode != 0 or not pdf.exists():
            log_file = work / "resume.log"
            log = log_file.read_text(encoding="utf-8", errors="ignore") if log_file.exists() else proc.stdout + proc.stderr
            raise CompileError(_explain(log + proc.stderr), log[-3000:])
        data = pdf.read_bytes()
    if len(_compile_cache) > 64:
        _compile_cache.clear()
    _compile_cache[key] = data
    return data


def overleaf_zip(tex_source: str, template_name: str) -> bytes:
    folder = TEMPLATE_DIR / template_name
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("resume.tex", tex_source)
        if folder.is_dir():
            for rel, src in template_assets(folder):
                z.write(src, str(rel).replace("\\", "/"))
    return buf.getvalue()
