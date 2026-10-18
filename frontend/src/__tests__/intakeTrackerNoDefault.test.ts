/** "+ Add source" never picks a tracker for you. A new user (v0.8.6) opened
 * it to a card already titled "GitHub Issues · claude (default)" over a
 * Provider select set to GitHub Issues — `add()` drafted `catalog[0]` — and
 * read it as "I am configuring GitHub Issues" before choosing anything. Now:
 * nothing is picked, Continue is disabled until a tile is, and no title or
 * hint names a tracker until one is chosen. GitHub Issues the TRACKER and the
 * Repository tile say, under their names, which is which. */

/// <reference types="vite/client" />
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { QueryClientProvider } from "@tanstack/react-query";
import { describe, expect, it, vi } from "vitest";
import { code, has } from "./sourceText";

vi.mock("react", async (orig) => {
  const R = (await orig()) as typeof import("react");
  return {
    ...R,
    useSyncExternalStore: <T,>(sub: (cb: () => void) => () => void, get: () => T) =>
      R.useSyncExternalStore(sub, get, get),
  };
});

const { queryClient } = await import("../state/queries");
const { TicketsTab } = await import("../components/intake/TicketsTab");
const { AddFlowShell, REPO_KIND, trackerKinds } = await import("../components/intake/kit");

const CATALOG = [
  { id: "github_issues", label: "GitHub Issues", blurb: "", fields: [] },
  { id: "shortcut", label: "Shortcut", blurb: "", fields: [] },
  { id: "jira", label: "Jira", blurb: "", fields: [] },
  { id: "linear", label: "Linear", blurb: "", fields: [] },
  { id: "asana", label: "Asana", blurb: "", fields: [] },
];
const TICKETS = code("components/intake/TicketsTab.tsx");

/** Intake → Tickets for someone with no ticket source yet: the add flow leads. */
function freshTicketsTab(): string {
  queryClient.setQueryData(["ticketing-catalog"], CATALOG);
  queryClient.setQueryData(["ticketing-sources"], []);
  return renderToStaticMarkup(
    createElement(
      QueryClientProvider,
      { client: queryClient },
      createElement(TicketsTab, {
        gotoTab() {},
        focus: "",
        adding: null,
        startAdd() {},
        endAdd() {},
      })
    )
  );
}

/** The add flow's own markup (the section), so the page's other copy — the
 * tab's intro, the switch — can't satisfy or spoil an assertion. */
const addFlowOf = (html: string) => {
  const at = html.indexOf('id="ik-add-flow"');
  expect(at, "the add flow leads for a fresh user").toBeGreaterThan(-1);
  return html.slice(html.lastIndexOf("<section", at), html.indexOf("</section>", at));
};

describe("a fresh user's add flow picks nothing", () => {
  const flow = addFlowOf(freshTicketsTab());

  it("offers every tracker and Repository, with none of them picked", () => {
    for (const p of CATALOG) expect(flow).toContain('data-kind="' + p.id + '"');
    expect(flow).toContain('data-kind="repo"');
    expect(flow).not.toContain('aria-checked="true"');
    expect(flow).not.toMatch(/class="ik-kind picked"/);
  });

  it("Continue is there and disabled, with the one-line Pick your tracker hint", () => {
    expect(flow).toMatch(/<button[^>]*id="ik-add-continue"[^>]*disabled=""/);
    expect(flow).toContain("<strong>Pick your tracker</strong>");
  });

  it("the title and hint name no tracker", () => {
    const title = /<b class="ik-add-title">([^<]*)<\/b>/.exec(flow)?.[1];
    expect(title).toBe("Add a source");
    const hint = /id="ik-add-pick-hint">([\s\S]*?)<\/span>/.exec(flow)?.[1] || "";
    for (const p of CATALOG) expect(hint).not.toContain(p.label);
  });

  it("no source is drafted — no editor, no Connect step body", () => {
    expect(flow).not.toContain("tk-source-editor");
    expect(flow).toContain('data-step="1"');
  });
});

describe("once a tracker is chosen", () => {
  it("only then does the title name it", () => {
    const html = renderToStaticMarkup(
      createElement(AddFlowShell, {
        title: "Add Jira",
        step: 2,
        kinds: [...trackerKinds(CATALOG), REPO_KIND],
        picked: "jira",
        onPick() {},
        onCancel() {},
      })
    );
    expect(html).toContain('<b class="ik-add-title">Add Jira</b>');
    expect(html.match(/aria-checked="true"/g)).toHaveLength(1);
    expect(html).toMatch(/aria-checked="true" class="ik-kind picked" data-kind="jira"/);
  });

  it("the title is the chosen tracker's — drafted from a pick, never a default", () => {
    expect(TICKETS).toContain('const addTitle = pastPick && draftMeta ? "Add " + draftMeta.label : "Add a source";');
    // Step 1's tiles only mark a choice; Continue (or a tile pressed past
    // step 1) is what drafts a source.
    expect(has(TICKETS, 'if (!pastPick) { setChoice(key); setFlowError(""); } else if')).toBe(true);
    expect(has(TICKETS, "disabled={!choice}")).toBe(true);
    expect(has(TICKETS, "onClick={() => continueWith(choice)}")).toBe(true);
  });

  it("nothing in Intake drafts the catalog's first entry", () => {
    for (const f of ["TicketsTab", "kit", "RepoSources", "IntakeDialog", "IssuesTab", "PullRequestsTab"]) {
      const src = code("components/intake/" + f + ".tsx");
      expect(src, f).not.toMatch(/catalog\[0\]/);
    }
  });

  it("a new tracker still auto-starts nothing until a state is picked", () => {
    expect(TICKETS).toContain('const fresh = { id, provider, needs_filter: "on" } as Source;');
  });
});

describe("GitHub Issues the tracker vs a watched Repository", () => {
  const kinds = [...trackerKinds(CATALOG), REPO_KIND];
  const sub = (key: string) => kinds.find((k) => k.key === key)?.sub || "";

  it("every tile says, in one line, whose work it brings in", () => {
    for (const k of kinds) expect(k.sub, k.key).toBeTruthy();
    expect(sub("github_issues")).toBe("Issues assigned to you, as tickets");
    expect(sub("repo")).toBe("Every PR or new issue in a repo");
    expect(sub("github_issues")).not.toBe(sub("repo"));
  });

  it("an unknown tracker still gets a line", () => {
    expect(trackerKinds([{ id: "youtrack", label: "YouTrack" }])[0].sub).toBe("Tickets assigned to you");
  });

  it("the GitHub tabs offer the same described tiles", () => {
    for (const f of ["IssuesTab", "PullRequestsTab"]) {
      expect(has(code("components/intake/" + f + ".tsx"), "trackers={trackerKinds(catalog)}"), f).toBe(true);
    }
    expect(has(code("components/intake/RepoSources.tsx"), "[...trackers, REPO_KIND]")).toBe(true);
  });
});
