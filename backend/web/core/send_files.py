"""Send files to another of your devices (``POST
/api/instances/{title}/send-files``; the MCP's ``send_files``, ``mindflock
send-files``; docs/devices.md "Send files to another device").

The sending half. The receiving half is :mod:`backend.web.core.inbox`; the
two speak over the SAME authenticated device link device forwarding and
``send_session`` use (:func:`remote.post_json` / :func:`remote.put_bytes`
with this device's fleet key or the token paired for that device) — no new
trust path, no third-party service.

1. **Pick** (:func:`collect`). Only paths inside the session's own worktree
   (resolved through symlinks; an escape is refused). Inside a folder:
   symlinks, special files and anything git ignores (``node_modules``,
   ``.venv``, build output) are skipped and listed. Never sent, whatever is
   passed: ``.git`` internals, agent logins/config/transcripts (``.claude``,
   ``.codex``, ``.aider*`` …), ``.ssh``/``.gnupg``/cloud credential folders,
   ``.netrc``/``.git-credentials``. Left out unless the caller passes
   ``include_secrets`` AND this device allows it (``transfers.
   allow_secrets``, off by default): ``.env*``, keys and certificates,
   credential files, and every file the repo's ``.secrets.baseline`` names.
   These name rules are a guardrail, not a content scan. If git can't say
   what it ignores, nothing is sent (fail closed; paths are given to git
   literally). Every file is opened folder by folder without following a
   symlink at any step (:func:`_open_under`). Paths in a red zone or
   outside the session's fence are refused. Size and count are capped
   (``transfers.max_mb`` / ``max_files``). Uncommitted files may go — the
   result lists them.
2. **Send** (:func:`transfer`). A manifest (paths, sizes, SHA-256 each and a
   SHA-256 over the lot) opens the transfer; each file follows in chunks of
   :data:`CHUNK` bytes, each at an offset the receiver confirms (a dropped
   chunk is retried once from what arrived; anything else cancels the
   transfer, which removes it there). ``finish`` verifies every checksum on
   the far side before the files appear in its inbox.
3. **Then** (:func:`send`): with ``to_session`` that session there gets a
   short message saying what arrived and where; with a ``note`` and no
   ``to_session`` a session is started there IN the inbox folder with the
   note as its task; with neither the files just land. The answer says
   where they landed, with their checksums.
"""

from __future__ import annotations

import fnmatch
import hashlib
import json
import os
import stat
import subprocess
import urllib.parse
from typing import Callable, Dict, List, Optional, Tuple

from backend.web.core import remote as _remote
from backend.web.core import send_session as _send_session
from backend.web.core.send_session import Refusal

#: Bytes per chunk on the wire (the receiver takes up to twice this).
CHUNK = 4 * 1024 * 1024
BEGIN_TIMEOUT_S = 30.0
CHUNK_TIMEOUT_S = 120.0
#: ``finish`` verifies every checksum (and unpacks a bundle) there.
FINISH_TIMEOUT_S = 300.0
MAX_PATHS = 100
NAMED = 20

# --------------------------------------------------------------------------- #
# what never travels, and what travels only on purpose
# --------------------------------------------------------------------------- #
#: Folders never sent: git internals, agent CLIs' logins/config/transcripts,
#: MindFlock's own state, and the usual credential stores.
NEVER_DIRS = frozenset(
    {
        ".git",
        ".claude",
        ".codex",
        ".gemini",
        ".goose",
        ".opencode",
        ".cline",
        ".copilot",
        ".cursor",
        ".continue",
        ".mindflock",
        ".ssh",
        ".gnupg",
        ".aws",
        ".azure",
        ".kube",
        ".docker",
        ".gcloud",
    }
)
#: Files never sent (lower-cased base names / patterns).
NEVER_FILES = (
    ".git",
    ".netrc",
    "_netrc",
    ".git-credentials",
    ".credentials.json",
    "oauth_creds.json",
    ".aider*",
    "claude.json",
    ".claude.json",
)
#: Files left out unless secrets are allowed (lower-cased patterns).
SECRET_FILES = (
    ".env",
    ".env.*",
    "*.env",
    ".envrc",
    "*.pem",
    "*.key",
    "*.p12",
    "*.pfx",
    "*.jks",
    "*.keystore",
    "*.kdbx",
    "*.ppk",
    "id_rsa*",
    "id_dsa*",
    "id_ecdsa*",
    "id_ed25519*",
    "credentials",
    "credentials.*",
    "*.credentials",
    "*credentials*.json",
    "client_secret*.json",
    "service-account*.json",
    "secrets.*",
    "secret.*",
    ".npmrc",
    ".pypirc",
    ".htpasswd",
    "*.tfvars",
    "*.tfstate",
    "*.tfstate.*",
)
#: Harmless look-alikes of a secret name: kept. Only what is plainly a
#: placeholder (``.env.example``) or a public key — ``.dist``, ``.template``
#: and ``.defaults`` copies often hold the real values, so they count as
#: secrets. A name is only a guardrail: content is never inspected.
_SAFE_SUFFIXES = (".example", ".sample", ".pub")
#: Copy suffixes: ``server.key.template`` / ``.env.dist`` / ``id_rsa.bak``
#: are judged by the name without them.
_COPY_SUFFIXES = (".dist", ".template", ".tmpl", ".defaults", ".bak", ".orig", ".old")


