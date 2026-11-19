/** UI sweep P5 — Settings and Intake.
 *
 *  - #12 Advanced: the auto-update switch flips back (with the reason) when
 *    the change failed, and a failed engine install() ends the busy state.
 *  - #17 Connections skips its poll while the window is hidden.
 *  - #29 Intake records the touched control like Settings, so a save shows
 *    the inline "Saved" cue; the saves that only toasted now cue first.
 *  - #30 "Settings → X" and "Customize" in a sentence are links. */

import { describe, expect, it } from "vitest";
import { code, squash } from "./sourceText";
import { failSentence } from "../components/settings/screens/Advanced";
import { DEVICES_LINK, splitAtLink, stalledText } from "../components/intake/QueueTab";
import { openCustomize } from "../components/settings/screens/Extensions";
import { useUi } from "../state/store";

const flat = (path: string) => squash(code(path));

describe("#12 Engine updates never claim a change that failed", () => {
  it("failSentence carries the reason when there is one", () => {
    expect(failSentence("Couldn't change automatic updates", new Error("disk is read-only."))).toBe(
      "Couldn't change automatic updates: disk is read-only."
    );
    expect(failSentence("The update didn't start", null)).toBe("The update didn't start.");
    expect(failSentence("The update didn't start", "no network")).toBe("The update didn't start: no network.");
  });

  it("a setAuto failure flips the switch back and toasts why", () => {
    const src = flat("components/settings/screens/Advanced.tsx");
    expect(src).not.toContain(".catch(() => {}); }; const update");
    expect(src).toContain("setInfo((i) => ({ ...(i || {}), autoUpdate: !on }));");
    expect(src).toContain('toast(failSentence("Couldn\'t change automatic updates", err));');
  });

  it("a rejected install() stops the poll, clears busy and says failed", () => {
    const src = flat("components/settings/screens/Advanced.tsx");
    expect(src).not.toContain("mfengine.install().catch(() => {});");
    expect(src).toContain(
      "mfengine.install().catch((err: unknown) => { if (timer.current) clearInterval(timer.current); " +
        "timer.current = null; setBusy(false); " +
        'setRun({ state: "failed", message: failSentence("The update didn\'t start", err) }); });'
    );
  });
});

describe("#17 Connections skips hidden ticks", () => {
  it("returns before loading when document.hidden", () => {
    expect(flat("components/settings/screens/Connections.tsx")).toContain(
      "window.setInterval(() => { if (document.hidden) return; void load(false); }, REFRESH_MS);"
    );
  });
});

describe("#29 the inline Saved cue, Intake included", () => {
  it("Intake notes the touched control the way Settings does", () => {
    const src = flat("components/intake/IntakeDialog.tsx");
    expect(src).toContain('import { noteTouched } from "../settings/savedCue";');
    expect(src).toContain(
      'id="intake-body" onChangeCapture={(e) => noteTouched(e.target)} onBlurCapture={(e) => noteTouched(e.target)}'
    );
  });

  it("the Ignore-comments lists cue their own row, toast only as a fallback", () => {
    for (const [file, id] of [
      ["components/intake/PullRequestsTab.tsx", "gh-skip-authors"],
      ["components/intake/IssuesTab.tsx", "gh-issue-skip-authors"],
    ]) {
      const src = flat(file);
      expect(src).not.toContain(', "Saved")}');
      expect(src).toContain('if (await s.saveGroup("github", patch)) { if (!flashSaved(document.getElementById(fieldId))) toast("Saved"); }');
      expect(src).toContain(`list }, "${id}")}`);
    }
  });

  it("the five Settings saves that only toasted cue first", () => {
    expect(flat("components/settings/screens/CodingCli.tsx")).toContain(
      'if (!flashSaved(from ?? document.getElementById("default-launch-args-input"))) toast("Saved launch flags");'
    );
    expect(flat("components/settings/screens/General.tsx")).toContain(
      'if (!flashSaved(document.getElementById("scroll-speed")))'
    );
    expect(flat("components/settings/screens/Accounts.tsx")).toContain(
      'if (!(from && flashSaved(from))) toast("Accounts saved");'
    );
    expect(flat("components/settings/screens/Extensions.tsx")).toContain(
      'if (await s.saveGroup("extensions", { disabled: next })) { if (!flashSaved(from)) toast('
    );
    expect(flat("components/settings/screens/RepoBranches.tsx")).toContain(
      'if (ok && !flashSaved(document.getElementById("workspace-repo-branches"))) toast("Saved per-repo branches");'
    );
  });

  it("a failed save says the server's reason", () => {
    expect(flat("components/settings/screens/CodingCli.tsx")).toContain(
      'toast("Save failed: " + ((err as Error)?.message || "launch flags"));'
    );
    expect(flat("components/settings/screens/General.tsx")).toContain(
      'toast("Scroll speed change failed" + (why ? ": " + why : ""));'
    );
  });

  it("Notifications never saves in silence", () => {
    const src = flat("components/settings/screens/Notifications.tsx");
    expect(src).toContain('if (!from || !flashSaved(from)) toast("Saved");');
    expect(src).not.toContain("if (from) flashSaved(from);");
  });
});

describe("#30 the pointers in a sentence are links", () => {
  it("the stalled sentence keeps its words and cuts around Your devices", () => {
    const s = { label: "Tickets", switchName: "Tickets", runsOn: "Mac", broken: "", state: "elsewhere" as const };
    const text = stalledText(s, true);
    const cut = splitAtLink(text, DEVICES_LINK);
    expect(cut).not.toBeNull();
    expect(cut![0] + DEVICES_LINK + cut![1]).toBe(text);
    expect(cut![0]).toMatch(/choose Run here in $/);
    expect(splitAtLink("no link here", DEVICES_LINK)).toBeNull();
    expect(flat("components/intake/QueueTab.tsx")).toContain(
      '<button type="button" className="linklike" onClick={() => openSettings("devices")}> {DEVICES_LINK} </button>'
    );
  });

  it("Workspace and Connections open their Settings screens", () => {
    expect(flat("components/intake/AutoStartSwitches.tsx")).toContain(
      'onClick={() => openSettings("workspace")}> Settings › Workspace </button>'
    );
    expect(flat("components/intake/TicketsTab.tsx")).toContain(
      'onClick={() => openSettings("connections")}> Settings → Connections → GitHub → Connect </button>'
    );
  });

  it("Customize opens the Customize dialog on the sidebar page", () => {
    useUi.setState({ customizePage: "pane", openDialog: null });
    openCustomize();
    expect(useUi.getState().customizePage).toBe("sidebar");
    expect(useUi.getState().openDialog).toBe("customize");
    expect(flat("components/settings/screens/Extensions.tsx")).toContain(
      'className="linklike" id="ext-open-customize" onClick={openCustomize}> Customize </button>'
    );
    expect(flat("components/settings/screens/Extensions.tsx")).not.toContain("footer Customize");
  });
});
