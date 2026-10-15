/** UX round 5, the Setup surface (worker r5w-setup): the modal-over-drawer
 * rule, Connect GitHub's one primary path, "Check I can push" saying what it
 * checks, the install window's words, and Settings opening on Connections. */

import { describe, expect, it, vi } from "vitest";
import { SOURCES } from "./sourceText";

vi.mock("../api/client", () => ({ api: vi.fn() }));
vi.mock("../lib/toast", () => ({ toast: vi.fn() }));

const src = (p: string) => {
  const text = SOURCES[p];
  if (text === undefined) throw new Error("no such source: " + p);
  return text;
};

describe("a dialog never opens under the narrow sidebar drawer", () => {
  it("every Modal announces itself when it opens", () => {
    const modal = src("components/Modal.tsx");
    expect(modal).toContain('export const MODAL_OPEN_EVENT = "mf:modal-open";');
    // In the mount effect, beside the Escape stack push.
    expect(modal).toMatch(/const pop = pushModal\(token\);\s*window\.dispatchEvent\(new CustomEvent\(MODAL_OPEN_EVENT\)\);/);
  });

  it("the sidebar closes its drawer on it — only when it IS a drawer and open", async () => {
    const { useUi } = await import("../state/store");
    const { closeDrawerForModal } = await import("../components/sidebar/Sidebar");
    useUi.setState({ narrow: true, sidebarHidden: false });
    expect(closeDrawerForModal()).toBe(true);
    expect(useUi.getState().sidebarHidden).toBe(true);
    // Already closed: nothing to do.
    expect(closeDrawerForModal()).toBe(false);
    // A wide window's sidebar is a column, not an overlay: left alone.
    useUi.setState({ narrow: false, sidebarHidden: false });
    expect(closeDrawerForModal()).toBe(false);
    expect(useUi.getState().sidebarHidden).toBe(false);
    const sidebar = src("components/sidebar/Sidebar.tsx");
    expect(sidebar).toMatch(/window\.addEventListener\(MODAL_OPEN_EVENT, on\)/);
  });
});

describe("Connect GitHub: one primary path", () => {
  it("device flow configured → one Sign in with GitHub, the token folded, no gh route", async () => {
    const { connectLayout } = await import("../components/dialogs/ConnectGitHub");
    expect(
      connectLayout({ connected: false, source: "none", methods: ["device", "gh", "token"], gh: { installed: true, authenticated: false } })
    ).toEqual({ primary: "device", tokenFolded: true, ghFolded: false });
  });

  it("not configured → the numbered token steps lead; gh folds under Other ways to connect", async () => {
    const { connectLayout } = await import("../components/dialogs/ConnectGitHub");
    expect(
      connectLayout({ connected: false, source: "none", methods: ["gh", "token"], gh: { installed: true, authenticated: false } })
    ).toEqual({ primary: "token", tokenFolded: false, ghFolded: true });
    // No gh on this computer: just the token steps.
    expect(connectLayout({ connected: false, source: "none", methods: ["token"] })).toEqual({
      primary: "token",
      tokenFolded: false,
      ghFolded: false,
    });
    const dlg = src("components/dialogs/ConnectGitHub.tsx");
    expect(dlg).toContain("<summary>Other ways to connect</summary>");
    expect(dlg).not.toContain("Or sign in with the GitHub CLI");
  });

  it("the gh window says what the terminal will ask, in order", async () => {
    const { GH_CLI_STEPS } = await import("../components/dialogs/ConnectGitHub");
    expect(GH_CLI_STEPS[0]).toContain("Authenticate Git with your GitHub credentials?");
    expect(GH_CLI_STEPS[1]).toContain("one-time code");
  });

  it("after ✓ Connected there is a Done that closes the dialog (back to Setup, ④ ticked)", () => {
    const dlg = src("components/dialogs/ConnectGitHub.tsx");
    expect(dlg).toContain('id="gh-done"');
    expect(dlg).toContain("<ConnectGitHub onDone={() => setOpen(false)} />");
  });

  it("Check I can push names the repo and remote BEFORE the click, and a command fix goes in a Copy box", async () => {
    const { pushTargetLine, isCommand } = await import("../components/dialogs/ConnectGitHub");
    expect(pushTargetLine({ repo: "/home/me/repo", name: "repo", remote: "git@github.com:me/repo.git" })).toBe(
      "Checks repo → git@github.com:me/repo.git"
    );
    expect(pushTargetLine({ repo: "/home/me/repo", name: "repo", remote: "" })).toBe(
      "Checks repo (it has no remote called origin yet)"
    );
    expect(pushTargetLine(undefined)).toContain("once a session has opened one");
    expect(isCommand("git remote add origin <url>")).toBe(true);
    expect(isCommand("Add an SSH key to GitHub, or switch the remote to HTTPS.")).toBe(false);
  });

});

