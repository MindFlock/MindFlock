/** lib/gateOn.ts — Settings → Devices' "Turn the gate on": sign this
 * browser in before the gate goes on, never after; a withheld token skips
 * the sign-in and lets the save's refusal surface. */

import { describe, it, expect } from "vitest";
import { turnGateOn } from "../lib/gateOn";

function fakeApi(token: string | null, refuse = "") {
  const calls: Array<[string, unknown]> = [];
  const call = async <T,>(path: string, opts?: { json?: unknown }): Promise<T> => {
    calls.push([path, opts?.json]);
    if (path === "/api/settings/auth-token") return { token } as T;
    if (path === "/api/settings" && refuse) throw new Error(refuse);
    return { ok: true } as T;
  };
  return { call, calls };
}

describe("turnGateOn", () => {
  it("signs this browser in, then turns the gate on", async () => {
    const { call, calls } = fakeApi("tok-123");
    await turnGateOn(call);
    expect(calls).toEqual([
      ["/api/settings/auth-token", undefined],
      ["/api/auth", { token: "tok-123" }],
      ["/api/settings", { general: { auth_mode: "on" } }],
    ]);
  });

  it("skips the sign-in when the token is withheld", async () => {
    const { call, calls } = fakeApi(null);
    await turnGateOn(call);
    expect(calls.map((c) => c[0])).toEqual(["/api/settings/auth-token", "/api/settings"]);
  });

  it("surfaces the save's refusal", async () => {
    const { call } = fakeApi(null, "needs this device's sign-in");
    await expect(turnGateOn(call)).rejects.toThrow("needs this device's sign-in");
  });
});
