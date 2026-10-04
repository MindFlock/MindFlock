/** Code tree — the live state the renderer draws on top of the model: agents
 * (birds), zones (rules), selection, camera and per-frame hit rects. */

import type { RedZone } from "../../api/types";
import type { Leaf, Model, TFile, TNode } from "./model";
import type { Palette } from "./palette";

export interface AgentDef {
  id: number;
  /** session title (stable identity) */
  key: string;
  name: string;
  color: string;
  glyph: string;
  /** this pane's own session */
  primary: boolean;
  /** a subagent: its parent bird's key, its 1-based index and its short
   * name for canvas tags and badges ("explore#1") */
  parent?: string;
  sub?: number;
  short?: string;
}

export type EvType = "read" | "edit" | "create" | "nest" | "plan";

export interface BirdEv {
  /** observed at (seconds, view clock: performance.now()/1000) */
  t: number;
  type: EvType;
  f: TFile | null;
  /** a refused edit: the zone it hit */
  blocked: TZone | null;
  nestNode: TNode | null;
}

export interface Blocked {
  t: number;
  f: TFile | null;
  z: TZone | null;
  kind: "keep" | "only";
  path: string;
}

export type AgentStatus = "waiting" | "planning" | "reading" | "editing" | "creating" | "blocked" | "thinking" | "done";

export interface AgentState {
  ag: AgentDef;
  /** file id -> server ts */
  reads: Map<number, number>;
  edits: Map<number, number>;
  /** planned existing files (buds) */
  plan: Set<number>;
  /** planned NEW files: a ghost bud on the folder that will hold them */
  planNew: Array<{ path: string; node: TNode }>;
  created: Set<number>;
  blocked: Blocked[];
  nest: TNode | null;
  nestSince: number;
  /** the last two events (a bird flies from the first's spot to the second's) */
  evs: BirdEv[];
  cur: BirdEv | null;
  lastEdit: { f: TFile; t: number } | null;
  status: AgentStatus;
  file: TFile | null;
  done: boolean;
  /** what the session itself says (activity) */
  activity: string;
  task: string;
  /** a subagent's bird: what the feed says about it */
  subInfo?: import("./subagents").SubInfo | null;
}

/** A rule as the tree draws it: on a folder (node) or a single file. */
export interface TZone {
  type: "keep" | "only";
  node: TNode | null;
  file: number | null;
  /** the server zone it stands for */
  z: RedZone;
  waived: boolean;
  label: string;
}

export interface Hit {
  leaf?: Leaf;
  label?: boolean;
  node?: TNode;
  viaLeaf?: Leaf;
  branch?: import("./model").Branch;
  trunk?: boolean;
  clump?: TNode;
  open?: boolean;
  pile?: TNode;
  tag?: TZone;
  bird?: AgentState;
  pointer?: boolean;
  nest?: AgentState;
  badge?: Badge;
  bud?: { A: AgentState; path: string };
}

export interface Badge {
  x: number;
  y: number;
  text: string;
  short: string;
  alpha: number;
  n: number;
  ag: AgentDef | null;
  pin: boolean;
  node: TNode;
  files: Set<number>;
  ids: Set<number>;
  what: string;
}

/** One folder of the could-break layer: how many of its files import a changed file (or the focused one). */
export interface RiskGroup {
  n: TNode;
  x: number;
  y: number;
  r: number;
  ids: Set<number>;
  /** the changed files they import */
  files: Set<number>;
  /** the smaller folders of a limb, counted together ("backend · other") */
  rest?: boolean;
}

export interface BadgeSet {
  n: TNode;
  ids: Set<number>;
  files: Set<number>;
  alpha: number;
  ag: AgentDef | null;
  pin: boolean;
  hover: boolean;
  who: string;
}

export interface Cam {
  x: number;
  y: number;
  z: number;
}

export type Tool = "explore" | "keep" | "only";

