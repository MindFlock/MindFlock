"""MindFlock MCP UX, the rail + answers unit: structural checks on the shipped
bundle. The wording and the state machines are unit-tested in
frontend/src/__tests__/flockRail.test.ts and answerStrip.test.ts (which also
server-render the real Sidebar to pin that nesting never renumbers the rail);
the rendering was checked with the screenshot harness.

Pins: visual-only family nesting (indent + connector, numbering untouched),
spawned-worker placement under the parent, the worker status line, the
orchestrator roll-up and waiting / wrap-up chip (wrap-up = a PASTE, never a
send), the shared answer strip (dialog id + ``by: "user"``, "Always" never
primary, keys 1-9 only while the row is focused) and the bell's
"· worker of" attention item.
"""

from __future__ import annotations

import re
from pathlib import Path

from fastapi.testclient import TestClient

from backend.web import server
from tests._bundle import in_bundle, squash

client = TestClient(server.app)

_SRC = Path(__file__).resolve().parents[2] / "frontend" / "src"


def _js() -> str:
    return client.get("/app.js").text


def _css() -> str:
    return client.get("/style.css").text


def _src(rel: str) -> str:
    return (_SRC / rel).read_text(encoding="utf-8")


def _fn(js: str, name: str) -> str:
    """The bundled body of top-level ``function name(`` up to the next one."""
    start = js.index("function " + name + "(")
    nxt = re.search(r"\n(?:async )?function \w+\(|\nvar \w+ = ", js[start + 1 :])
    return js[start : start + 1 + (nxt.start() if nxt else len(js))]


# --- Nesting is paint: the rail order, numbering and drops are untouched ----------


def test_nesting_is_computed_per_rendered_list_and_never_reorders():
    js = _js()
    assert "function railNesting(" in js
    sidebar = squash(js[js.index("function Sidebar(") :])
    # Computed from the list being rendered, index-aligned with it; the row
    # number is still the running rowIdx over that same list.
    assert (
        "const nest = railNesting(list.map((r) => ({ key: r.key, parent: r.inst?.parent })));"
        in sidebar
    )
    assert "rowIdx += 1;" in sidebar
    assert "idx: rowIdx," in sidebar
    assert "nest: stableNest(r.key, nest[i])," in sidebar
    # railNesting only reads: nothing in it splices, sorts or filters the rows.
    body = _fn(js, "railNesting")
    for verb in (".splice(", ".sort(", "setOrder", "setRailOrder"):
        assert verb not in body, verb


def test_a_never_seen_worker_is_placed_under_its_parent():
    js = _js()
    body = _fn(js, "placeNewWorkers")
    assert "orderWithAfter(order, t, rest[at])" in body
    # Only titles the saved order has never held: a drag owns the rest.
    assert "!seen.has(r.title)" in body
    sidebar = squash(js[js.index("function Sidebar(") :])
    # Every live row, remote ones included: another device's worker is placed
    # under its (namespaced) parent in that device's section too.
    assert (
        "placeNewWorkers(ui.order, listed)" in sidebar
    ), "placement must merge with the live rows"
    assert "deviceLineage(" in sidebar
    assert "if (order !== ui.order) ui.setOrder(order);" in sidebar
    # The rail renders and drags off the placed order, not the raw saved one.
    assert "orderedInstances(listed, order)" in sidebar
    assert "saved: order," in sidebar


def test_indent_moves_the_dot_onwards_never_the_number():
    css = _css()
    assert in_bundle(".inst.nest-1 .dot { margin-left: 14px; }", css)
    assert in_bundle(".inst.nest-2 .dot { margin-left: 28px; }", css)
    assert in_bundle(".inst.nest-3 .dot { margin-left: 42px; }", css)
    assert ".nest-1 .idx" not in css and ".nest-2 .idx" not in css
    # The 1px accent connector, drawn through the strip/menu under the row.
    assert in_bundle(
        ".inst .nl { position: absolute; width: 0; border-left: 1px solid rgba(var(--accent-rgb), 0.45);",
        css,
    )
    assert in_bundle(".inst-tail { position: relative; display: flow-root; }", css)
    assert in_bundle(".inst-tail > .nl-v { top: 0; bottom: -4px; }", css)
    js = _js()
    assert '"nl nl-elbow"' in js and '"nl nl-stem"' in js


