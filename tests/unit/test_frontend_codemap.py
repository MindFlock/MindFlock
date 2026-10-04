"""Code map (the Code Tree) + zones frontend wiring: structural contract checks.

The Map tab (frontend/src/components/grid/CodeMapTab.tsx and its codemap/
parts), the tree (lib/codetree/*: layout search in a Web Worker, canvas
renderer, live birds, zones on the tree — pinned by vitest), its pure math
(lib/codemap.ts), its fetchers
(lib/codemapApi.ts), the rail chip, the Zones dialog and the push/PR/merge
override are verified headlessly with screenshots; these pin the markup hooks,
routes and guard rails in the COMMITTED bundle so they can't silently regress —
and pin the one property a screenshot can't: that nothing on the zone path asks
through ``window.prompt``/``confirm``/``alert``, which the Electron app never
implements (a dead prompt fails silently: the button just does nothing).

The bundle is read from disk rather than through the server: it is a static
file, and reading it directly keeps these checks independent of server
start-up. Multi-token snippets go through ``tests/_bundle.py`` so a bundler
re-layout (Rolldown's tabs and line breaks) doesn't fail them.

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

from tests._bundle import in_bundle

ROOT = Path(__file__).resolve().parents[2]
STATIC = ROOT / "backend" / "web" / "static"
FIXTURES = ROOT / "frontend" / "src" / "__tests__" / "fixtures"
FIXTURE = FIXTURES / "red_zone_patterns.json"

# Calls that are silent no-ops in Electron. A word boundary so "prompt(" inside
# an identifier like `sendMessagePrompt(` doesn't count, and an optional
# `window.` because both spellings reach the same dead API.
DEAD_DIALOG = re.compile(r"(?<![\w.$])(?:window\.)?(?:prompt|confirm|alert)\(")

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


@pytest.fixture(scope="module")
def css() -> str:
    return (STATIC / "style.css").read_text(encoding="utf-8")


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


def _map(js: str) -> str:
    """Every Map-tab module's bundled text (the tab is split across files)."""
    return "\n".join(
        _region(js, m) for m in CODEMAP_MODULES if "RedZonesDialog" not in m
    )


def test_pane_has_map_tab_gated_like_diff(js):
    # A first-class pane tab beside Agent/Terminal/Diff/Queue.
    assert '"data-tab": "map"' in js
    assert '"pane-map"' in js
    assert "Code map — the worktree as a tree: agents, zones, plan, blast radius" in js
    # Mounted like DiffTab: {title, active}, active only while the tab shows.
    assert in_bundle('title, active: tab === "map"', js)
    # Git-gated exactly like Diff, in BOTH the saved-tab and programmatic-switch
    # fallbacks (one set drives both, so they can't disagree).
    assert in_bundle('new Set(["diff", "map"])', js)
    assert js.count("GIT_TABS.has(") >= 2


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


def test_map_tab_polls_only_while_active(js):
    tab = _region(js, "src/components/grid/CodeMapTab.tsx")
    assert "document.hidden" in tab
    # (the import alias is the bundler's to rename; the call shape is ours)
    assert in_bundle('(title, midFlight ? "remaining" : "plan")', tab)
    # Go carries the zones added since the plan; "only the planned files"
    # asks the server to scope the plan.
    assert in_bundle("goZones.map((z) => z.id)", tab)
    # The add route gets the kind; green is always worktree scope.
    assert in_bundle('scope: kind === "green" ? "worktree" : add.scope', tab)
    assert "tell_agent:" in tab


def test_map_copy_explains_empty_states_and_green_mode(js):
    m = _map(js)
    for s in (
        "Agent hasn't used any tools since MindFlock armed the map.",
        "No changes yet.",
        "Detect-only",
        "Go — with ",
        "Go — only the planned files",
        "Ask what's left",
        "Ask for plan",
        "answer the prompt in the terminal first",
        # the tree's own words (the prototype's legend and cards)
        "Growing the tree…",
        "leaf = file · branch = folder · roots = tests",
        "bird = an agent · small bird = its helper · nest = where it mostly works",
        # subagents are birds of their own; a card click follows its bird
        "sent out a helper",
        "Click to follow this bird",
        "in the nest — ",
        "what could break: hover or click a bright leaf — the files that import it light up gold",
        "red area = keep out (agents may read, never edit)",
        "lit branch = the path from the trunk to a change",
        "could break",
        "⛔ Keep out",
        "✓ Only here",
        "⛔ Keep agents out",
        "Allow this file",
        "peeked outside scope",
        "Scope requests",
        "Keep exempt",
        "Treat as breaches",
        "Whole tree",
        "Nothing here — scroll out, or press ⌂ Whole tree",
        "Paint a branch, not the trunk",
        "Undo",
        # the bird index's CHANGES: what could break is on demand, blocking is one click per changed folder
        "⚠ Could break",
        "What could break: show on the map which folders import the changed files, and rank the changed files",
        # only-here binds this worktree: this session's earlier changes are exempt, another session's are untouched
        "already changed · exempt from ✓",
        "other worktree · not affected",
        "⛔ kept out · agents may only read",
        "Collapse to the rail",
    ):
        assert s in m, s


