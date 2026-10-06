/** The Thread's order diagrams (lib/order.ts is the pure half).
 *
 *  - OrderDiagram: an orchestrator's workers in the order MindFlock runs
 *    them — one column per step, left to right; a column's cards run
 *    together. Each card: its state, what it waits on, its fence. A held
 *    card has Start now (the order's `start_now`, the same call the
 *    orchestrator's set_order makes).
 *  - StagesDiagram: a split / one-for-all group's stages (Plan → Pieces →
 *    Merge back → Check → One PR), the one that is running now lit.
 *
 * No connector lines: the columns are the order, and "why" is a line of text
 * on the card that waits. */

import { useState } from "react";
import type { WorkerOrder } from "../../api/types";
import { instApi } from "../../api/client";
import { displayName } from "../../state/store";
import { errMsg } from "../../lib/format";
import { selectSession } from "../../lib/sessionActions";
import { toast } from "../../lib/toast";
import { cardTone, fenceChips, modeLine, overlapLines, stepCaption, type Stage } from "../../lib/order";

export function OrderDiagram({
  title,
  order,
  onChanged,
}: {
  /** The orchestrator whose order this is. */
  title: string;
  order: WorkerOrder;
  onChanged?: () => void;
}) {
  const [busy, setBusy] = useState("");
  const startNow = async (worker: string) => {
    setBusy(worker);
    try {
      await instApi(title, "/order", { method: "POST", json: { start_now: [worker] } });
      toast(displayName(worker) + " starts now — before what it was waiting on");
      onChanged?.();
    } catch (err) {
      toast("Couldn't start it: " + errMsg(err));
    } finally {
      setBusy("");
    }
  };
  return (
    <section className="thread-sec od-sec">
      <div className="thread-sec-head">
        <span className="thread-label">Order</span>
        <span className="od-mode" title="How MindFlock runs these workers — set by the orchestrator (set_order / spawn_session after=)">
          {modeLine(order)}
        </span>
      </div>
      <div className="od-steps" role="list" aria-label="The order the workers run in">
        {order.steps.map((s, i) => (
          <div className="od-step-wrap" key={s.n} role="listitem">
            {i > 0 && (
              <span className="od-then" aria-hidden="true">
                →
              </span>
            )}
            <div className="od-step">
              <div className="od-step-cap">{stepCaption(i, s.workers.length, order)}</div>
              {s.workers.map((c) => {
                const chips = fenceChips(c.fence);
                const overlaps = overlapLines(c, displayName);
                return (
                  <div key={c.title} className={"od-card is-" + c.state}>
                    <div className="od-card-head">
                      <span className={"th-dot " + cardTone(c)} aria-hidden="true" />
                      {c.planned ? (
                        <span className="od-name">{displayName(c.title)}</span>
                      ) : (
                        <button
                          type="button"
                          className="od-name od-link"
                          title={"Go to " + displayName(c.title)}
                          onClick={() => selectSession(c.title)}
                        >
                          {displayName(c.title)}
                        </button>
                      )}
                      <span className="od-word">{c.word}</span>
                    </div>
                    {c.detail && <div className="od-detail">{c.detail}</div>}
                    {overlaps.map((t) => (
                      <div key={t} className="od-detail od-why">
                        {t}
                      </div>
                    ))}
                    {chips.length > 0 && (
                      <div className="od-chips" title={c.fence?.reason ? "Fence: " + c.fence.reason : "Its fence — enforced on every edit"}>
                        {chips.map((ch) => (
                          <span key={ch.text} className={"od-chip k-" + ch.kind}>
                            {ch.text}
                          </span>
                        ))}
                      </div>
                    )}
                    {c.state === "held" && !c.planned && (
                      <div className="od-actions">
                        <button
                          type="button"
                          className="th-btn"
                          disabled={!!busy}
                          title="Hand it its task now, whatever it is waiting on"
                          onClick={() => void startNow(c.title)}
                        >
                          Start now
                        </button>
                      </div>
                    )}
                  </div>
                );
              })}
            </div>
          </div>
        ))}
      </div>
    </section>
  );
}

export function StagesDiagram({ stages }: { stages: Stage[] }) {
  if (!stages.length) return null;
  return (
    <ol className="od-stages" aria-label="The order this group runs in">
      {stages.map((s, i) => (
        <li key={s.key} className={"od-stage is-" + s.state}>
          {i > 0 && (
            <span className="od-then" aria-hidden="true">
              →
            </span>
          )}
          <div className="od-stage-box" title={s.how}>
            <div className="od-stage-label">{s.label}</div>
            <div className="od-stage-how">{s.how}</div>
            {s.detail && <div className="od-stage-detail">{s.detail}</div>}
          </div>
        </li>
      ))}
    </ol>
  );
}
