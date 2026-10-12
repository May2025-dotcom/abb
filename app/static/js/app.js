// --- Toast ---------------------------------------------------------------

let toastTimer;
function toast(message, isError = false) {
  const el = document.getElementById("toast");
  el.textContent = message;
  el.classList.toggle("error", isError);
  el.classList.add("show");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => el.classList.remove("show"), 3500);
}

// --- Search form ---------------------------------------------------------

const form = document.getElementById("search-form");
if (form) {
  form.addEventListener("submit", () => {
    document.getElementById("search-button").setAttribute("aria-busy", "true");
  });
}

// --- Magnet links ----------------------------------------------------------

const magnetCache = new Map();

async function postJSON(url, body) {
  const resp = await fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  const data = await resp.json().catch(() => ({}));
  if (!resp.ok) throw new Error(data.message || `Request failed (${resp.status})`);
  return data;
}

async function getMagnet(link) {
  if (!magnetCache.has(link)) {
    const { magnet } = await postJSON("/magnet", { link });
    magnetCache.set(link, magnet);
  }
  return magnetCache.get(link);
}

async function copyText(text) {
  // navigator.clipboard only works on HTTPS or localhost.
  if (navigator.clipboard && window.isSecureContext) {
    try {
      await navigator.clipboard.writeText(text);
      return true;
    } catch (_) { /* permission denied, try the old way */ }
  }
  const ta = document.createElement("textarea");
  ta.value = text;
  ta.style.position = "fixed";
  ta.style.opacity = "0";
  document.body.appendChild(ta);
  ta.select();
  let ok = false;
  try { ok = document.execCommand("copy"); } catch (_) {}
  ta.remove();
  return ok;
}

function showMagnetDialog(magnet) {
  const dialog = document.getElementById("magnet-dialog");
  const text = document.getElementById("magnet-text");
  text.value = magnet;
  dialog.showModal();
  text.select();
}

async function handleAction(button) {
  const row = button.closest(".result");
  const { link, title } = row.dataset;
  const action = button.dataset.action;

  button.setAttribute("aria-busy", "true");
  try {
    if (action === "send") {
      const { message } = await postJSON("/send", { link, title });
      toast(message);
    } else if (action === "copy") {
      const magnet = await getMagnet(link);
      if (await copyText(magnet)) toast("Magnet link copied");
      else showMagnetDialog(magnet);
    } else if (action === "open") {
      const magnet = await getMagnet(link);
      button.setAttribute("href", magnet);
      window.location.href = magnet;
      toast("Opening in your torrent app…");
    }
  } catch (err) {
    toast(err.message, true);
  } finally {
    button.removeAttribute("aria-busy");
  }
}

const results = document.getElementById("results");
if (results) {
  results.addEventListener("click", (e) => {
    const button = e.target.closest("[data-action]");
    if (!button || button.dataset.action.startsWith("tb-")) return; // TorBox has its own handler
    // Once fetched, "Open magnet" is a real magnet: link and needs no JS.
    if (button.dataset.action === "open" && button.getAttribute("href").startsWith("magnet:")) return;
    e.preventDefault();
    handleAction(button);
  });
}

// --- Filters ---------------------------------------------------------------

function sizeInMB(text) {
  const m = /([\d.]+)\s*([KMGT])B/i.exec(text || "");
  if (!m) return null;
  const n = parseFloat(m[1]);
  return { K: n / 1024, M: n, G: n * 1024, T: n * 1024 * 1024 }[m[2].toUpperCase()];
}

function parseDate(text) {
  if (!text || text === "N/A") return null;
  // AudioBook Bay uses "12 Mar 2021"; reorder so every browser parses it.
  const d = new Date(text.replace(/(\d{1,2})\s(\w{3})\s(\d{4})/, "$2 $1, $3"));
  return isNaN(d) ? null : d;
}

