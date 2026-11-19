// Turf frontend: market selection. No build step: plain ES module + MapLibre
// (vendored, global `maplibregl`). State lives in `state` and is mirrored to the
// URL so any view can be shared.

const MAX_SEEDS = 10;    // matches MAX_SEEDS in markets.py
const MAX_SIGNALS = 20;  // matches MAX_SIGNALS in markets.py
const RESULT_LIMIT = 25;

const state = {
  preset: null,       // preset key, or "custom" once the signals are edited
  signals: [],        // category keys
  growth: false,      // population growth as a signal
  seeds: [],          // {key: metro code, display_name, ...}
  minPopulation: 250000,
  micro: false,
  selected: null,     // metro code shown in the detail panel
};
let presets = [];     // from /api/presets
let lastResult = null;
let requestSeq = 0;   // drops responses that arrive after a newer request

const $ = (id) => document.getElementById(id);
const fmt = new Intl.NumberFormat("en-US");
const compact = new Intl.NumberFormat("en-US", { notation: "compact", maximumFractionDigits: 1 });
const label = (key) => key.replaceAll("_", " ");

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
    } catch { /* not JSON */ }
    throw new ApiError(detail);
  }
  return resp;
}

const getJson = async (path) => (await api(path)).json();
const postJson = async (path, body) =>
  (await api(path, { method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify(body) })).json();

function marketRequest() {
  const edited = state.preset === "custom" || !state.preset;
  return {
    preset: edited ? null : state.preset,
    categories: edited ? state.signals : null,
    use_growth: state.growth,
    seeds: state.seeds.map((s) => s.key),
    min_population: state.minPopulation,
    include_micro: state.micro,
    limit: RESULT_LIMIT,
  };
}

const hasSignals = () => state.signals.length > 0 || state.growth;
const presetLabel = () => (presets.find((p) => p.key === state.preset) || {}).label || "Custom audience";

// ---------- Accessible combobox ----------

// search(q) resolves to items ({key, display_name, meta}).
function setupCombo(inputId, listId, search, onPick, noneText) {
  const input = $(inputId);
  const list = $(listId);
  let items = [];
  let active = -1;
  let timer = null;
  let seq = 0;

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
      li.textContent = noneText;
      list.append(li);
    }
    items.forEach((c, i) => {
      const li = document.createElement("li");
      li.id = `${listId}-${i}`;
      li.setAttribute("role", "option");
      li.setAttribute("aria-selected", String(i === active));
      const name = document.createElement("span");
      name.textContent = c.display_name;
      const meta = document.createElement("span");
      meta.className = "meta";
      meta.textContent = c.meta;
      li.append(name, meta);
      li.addEventListener("mousedown", (e) => { e.preventDefault(); pick(i); });
      list.append(li);
    });
    list.hidden = false;
    input.setAttribute("aria-expanded", "true");
    if (active >= 0) input.setAttribute("aria-activedescendant", `${listId}-${active}`);
  };

  const pick = (i) => {
    const item = items[i];
    if (!item) return;
    close();
    input.value = "";
    onPick(item);
  };

  input.addEventListener("input", () => {
    clearTimeout(timer);
    const q = input.value.trim();
    if (q.length < 2) { close(); return; }
    timer = setTimeout(async () => {
      const mine = ++seq;
      try {
        const results = await search(q);
        if (mine !== seq) return;
        items = results;
        active = items.length ? 0 : -1;
        render();
      } catch (err) {
        showError(err.message);
      }
    }, 200);
  });

  input.addEventListener("keydown", (e) => {
    if (list.hidden) return;
    if (e.key === "ArrowDown") { active = Math.min(active + 1, items.length - 1); render(); e.preventDefault(); }
    else if (e.key === "ArrowUp") { active = Math.max(active - 1, 0); render(); e.preventDefault(); }
    else if (e.key === "Enter") { if (active >= 0) pick(active); e.preventDefault(); }
    else if (e.key === "Escape") { close(); }
  });
  input.addEventListener("blur", () => setTimeout(close, 100));
}

const toMetroItem = (m) => ({
  key: m.metro_code, display_name: m.name, kind: m.kind, bounds: m.bounds,
  meta: `${m.kind} area · ${compact.format(m.population)} people`,
});
const searchMetros = async (q) =>
  (await getJson(`/api/metros/search?q=${encodeURIComponent(q)}&limit=8`))
    .map(toMetroItem).filter((m) => !state.seeds.some((s) => s.key === m.key));
