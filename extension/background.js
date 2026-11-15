// @ts-check
// Service worker. Talks to the local Resume Tailor server, downloads finished PDFs, and remembers
// the posting each tab last showed (so apply pages that hide the posting still work).
//
// MV3-safe: no long-running work lives here. The server runs each job; the page's content script
// polls through short messages, and the active job id per tab is kept in chrome.storage.session,
// so a sleeping/restarted service worker loses nothing.

const DEFAULT_SERVER = "http://127.0.0.1:8765";

async function getServer() {
  const { server } = await chrome.storage.local.get("server");
  const s = String(server || DEFAULT_SERVER).replace(/\/$/, "");
  // only ever talk to this computer
  return /^http:\/\/(127\.0\.0\.1|localhost)(:\d+)?$/.test(s) ? s : DEFAULT_SERVER;
}

class ServerError extends Error {
  /** @param {string} message @param {number} status @param {any} [body] */
  constructor(message, status, body) { super(message); this.status = status; this.body = body; }
}

/** Gets (and caches) the token from the server. Only this extension's origin can pair. */
async function token(refresh = false) {
  if (!refresh) {
    const { rtToken } = await chrome.storage.local.get("rtToken");
    if (rtToken) return rtToken;
  }
  const server = await getServer();
  const r = await fetch(server + "/api/pair", { method: "POST" });
  const body = await r.json().catch(() => ({}));
  if (!r.ok) throw new ServerError(body.detail || "Couldn't pair with the server.", r.status, body);
  await chrome.storage.local.set({ rtToken: body.token });
  return body.token;
}

/** @param {string} path @param {RequestInit} [opts] @param {boolean} [retry] */
async function api(path, opts = {}, retry = true) {
  const server = await getServer();
  let r;
  try {
    r = await fetch(server + path, { ...opts, headers: { "Content-Type": "application/json", "X-RT-Token": await token(), ...(opts.headers || {}) } });
  } catch (e) {
    if (e instanceof ServerError) throw e;
    throw new ServerError(`Can't reach Resume Tailor at ${server}. It starts when you log in; or double-click start.bat.`, 0);
  }
  if (r.status === 401 && retry) { await token(true); return api(path, opts, false); }
  const body = await r.json().catch(() => ({}));
  if (!r.ok) {
    const d = body.detail;
    throw new ServerError(typeof d === "string" ? d : d?.message || `Server error ${r.status}`, r.status, body);
  }
  return body;
}

/** @param {number | undefined} tabId @param {any} job */
async function rememberJob(tabId, job) {
  if (tabId == null || !job?.jd || job.jd.length < 200) return;
  await chrome.storage.session.set({ ["job:" + tabId]: { jd: job.jd, company: job.company, role: job.role, url: job.url, savedAt: Date.now() } });
}

/** @param {number | undefined} tabId */
async function recallJob(tabId) {
  const key = "job:" + tabId;
  return (await chrome.storage.session.get(key))[key] || null;
}

/** @param {ArrayBuffer} buf */
function toBase64(buf) {
  const bytes = new Uint8Array(buf);
  let s = "";
  for (let i = 0; i < bytes.length; i += 0x8000) s += String.fromCharCode(...bytes.subarray(i, i + 0x8000));
  return btoa(s);
}

/** Download a finished job's PDF once (checked: completed, has a file, fits one page unless forced). */
async function downloadJob(job, force = false) {
  const key = "dl:" + job.id;
  if ((await chrome.storage.session.get(key))[key]) return { downloaded: true, already: true };
  const res = job.result;
  if (job.state !== "completed" || !res?.filename || !res.run_id) return { downloaded: false, reason: "not ready" };
  if (!force && res.scorecard && res.scorecard.one_page === false) return { downloaded: false, reason: "not one page" };
  if (!/^[A-Za-z0-9_-]+\.pdf$/.test(res.filename)) return { downloaded: false, reason: "bad file name" };
  const server = await getServer();
  await chrome.downloads.download({
    url: `${server}/api/pdf/${encodeURIComponent(res.run_id)}?download=true`,
    filename: res.filename, conflictAction: "uniquify", saveAs: false,
    headers: [{ name: "X-RT-Token", value: await token() }],
  });
  await chrome.storage.session.set({ [key]: Date.now() });
  return { downloaded: true };
}

