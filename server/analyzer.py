"""Deterministic job analysis: raw posting text -> structured, scored requirements.

No LLM here. Every score comes from visible rules so results are explainable:
  * the posting is split into sections (responsibilities / required / preferred / about / benefits)
    from headings and cue phrases,
  * skills are found with the lexicon (aliases included),
  * each requirement's importance = section weight + mandatory-language bonus
    + repetition + early-position bonus + title bonus,
  * the role is classified from title and skill signals (hybrid roles allowed).
"""
from __future__ import annotations

import hashlib
import math
import re
from collections import Counter, defaultdict

from pydantic import BaseModel

import lexicon

SECTION_WEIGHT = {"required": 3.0, "responsibilities": 2.5, "general": 1.6, "preferred": 1.5, "about": 0.6, "benefits": 0.0}

HEADINGS = [
    ("benefits", r"benefits|perks|what we offer|compensation|salary|pay range|why (join|work)|equal (employment )?opportunit|eeo|accommodation|diversity|our values|life at"),
    ("preferred", r"nice[- ]to[- ]haves?|preferred|bonus( points)?|pluses?|it'?s a plus|good to have|desired|extra credit|ideally"),
    ("required", r"requirements?|qualifications?|must[- ]haves?|what you('ll)? (need|bring)|who you are|you (have|bring|should have)|minimum|basic qualifications|skills( required)?|required|what we('re)? look(ing)? for|you might be a fit"),
    ("responsibilities", r"responsibilit|what you('ll)? (do|work on|be doing)|the role|your role|in this role|day[- ]to[- ]day|you will|what you will|duties|the job|key accountabilities|your impact|projects you"),
    ("about", r"about (us|the company|the team|[A-Z])|who we are|our (mission|team|company)|company overview|the team"),
]
LINE_CUES = [
    ("preferred", r"\b(nice[- ]to[- ]haves?|preferred|is a plus|are a plus|a plus\b|bonus|ideally|familiarity with|exposure to|desirable)\b"),
    ("required", r"\b(must|required|requires|minimum|at least|proficien|strong (knowledge|experience|understanding)|solid|demonstrated|expert)\b"),
]
MANDATORY_RE = re.compile(r"\b(must|required|requires|minimum|need to|essential|mandatory)\b", re.I)

ROLE_TITLE_HINTS = {
    "ml": r"machine learning|\bml\b|data scien|deep learning|ai/ml|mlops",
    "ai": r"\bai\b|llm|generative|genai|applied ai|nlp|language model|agent",
    "cv": r"computer vision|perception|vision|imaging",
    "robotics": r"robot|autonom|drone|uav",
    "embedded": r"embedded|firmware|electrical|microcontroller|iot",
    "hardware": r"hardware|asic|fpga|rtl|silicon|chip|verification|design verification|accelerat",
    "backend": r"back[- ]?end|infrastructure|platform|devops|site reliability|\bsre\b|cloud|distributed",
    "frontend": r"front[- ]?end|full[- ]?stack|web|ui engineer",
    "mobile": r"mobile|ios|android",
    "data": r"data engineer|analytics|data analyst|business intelligence",
    "systems": r"systems|kernel|operating system|performance engineer|compiler",
    "swe": r"software|developer|engineer|programmer|swe",
}
SENIORITY = [("intern", r"\bintern(ship)?\b|co-?op|student|summer 20\d\d|fall 20\d\d|winter 20\d\d"),
             ("new grad", r"new grad|graduate|entry[- ]level|early career|university grad"),
             ("junior", r"junior|\bjr\.?\b|associate"),
             ("senior", r"senior|\bsr\.?\b|staff|principal|lead\b")]


class Requirement(BaseModel):
    skill: str
    kind: str
    importance: float
    section: str           # strongest section it appeared in
    mandatory: bool
    count: int
    roles: list[str]


