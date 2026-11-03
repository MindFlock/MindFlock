/**
 * The + palette's filter (StopPalette, docs/flights.md "Type to filter"). The
 * owner: "when i click the plus i should be able to just start typing and it
 * narrows down the list". The field has the focus as the palette opens; what
 * is typed narrows the kinds and the saved chains (label-starts-with first);
 * ↓ / ↑ walk, Enter picks, Esc clears and then closes; keys never reach the
 * Sky's own single-letter shortcuts.
 */
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";
import { SRC } from "./sourceText";
import type { StepKindDef } from "../lib/flights";
import { StopPalette, bestRow, paletteKey, paletteRank, paletteRows, type PaletteRow, type PaletteSaved } from "../components/flights/StopParts";
import { skyKeysOwn } from "../components/flights/SkyView";

const kind = (k: string, label: string, actor: "robot" | "you", summary: string): StepKindDef => ({ kind: k, label, actor, summary, fields: [], default_title: label });
const KINDS: StepKindDef[] = [
  kind("agent", "Agent", "robot", "Send the agent a prompt"),
  kind("ship", "Ship", "robot", "Commit, push or open a PR"),
  kind("verify", "Verify", "robot", "Run the checks; a failure halts"),
  kind("wait", "Wait", "robot", "Wait some minutes, or until quiet"),
  kind("loop", "Loop", "robot", "Go back to an earlier stop"),
  kind("you", "You", "you", "Wait for you to say done"),
  kind("someone", "Someone", "you", "Wait on someone else"),
];
const plan = (id: string, name: string, title = ""): PaletteSaved => ({ id, name, title, onPick: () => {} });
const SAVED = [plan("p1", "Release", "Verify → Ship"), plan("p2", "Nightly tidy", "Agent → Wait")];

/** What a row says: its kind id, or "plan:<id>". */
const said = (rows: PaletteRow[]) => rows.map((r) => (r.group === "saved" ? "plan:" + r.saved.id : r.kind.kind));

describe("the palette's filter (pure)", () => {
  it("an empty query keeps everything, groups in order: MindFlock does, You do, saved", () => {
    const rows = paletteRows(KINDS, SAVED, "  ");
    expect(said(rows)).toEqual(["agent", "ship", "verify", "wait", "loop", "you", "someone", "plan:p1", "plan:p2"]);
    expect(rows.map((r) => r.group)).toEqual(["robot", "robot", "robot", "robot", "robot", "you", "you", "saved", "saved"]);
    expect(bestRow(rows)).toBe(0);
  });

  it("matches the label kindName says, the kind id and the summary, case-blind", () => {
    // "Wait for me" is the `you` kind's palette name, not its server label.
    expect(said(paletteRows(KINDS, [], "FOR ME"))).toEqual(["you"]);
    expect(said(paletteRows(KINDS, [], "someone"))).toEqual(["someone"]);
    expect(said(paletteRows(KINDS, [], "push"))).toEqual(["ship"]);
    // Every word, anywhere (the floating palette's catalogMatch).
    expect(said(paletteRows(KINDS, [], "checks halts"))).toEqual(["verify"]);
    expect(said(paletteRows(KINDS, [], "zzz"))).toEqual([]);
  });

  it("a label that starts with it comes first, then a word of it, then the rest", () => {
    // "wait": Wait (label prefix) before Agent/Ship… whose summary only has it;
    // in You do, "Wait for me" (prefix) before "Someone" (summary word).
    const rows = paletteRows(KINDS, [], "wait");
    expect(said(rows)).toEqual(["wait", "you", "someone"]);
    expect(rows.map((r) => r.rank)).toEqual([0, 0, 2]);
    expect(paletteRank("me", "Wait for me")).toBe(1);
    expect(paletteRank("ait", "Wait for me")).toBe(3);
    expect(paletteRank("prompt", "Agent", "agent", "Send the agent a prompt")).toBe(2);
    expect(paletteRank("x", "Agent")).toBe(-1);
    // Within a group a better rank leads, the server's order breaks ties.
    // "s": Ship's label (0); Send / some / stop start a summary word (2);
    // Verify only has an s inside "checks" (3).
    expect(said(paletteRows(KINDS, [], "s"))).toEqual(["ship", "agent", "wait", "loop", "verify", "someone", "you"]);
  });

  it("the saved chains narrow by name and by their stops", () => {
    expect(said(paletteRows(KINDS, SAVED, "release"))).toEqual(["plan:p1"]);
    expect(said(paletteRows([], SAVED, "ship"))).toEqual(["plan:p1"]);
    expect(said(paletteRows([], SAVED, "tidy"))).toEqual(["plan:p2"]);
  });

  it("the highlight starts on the best match, the first drawn among equals", () => {
    // "ship": the Ship kind is a prefix match (rank 0), Release's stops a word (2).
    const rows = paletteRows(KINDS, SAVED, "ship");
    expect(said(rows)).toEqual(["ship", "plan:p1"]);
    expect(bestRow(rows)).toBe(0);
    // "night": only Nightly tidy — the highlight lands past every kind.
    expect(bestRow(paletteRows(KINDS, SAVED, "night"))).toBe(0);
    // A You do prefix beats a MindFlock does summary word.
    const some = paletteRows(KINDS, [], "some");
    expect(said(some)).toEqual(["wait", "someone"]);
    expect(said(some)[bestRow(some)]).toBe("someone");
    expect(bestRow([])).toBe(-1);
  });
});

