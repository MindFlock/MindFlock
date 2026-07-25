/** A dismissible getting-started hint: a small inline callout that guides new
 * users toward a feature. Renders nothing once the master switch is off or the
 * user has dismissed this specific hint. Each hint needs a stable `id` — that
 * key is what gets remembered as dismissed (and re-armed when hints are turned
 * back on in Settings → General).
 *
 * Hints are inline (they sit in the normal layout flow, nudging siblings) not
 * floating overlays — that keeps them robust across resize/scroll and reads as
 * a natural part of the UI rather than a modal tour. */

import type { ReactNode } from "react";
import { useUi } from "../../state/store";
import "./Hint.css";

interface Props {
  /** Stable key remembered as dismissed. */
  id: string;
  /** The hint body. */
  children: ReactNode;
  /** Optional call-to-action button (e.g. "Open Settings"). */
  action?: { label: string; onClick(): void };
  /** Extra class for placement tweaks. */
  className?: string;
}

export function Hint({ id, children, action, className }: Props) {
  const enabled = useUi((s) => s.hintsEnabled);
  const dismissed = useUi((s) => s.dismissedHints.has(id));
  const dismissHint = useUi((s) => s.dismissHint);

  if (!enabled || dismissed) return null;

  return (
    <div className={"hint" + (className ? " " + className : "")} role="note">
      <span className="hint-icon" aria-hidden="true">💡</span>
      <div className="hint-body">
        <div className="hint-text">{children}</div>
        {action && (
          <button
            type="button"
            className="hint-action"
            onClick={() => {
              action.onClick();
              dismissHint(id);
            }}
          >
            {action.label}
          </button>
        )}
      </div>
      <button
        type="button"
        className="hint-dismiss"
        title="Dismiss this hint"
        aria-label="Dismiss hint"
        onClick={() => dismissHint(id)}
      >
        ✕
      </button>
    </div>
  );
}
