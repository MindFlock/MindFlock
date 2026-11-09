/** What's new — the window that explains a release's features, once, after
 * an update to a release with big ones (docs/vision/whats-new.md).
 *
 * By itself: at page load, once the running version, the person's prefs and
 * the config are in, lib/whatsNew planWhatsNew decides — a fresh install
 * marks the version seen and shows nothing (the welcome tour is theirs); an
 * update with an unseen big entry opens this, but only once onboarding is
 * done and nothing else is on screen (no welcome card, no coach-mark tour, no
 * dialog), so it never covers the welcome tour. It shares the screen with
 * the ask pop-up as a queue (lib/whatsNew popupTurn): whichever opened first
 * stays and the other waits for it to close; at page load this goes first —
 * the ask waits while it is deciding (`auto`, seen.ts) or due.
 *
 * By hand: openWhatsNew() — the palette's "What's new", Settings → Help, a
 * click on the version in the top bar — shows the latest release's entries.
 *
 * "Got it", ×, Esc and the backdrop all record the running version as seen
 * (components/whatsNew/seen.ts: the synced prefs + this browser). A
 * feature's "Show me" closes the window the same way, then runs its action. */

import { useEffect, useRef, useState } from "react";
import { useConfig, useDoctor } from "../../state/queries";
import { useUi } from "../../state/store";
import { firstPrefsPull } from "../../lib/prefsSync";
import {
  isReleaseVersion,
  knownAction,
  laterVersion,
  planWhatsNew,
  popupTurn,
  reopenEntries,
  splitEntries,
  whatsNewTitle,
} from "../../lib/whatsNew";
import { RELEASES, type WhatsNewFeature, type WhatsNewRelease } from "../../whatsNew/releases";
import { Modal } from "../Modal";
import { isKnownAction, runWhatsNewAction } from "./actions";
import { loadSeen, saveSeen, useWhatsNew } from "./seen";
import "./WhatsNew.css";

/** Open What's new by hand: the latest release's entries. Closes whatever
 * dialog it was opened from (Settings → Help), so a "Show me" lands clean. */
export function openWhatsNew(): void {
  const entries = reopenEntries(RELEASES, useWhatsNew.getState().running);
  if (!entries.length) return;
  useUi.getState().closeDialog();
  useWhatsNew.getState().open(entries);
}

/** Close it, recording the running version as seen (never lower than the
 * seen one: a downgrade doesn't re-arm a newer release). */
function closeWhatsNew(): void {
  const s = useWhatsNew.getState();
  const seen = laterVersion(s.running, s.seen);
  if (seen) {
    saveSeen(seen);
    useWhatsNew.setState({ seen });
  }
  // Closed: the automatic opening is spent (a pending one never comes back).
  useWhatsNew.setState({ auto: "done" });
  s.close();
}

