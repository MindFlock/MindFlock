"""Fixtures for the journey tests (``tests/e2e``).

A journey drives the real UI — the committed bundle served by a real server —
in a headless system Chrome, and asserts on what a user sees (visible text and
DOM state), never on internals. Each test gets its own sandboxed server
(:mod:`tests.e2e._server`) and its own browser page; a failing test leaves a
screenshot and the server log under ``tests/e2e/_artifacts/``.

The package is opt-in: ``pytest tests/e2e`` (or ``MINDFLOCK_E2E=1``) runs it,
while a plain ``pytest`` over the whole tree skips it, so the unit matrix never
waits on a browser. It also skips itself when Chrome or Playwright is missing.
``MINDFLOCK_E2E_SHOTS=1`` saves the end-of-test screenshot for passing tests
too — the quickest way to see what a journey saw.
"""

from __future__ import annotations

import os
import re
import shutil
from pathlib import Path

import pytest

from tests.e2e import _server

E2E_DIR = Path(__file__).resolve().parent
ARTIFACTS = E2E_DIR / "_artifacts"
VIEWPORT = {"width": 1280, "height": 800}


def _chrome_binary():
    for name in (
        "google-chrome",
        "google-chrome-stable",
        "chromium",
        "chromium-browser",
    ):
        p = shutil.which(name)
        if p:
            return p
    return None


def _targeted(config) -> bool:
    """True when the run asked for this package (a ``tests/e2e`` path on the
    command line, or ``MINDFLOCK_E2E=1``)."""
    if os.environ.get("MINDFLOCK_E2E") == "1":
        return True
    for arg in config.invocation_params.args:
        head = str(arg).split("::", 1)[0]
        try:
            p = Path(head)
            resolved = p.resolve() if p.exists() else None
        except OSError:
            resolved = None
        if resolved is not None and (
            resolved == E2E_DIR or E2E_DIR in resolved.parents
        ):
            return True
    return False


def pytest_configure(config):
    config.addinivalue_line(
        "markers",
        "mf_server(onboarded=True, stub_agent=True): how the journey's sandbox "
        "server is laid out (first-run flag; a stub agent CLI on PATH or none)",
    )


def pytest_collection_modifyitems(config, items):
    reasons = []
    if not _targeted(config):
        reasons.append("journey tests run only when targeted: pytest tests/e2e")
    if _chrome_binary() is None:
        reasons.append("journey tests need Chrome (google-chrome / chromium)")
    try:
        import playwright.sync_api  # noqa: F401
    except Exception:  # noqa: BLE001
        reasons.append("journey tests need the playwright package (dev group)")
    if not reasons:
        return
    marker = pytest.mark.skip(reason="; ".join(reasons))
    for item in items:
        if E2E_DIR in Path(str(item.fspath)).resolve().parents:
            item.add_marker(marker)


@pytest.hookimpl(hookwrapper=True, tryfirst=True)
def pytest_runtest_makereport(item, call):
    # Remember the call-phase outcome on the item so fixture teardown can tell
    # a failure apart from a pass (and save artifacts only for failures).
    outcome = yield
    rep = outcome.get_result()
    setattr(item, "rep_" + rep.when, rep)


def _failed(request) -> bool:
    rep = getattr(request.node, "rep_call", None)
    return bool(rep is not None and rep.failed)


def _keep_shots() -> bool:
    return os.environ.get("MINDFLOCK_E2E_SHOTS") == "1"


def _artifact_dir(request) -> Path:
    name = re.sub(r"[^A-Za-z0-9_.-]+", "_", request.node.nodeid.split("::", 1)[-1])
    d = ARTIFACTS / name
    d.mkdir(parents=True, exist_ok=True)
    return d


@pytest.fixture
def mf_server(request, tmp_path):
    """A sandboxed MindFlock server with a stub agent; see ``_server.boot``.

    Yields the :class:`SandboxServer`: ``base_url``, ``settings_path``,
    ``repo`` (a throwaway git repo it manages), ``state()`` (the engine's
    session list) and ``api(path)``. Options come from the ``mf_server``
    marker.
    """
    marker = request.node.get_closest_marker("mf_server")
    kwargs = dict(marker.kwargs) if marker else {}
    srv = _server.boot(
        tmp_path,
        onboarded=bool(kwargs.get("onboarded", True)),
        stub_agent=bool(kwargs.get("stub_agent", True)),
    )
    try:
        yield srv
    finally:
        if _failed(request):
            (_artifact_dir(request) / "server.log").write_text(srv.log_text())
        srv.stop()


@pytest.fixture(scope="session")
def _browser():
    from playwright.sync_api import sync_playwright

    with sync_playwright() as pw:
        browser = pw.chromium.launch(
            executable_path=_chrome_binary(),
            headless=True,
            args=["--no-sandbox", "--disable-gpu"],
        )
        try:
            yield browser
        finally:
            browser.close()


@pytest.fixture
def page(request, _browser):
    """A fresh 1280x800 page; a screenshot of it lands in ``_artifacts/`` when
    the test fails, with the browser's uncaught errors beside it."""
    context = _browser.new_context(viewport=VIEWPORT)
    page = context.new_page()
    errors = []
    page.on("pageerror", lambda exc: errors.append(str(exc)))
    try:
        yield page
    finally:
        if _failed(request) or _keep_shots():
            out = _artifact_dir(request)
            try:
                page.screenshot(path=str(out / "screenshot.png"), full_page=True)
            except Exception:  # noqa: BLE001 — a closed page still gets the log
                pass
            (out / "page-errors.txt").write_text("\n".join(errors))
        context.close()


@pytest.fixture
def open_app(page):
    """``open_app(srv)``: load the UI and wait for the boot screen to clear."""
    from playwright.sync_api import expect

    def _open(srv, path: str = "/"):
        page.goto(srv.base_url + path)
        expect(page.locator("#boot-loading")).to_have_count(0, timeout=30_000)
        return page

    return _open
