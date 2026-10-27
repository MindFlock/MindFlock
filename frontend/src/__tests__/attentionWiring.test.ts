/** O1–O4 frontend wiring: the needs-attention section of the bell (O1), the
 * setup/check chips and their menu actions (O2/O3), the push soft-gate
 * override (O3) and the preview-port link (O4).
 *
 * Moved from tests/unit/test_frontend_attention.py, which grepped the built
 * app.js / style.css (its mobile.js test stays there: /m is hand-written and
 * not bundled). The pure pieces are called; the wiring is pinned in the
 * source module that owns it (sourceText.ts: comments stripped, layout-blind). */

import { describe, expect, it, vi } from "vitest";
import type { Instance } from "../api/types";
import { attentionItems } from "../components/sidebar/ordering";
import { REASON_LABEL, STATE_LABEL, deriveSessionState } from "../lib/sessionState";
import { checkChip } from "../lib/stage";
import INDEX_HTML from "../../index.html?raw";
import { CSS, allCode, normCode, fn, has } from "./sourceText";

// EventToasts reads `document` at import (the same shim runs.test.ts uses),
// and the title badge draws a favicon with `Image` — neither is under test.
vi.mock("../lib/titleBadge", () => ({ clearAttention: vi.fn(), updateTitleBadge: vi.fn(), useTitleBadge: vi.fn() }));
const g = globalThis as Record<string, unknown>;
if (typeof g.document === "undefined") g.document = { hidden: false };
const { attentionToast } = await import("../components/EventToasts");

const BELL = "components/NotificationsBell.tsx";
const ROW = "components/sidebar/SidebarRow.tsx";
const inst = (title: string, extra: Partial<Instance> = {}) =>
  ({ title, status: "running", activity: "idle", stage: "agent", branch: "", ...extra }) as Instance;

describe("O1: needs-attention, surfaced via the notification bell", () => {
  it("the attention source and its priorities", () => {
    // Priorities: question (with snippet) > broken > checks > ready-for-PR.
    // The words are lib/sessionState's labels — one per state, the same on
    // the chip, in the bell and in the toast.
    expect(REASON_LABEL).toMatchObject({
      question: "Question",
      setup_failed: "Setup failed",
      checks_failing: "Checks failing",
      precommit_failed: "Pre-commit failed",
    });
    const items = attentionItems([
      inst("pushed", { stage: "pushed" }),
      inst("checks", { check: { state: "failed", rc: 1 } } as Partial<Instance>),
      inst("asks", { activity: "clarify", last_turn: "which db?" }),
    ]);
    expect(items.map((i) => i.title)).toEqual(["asks", "checks", "pushed"]);
    expect(items[0]).toMatchObject({ reason: "Question", snippet: "which db?" });
    expect(items[2].reason).toBe("pushed — ready for PR");
    expect(has(fn("components/sidebar/ordering.ts", "attentionItems"), 'reason: "pushed — ready for PR"')).toBe(true);
  });

  it("attention is in the bell, not the sidebar", () => {
    // Attention rides the notification bell popover + badge — it touches
    // neither the session list nor the pane tabs (no sidebar bar/overlay).
    const bell = fn(BELL, "NotificationsBell");
    // Live section pinned in the bell popover + badge/refresh wiring.
    expect(bell).toContain('<div className="notif-attn-head">Needs you</div>');
    expect(has(bell, 'className={"notif-badge attn" + (attn.length === 0 ? " hidden" : "")} id="notif-badge"')).toBe(true);
    expect(has(bell, 'className={"attn-item p" + a.p')).toBe(true);
    expect(has(bell, "attentionItems(instances)")).toBe(true);
    // Badge NUMBER is attention-only (auto-clears on resolve); unread history
    // is demoted to a dot shown only when nothing is actionable.
    expect(has(bell, '{attn.length > 99 ? "99+" : String(attn.length)}')).toBe(true);
    expect(has(bell, '(attn.length === 0 && unread > 0 ? " has-unread" : "")')).toBe(true);
    // The retired sidebar bar/overlay is fully gone.
    expect(INDEX_HTML).not.toContain("attn-bar");
    expect(INDEX_HTML).not.toContain("attn-panel");
    const all = allCode();
    expect(all).not.toContain("attn-box");
    expect(all).not.toContain("updateAttention");
    // Clicking a pinned row focuses the session and closes the popover.
    expect(has(bell, "onClick={() => jump(it.title)}")).toBe(true);
  });

  it("the clarify notification carries the snippet", () => {
    // The toast is "<slot> <name>: <label> — “<last turn>”" (attentionToast).
    const ss = deriveSessionState(inst("a", { activity: "clarify" }), { activity: "clarify" });
    expect(attentionToast("1 a", ss, "which db?")).toBe("1 a: Question — “which db?”");
    expect(attentionToast("1 a", ss)).toBe("1 a: Question");
    // The ask's own question when the row has one (lib/ask.ts), else the last turn.
    expect(
      has(normCode("components/EventToasts.tsx"), "attentionToast(namedSlot(env.session), ss, inst.asking?.text || inst.last_turn)")
    ).toBe(true);
  });

  it("the attention styles", () => {
    // Attention lives in the bell popover now: amber badge + pinned section;
    // unread history is a neutral dot.
    const bellCss = normCode("components/NotificationsBell.css");
    for (const sel of [
      ".notif-badge.attn {",
      "#notif-btn.has-unread::after {",
      ".notif-attn {",
      ".notif-attn-head {",
      ".attn-item {",
      ".attn-snippet {",
    ]) {
      expect(bellCss, sel).toContain(sel);
    }
    expect(normCode("components/capsGate.css")).toContain(".stagechip.checkchip {");
    // The retired sidebar bar/overlay CSS is gone.
    for (const [file, css] of Object.entries(CSS)) {
      expect(css, file).not.toContain("#attn-bar");
      expect(css, file).not.toContain("#attn-panel");
    }
  });
});

