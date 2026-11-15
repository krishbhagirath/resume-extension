"""Loads the real unpacked extension in Chromium and drives it on synthetic job-site pages.

Pages are served through request interception at their real-looking URLs (greenhouse.io,
lever.co, myworkdayjobs.com, linkedin.com, simplify.jobs, ...), so the content script runs
exactly as it would there. The local server runs offline (no Claude usage).
Skipped when Playwright/Chromium aren't available.
"""
from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import pytest

from conftest import ROOT, needs_tex, BANK_FILE

pw = pytest.importorskip("playwright.sync_api")
PAGES = ROOT / "tests" / "fixtures" / "pages"
PORT = 8799
pytestmark = needs_tex

SITES = {
    "https://boards.greenhouse.io/examplecorp/jobs/123": "greenhouse.html",
    "https://jobs.lever.co/acme/5f1c-uuid": "lever.html",
    "https://acme.wd5.myworkdayjobs.com/en-US/careers/job/Embedded-Software-Intern_R1": "workday_spa.html",
    "https://www.linkedin.com/jobs/search/?currentJobId=1": "linkedin_spa.html",
    "https://simplify.jobs/p/abc123/full-stack-software-engineer-intern": "simplify.html",
    "https://careers.tailspin.example/jobs/42": "jsonld.html",
    "https://blog.example.com/2026/sourdough": "not_a_job.html",
    "https://jobs.example.org/opening/1": "malformed.html",
}


