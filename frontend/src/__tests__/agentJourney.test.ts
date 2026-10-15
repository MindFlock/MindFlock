/** Round 4 — getting an agent installed, signed in and actually working, and
 * telling the truth about it everywhere: the one agent card (Install → Sign
 * in → Test), the sign-in window's real steps, and a session whose agent
 * isn't signed in reading "Not signed in", never Idle / Question. */

import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it, vi } from "vitest";
import type { ConnectionRow } from "../api/types";

// The Install action reads the doctor's plan (react-query): stub it.
const doctorData: { current: unknown } = { current: undefined };
vi.mock("../state/queries", async (orig) => ({
  ...(await orig<object>()),
  useDoctor: () => ({ data: doctorData.current, error: null }),
  reprobeDoctor: vi.fn(),
}));

const { ConnectionRowView, agentStage, plainDetail, describeTestResult } = await import(
  "../components/settings/screens/Connections"
);
const { agentBlocked, agentBlockedLine, agentBlockedState } = await import("../lib/sessionState");
const { blockedAction, blockedSentence } = await import("../components/setup/AgentBlocked");
const { agentDisplayName, signInSteps } = await import("../components/dialogs/AgentSignIn");
const { agentRowFor } = await import("../components/setup/AgentCard");
import { code, src } from "./sourceText";

const agent = (over: Partial<ConnectionRow> & Record<string, unknown>): ConnectionRow =>
  ({
    id: "agent",
    name: "Coding agent · Claude",
    kind: "agent",
    status: "connected",
    detail: "signed in (login state found in /home/me/.claude.json) · /usr/bin/claude",
    action: null,
    test: { endpoint: "/api/connections/test/agent", method: "POST", body: { provider: "claude" } },
    provider: "claude",
    default: true,
    ...over,
  }) as ConnectionRow;

const deps = { gotoScreen: vi.fn(), openDialogFor: vi.fn(), closeDialog: vi.fn() };
const render = (c: ConnectionRow) =>
  renderToStaticMarkup(createElement(ConnectionRowView, { c, deps, onChanged: () => {}, onTest: () => {} }));
const buttons = (html: string) => (html.match(/<button[^>]*>[^<]*(<span[^>]*>[^<]*<\/span>)?[^<]*<\/button>/g) || []).length;

describe("one agent card: status + ONE next action", () => {
  it("not installed → Install (the Setup checklist's own one-click install), no Test", () => {
    doctorData.current = {
      checks: [],
      install: { steps: [{ id: "agent-cli", label: "agent CLI (claude)", cmd: "curl -fsSL https://claude.ai/install.sh | bash" }] },
    };
    const c = agent({
      status: "needs_attention",
      detail: "not installed — `claude` not found on PATH",
      action: { label: "Install", kind: "dialog", target: "agent-install", provider: "claude" },
      fix_command: "curl -fsSL https://claude.ai/install.sh | bash",
    });
    expect(agentStage(c)).toBe("install");
    const html = render(c);
    expect(html).toContain('id="doctor-install-btn"');
    // The agent by its product name (GLOSSARY: "Install Claude Code").
    expect(html).toContain("Install Claude Code");
    expect(html).not.toContain(">Test</button>");
  });

  it("not installed and not in the doctor's plan → the copyable command", () => {
    doctorData.current = { checks: [], install: { steps: [] } };
    const html = render(
      agent({
        status: "needs_attention",
        detail: "not installed — `claude` not found on PATH",
        action: { label: "Install", kind: "dialog", target: "agent-install", provider: "claude" },
        fix_command: "curl -fsSL https://claude.ai/install.sh | bash",
      })
    );
    expect(html).toContain("<code>curl -fsSL https://claude.ai/install.sh | bash</code>");
    expect(html).not.toContain(">Test</button>");
  });

  it("installed, not signed in → Sign in, and no Test or login command beside it", () => {
    const c = agent({
      status: "needs_attention",
      detail: "installed, not signed in · /usr/bin/claude",
      action: { label: "Sign in", kind: "dialog", target: "agent-sign-in", provider: "claude" },
      fix: "sign in: claude",
      fix_command: "claude",
    });
    expect(agentStage(c)).toBe("signin");
    const html = render(c);
    expect(html).toContain(">Sign in</button>");
    expect(html).not.toContain(">Test</button>");
    expect(html).not.toContain("<code>claude</code>");
    expect(buttons(html)).toBe(1);
  });

  it("signed in → Test, the account in words, and a quiet Change agent link", () => {
    const c = agent({ account: "me@example.com", action: { label: "Change agent", kind: "screen", target: "coding" } });
    expect(agentStage(c)).toBe("test");
    expect(plainDetail(c)).toBe("Signed in as me@example.com");
    const html = render(c);
    expect(html).toContain(">Test</button>");
    expect(html).toContain('class="linklike conn-configure conn-link"');
    expect(html).not.toContain(".claude.json<");
  });

  it("a row with action null is 'nothing to do', not an old server's Configure", () => {
    const html = render(agent({ default: false, id: "agent:codex", provider: "codex", settings_screen: "providers" }));
    expect(html).not.toContain(">Configure</button>");
  });

  it("Test answers in words — where the agent is, how it is signed in, how long it took — never the login file", () => {
    const out = describeTestResult(
      {
        ok: true,
        detail: "Signed in as me@example.com",
        cli: { status: "ok", detail: "/usr/bin/claude — 2.1.0 (Claude Code)" },
        auth: { status: "ok", detail: "login state found in /home/me/.claude.json" },
      },
      420
    );
    expect(out).toEqual({
      ok: true,
      text: "Found at /usr/bin/claude · signed in as me@example.com · answered in 0.4 s",
    });
    expect(out.text).not.toContain(".claude.json");
    // A failing probe says what failed, with no timing.
    expect(
      describeTestResult(
        { ok: false, detail: "Installed, not signed in yet", cli: { status: "ok", detail: "/usr/bin/claude" }, auth: { status: "warn" } },
        300
      )
    ).toEqual({ ok: false, text: "Found at /usr/bin/claude · installed, not signed in yet" });
  });

  it("the card finds the default, or a named agent", () => {
    const rows = [agent({}), agent({ id: "agent:codex", provider: "codex", default: false })];
    expect(agentRowFor(rows, "")?.id).toBe("agent");
    expect(agentRowFor(rows, "codex")?.id).toBe("agent:codex");
    expect(agentRowFor(rows, "goose")).toBeNull();
  });

  it("every agent surface renders the same card, and nothing points elsewhere for it", () => {
    for (const f of ["components/settings/screens/CodingCli.tsx", "components/settings/screens/Providers.tsx", "components/setup/SetupChecklist.tsx"])
      expect(src(f), f).toContain("<AgentCard");
    const coding = src("components/settings/screens/CodingCli.tsx");
    expect(coding).not.toContain("To install or sign in to another, see");
    // The "another agent" picker reads product names, not raw ids.
    expect(coding).toContain("agentName(p.name)");
    expect(coding).not.toContain('" (default)"');
    // Connections reads the shared product-name map.
    const conns = src("components/settings/screens/Connections.tsx");
    expect(conns).toContain('import { agentName } from "../../../lib/agentNames";');
    expect(conns).not.toContain("export const AGENT_NAMES");
  });
});

