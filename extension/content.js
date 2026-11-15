// @ts-check
// Floating "Tailor resume" button on job postings and application forms.
// Never submits anything: it only reads the posting, makes a PDF, and (if you click Attach)
// puts the file in the resume upload box for you to check.
(() => {
  if (window.top !== window || /** @type {any} */ (window).__resumeTailorLoaded) return;
  /** @type {any} */ (window).__resumeTailorLoaded = true;

  /** @param {any} msg @returns {Promise<any>} */
  const send = (msg) => new Promise((res) => {
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
  /** @type {any} */ let lastItems = null;   // what's on the page + what could replace each item
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
  .panel { width: 370px; max-width: calc(100vw - 40px); max-height: calc(100vh - 140px); overflow: auto; background: var(--paper); color: var(--ink);
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
  .items { background: var(--sheet); border: 1px solid var(--rule); border-radius: 8px; padding: 8px 10px; }
  .items h3 { margin: 6px 0 2px; font-size: 11.5px; text-transform: uppercase; letter-spacing: .05em; color: var(--slate); }
  .items h3:first-child { margin-top: 0; }
  .items ul { list-style: none; margin: 0; padding: 0; }
  .items li { padding: 5px 0; border-top: 1px solid var(--rule); }
  .items h3 + ul li:first-child { border-top: 0; }
  .items .it { display: flex; gap: 8px; align-items: baseline; }
  .items .nm { font-weight: 600; flex: 1; min-width: 0; }
  .items .nm small { font-weight: 400; color: var(--slate); }
  .items .rep { display: flex; flex-direction: column; gap: 6px; margin-top: 6px; }
  .items select { font: inherit; font-size: 12.5px; color: var(--ink); background: var(--paper); border: 1px solid var(--rule); border-radius: 6px; padding: 5px; width: 100%; }
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
    document.documentElement.appendChild(host);
  }

  /** @param {string} sel @returns {any} */
  const $ = (sel) => root.querySelector(sel);
  const panel = () => $(".panel");
  /** @param {string} html @param {string} [kind] */
  function setMsg(html, kind = "") { const m = $("#msg"); m.className = "msg " + kind; m.innerHTML = html; }

  // Drag the button anywhere along the right/bottom; position is remembered per site.
  function enableDrag() {
    const wrap = $(".wrap"), pill = $(".pill");
    chrome.storage.local.get("pos").then(({ pos = {} }) => {
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
      if (moved) {
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

  /** @param {boolean} [forceText] */
  async function make(forceText = false) {
    if (polling) return;
    const jd = $("#jd").value.trim();
    if (words(jd) < 10) { $("#jdWrap").hidden = false; renderSource(); return setMsg("Paste the full job description first.", "err"); }
    $("#result").innerHTML = "";
    setMsg("");
    const r = await send({ type: "startJob", payload: {
      jd, company: $("#company").value.trim(), role: $("#role").value.trim(), url: location.href,
      source: job?.source || "", confidence: job?.confidence ?? null, force_text: forceText } });
    if (!r.ok) return setMsg(esc(r.error), "err");
    await follow(r.job.id);
  }

  /** Poll the server until the job ends. Safe to call again after the panel or page is reopened. @param {string} id */
  async function follow(id) {
    polling = true;
    const btn = $(".primary[data-act=make]") || $(".secondary[data-act=make]");
    btn.disabled = true;
    btn.innerHTML = '<span class="spin"></span> Working…';
    $("#result").innerHTML = '<button class="secondary" data-act="cancel">Cancel</button>';
    $("#steps").hidden = false;
    let r;
    try {
      for (;;) {
        r = await send({ type: "jobStatus", id, autoDownload: false });   // you check it first, then download
        if (!r.ok) { setMsg(esc(r.error), "err"); break; }
        renderSteps(r.job);
        if (["completed", "failed", "cancelled"].includes(r.job.state)) break;
        await new Promise((res) => setTimeout(res, 800));
      }
    } finally {
      polling = false;
      btn.disabled = false;
      btn.textContent = "Make resume";
      btn.className = "primary";
    }
    $("#result").innerHTML = "";
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
    lastJob = j;
    const res = j.result, sc = res.scorecard || {}, st = res.stats || {};
    if (!$("#company").value) $("#company").value = res.company || "";
    if (!$("#role").value) $("#role").value = res.role || "";
    btn.className = "secondary";
    btn.textContent = "Make another version";
    const warnings = (res.warnings || []).map((/** @type {string} */ w) => `\n${esc(w)}`).join("");
    setMsg(`Ready: <span class="file">${esc(res.filename)}</span>. Check what's on it, then download.${warnings}`,
      warnings || !sc.one_page ? "warn" : "ok");
    const canAttach = !!findResumeInput();
    $("#result").innerHTML = `
      <div class="stats"><span class="chip">${pct(sc.supported_coverage)} of what you can show</span><span class="chip">${sc.one_page ? "1 page" : "over 1 page"}</span>
        <span class="chip">${st.llm_requests ? `${st.llm_requests} Claude call${st.llm_requests > 1 ? "s" : ""}` : "no Claude usage"}</span><span class="chip">${st.seconds ?? "?"}s</span></div>
      <div class="items" id="items"><span class="spin"></span> Loading what's on the page…</div>
      <button class="primary" data-act="download">${sc.one_page ? "Download" : "Download anyway (over 1 page)"}</button>
      ${canAttach ? '<button class="secondary" data-act="attach">Attach to this application</button>' : ""}
      <button class="secondary" data-act="openPdf">Open PDF</button>
      <button class="secondary" data-act="overleaf">Edit in Overleaf</button>`;
    loadItems(res.run_id);
  }

  // ------------------------------------------------------------ preview + replace
  const SECTION = { experiences: "Experience", projects: "Projects" };
  /** @param {string} runId */
  async function loadItems(runId) {
    const r = await send({ type: "runItems", runId });
    const box = $("#items");
    if (!box) return;
    if (!r.ok) { box.textContent = `Couldn't list what's on the page (${r.error}).`; return; }
    lastItems = r.items;
    box.innerHTML = Object.entries(SECTION).map(([key, label]) => {
      const s = r.items[key];
      if (!s?.on_page?.length) return "";
      return `<h3>${label}</h3><ul>${s.on_page.map((/** @type {any} */ it) => `
        <li data-item="${esc(it.id)}"><div class="it"><span class="nm">${esc(it.title)}${it.org ? ` <small>${esc(it.org)}</small>` : ""}</span>
          <small>${it.bullets} bullet${it.bullets === 1 ? "" : "s"}</small>
          ${s.unused.length ? `<button class="link" data-act="replace" data-section="${key}" data-item="${esc(it.id)}">Replace</button>` : ""}</div>
          <div class="rep" hidden></div></li>`).join("")}</ul>`;
    }).join("") || "Nothing listed.";
  }

  /** @param {HTMLElement} btn */
  function openReplace(btn) {
    const li = /** @type {HTMLElement} */ (btn.closest("li"));
    const rep = /** @type {HTMLElement} */ (li.querySelector(".rep"));
    if (!rep.hidden) { rep.hidden = true; return; }
    const out = btn.dataset.item || "", section = btn.dataset.section || "";
    // an item describing the same work as a different one on the page can't come in here
    const rows = (lastItems?.[section]?.unused || []).filter((/** @type {any} */ u) => u.overlaps.every((/** @type {string} */ o) => o === out));
    rep.innerHTML = rows.length ? `
      <select aria-label="Use this instead">${rows.map((/** @type {any} */ u) =>
        `<option value="${esc(u.id)}" ${u.blocked ? "disabled" : ""}>${esc(u.title)}${u.org ? `, ${esc(u.org)}` : ""}${u.blocked ? " (short last lines: can't print)" : ""}</option>`).join("")}</select>
      <button class="secondary" data-act="doReplace" data-item="${esc(out)}">Use this instead (rebuilds the resume)</button>`
      : "<small>Nothing else in this section can replace it.</small>";
    const sel = rep.querySelector("select");
    const first = rows.find((/** @type {any} */ u) => !u.blocked);
    if (sel && first) sel.value = first.id;
    rep.hidden = false;
  }

  /** @param {HTMLElement} btn */
  async function doReplace(btn) {
    if (polling || !lastJob) return;
    const sel = /** @type {HTMLSelectElement | null} */ (btn.closest(".rep")?.querySelector("select") || null);
    if (!sel?.value || sel.selectedOptions[0]?.disabled) return;
    setMsg('<span class="spin"></span> Rebuilding with your change…');
    const r = await send({ type: "replaceItem", runId: lastJob.result.run_id, out: btn.dataset.item, in: sel.value, url: location.href });
    if (!r.ok) return setMsg(esc(r.error), "err");
    await follow(r.job.id);
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
    if (act === "force") make(true);
    if (act === "cancel" && polling) {
      const a = await send({ type: "activeJob" });
      if (a.active) await send({ type: "cancelJob", id: a.active.id });
      setMsg('<span class="spin"></span> Cancelling…');
    }
    if (act === "attach") attach();
    if (act === "download" && lastJob) {
      const r = await send({ type: "download", id: lastJob.id });
      if (r.ok) setMsg(`Downloaded <span class="file">${esc(lastJob.result.filename)}</span>.`, "ok");
      else setMsg(esc(r.error), "err");
    }
    if (act === "replace") openReplace(/** @type {HTMLElement} */ (/** @type {HTMLElement} */ (e.target).closest("[data-act]")));
    if (act === "doReplace") doReplace(/** @type {HTMLElement} */ (/** @type {HTMLElement} */ (e.target).closest("[data-act]")));
    if (act === "openPdf" && lastJob) send({ type: "openRun", runId: lastJob.result.run_id, kind: "pdf" });
    if (act === "overleaf" && lastJob) send({ type: "openRun", runId: lastJob.result.run_id, kind: "overleaf" });
    if (act === "openApp") send({ type: "openApp" });
    if (act === "editJd") { $("#jdWrap").hidden = !$("#jdWrap").hidden; renderSource(); }
    if (act === "dismiss") { dismissedUrl = location.href; hide(); }
    if (act === "hideSite") {
      const { hiddenSites = [] } = await chrome.storage.local.get("hiddenSites");
      await chrome.storage.local.set({ hiddenSites: [...new Set([...hiddenSites, location.hostname])] });
      hide();
    }
  }

  // ------------------------------------------------------------ when to show
  async function check() {
    const { showButton = true, hiddenSites = [] } = await chrome.storage.local.get(["showButton", "hiddenSites"]);
    if (!showButton || hiddenSites.includes(location.hostname) || dismissedUrl === location.href) return hide();
    const isJob = looksLikeJobPage();
    if (isJob && !visible) show();
    if (!isJob && visible && (!host || panel().hidden) && !polling) hide();
    if (isJob) {
      const j = extractJob();
      if (j.jd.length >= 200 && j.confidence >= 0.6) send({ type: "rememberJob", job: { ...j, url: location.href } });
    }
  }

  function onUrlChange() {
    if (location.href === lastUrl) return;
    lastUrl = location.href;
    checks = 0;
    if (host && !polling) {
      $("#company").value = ""; $("#role").value = ""; $("#jd").value = ""; $("#result").innerHTML = ""; $("#steps").hidden = true;
      setMsg(""); lastJob = null; job = null;
      const b = $('[data-act="make"]'); b.className = "primary"; b.textContent = "Make resume";
      closePanel();
    }
    check();
  }

  // Single-page sites (LinkedIn, Workday, Simplify) change the URL and render late: watch the DOM,
  // debounced, instead of polling on a timer.
  let timer = 0;
  const schedule = (ms = 400) => { clearTimeout(timer); timer = /** @type {any} */ (setTimeout(() => { onUrlChange(); if (!visible) check(); }, ms)); };
  let checks = 0;
  const observer = new MutationObserver(() => { if (location.href !== lastUrl || (!visible && checks++ < 40)) schedule(); });
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