/** The automatic opening (see the header). */
function useAutoWhatsNew() {
  const rawVersion = useDoctor().data?.version;
  const { data: config } = useConfig();
  const welcomeSeen = useUi((s) => s.welcomeTourSeen);
  const touring = useUi((s) => s.tourOpen || !!s.coachTour);
  const dialog = useUi((s) => s.openDialog);
  const onboarded = welcomeSeen || config?.onboarded === true;
  const running = isReleaseVersion(rawVersion) ? rawVersion!.trim() : "";
  const [pulled, setPulled] = useState(false);
  const [pending, setPending] = useState<WhatsNewRelease[] | null>(null);
  const decided = useRef(false);

  useEffect(() => {
    let live = true;
    void firstPrefsPull.then(() => live && setPulled(true));
    return () => {
      live = false;
    };
  }, []);

  useEffect(() => {
    if (running) useWhatsNew.setState({ running });
  }, [running]);

  // A source tree (no release version): nothing will pop — the ask pop-up
  // needn't wait for a decision.
  useEffect(() => {
    if (rawVersion !== undefined && !running && !decided.current) useWhatsNew.setState({ auto: "done" });
  }, [rawVersion, running]);

  useEffect(() => {
    if (decided.current || !pulled || !running || !config) return;
    let live = true;
    void loadSeen().then((seen) => {
      if (!live || decided.current) return;
      useWhatsNew.setState({ seen });
      const plan = planWhatsNew({ releases: RELEASES, running, lastSeen: seen, onboarded });
      if (plan.markSeen) {
        decided.current = true;
        saveSeen(plan.markSeen);
        useWhatsNew.setState({ seen: plan.markSeen, auto: "done" });
        return;
      }
      if (!plan.pop) {
        decided.current = true;
        useWhatsNew.setState({ auto: "done" });
        return;
      }
      // Seen on another device but not past first-run here: wait for
      // onboarding (this runs again when it flips).
      if (!onboarded) return;
      decided.current = true;
      useWhatsNew.setState({ auto: "due" });
      setPending(plan.entries);
    });
    return () => {
      live = false;
    };
  }, [pulled, running, config, onboarded]);

  // Its turn in the pop-up queue: an ask pop-up already open stays, and this
  // opens when it closes (lib/whatsNew popupTurn).
  useEffect(() => {
    const st = useWhatsNew.getState();
    if (!pending || st.shown || st.auto !== "due") {
      if (pending && st.auto !== "due") setPending(null);
      return;
    }
    const turn = popupTurn({
      whatsNew: "due",
      askOpen: dialog === "ask",
      askWaiting: false,
      otherOpen: touring || (!!dialog && dialog !== "ask"),
      sinceLoadMs: 0,
    });
    if (turn !== "whats-new") return;
    st.open(pending);
    useWhatsNew.setState({ auto: "done" });
    setPending(null);
  }, [pending, touring, dialog]);
}

function FeatureRow({ f }: { f: WhatsNewFeature }) {
  const action = knownAction(f, isKnownAction);
  return (
    <li className="wn-feature">
      <div className="wn-feature-text">
        <span className="wn-feature-title">{f.title}</span>
        <span className="wn-feature-body">{f.body}</span>
      </div>
      {action && (
        <button
          type="button"
          className="wn-show"
          onClick={() => {
            closeWhatsNew();
            runWhatsNewAction(action.run);
          }}
        >
          {action.label}
        </button>
      )}
    </li>
  );
}

const STAR_URL = "https://github.com/MindFlock/MindFlock";

export function WhatsNew() {
  useAutoWhatsNew();
  const shown = useWhatsNew((s) => s.shown);
  if (!shown || !shown.length) return null;
  const { big, small } = splitEntries(shown);
  const many = big.length > 1;
  return (
    <Modal id="whats-new" portal role="dialog" aria-modal="true" aria-labelledby="wn-title" onClose={closeWhatsNew}>
      <div className="wn-card">
        <button type="button" className="wn-x" aria-label="Close" title="Close" onClick={closeWhatsNew}>
          ×
        </button>
        <h2 className="wn-title" id="wn-title">
          {whatsNewTitle(shown)}
        </h2>
        <div className="wn-body">
          {(big.length ? big : small.slice(0, 1)).map((r) => (
            <section className="wn-release" key={r.version}>
              {many && <h3 className="wn-version">{r.version}</h3>}
              <p className="wn-headline">{r.headline}</p>
              <ul className="wn-features">
                {r.features.map((f) => (
                  <FeatureRow key={f.title} f={f} />
                ))}
              </ul>
            </section>
          ))}
          {big.length > 0 && small.length > 0 && (
            <details className="wn-also">
              <summary>Also in this release</summary>
              {small.map((r) => (
                <section className="wn-release" key={r.version}>
                  <h4 className="wn-version">{r.version}</h4>
                  <ul className="wn-features">
                    {r.features.map((f) => (
                      <FeatureRow key={f.title} f={f} />
                    ))}
                  </ul>
                </section>
              ))}
            </details>
          )}
        </div>
        <div className="wn-actions">
          {/* The one place the app asks for anything: stars are how a solo
              open-source project learns whether to keep going, and this
              window only reaches people who already chose to update. */}
          <a className="wn-star" href={STAR_URL} target="_blank" rel="noreferrer">
            Finding MindFlock useful? A star on GitHub helps.
          </a>
          <button type="button" className="wn-primary" onClick={closeWhatsNew} autoFocus>
            Got it
          </button>
        </div>
      </div>
    </Modal>
  );
}
