// Turf frontend. No build step: plain ES module + MapLibre (vendored, global `maplibregl`).
// State lives in `state` and is mirrored to the URL so any view can be shared.

const STATES = [
  ["AL", "Alabama"], ["AK", "Alaska"], ["AZ", "Arizona"], ["AR", "Arkansas"], ["CA", "California"],
  ["CO", "Colorado"], ["CT", "Connecticut"], ["DE", "Delaware"], ["DC", "District of Columbia"],
  ["FL", "Florida"], ["GA", "Georgia"], ["HI", "Hawaii"], ["ID", "Idaho"], ["IL", "Illinois"],
  ["IN", "Indiana"], ["IA", "Iowa"], ["KS", "Kansas"], ["KY", "Kentucky"], ["LA", "Louisiana"],
  ["ME", "Maine"], ["MD", "Maryland"], ["MA", "Massachusetts"], ["MI", "Michigan"], ["MN", "Minnesota"],
  ["MS", "Mississippi"], ["MO", "Missouri"], ["MT", "Montana"], ["NE", "Nebraska"], ["NV", "Nevada"],
  ["NH", "New Hampshire"], ["NJ", "New Jersey"], ["NM", "New Mexico"], ["NY", "New York"],
  ["NC", "North Carolina"], ["ND", "North Dakota"], ["OH", "Ohio"], ["OK", "Oklahoma"], ["OR", "Oregon"],
  ["PA", "Pennsylvania"], ["RI", "Rhode Island"], ["SC", "South Carolina"], ["SD", "South Dakota"],
  ["TN", "Tennessee"], ["TX", "Texas"], ["UT", "Utah"], ["VT", "Vermont"], ["VA", "Virginia"],
  ["WA", "Washington"], ["WV", "West Virginia"], ["WI", "Wisconsin"], ["WY", "Wyoming"],
];
const METERS_PER_MILE = 1609.344;
const LIST_LIMIT = 300;  // rows shown in the sidebar list; the map and exports have all of them
const MAX_CHAINS = 10;   // matches MAX_CHAINS in targets.py
const MAX_UPLOAD = 5000; // matches MAX_UPLOADED_LOCATIONS in targets.py
const MAX_METROS = 10;   // matches MAX_METROS in targets.py
const DEFAULT_OWN_RADIUS = { only_near: 3, skip_near: 1 };  // matches targets.py

const state = {
  targets: [],        // SearchResults: up to MAX_CHAINS chains, or exactly one category
  own: null,          // SearchResult with kind "chain": the user's own chain
  uploaded: null,     // { fileName, locations: [{name, lat, lon}] } from an uploaded CSV
  ownFilter: "only_near",
  areas: [],          // states ({kind: "state", key: "TX"}) and metros ({kind: "metro", key: CBSA code, bounds}); a union
  radiusMi: 1,
  ownRadiusMi: DEFAULT_OWN_RADIUS.only_near,
  unverified: false,
};
const hasOwn = () => Boolean(state.own || state.uploaded);
const isCategory = () => state.targets.length === 1 && state.targets[0].kind === "category";
let lastResult = null;
let requestSeq = 0;   // drops responses that arrive after a newer request

const $ = (id) => document.getElementById(id);
const fmt = new Intl.NumberFormat("en-US");

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

function targetRequest() {
  return {
    chain_keys: isCategory() ? null : state.targets.map((t) => t.key),
    category: isCategory() ? state.targets[0].key : null,
    radius_mi: state.radiusMi,
    include_unverified: state.unverified,
    states: areaKeys("state"),
    metros: areaKeys("metro"),
    own_chain_key: state.own ? state.own.key : null,
    own_locations: state.uploaded ? state.uploaded.locations : null,
    own_filter: state.ownFilter,
    own_radius_mi: state.ownRadiusMi,
    campaign_name: $("campaign").value.trim() || null,
  };
}

function areaKeys(kind) {
  const keys = state.areas.filter((a) => a.kind === kind).map((a) => a.key);
  return keys.length ? keys : null;
}

// " in Texas", " in the Austin-Round Rock-San Marcos, TX area", " in Texas and Oklahoma", " in 4 areas"
function describeAreas() {
  const name = (a) => (a.kind === "metro" ? `the ${a.display_name} area` : a.display_name);
  if (!state.areas.length) return " in the US";
  if (state.areas.length <= 2) return " in " + state.areas.map(name).join(" and ");
  return ` in ${state.areas.length} areas`;
}

function targetLabel() {
  if (state.targets.length <= 2) return state.targets.map((t) => t.display_name).join(" and ");
  return `${state.targets[0].display_name} and ${state.targets.length - 1} more`;
}

function ownLabel() {
  const parts = [];
  if (state.own) parts.push(state.own.display_name);
  if (state.uploaded) parts.push(`uploaded stores`);
  return parts.join(" + ");
}

