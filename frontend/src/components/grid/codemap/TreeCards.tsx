/** The tree's HUD pieces, in the app's own furniture (cm-* toolbar buttons,
 * --panel cards with the 1px --border and 8px radius of the Diff/Queue tabs,
 * 10px uppercase section heads like the Session lists): the Legend, Activity,
 * the file / folder / blast cards and the hover tooltip (the agents, Rules and
 * Changes are the bird index, BirdIndex.tsx).
 * Everything here is text the canvas only paints — the accessible twin of
 * the picture. */

import { useEffect, useRef, type ReactNode } from "react";
import type { FeedRecord, FileView, OutlineSymbol, RedZone } from "../../../api/types";
import { relTime } from "../../../lib/format";
import { isGreen } from "../../../lib/codemap";
import { agentBlast } from "../../../lib/codetree/blast";
import { birdSprite, tinted } from "../../../lib/flock";
import { baseOf, dirOf, isUnder, shortName, type Model, type TFile, type TNode } from "../../../lib/codetree/model";
import { STATUS_TXT } from "../../../lib/codetree/live";
import { helperLine } from "../../../lib/codetree/subagents";
import { zoneOfFile, zoneWhere } from "../../../lib/codetree/zones";
import type { AgentState, Badge, Hit, TZone } from "../../../lib/codetree/types";

const plural = (n: number, one: string, many = one + "s") => `${n} ${n === 1 ? one : many}`;
const linesTxt = (f: TFile) => (f.kind === "a" ? "asset" : `~${(f.lines || 1).toLocaleString()} lines`);
export const nodeName = (M: Model, n: TNode) =>
  n.kind === "root" || n.kind === "pile" ? n.label || n.name : n.depth === 0 ? M.name : n.disp || shortName(n.name);
/** A file's display name: parent folder + name when the basename is not unique. */
export function dispName(M: Model, f: TFile | null | undefined): string {
  if (!f) return "";
  if ((M.nameCount.get(f.name) || 0) > 1 && f.path.includes("/")) return baseOf(dirOf(f.path)) + "/" + f.name;
  return f.name;
}
const clip = (s: string, n = 22) => (s.length > n ? s.slice(0, n - 1) + "…" : s);

/** The bird sprite tinted in the agent's colour. */
export function BirdIcon({ color }: { color: string }) {
  const ref = useRef<HTMLCanvasElement | null>(null);
  useEffect(() => {
    const c = ref.current;
    if (!c) return;
    const draw = () => {
      const g = c.getContext("2d");
      const img = tinted(color);
      if (!g) return;
      g.clearRect(0, 0, c.width, c.height);
      if (img) g.drawImage(img, 1, 1, c.width - 2, c.height - 2);
      else {
        g.fillStyle = color;
        g.beginPath();
        g.ellipse(c.width / 2, c.height / 2, c.width / 3, c.height / 4, 0, 0, Math.PI * 2);
        g.fill();
      }
    };
    draw();
    const s = birdSprite();
    if (s && !(s.complete && s.naturalWidth)) {
      s.addEventListener("load", draw);
      return () => s.removeEventListener("load", draw);
    }
  }, [color]);
  return <canvas ref={ref} width={44} height={32} className="ct-bird-ic" aria-hidden="true" />;
}

/** A helper's one line: "editing x.py" / "reading x.py" / "done". */
export function subLine(M: Model, S: AgentState): string {
  const bk = S.blocked[S.blocked.length - 1];
  const name = S.status === "blocked" && bk ? (bk.f ? dispName(M, bk.f) : bk.path) : S.file ? dispName(M, S.file) : "";
  return helperLine(S.status, name);
}

/** The legend: the same sentences the prototype's legend says, with swatches
 * drawn in the theme's colours (CSS variables). */