# --- Status line, roll-up, chip ----------------------------------------------------


def test_worker_status_and_rollup_phrases_ship():
    js = _js()
    for phrase in (
        '"? needs your answer"',
        '"✓ reported"',
        '"idle — no report"',
        '"working · "',
        '" needs you"',
        '" of "',
        '" reported"',
        '" working"',
        '"↳ "',
    ):
        assert phrase in js, phrase
    # Coloured by state on the rail row.
    css = _css()
    assert in_bundle(".inst .lineage.rep-done { color: var(--green); }", css)
    assert in_bundle(".inst .lineage.rep-blocked { color: var(--red); }", css)
    assert in_bundle(".inst .lineage.rep-ask { color: var(--gold); }", css)
    assert in_bundle(".inst .lineage .needs { color: var(--gold);", css)
    # The roll-up opens the Thread — from the keyboard too (review 2026-10-05).
    assert "onClick: (e) => act(() => openThread(title), e)" in squash(js)
    assert in_bundle('role: "button", tabIndex: 0,', js)
    assert in_bundle(".inst .lineage.workers:focus-visible { outline:", _css())


def test_wrap_up_chip_pastes_and_never_submits():
    js = _js()
    assert '"wrap up"' in js and '"waiting"' in js
    assert '"wrapchip"' in js and '"s-waiting"' in js
    assert "onClick: (e) => act(() => void pasteWrapup(title), e)" in squash(js)
    # Never a one-click paste into a session that can't take one (review
    # 2026-10-05): the fork button's block reason gates the chip.
    assert (
        "parentChip(inst, kids, nameOf, effectiveActivity, forkBlockReason(inst))"
        in squash(js)
    )
    # The chip goes through the ONE paste path (lib/playbooks.pastePlaybook),
    # whose render-then-submit:false shape test_frontend_playbooks pins.
    wrap = squash(_fn(js, "pasteWrapup"))
    assert 'pastePlaybook(title, { id: "wrapup", label: "Wrap up workers" })' in wrap
    assert "submit: true" not in wrap
    css = _css()
    assert in_bundle(".inst .stagechip.wrapchip { cursor: pointer;", css)
    assert in_bundle(".inst .stagechip.s-waiting {", css)
    assert "border: 1px dashed rgba(var(--accent-rgb), 0.55)" in css


# --- The answer strip --------------------------------------------------------------


def test_answer_strip_answers_as_the_user_against_the_shown_dialog():
    js = _js()
    answer = squash(_fn(js, "answerDialog"))
    assert (
        'instApi(title, "/answer", { json: { keys: [key], dialog_id: dialogId, by: "user" } })'
        in answer
    )
    # A 409 "the prompt changed" refetches instead of answering blind.
    assert "e?.status === 409 && " in js and "(e.body).dialog_changed === true" in js
    # "Always" is never the primary button.
    assert in_bundle(
        'return options.length && options[0].kind !== "always" ? 0 : -1;', js
    )
    assert '"answered"' in js


def test_answer_strip_reads_quietly_and_only_while_clarify():
    """Live run 2026-10-05: ~8 ``GET /dialog`` 409s ("not waiting on a
    prompt") per run, each a console error — strips re-reading right after
    an answer. Every read asks ``?quiet=1`` (204 = no dialog), and the
    strip's own re-reads (the answered recheck, the resize re-read) run only
    while the row's activity is ``clarify``."""
    js = squash(_js())
    assert 'instApi(title, "/dialog?quiet=1", { signal })' in js
    assert '"/dialog", {' not in js
    assert "if (!answered || !waiting) return;" in js
    assert "if (!waiting || !shown) return;" in js
    assert 'if (live.current.phase !== "ready") return;' in js


