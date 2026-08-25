import { describe, expect, it } from "vitest";
import {
  FOLDER_INIT,
  focusRowIndex,
  folderReducer,
  homeRelative,
  isNameQuery,
  looksLikePath,
  mayTakeOpeningFocus,
  type FolderAction,
  type FolderState,
} from "../components/dialogs/NewSessionDialog";

/** Replays a sequence of gestures and answers the way the dialog dispatches
 * them, so each trace below reads in the order the user lived it. */
function run(state: FolderState, ...actions: FolderAction[]): FolderState {
  return actions.reduce(folderReducer, state);
}

/** A freshly opened dialog whose suggestion has landed: the ordinary case, and
 * the one with something worth losing in the Folder field. */
const prefilled = run(
  FOLDER_INIT,
  { t: "reopen" },
  { t: "suggested", path: "/home/me/code/myrepo" }
);

describe("folderReducer — browsing, and cancelling a browse", () => {
  it("shows each selected row in the field, so the highlight and the git nudge follow it", () => {
    const s = run(
      prefilled,
      { t: "browse-open" },
      { t: "browse-select", path: "/home/me/Downloads" }
    );
    expect(s.path).toBe("/home/me/Downloads");
    expect(s.browsing).toBe(true);
  });

  it("puts the folder back when the user escapes out of a tree they were only checking", () => {
    // Browse to double-check something, walk into the wrong tree, Escape. The
    // field kept whichever directory had been passed through last, and Create
    // sent that as repo_path.
    const s = run(
      prefilled,
      { t: "browse-open" },
      { t: "browse-select", path: "/home/me/Downloads" },
      { t: "browse-select", path: "/home/me/Downloads/tmp" },
      { t: "browse-cancel" }
    );
    expect(s.path).toBe("/home/me/code/myrepo");
    expect(s.browsing).toBe(false);
    expect(s.undo).toBeNull();
  });

  it("hands a cancelled field back to a suggestion that was still walking", () => {
    const onHome = run(FOLDER_INIT, { t: "reopen" }, { t: "fallback", path: "/home/me" });
    const s = run(
      onHome,
      { t: "browse-open" },
      { t: "browse-select", path: "/home/me/Downloads" },
      { t: "browse-cancel" },
      { t: "suggested", path: "/home/me/code/myrepo" }
    );
    expect(s.path).toBe("/home/me/code/myrepo");
  });

  it("never escapes back to the empty field a first-ever open started from", () => {
    // Browse… is most tempting while the field is still blank, so the snapshot
    // Escape restores was "". A suggestion landing mid-browse has to move that
    // baseline, or cancelling empties the field and Create answers "a folder is
    // required" — after submit() has already closed the dialog.
    const s = run(
      FOLDER_INIT,
      { t: "reopen" },
      { t: "browse-open" },
      { t: "suggested", path: "/home/me/code/myrepo" },
      { t: "browse-select", path: "/home/me/Downloads" },
      { t: "browse-cancel" }
    );
    expect(s.path).toBe("/home/me/code/myrepo");
    expect(s.touched).toBe(false);
  });

  it("still prefers a hand-typed folder over a pre-fill when a browse is cancelled", () => {
    const s = run(
      FOLDER_INIT,
      { t: "reopen" },
      { t: "user-set", path: "/srv/work/project" },
      { t: "browse-open" },
      { t: "suggested", path: "/home/me/code/myrepo" },
      { t: "browse-select", path: "/home/me/Downloads" },
      { t: "browse-cancel" }
    );
    expect(s.path).toBe("/srv/work/project");
  });

  it("keeps the folder when the browse is finished deliberately", () => {
    // "use this folder", a folder just created, or the panel toggled shut.
    const s = run(prefilled, { t: "browse-open" }, { t: "browse-commit", path: "/srv/work" });
    expect(s).toEqual({ path: "/srv/work", browsing: false, undo: null, touched: true });
  });

  it("does not swallow a path typed while the browser was open", () => {
    // Escape cancels the browsing; the typing is not part of the browse.
    const s = run(
      prefilled,
      { t: "browse-open" },
      { t: "browse-select", path: "/home/me/Downloads" },
      { t: "user-set", path: "/srv/work/project" },
      { t: "browse-cancel" }
    );
    expect(s.path).toBe("/srv/work/project");
  });
});

