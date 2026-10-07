"""Persisted peer links (``links.json``, 0600).

Every operation re-reads the file under an exclusive ``fcntl.flock`` on a
sibling lock file, and every write is atomic (fresh 0600 tmp file, fsync,
``os.replace``). A file that doesn't parse — or holds any entry that fails
validation — loads as empty and is moved aside to ``links.json.corrupt-<ts>``.

Peer-supplied strings are untrusted: ``peer_name`` is sanitized to
``[A-Za-z0-9 ._-]{1,32}`` (empty -> ``peer``) whenever a Link is built.
"""

from __future__ import annotations

import contextlib
import dataclasses
import fcntl
import json
import logging
import os
import re
import stat
import threading
import time
from dataclasses import dataclass, field

from backend.peer import paths
from backend.peer.addr import parse_addr

__all__ = ["Link", "LinkStore", "sanitize_name", "DEFAULT_PERMS", "PERM_KEYS"]

log = logging.getLogger(__name__)

PERM_KEYS = ("messages", "diff", "read_file")
DEFAULT_PERMS = {"messages": True, "diff": True, "read_file": True}
ROLES = ("listener", "dialer")
_DOC_VERSION = 1
_MAX_FILE = 16 * 1024 * 1024

_NAME_BAD = re.compile(r"[^A-Za-z0-9 ._-]+")
_LINK_ID_RE = re.compile(r"^[0-9a-f]{32}\Z")
_PUB_RE = re.compile(r"^[0-9a-f]{64}\Z")
_SAS_RE = re.compile(r"^[0-9]{3}-[0-9]{3}-[0-9]{3}-[0-9]\Z")
_ADDR_RE = re.compile(r"^(?:\[[0-9A-Fa-f:.]{2,45}\]|[A-Za-z0-9.-]{1,253}):[0-9]{1,5}\Z")
_TITLE_MAX = 200


def sanitize_name(name) -> str:
    text = _NAME_BAD.sub("", name if isinstance(name, str) else "")
    text = " ".join(text.split())[:32].strip()
    return text or "peer"


