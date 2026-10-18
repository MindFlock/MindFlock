/** MindFlock MCP UX, the rail + answers unit: the structural wiring, moved
 * from tests/unit/test_frontend_flock_rail.py (which grepped the built
 * app.js / style.css). The wording and the state machines are unit-tested in
 * flockRail.test.ts and answerStrip.test.ts (which also server-render the real
 * Sidebar to pin that nesting never renumbers the rail); these pin where the
 * pieces meet, each in the module that owns it.
 *
 * Pinned: visual-only family nesting (indent + connector, numbering
 * untouched), spawned-worker placement under the parent, the worker status
 * line, the orchestrator roll-up and waiting / wrap-up chip (wrap-up = a
 * PASTE, never a send), the shared answer strip (dialog id + `by: "user"`,
 * "Always" never primary, keys 1-9 only while the row is focused) and the
 * bell's "· worker of" attention item. */

import { describe, expect, it } from "vitest";
import { CSS, SRC, allCode, normCode, fn, has } from "./sourceText";

const SIDEBAR = normCode("components/sidebar/Sidebar.tsx");
const ROW = normCode("components/sidebar/SidebarRow.tsx");
const ROW_CSS = normCode("components/sidebar/SidebarRow.css");
const STRIP = normCode("components/AnswerStrip.tsx");
const STRIP_CSS = normCode("components/AnswerStrip.css");
const BELL = normCode("components/NotificationsBell.tsx");
const MSGS = normCode("lib/agentMessages.ts");
const ALL = allCode();

describe("nesting is paint over one family-contiguous rail order", () => {
  it("computes nesting per rendered list and never reorders", () => {
    expect(has(normCode("components/sidebar/ordering.ts"), "export function railNesting(")).toBe(true);
    // Computed from the list being rendered, index-aligned with it; the row
    // number is still the running rowIdx over that same list.
    expect(
      has(SIDEBAR, "const nest = railNesting(list.map((r) => ({ key: r.key, parent: r.inst?.parent })));")
    ).toBe(true);
    expect(has(SIDEBAR, "rowIdx += 1;")).toBe(true);
    expect(has(SIDEBAR, "idx={rowIdx}")).toBe(true);
    expect(has(SIDEBAR, "nest={stableNest(r.key, nest[i])}")).toBe(true);
    // railNesting only reads: nothing in it splices, sorts or filters the rows.
    const body = fn("components/sidebar/ordering.ts", "railNesting");
    for (const verb of [".splice(", ".sort(", "setOrder", "setRailOrder"]) expect(body).not.toContain(verb);
  });

  it("places a never-seen worker under its parent", () => {
    const body = fn("components/sidebar/ordering.ts", "placeNewWorkers");
    expect(has(body, "orderWithAfter(order, t, rest[at])")).toBe(true);
    // Only titles the saved order has never held: a drag owns the rest.
    expect(has(body, "!seen.has(r.title)")).toBe(true);
    // Every live row, remote ones included: another device's worker is placed
    // under its (namespaced) parent in that device's section too.
    expect(has(SIDEBAR, "placeNewWorkers(ui.order, listed)")).toBe(true);
    expect(has(SIDEBAR, "deviceLineage(")).toBe(true);
    expect(has(SIDEBAR, "if (order !== ui.order) ui.setOrder(order);")).toBe(true);
    // The rail renders and drags off the placed order, not the raw saved one.
    expect(has(SIDEBAR, "orderedInstances(listed, order)")).toBe(true);
    expect(has(SIDEBAR, "saved: order,")).toBe(true);
  });

  it("keeps every family contiguous in the one rail order everything reads", () => {
    // railKeys feeds the rendered rows, the published railOrder and the drop
    // merge alike, so a worker is never drawn, numbered or dropped apart.
    expect(has(SIDEBAR, "return keepFamiliesTogether(")).toBe(true);
    expect(has(SIDEBAR, "live: railKeys,")).toBe(true);
  });

  it("indents the dot onwards, never the number, with a 1px accent connector", () => {
    expect(has(ROW_CSS, ".inst.nest-1 .dot { margin-left: 14px; }")).toBe(true);
    expect(has(ROW_CSS, ".inst.nest-2 .dot { margin-left: 28px; }")).toBe(true);
    expect(has(ROW_CSS, ".inst.nest-3 .dot { margin-left: 42px; }")).toBe(true);
    expect(Object.keys(CSS).length).toBeGreaterThan(20);
    for (const [path, css] of Object.entries(CSS)) {
      expect(css, path).not.toContain(".nest-1 .idx");
      expect(css, path).not.toContain(".nest-2 .idx");
    }
    // The connector, drawn through the strip/menu under the row.
    expect(
      has(ROW_CSS, ".inst .nl { position: absolute; width: 0; border-left: 1px solid rgba(var(--accent-rgb), 0.45);")
    ).toBe(true);
    expect(has(ROW_CSS, ".inst-tail { position: relative; display: flow-root; }")).toBe(true);
    expect(has(ROW_CSS, ".inst-tail > .nl-v { top: 0; bottom: -4px; }")).toBe(true);
    expect(has(ROW, 'className="nl nl-elbow"')).toBe(true);
    expect(has(ROW, 'className="nl nl-stem"')).toBe(true);
  });
});

