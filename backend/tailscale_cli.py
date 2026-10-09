"""One answer to "where is Tailscale, and is it actually working here?".

Every place that talks to the ``tailscale`` CLI goes through this module:
remote discovery, the shared phone link, phone URLs, tailnet trust, the
doctor, the caps gate and the peer invite host. Before it, each of them ran
``shutil.which("tailscale")`` and its own ``status --json``, so the two
common installs that put nothing called ``tailscale`` on PATH read as
"not installed" everywhere:

* the macOS app (App Store or Standalone), whose CLI is the app binary
  itself — ``/Applications/Tailscale.app/Contents/MacOS/Tailscale``, run with
  ``TAILSCALE_BE_CLI=1`` (Tailscale KB 1080);
* Windows Tailscale seen from WSL, where only ``tailscale.exe`` exists.

The second one is reported, never used: the Windows client is a different
node (its own name and IPs, on the Windows side of WSL's NAT), so answering
"who am I on the tailnet" with it would hand out addresses nothing listens
on. MindFlock in WSL needs Tailscale inside WSL — :func:`health` says so.

Resolution order (:func:`resolve`): ``$MINDFLOCK_TAILSCALE_BIN`` → ``PATH``
→ the macOS app bundle; on WSL, a Windows ``tailscale.exe`` only when none of
those exists (``kind="wsl-windows-host"``, ``usable=False``).

:func:`status_json` is ``tailscale status --json`` cached for
:data:`CACHE_TTL` seconds and shared by every caller (one subprocess per
window, whoever asks first; concurrent askers wait for it). :func:`health`
reads that snapshot into what a person needs to know: signed in or not, to
which tailnet, as which device, MagicDNS/HTTPS, key expiry, and the one next
step for whatever is wrong (``issues``). The doctor, ``GET
/api/tailscale/health`` and Settings → Devices' "Tailscale on this device"
card all render the same list. :func:`start_login` is the one write: it
starts ``tailscale login`` (or ``up``) in the background and hands back the
sign-in URL; it never waits for the person to finish.

Stdlib only, never raises from the read side.
"""

from __future__ import annotations

import copy
import datetime as _dt
import json
import os
import re
import shutil
import subprocess
import threading
import time
from dataclasses import asdict, dataclass
from typing import List, Optional

from backend import osenv

#: An explicit CLI path (or name on PATH) that wins over discovery.
BIN_ENV = "MINDFLOCK_TAILSCALE_BIN"

#: Test/sandbox hook: a ``tailscale status --json`` document read instead of
#: running the CLI — how an end-to-end test stands up several servers on one
#: machine as "devices" of a tailnet that doesn't exist. Every reader of the
#: status sees it (one truth), and nothing shells out while it is set.
STATUS_FILE_ENV = "MINDFLOCK_TAILSCALE_STATUS_FILE"

#: The macOS app's CLI (App Store and Standalone builds alike).
APP_BUNDLE_CLI = "/Applications/Tailscale.app/Contents/MacOS/Tailscale"

#: Where Windows Tailscale lives, seen from WSL's default /mnt/c automount.
WINDOWS_CANDIDATES = (
    "/mnt/c/Program Files/Tailscale/tailscale.exe",
    "/mnt/c/Program Files (x86)/Tailscale/tailscale.exe",
)

#: Seconds a ``status --json`` snapshot is shared before the next re-read.
CACHE_TTL = 5.0

_STATUS_TIMEOUT = 5

#: Key expiry this close (days) is a warning.
EXPIRY_WARN_DAYS = 30

ADMIN_MACHINES = "https://login.tailscale.com/admin/machines"
ADMIN_DNS = "https://login.tailscale.com/admin/dns"
DOWNLOAD = "https://tailscale.com/download"
DOWNLOAD_MAC = "https://tailscale.com/download/mac"
WSL_KB = "https://tailscale.com/kb/1295/install-windows-wsl2"
TAGGED_EXPIRY_KB = "https://tailscale.com/kb/1068/tags"

LINUX_INSTALL = "curl -fsSL https://tailscale.com/install.sh | sh"
MAC_INSTALL = "brew install --cask tailscale-app"
OPERATOR_UP = "sudo tailscale up --operator=$USER"


