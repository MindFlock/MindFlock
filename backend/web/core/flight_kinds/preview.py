"""The ``preview`` stop (docs/flights.md "Round 15", task ``preview-shots``):
run the project's preview-deploy command in the session's worktree and keep
the URL it prints.

The command prints the URL as its last non-empty line (a bare URL, or a line
ending in one: "Website draft URL: https://…"), or anywhere as a line
``MINDFLOCK_PREVIEW_URL=<url>`` (the last such line wins over the last line).
It runs as the ``command`` kind's job (:func:`flight_driver.run_shell`) with
``MINDFLOCK_BRANCH`` set to the session's branch. ``detail.url`` then fills
``{preview}`` in later prompts and in a ``screenshot`` stop's URLs.

``stop_command`` (optional) takes the preview down: :func:`stop_all` runs it
for every done preview stop of a flight, best effort; the driver calls it
when the flight finishes or is cancelled.
"""

from __future__ import annotations

import asyncio
import os
import re
import subprocess
import time
from typing import List, Optional
from urllib.parse import urlparse

from backend import log
from backend.web.core import flight_kinds as _kinds
from backend.web.core import flights as _flights

#: How much of the command's output the job keeps, and the step's detail.
KEEP = 8000
TAIL = 2000
#: How long one ``stop_command`` may take.
STOP_TIMEOUT_S = 120.0

_ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]|\x1b\][^\x07]*\x07")
_MARKER = re.compile(r"^\s*MINDFLOCK_PREVIEW_URL=(\S+)\s*$")
_URL = re.compile(r"https?://\S+", re.I)


def _lines(output: str) -> List[str]:
    return _ANSI.sub("", output or "").replace("\r\n", "\n").split("\n")


def valid_url(text: str) -> str:
    """``text`` (trailing punctuation dropped) when it is an http(s) URL with
    a host, else ""."""
    url = str(text or "").strip().rstrip(".,;:)]}>\"'")
    try:
        parsed = urlparse(url)
        parsed.port  # noqa: B018 — raises on a bad port
    except ValueError:
        return ""
    if parsed.scheme.lower() not in ("http", "https") or not parsed.hostname:
        return ""
    return url


def find_url(output: str) -> str:
    """The preview URL in a command's output: the last
    ``MINDFLOCK_PREVIEW_URL=<url>`` line, else the last http(s) URL on the
    last non-empty line; "" when neither is a valid URL."""
    lines = _lines(output)
    for line in reversed(lines):
        m = _MARKER.match(line)
        if m:
            return valid_url(m.group(1))
    for line in reversed(lines):
        if line.strip():
            found = _URL.findall(line)
            return valid_url(found[-1]) if found else ""
    return ""


def host_of(url: str) -> str:
    """The receipt: the URL's host (and port, when it has one)."""
    parsed = urlparse(url)
    host = parsed.hostname or ""
    return ("%s:%d" % (host, parsed.port) if parsed.port else host)[:80]


def latest_url(flight: dict) -> str:
    """The URL of the flight's last done preview stop (in plan order), "" when
    none has flown — what ``{preview}`` fills."""
    url = ""
    for step in flight.get("steps") or []:
        if step.get("kind") != "preview":
            continue
        st = (flight.get("step_states") or {}).get(step.get("id")) or {}
        if st.get("state") == "done":
            url = str((st.get("detail") or {}).get("url") or "") or url
    return url


def _config(raw: dict, strict: bool, where: str) -> dict:
    c = raw if isinstance(raw, dict) else {}
    command = _flights._text(c.get("command"), 4000)
    if not command and strict:
        raise _flights.PlanError(
            "a preview needs the command that deploys it", where + ".command"
        )
    return {
        "command": command,
        "timeout_minutes": _flights._int(c.get("timeout_minutes"), 20, 1, 24 * 60),
        "stop_command": _flights._text(c.get("stop_command"), 4000),
    }


def _tail(output: str) -> str:
    return (output or "")[-TAIL:]


def _reason(head: str, output: str) -> str:
    tail = (output or "").strip()
    room = TAIL - len(head) - 1
    if tail and room > 40:
        return head + "\n" + tail[-room:]
    return head[:TAIL]


def _judge(flight, step, ss, obs, now) -> dict:
    job = obs.get("preview")
    if not job:
        return {"action": "run", "note": "starting the preview again (interrupted)"}
    if job.get("running"):
        return _flights._wait("deploying the preview")
    output = str(job.get("output") or "")
    code = job.get("exit")
    detail = {"exit": code, "output": _tail(output)}
    if code != 0:
        head = (
            "the preview command did not finish"
            if code == -1
            else "the preview command exited %s" % code
        )
        return {"action": "fail", "reason": _reason(head, output), "detail": detail}
    url = find_url(output)
    if not url:
        return {
            "action": "fail",
            "reason": _reason(
                "the preview command printed no URL (its last line, or a "
                "MINDFLOCK_PREVIEW_URL=<url> line)",
                output,
            ),
            "detail": detail,
        }
    detail.update(url=url, receipt=host_of(url))
    return {"action": "done", "note": "preview at %s" % url, "detail": detail}


