// Flow polish seams: pieces two workers each owned half of.
import { describe, expect, it } from "vitest";
import { notifFromEvent } from "../components/NotificationsBell";

describe("a route's Notify stop shows in the bell", () => {
  const env = (data: Record<string, unknown>) =>
    ({ event: "flight.notified", session: "fix-login", data, seq: 1, at: 0 }) as never;

  it("lists the author's text", () => {
    const row = notifFromEvent(env({ id: "f_1", text: "PR is up — take a look", phone: false, why: "phone notifications are off" }));
    expect(row?.text).toBe("PR is up — take a look");
    expect(row?.cls).toBe("n-info");
  });

  it("an empty text makes no row", () => {
    expect(notifFromEvent(env({ id: "f_1", text: "  " }))).toBeNull();
  });
});