def test_answer_strip_survives_a_redraw_of_the_same_prompt():
    """Defect C, UI side: a read older than DIALOG_FRESH_MS is re-read right
    before the click, a resize re-reads the dialog, and a 409 "the prompt
    changed" whose fresh read is the same prompt is retried once."""
    js = squash(_js())
    assert "now - fetchedAt > DIALOG_FRESH_MS" in js
    assert "if (attempt > 0 || !fresh || !sameDialogShape(shown, fresh))" in js
    assert 'window.addEventListener("resize", settle);' in js
    assert "answerFresh(title, key, d, s.loadedAt)" in js


def test_rail_keys_only_while_the_row_is_focused():
    js = squash(_js())
    assert "tabIndex: answering ? 0 : void 0," in js
    assert (
        "if (!answering || e.ctrlKey || e.metaKey || e.altKey || !/^[1-9]$/.test(e.key)) return;"
        in js
    )
    assert "if (isEditingTarget(e.target)) return;" in js
    assert "if (strip.current?.answerKey(e.key)) {" in js


def test_answer_strip_css_reaches_the_bundle():
    css = _css()
    assert in_bundle(".flock-answer.fa-rail { margin: -1px 6px 6px 40px;", css)
    assert in_bundle(".inst.nest-1 .flock-answer.fa-rail { margin-left: 47px; }", css)
    assert ".flock-answer.fa-bell .fa-box" in css
    assert ".flock-answer.fa-thread" in css
    assert in_bundle(
        ".flock-answer button.primary { background: rgba(var(--accent-rgb), 0.9);", css
    )


def test_bell_names_the_parent_and_carries_the_strip():
    js = _js()
    assert '"· worker of "' in js
    assert '" is waiting on this worker too"' in js
    assert "children: workerOf(fam.parent, displayName)" in js
    assert 'variant: "bell",' in js
    assert in_bundle(".attn-lineage { color: rgba(var(--accent-rgb), 0.9);", _css())


def test_new_components_never_use_native_dialogs():
    # Electron implements no window.prompt, and alert/confirm are as bad here:
    # the answer strip and the paste actions are one-click by design.
    for rel in ("components/AnswerStrip.tsx", "lib/flockActions.ts"):
        src = _src(rel)
        for call in ("prompt(", "alert(", "confirm("):
            assert re.search(r"(?<![\w.])" + re.escape(call), src) is None, (rel, call)
            assert "window." + call not in src, (rel, call)


# --- A split orchestrator answers in place before its first worker (defect B) -----


def test_strip_gate_includes_the_created_playbook():
    """The rail row and the bell gate the answer strip on ``inFamily``: a
    worker, a parent, or a session created with a playbook (``row.playbook``,
    "split") — its first spawn_session prompt comes before any child."""
    js = _js()
    assert in_bundle(
        "return isWorker || kids > 0 || !!row.playbook;", _fn(js, "inFamily")
    )
    flat = squash(js)
    # Ship lanes widened the rail gate on purpose: a session MindFlock is
    # carrying (a group member, or one with a lane) is handed off like a
    # worker, so its prompt gets the strip too. inFamily itself is unchanged,
    # and a loner with no group and no lane still gets none
    # (frontend/src/__tests__/runs.test.ts pins both).
    assert (
        "const answering = (inFamily(inst, isWorker, kids.length) || shipLane) "
        '&& activity === "clarify" && !missing && !paused;'
    ) in flat
    assert "const shipLane = !!inst.run || !!ship;" in flat
    assert (
        "return inFamily(inst, !!parent, families.get(title)?.length ?? 0) ? { parent } : null;"
        in flat
    )
    # The old has-children-only gate is gone from both.
    assert "(isWorker || kids.length > 0) && activity" not in flat
    assert "return parent || families.has(title) ? { parent } : null;" not in flat
