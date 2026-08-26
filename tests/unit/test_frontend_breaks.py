"""Take a break, the idle flock, and "a duplicate lands under its source".

Three features whose whole point is *where and when something appears on
screen*, which is exactly the kind of thing a unit test can't watch. What it
can do is pin the contract into the shipped bundle:

- the break screen's markup hooks and both of its buttons really ship;
- the flock has a sprite to draw and the CSS that lets it cover the window;
- the idle overlay is explicitly not a surface — fixed, full-bleed, and
  ``pointer-events: none``, or it would silently eat every click in the app
  the moment somebody stopped typing for five minutes;
- the sidebar order helper that puts a copy beneath its source is wired into
  the duplicate action, not merely exported.
"""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from backend.web import server

client = TestClient(server.app)

_ROOT = Path(__file__).resolve().parents[2]


def _js() -> str:
    return client.get("/app.js").text


def _css() -> str:
    return client.get("/style.css").text


# --------------------------------------------------------------------------- #
# Take a break
# --------------------------------------------------------------------------- #


def test_break_screen_ships_with_both_of_its_answers():
    js = _js()
    assert 'id: "break-screen"' in js or '"break-screen"' in js
    # Snooze pushes it back; "Resumed work" restarts the whole interval. A
    # break screen with only one way out is a trap, not a reminder.
    assert "Snooze 5 min" in js
    assert "Resumed work" in js


def test_break_settings_row_sits_in_general_with_an_interval():
    """The row's whole description IS the sentence it configures."""
    js = _js()
    assert "Take a break" in js
    assert "Reminder to take a break every" in js
    # The two localStorage keys the setting persists under.
    assert "mf_break_on" in js
    assert "mf_break_every" in js


def test_break_screen_counts_as_a_modal_for_the_keymap():
    """Ctrl+W / Delete must not reach the session running behind the card."""
    js = _js()
    ids = js[js.index("MODAL_DOM_IDS") : js.index("MODAL_DOM_IDS") + 500]
    assert "break-screen" in ids


def test_every_global_shortcut_goes_quiet_behind_the_break_card():
    """MODAL_DOM_IDS only guards Ctrl+W/Delete — the dispatcher itself has to
    stand down, or Ctrl+P opens a palette UNDER an opaque scrim and swallows
    every keystroke (its Enter can push or delete the focused session)."""
    js = _js()
    assert "function breakScreenUp" in js
    disp = js[js.index("function _dispatch") : js.index("function _dispatch") + 400]
    assert "breakScreenUp()" in disp, "the keymap dispatcher never consults it"


def test_escape_is_bound_on_the_document_not_the_overlay():
    """A React onKeyDown only fires while focus is inside the card, and one
    click on the scrim drops focus to <body> — which killed Escape for the rest
    of the break and let a dialog hidden behind the scrim answer instead."""
    js = _js()
    assert "resumeNow" in js
    at = js.index("resumeNow")
    block = js[at : at + 900]
    assert 'document.addEventListener("keydown"' in block
    assert "resumeNow.current()" in block


def test_the_flock_is_a_round_trip_through_the_logo():
    """It streams OUT of the mark when it appears and folds back INTO it when
    dismissed — birds that simply materialise everywhere read as a bug."""
    js = _js()
    assert "beginHatch(opts.emergeFrom.x, opts.emergeFrom.y)" in js
    # BOTH surfaces arm it, measured off the real element rather than assumed.
    assert js.count("emergeFrom: logoPoint()") == 2


def test_the_emergence_is_a_slow_stream_not_one_long_interpolation():
    """Twenty seconds of whole-flock interpolation would be twenty seconds with
    the flocking rules switched off, which reads as dead. The window is long;
    each bird's own flight inside it is short, and a bird that lands rejoins the
    live flock while the rest are still queued on the mark."""
    js = _js()
    assert "EMERGE_MS = 2e4" in js or "EMERGE_MS = 20000" in js
    assert "HATCH_FLIGHT_MS" in js
    # step() and hatchStep() run in the SAME frame, on different birds.
    assert "if (hatchFrom) hatchStep(now);" in js
    assert "if (b.hatchAt) continue;" in js


def test_dismissing_sends_the_flock_home_to_the_logo():
    """Both surfaces leave by flying into the mark in the top bar."""
    js = _js()
    assert "gather" in js
    # The mark is measured, not assumed — macOS mirrors the whole cluster right.
    assert "brand-logo" in js
    css = _css()
    # The overlay outlives the click that dismissed it by the length of the
    # flight, so it must stop swallowing the pointer the moment you answer.
    assert ".break-leaving" in css
    block = css[css.index(".break-leaving") : css.index(".break-leaving") + 400]
    assert "pointer-events: none" in block.replace(
        "pointer-events:none", "pointer-events: none"
    )


