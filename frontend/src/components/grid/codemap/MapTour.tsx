/** The Map tab's tour: short coach marks over the real map, each ringing the
 * part it explains, in the order a newcomer needs them — the tree (trunk,
 * branch, leaf), the roots (tests), the scaffold (docs, CI, config), the
 * birds and what they changed, the rule tools — ending on this session's next
 * step (or, with none, on finding your way around). Six cards at most. It
 * opens by itself the first time a Map is shown on any of your devices (the
 * synced `map_tour` pref) and is replayable from the toolbar's "? Guide". The
 * coach-mark itself is the shared one (onboarding/CoachTour.tsx), contained
 * in the Map's root; the canopy, roots and scaffold it rings are invisible
 * boxes CodeTree keeps over the picture (`.ct-part-*`).
 *
 * The steps are built from what is on screen when it opens (mapTourSteps):
 * with nothing changed the birds step is reworded, a repo with no tests or no
 * scaffold skips that step, and a toolbar step whose target is not visible (a
 * narrow toolbar) is skipped rather than pointing at nothing; a step about a
 * part of the tree, or the agents column folded to a rail, keeps its card
 * and docks it in a corner instead.
 *
 * Non-modal on purpose: the map stays live underneath (the birds keep
 * flying), and a click anywhere outside the card is allowed. Esc or Skip ends
 * it and counts as seen; so does Done. */

import { useState, type RefObject } from "react";
import { CoachTour, findTarget, placeCard, type CoachStep } from "../../onboarding/CoachTour";
import { nextStepText, type NextStep } from "../../../lib/codemap";
import { mapScene, placeMapCard } from "./tourPlace";

/** A Map step: its `target` is a CSS selector inside the Map's root; "" =
 * the tree as a whole. */
export type TourStep = CoachStep;

const TREE: TourStep = {
  target: ".ct-part-canopy",
  title: "Your code, drawn as a tree",
  body: (
    <>
      The <b>trunk</b> is the repo, each <b>branch</b> a folder and each <b>leaf</b> a file. Thicker branches hold
      more code. Scroll to zoom in: the leaves open up and each one is named at its tip.
    </>
  ),
};

const ROOTS: TourStep = {
  target: ".ct-part-roots",
  title: "Roots are the tests",
  body: (
    <>
      Each <b>root tip</b> is one test file. A test grows under the code it tests, so its root is named for it,
      like <b>tests for backend/web</b>.
    </>
  ),
};

const SCAFFOLD: TourStep = {
  target: ".ct-part-scaffold",
  title: "Scaffolding holds docs and config",
  body: (
    <>
      Docs, CI and config stand beside the trunk as <b>scaffolding</b>. Each <b>lift</b> is one folder, named on
      its top ledger (<b>docs · 21 files</b>). Each <b>plank</b> is one file; a longer plank is a bigger file.
    </>
  ),
};

const BIRDS: TourStep = {
  target: '[data-hud="left"]',
  title: "Birds are agents, bright leaves are changes",
  body: (
    <>
      Each agent is a <b>bird</b>; it <b>nests</b> where it mostly works. A <b>glowing leaf</b> is a file it
      changed, and the <b>lit wood</b> leads you to it. Hover one: the files that import it turn <b>gold</b> —
      they could break. Click a row here to follow its bird.
    </>
  ),
};

const BIRDS_CALM: TourStep = {
  target: '[data-hud="left"]',
  title: "Birds are agents",
  body: (
    <>
      Each agent is a <b>bird</b>; it <b>nests</b> where it mostly works. Every leaf is grey for now. When an agent
      changes a file, its leaf <b>glows</b> and the <b>lit wood</b> leads you to it. Click a row here to follow its
      bird.
    </>
  ),
};

const RULES: TourStep = {
  target: ".ct-rules",
  title: "Rules: keep agents in or out",
  body: (
    <>
      <b>⛔ Keep out</b>: agents may read a folder but never edit it. <b>✓ Only here</b>: they may edit only
      inside it. Pick one, then click a folder on the tree.
    </>
  ),
};

const FIND: TourStep = {
  target: ".cm-search",
  title: "Getting around",
  body: (
    <>
      Press <kbd>/</kbd> to find a file or function. Drag to pan, double-click a folder to zoom into it, and{" "}
      <b>⌂ Whole tree</b> brings you back. <b>?</b> beside Agents is the legend.
    </>
  ),
};

/** Every step the tour can show, in order (the last, the next step, is built per session; with none, it is
 * Getting around). */
export const MAP_TOUR: TourStep[] = [TREE, ROOTS, SCAFFOLD, BIRDS, RULES, FIND];

/** The most cards the tour shows. */
export const MAP_TOUR_MAX = 6;

export interface MapTourCtx {
  /** changed files on the tree (any session) */
  changed: number;
  /** this session's next step (the bird index's next-step line) */
  next: NextStep;
  /** whether the tree has roots (tests) and a scaffold (docs, CI, config) to show (default: both) */
  roots?: boolean;
  scaffold?: boolean;
  /** whether a selector's target is on screen now (default: everything is) */
  visible?: (selector: string) => boolean;
}

/** The tour for what is on screen: the tree, its roots and scaffold when it has them, the birds (reworded when
 * nothing changed), the rules when the toolbar shows them, and a last card on the concrete next step — or
 * Getting around when there is none. A card about a part of the tree, or the agents column folded to a rail,
 * is kept and docked; a toolbar step that is not visible is skipped. At most MAP_TOUR_MAX cards. (Pure.) */
export function mapTourSteps(ctx: MapTourCtx): TourStep[] {
  const shows = (s: TourStep) => !s.target || !ctx.visible || ctx.visible(s.target);
  const docked = (s: TourStep) => (shows(s) ? s : { ...s, target: "" });
  const out: TourStep[] = [TREE];
  if (ctx.roots !== false) out.push(ROOTS);
  if (ctx.scaffold !== false) out.push(SCAFFOLD);
  out.push(docked(ctx.changed > 0 ? BIRDS : BIRDS_CALM));
  if (shows(RULES)) out.push(RULES);
  if (ctx.next.kind !== "none") out.push(docked({ target: ".ct-next", title: "Your next step", body: <>{nextStepText(ctx.next)}</> }));
  else if (shows(FIND)) out.push(FIND);
  return out.slice(0, MAP_TOUR_MAX);
}

/** Where the card goes (the generic rule, re-exported for the Map's tests). */
export { placeCard };

export function MapTour({
  rootRef,
  onClose,
  ctx,
  steps,
}: {
  rootRef: RefObject<HTMLElement | null>;
  onClose: () => void;
  ctx?: MapTourCtx;
  steps?: TourStep[];
}) {
  // built once, when it opens: a step list that shifted under the reader mid-tour would be worse than a stale one
  const [built] = useState(
    () =>
      steps ||
      mapTourSteps({
        changed: 0,
        next: { kind: "none" },
        ...ctx,
        visible: (sel) => !rootRef.current || !!findTarget(rootRef.current, sel),
      })
  );
  // The generic coach tour (onboarding/CoachTour), contained in the Map's
  // root and dressed in the Map's own .mt-* / .cm-btn styles.
  return (
    <CoachTour
      steps={built}
      onClose={onClose}
      rootRef={rootRef}
      prefix="mt"
      buttonClass="cm-btn"
      labelId="mt-title"
      place={(t, W, H, cardH) => placeMapCard(t, W, H, cardH, mapScene(rootRef.current))}
    />
  );
}
