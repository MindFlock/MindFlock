"""Per-session memory: who is eating the machine, now and over time.

The 2026-11-05 slowdown (WSL2, 19.5 GB, into swap until the PC had to be
rebooted) had ~10 agent sessions, another app's Python and the Voice Lab TTS
all resident, and nothing said WHICH of them held the memory. This module
measures it:

* **The sampler** — :func:`tick`, every :data:`TICK_SECONDS` on the server's
  background-tick pool (never the event loop). ONE scan of ``/proc`` per tick
  builds the whole process table (pid → ppid, start time, comm, RSS: one
  ``stat`` read each); every session's tree is attributed from that table,
  never a scan per session. Session roots are the tmux pane pids, all
  sessions in ONE ``tmux list-panes -a`` call.

* **Attribution** — a session's tree is its panes' processes and every
  descendant. ``agent_mb`` is the agent CLI (the first non-shell process under
  the pane, through launcher shells and ``uv``/``npx`` wrappers) plus its
  children that are the same CLI (codex's node wrapper → its native binary);
  ``mcp_mb`` is every MCP stdio server the agent started (a direct child of
  the CLI whose argv names ``backend.mcp`` or ``mcp``, or matches a server
  configured in ``~/.claude.json`` / ``~/.codex/config.toml``) with its whole
  subtree (``npm exec`` → node); ``other_mb`` is the rest (launcher shells,
  the Bash tool's shells, test runs, dev servers).

* **What the launch gate reads** — :func:`session_agent_mb` /
  :func:`session_agent_peak_mb`: ``agent_mb + mcp_mb`` per session and its
  peak this incarnation, the cost of one more session's AGENT. ``other_mb``
  is left out on purpose (2026-11-12): a test run in one pane moved the
  gate's per-session cost 637 → 1,780 MB in a minute and held back sessions
  the machine had room for. What it really takes still shows, once, in the
  machine's available memory and pressure readings.

* **PSS, on a budget** — memory is PSS (``smaps_rollup``: shared pages split
  between their users) where the kernel reports it, else RSS. Reading
  ``smaps_rollup`` makes the kernel walk the process's page tables, ~1 ms of
  CPU per 100 MB resident (measured on this WSL2 box) — 20 sessions of
  ~700 MB would be ~140 ms a tick. So each tick re-reads at most
  :data:`PSS_BUDGET_MB` of RSS worth of processes (never-read first, then the
  stalest) and every other process is its current RSS scaled by its last
  PSS/RSS ratio. A process is re-read every few ticks; a new one on its first.

* **History** — every :data:`HISTORY_SECONDS` one JSON line goes to
  ``<config dir>/logs/memory.jsonl``: ``{"at", "system", "sessions": {title:
  mb}, "others": [...]}`` — rotated at :data:`ROTATE_BYTES` keeping one old
  file (``memory.jsonl.1``). The next slowdown is diagnosed by reading it.
  Writes happen on the tick's thread; a failure is logged once and ignored.

* **Never slow the server under pressure** — a tick that takes longer than
  :data:`SLOW_TICK_SECONDS` (the box is thrashing: every ``/proc`` read
  faults) makes the loop skip the next one, logged once per episode.

* **Elsewhere** — no ``/proc`` (macOS): ``psutil`` when importable, else one
  ``ps -axo pid=,ppid=,rss=,command=`` per tick; RSS only.

Readers: :func:`row` (the session row's ``memory`` field — a dict lookup,
no I/O), :func:`snapshot` (``GET /api/memory``), :func:`history`
(``GET /api/memory/history``). Off under pytest (``ENGINE`` is the
developer's real state there) and with ``MINDFLOCK_MEMORY_SAMPLER=0``.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import shlex
import subprocess
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Dict, Iterable, List, Optional, Sequence, Tuple

from backend import config, log

__all__ = [
    "TICK_SECONDS",
    "HISTORY_SECONDS",
    "enabled",
    "scan",
    "attribute",
    "tick",
    "row",
    "snapshot",
    "history",
    "run_loop",
]

#: How often the sampler runs.
TICK_SECONDS = 15.0
#: How often a history line is appended.
HISTORY_SECONDS = 30.0
#: A tick slower than this (wall) skips the next one.
SLOW_TICK_SECONDS = 2.0
#: ``memory.jsonl`` is rotated to ``memory.jsonl.1`` at this size.
ROTATE_BYTES = 5 * 1024 * 1024
#: How many processes outside every session tree are reported.
OTHERS_TOP = 8
#: RSS (MB) worth of processes whose ``smaps_rollup`` one tick re-reads.
PSS_BUDGET_MB = 2500
#: A session row's sample older than this reads as not sampled (None).
STALE_SECONDS = 300.0
#: ``/api/memory`` samples on demand when the last tick is older than this.
ON_DEMAND_AFTER_S = 2 * TICK_SECONDS

#: The ``/proc`` the sampler reads (a test seam: point it at a fake tree).
PROC_ROOT = "/proc"

#: Test seam: None = on except under pytest / MINDFLOCK_MEMORY_SAMPLER=0.
ENABLED: Optional[bool] = None

#: Processes the agent search looks through (a launcher shell, ``env``, a
#: package runner): the agent is the first thing under them that is not one.
_SHELLS = frozenset(
    {"sh", "bash", "zsh", "dash", "fish", "ksh", "tcsh", "csh", "env", "login"}
)
_WRAPPERS = frozenset({"uv", "uvx", "npx", "npm", "pnpm", "bunx", "pipx", "script"})
#: Interpreters whose script argument names the program (``node …/codex``).
_RUNTIMES = frozenset({"node", "bun", "deno", "python", "python3", "ruby"})
_MCP_RE = re.compile(r"mcp", re.IGNORECASE)


def _info(fmt: str, *args) -> None:
    logger = log.InfoLog or log.ErrorLog
    if logger is not None:
        try:
            logger.Printf(fmt, *args)
        except Exception:  # noqa: BLE001
            pass


def enabled() -> bool:
    """Whether the server runs the sampler loop."""
    if ENABLED is not None:
        return bool(ENABLED)
    if os.environ.get("MINDFLOCK_MEMORY_SAMPLER", "1") == "0":
        return False
    try:
        from backend.web.core import restart as _restart

        return not _restart._under_pytest()
    except Exception:  # noqa: BLE001
        return True


# --------------------------------------------------------------------------- #
# the process table
# --------------------------------------------------------------------------- #
@dataclass
class Proc:
    pid: int
    ppid: int
    start: int
    comm: str
    rss_kb: int
    argv: Optional[List[str]] = None  # read lazily (cached per pid+start)


@dataclass
class _Caches:
    """What survives between ticks, keyed by ``(pid, start)`` so a recycled
    pid never inherits another process's reading."""

    argv: Dict[Tuple[int, int], List[str]] = field(default_factory=dict)
    #: (pss/rss ratio, tick number it was read on)
    ratio: Dict[Tuple[int, int], Tuple[float, int]] = field(default_factory=dict)
    tick_no: int = 0


