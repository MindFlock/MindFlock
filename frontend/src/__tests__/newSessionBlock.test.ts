/** The new-session block's round-12 folds on screen (server-rendered, mocked
 * API; docs/flights.md "As built: round 12 new block"): More options folded
 * with its summary line, open with the New dialog's choices as chips (never a
 * <select>), the From a session picker, and the body a fork sends — through
 * the New dialog's own builder and the same POST /api/instances. */

/// <reference types="vite/client" />
import { createElement, type ReactElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { code } from "./sourceText";
import type { FlightPlan } from "../lib/flights";

const api = vi.fn((..._args: unknown[]): Promise<unknown> => new Promise(() => {}));
vi.mock("../api/client", () => ({ api: (...a: unknown[]) => api(...a), ApiError: class extends Error {} }));
vi.mock("../lib/toast", () => ({ toast: vi.fn() }));

const { QueryClient, QueryClientProvider } = await import("@tanstack/react-query");
const { NewSessionBlock, blockBody, postInstance } = await import("../components/flights/NewSessionBlock");
const { ParentPicker } = await import("../components/flights/newBlockOptions");
const { defaultOptions, parentChoices } = await import("../lib/newBlock");

(globalThis as Record<string, unknown>).document ??= {
  hidden: false,
  addEventListener: () => {},
  removeEventListener: () => {},
  getElementById: () => null,
  querySelector: () => null,
};

const PLANS = [{ id: "builtin:ticket", name: "Ticket to merge", builtin: true }] as FlightPlan[];

function withClient(el: ReactElement, prime: (c: InstanceType<typeof QueryClient>) => void = () => {}): string {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false, staleTime: Infinity } } });
  prime(client);
  return renderToStaticMarkup(createElement(QueryClientProvider, { client }, el));
}

const stubMore = (open: boolean) =>
  vi.stubGlobal("localStorage", { getItem: (k: string) => (k === "mf_sky_new_more" && open ? "1" : null), setItem: () => {}, removeItem: () => {} });

const prime = (c: InstanceType<typeof QueryClient>) => {
  c.setQueryData(["providers"], { providers: [{ name: "claude" }], default: "claude" });
  c.setQueryData(["settings"], { coding_cli: { default_launch_args: { claude: "--model opus" } } });
  c.setQueryData(["auth-profiles"], {
    profiles: [{ id: "or", label: "OpenRouter", kind: "openrouter", model: "x/y" }],
    default_profile: "",
  });
  c.setQueryData(["devices"], { devices: [{ device: "mac.ts.net", host: "mac", connected: true }] });
  c.setQueryData(["instances"], [{ title: "r12", path: "/code/app", branch: "mf/r12", provider: "claude" }]);
};

const card = () => withClient(createElement(NewSessionBlock, { x: 0, y: 0, plans: PLANS, onCreated: () => {}, onCancel: () => {} }), prime);

beforeEach(() => {
  api.mockClear();
});
afterEach(() => vi.unstubAllGlobals());

