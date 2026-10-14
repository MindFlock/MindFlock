/** `isCalm` (lib/calm.ts) read off the live caches: the person's own sessions
 * on this device, the setup record and the server's `onboarded` flag. One
 * hook so the sidebar, the pane headers and the rows agree on the moment the
 * full screen comes back. */

import { useConfig, useInstances } from "../state/queries";
import { useSetup } from "../components/setup/useSetup";
import { isVerifySession } from "../components/dialogs/verify";
import { isCalm } from "./calm";

export function useCalm(): boolean {
  const { data: instances } = useInstances();
  const { data: setup } = useSetup();
  const { data: config } = useConfig();
  if (!instances) return false;
  const sessions = instances.filter((i) => !i.device && !isVerifySession(i)).length;
  return isCalm({ sessions, status: setup, onboarded: config?.onboarded });
}
