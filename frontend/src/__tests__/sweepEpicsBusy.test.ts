/** Stability sweep (2026-11-12): the Epics tab tracked "starting…" with one
 * title. Starting epic B while A was still starting turned A's button back
 * into an enabled Start (a second press got the server's 409 "…is already
 * working this epic"), and A finishing cleared B's "Starting…" while B was
 * still in flight. Each epic now has its own, keyed by source:id (two
 * sources may share a title). */
import { describe, expect, it } from "vitest";
import { code } from "./sourceText";

describe("each epic starting shows its own Starting…", () => {
  it("busy is a set of source:id keys, added and removed per epic", () => {
    const tab = code("components/intake/EpicsTab.tsx");
    expect(tab).toContain("useState<ReadonlySet<string>>(() => new Set())");
    expect(tab).toContain('const epicKey = (e: EpicRow) => e.source + ":" + e.id;');
    expect(tab).toContain("setBusy((b) => new Set(b).add(k));");
    expect(tab).toContain("next.delete(k);");
    expect(tab).toContain("busy={busy.has(epicKey(e))}");
    expect(tab).not.toContain('setBusy("")');
  });
});
