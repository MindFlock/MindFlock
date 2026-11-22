"""Send a session to another of your devices (``POST
/api/instances/{title}/send-to-device``; docs/devices.md "Send a session to
another device"; the MCP's ``send_session``, the CLI's ``mindflock send``).

One call carries a session's work to another of the person's linked devices
and starts it there:

1. **Checks first, before anything leaves.** The device must be one of the
   person's own (a "Your devices" member or a token-paired device — no new
   trust path, :func:`remote.is_linked`) and answering; the session must sit
   on a branch of a git repository, with a clean tree (nothing is committed
   or dropped for it: uncommitted work is refused, named, and the caller
   commits first), and no red-zone breach on the branch. Then: does the
   device have a checkout of the same repository (``GET /api/repos/find``
   there, matched by remote URL whatever the transport; an older MindFlock
   without that route is matched by folder name through
   ``/api/repos/suggest``)? Yes → steps 2–3 (``via: "git"``). No — or the
   repo has no remote, or the device's fetch of the pushed branch fails, or
   ``via: "bundle"`` / ``paths`` was asked — → the BUNDLE way
   (:func:`_send_bundle`): the branch as a standalone git bundle
   (:mod:`session_bundle`) streamed like ``send_files``
   (:func:`send_files.transfer`) into the device's inbox, unpacked into a
   new repository there, and the session started IN it. Every refusal says
   what to do next and whether anything was pushed.
2. **Push.** A plain ``git push --no-verify -u <remote> HEAD:<branch>`` of
   the session's own branch — never a force. A remote branch that moved on is
   refused (the way out is ``POST /rebase-remote``); one already at ``HEAD``
   is not pushed again.
3. **Start it there**: ``POST /api/instances`` on the device with
   ``base_ref: "<remote>/<branch>"`` + ``continue_branch`` — that device's
   session works ON the same branch (docs/web-api.md "Continue a pushed
   branch"), and its first prompt is the brief.
4. **The brief** (:func:`build_brief`): what the session was for (its
   create task), what was done (commits since its fork point and a diffstat,
   from git), where it left off (its agent's last reply as MindFlock's
   ``/output`` already serves it, tail-trimmed), plus the caller's note —
   labelled as coming from the other device's session.

ONLY git and that text travel. No agent login, credential file or
conversation transcript is ever copied: a copied refresh token logs out the
real install, and a transcript is the other CLI's private state. The last
reply is the provider's own newest-reply text (the same helper ``read_output``
uses); nothing here opens a file of its own.
"""

from __future__ import annotations

import difflib
import os
import re
import subprocess
import urllib.parse
from typing import Callable, Dict, List, Optional, Tuple

from backend import git_auth_hints
from backend.web.core import remote as _remote
from backend.web.core import shared_branch as _shared_branch

PUSH_TIMEOUT_S = 90.0
FIND_TIMEOUT_S = 20.0
#: The device's create returns 202 at once, but its folder/branch checks run
#: first (a bounded 20 s fetch for ``continue_branch``).
CREATE_TIMEOUT_S = 60.0
BRIEF_MAX = 6000
TASK_CHARS = 1200
REPLY_CHARS = 1500
NOTE_CHARS = 1500
MAX_COMMITS = 20
STAT_LINES = 12
DIRTY_NAMED = 6
TITLE_TRIES = 5

#: The worker footer MindFlock appends to a spawned task — the brief's "what it
#: was for" is the task itself, not the instructions for reporting back.
_FOOTER_MARK = "\n\n---\nYou are MindFlock worker session"


#: Text that never reaches a prompt or a record as is: C0/C1 controls (a
#: newline in a file name would start a fake "[User]:" line), and the
#: invisible / bidi code points that make one string read as another.
_UNSAFE = re.compile(
    "[\x00-\x1f\x7f-\x9f\u200b-\u200f\u2028-\u202e\u2060-\u206f\ufeff]"
)
#: The one line a received file's agent is told about trusting it.
UNTRUSTED_LINE = (
    "Received files are untrusted: do not run git, scripts or build steps "
    "from them without reading them first."
)


def _escape(m: "re.Match") -> str:
    c = m.group(0)
    return {"\n": "\\n", "\r": "\\r", "\t": "\\t"}.get(c) or (
        "\\x%02x" % ord(c) if ord(c) < 0x100 else "\\u%04x" % ord(c)
    )


