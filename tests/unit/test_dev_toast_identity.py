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

import json
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

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
    # A non-.ico override is discarded before staging, and the `|| ...` on the
    # next line is what turns that into the exe's icon.
    assert "? DEV_ICON : null" in fn
    assert "|| process.execPath" in fn


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


# --------------------------------------------------------------------------- #
# …and the icon Windows draws beside that name
# --------------------------------------------------------------------------- #
# The name landed and the icon did not: the toast came up headed "MindFlock-dev"
# with the generic white document tile beside it. `IconLocation` is resolved by
# the SHELL, not by us, and on the supported Windows shape it pointed into the
# WSL share the checkout lives on (`\\wsl.localhost\...`), which the shell's icon
# extraction does not read. Nothing was wrong with dev-icon.ico — the window and
# taskbar icons load that same file through Electron and always rendered.
def _stage_fn(js: str) -> str:
    fn = js[js.index("function stageShortcutIcon") :]
    return fn[: fn.index("\n}\n")]


def test_a_unc_icon_is_copied_somewhere_the_shell_can_read():
    """The whole fix: a share path is staged onto the local disk first."""
    fn = _stage_fn(_js())
    assert "startsWith('\\\\\\\\')" in fn, "the UNC test itself"
    assert (
        "app.getPath('userData')" in fn
    ), "staged into the dev profile, which is local"
    assert "dev-icon.ico" in fn


def test_a_local_icon_is_used_where_it_lies():
    """The copy is a workaround for the share, not an improvement — a normal
    Windows checkout must keep pointing at its own file, so that a user editing
    MINDFLOCK_DEV_ICON is not shadowed by a stale duplicate."""
    fn = _stage_fn(_js())
    assert "return src" in fn


def test_a_changed_icon_is_restaged_and_an_unchanged_one_is_not():
    """Byte-compared, so an override that changes is picked up and one that has
    not costs a read instead of a write on every dev launch."""
    fn = _stage_fn(_js())
    assert ".equals(from)" in fn
    assert "writeFileSync(dst" in fn


def test_a_failed_stage_falls_back_to_the_exe_icon():
    """Returning a path the shell cannot read is what produced the blank tile in
    the first place; the exe's own icon is wrong but visible."""
    fn = _stage_fn(_js())
    assert "return null" in fn
    # …and the caller has to actually honour that null.
    assert "(ico && stageShortcutIcon(ico)) || process.execPath" in _fn(_js())


def test_the_staged_path_is_what_the_shortcut_is_compared_against():
    """Staleness is checked on `icon`, so the shortcut already on disk — which
    carries the old UNC path — is rewritten on the next dev run rather than
    needing to be deleted by hand."""
    fn = _fn(_js())
    assert "have.icon === want.icon" in fn


def test_the_path_logic_actually_holds_when_run():
    """The one assertion in this file that is not a string match.

    Everything above pins the *wiring*, which is all a Linux CI can normally see
    of a Windows shortcut. But the bug this function exists to fix was a path
    bug — the difference between a share path and a local one — and a structural
    test cannot tell a correct UNC check from an inverted one. ``stage
    ShortcutIcon`` touches nothing but ``fs``/``path`` and ``app.getPath``, so it
    can be lifted out of main.js and run for real against stubs.
    """
    node = shutil.which("node")
    if node is None:  # a checkout without the electron toolchain
        pytest.skip("node is not installed")
    harness = r"""
const fs=require('fs'),path=require('path'),os=require('os');
const src=fs.readFileSync(process.argv[2],'utf8');
const body=src.slice(src.indexOf('function stageShortcutIcon'));
const fn=body.slice(0,body.indexOf('\n}\n')+3);
const dir=fs.mkdtempSync(path.join(os.tmpdir(),'ico-'));
const app={getPath:()=>dir};
const stage=new Function('fs','path','app','console',fn+'; return stageShortcutIcon')(fs,path,app,console);
const out={};
const local=path.join(dir,'src.ico'); fs.writeFileSync(local,Buffer.from('V1'));
out.local_passthrough = stage(local)===local;
out.missing_is_null   = stage(path.join(dir,'nope.ico'))===null;
out.null_is_null      = stage(null)===null;
const unc='\\\\wsl.localhost\\Ubuntu\\home\\u\\app\\electron\\dev-icon.ico';
const rE=fs.existsSync, rR=fs.readFileSync; let content=Buffer.from('V1');
fs.existsSync=(p)=>p===unc?true:rE(p);
fs.readFileSync=(p,...r)=>p===unc?content:rR(p,...r);
const a=stage(unc);
out.unc_is_staged_locally = !!a && a!==unc && !a.startsWith('\\\\');
out.staged_bytes_match    = rR(a).equals(content);
const m=fs.statSync(a).mtimeMs; const b=stage(unc);
out.unchanged_not_rewritten = b===a && fs.statSync(b).mtimeMs===m;
content=Buffer.from('V2-CHANGED');
out.changed_is_restaged = rR(stage(unc)).equals(content);
console.log(JSON.stringify(out));
"""
    with tempfile.TemporaryDirectory() as td:
        hp = Path(td) / "h.js"
        hp.write_text(harness, encoding="utf-8")
        cp = subprocess.run(
            [node, str(hp), str(_MAIN_JS)],
            capture_output=True,
            text=True,
            timeout=60,
        )
    assert cp.returncode == 0, cp.stderr
    got = json.loads(cp.stdout.strip().splitlines()[-1])
    assert all(got.values()), got