def test_break_screen_keeps_your_view_and_flies_the_flock_over_it():
    """No scrim. The grid stays exactly as you left it and the birds fly OVER
    it — the same treatment as the idle overlay, which is what was asked for."""
    css = _css()
    assert "\n.modal.break-screen {" in css
    at = css.index("\n.modal.break-screen {")
    block = css[at : at + 400]
    assert "z-index" in block
    assert "background: transparent" in block, "the scrim is back — it hides the view"
    # The card paints above the canvas behind it. Anchored on the rule itself —
    # the leaving state's `.break-screen.break-leaving .break-card` contains the
    # same text, and matching that would read the wrong block.
    assert "\n.break-card {" in css
    at = css.index("\n.break-card {")
    card = css[at : at + 400]
    assert "position: relative" in card or "position:relative" in card


# --------------------------------------------------------------------------- #
# The idle flock
# --------------------------------------------------------------------------- #


def test_idle_flock_covers_the_window_without_capturing_it():
    css = _css()
    assert ".idle-flock" in css, "the idle overlay never reached the bundle"
    block = css[css.index(".idle-flock") : css.index(".idle-flock") + 400]
    flat = block.replace(" ", "")
    assert "position:fixed" in flat, "must ignore the sidebar/pane boxes entirely"
    assert "inset:0" in flat
    # The one rule that keeps a decorative overlay from breaking the whole app.
    assert "pointer-events:none" in flat


def test_idle_flock_is_dismissed_by_deliberate_input_only():
    """A click, a keystroke, a tap, a scroll — and pointedly NOT a mousemove.

    A cursor drifting across the window, or parked on it while its owner reads
    something else, is the one signal that fires constantly without anyone
    meaning anything by it. It must neither hold the flock off nor dismiss it.
    """
    js = _js()
    at = js.index("const ACTIVITY = [")
    activity = js[at : js.index("]", at) + 1]
    for evt in ("pointerdown", "keydown", "touchstart", "wheel"):
        assert evt in activity
    assert "pointermove" not in activity, "a hovering cursor is not someone working"


def test_idle_flock_is_a_switch_that_ships_on():
    """It is a setting, and the one animation in the app that defaults to ON.

    The break card is off out of the box because it interrupts you; the flock
    can only ever appear in a room you have already left, so leaving it on
    costs no interruptions. The row is the same sentence-with-a-field shape as
    the break row above it, with a key for the switch and one for the delay.
    """
    js = _js()
    assert "Idle flock" in js
    assert "Fly the flock over your grid after" in js
    assert 'mf_idle_flock", true' in js, "the flock must default to on"
    assert "mf_idle_after" in js


def test_two_things_stand_the_idle_flock_down():
    """The switch, and the break card — which is already flying its own denser
    flock and must not have a second one land on top of it. Nothing else: an
    agent streaming output into a pane is the machine working, not you."""
    js = _js()
    at = js.index("useIdle(flockAfter")
    call = js[at : js.index(")", at) + 1]
    assert "flockOn" in call and "!onBreak" in call


def test_no_test_scaffolding_survives_in_the_shipped_bundle():
    """The "Show the birds now" switch was scaffolding and is gone."""
    js = _js()
    assert "Show the birds now" not in js
    assert "idleFlockNow" not in js


def test_work_in_another_window_does_not_stop_mindflock_idling():
    """The window merely coming back to the front is not you touching it.

    `focus` and `visibilitychange` both used to reset the countdown, which meant
    that going off to another app and returning greeted you with the app instead
    of birds — the opposite of what the feature is for. MindFlock has to idle
    behind you, so a second monitor fills up while you work elsewhere.
    """
    js = _js()
    at = js.index("const ACTIVITY = [")
    activity = js[at : js.index("]", at) + 1]
    assert "focus" not in activity, "window focus is not human input into MindFlock"
    # The hook's own effect must not re-arm on a visibility change either.
    hook = js[at : at + 1800]
    assert "visibilitychange" not in hook


def test_flock_has_a_sprite_to_draw():
    js = _js()
    assert "/bird.png" in js
    r = client.get("/bird.png")
    assert r.status_code == 200
    assert r.content[:8] == b"\x89PNG\r\n\x1a\n"
    # It ships inside the package, so a wheel install has it too.
    assert (_ROOT / "backend" / "web" / "static" / "bird.png").exists()


# --------------------------------------------------------------------------- #
# Duplicate lands under its source
# --------------------------------------------------------------------------- #


def test_duplicate_is_placed_after_its_source_in_the_sidebar_order():
    js = _js()
    assert "orderWithAfter" in js
    # Wired into the copy action, not just exported: both the optimistic
    # provisioning row and the real title the server picks get placed.
    assert "placeAfter" in js
    copy = js[
        js.index("async function copySession") : js.index("async function copySession")
        + 900
    ]
    assert "placeAfter" in copy


def test_web_ui_docs_describe_both_features():
    text = (_ROOT / "docs" / "web-ui.md").read_text(encoding="utf-8")
    assert "Take a break" in text
    assert "The idle flock" in text
    assert "beneath the one it was copied from" in text
