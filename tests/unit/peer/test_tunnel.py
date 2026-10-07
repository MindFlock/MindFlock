"""The cloudflared quick-tunnel wrapper (backend/peer/tunnel.py).

No network and no Cloudflare: "cloudflared" here is a small Python script
that records how it was started and prints scripted (often hostile) output.
"""

from __future__ import annotations

import asyncio
import json
import os
import stat
import sys

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from backend.peer import tunnel
from backend.peer.tunnel import QuickTunnel, TunnelError, parse_quick_tunnel_host

BANNER = [
    "2026-10-07T02:41:47Z INF Thank you for trying Cloudflare Tunnel.",
    "2026-10-07T02:41:47Z INF Requesting new quick Tunnel on trycloudflare.com...",
    "2026-10-07T02:41:52Z INF +--------------------------------------------+",
    "2026-10-07T02:41:52Z INF |  Your quick Tunnel has been created! Visit it at"
    " (it may take some time to be reachable):  |",
    "2026-10-07T02:41:52Z INF |  https://mailman-measures-label-charleston"
    ".trycloudflare.com                               |",
    "2026-10-07T02:41:52Z INF +--------------------------------------------+",
    "2026-10-07T02:41:53Z INF Registered tunnel connection connIndex=0 "
    "connection=b8212750 event=0 ip=198.41.200.43 location=ewr14 protocol=quic",
]
GOOD_HOST = "mailman-measures-label-charleston.trycloudflare.com"

FAKE = r"""#!{python}
import json, os, signal, sys, time
rec = {{"argv": sys.argv[1:], "env": dict(os.environ), "cwd": os.getcwd()}}
with open({record!r}, "w") as f:
    json.dump(rec, f)
signal.signal(signal.SIGTERM, lambda *a: sys.exit(0))
script = json.load(open({script!r}))
for item in script["lines"]:
    if isinstance(item, (int, float)):
        time.sleep(item)
        continue
    sys.stdout.buffer.write(item.encode("utf-8", "surrogateescape") + b"\n")
    sys.stdout.flush()
if script.get("exit") is not None:
    sys.exit(script["exit"])
while True:
    time.sleep(0.1)
"""


@pytest.fixture
def fake_cf(tmp_path):
    def make(lines, exit=None):
        record = tmp_path / "record.json"
        script = tmp_path / "script.json"
        script.write_text(json.dumps({"lines": lines, "exit": exit}))
        exe = tmp_path / "cloudflared"
        exe.write_text(
            FAKE.format(python=sys.executable, record=str(record), script=str(script))
        )
        exe.chmod(exe.stat().st_mode | stat.S_IXUSR)
        return str(exe), record

    return make


def make_tunnel(exe, tmp_path, port=40001, **kw):
    kw.setdefault("start_timeout", 5)
    kw.setdefault("register_grace", 2)
    return QuickTunnel(exe, port, workdir=str(tmp_path / "relay"), **kw)


# -- parsing ---------------------------------------------------------------------


@pytest.mark.parametrize(
    "line,want",
    [
        (BANNER[4], GOOD_HOST),
        ("|  https://a.trycloudflare.com  |", "a.trycloudflare.com"),
        ("https://abc-def.trycloudflare.com", "abc-def.trycloudflare.com"),
        (
            "\x1b[32mINF\x1b[0m |  https://x-y.trycloudflare.com |",
            "x-y.trycloudflare.com",
        ),
        # Never the API endpoint (appears in error messages), never a path.
        (
            'ERR failed to request quick Tunnel: Post "https://api.trycloudflare.com/tunnel"',
            None,
        ),
        ("https://api.trycloudflare.com", None),
        ("https://www.trycloudflare.com ", None),
        ("https://x.trycloudflare.com/path", None),
        ("https://x.trycloudflare.com:8443", None),
        ("https://x.trycloudflare.com.evil.example", None),
        ("https://x.trycloudflare.community", None),
        ("https://evil.example/https://x.trycloudflare.com", None),
        ("https://a.b.trycloudflare.com", None),  # one label only
        ("https://x.evil.com/.trycloudflare.com", None),
        ("http://x.trycloudflare.com", None),
        ("HTTPS://X.TRYCLOUDFLARE.COM", None),
        ("https://-x.trycloudflare.com", None),
        ("https://x-.trycloudflare.com", None),
        ("https://" + "a" * 64 + ".trycloudflare.com", None),
        ("https://xn--bcher-kva.trycloudflare.com", "xn--bcher-kva.trycloudflare.com"),
        ("https://x​.trycloudflare.com", None),
        ("", None),
        ("\x00\xff garbage", None),
        (None, None),
        (b"https://x.trycloudflare.com", None),
    ],
)
def test_parse_quick_tunnel_host(line, want):
    assert parse_quick_tunnel_host(line) == want


