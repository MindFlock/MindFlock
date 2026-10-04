/** The Code Tree: the session's worktree as a tree — trunk = repo, branch =
 * folder (thickness ~ √ of the code beneath), leaf = file, roots underground
 * = tests (under the code they test), ground piles = docs / CI / root config —
 * with the agents as birds on it (docs/web-ui.md "Map tab").
 *
 * This component hosts the canvas (TreeCtl draws and handles input) and the
 * HUD around it in the app's own card / button styles. It holds no zones of
 * its own: painting a rule calls `onPaint`, which goes to the server. */

import { useEffect, useLayoutEffect, useMemo, useRef, useState, type MutableRefObject, type ReactNode } from "react";
import type { FeedRecord, FileView, RedZone } from "../../../api/types";
import type { ZoneClass } from "../../../lib/codemap";
import type { Model, TFile, TNode } from "../../../lib/codetree/model";
import type { AgentState, TZone } from "../../../lib/codetree/types";
import { ActivityCard, BlastCard, FileCard, NodeCard, TipBody, activityRows, affectsOf } from "./TreeCards";
import { BirdIndex } from "./BirdIndex";
import { TreeCtl, type Insets } from "./treeCtl";

export interface CodeTreeProps {
  title: string;
  active: boolean;
  model: Model | null;
  /** layout progress 0..1 while the tree grows (null = not growing) */
  growing: number | null;
  growErr: string;
  files: number;
  agents: AgentState[];
  tzones: TZone[];
  zones: RedZone[];
  feed: FeedRecord[];
  skew: number;
  changed: Set<string>;
  classify: (p: string) => ZoneClass;
  reqBusy: Record<string, string>;
  zoneBusy: string;
  reducedMotion: boolean;
  ctlRef: MutableRefObject<TreeCtl | null>;
  /** extra lines for this session's agent card (plan, scope requests) */
  primaryExtra?: ReactNode;
  primaryTask?: string;
  toast: ReactNode;
  fileView: (path: string) => Promise<FileView>;
  onPaint: (tool: "keep" | "only", target: { node?: TNode | null; file?: TFile | null }) => void;
  onRemoveZone: (z: RedZone) => void;
  onAllow: (path: string) => void;
  onOpenDiff: () => void;
  onNote: (msg: string) => void;
  /** the controller's HUD state moved (tool, history, selection) */
  onChange?: () => void;
}

/** Per-title HUD choices (in memory for the life of the page). */
const HUD = new Map<string, { activity: boolean; risk?: boolean }>();
const FILES_SEEN = new Map<string, FileView>();

