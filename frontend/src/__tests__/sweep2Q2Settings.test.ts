/** UI sweep round 2, package Q2 — the Settings screens round 1 skipped.
 *
 *  - #5  Deleting a custom agent asks first (InlineConfirm), and says
 *        "Deleted X" / "Couldn't delete X: why".
 *  - #6  Your phone re-reads its steps when the embedded Tailscale card sees
 *        the sign-in finish (TailscaleCard onHealth).
 *  - #18 Revoking a guest link asks first, in the danger style.
 *  - #19 Saves report once, by the control: no up-front success toast; the
 *        inline "✓ Saved" (flashSaved) with a toast only as the fallback;
 *        the auto-adopt failure carries the server's reason.
 *  - #20 Load failures read "Couldn't load <what>." with a Retry; System logs
 *        keeps its last good text, and Copy log stays off until there is some.
 *  - #29 Ctrl+F focuses "Search settings".
 *  - #30 The Settings polls skip ticks while the window is hidden. */

import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";
import { code, squash, src } from "./sourceText";
import { runningFlippedOn, type TailscaleHealth } from "../components/settings/screens/TailscaleCard";
import { logViewText } from "../components/settings/screens/SystemLogs";
import { SEARCH_PLACEHOLDER, SettingsSearch, isFindKey } from "../components/settings/SettingsSearch";

const flat = (path: string) => squash(code(path));
const SCREENS = "components/settings/screens/";

/** Every load-failure line has the shell's shape: a set-hint alert, the
 * sentence, then a linklike Retry. Raw text, not `code`: Traffic's JSX prose
 * ("mindflock.ai/go/*") reads to the comment stripper as a comment opener. */
function expectRetryLine(path: string, sentence: string, retry: string) {
  expect(squash(src(path))).toContain(
    `role="alert"> ${sentence}{" "} <button type="button" className="linklike" onClick={() => ${retry}}> Retry </button>`
  );
}

describe("#5 deleting a custom agent asks first", () => {
  const src = () => flat(SCREENS + "Providers.tsx");

  it("Delete opens an inline confirm instead of deleting", () => {
    expect(src()).toContain("onClick={() => setConfirmDel(row.name)}");
    expect(src()).not.toContain("onClick={() => del(row.name)}");
    expect(src()).toContain('title={"Delete custom agent " + agentName(row.name) + "?"}');
    expect(src()).toContain('body="Its file is removed."');
    expect(src()).toContain('confirmLabel={delBusy ? "Deleting…" : "Delete"}');
    expect(src()).toContain("onConfirm={() => void del(row.name)} onCancel={() => setConfirmDel(null)}");
  });

  it("toasts say Deleted / Couldn't delete with the reason", () => {
    expect(src()).toContain('toast("Deleted " + agentName(name));');
    expect(src()).toContain(`toast("Couldn't delete " + agentName(name) + ": " + ((err as Error).message || "unknown error"));`);
    expect(src()).not.toContain('"Removed "');
  });
});

describe("#6 Your phone follows the Tailscale sign-in", () => {
  const h = (running: boolean) => ({ running }) as TailscaleHealth;

  it("runningFlippedOn is true only for a real not-running → running flip", () => {
    expect(runningFlippedOn(h(false), h(true))).toBe(true);
    expect(runningFlippedOn(null, h(true))).toBe(false);
    expect(runningFlippedOn(h(true), h(true))).toBe(false);
    expect(runningFlippedOn(h(false), h(false))).toBe(false);
    expect(runningFlippedOn(h(false), null)).toBe(false);
  });

  it("the card calls onHealth on the flip, and Mobile reloads on it", () => {
    expect(flat(SCREENS + "TailscaleCard.tsx")).toContain(
      "if (runningFlippedOn(prevHealth.current, health) && health) onHealthRef.current?.(health);"
    );
    const mobile = flat(SCREENS + "Mobile.tsx");
    expect(mobile.split("<TailscaleCard active={p.active} onHealth={() => void load()} />").length).toBe(3);
    expect(mobile).not.toContain("<TailscaleCard active={p.active} />");
  });
});

describe("#18 revoking a guest link asks first", () => {
  it("Revoke… is a danger button that opens an inline confirm", () => {
    const src = flat(SCREENS + "peer/GuestLinks.tsx");
    expect(src).toContain('className="test-btn peer-danger" disabled={busy || confirmRevoke === l.id} onClick={() => setConfirmRevoke(l.id)} > Revoke…');
    expect(src).not.toContain("onClick={() => void revoke(l.id)}");
    expect(src).toContain('title="Revoke this guest link?" body="Whoever has it loses access."');
    expect(src).toContain("onConfirm={() => void revoke(l.id)} onCancel={() => setConfirmRevoke(null)}");
  });
});

