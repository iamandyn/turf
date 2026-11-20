// Turf frontend: a brand's results in, a market report out. No build step: plain
// ES module + MapLibre (vendored, global `maplibregl`).

const MAX_RESULT_ROWS = 20000;  // matches MAX_ROWS in performance.py
const MAX_SIGNALS = 20;         // matches MAX_SIGNALS in markets.py
const TOP_RECS = 5;             // shown up front; the rest behind "show more"
const REC_LIMIT = 15;

const state = {
  brand: null,     // {name, synthetic, sampleKey, description, rows, geo_type, kpi}
  preset: null,    // brand category (a preset key)
  signals: null,   // null = the preset's; an array once adjusted
  selected: null,  // metro code highlighted in the report and on the map
  showAll: false,
};
let presets = [];
let samples = [];
let report = null;   // the last /api/performance response
let pending = null;  // an uploaded file waiting for its category
let requestSeq = 0;

const $ = (id) => document.getElementById(id);
const fmt = new Intl.NumberFormat("en-US");
const compact = new Intl.NumberFormat("en-US", { notation: "compact", maximumFractionDigits: 1 });
const money = new Intl.NumberFormat("en-US", { style: "currency", currency: "USD", maximumFractionDigits: 0 });
const short = (name) => name.split(",")[0].split("-")[0];
const kpiValue = (kpi, v) => (v === null || v === undefined ? "n/a" : kpi === "roas" ? `${v.toFixed(2)}x` : money.format(v));
const kpiName = (kpi) => (kpi === "roas" ? "ROAS" : "CPA");

// ---------- API ----------

class ApiError extends Error {}

async function api(path, options = {}) {
  let resp;
  try {
    resp = await fetch(path, options);
  } catch {
    throw new ApiError("Can't reach the server. Check your connection and try again.");
  }
  if (resp.status === 429) {
    const wait = resp.headers.get("Retry-After") || "a few";
    throw new ApiError(`Too many requests. Try again in ${wait} seconds.`);
  }
  if (!resp.ok) {
    let detail = `Request failed (${resp.status}).`;
    try {
      const body = await resp.json();
      if (typeof body.detail === "string") detail = body.detail;
      else if (Array.isArray(body.detail) && body.detail[0]?.msg) detail = body.detail[0].msg.replace(/^Value error, /, "");
    } catch { /* not JSON */ }
    throw new ApiError(detail);
  }
  return resp;
}

const getJson = async (path) => (await api(path)).json();
const postJson = async (path, body) =>
  (await api(path, { method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify(body) })).json();

function reportRequest() {
  return {
    rows: state.brand.rows, geo_type: state.brand.geo_type, kpi: state.brand.kpi,
    preset: state.signals ? null : state.preset,
    categories: state.signals,
    limit: REC_LIMIT,
  };
}

function showError(id, message) {
  const el = $(id);
  el.textContent = message || "";
  el.hidden = !message;
}

// ---------- Upload ----------

// Minimal CSV parser: quoted fields, doubled quotes, comma / semicolon / tab.
function parseCsv(text) {
  const firstLine = text.slice(0, text.indexOf("\n") >>> 0);
  const delim = [",", ";", "\t"].sort((a, b) => firstLine.split(b).length - firstLine.split(a).length)[0];
  const rows = [];
  let row = [], field = "", quoted = false;
  for (let i = 0; i < text.length; i++) {
    const ch = text[i];
    if (quoted) {
      if (ch === '"' && text[i + 1] === '"') { field += '"'; i++; }
      else if (ch === '"') quoted = false;
      else field += ch;
    } else if (ch === '"') quoted = true;
    else if (ch === delim) { row.push(field); field = ""; }
    else if (ch === "\n" || ch === "\r") {
      if (ch === "\r" && text[i + 1] === "\n") i++;
      row.push(field); field = "";
      if (row.some((v) => v.trim())) rows.push(row);
      row = [];
    } else field += ch;
  }
  row.push(field);
  if (row.some((v) => v.trim())) rows.push(row);
  return rows;
}

// Column names ad platforms use (Meta Ads Manager, Google Ads geo reports, generic exports).
const COLUMNS = {
  zip: ["zip", "zipcode", "zip code", "postal code", "postcode", "postal"],
  geo: ["metro", "market", "dma", "designated market area", "metro area", "city", "location", "region name", "geo", "area"],
  state: ["state", "region", "state code", "province"],
  spend: ["spend", "cost", "amount spent", "amount spent usd", "spend usd", "cost usd", "media cost"],
  conversions: ["conversions", "purchases", "results", "leads", "installs", "app installs", "signups", "sign ups", "orders", "subscriptions"],
  revenue: ["revenue", "conversion value", "conv value", "total conv value", "purchase value", "purchases conversion value", "value", "sales"],
};

// Returns {rows, geo_type, kpi} or throws an Error with a message for the user.
function parseResults(text) {
  const table = parseCsv(text.replace(/^﻿/, ""));
  if (table.length < 2) throw new Error("That file has no data rows.");
  const norm = (h) => h.toLowerCase().replace(/[()$.,]/g, "").replace(/[_-]/g, " ").replace(/\s+/g, " ").trim();
  const header = table[0].map(norm);
  const find = (names) => header.findIndex((h) => names.includes(h));
  const zip = find(COLUMNS.zip), geo = find(COLUMNS.geo), stateCol = find(COLUMNS.state);
  const spend = find(COLUMNS.spend), conv = find(COLUMNS.conversions), rev = find(COLUMNS.revenue);
  if (zip < 0 && geo < 0) throw new Error('Couldn\'t find a location column. Name it "zip", or "market" with values like "Austin, TX".');
  if (spend < 0) throw new Error('Couldn\'t find a spend column ("spend", "cost" or "amount spent").');
  if (conv < 0 && rev < 0) throw new Error('Couldn\'t find conversions or revenue ("conversions", "purchases", "revenue", "conversion value"...).');
  const num = (v) => { const n = Number(String(v ?? "").replace(/[$,\s]/g, "")); return Number.isFinite(n) && n >= 0 ? n : null; };
  const rows = [];
  for (const r of table.slice(1)) {
    let where = zip >= 0 ? (r[zip] || "").trim() : (r[geo] || "").trim();
    // Separate city and state columns (common in Google Ads geo reports).
    if (zip < 0 && stateCol >= 0 && stateCol !== geo && where && !where.includes(",") && r[stateCol]) where = `${where}, ${r[stateCol].trim()}`;
    const s = num(r[spend]);
    if (!where || s === null) continue;
    rows.push({ geo: where.slice(0, 120), spend: s, conversions: conv >= 0 ? num(r[conv]) : null, revenue: rev >= 0 ? num(r[rev]) : null });
  }
  if (!rows.length) throw new Error("No rows had a location and a spend amount.");
  if (rows.length > MAX_RESULT_ROWS) throw new Error(`That's ${fmt.format(rows.length)} rows; the limit is ${fmt.format(MAX_RESULT_ROWS)}. Aggregate by market or ZIP first.`);
  const kpi = rev >= 0 && rows.some((r) => r.revenue) ? "roas" : "cpa";
  return { rows, geo_type: zip >= 0 ? "zip" : "metro", kpi };
}

