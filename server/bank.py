"""Experience bank: schema, validation, migration, import/export, and safe LaTeX.

The bank is the factual source of truth. Nothing in the pipeline writes to it: AI
rewording lives only in each run's history, with a link back to the bullet it came from.

Storage: data/experiences.yaml (human-editable). Schema version 2 adds `version`, item
`kind`, `pinned`, `hidden`, `notes`, and bullet `skills`/`pinned`/`hidden`/`notes`.
Version 1 files load unchanged and are upgraded in memory (and on the next save).
"""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Literal, Optional, Union

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

import lexicon

SCHEMA_VERSION = 2
ID_RE = r"^[A-Za-z0-9][A-Za-z0-9._-]{0,79}$"
Kind = Literal["work", "internship", "research", "team", "leadership", "volunteer", "project", "school", "other"]


class _Base(BaseModel):
    model_config = ConfigDict(extra="allow", str_strip_whitespace=True)


class Bullet(_Base):
    id: str = Field(pattern=ID_RE)
    text: str = Field(min_length=3, max_length=600)
    alt_of: Optional[str] = None          # another wording of that bullet; never both on a resume
    skills: list[str] = []                # extra skills this shows (besides what the text names)
    pinned: bool = False                  # always include when the item is included
    hidden: bool = False                  # keep in the bank but never use
    notes: str = ""                       # extra verified context (used to ground rewording)
    tags: list[str] = []


class Item(_Base):
    id: str = Field(pattern=ID_RE)
    title: str = Field(min_length=1, max_length=160)
    kind: Kind = "work"
    org: str = ""
    location: str = ""
    dates: str = ""
    stack: str = ""                       # projects: tech line printed after the title
    url: str = ""
    tags: list[str] = []
    overlaps: list[str] = []              # ids of items describing the same work
    pinned: bool = False                  # always include
    hidden: bool = False                  # never include
    notes: str = ""
    bullets: list[Bullet] = Field(min_length=1)

    @field_validator("url")
    @classmethod
    def _url(cls, v: str) -> str:
        if v and not re.match(r"^https?://", v):
            raise ValueError("must start with http:// or https://")
        return v


class Education(_Base):
    school: str
    location: str = ""
    degree: str = ""
    dates: str = ""
    details: str = ""
    bullets: list[str] = []


class SkillGroup(_Base):
    category: str = Field(min_length=1)
    items: list[str] = Field(min_length=1)


class Link(_Base):
    label: str
    url: str
    icon: str = ""

    @field_validator("icon")
    @classmethod
    def _icon(cls, v: str) -> str:
        if v and not re.fullmatch(r"fa[A-Za-z]+", v):
            raise ValueError("must be a Font Awesome name like faLinkedin")
        return v


class Profile(_Base):
    first_name: str = Field(min_length=1)
    last_name: str = Field(min_length=1)
    email: str = ""
    phone: str = ""
    location: str = ""
    links: list[Link] = []


class Settings(_Base):
    template: str = "default"
    latex_bullets: bool = False
    max_experiences: int = Field(3, ge=1, le=8)
    max_projects: int = Field(3, ge=0, le=8)
    max_bullets_per_item: int = Field(4, ge=1, le=8)
    min_bullets_per_item: int = Field(2, ge=1, le=8)
    keep_bank_order: Union[bool, Literal["experiences"]] = True
    rephrase: bool = False                # reword a few picked bullets (fact-checked, line-checked)
    max_rephrase: int = Field(3, ge=0, le=10)          # at most this many rewordings per resume
    min_last_line_fill: float = Field(0.6, ge=0.0, le=1.0)  # a 2-line bullet's last line must be this full
    strict_lines: bool = False            # never print a bullet with a short last line (use a clean wording)
    max_skill_line_chars: int = Field(100, ge=40, le=200)
    filename_pattern: str = "{first}_{last}_{company}_{role}_Resume"


class Bank(_Base):
    version: int = SCHEMA_VERSION
    profile: Profile
    settings: Settings = Settings()
    education: list[Education] = []
    experiences: list[Item] = []
    projects: list[Item] = []
    skills: list[SkillGroup] = []

    @model_validator(mode="after")
    def _ids(self) -> "Bank":
        seen: dict[str, str] = {}
        problems = []
        for section in ("experiences", "projects"):
            for item in getattr(self, section):
                for i in [item.id] + [b.id for b in item.bullets]:
                    if i in seen:
                        problems.append(f"duplicate id '{i}'")
                    seen[i] = section
                bullet_ids = {b.id for b in item.bullets}
                for b in item.bullets:
                    if b.alt_of and b.alt_of not in bullet_ids:
                        problems.append(f"{b.id}: alt_of '{b.alt_of}' isn't a bullet of {item.id}")
        items = {i.id for i in self.experiences + self.projects}
        for item in self.experiences + self.projects:
            for o in item.overlaps:
                if o not in items:
                    problems.append(f"{item.id}: overlaps '{o}' isn't an experience or project")
        if self.settings.min_bullets_per_item > self.settings.max_bullets_per_item:
            problems.append("settings: min_bullets_per_item is larger than max_bullets_per_item")
        if problems:
            raise ValueError("; ".join(problems))
        return self


