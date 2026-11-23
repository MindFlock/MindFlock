"""The hub: one shared place per MindFlock home for files, images and text
snippets every session can use (``/api/hub``; the MCP's ``hub`` tool;
``mindflock hub``; docs/web-ui.md "Hub").

An **item** is one file (a picture, a log, a PDF …) or a text snippet. It
lives at ``<mindflock home>/hub/<id>/<name>`` — a stable absolute path any
agent can read — and has a **reference**, ``hub:<id>`` (``hub:k7m2``), that
the owner types or pastes into any prompt: when that prompt is sent through
MindFlock it is expanded (:func:`expand`) to the item's path, name and kind.

Safety (hub content can come from anywhere — a phone, an agent, a paste):

* **Where.** Only inside ``<home>/hub``: each item in its own folder named by
  its id (``[a-z2-9]{4,8}``), so no name can traverse. Names are sanitised
  (:func:`clean_name`: no separators, control/bidi/invisible characters,
  Windows-reserved names or characters, trailing dots/spaces, leading dots).
  Files are created here with ``O_EXCL|O_NOFOLLOW`` in folders this code
  made; nothing is ever followed through a symlink, and every file lands
  ``0644`` (no executable bit). The hub folder itself must not be a link.
* **What reaches a prompt.** A reference expands to ONE line, labelled as
  hub content and untrusted, with the name passed through
  :func:`send_session.clean_line` (control characters shown escaped).
* **How much.** A total quota, a per-item limit and an item count limit
  (``hub.*`` in Settings, docs/configuration.md). An add reserves its size
  under the lock before a byte is written, so concurrent adds can't pass the
  quota together; the commit rechecks against the index on disk (two
  servers may share one home: a file lock, :func:`_locked`).
* **How long.** An unpinned item is removed ``hub.keep_days`` (14) after it
  was added or last used (a reference sent, a copy into a session). A
  removed item is kept :data:`UNDO_S` for Undo, then purged; its id is never
  reused while :data:`TOMBSTONE_S` remembers it, so an old reference says
  "removed" instead of naming a new item. An item being copied is never
  expired under the copy (:data:`_BUSY`).
* **Who.** The owner may change or remove anything; an agent (a session's
  MCP) may put only files from inside its own worktree (resolved real path,
  the transfer's secrets filter, its fence and red zones), copy only INTO its
  own worktree, and remove only items it added. Enforced by the routes that
  carry the session (server.py), with the predicates here.

Nothing here runs, opens or renders anything it stores.
"""

from __future__ import annotations

import contextlib
import hashlib
import os
import re
import secrets
import stat
import threading
import time
import unicodedata
from typing import Callable, Dict, Iterable, Iterator, List, Optional, Tuple

from backend.config import config
from backend.fsutil import atomic_write_json, read_json
from backend.web.core import send_files as _send_files
from backend.web.core.send_session import Refusal, clean_line, clean_text

try:  # POSIX: a lock two servers sharing one home both honour.
    import fcntl as _fcntl
except ImportError:  # pragma: no cover — Windows: the in-process lock only
    _fcntl = None  # type: ignore[assignment]

INDEX = "hub.json"
LOCK_FILE = ".lock"
INCOMING = ".incoming"
#: A removed item stays restorable (Undo) this long, then is purged.
UNDO_S = 10 * 60
#: A removed or expired id is remembered (never reused; its reference says
#: "removed") this long.
TOMBSTONE_S = 30 * 86400
#: An unfinished upload left in ``.incoming`` is cleared after this long.
INCOMING_TTL_S = 3600
NAME_MAX = 120
NOTE_MAX = 500
#: The text of a snippet / a textual file shown inline (list preview, MCP).
PREVIEW_CHARS = 400
INLINE_TEXT_MAX = 16 * 1024
#: A text snippet's ceiling (a bigger text is a file).
TEXT_MAX = 1024 * 1024
#: Ids: lower-case letters and digits a person can read back unambiguously
#: (no 0/o, 1/l/i).
ALPHABET = "abcdefghjkmnpqrstuvwxyz23456789"  # pragma: allowlist secret
ID_LEN = 4
_ID_RE = re.compile(r"^[a-z2-9]{4,8}$")
#: ``hub:<id>`` in prompt text: not glued to a word or path before it
#: (``github:x``, ``a/hub:x``), not already expanded.
HANDLE_RE = re.compile(
    r"(?<![A-Za-z0-9_./:@-])hub:([A-Za-z0-9]{4,8})(?![A-Za-z0-9_])(?! \(MindFlock hub)"
)
PREFIX = "hub:"

#: Only these are ever served with their own content type (and drawn as a
#: thumbnail); everything else is a download. Checked by magic bytes, not
#: by the name.
IMAGE_TYPES = {
    "png": "image/png",
    "jpeg": "image/jpeg",
    "gif": "image/gif",
    "webp": "image/webp",
}
#: Names read as text for a preview (when the bytes are UTF-8 too).
_TEXT_EXT = frozenset(
    "txt log md markdown json jsonl yaml yml toml ini cfg conf csv tsv xml "
    "py js ts tsx jsx mjs cjs go rs java kt c h cc cpp hpp cs rb php sh bash "
    "zsh ps1 sql diff patch rst tex css scss html htm svg vue swift dart "
    "lua r pl gradle properties env sample example".split()
)

#: Windows: never a file name, with or without an extension.
_RESERVED = re.compile(r"^(con|prn|aux|nul|com[0-9¹²³]|lpt[0-9¹²³])(\..*)?$", re.I)
_BAD_NAME = re.compile("[\x00-\x1f\x7f-\x9f​-‏ -‮⁠-⁯﻿￹-￻" '/\\\\:*?"<>|]')

_LOCK = threading.RLock()
#: In-flight adds: token -> reserved bytes (this process).
_PENDING: Dict[str, int] = {}
#: Items being copied out: id -> count (never expired or purged under it).
_BUSY: Dict[str, int] = {}
#: Called (no arguments) after any change — server.py emits an event.
_LISTENERS: List[Callable[[], None]] = []


