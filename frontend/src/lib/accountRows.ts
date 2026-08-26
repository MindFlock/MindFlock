/** The account-swap menu's rows, as a pure function of the session's stored
 * pin — so the one thing that is easy to get wrong here is testable.
 *
 * The pin is a tri-state, the same one used for launch flags and on the
 * server: `""` = follow the app-wide default, `"default"` = explicitly the
 * CLI's own ambient login, anything else = that profile id. A menu that
 * checkmarks the *resolved* identity instead of the pin looks right in every
 * case but one — the session that is FOLLOWING the app default — and clicking
 * its own checked row would then quietly convert it into a pinned session that
 * stops tracking the default. So the rows are built from the pin, and the
 * inherit row carries `""`.
 *
 * With no app default configured, `""` and `"default"` mean the same thing, so
 * the inherit row would be a second spelling of the ambient row: it is left
 * out rather than shown as a duplicate.
 */

import type { AuthProfile } from "../api/types";

export interface AccountRow {
  /** What a click sends as `profile_id`. */
  id: string;
  label: string;
  current: boolean;
}

export function accountRows(
  pin: string,
  defaultProfileId: string,
  profiles: AuthProfile[]
): AccountRow[] {
  const appDefault = profiles.find((p) => p.id === defaultProfileId);
  const inheriting = pin === "" && !!appDefault;
  const onAmbient = pin === "default" || (pin === "" && !appDefault);
  const rows: AccountRow[] = [];
  if (appDefault) {
    rows.push({
      id: "",
      label: "App default (" + (appDefault.label || appDefault.id) + ")",
      current: inheriting,
    });
  }
  rows.push({ id: "default", label: "CLI's own login", current: onAmbient });
  for (const p of profiles) {
    rows.push({ id: p.id, label: p.label || p.id, current: pin === p.id });
  }
  return rows;
}

/** How a swap reads back in a toast. Mirrors `accountRows` labels. */
export function swapLabel(profileId: string, profiles: AuthProfile[]): string {
  if (profileId === "default") return "the CLI's own login";
  if (profileId === "") return "the app default account";
  return profiles.find((p) => p.id === profileId)?.label || profileId;
}
