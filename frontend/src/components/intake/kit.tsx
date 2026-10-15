/** The shared vocabulary behind the three Intake tabs — Tickets, Pull requests
 * and Issues.
 *
 * All three answer the same question ("what work is out there, and which of it
 * is MindFlock allowed to pick up?") and so all three have the same anatomy:
 *
 *   master switch  →  status line
 *   work           →  the items those sources yielded, grouped by source,
 *                     each row saying why auto-pickup did or didn't take it;
 *                     each SOURCE heading is that source's handle — Edit (or
 *                     Fix) opens its editor attached under it
 *   advanced       →  the tab-wide defaults, folded
 *
 * Adding a source is "+ Add source" in the dialog head: one inline flow
 * (`AddFlowShell`) — what, connect, what it does — for a tracker or a
 * repository alike. With nothing configured that flow leads the tab. There
 * are no source cards under the work any more ("add once, edit where it
 * shows"): a card list sat below a list that grows without bound, collapsed,
 * and the work list could only say "open its card below".
 *
 * They used to be three dialects of that shape. Ticketing had the good one —
 * add/remove cards, each carrying its own credentials, repo and agent — while
 * PR review and issue handling had a flat chip list of repo names plus one
 * screen-wide set of options, so "this repo, but with a longer grace period"
 * was not expressible. The pieces here are that better shape, generalized:
 * a ticketing source and a watched repo are both just *sources* now.
 *
 * What legitimately differs stays in the tabs: only tickets have workflow-state
 * buckets inside a source, and only the GitHub tabs share one credential.
 *
 * Class names are historical (`tk-` from ticketing, `pr-` from PR review) and
 * deliberately left alone — they were already shared by all three screens, and
 * renaming them would churn the built bundle and its assertions without moving
 * a single pixel. Read them as "work card" / "work item".
 */

import { useEffect, useMemo, useRef, useState } from "react";
import { api } from "../../api/client";
import type { Instance } from "../../api/types";
import { refreshInstances, useGithubStatus, useProviderEfforts } from "../../state/queries";
import { openConnectGitHub } from "../../lib/githubConnect";
import { displayName } from "../../state/store";
import { selectSession } from "../../lib/sessionActions";
import { toast } from "../../lib/toast";
import { relTime } from "../../lib/format";
import { errorPop } from "../../lib/errorPop";
import { shortReason } from "../../lib/failureText";
import { LANE_LABEL, SESSION_DEPTHS, laneLabel } from "../../lib/autopilot";
import { searchTokens } from "../../lib/rowSearch";
import { useUi } from "../../state/store";
import { DialogFilter } from "../dialogs/DialogFilter";
import {
  EFFORTS,
  EFFORT_LABELS,
  effortOptionLabel,
  effortTitle,
  supportsEffort,
} from "../../lib/effort";

/** A workspace an earlier run of a work item left on THIS machine, as the
 * server found it (see backend/web/core/reopen.py). Present on a row only when
 * the directory is still there, so its mere presence is the answer to "can this
 * be reopened?" — the fields are for saying which thing is being reopened. */
export interface ItemWorkspace {
  /** `closed` = a session you ended (restored in full), `worktree` / `clone` =
   * a workspace whose session is gone but whose files are not. */
  kind?: string;
  path?: string;
  branch?: string;
  /** Only on `closed` — when you ended it. */
  closed_at?: string;
}

/** Put a window back on the workspace an intake item already has here.
 *
 * `target` is the item's identity in the same shape its /start route takes
 * (`{kind: "tickets", source, id}` / `{kind: "prs"|"issues", repo, number}`) —
 * the server re-resolves the workspace itself rather than trusting a path from
 * the client, so a panel left open for an hour can't name a directory that has
 * since been deleted. Resolves with the reopened session's title. */
export async function reopenIntakeItem(target: Record<string, unknown>): Promise<string> {
  const inst = await api<Instance>("/api/intake/reopen", { json: target });
  await refreshInstances();
  if (inst?.title) selectSession(inst.title);
  return inst?.title || "";
}

/** "closed 2d ago" / "on disk" — what the Reopen button is pointing at. */
function workspaceNote(ws: ItemWorkspace): string {
  if (ws.kind === "closed") {
    const t = Date.parse(ws.closed_at || "");
    const when = isFinite(t) ? relTime(t / 1000) : "";
    return when ? "session ended " + when : "session you ended";
  }
  return "workspace left on this machine";
}

/** "3h old" / "20m old" / "2d old" for an ISO timestamp (every Intake
 * payload's spelling) — lib/format's relTime with an age's suffix; "" when
 * the timestamp is missing or unparseable. */
export function ageText(iso?: string): string {
  const t = Date.parse(iso || "");
  return isFinite(t) ? relTime(t / 1000, { suffix: " old" }) : "";
}

/** The one-line status under a panel's Refresh button.
 *
 * Every tab needs the same three-way answer — an upstream error, a refresh in
 * progress over rows already on screen, or a first load — and each used to
 * spell it out itself. Shared so the three can't drift into describing the same
 * state three ways. */
export function panelNote(opts: {
  error?: string;
  fetching: boolean;
  /** Whether rows are already on screen (null = nothing loaded yet). */
  loaded: boolean;
  /** Extra text that outranks the progress note (e.g. per-source errors). */
  detail?: string;
}): string {
  if (opts.error || opts.detail) return opts.detail || "";
  if (!opts.fetching) return "";
  return opts.loaded ? "Refreshing…" : "Loading…";
}

/** The master on/off switch at the top of a tab: one banded row, the automation
 * named on the left and the switch on the right.
 *
 * There used to be a status sentence under it — "● Active — polling 1 source for
 * tickets assigned to you", "‖ Paused — 1 source kept; turn Automated ingestion
 * on to resume" — and it has been dropped. With the switch a finger-width to the
 * right and the sources it counts listed directly below, the line restated two
 * things already on screen, in a third colour, and cost a full row of panel
 * height in each of the three tabs.
 *
 * The one state a switch genuinely cannot show is "there is nothing to turn on
 * yet", so that survives as `note` — and it sits *inside* the row as a subtitle,
 * where it reads as part of the control rather than as a stray line of copy. */
export function AutomationSwitch({
  label,
  title,
  rowId,
  inputId,
  statusId,
  checked,
  onChange,
  note,
  disabled,
}: {
  label: string;
  title: string;
  rowId?: string;
  inputId?: string;
  statusId?: string;
  checked: boolean;
  onChange(next: boolean): void;
  /** Subtitle inside the row, for what the switch can't say itself: that there
   * is nothing to turn on yet — never to narrate a plain on/off the switch
   * already shows. */
  note?: string;
  disabled?: boolean;
}) {
  // The switch's accessible name is the row's own words (aria-labelledby), so
  // a screen reader hears "Auto-start tickets, switch" rather than nothing.
  const nameId = inputId ? inputId + "-name" : undefined;
  return (
    <div className="ik-switch" id={rowId} title={title}>
      <span className="ik-switch-text">
        <span className="set-label" id={nameId}>
          {label}
        </span>
        {note ? (
          <span className="set-hint" id={statusId}>
            {note}
          </span>
        ) : null}
      </span>
      {/* label wraps only the switch, so clicking the row text never flips it
          (the app-wide rule — settingsRevamp.test pins it) */}
      <label className="ca-switch">
        <input
          type="checkbox"
          id={inputId}
          checked={checked}
          disabled={disabled}
          aria-labelledby={nameId}
          aria-describedby={note ? statusId : undefined}
          onChange={(e) => onChange(e.target.checked)}
        />
        <span className="ca-slider" />
      </label>
    </div>
  );
}

