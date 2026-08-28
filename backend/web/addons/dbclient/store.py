"""Connection-profile store for the Database Client extension.

Profiles live in their OWN file — ``~/.mindflock/dbclient.json`` (override
``$MINDFLOCK_DBCLIENT_FILE`` for tests and ad-hoc scripts) — never in
settings.json and never through ``/api/settings``: the namespaced-everything
rule says an extension's persistence is its own affair, and this file carries
database passwords, so it gets the same treatment ``save_settings`` gives the
settings store (mode 0600, atomic tempfile + ``os.replace``).

Shape::

    {"connections": [{id, name, engine, host, port, user, password,
                      database, file, read_only}]}

Reads mask ``password`` with :data:`SECRET_MASK`; a write carrying the mask (or
"") keeps the stored password, matched by ``id`` — the auth-profiles precedent.
A NEW id with a masked/empty password is refused for password-bearing engines
(there is nothing to keep), naming the field so the UI can point at it.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import tempfile
from pathlib import Path
from typing import Dict, List, Optional

from backend.config.config import GetConfigDir

from ..base import SECRET_MASK

#: File name under the config dir (``~/.mindflock/``).
STORE_FILE_NAME = "dbclient.json"

#: Engines the adapters module knows. Kept here too so a profile is validated
#: at write time, not first at connect time.
ENGINES = ("sqlite", "postgres", "mysql")

#: Engines whose profiles carry a password (sqlite is a file).
PASSWORD_ENGINES = ("postgres", "mysql")

#: Default ports, filled in when a profile omits one.
DEFAULT_PORTS = {"postgres": 5432, "mysql": 3306}

#: Every field a profile carries, in serialization order.
PROFILE_FIELDS = (
    "id",
    "name",
    "engine",
    "host",
    "port",
    "user",
    "password",
    "database",
    "file",
    "read_only",
)

#: Profile ids double as URL segments (``/api/dbclient/connections/<id>``) and
#: cache keys, so they stay to a URL-safe slug.
_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")


class ProfileError(ValueError):
    """A profile failed validation. ``field`` names the offending field so the
    UI can highlight it; ``str(err)`` is the human message."""

    def __init__(self, field: str, message: str) -> None:
        super().__init__(message)
        self.field = field


def store_path() -> Path:
    """Path to ``dbclient.json`` — ``$MINDFLOCK_DBCLIENT_FILE`` when set (tests
    MUST set it, alongside ``MINDFLOCK_SETTINGS_FILE``), else the config dir."""
    env = os.environ.get("MINDFLOCK_DBCLIENT_FILE")
    if env:
        return Path(env)
    return Path(GetConfigDir()) / STORE_FILE_NAME


def load_profiles() -> List[dict]:
    """Every stored profile (passwords in the clear — callers mask). Never
    raises: a missing, unreadable or corrupt file reads as no profiles, the
    same contract ``load_settings`` gives the settings store."""
    try:
        parsed = json.loads(store_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    if not isinstance(parsed, dict):
        return []
    raw = parsed.get("connections")
    if not isinstance(raw, list):
        return []
    out: List[dict] = []
    for item in raw:
        if isinstance(item, dict) and str(item.get("id", "") or ""):
            out.append(_complete(item))
    return out


def save_profiles(profiles: List[dict]) -> None:
    """Persist the whole list atomically with owner-only permissions (the
    ``save_settings`` recipe: 0700 dir, 0600 temp file in the same dir, fsync,
    ``os.replace`` so a concurrent reader never sees a partial file)."""
    path = store_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(path.parent, 0o700)
    except OSError:
        pass  # best-effort (e.g. a tmp dir we don't own)

    data = (
        json.dumps(
            {"connections": [_complete(p) for p in profiles]},
            indent=2,
            ensure_ascii=False,
        )
        + "\n"
    )
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".dbclient.", suffix=".tmp")
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def get_profile(profile_id: str) -> Optional[dict]:
    """The stored profile with ``id == profile_id`` (clear password), or None."""
    for p in load_profiles():
        if p["id"] == profile_id:
            return p
    return None


def masked(profile: dict) -> dict:
    """The read view: the password replaced by the mask sentinel (or "" when
    none is stored) so a GET never transmits the credential."""
    view = dict(profile)
    view["password"] = SECRET_MASK if profile.get("password") else ""
    return view


def fingerprint(profile: dict) -> str:
    """Stable hash of the whole profile (password included). Connection-cache
    keys carry it so an out-of-band edit to dbclient.json — or a saved change
    the cache did not see — simply misses the cache instead of reusing a
    connection built from stale credentials."""
    canon = json.dumps(_complete(profile), sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(canon.encode("utf-8")).hexdigest()[:16]


def normalize_profile(raw: dict, existing: Optional[dict]) -> dict:
    """Validate + complete an incoming profile against the stored one with the
    same id (``existing``; None for a new id). Raises :class:`ProfileError`
    naming the bad field. EVERYTHING is validated before the caller writes."""
    if not isinstance(raw, dict):
        raise ProfileError("body", "expected a connection object")
    pid = str(raw.get("id", "") or "").strip()
    if not pid:
        # A fresh id for a profile the UI created without one. Random rather
        # than name-derived: names are free text and get renamed.
        pid = "c-" + secrets.token_hex(4)
    if not _ID_RE.match(pid):
        raise ProfileError("id", "id '%s' must be letters/digits/-/_ (max 64)" % pid)
    engine = str(raw.get("engine", "") or "").strip().lower()
    if engine not in ENGINES:
        raise ProfileError(
            "engine",
            "unknown engine '%s' (expected one of %s)" % (engine, ", ".join(ENGINES)),
        )
    name = str(raw.get("name", "") or "").strip() or pid
    host = str(raw.get("host", "") or "").strip()
    user = str(raw.get("user", "") or "").strip()
    database = str(raw.get("database", "") or "").strip()
    file = str(raw.get("file", "") or "").strip()

    port_raw = raw.get("port", None)
    port: Optional[int] = None
    if port_raw not in (None, ""):
        try:
            port = int(port_raw)
        except (TypeError, ValueError):
            raise ProfileError("port", "port must be a number")
        if not 1 <= port <= 65535:
            raise ProfileError("port", "port must be between 1 and 65535")
    elif engine in DEFAULT_PORTS:
        port = DEFAULT_PORTS[engine]

    if engine == "sqlite":
        if not file:
            raise ProfileError("file", "sqlite connections need a database file path")
        password = ""
    else:
        if not host:
            host = "localhost"
        password = str(raw.get("password", "") or "")
        if password in ("", SECRET_MASK):
            # The keep-secret rule resolves by id, so a new id has nothing to
            # keep — silently saving a passwordless profile would only fail
            # later, at connect time, with a far less helpful error.
            if existing is None:
                raise ProfileError(
                    "password",
                    "password: enter the password for new connection '%s'" % name,
                )
            password = str(existing.get("password", "") or "")

    read_only_raw = raw.get("read_only", None)
    if read_only_raw is None:
        read_only = bool(existing.get("read_only", False)) if existing else False
    else:
        read_only = bool(read_only_raw)

    return {
        "id": pid,
        "name": name,
        "engine": engine,
        "host": host,
        "port": port,
        "user": user,
        "password": password,
        "database": database,
        "file": file,
        "read_only": read_only,
    }


def upsert_profile(clean: dict) -> List[dict]:
    """Insert or replace by id; returns the new full list (already saved)."""
    profiles = load_profiles()
    replaced = False
    for i, p in enumerate(profiles):
        if p["id"] == clean["id"]:
            profiles[i] = clean
            replaced = True
            break
    if not replaced:
        profiles.append(clean)
    save_profiles(profiles)
    return profiles


def delete_profile(profile_id: str) -> bool:
    """Remove a profile by id; False when there was none."""
    profiles = load_profiles()
    kept = [p for p in profiles if p["id"] != profile_id]
    if len(kept) == len(profiles):
        return False
    save_profiles(kept)
    return True


def _complete(item: dict) -> Dict[str, object]:
    """A profile dict with every field present (older files may lack some)."""
    out: Dict[str, object] = {}
    for key in PROFILE_FIELDS:
        if key == "read_only":
            out[key] = bool(item.get(key, False))
        elif key == "port":
            v = item.get(key)
            out[key] = (
                int(v)
                if isinstance(v, (int, float)) and not isinstance(v, bool)
                else None
            )
        else:
            v = item.get(key, "")
            out[key] = "" if v is None else str(v)
    return out
