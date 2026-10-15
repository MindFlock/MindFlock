"""The standing export mirror: the peer's shared work, kept as a ``peer/…``
branch in one of YOUR repos (``~/.mindflock/peer/mirror/<link_id>.json``, 0600).

Export is the manual button (checkpoint YOUR share, fetch it into your
repo); the mirror is its standing form for THEIR share. It cannot fetch their
git dir — that lives on their machine — so the objects cross the link:

* their ``status`` answers ``head`` (their share's HEAD); a change, with a
  mirror enabled here, starts an update (:func:`wants_update`), and so does
  enabling one;
* the update asks them ``mirror_pull {have, offset}`` until it holds the
  whole git bundle they cut (:func:`serve_pull` on their side,
  ``share.mirror_bundle``): the parent-rewritten copy of their HEAD's chain,
  only what follows ``have`` (our mirror branch's tip) when they can;
* then ``share.fetch_into`` — export's own fetch step — fetches it into the
  mirror repo as ``refs/heads/<branch>``, fscking every object. Only that ref
  moves: the working tree is never touched, and a branch checked out in the
  repo is refused (``share.check_target``, export's own checks).

The per-link file is separate from ``links.json`` on purpose: an older
MindFlock's ``LinkStore.validate()`` must never see it. Single process (the
server's one PeerService), so a thread lock plus atomic writes are enough.
See ``docs/peer-link.md`` ("The mirror — CONTRACT").
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import contextlib
import datetime as _dt
import hashlib
import json
import os
import re
import tempfile
import threading
import time
from typing import Awaitable, Callable, Dict, List, Optional, Tuple

from backend.peer import paths
from backend.peer import share as share_mod

__all__ = [
    "MirrorError",
    "load",
    "view",
    "enable",
    "drop",
    "move",
    "enabled_links",
    "default_branch",
    "wants_update",
    "serve_pull",
    "update",
    "CHUNK",
]

VERSION = 1
#: Raw bytes of bundle per ``mirror_pull`` response (base64 keeps the frame
#: under wire.MAX_FRAME).
CHUNK = 720_000
#: Pause between chunk requests, so a large first mirror stays well under the
#: link's 120 requests/min and its status polls keep answering.
PULL_PAUSE_S = 0.5
#: After a failed update for one head, wait this long before trying it again.
RETRY_S = 60.0
_LINK_ID_RE = re.compile(r"^[0-9a-f]{32}\Z")
_SHA_RE = re.compile(r"^[0-9a-f]{40}(?:[0-9a-f]{24})?\Z")
_SLUG_RE = re.compile(r"[^a-z0-9._-]+")
_LOCK = threading.Lock()
_FIELDS = ("repo_path", "branch", "enabled", "last_head", "last_at", "last_error")


class MirrorError(Exception):
    """A mirror request the user must fix; the message says how."""


# --------------------------------------------------------------------------- #
# The per-link file
# --------------------------------------------------------------------------- #
def _dir() -> str:
    return paths.ensure_dir(os.path.join(paths.peer_root(), "mirror"))


def _path(link_id: str) -> str:
    if not isinstance(link_id, str) or not _LINK_ID_RE.match(link_id):
        raise ValueError("bad link id")
    return os.path.join(_dir(), link_id + ".json")


def _norm(m) -> Optional[dict]:
    if not isinstance(m, dict):
        return None
    repo, branch = m.get("repo_path"), m.get("branch")
    if not isinstance(repo, str) or not repo or not isinstance(branch, str):
        return None
    out = {
        "repo_path": repo,
        "branch": branch,
        "enabled": m.get("enabled") is True,
        "last_head": "",
        "last_at": "",
        "last_error": "",
    }
    for k in ("last_head", "last_at", "last_error"):
        if isinstance(m.get(k), str):
            out[k] = m[k][:300]
    return out


def load(link_id: str) -> Optional[dict]:
    """The link's mirror (``{repo_path, branch, enabled, last_head, last_at,
    last_error}``) or None. A missing, unreadable or foreign file is None."""
    try:
        fd = os.open(_path(link_id), os.O_RDONLY | os.O_NOFOLLOW)
    except (OSError, ValueError):
        return None
    try:
        with os.fdopen(fd, "rb") as f:
            doc = json.loads(f.read(65536).decode("utf-8"))
    except (OSError, ValueError, UnicodeDecodeError):
        return None
    return _norm(doc.get("mirror")) if isinstance(doc, dict) else None


def _save(link_id: str, m: dict) -> dict:
    path = _path(link_id)
    m = {k: m[k] for k in _FIELDS}
    tmp = "%s.tmp-%d-%s" % (path, os.getpid(), os.urandom(4).hex())
    data = json.dumps({"version": VERSION, "mirror": m}).encode("utf-8")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise
    return m


def _patch(link_id: str, **fields) -> Optional[dict]:
    """Merge ``fields`` into the stored mirror (None when there is none —
    disabled or removed meanwhile, so a finishing update can't revive it)."""
    with _LOCK:
        m = load(link_id)
        if m is None:
            return None
        m.update(fields)
        return _save(link_id, m)


def drop(link_id: str) -> None:
    """Forget a link's mirror (``DELETE …/mirror``, or the link went). The
    ``peer/`` branch it made stays in the repo: it's the user's now."""
    with _LOCK, contextlib.suppress(OSError, ValueError):
        os.unlink(_path(link_id))


def move(old_id: str, new_id: str) -> None:
    """A re-pair minted a new link id: carry the mirror over (unless the new
    link has one)."""
    with _LOCK:
        m = load(old_id)
        if m is not None and load(new_id) is None:
            _save(new_id, m)
    drop(old_id)


def enabled_links() -> List[str]:
    try:
        names = os.listdir(_dir())
    except OSError:
        return []
    out = []
    for n in sorted(names):
        lid = n[:-5] if n.endswith(".json") else ""
        if _LINK_ID_RE.match(lid) and (load(lid) or {}).get("enabled"):
            out.append(lid)
    return out


def view(link_id: str, updating: bool = False) -> Optional[dict]:
    """``link_view.mirror``: the stored mirror plus ``updating`` (an update
    is running now), or None when the link has none."""
    m = load(link_id)
    if m is None:
        return None
    m["updating"] = bool(updating)
    return m


def default_branch(peer_name: str) -> str:
    """``peer/<their name>`` as a branch-safe slug (``peer/peer`` at worst)."""
    slug = _SLUG_RE.sub("-", str(peer_name or "").strip().lower()).strip(".-_")
    return "peer/" + (slug[:60] or "peer")


def enable(link_id: str, repo_path, branch, peer_name: str) -> dict:
    """Validate and store a mirror (``POST …/mirror``) → the stored mirror.

    Validation is export's (``share.check_target``): an existing git repo
    outside the peer folder, a ``peer/…`` branch that is a valid name and
    is checked out nowhere in that repo. Blocking (runs git)."""
    repo = str(repo_path or "").strip()
    if not repo:
        raise MirrorError("pick the repository to mirror into")
    branch = str(branch or "").strip() or default_branch(peer_name)
    try:
        target = share_mod.check_target(repo, branch)
    except share_mod.ShareError as err:
        raise MirrorError(_why(err, branch)) from None
    with _LOCK:
        old = load(link_id) or {}
        same = old.get("repo_path") == target and old.get("branch") == branch
        return _save(
            link_id,
            {
                "repo_path": target,
                "branch": branch,
                "enabled": True,
                "last_head": old.get("last_head", "") if same else "",
                "last_at": old.get("last_at", "") if same else "",
                "last_error": "",
            },
        )


def _why(err: Exception, branch: str) -> str:
    text = str(err)
    if text == "target is not a git repository":
        return "that folder is not a git repository"
    if text == "target repo is not a directory":
        return "that folder doesn't exist"
    if text in ("bad branch name", "branch name must start with peer/"):
        return 'the branch must be a valid name starting with "peer/"'
    if "is checked out in the target" in text:
        return (
            "%s is checked out in that repository — switch it to another "
            "branch, or mirror into a different branch" % branch
        )
    return text


# --------------------------------------------------------------------------- #
# When to update
# --------------------------------------------------------------------------- #
def wants_update(
    m: Optional[dict],
    head,
    failed: Optional[Tuple[str, float]] = None,
    now: Optional[float] = None,
) -> bool:
    """Should their reported ``head`` start an update of mirror ``m``?

    Yes when the mirror is enabled and ``head`` is a sha other than the one
    last mirrored — except right after an update for that same head failed
    (``failed`` = (head, monotonic time)), for :data:`RETRY_S`. No ``head``
    (an older peer, or nothing shared) never triggers."""
    if not m or not m.get("enabled"):
        return False
    if not isinstance(head, str) or not _SHA_RE.match(head):
        return False
    if head == m.get("last_head"):
        return False
    if failed is not None and failed[0] == head:
        t = time.monotonic() if now is None else now
        if t - failed[1] < RETRY_S:
            return False
    return True


# --------------------------------------------------------------------------- #
# The sharing side: answering mirror_pull
# --------------------------------------------------------------------------- #
# link_id -> the bundle being handed out: {have, head, tip, size, sha256, path}
_OUT: Dict[str, dict] = {}
_OUT_LOCK = threading.Lock()


def _sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def serve_pull(share, link_id: str, have: str, offset: int) -> dict:
    """One ``mirror_pull`` for OUR share (blocking) → ``{head, tip, size,
    sha256, offset, data}``.

    ``offset`` 0 cuts a fresh bundle (``share.mirror_bundle``) into the
    share's root (outside every sandbox mount); later offsets read the same
    one. ``size`` 0: the peer's mirror already holds ``tip``. The file goes
    once its last chunk is served; a later request rebuilds it (its
    ``sha256`` tells the receiver whether it is the same bundle)."""
    have = have if isinstance(have, str) and _SHA_RE.match(have) else ""
    with _OUT_LOCK:
        cur = _OUT.get(link_id)
        if offset == 0 or cur is None or cur["have"] != have:
            if cur is not None:
                with contextlib.suppress(OSError):
                    os.unlink(cur["path"])
            _OUT.pop(link_id, None)
            out_path = os.path.join(share.root, "mirror-out.bundle")
            with contextlib.suppress(OSError):
                os.unlink(out_path)
            info = share_mod.mirror_bundle(share, have, out_path)
            if not info["size"]:
                return dict(info, sha256="", offset=0, data="")
            cur = dict(info, have=have, path=out_path, sha256=_sha256_file(out_path))
            _OUT[link_id] = cur
        if not 0 <= offset < cur["size"]:
            raise share_mod.ShareError("bad offset")
        with open(cur["path"], "rb") as f:
            f.seek(offset)
            data = f.read(CHUNK)
        if offset + len(data) >= cur["size"]:
            _OUT.pop(link_id, None)
            with contextlib.suppress(OSError):
                os.unlink(cur["path"])
    return {
        "head": cur["head"],
        "tip": cur["tip"],
        "size": cur["size"],
        "sha256": cur["sha256"],
        "offset": offset,
        "data": base64.b64encode(data).decode("ascii"),
    }


# --------------------------------------------------------------------------- #
# The receiving side: one update
# --------------------------------------------------------------------------- #
def _now_iso() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds")


def _branch_tip(target: str, branch: str) -> str:
    res = share_mod._target_git(
        target, "rev-parse", "--verify", "-q", "refs/heads/%s^{commit}" % branch
    )
    sha = res.stdout.decode("ascii", "replace").strip()
    return sha if _SHA_RE.match(sha) else ""


class _Changed(Exception):
    """The sharer cut a different bundle mid-transfer."""


async def _download(pull, have: str, fd: int) -> dict:
    """Pull the whole bundle into ``fd`` → the first response's header."""
    first = None
    offset = 0
    while True:
        if offset:
            await asyncio.sleep(PULL_PAUSE_S)
        res = await pull({"have": have, "offset": offset})
        hdr = {k: res.get(k) for k in ("head", "tip", "size", "sha256")}
        if first is None:
            first = hdr
            if not first["size"]:
                return first
        elif hdr != first:
            raise _Changed()
        if res.get("offset") != offset:
            raise MirrorError("their answer was out of order")
        try:
            data = base64.b64decode(res.get("data") or "", validate=True)
        except (binascii.Error, ValueError):
            raise MirrorError("their answer was garbled") from None
        if not data or offset + len(data) > first["size"]:
            raise MirrorError("their answer was garbled")
        os.write(fd, data)
        offset += len(data)
        if offset == first["size"]:
            return first


async def update(
    link_id: str,
    *,
    pull: Callable[[dict], Awaitable[dict]],
    peer_name: str,
    describe: Callable[[BaseException], str] = str,
) -> Optional[Tuple[str, dict]]:
    """Bring the link's mirror up to date with their share → the event to
    emit, ``("updated", data)`` / ``("failed", data)``, or None (nothing
    changed, nothing to say: no mirror, up to date, nothing shared yet, or a
    failure already reported). ``pull`` sends one ``mirror_pull``;
    ``describe`` words a transport error for the user."""
    m = load(link_id)
    if m is None or not m["enabled"]:
        return None
    repo, branch = m["repo_path"], m["branch"]
    base = {
        "link_id": link_id,
        "peer_name": peer_name,
        "repo_path": repo,
        "branch": branch,
    }
    fd, tmp = tempfile.mkstemp(prefix=link_id[:8] + "-", suffix=".bundle", dir=_dir())
    try:
        target = await asyncio.to_thread(share_mod.check_target, repo, branch)
        have = await asyncio.to_thread(_branch_tip, target, branch)
        try:
            hdr = await _download(pull, have, fd)
        except _Changed:  # they checkpointed meanwhile: once more from the top
            os.ftruncate(fd, 0)
            os.lseek(fd, 0, os.SEEK_SET)
            try:
                hdr = await _download(pull, have, fd)
            except _Changed:
                raise MirrorError(
                    "their work kept changing; trying again later"
                ) from None
        head, tip = str(hdr["head"]), str(hdr["tip"])
        if hdr["size"]:
            if await asyncio.to_thread(_sha256_file, tmp) != hdr["sha256"]:
                raise MirrorError("their answer was garbled")
            await asyncio.to_thread(
                share_mod.fetch_into,
                target,
                tmp,
                share_mod.MIRROR_REF,
                branch,
                tip,
                what="mirror",
                untrusted=True,
            )
        changed = bool(hdr["size"])
        if _patch(link_id, last_head=head, last_at=_now_iso(), last_error="") is None:
            return None
        return ("updated", dict(base, sha=tip, head=head)) if changed else None
    except asyncio.CancelledError:
        raise
    except Exception as err:  # noqa: BLE001
        if isinstance(err, (share_mod.ShareError, MirrorError)):
            text = _why(err, branch)
        else:
            text = describe(err)
        if "hasn't shared a folder" in text or str(err) == "no shared folder":
            return None  # nothing to mirror yet; their next share starts it
        text = text[:300]
        if _patch(link_id, last_error=text) is None or text == m["last_error"]:
            return None  # reported already: once per distinct error
        return ("failed", dict(base, error=text))
    finally:
        os.close(fd)
        with contextlib.suppress(OSError):
            os.unlink(tmp)
