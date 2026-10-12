/** MindFlock MCP, frontend wiring: the rail row's lineage sub-line, the
 * `session.message` toast + bell curation, and the Settings → Agent
 * orchestration attach switch + scope select bound to `general.agent_mcp` /
 * `general.agent_mcp_scope`.
 *
 * Moved from tests/unit/test_frontend_agent_mcp.py, which grepped the built
 * app.js / style.css. The wording itself is unit-tested in
 * agentMessages.test.ts; the rendering was checked with the screenshot
 * harness. Here the pure pieces are called, and the wiring is pinned in the
 * source module that owns it (sourceText.ts: comments stripped, layout-blind). */

import { describe, expect, it } from "vitest";
import { lineageMark, messageNotif, messageToastText } from "../lib/agentMessages";
import { AGENT_MCP_SCOPE_OPTIONS } from "../components/settings/screens/AgentOrchestration";
import { SRC, normCode, fn, has } from "./sourceText";

const id = (t: string) => t;
const ROW = "components/sidebar/SidebarRow.tsx";

describe("the rail row renders the lineage sub-line", () => {
  it("marks a child with its parent and an orphaned spawn as the agent's", () => {
    expect(lineageMark("orch", true, id)?.text).toBe("↳ orch");
    expect(lineageMark("orch", false, id)?.spawned).toBe(false);
    expect(lineageMark("", true, id)).toMatchObject({ text: "↳ agent", spawned: true });
    expect(lineageMark("", false, id)).toBeNull();
  });

  it("SidebarRow wires the mark into the meta column", () => {
    const row = fn(ROW, "SidebarRow");
    expect(has(row, "lineageMark(inst.parent, inst.spawned, nameOf)")).toBe(true);
    expect(has(row, 'className={"lineage" + (lineage.spawned ? " spawned" : "")}')).toBe(true);
    expect(has(row, '<span className={"meta" + (subline ? " has-lineage" : "")}>')).toBe(true);
    const css = normCode("components/sidebar/SidebarRow.css");
    expect(has(css, ".inst .meta.has-lineage { flex-direction: column;")).toBe(true);
    expect(css).toContain(".inst .lineage.spawned");
  });
});

describe("session.message: toasts, and only results reach the bell", () => {
  it("toasts every message and phrases a worker's report", () => {
    expect(messageToastText("w1", { from: "orch", text: "rebase first" }, id)).toMatch(/^✉ orch → w1/);
    expect(messageToastText("orch", { from: "w1", kind: "result", text: "done" }, id)).toContain(" reported");
    const toasts = normCode("components/EventToasts.tsx");
    expect(has(toasts, 'ev.subscribe("session.message", (env) => {')).toBe(true);
    expect(has(toasts, "messageToastText(env.session, d, displayName)")).toBe(true);
  });

  it("the bell routes through messageNotif, which drops plain messages", () => {
    expect(messageNotif({ from: "orch", text: "hi" }, id)).toBeNull();
    expect(messageNotif({ from: "w1", kind: "result", text: "done" }, id)?.text).toContain("worker w1 reported");
    expect(
      has(fn("components/NotificationsBell.tsx", "notifFromEvent"), 'case "session.message": return messageNotif(')
    ).toBe(true);
  });

  it("respects replay", () => {
    const src = SRC["components/EventToasts.tsx"];
    let block = src.slice(src.indexOf('ev.subscribe("session.message"'));
    block = block.slice(0, block.indexOf("unsubs.push("));
    expect(block).toContain("isReplay(env)");
    expect(block).toContain("notifyOnce("); // throttled, never a bare toast()
  });
});

describe("Settings: the attach switch and the scope select", () => {
  it("binds general.agent_mcp and general.agent_mcp_scope", () => {
    const rows = fn("components/settings/screens/AgentOrchestration.tsx", "AgentMcpRows");
    expect(rows).toContain("Give agents the MindFlock MCP (agent-to-agent messaging and orchestration)");
    expect(has(rows, 's.get("general", "agent_mcp")')).toBe(true);
    expect(has(rows, 's.saveField("general", "agent_mcp", e.target.checked)')).toBe(true);
    expect(has(rows, '<SettingField group="general" field="agent_mcp_scope" options={AGENT_MCP_SCOPE_OPTIONS} />')).toBe(
      true
    );
    expect(AGENT_MCP_SCOPE_OPTIONS.map((o) => o.value)).toEqual(expect.arrayContaining(["children", "readonly", "all"]));
    expect(rows).toContain("MINDFLOCK_AGENT_MCP=0");
    expect(rows).toContain("next launch");
  });

  it("never uses native dialogs", () => {
    // Electron implements no window.prompt — and alert/confirm are as bad in
    // a settings row. The whole Agent orchestration screen (the MCP rows and
    // the cap rows).
    const src = SRC["components/settings/screens/AgentOrchestration.tsx"];
    expect(src).toContain("function AgentMcpRows(");
    expect(src).not.toMatch(/\b(prompt|alert|confirm)\(/);
  });
});

it("the row types carry the lineage fields", () => {
  const src = SRC["api/types.ts"];
  let inst = src.slice(src.indexOf("export interface Instance {"));
  inst = inst.slice(0, inst.indexOf("\n}\n"));
  expect(inst).toContain("parent?: string;");
  expect(inst).toContain("spawned?: boolean;");
  expect(src).toContain("agent_mcp?: { enabled: boolean; providers: string[] };");
});
