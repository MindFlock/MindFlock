"""macOS window chrome: native traffic lights + the mirrored top-bar cluster.

The window chrome became platform-conditional here for the first time. Three
surfaces have to agree, and they ship on *separate* release cadences (the shell
is the desktop app, the frontend rides the engine), so each side is pinned:

- ``electron/main.js`` — ``titleBarStyle: 'hidden'`` + ``trafficLightPosition``
  on darwin instead of ``frame: false``, TITLEBAR_JS skipping its own – □ ✕
  there, and the new ``fullscreen-changed`` push.
- ``electron/preload.js`` — the ``mfshell.nativeTitleBar`` capability flag and
  ``winctl.onFullScreenChanged``, which (unlike ``onMaximizedChanged``) returns
  an unsubscribe the React effect depends on.
- the built bundle in ``backend/web/static/`` — same structural style as
  test_frontend_*.py, which is also what proves ``npm run build`` was re-run
  after the TSX/CSS edit.

The live layout is verified with screenshots; these pin the wiring and the
literal strings the three sides share so they can't silently drift apart.
"""

from __future__ import annotations

import re
from pathlib import Path

from fastapi.testclient import TestClient

from backend.web import server

client = TestClient(server.app)

_REPO = Path(__file__).resolve().parents[2]
_MAIN_JS = _REPO / "electron" / "main.js"
_PRELOAD_JS = _REPO / "electron" / "preload.js"
_TOPBAR_TSX = _REPO / "frontend" / "src" / "components" / "TopBar.tsx"
_TOPBAR_CSS = _REPO / "frontend" / "src" / "components" / "TopBar.css"
_SHELL_TS = _REPO / "frontend" / "src" / "lib" / "shell.ts"

# The channel the shell pushes fullscreen state on. Asserted from BOTH sides
# below: a literal-string mismatch between main.js and preload.js fails
# silently at runtime (the listener simply never fires).
_FULLSCREEN_CHANNEL = "fullscreen-changed"