async def _run(flight, step, action, now) -> dict:
    from backend.web.core import flight_driver as D

    cmd = str(step["config"].get("command") or "").strip()
    if not cmd:
        raise _kinds.StepFailed("the preview stop has no command")
    sess = await asyncio.to_thread(D._session_obs, flight, time.time(), False)
    folder = str(sess.get("folder") or "")
    if not folder:
        raise _kinds.StepFailed("the session has no worktree to deploy from")
    D.run_shell(
        flight,
        step,
        cmd,
        float(step["config"].get("timeout_minutes") or 20) * 60.0,
        folder,
        env_extra={"MINDFLOCK_BRANCH": str(sess.get("branch") or "")},
        keep=KEEP,
    )
    return dict(action, detail=dict(action.get("detail") or {}, command=cmd))


async def _observe(flight, step, ss, sess, now):
    from backend.web.core import flight_driver as D

    return D.job_of(flight, step)


def _log(level: str, fmt: str, *args) -> None:
    logger = getattr(log, level, None)  # None until log.Initialize ran
    if logger is not None:
        try:
            logger.Printf(fmt, *args)
        except Exception:  # noqa: BLE001 — logging never fails a stop
            pass


def stop_all(flight: Optional[dict]) -> List[dict]:
    """Take down every preview this flight put up: run each done preview
    stop's ``stop_command`` (in the session's worktree when it still exists,
    with ``MINDFLOCK_PREVIEW_URL`` / ``MINDFLOCK_BRANCH`` / ``MINDFLOCK_FLIGHT``
    set). Best effort and blocking (call it on a thread): every outcome is
    logged and returned as ``{step, exit, output}``; never raises."""
    if not flight:
        return []
    out: List[dict] = []
    folder = branch = ""
    looked = False
    for step in flight.get("steps") or []:
        if step.get("kind") != "preview":
            continue
        cmd = str((step.get("config") or {}).get("stop_command") or "").strip()
        st = (flight.get("step_states") or {}).get(step.get("id")) or {}
        if not cmd or st.get("state") != "done":
            continue
        if not looked:
            looked = True
            try:
                from backend.web.core import flight_driver as D

                sess = D._session_obs(flight, time.time(), False)
                folder = str(sess.get("folder") or "")
                branch = str(sess.get("branch") or "")
            except Exception:  # noqa: BLE001 — the session may be long gone
                folder = branch = ""
        env = dict(os.environ)
        env.update(
            MINDFLOCK_FLIGHT=str(flight.get("id") or ""),
            MINDFLOCK_PREVIEW_URL=str((st.get("detail") or {}).get("url") or ""),
            MINDFLOCK_BRANCH=branch,
        )
        try:
            cp = subprocess.run(
                ["/bin/sh", "-c", cmd],
                cwd=folder if folder and os.path.isdir(folder) else None,
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                timeout=STOP_TIMEOUT_S,
            )
            code, text = cp.returncode, cp.stdout.decode("utf-8", "replace")
        except subprocess.TimeoutExpired:
            code, text = -1, "timed out after %d s" % int(STOP_TIMEOUT_S)
        except OSError as err:
            code, text = -1, "could not run it: %s" % err
        if code != 0:
            _log(
                "ErrorLog",
                "flight %s: stopping preview %s exited %v: %s",
                flight.get("id"),
                step.get("id"),
                code,
                text[-300:],
            )
        else:
            _log(
                "InfoLog",
                "flight %s: stopped preview %s",
                flight.get("id"),
                step.get("id"),
            )
        out.append({"step": step.get("id"), "exit": code, "output": _tail(text)})
    return out


KIND = _kinds.register(
    _kinds.Kind(
        kind="preview",
        spec={
            "label": "Preview",
            "actor": "robot",
            "family": "outside",
            "risk": ["remote"],
            "summary": (
                "Deploy a preview of the branch with your command and keep "
                "the URL it prints; {preview} fills it into later stops."
            ),
            "default_title": "Deploy a preview",
            "fields": [
                _flights._f(
                    "command",
                    "textarea",
                    "Command (prints the URL last, or MINDFLOCK_PREVIEW_URL=…)",
                    default="",
                    placeholder="npx vercel deploy --yes",
                ),
                _flights._f(
                    "timeout_minutes", "int", "Give up after (minutes)", default=20
                ),
                _flights._f(
                    "stop_command",
                    "textarea",
                    "Take it down when the route ends (optional)",
                    default="",
                    placeholder="npx vercel rm $MINDFLOCK_PREVIEW_URL --yes",
                ),
            ],
        },
        config=_config,
        judge=_judge,
        run=_run,
        observe=_observe,
    )
)