const filters = document.getElementById("filters");
if (filters && results) {
  const rows = [...results.querySelectorAll(".result")];
  const selects = [...filters.querySelectorAll("select[data-filter]")];
  const sizeSel = document.getElementById("filter-size");
  const ageSel = document.getElementById("filter-age");
  const clearBtn = document.getElementById("clear-filters");

  // Fill dropdowns from the results, hiding any with fewer than two options.
  for (const sel of selects) {
    const values = [...new Set(rows.map((r) => r.dataset[sel.dataset.filter]))]
      .filter((v) => v && v !== "N/A")
      .sort((a, b) => a.localeCompare(b, undefined, { numeric: true }));
    for (const v of values) sel.add(new Option(v, v));
    if (values.length < 2) sel.hidden = true;
  }

  function apply() {
    const active = selects.filter((s) => s.value);
    const [minMB, maxMB] = (sizeSel.value || "-").split("-").map((v) => (v ? +v : null));
    const maxAgeDays = ageSel.value ? +ageSel.value : null;
    const now = Date.now();
    let shown = 0;

    for (const row of rows) {
      let ok = active.every((s) => row.dataset[s.dataset.filter] === s.value);

      if (ok && sizeSel.value) {
        const mb = sizeInMB(row.dataset.size);
        ok = mb !== null && (minMB === null || mb >= minMB) && (maxMB === null || mb < maxMB);
      }
      if (ok && maxAgeDays) {
        const d = parseDate(row.dataset.date);
        ok = d !== null && now - d.getTime() <= maxAgeDays * 86400000;
      }
      row.hidden = !ok;
      if (ok) shown++;
    }

    document.getElementById("result-count").textContent = shown;
    document.getElementById("no-matches").hidden = shown > 0;
    clearBtn.hidden = !(active.length || sizeSel.value || ageSel.value);
  }

  filters.addEventListener("change", apply);
  clearBtn.addEventListener("click", () => {
    filters.querySelectorAll("select").forEach((s) => (s.value = ""));
    apply();
  });
}

// --- TorBox ----------------------------------------------------------------
//
// States from the server: not_cached, cached, downloading (progress), ready
// (files), error. Download links go to /torbox/download, which redirects to a
// fresh TorBox link, so the API key never reaches the browser.

const TB_POLL_MS = 15000;
const tbTimers = new Map();

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
}

function humanSize(bytes) {
  if (!bytes) return "";
  const units = ["B", "KB", "MB", "GB", "TB"];
  let i = 0, n = bytes;
  while (n >= 1024 && i < units.length - 1) { n /= 1024; i++; }
  return `${n.toFixed(n >= 10 || i === 0 ? 0 : 1)} ${units[i]}`;
}

function tbDownloadUrl(row, fileId) {
  const q = new URLSearchParams({ link: row.dataset.link });
  if (fileId != null) q.set("file", fileId);
  return `/torbox/download?${q}`;
}

function duration(sec) {
  sec = Math.max(0, sec || 0);
  if (sec < 3600) return `${Math.max(1, Math.round(sec / 60))} min`;
  if (sec < 86400) return `${Math.floor(sec / 3600)} h`;
  return `${Math.floor(sec / 86400)} d ${Math.floor((sec % 86400) / 3600)} h`;
}

