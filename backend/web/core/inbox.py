"""Receiving files from another of your devices — the inbox (``/api/
transfers``; docs/devices.md "Send files to another device").

The receiving half of :mod:`backend.web.core.send_files` (and of
``send_session``'s git bundle, :mod:`backend.web.core.session_bundle`). A
transfer is untrusted input even between one person's own devices — the
receiving machine runs agents — so:

* **Who.** Only another MindFlock over the device link: the request carries
  the remote marker AND a valid credential (this device's token or the fleet
  key) — checked by the route even when the token gate is off — and "Allow
  remote control" must be on here. Guest links never reach it, and the
  gateway never forwards these routes (they are not on the ``fwd``
  allow-list nor under a session title).
* **Where.** Only inside ``<mindflock home>/inbox``: a transfer is staged
  in ``inbox/.partial/<id>/`` and, once every checksum holds, moved to
  ``inbox/<from-device>/<id>/`` — a NEW folder per transfer (an id is never
  reused; nothing is ever overwritten). Its record sits beside it,
  ``inbox/<from-device>/<id>.json``.
* **What.** A manifest of plain relative paths — no archive to unpack.
  Refused: absolute paths, ``..`` / ``.`` / empty segments, backslashes,
  drive letters, control characters, ``.git`` anywhere, duplicates (case-
  insensitively), a file that is also a folder, more files or bytes than this
  device's limits, more than the inbox quota. Files are only ever created
  here (``O_NOFOLLOW``, folders made by this code), so no symlink, hard link
  or device file can appear. Every file lands ``0644`` (no executable,
  setuid or setgid bit; the ones the sender had executable are recorded).
* **Checked.** SHA-256 per file and over the whole transfer
  (:func:`send_files.total_sha256`), at ``finish``; any mismatch drops the
  transfer.
* **Bundles** go through :func:`session_bundle.unpack` (verified, fetched
  into a new repository, hooks off) into ``inbox/<from-device>/<id>/<name>``.
* **Kept** for ``transfers.keep_days`` (14): older transfers no live session
  works in are removed on the next receive; an unfinished one after
  :data:`PARTIAL_TTL_S`.

Nothing here runs anything it received.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import shutil
import stat
import threading
import time
from typing import Callable, Dict, List, Optional

from backend.config import config
from backend.web.core import send_files as _send_files
from backend.web.core import session_bundle as _bundle
from backend.web.core.send_session import Refusal

#: A received chunk's ceiling (the sender sends :data:`send_files.CHUNK`).
CHUNK_MAX = 2 * _send_files.CHUNK
#: An unfinished transfer is dropped this long after it was opened.
PARTIAL_TTL_S = 6 * 3600
PATH_MAX = 1024
SEGMENT_MAX = 255
NOTE_MAX = 1500
RECORD = "transfer.json"

_ID_RE = re.compile(r"^\d{8}-\d{6}-[0-9a-f]{8}$")
_DEVICE_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")
_SHA_RE = re.compile(r"^[0-9a-f]{64}$")
_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$")
_BAD_CHARS = re.compile(r"[\x00-\x1f\x7f\\]")

_LOCK = threading.RLock()


def root() -> str:
    """``<mindflock home>/inbox``."""
    return os.path.join(config.GetConfigDir(), "inbox")


def _partial_dir(tid: str) -> str:
    return os.path.join(root(), ".partial", tid)


def device_label(raw: str) -> str:
    """The sending device's folder name: its MagicDNS label, else
    ``unknown-device``."""
    v = str(raw or "").strip().lower()
    return v if _DEVICE_RE.match(v) else "unknown-device"


# --------------------------------------------------------------------------- #
# manifest checks
# --------------------------------------------------------------------------- #
def check_path(p) -> str:
    """A safe relative path, or a :class:`Refusal`."""
    if not isinstance(p, str) or not p or len(p) > PATH_MAX:
        raise Refusal(
            400, "bad_manifest", "a path in the manifest is empty or too long"
        )
    if _BAD_CHARS.search(p) or p.startswith("/") or re.match(r"^[A-Za-z]:", p):
        raise Refusal(400, "bad_manifest", "%r is not a plain relative path" % p[:200])
    for seg in p.split("/"):
        if seg in ("", ".", "..") or len(seg) > SEGMENT_MAX:
            raise Refusal(
                400, "bad_manifest", "%r is not a plain relative path" % p[:200]
            )
        if seg.lower() == ".git" or seg.lower().startswith(".git/"):
            raise Refusal(400, "bad_manifest", "%r: .git is never received" % p[:200])
    return p


def check_manifest(m, lim: dict) -> dict:
    """The normalized manifest, or a :class:`Refusal`."""
    if not isinstance(m, dict) or m.get("v") != 1:
        raise Refusal(400, "bad_manifest", "not a transfer manifest this device reads")
    kind = m.get("kind")
    if kind not in ("files", "bundle"):
        raise Refusal(400, "bad_manifest", "unknown transfer kind %r" % (kind,))
    files = m.get("files")
    if not isinstance(files, list) or not files:
        raise Refusal(400, "bad_manifest", "a transfer carries at least one file")
    if len(files) > lim["max_files"]:
        raise Refusal(
            413,
            "too_many_files",
            "%d files is more than this device takes in one transfer (%d; "
            "transfers.max_files there)" % (len(files), lim["max_files"]),
        )
    out: List[dict] = []
    seen: Dict[str, str] = {}
    dirs: set = set()
    total = 0
    for f in files:
        if not isinstance(f, dict):
            raise Refusal(400, "bad_manifest", "a manifest entry is not an object")
        path = check_path(f.get("path"))
        size = f.get("size")
        sha = f.get("sha256")
        if not isinstance(size, int) or isinstance(size, bool) or size < 0:
            raise Refusal(400, "bad_manifest", "%s has no valid size" % path)
        if not isinstance(sha, str) or not _SHA_RE.match(sha):
            raise Refusal(400, "bad_manifest", "%s has no valid sha256" % path)
        low = path.lower()
        if low in seen:
            raise Refusal(400, "bad_manifest", "%s is in the manifest twice" % path)
        seen[low] = path
        parts = low.split("/")
        for i in range(1, len(parts)):
            dirs.add("/".join(parts[:i]))
        total += size
        out.append(
            {"path": path, "size": size, "sha256": sha, "exec": f.get("exec") is True}
        )
    clash = sorted(set(seen) & dirs)
    if clash:
        raise Refusal(
            400, "bad_manifest", "%s is both a file and a folder" % seen[clash[0]]
        )
    if total > lim["max_bytes"]:
        raise Refusal(
            413,
            "too_large",
            "%s is over this device's limit for one transfer (%s; "
            "transfers.max_mb there)"
            % (_send_files.human(total), _send_files.human(lim["max_bytes"])),
        )
    want = _send_files.total_sha256(out)
    if m.get("total_sha256") != want:
        raise Refusal(
            400,
            "checksum_mismatch",
            "the manifest's whole-transfer checksum does not match its files",
        )
    bundle = None
    if kind == "bundle":
        b = m.get("bundle")
        if (
            len(out) != 1
            or not out[0]["path"].endswith(".bundle")
            or "/" in out[0]["path"]
            or not isinstance(b, dict)
            or not isinstance(b.get("branch"), str)
            or not _bundle.valid_branch(b["branch"])
            or not isinstance(b.get("name"), str)
            or not _NAME_RE.match(b["name"])
        ):
            raise Refusal(400, "bad_manifest", "not a well-formed bundle transfer")
        bundle = {"branch": b["branch"], "name": b["name"]}
    note = m.get("note") if isinstance(m.get("note"), str) else ""
    return {
        "v": 1,
        "kind": kind,
        "files": out,
        "total_size": total,
        "total_sha256": want,
        "from_session": str(m.get("from_session") or "")[:200],
        "note": note[:NOTE_MAX],
        "to_session": str(m.get("to_session") or "")[:200],
        "bundle": bundle,
    }


# --------------------------------------------------------------------------- #
# housekeeping
# --------------------------------------------------------------------------- #
def _tree_size(path: str) -> int:
    n = 0
    for dirpath, _dirs, files in os.walk(path, followlinks=False):
        for fn in files:
            try:
                n += os.lstat(os.path.join(dirpath, fn)).st_size
            except OSError:
                pass
    return n


def usage() -> int:
    """Bytes the inbox holds now (partials included)."""
    return _tree_size(root()) if os.path.isdir(root()) else 0


def prune(
    keep_days: int, in_use: Callable[[str], bool] = lambda p: False, now=None
) -> List[str]:
    """Remove unfinished transfers older than :data:`PARTIAL_TTL_S` and
    received ones older than ``keep_days`` that no live session works in
    (``in_use(path)``). → the folders removed."""
    now = time.time() if now is None else now
    gone: List[str] = []
    base = root()
    part = os.path.join(base, ".partial")
    if os.path.isdir(part):
        for tid in os.listdir(part):
            p = os.path.join(part, tid)
            try:
                if now - os.lstat(p).st_mtime > PARTIAL_TTL_S:
                    shutil.rmtree(p, ignore_errors=True)
                    gone.append(p)
            except OSError:
                pass
    if not os.path.isdir(base):
        return gone
    for dev in os.listdir(base):
        dpath = os.path.join(base, dev)
        if dev.startswith(".") or not os.path.isdir(dpath) or os.path.islink(dpath):
            continue
        for tid in os.listdir(dpath):
            p = os.path.join(dpath, tid)
            if not _ID_RE.match(tid) or not os.path.isdir(p):
                continue
            try:
                age = now - os.lstat(p).st_mtime
            except OSError:
                continue
            if age > keep_days * 86400 and not in_use(p):
                shutil.rmtree(p, ignore_errors=True)
                try:
                    os.unlink(p + ".json")
                except OSError:
                    pass
                gone.append(p)
    return gone


def list_transfers() -> List[dict]:
    """Every received transfer's record, newest first."""
    out: List[dict] = []
    base = root()
    if not os.path.isdir(base):
        return out
    for dev in sorted(os.listdir(base)):
        dpath = os.path.join(base, dev)
        if dev.startswith(".") or not os.path.isdir(dpath):
            continue
        for name in os.listdir(dpath):
            if not name.endswith(".json") or not _ID_RE.match(name[:-5]):
                continue
            try:
                with open(os.path.join(dpath, name), encoding="utf-8") as f:
                    rec = json.load(f)
            except (OSError, ValueError):
                continue
            if isinstance(rec, dict):
                out.append(rec)
    out.sort(key=lambda r: str(r.get("id") or ""), reverse=True)
    return out