describe("the sign-in window says what the terminal really shows", () => {
  it("Claude Code: a text style, then a login method, then the link", () => {
    const steps = signInSteps("claude");
    expect(steps).toHaveLength(3);
    expect(steps[0]).toMatch(/text style/);
    expect(steps[1]).toMatch(/how to log in/);
    expect(steps[2]).toMatch(/link/);
    expect(agentDisplayName("claude")).toBe("Claude Code");
  });

  it("other agents: their own prompts", () => {
    expect(signInSteps("codex")[0]).toContain("Codex");
  });

  it("no more 'copy the link below' (there was no link), no 'in in'", () => {
    const s = code("components/dialogs/AgentSignIn.tsx");
    expect(s).not.toContain("copy the link below");
    expect(s).not.toContain("signing in in");
    expect(s).toContain("✓ Signed in. You can close this window.");
  });
});

describe("a session whose agent isn't signed in says so", () => {
  const logins = [
    { name: "claude", installed: true, logged_in: false },
    { name: "codex", installed: true, logged_in: null },
    { name: "aider", installed: false },
  ];

  it("reads Not signed in instead of Idle or Question", () => {
    const b = agentBlocked({ title: "a", provider: "claude" }, logins, "idle");
    expect(b).toEqual({ name: "claude", why: "signed_out", exited: false });
    expect(agentBlocked({ title: "a", provider: "claude" }, logins, "clarify")?.why).toBe("signed_out");
    const ss = agentBlockedState(b!);
    expect(ss.label).toBe("Not signed in");
    expect(ss.state).toBe("needs_you");
    expect(agentBlockedLine(b!)).toBe("Not signed in");
    expect(blockedAction(b!)).toBe("Sign in");
    expect(blockedSentence(b!)).toContain("Answer its questions in the terminal below");
  });

  it("an agent that quit at launch: sign in, then it restarts", () => {
    const b = agentBlocked({ title: "a", provider: "claude" }, logins, "offline")!;
    expect(b.exited).toBe(true);
    expect(blockedSentence(b)).toContain("Sign in, and it restarts.");
  });

  it("never for a working agent, an unknowable login, an account profile, or another device", () => {
    expect(agentBlocked({ title: "a", provider: "claude" }, logins, "working")).toBeNull();
    expect(agentBlocked({ title: "a", provider: "claude" }, logins, "limit")).toBeNull();
    expect(agentBlocked({ title: "a", provider: "codex" }, logins, "idle")).toBeNull();
    expect(agentBlocked({ title: "a", provider: "claude", profile_effective: "work" }, logins, "idle")).toBeNull();
    expect(agentBlocked({ title: "a", provider: "claude", device: "rig" }, logins, "idle")).toBeNull();
    expect(agentBlocked({ title: "a", provider: "claude" }, null, "idle")).toBeNull();
  });

  it("not installed keeps its own words and Install", () => {
    const b = agentBlocked({ title: "a", provider: "aider" }, logins, "idle")!;
    expect(b.why).toBe("missing");
    expect(agentBlockedLine(b)).toBe("Failed: aider isn't installed");
    expect(blockedAction(b)).toBe("Install");
  });

  it("SidebarRow and Pane only place the shared pieces", () => {
    expect(src("components/sidebar/SidebarRow.tsx")).toContain("<AgentBlockedChip");
    expect(src("components/grid/Pane.tsx")).toContain("<AgentBlockedBanner");
  });
});

