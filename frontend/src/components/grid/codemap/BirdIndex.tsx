/** The bird index: every agent and helper as a compact row in a LEFT column
 * of the Map — at every pane size, never a strip along the top. Wide panes
 * get the column (glyph, colour, status, what it is doing now, how many files
 * it changed; a row expands for the details and its changed files ranked by
 * how much depends on them); small panes get a slim left rail (glyph + short
 * name, "✎ N changed" / "⛔ N rules" tiles) that expands into the same column
 * over the canvas. A click on a row follows the bird.
 *
 * Under the birds: CHANGES — where the changes are ("backend 5
 * · frontend 4 · this session 7 · other sessions 2"), the one most code depends
 * on, "⚠ Could break" (a ranked list, on demand), and one row per changed
 * folder with ⛔ / ✓ buttons: blocking or allowing an area is one click from
 * where you read about it — then the Rules (below, so a rule added from a
 * Changes row never moves that row under the pointer). The legend is behind "?". */

import { useState, type ReactNode } from "react";
import type { RedZone } from "../../../api/types";
import { isGreen } from "../../../lib/codemap";
import { blockedTxt, folderPath, tailPath } from "../../../lib/codetree/draw";
import { STATUS_TXT } from "../../../lib/codetree/live";
import { isUnder, type Model, type TFile, type TNode } from "../../../lib/codetree/model";
import { zoneOfFile } from "../../../lib/codetree/zones";
import type { AgentState, TZone } from "../../../lib/codetree/types";
import { BirdIcon, LegendCard, dispName, nodeName, subLine } from "./TreeCards";

const clip = (s: string, n = 22) => (s.length > n ? s.slice(0, n - 1) + "…" : s);
const plural = (n: number, one: string, many = one + "s") => `${n} ${n === 1 ? one : many}`;

export interface BirdIndexProps {
  M: Model;
  agents: AgentState[];
  tzones: TZone[];
  zones: RedZone[];
  zoneBusy: string;
  size: "wide" | "narrow" | "tiny";
  followKey: string | null;
  primaryTask?: string;
  primaryExtra?: ReactNode;
  affects: (A: AgentState) => number;
  onFollow: (key: string) => void;
  onNest: (A: AgentState) => void;
  onFile: (f: TFile) => void;
  onFolder: (n: TNode) => void;
  onPaint: (tool: "keep" | "only", n: TNode) => void;
  onGoZone: (z: RedZone) => void;
  onRemoveZone: (z: RedZone) => void;
  /** "⚠ Could break" is on (the map shows the folders that import a change) */
  riskOpen?: boolean;
  onRisk?: (on: boolean) => void;
  /** the changed file whose dependents the map shows now (null = none) */
  riskFile?: number | null;
  onRiskFile?: (f: TFile) => void;
}

/** Which rows are expanded, per session (the primary bird's key) — survives remounts. */
const EXPANDED = new Map<string, Set<string>>();

/** One changed folder: its files (most depended-on first) and who changed them. */
export interface ChangedFolder {
  n: TNode;
  label: string;
  files: TFile[];
  cols: string[];
  /** of its changed files: this session's (and its helpers'), other sessions' */
  mine: number;
  others: number;
}

export interface ChangeSummary {
  total: number;
  mine: number;
  others: number;
  /** by top-level folder, most first */
  tops: Array<[string, number]>;
  folders: ChangedFolder[];
  /** every changed file, most depended-on first */
  ranked: TFile[];
}

