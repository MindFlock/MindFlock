/// <reference types="vite/client" />
import { describe, it, expect } from "vitest";
import { agentDisplayName, signInTarget } from "../components/dialogs/AgentSignIn";
import { asksForPassword, installButtonLabel } from "../components/dialogs/InstallTerminal";
import { consumeSetupIntent } from "../components/dialogs/SetupDialog";
import { lastSlide } from "../components/onboarding/WelcomeTour";

describe("signInTarget (Sign in to <agent>)", () => {
  const row = { id: "agent-auth", status: "warn", cmd: "claude", provider: "claude" };

  it("offers a sign-in for an agent-auth row that found no login", () => {
    expect(signInTarget(row)).toBe("claude");
  });

  it("never for a CLI that declares no login flow (no cmd)", () => {
    // aider reads API keys: a button would only drop the user into its REPL.
    expect(signInTarget({ ...row, cmd: "" })).toBeNull();
  });

  it("never once signed in, for other rows, or without a provider", () => {
    expect(signInTarget({ ...row, status: "ok" })).toBeNull();
    expect(signInTarget({ ...row, status: "info" })).toBeNull();
    expect(signInTarget({ ...row, id: "agent-cli" })).toBeNull();
    expect(signInTarget({ ...row, provider: "" })).toBeNull();
    expect(signInTarget(undefined)).toBeNull();
  });

  it("labels the button with the agent's name", () => {
    expect(agentDisplayName("claude")).toBe("Claude");
    expect(agentDisplayName("codex")).toBe("Codex");
  });
});

describe("installButtonLabel / asksForPassword", () => {
  const pkgs = { id: "packages", label: "system packages: tmux", cmd: "sudo apt-get install -y tmux" };
  const agent = {
    id: "agent-cli",
    label: "agent CLI (claude)",
    cmd: "curl -fsSL https://claude.ai/install.sh | bash",
  };

  it("names what one click installs", () => {
    expect(installButtonLabel([pkgs, agent])).toBe("Install tmux + claude");
    expect(
      installButtonLabel([{ id: "homebrew", label: "Homebrew (the macOS package manager)", cmd: "x" }, pkgs])
    ).toBe("Install Homebrew + tmux");
  });

  it("falls back to the generic label when the list is long", () => {
    const many = { ...pkgs, label: "system packages: tmux, git, bubblewrap" };
    expect(installButtonLabel([many, agent])).toBe("Install everything missing");
    expect(installButtonLabel([])).toBe("Install everything missing");
  });

  it("warns about the one password prompt only when there is one", () => {
    expect(asksForPassword([pkgs, agent])).toBe(true);
    expect(asksForPassword([agent])).toBe(false);
  });
});

describe("consumeSetupIntent (the desktop's first-run hand-off)", () => {
  const fake = (search: string) => {
    const calls: string[] = [];
    return {
      loc: { search, pathname: "/", hash: "" },
      hist: { replaceState: (_d: unknown, _u: string, url?: string) => calls.push(url || "") },
      calls,
    };
  };

  it("asks for Setup once and strips the parameter", () => {
    const f = fake("?setup=install&x=1");
    expect(consumeSetupIntent(f.loc, f.hist)).toBe(true);
    expect(f.calls).toEqual(["/?x=1"]);
  });

  it("leaves every other URL alone", () => {
    const f = fake("?token=abc");
    expect(consumeSetupIntent(f.loc, f.hist)).toBe(false);
    expect(f.calls).toEqual([]);
  });
});

describe("the tour's last slide", () => {
  it("doesn't say 'all set' while the doctor reports something missing", () => {
    expect(lastSlide(false).title).toBe("You're all set");
    expect(lastSlide(true).title).toBe("One thing left");
  });
});