/** What a Test is doing or last said — owned by the button itself, or by the
 * card when the test can also run without a click (after a save). */
export interface TestResult {
  testing: boolean;
  ok?: boolean;
  msg?: string;
}

/** The Test button + its ✓/✗ result, as every card's footer wears it.
 *
 * Uncontrolled by default. A card whose tests also run on their own (the
 * ticket sources auto-test after a save) passes `result` and the button shows
 * that instead of a verdict of its own — so a save's test and a click's test
 * are the same line, not two. */
export function TestButton({
  label = "Test connection",
  onTest,
  result,
}: {
  label?: string;
  /** Resolve with the success message, or throw with the failure. */
  onTest(): Promise<string>;
  /** Controlled result; when given, the button keeps no state of its own. */
  result?: TestResult;
}) {
  const [own, setOwn] = useState<TestResult>({ testing: false });
  const state = result || own;
  return (
    <>
      <button
        type="button"
        className="test-btn"
        disabled={state.testing}
        onClick={async () => {
          if (!result) setOwn({ testing: true });
          try {
            const msg = await onTest();
            if (!result) setOwn({ testing: false, ok: true, msg });
          } catch (err) {
            if (!result)
              setOwn({ testing: false, ok: false, msg: (err as Error).message || "failed" });
          }
        }}
      >
        {label}
      </button>
      <span className={"test-result" + (state.msg ? (state.ok ? " ok" : " bad") : "")}>
        {state.testing ? "testing…" : state.msg ? (state.ok ? "✓ " : "✗ ") + state.msg : ""}
      </span>
    </>
  );
}

/** The three-state pill a ticket source wears: connected (its stored
 * configuration was seen to work within a day), needs attention (with why),
 * or off (no credentials). `title` carries the message for the collapsed
 * header, where there is no room to print it. */
export function SourcePill({
  state,
  label,
  message,
}: {
  state: "connected" | "needs_attention" | "off";
  label: string;
  message?: string;
}) {
  return (
    <span className={"ik-source-pill " + state} title={message || label} data-state={state}>
      {label}
    </span>
  );
}

/** A named, collapsible group of work rows with a count badge.
 *
 * Two levels use it: the source group (a ticketing source, a watched repo) and,
 * inside a ticket source, the workflow-state bucket. Same affordance both
 * times, so "click the heading to open it" only has to be learned once.
 *
 * A SOURCE group is also where that source is edited ("add once, edit where it
 * shows"): `action` is its Edit / Fix button, `editor` renders attached under
 * the heading — right where a problem was read — and `onDetail` makes the
 * heading's "auto-starts …" note itself a press that opens it. There are no
 * source cards under the work any more; the heading IS the handle. */
export function WorkGroup({
  name,
  count,
  detail,
  detailTone,
  onDetail,
  detailTitle,
  open,
  onToggle,
  onHide,
  hideTitle,
  indent,
  heading,
  middle,
  action,
  editor,
  groupKey,
  children,
}: {
  name: string;
  /** The badge. Omitted while a source's rows are still loading. */
  count?: number;
  /** Muted right-hand note on the heading (e.g. a source's error). */
  detail?: string;
  /** "bad": the note is a problem (can't connect); "warn": a gap (nothing
   * auto-starts yet). */
  detailTone?: "bad" | "warn";
  /** Present = the note is a setting, and pressing it opens the editor. */
  onDetail?: () => void;
  detailTitle?: string;
  open: boolean;
  onToggle(): void;
  /** Present = the group can be hidden from the panel entirely (✕). */
  onHide?: () => void;
  hideTitle?: string;
  /** Indent under a parent group (a workflow-state bucket inside its source). */
  indent?: boolean;
  /** Heavier type — this group names a whole queue, not a state inside one. */
  heading?: boolean;
  /** The level between a source and its states (a workflow, a board). */
  middle?: boolean;
  /** Right-aligned on the heading: the source's Edit / Fix. */
  action?: React.ReactNode;
  /** The source's editor, attached under the heading while it is open. */
  editor?: React.ReactNode;
  /** `data-group-key`, so a deep link can scroll its heading into view. */
  groupKey?: string;
  children?: React.ReactNode;
}) {
  return (
    <div
      className={
        "tk-bucket" +
        (indent ? " ik-nested" : "") +
        (heading ? " ik-source-group" : "") +
        (middle ? " ik-workflow-group" : "") +
        (editor ? " ik-editing" : "")
      }
      data-group-key={groupKey}
    >
      <div className="tk-bucket-head">
        <button
          type="button"
          className={"tk-bucket-toggle" + (onDetail ? " ik-toggle-tight" : "")}
          aria-expanded={open}
          title={(open ? "Collapse" : "Expand") + " " + name}
          onClick={onToggle}
        >
          {/* One glyph, rotated by CSS on aria-expanded — a turn is motion the
              eye catches, where swapping ▸ for ▾ is a shape you have to read. */}
          <span className="tk-caret">▸</span>
          <span className="tk-bucket-name">{name}</span>
          {count !== undefined ? <span className="tk-bucket-count">{count}</span> : null}
          {detail && !onDetail ? (
            <span className={"ik-group-detail" + (detailTone ? " " + detailTone : "")}>{detail}</span>
          ) : null}
        </button>
        {detail && onDetail ? (
          <button
            type="button"
            className={"ik-group-detail ik-detail-btn" + (detailTone ? " " + detailTone : "")}
            title={detailTitle || "Change this"}
            onClick={onDetail}
          >
            {detail}
          </button>
        ) : null}
        {/* The rest of the heading still folds the group: only the note and
            the action are their own targets. */}
        {onDetail ? <span className="ik-head-fill" aria-hidden="true" onClick={onToggle} /> : null}
        {onHide && (
          <button
            type="button"
            className="tk-bucket-x"
            title={hideTitle || "Hide " + name}
            aria-label={"Hide " + name}
            onClick={onHide}
          >
            ✕
          </button>
        )}
        {action}
      </div>
      {editor ? <div className="ik-editor">{editor}</div> : null}
      {open && children ? <div className="ik-group-body">{children}</div> : null}
    </div>
  );
}

/** Which source's editor is open (one at a time per list), seeded from a deep
 * link — Settings → Connections → "Open in Intake", or a tab handing over with
 * a source named — and scrolled into view when it is. */
export function useEditing(focus: string | undefined) {
  const [editing, setEditing] = useState<string | null>(focus || null);
  useEffect(() => {
    if (!focus) return;
    setEditing(focus);
    const t = window.setTimeout(() => scrollToGroup(focus), 60);
    return () => window.clearTimeout(t);
  }, [focus]);
  return {
    editing,
    isEditing: (key: string) => editing === key,
    toggle: (key: string) => setEditing((cur) => (cur === key ? null : key)),
    open: (key: string) => {
      setEditing(key);
      window.setTimeout(() => scrollToGroup(key), 60);
    },
    close: () => setEditing(null),
  };
}

