/** First-run corners (setup checklist, agent card, doctor list, sign-in and
 * install windows, the empty Sky): failures say so in words with a way on,
 * a re-check shows it is running, Enter saves the git identity, and closing
 * the sign-in window relaunches a session only once the login is there.
 *
 * vitest runs in node with no DOM, so components are SHALLOW-rendered: React's
 * state/effect hooks are swapped for a tiny slot store below, the component
 * is called as a function, and the element tree it returns is searched and
 * its handlers called — enough to watch state change across renders. */

import { describe, expect, it, vi, beforeEach } from "vitest";

// ---- a minimal hook runtime ------------------------------------------------
const H = {
  slots: [] as unknown[],
  i: 0,
  deps: [] as Array<unknown[] | undefined>,
  cleanups: [] as Array<(() => void) | undefined>,
  queue: [] as Array<() => void>,
};
function depsChanged(k: number, deps?: unknown[]): boolean {
  const prev = H.deps[k];
  if (!deps || !prev) return true;
  return deps.length !== prev.length || deps.some((d, j) => !Object.is(d, prev[j]));
}
vi.mock("react", async (orig) => {
  const R = (await orig()) as typeof import("react");
  const hooks = {
    useState: <T,>(init: T | (() => T)) => {
      const k = H.i++;
      if (!(k in H.slots)) H.slots[k] = typeof init === "function" ? (init as () => T)() : init;
      const set = (v: T | ((p: T) => T)) => {
        H.slots[k] = typeof v === "function" ? (v as (p: T) => T)(H.slots[k] as T) : v;
      };
      return [H.slots[k] as T, set] as const;
    },
    useRef: <T,>(v: T) => {
      const k = H.i++;
      if (!(k in H.slots)) H.slots[k] = { current: v };
      return H.slots[k] as { current: T };
    },
    useEffect: (fn: () => void | (() => void), deps?: unknown[]) => {
      const k = H.i++;
      H.slots[k] = null;
      if (!depsChanged(k, deps)) return;
      H.deps[k] = deps;
      H.queue.push(() => {
        H.cleanups[k]?.();
        const c = fn();
        H.cleanups[k] = typeof c === "function" ? c : undefined;
      });
    },
    useCallback: <F,>(f: F) => f,
    useMemo: <T,>(f: () => T) => f(),
  };
  return { ...R, ...hooks, default: { ...R, ...hooks } };
});

function reset() {
  for (const c of H.cleanups) c?.();
  H.slots = [];
  H.deps = [];
  H.cleanups = [];
  H.queue = [];
}
/** Call the component as a function, then run the effects it queued. */
function render<P>(C: (p: P) => unknown, props: P): unknown {
  H.i = 0;
  H.queue = [];
  const out = C(props);
  for (const run of H.queue) run();
  return out;
}
const flush = () => new Promise((r) => setTimeout(r, 0));

type El = { type: unknown; props: Record<string, unknown> };
function* walk(n: unknown): Generator<El> {
  if (n == null || typeof n !== "object") return;
  if (Array.isArray(n)) {
    for (const c of n) yield* walk(c);
    return;
  }
  if (!("props" in n) || !("type" in n)) return;
  const e = n as El;
  yield e;
  for (const v of Object.values(e.props || {})) yield* walk(v);
}
function text(n: unknown): string {
  if (n == null || typeof n === "boolean") return "";
  if (typeof n === "string" || typeof n === "number") return String(n);
  if (Array.isArray(n)) return n.map(text).join("");
  if (typeof n === "object" && "props" in n) return text((n as El).props.children);
  return "";
}
const byClass = (tree: unknown, cls: string) =>
  [...walk(tree)].find((e) => typeof e.props.className === "string" && (e.props.className as string).split(" ").includes(cls));
const byType = (tree: unknown, t: unknown) => [...walk(tree)].filter((e) => e.type === t);
const ev = (extra: Record<string, unknown> = {}) => ({ stopPropagation: vi.fn(), preventDefault: vi.fn(), ...extra });