describe("folderReducer — the pre-fill racing the user", () => {
  it("never replaces a path typed while the filesystem walk was still running", () => {
    // /api/repos/suggest does hundreds of listdir/stat probes under $HOME plus a
    // git rev-parse per survivor, and the dialog is typeable throughout.
    const s = run(
      FOLDER_INIT,
      { t: "reopen" },
      { t: "user-set", path: "/srv/work/project" },
      { t: "suggested", path: "/home/me/nearby" },
      { t: "fallback", path: "/home/me" }
    );
    expect(s.path).toBe("/srv/work/project");
  });

  it("never replaces a folder chosen from a chip, a template or the browser", () => {
    const chosen: FolderAction[] = [
      { t: "user-set", path: "/srv/work/project" },
      { t: "browse-select", path: "/srv/work/project" },
      { t: "browse-commit", path: "/srv/work/project" },
    ];
    for (const a of chosen)
      expect(
        run(FOLDER_INIT, { t: "reopen" }, a, { t: "suggested", path: "/home/me/nearby" }).path
      ).toBe("/srv/work/project");
  });

  it("still fills a field the user has emptied — there is nothing there to protect", () => {
    const s = run(
      FOLDER_INIT,
      { t: "reopen" },
      { t: "user-set", path: "" },
      { t: "suggested", path: "/home/me/code/myrepo" }
    );
    expect(s.path).toBe("/home/me/code/myrepo");
  });

  it("prefers the suggestion to $HOME whichever order the two requests land in", () => {
    const suggestFirst = run(
      FOLDER_INIT,
      { t: "reopen" },
      { t: "suggested", path: "/home/me/code/myrepo" },
      { t: "fallback", path: "/home/me" }
    );
    const homeFirst = run(
      FOLDER_INIT,
      { t: "reopen" },
      { t: "fallback", path: "/home/me" },
      { t: "suggested", path: "/home/me/code/myrepo" }
    );
    expect(suggestFirst.path).toBe("/home/me/code/myrepo");
    expect(homeFirst.path).toBe("/home/me/code/myrepo");
  });

  it("clears nothing when an endpoint has no folder to offer", () => {
    const s = run(
      FOLDER_INIT,
      { t: "reopen" },
      { t: "fallback", path: "/home/me" },
      { t: "suggested", path: "" }
    );
    expect(s.path).toBe("/home/me");
  });

  it("keeps the last opening's folder as a placeholder, but not its touches", () => {
    const lastTime = run(
      FOLDER_INIT,
      { t: "user-set", path: "/srv/work/project" },
      { t: "browse-open" }
    );
    const s = run(lastTime, { t: "reopen" });
    expect(s).toEqual({ path: "/srv/work/project", browsing: false, undo: null, touched: false });
    // Which is what leaves this opening's own suggestion free to improve on it.
    expect(run(s, { t: "suggested", path: "/home/me/code/myrepo" }).path).toBe(
      "/home/me/code/myrepo"
    );
  });
});

describe("looksLikePath — the one predicate that keeps a combobox a path field", () => {
  it("leaves an absolute path, or a ~ path, on the old behaviour", () => {
    // These never reach /api/repos/search: they go to the check_repo probe and
    // its status line, exactly as they did before the field could search.
    expect(looksLikePath("/home/me/code/api")).toBe(true);
    expect(looksLikePath("~/code/api")).toBe(true);
    expect(looksLikePath("  /srv/work  ")).toBe(true); // a stray space is not a name
  });

  it("treats anything else as a name to look up", () => {
    expect(looksLikePath("api")).toBe(false);
    expect(looksLikePath("acme/api")).toBe(false); // a fragment, not a location
    expect(looksLikePath("")).toBe(false);
  });

  it("switches back and forth with the leading character, so no mode can stick", () => {
    // Re-read from the text every keystroke: deleting the slash turns a path
    // back into a search, typing one turns it back again.
    expect(looksLikePath("/api")).toBe(true);
    expect(looksLikePath("api")).toBe(false);
  });
});

