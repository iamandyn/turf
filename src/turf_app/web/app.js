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

const state = {
  target: null,       // ChainSummary
  own: null,          // ChainSummary
  usState: "",
  radiusMi: 1,
  ownRadiusMi: 1,
  unverified: false,
};
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
    chain_key: state.target.chain_key,
    radius_mi: state.radiusMi,
    include_unverified: state.unverified,
    states: state.usState ? [state.usState] : null,
    exclude_chain_key: state.own ? state.own.chain_key : null,
    exclude_radius_mi: state.ownRadiusMi,
    campaign_name: $("campaign").value.trim() || null,
  };
}

// ---------- Chain search (accessible combobox) ----------

function setupCombo(inputId, listId, onPick) {
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
      li.textContent = "No chains found. Try a shorter name.";
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
      meta.textContent = `${fmt.format(c.n_open)} open`;
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

  input.addEventListener("input", () => {
    clearTimeout(timer);
    const q = input.value.trim();
    if (q.length < 2) { close(); return; }
    timer = setTimeout(async () => {
      const mine = ++seq;
      try {
        const results = await getJson(`/api/chains/search?q=${encodeURIComponent(q)}&limit=8`);
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
    else if (e.key === "Enter") { pick(active); e.preventDefault(); }
    else if (e.key === "Escape") { close(); }
  });
  input.addEventListener("blur", () => setTimeout(close, 100));
}

function renderSelected(boxId, chain, onClear) {
  const box = $(boxId);
  box.innerHTML = "";
  if (!chain) { box.hidden = true; return; }
  const text = document.createElement("div");
  const name = document.createElement("div");
  name.className = "name";
  name.textContent = chain.display_name;
  const meta = document.createElement("div");
  meta.className = "meta";
  const unverified = chain.n_total - chain.n_open;
  meta.textContent = `${fmt.format(chain.n_open)} open locations` +
    (unverified ? ` · ${fmt.format(unverified)} unverified` : "") +
    (chain.category ? ` · ${chain.category.replaceAll("_", " ")}` : "");
  text.append(name, meta);
  const clear = document.createElement("button");
  clear.type = "button";
  clear.setAttribute("aria-label", `Clear ${chain.display_name}`);
  clear.textContent = "×";
  clear.addEventListener("click", onClear);
  box.append(text, clear);
  box.hidden = false;
}

// ---------- Map ----------

const map = new maplibregl.Map({
  container: "map",
  style: "https://tiles.openfreemap.org/styles/positron",
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
  for (const id of ["targets", "excluded", "own"]) map.addSource(id, { type: "geojson", data: empty });

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
    id: "excluded-point", type: "circle", source: "excluded",
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

  for (const layer of ["target-point", "excluded-point", "own-point"]) {
    map.on("click", layer, (e) => showPopup(e.features[0]));
    map.on("mouseenter", layer, () => { map.getCanvas().style.cursor = "pointer"; });
    map.on("mouseleave", layer, () => { map.getCanvas().style.cursor = ""; });
  }
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
  if (p.kind === "excluded") meta.textContent = `Skipped: ${p.distance_mi} mi from your ${p.nearest_own_location}`;
  else if (p.kind === "own") meta.textContent = "Your location";
  else meta.textContent = p.status === "open" ? "Open (recent activity)" : "Unverified (no recent activity)";
  root.append(title, addr, meta);
  new maplibregl.Popup({ offset: 8 }).setLngLat(feature.geometry.coordinates).setDOMContent(root).addTo(map);
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

function renderList(targets) {
  const ol = $("target-list");
  ol.innerHTML = "";
  targets.slice(0, LIST_LIMIT).forEach((t) => {
    const li = document.createElement("li");
    li.tabIndex = 0;
    const name = document.createElement("div");
    name.textContent = t.name;
    if (t.status !== "open") {
      const tag = document.createElement("span");
      tag.className = "tag";
      tag.textContent = "unverified";
      name.append(tag);
    }
    const addr = document.createElement("div");
    addr.className = "addr";
    addr.textContent = [t.address, t.locality, t.state].filter(Boolean).join(", ");
    li.append(name, addr);
    const go = () => {
      map.flyTo({ center: [t.lon, t.lat], zoom: 12 });
      showPopup({ geometry: { coordinates: [t.lon, t.lat] }, properties: { ...t, kind: "target" } });
    };
    li.addEventListener("click", go);
    li.addEventListener("keydown", (e) => { if (e.key === "Enter") go(); });
    ol.append(li);
  });
  const more = $("list-more");
  more.hidden = targets.length <= LIST_LIMIT;
  more.textContent = `Showing ${LIST_LIMIT} of ${fmt.format(targets.length)}. All of them are on the map and in the export.`;
  $("list-section").hidden = targets.length === 0;
}

async function refresh({ fit = false } = {}) {
  syncUrl();
  showError("");
  const hasTarget = Boolean(state.target);
  $("empty-state").hidden = hasTarget;
  $("examples").hidden = hasTarget;
  if (!hasTarget) {
    $("summary").hidden = true;
    $("export").hidden = true;
    $("list-section").hidden = true;
    await mapReady;
    for (const id of ["targets", "excluded", "own"]) map.getSource(id).setData(toFeatures([]));
    return;
  }

  const mine = ++requestSeq;
  $("loading").hidden = false;
  try {
    const ownPromise = state.own
      ? getJson(`/api/chains/${encodeURIComponent(state.own.chain_key)}/locations?include_unverified=true` +
          (state.usState ? `&state=${state.usState}` : ""))
      : Promise.resolve(null);
    const [result, own] = await Promise.all([postJson("/api/targets", targetRequest()), ownPromise]);
    if (mine !== requestSeq) return;
    lastResult = result;

    await mapReady;
    const radiusM = state.radiusMi * METERS_PER_MILE;
    map.getSource("targets").setData(toFeatures(result.targets, { kind: "target", radius_m: radiusM }));
    map.getSource("excluded").setData(toFeatures(result.excluded, { kind: "excluded" }));
    map.getSource("own").setData(toFeatures(own ? own.locations : [], { kind: "own", radius_m: state.ownRadiusMi * METERS_PER_MILE }));

    $("n-targets").textContent = fmt.format(result.targets.length);
    const where = state.usState ? ` in ${STATES.find(([c]) => c === state.usState)[1]}` : " in the US";
    const parts = [`${state.target.display_name}${where}, ${state.radiusMi} mi radius`];
    if (state.own) parts.push(`${fmt.format(result.excluded_count)} skipped within ${state.ownRadiusMi} mi of your ${state.own.display_name}`);
    if (!state.unverified) parts.push("confirmed-open only");
    $("summary-detail").textContent = parts.join(" · ");
    $("summary").hidden = false;
    $("export").hidden = result.targets.length === 0;
    if (!$("campaign").value) $("campaign").placeholder = `Turf - ${state.target.chain_key}`;
    if (result.targets.length === 0) {
      showError(result.candidates
        ? "Every location is near one of yours. Lower the skip distance to keep some."
        : "No locations match. Try all states or include unverified locations.");
    }
    renderList(result.targets);
    if (fit) fitTo(result.targets.length ? result.targets : (own ? own.locations : []));
  } catch (err) {
    if (mine === requestSeq) showError(err.message);
  } finally {
    if (mine === requestSeq) $("loading").hidden = true;
  }
}

// ---------- Export ----------

async function exportCsv(format, button) {
  if (!state.target) return;
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
  if (state.target) p.set("chain", state.target.chain_key);
  if (state.own) p.set("own", state.own.chain_key);
  if (state.usState) p.set("state", state.usState);
  if (state.radiusMi !== 1) p.set("r", String(state.radiusMi));
  if (state.own && state.ownRadiusMi !== 1) p.set("skip", String(state.ownRadiusMi));
  if (state.unverified) p.set("unverified", "1");
  const qs = p.toString();
  history.replaceState(null, "", qs ? `?${qs}` : location.pathname);
}

async function chainByKey(key) {
  const results = await getJson(`/api/chains/search?q=${encodeURIComponent(key)}&limit=5`);
  return results.find((c) => c.chain_key === key) || null;
}

async function restoreFromUrl() {
  const p = new URLSearchParams(location.search);
  const clamp = (v, lo, hi, d) => (Number.isFinite(v) ? Math.min(hi, Math.max(lo, v)) : d);
  state.radiusMi = clamp(parseFloat(p.get("r")), 1, 50, 1);
  state.ownRadiusMi = clamp(parseFloat(p.get("skip")), 0.5, 10, 1);
  state.unverified = p.get("unverified") === "1";
  const st = (p.get("state") || "").toUpperCase();
  state.usState = STATES.some(([c]) => c === st) ? st : "";
  try {
    if (p.get("chain")) state.target = await chainByKey(p.get("chain"));
    if (p.get("own")) state.own = await chainByKey(p.get("own"));
  } catch (err) {
    showError(err.message);
  }
  $("radius").value = state.radiusMi;
  $("own-radius").value = state.ownRadiusMi;
  $("unverified").checked = state.unverified;
  $("state-select").value = state.usState;
  updateOutputs();
}

// ---------- Wiring ----------

function updateOutputs() {
  $("radius-out").textContent = `${state.radiusMi} mi`;
  $("own-radius-out").textContent = `${state.ownRadiusMi} mi`;
  $("own-radius-field").hidden = !state.own;
  renderSelected("target-selected", state.target, () => { state.target = null; updateOutputs(); refresh(); });
  renderSelected("own-selected", state.own, () => { state.own = null; updateOutputs(); refresh(); });
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
  const select = $("state-select");
  for (const [code, name] of STATES) select.add(new Option(name, code));

  setupCombo("target-input", "target-results", (c) => { state.target = c; update({ fit: true }); });
  setupCombo("own-input", "own-results", (c) => { state.own = c; update(); });

  document.querySelectorAll("#examples button").forEach((b) => b.addEventListener("click", async () => {
    try {
      const c = await chainByKey(b.dataset.chain);
      if (c) { state.target = c; update({ fit: true }); }
    } catch (err) { showError(err.message); }
  }));

  const slow = debounce(() => refresh(), 250);
  $("radius").addEventListener("input", (e) => { state.radiusMi = Number(e.target.value); updateOutputs(); slow(); });
  $("own-radius").addEventListener("input", (e) => { state.ownRadiusMi = Number(e.target.value); updateOutputs(); slow(); });
  $("unverified").addEventListener("change", (e) => { state.unverified = e.target.checked; update(); });
  select.addEventListener("change", (e) => { state.usState = e.target.value; update({ fit: true }); });
  document.querySelectorAll(".export button[data-format]").forEach((b) =>
    b.addEventListener("click", () => exportCsv(b.dataset.format, b)));

  getJson("/api/meta").then((m) => { $("release").textContent = m.release; }).catch(() => {});
}

init();
await setupLayers();
await restoreFromUrl();
await update({ fit: true });
