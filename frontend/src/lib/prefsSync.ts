/** The live half of lib/prefs.ts: pull the person's prefs from the server at
 * page load (and again whenever settings sync brings a change in), write
 * them into localStorage, and push them into whatever already read the old
 * value — the store, the keymap, the prompt lists, the theme class.
 *
 * Kept apart from lib/prefs.ts because it imports the store, and the store
 * imports lib/prefs.ts (every store setter that saves a mapped key reports
 * the write there). */

import { api } from "../api/client";
import type { Json } from "../api/types";
import { queryClient, refreshConfig } from "../state/queries";
import { useUi } from "../state/store";
import { defaultHiddenBars } from "../components/sidebar/barDefs";
import {
  BREAK_DEFAULT_MINUTES,
  clampBreakMinutes,
  clampIdleMinutes,
  IDLE_DEFAULT_MINUTES,
} from "./breakTimer";
import { reloadKeymap } from "./keymap";
import { adoptAccent, storedAccent } from "../components/settings/screens/Appearance";
import { PRESETS_CHANGED, tidyUserPresets } from "./presets";
import { toast } from "./toast";
import {
  PREF_MAP,
  SEEDED_KEY,
  THEME_CHANGED,
  dirtyFields,
  flushPrefWrites,
  flushPrefsOnHide,
  noteRefused,
  pendingFields,
  planBoot,
  readAllLocal,
  refusedFields,
  setRefusedListener,
  settleFields,
  withoutEcho,
  writeGens,
  writeLocal,
  type PrefField,
  type Prefs,
} from "./prefs";

function seeded(): boolean {
  try {
    return localStorage.getItem(SEEDED_KEY) === "1";
  } catch {
    return false;
  }
}

function markSeeded() {
  try {
    localStorage.setItem(SEEDED_KEY, "1");
  } catch {
    /* storage unavailable */
  }
}

/** Push adopted values into the parts of the UI that read them at startup. */
function applyLive(fields: Partial<Prefs>) {
  const st: Record<string, unknown> = {};
  for (const f of Object.keys(fields) as PrefField[]) {
    const v = fields[f];
    switch (f) {
      case "hidden_bars":
        st.hiddenBars = new Set(Array.isArray(v) ? (v as string[]) : defaultHiddenBars());
        break;
      case "bar_order":
        st.barOrder = Array.isArray(v) ? v : [];
        break;
      case "reduce_motion":
        st.reduceMotion = v === true;
        break;
      case "break_on":
        st.breakReminder = v === true;
        break;
      case "break_every":
        st.breakEveryMin = clampBreakMinutes(typeof v === "number" ? v : BREAK_DEFAULT_MINUTES);
        break;
      case "idle_flock":
        st.idleFlock = v !== false;
        break;
      case "idle_after":
        st.idleFlockAfterMin = clampIdleMinutes(typeof v === "number" ? v : IDLE_DEFAULT_MINUTES);
        break;
      case "hints":
        st.hintsEnabled = v !== false;
        break;
      case "keymap":
        reloadKeymap();
        break;
      case "prompt_presets":
        document.dispatchEvent(new Event(PRESETS_CHANGED));
        break;
      case "theme":
        document.documentElement.classList.toggle("light", v === "light");
        document.dispatchEvent(new Event(THEME_CHANGED));
        break;
      // diff_mode / diff_base are read when a diff opens — the cache is enough.
    }
  }
  if (Object.keys(st).length) useUi.setState(st);
}

/** Adopt server values: localStorage first (the cache every reader uses),
 * then the live UI. Skipped: fields with a local write still in flight (that
 * write is newer than what the server just said), and — given the write
 * generations from when the pull started — fields written here since then,
 * even if that write already reached the server: the pull's snapshot was
 * read before it, so it would put the old value back. */
function adopt(fields: Partial<Prefs>, since: Map<PrefField, number> | null = null) {
  const inFlight = pendingFields();
  const now = since ? writeGens(since.keys()) : null;
  const take: Partial<Record<PrefField, unknown>> = {};
  for (const f of Object.keys(fields) as PrefField[]) {
    if (inFlight.has(f)) continue;
    if (since && now && (now.get(f) || 0) !== (since.get(f) || 0)) continue;
    take[f] = fields[f];
  }
  withoutEcho(() => {
    for (const f of Object.keys(take) as PrefField[]) writeLocal(f, take[f]);
  });
  applyLive(take as Partial<Prefs>);
}

/** The accent is a plain synced setting (`ui.accent`), not a pref, but the
 * desktop app never read it back: Appearance wrote it and the pre-paint
 * script read localStorage. Same reconcile rule as the prefs.
 *
 * A settings document with no `ui` group at all is the server saying "no
 * accent": GET /api/settings leaves out an empty group, and a cleared
 * accent is often the group's only field. So a missing group reads as an
 * unset accent (use the default), not as "nothing to compare". */
function reconcileAccent(settings: Json | undefined, mayUpload: boolean) {
  if (!settings || typeof settings !== "object") return;
  const rawUi = (settings as Json).ui;
  const ui = (rawUi && typeof rawUi === "object" ? rawUi : {}) as Json;
  const server = String(ui.accent ?? "");
  const local = storedAccent();
  if (server === local) return;
  if (!server && local && mayUpload) {
    api("/api/settings", { json: { ui: { accent: local } } }).catch(() => {});
    return;
  }
  adoptAccent(server);
}

