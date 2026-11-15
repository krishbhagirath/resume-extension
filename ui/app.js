// Resume Tailor web page. Talks only to this local server (token from the page's meta tag).
"use strict";

const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];
const TOKEN = document.querySelector('meta[name="rt-token"]').content;
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
// LaTeX-formatted bullet -> readable text
const plain = (s) => String(s ?? "").replace(/\\(?:textbf|textit|emph|underline|texttt)\{([^{}]*)\}/g, "$1")
  .replace(/\$\\times\$/g, "×").replace(/\$\\sim\$/g, "~").replace(/\$\\pm\$/g, "±").replace(/\$\|\$/g, "|")
  .replace(/\$\\(?:to|rightarrow)\$/g, "→").replace(/\\([%$&#_{}])/g, "$1");
const fmtK = (n) => (n ? (n >= 1000 ? (n / 1000).toFixed(1) + "K" : String(n)) : "0");
const pct = (x) => (x == null ? "–" : Math.round(x * 100) + "%");

class ApiError extends Error {
  constructor(message, status, errors = [], code = "") { super(message); this.status = status; this.errors = errors; this.code = code; }
}

async function api(path, opts = {}) {
  const r = await fetch(path, { ...opts, headers: { "Content-Type": "application/json", "X-RT-Token": TOKEN, ...(opts.headers || {}) } });
  if (!r.ok) {
    let msg = r.statusText, errors = [], code = "";
    try {
      const body = await r.json();
      const d = body.detail ?? body;
      if (typeof d === "string") msg = d;
      else if (Array.isArray(d)) msg = d.map((x) => x.msg).join("; ");
      else { msg = d.message || msg; errors = d.errors || []; code = d.code || body.code || ""; }
    } catch { /* not JSON */ }
    if (r.status === 401) msg = "This page is out of date. Reload it.";
    throw new ApiError(msg, r.status, errors, code);
  }
  return (r.headers.get("content-type") || "").includes("json") ? r.json() : r.text();
}

// ---------------------------------------------------------------- tabs
const views = ["tailor", "bank", "history", "settings"];
$$("header nav button").forEach((b) => b.addEventListener("click", () => show(b.dataset.view)));
function show(view) {
  if (!views.includes(view)) view = "tailor";
  $$("header nav button").forEach((b) => b.setAttribute("aria-selected", b.dataset.view === view));
  $$(".view").forEach((v) => (v.hidden = v.id !== "view-" + view));
  history.replaceState(null, "", "#" + view);
  if (view === "bank") Bank.open();
  if (view === "history") loadHistory();
  if (view === "settings") Settings.open();
}
window.addEventListener("beforeunload", (e) => { if (Bank.dirty) { e.preventDefault(); e.returnValue = ""; } });

// ---------------------------------------------------------------- status
let health = null;
async function loadHealth() {
  try {
    health = await api("/api/health");
    const label = { "claude-code": `Claude Code · ${health.model}`, api: `Claude API · ${health.model}`, offline: "Offline matcher" }[health.mode] || "Offline matcher";
    const cls = health.problem ? "warn" : "ok";
    $("#status").innerHTML = `<span class="dot ${cls}"></span><span>${esc(label)}</span>` +
      (health.problem ? `<span style="color:var(--warn)">· ${esc(health.problem)}</span>` : "");
  } catch {
    $("#status").innerHTML = `<span class="dot bad"></span><span>Server not reachable</span>`;
  }
}

// ---------------------------------------------------------------- tailor
const STATE_LABEL = { queued: "Waiting", analyzing: "Reading the posting", matching: "Matching your experience", selecting: "Choosing content",
  writing: "Rewording", rendering: "Building the PDF", optimizing: "Fitting one page", completed: "Done", failed: "Failed", cancelled: "Cancelled" };
let currentJob = null;
let activeJobs = [];   // job ids being followed (two for "Make 2 versions")
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const isDone = (j) => ["completed", "failed", "cancelled"].includes(j.state);

$("#form").addEventListener("submit", (e) => { e.preventDefault(); startJob(); });
$("#go2").addEventListener("click", () => startCompare());
$("#jd").addEventListener("keydown", (e) => { if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) startJob(); });
$("#cancel").addEventListener("click", async () => {
  if (!activeJobs.length) return;
  $("#cancel").disabled = true;
  try { await Promise.all(activeJobs.map((id) => api(`/api/jobs/${id}/cancel`, { method: "POST" }))); }
  catch (e) { $("#err").textContent = e.message; }
});

function formBody(extra = {}) {
  return { jd: $("#jd").value, company: $("#company").value.trim(), role: $("#role").value.trim(),
           rephrase: $("#rephrase").checked, source: "manual", ...extra };
}

async function startJob(extra = {}) {
  if (currentJob) return;
  $("#err").textContent = "";
  const body = formBody(extra);
  if (body.jd.trim().length < 50) { $("#err").textContent = "Paste the full job description first."; return; }
  setBusy(true);
  try {
    const job = await api("/api/jobs", { method: "POST", body: JSON.stringify(body) });
    await follow(job);
  } catch (e) {
    $("#err").textContent = e.message;
  } finally { setBusy(false); }
}

function setBusy(on) {
  $("#go").disabled = on;
  $("#go2").disabled = on;
  $("#go").innerHTML = on ? '<span class="spin" aria-hidden="true"></span>Working…' : "Make resume";
  $("#cancel").hidden = !on;
  $("#cancel").disabled = false;
  if (!on) { currentJob = null; activeJobs = []; }
}

function jobError(job) {
  if (job.state === "cancelled") return "Cancelled.";
  const e = job.error || {};
  let msg = e.message || "Failed.";
  if (e.code === "low_confidence") msg += "\nIf this really is the full posting, press Make resume again with Shift held.";
  if (e.errors?.length) msg += "\n" + e.errors.slice(0, 5).map((x) => `• ${x.path}: ${x.message}`).join("\n");
  return msg;
}

async function loadRun(job) {
  const run = await api(`/api/run/${encodeURIComponent(job.result.run_id)}`);
  if (!$("#company").value) $("#company").value = run.company || "";
  if (!$("#role").value) $("#role").value = run.role || "";
  return run;
}

async function follow(job) {
  currentJob = job.id;
  activeJobs = [job.id];
  $("#progress").hidden = false;
  for (;;) {
    renderSteps(job);
    if (isDone(job)) break;
    await sleep(700);
    job = await api(`/api/jobs/${job.id}`);
  }
  if (job.state === "completed") showRun(await loadRun(job));
  else if (job.state === "failed") $("#err").textContent = jobError(job);
}

// ---------------------------------------------------------------- A/B: two versions, you pick
const REASONS = [["projects", "Projects"], ["bullets", "Bullets"], ["skills", "Skills line"], ["layout", "Layout"]];
let compare = null;   // {a, b, picked} for the last "Make 2 versions"

async function startCompare() {
  if (currentJob) return;
  $("#err").textContent = "";
  const body = formBody();
  if (body.jd.trim().length < 50) { $("#err").textContent = "Paste the full job description first."; return; }
  setBusy(true);
  currentJob = "compare";
  try {
    let { a, b } = await api("/api/compare", { method: "POST", body: JSON.stringify(body) });
    activeJobs = [a.id, b.id];
    $("#progress").hidden = false;
    for (;;) {
      renderPair(a, b);
      if (isDone(a) && isDone(b)) break;
      await sleep(700);
      [a, b] = await Promise.all([a, b].map((j) => (isDone(j) ? j : api(`/api/jobs/${j.id}`))));
    }
    const errs = [["A", a], ["B", b]].filter(([, j]) => j.state !== "completed").map(([k, j]) => `Version ${k}: ${jobError(j)}`);
    if (!errs.length) {
      const [ra, rb] = await Promise.all([loadRun(a), loadRun(b)]);
      compare = { a: ra, b: rb, picked: null };
      showCompare();
    } else {
      $("#err").textContent = errs.join("\n");
      const ok = [a, b].find((j) => j.state === "completed");
      if (ok) showRun(await loadRun(ok));
    }
  } catch (e) {
    $("#err").textContent = e.message;
  } finally { setBusy(false); }
}

function renderPair(a, b) {
  $("#steps").innerHTML = [["A", a], ["B", b]].map(([k, j]) => {
    const icon = isDone(j) ? (j.state === "completed" ? "✓" : "✕") : '<span class="spin"></span>';
    const t = j.progress?.length ? j.progress[j.progress.length - 1].t : 0;
    return `<li class="${isDone(j) ? "done" : "now"}"><span class="ic">${icon}</span><b>Version ${k}</b>&nbsp;${esc(j.message)}<span class="hint" style="margin-left:auto">${t}s</span></li>`;
  }).join("");
}

function variantLabel(run) {
  if (run.variant === "matcher") return "Built-in matcher";
  return run.mode === "offline" ? "Claude (not used: fell back to the matcher)" : "Ranked by Claude";
}

function showCompare() {
  const { a, b, picked } = compare;
  const ids = (r) => (r.provenance || []).map((p) => p.bullet_id).sort().join(",");
  const same = ids(a) === ids(b) && JSON.stringify(a.selection?.skills) === JSON.stringify(b.selection?.skills);
  const side = (k, r) => {
    const sc = r.scorecard || {};
    const v = r.validation || {};
    const pdf = `/api/pdf/${encodeURIComponent(r.id)}`;
    return `<div class="side${picked?.winner === k.toLowerCase() ? " won" : ""}">
      <h3>Version ${k}
        <span class="chip ${v.one_page ? "ok" : "bad"}">${v.one_page ? "1 page" : (v.pages || "?") + " pages"}</span>
        <span class="chip" title="Share of the job's needs your bank can show that this version shows">${pct(sc.supported_coverage)} coverage</span>
        <span class="chip">${sc.bullets ?? "?"} bullets · ${pct(sc.quantified)} measured</span>
        ${picked ? `<span class="chip ok">${esc(variantLabel(r))}</span>` : ""}</h3>
      ${(r.warnings || []).map((w) => `<p class="hint" style="color:var(--warn)">${esc(w)}</p>`).join("")}
      <iframe class="pdf" title="Version ${k} preview" src="${pdf}#view=FitH"></iframe>
      <div style="display:flex;gap:6px;flex-wrap:wrap">
        <a class="quiet" href="${pdf}" target="_blank" rel="noopener">Open</a>
        <a class="quiet" href="${pdf}?download=true" download="${esc(r.filename)}">Download</a>
        <button class="quiet" type="button" data-details="${k.toLowerCase()}">Details and swaps</button>
      </div></div>`;
  };
  $("#proof").innerHTML = `
    <div class="proof-bar"><h2>Which version is better?</h2>
      <span class="hint">${picked ? "Pick saved." : "One was ranked by Claude, one by the built-in matcher. You'll see which after you pick."}</span></div>
    ${same ? `<p class="hint" style="color:var(--warn)">Both versions printed the same bullets and skills, so there's little to choose between.</p>` : ""}
    <div class="card pickbar">
      <span class="why" role="group" aria-label="What made the difference">What made the difference?
        ${REASONS.map(([id, label]) => `<button type="button" data-why="${id}" aria-pressed="${!!picked?.reasons.includes(id)}" ${picked ? "disabled" : ""}>${label}</button>`).join("")}</span>
      <span class="go">
        <button class="primary" type="button" data-win="a" ${picked ? "disabled" : ""}>A is better</button>
        <button class="primary" type="button" data-win="b" ${picked ? "disabled" : ""}>B is better</button></span>
    </div>
    <p class="error" id="pickErr" role="alert"></p>
    <div class="ab">${side("A", a)}${side("B", b)}</div>`;
  $$("#proof [data-why]").forEach((btn) => btn.addEventListener("click", () =>
    btn.setAttribute("aria-pressed", btn.getAttribute("aria-pressed") !== "true")));
  $$("#proof [data-win]").forEach((btn) => btn.addEventListener("click", async () => {
    const reasons = $$('#proof [data-why][aria-pressed="true"]').map((x) => x.dataset.why);
    $$("#proof [data-win]").forEach((x) => (x.disabled = true));
    try {
      await api("/api/picks", { method: "POST", body: JSON.stringify({ run_a: a.id, run_b: b.id, winner: btn.dataset.win, reasons }) });
      compare.picked = { winner: btn.dataset.win, reasons };
      showCompare();
    } catch (e) {
      $("#pickErr").textContent = e.message;
      $$("#proof [data-win]").forEach((x) => (x.disabled = false));
    }
  }));
  $$("#proof [data-details]").forEach((btn) => btn.addEventListener("click", () =>
    showRun(compare[btn.dataset.details], { back: true })));
}
// Shift+click = go ahead even if the posting looks incomplete
$("#go").addEventListener("click", (e) => { if (e.shiftKey) { e.preventDefault(); startJob({ force_text: true }); } });

function renderSteps(job) {
  const rows = [];
  const seen = new Map();
  for (const p of job.progress || []) seen.set(p.state, p);
  const list = [...seen.values()];
  list.forEach((p, i) => {
    const last = i === list.length - 1;
    const terminal = ["completed", "failed", "cancelled"].includes(p.state);
    const icon = terminal ? (p.state === "completed" ? "✓" : "✕") : last ? '<span class="spin"></span>' : "✓";
    rows.push(`<li class="${last ? "now" : "done"}"><span class="ic">${icon}</span>${esc(p.message)}<span class="hint" style="margin-left:auto">${p.t}s</span></li>`);
  });
  $("#steps").innerHTML = rows.join("") || `<li class="now"><span class="ic"><span class="spin"></span></span>${esc(job.message)}</li>`;
}

function showRun(run, opts = {}) {
  const sc = run.scorecard || {};
  const st = run.stats || {};
  const v = run.validation || {};
  const pdf = `/api/pdf/${encodeURIComponent(run.id)}`;
  const tokens = (st.input_tokens || 0) + (st.output_tokens || 0);
  const warn = (run.warnings || []).map((w) => `<p class="hint" style="color:var(--warn)">${esc(w)}</p>`).join("");
  $("#proof").innerHTML = `
    <div class="proof-bar">
      <h2>${esc(run.filename)}</h2>
      <span class="chip ${v.one_page ? "ok" : "bad"}">${v.one_page ? "1 page" : (v.pages || "?") + " pages"}</span>
      <span class="chip">${run.mode === "offline" ? "Built-in matcher" : "Ranked by Claude"}</span>
      <div class="actions">
        ${opts.back && compare ? '<button class="quiet" type="button" id="backAB">← Both versions</button>' : ""}
        <a class="quiet" href="${pdf}" target="_blank" rel="noopener">Open</a>
        <a class="quiet" href="/api/overleaf/${encodeURIComponent(run.id)}" target="_blank" rel="noopener">Edit in Overleaf</a>
        <button class="quiet" type="button" id="regen">Regenerate</button>
        <a class="primary" href="${pdf}?download=true" download="${esc(run.filename)}">Download PDF</a>
      </div>
    </div>
    ${warn}
    <iframe class="pdf" title="Resume preview" src="${pdf}#view=FitH"></iframe>
    <div class="score">
      <div class="card"><b>${pct(sc.supported_coverage)}</b><span>of the job's needs you can show, shown</span></div>
      <div class="card"><b>${pct(sc.coverage)}</b><span>of all listed needs covered</span></div>
      <div class="card"><b>${pct(sc.quantified)}</b><span>bullets with a measured result</span></div>
      <div class="card"><b>${sc.rephrased ?? 0}</b><span>reworded (${sc.rephrase_rejected ?? 0} rejected)</span></div>
      <div class="card"><b>${st.compiles ?? "–"}</b><span>LaTeX compiles</span></div>
      <div class="card"><b>${st.seconds ?? "–"}s</b><span>${st.llm_requests ? `${st.llm_requests} Claude call${st.llm_requests > 1 ? "s" : ""}: ${fmtK(st.input_tokens)} in / ${fmtK(st.output_tokens)} out tokens${st.est_cost_usd ? ` (≈$${st.est_cost_usd.toFixed(4)} at API prices, not billed)` : ""}${st.llm_cached ? " · cached" : ""}` : st.llm_cached ? "answer reused from cache: no new usage" : "no Claude usage"}</span></div>
      <div class="card"><b>${(sc.short_last_lines || []).length}</b><span>bullets with a short last line${(sc.short_last_lines || []).length ? " (fix in the bank)" : ""}</span></div>
    </div>
    <div>
      ${(sc.evidenced_in_bullets || []).map((s) => `<span class="chip ok">${esc(s)}</span>`).join("")}
      ${(sc.covered || []).filter((s) => !(sc.evidenced_in_bullets || []).includes(s)).map((s) => `<span class="chip" title="Shown in skills or item headings">${esc(s)}</span>`).join("")}
      ${(sc.unsupported || []).map((s) => `<span class="chip bad" title="Your bank has no evidence for this, so it wasn't added">${esc(s)} (not in bank)</span>`).join("")}
    </div>
    ${provenanceHtml(run)}`;
  $("#backAB")?.addEventListener("click", () => showCompare());
  $("#proof .prov").addEventListener("click", (e) => {
    const out = e.target.dataset?.swap;
    if (out) openSwap(run, out, e.target.closest("li"));
    const item = e.target.dataset?.replace;
    if (item) openReplace(run, item, e.target.closest(".item"));
  });
  $("#regen").addEventListener("click", async () => {
    $("#jd").value = $("#jd").value || run.jd || "";
    if (!$("#jd").value) {
      setBusy(true);
      try { await follow(await api(`/api/run/${encodeURIComponent(run.id)}/regenerate`, { method: "POST" })); }
      catch (e) { $("#err").textContent = e.message; } finally { setBusy(false); }
    } else startJob({ force: true, variant: run.variant || "claude" });
  });
}

// Manual swap: rebuild with one printed bullet replaced. The server logs it as a pick (new beats old).
async function openSwap(run, out, li) {
  const open = li.querySelector(".swapbox");
  if (open) { open.remove(); return; }
  const box = document.createElement("div");
  box.className = "swapbox";
  box.innerHTML = '<span class="hint"><span class="spin"></span> Loading…</span>';
  li.appendChild(box);
  let options;
  try { options = await api(`/api/run/${encodeURIComponent(run.id)}/swap-options`); }
  catch (e) { box.innerHTML = `<span class="error">${esc(e.message)}</span>`; return; }
  const item = (run.provenance || []).find((p) => p.bullet_id === out)?.item_id;
  // another wording of a different printed bullet would just reword that one: only offer it on that bullet
  const rows = (options[item] || []).filter((r) => !r.same_as || r.same_as === out);
  if (!rows.length) { box.innerHTML = '<span class="hint">No other bullets in this item to swap in.</span>'; return; }
  box.innerHTML = `<select aria-label="Bullet to use instead">${rows.map((r) =>
      `<option value="${esc(r.id)}" ${r.blocked ? "disabled" : ""}>${r.same_as ? "Other wording: " : ""}${esc(r.text.slice(0, 140))}${r.blocked ? " (short last line: never printed)" : ""}</option>`).join("")}</select>
    <button class="quiet" type="button">Use this</button>
    <span class="hint">Rebuilds the resume and saves your choice as a pick.</span>`;
  const sel = box.querySelector("select");
  const firstOk = rows.find((r) => !r.blocked);
  if (firstOk) sel.value = firstOk.id;
  box.querySelector("button").addEventListener("click", async () => {
    if (currentJob || !sel.value || sel.selectedOptions[0]?.disabled) return;
    $("#err").textContent = "";
    setBusy(true);
    try {
      await follow(await api(`/api/run/${encodeURIComponent(run.id)}/swap`, { method: "POST", body: JSON.stringify({ out, in: sel.value }) }));
    } catch (e) { $("#err").textContent = e.message; } finally { setBusy(false); }
  });
}

// Replace a whole experience/project with an unused one from the same section, then rebuild.
async function openReplace(run, out, itemEl) {
  const open = itemEl.querySelector(":scope > .swapbox");
  if (open) { open.remove(); return; }
  const box = document.createElement("div");
  box.className = "swapbox";
  box.innerHTML = '<span class="hint"><span class="spin"></span> Loading…</span>';
  itemEl.querySelector("h4").after(box);
  let opts;
  try { opts = await api(`/api/run/${encodeURIComponent(run.id)}/items`); }
  catch (e) { box.innerHTML = `<span class="error">${esc(e.message)}</span>`; return; }
  const section = ["experiences", "projects"].find((s) => opts[s].on_page.some((i) => i.id === out));
  // an item describing the same work as a different one on the page can't come in here
  const rows = section ? opts[section].unused.filter((u) => u.overlaps.every((o) => o === out)) : [];
  if (!rows.length) { box.innerHTML = '<span class="hint">Nothing else in this section can replace it.</span>'; return; }
  box.innerHTML = `<select aria-label="Use this instead">${rows.map((u) =>
      `<option value="${esc(u.id)}" ${u.blocked ? "disabled" : ""}>${esc(u.title)}${u.org ? ", " + esc(u.org) : ""} (${u.bullets} bullet${u.bullets === 1 ? "" : "s"})${u.blocked ? ": short last lines, can't print" : ""}</option>`).join("")}</select>
    <button class="quiet" type="button">Use this instead</button>
    <span class="hint">Rebuilds the resume and saves your choice as a pick.</span>`;
  const sel = box.querySelector("select");
  const firstOk = rows.find((u) => !u.blocked);
  if (firstOk) sel.value = firstOk.id;
  box.querySelector("button").addEventListener("click", async () => {
    if (currentJob || !sel.value || sel.selectedOptions[0]?.disabled) return;
    $("#err").textContent = "";
    setBusy(true);
    try {
      await follow(await api(`/api/run/${encodeURIComponent(run.id)}/replace`, { method: "POST", body: JSON.stringify({ out, in: sel.value }) }));
    } catch (e) { $("#err").textContent = e.message; } finally { setBusy(false); }
  });
}

function provenanceHtml(run) {
  const prov = run.provenance || [];
  const byItem = new Map();
  for (const p of prov) { if (!byItem.has(p.item_id)) byItem.set(p.item_id, []); byItem.get(p.item_id).push(p); }
  const titles = Object.fromEntries((Bank.data ? [...Bank.data.experiences, ...Bank.data.projects] : []).map((i) => [i.id, i.title + (i.org ? ", " + i.org : "")]));
  const items = [...byItem.entries()].map(([id, rows]) => `
    <div class="item"><h4>${esc(titles[id] || id)}<button class="ghost swap" type="button" data-replace="${esc(id)}" title="Replace this whole item with another from the same section">Replace</button></h4><ul>${rows.map((p) => `
      <li>${esc(p.rephrased ? p.text.replace(/\*\*/g, "") : p.source)}<button class="ghost swap" type="button" data-swap="${esc(p.bullet_id)}" title="Replace this with another bullet from the same item">Swap</button><span class="tag">${p.rephrased ? "reworded · checked" : "your wording"} · ${esc(p.bullet_id)}${p.lines ? ` · ${p.lines} line${p.lines > 1 ? "s" : ""}` : ""}</span>${p.short_last_line ? `<span class="chip warn">last line ${Math.round(p.last_fill * 100)}% full</span>` : ""}
      ${p.rephrased ? `<div class="rej">Original: ${esc(p.source)}</div>` : ""}
      ${p.rejected ? `<div class="rej">Rejected rewording: “${esc(p.rejected_text)}” (${esc((p.reasons || []).join("; "))})</div>` : ""}</li>`).join("")}</ul></div>`).join("");
  const req = (run.analysis?.requirements || []).slice(0, 12).map((r) => `<span class="chip">${esc(r.skill)} · ${esc(r.section)}</span>`).join("");
  return `<div class="prov"><h3>What's on the page</h3>${items}
    <details><summary>How the posting was read</summary>
      <p class="hint">Role type: ${esc((run.analysis?.primary_roles || []).join(", "))} · text confidence ${pct(run.analysis?.quality?.confidence)}</p>${req}
    </details></div>`;
}

// ---------------------------------------------------------------- history
async function loadUsage() {
  try {
    const u = await api("/api/usage");
    const row = (k, label) => { const x = u[k]; return `<div class="card"><b>${x.resumes}</b><span>${label}: ${x.claude_calls} Claude call${x.claude_calls === 1 ? "" : "s"}, ${fmtK(x.input_tokens)} in / ${fmtK(x.output_tokens)} out${x.est_cost_usd ? `, ≈$${x.est_cost_usd.toFixed(3)} API-equivalent` : ""}</span></div>`; };
    $("#usage").innerHTML = row("today", "today") + row("7d", "last 7 days") + row("30d", "last 30 days") +
      `<div class="card"><b>${fmtK(u.all.per_resume.input_tokens + u.all.per_resume.output_tokens)}</b><span>tokens per resume on average (${u.all.per_resume.claude_calls} calls)</span></div>`;
  } catch { $("#usage").innerHTML = ""; }
}
async function loadHistory() {
  loadUsage();
  let runs = [];
  try { runs = await api("/api/history"); } catch (e) { $("#hist").innerHTML = `<tr><td colspan="7" class="error">${esc(e.message)}</td></tr>`; return; }
  $("#hist").innerHTML = runs.length ? runs.map((r) => `
    <tr>
      <td>${esc((r.created || "").replace("T", " ").slice(0, 16))}<div class="hint">${r.mode === "offline" ? "Built-in" : "Claude"}${r.seconds ? ` · ${r.seconds}s` : ""}</div></td>
      <td>${esc(r.company)}</td><td>${esc(r.role)}</td>
      <td>${pct(r.coverage)}</td>
      <td>${r.one_page === false ? '<span class="chip bad">over</span>' : r.one_page ? '<span class="chip ok">1 page</span>' : ""}</td>
      <td><a href="#" data-run="${esc(r.id)}">${esc(r.filename)}</a></td>
      <td>${/^https?:\/\//.test(r.url || "") ? `<a href="${esc(r.url)}" target="_blank" rel="noopener noreferrer">Posting</a>` : ""}</td>
    </tr>`).join("") : `<tr><td colspan="7" class="hint">No resumes yet. Make one here or with the extension.</td></tr>`;
}
$("#hist").addEventListener("click", async (e) => {
  const id = e.target.dataset?.run;
  if (!id) return;
  e.preventDefault();
  const run = await api(`/api/run/${encodeURIComponent(id)}`);
  $("#jd").value = run.jd || "";
  $("#company").value = run.company || "";
  $("#role").value = run.role || "";
  show("tailor");
  if (!Bank.data) await Bank.load().catch(() => {});
  showRun(run);
});

// ---------------------------------------------------------------- bank editor
const KINDS = { experiences: ["internship", "work", "research", "team", "leadership", "volunteer", "other"],
                projects: ["project", "school", "hackathon", "other"] };

const Bank = {
  data: null, version: null, dirty: false, mode: "cards", errors: [], open_: new WeakSet(), timer: null,

  async load() {
    const r = await api("/api/bank.json");
    this.data = r.bank;
    this.data.experiences ||= []; this.data.projects ||= []; this.data.skills ||= [];
    this.version = r.version;
    this.errors = r.errors || [];
    this.setDirty(false);
    return r;
  },
  async loadLines() {
    try {
      this.lines = await api("/api/bank/lines");
      this.linesFor = {};
      for (const s of ["experiences", "projects"]) for (const it of this.data[s]) for (const b of it.bullets) this.linesFor[b.id] = b.text;
    } catch { this.lines = null; }
  },
  fitHtml(b) {
    const L = this.lines;
    if (!L) return "";
    const minFill = L.min_last_line_fill ?? 0.6;
    const measured = L.bullets[b.id] && this.linesFor?.[b.id] === b.text ? L.bullets[b.id] : null;
    let lines, last, label;
    if (measured) { lines = measured.lines; last = measured.last_fill; label = ""; }
    else {
      const n = plain(b.text).length / (L.chars_per_line || 113);
      lines = Math.max(1, Math.ceil(n - 0.02)); last = n - (lines - 1); label = "≈";
    }
    const short = lines > 1 && last < minFill;
    const len = plain(b.text).length;
    return `<span class="chip ${short ? "warn" : ""}" title="${len} characters; about ${L.chars_per_line} fit on one line">${label}${lines} line${lines > 1 ? "s" : ""}${lines > 1 ? `, last ${Math.round(last * 100)}% full` : ` (${Math.round(last * 100)}%)`}${short ? " · short last line" : ""}</span>`;
  },
  isShort(b) { return /short last line/.test(this.fitHtml(b)); },
  async open() {
    if (!this.lines) this.loadLines().then(() => this.render());
    if (!this.data || !this.dirty) await this.load().catch((e) => { $("#editor").innerHTML = `<p class="error">${esc(e.message)}</p>`; });
    if (this.mode === "yaml") $("#yaml").value = await api("/api/bank");
    this.render();
  },
  setDirty(on, text) {
    this.dirty = on;
    const s = $("#saveState");
    s.className = on ? "dirty" : this.errors.length ? "bad" : "";
    s.textContent = text || (on ? "Unsaved changes" : this.errors.length ? `${this.errors.length} problem(s)` : "All changes saved");
  },
  changed() {
    this.setDirty(true);
    clearTimeout(this.timer);
    this.timer = setTimeout(() => this.validate(), 600);
  },
  async validate() {
    if (this.mode !== "cards") return;
    const r = await api("/api/bank/validate", { method: "POST", body: JSON.stringify({ bank: this.data }) }).catch(() => null);
    if (!r) return;
    this.errors = r.errors;
    this.showErrors();
    this.setDirty(this.dirty);
  },
  async save() {
    const btn = $("#saveBank");
    btn.disabled = true;
    try {
      let r;
      if (this.mode === "yaml") {
        r = await api("/api/bank", { method: "PUT", body: JSON.stringify({ yaml: $("#yaml").value, version: this.version }) });
        await this.load();
      } else {
        r = await api("/api/bank.json", { method: "PUT", body: JSON.stringify({ bank: this.data, version: this.version }) });
        this.version = r.version;
      }
      this.errors = [];
      this.showErrors();
      this.setDirty(false, "Saved. The next resume uses this version.");
      this.loadLines().then(() => this.render());
    } catch (e) {
      this.errors = e.errors || [];
      this.showErrors(e.message);
      this.setDirty(true, e.status === 409 ? "Changed elsewhere: reload" : "Not saved");
      $("#saveState").className = "bad";
    } finally { btn.disabled = false; }
  },
  showErrors(headline) {
    $$(".invalid").forEach((el) => el.classList.remove("invalid"));
    $$(".errmsg").forEach((el) => el.remove());
    if (!this.errors.length && !headline) { $("#errPanel").innerHTML = ""; return; }
    const rows = this.errors.slice(0, 30).map((e, i) => `<li><a href="#" data-err="${i}">${esc(this.describe(e.path))}</a>: ${esc(e.message)}</li>`).join("");
    $("#errPanel").innerHTML = `<div class="errlist" role="alert"><b>${esc(headline || "Fix these before saving")}</b>${rows ? `<ul>${rows}</ul>` : ""}</div>`;
    for (const e of this.errors) {
      const el = this.fieldFor(e.path);
      if (!el) continue;
      el.classList.add("invalid");
      el.insertAdjacentHTML("afterend", `<div class="errmsg">${esc(e.message)}</div>`);
    }
  },
  fieldFor(path) {
    let p = path;
    while (p) {
      const el = document.querySelector(`[data-path="${CSS.escape(p)}"]`);
      if (el) return el;
      p = p.includes(".") ? p.slice(0, p.lastIndexOf(".")) : "";
    }
    return null;
  },
  describe(path) {
    const parts = (path || "").split(".");
    const sec = parts[0], i = +parts[1];
    const item = this.data?.[sec]?.[i];
    if (!item || typeof item !== "object") return path || "Bank";
    let s = (item.title || item.school || item.category || sec) + "";
    if (parts[2] === "bullets") s += ` → bullet ${+parts[3] + 1}`;
    const field = parts[parts[2] === "bullets" ? 4 : 2];
    return field ? `${s} (${field})` : s;
  },

  // ---- rendering
  render() {
    $("#editor").hidden = this.mode !== "cards";
    $("#yamlBox").hidden = this.mode !== "yaml";
    $("#q").disabled = $("#filter").disabled = this.mode !== "cards";
    if (this.mode !== "cards" || !this.data) return;
    const d = this.data;
    const p = d.profile || {};
    $("#editor").innerHTML = `
      <div class="sec-title">Profile</div>
      <div class="card item" data-kind="profile">
        <div class="head" data-toggle><span class="t">${esc(p.first_name)} ${esc(p.last_name)}</span><span class="o">${esc(p.email)}</span></div>
        <div class="body" ${this.open_.has(p) ? "" : "hidden"}>
          <div class="grid">
            ${field("profile.first_name", "First name", p.first_name)}${field("profile.last_name", "Last name", p.last_name)}
            ${field("profile.email", "Email", p.email)}${field("profile.phone", "Phone", p.phone)}
          </div>
          <p class="hint">Education and header links are edited on the YAML tab.</p>
        </div>
      </div>
      <div class="sec-title">Experience <button class="ghost" data-add="experiences" type="button">+ Add</button></div>
      ${d.experiences.map((it, i) => this.itemHtml("experiences", it, i)).join("")}
      <div class="sec-title">Projects <button class="ghost" data-add="projects" type="button">+ Add</button></div>
      ${d.projects.map((it, i) => this.itemHtml("projects", it, i)).join("")}
      <div class="sec-title">Skills <button class="ghost" data-add="skills" type="button">+ Add group</button></div>
      <div class="card panel">${d.skills.map((g, i) => `
        <div class="grid" style="margin-bottom:6px">
          ${field(`skills.${i}.category`, "Category", g.category)}
          <label class="w2">Items (comma separated)<input data-path="skills.${i}.items" data-type="list" value="${esc((g.items || []).join(", "))}"></label>
          <div style="align-self:end"><button class="ghost danger" data-del="skills.${i}" type="button">Remove</button></div>
        </div>`).join("")}
        <p class="hint">Only skills you list here (or that your bullets prove) can appear on the skills line.</p>
      </div>`;
    this.applyFilter();
    this.showErrors();
  },

  itemHtml(sec, it, i) {
    const base = `${sec}.${i}`;
    const others = [...this.data.experiences, ...this.data.projects].filter((x) => x !== it).map((x) => x.id);
    const bulletIds = it.bullets.map((b) => b.id);
    const open = this.open_.has(it);
    const search = [it.title, it.org, it.stack, (it.tags || []).join(" "), ...it.bullets.map((b) => plain(b.text))].join(" ").toLowerCase();
    return `
    <div class="card item ${it.hidden ? "hidden-item" : ""}" data-sec="${sec}" data-i="${i}" data-search="${esc(search)}" data-pinned="${!!it.pinned}" data-hidden="${!!it.hidden}" data-short="${it.bullets.some((b) => this.isShort(b))}" id="item-${esc(it.id)}">
      <div class="head" data-toggle>
        <span aria-hidden="true">${open ? "▾" : "▸"}</span>
        <span class="t">${esc(it.title || "Untitled")}</span><span class="o">${esc(it.org || it.stack || "")}</span>
        <span class="chip">${esc(it.kind)}</span>${it.pinned ? '<span class="chip ok">pinned</span>' : ""}${it.hidden ? '<span class="chip">hidden</span>' : ""}
        <span class="chip">${it.bullets.length} bullet${it.bullets.length === 1 ? "" : "s"}</span>
        <span class="tools">
          <button class="ghost" data-move="${base}" data-dir="-1" title="Move up" aria-label="Move up" type="button">↑</button>
          <button class="ghost" data-move="${base}" data-dir="1" title="Move down" aria-label="Move down" type="button">↓</button>
          <button class="ghost" data-dup="${base}" type="button">Duplicate</button>
          <button class="ghost danger" data-del="${base}" type="button">Delete</button>
        </span>
      </div>
      <div class="body" ${open ? "" : "hidden"}>
        <div class="grid">
          ${field(base + ".title", "Title", it.title, "w2")}
          ${sec === "experiences" ? field(base + ".org", "Organization", it.org) : field(base + ".stack", "Tech stack", it.stack)}
          <label>Kind<select data-path="${base}.kind">${KINDS[sec].concat(KINDS[sec].includes(it.kind) ? [] : [it.kind]).map((k) => `<option ${k === it.kind ? "selected" : ""}>${esc(k)}</option>`).join("")}</select></label>
          ${field(base + ".dates", "Dates", it.dates)}
          ${field(base + ".location", "Location", it.location)}
          ${field(base + ".url", "Link", it.url)}
          ${field(base + ".id", "Id", it.id)}
          <label class="w2">Tags (comma separated)<input data-path="${base}.tags" data-type="list" value="${esc((it.tags || []).join(", "))}"></label>
          <label class="w2">Same work as (only one is used)<input data-path="${base}.overlaps" data-type="list" list="itemIds" value="${esc((it.overlaps || []).join(", "))}" placeholder="${esc(others.slice(0, 2).join(", "))}"></label>
          <label class="w4">Private notes (context the fact checker may use; never printed)<textarea rows="2" data-path="${base}.notes">${esc(it.notes)}</textarea></label>
        </div>
        <div style="display:flex;gap:14px">
          <label class="inline"><input type="checkbox" data-path="${base}.pinned" data-type="bool" ${it.pinned ? "checked" : ""}> Always include</label>
          <label class="inline"><input type="checkbox" data-path="${base}.hidden" data-type="bool" ${it.hidden ? "checked" : ""}> Never include</label>
        </div>
        ${it.bullets.map((b, j) => this.bulletHtml(base, b, j, bulletIds)).join("")}
        <div><button class="quiet" data-addbullet="${base}" type="button">+ Add bullet</button></div>
      </div>
    </div>`;
  },

  bulletHtml(base, b, j, ids) {
    const bp = `${base}.bullets.${j}`;
    const latex = this.data.settings?.latex_bullets;
    return `
      <div class="bullet ${b.alt_of ? "alt" : ""}">
        <textarea data-path="${bp}.text" aria-label="Bullet text" spellcheck="true">${esc(b.text)}</textarea>
        <div class="side">
          <button class="ghost" data-move="${bp}" data-dir="-1" aria-label="Move up" type="button">↑</button>
          <button class="ghost" data-move="${bp}" data-dir="1" aria-label="Move down" type="button">↓</button>
          <button class="ghost danger" data-del="${bp}" aria-label="Delete bullet" type="button">✕</button>
        </div>
        ${latex ? `<div class="preview">${esc(plain(b.text))}</div>` : ""}
        <div class="meta">
          <label>Id<input data-path="${bp}.id" value="${esc(b.id)}"></label>
          <label>Other wording of<select data-path="${bp}.alt_of" data-type="nullable"><option value="">(none)</option>${ids.filter((x) => x !== b.id).map((x) => `<option ${x === b.alt_of ? "selected" : ""}>${esc(x)}</option>`).join("")}</select></label>
          <label>Extra skills<input data-path="${bp}.skills" data-type="list" value="${esc((b.skills || []).join(", "))}" placeholder="auto-detected"></label>
          <span class="fit" data-fit="${bp}">${this.fitHtml(b)}</span>
          <label class="inline"><input type="checkbox" data-path="${bp}.pinned" data-type="bool" ${b.pinned ? "checked" : ""}> Always</label>
          <label class="inline"><input type="checkbox" data-path="${bp}.hidden" data-type="bool" ${b.hidden ? "checked" : ""}> Never</label>
        </div>
      </div>`;
  },

  applyFilter() {
    const q = $("#q").value.trim().toLowerCase();
    const f = $("#filter").value;
    $$("#editor .item[data-sec]").forEach((el) => {
      const okQ = !q || el.dataset.search.includes(q);
      const okF = !f || (f === "pinned" ? el.dataset.pinned === "true" : f === "short" ? el.dataset.short === "true" : f === "hidden" ? el.dataset.hidden === "true" : el.dataset.sec === f);
      el.hidden = !(okQ && okF);
    });
  },

  // ---- edits
  get(path) { return path.split(".").reduce((o, k) => (o == null ? o : o[k]), this.data); },
  set(path, value) {
    const keys = path.split(".");
    const last = keys.pop();
    const parent = keys.reduce((o, k) => o[k], this.data);
    if (value === undefined || value === null || value === "" && ["alt_of"].includes(last)) delete parent[last];
    else parent[last] = value;
  },
  parentList(path) {
    const keys = path.split(".");
    const idx = +keys.pop();
    return [keys.reduce((o, k) => o[k], this.data), idx];
  },
  uniqueId(base) {
    const all = new Set();
    for (const s of ["experiences", "projects"]) for (const it of this.data[s]) { all.add(it.id); it.bullets.forEach((b) => all.add(b.id)); }
    const slug = (base || "item").toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-|-$/g, "").slice(0, 24) || "item";
    if (!all.has(slug)) return slug;
    for (let n = 2; ; n++) if (!all.has(`${slug}-${n}`)) return `${slug}-${n}`;
  },
  newItem(sec, title = "New " + (sec === "projects" ? "project" : "experience")) {
    const id = this.uniqueId(title);
    return { id, title, kind: sec === "projects" ? "project" : "work", org: "", dates: "", stack: "",
             bullets: [{ id: this.uniqueId(id + "-1"), text: "Describe one accomplishment with a measurable result." }] };
  },
};

function field(path, label, value, cls = "") {
  return `<label class="${cls}">${esc(label)}<input data-path="${path}" value="${esc(value ?? "")}"></label>`;
}

$("#editor").addEventListener("input", (e) => {
  const el = e.target;
  const path = el.dataset.path;
  if (!path) return;
  let v = el.value;
  const t = el.dataset.type;
  if (t === "bool") v = el.checked;
  else if (t === "list") v = v.split(",").map((s) => s.trim()).filter(Boolean);
  else if (t === "nullable") v = v || null;
  Bank.set(path, v);
  if (path.endsWith(".text")) {
    const fitEl = document.querySelector(`[data-fit="${CSS.escape(path.slice(0, -5))}"]`);
    if (fitEl) fitEl.innerHTML = Bank.fitHtml(Bank.get(path.slice(0, -5)));
    const pv = el.parentElement.querySelector(".preview");
    if (pv) pv.textContent = plain(el.value);
  }
  if (t === "bool" || t === "nullable") Bank.render();
  Bank.changed();
});
$("#editor").addEventListener("click", (e) => {
  const b = e.target.closest("button, [data-toggle]");
  if (!b) return;
  const D = Bank.data;
  if (b.hasAttribute("data-toggle") && !e.target.closest("button")) {
    const card = b.closest(".item");
    const obj = card.dataset.kind === "profile" ? D.profile : D[card.dataset.sec][+card.dataset.i];
    Bank.open_.has(obj) ? Bank.open_.delete(obj) : Bank.open_.add(obj);
    return Bank.render();
  }
  if (b.dataset.add) {
    if (b.dataset.add === "skills") D.skills.push({ category: "New group", items: [] });
    else { const it = Bank.newItem(b.dataset.add); D[b.dataset.add].push(it); Bank.open_.add(it); }
  } else if (b.dataset.addbullet) {
    const it = Bank.get(b.dataset.addbullet);
    it.bullets.push({ id: Bank.uniqueId(it.id + "-" + (it.bullets.length + 1)), text: "" });
  } else if (b.dataset.del) {
    const [list, i] = Bank.parentList(b.dataset.del);
    const target = list[i];
    if (target.bullets && !confirmInline(b, `Delete “${target.title}” and its ${target.bullets.length} bullets?`)) return;
    list.splice(i, 1);
  } else if (b.dataset.move) {
    const [list, i] = Bank.parentList(b.dataset.move);
    const j = i + +b.dataset.dir;
    if (j < 0 || j >= list.length) return;
    [list[i], list[j]] = [list[j], list[i]];
  } else if (b.dataset.dup) {
    const [list, i] = Bank.parentList(b.dataset.dup);
    const copy = JSON.parse(JSON.stringify(list[i]));
    copy.id = Bank.uniqueId(copy.id + "-copy");
    copy.title += " (copy)";
    const map = {};
    copy.bullets.forEach((x) => { const n = Bank.uniqueId(copy.id + "-" + x.id.split("-").pop()); map[x.id] = n; x.id = n; });
    copy.bullets.forEach((x) => { if (x.alt_of) x.alt_of = map[x.alt_of] || null; if (!x.alt_of) delete x.alt_of; });
    list.splice(i + 1, 0, copy);
    Bank.open_.add(copy);
  } else return;
  Bank.render();
  Bank.changed();
});
// two-click confirm on the button itself (no browser dialogs)
function confirmInline(btn, msg) {
  if (btn.dataset.armed) return true;
  btn.dataset.armed = "1";
  const old = btn.textContent;
  btn.textContent = "Click again to delete";
  btn.title = msg;
  setTimeout(() => { btn.textContent = old; delete btn.dataset.armed; }, 3000);
  return false;
}

$("#q").addEventListener("input", () => Bank.applyFilter());
$("#filter").addEventListener("change", () => Bank.applyFilter());
$("#saveBank").addEventListener("click", () => Bank.save());
$("#errPanel").addEventListener("click", (e) => {
  const i = e.target.dataset?.err;
  if (i == null) return;
  e.preventDefault();
  const path = Bank.errors[+i].path;
  const [sec, idx] = path.split(".");
  const obj = Bank.data[sec]?.[+idx];
  if (obj && typeof obj === "object" && obj.bullets) { Bank.open_.add(obj); Bank.render(); }
  Bank.fieldFor(path)?.scrollIntoView({ block: "center" });
  Bank.fieldFor(path)?.focus();
});
document.addEventListener("keydown", (e) => {
  if (e.key === "s" && (e.metaKey || e.ctrlKey) && !$("#view-bank").hidden) { e.preventDefault(); Bank.save(); }
});
$("#yaml").addEventListener("input", () => Bank.setDirty(true));
$("#yaml").addEventListener("keydown", (e) => {
  if (e.key === "Tab") { e.preventDefault(); document.execCommand("insertText", false, "  "); }
});
$$("#view-bank .bankbar nav button").forEach((b) => b.addEventListener("click", async () => {
  if (Bank.dirty && !confirmInline(b, "Switching discards unsaved changes")) { $("#saveState").textContent = "Save first, or click again to discard"; return; }
  Bank.mode = b.dataset.mode;
  $$("#view-bank .bankbar nav button").forEach((x) => x.setAttribute("aria-selected", x === b));
  await Bank.load();
  if (Bank.mode === "yaml") $("#yaml").value = await api("/api/bank");
  Bank.render();
}));

// ---- export / import
$("#exportJson").addEventListener("click", async (e) => {
  e.preventDefault();
  const text = await api("/api/bank/export");
  const blob = new Blob([typeof text === "string" ? text : JSON.stringify(text, null, 2)], { type: "application/json" });
  const a = Object.assign(document.createElement("a"), { href: URL.createObjectURL(blob), download: "experience-bank.json" });
  a.click();
  setTimeout(() => URL.revokeObjectURL(a.href), 1000);
});
$("#importJsonBtn").addEventListener("click", () => $("#importJson").click());
$("#importJson").addEventListener("change", async (e) => {
  const f = e.target.files[0];
  e.target.value = "";
  if (!f) return;
  const r = await api("/api/bank/import", { method: "POST", body: JSON.stringify({ text: await f.text() }) });
  if (!r.ok) { Bank.errors = r.errors; Bank.showErrors("That file isn't a valid bank"); return; }
  const n = r.bank.experiences.length + r.bank.projects.length;
  $("#importPanel").innerHTML = `<div class="card panel"><h3>Replace your bank with ${esc(f.name)}?</h3>
    <p class="hint">${n} items, ${r.bank.experiences.concat(r.bank.projects).reduce((s, i) => s + i.bullets.length, 0)} bullets. Your current bank is backed up when you save.</p>
    <button class="primary" id="impYes" type="button">Load it into the editor</button> <button class="ghost" id="impNo" type="button">Cancel</button></div>`;
  $("#impNo").onclick = () => ($("#importPanel").innerHTML = "");
  $("#impYes").onclick = () => { Bank.data = r.bank; $("#importPanel").innerHTML = ""; Bank.mode = "cards"; Bank.render(); Bank.changed(); };
});

$("#importPdfBtn").addEventListener("click", () => $("#importPdf").click());
$("#importPdf").addEventListener("change", async (e) => {
  const f = e.target.files[0];
  e.target.value = "";
  if (!f) return;
  if (f.size > 10e6) { $("#importPanel").innerHTML = `<p class="error">That PDF is too large.</p>`; return; }
  $("#importPanel").innerHTML = `<div class="card panel"><span class="spin"></span> Reading ${esc(f.name)}…</div>`;
  const b64 = await new Promise((res) => { const r = new FileReader(); r.onload = () => res(r.result); r.readAsDataURL(f); });
  try {
    const r = await api("/api/parse-resume", { method: "POST", body: JSON.stringify({ pdf_base64: b64 }) });
    showDrafts(r, f.name);
  } catch (err) { $("#importPanel").innerHTML = `<p class="error">${esc(err.message)}</p>`; }
});

function norm(s) { return plain(s).toLowerCase().replace(/[^a-z0-9]+/g, " ").trim(); }
function showDrafts(r, name) {
  const existing = [...Bank.data.experiences, ...Bank.data.projects];
  const known = new Set(existing.flatMap((i) => i.bullets.map((b) => norm(b.text))));
  const drafts = r.drafts.map((d, i) => {
    const match = existing.find((x) => norm(x.title) === norm(d.title) && (!d.org || norm(x.org || "") === norm(d.org)));
    return { ...d, i, match, bullets: d.bullets.map((t) => ({ t, dupe: known.has(norm(t)) })) };
  });
  $("#importPanel").innerHTML = `<div class="card panel"><h3>Found in ${esc(name)}</h3>
    <p class="hint">Tick what to add. Bullets already in your bank are unticked. New bullets on an item you already have are added to that item. Nothing is saved until you press Save.</p>
    ${drafts.map((d) => `<div class="draft"><b>${esc(d.title)}</b> <span class="hint">${esc(d.org || d.stack)} ${esc(d.dates)}</span>
      ${d.match ? `<span class="chip">adds to your “${esc(d.match.title)}”</span>` : `<span class="chip ok">new ${d.section === "projects" ? "project" : "experience"}</span>`}
      <ul>${d.bullets.map((b, j) => `<li><label><input type="checkbox" data-d="${d.i}" data-b="${j}" ${b.dupe ? "" : "checked"}> <span>${esc(plain(b.t))}${b.dupe ? ' <span class="chip">already in bank</span>' : ""}</span></label></li>`).join("")}</ul></div>`).join("")}
    ${r.skills?.length ? `<p class="hint">Skills found: ${esc(r.skills.map((s) => s.category + ": " + s.items.join(", ")).join(" · "))}. Add them on the Skills card if you want.</p>` : ""}
    <button class="primary" id="addDrafts" type="button">Add ticked bullets</button> <button class="ghost" id="dropDrafts" type="button">Cancel</button></div>`;
  $("#dropDrafts").onclick = () => ($("#importPanel").innerHTML = "");
  $("#addDrafts").onclick = () => {
    let added = 0;
    for (const d of drafts) {
      const picked = $$(`#importPanel input[data-d="${d.i}"]:checked`).map((x) => d.bullets[+x.dataset.b].t);
      if (!picked.length) continue;
      let it = d.match;
      if (!it) {
        it = { ...Bank.newItem(d.section, d.title), org: d.org || "", location: d.location || "", dates: d.dates || "", stack: d.stack || "", bullets: [] };
        if (d.section !== "projects") it.kind = /intern/i.test(d.title) ? "internship" : "work";
        Bank.data[d.section].push(it);
        Bank.open_.add(it);
      }
      for (const t of picked) { it.bullets.push({ id: Bank.uniqueId(it.id + "-" + (it.bullets.length + 1)), text: t }); added++; }
    }
    if (added && !Bank.data.settings?.latex_bullets) Bank.data.settings = { ...(Bank.data.settings || {}), latex_bullets: true };
    $("#importPanel").innerHTML = `<p class="hint" style="margin-bottom:10px">Added ${added} bullet${added === 1 ? "" : "s"}. Review them, then press Save.</p>`;
    Bank.render();
    Bank.changed();
  };
}

// ---------------------------------------------------------------- settings
const Settings = {
  async open() {
    try {
      if (!Bank.data || !Bank.dirty) await Bank.load();
      const [tpls, h] = await Promise.all([api("/api/templates"), api("/api/health")]);
      const s = Bank.data.settings || {};
      $("#tplSelect").innerHTML = tpls.map((t) => `<option ${t === s.template ? "selected" : ""}>${esc(t)}</option>`).join("");
      $$("#settingsForm [data-s]").forEach((el) => {
        const v = s[el.dataset.s];
        if (el.type === "checkbox") el.checked = !!v;
        else el.value = v === undefined ? "" : String(v);
      });
      $("#pairInfo").textContent = h.paired ? "Paired with your Resume Tailor extension." : "Not paired yet. Open the extension once (click its icon) and it pairs itself.";
    } catch (e) { $("#settingsMsg").textContent = e.message; }
  },
};
$("#saveSettings").addEventListener("click", async () => {
  if (Bank.dirty) { $("#settingsMsg").textContent = "Save or discard your bank edits first."; return; }
  const s = { ...(Bank.data.settings || {}) };
  $$("#settingsForm [data-s]").forEach((el) => {
    const k = el.dataset.s;
    if (el.type === "checkbox") s[k] = el.checked;
    else if (el.type === "number") s[k] = +el.value;
    else if (k === "keep_bank_order") s[k] = el.value === "true" ? true : el.value === "false" ? false : el.value;
    else s[k] = el.value;
  });
  try {
    const r = await api("/api/bank.json", { method: "PUT", body: JSON.stringify({ bank: { ...Bank.data, settings: s }, version: Bank.version }) });
    Bank.data.settings = s; Bank.version = r.version;
    $("#settingsMsg").textContent = "Saved.";
    $("#rephrase").checked = !!s.rephrase;
  } catch (e) { $("#settingsMsg").textContent = e.message + (e.errors?.length ? " " + e.errors.map((x) => x.path + ": " + x.message).join("; ") : ""); }
});
$("#resetPair").addEventListener("click", async () => {
  await api("/api/pair/reset", { method: "POST" });
  $("#pairInfo").textContent = "Pairing reset. Open the extension to pair it again.";
});
$("#loadDiag").addEventListener("click", async () => {
  try { $("#diag").textContent = JSON.stringify(await api("/api/diagnostics"), null, 2); }
  catch (e) { $("#diag").textContent = e.message; }
});

// ---------------------------------------------------------------- start
(async function init() {
  loadHealth();
  setInterval(loadHealth, 60000);
  try { await Bank.load(); $("#rephrase").checked = !!Bank.data.settings?.rephrase; } catch { /* shown on the bank tab */ }
  show(location.hash.slice(1) || "tailor");
  const dl = document.createElement("datalist");
  dl.id = "itemIds";
  dl.innerHTML = Bank.data ? [...Bank.data.experiences, ...Bank.data.projects].map((x) => `<option value="${esc(x.id)}">`).join("") : "";
  document.body.append(dl);
})();
