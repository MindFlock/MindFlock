/** A click or a double-click places once (lib/clickOrDouble, docs/flights.md
 * "Round 14" → "The palette fills itself"): a click waits and places; a
 * double-click (click, click, dblclick) places once; the keyboard places at
 * once; two slow clicks that the OS still calls a double place once. */

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { clickOrDouble } from "../lib/clickOrDouble";
import { code } from "./sourceText";

describe("clickOrDouble", () => {
  beforeEach(() => {
    vi.useFakeTimers();
    vi.setSystemTime(1_000_000);
  });
  afterEach(() => vi.useRealTimers());

  it("a click places after the delay, not before", () => {
    const place = vi.fn();
    const cd = clickOrDouble(place);
    cd.onClick({ detail: 1 });
    vi.advanceTimersByTime(249);
    expect(place).not.toHaveBeenCalled();
    vi.advanceTimersByTime(1);
    expect(place).toHaveBeenCalledTimes(1);
    vi.advanceTimersByTime(1000);
    expect(place).toHaveBeenCalledTimes(1);
  });

  it("a double-click (click, click, dblclick) places once, at the dblclick", () => {
    const place = vi.fn();
    const cd = clickOrDouble(place);
    cd.onClick({ detail: 1 });
    vi.advanceTimersByTime(90);
    cd.onClick({ detail: 2 });
    expect(place).not.toHaveBeenCalled();
    cd.onDoubleClick();
    expect(place).toHaveBeenCalledTimes(1);
    vi.advanceTimersByTime(1000);
    expect(place).toHaveBeenCalledTimes(1);
  });

  it("a slow double — the first click placed already, the OS still sends a dblclick — places once", () => {
    const place = vi.fn();
    const cd = clickOrDouble(place);
    cd.onClick({ detail: 1 });
    vi.advanceTimersByTime(300);
    expect(place).toHaveBeenCalledTimes(1);
    cd.onClick({ detail: 2 });
    cd.onDoubleClick();
    vi.advanceTimersByTime(1000);
    expect(place).toHaveBeenCalledTimes(1);
  });

  it("a later double-click is a new one: it places again", () => {
    const place = vi.fn();
    const cd = clickOrDouble(place);
    cd.onClick({ detail: 1 });
    vi.advanceTimersByTime(800);
    cd.onClick({ detail: 1 });
    cd.onClick({ detail: 2 });
    cd.onDoubleClick();
    vi.advanceTimersByTime(1000);
    expect(place).toHaveBeenCalledTimes(2);
  });

  it("two separate clicks place twice", () => {
    const place = vi.fn();
    const cd = clickOrDouble(place);
    cd.onClick();
    vi.advanceTimersByTime(260);
    cd.onClick();
    vi.advanceTimersByTime(260);
    expect(place).toHaveBeenCalledTimes(2);
  });

  it("the keyboard's click (detail 0) places at once, dropping a waiting click", () => {
    const place = vi.fn();
    const cd = clickOrDouble(place);
    cd.onClick({ detail: 1 });
    cd.onClick({ detail: 0 });
    expect(place).toHaveBeenCalledTimes(1);
    vi.advanceTimersByTime(1000);
    expect(place).toHaveBeenCalledTimes(1);
  });

  it("a lone dblclick places; cancel drops a waiting click; the delay is an option", () => {
    const place = vi.fn();
    const cd = clickOrDouble(place, { delay: 100 });
    cd.onDoubleClick();
    expect(place).toHaveBeenCalledTimes(1);
    vi.advanceTimersByTime(1000);
    cd.onClick();
    cd.cancel();
    vi.advanceTimersByTime(1000);
    expect(place).toHaveBeenCalledTimes(1);
    cd.onClick();
    vi.advanceTimersByTime(100);
    expect(place).toHaveBeenCalledTimes(2);
  });

  it("the hook keeps one per mount, calls the newest place, cancels on unmount", () => {
    const src = code("lib/clickOrDouble.ts");
    expect(src).toContain("if (!cd.current) cd.current = clickOrDouble(() => latest.current?.(), opts);");
    expect(src).toContain("useEffect(() => () => cd.current?.cancel(), []);");
  });
});
