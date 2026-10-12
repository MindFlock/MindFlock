/** The UI surfaces for auth profiles: Settings → Accounts, the New dialog's
 * account and model pickers, and the pane header's account-swap chip.
 *
 * Moved from tests/unit/test_frontend_accounts.py, which grepped the built
 * bundle. The routes these call are a backend contract and stay pinned there;
 * here: the screen is registered, the pickers and the chip are wired, and the
 * chip's copy promises what is actually true. */

import { describe, expect, it } from "vitest";
import type { AuthProfile } from "../api/types";
import { accountRows, swapLabel } from "../lib/accountRows";
import { ALL_CODE, attr, src, squash, tagsWith, tagsWithId } from "./sourceText";

const SETTINGS = src("components/settings/SettingsDialog.tsx");
const AGENT_ACCOUNT = src("components/dialogs/newSession/AgentAccount.tsx");
const CHIP = src("components/grid/AccountChip.tsx");

describe("Settings → Accounts", () => {
  it("is a registered screen", () => {
    // The screen KEY, not the word "Accounts" — that appeared in the app before
    // this feature existed, so asserting on it proves nothing.
    expect(SETTINGS).toContain('{ key: "accounts", label: "Accounts",');
  });
});

describe("the New dialog's account picker", () => {
  it("is a select, with a way to manage accounts in Settings", () => {
    const picker = tagsWithId(AGENT_ACCOUNT, "new-account");
    expect(picker.map((t) => t.name)).toEqual(["select"]);
    // The unset option must NAME the fallback; "App default" alone is not the
    // assertion to make (it predates the feature) — key on the picker's own
    // affordance instead.
    const manage = tagsWithId(AGENT_ACCOUNT, "new-account-manage");
    expect(manage).toHaveLength(1);
    expect(attr(manage[0], "title")).toBe("Manage accounts in Settings");
  });

  it("has a per-session model field that rides along as profile_model", () => {
    // With an OpenRouter/key account the model is the choice that matters.
    expect(tagsWithId(AGENT_ACCOUNT, "new-account-model").length).toBeGreaterThan(0);
    expect(src("lib/newSessionSubmit.ts")).toContain(
      "if (profileId && profileModel.trim()) body.profile_model = profileModel.trim();"
    );
    // A no-route agent/account combination warns instead of silently
    // launching on the CLI's own login.
    expect(AGENT_ACCOUNT).toContain("has no route for");
  });
});

describe("the pane header's account-swap chip", () => {
  it("is a chip whose rows are styled, hoverable rows", () => {
    expect(tagsWith(CHIP, "className", "tok usage-trigger acct-chip")).toHaveLength(1);
    // The class is what usage.css hangs the hover state on — rows, not bare
    // table cells with an inline cursor.
    expect(CHIP).toContain('className={"acct-pop-row" + (row.current ? " acct-pop-current" : "")}');
    expect(src("components/usage/usage.css")).toContain(".acct-pop-row:hover td");
  });

  it("offers the inherit row a session following the app default must keep", () => {
    // Without it the only clickable row for such a session pins it to whatever
    // the default currently resolves to: the menu's one lossy misclick.
    const profiles = [{ id: "work", label: "Work" }] as AuthProfile[];
    const rows = accountRows("", "work", profiles);
    expect(rows[0]).toEqual({ id: "", label: "App default (Work)", current: true });
    expect(swapLabel("", profiles)).toBe("the app default account");
    expect(CHIP).toContain("accountRows(");
  });

  it("explains the per-account conversation truthfully", () => {
    // A window keeps one thread per identity — never the older, wrong claim
    // that a swap resumes whatever conversation happened to be open.
    expect(squash(CHIP)).toContain("one conversation per account");
    expect(ALL_CODE).not.toContain("conversations live per account");
  });
});
