/** UI sweep round 3, package R2: the pane and its terminal.
 *
 * - #3  a worker waiting its turn (close code 4410) checks again slowly and
 *       its pane says "Waiting its turn" with Start now, instead of dying;
 * - #2  an open history view takes Escape / Ctrl+F only while focus is in its
 *       pane (or nowhere), never from a dialog;
 * - #5  input to a socket that isn't open says it wasn't sent (no "Pasted"
 *       toast, no upload), and a dropped socket is said over the pane —
 *       "Reconnecting…", then "Can't reach the session" with Retry;
 * - #19 "Merge blocked" opens the PR;
 * - #20 an error the server keeps answering with stops after a few tries,
 *       is written once, and the pane offers Retry; the shell gets a cover;
 * - #32 the ⏩ picker and the chat hand the keyboard back;
 * - #33 the peer session viewer rests while the page is hidden.
 *
 * Node env (no DOM): pure helpers, server renders and source text. */

import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it, vi } from "vitest";
import type { Instance, MergeState } from "../api/types";
import { code, css } from "./sourceText";

vi.mock("../api/client", () => ({
  api: vi.fn(() => new Promise(() => {})),
  instApi: vi.fn(() => new Promise(() => {})),
  ApiError: class extends Error {},
}));
vi.mock("../lib/toast", () => ({ toast: vi.fn() }));
vi.mock("../lib/chat", async (orig) => ({ ...(await orig<object>()), openChat: vi.fn() }));

const { toast } = await import("../lib/toast");
const {
  closeAction,
  ERROR_RETRY_LIMIT,
  notSentLine,
  termCover,
  UNREACHABLE_MS,
  WAITING_CLOSE,
  WAITING_RETRY_MS,
} = await import("../lib/terminals");
const { attachFileDrop, pasteClipboard, pasteFilesAsPaths } = await import("../lib/clipboard");
const { nextStep } = await import("../lib/stage");
const { keysAreOurs } = await import("../components/grid/HistoryOverlay");
const { TermCover } = await import("../components/grid/Pane");
const { openChatByHand, takeHandOpen } = await import("../components/chat/ChatPanel");
const { openChat } = await import("../lib/chat");

