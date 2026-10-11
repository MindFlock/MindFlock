import { describe, it, expect } from "vitest";
import type { ProviderStatus } from "../api/types";
import { pickerOptions, providerRow, providerRows, signInState } from "../lib/providerRows";

const base: ProviderStatus = {
  name: "codex",
  installed: true,
  path: "/usr/bin/codex",
  authenticated: false,
  auth_known: false,
  auth_probe: true,
  has_login: true,
  login_command: "codex login",
  install_hint: "curl … | sh",
};

describe("providerRow", () => {
  it("installed + evidence = signed in, no Sign in button", () => {
    const r = providerRow({ ...base, authenticated: true, auth_detail: "~/.codex/auth.json" });
    expect(r.installLabel).toBe("installed");
    expect(r.installTitle).toBe("/usr/bin/codex");
    expect(r.signIn).toBe("signed-in");
    expect(r.signInLabel).toBe("signed in");
    expect(r.signInDetail).toBe("~/.codex/auth.json");
    expect(r.canSignIn).toBe(false);
    expect(r.installHint).toBe(""); // only when missing
  });

  it("installed, probeable, no evidence = not signed in with a Sign in button", () => {
    const r = providerRow(base);
    expect(r.signIn).toBe("not-signed-in");
    expect(r.signInLabel).toBe("not signed in");
    expect(r.canSignIn).toBe(true);
  });

  it("a provider with no login step says no sign-in needed", () => {
    const r = providerRow({ ...base, name: "aider", has_login: false, auth_probe: true });
    expect(r.signIn).toBe("none");
    expect(r.signInLabel).toBe("no sign-in needed");
    expect(r.canSignIn).toBe(false);
  });

  it("a login flow with nothing to probe is unknown, not logged out", () => {
    const r = providerRow({ ...base, name: "goose", auth_probe: false });
    expect(r.signIn).toBe("unknown");
    expect(r.canSignIn).toBe(true); // it still has a login flow to run
    expect(r.signInDetail).toMatch(/check inside the CLI/);
  });

  it("not installed: install command + no sign-in chip", () => {
    const r = providerRow({ ...base, installed: false, path: "" });
    expect(r.installLabel).toBe("not installed");
    expect(r.installTitle).toBe("not found on PATH");
    expect(r.installHint).toBe("curl … | sh");
    expect(r.signIn).toBe("not-installed");
    expect(r.signInLabel).toBe("");
    expect(r.canSignIn).toBe(false);
  });

  it("a saved binary path that doesn't resolve is blamed, not PATH", () => {
    const r = providerRow({ ...base, installed: false, binary_override: "/opt/codex" });
    expect(r.installTitle).toBe("/opt/codex is not an executable file");
    expect(r.binaryPath).toBe("/opt/codex");
  });

  it("carries the env pin so the field can explain a no-op edit", () => {
    const r = providerRow({ ...base, binary_override_env: "MINDFLOCK_PROVIDER_BIN_CODEX" });
    expect(r.binaryPinnedBy).toBe("MINDFLOCK_PROVIDER_BIN_CODEX");
  });

  it("signInState never reports signed in for an absent CLI", () => {
    expect(signInState({ ...base, installed: false, authenticated: true })).toBe("not-installed");
  });
});

describe("providerRows", () => {
  it("appends one red row per TOML that failed to load", () => {
    const rows = providerRows([base], [{ file: "bad.toml", error: "TOMLDecodeError: …" }]);
    expect(rows.map((r) => r.kind)).toEqual(["provider", "error"]);
    expect(rows[1]).toEqual({ kind: "error", file: "bad.toml", error: "TOMLDecodeError: …" });
  });

  it("tolerates a server without the errors field", () => {
    expect(providerRows([base]).length).toBe(1);
    expect(providerRows([base], null).length).toBe(1);
  });
});

describe("pickerOptions", () => {
  it("lists every provider and disables the absent ones with the reason", () => {
    const opts = pickerOptions([base, { ...base, name: "aider", installed: false }]);
    expect(opts).toEqual([
      { name: "codex", disabled: false, label: "codex" },
      { name: "aider", disabled: true, label: "aider — not installed" },
    ]);
  });
});
