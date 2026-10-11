/** Settings → Connections dashboard: grouping by kind, the three-state pill,
 * the header summary, and how a row's Test result renders. */
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it, vi } from "vitest";
import type { ConnectionRow } from "../api/types";
import {
  CONNECTIONS_CHANGED_EVENT,
  ConnectionRowView,
  actionHandler,
  describeTestResult,
  groupRows,
  pillText,
  rowStatus,
  summaryLine,
} from "../components/settings/screens/Connections";
import connsSrc from "../components/settings/screens/Connections.tsx?raw";

const row = (over: Partial<ConnectionRow>): ConnectionRow => ({
  id: "x",
  name: "X",
  kind: "agent",
  status: "connected",
  detail: "",
  action: null,
  test: null,
  ...over,
});

const deps = { gotoScreen: vi.fn(), openDialogFor: vi.fn(), closeDialog: vi.fn() };

const render = (c: ConnectionRow, extra: Record<string, unknown> = {}) =>
  renderToStaticMarkup(createElement(ConnectionRowView, { c, deps, onChanged: () => {}, ...extra }));

describe("grouping", () => {
  it("groups rows by kind in the server's order, with headings", () => {
    const groups = groupRows([
      row({ id: "agent", kind: "agent" }),
      row({ id: "agent:codex", kind: "agent" }),
      row({ id: "github", kind: "github" }),
      row({ id: "devices", kind: "devices" }),
      row({ id: "phone", kind: "devices" }),
    ]);
    expect(groups.map((g) => g.kind)).toEqual(["agent", "github", "devices"]);
    expect(groups.map((g) => g.label)).toEqual(["Agent CLIs", "GitHub", "Devices and people"]);
    expect(groups[2].rows.map((r) => r.id)).toEqual(["devices", "phone"]);
  });

  it("an unknown kind still gets a heading", () => {
    expect(groupRows([row({ kind: "weather" })])[0].label).toBe("Weather");
  });
});

describe("pill + summary", () => {
  it("names the three states", () => {
    expect(pillText({ status: "connected" })).toBe("Connected");
    expect(pillText({ status: "needs_attention" })).toBe("Needs attention");
    expect(pillText({ status: "off" })).toBe("Off");
  });

  it("reads an older server's legacy vocabulary", () => {
    expect(rowStatus({ status: "attention" })).toBe("needs_attention");
    expect(rowStatus({ status: "not_connected" })).toBe("off");
  });

  it("renders the pill with the conn-<status> class and the name", () => {
    const html = render(row({ name: "Coding agent · Claude", status: "needs_attention" }));
    expect(html).toContain('class="conn-row"');
    expect(html).toContain('<span class="conn-name">Coding agent · Claude</span>');
    expect(html).toContain('class="conn-pill conn-needs_attention">Needs attention<');
  });

  it("counts N connected · M need attention · K off", () => {
    const rows = [
      row({ status: "connected" }),
      row({ status: "connected" }),
      row({ status: "needs_attention" }),
      row({ status: "off" }),
      row({ status: "off" }),
      row({ status: "off" }),
    ];
    expect(summaryLine(rows)).toBe("2 connected · 1 needs attention · 3 off");
    expect(summaryLine([...rows, row({ status: "needs_attention" })])).toBe(
      "2 connected · 2 need attention · 3 off"
    );
    expect(summaryLine([])).toBe("");
  });
});

describe("Test results", () => {
  it("reads each endpoint's answer shape", () => {
    expect(describeTestResult({ ok: true, detail: "@octo via settings" })).toEqual({
      ok: true,
      text: "@octo via settings",
    });
    expect(describeTestResult({ ok: false, error: "403 Forbidden" })).toEqual({ ok: false, text: "403 Forbidden" });
    expect(describeTestResult({ ok: true, server: "PostgreSQL 16" }).text).toBe("PostgreSQL 16");
    expect(
      describeTestResult({ ok: false, cli: { detail: "/usr/bin/codex" }, auth: { detail: "no login found" } }).text
    ).toBe("/usr/bin/codex");
    expect(describeTestResult({ ok: true, cli: { detail: "/bin/x" }, auth: { detail: "signed in" } }).text).toBe(
      "signed in"
    );
    expect(describeTestResult(null)).toEqual({ ok: false, text: "Failed" });
    expect(describeTestResult({ ok: true })).toEqual({ ok: true, text: "Works" });
  });

  it("renders a passed and a failed result inline", () => {
    const c = row({ test: { endpoint: "/api/settings/test/github", method: "POST" } });
    const ok = render(c, { result: { ok: true, text: "@octo via settings" }, onTest: () => {} });
    expect(ok).toContain('class="conn-test-result ok"');
    expect(ok).toContain("✓ @octo via settings");
    expect(ok).toContain(">Test</button>");
    const bad = render(c, { result: { ok: false, text: "Bad credentials" }, onTest: () => {} });
    expect(bad).toContain('class="conn-test-result bad"');
    expect(bad).toContain("✗ Bad credentials");
    expect(render(c, { testing: true, onTest: () => {} })).toContain("Testing…");
  });

  it("shows no Test button for a row without one", () => {
    expect(render(row({}), { onTest: () => {} })).not.toContain(">Test</button>");
  });
});

describe("actions and fixes", () => {
  it("shows the fix command with Copy, once, for a copy action", () => {
    const html = render(
      row({
        status: "off",
        fix_command: "npm i -g goose",
        action: { label: "Copy install command", kind: "copy", target: "npm i -g goose" },
      })
    );
    expect(html).toContain("<code>npm i -g goose</code>");
    expect(html.match(/>Copy</g)?.length).toBe(1);
    expect(html).not.toContain("Copy install command");
  });

  it("hides the fix on a connected row", () => {
    expect(render(row({ status: "connected", fix_command: "gh auth login" }))).not.toContain("gh auth login");
  });

  it("marks the default provider", () => {
    expect(render(row({ default: true }))).toContain('class="conn-badge">default<');
  });

  it("routes each action kind", () => {
    actionHandler({ label: "Manage", kind: "screen", target: "devices" }, deps)!();
    expect(deps.gotoScreen).toHaveBeenCalledWith("devices");
    actionHandler({ label: "Open", kind: "dialog", target: "intake", tab: "tickets" }, deps)!();
    expect(deps.openDialogFor).toHaveBeenCalledWith("intake", "tickets");
    expect(actionHandler({ label: "?", kind: "dialog", target: "nope" }, deps)).toBeNull();
  });

  it("renders the action button with the server's label", () => {
    const html = render(row({ action: { label: "Connect", kind: "dialog", target: "connect-github" } }));
    expect(html).toContain('class="test-btn conn-configure">Connect</button>');
  });
});

describe("refresh", () => {
  it("re-reads every 30 s and on mf:connections-changed", () => {
    expect(CONNECTIONS_CHANGED_EVENT).toBe("mf:connections-changed");
    expect(connsSrc).toContain("window.setInterval(() => void load(false), REFRESH_MS)");
    expect(connsSrc).toContain("export const REFRESH_MS = 30_000;");
    expect(connsSrc).toContain("window.addEventListener(CONNECTIONS_CHANGED_EVENT, onChanged)");
  });
});