def test_tree_is_a_canvas_with_a_text_twin(js):
    """The tree is a canvas (labelled, keyboard-focusable) — and everything it
    paints is also text: the agent cards, Rules, Activity, the cards and the
    Session panel's lists, plus a screen-reader summary of the picture."""
    tree = _region(js, "src/components/grid/codemap/CodeTree.tsx")
    assert '"ct-canvas"' in tree and 'role: "application"' in tree
    assert "tabIndex: 0" in tree
    assert 'className: "cm-sr"' in tree
    # The renderer takes every colour from the palette (app tokens + --ct-*).
    pal = _region(js, "src/lib/codetree/palette.ts")
    for tok in (
        '"--bg"',
        '"--accent"',
        '"--red"',
        '"--green"',
        '"--gold"',
        '"--ct-bark"',
        '"--ct-sky-top"',
    ):
        assert tok in pal, tok
    draw = _region(js, "src/lib/codetree/draw.ts")
    assert not re.search(r"fillStyle = \"#[0-9a-fA-F]{3,6}\"", draw)
    # A theme / accent / surface flip repaints in the new palette.
    ctl = _region(js, "src/components/grid/codemap/treeCtl.ts")
    assert "new MutationObserver(" in ctl and "attributeFilter:" in ctl
    assert '"data-accent"' in ctl and '"data-surface"' in ctl
    # Paint tools disarm after one use (Shift keeps them armed).
    assert in_bundle('if (!shift) this.setTool("explore")', ctl)
    # DPR is capped at 2.
    assert "Math.min(2, window.devicePixelRatio || 1)" in ctl


def test_layout_search_runs_off_the_ui_thread_and_is_cached(js):
    """The search took ~43 s on a 4.8k-file repo in the prototype; in the app it
    runs in a module Web Worker (a stable file next to app.js), is cached per
    repo + data in IndexedDB, and warm-starts from the previous layout."""
    worker = STATIC / "codetree-worker.js"
    assert worker.exists(), "the layout worker is not in the committed static dir"
    w = worker.read_text(encoding="utf-8")
    assert "self.onmessage" in w and "buildModel" in w
    eng = _region(js, "src/lib/codetree/engine.ts")
    assert '"/codetree-worker.js"' in eng
    assert 'type: "module"' in eng
    assert "loadRecords(repo)" in eng and "saveRecord(repo, sig, rec)" in eng
    assert "WARM_RUN_MAX" in eng
    cache = _region(js, "src/lib/codetree/cache.ts")
    assert '"mindflock-codetree"' in cache and "indexedDB.open(" in cache


def test_no_dead_dialogs_on_the_codemap_path(js):
    for mod in CODEMAP_MODULES:
        body = _region(js, mod)
        hits = DEAD_DIALOG.findall(body)
        assert not hits, f"{mod} calls {hits} — a no-op in Electron"
    # The push/PR/merge override is an inline card, never confirm().
    override = _function(js, "offerRedZoneOverride")
    assert "errorPop(" in override
    assert not DEAD_DIALOG.search(override)
    assert '" anyway"' in override and '"Open map"' in override


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


def test_chord_and_palette_entries(js):
    # Ctrl+K M → the Map tab of the focused session.
    assert in_bundle('m: { desc: "Code map", run: (t) => {', js)
    assert in_bundle('useUi.getState().setLastTab(t, "map")', js)
    # Palette: per-session Code map + the repo-level dialog.
    assert "`Code map — ${t}`" in js
    assert '"Zones…"' in js
    assert 'openDialogFor("red-zones")' in js


def test_rail_chip_and_seen_marker(js):
    assert '"mf_codemap_seen"' in js
    assert '"stagechip rzchip "' in js
    for cls in ('"rz-breach"', '"rz-warn"', '"rz-ok"'):
        assert cls in js, cls
    # Row field from the server snapshot.
    assert "inst.redzone" in js
    # Clicking the chip opens the Map.
    row = _region(js, "src/components/sidebar/SidebarRow.tsx")
    assert 'setLastTab(title, "map")' in row


def test_events_reach_toasts_and_bell(js):
    for ev in (
        "session.red_zone_blocked",
        "session.red_zone_breached",
        "session.red_zone_tampered",
    ):
        assert js.count(f'"{ev}"') >= 2, ev  # the bell's case + the toast subscriber
    bell = _region(js, "src/components/NotificationsBell.tsx")
    assert (
        bell.count('cls: "n-warn"') >= 5
    )  # the three new cases join the existing warns
    toasts = _region(js, "src/components/EventToasts.tsx")
    assert "isReplay(env)" in toasts


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


def test_map_failure_note_keeps_the_lists_reachable(js):
    """The first reads failing (a provisioning session 409s, a restarting
    server 5xx) shows one note — only while there is NOTHING to show; a tree
    or a live poll that did arrive keeps rendering."""
    tab = _region(js, "src/components/grid/CodeMapTab.tsx")
    assert "const failed = !!err && !snap && !live && !model" in tab
    assert '"cm-note cm-note-err"' in tab