/** Bring a source heading (`data-group-key`) to the top of its scroller. */
export function scrollToGroup(key: string) {
  const sel = "[data-group-key=\"" + (typeof CSS !== "undefined" && CSS.escape ? CSS.escape(key) : key) + "\"]";
  const el = document.querySelector(sel);
  if (el && "scrollIntoView" in el) (el as HTMLElement).scrollIntoView({ block: "start", behavior: "smooth" });
}

/** The repository groups a GitHub work list shows: EVERY watched repository —
 * its heading is where it is edited, so it never drops out, not even while a
 * Ctrl+F filter matches none of its rows (it reads "0" then) — followed by any
 * repository that answered with rows but is no longer watched. */
export function repoGroupOrder<T>(repos: string[], byRepo: Map<string, T[]>): string[] {
  const watched = new Set(repos.map((r) => r.toLowerCase()));
  return [...repos, ...[...byRepo.keys()].filter((r) => !watched.has(r.toLowerCase()))];
}

/** A source heading's action: quiet **Edit**, or **Fix** when the source is
 * broken, or **Done** while its editor is open. */
export function HeadAction({
  editing,
  broken,
  name,
  onClick,
}: {
  editing: boolean;
  broken?: boolean;
  name: string;
  onClick(): void;
}) {
  return (
    <button
      type="button"
      className={"ik-head-action" + (editing ? " done" : broken ? " fix" : "")}
      aria-expanded={editing}
      title={editing ? "Close " + name + "'s settings" : (broken ? "Fix " : "Edit ") + name}
      onClick={onClick}
    >
      {editing ? "Done" : broken ? "Fix" : "Edit"}
    </button>
  );
}

/** A destructive button that ARMS rather than fires: the first press turns
 * "Remove" into a red "Confirm — remove Jira — payments", the second does it.
 * Inline, never `confirm()` — these live inside an `aria-modal` dialog, and the
 * desktop app has no native confirm at all. Disarms on blur or after 5 s, so a
 * stray earlier press can never be the first half of a later one. */
export function ArmButton({
  label,
  armedLabel,
  onConfirm,
  title,
  className,
  id,
}: {
  label: string;
  armedLabel: string;
  onConfirm(): void;
  title?: string;
  className?: string;
  id?: string;
}) {
  const [armed, setArmed] = useState(false);
  useEffect(() => {
    if (!armed) return;
    const t = window.setTimeout(() => setArmed(false), 5000);
    return () => window.clearTimeout(t);
  }, [armed]);
  return (
    <button
      type="button"
      id={id}
      className={"ik-arm" + (armed ? " armed" : "") + (className ? " " + className : "")}
      title={armed ? "Press again to confirm" : title}
      data-armed={armed || undefined}
      onBlur={() => setArmed(false)}
      onClick={() => {
        if (!armed) return setArmed(true);
        setArmed(false);
        onConfirm();
      }}
    >
      {armed ? armedLabel : label}
    </button>
  );
}

/** "GitHub — connected as @you · Manage…": the one GitHub credential, with its
 * real state, where the tabs that need it are. It used to be a stateless
 * "Connect GitHub" button inside two Advanced folds, reading Connect even
 * while connected. */
export function GithubStatusLine({ buttonId, needFor }: { buttonId?: string; needFor: string }) {
  const q = useGithubStatus(true);
  const st = q.data;
  if (!st) {
    return (
      <div className="ik-ghline" data-gh="loading">
        <span className="ik-dot off" />
        <b>GitHub</b>
        <span className="ik-ghline-text">checking…</span>
      </div>
    );
  }
  const on = !!st.connected;
  const who = st.login ? "connected as @" + st.login : st.user_error ? "connected — " + st.user_error : "connected";
  return (
    <div className={"ik-ghline" + (on ? "" : " off")} data-gh={on ? "connected" : "off"}>
      <span className={"ik-dot" + (on ? "" : " warn")} />
      <b>GitHub</b>
      <span className="ik-ghline-text">
        {on ? who + " — one sign-in for PRs, issues, Verify and Open PR" : "not connected — " + needFor}
      </span>
      <button
        type="button"
        id={buttonId}
        className={on ? "ik-quiet-btn" : "test-btn"}
        onClick={() => openConnectGitHub()}
      >
        {on ? "Manage…" : "Connect GitHub"}
      </button>
    </div>
  );
}

/** The GitHub PRs / GitHub issues tab with nothing watched yet: ONE card,
 * three steps — Connect GitHub → Pick a repo → Turn on. It replaced the
 * generic "Add a source" wizard that opened on its own there PLUS a separate
 * "GitHub — not connected" box: two Connect GitHub buttons and two ways to
 * add on one screen (round 5). The wizard is still one press away, behind
 * "+ Add source" — and "Pick a repo…" opens its repository step. */
export function GithubSetupCard({
  what,
  onPickRepo,
  onTurnOn,
}: {
  /** "PR review comments" / "new issues" — what step 3 turns on. */
  what: string;
  onPickRepo(): void;
  onTurnOn(): void;
}) {
  const st = useGithubStatus(true).data;
  const connected = !!st?.connected;
  const steps: Array<{ key: string; label: string; done: boolean; body: React.ReactNode }> = [
    {
      key: "github",
      label: "Connect GitHub",
      done: connected,
      body: connected ? (
        <span className="ik-setup-done">connected{st?.login ? " as @" + st.login : ""}</span>
      ) : (
        <button type="button" className="btn-primary" id="ik-setup-connect" onClick={() => openConnectGitHub()}>
          Connect GitHub
        </button>
      ),
    },
    {
      key: "repo",
      label: "Pick a repo",
      done: false,
      body: (
        <button
          type="button"
          className={connected ? "btn-primary" : "test-btn"}
          id="ik-setup-repo"
          onClick={onPickRepo}
        >
          Pick a repo…
        </button>
      ),
    },
    {
      key: "on",
      label: "Turn on",
      done: false,
      body: (
        <span className="set-hint">
          Then switch on <strong>{what}</strong> on the{" "}
          <button type="button" className="linklike" onClick={onTurnOn}>
            Auto-start tab
          </button>
          .
        </span>
      ),
    },
  ];
  return (
    <section className="ik-setup-card" id="ik-github-setup" aria-label="Set up">
      <ol className="ik-setup-steps">
        {steps.map((s, i) => (
          <li key={s.key} className={"ik-setup-step" + (s.done ? " done" : "")} data-step={s.key}>
            <span className="ik-step-num">{s.done ? "✓" : i + 1}</span>
            <span className="ik-setup-label">{s.label}</span>
            <span className="ik-setup-body">{s.body}</span>
          </li>
        ))}
      </ol>
    </section>
  );
}

/** One kind of source the add flow can create: a tracker, or a repository. */
export interface AddKind {
  key: string;
  label: string;
  sub?: string;
}

/** What each tracker tile says under its name: whose work it brings in. The
 * point is GitHub Issues — a TRACKER here (issues assigned to you, worked
 * like tickets), which a new user otherwise reads as the GitHub issues tab
 * (every new issue in a watched repo: the Repository tile). */
