/** The rail row's task line (components/sidebar/TaskLine): the server's task
 * headline under the session name, the full prompt on hover.
 *
 * Pinned: the headline wins over the raw prompt; an older server (no
 * headline) falls back to the prompt's first line, then the create task's;
 * the tooltip is always the whole prompt that was typed; nothing at all
 * renders no span; and the row really draws it through TaskLine. */

import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";
import { TaskLine, taskLineOf } from "../components/sidebar/TaskLine";
import { normCode } from "./sourceText";

const FULL = "ok so can you please fix the bug where the pin bar draws below the terminal\nit started after the refactor";

describe("taskLineOf", () => {
  it("shows the headline and hovers the whole prompt", () => {
    const line = taskLineOf({
      task_headline: "Fix pin bar drawing below terminal",
      last_prompt: "ok so can you please fix the bug where the pin bar draws below the terminal",
      last_prompt_full: FULL,
      task: "build the sidebar",
    });
    expect(line.text).toBe("Fix pin bar drawing below terminal");
    expect(line.title).toBe(FULL);
  });

  it("falls back to the prompt, then the create task, on an older server", () => {
    expect(taskLineOf({ last_prompt: "rename the flag", task: "x" }).text).toBe("rename the flag");
    expect(taskLineOf({ task: "\n  add a README section\nwith details" }).text).toBe("add a README section");
    expect(taskLineOf({ task: "\n  add a README section\nwith details" }).title).toBe(
      "add a README section\nwith details",
    );
  });

  it("keeps the headline when the newest prompt was only 'continue'", () => {
    // The server kept the last real task; the hover still says what was typed.
    const line = taskLineOf({ task_headline: "Add dark mode toggle", last_prompt: "continue", last_prompt_full: "continue" });
    expect(line.text).toBe("Add dark mode toggle");
    expect(line.title).toBe("continue");
  });

  it("is empty when there is nothing to say", () => {
    expect(taskLineOf({}).text).toBe("");
    expect(taskLineOf({ task_headline: null, last_prompt: null, task: null }).text).toBe("");
    expect(taskLineOf({ task_headline: "  " }).text).toBe("");
  });
});

describe("TaskLine", () => {
  const html = (inst: Parameters<typeof taskLineOf>[0]) => renderToStaticMarkup(createElement(TaskLine, { inst }));

  it("renders the headline in the task-line slot with the prompt as its title", () => {
    const out = html({ task_headline: "Investigate server memory use", last_prompt_full: "why is the server using so much memory" });
    expect(out).toBe(
      '<span class="lineage task-line" title="why is the server using so much memory">Investigate server memory use</span>',
    );
  });

  it("escapes what the user typed", () => {
    const out = html({ task_headline: "Fix <b> tags", last_prompt_full: 'fix the "<b>" tags' });
    expect(out).toContain("Fix &lt;b&gt; tags");
    expect(out).toContain('title="fix the &quot;&lt;b&gt;&quot; tags"');
  });

  it("renders nothing without a task", () => {
    expect(html({})).toBe("");
  });

  it("is what the rail row draws", () => {
    const row = normCode("components/sidebar/SidebarRow.tsx");
    expect(row).toContain("<TaskLine inst={inst} />");
    expect(row).toContain("const task = taskLineOf(inst).text;");
    // The old inline span (prompt text, no headline) is gone.
    expect(row).not.toContain('title={String(inst.last_prompt_full || task)}');
  });
});
