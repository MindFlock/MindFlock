/** Where "show me the behaviors" goes: the Sky (docs/behaviors.md §4).
 *
 * SEAM (pre-committed by the integrator; frozen). Every former Verify entry
 * (Alt+V, palette, session menu, verdict chips, bell rows, hashes) calls
 * `openBehaviors`. It switches to the Sky and publishes a focus request; the
 * Sky (SkyView) flies the camera to the session's frame or the Behaviors
 * board, and the session's chain (SkyChain) opens the Behaviors card in the
 * tile's place. Each request carries a fresh `n` so the same target twice
 * re-flies. */

import { create } from "zustand";
import type { TestPlan } from "../api/types";
import { useUi } from "../state/store";

export interface BehaviorsTarget {
  /** A plan id (`TestPlan.id`, usually the session's title). */
  planId?: string;
  /** A session title. */
  title?: string;
  /** A branch (old `#verify/<branch>` links). */
  branch?: string;
  /** Alt+V: the next behaviors that need you, else failed, else the board. */
  next?: boolean;
  /** Just the Behaviors board. */
  board?: boolean;
}

export interface BehaviorsRequest extends BehaviorsTarget {
  n: number;
}

interface FocusState {
  /** The latest request, or null once taken. */
  request: BehaviorsRequest | null;
  /** The plan whose card is open in the Sky ("" = none). */
  openPlan: string;
  /** The board forced visible by a request (cleared when the board is closed). */
  boardRequested: boolean;
  setOpenPlan(id: string): void;
  take(): void;
  setBoardRequested(v: boolean): void;
}

let _n = 0;

export const useBehaviorsFocus = create<FocusState>((set) => ({
  request: null,
  openPlan: "",
  boardRequested: false,
  setOpenPlan: (id) => set({ openPlan: id }),
  take: () => set({ request: null }),
  setBoardRequested: (v) => set({ boardRequested: v }),
}));

/** Show behaviors in the Sky. From the grid this switches to the Sky first. */
export function openBehaviors(target: BehaviorsTarget = { board: true }): void {
  const ui = useUi.getState();
  ui.showSky();
  useBehaviorsFocus.setState({ request: { ...target, n: ++_n }, ...(target.board ? { boardRequested: true } : {}) });
}

/** Resolve a target against the plan list (planId, then title, then branch). */
export function findPlan(plans: readonly TestPlan[] | null | undefined, t: BehaviorsTarget): TestPlan | null {
  const list = plans || [];
  if (t.planId) {
    const p = list.find((x) => x.id === t.planId);
    if (p) return p;
  }
  if (t.title) {
    const p = list.find((x) => x.id === t.title);
    if (p) return p;
  }
  if (t.branch) return list.find((x) => x.branch === t.branch) || null;
  return null;
}

/** `#behaviors` (the board) or `#behaviors/<plan id>`. */
export function behaviorsHash(planId = ""): string {
  return "#behaviors" + (planId ? "/" + encodeURIComponent(planId) : "");
}

/** The target a `#behaviors…` / old `#verify…` hash names, or null. */
export function parseBehaviorsHash(hash: string): BehaviorsTarget | null {
  const h = hash.replace(/^#/, "");
  const [head, ...rest] = h.split("/");
  const tail = rest.length ? decodeURIComponent(rest.join("/")) : "";
  if (head === "behaviors") return tail ? { planId: tail } : { board: true };
  if (head === "verify") return tail ? { branch: tail } : { next: true };
  return null;
}

/** Where an old `openDialogFor("verify", target)` lands. The Verify dialog
 * is retired (Verify is a stop on the route): its target was a branch (the
 * verdict chip, the session menu) or a plan id (the bell), so it is tried as
 * each — `findPlan` takes the first that matches; none = the next that needs
 * you. */
export function verifyDialogTarget(target: string | null | undefined): BehaviorsTarget {
  const t = String(target || "").trim();
  return t ? { planId: t, title: t, branch: t } : { next: true };
}

/** Catch every caller that still opens the retired Verify dialog (the replay
 * of the Verify tour, Customize's bar link, an extension) and send it to the
 * Sky instead. Returns the unhook. */
export function installVerifyRedirect(): () => void {
  const land = () => {
    const s = useUi.getState();
    if (s.openDialog !== "verify") return;
    const target = s.dialogTarget;
    s.closeDialog();
    openBehaviors(verifyDialogTarget(target));
  };
  land();
  return useUi.subscribe((s, prev) => {
    if (s.openDialog === "verify" && prev.openDialog !== "verify") land();
  });
}