const TRACKER_SUB: Record<string, string> = {
  github_issues: "Issues assigned to you, as tickets",
  shortcut: "Stories assigned to you",
  jira: "Issues assigned to you",
  linear: "Issues assigned to you",
  asana: "Tasks assigned to you",
};

/** The tracker tiles, one per catalog entry, in the catalog's order. */
export function trackerKinds(catalog: Array<{ id: string; label: string }>): AddKind[] {
  return catalog.map((p) => ({ key: p.id, label: p.label, sub: TRACKER_SUB[p.id] || "Tickets assigned to you" }));
}

/** The Repository tile: a watched GitHub repo, not a tracker. */
export const REPO_KIND: AddKind = {
  key: "repo",
  label: "Repository",
  sub: "Every PR or new issue in a repo",
};

/** The add flow's frame: title, the three steps, the kind tiles, the body and
 * the footer. Shared by the tracker flow (TicketsTab) and the repository flow
 * (RepoSources' AddRepoFlow), so "+ Add source" is one door whichever kind you
 * pick behind it. Inline in the panel — never a second window. */
export function AddFlowShell({
  title = "Add a source",
  step,
  kinds,
  picked,
  onPick,
  onCancel,
  cancelLabel = "Cancel",
  children,
  footer,
}: {
  title?: string;
  step: 1 | 2 | 3;
  /** Tiles; none or one = no tile row (Verify only adds repositories). */
  kinds: AddKind[];
  picked: string;
  onPick(key: string): void;
  onCancel(): void;
  cancelLabel?: string;
  children?: React.ReactNode;
  footer?: React.ReactNode;
}) {
  const steps = ["What", "Connect", "What it does"];
  return (
    <section className="ik-add" id="ik-add-flow" aria-label={title} data-step={step}>
      <div className="ik-add-head">
        <b className="ik-add-title">{title}</b>
        <ol className="ik-steps" aria-label="Steps">
          {steps.map((label, i) => {
            const n = (i + 1) as 1 | 2 | 3;
            const state = n < step ? "done" : n === step ? "cur" : "todo";
            return (
              <li key={label} className={"ik-step " + state} aria-current={state === "cur" ? "step" : undefined}>
                <span className="ik-step-num">{state === "done" ? "✓" : n}</span>
                {label}
              </li>
            );
          })}
        </ol>
        <button type="button" className="ik-quiet-btn" id="ik-add-cancel" onClick={onCancel}>
          {cancelLabel}
        </button>
      </div>
      {kinds.length > 1 && step < 3 ? (
        <div className="ik-kinds" role="radiogroup" aria-label="What to add" data-tour="intake-tiles">
          {kinds.map((k) => (
            <button
              key={k.key}
              type="button"
              role="radio"
              aria-checked={picked === k.key}
              className={"ik-kind" + (picked === k.key ? " picked" : "")}
              data-kind={k.key}
              onClick={() => onPick(k.key)}
            >
              <span className="ik-kind-label">{k.label}</span>
              {k.sub ? <span className="ik-kind-sub">{k.sub}</span> : null}
            </button>
          ))}
        </div>
      ) : null}
      {children}
      {footer ? <div className="ik-add-foot">{footer}</div> : null}
    </section>
  );
}

/** A small popover menu on a toolbar, for view choices (whose tickets, which
 * states) — kept apart from the source's settings so the two can't pass for
 * each other. Same three duties as the ⋯ OverflowMenu: Escape closes it first
 * (capture phase, before the dialog's own Escape), a press outside closes it,
 * and the summary is a real focusable control. */
export function ViewMenu({
  id,
  label,
  title,
  children,
}: {
  id?: string;
  label: string;
  title?: string;
  children: React.ReactNode;
}) {
  const ref = useRef<HTMLDetailsElement>(null);
  const [open, setOpen] = useState(false);
  useEffect(() => {
    if (!open) return;
    const onDown = (e: MouseEvent) => {
      if (ref.current && !ref.current.contains(e.target as Node)) setOpen(false);
    };
    const onKey = (e: KeyboardEvent) => {
      if (e.key !== "Escape") return;
      setOpen(false);
      e.stopPropagation();
      e.preventDefault();
    };
    document.addEventListener("mousedown", onDown);
    document.addEventListener("keydown", onKey, true);
    return () => {
      document.removeEventListener("mousedown", onDown);
      document.removeEventListener("keydown", onKey, true);
    };
  }, [open]);
  return (
    <details className="ik-view" id={id} open={open} ref={ref}>
      <summary
        title={title}
        onClick={(e) => {
          e.preventDefault();
          setOpen((v) => !v);
        }}
      >
        {label}
      </summary>
      <div className="ik-view-pop">{children}</div>
    </details>
  );
}

/** "Here is the work we can see, and why each item has or hasn't been picked
 * up" — open PRs, open issues, assigned tickets. */
export function WorkListPanel({
  label,
  onRefresh,
  note,
  hint,
  children,
  rowId,
  refreshId,
  noteId,
  listId,
  toolbarExtra,
}: {
  label: string;
  onRefresh(): void;
  note?: string;
  hint: React.ReactNode;
  children: React.ReactNode;
  rowId?: string;
  refreshId?: string;
  noteId?: string;
  listId?: string;
  /** Tickets put their bucket picker here. */
  toolbarExtra?: React.ReactNode;
}) {
  return (
    <div className="set-row" id={rowId}>
      <span className="set-label">{label}</span>
      <div className="pr-open-toolbar">
        <button type="button" id={refreshId} className="test-btn" onClick={onRefresh}>
          Refresh
        </button>
        {toolbarExtra}
        {note ? (
          <span id={noteId} className="pr-open-note">
            {note}
          </span>
        ) : null}
      </div>
      <div id={listId} className="ik-groups">
        {children}
      </div>
      <span className="set-hint">{hint}</span>
    </div>
  );
}

/** Where a ticket on a several-repo source will start, and why — the server's
 * `route` (backend/ticket_ingestion/repo_routing.py::preview). */
export interface ItemRoute {
  repos?: string[];
  /** The routed (primary) repo's URL; "" while pending. */
  repo_url?: string;
  reason?: string;
  /** label | title | llm | manual | pending | … */
  method?: string;
  summary?: string;
  /** Every repo of the source, for the row's Repo picker. */
  choices?: Array<{ name: string; url: string }>;
}

/** `route.method` of a ticket on a source with ONE repo: not a routing
 * decision, just where every ticket from it starts (built client-side — the
 * server only routes sources with several). */
export const ONLY_REPO = "only";

/** The repo name in a clone URL or path: `git@github.com:acme/api.git` → `api`. */
export function repoNameOf(url: string): string {
  const u = (url || "").trim().replace(/\/+$/, "").replace(/\.git$/, "");
  return u.split(/[/:\\]/).pop() || u;
}

/** A repo as a person names it: `owner/name` for a GitHub-style clone URL
 * (`git@github.com:acme/api.git`, `https://github.com/acme/api`), else the
 * folder name — "→ acme/api" says which acme's api. */
