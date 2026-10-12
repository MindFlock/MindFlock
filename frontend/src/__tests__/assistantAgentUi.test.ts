/** The assistant's agent-file editor: the sidebar button that opens it, the
 * dialog and its text box.
 *
 * Moved from tests/unit/test_assistant_agent_file.py, which grepped the built
 * bundle. The two endpoints the dialog calls (/api/assistant/instructions and
 * /api/assistant/restart) are a backend contract and stay pinned in Python. */

import { describe, expect, it } from "vitest";
import { src, tagsWith, tagsWithId } from "./sourceText";

const DIALOG = src("components/dialogs/AssistantAgentDialog.tsx");

describe("the assistant agent file UI", () => {
  it("has a sidebar button that opens the editor dialog", () => {
    const btn = tagsWithId(src("components/sidebar/SidebarBars.tsx"), "assistant-agent-btn");
    expect(btn.map((t) => t.name)).toEqual(["button"]);
    expect(btn[0].text).toContain('openDialogFor("assistant-agent")');
  });

  it("is a modal with a textarea for the instructions", () => {
    expect(tagsWith(DIALOG, "id", "assistant-agent-dialog").map((t) => t.name)).toEqual(["Modal"]);
    expect(tagsWithId(DIALOG, "assistant-agent-text").map((t) => t.name)).toEqual(["textarea"]);
  });
});