const searchCategories = async (q) =>
  (await getJson(`/api/categories/search?q=${encodeURIComponent(q)}&limit=8`))
    .filter((c) => !state.signals.includes(c.key))
    .map((c) => ({ key: c.key, display_name: c.label, meta: `${fmt.format(c.n_open)} open in the US` }));

// ---------- Step 1: audience ----------

function renderPresets() {
  const box = $("presets");
  box.innerHTML = "";
  for (const p of presets) {
    const b = document.createElement("button");
    b.type = "button";
    b.className = "preset";
    b.setAttribute("role", "radio");
    b.setAttribute("aria-checked", String(state.preset === p.key));
    b.dataset.key = p.key;
    const name = document.createElement("span");
    name.className = "name";
    name.textContent = p.label;
    const ex = document.createElement("span");
    ex.className = "example";
    ex.textContent = `e.g. ${p.example}`;
    b.append(name, ex);
    b.addEventListener("click", () => pickPreset(p.key));
    box.append(b);
  }
}

function pickPreset(key) {
  const p = presets.find((x) => x.key === key);
  if (!p) return;
  state.preset = key;
  state.signals = [...p.categories];
  state.selected = null;
  update();
}

function renderSignals() {
  $("signals-block").hidden = !state.preset;
  const ul = $("signal-chips");
  ul.innerHTML = "";
  state.signals.forEach((key, i) => {
    const li = document.createElement("li");
    li.className = "chip";
    const text = document.createElement("span");
    text.textContent = label(key);
    const x = document.createElement("button");
    x.type = "button";
    x.textContent = "×";
    x.setAttribute("aria-label", `Remove ${label(key)}`);
    x.addEventListener("click", () => {
      state.signals.splice(i, 1);
      state.preset = "custom";
      update();
      $("signal-input").focus();
    });
    li.append(text, x);
    ul.append(li);
  });
  $("signal-input").disabled = state.signals.length >= MAX_SIGNALS;
  $("growth").checked = state.growth;
}

// ---------- Step 2: seeds ----------

