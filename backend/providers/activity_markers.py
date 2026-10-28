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

import re
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
# The variant tag a guard-carrying (tool-hook) command uses. It CONTAINS the
# base tag as a substring, so ``is_mindflock_hook_entry`` and uninstall keep
# recognising it, while ``hooks_armed`` can tell an armed session (this tag
# present on a PreToolUse entry) from a bare activity install.
#
# VERSION STAMP. The hook source is baked into the hooks file at install time,
# so a checkout keeps running whatever guard it was armed with — a v1 hook
# read a green rule as red and nothing ever replaced it, because "armed" only
# looked for the tag. The tag now carries the guard REVISION
# (``_tool_hook_src._MF_HOOK_REV``) and a short hash of the embedded source:
# ``hooks_armed`` requires the current hash or a NEWER revision, so the
# reconcile loop reinstalls a stale hook (Claude hot-reloads the file) while
# two builds on one worktree converge on the newer guard instead of
# overwriting each other every tick.
TOOL_HOOK_TAG_PREFIX = "# mindflock-activity tool-hook"

# --------------------------------------------------------------------------- #
# Voice mode (talking to a session from the phone). The hook script below
# carries two event-keyed branches for it: ``Stop`` files the payload's
# ``last_assistant_message`` beside the marker as ``<session>.reply.json`` (the
# exact text the session just said, so the spoken line costs no transcript
# scan), and ``UserPromptSubmit`` consumes ``<voice dir>/<session>.next`` —
# the ONE-SHOT request the /send delivery arms right before a ``say: true``
# prompt is typed (or the standing ``<voice dir>/say-all`` marker, every
# prompt while someone listens to everything): younger than :data:`VOICE_NEXT_MAX_AGE_S`, the hook prints
# :data:`VOICE_INSTRUCTION` (Claude Code injects a prompt hook's stdout as
# additional context, invisibly and with no relaunch); stale or not, it
# deletes the file, so a prompt that never got submitted cannot attach its
# Say line to the next one. The Say line costs tokens, so only the prompts
# whose reply will be spoken ask for it. The sticky ``<session>.on`` (a phone
# is in a voice conversation) is NOT read here any more — the Stop branch
# files every reply regardless. The flag dir is resolved at fire time
# exactly like the marker dir (never baked in). ``backend.web.core.voice``
# owns the flags, ``backend.web.core.spoken`` the reader; both read these
# constants so the three agree on the paths.
# --------------------------------------------------------------------------- #
#: The instruction, byte for byte from the build contract (the phone page
#: parses the ``Say:`` line it asks for). Re-exported as
#: ``backend.web.core.spoken.INSTRUCTION``; the /send route types the same
#: sentence after a prompt for a CLI whose hooks cannot inject it.
VOICE_INSTRUCTION = (
    "Voice mode is on: the person is listening, not reading, and may have been "
    "away. End your reply with one final line that starts with `Say:` followed "
    "by up to three short plain sentences a person can hear, under about 50 "
    "words — no code, paths or markdown: first the context (what you were "
    "working on, the feature or area in plain words, and the project when more "
    "than one could be meant), then what you did or will do, then, if you need "
    "something from them, the question. For example: We were working on "
    "MindFlock's settings screen, and the voice install button now sits in the "
    "Try-it row. Should I add it to the phone page too?"
)
VOICE_FLAG_DIR_ENV = "MINDFLOCK_VOICE_FLAG_DIR"
VOICE_FLAG_SUBDIR = (".mindflock-assistant", ".voice")
#: An armed ``.next`` older than this (seconds) is dropped unprinted.
VOICE_NEXT_MAX_AGE_S = 120
#: ``<voice dir>/say-all``: while it exists, EVERY prompt in every session
#: is asked for the ``Say:`` line (a listener has "read everything" on) —
#: prompts typed straight into the terminal included, which no ``.next`` can
#: reach. ``core.voice.set_say_all`` owns it; the hook only looks.
VOICE_SAY_ALL_FILE = "say-all"
#: Cap on the reply text the Stop hook files (characters).
REPLY_TEXT_MAX = 8000


