/** The Map tab's tour: short coach marks over the real map, each pointing at
 * the part it explains (the tree, the agents column, ⚠ Could break, the rule
 * tools, Plan & lists, find), ending on this session's next step. It opens by
 * itself the first time a Map is shown on any of your devices (the synced
 * `map_tour` pref) and is replayable from the toolbar's "? Guide". The
 * coach-mark itself is the shared one (onboarding/CoachTour.tsx), contained
 * in the Map's root.
 *
 * The steps are built from what is on screen when it opens (mapTourSteps):
 * with nothing changed the changes step is reworded and ⚠ Could break is
 * left out (its button only exists with changes), and a step whose target
 * is not visible (the agents column folded to a rail, a narrow toolbar) is
 * skipped rather than pointing at nothing.
 *
 * Non-modal on purpose: the map stays live underneath (the birds keep
 * flying), and a click anywhere outside the card is allowed. Esc or Skip ends
 * it and counts as seen; so does Done. */

import { useState, type RefObject } from "react";
import { CoachTour, findTarget, placeCard, type CoachStep } from "../../onboarding/CoachTour";
import { nextStepText, type NextStep } from "../../../lib/codemap";

/** A Map step: its `target` is a CSS selector inside the Map's root; "" =
 * the tree as a whole. */
export type TourStep = CoachStep;

const TREE: TourStep = {
  target: "",
  title: "Your code, drawn as a tree",
  body: (
    <>
      The <b>trunk</b> is the repo, each <b>branch</b> a folder and each <b>leaf</b> a file. <b>Roots</b>{" "}
      underground are the tests, and the piles on the ground are docs and config. Thicker branches hold
      more code.
    </>
  ),
};

const CHANGED: TourStep = {
  target: "",
  title: "Bright leaves are what changed",
  body: (
    <>
      Grey is untouched. A <b>glowing leaf</b> is a file an agent changed, and the <b>lit branches</b> lead
      you to it. Labels like <b>web/core ●4</b> count the changes in that folder. Click a leaf or folder for
      its card.
    </>
  ),
};

const NOTHING_CHANGED: TourStep = {
  target: "",
  title: "Nothing has changed yet",
  body: (
    <>
      Every leaf is grey for now. When an agent changes a file its leaf <b>glows</b> and the <b>lit
      branches</b> lead you to it. Click a leaf or folder for its card.
    </>
  ),
};

const AGENTS: TourStep = {
  target: '[data-hud="left"]',
  title: "Each agent is a bird",
  body: (
    <>
      This column lists the agents working on this repo: what each is doing now and what it changed. A bird
      <b> nests</b> where it mostly works. Click a row to follow its bird around the tree.
    </>
  ),
};

const RISK: TourStep = {
  target: ".ct-bix-risk-tog",
  title: "What could break",
  body: (
    <>
      Hover a bright leaf and the files that <b>import</b> it light up <b>gold</b>. <b>⚠ Could break</b>{" "}
      ranks the changed files by how much depends on them, so you know what to review first.
    </>
  ),
};

const RULES: TourStep = {
  target: ".ct-rules",
  title: "Rules: keep agents in or out",
  body: (
    <>
      <b>⛔ Keep out</b>: agents may read a folder but never edit it. <b>✓ Only here</b>: they may edit only
      inside it. Pick one, then click a folder on the tree. Every changed folder in the column has the same
      two buttons.
    </>
  ),
};

const PLAN: TourStep = {
  target: ".ct-plan-btn",
  title: "Plan first",
  body: (
    <>
      <b>Plan &amp; lists</b> holds <b>Ask for plan</b>: the agent lists every file it means to touch, as buds on
      the tree, before it touches them. Review it, then <b>Go</b> — or <b>Go — only the planned files</b>.
    </>
  ),
};

const FIND: TourStep = {
  target: ".cm-search",
  title: "Getting around",
  body: (
    <>
      Press <kbd>/</kbd> to find a file or function. Scroll to zoom, drag to pan, double-click a folder to
      zoom into it, and <b>⌂ Whole tree</b> brings you back. <b>?</b> beside Agents is the legend.
    </>
  ),
};

/** Every step the tour can show, in order (the last, the next step, is built per session). */
export const MAP_TOUR: TourStep[] = [TREE, CHANGED, AGENTS, RISK, RULES, PLAN, FIND];

export interface MapTourCtx {
  /** changed files on the tree (any session) */
  changed: number;
  /** this session's next step (the bird index's next-step line) */
  next: NextStep;
  /** whether the Plan & lists control offers a plan (the CLI can declare one) */
  planSupported?: boolean;
  /** whether a selector's target is on screen now (default: everything is) */
  visible?: (selector: string) => boolean;
}

/** The tour for what is on screen: the changes step reworded when nothing
 * changed, ⚠ Could break only with changes, a step whose target is not
 * visible skipped, and a last card on the concrete next step. (Pure.) */
export function mapTourSteps(ctx: MapTourCtx): TourStep[] {
  const shows = (s: TourStep) => !s.target || !ctx.visible || ctx.visible(s.target);
  const out: TourStep[] = [TREE, ctx.changed > 0 ? CHANGED : NOTHING_CHANGED];
  for (const s of [AGENTS, ...(ctx.changed > 0 ? [RISK] : []), RULES, ...(ctx.planSupported === false ? [] : [PLAN]), FIND]) if (shows(s)) out.push(s);
  if (ctx.next.kind !== "none") {
    const nextStep: TourStep = { target: ".ct-next", title: "Your next step", body: <>{nextStepText(ctx.next)}</> };
    out.push(shows(nextStep) ? nextStep : { ...nextStep, target: "" });
  }
  return out;
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
  return <CoachTour steps={built} onClose={onClose} rootRef={rootRef} prefix="mt" buttonClass="cm-btn" labelId="mt-title" />;
}
