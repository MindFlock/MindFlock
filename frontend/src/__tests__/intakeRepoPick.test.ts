/** A multi-repo ticket source's row can start a ticket in a repo other than
 * the routed one (Options → Repo); Start posts it as `repo`. */

/// <reference types="vite/client" />
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";
import { RepoPick, type ItemRoute } from "../components/intake/kit";
import KIT_RAW from "../components/intake/kit.tsx?raw";
import TICKETS_RAW from "../components/intake/TicketsTab.tsx?raw";

const BACK = "git@github.com:acme/vnext-backend.git";
const FRONT = "git@github.com:acme/vnext-frontend.git";
const CHOICES = [
  { name: "vnext-backend", url: BACK },
  { name: "vnext-frontend", url: FRONT },
];

const html = (route: ItemRoute | null, value = "") =>
  renderToStaticMarkup(
    createElement(RepoPick, { route, value, reference: "sc-1", onChange() {} })
  );

describe("the row's Repo picker", () => {
  it("only appears for a source with several repos", () => {
    expect(html(null)).toBe("");
    expect(html({ repos: ["vnext-backend"], choices: [CHOICES[0]] })).toBe("");
    // An older server sends no choices: nothing to pick from.
    expect(html({ repos: ["vnext-backend"], method: "label" })).toBe("");
  });

  it("defaults to the routed repo and lists every repo of the source", () => {
    const out = html({ repos: ["vnext-frontend"], repo_url: FRONT, method: "label", choices: CHOICES });
    expect(out).toContain('class="ik-item-repo"');
    expect(out).toContain('<span class="ik-item-pick-label">Repo</span>');
    expect(out).toContain('<option value="" selected="">Default (vnext-frontend)</option>');
    expect(out).toContain('<option value="' + BACK + '">vnext-backend</option>');
    expect(out).toContain('<option value="' + FRONT + '">vnext-frontend</option>');
  });

  it("says the agent picks when nothing on the ticket routes it yet", () => {
    expect(html({ repos: [], method: "pending", choices: CHOICES })).toContain("Default (agent picks)");
  });

  it("shows a pick as picked", () => {
    const out = html({ repos: ["vnext-frontend"], method: "label", choices: CHOICES }, BACK);
    expect(out).toContain('data-picked="' + BACK + '"');
    expect(out).toContain('<option value="' + BACK + '" selected="">vnext-backend</option>');
  });

  it("Start passes the pick, and only when there is one", () => {
    expect(KIT_RAW).toContain("<RepoPick");
    expect(KIT_RAW).toMatch(/effort: effortUsable \? effort : "",\s*repo,/);
    expect(TICKETS_RAW).toContain("onStart={async ({ agent, depth, effort, repo }) => {");
    expect(TICKETS_RAW).toContain("...(repo ? { repo } : {}),");
  });
});