# --------------------------------------------------------------------------- #
# a transfer
# --------------------------------------------------------------------------- #
def _new_id() -> str:
    return time.strftime("%Y%m%d-%H%M%S", time.gmtime()) + "-" + secrets.token_hex(4)


def _load(tid: str) -> dict:
    if not isinstance(tid, str) or not _ID_RE.match(tid):
        raise Refusal(404, "no_transfer", "no such transfer")
    try:
        with open(os.path.join(_partial_dir(tid), RECORD), encoding="utf-8") as f:
            st = json.load(f)
    except (OSError, ValueError):
        raise Refusal(
            404,
            "no_transfer",
            "no transfer %s here (finished, cancelled or expired) — send again" % tid,
        ) from None
    return st


def _data_dir(tid: str) -> str:
    return os.path.join(_partial_dir(tid), "data")


def _target(tid: str, path: str) -> str:
    data = _data_dir(tid)
    full = os.path.join(data, *path.split("/"))
    real_parent = os.path.realpath(os.path.dirname(full))
    if real_parent != os.path.realpath(data) and not real_parent.startswith(
        os.path.realpath(data) + os.sep
    ):
        raise Refusal(400, "bad_manifest", "%s leaves the inbox" % path)
    return full


def _received(tid: str, st: dict) -> List[int]:
    out = []
    for f in st["files"]:
        full = os.path.join(_data_dir(tid), *f["path"].split("/"))
        try:
            s = os.lstat(full)
            out.append(s.st_size if stat.S_ISREG(s.st_mode) else 0)
        except OSError:
            out.append(0)
    return out