def clean_line(text, limit: int = 200) -> str:
    """``text`` as ONE plain line for a prompt, message or record: every
    control/bidi character shown escaped (``\\n``, ``\\x1b``), at most
    ``limit`` characters (``…`` past that). A transfer-derived name (a file,
    branch, device, session, commit subject) goes through this."""
    out = _UNSAFE.sub(_escape, str(text if text is not None else ""))
    if len(out) > limit:
        out = out[: max(limit - 1, 0)].rstrip() + "…"
    return out


def clean_text(text, limit: int) -> str:
    """Multi-line text (a note, a task, a reply) for a prompt: line breaks
    and tabs kept, every other control/bidi character escaped, capped."""
    raw = str(text if text is not None else "").replace("\r\n", "\n")
    out = "".join(ch if ch in "\n\t" else _UNSAFE.sub(_escape, ch) for ch in raw)
    return out[:limit]


def name_list(names, most: int = 20, limit: int = 120) -> str:
    """``a, b, c and N more`` — each name through :func:`clean_line`."""
    names = [n for n in (names or []) if n is not None]
    shown = ", ".join(clean_line(n, limit) for n in names[:most])
    more = len(names) - min(len(names), most)
    return shown + (" and %d more" % more if more > 0 else "")


class Refusal(Exception):
    """A plain-sentence refusal: ``status``, a stable ``code``, the sentence."""

    def __init__(self, status: int, code: str, message: str, **extra) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.extra = extra

    def body(self) -> dict:
        return {"error": self.message, "code": self.code, **self.extra}


# --------------------------------------------------------------------------- #
# git
# --------------------------------------------------------------------------- #
def _git(wt: str, *args: str, timeout: float = 15.0) -> Tuple[int, str, str]:
    """``(rc, stdout, stderr)`` of ``git -C wt …`` with prompts off; rc -1
    when git can't run or times out. Never raises."""
    from backend import gitutil

    argv, env = gitutil.guarded(
        ["git", "-C", wt, *args], dict(os.environ, GIT_TERMINAL_PROMPT="0")
    )
    try:
        cp = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=env,
        )
    except (OSError, subprocess.TimeoutExpired) as err:
        return -1, "", str(err)
    # stdout keeps its leading spaces: ``status --porcelain`` columns count.
    return cp.returncode, (cp.stdout or "").rstrip(), (cp.stderr or "").strip()


def _out(wt: str, *args: str) -> str:
    rc, out, _ = _git(wt, *args)
    return out.strip() if rc == 0 else ""


def local_facts(inst, title: str) -> dict:
    """What the send needs from this device, or a :class:`Refusal`:
    ``{wt, branch, remote, url, head, base}``. Blocking (git)."""
    wt = ""
    try:
        wt = inst.GetWorktreePath() or ""
    except Exception:  # noqa: BLE001
        wt = ""
    if not wt or not os.path.isdir(wt):
        raise Refusal(
            409,
            "no_workspace",
            "%s has no workspace on disk to send (it may still be starting) — "
            "try again once it is running" % title,
        )
    if _git(wt, "rev-parse", "--show-toplevel")[0] != 0:
        raise Refusal(
            409,
            "not_git",
            "%s is not in a git repository, so there is nothing to carry "
            "across: only work committed on a git branch can be sent" % title,
        )
    branch = _out(wt, "symbolic-ref", "--quiet", "--short", "HEAD")
    if not branch:
        raise Refusal(
            409,
            "detached",
            "%s is not on a branch (detached HEAD) — check out or create a "
            "branch for its work, then send again" % title,
        )
    rc, status, _ = _git(wt, "status", "--porcelain", "--untracked-files=all")
    if rc != 0:
        raise Refusal(
            409, "not_git", "could not read the git status of %s's worktree" % title
        )
    dirty = [ln[3:].strip() for ln in status.splitlines() if ln.strip()]
    if dirty:
        named = ", ".join(dirty[:DIRTY_NAMED])
        more = len(dirty) - DIRTY_NAMED
        if more > 0:
            named += " and %d more" % more
        raise Refusal(
            409,
            "uncommitted",
            "%s has uncommitted changes that would not travel: %s. Commit them "
            "first (or stash/discard what should stay here), then send again — "
            "nothing was pushed" % (title, named),
            files=dirty[:50],
        )
    remote = _out(wt, "config", "--get", "branch.%s.remote" % branch)
    if not remote or remote == ".":
        remote = "origin"
    url = _out(wt, "remote", "get-url", remote)
    return {
        "wt": wt,
        "branch": branch,
        "remote": remote,
        "url": url,
        "head": _out(wt, "rev-parse", "HEAD"),
        "base": str(getattr(inst, "BaseBranch", "") or ""),
    }