# --------------------------------------------------------------------------- #
# where, and the limits
# --------------------------------------------------------------------------- #
def root() -> str:
    """``<mindflock home>/hub``."""
    return os.path.join(config.GetConfigDir(), "hub")


def limits() -> dict:
    """The hub's limits (``hub.*``, docs/configuration.md)."""
    from backend.config import settings as _settings

    try:
        h = _settings.load_settings().hub
    except Exception:  # noqa: BLE001 — the defaults, never a crash
        h = _settings.HubSettings()
    mb = 1024 * 1024
    return {
        "quota_bytes": (h.max_mb or _settings.HUB_MAX_MB) * mb,
        "item_bytes": (h.item_mb or _settings.HUB_ITEM_MB) * mb,
        "max_items": h.max_items or _settings.HUB_MAX_ITEMS,
        "keep_days": h.keep_days or _settings.HUB_KEEP_DAYS,
    }


def human(n: int) -> str:
    return _send_files.human(n)


def on_change(fn: Callable[[], None]) -> None:
    if fn not in _LISTENERS:
        _LISTENERS.append(fn)


def _changed() -> None:
    for fn in list(_LISTENERS):
        try:
            fn()
        except Exception:  # noqa: BLE001 — a listener never breaks a change
            pass


def _ensure_root() -> str:
    """The hub folder, made if missing; refused if it (or the lock) is a
    symlink or not a folder."""
    r = root()
    try:
        st = os.lstat(r)
    except FileNotFoundError:
        os.makedirs(r, mode=0o755, exist_ok=True)
        st = os.lstat(r)
    if not stat.S_ISDIR(st.st_mode):
        raise Refusal(
            500,
            "hub_broken",
            "%s is not a plain folder (a link or a file) — the hub will not use "
            "it; move it aside and try again" % r,
        )
    return r


@contextlib.contextmanager
def _locked() -> Iterator[None]:
    """This process's lock, plus a file lock every server sharing the home
    honours (POSIX)."""
    with _LOCK:
        r = _ensure_root()
        fd = None
        if _fcntl is not None:
            fd = os.open(
                os.path.join(r, LOCK_FILE),
                os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0),
                0o644,
            )
            _fcntl.flock(fd, _fcntl.LOCK_EX)
        try:
            yield
        finally:
            if fd is not None:
                try:
                    _fcntl.flock(fd, _fcntl.LOCK_UN)
                finally:
                    os.close(fd)


# --------------------------------------------------------------------------- #
# the index
# --------------------------------------------------------------------------- #
def _load() -> dict:
    doc = read_json(os.path.join(root(), INDEX), {}, expect=dict) or {}
    items = doc.get("items")
    gone = doc.get("gone")
    return {
        "v": 1,
        "items": {
            k: v
            for k, v in (items.items() if isinstance(items, dict) else [])
            if isinstance(v, dict) and _ID_RE.match(str(k))
        },
        "gone": {
            k: v
            for k, v in (gone.items() if isinstance(gone, dict) else [])
            if _ID_RE.match(str(k)) and isinstance(v, (int, float))
        },
    }


def _save(doc: dict) -> None:
    atomic_write_json(os.path.join(root(), INDEX), doc, mode=0o644)


def _live(doc: dict) -> List[dict]:
    return [r for r in doc["items"].values() if not r.get("removed")]


def _used(doc: dict) -> Tuple[int, int]:
    """(bytes, count) of every item still on disk — removed ones awaiting
    Undo count against the quota (they are still there)."""
    recs = list(doc["items"].values())
    return sum(int(r.get("size") or 0) for r in recs), len(_live(doc))


def check_id(raw) -> str:
    """A well-formed item id from ``ab12`` / ``hub:ab12`` / ``HUB:AB12``, or a
    :class:`Refusal` (404: nothing could be called that)."""
    v = str(raw or "").strip()
    if v.lower().startswith(PREFIX):
        v = v[len(PREFIX) :]
    v = v.lower()
    if not _ID_RE.match(v):
        raise Refusal(
            404,
            "not_found",
            "%s is not a hub reference — they look like hub:k7m2 (see the Hub "
            "panel, or mindflock hub ls)" % clean_line(raw, 40),
        )
    return v


def _not_found(iid: str, doc: dict) -> Refusal:
    if iid in doc["gone"] or doc["items"].get(iid, {}).get("removed"):
        return Refusal(
            410,
            "removed",
            "hub:%s was removed from the hub (or expired) — add the file again" % iid,
        )
    return Refusal(
        404,
        "not_found",
        "there is no hub:%s in the hub — check the reference in the Hub panel "
        "(or mindflock hub ls)" % iid,
    )


def path_of(rec: dict) -> str:
    return os.path.join(root(), rec["id"], rec["name"])


def public(rec: dict, lim: Optional[dict] = None) -> dict:
    """An item as the API, CLI and MCP show it."""
    lim = lim or limits()
    out = {
        k: rec.get(k)
        for k in (
            "id",
            "name",
            "kind",
            "mime",
            "size",
            "sha256",
            "by",
            "by_name",
            "added",
            "used",
            "note",
            "pinned",
            "textual",
            "preview",
        )
    }
    out["ref"] = PREFIX + rec["id"]
    out["path"] = path_of(rec)
    out["expires"] = (
        None
        if rec.get("pinned")
        else float(rec.get("used") or rec.get("added") or 0) + lim["keep_days"] * 86400
    )
    if rec.get("removed"):
        out["removed"] = rec["removed"]
    return out


def list_items(*, include_removed: bool = False) -> dict:
    """``{items: [newest first], used, quota, count, max_items, item_bytes,
    keep_days, root}``."""
    lim = limits()
    doc = _load()
    recs = list(doc["items"].values()) if include_removed else _live(doc)
    recs.sort(key=lambda r: (-(float(r.get("added") or 0)), r.get("id", "")))
    used, count = _used(doc)
    return {
        "items": [public(r, lim) for r in recs],
        "used": used,
        "count": count,
        "quota": lim["quota_bytes"],
        "max_items": lim["max_items"],
        "item_bytes": lim["item_bytes"],
        "keep_days": lim["keep_days"],
        "root": root(),
    }


