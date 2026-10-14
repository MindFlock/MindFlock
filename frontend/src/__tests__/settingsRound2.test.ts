/** Settings, UX round 2: Notifications never shows a template placeholder
 * and groups its events; Connections leads with what you use and shows other
 * agents neutrally; the palette finds settings by the words people type. */
import { describe, expect, it } from "vitest";
import type { ConnectionRow } from "../api/types";
import {
  groupRules,
  hasPlaceholder,
  MORE_GROUP,
  RULE_COPY,
  RULE_GROUPS,
  ruleCopy,
  ruleGroup,
} from "../components/settings/screens/notifyCopy";
import {
  isCoreRow,
  plainDetail,
  rowName,
  shownStatus,
  splitRows,
  summaryLine,
} from "../components/settings/screens/Connections";
import { flagsPlaceholder } from "../components/settings/screens/CodingCli";
import { navHeading, navScreens } from "../components/settings/settingsMode";
import { SETTINGS_SEARCH, searchSettings, settingsActions } from "../components/palette/settingsSearch";
import dialogSrc from "../components/settings/SettingsDialog.tsx?raw";
import paletteSrc from "../components/palette/CommandPalette.tsx?raw";
import notifSrc from "../components/settings/screens/Notifications.tsx?raw";
import codingSrc from "../components/settings/screens/CodingCli.tsx?raw";
import generalSrc from "../components/settings/screens/General.tsx?raw";

/** Every rule id the server ships (backend/web/addons/notify.py), with the
 * templated title/body it sends — the screen must never show these bodies. */
const SERVER_RULES = [
  ["session_offline", "The agent process is gone — Restart it from the rail."],
  ["needs_input", "The agent is waiting on a clarification."],
  ["pr_closed", "The pull request is no longer open."],
  ["pr_approved", "A reviewer approved the pull request — it is ready to merge."],
  ["pr_changes_requested", "A reviewer asked for changes on the pull request."],
  ["budget_exceeded", "The session's estimated cost crossed its configured budget."],
  ["usage_limit", "The agent hit its usage limit."],
  ["usage_restored", "The provider's window reset — {session} can run again."],
  ["session_idle", "The agent finished its turn."],
  ["run_needs_you", "{detail}"],
  ["run_finished", "{detail}"],
  ["run_task_shipped", "{detail}"],
  ["precommit_running", "A commit triggered the pre-commit hooks."],
  ["precommit_failed", "A commit was blocked by the pre-commit hooks — re-commit to continue."],
  ["verify_plan_ready", "The agent finished writing the checklist for this work."],
  ["verify_plan_failed", "No checklist was written — open Verify for the reason."],
  ["verify_run_finished", "The agent worked the checklist — open Verify for what it found."],
  ["verify_run_gave_up", "The run recorded no answers — the plan is back to unchecked."],
  ["red_zone_blocked", "MindFlock {detail}."],
  ["red_zone_breached", "The agent {detail}."],
  ["red_zone_tampered", "{detail}."],
  ["device_join", "{detail}"],
].map(([id, body]) => ({ id, label: id, title: "{session}: x", body, enabled: true }));