_CACHES = _Caches()
_PAGE_KB = (os.sysconf("SC_PAGE_SIZE") // 1024) if hasattr(os, "sysconf") else 4


def _read(path: str) -> Optional[bytes]:
    try:
        with open(path, "rb") as fh:
            return fh.read()
    except OSError:
        return None


def _scan_proc(root: str) -> Optional[Dict[int, Proc]]:
    """The whole table from ``<root>/<pid>/stat`` — one read per process."""
    try:
        names = os.listdir(root)
    except OSError:
        return None
    table: Dict[int, Proc] = {}
    for name in names:
        if not name.isdigit():
            continue
        raw = _read(os.path.join(root, name, "stat"))
        if not raw:
            continue
        try:
            text = raw.decode("ascii", "replace")
            lp, rp = text.index("("), text.rindex(")")
            rest = text[rp + 1 :].split()
            pid = int(name)
            table[pid] = Proc(
                pid=pid,
                ppid=int(rest[1]),
                start=int(rest[19]),
                comm=text[lp + 1 : rp],
                rss_kb=max(0, int(rest[21])) * _PAGE_KB,
            )
        except (ValueError, IndexError):
            continue
    return table


def _scan_psutil() -> Optional[Dict[int, Proc]]:
    try:
        import psutil  # type: ignore[import-not-found]
    except Exception:  # noqa: BLE001
        return None
    table: Dict[int, Proc] = {}
    for p in psutil.process_iter(["pid", "ppid", "name", "memory_info", "cmdline"]):
        try:
            info = p.info
            mi = info.get("memory_info")
            table[info["pid"]] = Proc(
                pid=info["pid"],
                ppid=info.get("ppid") or 0,
                start=0,
                comm=info.get("name") or "",
                rss_kb=int(mi.rss // 1024) if mi else 0,
                argv=list(info.get("cmdline") or []),
            )
        except Exception:  # noqa: BLE001
            continue
    return table


def _scan_ps() -> Optional[Dict[int, Proc]]:
    try:
        from backend.web.core import proc as _proc

        out = _proc.run_capped(
            ["ps", "-axo", "pid=,ppid=,rss=,command="],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=10,
        )
    except Exception:  # noqa: BLE001
        return None
    if out.returncode != 0:
        return None
    table: Dict[int, Proc] = {}
    for line in (out.stdout or b"").decode("utf-8", "replace").splitlines():
        parts = line.split(None, 3)
        if len(parts) < 4:
            continue
        try:
            pid, ppid, rss = int(parts[0]), int(parts[1]), int(parts[2])
        except ValueError:
            continue
        argv = parts[3].split()
        table[pid] = Proc(
            pid=pid,
            ppid=ppid,
            start=0,
            comm=os.path.basename(argv[0]) if argv else "",
            rss_kb=rss,
            argv=argv,
        )
    return table


def scan(root: Optional[str] = None) -> Tuple[Dict[int, Proc], str]:
    """``(table, source)``: the whole process table, once. ``source`` is
    ``"proc"`` (PSS available), ``"psutil"``, ``"ps"`` or ``""`` (nothing)."""
    root = root or PROC_ROOT
    if os.path.isdir(root):
        table = _scan_proc(root)
        if table:
            return table, "proc"
    for fn, src in ((_scan_psutil, "psutil"), (_scan_ps, "ps")):
        table = fn()
        if table:
            return table, src
    return {}, ""


def _argv(p: Proc, root: str) -> List[str]:
    if p.argv is not None:
        return p.argv
    key = (p.pid, p.start)
    got = _CACHES.argv.get(key)
    if got is None:
        raw = _read(os.path.join(root, str(p.pid), "cmdline")) or b""
        got = [a.decode("utf-8", "replace") for a in raw.split(b"\0") if a]
        _CACHES.argv[key] = got
    p.argv = got
    return got


def _pss_kb(root: str, pid: int) -> Optional[int]:
    raw = _read(os.path.join(root, str(pid), "smaps_rollup"))
    if not raw:
        return None
    for line in raw.split(b"\n"):
        if line.startswith(b"Pss:"):
            try:
                return int(line.split()[1])
            except (IndexError, ValueError):
                return None
    return None


def _measure(
    table: Dict[int, Proc], pids: Iterable[int], source: str, root: str
) -> Dict[int, int]:
    """``{pid: kB}`` for ``pids``: PSS within this tick's budget (never-read
    first, then the stalest reading), the rest RSS × their last PSS/RSS."""
    pids = [p for p in set(pids) if p in table]
    if source != "proc":
        return {p: table[p].rss_kb for p in pids}
    tick_no = _CACHES.tick_no

    def order(pid: int):
        got = _CACHES.ratio.get((pid, table[pid].start))
        # Never read: first, biggest first. Then the stalest.
        return (0, -table[pid].rss_kb) if got is None else (1, got[1])

    budget_kb = PSS_BUDGET_MB * 1024
    for pid in sorted(pids, key=order):
        p = table[pid]
        key = (pid, p.start)
        got = _CACHES.ratio.get(key)
        if got is not None and got[1] == tick_no:
            continue
        if budget_kb <= 0 and got is not None:
            break  # what's left keeps its ratio until a later tick
        budget_kb -= p.rss_kb
        pss = _pss_kb(root, pid)
        if pss is not None and p.rss_kb > 0:
            _CACHES.ratio[key] = (min(1.0, pss / float(p.rss_kb)), tick_no)
        elif pss is not None:
            _CACHES.ratio[key] = (1.0, tick_no)
    out: Dict[int, int] = {}
    for pid in pids:
        p = table[pid]
        got = _CACHES.ratio.get((pid, p.start))
        out[pid] = int(p.rss_kb * (got[0] if got else 1.0))
    return out


def _prune_caches(table: Dict[int, Proc]) -> None:
    live = {(p.pid, p.start) for p in table.values()}
    for d in (_CACHES.argv, _CACHES.ratio):
        for k in [k for k in d if k not in live]:
            del d[k]


# --------------------------------------------------------------------------- #
# MCP server recognition
# --------------------------------------------------------------------------- #
_SIG_CACHE: Dict[str, object] = {"at": -1e18, "mtimes": None, "sigs": ()}
_SIG_RECHECK_S = 60.0


def _sig_of(command: str, args: Sequence[str]) -> str:
    """The distinctive token of a configured server's launch: the longest
    non-flag argument (``@upstash/context7-mcp@latest`` out of ``npx -y …``),
    else the command's basename when it isn't a generic launcher."""
    toks = [a for a in args if isinstance(a, str) and a and not a.startswith("-")]
    if toks:
        return max(toks, key=len)
    base = os.path.basename(command or "")
    if base and base not in _WRAPPERS and base not in _RUNTIMES:
        return base
    return ""


def _claude_sigs(path: str) -> List[str]:
    try:
        with open(path, encoding="utf-8") as fh:
            doc = json.load(fh)
    except (OSError, ValueError):
        return []
    groups = [doc.get("mcpServers") or {}]
    for proj in (doc.get("projects") or {}).values():
        if isinstance(proj, dict):
            groups.append(proj.get("mcpServers") or {})
    out = []
    for g in groups:
        if not isinstance(g, dict):
            continue
        for ent in g.values():
            if isinstance(ent, dict) and isinstance(ent.get("command"), str):
                out.append(_sig_of(ent["command"], ent.get("args") or []))
    return out


def _codex_sigs(path: str) -> List[str]:
    try:
        import tomllib

        with open(path, "rb") as fh:
            doc = tomllib.load(fh)
    except Exception:  # noqa: BLE001
        return []
    out = []
    for ent in (doc.get("mcp_servers") or {}).values():
        if isinstance(ent, dict) and isinstance(ent.get("command"), str):
            out.append(_sig_of(ent["command"], ent.get("args") or []))
    return out


def configured_mcp_signatures() -> Tuple[str, ...]:
    """Distinctive tokens of the MCP servers the user configured for Claude
    Code and Codex. Re-read when a file's mtime moved, at most once a minute
    (``~/.claude.json`` is rewritten constantly). Never raises."""
    now = time.monotonic()
    if now - float(_SIG_CACHE["at"]) < _SIG_RECHECK_S:  # type: ignore[arg-type]
        return _SIG_CACHE["sigs"]  # type: ignore[return-value]
    _SIG_CACHE["at"] = now
    home = os.path.expanduser("~")
    files = (
        (os.path.join(home, ".claude.json"), _claude_sigs),
        (os.path.join(home, ".codex", "config.toml"), _codex_sigs),
    )
    mtimes = []
    for path, _fn in files:
        try:
            mtimes.append(os.stat(path).st_mtime)
        except OSError:
            mtimes.append(None)
    if mtimes == _SIG_CACHE["mtimes"]:
        return _SIG_CACHE["sigs"]  # type: ignore[return-value]
    sigs: List[str] = []
    for (path, fn), mt in zip(files, mtimes):
        if mt is not None:
            sigs.extend(s for s in fn(path) if s and len(s) >= 3)
    _SIG_CACHE["mtimes"] = mtimes
    _SIG_CACHE["sigs"] = tuple(sorted(set(sigs)))
    return _SIG_CACHE["sigs"]  # type: ignore[return-value]


def is_mcp_argv(argv: Sequence[str], sigs: Sequence[str] = ()) -> bool:
    """Whether ``argv`` launches an MCP stdio server: MindFlock's own
    (``-m backend.mcp``, :data:`backend.providers.mcp_attach.MODULE`), any
    command line naming ``mcp``, or one carrying a configured server's
    signature."""
    joined = " ".join(argv)
    if not joined:
        return False
    if "backend.mcp" in joined or _MCP_RE.search(joined):
        return True
    return any(s in joined for s in sigs)


# --------------------------------------------------------------------------- #
# attribution
# --------------------------------------------------------------------------- #
def _base(s: str) -> str:
    b = os.path.basename(s or "")
    for ext in (".js", ".mjs", ".cjs", ".py", ".exe"):
        if b.endswith(ext):
            b = b[: -len(ext)]
    return b


def _cli_name(p: Proc, root: str) -> str:
    """What program ``p`` is: its argv[0]'s basename, or for an interpreter
    the script it runs (``node …/bin/codex`` → ``codex``)."""
    argv = _argv(p, root)
    head = _base(argv[0]) if argv else p.comm
    if (head in _RUNTIMES or head.startswith("python")) and len(argv) > 1:
        for a in argv[1:]:
            if not a.startswith("-"):
                return _base(a)
    return head or p.comm


def _passthrough(p: Proc, root: str) -> bool:
    if p.comm in _SHELLS or p.comm in _WRAPPERS:
        return True
    argv = _argv(p, root)
    return bool(argv) and _base(argv[0]) in (_SHELLS | _WRAPPERS)


def _children_map(table: Dict[int, Proc]) -> Dict[int, List[int]]:
    kids: Dict[int, List[int]] = {}
    for p in table.values():
        if p.ppid != p.pid:
            kids.setdefault(p.ppid, []).append(p.pid)
    return kids


def _find_agent(
    table: Dict[int, Proc], kids: Dict[int, List[int]], roots: Sequence[int], root: str
) -> Optional[int]:
    """The agent CLI: the shallowest non-shell, non-wrapper process under the
    panes (the biggest one when several sit at that depth)."""
    level = [r for r in roots if r in table]
    seen = set()
    while level:
        found = [p for p in level if not _passthrough(table[p], root)]
        if found:
            return max(found, key=lambda p: table[p].rss_kb)
        nxt = []
        for p in level:
            if p in seen:
                continue
            seen.add(p)
            nxt.extend(k for k in kids.get(p, ()) if k in table)
        level = nxt
    return None


def attribute(
    table: Dict[int, Proc],
    kids: Dict[int, List[int]],
    roots: Sequence[int],
    root: str = "",
    sigs: Sequence[str] = (),
) -> Dict[int, str]:
    """``{pid: "agent"|"mcp"|"other"}`` for every process in the panes'
    trees (``roots`` and all their descendants)."""
    root = root or PROC_ROOT
    agent = _find_agent(table, kids, roots, root)
    agent_name = _cli_name(table[agent], root) if agent is not None else ""
    agent_comm = table[agent].comm if agent is not None else ""
    out: Dict[int, str] = {}
    stack = [(r, "other") for r in roots if r in table]
    while stack:
        pid, inherited = stack.pop()
        if pid in out:
            continue
        p = table[pid]
        if pid == agent:
            cls = "agent"
        elif inherited == "agent":
            if (p.comm == agent_comm and p.comm not in _RUNTIMES) or _cli_name(
                p, root
            ) == agent_name:
                cls = "agent"
            elif not _passthrough(p, root) and is_mcp_argv(_argv(p, root), sigs):
                cls = "mcp"
            elif _passthrough(p, root) and p.comm in _WRAPPERS:
                # ``npm exec @x/server`` / ``uvx …``: an MCP launcher when its
                # argv says so, else just something the agent ran.
                cls = "mcp" if is_mcp_argv(_argv(p, root), sigs) else "other"
            else:
                cls = "other"
        else:
            cls = inherited
        out[pid] = cls
        stack.extend((k, cls) for k in kids.get(pid, ()) if k in table)
    return out


# --------------------------------------------------------------------------- #
# sessions and their pane pids
# --------------------------------------------------------------------------- #
def _sessions(srv) -> List[Tuple[str, str]]:
    """``[(title, tmux name)]`` of this server's own sessions."""
    from backend.session import tmux

    out = []
    for inst in list(srv.ENGINE.instances.values()):
        title = getattr(inst, "Title", "") or ""
        if title:
            out.append((title, tmux.to_mindflock_tmux_name(title)))
    return out


def pane_pids() -> Dict[str, List[int]]:
    """``{tmux session name: [pane pid, …]}`` for every tmux session — one
    ``tmux list-panes -a`` for all of them. ``{}`` on any failure."""
    try:
        from backend.web.core import proc as _proc

        out = _proc.run_capped(
            ["tmux", "list-panes", "-a", "-F", "#{session_name}\t#{pane_pid}"],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=10,
        )
    except Exception:  # noqa: BLE001
        return {}
    if out.returncode != 0:
        return {}
    res: Dict[str, List[int]] = {}
    for line in (out.stdout or b"").decode("utf-8", "replace").splitlines():
        name, _, pid = line.rpartition("\t")
        if name and pid.strip().isdigit():
            res.setdefault(name, []).append(int(pid))
    return res


# --------------------------------------------------------------------------- #
# the published state
# --------------------------------------------------------------------------- #
_LOCK = threading.Lock()
_STATE: Dict[str, object] = {
    "at": 0.0,
    "sessions": {},  # title -> row dict
    "others": [],
    "system": {},
    "tick_ms": None,
    "tick_cpu_ms": None,
    "source": "",
    "agent_peaks": {},  # title -> agent + MCP peak mb (the launch gate's)
}


def session_mb() -> Dict[str, int]:
    """{title: mb} from the last reading — the launch gate's learned
    per-session cost (``core.launch_gate``'s sizes seam)."""
    with _LOCK:
        rows = dict(_STATE["sessions"])  # type: ignore[arg-type]
    return {
        t: int(r["mb"]) for t, r in rows.items() if isinstance(r, dict) and r.get("mb")
    }


def session_agent_mb() -> Dict[str, int]:
    """{title: agent_mb + mcp_mb} from the last reading: what the session's
    agent CLI and the MCP servers it started take, without whatever else
    runs in its panes (``other_mb``). The launch gate's per-session cost
    (``core.launch_gate``'s agent sizes seam)."""
    with _LOCK:
        rows = dict(_STATE["sessions"])  # type: ignore[arg-type]
    out: Dict[str, int] = {}
    for t, r in rows.items():
        if not isinstance(r, dict):
            continue
        n = int(r.get("agent_mb") or 0) + int(r.get("mcp_mb") or 0)
        if n:
            out[t] = n
    return out


def session_agent_peak_mb() -> Dict[str, int]:
    """{title: peak of agent_mb + mcp_mb} this incarnation, from the last
    reading (the gate counts a warming agent at the learned agent peak)."""
    with _LOCK:
        rows = dict(_STATE["sessions"])  # type: ignore[arg-type]
        peaks = dict(_STATE.get("agent_peaks") or {})  # type: ignore[call-overload]
    return {t: int(v) for t, v in peaks.items() if t in rows and v}


def session_peak_mb() -> Dict[str, int]:
    """{title: peak mb} from the last reading (the gate counts a warming
    agent at the learned peak)."""
    with _LOCK:
        rows = dict(_STATE["sessions"])  # type: ignore[arg-type]
    return {
        t: int(r["peak_mb"])
        for t, r in rows.items()
        if isinstance(r, dict) and r.get("peak_mb")
    }


#: title -> (roots tuple, peak mb, agent + MCP peak mb): both peaks reset
#: with a new incarnation.
_PEAKS: Dict[str, Tuple[Tuple[int, ...], int, int]] = {}
_HIST = {"last": 0.0, "failed": False}
_TICK_LOCK = threading.Lock()


def _mb(kb: int) -> int:
    return int(kb // 1024)


def _system() -> dict:
    """The machine's reading (:mod:`backend.memory_pressure`, cached 2 s)."""
    try:
        from backend import memory_pressure as _mem

        s = _mem.snapshot()
    except Exception:  # noqa: BLE001
        return {"level": "unknown"}
    mib = 1024 * 1024
    psi = s.get("psi") or {}
    host = s.get("host") or None
    total, swap_total = s.get("total"), s.get("swap_total")
    return {
        "total_mb": int(total // mib) if total else None,
        "available_mb": (
            int(s["available"] // mib) if s.get("available") is not None else None
        ),
        "swap_total_mb": int(swap_total // mib) if swap_total is not None else None,
        "swap_used_mb": (
            int((swap_total - (s.get("swap_free") or 0)) // mib)
            if swap_total is not None
            else None
        ),
        "psi_some_avg10": psi.get("some_avg10"),
        "psi_full_avg10": psi.get("full_avg10"),
        "level": s.get("level") or "unknown",
        # WSL only (None elsewhere): Windows' own memory and level — the
        # level above is the worse of this and the guest's.
        "host_total_mb": int(host["total"] // mib) if host else None,
        "host_available_mb": int(host["available"] // mib) if host else None,
        "host_level": host.get("level") if host else None,
        "guest_level": s.get("guest_level") if host else None,
        # The distress signals the level rests on (memory_pressure), so a
        # later look at this history can replay the rule exactly.
        "psi_some_avg60": psi.get("some_avg60"),
        "psi_full_avg60": psi.get("full_avg60"),
        "swap_in_mb_s": s.get("swap_in_mb_s"),
        "oom_kills": s.get("oom_kills"),
        "host_headroom_mb": (
            int(host["headroom"] // mib)
            if host and host.get("headroom") is not None
            else None
        ),
        "host_commit_pct": host.get("commit_pct") if host else None,
        "host_pages_in_median": host.get("pages_in_median") if host else None,
        "pending": s.get("pending"),
        "why": list(s.get("why") or []),
    }


def _proc_name(p: Proc, root: str) -> str:
    argv = _argv(p, root)
    name = _cli_name(p, root) if argv else p.comm
    return name or p.comm


def _proc_cmd(p: Proc, root: str) -> str:
    argv = _argv(p, root)
    cmd = shlex.join(argv) if argv else "[%s]" % p.comm
    return cmd if len(cmd) <= 200 else cmd[:199] + "…"


def sample(
    sessions: Sequence[Tuple[str, str]],
    panes: Dict[str, List[int]],
    root: Optional[str] = None,
    now: Optional[float] = None,
) -> dict:
    """One measurement of every session in ``sessions`` (``[(title, tmux
    name)]``), with ``panes`` (``{tmux name: [pane pid]}``) as their roots.
    Publishes it (what :func:`row` / :func:`snapshot` read) and returns it."""
    root = root or PROC_ROOT
    now = time.time() if now is None else now
    table, source = scan(root)
    _CACHES.tick_no += 1
    kids = _children_map(table)
    sigs = configured_mcp_signatures()
    in_session: set = set()
    per: Dict[str, Tuple[Tuple[int, ...], Dict[int, str]]] = {}
    for title, name in sessions:
        roots = tuple(sorted(p for p in panes.get(name, ()) if p in table))
        if not roots:
            continue
        cls = attribute(table, kids, roots, root, sigs)
        per[title] = (roots, cls)
        in_session.update(cls)
    # Outside every session: rank by RSS (free, from stat), measure the top.
    me = os.getpid()
    cands = sorted(
        (
            p
            for p in table.values()
            if p.pid not in in_session and p.rss_kb > 0 and p.pid != 2 and p.ppid != 2
        ),
        key=lambda p: -p.rss_kb,
    )[: OTHERS_TOP * 2]
    kb = _measure(table, list(in_session) + [p.pid for p in cands], source, root)
    rows: Dict[str, dict] = {}
    agent_peaks: Dict[str, int] = {}
    live_titles = set()
    for title, (roots, cls) in per.items():
        live_titles.add(title)
        sums = {"agent": 0, "mcp": 0, "other": 0}
        for pid, c in cls.items():
            sums[c] += kb.get(pid, 0)
        total = _mb(sum(sums.values()))
        agent = _mb(sums["agent"]) + _mb(sums["mcp"])
        prev = _PEAKS.get(title)
        same = bool(prev) and prev[0] == roots  # type: ignore[index]
        peak = max(total, prev[1]) if same else total  # type: ignore[index]
        agent_peak = max(agent, prev[2]) if same else agent  # type: ignore[index]
        _PEAKS[title] = (roots, peak, agent_peak)
        agent_peaks[title] = agent_peak
        rows[title] = {
            "mb": total,
            "peak_mb": peak,
            "agent_mb": _mb(sums["agent"]),
            "mcp_mb": _mb(sums["mcp"]),
            "other_mb": _mb(sums["other"]),
            "procs": len(cls),
            "at": round(now, 3),
        }
    for t in [t for t in _PEAKS if t not in live_titles]:
        del _PEAKS[t]
    others = sorted(
        (
            {
                "pid": p.pid,
                "name": _proc_name(p, root),
                "cmd": _proc_cmd(p, root),
                "mb": _mb(kb.get(p.pid, p.rss_kb)),
                **({"server": True} if p.pid == me else {}),
            }
            for p in cands
        ),
        key=lambda o: -o["mb"],
    )[:OTHERS_TOP]
    _prune_caches(table)
    result = {
        "at": round(now, 3),
        "sessions": rows,
        "others": others,
        "system": _system(),
        "source": source,
    }
    with _LOCK:
        _STATE.update(result)
        _STATE["agent_peaks"] = agent_peaks
    return result


def tick(srv=None, *, write_history: bool = True) -> dict:
    """One sampler pass over the server's sessions (blocking: run it off the
    event loop). Appends a history line when :data:`HISTORY_SECONDS` have
    passed since the last one."""
    with _TICK_LOCK:
        w0, c0 = time.monotonic(), time.thread_time()
        if srv is None:
            from backend.web import server as srv  # type: ignore[no-redef]
        sessions = _sessions(srv)
        res = sample(sessions, pane_pids() if sessions else {})
        wall_ms = (time.monotonic() - w0) * 1000.0
        cpu_ms = (time.thread_time() - c0) * 1000.0
        with _LOCK:
            _STATE["tick_ms"] = round(wall_ms, 1)
            _STATE["tick_cpu_ms"] = round(cpu_ms, 1)
        if write_history and res["at"] - _HIST["last"] >= HISTORY_SECONDS - 0.5:
            _HIST["last"] = res["at"]
            append_history(res)
        return res


# --------------------------------------------------------------------------- #
# readers
# --------------------------------------------------------------------------- #
def row(title: str, now: Optional[float] = None) -> Optional[dict]:
    """The session row's ``memory`` field: ``{"mb", "peak_mb", "agent_mb",
    "mcp_mb", "other_mb", "procs", "at"}``, or None (not sampled yet, not
    running, or the last sample is stale). A dict lookup — no I/O."""
    with _LOCK:
        r = _STATE["sessions"].get(title)  # type: ignore[union-attr]
    if not r:
        return None
    now = time.time() if now is None else now
    if now - float(r.get("at") or 0) > STALE_SECONDS:
        return None
    return dict(r)


def snapshot(srv=None) -> dict:
    """``GET /api/memory``: ``{"system", "sessions": [{"title", …row}]
    (biggest first), "others", "at", "tick_ms", "tick_cpu_ms"}``. Samples
    on demand when the loop hasn't for :data:`ON_DEMAND_AFTER_S` (off,
    skipping, or still booting) — without a history line."""
    with _LOCK:
        at = float(_STATE["at"] or 0)  # type: ignore[arg-type]
    if time.time() - at > ON_DEMAND_AFTER_S:
        try:
            tick(srv, write_history=False)
        except Exception:  # noqa: BLE001
            pass
    with _LOCK:
        st = dict(_STATE)
    sessions = sorted(
        ({"title": t, **r} for t, r in st["sessions"].items()),  # type: ignore[union-attr]
        key=lambda r: -r["mb"],
    )
    return {
        "system": st["system"] or _system(),
        "sessions": sessions,
        "others": list(st["others"]),  # type: ignore[arg-type]
        "at": st["at"] or None,
        "tick_ms": st["tick_ms"],
        "tick_cpu_ms": st["tick_cpu_ms"],
    }


# --------------------------------------------------------------------------- #
# history
# --------------------------------------------------------------------------- #
#: Test seam: the history file (None = <config dir>/logs/memory.jsonl).
HISTORY_PATH: Optional[str] = None


def history_path() -> str:
    if HISTORY_PATH:
        return HISTORY_PATH
    return os.path.join(config.GetConfigDir(), "logs", "memory.jsonl")


def append_history(res: dict) -> bool:
    """Append one line for ``res`` (a :func:`sample` result), rotating at
    :data:`ROTATE_BYTES`. Never raises; the first failure is logged."""
    try:
        path = history_path()
        line = json.dumps(
            {
                "at": res["at"],
                "system": res.get("system") or {},
                "sessions": {t: r["mb"] for t, r in res["sessions"].items()},
                "others": res.get("others") or [],
            },
            separators=(",", ":"),
            ensure_ascii=False,
        )
        os.makedirs(os.path.dirname(path), exist_ok=True)
        try:
            if os.path.getsize(path) >= ROTATE_BYTES:
                os.replace(path, path + ".1")
        except OSError:
            pass
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
        _HIST["failed"] = False
        return True
    except Exception as err:  # noqa: BLE001
        if not _HIST["failed"]:
            _info("memory history: could not write (%s)", err)
        _HIST["failed"] = True
        return False


_AT_RE = re.compile(rb'^\{"at":([0-9.]+)')


def history(minutes: float = 60.0, now: Optional[float] = None) -> List[dict]:
    """The history lines of the last ``minutes``, oldest first (the rotated
    file too). Lines are parsed only when their ``at`` is in range."""
    now = time.time() if now is None else now
    cutoff = now - max(0.0, float(minutes)) * 60.0
    try:
        path = history_path()
    except Exception:  # noqa: BLE001
        return []
    out: List[dict] = []
    for p in (path + ".1", path):
        try:
            with open(p, "rb") as fh:
                for line in fh:
                    m = _AT_RE.match(line)
                    if not m:
                        continue
                    try:
                        if float(m.group(1)) < cutoff:
                            continue
                        out.append(json.loads(line))
                    except ValueError:
                        continue
        except OSError:
            continue
    return out


# --------------------------------------------------------------------------- #
# the loop
# --------------------------------------------------------------------------- #
async def run_loop(srv=None, interval: Optional[float] = None) -> None:
    """Drive :func:`tick` forever on the ticks' own pool (``srv._in_bg_pool``).
    A tick slower than :data:`SLOW_TICK_SECONDS` skips the next one."""
    if srv is None:
        from backend.web import server as srv  # type: ignore[no-redef]
    runner: Optional[Callable] = getattr(srv, "_in_bg_pool", None)
    slow_logged = False
    skip = False
    while True:
        step = TICK_SECONDS if interval is None else interval
        if skip:
            skip = False
        else:
            t0 = time.monotonic()
            try:
                await (runner(tick, srv) if runner else asyncio.to_thread(tick, srv))
            except Exception:  # noqa: BLE001 — the sampler must never die
                pass
            took = time.monotonic() - t0
            if took > SLOW_TICK_SECONDS:
                skip = True
                if not slow_logged:
                    _info(
                        "memory sampler: a tick took %.1fs (the box is "
                        "thrashing?) — skipping every other tick until it's fast",
                        took,
                    )
                    slow_logged = True
            else:
                slow_logged = False
        await asyncio.sleep(step)
