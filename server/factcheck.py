"""Deterministic fact checking for reworded bullets.

A rewording is accepted only if everything it states can be traced to the source evidence:
the original bullet, its alternate wordings, and the item's notes/title/org/stack/tags.
Checks (any failure rejects the rewording and the original wording is used instead):
  numbers     every number (with its unit: %, x, ms, s, k...) must appear in the source
  skills      every technology the dictionary knows must appear in the source (catches swaps
              like PyTorch -> TensorFlow)
  entities    capitalised names / acronyms must appear in the source (employers, tools)
  claims      escalations such as led / managed / production / users / millions / award
              must already be in the source
  drift       the rewording must stay about the same thing (word overlap)
  format      plain text with **bold** only; no LaTeX, HTML, links or instructions
  length      fits the line target: one line, or nearly two full lines (never a word or two over)
  xyz         keeps the action -> measurable result -> method shape: starts with a past-tense
              verb, keeps a number, and keeps the "by / using / via ..." part if the original had one
"""
from __future__ import annotations

import re

import bank as bankmod
import lexicon

CLAIMS = {
    "led": r"\b(led|lead|leading|headed|spearhead\w*)\b",
    "managed": r"\b(manag\w+|supervis\w+|oversaw|oversee\w*|direct(ed|ing) (a|the) team)\b",
    "mentored": r"\b(mentor\w*|coach\w*|train(ed|ing) (members|students|engineers))\b",
    "owned": r"\b(own(ed|ership)|sole(ly)?)\b",
    "architected": r"\barchitect\w*\b",
    "production": r"\b(production|prod\b|in prod|live (traffic|users))\b",
    "deployed": r"\b(deploy\w*|shipp\w+|launch\w*|released?)\b",
    "users": r"\b(users?|customers?|clients?|merchants?|students?|members?)\b",
    "scale": r"\b(million|billion|thousands?|at scale|large[- ]scale|enterprise[- ]scale)\b",
    "team": r"\b(team of|\d+[- ](member|person|engineer)s?)\b",
    "award": r"\b(award\w*|won|winner|prize|1st|first place)\b",
    "published": r"\b(publish\w*|paper|patent\w*)\b",
}
_IRREGULAR = {"built", "led", "ran", "wrote", "made", "cut", "won", "drove", "grew", "took", "set", "shipped", "brought",
              "taught", "rebuilt", "began", "chose", "held", "kept", "found", "gave", "got", "spun", "split", "sped",
              "rewrote", "overhauled", "put", "read", "sold", "won", "beat", "bought", "dealt", "drew", "fed", "fought",
              "hit", "lent", "met", "paid", "saw", "sent", "spent", "stood", "struck", "swept", "told", "thought", "underwent"}
_METHOD = re.compile(r"\b(by|using|via|with|through|leveraging|applying|in)\b", re.I)
_ALLOWED_CAPS = {"I", "API", "APIs", "A", "An", "The", "UI", "ML", "AI", "CI", "CD", "PDF", "JSON", "REST"}


def _unit_numbers(text: str) -> set[str]:
    """Numbers with units, normalised: '19×'->'19x', '17\\%'->'17%', '6.8ms'->'6.8ms', '757K+'->'757k'."""
    t = bankmod.plain(text).replace("×", "x")
    out = set()
    for m in re.finditer(r"(?<![\w.])(\d[\d,]*(?:\.\d+)?(?:[eE]-?\d+)?)\s*(%|x\b|ms\b|s\b|[kKmM]\b|[kKmM]\+|hz\b|°)?", t):
        num = m.group(1).replace(",", "")
        unit = (m.group(2) or "").lower().rstrip("+")
        out.add(num + unit)
    return out


def _entities(text: str) -> set[str]:
    """Capitalised words that aren't at a sentence start, and any word with an inner capital or digit."""
    t = bankmod.plain(text)
    out = set()
    for m in re.finditer(r"[A-Za-z][A-Za-z0-9.+#/-]*", t):
        w = m.group(0).strip(".")
        start = m.start() == 0 or re.search(r"[.;:]\s*$", t[:m.start()])
        has_inner = re.search(r"[A-Z].*[A-Z]|\d", w[1:]) is not None
        if (w[:1].isupper() and not start) or has_inner:
            if w not in _ALLOWED_CAPS:
                out.add(w.lower())
    return out


def evidence_text(bullet: dict, item: dict) -> str:
    alts = [b["text"] for b in item.get("bullets", [])
            if (b.get("alt_of") or b["id"]) == (bullet.get("alt_of") or bullet["id"])]
    parts = [bullet["text"], bullet.get("notes", ""), *alts, item.get("title", ""), item.get("org", ""),
             item.get("stack", ""), " ".join(item.get("tags") or []), item.get("notes", "")]
    return " ".join(p for p in parts if p)