export function CodeTree(p: CodeTreeProps) {
  const stageRef = useRef<HTMLDivElement | null>(null);
  const cvRef = useRef<HTMLCanvasElement | null>(null);
  const miniRef = useRef<HTMLCanvasElement | null>(null);
  const [, setTick] = useState(0);
  const bump = () => setTick((t) => (t + 1) & 0xffff);
  // the activity log starts folded (a header line); the legend lives behind "?" in the bird index
  const [activity, setActivity] = useState(HUD.get(p.title)?.activity ?? false);
  // "⚠ Could break": the bird index's ranked list AND the map's gold territories, one switch
  const [risk, setRisk] = useState(HUD.get(p.title)?.risk ?? false);
  /** wide: the full HUD (the bird index a left column); narrow (< 760 x 560): the bird index a slim left rail +
   * the card a bottom sheet; tiny (< 480 x 330): the rail alone */
  const [size, setSize] = useState<"wide" | "narrow" | "tiny">("wide");
  const narrow = size !== "wide";
  const [actNarrow, setActNarrow] = useState(false);
  const [fv, setFv] = useState<FileView | null>(null);
  const [fvErr, setFvErr] = useState("");
  const blastCache = useRef(new Map<number, { h1: Set<number> }>());
  // the controller calls back through the LATEST props
  const pref = useRef(p);
  pref.current = p;

  // --- the controller (one per mount; the camera survives remounts) ---------
  useEffect(() => {
    const cv = cvRef.current,
      st = stageRef.current;
    if (!cv || !st) return;
    const ctl = new TreeCtl(cv, st, p.title, {
      paint: (tool, target) => pref.current.onPaint(tool, target),
      removeZone: (z) => pref.current.onRemoveZone(z.z),
      note: (m) => pref.current.onNote(m),
      changed: () => {
        bump();
        pref.current.onChange?.();
      },
    });
    p.ctlRef.current = ctl;
    bump();
    pref.current.onChange?.();
    return () => {
      ctl.destroy();
      if (p.ctlRef.current === ctl) p.ctlRef.current = null;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [p.title]);
  const ctl = p.ctlRef.current;

  useEffect(() => {
    if (ctl && p.model) ctl.setModel(p.model);
  }, [ctl, p.model]);
  useEffect(() => {
    if (ctl) ctl.setLive(p.agents, p.tzones);
  }, [ctl, p.agents, p.tzones]);
  useEffect(() => {
    if (ctl) ctl.setActive(p.active);
  }, [ctl, p.active]);
  useEffect(() => {
    if (ctl) ctl.setReducedMotion(p.reducedMotion);
  }, [ctl, p.reducedMotion]);
  useEffect(() => {
    HUD.set(p.title, { activity, risk });
  }, [p.title, activity, risk]);
  useEffect(() => {
    if (ctl) ctl.setRiskAll(risk);
  }, [ctl, risk, p.model]);

  // --- HUD rects + insets: labels and badges keep off the panels, fits avoid them
  const measure = () => {
    const st = stageRef.current,
      c = p.ctlRef.current;
    if (!st || !c) return;
    const r0 = st.getBoundingClientRect();
    const sz = r0.width < 480 || r0.height < 330 ? "tiny" : r0.width < 760 || r0.height < 560 ? "narrow" : "wide";
    if (sz !== size) setSize(sz);
    const isNarrow = sz !== "wide";
    const rects: Array<[number, number, number, number]> = [];
    let left = 8,
      top = 8,
      bot = 8,
      card = 0;
    for (const el of Array.from(st.querySelectorAll<HTMLElement>("[data-hud]"))) {
      const r = el.getBoundingClientRect();
      if (r.width < 2 || r.height < 2) continue;
      const x0 = r.left - r0.left,
        y0 = r.top - r0.top,
        x1 = r.right - r0.left,
        y1 = r.bottom - r0.top;
      rects.push([x0 - 4, y0 - 4, x1 + 4, y1 + 4]);
      const kind = el.dataset.hud;
      // the bird index is a LEFT column at every size (a slim rail in small panes): it insets the left edge
      if (kind === "left") left = Math.max(left, x1 + 8);
      // (the paint-tool hint sits at the bottom and insets nothing: arming a tool never moves what you aim at)
      if (kind === "info" && !isNarrow) card = Math.max(card, r.width + 16);
      if (kind === "info" && isNarrow) bot = Math.max(bot, r0.height - y0 + 6);
      if (kind === "activity" && isNarrow) bot = Math.max(bot, r0.height - y0 + 6);
    }
    const insets: Insets = { top, bot, left, right: 8 };
    c.cardInset = card;
    c.setHud(rects, insets);
  };
  useLayoutEffect(measure);
  useEffect(() => {
    const st = stageRef.current;
    if (!st || typeof ResizeObserver === "undefined") return;
    const ro = new ResizeObserver(() => measure());
    ro.observe(st);
    for (const el of Array.from(st.querySelectorAll<HTMLElement>("[data-hud]"))) ro.observe(el);
    return () => ro.disconnect();
  });
  const showMini = !narrow && !!ctl && ctl.zoomedIn;
  useEffect(() => {
    if (ctl) ctl.setMini(miniRef.current);
  }, [ctl, p.model, showMini]);

  // --- the open file's outline (GET /code-map/file) --------------------------
  const info = ctl ? ctl.info : null;
  const infoPath = info && info.kind === "file" ? info.f.path : "";
  useEffect(() => {
    if (!infoPath || !p.active) return;
    const hit = FILES_SEEN.get(p.title + "\u0000" + infoPath);
    setFv(hit || null);
    setFvErr("");
    let dead = false;
    p.fileView(infoPath)
      .then((d) => {
        if (dead) return;
        FILES_SEEN.set(p.title + "\u0000" + infoPath, d);
        if (FILES_SEEN.size > 60) FILES_SEEN.delete(FILES_SEEN.keys().next().value!);
        setFv(d);
      })
      .catch((x) => !dead && setFvErr(String((x as Error)?.message || x)));
    return () => {
      dead = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [infoPath, p.title, p.active]);

  const M = p.model;
  const v = ctl ? ctl.v : null;
  const rows = useMemo(() => (M ? activityRows(M, p.agents, p.feed, p.zones, p.title) : []), [M, p.agents, p.feed, p.zones, p.title]);

  const onCanvasKey = (e: React.KeyboardEvent) => {
    if (ctl && ctl.key(e.nativeEvent)) e.preventDefault();
  };

  const tip = ctl && ctl.tip && M && v ? ctl.tip : null;
  const tool = v ? v.tool : "explore";

  let card: ReactNode = null;
  if (M && ctl && info) {
    if (info.kind === "file") {
      const f = info.f;
      card = (
        <FileCard
          M={M}
          f={f}
          agents={p.agents}
          zones={p.tzones}
          fv={fv && fv.path === f.path ? fv : null}
          fvErr={fvErr}
          pinned={!!v && v.pinBlast === f.id}
          changed={p.changed.has(f.path)}
          outside={p.classify(f.path) === "outside"}
          allowState={p.reqBusy[f.path] || ""}
          onClose={() => ctl.closeInfo()}
          onBlast={() => ctl.togglePinBlast(f)}
          onCentre={() => f.leaf && ctl.flyToLeaf(f.leaf, Math.max(v ? v.cam.z : 3, 3))}
          onFolder={() => {
            if (f.node) {
              ctl.selectNode(f.node);
              ctl.flyToNode(f.node, 0.8);
            }
          }}
          onDiff={p.onOpenDiff}
          onKeep={() => p.onPaint("keep", { file: f })}
          onOnly={() => p.onPaint("only", { file: f })}
          onAllow={() => p.onAllow(f.path)}
          onFile={(path) => ctl.revealPath(path)}
        />
      );
    } else if (info.kind === "node") {
      const n = info.n;
      card = (
        <NodeCard
          M={M}
          n={n}
          agents={p.agents}
          zones={p.tzones}
          serverZones={p.zones}
          folded={!!v && v.folded.has(n)}
          onClose={() => ctl.closeInfo()}
          onZoom={() => (n.depth === 0 && n.kind !== "pile" ? ctl.wholeTree() : ctl.flyToNode(n, 0.8))}
          onFold={() => ctl.toggleFold(n)}
          onKeep={() => p.onPaint("keep", { node: n })}
          onOnly={() => p.onPaint("only", { node: n })}
          onRemove={p.onRemoveZone}
          onKid={(k) => {
            ctl.selectNode(k);
            ctl.flyToNode(k, 0.8);
          }}
          onAgent={(A) => ctl.toggleFollow(A.ag.key)}
        />
      );
    } else {
      const bd = info.bd;
      card = (
        <BlastCard
          M={M}
          bd={bd}
          onClose={() => ctl.closeInfo()}
          onFrame={() => ctl.frameIds([...bd.ids])}
          onFolder={() => {
            ctl.selectNode(bd.node);
            ctl.flyToNode(bd.node, 0.8);
          }}
          onFile={(f) => f.leaf && ctl.selectLeaf(f.leaf, true)}
        />
      );
    }
  }

  const growing = p.growing !== null && !M;
  const regrowing = p.growing !== null && !!M;
  return (
    <div
      className={"ct-stage" + (narrow ? " narrow" : "") + (size === "tiny" ? " tiny" : "") + (showMini ? " mini-on" : "")}
      ref={stageRef}
      onKeyDown={(e) => {
        // Esc anywhere on the tree (a card you just clicked has focus) stops following
        if (e.key === "Escape" && !e.defaultPrevented && ctl && ctl.v && ctl.v.follow != null) {
          ctl.stopFollow(true, true);
          e.preventDefault();
        }
      }}
    >
      <canvas
        ref={cvRef}
        className="ct-canvas"
        tabIndex={0}
        role="application"
        aria-label={
          M
            ? `Code tree of ${M.name}: ${M.files.length} files. Scroll to zoom, drag or arrow keys to move, click a folder, leaf or bird for details. The Session panel lists the same information.`
            : "Code tree"
        }
        aria-describedby={"ct-sum-" + cssId(p.title)}
        onKeyDown={onCanvasKey}
      />
      <p className="cm-sr" id={"ct-sum-" + cssId(p.title)}>
        {M ? summary(M, p.agents, p.zones) : ""}
      </p>
      {growing && (
        <div className="ct-growing" role="status" aria-live="polite">
          <span className="ct-sprout" aria-hidden="true" />
          <b>Growing the tree…</b>
          <span className="ct-muted">
            {p.files ? `${p.files.toLocaleString()} files` : "reading the worktree"}
            {p.growing ? ` · ${Math.round(p.growing * 100)}%` : ""}
          </span>
          <span className="ct-bar" aria-hidden="true">
            <span style={{ width: Math.round((p.growing || 0) * 100) + "%" }} />
          </span>
        </div>
      )}
      {p.growErr && !M && (
        <div className="cm-note cm-note-err" role="alert">
          <b>Couldn't grow the tree.</b>
          <span className="muted">{p.growErr}</span>
        </div>
      )}
      {regrowing && (
        <div className="ct-regrow" role="status" title="The worktree changed: the tree is being re-laid out">
          growing…
        </div>
      )}
      {M && (
        <>
          {tool !== "explore" && (
            <div className={"ct-modehint " + tool} data-hud="hint" role="status" aria-live="polite">
              {tool === "keep" ? (
                <>
                  <b>⛔ Keep out</b> — click a folder name, branch or leaf to fence it off · <kbd>Shift</kbd>+click for several · <kbd>Esc</kbd> cancels
                </>
              ) : (
                <>
                  <b>✓ Only here</b> — click the one folder agents may edit in; the rest dims · <kbd>Esc</kbd> cancels
                </>
              )}
            </div>
          )}
          <BirdIndex
            M={M}
            agents={p.agents}
            tzones={p.tzones}
            zones={p.zones}
            zoneBusy={p.zoneBusy}
            size={size}
            followKey={v ? v.follow : null}
            primaryTask={p.primaryTask}
            primaryExtra={p.primaryExtra}
            affects={(A) => affectsOf(M, A, blastCache.current)}
            riskOpen={risk}
            onRisk={setRisk}
            riskFile={v ? (v.riskFile ?? null) : null}
            onRiskFile={(f) => ctl && ctl.focusRisk(f.id)}
            onFollow={(k) => ctl && ctl.toggleFollow(k)}
            onNest={(A) => {
              if (ctl && A.nest) {
                ctl.selectNode(A.nest);
                ctl.flyToNode(A.nest, 0.8);
              }
            }}
            onFile={(f) => ctl && f.leaf && ctl.selectLeaf(f.leaf, true)}
            onFolder={(n) => {
              if (ctl) {
                ctl.selectNode(n);
                ctl.flyToNode(n, 0.8);
              }
            }}
            onPaint={(tool, n) => p.onPaint(tool, { node: n })}
            onRemoveZone={p.onRemoveZone}
            onGoZone={(z) => {
              const t = p.tzones.find((x) => x.z.id === z.id);
              if (!ctl || !t) return;
              if (t.node) {
                if (t.node.depth >= 1) ctl.selectNode(t.node);
                ctl.flyToNode(t.node, 0.8);
              } else if (t.file != null && M.files[t.file].leaf) ctl.selectLeaf(M.files[t.file].leaf!, true);
            }}
          />
          {/* the minimap waits until you zoom in: at the whole-tree view it would only repeat the canvas */}
          {showMini && (
            <div className="ct-card ct-mini" data-hud="mini">
              <canvas
                ref={miniRef}
                aria-label="Minimap: click or drag to move the view"
                onPointerDown={(e) => {
                  (e.target as HTMLCanvasElement).setPointerCapture(e.pointerId);
                  ctl?.pushHist();
                  miniJump(ctl, e);
                }}
                onPointerMove={(e) => {
                  if (e.buttons) miniJump(ctl, e);
                }}
              />
            </div>
          )}
          {size !== "tiny" && (
          <div className="ct-activity-wrap" data-hud="activity">
            <ActivityCard
              rows={rows}
              skew={p.skew}
              open={narrow ? actNarrow : activity && !card}
              ticker={narrow}
              onToggle={() => (narrow ? setActNarrow(!actNarrow) : setActivity(!activity))}
              onRow={(r) => ctl && ctl.revealPath(r.path)}
              zoneOf={(r) => p.zones.find((z) => "z" + z.id === r.key) || null}
              onZone={(z) => {
                const t = p.tzones.find((x) => x.z.id === z.id);
                if (ctl && t && t.node) ctl.flyToNode(t.node, 0.8);
              }}
            />
          </div>
          )}
          {card && (
            <div className="ct-card ct-info" data-hud="info" role="region" aria-label="Details">
              {card}
            </div>
          )}
          {tip && v && (
            <div className="ct-tip" role="tooltip" style={tipPos(tip.x, tip.y, v.W, v.H)}>
              <TipBody M={M} hit={tip.hit} agents={p.agents} zones={p.tzones} tool={tool} folded={!!(tip.hit.clump && v.folded.has(tip.hit.clump))} />
            </div>
          )}
        </>
      )}
      {p.toast}
    </div>
  );
}

function miniJump(ctl: TreeCtl | null, e: React.PointerEvent<HTMLCanvasElement>) {
  if (!ctl || !ctl.v || !ctl.M) return;
  const c = e.currentTarget;
  const r = c.getBoundingClientRect();
  const M = ctl.M,
    B = M.bounds;
  const W = r.width,
    H = r.height;
  const z = Math.min(W / (B.x1 - B.x0), H / (B.y1 - B.y0)) * 0.92;
  const ox = W / 2 - ((B.x0 + B.x1) / 2) * z,
    oy = H / 2 - ((B.y0 + B.y1) / 2) * z;
  const wx = (e.clientX - r.left - ox) / z,
    wy = (e.clientY - r.top - oy) / z;
  ctl.stopFollow();
  ctl.camT = { x: wx, y: wy, z: ctl.camT.z };
  if (ctl.reducedMotion) ctl.v.cam = { ...ctl.camT };
  ctl.fly = null;
  ctl.dirty = true;
  ctl.kick();
}

function tipPos(x: number, y: number, W: number, H: number): React.CSSProperties {
  const left = Math.min(x + 14, W - 300);
  const top = y + 16 > H - 120 ? Math.max(4, y - 120) : y + 16;
  return { left: Math.max(4, left), top };
}

const cssId = (s: string) => s.replace(/[^A-Za-z0-9_-]/g, "_");

/** One sentence for screen readers: what the picture shows. */
function summary(M: Model, agents: AgentState[], zones: RedZone[]): string {
  const bits = [`${M.files.length} files: ${M.crown.nFiles} code files on ${M.crown.kids.length} top-level branches, ${M.roots.nFiles} tests as roots, ${M.piles.reduce((a, q) => a + q.nFiles, 0)} docs and config files on the ground.`];
  for (const A of agents) bits.push(`${A.ag.name}: ${A.status}${A.file ? " " + A.file.path : ""}, edited ${A.edits.size}, read ${A.reads.size}.`);
  if (zones.length) bits.push(`${zones.length} rule${zones.length === 1 ? "" : "s"}: ` + zones.map((z) => (z.kind === "green" ? "only here " : "keep out ") + (z.name || z.pattern)).join("; ") + ".");
  return bits.join(" ");
}
