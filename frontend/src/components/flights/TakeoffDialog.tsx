/** "Takes off…" on a saved stop (the shelf chip's right-click menu): where
 * its flights come from — by hand, from a ticket / PR / issue, on a
 * **Schedule** (days as chips, the time / date / cron as fields, the server's
 * preview of the next run, Paused), or, round 15, **when main moves** /
 * **when main is red** (the repository watched, quiet minutes) / **when a
 * hook fires** (the address to POST to, with Copy, once saved) — and, for
 * the ones that create their own session (by hand, scheduled, the triggers),
 * the repository, agent and model it starts in. A dialog (`Modal`), saved with `savePlan`; a refusal stays in the
 * dialog (Electron has no alert). It was the flyway card's trigger section
 * (FlywayTrigger); a scheduled saved stop is edited here ("Edit in the Sky"
 * from Schedule). */

import { useEffect, useState, type JSX } from "react";
import { cronOf, cronPreview, onceOf, type CronPreview } from "../../lib/templates";
import { MAIN_TRIGGERS, TRIGGER_LABEL, hookUrl, scheduleOfWhen, triggerMakesSession, whenFormOf, type FlightPlan, type TriggerKind, type WhenForm } from "../../lib/flights";
import { savePlan } from "../../lib/flightsApi";
import { errMsg } from "../../lib/format";
import { toast } from "../../lib/toast";
import { Modal } from "../Modal";
import { PathField } from "../PathField";
import { useAgentChoices } from "../settings/screens/AgentPicker";
import { cronPreviewLine } from "../dialogs/TemplateEditor";
import { ChipRow, FlField, NumberField, Seg, TextField, Toggle } from "./controls";
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

/** The hook's address, read-only, with Copy (once the plan is saved: the
 * server mints the token then). */
export function HookUrlRow({ plan }: { plan: FlightPlan }): JSX.Element {
  const url = hookUrl(plan.trigger);
  const [copied, setCopied] = useState(false);
  useEffect(() => {
    if (!copied) return;
    const t = setTimeout(() => setCopied(false), 1500);
    return () => clearTimeout(t);
  }, [copied]);
  if (!url) {
    return <span className="fl-fld-hint fl-hook-pending">Save to get the address outside systems POST to.</span>;
  }
  const copy = () => {
    const p = typeof navigator !== "undefined" && navigator.clipboard ? navigator.clipboard.writeText(url) : Promise.reject(new Error("no clipboard"));
    p.then(
      () => {
        setCopied(true);
        toast("Copied the hook address");
      },
      () => toast("Couldn't copy — select the address and copy it")
    );
  };
  return (
    <span className="fl-hook-url">
      <input type="text" className="fl-input fl-mono" aria-label="Hook address" value={url} readOnly onFocus={(e) => e.currentTarget.select()} />
      <button type="button" className="test-btn fl-hook-copy" onClick={copy} title="Copy the address">
        {copied ? "Copied" : "Copy"}
      </button>
    </span>
  );
}

/** Where a saved stop's flights come from, and what they fly in. */
export function TakeoffFields({ plan, onChange }: { plan: FlightPlan; onChange(p: Partial<FlightPlan>): void }): JSX.Element {
  const agents = useAgentChoices();
  const preview = useCronPreview(triggerCron(plan));
  const when = whenFormOf(plan.trigger.schedule);
  const needsSession = triggerMakesSession(plan.trigger.kind);
  const watchesMain = MAIN_TRIGGERS.includes(plan.trigger.kind);
  const isHook = plan.trigger.kind === "hook";
  const quiet = plan.trigger.quiet_minutes ?? 60;
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
        {watchesMain && (
          <FlField label="Watches" wide hint={plan.trigger.kind === "main_push" ? "Its default branch (origin/HEAD, else main) is fetched every minute; a new tip takes off." : "The checks of its default branch's tip, once per commit; red takes off."}>
            <PathField
              id="fl-trigger-repo"
              value={plan.trigger.repo || ""}
              placeholder="An absolute path to a git repository"
              pickerTitle="The repository whose main branch this watches"
              onChange={(v) => onChange({ trigger: { ...plan.trigger, repo: v } })}
            />
          </FlField>
        )}
        {isHook && (
          <FlField label="Hook" wide hint="POST anything here — Grafana, Slack, CI; a JSON title or text (else the first 80 characters) names the flight.">
            <HookUrlRow plan={plan} />
          </FlField>
        )}
        {(watchesMain || isHook) && (
          <>
            <FlField label="Quiet" hint="No second take-off inside this many minutes">
              <NumberField label="Quiet minutes" unit="min" value={quiet} onChange={(v) => onChange({ trigger: { ...plan.trigger, quiet_minutes: v === "" ? 0 : v } })} />
            </FlField>
            <FlField label="Paused">
              <Toggle
                on={plan.trigger.paused}
                onChange={(paused) => onChange({ trigger: { ...plan.trigger, paused } })}
                label="Paused"
                title="A paused trigger takes off nothing"
              />
            </FlField>
          </>
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
          <button type="button" className="test-btn" onClick={onClose}>
            Cancel
          </button>
          <button type="submit" className="btn-primary" disabled={busy}>
            {busy ? "Saving…" : "Save"}
          </button>
        </div>
      </form>
    </Modal>
  );
}
