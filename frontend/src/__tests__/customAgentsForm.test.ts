/** Settings → Custom agents: adding one that won't start as typed (a shell
 * alias) must leave a way to fix it, and the form must say what each field
 * does before you save — found walking the screen as a new user (polish
 * pass 3, area c). */

import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";
import { Providers, editingAfterSave, sourceBadge } from "../components/settings/screens/Providers";

const html = () =>
  renderToStaticMarkup(
    createElement(Providers, { active: true, gotoScreen: () => {}, onOpenSysLogsPane: () => {} })
  );

describe("saving a custom agent that answers with a launch warning", () => {
  it("switches the form to editing that agent, so the fix is a PUT, not a second Add", () => {
    // The server saved it AND warned it won't start (alias, bad path). On the
    // Add form the next click POSTed the same name: 409 "already exists".
    expect(editingAfterSave("claudekiro", "'claudekiro' was not found on PATH.")).toBe("claudekiro");
  });

  it("closes the form after a clean save", () => {
    expect(editingAfterSave("mycli", "")).toBeNull();
  });
});

describe("the add form", () => {
  it("labels every field (placeholders vanished as you typed)", () => {
    const out = html();
    for (const [id, label] of [
      ["prov-new-name", "Name"],
      ["prov-new-binary", "Binary path"],
      ["prov-new-args", "Launch flags"],
      ["prov-new-resume", "Resume flag"],
      ["prov-new-skip", "Skip-permissions flag"],
      ["prov-new-program", "Also matches"],
    ]) {
      const re = new RegExp(`<label class="set-row"><span class="set-label">${label}</span>(?:(?!</label>).)*id="${id}"`);
      expect(out, id).toMatch(re);
    }
  });

  it("explains the shell-alias trap at the binary path field, before saving", () => {
    const out = html();
    const binary = out.slice(out.indexOf('id="prov-new-binary"'), out.indexOf('id="prov-new-args"'));
    expect(binary).toContain("shell alias");
    expect(binary).toContain("plain shell");
  });

  it("says the 'also matches' names don't change what runs", () => {
    const out = html();
    const program = out.slice(out.indexOf('id="prov-new-program"'));
    expect(program).toContain("doesn&#x27;t change what runs");
  });

  it("points at the TOML file for what the form doesn't cover", () => {
    expect(html()).toContain('id="prov-toml-hint"');
    expect(html()).toContain("~/.mindflock-assistant/providers/");
  });

  it("sits above the provider rows, not after eight built-ins", () => {
    const out = html();
    expect(out.indexOf('id="prov-add"')).toBeGreaterThan(-1);
    expect(out.indexOf('id="prov-add"')).toBeLessThan(out.indexOf('class="prov-conn-list"'));
  });
});

describe("a provider's source badge", () => {
  it("reads as words, not the registry's code", () => {
    expect(sourceBadge("builtin")).toBe("built in");
    expect(sourceBadge("user")).toBe("custom");
  });
});
