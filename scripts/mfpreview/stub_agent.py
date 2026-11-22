"""The preview's stub ``claude``: a scripted screen instead of an agent.

:func:`render` returns the executable written to ``<scratch>/bin/claude``,
with the scratch paths baked in. Each session finds its ROLE in
``<scratch>/roles.json`` by its tmux session name (``mindflock_<title>``)::

    {"<title>": {"screen": "working" | "idle" | "ask" | "crash",
                 "lines": ["what the agent said", ...],
                 "ask": {"tool": "Bash command", "command": "uv add redis",
                         "why": "Add redis as a dependency"},
                 "files": {"path/in/worktree": "content"},   # written once
                 "commit": "message"}}                       # files committed

The screens copy the layouts the claude provider classifies
(``tests/unit/data/dialogs``): ``esc to interrupt`` under a rising token
counter reads as working; a ``Do you want to proceed?`` menu parses as a
question with answer buttons; a quiet prompt box settles to idle; ``crash``
exits 1 so the session goes offline. The stub also answers what the server
asks a CLI out of band (``--version``, ``agents``, ``-p``) at once, and it is
interactive enough to look alive: any key on a question, or a typed line +
Enter on the prompt, turns it "working" for a few seconds, then idle again.

The roles file is re-read every couple of seconds, so a scenario can move a
session to another screen while the preview runs. Nothing here calls a model,
the network or a real ``claude``.
"""

from __future__ import annotations