def get(raw_id) -> dict:
    """The item's record (public form), or a :class:`Refusal` (404 / 410)."""
    iid = check_id(raw_id)
    doc = _load()
    rec = doc["items"].get(iid)
    if rec is None or rec.get("removed"):
        raise _not_found(iid, doc)
    return public(rec)


def _rec(doc: dict, iid: str) -> dict:
    rec = doc["items"].get(iid)
    if rec is None or rec.get("removed"):
        raise _not_found(iid, doc)
    return rec


# --------------------------------------------------------------------------- #
# names and kinds
# --------------------------------------------------------------------------- #
def clean_name(raw, fallback: str = "item") -> str:
    """A safe file name for an item: the last part of a path, no
    control/bidi/invisible characters or Windows-illegal characters (each
    becomes ``-``), no leading dots, no trailing dots or spaces, not a reserved
    Windows name, at most :data:`NAME_MAX` characters (the extension kept)."""
    v = unicodedata.normalize("NFC", str(raw if raw is not None else ""))
    # A path (``C:\\fakepath\\shot.png``, ``../../x``) names its last part.
    v = re.split(r"[/\\]", v)[-1]
    v = _BAD_NAME.sub("-", v)
    v = re.sub(r"\s+", " ", v).strip()
    v = v.lstrip(". ").rstrip(". ")
    v = re.sub(r"-{2,}", "-", v)
    if not v or set(v) <= {"-", "_", " "}:
        v = fallback
    if _RESERVED.match(v):
        v = "_" + v
    if len(v) > NAME_MAX:
        stem, ext = os.path.splitext(v)
        ext = ext if 0 < len(ext) <= 16 else ""
        v = stem[: NAME_MAX - len(ext)].rstrip(". ") + ext
    if v.lower() in (".git", INDEX, LOCK_FILE, INCOMING):
        v = "_" + v
    return v


def _sniff_image(head: bytes) -> str:
    """The allow-listed image type of these first bytes, or ``""``."""
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return IMAGE_TYPES["png"]
    if head.startswith(b"\xff\xd8\xff"):
        return IMAGE_TYPES["jpeg"]
    if head[:6] in (b"GIF87a", b"GIF89a"):
        return IMAGE_TYPES["gif"]
    if len(head) >= 12 and head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return IMAGE_TYPES["webp"]
    return ""


def _text_preview(head: bytes, name: str, size: int, snippet: bool) -> Tuple[bool, str]:
    """(textual, preview) for an item whose first bytes are ``head``."""
    ext = os.path.splitext(name)[1].lower().lstrip(".")
    if not snippet and ext not in _TEXT_EXT and ext:
        return False, ""
    if b"\x00" in head:
        return False, ""
    try:
        txt = head.decode("utf-8")
    except UnicodeDecodeError as e:
        # A multi-byte character cut by the head's end is still text.
        if e.start < len(head) - 4 or size <= len(head):
            return False, ""
        txt = head[: e.start].decode("utf-8", "replace")
    return True, clean_text(txt[:PREVIEW_CHARS], PREVIEW_CHARS)


def _snippet_name(text: str) -> str:
    first = next((ln.strip() for ln in text.splitlines() if ln.strip()), "")
    words = re.sub(r"[^\w\- ]+", " ", first, flags=re.UNICODE).split()
    stem = "-".join(words[:6])[:40].strip("-_ ").lower()
    return (stem or "snippet") + ".txt"


# --------------------------------------------------------------------------- #
# adding
# --------------------------------------------------------------------------- #
def _new_id(doc: dict) -> str:
    taken = set(doc["items"]) | set(doc["gone"])
    try:
        taken |= set(os.listdir(root()))
    except OSError:
        pass
    for length in (ID_LEN, ID_LEN, ID_LEN, ID_LEN + 1, ID_LEN + 2):
        for _ in range(40):
            iid = "".join(secrets.choice(ALPHABET) for _ in range(length))
            if iid not in taken:
                return iid
    raise Refusal(500, "no_id", "could not pick a free hub id — try again")


def _room(doc: dict, more: int, lim: dict, *, mine: str = "") -> None:
    """Refuse an add of ``more`` bytes the quota, item limit or count can't
    take (counting the other adds in flight)."""
    used, count = _used(doc)
    pend = sum(v for k, v in _PENDING.items() if k != mine)
    npend = sum(1 for k in _PENDING if k != mine)
    if more > lim["item_bytes"]:
        raise Refusal(
            413,
            "too_large",
            "that is %s — one hub item can be at most %s (raise hub.item_mb in "
            "Settings, or share a smaller file)"
            % (human(more), human(lim["item_bytes"])),
        )
    if count + npend + 1 > lim["max_items"]:
        raise Refusal(
            507,
            "hub_full",
            "the hub already holds %d items, its limit — remove some (or unpin "
            "old ones) in the Hub panel, then add again" % lim["max_items"],
        )
    if used + pend + more > lim["quota_bytes"]:
        raise Refusal(
            507,
            "hub_full",
            "the hub has %s free and that is %s — remove some items in the Hub "
            "panel (or raise hub.max_mb in Settings), then add again"
            % (human(max(lim["quota_bytes"] - used - pend, 0)), human(more)),
        )


def _incoming_path(token: str) -> str:
    return os.path.join(root(), INCOMING, token)


