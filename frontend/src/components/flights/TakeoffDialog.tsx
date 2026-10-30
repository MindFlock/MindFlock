/** "Takes off…" on a saved stop (the shelf chip's right-click menu): where
 * its flights come from — by hand, from a ticket / PR / issue, or on a
 * **Schedule** (days as chips, the time / date / cron as fields, the server's
 * preview of the next run, Paused) — and, for the ones that create their own
 * session (by hand, scheduled), the repository, agent and model it starts
 * in. A dialog (`Modal`), saved with `savePlan`; a refusal stays in the
 * dialog (Electron has no alert). It was the flyway card's trigger section
 * (FlywayTrigger); a scheduled saved stop is edited here ("Edit in the Sky"
 * from Schedule). */

import { useEffect, useState, type JSX } from "react";
import { cronOf, cronPreview, onceOf, type CronPreview } from "../../lib/templates";
import { TRIGGER_LABEL, scheduleOfWhen, whenFormOf, type FlightPlan, type TriggerKind, type WhenForm } from "../../lib/flights";
import { savePlan } from "../../lib/flightsApi";
import { errMsg } from "../../lib/format";
import { toast } from "../../lib/toast";
import { Modal } from "../Modal";
import { PathField } from "../PathField";
import { useAgentChoices } from "../settings/screens/AgentPicker";
import { cronPreviewLine } from "../dialogs/TemplateEditor";
import { ChipRow, FlField, Seg, TextField, Toggle } from "./controls";
import "./StopParts.css";
import "./TakeoffDialog.css";

const DAYS: ReadonlyArray<{ v: WhenForm["days"]; label: string }> = [
  { v: "daily", label: "Every day" },
  { v: "weekdays", label: "Weekdays" },
  { v: "once", label: "Once" },
  { v: "cron", label: "Custom" },
];

/** The cron text the server previews for a trigger ("" = not scheduled). */
export function triggerCron(plan: Pick<FlightPlan, "trigger">): string {
  if (plan.trigger.kind !== "schedule") return "";
  const when = whenFormOf(plan.trigger.schedule);
  return (when.days === "cron" ? when.cron : when.days === "once" ? onceOf(when.date, when.at) : cronOf(when.at, when.days)).trim();
}

/** The server's preview of a cron, fetched as it is typed (debounced). */
function useCronPreview(cron: string): CronPreview | null {
  const [preview, setPreview] = useState<CronPreview | null>(null);
  useEffect(() => {
    if (!cron) {
      setPreview(null);
      return;
    }
    let live = true;
    const t = setTimeout(() => {
      cronPreview(cron)
        .then((pv) => live && setPreview(pv))
        .catch(() => live && setPreview(null));
    }, 300);
    return () => {
      live = false;
      clearTimeout(t);
    };
  }, [cron]);
  return preview;
}

/** A schedule's when: days as chips, the time / date / cron as fields, and
 * the server's preview of the next run under them. */
export function ScheduleChips({ when, onWhen, preview }: { when: WhenForm; onWhen(w: WhenForm): void; preview: CronPreview | null }): JSX.Element {
  const line = cronPreviewLine(preview, new Date());
  return (
    <div className="fl-schedule">
      <Seg label="Which days" value={when.days} options={DAYS} onChange={(days) => onWhen({ ...when, days })} />
      {when.days === "cron" ? (
        <TextField label="Cron expression" mono value={when.cron} placeholder="0 21 * * 1-5" onChange={(cron) => onWhen({ ...when, cron })} />
      ) : (
        <span className="fl-schedule-at">
          {when.days === "once" && (
            <input
              type="date"
              className="fl-input fl-date"
              aria-label="Date"
              value={when.date}
              onChange={(e) => onWhen({ ...when, date: e.target.value })}
            />
          )}
          <span className="muted">at</span>
          <input type="time" className="fl-input fl-time" aria-label="Time" value={when.at} onChange={(e) => onWhen({ ...when, at: e.target.value })} />
        </span>
      )}
      {line && <span className="fl-fld-hint">{line}</span>}
    </div>
  );
}