describe("Notifications copy", () => {
  it("has plain words for every rule the server ships, none with a {placeholder}", () => {
    for (const r of SERVER_RULES) {
      const c = ruleCopy(r);
      expect(RULE_COPY[r.id], r.id).toBeTruthy();
      expect(hasPlaceholder(c.label), r.id).toBe(false);
      expect(hasPlaceholder(c.desc), r.id).toBe(false);
      // No internal ids in the words either.
      expect(c.label + c.desc).not.toMatch(/session\.[a-z_]+|_[a-z]+_/);
    }
  });

  it("an unknown rule never shows a templated body or a {placeholder} title", () => {
    expect(ruleCopy({ id: "new_rule", label: "Something new", body: "{detail}" })).toEqual({
      label: "Something new",
      desc: "",
    });
    expect(ruleCopy({ id: "x", title: "{session} did it", body: "Done." })).toEqual({
      label: "Another event",
      desc: "Done.",
    });
  });

  it("groups: Needs you (session finished included), Pull requests, Usage & cost, then the folded rest", () => {
    expect(RULE_GROUPS.map((g) => g.title)).toEqual(["Needs you", "Pull requests", "Usage & cost"]);
    expect(ruleGroup("session_idle")).toBe("needs");
    for (const id of ["red_zone_tampered", "precommit_running", "run_task_shipped", "verify_plan_ready"])
      expect(ruleGroup(id), id).toBe(MORE_GROUP);
    const { groups, more } = groupRules(SERVER_RULES);
    const shown = groups.flatMap((g) => g.rules.map((r) => r.id));
    // Every rule lands somewhere exactly once.
    expect([...shown, ...more.map((r) => r.id)].sort()).toEqual(SERVER_RULES.map((r) => r.id).sort());
  });

  it("the screen renders our words, the channels block first, and never rule.body", () => {
    expect(notifSrc).toContain("ruleCopy(rule)");
    expect(notifSrc).not.toContain("{rule.body");
    expect(notifSrc.indexOf("Where alerts go")).toBeLessThan(notifSrc.indexOf("What you're alerted about"));
    expect(notifSrc).toContain(">Desktop notifications<");
    expect(notifSrc).not.toContain("Desktop / Chrome notifications");
  });
});

const row = (over: Partial<ConnectionRow> & Record<string, unknown>): ConnectionRow =>
  ({ id: "x", name: "X", kind: "agent", status: "connected", detail: "", action: null, test: null, ...over }) as ConnectionRow;

describe("Connections leads with what you use", () => {
  const rows = [
    row({ id: "agent", provider: "claude", default: true, detail: "signed in (login state found in /h/.claude.json) · /usr/bin/claude" }),
    row({ id: "agent:codex", provider: "codex", status: "needs_attention", detail: "installed, not signed in · /x/codex" }),
    row({ id: "agent:aider", provider: "aider", status: "off", detail: "not installed — `aider` not found on PATH" }),
    row({ id: "agent:agy", provider: "antigravity", detail: "installed · sign-in can't be checked from here · /x/agy" }),
    row({ id: "github", kind: "github", status: "off", detail: "You are not logged into any GitHub hosts. To log in, run: gh auth login · PR review: no repos set" }),
    row({ id: "ticketing", kind: "tracker", status: "off", name: "Ticketing" }),
    row({ id: "devices", kind: "devices", status: "needs_attention" }),
  ];

  it("splits default agent + GitHub + tracker / other agents / the rest", () => {
    const { core, otherAgents, rest } = splitRows(rows);
    expect(core.map((r) => r.id)).toEqual(["agent", "github", "ticketing"]);
    expect(otherAgents.map((r) => r.id)).toEqual(["agent:codex", "agent:aider", "agent:agy"]);
    expect(rest.map((r) => r.id)).toEqual(["devices"]);
  });

  it("the header counts what you use — the same rows in both modes", () => {
    expect(summaryLine(rows.filter(isCoreRow))).toBe("1 connected · 0 need attention · 2 off");
  });

  it("an agent you never set up is neutral, not a warning", () => {
    expect(shownStatus(rows[1])).toMatchObject({ pill: "Not set up", tone: "neutral" });
    expect(shownStatus(rows[2])).toMatchObject({ pill: "Not installed", tone: "neutral" });
    expect(shownStatus(rows[3])).toMatchObject({ pill: "Installed", tone: "neutral" });
    expect(shownStatus(rows[0])).toMatchObject({ pill: "Connected", tone: "connected" });
  });

  it("a failing Test outranks a stored Connected (both screens then agree)", () => {
    expect(shownStatus(rows[0], { ok: false, text: "no login" })).toMatchObject({
      status: "needs_attention",
      pill: "Needs attention",
    });
  });

  it("names agents by product, with no file paths or raw gh output", () => {
    expect(rowName(rows[0])).toBe("Agent · Claude Code");
    expect(rowName(rows[5])).toBe("Ticket tracker");
    expect(plainDetail(rows[0])).toBe("Signed in");
    expect(plainDetail(rows[3])).not.toContain("/x/agy");
    expect(plainDetail(rows[4])).toBe("Not connected. Agents can't open pull requests yet.");
    expect(
      plainDetail(row({ kind: "github", account: "octo", detail: "authenticated as @octo (token from settings) · PR review on · org/a" }))
    ).toBe("Connected as @octo · PR review on · org/a");
  });
});

