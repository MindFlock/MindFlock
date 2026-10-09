/** Settings → Devices' "Tailscale on this device" card
 * (components/settings/screens/TailscaleCard.tsx), rendered from health
 * payloads shaped like GET /api/tailscale/health.
 *
 * What is pinned: a running device shows who it's signed in as (a tagged one
 * as the tag, never a login), the tailnet and the device; a signed-out one
 * gets the Sign in button, and once POST /api/tailscale/login answered, the
 * sign-in URL as a link and a QR; a stopped one is "Turn on"; issues render
 * worst-first with their fix and admin-console link (key expiry); WSL with
 * Tailscale only on Windows gets both ways out, never "Install Tailscale".
 * The vitest environment is node, so views go through react-dom/server. */

import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it, vi } from "vitest";

vi.mock("../api/client", () => ({ api: vi.fn() }));
vi.mock("../lib/toast", () => ({ toast: vi.fn() }));
vi.mock("../lib/clipboard", () => ({ copyText: vi.fn(async () => true) }));

const { TailscaleCardView, canSignIn, signedInAs, stateLabel } = await import(
  "../components/settings/screens/TailscaleCard"
);
type Health = import("../components/settings/screens/TailscaleCard").TailscaleHealth;
type Login = import("../components/settings/screens/TailscaleCard").LoginResult;

function health(extra: Partial<Health> = {}): Health {
  return {
    installed: true,
    path: "/usr/bin/tailscale",
    kind: "native",
    os: "linux",
    backend_state: "Running",
    running: true,
    tailnet: "me@example.com",
    user: "me@example.com",
    tagged: false,
    tags: [],
    device: { name: "Box", dns: "box.tail0000.ts.net", ips: ["100.64.0.10", "fd7a::a"], os: "linux", id: "n1" },
    magicdns: true,
    https: true,
    key_expiry: { at: "2027-04-04T18:05:17Z", days: 177, expired: false, warn: false },
    issues: [],
    ...extra,
  };
}

const render = (h: Health | null, login?: Login | null) =>
  renderToStaticMarkup(createElement(TailscaleCardView, { health: h, login, onSignIn: () => {} }));

describe("TailscaleCardView", () => {
  it("shows a loading line before the first answer", () => {
    expect(render(null)).toContain("Checking Tailscale");
  });

  it("shows signed-in-as, tailnet and this device when running", () => {
    const html = render(health());
    expect(html).toContain("Tailscale on this device");
    expect(html).toContain("Connected");
    expect(html).toContain("Signed in as");
    expect(html).toContain("box.tail0000.ts.net");
    expect(html).toContain("100.64.0.10");
    expect(html).not.toContain("Sign in to Tailscale");
  });

  it("names a tagged device by its tag, not a login", () => {
    const h = health({ tagged: true, tags: ["tag:mindflock"], user: "" });
    expect(signedInAs(h)).toBe("a tagged device (tag:mindflock)");
    expect(render(h)).toContain("a tagged device (tag:mindflock)");
  });

  it("offers Sign in when signed out, then the URL as a link and a QR", () => {
    const h = health({ backend_state: "NeedsLogin", running: false, issues: [] });
    expect(canSignIn(h)).toBe(true);
    expect(stateLabel(h)).toBe("Not signed in");
    const before = render(h);
    expect(before).toContain("Sign in to Tailscale");
    expect(before).not.toContain("login.tailscale.com/a/");
    const after = render(h, {
      ok: true,
      auth_url: "https://login.tailscale.com/a/xyz",
      auth_qr_svg: "<svg id='auth'></svg>",
    });
    expect(after).toContain('href="https://login.tailscale.com/a/xyz"');
    expect(after).toContain("<svg id='auth'></svg>");
  });

  it("says Turn on for a stopped device", () => {
    const html = render(health({ backend_state: "Stopped", running: false }));
    expect(html).toContain("Turn on Tailscale");
    expect(html).toContain("Turned off");
  });

  it("shows a refused sign-in with the one command that fixes it", () => {
    const html = render(health({ backend_state: "NeedsLogin", running: false }), {
      ok: false,
      error: "Tailscale needs your permission",
      fix: "sudo tailscale up --operator=$USER",
    });
    expect(html).toContain("Tailscale needs your permission");
    expect(html).toContain("sudo tailscale up --operator=$USER");
  });

  it("warns about key expiry with the admin console link", () => {
    const html = render(
      health({
        key_expiry: { at: "2026-10-23T15:25:55Z", days: 14, expired: false, warn: true },
        issues: [
          {
            id: "key_expiry",
            level: "warn",
            message: "This device's Tailscale key expires in 14 days … → ⋯ → Disable key expiry.",
            docs: "https://login.tailscale.com/admin/machines",
          },
        ],
      })
    );
    expect(html).toContain('data-issue="key_expiry"');
    expect(html).toContain("ts-warn");
    expect(html).toContain("expires in 14 days");
    expect(html).toContain('href="https://login.tailscale.com/admin/machines"');
    expect(html).toContain("Open the admin console");
  });

  it("gives WSL with only Windows Tailscale both ways out, not 'install'", () => {
    const h: Health = {
      installed: false,
      kind: "wsl-windows-host",
      os: "wsl",
      path: "/mnt/c/Program Files/Tailscale/tailscale.exe",
      issues: [
        {
          id: "wsl_windows_only",
          level: "warn",
          message: "Tailscale is on Windows, but MindFlock runs inside WSL …",
          fix: "curl -fsSL https://tailscale.com/install.sh | sh && sudo tailscale up --hostname=box-wsl --operator=$USER",
          docs: "https://tailscale.com/kb/1295/install-windows-wsl2",
        },
      ],
    };
    expect(stateLabel(h)).toBe("On Windows only");
    expect(canSignIn(h)).toBe(false);
    const html = render(h);
    expect(html).toContain("On Windows only");
    expect(html).not.toContain("Not installed");
    expect(html).toContain("--hostname=box-wsl");
    expect(html).toContain("data-wsl-options");
    expect(html).toContain("Recommended:");
    expect(html).toContain("mirrored");
    expect(html).toContain("kb/1295");
  });

  it("notes the macOS app's built-in CLI", () => {
    const html = render(health({ kind: "app-bundle", path: "/Applications/Tailscale.app/Contents/MacOS/Tailscale" }));
    expect(html).toContain("Tailscale app");
  });
});