def begin(
    manifest,
    from_device: str,
    *,
    lim: dict,
    session_check: Callable[[str], Optional[str]] = lambda t: None,
    in_use: Callable[[str], bool] = lambda p: False,
) -> dict:
    """Open a transfer → ``{id, received}``. ``session_check(to_session)``
    → None when that session exists here, else the refusal sentence."""
    m = check_manifest(manifest, lim)
    if m["to_session"]:
        why = session_check(m["to_session"])
        if why:
            raise Refusal(404, "no_session", why)
    with _LOCK:
        prune(lim["keep_days"], in_use)
        used = usage()
        if used + m["total_size"] > lim["inbox_bytes"]:
            raise Refusal(
                507,
                "inbox_full",
                "the inbox here holds %s of its %s quota, too full for %s more — "
                "remove old transfers from %s there, or raise transfers.inbox_mb"
                % (
                    _send_files.human(used),
                    _send_files.human(lim["inbox_bytes"]),
                    _send_files.human(m["total_size"]),
                    root(),
                ),
            )
        tid = _new_id()
        part = _partial_dir(tid)
        os.makedirs(os.path.join(root(), ".partial"), mode=0o700, exist_ok=True)
        try:
            os.mkdir(part, 0o700)  # a fresh id: never reused, never merged into
        except FileExistsError:
            raise Refusal(
                409, "exists", "transfer %s already exists here — send again" % tid
            ) from None
        os.mkdir(_data_dir(tid), 0o700)
        m["id"] = tid
        m["from_device"] = device_label(from_device)
        m["opened"] = time.time()
        with open(os.path.join(part, RECORD), "w", encoding="utf-8") as f:
            json.dump(m, f)
    return {"id": tid, "received": [0] * len(m["files"])}