/** Where the changes are. A file two birds touched counts once — for this session when it is one of them. */
export function changeSummary(M: Model, agents: AgentState[]): ChangeSummary {
  const primary = agents.find((A) => A.ag.primary) || null;
  const mineA = (A: AgentState) => A.ag.primary || (!!primary && A.ag.parent === primary.ag.key);
  const order = [...agents].sort((a, b) => Number(mineA(b)) - Number(mineA(a)));
  const owner = new Map<number, AgentState>();
  for (const A of order) for (const id of A.edits.keys()) if (!owner.has(id) && M.files[id]) owner.set(id, A);
  const tops = new Map<string, number>();
  const byNode = new Map<TNode, ChangedFolder>();
  let mine = 0;
  for (const [id, A] of owner) {
    const f = M.files[id];
    if (mineA(A)) mine++;
    const n = f.node;
    let top: TNode | null = n;
    while (top && top.depth > 1) top = top.parent;
    const tk = !top || top.depth < 1 ? (n && n.kind === "pile" ? nodeName(M, n) : "root files") : nodeName(M, top);
    tops.set(tk, (tops.get(tk) || 0) + 1);
    if (!n) continue;
    let e = byNode.get(n);
    if (!e) byNode.set(n, (e = { n, label: n.depth >= 1 ? folderPath(n) : nodeName(M, n), files: [], cols: [], mine: 0, others: 0 }));
    e.files.push(f);
    if (mineA(A)) e.mine++;
    else e.others++;
    if (!e.cols.includes(A.ag.color)) e.cols.push(A.ag.color);
  }
  const byDeps = (a: TFile, b: TFile) => b.usedBy.length - a.usedBy.length || a.path.localeCompare(b.path);
  for (const e of byNode.values()) e.files.sort(byDeps);
  return {
    total: owner.size,
    mine,
    others: owner.size - mine,
    tops: [...tops.entries()].sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0])),
    folders: [...byNode.values()].sort((a, b) => b.files.length - a.files.length || a.label.localeCompare(b.label)),
    ranked: [...owner.keys()].map((id) => M.files[id]).sort(byDeps),
  };
}

/** How many edits a rule has refused so far (the map's sign says the same number the same way). */
function nBlocked(agents: AgentState[], z: RedZone): number {
  let n = 0;
  for (const A of agents) for (const bk of A.blocked) if (bk.z && bk.z.z.id === z.id) n++;
  return n;
}

/** Where a helper is: the folder of the file it is on (or was blocked at), else its nest — "backend/config". */
function helperWhere(M: Model, S: AgentState): string {
  const bk = S.blocked[S.blocked.length - 1];
  const f = S.status === "blocked" && bk ? bk.f : S.file;
  const n = f ? f.node : S.nest;
  if (!n) return "";
  if (n.depth < 1) return n.kind === "pile" ? nodeName(M, n) : "repo root";
  return folderPath(n).replace(" › ", "/");
}

