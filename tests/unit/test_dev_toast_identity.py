"""The dev shell's notifications are headed by a NAME, not by its AUMID.

Windows heads a toast with the display name of the Start-menu shortcut
registered for the AppUserModelID that raised it. The packaged app gets one from
its NSIS installer, which is why prod notifications read "MindFlock". Dev sets an
AUMID of its own (a separate taskbar group and icon) and nothing ever wrote a
shortcut for it, so Windows fell back to printing the raw id and every dev
notification was headed ``ai.mindflock.desktop.dev``.

``electron/main.js`` now writes that shortcut itself. Nothing here can watch a
real toast, so what is pinned is the wiring that decides what one says: the name,
the id it is registered against, and the two properties that make the shortcut
work as a launcher rather than a decoy.
"""

from __future__ import annotations

from pathlib import Path

_MAIN_JS = Path(__file__).resolve().parents[2] / "electron" / "main.js"


def _js() -> str:
    return _MAIN_JS.read_text(encoding="utf-8")


def _fn(js: str) -> str:
    """Just the body of ``ensureDevToastName``, so an assertion about it cannot
    be satisfied by a coincidence elsewhere in a 1600-line main.js."""
    fn = js[js.index("function ensureDevToastName") :]
    return fn[: fn.index("\n}\n")]


def test_the_name_windows_prints_is_prod_s_plus_a_suffix():
    """ "Like prod, with -dev at the end" is the whole requirement."""
    js = _js()
    assert "const DEV_TOAST_NAME = 'MindFlock-dev'" in js
    # The .lnk is named after it, because the FILENAME is what Windows shows.
    assert "DEV_TOAST_NAME + '.lnk'" in js


def test_the_shortcut_is_registered_against_the_dev_aumid():
    """A shortcut carrying any other id names nothing: the toast is attributed
    by the AUMID the process set on itself, so the two must be one constant."""
    js = _js()
    assert "const DEV_AUMID = 'ai.mindflock.desktop.dev'" in js
    assert "app.setAppUserModelId(DEV_AUMID)" in js
    fn = _fn(js)
    assert "appUserModelId: DEV_AUMID" in fn


def test_it_lands_in_the_start_menu_and_targets_the_exe():
    """Windows only resolves the name from a Start-menu shortcut, and it must
    target electron.exe directly — a .bat would attribute icon and identity to
    cmd.exe (the same reason the README's pin-to-taskbar recipe says so)."""
    fn = _fn(_js())
    assert "'Start Menu', 'Programs'" in fn
    assert "target: process.execPath" in fn
    assert "--mindflock-dev" in fn, "the shortcut must launch a DEV shell, not prod"


def test_prod_and_every_other_platform_are_untouched():
    """Prod already has the installer's shortcut, and no other OS resolves
    notification identity this way — so the whole thing is inert elsewhere."""
    fn = _fn(_js())
    assert "if (!DEV || process.platform !== 'win32') return" in fn


def test_a_locked_down_start_menu_cannot_stop_the_app():
    """It is a label. Policy can forbid writing there, and a dev run that
    refused to start over a cosmetic .lnk would be a far worse bug."""
    fn = _fn(_js())
    assert "try {" in fn and "catch (e)" in fn


def test_the_shortcut_is_not_rewritten_on_every_launch():
    """Only when missing or drifted — an .lnk rewritten every start churns the
    Start menu's index for nothing."""
    fn = _fn(_js())
    assert "readShortcutLink" in fn
    assert "have.appUserModelId === want.appUserModelId" in fn


def test_a_png_icon_override_does_not_become_a_broken_shortcut():
    """``MINDFLOCK_DEV_ICON`` is shared with the window/dock icon, which takes a
    .png happily. A shortcut's IconLocation does not: pointing one at a .png
    yields a blank tile, which is worse than the exe's own icon. So the override
    is used verbatim only when it is an .ico, and otherwise falls back."""
    fn = _fn(_js())
    assert "endsWith('.ico')" in fn
    assert "? DEV_ICON : process.execPath" in fn


def test_a_drifted_shortcut_is_rewritten_whichever_field_moved():
    """ "Only when missing or stale" is only true if staleness is checked
    against everything that can move. electron.exe relocates on an upgrade, the
    app dir moves with a checkout, the icon override changes — each one alone
    leaves a shortcut that names or launches the wrong thing."""
    fn = _fn(_js())
    for field in ("target", "args", "icon", "appUserModelId"):
        assert "have.%s === want.%s" % (field, field) in fn, field
    assert "writeShortcutLink(link, 'create', want)" in fn


def test_it_runs_where_its_own_log_line_is_readable():
    """A first dev run on a fresh machine is exactly when "did the shortcut get
    written?" is worth knowing, and the answer only reaches main.log if the call
    happens after the logger is up."""
    js = _js()
    ready = js[js.index("app.whenReady().then(") :]
    ready = ready[: ready.index("startUpdateChecks()")]
    assert "ensureDevToastName()" in ready
    assert ready.index("logger.init") < ready.index("ensureDevToastName()")