async function onFile(file) {
  if (!file) return;
  showError("start-error", "");
  if (file.size > 10 * 1024 * 1024) { showError("start-error", "That file is over 10 MB. Aggregate it by market or ZIP first."); return; }
  try {
    const parsed = parseResults(await file.text());
    pending = { name: file.name.replace(/\.[^.]+$/, ""), synthetic: false, sampleKey: null, ...parsed };
    $("file-summary").textContent = `${file.name}: ${fmt.format(parsed.rows.length)} rows by ${parsed.geo_type === "zip" ? "ZIP code" : "market"}, measured by ${kpiName(parsed.kpi)} (${parsed.kpi === "roas" ? "higher is better" : "lower is better"}).`;
    $("upload-step").hidden = false;
    $("category").focus();
  } catch (err) {
    showError("start-error", err.message);
  } finally {
    $("results-file").value = "";
  }
}

function downloadCsv(filename, lines) {
  const a = document.createElement("a");
  a.href = URL.createObjectURL(new Blob([lines.join("\n") + "\n"], { type: "text/csv" }));
  a.download = filename;
  document.body.append(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(a.href), 1000);
}

function downloadExample() {
  downloadCsv("turf-example-results.csv", [
    "market,spend,purchases,revenue",
    '"Denver, CO",12500,410,31200', '"Austin, TX",9800,355,27100', '"Seattle, WA",11200,300,22900',
    '"Atlanta, GA",8700,190,13800', '"Chicago, IL",15400,420,30100', '"Phoenix, AZ",7600,150,10200',
    '"Boston, MA",10300,330,25600', '"Dallas, TX",9900,240,17800',
  ]);
}

// ---------- Start screen ----------

function renderStart() {
  const cards = $("sample-cards");
  cards.innerHTML = "";
  for (const smp of samples) {
    const b = document.createElement("button");
    b.type = "button";
    b.className = "sample-card";
    b.dataset.key = smp.key;
    const name = document.createElement("span");
    name.className = "name";
    const label = smp.name.replace(/^Sample /, "");
    name.textContent = label.charAt(0).toUpperCase() + label.slice(1);
    const desc = document.createElement("span");
    desc.className = "desc";
    desc.textContent = `${smp.description} · measured by ${kpiName(smp.kpi)}`;
    b.append(name, desc);
    b.addEventListener("click", () => openSample(smp.key));
    cards.append(b);
  }
  const select = $("category");
  select.innerHTML = "";
  for (const p of presets) select.add(new Option(`${p.label} (e.g. ${p.example})`, p.key));
  const hint = () => {
    const p = presets.find((x) => x.key === select.value);
    $("category-hint").textContent = p ? `Compares metros by their share of: ${p.categories.map((c) => c.replaceAll("_", " ")).join(", ")}.` : "";
  };
  select.onchange = hint;
  hint();
  const connectSelect = $("connect-sample");
  connectSelect.innerHTML = "";
  for (const smp of samples) {
    const label = smp.name.replace(/^Sample /, "");
    connectSelect.add(new Option(`${label.charAt(0).toUpperCase()}${label.slice(1)} (measured by ${kpiName(smp.kpi)})`, smp.key));
  }
}

async function openSample(key) {
  showError("start-error", "");
  try {
    const smp = await getJson(`/api/samples/${key}`);
    state.brand = { name: smp.name, synthetic: true, sampleKey: key, description: smp.description,
                    rows: smp.rows, geo_type: smp.geo_type, kpi: smp.kpi };
    state.preset = smp.preset;
    state.signals = null;
    state.selected = null;
    state.showAll = false;
    await buildReport({ fit: true });
  } catch (err) {
    showError("start-error", err.message);
  }
}

function buildFromUpload() {
  if (!pending) return;
  state.brand = pending;
  state.preset = $("category").value;
  state.signals = null;
  state.selected = null;
  state.showAll = false;
  buildReport({ fit: true });
}

function changeBrand() {
  syncStream?.close();
  syncStream = null;
  setSyncBusy(false);
  state.brand = null;
  report = null;
  pending = null;
  $("upload-step").hidden = true;
  showView();
  clearMap();
  syncUrl();
  $("start").scrollIntoView({ block: "start" });
}

function showView() {
  const connected = Boolean(state.brand?.connection);
  const inReport = Boolean(state.brand && (report || connected));
  $("start").hidden = inReport;
  $("report").hidden = !inReport;
  // A connection's view opens before its first report: only the sync panel shows.
  $("report").classList.toggle("awaiting", !report);
  $("sync-panel").hidden = !connected;
  $("actionbar").hidden = !report;
  document.querySelector(".legend").hidden = !report;
}

// ---------- Report ----------

async function buildReport({ fit = false } = {}) {
  const mine = ++requestSeq;
  $("loading").hidden = false;
  showError("error", "");
  showError("start-error", "");
  try {
    const result = await postJson("/api/performance", reportRequest());
    if (mine !== requestSeq) return;
    report = result;
    showView();
    renderReport();
    await drawMap({ fit });
    syncUrl();
    if (state.selected) selectRec(state.selected, { scroll: false });
  } catch (err) {
    if (mine !== requestSeq) return;
    if (report && state.brand) showError("error", err.message);
    else { state.brand = null; showView(); showError("start-error", err.message); }
  } finally {
    if (mine === requestSeq) $("loading").hidden = true;
  }
}

const presetLabel = () => (presets.find((p) => p.key === state.preset) || {}).label || "this brand";
const currentSignals = () => state.signals || (presets.find((p) => p.key === state.preset) || {}).categories || [];

