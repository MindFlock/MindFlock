/** Notification wiring in the React app: the bell (notification center) and
 * Settings → Notifications (per-rule switches, the ntfy phone-push section).
 *
 * The bell replays the events-bus backlog ("what happened while I was away")
 * and badges unread events. Pinned: its markup hooks, that it is fed by every
 * event and curated by `notifFromEvent`, that unread keys on timestamp (which
 * survives a server seq reset), and the turn-boundary row's wording.
 *
 * Moved from tests/unit/test_frontend_notifications.py, test_notify_rules.py
 * and test_ntfy.py, which grepped the built app.js / style.css. The served
 * notify addon (/addons/notify.js, hand-written) and the route contract stay
 * in Python. */

import { describe, expect, it } from "vitest";
import { SRC, dropComments, src } from "./sourceText";
import { notifFromEvent } from "../components/NotificationsBell";
import type { EventEnvelope } from "../state/queries";

/** All app code, comments removed — the shipped bundle never carried them,
 * and the bell's own comments quote the wording that must stay retired. */
const ALL_CODE = Object.values(SRC).map(dropComments).join("\n");

const BELL = src("components/NotificationsBell.tsx");
const SCREEN = src("components/settings/screens/Notifications.tsx");

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

describe("the bell", () => {
  it("has its button and badge", () => {
    expect(BELL).toContain('id="notif-btn"');
    expect(BELL).toContain('id="notif-badge"');
  });

  it("is fed by every event on the bus, curated into notifications", () => {
    expect(BELL).toContain('subscribe("*"');
    expect(BELL).toContain("const n = notifFromEvent(env)");
    // Unread is keyed on timestamp (survives server seq resets), not seq.
    expect(BELL).toContain('const NOTIF_SEEN_KEY = "mf_notif_seen_ts"');
    expect(BELL).toContain("localStorage.setItem(NOTIF_SEEN_KEY");
    // Clicking a notification focuses that session.
    expect(BELL).toContain("selectSession(title)");
  });

  it("surfaces the curated event kinds", () => {
    const kinds: Array<Partial<EventEnvelope>> = [
      { event: "session.budget_exceeded", data: { cost: 3 } },
      { event: "session.prompt_sent", data: { remaining: 2 } },
      { event: "session.activity_changed", new: "clarify" },
      { event: "session.stage_changed", new: "pushed" },
      // "the agent has finished" comes from the turn-boundary event, never from
      // the raw idle flip — that one fires at the end of every assistant turn
      // and for a window that has merely been re-opened.
      { event: "session.turn_ended", data: { idle_for: 120 } },
    ];
    for (const k of kinds) {
      expect(BELL, k.event).toContain(`case "${k.event}"`);
      expect(notifFromEvent(env(k)), k.event).not.toBeNull();
    }
    expect(notifFromEvent(env({ event: "session.activity_changed", new: "idle" }))).toBeNull();
    expect(ALL_CODE).not.toContain("finished — now idle");
  });

  it("renders the dwell the turn boundary carries, as an n-done row", () => {
    // The event's whole claim is that the quiet LASTED; a bare "finished" throws
    // away the part that makes it trustworthy. Minutes past a point, so a session
    // left alone all afternoon does not report "finished — idle 9412s".
    expect(notifFromEvent(env({ event: "session.turn_ended", data: { idle_for: 9412 } }))).toEqual({
      text: "finished — idle 157m",
      cls: "n-done",
    });
    expect(notifFromEvent(env({ event: "session.turn_ended", data: { idle_for: 45 } }))?.text).toBe(
      "finished — idle 45s"
    );
  });

  it("is styled", () => {
    const css = src("components/NotificationsBell.css");
    for (const sel of ["#notif-btn {", ".notif-badge {", ".notif-pop {", ".notif-item {"]) {
      expect(css, sel).toContain(sel);
    }
  });
});

describe("Settings → Notifications", () => {
  it("renders a per-rule switch that applies without a reload", () => {
    expect(SCREEN).toContain("await api(`/api/notify/rules/${encodeURIComponent(rule.id)}`");
    expect(SCREEN).toContain("notifApi()?.refreshRules?.();");
  });

  it("wires the ntfy section", () => {
    expect(SCREEN).toContain('api<NtfyView>("/api/notify/ntfy")');
    expect(SCREEN).toContain('"/api/notify/ntfy/test"');
    // Generate uses the server's random topic.
    expect(SCREEN).toContain("view.suggested_topic");
  });
});