class _Add:
    """One add in flight: a reservation under the lock, bytes streamed to
    ``.incoming/<token>`` (capped), then :meth:`commit`."""

    def __init__(self, declared: Optional[int], lim: dict) -> None:
        self.lim = lim
        self.declared = declared
        self.token = secrets.token_hex(8)
        self.size = 0
        self.head = b""
        self.h = hashlib.sha256()
        with _locked():
            doc = _load()
            reserve = declared if declared is not None else 0
            _room(doc, reserve, lim)
            used, _ = _used(doc)
            pend = sum(_PENDING.values())
            # Without a declared size the stream may use what is left.
            self.cap = (
                declared
                if declared is not None
                else max(min(lim["item_bytes"], lim["quota_bytes"] - used - pend), 0)
            )
            _PENDING[self.token] = self.cap
            inc = os.path.join(root(), INCOMING)
            os.makedirs(inc, mode=0o755, exist_ok=True)
            if os.path.islink(inc):
                _PENDING.pop(self.token, None)
                raise Refusal(500, "hub_broken", "the hub's incoming folder is a link")
            self.fd = os.open(
                _incoming_path(self.token),
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
                0o644,
            )

    def write(self, data: bytes) -> None:
        if not data:
            return
        if self.size + len(data) > self.cap:
            over = self.size + len(data)
            self.abort()
            if self.declared is not None and over <= self.lim["item_bytes"]:
                raise Refusal(
                    400,
                    "incomplete",
                    "the upload was bigger than it said it would be — add it again",
                )
            if over > self.lim["item_bytes"]:
                raise Refusal(
                    413,
                    "too_large",
                    "that is over %s, the most one hub item can be (raise "
                    "hub.item_mb in Settings, or share a smaller file)"
                    % human(self.lim["item_bytes"]),
                )
            raise Refusal(
                507,
                "hub_full",
                "the hub ran out of room while adding that — remove some items "
                "in the Hub panel (or raise hub.max_mb in Settings), then add again",
            )
        if len(self.head) < 8192:
            self.head += data[: 8192 - len(self.head)]
        self.h.update(data)
        view = memoryview(data)
        while view:
            n = os.write(self.fd, view)
            view = view[n:]
        self.size += len(data)

    def abort(self) -> None:
        with _LOCK:
            _PENDING.pop(self.token, None)
        if self.fd is not None:
            try:
                os.close(self.fd)
            except OSError:
                pass
            self.fd = None
        try:
            os.unlink(_incoming_path(self.token))
        except OSError:
            pass

    def commit(
        self,
        name: str,
        *,
        by: str,
        by_name: str = "",
        note: str = "",
        snippet: bool = False,
        pinned: bool = False,
    ) -> dict:
        try:
            os.fchmod(self.fd, 0o644)
        except (AttributeError, OSError):
            pass
        os.close(self.fd)
        self.fd = None
        mime = "" if snippet else _sniff_image(self.head)
        textual, preview = (
            (False, "") if mime else _text_preview(self.head, name, self.size, snippet)
        )
        kind = "text" if snippet else ("image" if mime else "file")
        fallback = {"image": "image", "text": "snippet.txt"}.get(kind, "file")
        name = clean_name(name, fallback)
        try:
            with _locked():
                doc = _load()
                _room(doc, self.size, self.lim, mine=self.token)
                iid = _new_id(doc)
                folder = os.path.join(root(), iid)
                os.mkdir(folder, 0o755)
                os.replace(_incoming_path(self.token), os.path.join(folder, name))
                now = time.time()
                rec = {
                    "id": iid,
                    "name": name,
                    "kind": kind,
                    "mime": mime,
                    "size": self.size,
                    "sha256": self.h.hexdigest(),
                    "by": by,
                    "by_name": clean_line(by_name, 120) if by_name else "",
                    "added": now,
                    "used": now,
                    "note": clean_text(note, NOTE_MAX) if note else "",
                    "pinned": bool(pinned),
                    "textual": bool(textual),
                    "preview": preview,
                }
                doc["items"][iid] = rec
                _save(doc)
                _PENDING.pop(self.token, None)
        except BaseException:
            self.abort()
            raise
        _changed()
        return public(rec, self.lim)


def _by(by: str) -> str:
    return by if by in ("you", "phone", "session", "device") else "you"


def add_stream(
    chunks: Iterable[bytes],
    name: str,
    *,
    declared: Optional[int] = None,
    by: str = "you",
    by_name: str = "",
    note: str = "",
    snippet: bool = False,
    pinned: bool = False,
) -> dict:
    """Add one item from ``chunks`` (bytes). → its public record, or a
    :class:`Refusal` (nothing is left behind)."""
    lim = limits()
    if declared is not None and declared < 0:
        declared = None
    if declared is not None:
        # Refuse an oversized upload before reading a byte.
        with _locked():
            _room(_load(), declared, lim)
    add = _Add(declared, lim)
    try:
        for c in chunks:
            add.write(c)
    except BaseException:
        add.abort()
        raise
    if declared is not None and add.size != declared:
        add.abort()
        raise Refusal(
            400,
            "incomplete",
            "the upload ended after %s of %s — add it again"
            % (human(add.size), human(declared)),
        )
    return add.commit(
        name, by=_by(by), by_name=by_name, note=note, snippet=snippet, pinned=pinned
    )


async def add_stream_async(
    chunks, name: str, *, declared: Optional[int] = None, **kw
) -> dict:
    """:func:`add_stream` from an async byte iterator (a request body).
    File writes are small and local; the lock is held only to reserve and
    to commit."""
    lim = limits()
    if declared is not None and declared < 0:
        declared = None
    add = _Add(declared, lim)
    try:
        async for c in chunks:
            add.write(c)
    except BaseException:
        add.abort()
        raise
    if declared is not None and add.size != declared:
        add.abort()
        raise Refusal(
            400,
            "incomplete",
            "the upload ended after %s of %s — add it again"
            % (human(add.size), human(declared)),
        )
    by = kw.pop("by", "you")
    return add.commit(name, by=_by(by), **kw)


def add_text(
    text,
    *,
    name: str = "",
    by: str = "you",
    by_name: str = "",
    note: str = "",
    pinned: bool = False,
) -> dict:
    """A text snippet → its public record."""
    if not isinstance(text, str) or not text.strip():
        raise Refusal(400, "empty", "there is no text to add — type or paste some")
    data = text.replace("\r\n", "\n").encode("utf-8")
    if len(data) > TEXT_MAX:
        raise Refusal(
            413,
            "too_large",
            "that text is %s — a snippet can be at most %s; add it as a file "
            "instead" % (human(len(data)), human(TEXT_MAX)),
        )
    nm = name.strip() if isinstance(name, str) and name.strip() else _snippet_name(text)
    if not os.path.splitext(nm)[1]:
        nm += ".txt"
    return add_stream(
        [data],
        nm,
        declared=len(data),
        by=by,
        by_name=by_name,
        note=note,
        snippet=True,
        pinned=pinned,
    )