let pulling: Promise<void> | null = null;
/** The one pull queued behind the running one (see pullPrefs). */
let queued: Promise<void> | null = null;

const ALL_FIELDS = PREF_MAP.map((p) => p.field);

/** GET /api/prefs and reconcile (see planBoot). Until this browser has
 * reconciled once it may upload what it holds; after that it only adopts —
 * except fields written here the server never confirmed ("dirty"), which
 * are uploaded first, every time, and never overwritten.
 *
 * Asked while a pull runs (a sync event, a reconnect, the echo of this
 * page's own POST), it queues exactly ONE more pull after it rather than
 * joining it: the running pull may have read the server before the change
 * that asked. Any number of asks during one pull share that one follow-up. */
export function pullPrefs(): Promise<void> {
  if (pulling) {
    if (!queued)
      queued = pulling
        .catch(() => {})
        .then(() => {
          queued = null;
          return pullPrefs();
        });
    return queued;
  }
  pulling = (async () => {
    try {
      // A list saved before names ignored case: fold it (keeping the newer
      // prompt) and say so, rather than the server dropping one unseen.
      const dropped = tidyUserPresets();
      if (dropped.length)
        toast(
          "Saved prompts are one per name (ignoring case) — kept the newer of " +
            dropped.map((n) => "“" + n + "”").join(", ")
        );
      let server: Partial<Prefs>;
      const unsent = pendingFields();
      const gens = writeGens(unsent);
      // Every field's write generation as the pull starts: a write made
      // after this is newer than anything the GET below can return.
      const started = writeGens(ALL_FIELDS);
      try {
        server = (await api<Partial<Prefs>>("/api/prefs")) || {};
      } catch {
        return; // an older server: prefs stay per browser, as they were
      }
      const wasSeeded = seeded();
      const plan = planBoot(server, readAllLocal(), wasSeeded, pendingFields());
      // Fields this device won't take from this caller (a keymap or saved
      // prompts, on a gate-off device reached from elsewhere): the rest
      // still counts as uploaded; these stay dirty — kept in this browser.
      let refused: PrefField[] = [];
      if (Object.keys(plan.upload).length) {
        try {
          const res = await api("/api/prefs", { json: plan.upload });
          refused = refusedFields(res) || [];
          if (refused.length) noteRefused(refused, res);
        } catch (e) {
          const r = refusedFields(e);
          if (r) {
            refused = r;
            noteRefused(r, e);
          } else {
            // Not seeded: next load tries the upload again rather than
            // reading the still-unset server fields as "cleared". Dirty
            // fields stay dirty, and the write-back retries them.
            if (Object.keys(plan.apply).length) adopt(plan.apply, started);
            if (dirtyFields().size) void flushPrefWrites();
            return;
          }
        }
      }
      // Every other unsent field is now on the server: uploaded just now, or
      // it already held the same value.
      settleFields(
        [...unsent].filter((f) => !refused.includes(f)),
        gens
      );
      if (Object.keys(plan.apply).length) adopt(plan.apply, started);
      markSeeded();
      try {
        reconcileAccent(
          (await api<{ settings?: Json }>("/api/settings"))?.settings,
          !wasSeeded,
        );
      } catch {
        /* the accent just stays as it is */
      }
    } finally {
      pulling = null;
    }
  })();
  return pulling;
}

let installed = false;

/** Page-load wiring (main.tsx, after first paint): one reconcile now, a
 * re-pull whenever settings sync adopted something from another device or
 * the event socket reconnects, and unsent writes flushed as the page hides
 * or goes away. */
export function installPrefsSync(): void {
  if (installed) return;
  installed = true;
  setRefusedListener((_fields, message) => toast(message, { duration: 10000 }));
  void pullPrefs();
  if (typeof window !== "undefined" && typeof window.addEventListener === "function") {
    // A reload or a closed desktop window inside the 500 ms debounce used to
    // drop the write; keepalive lets the POST outlive the page.
    window.addEventListener("pagehide", () => flushPrefsOnHide());
    if (typeof document !== "undefined")
      document.addEventListener("visibilitychange", () => {
        if (document.visibilityState === "hidden") flushPrefsOnHide();
      });
  }
  const ev = window.mindflock?.events;
  if (!ev) return;
  let t: ReturnType<typeof setTimeout> | null = null;
  const repull = () => {
    // One pull per burst — a sync pass can adopt several units at once.
    if (t) clearTimeout(t);
    t = setTimeout(() => void pullPrefs(), 300);
  };
  // Replays included: after a reconnect, the events from while the socket
  // was down arrive as replays, and they are exactly the changes this page
  // missed. A pull is idempotent, so an extra one costs a GET.
  ev.subscribe("settings.synced", () => {
    void queryClient.invalidateQueries({ queryKey: ["settings"] });
    void refreshConfig();
    repull();
  });
  // A server restart resets the event cursor and loses its backlog: whatever
  // synced in before this page reconnected is only visible by asking.
  // events.js connects at load, usually before this runs, and onStatus
  // doesn't replay the current state — so a socket already up counts as the
  // first connection (the pull above covers it), and the next "connected"
  // is a REconnect that re-pulls.
  let connectedOnce = ev.connected === true;
  if (typeof ev.onStatus === "function")
    ev.onStatus((status) => {
      if (status !== "connected") return;
      if (connectedOnce) repull();
      connectedOnce = true;
    });
}
