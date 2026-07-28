"""Unit tests for scripts/bump-version.py — the release-gate that keeps the
three version manifests (pyproject.toml, electron/package.json,
frontend/package.json) in agreement and, on tagged builds, matching the tag.

The script is loaded by path because its filename carries a hyphen and cannot
be imported as a normal module. Its ``MANIFESTS`` global is repointed at temp
files so the real repo manifests are never read or written.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "bump-version.py"
_spec = importlib.util.spec_from_file_location("bump_version", _SCRIPT)
bv = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(bv)


def _fake_manifests(tmp_path, py, electron, frontend):
    """Write three throwaway manifests and return a MANIFESTS-shaped list that
    reuses the script's own compiled patterns but points at the temp files."""
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text(f'[project]\nname = "x"\nversion = "{py}"\n', encoding="utf-8")
    el = tmp_path / "electron_package.json"
    el.write_text(
        '{\n  "name": "e",\n  "version": "%s"\n}\n' % electron, encoding="utf-8"
    )
    fe = tmp_path / "frontend_package.json"
    fe.write_text(
        '{\n  "name": "f",\n  "version": "%s"\n}\n' % frontend, encoding="utf-8"
    )
    _, _, py_pat = bv.MANIFESTS[0]
    _, _, el_pat = bv.MANIFESTS[1]
    _, _, fe_pat = bv.MANIFESTS[2]
    return [
        (pyproject, "pyproject.toml", py_pat),
        (el, "electron/package.json", el_pat),
        (fe, "frontend/package.json", fe_pat),
    ]


@pytest.fixture
def manifests(tmp_path, monkeypatch):
    """Repoint the script at temp manifests; a test sets the three versions."""

    def _set(py, electron=None, frontend=None):
        monkeypatch.setattr(
            bv,
            "MANIFESTS",
            _fake_manifests(tmp_path, py, electron or py, frontend or py),
        )

    return _set


class TestCheck:
    def test_check_passes_when_all_agree(self, manifests, capsys):
        manifests("0.1.3")
        assert bv.main(["--check"]) == 0
        assert "ok: all manifests at 0.1.3" in capsys.readouterr().out

    def test_check_fails_on_drift(self, manifests, capsys):
        # electron drifted a patch ahead — the shared-version guard aborts.
        manifests("0.1.3", electron="0.1.4")
        with pytest.raises(SystemExit) as exc:
            bv.main(["--check"])
        assert exc.value.code != 0
        assert "version drift" in str(exc.value.code)


class TestCheckExpect:
    def test_expect_mismatch_fails(self, manifests, capsys):
        # A v0.1.4 tag on 0.1.3 code is the classic tag/code mismatch → exit 1.
        manifests("0.1.3")
        assert bv.main(["--check", "--expect", "v0.1.4"]) == 1
        assert "expected 0.1.4" in capsys.readouterr().err

    def test_expect_match_passes(self, manifests, capsys):
        manifests("0.1.3")
        assert bv.main(["--check", "--expect", "v0.1.3"]) == 0

    def test_expect_tolerates_missing_leading_v(self, manifests):
        manifests("0.1.3")
        assert bv.main(["--check", "--expect", "0.1.3"]) == 0


class TestPureHelpers:
    def test_shared_version_returns_common(self):
        assert (
            bv.shared_version(
                {"pyproject.toml": "1.2.3", "electron": "1.2.3", "frontend": "1.2.3"}
            )
            == "1.2.3"
        )

    def test_shared_version_exits_on_disagreement(self):
        with pytest.raises(SystemExit):
            bv.shared_version({"a": "1.0.0", "b": "1.0.1"})

    def test_bump_keywords(self):
        assert bv.bump("0.1.3", "patch") == "0.1.4"
        assert bv.bump("0.1.3", "minor") == "0.2.0"
        assert bv.bump("0.1.3", "major") == "1.0.0"
        # Pre-release / build suffixes are dropped before bumping.
        assert bv.bump("0.1.3-rc1+build", "patch") == "0.1.4"

    def test_write_manifests_round_trips(self, manifests, capsys):
        manifests("0.1.3")
        bv.write_manifests("0.9.0")
        assert bv.shared_version(bv.read_all()) == "0.9.0"
