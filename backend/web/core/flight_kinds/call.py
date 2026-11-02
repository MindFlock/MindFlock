"""The ``call`` stop: one HTTP request out (docs/flights.md "Round 15").

``method`` GET | POST | PUT to ``url`` with ``headers`` ("K: V" lines) and
``body`` (``fill_prompt`` placeholders plus ``{receipt}``: the previous flown
stop's receipt); done when the response status starts with ``expect``
(default "2"). The request runs on a thread (urllib, no new dependency) into
:data:`_JOBS`, which ``observe`` reads. Headers often carry a secret: the
plan stores them as given (it is local) but nothing here ever logs them.
"""

from __future__ import annotations

import threading
import urllib.error
import urllib.parse
import urllib.request
from typing import Dict, List, Optional, Tuple

from backend.web.core import flights as _flights
from backend.web.core.flight_kinds import Kind, StepFailed, register

_f = _flights._f

METHODS = ("GET", "POST", "PUT")
#: What a call keeps of the response body.
KEEP_BODY = 2000
#: What it reads of the response at most.
MAX_READ = 1024 * 1024

_JOBS: Dict[Tuple[str, str], dict] = {}
_JOBS_LOCK = threading.Lock()


def _config(raw: dict, strict: bool, where: str) -> dict:
    method = _flights._text(raw.get("method"), 10).upper() or "POST"
    if method not in METHODS:
        if strict:
            raise _flights.PlanError(
                "method is one of %s" % ", ".join(METHODS), where + ".method"
            )
        method = "POST"
    url = _flights._text(raw.get("url"), 2000)
    if strict:
        if not url:
            raise _flights.PlanError("a call needs a URL", where + ".url")
        if not url.lower().startswith(("http://", "https://", "{")):
            raise _flights.PlanError(
                "the URL starts with http:// or https://", where + ".url"
            )
    return {
        "method": method,
        "url": url,
        "headers": _flights._text(raw.get("headers"), 4000),
        "body": _flights._text(raw.get("body")),
        "expect": _flights._text(raw.get("expect"), 3) or "2",
        "timeout_minutes": _flights._int(raw.get("timeout_minutes"), 5, 1, 60),
    }


def _host(url: str) -> str:
    try:
        return urllib.parse.urlsplit(url).hostname or url[:60]
    except ValueError:
        return url[:60]


def _title(config: dict) -> str:
    url = str(config.get("url") or "")
    return "Call %s" % _host(url) if url else "Call a URL"


def parse_headers(text: str) -> List[Tuple[str, str]]:
    """``K: V`` lines → pairs (blank lines and lines without a colon are
    skipped)."""
    out = []
    for line in str(text or "").splitlines():
        if ":" not in line:
            continue
        k, v = line.split(":", 1)
        k, v = k.strip(), v.strip()
        if k:
            out.append((k, v))
    return out


def previous_receipt(flight: dict, step: dict) -> str:
    """The receipt of the nearest flown stop before ``step`` ("" when none)."""
    ids = [s["id"] for s in flight["steps"]]
    i = ids.index(step["id"]) if step["id"] in ids else len(ids)
    for sid in reversed(ids[:i]):
        ss = flight["step_states"].get(sid) or {}
        if ss.get("state") == "done":
            return str((ss.get("detail") or {}).get("receipt") or "")
    return ""


def _fill(text: str, flight: dict, step: dict) -> str:
    out = _flights.fill_prompt(text, flight)
    return out.replace("{receipt}", previous_receipt(flight, step))