describe("#19 a save reports once, by the control", () => {
  it("Agent MCP and failure notes no longer toast before the save answers", () => {
    const orch = flat(SCREENS + "AgentOrchestration.tsx");
    expect(orch).toContain('onChange={(e) => void s.saveField("general", "agent_mcp", e.target.checked)}');
    expect(orch).not.toContain("Agent MCP on —");
    const ws = flat(SCREENS + "Workspace.tsx");
    expect(ws).toContain('onChange={(e) => void s.saveField("repository", "fasttrack_failure_notes", e.target.checked)}');
    expect(ws).not.toContain("MindFlock will tell the agent what failed");
  });

  it("Appearance's switches and minute fields cue inline, toast only as the fallback", () => {
    const src = flat(SCREENS + "Appearance.tsx");
    for (const t of ['"Reduce motion on"', '"Birds in the Sky on"', '"Break reminder on — every "', '"Idle flock on — after "']) {
      const at = src.indexOf(t);
      expect(at).toBeGreaterThan(0);
      expect(src.slice(Math.max(0, at - 80), at)).toContain("if (!flashSaved(e.target))");
    }
    expect(src).toContain('if (!flashSaved(el) && on) toast("Break reminder every " + next + " min");');
    expect(src).toContain('if (!flashSaved(el) && on) toast("Idle flock after " + next + " min");');
  });

  it("IDE auto-adopt cues inline and its failure says why", () => {
    const src = flat(SCREENS + "Ide.tsx");
    expect(src).toContain('if (!flashSaved(el)) toast(r?.enabled ? "IDE auto-adopt on" : "IDE auto-adopt off");');
    expect(src).toContain(`"Couldn't turn IDE auto-adopt " + (want ? "on" : "off") + ": " + ((err as Error).message`);
    expect(src).not.toContain("Auto-adopt toggle failed");
  });

  it("a custom agent's binary path cues inline", () => {
    const src = flat(SCREENS + "Providers.tsx");
    expect(src).toContain("if (!flashSaved(el)) {");
    expect(src).toContain("onBlur={(e) => void save(e.target)}");
  });
});

describe("#20 one load-failure line, with Retry", () => {
  it("each screen says Couldn't load … and offers Retry", () => {
    expectRetryLine(SCREENS + "SystemLogs.tsx", "Couldn't load the log.", "void load()");
    expectRetryLine(SCREENS + "SharedLink.tsx", "Couldn't load the shared link.", "void load()");
    expectRetryLine(SCREENS + "Traffic.tsx", "Couldn't load traffic data.", "void q.refetch()");
    expectRetryLine(SCREENS + "Providers.tsx", "Couldn't load agents.", "void load()");
    expectRetryLine(SCREENS + "Ide.tsx", "Couldn't load IDE settings.", "void load()");
    // Your phone's sentence is state; its Retry sits right after it.
    const mobile = flat(SCREENS + "Mobile.tsx");
    expect(mobile).toContain(`setError("Couldn't load the phone links.");`);
    expect(mobile).toContain(
      '{error}{" "} <button type="button" className="linklike" onClick={() => void load()}> Retry </button>'
    );
  });

  it("no screen in this package says Could not load any more", () => {
    for (const f of ["SystemLogs", "SharedLink", "Traffic", "Mobile", "Providers", "Ide"]) {
      expect(flat(SCREENS + f + ".tsx")).not.toContain("Could not load");
    }
  });

  it("System logs shows the last good text, never the error in its place", () => {
    expect(logViewText(null, false, false)).toBe("Loading…");
    expect(logViewText(null, false, true)).toBe("");
    expect(logViewText("line 1\n", true, true)).toBe("line 1\n");
    expect(logViewText("", true, false)).toBe("(log is empty)");
    expect(logViewText("", false, false)).toBe("(no log file yet)");
  });

  it("Copy log is off until there is a log, and copies only real text", () => {
    const src = flat(SCREENS + "SystemLogs.tsx");
    expect(src).toContain("disabled={!text} onClick={copyLog}");
    expect(src).toContain("setText(d.text || \"\");");
    expect(src).not.toContain("setText(\"Could not load logs.\")");
  });
});

describe("#29 Ctrl+F reaches Search settings", () => {
  it("the box carries the hint and answers Ctrl/Cmd+F", () => {
    const html = renderToStaticMarkup(createElement(SettingsSearch, { onPick: () => undefined }));
    expect(SEARCH_PLACEHOLDER).toContain("( Ctrl+F )");
    expect(html).toContain(`placeholder="${SEARCH_PLACEHOLDER}"`);
    const k = (key: string, mods: Partial<{ ctrlKey: boolean; metaKey: boolean; altKey: boolean }>) =>
      isFindKey({ key, ctrlKey: false, metaKey: false, altKey: false, ...mods });
    expect(k("f", { ctrlKey: true })).toBe(true);
    expect(k("F", { metaKey: true })).toBe(true);
    expect(k("f", {})).toBe(false);
    expect(k("f", { ctrlKey: true, altKey: true })).toBe(false);
    expect(k("g", { ctrlKey: true })).toBe(false);
  });

  it("the handler is capture-phase and focuses the box", () => {
    const src = flat("components/settings/SettingsSearch.tsx");
    expect(src).toContain('document.addEventListener("keydown", onKey, true);');
    expect(src).toContain("e.preventDefault(); e.stopPropagation(); inputRef.current?.focus(); inputRef.current?.select();");
  });
});

describe("#30 Settings polls skip hidden ticks", () => {
  it("shared link, guest links and the join request return when hidden", () => {
    expect(flat(SCREENS + "SharedLink.tsx")).toContain(
      "window.setInterval(() => { if (document.hidden) return; onChanged(); }, 60_000);"
    );
    expect(flat(SCREENS + "peer/GuestLinks.tsx")).toContain(
      "setInterval(() => { if (document.hidden) return; void load(); }, 2000);"
    );
    expect(flat(SCREENS + "Devices.tsx")).toContain(
      'setInterval(async () => { if (document.hidden) return; try { const j = await api<FleetJoin>("/api/fleet/request");'
    );
  });
});
