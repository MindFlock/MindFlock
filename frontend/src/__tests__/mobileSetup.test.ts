/** Settings → Your phone (screens/Mobile.tsx: the phone steps and the URL
 * grid) and the shared phone link's setup checklist (screens/SharedLink.tsx,
 * rendered on Your devices).
 *
 * What is pinned: every step renders its server-given state as ✓ / ✗ / ? —
 * an unknown approval is never drawn as a tick; a failing step shows its
 * exact fix (the operator command, the named machine with the -1 note, the
 * one prefilled policy block, the admin console links); a passing
 * step folds its fix away; and the phone step carries the QR. The vitest
 * environment is node, so views are checked through react-dom/server. */

import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it, vi } from "vitest";

vi.mock("../api/client", () => ({ api: vi.fn() }));
vi.mock("../lib/toast", () => ({ toast: vi.fn() }));
vi.mock("../lib/clipboard", () => ({ copyText: vi.fn(async () => true) }));
vi.mock("../state/queries", () => ({ useConfig: () => ({ data: undefined }) }));
vi.mock("../components/settings/useServerRestart", () => ({
  useServerRestart: () => ({ restarting: false, restart: vi.fn() }),
}));

const { PhoneAppRow, UrlList, computerRow, reachableRow } = await import("../components/settings/screens/Mobile");
const { SetupChecklist } = await import("../components/settings/screens/SharedLink");
type Shared = import("../components/settings/screens/SharedLink").SharedLinkState;
type Step = import("../components/settings/screens/SharedLink").SharedLinkStep;

const POLICY = `"tagOwners": {\n  "tag:mindflock": ["autogroup:admin"],\n},\n"autoApprovers": {\n  "services": {\n    "svc:mindflock": ["tag:mindflock"],\n  },\n},\n"grants": [\n  {"src": ["tag:mindflock"], "dst": ["tag:mindflock"], "ip": ["tcp:8765", "tcp:443"]},\n  {"src": ["me@example.com"], "dst": ["svc:mindflock"], "ip": ["tcp:443"]},\n],\n"tests": [\n  {"src": "tag:mindflock", "accept": ["tag:mindflock:8765"]},\n],`;

function shared(steps: Partial<Record<string, Step["state"]>>, extra: Partial<Shared> = {}): Shared {
  const ids = ["operator", "tag", "define", "policy", "approval", "phone"];
  return {
    enabled: true,
    name: "mindflock",
    service: "svc:mindflock",
    url: "https://mindflock.tail0000.ts.net/m",
    tag: "tag:mindflock",
    machine: { hostname: "Box", dns: "box-1.tail0000.ts.net", ip: "100.64.0.10", duplicate_of: "box" },
    operator_fix: "sudo tailscale set --operator=$USER",
    policy: POLICY,
    admin: {
      machines: "https://login.tailscale.com/admin/machines",
      services: "https://login.tailscale.com/admin/services",
      policy: "https://login.tailscale.com/admin/acls/file",
    },
    steps: ids.map((id) => ({
      id,
      title: "Step " + id,
      state: steps[id] || "unknown",
      reason: "because " + id,
    })),
    ...extra,
  };
}

const render = (s: Shared, qrSvg?: string) =>
  renderToStaticMarkup(createElement(SetupChecklist, { shared: s, qrSvg, onRecheck: () => {} }));

/** The markup of one step's <li>. */
function step(html: string, id: string): string {
  const at = html.indexOf(`data-step="${id}"`);
  expect(at).toBeGreaterThan(-1);
  const start = html.lastIndexOf("<li", at);
  const end = html.indexOf("</li>", start);
  return html.slice(start, end);
}

describe("SetupChecklist", () => {
  it("renders the six steps in order with their states", () => {
    const html = render(
      shared({ operator: "ok", tag: "ok", define: "ok", policy: "unknown", approval: "fail" })
    );
    const order = ["operator", "tag", "define", "policy", "approval", "phone"].map((id) =>
      html.indexOf(`data-step="${id}"`)
    );
    expect(order.every((at, i) => at > -1 && (i === 0 || at > order[i - 1]))).toBe(true);
    expect(step(html, "operator")).toContain("sl-ok");
    expect(step(html, "operator")).toContain("✓");
    expect(step(html, "approval")).toContain("sl-fail");
    expect(step(html, "approval")).toContain("✗");
    expect(step(html, "approval")).toContain("because approval");
    expect(html).toContain("Re-check");
  });

  it("never draws an unknown approval as a tick", () => {
    const html = render(shared({ approval: "unknown" }));
    const s = step(html, "approval");
    expect(s).toContain("sl-unknown");
    expect(s).not.toContain("✓");
    expect(s).toContain('aria-label="unknown"');
  });

  it("shows the operator command with a copy button when serve config is refused", () => {
    const s = step(render(shared({ operator: "fail" })), "operator");
    expect(s).toContain("sudo tailscale set --operator=$USER");
    expect(s).toContain("Copy");
    expect(s).not.toContain("<details");
  });

  it("names the exact machine to tag, with the de-duplicated name explained", () => {
    const s = step(render(shared({ tag: "fail" })), "tag");
    expect(s).toContain("box-1.tail0000.ts.net");
    expect(s).toContain("100.64.0.10");
    expect(s).toContain("tag:mindflock");
    expect(s).toContain("already <code>box</code>");
    expect(s).toContain('href="https://login.tailscale.com/admin/machines"');
  });

  it("links the Services page to define svc:<name> on tcp:443", () => {
    const s = step(render(shared({ define: "unknown" })), "define");
    expect(s).toContain("svc:mindflock");
    expect(s).toContain("tcp:443");
    expect(s).toContain('href="https://login.tailscale.com/admin/services"');
  });

  it("prefills ONE merge-safe policy block with a single copy button", () => {
    const s = step(render(shared({ policy: "unknown" })), "policy");
    expect(s).toContain("autoApprovers");
    expect(s).toContain("&quot;svc:mindflock&quot;: [&quot;tag:mindflock&quot;]");
    expect(s).toContain("&quot;dst&quot;: [&quot;svc:mindflock&quot;]");
    // Device to device on the server port, and a tests stanza.
    expect(s).toContain("&quot;tcp:8765&quot;");
    expect(s).toContain("&quot;tests&quot;");
    expect(s.match(/>Copy</g)?.length).toBe(1);
    expect(s).toContain("can&#x27;t appear twice");
  });

  it("tells the tag step to disable key expiry too", () => {
    const s = step(render(shared({ tag: "fail" })), "tag");
    expect(s).toContain("Disable key expiry");
    expect(s).toContain("keeps its key expiry");
  });

  it("folds a passing step's fix away", () => {
    const s = step(render(shared({ tag: "ok" })), "tag");
    expect(s).toContain("<details");
    expect(s).toContain("Show how");
  });

  it("puts the QR and the shared URL on the phone step", () => {
    const s = step(render(shared({}), "<svg id='q'></svg>"), "phone");
    expect(s).toContain("<svg id='q'></svg>");
    expect(s).toContain("https://mindflock.tail0000.ts.net/m");
    expect(s).not.toContain("<details");
  });

  it("tells a machine without Tailscale where to get it", () => {
    const s = step(render(shared({ operator: "fail" }, { error_kind: "missing" })), "operator");
    expect(s).toContain("tailscale.com/download");
    expect(s).not.toContain("--operator");
  });
});