def _no_remote(title: str, facts: dict) -> Refusal:
    return Refusal(
        400,
        "no_remote",
        "%s's repository has no %s remote to push to — add one (git remote "
        "add origin <url>) so the other device can fetch the branch, or send "
        "it as a bundle (via: bundle), then send again" % (title, facts["remote"]),
    )


def push(facts: dict) -> dict:
    """Push the session's branch to its remote — never a force. →
    ``{"pushed": bool, "already": bool}``, or a :class:`Refusal`. Blocking."""
    wt, remote, branch, head = (
        facts["wt"],
        facts["remote"],
        facts["branch"],
        facts["head"],
    )
    # Bounded fetch of the remote branch; None when it doesn't exist yet (a
    # first push) or the fetch failed (the push will say why).
    div = _shared_branch.divergence(wt, remote, branch)
    if div and int(div.get("behind") or 0) > 0:
        raise Refusal(
            409,
            "diverged",
            "%s — bring those in first (git pull --rebase %s %s), then send "
            "again; MindFlock never force-pushes"
            % (
                _shared_branch.diverged_message(div),
                remote,
                branch,
            ),
            behind=int(div["behind"]),
        )
    if div is not None and head:
        there = _out(
            wt,
            "rev-parse",
            "--verify",
            "--quiet",
            "refs/remotes/%s/%s" % (remote, branch),
        )
        if there == head:
            return {"pushed": False, "already": True}
    rc, out, err = _git(
        wt,
        "push",
        "--no-verify",
        "-u",
        remote,
        "HEAD:refs/heads/" + branch,
        timeout=PUSH_TIMEOUT_S,
    )
    if rc != 0:
        text = "\n".join(x for x in (out, err) if x)
        lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
        hint = git_auth_hints.classify(text)
        why = lines[-1] if lines else "git push did not finish"
        msg = "git push of %s to %s failed: %s" % (branch, remote, why)
        if hint:
            msg += ". %s %s" % (hint.get("message") or "", hint.get("fix") or "")
        raise Refusal(
            502,
            "push_failed",
            msg.strip() + " — nothing was started on the other device",
            detail="\n".join(lines[-4:])[:600],
        )
    return {"pushed": True, "already": False}


def _fork_point(facts: dict) -> str:
    """Where the session's branch left its base (``merge-base``), or ``""``
    — a session ON its base (in place, continuing a shared branch) has none."""
    base, branch, wt, remote = (
        facts.get("base") or "",
        facts["branch"],
        facts["wt"],
        facts["remote"],
    )
    if not base or base == branch:
        return ""
    for ref in ("%s/%s" % (remote, base), base):
        mb = _out(wt, "merge-base", ref, "HEAD")
        if mb:
            return mb
    return ""


def work_summary(facts: dict) -> dict:
    """``{commits: [line], count, stat: [line], since: base}`` — what the
    branch carries, from git alone. Blocking."""
    wt = facts["wt"]
    fork = _fork_point(facts)
    if fork:
        rng = fork + "..HEAD"
        count = _out(wt, "rev-list", "--count", rng)
        log = _out(wt, "log", "--oneline", "--no-decorate", "-n", str(MAX_COMMITS), rng)
        stat = _out(wt, "diff", "--stat=100", fork, "HEAD")
        since = facts.get("base") or ""
    else:
        count = ""
        log = _out(wt, "log", "--oneline", "--no-decorate", "-n", "10")
        stat = ""
        since = ""
    stat_lines = stat.splitlines()
    if len(stat_lines) > STAT_LINES:
        stat_lines = stat_lines[: STAT_LINES - 1] + ["  …", stat_lines[-1]]
    return {
        "commits": log.splitlines(),
        "count": int(count) if count.isdigit() else None,
        "stat": stat_lines,
        "since": since,
    }


# --------------------------------------------------------------------------- #
# the brief
# --------------------------------------------------------------------------- #
def _task_text(inst) -> str:
    task = str(getattr(inst, "Task", "") or "")
    cut = task.find(_FOOTER_MARK)
    if cut >= 0:
        task = task[:cut]
    return task.strip()