function renderReport() {
  const r = report, b = state.brand, k = r.kpi;
  $("report-title").textContent = b.name;
  $("synthetic-note").hidden = !b.synthetic;
  $("report-meta").textContent = `${presetLabel()} · ${fmt.format(r.metros.length)} markets · ${money.format(r.total_spend)} spend · measured by ${kpiName(k)}`;

  // Where to focus next
  const best = r.metros.filter((m) => m.best).map((m) => short(m.name));
  $("focus-intro").textContent = `Metro areas that look most like ${b.synthetic ? "the brand" : b.name}'s best markets (${best.slice(0, 3).join(", ")}${best.length > 3 ? " and others" : ""}), where it spends little or nothing today.`;
  const ol = $("recs");
  ol.innerHTML = "";
  const shown = state.showAll ? r.recommendations.results : r.recommendations.results.slice(0, TOP_RECS);
  shown.forEach((rec, i) => ol.append(recCard(rec, r.details[i])));
  const more = $("more-recs");
  const extra = r.recommendations.results.length - TOP_RECS;
  more.hidden = extra <= 0;
  more.textContent = state.showAll ? "Show the top 5 only" : `Show ${extra} more`;

  // Trust
  const labels = { strong: "High", moderate: "Medium", weak: "Low", too_few: "Not enough data" };
  const plain = {
    strong: "The patterns behind these recommendations predicted this brand's results in markets they weren't fitted on.",
    moderate: "There's some evidence the patterns predict this brand's results. Treat the list as a shortlist to test.",
    weak: "These patterns don't predict this brand's results; other factors drive its performance. Treat the list as exploratory.",
    too_few: "Too few markets with enough spend to check the patterns against the brand's results.",
  };
  const badge = $("verdict-badge");
  badge.textContent = `${labels[r.check.verdict]} confidence`;
  badge.className = `badge ${r.check.verdict}`;
  $("verdict-text").textContent = plain[r.check.verdict];
  $("verdict-detail").textContent = r.check.summary;

  // Drivers
  const drivers = $("drivers");
  drivers.innerHTML = "";
  const helpful = r.check.signals.filter((f) => f.weight > 0 && f.rho >= 0.15);
  if (!helpful.length || r.check.verdict === "weak" || r.check.verdict === "too_few") {
    const li = document.createElement("li");
    li.textContent = "No kind of local business clearly tracks this brand's results; the recommendations weigh all of them equally.";
    drivers.append(li);
  }
  for (const f of helpful.slice(0, 3)) {
    if (r.check.verdict === "weak" || r.check.verdict === "too_few") break;
    const li = document.createElement("li");
    const strong = document.createElement("strong");
    strong.textContent = f.label;
    li.append(strong, document.createTextNode(
      f.best_ratio >= 1.05
        ? `: the best markets have ${f.best_ratio.toFixed(1)}x the typical share, and more of it goes with better results.`
        : `: more of it goes with better results across the brand's markets.`));
    drivers.append(li);
  }
  renderSignalBars();

  // Today
  const trusted = r.metros.filter((m) => m.trusted);
  const fillMini = (id, list) => {
    const el = $(id);
    el.innerHTML = "";
    for (const m of list) {
      const li = document.createElement("li");
      li.textContent = `${short(m.name)}: ${kpiValue(k, m.kpi)} ${kpiName(k)}, ${money.format(m.spend)}`;
      li.title = m.name;
      el.append(li);
    }
  };
  fillMini("today-best", trusted.slice(0, 5));
  fillMini("today-worst", trusted.slice(-5).reverse());
  $("kpi-col").textContent = kpiName(k);
  $("all-markets-title").textContent = `All ${fmt.format(r.metros.length)} markets in the data`;
  const tbody = $("all-markets");
  tbody.innerHTML = "";
  for (const m of r.metros) {
    const tr = document.createElement("tr");
    tr.dataset.code = m.metro_code;
    if (!m.trusted) tr.className = "muted";
    [m.name + (m.best ? " ★" : ""), money.format(m.spend), m.trusted ? kpiValue(k, m.kpi) : "too little spend"].forEach((text, i) => {
      const td = document.createElement("td");
      td.textContent = text;
      if (i) td.className = "num-col";
      tr.append(td);
    });
    tbody.append(tr);
  }

  // Method
  $("method-matched").textContent = `${fmt.format(r.matched_rows)} of ${fmt.format(r.rows)} rows` +
    (r.unmatched_examples.length ? `; not matched, e.g. ${r.unmatched_examples.slice(0, 3).join("; ")}` : "");
  $("method-kpi").textContent = k === "roas" ? "return on ad spend (revenue / spend)" : "cost per conversion (spend / conversions)";
  $("method-category").textContent = presetLabel().toLowerCase();
  renderSignalChips();
}

function renderSignalBars() {
  const ul = $("signal-bars");
  ul.innerHTML = "";
  for (const f of report.check.signals) {
    const li = document.createElement("li");
    const name = document.createElement("span");
    name.className = "bar-label";
    name.textContent = f.label;
    const track = document.createElement("span");
    track.className = "bar-track centered";
    const fill = document.createElement("span");
    fill.className = f.rho >= 0 ? "bar-fill up" : "bar-fill";
    const width = Math.min(50, Math.abs(f.rho) * 50);
    fill.style.width = `${width}%`;
    fill.style.marginLeft = f.rho >= 0 ? "50%" : `${50 - width}%`;
    track.append(fill);
    const value = document.createElement("span");
    value.className = "bar-value";
    value.textContent = `${f.rho >= 0 ? "+" : ""}${f.rho.toFixed(2)}`;
    li.append(name, track, value);
    ul.append(li);
  }
}

function recCard(rec, d) {
  const k = report.kpi;
  const li = document.createElement("li");
  li.className = "rec";
  li.dataset.code = rec.metro_code;
  const head = document.createElement("button");
  head.type = "button";
  head.className = "rec-head";
  head.setAttribute("aria-expanded", "false");
  const rank = document.createElement("span");
  rank.className = "rank";
  rank.textContent = rec.rank;
  const name = document.createElement("span");
  name.className = "name";
  name.textContent = rec.name;
  const status = document.createElement("span");
  status.className = "status-tag";
  status.textContent = d.status === "low_spend" ? `low spend (${money.format(d.spend)})` : "not running yet";
  head.append(rank, name, status);

  const summary = document.createElement("div");
  summary.className = "rec-summary";
  const stats = document.createElement("div");
  stats.className = "rec-stats";
  if (d.expected_kpi !== null) {
    stats.append(stat(
      `Expected ${kpiName(k)}`,
      kpiValue(k, d.expected_kpi),
      `${kpiValue(k, d.expected_range[0])}–${kpiValue(k, d.expected_range[1])}`,
      "headline",
    ));
  }
  const g = rec.growth_pct;
  stats.append(stat("People", compact.format(rec.population), `${g >= 0 ? "+" : ""}${g.toFixed(1)}% since 2020`));
  summary.append(stats);
  if (d.similar.length) {
    const like = document.createElement("p");
    like.className = "rec-like";
    like.textContent = `Like ${d.similar.map((m) => short(m.name)).join(", ")}`;
    like.title = "The brand's most similar existing markets, which the expected result comes from";
    summary.append(like);
  }
  // The kinds of business most over- and under-represented, as in the snapshot.
  const reasons = rec.reasons.filter((x) => x.signal !== "population_growth");
  const notable = [
    ...reasons.filter((x) => x.ratio >= 1.2).slice(0, 3),
    ...reasons.filter((x) => x.ratio < 0.7).slice(0, 1),
  ];
  if (notable.length) {
    const chips = document.createElement("ul");
    chips.className = "rec-chips";
    chips.setAttribute("aria-label", "Local businesses vs. the typical metro");
    for (const x of notable) {
      const c = document.createElement("li");
      c.className = x.ratio >= 1 ? "up" : "down";
      c.textContent = `${x.label} ${x.ratio >= 10 ? Math.round(x.ratio) : x.ratio.toFixed(1)}×`;
      c.title = `${x.value}% of local businesses vs ${x.typical}% in the typical metro`;
      chips.append(c);
    }
    summary.append(chips);
  }

  const body = document.createElement("div");
  body.className = "rec-body";
  body.hidden = true;
  body.append(...recDetails(rec, d));

  head.addEventListener("click", () => selectRec(rec.metro_code, { toggle: true }));
  li.append(head, summary, body);
  return li;
}