describe("UrlList", () => {
  it("puts every label/URL pair in one grid", () => {
    const html = renderToStaticMarkup(
      createElement(UrlList, {
        urls: [
          { label: "Local", url: "http://127.0.0.1:8765/m" },
          { label: "Shared link", url: "https://mindflock.tail0000.ts.net/m" },
          { label: "This device", url: "http://box.tail0000.ts.net:8765/m" },
        ],
      })
    );
    expect(html.match(/<dl class="mobile-urls"/g)?.length).toBe(1);
    expect(html.match(/<dt class="set-label">/g)?.length).toBe(3);
  });

  it("renders nothing for no URLs", () => {
    expect(renderToStaticMarkup(createElement(UrlList, { urls: [] }))).toBe("");
  });
});

describe("PhoneAppRow", () => {
  it("is row 2: Tailscale on the phone, signed in as this account, with the app's QR named as such", () => {
    const html = renderToStaticMarkup(
      createElement(PhoneAppRow, {
        app: { url: "https://tailscale.com/download", qr_svg: "<svg id='app'></svg>", login: "me@example.com" },
      })
    );
    expect(html).toContain('data-phone-step="tailscale"');
    expect(html).toContain("<strong>me@example.com</strong>");
    expect(html).toContain("<svg id='app'></svg>");
    expect(html).toContain('href="https://tailscale.com/download"');
    expect(html).toContain("This QR gets the app");
  });

  it("says 'the same account' when the login isn't known", () => {
    const html = renderToStaticMarkup(createElement(PhoneAppRow, { app: undefined }));
    expect(html).toContain("same account as this computer");
    expect(html).toContain("tailscale.com/download");
  });
});

describe("Your phone rows: live ✓/✗", () => {
  const base = { installed: true, issues: [] } as never;
  it("row 1 is ✓ only when Tailscale is running", () => {
    expect(computerRow({ ...(base as object), backend_state: "Running", user: "me@x.com" } as never, { caps: true })).toMatchObject({
      state: "ok",
      line: "Tailscale is running on this computer — signed in as me@x.com.",
    });
  });

  it("installed but not running: says so, and the ONE fix is a command for a Copy box (no backticks in the line)", () => {
    const r = computerRow(
      {
        installed: true,
        backend_state: "",
        issues: [
          {
            id: "daemon",
            level: "fail",
            message: "Tailscale is installed but isn't answering — start it with `sudo systemctl enable --now tailscaled`.",
            fix: "sudo systemctl enable --now tailscaled",
          },
        ],
      } as never,
      { caps: true }
    );
    expect(r.state).toBe("fail");
    expect(r.line).toBe("Tailscale is installed but not running. Start it:");
    expect(r.line).not.toContain("`");
    expect(r.fix).toBe("sudo systemctl enable --now tailscaled");
  });

  it("not installed / signed out: ✗ with the full card (install guidance, Sign in)", () => {
    expect(computerRow({ installed: false, issues: [] } as never, { caps: true })).toMatchObject({ state: "fail", card: true });
    expect(computerRow({ installed: true, backend_state: "NeedsLogin", issues: [] } as never, { caps: true })).toMatchObject({
      state: "fail",
      line: "Tailscale isn't signed in on this computer.",
      card: true,
    });
    expect(computerRow(null, { caps: false })).toMatchObject({ state: "fail" });
    expect(computerRow(null, { caps: true })).toMatchObject({ state: "wait" });
  });

  it("row 3 is ✗ while MindFlock only listens on this computer", () => {
    expect(reachableRow({ local_only: true }, { caps: true })).toBe("fail");
    expect(reachableRow({ local_only: false }, { caps: true })).toBe("ok");
    expect(reachableRow(null, { caps: true })).toBe("wait");
  });
});