describe("status line, roll-up, chip", () => {
  it("ships the worker status and roll-up phrases", () => {
    for (const phrase of [
      '"? needs you · question"',
      '"✓ reported"',
      '"idle — no report"',
      '"working · "',
      '" needs you"',
      '" of "',
      '" reported"',
      '" working"',
      '"↳ "',
    ])
      expect(MSGS, phrase).toContain(phrase);
    // Coloured by state on the rail row.
    expect(has(ROW_CSS, ".inst .lineage.rep-done { color: var(--green); }")).toBe(true);
    expect(has(ROW_CSS, ".inst .lineage.rep-blocked { color: var(--red); }")).toBe(true);
    expect(has(ROW_CSS, ".inst .lineage.rep-ask { color: var(--gold); }")).toBe(true);
    expect(has(ROW_CSS, ".inst .lineage .needs { color: var(--gold);")).toBe(true);
    // The roll-up opens the Thread — from the keyboard too (review 2026-10-05).
    expect(has(ROW, 'role="button" tabIndex={0} onClick={(e) => act(() => openThread(title), e)}')).toBe(true);
    expect(has(ROW_CSS, ".inst .lineage.workers:focus-visible { outline:")).toBe(true);
  });

  it("wraps up with a paste that never submits", () => {
    const chip = fn("lib/agentMessages.ts", "parentChip");
    expect(chip).toContain('label: "wrap up"');
    expect(chip).toContain('kind: "waiting"');
    expect(chip).toContain('label: "waiting"');
    expect(chip).toContain('cls: "wrapchip"');
    expect(chip).toContain('cls: "s-waiting"');
    expect(has(ROW, "onClick={(e) => act(() => void pasteWrapup(title), e)}")).toBe(true);
    // Never a one-click paste into a session that can't take one (review
    // 2026-10-05): the fork button's block reason gates the chip.
    expect(has(ROW, "parentChip(inst, kids, nameOf, effectiveActivity, forkBlockReason(inst))")).toBe(true);
    // The chip goes through the ONE paste path (lib/playbooks.pastePlaybook),
    // whose render-then-submit:false shape playbooks tests pin.
    const wrap = fn("lib/flockActions.ts", "pasteWrapup");
    expect(has(wrap, 'pastePlaybook(title, { id: "wrapup", label: "Wrap up workers" })')).toBe(true);
    expect(wrap).not.toContain("submit: true");
    expect(has(ROW_CSS, ".inst .stagechip.wrapchip { cursor: pointer;")).toBe(true);
    expect(has(ROW_CSS, ".inst .stagechip.s-waiting {")).toBe(true);
    expect(has(fn_css(ROW_CSS, ".inst .stagechip.s-waiting"), "border: 1px dashed rgba(var(--accent-rgb), 0.55)")).toBe(
      true
    );
  });
});

