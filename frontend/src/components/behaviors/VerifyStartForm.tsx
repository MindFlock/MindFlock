/** Starting a Verify by hand: what it should prove (optional) and, where the
 * session's repo has Verify environments, where — then Start. Drawn IN PLACE
 * in the pane's verify row (components/grid/PaneVerifyStrip), never as a modal
 * over the terminal. "Verify" on the ⋯ menu and "Verify again" on the row open
 * it (`openVerifyForm`); Enter with an empty field starts at once.
 *
 * Keyboard: every control is a real input/select/button (Tab reaches each),
 * Enter in the field submits (Shift+Enter is a new line), Escape closes it and
 * returns focus to the button that opened it. */

import { useEffect, useRef, useState, type JSX } from "react";
import { create } from "zustand";
import { openWalkingSession, startSessionVerify, VERIFY_WHERE_BRANCH } from "../../lib/verifyActions";
import type { VerifyEnvsResponse } from "../../lib/verifyStop";
import { useVerifyEnvironments } from "../../state/queries";

// --- Which pane's form is open ------------------------------------------------

interface VerifyFormState {
  /** The session whose pane shows the form ("" = none). */
  open: string;
  /** The control that opened it — focus goes back there on close. */
  opener: HTMLElement | null;
}

export const useVerifyForm = create<VerifyFormState>(() => ({ open: "", opener: null }));

/** Panes whose verify row is mounted, by session (a count: a session can be
 * in two panes). The ⋯ menu opens the form in place only where one is. */
const mounted = new Map<string, number>();

export function markVerifyRowMounted(title: string): () => void {
  mounted.set(title, (mounted.get(title) || 0) + 1);
  return () => {
    const n = (mounted.get(title) || 1) - 1;
    if (n > 0) mounted.set(title, n);
    else mounted.delete(title);
  };
}

export function verifyRowMounted(title: string): boolean {
  return (mounted.get(title) || 0) > 0;
}

/** How long the ⋯ menu waits for the session's pane to show its verify row
 * before starting at once (the old one-press behaviour). */
const MOUNT_WAIT_MS = 600;

/** Open the start form in `title`'s pane. When no pane of it shows its verify
 * row, its pane is opened first; if the row still isn't there a moment later
 * (an older pane that draws the row only once a verify exists), Verify starts
 * at once, as it did before the form. */
export function openVerifyForm(title: string, opener?: HTMLElement | null): void {
  if (!title) return;
  const from = opener ?? (typeof document !== "undefined" ? (document.activeElement as HTMLElement | null) : null);
  useVerifyForm.setState({ open: title, opener: from });
  if (verifyRowMounted(title)) return;
  openWalkingSession(title);
  setTimeout(() => {
    if (verifyRowMounted(title) || useVerifyForm.getState().open !== title) return;
    useVerifyForm.setState({ open: "", opener: null });
    void startSessionVerify(title);
  }, MOUNT_WAIT_MS);
}

/** Close the form; focus returns to whatever opened it when that is still on
 * the page, else to `fallback` (the row's own button). */
export function closeVerifyForm(fallback?: HTMLElement | null): void {
  const { opener } = useVerifyForm.getState();
  useVerifyForm.setState({ open: "", opener: null });
  const target = opener && opener.isConnected ? opener : fallback;
  if (target && typeof target.focus === "function") target.focus();
}

// --- Pure helpers ------------------------------------------------------------

export interface WhereChoice {
  value: string;
  label: string;
}

/** The Where choices for a session's repo: "This branch", then the repo's
 * Verify environments — or [] when it has none (no select is drawn). `repo`
 * is the row's repo name; the server lists environments by "owner/name",
 * matched on the name. */
export function startFormWhere(envs: VerifyEnvsResponse | null | undefined, repo: string | undefined): WhereChoice[] {
  const want = String(repo || "").trim().toLowerCase();
  if (!want || !envs?.repos) return [];
  const names: string[] = [];
  for (const [slug, list] of Object.entries(envs.repos)) {
    const name = slug.split("/").pop()?.trim().toLowerCase() || "";
    if (name !== want && slug.trim().toLowerCase() !== want) continue;
    for (const e of list || []) if (e?.name && e.name !== VERIFY_WHERE_BRANCH && !names.includes(e.name)) names.push(e.name);
  }
  if (!names.length) return [];
  return [{ value: VERIFY_WHERE_BRANCH, label: "This branch" }, ...names.map((n) => ({ value: n, label: n }))];
}

// --- The form ----------------------------------------------------------------

export interface VerifyStartFormProps {
  title: string;
  /** The session's repo name — picks its Verify environments. */
  repo?: string;
  /** Called after Start was taken, or on Cancel / Escape. */
  onClose(): void;
}

export function VerifyStartForm({ title, repo, onClose }: VerifyStartFormProps): JSX.Element {
  const [notes, setNotes] = useState("");
  const [where, setWhere] = useState(VERIFY_WHERE_BRANCH);
  const [busy, setBusy] = useState(false);
  const field = useRef<HTMLTextAreaElement | null>(null);
  const envs = useVerifyEnvironments(!!repo);
  const choices = startFormWhere(envs.data, repo);
  useEffect(() => {
    field.current?.focus();
  }, []);
  const start = async () => {
    if (busy) return;
    setBusy(true);
    try {
      const ok = await startSessionVerify(title, { notes, environment: where, open: false });
      if (ok) onClose();
    } finally {
      setBusy(false);
    }
  };
  return (
    <form
      className="pv-form"
      aria-label={"Verify " + title}
      data-no-drag
      onMouseDown={(e) => e.stopPropagation()}
      onSubmit={(e) => {
        e.preventDefault();
        void start();
      }}
      onKeyDown={(e) => {
        // The terminal and the grid's shortcuts must not see these keys.
        e.stopPropagation();
        if (e.key === "Escape") {
          e.preventDefault();
          onClose();
        }
      }}
    >
      <textarea
        ref={field}
        className="fl-input pv-form-notes"
        rows={1}
        placeholder="What should it prove? (optional)"
        aria-label="What should it prove? (optional)"
        value={notes}
        disabled={busy}
        onChange={(e) => setNotes(e.target.value)}
        onKeyDown={(e) => {
          if (e.key === "Enter" && !e.shiftKey && !e.nativeEvent.isComposing) {
            e.preventDefault();
            void start();
          }
        }}
      />
      {choices.length ? (
        <label className="pv-form-where">
          <span>Where</span>
          <select className="fl-input" value={where} disabled={busy} onChange={(e) => setWhere(e.target.value)}>
            {choices.map((c) => (
              <option key={c.value} value={c.value}>
                {c.label}
              </option>
            ))}
          </select>
        </label>
      ) : null}
      <button type="submit" className="btn-primary pv-form-start" disabled={busy} aria-busy={busy || undefined}>
        Start
      </button>
      <button type="button" className="test-btn pv-form-cancel" disabled={busy} onClick={onClose}>
        Cancel
      </button>
    </form>
  );
}