def _match(name: str, patterns) -> bool:
    return any(fnmatch.fnmatchcase(name, p) for p in patterns)


def classify(rel: str, extra: frozenset = frozenset()) -> Optional[str]:
    """``"never"`` / ``"secret"`` / None for a worktree-relative path.
    ``extra``: repo-specific secret paths (``.secrets.baseline``)."""
    parts = [p for p in rel.replace("\\", "/").split("/") if p not in ("", ".")]
    if not parts:
        return None
    low = [p.lower() for p in parts]
    if any(p in NEVER_DIRS for p in low[:-1]) or low[-1] in NEVER_DIRS:
        return "never"
    if any(p.startswith(".aider") for p in low):
        return "never"
    base = low[-1]
    if _match(base, NEVER_FILES):
        return "never"
    if "/".join(parts) in extra:
        return "secret"
    if base.endswith(_SAFE_SUFFIXES):
        return None
    names = [base] + [base[: -len(x)] for x in _COPY_SUFFIXES if base.endswith(x)]
    if any(_match(n, SECRET_FILES) for n in names if n):
        return "secret"
    return None


def baseline_paths(root: str) -> frozenset:
    """The files the repo's detect-secrets baseline names (its known
    secret-looking strings): repo-relative paths."""
    try:
        with open(os.path.join(root, ".secrets.baseline"), encoding="utf-8") as f:
            doc = json.load(f)
    except (OSError, ValueError):
        return frozenset()
    res = doc.get("results") if isinstance(doc, dict) else None
    if not isinstance(res, dict):
        return frozenset()
    return frozenset(str(k).replace("\\", "/") for k in res)


# --------------------------------------------------------------------------- #
# settings
# --------------------------------------------------------------------------- #
def limits() -> dict:
    """This device's transfer limits (``transfers.*``, docs/configuration.md)."""
    from backend.config import settings as _settings

    try:
        t = _settings.load_settings().transfers
    except Exception:  # noqa: BLE001 — the defaults, never a crash
        t = _settings.TransfersSettings()
    return {
        "max_bytes": (t.max_mb or _settings.TRANSFER_MAX_MB) * 1024 * 1024,
        "max_files": t.max_files or _settings.TRANSFER_MAX_FILES,
        "inbox_bytes": (t.inbox_mb or _settings.TRANSFER_INBOX_MB) * 1024 * 1024,
        "keep_days": t.keep_days or _settings.TRANSFER_KEEP_DAYS,
        "allow_secrets": bool(t.allow_secrets),
    }


