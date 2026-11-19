// Applies a saved light/dark choice before first paint (no flash). Loaded as a
// plain blocking script in <head>; the CSP doesn't allow inline scripts.
// With no saved choice, CSS follows the system setting.
try {
  const saved = localStorage.getItem("turf-theme");
  if (saved === "light" || saved === "dark") document.documentElement.dataset.theme = saved;
} catch { /* storage blocked: follow the system */ }
