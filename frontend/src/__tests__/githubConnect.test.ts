/// <reference types="vite/client" />
/** One GitHub credential path: the Intake tabs no longer render a raw token
 * field or their own "Test GitHub", both open the Connect GitHub dialog, and
 * the dialog's Test line is derived from the server's real /user + repo
 * check. The source-level pins read the .tsx files (vitest runs without a
 * DOM here); the Python suite has the same pins so a bundle-only change
 * cannot hide a regression. */
import { describe, it, expect } from "vitest";
import prSrc from "../components/intake/PullRequestsTab.tsx?raw";
import issuesSrc from "../components/intake/IssuesTab.tsx?raw";
import dialogSrc from "../components/dialogs/ConnectGitHub.tsx?raw";
import appSrc from "../App.tsx?raw";
import connsSrc from "../components/settings/screens/Connections.tsx?raw";
import {
  DEVICE_FLOW_UNCONFIGURED,
  OPEN_CONNECT_GITHUB_EVENT,
  describeConnectionTest,
} from "../lib/githubConnect";

const SOURCES: Record<string, string> = {
  "components/intake/PullRequestsTab.tsx": prSrc,
  "components/intake/IssuesTab.tsx": issuesSrc,
  "components/dialogs/ConnectGitHub.tsx": dialogSrc,
  "App.tsx": appSrc,
  "components/settings/screens/Connections.tsx": connsSrc,
};
const src = (rel: string) => SOURCES[rel] ?? "";

describe("Intake tabs: one door to the credential", () => {
  for (const rel of ["components/intake/PullRequestsTab.tsx", "components/intake/IssuesTab.tsx"]) {
    it(`${rel} renders no raw token field and no Test GitHub`, () => {
      const s = src(rel);
      expect(s).not.toContain('field="token"');
      expect(s).not.toContain("Test GitHub");
      expect(s).not.toContain("runGithubTest");
      expect(s).toContain("openConnectGitHub");
      expect(s).toContain("Connect GitHub");
    });
  }

  it("the Issues tab no longer sends people to the Pull requests tab for the token", () => {
    expect(src("components/intake/IssuesTab.tsx")).not.toContain('gotoTab("prs")');
  });
});

describe("ConnectGitHub dialog", () => {
  it("is mounted once and opened by the window event", () => {
    const dialog = src("components/dialogs/ConnectGitHub.tsx");
    expect(dialog).toContain("export function ConnectGitHubDialog");
    expect(dialog).toContain("OPEN_CONNECT_GITHUB_EVENT");
    expect(src("App.tsx")).toContain("<ConnectGitHubDialog />");
    expect(OPEN_CONNECT_GITHUB_EVENT).toBe("mf:open-connect-github");
  });

  it("Connections' Configure opens it for the GitHub row", () => {
    const conns = src("components/settings/screens/Connections.tsx");
    expect(conns).toContain('configure_dialog === "connect-github"');
    expect(conns).toContain("openConnectGitHub");
  });

  it("explains a missing device flow instead of showing a dead button", () => {
    const dialog = src("components/dialogs/ConnectGitHub.tsx");
    expect(dialog).toContain("DEVICE_FLOW_UNCONFIGURED");
    expect(DEVICE_FLOW_UNCONFIGURED).toBe(
      "Device flow is not configured in this build; paste a token or use gh"
    );
  });

  it("Test calls the real connection test", () => {
    expect(src("components/dialogs/ConnectGitHub.tsx")).toContain('"/api/settings/test/github"');
  });
});

describe("describeConnectionTest", () => {
  it("reads a full pass: who, where from, repo reach, gh as an optional extra", () => {
    const t = describeConnectionTest({
      ok: true,
      token_source: "settings",
      login: "octo",
      repo: "org/one",
      repo_ok: true,
      can_push: true,
      gh_installed: true,
      gh_authenticated: false,
    });
    expect(t.ok).toBe(true);
    expect(t.msg).toBe("@octo via settings · org/one reachable (can push) · gh installed, not signed in");
  });

  it("a read-only repo says so", () => {
    const t = describeConnectionTest({ ok: true, token_source: "gh-cli", login: "octo", repo: "o/r", repo_ok: true });
    expect(t.msg).toContain("read-only");
  });

  it("an unreachable repo is a red ✗ with the server's reason", () => {
    const t = describeConnectionTest({
      ok: false,
      token_source: "settings",
      login: "octo",
      repo: "o/private",
      repo_ok: false,
      repo_error: "no such repo, or this token cannot see it",
    });
    expect(t.ok).toBe(false);
    expect(t.msg).toContain("o/private: no such repo");
  });

  it("no token is the server's sentence, not a blame on gh", () => {
    const t = describeConnectionTest({ ok: false, token_source: "none", detail: "no GitHub token — connect with the button above" });
    expect(t.ok).toBe(false);
    expect(t.msg).toBe("no GitHub token — connect with the button above");
    expect(t.msg).not.toContain("gh");
  });

  it("a rejected token shows GitHub's verdict", () => {
    const t = describeConnectionTest({ ok: false, token_source: "settings", login: "", user_error: "GitHub doesn't accept this token (expired or revoked)" });
    expect(t.msg).toContain("expired or revoked");
  });

  it("no repository configured is still a pass", () => {
    const t = describeConnectionTest({ ok: true, token_source: "settings", login: "octo", repo: "" });
    expect(t.ok).toBe(true);
    expect(t.msg).toContain("no repository configured yet");
  });

  it("survives a null payload", () => {
    expect(describeConnectionTest(null).ok).toBe(false);
  });
});
