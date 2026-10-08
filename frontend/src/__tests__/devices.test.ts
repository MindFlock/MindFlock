import { describe, expect, it } from "vitest";
import type { Device } from "../api/types";
import { canDisconnect, deviceLabel, devicePath, deviceTitle, startableDevices } from "../lib/devices";
import { FOLDER_INIT, folderReducer } from "../components/dialogs/NewSessionDialog";

const dev = (over: Partial<Device>): Device => ({
  device: "box",
  host: "Box",
  connected: true,
  ...over,
});

describe("starting a session on another device", () => {
  it("leaves this device's paths alone and forwards another's", () => {
    expect(devicePath("", "/api/repos/suggest")).toBe("/api/repos/suggest");
    expect(devicePath("other-box", "/api/repos/check?path=~%2Fx")).toBe(
      "/api/devices/other-box/fwd/api/repos/check?path=~%2Fx"
    );
  });

  it("namespaces a remote session's title the way the merged list does", () => {
    expect(deviceTitle("", "fix-login")).toBe("fix-login");
    expect(deviceTitle("other-box", "fix-login")).toBe("other-box::fix-login");
  });

  it("offers only devices that can take a session right now", () => {
    const resp = {
      self: { device: "me", host: "Me" },
      devices: [
        dev({ device: "ok" }),
        dev({ device: "unpaired", connected: false, needs_token: true }),
        dev({ device: "asleep", connected: false, reachable: false }),
      ],
    };
    expect(startableDevices(resp).map((d) => d.device)).toEqual(["ok"]);
    expect(startableDevices(undefined)).toEqual([]);
  });

  it("names a device by host unless the host is ambiguous", () => {
    const a = dev({ device: "laptop", host: "Laptop" });
    const b = dev({ device: "desk", host: "Desk" });
    const twin = dev({ device: "desk-1", host: "Desk" });
    expect(deviceLabel(a, [a, b])).toBe("Laptop");
    expect(deviceLabel(b, [a, b, twin])).toBe("desk");
    expect(deviceLabel(a, [a], "Laptop")).toBe("laptop");
  });

  it("a device switch empties the folder so that device's own suggestion fills it", () => {
    const typed = folderReducer(FOLDER_INIT, { t: "user-set", path: "/home/me/code/api" });
    const switched = folderReducer(typed, { t: "device" });
    expect(switched).toEqual(FOLDER_INIT);
    // ...and the new device's suggestion is then allowed in.
    expect(folderReducer(switched, { t: "suggested", path: "/Users/me/api" }).path).toBe(
      "/Users/me/api"
    );
  });
});

describe("the sidebar's Disconnect ✕", () => {
  it("is offered for a token-paired device, never for one of Your devices", () => {
    expect(canDisconnect(dev({ has_token: true }))).toBe(true);
    // A member is reached with the shared device key: forgetting its pasted
    // token disconnects nothing (the server answers 409).
    expect(canDisconnect(dev({ has_token: true, member: true }))).toBe(false);
    expect(canDisconnect(dev({ has_token: false }))).toBe(false);
  });
});
