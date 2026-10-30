/** A stop's `when` (docs/flights.md "Round 15" → "`when`: a condition on
 * every stop"): the grammar the stop form's ⓘ lists, a cheap client-side
 * sanity check, the server's validator (`GET /api/flights/when`) and the
 * debounced checker the "Only when" field drives — pure but for the fetch,
 * so the tests read it without a DOM.
 *
 * An older server has no validator (404): the field still edits and saves,
 * it just isn't checked live. */

import { ApiError, api } from "../api/client";
import type { StepRunState } from "./flights";

/** One row of the grammar: what to type, and when it is true. */
export interface WhenRow {
  condition: string;
  meaning: string;
}

/** The contract's grammar table, in its order. */
export const WHEN_GRAMMAR: readonly WhenRow[] = [
  { condition: "diff touches <glob>[, <glob>…]", meaning: "a changed file (vs the base) matches any glob: frontend/**, *.sql, migrations/" },
  { condition: "diff lines > N  /  < N", meaning: "added + removed lines vs the base" },
  { condition: "files > N  /  < N", meaning: "changed files vs the base" },
  { condition: "weekday  /  weekend", meaning: "the server's local day" },
  { condition: "hour between H and H", meaning: "the local hour is in [H, H) — may wrap past midnight" },
  { condition: "branch matches <glob>", meaning: "the session's branch" },
  { condition: "label <name>", meaning: "the PR / issue carries that label" },
  { condition: "judge is <value>  /  judge <sid> is <value>", meaning: "the nearest earlier Judge stop's answer (or that stop's)" },
  { condition: "always  /  never", meaning: "—" },
];

/** How the conditions combine, under the table. */
export const WHEN_COMBINE = "Case doesn't matter. Combine with and / or (and binds tighter), put not in front to flip one.";

/** The longest `when` the server keeps. */
export const WHEN_MAX = 300;

/** How long the field waits after the last keystroke before it asks. */
export const WHEN_DEBOUNCE_MS = 300;

/** Worth asking the server about: something on one line, not too long. */
export function looksLikeWhen(text: string | null | undefined): boolean {
  const t = String(text ?? "");
  return !!t.trim() && !/[\r\n]/.test(t) && t.length <= WHEN_MAX;
}

/** `GET /api/flights/when`'s answer: `canonical` is the expression as the
 * server writes it back, `error` (with the `field` it names) when not ok. */
export interface WhenCheck {
  ok: boolean;
  canonical: string;
  error: string;
  field: string;
}

/** Ask the server whether `expr` parses. Null when this server has no
 * validator (404 — an older one) or couldn't answer: no live validation
 * then, never an error under the field for the server's own trouble. A
 * refusal that still carries the contract's shape is read as the answer. */
export async function checkWhen(expr: string): Promise<WhenCheck | null> {
  try {
    return asCheck(await api<unknown>("/api/flights/when?expr=" + encodeURIComponent(expr)));
  } catch (err) {
    if (err instanceof ApiError && err.status !== 404) return asCheck(err.body);
    return null;
  }
}

function asCheck(d: unknown): WhenCheck | null {
  if (!d || typeof d !== "object" || typeof (d as WhenCheck).ok !== "boolean") return null;
  const r = d as Partial<WhenCheck>;
  return { ok: !!r.ok, canonical: String(r.canonical ?? ""), error: String(r.error ?? ""), field: String(r.field ?? "") };
}

/** What the field shows under itself for `text` and the answer for it. */
export function whenError(text: string, res: WhenCheck | null): string {
  if (!String(text ?? "").trim()) return "";
  if (/[\r\n]/.test(text)) return "One line, please.";
  if (text.length > WHEN_MAX) return "Too long — " + WHEN_MAX + " characters at most.";
  return res && !res.ok ? res.error || "That condition doesn't parse." : "";
}

export interface WhenChecker {
  /** A keystroke: (re)start the wait, then ask. */
  input(text: string): void;
  /** The field lost focus: ask now (or reuse the answer for this text), and
   * hand the canonical form back when it differs. */
  blur(text: string): void;
  /** The field is being typed in again: a pending blur no longer applies. */
  focus(): void;
  /** Unmount: nothing more is asked or answered. */
  cancel(): void;
}

/** The "Only when" field's validation, debounced: the latest text only
 * (a slow answer for older text is dropped), the empty text is no condition
 * (cleared, never asked), text that isn't one line is refused locally, and
 * once the server says it has no validator (`check` → null) it is not asked
 * again. `onResult(text, res)` reports the answer for `text`;
 * `onCanonical(c)` fires after a blur when the server's form differs. */
export function whenChecker(opts: {
  check(expr: string): Promise<WhenCheck | null>;
  onResult(text: string, res: WhenCheck | null): void;
  onCanonical(canonical: string): void;
  delay?: number;
}): WhenChecker {
  const delay = opts.delay ?? WHEN_DEBOUNCE_MS;
  let latest = "";
  let timer: ReturnType<typeof setTimeout> | null = null;
  let answered: { text: string; res: WhenCheck | null } | null = null;
  let blurred = false;
  let unsupported = false;
  let dead = false;
  const stop = () => {
    if (timer) clearTimeout(timer);
    timer = null;
  };
  const settle = (text: string, res: WhenCheck | null) => {
    if (dead || text !== latest) return;
    answered = { text, res };
    opts.onResult(text, res);
    if (blurred && res?.ok && res.canonical && res.canonical !== text) opts.onCanonical(res.canonical);
  };
  const ask = (text: string) => {
    stop();
    if (answered?.text === text) return settle(text, answered.res);
    if (unsupported || !looksLikeWhen(text)) return settle(text, null);
    void opts.check(text).then(
      (res) => {
        if (res === null) unsupported = true;
        settle(text, res);
      },
      () => settle(text, null)
    );
  };
  return {
    input(text) {
      latest = text;
      blurred = false;
      stop();
      if (!text.trim() || unsupported || !looksLikeWhen(text)) return settle(text, null);
      timer = setTimeout(() => ask(text), delay);
    },
    blur(text) {
      latest = text;
      blurred = true;
      if (text.trim()) ask(text);
    },
    focus() {
      blurred = false;
    },
    cancel() {
      dead = true;
      stop();
    },
  };
}

/** What a skipped stop says about itself ("skipped: <expr> — <the fact>"):
 * its note, or the bare word when the server left none. Null for any other
 * state. */
export function skippedNote(run: Pick<StepRunState, "state" | "note"> | null | undefined): string | null {
  if (run?.state !== "skipped") return null;
  return run.note?.trim() || "skipped";
}