export function LegendCard({ open, onToggle, primary }: { open: boolean; onToggle: () => void; primary: string }) {
  return (
    <div className={"ct-card ct-legend" + (open ? "" : " collapsed")}>
      <h4>
        <button type="button" className="ct-tog" aria-expanded={open} onClick={onToggle}>
          Legend <span aria-hidden="true">{open ? "▾" : "▸"}</span>
        </button>
      </h4>
      {open && (
        <ul>
          <li>
            <svg className="sw" viewBox="0 0 12 12">
              <path d="M1 11 Q2 2 11 1 Q10 10 1 11Z" fill="var(--ct-legend-leaf)" opacity=".55" />
            </svg>
            grey tree = the repo, unchanged · leaf = file · branch = folder · roots = tests
          </li>
          <li>
            <svg className="sw" viewBox="0 0 12 12">
              <circle cx="6" cy="6" r="5.5" fill={primary} opacity=".35" />
              <path d="M2 10 Q3 3 10 2 Q9 9 2 10Z" fill={primary} stroke="var(--text)" strokeWidth=".8" />
            </svg>
            bright leaf = a changed file, in the colour of the bird that changed it · a ring breathing = being edited now
          </li>
          <li>
            <svg className="sw" viewBox="0 0 12 12">
              <path d="M6 12 Q6 6 10 1" stroke={primary} strokeWidth="2.2" fill="none" />
            </svg>
            lit branch = the path from the trunk to a change · "core ●4" = 4 changed in there
          </li>
          <li>
            <svg className="sw" viewBox="0 0 12 12">
              <path d="M2 7 Q6 1 11 4 Q8 6 7 9 Q4 9 2 7Z" fill={primary} />
            </svg>
            bird = an agent · small bird = its helper · nest = where it mostly works
          </li>
          <li>
            <svg className="sw" viewBox="0 0 12 12">
              <circle cx="6" cy="6" r="3" fill={primary} />
            </svg>
            <span>
              dot = read · bud = planned <small>(zoom in to see)</small>
            </span>
          </li>
          <li>
            <svg className="sw" viewBox="0 0 12 12">
              <circle cx="6" cy="6" r="4" fill="var(--gold)" />
            </svg>
            what could break: hover or click a bright leaf — the files that import it light up gold
          </li>
          <li>
            <svg className="sw" viewBox="0 0 12 12">
              <circle cx="6" cy="6" r="5" fill="color-mix(in srgb, var(--red) 25%, transparent)" stroke="var(--red)" strokeWidth="1.6" />
            </svg>
            red area = keep out (agents may read, never edit) · ✕ = an edit was blocked
          </li>
          <li>
            <svg className="sw" viewBox="0 0 12 12">
              <rect x="0" y="0" width="12" height="12" fill="var(--ct-dusk)" />
              <circle cx="6" cy="6" r="4" fill="color-mix(in srgb, var(--green) 20%, transparent)" stroke="var(--green)" strokeWidth="1.4" />
            </svg>
            green area = only here: agents may edit only inside it; the rest fogs
          </li>
        </ul>
      )}
    </div>
  );
}

export interface ActRow {
  key: string;
  ts: number;
  A: AgentState | null;
  text: string;
  path: string;
  bad: boolean;
  rule?: "keep" | "only";
}

/** Activity rows: each bird's touches (reads, edits, refusals) and the rules,
 * newest first. */
export function activityRows(M: Model, agents: AgentState[], feed: FeedRecord[], zones: RedZone[], primaryKey: string): ActRow[] {
  const out: ActRow[] = [];
  const prim0 = agents.find((a) => a.ag.key === primaryKey) || null;
  // a subagent's calls are its own bird's while it is listed (then its parent's)
  const subOf = new Map<string, AgentState>();
  for (const A of agents) if (A.subInfo) subOf.set(A.subInfo.id, A);
  const seen = new Set<string>();
  for (let i = feed.length - 1; i >= 0 && out.length < 40; i--) {
    const r = feed[i];
    if (r.id) {
      if (seen.has(r.id)) continue;
      seen.add(r.id);
    }
    const ts = r.ts || 0;
    const prim = (r.agent && subOf.get(String(r.agent))) || prim0;
    if (r.deny && r.deny.path) {
      const f = M.byPath.get(r.deny.path);
      const what = r.deny.push ? "a push" : f ? dispName(M, f) : r.deny.path;
      out.push({
        key: "d" + i, ts, A: prim, path: r.deny.push ? "" : r.deny.path, bad: true,
        text: `⛔ blocked at ${what} — ${r.deny.kind === "green" ? "outside only-here" : "keep out" + (r.deny.name ? ": " + r.deny.name : "")}`,
      });
      continue;
    }
    const w = r.writes && r.writes[0];
    const rd = r.reads && r.reads[0];
    if (w && r.ev !== "pre") {
      const f = M.byPath.get(w);
      const d = f ? f.usedBy.length : 0;
      out.push({ key: "w" + i, ts, A: prim, path: w, bad: false, text: `${r.tool === "Write" && !f ? "created" : "edited"} ${f ? dispName(M, f) : w}${d ? ` — ${d} depend on it` : ""}` });
    } else if (rd && (r.ev === "pre" || r.kind !== "read")) {
      const f = M.byPath.get(rd);
      out.push({ key: "r" + i, ts, A: prim, path: rd, bad: false, text: `read ${f ? dispName(M, f) : rd}${r.reads!.length > 1 ? ` +${r.reads!.length - 1}` : ""}` });
    } else if (r.kind === "plan" && r.ev !== "pre") out.push({ key: "p" + i, ts, A: prim, path: "", bad: false, text: "declared a plan (buds)" });
    else if (r.kind === "agent" && !r.agent) {
      const what = [r.atype, r.desc].filter(Boolean).join(" · ");
      const back = r.ev !== "pre"; // the call's post: the helper came back
      out.push({ key: "h" + i, ts, A: prim, path: "", bad: false, text: `${back ? "a helper finished" : "sent out a helper"}${what ? ": " + what : ""}` });
    }
  }
  for (const A of agents) {
    if (A.ag.primary || A.ag.parent) continue; // a helper's edits are feed rows already
    for (const [id, ts] of A.edits) {
      const f = M.files[id];
      out.push({ key: "o" + A.ag.key + id, ts, A, path: f.path, bad: false, text: `edited ${dispName(M, f)}${f.usedBy.length ? ` — ${f.usedBy.length} depend on it` : ""}` });
    }
  }
  for (const z of zones)
    if (z.created) out.push({ key: "z" + z.id, ts: z.created, A: null, path: "", bad: false, rule: isGreen(z) ? "only" : "keep", text: `${isGreen(z) ? "✓ only here" : "⛔ keep out"} set on ${z.name || z.pattern}` });
  out.sort((a, b) => b.ts - a.ts);
  return out.slice(0, 48);
}