def test_map_breach_set_is_the_servers_current_set(js):
    """A feed record's ``breach`` is history (it outlives the revert); only
    ``live.breaches`` says what is breached now — it drives "pushing is
    blocked" in the Session panel and on this session's agent card."""
    tab = _region(js, "src/components/grid/CodeMapTab.tsx")
    assert in_bundle("const breachList = live?.breaches || EMPTY_BREACHES", tab)
    assert in_bundle("currentBreaches(breachList)", tab)
    assert "feedBreachKey" not in tab
    assert "f.breach)" not in tab


def test_map_client_contract_fixes(js):
    tab = _region(js, "src/components/grid/CodeMapTab.tsx")
    # Running needs the session to be working (a refused call leaves a pre).
    assert in_bundle("feedState(feed, now, activity)", tab)
    # Case-insensitive filesystems: the map classifies like the guard does.
    assert in_bundle("zoneDoc(zones, live?.companions, live?.companion_files, ci)", tab)
    assert in_bundle('new RegExp(z.re, ci ? "i" : "")', tab)
    assert in_bundle("treeZones(model, zones, ci)", tab)
    # ONE predicate for every zone decision on the map.
    assert in_bundle("classifyPath(p, zdoc)", tab)
    # "Seen" is stamped on the server's clock, never the browser's.
    assert in_bundle("markCodemapSeen(title, serverNow(", tab)
    assert "markCodemapSeen(title)" not in tab
    # A partial import graph keeps being re-asked until it completes.
    assert "PARTIAL_REFRESH_MS" in tab and "snapStale(" in tab
    # A refused push is its own badge.
    assert '"blocked push"' in _region(
        js, "src/components/grid/codemap/SessionPanel.tsx"
    )
    # The pill label follows state; the tooltip is the server's sentence.
    assert in_bundle("guardPill(live?.guard", tab)
    # Tree-painted paths are anchored ("/name") so a root leaf means that path.
    zones = _region(js, "src/lib/codetree/zones.ts")
    assert in_bundle("anchoredZonePath(n.path)", zones)
    assert in_bundle("anchoredZonePath(target.file.path)", zones)
    # Painting is one POST to the server; the tree draws the server's answer.
    assert in_bundle("patternFor(M, { node, file })", tab)
    assert in_bundle('scope: kind === "green" ? "worktree" : "repo"', tab)
    assert in_bundle(
        'a.openAdd("red", anchoredZonePath(b.path))',
        _region(js, "src/components/grid/codemap/SessionPanel.tsx"),
    )


def test_zones_dialog_is_a_modal(js):
    """Delete on a zone's x button, or Ctrl+W in its pattern input, must not
    end the session behind the dialog; Escape closes it with focus anywhere."""
    km = _region(js, "src/lib/keymap.ts")
    assert '"red-zones"' in km and '"red-zones-dialog"' in km
    dlg = _region(js, "src/components/dialogs/RedZonesDialog.tsx")
    assert in_bundle('document.addEventListener("keydown", onKey)', dlg)


def test_plan_first_needs_a_plan_capable_cli(js):
    """Plan first on a CLI without plan support would leave the agent waiting
    on a Go the Map can't show: the option is disabled and never sent."""
    assert "(needs a CLI with plan support — Claude)" in js
    assert in_bundle("if (planFirst && planOk) body.plan_first = true", js)
    assert in_bundle("prov.plan_supported !== false", js)


def test_style_css_has_map_rules(css):
    for sel in (
        ".pane-map",
        ".cm-toolbar",
        ".ct-stage",
        ".ct-canvas",
        ".ct-left",
        # the bird index: a left column in wide panes, a slim left rail in small ones (never a top strip)
        ".ct-bix",
        ".ct-bix-row",
        ".ct-bix-subs",
        ".ct-bix-rail",
        ".ct-rail-row",
        ".ct-rail-tile",
        ".ct-bix-rules",
        # CHANGES: where the changes are, what could break (on demand), one-click ⛔ / ✓ per changed folder
        ".ct-bix-changes",
        ".ct-bix-atrisk",
        ".ct-bix-ranked",
        ".ct-bix-folder",
        ".ct-bix-zb",
        ".ct-legend",
        ".ct-mini",
        ".ct-activity",
        ".ct-info",
        ".ct-tip",
        ".ct-toast",
        ".ct-growing",
        ".ct-drawer",
        ".cm-side",
        ".cm-add",
        ".cm-guard.g-guarded",
        ".cm-guard.g-green",
        ".stagechip.rzchip.rz-breach",
        "#red-zones-panel",
        "#cs-errors .cs-error-actions",
    ):
        assert sel in css, sel
    # The tree's colours are custom properties derived from the app tokens…
    assert re.search(r"--ct-sky-top:\s*color-mix\(in srgb, var\(--bg\)", css)
    # …with a light-theme set, and the toolbar folds its labels in a narrow pane.
    assert ".light .ct-root" in css and "--ct-fol-a" in css
    assert "@container (max-width: 760px)" in css
    narrow = css[css.index("@container (max-width: 760px)") :]
    assert re.search(r"\.ct-toolbar \.lbl\s*\{\s*display: none", narrow)


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