def human(n: int) -> str:
    size = float(n)
    for unit in ("bytes", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return (
                ("%d %s" % (size, unit))
                if unit == "bytes"
                else "%.1f %s" % (size, unit)
            )
        size /= 1024
    return "%d bytes" % n


# --------------------------------------------------------------------------- #
# picking the files
# --------------------------------------------------------------------------- #
def _git_lines(root: str, *args: str) -> Optional[List[str]]:
    from backend import gitutil

    pre, env_extra = gitutil.untrusted_guard(root)
    try:
        cp = subprocess.run(
            # --literal-pathspecs: a folder named ":x" is a path, not magic.
            ["git", "--literal-pathspecs", *pre, "-C", root, *args],
            capture_output=True,
            timeout=30,
            env=dict(
                os.environ, GIT_TERMINAL_PROMPT="0", GIT_OPTIONAL_LOCKS="0", **env_extra
            ),
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if cp.returncode != 0:
        return None
    return [x for x in cp.stdout.decode("utf-8", "surrogateescape").split("\x00") if x]


def _open_under(root: str, rel: str) -> int:
    """A read-only fd for ``root/rel`` that never follows a symlink at ANY
    component: each folder is opened relative to the one before it with
    ``O_NOFOLLOW|O_DIRECTORY`` (a parent swapped for a link between the walk
    and the read fails instead of escaping the worktree). Where ``dir_fd``
    is not supported (Windows) the opened file's real path must still be
    ``root/rel``. → the fd of a regular file, or ``OSError``."""
    parts = [p for p in rel.split("/") if p]
    if not parts or any(p in (".", "..") for p in parts):
        raise OSError("not a path inside the worktree: %r" % rel)
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    if os.open in getattr(os, "supports_dir_fd", set()) and nofollow:
        dirfd = os.open(root, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            for name in parts[:-1]:
                nxt = os.open(
                    name,
                    os.O_RDONLY | nofollow | getattr(os, "O_DIRECTORY", 0),
                    dir_fd=dirfd,
                )
                os.close(dirfd)
                dirfd = nxt
            fd = os.open(parts[-1], os.O_RDONLY | nofollow, dir_fd=dirfd)
        finally:
            os.close(dirfd)
    else:
        full = os.path.join(root, *parts)
        fd = os.open(full, os.O_RDONLY | nofollow)
        if os.path.realpath(full) != os.path.normpath(full):
            os.close(fd)
            raise OSError("%s now leads outside the worktree" % rel)
    if not stat.S_ISREG(os.fstat(fd).st_mode):
        os.close(fd)
        raise OSError("%s is no longer a regular file" % rel)
    return fd


def _open_source(src) -> int:
    """``src``: a ``(root, rel)`` pair (a worktree file: :func:`_open_under`)
    or a plain path MindFlock made itself (a bundle in a temp folder)."""
    if isinstance(src, tuple):
        return _open_under(*src)
    return os.open(src, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))


def _sha256(path) -> Tuple[str, int]:
    h = hashlib.sha256()
    n = 0
    fd = _open_source(path)
    with os.fdopen(fd, "rb") as f:
        while True:
            block = f.read(1024 * 1024)
            if not block:
                break
            h.update(block)
            n += len(block)
    return h.hexdigest(), n


def total_sha256(files: List[dict]) -> str:
    """The whole transfer's checksum: SHA-256 over ``"<sha256> <size>
    <path>\\n"`` per file, in manifest order — both sides compute it."""
    h = hashlib.sha256()
    for f in files:
        h.update(("%s %d %s\n" % (f["sha256"], f["size"], f["path"])).encode("utf-8"))
    return h.hexdigest()


def collect(
    wt: str,
    paths: List[str],
    *,
    secrets_ok: bool = False,
    max_files: int = 5000,
    max_bytes: int = 512 * 1024 * 1024,
) -> dict:
    """What would travel → ``{root, files: [{path, abs, size, sha256,
    exec}], skipped: [{path, why}], uncommitted: [path], total}``, or a
    :class:`Refusal`. Blocking (walks and hashes)."""
    root = os.path.realpath(wt)
    extra = baseline_paths(root)
    is_git = _git_lines(root, "rev-parse", "--show-toplevel") is not None
    picked: Dict[str, dict] = {}
    skipped: List[dict] = []
    seen_skip: set = set()

    def skip(rel: str, why: str) -> None:
        if rel not in seen_skip:
            seen_skip.add(rel)
            skipped.append({"path": rel, "why": why})

    def gate(rel: str, named: bool) -> bool:
        kind = classify(rel, extra)
        if kind == "never":
            if named:
                raise Refusal(
                    403,
                    "never_sent",
                    "%s is never sent: git internals, agent logins, config and "
                    "transcripts, and credential stores stay on this device" % rel,
                )
            skip(rel, "never sent (git internals, agent login/config or credentials)")
            return False
        if kind == "secret" and not secrets_ok:
            if named:
                raise Refusal(
                    403,
                    "secret",
                    "%s looks like a secret (an env file, key or credential) and "
                    "is not sent by default — leave it out, or pass "
                    "include_secrets=true once the owner allows it (Settings: "
                    "transfers.allow_secrets on this device)" % rel,
                )
            skip(rel, "looks like a secret")
            return False
        return True

    def add(rel: str, full: str) -> None:
        if rel in picked:
            return
        if len(picked) >= max_files:
            raise Refusal(
                413,
                "too_many_files",
                "that is more than %d files — send a narrower folder, or raise "
                "transfers.max_files in Settings" % max_files,
            )
        st = os.lstat(full)
        picked[rel] = {
            "path": rel,
            "abs": full,
            "size": st.st_size,
            "exec": bool(st.st_mode & 0o111),
        }

    for raw in paths:
        if not isinstance(raw, str) or not raw.strip() or "\x00" in raw:
            raise Refusal(400, "bad_path", "every path must be a non-empty string")
        cand = raw if os.path.isabs(raw) else os.path.join(root, raw)
        real = os.path.realpath(cand)
        if real != root and not real.startswith(root + os.sep):
            raise Refusal(
                403,
                "outside_worktree",
                "%s is outside this session's worktree (%s) — only files inside "
                "it can be sent" % (raw, root),
            )
        if not os.path.lexists(real):
            raise Refusal(404, "not_found", "%s does not exist in %s" % (raw, root))
        rel = os.path.relpath(real, root).replace(os.sep, "/")
        st = os.lstat(real)
        if stat.S_ISREG(st.st_mode):
            if rel == "." or not gate(rel, True):
                continue
            add(rel, real)
            continue
        if not stat.S_ISDIR(st.st_mode):
            raise Refusal(400, "not_a_file", "%s is not a regular file or folder" % raw)
        if rel != "." and not gate(rel, True):
            continue
        ignored: set = set()
        if is_git:
            spec = [] if rel == "." else ["--", rel]
            rows = _git_lines(
                root,
                "ls-files",
                "-z",
                "--others",
                "--ignored",
                "--exclude-standard",
                "--directory",
                *spec,
            )
            if rows is None:
                # Fail closed: without git's answer every ignored file
                # (node_modules, .venv, build output, a local .env.*) would go.
                raise Refusal(
                    500,
                    "git_failed",
                    "could not ask git which files in %s it ignores (git "
                    "failed or timed out) — nothing was sent; send again, or "
                    "name the files" % raw,
                )
            ignored = set(rows)
        for dirpath, dirnames, filenames in os.walk(real, followlinks=False):
            drel = os.path.relpath(dirpath, root).replace(os.sep, "/")
            drel = "" if drel == "." else drel + "/"
            keep_dirs = []
            for d in sorted(dirnames):
                r = drel + d
                full = os.path.join(dirpath, d)
                if os.path.islink(full):
                    skip(r, "symlink")
                elif not gate(r, False):
                    pass
                elif r + "/" in ignored:
                    skip(r + "/", "ignored by git")
                else:
                    keep_dirs.append(d)
            dirnames[:] = keep_dirs
            for fn in sorted(filenames):
                r = drel + fn
                full = os.path.join(dirpath, fn)
                st = os.lstat(full)
                if stat.S_ISLNK(st.st_mode):
                    skip(r, "symlink")
                elif not stat.S_ISREG(st.st_mode):
                    skip(r, "not a regular file")
                elif not gate(r, False):
                    pass
                elif r in ignored:
                    skip(r, "ignored by git")
                else:
                    add(r, full)

    files = sorted(picked.values(), key=lambda f: f["path"])
    total = sum(f["size"] for f in files)
    if total > max_bytes:
        raise Refusal(
            413,
            "too_large",
            "that is %s, over the %s limit for one transfer — send a narrower "
            "folder, or raise transfers.max_mb in Settings"
            % (human(total), human(max_bytes)),
        )
    for f in files:
        try:
            f["sha256"], f["size"] = _sha256((root, f["path"]))
        except OSError as err:
            raise Refusal(
                409,
                "changed",
                "%s changed while it was being read (%s) — nothing was sent; "
                "send again" % (f["path"], err),
            ) from None
    uncommitted: List[str] = []
    if is_git and files:
        rows = _git_lines(
            root, "status", "--porcelain=v1", "-z", "--untracked-files=all"
        )
        dirty = set()
        it = iter(rows or [])
        for rec in it:
            code, path = rec[:2], rec[3:]
            dirty.add(path)
            if code[0] in "RC":
                next(it, None)  # the rename's source path
        uncommitted = [f["path"] for f in files if f["path"] in dirty]
    return {
        "root": root,
        "files": files,
        "skipped": skipped,
        "uncommitted": uncommitted,
        "total": sum(f["size"] for f in files),
        "git": is_git,
    }


# --------------------------------------------------------------------------- #
# the wire
# --------------------------------------------------------------------------- #
def _device_error(key: str, status: int, body, what: str, after: str = "") -> Refusal:
    if status == 0:
        return _send_session._unreachable(key, after)
    if status in (401, 403) and not (isinstance(body, dict) and body.get("code")):
        return _send_session._auth_refusal(key, status)
    if isinstance(body, dict) and body.get("code"):
        msg = str(body.get("error") or body.get("detail") or "refused")
        extra = {k: v for k, v in body.items() if k not in ("error", "code")}
        return Refusal(
            502 if status >= 500 else (409 if status < 400 else status),
            str(body["code"]),
            "%s refused %s: %s" % (key, what, msg),
            **extra,
        )
    if status == 404 or status == 405:
        return Refusal(
            502,
            "device_too_old",
            "%s's MindFlock cannot receive files yet — update it there "
            "(mindflock update), then send again" % key,
        )
    return Refusal(
        502,
        "device_refused",
        "%s refused %s (HTTP %d)" % (key, what, status),
    )


async def transfer(dev: dict, manifest: dict, sources: List[str], *, read=None) -> dict:
    """Open a transfer on ``dev``, stream each file of ``manifest["files"]``
    from ``sources`` (same order), finish it → the receiver's answer (``{id,
    path, files, total_sha256, …}``), or a :class:`Refusal` (the transfer is
    cancelled there first)."""
    import asyncio

    key = dev["key"]
    status, body = await _remote.post_json(
        dev, "/api/transfers", manifest, BEGIN_TIMEOUT_S
    )
    if status not in (200, 201) or not isinstance(body, dict) or not body.get("id"):
        raise _device_error(key, status, body, "the transfer")
    tid = str(body["id"])
    base = "/api/transfers/" + urllib.parse.quote(tid, safe="")
    reader = read or (lambda p, off, n: _read_chunk(p, off, n))

    async def cancel() -> None:
        try:
            await _remote.post_json(dev, base + "/cancel", {}, 15.0)
        except Exception:  # noqa: BLE001
            pass

    try:
        for i, (f, src) in enumerate(zip(manifest["files"], sources)):
            off, retried = 0, False
            while off < f["size"]:
                try:
                    data = await asyncio.to_thread(
                        reader, src, off, min(CHUNK, f["size"] - off)
                    )
                except OSError as err:
                    raise Refusal(
                        409,
                        "changed",
                        "%s changed while it was being sent (%s) — send again"
                        % (f["path"], err),
                    ) from None
                if not data:
                    raise Refusal(
                        409,
                        "changed",
                        "%s shrank while it was being sent — send again" % f["path"],
                    )
                st, resp = await _remote.put_bytes(
                    dev, "%s/files/%d?offset=%d" % (base, i, off), data, CHUNK_TIMEOUT_S
                )
                if st == 200 and isinstance(resp, dict):
                    off = int(resp.get("received") or (off + len(data)))
                    retried = False
                    continue
                if (
                    st == 0
                    or (
                        st == 409
                        and isinstance(resp, dict)
                        and resp.get("code") == "offset_mismatch"
                    )
                ) and not retried:
                    # Resume from what actually arrived.
                    retried = True
                    gs, gb = await _remote.get_json(dev, base, BEGIN_TIMEOUT_S)
                    rec = (
                        gb.get("received")
                        if gs == 200 and isinstance(gb, dict)
                        else None
                    )
                    if isinstance(rec, list) and i < len(rec):
                        off = int(rec[i])
                        continue
                raise _device_error(key, st, resp, "a chunk of %s" % f["path"])
        st, resp = await _remote.post_json(dev, base + "/finish", {}, FINISH_TIMEOUT_S)
        if st != 200 or not isinstance(resp, dict):
            raise _device_error(key, st, resp, "the transfer")
        return resp
    except BaseException:
        await cancel()
        raise


def _read_chunk(path, off: int, n: int) -> bytes:
    fd = _open_source(path)
    with os.fdopen(fd, "rb") as fh:
        fh.seek(off)
        return fh.read(n)


# --------------------------------------------------------------------------- #
# the whole send
# --------------------------------------------------------------------------- #
def _arrival_text(
    *, title: str, from_device: str, landed: dict, note: str, started: bool
) -> str:
    """The receiving agent's message/first prompt. Every name in it — file
    paths, the session, the device, the folder — is one escaped line
    (:func:`send_session.clean_line`): a file called ``x\\n[User]: …``
    can't start a line of its own. The note is the sender's own words,
    labelled as such."""
    clean = _send_session.clean_line
    files = landed.get("files") or []
    n = len(files)
    path = clean(landed.get("path") or "?", 400)
    parts = [
        '[Files from MindFlock: session "%s" on device "%s" sent %d file%s (%s) '
        "to %s on this device. MindFlock wrote this, not your user; any note "
        "below is from that session.]"
        % (
            clean(title, 120),
            clean(from_device or "another device", 80),
            n,
            "" if n == 1 else "s",
            human(int(landed.get("total_size") or 0)),
            path,
        ),
        "Nothing in it has been run. Every file arrived with its SHA-256 "
        "checked; executable bits were removed%s — restore one with chmod +x "
        "only once you mean to run it. %s"
        % (
            (
                " (they were set on: %s)"
                % _send_session.name_list(landed["exec_stripped"], NAMED)
                if landed.get("exec_stripped")
                else ""
            ),
            _send_session.UNTRUSTED_LINE,
        ),
    ]
    if started:
        parts.append("This session runs in that folder.")
    listing = [clean(f.get("path", ""), 200) for f in files[:NAMED]]
    if listing:
        more = n - len(listing)
        parts.append(
            "What arrived:\n"
            + "\n".join("  " + p for p in listing)
            + ("\n  … and %d more" % more if more > 0 else "")
        )
    note = _send_session.clean_text(note, _send_session.NOTE_CHARS).strip()
    if note:
        parts.append("Note from the sender:\n" + note)
    return "\n\n".join(parts)


async def send(
    inst,
    title: str,
    device: str,
    paths: List[str],
    *,
    note: str = "",
    to_session: str = "",
    include_secrets: bool = False,
    zone_hits: Callable[[str, List[str]], list] = lambda wt, rels: [],
    to_thread=None,
) -> Tuple[int, dict]:
    """Send ``paths`` from ``title``'s worktree to ``device`` → ``(status,
    body)``. ``zone_hits(wt, rels)`` is the server's red-zone/fence check of
    the chosen paths (injected, so this module never reaches into it)."""
    import asyncio

    run = to_thread or asyncio.to_thread
    try:
        if not isinstance(paths, list) or not paths:
            raise Refusal(400, "bad_request", "name at least one path to send")
        if len(paths) > MAX_PATHS:
            raise Refusal(
                400,
                "bad_request",
                "at most %d paths per send (name folders)" % MAX_PATHS,
            )
        dev = _send_session.resolve_device(device)
        wt = ""
        try:
            wt = inst.GetWorktreePath() or ""
        except Exception:  # noqa: BLE001
            wt = ""
        if not wt or not os.path.isdir(wt):
            raise Refusal(
                409,
                "no_workspace",
                "%s has no workspace on disk to send from (it may still be "
                "starting) — try again once it is running" % title,
            )
        lim = limits()
        if include_secrets and not lim["allow_secrets"]:
            raise Refusal(
                403,
                "secrets_not_allowed",
                "include_secrets is off on this device: the owner must turn on "
                "transfers.allow_secrets in Settings first — or send without "
                "the secret files; nothing was sent",
            )
        plan = await run(
            collect,
            wt,
            paths,
            secrets_ok=bool(include_secrets and lim["allow_secrets"]),
            max_files=lim["max_files"],
            max_bytes=lim["max_bytes"],
        )
        if not plan["files"]:
            raise Refusal(
                400,
                "nothing_to_send",
                (
                    "nothing to send: every file there was left out (%s)"
                    % "; ".join(
                        "%s: %s" % (s["path"], s["why"]) for s in plan["skipped"][:6]
                    )
                    if plan["skipped"]
                    else "nothing to send: those folders are empty"
                ),
                skipped=plan["skipped"][:50],
            )
        try:
            hits = await run(
                zone_hits, plan["root"], [f["path"] for f in plan["files"]]
            )
        except Exception:  # noqa: BLE001 — the gate fails open on its own bugs
            hits = []
        if hits:
            first = hits[0]
            raise Refusal(
                409,
                "red_zone",
                "%s is %s for this session%s — those files are not sent; "
                "nothing was sent"
                % (
                    first.get("path"),
                    (
                        "outside the fence"
                        if first.get("kind") == "green"
                        else "in a red zone (%s)" % first.get("pattern")
                    ),
                    " (and %d more)" % (len(hits) - 1) if len(hits) > 1 else "",
                ),
                red_zone_breaches=hits[:50],
            )
        me = str(_remote.self_identity().get("key") or "")
        wire = [
            {
                "path": f["path"],
                "size": f["size"],
                "sha256": f["sha256"],
                "exec": f["exec"],
            }
            for f in plan["files"]
        ]
        manifest = {
            "v": 1,
            "kind": "files",
            "from_device": me,
            "from_session": title,
            "note": (note or "")[: _send_session.NOTE_CHARS],
            "to_session": to_session or "",
            "files": wire,
            "total_size": plan["total"],
            "total_sha256": total_sha256(wire),
        }
        landed = await transfer(
            dev, manifest, [(plan["root"], f["path"]) for f in plan["files"]]
        )
        landed.setdefault("total_size", plan["total"])
        after = "The files ARE in %s's inbox at %s" % (dev["key"], landed.get("path"))
        started_title = ""
        messaged = ""
        caveats: List[str] = []
        if to_session:
            text = _arrival_text(
                title=title, from_device=me, landed=landed, note=note, started=False
            )
            st, resp = await _remote.post_json(
                dev,
                "/api/instances/%s/messages" % urllib.parse.quote(to_session, safe=""),
                {"text": text, "delivery": "auto"},
                20.0,
            )
            if st in (200, 201):
                messaged = to_session
            else:
                caveats.append(
                    "%s could not be told (%s); the files are there all the same"
                    % (
                        to_session,
                        (
                            (resp or {}).get("error")
                            if isinstance(resp, dict)
                            else "HTTP %d" % st
                        ),
                    )
                )
        elif (note or "").strip():
            body: Dict[str, object] = {
                "repo_path": landed.get("path"),
                "in_place": True,
                "prompt": _arrival_text(
                    title=title, from_device=me, landed=landed, note=note, started=True
                ),
                "spawned": True,
            }
            program = str(getattr(inst, "Program", "") or "")
            if program:
                body["program"] = program
            started_title, resp = await _send_session._create(dev, title, body, after)
            await _remote.refresh_instances(dev["key"])
            if resp.get("queued"):
                caveats.append(
                    "%s is short on memory: the session is queued and starts "
                    "when there is room" % dev["key"]
                )
    except Refusal as r:
        return r.status, r.body()
    out = {
        "ok": True,
        "device": dev["key"],
        "transfer_id": landed.get("id"),
        "path": landed.get("path"),
        "files": [
            {"path": f["path"], "size": f["size"], "sha256": f["sha256"]} for f in wire
        ],
        "total_size": plan["total"],
        "total_sha256": manifest["total_sha256"],
        "exec_stripped": landed.get("exec_stripped") or [],
        "uncommitted": plan["uncommitted"],
        "skipped": plan["skipped"][:100],
        "caveats": caveats,
    }
    if messaged:
        out["messaged"] = _remote.join_title(dev["key"], messaged)
    if started_title:
        out["title"] = _remote.join_title(dev["key"], started_title)
    return 200, out
