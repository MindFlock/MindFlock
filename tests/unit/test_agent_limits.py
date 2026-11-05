"""backend.providers.agent_limits — per-agent memory scopes + the gitnexus gate.

Pinned here:
  * size settings parse (percent of RAM, K/M/G, off) and junk falls back to
    the defaults; ``general.agent_memory`` round-trips through settings;
  * the systemd-run argv (slice, unit, only non-zero limits) and how
    :func:`wrap_launch` composes it, and that every off switch / failure
    leaves the launch string untouched;
  * the probe is cached, honours ``MINDFLOCK_AGENT_SCOPE=0`` and puts the
    fleet limits on the slice once;
  * the gitnexus shim: valid sh, passes every non-analyze call straight to the
    real binary, caps the heap / raises oom_score_adj for analyze, keeps a
    user's own heap flag, and never resolves itself as "the real gitnexus".

No real systemd scope is started: subprocess.run is faked throughout.
"""

from __future__ import annotations

import os
import subprocess
import sys

import pytest

from backend.config.settings import GeneralSettings
from backend.providers import agent_limits as al

GB = 1 << 30


@pytest.fixture(autouse=True)
def _fresh(monkeypatch, tmp_path):
    monkeypatch.setenv("MINDFLOCK_RUN_DIR", str(tmp_path / "run"))
    monkeypatch.delenv("MINDFLOCK_AGENT_SCOPE", raising=False)
    monkeypatch.delenv("MINDFLOCK_GITNEXUS_GATE", raising=False)
    monkeypatch.setattr(al, "_PROBE", None)
    monkeypatch.setattr(al, "_settings_blob", lambda: {})


# --- sizes and settings -------------------------------------------------------


@pytest.mark.parametrize(
    "spec,want",
    [
        ("30%", 6 * GB),
        ("100%", 20 * GB),
        ("250%", 20 * GB),  # clamped to RAM
        ("6G", 6 * GB),
        ("512m", 512 << 20),
        ("1.5GiB", int(1.5 * GB)),
        ("4096", 4096),
        ("off", 0),
        ("0", 0),
        ("0%", 0),
        ("", None),
        ("lots", None),
        ("-5G", None),
    ],
)
def test_parse_size(spec, want):
    assert al.parse_size(spec, 20 * GB) == want


def test_percent_needs_known_ram():
    assert al.parse_size("30%", 0) is None


def test_resolved_limits_defaults_and_fallback():
    lim = al.resolved_limits({**al.DEFAULTS, "max": "garbage", "swap": "off"}, 20 * GB)
    assert lim["high"] == 6 * GB
    assert lim["max"] == 8 * GB  # unreadable -> default 40%
    assert lim["swap"] == 0
    assert lim["fleet_max"] == 15 * GB


def test_clean_config_keeps_only_known_good_values():
    raw = {
        "enabled": False,
        "high": " 5G ",
        "max": "huge",
        "swap": 0,
        "fleet_max": "70%",
        "gitnexus_heap_mb": "2048",
        "gitnexus_at_once": -1,
        "bogus": 1,
    }
    assert al.clean_config(raw) == {
        "enabled": False,
        "high": "5G",
        "swap": "0",
        "fleet_max": "70%",
        "gitnexus_heap_mb": 2048,
    }
    assert al.clean_config("nope") == {}


def test_general_settings_round_trip():
    g = GeneralSettings.from_dict({"agent_memory": {"max": "6G", "junk": 1}})
    assert g.agent_memory == {"max": "6G"}
    assert g.to_dict()["agent_memory"] == {"max": "6G"}
    assert "agent_memory" not in GeneralSettings.from_dict({}).to_dict()


def test_agent_memory_is_per_device():
    from backend.web.core import settings_sync

    src = open(settings_sync.__file__, encoding="utf-8").read()
    assert '"agent_memory"' in src


# --- the scope ----------------------------------------------------------------