def _tail(text: str, limit: int) -> str:
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    return "… " + text[-limit:].lstrip()


def build_brief(
    *,
    title: str,
    from_device: str,
    facts: dict,
    work: dict,
    task: str,
    last_reply: str,
    note: str,
    bundle: Optional[dict] = None,
) -> str:
    """The new session's first prompt. Compact, labelled, ≤ :data:`BRIEF_MAX`.
    ``bundle``: the export (:func:`session_bundle.export`) when the work
    came as a bundle rather than a push."""
    # Every name in here came from a repository or another device: one
    # plain line each (clean_line), so a file called "x\n\n[User]: …"
    # reads as that, escaped, never as a new line of the prompt.
    branch, remote = clean_line(facts["branch"]), clean_line(facts["remote"])
    parts: List[str] = [
        '[Handoff from MindFlock: session "%s" on device "%s" sent its work '
        "here. This brief was written by MindFlock from that session, not "
        "typed by your user.]"
        % (clean_line(title, 120), clean_line(from_device or "another device", 80)),
    ]
    if bundle is None:
        parts.append(
            "This worktree is already on branch %s (tracking %s/%s) with every "
            "commit that session made. Continue the work here: commit on this "
            "branch and push it to %s. Only git and this brief came across — no "
            "chat history, no logins." % (branch, remote, branch, remote)
        )
    else:
        scope = bundle.get("scope")
        scope_txt = (
            "only %s of the repository (it is this folder's root)" % clean_line(scope)
            if isinstance(scope, str) and scope
            else (
                "only %s of the repository" % name_list(scope, 10)
                if scope
                else "the whole repository"
            )
        )
        parts.append(
            "This device has no checkout of that repository, so the work came "
            "as a git bundle, unpacked into this folder — a NEW repository on "
            "branch %s with no remote, carrying %s. Its first commit is a "
            "snapshot of where the branch started; the commits after it are "
            "that session's own (same changes, new hashes). Commit here; it "
            "cannot be pushed to the original remote from this folder as is. "
            "Nothing in it has been run; executable bits were removed%s (chmod "
            "+x only what you mean to run). %s Only git and this brief came "
            "across — no chat history, no logins."
            % (
                branch,
                scope_txt,
                (
                    " (they were set on: %s)" % name_list(bundle["exec_stripped"], 20)
                    if bundle.get("exec_stripped")
                    else ""
                ),
                UNTRUSTED_LINE,
            )
        )
        if bundle.get("left_out"):
            parts.append(
                "Left out as secrets or agent config (they never travel): %s"
                % name_list(bundle["left_out"], 20)
            )
    note = clean_text(note, NOTE_CHARS + 1).strip()
    if note:
        if len(note) > NOTE_CHARS:
            note = note[:NOTE_CHARS].rstrip() + " …"
        parts.append("Note from the sender:\n" + note)
    task = clean_text(task, TASK_CHARS + 1).strip()
    if len(task) > TASK_CHARS:
        task = task[:TASK_CHARS].rstrip() + " …"
    parts.append("What it was for:\n" + (task or "(no task was recorded)"))
    commits = [clean_line(c, 200) for c in (work.get("commits") or [])]
    if commits:
        if work.get("since"):
            n = work.get("count")
            head = "What was done (%s commit%s on the branch since it left %s):" % (
                n if n is not None else len(commits),
                "" if n == 1 else "s",
                clean_line(work["since"]),
            )
        else:
            head = "Latest commits on the branch:"
        body = "\n".join(commits)
        if work.get("stat"):
            body += "\n\n" + "\n".join(clean_line(x, 200) for x in work["stat"])
        parts.append(head + "\n" + body)
    else:
        parts.append("What was done: no commits on the branch yet.")
    reply = _tail(clean_text(last_reply, 1_000_000), REPLY_CHARS)
    parts.append(
        "Where it left off (its agent's last reply):\n"
        + (reply or "(no reply recorded)")
    )
    text = "\n\n".join(parts)
    if len(text) > BRIEF_MAX:
        text = text[: BRIEF_MAX - 2].rstrip() + " …"
    return text


# --------------------------------------------------------------------------- #
# the device
# --------------------------------------------------------------------------- #
def _device_names() -> List[str]:
    try:
        return [d["device"] for d in _remote.devices_json().get("devices") or []]
    except Exception:  # noqa: BLE001
        return []