describe("the palette's keys (pure)", () => {
  it("↓ / ↑ walk the rows, round the ends", () => {
    expect(paletteKey("ArrowDown", "", 0, 3)).toEqual({ hi: 1 });
    expect(paletteKey("ArrowDown", "", 2, 3)).toEqual({ hi: 0 });
    expect(paletteKey("ArrowUp", "", 0, 3)).toEqual({ hi: 2 });
    expect(paletteKey("ArrowUp", "", 2, 3)).toEqual({ hi: 1 });
    expect(paletteKey("ArrowDown", "zz", -1, 0)).toEqual({});
  });

  it("Enter picks the highlighted row — with no arrow pressed, the first match", () => {
    const rows = paletteRows(KINDS, SAVED, "ver");
    const hi = bestRow(rows);
    const out = paletteKey("Enter", "ver", hi, rows.length);
    expect(out).toEqual({ pick: 0 });
    expect(said(rows)[out!.pick!]).toBe("verify");
    // Nothing matches: Enter does nothing (but is still the field's).
    expect(paletteKey("Enter", "zz", -1, 0)).toEqual({});
  });

  it("Esc clears the text first, then closes", () => {
    expect(paletteKey("Escape", "ve", 0, 1)).toEqual({ query: "" });
    expect(paletteKey("Escape", "", 0, 9)).toEqual({ close: true });
  });

  it("any other key is the field's (it types)", () => {
    for (const k of ["t", "T", "Backspace", "Delete", "ArrowLeft", " ", "?"]) expect(paletteKey(k, "x", 0, 3)).toBeNull();
  });
});

describe("StopPalette draws the filter", () => {
  const render = (props: Partial<Parameters<typeof StopPalette>[0]> = {}) =>
    renderToStaticMarkup(createElement(StopPalette, { kinds: KINDS, onPick: () => {}, onClose: () => {}, ...props }));

  it("the field is first and has the focus as the palette opens", () => {
    const html = render();
    const field = html.match(/<input[^>]*fl-palette-filter[^>]*>/)?.[0] || "";
    expect(field).toContain('placeholder="Type to filter…"');
    expect(field).toContain("autofocus");
    // No stop card takes the focus from it.
    expect(html.replace(field, "")).not.toContain("autofocus");
    expect(html.indexOf("fl-palette-filter")).toBeLessThan(html.indexOf("MindFlock does"));
    // With no query the first card is the highlighted one (Enter picks it).
    expect(html.match(/class="fl-palette-item[^"]*is-hi"[^>]*>/g)?.length).toBe(1);
    expect(html).toMatch(/class="fl-palette-item fl-kind-robot is-hi"[^>]*>.*?>Agent</);
  });

  it("typing narrows the cards; an emptied group hides its head", () => {
    const html = render({ initialQuery: "verify" });
    expect(html).toContain(">Verify<");
    expect(html).not.toContain(">Agent<");
    expect(html).not.toContain("You do");
    expect(html).toContain("MindFlock does");
    const you = render({ initialQuery: "for me" });
    expect(you).not.toContain("MindFlock does");
    expect(you).toContain("You do");
    expect(you).toContain(">Wait for me<");
  });

  it("the saved chains are chips the filter narrows too, highlighted when they're the best", () => {
    const all = render({ saved: SAVED });
    expect(all).toContain("A saved chain — all of its stops");
    expect(all).toMatch(/class="fl-pick"[^>]*title="Verify → Ship"[^>]*>Release</);
    expect(all).toContain(">Nightly tidy<");
    const night = render({ saved: SAVED, initialQuery: "night" });
    expect(night).not.toContain(">Release<");
    expect(night).toMatch(/class="fl-pick is-hi"[^>]*>Nightly tidy</);
    expect(night).not.toContain("MindFlock does");
    // No saved chain matches: their heading goes with them.
    expect(render({ saved: SAVED, initialQuery: "verify checks" })).not.toContain("A saved chain");
    // None at all: no heading.
    expect(render()).not.toContain("A saved chain");
  });

  it("nothing left says so, quietly", () => {
    const html = render({ saved: SAVED, initialQuery: " zebra " });
    expect(html).toContain("No stop matches “zebra”");
    expect(html).not.toContain("fl-palette-item");
    expect(html).not.toContain("fl-palette-head");
    expect(render()).not.toContain("No stop matches");
  });

  it("a click still picks: every card and chip keeps its onClick", () => {
    const src = SRC["components/flights/StopParts.tsx"];
    expect(src).toContain("onClick={() => onPick(r.kind)}");
    expect(src).toContain("onClick={() => r.saved.onPick()}");
  });
});

describe("typed keys stay in the field", () => {
  it("the field stops every key before the Sky's handlers see it", () => {
    const src = SRC["components/flights/StopParts.tsx"];
    const body = src.slice(src.indexOf("const onFilterKey"), src.indexOf("const groups: Array<[PaletteRow"));
    expect(body.indexOf("e.stopPropagation()")).toBeGreaterThan(-1);
    expect(body.indexOf("e.stopPropagation()")).toBeLessThan(body.indexOf("paletteKey("));
  });

  it("the Sky's own keys (T tidies…) skip a focused field anyway", () => {
    const g = globalThis as Record<string, unknown>;
    const had = "document" in g;
    if (!had) g.document = { body: {} };
    try {
      const input = { tagName: "INPUT", closest: () => null } as unknown as Element;
      expect(skyKeysOwn(input)).toBe(false);
    } finally {
      if (!had) delete g.document;
    }
  });

  it("the Sky's chain passes its saved chains as data, not markup", () => {
    const hook = SRC["components/flights/useGroundedLine.tsx"];
    expect(hook).toContain("saved={savedChips}");
    expect(hook).not.toMatch(/<StopPalette[\s\S]*?extra=/);
  });
});