describe("More options", () => {
  it("folded by default: its head, From a session's head, and the summary line — and nothing of the fold", () => {
    stubMore(false);
    const html = card();
    expect(html).toMatch(/<button type="button" class="fl-icon-btn sky-new-fold sky-new-more" aria-expanded="false">.*More options<\/button>/);
    expect(html).toMatch(/class="fl-icon-btn sky-new-fold sky-new-from"[^>]*aria-expanded="false"/);
    expect(html).toContain('<p class="sky-new-summary" title="More options">claude · opus · worktree</p>');
    expect(html).not.toContain("sky-new-more-body");
    // The block's own rows are as they were, and the footer too.
    expect(html).toContain('class="fl-prompt sky-new-prompt"');
    expect(html).toContain(">Cancel</button>");
    expect(html).toMatch(/<button[^>]*disabled=""[^>]*>Fly<\/button>/);
  });

  it("open (remembered): account, workspace, base, launch flags pre-filled from the agent's defaults, device — chips, never a <select>", () => {
    stubMore(true);
    const html = card();
    expect(html).toContain('aria-expanded="true">');
    expect(html).not.toContain("sky-new-summary");
    expect(html).toContain('class="sky-new-more-body"');
    const account = html.slice(html.indexOf('class="fl-chiprow sky-new-account"'));
    expect([...account.matchAll(/data-value="([^"]*)">([^<]+)</g)].slice(0, 3).map((m) => m[2])).toEqual(["App default", "CLI&#x27;s own login", "OpenRouter"]);
    expect(html).toMatch(/class="fl-chiprow sky-new-workspace"[^]*?aria-checked="true" class="fl-pick on"[^>]*data-value="worktree">Worktree</);
    expect(html).toMatch(/class="fl-chiprow sky-new-base"[^]*?aria-checked="true" class="fl-pick on fl-pick-default"[^>]*>Default</);
    expect(html).toMatch(/class="sky-new-input sky-new-args"[^>]*value="--model opus"/);
    expect(html).toMatch(/class="fl-chiprow sky-new-device"[^]*?data-value="">This device<[^]*?data-value="mac.ts.net">mac</);
    expect(html).not.toContain("<select");
  });

  it("every option reaches the body through the New dialog's builder", () => {
    const body = blockBody({
      repo: "/code/app",
      prompt: "fix it",
      program: "claude",
      launchArgs: "--model opus --verbose",
      options: { ...defaultOptions(), profileId: "or", profileModel: "x/z", baseBranch: "origin/dev", baseRemote: true, template: "T" },
    });
    expect(body).toEqual({
      title: "",
      program: "claude",
      repo_path: "/code/app",
      prompt: "fix it",
      launch_args: ["--model", "opus", "--verbose"],
      profile_id: "or",
      profile_model: "x/z",
      init_repo: false,
      in_place: false,
      base_ref: "origin/dev",
      base_branch: "dev",
    });
    const inPlace = blockBody({ repo: "/code/app", prompt: "", program: "codex", launchArgs: "", options: { ...defaultOptions(), inPlace: true, baseBranch: "main" } });
    expect(inPlace).toMatchObject({ in_place: true, launch_args: [] });
    expect(inPlace).not.toHaveProperty("base_ref");
    // Another device signs in with its own accounts: none rides along.
    const away = blockBody({ repo: "/code/app", prompt: "", program: "claude", launchArgs: "", options: { ...defaultOptions(), device: "mac", profileId: "or" } });
    expect(away).not.toHaveProperty("profile_id");
  });
});

describe("From a session", () => {
  const rows = [
    { title: "r12", path: "/code/app", branch: "mf/r12", provider: "claude" },
    { title: "web", path: "/code/web", branch: "main", provider: "codex" },
  ];

  it("the picker lists the sessions by name as chips, the picked one on; a filter only past eight", () => {
    const html = renderToStaticMarkup(createElement(ParentPicker, { choices: parentChoices(rows), value: "web", onPick: () => {} }));
    expect(html).toContain('role="radiogroup" aria-label="Fork from a session"');
    expect(html).toMatch(/aria-checked="false" class="fl-pick" data-value="r12" title="\/code\/app · mf\/r12">r12</);
    expect(html).toMatch(/aria-checked="true" class="fl-pick on" data-value="web"/);
    expect(html).not.toContain("sky-new-parent-filter");
    const many = Array.from({ length: 9 }, (_, i) => ({ title: "s" + i, path: "/c", branch: "b" }));
    expect(renderToStaticMarkup(createElement(ParentPicker, { choices: parentChoices(many), value: "", onPick: () => {} }))).toContain(
      'placeholder="Filter 9 sessions…"'
    );
    expect(renderToStaticMarkup(createElement(ParentPicker, { choices: [], value: "", onPick: () => {} }))).toContain("No sessions running on this device");
  });

  it("a fork's body: the parent's repository, a worktree cut from its branch, parent + spawned, the worker title — POSTed like any other", async () => {
    const body = blockBody({
      repo: "/somewhere/else",
      prompt: "help r12",
      program: "claude",
      launchArgs: "",
      // In place and another device were picked before the fork: the fork wins.
      options: { ...defaultOptions(), inPlace: true, device: "mac", baseBranch: "dev" },
      title: "r12-w1",
      parent: { title: "r12", repo: "/code/app", branch: "mf/r12" },
    });
    expect(body).toEqual({
      title: "r12-w1",
      program: "claude",
      repo_path: "/code/app",
      prompt: "help r12",
      launch_args: [],
      init_repo: false,
      in_place: false,
      base_ref: "mf/r12",
      base_branch: "mf/r12",
      parent: "r12",
      spawned: true,
    });
    api.mockResolvedValueOnce({ title: "r12-w1" });
    await expect(postInstance(body, "")).resolves.toBe("r12-w1");
    expect(api).toHaveBeenCalledWith("/api/instances", { json: body });
    // On another device the same body goes there, and comes back namespaced.
    api.mockResolvedValueOnce({ title: "x" });
    await expect(postInstance({ title: "x" }, "mac")).resolves.toBe("mac::x");
    expect(String(api.mock.calls[1][0])).toContain("/api/instances");
    expect(String(api.mock.calls[1][0])).toContain("mac");
  });

  it("the block wires it: the fold's picker, the Repository row reads 'forked from', Clear restores the plain flow", () => {
    const src = code("components/flights/NewSessionBlock.tsx");
    expect(src).toContain("{fromOpen && <ParentPicker choices={parents} value={parent?.title || \"\"} onPick={forkFrom} />}");
    expect(src).toContain("forked from <b>{parent.name}</b>");
    expect(src).toContain("onClick={() => setParent(null)}");
    expect(src).toContain("workerTitle(parent.title, (rows || []).map((r) => r.title))");
    expect(src).toContain("title = await postInstance(body, device);");
    expect(src).not.toMatch(/<select|\balert\(|\bconfirm\(|window\.prompt/);
    expect(code("components/flights/newBlockOptions.tsx")).not.toMatch(/<select/);
  });
});