# ---------------------------------------------------------------- migration

def migrate(raw: dict) -> dict:
    """Upgrade an older bank dict to the current schema (pure function, no I/O)."""
    if not isinstance(raw, dict):
        raise ValueError("The bank must be a YAML mapping with a 'profile' section")
    data = json.loads(json.dumps(raw))  # deep copy, plain types
    v = int(data.get("version") or 1)
    prof = data.get("profile") if isinstance(data.get("profile"), dict) else None
    if prof and prof.get("name") and not (prof.get("first_name") and prof.get("last_name")):
        first, _, last = str(prof["name"]).strip().partition(" ")
        prof.setdefault("first_name", first)
        prof.setdefault("last_name", last or first)
    if v < 2:
        for item in data.get("experiences") or []:
            item.setdefault("kind", _guess_kind(item))
        for item in data.get("projects") or []:
            item.setdefault("kind", "school" if "school" in (item.get("tags") or []) else "project")
        data["version"] = 2
    return data


def _guess_kind(item: dict) -> str:
    title = (item.get("title") or "").lower()
    tags = {t.lower() for t in item.get("tags") or []}
    if "intern" in title:
        return "internship"
    if "research" in title:
        return "research"
    if re.search(r"president|representative|captain|chair|lead\b", title) and "software" not in title:
        return "leadership"
    if "teaching" in tags or "tutor" in title:
        return "volunteer" if "assistant" in title else "work"
    return "work"


# ---------------------------------------------------------------- load / save

def format_errors(e: Exception) -> list[dict]:
    """Pydantic errors -> [{'path': 'experiences.2.bullets.0.text', 'message': ...}]"""
    if isinstance(e, ValidationError):
        out = []
        for err in e.errors():
            path = ".".join(str(p) for p in err["loc"])
            msg = err["msg"].removeprefix("Value error, ")
            out.append({"path": path, "message": msg})
        return out
    return [{"path": "", "message": str(e)}]


def parse(raw: dict) -> Bank:
    return Bank.model_validate(migrate(raw))


def load(path: Path) -> Bank:
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    return parse(raw)


def load_dict(path: Path) -> dict:
    """Validated bank as plain dict (what the rest of the pipeline uses)."""
    return to_dict(load(path))


def to_dict(bank: Bank) -> dict:
    return bank.model_dump(mode="json", exclude_defaults=False)


HEADER = """# Resume Tailor experience bank (schema v{v}). Edit here or on the Experience bank page.
# Bullets: one accomplishment each. alt_of = another wording of a bullet (never both used).
# overlaps = items describing the same work (only one used). pinned = always include.
# With latex_bullets: true, bullets may use \\textbf{{}}, \\emph{{}}, \\%, \\$, \\&, $\\times$.
"""


def dump_yaml(data: dict) -> str:
    """Bank dict -> tidy YAML (defaults dropped so the file stays readable)."""
    clean = _drop_defaults(data)
    body = yaml.safe_dump(clean, sort_keys=False, allow_unicode=True, width=1000, default_flow_style=None)
    return HEADER.format(v=SCHEMA_VERSION) + "\n" + body


_DEFAULTS = {"pinned": False, "hidden": False, "notes": "", "skills": [], "tags": [], "overlaps": [],
             "alt_of": None, "stack": "", "url": "", "details": "", "icon": "", "bullets_edu": []}


def _drop_defaults(obj, key: str = ""):
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            if k in _DEFAULTS and v == _DEFAULTS[k] and k != "bullets":
                continue
            out[k] = _drop_defaults(v, k)
        return out
    if isinstance(obj, list):
        return [_drop_defaults(v, key) for v in obj]
    return obj


