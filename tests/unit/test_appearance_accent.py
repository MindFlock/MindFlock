"""Settings → Appearance: selectable accent + surface themes, shared with the
mobile page. Palettes live in theme.css (html[data-accent=…] accent trio,
html[data-surface=…] surface sets with a light-mode counterpart each); the
choice persists server-side as ui.accent / ui.surface so all devices match,
with localStorage (cs_accent / cs_surface) as a pre-paint cache.

The React screen, the top-bar glyphs and the base stylesheet are pinned in
vitest (frontend/src/__tests__/appearanceThemes.test.ts); this file keeps what
is served as-is (theme.css, mobile.css, mobile.js, the page heads) and the
settings round trip."""

import re

from starlette.testclient import TestClient

from backend.config import settings as settings_mod
from backend.web import server

client = TestClient(server.app)

# One bird per color — the rainbow hues plus pink (flamingo), gold (pheasant)
# and silver (heron). Every color exists in BOTH dimensions under the same key;
# Swallow (the default) and Raven are surface-only.
COLORS = (
    "cardinal",
    "oriole",
    "goldfinch",
    "quetzal",
    "bluejay",
    "bunting",
    "violetear",
    "flamingo",
    "pheasant",
    "heron",
)
# Surface-only sets: birds with no single "their color", so no accent counterpart.
SURFACE_ONLY = (
    "raven",
    "macaw",
    "lorikeet",
    "roller",
    "toucan",
    "greenjay",
    "gouldian",
    "mallard",
    "kingfisher",
    "peacock",
    "mandarin",
)
# The quiet sets — a single neutral throughout. Everything else is multi-color: a
# hue per region (top bar / sidebar / window), the way the bird is marked.
QUIET = ("raven", "heron")
ACCENTS = COLORS
SURFACES = COLORS + SURFACE_ONLY
MULTI = tuple(s for s in SURFACES if s not in QUIET)

# The region tokens a multi-color set steers. They are aliased to the globals on
# :root (pinned with the base stylesheet in frontend/src/__tests__/
# appearanceThemes.test.ts), so single-hue sets are unaffected.
REGION_VARS = tuple(
    "--%s-%s" % (region, part)
    for region in ("topbar", "sidebar")
    for part in ("bg", "hi", "border", "text", "muted")
)


def test_theme_css_defines_accent_trio_per_preset():
    css = client.get("/theme.css").text
    for name in ACCENTS:
        m = re.search(r'html\[data-accent="%s"\]\s*\{([^}]*)\}' % name, css)
        assert m, "no CSS block for accent theme %s" % name
        for var in ("--accent:", "--accent-rgb:", "--accent-deep:"):
            assert var in m.group(1), "%s missing %s" % (name, var)


def test_theme_css_defines_full_surface_set_dark_and_light():
    """Each surface set must swap every surface var AND ship a light-mode
    palette (html.light[…]) so the 🌙 toggle works inside every theme."""
    css = client.get("/theme.css").text
    surface_vars = (
        "--bg:",
        "--panel:",
        "--panel-2:",
        "--border:",
        "--text:",
        "--muted:",
        "--term-bg:",
        "--term-fg:",
    )
    for name in SURFACES:
        for sel in (r'html\[data-surface="%s"\]', r'html\.light\[data-surface="%s"\]'):
            m = re.search((sel % name) + r"\s*\{([^}]*)\}", css)
            assert m, "no %s block for surface theme %s" % (sel, name)
            for var in surface_vars:
                assert var in m.group(1), "%s missing %s" % (name, var)


def test_multicolor_sets_recolor_every_region_in_both_modes():
    """A multi-color set must paint all three regions — a set that recolors the
    bar but leaves the sidebar aliased to the window is just a single-hue set."""
    css = client.get("/theme.css").text
    for name in MULTI:
        for sel in (r'html\[data-surface="%s"\]', r'html\.light\[data-surface="%s"\]'):
            block = re.search((sel % name) + r"\s*\{([^}]*)\}", css).group(1)
            for var in REGION_VARS:
                assert var + ":" in block, "%s missing %s" % (name, var)
            # The three regions must actually differ, or the set isn't multi-color.
            hues = {
                re.search(r"%s:\s*([^;]+);" % v, block).group(1).strip()
                for v in ("--topbar-bg", "--sidebar-bg", "--panel")
            }
            assert len(hues) == 3, "%s reuses a hue across regions: %s" % (name, hues)


def test_quiet_sets_leave_the_regions_alone():
    """Swallow/Raven/Heron are the deliberately calm choices — one neutral, no
    region hues. The base-sheet aliases are what make that free; a stray region
    token here would silently paint a bar on them."""
    css = client.get("/theme.css").text
    for name in QUIET:
        for sel in (r'html\[data-surface="%s"\]', r'html\.light\[data-surface="%s"\]'):
            block = re.search((sel % name) + r"\s*\{([^}]*)\}", css).group(1)
            for var in REGION_VARS:
                assert var + ":" not in block, "%s should not set %s" % (name, var)


