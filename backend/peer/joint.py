"""Joint tasks: one feature built by two people's agents, one half each.

A link holds at most ONE joint task. Its state lives in its own file,
``~/.mindflock/peer/joint/<link_id>.json`` (0600, atomic writes) — never in
:class:`~backend.peer.store.Link`, whose ``validate()`` on an older release
would move the whole links file aside over a value it doesn't know::

    {"version": 1,
     "joint": {"id": "jt_<12 hex>", "brief", "proposed_by": "me"|"peer",
               "created": iso, "state": "proposed"|"active"|"done"|"cancelled",
               "reason": "",
               "mine":   {"repo_path", "repo_name", "branch", "program",
                          "share_id", "session_title", "error"},
               "theirs": {"repo_name", "branch", "program", "shared": bool},
               "contract": {"text", "rev", "updated": iso, "by": "me"|"peer"},
               "proposals": [{"id": "cp_<8 hex>", "by", "text", "at", "state":
                              "open"|"accepted"|"rejected"}]}}

``me``/``peer`` are always relative to the file's owner; on the wire they are
relative to the SENDER and flipped on receipt. Keys this version doesn't know
are kept as they are — the joint check (:mod:`backend.peer.joint_check`) lives
under ``check`` that way.

:class:`JointStore` is the file; :class:`JointTasks` is the protocol around
it, owned by the :class:`~backend.peer.service.PeerService`: the people's
routes (propose / accept / cancel / edit the contract / propose and decide
edits), the six inbound ``joint_*`` ops, the agent's ``peer_contract`` /
``peer_joint`` tools, and the ``joint_state`` resync after a reconnect.

The contract rule: the higher ``rev`` wins; a tie with different text goes to
the copy of the side that proposed the joint task. A side that keeps its own
copy over an incoming one sends its copy back, so both converge.

Everything the peer sends — the brief, the contract, a proposal — is
untrusted text. It is stored as received (the wire refused control
characters and capped the lengths); a contract proposal reaches this side's
agent framed as an untrusted PEER message, and only a PERSON accepts it.

See docs/peer-link.md ("Joint tasks — CONTRACT").
"""

from __future__ import annotations

import asyncio
import contextlib
import copy
import datetime
import json
import logging
import os
import re
import secrets
import threading
from typing import Any, Callable, Dict, List, Optional

from backend.peer import paths, wire

__all__ = [
    "JointStore",
    "JointTasks",
    "CONTRACT_FILE",
    "STARTER_CONTRACT",
    "handoff_prompt",
    "write_contract_file",
    "apply_contract",
    "merge_proposal",
    "wire_view",
    "summary",
]

_log = logging.getLogger("mindflock.peer")

MAX_BRIEF = wire.MAX_JOINT_BRIEF
MAX_CONTRACT = wire.MAX_JOINT_CONTRACT
MAX_PROPOSAL = wire.MAX_JOINT_PROPOSAL
MAX_PROPOSALS = wire.MAX_JOINT_PROPOSALS
MAX_REASON = wire.MAX_JOINT_REASON
STATES = wire.JOINT_STATES
OPEN_STATES = ("proposed", "active")
_RANK = {"proposed": 0, "active": 1, "done": 2, "cancelled": 2}
CONTRACT_FILE = "CONTRACT.md"
#: What the shared folder's sandbox can't do — said in the hand-off and in
#: the check-run note, so an agent marks such a step Can't check.
SANDBOX_LINE = (
    "The shared folder is sandboxed: no network and no package installs — a "
    "step that needs them is one to mark Can't check."
)
#: Seconds one joint request to the peer may take.
JOINT_TIMEOUT_S = 30.0

_LINK_ID_RE = re.compile(r"^[0-9a-f]{32}\Z")
_JOINT_ID_RE = re.compile(r"^jt_[0-9a-f]{12}\Z")
_PROPOSAL_ID_RE = re.compile(r"^cp_[0-9a-f]{8}\Z")
# What the wire refuses: in text (tab/newline/CR allowed) and in one line.
_BAD_TEXT = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f\ud800-\udfff]")
_BAD_LINE = re.compile(r"[\x00-\x1f\x7f-\x9f\ud800-\udfff]")

#: The contract a joint task starts with when its proposer wrote none (the
#: People screen pre-fills the same headings).
STARTER_CONTRACT = (
    "## What we're building\n\n\n"
    "## Interfaces between the halves\n\n\n"
    "## Who owns what\n\n\n"
    "## Done means\n\n"
)


class JointError(Exception):
    """A refusal with the words and HTTP status to show (the service turns
    it into a PeerServiceError)."""

    def __init__(self, message: str, status: int = 409) -> None:
        super().__init__(message)
        self.message = message
        self.status = status


def now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def flip(by: str) -> str:
    return "peer" if by == "me" else "me"


def _line(v, limit: int) -> str:
    return _BAD_LINE.sub("", str(v or ""))[:limit]


def _empty_mine() -> dict:
    return {
        "repo_path": "",
        "repo_name": "",
        "branch": "",
        "program": "",
        "share_id": "",
        "session_title": "",
        "error": "",
    }


def _empty_theirs() -> dict:
    return {"repo_name": "", "branch": "", "program": "", "shared": False}