def status(tid: str) -> dict:
    with _LOCK:
        st = _load(tid)
        return {"id": tid, "received": _received(tid, st)}


def write_chunk(tid: str, index: int, offset: int, data: bytes) -> dict:
    """Append ``data`` to file ``index`` at ``offset`` (which must be what
    arrived so far) → ``{received}``."""
    if len(data) > CHUNK_MAX:
        raise Refusal(413, "chunk_too_large", "a chunk is at most %d bytes" % CHUNK_MAX)
    with _LOCK:
        st = _load(tid)
        files = st["files"]
        if not isinstance(index, int) or not 0 <= index < len(files):
            raise Refusal(404, "no_file", "no file %r in this transfer" % (index,))
        f = files[index]
        full = _target(tid, f["path"])
        try:
            s = os.lstat(full)
            if not stat.S_ISREG(s.st_mode):
                raise Refusal(
                    409, "bad_state", "%s is not a plain file here" % f["path"]
                )
            have = s.st_size
        except FileNotFoundError:
            have = 0
        if offset != have:
            raise Refusal(
                409,
                "offset_mismatch",
                "%s has %d bytes here, not %d" % (f["path"], have, offset),
                received=have,
            )
        if have + len(data) > f["size"]:
            raise Refusal(
                400, "too_long", "%s is longer than its manifest says" % f["path"]
            )
        os.makedirs(os.path.dirname(full), mode=0o700, exist_ok=True)
        _target(tid, f["path"])  # re-check after the folders exist
        flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(full, flags, 0o600)
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode) or os.fstat(fd).st_nlink != 1:
                raise Refusal(
                    409, "bad_state", "%s is not a plain file here" % f["path"]
                )
            os.write(fd, data)
        finally:
            os.close(fd)
        return {"received": have + len(data)}