def resolve_device(name: str) -> dict:
    """The linked, answering device called ``name``, or a :class:`Refusal`."""
    want = str(name or "").strip()
    if not want:
        raise Refusal(400, "no_device", "name the device to send it to (list_devices)")
    if "::" in want or "/" in want:
        raise Refusal(400, "no_device", "%r is not a device name" % want)
    dev = _remote.find_device(want)
    if dev is None:
        if want.lower() == str(_remote.self_identity().get("key") or "").lower():
            raise Refusal(
                400, "same_device", "%s is this device — pick another one" % want
            )
        names = _device_names()
        close = difflib.get_close_matches(
            want.lower(), [n.lower() for n in names], n=1, cutoff=0.6
        )
        guess = [n for n in names if close and n.lower() == close[0]]
        raise Refusal(
            404,
            "no_device",
            "no device named %s%s%s. A device shows up once MindFlock runs "
            "there and it is one of your devices (Settings → Your devices); "
            "list_devices names them"
            % (
                want,
                " — did you mean %s?" % guess[0] if guess else "",
                " (known: %s)" % ", ".join(names) if names else "",
            ),
            **({"did_you_mean": guess[0]} if guess else {}),
        )
    key = dev["key"]
    if not _remote.is_linked(dev):
        raise Refusal(
            409,
            "not_linked",
            "%s is on your tailnet but is not one of your devices — add it in "
            "Settings → Your devices first; a session's work only goes to your "
            "own linked devices" % key,
        )
    if not dev.get("reachable"):
        raise Refusal(
            409,
            "device_offline",
            "%s is not answering right now (asleep, offline, or MindFlock is not "
            "running there) — wake it and send again; nothing was pushed" % key,
        )
    if not dev.get("remote_control"):
        raise Refusal(
            409,
            "remote_control_off",
            "%s does not accept sessions from other devices — turn on Settings → "
            "Your devices → Advanced → Allow remote control there, then send "
            "again" % key,
        )
    if not _remote.is_connected(dev):
        raise Refusal(
            409,
            "not_connected",
            "%s needs pairing again before it takes sessions from here "
            "(Settings → Your devices)" % key,
        )
    return dev


def _unreachable(key: str, after: str = "") -> Refusal:
    """``after``: what already happened (``"The branch IS pushed"``), when
    something did — the refusal then says so instead of "nothing was
    pushed"."""
    if after:
        return Refusal(
            502,
            "device_offline",
            "%s stopped answering before the session started there. %s; send "
            "again when it is back" % (key, after),
        )
    return Refusal(
        409,
        "device_offline",
        "%s is not answering right now (asleep, offline, or MindFlock is not "
        "running there) — wake it and send again; nothing was pushed" % key,
    )


def _auth_refusal(key: str, status: int) -> Refusal:
    if status == 403:
        return Refusal(
            409,
            "remote_control_off",
            "%s refused: remote control is off there — turn on Settings → Your "
            "devices → Advanced → Allow remote control on it" % key,
        )
    return Refusal(
        409,
        "not_connected",
        "%s did not accept this device's credential — pair it again in Settings "
        "→ Your devices" % key,
    )


def _repo_name(url: str, wt: str) -> str:
    tail = url.rstrip("/").rsplit("/", 1)[-1].rsplit(":", 1)[-1]
    if tail.endswith(".git"):
        tail = tail[: -len(".git")]
    return tail or os.path.basename(wt.rstrip("/"))


async def find_repo(dev: dict, facts: dict) -> Tuple[str, str, str]:
    """``(path on the device, remote name there, how it was matched)`` for the
    session's repository, or a :class:`Refusal` naming the remedy."""
    key, url = dev["key"], facts["url"]
    status, body = await _remote.get_json(
        dev, "/api/repos/find?url=" + urllib.parse.quote(url, safe=""), FIND_TIMEOUT_S
    )
    if status == 0:
        raise _unreachable(key)
    if status in (401, 403):
        raise _auth_refusal(key, status)
    if status == 200 and isinstance(body, dict):
        for m in body.get("matches") or []:
            if isinstance(m, dict) and m.get("path"):
                return str(m["path"]), str(m.get("remote") or "origin"), "url"
    elif status == 404:
        # A MindFlock from before /api/repos/find: match by folder name. The
        # create's own fetch of <remote>/<branch> then proves it is the repo.
        want = _repo_name(url, facts["wt"]).lower()
        status, body = await _remote.get_json(dev, "/api/repos/suggest", FIND_TIMEOUT_S)
        if status == 0:
            raise _unreachable(key)
        rows = body.get("suggestions") if isinstance(body, dict) else None
        for s in rows if isinstance(rows, list) else []:
            if (
                isinstance(s, dict)
                and s.get("is_git")
                and str(s.get("name") or "").lower() == want
            ):
                return str(s["path"]), "origin", "name"
    raise Refusal(
        409,
        "repo_missing",
        "%s has no checkout of %s. Clone it there (git clone %s), or open it in "
        "a MindFlock session there once, then send again — nothing was pushed"
        % (key, url, url),
        url=url,
    )


