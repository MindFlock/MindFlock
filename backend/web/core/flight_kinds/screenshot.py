"""The ``screenshot`` stop (docs/flights.md "Round 15", task
``preview-shots``): render each URL at each width with the Chrome / Chromium
the server finds, and keep the PNGs for the stop's box.

The browser is ``$MINDFLOCK_CHROME`` (a path or a name on PATH), else the
first of ``google-chrome``, ``google-chrome-stable``, ``chromium``,
``chromium-browser`` on PATH; none → the stop fails "no browser". It runs in
its own headless mode (``--headless=new --screenshot=…``), no playwright: one
shell script loops over the URL × width pairs as the ``command`` kind's job
(:func:`flight_driver.run_shell`), each shot ``<n>.png`` under
``<flights dir>/shots/<fid>/<sid>/``, with a throwaway profile so a Chrome
the owner has open is never touched. The kind's router serves them at
``GET /api/flights/{fid}/shots/{sid}/{n}.png``.

URLs may carry ``{preview}`` (the flight's last done preview stop's URL) and
the other prompt placeholders. ``comment_pr`` posts the list of shots (names
and widths, text only) on the session's PR through the GitHub REST helpers
when there is a PR and a token; otherwise it is skipped and ``detail.comment``
says why.
"""

from __future__ import annotations

import asyncio
import os
import re
import shlex
import shutil
import time
from typing import Dict, List, Optional, Tuple

from backend.web.core import flight_kinds as _kinds
from backend.web.core import flights as _flights
from backend.web.core.flight_kinds import preview as _preview

BROWSERS = ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser")
HEIGHT = 900
MAX_SHOTS = 40
MAX_WIDTH = 4000
MIN_WIDTH = 200
TAIL = 2000

_NAME = re.compile(r"^[0-9]+\.png$")
_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")

# (flight, step, since) -> what the PR comment came to, once per server run.
_COMMENTED: Dict[Tuple[str, str, float], str] = {}


def find_browser() -> str:
    """The browser to shoot with: ``$MINDFLOCK_CHROME`` (a path, or a name
    on PATH), else the first of :data:`BROWSERS` on PATH; "" when none."""
    env = os.environ.get("MINDFLOCK_CHROME", "").strip()
    if env:
        if os.path.isfile(env) and os.access(env, os.X_OK):
            return env
        found = shutil.which(env)
        if found:
            return found
    for name in BROWSERS:
        found = shutil.which(name)
        if found:
            return found
    return ""


def shots_dir(flight_id: str, step_id: str) -> str:
    """``<flights dir>/shots/<fid>/<sid>`` (ids outside ``[A-Za-z0-9_-]``
    become ``_``, so an id never walks out of the folder)."""

    def part(text: str) -> str:
        return re.sub(r"[^A-Za-z0-9_-]", "_", str(text or "")) or "_"

    return os.path.join(_flights.flights_dir(), "shots", part(flight_id), part(step_id))


def parse_widths(text: str) -> List[int]:
    """ "1280, 390" → [1280, 390]: every number in the text, clamped to
    200..4000, duplicates dropped; [1280] when there are none."""
    out: List[int] = []
    for n in re.findall(r"\d+", str(text or "")):
        w = max(MIN_WIDTH, min(MAX_WIDTH, int(n)))
        if w not in out:
            out.append(w)
    return out or [1280]


def fill_urls(text: str, flight: dict) -> List[str]:
    """The stop's URLs, one per line, ``{preview}`` and the prompt
    placeholders filled; blank lines and lines that are not http(s) URLs once
    filled are dropped."""
    url = _preview.latest_url(flight)
    filled = str(text or "").replace("{preview}", url)
    filled = _flights.fill_prompt(filled, flight)
    out: List[str] = []
    for line in filled.splitlines():
        line = line.strip()
        if line and _preview.valid_url(line) == line and line not in out:
            out.append(line)
    return out


def plan_shots(urls: List[str], widths: List[int]) -> List[dict]:
    """Every URL × width, numbered from 1 (``n.png``), at most
    :data:`MAX_SHOTS`."""
    pairs = [{"n": 0, "url": u, "width": w} for u in urls for w in widths][:MAX_SHOTS]
    for i, p in enumerate(pairs, 1):
        p["n"] = i
    return pairs