def _is_num(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


@dataclass
class Link:
    link_id: str
    peer_name: str
    peer_pub: str  # 64 lowercase hex chars (raw Ed25519 key)
    role: str  # "listener" | "dialer"
    # dialer only: "host:port" ("[v6]:port"), or a relay "wss://host:port/path"
    peer_addr: str | None = None
    created: float = field(default_factory=time.time)
    last_seen: float = field(default_factory=time.time)
    sas: str = ""
    perms: dict = field(default_factory=lambda: dict(DEFAULT_PERMS))
    share_id: str | None = None
    session_title: str | None = None

    def __post_init__(self):
        self.peer_name = sanitize_name(self.peer_name)
        if isinstance(self.perms, dict):
            self.perms = {**DEFAULT_PERMS, **self.perms}

    def validate(self) -> None:
        """Raise ValueError unless every field is well formed."""
        if not isinstance(self.link_id, str) or not _LINK_ID_RE.match(self.link_id):
            raise ValueError("bad link_id")
        if not isinstance(self.peer_pub, str) or not _PUB_RE.match(self.peer_pub):
            raise ValueError("bad peer_pub")
        if self.role not in ROLES:
            raise ValueError("bad role")
        if self.role == "dialer":
            if not isinstance(self.peer_addr, str):
                raise ValueError("bad peer_addr")
            if self.peer_addr.startswith("wss://"):
                try:
                    if str(parse_addr(self.peer_addr)) != self.peer_addr:
                        raise ValueError  # only the canonical form is stored
                except ValueError:
                    raise ValueError("bad peer_addr") from None
            else:
                if not _ADDR_RE.match(self.peer_addr):
                    raise ValueError("bad peer_addr")
                if not 1 <= int(self.peer_addr.rsplit(":", 1)[1]) <= 65535:
                    raise ValueError("bad peer_addr")
        elif self.peer_addr is not None:
            raise ValueError("listener links have no peer_addr")
        if not _is_num(self.created) or not _is_num(self.last_seen):
            raise ValueError("bad timestamps")
        if not isinstance(self.sas, str) or (self.sas and not _SAS_RE.match(self.sas)):
            raise ValueError("bad sas")
        if (
            not isinstance(self.perms, dict)
            or set(self.perms) != set(PERM_KEYS)
            or not all(isinstance(v, bool) for v in self.perms.values())
        ):
            raise ValueError("bad perms")
        if self.share_id is not None and (
            not isinstance(self.share_id, str)
            or not paths.SHARE_ID_RE.match(self.share_id)
        ):
            raise ValueError("bad share_id")
        if self.session_title is not None and (
            not isinstance(self.session_title, str)
            or not 0 < len(self.session_title) <= _TITLE_MAX
        ):
            raise ValueError("bad session_title")

    def to_dict(self) -> dict:
        return dataclasses.asdict(self)

    @classmethod
    def from_dict(cls, d) -> "Link":
        if not isinstance(d, dict):
            raise ValueError("link entry is not an object")
        names = {f.name for f in dataclasses.fields(cls)}
        if set(d) - names:
            raise ValueError("unknown link fields")
        try:
            link = cls(**d)
        except TypeError:
            raise ValueError("missing link fields") from None
        link.validate()
        return link


_MUTABLE = frozenset(
    {"peer_name", "peer_addr", "last_seen", "sas", "perms", "share_id", "session_title"}
)


class LinkStore:
    def __init__(self, path: str | None = None):
        self.path = path or paths.links_file()
        self._lock_path = self.path + ".lock"
        self._tlock = threading.Lock()

    # -- file plumbing -------------------------------------------------------

    @contextlib.contextmanager
    def _locked(self):
        paths.ensure_dir(os.path.dirname(self.path))
        with self._tlock:
            fd = os.open(self._lock_path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX)
                yield
            finally:
                os.close(fd)  # releases the flock

    def _backup_corrupt(self) -> None:
        dest = f"{self.path}.corrupt-{int(time.time())}-{os.urandom(2).hex()}"
        try:
            os.replace(self.path, dest)
            os.chmod(dest, 0o600)
            log.warning("peer: links file was corrupt; moved aside to %s", dest)
        except OSError as e:
            log.warning(
                "peer: links file was corrupt and could not be moved: %s", e.strerror
            )

    def _load(self) -> dict[str, Link]:
        try:
            fd = os.open(self.path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        except FileNotFoundError:
            return {}
        except OSError:
            # e.g. a symlink planted in our 0700 dir: never follow it.
            self._backup_corrupt()
            return {}
        try:
            st = os.fstat(fd)
            if not stat.S_ISREG(st.st_mode) or st.st_size > _MAX_FILE:
                raw = None
            else:
                if st.st_mode & 0o077:
                    os.fchmod(fd, 0o600)
                with os.fdopen(os.dup(fd), "rb") as f:
                    raw = f.read(_MAX_FILE + 1)
        finally:
            os.close(fd)
        try:
            if raw is None:
                raise ValueError
            doc = json.loads(raw.decode("utf-8"))
            if not isinstance(doc, dict) or doc.get("version") != _DOC_VERSION:
                raise ValueError
            entries = doc.get("links")
            if not isinstance(entries, dict):
                raise ValueError
            links = {}
            for lid, d in entries.items():
                link = Link.from_dict(d)
                if link.link_id != lid:
                    raise ValueError
                links[lid] = link
            return links
        except (ValueError, UnicodeDecodeError, RecursionError):
            self._backup_corrupt()
            return {}

    def _save(self, links: dict[str, Link]) -> None:
        doc = {
            "version": _DOC_VERSION,
            "links": {lid: l.to_dict() for lid, l in links.items()},
        }
        data = json.dumps(doc, indent=1, sort_keys=True).encode("utf-8")
        d = os.path.dirname(self.path)
        tmp = f"{self.path}.tmp-{os.getpid()}-{os.urandom(4).hex()}"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        try:
            os.fchmod(fd, 0o600)
            view = memoryview(data)
            while view:
                view = view[os.write(fd, view) :]
            os.fsync(fd)
        except BaseException:
            os.close(fd)
            os.unlink(tmp)
            raise
        os.close(fd)
        os.replace(tmp, self.path)
        dfd = os.open(d, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(dfd)
        finally:
            os.close(dfd)

    # -- public API ----------------------------------------------------------

    def list(self) -> list[Link]:
        with self._locked():
            return sorted(self._load().values(), key=lambda l: l.created)

    def get(self, link_id) -> Link | None:
        if not isinstance(link_id, str) or not _LINK_ID_RE.match(link_id):
            return None
        with self._locked():
            return self._load().get(link_id)

    def add(self, link: Link) -> Link:
        link = Link.from_dict(link.to_dict())  # sanitize + validate a copy
        with self._locked():
            links = self._load()
            if link.link_id in links:
                raise ValueError("link already exists")
            links[link.link_id] = link
            self._save(links)
        return link

    def update(self, link_id: str, /, **fields) -> Link | None:
        """Change mutable fields; returns the new Link, or None if absent."""
        bad = set(fields) - _MUTABLE
        if bad:
            raise ValueError(f"cannot update fields: {sorted(bad)}")
        if "perms" in fields:
            perms = fields["perms"]
            if not isinstance(perms, dict) or set(perms) - set(PERM_KEYS):
                raise ValueError("bad perms")
        with self._locked():
            links = self._load()
            cur = links.get(link_id)
            if cur is None:
                return None
            d = cur.to_dict()
            if "perms" in fields:
                fields = {**fields, "perms": {**cur.perms, **fields["perms"]}}
            d.update(fields)
            new = Link.from_dict(d)
            links[link_id] = new
            self._save(links)
            return new

    def remove(self, link_id: str) -> bool:
        with self._locked():
            links = self._load()
            if links.pop(link_id, None) is None:
                return False
            self._save(links)
            return True