function stat(label, value, sub, cls = "") {
  const box = document.createElement("div");
  box.className = `stat ${cls}`.trim();
  const l = document.createElement("span");
  l.className = "stat-label";
  l.textContent = label;
  const v = document.createElement("span");
  v.className = "stat-value";
  v.textContent = value;
  const s = document.createElement("span");
  s.className = "stat-sub";
  s.textContent = sub;
  box.append(l, v, s);
  return box;
}

function recDetails(rec, d) {
  const k = report.kpi;
  const out = [];
  const h1 = document.createElement("h4");
  h1.textContent = "Most similar markets the brand already runs in";
  const sim = document.createElement("ul");
  sim.className = "similar";
  for (const m of d.similar) {
    const li = document.createElement("li");
    li.textContent = `${m.name}: ${kpiValue(k, m.kpi)} ${kpiName(k)}`;
    sim.append(li);
  }
  const h2 = document.createElement("h4");
  h2.textContent = "Local businesses vs. the typical metro";
  const bars = document.createElement("ul");
  bars.className = "bars";
  const maxRatio = Math.max(2, ...rec.reasons.map((x) => x.ratio));
  for (const x of rec.reasons) {
    const li = document.createElement("li");
    const name = document.createElement("span");
    name.className = "bar-label";
    name.textContent = x.label;
    const track = document.createElement("span");
    track.className = "bar-track";
    track.style.setProperty("--typical", `${(1 / maxRatio) * 100}%`);
    const fill = document.createElement("span");
    fill.className = x.ratio >= 1 ? "bar-fill up" : "bar-fill";
    fill.style.width = `${Math.min(100, (x.ratio / maxRatio) * 100)}%`;
    track.append(fill);
    const value = document.createElement("span");
    value.className = "bar-value";
    value.textContent = `${x.ratio >= 10 ? Math.round(x.ratio) : x.ratio.toFixed(1)}x`;
    value.title = `${x.value}% of local businesses vs ${x.typical}% in the typical metro`;
    li.append(name, track, value);
    bars.append(li);
  }
  const facts = document.createElement("p");
  facts.className = "small muted";
  facts.textContent = `${fmt.format(rec.population)} people (2025) · ${rec.growth_pct >= 0 ? "+" : ""}${rec.growth_pct.toFixed(1)}% since 2020 · ${fmt.format(rec.zip_count)} ZIP codes in the export · ${rec.match}% match to the best markets`;
  const brief = document.createElement("div");
  brief.className = "brief";
  out.push(h1, sim, h2, bars, facts, brief);
  return out;
}

async function loadBrief(code, box) {
  if (box.dataset.loaded) return;
  box.dataset.loaded = "1";
  try {
    const b = await getJson(`/api/briefs/${state.preset}/${code}`);
    const h = document.createElement("h4");
    h.textContent = "Creative brief";
    const headline = document.createElement("p");
    headline.className = "brief-headline";
    headline.textContent = b.headline;
    const audience = document.createElement("p");
    audience.textContent = b.audience;
    const ul = document.createElement("ul");
    for (const item of [...b.angles, ...b.local_context]) {
      const li = document.createElement("li");
      li.textContent = item;
      ul.append(li);
    }
    box.append(h, headline, audience, ul);
  } catch {
    // No pre-written brief for this metro (see "About this demo").
  }
}

function selectRec(code, { toggle = false, scroll = true } = {}) {
  const card = document.querySelector(`.rec[data-code="${code}"]`);
  clearMarketHighlight();
  const already = state.selected === code;
  state.selected = toggle && already ? null : code;
  document.querySelectorAll(".rec").forEach((li) => {
    const open = li.dataset.code === state.selected;
    li.classList.toggle("open", open);
    li.querySelector(".rec-head").setAttribute("aria-expanded", String(open));
    li.querySelector(".rec-body").hidden = !open;
  });
  syncUrl();
  const rec = report.recommendations.results.find((x) => x.metro_code === state.selected);
  map.getSource("selected")?.setData(toFeatures(rec ? [rec] : []));
  if (!rec || !card) return;
  loadBrief(code, card.querySelector(".brief"));
  map.fitBounds([[rec.bounds[0], rec.bounds[1]], [rec.bounds[2], rec.bounds[3]]], { padding: 80, maxZoom: 8, duration: 600 });
  if (scroll) card.scrollIntoView({ behavior: "smooth", block: "nearest" });
}

// ---------- Adjust signals ----------

function renderSignalChips() {
  const ul = $("signal-chips");
  ul.innerHTML = "";
  currentSignals().forEach((key) => {
    const li = document.createElement("li");
    li.className = "chip";
    const text = document.createElement("span");
    text.textContent = key.replaceAll("_", " ");
    const x = document.createElement("button");
    x.type = "button";
    x.textContent = "×";
    x.setAttribute("aria-label", `Remove ${key.replaceAll("_", " ")}`);
    x.addEventListener("click", () => {
      const next = currentSignals().filter((s) => s !== key);
      if (!next.length) { showError("error", "Keep at least one kind of business."); return; }
      state.signals = next;
      buildReport();
    });
    li.append(text, x);
    ul.append(li);
  });
  $("signal-input").disabled = currentSignals().length >= MAX_SIGNALS;
}

