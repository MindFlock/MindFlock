// Addon-slot renderer + provider picker (the frontend half of the addon framework).
//
// Fetches the backend manifests and makes the UI extensible without editing
// app.js / index.html:
//   - /api/providers populates the New-session "Program" autocomplete, so a user
//     picks claude / aider / codex (or types any custom CLI).
//   - /api/addons renders a sidebar bar for every addon whose descriptor is NOT
//     flagged builtin_ui (MindFlock + Assistant keep their bespoke bars for now).
//     A brand-new addon therefore surfaces in the UI with ZERO frontend edits.
//   - Descriptors that carry a `module` URL (and aren't builtin_ui) get their ES
//     module dynamically imported; a module that registered
//     window.mindflockAddons[<addon id>] = { init(ctx) } is then initialized
//     with the client event bus + sessions accessor (see docs/extensions.md).
//
// Loaded as a module AFTER app.js, operating only on its own mount points
// (#provider-list, #addon-bars), so it never interferes with the core SPA.
import { WsXterm } from "/core/ws-xterm.js";

async function getJSON(url) {
  const r = await fetch(url);
  if (!r.ok) throw new Error(url + " -> " + r.status);
  return r.json();
}

async function populateProviderPicker() {
  const list = document.getElementById("provider-list");
  if (!list) return;
  try {
    const data = await getJSON("/api/providers");
    list.innerHTML = "";
    for (const p of data.providers || []) {
      const opt = document.createElement("option");
      opt.value = p.name;
      if (p.profiles && p.profiles.length) {
        opt.label = p.name + " (" + p.profiles.map((x) => x.id).join("/") + ")";
      }
      list.appendChild(opt);
    }
  } catch (e) {
    /* providers are optional UI sugar; ignore */
  }
}

let _openPane = null;

function openAddonPane(addon, desc) {
  // Reuse one pane per addon; toggle it closed if already open.
  if (_openPane && _openPane.id === addon.id) {
    _openPane.xterm.dispose();
    _openPane.el.remove();
    _openPane = null;
    return;
  }
  if (_openPane) {
    _openPane.xterm.dispose();
    _openPane.el.remove();
    _openPane = null;
  }
  const grid = document.getElementById("grid");
  if (!grid || !desc.ws_path) return;
  const el = document.createElement("section");
  el.className = "pane addon-pane";
  const head = document.createElement("div");
  head.className = "pane-head";
  head.textContent = addon.label;
  const close = document.createElement("button");
  close.textContent = "×";
  close.title = "Close";
  close.addEventListener("click", () => {
    if (_openPane) { _openPane.xterm.dispose(); _openPane.el.remove(); _openPane = null; }
  });
  head.appendChild(close);
  const body = document.createElement("div");
  body.className = "pane-term";
  el.appendChild(head);
  el.appendChild(body);
  grid.appendChild(el);
  const xterm = new WsXterm({ host: body, wsPath: desc.ws_path, interactive: !desc.read_only }).start();
  _openPane = { id: addon.id, el, xterm };
}

function renderAddonBar(host, addon, desc) {
  const bar = document.createElement("div");
  bar.className = "addon-bar";
  bar.dataset.addon = addon.id;
  const label = document.createElement("span");
  label.className = "addon-label";
  label.textContent = addon.label;
  bar.appendChild(label);
  if (desc.ws_path) {
    const btn = document.createElement("button");
    btn.className = "addon-toggle";
    btn.textContent = "Open";
    btn.title = "Open " + addon.label;
    btn.addEventListener("click", () => openAddonPane(addon, desc));
    bar.appendChild(btn);
  }
  host.appendChild(bar);
}

async function renderAddonBars() {
  const host = document.getElementById("addon-bars");
  if (!host) return [];
  try {
    const data = await getJSON("/api/addons");
    host.innerHTML = "";
    for (const addon of data.addons || []) {
      for (const desc of addon.frontend || []) {
        if (desc.where !== "sidebar-bar") continue;
        if (desc.builtin_ui) continue; // MindFlock/Assistant keep their bespoke bars
        renderAddonBar(host, addon, desc);
      }
    }
    return data.addons || [];
  } catch (e) {
    /* addons are optional UI; ignore */
    return [];
  }
}

// --- generic addon modules (roadmap B5) ------------------------------------ //
// The client API (core/events.js) loads alongside this module, so give
// window.mindflock.events a moment to appear before initializing addons.
function waitForClientApi(timeoutMs) {
  return new Promise((resolve) => {
    const t0 = Date.now();
    (function poll() {
      const mf = window.mindflock;
      if (mf && mf.events) return resolve(mf);
      if (Date.now() - t0 >= timeoutMs) return resolve(mf || null);
      setTimeout(poll, 50);
    })();
  });
}

const _initedAddonSlots = new Set();

async function initAddonModules(addons) {
  // Static files mount at "/", so descriptor module URLs like
  // "/addons/notify.js" resolve to static/addons/notify.js directly.
  const targets = [];
  for (const addon of addons || []) {
    for (const desc of addon.frontend || []) {
      if (desc.module && !desc.builtin_ui) targets.push([addon, desc]);
    }
  }
  if (!targets.length) return;
  const mf = await waitForClientApi(3000);
  const shared = {
    events: mf && mf.events ? mf.events : undefined,
    sessions: mf && typeof mf.sessions === "function" ? mf.sessions : undefined,
    toast: mf && typeof mf.toast === "function" ? mf.toast : undefined,
  };
  for (const [addon, desc] of targets) {
    const key = addon.id + ":" + desc.id;
    if (_initedAddonSlots.has(key)) continue;
    let mod;
    try {
      mod = await import(desc.module);
    } catch (e) {
      console.warn("addon module failed to load, skipping:", desc.module, e);
      continue;
    }
    const registry = window.mindflockAddons || {};
    const entry = registry[addon.id] || registry[desc.id] || (mod && mod.default);
    if (!entry || typeof entry.init !== "function") {
      console.warn("addon module registered no init():", desc.module);
      continue;
    }
    try {
      entry.init({
        descriptor: desc,
        addon: { id: addon.id, label: addon.label },
        events: shared.events,
        sessions: shared.sessions,
        toast: shared.toast,
      });
      _initedAddonSlots.add(key);
    } catch (e) {
      console.warn("addon init failed:", addon.id, e);
    }
  }
}

(async function main() {
  populateProviderPicker();
  const addons = await renderAddonBars(); // bars first, so modules can extend them
  await initAddonModules(addons);
})();

// Let the Settings provider-manager refresh the New-session picker after a
// create/delete without a page reload.
window.reloadProviderPicker = populateProviderPicker;