export function BirdIndex(p: BirdIndexProps) {
  const [legend, setLegend] = useState(false);
  const [railOpen, setRailOpen] = useState(false);
  const [, bump] = useState(0);
  const tops = p.agents.filter((A) => !A.ag.parent);
  const subsOf = (A: AgentState) => p.agents.filter((S) => S.ag.parent === A.ag.key);
  const primary = p.agents.find((A) => A.ag.primary) || null;
  const exKey = primary ? primary.ag.key : "";
  const ex = EXPANDED.get(exKey) || new Set<string>();
  const toggle = (k: string) => {
    const s = new Set(ex);
    if (s.has(k)) s.delete(k);
    else s.add(k);
    EXPANDED.set(exKey, s);
    bump((x) => x + 1);
  };
  const riskOpen = !!p.riskOpen;
  const setRisk = (b: boolean) => p.onRisk && p.onRisk(b);
  const sum = changeSummary(p.M, p.agents);

  const legendCard = legend && (
    <div className="ct-bix-legend" data-hud="pop">
      <LegendCard open onToggle={() => setLegend(false)} primary={primary ? primary.ag.color : "var(--accent)"} />
    </div>
  );

  const column = (
    <>
      <div className="ct-bix-head">
        <h4>Agents</h4>
        <button type="button" className="ct-bix-q" aria-expanded={legend} aria-label="Legend: how to read the map" title="How to read the map" onClick={() => setLegend(!legend)}>
          ?
        </button>
        {p.size !== "wide" && (
          <button type="button" className="ct-bix-q" aria-label="Collapse the agents column" title="Collapse to the rail" onClick={() => setRailOpen(false)}>
            «
          </button>
        )}
      </div>
      <ul className="ct-bix-list" role="list">
        {tops.map((A) => (
          <AgentRow key={A.ag.key} p={p} A={A} subs={subsOf(A)} open={ex.has(A.ag.key)} onToggle={() => toggle(A.ag.key)} />
        ))}
      </ul>
      {/* Changes before Rules: a rule added from a Changes row lands below it, so nothing moves under the pointer */}
      <Changes p={p} sum={sum} riskOpen={riskOpen} onRisk={() => setRisk(!riskOpen)} />
      <Rules p={p} />
    </>
  );

  if (p.size === "wide")
    return (
      <>
        <div className="ct-left ct-bix" data-hud="left" role="region" aria-label="Agents on this map">
          {column}
        </div>
        {legendCard}
      </>
    );

  // small panes: a slim rail on the left (never a strip along the top); » opens the full column over the canvas
  const rules = p.zones.filter((z) => !z.waived);
  const nKeep = rules.filter((z) => !isGreen(z)).length,
    nOnly = rules.length - nKeep;
  return (
    <>
      <div className={"ct-left ct-bix-rail" + (p.size === "tiny" ? " tiny" : "")} data-hud="left" role="region" aria-label="Agents on this map">
        <div className="ct-rail-head">
          <button type="button" className="ct-bix-q" aria-expanded={railOpen} aria-label="Expand the agents column" title="Expand: details, changes, rules" onClick={() => setRailOpen(!railOpen)}>
            »
          </button>
          <button type="button" className="ct-bix-q" aria-expanded={legend} aria-label="Legend: how to read the map" title="How to read the map" onClick={() => setLegend(!legend)}>
            ?
          </button>
        </div>
        <ul className="ct-bix-list" role="list">
          {tops.map((A) => (
            <li key={A.ag.key}>
              <RailRow A={A} following={p.followKey === A.ag.key} n={p.size === "tiny" ? 7 : 10} onFollow={() => p.onFollow(A.ag.key)} />
              {subsOf(A).map((S) => (
                <RailRow key={S.ag.key} A={S} sub following={p.followKey === S.ag.key} n={p.size === "tiny" ? 6 : 9} onFollow={() => p.onFollow(S.ag.key)} />
              ))}
            </li>
          ))}
        </ul>
        <div className="ct-rail-tiles">
          <button
            type="button"
            className="ct-rail-tile"
            title={sum.total ? `${plural(sum.total, "file")} changed, all sessions — ${sum.tops.map(([k, n]) => `${k} ${n}`).join(", ")}\nthis session ${sum.mine} · other sessions ${sum.others}` : "No changes yet."}
            onClick={() => setRailOpen(true)}
          >
            <b>✎{sum.total}</b> changed
          </button>
          <button
            type="button"
            className={"ct-rail-tile" + (nKeep ? " keep" : nOnly ? " only" : "")}
            title={rules.length ? rules.map((z) => (isGreen(z) ? "✓ only here " : "⛔ keep out ") + (z.name || z.pattern) + (nBlocked(p.agents, z) ? " · " + blockedTxt(nBlocked(p.agents, z)) : "")).join("\n") : "No rules — ⛔ Keep out / ✓ Only here"}
            onClick={() => setRailOpen(true)}
          >
            <b>
              {nOnly ? "✓" : "⛔"}
              {rules.length}
            </b>{" "}
            {rules.length === 1 ? "rule" : "rules"}
          </button>
        </div>
      </div>
      {railOpen && (
        <div className="ct-bix ct-bix-pop" data-hud="pop" role="region" aria-label="Agents (expanded)">
          {column}
        </div>
      )}
      {legendCard}
    </>
  );
}

function RailRow({ A, sub, following, n, onFollow }: { A: AgentState; sub?: boolean; following: boolean; n: number; onFollow: () => void }) {
  const st = A.status;
  return (
    <button
      type="button"
      className={"ct-rail-row" + (sub ? " sub" : "") + (A.done ? " done" : "") + (following ? " following" : "")}
      style={{ borderLeftColor: A.ag.color }}
      aria-pressed={following}
      onClick={onFollow}
      data-agent={A.ag.key}
      title={`${following ? "Following" : "Follow"} ${A.ag.name} — ${STATUS_TXT[st]}${A.file ? " " + A.file.name : ""} · ${A.edits.size} changed`}
    >
      <span className="g" style={{ color: A.ag.color }}>
        {A.ag.glyph}
      </span>
      <span className="nm">{clip(A.ag.short || A.ag.name, n)}</span>
      {A.edits.size > 0 && (
        <span className="n" style={{ color: A.ag.color }}>
          {A.edits.size}
        </span>
      )}
      <span className={"dot " + st} aria-hidden="true" />
    </button>
  );
}