def _normalize(j) -> Optional[dict]:
    """A loaded joint with every key present (tolerant: a missing or odd value
    gets its default, unknown keys stay). ``None`` when it isn't one."""
    if not isinstance(j, dict) or not _JOINT_ID_RE.match(str(j.get("id") or "")):
        return None
    out = dict(j)
    out["brief"] = str(j.get("brief") or "")[:MAX_BRIEF]
    out["proposed_by"] = (
        j.get("proposed_by") if j.get("proposed_by") in ("me", "peer") else "me"
    )
    out["created"] = str(j.get("created") or now_iso())
    out["state"] = j.get("state") if j.get("state") in STATES else "cancelled"
    out["reason"] = str(j.get("reason") or "")[:MAX_REASON]
    mine = _empty_mine()
    if isinstance(j.get("mine"), dict):
        mine.update({k: str(v or "") for k, v in j["mine"].items() if k in mine})
    out["mine"] = mine
    theirs = _empty_theirs()
    if isinstance(j.get("theirs"), dict):
        for k in ("repo_name", "branch", "program"):
            theirs[k] = str(j["theirs"].get(k) or "")
        theirs["shared"] = j["theirs"].get("shared") is True
    out["theirs"] = theirs
    c = j.get("contract") if isinstance(j.get("contract"), dict) else {}
    rev = c.get("rev")
    out["contract"] = {
        "text": str(c.get("text") or "")[:MAX_CONTRACT],
        "rev": rev if type(rev) is int and rev >= 0 else 0,
        "updated": str(c.get("updated") or out["created"]),
        "by": c.get("by") if c.get("by") in ("me", "peer") else "me",
    }
    props = []
    for p in j.get("proposals") if isinstance(j.get("proposals"), list) else []:
        if not isinstance(p, dict) or not _PROPOSAL_ID_RE.match(str(p.get("id") or "")):
            continue
        props.append(
            {
                "id": p["id"],
                "by": p.get("by") if p.get("by") in ("me", "peer") else "peer",
                "text": str(p.get("text") or "")[:MAX_PROPOSAL],
                "at": str(p.get("at") or out["created"]),
                "state": (
                    p.get("state")
                    if p.get("state") in wire.PROPOSAL_STATES
                    else "rejected"
                ),
            }
        )
    out["proposals"] = props
    return out


# --------------------------------------------------------------------------- #
# The file
# --------------------------------------------------------------------------- #
class JointStore:
    """``<root>/<link_id>.json`` per link (``root`` defaults to
    ``<peer root>/joint``). Single process, so a thread lock plus atomic
    writes are enough, like the message log."""

    def __init__(self, root: Optional[str] = None) -> None:
        self._root = root
        self._lock = threading.Lock()

    def _dir(self) -> str:
        return paths.ensure_dir(self._root or os.path.join(paths.peer_root(), "joint"))

    def _path(self, link_id: str) -> str:
        if not isinstance(link_id, str) or not _LINK_ID_RE.match(link_id):
            raise ValueError("bad link id")
        return os.path.join(self._dir(), link_id + ".json")

    def _load_doc(self, link_id: str) -> dict:
        try:
            fd = os.open(self._path(link_id), os.O_RDONLY | os.O_NOFOLLOW)
        except OSError:
            return {}
        try:
            with os.fdopen(fd, "rb") as f:
                doc = json.loads(f.read(4 * 1024 * 1024).decode("utf-8"))
        except (OSError, ValueError, UnicodeDecodeError):
            return {}
        return doc if isinstance(doc, dict) else {}

    def _save_doc(self, link_id: str, doc: dict) -> None:
        path = self._path(link_id)
        tmp = "%s.tmp-%d-%s" % (path, os.getpid(), os.urandom(4).hex())
        data = json.dumps(doc).encode("utf-8")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(data)
            os.replace(tmp, path)
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(tmp)
            raise

    def get(self, link_id: str) -> Optional[dict]:
        with self._lock:
            return _normalize(self._load_doc(link_id).get("joint"))

    def put(self, link_id: str, joint: dict) -> dict:
        with self._lock:
            doc = self._load_doc(link_id)
            doc["version"] = 1
            doc["joint"] = joint
            self._save_doc(link_id, doc)
        return copy.deepcopy(joint)

    def update(self, link_id: str, fn: Callable[[Optional[dict]], Any]):
        """``fn(joint or None)`` under the lock; when it returns a dict that is
        saved as the joint and a copy returned, else nothing is written and
        its result is returned. An exception writes nothing."""
        with self._lock:
            doc = self._load_doc(link_id)
            cur = _normalize(doc.get("joint"))
            out = fn(cur)
            if isinstance(out, dict):
                doc["version"] = 1
                doc["joint"] = out
                self._save_doc(link_id, doc)
                return copy.deepcopy(out)
            return out

    def drop(self, link_id: str) -> None:
        with self._lock, contextlib.suppress(OSError, ValueError):
            os.unlink(self._path(link_id))


# --------------------------------------------------------------------------- #
# Pure rules (no I/O)
# --------------------------------------------------------------------------- #
def new_joint(
    jid: str,
    brief: str,
    proposed_by: str,
    contract_text: str,
    *,
    mine: Optional[dict] = None,
    theirs: Optional[dict] = None,
) -> dict:
    now = now_iso()
    return {
        "id": jid,
        "brief": brief,
        "proposed_by": proposed_by,
        "created": now,
        "state": "proposed",
        "reason": "",
        "mine": {**_empty_mine(), **(mine or {})},
        "theirs": {**_empty_theirs(), **(theirs or {})},
        "contract": {
            "text": contract_text,
            "rev": 1,
            "updated": now,
            "by": proposed_by,
        },
        "proposals": [],
    }


def apply_contract(j: dict, rev: int, text: str, by: str) -> str:
    """Offer ``j`` a contract revision from the PEER (``by`` already in this
    side's terms). ``"taken"`` (``j`` changed), ``"same"`` (nothing to do) or
    ``"kept"`` (ours stays and should be sent back)."""
    cur = j["contract"]
    if rev == cur["rev"] and text == cur["text"]:
        return "same"
    if rev > cur["rev"] or (rev == cur["rev"] and j["proposed_by"] == "peer"):
        j["contract"] = {"text": text, "rev": rev, "updated": now_iso(), "by": by}
        return "taken"
    return "kept"


def bump_contract(j: dict, text: str, by: str = "me") -> None:
    j["contract"] = {
        "text": text,
        "rev": int(j["contract"]["rev"]) + 1,
        "updated": now_iso(),
        "by": by,
    }


def _trim(props: List[dict]) -> List[dict]:
    """At most MAX_PROPOSALS: every open one, then the newest decided."""
    if len(props) <= MAX_PROPOSALS:
        return props
    keep_open = [p for p in props if p["state"] == "open"]
    room = max(0, MAX_PROPOSALS - len(keep_open))
    decided = [p for p in props if p["state"] != "open"][-room:] if room else []
    keep = {id(p) for p in keep_open + decided}
    return [p for p in props if id(p) in keep]


def open_count(j: dict) -> int:
    return sum(1 for p in j["proposals"] if p["state"] == "open")


