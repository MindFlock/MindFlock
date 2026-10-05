/** MindFlock MCP, UI wording: the session.message toast, the bell's curation
 * of it (results only), and the rail row's lineage marker. */

import { describe, it, expect } from "vitest";
import {
  lineageMark,
  messageNotif,
  messageToastText,
  snippet,
} from "../lib/agentMessages";
import { notifFromEvent } from "../components/NotificationsBell";
import type { EventEnvelope } from "../state/queries";

const id = (t: string) => t;
const aliased = (t: string) => (t === "orch" ? "Orchestrator" : t);

describe("snippet", () => {
  it("is one line and cut at 80 chars with an ellipsis", () => {
    const s = snippet("line one\nline two\t" + "x".repeat(200));
    expect(s).not.toMatch(/[\n\t]/);
    expect(s.length).toBe(80);
    expect(s.endsWith("…")).toBe(true);
  });

  it("leaves a short body alone", () => {
    expect(snippet("  rebase first  ")).toBe("rebase first");
  });
});

describe("messageToastText", () => {
  it("names sender → recipient with the body", () => {
    expect(messageToastText("w1", { from: "orch", kind: "message", text: "rebase onto main" }, id)).toBe(
      "✉ orch → w1: rebase onto main"
    );
  });

  it("uses the display name (aliases) for both ends", () => {
    expect(messageToastText("w1", { from: "orch", text: "hi" }, aliased)).toBe("✉ Orchestrator → w1: hi");
  });

  it("calls an empty sender external (CLI / outside MCP client)", () => {
    expect(messageToastText("w1", { from: "", text: "hi" }, id)).toBe("✉ external → w1: hi");
  });

  it("reads a worker report as a report, with its status when the event has one", () => {
    expect(messageToastText("orch", { from: "w1", kind: "result", text: "done", status: "done" }, id)).toBe(
      "✓ worker w1 reported: done — done"
    );
    expect(messageToastText("orch", { from: "w1", kind: "result", text: "tests pass" }, id)).toBe(
      "✓ worker w1 reported: tests pass"
    );
    expect(messageToastText("orch", { from: "w2", kind: "result", text: "no creds", status: "blocked" }, id)).toBe(
      "⚠ worker w2 reported: blocked — no creds"
    );
  });
});

describe("messageNotif (bell feed)", () => {
  it("keeps plain agent messages out of the feed", () => {
    expect(messageNotif({ from: "orch", kind: "message", text: "hi" }, id)).toBeNull();
    expect(messageNotif({}, id)).toBeNull();
  });

  it("lists a worker's report, warn-coloured when it failed", () => {
    expect(messageNotif({ from: "w1", kind: "result", text: "all green" }, id)).toEqual({
      text: "worker w1 reported — all green",
      cls: "n-done",
    });
    expect(messageNotif({ from: "w1", kind: "result", text: "x", status: "failed" }, id)?.cls).toBe("n-warn");
  });

  it("is what the bell's notifFromEvent returns for session.message", () => {
    const env = (data: Record<string, unknown>): EventEnvelope => ({
      event: "session.message",
      session: "orch",
      seq: 1,
      ts: 0,
      old: null,
      new: null,
      data,
    });
    expect(notifFromEvent(env({ from: "orch2", kind: "message", text: "hi" }))).toBeNull();
    expect(notifFromEvent(env({ from: "w1", kind: "result", text: "done" }))?.text).toBe(
      "worker w1 reported — done"
    );
  });
});

describe("lineageMark", () => {
  it("is null for a plain session", () => {
    expect(lineageMark("", false, id)).toBeNull();
    expect(lineageMark(undefined, undefined, id)).toBeNull();
  });

  it("marks an agent-spawned child with its parent", () => {
    expect(lineageMark("orch", true, aliased)).toEqual({
      text: "↳ Orchestrator",
      title: "Spawned by the agent in “Orchestrator”",
      spawned: true,
    });
  });

  it("marks an adopted (human-made) child without the spawned tint", () => {
    const m = lineageMark("orch", false, id);
    expect(m?.spawned).toBe(false);
    expect(m?.text).toBe("↳ orch");
    expect(m?.title).toContain("adopted");
  });

  it("still flags an orphaned spawned session as agent-made", () => {
    const m = lineageMark("", true, id);
    expect(m?.spawned).toBe(true);
    expect(m?.text).toBe("↳ agent");
  });
});
