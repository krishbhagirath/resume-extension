# How Resume Tailor works

## Pieces

| Piece | Where | Job |
|---|---|---|
| Chrome extension (MV3) | `extension/` | Finds postings, shows the button and panel, starts a job, polls it, downloads, attaches |
| Local server (FastAPI) | `server/app.py` | Secured API on 127.0.0.1, web page, job store |
| Pipeline | `server/tailor.py` + modules below | Posting → one-page PDF |
| Web page | `ui/index.html`, `ui/app.js` | Tailor, bank editor, history, settings, diagnostics |
| Bank | `data/experiences.yaml` | Your accomplishments (schema v2, validated by `server/bank.py`) |
| Template | `templates/jakes/` | Your Overleaf (Jake's Resume) preamble with Jinja loops and spacing knobs |

## Pipeline (`server/tailor.py`)

1. **Analyze** (`analyzer.py`, deterministic). Splits the posting into sections (required, responsibilities, preferred, about, benefits), finds skills with `lexicon.py` (aliases, longest non-overlapping match, filters for ambiguous names like C, R, Go, CAN, Spring), and weights each requirement by section, "must/required" wording, repetition, position, and the job title. It also classifies role type (backend, ML, embedded, hardware…) and scores how complete the text looks. Below 45% confidence the job stops and asks for the real posting.
2. **Score** (`retrieval.py`). Every bullet is a candidate with evidence of skills (in its own text, or only in its item's heading/stack), keyword overlap, role fit, measured results, recency, and an estimated line cost.
3. **Re-rank** (`llm.py`, optional). The top 40 candidates (id + text) and the top requirements go to Claude in one call with a JSON schema. Claude returns an ordered list of ids, plus the company and title. Unknown ids are dropped. Its rank adds a bonus; it can't add text. Answers are cached by prompt hash. Failure or no usage → built-in ranking, with a warning.
4. **Select** (`planner.py`). Budgeted greedy selection with diminishing returns: each requirement's coverage counts less the more it's already covered. There's a redundancy penalty, and a cost in lines against a page budget derived from the template. Constraints: max experiences/projects, min/max bullets per item, `alt_of` and `overlaps` exclusivity, pinned/hidden. Recent internships get a small anchor bonus.
5. **Render + fit** (`render.py`, `layout.py`). The plan fills the template, so all text goes through `bank.safe_latex` (whitelisted formatting only). Tectonic compiles it, and pdfplumber measures the real PDF: pages, bottom gap, smallest font, text past the 0.25in edge. The loop then adjusts:
   - **Too long:** swap to shorter alternatives, then drop the lowest-value bullet, then tighten spacing within the template's bounds.
   - **Too short:** add the next best content, then relax spacing.

   It keeps the best valid attempt (up to 10 compiles; usually 1). A heading too wide for the line gets a specific error naming the line.
6. **Reword** (optional, off by default; `factcheck.py`, `linefit.py`). `linefit.py` renders every bank bullet once (cached per bank and template version) and measures lines used and last-line fill in the real PDF. Those measured line counts replace the character estimate in the page budget, and bullets with a short last line get a small penalty. Rewording only targets bullets with a short last line, or a tagged skill the job wants (at most `max_rephrase`). Each target gets a character limit (one line, or nearly two full lines). A rewording is accepted only if:
   - it keeps XYZ form (past-tense verb first, a number, the "by/using/via" method)
   - it keeps every number and unit
   - it adds no technology except from that bullet's own text, tags or notes (not the project stack)
   - it adds no new name or claim
   - it stays on topic and within its length limit

   After re-fitting, each reworded bullet is measured again; a short last line rejects it and the layout runs once more.
7. **Scorecard** (`evaluate.py`). Coverage of weighted requirements, *supported* coverage (only counting what your bank can show), unsupported requirements, share of quantified bullets, specificity, redundancy, rewording counts, one-page check. Every printed bullet has provenance: bank id, your wording or a checked rewording.

## Jobs (`server/pipeline.py`)

States: `queued → analyzing → matching → selecting → rendering → optimizing → (writing → optimizing) → completed | failed | cancelled`.

- Each job runs on a server thread (not in the extension's service worker, which Chrome can stop at any time). The extension polls `GET /api/jobs/{id}` and keeps the active job id in `chrome.storage.session`, so closing the panel or a worker restart loses nothing.
- **Idempotent:** a key over posting text, company, role, bank hash, template hash, engine, model, and reword setting. A duplicate click returns the running job; the same inputs return the finished file (unless `force`).
- **Cancel** kills the Claude process and stops between compiles. **Timeout:** 7 minutes.
- Errors carry a code (`low_confidence`, `usage_limit`, `not_signed_in`, `timeout`, `compile`, `layout`, `bank_invalid`, `interrupted`, `internal`), a plain message, and whether retrying helps.
- State is written to `output/jobs/<id>.json`; events go to `output/logs/pipeline.jsonl` (no posting text). Jobs cut off by a restart are marked `interrupted`.
- Each run folder keeps the PDF, `.tex`, and `run.json`. That holds the posting, analysis, plan, provenance, scorecard, layout history, timings, token counts, and bank/template versions.

## Security

- **Bind:** 127.0.0.1 only.
- **Host header** must be 127.0.0.1/localhost (blocks DNS rebinding).
- **Origin:** must be the page itself or a `chrome-extension://` origin. Requests marked `Sec-Fetch-Site: cross-site` with no valid token are refused, which covers links, `<img>`, iframes and form posts from websites.
- **Token:** every `/api/*` call except `/api/health` and `/api/pair` needs `X-RT-Token`. It's random, stored in `data/.token`, inlined into the web page, and given to the extension by `POST /api/pair`. Pairing is trust-on-first-use (only the first extension id; reset on the Settings tab). A website can't send the custom header cross-origin without a CORS preflight, and CORS is only granted to extension origins. PDF/Overleaf links opened as tabs work without the header, but only for direct (non-cross-site) navigation.
- **Paths** are run-id validated and resolved inside `output/`. Request sizes are capped (posting 60k chars, PDF upload ~10 MB).
- **LaTeX:** compiled with Tectonic (no shell escape); bullets pass through a whitelist sanitizer. The test suite tries `\input`, `\write18`, `\def`, `\catcode`, unbalanced braces and more.
- **Prompt injection:** posting text is wrapped in `<job_posting>` and the system prompt says it's untrusted data. The model can only return ids from the candidate list (validated), or rewordings that pass the deterministic fact check. Nothing the model says is executed.
- **Extension:** permissions are `activeTab`, `downloads`, `storage` and localhost hosts (no `scripting`). It only talks to 127.0.0.1/localhost, ignores messages from other extensions, and never submits forms.
- **Secrets:** `.claude-account/`, `data/.token`, `data/.paired-extension`, `.env` are git-ignored. The Claude Code subprocess gets no `ANTHROPIC_API_KEY`, so it can't bill the API by accident.

## Bank schema v2 (`server/bank.py`)

`version`, `profile`, `settings`, `education`, `skills`, `experiences[]`, `projects[]`. Items have `id`, `title`, `kind` (internship/work/research/team/leadership/volunteer/other or project/school/hackathon), `org`, `location`, `dates`, `stack`, `url`, `tags`, `overlaps`, `pinned`, `hidden`, `notes` and `bullets[]`. Bullets have `id`, `text`, `alt_of`, `skills`, `pinned`, `hidden` and `notes`. v1 files are migrated on load. Saving validates first (errors carry a path like `experiences.2.bullets.0.text`), backs up, and writes atomically. Concurrent edits are caught with a version hash (HTTP 409).

## Tests

```
pip install -r requirements-dev.txt
python -m playwright install chromium   # once, for the extension tests
python -m pytest -q                      # 131 tests, ~45 s, no Claude usage
python tests/benchmark.py                # per-role timing / coverage / overlap table
npm install && npm run check:ext         # type-check the extension (tsc --checkJs)
```

- `test_bank`, `test_analyzer`, `test_selection`, `test_factcheck`, `test_filenames`: unit tests, including adversarial rewordings and LaTeX injection.
- `test_layout`: real compiles; exactly one page for six role types, overflow recovery, special characters, a too-wide heading.
- `test_pipeline`: mocked Claude (bogus ids, rejected rewordings, usage limit fallback), idempotency, cancel, compile errors, restart recovery.
- `test_security`: Host/Origin/cross-site/token/pairing/CORS/path traversal/size limits against the real app.
- `test_extension`: the real unpacked extension in Chromium on synthetic Greenhouse, Lever, Workday (late render), LinkedIn (SPA navigation), Simplify, JSON-LD, non-job and malformed pages. It runs end to end through a real download and attach (checks the cover-letter box stays empty and nothing is submitted), and confirms a website's script can't reach the server.

On Linux the tests use XeLaTeX with a Font Awesome stub in place of Tectonic (`tests/support/`); on Windows they use the real `bin/tectonic.exe`.