/** The view: everything render() reads and writes. One per mounted tree. */
export interface TreeView {
  g: CanvasRenderingContext2D;
  M: Model;
  P: Palette;
  agents: AgentState[];
  W: number;
  H: number;
  dpr: number;
  cam: Cam;
  zones: TZone[];
  folded: Set<TNode>;
  tool: Tool;
  /** view clock, seconds */
  t: number;
  /** animations run (false under reduced motion) */
  playing: boolean;
  reducedMotion: boolean;
  hover: Hit | null;
  sel: { leaf?: Leaf; node?: TNode; fid?: number } | null;
  infoNode: TNode | null;
  infoFile: TFile | null;
  pinBlast: number | null;
  hoverBlast: number | null;
  pulse: { x: number; y: number; r: number; t0: number } | null;
  /** the followed bird's key */
  follow: string | null;
  markLeaf: Leaf | null;
  blastCache: Map<number, { h1: Set<number> }>;
  hudRects: Array<[number, number, number, number]>;
  free: { x0: number; y0: number; x1: number; y1: number };
  fitZv: number;
  noCache?: boolean;
  /** "⚠ Could break" is on: the folders holding files that import a changed file, as quiet gold territories */
  riskAll?: boolean;
  /** one changed file's dependents on the map (MOST AT RISK / a ranked row was clicked) */
  riskFile?: number | null;
  // ---- written by render ----
  sc?: { cv: HTMLCanvasElement; g: CanvasRenderingContext2D | null; cam: Cam | null; key: string; n: number; w?: number; h?: number; dpr?: number; fast?: boolean; lastMs?: number };
  prevCam?: Cam;
  lastMove?: number;
  hasOnly?: boolean;
  litAnc?: Set<TNode>;
  keepFiles?: Set<number>;
  onlyFiles?: Set<number>;
  termsInView?: number;
  leavesShown?: boolean;
  visBranches?: import("./model").Branch[];
  badges?: Badge[];
  badgeSets?: Map<string, BadgeSet>;
  /** the could-break territories this frame (world), counted per folder */
  riskGroups?: RiskGroup[];
  toS?: (x: number, y: number) => [number, number];
  fol?: {
    hit(x0: number, y0: number, x1: number, y1: number, own: TNode | null, subtree: boolean): boolean;
    sideOk(x0: number, y0: number, x1: number, y1: number, n: TNode | null, range?: number): boolean;
    belongs(x0: number, y0: number, x1: number, y1: number, n: TNode | null, anchors?: Array<[number, number, number]>, range?: number, clear?: number): boolean;
    near(x0: number, y0: number, x1: number, y1: number, n: TNode | null, anchors?: Array<[number, number, number]>, range?: number): { own: number; loud: number; ownC: number; calmC: number; by: TNode | null };
    addNest(x0: number, y0: number, x1: number, y1: number, n: TNode): void;
    addTag(x0: number, y0: number, x1: number, y1: number, n: TNode): void;
    addTerr(n: TNode, polys: number[][]): void;
  } | null;
  /** each zone territory on screen this frame: its folder and its convex screen polygons */
  terrPolys?: Array<{ n: TNode; keep: boolean; polys: number[][] }>;
  crumbAnc?: Set<TNode> | null;
  crumbNode?: TNode | null;
  nestLabelled?: Set<TNode>;
  birdScreen?: Array<{ A: AgentState; sx: number; sy: number; pose: unknown }>;
  /** each bird's world position this frame, by key */
  birdWorld?: Record<string, [number, number]>;
  anim?: boolean;
  labelBudget?: number;
  /** LIT PATHS: folders on a path to a change, with their counts */
  litInfo?: Map<TNode, import("./draw").LitInfo>;
  labelHits?: Array<{ r: number[]; n?: TNode; leaf?: Leaf; crumb?: boolean }>;
  tagHits?: Array<{ r: number[]; z: TZone }>;
  birdHits?: Array<{ r: number[]; A: AgentState; pointer?: boolean }>;
  nestHits?: Array<{ r: number[]; A: AgentState }>;
  badgeHits?: Array<{ r: number[]; bd: Badge }>;
  budHits?: Array<{ r: number[]; A: AgentState; path: string }>;
  /** strings the canvas shows (for the text alternative / tests) */
  emptyHint?: string;
}
