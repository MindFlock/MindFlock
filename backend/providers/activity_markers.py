"""Provider-agnostic activity markers.

MindFlock reports what a coding-agent CLI is doing *right now* by having the
CLI's own lifecycle hooks write a per-session ``{state, ts}`` JSON marker, which
the web layer (:mod:`backend.web.core.agent_state`) trusts over pane-hash
guessing. The mechanism is identical across every CLI that exposes command
hooks with a JSON-on-stdin payload carrying a ``session_id`` — Claude Code's
``settings.local.json`` hooks and Codex's ``.codex/hooks.json`` hooks share the
exact same config shape and payload field names. Only the config-file location
and the event→state map differ per provider.

This module holds the shared primitives:

* marker read (:func:`read_activity_marker` / :func:`read_activity_marker_age`)
* the hook command a CLI runs to write the marker (:func:`hook_command`,
  :func:`notification_hook_command`)
* the settings-file merge that installs those hooks
  (:func:`merge_activity_hooks`) and its inverse
  (:func:`remove_activity_hooks`, used by ``mindflock uninstall``)

Each provider supplies its own file path + event map; the Claude provider
re-exports these names for backwards compatibility with its long-standing
call-sites and tests.
"""

from __future__ import annotations

import shlex
from typing import Optional, Sequence, Tuple

# The three states MindFlock's UI understands. A marker with any other value is
# ignored (treated as no signal).
_ACTIVITY_STATES = ("working", "idle", "clarify")
# Ignore markers older than this — an ancient marker belongs to a dead run and
# must not outvote live pane inspection.
_ACTIVITY_STALE_AFTER = 6 * 3600.0
# Tag embedded in every hook command we write, so a re-install can recognise
# and replace only its own entries (never a user-authored hook).
_HOOK_TAG = "# mindflock-activity"


def marker_dir():
    """The directory per-session activity markers live in.

    ``MINDFLOCK_ACTIVITY_MARKER_DIR`` overrides the default
    ``~/.mindflock-assistant/.activity-markers`` (tests point it at a tmp dir).
    """
    import os
    from pathlib import Path

    return Path(
        os.environ.get(
            "MINDFLOCK_ACTIVITY_MARKER_DIR",
            os.path.join(
                os.path.expanduser("~"), ".mindflock-assistant", ".activity-markers"
            ),
        )
    )


def marker_path(session_name: str):
    """Path to ``session_name``'s marker file (name sanitised for the FS)."""
    import re

    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", session_name)
    return marker_dir() / (safe + ".json")


def _read_entry(session_name: str):
    """``(state, ts)`` from the session's marker, or None.

    Returns the entry only when the state is recognised and the marker is fresh
    (< 6h old). Never raises. Shared by :func:`read_activity_marker` and
    :func:`read_activity_marker_age`.
    """
    import json
    import time

    try:
        raw = marker_path(session_name).read_text(encoding="utf-8")
        obj = json.loads(raw)
        state = obj.get("state")
        ts = float(obj.get("ts"))
    except Exception:  # noqa: BLE001 — missing/garbled marker = no signal
        return None
    if state not in _ACTIVITY_STATES:
        return None
    if time.time() - ts > _ACTIVITY_STALE_AFTER:
        return None
    return state, ts


def read_activity_marker(session_name: str) -> Optional[str]:
    """State recorded by the session's CLI hooks, or None.

    Returns ``working`` / ``idle`` / ``clarify`` when the marker parses and is
    fresh (< 6h old). Never raises.
    """
    entry = _read_entry(session_name)
    return entry[0] if entry else None


def read_activity_marker_age(session_name: str) -> Optional[float]:
    """Seconds since the (fresh) marker was written, or None if absent/stale.

    The marker only updates when a hook fires; a long thinking/generating
    stretch between tool calls, or an abandoned prompt, leaves it stale. The web
    layer uses this to decide when to re-verify a ``working`` / ``clarify``
    marker against the pane.
    """
    import time

    entry = _read_entry(session_name)
    return (time.time() - entry[1]) if entry else None


