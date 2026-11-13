"""Turn an existing resume PDF into draft experience-bank records for you to confirm.

Nothing is saved automatically: the web page shows the drafts and you pick what to add.
Bold text is kept as \\textbf{} so your emphasis carries over.
"""
from __future__ import annotations

import io
import re

import pdfplumber

SECTION_RE = {
    "experiences": r"^(work |professional |relevant )?experience|employment|internships?$",
    "projects": r"^(technical |personal |selected )?projects?$",
    "education": r"^education$",
    "skills": r"^(technical )?skills|technologies$",
    "leadership": r"^(leadership|activities|extracurriculars?|volunteer(ing)?)",
}
BULLET_RE = re.compile(r"^\s*[•●◦▪·\-–*]\s*")
DATE_RE = re.compile(r"((jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*\.?\s+)?(19|20)\d\d\s*([-–—]|to)\s*"
                     r"(((jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*\.?\s+)?(19|20)\d\d|present|current)", re.I)


def _lines(pdf_bytes: bytes) -> list[dict]:
    """Lines with text (bold runs wrapped in \\textbf{}), x position and whether the line starts with a bullet."""
    out = []
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        for page in pdf.pages:
            # group characters into lines by vertical centre (bullet glyphs and symbols such as
            # the degree sign sit higher or lower than the text around them)
            rows: list[list] = []
            for ch in sorted(page.chars, key=lambda c: (c["top"] + c["bottom"]) / 2):
                mid = (ch["top"] + ch["bottom"]) / 2
                if rows and abs(mid - rows[-1][0]) <= 3.5:
                    rows[-1][1].append(ch)
                else:
                    rows.append([mid, [ch]])
            for _mid, row in rows:
                chars = sorted(row, key=lambda c: c["x0"])
                text, bold_open, prev = "", False, None
                for c in chars:
                    if prev is not None and c["x0"] - prev["x1"] > c["size"] * 4:
                        text += " \t "  # column gap (right-aligned dates / locations)
                    elif prev is not None and c["x0"] - prev["x1"] > c["size"] * 0.25:
                        text += " "
                    bold = bool(re.search(r"bold|bx|black|heavy", c["fontname"], re.I))
                    if bold != bold_open and c["text"].strip():
                        text += "\\textbf{" if bold else "}"
                        bold_open = bold
                    text += c["text"]
                    prev = c
                if bold_open:
                    text += "}"
                text = re.sub(r"\\textbf\{(\s*)\}", r"\1", text).strip()
                if text:
                    out.append({"text": text, "x": chars[0]["x0"], "bullet": bool(BULLET_RE.match(re.sub(r"\\textbf\{", "", text)))})
    return out


def _plain(t: str) -> str:
    return re.sub(r"\\textbf\{([^}]*)\}", r"\1", t).replace(" \t ", "  ")


def _escape_text(t: str) -> str:
    # escape LaTeX specials in non-command text so drafts are valid latex_bullets text
    t = re.sub(r"(?<!\\)([%$&#_])", r"\\\1", t)
    return t.replace("×", "$\\times$")


def parse_resume(pdf_bytes: bytes) -> dict:
    lines = _lines(pdf_bytes)
    section = None
    drafts: list[dict] = []
    skills: list[dict] = []
    current = None
    for ln in lines:
        plain = _plain(ln["text"]).strip()
        low = plain.lower().strip(": ")
        hit = next((s for s, pat in SECTION_RE.items() if re.match(pat, low) and len(low) < 40), None)
        if hit:
            section, current = hit, None
            continue
        if section in (None, "education"):
            continue
        if section == "skills":
            m = re.match(r"^([^:]{2,40}):\s*(.+)$", plain)
            if m:
                skills.append({"category": m.group(1).strip(), "items": [i.strip() for i in m.group(2).split(",") if i.strip()]})
            continue
        if ln["bullet"]:
            if current is None:
                current = {"section": "projects" if section == "projects" else "experiences", "title": "Untitled",
                           "org": "", "location": "", "dates": "", "stack": "", "bullets": []}
                drafts.append(current)
            current["bullets"].append(_escape_text(BULLET_RE.sub("", ln["text"].replace(" \t ", " ")).replace("\\textbf{• ", "\\textbf{")))
            continue
        if current and current["bullets"] and not ln["bullet"] and ln["x"] > 40 and not DATE_RE.search(plain) \
                and len(plain) < 120 and current["bullets"][-1][-1:] not in ".":
            current["bullets"][-1] += " " + _escape_text(ln["text"])  # wrapped bullet line
            continue
        # a heading line: new item, or the second line of the current one
        date = DATE_RE.search(plain)
        if current is None or current["bullets"]:
            current = {"section": "projects" if section == "projects" else "experiences", "title": "", "org": "", "location": "",
                       "dates": "", "stack": "", "bullets": []}
            drafts.append(current)
            left, _, right = plain.partition("  ")
            head = DATE_RE.sub("", left).strip(" |")
            if right and not DATE_RE.search(right):
                current["location"] = right.strip()
            if "|" in head:
                t, _, stack = head.partition("|")
                current["title"], current["stack"] = t.strip(" ↗"), stack.strip()
            else:
                current["title"] = head
        else:
            left, _, right = plain.partition("  ")
            current["org"] = DATE_RE.sub("", left).strip(" |")
            if right.strip() and not DATE_RE.search(right):
                current["location"] = right.strip()
        if date:
            current["dates"] = date.group(0)
    drafts = [d for d in drafts if d["bullets"] and d["title"]]
    return {"drafts": drafts, "skills": skills}
