// @ts-check
// Reads a job posting out of a page. Shared by the floating button (content.js).
// Works on the live page or on a fetched + parsed document (apply pages that hide the posting).
//
// Order: your highlighted text -> schema.org JobPosting data -> known job-site adapters
//        -> largest block of text that reads like a posting. Navigation, forms, cookie banners,
//        buttons and similar page chrome are stripped before reading.

/**
 * @typedef {{ match: RegExp, jd: string[], role: string[], company: string[], slug?: number, sub?: boolean, name: string }} Adapter
 * @typedef {{ jd: string, company: string, role: string, source: string, confidence: number, issues: string[] }} ExtractedJob
 */

/** @type {Adapter[]} */
var RT_ATS = [
  { name: "linkedin", match: /(^|\.)linkedin\.com$/, jd: ["#job-details", ".jobs-description__content", ".jobs-box__html-content", ".description__text"],
    company: [".job-details-jobs-unified-top-card__company-name", ".topcard__org-name-link", ".jobs-unified-top-card__company-name"],
    role: [".job-details-jobs-unified-top-card__job-title", ".top-card-layout__title", ".jobs-unified-top-card__job-title", "h1"] },
  { name: "greenhouse", match: /greenhouse\.io$/, jd: [".job__description", "#content", ".job-post", "#app_body"], role: [".job__title h1", ".app-title", "h1"], company: [".company-name"], slug: 0 },
  { name: "lever", match: /lever\.co$/, jd: [".posting-page .section-wrapper.page-full-width", ".content .section-wrapper", ".posting-page"], role: [".posting-headline h2"], company: [], slug: 0 },
  { name: "workday", match: /myworkdayjobs\.com$|myworkdaysite\.com$/, jd: ['[data-automation-id="jobPostingDescription"]'], role: ['[data-automation-id="jobPostingHeader"]'], company: [], sub: true },
  { name: "ashby", match: /ashbyhq\.com$/, jd: ['[class*="descriptionText"]', '[class*="_description_"]'], role: ["h1"], company: [], slug: 0 },
  { name: "smartrecruiters", match: /smartrecruiters\.com$/, jd: ['[itemprop="description"]', ".job-sections"], role: ["h1"], company: [], slug: 0 },
  { name: "icims", match: /icims\.com$/, jd: [".iCIMS_JobContent", ".iCIMS_Expandable_Text"], role: ["h1"], company: [] },
  { name: "workable", match: /workable\.com$/, jd: ['[data-ui="job-description"]', "main"], role: ["h1"], company: [], slug: 0 },
  { name: "bamboohr", match: /bamboohr\.com$/, jd: [".BambooRich", "#js-careers-root"], role: ["h2", "h1"], company: [], sub: true },
  { name: "jobvite", match: /jobvite\.com$/, jd: [".jv-job-detail-description"], role: [".jv-header"], company: [], slug: 0 },
  { name: "indeed", match: /(^|\.)indeed\.[a-z.]+$/, jd: ["#jobDescriptionText"], role: ['[data-testid="jobsearch-JobInfoHeader-title"]', "h1"], company: ['[data-testid="inlineHeader-companyName"]', '[data-company-name="true"]'] },
  { name: "glassdoor", match: /(^|\.)glassdoor\.[a-z.]+$/, jd: ['[class*="JobDetails_jobDescription"]', "#JobDescriptionContainer"], role: ['[data-test="job-title"]', "h1"], company: ['[data-test="employer-name"]'] },
  { name: "simplify", match: /(^|\.)simplify\.jobs$/, jd: ['[data-testid="job-description"]', '[class*="job-description"]', '[class*="JobDescription"]', "#job-description", "article", "main"],
    role: ['[data-testid="job-title"]', "h1"], company: ['[data-testid="company-name"]', 'a[href^="/c/"]', 'a[href*="simplify.jobs/c/"]'] },
  { name: "waterlooworks", match: /waterlooworks|mosaic|mcmaster/, jd: ["#postingDiv", ".postings-table", "main"], role: ["h1"], company: [] },
];

// Page chrome that is never part of a posting.
var RT_NOISE = "script, style, noscript, template, svg, nav, header, footer, form, aside, button, select, input, textarea, iframe, " +
  "[role=navigation], [role=banner], [role=contentinfo], [role=dialog], [aria-hidden=true], [hidden], " +
  "[class*=cookie], [id*=cookie], [class*=consent], [class*=share], [class*=breadcrumb], [class*=similar-jobs], [class*=recommend]";
var RT_BLOCK = "p, li, br, h1, h2, h3, h4, h5, h6, div, section, article, tr, dt, dd, ul, ol";
var RT_JUNK_LINE = /^(apply( now| for this job)?|save( job)?|share( this job)?|report( this)? job|easy apply|sign in|log in|back to (jobs|search)|show (more|less)|see more|copy link|follow|…more|\d+ (applicants|views))$/i;

