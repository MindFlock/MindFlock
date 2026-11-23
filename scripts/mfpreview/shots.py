"""Screenshots of a running preview: Playwright (the dev group's
``playwright`` package) driving the system Chrome — the journey tests'
recipe, so no ``playwright install`` download is needed.

``--view`` names a place in the app:

* ``sky`` (default: the Sky's "All" tab), ``sky/<session>`` (that
  session's route), ``grid``;
* ``settings`` or ``settings/<screen>`` (general, appearance, providers, …);
* ``intake``, ``schedule``, ``new``, ``hub`` (the Hub panel open);
* ``phone`` — the phone page ``/m`` (390 px wide unless ``--width``);
* ``#<anything>`` — a raw URL hash.

``--theme`` is ``dark`` / ``light`` (or ``all`` = both); ``--surface`` is a
surface set from Settings → Appearance (or ``all``). Several combinations
make several files: ``-o shot.png`` becomes ``shot-dark-oriole.png`` and so on.

The app pops a "needs you" card for a waiting session; the shot presses its
Later first (``--keep-popups`` keeps it). ``--click SELECTOR`` (repeatable,
Playwright selector syntax such as ``text=Stops``) clicks before shooting, to
open a menu or a dialog.
"""

from __future__ import annotations

import os
import shutil
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from mfpreview.sandbox import PreviewError, call

#: Settings → Appearance's surfaces ("" = Swallow, the default) — kept in
#: step with frontend/src/components/settings/screens/Appearance.tsx by
#: tests/unit/test_preview_sandbox.py.
SURFACES = (
    "",
    "raven",
    "heron",
    "owl",
    "wren",
    "magpie",
    "cardinal",
    "macaw",
    "pheasant",
    "oriole",
    "lorikeet",
    "roller",
    "goldfinch",
    "toucan",
    "quetzal",
    "greenjay",
    "gouldian",
    "mallard",
    "kingfisher",
    "peacock",
    "bluejay",
    "bunting",
    "violetear",
    "mandarin",
    "flamingo",
)
THEMES = ("dark", "light")
SETTINGS_SCREENS = (
    "general",
    "connections",
    "notifications",
    "coding",
    "providers",
    "accounts",
    "localmodel",
    "orchestration",
    "workspace",
    "ide",
    "cleanup",
    "security",
    "appearance",
    "devices",
    "mobile",
    "voice",
    "peer",
    "extensions",
    "doctor",
    "logs",
    "advanced",
)
VIEWS = (
    "sky",
    "sky/<session>",
    "grid",
    "settings",
    "settings/<screen>",
    "intake",
    "schedule",
    "new",
    "hub",
    "phone",
    "#<hash>",
)


def chrome_binary() -> Optional[str]:
    env = os.environ.get("MINDFLOCK_CHROME", "").strip()
    if env and os.access(env, os.X_OK):
        return env
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


def missing() -> Optional[str]:
    """Why shots cannot run here, or None."""
    try:
        import playwright.sync_api  # noqa: F401
    except Exception:  # noqa: BLE001
        return (
            "screenshots need the playwright package from the dev group: "
            "`uv sync --group dev --group web`, then run through `uv run`"
        )
    if chrome_binary() is None:
        return "screenshots need Chrome or Chromium on PATH (or $MINDFLOCK_CHROME)"
    return None


def resolve_view(view: str) -> Dict[str, Any]:
    """``view`` → ``{"path", "hash", "local": {...}, "phone": bool}``."""
    v = (view or "sky").strip()
    local: Dict[str, str] = {}
    if v == "phone" or v == "m" or v == "/m":
        return {"path": "/m", "hash": "", "local": local, "phone": True}
    if v.startswith("#"):
        return {"path": "/", "hash": v, "local": local, "phone": False}
    head, _, rest = v.partition("/")
    if head == "sky":
        local["mf_view"] = "sky"
        # The Sky's own tab: every route ("all"), or one session's.
        local["mf_sky_view"] = "one" if rest else "all"
        return {
            "path": "/",
            "hash": "#sky" + ("/" + rest if rest else ""),
            "local": local,
            "phone": False,
        }
    if head == "grid":
        local["mf_view"] = "grid"
        return {"path": "/", "hash": "", "local": local, "phone": False}
    if head == "settings":
        screen = rest or "general"
        if screen not in SETTINGS_SCREENS:
            raise PreviewError(
                "unknown settings screen %r; one of: %s"
                % (screen, ", ".join(SETTINGS_SCREENS))
            )
        return {
            "path": "/",
            "hash": "#settings/" + screen,
            "local": local,
            "phone": False,
        }
    if head == "intake":
        return {
            "path": "/",
            "hash": "#intake/" + (rest or "tickets"),
            "local": local,
            "phone": False,
        }
    if head in ("schedule", "new", "hub"):
        return {"path": "/", "hash": "#" + head, "local": local, "phone": False}
    raise PreviewError("unknown view %r; one of: %s" % (view, ", ".join(VIEWS)))


