// @ts-check
// Floating "Tailor resume" button on job postings and application forms.
// Never submits anything: it only reads the posting, makes a PDF, and (if you click Attach)
// puts the file in the resume upload box for you to check.
(() => {
  if (window.top !== window || /** @type {any} */ (window).__resumeTailorLoaded) return;
  /** @type {any} */ (window).__resumeTailorLoaded = true;

  /** False once the extension is reloaded or updated: this copy of the script is cut off from it. */
  const alive = () => { try { return !!chrome.runtime?.id; } catch { return false; } };
  /** A cut-off copy removes its button and stops watching the page, instead of throwing on every click.
   * The new copy loads when the tab is reloaded. */
  function teardown() {
    try { observer.disconnect(); } catch { /* not set up yet */ }
    host?.remove();
    host = null;
    visible = false;
  }
  /** @param {any} msg @returns {Promise<any>} */
  const send = (msg) => new Promise((res) => {
    if (!alive()) { teardown(); return res({ ok: false, error: "The extension was updated. Reload this page." }); }
    try { chrome.runtime.sendMessage(msg, (r) => res(r || { ok: false, error: chrome.runtime.lastError?.message || "No reply" })); }
    catch { res({ ok: false, error: "The extension was updated. Reload this page." }); }
  });
  /** @param {any} s */
  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c] || c));
  /** @param {string} s */
  const words = (s) => (s.trim() ? s.trim().split(/\s+/).length : 0);
  const pct = (/** @type {number|undefined} */ x) => (x == null ? "–" : Math.round(x * 100) + "%");

  /** @type {HTMLElement | null} */ let host = null;
  /** @type {ShadowRoot} */ let root;
  /** @type {any} */ let job = null;
  /** @type {any} */ let lastJob = null;
  /** @type {any} */ let draft = null;       // the planned resume you're editing (nothing compiled until Build PDF)
  let lastUrl = "", visible = false, polling = false, dismissedUrl = "";

  // ------------------------------------------------------------ UI
  const STYLE = `
  :host { all: initial; }
  [hidden] { display: none !important; }
  * { box-sizing: border-box; font-family: "Segoe UI Variable Text", "Segoe UI", system-ui, -apple-system, sans-serif; }
  .wrap { position: fixed; right: 20px; bottom: 96px; z-index: 2147483646; display: flex; flex-direction: column; align-items: flex-end; gap: 10px;
    --paper:#F5F7FA; --sheet:#fff; --ink:#18202E; --slate:#5A6578; --rule:#D8DEE7; --mark:#FFE14D; --error:#B8322A; --ok:#1F7A4D; --warn:#946200; --focus:#2F6FEB; }
  @media (prefers-color-scheme: dark) { .wrap { --paper:#12161E; --sheet:#1A202B; --ink:#E7EBF2; --slate:#97A1B3; --rule:#2C3443; --mark:#E8C93A; --error:#F07A6E; --ok:#5FD19B; --warn:#E0B341; --focus:#7AA5FF; } }
  button { font: inherit; cursor: pointer; }
  :focus-visible { outline: 2px solid var(--focus); outline-offset: 2px; }
  .pillbox { position: relative; display: flex; align-items: center; }
  .pill { display: flex; align-items: center; gap: 8px; background: #18202E; color: #fff; border: 0; border-radius: 999px;
    padding: 9px 15px 9px 11px; font-size: 13.5px; font-weight: 600; box-shadow: 0 6px 20px rgba(24,32,46,.28); touch-action: none; user-select: none; }
  .pill .mark { width: 18px; height: 18px; border-radius: 4px; background: #FFE14D; display: grid; place-items: center; }
  .pill .mark i { display: block; width: 10px; height: 2px; background: #18202E; box-shadow: 0 -4px 0 #18202E, 0 4px 0 #18202E; }
  .pill:hover { background: #26324A; }
  .pill.dragging { cursor: grabbing; opacity: .9; }
  .dismiss { position: absolute; top: -7px; left: -7px; width: 20px; height: 20px; border-radius: 50%; border: 0; background: #5A6578; color: #fff;
    font-size: 13px; line-height: 20px; padding: 0; opacity: 0; transition: opacity .15s; }
  .pillbox:hover .dismiss, .dismiss:focus-visible { opacity: 1; }
  .panel { width: 420px; max-width: calc(100vw - 40px); max-height: calc(100vh - 140px); overflow: auto; background: var(--paper); color: var(--ink);
    border: 1px solid var(--rule); border-radius: 12px; padding: 14px; font-size: 13px; line-height: 1.45; box-shadow: 0 12px 40px rgba(24,32,46,.25); }
  .head { display: flex; align-items: center; margin-bottom: 10px; }
  .head h2 { font-size: 14.5px; margin: 0; font-weight: 700; flex: 1; }
  .x { background: none; border: 0; color: var(--slate); font-size: 20px; line-height: 1; padding: 2px 6px; border-radius: 6px; }
  .row { display: grid; grid-template-columns: 1fr 1fr; gap: 8px; }
  label { display: flex; flex-direction: column; gap: 3px; color: var(--slate); margin-bottom: 8px; min-width: 0; font-size: 12px; }
  input, textarea { font: inherit; font-size: 13px; color: var(--ink); background: var(--sheet); border: 1px solid var(--rule); border-radius: 7px; padding: 7px 9px; width: 100%; }
  textarea { height: 140px; resize: vertical; }
  .src { color: var(--slate); margin: 0 0 10px; }
  .src.warn { color: var(--warn); }
  .link { background: none; border: 0; padding: 0; color: inherit; text-decoration: underline; font-size: 12px; }
  .primary { width: 100%; background: var(--ink); color: var(--paper); border: 0; border-radius: 8px; padding: 10px; font-weight: 600; font-size: 13.5px; }
  .primary:disabled { opacity: .55; cursor: default; }
  .secondary { width: 100%; background: var(--sheet); color: var(--ink); border: 1px solid var(--rule); border-radius: 8px; padding: 8px; font-weight: 500; font-size: 13px; }
  .msg { margin: 10px 0 0; white-space: pre-wrap; }
  .msg.err { color: var(--error); } .msg.ok { color: var(--ok); } .msg.warn { color: var(--warn); }
  .steps { list-style: none; margin: 10px 0 0; padding: 0; display: flex; flex-direction: column; gap: 3px; }
  .steps li { display: flex; gap: 7px; align-items: center; color: var(--slate); font-size: 12.5px; }
  .steps li.now { color: var(--ink); font-weight: 600; }
  .steps .t { margin-left: auto; font-size: 11.5px; }
  .result { margin-top: 12px; display: flex; flex-direction: column; gap: 8px; }
  .stats { display: flex; gap: 6px; flex-wrap: wrap; }
  .chip { padding: 1px 8px; border-radius: 99px; background: var(--sheet); border: 1px solid var(--rule); font-size: 11.5px; color: var(--slate); }
  .file { background: var(--mark); color: #18202E; padding: 1px 4px; word-break: break-all; font-weight: 500; }
  .editor { display: flex; flex-direction: column; gap: 6px; }
  .ed-head { display: flex; flex-direction: column; margin-bottom: 2px; }
  .ed-head small { color: var(--slate); font-size: 12px; }
  .editor h3 { margin: 6px 0 0; font-size: 11px; text-transform: uppercase; letter-spacing: .06em; color: var(--slate); }
  .card { background: var(--sheet); border: 1px solid var(--rule); border-left: 3px solid var(--rule); border-radius: 8px; padding: 8px 10px; }
  .card.changed { border-left-color: var(--mark); }
  .card.gone .nm { text-decoration: line-through; color: var(--slate); }
  .row1 { display: flex; gap: 8px; align-items: center; }
  .row1 .nm { flex: 1; min-width: 0; font-weight: 600; }
  .row1 .nm small { display: block; font-weight: 400; font-size: 11.5px; color: var(--slate); }
  .row1 select.act { max-width: 120px; }
  select.act { font: inherit; font-size: 12px; color: var(--ink); background: var(--paper); border: 1px solid var(--rule); border-radius: 6px; padding: 4px 6px; max-width: 175px; }
  select.act:disabled { opacity: .5; }
  .card .more { margin-top: 5px; font-size: 11.5px; color: var(--slate); text-decoration: none; }
  .bl { list-style: none; margin: 6px 0 0; padding: 0; display: flex; flex-direction: column; gap: 5px; }
  .bl li { display: flex; flex-direction: column; gap: 4px; padding: 6px 8px; border-radius: 6px; background: var(--paper); border: 1px solid transparent; }
  .bl li.changed { border-color: var(--mark); }
  .bl li.gone .tx { text-decoration: line-through; color: var(--slate); }
  .bl .tx { font-size: 12px; line-height: 1.35; }
  .bl select.act { max-width: 100%; align-self: flex-start; }
  .sec { display: flex; flex-direction: column; gap: 6px; }
  .grip { cursor: grab; color: var(--slate); user-select: none; font-size: 15px; line-height: 1; padding: 0 2px; }
  .grip:active { cursor: grabbing; }
  .chev { background: none; border: 0; color: var(--slate); padding: 0 2px; font-size: 12px; width: 16px; }
  .row1 .nm { cursor: pointer; }
  .card.dragging, .bl li.dragging { opacity: .35; }
  .bl { list-style: none; counter-reset: b; }
  .bl li { gap: 5px; }
  .brow { display: flex; gap: 6px; align-items: flex-start; }
  .brow .tx { flex: 1; }
  .bact { display: flex; gap: 8px; align-items: center; padding-left: 20px; }
  .icon { background: none; border: 0; color: var(--slate); padding: 0 3px; font-size: 13px; line-height: 1.2; border-radius: 4px; }
  .icon:hover { color: var(--ink); background: var(--sheet); }
  .tag { font-size: 10.5px; color: var(--warn); border: 1px solid var(--rule); border-radius: 99px; padding: 0 6px; white-space: nowrap; }
  .tedit { display: flex; flex-direction: column; gap: 5px; padding-left: 20px; }
  .tedit textarea { height: 64px; font-size: 12.5px; }
  .tedit small { color: var(--slate); font-size: 11px; }
  .tbtn { display: flex; gap: 6px; justify-content: flex-end; }
  .tbtn .primary, .tbtn .secondary { width: auto; padding: 6px 12px; font-size: 12.5px; }
  .editor.busy { opacity: .55; pointer-events: none; }
  .addrow select.act { width: 100%; max-width: none; border-style: dashed; background: transparent; color: var(--slate); padding: 7px 8px; }
  .addrow select.act:hover, .addrow select.act:focus { color: var(--ink); border-color: var(--slate); }
  .addb { margin-top: 6px; padding-left: 20px; }
  .addb select.act { padding: 5px 8px; font-size: 12px; }
  .buildbar { position: sticky; bottom: -14px; display: flex; gap: 10px; align-items: center; justify-content: flex-end; background: var(--paper); border-top: 1px solid var(--rule); padding: 10px 0 6px; z-index: 1; }
  .buildbar .primary { width: auto; padding: 9px 16px; }
  .foot { display: flex; justify-content: space-between; margin-top: 12px; padding-top: 10px; border-top: 1px solid var(--rule); color: var(--slate); font-size: 12px; }
  .spin { display: inline-block; width: 12px; height: 12px; border: 2px solid currentColor; border-right-color: transparent; border-radius: 50%; animation: s .7s linear infinite; flex: none; }
  @keyframes s { to { transform: rotate(360deg); } }
  @media (prefers-reduced-motion: reduce) { .spin { animation-duration: 2.4s; } .dismiss { transition: none; } }`;

  function mount() {
    if (host) return;
    host = document.createElement("resume-tailor-widget");
    root = host.attachShadow({ mode: "open" });
    root.innerHTML = `<style>${STYLE}</style>
      <div class="wrap">
        <section class="panel" hidden role="dialog" aria-label="Tailor resume">
          <div class="head"><h2>Tailor resume to this job</h2><button class="x" data-act="close" aria-label="Close">×</button></div>
          <div class="row">
            <label>Company <input id="company" autocomplete="off" maxlength="120"></label>
            <label>Role <input id="role" autocomplete="off" maxlength="200"></label>
          </div>
          <p class="src" id="src"></p>
          <label id="jdWrap" hidden>Job posting <textarea id="jd" placeholder="Paste the job description here" maxlength="60000"></textarea></label>
          <button class="primary" data-act="make">Make resume</button>
          <ol class="steps" id="steps" hidden></ol>
          <p class="msg" id="msg" role="status" aria-live="polite"></p>
          <div class="result" id="result"></div>
          <div class="foot">
            <button class="link" data-act="hideSite">Hide on this site</button>
            <button class="link" data-act="openApp">Open Resume Tailor</button>
          </div>
        </section>
        <div class="pillbox">
          <button class="dismiss" data-act="dismiss" aria-label="Hide the button on this page" title="Hide on this page">×</button>
          <button class="pill" data-act="toggle" title="Tailor resume (drag to move)"><span class="mark"><i></i></span>Tailor resume</button>
        </div>
      </div>`;
    root.addEventListener("click", onClick);
    root.addEventListener("input", (e) => {
      const t = /** @type {HTMLInputElement} */ (e.target);
      if (t.id === "company" || t.id === "role") delete t.dataset.auto; // keep what you typed
      if (t.id === "jd") { job = { ...(job || {}), source: "your edits", confidence: undefined }; renderSource(); }
    });
    root.addEventListener("keydown", (e) => {
      const ke = /** @type {KeyboardEvent} */ (e);
      if (ke.key === "Escape") closePanel();
      if (ke.key === "Enter" && (ke.metaKey || ke.ctrlKey)) make();
      e.stopPropagation(); // keep site shortcuts from firing while typing
    });
    enableDrag();
    enableEditorEvents();
    document.documentElement.appendChild(host);
  }

  /** @param {string} sel @returns {any} */
  const $ = (sel) => root.querySelector(sel);
  /** @param {string} sel @returns {any[]} */
  const $$ = (sel) => [...root.querySelectorAll(sel)];
  const panel = () => $(".panel");
  /** @param {string} html @param {string} [kind] */
  function setMsg(html, kind = "") { const m = $("#msg"); m.className = "msg " + kind; m.innerHTML = html; }

  // Drag the button anywhere along the right/bottom; position is remembered per site.
  function enableDrag() {
    const wrap = $(".wrap"), pill = $(".pill");
    if (alive()) chrome.storage.local.get("pos").then(({ pos = {} }) => {
      const p = pos[location.hostname];
      if (p) { wrap.style.right = p.right + "px"; wrap.style.bottom = p.bottom + "px"; }
    });
    let start = null, moved = false;
    pill.addEventListener("pointerdown", (/** @type {PointerEvent} */ e) => {
      start = { x: e.clientX, y: e.clientY, right: parseFloat(getComputedStyle(wrap).right), bottom: parseFloat(getComputedStyle(wrap).bottom) };
      moved = false;
      pill.setPointerCapture(e.pointerId);
    });
    pill.addEventListener("pointermove", (/** @type {PointerEvent} */ e) => {
      if (!start) return;
      const dx = e.clientX - start.x, dy = e.clientY - start.y;
      if (!moved && Math.hypot(dx, dy) < 5) return;
      moved = true;
      pill.classList.add("dragging");
      wrap.style.right = Math.min(Math.max(4, start.right - dx), innerWidth - 120) + "px";
      wrap.style.bottom = Math.min(Math.max(4, start.bottom - dy), innerHeight - 50) + "px";
    });
    pill.addEventListener("pointerup", async () => {
      pill.classList.remove("dragging");
      if (moved && alive()) {
        const { pos = {} } = await chrome.storage.local.get("pos");
        pos[location.hostname] = { right: parseFloat(wrap.style.right), bottom: parseFloat(wrap.style.bottom) };
        await chrome.storage.local.set({ pos });
      }
      start = null;
    });
    // a drag isn't a click
    pill.addEventListener("click", (/** @type {Event} */ e) => { if (moved) { e.stopImmediatePropagation(); moved = false; } }, true);
  }

  function show() { mount(); if (host) host.style.display = ""; visible = true; }
  function hide() { if (host) host.style.display = "none"; visible = false; }
  function closePanel() { if (host) { panel().hidden = true; $(".pillbox").hidden = false; } }

  async function openPanel() {
    panel().hidden = false;
    $(".pillbox").hidden = true;
    const active = await send({ type: "activeJob" });
    if (active.ok && active.active && !polling) {
      setMsg('<span class="spin"></span> Picking up the resume you started…');
      return follow(active.active.id);
    }
    if (polling) return;
    $("#result").innerHTML = "";
    setMsg('<span class="spin"></span> Reading the job posting…');
    const [health, found] = await Promise.all([send({ type: "health" }), gatherJob()]);
    job = found;
    const co = $("#company"), ro = $("#role");
    if (!co.value || co.dataset.auto) { co.value = job.company || ""; co.dataset.auto = "1"; }
    if (!ro.value || ro.dataset.auto) { ro.value = job.role || ""; ro.dataset.auto = "1"; }
    $("#jd").value = job.jd || "";
    renderSource();
    if (!health.ok) {
      setMsg("Resume Tailor isn't running.\nIt starts when you log in; or double-click start.bat in your resume-tailor folder.", "err");
    } else if (!health.paired) {
      setMsg("This extension isn't paired with your server. Open Resume Tailor → Settings → Reset pairing, then try again.", "err");
    } else if (!job.jd) {
      $("#jdWrap").hidden = false;
      setMsg("Couldn't find the posting on this page. Paste it in the box, or highlight it on the page and reopen.", "err");
      $("#jd").focus();
    } else {
      setMsg(health.problem ? `${esc(health.problem)}\nUntil then, the built-in matcher is used.` : "", health.problem ? "warn" : "");
      $(".primary[data-act=make]").focus();
    }
  }

  function renderSource() {
    const n = words($("#jd").value);
    const conf = job?.confidence;
    const low = conf != null && conf < 0.6;
    const src = $("#src");
    src.className = "src" + (low ? " warn" : "");
    src.innerHTML = n
      ? `${n} words from ${esc(job?.source || "the box")}` +
        (conf != null ? ` · ${low ? "may be incomplete" : "looks complete"} (${pct(conf)})${low && job.issues?.length ? ": " + esc(job.issues.join(", ")) : ""}` : "") +
        `. <button class="link" data-act="editJd">${$("#jdWrap").hidden ? "Check text" : "Hide text"}</button>`
      : "";
    if (low) $("#jdWrap").hidden = false;
  }

  // Find the posting: this page, then the posting page this apply form came from, then what this tab saw earlier.
  async function gatherJob() {
    const j = extractJob();
    if (j.jd.length >= 200 && j.confidence >= 0.5) return j;
    const parent = location.href.replace(/\/(apply|application|applications?\/new)(\/.*)?([?#].*)?$/i, "");
    if (parent !== location.href && new URL(parent).origin === location.origin) {
      try {
        const html = await (await fetch(parent, { credentials: "include" })).text();
        const doc = new DOMParser().parseFromString(html, "text/html");
        const pj = extractJob(doc, new URL(parent));
        if (pj.jd.length >= 200) return { ...pj, company: j.company || pj.company, role: pj.role || j.role, source: "the posting page" };
      } catch { /* offline or blocked */ }
    }
    const { job: saved } = await send({ type: "recallJob" });
    if (saved?.jd && (!j.jd || j.confidence < 0.5)) return { ...saved, source: "the posting you viewed earlier in this tab", confidence: 0.75, issues: [] };
    return j;
  }

  /** @param {boolean} [forceText] @param {boolean} [fresh] */
  async function make(forceText = false, fresh = false) {
    if (polling) return;
    const jd = $("#jd").value.trim();
    if (words(jd) < 10) { $("#jdWrap").hidden = false; renderSource(); return setMsg("Paste the full job description first.", "err"); }
    $("#result").innerHTML = "";
    setMsg("");
    draft = null; lastJob = null;
    const r = await send({ type: "startJob", payload: {
      jd, company: $("#company").value.trim(), role: $("#role").value.trim(), url: location.href, mode: "plan",
      source: job?.source || "", confidence: job?.confidence ?? null, force_text: forceText, force: fresh } });
    if (!r.ok) return setMsg(esc(r.error), "err");
    await follow(r.job.id);
  }

  /** Poll the server until the job ends. Safe to call again after the panel or page is reopened.
   * Plan jobs end in the preview editor; build jobs end with Download. @param {string} id @param {boolean} [building] */
  async function follow(id, building = false) {
    polling = true;
    const btn = $(".primary[data-act=make]") || $(".secondary[data-act=make]") || $("[data-act=startOver]");
    const area = (building && $("#built")) || $("#result");
    if (!building && btn) { btn.disabled = true; btn.innerHTML = '<span class="spin"></span> Working…'; }
    const build = $("[data-act=build]");
    if (building && build) { build.disabled = true; build.innerHTML = '<span class="spin"></span> Building…'; }
    area.innerHTML = '<button class="secondary" data-act="cancel">Cancel</button>';
    $("#steps").hidden = false;
    let r;
    try {
      for (;;) {
        r = await send({ type: "jobStatus", id, autoDownload: false });   // you check it first, then download
        if (!r.ok) { setMsg(esc(r.error), "err"); break; }
        renderSteps(r.job);
        if (["completed", "failed", "cancelled"].includes(r.job.state)) break;
        await new Promise((res) => setTimeout(res, 700));
      }
    } finally {
      polling = false;
      if (btn) { btn.disabled = false; btn.className = "primary"; btn.textContent = "Make resume"; btn.dataset.act = "make"; }
      if (build) { build.disabled = false; build.textContent = "Build PDF"; }
    }
    area.innerHTML = "";
    if (!r?.ok) return;
    const j = r.job;
    if (j.state === "cancelled") return setMsg("Cancelled.", "warn");
    if (j.state === "failed") {
      const e = j.error || {};
      setMsg(esc(e.message || "Failed."), "err");
      if (e.code === "low_confidence") {
        $("#jdWrap").hidden = false;
        $("#result").innerHTML = '<button class="secondary" data-act="force">This is the full posting. Make it anyway</button>';
      }
      return;
    }
    $("#steps").hidden = true;   // done: give the space to the preview
    const res = j.result;
    if (!$("#company").value) $("#company").value = res.company || "";
    if (!$("#role").value) $("#role").value = res.role || "";
    if (btn) { btn.className = "secondary"; btn.textContent = "Start over (new plan)"; btn.dataset.act = "startOver"; }
    if (res.run_id) showBuilt(j);
    else await showDraft(res.draft_id, res);
  }

  // ------------------------------------------------------------ preview first, then build once
  /** @param {string} t @param {number} n */
  const cut = (t, n) => (t.length > n ? t.slice(0, n - 1) + "…" : t);
  const SECTION = { experiences: "Experience", projects: "Projects" };
  /** @type {Set<string>} */ const openItems = new Set();
  let editing = false;

  /** @param {string} id @param {any} [res] */
  async function showDraft(id, res) {
    const r = await send({ type: "draft", id });
    if (!r.ok) return setMsg(esc(r.error), "err");
    draft = r.draft;
    const st = (res && res.stats) || draft.stats || {};
    const warnings = ((res && res.warnings) || []).map((/** @type {string} */ w) => `\n${esc(w)}`).join("");
    setMsg(`Preview ready. Change anything below, then <b>Build PDF</b>.${warnings}`, warnings ? "warn" : "ok");
    $("#result").innerHTML = `
      <div class="stats"><span class="chip">${st.llm_requests ? `${st.llm_requests} Claude call` : st.llm_cached ? "Claude answer reused" : "no Claude usage"}</span>
        <span class="chip">${st.seconds ?? "?"}s to plan</span><span class="chip" id="linesChip"></span></div>
      <div class="editor" id="editor"></div>
      <div class="buildbar"><button class="link" data-act="resetDraft" id="resetDraft">Undo all changes</button>
        <button class="primary" data-act="build">Build PDF</button></div>
      <div class="result" id="built"></div>`;
    renderDraft();
  }

  function renderDraft() {
    const box = $("#editor");
    if (!box || !draft) return;
    const pv = draft.preview, opts = draft.options;
    $("#linesChip").textContent = `~${Math.round(pv.lines_used)} of ${Math.round(pv.budget)} lines`;
    $("#resetDraft").hidden = !draft.changed;
    box.innerHTML = `<div class="ed-head"><b>On this resume</b><small>Drag ⠿ to reorder, ✎ to reword a bullet, use a dropdown to remove or replace, or + Add for another item or point. Each change updates in about a second; nothing compiles until Build PDF.</small></div>` +
      Object.entries(SECTION).map(([sec, label]) => {
        const rows = pv[sec] || [];
        // items you could add: unused, printable, and not the same work as something already on the page
        const addable = (opts[sec]?.unused || []).filter((/** @type {any} */ u) => !u.blocked && !u.overlaps.length);
        if (!rows.length && !addable.length) return "";
        const meta = Object.fromEntries((opts[sec]?.on_page || []).map((/** @type {any} */ it) => [it.id, it]));
        const noun = sec === "experiences" ? "an experience" : "a project";
        return `<h3>${label}</h3><div class="sec" data-section="${sec}">${rows.map((/** @type {any} */ it) =>
          card(it, meta[it.id] || { spare: [] }, opts[sec]?.unused || [])).join("")}</div>` +
          (addable.length ? `<div class="addrow"><select class="act add" data-kind="add" data-section="${sec}" aria-label="Add ${noun}">
            <option value="">+ Add ${noun}…</option><option value="any">Best fit for this job</option>
            ${addable.map((/** @type {any} */ u) => `<option value="${esc(u.id)}">${esc(cut(u.title + (u.org ? ", " + u.org : ""), 60))}</option>`).join("")}
          </select></div>` : "");
      }).join("");
  }

  /** @param {any} it @param {any} meta @param {any[]} unused */
  function card(it, meta, unused) {
    const open = openItems.has(it.id);
    const cands = unused.filter((u) => !u.blocked && u.overlaps.every((/** @type {string} */ o) => o === it.id));
    const spare = (meta.spare || []).filter((/** @type {any} */ o) => !o.blocked);
    const bl = it.bullets.map((/** @type {any} */ b) => {
      const swaps = spare.filter((/** @type {any} */ o) => !o.same_as || o.same_as === b.id);
      return `<li data-bullet="${esc(b.id)}" data-item="${esc(it.id)}">
        <div class="brow"><span class="grip" title="Drag to reorder">⠿</span>
          <span class="tx">${esc(b.text)}${b.edited ? ' <span class="tag">edited</span>' : ""}</span>
          <button class="icon" data-act="editText" title="Edit wording" aria-label="Edit wording">✎</button></div>
        <div class="bact"><select class="act" data-kind="bullet" data-out="${esc(b.id)}" aria-label="Change this bullet">
            <option value="keep">Keep</option><option value="remove">Remove</option>
            ${swaps.length ? `<optgroup label="Swap for">${swaps.map((/** @type {any} */ o) =>
              `<option value="${esc(o.id)}">${esc(cut((o.same_as ? "Other wording: " : "") + o.text, 80))}</option>`).join("")}</optgroup>` : ""}
          </select>${b.edited ? '<button class="link" data-act="revertText">Undo edit</button>' : ""}</div></li>`;
    }).join("");
    return `<div class="card" data-item="${esc(it.id)}">
      <div class="row1"><span class="grip" title="Drag to reorder">⠿</span>
        <button class="chev" data-act="toggleItem" aria-expanded="${open}" aria-label="Show bullets">${open ? "▾" : "▸"}</button>
        <div class="nm" data-act="toggleItem">${esc(it.title)}<small>${it.org ? esc(it.org) + " · " : ""}${it.bullets.length} bullet${it.bullets.length === 1 ? "" : "s"}${spare.length ? ` · ${spare.length} unused` : ""}</small></div>
        <select class="act" data-kind="item" data-out="${esc(it.id)}" aria-label="Change ${esc(it.title)}">
          <option value="keep">Keep</option><option value="remove">Remove</option>
          ${cands.length ? `<optgroup label="Replace with"><option value="any">Best fit for this job</option>${cands.map((u) =>
            `<option value="${esc(u.id)}">${esc(cut(u.title + (u.org ? ", " + u.org : ""), 48))}</option>`).join("")}</optgroup>` : ""}
        </select></div>
      <ol class="bl" data-item="${esc(it.id)}" ${open ? "" : "hidden"}>${bl}</ol>${addPoint(it, spare, open)}</div>`;
  }

  /** "+ Add a point" under an open card: the item's unused points (not another wording of one shown).
   * @param {any} it @param {any[]} spare @param {boolean} open */
  function addPoint(it, spare, open) {
    const fresh = spare.filter((/** @type {any} */ o) => !o.same_as);
    if (!fresh.length) return "";
    return `<div class="addrow addb" ${open ? "" : "hidden"}><select class="act add" data-kind="addBullet" data-item="${esc(it.id)}" aria-label="Add a point to ${esc(it.title)}">
      <option value="">+ Add a point…</option><option value="any">Best fit for this job</option>
      ${fresh.map((/** @type {any} */ o) => `<option value="${esc(o.id)}">${esc(cut(o.text, 80))}</option>`).join("")}
    </select></div>`;
  }

  /** Send one change, re-plan, redraw. @param {any} body @param {string} what */
  async function edit(body, what) {
    if (!draft || editing || polling) return;
    editing = true;
    $("#editor")?.classList.add("busy");
    setMsg(`<span class="spin"></span> ${esc(what)}…`);
    const r = await send({ type: "draftEdit", id: draft.id, body });
    editing = false;
    $("#editor")?.classList.remove("busy");
    if (!r.ok) { setMsg(esc(r.error), "err"); renderDraft(); return; }
    draft = r.draft;
    renderDraft();
    if (lastJob) {            // the PDF you built no longer matches
      lastJob = null;
      $("#built").innerHTML = '<p class="msg warn">Changed since your last build. Build PDF again to download.</p>';
    }
    setMsg("Updated. Change more, or <b>Build PDF</b>.", "ok");
  }

  /** @param {HTMLSelectElement} sel */
  function onSelect(sel) {
    if (sel.dataset.kind === "addBullet") {
      if (!sel.value) return;
      return edit({ add_bullets: [{ item: sel.dataset.item, in: sel.value }] }, "Adding a point");
    }
    if (sel.dataset.kind === "add") {
      if (!sel.value) return;
      const label = sel.selectedOptions[0]?.textContent || "";
      return edit({ add: [{ section: sel.dataset.section, in: sel.value }] },
        sel.value === "any" ? "Adding the best fit" : `Adding ${cut(label, 40)}`);
    }
    if (sel.value === "keep") return;
    const v = sel.value === "remove" ? null : sel.value;
    const kind = sel.dataset.kind;
    const label = sel.selectedOptions[0]?.textContent || "";
    edit(kind === "item" ? { items: [{ out: sel.dataset.out, in: v }] } : { bullets: [{ out: sel.dataset.out, in: v }] },
      v === null ? "Removing" : v === "any" ? "Finding the best fit" : `Swapping in ${cut(label, 40)}`);
  }

  async function build() {
    if (!draft || polling || editing) return;
    const r = await send({ type: "draftBuild", id: draft.id, url: location.href });
    if (!r.ok) return setMsg(esc(r.error), "err");
    setMsg('<span class="spin"></span> Building the PDF and fitting one page…');
    await follow(r.job.id, true);
  }

  /** @param {any} j */
  function showBuilt(j) {
    lastJob = j;
    const res = j.result, sc = res.scorecard || {};
    const fc = res.fit_changes || { dropped: [], added: [] };
    const fit = fc.dropped.length || fc.added.length
      ? `\nTo fit one page, the layout step ${[fc.dropped.length ? `left out: ${fc.dropped.map((/** @type {any} */ b) => `“${cut(b.text, 60)}”`).join(", ")}` : "",
          fc.added.length ? `added ${fc.added.length} bullet${fc.added.length === 1 ? "" : "s"}` : ""].filter(Boolean).join("; ")}.`
      : "";
    const warnings = (res.warnings || []).map((/** @type {string} */ w) => `\n${esc(w)}`).join("");
    setMsg(`Ready: <span class="file">${esc(res.filename)}</span>.${esc(fit)}${warnings}`, fit || warnings || !sc.one_page ? "warn" : "ok");
    const canAttach = !!findResumeInput();
    const html = `
      <div class="stats"><span class="chip">${pct(sc.supported_coverage)} of what you can show</span><span class="chip">${sc.one_page ? "1 page" : "over 1 page"}</span><span class="chip">${res.stats?.seconds ?? "?"}s to build</span></div>
      <button class="primary" data-act="download">${sc.one_page ? "Download" : "Download anyway (over 1 page)"}</button>
      ${canAttach ? '<button class="secondary" data-act="attach">Attach to this application</button>' : ""}
      <button class="secondary" data-act="openPdf">Open PDF</button>
      <button class="secondary" data-act="overleaf">Edit in Overleaf</button>`;
    if ($("#built")) $("#built").innerHTML = html; else $("#result").innerHTML = html;
  }

  // drag to reorder: items within their section, bullets within their item
  /** @type {HTMLElement | null} */ let dragEl = null;
  /** @type {string} */ let dragBefore = "";
  /** @param {HTMLElement} el */
  const siblingsOf = (el) => [...(/** @type {HTMLElement} */ (el.parentElement)).children].filter((x) => x.matches(el.matches("li") ? "li" : ".card"));
  function enableEditorEvents() {
    root.addEventListener("pointerdown", (/** @type {any} */ e) => {
      const g = e.target.closest(".grip");
      if (!g || !draft || editing) return;
      const el = g.closest("li") || g.closest(".card");
      el.draggable = true;
    });
    root.addEventListener("dragstart", (/** @type {any} */ e) => {
      const el = e.target.closest?.("li[draggable=true], .card[draggable=true]");
      if (!el) return;
      e.stopPropagation();
      dragEl = el;
      dragBefore = siblingsOf(el).map((x) => x.dataset.bullet || x.dataset.item).join(",");
      e.dataTransfer.effectAllowed = "move";
      e.dataTransfer.setData("text/plain", "");
      requestAnimationFrame(() => el.classList.add("dragging"));
    });
    root.addEventListener("dragover", (/** @type {any} */ e) => {
      if (!dragEl) return;
      const over = e.target.closest?.(dragEl.matches("li") ? "li" : ".card");
      if (!over || over === dragEl || over.parentElement !== dragEl.parentElement) return;
      e.preventDefault();
      const r = over.getBoundingClientRect();
      over.parentElement.insertBefore(dragEl, e.clientY < r.top + r.height / 2 ? over : over.nextSibling);
    });
    root.addEventListener("drop", (/** @type {any} */ e) => { if (dragEl) e.preventDefault(); });
    root.addEventListener("dragend", () => {
      if (!dragEl) return;
      const el = dragEl;
      dragEl = null;
      el.classList.remove("dragging");
      el.draggable = false;
      const ids = siblingsOf(el).map((x) => x.dataset.bullet || x.dataset.item);
      if (ids.join(",") === dragBefore) return;
      if (el.matches("li")) edit({ bullet_order: { [el.dataset.item || ""]: ids } }, "Reordering bullets");
      else edit({ order: { [el.closest(".sec")?.dataset.section || "experiences"]: ids } }, "Reordering");
    });
    root.addEventListener("change", (/** @type {any} */ e) => {
      if (e.target.matches?.("#editor select.act")) onSelect(e.target);
    });
  }

  /** @param {HTMLElement} li */
  function openTextEditor(li) {
    if (li.querySelector("textarea")) return;
    const bid = li.dataset.bullet || "";
    const b = draft.preview.experiences.concat(draft.preview.projects).flatMap((/** @type {any} */ it) => it.bullets)
      .find((/** @type {any} */ x) => x.id === bid);
    const box = document.createElement("div");
    box.className = "tedit";
    box.innerHTML = `<textarea maxlength="400" aria-label="Bullet wording">${esc(b?.text || "")}</textarea>
      <small>Your words for this resume only (your bank isn't changed). Keep it to about one line.</small>
      <div class="tbtn"><button class="secondary" data-act="cancelText">Cancel</button><button class="primary" data-act="saveText">Save</button></div>`;
    li.querySelector(".brow")?.after(box);
    const ta = /** @type {HTMLTextAreaElement} */ (box.querySelector("textarea"));
    ta.focus();
    ta.setSelectionRange(ta.value.length, ta.value.length);
  }

  const ICON = { completed: "✓", failed: "✕", cancelled: "✕" };
  /** @param {any} j */
  function renderSteps(j) {
    const seen = new Map();
    for (const p of j.progress || []) seen.set(p.state, p);
    const list = [...seen.values()];
    $("#steps").innerHTML = list.map((p, i) => {
      const last = i === list.length - 1;
      const icon = /** @type {any} */ (ICON)[p.state] || (last ? '<span class="spin"></span>' : "✓");
      return `<li class="${last ? "now" : ""}">${icon} ${esc(p.message)}<span class="t">${p.t}s</span></li>`;
    }).join("") || `<li class="now"><span class="spin"></span> ${esc(j.message)}</li>`;
  }

  async function attach() {
    const input = findResumeInput();
    if (!input || !lastJob) return setMsg("No resume upload box found on this page.", "err");
    const r = await send({ type: "pdfBytes", runId: lastJob.result.run_id });
    if (!r.ok) return setMsg(esc(r.error), "err");
    const bytes = Uint8Array.from(atob(r.base64), (c) => c.charCodeAt(0));
    const file = new File([bytes], lastJob.result.filename, { type: "application/pdf" });
    const dt = new DataTransfer();
    dt.items.add(file);
    try {
      input.files = dt.files;
      input.dispatchEvent(new Event("input", { bubbles: true }));
      input.dispatchEvent(new Event("change", { bubbles: true }));
      setMsg(`Attached <span class="file">${esc(lastJob.result.filename)}</span>. Check the form shows it. Nothing was submitted.`, "ok");
      const a = $('[data-act="attach"]');
      if (a) { a.textContent = "Attached"; a.disabled = true; a.className = "secondary"; }
    } catch {
      setMsg("This site blocked attaching. Upload the downloaded file by hand.", "err");
    }
  }

  /** @param {Event} e */
  async function onClick(e) {
    const act = /** @type {HTMLElement} */ (e.target).closest("[data-act]")?.getAttribute("data-act");
    if (!act) return;
    if (act === "toggle") panel().hidden ? openPanel() : closePanel();
    if (act === "close") closePanel();
    if (act === "make") make();
    if (act === "startOver") make(false, true);
    if (act === "force") make(true);
    if (act === "cancel" && polling) {
      const a = await send({ type: "activeJob" });
      if (a.active) await send({ type: "cancelJob", id: a.active.id });
      setMsg('<span class="spin"></span> Cancelling…');
    }
    if (act === "attach") attach();
    if (act === "download" && lastJob && !$('[data-act="download"]')?.disabled) {
      const r = await send({ type: "download", id: lastJob.id });
      if (r.ok) setMsg(`Downloaded <span class="file">${esc(lastJob.result.filename)}</span>.`, "ok");
      else setMsg(esc(r.error), "err");
    }
    const el = /** @type {HTMLElement} */ (/** @type {HTMLElement} */ (e.target).closest("[data-act]"));
    if (act === "build") build();
    if (act === "resetDraft" && draft && !editing) {
      editing = true;
      const r = await send({ type: "draftReset", id: draft.id });
      editing = false;
      if (r.ok) { draft = r.draft; renderDraft(); setMsg("Back to the original plan.", "ok"); } else setMsg(esc(r.error), "err");
    }
    if (act === "toggleItem") {
      const c = /** @type {HTMLElement} */ (el.closest(".card"));
      const id = c.dataset.item || "";
      openItems.has(id) ? openItems.delete(id) : openItems.add(id);
      const ol = /** @type {HTMLElement} */ (c.querySelector(".bl"));
      ol.hidden = !openItems.has(id);
      const addb = /** @type {HTMLElement | null} */ (c.querySelector(".addb"));
      if (addb) addb.hidden = ol.hidden;
      const chev = c.querySelector(".chev");
      if (chev) { chev.textContent = ol.hidden ? "▸" : "▾"; chev.setAttribute("aria-expanded", String(!ol.hidden)); }
    }
    if (act === "editText") openTextEditor(/** @type {HTMLElement} */ (el.closest("li")));
    if (act === "cancelText") el.closest(".tedit")?.remove();
    if (act === "saveText") {
      const li = /** @type {HTMLElement} */ (el.closest("li"));
      const text = /** @type {HTMLTextAreaElement} */ (li.querySelector("textarea")).value.trim();
      if (text) edit({ texts: { [li.dataset.bullet || ""]: text } }, "Saving your wording");
    }
    if (act === "revertText") edit({ texts: { [/** @type {HTMLElement} */ (el.closest("li")).dataset.bullet || ""]: null } }, "Undoing your edit");
    if (act === "openPdf" && lastJob) send({ type: "openRun", runId: lastJob.result.run_id, kind: "pdf" });
    if (act === "overleaf" && lastJob) send({ type: "openRun", runId: lastJob.result.run_id, kind: "overleaf" });
    if (act === "openApp") send({ type: "openApp" });
    if (act === "editJd") { $("#jdWrap").hidden = !$("#jdWrap").hidden; renderSource(); }
    if (act === "dismiss") { dismissedUrl = location.href; hide(); }
    if (act === "hideSite") {
      if (!alive()) return teardown();
      const { hiddenSites = [] } = await chrome.storage.local.get("hiddenSites");
      await chrome.storage.local.set({ hiddenSites: [...new Set([...hiddenSites, location.hostname])] });
      hide();
    }
  }

  // ------------------------------------------------------------ when to show
  /** The panel is open: you're using it, so nothing closes or hides it automatically. */
  const panelOpen = () => !!host && !panel().hidden;

  async function check() {
    if (!alive()) return teardown();
    const { showButton = true, hiddenSites = [] } = await chrome.storage.local.get(["showButton", "hiddenSites"]);
    if (!showButton || hiddenSites.includes(location.hostname) || dismissedUrl === location.href) {
      if (!panelOpen()) hide();   // opened by hand (toolbar button): it stays even where the button is hidden
      return;
    }
    const isJob = looksLikeJobPage();
    if (isJob && !visible) show();
    if (!isJob && visible && !panelOpen() && !polling) hide();
    if (isJob) {
      const j = extractJob();
      if (j.jd.length >= 200 && j.confidence >= 0.6) send({ type: "rememberJob", job: { ...j, url: location.href } });
    }
  }

  function resetPanel() {
    $("#company").value = ""; $("#role").value = ""; $("#jd").value = ""; $("#result").innerHTML = ""; $("#steps").hidden = true;
    setMsg(""); lastJob = null; job = null; draft = null; openItems.clear();
    const b = $('[data-act="make"], [data-act="startOver"]');
    if (b) { b.className = "primary"; b.textContent = "Make resume"; b.dataset.act = "make"; }
  }

  function onUrlChange() {
    if (location.href === lastUrl) return;
    lastUrl = location.href;
    checks = 0;
    if (panelOpen()) {
      // Many job sites rewrite their address on their own (tracking or scroll parameters). Never close the
      // panel for that. If it's idle, look again shortly: a different posting (e.g. a LinkedIn job switch)
      // gets loaded into the open panel; the same posting is left alone. Mid-job or mid-edit: untouched.
      if (!polling && !draft && !lastJob) setTimeout(refreshPosting, 700);
      return;
    }
    if (host && !polling) { resetPanel(); closePanel(); }
    check();
  }

  async function refreshPosting() {
    if (!panelOpen() || polling || draft || lastJob) return;
    const j = extractJob();
    if (j.jd.length < 200 || j.jd.trim() === $("#jd").value.trim()) return;   // nothing new to read here
    resetPanel();
    openPanel();
  }

  // Single-page sites (LinkedIn, Workday, Simplify) change the URL and render late: watch the DOM,
  // debounced, instead of polling on a timer.
  let timer = 0;
  const schedule = (ms = 400) => { clearTimeout(timer); timer = /** @type {any} */ (setTimeout(() => { onUrlChange(); if (!visible) check(); }, ms)); };
  let checks = 0;
  const observer = new MutationObserver(() => {
    if (!alive()) return teardown();
    // some sites re-render the whole document and drop anything they didn't create: put the widget back
    if (host && visible && !host.isConnected) document.documentElement.appendChild(host);
    if (location.href !== lastUrl || (!visible && checks++ < 40)) schedule();
  });
  observer.observe(document.documentElement, { childList: true, subtree: true });
  addEventListener("popstate", () => schedule(50));
  /** @type {any} */ const nav = /** @type {any} */ (window).navigation;
  if (nav?.addEventListener) nav.addEventListener("navigatesuccess", () => schedule(150));
  chrome.storage.onChanged.addListener((c) => { if (c.showButton || c.hiddenSites) check(); });
  // the toolbar popup asks the page to open the panel (no "scripting" permission needed)
  chrome.runtime.onMessage.addListener((msg, sender, reply) => {
    if (sender.id !== chrome.runtime.id || msg?.type !== "openPanel") return false;
    dismissedUrl = "";
    show();
    if (panel().hidden) openPanel();
    reply({ ok: true });
    return false;
  });
  onUrlChange();
})();