export function repoSlugOf(url: string): string {
  const u = (url || "").trim().replace(/\/+$/, "").replace(/\.git$/, "");
  const m = u.match(/^(?:[a-z+]+:\/\/)?(?:[^@/]+@)?[^/:]+[/:]([^/:]+)\/([^/]+)$/i);
  if (m && !/^[./~]/.test(u)) return m[1] + "/" + m[2];
  return repoNameOf(u);
}

/** Close Intake and bring a session forward — "Open session ›" on any row.
 * The dialog remembers its tab, so Alt+I comes back to the same place. */
export function openSessionFromIntake(title: string) {
  useUi.getState().closeDialog();
  if (title) selectSession(title);
}

/** "→ billing-service · label 'billing'" / "→ repo picked when it starts". */
export function routeText(route: ItemRoute | null | undefined): string {
  if (!route) return "";
  const repos = (route.repos || []).filter(Boolean);
  // A one-repo source: just the repo — there was nothing to decide.
  if (route.method === ONLY_REPO && repos.length) return "→ " + repos[0];
  if (!repos.length || route.method === "pending") return "→ repo picked by the agent when it starts";
  return "→ " + repos.join(" + ") + (route.reason ? " · " + route.reason : "");
}

/** A multi-repo ticket row's Repo picker: "" = where routing sends it (the
 * default it names), else one of the source's repos, for just this start.
 * Renders nothing unless the source has several repos. */
export function RepoPick({
  route,
  value,
  reference,
  disabled,
  onChange,
}: {
  route?: ItemRoute | null;
  value: string;
  reference: string;
  disabled?: boolean;
  onChange(url: string): void;
}) {
  const choices = route?.choices || [];
  if (choices.length < 2) return null;
  const routed = route?.method === "pending" ? [] : (route?.repos || []).filter(Boolean);
  return (
    <label className="ik-item-pick ik-item-pick-repo">
      <span className="ik-item-pick-label">Repo</span>
      <select
        className="ik-item-repo"
        value={value}
        data-picked={value || undefined}
        disabled={disabled}
        title="Which repo to start this one in — just this start"
        aria-label={"Repo for " + reference}
        onChange={(e) => onChange(e.target.value)}
      >
        <option value="">
          {routed.length ? "Default (" + routed.join(" + ") + ")" : "Default (agent picks)"}
        </option>
        {choices.map((c) => (
          <option key={c.url} value={c.url}>
            {c.name}
          </option>
        ))}
      </select>
    </label>
  );
}

/** What a start resolves with: the toast text, plus the new session's title
 * when the server named it (the button then becomes a link to it). */
export type StartResult = string | { message: string; title?: string };

/** A row's "⋯" menu: the rare, housekeeping actions (Merge into…) that should
 * not sit next to Start at the same weight. Opens inline, under the button. */
export function RowMore({
  reference,
  open,
  onToggle,
  children,
}: {
  reference: string;
  open: boolean;
  onToggle(): void;
  children: React.ReactNode;
}) {
  return (
    <div className="ik-row-more">
      <button
        type="button"
        className={"linklike ik-row-more-btn" + (open ? " on" : "")}
        aria-expanded={open}
        aria-label={"More actions for " + reference}
        title="More actions"
        onClick={onToggle}
      >
        ⋯
      </button>
      {open ? <div className="ik-row-more-list">{children}</div> : null}
    </div>
  );
}

/** One row of a WorkListPanel: reference, title, meta, eligibility chips and
 * the force-start button. */