describe("#3 a worker waiting its turn", () => {
  it("is close code 4410, retried slowly — never 'gone'", () => {
    expect(WAITING_CLOSE).toBe(4410);
    expect(closeAction(4410)).toBe("waiting");
    expect(WAITING_RETRY_MS).toBe(5000);
    // The other codes are unchanged.
    expect(closeAction(4409)).toBe("gone");
    expect(closeAction(4404)).toBe("gone");
    expect(closeAction(4423)).toBe("paused");
    expect(closeAction(4424)).toBe("held");
    expect(closeAction(1006)).toBe("retry");
  });

  it("keeps checking every WAITING_RETRY_MS, without a 'disconnected' flash or an error line", () => {
    const src = code("lib/terminals.ts");
    expect(src).toContain('if (closeAction(code) !== "waiting") handle._setState("disconnected");');
    expect(src).toMatch(/handle\._setState\("waiting"\);\s*reconnectTimer = setTimeout\(\(\) => \{\s*if \(!closed\) handle\.connect\(\);\s*\}, WAITING_RETRY_MS\);\s*return;/);
    // The waiting branch returns before the error line is written.
    expect(src.indexOf('handle._setState("waiting")')).toBeLessThan(src.indexOf('term.write("\\r\\n[error] "'));
  });

  it("is covered with 'Waiting its turn' and Start now, until the socket attaches", () => {
    expect(termCover("waiting", true, 0)).toBe("waiting");
    expect(termCover("waiting", false, 60_000)).toBe("waiting");
    expect(termCover("connected", true, 0)).toBe("none");
    const html = renderToStaticMarkup(createElement(TermCover, { title: "w", kind: "agent", state: "waiting", booted: true }));
    expect(html).toContain("Waiting its turn");
    expect(html).toContain("▶ Start now");
    expect(html).not.toContain("Loading session");
  });

  it("starts it with the rail's Start (revive) and attaches at once", () => {
    const src = code("components/grid/Pane.tsx");
    expect(src).toContain('import { startSession } from "../../lib/revive";');
    expect(src).toMatch(/startSession\(title\)\.then\(\(r\) => \{[\s\S]{0,200}if \(r\) peekTerm\(title, kind\)\?\.retry\(\);/);
  });

  it("is 'connected' only once PTY bytes arrive, not on the open the server always accepts", () => {
    const src = code("lib/terminals.ts");
    expect(src).toMatch(/sock\.onopen = \(\) => \{\s*downSince = 0;\s*handle\.doFit\(\);\s*\};/);
    expect(src).toMatch(/markAttached\(\);\s*term\.write\(new Uint8Array\(ev\.data\)\);/);
  });
});

describe("#2 the history view's keys", () => {
  type El = { tagName: string; parent?: El; closest(sel: string): El | null; contains(n: unknown): boolean; parentElement: El | null };
  const el = (tagName: string, parent?: El, cls = ""): El => {
    const self: El = {
      tagName,
      parent,
      parentElement: parent || null,
      closest(sel: string) {
        let at: El | undefined = self;
        while (at) {
          if (sel === ".pane" && (at as El & { cls?: string }).cls === "pane") return at;
          at = at.parent;
        }
        return null;
      },
      contains(n: unknown) {
        let at = n as El | undefined;
        while (at) {
          if (at === self) return true;
          at = at.parent;
        }
        return false;
      },
    };
    (self as El & { cls?: string }).cls = cls;
    return self;
  };
  const body = el("BODY");
  const pane = el("DIV", body, "pane");
  const overlay = el("DIV", el("DIV", pane));
  const termInPane = el("TEXTAREA", el("DIV", pane));
  const dialogInput = el("INPUT", el("DIV", body));
  const otherPane = el("TEXTAREA", el("DIV", body, "pane"));
  const E = (x: El) => x as unknown as Element;

  it("are taken while focus is in its own pane, or on the body", () => {
    expect(keysAreOurs(E(termInPane), E(overlay))).toBe(true);
    expect(keysAreOurs(E(body), E(overlay))).toBe(true);
    expect(keysAreOurs(null, E(overlay))).toBe(true);
  });

  it("are left alone while a dialog or another pane has focus", () => {
    expect(keysAreOurs(E(dialogInput), E(overlay))).toBe(false);
    expect(keysAreOurs(E(otherPane), E(overlay))).toBe(false);
  });

  it("guard the overlay's document listener first thing", () => {
    const src = code("components/grid/HistoryOverlay.tsx");
    expect(src).toMatch(/const onKey = \(e: KeyboardEvent\) => \{\s*if \(!keysAreOurs\(document\.activeElement, scrollRef\.current\)\) return;\s*if \(e\.key === "Escape"\)/);
  });
});

describe("#5 a disconnected pane", () => {
  it("says input wasn't sent, by why", () => {
    expect(notSentLine("disconnected")).toBe("Reconnecting — that wasn't sent");
    expect(notSentLine("error")).toBe("Reconnecting — that wasn't sent");
    expect(notSentLine("waiting")).toBe("Waiting its turn — that wasn't sent");
    expect(notSentLine("failed")).toBe("Not connected — that wasn't sent");
  });

  it("routes typing, pastes and drops through the socket check", () => {
    const src = code("lib/terminals.ts");
    expect(src).toContain("if (canSend()) ws!.send(data);");
    expect(src).toContain("showTermNotice(container, notSentLine(state));");
    expect(src).toContain("attachFileDrop(host, term, session, opts.canSend);");
    expect(src).toContain("pasteClipboard(term, session, opts.canSend);");
  });

  it("pastes, uploads and toasts nothing while the socket is closed", async () => {
    const paste = vi.fn();
    const term = { paste } as unknown as Parameters<typeof pasteClipboard>[0];
    vi.mocked(toast).mockClear();
    await pasteClipboard(term, "s", () => false);
    await pasteFilesAsPaths([new File(["x"], "a.png", { type: "image/png" })], term, "s", () => false);
    // The paste event on a terminal: text in the clipboard.
    const handlers: Record<string, (ev: unknown) => void> = {};
    const host = {
      addEventListener: (t: string, f: (ev: unknown) => void) => (handlers[t] = f),
      removeEventListener: () => {},
      classList: { add() {}, remove() {} },
    } as unknown as HTMLElement;
    attachFileDrop(host, term, "s", () => false);
    handlers.paste({
      clipboardData: { files: [], getData: () => "hello" },
      preventDefault() {},
      stopPropagation() {},
    });
    expect(paste).not.toHaveBeenCalled();
    expect(toast).not.toHaveBeenCalled();
    // Open: the same paste goes through and says so.
    attachFileDrop(host, term, "s", () => true);
    handlers.paste({
      clipboardData: { files: [], getData: () => "hello" },
      preventDefault() {},
      stopPropagation() {},
    });
    expect(paste).toHaveBeenCalledWith("hello");
    expect(toast).toHaveBeenCalledWith("Pasted 5 chars");
  });

  it("covers: loading before bytes, a Reconnecting line after, Can't reach after 10 s", () => {
    expect(UNREACHABLE_MS).toBe(10_000);
    expect(termCover("connecting", false, 0)).toBe("loading");
    expect(termCover("disconnected", false, 3000)).toBe("loading");
    expect(termCover("disconnected", true, 3000)).toBe("reconnecting");
    expect(termCover("error", true, 0)).toBe("reconnecting");
    expect(termCover("disconnected", true, 10_000)).toBe("unreachable");
    expect(termCover("connecting", false, 12_000)).toBe("unreachable");
    // The pane's own placeholders own these.
    expect(termCover("paused", false, 60_000)).toBe("none");
    expect(termCover("held", false, 60_000)).toBe("none");
    expect(termCover("gone", true, 60_000)).toBe("none");
  });

  it("draws the line and the loading cover", () => {
    const r = (state: string, booted: boolean, kind: "agent" | "shell" = "agent") =>
      renderToStaticMarkup(createElement(TermCover, { title: "s", kind, state, booted }));
    expect(r("disconnected", true)).toContain("Reconnecting…");
    expect(r("connecting", false)).toContain("Loading session…");
    expect(r("connected", true)).toBe("");
    expect(css("components/grid/Pane.css")).toContain(".term-reconnecting");
  });

  it("re-renders when the outage crosses UNREACHABLE_MS, and Retry goes now", () => {
    const src = code("components/grid/Pane.tsx");
    expect(src).toContain("Can't reach the session — retrying");
    expect(src).toContain("setTimeout(() => redraw((n) => n + 1), Math.max(0, UNREACHABLE_MS - downFor) + 50)");
    expect(src).toContain("peekTerm(title, kind)?.retry();");
  });
});

describe("#19 Merge blocked", () => {
  const ms = (o: Partial<MergeState> = {}): MergeState => ({
    number: 1, url: "https://x.test/1", state: "dirty",
    mergeable: false, checks: "ok", can_merge: false,
    blockers: ["the branch has merge conflicts with its base"], ...o,
  });
  const at = (o: Partial<Instance>) => nextStep({ title: "t", status: "running", stage: "pr", ...o } as Instance);

  it("opens the PR, like Merge on GitHub ↗", () => {
    const open = vi.fn();
    (globalThis as Record<string, unknown>).window ??= {};
    const w = globalThis.window as unknown as { open: unknown };
    const prev = w.open;
    w.open = open;
    try {
      const s = at({ merge_state: ms(), pr_url: "https://x.test/pr/1" });
      expect(s?.label).toBe("Merge blocked ↗");
      expect(s?.hint).toBe(true);
      expect(s?.disabled).toBeFalsy();
      expect(s?.title).toContain("the branch has merge conflicts with its base");
      expect(s?.title).toContain("Opens the PR to resolve it.");
      s?.run();
      expect(open).toHaveBeenCalledWith("https://x.test/pr/1", "_blank");
    } finally {
      w.open = prev;
    }
  });

  it("stays a disabled explanation with no PR to open", () => {
    const s = at({ merge_state: ms() });
    expect(s?.label).toBe("Merge blocked");
    expect(s?.disabled).toBe(true);
  });
});

describe("#20 errors that repeat", () => {
  const src = code("lib/terminals.ts");

  it("stop after ERROR_RETRY_LIMIT tries and wait for Retry", () => {
    expect(ERROR_RETRY_LIMIT).toBe(5);
    expect(src).toMatch(/if \(errorHere && !attached\) \{\s*errorRuns \+= 1;\s*if \(errorRuns >= ERROR_RETRY_LIMIT\) \{\s*closed = true;\s*handle\._setState\("failed"\);\s*return;/);
    expect(termCover("failed", true, 0)).toBe("failed");
  });

  it("write the same error line only once", () => {
    expect(src).toContain('if (errorHere && errorHere !== lastError)\n          term.write("\\r\\n[error] " + errorHere + "\\r\\n");');
  });

  it("are shown with Retry, and the shell tab has its own cover", () => {
    const html = renderToStaticMarkup(createElement(TermCover, { title: "s", kind: "shell", state: "failed", booted: true }));
    expect(html).toContain("The shell didn&#x27;t start");
    expect(html).toContain("Retry");
    const loading = renderToStaticMarkup(createElement(TermCover, { title: "s", kind: "shell", state: "connecting", booted: false }));
    expect(loading).toContain("Loading shell…");
    const pane = code("components/grid/Pane.tsx");
    expect(pane).toContain('<TermCover title={title} kind="shell" state={shellState} booted={shellBooted} />');
    expect(pane).toContain("handle.onState = (s) => setShellState(s);");
  });
});

describe("#32 focus after the ⏩ picker and the chat", () => {
  it("hands the keyboard back to the terminal when a pick comes from the keyboard", () => {
    const src = code("components/grid/FastTrackMenu.tsx");
    expect(src).toContain("activate(entries[sel], true);");
    expect(src).toContain("activate(entries[i], true);");
    expect(src).toContain("close(byKey);");
  });

  it("puts the cursor in a hand-opened chat once, never in an auto-pop", () => {
    openChatByHand("s1");
    expect(openChat).toHaveBeenCalledWith("s1");
    expect(takeHandOpen("s1")).toBe(true);
    expect(takeHandOpen("s1")).toBe(false);
    expect(takeHandOpen("popped")).toBe(false);
    const src = code("components/chat/ChatPanel.tsx");
    expect(src).toContain("if (takeHandOpen(title)) inputRef.current?.focus();");
    expect(src).toContain("onClick={() => openChatByHand(focused)}");
    expect(src).toContain("onClick={() => openChatByHand(w.title)}");
  });

  it("gives the terminal the keyboard when Escape minimizes the chat", () => {
    const src = code("components/chat/ChatPanel.tsx");
    expect(src).toMatch(/minimizeChat\(title\);\s*setTimeout\(\(\) => focusTerm\(title\), 0\);/);
  });
});

describe("#33 the peer session viewer", () => {
  it("polls their screen only while the page is visible, never two at once", () => {
    const src = code("components/PeerSessionViewer.tsx");
    expect(src).toContain("pollWhileVisible(() => void tick(), SCREEN_POLL_MS)");
    expect(src).not.toContain("setTimeout(() => void tick(), SCREEN_POLL_MS)");
    expect(src).toContain("if (busy) return;");
  });
});
