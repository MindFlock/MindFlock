/** The sidebar's overall Memory row — the Usage row's twin (OverallUsage):
 * the same full-width button ("Memory 9.8 / 19.5 GB" + a thin meter + ▾) and
 * the same body-mounted popover (UsagePopover): the machine line, the
 * sessions biggest first (a click opens one, as its rail row does), the
 * processes "Outside MindFlock", and a last-hour sparkline. Its own bar
 * key ("memory" in sidebar/barDefs) so Customize hides it like Usage.
 * The model is pure in ./memoryModel. */

import { useMemo, useRef, useState } from "react";
import { useMemory, useMemoryDetail, useMemoryHistory } from "../../state/queries";
import { useUi } from "../../state/store";
import { selectSession } from "../../lib/sessionActions";
import { UsagePopNote, UsagePopTable, UsagePopover } from "./UsagePopover";
import {
  machineFromDetail,
  machineFromReading,
  memoryPopModel,
  memoryReasons,
  memoryRowText,
  memoryRowTone,
  sparkPoints,
  type MemoryPopRow,
} from "./memoryModel";
import { fmtMb } from "../../lib/sessionMemory";
import { gateWhy } from "../../lib/waitWhy";

const SPARK_W = 200;
const SPARK_H = 28;

function BarRows({ rows, onPick }: { rows: MemoryPopRow[]; onPick?: (title: string) => void }) {
  return (
    <ul className="mem-pop-list">
      {rows.map((r) => {
        const inner = (
          <>
            <span className="mem-pop-name">
              <span className="mem-pop-label">{r.label}</span>
              {r.sub && <span className="mem-pop-sub">{r.sub}</span>}
            </span>
            <span className="mem-pop-bar" aria-hidden="true">
              <span style={{ width: Math.max(2, Math.round(r.frac * 100)) + "%" }} />
            </span>
            <span className="mem-pop-num">{r.value}</span>
          </>
        );
        return (
          <li key={r.key}>
            {onPick && r.title ? (
              <button
                type="button"
                className="mem-pop-row mem-pop-pick"
                title={r.tip}
                onClick={(ev) => {
                  ev.stopPropagation();
                  onPick(r.title);
                }}
              >
                {inner}
              </button>
            ) : (
              <div className="mem-pop-row" title={r.tip}>
                {inner}
              </div>
            )}
          </li>
        );
      })}
    </ul>
  );
}

export function OverallMemory() {
  const sidebarHidden = useUi((s) => s.sidebarHidden);
  const aliases = useUi((s) => s.aliases);
  const btnRef = useRef<HTMLButtonElement | null>(null);
  const [open, setOpen] = useState(false);
  // /api/memory polls only while the sidebar shows; /api/system/memory is
  // the memory notice's own query (always on), reused as the fallback.
  const detailQ = useMemoryDetail(!sidebarHidden);
  const { data: reading } = useMemory();
  const detail = detailQ.isError ? null : detailQ.data ?? null;
  const { data: hist } = useMemoryHistory(open && !!detail);

  const machine = useMemo(
    () => (detail ? machineFromDetail(detail.system) : machineFromReading(reading)),
    [detail, reading],
  );
  // The same words as the rail's wait bar (lib/waitWhy) and the memory
  // notice's reasons: an OK level beside a queue never reads "room to spare",
  // and a low one says what the server saw (stalls, swap-in, OOM kills…).
  const ctx = useMemo(
    () => ({ gate: gateWhy(reading?.gate), reasons: memoryReasons(reading) }),
    [reading],
  );
  const { head, title, share } = memoryRowText(machine, ctx);
  const tone = memoryRowTone(machine.level);
  const model = useMemo(
    () => (open ? memoryPopModel(detail, machine, aliases, ctx) : null),
    [open, detail, machine, aliases, ctx],
  );
  const spark = useMemo(
    () => (open && hist ? sparkPoints(hist.samples, SPARK_W, SPARK_H) : null),
    [open, hist],
  );

  // Nothing measured at all (no /proc/meminfo, server down): no row.
  if (machine.totalMb == null) return null;

  const pick = (t: string) => {
    // What a rail row's click does (SidebarRow reselect): focus it, and on a
    // narrow window close the sidebar overlay over it.
    selectSession(t);
    const ui = useUi.getState();
    if (ui.narrow && !ui.sidebarHidden) ui.toggleSidebar();
    setOpen(false);
  };

  return (
    <>
      <button
        id="overall-memory"
        ref={btnRef}
        className={"overall-usage overall-memory" + (tone ? " mem-" + tone : "")}
        type="button"
        title={title}
        onClick={(ev) => {
          ev.stopPropagation();
          setOpen((o) => {
            // Opening is the moment someone reads these numbers: fresh ones.
            if (!o && !sidebarHidden) void detailQ.refetch();
            return !o;
          });
        }}
      >
        <span className="usage-head">{head}</span>
        {share != null && (
          <span className="mem-meter" aria-hidden="true">
            <span style={{ width: Math.round(share * 100) + "%" }} />
          </span>
        )}
        <span className="caret">▾</span>
      </button>
      {open && btnRef.current && model && (
        <UsagePopover anchor={btnRef.current} onClose={() => setOpen(false)}>
          <div className={"usage-pop-head" + (model.tone ? " mem-" + model.tone : "")}>This machine</div>
          <UsagePopTable rows={model.machine} />
          {spark && (
            <div className="mem-spark" title={"Memory in use over the last hour — now " + fmtMb(spark.lastUsedMb) + " of " + fmtMb(spark.totalMb)}>
              <svg
                viewBox={`0 0 ${SPARK_W} ${SPARK_H}`}
                preserveAspectRatio="none"
                role="img"
                aria-label="Memory in use over the last hour"
              >
                <polyline points={spark.points} fill="none" vectorEffect="non-scaling-stroke" />
              </svg>
              <span className="mem-spark-cap">last hour</span>
            </div>
          )}
          {model.sessions.length > 0 && (
            <>
              <div className="usage-pop-head mem-pop-sec">Sessions</div>
              <BarRows rows={model.sessions} onPick={pick} />
            </>
          )}
          {model.others.length > 0 && (
            <>
              <div className="usage-pop-head mem-pop-sec">Outside MindFlock</div>
              <BarRows rows={model.others} />
            </>
          )}
          <UsagePopNote
            text={
              model.machineOnly
                ? "This server reports the machine only — update MindFlock to see what each session holds."
                : "Each session counts its whole process tree (agent, MCP servers, tests), sampled every 15 s."
            }
          />
        </UsagePopover>
      )}
    </>
  );
}
