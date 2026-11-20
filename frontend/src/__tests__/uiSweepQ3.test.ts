/** UI sweep round 2, package Q3 — the Code map and the app's chrome:
 *  #4  the Map skips a redraw when its panels did not move, one ResizeObserver
 *  #12 the Map's find box says "Searching…" (and Enter waits) until its answer
 *  #21 the usage popover claims Escape and refocuses its opener; the cost chip is a button
 *  #22 the Map's legend closes on Escape and on a canvas click
 *  #31 a pane's ⋯ menu ends in "Customize header…"; the footer and palette name the pane header
 *  #32 the low-memory notice opens the Memory popover
 *  #33 the state-file notice copies a ready rename command
 *  #34 the answer strip's re-check rests while the page is hidden */

/// <reference types="vite/client" />
import { afterEach, describe, expect, it, vi } from "vitest";
import type { Caps, Instance } from "../api/types";
import { code, norm } from "./sourceText";

vi.mock("../lib/toast", () => ({ toast: vi.fn() }));
vi.mock("../lib/errorPop", () => ({ errorPop: vi.fn(), confirmPop: vi.fn() }));
vi.mock("../lib/terminals", () => ({ focusTerm: vi.fn(), releaseTerms: vi.fn() }));
vi.mock("../api/client", async (orig) => ({
  ...((await orig()) as object),
  api: () => new Promise(() => {}),
}));

const { hudSig } = await import("../components/grid/codemap/treeCtl");
const { listState } = await import("../components/grid/codemap/SearchBox");
const { escapeRefocus } = await import("../components/usage/UsagePopover");
const { memoryRowShown } = await import("../components/usage/OverallMemory");
const { restoreCommand } = await import("../components/StateNotice");
const { actionCtx } = await import("../lib/sessionActionTable");
const { sessionMenu } = await import("../lib/sessionMenu");
const { useUi } = await import("../state/store");

afterEach(() => useUi.getState().closeDialog());

