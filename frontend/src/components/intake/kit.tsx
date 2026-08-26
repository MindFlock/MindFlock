/** The shared vocabulary behind the three Intake tabs — Tickets, Pull requests
 * and Issues.
 *
 * All three answer the same question ("what work is out there, and which of it
 * is MindFlock allowed to pick up?") and so all three have the same anatomy:
 *
 *   master switch  →  status line
 *   sources        →  one collapsible card per thing being watched, + Add
 *   work           →  the items those sources yielded, grouped by source,
 *                     each row saying why auto-pickup did or didn't take it
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

import { useMemo, useState } from "react";
import { api } from "../../api/client";
import type { Instance } from "../../api/types";
import { refreshInstances, useProviderEfforts } from "../../state/queries";
import { displayName } from "../../state/store";
import { selectSession } from "../../lib/sessionActions";
import { toast } from "../../lib/toast";
import { errorPop } from "../../lib/errorPop";
import { shortReason } from "../../lib/failureText";
import { DEPTHS, DEPTH_LABELS } from "../../lib/autopilot";
import { searchTokens } from "../../lib/rowSearch";
import { useUi } from "../../state/store";
import { DialogFilter } from "../dialogs/DialogFilter";
import {
  EFFORTS,
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
    const when = ageText(ws.closed_at).replace(" old", " ago");
    return when ? "session ended " + when : "session you ended";
  }
  return "workspace left on this machine";
}

/** "3h old" / "20m old" / "2d old" — one implementation, three tabs. */
export function ageText(iso?: string): string {
  const t = Date.parse(iso || "");
  if (!isFinite(t)) return "";
  const mins = Math.max(0, Math.round((Date.now() - t) / 60000));
  if (mins < 60) return mins + "m old";
  const h = Math.round(mins / 60);
  if (h < 48) return h + "h old";
  return Math.round(h / 24) + "d old";
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
}: {
  label: string;
  title: string;
  rowId?: string;
  inputId?: string;
  statusId?: string;
  checked: boolean;
  onChange(next: boolean): void;
  /** Subtitle inside the row, for what the switch can't say itself. Pass it only
   * when the tab has no sources yet — never to narrate on/off. */
  note?: string;
}) {
  return (
    <div className="ik-switch" id={rowId} title={title}>
      <span className="ik-switch-text">
        <span className="set-label">{label}</span>
        {note ? (
          <span className="set-hint" id={statusId}>
            {note}
          </span>
        ) : null}
      </span>
      {/* label wraps only the switch, so clicking the row text never flips it */}
      <label className="ca-switch">
        <input
          type="checkbox"
          id={inputId}
          checked={checked}
          onChange={(e) => onChange(e.target.checked)}
        />
        <span className="ca-slider" />
      </label>
    </div>
  );
}

/** One source card: a fold whose header is a one-line summary and whose body is
 * whatever that kind of source needs configured.
 *
 * Generic on purpose. A ticketing source's body is a provider picker plus
 * credentials; a watched repo's body is a slug, an agent and its filters. The
 * card, the fold, the summary and the footer action row are the same either
 * way, which is the whole point of the unification. */
export function SourceCard({
  sourceId,
  summary,
  collapsed,
  onToggle,
  children,
  footer,
}: {
  /** Stable identifier, exposed for tests / addon CSS. */
  sourceId: string;
  summary: string;
  collapsed: boolean;
  onToggle(): void;
  children: React.ReactNode;
  /** The Test / Remove row. */
  footer: React.ReactNode;
}) {
  return (
    <div className={"tk-source" + (collapsed ? " tk-collapsed" : "")} data-source-id={sourceId}>
      <button type="button" className="tk-head" aria-expanded={!collapsed} onClick={onToggle}>
        <span className="tk-caret">{collapsed ? "▸" : "▾"}</span>
        <span className="tk-summary">{summary}</span>
      </button>
      <div className="tk-body">
        {children}
        <div className="set-row">
          <span className="test-row">{footer}</span>
        </div>
      </div>
    </div>
  );
}