function renderTorBox(row, st) {
  const box = row.querySelector("[data-tb]");
  if (!box) return;
  clearTimeout(tbTimers.get(row));
  const zip = `<a class="btn btn-primary" href="${tbDownloadUrl(row)}">Download zip</a>`;
  const label = (text) => `<span class="tb-label"><strong>TorBox</strong> ${text}</span>`;
  const retry = `<button type="button" class="btn" data-action="tb-retry">Retry</button>`;
  const poll = () => tbTimers.set(row, setTimeout(() => tbRequest(row, "/torbox/status"), TB_POLL_MS));

  if (st.state === "ready") {
    const files = st.files || [];
    const list = files.length
      ? `<button type="button" class="btn btn-quiet" data-action="tb-files">Files (${files.length}) ▾</button>
         <ul class="tb-files" hidden>${files.map((f) =>
           `<li><a href="${tbDownloadUrl(row, f.id)}">${escapeHtml(f.name)}</a><span>${humanSize(f.size)}</span></li>`
         ).join("")}</ul>`
      : "";
    box.innerHTML = label(st.downloaded_at ? "downloaded" : "ready") + zip + list;
  } else if (st.state === "cached") {
    box.innerHTML = label("cached") + zip;
  } else if (st.state === "downloading") {
    const pct = Math.max(0, Math.min(100, st.progress || 0));
    const seeds = `${st.seeds || 0} seed${st.seeds === 1 ? "" : "s"}`;
    const text = st.stalled
      ? `waiting for seeds · ${pct}% · ${seeds} · ${duration(st.waiting_seconds)}`
      : `downloading ${pct}% · ${seeds}${st.speed ? ` · ${humanSize(st.speed)}/s` : ""}`;
    box.innerHTML = label(escapeHtml(text)) +
      `<span class="tb-bar"><span style="width:${pct}%"></span></span>` +
      `<a class="btn btn-quiet" href="/torbox">Details</a>`;
    poll();
  } else if (st.state === "queued") {
    box.innerHTML = label("queued - waiting for a free TorBox slot") + `<a class="btn btn-quiet" href="/torbox">Details</a>`;
    poll();
  } else if (st.state === "not_cached") {
    box.innerHTML = label("not cached") + `<button type="button" class="btn" data-action="tb-add">Add to TorBox</button>`;
  } else if (st.state === "gave_up" || st.state === "removed") {
    box.innerHTML = label(escapeHtml(st.message || st.state)) + retry;
  } else {
    box.innerHTML = `<span class="tb-label">${escapeHtml(st.message || "TorBox error")}</span>` +
      `<button type="button" class="btn" data-action="tb-check">Check again</button>`;
  }
}

async function tbRequest(row, url) {
  const busy = row.querySelector("[data-tb] .btn");
  if (busy) busy.setAttribute("aria-busy", "true");
  try {
    renderTorBox(row, await postJSON(url, { link: row.dataset.link, title: row.dataset.title }));
  } catch (err) {
    toast(err.message, true);
    renderTorBox(row, { state: "error", message: err.message });
  }
}

if (results && results.hasAttribute("data-torbox")) {
  results.addEventListener("click", (e) => {
    const button = e.target.closest('[data-action^="tb-"]');
    if (!button) return;
    const row = button.closest(".result");
    const action = button.dataset.action;
    if (action === "tb-files") {
      const list = row.querySelector(".tb-files");
      list.hidden = !list.hidden;
      button.textContent = button.textContent.replace(/[▾▴]/, list.hidden ? "▾" : "▴");
    } else if (action === "tb-add") {
      tbRequest(row, "/torbox/add");
    } else if (action === "tb-retry") {
      tbRequest(row, "/torbox/retry");
    } else if (action === "tb-check") {
      tbRequest(row, "/torbox/status");
    }
  });

  // Show the status of books whose magnet link is already known (no AudioBook
  // Bay traffic); the rest keep their "TorBox" button until clicked.
  const rows = [...results.querySelectorAll(".result")];
  postJSON("/torbox/peek", { links: rows.map((r) => r.dataset.link) })
    .then((states) => rows.forEach((r) => states[r.dataset.link] && renderTorBox(r, states[r.dataset.link])))
    .catch(() => {});
}

// --- TorBox page ----------------------------------------------------------------

const tbList = document.querySelector(".tb-list");
if (tbList) {
  tbList.addEventListener("click", async (e) => {
    const button = e.target.closest("[data-tb-action]");
    if (!button) return;
    const item = button.closest("[data-hash]");
    const action = button.dataset.tbAction;
    if (action === "remove" && !confirm("Remove this book from TorBox and from this list?")) return;
    button.setAttribute("aria-busy", "true");
    try {
      await postJSON(`/torbox/${action}`, { hash: item.dataset.hash });
      location.reload();
    } catch (err) {
      toast(err.message, true);
      button.removeAttribute("aria-busy");
    }
  });
}

const checkNow = document.getElementById("tb-check-now");
if (checkNow) {
  checkNow.addEventListener("click", async () => {
    checkNow.setAttribute("aria-busy", "true");
    try {
      await postJSON("/torbox/check-now", {});
      location.reload();
    } catch (err) {
      toast(err.message, true);
      checkNow.removeAttribute("aria-busy");
    }
  });
  // The server checks in the background; refresh what's shown every minute.
  setInterval(() => { if (!document.hidden) location.reload(); }, 60000);
}