def save(path: Path, data: dict) -> Bank:
    """Validate, back up the current file, then write atomically."""
    bank = parse(data)
    text = dump_yaml(to_dict(bank))
    if path.exists():
        path.with_suffix(".backup.yaml").write_text(path.read_text(encoding="utf-8"), encoding="utf-8")
    tmp = path.with_suffix(".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)
    return bank


def save_text(path: Path, text: str) -> Bank:
    """Save raw YAML exactly as typed (keeps comments), after validating it."""
    bank = parse(yaml.safe_load(text))
    if path.exists():
        path.with_suffix(".backup.yaml").write_text(path.read_text(encoding="utf-8"), encoding="utf-8")
    tmp = path.with_suffix(".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)
    return bank


def version_hash(data: dict) -> str:
    """Stable short hash of the bank content (for caching and run provenance)."""
    return hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()[:12]


# ---------------------------------------------------------------- text helpers

def plain(text: str) -> str:
    """LaTeX-formatted bullet -> readable plain text (for matching, prompts and checks)."""
    t = str(text or "")
    for _ in range(3):
        t = re.sub(r"\\(?:textbf|textit|emph|underline|texttt|small)\{([^{}]*)\}", r"\1", t)
    t = (t.replace("$\\times$", "×").replace("$\\sim$", "~").replace("$|$", "|")
         .replace("$\\pm$", "±").replace("$\\nearrow$", "↗"))
    t = t.replace("---", "—").replace("--", "–")
    t = re.sub(r"\\([%$&#_{}])", r"\1", t)
    t = re.sub(r"\$([^$]*)\$", r"\1", t)
    return re.sub(r"\s+", " ", t).strip()


def numbers(text: str) -> set[str]:
    """Normalised numbers in text: '19×' -> '19', '757K+' -> '757k', '65%' -> '65', '8e-7' -> '8e-7'."""
    out = set()
    for m in re.finditer(r"(?<![\w.])\d[\d,]*(?:\.\d+)?(?:[eE]-?\d+)?(?:[kKmM](?![a-zA-Z]))?", plain(text)):
        n = m.group(0).replace(",", "").lower().rstrip(".")
        out.add(n)
    return out


def bullet_skills(bullet: dict, item: dict | None = None) -> set[str]:
    """Canonical skills a bullet demonstrates: named in its text, plus any listed explicitly."""
    found = set(lexicon.find_skills(plain(bullet.get("text", ""))))
    for s in bullet.get("skills") or []:
        found.add(lexicon.canonical(s) or s)
    return found


def item_skills(item: dict) -> set[str]:
    text = " ".join([item.get("title", ""), item.get("stack", ""), " ".join(item.get("tags") or [])])
    found = set(lexicon.find_skills(text.replace("-", " ")))
    for t in item.get("tags") or []:
        c = lexicon.canonical(t.replace("-", " "))
        if c:
            found.add(c)
    return found


# ---------------------------------------------------------------- safe LaTeX

_ALLOWED_CMDS = {"textbf", "textit", "emph", "underline", "texttt"}
_ALLOWED_MATH = {"$\\times$", "$\\sim$", "$|$", "$\\pm$", "$\\nearrow$", "$\\approx$", "$\\rightarrow$", "$\\to$",
                 "$\\geq$", "$\\leq$", "$\\cdot$"}
_LATEX_ESC = {"\\": r"\textbackslash{}", "&": r"\&", "%": r"\%", "$": r"\$", "#": r"\#", "_": r"\_",
              "{": r"\{", "}": r"\}", "~": r"\textasciitilde{}", "^": r"\textasciicircum{}"}


def escape(text: str) -> str:
    """Plain text -> LaTeX with every special character escaped."""
    return "".join(_LATEX_ESC.get(c, c) for c in str(text or ""))


def safe_latex(text: str) -> str:
    """Allow a small formatting subset of LaTeX (bold, italics, escaped symbols, a few math
    symbols); escape everything else. Blocks injection such as \\input, \\write18, \\def or
    unbalanced braces that would break the template."""
    s = str(text or "")
    out, i, depth = [], 0, 0
    while i < len(s):
        c = s[i]
        if c == "\\":
            m = re.match(r"\\([A-Za-z]+)\{", s[i:])
            if m and m.group(1) in _ALLOWED_CMDS:
                out.append(f"\\{m.group(1)}{{")
                depth += 1
                i += len(m.group(0))
                continue
            if i + 1 < len(s) and s[i + 1] in "%$&#_{}":
                out.append(s[i:i + 2])
                i += 2
                continue
            out.append(r"\textbackslash{}")
            i += 1
            continue
        if c == "$":
            for sym in _ALLOWED_MATH:
                if s.startswith(sym, i):
                    out.append(sym)
                    i += len(sym)
                    break
            else:
                out.append(r"\$")
                i += 1
            continue
        if c == "{":
            out.append(r"\{")  # bare braces only open via an allowed command
            i += 1
            continue
        if c == "}":
            if depth > 0:
                out.append("}")
                depth -= 1
            else:
                out.append(r"\}")
            i += 1
            continue
        if c in "&%#_^~":
            out.append(_LATEX_ESC[c])
            i += 1
            continue
        out.append(c)
        i += 1
    out.append("}" * depth)  # close anything left open
    return "".join(out)


def to_latex(text: str, latex_bullets: bool) -> str:
    return safe_latex(text) if latex_bullets else escape(text)


# ---------------------------------------------------------------- import / export

def export_json(data: dict) -> str:
    return json.dumps({"format": "resume-tailor-bank", **data}, indent=2, ensure_ascii=False)


def import_json(text: str) -> dict:
    raw = json.loads(text)
    raw.pop("format", None)
    return to_dict(parse(raw))