function setupSignalSearch() {
  const input = $("signal-input");
  const list = $("signal-results");
  let items = [], active = -1, timer = null, seq = 0;
  const close = () => {
    list.hidden = true;
    input.setAttribute("aria-expanded", "false");
    input.removeAttribute("aria-activedescendant");
    active = -1;
  };
  const render = () => {
    list.innerHTML = "";
    if (!items.length) {
      const li = document.createElement("li");
      li.className = "none";
      li.textContent = "No matching kinds of business.";
      list.append(li);
    }
    items.forEach((c, i) => {
      const li = document.createElement("li");
      li.id = `signal-results-${i}`;
      li.setAttribute("role", "option");
      li.setAttribute("aria-selected", String(i === active));
      const name = document.createElement("span");
      name.textContent = c.label;
      const meta = document.createElement("span");
      meta.className = "meta";
      meta.textContent = `${fmt.format(c.n_open)} in the US`;
      li.append(name, meta);
      li.addEventListener("mousedown", (e) => { e.preventDefault(); pick(i); });
      list.append(li);
    });
    list.hidden = false;
    input.setAttribute("aria-expanded", "true");
    if (active >= 0) input.setAttribute("aria-activedescendant", `signal-results-${active}`);
  };
  const pick = (i) => {
    const c = items[i];
    if (!c) return;
    close();
    input.value = "";
    state.signals = [...currentSignals(), c.key].slice(0, MAX_SIGNALS);
    buildReport();
  };
  input.addEventListener("input", () => {
    clearTimeout(timer);
    const q = input.value.trim();
    if (q.length < 2) { close(); return; }
    timer = setTimeout(async () => {
      const mine = ++seq;
      try {
        const results = await getJson(`/api/categories/search?q=${encodeURIComponent(q)}&limit=8`);
        if (mine !== seq) return;
        items = results.filter((c) => !currentSignals().includes(c.key));
        active = items.length ? 0 : -1;
        render();
      } catch (err) { showError("error", err.message); }
    }, 200);
  });
  input.addEventListener("keydown", (e) => {
    if (list.hidden) return;
    if (e.key === "ArrowDown") { active = Math.min(active + 1, items.length - 1); render(); e.preventDefault(); }
    else if (e.key === "ArrowUp") { active = Math.max(active - 1, 0); render(); e.preventDefault(); }
    else if (e.key === "Enter") { if (active >= 0) pick(active); e.preventDefault(); }
    else if (e.key === "Escape") close();
  });
  input.addEventListener("blur", () => setTimeout(close, 100));
}

// ---------- Theme ----------

const MAP_STYLES = {
  light: "https://tiles.openfreemap.org/styles/positron",
  dark: "https://tiles.openfreemap.org/styles/dark",
};
const darkQuery = window.matchMedia("(prefers-color-scheme: dark)");

// The saved choice (theme.js put it on <html>), else the system setting.
function currentTheme() {
  const forced = document.documentElement.dataset.theme;
  return forced === "light" || forced === "dark" ? forced : darkQuery.matches ? "dark" : "light";
}

const cssVar = (name) => getComputedStyle(document.documentElement).getPropertyValue(name).trim();

// Map layer colors come from the CSS tokens, so they follow the theme.
function themeLayerPaint() {
  const accent = cssVar("--accent"), own = cssVar("--own"), panel = cssVar("--panel"), text = cssVar("--text");
  return {
    "current-circle": { "circle-color": panel, "circle-stroke-color": cssVar("--skip") },
    "results-circle": { "circle-color": accent, "circle-stroke-color": panel },
    "seeds-circle": { "circle-color": own, "circle-stroke-color": panel },
    "selected-ring": { "circle-stroke-color": text },
  };
}

function applyLayerTheme() {
  for (const [layer, paint] of Object.entries(themeLayerPaint())) {
    if (!map.getLayer(layer)) continue;
    for (const [prop, value] of Object.entries(paint)) map.setPaintProperty(layer, prop, value);
  }
}

const OUR_SOURCES = ["current", "results", "seeds", "selected"];
let mapTheme = currentTheme();

// Swap the basemap, carrying our sources (with their data) and layers over.
function applyMapTheme() {
  const theme = currentTheme();
  if (theme === mapTheme) return;
  mapTheme = theme;
  map.setStyle(MAP_STYLES[theme], {
    diff: false,
    transformStyle: (prev, next) => {
      if (!prev) return next;
      const sources = { ...next.sources };
      for (const id of OUR_SOURCES) if (prev.sources[id]) sources[id] = prev.sources[id];
      const ours = prev.layers.filter((l) => OUR_SOURCES.includes(l.source));
      return { ...next, sources, layers: [...next.layers, ...ours] };
    },
  });
  map.once("style.load", applyLayerTheme);
}

function renderThemeToggle() {
  $("theme-toggle").setAttribute("aria-pressed", String(currentTheme() === "dark"));
}

function toggleTheme() {
  const next = currentTheme() === "dark" ? "light" : "dark";
  document.documentElement.dataset.theme = next;
  try { localStorage.setItem("turf-theme", next); } catch { /* not saved; still applied */ }
  renderThemeToggle();
  applyLayerTheme();
  applyMapTheme();
}

darkQuery.addEventListener("change", () => {
  if (document.documentElement.dataset.theme) return;
  renderThemeToggle();
  applyLayerTheme();
  applyMapTheme();
});

// ---------- Map ----------

// The data covers the 50 states (no territories). Open on the lower 48; let the
// map pan as far as Alaska and Hawaii but no further. The west edge stays off
// -180: with renderWorldCopies off, MapLibre 4.7 wraps it and pins the view to it.
// The south edge leaves room to center the lower 48 in a tall, narrow map.
const LOWER_48 = [[-125, 24.3], [-66.9, 49.5]];
const US_LIMITS = [[-179, 5], [-55, 72]];
const usView = { padding: 24 };

const map = new maplibregl.Map({
  container: "map",
  style: MAP_STYLES[mapTheme],
  bounds: LOWER_48,
  fitBoundsOptions: usView,
  maxBounds: US_LIMITS,
  renderWorldCopies: false,
  attributionControl: { compact: true },
});
map.addControl(new maplibregl.NavigationControl({ showCompass: false }), "top-right");
const mapReady = new Promise((resolve) => map.on("load", resolve));

function toFeatures(rows, kind = "rec") {
  return {
    type: "FeatureCollection",
    features: rows.map((r) => ({
      type: "Feature",
      geometry: { type: "Point", coordinates: r.center },
      properties: { metro_code: r.metro_code, name: r.name, population: r.population || 400000, kind },
    })),
  };
}

// Circle area grows with population (radius ~ sqrt).
const sizeByPopulation = ["interpolate", ["linear"], ["sqrt", ["get", "population"]], 200, 4, 1500, 11, 4500, 22];

