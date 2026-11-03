/** "Takes off…" on a saved stop (the shelf chip's right-click menu, and a
 * scheduled one's **Edit** in Schedule): where its flights come from — by
 * hand, from a ticket / PR / issue, on a **Schedule** (THE schedule editor,
 * TemplateEditor's ScheduleWhen — Every day / Weekdays / Once / Custom (cron),
 * a 24-hour time, the server's preview of the next runs — the very fields a
 * template's Schedule… shows, then Paused), or, round 15, **when main moves** /
 * **when main is red** (the repository watched, quiet minutes) / **when a
 * hook fires** (the address to POST to, with Copy, once saved) — and, for
 * the ones that create their own session (by hand, scheduled, the triggers),
 * the repository, agent and model it starts in. A dialog (`Modal`), saved with `savePlan`; a refusal stays in the
 * dialog (Electron has no alert). It was the flyway card's trigger section
 * (FlywayTrigger). */

import { useEffect, useState, type JSX } from "react";
import { cronOf, cronPreview, onceOf, type CronPreview, type TemplateRunsStatus } from "../../lib/templates";
import { MAIN_TRIGGERS, TRIGGER_LABEL, hookUrl, scheduleOfWhen, triggerMakesSession, whenFormOf, type FlightPlan, type TriggerKind, type WhenForm } from "../../lib/flights";
import { savePlan } from "../../lib/flightsApi";
import { errMsg } from "../../lib/format";
import { toast } from "../../lib/toast";
import { Modal } from "../Modal";
import { PathField } from "../PathField";
import { useAgentChoices } from "../settings/screens/AgentPicker";
import { ScheduleWhen } from "../dialogs/TemplateEditor";
import { ChipRow, FlField, NumberField, Seg, TextField, Toggle } from "./controls";
import "./StopParts.css";
import "./TakeoffDialog.css";

/** The cron text the server previews for a schedule's fields. */
function whenCron(when: WhenForm): string {
  return (when.days === "cron" ? when.cron : when.days === "once" ? onceOf(when.date, when.at) : cronOf(when.at, when.days)).trim();
}

/** The cron text the server previews for a trigger ("" = not scheduled). */
export function triggerCron(plan: Pick<FlightPlan, "trigger">): string {
  if (plan.trigger.kind !== "schedule") return "";
  return whenCron(whenFormOf(plan.trigger.schedule));
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
  // The schedule's fields are the form's own (seeded once from the stored
  // schedule): a half-typed cron or a cleared time is not a schedule yet,
  // and re-deriving the fields from what it saves would flip the mode back.
  const [when, setWhen] = useState<WhenForm>(() => whenFormOf(plan.trigger.schedule));
  const preview = useCronPreview(plan.trigger.kind === "schedule" ? whenCron(when) : "");
  // A scheduled saved stop's run record (GET /api/flights/plans `runs`):
  // the zone its time is in, and where schedules fire.
  const runs = (plan as FlightPlan & { runs?: { tz?: string } & Partial<Pick<TemplateRunsStatus, "runs_here" | "runs_on">> }).runs;
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
            <div className="tpl-editor tpl-sched-editor fl-schedule-when">
              <ScheduleWhen
                form={when}
                cronPreview={preview}
                tz={runs?.tz || ""}
                runs={runs && typeof runs.runs_here === "boolean" ? { runs_here: runs.runs_here, runs_on: runs.runs_on || "" } : undefined}
                onForm={(patch) => {
                  const w = { ...when, ...patch };
                  setWhen(w);
                  onChange({ trigger: { ...plan.trigger, schedule: scheduleOfWhen(w) } });
                }}
              />
            </div>
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
    <Modal id="takeoff-dialog" portal onClose={onClose}>
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
