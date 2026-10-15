/** Intake, round 4 (fresh-eyes round 3 → fixes): save as you type, the safe
 * auto-start default, one truth for auto-start, GitHub untangled from
 * the trackers, tracker housekeeping away from Start, the repo on every row and
 * no raw tracker JSON in a failed Start. */

/// <reference types="vite/client" />
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { dropComments, norm } from "./sourceText";
import { AUTOSAVE_MS, makeAutosave } from "../components/intake/autosave";
import { ONLY_REPO, repoNameOf, routeText } from "../components/intake/kit";
import { friendlyStartError } from "../components/intake/vocab";
import TICKETS_RAW from "../components/intake/TicketsTab.tsx?raw";
import KIT_RAW from "../components/intake/kit.tsx?raw";
import DIALOG_RAW from "../components/intake/IntakeDialog.tsx?raw";
import REPO_FIELD_RAW from "../components/intake/RepoListField.tsx?raw";
import MERGE_RAW from "../components/intake/MergeTicket.tsx?raw";
import NEW_TICKET_RAW from "../components/dialogs/NewTicketPane.tsx?raw";
import AUTOMATION_BAR_RAW from "../components/sidebar/AutomationBar.tsx?raw";

const code = (raw: string) => norm(dropComments(raw));
const TICKETS = code(TICKETS_RAW);
const KIT = code(KIT_RAW);
const DIALOG = code(DIALOG_RAW);

describe("save as you type (M1: never 'Saved ✓' over unsent text)", () => {
  beforeEach(() => vi.useFakeTimers());
  afterEach(() => vi.useRealTimers());

  it("sends once typing pauses, says pending at once, and only the last value", () => {
    const sent: string[] = [];
    let pendings = 0;
    const s = makeAutosave<string>(() => (v) => sent.push(v), () => () => pendings++);
    s.schedule("git@github.com:acme/pay");
    s.schedule("git@github.com:acme/payments-api.git");
    expect(pendings).toBe(2);
    expect(s.isPending()).toBe(true);
    expect(sent).toEqual([]);
    vi.advanceTimersByTime(AUTOSAVE_MS);
    expect(sent).toEqual(["git@github.com:acme/payments-api.git"]);
    expect(s.isPending()).toBe(false);
  });

  it("blur / Enter / unmount flush immediately, and only once", () => {
    const sent: string[] = [];
    const s = makeAutosave<string>(() => (v) => sent.push(v), () => undefined);
    s.schedule("abc");
    s.flush();
    s.flush();
    vi.advanceTimersByTime(AUTOSAVE_MS * 2);
    expect(sent).toEqual(["abc"]);
  });

  it("commits with the CURRENT callback, not the one it was armed with", () => {
    const sent: string[] = [];
    let cb = (v: string) => sent.push("old:" + v);
    const s = makeAutosave<string>(() => cb, () => undefined);
    s.schedule("x");
    cb = (v: string) => sent.push("new:" + v);
    vi.advanceTimersByTime(AUTOSAVE_MS);
    expect(sent).toEqual(["new:x"]);
  });

  it("every text field on the card uses it, and the card reads 'Saving…' while one is dirty", () => {
    expect(TICKETS).toContain("onChange={(e) => save.schedule(e.target.value)}");
    expect(TICKETS).toContain("onBlur={save.flush}");
    expect(TICKETS).toContain("onChange={(e) => labelSave.schedule(e.target.value)}");
    expect(code(REPO_FIELD_RAW)).toContain("onBlur={save.flush}");
    // A re-seed from the server never overwrites text still on its way.
    expect(code(REPO_FIELD_RAW)).toContain("if (save.isPending()) return;");
    expect(TICKETS).toMatch(/dirty \|\| save\?\.state === "saving"/);
    // The save that flushes as the dialog closes runs outside a setState
    // updater (an updater never runs once the tab has unmounted).
    expect(TICKETS).toContain("latest.current = next; setSources(next);");
    expect(TICKETS).toContain("persist(next, { nulls, autoTest, changed: changedId }); } };");
  });
});