def _src(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _decommented(path: Path) -> str:
    """Source with ``//`` line comments dropped.

    Several of these files *explain* the rejected alternative in prose right
    above the code (main.js talks about ``frame:false`` on macOS in the comment
    that justifies not using it), so "appears only in the else branch" style
    assertions have to look at code alone.
    """
    return "\n".join(
        re.sub(r"(^|\s)//.*$", "", line) for line in _src(path).splitlines()
    )


def _css_block(css: str, selector: str) -> str:
    """The declarations inside ``selector { … }``, or "" when absent."""
    i = css.find(selector + " {")
    if i < 0:
        return ""
    return css[i + len(selector) + 2 :].split("}")[0]


# --------------------------------------------------------------------------- #
# electron/main.js — BrowserWindow chrome is platform-conditional.
# --------------------------------------------------------------------------- #


def test_main_js_uses_native_traffic_lights_on_darwin_only():
    """darwin keeps the REAL red/yellow/green top-left; every other platform
    keeps the frameless window the injected – □ ✕ cluster is drawn into."""
    code = _decommented(_MAIN_JS)
    # One spread ternary carries both arms, so the two can't drift.
    assert "process.platform === 'darwin'" in code
    m = re.search(
        r"\.\.\.\(process\.platform === 'darwin'\s*\?\s*(\{[^}]*\{[^}]*\}[^}]*\})"
        r"\s*:\s*(\{[^}]*\})\)",
        code,
    )
    assert m, "BrowserWindow options no longer branch on platform for the chrome"
    darwin, other = m.group(1), m.group(2)
    assert "titleBarStyle: 'hidden'" in darwin
    assert "trafficLightPosition" in darwin
    assert re.search(r"x:\s*13", darwin) and re.search(r"y:\s*13", darwin)
    # `frame: false` on macOS would remove the traffic lights too — the whole
    # point of the branch. It belongs to the non-darwin arm and nowhere else.
    assert "frame: false" in other
    assert "frame:" not in darwin
    assert code.count("frame: false") == 1


def test_titlebar_js_skips_its_own_controls_on_darwin():
    """The injected cluster must bail BEFORE creating #mf-winctl, else a Mac
    window carries two sets of window controls."""
    js = _src(_MAIN_JS)
    body = js.split("const TITLEBAR_JS = `")[1].split("`\n")[0]
    # Interpolated at build time from the shell's own platform (the string is
    # evaluated in the renderer, which can't see process.platform).
    guard = "if (${process.platform === 'darwin'}) return;"
    assert guard in body
    assert body.index(guard) < body.index(
        "'mf-winctl'"
    ), "the darwin early return must precede any #mf-winctl creation"


def test_main_js_pushes_fullscreen_state_on_both_transitions():
    """macOS hides the traffic lights in fullscreen, so the bar has to give the
    reserved 78px back — both directions, guarded like sendMax."""
    code = _decommented(_MAIN_JS)
    m = re.search(r"const sendFullScreen = \(\) => \{(.*?)\n  \}", code, re.S)
    assert m, "sendFullScreen sender is gone"
    sender = m.group(1)
    # Same destroyed-webContents guard as the older sendMax: the events can
    # arrive while the window is tearing down.
    assert "win && !win.webContents.isDestroyed()" in sender
    assert f"win.webContents.send('{_FULLSCREEN_CHANNEL}'" in sender
    assert "win.isFullScreen()" in sender
    for event in ("enter-full-screen", "leave-full-screen"):
        assert f"win.on('{event}', sendFullScreen)" in code, event


def test_fullscreen_channel_name_matches_on_both_sides_of_the_bridge():
    """A typo here fails silently — the renderer just never hears about it."""
    assert f"send('{_FULLSCREEN_CHANNEL}'" in _src(_MAIN_JS)
    preload = _src(_PRELOAD_JS)
    assert f"ipcRenderer.on('{_FULLSCREEN_CHANNEL}'" in preload
    assert f"removeListener('{_FULLSCREEN_CHANNEL}'" in preload


# --------------------------------------------------------------------------- #
# electron/preload.js — the capability flag + an unsubscribing listener.
# --------------------------------------------------------------------------- #


def test_preload_exposes_native_title_bar_capability_flag():
    """A capability flag, not a platform check, is what the frontend gates on:
    an OLDER mac shell still draws its own – □ ✕ top-right, and must keep the
    layout it was built for (see frontend/src/lib/shell.ts)."""
    code = _decommented(_PRELOAD_JS)
    mfshell = code.split("exposeInMainWorld('mfshell'")[1].split("exposeInMainWorld")[0]
    assert "nativeTitleBar: process.platform === 'darwin'" in mfshell
    # platform stays exposed too — shell.ts's isMacShell() reads it.
    assert "platform: process.platform" in mfshell


def test_preload_fullscreen_listener_lives_on_winctl_and_unsubscribes():
    """frontend/src/lib/shell.ts looks the method up on ``window.winctl`` and
    uses its return value as the React effect's cleanup, so both the placement
    and the returned closure are contract."""
    code = _decommented(_PRELOAD_JS)
    winctl = code.split("exposeInMainWorld('winctl'")[1]
    assert "onFullScreenChanged:" in winctl
    # Not on mfshell — that's the capability/info bridge.
    mfshell = code.split("exposeInMainWorld('mfshell'")[1].split("exposeInMainWorld")[0]
    assert "onFullScreenChanged" not in mfshell

    m = re.search(r"onFullScreenChanged: \(cb\) => \{(.*?)\n  \}", winctl, re.S)
    assert m, "onFullScreenChanged is no longer a block-bodied subscriber"
    body = m.group(1)
    # The handler is hoisted to a named const and the SAME reference is removed,
    # so repeated mount/unmount cycles can't leak IPC listeners. A
    # removeAllListeners would also tear down any other subscriber.
    assert re.search(r"const (\w+) = \(_e, isFull\) => cb\(isFull\)", body)
    handler = re.search(r"const (\w+) = \(_e, isFull\) => cb\(isFull\)", body).group(1)
    assert f"ipcRenderer.on('{_FULLSCREEN_CHANNEL}', {handler})" in body
    assert (
        f"return () => ipcRenderer.removeListener('{_FULLSCREEN_CHANNEL}', {handler})"
        in body
    )
    assert "removeAllListeners" not in body


# --------------------------------------------------------------------------- #
# frontend/src/lib/shell.ts — bridge-gated, never user-agent-gated.
# --------------------------------------------------------------------------- #


def test_shell_ts_gates_on_the_bridge_not_the_user_agent():
    """A Mac user in Safari has browser chrome of its own and no traffic lights
    to dodge, so the layout must not shift for them."""
    ts = _src(_SHELL_TS)
    # Every check reads the preload bridge off `window` (typed through a cast,
    # hence no bare `window.mfshell` literal).
    assert "mfshell?: ShellBridge }).mfshell" in ts
    assert "winctl?: WinCtl }).winctl" in ts
    for forbidden in ("navigator", "userAgent", "Macintosh", "MacIntel"):
        assert forbidden not in ts, forbidden
    # Strict === true: a sloppy future bridge value ("true", 1) must not move
    # the layout.
    assert "nativeTitleBar === true" in ts
    # The subscribe result is only trusted when it really is callable.
    assert 'typeof off === "function" ? off : () => {}' in ts


# --------------------------------------------------------------------------- #
# TopBar.tsx — one cluster definition, two placements.
# --------------------------------------------------------------------------- #


def test_topbar_marks_the_mac_layout_with_two_separate_attributes():
    """``data-mac`` (mirrored layout) and ``data-mac-lights`` (reserve room for
    the lights) are deliberately distinct: fullscreen drops the second only."""
    tsx = _src(_TOPBAR_TSX)
    assert 'data-mac={mac ? "" : undefined}' in tsx
    assert 'data-mac-lights={mac && !fullScreen ? "" : undefined}' in tsx
    # The capability flag decides, evaluated once — a window can't grow or lose
    # its title bar mid-run.
    assert "useState(hasNativeWindowControls)" in tsx


def test_topbar_tracks_fullscreen_only_in_a_native_title_bar_shell():
    """The fullscreen effect is gated on the capability flag (a non-mac renderer
    registers no IPC listener at all), and it takes BOTH readings: the initial
    state has to be pulled, because the push only carries transitions and a
    window already fullscreen at load never sees one."""
    tsx = _src(_TOPBAR_TSX)
    effect = tsx.split("useEffect(() => {\n    if (!mac) return;", 1)
    assert len(effect) == 2, "the fullscreen effect must bail out when !mac"
    body = effect[1].split("}, [mac]);", 1)[0]
    assert "isFullScreen()" in body  # initial state, pulled
    assert "onFullScreenChanged(setFullScreen)" in body  # transitions, pushed
    assert "off()" in body  # and unsubscribed on unmount


def test_topbar_defines_the_swapping_cluster_exactly_once():
    """The point of hoisting logo / theme / bell into consts: the two layouts
    render the same elements and can't drift apart."""
    tsx = _src(_TOPBAR_TSX)
    assert tsx.count('id="brand-logo"') == 1
    assert tsx.count('id="theme-btn"') == 1
    assert tsx.count("onClick={toggleTheme}") == 1
    assert tsx.count("<NotificationsBell />") == 2  # one per placement, gated
    # Left-hand placement (Windows/Linux/browser) is gated off on mac...
    for gated in (
        "{!mac && logo}",
        "{!mac && themeToggle}",
        "{!mac && <NotificationsBell />}",
    ):
        assert gated in tsx, gated
    # ...and the mirrored .tb-end exists only there.
    assert '{mac && (\n        <div className="tb-end">' in tsx


def test_topbar_mac_cluster_sits_after_the_drag_region():
    """.tb-end has to follow .tb-drag so the flexible handle keeps the width and
    the cluster hugs the right edge."""
    tsx = _src(_TOPBAR_TSX)
    assert tsx.index('className="tb-drag"') < tsx.index('className="tb-end"')


# --------------------------------------------------------------------------- #
# TopBar.css / the shipped stylesheet — and that they are the SAME rules.
# --------------------------------------------------------------------------- #


def test_style_css_ships_the_mac_topbar_rules():
    css = client.get("/style.css").text
    assert "#topbar[data-mac-lights]" in css
    assert "#topbar[data-mac] .tb-end" in css
    # The gap is keyed on data-mac-lights, NOT data-mac — otherwise fullscreen
    # (where macOS hides the lights) leaves a hole no button occupies.
    assert "padding-left" in _css_block(css, "#topbar[data-mac-lights]")
    assert "padding-left" not in _css_block(css, "#topbar[data-mac]")


def test_shipped_light_gap_matches_the_topbar_css_source():
    """Catches a TopBar.css edit committed without ``npm run build``."""
    served = _css_block(client.get("/style.css").text, "#topbar[data-mac-lights]")
    source = _css_block(_src(_TOPBAR_CSS), "#topbar[data-mac-lights]")
    assert source, "the data-mac-lights rule vanished from TopBar.css"
    pad = re.compile(r"padding-left:\s*([\w.]+)")
    assert pad.search(served) and pad.search(source)
    assert pad.search(served).group(1) == pad.search(source).group(1)
    # 13px origin + 3 x 12px buttons + 2 x 8px gaps + breathing room.
    assert pad.search(source).group(1) == "78px"


# --------------------------------------------------------------------------- #
# The built bundle carries the change (i.e. it was actually rebuilt).
# --------------------------------------------------------------------------- #


def test_app_js_bundle_was_rebuilt_with_the_mac_title_bar_sources():
    js = client.get("/app.js").text
    # From TopBar.tsx…
    assert '"data-mac-lights"' in js
    assert '"data-mac"' in js
    assert '"tb-end"' in js
    # …and from lib/shell.ts, strict-equality intact through the build.
    assert "nativeTitleBar" in js
    assert re.search(r"nativeTitleBar\)? === true", js)
    assert "onFullScreenChanged" in js


def test_app_js_bundle_builds_the_swapping_cluster_once():
    """Guards against a future edit that duplicates the cluster in the built
    output instead of reusing the hoisted element."""
    js = client.get("/app.js").text
    assert js.count('id: "theme-btn"') == 1
    assert js.count('id: "brand-logo"') == 1
    # Still positioned after the drag handle in the emitted tree.
    assert js.index('"tb-drag"') < js.index('"tb-end"')
