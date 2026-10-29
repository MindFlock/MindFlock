/** Asking for MindFlock workers without typing it (lib/workersRequest): the
 * request text and its fallback, typing it into the terminal, the Flock
 * toggle, and the surfaces that reach them — the Alt+G / Alt+Shift+G keys,
 * the pane's 🐦 Flock control, the row menu, the palette and Settings →
 * General. */

import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import type { Config, Instance } from "../api/types";

const term = { paste: vi.fn(), focus: vi.fn() };
const handle = { term, send: vi.fn(() => true), state: "connected" };
let haveTerm = true;
vi.mock("../lib/terminals", async (orig) => ({
  ...(await orig<typeof import("../lib/terminals")>()),
  peekTerm: () => (haveTerm ? handle : undefined),
}));
const instApi = vi.fn();
vi.mock("../api/client", async (orig) => ({
  ...(await orig<typeof import("../api/client")>()),
  instApi: (...a: unknown[]) => instApi(...a),
}));
const toast = vi.fn();
vi.mock("../lib/toast", async (orig) => ({ ...(await orig<typeof import("../lib/toast")>()), toast: (...a: unknown[]) => toast(...a) }));

const W = await import("../lib/workersRequest");
const { queryClient } = await import("../state/queries");
const { KEYMAP, comboProblem, sameCombo, workersKeyAllowed } = await import("../lib/keymap");
const { actionCtx, actionLabel, SESSION_ACTIONS } = await import("../lib/sessionActionTable");
const { sessionMenu } = await import("../lib/sessionMenu");
const { useUi } = await import("../state/store");

import PANE_SRC from "../components/grid/Pane.tsx?raw";
import PANE_CSS from "../components/grid/Pane.css?raw";
import PALETTE_SRC from "../components/palette/CommandPalette.tsx?raw";
import GENERAL_SRC from "../components/settings/screens/General.tsx?raw";
import TYPES_SRC from "../api/types.ts?raw";

const row = (o: Partial<Instance> = {}) => ({ title: "s", status: "ready", ...o }) as Instance;
const CAPS = { git: true, tailscale: true, ticketing: true };

beforeEach(() => {
  haveTerm = true;
  handle.state = "connected";
  term.paste.mockClear();
  term.focus.mockClear();
  handle.send.mockClear();
  instApi.mockReset();
  toast.mockClear();
  queryClient.setQueryData(["config"], undefined);
  queryClient.setQueryData(["instances"], [row()]);
});
afterEach(() => {
  useUi.setState({ focused: null } as never);
});

describe("the request text", () => {
  it("is the server's when it sends one", () => {
    expect(W.workersRequestText({ flock: { request: "  Fan it out.  ", model: "m" } })).toBe("Fan it out.");
  });

  it("falls back to the default, model filled in, on an older server", () => {
    const t = W.workersRequestText(undefined);
    expect(t).toContain("spawn as many MindFlock agents as the work needs (model claude-opus-5-5)");
    expect(t).not.toContain("{model}");
    expect(t.startsWith("Use MindFlock workers for this:")).toBe(true);
    expect(t.endsWith("Do the small connecting pieces yourself.")).toBe(true);
    // An empty request with a model: the default with that model.
    expect(W.workersRequestText({ flock: { request: "", model: "claude-sonnet-5-5" } })).toContain("(model claude-sonnet-5-5)");
  });

  it("fills every {model}, and a blank model is the default", () => {
    expect(W.fillWorkersTemplate("a {model} b {model}", "x")).toBe("a x b x");
    expect(W.fillWorkersTemplate("{model}", "  ")).toBe(W.DEFAULT_WORKERS_MODEL);
    expect(W.DEFAULT_WORKERS_MODEL).toBe("claude-opus-5-5");
  });
});

describe("the words", () => {
  it("the control, its tooltip and the toggle's label", () => {
    expect(W.flockChipLabel()).toBe("🐦 Flock");
    const tip = W.flockTooltip(false);
    expect(tip).toContain("Workers on every prompt — the request is added to everything you send here");
    expect(tip).toContain("(Alt+Shift+G to toggle; Alt+G to add it once)");
    expect(W.flockTooltip(true, ["F7", "F8"])).toContain("(F7 to toggle; F8 to add it once)");
    expect(W.flockActionLabel(false)).toBe("Workers on every prompt: turn on");
    expect(W.flockActionLabel(true)).toBe("Workers on every prompt: turn off");
    expect(W.flockToast("api", true)).toMatch(/^api: Flock on/);
    expect(W.flockToast("api", false)).toMatch(/^api: Flock off/);
  });
});