class JobAnalysis(BaseModel):
    title: str
    company: str
    seniority: str
    years_required: int | None
    roles: dict[str, float]          # category -> normalised score (1.0 = strongest)
    primary_roles: list[str]
    requirements: list[Requirement]  # sorted by importance, highest first
    keywords: dict[str, float]       # other important words (domain terms), weighted
    sections: dict[str, int]         # lines per section
    quality: dict                    # extraction quality checks
    text_hash: str
    focus_text: str                  # responsibilities + requirements only, for prompts

    def req(self, skill: str) -> Requirement | None:
        return next((r for r in self.requirements if r.skill == skill), None)


def _split_lines(text: str) -> list[str]:
    text = (text or "").replace("\r", "\n")
    text = re.sub(r"[ \t]+", " ", text)
    lines = []
    for raw in text.split("\n"):
        line = raw.strip(" -•*·–—\t")
        if not line:
            continue
        # split long run-on lines into sentences so section cues stay local
        if len(line) > 300:
            lines.extend(s.strip() for s in re.split(r"(?<=[.;])\s+(?=[A-Z])", line) if s.strip())
        else:
            lines.append(line)
    return lines


def _heading_section(line: str) -> str | None:
    """If a line looks like a heading, return the section it starts."""
    short = len(line) <= 70 and (line.endswith(":") or len(line.split()) <= 8) and not line.endswith(".")
    if not short:
        return None
    low = line.lower().rstrip(":")
    for sec, pat in HEADINGS:
        if re.search(pat, low):
            return sec
    return None


def sectionize(text: str) -> list[tuple[str, str]]:
    """[(section, line)] for every line of the posting."""
    out, current = [], "general"
    for line in _split_lines(text):
        h = _heading_section(line)
        if h:
            current = h
            out.append((current, line))
            continue
        # inline heading: "Nice to have: Tailwind, Supabase" -> this line belongs to that section
        m = re.match(r"^([^:]{3,45}):\s+\S", line)
        if m and _heading_section(m.group(1) + ":"):
            out.append((_heading_section(m.group(1) + ":"), line))
            continue
        sec = current
        if current in ("general", "required", "responsibilities"):
            for cue_sec, pat in LINE_CUES:
                if re.search(pat, line, re.I):
                    if cue_sec == "preferred":
                        sec = "preferred"
                        break
                    if cue_sec == "required" and current == "general":
                        sec = "required"
        out.append((sec, line))
    return out


def classify_roles(title: str, req_skills: dict[str, float]) -> dict[str, float]:
    scores: dict[str, float] = defaultdict(float)
    t = (title or "").lower()
    for role, pat in ROLE_TITLE_HINTS.items():
        if re.search(pat, t):
            scores[role] += 6.0 if role != "swe" else 2.0
    for skill, w in req_skills.items():
        for role in lexicon.roles_for(skill):
            scores[role] += w
    if not scores:
        return {"swe": 1.0}
    top = max(scores.values())
    return {r: round(v / top, 3) for r, v in sorted(scores.items(), key=lambda kv: -kv[1]) if v / top >= 0.15}


def quality_checks(text: str, lines: list[tuple[str, str]], reqs: list[Requirement], title: str) -> dict:
    words = len(re.findall(r"\w+", text or ""))
    n = max(1, len(lines))
    short = sum(1 for _, l in lines if len(l.split()) <= 3) / n
    dup = 1 - len({l.lower() for _, l in lines}) / n
    has_req = any(s in ("required", "responsibilities", "preferred") for s, _ in lines)
    cues = len(re.findall(r"\b(experience|responsib|qualif|requir|you will|we are looking|skills?)\b", (text or "").lower()))
    checks = {
        "words": words,
        "enough_text": words >= 80,
        "not_too_long": words <= 6000,
        "has_title": bool(title),
        "has_requirement_signals": has_req or cues >= 3,
        "has_skills": len(reqs) >= 1,
        "nav_like_ratio": round(short, 2),
        "duplicate_ratio": round(dup, 2),
    }
    score = 0.0
    score += 0.3 if checks["enough_text"] else 0.3 * min(1, words / 80)
    score += 0.25 if checks["has_requirement_signals"] else 0
    score += 0.2 if checks["has_skills"] else 0
    score += 0.1 if checks["has_title"] else 0
    score += 0.15 * (1 - min(1, max(short - 0.25, 0) * 2))
    score -= 0.2 if dup > 0.3 else 0
    score -= 0.2 if not checks["not_too_long"] else 0
    checks["confidence"] = round(max(0.0, min(1.0, score)), 2)
    issues = []
    if not checks["enough_text"]:
        issues.append("The posting text is very short. It may not have loaded fully.")
    if not checks["has_requirement_signals"]:
        issues.append("No responsibilities or requirements found. This may not be the job description.")
    if not checks["has_skills"]:
        issues.append("No technical skills found in the posting.")
    if short > 0.5:
        issues.append("Lots of menu-like short lines. The page text may include navigation.")
    if dup > 0.3:
        issues.append("Lots of repeated lines.")
    checks["issues"] = issues
    return checks


