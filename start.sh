#!/usr/bin/env bash
# Starts Resume Tailor on http://127.0.0.1:8765 (macOS / Linux)
set -e
cd "$(dirname "$0")"

if [ ! -d .venv ]; then
  echo "First run: setting up Python environment..."
  python3 -m venv .venv
  .venv/bin/pip install -q -r requirements.txt
fi

if ! command -v tectonic >/dev/null && ! command -v pdflatex >/dev/null; then
  echo "No LaTeX compiler found. Install Tectonic first:"
  echo "  macOS:  brew install tectonic"
  echo "  Linux:  curl --proto '=https' --tlsv1.2 -fsSL https://drop-sh.fullyjustified.net | sh"
  exit 1
fi

[ -f .env ] || echo "Tip: copy .env.example to .env and add your ANTHROPIC_API_KEY for smarter picks."

exec .venv/bin/python server/app.py --open