describe("where Flock is offered", () => {
  it("this machine's own settled sessions, never a guest", () => {
    expect(W.flockAvailable(row())).toBe(true);
    expect(W.flockAvailable(row({ status: "paused" }))).toBe(true);
    expect(W.flockAvailable(undefined)).toBe(false);
    expect(W.flockAvailable(row({ pending: true } as Partial<Instance>))).toBe(false);
    expect(W.flockAvailable(row({ peer_share: true }))).toBe(false);
    expect(W.flockAvailable(row({ device: "mac-mini" }))).toBe(false);
    expect(W.flockAvailable(row({ title: "ana::fix" }))).toBe(false);
    expect(W.flockAvailable(row(), { guest: { label: "", name: "Bo", sessions: ["s"] } })).toBe(false);
  });
});

describe("insertWorkersRequest", () => {
  it("pastes the request as one chunk, then a space — no Enter — and focuses the terminal", () => {
    expect(W.insertWorkersRequest("s", { flock: { request: "Use workers.", model: "m" } } as Config)).toBe(true);
    expect(term.paste).toHaveBeenCalledWith("Use workers.");
    expect(handle.send).toHaveBeenCalledWith(" ");
    expect(handle.send.mock.calls.flat().join("")).not.toMatch(/[\r\n]/);
    expect(term.focus).toHaveBeenCalled();
  });

  it("reads the cached config when not given one", () => {
    queryClient.setQueryData(["config"], { flock: { request: "From config.", model: "m" } });
    W.insertWorkersRequest("s");
    expect(term.paste).toHaveBeenCalledWith("From config.");
  });

  it("says so, and types nothing, without a connected terminal", () => {
    handle.state = "disconnected";
    expect(W.insertWorkersRequest("s")).toBe(false);
    haveTerm = false;
    expect(W.insertWorkersRequest("s")).toBe(false);
    expect(term.paste).not.toHaveBeenCalled();
    expect(toast).toHaveBeenCalledTimes(2);
  });
});

describe("toggleFlock", () => {
  it("POSTs the flip of the row's state and patches the row with the answer", async () => {
    instApi.mockResolvedValue({ flock: true });
    expect(await W.toggleFlock("s")).toBe(true);
    expect(instApi).toHaveBeenCalledWith("s", "/flock", { json: { on: true } });
    expect(queryClient.getQueryData<Instance[]>(["instances"])![0].flock).toBe(true);

    instApi.mockResolvedValue({ flock: false });
    expect(await W.toggleFlock("s")).toBe(false);
    expect(instApi).toHaveBeenLastCalledWith("s", "/flock", { json: { on: false } });
  });

  it("a failure toasts and resolves null", async () => {
    instApi.mockRejectedValue(new Error("nope"));
    expect(await W.toggleFlock("s", true)).toBeNull();
    expect(String(toast.mock.calls.at(-1)?.[0])).toContain("nope");
  });
});

describe("the hotkeys", () => {
  const byId = (id: string) => KEYMAP.find((e) => e.id === id)!;

  it("Alt+G asks once, Alt+Shift+G toggles; both in the ? sheet and free of every other default", () => {
    expect(sameCombo(byId("workers"), { key: "g", alt: true })).toBe(true);
    expect(sameCombo(byId("flock"), { key: "g", alt: true, shift: true })).toBe(true);
    expect(byId("workers").help).toEqual(["Focused session", "Alt+G", expect.stringMatching(/^Ask for workers/)]);
    expect(byId("flock").help).toEqual(["Focused session", "Alt+Shift+G", expect.stringMatching(/^Workers on every prompt/)]);
    expect(comboProblem({ key: "g", alt: true }, "workers")).toBeNull();
    expect(comboProblem({ key: "g", alt: true, shift: true }, "flock")).toBeNull();
  });

  it("fire from a terminal, not from another text field or with no session focused", () => {
    // A fake document (the suite runs in node): the focused element is all
    // the guard reads, plus "no overlay on screen".
    const el = (tagName: string, inXterm: boolean) => ({
      tagName,
      closest: (sel: string) => (inXterm && sel.includes(".xterm") ? {} : null),
    });
    const fakeDoc = (active: unknown) => ({ activeElement: active, querySelectorAll: () => [], getElementById: () => null });
    useUi.setState({ focused: "s" } as never);
    vi.stubGlobal("document", fakeDoc(el("INPUT", false)));
    expect(workersKeyAllowed()).toBe(false);
    vi.stubGlobal("document", fakeDoc(el("TEXTAREA", true))); // xterm's helper textarea
    expect(workersKeyAllowed()).toBe(true);
    vi.stubGlobal("document", fakeDoc(el("BODY", false)));
    expect(workersKeyAllowed()).toBe(true);
    useUi.setState({ focused: null } as never);
    expect(workersKeyAllowed()).toBe(false);
    vi.unstubAllGlobals();
  });
});

