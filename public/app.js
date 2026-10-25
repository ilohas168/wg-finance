// WG Finance — phone app. Plain ES module, no build step.
// Talks to the Python API in /api (see api/index.py). All money maths is done
// on the server; this file only previews shares while you edit a receipt.

const ROOMMATES = ["Shin", "Fabian", "Pierre"];
const CODE = { Shin: "A", Fabian: "B", Pierre: "C" };
const COLOR = { Shin: "var(--shin)", Fabian: "var(--fabian)", Pierre: "var(--pierre)" };
const FOR_OPTIONS = [
  ["ALL", "Everyone"], ["A", "Shin"], ["B", "Fabian"], ["C", "Pierre"],
  ["AB", "Shin+Fabian"], ["BC", "Fabian+Pierre"], ["AC", "Shin+Pierre"],
];
const CATEGORIES = ["Food", "Drink", "Toiletries", "Household", "General"];
const METHODS = ["Twint", "Bank Transfer", "Cash"];
const REFRESH_AFTER_MS = 60_000;

const S = {
  token: localStorage.getItem("wg.token"),
  name: localStorage.getItem("wg.name"),
  data: readCachedState(),
  loadedAt: 0,
  loading: false,
  offline: false,
  receiptsFilter: { month: thisMonth(), search: "", payer: "Everyone" },
  statsMonth: thisMonth(),
  draft: null,
  scan: null,
};

// ------------------------------------------------------------------------- //
// Helpers                                                                    //
// ------------------------------------------------------------------------- //

