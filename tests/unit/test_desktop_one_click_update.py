"""One-click desktop update: the app AND its engine from one button.

The desktop app used to open the GitHub release page; now ``electron/main.js``
downloads the release in the background (electron-updater) and the toast's
**Update** installs it — after updating the engine to the same release. Live
verification (2026-10-07): a CI-signed 0.7.90 test build updated itself to
0.7.91 on macOS (Squirrel.Mac, same self-signed identity, no quarantine left)
and on Windows (silent NSIS, ``--updated``), with the engine teardown in the
old uninstaller proven NOT to run on update.

These pin the wiring that would silently break it. The electron shell has no JS
test harness, so — like ``test_desktop_engine_check.py`` — they read the source.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import yaml
from fastapi.testclient import TestClient

from backend.web import server

_REPO = Path(__file__).resolve().parents[2]
_MAIN_JS = (_REPO / "electron" / "main.js").read_text(encoding="utf-8")
_PRELOAD = (_REPO / "electron" / "preload.js").read_text(encoding="utf-8")
_PKG = json.loads((_REPO / "electron" / "package.json").read_text(encoding="utf-8"))
_NSH = (_REPO / "electron" / "build" / "installer.nsh").read_text(encoding="utf-8")
_RELEASE = (_REPO / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8")


def _fn(js: str, name: str) -> str:
    start = js.index(name)
    depth = 0
    for k in range(js.index("{", start), len(js)):
        if js[k] == "{":
            depth += 1
        elif js[k] == "}":
            depth -= 1
            if depth == 0:
                return js[start : k + 1]
    raise AssertionError(name)


# --- packaging ----------------------------------------------------------------


def test_the_updater_ships_with_the_app():
    # A runtime dependency, not a dev one: electron-builder only packs these.
    assert "electron-updater" in _PKG.get("dependencies", {})
    assert _PKG["build"]["publish"][0] == {
        "provider": "github",
        "owner": "MindFlock",
        "repo": "MindFlock",
        "releaseType": "release",
    }


def test_download_button_names_are_unchanged():
    # The website's buttons link version-less asset names; the update zip is new.
    b = _PKG["build"]
    assert b["win"]["artifactName"] == "MindFlock-Setup.exe"
    assert b["linux"]["artifactName"] == "MindFlock.AppImage"
    assert b["mac"]["artifactName"] == "MindFlock.${ext}"  # MindFlock.dmg / .zip
    targets = {t["target"] for t in b["mac"]["target"]}
    assert targets == {"dmg", "zip"}  # Squirrel.Mac updates from the zip


def test_release_publishes_the_update_metadata():
    wf = yaml.safe_load(_RELEASE)
    upload = next(
        s
        for s in wf["jobs"]["desktop"]["steps"]
        if s.get("name") == "Upload desktop artifacts to the release"
    )
    assert "dist/latest*.yml" in upload["run"] and "dist/*.blockmap" in upload["run"]
    assert "no latest*.yml" in upload["run"]  # missing metadata fails the release
    mac = next(
        m
        for m in wf["jobs"]["desktop"]["strategy"]["matrix"]["include"]
        if m["os"] == "macos-latest"
    )
    assert "dist/*.zip" in mac["artifacts"]


def test_a_tagged_mac_build_must_be_signed():
    # One unsigned release would fail Squirrel's same-identity check on every
    # installed Mac, stranding them all.
    wf = yaml.safe_load(_RELEASE)
    step = next(
        s
        for s in wf["jobs"]["desktop"]["steps"]
        if s.get("name") == "Refuse an unsigned macOS release"
    )
    assert "github.ref_type == 'tag'" in step["if"]
    assert "exit 1" in step["run"]


def test_an_update_never_tears_the_engine_down():
    # electron-builder runs the OLD uninstaller during every update; without
    # this guard each update would `mindflock uninstall` the user's engine.
    body = _NSH[_NSH.index("!macro customUnInstall") :]
    body = body[: body.index("!macroend")]
    assert body.strip().splitlines()[1].strip() == "${ifNot} ${isUpdated}"
    assert "TEARDOWN-RAN" not in _NSH  # the live test's marker never ships


# --- main process ---------------------------------------------------------------


def test_updater_downloads_in_background_but_never_installs_on_quit():
    init = _fn(_MAIN_JS, "function initAutoUpdater")
    assert "autoUpdater.autoDownload = true" in init
    # A plain quit must not swap the app: the engine only moves with the button.
    assert "autoUpdater.autoInstallOnAppQuit = false" in init
    assert "!app.isPackaged" in init  # dev runs never self-update


def test_one_click_order_engine_then_stop_server_then_app():
    run = _fn(_MAIN_JS, "async function updateEverything")
    engine = run.index("startInstall(plan.ref)")
    stop = run.index("spawnEngineShell(KILL_SERVER)")
    swap = run.index("autoUpdater.quitAndInstall(true, true)")
    assert engine < stop < swap
    # A failed engine update stops before touching the app.
    failed = run.index("The engine update failed")
    assert failed < swap
    # Can't self-update: engine still updated, then the download page.
    assert "shell.openExternal" in run


def test_engine_step_leaves_developer_engines_alone():
    plan = _fn(_MAIN_JS, "async function enginePlan")
    assert "if (WSL_REPO)" in plan
    assert "/api/update/check" in plan
    assert "kind !== 'uv-tool'" in plan
    # Unknown kind is NOT treated as a release install.
    assert re.search(r"if \(!kind\) return \{ needed: false", plan)


def test_the_engine_check_carries_the_access_token(monkeypatch):
    # enginePlan reads /api/update/check with the local token — under the
    # access-token gate (Tailscale mode) a tokenless read 401s and the engine
    # step would silently be skipped.
    assert "readEngineToken()" in _fn(_MAIN_JS, "async function enginePlan")
    assert "Authorization: 'Bearer ' + token" in _fn(
        _MAIN_JS, "function fetchLocalJSON"
    )
    monkeypatch.setenv("MINDFLOCK_AUTH_TOKEN", "gate-is-on")
    monkeypatch.delenv("MINDFLOCK_AUTH", raising=False)
    client = TestClient(server.app)
    assert client.get("/api/update/check").status_code == 401
    ok = client.get("/api/update/check", headers={"Authorization": "Bearer gate-is-on"})
    assert ok.status_code == 200 and "kind" in ok.json()


def test_toast_installs_instead_of_opening_github():
    assert "install: () => ipcRenderer.invoke('update:install')" in _PRELOAD
    assert "state: () => ipcRenderer.invoke('update:state')" in _PRELOAD
    show = _MAIN_JS[_MAIN_JS.index("function show(info)") :]
    show = show[: show.index("// --- engine update available")]
    assert "window.mfupdate.install()" in show
    # The download page is only the fallback for a build without the bridge.
    assert show.count("openDownload") == 1
    assert "if (!window.mfupdate.install)" in show


def test_one_toast_not_two_when_the_app_is_behind():
    assert "if (pendingUpdate()) return" in _fn(_MAIN_JS, "function pushEngineNotice")
