import { afterEach, describe, expect, it, vi } from "vitest";
import {
  hasNativeWindowControls,
  inShell,
  isFullScreen,
  isMacShell,
  onFullScreenChanged,
} from "../lib/shell";

const g = globalThis as Record<string, unknown>;

function setWindow(w: unknown) {
  if (w === undefined) delete g.window;
  else g.window = w;
}

// Restore, don't just delete: the node environment has no `window`, but the
// setup file (and any future test in this worker) may add DOM globals of its
// own — clobbering one would make failures depend on file order.
const hadWindow = "window" in g;
const originalWindow = g.window;

afterEach(() => setWindow(hadWindow ? originalWindow : undefined));

describe("isMacShell", () => {
  it("is true in the desktop shell on macOS", () => {
    setWindow({ mfshell: { platform: "darwin" } });
    expect(isMacShell()).toBe(true);
  });

  it("is false for a browser tab ON a Mac", () => {
    // The decisive case: Safari/Chrome on macOS has its own chrome and no
    // traffic lights to leave room for, so the layout must NOT shift. Only the
    // preload bridge proves we're in the shell — never the user agent.
    setWindow({});
    expect(isMacShell()).toBe(false);
    expect(inShell()).toBe(false);
  });

  it("is false in the shell on other platforms", () => {
    setWindow({ mfshell: { platform: "win32" } });
    expect(isMacShell()).toBe(false);
    expect(inShell()).toBe(true);
    setWindow({ mfshell: { platform: "linux" } });
    expect(isMacShell()).toBe(false);
  });

  it("survives a missing window entirely", () => {
    setWindow(undefined);
    expect(isMacShell()).toBe(false);
    expect(inShell()).toBe(false);
  });

  it("counts a bridge with no platform as the shell, but not as a Mac", () => {
    // inShell() asks "is the preload bridge here", isMacShell() asks a strictly
    // narrower question — an unexpected/absent platform must not answer yes.
    setWindow({ mfshell: {} });
    expect(inShell()).toBe(true);
    expect(isMacShell()).toBe(false);
  });
});

describe("hasNativeWindowControls", () => {
  it("is true only when the shell says it uses a native title bar", () => {
    setWindow({ mfshell: { platform: "darwin", nativeTitleBar: true } });
    expect(hasNativeWindowControls()).toBe(true);
  });

  it("is false on a mac shell built BEFORE the traffic-light move", () => {
    // The version-skew case that matters: the engine (this frontend) updates
    // independently of the desktop app. An older shell still injects its own
    // – □ ✕ top-right, so mirroring the cluster there would stack them.
    setWindow({ mfshell: { platform: "darwin" } });
    expect(isMacShell()).toBe(true); // it IS a mac shell...
    expect(hasNativeWindowControls()).toBe(false); // ...but keeps its own layout
  });

  it("is false on Windows/Linux shells and in a browser", () => {
    setWindow({ mfshell: { platform: "win32", nativeTitleBar: false } });
    expect(hasNativeWindowControls()).toBe(false);
    setWindow({});
    expect(hasNativeWindowControls()).toBe(false);
    setWindow(undefined);
    expect(hasNativeWindowControls()).toBe(false);
  });

  it("demands a real boolean — a truthy string or 1 doesn't move the layout", () => {
    // The check is `=== true` on purpose. This flag decides where the window
    // controls are; a sloppy future bridge value must fail closed, keeping the
    // layout the shell actually draws rather than guessing from truthiness.
    for (const value of ["true", 1, {}, "darwin"]) {
      setWindow({ mfshell: { platform: "darwin", nativeTitleBar: value } });
      expect(hasNativeWindowControls()).toBe(false);
    }
  });
});

describe("onFullScreenChanged", () => {
  it("subscribes through the shell bridge and returns its unsubscribe", () => {
    const off = vi.fn();
    const sub = vi.fn(() => off);
    setWindow({ mfshell: { platform: "darwin" }, winctl: { onFullScreenChanged: sub } });
    const unsub = onFullScreenChanged(() => {});
    expect(sub).toHaveBeenCalledOnce();
    unsub();
    expect(off).toHaveBeenCalledOnce();
  });

  it("forwards the fullscreen payload verbatim", () => {
    // The top bar keys `data-mac-lights` off this value, so true/false have to
    // arrive unchanged and in order — not coerced, not swallowed.
    let fire!: (isFull: boolean) => void;
    setWindow({
      winctl: {
        onFullScreenChanged: (cb: (isFull: boolean) => void) => {
          fire = cb;
          return () => {};
        },
      },
    });
    const seen: boolean[] = [];
    onFullScreenChanged((isFull) => seen.push(isFull));
    fire(true);
    fire(false);
    expect(seen).toEqual([true, false]);
  });

  it("returns a callable no-op when the bridge predates the method", () => {
    // An older shell build has no onFullScreenChanged; the effect cleanup still
    // has to be safe to call.
    setWindow({ mfshell: { platform: "darwin" }, winctl: {} });
    expect(() => onFullScreenChanged(() => {})()).not.toThrow();
    setWindow({});
    expect(() => onFullScreenChanged(() => {})()).not.toThrow();
  });

  it("tolerates a bridge that returns nothing from subscribe", () => {
    setWindow({ winctl: { onFullScreenChanged: () => undefined } });
    expect(() => onFullScreenChanged(() => {})()).not.toThrow();
  });

  it("tolerates a bridge that returns a non-function from subscribe", () => {
    // React calls whatever the effect returns; a bridge handing back a number
    // or an object must not turn unmount into a TypeError.
    for (const value of [42, {}, null, "off"]) {
      setWindow({ winctl: { onFullScreenChanged: () => value } });
      expect(() => onFullScreenChanged(() => {})()).not.toThrow();
    }
  });

  it("returns a callable no-op with no window at all", () => {
    setWindow(undefined);
    expect(() => onFullScreenChanged(() => {})()).not.toThrow();
  });
});

describe("isFullScreen", () => {
  it("reports the shell's current state", async () => {
    setWindow({ winctl: { isFullScreen: () => Promise.resolve(true) } });
    expect(await isFullScreen()).toBe(true);
  });

  it("is false in a browser, on an older shell, and when the IPC rejects", async () => {
    // The initial-paint query must never be what breaks the top bar: without a
    // bridge, without the method, or on a rejected invoke, assume windowed.
    setWindow({});
    expect(await isFullScreen()).toBe(false);
    setWindow({ winctl: {} });
    expect(await isFullScreen()).toBe(false);
    setWindow({ winctl: { isFullScreen: () => Promise.reject(new Error("no window")) } });
    expect(await isFullScreen()).toBe(false);
    setWindow(undefined);
    expect(await isFullScreen()).toBe(false);
  });

  it("demands a real true — a truthy resolve doesn't count", async () => {
    setWindow({ winctl: { isFullScreen: () => Promise.resolve("yes" as unknown as boolean) } });
    expect(await isFullScreen()).toBe(false);
  });
});
