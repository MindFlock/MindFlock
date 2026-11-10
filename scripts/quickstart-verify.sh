#!/bin/sh
# Quickstart verifier — the README's Quickstart, scripted, so it can never rot.
#
# Runs the exact first-ten-minutes flow against a throwaway repo:
#   1. `mindflock doctor`
#   2. `mindflock serve local` from a fresh git repo (background)
#   3. `mindflock new` — session = git worktree + tmux + agent
#   4. `mindflock ls` — verify the session reports state
#   5. `mindflock rm --wipe --yes` — tear down cleanly (worktree and branch removed)
#
# CI runs this with a STUB agent binary (no API credentials needed):
#   MINDFLOCK_STUB_AGENT=1 sh scripts/quickstart-verify.sh
# On a real machine with claude installed/authenticated, run it bare.
set -eu

say()  { printf '\033[1;35m[quickstart]\033[0m %s\n' "$*"; }
fail() {
  printf '\033[1;31m[quickstart] FAIL:\033[0m %s\n' "$*" >&2
  diagnose >&2 || true
  cleanup
  exit 1
}

PORT="${MINDFLOCK_QS_PORT:-8971}"
WORK="$(mktemp -d)"
SERVER_PID=""

cleanup() {
  [ -n "$SERVER_PID" ] && kill "$SERVER_PID" 2>/dev/null || true
  tmux kill-session -t mindflock_qs-demo 2>/dev/null || true
  rm -rf "$WORK" 2>/dev/null || true
}
trap cleanup EXIT INT TERM

# The interpreter the installed `mindflock` runs on (its entry point's
# shebang): a uv tool install brings its own CPython when the system has no
# 3.12 (Debian bookworm), and that build's os.posix_spawn differs from a
# distro one — the engine's PTY spawn is probed with it, not with python3.
mf_python() {
  sed -n '1s/^#![[:space:]]*//p' "$(command -v mindflock)" | cut -d' ' -f1
}

# On failure: the server log, then the engine's PTY spawn
# (mux.spawn_pty, what `tmux new-session -d` goes through at session create)
# re-run by hand on a private tmux socket — argv, env keys, the exception or
# the child's exit status, and everything it printed (tmux's stderr).
diagnose() {
  if [ -f "$WORK/server.log" ]; then
    printf '%s\n' '----- server.log (last 200 lines) -----'
    tail -n 200 "$WORK/server.log" || true
    printf '%s\n' '----- end server.log -----'
  fi
  PY="$(mf_python)"
  [ -x "$PY" ] || { echo "spawn diagnostic: no interpreter found for mindflock ($PY)"; return 0; }
  printf '%s\n' '----- engine PTY spawn diagnostic -----'
  env -u TMUX "$PY" -I - "$WORK" <<'EOF' || true
import os, sys, time, traceback
from backend.session import mux
sock = "qs-diag-%d" % os.getpid()
argv = ["tmux", "-L", sock, "new-session", "-d", "-s", "diag", "-c", sys.argv[1], "sleep 5"]
print("python:", sys.version.split()[0], sys.executable)
print("argv:", argv)
print("env keys:", sorted(os.environ))
try:
    proc = mux.spawn_pty(argv)
except BaseException:
    traceback.print_exc(file=sys.stdout)
else:
    out = b""
    end = time.time() + 10
    while time.time() < end:
        try:
            chunk = os.read(proc.fd, 4096)
        except OSError:
            break
        if not chunk:
            break
        out += chunk
    _, status = os.waitpid(proc.pid, 0)
    print("child exit status:", os.waitstatus_to_exitcode(status))
    print("child output (tmux stderr):", out.decode("utf-8", "replace") or "<none>")
    rc = os.system("tmux -L %s has-session -t diag" % sock)
    print("tmux has-session:", "yes" if rc == 0 else "no (%s)" % rc)
    os.system("tmux -L %s kill-server 2>/dev/null" % sock)
EOF
  printf '%s\n' '----- end diagnostic -----'
}

command -v mindflock >/dev/null 2>&1 || fail "mindflock is not installed (run install.sh first)"

# --- optional stub agent (CI: no real credentials ever) -----------------------
if [ "${MINDFLOCK_STUB_AGENT:-0}" = "1" ]; then
  say "installing stub 'claude' agent (CI mode — no credentials involved)"
  mkdir -p "$WORK/bin"
  cat > "$WORK/bin/claude" <<'EOF'
