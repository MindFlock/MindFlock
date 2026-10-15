/** Intake → Pull requests (components/intake/PullRequestsTab.tsx + the shared
 * RepoSourceList in RepoSources.tsx) and the ticketing source cards
 * (TicketsTab.tsx): the switch, the watched-repo cards, the open-PR panel and
 * each source's Repo URL field.
 *
 * Moved from tests/unit/test_pr_review_settings.py (the four bundle tests),
 * tests/unit/test_pr_force_review.py (test_frontend_ships_the_open_pr_panel,
 * UI half) and tests/unit/test_multi_repo_ingestion.py (the two source-card
 * tests), which grepped the built app.js / style.css. These pin the owning
 * source module instead, comments stripped. That the bundle calls
 * /api/github/prs and /api/github/prs/review stays a Python contract test. */

/// <reference types="vite/client" />
import { describe, expect, it } from "vitest";
import { SRC, dropComments, norm } from "./sourceText";
import DIALOG_RAW from "../components/intake/IntakeDialog.tsx?raw";
import PRS_RAW from "../components/intake/PullRequestsTab.tsx?raw";
import AUTO_RAW from "../components/intake/AutoStartSwitches.tsx?raw";
import REPO_SOURCES_RAW from "../components/intake/RepoSources.tsx?raw";
import REPO_USES_RAW from "../components/intake/repoUses.ts?raw";
import KIT_RAW from "../components/intake/kit.tsx?raw";
import TICKETS_RAW from "../components/intake/TicketsTab.tsx?raw";
import REPO_FIELD_RAW from "../components/intake/RepoListField.tsx?raw";
import SETTINGS_DIALOG_RAW from "../components/settings/SettingsDialog.tsx?raw";
import WORKSPACE_RAW from "../components/settings/screens/Workspace.tsx?raw";
import INDEX_HTML from "../../index.html?raw";
import INTAKE_CSS_RAW from "../components/intake/IntakeDialog.css?raw";
import SCREENS_CSS_RAW from "../components/settings/screens/screens.css?raw";
import TICKETING_CSS_RAW from "../components/settings/screens/ticketingConnections.css?raw";

/** Source minus comments, layout-blind (`norm`). */
const code = (raw: string) => norm(dropComments(raw));

/** Every app module (tests excluded), comments stripped. */
const ALL = Object.values(SRC).map(code).join("\n");

