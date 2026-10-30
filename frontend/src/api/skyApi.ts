/** The Sky's own routes (docs/flights.md "As built: round 15 objects"):
 * the landing strip's data and the sticky notes, both served by
 * backend/web/routes/sky.py.
 *
 *  - GET /api/sky/landing        → {repos: [LandingRepo]}: every repo a session
 *                                  in the sky lives in — its default branch,
 *                                  the checks of that branch's head, the last
 *                                  merge's time, and the flights that landed
 *                                  (finished done, last ship step merged) in
 *                                  the last 24 h. Cached ~60 s on the server.
 *  - GET / PUT /api/sky/notes    → {notes: [SkyNote]}: the sticky notes, stored
 *                                  in <flights dir>/sky_notes.json (per
 *                                  server, never synced). PUT replaces them.
 *  - POST /api/instances/{title}/move {device, force}: a line dropped on a
 *                                  device block (the existing move route). */

import { api, instApi } from "./client";

export type CheckTone = "green" | "red" | "pending" | "none";

export interface LandedFlight {
  flight: string;
  /** The session the flight flew on (a title: show it through windowName). */
  title: string;
  pr_url: string;
  /** Unix seconds. */
  at: number;
}

export interface LandingRepo {
  root: string;
  /** The folder's name, for the strip's row. */
  name: string;
  branch: string;
  /** The default branch's head sha ("" when unknown). */
  head: string;
  checks: CheckTone;
  /** The last merge commit's time (unix seconds), null when there is none. */
  last_merge_at: number | null;
  landed: LandedFlight[];
}

export interface LandingData {
  repos: LandingRepo[];
  /** When the server built it (unix seconds). */
  at: number;
}

export function fetchLanding(): Promise<LandingData> {
  return api<LandingData>("/api/sky/landing");
}

export interface SkyNote {
  id: string;
  text: string;
  /** Plane px; relative to the line's spot when `line` is set. */
  x: number;
  y: number;
  w: number;
  h: number;
  /** The line (a session title or a grounded key) it is attached to: it
   * moves with that line. "" = free on the sky. */
  line: string;
  /** Unix seconds of the last edit. */
  updated: number;
}

export async function fetchSkyNotes(): Promise<SkyNote[]> {
  const d = await api<{ notes?: SkyNote[] }>("/api/sky/notes");
  return d?.notes || [];
}

export async function saveSkyNotes(notes: SkyNote[]): Promise<SkyNote[]> {
  const d = await api<{ notes?: SkyNote[] }>("/api/sky/notes", { method: "PUT", json: { notes } });
  return d?.notes || notes;
}

/** A line dropped on a device block: the existing move route with the device
 * as the target. Resolves to the server's 202 body; rejects with the route's
 * refusal (lib/sessionActions `moveRefusal` reads it). */
export function moveSessionToDevice(title: string, device: string, force = false): Promise<{ title?: string; from?: string; to?: string }> {
  return instApi(title, "/move", { method: "POST", json: { device, force } });
}
