/** The New form's Templates strip: one server-side list (managed in
 * TemplatesDialog), and the fill that writes a template into the form. */

import type { Dispatch, SetStateAction } from "react";
import {
  hasSchedule,
  runInfoFor,
  runProblem,
  templateScheduleLine,
  templatesTarget,
  type Template,
} from "../../../lib/templates";
import { useUi } from "../../../state/store";
import type { FolderAction } from "../newSession";
import type { Lane } from "../../../lib/laneActions";

/** A template as the server stores it, fast-track included (lib/templates'
 * Template predates the field). */

const LANES: readonly string[] = ["leave", "commit", "push", "pr", "merge"];

type Set<T> = Dispatch<SetStateAction<T>>;

export interface TemplateFillCtx {
  setAgent(value: string): void;
  folderDo: Dispatch<FolderAction>;
  setPrompt: Set<string>;
  setProvision: Set<boolean>;
  setStrategy: Set<string>;
  setInPlace: Set<boolean>;
  setInitRepo: Set<boolean>;
  setActiveTemplate: Set<string>;
  setAdvancedOpen: Set<boolean>;
  /** The template's fast-track ("" / "leave" = off). */
  setLane(lane: Lane): void;
  setAskFirst(on: boolean): void;
}

/** fillFromTemplate, built per render from the form it writes into. */
export function templateFiller(ctx: TemplateFillCtx) {
  const {
    setAgent,
    folderDo,
    setPrompt,
    setProvision,
    setStrategy,
    setInPlace,
    setInitRepo,
    setActiveTemplate,
    setAdvancedOpen,
    setLane,
    setAskFirst,
  } = ctx;
  /** Put a template into the form. `quiet` is the opening's own pre-fill
   * (the last-used template): the fields change, nothing else does — no
   * caret move, no fold opened, no title written over the one the person is
   * about to type. A click on a chip is the loud version. */
  const fillFromTemplate = (t: Template, opts: { quiet?: boolean } = {}) => {
    if (t.program) setAgent(t.program);
    if (t.repo_path) folderDo({ t: "user-set", path: t.repo_path });
    if (t.prompt) setPrompt(t.prompt);
    setProvision(!!t.provisioned);
    if (t.workspace_strategy) setStrategy(t.workspace_strategy);
    // A provisioned template's in_place is dead on arrival — the server drops it
    // (`in_place = … and not is_provisioned`) — and taking it at face value here
    // would tick the two boxes that now exclude each other, leaving the fold in
    // a state the user cannot get out of and the form claiming a mode the
    // session will not run in. init_repo, by contrast, is now free to arrive
    // alongside either: git-initialising a folder and working directly in it is
    // an ordinary combination.
    setInPlace(!!t.in_place && !t.provisioned);
    setInitRepo(!!t.init_repo);
    const ft = t.autopilot;
    setLane(ft && LANES.includes(ft as Lane) ? (ft as Lane) : "leave");
    setAskFirst(false);
    setActiveTemplate(t.name);
    if (opts.quiet) return;
    // The extras it set live under More options: show them.
    setAdvancedOpen(!!(t.provisioned || t.init_repo || ft));
    // The Name is NOT written: a session from a template is named from its
    // task (or the Name you type), so two runs never collide on the
    // template's own name.
  };
  return fillFromTemplate;
}

/** What a scheduled chip's ⏰ says on its face: the time it starts ("07:30"),
 * "paused", or "on" for a cron with no single time. "" = not scheduled. */
export function chipScheduleText(t: Template, line = templateScheduleLine(t)): string {
  if (!hasSchedule(t)) return "";
  if (t.schedule?.paused || runInfoFor(t.name).paused) return "paused";
  const hm = /\b\d\d:\d\d\b/.exec(line || "") || /\b\d\d:\d\d\b/.exec(t.schedule?.at || "");
  return hm ? hm[0] : "on";
}

export function TemplatesStrip({
  templates,
  activeTemplate,
  onPick,
  onRun,
  onSchedule,
  landing,
}: {
  templates: Template[];
  activeTemplate: string;
  /** A click on a template's name: fill the form from it. */
  onPick(t: Template): void;
  /** ▶: start a new session from it now (absent = not offered, e.g. the
   * session would live on another device). */
  onRun?: (t: Template) => void;
  /** ⏰: open its schedule in Templates. */
  onSchedule?: (t: Template) => void;
  /** Drawn on page 1, under the describe box. */
  landing?: boolean;
}) {
  // Nothing to pick: no strip at all (a new user's first form is short).
  // Templates are made with "Save as template…" at the bottom of this form.
  if (!templates.length) return null;
  const acts = !!(onRun || onSchedule);
  return (
    <div id="new-templates" className={"new-templates" + (landing ? " nt-landing" : "")}>
      <div className="nt-head">
        <span title="Your templates — one list on every device">
          Templates{" "}
          <span className="muted">
            {acts
              ? "— ▶ runs one now · ⏰ schedules it · its name fills the form"
              : "— click one to fill the form"}
          </span>
        </span>
        <button
          type="button"
          id="new-templates-manage"
          className="linklike"
          title="Run, schedule, edit and delete templates in Schedule — you come back here on Close"
          onClick={() => useUi.getState().openDialogFor("templates", templatesTarget({ fromNew: true, schedule: null }))}
        >
          {/* The dialog's own name: it is titled Schedule everywhere else. */}
          Open Schedule…
        </button>
      </div>
      <div id="new-templates-list" className="nt-list">
        {templates.map((t) => {
          const problem = runProblem(t);
          // THE schedule line (Schedule's card says the same) and its time.
          const line = templateScheduleLine(t);
          const sched = chipScheduleText(t, line);
          return (
            <span key={t.name} className={"nt-tpl" + (acts ? " nt-tpl-acts" : "")}>
              <button
                type="button"
                className={"nt-chip" + (activeTemplate === t.name ? " active" : "")}
                data-name={t.name}
                aria-pressed={activeTemplate === t.name}
                title={"Fill the form from “" + t.name + "”" + (t.prompt ? ": " + t.prompt : "")}
                onClick={() => onPick(t)}
              >
                {t.name}
              </button>
              {onRun && (
                <button
                  type="button"
                  className="nt-act nt-run"
                  aria-label={"Run “" + t.name + "” now"}
                  title={problem ? problem + " (Open Schedule… → Edit)" : "Run now — starts a new session from “" + t.name + "”"}
                  disabled={!!problem}
                  onClick={() => onRun(t)}
                >
                  ▶
                </button>
              )}
              {onSchedule && (
                <button
                  type="button"
                  className={"nt-act nt-sched" + (sched ? " on" : "")}
                  aria-label={sched ? "Change the schedule of “" + t.name + "”: " + line : "Schedule “" + t.name + "”"}
                  title={
                    sched
                      ? line + " — change or stop it"
                      : "Schedule… — start it by itself every day, weekdays, once or on a cron"
                  }
                  onClick={() => onSchedule(t)}
                >
                  ⏰{sched && <span className="nt-sched-at"> {sched}</span>}
                </button>
              )}
            </span>
          );
        })}
      </div>
    </div>
  );
}
