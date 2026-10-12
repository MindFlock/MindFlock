"""backend.fsutil: the shared atomic-write and forgiving JSON-read helpers."""

import json
import os
import stat

import pytest

from backend import fsutil


def _mode(p) -> int:
    return stat.S_IMODE(os.stat(p).st_mode)


def test_atomic_write_json_house_format_and_parent_dir(tmp_path):
    p = tmp_path / "a" / "b" / "doc.json"
    fsutil.atomic_write_json(p, {"é": 1})
    assert (
        p.read_text(encoding="utf-8")
        == json.dumps({"é": 1}, indent=2, ensure_ascii=False) + "\n"
    )
    assert os.listdir(p.parent) == ["doc.json"]


def test_atomic_write_json_passes_dumps_options(tmp_path):
    p = tmp_path / "doc.json"
    fsutil.atomic_write_json(
        p,
        {"b": 1, "a": "é"},
        indent=None,
        newline=False,
        ensure_ascii=True,
        sort_keys=True,
    )
    assert p.read_text() == json.dumps({"b": 1, "a": "é"}, sort_keys=True)


def test_mode_is_exact_and_defaults_to_owner_only(tmp_path):
    old = os.umask(0o077)
    try:
        fsutil.atomic_write_text(tmp_path / "shared", "x", mode=0o644)
    finally:
        os.umask(old)
    assert _mode(tmp_path / "shared") == 0o644
    fsutil.atomic_write_text(tmp_path / "private", "x")
    assert _mode(tmp_path / "private") == 0o600


def test_a_failed_write_keeps_the_old_file_and_no_temp(tmp_path, monkeypatch):
    p = tmp_path / "doc.json"
    p.write_text("old")

    def boom(src, dst):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(fsutil.os, "replace", boom)
    with pytest.raises(OSError):
        fsutil.atomic_write_bytes(p, b"new", prefix=".x.")
    assert p.read_text() == "old"
    assert os.listdir(tmp_path) == ["doc.json"]


def test_read_json_defaults(tmp_path):
    p = tmp_path / "doc.json"
    assert fsutil.read_json(p, {}) == {}  # missing
    p.write_text('{"a": ')
    assert fsutil.read_json(p, "d") == "d"  # corrupt
    p.write_bytes(b"\xff\xfe")
    assert fsutil.read_json(p, "d") == "d"  # not UTF-8
    p.write_text("[1, 2]")
    assert fsutil.read_json(p, None) == [1, 2]
    assert fsutil.read_json(p, {}, expect=dict) == {}  # wrong shape
    assert fsutil.read_json(tmp_path, "d") == "d"  # a directory: OSError
