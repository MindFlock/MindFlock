"""Agent-state inference — activity / stage / last-turn probes for a session.

Lifted verbatim from ``server.py``: the layered ``_agent_activity`` classifier
(exit marker / provider hook marker / process tree / CPU rate / pane-hash
hysteresis), the workflow-stage inference (``_session_stage``), the failed
pre-commit-step parser, and the last-turn snippet — plus their module-level
caches and tuning constants. The short-TTL ``_probe_cached`` memo that wraps
these probes stays in ``server.py`` next to its other per-session caches.

Cross-module seams (``_run_capped``, ``_pane_meta`` and friends, and the
git/PR helpers ``_session_stage`` leans on) are resolved through the *server
module's* attribute bindings at call time via :func:`_server`. That is
deliberate: tests (and the server itself) monkeypatch those seams as
``server._pane_meta`` etc., and rebinding the server attribute must keep
steering this code exactly as it did before the split.
"""

from __future__ import annotations

import os
import re
import subprocess
import time
from typing import Dict, Optional

from backend import providers
from backend import session
from backend.providers.usage_limits import is_limit_screen
from backend.session import tmux
from backend.session.storage import Loading
from backend.web.core.terminal import _exit_marker_path


def _server():
    """The ``backend.web.server`` module, imported lazily (it imports this
    module at startup, so a top-level import would be circular)."""
    from backend.web import server

    return server


def _session_last_turn(inst) -> Optional[str]:
    """L3: one-line snippet of the session's latest conversational turn (the
    provider reads its own transcript; Claude parses the newest JSONL entry,
    mtime-guard-cached ~10s provider-side). None when unavailable."""
    try:
        if not inst.Started():
            return None
        wt = inst.GetWorktreePath()
        if not wt:
            return None
        name = tmux.to_mindflock_tmux_name(inst.Title)
        return providers.resolve(getattr(inst, "Program", "") or "").last_turn_snippet(
            name, wt
        )
    except Exception:  # noqa: BLE001
        return None


# Layered activity detection (roadmap A1). Per-title rolling record of the
# agent pane: {hash, changed_epoch, state, streak}. Authoritative signals (exit
# marker, foreground process, provider hook marker) are consulted first; the
# pane-hash fallback is hardened with noise stripping + asymmetric hysteresis
# so a single changed frame can't flip the badge every poll.
_ACTIVITY_CACHE: Dict[str, dict] = {}
_ACTIVITY_IDLE_AFTER = (
    8.0  # static-pane seconds before working -> idle (2x the UI's 4s poll)
)
_ACTIVITY_CONFIRM_POLLS = 2  # consecutive changed polls before idle -> working
_ACTIVITY_NOISE_LINES = (
    2  # bottom pane lines ignored by the hash (cursor/spinner churn)
)
_MARKER_TRUST_WINDOW_S = 45.0  # a working/clarify hook marker older than this is re-verified against the live pane
# Per-window resume-thread binding (provider.record_thread) runs at most this
# often per session — discovery stats on-disk stores and the id changes rarely.
_THREAD_RECORD_EVERY_S = 20.0
_THREAD_RECORD_AT: dict = {}

# Last live branch observed per session title, so the stage pill can follow a
# branch switch inside one workspace (in-place sessions especially): on drift,
# the branch-scoped flow artifacts (commit-status marker, PR / origin-SHA /
# base-branch / diff caches) are cleared so the pill reflects the branch the
# user is actually on, not a previous task's leftovers. Dict ops are
# GIL-atomic and every drift step is idempotent, so no lock is needed.
_LAST_BRANCH: Dict[str, str] = {}

# Foreground commands that mean "no agent is running in this pane" — the CLI
# exited (or its launcher dropped to a shell), so the pane can't be 'working'.
_BARE_SHELLS = frozenset({"bash", "zsh", "sh", "dash", "fish", "ksh", "tcsh", "csh"})