describe("#4 the Map redraws only when its panels moved", () => {
  const I = { top: 8, bot: 8, left: 8, right: 8 };
  it("gives the same signature for the same panels, a new one when any moves", () => {
    const a = hudSig([[0, 0, 100, 40]], I, 0, 0);
    expect(hudSig([[0, 0, 100, 40]], { ...I }, 0, 0)).toBe(a);
    // sub-pixel jitter is not a move
    expect(hudSig([[0.01, 0, 100.02, 40]], I, 0, 0)).toBe(a);
    expect(hudSig([[0, 0, 100, 60]], I, 0, 0)).not.toBe(a);
    expect(hudSig([], I, 0, 0)).not.toBe(a);
    expect(hudSig([[0, 0, 100, 40]], { ...I, left: 120 }, 0, 0)).not.toBe(a);
    expect(hudSig([[0, 0, 100, 40]], I, 300, 0)).not.toBe(a);
    expect(hudSig([[0, 0, 100, 40]], I, 0, 90)).not.toBe(a);
  });

  it("setHud returns before touching the view when nothing changed", () => {
    const ctl = norm(code("components/grid/codemap/treeCtl.ts"));
    const set = ctl.slice(ctl.indexOf("setHud(rects"), ctl.indexOf("setMini("));
    expect(set).toContain("const sig = hudSig(rects, insets, this.cardInset, this.cardBot);");
    expect(set).toContain("if (this.v && this.v === this.hudFor && sig === this.hudLast) return;");
    expect(set.indexOf("return;")).toBeLessThan(set.indexOf("this.dirty = true"));
  });

  it("makes one ResizeObserver per tree, not one per render", () => {
    const tree = norm(code("components/grid/codemap/CodeTree.tsx"));
    expect(tree.match(/new ResizeObserver\(/g)).toHaveLength(1);
    const at = tree.indexOf("new ResizeObserver(");
    const effect = tree.slice(at, tree.indexOf("useLayoutEffect(", at));
    expect(effect).toContain("}, [ctl]);");
    // panels that come and go are (un)observed on the same observer
    expect(tree).toContain("o.ro.unobserve(el)");
    expect(tree).toContain("o.ro.observe(el)");
  });
});

describe("#12 the find box waits for its own answer", () => {
  it("is searching while the rows answer another query", () => {
    expect(listState("auth", "", 0, "")).toBe("searching");
    expect(listState("auth", "aut", 3, "")).toBe("searching");
    expect(listState(" auth ", "auth", 3, "")).toBe("rows");
    expect(listState("auth", "auth", 0, "")).toBe("empty");
    expect(listState("auth", "auth", 0, "boom")).toBe("error");
    // an error for an older query is not shown either
    expect(listState("auth2", "auth", 0, "boom")).toBe("searching");
  });

  it("shows Searching… and picks only from the current query's rows", () => {
    const box = norm(code("components/grid/codemap/SearchBox.tsx"));
    expect(box).toContain("setAnsQ(r.q);");
    expect(box).toContain('{state === "searching" && <li className="muted">Searching…</li>}');
    expect(box).toContain('{state === "empty" && <li className="muted">No matches.</li>}');
    expect(box).toContain("pick(rows[cur]);");
    expect(box).not.toContain("pick(items[cur])");
  });
});

describe("#21 the usage popover and the cost chip", () => {
  const el = (tag: string, kids: unknown[] = []) => ({ tagName: tag, contains: (x: unknown) => x === el || kids.includes(x) }) as unknown as Element;
  it("puts focus back on the opener only from the pop, the opener or nowhere", () => {
    const anchor = el("BUTTON");
    const inside = el("BUTTON");
    const pop = el("DIV", [inside]);
    expect(escapeRefocus(inside, pop, anchor)).toBe(true);
    expect(escapeRefocus(anchor, pop, anchor)).toBe(true);
    expect(escapeRefocus(el("BODY"), pop, anchor)).toBe(true);
    expect(escapeRefocus(null, pop, anchor)).toBe(true);
    // typing in a terminal: Escape closes the pop and leaves focus there
    expect(escapeRefocus(el("TEXTAREA"), pop, anchor)).toBe(false);
  });

  it("claims Escape so the bulk bar's Escape stays put", () => {
    const pop = norm(code("components/usage/UsagePopover.tsx"));
    const at = pop.indexOf("const onKey");
    const onKey = pop.slice(at, pop.indexOf("const close =", at));
    expect(onKey).toContain("e.preventDefault();");
    expect(onKey).toContain("anchor.focus();");
  });

  it("makes the cost chip a button that looks like the chip", () => {
    const chip = norm(code("components/usage/SessionUsageChip.tsx"));
    expect(chip).toContain('<button type="button" ref={chipRef} className="tok usage-trigger"');
    expect(chip).not.toMatch(/<span ref=\{chipRef\}/);
    const css = norm(code("components/usage/usage.css"));
    const reset = css.slice(css.indexOf(".pane-head button.tok.usage-trigger {"));
    expect(reset).toMatch(/^\.pane-head button\.tok\.usage-trigger \{ appearance: none; background: none; border: 0; padding: 0;/);
  });
});

describe("#22 the Map's legend closes like the agents column", () => {
  it("folds on the canvas's close signal and on Escape", () => {
    const bix = norm(code("components/grid/codemap/BirdIndex.tsx"));
    expect(bix).toContain("useEffect(() => { setRailOpen(false); setLegend(false); }, [p.closeSeq]);");
    expect(bix).toContain('<div className="ct-bix-legend" data-hud="pop" onKeyDown={legendKey}>');
    expect(bix.match(/onKeyDown=\{legendBtnKey\}/g)).toHaveLength(2);
    const tree = norm(code("components/grid/codemap/CodeTree.tsx"));
    const shut = tree.slice(tree.indexOf("const shutRail"), tree.indexOf("const onCanvasKey"));
    expect(shut).toContain('st.querySelector(".ct-bix-legend")');
    expect(shut).toContain("if (!st || (!pop && !leg)) return false;");
  });
});

describe("#31 the pane header page of Customize is findable", () => {
  const CAPS: Caps = { git: true, tailscale: true, ticketing: true, github: true };
  const inst = { title: "s", status: "ready", branch: "me/s", path: "/w/repo/s", git_repo: true } as Instance;
  it("ends a pane's ⋯ More with Customize header…, on its page", () => {
    const m = sessionMenu(inst, actionCtx(inst.title, { inst, caps: CAPS }), { visibleTabs: [] });
    const item = m.more.find((i) => i.key === "customize-header");
    expect(item?.label).toBe("Customize header…");
    expect(m.more[m.more.length - 1]).toBe(item);
    useUi.getState().setCustomizePage("sidebar");
    void item!.run();
    expect(useUi.getState().customizePage).toBe("pane");
    expect(useUi.getState().openDialog).toBe("customize");
  });

  it("leaves it out of a sidebar row's menu", () => {
    const m = sessionMenu(inst, actionCtx(inst.title, { inst, caps: CAPS }), { onSelect: () => {} });
    expect(m.more.some((i) => i.key === "customize-header")).toBe(false);
  });

  it("names the pane header in the footer's tooltip and the palette's hint", () => {
    expect(code("components/sidebar/FooterCustomize.tsx")).toContain("what each pane's header holds");
    expect(code("components/palette/CommandPalette.tsx")).toContain('label: "Customize…", hint: "choose sidebar bars and the pane header"');
  });
});

describe("#32 the low-memory notice opens the Memory popover", () => {
  it("asks the sidebar's row through the store", () => {
    const before = useUi.getState().memoryPopAsk;
    useUi.getState().askMemoryPop();
    expect(useUi.getState().memoryPopAsk).toBe(before + 1);
    const row = norm(code("components/usage/OverallMemory.tsx"));
    expect(row).toContain("const ask = useUi((s) => s.memoryPopAsk);");
    expect(row).toContain("setOpen(true);");
  });

  it("uses the row only when it is on screen, else its own popover", () => {
    const shown = { offsetParent: {} } as unknown as HTMLElement;
    const hidden = { offsetParent: null } as unknown as HTMLElement;
    expect(memoryRowShown(false, shown)).toBe(true);
    expect(memoryRowShown(true, shown)).toBe(false);
    expect(memoryRowShown(false, hidden)).toBe(false);
    expect(memoryRowShown(false, null)).toBe(false);
    const notice = norm(code("components/MemoryNotice.tsx"));
    expect(notice).toContain("See what&apos;s using it");
    expect(notice).toContain('if (memoryRowShown(ui.sidebarHidden, document.getElementById("overall-memory"))) ui.askMemoryPop(); else setPopHere((o) => !o);');
    expect(notice).toContain("<MemoryPopAt anchor={seeRef.current}");
    // the fallback pop rides above the notice it rises from
    expect(norm(code("components/usage/usage.css"))).toContain(".usage-pop.mem-pop-over-notice { z-index: 4000; }");
  });
});

describe("#33 the state-file notice copies the rename", () => {
  it("builds a quoted mv into the same folder", () => {
    expect(restoreCommand("/home/me/.mindflock/state.json.v9.bak")).toBe(
      "mv '/home/me/.mindflock/state.json.v9.bak' '/home/me/.mindflock/state.json'"
    );
    expect(restoreCommand("/home/o'neil/my dir/state.json.bak")).toBe(
      "mv '/home/o'\\''neil/my dir/state.json.bak' '/home/o'\\''neil/my dir/state.json'"
    );
  });

  it("uses PowerShell's Move-Item for a Windows path", () => {
    expect(restoreCommand("C:\\Users\\me\\.mindflock\\state.json.bak")).toBe(
      "Move-Item -Force 'C:\\Users\\me\\.mindflock\\state.json.bak' 'C:\\Users\\me\\.mindflock\\state.json'"
    );
  });

  it("puts a Copy button by the path", () => {
    const n = norm(code("components/StateNotice.tsx"));
    expect(n).toContain("copyText(restoreCommand(notice.backup_path!))");
    expect(n).toContain("Copy the rename command");
  });
});

describe("#34 the answer strip rests while hidden", () => {
  it("re-checks through pollWhileVisible, not a bare interval", () => {
    const s = norm(code("components/AnswerStrip.tsx"));
    const at = s.indexOf('const answered = state.phase === "answered";');
    const effect = s.slice(at, s.indexOf("}, [answered, waiting, title]);", at));
    expect(effect).toContain("pollWhileVisible(");
    expect(effect).not.toContain("setInterval");
  });
});