def hook_command(state: str, marker_dir=None, record_thread: bool = True) -> str:
    """The command a CLI hook runs to record ``state``.

    Resolves the *live* tmux session at fire-time (``tmux display-message
    #{session_name}`` — or ``MINDFLOCK_SESSION_NAME`` when the CLI exports it)
    and writes ``<marker dir>/<session>.json`` — instead of baking a fixed
    per-session path. This is essential when several sessions share one working
    directory (in-place sessions on the same repo): they share one hooks config
    file, so a fixed path would route every session's events into whichever
    session installed last. Resolving at runtime attributes each event to the
    session that actually fired it. If the session can't be resolved (hook
    running outside tmux), it no-ops (exit 0) and the web layer falls back to
    CPU/pane inspection.

    The MARKER DIRECTORY is resolved at fire-time too, from the hook's own
    environment (``MINDFLOCK_ACTIVITY_MARKER_DIR``, else the real
    ``~/.mindflock-assistant/.activity-markers``) — never baked in at install
    time. The install-time value was a live incident: a Verify session runs
    its sandboxed MindFlock with ``HOME`` redirected into a scratchpad, and
    when that instance re-pinned the SHARED repo's hooks file it embedded the
    sandbox's absolute path — after which every cohabiting session's hooks
    quietly wrote markers into a dead sandbox, their chips frozen on the last
    pre-poison reading ("idle" while visibly working). Resolving in the
    firing CLI's env gives each world its own markers: a sandboxed CLI writes
    into its sandbox, a real one into the real home, whoever installed last.
    ``marker_dir`` is accepted and ignored (kept for caller/test signatures).

    When ``record_thread`` is True (Claude), the hook also persists the payload's
    ``session_id`` as this window's resume-thread marker — the id
    ``claude --resume <id>`` targets after a crash. Codex records its own
    resume-thread id from its on-disk rollout files (the id its ``resume {id}``
    flag wants), so it installs the hook with ``record_thread=False`` to avoid
    clobbering that with a differently-shaped id.
    """
    import json

    lines = [
        "import json,sys,subprocess,os,time,re",
        "try:",
        "    p=json.load(sys.stdin)",
        "except Exception:",
        "    p={}",
        "if not isinstance(p,dict):",
        "    p={}",
        "s=os.environ.get('MINDFLOCK_SESSION_NAME') or ''",
        "if not s:",
        "    try:",
        "        s=subprocess.run(['tmux','display-message','-p','#{session_name}'],"
        "capture_output=True,text=True,timeout=5).stdout.strip()",
        "    except Exception:",
        "        s=''",
        "if not s:",
        "    raise SystemExit(0)",
        "s=re.sub(r'[^A-Za-z0-9_.-]','_',s)",
        "d=os.environ.get('MINDFLOCK_ACTIVITY_MARKER_DIR') or "
        "os.path.join(os.path.expanduser('~'),"
        "'.mindflock-assistant','.activity-markers')",
        "os.makedirs(d,exist_ok=True)",
        "open(os.path.join(d,s+'.json'),'w').write("
        "json.dumps({'state':%s,'ts':int(time.time())}))" % json.dumps(state),
    ]
    if record_thread:
        lines += [
            "sid=str(p.get('session_id') or '')",
            "if sid:",
            "    td=os.environ.get('MINDFLOCK_THREAD_MARKER_DIR') or "
            "os.path.join(os.path.expanduser('~'),"
            "'.mindflock-assistant','.thread-markers')",
            "    os.makedirs(td,exist_ok=True)",
            "    open(os.path.join(td,s+'.thread'),'w').write(sid)",
        ]
    code = "\n".join(lines) + "\n"
    return "python3 -c %s || true %s" % (shlex.quote(code), _HOOK_TAG)