function AgentRow({ p, A, subs, open, onToggle }: { p: BirdIndexProps; A: AgentState; subs: AgentState[]; open: boolean; onToggle: () => void }) {
  const M = p.M;
  const st = A.status;
  const f = A.file;
  const following = p.followKey === A.ag.key;
  const bk = A.blocked[A.blocked.length - 1];
  const working = subs.filter((S) => !S.done).length;
  const now =
    st === "blocked" && bk
      ? `⛔ tried ${bk.f ? dispName(M, bk.f) : bk.path}`
      : st === "done"
        ? "finished — back in the nest"
        : st === "planning"
          ? "planning (buds on the files it will touch)"
          : st === "thinking"
            ? working
              ? `in the nest — ${plural(working, "helper")} out working`
              : "in the nest, thinking"
            : A.ag.primary
              ? A.activity === "clarify"
                ? "waiting for your answer"
                : "waiting"
              : "editing on its own branch";
  // another session works in its own worktree: only repo-wide keep-outs bind it
  const binding = A.ag.primary ? p.tzones : p.tzones.filter((z) => z.type === "keep" && z.z.scope !== "worktree");
  const zf = f && st !== "blocked" && st !== "done" ? zoneOfFile(f, binding) : null;
  // the changed files, the ones most code depends on first: "what could break" answered on demand
  const changed = open
    ? [...A.edits.keys()]
        .map((id) => M.files[id])
        .filter(Boolean)
        .sort((a, b) => b.usedBy.length - a.usedBy.length || a.path.localeCompare(b.path))
    : [];
  // the session's count includes its helpers' changes (the pane header's "7 files" and CHANGES' "this session 7"
  // count the same worktree), and says how many of them the helpers made
  const allEdits = new Set<number>(A.edits.keys());
  for (const S of subs) for (const id of S.edits.keys()) allEdits.add(id);
  const byHelpers = allEdits.size - A.edits.size;
  const area = (() => {
    // where its changes are: the folders holding them, most first
    const by = new Map<string, number>();
    for (const id of allEdits) {
      const ff = M.files[id];
      if (!ff || !ff.node) continue;
      const k = ff.node.depth >= 1 ? ff.node.disp || ff.node.name : "root";
      by.set(k, (by.get(k) || 0) + 1);
    }
    return [...by.entries()].sort((a, b) => b[1] - a[1]);
  })();
  const task = A.ag.primary ? p.primaryTask : "another session on this repo";
  return (
    <li className={"ct-bix-row" + (following ? " following" : "") + (A.done ? " done" : "")} style={{ borderLeftColor: A.ag.color }}>
      <div className="ct-bix-main">
        <button
          type="button"
          className="ct-bix-follow"
          aria-pressed={following}
          onClick={() => p.onFollow(A.ag.key)}
          data-agent={A.ag.key}
          title={following ? "Following — click again, pan, zoom or press Esc to stop" : "Click to follow this bird"}
        >
          <BirdIcon color={A.ag.color} />
          <span className="nm" style={{ color: A.ag.color }}>
            {A.ag.glyph} {clip(A.ag.name, 18)}
          </span>
          <span className={"ct-st " + st} style={st === "editing" || st === "creating" ? { color: A.ag.color } : undefined}>
            {following ? "following" : STATUS_TXT[st]}
          </span>
        </button>
        <button
          type="button"
          className="ct-bix-more"
          aria-expanded={open}
          aria-label={(open ? "Hide" : "Show") + " details for " + A.ag.name}
          title={open ? "Less" : "Details · changed files ranked by what depends on them"}
          onClick={onToggle}
        >
          {open ? "▾" : "▸"}
        </button>
      </div>
      {/* a rule over where it works rides on this same line (never a new one): a ⛔ / ✓ click below never moves
          the row under the pointer */}
      <div className={"ct-bix-now" + (zf ? " warn" : "")} title={zf ? `${st === "reading" ? "Reading" : "Working"} ${zf.type === "keep" ? "inside ⛔ keep out" : "outside ✓ only here"} (${zf.z.label}) — ${st === "reading" ? "reads are fine, an edit here is blocked" : "the next edit here is blocked"}` : undefined}>
        {f && st !== "blocked" ? (
          <>
            {zf ? "⚠ " : ""}
            {st === "reading" ? "reading" : st === "creating" ? "creating" : "editing"}{" "}
            <button type="button" className="ct-link" onClick={() => p.onFile(f)}>
              {dispName(M, f)}
            </button>
            {zf && <span className="zw">{zf.type === "keep" ? " · in ⛔ keep out" : " · outside ✓ only here"}</span>}
          </>
        ) : (
          <span className={st === "blocked" ? "bad" : ""}>{now}</span>
        )}
      </div>
      {allEdits.size > 0 && (
        <div className="ct-bix-chg" title={area.map(([k, n]) => `${k}: ${n}`).join("\n") + (byHelpers ? `\n${A.edits.size} by ${A.ag.name}, ${byHelpers} by its helpers` : "")}>
          <b style={{ color: A.ag.color }}>{allEdits.size} changed</b>
          {byHelpers > 0 && <span className="hlp"> ({byHelpers} by helpers)</span>}
          <span>
            {" "}
            · {area
              .slice(0, 2)
              .map(([k, n]) => `${clip(k, 16)} ${n}`)
              .join(", ")}
            {area.length > 2 ? "…" : ""}
          </span>
        </div>
      )}
      {subs.length > 0 && (
        <ul className="ct-bix-subs" role="list" aria-label={"Helpers of " + A.ag.name}>
          {subs.map((S) => (
            <li key={S.ag.key}>
              <button
                type="button"
                className={"ct-bix-sub" + (S.done ? " done" : "") + (p.followKey === S.ag.key ? " following" : "")}
                aria-pressed={p.followKey === S.ag.key}
                onClick={() => p.onFollow(S.ag.key)}
                data-agent={S.ag.key}
                title={(p.followKey === S.ag.key ? "Following " : "Follow ") + S.ag.name}
              >
                <span className="g" style={{ color: S.ag.color }}>
                  {S.ag.glyph}
                </span>
                <span className="nm">{clip(S.ag.short || S.ag.name, 20)}</span>
                <span className={"st " + S.status}>{subLine(M, S)}</span>
                {/* where it is, on its own line: never cut off by a long file name */}
                {(helperWhere(M, S) || S.edits.size > 0) && (
                  <span className="wh">
                    {helperWhere(M, S) && !S.done ? "in " + helperWhere(M, S) : ""}
                    {helperWhere(M, S) && !S.done && S.edits.size ? " · " : ""}
                    {S.edits.size ? `${S.edits.size} changed` : ""}
                  </span>
                )}
              </button>
            </li>
          ))}
        </ul>
      )}
      {A.ag.primary && p.primaryExtra}
      {open && (
        <div className="ct-bix-detail">
          {task && <div className="ct-agent-task">{task}</div>}
          {A.nest && (
            <div className="ct-agent-nest">
              nest:{" "}
              <button type="button" className="ct-link" onClick={() => p.onNest(A)}>
                {A.nest === M.crown ? "trunk" : A.nest.path}
              </button>
            </div>
          )}
          <div className="ct-agent-nums">
            <span>
              read <b>{A.reads.size}</b>
            </span>
            <span>
              planned <b>{A.plan.size + A.planNew.length}</b>
            </span>
            <span className="aff" title="files that import something this agent changed — click a changed file to light them">
              could break <b>{p.affects(A)}</b>
            </span>
          </div>
          {changed.length > 0 && (
            <>
              <h5>Changed · most depended-on first</h5>
              <ul className="ct-bix-files" role="list">
                {changed.slice(0, 8).map((ff) => (
                  <li key={ff.id}>
                    <button type="button" onClick={() => p.onFile(ff)} title={`${ff.path}\n${ff.usedBy.length} files import it — click to light them`}>
                      <span className="fn">{dispName(M, ff)}</span>
                      <span className="dep">{ff.usedBy.length ? ff.usedBy.length + " depend" : "—"}</span>
                    </button>
                  </li>
                ))}
                {changed.length > 8 && <li className="ct-muted">+{changed.length - 8} more in the Session panel</li>}
              </ul>
            </>
          )}
        </div>
      )}
    </li>
  );
}

