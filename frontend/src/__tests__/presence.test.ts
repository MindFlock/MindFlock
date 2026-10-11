/** Presence on a session (lib/presence.ts, components/presence/Presence.tsx):
 * the rail row's initials dots and the pane header's one drive control, plus
 * the terminal's control frames (lib/wsTerm.ts controlNotice).
 *
 * What is pinned: nothing is drawn while you are alone on a session; the
 * driver's dot comes first and is ringed, with "Alice (laptop) · driving" as
 * its tooltip; three dots at most, the rest "+N"; a driver without a
 * terminal is still a dot; the header says "Driving" (click lets go) or
 * "Watching — take control" (click takes it) and the click posts the driver
 * route. The vitest environment is node, so the views are checked through
 * react-dom/server. */

import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { Instance, Viewer } from "../api/types";

const instApi = vi.fn();
vi.mock("../api/client", async (orig) => ({
  ...((await orig()) as object),
  instApi: (...a: unknown[]) => instApi(...a),
}));

const { MAX_DOTS, driveControl, initials, presenceDots, setDriver } = await import(
  "../lib/presence"
);
const { DriveButton, PresenceDots } = await import("../components/presence/Presence");
const { controlNotice } = await import("../lib/wsTerm");

const ME = "00000000-0000-4000-8000-00000000000a";
const BOB = "ffffffff-ffff-4fff-8fff-fffffffffffb";
const CAT = "cccccccc-cccc-4ccc-8ccc-cccccccccccc";
const DAN = "dddddddd-dddd-4ddd-8ddd-dddddddddddd";

function v(name: string, device: string, since: number, typing = false): Viewer {
  return { name, device, since, typing };
}

type P = Pick<Instance, "title" | "viewers" | "driver" | "driver_name">;
function inst(p: Partial<P>): P {
  return { title: "s", viewers: {}, driver: "", driver_name: "", ...p };
}

beforeEach(() => instApi.mockReset());

describe("initials", () => {
  it("takes two words, one word, or the device", () => {
    expect(initials("Alice Smith")).toBe("AS");
    expect(initials("bob")).toBe("B");
    expect(initials("", "laptop")).toBe("L");
    expect(initials("")).toBe("?");
  });
});

describe("presenceDots", () => {
  it("draws nothing while you are alone on the session", () => {
    const solo = inst({ driver: ME, viewers: { [ME]: v("Ada", "laptop", 1) } });
    expect(presenceDots(solo, ME)).toEqual({ dots: [], more: 0 });
    expect(renderToStaticMarkup(createElement(PresenceDots, { inst: solo, me: ME }))).toBe("");
  });

  it("puts the driver first, ringed, with who/where/what as the tooltip", () => {
    const i = inst({
      driver: BOB,
      driver_name: "Bob",
      viewers: { [ME]: v("Ada", "laptop", 1), [BOB]: v("Bob", "desk", 5, true) },
    });
    const { dots } = presenceDots(i, ME);
    expect(dots.map((d) => [d.initials, d.tip, d.driving])).toEqual([
      ["B", "Bob (desk) · driving", true],
      ["A", "Ada (laptop) · watching", false],
    ]);
    const html = renderToStaticMarkup(createElement(PresenceDots, { inst: i, me: ME }));
    expect(html).toContain('class="presence-dot driving typing" title="Bob (desk) · driving"');
    expect(html).toContain('class="presence-dot" title="Ada (laptop) · watching"');
  });

  it("shows three at most and folds the rest into +N", () => {
    const i = inst({
      driver: ME,
      viewers: {
        [ME]: v("", "laptop", 1),
        [BOB]: v("", "desk", 2),
        [CAT]: v("", "mini", 3),
        [DAN]: v("", "rig", 4),
      },
    });
    const { dots, more } = presenceDots(i, ME);
    expect(dots).toHaveLength(MAX_DOTS);
    expect(more).toBe(1);
    // nameless: you are "You", another person is their device
    expect(dots[0]!.tip).toBe("You (laptop) · driving");
    expect(dots[1]!.tip).toBe("desk (desk) · watching");
    expect(renderToStaticMarkup(createElement(PresenceDots, { inst: i, me: ME }))).toContain(
      '<span class="presence-more">+1</span>',
    );
  });

  it("counts a driver with no terminal open", () => {
    const i = inst({ driver: BOB, driver_name: "Bob", viewers: {} });
    expect(presenceDots(i, ME).dots.map((d) => d.tip)).toEqual(["Bob · driving"]);
  });
});

describe("the pane header's drive control", () => {
  it("is absent alone, or from a server that predates ownership", () => {
    expect(driveControl(inst({ driver: ME, viewers: { [ME]: v("Ada", "l", 1) } }), ME)).toBeNull();
    expect(driveControl({ viewers: undefined, driver: undefined }, ME)).toBeNull();
  });

  it("says Driving to the driver and lets go on click", async () => {
    const i = inst({ driver: ME, viewers: { [ME]: v("Ada", "l", 1), [BOB]: v("Bob", "d", 2) } });
    const ctl = driveControl(i, ME)!;
    expect(ctl).toMatchObject({ label: "Driving", driving: true, action: "release" });
    expect(ctl.tip).toBe("You hold the keyboard — Bob watching. Click to let go.");
    const html = renderToStaticMarkup(createElement(DriveButton, { inst: i, me: ME }));
    expect(html).toContain('class="drive-ctl driving"');
    expect(html).toContain(">Driving</button>");
  });

  it("says Watching — take control to everyone else", () => {
    const i = inst({ driver: BOB, driver_name: "Bob", viewers: { [ME]: v("Ada", "l", 1) } });
    const ctl = driveControl(i, ME)!;
    expect(ctl).toMatchObject({ label: "Watching — take control", action: "take" });
    expect(ctl.tip).toContain("Bob is driving");
    const html = renderToStaticMarkup(createElement(DriveButton, { inst: i, me: ME }));
    expect(html).toContain('class="drive-ctl watching"');
    expect(html).toContain(">Watching — take control</button>");
  });

  it("offers Take control while nobody drives and others watch", () => {
    const i = inst({ driver: "", viewers: { [BOB]: v("Bob", "d", 2) } });
    expect(driveControl(i, ME)).toMatchObject({ label: "Take control", action: "take" });
  });

  it("posts the driver route, remote titles included", async () => {
    instApi.mockResolvedValue({});
    await setDriver("mini::s", "take");
    await setDriver("s", "release");
    expect(instApi.mock.calls).toEqual([
      ["mini::s", "/driver", { json: { take: true } }],
      ["s", "/driver", { json: { release: true } }],
    ]);
  });
});

describe("terminal control frames", () => {
  it("shows a readonly notice and swallows driver frames", () => {
    expect(controlNotice({ type: "readonly", message: "Bob is driving — take control to type" })).toEqual({
      notice: "Bob is driving — take control to type",
    });
    expect(controlNotice({ type: "driver", driving: false, driver: BOB })).toEqual({ notice: null });
    expect(controlNotice({ type: "error", message: "x" })).toBeNull();
    expect(controlNotice(3)).toBeNull();
  });
});