def secret_refusal(name: str) -> Optional[Refusal]:
    """A refusal for a name the device transfer's filter calls a secret or
    never-sent (an env file, key, credential, agent login), else None."""
    kind = _send_files.classify(name)
    if kind == "never":
        return Refusal(
            403,
            "never_shared",
            "%s is an agent login, git internals or a credential store — it never "
            "goes in the hub (every session can read the hub)" % clean_line(name, 120),
        )
    if kind == "secret":
        return Refusal(
            403,
            "secret",
            "%s looks like a secret (an env file, key or credential) — every "
            "session can read the hub, so it is not added. If you really mean "
            "to share it, add it yourself and confirm" % clean_line(name, 120),
        )
    return None


def add_from_worktree(
    wt: str,
    path: str,
    *,
    session: str,
    note: str = "",
    name: str = "",
    zone_hits: Callable[[str, List[str]], list] = lambda wt, rels: [],
) -> dict:
    """An agent's put: ONE regular file from inside the session's own
    worktree ``wt`` (resolved real path; never a link at any step), passing
    the transfer's secrets filter and the session's fence/red zones."""
    if not isinstance(path, str) or not path.strip() or "\x00" in path:
        raise Refusal(400, "bad_path", "path must name one file in your worktree")
    base = os.path.realpath(wt)
    cand = path if os.path.isabs(path) else os.path.join(base, path)
    real = os.path.realpath(cand)
    if real == base or not real.startswith(base + os.sep):
        raise Refusal(
            403,
            "outside_worktree",
            "%s is outside this session's worktree (%s) — only files inside it "
            "can go in the hub" % (clean_line(path, 200), base),
        )
    rel = os.path.relpath(real, base).replace(os.sep, "/")
    if not os.path.lexists(real):
        raise Refusal(
            404, "not_found", "%s does not exist in %s" % (clean_line(rel), base)
        )
    kind = _send_files.classify(rel, _send_files.baseline_paths(base))
    if kind == "never":
        raise Refusal(
            403,
            "never_shared",
            "%s is git internals, an agent login/config/transcript or a "
            "credential store — it never goes in the hub" % clean_line(rel),
        )
    if kind == "secret":
        raise Refusal(
            403,
            "secret",
            "%s looks like a secret (an env file, key or credential) and is never "
            "put in the hub by an agent — every session can read the hub"
            % clean_line(rel),
        )
    hits = zone_hits(base, [rel])
    if hits:
        raise Refusal(
            403,
            "zone",
            "%s is in a red zone or outside this session's fence — it is not put "
            "in the hub" % clean_line(rel),
        )
    try:
        st0 = os.lstat(real)
    except OSError:
        st0 = None
    if st0 is None or not stat.S_ISREG(st0.st_mode):
        # Checked BEFORE opening: a FIFO would block the open forever.
        raise Refusal(
            400,
            "not_a_file",
            "%s is not a regular file (a folder, link or device) — put one file; "
            "zip a folder first" % clean_line(rel),
        )
    try:
        fd = _send_files._open_under(base, rel)
    except OSError:
        raise Refusal(
            400,
            "not_a_file",
            "%s is not a regular file (a folder, link or device) — put one file; "
            "zip a folder first" % clean_line(rel),
        ) from None
    size = os.fstat(fd).st_size

    def chunks() -> Iterator[bytes]:
        with os.fdopen(fd, "rb") as f:
            while True:
                b = f.read(1024 * 1024)
                if not b:
                    return
                yield b

    try:
        return add_stream(
            chunks(),
            name or os.path.basename(rel),
            declared=size,
            by="session",
            by_name=session,
            note=note,
        )
    finally:
        try:
            os.close(fd)
        except OSError:
            pass


# --------------------------------------------------------------------------- #
# reading
# --------------------------------------------------------------------------- #
def open_item(raw_id) -> Tuple[dict, int]:
    """``(public record, read-only fd)`` of an item's data — opened without
    following a link, a regular file. The caller closes the fd."""
    rec = get(raw_id)
    folder = os.path.join(root(), rec["id"])
    try:
        # From the hub folder down, never through a link (the item's own
        # folder included).
        fd = _send_files._open_under(_ensure_root(), rec["id"] + "/" + rec["name"])
    except OSError:
        raise Refusal(
            410,
            "missing",
            "hub:%s's file is missing from %s — remove the item and add it again"
            % (rec["id"], folder),
        ) from None
    return rec, fd


def read_text(raw_id, limit: int = TEXT_MAX) -> dict:
    """``{text, truncated}`` of a textual item (a snippet or a text file)."""
    rec, fd = open_item(raw_id)
    with os.fdopen(fd, "rb") as f:
        data = f.read(limit + 1)
    if not rec.get("textual") and rec.get("kind") != "text":
        raise Refusal(
            415, "not_text", "hub:%s is not text — download it instead" % rec["id"]
        )
    trunc = len(data) > limit
    return {"text": data[:limit].decode("utf-8", "replace"), "truncated": trunc}


# --------------------------------------------------------------------------- #
# changing
# --------------------------------------------------------------------------- #
def update(raw_id, *, name=None, note=None, pinned=None) -> dict:
    """Rename / note / pin an item (owner). Renaming moves the file inside
    its own folder (the reference never changes)."""
    iid = check_id(raw_id)
    with _locked():
        doc = _load()
        rec = _rec(doc, iid)
        if name is not None:
            if not isinstance(name, str) or not name.strip():
                raise Refusal(400, "bad_name", "a name can't be empty")
            new = clean_name(name, rec["name"])
            if new != rec["name"]:
                folder = os.path.join(root(), iid)
                src, dst = os.path.join(folder, rec["name"]), os.path.join(folder, new)
                if os.path.lexists(dst) and os.path.normcase(dst) != os.path.normcase(
                    src
                ):
                    raise Refusal(409, "exists", "that name is taken")
                os.rename(src, dst)
                rec["name"] = new
        if note is not None:
            if not isinstance(note, str):
                raise Refusal(400, "bad_note", "a note is text")
            rec["note"] = clean_text(note, NOTE_MAX)
        if pinned is not None:
            rec["pinned"] = bool(pinned)
            rec["used"] = time.time()
        _save(doc)
    _changed()
    return public(rec)