def _title_taken(err: str) -> bool:
    e = err.lower()
    return (
        "already exists" in e and ("instance " in e or "session named" in e)
    ) or "already named" in e


async def _create(dev: dict, title: str, body: dict, after: str) -> Tuple[str, dict]:
    """POST the create on the device, stepping the title past a name already
    taken there (``tripwire`` → ``tripwire-2``). → ``(title, response)``.
    ``after``: the sentence saying what already happened (``"The branch IS
    pushed"``), carried by every refusal from here on."""
    key = dev["key"]
    for n in range(TITLE_TRIES):
        name = title if n == 0 else "%s-%d" % (title, n + 1)
        status, resp = await _remote.post_json(
            dev, "/api/instances", dict(body, title=name), CREATE_TIMEOUT_S
        )
        if status in (200, 201, 202):
            return name, resp if isinstance(resp, dict) else {}
        if status == 0:
            raise _unreachable(key, after)
        if status in (401, 403):
            raise _auth_refusal(key, status)
        err = ""
        if isinstance(resp, dict):
            err = str(resp.get("detail") or resp.get("error") or "")
            if resp.get("memory_confirm"):
                err = "it is low on memory (%s)" % (resp.get("message") or err)
        if status == 409 and _title_taken(str((resp or {}).get("error") or err)):
            continue
        raise Refusal(
            502,
            "device_refused",
            "%s refused to start the session: %s. %s"
            % (key, err or "HTTP %d" % status, after),
            device_status=status,
            device_error=err[:600],
        )
    raise Refusal(
        409,
        "title_taken",
        "%s already has sessions named %s … %s-%d — close one there, then send "
        "again" % (key, title, title, TITLE_TRIES),
    )


# --------------------------------------------------------------------------- #
# the whole send
# --------------------------------------------------------------------------- #
VIAS = ("auto", "git", "bundle")


def _fetch_failed(r: Refusal) -> bool:
    """A device refusal because ITS fetch of the pushed branch failed (no
    access to the remote from there)."""
    err = str(r.extra.get("device_error") or r.message).lower()
    return r.code == "device_refused" and (
        "fetch failed" in err or "is not a remote-tracking branch" in err
    )


async def send(
    inst,
    title: str,
    device: str,
    note: str = "",
    *,
    via: str = "auto",
    paths: Optional[List[str]] = None,
    last_reply: Callable[[], str] = lambda: "",
    breaches: Callable[[str], list] = lambda wt: [],
    to_thread=None,
) -> Tuple[int, dict]:
    """Carry ``title`` to ``device`` → ``(status, body)``. ``last_reply`` and
    ``breaches`` are the server's (the agent's newest reply; committed
    red-zone breaches for a push of ``wt``), injected so this module never
    reaches into the server. See the module docstring for the steps.

    ``via``: ``"git"`` = push + the device's own checkout (refused when it has
    none); ``"bundle"`` = always a bundle; ``"auto"`` (default) = git when
    the device has a checkout of the repository, else — or when the repo has
    no remote, or the device cannot fetch from it — a bundle. ``paths``: send
    only these paths of the repository, as a bundle (implies ``bundle``)."""
    import asyncio

    run = to_thread or asyncio.to_thread
    try:
        if via not in VIAS:
            raise Refusal(400, "bad_request", "via is one of auto, git, bundle")
        paths = [p for p in (paths or []) if isinstance(p, str) and p.strip()]
        if paths and via == "git":
            raise Refusal(
                400,
                "bad_request",
                "paths go only with a bundle (via: bundle or auto); a git send "
                "carries the whole branch",
            )
        dev = resolve_device(device)
        facts = await run(local_facts, inst, title)
        try:
            hits = await run(breaches, facts["wt"])
        except Exception:  # noqa: BLE001 — the gate fails open on its own bugs
            hits = []
        if hits:
            first = hits[0]
            raise Refusal(
                409,
                "red_zone",
                "red zone breached on %s: %s%s — the Push button can override "
                "that, send cannot; nothing was pushed"
                % (
                    facts["branch"],
                    first.get("path"),
                    " and %d more" % (len(hits) - 1) if len(hits) > 1 else "",
                ),
                red_zone_breaches=hits,
            )
        reason = ""
        if paths or via == "bundle":
            reason = (
                "only %s was asked for" % ", ".join(paths)
                if paths
                else "a bundle was asked for"
            )
        elif not facts["url"]:
            if via == "git":
                raise _no_remote(title, facts)
            reason = "the repository has no %s remote" % facts["remote"]
        else:
            try:
                found = await find_repo(dev, facts)
            except Refusal as r:
                if r.code != "repo_missing" or via == "git":
                    raise
                reason = "%s has no checkout of %s" % (dev["key"], facts["url"])
        if not reason:
            try:
                return await _send_git(
                    inst, title, dev, facts, found, note, last_reply, run
                )
            except Refusal as r:
                if via == "git" or not _fetch_failed(r):
                    raise
                reason = "%s could not fetch the branch from %s" % (
                    dev["key"],
                    facts["remote"],
                )
        return await _send_bundle(
            inst, title, dev, facts, note, paths, reason, last_reply, run
        )
    except Refusal as r:
        return r.status, r.body()