describe("the row menu and the palette", () => {
  const menu = (o: Partial<Instance> = {}) => {
    const inst = row(o);
    return sessionMenu(inst, actionCtx("s", { inst, caps: CAPS as never }));
  };
  const flockItem = (o: Partial<Instance> = {}) =>
    menu(o).groups.find((g) => g.id === "session")?.items.find((i) => i.key === "flock");

  it("Workers on every prompt is a ✓ switch under Session", () => {
    expect(flockItem()).toMatchObject({ label: "Workers on every prompt", checked: false, keys: "Alt+Shift+G" });
    expect(flockItem({ flock: true })).toMatchObject({ label: "✓ Workers on every prompt", checked: true });
    expect(flockItem({ peer_share: true })).toBeUndefined();
    expect(flockItem({ device: "mac-mini" })).toBeUndefined();
  });

  it("the table labels the toggle by what it does, and Ask for workers needs a running agent", () => {
    const at = (o: Partial<Instance>) => actionCtx("s", { inst: row(o), caps: CAPS as never });
    expect(actionLabel("flock", at({}))).toBe("Workers on every prompt: turn on");
    expect(actionLabel("flock", at({ flock: true }))).toBe("Workers on every prompt: turn off");
    expect(SESSION_ACTIONS.askWorkers.enabled(at({}))).toBe(true);
    expect(SESSION_ACTIONS.askWorkers.enabled(at({ status: "paused" }))).toBe(false);
  });

  it("the palette lists both", () => {
    const list = PALETTE_SRC.slice(PALETTE_SRC.indexOf("const PALETTE_ACTIONS"), PALETTE_SRC.indexOf("];", PALETTE_SRC.indexOf("const PALETTE_ACTIONS")));
    expect(list).toContain('"askWorkers"');
    expect(list).toContain('"flock"');
  });
});

describe("the pane control and the settings", () => {
  it("the pane draws a button, not a .stagechip, styled next to ⏩", () => {
    const at = PANE_SRC.indexOf("nextstep nextstep-flock");
    expect(at).toBeGreaterThan(0);
    const start = PANE_SRC.lastIndexOf("<", at);
    const el = PANE_SRC.slice(start, PANE_SRC.indexOf("</button>", at));
    expect(el).toMatch(/^<button\b/);
    expect(el).not.toContain("stagechip");
    expect(PANE_SRC).toMatch(/flockShown = flockAvailable\(inst, config\)/);
    expect(PANE_SRC.slice(at, at + 900)).toContain("toggleFlock(title)");
    expect(PANE_CSS).toContain(".pane-head .actions .nextstep.nextstep-flock.is-on");
  });

  it("General has the request and the model, saved under general", () => {
    expect(GENERAL_SRC).toContain('"workers_request"');
    expect(GENERAL_SRC).toMatch(/<SettingField group="general" field="workers_model"/);
    expect(GENERAL_SRC).toContain("Workers request");
    expect(GENERAL_SRC).toContain("Workers model");
    expect(GENERAL_SRC).toContain("or to every prompt while a session's Flock is on");
  });

  it("the types carry the contract", () => {
    expect(TYPES_SRC).toContain("flock?: { request: string; model: string };");
    expect(TYPES_SRC).toContain("flock?: boolean;");
  });
});
