/** One action at a time on a People card: its busy flag (which action), its
 * inline error (the server's sentence), an optional toast on success, and a
 * reload of the screen afterwards either way. */

import { useState } from "react";
import { toast } from "../../../../lib/toast";

export function errText(e: unknown): string {
  return e instanceof Error ? e.message : String(e);
}

export function useAct(reload: () => void) {
  const [busy, setBusy] = useState("");
  const [err, setErr] = useState("");
  const act = async (what: string, fn: () => Promise<unknown>, ok?: string): Promise<boolean> => {
    setBusy(what);
    setErr("");
    let done = false;
    try {
      await fn();
      done = true;
      if (ok) toast(ok);
    } catch (e) {
      setErr(errText(e));
    }
    setBusy("");
    reload();
    return done;
  };
  return { busy, err, setErr, act };
}
