/** The session lists — the toolbar's "Plan & lists" panel:
 * Breaches, Scope requests (green denies, each with [Allow this file]),
 * Activity (with "blocked", "peeked outside scope" and "outside scope" rows),
 * Plan (Go / "Go — only the planned files"), Blast radius as a LIST, Rules
 * (with kind), Changed, Other agents. Plan mode leads with the plan; watch
 * mode with what is happening now; green mode with the rules. Each section
 * carries `data-sec` (the store's MapSection) so "Open map" can land on it. */

import type { ReactNode } from "react";
import type { CodeMapLive, CodeMapPlan, FeedRecord, RedZone } from "../../../api/types";
import {
  anchoredZonePath,
  isGreen,
  isGreenDeny,
  peeksOf,
  planScope,
  type BlastRow,
  type FeedState,
  type GuardPill,
  type PlanItemStatus,
  type ScopeRequest,
  type ZoneClass,
} from "../../../lib/codemap";
import { relTime } from "../../../lib/format";
import { ExemptPrompt } from "./AddZoneRow";
import { zoneLocked, zoneWhere } from "../../../lib/codetree/zones";
import { isShared, sharedWhere } from "./sharedZone";

export interface PanelModel {
  live: CodeMapLive | null;
  mode: "plan" | "watch";
  green: boolean;
  greenZones: RedZone[];
  zones: RedZone[];
  zoneCounts: Map<string, number>;
  breaches: CodeMapLive["breaches"];
  requests: ScopeRequest[];
  recent: FeedRecord[];
  fs: FeedState;
  guard: GuardPill;
  provider: string;
  skew: number;
  plan: CodeMapPlan | null;
  planSupported: boolean;
  progress: Map<string, PlanItemStatus>;
  offPlan: string[];
  goZones: RedZone[];
  midFlight: boolean;
  clarify: boolean;
  busy: string;
  actionMsg: { text: string; bad: boolean } | null;
  /** "Go — only the planned files" exempted these already-changed files. */
  goExempt: { paths: string[]; state: string } | null;
  blast: BlastRow[];
  blastTests: number;
  seeds: number;
  graphPartial: boolean;
  changed: CodeMapLive["changed"];
  exempt: Set<string>;
  others: CodeMapLive["others"];
  /** Fences on this session's workers (it is their orchestrator). */
  fences?: CodeMapLive["fences"];
  zoneBusy: string;
  reqBusy: Record<string, string>;
  levelName: (p: string) => string;
  classify: (p: string) => ZoneClass;
}

export interface PanelActions {
  selectPath: (p: string) => void;
  openDiff: () => void;
  askPlan: () => void;
  go: (scopeToPlan: boolean) => void;
  removeZone: (z: RedZone) => void;
  waiveZone: (z: RedZone, waived: boolean) => void;
  previewZone: (z: RedZone) => void;
  openAdd: (kind: "red" | "green", pattern: string) => void;
  allow: (path: string) => void;
  keepGoExempt: () => void;
  goExemptAsBreaches: () => void;
}

function recordTarget(r: FeedRecord): string {
  // A refused push names the command, not the breached file it was refused for.
  if (r.deny?.push) return r.cmd || "push";
  if (r.deny?.path) return r.deny.path;
  if (r.breach && r.breach.length) return r.breach.map((b) => b.path).join(", ");
  if (r.writes && r.writes.length) return r.writes[0] + (r.writes.length > 1 ? ` +${r.writes.length - 1}` : "");
  if (r.kind === "bash" && r.cmd) return r.cmd;
  if (r.reads && r.reads.length) return r.reads[0] + (r.reads.length > 1 ? ` +${r.reads.length - 1}` : "");
  if (r.kind === "plan") return "plan";
  return "";
}

export function recKey(r: FeedRecord): string {
  return (r.id || "") + "|" + r.ev + "|" + r.ts;
}

