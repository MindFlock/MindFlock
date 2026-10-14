/** Template schedules in the bell: a scheduled run that couldn't start is a
 * warning row named "Templates" (gated by its notify rule, opening the
 * Templates dialog); a skipped one is a quiet row on the previous run; a
 * start is not news. */

import { describe, it, expect } from "vitest";
import { notifFromEvent } from "../components/NotificationsBell";
import BELL from "../components/NotificationsBell.tsx?raw";
import type { EventEnvelope } from "../state/queries";

const env = (event: string, session: string, data: Record<string, unknown>): EventEnvelope => ({
  event,
  session,
  seq: 1,
  ts: 0,
  old: null,
  new: null,
  data,
});

describe("template events in the bell", () => {
  it("a scheduled failure warns, names its rule and opens Templates", () => {
    const n = notifFromEvent(
      env("template.run_failed", "", {
        template: "nightly",
        error: "repo moved",
        detail: "Scheduled run of “nightly” couldn't start: repo moved",
      })
    );
    expect(n).toMatchObject({
      text: "Scheduled run of “nightly” couldn't start: repo moved",
      cls: "n-warn",
      template: true,
      rule: "template_run_failed",
    });
    expect(BELL).toContain('useUi.getState().openDialogFor("templates")');
    expect(BELL).toContain('? "Templates"');
  });
  it("a skip is a quiet row on the run still going", () => {
    const n = notifFromEvent(
      env("template.run_skipped", "nightly-oct13", { detail: "Skipped a scheduled run of “nightly”: busy" })
    );
    expect(n).toMatchObject({ cls: "n-muted", text: "Skipped a scheduled run of “nightly”: busy" });
    expect(n?.template).toBeUndefined();
  });
  it("a start is not news", () => {
    expect(notifFromEvent(env("template.run_started", "nightly-oct14", { template: "nightly" }))).toBeNull();
  });
});
