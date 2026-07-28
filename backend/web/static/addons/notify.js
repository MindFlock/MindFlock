// Notify addon frontend: desktop notifications for session events.
//
// The reference implementation of the generic addon-module contract
// (docs/extensions.md): this file is the descriptor's `module` URL;
// core/slots.js imports it, then calls
// window.mindflockAddons[<addon id>].init(ctx) with:
//   ctx.descriptor — this addon's FrontendDescriptor (id, label, api_base…)
//   ctx.addon      — {id, label} of the owning addon
//   ctx.events     — window.mindflock.events (subscribe/lastSeq/connected)
//   ctx.sessions   — window.mindflock.sessions() snapshot accessor (optional)
//   ctx.toast      — window.mindflock.toast when the SPA provides one (optional)
//
// Behavior: an On/Off toggle in the addon's sidebar bar (state persisted in
// localStorage). Notification permission is requested lazily — only when the
// user first turns it on. The event → notification rules come from
// GET /api/notify/config; a shown notification carries the session title and
// clicking it focuses this tab.

const STORE_KEY = "mindflock.notify.enabled";
const DEDUPE_MS = 5000; // at most one notification per (session, event) per 5s

function isEnabled() {
  try {
    return localStorage.getItem(STORE_KEY) === "1";
  } catch (e) {
    return false;
  }
}

function setEnabled(on) {
  try {
    localStorage.setItem(STORE_KEY, on ? "1" : "0");
  } catch (e) {
    /* private mode: the toggle just won't persist */
  }
}

async function fetchRules(apiBase) {
  try {
    const r = await fetch((apiBase || "/api/notify") + "/config");
    if (!r.ok) throw new Error(String(r.status));
    return (await r.json()).rules || [];
  } catch (e) {
    console.warn("notify addon: config fetch failed", e);
    return [];
  }
}

// A rule matches when the event name is equal and its old/new constraints
// (null = any value) hold against the envelope.
function ruleMatches(rule, env) {
  if (rule.event !== env.event) return false;
  if (rule.old != null && rule.old !== env.old) return false;
  if (rule.new != null && rule.new !== env.new) return false;
  return true;
}

function fill(template, env) {
  return String(template || "").replace(/\{session\}/g, env.session || "session");
}

window.mindflockAddons = window.mindflockAddons || {};
window.mindflockAddons.notify = {
  init(ctx) {
    // This module has no visible surface of its own anymore — the on/off toggle
    // lives in the bell dropdown (app.js) and Settings → Notifications. Here we
    // just subscribe to the event bus and expose enable/disable/state so those
    // UIs drive the same flow, staying in sync via the "mf-notify-state" event.

    // When the Notification API is missing (most commonly a plain-http origin —
    // browsers gate it to secure contexts, and MindFlock's default serving mode
    // is http over tailscale/LAN) or the event bus never came up, expose an
    // "unsupported" state with the reason WHY, so the bell/Settings toggles can
    // render themselves disabled with an explanatory tooltip.
    const supported = "Notification" in window;
    const busOk = !!(ctx && ctx.events && typeof ctx.events.subscribe === "function");
    if (!supported || !busOk) {
      const why = !supported
        ? (window.isSecureContext
            ? "This browser has no desktop-notification support"
            : "Desktop notifications need a secure origin — open MindFlock via HTTPS or localhost")
        : "Live event stream unavailable — reload the page";
      console.warn("notify addon:", why);
      window.mindflockAddons.notify.supported = false;
      window.mindflockAddons.notify.unavailableReason = why;
      window.mindflockAddons.notify.state = () => "unsupported";
      return;
    }

    const lastShown = new Map(); // "session|event" -> ts of last notification
    let rules = [];
    let subscribed = false;

    function onEvent(env) {
      if (!isEnabled() || Notification.permission !== "granted") return;
      // /api/events replays a backlog (~100 envelopes) on every (re)connect —
      // page refresh or server restart. Skip those, or each reload re-pops a
      // desktop notification for stale clarify/PR/budget history. Mirrors the
      // in-app toast guard in app.js (feature-detected so an older core/events.js
      // that lacks isReplay can't break notifications entirely).
      if (ctx.events && typeof ctx.events.isReplay === "function" && ctx.events.isReplay(env)) return;
      for (const rule of rules) {
        if (rule.enabled === false) continue;  // user muted this rule
        if (!ruleMatches(rule, env)) continue;
        const key = env.session + "|" + env.event;
        const now = Date.now();
        if (now - (lastShown.get(key) || 0) < DEDUPE_MS) continue;
        lastShown.set(key, now);
        try {
          const n = new Notification(fill(rule.title, env), {
            body: fill(rule.body, env),
            tag: key, // replaces a still-visible older one for the same key
            icon: "/logo.png", // static/ is mounted at "/" (see server.py)
          });
          n.onclick = () => {
            try {
              window.focus();
            } catch (e) {
              /* some platforms refuse programmatic focus */
            }
            n.close();
          };
        } catch (e) {
          console.warn("notify addon: Notification failed", e);
        }
      }
    }

    async function start() {
      if (!rules.length) {
        rules = await fetchRules(ctx.descriptor && ctx.descriptor.api_base);
      }
      if (!subscribed) {
        ctx.events.subscribe("*", onEvent);
        subscribed = true;
      }
    }

    // Current tri-state for any UI showing the toggle (sidebar bar + Settings).
    function state() {
      if (Notification.permission === "denied") return "blocked";
      return isEnabled() ? "on" : "off";
    }
    // Broadcast so every mirror (bar button, Settings toggle) repaints together.
    function announce() {
      try {
        document.dispatchEvent(
          new CustomEvent("mf-notify-state", { detail: { state: state() } })
        );
      } catch (e) {
        /* CustomEvent unsupported — mirrors just won't auto-sync */
      }
    }
    // Turn on: request permission on first enable, subscribe, persist. Returns
    // the resulting state. Shared by the bar button and the Settings screen.
    async function enable() {
      const perm =
        Notification.permission === "granted"
          ? "granted"
          : await Notification.requestPermission();
      if (perm !== "granted") {
        if (typeof ctx.toast === "function") ctx.toast("Notifications are blocked by the browser");
        announce();
        return state();
      }
      setEnabled(true);
      await start();
      announce();
      return state();
    }
    function disable() {
      setEnabled(false);
      announce();
      return state();
    }

    // Re-pull the rules (with their enabled/muted state) so a per-rule toggle
    // in Settings takes effect immediately, without a page reload.
    async function refreshRules() {
      rules = await fetchRules(ctx.descriptor && ctx.descriptor.api_base);
    }

    // Public API so app.js (the bell dropdown + Settings → Notifications) can
    // drive the same flow.
    window.mindflockAddons.notify.enable = enable;
    window.mindflockAddons.notify.disable = disable;
    window.mindflockAddons.notify.refreshRules = refreshRules;
    window.mindflockAddons.notify.state = state;
    window.mindflockAddons.notify.supported = true;

    // Enabled on a previous visit: resume silently. (Permission was granted
    // back then; if it has been revoked since, onEvent's check keeps us quiet.)
    if (isEnabled()) start();
  },
};
