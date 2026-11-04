/** The sky as the main area's second view (lib/viewMode.ts, docs/web-ui.md
 * "Two views: the grid and the sky"): the view store, every switch point
 * (top bar, sidebar View row, Ctrl+Y, the palette, the hash), the isomorphism's
 * invariants (one focused session, one open tab per session, This session =
 * the focused one alone, the grid's 1 ↔ the sky's 1), the layout store (sizes,
 * Tidy, the mf_sky_pos migration) and the transition's pairing. */

import { afterEach, describe, expect, it, vi } from "vitest";
import { code, css } from "./sourceText";

/** A Map-backed localStorage, installed BEFORE the store module loads. */
const storage = new Map<string, string>();
vi.stubGlobal("localStorage", {
  getItem: (k: string) => (storage.has(k) ? storage.get(k)! : null),
  setItem: (k: string, v: string) => void storage.set(k, String(v)),
  removeItem: (k: string) => void storage.delete(k),
  clear: () => storage.clear(),
});
vi.mock("../api/client", () => ({ api: vi.fn(), ApiError: class extends Error {} }));
vi.mock("../lib/toast", () => ({ toast: vi.fn() }));

const { useUi, setSessionSelector } = await import("../state/store");
const { VIEWS, flipPairs, newSession, registerSkyHost, setView, showInSky, toggleView, viewLabel } = await import("../lib/viewMode");
const { installSurfaceHash, parseViewHash, viewHash } = await import("../lib/surfaceHash");
const { KEYMAP } = await import("../lib/keymap");
const sky = await import("../lib/skyLayout");

afterEach(() => {
  useUi.setState({ view: "grid", skyMode: "mine", focused: null, openDialog: null, dialogTarget: null } as never);
  setSessionSelector(null);
});

describe("the view store", () => {
  it("is the grid by default, remembered per device as mf_view", () => {
    expect(VIEWS).toEqual(["grid", "sky"]);
    expect(useUi.getState().view).toBe("grid");
    setView("sky");
    expect(useUi.getState().view).toBe("sky");
    expect(storage.get("mf_view")).toBe("sky");
    toggleView();
    expect(useUi.getState().view).toBe("grid");
    expect(storage.get("mf_view")).toBe("grid");
    expect([viewLabel("grid"), viewLabel("sky")]).toEqual(["Grid", "Sky"]);
  });

  it("the sky's lines default to This session and every pick is remembered", () => {
    expect(code("state/store.ts")).toContain('load<string>("mf_sky_view", "one", false)');
    expect(sky.SKY_MODES[0].id).toBe("one");
    useUi.getState().setSkyMode("all");
    expect(storage.get("mf_sky_view")).toBe("all");
    useUi.getState().setSkyMode("one");
    expect(storage.get("mf_sky_view")).toBe("one");
    expect(useUi.getState().skyMode).toBe("one");
    // A plain #sky (no title) keeps the pick rather than leaving This session.
    useUi.getState().showSky();
    expect(useUi.getState().skyMode).toBe("one");
  });
});

