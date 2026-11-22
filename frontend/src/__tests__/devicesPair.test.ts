/** Add a NEW device without Tailscale (docs/devices-anywhere.md §7): the
 * paste box routes the long `mfd1:` code, the join line says what is
 * happening ("Finishing setup…") and what is left, and the inviter's row
 * for a device that joined through the tunnel says why it isn't reachable
 * from there yet. The two controls live on Your devices only. */
import { describe, expect, it } from "vitest";
import type { FleetAdmit, FleetJoin } from "../api/types";
import { extractDeviceCode, farInviteLine } from "../lib/devicesAnywhere";
import { admitLine, joinedToast, joinLine, routeCode } from "../lib/fleet";
import { paletteCode } from "../components/palette/paletteRank";

const CODE = "mfd1:aeaqgnlpmvwgk3dnfzuxi2lmn5ygszjnmrsxm2ldmvzwcylsfuytemzu-abcd";

const SCREEN_SRC: Record<string, string> = Object.fromEntries(
  Object.entries(
    import.meta.glob<string>("../components/settings/screens/*.tsx", { query: "?raw", import: "default", eager: true })
  ).map(([path, text]) => [path.replace(/^.*\//, ""), text])
);

function join(over: Partial<FleetJoin>): FleetJoin {
  return { state: "idle", device: "anya", host: "Anya", code: "", error: "", id: "", ...over };
}

describe("the long code is routed to Your devices", () => {
  it("is found in pasted text, case forgiven", () => {
    expect(extractDeviceCode("here: " + CODE.toUpperCase() + " thanks")).toBe(CODE);
    expect(extractDeviceCode("ABCD-EFGH")).toBe("");
  });

  it("routeCode says anywhere — never a peer invite or an 8-character code", () => {
    expect(routeCode(CODE)).toEqual({ kind: "anywhere", code: CODE, device: "" });
    expect(routeCode("mfp2:abc-defg").kind).not.toBe("anywhere");
    expect(routeCode("ABCD-EFGH").kind).toBe("device");
  });

  it("the palette offers it too", () => {
    expect(paletteCode(CODE).kind).toBe("anywhere");
  });
});

describe("the joiner's line", () => {
  it("says it is finishing setup, then what is left", () => {
    expect(joinLine(join({ state: "finishing", via: "anywhere" }))).toMatch(/^Joined Anya\. Finishing setup/);
    const note = "Anya can't reach this computer yet: GitHub isn't signed in here …";
    expect(joinLine(join({ state: "joined", note }))).toBe("Joined Anya. " + note);
    expect(joinLine(join({ state: "joined" }))).toBe("Joined Anya.");
  });

  it("the joined toast carries the note and never offers Tailscale's Make reachable for it", () => {
    const t = joinedToast(join({ state: "joined", note: "Connect GitHub.", self_reachable: true, via: "anywhere" }));
    expect(t).toEqual({ text: "Joined Anya. Connect GitHub.", offerReach: false });
  });
});

describe("the inviter's row for a device that joined through the tunnel", () => {
  it("explains the wait plainly (GitHub sign-in over there), not a Tailscale fix", () => {
    const a: FleetAdmit = { device: "bart", host: "Bart", at: 1, state: "finishing", reason: "", via: "anywhere" };
    const line = admitLine(a);
    expect(line).toMatch(/^Bart joined\./);
    expect(line).toContain("GitHub sign-in on Bart");
    expect(line).not.toContain("Make reachable");
  });
});

describe("the code's countdown", () => {
  it("counts down, then says it expired", () => {
    const inv = { code: CODE, expires_at: 1000, host: "Anya" };
    expect(farInviteLine(inv, 1000 - 581)).toBe("Works once · expires in 9:41");
    expect(farInviteLine(inv, 1001)).toMatch(/expired/);
  });
});

describe("the two controls are on Your devices", () => {
  it("the far-away invite is its own module, rendered by Devices", () => {
    expect(SCREEN_SRC["DevicesFarInvite.tsx"]).toMatch(/data-control="devices-anywhere-invite"/);
    expect(SCREEN_SRC["Devices.tsx"]).toMatch(/<DevicesFarInvite /);
  });

  it("the joiner's checkbox is ticked by default and says what it turns on", () => {
    const src = SCREEN_SRC["Devices.tsx"];
    expect(src).toMatch(/const \[farReach, setFarReach\] = useState\(true\)/);
    expect(src).toMatch(/data-control="devices-anywhere-join"[\s\S]{0,300}id="devices-far-reach"/);
    expect(src).toContain("/api/devices/anywhere/join");
  });

  it("no native prompt/alert/confirm (the desktop app has none)", () => {
    for (const f of ["DevicesFarInvite.tsx", "Devices.tsx"]) {
      expect(SCREEN_SRC[f]).not.toMatch(/window\.(prompt|alert|confirm)\(/);
    }
  });
});
