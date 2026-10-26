/** Top bar (port of the 030 partial + section 17's chrome wiring):
 * sidebar/theme cluster, notifications bell and mic, the main menu (New / Intake /
 * Verify / Schedule / Settings, then the command palette as an icon), centered
 * wordmark, and the Electron drag region.
 *
 * Only the daily loop is on the bar. Everything that waits on you is the
 * bell's (one badge); saved prompts are a tab of Customize (the sidebar
 * footer); Recently closed sits under the session list.
 *
 * On macOS the whole thing mirrors: the window shows the native traffic lights
 * top-left (electron/main.js `titleBarStyle: 'hidden'`), so the bar leaves room
 * for them and moves the logo / theme / bell cluster to the right — where a Mac
 * has nothing else, since our own – □ ✕ aren't drawn there. Same elements, same
 * behaviour, mirrored placement. */

import { useEffect, useLayoutEffect, useRef, useState } from "react";
import { flushSync } from "react-dom";
import { useUi } from "../state/store";
import { useDoctor, useTestPlans } from "../state/queries";
import { dueCount, failingCount, verifyCountLine } from "./dialogs/verify";
import { rethemeAll } from "../lib/terminals";
import { NotificationsBell } from "./NotificationsBell";
import { TopBarMic } from "./TopBarMic";
import { redrawFavicon } from "./EventToasts";
import { THEME_CHANGED } from "../lib/prefs";
import { setTheme } from "../lib/theme";
import { hasNativeWindowControls, isFullScreen, onFullScreenChanged } from "../lib/shell";
import { openSettings } from "../lib/openSettings";
import { inShell } from "../lib/shell";
import { keymapVersion, subscribeKeymap } from "../lib/keymap";
import { usableShortcuts } from "./palette/ShortcutsSheet";
import { VERIFY_BLURB } from "../lib/vocab";
import { useScheduleStatus } from "../lib/useScheduleStatus";
import { failingSchedules } from "../lib/templates";
import { useCalm } from "../lib/useCalm";
import { topBarShows } from "../lib/calm";

/* The wordmark carries the RUNNING ENGINE's version (GET /api/doctor), not the
 * desktop shell's: this frontend ships inside the engine wheel, so that string
 * is the provenance of the UI you're looking at and of the sessions behind it.
 * When the shell is a different release the drift toast (electron/main.js) says
 * so explicitly — this only has to answer "what am I running". Read from the
 * shared doctor query (cached, so a remount doesn't flash an empty slot).
 *
 * A source tree with no installed dist reports "0+unknown" (see
 * backend/__init__.py). Show nothing rather than a fake-looking version — such
 * a run is a dev shell, which the -DEV badge marks. */
function engineVersion(raw: string | undefined): string {
  const v = (raw || "").trim();
  return !v || !/^\d/.test(v) || v.startsWith("0+") ? "" : v;
}

/** What a narrow bar gives up, in order: the wordmark first (it is a label,
 * not a control), then the foldable items from the RIGHT end (Settings, then
 * Schedule, then Verify — Verify's count is the reason it earns a slot, so it
 * goes last) into one ⋯ that sits where they were. New, Intake and the palette
 * never fold. Every argument is a measured width in px, gaps included:
 * `room` the bar's content box, `fixed` everything that always shows,
 * `brand` the wordmark, `items` the foldable items left to right, `more` the ⋯.
 * Pure, so the rule is pinned by a test rather than by a screenshot. */
export interface BarWidths {
  room: number;
  fixed: number;
  brand: number;
  items: number[];
  more: number;
}
export interface BarFit {
  brand: boolean;
  /** How many of `items`, from the left, stay on the bar; the rest are in ⋯. */
  shown: number;
}
export function fitTopBar({ room, fixed, brand, items, more }: BarWidths): BarFit {
  // Half a px of slack: rects are fractional and a sum of them can land a
  // hair over a room it visibly fits.
  const fits = (w: number) => w <= room + 0.5;
  const upTo = (n: number) => items.slice(0, n).reduce((a, b) => a + b, fixed);
  if (fits(upTo(items.length) + brand)) return { brand: true, shown: items.length };
  if (fits(upTo(items.length))) return { brand: false, shown: items.length };
  let shown = items.length - 1;
  while (shown > 0 && !fits(upTo(shown) + more)) shown--;
  return { brand: false, shown };
}