const PRS = code(PRS_RAW);
/** The switch moved to the Auto-start tab with the other two (round 5). */
const AUTO = code(AUTO_RAW);
const REPOS = code(REPO_SOURCES_RAW);
const USES = code(REPO_USES_RAW);
const TICKETS = code(TICKETS_RAW);
const css = (raw: string) => raw.replace(/\/\*[\s\S]*?\*\//g, "").replace(/\s+/g, " ");

/** The declarations of the one rule that starts a line with `selector {`. */
function rule(raw: string, selector: string): string {
  const flat = raw.replace(/\/\*[\s\S]*?\*\//g, "");
  const esc = selector.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  const hits = [...flat.matchAll(new RegExp("^" + esc + " \\{([^}]*)\\}", "gm"))];
  expect(hits.length, selector).toBe(1);
  return hits[0][1].replace(/\s+/g, " ");
}

describe("the Pull requests tab", () => {
  it("has the feature block: switch, status, filters and its legacy id", () => {
    // The tab label in the Intake dialog's strip; the tab strip IS the heading,
    // so the switch label and its status line are what name the feature.
    expect(code(DIALOG_RAW)).toContain('label: "GitHub PRs"');
    // Named for what it does: it addresses review comments on YOUR PRs; it
    // never reviews anyone's (round-2: "Automated review" read as the opposite).
    expect(AUTO).toContain('label="PR review comments"');
    expect(PRS).not.toMatch(/Automated review|Begin review|auto review/);
    // Legacy id, kept on the work panel for addon CSS that targeted the screen.
    expect(code(DIALOG_RAW)).toContain('legacyId: "pr-review-block"');
    expect(code(DIALOG_RAW)).toContain("id={panelId(t)}");
    expect(code(DIALOG_RAW)).toContain("t.legacyId || \"intake-tabpanel-\" + t.key");
    // Explicit on/off (pause without deleting repos) + a status line.
    expect(AUTO).toContain('inputId="gh-pr-enabled"');
    expect(AUTO).toContain('statusId="gh-pr-status"');
    expect(PRS).toContain('<AutoStartLink kind="prs" gotoTab={gotoTab} />');
    expect(PRS).toContain('id="gh-skip-authors"');
    expect(PRS).toContain('field="min_age_minutes"');
    expect(PRS).toContain('field="poll_interval_seconds"');
    // The global default repo is gone; workspace settings live on their own
    // screen and each ticketing source names its own repo instead.
    expect(code(SETTINGS_DIALOG_RAW)).toContain('{ key: "workspace", label: "Workspace"');
    expect(code(WORKSPACE_RAW)).toContain('field="workspace_dir"');
    // No global repo URL field anywhere (the page shell included).
    expect(INDEX_HTML).not.toContain('data-field="url"');
    expect(ALL).not.toContain('data-field="url"');
  });

  it("a watched repo is edited from its HEADING, and added once", () => {
    // No card list under the work: every watched repository has a heading in
    // the list (even filtered to nothing), whose Edit opens its editor.
    expect(PRS).not.toContain("RepoSourceList");
    expect(PRS).toContain("repoGroupOrder(");
    expect(PRS).toContain('editor={editing ? <RepoEditor use="pr" repo={repo} /> : undefined}');
    expect(PRS).toContain("<HeadAction editing={editing}");
    // "+ Add source" in the dialog head — the repository flow, PR ticked.
    expect(PRS).toContain("<AddRepoFlow");
    expect(PRS).toContain('uses={adding?.uses?.length ? adding.uses : ["pr"]}');
    expect(ALL).not.toContain("gh-repo-new");
    for (const field of ["agent", "depth", "base_branch", "min_age_minutes", "skip_authors"])
      expect(REPOS, field).toContain(field);
    expect(REPOS).toContain("data-repo-field={field}");
    expect(REPOS).toContain('data-repo-field="agent"');
  });

  it("wires the watched repos and the toggle", () => {
    expect(AUTO).toContain("gh-pr-enabled");
    expect(PRS).toContain("gh-skip-authors");
    expect(PRS).toContain("skip_authors");
    // The owner/name guard lives once, in the add flow.
    expect(REPOS).toContain("Type the repository as owner/name");
    // The lists are written by repoUses.ts — one patch for every tick, and
    // the first PR repository of a setup that never chose is saved OFF.
    expect(REPOS).toContain("usesPatch(s.settings, repo, want)");
    expect(USES).toContain('if (gh.enabled == null && !repos.length) put(group, "enabled", false);');
    expect(PRS).toContain("const enabled = prsOn(gh);"),
    // No "● Active / ‖ Paused" status line under the switch any more; the
    // switch's only subtitle is the one state a switch can't show.
    expect(ALL).not.toContain("Paused —");
    expect(AUTO).toContain('"Auto-address review comments off"');
    expect(AUTO).toContain('"Add a repository first — on the GitHub PRs tab"');
    // An empty tab is one setup card, not the wizard plus a second Connect.
    expect(PRS).toContain("<GithubSetupCard");
    expect(PRS).toContain("const showAdd = !!adding;");
  });

  it("styles the feature and the repo cards", () => {
    const screens = css(SCREENS_CSS_RAW);
    // Chips still carry the ticketing state picker.
    expect(screens).toContain(".repo-chip {");
    expect(screens).toContain(".repo-chip-x {");
    // The card the watched repos render as, and the grouped work list.
    expect(css(TICKETING_CSS_RAW)).toContain(".tk-source {");
    const intake = css(INTAKE_CSS_RAW);
    expect(intake).toContain(".ik-cards {");
    expect(intake).toContain(".ik-groups {");
  });
});

describe("the open-PR panel", () => {
  it("ships its list, refresh and Start", () => {
    expect(PRS).toContain('refreshId="gh-prs-refresh"');
    expect(PRS).toContain('listId="gh-prs-list"');
    expect(PRS).toContain('actionLabel="Start"');
    expect(rule(SCREENS_CSS_RAW, ".pr-open-item")).toContain("grid-template-columns");
  });
});

describe("ticketing source cards", () => {
  it("each card has a Repo URL field", () => {
    // Repo URL is required per source (there is no global default repo). The
    // field moved into RepoListField when a source learned to hold a SET of
    // repos; with one repo it is still labelled "Repo URL".
    expect(TICKETS).toMatch(/<RepoListField\s+source=\{source\}/);
    const field = code(REPO_FIELD_RAW);
    expect(field).toContain('data-tk-field={i === 0 ? "repo_url" : undefined}');
    expect(field).toContain('{many ? "Repos" : "Repo URL"}');
  });

  it("a source is edited from its heading — one editor open at a time", () => {
    expect(TICKETS).toContain("editor={isEditing ? editorFor(src) : undefined}");
    expect(TICKETS).toContain("const edit = useEditing(focus);");
    const kit = code(KIT_RAW);
    expect(kit).toContain('className="ik-editor"');
    expect(kit).toContain("setEditing((cur) => (cur === key ? null : key))");
    expect(kit).not.toContain('className="tk-head"');
  });
});