export function WorkItemRow({
  reference,
  url,
  title,
  meta,
  tooltip,
  hasSession,
  session,
  elsewhere,
  eligible,
  eligibleLabel,
  eligibleOn = true,
  route,
  reasons,
  actionLabel,
  onStart,
  failPrefix,
  linkTitle,
  agents,
  configuredAgent,
  configuredDepth,
  configuredEffort,
  workspace,
  onReopen,
  actionExtra,
  drawer,
  onDrawerClose,
  pick,
  housekeeping,
}: {
  reference: string;
  url?: string;
  title?: string;
  meta: React.ReactNode;
  tooltip: string;
  hasSession?: boolean;
  /** The title of the session this row already has (the panels' `session`):
   * "Open session ›" goes to it. */
  session?: string;
  /** Another of the user's devices already has this one (fleet claims): the
   * device's name. The row reads as taken there — no start button here. */
  elsewhere?: string;
  eligible?: boolean;
  /** e.g. "will auto-start" — or, with auto-start off, "auto-start off". */
  eligibleLabel: string;
  /** Whether the eligible chip is a promise (green) or only a fact (neutral):
   * false when the kind's auto-start is off, so the chip can never
   * say "will start" next to a switch that says it won't. */
  eligibleOn?: boolean;
  /** Where this item will start and why, on a source with several repos. */
  route?: ItemRoute | null;
  reasons?: string[];
  /** "Start" — the one verb every row uses. */
  actionLabel: string;
  /** Starts the session on `agent` ("" = the configured chain); resolve with the
   * toast text, or with `{message, title}` so the button can open the new
   * session. `effort` is a neutral rung the server translates into the
   * launching CLI's own flag ("" = that CLI's default). `repo` is a repo URL
   * picked on a multi-repo row ("" = where it is routed). */
  onStart(opts: { agent: string; depth: string; effort: string; repo: string }): Promise<StartResult>;
  /** e.g. "Begin review failed" */
  failPrefix: string;
  /** Tooltip on the reference link; defaults to GitHub's wording. */
  linkTitle?: string;
  /** Coding CLIs this row may be started on. Absent = no picker (the row's
   * source decides). */
  agents?: string[];
  /** What the empty choice resolves to — this row's source / repo card value,
   * so "Default" is never a mystery. */
  configuredAgent?: string;
  /** What this row's source defaults its automation depth to, so the empty
   * choice names it. */
  configuredDepth?: string;
  /** ...and its thinking effort. Absent/"" = the source has no opinion, so the
   * empty choice falls back to naming the CLI's own default. */
  configuredEffort?: string;
  /** The workspace this item already has here, when it has one. Present =>
   * the row leads with Reopen instead of with a fresh start. */
  workspace?: ItemWorkspace;
  /** Reopens `workspace`; resolve with the session title for the toast. */
  onReopen?(): Promise<string>;
  /** An extra action for the row, rendered in the ACTION COLUMN under the
   * primary button.
   *
   * It lived in the meta line for one release and that was a mistake worth
   * recording: the meta line is where a row states FACTS about itself — its
   * age, why auto ingestion skipped it — and a control dropped among them
   * inherits their styling and reads as one more label. Tickets' **Merge
   * into…** was invisible there, to the point of being reported missing by
   * someone looking straight at it. Every action a row has belongs in the one
   * column that holds actions, whatever it costs in height. */
  actionExtra?: React.ReactNode;
  /** A full-width block under the row, for whatever `metaExtra` opened.
   *
   * Inline, NOT a modal. These rows already live inside an `aria-modal`
   * dialog, and a second window opened from inside one is the mistake Verify
   * documented at length (VerifyDialog.tsx) — an OS-painted `confirm()` or a
   * nested overlay that traps focus in the wrong layer. What needs confirming
   * gets confirmed here, in the app's own paint, next to the row it is about. */
  drawer?: React.ReactNode;
  /** Closes `drawer`. Escape inside the drawer calls it — and stops there,
   * rather than closing the whole Intake dialog under it. */
  onDrawerClose?: () => void;
  /** A tick box before the reference, for picking several rows to start
   * together (Tickets → "Start together…"). Absent = no box. */
  pick?: { checked: boolean; onChange(on: boolean): void };
  /** Rare actions on the item in its TRACKER (Tickets: combine a duplicate
   * into another ticket), not on intake. Shown at the bottom of the row's
   * Options under their own "On the tracker" heading — never beside Start, so
   * a destructive tracker write is never one stray click from starting work. */
  housekeeping?: React.ReactNode;
}) {
  const [state, setState] = useState<"idle" | "starting" | "started">("idle");
  // The session a start created, once the server names it: "Started" becomes a
  // link to it rather than a dead end.
  const [startedTitle, setStartedTitle] = useState("");
  // Independent of `state`: reopening and starting are different actions on the
  // same row, and a failed reopen must leave Begin work usable.
  const [reopening, setReopening] = useState<"idle" | "busy" | "done">("idle");
  const canReopen = !!workspace && !!onReopen && !hasSession;
  // Per-start override, not persisted: it applies to THIS launch. Changing the
  // queue's CLI is a card edit; running one item elsewhere is this.
  const [agent, setAgent] = useState("");
  // Same lifetime, same reasoning: how far THIS launch should carry itself.
  // Unlike the source default, an individual item may choose "merge".
  const [depth, setDepth] = useState("");
  // …and how hard the agent should think about it. "This ticket is gnarly, run
  // it on ultracode" is a property of the ticket, not of the queue.
  const [effort, setEffort] = useState("");
  // …and, on a source with several repos, which one THIS start goes to.
  // "" = wherever routing sends it (the default the picker names).
  const [repo, setRepo] = useState("");
  const repoChoices = route?.choices || [];
  const canPickRepo = repoChoices.length > 1;
  // Which CLI this row would launch on, so the Effort picker can name that CLI's
  // ceiling. The cache is shared by every row (see useProviderEfforts).
  const effortCaps = useProviderEfforts().data;
  const effortProvider = agent || configuredAgent || "";
  // undefined = caps not loaded (or a custom program no provider claims), which
  // reads as "assume it works" rather than disabling a working control.
  const effortCap = effortCaps ? effortCaps[effortProvider] : undefined;
  const effortUsable = supportsEffort(effortCap);
  // The three pickers are a rarely-used override, and three dropdowns on every
  // row of a long list were most of its noise. Folded behind "Options" until
  // asked for — but never while one holds a pick, so a choice is never hidden.
  const [optsOpen, setOptsOpen] = useState(false);
  const picked = !!(agent || depth || (effortUsable && effort) || repo);
  const showPicks = optsOpen || picked;
  // What the empty choices resolve to, named once for the options and the
  // collapsed line alike.
  const defaultDepth = laneLabel(configuredDepth);
  const defaultIsOff = defaultDepth === LANE_LABEL.leave;
  const effortDefault = configuredEffort
    ? effortOptionLabel(configuredEffort, effortCap)
    : "";
  // "claude · Fast-track: Off · default effort" — what Start will use, said in
  // one line, with Fast-track named the way New's Options summary names it.
  const pickedRepo = repo ? repoChoices.find((c) => c.url === repo) : undefined;
  const where = pickedRepo ? "→ " + pickedRepo.name + " · your pick" : routeText(route);
  // Where it starts leads (round 5: "the row doesn't say where the session
  // will go"); an effort is named only when one is set.
  const uses = [
    where,
    agent || configuredAgent || (agents && agents.length > 0 ? "app default" : ""),
    "Fast-track: " + (depth ? laneLabel(depth) : defaultDepth),
    !effortUsable
      ? ""
      : effort
        ? (EFFORT_LABELS[effort] || effort) + " effort"
        : effortDefault
          ? effortDefault + " effort"
          : "",
  ]
    .filter(Boolean)
    .join(" · ");
  // The route is on the uses line while that line shows; a row without one
  // (a session is open, or it is taken elsewhere) keeps it in the meta line.
  const routeInMeta = hasSession || !!elsewhere;
  const routeTitle =
    route?.method === ONLY_REPO
      ? "The repo this starts in — this source's only repo. Add more with Edit on its heading."
      : (route?.method === "pending" || !(route?.repos || []).length
          ? "Nothing on the ticket names one of this source's repos, so the agent reads the ticket and picks when it starts."
          : "The repo this starts in, and why.") +
        (canPickRepo
          ? " To start it in another repo, pick one under Options."
          : " To send it elsewhere, give the ticket a label that matches another repo's name or hint (on the source card).");
  const openLabel = "Open session ›";
  const openTitle = "Close Intake and go to this session";
  const willAutoStart = !!eligible && eligibleOn && !hasSession;
  const housekeepingBlock = housekeeping ? (
    <div className="ik-item-housekeeping" role="group" aria-label="On the tracker">
      <span className="ik-item-pick-label">On the tracker</span>
      {housekeeping}
    </div>
  ) : null;
  // A row with no start (session open, or taken on another device) has no
  // Options of its own; it still gets the tracker actions behind one.
  const housekeepingFold = housekeeping ? (
    <>
      <button
        type="button"
        className="linklike ik-item-opts"
        aria-expanded={optsOpen}
        title="Actions on this ticket in the tracker"
        onClick={() => setOptsOpen(!optsOpen)}
      >
        {optsOpen ? "Hide options" : "Options"}
      </button>
      {optsOpen ? <div className="ik-item-picks">{housekeepingBlock}</div> : null}
    </>
  ) : null;
  return (
    <div
      className={"pr-open-item" + (pick?.checked ? " picked" : "")}
      title={tooltip}
      onKeyDown={(e) => {
        // Escape closes the innermost open thing: this row's Options before
        // the Intake dialog (a drawer under it has already taken its own).
        if (e.key !== "Escape" || e.defaultPrevented) return;
        if (drawer && onDrawerClose) {
          e.preventDefault();
          onDrawerClose();
        } else if (optsOpen) {
          e.preventDefault();
          setOptsOpen(false);
        }
      }}
    >
      <div className="pr-open-main">
        {pick && (
          <input
            type="checkbox"
            className="ik-pick"
            checked={pick.checked}
            aria-label={"Pick " + reference + " to start together"}
            title="Pick to start together with other tickets"
            onChange={(e) => pick.onChange(e.target.checked)}
          />
        )}
        <a
          href={url || "#"}
          target="_blank"
          rel="noopener noreferrer"
          className="pr-open-ref"
          title={linkTitle || "Open " + reference + " on GitHub"}
        >
          {reference}
        </a>
        <span className="pr-open-title">{title || ""}</span>
      </div>
      <div className="pr-open-meta">
        <span>{meta}</span>
        {where && routeInMeta ? (
          <span className="ik-item-route" data-route={route?.method || ""} title={routeTitle}>
            {where}
          </span>
        ) : null}
        {hasSession ? (
          <span className="pr-open-chip on">session open</span>
        ) : eligible ? (
          <span className={"pr-open-chip" + (eligibleOn ? " ok" : "")}>{eligibleLabel}</span>
        ) : (
          (reasons || []).map((reason) => {
            // A recorded failure reason is a full sentence of git output whose
            // actionable half is at the END ("… is already checked out at
            // <path>. Kill that session first"). It used to WRAP inside the chip
            // to keep that half visible, which turned one row into a paragraph.
            // Now the chip takes the front of the sentence and the whole thing
            // opens in the bottom-right error card — the corner this app already
            // uses for failures, and the only one with room for a paragraph.
            const { short, clipped } = shortReason(reason);
            if (!clipped)
              return (
                <span className="pr-open-chip" key={reason} title={reason}>
                  {reason}
                </span>
              );
            return (
              <button
                type="button"
                className="pr-open-chip pr-open-chip-more"
                key={reason}
                title={reason + "\n\nClick for the full message."}
                onClick={() => errorPop(reference + " — why it was skipped", reason)}
              >
                {short}
              </button>
            );
          })
        )}
      </div>
      {!hasSession && elsewhere ? (
        <div className="ik-item-start">
          <button
            type="button"
            className="btn-primary pr-review-btn"
            disabled
            title={"Already being worked on " + elsewhere + " — starting it here would duplicate it"}
          >
            On {elsewhere}
          </button>
          {actionExtra}
          {housekeepingFold}
        </div>
      ) : hasSession ? (
        // Wrapped in the same action column the other branch uses, rather than
        // sitting bare in the grid cell: a row whose session is open still has
        // actions (a ticket can be merged away while its session keeps running),
        // and they have to stack under the button the same way they do below.
        <div className="ik-item-start">
          {/* One control for "this row has a session", on every tab: it
              closes Intake and brings the session forward (round 5 found
              three — "Open session ›", "Started", a dead "Session open"). */}
          <button
            type="button"
            className="btn-primary pr-review-btn ik-open-session"
            title={openTitle}
            onClick={() => openSessionFromIntake(session || "")}
          >
            {openLabel}
          </button>
          {actionExtra}
          {housekeepingFold}
        </div>
      ) : (
        <div className="ik-item-start">
          {/* The work is already here: reopening it is the action, and starting
              over is the alternative — so Reopen takes the primary button and
              the start demotes to the quiet one below. Nothing is hidden: a
              leftover workspace can be one you want to abandon, and Begin work
              still reclaims it (worktree_reclaim) when it holds no work. */}
          {canReopen && (
            <button
              type="button"
              className="btn-primary pr-review-btn ik-reopen-btn"
              disabled={reopening !== "idle"}
              title={
                "Reopen the workspace this already has here — " +
                workspaceNote(workspace!) +
                (workspace!.branch ? "\nbranch: " + workspace!.branch : "") +
                (workspace!.path ? "\n" + workspace!.path : "")
              }
              onClick={async () => {
                setReopening("busy");
                try {
                  const title = await onReopen!();
                  toast("Reopened " + (title ? displayName(title) : reference));
                  setReopening("done");
                } catch (err) {
                  // Bottom-right card, not a 1.4s toast: the server's answer here
                  // is a sentence with a remedy in it, and it has to survive long
                  // enough to be read.
                  errorPop(
                    "Reopen failed — " + reference,
                    (err as Error).message || "error"
                  );
                  setReopening("idle");
                }
              }}
            >
              {reopening === "busy"
                ? "Reopening…"
                : reopening === "done"
                  ? "Reopened"
                  : "Reopen session"}
            </button>
          )}
          {/* Collapsed, the row says what Start will use and offers the
              pickers behind one quiet toggle. Once a picker holds a pick the
              toggle goes: hiding a choice that will still be applied is how a
              start ends up doing something nobody can see on screen. */}
          {!picked && (
            <div className="ik-item-optline">
              {!showPicks && (
                <span className="ik-item-uses" title={where ? routeTitle : undefined} data-route={route?.method || undefined}>
                  {uses}
                </span>
              )}
              <button
                type="button"
                className="linklike ik-item-opts"
                aria-expanded={showPicks}
                title={
                  showPicks
                    ? "Hide the per-start options"
                    : canPickRepo
                      ? "Pick a different repo, CLI, Fast-track or effort for just this start"
                      : "Pick a different CLI, Fast-track or effort for just this start"
                }
                onClick={() => setOptsOpen(!optsOpen)}
              >
                {showPicks ? "Hide options" : "Options"}
              </button>
            </div>
          )}
          {/* The per-launch pickers share ONE line. `.ik-item-start` is a
              column whose children are stretched to the widest, so a third
              stacked control would make every row in the list taller. */}
          {showPicks && (
          <div className="ik-item-picks">
            <RepoPick
              route={route}
              value={repo}
              reference={reference}
              disabled={state !== "idle"}
              onChange={setRepo}
            />
            {agents && agents.length > 0 && (
              <label className="ik-item-pick">
              <span className="ik-item-pick-label">Agent</span>
              <select
                className="ik-item-agent"
                value={agent}
                data-picked={agent || undefined}
                disabled={state !== "idle"}
                title={
                  "Coding CLI to run this one on — just this start, not the whole queue" +
                  (configuredAgent ? " (default: " + configuredAgent + ")" : "")
                }
                aria-label={"Coding CLI for " + reference}
                onChange={(e) => setAgent(e.target.value)}
              >
                <option value="">
                  {"Default (" + (configuredAgent || "app default") + ")"}
                </option>
                {agents.map((n) => (
                  <option key={n} value={n}>
                    {n}
                  </option>
                ))}
              </select>
              </label>
            )}
            <label className="ik-item-pick">
            <span className="ik-item-pick-label">Fast-track</span>
            <select
              className="ik-item-depth"
              value={depth}
              data-picked={depth || undefined}
              disabled={state !== "idle"}
              title={
                "Fast-track this one to — just this start, not the whole queue" +
                " (default: " + defaultDepth + ")"
              }
              aria-label={"Fast-track " + reference + " to"}
              onChange={(e) => setDepth(e.target.value)}
            >
              {/* Always NAME what the empty choice resolves to. A bare
                  "Configured" made you open Settings to find out what pressing
                  Start would actually do; an unset source default means no
                  Fast-track at all, which is "Off" — worth saying out loud.
                  The same five words as every other Fast-track control; an
                  explicit Off is offered only when the default is something
                  else (the start routes accept "off"). */}
              <option value="">{"Default (" + defaultDepth + ")"}</option>
              {!defaultIsOff && <option value="off">{LANE_LABEL.leave}</option>}
              {SESSION_DEPTHS.map((d) => (
                <option key={d} value={d}>
                  {LANE_LABEL[d]}
                </option>
              ))}
            </select>
            </label>
            {/* Thinking effort, and now it has a configured default to name like
                the other two: a ticketing source carries its own rung, so the
                empty choice says which rung this row would run at rather than
                the bare "Default effort" it used to show whatever the queue was
                set to. With no source opinion it still falls back to the CLI's
                own default, and the tooltip says where the CLI this row would
                launch tops out. A CLI with no effort setting gets the control
                disabled rather than an enabled one that does nothing. */}
            <label className="ik-item-pick">
            <span className="ik-item-pick-label">Effort</span>
            <select
              className="ik-item-effort"
              value={effortUsable ? effort : ""}
              data-picked={(effortUsable && effort) || undefined}
              disabled={state !== "idle" || !effortUsable}
              title={effortTitle(effortProvider, effortCap)}
              aria-label={"How hard to think about " + reference}
              onChange={(e) => setEffort(e.target.value)}
            >
              <option value="">
                {!effortUsable
                  ? "No effort (" + (effortProvider || "this CLI") + ")"
                  : effortDefault
                    ? "Default (" + effortDefault + ")"
                    : "Default effort"}
              </option>
              {effortUsable &&
                EFFORTS.map((e) => (
                  // A rung above this CLI's ceiling still runs — clamped — and
                  // the top rung is named the way the CLI names it (claude:
                  // `ultracode`), so a pick says what it will actually do.
                  <option key={e} value={e}>
                    {effortOptionLabel(e, effortCap)}
                  </option>
                ))}
            </select>
            </label>
            {where && !canPickRepo ? (
              <span className="ik-item-pick-note">
                {route?.method === ONLY_REPO ? (
                  <>Starts in {where.replace(/^→ /, "")}, this source's repo.</>
                ) : (
                  <>
                    Starts in {where.replace(/^→ /, "")}. To send it to another repo, give the
                    ticket a label matching that repo's name or hint.
                  </>
                )}
              </span>
            ) : null}
            {housekeepingBlock}
          </div>
          )}
          <button
            type="button"
            className={
              canReopen
                ? "test-btn ik-start-again"
                : // It starts on its own anyway: Start is the quiet "now, not
                  // later" button, and the "will auto-start" chip carries the row.
                  willAutoStart
                  ? "test-btn pr-review-btn ik-start-quiet"
                  : "btn-primary pr-review-btn"
            }
            disabled={state === "starting"}
            data-tour={state === "idle" ? "intake-start" : undefined}
            title={
              state === "started"
                ? openTitle
                : canReopen
                  ? actionLabel + " starts a NEW session, leaving the workspace above alone"
                  : "Start a session for " + reference + " now"
            }
            onClick={async () => {
              if (state === "started") {
                openSessionFromIntake(startedTitle || session || "");
                return;
              }
              setState("starting");
              try {
                const created = await onStart({
                  agent,
                  depth,
                  // Never sent for a CLI that has no effort control: the pick is
                  // unreachable there, and posting a stale one would put a
                  // "started at its own default" note on a start nobody asked
                  // an effort for.
                  effort: effortUsable ? effort : "",
                  repo,
                });
                const msg = typeof created === "string" ? created : created.message;
                const newTitle = typeof created === "string" ? "" : created.title || "";
                toast(msg + " — starting, see the sidebar");
                setStartedTitle(newTitle);
                setState("started");
              } catch (err) {
                errorPop(
                  failPrefix + " — " + reference,
                  (err as Error).message || "error"
                );
                setState("idle");
              }
            }}
          >
            {state === "starting"
              ? "Starting…"
              : state === "started"
                ? openLabel
                : actionLabel}
          </button>
          {actionExtra}
        </div>
      )}
      {drawer ? (
        <div
          className="ik-item-drawer"
          onKeyDown={(e) => {
            if (e.key !== "Escape" || !onDrawerClose) return;
            // Modal skips a defaultPrevented Escape: the drawer takes this one.
            e.preventDefault();
            // Focus goes back to the row (its Options toggle), so the NEXT
            // Escape closes the row's Options rather than, from a focus lost
            // to the page, the whole dialog.
            const rowEl = (e.currentTarget as HTMLElement).closest(".pr-open-item");
            onDrawerClose();
            requestAnimationFrame(() =>
              (rowEl?.querySelector(".ik-item-opts") as HTMLElement | null)?.focus()
            );
          }}
        >
          {drawer}
        </div>
      ) : null}
    </div>
  );
}

