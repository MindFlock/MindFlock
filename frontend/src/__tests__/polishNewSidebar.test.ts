/** Touch-ups to the New session dialog and the sidebar: the filter keeps its
 * spaces, a taken name blocks Start, names stop at the server's cap, the
 * folder browser speaks in sentences, the bulk bar's Escape / Delete missing /
 * Delete question / failure reasons, and a row's Close / Delete in flight. */

/// <reference types="vite/client" />
import { describe, expect, it, vi } from "vitest";
import type { Instance } from "../api/types";
import { has, normCode } from "./sourceText";
import RENAME_PY from "../../../backend/web/core/rename.py?raw";

/** A Map-backed localStorage, installed BEFORE the store module loads. */
const storage = new Map<string, string>();
vi.stubGlobal("localStorage", {
  getItem: (k: string) => (storage.has(k) ? storage.get(k)! : null),
  setItem: (k: string, v: string) => void storage.set(k, String(v)),
  removeItem: (k: string) => void storage.delete(k),
  clear: () => storage.clear(),
});

const { useUi } = await import("../state/store");
const { filterQuery, matchesFilter } = await import("../components/sidebar/ordering");
const { MAX_TITLE_LEN, deleteConfirmText, deleteManyConfirmText, whileEnding } = await import(
  "../lib/sessionActions"
);
const { folderErrorText, osErrorText } = await import("../components/dialogs/newSession/folderErrors");
const { bulkEscape, bulkFailText, shortReason } = await import("../components/sidebar/BulkBar");
const { endingLabel, withEnding } = await import("../components/sidebar/SessionMenu");
const { nameClashText } = await import("../components/dialogs/newSession/WorkspaceFold");

const inst = (o: Partial<Instance>): Instance => o as unknown as Instance;

describe("the sidebar filter keeps what was typed", () => {
  it("stores the raw text (spaces and case), whitespace-only as empty", () => {
    useUi.getState().setFilter("Fix bug");
    expect(useUi.getState().filter).toBe("Fix bug");
    useUi.getState().setFilter("fix ");
    expect(useUi.getState().filter).toBe("fix ");
    useUi.getState().setFilter("   ");
    expect(useUi.getState().filter).toBe("");
    useUi.getState().setFilter("");
  });

  it("trims and lowercases only when matching", () => {
    expect(filterQuery("  Fix Bug ")).toBe("fix bug");
    const row = inst({ title: "s1", branch: "me/fix-bug" });
    expect(matchesFilter(row, "fix bug", { s1: "Fix bug in login" })).toBe(true);
    expect(matchesFilter(row, " FIX-BUG ", {})).toBe(true);
    expect(matchesFilter(row, "   ", {})).toBe(true);
    // Each word on its own, like Recently closed: "fix bug" finds me/fix-bug.
    expect(matchesFilter(row, "fix bug", {})).toBe(true);
    expect(matchesFilter(row, "fix login", {})).toBe(false);
  });

  it("the box hands its text over untrimmed; open windows match the same way", () => {
    const box = normCode("components/sidebar/SessionFilter.tsx");
    expect(has(box, "onChange={(e) => setFilter(e.target.value)}")).toBe(true);
    expect(box).not.toContain("setFilter(e.target.value.trim())");
    expect(has(normCode("components/sidebar/Sidebar.tsx"), "windows.filter((w) => matchesWindowFilter(w.title, ui.filter))")).toBe(true);
  });
});

describe("a taken name blocks Start", () => {
  it("is part of startBlocked, worded as the clash line", () => {
    const dlg = normCode("components/dialogs/NewSessionDialog.tsx");
    expect(has(dlg, ": planned.clash ? nameClashText(planned.title)")).toBe(true);
    // The line under the branch says the same sentence.
    expect(has(normCode("components/dialogs/newSession/WorkspaceFold.tsx"), "{nameClashText(name.title)}")).toBe(true);
    expect(nameClashText("fix")).toBe(
      "A session called “fix” already exists — give this one another name under More options, or clear it to name it from the task."
    );
  });
});