async function setupLayers() {
  await mapReady;
  const empty = { type: "FeatureCollection", features: [] };
  for (const id of OUR_SOURCES) map.addSource(id, { type: "geojson", data: empty });
  // The brand's other markets: context, kept quieter than the recommendations.
  map.addLayer({ id: "current-circle", type: "circle", source: "current",
    paint: { "circle-radius": 4.5, "circle-color": "#ffffff", "circle-opacity": 0.75, "circle-stroke-color": "#9aa0a6", "circle-stroke-width": 1.2, "circle-stroke-opacity": 0.8 } });
  map.addLayer({ id: "seeds-circle", type: "circle", source: "seeds",
    paint: { "circle-radius": 8, "circle-color": "#2e6fe4", "circle-opacity": 0.85, "circle-stroke-color": "#ffffff", "circle-stroke-width": 1.2 } });
  map.addLayer({ id: "results-circle", type: "circle", source: "results",
    paint: { "circle-radius": sizeByPopulation, "circle-color": "#e4572e", "circle-opacity": 0.8, "circle-stroke-color": "#ffffff", "circle-stroke-width": 1.2 } });
  map.addLayer({ id: "selected-ring", type: "circle", source: "selected",
    paint: {
      "circle-radius": ["match", ["get", "kind"], "rec", ["+", sizeByPopulation, 4], "best", 12, 9],
      "circle-color": "rgba(0,0,0,0)", "circle-stroke-color": "#1d1d1f", "circle-stroke-width": 2.5,
    } });
  applyLayerTheme();
  map.on("click", "results-circle", (e) => {
    const code = e.features[0].properties.metro_code;
    if (!state.showAll && !document.querySelector(`.rec[data-code="${code}"]`)) { state.showAll = true; renderReport(); }
    selectRec(code);
  });
  for (const layer of ["seeds-circle", "current-circle"]) {
    map.on("click", layer, (e) => {
      showPopup(e.features[0]);  // touch screens have no hover
      selectMarket(e.features[0].properties.metro_code);
    });
  }
  // Moving straight from one layer's dot to another's fires the new layer's
  // mouseenter before the old one's mouseleave, so a layer only closes its own popup.
  let hovered = null;
  for (const layer of ["results-circle", "seeds-circle", "current-circle"]) {
    map.on("mouseenter", layer, (e) => {
      hovered = layer;
      map.getCanvas().style.cursor = "pointer";
      showPopup(e.features[0]);
    });
    map.on("mouseleave", layer, () => {
      if (hovered !== layer) return;
      hovered = null;
      map.getCanvas().style.cursor = "";
      popup.remove();
    });
  }
}
const popup = new maplibregl.Popup({ closeButton: false, closeOnClick: false, offset: 10 });

// A dot's popup: the metro and what the report says about it.
function showPopup(feature) {
  const { metro_code: code, name, kind } = feature.properties;
  const k = report.kpi;
  const lines = [];
  if (kind === "rec") {
    const i = report.recommendations.results.findIndex((x) => x.metro_code === code);
    const d = report.details[i];
    lines.push(`Recommended #${i + 1}`);
    if (d?.expected_kpi != null) lines.push(`Expected ${kpiName(k)} about ${kpiValue(k, d.expected_kpi)}`);
  } else {
    const m = report.metros.find((x) => x.metro_code === code);
    lines.push(m.best ? "Best today" : "Running now");
    lines.push(m.trusted
      ? `${kpiValue(k, m.kpi)} ${kpiName(k)} · ${money.format(m.spend)} spend`
      : `${money.format(m.spend)} spend, too little to measure`);
  }
  const box = document.createElement("div");
  const title = document.createElement("strong");
  title.textContent = name;
  box.append(title);
  for (const text of lines) {
    const line = document.createElement("span");
    line.className = "popup-line";
    line.textContent = text;
    box.append(line);
  }
  popup.setLngLat(feature.geometry.coordinates).setDOMContent(box).addTo(map);
}

// One of the brand's existing markets: ring it and show its row in the report.
function selectMarket(code) {
  if (state.selected) selectRec(state.selected, { toggle: true });
  const m = report.metros.find((x) => x.metro_code === code);
  if (!m) return;
  map.getSource("selected").setData(toFeatures([m], m.best ? "best" : "current"));
  $("today-fold").open = true;
  document.querySelector(".brand-table").open = true;
  let row = null;
  document.querySelectorAll("#all-markets tr").forEach((tr) => {
    const hit = tr.dataset.code === code;
    tr.classList.toggle("highlight", hit);
    if (hit) row = tr;
  });
  row?.scrollIntoView({ behavior: "smooth", block: "center" });
}

function clearMarketHighlight() {
  document.querySelectorAll("#all-markets tr.highlight").forEach((tr) => tr.classList.remove("highlight"));
}

async function drawMap({ fit = false } = {}) {
  await mapReady;
  const recs = report.recommendations.results;
  const best = report.metros.filter((m) => m.best);
  const current = report.metros.filter((m) => !m.best);
  map.getSource("results").setData(toFeatures(recs));
  map.getSource("seeds").setData(toFeatures(best, "best"));
  map.getSource("current").setData(toFeatures(current, "current"));
  if (fit) {
    const bounds = new maplibregl.LngLatBounds();
    [...recs, ...best].forEach((r) => bounds.extend(r.center));
    map.fitBounds(bounds, { padding: 60, maxZoom: 6, duration: 600 });
  }
}

async function clearMap() {
  await mapReady;
  for (const id of OUR_SOURCES) map.getSource(id).setData(toFeatures([]));
  map.fitBounds(LOWER_48, { ...usView, duration: 600 });
}

const LAYER_GROUPS = { results: ["results-circle"], seeds: ["seeds-circle"], current: ["current-circle"] };

// ---------- Export and print ----------

async function exportCsv(format, button) {
  if (!state.brand) return;
  button.disabled = true;
  showError("error", "");
  try {
    const resp = await api(`/api/performance/export?format=${format}`, {
      method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify(reportRequest()),
    });
    const blob = await resp.blob();
    const match = /filename="([^"]+)"/.exec(resp.headers.get("Content-Disposition") || "");
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = match ? match[1] : `turf-${format}.csv`;
    document.body.append(a);
    a.click();
    a.remove();
    setTimeout(() => URL.revokeObjectURL(a.href), 1000);
  } catch (err) {
    showError("error", err.message);
  } finally {
    button.disabled = false;
  }
}

// Print shows every recommendation with its details.
window.addEventListener("beforeprint", () => {
  if (!report) return;
  state.showAll = true;
  renderReport();
  document.querySelectorAll(".rec").forEach((li) => {
    li.querySelector(".rec-body").hidden = false;
    loadBrief(li.dataset.code, li.querySelector(".brief"));
  });
  document.querySelectorAll("details").forEach((d) => { d.open = true; });
});

// ---------- Ad-account connector (simulated platform; see connector/ on the server) ----------

let syncStream = null;  // the open EventSource, if a sync is being followed
const shortDate = new Intl.DateTimeFormat("en-US", { month: "short", day: "numeric", timeZone: "UTC" });
const day = (iso) => shortDate.format(new Date(`${iso}T00:00:00Z`));
const dayRange = (a, b) => (a === b ? day(a) : `${day(a)} – ${day(b)}`);