#!/bin/sh
# Stub coding agent for CI: accepts any args, announces itself, then idles so
# the tmux session stays alive like a real agent waiting for input.
echo "[stub-claude] started with args: $*"
while true; do sleep 60; done
EOF
  chmod +x "$WORK/bin/claude"
  PATH="$WORK/bin:$PATH"
  # Minimal login evidence so `doctor`'s auth probe passes without a real login.
  if [ ! -e "$HOME/.claude.json" ]; then
    printf '{"oauthAccount": {"stub": true}}\n' > "$HOME/.claude.json"
  fi
fi

# Which posix_spawn the engine's PTY gets on this interpreter (see
# backend/session/mux.py spawn_pty): CPython's own setsid=, or libc's
# POSIX_SPAWN_SETSID through ctypes when the build lacks it.
PY="$(mf_python)"
if [ -x "$PY" ]; then
  say "interpreter: $("$PY" -I -c 'import os, sys
try:
    os.posix_spawn("/nonexistent", ["x"], {}, setsid=True)
except NotImplementedError:
    s = "unavailable (libc fallback)"
except OSError:
    s = "native"
print(sys.version.split()[0], "- os.posix_spawn setsid:", s)' 2>&1)"
fi

# --- 1. doctor -----------------------------------------------------------------
say "1/5 mindflock doctor"
mindflock doctor || [ "${MINDFLOCK_QS_ALLOW_DOCTOR_FAIL:-0}" = "1" ] || fail "doctor reported missing required dependencies"

# --- 2. serve from a throwaway repo ---------------------------------------------
say "2/5 creating a throwaway git repo + mindflock serve local (port $PORT)"
REPO="$WORK/demo-repo"
mkdir -p "$REPO"
git init -q "$REPO"
git -C "$REPO" config user.email "qs@example.com"
git -C "$REPO" config user.name "quickstart"
echo "hello" > "$REPO/README.md"
git -C "$REPO" add README.md
git -C "$REPO" commit -qm "initial"

( cd "$REPO" && mindflock serve local --port "$PORT" >"$WORK/server.log" 2>&1 ) &
SERVER_PID=$!

i=0
until curl -fsS "http://127.0.0.1:$PORT/api/doctor" >/dev/null 2>&1; do
  i=$((i + 1))
  [ "$i" -gt 60 ] && { cat "$WORK/server.log" >&2 || true; fail "server did not come up on port $PORT"; }
  sleep 1
done
say "server is up"

# --- 3. create a session ---------------------------------------------------------
say "3/5 mindflock new (worktree + tmux + agent)"
MINDFLOCK_PORT="$PORT" mindflock new "$REPO" -t qs-demo -p "say hello" \
  || fail "mindflock new failed"

# Wait for the session to leave 'loading' and verify it reports state.
i=0
while :; do
  STATUS="$(MINDFLOCK_PORT="$PORT" mindflock ls --json | python3 -c '
import json,sys
rows = json.load(sys.stdin)
match = [r for r in rows if r.get("title") == "qs-demo"]
print(match[0].get("status", "") if match else "GONE")
')"
  [ "$STATUS" = "GONE" ] && fail "session vanished during startup (see server.log)"
  [ "$STATUS" != "loading" ] && [ -n "$STATUS" ] && break
  i=$((i + 1))
  [ "$i" -gt 90 ] && fail "session stuck in 'loading' after 90s"
  sleep 1
done
say "session status: $STATUS"

# --- 4. state reporting -----------------------------------------------------------
say "4/5 mindflock ls"
MINDFLOCK_PORT="$PORT" mindflock ls
tmux has-session -t mindflock_qs-demo 2>/dev/null || fail "tmux session mindflock_qs-demo not found"
say "tmux session exists"

# --- 5. teardown ------------------------------------------------------------------
say "5/5 mindflock rm --wipe --yes"
MINDFLOCK_PORT="$PORT" mindflock rm --wipe qs-demo --yes || fail "mindflock rm --wipe failed"
MINDFLOCK_PORT="$PORT" mindflock ls --json | grep -q '"qs-demo"' && fail "session still listed after rm"

say "PASS — install → serve → new → ls → rm all verified"
