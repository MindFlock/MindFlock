/** The fast-track choice as a dialog draws it: the same rungs and the same
 * "Ask me before it ships" as the ⏩ picker (lib/laneActions owns the words),
 * laid out as a segmented control for a form. The New dialog ("Fast-track
 * to" / "Fast-track each to" — Intake's "Start together" opens New) and the
 * Commit dialog ("Then fast-track to") both draw this one component, so a
 * rung can never read differently in two places. */

import {
  ASK_FIRST_DESC,
  ASK_FIRST_LABEL,
  LANE_CHOICES,
  LANE_DESC,
  LANE_LABEL,
  askFirstApplies,
  type Lane,
} from "../../lib/laneActions";

/** A small segmented control: one pressed button out of a few. */
export function Seg<T extends string>({
  value,
  options,
  onChange,
  label,
  id,
}: {
  value: T;
  options: ReadonlyArray<{ v: T; label: string; title?: string; disabled?: boolean }>;
  onChange(v: T): void;
  label: string;
  id?: string;
}) {
  return (
    <div className="rt-seg" role="group" aria-label={label} id={id}>
      {options.map((o) => (
        <button
          key={o.v}
          type="button"
          className={o.v === value ? "on" : undefined}
          aria-pressed={o.v === value}
          title={o.title}
          disabled={o.disabled || undefined}
          onClick={() => onChange(o.v)}
        >
          {o.label}
        </button>
      ))}
    </div>
  );
}

export function FastTrackChoice({
  id,
  label,
  value,
  onChange,
  askFirst,
  onAskFirst,
  lanes = LANE_CHOICES,
  laneLabel,
  laneTitle,
  disabledReason,
  askReason,
}: {
  /** The segmented control's id; the checkbox is `${id}-ask`. */
  id: string;
  /** Read out for the group ("Fast-track to"). */
  label: string;
  value: Lane;
  onChange(l: Lane): void;
  askFirst: boolean;
  onAskFirst(on: boolean): void;
  /** The rungs this place offers (default: all five). */
  lanes?: readonly Lane[];
  /** A label a context words differently (the Commit dialog's Off). */
  laneLabel?: Partial<Record<Lane, string>>;
  /** A tooltip a context words differently. */
  laneTitle?: Partial<Record<Lane, string>>;
  /** Why a rung can't be chosen here (shown on it, greyed). */
  disabledReason?: Partial<Record<Lane, string>>;
  /** Why "ask me first" can't be used here, overriding the Off rule. */
  askReason?: string;
}) {
  const askWhy =
    askReason || (askFirstApplies(value) ? "" : "Nothing ships while fast-track is off");
  return (
    <>
      <Seg
        id={id}
        label={label}
        value={value}
        options={lanes.map((l) => ({
          v: l,
          label: laneLabel?.[l] || LANE_LABEL[l],
          title: disabledReason?.[l] || laneTitle?.[l] || LANE_DESC[l],
          disabled: !!disabledReason?.[l],
        }))}
        onChange={onChange}
      />
      <label
        className={"check rt-ask" + (askWhy ? " disabled" : "")}
        title={askWhy || ASK_FIRST_DESC}
      >
        <input
          type="checkbox"
          id={id + "-ask"}
          checked={!askWhy && askFirst}
          disabled={!!askWhy}
          onChange={(e) => onAskFirst(e.target.checked)}
        />
        {ASK_FIRST_LABEL}
      </label>
    </>
  );
}
