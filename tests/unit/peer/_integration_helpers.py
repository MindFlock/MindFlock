"""Shared fakes for the peer-link integration tests.

The sandbox / share / transport modules are written by other workers; these
tests never depend on their implementation — :func:`fake_sandbox` installs a
stand-in ``backend.peer.sandbox`` whose ``available()`` the test controls.
"""

from __future__ import annotations

import datetime as _dt
import os
import sys
import types

import backend.peer as peer_pkg
from backend.peer import launch as peer_launch
from backend.peer import paths

SHARE_ID = "ab" * 16  # 32 hex chars, like secrets.token_hex(16)


def fake_sandbox(monkeypatch, ok: bool, reason: str = "no bwrap here"):
    mod = types.ModuleType("backend.peer.sandbox")
    mod.available = lambda: (ok, "" if ok else reason)
    mod.egress_allow = lambda provider, extra=None: [
        "api.anthropic.com",
        *(extra or []),
    ]
    monkeypatch.setitem(sys.modules, "backend.peer.sandbox", mod)
    monkeypatch.setattr(peer_pkg, "sandbox", mod, raising=False)
    return mod


def make_share(share_id: str = SHARE_ID) -> dict:
    """The share's directories (work/ with a gitfile, like share.create_share)."""
    p = paths.share_paths(share_id)
    for key in ("root", "work", "gitdir", "home", "run"):
        paths.ensure_dir(p[key])
    with open(os.path.join(p["work"], ".git"), "w") as fh:
        fh.write("gitdir: %s\n" % p["gitdir"])
    return p


def register_token(
    monkeypatch, share_id: str = SHARE_ID, token: str = "tok-" + "x" * 40
):
    monkeypatch.setattr(peer_launch, "_TOKENS", {}, raising=True)
    peer_launch.register_token(share_id, token)
    return token


class FakeTmux:
    """Stands in for ``tmux.TmuxSession``: records every start."""

    def __init__(self, name: str = "mindflock_t"):
        self.sanitized_name = name
        self.program = "claude"
        self.launch_command = None
        self.extra_env = {}
        self.starts = []

    def start(self, work_dir):
        self.starts.append((work_dir, self.launch_command, dict(self.extra_env)))
        return None

    def restore(self):
        return None

    def does_session_exist(self):
        return False

    def close(self):
        return None


def mk_inst(title: str, wt: str, *, peer_share: str = "", program: str = "claude"):
    """A registered-looking (unattached) instance on ``wt``."""
    from backend.session.instance import FromInstanceData
    from backend.session.storage import GitWorktreeData, InstanceData, Status

    t = _dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(seconds=120)
    data = InstanceData(
        title=title,
        path=wt,
        branch="main",
        status=Status.Running,
        created_at=t,
        updated_at=t,
        program=program,
        in_place=True,
        peer_share=peer_share,
        worktree=GitWorktreeData(
            repo_path=wt, worktree_path=wt, session_name=title, branch_name="main"
        ),
    )
    return FromInstanceData(data, attach=False)