// ---------- Store list upload ----------

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

// Returns { locations, skipped } or throws an Error with a message for the user.
function parseStoreList(text) {
  const rows = parseCsv(text.replace(/^\uFEFF/, ""));
  if (!rows.length) throw new Error("That file is empty.");
  const norm = (h) => h.trim().toLowerCase().replace(/[^a-z]/g, "");
  const header = rows[0].map(norm);
  const find = (names) => header.findIndex((h) => names.includes(h));
  let lat = find(["lat", "latitude", "y"]);
  let lon = find(["lon", "lng", "long", "longitude", "x"]);
  let name = find(["name", "store", "storename", "location", "locationname", "label", "title"]);
  if (name < 0) name = find(["address", "streetaddress", "address1"]);
  let body = rows.slice(1);
  if (lat < 0 || lon < 0) {
    // No header: use the first two numeric columns as latitude, longitude.
    const numeric = rows[0].map((v, i) => (v.trim() !== "" && Number.isFinite(Number(v)) ? i : -1)).filter((i) => i >= 0);
    if (numeric.length < 2) {
      throw new Error("Couldn't find latitude and longitude columns. Name them \"latitude\" and \"longitude\" (Turf doesn't geocode addresses yet).");
    }
    [lat, lon] = numeric;
    name = rows[0].findIndex((v, i) => i !== lat && i !== lon && v.trim() && !Number.isFinite(Number(v)));
    body = rows;
  }
  let locations = [];
  let skipped = 0;
  for (const r of body) {
    const la = Number(r[lat]), lo = Number(r[lon]);
    if (r[lat]?.trim() && r[lon]?.trim() && Math.abs(la) <= 90 && Math.abs(lo) <= 180) {
      const label = name >= 0 ? (r[name] || "").trim().slice(0, 200) : "";
      locations.push({ name: label || null, lat: la, lon: lo });
    } else skipped++;
  }
  if (!locations.length) throw new Error("No rows had a valid latitude and longitude.");
  // US stores have positive latitude and negative longitude; swapped columns are a common mistake.
  if (locations.every((l) => l.lat < 0 && l.lon > 0 && l.lon <= 90)) {
    locations = locations.map((l) => ({ ...l, lat: l.lon, lon: l.lat }));
  }
  if (locations.length > MAX_UPLOAD) {
    throw new Error(`That's ${fmt.format(locations.length)} locations; the limit is ${fmt.format(MAX_UPLOAD)}.`);
  }
  return { locations, skipped };
}

function showUploadError(message) {
  const el = $("upload-error");
  el.textContent = message || "";
  el.hidden = !message;
}

async function onUpload(file) {
  showUploadError("");
  if (!file) return;
  if (file.size > 5 * 1024 * 1024) { showUploadError("That file is over 5 MB. Keep just the name, latitude and longitude columns."); return; }
  try {
    const { locations, skipped } = parseStoreList(await file.text());
    state.uploaded = { fileName: file.name, locations, skipped };
    update({ fit: false });
  } catch (err) {
    showUploadError(err.message);
  }
}

function downloadTemplate() {
  const csv = "name,latitude,longitude\nDowntown store,30.2672,-97.7431\nNorth store,30.4021,-97.7253\n";
  const a = document.createElement("a");
  a.href = URL.createObjectURL(new Blob([csv], { type: "text/csv" }));
  a.download = "turf-store-list-example.csv";
  document.body.append(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(a.href), 1000);
}

// ---------- Chain search (accessible combobox) ----------

