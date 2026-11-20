/** UI sweep P4, onboarding: Connect GitHub never dead-ends (a failed status
 * read has Retry, a pending device code has Cancel, closing the gh window
 * early says so), its identity form saves on Enter like Setup's, the
 * "Connected" line names where the token comes from in words, Setup's doctor
 * fixes get a Copy, and the New dialog's agent notes name the agent and open
 * the fix in place.
 *
 * vitest runs in node with no DOM, so ConnectGitHub is SHALLOW-rendered the
 * way onboardingCorners.test.ts does it: React's hooks are swapped for a slot
 * store, the component is called as a function, and its element tree is
 * searched and its handlers called. */

import { beforeEach, describe, expect, it, vi } from "vitest";
import { SOURCES } from "./sourceText";

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
    // Stable across renders, as React's is for [] deps: ConnectGitHub's
    // effects depend on `load` / `connected`.
    useCallback: <F,>(f: F) => {
      const k = H.i++;
      if (!(k in H.slots)) H.slots[k] = f;
      return H.slots[k] as F;
    },
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
const byId = (tree: unknown, id: string) => [...walk(tree)].find((e) => e.props.id === id);
const byClass = (tree: unknown, cls: string) =>
  [...walk(tree)].find((e) => typeof e.props.className === "string" && (e.props.className as string).split(" ").includes(cls));
const ev = (extra: Record<string, unknown> = {}) => ({ stopPropagation: vi.fn(), preventDefault: vi.fn(), ...extra });

// ---- module stubs ----------------------------------------------------------
const api = vi.fn();
vi.mock("../api/client", () => ({ api: (...a: unknown[]) => api(...a) }));
vi.mock("../lib/toast", () => ({ toast: vi.fn() }));
vi.mock("../lib/wsTerm", () => ({ useWsTerm: () => "connecting" }));
vi.stubGlobal("window", new EventTarget());

const { ConnectGitHub, githubSourceWords, GH_NOT_CONNECTED, isCommand } = await import(
  "../components/dialogs/ConnectGitHub"
);
const { fixCommand } = await import("../components/setup/DoctorList");
const { agentMissingReason, agentSignedOutLine } = await import("../components/dialogs/newSession");

const src = (p: string) => {
  const t = SOURCES[p];
  if (t === undefined) throw new Error("no such source: " + p);
  return t;
};

const STATUS = {
  connected: false,
  login: "",
  source: "none",
  methods: ["token", "gh"],
  gh: { installed: true },
  identity: { name: "", email: "" },
  token_url: "https://github.com/settings/tokens/new",
};

/** Render, let load() land, render again. */
async function settle(props: Record<string, unknown> = {}) {
  render(ConnectGitHub, props);
  await flush();
  await flush();
  return render(ConnectGitHub, props);
}

beforeEach(() => {
  reset();
  api.mockReset();
});

describe("Connect GitHub: a failed status read", () => {
  it("says Couldn't check GitHub, the reason under it, and Retry loads again", async () => {
    api.mockRejectedValueOnce(new Error("server said 502"));
    let tree = await settle();
    const box = byId(tree, "gh-load-failed")!;
    expect(text(box.props.children)).toContain("Couldn't check GitHub.");
    expect(text(byClass(tree, "gh-load-reason"))).toBe("server said 502");
    expect(text(tree)).not.toContain("GitHub status failed");

    api.mockResolvedValue({ ...STATUS, connected: true, login: "ada", source: "settings" });
    (byId(tree, "gh-retry")!.props.onClick as (e: unknown) => void)(ev());
    await flush();
    await flush();
    tree = render(ConnectGitHub, {});
    expect(api).toHaveBeenLastCalledWith("/api/github/status");
    expect(byId(tree, "gh-load-failed")).toBeUndefined();
    expect(text(byId(tree, "gh-connected"))).toContain("Connected as @ada");
  });
});

describe("Connect GitHub: the Connected line names the token's source in words", () => {
  it("maps the server's ids, and falls back to the raw id", () => {
    expect(githubSourceWords("settings")).toBe("your saved token");
    expect(githubSourceWords("env:GITHUB_TOKEN")).toBe("the GITHUB_TOKEN variable");
    expect(githubSourceWords("env:GH_TOKEN")).toBe("the GH_TOKEN variable");
    expect(githubSourceWords("gh-cli")).toBe("gh's sign-in");
    expect(githubSourceWords("config.toml")).toBe("config.toml");
    expect(githubSourceWords("none")).toBe("");
    expect(githubSourceWords(undefined)).toBe("");
  });

  it("renders it after the handle", async () => {
    api.mockResolvedValue({ ...STATUS, connected: true, login: "ada", source: "env:GITHUB_TOKEN" });
    const tree = await settle();
    const line = text(byId(tree, "gh-connected"));
    expect(line).toContain("· via the GITHUB_TOKEN variable");
    expect(line).not.toContain("env:");
  });
});

describe("Connect GitHub: a pending device code can be cancelled", () => {
  it("Cancel asks the server to drop it and goes back to Sign in with GitHub", async () => {
    const flow = {
      state: "pending",
      user_code: "ABCD-1234",
      verification_uri: "https://github.com/login/device",
      interval: 5,
    };
    api.mockResolvedValue({ ...STATUS, methods: ["device", "token"], flow });
    let tree = await settle();
    expect(text(byId(tree, "gh-device"))).toContain("ABCD-1234");
    const cancel = byId(tree, "gh-device-cancel")!;
    expect(text(cancel)).toBe("Cancel");
    expect(cancel.props.className).toContain("linklike");

    api.mockResolvedValue({});
    (cancel.props.onClick as (e: unknown) => void)(ev());
    await flush();
    await flush();
    expect(api).toHaveBeenCalledWith("/api/github/device", { method: "DELETE" });
    tree = render(ConnectGitHub, {});
    expect(byId(tree, "gh-device")).toBeUndefined();
    expect(byId(tree, "gh-device-start")).toBeDefined();
  });
});

