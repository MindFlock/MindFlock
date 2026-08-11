/** The folder-row fit test.
 *
 * The suggestion rows are one line with no scrollbar, so chips past the edge
 * are hidden rather than clipped mid-pill. The comparison is one line of
 * arithmetic and still managed to be wrong in the way that matters: the first
 * version measured children with offsetLeft, which is relative to
 * offsetParent — and the offsetParent here is .modal (position: fixed), so
 * every chip's offset included the dialog's distance from the window edge and
 * compared as overflowing. Recent and Nearby rendered completely empty.
 *
 * Hence the shape of these: the two edges are only comparable when they come
 * from the same origin, and the cases below are written in viewport
 * coordinates (a dialog that does NOT start at x=0) so that mistake cannot
 * pass again.
 */

import { describe, it, expect } from "vitest";
import { fitsWithin } from "../components/dialogs/NewSessionDialog";

describe("fitsWithin", () => {
  // A row inside a dialog that starts 300px into the window and ends at 900.
  const rowRight = 900;

  it("keeps a chip that ends inside the row", () => {
    expect(fitsWithin(420, rowRight)).toBe(true);
    expect(fitsWithin(899, rowRight)).toBe(true);
  });

  it("drops a chip that ends past the row", () => {
    expect(fitsWithin(901, rowRight)).toBe(false);
    expect(fitsWithin(1400, rowRight)).toBe(false);
  });

  it("keeps a chip landing exactly on the edge, and a hair past it", () => {
    // Sub-pixel layout routinely reports a fitting child a fraction over.
    expect(fitsWithin(900, rowRight)).toBe(true);
    expect(fitsWithin(900.4, rowRight)).toBe(true);
    expect(fitsWithin(900.6, rowRight)).toBe(false);
  });

  it("keeps chips whose coordinates are offset far from the origin", () => {
    // The regression, with the geometry it actually had: a 620px dialog
    // centred in a ~1500px window, so the row runs x = 516…1016 and its first
    // chip ends at 616. Measured against the row's right EDGE it plainly
    // fits; measured against the row's WIDTH (500) — which is what comparing
    // an offsetParent-relative offset to clientWidth amounts to — it reads as
    // overflow, and so did every chip after it.
    const rowRightEdge = 1016;
    const rowWidth = 500;
    expect(fitsWithin(616, rowRightEdge)).toBe(true);
    expect(fitsWithin(616, rowWidth)).toBe(false); // the old, wrong comparison
  });
});