describe("O2/O3: chips, menu actions, gate override", () => {
  it("setup states in the chip", () => {
    // A failed setup needs you ("Setup failed"); a running one is "Starting".
    expect(STATE_LABEL.starting).toBe("Starting");
    expect(deriveSessionState(inst("a", { setup: { state: "running" } } as Partial<Instance>)).label).toBe("Starting");
    expect(deriveSessionState(inst("a", { setup: { state: "failed" } } as Partial<Instance>)).label).toBe("Setup failed");
    expect(has(fn("lib/sessionState.ts", "deriveSessionState"), 'if (inst.setup && inst.setup.state === "running")')).toBe(
      true
    );
  });

  it("the check chip and its actions", () => {
    expect(checkChip(inst("a"))).toBeNull();
    expect(checkChip(inst("a", { check: { state: "failed", rc: 2 } } as Partial<Instance>))?.label).toBe("Checks ✗");
    const row = fn(ROW, "SidebarRow");
    expect(has(row, "const check = checkChip(inst);")).toBe(true);
    // The two actions live in the session menu's model (row ⋯ and pane ⋯).
    const menu = normCode("lib/sessionMenu.ts");
    expect(has(menu, 'instApi(title, "/setup/rerun", { method: "POST" })')).toBe(true);
    expect(menu).toContain('label: "Re-run worktree setup"');
    expect(has(menu, 'instApi(title, "/check", { method: "POST" })')).toBe(true);
    expect(menu).toContain('label: "Run checks now"');
    const toasts = normCode("components/EventToasts.tsx");
    expect(has(toasts, 'ev.subscribe("session.setup_finished"')).toBe(true);
    expect(has(toasts, 'ev.subscribe("session.check_finished"')).toBe(true);
    const bell = fn(BELL, "notifFromEvent");
    expect(bell).toContain('case "session.setup_finished":');
    expect(bell).toContain('case "session.check_finished":');
  });

  it("the push gate override", () => {
    const actions = normCode("lib/sessionActions.ts");
    expect(has(actions, '(err as Error).message === "checks haven\'t passed for this commit"')).toBe(true);
    expect(has(actions, "pushSession(title, true)")).toBe(true);
  });
});

describe("O4: preview link", () => {
  it("opens the session's base port", () => {
    const menu = normCode("lib/sessionMenu.ts");
    expect(menu).toContain('label: "Open preview"');
    expect(has(menu, 'keys: ":" + inst.ports!.base')).toBe(true);
    expect(has(menu, "window.open(`http://${location.hostname}:${inst.ports!.base}/`")).toBe(true);
  });
});
