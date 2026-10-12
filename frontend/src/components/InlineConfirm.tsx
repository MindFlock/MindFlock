/** The app's one inline confirmation row. Lives in components/ (not
 * settings/) because it is a primitive, not a Settings screen; its look is the
 * `.set-confirm` rules in settings/screens/screens.css. */

import type React from "react";

/** "Are you sure?" drawn in the app's own paint, directly under the control
 * that asked.
 *
 * The desktop app has no `window.confirm` — Electron implements it as a no-op
 * that answers false — so the guarded actions in Settings (turning the token
 * gate off, regenerating the token, removing an account sessions still use)
 * simply never happened there, with no sign why. A row driven by state works
 * in every host, and it can show the consequence next to the thing it is
 * about instead of in a box the OS draws over everything.
 *
 * Focus lands on Cancel: the safe answer is the one Enter gives. */
export function InlineConfirm(props: {
  /** One line: what is about to happen. */
  title: string;
  /** The consequence, in a sentence or two. */
  body: React.ReactNode;
  /** The verb on the go-ahead button ("Turn it off", "Regenerate", …). */
  confirmLabel: string;
  busy?: boolean;
  onConfirm(): void;
  onCancel(): void;
  id?: string;
}) {
  return (
    <div className="set-confirm" role="alertdialog" aria-label={props.title} id={props.id}>
      <p className="set-confirm-text">
        <strong>{props.title}</strong> {props.body}
      </p>
      <div className="set-confirm-actions">
        <button
          type="button"
          className="test-btn set-confirm-yes"
          disabled={props.busy}
          onClick={props.onConfirm}
        >
          {props.confirmLabel}
        </button>
        <button
          type="button"
          className="test-btn"
          autoFocus
          disabled={props.busy}
          onClick={props.onCancel}
        >
          Cancel
        </button>
      </div>
    </div>
  );
}