// search(q) resolves to items ({kind, key, display_name, detail, n_open, ...}).
// opts.minChars: characters before searching (default 2). opts.emptyItems(): suggestions
// shown when the box is focused and empty, so it also works like a dropdown.
function setupCombo(inputId, listId, search, onPick, noneText, opts = {}) {
  const minChars = opts.minChars ?? 2;
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
      li.textContent = typeof noneText === "function" ? noneText() : noneText;
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
      meta.textContent = optionMeta(c);
      li.append(name, meta);
      li.addEventListener("mousedown", (e) => { e.preventDefault(); pick(i); });
      list.append(li);
    });
    list.hidden = false;
    input.setAttribute("aria-expanded", "true");
    if (active >= 0) input.setAttribute("aria-activedescendant", `${listId}-${active}`);
  };

  const pick = (i) => {
    const chain = items[i];
    if (!chain) return;
    close();
    input.value = "";
    onPick(chain);
  };

  const showEmpty = () => {
    if (!opts.emptyItems || input.value.trim()) return;
    seq++;  // drop any search still in flight
    items = opts.emptyItems();
    active = -1;
    render();
  };
  input.addEventListener("focus", showEmpty);
  input.addEventListener("click", () => { if (list.hidden) showEmpty(); });

  input.addEventListener("input", () => {
    clearTimeout(timer);
    const q = input.value.trim();
    if (!q && opts.emptyItems) { showEmpty(); return; }
    if (q.length < minChars) { close(); return; }
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

function renderSelected(boxId, chain, onClear) {
  const box = $(boxId);
  box.innerHTML = "";
  if (!chain) { box.hidden = true; return; }
  renderSelectedInto(box, chain, onClear);
  box.hidden = false;
}

function renderSelectedInto(box, chain, onClear) {
  const text = document.createElement("div");
  const name = document.createElement("div");
  name.className = "name";
  name.textContent = chain.display_name;
  const meta = document.createElement("div");
  meta.className = "meta";
  const unverified = chain.n_total - chain.n_open;
  const kindLabel = chain.kind === "category" ? "Every business in this category · " : "";
  meta.textContent = chain.kind === "metro"
    ? `Census ${chain.detail} area · ${fmt.format(chain.n_total)} places`
    : kindLabel + `${fmt.format(chain.n_open)} open` +
      (unverified ? ` · ${fmt.format(unverified)} unverified` : "") +
      (chain.kind === "chain" && chain.detail ? ` · ${chain.detail.replaceAll("_", " ")}` : "");
  text.append(name, meta);
  const clear = document.createElement("button");
  clear.type = "button";
  clear.setAttribute("aria-label", `Clear ${chain.display_name}`);
  clear.textContent = "×";
  clear.addEventListener("click", onClear);
  box.append(text, clear);
}

function renderUploaded() {
  const box = $("upload-selected");
  box.innerHTML = "";
  if (!state.uploaded) { box.hidden = true; return; }
  const u = state.uploaded;
  const text = document.createElement("div");
  const name = document.createElement("div");
  name.className = "name";
  name.textContent = u.fileName;
  const meta = document.createElement("div");
  meta.className = "meta";
  meta.textContent = `${fmt.format(u.locations.length)} of your locations` +
    (u.skipped ? ` · ${fmt.format(u.skipped)} rows without coordinates ignored` : "") +
    " · not saved in shared links";
  text.append(name, meta);
  const clear = document.createElement("button");
  clear.type = "button";
  clear.setAttribute("aria-label", `Remove ${u.fileName}`);
  clear.textContent = "×";
  clear.addEventListener("click", () => { state.uploaded = null; $("own-file").value = ""; update(); });
  box.append(text, clear);
  box.hidden = false;
}

function optionMeta(c) {
  if (c.kind === "category") return `every business in category · ${fmt.format(c.n_open)} open`;
  if (c.kind === "metro") return `${c.detail} area · ${fmt.format(c.n_total)} places`;
  if (c.kind === "state") return "state";
  // Chains show their main category, so similar names ("Orangetheory" the gym
  // vs. a store) can be told apart.
  return [c.detail && c.detail.replaceAll("_", " "), `${fmt.format(c.n_open)} open`].filter(Boolean).join(" · ");
}

const searchAll = (kind) => (q) =>
  getJson(`/api/search?q=${encodeURIComponent(q)}&limit=8` + (kind ? `&kind=${kind}` : ""));

const toMetroItem = (m) => ({
  kind: "metro", key: m.metro_code, display_name: m.name, detail: m.kind,
  n_open: m.n_open, n_total: m.n_total, bounds: m.bounds,
});
const searchMetros = async (q) =>
  (await getJson(`/api/metros/search?q=${encodeURIComponent(q)}&limit=8`)).map(toMetroItem);

const stateItem = ([code, name]) => ({ kind: "state", key: code, display_name: name });
const chosenArea = (item) => state.areas.some((a) => a.kind === item.kind && a.key === item.key);

// States match on their code ("tx") or the start of any word ("carolina"); they rank
// before metros, so "Texas" comes before "Texarkana". Metros come from the API.
async function searchAreas(q) {
  const norm = q.toLowerCase().trim();
  const states = STATES.filter(([code, name]) =>
    code.toLowerCase() === norm || name.toLowerCase().split(" ").some((w, i, ws) => ws.slice(i).join(" ").startsWith(norm)))
    .map(stateItem);
  const metros = norm.length >= 2 ? await searchMetros(q) : [];
  return [...states.slice(0, 5), ...metros].filter((a) => !chosenArea(a)).slice(0, 10);
}

function addArea(item) {
  showError("");
  if (chosenArea(item)) return;
  if (item.kind === "metro" && state.areas.filter((a) => a.kind === "metro").length >= MAX_METROS) {
    showError(`That's the limit of ${MAX_METROS} metro areas in one list.`);
    return;
  }
  state.areas = [...state.areas, item];
  update({ fit: true });
  if (!state.targets.length && item.bounds) map.fitBounds(item.bounds, { padding: 40, duration: 600 });
}

function renderAreas() {
  const ul = $("area-selected");
  ul.innerHTML = "";
  state.areas.forEach((a, i) => {
    const li = document.createElement("li");
    li.className = "chip";
    const label = document.createElement("span");
    label.textContent = a.kind === "metro" ? `${a.display_name} ${a.detail} area` : a.display_name;
    const clear = document.createElement("button");
    clear.type = "button";
    clear.setAttribute("aria-label", `Remove ${a.display_name}`);
    clear.textContent = "×";
    clear.addEventListener("click", () => {
      state.areas.splice(i, 1);
      update({ fit: true });
      $("area-input").focus();
    });
    li.append(label, clear);
    ul.append(li);
  });
  ul.hidden = state.areas.length === 0;
  $("area-input").placeholder = state.areas.length ? "Add another state or metro area" : "All of the US. Type a state or metro area";
}

function renderTargets() {
  const ul = $("target-selected");
  ul.innerHTML = "";
  state.targets.forEach((t, i) => {
    const li = document.createElement("li");
    li.className = "selected";
    renderSelectedInto(li, t, () => {
      state.targets.splice(i, 1);
      update({ fit: true });
      $("target-input").focus();
    });
    ul.append(li);
  });
  ul.hidden = state.targets.length === 0;
  const combo = $("target-combo");
  const input = $("target-input");
  const hint = $("target-hint");
  combo.hidden = isCategory() || state.targets.length >= MAX_CHAINS;
  if (state.targets.length === 0) {
    input.placeholder = "A competitor chain or a category";
    hint.hidden = true;
  } else if (isCategory()) {
    hint.textContent = "Remove the category to pick chains instead.";
    hint.hidden = false;
  } else if (state.targets.length >= MAX_CHAINS) {
    hint.textContent = `That's the limit of ${MAX_CHAINS} chains in one list.`;
    hint.hidden = false;
  } else {
    input.placeholder = "Add another competitor chain";
    hint.hidden = true;
  }
}

function addTarget(result) {
  showError("");
  if (result.kind === "category") {
    state.targets = [result];  // a category stands alone: it already covers every chain in it
  } else {
    if (state.own && state.own.key === result.key) { showError(`${result.display_name} is set as your own chain.`); return; }
    if (state.targets.some((t) => t.key === result.key)) return;
    state.targets = state.targets.filter((t) => t.kind === "chain").concat(result).slice(0, MAX_CHAINS);
  }
  update({ fit: true });
}

// ---------- Map ----------

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
  const accent = cssVar("--accent"), own = cssVar("--own"), panel = cssVar("--panel"), skip = cssVar("--skip");
  return {
    "own-radius": { "circle-color": own, "circle-stroke-color": own },
    "target-radius": { "circle-color": accent, "circle-stroke-color": accent },
    "dropped-point": { "circle-color": panel, "circle-stroke-color": skip },
    "own-point": { "circle-color": own, "circle-stroke-color": panel },
    "target-point": { "circle-color": accent, "circle-stroke-color": panel },
  };
}

function applyLayerTheme() {
  for (const [layer, paint] of Object.entries(themeLayerPaint())) {
    if (!map.getLayer(layer)) continue;
    for (const [prop, value] of Object.entries(paint)) map.setPaintProperty(layer, prop, value);
  }
}

const OUR_SOURCES = ["targets", "dropped", "own"];
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
  const dark = currentTheme() === "dark";
  const button = $("theme-toggle");
  button.setAttribute("aria-pressed", String(dark));
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

const map = new maplibregl.Map({
  container: "map",
  style: MAP_STYLES[mapTheme],
  center: [-97, 38.5],
  zoom: 3.4,
  attributionControl: { compact: true },
});
map.addControl(new maplibregl.NavigationControl({ showCompass: false }), "top-right");
const mapReady = new Promise((resolve) => map.on("load", resolve));

// Circle radius in pixels for a real-world radius in meters at the feature's latitude.
// Web Mercator: meters per pixel = 156543.03 * cos(lat) / 2^zoom (512px tiles halve it).
function metersToPixels(metersExpr) {
  const perPixelAtZ0 = ["*", 78271.517, ["cos", ["*", ["get", "lat"], Math.PI / 180]]];
  return ["interpolate", ["exponential", 2], ["zoom"],
    0, ["/", metersExpr, perPixelAtZ0],
    22, ["/", ["*", metersExpr, 2 ** 22], perPixelAtZ0]];
}

function toFeatures(rows, extra = {}) {
  return {
    type: "FeatureCollection",
    features: rows.map((r) => ({
      type: "Feature",
      geometry: { type: "Point", coordinates: [r.lon, r.lat] },
      properties: { ...r, ...extra },
    })),
  };
}

async function setupLayers() {
  await mapReady;
  const empty = { type: "FeatureCollection", features: [] };
  for (const id of ["targets", "dropped", "own"]) map.addSource(id, { type: "geojson", data: empty });

  map.addLayer({
    id: "own-radius", type: "circle", source: "own",
    paint: {
      "circle-radius": metersToPixels(["get", "radius_m"]),
      "circle-color": "#2e6fe4", "circle-opacity": 0.08,
      "circle-stroke-color": "#2e6fe4", "circle-stroke-width": 1, "circle-stroke-opacity": 0.5,
      "circle-pitch-alignment": "map",
    },
  });
  map.addLayer({
    id: "target-radius", type: "circle", source: "targets",
    paint: {
      "circle-radius": metersToPixels(["get", "radius_m"]),
      "circle-color": "#e4572e", "circle-opacity": 0.14,
      "circle-stroke-color": "#e4572e", "circle-stroke-width": 1, "circle-stroke-opacity": 0.6,
      "circle-pitch-alignment": "map",
    },
  });
  map.addLayer({
    id: "dropped-point", type: "circle", source: "dropped",
    paint: {
      "circle-radius": ["interpolate", ["linear"], ["zoom"], 3, 2, 10, 5],
      "circle-color": "#ffffff", "circle-stroke-color": "#9aa0a6", "circle-stroke-width": 1.5,
    },
  });
  map.addLayer({
    id: "own-point", type: "circle", source: "own",
    paint: {
      "circle-radius": ["interpolate", ["linear"], ["zoom"], 3, 1.5, 10, 4],
      "circle-color": "#2e6fe4", "circle-opacity": 0.65, "circle-stroke-color": "#ffffff", "circle-stroke-width": 0.5,
    },
  });
  map.addLayer({
    id: "target-point", type: "circle", source: "targets",
    paint: {
      "circle-radius": ["interpolate", ["linear"], ["zoom"], 3, 2, 10, 5],
      "circle-color": "#e4572e", "circle-stroke-color": "#ffffff", "circle-stroke-width": 1,
    },
  });

  for (const layer of ["target-point", "dropped-point", "own-point"]) {
    map.on("click", layer, (e) => showPopup(e.features[0]));
    map.on("mouseenter", layer, () => { map.getCanvas().style.cursor = "pointer"; });
    map.on("mouseleave", layer, () => { map.getCanvas().style.cursor = ""; });
  }
  applyLayerTheme();
  map.on("zoomend", updateZoomHint);
  map.on("moveend", updateZoomHint);
}

// Map layers per legend toggle.
const LAYER_GROUPS = {
  targets: ["target-radius", "target-point"],
  dropped: ["dropped-point"],
  own: ["own-radius", "own-point"],
};

function setLayerVisible(group, visible) {
  for (const id of LAYER_GROUPS[group]) map.setLayoutProperty(id, "visibility", visible ? "visible" : "none");
}

// A 1 mi circle is a few pixels at state zoom; say so instead of leaving it invisible.
function updateZoomHint() {
  const hint = $("zoom-hint");
  if (!lastResult || !lastResult.targets.length) { hint.hidden = true; return; }
  const metersPerPixel = 78271.517 * Math.cos(map.getCenter().lat * Math.PI / 180) / 2 ** map.getZoom();
  const px = (state.radiusMi * METERS_PER_MILE) / metersPerPixel;
  hint.hidden = px >= 4;
}

function showPopup(feature) {
  const p = feature.properties;
  const root = document.createElement("div");
  const title = document.createElement("strong");
  title.textContent = p.name;
  const addr = document.createElement("div");
  addr.textContent = [p.address, p.locality, p.state].filter((v) => v && v !== "null").join(", ");
  const meta = document.createElement("div");
  meta.className = "popup-meta";
  meta.textContent = describe(p);
  root.append(title, addr, meta);
  new maplibregl.Popup({ offset: 8 }).setLngLat(feature.geometry.coordinates).setDOMContent(root).addTo(map);
}

const has = (v) => v !== null && v !== undefined && v !== "null";

function describe(p) {
  if (p.kind === "own") return "Your location";
  if (p.kind === "dropped") {
    return p.reason === "near_yours"
      ? `Skipped: ${p.distance_mi} mi from your ${p.nearest_own_location}`
      : `Skipped: none of your locations within ${state.ownRadiusMi} mi`;
  }
  const status = p.status === "open" ? "Open (recent activity)" : "Unverified (no recent activity)";
  return has(p.distance_mi) ? `${status} · ${p.distance_mi} mi from your ${p.nearest_own_location}` : status;
}

function fitTo(rows) {
  if (!rows.length) return;
  const bounds = new maplibregl.LngLatBounds();
  rows.forEach((r) => bounds.extend([r.lon, r.lat]));
  map.fitBounds(bounds, { padding: 60, maxZoom: 12, duration: 600 });
}

// ---------- Results ----------

function showError(message) {
  const el = $("error");
  el.textContent = message || "";
  el.hidden = !message;
}

function renderList(olId, moreId, rows, kind) {
  const ol = $(olId);
  ol.innerHTML = "";
  rows.slice(0, LIST_LIMIT).forEach((t) => {
    const li = document.createElement("li");
    li.tabIndex = 0;
    const name = document.createElement("div");
    name.textContent = t.name;
    if (kind === "target" && t.status !== "open") {
      const tag = document.createElement("span");
      tag.className = "tag";
      tag.textContent = "unverified";
      name.append(tag);
    }
    const addr = document.createElement("div");
    addr.className = "addr";
    addr.textContent = [t.address, t.locality, t.state].filter(Boolean).join(", ");
    li.append(name, addr);
    if (kind === "dropped" || has(t.distance_mi)) {
      const why = document.createElement("div");
      why.className = "addr";
      why.textContent = describe({ ...t, kind }).replace(/^Open \(recent activity\) · |^Unverified \(no recent activity\) · /, "");
      li.append(why);
    }
    const go = () => {
      map.flyTo({ center: [t.lon, t.lat], zoom: 12 });
      showPopup({ geometry: { coordinates: [t.lon, t.lat] }, properties: { ...t, kind } });
    };
    li.addEventListener("click", go);
    li.addEventListener("keydown", (e) => { if (e.key === "Enter") go(); });
    ol.append(li);
  });
  const more = $(moreId);
  more.hidden = rows.length <= LIST_LIMIT;
  more.textContent = kind === "target"
    ? `Showing ${LIST_LIMIT} of ${fmt.format(rows.length)}. All of them are on the map and in the export.`
    : `Showing ${LIST_LIMIT} of ${fmt.format(rows.length)}. All of them are on the map.`;
}

function renderLists(result) {
  renderList("target-list", "list-more", result.targets, "target");
  renderList("dropped-list", "dropped-more", result.dropped, "dropped");
  $("dropped-title").textContent = `${fmt.format(result.dropped.length)} skipped` +
    (state.ownFilter === "only_near" ? ` (not within ${state.ownRadiusMi} mi of yours)` : ` (within ${state.ownRadiusMi} mi of yours)`);
  $("dropped-section").hidden = result.dropped.length === 0;
  $("list-section").hidden = result.targets.length === 0 && result.dropped.length === 0;
}

function renderLegend() {
  const own = hasOwn();
  document.querySelector('.legend [data-layer="dropped"]').hidden = !own;
  document.querySelector('.legend [data-layer="own"]').hidden = !own;
  $("legend-dropped").textContent = state.ownFilter === "only_near" ? "Skipped (not near yours)" : "Skipped (near yours)";
}

async function refresh({ fit = false } = {}) {
  syncUrl();
  showError("");
  renderLegend();
  const hasTarget = state.targets.length > 0;
  $("empty-state").hidden = hasTarget;
  $("examples").hidden = hasTarget;
  if (!hasTarget) {
    lastResult = null;
    $("summary").hidden = true;
    $("export").hidden = true;
    $("list-section").hidden = true;
    await mapReady;
    for (const id of ["targets", "dropped"]) map.getSource(id).setData(toFeatures([]));
    await drawOwnOnly();
    updateZoomHint();
    return;
  }

  const mine = ++requestSeq;
  $("loading").hidden = false;
  try {
    const [result, ownRows] = await Promise.all([postJson("/api/targets", targetRequest()), ownLocations()]);
    if (mine !== requestSeq) return;
    lastResult = result;

    await mapReady;
    const radiusM = state.radiusMi * METERS_PER_MILE;
    map.getSource("targets").setData(toFeatures(result.targets, { kind: "target", radius_m: radiusM }));
    map.getSource("dropped").setData(toFeatures(result.dropped, { kind: "dropped" }));
    map.getSource("own").setData(toFeatures(ownRows, { kind: "own", radius_m: state.ownRadiusMi * METERS_PER_MILE }));

    $("n-targets").textContent = fmt.format(result.targets.length);
    const where = describeAreas();
    const parts = [`${targetLabel()}${where}, ${state.radiusMi} mi radius`];
    if (hasOwn()) {
      parts.push(state.ownFilter === "only_near"
        ? `only within ${state.ownRadiusMi} mi of your ${ownLabel()} (${fmt.format(result.dropped_count)} farther skipped)`
        : `${fmt.format(result.dropped_count)} skipped within ${state.ownRadiusMi} mi of your ${ownLabel()}`);
    }
    if (!state.unverified) parts.push("confirmed-open only");
    $("summary-detail").textContent = parts.join(" · ");
    $("summary").hidden = false;
    $("export").hidden = result.targets.length === 0;
    if (!$("campaign").value) $("campaign").placeholder = `Turf - ${defaultCampaignKey()}`;
    if (result.targets.length === 0) {
      showError(!result.candidates
        ? "No locations match. Try a wider area or include unverified locations."
        : state.ownFilter === "only_near"
          ? `None are within ${state.ownRadiusMi} mi of your locations. Widen the distance, or switch to "Skip near mine".`
          : "Every location is near one of yours. Lower the skip distance to keep some.");
    }
    renderLists(result);
    if (fit) fitTo(result.targets.length ? result.targets : ownRows);
    updateZoomHint();
  } catch (err) {
    if (mine !== requestSeq) return;
    // Don't leave the previous results next to the error.
    lastResult = null;
    $("summary").hidden = true;
    $("export").hidden = true;
    $("list-section").hidden = true;
    for (const id of ["targets", "dropped"]) map.getSource(id).setData(toFeatures([]));
    updateZoomHint();
    showError(err.message);
  } finally {
    if (mine === requestSeq) $("loading").hidden = true;
  }
}

// Same rule as TargetRequest.target_key, for the campaign placeholder.
function defaultCampaignKey() {
  const keys = state.targets.map((t) => t.key);
  return keys.length === 1 ? keys[0] : `${keys[0]} and ${keys.length - 1} more`;
}

// Your locations for the map: your chain's (from the API, unverified included,
// same as the backend uses) plus any uploaded ones.
async function ownLocations() {
  const rows = [];
  if (state.own) {
    const res = await getJson(`/api/chains/${encodeURIComponent(state.own.key)}/locations?include_unverified=true` +
      ownAreaParam());
    rows.push(...res.locations);
  }
  if (state.uploaded) {
    rows.push(...state.uploaded.locations.map((l, i) => ({
      id: `upload-${i}`, name: l.name || "Your location", address: null, locality: null, state: null, lat: l.lat, lon: l.lon,
    })));
  }
  return rows;
}

// The locations endpoint takes one area; with several, fetch all of yours (the map
// shows them all anyway, and the backend counts them regardless of area).
function ownAreaParam() {
  if (state.areas.length !== 1) return "";
  const [a] = state.areas;
  return a.kind === "state" ? `&state=${a.key}` : `&metro=${a.key}`;
}

// With no competitor picked yet, still show your locations once you add them.
async function drawOwnOnly() {
  try {
    const rows = await ownLocations();
    map.getSource("own").setData(toFeatures(rows, { kind: "own", radius_m: state.ownRadiusMi * METERS_PER_MILE }));
    if (rows.length && state.uploaded) fitTo(rows);
  } catch (err) {
    showError(err.message);
  }
}

// ---------- Export ----------

async function exportCsv(format, button) {
  if (!state.targets.length) return;
  button.disabled = true;
  showError("");
  try {
    const resp = await api(`/api/targets/export?format=${format}`, {
      method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify(targetRequest()),
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
  for (const t of state.targets) p.append(t.kind === "category" ? "cat" : "chain", t.key);
  if (state.own) {
    p.set("own", state.own.key);
    // Always written with `own`, so old links (which meant "skip near") are recognisable.
    p.set("mode", state.ownFilter === "only_near" ? "near" : "skip");
    if (state.ownRadiusMi !== DEFAULT_OWN_RADIUS[state.ownFilter]) p.set("d", String(state.ownRadiusMi));
  }
  for (const a of state.areas) p.append(a.kind, a.key);
  if (state.radiusMi !== 1) p.set("r", String(state.radiusMi));
  if (state.unverified) p.set("unverified", "1");
  const qs = p.toString();
  history.replaceState(null, "", qs ? `?${qs}` : location.pathname);
}

async function lookup(kind, key) {
  const results = await getJson(`/api/search?q=${encodeURIComponent(key)}&kind=${kind}&limit=10`);
  return results.find((r) => r.key === key) || null;
}

async function restoreFromUrl() {
  const p = new URLSearchParams(location.search);
  const clamp = (v, lo, hi, d) => (Number.isFinite(v) ? Math.min(hi, Math.max(lo, v)) : d);
  state.radiusMi = clamp(parseFloat(p.get("r")), 1, 50, 1);
  // Links from before "only near" existed have `own` without `mode` and meant skip-near,
  // with the distance in `skip`.
  state.ownFilter = p.get("own") && p.get("mode") !== "near" ? "skip_near" : "only_near";
  state.ownRadiusMi = clamp(parseFloat(p.get("d") ?? p.get("skip")), 0.5, 25, DEFAULT_OWN_RADIUS[state.ownFilter]);
  state.unverified = p.get("unverified") === "1";
  try {
    if (p.get("cat")) {
      const c = await lookup("category", p.get("cat"));
      state.targets = c ? [c] : [];
    } else {
      const keys = [...new Set(p.getAll("chain"))].slice(0, MAX_CHAINS);
      state.targets = (await Promise.all(keys.map((k) => lookup("chain", k)))).filter(Boolean);
    }
    if (p.get("own")) state.own = await lookup("chain", p.get("own"));
    const states = [...new Set(p.getAll("state").map((v) => v.toUpperCase()))]
      .map((c) => STATES.find(([code]) => code === c)).filter(Boolean).map(stateItem);
    const codes = [...new Set(p.getAll("metro"))].filter((c) => /^[0-9]{5}$/.test(c)).slice(0, MAX_METROS);
    const metros = await Promise.all(codes.map((c) => getJson(`/api/metros/${c}`).then(toMetroItem).catch(() => null)));
    state.areas = [...states, ...metros.filter(Boolean)];
  } catch (err) {
    showError(err.message);
  }
  $("radius").value = state.radiusMi;
  $("own-radius").value = state.ownRadiusMi;
  $("unverified").checked = state.unverified;
  document.querySelector(`input[name="own-filter"][value="${state.ownFilter}"]`).checked = true;
  updateOutputs();
}

// ---------- Wiring ----------

function updateOutputs() {
  $("radius-out").textContent = `${state.radiusMi} mi`;
  $("own-radius-out").textContent = `${state.ownRadiusMi} mi`;
  $("own-radius-label").textContent = state.ownFilter === "only_near" ? "Keep targets within" : "Skip targets within";
  $("own-radius").value = state.ownRadiusMi;
  $("own-radius-field").hidden = !hasOwn();
  $("own-mode").hidden = !hasOwn();
  renderTargets();
  renderSelected("own-selected", state.own, () => { state.own = null; update(); });
  renderAreas();
  renderUploaded();
}

async function update(opts) {
  updateOutputs();
  await refresh(opts);
}

function debounce(fn, ms) {
  let t;
  return (...args) => { clearTimeout(t); t = setTimeout(() => fn(...args), ms); };
}

function init() {

  // Once a chain is picked, the search only offers more chains.
  setupCombo("target-input", "target-results", (q) => searchAll(state.targets.length ? "chain" : null)(q), addTarget,
    () => (state.targets.length ? "No chains found. Try a shorter name." : "Nothing found. Try a shorter name."));
  setupCombo("area-input", "area-results", searchAreas, addArea, "No states or metro areas found. Try a city name.",
    { minChars: 1, emptyItems: () => STATES.map(stateItem).filter((a) => !chosenArea(a)) });
  setupCombo("own-input", "own-results", searchAll("chain"), (c) => {
    if (state.targets.some((t) => t.key === c.key)) { showError(`${c.display_name} is already a competitor in step 1.`); return; }
    state.own = c;
    update();
  }, "No chains found. Try a shorter name.");
  $("own-file").addEventListener("change", (e) => onUpload(e.target.files[0]));
  $("own-template").addEventListener("click", downloadTemplate);
  document.querySelectorAll('input[name="own-filter"]').forEach((r) => r.addEventListener("change", (e) => {
    // Each mode has its own sensible distance: a few miles to switch, a mile to skip.
    if (state.ownRadiusMi === DEFAULT_OWN_RADIUS[state.ownFilter]) state.ownRadiusMi = DEFAULT_OWN_RADIUS[e.target.value];
    state.ownFilter = e.target.value;
    update();
  }));
  document.querySelectorAll(".legend button").forEach((b) => b.addEventListener("click", () => {
    const on = b.getAttribute("aria-pressed") !== "true";
    b.setAttribute("aria-pressed", String(on));
    setLayerVisible(b.dataset.layer, on);
  }));

  document.querySelectorAll("#examples button").forEach((b) => b.addEventListener("click", async () => {
    try {
      const c = await lookup(b.dataset.kind || "chain", b.dataset.key);
      if (c) addTarget(c);
    } catch (err) { showError(err.message); }
  }));

  const slow = debounce(() => refresh(), 250);
  $("radius").addEventListener("input", (e) => { state.radiusMi = Number(e.target.value); updateOutputs(); slow(); });
  $("own-radius").addEventListener("input", (e) => { state.ownRadiusMi = Number(e.target.value); updateOutputs(); slow(); });
  $("unverified").addEventListener("change", (e) => { state.unverified = e.target.checked; update(); });
  document.querySelectorAll(".export button[data-format]").forEach((b) =>
    b.addEventListener("click", () => exportCsv(b.dataset.format, b)));

  renderThemeToggle();
  $("theme-toggle").addEventListener("click", toggleTheme);

  getJson("/api/meta").then((m) => { $("release").textContent = m.release; }).catch(() => {});
}

init();
await setupLayers();
await restoreFromUrl();
await update({ fit: true });
