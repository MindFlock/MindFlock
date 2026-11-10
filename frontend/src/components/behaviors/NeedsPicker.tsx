/** "After": which EARLIER behaviors this one relies on (a step's `needs`).
 * The walker does those first, and answers this one blocked when one of them
 * failed. One toggle per earlier behavior, numbered as the card shows them;
 * nothing renders when there is no earlier behavior to rely on. */

import type { JSX } from "react";
import type { TestStep } from "../../api/types";

export function NeedsPicker({
  earlier,
  value,
  disabled,
  onChange,
}: {
  earlier: ReadonlyArray<{ step: TestStep; n: number }>;
  value: readonly string[];
  disabled?: boolean;
  onChange(next: string[]): void;
}): JSX.Element | null {
  if (!earlier.length) return null;
  const on = new Set(value);
  return (
    <span className="bh-needs-pick" role="group" aria-label="Relies on (done first)">
      <span className="bh-hint">after</span>
      {earlier.map(({ step, n }) => {
        const pressed = on.has(step.id);
        return (
          <button
            key={step.id}
            type="button"
            className={"bh-need" + (pressed ? " is-on" : "")}
            aria-pressed={pressed}
            disabled={disabled}
            title={"Relies on behavior " + n + ": " + step.text}
            onClick={() =>
              // Kept in plan order, whatever order they were pressed in.
              onChange(earlier.map((e) => e.step.id).filter((id) => (id === step.id ? !pressed : on.has(id))))
            }
          >
            #{n}
          </button>
        );
      })}
    </span>
  );
}