def _contrast(fg: str, bg: str) -> float:
    def rel(hex_color):
        out = []
        for i in (1, 3, 5):
            c = int(hex_color[i : i + 2], 16) / 255
            out.append(c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4)
        return 0.2126 * out[0] + 0.7152 * out[1] + 0.0722 * out[2]

    hi, lo = sorted((rel(fg), rel(bg)), reverse=True)
    return (hi + 0.05) / (lo + 0.05)


def test_every_set_is_legible_in_every_region():
    """The palettes are deliberately loud, so this is the guard rail: each region
    is either dark with light text or bright with dark text, and the numbers have
    to prove it. A brighter repaint that strands the text fails here."""
    css = client.get("/theme.css").text
    for m in re.finditer(
        r'html(\.light)?\[data-surface="([\w-]+)"\]\s*\{([^}]*)\}', css
    ):
        light, name, body = m.groups()
        v = dict(re.findall(r"(--[\w-]+):\s*(#[0-9a-f]{6})", body))
        tag = "%s%s" % (name, ".light" if light else "")
        pairs = [
            ("text/panel", v["--text"], v["--panel"], 4.5),
            ("text/bg", v["--text"], v["--bg"], 4.5),
            ("text/panel-2", v["--text"], v["--panel-2"], 4.5),
            ("muted/panel", v["--muted"], v["--panel"], 3.0),
            ("term fg/bg", v["--term-fg"], v["--term-bg"], 4.5),
        ]
        for r in ("topbar", "sidebar"):
            if "--%s-bg" % r not in v:
                continue  # a quiet set; the aliased globals are covered above
            pairs += [
                # Hover fill included on purpose: it's where a saturated region
                # most easily slips under the bar, and it's a hit target.
                ("%s text/bg" % r, v["--%s-text" % r], v["--%s-bg" % r], 4.5),
                ("%s text/hi" % r, v["--%s-text" % r], v["--%s-hi" % r], 4.5),
                ("%s muted/bg" % r, v["--%s-muted" % r], v["--%s-bg" % r], 3.0),
                # A hairline carries no text, but it still has to be a visible edge.
                ("%s border/bg" % r, v["--%s-border" % r], v["--%s-bg" % r], 1.4),
            ]
        for label, fg, bg, floor in pairs:
            got = _contrast(fg, bg)
            assert got >= floor, "%s %s: %.2f < %s (%s on %s)" % (
                tag,
                label,
                got,
                floor,
                fg,
                bg,
            )


def test_both_pages_link_theme_css_and_preapply_cached_choice():
    for page in ("/", "/m"):
        html = client.get(page).text
        assert '"/theme.css"' in html, page
        # Inline <head> script applies the cached choice — no flash on load.
        assert "localStorage.getItem('cs_accent')" in html, page
        assert "localStorage.getItem('cs_surface')" in html, page


def test_mobile_css_is_themable():
    css = client.get("/mobile.css").text
    root = css.split(":root {", 1)[1].split("}", 1)[0]
    for var in (
        "--bg:",
        "--panel:",
        "--border:",
        "--text:",
        "--muted:",
        "--accent:",
        "--term-bg:",
    ):
        assert var in root, var
    # The old fixed purples/surfaces must be gone below :root.
    body = css.split("}", 1)[1]
    assert "#7d56f4" not in body
    assert "#14171f" not in body


def test_ui_settings_persist_accent_and_surface(tmp_path, monkeypatch):
    monkeypatch.setenv("MINDFLOCK_SETTINGS_FILE", str(tmp_path / "settings.json"))
    settings_mod.invalidate()
    try:
        settings_mod.update_settings(ui={"accent": "bluejay", "surface": "raven"})
        got = settings_mod.load_settings()
        assert got.ui.accent == "bluejay"
        assert got.ui.surface == "raven"
        # Clearing falls back to defaults (empty = built-in look).
        settings_mod.update_settings(ui={"accent": "", "surface": ""})
        got = settings_mod.load_settings()
        assert got.ui.accent == "" and got.ui.surface == ""
    finally:
        settings_mod.invalidate()


def test_mobile_js_reads_live_vars_and_syncs_server():
    mjs = client.get("/mobile.js").text
    assert "function termTheme()" in mjs and '"--term-bg"' in mjs
    assert '"/api/settings"' in mjs  # pulls the server-chosen look
    assert "cs_surface" in mjs  # caches it for pre-paint apply
    # The retired-name migration map is gone from the hand-written page too.
    assert "LEGACY_PRESETS" not in mjs


def test_bundle_saves_the_look_to_a_route_the_server_serves():
    """Contract: swatches persist through /api/settings (all devices)."""
    assert "/api/settings" in client.get("/app.js").text
    assert "post" in server.app.openapi()["paths"]["/api/settings"]