// ---- module stubs ----------------------------------------------------------
const api = vi.fn();
vi.mock("../api/client", () => ({ api: (...a: unknown[]) => api(...a) }));
vi.mock("../lib/toast", () => ({ toast: vi.fn() }));
const doctorQ: { current: { data: unknown; error: unknown } } = { current: { data: undefined, error: null } };
const reprobeDoctor = vi.fn();
const getQueryData = vi.fn();
vi.mock("../state/queries", async (orig) => ({
  ...(await orig<object>()),
  useDoctor: () => doctorQ.current,
  reprobeDoctor: () => reprobeDoctor(),
  queryClient: { getQueryData: (k: unknown) => getQueryData(k), setQueryData: vi.fn(), invalidateQueries: vi.fn() },
  useInstances: () => ({ data: undefined }),
  useConfig: () => ({ data: undefined }),
}));
const ui = { focused: null, openDialog: null, openDialogFor: vi.fn(), closeDialog: vi.fn() };
vi.mock("../state/store", async (orig) => ({
  ...(await orig<object>()),
  useUi: Object.assign((sel: (s: typeof ui) => unknown) => sel(ui), { getState: () => ui }),
}));
const setupQ: { current: { data: unknown; error: unknown } } = { current: { data: undefined, error: null } };
const refreshSetup = vi.fn(async (_reprobe?: boolean) => {});
vi.mock("../components/setup/useSetup", () => ({
  SETUP_KEY: ["setup"],
  useSetup: () => setupQ.current,
  refreshSetup: (r?: boolean) => refreshSetup(r),
  skipSetupStep: vi.fn(),
  chooseSetupAgent: vi.fn(),
}));
const reviveSession = vi.fn(async (_title: string) => true);
vi.mock("../lib/revive", () => ({ reviveSession: (t: string) => reviveSession(t) }));
vi.mock("../components/flights/useWaiting", () => ({ useWaiting: () => ({ waiting: [] }) }));
vi.mock("../components/dialogs/InstallTerminal", async (orig) => ({
  ...(await orig<object>()),
  useInstallWindow: (sel: (s: { open: boolean }) => unknown) => sel({ open: false }),
}));

const { DoctorList, GitIdentityForm } = await import("../components/setup/DoctorList");
const { AgentCard, AgentCardError } = await import("../components/setup/AgentCard");
const { SetupChecklist } = await import("../components/setup/SetupChecklist");
const { SkyEmpty, ADD_TICKET_SOURCE } = await import("../components/onboarding/SkyEmpty");
const { AgentSignIn, termStateText } = await import("../components/dialogs/AgentSignIn");
const { InstallMissing, terminalFailed, TERM_CLOSE_GRACE_MS } = await import("../components/dialogs/InstallTerminal");
const { readsSignedIn, reviveIfSignedIn } = await import("../components/setup/AgentBlocked");

// The cards and the checklist listen for "connections changed" on window.
vi.stubGlobal("window", new EventTarget());

beforeEach(() => {
  reset();
  vi.clearAllMocks();
  doctorQ.current = { data: undefined, error: null };
  setupQ.current = { data: undefined, error: null };
});

const PAYLOAD = {
  ok: false,
  checks: [
    { id: "git", label: "git", status: "ok", detail: "git 2.43", required: true },
    { id: "tmux", label: "tmux", status: "fail", detail: "not found", fix: "sudo apt install tmux", install: true, required: true },
  ],
  install: { steps: [{ id: "packages", label: "system packages: tmux", cmd: "sudo apt install -y tmux" }] },
};

// ---- 1. sign-in window closed → relaunch only when signed in --------------
describe("closing the sign-in window on a blocked session", () => {
  it("reads signed in the way the banner reads signed out (only a found-nothing false blocks)", () => {
    const st = (v: boolean | null) => ({ logins: [{ name: "claude", installed: true, logged_in: v, login_cmd: "", default: true }] });
    expect(readsSignedIn(st(true), "claude")).toBe(true);
    expect(readsSignedIn(st(null), "claude")).toBe(true);
    expect(readsSignedIn(st(false), "claude")).toBe(false);
    expect(readsSignedIn(st(true), "codex")).toBe(false);
    expect(readsSignedIn(undefined, "claude")).toBe(false);
  });

  it("re-reads the login, and leaves the session alone when the person did not sign in", async () => {
    getQueryData.mockReturnValue({ logins: [{ name: "claude", installed: true, logged_in: false }] });
    expect(await reviveIfSignedIn("s1", "claude")).toBe(false);
    expect(refreshSetup).toHaveBeenCalledWith(true);
    expect(reviveSession).not.toHaveBeenCalled();
  });

  it("relaunches once the login landed", async () => {
    getQueryData.mockReturnValue({ logins: [{ name: "claude", installed: true, logged_in: true }] });
    expect(await reviveIfSignedIn("s1", "claude")).toBe(true);
    expect(reviveSession).toHaveBeenCalledWith("s1");
  });
});