/** @param {string} s */
function rtClean(s) {
  return (s || "").replace(/ /g, " ").replace(/[ \t]+/g, " ").replace(/\n[ \t]+/g, "\n").replace(/\n{3,}/g, "\n\n").trim();
}

/** @param {string} slug */
function rtTitleCase(slug) {
  return decodeURIComponent(slug || "").replace(/[-_]+/g, " ").replace(/\b\w/g, (c) => c.toUpperCase()).trim();
}

/** Readable text of an element without page chrome. @param {Element | null} el */
function rtText(el) {
  if (!el) return "";
  const copy = /** @type {Element} */ (el.cloneNode(true));
  copy.querySelectorAll(RT_NOISE).forEach((n) => n.remove());
  copy.querySelectorAll(RT_BLOCK).forEach((n) => n.append("\n"));
  const lines = rtClean(copy.textContent || "").split("\n").filter((ln) => !RT_JUNK_LINE.test(ln.trim()));
  return rtClean(lines.join("\n"));
}

/** @param {string} html */
function rtHtmlToText(html) {
  const d = new DOMParser().parseFromString(`<div>${html}</div>`, "text/html");
  return rtText(d.body);
}

/** How much this text looks like a complete posting (0..1), with reasons when it doesn't. @param {string} jd @param {string} source */
function rtConfidence(jd, source) {
  const issues = [];
  const words = jd ? jd.split(/\s+/).length : 0;
  let c = { selection: 0.95, "page data": 0.9, adapter: 0.85, "the posting page": 0.8, earlier: 0.75, heuristic: 0.6 }[source] ?? 0.5;
  if (words < 40) { c -= 0.45; issues.push("very short"); }
  else if (words < 80) { c -= 0.2; issues.push("short"); }
  else if (words < 150) { c -= 0.08; }
  if (words > 3500) { c -= 0.2; issues.push("very long (may include other page text)"); }
  const cues = [/responsibilit|what you('ll| will) do|the role|day[- ]to[- ]day/i, /qualifications?|requirements?|what you('ll)? (need|bring)|must have|you have/i,
    /experience|skills?|degree|knowledge of/i];
  const hits = cues.filter((r) => r.test(jd)).length;
  if (hits < 2) { c -= 0.25; issues.push("no requirements or responsibilities section found"); }
  if (/cookie|privacy policy|sign in to|create (an )?account|captcha/i.test(jd.slice(0, 400))) { c -= 0.2; issues.push("starts with site text, not the posting"); }
  return { confidence: Math.max(0, Math.min(1, Math.round(c * 100) / 100)), issues };
}

/**
 * @param {Document} [doc]
 * @param {Location | URL} [loc]
 * @returns {ExtractedJob}
 */
