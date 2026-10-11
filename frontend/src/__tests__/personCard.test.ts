/** The "You" card (components/settings/screens/PersonCard.tsx), rendered from
 * payloads shaped like GET /api/person.
 *
 * What is pinned: the name and git identity show as editable fields, the id
 * shows small with a Copy button (never as an input), Save is off until the
 * draft differs and is valid, the patch carries only what changed (trimmed,
 * "" clears), and a half-set git identity is refused before it reaches the
 * server. The vitest environment is node, so views go through
 * react-dom/server. */

import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it, vi } from "vitest";

vi.mock("../api/client", () => ({ api: vi.fn() }));
vi.mock("../lib/toast", () => ({ toast: vi.fn() }));
vi.mock("../lib/clipboard", () => ({ copyText: vi.fn(async () => true) }));

const { PersonCardView, draftOf, draftProblem, patchOf } = await import(
  "../components/settings/screens/PersonCard"
);
type Person = import("../api/types").Person;
type Draft = import("../components/settings/screens/PersonCard").PersonDraft;

const ID = "00000000-0000-4000-8000-00000000000a";

function person(extra: Partial<Person> = {}): Person {
  return { id: ID, name: "Ada", git_name: "Ada L", git_email: "ada@example.com", ...extra };
}

const render = (p: Person | null, draft?: Draft, error?: string) =>
  renderToStaticMarkup(
    createElement(PersonCardView, { person: p, draft: draft || draftOf(p), error })
  );

describe("PersonCardView", () => {
  it("shows a loading line, then the error if the load failed", () => {
    expect(render(null)).toContain("Loading");
    expect(render(null, undefined, "boom")).toContain("boom");
  });

  it("shows the editable fields and the id small with Copy", () => {
    const html = render(person());
    expect(html).toContain('value="Ada"');
    expect(html).toContain('value="Ada L"');
    expect(html).toContain('value="ada@example.com"');
    expect(html).toContain(`<code>${ID}</code>`);
    expect(html).toContain('id="person-copy-id"');
    expect(html).not.toContain(`value="${ID}"`); // the id is never an input
    expect(html).toContain(`data-person="${ID}"`);
  });

  it("disables Save until something changed", () => {
    const p = person();
    expect(render(p)).toMatch(/id="person-save" disabled/);
    const changed = { ...draftOf(p), name: "Ada Lovelace" };
    expect(render(p, changed)).not.toMatch(/id="person-save" disabled/);
  });

  it("refuses a half-set or malformed git identity", () => {
    const p = person({ git_name: "", git_email: "" });
    const half = { ...draftOf(p), git_email: "ada@example.com" };
    expect(draftProblem(half)).toContain("both");
    expect(render(p, half)).toMatch(/id="person-save" disabled/);
    expect(draftProblem({ ...half, git_name: "Ada", git_email: "nope" })).toContain(
      "doesn't look like"
    );
    expect(draftProblem({ name: "", git_name: "", git_email: "" })).toBe("");
  });
});

describe("patchOf", () => {
  it("carries only the changed fields, trimmed, with blank as a clear", () => {
    const p = person();
    expect(patchOf(p, draftOf(p))).toEqual({});
    expect(patchOf(p, { name: " Ada Lovelace ", git_name: "Ada L", git_email: "" })).toEqual({
      name: "Ada Lovelace",
      git_email: "",
    });
  });
});
