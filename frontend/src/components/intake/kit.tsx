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

import { useState } from "react";
import { toast } from "../../lib/toast";

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

/** Master on/off switch plus the one-line status underneath it.
 *
 * The two are one component because the status is only ever a readout of the
 * switch and the source count; keeping them together is what stops the three
 * tabs from describing the same three states in three ways. */
export function AutomationSwitch({
  label,
  title,
  rowId,
  inputId,
  statusId,
  checked,
  onChange,
  status,
  tone,
}: {
  label: string;
  title: string;
  rowId?: string;
  inputId?: string;
  statusId?: string;
  checked: boolean;
  onChange(next: boolean): void;
  /** The status sentence. Convention: "● " active, "‖ " paused, "○ " not set up. */
  status: string;
  /** "on" tints it live, "paused" tints it muted, "" is the not-yet-set-up grey. */
  tone: "on" | "paused" | "";
}) {
  return (
    <>
      <div className="set-row set-switch-row" id={rowId} title={title}>
        <span className="set-label">{label}</span>
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
      <div id={statusId} className={"pr-status" + (tone ? " " + tone : "")}>
        {status}
      </div>
    </>
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
   * created session's title for the toast. */
  onStart(agent: string): Promise<string>;
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
}) {
  const [state, setState] = useState<"idle" | "starting" | "started">("idle");
  // Per-start override, not persisted: it applies to THIS launch. Changing the
  // queue's CLI is a card edit; running one item elsewhere is this.
  const [agent, setAgent] = useState("");
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
          (reasons || []).map((reason) => (
            // title: a recorded failure reason is a full sentence of git output
            // whose actionable half is at the end, so the chip wraps to keep it
            // visible (see .pr-open-chip) and hovering still gives the raw
            // string on one line.
            <span className="pr-open-chip" key={reason} title={reason}>
              {reason}
            </span>
          ))
        )}
      </div>
      {hasSession ? (
        <button type="button" className="btn-primary pr-review-btn" disabled>
          Session open
        </button>
      ) : (
        <div className="ik-item-start">
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
                {configuredAgent ? "Configured (" + configuredAgent + ")" : "Configured"}
              </option>
              {agents.map((n) => (
                <option key={n} value={n}>
                  {n}
                </option>
              ))}
            </select>
          )}
          <button
            type="button"
            className="btn-primary pr-review-btn"
            disabled={state !== "idle"}
            onClick={async () => {
              setState("starting");
              try {
                const created = await onStart(agent);
                toast(created + " — provisioning, see the sidebar");
                setState("started");
              } catch (err) {
                toast(failPrefix + ": " + ((err as Error).message || "error"));
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
