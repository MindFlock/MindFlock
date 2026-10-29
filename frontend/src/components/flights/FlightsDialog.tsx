/** Flights — one route from waiting to landed (docs/flights.md).
 *
 * Intake, Verify and Schedule were three top-bar surfaces that were really one
 * journey seen from three sides; this dialog is the journey. Its tabs:
 *
 *   Sky         the open expanse: every session a line of blocks floating in
 *               it (pan, zoom, drag a line anywhere), the bird on each
 *               line's active box, a new session started from the space, and
 *               the drawer of what is perched on you (Sky.tsx). On this tab
 *               the dialog takes the whole window (`.sky-full`).
 *   Flyways     the flyways (the wire's `plans`) on the canvas, and the editor
 *   Sources     Intake's tabs, mounted as they are (IntakeDialog embedded)
 *   Schedule    the templates view, mounted as it is (TemplatesDialog embedded)
 *   Checklists  Verify's list, mounted as it is (VerifyDialog embedded)
 *
 * The tab lives in the store's dialogTarget (`<tab>[/<deep link>]`, lib/flights
 * parseFlightsTarget), so the URL hash follows it and every old deep link —
 * openDialogFor("intake" | "verify" | "schedule", …) — lands on its tab, and
 * the old Board's (`board`) lands on the Sky. `sky/<title>` is the Sky on one
 * session (the pane's "Open in the Sky", the session ⋯ "Show in the Sky"). */

import { useCallback, useEffect, useRef, useState } from "react";
import { useUi } from "../../state/store";
import { Modal } from "../Modal";
import { InlineConfirm } from "../InlineConfirm";
import { TourButton } from "../onboarding/TourButton";
import { useAutoTour } from "../onboarding/useAutoTour";
import { IntakeDialog } from "../intake/IntakeDialog";
import { VerifyDialog } from "../dialogs/VerifyDialog";
import { TemplatesDialog } from "../dialogs/TemplatesDialog";
import { prefetchIntakeMeta, prefetchIntakePanels, useFlights } from "../../state/queries";
import { FLIGHTS_TABS, FLIGHTS_TAB_LABEL, needsYouFlights, parseFlightsTarget, type FlightsTab } from "../../lib/flights";
import { FLIGHTS_TOUR } from "../../lib/tours/flights";
import { Sky } from "./Sky";
import { Plans } from "./PlanEditor";
import { embeddedGuard } from "./embedGuard";
import { Bird } from "./Bird";
import "./FlightsDialog.css";