describe("the install window ends with a message", () => {
  it("names the next step when the agent was part of it", async () => {
    const { installedMessage } = await import("../components/dialogs/InstallTerminal");
    expect(installedMessage([{ id: "agent-cli", label: "agent CLI (claude)", cmd: "x" }])).toBe(
      "✓ Claude Code installed — Next: Sign in"
    );
    expect(installedMessage([{ id: "packages", label: "system packages: tmux", cmd: "x" }])).toBe(
      "✓ Everything is installed"
    );
  });
});

describe("a passing Test doesn't repeat the line above it", () => {
  it("says Checked just now", () => {
    const c = agent({ account: "me@example.com" });
    const html = renderToStaticMarkup(
      createElement(ConnectionRowView, {
        c,
        deps,
        onChanged: () => {},
        onTest: () => {},
        result: { ok: true, text: "Signed in as me@example.com" },
      })
    );
    expect(html).toContain("Checked just now");
  });
});

describe("orchestrator leftovers", () => {
  it("an extension that loaded reads Loaded, not Connected", () => {
    const html = render({ id: "extension:db", name: "Database", kind: "extension", status: "connected", detail: "", action: null, test: null } as ConnectionRow);
    expect(html).toContain(">Loaded<");
    expect(html).not.toContain(">Connected<");
  });

  it("Other agents stays visible in basic mode (a plain fold, not AdvancedFold)", () => {
    const conns = src("components/settings/screens/Connections.tsx");
    expect(conns).toContain('<details className="pr-advanced set-adv-fold" id="connections-other-agents">');
  });

  it("warns once a flag lets the agent act without asking", async () => {
    const { skipsPermissions } = await import("../components/settings/screens/CodingCli");
    expect(skipsPermissions("--model opus --dangerously-skip-permissions")).toBe(true);
    expect(skipsPermissions("--dangerously-bypass-approvals-and-sandbox")).toBe(true);
    expect(skipsPermissions("--yes-always")).toBe(true);
    expect(skipsPermissions("--model opus")).toBe(false);
    expect(skipsPermissions("")).toBe(false);
    const coding = src("components/settings/screens/CodingCli.tsx");
    expect(coding).not.toContain("Bypass approvals");
    // One wording for the risk, shared with every chip: the literal lives in
    // lib/vocab (round 5), CodingCli re-exports it under its own names.
    const vocab = src("lib/vocab.ts");
    expect(vocab).toContain('SKIP_PERMS_LABEL = "⚠ Skip permission prompts"');
    expect(vocab).toContain('SKIP_PERMS_TITLE = "The agent runs commands and edits files without asking you"');
    expect(coding).toContain("SKIP_PERMISSIONS_LABEL = SKIP_PERMS_LABEL");
    expect(coding).toContain("SKIP_PERMISSIONS_TIP = SKIP_PERMS_TITLE");
    expect(coding).toContain("{SKIP_PERMISSIONS_LABEL}");
    // And the chips themselves wear it (New's launch flags and Settings → Agents).
    const chips = src("components/dialogs/FlagChips.tsx");
    expect(chips).not.toContain('"Skip permissions"');
    expect(chips).toContain("title={p.title}");
  });
});

describe("Setup's API-key link lands on the API-key form", () => {
  it("leaves a one-shot intent for Accounts", async () => {
    const { requestApiKeyForm, takeAccountsIntent } = await import("../components/setup/accountsIntent");
    expect(takeAccountsIntent()).toBeNull();
    requestApiKeyForm("codex");
    expect(takeAccountsIntent()).toEqual({ kind: "api_key", provider: "codex" });
    expect(takeAccountsIntent()).toBeNull(); // applies exactly once
    requestApiKeyForm("");
    expect(takeAccountsIntent()?.provider).toBe("claude");
  });

  it("the link sets the intent before opening Accounts", () => {
    const s = code("components/setup/SetupChecklist.tsx");
    expect(s).toMatch(/requestApiKeyForm\(defaultAgent\);\s*go\("settings", "accounts"\);/);
    expect(s).toContain("Add an API key");
  });

  it("Accounts takes the intent once and opens its add form on the API-key kind", () => {
    const s = code("components/settings/screens/Accounts.tsx");
    expect(s).toContain("useState(takeAccountsIntent)");
    expect(s).toMatch(/useState\(!!intent\)/);
    expect(s).toMatch(/kind: "api_key", provider: intent\.provider/);
  });
});