// ---- 3 + 4. the doctor list: keep the last good result; say "Checking…" ----
describe("DoctorList", () => {
  it("a failed re-check keeps the last good list and its Install button, with one line under it", () => {
    doctorQ.current = { data: PAYLOAD, error: new Error("/api/doctor -> 500") };
    const tree = render(DoctorList, { essential: true });
    expect(byType(tree, InstallMissing)).toHaveLength(1);
    expect(byClass(tree, "setup-allchecks")).toBeTruthy();
    const note = byClass(tree, "doctor-recheck-failed");
    expect(text(note)).toBe("Couldn't re-check. Try again.");
    expect(JSON.stringify(text(tree))).not.toMatch(/doctor/i);
  });

  it("the full list (Settings → Doctor) keeps its result the same way", () => {
    doctorQ.current = { data: PAYLOAD, error: new Error("boom") };
    const tree = render(DoctorList, {});
    expect(byClass(tree, "doctor-list")).toBeTruthy();
    expect(text(byClass(tree, "doctor-recheck-failed"))).toBe("Couldn't re-check. Try again.");
  });

  it("with nothing to keep, says the checks didn't run — never 'doctor failed'", () => {
    doctorQ.current = { data: undefined, error: new Error("boom") };
    const tree = render(DoctorList, { essential: true });
    expect(text(tree)).toBe("Couldn't run the checks. Try again.");
    doctorQ.current = { data: { ok: true, checks: [] }, error: null };
    expect(text(render(DoctorList, {}))).not.toMatch(/doctor/i);
  });

  it("a re-check reports itself in flight, then done — and a failure keeps the list", async () => {
    doctorQ.current = { data: PAYLOAD, error: null };
    let fail: (e: Error) => void = () => {};
    reprobeDoctor.mockReturnValue(new Promise((_, rej) => (fail = rej)));
    const onChecking = vi.fn();
    render(DoctorList, { essential: true, reprobeKey: 1, onChecking });
    expect(onChecking).toHaveBeenLastCalledWith(true);
    fail(new Error("/api/doctor -> 502"));
    await flush();
    expect(onChecking).toHaveBeenLastCalledWith(false);
    const tree = render(DoctorList, { essential: true, reprobeKey: 1, onChecking });
    expect(byType(tree, InstallMissing)).toHaveLength(1);
    expect(text(byClass(tree, "doctor-recheck-failed"))).toBe("Couldn't re-check. Try again.");
  });

  it("Settings → Doctor (no Re-check of its own to relabel) shows Checking… while it runs", () => {
    doctorQ.current = { data: PAYLOAD, error: null };
    reprobeDoctor.mockReturnValue(new Promise(() => {}));
    render(DoctorList, { reprobeKey: 1 });
    const tree = render(DoctorList, { reprobeKey: 1 });
    expect(text(byClass(tree, "doctor-checking"))).toBe("Checking…");
  });

  it("unmounting mid-probe never leaves the caller's button on Checking…", () => {
    doctorQ.current = { data: PAYLOAD, error: null };
    reprobeDoctor.mockReturnValue(new Promise(() => {}));
    const onChecking = vi.fn();
    render(DoctorList, { essential: true, reprobeKey: 1, onChecking });
    reset();
    expect(onChecking).toHaveBeenLastCalledWith(false);
  });
});

describe("SetupChecklist", () => {
  it("Re-check says Checking… and is disabled while the probe runs", () => {
    setupQ.current = { data: undefined, error: null };
    let tree = render(SetupChecklist, {});
    const recheck = () => byClass(tree, "setup-recheck")!;
    expect(text(recheck())).toBe("Re-check");
    expect(recheck().props.disabled).toBe(false);
    // The step's DoctorList reports its probe through onChecking.
    const list = byType(tree, DoctorList).find((e) => !!e.props.onChecking)!;
    (list.props.onChecking as (b: boolean) => void)(true);
    tree = render(SetupChecklist, {});
    expect(text(recheck())).toBe("Checking…");
    expect(recheck().props.disabled).toBe(true);
    // A click meanwhile starts no second probe.
    const before = byType(tree, DoctorList)[0].props.reprobeKey;
    (recheck().props.onClick as (e: unknown) => void)(ev());
    tree = render(SetupChecklist, {});
    expect(byType(tree, DoctorList)[0].props.reprobeKey).toBe(before);
  });

  it("a failed status read offers Retry, which re-reads setup", () => {
    setupQ.current = { data: undefined, error: new Error("/api/setup -> 500") };
    const tree = render(SetupChecklist, {});
    expect(text(byClass(tree, "setup-load-failed"))).toContain("Couldn't load setup.");
    (byClass(tree, "setup-retry")!.props.onClick as (e: unknown) => void)(ev());
    expect(refreshSetup).toHaveBeenCalled();
  });
});

