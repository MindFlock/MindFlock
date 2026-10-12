/** Send / prompt-queue wiring (M-series): the Queue pane tab, its inline
 * console and the palette's Queue prompt… — pinned at the modules that own
 * them, so the tab, its console and the palette actions can't silently
 * regress. Moved from tests/unit/test_frontend_queue.py, which grepped the
 * built bundle; the routes the console calls existing on the server stays a
 * Python contract test there. */

/// <reference types="vite/client" />
import { describe, expect, it } from "vitest";
import { ALL_CODE } from "./sourceText";
import PANE from "../components/grid/Pane.tsx?raw";
import QUEUE from "../components/grid/QueueTab.tsx?raw";
import QUEUE_CSS from "../components/grid/QueueTab.css?raw";
import TABLE from "../lib/sessionActionTable.ts?raw";
import PALETTE from "../components/palette/CommandPalette.tsx?raw";
import PLAYBOOKS from "../lib/playbooks.ts?raw";


describe("the Queue tab", () => {
  it("is a first-class pane tab (beside Agent/Terminal/Diff), with a badge", () => {
    expect(PANE).toContain('data-tab="queue"');
    expect(PANE).toContain('<span className="queue-tab-badge">');
    expect(PANE).toContain('"pane-queue" + (tab !== "queue" ? " hidden" : "")');
    // Selecting the tab loads the inline console.
    expect(PANE).toContain('import { QueueTab } from "./QueueTab";');
    expect(PANE).toContain("<QueueTab title={title}");
    // Badge painted from the /api/instances queue summary each poll.
    expect(PANE).toContain("const q = inst.queue;");
  });

  it("its console calls the queue endpoints and carries its toggles", () => {
    expect(QUEUE).toContain("Add to queue");
    // Send-now hits /send; add hits /queue; flags + reorder + delete wired.
    expect(QUEUE).toContain('qApi(title, "/send", { json: { text } })');
    expect(QUEUE).toContain('qApi(title, "/queue", { json: { text } })');
    expect(QUEUE).toContain('mutate("/queue/flags", { json: f })');
    expect(QUEUE).toContain('mutate("/queue/reorder", { json: { id, index: to } })');
    expect(QUEUE).toContain('mutate("/queue", { method: "DELETE" })');
    // Per-item ▶ fires one queued entry immediately via the send_now endpoint.
    expect(QUEUE).toContain('qApi(title, "/queue/send_now", { json: { id: it.id } })');
    expect(QUEUE).toContain('className="qi-send"');
    // Loop + auto-run + wait-out-usage-limit toggles present.
    expect(QUEUE).toContain('className="queue-loop"');
    expect(QUEUE).toContain('className="queue-enabled"');
    expect(QUEUE).toContain('className="queue-wait"');
    // The wait-out toggle posts the wait_for_limit flag.
    expect(QUEUE).toContain("flags({ wait_for_limit: e.target.checked })");
    // Timed loop: an interval input that posts loop_interval (minutes).
    expect(QUEUE).toContain('className="queue-loop-interval"');
    expect(QUEUE).toContain("flags({ loop_interval: n })");
  });
});

describe("the palette's Message… and Queue prompt…", () => {
  it("are action-table rows the palette lists", () => {
    expect(TABLE).toContain('label: "Message…"');
    expect(TABLE).toContain('label: "Queue prompt…"');
    expect(PALETTE).toContain('"message",');
    expect(PALETTE).toContain('"queue",');
  });

  it("no longer ask with window.prompt", () => {
    // Both used to, a no-op in Electron. Message… now opens the Thread
    // composer and Queue prompt… the Queue tab's own box (pinned in
    // fastTrackSurfaces.test.ts); the old helpers must stay gone.
    expect(ALL_CODE).not.toContain("function sendMessagePrompt(");
    expect(ALL_CODE).not.toContain("function queuePromptPrompt(");
    expect(PLAYBOOKS).toContain("export function focusQueueInput(");
  });
});

describe("styles", () => {
  it("QueueTab.css styles the tab, its console and its states", () => {
    for (const sel of [
      ".queue-tab",
      ".queue-tab-badge",
      ".pane-queue",
      ".queue-input",
      ".queue-item",
      ".queue-flags",
      ".queue-status.queue-stopped",
    ])
      expect(QUEUE_CSS, sel).toContain(sel);
  });
});