def merge_proposal(j: dict, prop: dict) -> str:
    """Fold one proposal (this side's terms) into ``j``: ``"new_open"``,
    ``"new"`` (arrived already decided), ``"decided"`` (an open one was
    accepted or rejected) or ``"same"``. Raises JointError when it would be
    one open proposal too many."""
    for cur in j["proposals"]:
        if cur["id"] == prop["id"]:
            if cur["state"] == "open" and prop["state"] != "open":
                cur["state"] = prop["state"]
                return "decided"
            return "same"
    if prop["state"] == "open" and open_count(j) >= MAX_PROPOSALS:
        raise JointError("too many open proposals — decide some first")
    j["proposals"] = _trim(j["proposals"] + [dict(prop)])
    return "new_open" if prop["state"] == "open" else "new"


def public_half(mine: dict) -> dict:
    return {
        "repo_name": _line(mine.get("repo_name"), wire.MAX_NAME),
        "branch": _line(mine.get("branch"), 200),
        "program": _line(mine.get("program"), 64),
    }


def wire_view(j: dict) -> dict:
    """The joint as THIS side sees it, for a ``joint_state`` answer: halves
    by name only — never a local path, share id or session title."""
    return {
        "id": j["id"],
        "brief": j["brief"] or "(no brief)",
        "proposed_by": j["proposed_by"],
        "created": j["created"],
        "state": j["state"],
        "mine": {**public_half(j["mine"]), "shared": bool(j["mine"]["share_id"])},
        "theirs": {
            **public_half(j["theirs"]),
            "shared": j["theirs"]["shared"] is True,
        },
        "contract": dict(j["contract"]),
        "proposals": [dict(p) for p in j["proposals"][-MAX_PROPOSALS:]],
        **_wire_check(j),
    }


def _wire_check(j: dict) -> dict:
    """``{"check": …}`` for a joint with a written checklist, else ``{}``."""
    from backend.peer import joint_check

    c = joint_check.wire_check(j)
    return {"check": c} if c is not None else {}


def summary(
    j: Optional[dict],
    theirs_shared: Optional[bool] = None,
    mine_shared: Optional[bool] = None,
) -> Optional[dict]:
    """``link_view.joint``: what a link card needs at a glance."""
    if j is None:
        return None
    brief = (j["brief"] or "").strip()
    head = brief.splitlines()[0] if brief else ""
    return {
        "id": j["id"],
        "state": j["state"],
        "brief_head": head[:120],
        "contract_rev": j["contract"]["rev"],
        "open_proposals": open_count(j),
        "mine_shared": bool(
            j["mine"]["share_id"] if mine_shared is None else mine_shared
        ),
        "theirs_shared": bool(
            j["theirs"]["shared"] if theirs_shared is None else theirs_shared
        ),
    }


def handoff_prompt(j: dict, peer_name: str, framed: bool = True) -> str:
    """What the joint's shared session is started with (after the share's
    own intro). The brief is the people's; when the PEER wrote it, it says so.
    ``framed=False`` is the same text for a mailbox note, whose own
    ``[MindFlock note …]`` header is the frame (a second ``[MindFlock`` would
    be typed with a fullwidth bracket)."""
    name = _line(peer_name, 32) or "the peer"
    brief = j["brief"].strip()
    if j["proposed_by"] == "peer":
        brief = "(brief written by %s, accepted by your user) %s" % (name, brief)
    repo = _line(j["theirs"].get("repo_name"), 128)
    where = "their repo %s" % repo if repo else "their own repo"
    head = ("[MindFlock joint task %s] " if framed else "Joint task %s — ") % j["id"]
    return (
        "%s%s\n"
        "Your half runs in this folder; %s's half runs in %s.\n"
        "The shared contract is in %s at the root of this folder — read it "
        "first, follow it, and when it must change use peer_contract (propose) "
        "rather than editing the file. Talk to the other agent with peer_send / "
        "peer_inbox; read their files with peer_read_file. Checkpoint when a "
        "piece is done.\n%s" % (head, brief, name, where, CONTRACT_FILE, SANDBOX_LINE)
    )


def contract_file_text(j: dict, peer_name: str) -> str:
    name = _line(peer_name, 32) or "the peer"
    head = (
        "<!-- MindFlock joint task %s with %s, contract rev %d. MindFlock "
        "rewrites this file on every accepted revision: change it with "
        "peer_contract (propose), not by editing it. -->\n\n"
        % (j["id"], name, int(j["contract"]["rev"]))
    )
    return head + (j["contract"]["text"] or "")


