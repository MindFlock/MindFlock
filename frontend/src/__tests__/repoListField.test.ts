/** A ticketing source's repo field: one repo, or a set of them with hints
 * (components/intake/RepoListField.tsx + repoList.ts). The server half — the
 * stored shape and the routing — is tests/unit/test_repo_routing.py. */

import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";
import type { TicketingSource } from "../api/types";
import {
  REPO_HELP_MANY,
  REPO_HELP_ONE,
  RepoListField,
} from "../components/intake/RepoListField";
import {
  addRepoRow,
  cleanRepos,
  removeRepoRow,
  reposPatch,
  sameRepos,
  sourceRepos,
} from "../components/intake/repoList";

const BACK = "git@github.com:acme/vnext-backend.git";
const FRONT = "git@github.com:acme/vnext-frontend.git";

const src = (extra: Record<string, unknown> = {}) =>
  ({ id: "sc", provider: "shortcut", ...extra }) as unknown as TicketingSource;

const render = (source: TicketingSource) =>
  renderToStaticMarkup(createElement(RepoListField, { source, onChange() {} }));

describe("reading a source's repos", () => {
  it("reads an old single-repo source as a one-element set", () => {
    expect(sourceRepos(src({ repo_url: BACK }))).toEqual([{ url: BACK }]);
    expect(sourceRepos(src())).toEqual([]);
  });

  it("reads the stored list when it names two or more", () => {
    const s = src({ repo_url: BACK, repos: [{ url: BACK, hint: "api" }, { url: FRONT }] });
    expect(sourceRepos(s)).toEqual([{ url: BACK, hint: "api" }, { url: FRONT }]);
  });

  it("lets repo_url win over a lone or empty list (what a one-repo save stores)", () => {
    expect(sourceRepos(src({ repo_url: FRONT, repos: [] }))).toEqual([{ url: FRONT }]);
    expect(sourceRepos(src({ repo_url: FRONT, repos: "" }))).toEqual([{ url: FRONT }]);
  });
});

describe("writing it back", () => {
  it("one repo saves the historic shape: repo_url and an empty list (a hint means nothing alone)", () => {
    expect(reposPatch([{ url: ` ${BACK} `, hint: "api" }])).toEqual({
      repo_url: BACK,
      repos: [],
    });
  });

  it("several repos save the list, repo_url mirroring the first", () => {
    const patch = reposPatch([
      { url: BACK, hint: " api " },
      { url: "" },
      { url: FRONT, hint: "" },
      { url: BACK },
    ]) as unknown as { repo_url: string; repos: unknown };
    expect(patch.repo_url).toBe(BACK);
    expect(patch.repos).toEqual([{ url: BACK, hint: "api" }, { url: FRONT }]);
  });

  it("blank rows are on screen but not stored", () => {
    expect(cleanRepos(addRepoRow([{ url: BACK }]))).toEqual([{ url: BACK }]);
    expect(sameRepos(cleanRepos([{ url: BACK }, { url: " " }]), [{ url: BACK }])).toBe(true);
    expect(sameRepos([{ url: BACK, hint: "a" }], [{ url: BACK }])).toBe(false);
  });

  it("add appends a blank row; remove never leaves zero rows", () => {
    expect(addRepoRow([{ url: BACK }])).toEqual([{ url: BACK }, { url: "", hint: "" }]);
    expect(removeRepoRow([{ url: BACK }, { url: FRONT }], 0)).toEqual([{ url: FRONT }]);
    expect(removeRepoRow([{ url: BACK }], 0)).toEqual([{ url: "", hint: "" }]);
  });
});

describe("the field", () => {
  it("with one repo it is the Repo URL box it always was, plus + Add repo", () => {
    const html = render(src({ repo_url: BACK }));
    expect(html).toContain(">Repo URL</span>");
    expect(html).toContain('data-tk-field="repo_url"');
    expect(html).toContain(`value="${BACK}"`);
    expect(html).toContain("+ Add repo");
    expect(html).not.toContain("tk-repo-hint");
    expect(html).not.toContain("tk-repo-remove");
    expect(html).toContain(REPO_HELP_ONE.replace(/'/g, "&#x27;").slice(0, 40));
  });

  it("flags a missing repo", () => {
    expect(render(src())).toContain("tk-repo-url field-missing");
    expect(render(src({ repo_url: BACK }))).not.toContain("field-missing");
  });

  it("with several repos: one row each, with a hint and a Remove", () => {
    const html = render(
      src({ repo_url: BACK, repos: [{ url: BACK, hint: "api, django" }, { url: FRONT }] })
    );
    expect(html).toContain(">Repos</span>");
    expect(html.match(/class="tk-repo-row"/g)?.length).toBe(2);
    expect(html.match(/class="tk-repo-hint"/g)?.length).toBe(2);
    expect(html.match(/aria-label="Remove repo \d"/g)?.length).toBe(2);
    expect(html).toContain('value="api, django"');
    // Only the first row carries the field marker the source card's save reads.
    expect(html.match(/data-tk-field="repo_url"/g)?.length).toBe(1);
    expect(html).toContain(REPO_HELP_MANY.slice(0, 40));
  });
});
