/** UI sweep round 2, package Q1: the git-flow and work dialogs.
 *
 * Commit keeps your message and fails on a card; the assistant's instructions
 * can't be wiped by a failed load; Ask and Verify keep focus in the box while
 * sending; Ctrl+Enter saves the template / Schedule editor; a removed keep-out
 * zone has Undo; a failed Templates load has Retry; Verify's messages use the
 * session's own name. */

import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { code, norm, normCode, tagsWithId } from "./sourceText";

const api = vi.fn();
const toast = vi.fn();
const errorPop = vi.fn();
const opened: Array<[string, string | null | undefined]> = [];
vi.mock("../api/client", async (orig) => ({ ...((await orig()) as object), api: (...a: unknown[]) => api(...a) }));
vi.mock("../lib/toast", () => ({ toast: (...a: unknown[]) => toast(...a) }));
vi.mock("../lib/errorPop", () => ({ errorPop: (...a: unknown[]) => errorPop(...a) }));
vi.mock("../lib/sessionActions", async (orig) => ({
  ...((await orig()) as object),
  commitSession: (t: string) => opened.push(["commit", t]),
}));
vi.mock("../state/queries", async (orig) => ({
  ...((await orig()) as object),
  refreshInstances: () => Promise.resolve(),
  refreshTestPlans: () => Promise.resolve(),
}));

const { useUi } = await import("../state/store");
const Commit = await import("../components/dialogs/CommitDialog");
const { assistantSaveStatus } = await import("../components/dialogs/AssistantAgentDialog");
const { isSaveKey } = await import("../components/dialogs/TemplateEditor");
const { removedZoneLine } = await import("../components/dialogs/RedZonesDialog");
const VA = await import("../lib/verifyActions");
const { TemplatesDialogView, TEMPLATES_UI_INIT } = await import("../components/dialogs/TemplatesDialog");
const { formFromTemplate } = await import("../components/dialogs/TemplateEditor");

beforeEach(() => {
  api.mockReset();
  toast.mockReset();
  errorPop.mockReset();
  opened.length = 0;
  useUi.setState({ aliases: {} });
});

describe("#2 Commit keeps your message", () => {
  it("a failed commit is a card that stays, with Edit message reopening Commit", () => {
    Commit.commitFailed("s1", new Error("pre-commit hook failed"));
    expect(toast).not.toHaveBeenCalled();
    expect(errorPop).toHaveBeenCalledTimes(1);
    const [title, detail, actions] = errorPop.mock.calls[0];
    expect(title).toBe("Commit failed");
    expect(detail).toBe("pre-commit hook failed");
    expect(actions.map((a: { label: string }) => a.label)).toEqual(["Edit message"]);
    actions[0].run();
    expect(opened).toEqual([["commit", "s1"]]);
  });

  it("both failure paths (plain and the zone warning's Commit anyway) use the card", () => {
    const body = normCode("components/dialogs/CommitDialog.tsx");
    expect(body).toContain("(e) => commitFailed(title, e)");
    expect(body).toContain("commitFailed(title, err);");
    expect(body).not.toContain('toast("Commit failed');
  });

  it("Escape, the backdrop and Cancel keep the box's text for that session", () => {
    const src = code("components/dialogs/CommitDialog.tsx");
    expect(norm(src)).toContain('<Modal id="commit-dialog" onClose={dismiss}>');
    expect(tagsWithId(src, "commit-cancel")[0].text).toContain("onClick={dismiss}");
    expect(norm(src)).toContain("if (target) rememberCommitMessage(target, msg);");
    Commit.rememberCommitMessage("s2", "Half a message");
    expect(Commit.rememberedCommitMessage("s2")).toBe("Half a message");
    // An emptied box forgets, so the worktree's pending message can fill it.
    Commit.rememberCommitMessage("s2", "  ");
    expect(Commit.rememberedCommitMessage("s2")).toBe("");
  });
});

