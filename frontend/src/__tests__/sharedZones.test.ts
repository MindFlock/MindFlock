/** Shared zones and shared claims in the UI.
 *
 * - The Zones dialog's "Shared with the repo (committed)" section: each shared
 *   zone with who added it, the empty / no-session / unusable-file states, and
 *   the session picker when several worktrees could hold the file.
 * - The local↔shared match ("in repo") and the author line.
 * - The Map's wording for a shared zone.
 * - Intake: a 409 naming a teammate is read as a claim (and nothing else is).
 * - A person's own commit into a red zone: a warning card with "Commit
 *   anyway", never a block.
 *
 * The vitest environment is node: views go through react-dom/server. */

import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it, vi } from "vitest";
import type { RedZone } from "../api/types";

const errorPop = vi.fn();
vi.mock("../lib/errorPop", () => ({
  errorPop: (...a: unknown[]) => errorPop(...a),
  confirmPop: vi.fn(),
}));

const { SharedZonesView, inShared, sharedAuthorLine, sharedKey, SHARED_FILE } = await import(
  "../components/dialogs/RedZonesDialog"
);
const { isShared, sharedWhere } = await import("../components/grid/codemap/sharedZone");
const { claimedBy } = await import("../components/intake/TicketsTab");
const { warnZones } = await import("../components/dialogs/CommitDialog");
const { ApiError } = await import("../api/client");

const SHARED: RedZone[] = [
  {
    id: "sz_1",
    pattern: "config/",
    name: "",
    note: "prod settings",
    kind: "red",
    scope: "shared",
    author: "Ethan",
    added: "2026-10-10",
  },
  { id: "sz_2", pattern: "/src/api", name: "API", kind: "green", scope: "shared" },
];

function view(over: Partial<Parameters<typeof SharedZonesView>[0]> = {}) {
  return renderToStaticMarkup(
    createElement(SharedZonesView, {
      zones: SHARED,
      error: "",
      target: "sess-a",
      sessions: ["sess-a"],
      sessionName: (t: string) => "Session " + t,
      busy: "",
      onTarget: () => {},
      onRemove: () => {},
      ...over,
    })
  );
}

describe("the Zones dialog's shared section", () => {
  it("lists the committed zones with who added them", () => {
    const html = view();
    expect(html).toContain("Shared with the repo (committed)");
    expect(html).toContain("config/");
    expect(html).toContain("added by Ethan · 2026-10-10");
    expect(html).toContain("prod settings");
    expect(html).toContain("/src/api");
    expect(html).toContain("only here");
    expect(html).toContain("in Session sess-a&#x27;s worktree");
    expect(html).toContain('aria-label="Remove shared zone config/"');
  });

  it("says where to add one when the file has none", () => {
    const html = view({ zones: [] });
    expect(html).toContain("None yet.");
    expect(html).toContain(SHARED_FILE);
  });

  it("needs a session — the file lives in a worktree", () => {
    const html = view({ target: "", sessions: [] });
    expect(html).toContain("Open a session on this repo");
    expect(html).not.toContain("config/");
  });

  it("says a broken file is being ignored", () => {
    const html = view({ error: "not valid TOML: line 1" });
    expect(html).toContain("is being ignored: not valid TOML: line 1");
    expect(html).not.toContain("prod settings");
  });

  it("offers the worktree to read when several sessions could hold it", () => {
    const html = view({ sessions: ["sess-a", "sess-b"] });
    expect(html).toContain("<select");
    expect(html).toContain("Session sess-b");
  });
});

describe("matching local and shared zones", () => {
  it("ignores a trailing slash and keeps red and green apart", () => {
    expect(sharedKey({ kind: "red", pattern: "config/" })).toBe(sharedKey({ kind: "red", pattern: "config" }));
    expect(inShared({ kind: "red", pattern: "config" }, SHARED)).toBe(true);
    expect(inShared({ kind: "green", pattern: "config/" }, SHARED)).toBe(false);
    expect(inShared({ kind: undefined, pattern: "config/" }, SHARED)).toBe(true); // red by default
  });

  it("words the author line", () => {
    expect(sharedAuthorLine({ author: "Ethan", added: "2026-10-10" })).toBe("added by Ethan · 2026-10-10");
    expect(sharedAuthorLine({ author: "Ethan" })).toBe("added by Ethan");
    expect(sharedAuthorLine({ added: "2026-10-10" })).toBe("added 2026-10-10");
    expect(sharedAuthorLine({})).toBe("");
  });
});

describe("the Map's shared zones", () => {
  it("names the repo and the author", () => {
    expect(isShared(SHARED[0])).toBe(true);
    expect(isShared({ scope: "repo" })).toBe(false);
    expect(sharedWhere(SHARED[0])).toBe("shared with the repo · added by Ethan");
    expect(sharedWhere({})).toBe("shared with the repo");
  });
});

describe("Intake: a teammate is on it", () => {
  it("reads the 409's claimed_by and nothing else", () => {
    const claim = { kind: "tracker", name: "Bob", why: "label" };
    expect(claimedBy(new ApiError(409, "sc-1: Bob is on this", { claimed_by: claim }))).toEqual(claim);
    expect(claimedBy(new ApiError(409, "already exists", { title: "sc-1" }))).toBeNull();
    expect(claimedBy(new ApiError(500, "x", { claimed_by: claim }))).toBeNull();
    expect(claimedBy(new Error("x"))).toBeNull();
  });
});

describe("a person's own commit into a zone", () => {
  it("warns with Commit anyway instead of blocking", () => {
    errorPop.mockReset();
    const proceed = vi.fn();
    warnZones("me", [{ path: "config/a.toml", pattern: "config/", kind: "red" }], proceed);
    const [title, detail, actions] = errorPop.mock.calls[0] as [string, string, { label: string; run: () => void }[]];
    expect(title).toBe("Commit — red zone changed");
    expect(detail).toContain("config/a.toml (config/)");
    const anyway = actions.find((a) => a.label === "Commit anyway")!;
    anyway.run();
    expect(proceed).toHaveBeenCalledOnce();
  });

  it("names a green scope breach as outside the scope", () => {
    errorPop.mockReset();
    warnZones("me", [{ path: "docs/x.md", pattern: "outside green", kind: "green" }], () => {});
    expect(errorPop.mock.calls[0][0]).toBe("Commit — changes outside the green zone");
  });
});