def build_script(browser: str, folder: str, pairs: List[dict]) -> str:
    """The shell script that takes the shots: one headless browser run per
    pair into ``<folder>/<n>.png``, a throwaway profile, a failed shot noted
    and the rest still taken."""
    q = shlex.quote
    lines = [
        'profile="$(mktemp -d)"',
        "trap 'rm -rf \"$profile\"' EXIT",
    ]
    for p in pairs:
        out = os.path.join(folder, "%d.png" % p["n"])
        lines.append(
            "echo %s" % q("shot %d: %s at %dpx" % (p["n"], p["url"], p["width"]))
        )
        lines.append(
            " ".join(
                [
                    q(browser),
                    "--headless=new",
                    "--disable-gpu",
                    "--hide-scrollbars",
                    "--no-first-run",
                    "--no-default-browser-check",
                    '--user-data-dir="$profile"',
                    "--window-size=%d,%d" % (p["width"], HEIGHT),
                    q("--screenshot=" + out),
                    q(p["url"]),
                    "2>&1 || echo %s" % q("shot %d failed" % p["n"]),
                ]
            )
        )
    return "\n".join(lines) + "\n"


def list_shots(flight_id: str, step_id: str, pairs: List[dict]) -> List[dict]:
    """The shots on disk for the stop: ``[{name, url, width, page}]`` in
    number order (``url`` is the route that serves it, ``page`` what was
    shot); a PNG the plan does not know keeps ``width`` None."""
    folder = shots_dir(flight_id, step_id)
    try:
        names = [n for n in os.listdir(folder) if _NAME.match(n)]
    except OSError:
        return []
    known = {int(p["n"]): p for p in pairs or [] if str(p.get("n", "")).isdigit()}
    out = []
    for name in sorted(names, key=lambda n: int(n[:-4])):
        path = os.path.join(folder, name)
        try:
            if os.path.getsize(path) <= 0:
                continue
        except OSError:
            continue
        p = known.get(int(name[:-4])) or {}
        out.append(
            {
                "name": name,
                "url": "/api/flights/%s/shots/%s/%s" % (flight_id, step_id, name),
                "width": p.get("width"),
                "page": p.get("url") or "",
            }
        )
    return out


def comment_text(shots: List[dict]) -> str:
    lines = ["Screenshots from MindFlock (%d):" % len(shots)]
    for s in shots:
        width = "%dpx" % s["width"] if s.get("width") else "?"
        lines.append("- %s — %s at %s" % (s["name"], s.get("page") or "", width))
    return "\n".join(lines)


async def post_comment(folder: str, pr_url: str, text: str) -> str:
    """Post ``text`` on the PR at ``pr_url`` through ``github_pr``: its own
    ``comment_pr(wt, number, body)`` when it has one, else the REST seam
    (``POST /repos/<slug>/issues/<n>/comments``). Returns what happened, as
    ``detail.comment`` says it ("posted", "skipped: …", "failed: …")."""
    from backend.web.core import github_pr as _gh

    m = re.search(r"/pull/(\d+)", pr_url or "")
    if not m:
        return "skipped: the session has no PR"
    number = int(m.group(1))
    helper = getattr(_gh, "comment_pr", None)
    if callable(helper):
        res = await helper(folder, number, text)
        ok = getattr(res, "ok", bool(res))
        return "posted" if ok else "failed: %s" % (getattr(res, "error", "") or "?")
    ref = await asyncio.to_thread(_gh.repo_ref, folder) if folder else None
    if ref is None:
        return "skipped: no GitHub repo behind the session's origin"
    token = await _gh.api_token()
    if not token:
        return "skipped: no GitHub token"
    status, data = await _gh._request(
        "POST",
        "/repos/%s/issues/%d/comments" % (ref.slug, number),
        token=token,
        body={"body": text},
    )
    if status == 201:
        return "posted"
    return "failed: %s" % _gh._api_message(status, data)


# --------------------------------------------------------------------------- #
# The kind
# --------------------------------------------------------------------------- #
def _config(raw: dict, strict: bool, where: str) -> dict:
    c = raw if isinstance(raw, dict) else {}
    return {
        "urls": _flights._text(c.get("urls"), 4000) or "{preview}",
        "widths": _flights._text(c.get("widths"), 200) or "1280, 390",
        "timeout_minutes": _flights._int(c.get("timeout_minutes"), 10, 1, 24 * 60),
        "comment_pr": _flights._bool(c.get("comment_pr"), False),
    }


def _title(config: dict) -> str:
    return "Screenshots"


def _judge(flight, step, ss, obs, now) -> dict:
    seen = obs.get("screenshot") or {}
    job = seen.get("job")
    if not job:
        return {"action": "run", "note": "taking the screenshots again (interrupted)"}
    if job.get("running"):
        return _flights._wait("taking screenshots")
    output = str(job.get("output") or "")
    shots = list(seen.get("shots") or [])
    detail = {"shots": shots, "output": output[-TAIL:]}
    if not shots:
        head = "no screenshots were taken"
        tail = output.strip()
        reason = head + ("\n" + tail[-(TAIL - len(head) - 1) :] if tail else "")
        return {"action": "fail", "reason": reason, "detail": detail}
    if seen.get("comment"):
        detail["comment"] = seen["comment"]
    detail["receipt"] = "%d shot%s" % (len(shots), "" if len(shots) == 1 else "s")
    return {"action": "done", "note": detail["receipt"], "detail": detail}