def _past_verb(word: str) -> bool:
    w = word.lower().strip(",;:")
    return w.endswith("ed") or w in _IRREGULAR


def check(rewrite: str, bullet: dict, item: dict, limits: dict | None = None) -> tuple[bool, list[str]]:
    """limits: {"one_line": max chars for one line, "two_lines": [min, max] chars for ~2 full lines}."""
    reasons = []
    original = bankmod.plain(bullet["text"])
    src = evidence_text(bullet, item)
    src_plain = bankmod.plain(src)
    # technologies must come from this bullet (its text, wordings, notes, tags), not just the project's stack
    own_plain = bankmod.plain(" ".join(p for p in [bullet["text"], bullet.get("notes", ""), *[
        x["text"] for x in item.get("bullets", []) if (x.get("alt_of") or x["id"]) == (bullet.get("alt_of") or bullet["id"])]] if p))
    new = rewrite.strip()
    new_plain = re.sub(r"\*\*(.+?)\*\*", r"\1", new)

    # format
    if re.search(r"\\[A-Za-z]|<[a-z/]|\]\(|https?://|\n|`", new):
        reasons.append("format: contains LaTeX, HTML, links or line breaks")
    if re.search(r"\b(ignore|disregard) (all|previous|the above)\b|\bas an ai\b|\bsystem prompt\b", new, re.I):
        reasons.append("format: contains instructions")
    if new.count("**") % 2:
        reasons.append("format: unbalanced ** bold marks")

    # numbers with units
    extra_nums = _unit_numbers(new_plain) - _unit_numbers(src_plain)
    bare_src = {re.sub(r"[a-z%°]+$", "", n) for n in _unit_numbers(src_plain)}
    really_extra = {n for n in extra_nums if re.sub(r"[a-z%°]+$", "", n) not in bare_src or re.search(r"[a-z%°]$", n)}
    if really_extra:
        reasons.append(f"numbers not in the source: {', '.join(sorted(really_extra))}")
    missing = {n for n in _unit_numbers(original) if n not in _unit_numbers(new_plain)}
    if missing:
        reasons.append(f"dropped or changed numbers: {', '.join(sorted(missing))}")

    # technologies
    src_skills = set(lexicon.find_skills(own_plain)) | set(lexicon.find_skills(own_plain.replace("-", " ")))
    src_skills |= {lexicon.canonical(s) or s for s in bullet.get("skills") or []}
    new_skills = set(lexicon.find_skills(new_plain))
    extra_skills = new_skills - src_skills
    if extra_skills:
        reasons.append(f"technologies not in the source: {', '.join(sorted(extra_skills))}")

    # named entities
    src_words = {w.lower() for w in re.findall(r"[A-Za-z][A-Za-z0-9.+#/-]*", src_plain)}
    src_words |= {w.strip(".").lower() for w in src_words}
    extra_ents = {e for e in _entities(new_plain) if e not in src_words and e.rstrip("s") not in src_words}
    if extra_ents:
        reasons.append(f"names not in the source: {', '.join(sorted(extra_ents))}")

    # escalated claims
    for name, pat in CLAIMS.items():
        if re.search(pat, new_plain, re.I) and not re.search(pat, src_plain, re.I):
            reasons.append(f"new claim not in the source: {name}")

    # drift and length
    a, b = set(lexicon.content_words(new_plain)), set(lexicon.content_words(original))
    if a and b and len(a & b) / len(a | b) < 0.2:
        reasons.append("rewording drifted too far from the original")
    if limits:
        one, two = limits.get("one_line"), limits.get("two_lines")
        n = len(new_plain)
        if not ((one and n <= one) or (two and two[0] <= n <= two[1])):
            want = f"at most {one}" + (f", or {two[0]}-{two[1]} for two full lines" if two else "")
            reasons.append(f"length {n} chars would leave a short last line ({want})")
    elif len(new_plain) > max(len(original) * 1.1, len(original) + 15):
        reasons.append("longer than the original")
    # XYZ shape: action verb first, a measurable result, and how it was done
    first = (new_plain.split() or [""])[0]
    if _past_verb((original.split() or [""])[0]) and not _past_verb(first):
        reasons.append("xyz: doesn't start with a past-tense action verb")
    if _unit_numbers(original) and not _unit_numbers(new_plain):
        reasons.append("xyz: lost the measurable result")
    if _METHOD.search(original) and not _METHOD.search(new_plain):
        reasons.append("xyz: lost how it was done (by / using / via ...)")
    if len(new_plain) < len(original) * 0.4:
        reasons.append("lost too much of the original")
    return (not reasons), reasons
