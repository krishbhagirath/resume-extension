"""Makes one test resume from your bank. setup.bat runs this so Tectonic downloads its
LaTeX packages once, and so you know everything works. Uses no Claude usage."""
import os
import sys
from pathlib import Path

os.environ["ENGINE"] = "offline"
sys.path.insert(0, str(Path(__file__).resolve().parent))
import bank as bankmod  # noqa: E402
import tailor  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
JD = """Software Engineering Intern
Requirements:
- Experience with Python and one of Java, C++ or TypeScript
- Familiarity with REST APIs, SQL databases and Git
- Writing unit tests and debugging
Nice to have: React, Docker, cloud (AWS or GCP)
Responsibilities: build and test features with your team, review code, write documentation."""

bank = bankmod.load_dict(ROOT / "data" / "experiences.yaml")
print("Making a test resume (the first one takes a minute while LaTeX packages download)...")
res = tailor.run(bank, JD, title="Software Engineering Intern", company="Test", force=True)
out = ROOT / "output" / "_setup_test" / "test.pdf"
out.parent.mkdir(parents=True, exist_ok=True)
out.write_bytes(res.pdf)
print(f"Test resume OK: {out}  ({res.measure.get('pages')} page, {res.stats['compiles']} compile(s))")
