# Resume Tailor

On a job posting or application form, a **Tailor resume** button shows up in the corner. Click it and you get a one-page PDF built only from accomplishments in your experience bank, named `Krish_Bhagirath_Company_Role_Resume.pdf`. On application forms, one more click attaches it to the resume upload box (it never submits anything).

```
Job page ─ Tailor resume ─▶ local server 127.0.0.1:8765 (starts at login)
                              ├─ reads the posting (required vs. nice-to-have, role type)
                              ├─ scores every bullet in data/experiences.yaml
                              ├─ Claude Code on your plan re-ranks the top ~40 (ids only)
                              ├─ picks the best set that fits one page
                              ├─ fills your Overleaf template → Tectonic → measures the real PDF
                              └─ adjusts until it's exactly one page
Downloads / resume upload box ◀┘
```

## Setup on Windows (one time)

1. **Double-click `setup.bat`.** It installs Python packages into `.venv`, downloads the Tectonic LaTeX compiler, installs Claude Code if needed, signs Resume Tailor into *its own* Claude account (your normal `claude` in a terminal is untouched), makes a test resume, and turns on start-at-login.
2. **Load the extension:** `chrome://extensions` → **Developer mode** → **Load unpacked** → pick the `extension` folder. Pin it. The first time it talks to the server it pairs itself.

Updating: double-click `update.bat` (stops the server, installs anything new, tests, restarts), then click the reload arrow on the extension in `chrome://extensions` and reload any job tabs already open.

## Making a resume

1. Open a posting or application form. Works on LinkedIn, Greenhouse, Lever, Workday, Ashby, iCIMS, SmartRecruiters, Workable, BambooHR, Jobvite, Indeed, Glassdoor, Simplify, WaterlooWorks, and most career pages (via the schema.org data most of them include).
2. Click **Tailor resume** (drag it anywhere; hover for × to hide it on this page).
3. Check company and role. The panel tells you how much text it found and whether it looks like a complete posting. Click **Make resume**. You'll see each step; you can cancel.
4. The PDF downloads once it passes the one-page check. On an application form, click **Attach to this application**, then check the form before you submit.

### Two versions, and teaching it what you like

- **Make 2 versions** (Tailor tab) builds the same posting twice: one ranked by Claude, one by the built-in matcher only. Which is A and which is B is random. The two PDFs sit side by side. Tick what made the difference (projects, bullets, skills line, layout), click **A is better** or **B is better**, and it shows which was which.
- **Swap** next to any printed bullet ("What's on the page") replaces it with another bullet from the same item and rebuilds the resume (still checked to one page). Swaps carry over if you swap again. With strict lines on, bullets with a short last line can't be swapped in.
- Every pick and every swap is saved to `output/picks.jsonl` with both resumes' scorecards and each printed bullet's score parts, ready for learning ranking weights from your choices later. A swap counts as "the new version is better, because of bullets". Posting text isn't copied into it.

If the button can't find the posting: highlight the job text and click again, or paste it into the box. Apply pages that hide the posting (Lever, Workday) use the posting you viewed earlier in the same tab.

## The web page (http://127.0.0.1:8765)

- **Tailor:** paste a posting, follow progress, preview the PDF, and see exactly which bank bullets were printed, which job needs they cover, and which needs your bank has no evidence for (those are never added).
- **Experience bank:** a card editor (search, filter, add, duplicate, reorder, pin, hide, alternate wordings, "same work as" links, private notes) with problems shown on the field. Ctrl+S saves. The YAML tab edits the file directly. Import from a resume PDF (you tick which bullets to add), import/export JSON. Every save keeps a backup of the previous version (`experiences.backup.yaml`).
- **History:** every resume with its coverage and fit; click to reopen.
- **Settings:** template, limits, file name pattern, rewording, extension pairing, and diagnostics.

## Your experience bank (`data/experiences.yaml`)

Your bank stays on your computer and is never committed (it's in `.gitignore`). A fresh clone starts from `data/example-bank.yaml`, which `setup.bat` copies to `data/experiences.yaml` the first time.

- Put everything in it. One bullet = one accomplishment. More bullets means better matches.
- `alt_of: <bullet id>` marks another wording of the same accomplishment; only one is ever used.
- `overlaps: [<item id>]` marks items describing the same work (e.g. a club role and its project); only one is used.
- `pinned: true` always includes an item or bullet; `hidden: true` never does.
- The bank editor shows how many lines each bullet takes in your template and flags a **short last line** (a word or two spilling onto line 2). Filter by "Short last lines" to fix them once. With `strict_lines: true` (Settings) such a bullet is never printed: a clean wording of it (`alt_of`) is used instead, or it's skipped.
- `notes` are private context. They're never printed, but the fact checker accepts rewordings that use them.
- With `latex_bullets: true` you can use `\textbf{}`, `\emph{}`, `\%`, `$\times$`. Anything else is escaped, so a bullet can't break or inject into the template.

## Claude usage

Default: Claude Code on Resume Tailor's own Claude account, model Haiku, **one request per resume** (it ranks ~40 candidate bullet ids; it never writes the resume). Identical requests are cached. If Claude is unavailable or out of usage, the built-in matcher is used and the panel says so.

Measured on real runs: about 6K tokens in and 1.6K out per resume, about 10 seconds of Claude time (roughly $0.002 if it were billed at Haiku API prices; it isn't, it counts against the plan). The History tab totals usage for today, 7 and 30 days.

Rewording is **off** by default (your exact words). If you turn it on in Settings, it stays light:
- Only bullets with a reason are touched: a short last line, or a skill the job wants that you tagged on that bullet. At most 3 per resume (setting). If none qualify, no extra Claude call is made.
- Each rewording must keep XYZ form (past-tense verb, the measurable result, how it was done), keep every number, use only facts from that same bullet, and fit one line or nearly two full lines.
- The reworded PDF is measured. A rewording that leaves a word or two on its own line is rejected and your wording is used.

`ENGINE=offline` in `.env` uses no Claude at all. `ENGINE=api` uses the paid API (needs `ANTHROPIC_API_KEY`).

## Privacy and safety

- The server only listens on 127.0.0.1. Websites you visit can't use it: wrong Host headers are refused, cross-site requests are refused, and every action needs a secret token (`data/.token`) that only this page and your paired extension have.
- Job text sent to Claude is marked as untrusted, and Claude only returns bullet ids, which are checked against your bank.
- Never commit `.claude-account/`, `data/.token`, or `.env`; they're in `.gitignore`.

## Files

`output/<date>_<Company>/` holds the PDF, the `.tex`, and `run.json` (posting text, what was picked and why, scorecard, timings). `output/logs/pipeline.jsonl` logs each job's steps (no posting text). See [ARCHITECTURE.md](ARCHITECTURE.md) for how it works and how to run the tests.
