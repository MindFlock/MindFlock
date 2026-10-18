/** The Map tab's tour: six short coach marks over the real map, each pointing
 * at the part it explains (the tree, the agents column, ⚠ Could break, the
 * rule tools, find). It opens by itself the first time a Map is shown on any
 * of your devices (the synced `map_tour` pref) and is replayable from the
 * toolbar's "? Guide". The coach-mark itself is the shared one
 * (onboarding/CoachTour.tsx), contained in the Map's root.
 *
 * Non-modal on purpose: the map stays live underneath (the birds keep
 * flying), and a click anywhere outside the card is allowed. Esc or Skip ends
 * it and counts as seen; so does Done. Targets are found by selector inside
 * the tab's root at each step, so a step whose target is absent (no changes
 * yet, a narrow pane) just points at the tree instead. */

import type { RefObject } from "react";
import { CoachTour, placeCard, type CoachStep } from "../../onboarding/CoachTour";
import { mapScene, placeMapCard } from "./tourPlace";

/** A Map step: its `target` is a CSS selector inside the Map's root; "" =
 * the tree as a whole. */
export type TourStep = CoachStep;

export const MAP_TOUR: TourStep[] = [
  {
    target: "",
    title: "Your code, drawn as a tree",
    body: (
      <>
        The <b>trunk</b> is the repo, each <b>branch</b> a folder and each <b>leaf</b> a file. <b>Roots</b>{" "}
        underground are the tests, and the piles on the ground are docs and config. Thicker branches hold
        more code.
      </>
    ),
  },
  {
    target: "",
    title: "Bright leaves are what changed",
    body: (
      <>
        Grey is untouched. A <b>glowing leaf</b> is a file an agent changed, and the <b>lit branches</b> lead
        you to it. Labels like <b>web/core ●4</b> count the changes in that folder. Click a leaf or folder for
        its card.
      </>
    ),
  },
  {
    target: '[data-hud="left"]',
    title: "Each agent is a bird",
    body: (
      <>
        This column lists the agents working on this repo: what each is doing now and what it changed. A bird
        <b> nests</b> where it mostly works. Click a row to follow its bird around the tree.
      </>
    ),
  },
  {
    target: ".ct-bix-risk-tog",
    title: "What could break",
    body: (
      <>
        Hover a bright leaf and the files that <b>import</b> it light up <b>gold</b>. <b>⚠ Could break</b>{" "}
        ranks the changed files by how much depends on them, so you know what to review first.
      </>
    ),
  },
  {
    target: ".ct-rules",
    title: "Fence agents in or out",
    body: (
      <>
        <b>⛔ Keep out</b>: agents may read a folder but never edit it. <b>✓ Only here</b>: they may edit only
        inside it. Pick one, then click a folder on the tree. Every changed folder in the column has the same
        two buttons.
      </>
    ),
  },
  {
    target: ".cm-search",
    title: "Getting around",
    body: (
      <>
        Press <kbd>/</kbd> to find a file or function. Scroll to zoom, drag to pan, double-click a folder to
        zoom into it, and <b>⌂ Whole tree</b> brings you back. <b>?</b> beside Agents is the legend.
      </>
    ),
  },
];

/** Where the card goes (the generic rule, re-exported for the Map's tests). */
export { placeCard };

export function MapTour({
  rootRef,
  onClose,
  steps = MAP_TOUR,
}: {
  rootRef: RefObject<HTMLElement | null>;
  onClose: () => void;
  steps?: TourStep[];
}) {
  // The generic coach tour (onboarding/CoachTour), contained in the Map's
  // root and dressed in the Map's own .mt-* / .cm-btn styles.
  return (
    <CoachTour
      steps={steps}
      onClose={onClose}
      rootRef={rootRef}
      prefix="mt"
      buttonClass="cm-btn"
      labelId="mt-title"
      place={(t, W, H, cardH) => placeMapCard(t, W, H, cardH, mapScene(rootRef.current))}
    />
  );
}