def write_contract_file(work: str, text: str) -> None:
    """Write ``CONTRACT.md`` at the root of a share's work dir. The sandboxed
    agent can write there, so nothing is followed: the dir is opened
    O_NOFOLLOW and the file goes in as a fresh temp file renamed over
    whatever is there (a planted symlink is replaced, never written through)."""
    dfd = os.open(work, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        tmp = ".%s.tmp-%s" % (CONTRACT_FILE, secrets.token_hex(4))
        fd = os.open(
            tmp,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
            0o644,
            dir_fd=dfd,
        )
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(text.encode("utf-8"))
            os.replace(tmp, CONTRACT_FILE, src_dir_fd=dfd, dst_dir_fd=dfd)
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(tmp, dir_fd=dfd)
            raise
    finally:
        os.close(dfd)


def _get(obj, key: str, default=None):
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def _op_error(message: str) -> Exception:
    from backend.peer import service as _svc

    return _svc._peer_op_error(message)


# Inbound refusals (short, peer-safe) and the words the SENDER's route shows.
_ERR_EXISTS = "joint task exists"
_ERR_NONE = "no joint task"
_ERR_STATE = "joint task not proposed"
_ERR_FULL = "too many open proposals"


# --------------------------------------------------------------------------- #
# The protocol
# --------------------------------------------------------------------------- #
class JointTasks:
    """Joint tasks for every link of one :class:`PeerService` (``svc``)."""

    def __init__(self, svc, store: Optional[JointStore] = None) -> None:
        self.svc = svc
        self.store = store or JointStore()
        self._locks: Dict[str, asyncio.Lock] = {}
        self._check = None

    @property
    def check(self):
        """The joint checks (:class:`backend.peer.joint_check.JointChecks`)."""
        if self._check is None:
            from backend.peer.joint_check import JointChecks

            self._check = JointChecks(self)
        return self._check

    # -- helpers ------------------------------------------------------------ #
    def _lock(self, link_id: str) -> asyncio.Lock:
        lk = self._locks.get(link_id)
        if lk is None:
            lk = self._locks[link_id] = asyncio.Lock()
        return lk

    def _err(self, message: str, status: int = 409):
        from backend.peer.service import PeerServiceError

        return PeerServiceError(message, status)

    def _name(self, link) -> str:
        return str(_get(link, "peer_name") or "peer")

    def _require_verified(self, link) -> None:
        if _get(link, "sas_verified") is not True:
            raise self._err(
                "compare the safety number with %s first, then press It matches"
                % self._name(link)
            )

    def _emit(self, event: str, link, j: Optional[dict], **data) -> None:
        payload = {
            "link_id": _get(link, "link_id"),
            "peer_name": self._name(link),
            "joint_id": j["id"] if j else "",
        }
        payload.update(data)
        self.svc._emit(event, payload)

    def _get_or_404(self, link_id: str) -> tuple:
        link = self.svc._link_or_404(link_id)
        j = self.store.get(link_id)
        if j is None:
            raise self._err("this link has no joint task", 404)
        return link, j

    async def _send(self, link, op: str, p: dict):
        """One joint request; refusals in words for the person."""
        name = self._name(link)
        try:
            return await self.svc.request(
                _get(link, "link_id"), op, p, timeout=JOINT_TIMEOUT_S
            )
        except Exception as err:  # noqa: BLE001 — PeerUnavailable, PeerOpError
            text = str(err)
            if "unsupported" in text:
                raise self._err(
                    "%s's MindFlock is older — ask them to update" % name
                ) from err
            if _ERR_EXISTS in text:
                raise self._err(
                    "%s already has a joint task with you — finish or cancel it "
                    "first" % name
                ) from err
            if _ERR_NONE in text or _ERR_STATE in text:
                raise self._err(
                    "%s no longer has this joint task (it may have been "
                    "cancelled on their side)" % name
                ) from err
            if _ERR_FULL in text:
                raise self._err(
                    "%s has too many open contract proposals — decide some "
                    "first" % name
                ) from err
            raise self.svc._peer_error(link, err) from err

    async def _try_send(self, link, op: str, p: dict) -> bool:
        """Best effort (the reconnect resync catches up): True when it got
        there."""
        try:
            await self._send(link, op, p)
            return True
        except Exception as err:  # noqa: BLE001
            _log.info("peer: joint %s not synced now: %s", op, err)
            return False

    def _clean_text(self, v, limit: int, what: str, required: bool = True) -> str:
        text = str(v or "").replace("\r\n", "\n").strip()
        if required and not text:
            raise self._err("write the %s first" % what, 400)
        if len(text) > limit:
            raise self._err(
                "the %s is too long (%s characters at most)"
                % (what, format(limit, ",")),
                400,
            )
        if _BAD_TEXT.search(text):
            raise self._err("the %s has control characters in it" % what, 400)
        return text

    def _choose_half(self, link, repo_path, branch, program) -> dict:
        """My half from a route body. An empty ``repo_path`` means "use the
        folder this link already shares" (or none: no session on my side)."""
        from backend.peer import launch as peer_launch
        from backend.peer import service as svc_mod

        mine = _empty_mine()
        repo_path = str(repo_path or "").strip()
        if branch not in (None, ""):
            if not isinstance(branch, str) or not svc_mod._BRANCH_RE.match(branch):
                raise self._err("bad branch name", 400)
            mine["branch"] = branch
        program = str(program or "").strip()
        if not repo_path:
            if program or mine["branch"]:
                raise self._err("pick a folder for your half first", 400)
            return mine
        if _get(link, "share_id"):
            raise self._err(
                "this link already shares a folder — leave the folder empty to "
                "use that one, or unshare it first"
            )
        repo = os.path.realpath(os.path.expanduser(repo_path))
        if not os.path.isdir(repo):
            raise self._err("repo_path must be an existing folder", 400)
        if paths.is_inside_peer_root(repo):
            raise self._err("can't share a folder from inside the peer root", 400)
        try:
            peer_launch.check_sandbox()
            provider = peer_launch.provider_name(
                program or self.svc.server.ENGINE.default_program()
            )
        except peer_launch.PeerLaunchError as err:
            raise self._err(str(err)) from err
        mine.update(
            repo_path=repo,
            repo_name=_line(os.path.basename(repo), wire.MAX_NAME),
            program=program,
        )
        mine["_provider"] = provider
        return mine

    @staticmethod
    def _wire_half(mine: dict) -> dict:
        half = public_half(mine)
        # The CLI's name, never its command line.
        half["program"] = _line(mine.get("_provider") or "", 64)
        return half

    def _full_view(self, link, j: Optional[dict]) -> Optional[dict]:
        """The local joint for the People screen (``GET …/joint``), with
        ``theirs.shared`` refreshed from their last ``status``."""
        if j is None:
            return None
        out = copy.deepcopy(j)
        out.pop("noted_rev", None)  # this side's own bookkeeping
        lid = _get(link, "link_id")
        seen = None
        with contextlib.suppress(Exception):
            seen = self.svc._their_share_view(lid, self.svc.is_connected(lid))
        if isinstance(seen, dict) and j["state"] == "active":
            out["theirs"]["shared"] = bool(seen.get("shared"))
        from backend.peer import joint_check

        out["check"] = joint_check.view(
            joint_check.normalize_check(j.get("check"), j["id"]),
            lid,
            j["id"] if j["state"] == "active" else "",
        )
        return out

    def summary(self, link) -> Optional[dict]:
        """``link_view.joint`` (never raises)."""
        lid = _get(link, "link_id")
        try:
            j = self.store.get(lid)
        except Exception:  # noqa: BLE001
            return None
        if j is None:
            return None
        seen = self.svc._their_status.get(lid) if j["state"] == "active" else None
        their = bool(seen[1].get("shared")) if seen else None
        out = summary(j, their, self._mine_shared(link, j))
        from backend.peer import joint_check

        with contextlib.suppress(Exception):
            check = joint_check.summary(j)
            if check is not None:
                out["check"] = check
        return out

    @staticmethod
    def _mine_shared(link, j: dict) -> bool:
        """My half's share is the one this link still has (not unshared)."""
        sid = j["mine"]["share_id"]
        return bool(sid) and sid == (_get(link, "share_id") or "")

    def _write_contract(self, link, j: dict) -> None:
        """Rewrite CONTRACT.md in my half's shared folder (if it has one)."""
        if not self._mine_shared(link, j):
            return
        work = paths.share_paths(j["mine"]["share_id"])["work"]
        if not os.path.isdir(work):
            return
        try:
            write_contract_file(work, contract_file_text(j, self._name(link)))
        except OSError as err:
            _log.warning("peer: CONTRACT.md not written: %s", err)

    def _post_note(self, link, text: str) -> None:
        title = self.svc._share_session(link)
        if not title:
            return
        try:
            from backend.web.core import mailbox

            mailbox.post(title, text, kind=mailbox.NOTE_KIND)
        except Exception as err:  # noqa: BLE001
            _log.warning("peer: joint note not delivered: %s", err)

    def _note_contract(self, link, j: dict) -> None:
        """Tell my shared agent CONTRACT.md moved to a new rev (a person's
        edit, an accepted proposal, the peer's revision) — once per rev, and
        only for an active joint (the hand-off covers the rev it starts on).
        Rides the note lane, so it is typed when the agent is idle."""
        if j["state"] != "active":
            return
        rev = int(j["contract"]["rev"])
        claimed: List[bool] = []

        def claim(cur):
            if cur is None or cur["id"] != j["id"]:
                return None
            seen = cur.get("noted_rev")
            if type(seen) is int and seen >= rev:
                return None
            cur["noted_rev"] = rev
            claimed.append(True)
            return cur

        self.store.update(str(_get(link, "link_id") or ""), claim)
        if not claimed:
            return
        name = _line(self._name(link), 32) or "the peer"
        text = j["contract"]["text"]
        took = [
            p for p in j["proposals"] if p["state"] == "accepted" and p["text"] == text
        ]
        if took:
            note = (
                "%s was accepted — %s is now rev %d: re-read it before continuing."
                % (
                    self._whose_proposal(took[-1], name),
                    CONTRACT_FILE,
                    rev,
                )
            )
        else:
            who = "your user" if j["contract"]["by"] == "me" else name
            note = "%s changed to rev %d (by %s) — re-read it before continuing." % (
                CONTRACT_FILE,
                rev,
                who,
            )
        self._post_note(link, note)

    def _note_rejected(self, link, j: dict, prop: dict, by: str) -> None:
        """Tell my shared agent a contract proposal was rejected (``by`` =
        "me" for my person, else the peer's). Once: a proposal is decided
        only once on each side."""
        if j["state"] != "active":
            return
        name = _line(self._name(link), 32) or "the peer"
        self._post_note(
            link,
            "%s was rejected by %s: continue with the current contract (%s rev %d)."
            % (
                self._whose_proposal(prop, name),
                "your user" if by == "me" else name,
                CONTRACT_FILE,
                int(j["contract"]["rev"]),
            ),
        )

    @staticmethod
    def _whose_proposal(prop: dict, name: str) -> str:
        if prop["by"] == "me":
            return "Your contract proposal %s" % prop["id"]
        return "%s's contract proposal %s" % (name, prop["id"])

    def _deliver_proposal(self, link, j: dict, prop: dict) -> None:
        """A contract proposal from the peer's agent, into MY shared agent as
        an untrusted PEER message (it can't accept it; the person decides)."""
        title = self.svc._share_session(link)
        if not title:
            return
        try:
            from backend.web.core import mailbox

            name = mailbox.peer_display_name("peer:" + self._name(link))
            text = (
                "Contract proposal %s for joint task %s — the whole new "
                "contract text follows. The people accept or reject it in "
                "People, you cannot; read the current contract with "
                "peer_contract. Proposed text: %s" % (prop["id"], j["id"], prop["text"])
            )
            mailbox.post(
                title,
                text,
                sender="peer:" + name,
                data={
                    "link_id": _get(link, "link_id"),
                    "peer_kind": "contract_proposal",
                    "joint_id": j["id"],
                    "proposal_id": prop["id"],
                },
                delivery="auto",
            )
        except Exception as err:  # noqa: BLE001
            _log.warning("peer: contract proposal not delivered: %s", err)

    # -- my side's share ---------------------------------------------------- #
    async def start_share(self, link_id: str) -> Optional[dict]:
        """An active joint whose half has a folder chosen and no share yet:
        start it (the share's prompt is the hand-off, CONTRACT.md is written
        before the agent starts). With no folder chosen but a folder this link
        already shares, adopt that one. Never raises: a failure is kept in
        ``mine.error`` and announced."""
        link = self.svc.store.get(link_id)
        j = self.store.get(link_id)
        if link is None or j is None or j["state"] != "active" or j["mine"]["share_id"]:
            return None
        name = self._name(link)
        mine = j["mine"]
        if not mine["repo_path"]:
            sid = _get(link, "share_id") or ""
            if not sid:
                return None

            def adopt(cur):
                if cur is None or cur["id"] != j["id"]:
                    return None
                cur["mine"].update(
                    share_id=sid,
                    session_title=_get(link, "session_title") or "",
                    error="",
                )
                return cur

            j2 = self.store.update(link_id, adopt)
            if j2:
                self._write_contract(link, j2)
                self._post_note(link, handoff_prompt(j2, name, framed=False))
                self._emit(
                    "peer.joint_state",
                    link,
                    j2,
                    state=j2["state"],
                    by="you",
                    mine_shared=True,
                )
            return None

        def seed(share) -> None:
            # Before the agent starts: its first instruction is to read it.
            write_contract_file(_get(share, "work"), contract_file_text(j, name))

        try:
            res = await self.svc.share(
                link_id,
                mine["repo_path"],
                mine["branch"] or None,
                mine["program"],
                handoff_prompt(j, name),
                seed=seed,
            )
        except Exception as err:  # noqa: BLE001 — kept for the card, announced
            msg = str(getattr(err, "message", "") or err)[:300] or type(err).__name__

            def failed(cur):
                if cur is None or cur["id"] != j["id"]:
                    return None
                cur["mine"]["error"] = msg
                return cur

            j2 = self.store.update(link_id, failed) or j
            self._emit(
                "peer.joint_state",
                link,
                j2,
                state=j2["state"],
                by="you",
                mine_shared=False,
                error=msg,
            )
            return None
        fresh = self.svc.store.get(link_id)

        def bound(cur):
            if cur is None or cur["id"] != j["id"]:
                return None
            cur["mine"].update(
                share_id=_get(fresh, "share_id") or "",
                session_title=_get(fresh, "session_title") or "",
                error="",
            )
            return cur

        j2 = self.store.update(link_id, bound) or j
        self._emit(
            "peer.joint_state", link, j2, state=j2["state"], by="you", mine_shared=True
        )
        return res

    # -- the people's routes ------------------------------------------------ #
    def get(self, link_id: str) -> dict:
        link = self.svc._link_or_404(link_id)
        return {"joint": self._full_view(link, self.store.get(link_id))}

    async def propose(
        self,
        link_id: str,
        brief,
        repo_path="",
        branch=None,
        program="",
        contract_text=None,
    ) -> dict:
        self.svc._require_enabled()
        link = self.svc._link_or_404(link_id)
        self._require_verified(link)
        brief = self._clean_text(brief, MAX_BRIEF, "brief")
        contract = self._clean_text(
            contract_text, MAX_CONTRACT, "contract", required=False
        )
        contract = contract or STARTER_CONTRACT
        mine = self._choose_half(link, repo_path, branch, program)
        async with self._lock(link_id):
            prev = self.store.get(link_id)
            if prev is not None and prev["state"] in OPEN_STATES:
                raise self._err(
                    "this link already has a joint task — finish or cancel it first"
                )
            jid = "jt_" + secrets.token_hex(6)
            wire_half = self._wire_half(mine)
            mine.pop("_provider", None)
            j = new_joint(jid, brief, "me", contract, mine=mine)
            self.store.put(link_id, j)
            try:
                await self._send(
                    link,
                    "joint_propose",
                    {
                        "id": jid,
                        "brief": brief,
                        "contract_text": contract,
                        "mine": wire_half,
                    },
                )
            except BaseException:
                if prev is not None:
                    self.store.put(link_id, prev)
                else:
                    self.store.drop(link_id)
                raise
        self._emit("peer.joint_state", link, j, state="proposed", by="you")
        return {"joint": self._full_view(link, j)}

    async def accept(self, link_id: str, repo_path="", branch=None, program="") -> dict:
        self.svc._require_enabled()
        link = self.svc._link_or_404(link_id)
        self._require_verified(link)
        mine = self._choose_half(link, repo_path, branch, program)
        async with self._lock(link_id):
            j = self.store.get(link_id)
            if j is None or j["state"] != "proposed" or j["proposed_by"] != "peer":
                raise self._err(
                    "there's no joint task from %s to accept" % self._name(link)
                )
            await self._send(
                link, "joint_accept", {"id": j["id"], "mine": self._wire_half(mine)}
            )
            mine.pop("_provider", None)

            def activate(cur):
                if cur is None or cur["id"] != j["id"]:
                    return None
                cur["state"] = "active"
                cur["mine"] = {**_empty_mine(), **mine}
                return cur

            j = self.store.update(link_id, activate) or j
        self._emit("peer.joint_state", link, j, state="active", by="you")
        res = await self.start_share(link_id)
        out = {"joint": self._full_view(link, self.store.get(link_id))}
        if res is not None:
            out["session"] = res.get("session")
        return out

    async def cancel(self, link_id: str, reason="") -> dict:
        link, j = self._get_or_404(link_id)
        reason = _line(reason, MAX_REASON).strip()
        if j["state"] not in OPEN_STATES:
            raise self._err("this joint task is already %s" % j["state"])
        state = "done" if reason == "done" else "cancelled"

        def close(cur):
            if cur is None or cur["id"] != j["id"]:
                return None
            cur["state"], cur["reason"] = state, reason
            return cur

        j = self.store.update(link_id, close) or j
        synced = await self._try_send(
            link, "joint_cancel", {"id": j["id"], "reason": reason}
        )
        self._emit("peer.joint_state", link, j, state=state, by="you", reason=reason)
        return {"joint": self._full_view(link, j), "synced": synced}

    def _require_open(self, link, j: dict) -> None:
        if j["state"] not in OPEN_STATES:
            raise self._err("this joint task is %s" % j["state"])

    async def set_contract(self, link_id: str, text) -> dict:
        link, j = self._get_or_404(link_id)
        self._require_verified(link)
        self._require_open(link, j)
        text = self._clean_text(text, MAX_CONTRACT, "contract", required=False)

        def edit(cur):
            if cur is None or cur["id"] != j["id"]:
                raise self._err("this link has no joint task", 404)
            bump_contract(cur, text)
            return cur

        j = self.store.update(link_id, edit)
        self._write_contract(link, j)
        self._note_contract(link, j)
        self._emit(
            "peer.joint_contract",
            link,
            j,
            kind="revision",
            rev=j["contract"]["rev"],
            by="you",
            open=False,
        )
        synced = await self._push_contract(link, j)
        return {"joint": self._full_view(link, j), "synced": synced}

    async def _push_contract(self, link, j: dict) -> bool:
        c = j["contract"]
        return await self._try_send(
            link,
            "joint_contract",
            {
                "id": j["id"],
                "rev": max(1, int(c["rev"])),
                "text": c["text"],
                "by": c["by"],
            },
        )

    async def propose_edit(self, link_id: str, text, by: str = "you") -> dict:
        """A proposed contract change (the whole new text), from you or from
        the shared agent (``by="agent"``)."""
        link, j = self._get_or_404(link_id)
        self._require_open(link, j)
        text = self._clean_text(text, MAX_PROPOSAL, "proposal")
        prop = {
            "id": "cp_" + secrets.token_hex(4),
            "by": "me",
            "text": text,
            "at": now_iso(),
            "state": "open",
        }

        def add(cur):
            if cur is None or cur["id"] != j["id"]:
                raise self._err("this link has no joint task", 404)
            try:
                merge_proposal(cur, prop)
            except JointError as err:
                raise self._err(err.message) from err
            return cur

        j = self.store.update(link_id, add)
        self._emit(
            "peer.joint_contract",
            link,
            j,
            kind="proposal",
            proposal_id=prop["id"],
            open=True,
            by=by,
            rev=j["contract"]["rev"],
        )
        synced = await self._try_send(
            link, "joint_proposal", {"id": j["id"], "proposal": prop}
        )
        return {
            "proposal_id": prop["id"],
            "synced": synced,
            "joint": self._full_view(link, j),
        }

    async def decide(self, link_id: str, proposal_id: str, accept: bool) -> dict:
        link, j = self._get_or_404(link_id)
        if accept:
            self._require_verified(link)
        self._require_open(link, j)
        if not isinstance(proposal_id, str) or not _PROPOSAL_ID_RE.match(proposal_id):
            raise self._err("no such proposal", 404)
        decided: dict = {}

        def apply(cur):
            if cur is None or cur["id"] != j["id"]:
                raise self._err("this link has no joint task", 404)
            for p in cur["proposals"]:
                if p["id"] == proposal_id:
                    if p["state"] != "open":
                        raise self._err("that proposal was already %s" % p["state"])
                    p["state"] = "accepted" if accept else "rejected"
                    decided.update(p)
                    if accept:
                        bump_contract(cur, p["text"])
                    return cur
            raise self._err("no such proposal", 404)

        j = self.store.update(link_id, apply)
        if accept:
            self._write_contract(link, j)
            self._note_contract(link, j)
        else:
            self._note_rejected(link, j, decided, "me")
        self._emit(
            "peer.joint_contract",
            link,
            j,
            kind="proposal",
            proposal_id=proposal_id,
            open=False,
            state=decided["state"],
            by="you",
            rev=j["contract"]["rev"],
        )
        synced = await self._try_send(
            link, "joint_proposal", {"id": j["id"], "proposal": dict(decided)}
        )
        if accept:
            synced = await self._push_contract(link, j) and synced
        return {"joint": self._full_view(link, j), "synced": synced}

    # -- the shared agent's tools (via AgentApi) ---------------------------- #
    def agent_joint(self, link_id: str) -> dict:
        j = self.store.get(link_id)
        if j is None:
            raise self._err("this link has no joint task", 404)
        v = wire_view(j)
        return {k: v[k] for k in ("id", "brief", "state", "mine", "theirs")}

    def agent_contract(self, link_id: str) -> dict:
        j = self.store.get(link_id)
        if j is None:
            raise self._err("this link has no joint task", 404)
        return {
            "rev": j["contract"]["rev"],
            "text": j["contract"]["text"],
            "open_proposals": [
                {k: p[k] for k in ("id", "by", "text", "at")}
                for p in j["proposals"]
                if p["state"] == "open"
            ],
        }

    # -- inbound ops ---------------------------------------------------------- #
    async def handle(self, link, op: str, p: dict) -> dict:
        lid = str(_get(link, "link_id") or "")
        fn = getattr(self, "_in_" + op[len("joint_") :], None)
        if fn is None:
            raise _op_error("not permitted")
        try:
            return await fn(link, lid, p)
        except JointError as err:
            raise _op_error(err.message) from err

    async def _in_propose(self, link, lid: str, p: dict) -> dict:
        def store(cur):
            if cur is not None and cur["state"] in OPEN_STATES:
                if cur["id"] == p["id"]:
                    return None  # a retry
                raise JointError(_ERR_EXISTS)
            theirs = {**public_half(p["mine"]), "shared": False}
            return new_joint(
                p["id"], p["brief"], "peer", p["contract_text"], theirs=theirs
            )

        j = self.store.update(lid, store)
        if j is not None:
            self._emit(
                "peer.joint_proposed",
                link,
                j,
                brief=_line(j["brief"], 200),
            )
        return {"ok": True}

    async def _in_accept(self, link, lid: str, p: dict) -> dict:
        def activate(cur):
            if cur is None or cur["id"] != p["id"]:
                raise JointError(_ERR_NONE)
            if cur["state"] == "active" and cur["proposed_by"] == "me":
                return None  # a retry
            if cur["state"] != "proposed" or cur["proposed_by"] != "me":
                raise JointError(_ERR_STATE)
            cur["state"] = "active"
            cur["theirs"] = {**public_half(p["mine"]), "shared": False}
            return cur

        j = self.store.update(lid, activate)
        if j is not None:
            self._emit("peer.joint_state", link, j, state="active", by="peer")
            self.svc._schedule(self.start_share(lid))
        return {"ok": True}

    async def _in_cancel(self, link, lid: str, p: dict) -> dict:
        reason = p.get("reason") or ""

        def close(cur):
            if cur is None or cur["id"] != p["id"]:
                raise JointError(_ERR_NONE)
            if cur["state"] not in OPEN_STATES:
                return None
            cur["state"] = "done" if reason == "done" else "cancelled"
            cur["reason"] = reason
            return cur

        j = self.store.update(lid, close)
        if j is not None:
            self._emit(
                "peer.joint_state", link, j, state=j["state"], by="peer", reason=reason
            )
        return {"ok": True}

    async def _in_contract(self, link, lid: str, p: dict) -> dict:
        outcome: List[str] = []

        def offer(cur):
            if cur is None or cur["id"] != p["id"]:
                raise JointError(_ERR_NONE)
            res = apply_contract(cur, p["rev"], p["text"], flip(p["by"]))
            outcome.append(res)
            return cur if res == "taken" else None

        j = self.store.update(lid, offer)
        if j is not None:
            self._write_contract(link, j)
            self._note_contract(link, j)
            self._emit(
                "peer.joint_contract",
                link,
                j,
                kind="revision",
                rev=j["contract"]["rev"],
                by="peer",
                open=False,
            )
            return {"ok": True, "rev": j["contract"]["rev"]}
        cur = self.store.get(lid)
        if outcome == ["kept"] and cur is not None:
            self.svc._schedule(self._push_contract(link, cur))
        return {"ok": True, "rev": cur["contract"]["rev"] if cur else 0}

    async def _in_proposal(self, link, lid: str, p: dict) -> dict:
        prop = dict(p["proposal"], by=flip(p["proposal"]["by"]))
        outcome: List[str] = []

        def fold(cur):
            if cur is None or cur["id"] != p["id"]:
                raise JointError(_ERR_NONE)
            res = merge_proposal(cur, prop)
            outcome.append(res)
            return cur if res != "same" else None

        j = self.store.update(lid, fold)
        if j is not None:
            self._announce_proposal(link, j, prop, outcome[0])
        return {"ok": True}

    def _announce_proposal(self, link, j: dict, prop: dict, outcome: str) -> None:
        if outcome == "new_open" and prop["by"] == "peer":
            self._deliver_proposal(link, j, prop)
        elif outcome == "decided" and prop["state"] == "rejected":
            self._note_rejected(link, j, prop, "peer")
        self._emit(
            "peer.joint_contract",
            link,
            j,
            kind="proposal",
            proposal_id=prop["id"],
            open=prop["state"] == "open",
            state=prop["state"],
            by="peer",
            rev=j["contract"]["rev"],
        )

    async def _in_check(self, link, lid: str, p: dict) -> dict:
        return await self.check.inbound(link, lid, p)

    async def _in_state(self, link, lid: str, p: dict) -> dict:
        j = self.store.get(lid)
        if j is None:
            return {"joint": None}
        view = wire_view(j)
        view["mine"]["shared"] = self._mine_shared(link, j)
        return {"joint": view}

    # -- resync after a reconnect ------------------------------------------- #
    async def resync(self, link_id: str) -> None:
        """The link came (back) up: ask the peer for its copy and reconcile.
        State only moves forward (proposed → active → done|cancelled), the
        contract follows the rev rule, proposals are a union (a decision beats
        an open one); whatever the peer lacks is sent to it. Never raises."""
        try:
            await self._resync(link_id)
        except Exception as err:  # noqa: BLE001
            _log.info("peer: joint resync failed: %s", err)

    async def _resync(self, link_id: str) -> None:
        link = self.svc.store.get(link_id)
        local = self.store.get(link_id)
        if link is None or local is None:
            return
        try:
            res = await self.svc.request(
                link_id, "joint_state", {"id": local["id"]}, timeout=JOINT_TIMEOUT_S
            )
        except Exception as err:  # noqa: BLE001 — offline again, or an older peer
            _log.info("peer: joint state not fetched: %s", err)
            return
        theirs = res.get("joint") if isinstance(res, dict) else None
        if theirs is None or theirs.get("id") != local["id"]:
            await self._resync_lost(link, link_id, local, theirs)
            return
        async with self._lock(link_id):
            await self._resync_same(link, link_id, theirs)

    async def _resync_lost(self, link, link_id: str, local: dict, theirs) -> None:
        """The peer doesn't have OUR joint: it is over here too. A fresh
        proposal of theirs takes its place."""
        if local["state"] in OPEN_STATES:

            def close(cur):
                if (
                    cur is None
                    or cur["id"] != local["id"]
                    or cur["state"] not in OPEN_STATES
                ):
                    return None
                cur["state"] = "cancelled"
                cur["reason"] = "gone on their side"
                return cur

            j = self.store.update(link_id, close)
            if j is not None:
                self._emit(
                    "peer.joint_state",
                    link,
                    j,
                    state="cancelled",
                    by="peer",
                    reason=j["reason"],
                )
        if (
            isinstance(theirs, dict)
            and theirs.get("state") == "proposed"
            and theirs.get("proposed_by") == "me"
        ):
            await self._in_propose(
                link,
                link_id,
                {
                    "id": theirs["id"],
                    "brief": theirs["brief"],
                    "contract_text": theirs["contract"]["text"],
                    "mine": theirs["mine"],
                },
            )

    async def _resync_same(self, link, link_id: str, theirs: dict) -> None:
        jid = theirs["id"]
        notes: Dict[str, Any] = {
            "state": None,
            "contract": None,
            "props": [],
            "push": [],
        }

        def merge(cur):
            if cur is None or cur["id"] != jid:
                return None
            changed = False
            st = theirs["state"]
            if _RANK[st] > _RANK[cur["state"]]:
                cur["state"] = st
                if st not in OPEN_STATES and not cur["reason"]:
                    cur["reason"] = "done" if st == "done" else ""
                notes["state"] = st
                changed = True
            elif _RANK[cur["state"]] > _RANK[st] and cur["state"] not in OPEN_STATES:
                notes["push"].append("state")
            half = {
                **public_half(theirs["mine"]),
                "shared": theirs["mine"]["shared"] is True,
            }
            if half != cur["theirs"]:
                cur["theirs"] = half
                changed = True
            c = theirs["contract"]
            if c["rev"] >= 1:
                res = apply_contract(cur, c["rev"], c["text"], flip(c["by"]))
                if res == "taken":
                    notes["contract"] = cur["contract"]["rev"]
                    changed = True
                elif res == "kept":
                    notes["push"].append("contract")
            seen = set()
            for tp in theirs["proposals"]:
                prop = dict(tp, by=flip(tp["by"]))
                seen.add(prop["id"])
                try:
                    out = merge_proposal(cur, prop)
                except JointError:
                    continue
                if out != "same":
                    notes["props"].append((prop, out))
                    changed = True
                for mp in cur["proposals"]:
                    if mp["id"] == prop["id"] and mp["state"] != prop["state"]:
                        notes["push"].append(("prop", dict(mp)))
            for mp in cur["proposals"]:
                if mp["id"] not in seen and mp["state"] == "open":
                    notes["push"].append(("prop", dict(mp)))
            return cur if changed else None

        j = self.store.update(link_id, merge)
        cur = j or self.store.get(link_id)
        if cur is None:
            return
        if notes["state"]:
            self._emit(
                "peer.joint_state",
                link,
                cur,
                state=cur["state"],
                by="peer",
                reason=cur["reason"],
            )
            if cur["state"] == "active":
                self.svc._schedule(self.start_share(link_id))
        if notes["contract"]:
            self._write_contract(link, cur)
            self._note_contract(link, cur)
            self._emit(
                "peer.joint_contract",
                link,
                cur,
                kind="revision",
                rev=cur["contract"]["rev"],
                by="peer",
                open=False,
            )
        for prop, out in notes["props"]:
            self._announce_proposal(link, cur, prop, out)
        for item in notes["push"]:
            if item == "state":
                await self._try_send(
                    link, "joint_cancel", {"id": jid, "reason": cur["reason"]}
                )
            elif item == "contract":
                await self._push_contract(link, cur)
            else:
                await self._try_send(
                    link, "joint_proposal", {"id": jid, "proposal": item[1]}
                )
        # The joint check: their side's results (and steps), then mine.
        await self.check.reconcile(link, link_id, theirs.get("check"))
