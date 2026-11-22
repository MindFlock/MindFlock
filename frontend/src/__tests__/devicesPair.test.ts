/** Add a NEW device without Tailscale (docs/devices-anywhere.md §7): the
 * paste box routes the long `mfd1:` code, the join line says what is
 * happening ("Finishing setup…") and what is left, and the inviter's row
 * for a device that joined through the tunnel says why it isn't reachable
 * from there yet. The two controls live on Your devices only. */
import { describe, expect, it } from "vitest";
import type { FleetAdmit, FleetJoin } from "../api/types";
import {
  extractDeviceCode,
  farConfirmBody,
  farConfirmLabel,
  farInviteLine,
  farSafetyLine,
  type FarJoinConfirm,
} from "../lib/devicesAnywhere";
import { admitLine, deviceEventNote, joinedToast, joinLine, routeCode } from "../lib/fleet";
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

describe("a hostile code: the joiner confirms first (review M2)", () => {
  const ask: FarJoinConfirm = {
    needs_confirm: true,
    text: "The computer that made this code will be able to run agents on this computer, and its settings will replace this computer's. Only continue if the code came from your own computer.",
    safety: "482 093 117",
    safety_text: "Check that your other computer shows the same number next to the code: 482 093 117",
    other_group: null,
    leave_text: "",
  };

  it("says what the code's maker gets, the number, and what the box turns on", () => {
    const body = farConfirmBody(ask, true);
    expect(body).toContain("run agents on this computer");
    expect(body).toContain("482 093 117");
    expect(body).toContain("remote control");
    expect(farConfirmBody(ask, false)).not.toContain("remote control");
    expect(farConfirmLabel(ask)).toBe("It's my computer — join");
  });

  it("names the group this computer would leave", () => {
    const leaving = { ...ask, other_group: { count: 3, hosts: ["Rig", "Mini"] }, leave_text: "This computer is already one of 3 devices (Rig, Mini)." };
    expect(farConfirmBody(leaving, true)).toContain("Rig, Mini");
    expect(farConfirmLabel(leaving)).toBe("Leave that group and join");
  });

  it("the inviter shows the same number by its code", () => {
    expect(farSafetyLine({ code: CODE, expires_at: 1, host: "Anya", safety: "482 093 117" })).toContain("482 093 117");
    expect(farSafetyLine({ code: CODE, expires_at: 1, host: "Anya" })).toBe("");
  });

  it("Join goes through an in-page confirm with the server's answer", () => {
    const src = SCREEN_SRC["Devices.tsx"];
    expect(src).toContain('id="devices-far-confirm"');
    expect(src).toMatch(/needs_confirm/);
    expect(src).toMatch(/confirm: true, leave_group: !!ask\.info\.other_group/);
    expect(SCREEN_SRC["DevicesFarInvite.tsx"]).toContain('id="devices-far-safety"');
  });
});

describe("a code used without the join finishing (review M1)", () => {
  it("is a loud bell row and toast with the server's words", () => {
    const n = deviceEventNote("device.pair_unfinished", {
      detail: "A pairing code was used but the join didn't finish (x). If that wasn't you, change your devices' key now.",
    });
    expect(n?.cls).toBe("n-warn");
    expect(n?.text).toContain("change your devices' key");
    expect(n?.toast).toBe(n?.text);
  });

  it("Your devices shows it with Change key and It was me — on that screen only", () => {
    const src = SCREEN_SRC["DevicesPairAlerts.tsx"];
    expect(src).toContain('id="devices-pair-change-key"');
    expect(src).toContain("It was me");
    expect(src).toContain("/api/devices/anywhere/pair-alerts");
    expect(SCREEN_SRC["Devices.tsx"]).toMatch(/<DevicesPairAlerts /);
    const elsewhere = Object.entries(SCREEN_SRC).filter(
      ([f, t]) => f !== "DevicesPairAlerts.tsx" && t.includes("devices-pair-change-key")
    );
    expect(elsewhere).toEqual([]);
    expect(src).not.toMatch(/window\.(prompt|alert|confirm)\(/);
  });
});