def remove(raw_id, *, session: Optional[str] = None) -> dict:
    """Remove an item (kept :data:`UNDO_S` for :func:`restore`). With
    ``session`` (an agent): only an item that session added."""
    iid = check_id(raw_id)
    with _locked():
        doc = _load()
        rec = _rec(doc, iid)
        if session is not None and not (
            rec.get("by") == "session"
            and rec.get("by_name") == clean_line(session, 120)
        ):
            raise Refusal(
                403,
                "not_yours",
                "hub:%s was added by %s, not this session — an agent may remove "
                "only items it added (ask your user to remove it)" % (iid, _who(rec)),
            )
        rec["removed"] = time.time()
        _save(doc)
    _changed()
    return {"ok": True, "id": iid, "ref": PREFIX + iid, "undo_s": UNDO_S}


def restore(raw_id) -> dict:
    iid = check_id(raw_id)
    with _locked():
        doc = _load()
        rec = doc["items"].get(iid)
        if rec is None or not rec.get("removed"):
            if rec is not None:
                return public(rec)
            raise _not_found(iid, doc)
        if not os.path.isfile(os.path.join(root(), iid, rec["name"])):
            raise Refusal(410, "removed", "hub:%s is gone for good" % iid)
        rec.pop("removed", None)
        rec["used"] = time.time()
        _save(doc)
    _changed()
    return public(rec)


def _who(rec: dict) -> str:
    by = rec.get("by")
    if by == "session":
        return 'session "%s"' % clean_line(rec.get("by_name") or "?", 80)
    if by == "device":
        return 'device "%s"' % clean_line(rec.get("by_name") or "?", 80)
    return {"phone": "your phone"}.get(by, "you")


def touch(ids: Iterable[str]) -> None:
    """Mark items used now (their expiry starts again)."""
    ids = [i for i in ids if _ID_RE.match(i)]
    if not ids:
        return
    with _locked():
        doc = _load()
        now = time.time()
        hit = False
        for i in ids:
            rec = doc["items"].get(i)
            if rec is not None and not rec.get("removed"):
                rec["used"] = now
                hit = True
        if hit:
            _save(doc)


# --------------------------------------------------------------------------- #
# copying into a session's worktree
# --------------------------------------------------------------------------- #
def default_dest(rec: dict) -> str:
    return "hub/" + rec["name"]


def _check_dest(dest: str) -> List[str]:
    if not isinstance(dest, str) or not dest.strip() or "\x00" in dest:
        raise Refusal(400, "bad_path", "say where in the worktree, like hub/shot.png")
    d = dest.strip().replace("\\", "/")
    if d.startswith("/") or re.match(r"^[A-Za-z]:", d):
        raise Refusal(
            400,
            "bad_path",
            "%s: give a path inside the worktree (like hub/shot.png), not an "
            "absolute one" % clean_line(dest, 200),
        )
    parts = [p for p in d.split("/") if p]
    if not parts:
        raise Refusal(400, "bad_path", "say where in the worktree, like hub/shot.png")
    for p in parts:
        if (
            p in (".", "..")
            or p.lower() == ".git"
            or len(p) > 255
            or _BAD_NAME.search(p)
            or p != p.strip()
            or p.endswith(".")
            or _RESERVED.match(p)
        ):
            raise Refusal(
                400,
                "bad_path",
                "%s is not a plain path inside the worktree (no .., .git, or "
                "special characters)" % clean_line(dest, 200),
            )
    if _send_files.classify("/".join(parts)) == "never":
        raise Refusal(
            403,
            "never_shared",
            "%s is a place MindFlock never writes (git internals, agent config, "
            "credentials) — pick another path" % clean_line(dest, 200),
        )
    return parts


def free_dest(wt: str, dest: str) -> str:
    """``dest``, or the first ``name-2.ext`` / ``name-3.ext`` … not taken."""
    parts = _check_dest(dest)
    base = os.path.realpath(wt)
    stem, ext = os.path.splitext(parts[-1])
    for n in range(1, 1000):
        leaf = parts[-1] if n == 1 else "%s-%d%s" % (stem, n, ext)
        cand = os.path.join(base, *parts[:-1], leaf)
        if not os.path.lexists(cand):
            return "/".join(parts[:-1] + [leaf])
    return "/".join(parts)


def _open_parent(base: str, parts: List[str]) -> int:
    """An fd of ``base/parts[:-1]``, each folder made if missing and none
    followed through a symlink."""
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    odir = getattr(os, "O_DIRECTORY", 0)
    if os.open not in getattr(os, "supports_dir_fd", set()) or not nofollow:
        full = os.path.join(base, *parts[:-1])
        os.makedirs(full, exist_ok=True)
        if os.path.realpath(full) != os.path.normpath(full):
            raise OSError("a folder on the way is a link")
        return os.open(full, os.O_RDONLY)
    dirfd = os.open(base, os.O_RDONLY | odir)
    try:
        for name in parts[:-1]:
            try:
                os.mkdir(name, 0o755, dir_fd=dirfd)
            except FileExistsError:
                pass
            nxt = os.open(name, os.O_RDONLY | nofollow | odir, dir_fd=dirfd)
            os.close(dirfd)
            dirfd = nxt
    except BaseException:
        os.close(dirfd)
        raise
    return dirfd


