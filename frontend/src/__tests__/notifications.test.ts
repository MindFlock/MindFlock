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

  it("says a stage change as what happened, not as an arrow", () => {
    const say = (stage: string) => notifFromEvent(env({ event: "session.stage_changed", new: stage }))?.text;
    expect(say("committed")).toBe("committed");
    expect(say("pushed")).toBe("pushed");
    expect(say("pr")).toBe("opened a PR");
    expect(say("merged")).toBe("merged");
    expect(say("interrupt")).toBe("pre-commit failed");
    expect(say("precommit")).toBe("running pre-commit hooks");
    // Anything else reads as the raw value — never "stage → …".
    expect(say("agent")).toBe("agent");
  });

  it("says a drained queue prompt as sent, with what is left", () => {
    const n = notifFromEvent(env({ event: "session.prompt_sent", data: { remaining: 2 } }));
    expect(n?.text).toBe("sent the next queued prompt (2 left)");
  });
});

describe("notifFromEvent — your devices", () => {
  it("a join request is a warn row carrying the server's detail, marked as a device row", () => {
    const n = notifFromEvent(
      env({
        event: "device.join_requested",
        session: "",
        data: { device: "mini", host: "mac-mini", code: "123 456", detail: "mac-mini · code 123 456" },
      })
    );
    expect(n).toEqual({ text: "mac-mini · code 123 456", cls: "n-warn", device: true });
  });

  it("joined is done, removed is info — both click through to Settings → Devices", () => {
    expect(notifFromEvent(env({ event: "device.joined", session: "", data: { host: "ml-rig" } }))).toEqual({
      text: "ml-rig joined your devices",
      cls: "n-done",
      device: true,
    });
    const removed = notifFromEvent(env({ event: "device.removed", session: "", data: { device: "rig" } }));
    expect(removed?.cls).toBe("n-info");
    expect(removed?.device).toBe(true);
  });

  it("settings.synced is not news for the bell", () => {
    expect(notifFromEvent(env({ event: "settings.synced", session: "", data: { paths: ["ui.accent"] } }))).toBeNull();
  });
});