describe("Agents screen", () => {
  it("never suggests a permission-skipping flag, and follows the agent", () => {
    expect(codingSrc).not.toContain('placeholder="--dangerously-skip-permissions"');
    expect(flagsPlaceholder("claude")).not.toMatch(/dangerous|skip|bypass|yes-always/);
    expect(flagsPlaceholder("aider")).toContain("--no-auto-commits");
    expect(flagsPlaceholder("goose")).toBe("");
    expect(codingSrc).toContain("placeholder={flagsPlaceholder(selected)}");
  });

  it("answers 'how many at once' plainly, with the real field", () => {
    expect(codingSrc).toContain('id="coding-sessions-at-once"');
    expect(codingSrc).toContain('<SettingField group="engine" field="max_sessions"');
    expect(codingSrc).toContain("Sessions you start yourself have no limit.");
  });

  it("General carries no internal event id", () => {
    expect(generalSrc).not.toContain("session.budget_exceeded");
  });
});

describe("nav: a peeked advanced screen goes last", () => {
  const S = [
    { key: "a", advanced: false },
    { key: "p", group: "G", advanced: true },
    { key: "w", group: "C", advanced: false },
    { key: "z", group: "D", advanced: false },
  ];
  it("keeps basic screens out from under the Advanced heading", () => {
    const shown = navScreens(S, false, "p");
    expect(shown.map((s) => s.key)).toEqual(["a", "w", "z", "p"]);
    expect(shown.map((_, i) => navHeading(shown, i, false))).toEqual(["", "", "", "Advanced"]);
  });
});

describe("palette settings search", () => {
  it("every entry names a real screen key with its real nav label", () => {
    const labels = new Map<string, string>();
    for (const m of dialogSrc.matchAll(/\{ key: "([a-z]+)", label: "([^"]+)"/g)) labels.set(m[1], m[2]);
    for (const e of SETTINGS_SEARCH) {
      expect(labels.has(e.key), e.key).toBe(true);
      expect(e.label.split(" → ")[0], e.key).toBe(labels.get(e.key)!.replace("&amp;", "&"));
    }
  });

  it("finds the screens by the words a new user types", () => {
    const first = (q: string) => searchSettings(q).map((e) => e.label);
    expect(first("github")).toContain("Connections → GitHub");
    expect(first("notification")[0]).toBe("Notifications");
    expect(first("worktree")).toContain("Workspace");
    expect(first("parallel")).toContain("Agents → Sessions running at once");
    expect(first("concurrency")).toContain("Agents → Sessions running at once");
    expect(first("phone")).toEqual(expect.arrayContaining(["Notifications → Phone alerts", "Your phone"]));
    expect(first("token")).toEqual(expect.arrayContaining(["Connections → GitHub", "Security"]));
    expect(first("theme")).toEqual(["Appearance"]);
    expect(first("how many sessions")).toContain("Agents → Sessions running at once");
  });

  it("offers nothing for an empty or one-letter query, and labels rows 'Settings → …'", () => {
    expect(settingsActions("")).toEqual([]);
    expect(settingsActions("g")).toEqual([]);
    expect(settingsActions("github")[0].label).toMatch(/^Settings → /);
  });

  it("is merged into the palette's results in one place", () => {
    expect(paletteSrc).toContain('import { settingsActions } from "./settingsSearch";');
    expect(paletteSrc.match(/settingsActions\(/g)).toHaveLength(1);
  });
});