STUB = r"""#!{python}
# MindFlock preview stub "claude" (scripts/mfpreview/stub_agent.py).
import json, os, re, select, subprocess, sys, time

ROLES = {roles!r}
MARKERS = {markers!r}
TMUX = {tmux!r}
GITCONFIG = {gitconfig!r}
SUBCOMMANDS = ("auth", "login", "logout", "doctor", "mcp", "update", "config",
               "install", "setup-token", "plugin", "migrate-installer")

args = sys.argv[1:]
if "--version" in args or "-v" in args:
    print("2.1.0 (MindFlock preview stub)")
    sys.exit(0)
if args and args[0] == "agents":
    sys.exit(1)
if args and args[0] in SUBCOMMANDS:
    sys.exit(0)
if "-p" in args or "--print" in args:
    print("(preview stub) no model is called in the MindFlock preview.")
    sys.exit(0)


def session_name():
    pane = os.environ.get("TMUX_PANE", "")
    if not pane:
        return ""
    try:
        out = subprocess.run([TMUX, "display-message", "-p", "-t", pane, "#S"],
                             capture_output=True, text=True, timeout=5)
        return out.stdout.strip()
    except Exception:
        return ""


def tmux_name(title):
    return "mindflock_" + re.sub(r"\s+", "", title).replace(".", "_")


NAME = session_name()


def load_role():
    try:
        roles = json.load(open(ROLES))
    except Exception:
        return "", {{}}
    best = ""
    for title in roles:
        t = tmux_name(title)
        if NAME == t or (NAME.startswith(t) and len(title) > len(best)):
            if NAME == t:
                return title, roles[title]
            best = title
    return best, (roles.get(best) or {{}})


def write_marker(state):
    if not NAME:
        return
    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", NAME)
    try:
        os.makedirs(MARKERS, exist_ok=True)
        tmp = os.path.join(MARKERS, "." + safe + ".tmp")
        with open(tmp, "w") as f:
            json.dump({{"state": state, "ts": time.time()}}, f)
        os.replace(tmp, os.path.join(MARKERS, safe + ".json"))
    except OSError:
        pass


def first_run(role):
    files = role.get("files") or {{}}
    for rel, text in files.items():
        path = os.path.join(os.getcwd(), rel)
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(path, "w") as f:
            f.write(text)
    if files and role.get("commit"):
        env = dict(os.environ, GIT_CONFIG_GLOBAL=GITCONFIG)
        subprocess.run(["git", "add", "-A"], env=env, capture_output=True)
        subprocess.run(["git", "commit", "-qm", role["commit"]], env=env, capture_output=True)


def width():
    try:
        return max(40, os.get_terminal_size().columns)
    except OSError:
        return 100


def height():
    try:
        return max(10, os.get_terminal_size().lines)
    except OSError:
        return 30


def header(title):
    return [
        "",
        " * Claude Code (MindFlock preview stub)",
        "   no model, no login: a scripted screen for " + (title or "this session"),
        "",
    ]


def transcript(role, extra):
    out = []
    prompt = role.get("prompt")
    if prompt:
        out += ["> " + prompt, ""]
    for line in (role.get("lines") or []) + extra:
        out += [("● " + line) if not line.startswith(" ") else line, ""]
    return out


def prompt_box(footer):
    rule = "─" * width()
    return [rule, "❯ ", rule, "  " + footer]


def paint(lines, bottom):
    h = height()
    body = lines[-max(1, h - len(bottom) - 1):]
    pad = max(0, h - len(body) - len(bottom) - 1)
    sys.stdout.write("\033[2J\033[H" + "\n".join(body + [""] * pad + bottom))
    sys.stdout.flush()


SPIN = "✢✳✶✻✽"
VERBS = ["Thinking", "Reading", "Editing", "Testing", "Sublimating"]


def screen(mode, title, role, extra, t0, tokens):
    top = header(title) + transcript(role, extra)
    if mode == "working":
        secs = int(time.time() - t0)
        spin = SPIN[secs % len(SPIN)]
        verb = VERBS[(secs // 6) % len(VERBS)]
        line = "%s %s… (%ds · ↓ %d tokens · thinking)" % (spin, verb, secs, tokens)
        paint(top + [line, ""], prompt_box("⏸ manual mode on · esc to interrupt"))
    elif mode == "ask":
        ask = role.get("ask") or {{}}
        rule = "─" * width()
        cmd = ask.get("command") or "uv add redis"
        why = ask.get("why") or "Add redis as a dependency"
        bottom = [rule, " " + (ask.get("tool") or "Bash command"), "",
                  "   " + cmd, "   " + why, "",
                  " Do you want to proceed?",
                  " ❯ 1. Yes",
                  "   2. Yes, and don't ask again for this command",
                  "   3. No, and tell Claude what to do differently (esc)",
                  "", " Esc to cancel · Tab to amend"]
        paint(top, bottom)
    else:
        paint(top, prompt_box("? for shortcuts"))


def read_keys(timeout):
    if not sys.stdin.isatty():
        time.sleep(timeout)
        return ""
    r, _, _ = select.select([sys.stdin], [], [], timeout)
    if not r:
        return ""
    try:
        return os.read(sys.stdin.fileno(), 1024).decode("utf-8", "replace")
    except OSError:
        return ""


def main():
    title, role = load_role()
    if NAME:
        done = os.path.join(os.environ.get("TMPDIR", "/tmp"), "stub-ran-" + NAME)
        if not os.path.exists(done):
            first_run(role)
            open(done, "w").close()
    if role.get("screen") == "crash":
        sys.stdout.write("\n".join(header(title) + [
            "Error: the agent process crashed (preview stub, on purpose).",
            "  at main (cli.js:1:1)", ""]) + "\n")
        sys.stdout.flush()
        time.sleep(1.5)
        sys.exit(1)
    try:
        import termios, tty
        saved = termios.tcgetattr(sys.stdin.fileno()) if sys.stdin.isatty() else None
        if saved:
            tty.setcbreak(sys.stdin.fileno())
    except Exception:
        saved = None
    mode = role.get("screen") or "idle"
    extra, typed = [], ""
    t0, tokens = time.time(), 120
    busy_until = 0.0
    last_marker = 0.0
    last_roles = 0.0
    shown = None
    try:
        while True:
            now = time.time()
            if now - last_roles > 2 and not busy_until:
                last_roles = now
                title, role = load_role()
                want = role.get("screen") or "idle"
                if want != mode and want in ("working", "idle", "ask"):
                    mode, t0 = want, now
            if busy_until and now > busy_until:
                busy_until = 0.0
                mode = "idle"
                extra.append("(preview stub) Done — nothing was really changed.")
            marker = {{"working": "working", "ask": "clarify"}}.get(mode, "idle")
            if marker != shown or (mode == "working" and now - last_marker > 15):
                write_marker(marker)
                shown, last_marker = marker, now
            if mode == "working":
                tokens += 37
            screen(mode, title, role, extra, t0, tokens)
            keys = read_keys(1.0)
            if not keys:
                continue
            if mode == "ask":
                extra.append("Ran " + ((role.get("ask") or {{}}).get("command") or "the command"))
                mode, t0, busy_until = "working", time.time(), time.time() + 5
            elif mode == "idle":
                for ch in keys:
                    if ch in "\r\n":
                        if typed.strip():
                            extra.append("  > " + typed.strip())
                            mode, t0, busy_until = "working", time.time(), time.time() + 6
                        typed = ""
                    elif ch in "\x7f\b":
                        typed = typed[:-1]
                    elif ch.isprintable():
                        typed += ch
    finally:
        if saved:
            import termios
            termios.tcsetattr(sys.stdin.fileno(), termios.TCSADRAIN, saved)


main()
"""


def render(python: str, roles: str, markers: str, tmux: str, gitconfig: str) -> str:
    """The stub's source with the scratch paths baked in. The interpreter is
    named by absolute path in the shebang (not ``/usr/bin/env``), so the
    process keeps the name ``claude`` the engine's process check looks for."""
    return STUB.format(
        python=python, roles=roles, markers=markers, tmux=tmux, gitconfig=gitconfig
    )