# --------------------------------------------------------------------------- #
# Resolver
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Resolution:
    """Where the CLI is. ``usable`` is whether MindFlock may run it for this
    machine's own tailnet identity (False for the Windows client seen from
    WSL — that one is diagnostics only)."""

    path: str
    #: ``native`` | ``app-bundle`` | ``wsl-windows-host``
    kind: str
    #: ``env`` | ``path`` | ``app-bundle`` | ``windows``
    source: str
    usable: bool

    def to_dict(self) -> dict:
        return asdict(self)


def _is_exec(path: str) -> bool:
    return bool(path) and os.path.isfile(path) and os.access(path, os.X_OK)


def _kind_of(path: str) -> str:
    if path.lower().endswith(".exe"):
        return "wsl-windows-host"
    real = path
    try:
        real = os.path.realpath(path)
    except OSError:
        pass
    if "Tailscale.app/Contents/MacOS" in real or "Tailscale.app/Contents/MacOS" in path:
        return "app-bundle"
    return "native"


def _make(path: str, source: str) -> Resolution:
    kind = _kind_of(path)
    return Resolution(path, kind, source, kind != "wsl-windows-host")


def windows_tailscale() -> str:
    """On WSL, the Windows client's ``tailscale.exe`` (``""`` elsewhere or
    when absent). For diagnostics only: it is a different tailnet node."""
    if osenv.os_kind() != "wsl":
        return ""
    found = shutil.which("tailscale.exe")
    if found:
        return found
    for cand in WINDOWS_CANDIDATES:
        if os.path.isfile(cand):
            return cand
    return ""


def resolve() -> Optional[Resolution]:
    """The CLI, by precedence (module docstring), or None when there is none
    at all. Not cached: a ``which`` and a stat or two, and an install shows
    up on the next call."""
    override = (os.environ.get(BIN_ENV) or "").strip()
    if override:
        found = override if os.sep in override else shutil.which(override)
        if found and (_is_exec(found) or os.sep not in override):
            return _make(found, "env")
    found = shutil.which("tailscale")
    if found:
        return _make(found, "path")
    if osenv.os_kind() == "macos" and _is_exec(APP_BUNDLE_CLI):
        return Resolution(APP_BUNDLE_CLI, "app-bundle", "app-bundle", True)
    win = windows_tailscale()
    if win:
        return Resolution(win, "wsl-windows-host", "windows", False)
    return None


def tailscale_bin() -> Optional[str]:
    """The CLI MindFlock may run for this machine, or None — the drop-in for
    the old ``shutil.which("tailscale")`` checks."""
    r = resolve()
    return r.path if r and r.usable else None


def available() -> bool:
    return tailscale_bin() is not None


def command(args: List[str]) -> List[str]:
    """``args`` (``["tailscale", "serve", …]``) with the leading
    ``"tailscale"`` swapped for the resolved CLI. Unchanged when there is no
    usable one (the run then fails like a missing binary would)."""
    args = list(args)
    if args and args[0] == "tailscale":
        path = tailscale_bin()
        if path:
            args[0] = path
    return args


def env() -> Optional[dict]:
    """The environment to run the CLI with: the app bundle's binary only acts
    as a CLI with ``TAILSCALE_BE_CLI=1``. None (inherit) otherwise."""
    r = resolve()
    if r and r.usable and r.kind == "app-bundle":
        e = dict(os.environ)
        e["TAILSCALE_BE_CLI"] = "1"
        return e
    return None


def run(args: List[str], **kw) -> subprocess.CompletedProcess:
    """``subprocess.run`` for a ``["tailscale", …]`` argv, resolved (see
    :func:`command`/:func:`env`). Raises what ``subprocess.run`` raises."""
    e = env()
    if e is not None and "env" not in kw:
        kw["env"] = e
    return subprocess.run(command(args), **kw)


# --------------------------------------------------------------------------- #
# Cached status
# --------------------------------------------------------------------------- #
_lock = threading.Lock()
_cache: dict = {"at": 0.0, "data": None, "valid": False}


def invalidate() -> None:
    """Drop the shared snapshot (the next reader re-runs the CLI)."""
    with _lock:
        _cache.update(at=0.0, data=None, valid=False)


