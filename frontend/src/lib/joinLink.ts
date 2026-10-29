/** Join by link, the app's half. An invite link opens the server's /join
 * page, whose "Open in MindFlock" button is `mindflock://join?code=…`; the
 * desktop shell (electron/main.js) turns that into this app's own URL with
 * `#join=<code>`. Here the code leaves the address bar at once and People
 * opens with Join someone prefilled — joining is still the person's click. */

import { openJoinWithCode } from "./peerActions";

const EXACT_CODE_RE = /^mfp[12]:[a-z2-7]+-[a-z2-7]{4}$/;

/** The invite code in `#join=<code>` of `href`, lowercased, or "" (no hash,
 * another hash, or anything that isn't exactly one code). */
export function joinCodeFromLocation(href: string): string {
  let hash: string;
  try {
    hash = new URL(href).hash;
  } catch {
    return "";
  }
  const code = (new URLSearchParams(hash.replace(/^#/, "")).get("join") || "").trim().toLowerCase();
  return EXACT_CODE_RE.test(code) ? code : "";
}

/** Take a `#join=` code from the current URL, now and whenever the hash
 * changes (the shell re-pointing an open window only changes the hash, which
 * reloads nothing). */
export function installJoinLink(): void {
  const take = () => {
    const code = joinCodeFromLocation(window.location.href);
    if (!code) return;
    // Out of the address bar and history before anything else reads it.
    history.replaceState(history.state, "", window.location.pathname + window.location.search);
    openJoinWithCode(code);
  };
  take();
  window.addEventListener("hashchange", take);
}