async function connectSample(key) {
  showError("start-error", "");
  const button = $("connect");
  button.disabled = true;
  try {
    const created = await postJson("/api/connections", { sample: key });
    openConnection(created.connection);
    followSync(created.sync.id);
  } catch (err) {
    showError("start-error", err.message);
  } finally {
    button.disabled = false;
  }
}

// Show a connection's report view; the report itself appears once a sync has finished.
function openConnection(conn) {
  const smp = conn.sample;
  state.brand = { name: smp.name, synthetic: true, sampleKey: null, description: smp.description,
                  rows: [], geo_type: "metro", kpi: smp.kpi,
                  connection: { id: conn.id, accountId: conn.account_id } };
  state.preset = smp.preset;
  state.signals = null;
  state.selected = null;
  state.showAll = false;
  report = null;
  $("sync-log").innerHTML = "";
  $("sync-changes").hidden = true;
  renderConnection(conn);
  $("report-title").textContent = smp.name;
  $("report-meta").textContent = `${presets.find((p) => p.key === smp.preset)?.label || ""} · measured by ${kpiName(smp.kpi)}`;
  $("synthetic-note").hidden = false;
  showView();
  clearMap();
  syncUrl();
}

function renderConnection(conn, platformDate) {
  const parts = [conn.account_id];
  if (conn.synced_through) {
    const since = new Date(`${conn.synced_through}T00:00:00Z`);
    since.setUTCDate(since.getUTCDate() - 27);
    parts.push(`synced through ${day(conn.synced_through)}`,
               `report on the last 28 days (${dayRange(since.toISOString().slice(0, 10), conn.synced_through)})`,
               `${fmt.format(conn.stored_rows)} daily rows stored`);
  } else {
    parts.push("not synced yet");
  }
  if (platformDate) parts.push(`platform date ${day(platformDate)}`);
  $("sync-meta").textContent = parts.join(" · ");
}

async function refreshConnection(platformDate) {
  const c = state.brand?.connection;
  if (!c) return;
  try {
    renderConnection(await getJson(`/api/connections/${c.id}`), platformDate);
  } catch { /* the meta line just stays as it was */ }
}

function setSyncBusy(busy, label) {
  for (const id of ["sync-now", "advance-1", "advance-7", "disconnect"]) $(id).disabled = busy;
  const status = $("sync-status");
  status.textContent = label || "";
  status.className = `sync-status${busy ? " busy" : ""}`;
  $("sync-progress").hidden = !busy;
}

function logLine(text, kind = "") {
  const li = document.createElement("li");
  li.className = kind;
  li.textContent = text;
  const log = $("sync-log");
  log.append(li);
  log.scrollTop = log.scrollHeight;
}

// Follow a sync's progress over server-sent events, then rebuild the report.
function followSync(syncId) {
  const c = state.brand?.connection;
  if (!c) return;
  syncStream?.close();
  $("sync-log-wrap").open = true;
  $("sync-bar").style.width = "0%";
  setSyncBusy(true, "Syncing…");
  let range = null;
  let startedAt = null;
  const es = new EventSource(`/api/connections/${c.id}/syncs/${syncId}/events`);
  syncStream = es;
  const on = (kind, fn) => es.addEventListener(kind, (e) => {
    if (syncStream !== es) return;  // superseded (brand changed, or another sync followed)
    const data = JSON.parse(e.data);
    startedAt ??= data.at;
    fn(data);
  });
  on("queued", () => logLine("Queued"));
  on("auth", (d) => logLine(d.reason === "first request"
    ? "Got an access token from the platform"
    : `Token rejected (${d.reason}); got a new one`, "auth"));
  on("started", (d) => {
    range = [Date.parse(d.since), Date.parse(d.until)];
    logLine(d.mode === "backfill"
      ? `Backfill: ${dayRange(d.since, d.until)} from ${d.account}`
      : `Incremental: ${dayRange(d.since, d.until)}, re-fetching the last 3 synced days for late conversions`);
  });
  on("retry", (d) => logLine(`${d.status} ${d.reason}: retrying in ${d.wait_s}s${d.retry_after ? " (Retry-After)" : " (backoff)"}`, "retry"));
  on("page", (d) => {
    const parts = [];
    if (d.inserted) parts.push(`${d.inserted} new`);
    if (d.updated) parts.push(`${d.updated} updated`);
    if (d.unchanged) parts.push(`${d.unchanged} unchanged`);
    if (d.rejected) parts.push(`${d.rejected} rejected (${d.reasons.join("; ")})`);
    const days = d.first_day ? ` (${dayRange(d.first_day, d.last_day)})` : "";
    logLine(`Page ${d.page}: ${d.rows} rows${days}: ${parts.join(", ") || "empty"}`, d.updated ? "updated" : "");
    if (range && d.last_day) {
      const span = Math.max(1, range[1] - range[0]);
      $("sync-bar").style.width = `${Math.min(95, ((Date.parse(d.last_day) - range[0]) / span) * 95)}%`;
    }
  });
  on("fetched", (d) => logLine(`Fetched ${fmt.format(d.rows)} rows in ${d.pages} pages: ${fmt.format(d.inserted)} new, ${fmt.format(d.updated)} updated, ${fmt.format(d.unchanged)} unchanged · ${d.requests} requests, ${d.retries} retried`, "summary"));
  on("report", (d) => {
    if (d.stage === "started") logLine(`Recomputing the report on ${dayRange(d.since, d.until)} (${d.markets} markets)…`);
    else { logLine("Report updated", "summary"); renderChanges(d.changes); }
  });
  on("done", async (d) => {
    es.close();
    syncStream = null;
    $("sync-bar").style.width = "100%";
    logLine(`Done in ${(d.at - startedAt).toFixed(1)}s`, "summary");
    setSyncBusy(false, "Synced");
    $("sync-log-wrap").open = false;  // the report needs the room; the log is a click away
    await loadConnectionReport();
  });
  on("failed", (d) => {
    es.close();
    syncStream = null;
    logLine(d.error || "The sync failed.", "error");
    setSyncBusy(false, "Sync failed");
    refreshConnection();
  });
  es.onerror = () => {
    // The browser reconnects by itself (resuming after the last event id) unless the
    // stream is closed for good.
    if (es.readyState === EventSource.CLOSED && syncStream === es) {
      syncStream = null;
      logLine("Lost the connection to the progress stream.", "error");
      setSyncBusy(false, "Disconnected");
    }
  };
}

async function loadConnectionReport() {
  const c = state.brand?.connection;
  if (!c) return;
  try {
    const res = await getJson(`/api/connections/${c.id}/results`);
    state.brand.rows = res.rows;
    await buildReport({ fit: !report });
    refreshConnection();
  } catch (err) {
    showError("error", err.message);
  }
}