def expand(themes: str, surfaces: str) -> List[tuple]:
    ts = (
        list(THEMES)
        if themes == "all"
        else [t.strip() for t in themes.split(",") if t.strip()]
    )
    for t in ts:
        if t not in THEMES:
            raise PreviewError("unknown theme %r; one of: dark, light, all" % t)
    if surfaces == "all":
        ss = list(SURFACES)
    else:
        ss = [
            ("" if s.strip() in ("", "default", "swallow") else s.strip())
            for s in surfaces.split(",")
        ]
    for s in ss:
        if s not in SURFACES:
            raise PreviewError(
                "unknown surface %r; one of: default, %s"
                % (s, ", ".join(x for x in SURFACES if x))
            )
    return [(t, s) for s in ss for t in ts]


def out_name(out: Path, theme: str, surface: str, many: bool) -> Path:
    if not many:
        return out
    return out.with_name(
        "%s-%s-%s%s" % (out.stem, theme, surface or "default", out.suffix or ".png")
    )


def take(
    base_url: str,
    view: str,
    out: Path,
    themes: str = "dark",
    surfaces: str = "",
    width: int = 0,
    height: int = 0,
    full_page: bool = False,
    settle: float = 2.0,
    scale: float = 1.0,
    clicks: Sequence[str] = (),
    keep_popups: bool = False,
    first_run: bool = False,
) -> List[Dict[str, Any]]:
    """Shoot ``view`` once per (theme, surface); returns one record per file
    with the page errors the browser reported."""
    why = missing()
    if why:
        raise PreviewError(why)
    from playwright.sync_api import sync_playwright

    spec = resolve_view(view)
    combos = expand(themes, surfaces)
    many = len(combos) > 1
    if not width:
        width = 390 if spec["phone"] else 1440
    if not height:
        height = 844 if width <= 500 else 900
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    results = []
    try:
        version = str(
            call(base_url, "GET", "/api/remote/hello")[1].get("version") or ""
        )
    except Exception:  # noqa: BLE001
        version = ""
    with sync_playwright() as pw:
        browser = pw.chromium.launch(
            executable_path=chrome_binary(),
            headless=True,
            args=["--no-sandbox", "--disable-gpu"],
        )
        try:
            for theme, surface in combos:
                # The server is the source of truth for the theme (synced
                # prefs) and the surface (ui.surface): the app adopts them
                # over localStorage, so set them there. "What's new" is marked
                # seen and the tours done, so no modal covers the view.
                # A first-run scenario keeps its welcome tour.
                prefs: Dict[str, Any] = {"theme": theme}
                if not first_run:
                    prefs.update(welcome_tour=True, hints=False)
                    if version:
                        prefs["whats_new_seen"] = version
                call(base_url, "POST", "/api/prefs", prefs)
                call(base_url, "POST", "/api/settings", {"ui": {"surface": surface}})
                local = dict(spec["local"])
                local.update({"cs_theme": theme, "cs_surface": surface})
                if not first_run:
                    local.update({"mf_welcome_tour": "true", "mf_hints": "false"})
                ctx = browser.new_context(
                    viewport={"width": width, "height": height},
                    device_scale_factor=scale,
                    is_mobile=spec["phone"],
                    has_touch=spec["phone"],
                    color_scheme=theme,
                )
                ctx.add_init_script(
                    "(() => { const kv = %s; for (const k in kv) {"
                    " try { if (!sessionStorage.getItem('mfpv:' + k)) {"
                    " localStorage.setItem(k, kv[k]); sessionStorage.setItem('mfpv:' + k, '1'); } }"
                    " catch (e) {} } })();" % _js(local)
                )
                page = ctx.new_page()
                errors: List[str] = []
                page.on("pageerror", lambda exc, errors=errors: errors.append(str(exc)))
                page.on(
                    "console",
                    lambda msg, errors=errors: (
                        errors.append("console: " + msg.text)
                        if msg.type == "error"
                        else None
                    ),
                )
                page.goto(base_url + spec["path"] + spec["hash"], wait_until="load")
                if not spec["phone"]:
                    try:
                        page.locator("#boot-loading").wait_for(
                            state="detached", timeout=30_000
                        )
                    except Exception:  # noqa: BLE001 — shoot what is there
                        errors.append("boot screen still up after 30 s")
                time.sleep(settle)
                if not keep_popups:
                    _dismiss_asks(page)
                for sel in clicks:
                    try:
                        page.locator(sel).first.click(timeout=10_000)
                    except Exception as err:  # noqa: BLE001 — say so, shoot anyway
                        errors.append(
                            "--click %r: %s" % (sel, str(err).splitlines()[0])
                        )
                    time.sleep(0.8)
                path = out_name(out, theme, surface, many)
                page.screenshot(path=str(path), full_page=full_page)
                results.append(
                    {
                        "file": str(path),
                        "theme": theme,
                        "surface": surface or "default",
                        "errors": errors,
                    }
                )
                ctx.close()
        finally:
            browser.close()
            call(base_url, "POST", "/api/settings", {"ui": {"surface": ""}})
    return results


def _dismiss_asks(page) -> None:
    """Put off the "needs you" pop-up the app raises for a waiting session
    (the desktop's ``.ask-modal``, the phone page's card: both have a Later
    button) so it does not cover the view being shot; ``--keep-popups``
    shoots it instead."""
    for _ in range(4):
        later = page.get_by_role("button", name="Later", exact=True)
        try:
            visible = [b for b in later.all() if b.is_visible()]
            if not visible:
                return
            visible[0].click(timeout=3_000)
        except Exception:  # noqa: BLE001
            return
        time.sleep(0.6)


def _js(d: Dict[str, str]) -> str:
    import json

    return json.dumps(d)
