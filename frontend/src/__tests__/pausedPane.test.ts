/** A paused session's pane (backend: agent_sessions.PAUSED_REFUSED).
 *
 * Pause stops the agent and keeps the worktree. The pane used to mount the
 * agent terminal anyway, and its socket's reconnect relaunched the agent the
 * server had just stopped — within a second, while the row still read Paused.
 * The server now refuses that socket with close code 4423; the pane shows a
 * placeholder with ▶ Resume instead of a terminal, and neither terminal
 * implementation reconnects on 4423. */

import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it, vi } from "vitest";
import { code } from "./sourceText";

vi.mock("../lib/sessionActions", () => ({ resumeSession: vi.fn(() => Promise.resolve()) }));

const { closeAction } = await import("../lib/terminals");
const { PAUSED_CLOSE, retryAfter } = await import("../lib/wsTerm");
const { PausedBody, PAUSED_TEXT } = await import("../components/grid/PausedBody");

describe("the paused close code", () => {
  it("is 4423 and never reconnects the grid's terminals", () => {
    expect(PAUSED_CLOSE).toBe(4423);
    expect(closeAction(4423)).toBe("paused");
    // The existing final codes are unchanged…
    expect(closeAction(4404)).toBe("gone");
    expect(closeAction(4409)).toBe("gone");
    // …and anything else (a server restart, the agent rebooting) still retries.
    expect(closeAction(1006)).toBe("retry");
    expect(closeAction(1000)).toBe("retry");
    expect(closeAction(undefined)).toBe("retry");
  });

  it("is final for the shared ws terminal too, even with reconnect on", () => {
    expect(retryAfter(4423, 0, true)).toBe(null);
    expect(retryAfter(4423, 5, true)).toBe(null);
  });

  it("stops the reconnect loop in the terminal's close handler", () => {
    const src = code("lib/terminals.ts");
    expect(src).toContain('const action = closeAction(ev?.code);');
    expect(src).toContain('if (action !== "retry") {');
    // A terminal refused while paused is rebuilt (not revived) after Resume.
    expect(src).toContain('if (h && h.state === "paused") {');
  });
});

describe("PausedBody", () => {
  it("says what paused means and offers Resume, with no terminal", () => {
    const html = renderToStaticMarkup(createElement(PausedBody, { title: "s", hidden: false }));
    expect(html).toContain("Paused");
    expect(html).toContain(PAUSED_TEXT);
    expect(PAUSED_TEXT).toContain("worktree and conversation are kept");
    expect(html).toContain("▶ Resume");
    expect(html).not.toContain("term-container");
    expect(html).not.toContain(" hidden");
  });

  it("hides with the Agent tab like the terminal it replaces", () => {
    const html = renderToStaticMarkup(createElement(PausedBody, { title: "s", hidden: true }));
    expect(html).toContain("pane-term agent-term pane-paused hidden");
  });

  it("asks nothing through window dialogs (Electron has none)", () => {
    const src = code("components/grid/PausedBody.tsx");
    expect(src).not.toMatch(/\b(prompt|alert|confirm)\(/);
    expect(src).toContain("resumeSession(title)");
  });
});

describe("Pane", () => {
  const src = code("components/grid/Pane.tsx");

  it("renders the placeholder instead of mounting the agent terminal when paused", () => {
    expect(src).toContain('const paused = inst.status === "paused";');
    expect(src).toContain('<PausedBody key="paused" title={title} hidden={tab !== "agent"} />');
    // The agent socket is opened only by adopt("agent"), which a paused pane
    // never calls; the shell's adopt is untouched.
    expect(src).toContain('if (!el || missing || loading || (kind === "agent" && paused)) return;');
    expect(src).toContain('ref={adopt("shell")}');
  });

  it("drops an open agent terminal when the session is paused under it", () => {
    expect(src).toContain('releaseTerm(title, "agent");');
  });
});
