// @ts-check
/** @param {string} id @returns {any} */
const $ = (id) => document.getElementById(id);
/** @param {any} msg @returns {Promise<any>} */
const send = (msg) => new Promise((res) => chrome.runtime.sendMessage(msg, (r) => res(r || { ok: false, error: chrome.runtime.lastError?.message })));
/** @type {chrome.tabs.Tab | undefined} */ let tab;
let siteHost = "";

/** @param {string} text @param {boolean} [isErr] */
function setMsg(text, isErr = false) { $("msg").className = isErr ? "err" : ""; $("msg").textContent = text; }

async function init() {
  [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  try { siteHost = new URL(tab?.url || "").hostname; } catch { /* chrome:// pages */ }
  const { showButton = true, hiddenSites = [], server = "" } = await chrome.storage.local.get(["showButton", "hiddenSites", "server"]);
  $("showButton").checked = showButton;
  $("serverUrl").value = server;
  if (hiddenSites.includes(siteHost)) { $("siteLine").hidden = false; $("siteText").textContent = `Hidden on ${siteHost}`; }
  const h = await send({ type: "health" });
  $("dot").className = "dot " + (h.ok && h.paired ? "ok" : "bad");
  if (!h.ok) setMsg("The server isn't running. It starts when you log in, or double-click start.bat.", true);
  else if (!h.paired) setMsg("Not paired with the server. In Resume Tailor → Settings, press Reset pairing, then reopen this.", true);
  else setMsg(h.mode === "offline" ? (h.problem || "Using the built-in matcher.") : `Using Claude (${h.model}).`);
}

$("open").addEventListener("click", async () => {
  if (!tab?.id) return;
  try {
    const r = await chrome.tabs.sendMessage(tab.id, { type: "openPanel" });
    if (r?.ok) window.close();
  } catch {
    setMsg("This page can't be read (or was open before the extension loaded: reload it). You can paste the posting in Resume Tailor instead.", true);
  }
});
$("openApp").addEventListener("click", () => { send({ type: "openApp" }); window.close(); });
$("showButton").addEventListener("change", () => chrome.storage.local.set({ showButton: $("showButton").checked }));
$("unhide").addEventListener("click", async () => {
  const { hiddenSites = [] } = await chrome.storage.local.get("hiddenSites");
  await chrome.storage.local.set({ hiddenSites: hiddenSites.filter((/** @type {string} */ h) => h !== siteHost) });
  $("siteLine").hidden = true;
});
$("serverUrl").addEventListener("change", () => {
  const v = $("serverUrl").value.trim();
  if (v && !/^http:\/\/(127\.0\.0\.1|localhost)(:\d+)?\/?$/.test(v)) return setMsg("Only http://127.0.0.1:PORT or http://localhost:PORT is allowed.", true);
  chrome.storage.local.set({ server: v, rtToken: "" });
});
init();