def _source_hash8() -> str:
    try:
        import hashlib

        return hashlib.sha1(_tool_hook_source().encode("utf-8", "replace")).hexdigest()[
            :8
        ]
    except Exception:  # noqa: BLE001 — never break an import over a stamp
        return "00000000"


def marker_dir():
    """The directory per-session activity markers live in.

    ``MINDFLOCK_ACTIVITY_MARKER_DIR`` overrides the default
    ``~/.mindflock-assistant/.activity-markers`` (tests point it at a tmp dir).
    """
    import os
    from pathlib import Path

    from backend.config.home_guard import guard

    return Path(
        guard(
            os.environ.get(
                "MINDFLOCK_ACTIVITY_MARKER_DIR",
                os.path.join(
                    os.path.expanduser("~"), ".mindflock-assistant", ".activity-markers"
                ),
            ),
            "activity-marker dir",
        )
    )


def safe_session_name(session_name: str) -> str:
    """``session_name`` sanitised for a file name — the same substitution the
    hook script applies at fire time, so the writer and every reader (marker,
    reply file, voice flag) name one file."""
    import re

    return re.sub(r"[^A-Za-z0-9_.-]", "_", session_name or "")


def marker_path(session_name: str):
    """Path to ``session_name``'s marker file (name sanitised for the FS)."""
    return marker_dir() / (safe_session_name(session_name) + ".json")


def reply_path(session_name: str):
    """Path to ``session_name``'s last-reply file, ``<marker dir>/<session>
    .reply.json`` = ``{"ts", "text"}``, written by the Stop hook."""
    return marker_dir() / (safe_session_name(session_name) + ".reply.json")


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


def _tool_hook_source() -> str:
    """The embeddable source text of :mod:`backend.providers._tool_hook_src`.

    Read once via ``inspect.getsource`` (the module is pure defs + constants, so
    exec-compiling it defines ``_mf_tool_hook``) and cached. This is what makes
    the guard self-contained: the whole source is baked into the ``python3 -c``
    command as a literal, so the fire-time hook needs no ``backend`` import.
    """
    global _TOOL_HOOK_SRC
    if _TOOL_HOOK_SRC is None:
        import inspect

        from . import _tool_hook_src as _src

        _TOOL_HOOK_SRC = inspect.getsource(_src)
    return _TOOL_HOOK_SRC


_TOOL_HOOK_SRC = None


def _hook_rev() -> int:
    try:
        from . import _tool_hook_src as _src

        return int(getattr(_src, "_MF_HOOK_REV", 0))
    except Exception:  # noqa: BLE001
        return 0


TOOL_HOOK_REV = _hook_rev()
TOOL_HOOK_TAG = "%s v2.%d %s" % (TOOL_HOOK_TAG_PREFIX, TOOL_HOOK_REV, _source_hash8())
_TAG_RE = re.compile(
    re.escape(TOOL_HOOK_TAG_PREFIX) + r" v(\d+)(?:\.(\d+))?(?: ([0-9a-f]{8}))?"
)


def tool_hook_tag_rank(command: str) -> Optional[int]:
    """How a tool-hook command's tag compares to THIS build's: ``0`` = this
    build's exact hook, ``1`` = a NEWER revision (another build's; left
    alone), ``-1`` = older (a stale guard to heal) or same revision with
    another hash (a dev build — healed, but never called tampering). None
    when the command carries no MindFlock tool-hook tag."""
    if TOOL_HOOK_TAG in (command or ""):
        return 0
    m = _TAG_RE.search(command or "")
    if not m:
        return None
    major = int(m.group(1))
    rev = int(m.group(2) or 0)
    if major > 2 or (major == 2 and rev > TOOL_HOOK_REV):
        return 1
    return -1


