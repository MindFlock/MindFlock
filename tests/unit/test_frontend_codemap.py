"""Code map (the Code Tree) + zones: what stays on the Python side.

The Map tab's markup hooks, copy, tree wiring, rail chip, events, the Zones
dialog's modal behaviour, Plan first, the no-dead-dialogs rule and the Map's
CSS are pinned by vitest against the source:
``frontend/src/__tests__/codemapUi.test.ts``.

Here: the layout worker as a served static file (a static-server contract),
and three bundle checks that quote strings a MindFlock guard hook refuses in
any agent edit (the fetchers' route list, the push/PR/merge override re-post,
the Zones dialog's API routes) — they stay until a human moves them. Multi-token
snippets go through ``tests/_bundle.py`` so a bundler re-layout doesn't fail
them.

The last tests are the other half of the shared pattern fixture: the same
``glob → regex`` cases vitest runs through ``new RegExp`` (and the TS
``compilePattern`` port) are run here through Python ``re`` and
``backend.config.red_zones``; the zone-classification cases are the backend's
own ``tests/fixtures/zone_classify_cases.json``, which vitest reads directly.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from backend.web import server
from tests._bundle import in_bundle

ROOT = Path(__file__).resolve().parents[2]
STATIC = ROOT / "backend" / "web" / "static"
FIXTURES = ROOT / "frontend" / "src" / "__tests__" / "fixtures"
FIXTURE = FIXTURES / "red_zone_patterns.json"

CODEMAP_MODULES = (
    "src/lib/codemap.ts",
    "src/lib/codemapApi.ts",
    "src/lib/codemapSeen.ts",
    "src/components/grid/CodeMapTab.tsx",
    "src/components/grid/codemap/CodeTree.tsx",
    "src/components/grid/codemap/TreeCards.tsx",
    "src/components/grid/codemap/BirdIndex.tsx",
    "src/components/grid/codemap/treeCtl.ts",
    "src/components/grid/codemap/SearchBox.tsx",
    "src/components/grid/codemap/SessionPanel.tsx",
    "src/components/grid/codemap/AddZoneRow.tsx",
    "src/components/dialogs/RedZonesDialog.tsx",
    "src/lib/codetree/layout.ts",
    "src/lib/codetree/model.ts",
    "src/lib/codetree/input.ts",
    "src/lib/codetree/draw.ts",
    "src/lib/codetree/palette.ts",
    "src/lib/codetree/live.ts",
    "src/lib/codetree/zones.ts",
    "src/lib/codetree/blast.ts",
    "src/lib/codetree/engine.ts",
    "src/lib/codetree/cache.ts",
)


@pytest.fixture(scope="module")
def js() -> str:
    return (STATIC / "app.js").read_text(encoding="utf-8")


def _region(js: str, module: str) -> str:
    """The bundled text of one source module (Rolldown's ``//#region`` markers)."""
    start = js.find("//#region " + module + "\n")
    assert start >= 0, f"{module} is not in the bundle"
    end = js.find("//#endregion", start)
    assert end > start
    return js[start:end]


def _function(js: str, name: str) -> str:
    """The text of a top-level bundled function, up to the next top-level one."""
    m = re.search(r"^(?:async )?function " + re.escape(name) + r"\(", js, re.M)
    assert m, f"function {name} not in bundle"
    nxt = re.search(r"^(?:async )?function |^//#endregion", js[m.end() :], re.M)
    return js[m.start() : m.end() + (nxt.start() if nxt else len(js))]


def test_every_route_lives_in_the_fetchers_module(js):
    """One module owns every URL (so a shape change is one edit); the tab and
    its parts only call its functions."""
    api = _region(js, "src/lib/codemapApi.ts")
    for route in (
        '"/code-map" + (fp ? "?fp=" + enc(fp) : "")',
        '"/code-map/live?since=" + since',
        '"/code-map/file?path=" + enc(path)',
        '"/code-map/search?q=" + enc(q)',
        '"/code-map/ask-plan"',
        '"/code-map/go", { json: { zone_ids: zoneIds, scope_to_plan: scopeToPlan } }',
        '"/red-zones/preview", { json: { pattern, kind } }',
        '"/red-zones/allow", { json: { path } }',
        '"/red-zones/exempt"',
        '"/waive"',
        '"/api/red-zones/companions?repo_id=" + enc(repoId)',
        '"/api/red-zones/companions", { method: "PUT"',
    ):
        assert in_bundle(route, api), route
    for mod in CODEMAP_MODULES:
        if mod.endswith(("codemapApi.ts", "RedZonesDialog.tsx")):
            continue
        body = _region(js, mod)
        assert "instApi(" not in body, f"{mod} fetches directly"
        assert "fetch(" not in body, f"{mod} fetches directly"
        assert "/code-map" not in body.replace("code-map/", ""), mod


def test_layout_worker_ships_next_to_the_bundle(js):
    """The layout search runs in a module Web Worker built to a stable file next
    to app.js (lib/codetree/engine.ts; its source side is pinned in
    codemapUi.test.ts). The file is committed, served, and what the bundle
    loads."""
    worker = STATIC / "codetree-worker.js"
    assert worker.exists(), "the layout worker is not in the committed static dir"
    w = worker.read_text(encoding="utf-8")
    assert "self.onmessage" in w and "buildModel" in w
    r = TestClient(server.app).get("/codetree-worker.js")
    assert r.status_code == 200 and "javascript" in r.headers["content-type"]
    assert '"/codetree-worker.js"' in js


def test_push_pr_merge_offer_the_red_zone_override(js):
    # A 409 carrying red_zone_breaches is recognized…
    detector = _function(js, "redZoneBreaches")
    assert "err.status !== 409" in detector and "red_zone_breaches" in detector
    # …and each of the three actions re-posts with the override flag.
    push = _function(js, "pushSession")
    assert "body.override_red_zones = true" in push
    assert in_bundle('offerRedZoneOverride(title, "Push", rz', push)
    pr = _function(js, "submitMakePr")
    assert "body.override_red_zones = true" in pr
    assert in_bundle('offerRedZoneOverride(title, "Open PR", rz', pr)
    merge = _function(js, "mergeSession")
    assert in_bundle("{ json: { override_red_zones: true } }", merge)
    assert in_bundle('offerRedZoneOverride(title, "Merge", rz', merge)
    # errorPop grew buttons for it.
    assert "cs-error-actions" in js and "cs-error-act" in js


def test_zones_dialog_kind_companions_and_plan_first(js):
    dlg = _region(js, "src/components/dialogs/RedZonesDialog.tsx")
    assert '"/api/red-zones"' in dlg
    assert '"/api/red-zones/plan-first"' in dlg
    assert '"red-zones-panel"' in dlg
    assert '"Zones"' in dlg or ">Zones<" in dlg or '"Zones")' in dlg
    # Each zone shows its kind; the repo's derived outputs are editable.
    assert '"only here" : "keep out"' in dlg
    assert "fetchCompanions(repoId)" in dlg and "saveCompanions(repoId" in dlg
    assert "Derived outputs" in dlg
    # New Session: the Plan-first checkbox rides the create payload.
    assert '"new-plan-first"' in js
    assert "body.plan_first = true" in js


def _fixture():
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def test_shared_pattern_fixture_matches_python_engines():
    """The vitest fixture, run through Python: its regex sources behave the
    same in ``re``, and ``red_zones.compile_pattern`` agrees on every case.
    Behavioural, not textual — escaping (``\\/`` vs ``/``) may differ; what
    must never differ is which paths a zone covers."""
    fx = _fixture()
    assert len(fx["cases"]) > 5
    rz = pytest.importorskip("backend.config.red_zones")
    for ci, cases in ((False, fx["cases"]), (True, fx["ci"])):
        flags = re.IGNORECASE if ci else 0
        for c in cases:
            src = rz.compile_pattern(c["pattern"])
            # Never Python-only syntax: the frontend compiles it with new RegExp.
            assert not re.search(r"\\Z|\(\?P|\(\?[aiLmsux]+\)", src), src
            for p in c["match"]:
                assert re.match(c["re"], p, flags), (c["pattern"], p)
                assert rz.matches(src, p, ci), (c["pattern"], src, p)
            for p in c["miss"]:
                assert not re.match(c["re"], p, flags), (c["pattern"], p)
                assert not rz.matches(src, p, ci), (c["pattern"], src, p)


def test_classify_cases_are_shared_with_the_backend():
    """vitest runs the backend's own classify fixture through ``classifyPath``
    (three engines — red_zones.classify, the hook's copy, the Map — one set of
    cases), not a frontend copy that could drift."""
    src = (ROOT / "frontend" / "src" / "__tests__" / "codemap.test.ts").read_text(
        encoding="utf-8"
    )
    assert "tests/fixtures/zone_classify_cases.json?raw" in src
    assert not (FIXTURES / "zone_classify_cases.json").exists()
