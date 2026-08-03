/** The shared shell behind the three "MindFlock watches X and starts sessions"
 * screens: PR review, Git issues and Ticketing.
 *
 * They had drifted into three dialects of the same screen — same master switch,
 * same status line, same watched-things list, same open-work panel with a
 * force-start button, same Advanced block — with different wording, different
 * ordering and, in Ticketing's case, a missing status line and a differently
 * named agent field. Every difference was incidental, and each one cost a reader
 * a moment working out whether it meant something.
 *
 * The pieces here are the vocabulary all three now speak. What legitimately
 * differs (Ticketing is multi-source; PR review owns the shared GitHub token)
 * stays in the individual screens.
 */

import { useState } from "react";
import { toast } from "../../../lib/toast";

/** "3h old" / "20m old" / "2d old" — one implementation, three screens. */
export function ageText(iso?: string): string {
  const t = Date.parse(iso || "");
  if (!isFinite(t)) return "";
  const mins = Math.max(0, Math.round((Date.now() - t) / 60000));
  if (mins < 60) return mins + "m old";
  const h = Math.round(mins / 60);
  if (h < 48) return h + "h old";
  return Math.round(h / 24) + "d old";
}

/** Master on/off switch plus the one-line status underneath it.
 *
 * The two are one component because the status is only ever a readout of the
 * switch and the watched-list size; keeping them together is what stops the
 * three screens from describing the same three states in three ways. */
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

/** The `owner/name` chip list + add row that PR review and Git issues both use
 * to choose what gets watched. */
export function RepoListField({
  label,
  repos,
  onSave,
  emptyText,
  hint,
  listId,
  inputId,
  addId,
}: {
  label: string;
  repos: string[];
  /** Called with the new list and a toast message. */
  onSave(next: string[], msg: string): void;
  emptyText: string;
  hint: React.ReactNode;
  listId?: string;
  inputId?: string;
  addId?: string;
}) {
  const [draft, setDraft] = useState("");

  const add = () => {
    const val = draft.trim();
    if (!val) return;
    if (!/^[^\s/]+\/[^\s/]+$/.test(val)) {
      toast("Use owner/name, e.g. MindFlock/MindFlock");
      return;
    }
    if (repos.some((r) => r.toLowerCase() === val.toLowerCase())) {
      setDraft("");
      toast(val + " is already in the list");
      return;
    }
    setDraft("");
    onSave([...repos, val], "Added " + val);
  };

  return (
    <div className="set-row">
      <span className="set-label">{label}</span>
      <div id={listId} className="repo-list">
        {!repos.length ? (
          <div className="repo-empty">{emptyText}</div>
        ) : (
          repos.map((repo) => (
            <span className="repo-chip" key={repo}>
              <span className="repo-chip-name">{repo}</span>
              <button
                type="button"
                className="repo-chip-x"
                title={"Remove " + repo}
                aria-label={"Remove " + repo}
                onClick={() =>
                  onSave(
                    repos.filter((r) => r !== repo),
                    "Removed " + repo
                  )
                }
              >
                ✕
              </button>
            </span>
          ))
        )}
      </div>
      <div className="repo-add-row">
        <input
          type="text"
          id={inputId}
          placeholder="owner/name — e.g. mindflockai/MindFlock"
          autoComplete="off"
          spellCheck={false}
          value={draft}
          onChange={(e) => setDraft(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter") {
              e.preventDefault();
              add();
            }
          }}
        />
        <button type="button" id={addId} className="btn-primary" onClick={add}>
          + Add
        </button>
      </div>
      <span className="set-hint">{hint}</span>
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
  /** Ticketing puts its bucket picker here. */
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
      <div id={listId} className="pr-open-list">
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
  /** Starts the session; resolve with the created session's title for the toast. */
  onStart(): Promise<string>;
  /** e.g. "Begin review failed" */
  failPrefix: string;
}) {
  const [state, setState] = useState<"idle" | "starting" | "started">("idle");
  return (
    <div className="pr-open-item" title={tooltip}>
      <div className="pr-open-main">
        <a
          href={url || "#"}
          target="_blank"
          rel="noopener noreferrer"
          className="pr-open-ref"
          title={"Open " + reference + " on GitHub"}
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
            <span className="pr-open-chip" key={reason}>
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
        <button
          type="button"
          className="btn-primary pr-review-btn"
          disabled={state !== "idle"}
          onClick={async () => {
            setState("starting");
            try {
              const created = await onStart();
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
      )}
    </div>
  );
}
