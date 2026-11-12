/** Optimistic flight edits (2026-11-02, the owner: "block deletes happen far
 * too slowly, they should disappear right away" — and adds): the shared
 * flights query (`["flights"]`) is changed at once, the request goes out, and
 * a refusal puts the cache back; the refetch after a success replaces the
 * guess with the server's flight (a tacked-on stop's temporary id included).
 */
import { queryClient } from "../state/queries";
import type { Flight, FlightStep, StepRunState } from "./flights";

type Edit = (f: Flight) => Flight;

/** Apply `edit` to flight `id` in the cache now; returns the rollback. The
 * rollback puts back only this flight, and only while the cache still holds
 * our guess: a poll that landed meanwhile (other flights moved on, or this
 * one's server copy replaced the guess) and another press's edit are kept. */
export function patchFlightCache(id: string, edit: Edit): () => void {
  const key = ["flights"];
  void queryClient.cancelQueries({ queryKey: key });
  const before = queryClient.getQueryData<Flight[] | null>(key);
  const orig = before?.find((f) => f.id === id);
  if (!before || !orig) return () => {};
  queryClient.setQueryData<Flight[] | null>(key, before.map((f) => (f.id === id ? edit(f) : f)));
  // What the cache holds now (structural sharing may store a copy of ours).
  const guess = queryClient.getQueryData<Flight[] | null>(key)?.find((f) => f.id === id);
  return () => queryClient.setQueryData<Flight[] | null>(key, (now) => (now ? now.map((f) => (f === guess ? orig : f)) : now));
}

/** Run `request` with `edit` shown first; a refusal (false / a throw) rolls back. */
export async function optimistic(id: string, edit: Edit, request: () => Promise<boolean>): Promise<boolean> {
  const rollback = patchFlightCache(id, edit);
  try {
    const ok = await request();
    if (!ok) rollback();
    return ok;
  } catch (err) {
    rollback();
    throw err;
  }
}

/** The flight without the steps `ids` (the cursor follows what left before it). */
export function withoutSteps(ids: ReadonlySet<string>): Edit {
  return (f) => {
    const before = f.steps.slice(0, Math.max(0, f.cursor)).filter((s) => ids.has(s.id)).length;
    return { ...f, steps: f.steps.filter((s) => !ids.has(s.id)), cursor: Math.max(0, f.cursor - before) };
  };
}

let tmp = 0;
const PENDING: StepRunState = { state: "pending", since: 0, note: "", detail: {}, rounds: 0 };

/** The flight with `steps` inserted before index `at` (undefined = the end),
 * each under a temporary id until the server's flight replaces it. */
export function withSteps(steps: readonly FlightStep[], at?: number): Edit {
  return (f) => {
    const fresh = steps.map((s) => ({ ...s, id: "s_tmp" + ++tmp }));
    const i = at === undefined ? f.steps.length : Math.max(0, Math.min(at, f.steps.length));
    const states = { ...f.step_states };
    for (const s of fresh) states[s.id] = { ...PENDING, since: Date.now() / 1000 };
    return { ...f, steps: [...f.steps.slice(0, i), ...fresh, ...f.steps.slice(i)], step_states: states, cursor: i <= f.cursor && f.state !== "grounded" ? f.cursor + fresh.length : f.cursor };
  };
}

/** Flight `id` gone from the cache at once while `request` deletes it; a
 * refusal puts it back — where it was, among what the cache holds by then
 * (another delete meanwhile stays done), unless a poll already brought it. */
export async function optimisticDelete(id: string, request: () => Promise<boolean>): Promise<boolean> {
  const key = ["flights"];
  void queryClient.cancelQueries({ queryKey: key });
  const before = queryClient.getQueryData<Flight[] | null>(key);
  const at = before ? before.findIndex((f) => f.id === id) : -1;
  if (before && at >= 0) queryClient.setQueryData<Flight[] | null>(key, before.filter((f) => f.id !== id));
  const putBack = () => {
    if (!before || at < 0) return;
    const orig = before[at];
    const after = new Set(before.slice(at + 1).map((f) => f.id));
    queryClient.setQueryData<Flight[] | null>(key, (now) => {
      if (!now || now.some((f) => f.id === id)) return now;
      const i = now.findIndex((f) => after.has(f.id));
      return i < 0 ? [...now, orig] : [...now.slice(0, i), orig, ...now.slice(i)];
    });
  };
  try {
    const ok = await request();
    if (!ok) putBack();
    return ok;
  } catch (err) {
    putBack();
    throw err;
  }
}
