"""Settings → Appearance: selectable accent + surface themes, shared with the
mobile page. Palettes live in theme.css (html[data-accent=…] accent trio,
html[data-surface=…] surface sets with a light-mode counterpart each); the
choice persists server-side as ui.accent / ui.surface so all devices match,
with localStorage (cs_accent / cs_surface) as a pre-paint cache."""

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

# The region tokens a multi-color set steers, and the app-palette copies that let
# a popover floating out of a recolored region opt back out. Both halves are
# aliased to the globals on :root, so single-hue sets are unaffected.
REGION_VARS = tuple(
    "--%s-%s" % (region, part)
    for region in ("topbar", "sidebar")
    for part in ("bg", "hi", "border", "text", "muted")
)
APP_ALIAS_VARS = (
    "--app-panel",
    "--app-panel-2",
    "--app-border",
    "--app-text",
    "--app-muted",
)


def test_appearance_screen_and_nav_present():
    html = client.get("/").text
    assert "Appearance" in client.get("/app.js").text  # nav item
    assert '"appearance"' in client.get("/app.js").text  # screen
    assert '"accent-swatches"' in client.get("/app.js").text
    assert '"surface-swatches"' in client.get("/app.js").text


def test_swatch_per_preset_plus_defaults():
    html = client.get("/").text
    # Default swatches clear the override (empty choice = built-in look).
    assert '"data-accent-choice"' in client.get("/app.js").text
    assert '"data-surface-choice"' in client.get("/app.js").text
    js = client.get("/app.js").text
    for name in ACCENTS:
        assert '"%s"' % name in js, name
    for name in SURFACES:
        assert '"%s"' % name in js, name


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


def _button_glyph_markup(js, marker, span):
    """A button's own markup plus the body of any *Glyph component it renders.

    A glyph used in more than one place gets hoisted into its own component, so
    the <svg> stops being inline next to the button — resolve that one hop
    before checking what paints it."""
    win = js.split(marker, 1)[1][:span]
    parts = [win]
    for name in sorted(set(re.findall(r"jsx\((\w+Glyph)\b", win))):
        decl = "function %s(" % name
        assert decl in js, "%s renders <%s/> but it is not in the bundle" % (
            marker,
            name,
        )
        parts.append(js.split(decl, 1)[1][:1200])
    return "\n".join(parts)


def test_top_bar_glyphs_are_monochrome_not_emoji():
    """An emoji paints its own colors, so 🌙/🔔 vanish on Goldfinch's lemon bar
    or Toucan's amber one. Both are currentColor SVGs, which follow the top bar's
    rebound --text."""
    js = client.get("/app.js").text
    theme_btn = _button_glyph_markup(js, '"theme-btn"', 1200)
    assert "🌙" not in theme_btn and "☀" not in theme_btn
    assert '"currentColor"' in theme_btn
    bell = _button_glyph_markup(js, '"notif-btn"', 1600)
    assert "🔔" not in bell
    assert '"currentColor"' in bell


def test_region_tokens_alias_the_globals_and_reach_the_regions():
    css = client.get("/style.css").text
    root = css.split(":root {", 1)[1].split("}", 1)[0]
    for var in REGION_VARS + APP_ALIAS_VARS:
        assert var + ":" in root, "base :root missing %s" % var
    # Aliases must NOT be repeated in .light: a custom property resolves its
    # var()s against the value that won on the same element, so one declaration
    # on :root already picks up the light palette. A copy there would rot.
    light = css.split(".light {", 1)[1].split("}", 1)[0]
    for var in REGION_VARS:
        assert var + ":" not in light, ".light re-declares %s" % var
    # Each region rebinds the generic surface tokens on itself (regions.css) —
    # that's what makes every control inside it follow the region hue with no
    # per-component CSS.
    for region in ("topbar", "sidebar"):
        block = re.search(
            r"#%s\s*\{([^}]*--panel:\s*var\(--%s-bg\)[^}]*)\}" % (region, region), css
        )
        assert block, "regions.css does not rebind --panel on #%s" % region
        for generic, token in (
            ("--panel-2", "hi"),
            ("--border", "border"),
            ("--text", "text"),
            ("--muted", "muted"),
        ):
            want = "%s: var(--%s-%s)" % (generic, region, token)
            assert want in block.group(1), "#%s missing `%s`" % (region, want)
    # Popovers that float out of a recolored region opt back into the app palette.
    assert "--panel: var(--app-panel)" in css


def test_both_pages_link_theme_css_and_preapply_cached_choice():
    for page in ("/", "/m"):
        html = client.get(page).text
        assert '"/theme.css"' in html, page
        # Inline <head> script applies the cached choice — no flash on load.
        assert "localStorage.getItem('cs_accent')" in html, page
        assert "localStorage.getItem('cs_surface')" in html, page


def test_desktop_base_palette_is_themable():
    css = client.get("/style.css").text
    # :root and .light must carry the accent companions + terminal vars so
    # rgba() glows, light-mode deep text, and xterm all resolve per theme.
    for scope in (":root {", ".light {"):
        block = css.split(scope, 1)[1].split("}", 1)[0]
        for var in ("--accent-rgb:", "--accent-deep:", "--term-bg:", "--term-fg:"):
            assert var in block, "%s missing %s" % (scope, var)
    # No accent-purple glow may remain hard-coded (would ignore the theme).
    assert "rgba(125, 86, 244" not in css


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


def test_js_reads_live_vars_and_syncs_server():
    js = client.get("/app.js").text
    # Terminal canvas/cursor + favicon follow the live CSS vars.
    assert "function cssVar(" in js
    assert '"--term-bg"' in js and '"--term-fg"' in js
    assert "function cssVar" in js
    # Swatches persist to the server (all devices) and the local cache.
    assert "/api/settings" in js and '"cs_surface"' in js
    mjs = client.get("/mobile.js").text
    assert "function termTheme()" in mjs and '"--term-bg"' in mjs
    assert '"/api/settings"' in mjs  # pulls the server-chosen look
    assert "cs_surface" in mjs  # caches it for pre-paint apply


def test_no_legacy_preset_map_remains():
    """The retired-name migration map is gone from both bundles."""
    for path in ("/app.js", "/mobile.js"):
        assert "LEGACY_PRESETS" not in client.get(path).text, path


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