def analyze(text: str, title: str = "", company: str = "") -> JobAnalysis:
    lines = sectionize(text)
    total = max(1, len(lines))
    per_skill: dict[str, dict] = {}
    for idx, (sec, line) in enumerate(lines):
        if sec == "benefits":
            continue
        found = lexicon.find_skills(line)
        mandatory = bool(MANDATORY_RE.search(line)) and sec != "preferred"
        for skill, n in found.items():
            d = per_skill.setdefault(skill, {"count": 0, "best": 0.0, "section": sec, "mandatory": False, "first": idx})
            d["count"] += n
            w = SECTION_WEIGHT[sec]
            if w > d["best"]:
                d["best"], d["section"] = w, sec
            d["mandatory"] |= mandatory
    t_low = (title or "").lower()
    title_skills = lexicon.find_skills(title or "")
    reqs = []
    for skill, d in per_skill.items():
        imp = d["best"]
        imp += 1.0 if d["mandatory"] else 0.0
        imp += 0.6 * math.log1p(d["count"] - 1) if d["count"] > 1 else 0.0
        imp += 0.4 * (1 - d["first"] / total)          # earlier mentions matter a bit more
        imp += 2.0 if skill in title_skills else 0.0
        if lexicon.kind(skill) == "concept":
            imp *= 0.8                                    # broad concepts weigh less than named tools
        reqs.append(Requirement(skill=skill, kind=lexicon.kind(skill), importance=round(imp, 2),
                                section=d["section"], mandatory=d["mandatory"], count=d["count"],
                                roles=lexicon.roles_for(skill)))
    reqs.sort(key=lambda r: -r.importance)

    # Domain keywords that aren't in the skills dictionary (e.g. "fintech", "payments", "autonomy")
    kw = Counter()
    skill_words = {w for r in reqs for w in lexicon.content_words(r.skill)}
    for sec, line in lines:
        w = SECTION_WEIGHT[sec]
        if w <= 0.6:
            continue
        for word in lexicon.content_words(line):
            if word not in skill_words and not word.isdigit():
                kw[word] += w
    keywords = {k: round(v, 2) for k, v in kw.most_common(40) if v >= 3.0}

    roles = classify_roles(title or " ".join(l for _, l in lines[:3]), {r.skill: r.importance for r in reqs})
    full = " ".join([title or ""] + [l for _, l in lines[:12]]).lower()
    seniority = next((name for name, pat in SENIORITY if re.search(pat, full)), "unspecified")
    yrs = re.search(r"(\d+)\+?\s*(?:-\s*\d+\s*)?years?", text or "", re.I)
    focus = "\n".join(l for s, l in lines if s in ("responsibilities", "required", "preferred", "general"))[:6000]
    sections = Counter(s for s, _ in lines)
    return JobAnalysis(
        title=title or "", company=company or "", seniority=seniority,
        years_required=int(yrs.group(1)) if yrs and int(yrs.group(1)) < 20 else None,
        roles=roles, primary_roles=[r for r, v in roles.items() if v >= 0.5][:3] or ["swe"],
        requirements=reqs, keywords=keywords, sections=dict(sections),
        quality=quality_checks(text, lines, reqs, title), text_hash=hashlib.sha256((text or "").encode()).hexdigest()[:16],
        focus_text=focus,
    )