export function TopBar() {
  const ui = useUi();
  // In the store, not local state: Settings → Appearance → Mode drives the
  // same value (lib/theme.ts), so the moon and the Mode buttons always agree.
  const light = useUi((s) => s.themeLight);
  const version = engineVersion(useDoctor().data?.version);
  // How much verification is waiting on you, without opening the dialog. Same
  // query and same helper the dialog itself uses, so the badge and the list can
  // never disagree — the discipline the Intake tab counts follow. The top bar is
  // mounted for the life of the page, which is deliberately what keeps this
  // (cheap, local-file) poll running: the point of a badge is to tell you about
  // the thing you were not already looking at.
  const { data: testPlans } = useTestPlans();
  const due = dueCount(testPlans?.plans || []);
  // Of those, the ones that FAILED: the badge turns red for them.
  const failing = failingCount(testPlans?.plans || []);
  // Schedule's badge: the scheduled templates whose last run FAILED — the
  // cards that say "Last run failed", nothing else. A schedule that is merely
  // set, or a run skipped because the last one was still going, is the
  // feature working and gets no mark.
  const sched = useScheduleStatus();
  const schedFailing = failingSchedules(sched.templates, sched.status).length;
  // The calm first hour (lib/calm.ts topBarShows): Verify waits for its first
  // checklist and Schedule for its first template. Their shortcuts, the
  // palette and the #verify / #schedule links keep working meanwhile.
  const shows = topBarShows({
    calm: useCalm(),
    checklists: testPlans?.plans?.length ?? 0,
    templates: sched.templates.length,
  });
  // Evaluated once: the shell can't grow or lose its title bar mid-run.
  const [mac] = useState(hasNativeWindowControls);
  // macOS hides the traffic lights in fullscreen — stop reserving their room.
  const [fullScreen, setFullScreen] = useState(false);

  // Whoever changed the theme — this toggle, Settings → Appearance, or a pref
  // adopted from another of your devices — repaint what the CSS can't reach.
  useEffect(() => {
    const onTheme = () => {
      rethemeAll(); // re-theme already-open terminals
      redrawFavicon(); // the tab favicon inverts with the theme
    };
    document.addEventListener(THEME_CHANGED, onTheme);
    return () => document.removeEventListener(THEME_CHANGED, onTheme);
  }, []);

  useEffect(() => {
    if (!mac) return;
    // Ask for the current state (a window already fullscreen at load never gets
    // a transition), then follow changes.
    let live = true;
    void isFullScreen().then((f) => {
      if (live) setFullScreen(f);
    });
    const off = onFullScreenChanged(setFullScreen);
    return () => {
      live = false;
      off();
    };
  }, [mac]);


  const toggleTheme = () => setTheme(!light);

  // Tooltip shortcuts come from the live keymap (rebinds included), never a
  // hand-typed string: in a browser tab, only keys the browser hands to the
  // page (Ctrl+N opens a browser window there — Alt+N is New).
  const [, setKeysVer] = useState(keymapVersion());
  useEffect(() => subscribeKeymap(() => setKeysVer(keymapVersion())), []);
  const tab = !inShell();
  const keys = (id: string) => {
    const k = usableShortcuts(id, tab);
    return k.length ? " (" + k.join(" / ") + ")" : "";
  };

  // One tooltip per item, worn by the bar button and by its row in ⋯ alike.
  const verifyTitle = "Verify — " + VERIFY_BLURB + keys("verify") + "\n" + verifyCountLine(due, failing);
  const scheduleTitle =
    "Schedule — templates; starts sessions on a timer" +
    keys("schedule") +
    (schedFailing > 0
      ? "\n" +
        (schedFailing === 1
          ? "1 scheduled template's last run failed"
          : schedFailing + " scheduled templates' last runs failed")
      : "");

  // --- Fitting a narrow window (fitTopBar above) ---------------------------
  // The bar is fixed and never wraps, so whatever doesn't fit isn't squeezed,
  // it is simply under the window's edge — the palette icon first, since it
  // is last. So the bar MEASURES itself and folds, rather than guessing from
  // breakpoints: the words' widths change with the font, the theme set, a
  // badge, a rebound shortcut's absence, the mac lights' gap.
  //
  // The foldable items, in bar order; Verify / Schedule only once the calm
  // rule above lets them on the bar at all (this never overrides it).
  const foldable = [
    shows.verify && {
      key: "verify",
      label: "Verify",
      title: verifyTitle,
      count: due,
      failing: failing > 0,
      open: () => ui.openDialogFor("verify"),
    },
    shows.schedule && {
      key: "schedule",
      label: "Schedule",
      title: scheduleTitle,
      count: schedFailing,
      failing: true,
      open: () => ui.openDialogFor("schedule"),
    },
    { key: "settings", label: "Settings", title: "Settings", count: 0, failing: false, open: () => openSettings() },
  ].filter((f) => !!f);
  const [fit, setFit] = useState<BarFit>({ brand: true, shown: Infinity });
  const folded = foldable.slice(fit.shown);
  const isFolded = (key: string) => folded.some((f) => f.key === key);
  const barRef = useRef<HTMLDivElement>(null);
  const startRef = useRef<HTMLDivElement>(null);
  const menuRef = useRef<HTMLElement>(null);
  const brandRef = useRef<HTMLSpanElement>(null);
  // A folded item (display:none, so the tours' target lookup skips it) has no
  // width to read: the last one it had stands in. Every item is on the bar at
  // first paint, so each is measured before it can ever be folded.
  const widths = useRef<Record<string, number>>({});
  const measure = useRef<() => void>(() => {});
  measure.current = () => {
    const bar = barRef.current;
    const start = startRef.current;
    const menu = menuRef.current;
    if (!bar || !start || !menu) return;
    const w = (el: Element | null) => (el ? el.getBoundingClientRect().width : 0);
    const gap = (el: Element) => parseFloat(getComputedStyle(el).columnGap) || 0;
    const seen = widths.current;
    const menuGap = gap(menu);
    let fixed = 0;
    for (const el of Array.from(menu.children) as HTMLElement[]) {
      const key = el.dataset.fold || (el.classList.contains("tb-more") ? "more" : "");
      const width = w(el);
      if (!key) fixed += width + menuGap;
      else if (width > 0) seen[key] = width;
    }
    const brandW = w(brandRef.current);
    if (brandW > 0) seen.brand = brandW;
    const cs = getComputedStyle(bar);
    const barGap = gap(bar);
    const end = bar.querySelector(".tb-end");
    // The desktop shell's – □ ✕ float over the drag tail (electron/main.js).
    const winctl = document.getElementById("mf-winctl");
    const room =
      bar.clientWidth - (parseFloat(cs.paddingLeft) || 0) - (parseFloat(cs.paddingRight) || 0) - w(winctl);
    const next = fitTopBar({
      room,
      // the left cluster + the start zone's gap + the kept menu items, then
      // the gap to the drag tail and, on a Mac, the mirrored cluster.
      fixed: w(start.firstElementChild) + gap(start) + fixed - menuGap + barGap + (end ? barGap + w(end) : 0),
      brand: (seen.brand || 0) + barGap,
      items: foldable.map((f) => (seen[f.key] || 0) + menuGap),
      // Never measured yet: the palette icon's box, which ⋯ shares.
      more: (seen.more || w(document.getElementById("palette-btn"))) + menuGap,
    });
    setFit((prev) => {
      if (prev.brand === next.brand && prev.shown === next.shown) return prev;
      return next;
    });
  };
  // Before paint, on every render: a count that appears, Verify arriving with
  // its first checklist, the version landing beside the wordmark — each is
  // folded in the same frame it is drawn, so the bar never shows itself
  // clipped (and never jumps on load).
  useLayoutEffect(() => measure.current());
  useEffect(() => {
    const bar = barRef.current;
    if (!bar || typeof ResizeObserver === "undefined") return;
    // The window resizing, and a web font swapping in under the words.
    const ro = new ResizeObserver(() => flushSync(() => measure.current()));
    ro.observe(bar);
    if (menuRef.current) ro.observe(menuRef.current);
    if (startRef.current?.firstElementChild) ro.observe(startRef.current.firstElementChild);
    return () => ro.disconnect();
  }, []);

  // The ⋯ menu itself.
  const [moreOpen, setMoreOpen] = useState(false);
  const moreRef = useRef<HTMLDivElement>(null);
  const moreBtn = useRef<HTMLButtonElement>(null);
  const hasFolded = folded.length > 0;
  useEffect(() => {
    if (!hasFolded) setMoreOpen(false);
  }, [hasFolded]);
  useEffect(() => {
    if (!moreOpen) return;
    moreRef.current?.querySelector<HTMLElement>('[role="menuitem"]')?.focus();
    const onDown = (e: MouseEvent) => {
      if (!moreRef.current?.contains(e.target as Node)) setMoreOpen(false);
    };
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        // Capture phase: this Escape is the menu's, not a dialog's below it.
        e.stopPropagation();
        e.preventDefault();
        setMoreOpen(false);
        moreBtn.current?.focus();
        return;
      }
      if (e.key !== "ArrowDown" && e.key !== "ArrowUp") return;
      const rows = Array.from(moreRef.current?.querySelectorAll<HTMLElement>('[role="menuitem"]') || []);
      if (!rows.length) return;
      e.preventDefault();
      const at = rows.indexOf(document.activeElement as HTMLElement);
      const step = e.key === "ArrowDown" ? 1 : -1;
      rows[(at + step + rows.length) % rows.length].focus();
    };
    document.addEventListener("mousedown", onDown);
    document.addEventListener("keydown", onKey, true);
    return () => {
      document.removeEventListener("mousedown", onDown);
      document.removeEventListener("keydown", onKey, true);
    };
  }, [moreOpen]);
  const foldedCount = folded.reduce((n, f) => n + f.count, 0);
  const foldedFailing = folded.some((f) => f.count > 0 && f.failing);

  // The cluster that swaps sides on macOS. Defined once and rendered in exactly
  // one place, so the two layouts can't drift apart.
  const logo = <span id="brand-logo" aria-hidden="true" />;
  const themeToggle = (
    <button
      id="theme-btn"
      type="button"
      title={light ? "Switch to dark mode" : "Switch to light mode"}
      aria-label="Toggle light / dark mode"
      onClick={toggleTheme}
    >
      {/* Monochrome, not ☀️/🌙: an emoji glyph paints its own colors, and a
          yellow moon on a yellow top bar (Goldfinch, Toucan) vanishes.
          currentColor follows --text, which regions.css rebinds per region,
          so these stay legible on a bar of any hue. */}
      <svg viewBox="0 0 24 24" width="15" height="15" aria-hidden="true">
        {light ? (
          <g fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round">
            <circle cx="12" cy="12" r="4.2" />
            <path d="M12 2.5v2.4M12 19.1v2.4M2.5 12h2.4M19.1 12h2.4M5.3 5.3l1.7 1.7M17 17l1.7 1.7M18.7 5.3L17 7M7 17l-1.7 1.7" />
          </g>
        ) : (
          <path
            fill="currentColor"
            d="M20.2 14.6A8.6 8.6 0 0 1 9.4 3.8a8.6 8.6 0 1 0 10.8 10.8z"
          />
        )}
      </svg>
    </button>
  );

  return (
    <div
      id="topbar"
      ref={barRef}
      data-mac={mac ? "" : undefined}
      data-mac-lights={mac && !fullScreen ? "" : undefined}
    >
      <div className="tb-start" ref={startRef}>
        <div className="tb-left">
          {!mac && logo}
          <button
            id="sidebar-toggle"
            type="button"
            title={(ui.sidebarHidden ? "Show" : "Hide") + " sidebar" + keys("sidebar")}
            aria-label="Toggle sidebar"
            onClick={() => ui.toggleSidebar()}
          >
            <svg
              viewBox="0 0 24 24"
              width="16"
              height="16"
              fill="none"
              stroke="currentColor"
              strokeWidth="2"
              strokeLinejoin="round"
            >
              <rect x="3" y="4" width="18" height="16" rx="2.5" />
              <line x1="9" y1="4" x2="9" y2="20" />
            </svg>
          </button>
          {!mac && themeToggle}
          {!mac && <NotificationsBell />}
          {/* The mic sits beside the bell (TopBarMic): talk to the focused
              session — the bell says what needs you, the mic answers it. */}
          {!mac && <TopBarMic />}
        </div>
        <nav className="tb-menu" ref={menuRef} aria-label="Main actions">
          <button
            id="new-btn"
            className="tb-new"
            title={"New session" + keys("new")}
            onClick={() => ui.openDialogFor("new-session")}
          >
            New
          </button>
          {/* Intake sits next to New because both are about starting sessions —
              one from scratch, one from what came in. */}
          <button
            id="intake-btn"
            className="tb-item"
            type="button"
            title={"Intake — tickets, pull requests and issues; starts sessions when work arrives" + keys("intake")}
            /* No aria-label: the visible "Intake" IS the name. The old one still
               said "Open work", the surface's name two renames ago, so a screen
               reader announced a button nobody could see. */
            onClick={() => ui.openDialogFor("intake")}
          >
            Intake
          </button>
          {/* Verify closes the loop Intake opens: work came in there, and this is
              where it comes back once it has actually shipped. The count is the
              whole reason it earns a slot on the bar — work that ships while you
              are elsewhere is exactly the thing nobody remembers to go and look
              for. It counts SHIPPED CHANGES NOBODY HAS FINISHED CHECKING, which
              is the same rule the dialog's first heading renders (`dueCount` and
              the "Not checked yet" group are one predicate) — deliberately not
              "things waiting on you personally", since one of them may be an
              agent mid-run. */}
          {shows.verify && (
          <button
            id="verify-btn"
            className={"tb-item" + (isFolded("verify") ? " tb-folded" : "")}
            data-fold="verify"
            type="button"
            title={verifyTitle}
            /* No aria-label, for the same reason as Intake above: the visible
               "Verify" IS the name, and a second wording would be the one a
               screen reader announced. */
            onClick={() => ui.openDialogFor("verify")}
          >
            Verify
            {/* Nothing at all on zero, the way the Intake tab counts do it: a "0"
                that becomes "3" a moment later reads as "nothing to do", which is
                the one thing this badge must never say while it is still finding
                out. */}
            {due > 0 && <span className={"tb-count" + (failing > 0 ? " is-failing" : "")}>{due}</span>}
          </button>
          )}
          {/* Schedule: what starts by itself (and the saved setups it starts
              from). Before it had a slot here, the only ways in were the
              palette and New's strip — the owner couldn't find it. */}
          {shows.schedule && (
          <button
            id="schedule-btn"
            className={"tb-item" + (isFolded("schedule") ? " tb-folded" : "")}
            data-fold="schedule"
            type="button"
            title={scheduleTitle}
            onClick={() => ui.openDialogFor("schedule")}
          >
            Schedule
            {/* Only a failure, in the failure red, and nothing at zero — the
                rule Verify's count keeps. */}
            {schedFailing > 0 && <span className="tb-count is-failing">{schedFailing}</span>}
          </button>
          )}
          <button
            id="settings-btn"
            className={"tb-item" + (isFolded("settings") ? " tb-folded" : "")}
            data-fold="settings"
            type="button"
            title="Settings"
            aria-label="Open settings"
            onClick={() => openSettings()}
          >
            Settings
          </button>
          {/* What a narrow window folded (fitTopBar): one ⋯ where the folded
              items stood, carrying their counts, so a red badge is never lost
              with its word. The ids above stay on the (hidden) bar buttons. */}
          {hasFolded && (
            <div className="tb-more" ref={moreRef}>
              <button
                id="more-btn"
                ref={moreBtn}
                className="tb-item"
                type="button"
                title={"More — " + folded.map((f) => f.label).join(", ")}
                aria-label="More actions"
                aria-haspopup="menu"
                aria-expanded={moreOpen}
                onClick={() => setMoreOpen((v) => !v)}
              >
                <svg viewBox="0 0 24 24" width="15" height="15" aria-hidden="true" fill="currentColor">
                  <circle cx="5" cy="12" r="1.9" />
                  <circle cx="12" cy="12" r="1.9" />
                  <circle cx="19" cy="12" r="1.9" />
                </svg>
                {foldedCount > 0 && (
                  <span className={"tb-count" + (foldedFailing ? " is-failing" : "")}>{foldedCount}</span>
                )}
              </button>
              {moreOpen && (
                <div className="tb-more-menu" role="menu" aria-label="More actions">
                  {folded.map((f) => (
                    <button
                      key={f.key}
                      type="button"
                      role="menuitem"
                      className="tb-more-item"
                      data-fold-item={f.key}
                      title={f.title}
                      onClick={() => {
                        setMoreOpen(false);
                        f.open();
                      }}
                    >
                      <span>{f.label}</span>
                      {f.count > 0 && (
                        <span className={"tb-count" + (f.failing ? " is-failing" : "")}>{f.count}</span>
                      )}
                    </button>
                  ))}
                </div>
              )}
            </div>
          )}
          {/* The palette last, as an icon: it reaches everything above (and the
              rest), so it is the keyboard user's door, not one more word in the
              row. Monochrome currentColor like #theme-btn — never an emoji. */}
          <button
            id="palette-btn"
            className="tb-item"
            type="button"
            title={"Command palette" + keys("palette")}
            aria-label="Open command palette"
            onClick={() => ui.openDialogFor("palette")}
          >
            <svg
              viewBox="0 0 24 24"
              width="15"
              height="15"
              aria-hidden="true"
              fill="none"
              stroke="currentColor"
              strokeWidth="2.2"
              strokeLinecap="round"
            >
              <circle cx="10.5" cy="10.5" r="6" />
              <path d="M15 15l5 5" />
            </svg>
          </button>
        </nav>
      </div>
      {/* No title= on the wordmark: .tb-brand is pointer-events:none so the
          window-drag works across it, which also means a tooltip can never
          fire. The version is visible, so nothing is lost. */}
      <span className={"tb-brand" + (fit.brand ? "" : " tb-folded")} ref={brandRef}>
        MindFlock
        {(window as unknown as { mfshell?: { dev?: boolean } }).mfshell?.dev && (
          <span style={{ color: "var(--red)", fontWeight: 700 }}>-DEV</span>
        )}
        {version && <span className="tb-version">v{version}</span>}
      </span>
      <div className="tb-drag" />
      {/* macOS: the mirror of .tb-left. Ordered so the logo anchors the far
          corner, the way it does on the left everywhere else. */}
      {mac && (
        <div className="tb-end">
          <TopBarMic />
          <NotificationsBell />
          {themeToggle}
          {logo}
        </div>
      )}
    </div>
  );
}