function Rules({ p }: { p: BirdIndexProps }) {
  return (
    <div className="ct-bix-rules">
      <h4>Rules</h4>
      {!p.zones.length ? (
        <p className="ct-muted">none — ⛔ / ✓ on a changed folder below, or the toolbar's ⛔ Keep out / ✓ Only here</p>
      ) : (
        <ul>
          {p.zones.map((z) => {
            const g = isGreen(z);
            const drawn = p.tzones.some((t) => t.z.id === z.id);
            const full = z.name || z.pattern.replace(/^\/+/, "").replace(/\/(\*\*)?$/, "");
            const nb = z.waived ? 0 : nBlocked(p.agents, z);
            return (
              <li key={z.id} className={"ct-rule " + (g ? "only" : "keep") + (z.waived ? " waived" : "")}>
                <button
                  type="button"
                  className="ct-rule-go"
                  onClick={() => p.onGoZone(z)}
                  disabled={!drawn}
                  title={(drawn ? "Zoom there — " : "Matches no file on the tree yet — ") + z.pattern + (z.note ? "\n" + z.note : "")}
                >
                  <span className="sw" aria-hidden="true">
                    {g ? "✓" : "⛔"}
                  </span>
                  <span className="tx" title={full}>
                    {z.name || tailPath(full)}
                  </span>
                  <small>{z.waived ? "allowed here" : z.scope === "worktree" ? "this worktree" : "whole repo"}</small>
                  {nb > 0 && <small className="nb">{blockedTxt(nb)}</small>}
                </button>
                <button type="button" className="ct-x" aria-label={"Remove rule " + (z.name || z.pattern)} title="Remove this rule" disabled={p.zoneBusy === z.id} onClick={() => p.onRemoveZone(z)}>
                  ×
                </button>
              </li>
            );
          })}
        </ul>
      )}
    </div>
  );
}

