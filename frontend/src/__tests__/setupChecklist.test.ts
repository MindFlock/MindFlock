/** The five-step checklist (components/setup/SetupChecklist.tsx) rendered per
 * state. The vitest environment is node, so views are checked through
 * react-dom/server with the server query mocked. */

import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { SetupStatus } from "../api/types";

vi.mock("../api/client", () => ({ api: vi.fn() }));
vi.mock("../lib/toast", () => ({ toast: vi.fn() }));
vi.mock("../state/queries", () => ({
  queryClient: { invalidateQueries: vi.fn(), setQueryData: vi.fn() },
  useConfig: () => ({ data: undefined }),
  useInstances: () => ({ data: undefined }),
}));

const setupData: { current: SetupStatus | undefined; error?: Error } = { current: undefined };
vi.mock("../components/setup/useSetup", () => ({
  useSetup: () => ({ data: setupData.current, error: setupData.error }),
  refreshSetup: vi.fn(),
  skipSetupStep: vi.fn(),
}));

import { SetupChecklist, progressLine } from "../components/setup/SetupChecklist";
import { STEP_ORDER } from "../components/setup/setupModel";

const render = () => renderToStaticMarkup(createElement(SetupChecklist));

function status(over: Record<string, Partial<SetupStatus["steps"][number]>> = {}, extra: Partial<SetupStatus> = {}): SetupStatus {
  const titles: Record<string, string> = {
    tools: "Engine and tools",
    agent_login: "Sign in to your agent",
    repo: "Pick a repo",
    tracker: "Connect a tracker and GitHub",
    phone: "Your phone",
  };
  const steps = STEP_ORDER.map((id) => {
    const o = over[id] || {};
    const done = !!o.done;
    const skipped = !!o.skipped && !done;
    return {
      id,
      title: titles[id],
      required: id === "tools" || id === "agent_login" || id === "repo",
      done,
      at: null,
      skipped,
      status: done ? "done" : skipped ? "skipped" : "todo",
    };
  });
  const todo = steps.filter((s) => s.status === "todo");
  return {
    steps,
    summary: {
      done: 5 - todo.length,
      total: 5,
      required_done: steps.filter((s) => s.required).every((s) => s.done),
      complete: !todo.length,
      next: todo[0]?.id || "",
    },
    doctor: null,
    tailscale: null,
    logins: [],
    default_provider: "claude",
    report: "",
    ...extra,
  };
}

beforeEach(() => {
  setupData.current = undefined;
  setupData.error = undefined;
});

describe("SetupChecklist", () => {
  it("draws all five steps, numbered, in order, even before the payload", () => {
    const html = render();
    for (const id of STEP_ORDER) expect(html).toContain('data-step="' + id + '"');
    const order = STEP_ORDER.map((id) => html.indexOf('data-step="' + id + '"'));
    expect(order).toEqual([...order].sort((a, b) => a - b));
    expect(html).toContain("①");
    expect(html).toContain("⑤");
    expect(html).toContain("Checking…");
  });

  it("marks done and skipped steps, and names the next one", () => {
    setupData.current = status({ tools: { done: true }, agent_login: { done: true }, phone: { skipped: true } });
    const html = render();
    expect(html).toContain('data-step="tools" data-status="done"');
    expect(html).toContain('data-step="phone" data-status="skipped"');
    expect(html).toContain('data-step="repo" data-status="todo"');
    expect(html).toContain("3 of 5 done · next: Pick a repo");
    expect(html).toContain("Show again"); // the skipped step can come back
    expect(progressLine(status({ tools: { done: true }, agent_login: { done: true }, repo: { done: true }, tracker: { skipped: true }, phone: { done: true } }))).toBe("All set");
  });

  it("step ② offers one sign-in per INSTALLED agent CLI and shows who is signed in", () => {
    setupData.current = status(
      {},
      {
        logins: [
          { name: "claude", installed: true, logged_in: true, login_cmd: "claude", default: true },
          { name: "codex", installed: true, logged_in: false, login_cmd: "codex login", default: false },
          { name: "aider", installed: false, logged_in: null, login_cmd: "", default: false },
        ],
      }
    );
    const html = render();
    expect(html).toContain('data-provider="claude"');
    expect(html).toContain('data-provider="codex"');
    expect(html).not.toContain('data-provider="aider"');
    expect(html).toContain("✓ signed in");
    // A signed-in CLI gets no button; the signed-out one gets its own.
    expect((html.match(/Sign in to Codex/g) || []).length).toBe(1);
    expect(html).not.toContain("Sign in to Claude");
  });

  it("step ② says what to do when no agent CLI is installed yet", () => {
    setupData.current = status({}, { logins: [] });
    expect(render()).toContain("No agent CLI is installed yet");
  });

  it("step ③ opens New session until the first session exists", () => {
    setupData.current = status();
    expect(render()).toContain("+ New session");
    setupData.current = status({ repo: { done: true } });
    const html = render();
    expect(html).not.toContain("+ New session");
    expect(html).toContain("Your first session was created");
  });

  it("step ④ links to Settings → Connections and can be skipped", () => {
    setupData.current = status();
    const html = render();
    expect(html).toContain("Open Settings → Connections");
    const tracker = html.slice(html.indexOf('data-step="tracker"'), html.indexOf('data-step="phone"'));
    expect(tracker).toContain("setup-skip");
    // GitHub's one door is the Connect GitHub dialog, not a test of its own.
    expect(tracker).toContain("setup-connect-github");
    expect(tracker).not.toContain("setup-test-github");
    expect(tracker).toContain("setup-shortcut-token");
  });

  it("step ⑤ offers Install Tailscale with the doctor's hint when it is missing", () => {
    setupData.current = status(
      {},
      { tailscale: { status: "warn", detail: "not found", fix: "curl -fsSL https://tailscale.com/install.sh | sh", docs: "https://tailscale.com/download" } }
    );
    const html = render();
    expect(html).toContain('data-phone="install-tailscale"');
    expect(html).toContain("Install Tailscale");
    expect(html).toContain("tailscale.com/install.sh");
    expect(html).not.toContain("Show QR");
  });

  it("step ⑤ offers the QR when Tailscale is fine, and is always visible", () => {
    setupData.current = status({}, { tailscale: { status: "ok", detail: "running", fix: "", docs: "" } });
    const html = render();
    expect(html).toContain('data-phone="qr"');
    expect(html).toContain("Show QR");
    expect(html).toContain('data-step="phone"');
  });

  it("shows the first-run report the server printed at boot, when there is one", () => {
    setupData.current = status({}, { report: "First run — here is where this machine actually stands:" });
    expect(render()).toContain("where this machine actually stands");
    setupData.current = status();
    expect(render()).not.toContain("What the server found at first start");
  });

  it("reports a failed status read instead of a blank card", () => {
    setupData.error = new Error("/api/setup -> 500");
    expect(render()).toContain("setup status failed");
  });
});