describe("#1 Assistant instructions", () => {
  it("a failed restart is reported, not covered by 'restarted'", () => {
    expect(assistantSaveStatus(true, "HTTP 500")).toBe("Saved, but the assistant didn't restart: HTTP 500");
    expect(assistantSaveStatus(true, null)).toContain("the assistant restarted");
    expect(assistantSaveStatus(false, null)).toContain("next time the assistant starts");
  });

  it("a failed load says so with Retry, and Save stays off until a load succeeds", () => {
    const src = code("components/dialogs/AssistantAgentDialog.tsx");
    const body = norm(src);
    expect(src).toContain("Couldn't load the instructions.");
    expect(body).toContain('onClick={() => void loadInstructions()}> Retry');
    expect(body).toContain('const canSave = load === "ok" && !saving;');
    for (const id of ["assistant-agent-restart", "assistant-agent-save"])
      expect(tagsWithId(src, id)[0].text).toContain("disabled={!canSave}");
    // A failed load never blanks the box.
    expect(body).not.toMatch(/catch \{ setText\(""\)/);
  });

  it("a slow load never overwrites typed text, and the backdrop keeps unsaved edits", () => {
    const body = normCode("components/dialogs/AssistantAgentDialog.tsx");
    expect(body).toContain("if (!typed.current) setText(file);");
    expect(body).toContain("typed.current = true;");
    expect(body).toContain("closeOnBackdrop={!dirty}");
    expect(body).toContain("if (!canSave) return;");
  });
});

describe("#24 A failed send keeps focus in the box", () => {
  it("Ask: readOnly while sending, a guard in send, refocus on failure", () => {
    const src = code("components/dialogs/AskDialog.tsx");
    const box = tagsWithId(src, "ask-answer")[0].text;
    expect(box).toContain("readOnly={sending}");
    expect(box).not.toContain("disabled={sending}");
    const body = norm(src);
    expect(body).toContain("if (sendingRef.current) return");
    expect(body).toContain("field.current?.focus(); return { ok: false, line: f.line };");
  });

  it("Verify's start form: readOnly while starting, refocus on failure, and says Starting…", () => {
    const body = normCode("components/behaviors/VerifyStartForm.tsx");
    expect(body).toContain("readOnly={busy}");
    expect(body).toContain("if (busyRef.current) return;");
    expect(body).toContain("if (!ok) field.current?.focus();");
    expect(body).toContain('{busy ? "Starting…" : "Start"}');
  });
});

describe("#25 Ctrl+Enter saves the template and Schedule editors", () => {
  it("is Ctrl or Cmd with Enter, never plain Enter or mid-composition", () => {
    expect(isSaveKey({ key: "Enter", ctrlKey: true, metaKey: false })).toBe(true);
    expect(isSaveKey({ key: "Enter", ctrlKey: false, metaKey: true })).toBe(true);
    expect(isSaveKey({ key: "Enter", ctrlKey: false, metaKey: false })).toBe(false);
    expect(isSaveKey({ key: "s", ctrlKey: true, metaKey: false })).toBe(false);
    expect(isSaveKey({ key: "Enter", ctrlKey: true, metaKey: false, isComposing: true })).toBe(false);
  });

  it("both forms listen", () => {
    const body = normCode("components/dialogs/TemplateEditor.tsx");
    expect(body.match(/onKeyDown=\{saveOnKey\(p\)\}/g)?.length).toBe(2);
    expect(body).toContain("if (!p.busy) p.onSave();");
  });
});

describe("#26 Removing a keep-out zone has Undo", () => {
  it("names the zone like its row does", () => {
    expect(removedZoneLine({ pattern: "infra/**", name: "" })).toBe("Removed keep-out zone infra/**");
    expect(removedZoneLine({ pattern: "infra/**", name: "Infra" })).toBe("Removed keep-out zone Infra");
  });

  it("the × toasts after the remove took, and Undo adds the same zone back", () => {
    const body = normCode("components/dialogs/RedZonesDialog.tsx");
    expect(body).toContain("void removed(z, call(\"del:\" + z.id");
    expect(body).toContain("if (!(await took)) return;");
    expect(body).toContain(
      'label: "Undo", onClick: () => void add({ pattern: z.pattern, name: z.name || "", note: z.note || "" })'
    );
    expect(body).toContain("name: undo ? undo.name : name.trim(), note: undo ? undo.note : \"\"");
  });
});

describe("#27 Templates' failed load has Retry", () => {
  const view = (over: Record<string, unknown>) =>
    renderToStaticMarkup(
      createElement(TemplatesDialogView, {
        templates: [],
        ui: TEMPLATES_UI_INIT,
        form: formFromTemplate(null),
        busy: false,
        loaded: false,
        onForm: () => {},
        dispatch: () => {},
        onSave: () => {},
        onDelete: () => {},
        onClose: () => {},
        ...over,
      } as Parameters<typeof TemplatesDialogView>[0])
    );

  it("says Couldn't load templates. with the reason and a Retry", () => {
    const html = view({ loadError: "HTTP 500", onRetryLoad: () => {} });
    expect(html).toContain("Couldn&#x27;t load templates. HTTP 500");
    expect(html).toContain('id="templates-retry"');
    expect(html).toContain(">Retry<");
  });

  it("the dialog wires Retry to the same list load it runs on open", () => {
    const body = normCode("components/dialogs/TemplatesDialog.tsx");
    expect(body).toContain("onRetryLoad={loadList}");
    expect(body).toContain("loadList(); return () =>");
  });
});

describe("#28 Verify's messages use the session's name", () => {
  it("the started line", () => {
    useUi.setState({ aliases: { "sc-1": "Login fix" } });
    expect(VA.verifyStartedMessage("sc-1", {})).toBe(
      "Verifying Login fix — its agent writes its steps and works them in its terminal"
    );
    expect(VA.verifyStartedMessage("sc-1", { queued: true, session: "sc-1" })).toBe(
      "Verify starts when Login fix's current turn ends"
    );
  });

  it("the refusal card", async () => {
    useUi.setState({ aliases: { "sc-1": "Login fix" } });
    api.mockRejectedValueOnce(new Error("no worktree"));
    expect(await VA.startSessionVerify("sc-1", { open: false })).toBe(false);
    expect(errorPop).toHaveBeenCalledWith("Couldn't start verifying Login fix", "no worktree");
  });
});
