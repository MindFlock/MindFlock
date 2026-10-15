/** `isCalm` (lib/calm.ts) read off the live caches: the person's own sessions
 * on this device, the setup record and the server's `onboarded` flag. One
 * hook so the sidebar, the pane headers and the rows agree on the moment the
 * full screen comes back. While the server hasn't answered yet, the browser's
 * last decision (the calm render hint) picks the first paint, so the layout
 * doesn't flash dense and then collapse; with no hint either, calm — never
 * dense-then-calm (lib/calm.ts undecidedCalm). */

import { useEffect } from "react";
import { useConfig, useInstances } from "../state/queries";
import { useSetup } from "../components/setup/useSetup";
import { isVerifySession } from "../components/dialogs/verify";
import { calmDecision, readCalmHint, undecidedCalm, writeCalmHint } from "./calm";

export function useCalm(): boolean {
  const { data: instances } = useInstances();
  const { data: setup } = useSetup();
  const { data: config } = useConfig();
  const sessions = instances ? instances.filter((i) => !i.device && !isVerifySession(i)).length : null;
  // Decided only when both the session count and the setup record are in;
  // a fresh install's `onboarded: false` decides too (isNewcomer).
  const decided = sessions === null ? null : calmDecision({ sessions, status: setup, onboarded: config?.onboarded });
  useEffect(() => {
    if (decided !== null) writeCalmHint(decided);
  }, [decided]);
  if (decided !== null) return decided;
  const hint = readCalmHint();
  return undecidedCalm(hint);
}