/** Where a saved stop's flights come from, and what they fly in. */
export function TakeoffFields({ plan, onChange }: { plan: FlightPlan; onChange(p: Partial<FlightPlan>): void }): JSX.Element {
  const agents = useAgentChoices();
  const preview = useCronPreview(triggerCron(plan));
  const when = whenFormOf(plan.trigger.schedule);
  const needsSession = plan.trigger.kind === "manual" || plan.trigger.kind === "schedule";
  return (
    <section className="fl-section fl-trigger" aria-label="Where its flights come from">
      <div className="fl-form">
        <FlField label="Takes off" wide>
          <Seg
            label="Takes off"
            value={plan.trigger.kind}
            options={(Object.keys(TRIGGER_LABEL) as TriggerKind[]).map((k) => ({ v: k, label: TRIGGER_LABEL[k] }))}
            onChange={(kind) =>
              onChange({
                trigger: {
                  ...plan.trigger,
                  kind,
                  schedule: kind === "schedule" ? plan.trigger.schedule || { at: "21:00", days: "daily" } : null,
                },
              })
            }
          />
        </FlField>
        {plan.trigger.kind === "schedule" && (
          <FlField label="When" wide>
            <ScheduleChips when={when} preview={preview} onWhen={(w) => onChange({ trigger: { ...plan.trigger, schedule: scheduleOfWhen(w) } })} />
            <Toggle
              on={plan.trigger.paused}
              onChange={(paused) => onChange({ trigger: { ...plan.trigger, paused } })}
              label="Paused"
              title="A paused schedule takes off nothing"
            />
          </FlField>
        )}
        {needsSession && (
          <>
            <FlField label="Repository" wide>
              <PathField
                id="fl-plan-repo"
                value={plan.session.repo_path}
                placeholder="Ask when it takes off"
                pickerTitle="The repository these stops' sessions start in"
                onChange={(v) => onChange({ session: { ...plan.session, repo_path: v } })}
              />
            </FlField>
            <FlField label="Agent" wide>
              <ChipRow
                label="Agent"
                value={plan.session.program}
                options={[
                  { value: "", label: agents.fallback ? "Default · " + agents.fallback : "Default" },
                  ...agents.names.map((n) => ({ value: n, label: n })),
                ]}
                onChange={(program) => onChange({ session: { ...plan.session, program } })}
              />
            </FlField>
            <FlField label="Model">
              <TextField
                label="Model"
                value={plan.session.model}
                placeholder="The agent's default"
                onChange={(model) => onChange({ session: { ...plan.session, model } })}
              />
            </FlField>
          </>
        )}
      </div>
    </section>
  );
}

/** The dialog: the saved stop's Takes off, Save / Cancel. */
export function TakeoffDialog({ plan, onClose }: { plan: FlightPlan; onClose(): void }): JSX.Element {
  const [draft, setDraft] = useState<FlightPlan>(() => JSON.parse(JSON.stringify(plan)) as FlightPlan);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const dirty = JSON.stringify({ ...draft, updated: 0 }) !== JSON.stringify({ ...plan, updated: 0 });

  const save = async () => {
    if (!dirty) return onClose();
    setBusy(true);
    setError("");
    try {
      const saved = await savePlan(draft);
      toast("Saved “" + saved.name + "”");
      onClose();
    } catch (err) {
      setError(errMsg(err));
      setBusy(false);
    }
  };

  return (
    <Modal id="takeoff-dialog" onClose={onClose}>
      <form
        className="fl-takeoff"
        onSubmit={(e) => {
          e.preventDefault();
          void save();
        }}
      >
        <h2>“{plan.name}” takes off</h2>
        <p className="muted fl-takeoff-lede">When it flies by itself, and the session it starts in.</p>
        <TakeoffFields plan={draft} onChange={(p) => setDraft((d) => ({ ...d, ...p }))} />
        {error && (
          <p className="error fl-error" role="alert">
            {error}
          </p>
        )}
        <div className="modal-actions">
          <button type="button" onClick={onClose}>
            Cancel
          </button>
          <button type="submit" disabled={busy}>
            {busy ? "Saving…" : "Save"}
          </button>
        </div>
      </form>
    </Modal>
  );
}