describe("names stop at the server's cap", () => {
  it("is the server's MAX_TITLE_LEN", () => {
    expect(RENAME_PY as string).toMatch(new RegExp("^MAX_TITLE_LEN = " + MAX_TITLE_LEN + "$", "m"));
  });

  it("caps the New dialog's Name box and the rail's rename box", () => {
    expect(has(normCode("components/dialogs/NewSessionDialog.tsx"), 'id="new-title" ref={titleRef} autoComplete="off" maxLength={MAX_TITLE_LEN}')).toBe(true);
    expect(has(normCode("components/sidebar/SidebarRow.tsx"), 'className="title title-edit" type="text" maxLength={MAX_TITLE_LEN}')).toBe(true);
  });
});

describe("folder errors read as sentences", () => {
  it("strips the Python dressing", () => {
    expect(osErrorText("could not create folder: [Errno 28] No space left on device: '/x/y'")).toBe(
      "No space left on device"
    );
  });

  it("says permission, missing parent, taken name and bad name plainly", () => {
    const perm = "could not create folder: [Errno 13] Permission denied: '/x/y'";
    expect(folderErrorText(perm, "browser")).toBe(
      "You don't have permission to create a folder there — pick another place."
    );
    expect(folderErrorText("permission denied: /root", "list")).toBe("You don't have permission to open that folder.");
    expect(folderErrorText("not a directory: /x", "list")).toBe("That folder doesn't exist anymore — pick another one.");
    expect(folderErrorText("already exists: /x/y", "browser")).toBe(
      "Something called that is already here — give it another name."
    );
    expect(folderErrorText("folder name must not contain a path", "browser")).toBe(
      "Use a single folder name, without / or \\."
    );
    expect(folderErrorText("could not create folder: [Errno 30] Read-only file system: '/x'", "browser")).toBe(
      "Couldn't create it: Read-only file system"
    );
    for (const t of ["browser", "list", "field"] as const) expect(folderErrorText(perm, t)).not.toMatch(/Errno|'\//);
  });

  it("keeps the dialog's own Create-this-folder sentences", () => {
    expect(folderErrorText("not a directory: /x", "field")).toBe(
      "The folder it would go in doesn't exist either — use Browse… to pick a place."
    );
    expect(folderErrorText("already exists: /x", "field")).toBe("Something is already there — pick it with Browse….");
  });

  it("is what the browser and the dialog show", () => {
    const fb = normCode("components/dialogs/newSession/FolderBrowser.tsx");
    expect(has(fb, 'setError(folderErrorText((err as Error).message, "list"));')).toBe(true);
    expect(has(fb, 'setError(folderErrorText((err as Error).message, "browser"));')).toBe(true);
    expect(fb).not.toContain("setError((err as Error).message)");
    expect(has(normCode("components/dialogs/NewSessionDialog.tsx"), 'setMkError(folderErrorText(errMsg(err), "field"));')).toBe(true);
  });
});

describe("the bulk bar", () => {
  const BULK = normCode("components/sidebar/BulkBar.tsx");

  it("Escape answers an open question first, then leaves select mode", () => {
    expect(bulkEscape("delete")).toBe("cancel-ask");
    expect(bulkEscape("cleanmissing")).toBe("cancel-ask");
    expect(bulkEscape(null)).toBe("leave");
    expect(has(BULK, 'if (bulkEscape(ask) === "leave") useUi.getState().setBulkMode(false);')).toBe(true);
    expect(has(BULK, "}, [bulkMode, ask]);")).toBe(true);
  });

  it("asks before Delete missing", () => {
    expect(has(BULK, 'onClick={() => setAsk("cleanmissing")}')).toBe(true);
    expect(BULK).not.toContain('onClick={() => void run("cleanmissing")}');
    expect(BULK).toContain("whose folder is gone? This removes the dead sessions.");
  });

  it("asks Delete with the row's wording: uncommitted work goes, in-place keeps its folder", () => {
    expect(deleteManyConfirmText([{ branch: "me/x" }])).toBe(deleteConfirmText({ branch: "me/x" }));
    expect(deleteManyConfirmText([{}, {}])).toBe(
      "Deletes their folders and branches, including uncommitted changes. This can't be undone — use Close to keep them."
    );
    expect(deleteManyConfirmText([{}, { in_place: true }])).toContain("(the 1 working in place keep theirs)");
    expect(deleteManyConfirmText([{ in_place: true }, { in_place: true }])).toBe(
      "Removes these sessions. Their folders and branches stay as they are; Close keeps them to reopen."
    );
    expect(BULK).not.toContain("Removes their worktrees and branches.`");
    expect(has(BULK, "deleteManyConfirmText(rows.filter((i) => selected.includes(i.title)))")).toBe(true);
  });

  it("names each failure with its reason, shortened", () => {
    expect(shortReason("git worktree remove failed\nfatal: something long")).toBe("git worktree remove failed");
    expect(shortReason("x".repeat(200)).length).toBe(80);
    expect(shortReason("x".repeat(200)).endsWith("…")).toBe(true);
    expect(bulkFailText([
      { name: "a", why: "boom" },
      { name: "b", why: "HTTP 500" },
    ])).toBe("a — boom; b — HTTP 500");
    expect(has(BULK, "failed.push({ name: displayName(t), why: errMsg(err) });")).toBe(true);
  });
});

describe("a row's Close / Delete in flight", () => {
  const model = {
    groups: [
      { id: "work" as const, label: "Work", items: [{ key: "open", label: "Open", run: () => undefined }] },
      {
        id: "danger" as const,
        label: "",
        items: [
          { key: "close", label: "✕ Close", run: () => undefined },
          { key: "delete", label: "Delete…", danger: true, confirm: "sure?", run: () => undefined },
        ],
      },
    ],
    more: [],
  };

  it("greys both ends in the menu, saying why", () => {
    expect(withEnding(model, undefined)).toBe(model);
    const m = withEnding(model, "deleting");
    const ends = m.groups.find((g) => g.id === "danger")!.items;
    expect(ends.every((it) => it.disabled && it.note === "Deleting…" && !it.confirm)).toBe(true);
    expect(m.groups[0].items[0].disabled).toBeFalsy();
    expect(endingLabel("closing")).toBe("Closing…");
  });

  it("marks the row while the request runs, refuses a second, clears when it settles", async () => {
    let release!: () => void;
    const gate = new Promise<void>((r) => (release = r));
    let runs = 0;
    const first = whileEnding("s1", "deleting", async () => {
      runs++;
      await gate;
      return "done";
    });
    expect(useUi.getState().ending.s1).toBe("deleting");
    expect(await whileEnding("s1", "closing", async () => (runs++, "again"))).toBeUndefined();
    expect(runs).toBe(1);
    release();
    expect(await first).toBe("done");
    expect(useUi.getState().ending.s1).toBeUndefined();
    // A failure clears it too.
    await expect(whileEnding("s1", "closing", async () => Promise.reject(new Error("x")))).rejects.toThrow("x");
    expect(useUi.getState().ending.s1).toBeUndefined();
  });

  it("close and delete go through it; the row shows it and turns its ✕ off", () => {
    const acts = normCode("lib/sessionActions.ts");
    expect(has(acts, 'await whileEnding(title, "closing", () => closeNow(title));')).toBe(true);
    expect(has(acts, 'return (await whileEnding(title, "deleting", () => deleteNow(title))) ?? null;')).toBe(true);
    const row = normCode("components/sidebar/SidebarRow.tsx");
    expect(has(row, '{ending ? ( <span className="stagechip chip-ending" role="status"> {endingLabel(ending)} </span>')).toBe(true);
    expect(has(row, "disabled={!!ending}")).toBe(true);
    expect(has(row, "ending={ending}")).toBe(true);
    expect(has(normCode("components/sidebar/SessionMenu.tsx"), "const model = withEnding(given, ending);")).toBe(true);
  });
});
