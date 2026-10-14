/** Settings → Agent orchestration → Shared MCP servers
 * (components/settings/screens/SharedMcpServers.tsx), rendered from payloads
 * shaped like GET /api/settings/mcp-shared.
 *
 * What is pinned: one switch per server, checked only for a shared one; an
 * unshareable row (remote, MindFlock's own) is disabled with its reason; a
 * risky one shows its caution; a live shared server says it runs once and for
 * how many sessions; a refused server→client request tells the user to switch
 * it off; the server's kill switch disables every row. The save is bound to
 * `general.mcp_shared_servers`, and the section is mounted on the
 * orchestration screen. The vitest environment is node, so views go through
 * react-dom/server. */

import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it, vi } from "vitest";
import { fn, has, SRC } from "./sourceText";

vi.mock("../api/client", () => ({ api: vi.fn() }));
vi.mock("../lib/toast", () => ({ toast: vi.fn() }));

const { SharedMcpServersView } = await import("../components/settings/screens/SharedMcpServers");
type Info = import("../components/settings/screens/SharedMcpServers").McpSharedInfo;
type Row = import("../components/settings/screens/SharedMcpServers").McpServerRow;

const row = (extra: Partial<Row>): Row => ({
  name: "context7",
  transport: "stdio",
  command: "npx",
  shareable: true,
  caution: "",
  reason: "",
  shared: false,
  ...extra,
});

const info = (servers: Row[], extra: Partial<Info> = {}): Info => ({
  available: true,
  servers,
  shared: [],
  bridge: { running: false, upstreams: [] },
  ...extra,
});

const render = (i: Info | null, shared: string[] = [], failed = false) =>
  renderToStaticMarkup(createElement(SharedMcpServersView, { info: i, failed, shared, onToggle: () => {} }));

const switchOf = (html: string, name: string) => {
  const start = html.indexOf(`data-mcp-server="${name}"`);
  const rest = html.slice(start);
  return rest.slice(rest.indexOf("<input"), rest.indexOf("/>", rest.indexOf("<input")) + 2);
};

describe("SharedMcpServersView", () => {
  it("one switch per server, checked only when shared", () => {
    const html = render(info([row({}), row({ name: "excalidraw" })]), ["context7"]);
    expect(switchOf(html, "context7")).toContain("checked");
    expect(switchOf(html, "excalidraw")).not.toContain("checked");
    expect(html).toContain('aria-label="Share context7 across sessions"');
  });

  it("an unshareable server is disabled and says why", () => {
    const html = render(info([row({ name: "remote", shareable: false, reason: "Already a remote server." })]));
    expect(switchOf(html, "remote")).toContain("disabled");
    expect(html).toContain("Already a remote server.");
  });

  it("shows the trade-off for a risky server", () => {
    const html = render(info([row({ name: "gitnexus", caution: "Works on the folder it starts in." })]));
    expect(html).toContain('class="mcp-share-caution"');
    expect(html).toContain("Works on the folder it starts in.");
  });

  it("a running shared server says it runs once, for how many sessions", () => {
    const i = info([row({})], {
      bridge: { running: true, upstreams: [{ name: "context7", pid: 42, running: true, sessions: 3 }] },
    });
    expect(render(i, ["context7"])).toContain("Running once (pid 42) for 3 sessions.");
  });

  it("a refused server→client request tells the user to switch it off", () => {
    const i = info([row({})], {
      bridge: {
        running: true,
        upstreams: [
          { name: "context7", pid: 1, running: true, sessions: 1, refused_requests: { "sampling/createMessage": 2 } },
        ],
      },
    });
    expect(render(i, ["context7"])).toContain("sampling/createMessage");
  });

  it("the server's kill switch disables every row", () => {
    const html = render(info([row({})], { available: false }), ["context7"]);
    expect(html).toContain("MINDFLOCK_MCP_SHARE=0");
    expect(switchOf(html, "context7")).toContain("disabled");
  });

  it("loading, failure and an empty config", () => {
    expect(render(null)).toContain("Reading your MCP servers");
    expect(render(null, [], true)).toContain("Couldn&#x27;t read your MCP servers");
    expect(render(info([]))).toContain("No MCP servers in your Claude config.");
  });
});

describe("wiring", () => {
  it("saves general.mcp_shared_servers and lives on the orchestration screen", () => {
    const main = fn("components/settings/screens/SharedMcpServers.tsx", "SharedMcpServers");
    expect(has(main, 's.saveField("general", "mcp_shared_servers", next)')).toBe(true);
    expect(has(main, 'api<McpSharedInfo>("/api/settings/mcp-shared")')).toBe(true);
    expect(SRC["components/settings/screens/AgentOrchestration.tsx"]).toContain("<SharedMcpServers />");
  });
});
