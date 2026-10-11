/** `GET /api/setup` as a query, shared by the grid card, the chip and the
 * Setup modal (one request, however many of them are mounted). The server's
 * doctor result rides along in the payload (its own 30 s cache), so a
 * `refreshSetup(true)` after an install or a sign-in asks it to re-probe. */

import { useQuery } from "@tanstack/react-query";
import { api } from "../../api/client";
import { queryClient } from "../../state/queries";
import type { SetupStatus } from "../../api/types";

export const SETUP_KEY = ["setup"] as const;

export function useSetup(enabled = true) {
  return useQuery({
    queryKey: SETUP_KEY,
    queryFn: () => api<SetupStatus>("/api/setup"),
    staleTime: 30_000,
    enabled,
  });
}

/** Re-read the checklist. `reprobe` also re-runs the doctor (slow: it shells
 * out), for right after something was installed or signed in. */
export async function refreshSetup(reprobe = false): Promise<void> {
  if (!reprobe) {
    await queryClient.invalidateQueries({ queryKey: SETUP_KEY });
    return;
  }
  try {
    const d = await api<SetupStatus>("/api/setup?refresh=1");
    queryClient.setQueryData(SETUP_KEY, d);
  } catch {
    await queryClient.invalidateQueries({ queryKey: SETUP_KEY });
  }
}

/** Skip (or un-skip) an optional step; the payload's steps/summary update in
 * place. Throws on a refusal so the caller can toast it. */
export async function skipSetupStep(step: string, undo = false): Promise<void> {
  const r = await api<Pick<SetupStatus, "steps" | "summary">>(
    "/api/setup/" + encodeURIComponent(step) + (undo ? "/unskip" : "/skip"),
    { method: "POST" }
  );
  queryClient.setQueryData<SetupStatus | undefined>(SETUP_KEY, (old) =>
    old ? { ...old, steps: r.steps, summary: r.summary } : old
  );
}
