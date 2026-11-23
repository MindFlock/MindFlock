/** "+ Shared" on a file a session shows (the Diff tab's file rows): the
 * file goes from that session's folder into Shared files (POST /api/instances/
 * {title}/hub/put — inside its folder only, no secrets, its fence and red
 * zones respected) and its reference is copied. */

import { instApi } from "../../api/client";
import { copyText } from "../../lib/clipboard";
import { errMsg } from "../../lib/format";
import { refreshHub, type HubItem } from "../../lib/hub";
import { toast } from "../../lib/toast";
import { useConfig } from "../../state/queries";

export async function addSessionFile(title: string, path: string): Promise<HubItem | null> {
  try {
    const it = await instApi<HubItem>(title, "/hub/put", { json: { path } });
    refreshHub();
    const ok = await copyText(it.ref);
    toast("Added " + it.name + " to Shared files — " + it.ref + (ok ? " (copied)" : ""));
    return it;
  } catch (err) {
    toast("Couldn't add " + path + " to Shared files: " + errMsg(err));
    return null;
  }
}

export function HubAddButton({ title, path }: { title: string; path: string }) {
  const guest = !!useConfig().data?.guest;
  if (guest) return null;
  return (
    <button
      type="button"
      className="diff-file-map diff-file-hub"
      title={"Add " + path + " to Shared files — every session can use it"}
      aria-label={"Add " + path + " to Shared files"}
      onClick={(ev) => {
        ev.stopPropagation();
        void addSessionFile(title, path);
      }}
    >
      <span>+ Shared</span>
    </button>
  );
}