def test_scope_command_shape(monkeypatch):
    monkeypatch.setattr(al.time, "time_ns", lambda: 1_700_000_000_000_000_000)
    argv = al.scope_command(
        "mindflock_td docs!",
        {"high": 2 * GB, "max": 3 * GB, "swap": 0},
        "/x/systemd-run",
    )
    assert argv == [
        "/x/systemd-run",
        "--user",
        "--scope",
        "--quiet",
        "--collect",
        "--slice=app-mindflock.slice",
        "--unit=mindflock-mindflock_td_docs_-1700000000000",
        "-p",
        "MemoryHigh=%d" % (2 * GB),
        "-p",
        "MemoryMax=%d" % (3 * GB),
        "--",
    ]


class _Runs:
    def __init__(self, rc=0):
        self.calls = []
        self.rc = rc

    def __call__(self, argv, **kw):
        self.calls.append(list(argv))
        return subprocess.CompletedProcess(argv, self.rc)


def test_probe_cached_and_applies_fleet_limits_once(monkeypatch):
    runs = _Runs(0)
    monkeypatch.setattr(al.subprocess, "run", runs)
    monkeypatch.setattr(al, "_systemd_run", lambda: "/usr/bin/systemd-run")
    monkeypatch.setattr(al.shutil, "which", lambda name: "/usr/bin/" + name)
    assert al.scope_available() is True
    assert al.scope_available() is True  # cached: no second probe
    probes = [c for c in runs.calls if c[0].endswith("systemd-run")]
    sets = [c for c in runs.calls if c[0].endswith("systemctl")]
    assert len(probes) == 1 and probes[0][-2:] == ["--", "true"]
    assert "--slice=app-mindflock.slice" in probes[0]
    assert len(sets) == 1
    assert sets[0][:5] == [
        "/usr/bin/systemctl",
        "--user",
        "set-property",
        "--runtime",
        "app-mindflock.slice",
    ]
    assert any(p.startswith("MemoryMax=") for p in sets[0])


def test_probe_failure_means_no_scope(monkeypatch):
    runs = _Runs(1)
    monkeypatch.setattr(al.subprocess, "run", runs)
    monkeypatch.setattr(al, "_systemd_run", lambda: "/usr/bin/systemd-run")
    assert al.scope_available() is False
    assert not any(c[0].endswith("systemctl") for c in runs.calls)


def test_no_systemd_run_means_no_scope(monkeypatch):
    monkeypatch.setattr(al, "_systemd_run", lambda: None)
    monkeypatch.setattr(al.subprocess, "run", _Runs(0))
    assert al.scope_available() is False


def test_env_off_skips_the_probe(monkeypatch):
    runs = _Runs(0)
    monkeypatch.setattr(al.subprocess, "run", runs)
    monkeypatch.setenv("MINDFLOCK_AGENT_SCOPE", "0")
    assert al.scope_available() is False
    assert runs.calls == []


# --- wrap_launch ----------------------------------------------------------------


def test_wrap_launch_composes_scope_and_path(monkeypatch):
    monkeypatch.setattr(al, "scope_available", lambda: True)
    monkeypatch.setattr(al, "_systemd_run", lambda: "/usr/bin/systemd-run")
    monkeypatch.setattr(al, "unit_name", lambda n: "mindflock-%s-1" % n)
    monkeypatch.setattr(al, "_total_memory", lambda: 20 * GB)
    monkeypatch.setattr(al, "gitnexus_shim_dir", lambda cfg=None: "/r/bin")
    out = al.wrap_launch("claude --x 'a b'", "s1")
    assert out == (
        'PATH=/r/bin:"$PATH"; export PATH; '
        "/usr/bin/systemd-run --user --scope --quiet --collect "
        "--slice=app-mindflock.slice --unit=mindflock-s1-1 "
        "-p MemoryHigh=%d -p MemoryMax=%d -p MemorySwapMax=%d -- "
        "sh -c 'claude --x '\"'\"'a b'\"'\"''"
        % (int(20 * GB * 0.30), int(20 * GB * 0.40), int(20 * GB * 0.15))
    )