def test_parse_ignores_matches_past_the_line_cap():
    line = "x" * (tunnel.MAX_LINE + 10) + " https://late.trycloudflare.com"
    assert parse_quick_tunnel_host(line) is None


_LABEL_CHARS = set("abcdefghijklmnopqrstuvwxyz0123456789-")  # pragma: allowlist secret


@settings(max_examples=500)
@given(st.text(max_size=400))
def test_parse_never_returns_anything_but_a_strict_quick_tunnel_host(text):
    host = parse_quick_tunnel_host(text)
    if host is not None:
        label, _, suffix = host.partition(".")
        assert suffix == "trycloudflare.com"
        assert label not in ("api", "www")
        assert 1 <= len(label) <= 63
        assert set(label) <= _LABEL_CHARS
        assert not label.startswith("-") and not label.endswith("-")


@settings(max_examples=200)
@given(
    prefix=st.text(alphabet=" |:=[]\t()", max_size=100),
    label=st.from_regex(r"[a-z0-9]{1,20}", fullmatch=True),
)
def test_parse_finds_the_banner_amid_noise(prefix, label):
    line = f"2026-10-07T02:41:52Z INF{prefix} |  https://{label}.trycloudflare.com  |"
    want = None if label in ("api", "www") else f"{label}.trycloudflare.com"
    assert parse_quick_tunnel_host(line) == want


# -- the process -------------------------------------------------------------------


async def test_start_returns_the_issued_host(fake_cf, tmp_path, monkeypatch):
    monkeypatch.setenv("TUNNEL_TOKEN", "would-run-someone-elses-tunnel")
    monkeypatch.setenv("TUNNEL_URL", "http://127.0.0.1:8765")
    monkeypatch.setenv("SECRET_API_KEY", "x")
    exe, record = fake_cf(BANNER)
    t = make_tunnel(exe, tmp_path, port=40123)
    try:
        assert await t.start() == GOOD_HOST
        assert t.alive and t.registered and t.hostname == GOOD_HOST
        rec = json.loads(record.read_text())
    finally:
        await t.stop()
    argv = rec["argv"]
    assert argv[0] == "tunnel" and "--no-autoupdate" in argv
    # Exactly one origin: the relay ingress on loopback. Never the web UI.
    assert argv[argv.index("--url") + 1] == "http://127.0.0.1:40123"
    assert argv.count("--url") == 1 and "8765" not in " ".join(argv)
    cfg = argv[argv.index("--config") + 1]
    assert open(cfg).read().strip().endswith("no-autoupdate: true")
    assert stat.S_IMODE(os.stat(cfg).st_mode) == 0o600
    # A private HOME (no ~/.cloudflared config or cert), no inherited env.
    env = rec["env"]
    assert env["HOME"].startswith(str(tmp_path / "relay"))
    assert os.listdir(env["HOME"]) == []
    for leaked in ("TUNNEL_TOKEN", "TUNNEL_URL", "SECRET_API_KEY"):
        assert leaked not in env
    # macOS adds __CF_USER_TEXT_ENCODING to every process's environment
    # itself; it isn't something we passed.
    assert set(env) - {"__CF_USER_TEXT_ENCODING"} <= {
        "PATH",
        "HOME",
        "LANG",
        "LC_CTYPE",
    }


async def test_stop_terminates_and_does_not_report_an_exit(fake_cf, tmp_path):
    exe, _ = fake_cf(BANNER)
    exits = []
    t = make_tunnel(exe, tmp_path, on_exit=lambda: exits.append(1))
    await t.start()
    proc = t.proc
    await t.stop()
    assert proc.returncode is not None and not t.alive and t.hostname is None
    await asyncio.sleep(0.1)
    assert exits == []