def _request(
    job: dict, method: str, url: str, headers, body: Optional[bytes], timeout: float
) -> None:
    from backend.web.core import flight_driver as _driver

    req = urllib.request.Request(url, data=body, method=method)
    for k, v in headers:
        req.add_header(k, v)
    if body is not None and not any(k.lower() == "content-type" for k, _ in headers):
        stripped = body.lstrip()[:1]
        req.add_header(
            "Content-Type",
            (
                "application/json"
                if stripped in (b"{", b"[")
                else "text/plain; charset=utf-8"
            ),
        )
    try:
        with urllib.request.urlopen(
            req, timeout=timeout
        ) as resp:  # noqa: S310 — the plan's own URL
            status = int(resp.status)
            text = resp.read(MAX_READ).decode("utf-8", "replace")
        out = {"status": status, "body": text[-KEEP_BODY:], "error": ""}
    except urllib.error.HTTPError as err:
        try:
            text = err.read(MAX_READ).decode("utf-8", "replace")
        except Exception:  # noqa: BLE001
            text = ""
        out = {"status": int(err.code), "body": text[-KEEP_BODY:], "error": ""}
    except Exception as err:  # noqa: BLE001 — URLError, timeout, a bad URL
        reason = getattr(err, "reason", None) or err
        out = {"status": 0, "body": "", "error": str(reason)[:300] or "no answer"}
    with _JOBS_LOCK:
        job.update(out, running=False)
    _driver.wake()


async def _run(flight, step, action, now) -> dict:
    cfg = step["config"]
    url = _fill(cfg.get("url") or "", flight, step).strip()
    if not url.lower().startswith(("http://", "https://")):
        raise StepFailed("not an http(s) URL: %s" % url[:200])
    method = cfg.get("method") or "POST"
    headers = parse_headers(cfg.get("headers") or "")
    body: Optional[bytes] = None
    if method != "GET" and cfg.get("body"):
        body = _fill(cfg["body"], flight, step).encode("utf-8")
    job = {"running": True, "status": None, "body": "", "error": "", "method": method}
    with _JOBS_LOCK:
        _JOBS[(flight["id"], step["id"])] = job
    threading.Thread(
        target=_request,
        args=(
            job,
            method,
            url,
            headers,
            body,
            float(cfg.get("timeout_minutes") or 5) * 60.0,
        ),
        name="mf-flight-call-%s" % flight["id"],
        daemon=True,
    ).start()
    return dict(
        action, note="calling %s %s" % (method, _host(url)), detail={"host": _host(url)}
    )


async def _observe(flight, step, ss, sess, now):
    with _JOBS_LOCK:
        job = _JOBS.get((flight["id"], step["id"]))
        return dict(job) if job else None


def _judge(flight, step, ss, obs, now) -> dict:
    job = obs.get("call")
    if not job:
        # The server restarted mid-request: the answer is lost, ask again.
        return {"action": "run", "note": "calling again (it was interrupted)"}
    if job.get("running"):
        return _flights._wait(
            "calling %s %s" % (job.get("method") or "", ss["detail"].get("host") or "")
        )
    if job.get("error"):
        return {
            "action": "fail",
            "reason": "no answer from %s: %s"
            % (ss["detail"].get("host") or "the URL", job["error"]),
        }
    status = int(job.get("status") or 0)
    tail = str(job.get("body") or "")
    detail = {"status": status, "body": tail}
    if str(status).startswith(str(step["config"].get("expect") or "2")):
        return {
            "action": "done",
            "note": "HTTP %d" % status,
            "detail": dict(detail, receipt=str(status)),
        }
    return {
        "action": "fail",
        "reason": "HTTP %d: %s" % (status, tail.strip()[-300:] or "no body"),
        "detail": detail,
    }


register(
    Kind(
        kind="call",
        spec={
            "label": "Call",
            "actor": "robot",
            "family": "outside",
            "risk": ["remote"],
            "summary": "Send one HTTP request (a deploy hook, a Slack message, "
            "an API) and check its status.",
            "default_title": "Call a URL",
            "fields": [
                _f("method", "select", "Method", default="POST", options=METHODS),
                _f("url", "text", "URL", default="", placeholder="https://…"),
                _f(
                    "headers",
                    "textarea",
                    "Headers (one “Name: value” per line)",
                    default="",
                    placeholder="Authorization: Bearer …",  # pragma: allowlist secret
                ),
                _f(
                    "body",
                    "textarea",
                    "Body ({branch}, {pr}, {ticket}, {subject}, {receipt})",
                    default="",
                ),
                _f("expect", "text", "Done when the status starts with", default="2"),
                _f("timeout_minutes", "int", "Give up after (minutes)", default=5),
            ],
        },
        config=_config,
        judge=_judge,
        run=_run,
        observe=_observe,
        needs_session=False,
        default_title=_title,
    )
)