def test_wrap_launch_scope_runs_the_command(monkeypatch, tmp_path):
    """The composed string is real sh: with a stand-in systemd-run that just
    execs what follows ``--``, the command runs and its status comes back."""
    fake = tmp_path / "systemd-run"
    fake.write_text(
        '#!/bin/sh\nwhile [ "$1" != -- ]; do shift; done; shift; exec "$@"\n'
    )
    fake.chmod(0o755)
    monkeypatch.setattr(al, "scope_available", lambda: True)
    monkeypatch.setattr(al, "_systemd_run", lambda: str(fake))
    monkeypatch.setattr(al, "gitnexus_shim_dir", lambda cfg=None: None)
    out = al.wrap_launch('echo "it\'s $((1+2))"; exit 7', "s")
    r = subprocess.run(["sh", "-c", out], capture_output=True, text=True)
    assert r.returncode == 7
    assert r.stdout == "it's 3\n"


def test_wrap_launch_unchanged_when_off(monkeypatch):
    monkeypatch.setattr(al, "scope_available", lambda: True)
    monkeypatch.setattr(al, "gitnexus_shim_dir", lambda cfg=None: "/r/bin")
    monkeypatch.setattr(al, "_settings_blob", lambda: {"enabled": False})
    assert al.wrap_launch("claude", "s") == "claude"


def test_wrap_launch_unchanged_when_nothing_applies(monkeypatch):
    monkeypatch.setattr(al, "scope_available", lambda: False)
    monkeypatch.setattr(al, "gitnexus_shim_dir", lambda cfg=None: None)
    assert al.wrap_launch("claude", "s") == "claude"


def test_wrap_launch_never_raises(monkeypatch):
    def boom():
        raise RuntimeError("bus gone")

    monkeypatch.setattr(al, "scope_available", boom)
    assert al.wrap_launch("claude", "s") == "claude"


def test_terminal_wrapper_puts_guards_inside_the_marker(monkeypatch):
    from backend.web.core import terminal

    monkeypatch.setattr(
        al, "wrap_launch", lambda cmd, name: "GUARD<%s|%s>" % (cmd, name)
    )
    out = terminal._wrap_launch_cmd("claude", "s9")
    assert "GUARD<claude|s9>; echo $? > " in out
    assert out.startswith("rm -f ")


# --- the gitnexus shim --------------------------------------------------------


def _fake_gitnexus(directory):
    directory.mkdir(parents=True, exist_ok=True)
    real = directory / "gitnexus"
    real.write_text(
        "#!/bin/sh\n"
        'echo "args=$*"\n'
        'echo "node=${NODE_OPTIONS-}"\n'
        "cat /proc/self/oom_score_adj 2>/dev/null || echo noproc\n"
    )
    real.chmod(0o755)
    return real


def test_shim_dir_needs_gitnexus_on_path(monkeypatch, tmp_path):
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))
    assert al.gitnexus_shim_dir() is None


def test_shim_dir_off_switches(monkeypatch, tmp_path):
    _fake_gitnexus(tmp_path / "npm")
    monkeypatch.setenv("PATH", str(tmp_path / "npm"))
    assert al.gitnexus_shim_dir({**al.DEFAULTS, "gitnexus_heap_mb": 0}) is None
    assert al.gitnexus_shim_dir({**al.DEFAULTS, "enabled": False}) is None
    monkeypatch.setenv("MINDFLOCK_GITNEXUS_GATE", "0")
    assert al.gitnexus_shim_dir() is None


def test_shim_written_executable_and_idempotent(monkeypatch, tmp_path):
    _fake_gitnexus(tmp_path / "npm")
    monkeypatch.setenv("PATH", str(tmp_path / "npm"))
    d = al.gitnexus_shim_dir()
    assert d == str(tmp_path / "run" / "bin")
    shim = os.path.join(d, "gitnexus")
    assert os.access(shim, os.X_OK)
    mtime = os.stat(shim).st_mtime_ns
    assert al.gitnexus_shim_dir() == d
    assert os.stat(shim).st_mtime_ns == mtime  # unchanged text: not rewritten
    subprocess.run(["/bin/sh", "-n", shim], check=True)