describe("the safe default (M2): a new source starts nothing until a state is picked", () => {
  it("a new source preselects no tracker and carries needs_filter", () => {
    // "+ Add source" opens on the tiles with none picked (step 1) — a tile
    // marks the choice, Continue drafts it (intakeTrackerNoDefault.test.ts
    // renders it); the draft is the safe default — nothing auto-starts until
    // a state.
    expect(TICKETS).toContain('const fresh = { id, provider, needs_filter: "on" } as Source;');
    expect(TICKETS).not.toContain("const provider = catalog[0]?.id");
    expect(TICKETS).toContain('picked={draftSource?.provider || (draft ? "" : choice)}');
    expect(TICKETS).toContain('const [choice, setChoice] = useState("");');
    expect(TICKETS).toContain("step={draft ? draft.step : 1}");
  });

  it("says so on the card, and offers 'Any state' only as an explicit choice", () => {
    expect(TICKETS).toContain("None picked — nothing auto-starts from this source yet.");
    expect(TICKETS).toContain('if (v === ANY_STATE) onChange({ needs_filter: "" });');
  });

  it("a state can be typed before Test, and is swapped for the real id once listed", () => {
    expect(TICKETS).toContain('placeholder={"Type a " + noun + " name, e.g. Ready for dev"}');
    expect(TICKETS).toContain("byName.get(v.trim().toLowerCase())");
  });
});

describe("one truth for auto-start (M4, M5)", () => {
  // "Pause intake" (and its server-side record) is gone — intakeNoPause.test.
  it("the Intake header says what auto-starts, and leads to the switches", () => {
    // Round 5: its link goes to the Auto-start tab's switches, whatever is on.
    expect(DIALOG).toContain('"Turn on ›"');
    expect(DIALOG).toContain('onClick={() => gotoTab("autostart")}');
    expect(DIALOG).not.toContain("Pause intake");
    const bar = code(AUTOMATION_BAR_RAW);
    expect(bar).toContain('<label className="dc-switch"');
    expect(bar).not.toContain("pause");
  });

  it("an off switch is drawn as off — there is no second, 'paused' look", () => {
    expect(KIT).toContain('className="ik-switch"');
    expect(KIT).not.toContain("paused");
    expect(TICKETS).not.toContain("paused={");
  });

  it("New → Ticket reads the same switch Intake does, and never says 'ingestion'", () => {
    const pane = code(NEW_TICKET_RAW);
    expect(pane).toContain("useMindflockStatus()");
    expect(pane).not.toMatch(/[Ii]ngestion is on/);
    expect(pane).toContain("Auto-start is on for tickets");
    expect(pane).toContain("Auto-start is off for tickets.");
  });

  it("no 'bucket' in what the ticket list says", () => {
    expect(TICKETS).not.toMatch(/Add bucket|All buckets|bucket everywhere|buckets you're showing/);
    // Which states the panel shows is a View choice now, beside "whose".
    expect(TICKETS).toContain('<span className="ik-view-h">States shown</span>');
  });
});

describe("tracker housekeeping is not next to Start (M7)", () => {
  it("Combine lives under the row's Options, with a confirm naming both tickets", () => {
    expect(TICKETS).not.toContain("<RowMore");
    expect(TICKETS).toContain("housekeeping={");
    expect(KIT).toContain('aria-label="On the tracker"');
    const merge = code(MERGE_RAW);
    expect(merge).toContain("Combine with another ticket (deletes this one)…");
    expect(merge).toContain('"Confirm — delete " + row.slug + ", keep " + (picked?.slug || "")');
    expect(merge).not.toContain("Merge into…");
  });

  it("Escape closes a row's open Options before the dialog (M8)", () => {
    expect(KIT).toContain('if (e.key !== "Escape" || e.defaultPrevented) return; if (drawer && onDrawerClose) {');
    expect(KIT).toContain("} else if (optsOpen) { e.preventDefault(); setOptsOpen(false); }");
  });
});

describe("the repo on every row (M3)", () => {
  it("a one-repo source names its repo; several still say where and why", () => {
    expect(routeText({ repos: ["payments-api"], method: ONLY_REPO })).toBe("→ payments-api");
    expect(routeText({ repos: ["ledger"], method: "label", reason: "label 'ledger'" })).toBe(
      "→ ledger · label 'ledger'"
    );
    expect(repoNameOf("git@github.com:acme/payments-api.git")).toBe("payments-api");
    expect(repoNameOf("/home/me/src/ledger/")).toBe("ledger");
    expect(TICKETS).toContain("method: ONLY_REPO");
  });
});

describe("a failed Start is one sentence (m6)", () => {
  it("a rejected token names where to fix it, without JSON", () => {
    const raw = 'Shortcut API returned 401 for story 4101: {"message":"Unauthorized","tag":"unauthorized"}';
    const out = friendlyStartError("Shortcut", raw);
    expect(out).toBe("Shortcut rejected the API token. Fix it in Intake → Tickets — press Fix on the source's heading.");
    expect(out).not.toContain("{");
  });

  it("anything else keeps its words, minus a JSON tail", () => {
    expect(friendlyStartError("Jira", 'worktree is already checked out: {"x":1}')).toBe(
      "worktree is already checked out"
    );
  });
});
