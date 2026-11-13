/** Stability sweep (2026-11-12): tickets ticked to "Start together" stayed
 * picked after one of them was started on its own (its row's Start, Override
 * or Reopen) or got a session elsewhere, and the checkbox was drawn on rows
 * that already have one. "Start together…" then sent those to New, where
 * each failed with "session … already exists" — tickets the person never
 * knowingly included. Only tickets the list still offers to start count,
 * as they are listed now. */
import { describe, expect, it } from "vitest";
import { canPick, startablePicks } from "../components/intake/picks";
import { code } from "./sourceText";

type T = { source: string; id: string; has_session?: boolean; elsewhere?: { device: string; label: string; kind: string }; name?: string };
const key = (t: T) => t.source + ":" + t.id;

describe("Start together takes only the picks still startable", () => {
  it("a picked ticket that got a session, is held on another device, or left the list drops out", () => {
    const a: T = { source: "sc", id: "1" };
    const b: T = { source: "sc", id: "2" };
    const c: T = { source: "sc", id: "3" };
    const d: T = { source: "sc", id: "4" };
    const picked = new Map([a, b, c, d].map((t) => [key(t), t]));
    const listed: T[] = [
      { ...a, name: "renamed" },
      { ...b, has_session: true },
      { ...c, elsewhere: { device: "mini", label: "on mini", kind: "session" } },
    ];
    expect(startablePicks(picked, listed, key)).toEqual([{ ...a, name: "renamed" }]);
  });

  it("the list not read yet: the picks as they are", () => {
    const a: T = { source: "sc", id: "1" };
    expect(startablePicks(new Map([[key(a), a]]), null, key)).toEqual([a]);
  });

  it("a row with a session (here or elsewhere) has no pick box", () => {
    const row = (over: Partial<T>): T => ({ source: "sc", id: "1", ...over });
    expect(canPick(row({}))).toBe(true);
    expect(canPick(row({ has_session: true }))).toBe(false);
    expect(canPick(row({ elsewhere: { device: "m", label: "l", kind: "k" } }))).toBe(false);
  });
});


describe("the Tickets tab asks it", () => {
  it("the pick box, the bar's count and what Start together sends", () => {
    const tab = code("components/intake/TicketsTab.tsx");
    expect(tab).toContain("const pickedNow = startablePicks(picked, tickets, pickKey);");
    expect(tab).toMatch(/pick=\{\s*canPick\(t\)/);
    expect(tab).toContain("{pickedNow.length > 0 && (");
    expect(tab).toContain("startTogetherText(pickedNow)");
    expect(tab).not.toContain("startTogetherText([...picked.values()])");
  });
});