@pytest.fixture(scope="module")
def server(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("srv")
    (tmp / "data").mkdir()
    shutil.copy(BANK_FILE, tmp / "data" / "experiences.yaml")
    env = {**os.environ, "ENGINE": "offline", "PORT": str(PORT), "BANK_PATH": str(tmp / "data" / "experiences.yaml"),
           "OUTPUT_DIR": str(tmp / "output")}
    p = subprocess.Popen([sys.executable, str(ROOT / "server" / "app.py")], env=env, stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT)
    for _ in range(60):
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{PORT}/api/health", timeout=1)
            break
        except Exception:
            time.sleep(0.25)
    yield tmp
    p.terminate()


@pytest.fixture(scope="module")
def browser(server, tmp_path_factory):
    profile = tmp_path_factory.mktemp("profile")
    dl = tmp_path_factory.mktemp("downloads")
    with pw.sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(
            str(profile), headless=True, channel="chromium", accept_downloads=True, downloads_path=str(dl),
            args=[f"--disable-extensions-except={ROOT / 'extension'}", f"--load-extension={ROOT / 'extension'}"])
        sw = ctx.service_workers[0] if ctx.service_workers else ctx.wait_for_event("serviceworker", timeout=15000)
        sw.evaluate(f"chrome.storage.local.set({{server: 'http://127.0.0.1:{PORT}'}})")

        def serve(route):
            url = route.request.url
            for prefix, name in SITES.items():
                if url.split("?")[0].startswith(prefix.split("?")[0].rsplit("/", 1)[0]) or url.startswith(prefix):
                    return route.fulfill(path=str(PAGES / name), content_type="text/html")
            return route.fulfill(status=404, body="")
        for prefix in SITES:
            host = prefix.split("/")[2]
            ctx.route(f"https://{host}/**", serve)
        ctx.sw, ctx.dl = sw, dl
        yield ctx
        ctx.close()


def open_page(browser, url, wait=900):
    page = browser.new_page()
    page.goto(url)
    page.wait_for_timeout(wait)
    return page


def pill(page):
    return page.locator("resume-tailor-widget .pill")


def open_panel(page):
    pill(page).click()
    page.wait_for_function("""() => {
      const r = document.querySelector('resume-tailor-widget')?.shadowRoot;
      return r && !r.querySelector('#msg').textContent.includes('Reading');
    }""", timeout=15000)
    return page.evaluate("""() => {
      const r = document.querySelector('resume-tailor-widget').shadowRoot;
      return { company: r.querySelector('#company').value, role: r.querySelector('#role').value,
               jd: r.querySelector('#jd').value, src: r.querySelector('#src').textContent, msg: r.querySelector('#msg').textContent };
    }""")


@pytest.mark.parametrize("url,company,role,must", [
    ("https://boards.greenhouse.io/examplecorp/jobs/123", "Examplecorp", "Software Engineering Intern, Backend", "PostgreSQL"),
    ("https://jobs.lever.co/acme/5f1c-uuid", "Acme", "Machine Learning Intern", "PyTorch"),
    ("https://simplify.jobs/p/abc123/full-stack-software-engineer-intern", "Fabrikam", "Full Stack Software Engineer Intern", "Next.js"),
    ("https://careers.tailspin.example/jobs/42", "Tailspin Robotics", "Computer Vision Intern", "OpenCV"),
])
def test_extraction(browser, url, company, role, must):
    page = open_page(browser, url)
    assert pill(page).is_visible()
    got = open_panel(page)
    assert got["company"] == company and got["role"] == role
    assert must in got["jd"]
    for noise in ("cookies", "Sign in", "Share", "Apply for this job", "Privacy", "Submit application", "First name"):
        assert noise not in got["jd"], noise
    assert "looks complete" in got["src"], got["src"]
    page.close()


def test_not_a_job_page_has_no_button(browser):
    page = open_page(browser, "https://blog.example.com/2026/sourdough", wait=1500)
    assert pill(page).count() == 0 or not pill(page).is_visible()
    page.close()


def test_malformed_page_asks_for_text(browser):
    page = open_page(browser, "https://jobs.example.org/opening/1", wait=1500)
    if pill(page).count() and pill(page).is_visible():
        got = open_panel(page)
        assert "Paste" in got["msg"] or "may be incomplete" in got["src"], got
    page.close()


def test_spa_renders_late(browser):
    page = open_page(browser, "https://acme.wd5.myworkdayjobs.com/en-US/careers/job/Embedded-Software-Intern_R1", wait=300)
    page.wait_for_selector("resume-tailor-widget", state="attached", timeout=8000)  # shown while the page loads
    page.wait_for_selector('[data-automation-id="jobPostingDescription"]', timeout=8000)
    got = open_panel(page)
    assert got["role"] == "Embedded Software Intern" and "FreeRTOS" in got["jd"] and got["company"] == "Acme"
    page.close()


def test_linkedin_spa_navigation_updates(browser):
    page = open_page(browser, "https://www.linkedin.com/jobs/search/?currentJobId=1")
    got = open_panel(page)
    assert got["role"] == "Backend Intern" and "Go" in got["jd"]
    page.evaluate("document.querySelector('resume-tailor-widget').shadowRoot.querySelector('[data-act=close]').click()")
    page.click("#job2")
    page.wait_for_timeout(1200)
    got = open_panel(page)
    assert got["role"] == "Hardware Verification Intern" and got["company"] == "Contoso Silicon"
    assert "SystemVerilog" in got["jd"] and "PostgreSQL" not in got["jd"]
    page.close()


def test_dismiss_and_hide(browser):
    page = open_page(browser, "https://jobs.lever.co/acme/5f1c-uuid")
    page.evaluate("document.querySelector('resume-tailor-widget').shadowRoot.querySelector('[data-act=dismiss]').click()")
    page.wait_for_timeout(200)
    assert not pill(page).is_visible()
    page.close()


def test_end_to_end_download_and_attach(browser):
    page = open_page(browser, "https://boards.greenhouse.io/examplecorp/jobs/123")
    open_panel(page)
    R = "document.querySelector('resume-tailor-widget').shadowRoot"
    page.evaluate(f"{R}.querySelector('.primary[data-act=make]').click()")
    page.wait_for_function(f"() => /^Ready:/.test({R}.querySelector('#msg').textContent)", timeout=120000)
    msg = page.evaluate(f"{R}.querySelector('#msg').textContent")
    assert "Krish_Bhagirath_Examplecorp_SoftwareEngineeringInternBackend_Resume.pdf" in msg, msg
    steps = page.evaluate(f"{R}.querySelector('#steps').textContent")
    assert "Resume ready" in steps
    # nothing downloads until you click Download: first, preview what's on the page
    assert browser.sw.evaluate("chrome.downloads.search({}).then(x => x.length)") == 0
    cards = f"[...{R}.querySelectorAll('#editor .card')].map(c => c.dataset.item)"
    page.wait_for_function(f"() => {cards}.length > 1", timeout=15000)
    before = page.evaluate(cards)
    # several changes at once: replace one item (best fit, or remove if nothing can replace it) and remove a
    # bullet in a different item; the apply bar counts them and Download waits
    plan = page.evaluate(f"""() => {{
      const R = {R};
      const set = (sel, v) => {{ sel.value = v; sel.dispatchEvent(new Event('change', {{bubbles: true}})); }};
      const cs = [...R.querySelectorAll('#editor .card')];
      const isel = cs[0].querySelector('select[data-kind=item]');
      const how = [...isel.options].some(o => o.value === 'any') ? 'any' : 'remove';
      set(isel, how);
      cs[1].querySelector('[data-act=toggleBullets]').click();
      const bsel = cs[1].querySelector('select[data-kind=bullet]');
      set(bsel, 'remove');
      return {{ out: cs[0].dataset.item, bullet: bsel.dataset.out, how,
               bar: R.querySelector('#nchanges').textContent, barHidden: R.querySelector('#applybar').hidden,
               dlDisabled: R.querySelector('[data-act=download]').disabled,
               locked: [...cs[0].querySelectorAll('select[data-kind=bullet]')].every(b => b.disabled) }};
    }}""")
    assert plan["bar"] == "2 changes" and not plan["barHidden"] and plan["dlDisabled"] and plan["locked"], plan
    page.evaluate(f"{R}.querySelector('[data-act=applyEdits]').click()")
    page.wait_for_function(f"() => /^Ready:/.test({R}.querySelector('#msg').textContent) "
                           f"&& {cards}.length > 0 && !{cards}.includes({json.dumps(plan['out'])})", timeout=120000)
    after = page.evaluate(cards)
    bullets_now = page.evaluate(f"[...{R}.querySelectorAll('#editor li[data-bullet]')].map(li => li.dataset.bullet)")
    assert plan["out"] in before and plan["out"] not in after, (before, after)
    assert plan["bullet"] not in bullets_now
    # Reset clears pending changes and turns Download back on
    page.evaluate(f"""() => {{
      const sel = {R}.querySelector('#editor select[data-kind=item]');
      sel.value = 'remove'; sel.dispatchEvent(new Event('change', {{bubbles: true}}));
      {R}.querySelector('[data-act=resetEdits]').click(); }}""")
    assert page.evaluate(f"{R}.querySelector('#applybar').hidden") is True
    assert page.evaluate(f"{R}.querySelector('[data-act=download]').disabled") is False
    assert browser.sw.evaluate("chrome.downloads.search({}).then(x => x.length)") == 0
    page.evaluate(f"{R}.querySelector('[data-act=download]').click()")
    page.wait_for_function(f"() => /^Downloaded/.test({R}.querySelector('#msg').textContent)", timeout=15000)
    assert "Downloaded Krish_Bhagirath_Examplecorp_SoftwareEngineeringInternBackend_Resume.pdf" in \
        page.evaluate(f"{R}.querySelector('#msg').textContent")
    # downloaded through chrome.downloads, complete, right name
    for _ in range(40):
        items = browser.sw.evaluate("chrome.downloads.search({}).then(x => x.map(d => [d.filename, d.state, d.error || '', d.url]))")
        if items and items[-1][1] == "complete":
            break
        time.sleep(0.25)
    assert items[-1][1] == "complete", items
    # (Playwright stores managed downloads under a GUID; the name Chrome was given is checked via the message above)
    assert items[-1][3].endswith("?download=true")
    assert Path(items[-1][0]).read_bytes().startswith(b"%PDF")
    # attach to the resume box (not the cover letter), and never submit
    page.evaluate("document.querySelector('resume-tailor-widget').shadowRoot.querySelector('[data-act=attach]').click()")
    page.wait_for_timeout(800)
    assert page.evaluate("document.getElementById('resume').files[0]?.name") == "Krish_Bhagirath_Examplecorp_SoftwareEngineeringInternBackend_Resume.pdf"
    assert page.evaluate("document.getElementById('cover_letter').files.length") == 0
    assert page.evaluate("window.submitted") is False
    page.close()


def test_website_cannot_use_server(browser):
    """A page script on a job site can't read or drive the local server."""
    page = open_page(browser, "https://jobs.lever.co/acme/5f1c-uuid")
    res = page.evaluate(f"""async () => {{
      const out = {{}};
      for (const [k, opts] of [["get", {{}}], ["post", {{method: "POST", headers: {{"Content-Type": "application/json"}}, body: JSON.stringify({{jd: "x".repeat(300)}})}}]]) {{
        try {{ const r = await fetch("http://127.0.0.1:{PORT}/api/" + (k === "get" ? "bank" : "jobs"), opts); out[k] = r.status; }}
        catch (e) {{ out[k] = "blocked"; }}
      }}
      try {{ await fetch("http://127.0.0.1:{PORT}/api/pair", {{method: "POST"}}); out.pair = "readable"; }} catch {{ out.pair = "blocked"; }}
      return out;
    }}""")
    assert res["get"] in ("blocked", 403) and res["post"] in ("blocked", 403), res
    assert res["pair"] == "blocked", res
    page.close()