const app = document.getElementById("app");
const nf = new Intl.NumberFormat("de-CH", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
const chf = (x) => `CHF ${nf.format(Math.abs(x) < 0.005 ? 0 : x)}`;
const signed = (x) => (x > 0.004 ? "+" : x < -0.004 ? "−" : "") + chf(Math.abs(x));
const esc = (v) => String(v ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
const cents = (x) => Math.round(parseMoney(x) * 100);
const money2 = (x) => (typeof x === "number" ? x.toFixed(2) : x ?? "");

function thisMonth() {
  const d = new Date();
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}`;
}
function shiftMonth(month, step) {
  const [y, m] = month.split("-").map(Number);
  const d = new Date(y, m - 1 + step, 1);
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}`;
}
function monthLabel(month) {
  if (month === "all") return "All time";
  const [y, m] = month.split("-").map(Number);
  return new Date(y, m - 1, 1).toLocaleDateString("en-GB", { month: "long", year: "numeric" });
}
function todayISO() {
  const d = new Date();
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
}
function dayLabel(iso) {
  if (!iso) return "Date unknown";
  const today = todayISO();
  const y = new Date(Date.now() - 86400000);
  const yesterday = `${y.getFullYear()}-${String(y.getMonth() + 1).padStart(2, "0")}-${String(y.getDate()).padStart(2, "0")}`;
  if (iso === today) return "Today";
  if (iso === yesterday) return "Yesterday";
  const [yy, mm, dd] = iso.split("-").map(Number);
  return new Date(yy, mm - 1, dd).toLocaleDateString("en-GB", { weekday: "short", day: "numeric", month: "short", year: yy === new Date().getFullYear() ? undefined : "numeric" });
}
function parseMoney(text) {
  const cleaned = String(text ?? "").replace(/[’'\s]/g, "").replace(/CHF/i, "").replace(",", ".");
  const n = parseFloat(cleaned);
  return Number.isFinite(n) ? n : 0;
}
function forLabel(code) {
  return (FOR_OPTIONS.find(([c]) => c === code) || FOR_OPTIONS[0])[1];
}
function avatar(name, cls = "") {
  return `<span class="avatar ${cls}" style="--c:${COLOR[name] || "var(--ink-3)"}">${esc((name || "?")[0])}</span>`;
}
function readCachedState() {
  try { return JSON.parse(localStorage.getItem("wg.state") || "null"); } catch { return null; }
}

// ------------------------------------------------------------------------- //
// API                                                                        //
// ------------------------------------------------------------------------- //

async function api(path, { method = "GET", body, form } = {}) {
  const headers = {};
  if (S.token) headers.Authorization = `Bearer ${S.token}`;
  if (body) headers["Content-Type"] = "application/json";
  let res;
  try {
    res = await fetch(path, { method, headers, body: form || (body ? JSON.stringify(body) : undefined) });
  } catch {
    throw new Error("No connection. Check your internet and try again.");
  }
  let payload = null;
  try { payload = await res.json(); } catch { /* empty body */ }
  if (res.status === 401 && S.token && path !== "/api/login") {
    logout("Your login expired. Please log in again.");
    throw new Error("Please log in again.");
  }
  if (!res.ok) {
    const detail = payload?.detail;
    const message = Array.isArray(detail) ? detail.map((d) => d.msg).join(", ") : detail;
    throw new Error(message || `Something went wrong (${res.status}).`);
  }
  return payload;
}

async function load(fresh = false) {
  if (!S.token || S.loading) return;
  S.loading = true;
  renderRefreshing();
  try {
    S.data = await api(`/api/state${fresh ? "?fresh=1" : ""}`);
    S.loadedAt = Date.now();
    S.offline = false;
    try { localStorage.setItem("wg.state", JSON.stringify(S.data)); } catch { /* storage full */ }
  } catch (err) {
    // Keep showing the last saved data; say why it couldn't refresh.
    S.offline = Boolean(S.data) && err.message.startsWith("No connection");
    if (S.token) toast(err.message, true);
  } finally {
    S.loading = false;
    render();
  }
}

function logout(message) {
  S.token = null; S.name = null; S.data = null;
  localStorage.removeItem("wg.token");
  localStorage.removeItem("wg.name");
  localStorage.removeItem("wg.state");
  location.hash = "";
  render();
  if (message) toast(message, true);
}

// ------------------------------------------------------------------------- //
// Toasts & bottom sheets                                                     //
// ------------------------------------------------------------------------- //

function toast(message, error = false) {
  const root = document.getElementById("toast-root");
  const el = document.createElement("div");
  el.className = `toast${error ? " error" : ""}`;
  el.textContent = message;
  root.replaceChildren(el);
  setTimeout(() => el.remove(), error ? 5000 : 2600);
}

function openSheet(html) {
  const root = document.getElementById("sheet-root");
  root.innerHTML = `<div class="sheet-backdrop" data-action="close-sheet"></div>
    <div class="sheet" role="dialog" aria-modal="true"><div class="grabber"></div>${html}</div>`;
}
function closeSheet() {
  document.getElementById("sheet-root").innerHTML = "";
}

// ------------------------------------------------------------------------- //
// Routing & layout                                                           //
// ------------------------------------------------------------------------- //

function route() {
  const parts = location.hash.replace(/^#\/?/, "").split("/").filter(Boolean);
  return { name: parts[0] || "home", arg: parts[1] };
}

function nav(active) {
  const link = (name, ico, label) =>
    `<a href="#/${name}" class="${active === name ? "active" : ""}"><span class="ico">${ico}</span>${label}</a>`;
  return `<nav class="nav">
    ${link("home", "🏠", "Home")}
    ${link("receipts", "🧾", "Receipts")}
    <a href="#/add" aria-label="Add"><span class="add">+</span></a>
    ${link("balances", "⚖️", "Balances")}
    ${link("stats", "📊", "Stats")}
  </nav>`;
}

function topbar(title, sub = "", right = "") {
  return `<div class="topbar"><div><h1>${esc(title)}</h1>${sub ? `<div class="sub">${esc(sub)}</div>` : ""}</div>${right}</div>`;
}

function render() {
  if (!S.token) { app.innerHTML = loginScreen(); return; }
  if (!S.data) { app.innerHTML = `<div class="boot"><div class="spinner"></div></div>`; return; }
  const r = route();
  const screens = {
    home: homeScreen, receipts: receiptsScreen, receipt: () => receiptScreen(r.arg),
    add: addScreen, scan: scanScreen, editor: editorScreen, edit: () => startEdit(r.arg),
    balances: balancesScreen, stats: statsScreen, settings: settingsScreen,
  };
  const screen = (screens[r.name] || homeScreen)();
  if (screen === null) return; // the screen redirected
  const activeTab = { receipt: "receipts", scan: "add", editor: "add", edit: "receipts" }[r.name] || r.name;
  app.innerHTML = `<main class="screen">${screen}</main>${nav(activeTab)}${
    S.offline ? `<div class="offline pill warn">Offline — showing saved data</div>` : ""}`;
  if (r.name === "editor") updateCheckBar();
}

function renderRefreshing() {
  const btn = document.querySelector("[data-action=refresh]");
  if (btn) btn.innerHTML = `<span class="spinner sm"></span>`;
}

// ------------------------------------------------------------------------- //
// Login                                                                      //
// ------------------------------------------------------------------------- //

function loginScreen() {
  return `<form class="login" data-form="login">
    <img class="logo" src="/icons/icon.svg" alt="">
    <h1>WG Finance</h1>
    <p class="muted" style="margin:0 0 24px">Shared receipts, fair splits, and who owes whom.</p>
    <div class="stack">
      <label class="field"><span>Username</span><input class="input" name="username" autocomplete="username" autocapitalize="none" required></label>
      <label class="field"><span>Password</span><input class="input" name="password" type="password" autocomplete="current-password" required></label>
      <button class="btn primary" type="submit">Log in</button>
    </div>
    <p class="muted small" style="margin-top:16px">You stay logged in on this phone. Forgot your password? Ask Shin.</p>
  </form>`;
}

async function submitLogin(form) {
  const button = form.querySelector("button");
  button.disabled = true;
  button.innerHTML = `<span class="spinner sm"></span> Logging in…`;
  try {
    const res = await api("/api/login", { method: "POST", body: { username: form.username.value, password: form.password.value } });
    S.token = res.token; S.name = res.name;
    localStorage.setItem("wg.token", res.token);
    localStorage.setItem("wg.name", res.name);
    location.hash = "#/home";
    render();
    await load();
  } catch (err) {
    toast(err.message, true);
    button.disabled = false;
    button.textContent = "Log in";
  }
}

// ------------------------------------------------------------------------- //
// Home                                                                       //
// ------------------------------------------------------------------------- //

function receiptRow(r, showDate = true) {
  const who = r.payer || "⚠ unknown";
  return `<a class="list-item" href="#/receipt/${encodeURIComponent(r.id)}">
    ${r.manual ? `<span class="avatar" style="--c:var(--surface-2);color:var(--ink)">✍️</span>` : avatar(r.payer)}
    <div class="grow"><div class="title">${esc(r.store)}</div>
      <div class="meta">${showDate ? `${esc(dayLabel(r.date))} · ` : ""}paid by ${esc(who)}${r.mismatch ? " · ⚠ check items" : ""}</div></div>
    <div class="amount">${chf(r.total)}</div></a>`;
}

function settleRows(steps, withButtons = true) {
  return steps.map((s) => `<div class="list-item" style="cursor:default">
      <span class="pair">${avatar(s.from, "sm")}${avatar(s.to, "sm")}</span>
      <div class="grow"><div class="title">${esc(s.from)} → ${esc(s.to)}</div>
        <div class="amount">${chf(s.amount)}</div></div>
      ${withButtons ? `<button class="btn small primary" data-action="settle" data-from="${esc(s.from)}" data-to="${esc(s.to)}" data-amount="${s.amount}">Paid</button>` : ""}
    </div>`).join("");
}

function homeScreen() {
  const d = S.data;
  const me = d.me;
  const month = thisMonth();
  const p = d.periods[month] || { total_paid: 0, people: {}, receipts: 0 };
  const last = d.periods[shiftMonth(month, -1)];
  let hero;
  if (me) {
    const bal = d.balances[me];
    hero = bal > 0.004
      ? `<div class="hero good"><div class="label">Your balance</div><div class="big">${chf(bal)}</div><div class="hint">Your roommates owe you this.</div></div>`
      : bal < -0.004
        ? `<div class="hero bad"><div class="label">Your balance</div><div class="big">${chf(-bal)}</div><div class="hint">You owe this — see Settle up below.</div></div>`
        : `<div class="hero"><div class="label">Your balance</div><div class="big">All square ✓</div><div class="hint">Nobody owes anybody.</div></div>`;
  } else {
    hero = `<div class="hero"><div class="label">Household</div><div class="big">${chf(p.total_paid)}</div><div class="hint">spent in ${esc(monthLabel(month))}</div></div>`;
  }
  const change = last && last.total_paid > 0 ? Math.round(((p.total_paid - last.total_paid) / last.total_paid) * 100) : null;
  const mine = me ? p.people[me] : null;
  const recent = d.receipts.slice(0, 5);
  return `${topbar(`Hi ${S.name || ""} 👋`, new Date().toLocaleDateString("en-GB", { weekday: "long", day: "numeric", month: "long" }),
      `<div class="row" style="gap:8px"><button class="icon-btn" data-action="refresh" aria-label="Refresh">↻</button>
       <a class="icon-btn" href="#/settings" aria-label="Settings">⚙️</a></div>`)}
    ${hero}
    <div class="section-title">Settle up</div>
    ${d.settle_up.length ? `<div class="list">${settleRows(d.settle_up)}</div>` : `<div class="card muted">Everyone is square 🎉</div>`}
    <div class="section-title">${esc(monthLabel(month))}</div>
    <div class="grid-2">
      <div class="tile"><div class="label">Household spent</div><div class="value num">${chf(p.total_paid)}</div>
        <div class="note">${change === null ? `${p.receipts} receipt(s)` : `${change >= 0 ? "▲" : "▼"} ${Math.abs(change)}% vs last month`}</div></div>
      <div class="tile"><div class="label">${me ? "Your share" : "Receipts"}</div>
        <div class="value num">${me ? chf(mine?.share || 0) : p.receipts}</div>
        <div class="note">${me ? `you paid ${chf(mine?.paid || 0)}` : "this month"}</div></div>
    </div>
    <div class="section-title row" style="justify-content:space-between"><span>Recent</span><a href="#/receipts" class="small" style="text-transform:none;letter-spacing:0">See all</a></div>
    ${recent.length ? `<div class="list">${recent.map((r) => receiptRow(r)).join("")}</div>`
      : `<div class="card empty"><div class="big">🧾</div>No receipts yet. Tap + to add one.</div>`}`;
}

// ------------------------------------------------------------------------- //
// Receipts list & detail                                                     //
// ------------------------------------------------------------------------- //

function monthSwitch(value, action) {
  const now = thisMonth();
  return `<div class="month-switch">
    <button data-action="${action}" data-step="-1" aria-label="Previous month" ${value === "all" ? "disabled" : ""}>‹</button>
    <div class="label">${esc(monthLabel(value))}</div>
    <button data-action="${action}" data-step="1" aria-label="Next month" ${value === "all" || value >= now ? "disabled" : ""}>›</button>
    <button class="all" data-action="${action}" data-step="all">${value === "all" ? "Month" : "All"}</button>
  </div>`;
}

function filteredReceipts() {
  const f = S.receiptsFilter;
  const q = f.search.trim().toLowerCase();
  return S.data.receipts.filter((r) =>
    (f.month === "all" || (r.date || "").startsWith(f.month)) &&
    (f.payer === "Everyone" || r.payer === f.payer) &&
    (!q || r.store.toLowerCase().includes(q) || r.items.some((i) => i.name.toLowerCase().includes(q))));
}

function receiptsListHtml() {
  const list = filteredReceipts();
  if (!list.length) return `<div class="card empty"><div class="big">🔍</div>No receipts here. Try another month or clear the search.</div>`;
  const total = list.reduce((s, r) => s + cents(r.total), 0) / 100;
  let html = `<div class="muted small" style="margin:12px 4px 0">${list.length} receipt(s) · <b>${chf(total)}</b></div>`;
  let day = null;
  let open = false;
  for (const r of list) {
    if (r.date !== day) {
      if (open) html += `</div>`;
      day = r.date;
      html += `<div class="day-header">${esc(dayLabel(r.date))}</div><div class="list">`;
      open = true;
    }
    html += receiptRow(r, false);
  }
  return html + (open ? `</div>` : "");
}

function receiptsScreen() {
  const f = S.receiptsFilter;
  return `${topbar("Receipts")}
    <div class="stack">
      ${monthSwitch(f.month, "receipts-month")}
      <input class="search" type="search" placeholder="Search store or item…" value="${esc(f.search)}" data-bind="receipts-search" enterkeyhint="search">
      <div class="chips">${["Everyone", ...ROOMMATES].map((p) =>
        `<button class="chip ${f.payer === p ? "on" : ""}" data-action="receipts-payer" data-payer="${p}">${p === "Everyone" ? "Everyone" : `Paid by ${p}`}</button>`).join("")}</div>
    </div>
    <div id="receipts-list">${receiptsListHtml()}</div>`;
}

function receiptScreen(id) {
  const r = S.data.receipts.find((x) => x.id === decodeURIComponent(id || ""));
  if (!r) return `<button class="back" data-action="back">‹ Back</button><div class="card empty">This receipt no longer exists.</div>`;
  return `<button class="back" data-action="back">‹ Receipts</button>
    <div class="detail-head">
      <div class="store">${r.manual ? "✍️ " : ""}${esc(r.store)}</div>
      <div class="total num">${chf(r.total)}</div>
      <div class="muted">${esc(dayLabel(r.date))} · paid by <b>${esc(r.payer || r.raw_payer || "unknown")}</b></div>
    </div>
    ${r.mismatch ? `<div class="banner warn" style="margin-top:12px">⚠ The items add up to ${chf(r.items_total)}, not ${chf(r.total)} (${signed(r.items_total - r.total)}). Tap Edit to fix the items or the total.</div>` : ""}
    ${!r.payer ? `<div class="banner bad" style="margin-top:12px">Payer “${esc(r.raw_payer)}” isn't a roommate, so this receipt is left out of balances. Edit it to choose who paid.</div>` : ""}
    <div class="section-title">Who pays what</div>
    <div class="card"><div class="shares">${ROOMMATES.map((n) =>
      `<span class="share" style="--c:${COLOR[n]}"><span class="dot">${n[0]}</span>${esc(n)} <b class="num">${chf(r.shares[n])}</b></span>`).join("")}</div></div>
    <div class="section-title">Items (${r.items.length})</div>
    <div class="list">${r.items.map((i) => `<div class="list-item" style="cursor:default">
        <div class="grow"><div class="title">${i.qty !== 1 ? `${+i.qty.toFixed(3)} × ` : ""}${esc(i.name)}</div>
          <div class="meta">${esc(i.category)} · for ${esc(forLabel(i.beneficiary))}</div></div>
        <div class="amount ${i.line_total < 0 ? "good" : ""}">${chf(i.line_total)}</div></div>`).join("") || `<div class="list-item muted">No items saved.</div>`}
    </div>
    ${r.header_discount ? `<div class="card" style="margin-top:10px"><div class="kv"><span class="k">Receipt discount</span><span class="num">−${chf(r.header_discount)}</span></div></div>` : ""}
    ${r.notes ? `<div class="card muted small" style="margin-top:10px">${esc(r.notes)}</div>` : ""}
    <div class="grid-2" style="margin-top:20px">
      <a class="btn" href="#/edit/${encodeURIComponent(r.id)}">✏️ Edit</a>
      <button class="btn danger" data-action="delete" data-id="${esc(r.id)}">🗑 Delete</button>
    </div>`;
}

// ------------------------------------------------------------------------- //
// Add: choose, scan, manual                                                  //
// ------------------------------------------------------------------------- //

function addScreen() {
  return `${topbar("Add", "What would you like to add?")}
    <div class="stack">
      <button class="big-choice" data-action="pick-photo" data-capture="1"><span class="emoji">📷</span>
        <span><div class="t">Scan a receipt</div><div class="d">Take a photo — the items are read for you.</div></span></button>
      <button class="big-choice" data-action="pick-photo"><span class="emoji">🖼️</span>
        <span><div class="t">Choose a photo</div><div class="d">Use a receipt picture from your gallery.</div></span></button>
      <button class="big-choice" data-action="new-manual"><span class="emoji">✍️</span>
        <span><div class="t">Payment without receipt</div><div class="d">E.g. cutlery, a deposit, a Twint payment.</div></span></button>
      <button class="big-choice" data-action="settle"><span class="emoji">💸</span>
        <span><div class="t">Record a payback</div><div class="d">Someone paid back money they owed.</div></span></button>
    </div>
    <input type="file" accept="image/*" capture="environment" class="hidden" id="photo-camera">
    <input type="file" accept="image/*" class="hidden" id="photo-gallery">`;
}

function scanScreen() {
  const sc = S.scan;
  if (!sc) { location.hash = "#/add"; return null; }
  return `<button class="back" data-action="cancel-scan">‹ Cancel</button>
    ${topbar(sc.error ? "Couldn't read it" : "Reading receipt…")}
    <img class="photo-preview" src="${sc.preview}" alt="Receipt photo">
    ${sc.error
      ? `<div class="banner bad" style="margin-top:12px">${esc(sc.error)}</div>
         <div class="grid-2" style="margin-top:12px"><button class="btn primary" data-action="retry-scan">Try again</button>
         <button class="btn" data-action="scan-to-manual">Type it in</button></div>`
      : `<div class="progress"><div></div></div>
         <p class="muted small" style="text-align:center">This usually takes 5–20 seconds. You can check and fix everything afterwards.</p>`}`;
}

async function compressImage(file) {
  // Phone photos are often 4–10 MB; the server accepts at most ~4 MB per request.
  const maxSide = 2400, maxPixels = 5_000_000;
  let bitmap;
  try {
    bitmap = await createImageBitmap(file, { imageOrientation: "from-image" });
  } catch {
    bitmap = await new Promise((resolve, reject) => {
      const img = new Image();
      img.onload = () => resolve(img);
      img.onerror = () => reject(new Error("This photo format isn't supported. Try a JPEG or PNG."));
      img.src = URL.createObjectURL(file);
    });
  }
  const w0 = bitmap.width, h0 = bitmap.height;
  const scale = Math.min(1, maxSide / Math.max(w0, h0), Math.sqrt(maxPixels / (w0 * h0)));
  const canvas = document.createElement("canvas");
  canvas.width = Math.round(w0 * scale);
  canvas.height = Math.round(h0 * scale);
  canvas.getContext("2d").drawImage(bitmap, 0, 0, canvas.width, canvas.height);
  for (const quality of [0.88, 0.8, 0.7, 0.6]) {
    const blob = await new Promise((r) => canvas.toBlob(r, "image/jpeg", quality));
    if (blob && blob.size < 3_500_000) return blob;
  }
  throw new Error("The photo is too large. Try again with a closer, simpler shot.");
}

async function startScan(file) {
  S.scan = { file, preview: URL.createObjectURL(file), error: null };
  location.hash = "#/scan";
  render();
  try {
    const blob = await compressImage(file);
    const form = new FormData();
    form.append("file", blob, "receipt.jpg");
    const parsed = await api("/api/parse", { method: "POST", form });
    if (!S.scan || S.scan.file !== file) return; // cancelled meanwhile
    S.draft = {
      mode: "receipt", id: null, store: parsed.store || "", date: parsed.date || todayISO(),
      payer: S.data.me || ROOMMATES[0], total: parsed.total || 0, header_discount: parsed.header_discount || 0, notes: "",
      items: parsed.items.length ? parsed.items : [{ name: "", category: "General", qty: 1, line_total: 0, beneficiary: "ALL" }],
      fromScan: true,
    };
    S.scan = null;
    location.hash = "#/editor";
  } catch (err) {
    if (!S.scan || S.scan.file !== file) return;
    S.scan.error = err.message;
    render();
  }
}

function newManualDraft() {
  return {
    mode: "manual", id: null, store: "", date: todayISO(), payer: S.data.me || ROOMMATES[0],
    total: 0, header_discount: 0, notes: "", category: "Household", beneficiary: "ALL", items: [],
  };
}

function startEdit(id) {
  const r = S.data.receipts.find((x) => x.id === decodeURIComponent(id || ""));
  if (!r) { location.hash = "#/receipts"; return null; }
  S.draft = {
    mode: "receipt", id: r.id, store: r.store, date: r.date || todayISO(),
    payer: r.payer || S.data.me || ROOMMATES[0], total: r.total, header_discount: r.header_discount,
    notes: r.notes, manual: r.manual, items: r.items.map((i) => ({ ...i })),
  };
  location.replace("#/editor");
  return null;
}

// ------------------------------------------------------------------------- //
// Editor (receipt or manual payment)                                         //
// ------------------------------------------------------------------------- //

function segButtons(name, options, value) {
  return `<div class="seg">${options.map(([v, label]) =>
    `<button type="button" class="${v === value ? "on" : ""}" data-action="seg" data-name="${name}" data-value="${esc(v)}">${esc(label)}</button>`).join("")}</div>`;
}

function selectHtml(attrs, options, value) {
  return `<select class="select" ${attrs}>${options.map(([v, label]) =>
    `<option value="${esc(v)}" ${v === value ? "selected" : ""}>${esc(label)}</option>`).join("")}</select>`;
}

function editorScreen() {
  const d = S.draft;
  if (!d) { location.hash = "#/add"; return null; }
  const payer = segButtons("payer", ROOMMATES.map((n) => [n, n]), d.payer);
  if (d.mode === "manual") {
    return `<button class="back" data-action="cancel-edit">‹ Cancel</button>
      ${topbar("Payment without receipt")}
      <div class="stack">
        <label class="field"><span>What was it for?</span><input class="input" data-draft="store" value="${esc(d.store)}" placeholder="e.g. Cutlery — plates, forks, knives"></label>
        <div class="grid-2">
          <label class="field"><span>Amount (CHF)</span><input class="input money" inputmode="decimal" data-draft="total" value="${d.total ? d.total : ""}" placeholder="0.00"></label>
          <label class="field"><span>Date</span><input class="input" type="date" data-draft="date" value="${esc(d.date)}"></label>
        </div>
        <div class="field"><span>Paid by</span>${payer}</div>
        <label class="field"><span>Shared between</span>${selectHtml('data-draft="beneficiary"', FOR_OPTIONS, d.beneficiary)}</label>
        <label class="field"><span>Category</span>${selectHtml('data-draft="category"', CATEGORIES.map((c) => [c, c]), d.category)}</label>
        <label class="field"><span>Note (optional)</span><input class="input" data-draft="notes" value="${esc(d.notes)}"></label>
      </div>
      <div class="check-bar" id="check-bar"></div>`;
  }
  return `<button class="back" data-action="cancel-edit">‹ Cancel</button>
    ${topbar(d.id ? "Edit receipt" : "Check receipt", d.fromScan ? "Read from your photo — please check the amounts." : "")}
    <div class="stack">
      <label class="field"><span>Store</span><input class="input" data-draft="store" value="${esc(d.store)}"></label>
      <div class="grid-2">
        <label class="field"><span>Date</span><input class="input" type="date" data-draft="date" value="${esc(d.date)}"></label>
        <label class="field"><span>Total on receipt</span><input class="input money" inputmode="decimal" data-draft="total" value="${esc(money2(d.total))}"></label>
      </div>
      <div class="field"><span>Paid by</span>${payer}</div>
    </div>
    <div class="section-title">Items (${d.items.length})</div>
    <div id="items">${d.items.map(itemCard).join("")}</div>
    <button class="btn ghost" data-action="add-item" style="margin-top:6px">+ Add item</button>
    <details class="card" style="margin-top:10px"><summary class="muted">More: receipt discount & note</summary>
      <div class="stack" style="margin-top:10px">
        <label class="field"><span>Receipt-wide discount (CHF, not already in the items)</span><input class="input money" inputmode="decimal" data-draft="header_discount" value="${d.header_discount || ""}" placeholder="0.00"></label>
        <label class="field"><span>Note</span><input class="input" data-draft="notes" value="${esc(d.notes)}"></label>
      </div></details>
    <div class="check-bar" id="check-bar"></div>`;
}

function itemCard(item, i) {
  return `<div class="item-card" data-index="${i}">
    <div class="top">
      <input class="input" data-item="${i}" data-field="name" value="${esc(item.name)}" placeholder="Item name">
      <input class="input money" inputmode="decimal" data-item="${i}" data-field="line_total" value="${esc(money2(item.line_total))}">
    </div>
    <div class="bottom">
      ${selectHtml(`data-item="${i}" data-field="beneficiary" aria-label="Who is it for"`, FOR_OPTIONS, item.beneficiary)}
      ${selectHtml(`data-item="${i}" data-field="category" aria-label="Category"`, CATEGORIES.map((c) => [c, c]), item.category)}
      <button class="remove" data-action="remove-item" data-index="${i}" aria-label="Remove item">✕</button>
    </div></div>`;
}

function draftItems(d) {
  if (d.mode === "manual") {
    return [{ name: d.store, category: d.category, qty: 1, line_total: d.total, beneficiary: d.beneficiary }];
  }
  return d.items;
}

// Preview only — the server recomputes the exact split when saving.
function previewShares(items, headerDiscount) {
  const exact = { A: 0, B: 0, C: 0 };
  for (const item of items) {
    const code = item.beneficiary === "ALL" ? "ABC" : item.beneficiary;
    for (const c of code) exact[c] += cents(item.line_total) / code.length;
  }
  const disc = cents(headerDiscount);
  if (disc) {
    const w = Object.fromEntries(Object.entries(exact).map(([k, v]) => [k, Math.max(0, v)]));
    const total = w.A + w.B + w.C || 3;
    for (const c of "ABC") exact[c] -= (disc * (w.A + w.B + w.C ? w[c] : 1)) / total;
  }
  return Object.fromEntries(ROOMMATES.map((n) => [n, Math.round(exact[CODE[n]]) / 100]));
}

function updateCheckBar() {
  const bar = document.getElementById("check-bar");
  const d = S.draft;
  if (!bar || !d) return;
  const items = draftItems(d);
  const itemsCents = items.reduce((s, i) => s + cents(i.line_total), 0) - cents(d.header_discount);
  const gap = itemsCents - cents(d.total);
  const shares = previewShares(items, d.header_discount);
  const status = d.mode === "manual"
    ? `<div class="status muted">Split:</div>`
    : Math.abs(gap) <= 1
      ? `<div class="status good">✓ Items match the total (${chf(itemsCents / 100)})</div>`
      : `<div class="status warn">⚠ Items add up to ${chf(itemsCents / 100)} — ${chf(Math.abs(gap) / 100)} ${gap > 0 ? "more" : "less"} than the total</div>`;
  bar.innerHTML = `${status}
    <div class="shares" style="margin-bottom:10px">${ROOMMATES.filter((n) => Math.abs(shares[n]) > 0.004).map((n) =>
      `<span class="share" style="--c:${COLOR[n]}"><span class="dot">${n[0]}</span><b class="num">${chf(shares[n])}</b></span>`).join("") || `<span class="muted small">Enter an amount</span>`}</div>
    <button class="btn primary" data-action="save-draft">${d.id ? "Save changes" : "Save"}</button>`;
}

async function saveDraft(button) {
  const d = S.draft;
  const items = draftItems(d).map((i) => ({ ...i, line_total: parseMoney(i.line_total), qty: Number(i.qty) || 1 }));
  if (!d.store.trim()) return toast(d.mode === "manual" ? "Say what it was for." : "Enter the store name.", true);
  if (d.mode === "manual" && !(parseMoney(d.total) > 0)) return toast("Enter an amount above 0.", true);
  if (!items.length) return toast("Add at least one item.", true);
  const body = {
    date: d.date || todayISO(), store: d.store.trim(), payer: d.payer, total: parseMoney(d.total),
    header_discount: parseMoney(d.header_discount), notes: d.notes || "", manual: d.mode === "manual" || !!d.manual, items,
  };
  button.disabled = true;
  button.innerHTML = `<span class="spinner sm"></span> Saving…`;
  try {
    if (d.id) await api(`/api/receipts/${encodeURIComponent(d.id)}`, { method: "PUT", body });
    else await api("/api/receipts", { method: "POST", body });
    toast(d.id ? "Changes saved ✓" : `Saved ${chf(body.total)} — paid by ${body.payer} ✓`);
    const wasEdit = d.id;
    S.draft = null;
    location.hash = wasEdit ? `#/receipt/${encodeURIComponent(wasEdit)}` : "#/home";
    await load(true);
  } catch (err) {
    toast(err.message, true);
    button.disabled = false;
    button.textContent = d.id ? "Save changes" : "Save";
  }
}

// ------------------------------------------------------------------------- //
// Balances                                                                   //
// ------------------------------------------------------------------------- //

function balancesScreen() {
  const d = S.data;
  const cards = ROOMMATES.map((n) => {
    const b = d.balances[n];
    const pill = b > 0.004 ? `<span class="pill good">▲ is owed</span>` : b < -0.004 ? `<span class="pill bad">▼ owes</span>` : `<span class="pill">● square</span>`;
    return `<div class="list-item" style="cursor:default">${avatar(n)}
      <div class="grow"><div class="title">${esc(n)}${n === d.me ? ` <span class="muted small">(you)</span>` : ""}</div>${pill}</div>
      <div class="amount ${b > 0.004 ? "good" : b < -0.004 ? "bad" : ""}">${chf(Math.abs(b))}</div></div>`;
  }).join("");
  const parts = ROOMMATES.map((n) => {
    const p = d.balance_parts[n];
    return `<div class="card"><div class="row">${avatar(n, "sm")}<b>${esc(n)}</b></div>
      <div class="kv small"><span class="k">Paid at the till</span><span class="num">${chf(p.paid)}</span></div>
      <div class="kv small"><span class="k">− Their share of items</span><span class="num">${chf(p.share)}</span></div>
      <div class="kv small"><span class="k">+ Paybacks sent</span><span class="num">${chf(p.settlements_paid)}</span></div>
      <div class="kv small"><span class="k">− Paybacks received</span><span class="num">${chf(p.settlements_received)}</span></div>
      <div class="kv"><span class="k"><b>= Balance</b></span><b class="num">${signed(d.balances[n])}</b></div></div>`;
  }).join("");
  return `${topbar("Balances", "Who owes whom right now", `<button class="icon-btn" data-action="refresh" aria-label="Refresh">↻</button>`)}
    ${Math.abs(d.unallocated) > 0.009 ? `<div class="banner warn" style="margin-bottom:10px">⚠ Some receipts' items don't add up to their totals (${chf(Math.abs(d.unallocated))} unassigned). Fix the ⚠ receipts so balances add up.</div>` : ""}
    <div class="list">${cards}</div>
    <div class="section-title">Settle up</div>
    ${d.settle_up.length ? `<div class="list">${settleRows(d.settle_up)}</div><p class="muted small" style="margin:8px 4px">Tap “Paid” after sending the money — it's recorded and balances update.</p>`
      : `<div class="card muted">Everyone is square 🎉</div>`}
    <button class="btn" data-action="settle" style="margin-top:10px">💸 Record another payback</button>
    <div class="section-title">Payback history</div>
    ${d.settlements.length ? `<div class="list">${d.settlements.slice(0, 30).map((s) => `<div class="list-item" style="cursor:default">
        <span class="pair">${avatar(s.from, "sm")}${avatar(s.to, "sm")}</span>
        <div class="grow"><div class="title">${esc(s.from)} → ${esc(s.to)}</div><div class="meta">${esc(dayLabel(s.date))} · ${esc(s.method)}</div></div>
        <div class="amount">${chf(s.amount)}</div></div>`).join("")}</div>` : `<div class="card muted">No paybacks yet.</div>`}
    <details style="margin-top:16px"><summary class="section-title" style="display:list-item;cursor:pointer">How is this calculated?</summary>
      <div class="stack">${parts}</div>
      <p class="muted small" style="margin:8px 4px">Each item is split evenly between the people it's for. Receipt-wide discounts are shared in proportion to what each person bought.</p>
    </details>`;
}

function settleSheet({ from, to, amount } = {}) {
  const me = S.data.me;
  from = from || me || ROOMMATES[1];
  to = to || ROOMMATES.find((n) => n !== from);
  openSheet(`<h2>Record a payback</h2>
    <form class="stack" data-form="settle">
      <div class="field"><span>Who paid?</span>${segButtons("settle-from", ROOMMATES.map((n) => [n, n]), from)}</div>
      <div class="field"><span>To whom?</span>${segButtons("settle-to", ROOMMATES.map((n) => [n, n]), to)}</div>
      <label class="field"><span>Amount (CHF)</span><input class="input money" name="amount" inputmode="decimal" value="${amount ? amount.toFixed(2) : ""}" placeholder="0.00" required></label>
      <div class="field"><span>How?</span>${segButtons("settle-method", METHODS.map((m) => [m, m === "Bank Transfer" ? "Bank" : m]), "Twint")}</div>
      <button class="btn primary" type="submit">Save payback</button>
      <button class="btn" type="button" data-action="close-sheet">Cancel</button>
    </form>`);
}

async function submitSettle(form) {
  const sheet = form.closest(".sheet");
  const val = (name) => sheet.querySelector(`.seg button.on[data-name="${name}"]`)?.dataset.value;
  const body = { from_roommate: val("settle-from"), to_roommate: val("settle-to"), amount: parseMoney(form.amount.value), method: val("settle-method") };
  if (body.from_roommate === body.to_roommate) return toast("Choose two different people.", true);
  if (!(body.amount > 0)) return toast("Enter an amount above 0.", true);
  const button = form.querySelector("button[type=submit]");
  button.disabled = true;
  button.innerHTML = `<span class="spinner sm"></span> Saving…`;
  try {
    await api("/api/settlements", { method: "POST", body });
    closeSheet();
    toast(`${body.from_roommate} paid ${body.to_roommate} ${chf(body.amount)} ✓`);
    await load(true);
  } catch (err) {
    toast(err.message, true);
    button.disabled = false;
    button.textContent = "Save payback";
  }
}

// ------------------------------------------------------------------------- //
// Stats                                                                      //
// ------------------------------------------------------------------------- //

function bars(rows, color) {
  const max = Math.max(...rows.map((r) => r.amount), 0.01);
  return rows.map((r) => `<div class="bar-row"><span class="name">${esc(r.name)}</span>
    <div class="bar"><div style="width:${(r.amount / max) * 100}%;--c:${color}"></div></div>
    <span class="num small">${nf.format(r.amount)}</span></div>`).join("");
}

function trendChart(endMonth) {
  const months = [];
  let m = endMonth === "all" ? thisMonth() : endMonth;
  for (let i = 0; i < 6; i++) { months.unshift(m); m = shiftMonth(m, -1); }
  const totals = months.map((mo) => S.data.periods[mo]?.total_share || 0);
  const max = Math.max(...totals, 1);
  return `<div class="trend">${months.map((mo, i) => {
      const p = S.data.periods[mo];
      const segs = ROOMMATES.slice().reverse().map((n) => {
        const v = p?.people[n]?.share || 0;
        return v > 0 ? `<div class="seg-bar" style="height:${(v / max) * 100}%;background:${COLOR[n]}" title="${esc(n)} ${chf(v)}"></div>` : "";
      }).join("");
      const [y, mm] = mo.split("-").map(Number);
      return `<div style="flex:1;display:flex;flex-direction:column;height:100%">
        <div class="tot">${totals[i] ? Math.round(totals[i]) : ""}</div>
        <div class="col">${segs}</div>
        <div class="lbl">${new Date(y, mm - 1, 1).toLocaleDateString("en-GB", { month: "short" })}</div></div>`;
    }).join("")}</div>
    <div class="legend">${ROOMMATES.map((n) => `<span><i style="--c:${COLOR[n]}"></i>${esc(n)}</span>`).join("")}</div>`;
}

function statsScreen() {
  const month = S.statsMonth;
  const p = S.data.periods[month] || { receipts: 0, total_paid: 0, total_share: 0, people: {}, categories: [], stores: [], mismatched: [] };
  const people = ROOMMATES.map((n) => {
    const x = p.people[n] || { paid: 0, share: 0, receipts_paid: 0 };
    const diff = x.paid - x.share;
    const pill = diff > 0.004 ? `<span class="pill good">fronted ${chf(diff)} for others</span>`
      : diff < -0.004 ? `<span class="pill bad">used ${chf(-diff)} more than paid</span>` : `<span class="pill">even</span>`;
    const pct = p.total_share ? Math.round((x.share / p.total_share) * 100) : 0;
    return `<div class="card"><div class="row">${avatar(n)}<div class="grow"><b>${esc(n)}</b><div>${pill}</div></div></div>
      <div class="grid-2" style="margin-top:10px">
        <div><div class="muted small">Paid at the till</div><div class="amount" style="font-size:1.15rem">${chf(x.paid)}</div><div class="muted small">${x.receipts_paid} receipt(s)</div></div>
        <div><div class="muted small">Their share</div><div class="amount" style="font-size:1.15rem">${chf(x.share)}</div><div class="muted small">${pct}% of the total</div></div>
      </div></div>`;
  }).join("");
  return `${topbar("Stats")}
    ${monthSwitch(month, "stats-month")}
    <div class="grid-3" style="margin-top:10px">
      <div class="tile"><div class="label">Spent</div><div class="value num" style="font-size:1.05rem">${chf(p.total_paid)}</div></div>
      <div class="tile"><div class="label">Receipts</div><div class="value">${p.receipts}</div></div>
      <div class="tile"><div class="label">Per person</div><div class="value num" style="font-size:1.05rem">${chf(p.total_share / 3)}</div></div>
    </div>
    ${p.mismatched.length ? `<div class="banner warn" style="margin-top:10px">⚠ ${p.mismatched.length} receipt(s) here don't add up — “paid” and “share” totals differ.</div>` : ""}
    <div class="section-title">Who spent how much</div>
    <div class="stack">${people}</div>
    <div class="section-title">Categories</div>
    <div class="card">${p.categories.length ? bars(p.categories, "#6250d6") : `<span class="muted">Nothing yet.</span>`}</div>
    <div class="section-title">Top stores</div>
    <div class="card">${p.stores.length ? bars(p.stores, "#6250d6") : `<span class="muted">Nothing yet.</span>`}</div>
    <div class="section-title">Last 6 months · shares</div>
    <div class="card">${trendChart(month)}</div>`;
}

// ------------------------------------------------------------------------- //
// Settings                                                                   //
// ------------------------------------------------------------------------- //

function settingsScreen() {
  const ios = /iphone|ipad|ipod/i.test(navigator.userAgent);
  const standalone = matchMedia("(display-mode: standalone)").matches || navigator.standalone;
  return `<button class="back" data-action="back">‹ Back</button>
    ${topbar("Settings")}
    <div class="card row">${avatar(S.name)}<div class="grow"><b>${esc(S.name)}</b><div class="muted small">Logged in on this phone</div></div></div>
    ${standalone ? "" : `<div class="section-title">Install the app</div><div class="card small">${ios
      ? "In Safari, tap the Share button <b>⎋</b>, then <b>Add to Home Screen</b>."
      : "In Chrome, open the ⋮ menu and tap <b>Install app</b> (or <b>Add to Home screen</b>)."}
      It then opens full-screen like a normal app.</div>`}
    <div class="section-title">Change password</div>
    <form class="card stack" data-form="password">
      <input class="input" type="password" name="old" placeholder="Current password" autocomplete="current-password" required>
      <input class="input" type="password" name="new" placeholder="New password (min. 6 characters)" autocomplete="new-password" minlength="6" required>
      <button class="btn" type="submit">Update password</button>
    </form>
    <div class="section-title">Data</div>
    <button class="btn" data-action="refresh-now">↻ Reload from Google Sheets</button>
    <button class="btn danger" data-action="logout" style="margin-top:10px">Log out</button>`;
}

// ------------------------------------------------------------------------- //
// Events                                                                     //
// ------------------------------------------------------------------------- //

document.addEventListener("click", async (e) => {
  const el = e.target.closest("[data-action]");
  if (!el) return;
  const a = el.dataset.action;
  if (a === "back") { history.length > 1 ? history.back() : (location.hash = "#/home"); }
  else if (a === "refresh") { load(true); }
  else if (a === "refresh-now") { await load(true); toast("Up to date ✓"); }
  else if (a === "logout") { logout(); }
  else if (a === "close-sheet") { closeSheet(); }
  else if (a === "settle") { settleSheet({ from: el.dataset.from, to: el.dataset.to, amount: el.dataset.amount ? Number(el.dataset.amount) : undefined }); }
  else if (a === "receipts-month" || a === "stats-month") {
    const key = a === "stats-month" ? "statsMonth" : null;
    const cur = key ? S.statsMonth : S.receiptsFilter.month;
    const step = el.dataset.step;
    const next = step === "all" ? (cur === "all" ? thisMonth() : "all") : shiftMonth(cur, Number(step));
    if (key) S.statsMonth = next; else S.receiptsFilter.month = next;
    render();
  }
  else if (a === "receipts-payer") { S.receiptsFilter.payer = el.dataset.payer; render(); }
  else if (a === "pick-photo") { document.getElementById(el.dataset.capture ? "photo-camera" : "photo-gallery").click(); }
  else if (a === "cancel-scan") { S.scan = null; location.hash = "#/add"; }
  else if (a === "retry-scan") { if (S.scan) startScan(S.scan.file); }
  else if (a === "scan-to-manual") {
    S.scan = null;
    S.draft = { mode: "receipt", id: null, store: "", date: todayISO(), payer: S.data.me || ROOMMATES[0], total: 0, header_discount: 0, notes: "",
      items: [{ name: "", category: "General", qty: 1, line_total: 0, beneficiary: "ALL" }] };
    location.hash = "#/editor";
  }
  else if (a === "new-manual") { S.draft = newManualDraft(); location.hash = "#/editor"; }
  else if (a === "cancel-edit") { S.draft = null; history.length > 1 ? history.back() : (location.hash = "#/home"); }
  else if (a === "seg") {
    el.parentElement.querySelectorAll("button").forEach((b) => b.classList.toggle("on", b === el));
    if (el.dataset.name === "payer" && S.draft) S.draft.payer = el.dataset.value;
  }
  else if (a === "add-item") {
    S.draft.items.push({ name: "", category: "General", qty: 1, line_total: 0, beneficiary: "ALL" });
    document.getElementById("items").insertAdjacentHTML("beforeend", itemCard(S.draft.items.at(-1), S.draft.items.length - 1));
    document.querySelector(`[data-item="${S.draft.items.length - 1}"][data-field="name"]`)?.focus();
    updateCheckBar();
  }
  else if (a === "remove-item") {
    S.draft.items.splice(Number(el.dataset.index), 1);
    document.getElementById("items").innerHTML = S.draft.items.map(itemCard).join("");
    updateCheckBar();
  }
  else if (a === "save-draft") { saveDraft(el); }
  else if (a === "delete") {
    openSheet(`<h2>Delete this receipt?</h2><p class="muted">It is removed from the Google Sheet with all its items, and balances update.</p>
      <div class="stack"><button class="btn danger" data-action="confirm-delete" data-id="${esc(el.dataset.id)}">Delete</button>
      <button class="btn" data-action="close-sheet">Cancel</button></div>`);
  }
  else if (a === "confirm-delete") {
    el.disabled = true;
    try {
      await api(`/api/receipts/${encodeURIComponent(el.dataset.id)}`, { method: "DELETE" });
      closeSheet();
      toast("Receipt deleted");
      location.hash = "#/receipts";
      await load(true);
    } catch (err) { toast(err.message, true); el.disabled = false; }
  }
});

document.addEventListener("input", (e) => {
  const t = e.target;
  if (t.dataset.bind === "receipts-search") {
    S.receiptsFilter.search = t.value;
    document.getElementById("receipts-list").innerHTML = receiptsListHtml();
    return;
  }
  if (!S.draft) return;
  if (t.dataset.draft) {
    S.draft[t.dataset.draft] = t.value;
    updateCheckBar();
  } else if (t.dataset.item !== undefined) {
    S.draft.items[Number(t.dataset.item)][t.dataset.field] = t.value;
    updateCheckBar();
  }
});
document.addEventListener("change", (e) => {
  const t = e.target;
  if (t.id === "photo-camera" || t.id === "photo-gallery") {
    const file = t.files?.[0];
    t.value = "";
    if (file) startScan(file);
    return;
  }
  // <select> fires change (and input) — reuse the input handler.
  if (t.tagName === "SELECT") t.dispatchEvent(new Event("input", { bubbles: true }));
});

document.addEventListener("submit", async (e) => {
  const form = e.target.closest("[data-form]");
  if (!form) return;
  e.preventDefault();
  if (form.dataset.form === "login") submitLogin(form);
  else if (form.dataset.form === "settle") submitSettle(form);
  else if (form.dataset.form === "password") {
    try {
      await api("/api/password", { method: "POST", body: { old_password: form.old.value, new_password: form.new.value } });
      form.reset();
      toast("Password updated ✓");
    } catch (err) { toast(err.message, true); }
  }
});

window.addEventListener("hashchange", () => { closeSheet(); render(); window.scrollTo(0, 0); });
document.addEventListener("visibilitychange", () => {
  if (document.visibilityState === "visible" && Date.now() - S.loadedAt > REFRESH_AFTER_MS) load();
});

if ("serviceWorker" in navigator) {
  navigator.serviceWorker.register("/sw.js").catch(() => {});
}

render();
load();