// ---- 5. the agent card when /api/connections fails --------------------------
describe("AgentCard", () => {
  it("a failed connections read says so, with Retry, and still offers sign-in", async () => {
    api.mockRejectedValue(new Error("/api/connections -> 500"));
    render(AgentCard, { provider: "claude", tourNext: true });
    await flush();
    const tree = render(AgentCard, { provider: "claude", tourNext: true });
    const card = byType(tree, AgentCardError)[0];
    expect(card).toBeTruthy();
    const inner = render(AgentCardError, card.props as never);
    expect(text(byClass(inner, "agent-card-failed"))).toContain("Couldn't check Claude Code.");
    const signIn = byType(inner, AgentSignIn)[0];
    expect(signIn.props.provider).toBe("claude");
    expect(signIn.props.dataTour).toBe("setup-next");
    // Retry asks again, past the shared cache.
    api.mockResolvedValue({ connections: [{ kind: "agent", provider: "claude", name: "Claude Code" }] });
    (byClass(inner, "agent-card-retry")!.props.onClick as (e: unknown) => void)(ev());
    await flush();
    expect(api).toHaveBeenLastCalledWith("/api/connections?refresh=1");
    const after = render(AgentCard, { provider: "claude", tourNext: true });
    expect(byType(after, AgentCardError)).toHaveLength(0);
  });

  it("with no agent named (the default, unknown without the read) it offers Retry only", () => {
    const inner = render(AgentCardError, { provider: "", onRetry: vi.fn(), onSignedIn: vi.fn() });
    expect(byClass(inner, "agent-card-retry")).toBeTruthy();
    expect(byType(inner, AgentSignIn)).toHaveLength(0);
  });
});

// ---- 6. the install and sign-in windows' words ------------------------------
describe("terminal windows", () => {
  it("say the terminal's state in plain words, never the socket's", () => {
    expect(termStateText("connecting")).toBe("Opening the terminal…");
    expect(termStateText("disconnected")).toBe("The terminal closed.");
    expect(termStateText("reconnecting")).toBe("Reconnecting…");
    expect(termStateText("streaming")).toBe("");
    expect(termStateText("held")).toBe("");
  });

  it("the install window offers the commands as soon as its terminal is gone with no result", () => {
    // Never opened: at once.
    expect(terminalFailed("disconnected", null, false, 0)).toBe(true);
    // Closed after a run was seen: the exit status gets a moment to land.
    expect(terminalFailed("disconnected", null, true, 1000)).toBe(false);
    expect(terminalFailed("disconnected", null, true, TERM_CLOSE_GRACE_MS)).toBe(true);
    // A result, or a live terminal: no.
    expect(terminalFailed("disconnected", 0, true, 99_999)).toBe(false);
    expect(terminalFailed("disconnected", 1, false, 0)).toBe(false);
    expect(terminalFailed("connecting", null, false, 0)).toBe(false);
    expect(terminalFailed("streaming", null, true, 99_999)).toBe(false);
  });
});

// ---- 8. the empty Sky's ticket-source button --------------------------------
describe("the empty Sky", () => {
  it("says Add a ticket source, like the checklist, and opens Intake on Tickets", () => {
    const tree = render(SkyEmpty, { blank: true, className: "x" });
    const b = byClass(tree, "sky-empty-intake")!;
    expect(text(b)).toBe(ADD_TICKET_SOURCE);
    expect(ADD_TICKET_SOURCE).toBe("Add a ticket source");
    (b.props.onClick as () => void)();
    expect(ui.openDialogFor).toHaveBeenCalledWith("intake", "tickets");
  });
});

// ---- 9. Enter saves the git identity -----------------------------------------
describe("GitIdentityForm", () => {
  function filled() {
    render(GitIdentityForm, { onSaved: vi.fn() });
    // Slots 0 and 1 are the name and the email.
    H.slots[0] = "Ada Lovelace";
    H.slots[1] = "ada@example.com";
    return render(GitIdentityForm, { onSaved: vi.fn() });
  }

  it("Enter in either field saves, like the button", () => {
    api.mockResolvedValue({});
    for (const cls of ["doctor-ident-name", "doctor-ident-email"]) {
      reset();
      api.mockClear();
      const input = byClass(filled(), cls)!;
      const e = ev({ key: "Enter" });
      (input.props.onKeyDown as (e: unknown) => void)(e);
      expect(e.preventDefault).toHaveBeenCalled();
      expect(api).toHaveBeenCalledWith("/api/github/identity", {
        json: { name: "Ada Lovelace", email: "ada@example.com" },
      });
    }
  });

  it("other keys type; Enter with a field empty saves nothing", () => {
    const tree = filled();
    (byClass(tree, "doctor-ident-name")!.props.onKeyDown as (e: unknown) => void)(ev({ key: "a" }));
    expect(api).not.toHaveBeenCalled();
    reset();
    const empty = render(GitIdentityForm, { onSaved: vi.fn() });
    (byClass(empty, "doctor-ident-email")!.props.onKeyDown as (e: unknown) => void)(ev({ key: "Enter" }));
    expect(api).not.toHaveBeenCalled();
  });
});
