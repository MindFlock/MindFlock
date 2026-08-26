/** The bell feed's curation: which events become a row in "what happened while
 * you were away", and — more to the point — which do not.
 *
 * The bell is the one notification channel with no opt-in gate and no dedupe:
 * it subscribes to "*" and renders whatever `notifFromEvent` returns. So it was
 * the channel actually producing the jumpy "finished" the user saw, whether or
 * not they had ever turned the ntfy/desktop rule on. */

import { describe, it, expect } from "vitest";
import { notifFromEvent } from "../components/NotificationsBell";
import type { EventEnvelope } from "../state/queries";

const env = (e: Partial<EventEnvelope>): EventEnvelope => ({
  event: "",
  session: "s",
  seq: 1,
  ts: 0,
  old: null,
  new: null,
  data: {},
  ...e,
});

describe("notifFromEvent", () => {
  it("ignores a raw idle flip — that is a chip colour, not news", () => {
    expect(notifFromEvent(env({ event: "session.activity_changed", new: "idle" }))).toBeNull();
  });

  it("still ignores working and offline", () => {
    expect(notifFromEvent(env({ event: "session.activity_changed", new: "working" }))).toBeNull();
    expect(notifFromEvent(env({ event: "session.activity_changed", new: "offline" }))).toBeNull();
  });

  it("keeps clarify — a question needs answering now", () => {
    const n = notifFromEvent(env({ event: "session.activity_changed", new: "clarify" }));
    expect(n?.text).toBe("needs your input");
    expect(n?.cls).toBe("n-warn");
  });

  it("reports a real turn boundary as the finish", () => {
    const n = notifFromEvent(env({ event: "session.turn_ended", data: { idle_for: 46 } }));
    expect(n?.text).toContain("finished");
    expect(n?.cls).toBe("n-done");
  });
});
