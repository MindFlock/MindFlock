"""The desktop app signs in to its OWN server.

Turning on Tailscale mode turns the access-token gate on, and the gate answers
the desktop window with the sign-in page like any other client — while the
token it asks for sits in Settings, behind that page. ``electron/main.js``
recognises the page by its ``<title>``, reads the token with ``mindflock
token`` (falling back to grepping ``settings.json`` for engines older than that
command) and reloads with ``?token=``.

Pinned here: the title both sides agree on, the hook into ``did-finish-load``,
the ``mindflock token`` command, and — when ``node`` is available — the shell
snippet main.js actually runs, against a throwaway HOME.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest

from backend import cli
from backend.web.core import auth

_REPO = Path(__file__).resolve().parents[2]
_MAIN_JS = _REPO / "electron" / "main.js"


def _main_js() -> str:
    return _MAIN_JS.read_text(encoding="utf-8")


def test_sign_in_title_matches_the_login_page():
    m = re.search(r"const SIGN_IN_TITLE = '([^']*)'", _main_js())
    assert m, "main.js lost its SIGN_IN_TITLE constant"
    assert "<title>%s</title>" % m.group(1) in auth.login_page_html()


def test_auto_sign_in_runs_on_every_app_load():
    js = _main_js()
    start = js.index("win.webContents.on('did-finish-load'")
    handler = js[start : js.index("win.webContents.on(", start + 1)]
    assert "autoSignIn()" in handler


def test_loaded_log_line_never_carries_the_token_query():
    # A refused auto sign-in leaves `?token=` in the window URL.
    assert (
        "console.log('[mindflock] loaded:', win.webContents.getURL())" not in _main_js()
    )


def test_login_page_no_longer_points_at_the_startup_banner():
    html = auth.login_page_html()
    assert "startup banner" not in html
    assert "mindflock token" in html


# --------------------------------------------------------------------------- #
# mindflock token
# --------------------------------------------------------------------------- #
def test_token_prints_the_settings_token(tmp_path, monkeypatch, capsys):
    settings = tmp_path / "settings.json"
    settings.write_text(json.dumps({"general": {"auth_token": "tok-from-file"}}))
    monkeypatch.delenv("MINDFLOCK_AUTH_TOKEN", raising=False)
    monkeypatch.setenv("MINDFLOCK_SETTINGS_FILE", str(settings))
    assert cli.main(["token"]) == 0
    assert capsys.readouterr().out == "tok-from-file\n"


def test_token_prefers_the_env_token_like_the_server(tmp_path, monkeypatch, capsys):
    settings = tmp_path / "settings.json"
    settings.write_text(json.dumps({"general": {"auth_token": "tok-from-file"}}))
    monkeypatch.setenv("MINDFLOCK_SETTINGS_FILE", str(settings))
    monkeypatch.setenv("MINDFLOCK_AUTH_TOKEN", "tok-from-env")
    assert cli.main(["token"]) == 0
    assert capsys.readouterr().out == "tok-from-env\n"


def test_token_without_one_exits_1_with_a_hint(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("MINDFLOCK_AUTH_TOKEN", raising=False)
    monkeypatch.setenv("MINDFLOCK_SETTINGS_FILE", str(tmp_path / "missing.json"))
    assert cli.main(["token"]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "no access token yet" in captured.err


# --------------------------------------------------------------------------- #
# The shell snippet main.js runs (needs node to evaluate the JS that builds it)
# --------------------------------------------------------------------------- #
_EVAL_JS = textwrap.dedent(r"""
    const src = require('fs').readFileSync(process.argv[1], 'utf8')
    const grab = (name) => {
      const i = src.indexOf('function ' + name + '(')
      let depth = 0
      for (let k = src.indexOf('{', i); k < src.length; k++) {
        if (src[k] === '{') depth++
        else if (src[k] === '}' && --depth === 0) return src.slice(i, k + 1)
      }
    }
    const WSL_REPO = ''
    eval(grab('shq')); eval(grab('tokenScript')); eval(grab('lastTokenLine'))
    const { spawnSync } = require('child_process')
    const r = spawnSync('/bin/bash', ['-c', tokenScript()], { env: { HOME: process.argv[2], PATH: '/usr/bin:/bin' } })
    process.stdout.write(lastTokenLine(r.stdout.toString()))
    """)


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
@pytest.mark.parametrize("indent", [None, 2])
def test_token_script_falls_back_to_the_settings_file(tmp_path, indent):
    """No `mindflock` on PATH (or one too old for `token`): the grep fallback
    still finds the token in compact and pretty-printed settings.json."""
    (tmp_path / ".mindflock").mkdir()
    (tmp_path / ".mindflock" / "settings.json").write_text(
        json.dumps(
            {"general": {"serve_mode": "tailscale", "auth_token": "Ab_9-xyz"}},
            indent=indent,
        )
    )
    out = subprocess.run(
        ["node", "-e", _EVAL_JS, str(_MAIN_JS), str(tmp_path)],
        capture_output=True,
        text=True,
        timeout=30,
        check=True,
    ).stdout
    assert out == "Ab_9-xyz"
