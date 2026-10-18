/** The whole-tree view (⌂, a first look): the box the picture needs and the
 * camera that fits it into the free canvas (the canvas minus the bird index on
 * the left and any card or panel on the right). Pure, for the tests. */

import { S0, type Model } from "./model";
import type { Cam } from "./types";

export interface Box {
  x0: number;
  y0: number;
  x1: number;
  y1: number;
}

export interface Edges {
  top: number;
  bot: number;
  left: number;
  right: number;
}

/** Screen px the picture needs past its outermost leaves: names beside the
 * outer leaves, bird tags over the crown, the pile names under the ground. */
export const WHOLE_PAD = { side: 36, top: 40, bot: 14, piles: 34 };

/** The world box the whole-tree view shows: every leaf (crown, roots, ground
 * piles) and the ground line under the trunk — not the model's padded bounds. */
export function pictureBox(M: Pick<Model, "bounds" | "groundY" | "leaves">): Box {
  const B = M.bounds,
    m = M.leaves.length ? S0 * 6 : 0;
  return { x0: B.x0 + m, y0: B.y0 + m, x1: B.x1 - m, y1: Math.max(B.y1 - m, M.groundY) };
}

/** The camera that fits box B into a W×H canvas inside insets I: as large as
 * fits, centred across the free canvas. Spare height goes to the sky, so the
 * soil below the ground line is only as deep as the roots and the pile names
 * need — a thin band when there are no tests. */
export function fitWhole(B: Box, groundY: number, piles: boolean, W: number, H: number, I: Edges): Cam {
  // pile names hang ~30 px under the ground line; roots deeper than that carry their own room
  const padB = piles && B.y1 - groundY < S0 * 2 ? WHOLE_PAD.piles : WHOLE_PAD.bot;
  const w = Math.max(40, W - I.left - I.right - 2 * WHOLE_PAD.side),
    h = Math.max(40, H - I.top - I.bot - WHOLE_PAD.top - padB);
  const z = Math.min(w / Math.max(1, B.x1 - B.x0), h / Math.max(1, B.y1 - B.y0));
  const sx = (I.left + W - I.right) / 2,
    sy = H - I.bot - padB;
  return { x: (B.x0 + B.x1) / 2 - (sx - W / 2) / z, y: B.y1 - (sy - H / 2) / z, z };
}

/** Where box B lands on screen under camera c (canvas px). */
export function boxOnScreen(B: Box, c: Cam, W: number, H: number): Box {
  return {
    x0: (B.x0 - c.x) * c.z + W / 2,
    y0: (B.y0 - c.y) * c.z + H / 2,
    x1: (B.x1 - c.x) * c.z + W / 2,
    y1: (B.y1 - c.y) * c.z + H / 2,
  };
}