async def _brief(inst, title, facts, work, note, last_reply, run, bundle=None) -> str:
    try:
        reply = await run(last_reply)
    except Exception:  # noqa: BLE001 — the brief says "(no reply recorded)"
        reply = ""
    return build_brief(
        title=title,
        from_device=str(_remote.self_identity().get("key") or ""),
        facts=facts,
        work=work,
        task=_task_text(inst),
        last_reply=reply,
        note=note,
        bundle=bundle,
    )


def _commit_count(work: dict) -> int:
    if work.get("count") is not None:
        return work["count"]
    return len(work.get("commits") or [])


async def _send_git(inst, title, dev, facts, found, note, last_reply, run):
    path, there_remote, matched = found
    pushed = await run(push, facts)
    work = await run(work_summary, facts)
    brief = await _brief(inst, title, facts, work, note, last_reply, run)
    body: Dict[str, object] = {
        "repo_path": path,
        "base_ref": "%s/%s" % (there_remote, facts["branch"]),
        "continue_branch": facts["branch"],
        "prompt": brief,
        "spawned": True,
    }
    program = str(getattr(inst, "Program", "") or "")
    if program:
        body["program"] = program
    after = "The branch %s pushed" % ("IS" if pushed["pushed"] else "was already")
    name, resp = await _create(dev, title, body, after)
    await _remote.refresh_instances(dev["key"])
    caveats: List[str] = []
    if matched == "name":
        caveats.append(
            "%s's MindFlock is older: its checkout was matched by folder name (%s)"
            % (dev["key"], path)
        )
    if resp.get("queued"):
        caveats.append(
            "%s is short on memory: the session is queued and starts when there "
            "is room" % dev["key"]
        )
    if name != title:
        caveats.append("%s already had a session named %s" % (dev["key"], title))
    return 200, {
        "ok": True,
        "via": "git",
        "how": "pushed %s to %s; %s works on it in its own checkout at %s"
        % (facts["branch"], facts["remote"], dev["key"], path),
        "title": _remote.join_title(dev["key"], name),
        "device": dev["key"],
        "remote_title": name,
        "branch": facts["branch"],
        "remote": facts["remote"],
        "url": facts["url"],
        "head": facts["head"][:12],
        "pushed": pushed["pushed"],
        "already_pushed": pushed["already"],
        "commits": _commit_count(work),
        "repo_path": path,
        "queued": bool(resp.get("queued")),
        "caveats": caveats,
    }