describe("Connect GitHub: closing the gh window before signing in", () => {
  it("says so inline instead of only reloading", async () => {
    api.mockResolvedValue(STATUS);
    let tree = await settle();
    (byId(tree, "gh-cli-signin")!.props.onClick as (e: unknown) => void)(ev());
    tree = render(ConnectGitHub, {});
    const win = [...walk(tree)].find((e) => typeof e.type === "function" && (e.type as { name: string }).name === "GhSignInWindow")!;
    expect(win).toBeDefined();

    // import-gh answers 409 (api throws); the status re-read still works.
    api.mockImplementation(async (path: string) => {
      if (path === "/api/github/import-gh") throw new Error("gh is not signed in");
      return STATUS;
    });
    (win.props.onClose as () => void)();
    await flush();
    await flush();
    tree = render(ConnectGitHub, {});
    expect(text(byId(tree, "gh-cli-note"))).toBe(GH_NOT_CONNECTED);
    expect(GH_NOT_CONNECTED).toBe("Not connected yet. Finish the steps in the terminal, or paste a token.");
  });
});

describe("Connect GitHub: the git name/email form matches Setup's", () => {
  it("has labels, an email box, and Enter saves", async () => {
    api.mockResolvedValue({ ...STATUS, identity_suggested: { name: "Ada", email: "ada@users.noreply.github.com" } });
    const tree = await settle();
    const nameBox = byClass(tree, "gh-ident-name")!;
    const emailBox = byClass(tree, "gh-ident-email")!;
    expect(nameBox.props["aria-label"]).toBe("Your name for commits");
    expect(emailBox.props["aria-label"]).toBe("Your email for commits");
    expect(emailBox.props.type).toBe("email");

    api.mockClear();
    const other = ev({ key: "a" });
    (emailBox.props.onKeyDown as (e: unknown) => void)(other);
    expect(api).not.toHaveBeenCalled();
    const enter = ev({ key: "Enter" });
    (emailBox.props.onKeyDown as (e: unknown) => void)(enter);
    expect(enter.preventDefault).toHaveBeenCalled();
    expect(api).toHaveBeenCalledWith("/api/github/identity", {
      json: { name: "Ada", email: "ada@users.noreply.github.com" },
    });
  });
});

describe("Setup: a doctor fix that is a command has a Copy", () => {
  it("knows a command from prose, and copies the command alone", () => {
    expect(fixCommand("sudo apt install tmux")).toBe("sudo apt install tmux");
    expect(fixCommand("brew install tmux")).toBe("brew install tmux");
    expect(fixCommand("xcode-select --install (or: brew install git)")).toBe("xcode-select --install");
    expect(fixCommand("git config --global user.email you@example.com")).toBe(
      "git config --global user.email you@example.com"
    );
    expect(fixCommand("install `claude` or set a binary path in Settings → Agents")).toBeNull();
    expect(fixCommand("upgrade MindFlock, then rename that file back")).toBeNull();
    expect(fixCommand(undefined)).toBeNull();
    // The push check's own words still read the same.
    expect(isCommand("git remote add origin <url>")).toBe(true);
    expect(isCommand("Add an SSH key to GitHub, or switch the remote to HTTPS.")).toBe(false);
  });

  it("the row draws the Copy beside the fix, with the shared toast", () => {
    const list = src("components/setup/DoctorList.tsx");
    expect(list).toContain("{fixCommand(c.fix) && <CopyFix command={fixCommand(c.fix) as string} />}");
    expect(list).toContain('toast(ok ? "Copied — paste it into a terminal" : "Copy failed")');
  });
});

describe("New dialog: the agent notes", () => {
  it("names the agent as people read it, not its id", () => {
    expect(agentSignedOutLine("claude")).toBe(
      "Claude Code isn't signed in yet — a session would stop at its sign-in screen."
    );
  });

  it("the not-installed note says Setup, and has an Open Setup button", () => {
    const why = agentMissingReason("codex", { codex: false });
    expect(why).toContain("one click from Setup (step ①");
    expect(why).not.toContain("Get set up");
    const dlg = src("components/dialogs/NewSessionDialog.tsx");
    expect(dlg).toMatch(/id="new-agent-open-setup"[\s\S]*?closeDialog\(\);\s*useUi\.getState\(\)\.openDialogFor\("setup"\);[\s\S]*?Open Setup/);
  });

  it("Sign in… opens the sign-in window over the dialog and re-reads the agents on close", () => {
    const dlg = src("components/dialogs/NewSessionDialog.tsx");
    expect(dlg).toContain("{agentSignedOutLine(canonAgent(program))}");
    expect(dlg).toContain("onClick={() => setSignInFor(canonAgent(program))}");
    expect(dlg).toMatch(/<SignInWindow\s+provider=\{signInFor\}\s+onClose=\{\(\) => \{\s*setSignInFor\(""\);\s*notifyConnectionsChanged\(\);\s*refreshAgentState\(\);/);
    // No longer sends the person away to Settings.
    expect(dlg).not.toContain('openSettings("coding")');
  });
});