describe("the install window", () => {
  it("names the agent while downloading, and the next step after", async () => {
    const { installProgress, installedMessage, installButtonLabel } = await import("../components/dialogs/InstallTerminal");
    const agent = { id: "agent-cli", label: "agent CLI (claude)", cmd: "curl -fsSL https://claude.ai/install.sh | bash" };
    expect(installButtonLabel([agent])).toBe("Install Claude Code");
    expect(installProgress([agent], 12)).toBe("Downloading Claude Code… 0:12 — can take a few minutes");
    expect(installedMessage([agent])).toBe("✓ Claude Code installed — Next: Sign in");
    const pkgs = { id: "packages", label: "system packages: tmux", cmd: "sudo apt-get install -y tmux" };
    expect(installProgress([pkgs, agent], 5)).toBe("Installing tmux, Claude Code… 0:05 — type your password if asked");
  });

  it("stays open on success with a Sign in button, instead of closing itself", () => {
    const win = src("components/dialogs/InstallTerminal.tsx");
    expect(win).toContain('id="install-signin"');
    expect(win).not.toContain("toast(installedMessage(planned))");
    // The checklist keeps step ①'s body (and so this window) mounted while it is up.
    expect(src("components/setup/SetupChecklist.tsx")).toContain("keepBody={installOpen}");
  });

  it("login terminals are dark whatever the theme (the CLI's highlighted option stays readable)", () => {
    for (const f of ["components/dialogs/AgentSignIn.tsx", "components/dialogs/InstallTerminal.tsx", "components/dialogs/ConnectGitHub.tsx"])
      expect(src(f), f).toContain("prov-login-term login-term-dark");
    expect(src("components/dialogs/ConnectGitHub.css")).toMatch(/\.login-term-dark \.xterm-viewport/);
  });
});

describe("Settings", () => {
  it("opens on Connections when nothing names a screen", async () => {
    const { DEFAULT_SETTINGS_SCREEN } = await import("../lib/openSettings");
    expect(DEFAULT_SETTINGS_SCREEN).toBe("connections");
  });

  it("Agents: the install path sits next to the picker, not under Advanced", () => {
    const coding = src("components/settings/screens/CodingCli.tsx");
    expect(coding).toContain('"Not installed — how to install"');
    const fold = coding.indexOf('id="coding-other-agent-fold"');
    const adv = coding.indexOf('<AdvancedFold id="coding-advanced">');
    expect(fold).toBeGreaterThan(-1);
    expect(fold).toBeLessThan(adv);
    expect(coding.slice(adv, adv + 120)).not.toContain("coding-other-agent");
  });

  it("Your devices: Add a device comes before the identity form", () => {
    const dev = src("components/settings/screens/Devices.tsx");
    expect(dev.indexOf("<h4 className=\"set-subtitle\">Add a device</h4>")).toBeLessThan(dev.indexOf("<PersonCard />"));
  });

  it("a Tailscale issue never repeats its fix as backticked prose", async () => {
    const { plainIssueMessage } = await import("../components/settings/screens/TailscaleCard");
    const fix = "sudo systemctl enable --now tailscaled";
    expect(plainIssueMessage("Tailscale is installed but isn't answering — start it with `" + fix + "`.", fix)).toBe(
      "Tailscale is installed but isn't answering."
    );
    expect(plainIssueMessage("Run `tailscale up` first.")).toBe("Run tailscale up first.");
  });
});