/** @param {any} msg @param {chrome.runtime.MessageSender} sender */
async function handle(msg, sender) {
  const tabId = msg.tabId ?? sender.tab?.id;
  const server = await getServer();
  switch (msg.type) {
    case "health": {
      try {
        const r = await fetch(server + "/api/health");
        const h = await r.json();
        let paired = true;
        try { await token(); } catch { paired = false; }
        return { ok: true, ...h, server, paired };
      } catch { return { ok: false, server }; }
    }
    case "analyze":
      return { ok: true, data: await api("/api/analyze", { method: "POST", body: JSON.stringify(msg.payload) }) };
    case "startJob": {
      const job = await api("/api/jobs", { method: "POST", body: JSON.stringify(msg.payload) });
      await chrome.storage.session.set({ ["active:" + tabId]: { id: job.id, url: msg.payload.url, at: Date.now() } });
      await rememberJob(tabId, msg.payload);
      return { ok: true, job };
    }
    case "jobStatus": {
      const job = await api(`/api/jobs/${encodeURIComponent(msg.id)}`);
      let dl = null;
      if (job.state === "completed" && msg.autoDownload !== false) dl = await downloadJob(job);
      if (["completed", "failed", "cancelled"].includes(job.state)) await chrome.storage.session.remove("active:" + tabId);
      return { ok: true, job, download: dl };
    }
    case "download": {
      const job = await api(`/api/jobs/${encodeURIComponent(msg.id)}`);
      return { ok: true, download: await downloadJob(job, true) };
    }
    case "editOptions":
      return { ok: true, items: await api(`/api/run/${encodeURIComponent(msg.runId)}/edit-options`) };
    case "applyEdits": {
      const job = await api(`/api/run/${encodeURIComponent(msg.runId)}/edit`,
        { method: "POST", body: JSON.stringify({ items: msg.items || [], bullets: msg.bullets || [] }) });
      await chrome.storage.session.set({ ["active:" + tabId]: { id: job.id, url: msg.url || "", at: Date.now() } });
      return { ok: true, job };
    }
    case "activeJob": {
      const key = "active:" + tabId;
      return { ok: true, active: (await chrome.storage.session.get(key))[key] || null };
    }
    case "cancelJob":
      return { ok: true, job: await api(`/api/jobs/${encodeURIComponent(msg.id)}/cancel`, { method: "POST" }) };
    case "rememberJob":
      await rememberJob(tabId, msg.job);
      return { ok: true };
    case "recallJob":
      return { ok: true, job: await recallJob(tabId) };
    case "pdfBytes": {
      const r = await fetch(`${server}/api/pdf/${encodeURIComponent(msg.runId)}`, { headers: { "X-RT-Token": await token() } });
      if (!r.ok) return { ok: false, error: "Couldn't load the PDF." };
      return { ok: true, base64: toBase64(await r.arrayBuffer()) };
    }
    case "openRun": {
      const kind = msg.kind === "overleaf" ? "overleaf" : "pdf";
      await chrome.tabs.create({ url: `${server}/api/${kind}/${encodeURIComponent(msg.runId)}` });
      return { ok: true };
    }
    case "openApp":
      await chrome.tabs.create({ url: server + "/" });
      return { ok: true };
  }
  return { ok: false, error: "Unknown message" };
}

chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
  // only our own pages and content scripts
  if (sender.id !== chrome.runtime.id) return false;
  handle(msg, sender).then(sendResponse, (e) => sendResponse({ ok: false, error: String(e?.message || e), status: e?.status, body: e?.body }));
  return true; // async reply
});

chrome.tabs.onRemoved.addListener((tabId) => chrome.storage.session.remove(["job:" + tabId, "active:" + tabId]));
chrome.runtime.onInstalled.addListener(() => { token().catch(() => {}); });
