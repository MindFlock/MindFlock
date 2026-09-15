import { describe, expect, it } from "vitest";
import {
  FOLDER_INIT,
  PLAN_FOLDER_NONE,
  cancelPlanRun,
  describeBlockReason,
  focusRowIndex,
  folderReducer,
  homeRelative,
  isNameQuery,
  looksLikePath,
  mayTakeOpeningFocus,
  newFolderBlockReason,
  newFolderGate,
  planFolderReducer,
  planGatePath,
  planInPlace,
  planNoteFor,
  provisionBlockReason,
  startPlanRun,
  submitHoldReason,
  immediateStartBlockReason,
  worktreeClampReason,
  type FolderAction,
  type FolderState,
  type PlanFolder,
  type PlanRun,
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

describe("provisionBlockReason — the create the server is certain to refuse", () => {
  /** The folder that started this: a plain parent directory whose repo is one
   * level further down (~/MindFlock, holding ~/MindFlock/app). */
  const plainParent = {
    provision: true,
    plainFolder: true,
    initRepo: false,
    folderPath: "/home/me/MindFlock",
  };

  it("names the folder, because the aside that would have is three screens up", () => {
    const reason = provisionBlockReason(plainParent);
    expect(reason).toContain("/home/me/MindFlock");
    expect(reason).toContain("needs a git repo");
  });

  it("says nothing about a repo — the ordinary provisioned create", () => {
    expect(provisionBlockReason({ ...plainParent, plainFolder: false })).toBe("");
  });

  it("says nothing when the folder is about to BECOME a repo", () => {
    // "Create a git repo in this folder" runs git init + an initial commit
    // first, so by the time provisioning looks there is a repo there.
    expect(provisionBlockReason({ ...plainParent, initRepo: true })).toBe("");
  });

  it("leaves a plain session in a plain folder alone, which is a legal thing to want", () => {
    expect(provisionBlockReason({ ...plainParent, provision: false })).toBe("");
  });
});

describe("describeBlockReason — the sentence that isn't worth a model turn", () => {
  it("asks for a sentence when the box is empty, rather than sending nothing", () => {
    // Enter in an empty box costs a 75s budget and a subprocess to be told the
    // model has no idea what was wanted. A line under the box says it for free.
    expect(describeBlockReason({ text: "", busy: false })).toBe(
      "Type what you want to work on first."
    );
    // Whitespace is an empty box that looks typed-in; it is trimmed first.
    expect(describeBlockReason({ text: "   ", busy: false })).toBe(
      "Type what you want to work on first."
    );
  });

  it("quotes a one-word sentence back and says what it is missing", () => {
    // The hazard this floor exists for: search_repos("scan") resolves to exactly
    // one folder on this machine — EfficientRescan — and it is the wrong one, so
    // a one-word box buys a 25-second wait and a folder the user has to undo.
    // Naming what was typed is what makes the refusal repairable.
    const reason = describeBlockReason({ text: "scan", busy: false });
    expect(reason).toBe("Say a bit more — “scan” doesn't say which project or what to do.");
    // Trimmed before it is quoted, so the aside never shows stray spaces.
    expect(describeBlockReason({ text: "  fix bug  ", busy: false })).toContain("“fix bug”");
  });

  it("lets a real sentence through, and anything from the floor up", () => {
    expect(describeBlockReason({ text: "fix the auth bug in sitecheck-bot6", busy: false })).toBe(
      ""
    );
    // The floor is 8 characters, counted after the trim: 7 is refused, 8 goes.
    expect(describeBlockReason({ text: "fix bug", busy: false })).not.toBe("");
    expect(describeBlockReason({ text: "fix bugs", busy: false })).toBe("");
  });

  it("says nothing at all while a turn is in flight, because the button already is", () => {
    // The button reads "Reading…" with its ring; a second line under it saying
    // the box is empty (it is readOnly, so it cannot have changed) would be the
    // dialog talking over itself.
    expect(describeBlockReason({ text: "", busy: true })).toBe("");
    expect(describeBlockReason({ text: "scan", busy: true })).toBe("");
  });
});

describe("submitHoldReason — the Enter that landed on a form nobody has read", () => {
  /** The instant a fill landed and moved the caret into the Folder field. */
  const filledAt = 1_700_000_000_000;
  /** submitArmAt.current, set to Date.now() + SUBMIT_ARM_MS by runDescribe. */
  const armAt = filledAt + 500;

  it("holds Create for the half second after a fill lands, and says why", () => {
    // The answer arrives 10-25s after the keystroke that asked for it, by which
    // time the user's hand may be back on Enter for an unrelated reason — and
    // submit() closes the dialog optimistically before the POST, so an Enter
    // that gets through leaves nothing on screen to cancel.
    const held = submitHoldReason(armAt, filledAt + 120);
    expect(held).toBe("Just filled the form in — check the folder, then press Create.");
    // It explains itself rather than swallowing the key.
    expect(held).toContain("check the folder");
  });

  it("lets Create through the moment the window has passed", () => {
    expect(submitHoldReason(armAt, armAt)).toBe("");
    expect(submitHoldReason(armAt, armAt + 1)).toBe("");
    expect(submitHoldReason(armAt, filledAt + 9000)).toBe("");
  });

  it("never holds a form the user filled in by hand", () => {
    // Ctrl+N, type a name, Enter: nothing was ever filled in for them, so armAt
    // is still 0 and the oldest muscle memory in the dialog keeps working.
    expect(submitHoldReason(0, filledAt)).toBe("");
    expect(submitHoldReason(0, 0)).toBe("");
  });
});

describe("planInPlace — the absent key that would cut a branch", () => {
  it("honours an explicit false as the worktree request it is", () => {
    expect(planInPlace({ in_place: false })).toBe(false);
  });

  it("honours an explicit true as working in the folder itself", () => {
    expect(planInPlace({ in_place: true })).toBe(true);
  });

  it("reads a MISSING in_place as the folder, not as a worktree", () => {
    // `!!a.in_place` would read an absent key as false, and false here means
    // "cut a branch and a worktree in somebody's repo". Of the two ways to be
    // wrong about a key that isn't there, only one of them writes to a git repo.
    expect(planInPlace({})).toBe(true);
    expect(planInPlace({ title: "auth-bug", repo_path: "/home/me/code/api" })).toBe(true);
    expect(planInPlace({ in_place: undefined })).toBe(true);
  });
});

describe("newFolderGate — the folder a model proposed making", () => {
  /** The brand-new-project plan: "start a new project called invoice-parser".
   * The server answered `new:invoice-parser`, so folder_exists came back false
   * and nothing is at that path yet. */
  const plan = {
    planPath: "/home/me/code/invoice-parser",
    planDisplay: "~/code/invoice-parser",
  };

  it("asks nothing about a plan whose folder was already there", () => {
    // Every numbered candidate came out of a walk of the real filesystem, so
    // this is every plan but the `new:<name>` one — and the common case. A
    // confirmation people meet on every fill is a confirmation they stop
    // reading before the one fill that mattered.
    expect(newFolderGate({ planPath: "", planDisplay: "", folderPath: "/home/me/code/api" })).toBe(
      ""
    );
  });

  it("asks the moment a fill lands, in the spelling the note used", () => {
    expect(newFolderGate({ ...plan, folderPath: "/home/me/code/invoice-parser" })).toBe(
      "~/code/invoice-parser"
    );
  });

  it("stops asking once the Folder field is edited to anything else", () => {
    // THE BUG THE DERIVATION EXISTS FOR. The field is written from six places —
    // typing, browse select / commit / cancel, a suggestion chip, a match, a
    // template — and a boolean cleared at five of them is a gate that survives
    // onto a folder the plan never proposed, which is the create nobody
    // confirmed. Derived from the field, none of those six has to remember.
    expect(newFolderGate({ ...plan, folderPath: "/home/me/code/api" })).toBe("");
    expect(newFolderGate({ ...plan, folderPath: "/home/me/code/invoice-parser2" })).toBe("");
    // Mid-edit, too: a half-deleted path is not the folder the plan proposed.
    expect(newFolderGate({ ...plan, folderPath: "/home/me/code/invoice-pars" })).toBe("");
    expect(newFolderGate({ ...plan, folderPath: "" })).toBe("");
  });

  it("asks again when the plan's folder is typed back, because that IS the model's folder", () => {
    // Not a leftover: the question is about what Create would make, and Create
    // would make the model's folder again. Undoing an edit has to bring the
    // gate back, or the escape from it is "type anything, then type it back".
    const wandered = newFolderGate({ ...plan, folderPath: "/home/me/code/api" });
    expect(wandered).toBe("");
    expect(newFolderGate({ ...plan, folderPath: "/home/me/code/invoice-parser" })).toBe(
      "~/code/invoice-parser"
    );
  });

  it("ignores the whitespace a paste or a stray space leaves in the field", () => {
    expect(newFolderGate({ ...plan, folderPath: "  /home/me/code/invoice-parser  " })).toBe(
      "~/code/invoice-parser"
    );
  });

  it("falls back to the absolute path rather than letting a missing spelling silence it", () => {
    // folder_display is cosmetic and repo_path is the contract. Of the two ways
    // to be wrong about an empty display, only one of them makes a directory
    // nobody read about — so the question is asked in the ugly spelling.
    expect(
      newFolderGate({
        planPath: "/home/me/code/invoice-parser",
        planDisplay: "",
        folderPath: "/home/me/code/invoice-parser",
      })
    ).toBe("/home/me/code/invoice-parser");
  });
});

describe("newFolderBlockReason — the create that would make a folder nobody agreed to", () => {
  const gate = "~/code/invoice-parser";

  it("refuses while the question is unanswered, naming the folder and the tick", () => {
    // The row is several screens up by the time Create is pressed (this card
    // scrolls), so "confirm the folder first" would be an instruction about a
    // control the reader cannot see.
    const reason = newFolderBlockReason({ gate, confirmed: false });
    expect(reason).toContain("~/code/invoice-parser");
    expect(reason).toContain("Yes, create ~/code/invoice-parser");
  });

  it("lets Create through once the folder has been agreed to in as many words", () => {
    expect(newFolderBlockReason({ gate, confirmed: true })).toBe("");
  });

  it("never blocks a form no plan has touched", () => {
    // Ctrl+N, type a folder, Enter: the user's own folder, which this dialog has
    // always created on Create. Only a folder a MODEL proposed is gated.
    expect(newFolderBlockReason({ gate: "", confirmed: false })).toBe("");
    expect(newFolderBlockReason({ gate: "", confirmed: true })).toBe("");
  });
});

/** The brand-new-project plan, as planFolderReducer stores it: "start a new
 * project called invoice-parser". The menu the model was shown held no such
 * folder, so it answered `new:invoice-parser` and the server came back
 * folder_exists: false. */
const NEW_PLAN = planFolderReducer(PLAN_FOLDER_NONE, {
  t: "answer",
  plan: {
    repo_path: "/home/me/code/invoice-parser",
    folder_display: "~/code/invoice-parser",
    folder_exists: false,
  },
});

/** The gate exactly as the dialog derives it: the plan through planGatePath,
 * the live Folder field, nothing remembered in between. */
function gateFor(plan: PlanFolder, folderPath: string): string {
  return newFolderGate({
    planPath: planGatePath(plan),
    planDisplay: plan.display,
    folderPath,
  });
}

describe("planFolderReducer — the plan's folder across the dialog's lifecycle", () => {
  it("keeps the folder of an EXISTING-repo plan too, and asks nothing about it", () => {
    // The commonest plan there is. The path is kept anyway, because the note
    // above the form needs something to compare itself against; only the gate
    // cares whether the folder has to be made.
    const plan = planFolderReducer(PLAN_FOLDER_NONE, {
      t: "answer",
      plan: {
        repo_path: "/home/me/code/api",
        folder_display: "~/code/api",
        folder_exists: true,
      },
    });
    expect(plan).toEqual({ path: "/home/me/code/api", display: "~/code/api", exists: true });
    expect(planGatePath(plan)).toBe("");
    expect(gateFor(plan, "/home/me/code/api")).toBe("");
  });

  it("reads a MISSING folder_exists as a folder that is not there", () => {
    // Same reasoning as planInPlace's absent key: a server too old to send it
    // costs one tick, while reading it as "already there" makes a directory
    // nobody was ever shown.
    const plan = planFolderReducer(PLAN_FOLDER_NONE, {
      t: "answer",
      plan: { repo_path: "/home/me/code/invoice-parser" },
    });
    expect(plan.exists).toBe(false);
    expect(planGatePath(plan)).toBe("/home/me/code/invoice-parser");
  });

  it("KEEPS the question across a reopen, because the field it is about survives", () => {
    // THE BUG THIS CASE EXISTS FOR. The tick dies on a reopen and the question
    // does not: folderReducer's own "reopen" deliberately keeps folder.path, so
    // clearing the plan here left the Folder field holding the model's
    // not-yet-existing folder with nothing asking about it.
    expect(planFolderReducer(NEW_PLAN, { t: "reopen" })).toEqual(NEW_PLAN);
  });

  it("forgets it once the create has actually happened", () => {
    // After a 200 the folder is on disk (and if the POST failed, the plan is not
    // what the next opening is about either) — so a later reopen must not offer
    // to create a folder that is already there.
    expect(planFolderReducer(NEW_PLAN, { t: "created" })).toEqual(PLAN_FOLDER_NONE);
  });
});

describe("the confirm gate across a reopen — the two reducers together", () => {
  // Both halves were tested alone and both were right alone, which is how this
  // shipped: folderReducer keeps the folder across a reopen, newFolderGate arms
  // on the folder in the field, and the dialog in between threw the plan away.

  it("is still armed after the dialog is closed and opened again", () => {
    // applyPlan writes the model's folder through the same reducer as every
    // other way of naming one.
    const filled = run(FOLDER_INIT, { t: "reopen" }, { t: "user-set", path: NEW_PLAN.path });
    // Close, reopen. The field still reads /home/me/code/invoice-parser.
    const folder = folderReducer(filled, { t: "reopen" });
    expect(folder.path).toBe("/home/me/code/invoice-parser");
    const plan = planFolderReducer(NEW_PLAN, { t: "reopen" });
    const gate = gateFor(plan, folder.path);
    expect(gate).toBe("~/code/invoice-parser");
    // And the tick does NOT come back with it — the reopen clears it — so
    // Create still refuses, by name.
    expect(newFolderBlockReason({ gate, confirmed: false })).toContain(
      "Yes, create ~/code/invoice-parser"
    );
  });

  it("stays armed on the machine with no suggestions — the one that forced `new:`", () => {
    // The dispatch that would normally overwrite the field on a reopen is
    // {t:"suggested"}, and it is a no-op when /api/repos/suggest answers an
    // empty list. That is exactly the machine whose menu was empty and whose
    // model therefore had to answer `new:<name>` — so on the one machine where
    // a plan is certain to propose a new folder, nothing re-armed the gate.
    const folder = run(
      FOLDER_INIT,
      { t: "reopen" },
      { t: "user-set", path: NEW_PLAN.path },
      { t: "reopen" },
      { t: "suggested", path: "" },
      { t: "fallback", path: "/home/me" }
    );
    expect(folder.path).toBe("/home/me/code/invoice-parser");
    expect(gateFor(planFolderReducer(NEW_PLAN, { t: "reopen" }), folder.path)).toBe(
      "~/code/invoice-parser"
    );
  });

  it("disarms the moment this opening's own suggestion replaces the field", () => {
    // A retained plan self-clears, which is the whole point of deriving the gate
    // from the field: the folder the model proposed is no longer the folder
    // Create would make.
    const folder = run(
      FOLDER_INIT,
      { t: "reopen" },
      { t: "user-set", path: NEW_PLAN.path },
      { t: "reopen" },
      { t: "suggested", path: "/home/me/code/api" }
    );
    expect(folder.path).toBe("/home/me/code/api");
    expect(gateFor(NEW_PLAN, folder.path)).toBe("");
    expect(newFolderBlockReason({ gate: gateFor(NEW_PLAN, folder.path), confirmed: false })).toBe(
      ""
    );
  });

  it("asks nothing about a folder the create has already made", () => {
    const after = planFolderReducer(NEW_PLAN, { t: "created" });
    expect(gateFor(after, "/home/me/code/invoice-parser")).toBe("");
  });
});

describe("planNoteFor — the note that outlived the folder it describes", () => {
  const note = "Using ~/code/invoice-parser — a new folder, working in it directly.";

  it("explains the form while the form still holds the folder it is about", () => {
    expect(planNoteFor({ note, planPath: NEW_PLAN.path, folderPath: NEW_PLAN.path })).toBe(note);
  });

  it("goes quiet the moment the Folder field moves to another folder", () => {
    // THE BUG. Click a suggestion chip for an existing repo after a `new:` plan
    // and the muted line still said "a new folder" while the field, the git
    // nudge and Create's repo_path all said /home/me/code/api.
    expect(planNoteFor({ note, planPath: NEW_PLAN.path, folderPath: "/home/me/code/api" })).toBe(
      ""
    );
    // Mid-edit too: a half-deleted path is not the folder the note is about.
    expect(
      planNoteFor({ note, planPath: NEW_PLAN.path, folderPath: "/home/me/code/invoice-pars" })
    ).toBe("");
    expect(planNoteFor({ note, planPath: NEW_PLAN.path, folderPath: "" })).toBe("");
  });

  it("comes back when the plan's folder is typed back, exactly like the gate", () => {
    expect(planNoteFor({ note, planPath: NEW_PLAN.path, folderPath: "/home/me/code/api" })).toBe(
      ""
    );
    expect(planNoteFor({ note, planPath: NEW_PLAN.path, folderPath: NEW_PLAN.path })).toBe(note);
  });

  it("still explains a plan whose folder already existed", () => {
    // The state this replaced kept a path only for a folder that needed making,
    // so for every other plan — the common case — the note had nothing to
    // compare against and could never be silenced.
    const plan = planFolderReducer(PLAN_FOLDER_NONE, {
      t: "answer",
      plan: { repo_path: "/home/me/code/api", folder_display: "~/code/api", folder_exists: true },
    });
    const existing = "Using ~/code/api — an existing repo, in a worktree.";
    expect(planNoteFor({ note: existing, planPath: plan.path, folderPath: "/home/me/code/api" })).toBe(
      existing
    );
    expect(planNoteFor({ note: existing, planPath: plan.path, folderPath: "/srv/work" })).toBe("");
  });

  it("says nothing when no plan has landed this opening", () => {
    expect(planNoteFor({ note: "", planPath: "", folderPath: "/home/me/code/api" })).toBe("");
    expect(planNoteFor({ note, planPath: "", folderPath: "" })).toBe("");
  });

  it("ignores the whitespace a paste leaves in the field, like the gate", () => {
    expect(
      planNoteFor({ note, planPath: NEW_PLAN.path, folderPath: "  /home/me/code/invoice-parser  " })
    ).toBe(note);
  });
});

describe("startPlanRun / cancelPlanRun — one turn at a time, and none left behind", () => {
  it("refuses the Enter pressed while the button already says “Reading…”", () => {
    const slot: PlanRun = { seq: 0, abort: null };
    const first = startPlanRun(slot, false);
    expect(first).not.toBeNull();
    // Nothing else stops these: the box is readOnly rather than disabled, so it
    // keeps focus and goes on firing keydown, and the sentence-level check
    // answers "" while busy ON PURPOSE (the button is already saying it).
    expect(describeBlockReason({ text: "fix the auth bug in acme-api", busy: true })).toBe("");
    // Five impatient Enters used to be five concurrent headless CLI turns, each
    // with three 1.5s filesystem walks behind it and a 75s server budget.
    for (let i = 0; i < 5; i++) expect(startPlanRun(slot, true)).toBeNull();
    // The refused presses moved nothing: the first run still owns the slot, and
    // its request is still the one in flight.
    expect(slot.seq).toBe(first!.seq);
    expect(slot.abort).toBe(first!.ctl);
    expect(first!.ctl.signal.aborted).toBe(false);
  });

  it("never orphans the request it replaces", () => {
    // The second run can only start once the first is over or cancelled, but the
    // old code assigned the new controller over the old one without aborting it
    // either way — and an orphaned controller is a socket nobody can cancel.
    const slot: PlanRun = { seq: 0, abort: null };
    const first = startPlanRun(slot, false)!;
    const second = startPlanRun(slot, false)!;
    expect(first.ctl.signal.aborted).toBe(true);
    expect(second.ctl.signal.aborted).toBe(false);
    expect(slot.abort).toBe(second.ctl);
    // And the stamp moved, so the first run's answer can no longer land.
    expect(second.seq).not.toBe(first.seq);
  });

  it("makes a late answer stale when the dialog is CLOSED, not only when it is reopened", () => {
    // THE BUG. The seq was bumped on open and by Cancel, and a close is neither
    // — and this component never unmounts, so ~15s later the answer arrived
    // with its staleness check still passing and applyPlan wrote the model's
    // path into a form nobody was looking at.
    const slot: PlanRun = { seq: 0, abort: null };
    const started = startPlanRun(slot, false)!;
    cancelPlanRun(slot);
    expect(started.ctl.signal.aborted).toBe(true);
    expect(slot.abort).toBeNull();
    // Which is what the landing checks: seq moved, so applyPlan is skipped.
    expect(slot.seq).not.toBe(started.seq);
  });

  it("lets the next opening start a run of its own after that cancel", () => {
    const slot: PlanRun = { seq: 0, abort: null };
    startPlanRun(slot, false);
    cancelPlanRun(slot);
    const next = startPlanRun(slot, false);
    expect(next).not.toBeNull();
    expect(next!.ctl.signal.aborted).toBe(false);
  });
});

describe("worktreeClampReason — a positive choice the server would have overridden", () => {
  const plain = { inPlace: false, provisionOn: false, plainFolder: true, initRepo: false };

  it("warns when New worktree is selected over a folder with no repo in it", () => {
    // create_instance forces in_place for exactly this case, so without the line
    // the radio says "New worktree" and the 202 hands back a session running in
    // the folder. Silent was survivable while the mode was an unticked box; it
    // is not survivable now that something is always visibly selected.
    expect(worktreeClampReason(plain)).toContain("no git repo in that folder");
    expect(worktreeClampReason(plain)).toContain("run in the folder itself");
  });

  it("says nothing once Create a git repo is ticked, which supplies the missing repo", () => {
    expect(worktreeClampReason({ ...plain, initRepo: true })).toBe("");
  });

  it("says nothing about a folder that is already a repo", () => {
    expect(worktreeClampReason({ ...plain, plainFolder: false })).toBe("");
  });

  it("says nothing when the folder IS the workspace — there is no worktree to clamp", () => {
    expect(worktreeClampReason({ ...plain, inPlace: true })).toBe("");
  });

  it("leaves provisioning to provisionBlockReason rather than answering twice", () => {
    // Both would fire on the same folder, and two lines saying the same thing
    // under two different controls is how a user stops reading either.
    expect(worktreeClampReason({ ...plain, provisionOn: true })).toBe("");
    expect(
      provisionBlockReason({ provision: true, plainFolder: true, initRepo: false, folderPath: "/x" })
    ).not.toBe("");
  });
});

describe("immediateStartBlockReason — the one thing the fast path does not skip", () => {
  const missing = { folderExists: false, folderLabel: "~/code/invoice-parser", confirmed: false };

  it("stops an immediate start when the plan's folder does not exist yet", () => {
    // "Start session now" skips page 2 on purpose. It does NOT skip this: the
    // create would make a directory, and a directory outlives the session that
    // made it — closing a session takes its worktree, never its folder.
    const why = immediateStartBlockReason(missing);
    expect(why).toContain("~/code/invoice-parser");
    expect(why).toContain("does not exist yet");
  });

  it("lets it through once the box is ticked", () => {
    expect(immediateStartBlockReason({ ...missing, confirmed: true })).toBe("");
  });

  it("never asks about a folder that is already there", () => {
    // Every numbered candidate came out of a walk of the real filesystem, so
    // this is the common case and it must cost no extra press.
    expect(immediateStartBlockReason({ ...missing, folderExists: true })).toBe("");
  });

  it("names something rather than nothing when the plan sent no label", () => {
    expect(immediateStartBlockReason({ ...missing, folderLabel: "that folder" })).toContain(
      "that folder"
    );
  });
});