def notification_hook_command(marker_dir=None) -> str:
    """The command a Notification hook runs — records ``clarify`` only for
    notifications that genuinely need the user (permission / plan / question).

    Claude Code fires a Notification ~60s after the session goes idle
    ("Claude is waiting for your input", ``notification_type: "idle_prompt"``);
    mapping that to clarify flipped every finished task to a false amber
    "needs input" one minute later. Claude Code passes the notification payload
    as JSON on the hook's stdin, so this command inspects it: the structured
    ``notification_type`` field is preferred (any ``idle*`` type is skipped —
    Stop already recorded "idle"), with the message substring as the fallback
    for versions without the field. An unreadable/garbled payload falls back to
    clarify so a real "needs input" is never silently dropped.
    """
    import json

    code = (
        "import json,sys,subprocess,os,time,re\n"
        "try:\n"
        "    d = json.load(sys.stdin)\n"
        "except Exception:\n"
        "    d = {}\n"
        "if not isinstance(d, dict):\n"
        "    d = {}\n"
        't = str(d.get("notification_type") or d.get("type") or "").lower()\n'
        'm = str(d.get("message") or "").lower()\n'
        'if t.startswith("idle") or "waiting for your input" in m:\n'
        "    sys.exit(0)\n"
        "s=os.environ.get('MINDFLOCK_SESSION_NAME') or ''\n"
        "if not s:\n"
        "    try:\n"
        "        s=subprocess.run(['tmux','display-message','-p','#{session_name}'],"
        "capture_output=True,text=True,timeout=5).stdout.strip()\n"
        "    except Exception:\n"
        "        s=''\n"
        "if not s:\n"
        "    sys.exit(0)\n"
        "s=re.sub(r'[^A-Za-z0-9_.-]','_',s)\n"
        "md=os.environ.get('MINDFLOCK_ACTIVITY_MARKER_DIR') or "
        "os.path.join(os.path.expanduser('~'),"
        "'.mindflock-assistant','.activity-markers')\n"
        "os.makedirs(md, exist_ok=True)\n"
        "open(os.path.join(md,s+'.json'), \"w\").write("
        'json.dumps({"state": "clarify", "ts": int(time.time())}))\n'
    )
    return "python3 -c %s || true %s" % (shlex.quote(code), _HOOK_TAG)


def is_mindflock_hook_entry(entry) -> bool:
    """True when ``entry`` (a settings hook matcher-group) is one of ours."""
    try:
        return any(
            _HOOK_TAG in (h.get("command") or "")
            for h in entry.get("hooks", [])
            if isinstance(h, dict)
        )
    except Exception:  # noqa: BLE001
        return False