def hook_command(
    state: str,
    marker_dir=None,
    record_thread: bool = True,
    tool_hook=None,
    prompt_context: Optional[bool] = None,
) -> str:
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

    The lookup targets the hook's OWN pane (``-t $TMUX_PANE``) and never runs
    without one. A bare ``display-message`` answers for tmux's "current"
    session, which, with no live pane to anchor it, is whichever session was
    most recently active, i.e. the window the user is looking at. That was a
    live incident: closing a ticket window killed its pane, the dying Claude's
    SessionEnd/Stop hook asked tmux, and the answer was the window the user had
    focused. That window got an ``idle`` marker and the dead conversation's
    thread id, so it read idle while it waited on a background agent, and its
    ``claude agents --json`` live signal (correctly ``busy``) was looked up
    under the wrong conversation and missed. A dead pane id answers empty, so
    the hook no-ops.

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

    Voice mode rides the same script (see :data:`VOICE_INSTRUCTION`): on a
    ``Stop`` payload the hook files ``last_assistant_message`` as the
    session's reply file (every CLI; best effort where the payload lacks the
    field), and on ``UserPromptSubmit`` it consumes the session's one-shot
    ``.next`` request (printing the instruction when the request is younger
    than :data:`VOICE_NEXT_MAX_AGE_S`, deleting it always) — only when
    ``prompt_context`` is true, which
    defaults to ``record_thread``: the Claude install. Claude Code documents a
    prompt hook's stdout as injected context; a CLI without that channel gets
    the same sentence typed after its prompt by the /send route, and a hook
    that ALSO printed it would say it twice.
    """
    import json

    if prompt_context is None:
        prompt_context = record_thread

    lines = [
        "import json,sys,subprocess,os,time,re",
        "try:",
        "    p=json.load(sys.stdin)",
        "except Exception:",
        "    p={}",
        "if not isinstance(p,dict):",
        "    p={}",
        "s=os.environ.get('MINDFLOCK_SESSION_NAME') or ''",
        "tp=os.environ.get('TMUX_PANE') or ''",
        "if not s and tp:",
        "    try:",
        "        s=subprocess.run(['tmux','display-message','-p','-t',tp,"
        "'#{session_name}'],capture_output=True,text=True,timeout=5).stdout.strip()",
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
        from . import auth_profiles

        lines += [
            "sid=str(p.get('session_id') or '')",
            "if sid:",
            "    td=os.environ.get('MINDFLOCK_THREAD_MARKER_DIR') or "
            "os.path.join(os.path.expanduser('~'),"
            "'.mindflock-assistant','.thread-markers')",
            "    os.makedirs(td,exist_ok=True)",
            "    open(os.path.join(td,s+'.thread'),'w').write(sid)",
            # Every conversation this window has had, one id per line (the
            # current marker above is overwritten on /clear): per-session cost
            # attributes exactly these conversations (thread_markers.history).
            "    hp=os.path.join(td,s+'.threads')",
            "    try:",
            "        h=open(hp).read().split()",
            "    except Exception:",
            "        h=[]",
            "    if sid not in h:",
            "        open(hp,'w').write('\\n'.join((h+[sid])[-500:])+'\\n')",
            # The hook runs INSIDE the session, so it can see which auth
            # profile the session was launched under and file a per-account
            # memory of this conversation beside the current marker. That is
            # what lets a swap back to this identity resume the thread it
            # actually owns instead of starting over. Absent for a session on
            # the CLI's ambient login, which writes only the line above — the
            # pre-profiles behaviour, byte for byte.
            "    a=re.sub(r'[^A-Za-z0-9_.-]','_',os.environ.get(%s) or '')"
            % json.dumps(auth_profiles.PROFILE_ID_ENV),
            "    if a:",
            "        open(os.path.join(td,s+'@'+a+'.thread'),'w').write(sid)",
        ]
    # Voice: the reply file (tmp + os.replace, so a reader never sees a torn
    # file) and the prompt-context instruction. Both keyed on the payload's
    # event name at FIRE time (one script serves every event), both wrapped so
    # they can never raise past the marker write above.
    lines += [
        "e=str(p.get('hook_event_name') or '')",
        "if e=='Stop':",
        "    try:",
        "        rp=os.path.join(d,s+'.reply.json')",
        "        rt=rp+'.%d.tmp'%os.getpid()",
        "        open(rt,'w').write(json.dumps({'ts':int(time.time()),"
        "'text':str(p.get('last_assistant_message') or '')[:%d]}))" % REPLY_TEXT_MAX,
        "        os.replace(rt,rp)",
        "    except Exception:",
        "        pass",
    ]
    if prompt_context:
        lines += [
            "if e=='UserPromptSubmit':",
            "    try:",
            "        vd=os.environ.get(%s) or os.path.join(os.path.expanduser('~'),%s)"
            % (
                json.dumps(VOICE_FLAG_DIR_ENV),
                ",".join(json.dumps(x) for x in VOICE_FLAG_SUBDIR),
            ),
            "        np=os.path.join(vd,s+'.next')",
            "        w=os.path.exists(os.path.join(vd,%s))"
            % json.dumps(VOICE_SAY_ALL_FILE),
            "        if os.path.exists(np):",
            "            try:",
            "                t=float(open(np).read().strip() or 0)",
            "            except Exception:",
            "                t=0",
            "            try:",
            "                os.remove(np)",
            "            except Exception:",
            "                pass",
            "            if 0<=time.time()-t<%d:" % VOICE_NEXT_MAX_AGE_S,
            "                w=True",
            "        if w:",
            "            print(%s)" % json.dumps(VOICE_INSTRUCTION),
            "    except Exception:",
            "        pass",
        ]
    tag = _HOOK_TAG
    if tool_hook in ("pre", "post", "fail"):
        # Run the red-zone guard FIRST — before the `if not s` exit and before
        # any marker/thread write — in its own try, so an unwritable marker dir
        # or an unresolved session name can never skip enforcement. The guard is
        # fail-open on its own bugs (the wrapped exec) and reads its guard file
        # at fire time, so a zone added mid-flight applies on the next tool call.
        guard_lines = [
            "_MF_SRC=%s" % repr(_tool_hook_source()),
            "try:",
            "    _mf_ns={}",
            "    exec(compile(_MF_SRC,'<mf-tool-hook>','exec'),_mf_ns)",
            "    _mf_ns['_mf_tool_hook'](p,s,%s)" % json.dumps(tool_hook),
            "except Exception:",
            "    pass",
        ]
        insert_at = lines.index("    raise SystemExit(0)") - 1
        lines[insert_at:insert_at] = guard_lines
        tag = TOOL_HOOK_TAG
    code = "\n".join(lines) + "\n"
    return "python3 -c %s || true %s" % (shlex.quote(code), tag)


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
        "tp=os.environ.get('TMUX_PANE') or ''\n"
        "if not s and tp:\n"
        "    try:\n"
        "        s=subprocess.run(['tmux','display-message','-p','-t',tp,"
        "'#{session_name}'],capture_output=True,text=True,timeout=5).stdout.strip()\n"
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
    # A `disableAllHooks: false` we added alongside our guard hooks has no reason
    # to outlive them (popping a False is harmless — the default is already off).
    if data.get("disableAllHooks") is False:
        data.pop("disableAllHooks", None)
        changed = True
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

    if not _atomic_write_json(settings_path, data):
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


def _atomic_write_json(path, data) -> bool:
    """Write ``data`` as pretty JSON to ``path`` via a tmp file + ``os.replace``.

    Atomic so a hot-reloading CLI (Claude Code re-reads settings mid-turn) never
    observes a truncated file and drops ALL local hooks. Returns True on success.
    """
    from backend.fsutil import atomic_write_json

    try:
        atomic_write_json(path, data, ensure_ascii=True, prefix="tmp")
        return True
    except OSError:
        return False


def hooks_armed(settings_path, any_version: bool = False) -> bool:
    """Whether the red-zone guard is armed in ``settings_path``.

    True when the file parses, has a PreToolUse entry whose command carries
    :data:`TOOL_HOOK_TAG` — the CURRENT source hash, or a NEWER guard
    revision another build installed (:func:`tool_hook_tag_rank`); a hook
    baked by an older build counts as not armed and gets reinstalled
    (``any_version=True``: any MindFlock tool hook counts) — and neither it
    nor the sibling ``settings.json`` sets
    ``disableAllHooks: true`` (a project-level ``true`` disables everything; a
    local ``false`` resists it — we require the effective value to be non-true).
    Never raises.
    """
    import json
    from pathlib import Path

    p = Path(settings_path)
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    if not isinstance(data, dict):
        return False
    hooks = data.get("hooks")
    if not isinstance(hooks, dict):
        return False
    pre = hooks.get("PreToolUse")
    if not isinstance(pre, list):
        return False
    ok = (-1, 0, 1) if any_version else (0, 1)
    tagged = any(
        tool_hook_tag_rank(h.get("command") or "") in ok
        for e in pre
        if isinstance(e, dict)
        for h in e.get("hooks", [])
        if isinstance(h, dict)
    )
    if not tagged:
        return False
    if data.get("disableAllHooks") is True:
        return False
    # A sibling project-level settings.json can force-disable, unless THIS file
    # (settings.local.json) explicitly sets false, which resists it (verified).
    if data.get("disableAllHooks") is not False:
        sibling = p.parent / "settings.json"
        try:
            sdata = json.loads(sibling.read_text(encoding="utf-8"))
            if isinstance(sdata, dict) and sdata.get("disableAllHooks") is True:
                return False
        except (OSError, ValueError):
            pass
    return True


def merge_activity_hooks(
    settings_path,
    event_states: Sequence[Tuple[str, str]],
    session_name: str,
    notification_event: Optional[str] = None,
    record_thread: bool = True,
    tool_hook_events: Optional[dict] = None,
    resist_disable_all: bool = False,
    prompt_context: Optional[bool] = None,
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

    ``resist_disable_all`` writes ``"disableAllHooks": false`` — only for a CLI
    whose hard guard reads this file (Claude: a local ``false`` beats a
    project-level ``true``). It is NOT keyed on ``tool_hook_events``: Codex
    carries tool hooks too, but its ``hooks.json`` schema rejects the key and
    drops EVERY hook in the file; a ``false`` an earlier build left there is
    removed so such a file heals on the next install.

    Merge, never clobber: user-authored keys and hook entries are preserved;
    only prior MindFlock entries (recognised by :data:`_HOOK_TAG`) are replaced,
    so re-installing with a new session name is idempotent. A file that EXISTS
    but does not parse (a hand edit with a trailing comma, a writer caught
    mid-write) or is not a JSON object is left untouched and False returned —
    rewriting it as ``{}`` + our hooks would erase the user's permissions/env/
    own hooks, and the red-zone monitor re-installs every few seconds. Returns
    True when the file was written. Raises nothing on the happy path but callers
    still wrap it — a launch must never break over hook install.
    """
    import json
    from pathlib import Path

    settings_path = Path(settings_path)
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        raw = settings_path.read_text(encoding="utf-8")
    except FileNotFoundError:
        raw = ""
    except (OSError, ValueError):
        return False  # unreadable (permissions, bad encoding): hands off
    if raw.strip():
        try:
            data = json.loads(raw)
        except ValueError:
            return False
        if not isinstance(data, dict):
            return False
    else:
        data = {}
    hooks = data.get("hooks")
    if not isinstance(hooks, dict):
        hooks = {}
        data["hooks"] = hooks
    th_map = dict(tool_hook_events or ())
    for event, state in event_states:
        entries = hooks.get(event)
        if not isinstance(entries, list):
            entries = []
        entries = [e for e in entries if not is_mindflock_hook_entry(e)]
        if notification_event is not None and event == notification_event:
            cmd = notification_hook_command()
        else:
            cmd = hook_command(
                state,
                record_thread=record_thread,
                tool_hook=th_map.get(event),
                prompt_context=prompt_context,
            )
        entries.append({"hooks": [{"type": "command", "command": cmd}]})
        hooks[event] = entries
    # Resist an accidental or malicious project-level `disableAllHooks: true`
    # (settings.json) with an explicit `false` here — verified to win. Only for
    # a hard-guard install (Claude); a tool-hook install without one (Codex)
    # must not carry the key at all — Codex's hooks.json schema rejects it and
    # loads NO hooks — so a `false` an earlier build wrote there is dropped.
    if resist_disable_all:
        data["disableAllHooks"] = False
    elif tool_hook_events is not None and data.get("disableAllHooks") is False:
        data.pop("disableAllHooks")
    # Skip the write when nothing changed, so a hot-reloading CLI's watcher never
    # sees a needless rewrite (and the tick can re-pin every 4s for free). The
    # command is session-agnostic now, so a re-pin with a new name is a no-op.
    body = json.dumps(data, indent=2) + "\n"
    try:
        if settings_path.exists() and settings_path.read_text(encoding="utf-8") == body:
            return False
    except OSError:
        pass
    _atomic_write_json(settings_path, data)
    return True
