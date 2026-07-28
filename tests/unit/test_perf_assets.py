"""Static-asset delivery: gzip + sensible cache headers (perf productizing).

Cold load ships ~510KB of uncompressed JS/CSS; these lock in gzip compression
and long-lived caching for the stable third-party vendor bundles, while keeping
our own hand-edited assets revalidated so edits still show up on reload.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from backend.web import server

client = TestClient(server.app)


def test_app_js_is_gzipped():
    r = client.get("/app.js", headers={"Accept-Encoding": "gzip"})
    assert r.status_code == 200
    # Starlette's GZipMiddleware compresses text bodies over the min size.
    assert r.headers.get("content-encoding") == "gzip"


def test_vendor_assets_cached_immutable():
    r = client.get("/vendor/xterm.js")
    assert r.status_code == 200
    cc = r.headers.get("cache-control", "")
    assert "max-age=31536000" in cc and "immutable" in cc


def test_our_assets_stay_no_cache():
    for path in ("/", "/app.js", "/style.css"):
        r = client.get(path)
        assert r.headers.get("cache-control") == "no-cache", path