describe("the isomorphism", () => {
  it("one focused session for both views: switching keeps it, and the grid re-selects it so a capped view gives it a slot", () => {
    const picked: string[] = [];
    setSessionSelector((t) => {
      picked.push(t);
      useUi.getState().setFocused(t);
    });
    useUi.setState({ focused: "fix-login" } as never);
    setView("sky");
    expect(useUi.getState().focused).toBe("fix-login");
    expect(picked).toEqual([]);
    setView("grid");
    expect(useUi.getState().focused).toBe("fix-login");
    expect(picked).toEqual(["fix-login"]);
  });

  it("This session is the focused pane alone: Show in the Sky selects the session and shows it by itself", () => {
    const picked: string[] = [];
    setSessionSelector((t, o) => {
      picked.push(t + (o?.noKeyboard ? ":quiet" : ""));
      useUi.getState().setFocused(t);
    });
    showInSky("worker-2");
    const s = useUi.getState();
    expect([s.view, s.skyMode, s.focused]).toEqual(["sky", "one", "worker-2"]);
    expect(picked).toEqual(["worker-2:quiet"]);
    // A plain #sky keeps the pick (This session is the default now).
    useUi.getState().showSky();
    expect(useUi.getState().skyMode).toBe("one");
    // The sky's This session is skyLineTitles' "one" on the focused title.
    expect(code("components/flights/SkyView.tsx")).toContain("skyLineTitles(rows, mode, { query, focus: oneFocus })");
  });

  it("the pane's open tab is the line's open box: the box IS the session's Pane, which reads lastTab per title", () => {
    // The seam re-exports the real line, whose open card is the session's Pane.
    expect(code("components/flights/skyContracts.tsx")).toContain('export * from "./SkyLine";');
    expect(code("components/flights/SkyLine.tsx")).toContain("<Pane key={inst.title} inst={inst} embedded />");
    expect(code("components/grid/Pane.tsx")).toContain("const lastTab = useUi((s) => s.lastTab[title]);");
    // A line is keyed by its title, so its box (and the terminal it adopts
    // from lib/terminals' registry) is the same session's either way.
    expect(code("components/flights/SkyView.tsx")).toContain("key={id}\n      title={id}");
  });

  it("selecting a line is selecting the session (a pane click's selectSession), and a selection anywhere brings its line into view", () => {
    const v = code("components/flights/SkyView.tsx");
    expect(v).toContain("selectSession(id, { noKeyboard: true });");
    expect(v).toContain("focused={focused === id}");
    // Round 17: eased (goTo), the same target.
    expect(v).toContain("goTo(centerOn(r, vp.w, vp.h, Math.max(c.z, 0.85)));");
    // A press on the line itself never moves the camera (a wide line is never whole in view).
    expect(v).toContain("if (!inView && !pressed) {");
    expect(v).toContain("onFocus={() => focusOn(id, true)}");
    // The browser's scroll-into-view on a focused box becomes a pan, never a slide of the plane.
    expect(v).toContain("onScroll={onViewportScroll}");
  });

  it("the grid's 1 is the sky's 1 (fit the focused line), its 4 / 9 the sky's Fit (all of them)", () => {
    const v = code("components/flights/SkyView.tsx");
    // Round 17: the first picture is instant (nothing to glide from); 1 and the button ease.
    expect(v).toContain("if (first && gridOne && mode !== \"one\") fitFocused(true);");
    expect(v).toContain('} else if (k === "1") {');
    expect(v).toMatch(/className="sky-zoom-btn sky-fit-one"[^>]*onClick=\{\(\) => fitFocused\(\)\}/);
  });

  it("New is the sky's new-session card: Ctrl/Alt+N and the top bar's New open it mid-view in the sky, the dialog in the grid", () => {
    const here = vi.fn();
    const off = registerSkyHost({ newHere: here, ready: () => true, lineRects: () => new Map() });
    newSession();
    expect(useUi.getState().openDialog).toBe("new-session");
    useUi.getState().closeDialog();
    setView("sky");
    newSession();
    expect(here).toHaveBeenCalledTimes(1);
    expect(useUi.getState().openDialog).toBeNull();
    off();
    newSession();
    expect(useUi.getState().openDialog).toBe("new-session");
    for (const k of KEYMAP.filter((e) => e.id === "new" || e.aliasOf === "new")) {
      useUi.getState().closeDialog();
      here.mockClear();
      const off2 = registerSkyHost({ newHere: here, ready: () => true, lineRects: () => new Map() });
      k.run({} as KeyboardEvent);
      expect(here, k.key + (k.alt ? " alt" : "")).toHaveBeenCalledTimes(1);
      off2();
    }
    expect(code("components/TopBar.tsx")).toContain("onClick={() => newSession()}");
  });
});

