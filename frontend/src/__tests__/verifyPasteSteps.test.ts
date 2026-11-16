/** Verify: a person's step carries the exact text to paste (`paste`), shown
 * in a monospace box with a one-click Copy — on the pane / bell / stop
 * confirm (HumanSteps), on the card's rows (BehaviorRow) and in the tooltip
 * (verifyStepLines). Copy goes through lib/clipboard's copyText (Electron
 * blocks navigator.clipboard) and never lets the click reach a drag or
 * selection handler.
 *
 * vitest is node-only: pure helpers and renderToStaticMarkup. */

import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { SessionVerifyHumanStep, TestPlan, TestStep } from "../api/types";

const copied: string[] = [];
let copyOk = true;
vi.mock("../lib/clipboard", async (orig) => ({
  ...((await orig()) as object),
  copyText: (t: string) => {
    copied.push(t);
    return Promise.resolve(copyOk);
  },
}));

const { PasteBox, copyPaste, pasteCopyLabel, COPIED_MS } = await import("../components/behaviors/PasteBox");
const { HumanSteps, VerifyConfirm } = await import("../components/behaviors/VerifyConfirm");
const { BehaviorRow } = await import("../components/behaviors/BehaviorRow");
const { verifyStepLines } = await import("../lib/sessionState");

const CMD = "/deploy staging --service web";
const STEPS: SessionVerifyHumanStep[] = [
  { id: "s3", text: "In the team chat run the deploy command", expect: "a 'deploying web' reply", paste: CMD },
  { id: "s4", text: "Click Save on the Settings page", expect: "a 'Saved' toast" },
];

beforeEach(() => {
  copied.length = 0;
  copyOk = true;
});

describe("PasteBox", () => {
  it("shows the text in a monospace box with a Copy button, kept off drags", () => {
    const html = renderToStaticMarkup(createElement(PasteBox, { text: CMD }));
    expect(html).toContain('<code class="vc-paste-text">' + CMD + "</code>");
    expect(html).toContain("vc-paste-copy");
    expect(html).toContain(">Copy</button>");
    expect(html).toContain("data-no-drag");
  });

  it("draws nothing without text", () => {
    expect(renderToStaticMarkup(createElement(PasteBox, { text: "" }))).toBe("");
    expect(renderToStaticMarkup(createElement(PasteBox, { text: "   " }))).toBe("");
    expect(renderToStaticMarkup(createElement(PasteBox, {}))).toBe("");
  });

  it("copies through copyText, stops the click, and says Copied briefly", async () => {
    const e = { stopPropagation: vi.fn(), preventDefault: vi.fn() };
    expect(await copyPaste(e, CMD)).toBe("copied");
    expect(copied).toEqual([CMD]);
    expect(e.stopPropagation).toHaveBeenCalled();
    expect(pasteCopyLabel("copied")).toBe("Copied");
    expect(pasteCopyLabel("")).toBe("Copy");
    expect(COPIED_MS).toBeGreaterThan(0);
    expect(COPIED_MS).toBeLessThanOrEqual(3000);
  });

  it("says when the copy failed", async () => {
    copyOk = false;
    expect(await copyPaste({ stopPropagation() {} }, CMD)).toBe("failed");
    expect(pasteCopyLabel("failed")).toBe("Copy failed");
  });
});

describe("the person's steps show their paste", () => {
  it("HumanSteps: the step with a paste gets the box, the other does not", () => {
    const html = renderToStaticMarkup(createElement(HumanSteps, { steps: STEPS }));
    expect(html.match(/vc-paste-text/g)?.length).toBe(1);
    // The paste sits between the instruction and what to expect.
    const at = (s: string) => html.indexOf(s);
    expect(at(STEPS[0].text)).toBeLessThan(at(CMD));
    expect(at(CMD)).toBeLessThan(at("Expect: a &#x27;deploying web&#x27; reply"));
  });

  it("VerifyConfirm draws it (the pane, the bell, a Flight stop)", () => {
    const html = renderToStaticMarkup(createElement(VerifyConfirm, { verify: { plan: "sc-1", human_steps: STEPS } }));
    expect(html).toContain(CMD);
    expect(html).toContain("vc-paste-copy");
  });

  it("the tooltip lists it under its step", () => {
    expect(verifyStepLines(STEPS)).toBe(
      "• In the team chat run the deploy command — a 'deploying web' reply\n    Paste: " + CMD + "\n• Click Save on the Settings page — a 'Saved' toast",
    );
  });
});

describe("the card's row", () => {
  const step: TestStep = { id: "s1", text: "In the team chat run the deploy command", expect: "a reply", actor: "human", paste: CMD };
  const plan = (results: Record<string, unknown> = {}): TestPlan =>
    ({
      id: "sc-1",
      state: "running",
      steps: [step],
      runs: [{ id: "r1", by: "agent", session: "x", at: 1, results, verdict: "" }],
    }) as unknown as TestPlan;

  it("shows a person's open paste even folded", () => {
    const html = renderToStaticMarkup(createElement(BehaviorRow, { plan: plan(), step, index: 0 }));
    expect(html).toContain("bh-paste");
    expect(html).toContain(CMD);
  });

  it("folds it away once the step is answered", () => {
    const html = renderToStaticMarkup(
      createElement(BehaviorRow, { plan: plan({ s1: { result: "pass", note: "", at: 2, by: "human" } }), step, index: 0 }),
    );
    expect(html).not.toContain("bh-paste");
  });
});
