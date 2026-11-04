/** Round 20 A — a session is a FRAME, the agent is a tile (docs/flights.md
 * "Round 20" A, docs/flights-r20-frames.md): the frame round a session's
 * chain (its bar: name · branch · state · ⋯, double-click renames), the
 * agent as the chain's first tile (its name, what it is doing, its chip, its
 * menu), sub-sessions as small frames hung from the agent (their own agent,
 * their own chain as mini stops), the Flight tab drawing the same, and the
 * tour's wording. */

/// <reference types="vite/client" />
import { createElement, type ReactElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { afterEach, describe, expect, it, vi } from "vitest";
import { code, css } from "./sourceText";
import type { Instance } from "../api/types";
import type { StepKindDef } from "../lib/flights";

vi.mock("../api/client", () => ({ api: vi.fn(() => new Promise(() => {})), instApi: vi.fn(() => new Promise(() => {})), ApiError: class extends Error {} }));
vi.mock("../lib/toast", () => ({ toast: vi.fn() }));

const { QueryClient, QueryClientProvider } = await import("@tanstack/react-query");
const { useUi } = await import("../state/store");
const { sessionDiagram } = await import("../lib/flightDiagram");
const { HEAD_ID, chainColor, chainGraph } = await import("../lib/skyGraph");
const { SkyLine } = await import("../components/flights/SkyLine");
const C = await import("../components/flights/SkyChain");
const { agentMenuItems } = await import("../components/flights/useChainMenu");
const { TOURS } = await import("../lib/tours");

(globalThis as Record<string, unknown>).document ??= {
  hidden: false,
  addEventListener: () => {},
  removeEventListener: () => {},
  getElementById: () => null,
  querySelector: () => null,
};

const KINDS: StepKindDef[] = [{ kind: "agent", label: "Agent", actor: "robot", summary: "The agent works", fields: [], default_title: "Agent works" }];

const rowOf = (title: string, over: Record<string, unknown> = {}) =>
  ({
    title,
    status: "running",
    activity: "idle",
    stage: "agent",
    provider: "claude",
    program: "claude",
    task_headline: "Do " + title,
    last_prompt: "Make the login page stop looping",
    parent: "",
    branch: "feature/" + title,
    flight: null,
    autopilot: null,
    ...over,
  }) as unknown as Instance;

const flying = (cursor: number, total: number) => ({ id: "f_" + cursor + total, plan_name: "Fix", cursor, total, state: "running", step: { id: "s" + cursor, kind: "agent", title: "Write it", state: "running", actor: "robot" } });

function withClient(el: ReactElement) {
  const c = new QueryClient({ defaultOptions: { queries: { retry: false, enabled: false } } });
  c.setQueryData(["flights"], []);
  c.setQueryData(["flight-kinds"], KINDS);
  c.setQueryData(["config"], { caps: { git: true, tailscale: false, ticketing: false, github: false } });
  return renderToStaticMarkup(createElement(QueryClientProvider, { client: c }, el));
}

afterEach(() => {
  Object.assign(useUi.getInitialState(), useUi.getState());
});

const line = (rows: Instance[], over: Record<string, unknown> = {}) =>
  withClient(
    createElement(SkyLine, {
      title: "me",
      rows,
      plans: [],
      layout: { x: 0, y: 0, size: "normal", open: false },
      onLayout: () => {},
      focused: false,
      onFocus: () => {},
      zoom: 1,
      ...over,
    })
  );

describe("the agent", () => {
  it("is named by its provider, short — else just Agent", () => {
    expect(C.agentTitle("claude")).toBe("Claude");
    expect(C.agentTitle("codex")).toBe("Codex");
    expect(C.agentTitle("opencode")).toBe("OpenCode");
    expect(C.agentTitle("mybot")).toBe("Mybot");
    expect(C.agentTitle("")).toBe("Agent");
    expect(C.agentTitle(null)).toBe("Agent");
  });

  it("says what it is doing now: the task headline, else the create task, else the last prompt", () => {
    expect(C.agentDoing({ task_headline: "Fix the pin bar\nmore", last_prompt: "yes", task: "t" })).toBe("Fix the pin bar");
    expect(C.agentDoing({ task_headline: "", last_prompt: "go on", task: "Build the thing" })).toBe("Build the thing");
    expect(C.agentDoing({ task_headline: null, last_prompt: "Retry the push", task: "" } as never)).toBe("Retry the push");
    expect(C.agentDoing(null)).toBe("");
  });

  it("its menu: Open terminal, Send a prompt…, Stop — Resume while paused", () => {
    const calls: string[] = [];
    const items = agentMenuItems(rowOf("me"), { openTerminal: () => calls.push("open"), compose: () => calls.push("compose") });
    expect(items.map((i) => i.label)).toEqual(["Open terminal", "Send a prompt…", "Stop"]);
    expect(items[2].danger).toBe(true);
    items[0].onSelect?.();
    items[1].onSelect?.();
    expect(calls).toEqual(["open", "compose"]);
    const opened = agentMenuItems(rowOf("me"), { openTerminal: () => {}, compose: () => {}, opened: true });
    expect(opened[0].label).toBe("Fold the terminal");
    const paused = agentMenuItems(rowOf("me", { status: "paused" }), { openTerminal: () => {}, compose: () => {} });
    expect(paused.map((i) => i.label)).toEqual(["Open terminal", "Send a prompt…", "Resume"]);
    expect(paused[1].disabled).toBe(true);
    expect(agentMenuItems(null, { openTerminal: () => {}, compose: () => {} })).toEqual([]);
  });

  it("Send a prompt… posts to the session's /send, Stop pauses (the agent stops, the session stays)", () => {
    const src = code("components/flights/useChainMenu.tsx");
    expect(src).toContain('await api("/api/instances/" + encodeURIComponent(title) + "/send", { method: "POST", json: { text: t, dialog_safe: true } });');
    expect(src).toContain("onSelect: () => void pauseSession(row.title)");
    expect(src).toContain("onSelect: () => void resumeSession(row.title)");
    // Electron has no window.prompt: the composer is the app's own.
    expect(src).toContain('className="sky-agent-prompt"');
    expect(src).not.toMatch(/window\.prompt|[^.\w]prompt\(/);
  });
});

describe("sub-sessions are small frames", () => {
  const rows = [rowOf("me", { activity: "working", flight: flying(1, 3) }), rowOf("kid1", { parent: "me", provider: "codex", flight: flying(1, 2) }), rowOf("kid2", { parent: "me" })];

  it("each one's frame: its agent's name and its own chain's stops", () => {
    const f = C.subFramesOf(rows, "me", [], KINDS);
    expect(Object.keys(f).sort()).toEqual(["kid1", "kid2"]);
    expect(f.kid1.agent).toBe("Codex");
    expect(f.kid1.stops.map((s) => s.state)).toEqual(["done", "current"]);
    expect(f.kid2).toEqual({ agent: "Claude", stops: [] });
  });

  it("the frame's box: the agent tile's room, wider and taller with a chain strip", () => {
    expect(C.subFrameSize(null)).toEqual({ w: C.SUB_W, h: C.SUB_H });
    const two = C.subFrameSize({ agent: "A", stops: [{ id: "a", title: "a", state: "done", icon: "agent" }, { id: "b", title: "b", state: "current", icon: "agent" }] });
    expect(two).toEqual({ w: C.SUB_W, h: C.SUB_H + C.MINI_STRIP });
    const many = C.subFrameSize({ agent: "A", stops: Array.from({ length: 20 }, (_, i) => ({ id: "s" + i, title: "s", state: "pending", icon: "agent" })) });
    // Capped at MINI_MAX squares and a "+n".
    expect(many.w).toBe(Math.max(C.SUB_W, 2 * C.SUB_PAD + (C.MINI_MAX + 1) * C.MINI + C.MINI_MAX * C.MINI_GAP));
  });

  it("every sub-session node gets its box — an open card's when open", () => {
    const d = sessionDiagram(rows, "me", { kinds: KINDS });
    const f = C.subFramesOf(rows, "me", [], KINDS);
    const boxes = C.subFrameBoxes(d.nodes, f, { kid2: { w: 720, h: 490 } });
    expect(boxes["child:kid1"]).toEqual(C.subFrameSize(f.kid1));
    expect(boxes["child:kid2"]).toEqual({ w: 720, h: 490 });
    expect(Object.keys(boxes).every((k) => k.startsWith("child:"))).toBe(true);
  });

  it("they hang from the AGENT tile, and the line they hung across is drawn straight again", () => {
    const d = sessionDiagram(rows, "me", { kinds: KINDS });
    const kid = d.nodes.find((n) => n.id === "child:kid1")!;
    // lib/flightDiagram hangs them at the flight's current stop …
    expect(kid.branch!.from).not.toBe("session:me");
    const hung = C.hangFromAgent(d);
    // … the frames hang them from the session (the head: its agent).
    expect(hung.nodes.find((n) => n.id === "child:kid1")!.branch!.from).toBe("session:me");
    expect(hung.edges.filter((e) => e.kind === "branch").every((e) => e.from === "session:me")).toBe(true);
    expect(hung.edges.some((e) => e.from === kid.branch!.from && e.to === kid.branch!.to && (e.kind === "line" || e.kind === "add"))).toBe(true);
    // The diagram itself is untouched; nothing to do without below-children.
    expect(kid.branch!.from).not.toBe("session:me");
    const lone = sessionDiagram([rowOf("me")], "me", {});
    expect(C.hangFromAgent(lone)).toBe(lone);
  });

  it("draws as a frame: its name on the bar, its agent tile (no ports), its chip, its mini chain, a dashed bus", () => {
    const d = C.hangFromAgent(sessionDiagram(rows, "me", { kinds: KINDS }));
    const f = C.subFramesOf(rows, "me", [], KINDS);
    const me = d.nodes.find((n) => n.kind === "session")!;
    const g = chainGraph(d, { head: { kind: "session", title: "Claude", sub: "Do me", session: "me", color: chainColor("me"), node: me }, key: "me", flightId: "f_13", big: C.subFrameBoxes(d.nodes, f) });
    const html = renderToStaticMarkup(createElement(C.SkyChain, { graph: g, zoom: 1, subFrames: f }));
    expect(html).toMatch(/class="sky-node sky-node-child[^"]*sky-subframe/);
    expect(html).toContain('<div class="sky-subframe-bar" title="kid1"><span class="sky-sub-dot" aria-hidden="true"></span>kid1</div>');
    expect(html).toContain('class="sky-node-title">Codex<');
    expect(html).toContain("--sky-sub-color:" + chainColor("kid1"));
    expect(html).toContain('class="sky-mini-chain"');
    expect((html.match(/class="sky-mini st-/g) || []).length).toBe(2);
    expect(html).toMatch(/aria-label="Sub-session kid1 — its agent \(Codex\)/);
    // The bus from the agent (head) into each frame, dashed.
    expect(html).toMatch(/class="sky-wire sky-wire-fan sky-wire-bus"[^>]*data-from="head" data-to="child:kid1"/);
    // A sub-frame's tile carries no port.
    const kidTile = html.slice(html.indexOf('data-node="child:kid1"'), html.indexOf('data-node="child:kid2"'));
    expect(kidTile).not.toContain("sky-port");
    // Its box is the frame's.
    const node = g.nodes.find((n) => n.id === "child:kid1")!;
    expect({ w: node.w, h: node.h }).toEqual(C.subFrameSize(f.kid1));
  });
});

describe("a session is a frame", () => {
  it("the frame box, its bar (name · branch · state · ⋯), the agent tile first inside", () => {
    const html = line([rowOf("me", { activity: "working" }), rowOf("kid1", { parent: "me" })]);
    expect(html).toContain('class="skl skl-chainline skl-frame"');
    expect(html).toContain('<div class="skl-frame-box" aria-hidden="true" style="left:0;top:0"></div>');
    expect(html).toContain('class="skl-bar skl-label skl-frame-bar" data-tour="sky-line-bar"');
    expect(html).toContain('<span class="skl-name">me</span>');
    expect(html).toContain('<span class="skl-branch" title="Branch feature/me">feature/me</span>');
    expect(html).toMatch(/class="skl-chip fl-box-chip skl-state-chip tone-working"[^>]*>Working</);
    expect(html).toContain('aria-label="Actions for me"');
    // The agent heads the chain: its name, what it is doing, its chip.
    expect(html).toMatch(/class="sky-node sky-head sky-head-session sky-agent/);
    expect(html).toMatch(/class="sky-node-title">Claude</);
    expect(html).toContain('class="sky-node-sub">Do me<');
    expect(html).toContain("sky-head-chip");
    // Its sub-session: a small frame.
    expect(html).toContain('class="sky-subframe-bar" title="kid1"');
  });

  it("an unknown provider's agent is just “Agent”", () => {
    const html = line([rowOf("me", { provider: "", program: "" })]);
    expect(html).toMatch(/class="sky-node-title">Agent</);
  });

  it("grows to hold a tile dragged left of / above the chain's origin", () => {
    // A ready flight (round 19) whose first stop sits at (-200, -60).
    const fl = { id: "f_r", plan_name: "Later", cursor: 0, total: 1, state: "ready", step: { id: "s1", kind: "agent", title: "Write it", state: "pending", actor: "robot" } };
    const flights = [{ id: "f_r", plan_id: "p", plan_name: "Later", session: "me", state: "ready", cursor: 0, start_on: "prompt", steps: [{ id: "s1", kind: "agent", title: "Write it", config: {}, on_fail: "halt", pos: { x: -200, y: -60 } }], step_states: {}, history: [], started: 1, updated: 1 }];
    const c = new QueryClient({ defaultOptions: { queries: { retry: false, enabled: false } } });
    c.setQueryData(["flights"], flights);
    c.setQueryData(["flight-kinds"], KINDS);
    c.setQueryData(["config"], { caps: { git: true } });
    const html = renderToStaticMarkup(
      createElement(
        QueryClientProvider,
        { client: c },
        createElement(SkyLine, { title: "me", rows: [rowOf("me", { flight: fl })], plans: [], layout: { x: 0, y: 0, size: "normal", open: false }, onLayout: () => {}, focused: false, onFocus: () => {}, zoom: 1 })
      )
    );
    expect(html).toContain('class="skl-frame-box" aria-hidden="true" style="left:-200px;top:-60px"');
    expect(html).toMatch(/class="skl-bar skl-label skl-frame-bar"[^>]*style="margin-left:-200px;transform:translateY\(-60px\)"/);
    // Round 19's Ready + ▶ Run: round 21 moved it to the frame's bar, once.
    expect(html.match(/class="sky-ready"/g)?.length).toBe(1);
    expect(html).toMatch(/class="skl-bar skl-label skl-frame-bar"[^]*class="sky-ready"[^]*class="skl-menu"/);
  });

  it("double-click on the bar renames (the session itself; a taken name is refused)", () => {
    const src = code("components/flights/SkyLine.tsx");
    expect(src).toContain("onDoubleClick={(e) => {");
    expect(src).toContain("startRename();");
    expect(src).toContain("const taken = nameTakenBy(p.title, next);");
    expect(src).toContain("void renameSession(p.title, next);");
    expect(src).not.toContain("setAlias(");
    expect(src).toContain('className="fl-input skl-name-input"');
  });

  it("the agent's (and a sub-session agent's) right-click is the agent menu; a stop's stays the stop's", () => {
    const src = code("components/flights/SkyLine.tsx");
    expect(src).toContain('const t = n.kind === "session" ? p.title : (n.kind === "child" || n.kind === "branch") && n.session ? n.session : "";');
    expect(src).toContain("if (t) agentMenu.open(t, at, agentItems(t, at));");
    expect(src).toContain("else chainMenu.onMenu(n, at);");
    // The session's own ⋯ follows the agent's items, under a label line.
    expect(src).toContain('{ key: "s-note", label: "Session “" + name + "”", title: "The session round this agent", note: true }');
    expect(src).toContain("onMenu={onMenu}");
  });

  it("keeps the tour hooks: the agent tile is sky-session-box, the bar sky-line-bar", () => {
    const src = code("components/flights/SkyLine.tsx");
    expect(src).toContain('useTourHooks(wrapRef, { ".sky-head": "sky-session-box", ".sky-node:not(.sky-head):not(.sky-subframe)": "sky-stop" });');
    expect(src).toContain('data-tour="sky-line-bar"');
  });

  it("a stops-only chain has no frame; dropped on a frame, the frame lights", () => {
    expect(code("components/flights/GroundedLine.tsx")).not.toContain("skl-frame");
    const sheet = css("components/flights/SkyLine.css");
    expect(sheet).toMatch(/\.skl-chainline\.skl-frame\.is-merge-target > \.skl-frame-box,\s*\.skl-chainline\.skl-frame\.is-drop > \.skl-frame-box \{/);
    expect(sheet).toMatch(/\.skl-frame-box \{[^}]*pointer-events: none;/);
  });

  it("an open card hides only ITS chain's head — the card's Flight tab draws its own agent", () => {
    const sheet = css("components/flights/SkyLine.css");
    expect(sheet).toContain(".skl-chain-wrap.has-card > .sky-chain > .sky-head > :not(.sky-tile),");
    expect(sheet).not.toMatch(/\.skl-chain-wrap\.has-card \.sky-head/);
  });
});

describe("the Flight tab draws the same", () => {
  it("a frame (bar: name · branch · state · where) round the chain, the agent first, sub-session frames", () => {
    const src = code("components/flights/FlightChain.tsx");
    expect(src).toContain('<div className="fl-chain-frame" style={{ ["--sky-chain-color" as string]: head.color }}>');
    expect(src).toContain('<span className="fl-chain-frame-name">{windowName(title)}</span>');
    expect(src).toContain('() => ({ kind: "session", title: agent, sub: headSub, session: title, color: chainColor(title), node: sessionNode })');
    expect(src).toContain("chainGraph(hangFromAgent(moves.diagram), { head, key: title, flightId, big })");
    expect(src).toContain("subFrames={subFrames}");
    // Its agent's terminal is the pane's Agent tab.
    expect(src).toContain('useUi.getState().setLastTab(title, "agent");');
    expect(css("components/flights/Flights.css")).toMatch(/\.fl-chain-frame \{[^}]*border-radius: 14px;/);
  });
});

// Vision round (docs/vocabulary.md): a session has a ROUTE, drawn in a soft
// outline — "chain" and "frame" are retired copy words.
describe("the tour and the guide say route and agent", () => {
  it("the Sky tour", () => {
    const steps = TOURS.sky.steps;
    const agent = steps.find((s) => s.target.startsWith('[data-tour="sky-session-box"]'))!;
    expect(agent.title).toBe("Each session's route");
    expect(agent.body).toContain("The agent comes first, then the stops its work goes through");
  });

  it("the guide", () => {
    const g = code("components/flights/SkyGuide.tsx");
    expect(g).toContain("Each session draws as a soft outline in its own colour round its <b>route</b>");
    expect(g).toContain("<b>Open terminal</b>, <b>Send a prompt…</b> and <b>Stop</b>");
    expect(g).toContain("A <b>worker</b> — a session this session started — is a small outline of its own");
    expect(g).not.toMatch(/\bchain\b|\bframe\b/);
    expect(g).not.toContain("sub-session");
    expect(g).not.toContain("session tile");
  });
});

it("the head id stays the graph's (the wiring and the bird read it)", () => {
  expect(HEAD_ID).toBe("head");
});
