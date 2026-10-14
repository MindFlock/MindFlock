/** Capability gating: optional integrations (git, Tailscale, a ticketing
 * source) the app degrades without, rather than breaking.
 *
 * Moved from tests/unit/test_capability_gating.py's bundle checks. The body
 * classes App.tsx stamps from config.caps drive capsGate.css; screens and work
 * panels that need an integration declare it (data-caps-need) and carry a
 * "connect X" gate (data-caps-gate); entry points that cannot exist without
 * git are tagged data-caps="git" and vanish; every git action funnels through
 * requireGit(). */

import { describe, expect, it, vi } from "vitest";
import type { Config } from "../api/types";
import { attr, cssRule, src, tagsWith, tagsWithId } from "./sourceText";

const toast = vi.fn();
vi.mock("../lib/toast", () => ({ toast: (...a: unknown[]) => toast(...a) }));
const { requireGit } = await import("../lib/sessionActions");
const { queryClient } = await import("../state/queries");

const CSS = src("components/capsGate.css");
const APP = src("App.tsx");

describe("the body-class switch", () => {
  it.each(["git", "tailscale", "ticketing"])("stamps body.no-%s from config.caps", (cap) => {
    expect(APP).toContain(`document.body.classList.toggle("no-${cap}", caps ? !caps.${cap} : false);`);
  });

  it("hides tagged entry points and gated content per missing capability", () => {
    expect(cssRule(CSS, 'body.no-git [data-caps~="git"]')).toContain("display: none");
    expect(cssRule(CSS, 'body.no-tailscale [data-caps~="tailscale"]')).toContain("display: none");
    expect(cssRule(CSS, 'body.no-ticketing [data-caps~="ticketing"]')).toContain("display: none");
    expect(cssRule(CSS, ".caps-gate")).not.toBe("");
  });
});

describe("screens and panels declare what they need, and say how to get it", () => {
  it("tags the Workspace screen as needing git, with a git gate linking to its setup", () => {
    expect(src("components/settings/SettingsDialog.tsx")).toContain(
      'data-caps-need={s.key === "workspace" ? "git" : undefined}'
    );
    const ws = src("components/settings/screens/Workspace.tsx");
    expect(tagsWith(ws, "data-caps-gate", "git")).toHaveLength(1);
    // Gate links jump to the screen where the integration is set up.
    expect(attr(tagsWith(ws, "data-goto-screen", "doctor")[0], "onClick")).toContain('gotoScreen("doctor")');
  });

  it("gates Intake's Pull requests and Issues tabs on git (not on a tracker)", () => {
    // They run on their own GitHub sign-in and switches — the pipeline boots
    // for them with no ticket source — so only git can lock them.
    const intake = src("components/intake/IntakeDialog.tsx");
    expect(intake.split('caps: "git",').length - 1).toBe(2);
    for (const tab of ["components/intake/IssuesTab.tsx", "components/intake/PullRequestsTab.tsx"]) {
      expect(tagsWith(src(tab), "data-caps-gate", "git"), tab).toHaveLength(1);
      expect(tagsWith(src(tab), "data-caps-gate", "ticketing"), tab).toHaveLength(0);
    }
  });

  it("gates the sidebar PR-review / issue bars on git alone, matching those tabs", () => {
    const hook = src("components/sidebar/useGithubToggleBar.ts");
    expect(hook).toContain("const capsOk = !(config?.caps && !config.caps.git);");
    expect(hook).not.toContain("caps.ticketing");
  });

  it("tags the git-only entry points so they vanish without git", () => {
    // New's "Works in" choice (separate worktree / this folder directly) is
    // git-only; More options holds the Name too, so it stays.
    const worksIn = tagsWithId(src("components/dialogs/newSession/WorkspaceFold.tsx"), "new-works-in");
    expect(worksIn.map((t) => attr(t, "data-caps"))).toEqual(["git"]);
    // The disk manager's git-only action: the unused-worktree sweep.
    const prune = tagsWithId(src("components/dialogs/RecentDialog.tsx"), "recent-prune");
    expect(prune.map((t) => attr(t, "data-caps"))).toEqual(["git"]);
  });
});

describe("git actions funnel through requireGit()", () => {
  it("refuses, with a toast, when git is missing", () => {
    queryClient.setQueryData<Partial<Config>>(["config"], { caps: { git: false, tailscale: true, ticketing: true, github: true } } as Partial<Config>);
    toast.mockReset();
    expect(requireGit()).toBe(false);
    expect(toast).toHaveBeenCalledTimes(1);
    queryClient.setQueryData<Partial<Config>>(["config"], { caps: { git: true, tailscale: true, ticketing: true, github: true } } as Partial<Config>);
    toast.mockReset();
    expect(requireGit()).toBe(true);
    expect(toast).not.toHaveBeenCalled();
  });

  it("gates the ingestion bar on a ticketing source, and the Diff tab / git menu on git", () => {
    expect(src("components/sidebar/AutomationBar.tsx")).toContain("if (config?.caps && !config.caps.ticketing) return null;");
    expect(src("components/grid/Pane.tsx")).toContain("{caps.git && (");
  });
});
