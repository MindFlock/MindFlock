/** Round 15 triggers (docs/flights.md "Triggers"): `TriggerKind` grows
 * main_push / main_red / hook with their chip words; the Takes off section
 * shows each with its fields — the repository watched (a PathField), quiet
 * minutes, Paused; the hook's address read-only with Copy once the plan is
 * saved (a hint before); the trigger kinds that start their own session.
 * Server-rendered with the settings seeded in a QueryClient; the handlers
 * are pinned in the source. */

import { createElement, type ReactElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it, vi } from "vitest";
import { code, css } from "./sourceText";
import type { FlightPlan, FlightTrigger } from "../lib/flights";

vi.mock("../api/client", () => ({ api: vi.fn(() => new Promise(() => {})), ApiError: class extends Error {} }));
vi.mock("../lib/toast", () => ({ toast: vi.fn() }));

const { QueryClient, QueryClientProvider } = await import("@tanstack/react-query");
const { TakeoffFields, HookUrlRow } = await import("../components/flights/TakeoffDialog");
const { TRIGGER_LABEL, MAIN_TRIGGERS, hookUrl, triggerMakesSession } = await import("../lib/flights");

(globalThis as Record<string, unknown>).document ??= {
  hidden: false,
  addEventListener: () => {},
  removeEventListener: () => {},
  getElementById: () => null,
  querySelector: () => null,
};

const TOKEN = "ab".repeat(16);
const plan = (trigger: FlightTrigger): FlightPlan => ({
  id: "p_t",
  name: "Guard",
  description: "",
  builtin: false,
  trigger,
  session: { repo_path: "", program: "", model: "", in_place: false },
  steps: [{ id: "s1", kind: "agent", title: "Agent", config: {}, on_fail: "halt" }],
  updated: 1,
});

function render(el: ReactElement) {
  const c = new QueryClient({ defaultOptions: { queries: { retry: false, enabled: false } } });
  c.setQueryData(["settings"], {});
  return renderToStaticMarkup(createElement(QueryClientProvider, { client: c }, el));
}

const fields = (trigger: FlightTrigger) => render(createElement(TakeoffFields, { plan: plan(trigger), onChange() {} }));

describe("the trigger kinds", () => {
  it("three more, with their chip words", () => {
    expect(TRIGGER_LABEL.main_push).toBe("When main moves");
    expect(TRIGGER_LABEL.main_red).toBe("When main is red");
    expect(TRIGGER_LABEL.hook).toBe("When a hook fires");
    expect(Object.keys(TRIGGER_LABEL)).toEqual(["manual", "ticket", "pr", "issue", "schedule", "main_push", "main_red", "hook"]);
    expect(MAIN_TRIGGERS).toEqual(["main_push", "main_red"]);
  });

  it("the ones that start their own session", () => {
    expect(["manual", "schedule", "main_push", "main_red", "hook"].every((k) => triggerMakesSession(k as never))).toBe(true);
    expect(["ticket", "pr", "issue"].some((k) => triggerMakesSession(k as never))).toBe(false);
  });

  it("the hook address: origin + /api/hooks/flyway/<token>, empty until saved", () => {
    expect(hookUrl({ token: TOKEN }, "https://mf.example")).toBe("https://mf.example/api/hooks/flyway/" + TOKEN);
    expect(hookUrl({ token: "" }, "https://mf.example")).toBe("");
    expect(hookUrl({}, "https://mf.example")).toBe("");
    // No window on the server: a bare path.
    expect(hookUrl({ token: TOKEN })).toBe("/api/hooks/flyway/" + TOKEN);
    const src = code("lib/flights.ts");
    expect(src).toContain('window.location.origin : "")');
  });
});

describe("Takes off: the three triggers' fields", () => {
  it("every kind is a chip of the segmented control", () => {
    const html = fields({ kind: "manual", schedule: null, paused: false });
    for (const label of Object.values(TRIGGER_LABEL)) expect(html).toContain(label);
  });

  it("When main moves: the repository watched is a PathField, quiet minutes, Paused — and the session it starts", () => {
    const html = fields({ kind: "main_push", schedule: null, paused: false, repo: "/home/me/repo", quiet_minutes: 60 });
    expect(html).toContain('id="fl-trigger-repo"');
    expect(html).toContain('value="/home/me/repo"');
    expect(html).toContain("Watches");
    expect(html).toContain("a new tip starts it");
    expect(html).toContain('aria-label="Quiet minutes (min)"');
    expect(html).toContain("Paused");
    // Its own session: the Repository / Agent / Model fields follow.
    expect(html).toContain('id="fl-plan-repo"');
    expect(html).toContain("Agent");
    // No hook address, no schedule.
    expect(html).not.toContain("fl-hook-url");
    expect(html).not.toContain("Which days");
  });

  it("When main is red says what it watches", () => {
    const html = fields({ kind: "main_red", schedule: null, paused: true, repo: "/r", quiet_minutes: 15 });
    expect(html).toContain("red starts it");
    expect(html).toContain('value="15"');
    expect(html).toContain('aria-pressed="true"');
  });

  it("When a hook fires: a hint before the plan is saved, the address with Copy after", () => {
    const before = fields({ kind: "hook", schedule: null, paused: false, token: "" });
    expect(before).toContain("Save to get the address");
    expect(before).not.toContain("fl-hook-copy");
    expect(before).toContain('aria-label="Quiet minutes (min)"');
    expect(before).not.toContain('id="fl-trigger-repo"');
    const after = fields({ kind: "hook", schedule: null, paused: false, token: TOKEN, quiet_minutes: 60 });
    expect(after).toContain('aria-label="Hook address"');
    expect(after).toContain('value="/api/hooks/flyway/' + TOKEN + '"');
    expect(after).toContain("readOnly");
    expect(after).toContain(">Copy</button>");
    expect(after).toContain("POST anything here");
  });

  it("the hook row alone", () => {
    const html = render(createElement(HookUrlRow, { plan: plan({ kind: "hook", schedule: null, paused: false, token: TOKEN }) }));
    expect(html).toContain('class="fl-hook-url"');
    expect(html).toContain('class="test-btn fl-hook-copy"');
  });

  it("the handlers: repo, quiet and paused patch the trigger; Copy writes the clipboard and says so", () => {
    const src = code("components/flights/TakeoffDialog.tsx");
    expect(src).toContain("onChange({ trigger: { ...plan.trigger, repo: v } })");
    expect(src).toContain('onChange({ trigger: { ...plan.trigger, quiet_minutes: v === "" ? 0 : v } })');
    expect(src).toContain("onChange({ trigger: { ...plan.trigger, paused } })");
    expect(src).toContain("navigator.clipboard.writeText(url)");
    expect(src).toContain('toast("Copied the hook address")');
    // Electron has no prompt/alert: the address is an input one can select.
    expect(src).toContain("onFocus={(e) => e.currentTarget.select()}");
    expect(src).toContain("const needsSession = triggerMakesSession(plan.trigger.kind);");
  });

  it("styles: the address takes the row, Copy beside it", () => {
    const sheet = css("components/flights/TakeoffDialog.css");
    expect(sheet).toMatch(/\.fl-hook-url\s*\{[^}]*display:\s*flex/);
    expect(sheet).toMatch(/\.fl-hook-url \.fl-input\s*\{[^}]*flex:\s*1 1 auto/);
    expect(sheet).toMatch(/\.fl-hook-copy\s*\{[^}]*white-space:\s*nowrap/);
  });
});
