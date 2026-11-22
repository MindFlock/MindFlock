"""End-to-end: the desktop app's ENGINE update on Linux/macOS, in a sandbox.

The packaged app updates its engine by running its bundled ``install.sh``
with ``MINDFLOCK_INSTALL_REF=v<target>`` (``startInstall`` in
``electron/main.js``). This runs that real script against:

* a sandbox ``$HOME`` with its own uv tool dir and bin dir — the owner's real
  install and ``~/.mindflock`` are never touched;
* a local git repository standing in for github.com/MindFlock/MindFlock
  (``MINDFLOCK_INSTALL_REPO=file://…``), whose tags are tiny fake engines
  named ``mindflock`` that print their version;
* a local package index (``UV_EXTRA_INDEX_URL``) that serves good metadata
  but cuts a wheel download off halfway — a network drop mid-update.

What it pins: an update lands (shim, ``install.json``); and every failure —
an unknown ref, an unresolvable dependency, a download cut off after
resolution, the real install failing after the stage — leaves the engine
that was there working. Before the stage step, the cut-off download deleted
the engine outright (``test_the_hazard_the_stage_exists_for`` shows uv
still does that). Linux only; ``MINDFLOCK_SKIP_E2E=1`` skips. Build backends
come from the uv cache (or PyPI); nothing talks to GitHub.
"""

from __future__ import annotations

import hashlib
import http.server
import io
import json
import os
import shutil
import subprocess
import sys
import threading
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
UV = shutil.which("uv")
GIT = shutil.which("git")

pytestmark = pytest.mark.skipif(
    sys.platform == "win32"
    or not UV
    or not GIT
    or os.environ.get("MINDFLOCK_SKIP_E2E") == "1",
    reason="needs uv + git on Linux/macOS (MINDFLOCK_SKIP_E2E=1 skips)",
)


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        [GIT, *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
        env=dict(
            os.environ,
            GIT_AUTHOR_NAME="t",
            GIT_AUTHOR_EMAIL="t@example.invalid",
            GIT_COMMITTER_NAME="t",
            GIT_COMMITTER_EMAIL="t@example.invalid",
            GIT_CONFIG_GLOBAL=os.devnull,
            GIT_CONFIG_SYSTEM=os.devnull,
        ),
    ).stdout.strip()


_PYPROJECT = """[project]
name = "mindflock"
version = "{version}"
dependencies = [{deps}]
[project.optional-dependencies]
web = []
[project.scripts]
mindflock = "fakemf:main"
[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"
[tool.hatch.build.targets.wheel]
packages = ["fakemf"]
"""


def _release_repo(root: Path) -> Path:
    """A bare repo whose tags are fake engines:
    v0.1.0 / v0.2.0 good; v0.3.0 needs ``depzz`` (whose download the local
    index cuts off); v0.4.0 needs a package that exists nowhere."""
    work = root / "src"
    (work / "fakemf").mkdir(parents=True)
    _git(work, "init", "-q", "-b", "main")
    for version, deps in (
        ("0.1.0", ""),
        ("0.2.0", ""),
        ("0.3.0", '"depzz"'),
        ("0.4.0", '"mindflock-no-such-package-zz9==1.0"'),
    ):
        (work / "pyproject.toml").write_text(
            _PYPROJECT.format(version=version, deps=deps), encoding="utf-8"
        )
        (work / "fakemf" / "__init__.py").write_text(
            "def main():\n    print('mindflock %s')\n" % version, encoding="utf-8"
        )
        _git(work, "add", "-A")
        _git(work, "commit", "-q", "-m", "v" + version)
        _git(work, "tag", "-a", "v" + version, "-m", "v" + version)
    bare = root / "MindFlock.git"
    _git(root, "clone", "-q", "--bare", str(work), str(bare))
    return bare


def _wheel(name: str, version: str) -> bytes:
    buf = io.BytesIO()
    di = "%s-%s.dist-info" % (name, version)
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("%s/__init__.py" % name, "X = 1\n")
        z.writestr(di + "/METADATA", _metadata(name, version))
        z.writestr(
            di + "/WHEEL",
            "Wheel-Version: 1.0\nRoot-Is-Purelib: true\nTag: py3-none-any\n",
        )
        z.writestr(di + "/RECORD", "")
    return buf.getvalue()


def _metadata(name: str, version: str) -> str:
    return "Metadata-Version: 2.1\nName: %s\nVersion: %s\n" % (name, version)


