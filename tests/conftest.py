"""Shared test setup.

* Never calls Claude: ENGINE=offline unless a test installs the fake model below.
* LaTeX: uses real Tectonic if installed; otherwise (Linux CI) tests/support/bin/tectonic,
  a XeLaTeX stand-in. Tests that compile are skipped when neither exists.
"""
from __future__ import annotations

import copy
import os
import shutil
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "server"))
os.environ["ENGINE"] = "offline"
if not shutil.which("tectonic") and not (ROOT / "bin" / "tectonic").exists() and shutil.which("xelatex"):
    os.environ["PATH"] = str(ROOT / "tests" / "support" / "bin") + os.pathsep + os.environ["PATH"]

import bank as bankmod  # noqa: E402
import render  # noqa: E402

HAS_TEX = render.find_compiler() is not None
needs_tex = pytest.mark.skipif(not HAS_TEX, reason="no LaTeX compiler")
FIXTURES = ROOT / "tests" / "fixtures"
JDS = {p.stem: p.read_text(encoding="utf-8") for p in (FIXTURES / "jds").glob("*.txt")}

_BANK = ROOT / "data" / "experiences.yaml"
BANK_FILE = _BANK if _BANK.exists() else ROOT / "data" / "example-bank.yaml"  # your bank, or the example in a fresh clone
_REAL = bankmod.load_dict(BANK_FILE)


@pytest.fixture
def real_bank() -> dict:
    return copy.deepcopy(_REAL)


@pytest.fixture
def small_bank() -> dict:
    """A tiny synthetic bank (no personal data) for edge-case tests."""
    return bankmod.to_dict(bankmod.parse({
        "version": 2,
        "profile": {"name": "Test Person", "first_name": "Test", "last_name": "Person", "email": "t@example.com",
                    "links": []},
        "settings": {"template": "jakes", "max_experiences": 3, "max_projects": 3},
        "education": [{"school": "Example University", "degree": "B.Eng. Software", "dates": "2023 – 2027",
                       "location": "Town, ON"}],
        "skills": [{"category": "Languages", "items": ["Python", "C++", "SQL", "TypeScript"]}],
        "experiences": [
            {"id": "acme", "title": "Software Intern", "org": "Acme", "dates": "May 2025 – Aug. 2025", "kind": "internship",
             "bullets": [
                 {"id": "acme-api", "text": "Built a Python FastAPI service backed by PostgreSQL, cutting p95 latency by 40\\%."},
                 {"id": "acme-api-alt", "alt_of": "acme-api", "text": "Cut API p95 latency 40\\% with a FastAPI + PostgreSQL rewrite."},
                 {"id": "acme-ci", "text": "Added pytest suites and GitHub Actions CI for 12 services."},
                 {"id": "acme-ui", "text": "Shipped React and TypeScript dashboards used by the support team."}]},
            {"id": "lab", "title": "Research Assistant", "org": "Example Lab", "dates": "Sep. 2024 – Apr. 2025", "kind": "research",
             "bullets": [
                 {"id": "lab-cv", "text": "Trained PyTorch CNN models for defect detection reaching 94\\% accuracy."},
                 {"id": "lab-data", "text": "Wrote OpenCV preprocessing for 20k images."}]},
        ],
        "projects": [
            {"id": "fw", "title": "Sensor Node", "stack": "C, FreeRTOS, STM32", "bullets": [
                {"id": "fw-rtos", "text": "Wrote FreeRTOS tasks on an STM32 reading I2C sensors at 100 Hz."},
                {"id": "fw-power", "text": "Reduced idle power draw 3$\\times$ with sleep scheduling."}]},
            {"id": "web", "title": "Planner App", "stack": "TypeScript, Next.js", "hidden": True, "bullets": [
                {"id": "web-1", "text": "Built a Next.js planner with Prisma and PostgreSQL."}]},
        ],
    }))
