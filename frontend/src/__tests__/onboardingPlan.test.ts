/// <reference types="vite/client" />
import { describe, it, expect } from "vitest";
import {
  STEP_ORDER,
  currentStep,
  flowFinished,
  hasSyncedAgentStep,
  needsIdentity,
  orderedSteps,
  pollDelay,
  primaryGithubMethod,
  pushFailedNote,
  readinessLine,
  stepNumber,
  visibleSteps,
  type OnboardingPlan,
} from "../lib/onboarding";

const step = (id: string, status: string, extra: Record<string, unknown> = {}) =>
  ({ id, title: id, status, reason: id + " " + status, ...extra }) as never;

describe("Setup's step order (GET /api/onboarding)", () => {
  it("asks the join question before the agent and GitHub steps", () => {
    expect(STEP_ORDER).toEqual(["deps", "devices", "agent", "tailscale", "github", "repo"]);
    expect(STEP_ORDER.indexOf("devices")).toBeLessThan(STEP_ORDER.indexOf("agent"));
    expect(STEP_ORDER.indexOf("devices")).toBeLessThan(STEP_ORDER.indexOf("github"));
  });

  it("draws a shuffled payload in the fixed order", () => {
    const plan: OnboardingPlan = {
      steps: [step("repo", "todo"), step("github", "ok"), step("deps", "ok"), step("devices", "todo")],
      next: "",
      done: false,
    };
    expect(orderedSteps(plan).map((s) => s.id)).toEqual(["deps", "devices", "github", "repo"]);
    expect(currentStep(plan)).toBe("devices");
  });

  it("hides Tailscale until going multi-device, and numbers what it shows", () => {
    const first: OnboardingPlan = {
      steps: STEP_ORDER.map((id) => step(id, id === "tailscale" ? "skip" : "ok")),
      next: "",
      done: true,
    };
    const shown = visibleSteps(first);
    expect(shown).toEqual(["deps", "devices", "agent", "github", "repo"]);
    expect(stepNumber("github", shown)).toBe("④");
    const joining: OnboardingPlan = {
      steps: STEP_ORDER.map((id) => step(id, id === "agent" || id === "github" ? "skip" : "todo")),
      next: "tailscale",
      done: false,
    };
    expect(visibleSteps(joining)).toContain("tailscale");
    expect(stepNumber("tailscale", visibleSteps(joining))).toBe("④");
    // Joining: the agent and GitHub steps stay on screen, with their reason.
    expect(visibleSteps(joining)).toContain("agent");
    expect(currentStep(joining)).toBe("tailscale");
  });

  it("before the plan loads, every step but Tailscale", () => {
    expect(visibleSteps(null)).toEqual(["deps", "devices", "agent", "github", "repo"]);
    expect(currentStep(null)).toBe("");
  });
});

describe("Connect GitHub", () => {
  it("offers the device flow, then gh, then a token", () => {
    expect(primaryGithubMethod({ connected: false, source: "none", methods: ["device", "gh", "token"] })).toBe(
      "device"
    );
    expect(primaryGithubMethod({ connected: false, source: "none", methods: ["gh", "token"] })).toBe("gh");
    expect(primaryGithubMethod({ connected: false, source: "none", methods: ["token"] })).toBe("token");
    expect(primaryGithubMethod(null)).toBe("token");
  });

  it("asks for the git identity only when it's missing", () => {
    expect(needsIdentity({ connected: true, source: "settings", identity: { name: "a", email: "" } })).toBe(true);
    expect(needsIdentity({ connected: true, source: "settings", identity: { name: "a", email: "b@c" } })).toBe(
      false
    );
  });

  it("polls at GitHub's interval and stops when the flow ends", () => {
    const f = { state: "pending", user_code: "X", verification_uri: "", expires_at: 0, interval: 5, login: "", error: "" };
    expect(flowFinished(f)).toBe(false);
    expect(pollDelay(f)).toBe(5);
    expect(pollDelay({ ...f, interval: 0 })).toBe(5);
    expect(pollDelay({ ...f, interval: 1 })).toBe(2);
    expect(flowFinished({ ...f, state: "done" })).toBe(true);
  });

  it("points a failed push at the fix", () => {
    const n = pushFailedNote("[1] api", { reason: "https_auth", message: "git has no GitHub sign-in" });
    expect(n.setup).toBe(true);
    expect(n.text).toBe("Push failed on [1] api: git has no GitHub sign-in — Connect GitHub in Setup");
    const ssh = pushFailedNote("api", { reason: "ssh_auth", message: "no SSH key", fix: "add your key" });
    expect(ssh.setup).toBe(false);
    expect(ssh.text).toContain("— add your key");
  });
});

describe("Devices: readiness and synced agents", () => {
  it("says ready, or what's missing on that computer", () => {
    expect(readinessLine({ ready: true })?.text).toBe("ready to work");
    const r = readinessLine({
      missing: ["tmux"],
      agent: { provider: "codex", signed_in: false },
      push: { ok: false, message: "" },
      tailscale: { running: true, key_expiry_days: 4, key_expired: false, key_warn: true },
    });
    expect(r?.warn).toBe(true);
    expect(r?.text).toBe("missing tmux · codex not signed in · can't push · Tailscale key expires in 4d");
    expect(readinessLine({ error: "offline" })?.text).toBe("readiness unknown — offline");
    expect(readinessLine(undefined)).toBeNull();
  });

  it("offers Install only when the plan carries a synced agent CLI", () => {
    expect(hasSyncedAgentStep([{ id: "packages" }, { id: "synced-codex-cli" }])).toBe(true);
    expect(hasSyncedAgentStep([{ id: "agent-cli" }])).toBe(false);
    expect(hasSyncedAgentStep(undefined)).toBe(false);
  });
});
