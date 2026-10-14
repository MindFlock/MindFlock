/** The first-run checklist (components/setup/SetupChecklist.tsx) rendered per
 * state: Get started (3 required) · Connect your work · Optional, later. The vitest environment is node, so views are checked through
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
  // The doctor list's shared query: nothing loaded yet ("Checking dependencies…").
  useDoctor: () => ({ data: undefined, error: null }),
  reprobeDoctor: vi.fn(),
}));

const setupData: { current: SetupStatus | undefined; error?: Error } = { current: undefined };
vi.mock("../components/setup/useSetup", () => ({
  useSetup: () => ({ data: setupData.current, error: setupData.error }),
  refreshSetup: vi.fn(),
  skipSetupStep: vi.fn(),
}));

import { SetupChecklist, progressLine } from "../components/setup/SetupChecklist";
import { STEP_GROUP, STEP_ORDER, STEP_TITLES } from "../components/setup/setupModel";

const render = () => renderToStaticMarkup(createElement(SetupChecklist));

function status(over: Record<string, Partial<SetupStatus["steps"][number]>> = {}, extra: Partial<SetupStatus> = {}): SetupStatus {
  const titles: Record<string, string> = STEP_TITLES;
  const steps = STEP_ORDER.map((id) => {
    const o = over[id] || {};
    const done = !!o.done;
    const skipped = !!o.skipped && !done;
    return {
      id,
      title: titles[id],
      required: id === "tools" || id === "agent_login" || id === "repo",
      group: STEP_GROUP[id],
      done,
      at: null,
      skipped,
      status: done ? "done" : skipped ? "skipped" : "todo",
      ...((o as { started?: boolean }).started ? { started: true } : {}),
    };
  });
  const counted = steps.filter((s) => s.group !== "later");
  const todo = counted.filter((s) => s.status === "todo");
  return {
    steps,
    summary: {
      done: counted.length - todo.length,
      total: counted.length,
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
  it("draws every step in order, in three groups, even before the payload", () => {
    const html = render();
    for (const id of STEP_ORDER) expect(html).toContain('data-step="' + id + '"');
    const order = STEP_ORDER.map((id) => html.indexOf('data-step="' + id + '"'));
    expect(order).toEqual([...order].sort((a, b) => a - b));
    expect(html).toContain("①");
    expect(html).toContain("⑤");
    expect(html).not.toContain("⑥"); // the phone is not a numbered step
    expect(html).toContain("Checking…");
    const groups = ["start", "connect", "later"].map((g) => html.indexOf('data-group="' + g + '"'));
    expect(groups.every((i) => i >= 0)).toBe(true);
    expect(groups).toEqual([...groups].sort((a, b) => a - b));
    expect(html).toContain("Get started");
    expect(html).toContain("Connect your work");
  });

  it("keeps phone/Tailscale out of getting started: folded under Optional, later", () => {
    setupData.current = status(
      {},
      { tailscale: { status: "info", detail: "not found", fix: "brew install --cask tailscale-app", docs: "" } }
    );
    const html = render();
    const later = html.indexOf('<details class="setup-later"');
    expect(later).toBeGreaterThan(0);
    expect(html.slice(later)).toContain("Optional, later");
    // Nothing before the fold mentions the phone or Tailscale.
    const before = html.slice(0, later);
    expect(before).not.toMatch(/phone|Tailscale/i);
    expect(html.slice(later)).toContain('data-step="phone"');
    // The fold is closed: not part of the first look.
    expect(html.slice(later, later + 60)).not.toContain("open");
  });

  it("marks done and skipped steps, and names the next one", () => {
    setupData.current = status({ tools: { done: true }, agent_login: { done: true }, github: { skipped: true } });
    const html = render();
    expect(html).toContain('data-step="tools" data-status="done"');
    expect(html).toContain('data-step="github" data-status="skipped"');
    expect(html).toContain('data-step="repo" data-status="todo"');
    // Until the required three are done the line counts those three.
    expect(html).toContain("2 of 3 done · next: Start your first agent");
    expect(html).toContain("Show again"); // the skipped step can come back
    expect(
      progressLine(status({ tools: { done: true }, agent_login: { done: true }, repo: { done: true } }))
    ).toBe("3 of 3 done · 2 optional left · next: Connect GitHub");
    expect(
      progressLine(
        status({ tools: { done: true }, agent_login: { done: true }, repo: { done: true }, github: { done: true }, tracker: { skipped: true } })
      )
    ).toBe("All set");
  });

  it("highlights one next step, whose button alone is primary", () => {
    setupData.current = status({ tools: { done: true }, agent_login: { done: true } });
    const html = render();
    expect((html.match(/is-next/g) || []).length).toBe(1);
    expect(html).toMatch(/class="setup-step st-todo is-next" data-step="repo"/);
    expect((html.match(/setup-primary/g) || []).length).toBe(1);
    expect(html).toContain("setup-new setup-primary");
    // GitHub is not next: its button is plain.
    expect(html).not.toContain("setup-connect-github setup-primary");
  });

  it("a done step shrinks to its title", () => {
    setupData.current = status({ tools: { done: true }, agent_login: { done: true }, repo: { done: true } });
    const html = render();
    const repo = html.slice(html.indexOf('data-step="repo"'), html.indexOf('data-step="github"'));
    expect(repo).toContain("✓ done");
    expect(repo).not.toContain("+ New session");
    expect(repo).not.toContain("setup-step-body");
  });

  it("step ② is the default agent's card — the same one Settings shows — not a list of every CLI", () => {
    setupData.current = status(
      {},
      {
        logins: [
          { name: "claude", installed: true, logged_in: false, login_cmd: "claude", default: true },
          { name: "codex", installed: true, logged_in: false, login_cmd: "codex login", default: false },
        ],
      }
    );
    const html = render();
    // The card loads its row on mount (Checking… until then); no per-CLI list.
    expect(html).toContain("agent-card-loading");
    expect(html).not.toContain("setup-logins");
    expect(html).not.toContain("Sign in to Codex");
    expect(html).toContain("Using Claude Code");
    expect(html).toContain("setup-change-agent");
  });

  it("step ② says what to do when the default agent isn't installed yet", () => {
    setupData.current = status({}, { logins: [] });
    expect(render()).toContain("Install the tools first — that installs Claude Code.");
    setupData.current = status(
      {},
      { logins: [{ name: "claude", installed: false, logged_in: null, login_cmd: "", default: true }] }
    );
    expect(render()).toContain("Install the tools first");
  });

  it("step ③ says when the session exists but its agent hasn't started yet", () => {
    setupData.current = status({ repo: { started: true } as never });
    expect(render()).toContain("start until you sign in (②)");
    setupData.current = status({ agent_login: { done: true }, repo: { started: true } as never });
    expect(render()).toContain("This ticks once its agent starts on your task.");
  });

  it("step ③ opens New session until the first session exists", () => {
    setupData.current = status();
    expect(render()).toContain("+ New session");
    setupData.current = status({ repo: { done: true } });
    expect(render()).not.toContain("+ New session");
  });

  it("step ④ is GitHub alone: the Connect GitHub dialog, skippable", () => {
    setupData.current = status();
    const html = render();
    const github = html.slice(html.indexOf('data-step="github"'), html.indexOf('data-step="tracker"'));
    expect(github).toContain("setup-connect-github");
    expect(github).toContain("setup-skip");
    // GitHub's one door is the Connect GitHub dialog, not a test of its own.
    expect(html).not.toContain("setup-test-github");
  });

  it("step ⑤ opens Intake to add a ticket source, and can be skipped", () => {
    setupData.current = status();
    const html = render();
    const tracker = html.slice(html.indexOf('data-step="tracker"'), html.indexOf('data-group="later"'));
    expect(tracker).toContain("setup-open-intake");
    expect(tracker).toContain("Add a ticket source");
    expect(tracker).toContain("setup-skip");
    // No second token form: the Intake source form is where a tracker's token goes.
    expect(html).not.toContain("setup-shortcut-token");
  });

  it("the phone offers Install Tailscale with the doctor's hint when it is missing", () => {
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

  it("the phone offers the QR when Tailscale is fine, and is always there", () => {
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