class _CutOffIndex(http.server.ThreadingHTTPServer):
    """A PEP 503 index with one package, ``depzz``: its metadata (PEP 658) is
    served whole, so resolution succeeds; its wheel stops a third of the way
    in — the download dies AFTER uv decided what to install."""

    def __init__(self) -> None:
        self.whl_name = "depzz-1.0-py3-none-any.whl"
        self.whl = _wheel("depzz", "1.0")
        self.meta = _metadata("depzz", "1.0").encode()
        self.hits: list = []
        super().__init__(("127.0.0.1", 0), _IndexHandler)


class _IndexHandler(http.server.BaseHTTPRequestHandler):
    server: _CutOffIndex

    def log_message(self, *_a) -> None:  # quiet
        pass

    def do_GET(self) -> None:  # noqa: N802
        srv = self.server
        p = self.path.split("?")[0]
        srv.hits.append(p)
        if p.rstrip("/") == "/simple/depzz":
            sha = hashlib.sha256(srv.meta).hexdigest()
            body = (
                '<html><body><a href="/f/%s" data-core-metadata="sha256=%s">%s</a>'
                "</body></html>" % (srv.whl_name, sha, srv.whl_name)
            ).encode()
            self._send(200, body, "text/html")
        elif p == "/f/%s.metadata" % srv.whl_name:
            self._send(200, srv.meta)
        elif p == "/f/%s" % srv.whl_name:
            self.send_response(200)
            self.send_header("Content-Length", str(len(srv.whl)))
            self.end_headers()
            self.wfile.write(srv.whl[: len(srv.whl) // 3])
            self.wfile.flush()
            self.close_connection = True
        else:
            self._send(404, b"")

    def _send(
        self, code: int, body: bytes, ctype: str = "application/octet-stream"
    ) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


@pytest.fixture(scope="module")
def world(tmp_path_factory):
    root = tmp_path_factory.mktemp("engine-update")
    repo = _release_repo(root)
    index = _CutOffIndex()
    t = threading.Thread(target=index.serve_forever, daemon=True)
    t.start()
    # uv and git only: never the owner's ~/.local/bin (a real `mindflock`).
    tools = root / "tools-on-path"
    tools.mkdir()
    for exe in (UV, GIT):
        (tools / Path(exe).name).symlink_to(exe)

    def real(*args: str) -> str:
        return subprocess.run(
            [UV, *args], capture_output=True, text=True, check=True
        ).stdout.strip()

    shared = {
        # The real caches, read-mostly: the build backend and Python come from
        # there instead of the network. Everything installed is sandboxed.
        "UV_CACHE_DIR": real("cache", "dir"),
        "UV_PYTHON_INSTALL_DIR": real("python", "dir"),
    }
    yield {"root": root, "repo": repo, "index": index, "tools": tools, "shared": shared}
    index.shutdown()


class Sandbox:
    def __init__(self, world, name: str) -> None:
        self.w = world
        self.home = world["root"] / name / "home"
        self.home.mkdir(parents=True)
        self.tool_dir = self.home / ".local" / "share" / "uv" / "tools"
        self.bin_dir = self.home / ".local" / "bin"
        self.shim = self.bin_dir / "mindflock"

    def env(self, **extra: str) -> dict:
        keep = {
            k: v
            for k, v in os.environ.items()
            if k in ("LANG", "LC_ALL", "TMPDIR", "USER", "LOGNAME")
        }
        env = dict(
            keep,
            HOME=str(self.home),
            PATH="%s:/usr/bin:/bin" % self.w["tools"],
            UV_TOOL_DIR=str(self.tool_dir),
            UV_TOOL_BIN_DIR=str(self.bin_dir),
            UV_PYTHON_DOWNLOADS="never",
            UV_EXTRA_INDEX_URL="http://127.0.0.1:%d/simple"
            % self.w["index"].server_address[1],
            MINDFLOCK_INSTALL_REPO="file://%s" % self.w["repo"],
            MINDFLOCK_NONINTERACTIVE="1",
            MINDFLOCK_INSTALL_NO_RESTART="1",
            MINDFLOCK_PORT="1",
            GIT_CONFIG_GLOBAL=os.devnull,
            GIT_CONFIG_SYSTEM=os.devnull,
            **self.w["shared"],
        )
        env.update(extra)
        return env

    def install(self, ref: str, **extra: str) -> subprocess.CompletedProcess:
        """Exactly the desktop app's call: the bundled script, the pinned ref."""
        return subprocess.run(
            ["/bin/bash", str(ROOT / "install.sh")],
            env=self.env(MINDFLOCK_INSTALL_REF=ref, **extra),
            capture_output=True,
            text=True,
            timeout=600,
        )

    def version(self) -> str:
        if not self.shim.exists():
            return ""
        r = subprocess.run([str(self.shim)], capture_output=True, text=True, timeout=60)
        return r.stdout.strip() if r.returncode == 0 else ""

    def record(self) -> dict:
        try:
            return json.loads((self.home / ".mindflock" / "install.json").read_text())
        except OSError:
            return {}

    def leftovers(self) -> list:
        parent = self.tool_dir.parent
        return sorted(
            p.name for p in parent.iterdir() if p.name.startswith("tools-mindflock")
        )


@pytest.fixture
def old_engine(world, request):
    """A machine on MindFlock 0.1.0, installed the way first launch does."""
    sb = Sandbox(world, request.node.name)
    r = sb.install("v0.1.0")
    assert r.returncode == 0, r.stdout + r.stderr
    assert sb.version() == "mindflock 0.1.0"
    return sb


def _out(r: subprocess.CompletedProcess) -> str:
    return r.stdout + r.stderr


def test_an_update_lands_and_is_recorded(old_engine, world):
    r = old_engine.install("v0.2.0")
    assert r.returncode == 0, _out(r)
    assert "building the new engine beside the one you have" in _out(r)
    assert old_engine.version() == "mindflock 0.2.0"
    rec = old_engine.record()
    assert rec["ref"] == "v0.2.0" and rec["method"] == "uv-tool"
    assert rec["sha"] == _git(world["repo"], "rev-parse", "v0.2.0^{commit}")
    assert old_engine.leftovers() == []


def test_a_download_cut_off_mid_update_leaves_the_engine_working(old_engine, world):
    r = old_engine.install("v0.3.0")
    assert r.returncode != 0
    assert "nothing was changed" in _out(r)
    assert any(
        h.endswith(".whl") for h in world["index"].hits
    ), "the cut-off download happened"
    assert old_engine.version() == "mindflock 0.1.0"
    assert old_engine.record()["ref"] == "v0.1.0"
    assert old_engine.leftovers() == []


def test_an_unresolvable_release_leaves_the_engine_working(old_engine):
    r = old_engine.install("v0.4.0")
    assert r.returncode != 0
    assert "nothing was changed" in _out(r)
    assert old_engine.version() == "mindflock 0.1.0"
    assert old_engine.leftovers() == []


def test_an_unknown_ref_leaves_the_engine_working(old_engine):
    r = old_engine.install("v9.9.9")
    assert r.returncode != 0
    assert "could not resolve" in _out(r)
    assert old_engine.version() == "mindflock 0.1.0"


def test_a_failed_real_install_puts_the_old_engine_back(old_engine, world, tmp_path):
    """The stage built fine, then the real install died (disk full, killed):
    simulated with a `uv` wrapper that fails the second install the way uv
    does, after wiping what it was replacing."""
    wrapper = tmp_path / "bin"
    wrapper.mkdir()
    (wrapper / "git").symlink_to(GIT)
    (wrapper / "uv").write_text(
        "#!/bin/sh\n"
        'case "$UV_TOOL_DIR" in *-mindflock-next*) exec %s "$@";; esac\n'
        'if [ "$1 $2" = "tool install" ]; then\n'
        '  mkdir -p "$UV_TOOL_DIR/mindflock" && echo half > "$UV_TOOL_DIR/mindflock/junk"\n'
        '  rm -f "$UV_TOOL_BIN_DIR/mindflock"; echo "error: No space left on device" >&2; exit 2\n'
        "fi\n"
        'exec %s "$@"\n' % (UV, UV),
        encoding="utf-8",
    )
    (wrapper / "uv").chmod(0o755)
    r = old_engine.install("v0.2.0", PATH="%s:/usr/bin:/bin" % wrapper)
    assert r.returncode != 0
    assert "the engine you had was put back" in _out(r)
    assert old_engine.version() == "mindflock 0.1.0"
    assert not (old_engine.tool_dir / "mindflock" / "junk").exists()
    assert old_engine.leftovers() == []


def test_the_hazard_the_stage_exists_for(old_engine, world):
    """Why install.sh stages: a plain `uv tool install --force` whose download
    dies after resolution removes the engine it was replacing. If uv ever
    stops doing that, this skips — the stage is then merely belt and braces."""
    sb = old_engine
    spec = "mindflock[web] @ git+file://%s@%s" % (
        world["repo"],
        _git(world["repo"], "rev-parse", "v0.3.0^{commit}"),
    )
    r = subprocess.run(
        [UV, "tool", "install", "--force", "--python", "3.12", spec],
        env=sb.env(),
        capture_output=True,
        text=True,
        timeout=600,
    )
    assert r.returncode != 0
    if sb.version() == "mindflock 0.1.0":
        pytest.skip("this uv keeps the old engine on a failed download")
    assert sb.version() == ""