export function FlightsDialog() {
  const open = useUi((s) => s.openDialog === "flights");
  useAutoTour("flights", open);
  const target = useUi((s) => (s.openDialog === "flights" ? s.dialogTarget : null));
  const openDialogFor = useUi((s) => s.openDialogFor);
  const closeDialog = useUi((s) => s.closeDialog);
  const { tab, rest } = parseFlightsTarget(target);
  // The flyway editor's unsaved changes: leaving the Flyways tab (or the dialog)
  // asks first, inline.
  const [plansDirty, setPlansDirty] = useState(false);
  const [leaving, setLeaving] = useState<FlightsTab | "close" | null>(null);
  const tabRefs = useRef<Record<string, HTMLButtonElement | null>>({});
  const waiting = needsYouFlights(useFlights(open).data || []).length;
  const onDirty = useCallback((d: boolean) => setPlansDirty(d), []);

  useEffect(() => {
    if (!open) {
      setLeaving(null);
      setPlansDirty(false);
      return;
    }
    // Waiting to fly is Intake's lists: warm them on the way in (App's
    // useIntakeWarm normally has already).
    prefetchIntakePanels();
    prefetchIntakeMeta();
  }, [open]);

  // The Flights tour walks two tabs: follow its step's `screen`.
  const tourScreen = useUi((s) => (s.coachTour?.id === "flights" ? FLIGHTS_TOUR.steps[s.coachTour.step]?.screen || "" : ""));
  useEffect(() => {
    if (!open || !tourScreen || tourScreen === tab) return;
    if ((FLIGHTS_TABS as readonly string[]).includes(tourScreen)) openDialogFor("flights", tourScreen);
  }, [open, tourScreen, tab, openDialogFor]);

  if (!open) return null;

  const go = (next: FlightsTab, deep = "") => openDialogFor("flights", deep ? next + "/" + deep : next);
  // Unsaved work on the tab showing: the flyway editor's, or an embedded
  // surface's (Schedule's template editor registers through embedGuard).
  const unsaved = () => (tab === "plans" && plansDirty) || (tab === "schedule" && !!embeddedGuard.current?.dirty());
  const switchTab = (next: FlightsTab, deep = "") => {
    if (next === tab && !deep) return;
    if (unsaved()) {
      setLeaving(next);
      return;
    }
    go(next, deep);
  };
  const close = () => {
    if (unsaved()) {
      setLeaving("close");
      return;
    }
    closeDialog();
  };
  const onTabKey = (e: React.KeyboardEvent, i: number) => {
    const n = FLIGHTS_TABS.length;
    const to =
      e.key === "ArrowRight" ? (i + 1) % n : e.key === "ArrowLeft" ? (i - 1 + n) % n : e.key === "Home" ? 0 : e.key === "End" ? n - 1 : -1;
    if (to < 0) return;
    e.preventDefault();
    switchTab(FLIGHTS_TABS[to]);
    tabRefs.current[FLIGHTS_TABS[to]]?.focus();
  };

  return (
    <Modal
      id="flights-dialog"
      // The Sky fills the window (no card margins); every other tab is the
      // top-bar card (Modal.css).
      className={"top-dialog" + (tab === "sky" ? " sky-full" : "")}
      role="dialog"
      aria-modal="true"
      aria-labelledby="flights-title"
      onClose={close}
      // A press outside never throws away a half-edited flyway (close() asks).
      closeOnBackdrop
      onEscape={() => {
        if (leaving) {
          setLeaving(null);
          return true;
        }
        if (tab === "schedule" && embeddedGuard.current?.escape()) return true;
        if (unsaved()) {
          setLeaving("close");
          return true;
        }
        return false;
      }}
    >
      <div id="flights-panel">
        <div className="ws-head fl-head">
          <span className="fl-head-bird" aria-hidden="true">
            <Bird size={17} />
          </span>
          <h2 id="flights-title">Flights</h2>
          <span className="ik-subtitle">Every ticket, PR and scheduled job, from waiting to fly to landed</span>
          <TourButton tour="flights" />
          <button type="button" id="flights-close" onClick={close}>
            Close
          </button>
        </div>
        <div id="flights-tabs" className="fl-tabs" role="tablist" aria-label="Flights">
          {FLIGHTS_TABS.map((t, i) => (
            <button
              key={t}
              ref={(el) => {
                tabRefs.current[t] = el;
              }}
              type="button"
              role="tab"
              id={"flights-tab-" + t}
              aria-selected={tab === t}
              aria-controls={"flights-tabpanel-" + t}
              tabIndex={tab === t ? 0 : -1}
              className={"fl-tab" + (tab === t ? " active" : "")}
              data-flights-tab={t}
              onClick={() => switchTab(t)}
              onKeyDown={(e) => onTabKey(e, i)}
            >
              {FLIGHTS_TAB_LABEL[t]}
              {t === "sky" && waiting > 0 && <span className="fl-count fl-count-you">{waiting}</span>}
            </button>
          ))}
        </div>
        {leaving && (
          <InlineConfirm
            id="fl-leave-confirm"
            title="Discard unsaved changes?"
            body="They haven't been saved."
            confirmLabel="Discard"
            cancelLabel="Keep editing"
            onCancel={() => setLeaving(null)}
            onConfirm={() => {
              const to = leaving;
              setLeaving(null);
              setPlansDirty(false);
              if (to === "close") closeDialog();
              else go(to);
            }}
          />
        )}
        <div id="flights-body" className={"fl-body fl-body-" + tab}>
          <section key={tab} id={"flights-tabpanel-" + tab} className="fl-panel" role="tabpanel" aria-labelledby={"flights-tab-" + tab}>
            {tab === "sky" && <Sky focus={rest} onTab={(t, deep) => switchTab(t, deep)} />}
            {tab === "plans" && <Plans initialPlan={rest} onDirty={onDirty} />}
            {tab === "sources" && <IntakeDialog embedded target={rest || null} />}
            {tab === "schedule" && <TemplatesDialog embedded target={rest || null} />}
            {tab === "checklists" && <VerifyDialog embedded target={rest || null} />}
          </section>
        </div>
      </div>
    </Modal>
  );
}