def take(
    raw_id,
    wt: str,
    dest: Optional[str] = None,
    *,
    replace: bool = False,
    zone_hits: Optional[Callable[[str, List[str]], list]] = None,
) -> dict:
    """Copy an item INTO the worktree ``wt`` at ``dest`` (relative; default
    ``hub/<name>``). Never outside it, never through a link, never over an
    existing file unless ``replace`` (then said in the answer). With
    ``zone_hits`` (an agent's copy): refused in a red zone or outside its
    fence. → ``{ok, ref, path, abs, size, sha256, replaced}``."""
    iid = check_id(raw_id)
    with _locked():
        doc = _load()
        rec = dict(_rec(doc, iid))
        _BUSY[iid] = _BUSY.get(iid, 0) + 1
        doc["items"][iid]["used"] = time.time()
        _save(doc)
    try:
        dest = dest if isinstance(dest, str) and dest.strip() else default_dest(rec)
        parts = _check_dest(dest)
        rel = "/".join(parts)
        base = os.path.realpath(wt)
        if not os.path.isdir(base):
            raise Refusal(404, "no_worktree", "that session's folder %s is gone" % base)
        if zone_hits is not None and zone_hits(base, [rel]):
            raise Refusal(
                403,
                "zone",
                "%s is in a red zone or outside this session's fence — copy it "
                "somewhere inside your fence" % clean_line(rel),
            )
        full = os.path.join(base, *parts)
        existed = os.path.lexists(full)
        if existed and not replace:
            raise Refusal(
                409,
                "exists",
                "%s already exists in the worktree — pick another path (%s is "
                "free), or replace it on purpose"
                % (clean_line(rel), free_dest(wt, rel)),
                free=free_dest(wt, rel),
            )
        try:
            src_fd = _send_files._open_under(_ensure_root(), iid + "/" + rec["name"])
        except OSError:
            raise Refusal(
                410, "missing", "hub:%s's file is missing — add it again" % iid
            ) from None
        h = hashlib.sha256()
        size = 0
        tmp = ".%s.hub-%s" % (parts[-1][:60], secrets.token_hex(4))
        try:
            pfd = _open_parent(base, parts)
        except OSError:
            os.close(src_fd)
            raise Refusal(
                403,
                "bad_path",
                "a folder on the way to %s is a link or a file — pick another path"
                % clean_line(rel),
            ) from None
        try:
            if existed:
                try:
                    st = os.stat(parts[-1], dir_fd=pfd, follow_symlinks=False)
                except (TypeError, NotImplementedError):
                    st = os.lstat(full)
                if not stat.S_ISREG(st.st_mode):
                    raise Refusal(
                        409,
                        "exists",
                        "%s exists and is not a plain file — pick another path"
                        % clean_line(rel),
                    )
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
            out = os.open(tmp, flags, 0o644, dir_fd=pfd) if pfd is not None else None
            try:
                with os.fdopen(src_fd, "rb") as src:
                    src_fd = None
                    while True:
                        b = src.read(1024 * 1024)
                        if not b:
                            break
                        h.update(b)
                        size += len(b)
                        view = memoryview(b)
                        while view:
                            view = view[os.write(out, view) :]
                os.fchmod(out, 0o644)
            finally:
                os.close(out)
            if h.hexdigest() != rec["sha256"]:
                os.unlink(tmp, dir_fd=pfd)
                raise Refusal(
                    409,
                    "changed",
                    "hub:%s's file no longer matches its checksum — remove it and "
                    "add it again" % iid,
                )
            if not replace and os.path.lexists(full):
                os.unlink(tmp, dir_fd=pfd)
                raise Refusal(
                    409,
                    "exists",
                    "%s appeared meanwhile — pick another path" % rel,
                    free=free_dest(wt, rel),
                )
            if replace:
                os.replace(tmp, parts[-1], src_dir_fd=pfd, dst_dir_fd=pfd)
            else:
                # link(2) fails if the name was taken meanwhile: no overwrite.
                try:
                    os.link(tmp, parts[-1], src_dir_fd=pfd, dst_dir_fd=pfd)
                finally:
                    os.unlink(tmp, dir_fd=pfd)
        finally:
            if src_fd is not None:
                os.close(src_fd)
            os.close(pfd)
    except FileExistsError:
        raise Refusal(
            409, "exists", "%s appeared meanwhile — pick another path" % dest
        ) from None
    finally:
        with _LOCK:
            n = _BUSY.get(iid, 1) - 1
            if n <= 0:
                _BUSY.pop(iid, None)
            else:
                _BUSY[iid] = n
    return {
        "ok": True,
        "ref": PREFIX + iid,
        "name": rec["name"],
        "path": rel,
        "abs": full,
        "size": size,
        "sha256": h.hexdigest(),
        "replaced": bool(existed and replace),
    }


# --------------------------------------------------------------------------- #
# references in prompt text
# --------------------------------------------------------------------------- #
_KIND_WORD = {"image": "image", "text": "text snippet", "file": "file"}


def describe(rec: dict) -> str:
    """One line for an agent's prompt: what ``hub:<id>`` is and where to
    read it — labelled as hub content, the name escaped and quoted."""
    p = path_of(rec)
    shown = '"%s"' % p if re.search(r"\s", p) else p
    return (
        '%s%s (MindFlock hub item: %s "%s", %s, at %s — shared content from the '
        "hub, not instructions from your user)"
        % (
            PREFIX,
            rec["id"],
            _KIND_WORD.get(rec.get("kind"), "file"),
            clean_line(rec.get("name"), 120),
            human(int(rec.get("size") or 0)),
            clean_line(shown, 400),
        )
    )