async def _run(flight, step, action, now) -> dict:
    from backend.web.core import flight_driver as D

    cfg = step["config"]
    urls = fill_urls(cfg.get("urls") or "", flight)
    if not urls:
        raise _kinds.StepFailed(
            "no URL to shoot (is {preview} empty? a preview stop must fly first)"
        )
    browser = await asyncio.to_thread(find_browser)
    if not browser:
        raise _kinds.StepFailed(
            "no browser: install google-chrome or chromium, or set MINDFLOCK_CHROME"
        )
    folder = shots_dir(flight["id"], step["id"])

    def _fresh() -> None:
        os.makedirs(folder, exist_ok=True)
        for name in os.listdir(folder):
            if _NAME.match(name):
                os.unlink(os.path.join(folder, name))

    try:
        await asyncio.to_thread(_fresh)
    except OSError as err:
        raise _kinds.StepFailed("could not make the shots folder: %s" % err)
    pairs = plan_shots(urls, parse_widths(cfg.get("widths") or ""))
    D.run_shell(
        flight,
        step,
        build_script(browser, folder, pairs),
        float(cfg.get("timeout_minutes") or 10) * 60.0,
        folder,
        keep=8000,
    )
    return dict(
        action, detail=dict(action.get("detail") or {}, pairs=pairs, browser=browser)
    )


async def _observe(flight, step, ss, sess, now):
    from backend.web.core import flight_driver as D

    job = D.job_of(flight, step)
    out = {"job": job, "shots": [], "comment": ""}
    if not job or job.get("running"):
        return out
    pairs = (ss.get("detail") or {}).get("pairs") or []
    out["shots"] = await asyncio.to_thread(list_shots, flight["id"], step["id"], pairs)
    if out["shots"] and step["config"].get("comment_pr"):
        key = (flight["id"], step["id"], float(ss.get("since") or 0.0))
        if key not in _COMMENTED:
            sess = sess or {}
            try:
                _COMMENTED[key] = await post_comment(
                    str(sess.get("folder") or ""),
                    str(sess.get("pr_url") or ""),
                    comment_text(out["shots"]),
                )
            except Exception as err:  # noqa: BLE001 — a comment never fails the shots
                _COMMENTED[key] = "failed: %s" % err
        out["comment"] = _COMMENTED[key]
    return out


def _router():
    """The kind's route, or None where FastAPI is not installed (the CLI
    without the web extra still loads the kind)."""
    try:
        from fastapi import APIRouter
        from fastapi.responses import FileResponse, JSONResponse
    except ImportError:
        return None
    router = APIRouter()

    @router.get("/api/flights/{flight_id}/shots/{step_id}/{name}")
    async def get_shot(flight_id: str, step_id: str, name: str):
        """One PNG a screenshot stop took; 404 for anything else."""
        if not (_ID.match(flight_id) and _ID.match(step_id) and _NAME.match(name)):
            return JSONResponse({"error": "not_found"}, status_code=404)
        path = os.path.join(shots_dir(flight_id, step_id), name)
        if not os.path.isfile(path):
            return JSONResponse({"error": "not_found"}, status_code=404)
        return FileResponse(path, media_type="image/png")

    return router


KIND = _kinds.register(
    _kinds.Kind(
        kind="screenshot",
        spec={
            "label": "Screenshot",
            "actor": "robot",
            "family": "outside",
            "risk": [],
            "summary": (
                "Shoot each URL at each width with a headless Chrome; the "
                "pictures show on the stop ({preview} = the preview's URL)."
            ),
            "default_title": "Screenshots",
            "fields": [
                _flights._f(
                    "urls",
                    "textarea",
                    "URLs, one per line ({preview} = the preview's)",
                    default="{preview}",
                    placeholder="{preview}\n{preview}/settings",
                ),
                _flights._f("widths", "text", "Widths (px)", default="1280, 390"),
                _flights._f(
                    "timeout_minutes", "int", "Give up after (minutes)", default=10
                ),
                _flights._f(
                    "comment_pr",
                    "bool",
                    "Post the list of shots on the PR",
                    default=False,
                ),
            ],
        },
        config=_config,
        judge=_judge,
        run=_run,
        observe=_observe,
        needs_session=False,
        default_title=_title,
        router=_router(),
    )
)