def _pane_meta(name: str):
    """``(pane_current_command, session_created_epoch, pane_pid, pane_size)`` for
    tmux session ``name`` (one display-message call), or ``(None, None, None,
    None)`` on any failure. ``pane_size`` is ``"<width>x<height>"`` (e.g.
    ``"120x30"``) — used to tell a resize-driven repaint apart from real agent
    output in the activity classifier."""
    cp = _server()._run_capped(
        [
            "tmux",
            "display-message",
            "-p",
            "-t",
            name,
            "#{pane_current_command}\t#{session_created}\t#{pane_pid}\t#{pane_width}x#{pane_height}",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        timeout=10,
    )
    if cp.returncode != 0:
        return None, None, None, None
    parts = cp.stdout.decode("utf-8", "replace").strip().split("\t")
    fg = (parts[0].strip() or None) if parts else None
    created = None
    if len(parts) > 1:
        try:
            created = float(parts[1])
        except ValueError:
            created = None
    pane_pid = parts[2].strip() if len(parts) > 2 and parts[2].strip() else None
    pane_size = parts[3].strip() if len(parts) > 3 and parts[3].strip() else None
    return fg, created, pane_pid, pane_size


# Process-tree probe (F1): provisioned sessions run through a bash launcher
# script, so the pane's foreground command reads as ``bash`` with ``claude``
# running as a CHILD — a bare-shell foreground alone must not mean "agent not
# running".
# One ``ps`` snapshot per lookup, cached ~one poll so several sessions (and
# the /api/events tick running alongside the HTTP poll) share the cost.
_PID_TREE_CACHE: Dict[str, tuple] = {}
_PID_TREE_TTL = 3.0


def _pane_has_agent_process(pane_pid) -> bool:
    """True when the pane's process tree holds a live NON-shell descendant.

    Walks children of ``pane_pid`` from a single ``ps -e -o pid=,ppid=,comm=``
    snapshot; only when every descendant is itself a bare shell (or there are
    none) is the agent really gone. Unknown pid / ps failure -> False (the
    conservative pre-F1 behaviour: bare shell reads idle)."""
    if not pane_pid:
        return False
    key = str(pane_pid)
    now = time.time()
    cached = _PID_TREE_CACHE.get(key)
    if cached and cached[0] > now:
        return cached[1]
    result = False
    try:
        cp = _server()._run_capped(
            ["ps", "-e", "-o", "pid=,ppid=,comm="],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=10,
        )
        if cp.returncode == 0:
            children: Dict[str, list] = {}
            comms: Dict[str, str] = {}
            for line in cp.stdout.decode("utf-8", "replace").splitlines():
                fields = line.split(None, 2)
                if len(fields) < 3:
                    continue
                pid, ppid, comm = fields[0], fields[1], fields[2].strip()
                children.setdefault(ppid, []).append(pid)
                comms[pid] = comm
            stack = list(children.get(key, []))
            seen = set()
            while stack:
                pid = stack.pop()
                if pid in seen:
                    continue
                seen.add(pid)
                comm = os.path.basename(comms.get(pid, "")).lower()
                if comm and comm not in _BARE_SHELLS:
                    result = True
                    break
                stack.extend(children.get(pid, []))
    except OSError:
        result = False
    _PID_TREE_CACHE[key] = (now + _PID_TREE_TTL, result)
    return result


# CPU-based activity (robust idle detection): an agent parked at its prompt is
# blocked reading stdin and burns ~0 CPU, no matter how its idle screen redraws
# (spinner / status line / cursor). A working/streaming agent burns CPU. We
# sample the pane's process-tree CPU jiffies per poll and compare the rate — far
# more reliable than screen-scraping. Linux-only (/proc); other OSes fall back
# to the pane-hash heuristic.
_PROC_SNAPSHOT_CACHE: dict = {"at": 0.0, "children": None, "jiffies": None}
# Must stay BELOW the server's 2.5s probe memo: a session's consecutive
# activity computations are ≥2.5s apart, and if both hit the same cached scan
# the CPU delta reads 0 (phantom idle). 2.0s halves the full-/proc scan rate
# vs the old 1.0s without that aliasing.
_PROC_SNAPSHOT_TTL = 2.0
# Pane-tree CPU rate (jiffies/s ≈ % of one core) above which the fallback calls
# the agent "working". Measured baseline: an IDLE Claude Code TUI still burns
# ~8 jiffies/s just redrawing its prompt, so the bar sits well above that. A
# genuinely working session is normally caught first by its fresh hook marker
# (Layer 1); this only decides when that marker is stale/absent, where erring
# toward idle matches the user's expectation (no phantom "running").
_CPU_ACTIVE_JIFFIES_PER_S = 25.0


def _proc_cpu_snapshot():
    """``(children_by_ppid, jiffies_by_pid)`` from one ``/proc`` scan, cached ~1s
    so all sessions in a poll share it. ``jiffies`` = utime+stime per pid.
    ``({}, {})`` on non-Linux / failure."""
    now = time.time()
    c = _PROC_SNAPSHOT_CACHE
    if c["children"] is not None and now - c["at"] < _PROC_SNAPSHOT_TTL:
        return c["children"], c["jiffies"]
    children: Dict[str, list] = {}
    jiffies: Dict[str, int] = {}
    try:
        entries = os.listdir("/proc")
    except OSError:
        return {}, {}
    for entry in entries:
        if not entry.isdigit():
            continue
        try:
            with open("/proc/%s/stat" % entry, "r") as f:
                data = f.read()
        except OSError:
            continue
        # comm (in parens) can contain spaces/parens; everything after the last
        # ')' is the fixed-order field list: state ppid ... utime(14) stime(15).
        _, _, tail = data.rpartition(")")
        parts = tail.split()
        if len(parts) < 13:
            continue
        ppid = parts[1]
        try:
            jiffies[entry] = int(parts[11]) + int(parts[12])
        except ValueError:
            jiffies[entry] = 0
        children.setdefault(ppid, []).append(entry)
    c.update(at=now, children=children, jiffies=jiffies)
    return children, jiffies


def _pane_cpu_jiffies(pane_pid):
    """Total CPU jiffies of the pane's process subtree, or ``None`` when
    unavailable (non-Linux / no /proc)."""
    if not pane_pid:
        return None
    children, jiffies = _proc_cpu_snapshot()
    if not jiffies:
        return None
    key = str(pane_pid)
    total = jiffies.get(key, 0)
    stack = list(children.get(key, []))
    seen: set = set()
    while stack:
        pid = stack.pop()
        if pid in seen:
            continue
        seen.add(pid)
        total += jiffies.get(pid, 0)
        stack.extend(children.get(pid, []))
    return total


# Trust-gate auto-answer (F2): pre-trust in the Claude provider normally keeps
# the gate from ever appearing; this is the safety net for launches that missed
# it (adopted sessions, other providers, config resets). Rate-limited per title
# so a slow redraw isn't spammed with keystrokes.
_TRUST_DISMISS_AT: Dict[str, float] = {}
_TRUST_DISMISS_COOLDOWN = 5.0
# The trust gate only appears at session startup. For this long after a
# session is created we check for it on every poll BEFORE trusting any hook
# marker — otherwise a fresh clarify/idle marker (Claude can fire a
# Notification hook while the gate is up) short-circuits Layer 1 and the gate
# is reported as "needs input" but never auto-answered, so a seeded prompt
# sits behind it until a human presses Enter (the PR-ingestion symptom).
_TRUST_GATE_STARTUP_WINDOW_S = 300.0


def _dismiss_trust_prompt(inst, title: str, name: str, text: str) -> bool:
    """Auto-answer the provider's trust/MCP gate when it is on screen.

    This wires the previously dead trust handling (Instance.
    CheckAndHandleTrustPrompt / TrustSpec) into the web poll path: whenever the
    captured agent pane matches the provider's trust patterns, the provider's
    dismiss keystroke is sent straight to the tmux session (the web server has
    no attached PTY, so ``send-keys`` is the reliable channel). Returns True
    when a trust screen was seen (the caller reports 'clarify' so the moment
    is visible either way). Never raises."""
    try:
        spec = providers.resolve(getattr(inst, "Program", "") or "").trust_prompt()
        if not spec or not any(p in text for p in spec.patterns):
            return False
        now = time.time()
        if now - _TRUST_DISMISS_AT.get(title, 0.0) >= _TRUST_DISMISS_COOLDOWN:
            _TRUST_DISMISS_AT[title] = now
            subprocess.run(
                [
                    "tmux",
                    "send-keys",
                    "-t",
                    name,
                    "-l",
                    spec.keystroke.decode("utf-8", "replace"),
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=10,
            )
        return True
    except Exception:  # noqa: BLE001 — trust handling is best-effort
        return False


def _agent_exited(name: str, session_created) -> bool:
    """True when the exit-recording launch wrapper says the agent command in
    tmux session ``name`` has ENDED (any recorded code — natural or not).

    Guarded by the marker's mtime vs the session's creation time: a leftover
    marker from a previous same-named session (which the engine's first launch
    doesn't clear) must not make a freshly started agent read as ended."""
    if _server()._read_exit_marker(name) is None:
        return False
    try:
        mtime = _exit_marker_path(name).stat().st_mtime
    except OSError:
        return False
    if session_created is not None and mtime < float(session_created):
        return False  # stale marker from a prior incarnation of this name
    return True


def _normalized_pane_hash(text: str) -> str:
    """Hash of the pane text with per-frame churn stripped.

    Per-line trailing whitespace and trailing blank lines are dropped, plus the
    bottom ``_ACTIVITY_NOISE_LINES`` lines, where Claude's cursor / spinner /
    token counter redraw every frame even while nothing is really happening —
    otherwise that churn alone reads as 'working' on every poll."""
    import hashlib

    lines = [ln.rstrip() for ln in text.splitlines()]
    while lines and not lines[-1]:
        lines.pop()
    if _ACTIVITY_NOISE_LINES:
        lines = lines[:-_ACTIVITY_NOISE_LINES]
    return hashlib.md5("\n".join(lines).encode("utf-8")).hexdigest()


def _parse_progress_tokens(text: str, provider) -> Optional[int]:
    """The CLI's climbing turn-token counter parsed to an int, or None.

    Reads the provider's ``progress_token_pattern`` (one capture group) from the
    raw pane and normalizes a ``k``/``m`` suffix (``"15.3k"`` -> ``15300``). The
    caller treats an INCREASE across polls as proof of work — positive even when
    local CPU is ~0 (extended thinking runs server-side). Never raises; any
    provider without the pattern, or an unparseable capture, yields None."""
    try:
        pat = provider.progress_token_pattern()
        if not pat:
            return None
        m = re.search(pat, text)
        if not m:
            return None
        raw = m.group(1).strip().replace(",", "").replace(" ", "")
        mult = 1
        if raw and raw[-1] in "kKmM":
            mult = 1000 if raw[-1] in "kK" else 1_000_000
            raw = raw[:-1]
        return int(float(raw) * mult)
    except Exception:  # noqa: BLE001 — a triage signal, never break the poll
        return None


def _startup_trust_gate_clarify(srv, inst, title: str, name: str, created) -> bool:
    """F2 safety net (startup window only): auto-answer a trust/MCP gate.

    A trust/MCP gate on a freshly-created session is auto-answered BEFORE any
    hook-marker short-circuit in :func:`_agent_activity`. pre-trust normally
    prevents the gate, but the shared ``~/.claude.json`` can be clobbered by a
    concurrent Claude, and a gate masked by a fresh clarify marker would
    otherwise never be dismissed — leaving a seeded (PR/ticket) prompt parked
    behind it. Bounded to :data:`_TRUST_GATE_STARTUP_WINDOW_S` so steady-state
    sessions never pay the extra capture; the dismiss itself is rate-limited
    per title. Returns True when a gate was seen (the caller reports
    'clarify'). Never raises.
    """
    try:
        if (
            created is not None
            and (time.time() - float(created)) <= _TRUST_GATE_STARTUP_WINDOW_S
        ):
            cp0 = subprocess.run(
                ["tmux", "capture-pane", "-p", "-t", name],
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                timeout=10,
            )
            if cp0.returncode == 0 and srv._dismiss_trust_prompt(
                inst, title, name, cp0.stdout.decode("utf-8", "replace")
            ):
                return True
    except Exception:  # noqa: BLE001 — safety net must never break the poll
        pass
    return False


def _maybe_record_thread(provider, inst, name: str, created) -> None:
    """Bind this window to its own conversation id while it runs (throttled).

    Feeds the per-window ``resume <id>`` after a crash — several windows on one
    directory must not all resume the newest thread. Claude records via its
    hooks; codex/antigravity discover from their on-disk stores here. Throttled
    to at most once per :data:`_THREAD_RECORD_EVERY_S` per session (discovery
    stats files, and the id changes rarely). Enrichment only; never raises.
    """
    try:
        now0 = time.time()
        if now0 - _THREAD_RECORD_AT.get(name, 0.0) >= _THREAD_RECORD_EVERY_S:
            _THREAD_RECORD_AT[name] = now0
            provider.record_thread(name, inst.GetWorktreePath() or "", created)
    except Exception:  # noqa: BLE001 — thread binding is enrichment only
        pass


def _limit_on_pane(srv, name: str, provider) -> bool:
    """True when the agent pane currently shows a usage-limit banner/menu.

    A standalone capture used to see *past* a fresh clarify marker: a CLI that
    hits its usage limit often prints a limit notice with an interactive
    "❯ 1. …" menu and fires a Notification/PermissionRequest hook — which would
    otherwise read as an ordinary 'clarify' (a human gate the queue never
    overrides). Reporting it as its own 'limit' state instead lets the drain
    hold + auto-resume (escape the menu, then send). The common pane-inspection
    path reuses its own capture (see :func:`_pane_hash_activity`); this helper
    only covers the marker short-circuit. Never raises."""
    try:
        cp = srv._run_capped(
            ["tmux", "capture-pane", "-p", "-t", name],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=10,
        )
        if cp.returncode != 0:
            return False
        text = cp.stdout.decode("utf-8", "replace")
        # High-precision screen match (not the looser drain gate): a clarify
        # marker means the agent is parked, so there is no live turn to misread,
        # but the pane can still carry stray "rate limited" text we must ignore.
        return is_limit_screen(text)
    except Exception:  # noqa: BLE001 — detection must never break the poll
        return False


def _pane_hash_activity(
    srv, inst, title: str, name: str, provider, pane_pid, pane_size
) -> str:
    """Layers 3-4 of :func:`_agent_activity`: classify from the live pane.

    Reached only once the authoritative signals (exit marker, hook marker,
    process tree) are inconclusive. Captures the pane and decides:

    * First sighting of ``title`` -> assume 'working' (a fresh agent usually
      is) and seed the rolling record; it settles once CPU/pane go quiet.
    * A pane RESIZE (a web tab attaching reflows tmux) rebaselines the hash /
      CPU / token counter and keeps the PRIOR state — clicking a tab must not
      read as activity.
    * Primary signal: pane process-tree CPU rate (jiffies/s). At or above
      :data:`_CPU_ACTIVE_JIFFIES_PER_S` -> 'working'. Below it, a visible
      waiting-prompt on a STABLE pane -> 'clarify'; otherwise a live status
      line (interrupt hint / climbing token counter, Layer 4) -> 'working'
      even at ~0 CPU (extended thinking, work runs server-side); else keep the
      prior state until it has been quiet for :data:`_ACTIVITY_IDLE_AFTER`
      seconds, then 'idle'.
    * Fallback where /proc CPU is unavailable (e.g. macOS): pane-hash
      hysteresis — a stable waiting-prompt -> 'clarify'; a live status line ->
      'working'; a pane changing on :data:`_ACTIVITY_CONFIRM_POLLS` consecutive
      polls -> 'working'; a pane static for :data:`_ACTIVITY_IDLE_AFTER`
      seconds -> 'idle'.

    A visible trust/MCP gate seen here is auto-answered (F2) and reported
    'clarify'. Returns 'offline' when the pane capture fails.
    """
    # Layer 3: hardened pane-hash fallback.
    cp = srv._run_capped(
        ["tmux", "capture-pane", "-p", "-t", name],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        timeout=10,
    )
    if cp.returncode != 0:
        return "offline"
    text = cp.stdout.decode("utf-8", "replace")
    # F2 safety net: a visible trust/MCP gate is auto-answered and shown
    # as clarify (pre-trust normally prevents it from appearing at all).
    if srv._dismiss_trust_prompt(inst, title, name, text):
        return "clarify"
    h = _normalized_pane_hash(text)
    now = time.time()
    cpu_now = srv._pane_cpu_jiffies(pane_pid)
    rec = _ACTIVITY_CACHE.get(title)
    if rec is None:
        # First sighting: assume working (a fresh agent usually is); it
        # settles to idle once CPU (or the pane) stays quiet past the
        # threshold.
        _ACTIVITY_CACHE[title] = {
            "hash": h,
            "changed": now,
            "state": "working",
            "streak": 0,
            "size": pane_size,
            "cpu": cpu_now,
            "cpu_at": now,
            "busy_at": now,
            # Baseline the turn-token counter now so the NEXT poll can already
            # tell a climb (work) from a flat counter (idle).
            "tokens": _parse_progress_tokens(text, provider),
        }
        return "working"
    # A pane RESIZE (opening/focusing the session in the web UI attaches a
    # terminal, which resizes tmux and reflows the pane) changes the captured
    # text without the agent doing anything. Rebaseline hash + CPU and DON'T
    # count it as activity, so clicking a tab can't flip idle -> working.
    if pane_size is not None and rec.get("size") not in (None, pane_size):
        rec["size"] = pane_size
        rec["hash"] = h
        rec["streak"] = 0
        rec["cpu"] = cpu_now
        rec["cpu_at"] = now
        # Rebaseline the token counter too: a reflow can move/redraw it, and a
        # stale prior value must not read as a climb on the next poll.
        rec["tokens"] = _parse_progress_tokens(text, provider)
        return rec.get("state", "idle")
    rec["size"] = pane_size if pane_size is not None else rec.get("size")

    # Primary signal: CPU rate of the pane's process tree (jiffies/second)
    # since the last poll. Robust against idle-screen redraws — a spinner or
    # status line on a parked prompt burns no CPU.
    rate = None
    if cpu_now is not None and rec.get("cpu") is not None:
        dt = now - rec.get("cpu_at", now)
        if dt > 0:
            rate = (cpu_now - rec["cpu"]) / dt
    rec["cpu"] = cpu_now if cpu_now is not None else rec.get("cpu")
    rec["cpu_at"] = now

    changed = rec.get("hash") != h
    if changed:
        rec["hash"] = h

    # Layer 4 — status-line proof of a live turn, read from the RAW pane
    # (NOT the change-hash, which strips these very bottom lines as per-frame
    # noise). Either the provider's interrupt hint (``esc to interrupt``) or a
    # climbing token counter means the agent is working even at ~0 local CPU —
    # the fix for extended thinking, whose work runs server-side while the
    # local process blocks on a network read and looks idle to CPU alone.
    tok_now = _parse_progress_tokens(text, provider)
    tok_prev = rec.get("tokens")
    tok_climbing = tok_now is not None and tok_prev is not None and tok_now > tok_prev
    rec["tokens"] = tok_now if tok_now is not None else tok_prev
    wpats = provider.working_pane_patterns()
    status_working = tok_climbing or bool(
        wpats and any(re.search(p, text) for p in wpats)
    )

    if rate is not None:
        if rate >= _CPU_ACTIVE_JIFFIES_PER_S:
            # Real computation -> working (and remember when we last saw it).
            rec["busy_at"] = now
            rec["changed"] = now
            rec["streak"] = 0
            rec["state"] = "working"
            return "working"
        # CPU quiet. On a STABLE pane, a usage-limit SCREEN or a visible
        # waiting-prompt means the agent has parked. The limit banner is checked
        # first (it outranks the '❯ 1.' selection menu a limit screen also
        # shows) and reported as its own 'limit' state so the queue drain rides
        # the window out. Requiring a STABLE, CPU-quiet pane is what keeps a
        # genuinely working turn — or stray "rate limited" text scrolling by in
        # output — from ever being misread as a limit (the status-line proof
        # below still wins for a live turn on a changing pane).
        if not changed:
            if is_limit_screen(text):
                rec["state"] = "limit"
                return "limit"
            pats = provider.waiting_prompt_patterns()
            if pats and any(re.search(p, text) for p in pats):
                rec["state"] = "clarify"
                return "clarify"
        # CPU is quiet but the status line still shows a live turn (interrupt
        # hint / climbing token counter) -> working. This is the extended-
        # thinking case: server-side work, ~0 local CPU. Refresh busy_at so
        # the idle-settle window restarts from now.
        if status_working:
            rec["busy_at"] = now
            rec["changed"] = now
            rec["streak"] = 0
            rec["state"] = "working"
            return "working"
        # Otherwise keep the prior state through brief lulls (streaming
        # pauses / model round-trips), then settle to idle after the same
        # hysteresis window — regardless of cosmetic pane churn.
        busy_at = rec.get("busy_at", rec.get("changed", now))
        if now - busy_at >= _ACTIVITY_IDLE_AFTER:
            rec["streak"] = 0
            rec["state"] = "idle"
        return rec.get("state", "idle")

    # Fallback (no /proc CPU, e.g. macOS): pane-hash hysteresis. A usage-limit
    # SCREEN or a visible waiting-prompt on a STABLE pane still wins first
    # (matching the CPU branch: the limit banner outranks the selection menu,
    # and both outrank a stale interrupt hint).
    if not changed:
        if is_limit_screen(text):
            rec["streak"] = 0
            rec["state"] = "limit"
            return "limit"
        pats = provider.waiting_prompt_patterns()
        if pats and any(re.search(p, text) for p in pats):
            rec["streak"] = 0
            return "clarify"
    # The status line is the stronger positive signal where it exists — a
    # live interrupt hint / climbing token counter means working immediately,
    # even on a visually static pane (the extended-thinking case CPU can't
    # see either).
    if status_working:
        rec["changed"] = now
        rec["streak"] = 0
        rec["state"] = "working"
        return "working"
    if changed:
        rec["changed"] = now
        rec["streak"] = rec.get("streak", 0) + 1
        if rec.get("state") != "working" and rec["streak"] >= _ACTIVITY_CONFIRM_POLLS:
            rec["state"] = "working"
    else:
        rec["streak"] = 0
        if now - rec.get("changed", now) >= _ACTIVITY_IDLE_AFTER:
            rec["state"] = "idle"
    return rec.get("state", "idle")


def _agent_activity(inst, title: str) -> str:
    """'working' | 'clarify' | 'limit' | 'idle' | 'offline' for the agent's tmux
    session ('limit' = a usage-limit banner/menu is on screen).

    Layered, most-authoritative first:

    0. exit marker — the launch wrapper recorded the agent command ending
       inside the current tmux session -> idle (session gone -> offline);
    1. provider activity marker — the CLI's own hook-reported state (Claude
       Code Stop/UserPromptSubmit/Notification hooks, A2) -> returned as-is
       (above the shell heuristic on purpose: the CLI's own report outvotes
       what the pane's foreground command looks like);
    2. process tree — a bare shell holds the pane AND no non-shell descendant
       is alive -> the agent isn't running -> idle. A wrapper shell (the
       provisioned launcher script) with a live ``claude`` child falls through
       to pane inspection instead of being misread as idle (F1);
    3. pane-hash fallback with hysteresis — noise-stripped hash; a pane must
       change on ``_ACTIVITY_CONFIRM_POLLS`` consecutive polls to flip
       idle -> working, and must stay static ``_ACTIVITY_IDLE_AFTER`` seconds
       to fall back to idle (asymmetric on purpose: working is sticky).
       'clarify' (provider confirmation prompt on screen) is only reported on
       a STABLE pane, so a scrolling numbered list mid-generation can't match.
       A visible trust/MCP gate is auto-answered (F2) and reported 'clarify'.
    4. status-line proof — before CPU-quiet settles to idle, the provider's
       ``working_pane_patterns`` (Claude's ``esc to interrupt`` hint) and a
       climbing ``progress_token_pattern`` counter are read from the RAW pane.
       Either one means the turn is still live even at ~0 CPU — the fix for
       extended thinking (work runs server-side, so the local process blocks on
       a network read and looks exactly like an idle prompt to CPU alone).
    """
    try:
        srv = _server()
        if not inst.Started() or inst.Status == session.Paused:
            return "offline"
        name = tmux.to_mindflock_tmux_name(title)
        exists = (
            srv._run_capped(
                ["tmux", "has-session", "-t=" + name],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=10,
            ).returncode
            == 0
        )
        if not exists:
            _ACTIVITY_CACHE.pop(title, None)
            return "offline"
        fg, created, pane_pid, pane_size = srv._pane_meta(name)
        # Layer -1 (F2 safety net, startup only): auto-answer a trust/MCP gate
        # on a freshly-created session BEFORE any hook-marker short-circuit
        # below, so a seeded (PR/ticket) prompt can't sit parked behind it.
        if _startup_trust_gate_clarify(srv, inst, title, name, created):
            return "clarify"
        # Layer 0: the agent command ENDED (exit marker written this session).
        if srv._agent_exited(name, created):
            return "idle"
        # Layer 1: the CLI's own report (Claude Code hook marker).
        provider = providers.resolve(getattr(inst, "Program", "") or "")
        # Bind this window to its own conversation id while it runs (throttled)
        # so a per-window `resume <id>` after a crash targets the right thread.
        _maybe_record_thread(provider, inst, name, created)
        marked = provider.activity_state(name)
        if marked == "idle":
            # Stop hook fired -> the turn definitively ended; trust at any age.
            return "idle"
        if marked in ("working", "clarify"):
            # working/clarify markers only refresh on hook events (tool call,
            # prompt submit, notification). A long thinking/generating stretch
            # between tool calls, or an abandoned prompt, leaves the marker
            # stale — that is what makes an idle/working session read as the
            # previous state forever. Trust a FRESH marker (fast path, skips the
            # pane capture); once it goes stale, fall through to live pane
            # inspection, which reads the truth from the spinner/timer (working)
            # vs a bare idle prompt.
            age = provider.activity_state_age(name)
            if age is None or age <= _MARKER_TRUST_WINDOW_S:
                # A fresh clarify marker can mask a usage-limit menu: the CLI
                # prints a limit notice + selection box and fires a
                # Notification/PermissionRequest hook. Verify against the pane so
                # the limit claims its own 'limit' state — otherwise the drain
                # treats it as a human gate and the queue stalls behind a menu
                # that never clears on its own, even after the window reopens.
                if marked == "clarify" and _limit_on_pane(srv, name, provider):
                    return "limit"
                return marked
        # Layer 2: a bare shell holds the pane AND nothing but shells lives
        # under it -> the agent isn't running, whatever the pane looks like.
        if (
            fg
            and fg.lower() in _BARE_SHELLS
            and not srv._pane_has_agent_process(pane_pid)
        ):
            return "idle"
        # Layers 3-4: live-pane inspection (CPU rate + status-line proof under
        # asymmetric hysteresis, with a pane-hash fallback where /proc CPU is
        # unavailable). Kept in its own helper so the layered dispatch above
        # reads as a sequence of authoritative-first probes.
        return _pane_hash_activity(
            srv, inst, title, name, provider, pane_pid, pane_size
        )
    except Exception:  # noqa: BLE001
        return "offline"


# Lines of our own commit plumbing (the typed command echo and its marker
# files) and shell prompts — never useful as a "what failed" detail (K3).
_COMMIT_PLUMBING_RE = re.compile(r"\.mindflock_\w|precommit\.lock")
_SHELL_PROMPT_RE = re.compile(r"[$%#❯>]\s*$")
# Fallback-tail triage: a line that names a problem vs. one that names nothing.
# Pointer/path echoes ("→ ~/repo", a bare path) and letterless decoration are
# common last lines of hook output but useless as a badge.
_ERRORISH_RE = re.compile(
    r"error|fail|fatal|abort|denied|reject|blocked|forbidden|✗", re.IGNORECASE
)
_NOISE_LINE_RE = re.compile(r"^(?:[→➜↳›»▶]|->)\s|^(?:~|\.{1,2})?/\S*$")


def _parse_failed_step(text: str) -> Optional[str]:
    """Extract a failed-step detail from captured shell-pane ``text``.

    Primary: pre-commit-framework output — one line per hook ending in
    ``Passed`` / ``Failed``; returns the most recently failed hook's name.

    Fallback (K3): raw git hooks print no ``name....Failed`` lines, which used
    to leave the interrupt stage nameless. Anchor on the last commit-plumbing
    command line (the guided commit typed into the pane), then surface the last
    error-looking output line after it — or, failing that, the last line that
    isn't pointer/path noise — truncated to 80 chars. Prompts, our own marker
    plumbing, and lines that name nothing (e.g. "→ ~/repo") are never used;
    returning None (generic badge) beats a garbage detail.
    """
    lines = text.splitlines()
    failed = []
    for line in lines:
        # e.g. "ruff.....................................................Failed"
        m = re.match(r"^(.*?)\.{3,}.*\bFailed\s*$", line)
        if m:
            nm = m.group(1).strip()
            if nm:
                failed.append(nm)
    if failed:
        last = failed[-1]
        others = len([f for f in failed if f != last])
        return last if others == 0 else "%s (+%d)" % (last, others)
    # Generic fallback: last meaningful line the failing hook printed.
    start = 0
    for i, line in enumerate(lines):
        if _COMMIT_PLUMBING_RE.search(line):
            start = i + 1
    tail = []
    for line in lines[start:]:
        s = line.strip()
        if not s:
            continue
        if _COMMIT_PLUMBING_RE.search(s) or _SHELL_PROMPT_RE.search(s):
            continue
        tail.append(s)
    last = next((s for s in reversed(tail) if _ERRORISH_RE.search(s)), None)
    if last is None:
        last = next(
            (
                s
                for s in reversed(tail)
                if not _NOISE_LINE_RE.match(s) and re.search(r"[A-Za-z]", s)
            ),
            None,
        )
    if last is None:
        return None
    return last if len(last) <= 80 else last[:77] + "..."


def _failed_precommit_step(title: str) -> Optional[str]:
    """Best-effort detail of the commit hook that failed, from the shell pane.

    The guided commit runs in the session's shell tmux; the pane is captured
    and parsed by :func:`_parse_failed_step` (pre-commit framework hook names
    first, generic raw-hook output fallback). Returns None if nothing useful
    was found. Never raises.
    """
    try:
        name = _server()._shell_tmux_name(title)
        cp = subprocess.run(
            ["tmux", "capture-pane", "-p", "-S", "-400", "-t", name],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=10,
        )
        if cp.returncode != 0:
            return None
        return _parse_failed_step(cp.stdout.decode("utf-8", "replace"))
    except Exception:  # noqa: BLE001
        return None


def _handle_branch_drift(srv, inst, wt: str, live: str) -> None:
    """Reset branch-scoped flow state when the worktree's branch changes.

    A workspace used for several branches in series must not carry one
    branch's flow leftovers (a failed-commit status marker, cached PR /
    origin-SHA / base-branch / diff answers) onto the next — that is what
    made the stage pill stick on "interrupt"/"committed" from previous work.
    Never raises; every step is idempotent.
    """
    if not live:
        # Detached HEAD / non-repo / git error: can't judge drift — leave the
        # last observation alone so A -> detached -> A is not a false drift.
        return
    title = getattr(inst, "Title", "") or ""
    prev = _LAST_BRANCH.get(title)
    stored = getattr(inst, "Branch", "") or ""
    drifted = (prev is not None and live != prev) or (
        # First observation (fresh server): trust the stored branch as the
        # baseline so a pre-restart switch is still caught.
        prev is None
        and bool(stored)
        and live != stored
    )
    if drifted:
        # The last commit attempt's exit status belonged to the old branch's
        # flow; without this a failure there re-fires "interrupt" on any
        # dirty tree here. (The precommit lock and commit-msg files are
        # transient / reused deliberately — left alone.)
        try:
            os.unlink(os.path.join(wt, ".mindflock_commit_status"))
        except OSError:
            pass
        for b in {prev, live, stored}:
            if b:
                try:
                    srv._PR_CACHE.pop(b, None)
                    srv._ORIGIN_SHA_CACHE.pop((wt, b), None)
                except Exception:  # noqa: BLE001 — cache shape is server-owned
                    pass
        try:
            srv._BASE_BRANCH_CACHE.pop(wt, None)
            srv._DIFF_STAT_CACHE.pop(wt, None)
        except Exception:  # noqa: BLE001
            pass
        # In-place sessions live in the user's real repo where branches come
        # and go — keep the displayed branch (and the merge fallback) live.
        # Managed worktree/provisioned sessions keep their stored identity.
        if getattr(inst, "InPlace", False):
            inst.Branch = live
    _LAST_BRANCH[title] = live


# A commit runs its whole `touch lock; git add; git commit; …; rm lock` chain
# inside the SHELL tmux pane, so the lock is only removed if that chain runs to
# the end. Cancel it (Ctrl+C) or kill the process and the `rm` never fires — the
# lock lingers and the session wedges in the "precommit" stage forever, whose
# guided button is disabled. Treat the lock as stale (and clean it up) once the
# shell is back at its prompt with no non-shell descendant running: the commit is
# no longer in flight, so the button should light up again as a re-commit.
#
# BUT the liveness probe must not fire during the commit's own startup: the chain
# is sent into the shell a beat before `git` actually forks, so the first poll
# after Commit… (the endpoint drops the probe memo, forcing a fresh read) can
# catch the pane still at its bare prompt and read "not live" — which would DELETE
# the just-touched lock and drop the stage to "idle" for the whole commit. So a
# freshly-touched lock (younger than this grace) is always treated as live; only
# an older lock whose shell has gone quiet is the genuinely-abandoned case.
_PRECOMMIT_LOCK_GRACE_S = 15.0

# A live commit's liveness probe is not perfectly reliable: `_precommit_lock_is_live`
# reads False on ANY transient hiccup (a tmux/`ps` call timing out under load, a
# momentary bare-shell instant between hooks) — its `except -> False`. A long
# single `git commit` (e.g. a Claude-backed docs/tests hook running for minutes)
# only re-touches the lock at while-loop boundaries, so ONE unlucky not-live read
# used to be enough to delete the lock and strand the stage pill on "idle" for the
# rest of the commit. So a stale lock must be observed not-live *continuously* for
# this long before we treat it as genuinely abandoned (cancelled / killed) and
# self-heal it; transient blips keep the "pre-commit" pill up.
_PRECOMMIT_LOCK_STALE_HEAL_S = 25.0
# title -> epoch when this lock was FIRST seen stale-and-not-live (reset the moment
# it reads live again, or the lock is gone). Drives the debounce above.
_LOCK_STALE_SINCE: Dict[str, float] = {}


def _precommit_lock_is_live(title: str) -> bool:
    """True while a commit is actually running in ``title``'s shell pane.

    Missing shell session, an idle bare-shell prompt, or any tmux failure ->
    False (stale lock). Conservative on the "still running" side only when we can
    positively see a non-shell process (git / pre-commit / a hook) alive."""
    srv = _server()
    try:
        name = srv._live_session_name(srv._shell_tmux_name(title))
        if name is None:
            return False  # shell gone -> whatever it was doing is over
        fg, _created, pane_pid, _size = srv._pane_meta(name)
        # A non-shell foreground (git/python/node/…) is the commit chain running.
        if fg and fg.lower() not in _BARE_SHELLS:
            return True
        # Bare-shell foreground: live only if a non-shell descendant survives
        # (e.g. a hook backgrounded a helper). Otherwise we're at the prompt.
        return srv._pane_has_agent_process(pane_pid)
    except Exception:  # noqa: BLE001 — liveness is best-effort; err toward stale
        return False


def _commit_shell_live(inst, wt: str) -> bool:
    """True if the commit lock for ``wt`` is backed by a live commit in ANY
    session that shares it — not only ``inst``'s own shell.

    The lock lives in the worktree's git dir, so every session pointing at the
    SAME worktree path shares one lock (linked worktrees get distinct
    ``.git/worktrees/<name>`` dirs, so they do not). Without this, a sibling
    session sitting idle on the same worktree runs its own liveness probe against
    its quiet shell, reads "not live", and self-heals away the lock that ANOTHER
    session's commit is actively holding — dropping the committing session's pill
    to "idle". So we treat the lock as live when the polling session OR any
    same-worktree sibling shows a running commit. Own shell is checked first (the
    common single-session case stays a single probe); never raises."""
    srv = _server()
    self_title = getattr(inst, "Title", None)
    order = [self_title] if self_title else []
    try:
        for other in list(srv.ENGINE.instances.values()):
            if other is inst:
                continue
            try:
                if other.GetWorktreePath() != wt:
                    continue
                t = getattr(other, "Title", None)
            except Exception:  # noqa: BLE001 — one bad instance must not abort
                continue
            if t and t not in order:
                order.append(t)
    except Exception:  # noqa: BLE001 — engine snapshot is best-effort
        pass
    for t in order:
        try:
            # Via the server module (not the bare name) so it resolves through
            # the same reference the rest of the stage code — and the tests —
            # patch.
            if srv._precommit_lock_is_live(t):
                return True
        except Exception:  # noqa: BLE001
            continue
    return False


# Legacy location: the lock used to live at the WORKTREE ROOT, but an ignored
# scratch file there does not survive a long commit whose hooks run in-tree
# tooling (``git add -A`` / ``git clean -fdx`` / nested agents operating in the
# worktree) — it gets swept, ``_session_stage`` loses its only "commit running"
# signal, and the stage pill wrongly falls through to "idle". The lock now lives
# inside the worktree's PRIVATE git dir, which is never staged, never cleaned,
# and never traversed by worktree-local tooling.
_LEGACY_PRECOMMIT_LOCK = ".mindflock_precommit.lock"


def _precommit_lock_path(wt: str) -> str:
    """Absolute path to the commit lock, inside ``wt``'s private git dir so a
    running commit's own hooks (and any agent working in the tree) can't sweep
    it. Falls back to the legacy worktree-root path when the git dir can't be
    resolved (e.g. ``wt`` is not a repo)."""
    try:
        cp = _server()._run_capped(
            ["git", "-C", wt, "rev-parse", "--absolute-git-dir"],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=10,
        )
        if cp.returncode == 0:
            git_dir = cp.stdout.decode("utf-8", "replace").strip()
            if git_dir:
                return os.path.join(git_dir, "mindflock_precommit.lock")
    except Exception:  # noqa: BLE001 — resolution is best-effort
        pass
    return os.path.join(wt, _LEGACY_PRECOMMIT_LOCK)


def _clear_precommit_locks(wt: str) -> None:
    """Remove the pre-commit lock, best-effort. Clears BOTH the git-dir lock and
    any lingering legacy worktree-root lock left by an older build."""
    for path in (_precommit_lock_path(wt), os.path.join(wt, _LEGACY_PRECOMMIT_LOCK)):
        try:
            os.unlink(path)
        except OSError:
            pass


def _precommit_lock_active(inst, wt: str) -> bool:
    """True while ``wt``'s commit lock should hold the stage pill at 'precommit'.

    Encapsulates the lock's liveness + self-heal debounce so :func:`_session_stage`
    reads as a plain stage ladder:

    * No lock -> not active (and any prior stale timer is forgotten so a future
      commit starts clean).
    * A freshly-touched lock (younger than :data:`_PRECOMMIT_LOCK_GRACE_S`) or a
      genuinely live commit shell -> active; the debounce timer is cleared.
    * Stale AND not-live: never healed on a single reading (the liveness probe
      reads False on any transient tmux/ps hiccup). The 'precommit' pill is kept
      until the not-live condition has PERSISTED for
      :data:`_PRECOMMIT_LOCK_STALE_HEAL_S`; only then is the lock treated as
      abandoned (cancelled / killed), cleared, and reported inactive.
    """
    lock_path = _precommit_lock_path(wt)
    if not os.path.exists(lock_path):
        # No lock -> nothing to debounce; forget any prior stale timer so a
        # future commit starts clean.
        _LOCK_STALE_SINCE.pop(inst.Title, None)
        return False
    # Only stay in "precommit" while the commit is genuinely running in the
    # shell. If the user cancelled it (Ctrl+C) or killed the process, the
    # chain's own `rm -f lock` never fired and the lock lingers — self heal it
    # so the guided button lights up again instead of wedging on a disabled
    # "pre-commit" chip. (The status file was never written on a cancel, so the
    # caller falls through to the git-based agent/committed check.)
    #
    # A freshly-touched lock is always live (grace window): the commit chain
    # forks `git` a beat after the lock appears, so the liveness probe would
    # otherwise race the startup and delete the lock, dropping the whole commit
    # to "idle" instead of "pre-commit". Only an older lock over a quiet shell
    # is the abandoned case worth self-healing.
    try:
        lock_age = time.time() - os.path.getmtime(lock_path)
    except OSError:
        lock_age = _PRECOMMIT_LOCK_GRACE_S + 1.0  # can't stat -> not fresh
    if lock_age < _PRECOMMIT_LOCK_GRACE_S or _commit_shell_live(inst, wt):
        # Fresh or genuinely live -> commit is running. Clear the debounce
        # timer so a later blip starts counting from scratch.
        _LOCK_STALE_SINCE.pop(inst.Title, None)
        return True
    # Stale AND not-live on THIS read. Do not delete the lock on a single
    # not-live reading — the probe fails False on any transient tmux/ps hiccup
    # or momentary bare-shell instant between hooks, and a long commit would
    # then fall to "idle" for the rest of its run. Keep the "pre-commit" pill up
    # until the not-live condition has PERSISTED past the debounce window; only
    # then is the lock genuinely abandoned (cancelled / killed) and safe to
    # self-heal.
    now = time.time()
    since = _LOCK_STALE_SINCE.get(inst.Title)
    if since is None:
        _LOCK_STALE_SINCE[inst.Title] = now
        return True
    if now - since < _PRECOMMIT_LOCK_STALE_HEAL_S:
        return True
    _LOCK_STALE_SINCE.pop(inst.Title, None)
    _clear_precommit_locks(wt)
    return False


def _session_stage(inst) -> dict:
    """Best-effort workflow stage for the left-rail badge + guided buttons.

    provisioning -> agent -> precommit -> committed -> pushed -> pr.
    pushed/pr are exact (git upstream / gh); precommit is the UI commit lock;
    agent/committed are inferred from git. Never raises.
    """
    res = {"stage": "agent", "pr_url": None}
    try:
        srv = _server()
        if not inst.Started():
            res["stage"] = "provisioning" if inst.Status == Loading else "agent"
            return res
        wt = inst.GetWorktreePath()
        if not wt or not os.path.isdir(wt):
            return res
        # Branch drift first: the interrupt path below returns early, so a
        # stale status marker must be cleared before it is consulted.
        live_branch = srv._current_branch(wt)
        _handle_branch_drift(srv, inst, wt, live_branch)
        # Commit lock (with liveness + self-heal debounce) -> hold at precommit.
        if _precommit_lock_active(inst, wt):
            res["stage"] = "precommit"
            return res
        # Last commit attempt was blocked by pre-commit and changes still remain
        # uncommitted -> show an interrupt stage (not "agent") so the user knows
        # to re-commit (which re-stages the auto-fixes with the same message).
        status_file = os.path.join(wt, ".mindflock_commit_status")
        if os.path.isfile(status_file):
            try:
                with open(status_file) as f:
                    last_status = f.read().strip()
            except OSError:
                last_status = "0"
            if last_status not in ("", "0"):
                if srv._is_dirty(wt):
                    res["stage"] = "interrupt"
                    res["failed_step"] = srv._failed_precommit_step(inst.Title)
                    return res
                # Clean tree: the failure is history (the user committed or
                # stashed past it) — self-heal so it can't re-fire later.
                try:
                    os.unlink(status_file)
                except OSError:
                    pass
        base = srv._session_base_branch(inst)
        # Key push/PR detection off the branch the worktree is actually on, so
        # the stage chip and Make PR target the same (current) branch.
        branch = live_branch or inst.Branch or ""
        beyond = srv._commits_beyond_base(wt, base)
        # Only ask gh once there are commits (bounded + cached). PR detection is
        # by head branch alone (not base): make-pr can target any base — a
        # configured default like `staging`, or one picked in the dialog the
        # server never learns — so filtering the lookup by the fork-point would
        # miss the real PR and wedge the chip on "pushed". `beyond` still keys
        # off the fork-point ("is there work to push"), not where the PR goes.
        info = srv._pr_info(wt, branch) if beyond > 0 else None
        # Surface the PR's URL so the chip can link to it whatever its state
        # (open / merged / closed) — a PR-review session has one from the start.
        if info and info.get("url"):
            res["pr_url"] = info.get("url")

        # Only a *currently open* PR changes the workflow stage. Merged/closed PR
        # history never blocks Make PR: once new commits are pushed beyond the
        # base, "pushed" (Make PR) is offered again. "pushed" is confirmed against
        # origin — not the local @{u} tracking ref, which can be set before the
        # branch is really on the remote. So:
        #   dirty tree                         -> agent      (next: Commit)
        #   commits not confirmed on origin    -> committed  (next: Push)
        #   confirmed on origin + PR open      -> pr         (next: Merge / View PR)
        #   confirmed on origin + no open PR   -> pushed     (next: Make PR)
        if srv._is_dirty(wt) or beyond <= 0:
            res["stage"] = "agent"
            return res
        local_head = srv._git_head_sha(wt)
        remote_sha = srv._origin_branch_sha(wt, branch)
        confirmed_pushed = (
            bool(remote_sha) and bool(local_head) and remote_sha == local_head
        )
        if not confirmed_pushed:
            res["stage"] = "committed"
        elif info and info.get("state") == "OPEN":
            res["stage"] = "pr"
        else:
            res["stage"] = "pushed"
        return res
    except Exception:  # noqa: BLE001
        return res
