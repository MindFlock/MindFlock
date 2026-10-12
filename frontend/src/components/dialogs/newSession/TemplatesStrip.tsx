/** The New form's Templates strip: one server-side list (managed in
 * TemplatesDialog), and the fill that writes a template into the form. */

import type { Dispatch, MutableRefObject, SetStateAction } from "react";
import type { Template } from "../../../lib/templates";
import { useUi } from "../../../state/store";
import type { FolderAction } from "../newSession";

type Set<T> = Dispatch<SetStateAction<T>>;

export interface TemplateFillCtx {
  title: string;
  titleRef: MutableRefObject<HTMLInputElement | null>;
  setAgent(value: string): void;
  folderDo: Dispatch<FolderAction>;
  setPrompt: Set<string>;
  setProvision: Set<boolean>;
  setStrategy: Set<string>;
  setInPlace: Set<boolean>;
  setInitRepo: Set<boolean>;
  setActiveTemplate: Set<string>;
  setAdvancedOpen: Set<boolean>;
  setPromptOpen: Set<boolean>;
  setTitle: Set<string>;
}

/** fillFromTemplate, built per render from the form it writes into. */
export function templateFiller(ctx: TemplateFillCtx) {
  const {
    title,
    titleRef,
    setAgent,
    folderDo,
    setPrompt,
    setProvision,
    setStrategy,
    setInPlace,
    setInitRepo,
    setActiveTemplate,
    setAdvancedOpen,
    setPromptOpen,
    setTitle,
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
    setActiveTemplate(t.name);
    if (opts.quiet) return;
    setAdvancedOpen(!!(t.provisioned || t.init_repo || !t.in_place));
    // A template that brings a prompt has just written into a fold that
    // defaults shut; leaving it shut hides the text it filled in.
    if (t.prompt) setPromptOpen(true);
    if (!title.trim()) setTitle(t.name || "");
    titleRef.current?.focus();
  };
  return fillFromTemplate;
}

export function TemplatesStrip({
  templates,
  activeTemplate,
  fillFromTemplate,
  closeDialog,
}: {
  templates: Template[];
  activeTemplate: string;
  fillFromTemplate(t: Template): void;
  closeDialog(): void;
}) {
  return (
    <div id="new-templates" className="new-templates">
      <div className="nt-head">
        <span title="Saved New-session setups — one list on every device">Templates</span>
        <button
          type="button"
          id="new-templates-manage"
          className="linklike"
          title="Add, edit and delete templates"
          onClick={() => {
            closeDialog();
            useUi.getState().openDialogFor("templates", "new");
          }}
        >
          Manage…
        </button>
      </div>
      {templates.length > 0 ? (
        <div id="new-templates-list" className="nt-list">
          {templates.map((t) => (
            <button
              key={t.name}
              type="button"
              className={"nt-chip" + (activeTemplate === t.name ? " active" : "")}
              data-name={t.name}
              aria-pressed={activeTemplate === t.name}
              title={(t.program ? "[" + t.program + "] " : "") + (t.prompt || "launch this recipe")}
              onClick={() => fillFromTemplate(t)}
            >
              {t.name}
            </button>
          ))}
        </div>
      ) : (
        <p className="nf-describe-note">
          No templates yet. Save this form's prompt under a name (Prompt fold), or
          Manage… to add one.
        </p>
      )}
    </div>
  );
}