def remove_activity_hooks(settings_path) -> bool:
    """Strip MindFlock's hook entries from ``settings_path`` — the inverse of
    :func:`merge_activity_hooks`.

    Only entries carrying :data:`_HOOK_TAG` are dropped, so a user's own hooks
    (and every non-hook key in the file) survive untouched. An event whose list
    empties out is dropped, and if that leaves the file with nothing but an
    empty ``hooks`` map the file itself is deleted — a settings file MindFlock
    created solely to hold these hooks should not outlive them.

    Returns True when something was actually removed. Never raises: uninstall
    walks directories that may be read-only, deleted, or not JSON at all.

    Motivation: the hook body is self-contained inline ``python3`` with no
    dependency on the ``mindflock`` binary, so hooks left behind in a user's
    repo keep firing (and keep re-creating ``~/.mindflock-assistant``) long
    after the engine is gone. Uninstall has to remove them explicitly.
    """
    import json
    from pathlib import Path

    settings_path = Path(settings_path)
    try:
        data = json.loads(settings_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    if not isinstance(data, dict):
        return False
    hooks = data.get("hooks")
    if not isinstance(hooks, dict):
        return False

    changed = False
    for event in list(hooks.keys()):
        entries = hooks.get(event)
        if not isinstance(entries, list):
            continue
        kept = [e for e in entries if not is_mindflock_hook_entry(e)]
        if len(kept) == len(entries):
            continue
        changed = True
        if kept:
            hooks[event] = kept
        else:
            del hooks[event]
    if not changed:
        return False

    # The file existed only to carry our hooks -> remove it entirely rather
    # than leaving an inert `{"hooks": {}}` behind in the user's repo.
    if not hooks and list(data.keys()) == ["hooks"]:
        try:
            settings_path.unlink()
        except OSError:
            return False
        return True

    try:
        settings_path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    except OSError:
        return False
    return True


def remove_git_exclude(workdir: str, rel: str) -> bool:
    """Drop ``rel`` from the repo's ``.git/info/exclude`` — the inverse of
    :func:`ensure_git_excluded`. Returns True when a line was removed.

    Only an exact whole-line match is removed, so a user's own pattern that
    merely contains ``rel`` as a substring is never touched. Best-effort: any
    failure (not a repo, unreadable exclude file) is a silent no-op.
    """
    import os
    import subprocess

    try:
        cp = subprocess.run(
            ["git", "-C", workdir, "rev-parse", "--git-path", "info/exclude"],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=30,
        )
        if cp.returncode != 0:
            return False
        path = cp.stdout.decode("utf-8", "replace").strip()
        if not path:
            return False
        if not os.path.isabs(path):
            path = os.path.join(workdir, path)
        with open(path, encoding="utf-8") as f:
            lines = f.read().splitlines()
        kept = [ln for ln in lines if ln.strip() != rel]
        if len(kept) == len(lines):
            return False
        with open(path, "w", encoding="utf-8") as f:
            f.write("\n".join(kept) + ("\n" if kept else ""))
        return True
    except Exception:  # noqa: BLE001
        return False


def ensure_git_excluded(workdir: str, rel: str) -> None:
    """Append ``rel`` to the repo's ``.git/info/exclude`` unless already listed.

    Uses ``git rev-parse --git-path`` so worktrees (whose ``.git`` is a file
    pointing at the shared gitdir) resolve correctly. Best-effort — a hooks
    config file MindFlock writes into a user's checkout should never show up as
    a dirty file.
    """
    import os
    import subprocess

    try:
        cp = subprocess.run(
            ["git", "-C", workdir, "rev-parse", "--git-path", "info/exclude"],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
        if cp.returncode != 0:
            return
        path = cp.stdout.decode("utf-8", "replace").strip()
        if not path:
            return
        if not os.path.isabs(path):
            path = os.path.join(workdir, path)
        try:
            existing = open(path, encoding="utf-8").read()
        except OSError:
            existing = ""
        if rel in existing.splitlines():
            return
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            if existing and not existing.endswith("\n"):
                f.write("\n")
            f.write(rel + "\n")
    except Exception:  # noqa: BLE001
        pass


def merge_activity_hooks(
    settings_path,
    event_states: Sequence[Tuple[str, str]],
    session_name: str,
    notification_event: Optional[str] = None,
    record_thread: bool = True,
) -> bool:
    """Merge MindFlock's activity-reporting hooks into ``settings_path``.

    ``settings_path`` is a JSON file with the shared hooks shape
    ``{"hooks": {<Event>: [{"hooks": [{"type": "command", "command": ...}]}]}}``
    — the same schema Claude Code's ``settings.local.json`` and Codex's
    ``.codex/hooks.json`` both use. Created (with parents) if absent.

    ``event_states`` is an iterable of ``(event, state)`` — each event fires a
    command that records ``state``. ``notification_event`` (if given) names the
    one event that must inspect its payload instead of recording a fixed state
    (Claude's ``Notification`` idle-timeout filter); pass None for CLIs without
    such an event (Codex has a dedicated ``PermissionRequest`` instead).

    Merge, never clobber: user-authored keys and hook entries are preserved;
    only prior MindFlock entries (recognised by :data:`_HOOK_TAG`) are replaced,
    so re-installing with a new session name is idempotent. Returns True when the
    file was written. Raises nothing on the happy path but callers still wrap it
    — a launch must never break over hook install.
    """
    import json
    from pathlib import Path

    settings_path = Path(settings_path)
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        data = json.loads(settings_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = {}
    if not isinstance(data, dict):
        data = {}
    hooks = data.get("hooks")
    if not isinstance(hooks, dict):
        hooks = {}
        data["hooks"] = hooks
    for event, state in event_states:
        entries = hooks.get(event)
        if not isinstance(entries, list):
            entries = []
        entries = [e for e in entries if not is_mindflock_hook_entry(e)]
        if notification_event is not None and event == notification_event:
            cmd = notification_hook_command()
        else:
            cmd = hook_command(state, record_thread=record_thread)
        entries.append({"hooks": [{"type": "command", "command": cmd}]})
        hooks[event] = entries
    settings_path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    return True