describe("isNameQuery — the text Create must not treat as a folder", () => {
  it("calls a bare name what it is: a query, not a location", () => {
    // The hazard this exists for: Create sends the field verbatim, and the
    // server resolves a bare name against its OWN working directory and then
    // creates it — so "api" typed, never picked, and submitted made a
    // MindFlock/api directory and started a session in it, while "backend"
    // would have found the server's own source tree.
    expect(isNameQuery("api")).toBe(true);
    expect(isNameQuery("notathing")).toBe(true);
    expect(isNameQuery("acme/api")).toBe(true); // a fragment is still not a path
  });

  it("refuses relative paths too, which resolve against the server and not the user", () => {
    expect(isNameQuery("./foo")).toBe(true);
    expect(isNameQuery("$HOME/code")).toBe(true);
  });

  it("passes a real path straight through, so the field is the path field it was", () => {
    expect(isNameQuery("/home/me/code/api")).toBe(false);
    expect(isNameQuery("~/code/api")).toBe(false);
    expect(isNameQuery("  /srv/work  ")).toBe(false);
  });

  it("says nothing about an empty field, which Create already answers for itself", () => {
    // An empty folder is "a folder is required", not "that is a search term",
    // and Enter in an empty field must keep submitting the way it always did.
    expect(isNameQuery("")).toBe(false);
    expect(isNameQuery("   ")).toBe(false);
  });
});

describe("homeRelative — where a match is, said quietly", () => {
  it("shortens the $HOME prefix every match shares to a ~", () => {
    expect(homeRelative("/home/me/code/acme/api", "/home/me")).toBe("~/code/acme/api");
  });

  it("says ~ for home itself rather than an empty line", () => {
    expect(homeRelative("/home/me", "/home/me")).toBe("~");
  });

  it("only shortens at a path boundary, so a sibling of home keeps its full path", () => {
    // /home/mementos is not inside /home/me, and "~mentos" would be a lie.
    expect(homeRelative("/home/mementos/code", "/home/me")).toBe("/home/mementos/code");
  });

  it("leaves a path from somewhere else alone", () => {
    expect(homeRelative("/srv/work/project", "/home/me")).toBe("/srv/work/project");
    expect(homeRelative("/home/me/code", "")).toBe("/home/me/code");
  });
});

describe("mayTakeOpeningFocus", () => {
  it("takes the caret from whatever opened the dialog", () => {
    // The New button, the command palette's input as it unmounts, the terminal.
    expect(mayTakeOpeningFocus({ activeIsTarget: false, activeInsideDialog: false })).toBe(true);
  });

  it("leaves a field inside the dialog that the user reached first", () => {
    // The bug it exists for: the caret in Prompt, mid-sentence.
    expect(mayTakeOpeningFocus({ activeIsTarget: false, activeInsideDialog: true })).toBe(false);
  });

  it("is a harmless no-op once the Name box already has the caret", () => {
    expect(mayTakeOpeningFocus({ activeIsTarget: true, activeInsideDialog: true })).toBe(true);
  });
});

describe("focusRowIndex — where a keyboard navigation lands", () => {
  it("lands on the folder it stepped out of, so ← walks back out the way it came in", () => {
    const home = ["/home/me/code", "/home/me/Downloads", "/home/me/notes"];
    expect(focusRowIndex(home, "/home/me/Downloads")).toBe(1);
  });

  it("starts at the top when stepping in, where the folder left behind isn't listed", () => {
    const code = ["/home/me/code/src", "/home/me/code/tests"];
    expect(focusRowIndex(code, "/home/me/code")).toBe(0);
  });

  it("has no row to give in an empty folder, which sends focus to “use this folder”", () => {
    expect(focusRowIndex([], "/home/me/code")).toBe(-1);
  });
});