function extractJob(doc = document, loc = location) {
  const live = doc === document;
  /** @param {string} sel */
  const text = (sel) => { try { return rtText(doc.querySelector(sel)); } catch { return ""; } };
  /** @param {string[] | undefined} sels */
  const first = (sels) => { for (const s of sels || []) { const t = text(s); if (t) return t; } return ""; };
  /** @param {string} s */
  const oneLine = (s) => rtClean(s).split("\n")[0].trim();
  const out = { jd: "", company: "", role: "", source: "", confidence: 0, issues: /** @type {string[]} */ ([]) };
  let kind = "";
  const host = loc.hostname;

  // 1) Text you highlighted always wins.
  if (live) {
    const picked = rtClean(String(window.getSelection() || ""));
    if (picked.length > 200) { out.jd = picked; out.source = "your selection"; kind = "selection"; }
  }

  // 2) schema.org JobPosting data, which many job boards include.
  for (const s of doc.querySelectorAll('script[type="application/ld+json"]')) {
    try {
      const data = JSON.parse(s.textContent || "");
      const list = Array.isArray(data) ? data : data["@graph"] || [data];
      const job = list.find((d) => d && [].concat(d["@type"]).includes("JobPosting"));
      if (!job) continue;
      if (!out.jd && typeof job.description === "string") { out.jd = rtHtmlToText(job.description); out.source = "page data"; kind = "page data"; }
      out.role = out.role || oneLine(String(job.title || ""));
      const org = job.hiringOrganization;
      out.company = out.company || oneLine(typeof org === "string" ? org : String(org?.name || ""));
      break;
    } catch { /* malformed JSON-LD: ignore */ }
  }

  // 3) Known job sites.
  const site = RT_ATS.find((s) => s.match.test(host));
  if (site) {
    if (!out.jd) { const t = first(site.jd); if (t.length > 200) { out.jd = t; out.source = site.name; kind = "adapter"; } }
    out.role = out.role || oneLine(first(site.role));
    out.company = out.company || oneLine(first(site.company));
    const parts = loc.pathname.split("/").filter(Boolean);
    if (!out.company && site.slug !== undefined && parts[site.slug]) out.company = rtTitleCase(parts[site.slug]);
    if (!out.company && site.sub) out.company = rtTitleCase(host.split(".")[0]);
  }

  // 4) Fallback: the smallest block that still holds the posting (avoids grabbing the whole page).
  if (!out.jd) {
    let best = "", bestScore = 0;
    for (const el of doc.querySelectorAll("main, article, [role=main], section, div")) {
      const raw = (el.textContent || "").length;
      if (raw < 300 || raw > 40000) continue;
      const rawText = el.textContent || "";
      if (!/responsib|what you('ll| will) do|qualif|requirement/i.test(rawText)) continue;  // cheap check before cleaning
      const t = rtText(el);
      const cues = [/responsib|what you('ll| will) do/i, /qualif|requirement|what you (need|bring)/i, /experience|skills/i].filter((r) => r.test(t)).length;
      if (cues < 2) continue;
      const score = cues * 1000 - Math.abs(t.length - 3500) / 10;  // postings are usually 1.5k-6k chars
      if (score > bestScore) { best = t; bestScore = score; }
    }
    if (best.length > 200) { out.jd = best; out.source = "page text"; kind = "heuristic"; }
  }

  // Company from page metadata.
  if (!out.company) {
    const og = doc.querySelector('meta[property="og:site_name"]')?.getAttribute("content") || "";
    const title = doc.title || "";
    const m = title.match(/ at ([^|\-–]+)/i) || title.match(/^([^|\-–]+?)\s[-–|]/);
    out.company = rtClean(og && !/linkedin|indeed|glassdoor|greenhouse|lever|workday|ashby|simplify/i.test(og) ? og : m ? m[1] : "");
  }
  out.company = out.company.replace(/\s*(careers|jobs|job board)$/i, "").slice(0, 80);
  out.role = (out.role || oneLine(text("h1"))).slice(0, 120);
  out.jd = out.jd.slice(0, 20000);
  const q = rtConfidence(out.jd, kind);
  out.confidence = out.jd ? q.confidence : 0;
  out.issues = out.jd ? q.issues : ["no posting text found"];
  return out;
}

/** Is this page a job posting or an application form? @param {Document} [doc] @param {Location | URL} [loc] */
function looksLikeJobPage(doc = document, loc = location) {
  const host = loc.hostname, path = loc.pathname + loc.search;
  if (/(^|\.)linkedin\.com$/.test(host)) return /\/jobs\//.test(path) && /currentJobId=|\/jobs\/view\//.test(path);
  if (/(^|\.)indeed\./.test(host)) return /viewjob|vjk=|jk=/.test(path);
  if (/(^|\.)glassdoor\./.test(host)) return /job-listing|jobListingId|\/Job\//i.test(path);
  if (/(^|\.)simplify\.jobs$/.test(host)) return /^\/p\/|\/jobs?\/[\w-]+/.test(loc.pathname) || /jobId=/.test(loc.search);
  if (RT_ATS.some((s) => s.match.test(host))) return true;
  for (const s of doc.querySelectorAll('script[type="application/ld+json"]')) if (/JobPosting/.test(s.textContent || "")) return true;
  if (!/job|career|position|opening|posting|apply|vacanc|intern/i.test(host + path)) return false;
  const body = (doc.body?.textContent || "").slice(0, 40000);
  const hits = [/responsibilit/i, /qualification|requirement/i, /apply|application/i, /experience/i].filter((r) => r.test(body)).length;
  return hits >= 3;
}

/** The file input on an application form where a resume goes. @param {Document} [doc] */
function findResumeInput(doc = document) {
  const inputs = /** @type {HTMLInputElement[]} */ ([...doc.querySelectorAll('input[type="file"]')]);
  if (!inputs.length) return null;
  const RESUME = /resume|résumé|\bcv\b|curriculum/i, OTHER = /cover|transcript|portfolio|photo|image|additional/i;
  /** @param {string} text @param {number} weight */
  const score = (text, weight) => (RESUME.test(text) ? weight : 0) - (OTHER.test(text) ? weight : 0);
  const scored = inputs.map((el) => {
    const own = [el.id, el.name, el.getAttribute("aria-label"), el.getAttribute("data-automation-id"),
      el.id ? doc.querySelector(`label[for="${CSS.escape(el.id)}"]`)?.textContent : ""].join(" ");
    let s = score(own, 4);
    let p = el.parentElement;
    for (let i = 0; i < 5 && p && p.querySelectorAll('input[type="file"]').length === 1; i++, p = p.parentElement) {
      s += score((p.textContent || "").slice(0, 300), 2);
    }
    return { el, s };
  }).sort((a, b) => b.s - a.s);
  if (scored[0].s > 0) return scored[0].el;
  return inputs.length === 1 ? inputs[0] : null;
}
