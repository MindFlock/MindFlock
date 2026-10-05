"""Which MindFlock session is this MCP server running inside?

Resolution order (first hit wins):

1. ``MINDFLOCK_SESSION_TITLE`` — baked per session by the auto-attach config
   (and the only option under Codex, which clears the environment of its MCP
   servers down to an allow-list). Must name a live local row — and when the
   tmux pane unambiguously belongs to a DIFFERENT live session, resolution
   fails closed (a stale baked title must never borrow a namesake's identity).
2. Inside tmux (``TMUX`` and ``TMUX_PANE`` set — never a bare lookup, which
   answers for the FOCUSED window): ``tmux display-message -p -t $TMUX_PANE
   '#{session_name}'`` matched EXACTLY against the rows' ``tmux_name``. The
   title → tmux-name mapping is lossy (whitespace dropped, ``.`` → ``_``), so
   more than one match is ambiguous and resolves to nobody. Only when nothing
   matches and the name ends in ``_sh`` (the agent's shell pane,
   ``<tmux_name>_sh``) is the suffix stripped and the match retried — exact
   first, because a title may legitimately end in ``_sh``.
3. Otherwise: no session — an external client (the user's own Claude Code
   registered via ``mindflock mcp --print-config``).

``MINDFLOCK_MCP_MANAGED=1`` marks a server MindFlock attached itself. Such a
server that cannot resolve its session must FAIL CLOSED (the policy drops it
to ``readonly``) instead of silently becoming an all-powerful external client.

A resolved title is cached and re-validated against every fresh listing; if
it vanishes, resolution runs again. The tmux name is looked up once.
"""

from __future__ import annotations

import logging
import os
import subprocess
from typing import Any, Callable, Iterable, List, Mapping, Optional

_log = logging.getLogger(__name__)

__all__ = ["Identity", "is_local_row", "SHELL_SUFFIX"]

#: The agent shell pane's tmux session is ``<agent tmux name>`` + this.
SHELL_SUFFIX = "_sh"


def is_local_row(row: dict) -> bool:
    """A row for a real session on THIS server: not a remote ``device::title``
    row and not a pending (not-yet-created) placeholder."""
    title = str(row.get("title") or "")
    return bool(title) and "::" not in title and not row.get("pending")


class Identity:
    """Resolves (and caches) this process's own session title."""

    def __init__(
        self,
        env: Optional[Mapping[str, str]] = None,
        run: Optional[Callable[..., Any]] = None,
    ) -> None:
        self.env = os.environ if env is None else env
        self._run = run or subprocess.run
        self._title: Optional[str] = None
        self._tmux_name: Optional[str] = None
        self._tmux_looked_up = False
        #: Why the last resolution failed ("" when resolved / not attempted).
        self.reason = ""

    @property
    def managed(self) -> bool:
        """True when MindFlock itself attached this server to a session."""
        return (self.env.get("MINDFLOCK_MCP_MANAGED") or "").strip() == "1"

    @property
    def env_title(self) -> str:
        return (self.env.get("MINDFLOCK_SESSION_TITLE") or "").strip()

    def tmux_session_name(self) -> str:
        """``#{session_name}`` of the pane we run in ("" outside tmux)."""
        if self._tmux_looked_up:
            return self._tmux_name or ""
        self._tmux_looked_up = True
        if not self.env.get("TMUX"):
            return ""
        # Never a bare `display-message`: without -t tmux answers for the
        # client's CURRENT window — whatever the human is looking at — so a
        # pane-less lookup would borrow the focused session's identity.
        pane = (self.env.get("TMUX_PANE") or "").strip()
        if not pane:
            return ""
        cmd = ["tmux", "display-message", "-p", "-t", pane, "#{session_name}"]
        try:
            cp = self._run(cmd, capture_output=True, text=True, timeout=5)
        except (OSError, subprocess.SubprocessError) as err:
            _log.debug("mcp identity: tmux lookup failed: %s", err)
            return ""
        if getattr(cp, "returncode", 1) != 0:
            return ""
        self._tmux_name = str(cp.stdout or "").strip()
        return self._tmux_name

    def resolve(self, rows: Iterable[dict]) -> Optional[str]:
        """This session's title given a fresh ``/api/instances`` listing, or
        None (see the module docstring for the rules)."""
        local = [r for r in rows if isinstance(r, dict) and is_local_row(r)]
        titles = {str(r.get("title")) for r in local}
        if self._title is not None and self._title in titles:
            return self._title
        self._title = None
        self.reason = ""
        want = self.env_title
        if want:
            if want in titles:
                other = self._pane_owner(local, want)
                if other:
                    # The baked title names a live session, but we run in
                    # ANOTHER live session's pane (a stale launcher, a
                    # reopened-and-renamed session): fail closed rather than
                    # act with a namesake's authority.
                    self.reason = (
                        "MINDFLOCK_SESSION_TITLE=%r but this is the terminal of "
                        "session %r" % (want, other)
                    )
                    return None
                self._title = want
                return want
            self.reason = "MINDFLOCK_SESSION_TITLE=%r is not a live session" % want
            return None
        name = self.tmux_session_name()
        if not name:
            self.reason = "not running inside a MindFlock session"
            return None
        match = self._match(local, name)
        if match is None and name.endswith(SHELL_SUFFIX):
            match = self._match(local, name[: -len(SHELL_SUFFIX)])
        if match == "":
            self.reason = "tmux session %r matches more than one session" % name
            return None
        if match is None:
            self.reason = "tmux session %r is not a MindFlock session" % name
            return None
        self._title = match
        return match

    def _pane_owner(self, rows: List[dict], want: str) -> str:
        """The OTHER live session whose terminal this process runs in, when the
        tmux pane says so unambiguously; "" otherwise (outside tmux, an
        unknown pane, or ``want``'s own)."""
        name = self.tmux_session_name()
        if not name:
            return ""
        mine = {
            str(r.get("tmux_name") or "") for r in rows if str(r.get("title")) == want
        }
        if name in mine or (
            name.endswith(SHELL_SUFFIX) and name[: -len(SHELL_SUFFIX)] in mine
        ):
            return ""
        match = self._match(rows, name)
        if match is None and name.endswith(SHELL_SUFFIX):
            match = self._match(rows, name[: -len(SHELL_SUFFIX)])
        return match if match and match != want else ""

    @staticmethod
    def _match(rows: List[dict], tmux_name: str) -> Optional[str]:
        """The unique title whose ``tmux_name`` equals ``tmux_name``; None for
        no match, "" for an ambiguous one."""
        hits = [str(r.get("title")) for r in rows if r.get("tmux_name") == tmux_name]
        if not hits:
            return None
        return hits[0] if len(hits) == 1 else ""
