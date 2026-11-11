/** "Memory is low": the confirm a person's own start gets while the machine
 * is short of memory (backend core/launch_gate.py).
 *
 * A person's New session, Duplicate, Start, Start all, Resume and Wake are
 * never queued like an agent's starts, but while memory reads low or
 * critical NOW (never over a projection: those pace automatic starts only)
 * the server answers 409 `{memory_confirm: true, level, reason, message}`
 * instead of starting; `message` leads with the signals behind the level,
 * so the dialog never says "critically low" over 13 GB free without the
 * stall that set it. The app
 * asks (components/MemoryConfirm.tsx):
 *  - "Start anyway" resends the request with `start_anyway: true`;
 *  - "Put idle agents to sleep first" asks the server to sleep the idle,
 *    unwatched agents (`POST /api/server/sleep-idle`), then resends the
 *    plain request (which may ask again if that freed too little);
 *  - "Cancel" starts nothing.
 * Critical memory is the same dialog with stronger words.
 *
 * Electron has no window.confirm, so this is a promise the dialog settles:
 * `withMemoryConfirm(send)` wraps any start. */

import { api, type ApiError } from "../api/client";

/** The 409 body (core/launch_gate.person_refusal). */
export interface MemoryConfirmBody {
  memory_confirm: true;
  level: "low" | "critical";
  /** The dialog's own line, leading with the reason: "Memory is critical:
   * processes stalled on memory 34% of the last minute. Starting another
   * agent will likely freeze the machine." (an older snapshot without `why`:
   * "Memory is low: 3.1 GB free. …"). */
  message: string;
  /** The verdict's reason alone (memory_pressure.describe), "" if none. */
  reason?: string;
  error?: string;
  free?: number;
  total?: number;
  warming?: number;
  live?: number | null;
}

export type MemoryChoice = "start" | "sleep" | "cancel";

/** The memory-confirm body of a failed request, or null for any other
 * failure. */
export function memoryConfirmOf(e: unknown): MemoryConfirmBody | null {
  // Read by shape (an ApiError's status + body), not instanceof: a caller's
  // module may hold a different copy of the class (a test's client mock).
  const err = e as Partial<ApiError> | null;
  if (!err || typeof err !== "object" || err.status !== 409) return null;
  const b = err.body as Partial<MemoryConfirmBody> | null;
  if (!b || typeof b !== "object" || b.memory_confirm !== true) return null;
  return {
    memory_confirm: true,
    level: b.level === "critical" ? "critical" : "low",
    message: typeof b.message === "string" && b.message ? b.message : "Memory is low.",
    error: b.error,
    reason: typeof b.reason === "string" ? b.reason : undefined,
    free: b.free,
    total: b.total,
    warming: b.warming,
    live: b.live,
  };
}

/** What the dialog is showing: the body, what is being started, and the
 * promise's resolver. */
export interface PendingConfirm {
  body: MemoryConfirmBody;
  what: string;
  /** False once the idle agents were already put to sleep for this start:
   * the dialog then offers only Start anyway / Cancel. */
  canSleep: boolean;
  resolve(choice: MemoryChoice): void;
}

let pending: PendingConfirm | null = null;
const listeners = new Set<() => void>();

function emit() {
  for (const l of listeners) l();
}

export function subscribeMemoryConfirm(fn: () => void): () => void {
  listeners.add(fn);
  return () => listeners.delete(fn);
}

export function currentMemoryConfirm(): PendingConfirm | null {
  return pending;
}

/** Show the dialog and wait for the person's choice. A second ask while one
 * is open answers "cancel" for the older one (one dialog at a time). */
export function askMemoryConfirm(
  body: MemoryConfirmBody,
  what: string,
  canSleep = true
): Promise<MemoryChoice> {
  return new Promise((resolve) => {
    if (pending) pending.resolve("cancel");
    const me: PendingConfirm = {
      body,
      what,
      canSleep,
      resolve(choice) {
        if (pending === me) {
          pending = null;
          emit();
        }
        resolve(choice);
      },
    };
    pending = me;
    emit();
  });
}

/** The sleep-idle answer. */
export interface SleepIdleAnswer {
  slept: string[];
  freed_mb: number;
}

/** Hooks the wrapper uses, swappable in tests. */
export const memoryConfirmDeps = {
  ask: askMemoryConfirm,
  sleepIdle: () => api<SleepIdleAnswer>("/api/server/sleep-idle", { json: {} }),
};

/** Thrown by `withMemoryConfirm` when the person chose Cancel. Callers that
 * show their own failure toast skip it (`isMemoryCancel`). */
export class MemoryCancelled extends Error {
  constructor() {
    super("Not started: memory is low");
  }
}

export function isMemoryCancel(e: unknown): boolean {
  return e instanceof MemoryCancelled;
}

/** Run a person's start `send(startAnyway)`. On a memory-confirm 409 ask, then
 * resend as the person chose. Rethrows every other failure, and
 * MemoryCancelled on Cancel. `onSlept` hears what the sleep choice freed. */
export async function withMemoryConfirm<T>(
  what: string,
  send: (startAnyway: boolean) => Promise<T>,
  onSlept?: (a: SleepIdleAnswer) => void
): Promise<T> {
  let slept = false;
  for (;;) {
    try {
      return await send(false);
    } catch (e) {
      const body = memoryConfirmOf(e);
      if (!body) throw e;
      const choice = await memoryConfirmDeps.ask(body, what, !slept);
      if (choice === "start") return await send(true);
      if (choice === "cancel" || slept) throw new MemoryCancelled();
      slept = true;
      const a = await memoryConfirmDeps.sleepIdle();
      onSlept?.(a);
    }
  }
}

/** "Put 3 idle agents to sleep (freed ~1.2 GB)." / "No agent was idle enough
 * to sleep." */
export function sleptLine(a: SleepIdleAnswer): string {
  const n = a.slept.length;
  if (!n) return "No agent was idle and unwatched, so none was put to sleep.";
  const freed = a.freed_mb >= 1024 ? (a.freed_mb / 1024).toFixed(1) + " GB" : a.freed_mb + " MB";
  return "Put " + n + " idle agent" + (n === 1 ? "" : "s") + " to sleep (freed ~" + freed + ").";
}