function renderChanges(changes) {
  const ul = $("sync-changes");
  ul.innerHTML = "";
  const add = (text, cls = "") => { const li = document.createElement("li"); li.className = cls; li.textContent = text; ul.append(li); };
  if (!changes || changes.first) {
    add("First sync: the report below is built from the last 28 days of synced results.");
  } else {
    const k = state.brand.kpi;
    const { before, after } = changes.kpi;
    if (before != null && after != null && changes.window.before) {
      const exact = (v) => (k === "roas" ? `${v.toFixed(2)}x` : `$${v.toFixed(2)}`);
      add(`${kpiName(k)} over the last 28 days: ${exact(before)} → ${exact(after)} (now ${dayRange(...changes.window.after)})`, "kpi");
    }
    for (const e of changes.entered) add(`${short(e.name)} entered the top 10 at #${e.rank}`, "up");
    for (const m of changes.moved) add(`${short(m.name)} moved #${m.from} → #${m.to}`, m.to < m.from ? "up" : "down");
    for (const l of changes.left) add(`${short(l.name)} left the top 10 (was #${l.was})`, "down");
    if (changes.verdict.before !== changes.verdict.after) add(`Confidence changed: ${changes.verdict.before} → ${changes.verdict.after}`);
    if (!changes.entered.length && !changes.moved.length && !changes.left.length) add("No change in the top 10 recommendations.");
  }
  ul.hidden = false;
}

async function startSync() {
  const c = state.brand?.connection;
  if (!c) return;
  showError("error", "");
  try {
    const resp = await fetch(`/api/connections/${c.id}/syncs`, { method: "POST" });
    const body = await resp.json();
    if (resp.status === 409 && body.sync) { followSync(body.sync.id); return; }  // already running: follow it
    if (!resp.ok) throw new ApiError(body.detail || `Request failed (${resp.status}).`);
    followSync(body.id);
  } catch (err) {
    showError("error", err instanceof ApiError ? err.message : "Can't reach the server.");
  }
}

async function advancePlatform(days) {
  const c = state.brand?.connection;
  if (!c) return;
  try {
    const acct = await postJson(`/mock/ads/_demo/accounts/${c.accountId}/advance`, { days });
    logLine(`Platform date moved to ${day(acct.current_date)}: new results through ${day(acct.data_through)}. Sync to fetch them.`, "platform");
    $("sync-log-wrap").open = true;
    refreshConnection(acct.current_date);
    $("sync-now").classList.add("primary");
  } catch (err) {
    showError("error", err.message);
  }
}

async function disconnect() {
  const c = state.brand?.connection;
  if (!c) return;
  try { await api(`/api/connections/${c.id}`, { method: "DELETE" }); } catch { /* gone already */ }
  changeBrand();
}

async function restoreConnection(id) {
  try {
    const conn = await getJson(`/api/connections/${id}`);
    openConnection(conn);
    const latest = conn.latest_sync;
    if (latest && (latest.status === "queued" || latest.status === "running")) followSync(latest.id);
    else if (conn.synced_through) {
      if (latest?.result?.changes) renderChanges(latest.result.changes);
      await loadConnectionReport();
    } else startSync();
  } catch (err) {
    changeBrand();
    showError("start-error", err instanceof ApiError && /expire|No such/.test(err.message)
      ? "That ad-account connection has expired. Connect again below." : err.message);
  }
}

// ---------- URL state ----------

function syncUrl() {
  const p = new URLSearchParams();
  if (state.brand?.sampleKey) {
    p.set("sample", state.brand.sampleKey);
    if (state.selected) p.set("metro", state.selected);
  } else if (state.brand?.connection) {
    p.set("connection", state.brand.connection.id);
  }
  const qs = p.toString();
  history.replaceState(null, "", qs ? `?${qs}` : location.pathname);
}

// ---------- About dialog ----------

function setupAbout() {
  const dialog = $("about");
  $("about-open").addEventListener("click", () => dialog.showModal());
  const remember = () => {
    try { localStorage.setItem("turf-about-seen", "1"); } catch { /* shown again next time */ }
  };
  // "close" fires asynchronously; the button's submit is immediate, so a quick
  // reload right after dismissing doesn't bring the notice back.
  dialog.querySelector("form").addEventListener("submit", remember);
  dialog.addEventListener("close", remember);  // Escape key
  let seen = false;
  try { seen = localStorage.getItem("turf-about-seen") === "1"; } catch { /* not available */ }
  if (!seen) dialog.showModal();
}

// ---------- Wiring ----------

function init() {
  $("results-file").addEventListener("change", (e) => onFile(e.target.files[0]));
  $("example-csv").addEventListener("click", downloadExample);
  $("build-report").addEventListener("click", buildFromUpload);
  $("change-brand").addEventListener("click", changeBrand);
  $("more-recs").addEventListener("click", () => { state.showAll = !state.showAll; renderReport(); });
  $("print-report").addEventListener("click", () => window.print());
  $("connect").addEventListener("click", () => connectSample($("connect-sample").value));
  $("sync-now").addEventListener("click", () => { $("sync-now").classList.remove("primary"); startSync(); });
  $("advance-1").addEventListener("click", () => advancePlatform(1));
  $("advance-7").addEventListener("click", () => advancePlatform(7));
  $("disconnect").addEventListener("click", disconnect);
  document.querySelectorAll(".actionbar button[data-format]").forEach((b) =>
    b.addEventListener("click", () => exportCsv(b.dataset.format, b)));
  document.querySelectorAll(".legend button").forEach((b) => b.addEventListener("click", () => {
    const on = b.getAttribute("aria-pressed") !== "true";
    b.setAttribute("aria-pressed", String(on));
    for (const id of LAYER_GROUPS[b.dataset.layer]) map.setLayoutProperty(id, "visibility", on ? "visible" : "none");
  }));
  setupSignalSearch();
  renderThemeToggle();
  $("theme-toggle").addEventListener("click", toggleTheme);
  setupAbout();
  getJson("/api/meta").then((m) => {
    $("release").textContent = m.release;
    $("about-release").textContent = m.release;
  }).catch(() => {});
}

init();
try {
  [presets, samples] = await Promise.all([getJson("/api/presets"), getJson("/api/samples")]);
} catch (err) {
  showError("start-error", err.message);
}
renderStart();
await setupLayers();
const params = new URLSearchParams(location.search);
const sampleKey = params.get("sample");
const connectionId = params.get("connection");
if (connectionId && /^[A-Za-z0-9_-]{10,40}$/.test(connectionId)) {
  await restoreConnection(connectionId);
} else if (sampleKey && samples.some((s) => s.key === sampleKey)) {
  const metro = params.get("metro");
  await openSample(sampleKey);
  if (metro && /^[0-9]{5}$/.test(metro) && report) {
    if (!report.recommendations.results.slice(0, TOP_RECS).some((r) => r.metro_code === metro)) { state.showAll = true; renderReport(); }
    selectRec(metro, { scroll: false });
  }
}