export function ActivityCard(p: {
  rows: ActRow[];
  skew: number;
  open: boolean;
  onToggle: () => void;
  onRow: (r: ActRow) => void;
  zoneOf: (r: ActRow) => RedZone | null;
  onZone: (z: RedZone) => void;
  /** a small pane: folded, the card is a one-line ticker (the latest row and +), not a header over a row */
  ticker?: boolean;
}) {
  const tick = !!p.ticker && !p.open;
  return (
    <div className={"ct-card ct-activity" + (p.open ? "" : " collapsed") + (tick ? " ticker" : "")}>
      <h4>
        {!tick && "Activity"}
        <button type="button" className="ct-tog" aria-expanded={p.open} aria-label={p.open ? "Collapse activity" : "Expand activity"} onClick={p.onToggle}>
          {p.open ? "–" : "+"}
        </button>
      </h4>
      <ul className="ct-act-list">
        {!p.rows.length && <li className="ct-muted">Nothing yet — the agent hasn't touched a file since the map armed.</li>}
        {(p.open ? p.rows : p.rows.slice(0, 1)).map((r) => (
          <li key={r.key}>
            <button
              type="button"
              className={"ct-act-row" + (r.bad ? " bad" : "") + (r.rule ? " rule " + r.rule : "")}
              disabled={!r.path && !r.rule}
              onClick={() => {
                if (r.rule) {
                  const z = p.zoneOf(r);
                  if (z) p.onZone(z);
                } else p.onRow(r);
              }}
            >
              <span className="tm">{r.ts ? relTime(r.ts - p.skew) : ""}</span>
              <span className="dot" style={{ background: r.A ? r.A.ag.color : r.rule === "only" ? "var(--green)" : "var(--red)" }} aria-hidden="true" />
              <span className="tx">
                {r.A && (
                  <span className="who" style={{ color: r.A.ag.color }}>
                    {clip(r.A.ag.short || r.A.ag.name, 18)}{" "}
                  </span>
                )}
                {r.text}
              </span>
            </button>
          </li>
        ))}
      </ul>
    </div>
  );
}

// --- the info card -----------------------------------------------------------

function depOf(M: Model, A: AgentState, id: number): TFile | null {
  for (const [fid] of A.edits) if (M.files[fid].usedBy.includes(id)) return M.files[fid];
  return null;
}

const CLASSY = new Set(["class", "interface", "type", "struct", "enum", "trait", "record", "table", "model"]);
function outline(fv: FileView | null) {
  const cls: string[] = [],
    fns: string[] = [];
  const walk = (s: OutlineSymbol[]) => {
    for (const x of s) {
      if (CLASSY.has(x.kind)) cls.push(x.name);
      else if (x.kind !== "route" && x.kind !== "var" && x.kind !== "const" && x.kind !== "import") fns.push(x.name);
      if (x.children && x.children.length && CLASSY.has(x.kind)) walk(x.children);
    }
  };
  if (fv) walk(fv.symbols);
  // the public API first: a reader cares about what the file offers before its helpers
  const pub = (a: string[]) => a.slice().sort((p, q) => Number(/^_/.test(p)) - Number(/^_/.test(q)));
  const routes = fv ? fv.entry.filter((e) => e.kind === "http").map((e) => (e.method + " " + e.route).trim()) : [];
  return { cls: pub(cls), fns: pub(fns), routes };
}

function Chips({ items, max = 12, onPick }: { items: string[]; max?: number; onPick?: (s: string) => void }) {
  if (!items.length) return null;
  return (
    <div className="ct-chips">
      {items.slice(0, max).map((x, i) =>
        onPick ? (
          <button type="button" key={x + i} className="ct-chip" onClick={() => onPick(x)} title={x}>
            {x}
          </button>
        ) : (
          <code key={x + i} className="ct-chip">
            {x}
          </code>
        )
      )}
      {items.length > max && <code className="ct-chip more">+{items.length - max} more</code>}
    </div>
  );
}

export interface FileCardProps {
  M: Model;
  f: TFile;
  agents: AgentState[];
  zones: TZone[];
  fv: FileView | null;
  fvErr: string;
  pinned: boolean;
  changed: boolean;
  outside: boolean;
  allowState: string;
  onClose: () => void;
  onBlast: () => void;
  onCentre: () => void;
  onFolder: () => void;
  onDiff: () => void;
  onKeep: () => void;
  onOnly: () => void;
  onAllow: () => void;
  onFile: (path: string) => void;
}

export function FileCard(p: FileCardProps) {
  const { M, f } = p;
  const used = f.usedBy.length,
    imp = f.imports.length;
  const kind = f.test ? "test" : f.kind === "c" ? "code" : f.kind === "a" ? "asset" : "doc / config";
  const who: ReactNode[] = [];
  for (const A of p.agents) {
    const bits: string[] = [];
    if (A.edits.has(f.id)) bits.push(A.created.has(f.id) ? "created it" : "edited it");
    if (A.reads.has(f.id)) bits.push("read it");
    if (A.plan.has(f.id)) bits.push("plans to touch it (bud)");
    for (const bk of A.blocked) if (bk.f === f) bits.push("was blocked here (" + (bk.kind === "keep" ? "keep out" : "outside only-here") + ")");
    const dep = depOf(M, A, f.id);
    if (dep) bits.push(`imports ${dep.name} — ${A.ag.name}'s edit could break it`);
    if (bits.length)
      who.push(
        <div className="ct-who" key={A.ag.key}>
          <span style={{ color: A.ag.color }}>
            {A.ag.glyph} {clip(A.ag.name)}
          </span>{" "}
          {bits.join(" · ")}
        </div>
      );
  }
  const zf = zoneOfFile(f, p.zones);
  const hasOnly = p.zones.some((z) => z.type === "only" && !z.waived);
  const { cls, fns, routes } = outline(p.fv);
  const lines = p.fv && p.fv.loc ? `${p.fv.loc.toLocaleString()} lines` : linesTxt(f);
  return (
    <>
      <button type="button" className="ct-close" aria-label="Close (Esc)" title="Close (Esc)" onClick={p.onClose}>
        ×
      </button>
      <h3>{f.name}</h3>
      <div className="ct-path">{f.path}</div>
      <div className="ct-facts">
        <span>{lines}</span>
        <span>{kind}</span>
        <span className={used ? "gold" : ""}>used by {plural(used, "file")}</span>
        <span>imports {imp}</span>
      </div>
      {who}
      {zf ? (
        <div className="ct-who block">
          ⛔ {zf.type === "keep" ? "inside keep out" : "outside the only-here zone"} ({zf.z.label}) — {zf.type === "keep" ? "reads allowed, edits blocked" : "edits blocked here"}
        </div>
      ) : hasOnly ? (
        <div className="ct-who only">✓ inside the only-here zone — edits allowed</div>
      ) : null}
      {p.fvErr && !p.fv ? <p className="ct-muted">Couldn't read its outline: {p.fvErr}</p> : !p.fv ? <p className="ct-muted">Reading its outline…</p> : null}
      {cls.length > 0 && (
        <>
          <h5>Classes</h5>
          <Chips items={cls} />
        </>
      )}
      {fns.length > 0 && (
        <>
          <h5>
            Functions {fns.some((x) => !/^_/.test(x)) && fns.some((x) => /^_/.test(x)) ? <span className="ct-h5-note">· public first</span> : null}
          </h5>
          <Chips items={fns} />
        </>
      )}
      {routes.length > 0 && (
        <>
          <h5>Routes</h5>
          <Chips items={routes} max={10} />
        </>
      )}
      {f.test && f.tests && (
        <>
          <h5>Tests</h5>
          <Chips items={[f.tests.path]} />
        </>
      )}
      {p.fv && p.fv.tested_by && p.fv.tested_by.length > 0 && (
        <>
          <h5>Tested by</h5>
          <Chips items={p.fv.tested_by} max={6} onPick={p.onFile} />
        </>
      )}
      {p.fv && !cls.length && !fns.length && !routes.length && <p className="ct-muted">No classes or functions found.</p>}
      <div className="ct-actions">
        {used > 0 && (
          <button type="button" className="cm-btn gold" onClick={p.onBlast}>
            {p.pinned ? "Hide" : "Show"} the {used} that depend on it
          </button>
        )}
        <button type="button" className="cm-btn" onClick={p.onCentre}>
          Centre on it
        </button>
        {f.node && f.node.depth >= 1 && (
          <button type="button" className="cm-btn" onClick={p.onFolder}>
            Folder: {clip(f.node.kind === "crown" ? baseOf(f.node.name) : f.node.label || f.node.name, 18)}
          </button>
        )}
        {p.changed && (
          <button type="button" className="cm-btn" onClick={p.onDiff}>
            Open diff
          </button>
        )}
        <button type="button" className="cm-btn red" onClick={p.onKeep} title="Keep agents out of this file (reads stay allowed)">
          ⛔ Keep out
        </button>
        {p.outside ? (
          <button type="button" className="cm-btn green" onClick={p.onAllow} disabled={p.allowState === "busy" || p.allowState === "done"} title="Add this file to the green zones (this worktree) and tell the agent">
            {p.allowState === "busy" ? "Allowing…" : p.allowState === "done" ? "Allowed ✓" : "Allow this file"}
          </button>
        ) : (
          <button type="button" className="cm-btn green" onClick={p.onOnly} title="Agents may edit only this file; everything else dims">
            ✓ Only here
          </button>
        )}
      </div>
    </>
  );
}

export interface NodeCardProps {
  M: Model;
  n: TNode;
  agents: AgentState[];
  zones: TZone[];
  serverZones: RedZone[];
  folded: boolean;
  onClose: () => void;
  onZoom: () => void;
  onFold: () => void;
  onKeep: () => void;
  onOnly: () => void;
  onRemove: (z: RedZone) => void;
  onKid: (k: TNode) => void;
  onAgent: (A: AgentState) => void;
}

export function NodeCard(p: NodeCardProps) {
  const { M, n } = p;
  const isRoot = n.kind === "root",
    isPile = n.kind === "pile",
    isTrunk = n.depth === 0 && !isPile;
  const title = nodeName(M, n);
  const path = isTrunk ? (n === M.roots ? "all tests" : "repo root") : isRoot ? "tests · " + (n.crownTwin ? n.crownTwin.path : n.name) : isPile ? "on the ground (docs, CI, config)" : n.path;
  const who: ReactNode[] = [];
  if (!isTrunk)
    for (const A of p.agents) {
      const bits: string[] = [];
      if (A.nest === n) bits.push("nests here");
      let e = 0,
        r = 0,
        pl = 0;
      for (const [id] of A.edits) if (isUnder(M.files[id].node, n)) e++;
      for (const [id] of A.reads) if (isUnder(M.files[id].node, n)) r++;
      for (const id of A.plan) if (isUnder(M.files[id].node, n)) pl++;
      if (e) bits.push(`edited ${e}`);
      if (r) bits.push(`read ${r}`);
      if (pl) bits.push(`${pl} planned`);
      if (bits.length)
        who.push(
          <div className="ct-who" key={A.ag.key}>
            <span style={{ color: A.ag.color }}>
              {A.ag.glyph} {clip(A.ag.name)}
            </span>{" "}
            {bits.join(" · ")}
          </div>
        );
    }
  const here = p.zones.filter((z) => z.node === n);
  const keepZ = here.find((z) => z.type === "keep"),
    onlyZ = here.find((z) => z.type === "only");
  let inherited: ReactNode = null;
  for (let q = n.parent; q && !inherited; q = q.parent) {
    const z = p.zones.find((zz) => zz.type === "keep" && zz.node === q && !zz.waived);
    if (z) inherited = <div className="ct-who block">⛔ inside keep out · {z.label}</div>;
  }
  const hasOnly = p.zones.some((z) => z.type === "only" && !z.waived);
  if (!keepZ && !onlyZ && !inherited && hasOnly && !isTrunk)
    inherited = n.lit ? <div className="ct-who only">✓ inside the only-here zone — edits allowed</div> : <div className="ct-who block">⛔ outside the only-here zone — edits blocked here</div>;
  const kids = (n.kids || []).filter((k) => k.nFiles).slice().sort((a, b) => b.nFiles - a.nFiles);
  return (
    <>
      <button type="button" className="ct-close" aria-label="Close (Esc)" title="Close (Esc)" onClick={p.onClose}>
        ×
      </button>
      <h3>{title}</h3>
      <div className="ct-path">{path}</div>
      <div className="ct-facts">
        <span>{isRoot ? "tests" : isPile ? "ground pile" : isTrunk ? (n === M.roots ? "all tests" : "repo (the trunk)") : "folder"}</span>
        <span>
          {plural(n.nFiles, isTrunk && n === M.crown ? "code file" : "file")}
        </span>
        {n.lines > 0 && <span>~{(n.lines).toLocaleString()} lines</span>}
        {n.kids.length > 0 && !isTrunk && <span>{plural(n.kids.length, "sub-folder")}</span>}
      </div>
      {isTrunk && n === M.crown && (
        <>
          <div className="ct-facts">
            <span>{plural(M.roots.nFiles, "test")} (roots)</span>
            <span>{M.piles.reduce((a, q) => a + q.nFiles, 0)} docs / config (ground)</span>
            <span>{plural(M.crown.kids.length, "top-level folder")}</span>
          </div>
          <h5>Agents</h5>
          {p.agents.map((A) => (
            <div className="ct-who" key={A.ag.key}>
              <button type="button" className="ct-link" style={{ color: A.ag.color }} onClick={() => p.onAgent(A)}>
                {A.ag.glyph} {clip(A.ag.name)}
              </button>{" "}
              {STATUS_TXT[A.status]}
              {A.file ? " · " + dispName(M, A.file) : ""} · edited {A.edits.size}, read {A.reads.size}
              {A.nest && A.nest !== M.crown ? " · nest " + A.nest.path : ""}
            </div>
          ))}
          <h5>Rules</h5>
          {p.serverZones.length ? (
            p.serverZones.map((z) => (
              <div className={"ct-who " + (isGreen(z) ? "only" : "keep")} key={z.id}>
                {isGreen(z) ? "✓ only here" : "⛔ keep out"} · {z.name || z.pattern}
              </div>
            ))
          ) : (
            <p className="ct-muted">no rules yet — ⛔ Keep out / ✓ Only here paint one on a branch</p>
          )}
        </>
      )}
      {who}
      {keepZ && <div className="ct-who keep">⛔ keep out · {zoneWhere(keepZ.z, keepZ.waived)}</div>}
      {onlyZ && <div className="ct-who only">✓ only here · {onlyZ.z.scope === "session" ? zoneWhere(onlyZ.z) : "this worktree"}</div>}
      {inherited}
      {kids.length > 0 && (
        <>
          <h5>Sub-folders</h5>
          <div className="ct-chips">
            {kids.slice(0, 12).map((k) => (
              <button type="button" key={k.id} className="ct-chip" title={"Open " + k.path} onClick={() => p.onKid(k)}>
                {k.depth === 1 ? k.name : k.core || baseOf(k.name)} <span className="n">{k.nFiles}</span>
              </button>
            ))}
            {kids.length > 12 && <code className="ct-chip more">+{kids.length - 12} more</code>}
          </div>
        </>
      )}
      <div className="ct-actions">
        <button type="button" className="cm-btn" onClick={p.onZoom}>
          {isTrunk ? "Whole tree" : "Zoom to it"}
        </button>
        {!isTrunk && (
          <button type="button" className="cm-btn" onClick={p.onFold}>
            {p.folded ? "Unfold" : "Fold"}
          </button>
        )}
        {!isTrunk &&
          (keepZ ? (
            <button type="button" className="cm-btn red" onClick={() => p.onRemove(keepZ.z)}>
              Remove ⛔ keep out
            </button>
          ) : (
            <button type="button" className="cm-btn red" onClick={p.onKeep} title="Agents may read here; every edit is blocked">
              ⛔ Keep agents out
            </button>
          ))}
        {!isTrunk &&
          (onlyZ ? (
            <button type="button" className="cm-btn green" onClick={() => p.onRemove(onlyZ.z)}>
              Remove ✓ only here
            </button>
          ) : (
            <button type="button" className="cm-btn green" onClick={p.onOnly} title="The one place agents may edit; everything else dims">
              ✓ Only here
            </button>
          ))}
      </div>
    </>
  );
}

export function BlastCard(p: { M: Model; bd: Badge; onClose: () => void; onFrame: () => void; onFolder: () => void; onFile: (f: TFile) => void }) {
  const { M, bd } = p;
  const n = bd.node;
  const where = n.kind === "pile" ? n.label : n.depth === 0 ? (n === M.roots ? "tests" : "repo root files") : n.kind === "root" ? n.label : n.disp || n.name;
  const srcs = [...bd.files].map((id) => M.files[id]).filter(Boolean);
  const groups = new Map<string, TFile[]>();
  for (const id of bd.ids) {
    const f = M.files[id];
    const k = f.node ? (f.node.kind === "pile" || f.node.kind === "root" ? f.node.label || f.node.name : f.node.path || M.name) : "?";
    const g = groups.get(k);
    if (g) g.push(f);
    else groups.set(k, [f]);
  }
  return (
    <>
      <button type="button" className="ct-close" aria-label="Close (Esc)" title="Close (Esc)" onClick={p.onClose}>
        ×
      </button>
      <h3 className="ct-gold">{plural(bd.n, "file")} could break</h3>
      <div className="ct-path">
        in {where} · they import {srcs.map((f) => f.name).join(", ")}
      </div>
      {bd.ag && (
        <div className="ct-who">
          <span style={{ color: bd.ag.color }}>
            {bd.ag.glyph} {clip(bd.ag.name)}
          </span>{" "}
          edited {srcs.map((f) => f.name).join(", ")}
        </div>
      )}
      <p className="ct-muted">Gold leaves on the tree. Nothing has broken yet — these are the files whose imports changed: the ones to check or test.</p>
      {[...groups.entries()]
        .sort((a, b) => b[1].length - a[1].length)
        .map(([k, fs]) => (
          <div key={k}>
            <h5>
              {k} · {fs.length}
            </h5>
            <div className="ct-chips">
              {fs
                .sort((a, b) => b.usedBy.length - a.usedBy.length)
                .map((f) => (
                  <button type="button" key={f.id} className="ct-chip gold" title={f.path + " — open"} onClick={() => p.onFile(f)}>
                    {f.name}
                  </button>
                ))}
            </div>
          </div>
        ))}
      <div className="ct-actions">
        <button type="button" className="cm-btn" onClick={p.onFrame}>
          Frame them
        </button>
        {n.cnt > 0 && (
          <button type="button" className="cm-btn" onClick={p.onFolder}>
            Folder: {clip(where || "", 18)}
          </button>
        )}
        {srcs.length === 1 && srcs[0].leaf && (
          <button type="button" className="cm-btn" onClick={() => p.onFile(srcs[0])}>
            Go to {clip(srcs[0].name, 18)}
          </button>
        )}
      </div>
    </>
  );
}

/** The hover tooltip: what is under the pointer, and what a click does. */
export function TipBody({ M, hit, agents, zones, tool, folded }: { M: Model; hit: Hit; agents: AgentState[]; zones: TZone[]; tool: string; folded: boolean }): ReactNode {
  const act = (n: TNode) =>
    tool === "keep" ? (
      <span className="act keep">click to paint ⛔ keep out on {shortName(n.path || n.name)} · {plural(n.nFiles, "file")}</span>
    ) : tool === "only" ? (
      <span className="act only">click to paint ✓ only here on {shortName(n.path || n.name)} · {plural(n.nFiles, "file")}</span>
    ) : folded ? (
      <i>folded — click to unfold</i>
    ) : (
      <i>click for details · double-click to zoom in</i>
    );
  if (hit.leaf) {
    const f = hit.leaf.file;
    const rows: ReactNode[] = [];
    for (const A of agents) {
      if (A.edits.has(f.id))
        rows.push(
          <div key={"e" + A.ag.id}>
            <span style={{ color: A.ag.color }}>{A.created.has(f.id) ? "created" : "edited"} by {clip(A.ag.name)}</span> <i>— {f.usedBy.length ? `${f.usedBy.length} file${f.usedBy.length === 1 ? "" : "s"} depend on it (lit gold)` : "nothing depends on it"}</i>
          </div>
        );
      else if (A.plan.has(f.id)) rows.push(<div key={"p" + A.ag.id} style={{ color: A.ag.color }}>bud: {clip(A.ag.name)} plans to touch it</div>);
      else if (A.reads.has(f.id)) rows.push(<div key={"r" + A.ag.id} style={{ color: A.ag.color }}>read by {clip(A.ag.name)}</div>);
      const dep = depOf(M, A, f.id);
      if (dep)
        rows.push(
          <div key={"g" + A.ag.id}>
            <span className="ct-gold">imports {dep.name}</span> <i>({clip(A.ag.name)}'s edit — could break)</i>
          </div>
        );
    }
    const zf = zoneOfFile(f, zones);
    return (
      <>
        <b>{f.name}</b>{" "}
        <i>
          {linesTxt(f)} · used by {f.usedBy.length}
        </i>
        <div>
          <i>{f.path}</i>
        </div>
        {rows}
        {zf && zf.type === "keep" && <div className="keep">⛔ inside keep out ({zf.z.label}) — reads allowed, edits blocked</div>}
        {zf && zf.type === "only" && <div className="keep">⛔ outside the only-here zone ({zf.z.label}) — edits here are blocked</div>}
        {!zf && zones.some((z) => z.type === "only" && !z.waived) && <div className="only">✓ inside the only-here zone — edits allowed</div>}
        <div>{tool !== "explore" ? <span className={"act " + tool}>click to paint {tool === "keep" ? "⛔ keep out" : "✓ only here"} on this file</span> : <i>click for details</i>}</div>
      </>
    );
  }
  const n = hit.node || (hit.branch ? hit.branch.node || (hit.branch.owner && hit.branch.owner.depth >= 1 ? hit.branch.owner : null) : null) || hit.clump || hit.pile || null;
  if (n && hit.viaLeaf)
    return (
      <>
        <b>{nodeName(M, n)}</b> <i>folder · {plural(n.nFiles, "file")}</i>
        <div>{act(n)}</div>
        <div>
          <i>zoom in to pick a single file</i>
        </div>
      </>
    );
  if (n)
    return (
      <>
        <b>{nodeName(M, n)}</b>{" "}
        <i>
          {n.kind === "root" ? "tests" : n.kind === "pile" ? "on the ground (docs, CI, config)" : "folder"} · {plural(n.nFiles, "file")}
          {n.lines ? ` · ~${n.lines.toLocaleString()} lines` : ""}
        </i>
        {n.kind === "crown" && n.depth > 1 && (
          <div>
            <i>{n.path}</i>
          </div>
        )}
        <div>{act(n)}</div>
      </>
    );
  if (hit.trunk || hit.branch)
    return (
      <>
        <b>{M.name}</b>{" "}
        <i>
          the trunk = the whole repo · {plural(M.files.length, "file")} ({M.crown.nFiles} code)
        </i>
        <div>
          <i>{tool === "explore" ? "click for the repo card" : "paint a branch, not the trunk"}</i>
        </div>
      </>
    );
  if (hit.tag)
    return (
      <>
        <b>
          {hit.tag.type === "keep" ? "⛔ keep out" : "✓ only here"} · {hit.tag.label}
        </b>{" "}
        <i>{zoneWhere(hit.tag.z, hit.tag.waived)}</i>
        <div>
          <i>{hit.tag.type === "keep" ? "agents may read here but every edit is blocked" : "agents may edit only inside this; the rest is dusk"}</i>
        </div>
        <div>
          <span className="act">{tool === "explore" ? "click for the folder · remove it from its card or Rules" : "click to remove this rule"}</span>
        </div>
      </>
    );
  if (hit.bird) {
    const A = hit.bird;
    return (
      <>
        <b style={{ color: A.ag.color }}>
          {A.ag.glyph} {A.ag.name}
        </b>{" "}
        · {STATUS_TXT[A.status]}
        {A.file ? " " + dispName(M, A.file) : ""}
        {hit.pointer && (
          <div>
            <i>off-screen, in that direction</i>
          </div>
        )}
        <div>
          <i>click to follow this bird</i>
        </div>
      </>
    );
  }
  if (hit.nest) {
    const A = hit.nest;
    return (
      <>
        <b style={{ color: A.ag.color }}>{clip(A.ag.name)}'s nest</b> <i>{A.nest === M.crown ? "trunk" : A.nest ? A.nest.path : ""}</i>
        <div>
          <i>the folder it mostly works in · click for the folder's details</i>
        </div>
      </>
    );
  }
  if (hit.badge) {
    const bd = hit.badge;
    const files = [...bd.files].map((id) => M.files[id].name).join(", ");
    return (
      <>
        <b className="ct-gold">
          {plural(bd.n, "file")} in {nodeName(M, bd.node)}
        </b>{" "}
        import {files}
        {bd.ag ? <i> ({clip(bd.ag.name)}'s edit{bd.files.size > 1 ? "s" : ""})</i> : null}
        <div>
          <i>they could break · click to list them</i>
        </div>
      </>
    );
  }
  if (hit.bud)
    return (
      <>
        <b style={{ color: hit.bud.A.ag.color }}>bud: a new file is planned</b>
        <div>
          <i>{hit.bud.path}</i>
        </div>
      </>
    );
  return null;
}

/** How many leaves a bird's edits make gold (its "affects N"). */
export function affectsOf(M: Model, A: AgentState, cache: Map<number, { h1: Set<number> }>): number {
  return A.edits.size ? agentBlast(M, A, cache).ids.size : 0;
}