def _sha(path: str) -> str:
    h = hashlib.sha256()
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    with os.fdopen(fd, "rb") as f:
        for block in iter(lambda: f.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def cancel(tid: str) -> dict:
    with _LOCK:
        _load(tid)
        shutil.rmtree(_partial_dir(tid), ignore_errors=True)
    return {"ok": True, "cancelled": tid}


def finish(tid: str) -> dict:
    """Verify every file and the whole transfer, then move it into the
    inbox (a bundle: unpack it) → ``{id, path, files, total_sha256,
    exec_stripped, …}``. A failed check drops the transfer."""
    with _LOCK:
        st = _load(tid)
        data = _data_dir(tid)
        try:
            got = []
            for f in st["files"]:
                full = _target(tid, f["path"])
                if f["size"] == 0 and not os.path.lexists(full):
                    os.makedirs(os.path.dirname(full), mode=0o700, exist_ok=True)
                    fd = os.open(full, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                    os.close(fd)
                s = os.lstat(full) if os.path.lexists(full) else None
                if s is None or not stat.S_ISREG(s.st_mode) or s.st_size != f["size"]:
                    raise Refusal(
                        409,
                        "incomplete",
                        "%s did not arrive whole (%d of %d bytes)"
                        % (f["path"], s.st_size if s else 0, f["size"]),
                    )
                sha = _sha(full)
                if sha != f["sha256"]:
                    raise Refusal(
                        422,
                        "checksum_mismatch",
                        "%s arrived with a different SHA-256 than was sent — the "
                        "transfer was dropped; send again" % f["path"],
                    )
                got.append(dict(f, sha256=sha))
            if _send_files.total_sha256(got) != st["total_sha256"]:
                raise Refusal(
                    422,
                    "checksum_mismatch",
                    "the transfer's whole checksum does not match — dropped; send again",
                )
            # Every file 0644, every folder 0755: no exec/setuid/setgid bit.
            exec_was = sorted(f["path"] for f in got if f.get("exec"))
            want = {os.path.join(data, *f["path"].split("/")) for f in got}
            for dirpath, dirnames, filenames in os.walk(data, followlinks=False):
                for name in dirnames + filenames:
                    full = os.path.join(dirpath, name)
                    s = os.lstat(full)
                    if stat.S_ISDIR(s.st_mode):
                        os.chmod(full, 0o755)
                    elif stat.S_ISREG(s.st_mode) and full in want and s.st_nlink == 1:
                        os.chmod(full, 0o644)
                    else:
                        raise Refusal(
                            409,
                            "bad_state",
                            "something other than the sent files appeared in the "
                            "transfer (%s) — dropped" % name,
                        )
            os.chmod(data, 0o755)
            dev_dir = os.path.join(root(), st["from_device"])
            os.makedirs(dev_dir, mode=0o755, exist_ok=True)
            if os.path.islink(dev_dir):
                raise Refusal(
                    409, "bad_state", "the inbox folder for this device is a link"
                )
            dest = os.path.join(dev_dir, tid)
            if os.path.lexists(dest):
                raise Refusal(
                    409, "exists", "transfer %s is already in the inbox" % tid
                )
            os.rename(data, dest)
        except Refusal:
            shutil.rmtree(_partial_dir(tid), ignore_errors=True)
            raise
        rec = {
            "id": tid,
            "kind": st["kind"],
            "from_device": st["from_device"],
            "from_session": st.get("from_session") or "",
            "note": st.get("note") or "",
            "received": time.time(),
            "path": dest,
            "files": [
                {"path": f["path"], "size": f["size"], "sha256": f["sha256"]}
                for f in got
            ],
            "total_size": st["total_size"],
            "total_sha256": st["total_sha256"],
            "exec_stripped": exec_was,
        }
        if st["kind"] == "bundle":
            b = st["bundle"]
            bfile = os.path.join(dest, got[0]["path"])
            try:
                res = _bundle.unpack(bfile, os.path.join(dest, b["name"]), b["branch"])
            except _bundle.BundleError as err:
                shutil.rmtree(dest, ignore_errors=True)
                shutil.rmtree(_partial_dir(tid), ignore_errors=True)
                raise Refusal(422, err.code, err.message) from None
            os.unlink(bfile)
            rec.update(
                path=res["path"],
                transfer_path=dest,
                branch=res["branch"],
                head=res["head"],
                exec_stripped=res["exec_stripped"],
            )
        with open(dest + ".json", "w", encoding="utf-8") as f:
            json.dump(rec, f, indent=1)
        shutil.rmtree(_partial_dir(tid), ignore_errors=True)
        return rec