/** Where the changes are, what could break, and one-click ⛔ / ✓ per changed folder. */
function Changes({ p, sum, riskOpen, onRisk }: { p: BirdIndexProps; sum: ChangeSummary; riskOpen: boolean; onRisk: () => void }) {
  const M = p.M;
  if (!sum.total)
    return (
      <div className="ct-bix-changes">
        <h4>Changes</h4>
        <p className="ct-muted">No changes yet.</p>
      </div>
    );
  const risk = sum.ranked[0];
  const maxDep = Math.max(1, risk ? risk.usedBy.length : 1);
  const live = p.tzones.filter((t) => !t.waived && t.node);
  const hasOnly = live.some((t) => t.type === "only");
  return (
    <div className="ct-bix-changes">
      <div className="ct-bix-chead">
        <h4>
          Changes{" "}
          <span className="ct-bix-total" title="Changed files on this repo, all sessions (each file counted once)">
            <b>{sum.total}</b> in all
          </span>
        </h4>
        <button
          type="button"
          className={"ct-bix-risk-tog" + (riskOpen ? " on" : "")}
          aria-pressed={riskOpen}
          title={
            riskOpen
              ? "Showing on the map: the folders whose files import a changed file (gold, counted) — click to hide"
              : "What could break: show on the map which folders import the changed files, and rank the changed files"
          }
          onClick={onRisk}
        >
          ⚠ Could break
        </button>
      </div>
      {/* 9 in all = this session's 7 (the pane header's "7 files") + the other sessions' 2 */}
      <div className="ct-bix-sum ct-bix-split" title="This session = the pane header's file count (its branch, helpers included)">
        this session <b>{sum.mine}</b> + other sessions <b>{sum.others}</b>
      </div>
      <div className="ct-bix-sum">
        {sum.tops.map(([k, n], i) => (
          <span key={k}>
            {i ? " · " : ""}
            {clip(k, 18)} <b>{n}</b>
          </span>
        ))}
      </div>
      {risk && risk.usedBy.length > 0 && !riskOpen && (
        <button
          type="button"
          className={"ct-bix-atrisk" + (p.riskFile === risk.id ? " on" : "")}
          aria-pressed={p.riskFile === risk.id}
          onClick={() => (p.onRiskFile ? p.onRiskFile(risk) : p.onFile(risk))}
          title={
            p.riskFile === risk.id
              ? `${risk.path}\nOn the map: the folders holding the ${risk.usedBy.length} files that import it — click to clear`
              : `${risk.path}\nclick to show on the map which folders hold the ${risk.usedBy.length} files that import it`
          }
        >
          <span className="k">
            Most at risk · <b>{risk.usedBy.length}</b> import it
          </span>
          <span className="v fn">{dispName(M, risk)}</span>
        </button>
      )}
      {riskOpen && (
        <ol className="ct-bix-ranked" aria-label="Changed files by how many files import them">
          {sum.ranked.slice(0, 10).map((f) => (
            <li key={f.id}>
              <button
                type="button"
                className={p.riskFile === f.id ? "on" : ""}
                aria-pressed={p.riskFile === f.id}
                onClick={() => (p.onRiskFile ? p.onRiskFile(f) : p.onFile(f))}
                title={`${f.path}\n${f.usedBy.length} files import it — ${p.riskFile === f.id ? "click to show every changed file's again" : "click to show only its dependents on the map"}`}
              >
                <span className="fn">{dispName(M, f)}</span>
                <span className="bar" aria-hidden="true">
                  <span style={{ width: Math.max(2, Math.round((100 * f.usedBy.length) / maxDep)) + "%" }} />
                </span>
                <span className="dep">{f.usedBy.length}</span>
              </button>
            </li>
          ))}
          {sum.ranked.length > 10 && <li className="ct-muted">+{sum.ranked.length - 10} more</li>}
        </ol>
      )}
      {/* the row buttons say what they do, always (not only on hover) */}
      <div className="ct-bix-fcap" aria-hidden="true">
        <span>folders</span>
        <span className="k">⛔ keep out</span>
        <span className="o">✓ only here</span>
      </div>
      <ul className="ct-bix-folders" role="list" aria-label="Changed folders">
        {sum.folders.map((cf) => {
          const n = cf.n;
          const keep = live.find((t) => t.type === "keep" && isUnder(n, t.node!)) || null;
          const only = live.find((t) => t.type === "only" && isUnder(n, t.node!)) || null;
          // only-here binds THIS session's worktree: another session's changes elsewhere are not affected by it
          const otherWt = hasOnly && !only && cf.mine === 0;
          const exempt = hasOnly && !only && !otherWt;
          const canPaint = n.depth >= 1;
          const zoneBtn = (kind: "keep" | "only", t: TZone | null) => {
            const sign = kind === "keep" ? "⛔" : "✓";
            const what = kind === "keep" ? "Keep out" : "Only here";
            const exact = !!t && t.node === n;
            return (
              <button
                type="button"
                className={"ct-bix-zb " + kind + (t ? " on" : "")}
                aria-pressed={!!t}
                aria-label={(exact ? "Remove " + what + " on " : t ? what + " already covers " : what + " on ") + cf.label}
                title={
                  exact
                    ? `${sign} ${what} is on here — click to remove it`
                    : t
                      ? `${sign} inside ${what.toLowerCase()} · ${t.label}`
                      : kind === "keep"
                        ? `⛔ Keep agents out of ${cf.label}`
                        : `✓ Agents may edit only in ${cf.label}`
                }
                disabled={!canPaint || (!!t && !exact) || (exact && p.zoneBusy === t!.z.id)}
                onClick={() => (exact ? p.onRemoveZone(t!.z) : p.onPaint(kind, n))}
              >
                {sign}
              </button>
            );
          };
          return (
            <li key={n.id + ":" + cf.label} className={"ct-bix-folder" + (keep ? " keep" : "") + (only ? " only" : "") + (exempt ? " exempt" : "") + (otherWt ? " other" : "")}>
              <div className="ct-bix-fl1">
                <button type="button" className="ct-bix-fname" onClick={() => p.onFolder(n)} title={`${n.path || cf.label} — show it on the tree`}>
                  <span className="dots" aria-hidden="true">
                    {cf.cols.map((c) => (
                      <i key={c} style={{ background: c }} />
                    ))}
                  </span>
                  <span className="nm">{cf.label}</span>
                  <span className="cnt">✎{cf.files.length}</span>
                </button>
                {zoneBtn("keep", keep)}
                {zoneBtn("only", only)}
              </div>
              <div className="ct-bix-fl2">
                {keep ? (
                  <span className="keep">⛔ kept out · agents may only read</span>
                ) : otherWt ? (
                  <span className="other" title="✓ Only here binds this session's worktree; the other session edits in its own worktree">
                    other worktree · not affected
                  </span>
                ) : exempt ? (
                  <span className="exempt" title="Outside ✓ only here, but changed before it was set: kept exempt, so these files may still be edited">
                    already changed · exempt from ✓
                  </span>
                ) : (
                  <span>
                    {cf.files
                      .slice(0, 2)
                      .map((f) => dispName(M, f))
                      .join(", ")}
                    {cf.files.length > 2 ? ` +${cf.files.length - 2}` : ""}
                  </span>
                )}
              </div>
            </li>
          );
        })}
      </ul>
    </div>
  );
}
