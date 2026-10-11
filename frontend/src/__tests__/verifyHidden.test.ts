/** Verify's helper sessions are hidden by the server's `hidden_by` flag, not
 * by their title — see `isVerifySession` in components/dialogs/verify.ts.
 *
 * The bug: any session a person named `verify-foo` vanished from the rail,
 * the grid and the Ctrl+1-9 numbers, because `verify-` was the only signal.
 * Now the row's `hidden_by` decides, and the prefix survives only as the
 * fallback for a row that carries no flag at all (an older server). */
import { describe, it, expect, beforeEach, afterEach } from "vitest";
import {
  isVerifySession,
  VERIFY_HIDDEN_BY,
  VERIFY_SESSION_PREFIX,
} from "../components/dialogs/verify";
import { isRunningTarget } from "../lib/promptTargets";
import { slotNumber } from "../lib/windowName";
import { useUi } from "../state/store";

describe("isVerifySession — the flag decides", () => {
  it("hides a session the server flagged, whatever its title", () => {
    expect(isVerifySession({ title: "verify-p1-abc1234", hidden_by: VERIFY_HIDDEN_BY })).toBe(
      true
    );
    // The flag, not the name: a run the server chose to title differently
    // is still a run.
    expect(isVerifySession({ title: "checklist-run", hidden_by: "verify" })).toBe(true);
  });

  it("keeps a user-named verify-foo visible when the server says it is ordinary", () => {
    // A current server sends `hidden_by: ""` for the user's own sessions — that
    // is authoritative, and the prefix must not override it.
    expect(isVerifySession({ title: "verify-foo", hidden_by: "" })).toBe(false);
    expect(isVerifySession({ title: VERIFY_SESSION_PREFIX + "anything", hidden_by: "" })).toBe(
      false
    );
  });

  it("ignores an unknown owner (nothing but verify hides)", () => {
    expect(isVerifySession({ title: "verify-foo", hidden_by: "something-else" })).toBe(false);
  });

  it("leaves an ordinary session alone", () => {
    expect(isVerifySession({ title: "api", hidden_by: "" })).toBe(false);
    expect(isVerifySession({ title: "api" })).toBe(false);
    expect(isVerifySession("api")).toBe(false);
  });
});

describe("isVerifySession — the legacy fallback", () => {
  // Documented exception: a row with NO `hidden_by` (undefined / null) comes
  // from a server that predates the flag (or a paired device still running
  // one). It has nothing else to go on, so the title prefix stands in. Runs
  // created before the flag on an upgraded server never reach this branch —
  // the server backfills them from the checklists that name them, and sends
  // `hidden_by` for every row.
  it("falls back to the prefix for a row that carries no flag at all", () => {
    expect(isVerifySession({ title: "verify-p1-abc1234" })).toBe(true);
    expect(isVerifySession({ title: "verify-p1-abc1234", hidden_by: null })).toBe(true);
    expect(isVerifySession({ title: "real" })).toBe(false);
  });

  it("falls back to the prefix for a bare title (an event envelope)", () => {
    expect(isVerifySession("verify-p1-abc1234")).toBe(true);
    expect(isVerifySession("fix-p1")).toBe(false);
    expect(isVerifySession("")).toBe(false);
  });
});

describe("the surfaces that leave verify runs out read the flag too", () => {
  type Row = { title: string; hidden_by?: string; status?: string; started?: boolean };
  const withSessions = (rows: Row[]) => {
    (globalThis as unknown as { mindflock?: Record<string, unknown> }).mindflock = {
      sessions: () => rows,
    };
  };

  beforeEach(() => {
    useUi.setState({ aliases: {}, railOrder: [], order: [], filter: "" });
    withSessions([]);
  });
  afterEach(() => {
    delete (globalThis as unknown as { mindflock?: unknown }).mindflock;
  });

  it("Ctrl+1-9 numbers a user-named verify-foo and skips the flagged run", () => {
    withSessions([
      { title: "verify-foo", hidden_by: "" },
      { title: "verify-p1-abc1234", hidden_by: "verify" },
      { title: "real", hidden_by: "" },
    ]);
    expect(slotNumber("verify-foo")).toBe("1");
    expect(slotNumber("real")).toBe("2");
    expect(slotNumber("verify-p1-abc1234")).toBe("");
  });

  it("prompt targets offer a user-named verify-foo and not the flagged run", () => {
    expect(isRunningTarget({ title: "verify-foo", hidden_by: "", status: "running" })).toBe(
      true
    );
    expect(
      isRunningTarget({ title: "verify-p1-abc1234", hidden_by: "verify", status: "running" })
    ).toBe(false);
  });
});