describe("the switch points", () => {
  it("the top bar: a Grid · Sky pair beside Flights, the sky's half the tour's target, the shortcut in the tooltip", () => {
    const top = code("components/TopBar.tsx");
    expect(top.indexOf('id="flights-btn"')).toBeLessThan(top.indexOf('<span className={"tb-views"'));
    expect(top).toContain('data-tour={v === "sky" ? "view-sky" : undefined}');
    expect(top).toContain('title={viewLabel(v) + " — " + VIEW_BLURB[v] + keys("view")}');
    expect(top).toContain("onClick={() => setView(v)}");
    expect(top).toContain("aria-pressed={ui.view === v}");
    // The words go on a phone; the pair folds only after Settings, into ⋯.
    expect(top).toContain('<span className={"tb-views" + (isFolded("view") ? " tb-folded" : "")} data-fold="view"');
    expect(top.indexOf('key: "view"')).toBeLessThan(top.indexOf('{ key: "settings"'));
    expect(css("components/TopBar.css")).toMatch(/body\.narrow #topbar \.tb-view-word \{[^}]*display: none;/);
  });

  it("the sidebar's View row: Auto · 1 · 2 · 4 · 9 │ Grid · Sky — a number returns to the grid", () => {
    const side = code("components/sidebar/Sidebar.tsx");
    expect(side).toContain('className={"vt" + (ui.view === v ? " active" : "")}');
    expect(side).toContain('data-tour={v === "sky" ? "view-sky" : undefined}');
    expect(side).not.toContain("vm-sky");
    // Both halves of the toggle carry their view (VIEWS = grid, sky).
    expect(side).toContain("data-view={v}");
    expect(side).toMatch(/ui\.setViewMode\(v\);\s*setView\("grid"\);/);
    expect(side).toContain('className={"vm" + (ui.view === "grid" && ui.viewMode === v ? " active" : "")}');
  });

  it("Ctrl+Y (⌘Y) toggles, in the Navigation group, on a letter no other binding uses; Alt+Y is gone", () => {
    const y = KEYMAP.find((k) => k.id === "view")!;
    expect(y).toMatchObject({ key: "y", mod: true });
    expect(y.alt).toBeUndefined();
    expect(y.help?.[0]).toBe("Navigation");
    expect(y.help?.[1]).toBe("Ctrl+Y");
    expect(KEYMAP.filter((k) => k.key === "y")).toHaveLength(1);
    y.run({} as KeyboardEvent);
    expect(useUi.getState().view).toBe("sky");
    y.run({} as KeyboardEvent);
    expect(useUi.getState().view).toBe("grid");
  });

  it("the palette offers the other view", () => {
    const p = code("components/palette/CommandPalette.tsx");
    expect(p).toContain('label: "Switch to the grid", hint: "Ctrl+Y"');
    expect(p).toContain('label: "Switch to the Sky", hint: "Ctrl+Y"');
  });

  it("App draws the sky in the grid's place — nothing else on the page changes", () => {
    const app = code("App.tsx");
    expect(app).toContain('{view === "sky" ? <SkyView /> : <TerminalGrid specialPanes={specialPanes} />}');
    expect(app).toContain("useEffect(() => installViewFlip(), []);");
    expect(app).toContain("<TopBar />");
    // The flat dialogs, each its own; no Flights dialog any more.
    for (const d of ["<IntakeDialog />"]) expect(app).toContain(d);
    // Verify is a stop on the route: no dialog (docs/behaviors.md §4).
    expect(app).not.toContain("<VerifyDialog />");
    expect(app).not.toContain("FlightsDialog");
  });
});

describe("the hash", () => {
  const fakeWin = (hash: string) => {
    const loc = { pathname: "/", search: "", hash };
    const calls: string[] = [];
    const listeners: Array<() => void> = [];
    const win = {
      location: loc,
      history: {
        pushState: (_d: unknown, _u: string, url: string) => {
          calls.push("push " + url);
          loc.hash = url.includes("#") ? url.slice(url.indexOf("#")) : "";
        },
        replaceState: (_d: unknown, _u: string, url: string) => {
          calls.push("replace " + url);
          loc.hash = url.includes("#") ? url.slice(url.indexOf("#")) : "";
        },
        back: () => {
          calls.push("back");
          loc.hash = "";
          listeners.forEach((f) => f());
        },
      },
      addEventListener: (_t: string, f: () => void) => void listeners.push(f),
      removeEventListener: () => {},
    } as unknown as Window;
    return { win, loc, calls, pop: () => listeners.forEach((f) => f()) };
  };

  it("#sky and #sky/<title> say the view; the grid has no hash", () => {
    expect(viewHash({ view: "grid", skyMode: "mine", focused: "a" })).toBe("");
    expect(viewHash({ view: "sky", skyMode: "all", focused: "a" })).toBe("#sky");
    expect(viewHash({ view: "sky", skyMode: "one", focused: "a b" })).toBe("#sky/a%20b");
    expect(parseViewHash("#sky/a%20b")).toEqual({ view: "sky", focus: "a b" });
    expect(parseViewHash("#flights")).toBeNull();
  });

  it("an old #flights/sky/<title> link opens the view on that session and is rewritten in place", () => {
    const w = fakeWin("#flights/sky/fix-login");
    const off = installSurfaceHash(w.win);
    const s = useUi.getState();
    expect([s.view, s.skyMode, s.focused, s.openDialog]).toEqual(["sky", "one", "fix-login", null]);
    expect(w.calls).toEqual(["replace /#sky/fix-login"]);
    off();
  });

  it("a switch replaces the entry (Back is for surfaces), a surface over the sky closes back onto #sky, Back to no hash is the grid", async () => {
    const w = fakeWin("");
    const off = installSurfaceHash(w.win);
    setView("sky");
    expect(w.calls).toEqual(["replace /#sky"]);
    useUi.getState().openDialogFor("settings", "accounts");
    expect(w.calls.at(-1)).toBe("push /#settings/accounts");
    useUi.getState().closeDialog();
    await Promise.resolve();
    // Stepped back over the surface's entry, then said the view again.
    expect(w.calls.slice(-2)).toEqual(["back", "replace /#sky"]);
    expect(useUi.getState().view).toBe("sky");
    // A link's #sky (Back / Forward between views) — and no hash is the grid.
    w.loc.hash = "";
    w.pop();
    expect(useUi.getState().view).toBe("grid");
    w.loc.hash = "#sky";
    w.pop();
    expect(useUi.getState().view).toBe("sky");
    off();
  });

  it("a remembered sky with no hash says so in the URL at load", () => {
    useUi.setState({ view: "sky" } as never);
    const w = fakeWin("");
    const off = installSurfaceHash(w.win);
    expect(w.calls).toEqual(["replace /#sky"]);
    off();
  });
});

describe("the layout store", () => {
  const mem = () => {
    const m = new Map<string, string>();
    return { getItem: (k: string) => m.get(k) ?? null, setItem: (k: string, v: string) => void m.set(k, v), m };
  };

  it("migrates mf_sky_pos once: every remembered spot becomes a pinned line", () => {
    const s = mem();
    s.m.set(sky.SKY_POS_KEY, JSON.stringify({ a: { x: 10, y: 20 }, b: { x: "junk" } }));
    expect(sky.loadSkyLayout(s)).toEqual({ a: { x: 10, y: 20, pinned: true } });
    expect(JSON.parse(s.m.get(sky.SKY_LAYOUT_KEY)!)).toEqual({ a: { x: 10, y: 20, pinned: true } });
    // The new record wins from then on.
    s.m.set(sky.SKY_LAYOUT_KEY, "{}");
    expect(sky.loadSkyLayout(s)).toEqual({});
  });

  it("Tidy clears the pins — all, or one line — and keeps sizes and open boxes", () => {
    const s = mem();
    const all = { a: { x: 1, y: 2, pinned: true, size: "wide" as const }, b: { x: 3, y: 4, pinned: true }, c: { open: true } };
    expect(sky.tidySkyLayout(all, undefined, s)).toEqual({ a: { size: "wide" }, c: { open: true } });
    expect(sky.tidySkyLayout(all, "b", s)).toEqual({ a: all.a, c: { open: true } });
    expect(sky.pinnedPositions(all)).toEqual({ a: { x: 1, y: 2 }, b: { x: 3, y: 4 } });
    // The host reads `{ pinned: false }` from a line as "Tidy this line".
    expect(code("components/flights/SkyView.tsx")).toContain("patch.pinned === false ? closeSkyWindows(tidySkyLayout(l, id), [id]) : patchSkyLine(l, id, patch)");
  });

  it("sizes cycle compact → normal → wide, and a line's layout fills the defaults", () => {
    expect([sky.cycleSize("compact"), sky.cycleSize("normal"), sky.cycleSize("wide"), sky.cycleSize(undefined)]).toEqual(["normal", "wide", "compact", "wide"]);
    expect(sky.lineLayout(undefined, { x: 5, y: 6 })).toEqual({ x: 5, y: 6, size: "normal", open: false, paneW: undefined, paneH: undefined, pinned: false });
    expect(sky.lineLayout({ size: "wide", open: true, paneW: 900 }, { x: 0, y: 0 })).toMatchObject({ size: "wide", open: true, paneW: 900 });
  });

  it("placeLines honours sizes: a wider, opened line takes more room and nothing overlaps", () => {
    const base = { w: 400, h: 160 };
    const wide = sky.estimateLine(base, { size: "wide" });
    const compact = sky.estimateLine(base, { size: "compact" });
    const open = sky.estimateLine(base, { open: true });
    expect(wide.w).toBeGreaterThan(base.w);
    expect(compact.w).toBeLessThan(base.w);
    expect(open.w).toBeGreaterThanOrEqual(sky.PANE_W);
    expect(open.h).toBeGreaterThan(base.h + sky.PANE_H);
    const lines = [
      { id: "a", ...open },
      { id: "b", ...wide },
      { id: "c", ...compact },
    ];
    const pos = sky.placeLines(lines, {}, { width: 1600, left: 0, top: 0 });
    expect(pos.b.x).toBeGreaterThanOrEqual(open.w + sky.SKY_GAP);
    const r = lines.map((l) => ({ ...pos[l.id], w: l.w, h: l.h }));
    for (let i = 0; i < r.length; i++)
      for (let j = i + 1; j < r.length; j++)
        expect(r[i].x < r[j].x + r[j].w && r[j].x < r[i].x + r[i].w && r[i].y < r[j].y + r[j].h && r[j].y < r[i].y + r[i].h).toBe(false);
  });

  it("measured sizes win over the estimate, and the Tidy button and T run it", () => {
    const v = code("components/flights/SkyView.tsx");
    // Round 18: the estimate is a chain's (its tiles at the pitch, its label).
    expect(v).toContain("...(measured[t] || estimateLine(chainSize(diagram, true), layout[t]))");
    expect(v).toContain("const saved: Record<string, Pos> = { ...autoPos, ...pinnedPositions(layout) };");
    expect(v).toContain("const fresh = assignSpots(existing, newcomers, area);");
    expect(v).toMatch(/className="fl-btn sky-tidy"[^>]*onClick=\{tidy\}/);
    expect(v).toContain('} else if (k === "t" || k === "T") {');
  });
});

describe("the transition", () => {
  const b = (x: number, w = 200) => ({ x, y: 10, w, h: 100 });

  it("flies only sessions on screen in both views, the focused one first, capped", () => {
    vi.stubGlobal("window", { innerWidth: 1200, innerHeight: 800 });
    const from = new Map([
      ["a", b(0)],
      ["b", b(300)],
      ["gone", b(600)],
      ["off", b(5000)],
    ]);
    const to = new Map([
      ["a", b(10)],
      ["b", b(310)],
      ["off", b(20)],
    ]);
    expect(flipPairs(from, to, "b").map((p) => p.title)).toEqual(["b", "a"]);
    expect(flipPairs(from, to, null, 1)).toHaveLength(1);
    vi.unstubAllGlobals();
    vi.stubGlobal("localStorage", {
      getItem: (k: string) => (storage.has(k) ? storage.get(k)! : null),
      setItem: (k: string, v: string) => void storage.set(k, String(v)),
      removeItem: (k: string) => void storage.delete(k),
      clear: () => storage.clear(),
    });
  });

  it("honours prefers-reduced-motion and fades the incoming view", () => {
    const vm = code("lib/viewMode.ts");
    expect(vm).toContain('window.matchMedia?.("(prefers-reduced-motion: reduce)").matches');
    expect(vm).toContain("const from = calm ? new Map<string, Box>() : toSky ? paneRects() : _sky?.lineRects() || new Map<string, Box>();");
    const sheet = css("components/flights/Sky.css");
    expect(sheet).toMatch(/body\.view-switch #grid,\s*body\.view-switch #sky-view \{[^}]*animation: view-in/);
    expect(sheet).toMatch(/\.view-ghost \{[^}]*position: fixed;[^}]*pointer-events: none;/);
  });
});

describe("round 12: the intake lane on the sky, the tools' presses", () => {
  const v = code("components/flights/SkyView.tsx");

  it("the lane is placed like a line, first; no line keeps a spot before it has one", () => {
    expect(v).toContain("const items = [{ id: INTAKE_KEY, ...intakeBox }, ...(paletteOn ? [{ id: PALETTE_KEY, ...paletteBox }] : []), ...lines];");
    expect(v).toContain("const intakeReady = !!autoPos[INTAKE_KEY] || !!layout[INTAKE_KEY]?.pinned || !!measured[INTAKE_KEY] || waited;");
    // Its room is never under the folded lane's width (unfolding the rail
    // never runs into a line).
    expect(v).toContain("return { w: Math.max(b.w, INTAKE_W), h: b.h };");
    // This session (one line) has no lane.
    expect(v).toContain('const intakeOn = mode !== "one";');
  });

  it("it drags on the line drag's own path; a click on its head focuses no session", () => {
    expect(v).toContain('if (e.button !== 0 || gestureFor(liveTool, "line", { space: space.current }) !== "move-line") return;');
    expect(v).toMatch(/if \(!sel\.lines\.has\(id\)\) clearSel\(\);\s*if \(id === INTAKE_KEY \|\| id === PALETTE_KEY\) return;/);
    expect(v).toContain('out.lines.push({ id: INTAKE_KEY, rect: lane, heads });');
  });

  it("click mode: the sky takes the press in its capture phase, pans once it moves, and a still press is a click", () => {
    expect(v).toMatch(/const t = space\.current \? "click" : tool;\s*if \(t === "drag"\) return;/);
    expect(v).toContain("const g = gestureFor(t, skyTargetOf(e.target as Element, ground), { shift: e.shiftKey, space: space.current, touch: e.pointerType === \"touch\" });");
    expect(v).toMatch(/if \(Math\.hypot\(e\.clientX - pr\.sx, e\.clientY - pr\.sy\) < MARQUEE_MIN\) return;\s*press\.current = null;/);
    expect(v).toMatch(/if \(e\.type !== "pointercancel"\) clickOn\(pr\.el\);/);
  });
});

describe("round 12: workers live inside their lead's line", () => {
  it("every view draws a line's family; the estimate does too", () => {
    const v = code("components/flights/SkyView.tsx");
    expect(v).toContain("const diagram = sessionDiagram(rows, t, { flights, kinds, family: true });");
    expect(code("components/flights/SkyLine.tsx")).toContain("useSessionLine(p.title, src, { family: true,");
    expect(sky.SKY_MODES.find((m) => m.id === "all")?.title).toContain("inside its lead's route");
  });
});

describe("round 13: nothing focused, place don't tack, the floating palette", () => {
  const v = code("components/flights/SkyView.tsx");

  it("selectSession(null) selects nothing: no session is focused, in either view", async () => {
    const { selectSession } = await import("../lib/sessionActions");
    useUi.setState({ focused: "fix-login" } as never);
    selectSession(null);
    expect(useUi.getState().focused).toBeNull();
  });

  it("a click on empty sky (both tools) and Esc with nothing else to close leave nothing focused", () => {
    expect(v).toContain("if (useUi.getState().focused) selectSession(null);");
    // Drag mode: the still marquee press on empty sky.
    expect(v).toMatch(/else if \(!m\.add\) \{\s*clearSel\(\);\s*unfocus\(\);\s*\}/);
    // Click mode: a still press on nothing of the sky's.
    expect(v).toMatch(/if \(!id\) \{\s*vpRef\.current\?\.focus\(\{ preventScroll: true \}\);\s*unfocus\(\);\s*\}/);
    // Esc: after the cards, the selection and an open box.
    expect(v).toMatch(/k === "Escape" && \(focused \|\| groundFocus\) && !marquee\.current\) \{\s*handled\(\);\s*unfocus\(\);/);
  });

  it("the sky never picks a line by itself: not a view switch, not a new session, not an intake item flown", () => {
    expect(v).toContain("const setMode = (m: SkyMode) => setSkyMode(m);");
    expect(v).not.toContain("focusOn(titles[0]);\n");
    expect(v).not.toMatch(/setNewAt\(null\);\s*focusOn\(title\);/);
    expect(v).toContain("onFly={flashLine}");
    // A drop on empty sky makes its line, unfocused.
    expect(v).not.toMatch(/pinned: true \}\)\);\s*setGroundFocus\(id\);/);
    // This session with nothing focused says what to do.
    expect(v).toContain('"Pick a route to show it alone."');
    // Enter opens only the focused line's box.
    expect(v).toContain('} else if (k === "Enter" && focusLine) {');
  });

  it("a palette click places its block on the Sky, near the palette; chips never tack onto the focused line", () => {
    expect(v).toContain("onPlace={(d) => void placeGrounded(d, \"shelf\")}");
    expect(v).toContain("onPlace={(d) => void placeGrounded(d, \"palette\")}");
    expect(v).toContain("const want = pal ? { x: pal.x + pal.w + PLACE_GAP, y: pal.y } : { x: view.x + view.w / 2 - size.w / 2, y: view.y + view.h - size.h };");
    expect(v).toContain("return spotNear(want, taken, size, view);");
    expect(v).toContain("const at = spotRef.current(f, near);");
    // Placed at its estimate, then once more at its measured size (never onto
    // itself: its own rect is not `taken`).
    expect(v).toContain("placing.current.set(groundedKey(f.id), near);");
    expect(v).toMatch(/if \(!measured\[key\]\) continue;\s*placing\.current\.delete\(key\);/);
    expect(v).toContain(".filter((id) => id !== key), INTAKE_KEY, PALETTE_KEY].map(rectOf)");
    // The old fallbacks are gone: no tack or Add onto `focusLine`, no picker for them.
    expect(v).not.toContain("title || focusLine");
    expect(v).not.toContain("tackPick");
    expect(v).not.toContain("flyPick");
  });

  it("the floating palette is a sky object: mf_sky_palette, placed after the lane, dragged by its head, sized by its corner", () => {
    expect(v).toContain('readPref(PALETTE_MODE_KEY) === "float" ? "float" : "shelf"');
    expect(v).toContain("writePref(PALETTE_MODE_KEY, m);");
    expect(v).toContain('const paletteOn = paletteMode === "float" && !narrow;');
    expect(v).toMatch(/<SkyPalette[\s\S]*?zoom=\{cam\.z\}[\s\S]*?onHeadDown=\{\(e\) => onDragStart\(PALETTE_KEY, e\)\}[\s\S]*?onResize=\{\(w, h\) => onLayout\(PALETTE_KEY, \{ w, h \}\)\}[\s\S]*?onDock=\{\(\) => setPaletteMode\("shelf"\)\}[\s\S]*?onSession=\{newHere\}/);
    expect(v).toContain('onFloat={narrow ? undefined : () => setPaletteMode("float")}');
    // Floating, the bottom shelf gives way.
    expect(v).toMatch(/\{!paletteOn && \(\s*<StopShelf/);
    // Its blocks' drags are read as the shelf's are.
    expect(v).toMatch(/className=\{"sky-palette-slot"[\s\S]*?onDragStart=\{onShelfDragStart\}\s*onDragEnd=\{onShelfDragEnd\}/);
    // A press on it is its own (never a pan or a marquee), and Delete there is not the sky's.
    expect(v).toContain(".sky-new, .sky-palette-slot\";");
    expect(v).toContain(".sky-selbar, .sky-palette-slot, [role=menu]");
    // The marquee never takes it: selectables lists the lines and the lane only.
    expect(v).not.toContain("out.lines.push({ id: PALETTE_KEY");
  });
});

describe("round 14: Ctrl+Z, removals that don't ask, All blocks… on the Sky", () => {
  const v = code("components/flights/SkyView.tsx");
  const line = code("components/flights/SkyLine.tsx");
  const grounded = code("components/flights/GroundedLine.tsx");
  const hook = code("components/flights/useSessionLine.tsx");

  it("Ctrl+Z / ⌘Z in the window's capture phase pops the sky's stack — never in a field, a pane, a menu or a dialog, nor mid-drag; Ctrl+Shift+Z redoes (round 17)", () => {
    expect(v).toContain("if (undoKey(e)) return undoNow(e);");
    expect(v).toContain("if (redoKey(e)) return undoNow(e, true);");
    expect(v).toContain('const redoKey = (e: KeyboardEvent) => (e.ctrlKey || e.metaKey) && !e.altKey && e.shiftKey && (e.key === "z" || e.key === "Z");');
    expect(v).toContain('toast("Redid: " + again.label);');
    expect(v).toContain('toast("Nothing to redo");');
    expect(v).toContain('const undoKey = (e: KeyboardEvent) => (e.ctrlKey || e.metaKey) && !e.altKey && !e.shiftKey && (e.key === "z" || e.key === "Z");');
    expect(v).toContain("if (typing(el) || overlayOpen()) return;");
    expect(v).toContain('el.closest(".pane, .skl-card, [role=menu], [role=dialog], [role=alertdialog]")');
    expect(v).toContain("if (drag || marquee.current || pan.current) return;");
    expect(v).toContain('toast("Undid: " + entry.label);');
    expect(v).toContain('toast("Nothing to undo"');
    // Ctrl+Y stays the view toggle.
    expect(v).not.toMatch(/key === "y"/i);
  });

  it("a grounded line goes at once through the host: one entry, its toast takes it, Undo makes it again where it sat", () => {
    expect(v).toContain("onRemove={() => removeGrounded(id)}");
    // Round 17: the entry carries redo (the remade line, by its new id, goes again).
    expect(v).toMatch(/const entry = pushUndo\(\{\s*label: "Remove “" \+ shown \+ "”",\s*undo: async \(\) => \{\s*const g = await reground\(steps, name, was, ed\);\s*if \(g\) cur = g\.id;[\s\S]*?redo: \(\) => deleteFlight\(cur\),/);
    expect(v).toContain('toast("Removed “" + shown + "”", { action: undoAction(entry, ');
    expect(grounded).not.toContain("InlineConfirm");
  });

  it("a session line closes at once when idle (Undo reopens it from Recently closed), asks while its agent works or asks", () => {
    expect(line).toContain('if (removalNeedsAsk({ kind: "session", row: p.rows.find((r) => r.title === t) })) setClosing(t);');
    expect(line).toContain("onSelect: () => askClose(p.title) },");
    expect(line).toContain("if (!sessionOpen(title)) pushCloseUndo([title]);");
    expect(line).toContain("for (const t of [...titles].reverse()) if (!sessionOpen(t)) await undoLastClose();");
    expect(line).toContain("valid: () => titles.some((t) => !sessionOpen(t)),");
    expect(line).toContain('"Its agent " + runningWord(');
  });

  it("a stop: Undo on the toast and on Ctrl+Z is the same entry; the stop a running flight is at asks first, on the sky only", () => {
    expect(hook.match(/const entry = pushUndo\(\{/g)?.length).toBe(2);
    expect(hook.match(/onClick: \(\) => \{\s*if \(takeUndo\(entry\)\) void entry\.undo\(\);\s*\}/g)?.length).toBe(2);
    expect(hook).toContain('if (opts.askRunning && removalNeedsAsk({ kind: "stops", indexes: nodeIndexes(n), flight })) setRemoveAsk(n);');
    expect(hook).toContain("onRemove: flight ? (n) => requestRemove(own(n)) : landedId ? () => void clearLanded() : undefined,");
    expect(line).toContain("askRunning: true");
    // Delete from the sky reaches the stop the flight is at (not editable), which then asks.
    expect(line).toContain("nodeIndexes(n).includes(line.flight.cursor);");
    expect(line).toContain("if (!(removableBox(n) || here) || !gc.onRemove) return false;");
    expect(line).toContain("{line.removeAsk}");
    // The pane's Flight tab doesn't ask (it draws no question).
    expect(code("components/flights/SessionFlight.tsx")).not.toContain("askRunning");
  });

  it("the selection bar gets what it needs to ask and to put things back", () => {
    expect(v).toContain("rowOf={(id) => rows.find((r) => r.title === id)}");
    expect(v).toContain("restoreGrounded={reground}");
  });

  it("All blocks… floats the palette LARGE on the Sky (a sky object, not a modal); × and Esc dock it", () => {
    expect(v).toContain("onCatalog={narrow ? undefined : openCatalog}");
    expect(v).toContain("const f = catalogFrame(view, screen, c.z, stored);");
    expect(v).toContain("onLayout(PALETTE_KEY, { x: f.x, y: f.y, pinned: true });");
    expect(v).toContain('setPaletteMode("float");');
    expect(v).toContain('onClose={() => setPaletteMode("shelf")}');
    expect(v).toContain("focusFilter={!!catalogOpen || filterFocus}");
    expect(v).toContain("width={paletteFrameNow?.w ?? (catalogOpen?.w || undefined)}");
    expect(v).toMatch(/k === "Escape" && catalogOpen && paletteOn\) \{\s*handled\(\);\s*setPaletteMode\("shelf"\);/);
    // Docking forgets the large frame (it was for that opening only).
    expect(v).toMatch(/if \(m === "shelf"\) \{\s*setCatalogOpen\(null\);/);
  });
});

describe("switching session in the sky never opens its box on the terminal", () => {
  it("the pick effect moves the camera and flashes; it never opens a box", () => {
    const src = code("components/flights/SkyView.tsx");
    expect(src).not.toContain("openedFor");
    // Only Enter (asked for) opens the focused box.
    expect(src.match(/onLayout\(focusLine, \{ open: true \}\)/g)?.length).toBe(1);
    expect(src).toContain('} else if (k === "Enter" && focusLine) {');
  });
  it("This session with nothing focused shows the last session you were on", () => {
    const src = code("components/flights/SkyView.tsx");
    expect(src).toContain("return mru.find((t) => shown.has(t))");
    expect(src).toContain("skyLineTitles(rows, mode, { query, focus: oneFocus })");
  });
});