def _run_shim(tmp_path, args, extra_env=None, path_dirs=None):
    real_dir = tmp_path / "npm"
    _fake_gitnexus(real_dir)
    shim_dir = al.shim_dir()
    os.makedirs(shim_dir, exist_ok=True)
    shim = os.path.join(shim_dir, "gitnexus")
    with open(shim, "w", encoding="utf-8") as fh:
        fh.write(al.gitnexus_shim_text(sys.executable, 4096))
    os.chmod(shim, 0o755)
    dirs = path_dirs if path_dirs is not None else [shim_dir, str(real_dir)]
    env = {
        "PATH": os.pathsep.join(dirs + ["/usr/bin", "/bin"]),
        # The slot itself is backend.slot's to test; here it just runs CMD.
        "MINDFLOCK_SLOT": "0",
        "MINDFLOCK_RUN_DIR": str(tmp_path / "run"),
        "HOME": str(tmp_path),
    }
    env.update(extra_env or {})
    return subprocess.run(
        ["gitnexus", *args], env=env, capture_output=True, text=True, timeout=60
    )


def test_shim_passes_other_commands_through(tmp_path):
    r = _run_shim(tmp_path, ["mcp", "--x"], {"NODE_OPTIONS": ""})
    assert r.returncode == 0, r.stderr
    lines = r.stdout.splitlines()
    assert lines[0] == "args=mcp --x"
    assert lines[1] == "node="  # no heap cap outside analyze


def test_shim_caps_analyze(tmp_path):
    r = _run_shim(tmp_path, ["analyze", "--force"])
    assert r.returncode == 0, r.stderr
    lines = r.stdout.splitlines()
    assert lines[0] == "args=analyze --force"
    assert lines[1] == "node=--max-old-space-size=4096"
    if os.path.exists("/proc/self/oom_score_adj"):
        assert lines[2] == str(al.GITNEXUS_OOM_SCORE_ADJ)


def test_shim_keeps_a_users_own_heap(tmp_path):
    r = _run_shim(
        tmp_path,
        ["analyze"],
        {"NODE_OPTIONS": "--max-old-space-size=9000 --trace-exit"},
    )
    assert r.stdout.splitlines()[1] == "node=--max-old-space-size=9000 --trace-exit"


def test_shim_skips_other_shim_copies(tmp_path):
    """A second shim copy earlier on PATH (another run dir) is never taken for
    the real binary — no exec loop."""
    other = tmp_path / "other-run" / "bin"
    other.mkdir(parents=True)
    copy = other / "gitnexus"
    copy.write_text(al.gitnexus_shim_text(sys.executable, 4096))
    copy.chmod(0o755)
    r = _run_shim(
        tmp_path,
        ["status"],
        path_dirs=[al.shim_dir(), str(other), str(tmp_path / "npm")],
    )
    assert r.returncode == 0, r.stderr
    assert r.stdout.splitlines()[0] == "args=status"


def test_shim_reports_missing_real_binary(tmp_path):
    r = _run_shim(tmp_path, ["analyze"], path_dirs=[al.shim_dir()])
    assert r.returncode == 127
    assert "not found" in r.stderr


def test_shim_text_queues_analyze_through_the_slot():
    text = al.gitnexus_shim_text("/py/bin/python", 2048, 1, package_root="/pkg")
    assert (
        "PYTHONPATH=/pkg exec /py/bin/python -P -m backend.slot --pool gitnexus "
        '--max 1 --timeout 1800 -- "$real" "$@"'
    ) in text
    assert "--max-old-space-size=2048" in text


# --- the server's own OOM score ---------------------------------------------


class _AdjFile:
    """A fake /proc/self/oom_score_adj with a floor, like the kernel's."""

    def __init__(self, tmp_path, value, floor):
        self.path = tmp_path / "oom_score_adj"
        self.path.write_text(str(value))
        self.floor = floor