/* --------------------------------------------------------------------------
 * Per-device UI state (which groups are open, which buckets are shown).
 *
 * Kept in localStorage rather than settings.json: "I collapsed Backlog on this
 * laptop" is not a property of the flock, and syncing it would mean one
 * machine's reading habits reorganizing another's screen.
 * ------------------------------------------------------------------------ */

export function loadStringSet(key: string, keep?: (v: string) => boolean): Set<string> {
  try {
    const v = JSON.parse(localStorage.getItem(key) || "[]");
    const all = Array.isArray(v) ? v.map(String) : [];
    return new Set(keep ? all.filter(keep) : all);
  } catch {
    return new Set();
  }
}

export function saveStringSet(key: string, v: Set<string>) {
  try {
    localStorage.setItem(key, JSON.stringify([...v]));
  } catch {
    /* storage unavailable */
  }
}

/** A toggle-a-key-in-a-persisted-set hook, for group open/closed state.
 *
 * `invert` flips the meaning of membership: source groups default OPEN (the
 * rows are the reason you came), state buckets default CLOSED (the headings
 * with their counts are the overview), and expressing both as "the set holds
 * the exceptions" keeps a fresh install writing nothing at all. */
/** The Ctrl+F filter for one Intake work list.
 *
 * ONE HOOK, FOUR PANELS. Tickets, Pull requests, Issues and the Auto-start
 * roll-up all grow without bound — every open PR on every watched repo, every
 * ticket in every workflow state — and each of them wants the same control the
 * other list dialogs already have. Written once so a third dialect cannot
 * appear: the box is `DialogFilter` (Recently closed's, down to Ctrl+F focusing
 * it and Escape clearing before it closes), and the tokens it produces go
 * through `matchesTokens` in :mod:`intake/search`.
 *
 * Only the ACTIVE tab is mounted (`IntakeDialog` renders `tab === t.key &&`),
 * so exactly one of these exists at a time and Ctrl+F is never ambiguous.
 *
 * Returns the tokens (empty = match everything, so a panel with no query does
 * no work), whether the filter is doing anything, and the control to hand to
 * `WorkListPanel`'s `toolbarExtra`. */
export function useListFilter(id: string, placeholder: string) {
  const closeDialog = useUi((s) => s.closeDialog);
  const [query, setQuery] = useState("");
  const tokens = useMemo(() => searchTokens(query), [query]);
  return {
    query,
    tokens,
    active: tokens.length > 0,
    control: (
      <DialogFilter
        id={id}
        value={query}
        onChange={setQuery}
        placeholder={placeholder}
        onEscape={closeDialog}
      />
    ),
  };
}

export function useToggleSet(
  key: string,
  invert = false,
  /** Drops saved entries that no longer fit the key format — see the ticket
   * buckets, which went from a bare state name to `source::state`. */
  keep?: (v: string) => boolean
) {
  const [set, setSet] = useState<Set<string>>(() => loadStringSet(key, keep));
  return {
    isOpen: (k: string) => (invert ? !set.has(k) : set.has(k)),
    toggle: (k: string) =>
      setSet((prev) => {
        const next = new Set(prev);
        if (next.has(k)) next.delete(k);
        else next.add(k);
        saveStringSet(key, next);
        return next;
      }),
  };
}
