/** Ports the per-session cost/token chip from 140-panes.js (the
 * `.tok.usage-trigger` header chip + its click handler) and toggleUsagePop
 * from 050-usage-cost.js: headline "Codex · ~$0.62 · 128k/200k", click for
 * the session's full token breakdown (usageRows) in the shared popover. */

import { useRef, useState } from "react";
import type { Instance } from "../../api/types";
import { provLabel } from "../../lib/format";
import { useUsage } from "../../state/queries";
import { UsagePopNote, UsagePopTable, UsagePopover } from "./UsagePopover";
import { USAGE_NOTE, asUsageWindows, isPlanMode, usageHeadline, usageRows } from "./usageModel";

export interface SessionUsageChipProps {
  inst: Instance;
}

export function SessionUsageChip({ inst }: SessionUsageChipProps) {
  const chipRef = useRef<HTMLSpanElement | null>(null);
  const [open, setOpen] = useState(false);
  // Plan-vs-metered picks the cost row's label ("≈ API-equiv. cost" on a
  // subscription); shared 60s-polled query, same cadence as the vanilla cache.
  const { data: usageData } = useUsage();
  const usage = asUsageWindows(usageData);

  const pl = provLabel(inst.provider);
  return (
    <>
      <span
        ref={chipRef}
        className="tok usage-trigger"
        title="Session cost & tokens — click for the breakdown"
        onClick={(ev) => {
          ev.stopPropagation();
          setOpen((o) => !o);
        }}
      >
        <span className="usage-head">{usageHeadline(inst)}</span>
        <span className="caret">▾</span>
      </span>
      {open && chipRef.current && (
        <UsagePopover anchor={chipRef.current} onClose={() => setOpen(false)}>
          <div className="usage-pop-head">
            {(pl ? pl + " · " : "") + (inst.title || "Session usage")}
          </div>
          <UsagePopTable rows={usageRows(inst, isPlanMode(usage))} />
          <UsagePopNote text={USAGE_NOTE} />
        </UsagePopover>
      )}
    </>
  );
}