async def test_unexpected_exit_is_reported(fake_cf, tmp_path):
    exe, _ = fake_cf(BANNER + [0.3], exit=1)
    exits = []
    t = make_tunnel(exe, tmp_path, on_exit=lambda: exits.append(1))
    await t.start()
    for _ in range(100):
        if exits:
            break
        await asyncio.sleep(0.05)
    assert exits == [1] and t.hostname is None and not t.alive
    await t.stop()


async def test_exit_before_a_host_is_an_error(fake_cf, tmp_path):
    exe, _ = fake_cf(
        [
            'ERR failed to request quick Tunnel: Post "https://api.trycloudflare.com/tunnel": dial tcp'
        ],
        exit=1,
    )
    exits = []
    t = make_tunnel(exe, tmp_path, on_exit=lambda: exits.append(1))
    with pytest.raises(TunnelError, match="exited"):
        await t.start()
    assert exits == [] and not t.alive


async def test_silence_times_out_and_kills(fake_cf, tmp_path):
    exe, _ = fake_cf(["INF nothing useful", 30])
    t = make_tunnel(exe, tmp_path, start_timeout=0.8)
    with pytest.raises(TunnelError, match="did not report"):
        await t.start()
    assert t.proc is None


async def test_hostile_output_is_survived(fake_cf, tmp_path):
    lines = [
        "\x1b]0;evil title\x07\x1b[2J",
        "A" * 200_000,  # one enormous line
        "\udcff\udcfe binary-ish",
        'ERR Post "https://api.trycloudflare.com/tunnel": EOF',
        "https://x.trycloudflare.com.evil.example",
        "|  https://first-one.trycloudflare.com  |",
        "|  https://second-one.trycloudflare.com  |",  # first wins
    ] + ["B" * 5000] * 50
    exe, _ = fake_cf(lines)
    t = make_tunnel(exe, tmp_path, register_grace=0.2)
    try:
        assert await t.start() == "first-one.trycloudflare.com"
        await asyncio.sleep(0.3)
        assert t.alive  # its later output is drained, not left to block it
    finally:
        await t.stop()


async def test_missing_binary_is_an_error(tmp_path):
    t = make_tunnel(str(tmp_path / "nope"), tmp_path)
    with pytest.raises(TunnelError, match="could not start"):
        await t.start()


def test_bad_origin_port():
    for port in (0, 65536, "80", True):
        with pytest.raises(ValueError):
            QuickTunnel("/bin/true", port)


def test_find_cloudflared(tmp_path, monkeypatch):
    exe = tmp_path / "cf"
    exe.write_text("#!/bin/sh\n")
    exe.chmod(0o700)
    noexec = tmp_path / "cf2"
    noexec.write_text("x")
    assert tunnel.find_cloudflared(str(exe)) == str(exe)
    assert tunnel.find_cloudflared(str(noexec)) is None
    assert tunnel.find_cloudflared("cf") is None  # relative: never resolved
    assert tunnel.find_cloudflared(str(tmp_path)) is None  # a directory
    bindir = tmp_path / "bin"
    bindir.mkdir()
    (bindir / "cloudflared").write_text("#!/bin/sh\n")
    (bindir / "cloudflared").chmod(0o700)
    monkeypatch.setenv("PATH", str(bindir))
    assert tunnel.find_cloudflared("") == str(bindir / "cloudflared")
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))
    assert tunnel.find_cloudflared("") is None


def test_doctor_reports_cloudflared(tmp_path, monkeypatch):
    from backend import doctor

    exe = tmp_path / "cloudflared"
    exe.write_text("#!/bin/sh\n")
    exe.chmod(0o700)
    monkeypatch.setenv("MINDFLOCK_CLOUDFLARED", str(exe))
    c = doctor.check_cloudflared()
    assert (c.status, c.detail) == ("ok", str(exe))
    monkeypatch.setenv("MINDFLOCK_CLOUDFLARED", str(tmp_path / "missing"))
    monkeypatch.setattr(doctor, "_peer_settings", lambda: {})
    c = doctor.check_cloudflared()
    # Missing with no relay wanting it: reported, installable on request, but
    # never part of the one-shot install plan.
    assert c.status == "info" and c.install is False