def _read_fake(path: str) -> Optional[dict]:
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _fetch() -> Optional[dict]:
    if tailscale_bin() is None:
        return None
    try:
        cp = run(
            ["tailscale", "status", "--json"],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=_STATUS_TIMEOUT,
        )
        if cp.returncode != 0:
            return None
        data = json.loads(cp.stdout.decode("utf-8", "replace") or "{}")
    except (subprocess.TimeoutExpired, OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def status_json(*, fresh: bool = False) -> Optional[dict]:
    """The parsed ``tailscale status --json`` (or the :data:`STATUS_FILE_ENV`
    document), None when there is no usable CLI or it didn't answer. A copy:
    callers may mutate it. ``fresh`` skips the cache (and refills it)."""
    fake = (os.environ.get(STATUS_FILE_ENV) or "").strip()
    if fake:
        return _read_fake(fake)
    # Held across the fetch on purpose: whoever arrives during a re-read
    # waits for that one subprocess instead of starting another.
    with _lock:
        now = time.monotonic()
        if not fresh and _cache["valid"] and now - _cache["at"] < CACHE_TTL:
            data = _cache["data"]
        else:
            data = _fetch()
            _cache.update(at=time.monotonic(), data=data, valid=True)
    return copy.deepcopy(data) if data is not None else None


def _self(data: Optional[dict]) -> dict:
    node = (data or {}).get("Self")
    return node if isinstance(node, dict) else {}


def tailnet_ips(data: Optional[dict] = None) -> List[str]:
    """This node's tailnet addresses (v4 and v6), ``[]`` off-tailnet."""
    if data is None:
        data = status_json()
    return [str(a) for a in _self(data).get("TailscaleIPs") or [] if a]


def self_ipv4(data: Optional[dict] = None) -> str:
    for a in tailnet_ips(data):
        if ":" not in a:
            return a
    return ""


# --------------------------------------------------------------------------- #
# Health
# --------------------------------------------------------------------------- #
def _parse_time(raw) -> Optional[_dt.datetime]:
    s = str(raw or "").strip()
    if not s or s.startswith("0001-"):
        return None
    try:
        # Go's RFC 3339 may carry nanoseconds; fromisoformat takes micro.
        s = re.sub(r"(\.\d{6})\d+", r"\1", s.replace("Z", "+00:00"))
        t = _dt.datetime.fromisoformat(s)
    except ValueError:
        return None
    return t if t.tzinfo else t.replace(tzinfo=_dt.timezone.utc)


def key_expiry(node: dict, now: Optional[_dt.datetime] = None) -> dict:
    """``{at, days, expired, warn}`` for a status node's ``KeyExpiry``
    (``at=""`` when the key doesn't expire)."""
    at = _parse_time(node.get("KeyExpiry")) if isinstance(node, dict) else None
    if at is None:
        return {"at": "", "days": None, "expired": False, "warn": False}
    now = now or _dt.datetime.now(_dt.timezone.utc)
    secs = (at - now).total_seconds()
    days = int(secs // 86400)
    expired = bool(node.get("Expired")) or secs <= 0
    return {
        "at": at.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "days": max(days, 0),
        "expired": expired,
        "warn": expired or days <= EXPIRY_WARN_DAYS,
    }


def _issue(iid: str, level: str, message: str, **extra) -> dict:
    out = {"id": iid, "level": level, "message": message}
    out.update({k: v for k, v in extra.items() if v})
    return out


def _install_fix(kind: str) -> str:
    return MAC_INSTALL if kind == "macos" else LINUX_INSTALL


def _wsl_hostname() -> str:
    """A name for an in-WSL node next to the Windows one (``<host>-wsl``)."""
    try:
        import socket

        host = socket.gethostname().split(".")[0].lower()
    except OSError:
        host = ""
    host = re.sub(r"[^a-z0-9-]", "-", host).strip("-") or "windows"
    return host[:57] + "-wsl"


def wsl_fix() -> str:
    """The tested path for MindFlock in WSL: a second Tailscale node inside
    WSL, named after the Windows machine (needs systemd in WSL)."""
    return "%s && sudo tailscale up --hostname=%s --operator=$USER" % (
        LINUX_INSTALL,
        _wsl_hostname(),
    )


def health(*, fresh: bool = False, now: Optional[_dt.datetime] = None) -> dict:
    """Everything "Tailscale on this device" shows, from the shared snapshot.

    ``installed`` means a CLI MindFlock can use here; ``backend_state`` is
    Tailscale's own (``Running`` / ``NeedsLogin`` / ``Stopped`` / ``NoState``
    / ``Starting`` / ``NeedsMachineAuth``, ``""`` when the CLI didn't
    answer). ``issues`` is the list of what is wrong, worst first, each with
    its one fix — the doctor and the card render the same list.
    """
    kind_os = osenv.os_kind()
    r = resolve()
    win = windows_tailscale() if kind_os == "wsl" else ""
    out: dict = {
        "installed": bool(r and r.usable),
        "path": r.path if r else "",
        "kind": r.kind if r else "",
        "source": r.source if r else "",
        "os": kind_os,
        "windows_host": win,
        "version": "",
        "backend_state": "",
        "running": False,
        "auth_url": "",
        "tailnet": "",
        "user": "",
        "tagged": False,
        "tags": [],
        "device": {"name": "", "dns": "", "ips": [], "os": "", "id": ""},
        "magicdns": None,
        "magicdns_suffix": "",
        "https": None,
        "key_expiry": key_expiry({}, now),
        "messages": [],
        "issues": [],
        "admin": {"machines": ADMIN_MACHINES, "dns": ADMIN_DNS},
    }
    issues: List[dict] = out["issues"]
    if not out["installed"]:
        if r and r.kind == "wsl-windows-host":
            issues.append(
                _issue(
                    "wsl_windows_only",
                    "warn",
                    "Tailscale is on Windows, but MindFlock runs inside WSL, so "
                    "your other devices can't reach it yet. Also run Tailscale "
                    "inside WSL (needs systemd on in /etc/wsl.conf); it shows up "
                    "as its own device next to the Windows one.",
                    fix=wsl_fix(),
                    docs=WSL_KB,
                )
            )
        elif kind_os == "macos":
            issues.append(
                _issue(
                    "missing",
                    "info",
                    "Tailscale isn't installed. Get the Tailscale app (the "
                    "Standalone download, or the Mac App Store) and sign in.",
                    fix=MAC_INSTALL,
                    docs=DOWNLOAD_MAC,
                )
            )
        else:
            issues.append(
                _issue(
                    "missing",
                    "info",
                    "Tailscale isn't installed.",
                    fix=_install_fix(kind_os),
                    docs=DOWNLOAD,
                )
            )
        return out

    data = status_json(fresh=fresh)
    if data is None:
        issues.append(
            _issue(
                "not_running",
                "warn",
                "Tailscale is installed but isn't answering — "
                + (
                    "open the Tailscale app."
                    if kind_os == "macos"
                    else "start it with `sudo systemctl enable --now tailscaled`."
                ),
                fix=(
                    ""
                    if kind_os == "macos"
                    else "sudo systemctl enable --now tailscaled"
                ),
            )
        )
        _wsl_note(out, win)
        return out

    state = str(data.get("BackendState") or "")
    tailnet = (
        data.get("CurrentTailnet")
        if isinstance(data.get("CurrentTailnet"), dict)
        else {}
    )
    node = _self(data)
    tags = [str(t) for t in node.get("Tags") or [] if t]
    users = data.get("User") if isinstance(data.get("User"), dict) else {}
    login = ""
    if not tags:
        login = str((users.get(str(node.get("UserID"))) or {}).get("LoginName") or "")
    suffix = str(
        data.get("MagicDNSSuffix") or (tailnet or {}).get("MagicDNSSuffix") or ""
    ).strip(".")
    magic = (tailnet or {}).get("MagicDNSEnabled")
    certs = data.get("CertDomains")
    msgs = data.get("Health")
    out.update(
        version=str(data.get("Version") or ""),
        backend_state=state,
        running=state == "Running",
        auth_url=str(data.get("AuthURL") or ""),
        tailnet=str((tailnet or {}).get("Name") or ""),
        user=login,
        tagged=bool(tags),
        tags=tags,
        device={
            "name": str(node.get("HostName") or ""),
            "dns": str(node.get("DNSName") or "").rstrip("."),
            "ips": tailnet_ips(data),
            "os": str(node.get("OS") or ""),
            "id": str(node.get("ID") or ""),
        },
        magicdns=bool(magic) if isinstance(magic, bool) else None,
        magicdns_suffix=suffix,
        https=(
            bool(certs)
            if state == "Running" and isinstance(tailnet, dict) and tailnet
            else None
        ),
        key_expiry=key_expiry(node, now),
        messages=[str(m) for m in msgs] if isinstance(msgs, list) else [],
    )

    if state in ("NeedsLogin", "NoState"):
        issues.append(
            _issue(
                "sign_in",
                "warn",
                "Tailscale isn't signed in on this device."
                + (" Open the sign-in link to finish." if out["auth_url"] else ""),
                # Linux won't let a non-root user sign in until it is the
                # operator; this one command does both.
                fix=OPERATOR_UP if kind_os in ("linux", "wsl") else "tailscale login",
            )
        )
    elif state == "NeedsMachineAuth":
        issues.append(
            _issue(
                "machine_auth",
                "warn",
                "This device is waiting for an admin to approve it in the "
                "Tailscale admin console.",
                docs=ADMIN_MACHINES,
            )
        )
    elif state == "Stopped":
        issues.append(
            _issue(
                "stopped",
                "warn",
                "Tailscale is signed in but turned off on this device.",
                fix="tailscale up",
            )
        )
    elif state and state != "Running":
        issues.append(_issue("starting", "info", "Tailscale is starting (%s)." % state))

    exp = out["key_expiry"]
    if state == "Running" and exp["at"] and exp["warn"]:
        when = (
            "has expired"
            if exp["expired"]
            else (
                "expires today"
                if exp["days"] == 0
                else "expires in %d day%s"
                % (exp["days"], "" if exp["days"] == 1 else "s")
            )
        )
        msg = (
            "This device's Tailscale key %s; when it does, the device drops off "
            "your tailnet (and Your devices, and the shared link). In the admin "
            "console's Machines page open %s → ⋯ → Disable key expiry."
            % (when, out["device"]["dns"] or out["device"]["name"] or "this device")
        )
        if tags:
            # Tagging in the console after sign-in keeps the expiry; only a
            # tag applied AT sign-in turns it off. Disabling is the safe fix.
            msg += (
                " Tagging a device in the console after it signed in keeps its "
                "expiry; only a tag applied at sign-in turns it off. (Re-signing "
                "in with `tailscale up --advertise-tags=%s --force-reauth` also "
                "works, but re-authenticates and can drop an SSH session over "
                "Tailscale.)" % tags[0]
            )
        issues.append(
            _issue(
                "key_expiry",
                "fail" if exp["expired"] else "warn",
                msg,
                docs=ADMIN_MACHINES,
            )
        )

    if state == "Running" and out["magicdns"] is False:
        issues.append(
            _issue(
                "magicdns_off",
                "info",
                "MagicDNS is off for your tailnet, so phone links use this "
                "device's IP and the shared link can't work. Turn it on in the "
                "admin console's DNS page.",
                docs=ADMIN_DNS,
            )
        )
    elif state == "Running" and out["https"] is False:
        issues.append(
            _issue(
                "https_off",
                "info",
                "HTTPS certificates are off for your tailnet; the shared phone "
                "link needs them. Turn them on in the admin console's DNS page.",
                docs=ADMIN_DNS,
            )
        )
    _wsl_note(out, win)
    issues.sort(key=lambda i: _RANK.get(i["level"], 3))
    return out


_RANK = {"fail": 0, "warn": 1, "info": 2}


def _wsl_note(out: dict, win: str) -> None:
    """WSL with Tailscale on both sides: two devices on the tailnet, and the
    one MindFlock uses is the WSL one. Not a problem — said once so the
    second entry in the admin console isn't a surprise."""
    if out["os"] == "wsl" and win and out["installed"]:
        out["issues"].append(
            _issue(
                "wsl_two_nodes",
                "info",
                "Windows runs Tailscale too, as a separate device. MindFlock "
                "uses the one inside WSL (%s)."
                % (out["device"]["dns"] or out["device"]["name"] or "this one"),
                docs=WSL_KB,
            )
        )


# --------------------------------------------------------------------------- #
# Sign in
# --------------------------------------------------------------------------- #
_URL_RE = re.compile(r"https://\S+")
_login_lock = threading.Lock()
_LOGIN: dict = {"proc": None, "auth_url": "", "output": [], "started": 0.0}

#: How long ``tailscale login`` waits for the person before giving up.
LOGIN_TIMEOUT = "600s"


def _pump(proc: subprocess.Popen) -> None:
    try:
        for raw in iter(proc.stdout.readline, b""):
            line = raw.decode("utf-8", "replace").strip()
            if not line:
                continue
            with _login_lock:
                _LOGIN["output"] = (_LOGIN["output"] + [line])[-20:]
                m = _URL_RE.search(line)
                if m and not _LOGIN["auth_url"]:
                    _LOGIN["auth_url"] = m.group(0).rstrip(".,")
    except (OSError, ValueError):
        pass
    finally:
        try:
            proc.wait(timeout=1)
        except Exception:  # noqa: BLE001
            pass


def _login_error(output: List[str]) -> dict:
    text = "\n".join(output)
    low = text.lower()
    if "access denied" in low or "permission" in low or "operator" in low:
        return {
            "error": "Tailscale needs your permission to sign in from MindFlock. "
            "Run this once in a terminal (it signs in too):",
            "fix": OPERATOR_UP,
        }
    return {"error": output[-1] if output else "tailscale login failed", "fix": ""}


def start_login(wait: float = 5.0) -> dict:
    """Start signing this device in, without waiting for the person.

    ``tailscale login`` when it needs a login, ``tailscale up`` when it is
    signed in but stopped. Runs in the background; returns as soon as the
    sign-in URL is known, the command failed, or ``wait`` seconds pass.
    ``{ok, state, auth_url, error, fix}``; the caller polls :func:`health`.
    """
    r = resolve()
    if not r or not r.usable:
        h = health()
        first = (h["issues"] or [{}])[0]
        return {
            "ok": False,
            "state": "",
            "auth_url": "",
            "error": first.get("message") or "Tailscale isn't installed.",
            "fix": first.get("fix", ""),
        }
    data = status_json(fresh=True) or {}
    state = str(data.get("BackendState") or "")
    if state == "Running":
        return {"ok": True, "state": state, "auth_url": "", "error": "", "fix": ""}
    if data.get("AuthURL"):
        return {
            "ok": True,
            "state": state,
            "auth_url": str(data["AuthURL"]),
            "error": "",
            "fix": "",
        }
    with _login_lock:
        proc = _LOGIN["proc"]
        if proc is None or proc.poll() is not None:
            verb = (
                ["up"]
                if state == "Stopped"
                else ["login", "--timeout=" + LOGIN_TIMEOUT]
            )
            try:
                proc = subprocess.Popen(
                    command(["tailscale", *verb]),
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    env=env(),
                    start_new_session=True,
                )
            except OSError as err:
                return {
                    "ok": False,
                    "state": state,
                    "auth_url": "",
                    "error": str(err),
                    "fix": "",
                }
            pump = threading.Thread(target=_pump, args=(proc,), daemon=True)
            _LOGIN.update(
                proc=proc, pump=pump, auth_url="", output=[], started=time.monotonic()
            )
            pump.start()
        pump = _LOGIN.get("pump")
    deadline = time.monotonic() + max(0.0, wait)
    while True:
        done = proc.poll() is not None
        if done and pump is not None:
            pump.join(1.0)  # let it read the last lines the command printed
        with _login_lock:
            url = _LOGIN["auth_url"]
            output = list(_LOGIN["output"])
        if url:
            invalidate()
            return {"ok": True, "state": state, "auth_url": url, "error": "", "fix": ""}
        if done:
            invalidate()
            if proc.returncode == 0:
                st = status_json(fresh=True) or {}
                return {
                    "ok": True,
                    "state": str(st.get("BackendState") or ""),
                    "auth_url": str(st.get("AuthURL") or ""),
                    "error": "",
                    "fix": "",
                }
            return {"ok": False, "state": state, "auth_url": "", **_login_error(output)}
        if time.monotonic() >= deadline:
            return {"ok": True, "state": state, "auth_url": "", "error": "", "fix": ""}
        time.sleep(0.1)