describe("the answer strip", () => {
  it("answers as the user, against the dialog it showed", () => {
    expect(
      has(
        fn("lib/flockActions.ts", "answerDialog"),
        'instApi(title, "/answer", { json: { keys: [key], dialog_id: dialogId, by: "user" } })'
      )
    ).toBe(true);
    // A 409 "the prompt changed" refetches instead of answering blind.
    expect(
      has(fn("lib/flockActions.ts", "isDialogChanged"), "e?.status === 409 && obj(e.body).dialog_changed === true")
    ).toBe(true);
    // "Always" is never the primary button.
    expect(has(STRIP, 'return options.length && options[0].kind !== "always" ? 0 : -1;')).toBe(true);
    expect(STRIP).toContain('"answered"');
  });

  it("reads quietly, and re-reads only while clarify", () => {
    // Live run 2026-10-05: ~8 `GET /dialog` 409s per run, each a console error.
    // Every read asks ?quiet=1 (204 = no dialog), and the strip's own re-reads
    // run only while the row's activity is `clarify`.
    expect(has(fn("lib/flockActions.ts", "fetchDialog"), 'instApi<unknown>(title, "/dialog?quiet=1", { signal })')).toBe(
      true
    );
    expect(ALL).not.toMatch(/"\/dialog"\s*,/);
    expect(has(STRIP, "if (!answered || !waiting) return;")).toBe(true);
    expect(has(STRIP, "if (!waiting || !shown) return;")).toBe(true);
    expect(has(STRIP, 'if (live.current.phase !== "ready") return;')).toBe(true);
  });

  it("survives a redraw of the same prompt", () => {
    // Defect C, UI side: a read older than DIALOG_FRESH_MS is re-read right
    // before the click, a resize re-reads the dialog, and a 409 "the prompt
    // changed" whose fresh read is the same prompt is retried once.
    const fresh = fn("lib/flockActions.ts", "answerFresh");
    expect(has(fresh, "now - fetchedAt > DIALOG_FRESH_MS")).toBe(true);
    expect(has(fresh, "if (attempt > 0 || !fresh || !sameDialogShape(shown, fresh))")).toBe(true);
    expect(has(STRIP, 'window.addEventListener("resize", settle);')).toBe(true);
    expect(has(STRIP, "answerFresh(title, key, d, s.loadedAt)")).toBe(true);
  });

  it("takes rail keys only while the row is focused", () => {
    expect(
      has(ROW, "tabIndex={pending ? undefined : answering || focused === title || (idx === 0 && !focused) ? 0 : -1}")
    ).toBe(true);
    expect(
      has(ROW, "if (!answering || e.ctrlKey || e.metaKey || e.altKey || !/^[1-9]$/.test(e.key)) return;")
    ).toBe(true);
    expect(has(ROW, "if (isEditingTarget(e.target as Element)) return;")).toBe(true);
    expect(has(ROW, "if (strip.current?.answerKey(e.key)) {")).toBe(true);
  });

  it("styles the rail, bell and Thread hosts", () => {
    expect(has(STRIP_CSS, ".flock-answer.fa-rail { margin: -1px 6px 6px 40px;")).toBe(true);
    expect(has(ROW_CSS, ".inst.nest-1 .flock-answer.fa-rail { margin-left: 47px; }")).toBe(true);
    expect(STRIP_CSS).toContain(".flock-answer.fa-bell .fa-box");
    expect(STRIP_CSS).toContain(".flock-answer.fa-thread");
    expect(has(STRIP_CSS, ".flock-answer button.primary { background: rgba(var(--accent-rgb), 0.9);")).toBe(true);
  });

  it("names the parent in the bell and carries the strip there", () => {
    expect(MSGS).toContain('"· worker of "');
    expect(STRIP).toContain('" is waiting on this worker too"');
    expect(has(BELL, '<span className="attn-lineage">{workerOf(fam.parent, displayName)}</span>')).toBe(true);
    expect(has(BELL, 'variant="bell"')).toBe(true);
    expect(has(normCode("components/NotificationsBell.css"), ".attn-lineage { color: rgba(var(--accent-rgb), 0.9);")).toBe(
      true
    );
  });

  it("never uses native dialogs in the strip or the paste actions", () => {
    // Electron implements no window.prompt, and alert/confirm are as bad here:
    // the answer strip and the paste actions are one-click by design.
    for (const rel of ["components/AnswerStrip.tsx", "lib/flockActions.ts"]) {
      const src = SRC[rel];
      expect(src, rel).toBeDefined();
      for (const call of ["prompt(", "alert(", "confirm("]) {
        const re = new RegExp("(?<![\\w.])" + call.replace("(", "\\("));
        expect(re.test(src), `${rel} ${call}`).toBe(false);
        expect(src, `${rel} window.${call}`).not.toContain("window." + call);
      }
    }
  });
});

describe("a split orchestrator answers in place before its first worker (defect B)", () => {
  it("gates the strip on inFamily, which includes the created playbook", () => {
    // A worker, a parent, or a session created with a playbook (`row.playbook`,
    // "split") — its first spawn_session prompt comes before any child.
    expect(has(fn("lib/agentMessages.ts", "inFamily"), "return isWorker || kids > 0 || !!row.playbook;")).toBe(true);
    // Ship lanes widened the rail gate on purpose: a session MindFlock is
    // carrying (a group member, or one with a lane) is handed off like a
    // worker, so its prompt gets the strip too (runs.test.ts pins both sides).
    expect(
      has(
        ROW,
        "const answering = (inFamily(inst, isWorker, kids.length) || shipLane || !onScreen) " +
          '&& activity === "clarify" && !missing && !paused;'
      )
    ).toBe(true);
    expect(has(ROW, "const shipLane = !!inst.run || !!ship;")).toBe(true);
    expect(
      has(BELL, "return inFamily(inst, !!parent, families.get(title)?.length ?? 0) ? { parent } : null;")
    ).toBe(true);
    // The old has-children-only gate is gone from both.
    expect(has(ALL, "(isWorker || kids.length > 0) && activity")).toBe(false);
    expect(has(ALL, "return parent || families.has(title) ? { parent } : null;")).toBe(false);
  });
});

/** One rule's declarations out of an already-normalised sheet. */
function fn_css(css: string, selector: string): string {
  const start = css.indexOf(selector + " {");
  if (start < 0) throw new Error("no rule for " + selector);
  return css.slice(start, css.indexOf("}", start));
}