def test_server_lowers_itself_to_the_floor(monkeypatch, tmp_path):
    from backend.web import run

    monkeypatch.delenv("MINDFLOCK_SERVER_OOM_PROTECT", raising=False)
    f = _AdjFile(tmp_path, 200, 100)
    real_open = open

    def fake_open(path, mode="r", **kw):
        if str(path) == str(f.path) and "w" in mode:

            class W:
                def __enter__(self_):
                    return self_

                def __exit__(self_, *a):
                    return False

                def write(self_, text):
                    if int(text) < f.floor:
                        raise PermissionError(13, "denied")
                    f.path.write_text(text)

            return W()
        return real_open(path, mode, **kw)

    monkeypatch.setattr("builtins.open", fake_open)
    assert run.protect_from_oom(str(f.path)) == 100
    assert f.path.read_text() == "100"


def test_server_never_raises_its_score(monkeypatch, tmp_path):
    from backend.web import run

    monkeypatch.delenv("MINDFLOCK_SERVER_OOM_PROTECT", raising=False)
    p = tmp_path / "adj"
    p.write_text("0")
    assert run.protect_from_oom(str(p)) == 0
    assert p.read_text() == "0"
    p.write_text("-900")
    assert run.protect_from_oom(str(p)) == -900  # never raised toward 0
    assert run.protect_from_oom(str(tmp_path / "missing")) is None


def test_server_protect_off_switch(monkeypatch, tmp_path):
    from backend.web import run

    p = tmp_path / "adj"
    p.write_text("300")
    monkeypatch.setenv("MINDFLOCK_SERVER_OOM_PROTECT", "0")
    assert run.protect_from_oom(str(p)) is None
    assert p.read_text() == "300"


# --- the all-agents slice -------------------------------------------------------


def test_server_start_puts_the_fleet_ceiling_on_the_slice(monkeypatch):
    """ensure_fleet_slice (server start): the slice gets MemoryHigh = 65% of
    RAM for all agents together before any agent launches."""
    runs = _Runs(0)
    monkeypatch.setattr(al.subprocess, "run", runs)
    monkeypatch.setattr(al, "_systemd_run", lambda: "/usr/bin/systemd-run")
    monkeypatch.setattr(al.shutil, "which", lambda name: "/usr/bin/" + name)
    monkeypatch.setattr(al, "_total_memory", lambda: 20 * GB)
    monkeypatch.setattr(al, "_settings_blob", lambda: {})
    assert al.ensure_fleet_slice() is True
    sets = [c for c in runs.calls if c[0].endswith("systemctl")]
    assert sets and sets[-1][4] == "app-mindflock.slice"
    assert "MemoryHigh=%d" % int(20 * GB * 0.65) in sets[-1]
    # Idempotent: a second call sets the same values again, nothing else.
    al.ensure_fleet_slice()
    sets2 = [c for c in runs.calls if c[0].endswith("systemctl")]
    assert sets2[-1] == sets[-1]


def test_a_fresh_probe_yes_reapplies_the_ceiling(monkeypatch):
    """A user manager restarted since the last probe lost the runtime
    properties: every fresh yes (not a cached one) puts them back."""
    runs = _Runs(0)
    monkeypatch.setattr(al.subprocess, "run", runs)
    monkeypatch.setattr(al, "_systemd_run", lambda: "/usr/bin/systemd-run")
    monkeypatch.setattr(al.shutil, "which", lambda name: "/usr/bin/" + name)
    assert al.scope_available() is True
    assert al.scope_available(refresh=True) is True
    sets = [c for c in runs.calls if c[0].endswith("systemctl")]
    assert len(sets) == 2


def test_no_scope_no_slice(monkeypatch):
    runs = _Runs(0)
    monkeypatch.setattr(al.subprocess, "run", runs)
    monkeypatch.setenv("MINDFLOCK_AGENT_SCOPE", "0")
    assert al.ensure_fleet_slice() is False
    assert runs.calls == []
    monkeypatch.delenv("MINDFLOCK_AGENT_SCOPE")
    monkeypatch.setattr(al, "_systemd_run", lambda: None)  # macOS, no systemd
    al._PROBE = None
    assert al.ensure_fleet_slice() is False
    assert runs.calls == []


def test_shed_idle_is_an_agent_memory_key():
    assert al.DEFAULTS["shed_idle"] is True
    assert al.clean_config({"shed_idle": False, "junk": 1}) == {"shed_idle": False}
    assert al.config({"shed_idle": False})["shed_idle"] is False