function renderSeeds() {
  const ul = $("seed-chips");
  ul.innerHTML = "";
  state.seeds.forEach((s, i) => {
    const li = document.createElement("li");
    li.className = "chip seed";
    const text = document.createElement("span");
    text.textContent = s.display_name;
    const x = document.createElement("button");
    x.type = "button";
    x.textContent = "×";
    x.setAttribute("aria-label", `Remove ${s.display_name}`);
    x.addEventListener("click", () => {
      state.seeds.splice(i, 1);
      update({ fit: true });
      $("seed-input").focus();
    });
    li.append(text, x);
    ul.append(li);
  });
  ul.hidden = state.seeds.length === 0;
  $("seed-combo").hidden = state.seeds.length >= MAX_SEEDS;
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

const OUR_SOURCES = ["results", "seeds", "selected"];
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

// Following the system (no saved choice): track it live.
darkQuery.addEventListener("change", () => {
  if (document.documentElement.dataset.theme) return;
  renderThemeToggle();
  applyLayerTheme();
  applyMapTheme();
});

// ---------- Map ----------

const map = new maplibregl.Map({
  container: "map",
  style: MAP_STYLES[mapTheme],
  center: [-97, 38.5],
  zoom: 3.4,
  attributionControl: { compact: true },
});
map.addControl(new maplibregl.NavigationControl({ showCompass: false }), "top-right");
const mapReady = new Promise((resolve) => map.on("load", resolve));

function toFeatures(rows, extra = {}) {
  return {
    type: "FeatureCollection",
    features: rows.map((r) => ({
      type: "Feature",
      geometry: { type: "Point", coordinates: r.center },
      properties: { metro_code: r.metro_code, name: r.name, rank: r.rank, population: r.population, ...extra },
    })),
  };
}

// Circle area grows with population (radius ~ sqrt), readable from 50k to 20M people.
const sizeByPopulation = ["interpolate", ["linear"], ["sqrt", ["get", "population"]], 200, 4, 1500, 11, 4500, 22];

async function setupLayers() {
  await mapReady;
  const empty = { type: "FeatureCollection", features: [] };
  for (const id of OUR_SOURCES) map.addSource(id, { type: "geojson", data: empty });
  map.addLayer({
    id: "results-circle", type: "circle", source: "results",
    paint: {
      "circle-radius": sizeByPopulation, "circle-color": "#e4572e", "circle-opacity": 0.75,
      "circle-stroke-color": "#ffffff", "circle-stroke-width": 1.2,
    },
  });
  map.addLayer({
    id: "seeds-circle", type: "circle", source: "seeds",
    paint: {
      "circle-radius": sizeByPopulation, "circle-color": "#2e6fe4", "circle-opacity": 0.75,
      "circle-stroke-color": "#ffffff", "circle-stroke-width": 1.2,
    },
  });
  map.addLayer({
    id: "selected-ring", type: "circle", source: "selected",
    paint: {
      "circle-radius": ["+", sizeByPopulation, 4], "circle-color": "rgba(0,0,0,0)",
      "circle-stroke-color": "#1d1d1f", "circle-stroke-width": 2.5,
    },
  });
  applyLayerTheme();
  for (const layer of ["results-circle", "seeds-circle"]) {
    map.on("click", layer, (e) => selectMetro(e.features[0].properties.metro_code, { fly: false }));
    map.on("mouseenter", layer, () => { map.getCanvas().style.cursor = "pointer"; });
    map.on("mouseleave", layer, () => { map.getCanvas().style.cursor = ""; });
  }
}

const LAYER_GROUPS = { results: ["results-circle"], seeds: ["seeds-circle"] };

function setLayerVisible(group, visible) {
  for (const id of LAYER_GROUPS[group]) map.setLayoutProperty(id, "visibility", visible ? "visible" : "none");
}

function fitTo(rows) {
  if (!rows.length) return;
  const bounds = new maplibregl.LngLatBounds();
  rows.forEach((r) => bounds.extend(r.center));
  map.fitBounds(bounds, { padding: 70, maxZoom: 7, duration: 600 });
}

// ---------- Results ----------

function showError(message) {
  const el = $("error");
  el.textContent = message || "";
  el.hidden = !message;
}

const ratioText = (r) => `${r.ratio >= 10 ? Math.round(r.ratio) : r.ratio.toFixed(1)}×`;

function renderResults(result) {
  const ol = $("results");
  ol.innerHTML = "";
  for (const r of result.results) {
    const li = document.createElement("li");
    li.tabIndex = 0;
    li.dataset.code = r.metro_code;
    li.setAttribute("aria-current", String(state.selected === r.metro_code));
    const head = document.createElement("div");
    head.className = "row-head";
    const rank = document.createElement("span");
    rank.className = "rank";
    rank.textContent = r.rank;
    const name = document.createElement("span");
    name.className = "name";
    name.textContent = r.name;
    head.append(rank, name);
    if (r.match !== null) {
      const match = document.createElement("span");
      match.className = "match";
      match.textContent = `${r.match}% match`;
      head.append(match);
    }
    const meta = document.createElement("div");
    meta.className = "meta";
    meta.textContent = `${compact.format(r.population)} people · ${r.growth_pct >= 0 ? "+" : ""}${r.growth_pct.toFixed(1)}% since 2020`;
    const reasons = document.createElement("div");
    reasons.className = "reasons";
    for (const x of r.reasons.slice(0, 3)) {
      const tag = document.createElement("span");
      tag.className = x.ratio >= 1 ? "reason up" : "reason";
      tag.textContent = `${x.label} ${ratioText(x)}`;
      reasons.append(tag);
    }
    li.append(head, meta, reasons);
    const go = () => selectMetro(r.metro_code);
    li.addEventListener("click", go);
    li.addEventListener("keydown", (e) => { if (e.key === "Enter") go(); });
    ol.append(li);
  }
  $("results-title").textContent = result.mode === "lookalike"
    ? "Closest to your best markets" : "Strongest signals";
  $("results-section").hidden = result.results.length === 0;
}

function findMetro(code) {
  if (!lastResult) return null;
  return lastResult.results.find((r) => r.metro_code === code)
    || lastResult.seeds.find((r) => r.metro_code === code) || null;
}

async function selectMetro(code, { fly = true } = {}) {
  const r = findMetro(code);
  state.selected = r ? code : null;
  syncUrl();
  document.querySelectorAll("#results li").forEach((li) =>
    li.setAttribute("aria-current", String(li.dataset.code === state.selected)));
  map.getSource("selected")?.setData(toFeatures(r ? [r] : []));
  const panel = $("detail");
  if (!r) { panel.hidden = true; return; }
  const isSeed = lastResult.seeds.some((s) => s.metro_code === code);
  $("detail-rank").textContent = isSeed ? "One of your best markets"
    : lastResult.mode === "lookalike" ? `#${r.rank} · ${r.match}% match to your best markets` : `#${r.rank} by signal strength`;
  $("detail-title").textContent = r.name;
  $("detail-meta").textContent = `${r.kind === "metro" ? "Metropolitan" : "Micropolitan"} area · ${fmt.format(r.population)} people (2025) · ${r.growth_pct >= 0 ? "+" : ""}${r.growth_pct.toFixed(1)}% since 2020 · ${fmt.format(r.zip_count)} ZIP codes`;
  renderBars(r);
  panel.hidden = false;
  if (fly) map.fitBounds([[r.bounds[0], r.bounds[1]], [r.bounds[2], r.bounds[3]]], { padding: 80, maxZoom: 9, duration: 600 });
  await renderBrief(r);
}

// Each signal as a bar against the typical metro (the 1× line).
function renderBars(r) {
  const ul = $("detail-signals");
  ul.innerHTML = "";
  const maxRatio = Math.max(2, ...r.reasons.map((x) => x.ratio));
  for (const x of r.reasons) {
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
    value.textContent = ratioText(x);
    value.title = x.signal === "population_growth"
      ? `${x.value}% vs ${x.typical}% typical`
      : `${x.value}% of local businesses vs ${x.typical}% typical`;
    li.append(name, track, value);
    ul.append(li);
  }
}

async function renderBrief(r) {
  const box = $("detail-brief");
  box.innerHTML = "";
  const preset = presets.find((p) => p.key === state.preset);
  if (!preset || !preset.has_briefs) return;
  const heading = document.createElement("h3");
  heading.textContent = "Creative brief";
  box.append(heading);
  let brief = null;
  try {
    brief = await getJson(`/api/briefs/${preset.key}/${r.metro_code}`);
  } catch { /* none for this metro */ }
  if (state.selected !== r.metro_code) return;  // another metro was picked meanwhile
  if (!brief) {
    const p = document.createElement("p");
    p.className = "muted";
    p.textContent = "No brief for this metro in the demo. Briefs are pre-written for each preset's top markets, so the live site never calls an AI model; a production setup would write one on demand.";
    box.append(p);
    return;
  }
  const headline = document.createElement("p");
  headline.className = "brief-headline";
  headline.textContent = brief.headline;
  const audience = document.createElement("p");
  audience.textContent = brief.audience;
  box.append(headline, audience);
  for (const [title, items] of [["Angles", brief.angles], ["Local context", brief.local_context], ["Based on", brief.grounded_in]]) {
    const h = document.createElement("h4");
    h.textContent = title;
    const ul = document.createElement("ul");
    for (const item of items) {
      const li = document.createElement("li");
      li.textContent = item;
      ul.append(li);
    }
    box.append(h, ul);
  }
}

async function refresh({ fit = false } = {}) {
  syncUrl();
  showError("");
  const ready = hasSignals();
  $("empty-state").hidden = ready;
  document.querySelector('.legend [data-layer="seeds"]').hidden = state.seeds.length === 0;
  if (!ready) {
    lastResult = null;
    $("summary").hidden = true;
    $("export").hidden = true;
    $("results-section").hidden = true;
    $("detail").hidden = true;
    await mapReady;
    for (const id of OUR_SOURCES) map.getSource(id).setData(toFeatures([]));
    if (state.preset === "custom") showError("Add at least one signal, or use population growth.");
    return;
  }

  const mine = ++requestSeq;
  $("loading").hidden = false;
  try {
    const result = await postJson("/api/markets", marketRequest());
    if (mine !== requestSeq) return;
    lastResult = result;
    await mapReady;
    map.getSource("results").setData(toFeatures(result.results));
    map.getSource("seeds").setData(toFeatures(result.seeds));

    $("n-results").textContent = fmt.format(result.results.length);
    $("summary-unit").textContent = result.mode === "lookalike" ? "lookalike metros" : "top metros";
    const seedNames = result.seeds.map((s) => s.name.split(",")[0].split("-")[0]);
    const detail = [`${presetLabel()}, ${result.signals.length} signals`];
    detail.push(result.mode === "lookalike"
      ? `closest to ${seedNames.length <= 2 ? seedNames.join(" and ") : `${seedNames.length} best markets`}`
      : "ranked by signal strength");
    detail.push(`out of ${fmt.format(result.universe)} metros with ${compact.format(state.minPopulation)}+ people`);
    $("summary-detail").textContent = detail.join(" · ");
    $("summary").hidden = false;
    $("export").hidden = result.results.length === 0;
    renderResults(result);
    if (fit) fitTo([...result.results, ...result.seeds]);
    if (state.selected && findMetro(state.selected)) await selectMetro(state.selected, { fly: false });
    else { state.selected = null; $("detail").hidden = true; map.getSource("selected").setData(toFeatures([])); }
  } catch (err) {
    if (mine !== requestSeq) return;
    lastResult = null;
    $("summary").hidden = true;
    $("export").hidden = true;
    $("results-section").hidden = true;
    $("detail").hidden = true;
    showError(err.message);
  } finally {
    if (mine === requestSeq) $("loading").hidden = true;
  }
}

// ---------- Export ----------

async function exportCsv(format, button) {
  if (!hasSignals()) return;
  button.disabled = true;
  showError("");
  try {
    const resp = await api(`/api/markets/export?format=${format}`, {
      method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify(marketRequest()),
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
    showError(err.message);
  } finally {
    button.disabled = false;
  }
}

// ---------- URL state ----------

function syncUrl() {
  const p = new URLSearchParams();
  if (state.preset && state.preset !== "custom") p.set("preset", state.preset);
  if (state.preset === "custom") for (const s of state.signals) p.append("signal", s);
  if (state.growth) p.set("growth", "1");
  for (const s of state.seeds) p.append("seed", s.key);
  if (state.minPopulation !== 250000) p.set("minpop", String(state.minPopulation));
  if (state.micro) p.set("micro", "1");
  if (state.selected) p.set("metro", state.selected);
  const qs = p.toString();
  history.replaceState(null, "", qs ? `?${qs}` : location.pathname);
}

async function restoreFromUrl() {
  const p = new URLSearchParams(location.search);
  const preset = presets.find((x) => x.key === p.get("preset"));
  const signals = [...new Set(p.getAll("signal"))].filter((s) => /^[a-z0-9_]+$/.test(s)).slice(0, MAX_SIGNALS);
  if (signals.length) { state.preset = "custom"; state.signals = signals; }
  else if (preset) { state.preset = preset.key; state.signals = [...preset.categories]; }
  state.growth = p.get("growth") === "1";
  if (state.growth && !state.preset) state.preset = "custom";
  const minpop = Number(p.get("minpop"));
  if ([100000, 250000, 500000, 1000000].includes(minpop)) state.minPopulation = minpop;
  state.micro = p.get("micro") === "1";
  const codes = [...new Set(p.getAll("seed"))].filter((c) => /^[0-9]{5}$/.test(c)).slice(0, MAX_SEEDS);
  const seeds = await Promise.all(codes.map((c) => getJson(`/api/metros/${c}`).then(toMetroItem).catch(() => null)));
  state.seeds = seeds.filter(Boolean);
  const metro = p.get("metro");
  state.selected = metro && /^[0-9]{5}$/.test(metro) ? metro : null;
  $("min-pop").value = String(state.minPopulation);
  $("micro").checked = state.micro;
}

// ---------- Wiring ----------

function renderControls() {
  renderPresets();
  renderSignals();
  renderSeeds();
}

async function update(opts = { fit: true }) {
  renderControls();
  await refresh(opts);
}

function init() {
  setupCombo("signal-input", "signal-results", searchCategories, (c) => {
    state.signals = [...state.signals, c.key].slice(0, MAX_SIGNALS);
    state.preset = "custom";
    update({ fit: true });
  }, "No categories found. Try a shorter word.");
  setupCombo("seed-input", "seed-results", searchMetros, (m) => {
    state.seeds = [...state.seeds, m].slice(0, MAX_SEEDS);
    update({ fit: true });
  }, "No metro areas found. Try a city name.");
  $("growth").addEventListener("change", (e) => {
    state.growth = e.target.checked;
    if (!state.preset) state.preset = "custom";
    update({ fit: true });
  });
  $("min-pop").addEventListener("change", (e) => { state.minPopulation = Number(e.target.value); update({ fit: true }); });
  $("micro").addEventListener("change", (e) => { state.micro = e.target.checked; update({ fit: true }); });
  $("detail-close").addEventListener("click", () => selectMetro(null));
  document.querySelectorAll(".legend button").forEach((b) => b.addEventListener("click", () => {
    const on = b.getAttribute("aria-pressed") !== "true";
    b.setAttribute("aria-pressed", String(on));
    setLayerVisible(b.dataset.layer, on);
  }));
  document.querySelectorAll(".export button[data-format]").forEach((b) =>
    b.addEventListener("click", () => exportCsv(b.dataset.format, b)));
  renderThemeToggle();
  $("theme-toggle").addEventListener("click", toggleTheme);
  getJson("/api/meta").then((m) => { $("release").textContent = m.release; }).catch(() => {});
}

init();
try {
  presets = await getJson("/api/presets");
} catch (err) {
  showError(err.message);
}
await setupLayers();
await restoreFromUrl();
await update({ fit: true });