/** The Test button + its ✓/✗ result, as every card's footer wears it. */
export function TestButton({
  label = "Test connection",
  onTest,
}: {
  label?: string;
  /** Resolve with the success message, or throw with the failure. */
  onTest(): Promise<string>;
}) {
  const [state, setState] = useState<{ testing: boolean; ok?: boolean; msg?: string }>({
    testing: false,
  });
  return (
    <>
      <button
        type="button"
        className="test-btn"
        onClick={async () => {
          setState({ testing: true });
          try {
            setState({ testing: false, ok: true, msg: await onTest() });
          } catch (err) {
            setState({ testing: false, ok: false, msg: (err as Error).message || "failed" });
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

/** A named, collapsible group of work rows with a count badge.
 *
 * Two levels use it: the source group (a ticketing source, a watched repo) and,
 * inside a ticket source, the workflow-state bucket. Same affordance both
 * times, so "click the heading to open it" only has to be learned once. */
export function WorkGroup({
  name,
  count,
  detail,
  open,
  onToggle,
  onHide,
  hideTitle,
  indent,
  heading,
  middle,
  children,
}: {
  name: string;
  count: number;
  /** Muted right-hand note on the heading (e.g. a source's error). */
  detail?: string;
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
  children: React.ReactNode;
}) {
  return (
    <div
      className={
        "tk-bucket" +
        (indent ? " ik-nested" : "") +
        (heading ? " ik-source-group" : "") +
        (middle ? " ik-workflow-group" : "")
      }
    >
      <div className="tk-bucket-head">
        <button
          type="button"
          className="tk-bucket-toggle"
          aria-expanded={open}
          title={(open ? "Collapse" : "Expand") + " " + name}
          onClick={onToggle}
        >
          {/* One glyph, rotated by CSS on aria-expanded — a turn is motion the
              eye catches, where swapping ▸ for ▾ is a shape you have to read. */}
          <span className="tk-caret">▸</span>
          <span className="tk-bucket-name">{name}</span>
          <span className="tk-bucket-count">{count}</span>
          {detail ? <span className="ik-group-detail">{detail}</span> : null}
        </button>
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
      </div>
      {open && <div className="ik-group-body">{children}</div>}
    </div>
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

/** One row of a WorkListPanel: reference, title, meta, eligibility chips and
 * the force-start button. */
export function WorkItemRow({
  reference,
  url,
  title,
  meta,
  tooltip,
  hasSession,
  eligible,
  eligibleLabel,
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
}: {
  reference: string;
  url?: string;
  title?: string;
  meta: React.ReactNode;
  tooltip: string;
  hasSession?: boolean;
  eligible?: boolean;
  /** e.g. "queued for auto review" */
  eligibleLabel: string;
  reasons?: string[];
  /** e.g. "Begin review" */
  actionLabel: string;
  /** Starts the session on `agent` ("" = the configured chain); resolve with the
   * created session's title for the toast. `effort` is a neutral rung the server
   * translates into the launching CLI's own flag ("" = that CLI's default). */
  onStart(opts: { agent: string; depth: string; effort: string }): Promise<string>;
  /** e.g. "Begin review failed" */
  failPrefix: string;
  /** Tooltip on the reference link; defaults to GitHub's wording. */
  linkTitle?: string;
  /** Coding CLIs this row may be started on. Absent = no picker (the row's
   * source decides). */
  agents?: string[];
  /** What the empty choice resolves to — this row's source / repo card value,
   * so "Configured" is never a mystery. */
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
}) {
  const [state, setState] = useState<"idle" | "starting" | "started">("idle");
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
  // Which CLI this row would launch on, so the Effort picker can name that CLI's
  // ceiling. The cache is shared by every row (see useProviderEfforts).
  const effortCaps = useProviderEfforts().data;
  const effortProvider = agent || configuredAgent || "";
  // undefined = caps not loaded (or a custom program no provider claims), which
  // reads as "assume it works" rather than disabling a working control.
  const effortCap = effortCaps ? effortCaps[effortProvider] : undefined;
  const effortUsable = supportsEffort(effortCap);
  return (
    <div className="pr-open-item" title={tooltip}>
      <div className="pr-open-main">
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
        {hasSession ? (
          <span className="pr-open-chip on">session open</span>
        ) : eligible ? (
          <span className="pr-open-chip ok">{eligibleLabel}</span>
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
      {hasSession ? (
        <button type="button" className="btn-primary pr-review-btn" disabled>
          Session open
        </button>
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
                  : "Reopen window"}
            </button>
          )}
          {/* The two per-launch pickers share ONE line. `.ik-item-start` is a
              column whose children are stretched to the widest, so a third
              stacked control would make every row in the list taller. */}
          <div className="ik-item-picks">
            {agents && agents.length > 0 && (
              <select
                className="ik-item-agent"
                value={agent}
                data-picked={agent || undefined}
                disabled={state !== "idle"}
                title={
                  "Coding CLI to run this one on — just this start, not the whole queue" +
                  (configuredAgent ? " (configured: " + configuredAgent + ")" : "")
                }
                aria-label={"Coding CLI for " + reference}
                onChange={(e) => setAgent(e.target.value)}
              >
                <option value="">
                  {"Configured (" + (configuredAgent || "app default") + ")"}
                </option>
                {agents.map((n) => (
                  <option key={n} value={n}>
                    {n}
                  </option>
                ))}
              </select>
            )}
            <select
              className="ik-item-depth"
              value={depth}
              data-picked={depth || undefined}
              disabled={state !== "idle"}
              title={
                "How far to take this one on its own — just this start, not the whole queue" +
                (configuredDepth ? " (configured: " + DEPTH_LABELS[configuredDepth] + ")" : "")
              }
              aria-label={"How far to take " + reference}
              onChange={(e) => setDepth(e.target.value)}
            >
              {/* Always NAME what the empty choice resolves to. A bare
                  "Configured" made you open Settings to find out what pressing
                  Start would actually do; an unset source default means no
                  autopilot at all, which is "Off" — worth saying out loud. */}
              <option value="">
                {"Configured (" + DEPTH_LABELS[configuredDepth || "off"] + ")"}
              </option>
              {DEPTHS.map((d) => (
                <option key={d} value={d}>
                  {DEPTH_LABELS[d]}
                </option>
              ))}
            </select>
            {/* Thinking effort, and now it has a configured default to name like
                the other two: a ticketing source carries its own rung, so the
                empty choice says which rung this row would run at rather than
                the bare "Default effort" it used to show whatever the queue was
                set to. With no source opinion it still falls back to the CLI's
                own default, and the tooltip says where the CLI this row would
                launch tops out. A CLI with no effort setting gets the control
                disabled rather than an enabled one that does nothing. */}
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
                  : configuredEffort
                    ? "Configured (" + effortOptionLabel(configuredEffort, effortCap) + ")"
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
          </div>
          <button
            type="button"
            className={
              canReopen ? "test-btn ik-start-again" : "btn-primary pr-review-btn"
            }
            disabled={state !== "idle"}
            title={
              canReopen
                ? actionLabel + " starts a NEW session, leaving the workspace above alone"
                : undefined
            }
            onClick={async () => {
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
                });
                toast(created + " — provisioning, see the sidebar");
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
            {state === "starting" ? "Starting…" : state === "started" ? "Started" : actionLabel}
          </button>
        </div>
      )}
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
