/** Pane-header account chip: which auth profile this session's agent runs as,
 * click to hot-swap. Renders nothing until at least one profile exists, so the
 * header stays unchanged for anyone not using the feature. A swap persists the
 * pin and restarts the agent under the new identity server-side (the worktree,
 * shell pane and diff survive; the conversation resumes). */

import { useRef, useState } from "react";
import { instApi } from "../../api/client";
import type { Instance } from "../../api/types";
import { toast } from "../../lib/toast";
import { refreshInstances, useAuthProfiles } from "../../state/queries";
import { UsagePopover } from "../usage/UsagePopover";

export function AccountChip({ inst }: { inst: Instance }) {
  const chipRef = useRef<HTMLSpanElement | null>(null);
  const [open, setOpen] = useState(false);
  const [busy, setBusy] = useState(false);
  const { data } = useAuthProfiles();
  const profiles = data?.profiles || [];
  if (!profiles.length) return null;

  const effective = inst.profile_effective || "";
  const label = inst.profile_label || "default";

  const swap = async (profileId: string) => {
    setBusy(true);
    setOpen(false);
    try {
      const r = await instApi<{ ok: boolean; note?: string }>(inst.title, "/profile", {
        json: { profile_id: profileId },
      });
      toast(
        r?.note ||
          "Now running as " +
            (profileId === "default"
              ? "the CLI's own login"
              : profiles.find((p) => p.id === profileId)?.label || profileId)
      );
      void refreshInstances();
    } catch (err) {
      toast("Swap failed: " + (err as Error).message);
    } finally {
      setBusy(false);
    }
  };

  return (
    <>
      <span
        ref={chipRef}
        className="tok usage-trigger acct-chip"
        title="Account this agent runs as — click to swap"
        onClick={(ev) => {
          ev.stopPropagation();
          if (!busy) setOpen((o) => !o);
        }}
      >
        <span className="usage-head">{busy ? "swapping…" : "@" + label}</span>
        <span className="caret">▾</span>
      </span>
      {open && chipRef.current && (
        <UsagePopover anchor={chipRef.current} onClose={() => setOpen(false)}>
          <div className="usage-pop-head">Run this session as</div>
          <table className="usage-pop-tbl">
            <tbody>
              <tr
                className="acct-pop-row"
                style={{ cursor: "pointer" }}
                onClick={() => swap("default")}
              >
                <td>CLI's own login</td>
                <td className="num">{effective === "" ? "✓" : ""}</td>
              </tr>
              {profiles.map((p) => (
                <tr
                  key={p.id}
                  className="acct-pop-row"
                  style={{ cursor: "pointer" }}
                  onClick={() => swap(p.id)}
                >
                  <td>{p.label || p.id}</td>
                  <td className="num">{effective === p.id ? "✓" : ""}</td>
                </tr>
              ))}
            </tbody>
          </table>
          <div className="usage-pop-note">
            Swapping restarts the agent under the new identity. Files, diff and
            terminal stay; the conversation continues only if the new account
            has seen it before (conversations live per account).
          </div>
        </UsagePopover>
      )}
    </>
  );
}