async def _send_bundle(inst, title, dev, facts, note, paths, reason, last_reply, run):
    import shutil
    import tempfile

    from backend.web.core import send_files as _send_files
    from backend.web.core import session_bundle as _bundle

    lim = _send_files.limits()
    work = await run(work_summary, facts)
    # The repository's own list of secret-holding files (detect-secrets'
    # .secrets.baseline, repo-relative) is left out of the bundle too.
    top = await run(_out, facts["wt"], "rev-parse", "--show-toplevel")
    extra = await run(_send_files.baseline_paths, top or facts["wt"])
    tmp = tempfile.mkdtemp(prefix="mf-send-")
    try:
        try:
            exp = await run(
                lambda: _bundle.export(
                    facts["wt"],
                    facts["branch"],
                    _fork_point(facts),
                    tmp,
                    paths=paths,
                    classify=lambda rel: _send_files.classify(rel, extra),
                )
            )
        except _bundle.BundleError as err:
            raise Refusal(
                400 if err.code in ("bad_path", "bad_branch") else 500,
                err.code,
                "could not bundle %s: %s — nothing was sent"
                % (facts["branch"], err.message),
            ) from None
        if exp["size"] > lim["max_bytes"]:
            raise Refusal(
                413,
                "too_large",
                "the bundle of %s is %s, over the %s limit for one transfer — "
                'send only the folder the work is in (paths=["<folder>"]), or '
                "raise transfers.max_mb in Settings; nothing was sent"
                % (
                    facts["branch"],
                    _send_files.human(exp["size"]),
                    _send_files.human(lim["max_bytes"]),
                ),
                suggest_paths=_changed_top(facts),
            )
        sha, size = await run(_send_files._sha256, exp["path"])
        wire = [
            {
                "path": exp["name"] + ".bundle",
                "size": size,
                "sha256": sha,
                "exec": False,
            }
        ]
        manifest = {
            "v": 1,
            "kind": "bundle",
            "from_device": str(_remote.self_identity().get("key") or ""),
            "from_session": title,
            "note": (note or "")[:NOTE_CHARS],
            "to_session": "",
            "files": wire,
            "total_size": size,
            "total_sha256": _send_files.total_sha256(wire),
            "bundle": {"branch": facts["branch"], "name": exp["name"]},
        }
        landed = await _send_files.transfer(dev, manifest, [exp["path"]])
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    path = str(landed.get("path") or "")
    exp["exec_stripped"] = landed.get("exec_stripped") or []
    brief = await _brief(inst, title, facts, work, note, last_reply, run, bundle=exp)
    body: Dict[str, object] = {
        "repo_path": path,
        "in_place": True,
        "prompt": brief,
        "spawned": True,
    }
    program = str(getattr(inst, "Program", "") or "")
    if program:
        body["program"] = program
    after = "The work IS unpacked at %s there" % path
    name, resp = await _create(dev, title, body, after)
    await _remote.refresh_instances(dev["key"])
    caveats: List[str] = []
    if resp.get("queued"):
        caveats.append(
            "%s is short on memory: the session is queued and starts when there "
            "is room" % dev["key"]
        )
    if name != title:
        caveats.append("%s already had a session named %s" % (dev["key"], title))
    if exp["left_out"]:
        caveats.append(
            "left out as secrets/agent config: %s" % name_list(exp["left_out"], 10)
        )
    if exp["folded"]:
        caveats.append(
            "the %d oldest commits were folded into the snapshot" % exp["folded"]
        )
    return 200, {
        "ok": True,
        "via": "bundle",
        "how": "%s, so %s went as a git bundle (%s, %d commit%s on a snapshot of "
        "where it started) unpacked into a new repository at %s there; nothing "
        "was pushed"
        % (
            reason,
            facts["branch"],
            _send_files.human(size),
            exp["commits"],
            "" if exp["commits"] == 1 else "s",
            path,
        ),
        "title": _remote.join_title(dev["key"], name),
        "device": dev["key"],
        "remote_title": name,
        "branch": facts["branch"],
        "head": facts["head"][:12],
        "pushed": False,
        "already_pushed": False,
        "commits": _commit_count(work),
        "repo_path": path,
        "bundle": {
            "size": size,
            "sha256": sha,
            "commits": exp["commits"],
            "scope": exp["scope"],
            "left_out": exp["left_out"],
            "transfer_id": landed.get("id"),
        },
        "queued": bool(resp.get("queued")),
        "caveats": caveats,
    }


def _changed_top(facts: dict) -> List[str]:
    """The top folders the branch's own changes are in (a hint for
    ``paths``)."""
    fork = _fork_point(facts)
    if not fork:
        return []
    names = _out(facts["wt"], "diff", "--name-only", fork, "HEAD").splitlines()
    tops = sorted({n.split("/", 1)[0] for n in names if "/" in n})
    return tops[:5]