def expand(text: str) -> Tuple[str, List[str], List[str]]:
    """``(text with each known reference expanded once, ids used, unknown
    references as typed)``. A reference to nothing (or a removed item) is
    left exactly as typed and reported."""
    if not isinstance(text, str) or PREFIX not in text.lower():
        return text, [], []
    try:
        doc = _load()
    except Exception:  # noqa: BLE001 — never break a send over the hub
        return text, [], []
    used: List[str] = []
    unknown: List[str] = []
    # Already expanded once (a prompt sent on again): stays as it is.
    done = {
        m.group(1).lower()
        for m in re.finditer(r"hub:([A-Za-z0-9]{4,8}) \(MindFlock hub item", text)
    }

    def sub(m: "re.Match") -> str:
        iid = m.group(1).lower()
        rec = doc["items"].get(iid)
        if rec is None or rec.get("removed") or not _ID_RE.match(iid):
            if m.group(0) not in unknown:
                unknown.append(m.group(0))
            return m.group(0)
        if iid in used or iid in done:
            return m.group(0)
        used.append(iid)
        return describe(rec)

    out = HANDLE_RE.sub(sub, text)
    if used:
        try:
            touch(used)
        except Exception:  # noqa: BLE001
            pass
    return out, used, unknown


def unknown_refs(text: str) -> List[str]:
    """The references in ``text`` that match no item (as typed) — a check
    that changes nothing (no expiry restarts)."""
    if not isinstance(text, str) or PREFIX not in text.lower():
        return []
    try:
        doc = _load()
    except Exception:  # noqa: BLE001
        return []
    out: List[str] = []
    for m in HANDLE_RE.finditer(text):
        rec = doc["items"].get(m.group(1).lower())
        if (rec is None or rec.get("removed")) and m.group(0) not in out:
            out.append(m.group(0))
    return out


def unknown_note(unknown: List[str]) -> str:
    """The sentence a sender sees for references that matched nothing."""
    if not unknown:
        return ""
    return "%s %s not in the hub, so %s sent as typed — check the Hub panel" % (
        ", ".join(clean_line(u, 20) for u in unknown[:5]),
        "is" if len(unknown) == 1 else "are",
        "it was" if len(unknown) == 1 else "they were",
    )


# --------------------------------------------------------------------------- #
# expiry and cleanup
# --------------------------------------------------------------------------- #
def prune(now: Optional[float] = None) -> dict:
    """Expire unpinned items past ``keep_days`` since last use, purge removed
    items past the Undo window, clear stale unfinished uploads and forget old
    tombstones. Never an item being copied. → ``{expired, purged}``."""
    now = time.time() if now is None else now
    lim = limits()
    keep = lim["keep_days"] * 86400
    expired: List[str] = []
    purged: List[str] = []
    try:
        _ensure_root()
    except Refusal:
        return {"expired": [], "purged": []}
    with _locked():
        doc = _load()
        for iid, rec in list(doc["items"].items()):
            if _BUSY.get(iid):
                continue
            if rec.get("removed"):
                if now - float(rec["removed"]) >= UNDO_S:
                    purged.append(iid)
            elif (
                not rec.get("pinned")
                and now - float(rec.get("used") or rec.get("added") or 0) >= keep
            ):
                expired.append(iid)
        for iid in expired + purged:
            doc["items"].pop(iid, None)
            doc["gone"][iid] = now
        for iid, t in list(doc["gone"].items()):
            if now - float(t) >= TOMBSTONE_S:
                doc["gone"].pop(iid)
        if expired or purged:
            _save(doc)
        for iid in expired + purged:
            _rmtree(os.path.join(root(), iid))
        # Folders the index doesn't know (a crash between mkdir and save).
        known = set(doc["items"])
        for entry in _scandir(root()):
            if (
                _ID_RE.match(entry.name)
                and entry.name not in known
                and entry.is_dir(follow_symlinks=False)
                and now - entry.stat(follow_symlinks=False).st_mtime > INCOMING_TTL_S
            ):
                _rmtree(entry.path)
        for entry in _scandir(os.path.join(root(), INCOMING)):
            if entry.name in _PENDING:
                continue
            try:
                if now - entry.stat(follow_symlinks=False).st_mtime > INCOMING_TTL_S:
                    os.unlink(entry.path)
            except OSError:
                pass
    if expired or purged:
        _changed()
    return {"expired": expired, "purged": purged}


def _scandir(path: str) -> List["os.DirEntry"]:
    try:
        with os.scandir(path) as it:
            return list(it)
    except OSError:
        return []


def _rmtree(path: str) -> None:
    """Remove an item folder this code made — never following a link."""
    try:
        st = os.lstat(path)
    except OSError:
        return
    if stat.S_ISLNK(st.st_mode) or not stat.S_ISDIR(st.st_mode):
        try:
            os.unlink(path)
        except OSError:
            pass
        return
    for e in _scandir(path):
        if e.is_dir(follow_symlinks=False):
            _rmtree(e.path)
        else:
            try:
                os.unlink(e.path)
            except OSError:
                pass
    try:
        os.rmdir(path)
    except OSError:
        pass


def clear_unpinned() -> dict:
    """Disk cleanup: remove every unpinned item now (owner). → ``{removed,
    freed}``."""
    with _locked():
        doc = _load()
        now = time.time()
        gone = [
            i
            for i, r in doc["items"].items()
            if (r.get("removed") or not r.get("pinned")) and not _BUSY.get(i)
        ]
        freed = sum(int(doc["items"][i].get("size") or 0) for i in gone)
        for i in gone:
            doc["items"].pop(i, None)
            doc["gone"][i] = now
        _save(doc)
        for i in gone:
            _rmtree(os.path.join(root(), i))
    _changed()
    return {"removed": len(gone), "freed": freed}


def usage() -> dict:
    """``{bytes, items, pinned, path}`` for Disk cleanup."""
    doc = _load()
    used, count = _used(doc)
    return {
        "bytes": used,
        "items": count,
        "pinned": sum(1 for r in _live(doc) if r.get("pinned")),
        "path": root(),
    }


def rename_session(old: str, new: str) -> None:
    """A session renamed: the items it added say its new title."""
    if not os.path.isfile(os.path.join(root(), INDEX)):
        return
    old_c, new_c = clean_line(old, 120), clean_line(new, 120)
    with _locked():
        doc = _load()
        hit = False
        for rec in doc["items"].values():
            if rec.get("by") == "session" and rec.get("by_name") == old_c:
                rec["by_name"] = new_c
                hit = True
        if hit:
            _save(doc)