function AllowBtn({ path, busy, onAllow }: { path: string; busy: string; onAllow: (p: string) => void }) {
  return (
    <button
      type="button"
      className="cm-row-act cm-allow"
      disabled={busy === "busy" || busy === "done"}
      title={`Add ${anchoredZonePath(path)} to ✓ Only here (this worktree) and tell the agent`}
      onClick={() => onAllow(path)}
    >
      {busy === "busy" ? "Allowing…" : busy === "done" ? "Allowed ✓" : "Allow this file"}
    </button>
  );
}

export function SessionPanel({ m, a }: { m: PanelModel; a: PanelActions }) {
  const { plan } = m;
  const breachSec =
    m.breaches.length > 0 ? (
      <section className="cm-sec cm-sec-breach" key="breach" data-sec="breaches">
        <h4>
          Breaches <span className="cm-count">{m.breaches.length}</span>
        </h4>
        <p className="cm-hint">
          {m.breaches.every((b) => b.kind === "green")
            ? "Changed outside ✓ Only here."
            : m.breaches.some((b) => b.kind === "green")
              ? "Changed inside ⛔ Keep out or outside ✓ Only here."
              : "Changed inside ⛔ Keep out."}{" "}
          Pushing, PRs and merges are blocked until these are reverted{m.green ? " or allowed" : ""}.
        </p>
        <ul className="cm-list">
          {m.breaches.slice(0, 30).map((b) => (
            <li key={b.path}>
              <button type="button" className="cm-row bad" onClick={() => a.selectPath(b.path)}>
                <span className="cm-row-main">
                  <span className="cm-target">{b.path}</span>
                </span>
                <span className="cm-row-sub">
                  {b.kind === "green" ? "outside scope" : "⛔ keep out" + (b.pattern ? " · " + b.pattern : "")}
                  {b.committed ? " · committed" : ""}
                </span>
              </button>
              {b.kind === "green" && <AllowBtn path={b.path} busy={m.reqBusy[b.path] || ""} onAllow={a.allow} />}
              <button type="button" className="cm-row-act" onClick={a.openDiff}>
                Open diff
              </button>
            </li>
          ))}
        </ul>
      </section>
    ) : null;

  const reqSec =
    m.requests.length > 0 ? (
      <section className="cm-sec cm-sec-req" key="req" data-sec="scope">
        <h4>
          Scope requests <span className="cm-count">{m.requests.length}</span>
        </h4>
        <p className="cm-hint">The agent tried to change these outside its scope and was stopped.</p>
        <ul className="cm-list">
          {m.requests.slice(0, 20).map((r) => (
            <li key={r.path}>
              <button type="button" className="cm-row" onClick={() => a.selectPath(r.path)} title={r.reason || r.path}>
                <span className="cm-row-main">
                  <span className="cm-target">{r.path}</span>
                </span>
                <span className="cm-row-time">{relTime(r.ts - m.skew)}</span>
              </button>
              <AllowBtn path={r.path} busy={m.reqBusy[r.path] || ""} onAllow={a.allow} />
            </li>
          ))}
        </ul>
      </section>
    ) : null;

  const activitySec = (
    <section className="cm-sec" key="act">
      <h4>Activity</h4>
      {m.recent.length === 0 ? (
        <p className="cm-empty-line">
          {m.guard.cls.includes("g-detect")
            ? `Detect-only for ${m.provider || "this agent"} — MindFlock sees its edits on disk, not its tool calls.`
            : m.changed.length
              ? `No tool calls since MindFlock armed the map — the ${m.changed.length} changed file${m.changed.length === 1 ? " was" : "s were"} changed before it started watching.`
              : "Agent hasn't used any tools since MindFlock armed the map."}
        </p>
      ) : (
        <ul className="cm-list">
          {m.recent.map((r) => {
            const target = recordTarget(r);
            const path = r.deny?.path || r.writes?.[0] || r.reads?.[0] || "";
            const greenDeny = isGreenDeny(r);
            const peeks = m.green ? peeksOf(r, (p) => m.classify(p) === "outside") : [];
            const bad = !!(r.deny || (r.breach && r.breach.length));
            const stillOut = greenDeny && m.classify(r.deny!.path) === "outside";
            return (
              <li key={recKey(r)}>
                <button
                  type="button"
                  className={"cm-row" + (bad ? " bad" : "") + (r.ev === "fail" ? " failed" : "") + (peeks.length ? " peek" : "")}
                  disabled={!path}
                  onClick={() => path && a.selectPath(path)}
                  title={r.deny?.reason || r.err || r.cmd || target}
                >
                  <span className="cm-row-main">
                    <span className="cm-tool">{r.tool}</span>
                    {r.deny ? (
                      <span className={"cm-badge " + (greenDeny ? "soft-bad" : "bad")}>
                        {r.deny.push ? "blocked push" : greenDeny ? "outside scope" : "blocked"}
                      </span>
                    ) : null}
                    {r.breach && r.breach.length ? <span className="cm-badge bad">breach</span> : null}
                    {peeks.length ? (
                      <span className="cm-badge soft-warn" title={"Read outside ✓ Only here (allowed): " + peeks.join(", ")}>
                        peeked outside scope
                      </span>
                    ) : null}
                    {r.ev === "fail" && !r.deny ? <span className="cm-badge">failed</span> : null}
                    {r.agent ? (
                      <span className="cm-badge agent" title={"subagent " + r.agent}>
                        sub
                      </span>
                    ) : null}
                    <span className="cm-target">{target}</span>
                  </span>
                  <span className="cm-row-time">{relTime((r.ts || 0) - m.skew)}</span>
                </button>
                {stillOut && <AllowBtn path={r.deny!.path} busy={m.reqBusy[r.deny!.path] || ""} onAllow={a.allow} />}
              </li>
            );
          })}
        </ul>
      )}
      {m.fs.running.length > 0 && (
        <p className="cm-running">
          <span className="cm-running-dot" aria-hidden="true" /> running: {m.fs.running[m.fs.running.length - 1].cmd || "a command"}
        </p>
      )}
    </section>
  );

  const scope = plan ? planScope(plan.items) : [];
  const goExemptRow =
    m.goExempt && m.goExempt.paths.length > 0 && m.goExempt.state !== "kept" ? (
      <p className="cm-hint cm-go-exempt">
        The plan is now the scope.
        <ExemptPrompt
          paths={m.goExempt.paths}
          state={m.goExempt.state}
          onKeep={a.keepGoExempt}
          onTreatAsBreaches={a.goExemptAsBreaches}
        />
      </p>
    ) : null;
  const planSec = m.planSupported ? (
    <section className="cm-sec" key="plan" data-sec="plan">
      <h4>
        Plan{" "}
        {plan && (
          <span className="cm-count" title={plan.source === "exitplan" ? "From plan mode" : "Declared by the agent"}>
            {plan.items.length}
          </span>
        )}
      </h4>
      {plan ? (
        <ul className="cm-list">
          {plan.items.slice(0, 60).map((it) => {
            const st = m.progress.get(it.path);
            const cls = m.classify(it.path);
            return (
              <li key={it.path}>
                <button type="button" className={"cm-row plan-" + (st || "untouched")} onClick={() => a.selectPath(it.path)} title={it.intent}>
                  <span className="cm-row-main">
                    <span className="cm-plan-mark" aria-hidden="true">
                      {st === "done" ? "✓" : it.new ? "+" : "○"}
                    </span>
                    <span className="cm-target">{it.path}</span>
                    {cls === "blocked" && <span className="cm-badge bad">⛔ keep out</span>}
                    {(it.outside || cls === "outside") && <span className="cm-badge soft-warn">outside scope</span>}
                  </span>
                  <span className="cm-row-sub">{it.intent}</span>
                </button>
              </li>
            );
          })}
          {m.offPlan.map((p) => (
            <li key={"off:" + p}>
              <button type="button" className="cm-row plan-off" onClick={() => a.selectPath(p)}>
                <span className="cm-row-main">
                  <span className="cm-plan-mark" aria-hidden="true">
                    !
                  </span>
                  <span className="cm-target">{p}</span>
                  <span className="cm-badge warn">off-plan</span>
                </span>
              </button>
            </li>
          ))}
        </ul>
      ) : (
        <p className="cm-empty-line">
          {m.midFlight
            ? "No plan declared. Ask what's left to see where it's headed."
            : "No plan yet. Ask for one to see every file it means to touch — before it touches them."}
        </p>
      )}
      <div className="cm-actions">
        <button type="button" disabled={m.clarify || m.busy === "ask"} onClick={a.askPlan}>
          {m.busy === "ask" ? "Asking…" : m.midFlight ? "Ask what's left" : "Ask for plan"}
        </button>
        {plan && (
          <button
            type="button"
            className="cm-primary"
            disabled={m.clarify || !!m.busy}
            title={
              m.goZones.length
                ? "Tell the agent to go ahead, and which rules are new: " + m.goZones.map((z) => z.pattern).join(", ")
                : "Tell the agent to go ahead with its plan"
            }
            onClick={() => a.go(false)}
          >
            {m.busy === "go"
              ? "Sending…"
              : m.goZones.length
                ? `Go — with ${m.goZones.length} rule${m.goZones.length === 1 ? "" : "s"}`
                : "Go"}
          </button>
        )}
        {plan && (
          <button
            type="button"
            className="cm-primary green"
            disabled={m.clarify || !!m.busy || !scope.length}
            title={
              "Go, and make the plan the scope: only these may change (✓ Only here, this worktree) — " +
              scope.slice(0, 12).join(", ") +
              (scope.length > 12 ? ` +${scope.length - 12} more` : "")
            }
            onClick={() => a.go(true)}
          >
            {m.busy === "go-scope" ? "Sending…" : "Go — only the planned files"}
          </button>
        )}
      </div>
      {m.clarify && <p className="cm-hint">The agent is asking something — answer the prompt in the terminal first.</p>}
      {m.actionMsg && <p className={m.actionMsg.bad ? "cm-err" : "cm-ok"}>{m.actionMsg.text}</p>}
      {goExemptRow}
    </section>
  ) : m.actionMsg || goExemptRow ? (
    <div key="plan" className="cm-sec" data-sec="plan">
      {m.actionMsg && <p className={m.actionMsg.bad ? "cm-err" : "cm-ok"}>{m.actionMsg.text}</p>}
      {goExemptRow}
    </div>
  ) : null;

  const blastSec = (
    <section className="cm-sec" key="blast" data-sec="blast">
      <h4>Blast radius</h4>
      <p className="cm-hint">Code that imports {m.mode === "plan" ? "what the plan touches" : "what changed"}.</p>
      {!m.blast.some((b) => b.count > 0) ? (
        <p className="cm-empty-line">
          {!m.seeds
            ? m.mode === "plan"
              ? "Nothing in the plan exists yet to depend on."
              : "No changes yet."
            : m.graphPartial
              ? "Still indexing imports…"
              : "Nothing imports what changed."}
        </p>
      ) : (
        <ul className="cm-list">
          {m.blast.filter((b) => b.count > 0).slice(0, 20).map((b) => (
            <li key={b.path}>
              <button
                type="button"
                className="cm-row"
                onClick={() => a.selectPath(b.path)}
                title={
                  `${b.count} file${b.count === 1 ? "" : "s"}` +
                  (b.tests ? ` + ${b.tests} test${b.tests === 1 ? "" : "s"}` : "") +
                  " import it" +
                  (b.paths.length ? "\n" + b.paths.slice(0, 12).join("\n") : "")
                }
              >
                <span className="cm-row-main">
                  <span className="cm-blast-dot" aria-hidden="true" />
                  <span className="cm-target">{b.outside ? b.path : m.levelName(b.path)}</span>
                  {b.outside && <span className="cm-badge">elsewhere</span>}
                </span>
                <span className="cm-row-time">
                  {b.count > 0 ? b.count : ""}
                  {b.tests ? <span className="cm-tests">{(b.count > 0 ? " +" : "+") + b.tests + " tests"}</span> : null}
                </span>
              </button>
              <button
                type="button"
                className="cm-row-act quiet"
                title={`⛔ Keep out: ${b.path || "this"}`}
                onClick={() => a.openAdd("red", anchoredZonePath(b.path))}
              >
                ⛔
              </button>
            </li>
          ))}
        </ul>
      )}
      {m.blastTests > 0 && (
        <p className="cm-hint">
          +{m.blastTests} test{m.blastTests === 1 ? "" : "s"} depend on these.
        </p>
      )}
    </section>
  );

  const zonesSec = (
    <section className="cm-sec" key="zones" data-sec="zones">
      <h4>
        Rules <span className="cm-count">{m.zones.length}</span>
      </h4>
      {m.green && (
        <p className="cm-hint">
          Edits outside ✓ Only here are blocked and the agent is told why. Reads are allowed (shown as “peeked”).
        </p>
      )}
      {!m.zones.length ? (
        <p className="cm-empty-line">
          None yet. Click a folder on the tree, then “⛔ Keep out” or “✓ Only here” — or use + Rule.
        </p>
      ) : (
        <ul className="cm-list">
          {m.zones.map((z) => {
            const n = m.zoneCounts.get(z.id) ?? 0;
            const g = isGreen(z);
            return (
              <li key={z.id} className={z.waived ? "is-waived" : ""}>
                <button type="button" className="cm-row" onClick={() => a.previewZone(z)} title={z.note || z.pattern}>
                  <span className="cm-row-main">
                    <span className={"cm-zone-mark " + (g ? "green" : "red")} aria-hidden="true">
                      {g ? "✓" : "⛔"}
                    </span>
                    <span className="cm-target">{z.name || z.pattern}</span>
                    <span className={"cm-row-kind " + (g ? "green" : "red")}>{g ? "only here" : "keep out"}</span>
                  </span>
                  <span className={"cm-row-sub" + (n === 0 ? " cm-warn-text" : "")}>
                    {z.name ? z.pattern + " · " : ""}
                    {isShared(z) ? sharedWhere(z) : zoneWhere(z)}
                    {z.waived ? " · allowed here" : ""} ·{" "}
                    {n === 0
                      ? g
                        ? "matches no files yet — the agent may only create new files under it"
                        : "matches no files yet"
                      : `${n} file${n === 1 ? "" : "s"}${g ? " writable" : ""}`}
                  </span>
                </button>
                {z.scope !== "worktree" && !g && !zoneLocked(z) && (
                  <button
                    type="button"
                    className="cm-row-act quiet"
                    disabled={m.zoneBusy === z.id}
                    title={z.waived ? "Protect it here again" : "Allow edits in this worktree only (the rule stays for the repo)"}
                    onClick={() => a.waiveZone(z, !z.waived)}
                  >
                    {z.waived ? "Re-protect" : "Allow here"}
                  </button>
                )}
                {!zoneLocked(z) && (
                  <button
                    type="button"
                    className="cm-row-act cm-x quiet"
                    aria-label={"Remove rule " + (z.name || z.pattern)}
                    title="Remove this rule"
                    disabled={m.zoneBusy === z.id}
                    onClick={() => a.removeZone(z)}
                  >
                    ×
                  </button>
                )}
              </li>
            );
          })}
        </ul>
      )}
    </section>
  );

  const changedSec =
    m.changed.length > 0 ? (
      <section className="cm-sec" key="changed" data-sec="changed">
        <h4>
          Changed <span className="cm-count">{m.changed.length}</span>
        </h4>
        <ul className="cm-list">
          {m.changed.slice(0, 40).map((c) => {
            const cls = m.green ? m.classify(c.path) : "ok";
            return (
              <li key={c.path}>
                <button type="button" className={"cm-row" + (m.offPlan.includes(c.path) ? " off" : "")} onClick={() => a.selectPath(c.path)}>
                  <span className="cm-row-main">
                    <span className="cm-status">{(c.status || "M")[0]}</span>
                    <span className="cm-target">{c.path}</span>
                    {m.offPlan.includes(c.path) && <span className="cm-badge warn">off-plan</span>}
                    {cls === "companion" && (
                      <span className="cm-badge soft-warn" title="Outside ✓ Only here but allowed: a companion (lockfile, snapshot, test, derived output)">
                        companion
                      </span>
                    )}
                    {cls === "outside" && m.exempt.has(c.path) && (
                      <span className="cm-badge" title="Changed before ✓ Only here was set — exempt while unchanged">
                        exempt
                      </span>
                    )}
                  </span>
                  <span className="cm-row-time">
                    <span className="add">+{c.added || 0}</span> <span className="del">−{c.removed || 0}</span>
                  </span>
                </button>
              </li>
            );
          })}
        </ul>
        <div className="cm-actions">
          <button type="button" onClick={a.openDiff}>
            Open diff
          </button>
        </div>
      </section>
    ) : null;

  const othersSec =
    m.others.length > 0 ? (
      <section className="cm-sec" key="others" data-sec="others">
        <h4>
          Other agents here <span className="cm-count">{m.others.length}</span>
        </h4>
        <ul className="cm-list">
          {m.others.slice(0, 12).map((o) => (
            <li key={o.session + o.path + o.ts}>
              <button type="button" className="cm-row" onClick={() => a.selectPath(o.path)}>
                <span className="cm-row-main">
                  <span className="cm-other-dot" aria-hidden="true" />
                  <span className="cm-target">{o.path}</span>
                </span>
                <span className="cm-row-sub">
                  {o.session} · {relTime(o.ts - m.skew)}
                </span>
              </button>
            </li>
          ))}
        </ul>
      </section>
    ) : null;

  const fences = m.fences || [];
  const fencesSec =
    fences.length > 0 ? (
      <section className="cm-sec" key="fences">
        <h4>
          Workers' fences <span className="cm-count">{fences.length}</span>
        </h4>
        <p className="cm-hint">
          What each worker may change — set by its orchestrator, enforced on every edit for that worker only.
        </p>
        <ul className="cm-list">
          {fences.map((f) => (
            <li key={f.session}>
              <div className="cm-row" title={f.reason || undefined}>
                <span className="cm-row-main">
                  <span className="cm-other-dot" aria-hidden="true" />
                  <span className="cm-target">{f.session}</span>
                </span>
                <span className="cm-row-sub">
                  {f.only.map((p) => (
                    <button type="button" key={"o" + p} className="cm-fence only" title="Show it on the tree" onClick={() => a.selectPath(p.replace(/^\//, "").replace(/\/?\*\*.*$/, ""))}>
                      ✓ {p}
                    </button>
                  ))}
                  {f.keep_out.map((p) => (
                    <button type="button" key={"k" + p} className="cm-fence keep" title="Show it on the tree" onClick={() => a.selectPath(p.replace(/^\//, "").replace(/\/?\*\*.*$/, ""))}>
                      ⛔ {p}
                    </button>
                  ))}
                  {f.reason ? <span className="cm-fence-why">{f.reason}</span> : null}
                </span>
              </div>
            </li>
          ))}
        </ul>
      </section>
    ) : null;

  let order: ReactNode[];
  if (m.green) order = [zonesSec, activitySec, planSec, blastSec];
  else if (m.mode === "plan") order = [planSec, blastSec, zonesSec, activitySec];
  else order = [activitySec, planSec, blastSec, zonesSec];
  return (
    <>
      {breachSec}
      {reqSec}
      {order}
      {changedSec}
      {fencesSec}
      {othersSec}
      <p className="cm-hint cm-sel-hint">Click a leaf or a folder on the tree for its card.</p>
    </>
  );
}
