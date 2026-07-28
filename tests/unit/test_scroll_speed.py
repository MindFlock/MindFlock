"""Terminal scroll-speed setting: persistence, clamping, and the tmux binding.

The web terminals run tmux with mouse-on, so the wheel scrolls copy-mode; this
setting tunes the copy-mode WheelUp/DownPane binding's line count.
"""

from __future__ import annotations

from backend.web.core import terminal


def test_load_default_and_clamp(monkeypatch, tmp_path):
    monkeypatch.setattr(terminal, "SCROLL_SPEED_PATH", tmp_path / "scroll-speed")
    assert terminal.load_scroll_speed() == 1  # default when unset
    assert terminal.save_scroll_speed(2) == 2
    assert terminal.load_scroll_speed() == 2
    assert terminal.save_scroll_speed(999) == 3  # clamp high
    assert terminal.save_scroll_speed(0) == 0.3333  # clamp low (1/3)
    assert terminal.save_scroll_speed("abc") == 1  # garbage -> default


def test_fractional_speeds(monkeypatch, tmp_path):
    """Speeds snap to thirds of a line across the whole 0.33-3 range; the
    fractional part is enforced browser-side (tmux -N counts are integers)."""
    monkeypatch.setattr(terminal, "SCROLL_SPEED_PATH", tmp_path / "scroll-speed")
    assert terminal.save_scroll_speed(1 / 3) == 0.3333
    assert terminal.load_scroll_speed() == 0.3333  # round-trips through the file
    assert terminal.save_scroll_speed(0.5) == 0.6667  # snapped to nearest third
    assert terminal.save_scroll_speed(2.4) == 2.3333  # thirds above 1 too
    assert terminal.save_scroll_speed(3) == 3  # whole values stay whole
    assert terminal.save_scroll_speed(float("nan")) == 1  # NaN -> default


def test_apply_floors_sub_line_speed_at_one(monkeypatch, tmp_path):
    """tmux can't scroll less than a line: a 0.25 speed still binds -N 1 and a
    single root forward — the fraction is enforced by the browser instead."""
    monkeypatch.setattr(terminal, "SCROLL_SPEED_PATH", tmp_path / "scroll-speed")
    monkeypatch.setattr(terminal, "_tmux_server_running", lambda: True)
    cmds = []
    monkeypatch.setattr(
        terminal.subprocess, "run", lambda argv, *a, **k: cmds.append(argv)
    )
    terminal.apply_scroll_speed(0.25)
    copy_cmds = [
        c for c in cmds if c[c.index("-T") + 1] in ("copy-mode", "copy-mode-vi")
    ]
    root_cmds = [c for c in cmds if c[c.index("-T") + 1] == "root"]
    for c in copy_cmds:
        assert c[c.index("-N") + 1] == "1"
    for c in root_cmds:
        assert c[c.index("-F") + 2] == "send-keys -M"


def test_apply_noop_without_tmux_server(monkeypatch, tmp_path):
    monkeypatch.setattr(terminal, "SCROLL_SPEED_PATH", tmp_path / "scroll-speed")
    monkeypatch.setattr(terminal, "_tmux_server_running", lambda: False)
    calls = []
    monkeypatch.setattr(terminal.subprocess, "run", lambda *a, **k: calls.append(a))
    terminal.apply_scroll_speed(5)
    assert calls == []  # nothing bound when no server is running


def test_apply_binds_copy_mode_wheel(monkeypatch, tmp_path):
    monkeypatch.setattr(terminal, "SCROLL_SPEED_PATH", tmp_path / "scroll-speed")
    monkeypatch.setattr(terminal, "_tmux_server_running", lambda: True)
    cmds = []

    def fake_run(argv, *a, **k):
        cmds.append(argv)

        class R:  # noqa: D401 - minimal CompletedProcess stand-in
            returncode = 0

        return R()

    monkeypatch.setattr(terminal.subprocess, "run", fake_run)
    terminal.apply_scroll_speed(3)

    # 4 copy-mode binds (2 tables x 2 directions) + 2 root binds (up/down).
    assert len(cmds) == 6
    copy_cmds = [
        c for c in cmds if c[c.index("-T") + 1] in ("copy-mode", "copy-mode-vi")
    ]
    root_cmds = [c for c in cmds if c[c.index("-T") + 1] == "root"]

    # Copy-mode: both tables, both directions, scrolling -N 3 lines.
    assert len(copy_cmds) == 4
    assert {c[c.index("-T") + 1] for c in copy_cmds} == {"copy-mode", "copy-mode-vi"}
    assert {c[4] for c in copy_cmds} == {"WheelUpPane", "WheelDownPane"}
    for c in copy_cmds:
        assert c[c.index("-N") + 1] == "3"
        assert c[-1] in ("scroll-up", "scroll-down")

    # Root: both wheel keys forward the event 3x (speed copies of "send-keys -M").
    assert len(root_cmds) == 2
    assert {c[4] for c in root_cmds} == {"WheelUpPane", "WheelDownPane"}
    for c in root_cmds:
        assert "if-shell" in c
        forward = c[c.index("-F") + 2]  # the "then" branch follows the -F condition
        assert forward == " ; ".join(["send-keys -M"] * 3)
