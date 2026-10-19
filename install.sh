#!/bin/sh
# MindFlock installer — safe to re-run, no root, no repo clone, no Python setup
# needed beforehand.
#
#   curl -LsSf https://raw.githubusercontent.com/MindFlock/MindFlock/main/install.sh | sh
#
# Option (after `sh -s --`):
#   --join 'DEVICE CODE'  after installing, start the server, sign in to
#                         Tailscale if needed, and join DEVICE's devices with
#                         the code it showed (Settings → Devices → Add a device
#                         prints the whole line, pinned to its own version).
#
# What it does (and prints as it goes):
#   1. Checks the platform (Linux, macOS, or WSL; native Windows is refused
#      with a pointer to WSL2).
#   2. Installs `uv` (Astral's Python manager) via its official installer if
#      it isn't already present — into ~/.local/bin, no root. The installer is
#      version-pinned and sha256-verified before it runs (no blind curl|sh).
#   3. `uv tool install "mindflock[web]"` straight from GitHub — the newest
#      RELEASE by default (MINDFLOCK_INSTALL_REF picks another tag or a
#      branch, e.g. main for unreleased code), first resolved to a full commit
#      SHA (printed, and pinned for the install so what you audited is what
#      you get). uv fetches a suitable Python by itself, builds in an isolated
#      venv, and links the `mindflock` command into ~/.local/bin. Re-running
#      upgrades in place — and restarts a MindFlock server already running on
#      this machine so it serves the new version (step 5). What was installed
#      (method, ref, commit, Python, time) is recorded in
#      ~/.mindflock/install.json so `mindflock update` follows the same path.
#   4. Ends on the same three steps `mindflock init` and `mindflock serve`
#      print (`mindflock init --steps`: install the tools, sign in to your
#      agent, pick a project folder — ✓/✗ each) and ONE next action: run
#      `mindflock init`, which installs whatever is missing (one confirmation,
#      one sudo prompt), signs your agent in and picks the folder. It installs
#      nothing itself past MindFlock, so nobody is walked through the tools
#      twice. The desktop app's run (MINDFLOCK_NONINTERACTIVE=1) gets one line
#      instead: the app's own Setup installs tmux and your agent next.
#
# Overrides (env vars):
#   MINDFLOCK_INSTALL_REPO   git URL to install from
#                            (default https://github.com/MindFlock/MindFlock)
#   MINDFLOCK_INSTALL_REF    branch / tag / commit to install (default: the
#                            newest release; `main` for unreleased code)
#   MINDFLOCK_INSTALL_LOCAL  path to a local checkout — install from disk
#                            instead of git (used by CI's cold-install job)
#   MINDFLOCK_UV_VERSION     uv version to install (default pinned below).
#                            Overriding skips the checksum (no known hash) —
#                            a warning is printed.
#   MINDFLOCK_PORT           port of a running server to restart afterwards
#                            (default 8765)
#   MINDFLOCK_INSTALL_NO_RESTART  set to 1 to leave a running server alone (the
#                            desktop app sets it: it restarts the server itself)
#   MINDFLOCK_NONINTERACTIVE set to 1 when nobody can answer a prompt. The
#                            desktop app sets this when it runs this script
#                            from its offline page: there is no terminal behind
#                            a GUI process, and the app's own Setup installs
#                            tmux and the agent next, so the script ends on one
#                            line saying so instead of the three steps.
set -eu

# The uv installer we run if uv is absent: version-pinned + checksum-verified.
# Bump both together (sha256 of https://astral.sh/uv/<version>/install.sh).
UV_PINNED_VERSION="0.13.1"
UV_INSTALLER_SHA256="7c482d409673a53f4d9bd34ae95c9166be3b8d89ca82f27dd7bca3cab457afff"  # pragma: allowlist secret
# The interpreter uv pins the tool venv to. Kept in step with INSTALL_PYTHON in
# backend/web/core/self_update.py (the in-app updater runs the same install).
INSTALL_PYTHON="3.12"

say()  { printf '\033[1;36m[mindflock]\033[0m %s\n' "$*"; }
fail() { printf '\033[1;31m[mindflock] ERROR:\033[0m %s\n' "$*" >&2; exit 1; }
# A value as the inside of a JSON string (backslashes and quotes escaped).
json_str() { printf %s "$1" | sed -e 's/\\/\\\\/g' -e 's/"/\\"/g'; }

JOIN=""
while [ $# -gt 0 ]; do
  case "$1" in
    --join)
      [ $# -ge 2 ] || fail "--join needs 'DEVICE CODE' (what Add a device shows)"
      JOIN="$2"; shift 2 ;;
    --join=*) JOIN="${1#--join=}"; shift ;;
    *) fail "unknown option: $1 (supported: --join 'DEVICE CODE')" ;;
  esac
done

REPO="${MINDFLOCK_INSTALL_REPO:-https://github.com/MindFlock/MindFlock}"
REF="${MINDFLOCK_INSTALL_REF:-}"

# --- 1. platform check ------------------------------------------------------
OS="$(uname -s 2>/dev/null || echo unknown)"
case "$OS" in
  Linux|Darwin) ;;
  MINGW*|MSYS*|CYGWIN*|Windows_NT)
    fail "native Windows is not a supported MindFlock host (the engine needs
tmux and Unix PTYs). Install WSL2 (https://learn.microsoft.com/windows/wsl/install)
and run this installer inside your WSL distro." ;;
  *) fail "unsupported platform: $OS (supported: Linux, macOS, Windows-via-WSL2)" ;;
esac
say "platform: $OS $(uname -m 2>/dev/null || true)"

# Is a real terminal reachable? Not `[ -r /dev/tty ]` — /dev/tty is mode
# crw-rw-rw- so that test passes even for a GUI-spawned process with no
# controlling terminal, and the redirect then fails at the point of use.
# Actually opening it is the only honest check.
has_tty() {
  [ "${MINDFLOCK_NONINTERACTIVE:-}" != "1" ] && (exec 3<>/dev/tty) 2>/dev/null
}

# macOS ships /usr/bin/git as a stub whose only job is to pop the Xcode Command
# Line Tools installer, so `command -v git` succeeds on a machine where git
# cannot actually clone anything — and uv would then fail deep in step 3 with a
# baffling error. Check for the real tools up front instead.
# With a terminal attached, open Apple's installer and WAIT for it (it takes
# 5-20 minutes) rather than failing and making the user start over.
if [ "$OS" = "Darwin" ] && ! xcode-select -p >/dev/null 2>&1; then
  if has_tty; then
    say "the Xcode Command Line Tools (which provide git) are missing — opening Apple's installer…"
    xcode-select --install >/dev/null 2>&1 || true
    say "finish Apple's installer; this script continues on its own when it's done (waiting up to 30 min)…"
    waited=0
    while ! xcode-select -p >/dev/null 2>&1 && [ "$waited" -lt 1800 ]; do
      sleep 10
      waited=$((waited + 10))
    done
  fi
  xcode-select -p >/dev/null 2>&1 || fail "the Xcode Command Line Tools are required (they provide git).
Run  xcode-select --install , finish Apple's installer, then re-run this script."
  say "Xcode Command Line Tools installed"
fi

command -v curl >/dev/null 2>&1 || fail "curl is required to install — install it with your package manager (e.g. apt/dnf/pacman/zypper) and re-run."

# --- 2. uv ------------------------------------------------------------------
# ~/.local/bin is where both uv and the mindflock entry point land. The PATH
# this shell came with is kept: the closing lines say whether a NEW terminal
# is needed before `mindflock` is found.
ORIG_PATH="$PATH"
PATH="$HOME/.local/bin:$PATH"

# sha256 of a file, portable across Linux (sha256sum) and macOS (shasum).
sha256_of() {
  if command -v sha256sum >/dev/null 2>&1; then sha256sum "$1" | awk '{print $1}'
  elif command -v shasum >/dev/null 2>&1; then shasum -a 256 "$1" | awk '{print $1}'
  else echo ""; fi
}

if command -v uv >/dev/null 2>&1; then
  say "uv already installed: $(uv --version)"
else
  UV_VERSION="${MINDFLOCK_UV_VERSION:-$UV_PINNED_VERSION}"
  say "installing uv $UV_VERSION (Astral's official installer, no root, into ~/.local/bin)…"
  TMP_INSTALLER="$(mktemp)"
  trap 'rm -f "$TMP_INSTALLER"' EXIT
  curl -LsSf -o "$TMP_INSTALLER" "https://astral.sh/uv/$UV_VERSION/install.sh"
  if [ "$UV_VERSION" = "$UV_PINNED_VERSION" ]; then
    GOT="$(sha256_of "$TMP_INSTALLER")"
    [ -n "$GOT" ] || fail "neither sha256sum nor shasum is available to verify the uv installer"
    [ "$GOT" = "$UV_INSTALLER_SHA256" ] || fail "uv installer checksum mismatch
  expected: $UV_INSTALLER_SHA256
  got:      $GOT
The download may be corrupted or tampered with — not running it. Re-try, or
inspect https://astral.sh/uv/$UV_VERSION/install.sh yourself."
    say "uv installer sha256 verified"
  else
    say "WARNING: MINDFLOCK_UV_VERSION=$UV_VERSION overrides the pinned version — no checksum on file, skipping verification"
  fi
  sh "$TMP_INSTALLER"
  rm -f "$TMP_INSTALLER"
  trap - EXIT
  PATH="$HOME/.local/bin:$PATH"
  command -v uv >/dev/null 2>&1 || fail "uv did not land on PATH — see https://docs.astral.sh/uv/getting-started/installation/"
  say "uv installed: $(uv --version)"
fi

# --- 3. mindflock ------------------------------------------------------------
# The newest published release tag ("" when none can be found): the GitHub API,
# then the releases/latest redirect, then the highest v* tag on the remote.
latest_release_tag() {
  slug=""
  case "$REPO" in
    https://github.com/*) slug="${REPO#https://github.com/}"; slug="${slug%/}"; slug="${slug%.git}" ;;
  esac
  tag=""
  if [ -n "$slug" ]; then
    tag="$(curl -fsSL -H 'Accept: application/vnd.github+json' \
      "https://api.github.com/repos/$slug/releases/latest" 2>/dev/null \
      | sed -n 's/.*"tag_name"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' | head -n1)"
    if [ -z "$tag" ]; then
      url="$(curl -fsSLI -o /dev/null -w '%{url_effective}' \
        "https://github.com/$slug/releases/latest" 2>/dev/null || true)"
      case "$url" in */releases/tag/*) tag="${url##*/releases/tag/}" ;; esac
    fi
  fi
  if [ -z "$tag" ]; then
    # Release tags only (vX.Y.Z): `v*` also matches a pre-release or any
    # other tag someone pushed, and the highest of those is no release.
    tag="$(git ls-remote --tags --refs --sort=-v:refname "$REPO" 'v*' 2>/dev/null \
      | sed 's|.*refs/tags/||' | grep -E '^v[0-9]+\.[0-9]+\.[0-9]+$' | head -n1)"
  fi
  case "$tag" in v[0-9]*) printf %s "$tag" ;; esac
}

# Shaped like a release tag (v0.7.5) — the same rule as is_release_tag() in
# backend/web/core/self_update.py.
is_release_tag() {
  printf %s "$1" | grep -Eq '^v?[0-9]+(\.[0-9]+){1,3}$'
}

PINNED=""
if [ -n "${MINDFLOCK_INSTALL_LOCAL:-}" ]; then
  SPEC="mindflock[web] @ file://$MINDFLOCK_INSTALL_LOCAL"
  # The record below names the checkout's commit when it is a git checkout
  # (CI's cold-install job asserts it is the one under test).
  REF="local"
  PINNED="$(git -C "$MINDFLOCK_INSTALL_LOCAL" rev-parse HEAD 2>/dev/null || true)"
  SOURCE_FIELD="\"path\":\"$(json_str "$MINDFLOCK_INSTALL_LOCAL")\""
  say "installing MindFlock from local checkout $MINDFLOCK_INSTALL_LOCAL…"
else
  # Resolve the branch/tag to a full commit SHA and install THAT, so the ref
  # can't move between "you read the code" and "it runs on your machine" —
  # and the printed SHA is an audit trail. A 40-hex REF is already a SHA.
  command -v git >/dev/null 2>&1 || fail "git is required to install from $REPO — install it with your package manager (e.g. apt/dnf/pacman/zypper) and re-run."
  if [ -z "$REF" ]; then
    REF="$(latest_release_tag)"
    if [ -n "$REF" ]; then
      say "newest release: $REF (set MINDFLOCK_INSTALL_REF=main for unreleased code)"
    else
      REF="main"
      say "WARNING: couldn't find the newest release — installing main instead:"
      say "         an unreleased build; the desktop app pins releases"
    fi
  elif ! is_release_tag "$REF"; then
    # A branch or a bare commit was asked for: say so, because the desktop
    # app installs the release matching its own version and an engine AHEAD
    # of it is what its downgrade path trips over.
    say "WARNING: MINDFLOCK_INSTALL_REF=$REF is not a release tag — installing an unreleased build; the desktop app pins releases"
  fi
  SOURCE_FIELD="\"repo\":\"$(json_str "$REPO")\""
  if [ "${#REF}" -eq 40 ] && [ -z "$(printf %s "$REF" | tr -d '0-9a-f')" ]; then
    PINNED="$REF"
  fi
  if [ -z "$PINNED" ]; then
    # Peeled tags first: an ANNOTATED tag's own ref points at the tag object,
    # not the commit, and installing that SHA is a coin flip on whether the
    # server will serve it. `refs/tags/X^{}` is the commit it wraps; it simply
    # doesn't exist for branches or lightweight tags, hence the fallback.
    PINNED="$(git ls-remote "$REPO" "refs/tags/$REF^{}" 2>/dev/null | head -n1 | cut -f1)"
    if [ -z "$PINNED" ]; then
      PINNED="$(git ls-remote "$REPO" "refs/heads/$REF" "refs/tags/$REF" 2>/dev/null | head -n1 | cut -f1)"
    fi
    [ -n "$PINNED" ] || fail "could not resolve '$REF' on $REPO (branch or tag?) — check MINDFLOCK_INSTALL_REF"
  fi
  SPEC="mindflock[web] @ git+$REPO@$PINNED"
  say "installing MindFlock from $REPO@$REF (commit $PINNED)…"
fi
# --force makes re-runs an in-place upgrade instead of an error (idempotent).
# An engine first installed with pipx stays a pipx install: `mindflock update`
# and the desktop updater both come through here and must not turn it into a
# second, uv-tool copy under the same `mindflock` shim.
INSTALL_METHOD="uv-tool"
if [ -r "$HOME/.mindflock/install.json" ] && grep -q '"method": *"pipx"' "$HOME/.mindflock/install.json" 2>/dev/null && command -v pipx >/dev/null 2>&1; then
  say "this engine was installed with pipx — keeping it that way"
  pipx install --force "$SPEC"
  INSTALL_METHOD="pipx"
else
  uv tool install --force --python "$INSTALL_PYTHON" "$SPEC"
fi

command -v mindflock >/dev/null 2>&1 || fail "install finished but \`mindflock\` is not on PATH.
Add ~/.local/bin to PATH (uv prints the exact line), then run: mindflock init"
MF="$(command -v mindflock)"
say "installed: $MF ($("$MF" --version 2>/dev/null || echo mindflock))"

# Record what was installed, next to the engine's own state (state.json,
# update.json live in ~/.mindflock — the engine's config dir honours $HOME and
# nothing else, backend/config/config.py GetConfigDir). `mindflock update`
# reads it to follow the same path — method, ref, commit, Python — and detects
# the install when the file is missing, so failing to write it is a warning,
# not a failure.
STATE_DIR="$HOME/.mindflock"
INSTALL_RECORD="$STATE_DIR/install.json"
if mkdir -p "$STATE_DIR" 2>/dev/null && printf '{"method":"'"$INSTALL_METHOD"'","ref":"%s","sha":"%s","python":"%s","at":"%s",%s}\n' \
    "$(json_str "$REF")" "$(json_str "$PINNED")" "$INSTALL_PYTHON" \
    "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$SOURCE_FIELD" > "$INSTALL_RECORD" 2>/dev/null; then
  say "recorded the install in $INSTALL_RECORD"
else
  say "WARNING: couldn't write $INSTALL_RECORD — mindflock update will detect this install instead"
fi

# Make sure FUTURE shells find it too: `uv tool update-shell` appends the
# ~/.local/bin PATH line to the shell rc if (and only if) it's missing.
# Best-effort — a failure here never fails the install.
uv tool update-shell >/dev/null 2>&1 || true

# --- 5. a server already running here -----------------------------------------
# Re-running this script is how people update, and `uv tool install --force`
# just replaced the venv under any server that is running: restart it so it
# serves the version that was installed instead of the one it booted with.
PORT_="${MINDFLOCK_PORT:-8765}"
if [ "${MINDFLOCK_INSTALL_NO_RESTART:-}" != "1" ] \
  && curl -fsS --max-time 2 "http://127.0.0.1:$PORT_/api/remote/hello" >/dev/null 2>&1; then
  say "restarting the MindFlock server on port $PORT_ onto the new version…"
  if MINDFLOCK_PORT="$PORT_" "$MF" restart; then
    if [ -z "$JOIN" ]; then
      say ""
      say "Done — the server on http://127.0.0.1:$PORT_ runs the new version."
      exit 0
    fi
  else
    say "couldn't restart it — it runs the previous version until you run: $MF restart"
    if [ -z "$JOIN" ]; then
      say ""
      say "Installed. The server on http://127.0.0.1:$PORT_ still runs the previous version."
      exit 1
    fi
  fi
fi

# --- 6. join your other computer (--join) ------------------------------------
# A new computer added from another one's "Add a device" line. Not fatal:
# MindFlock is installed either way, and the line to finish is printed.
if [ -n "$JOIN" ]; then
  say "joining your devices ($JOIN)…"
  if has_tty; then
    "$MF" devices bootstrap --join "$JOIN" </dev/tty && JOINED=1 || JOINED=0
  else
    "$MF" devices bootstrap --join "$JOIN" && JOINED=1 || JOINED=0
  fi
  if [ "$JOINED" = 1 ]; then
    say "joined — this computer is one of your devices now"
  else
    say "joining didn't finish — once the above is fixed, run:"
    say "  $MF devices bootstrap --join '$JOIN'"
  fi
fi

# --- 7. the three steps --------------------------------------------------------
# The desktop app (no terminal) goes on to its own Setup, which installs tmux
# and the agent with one button. Everyone else gets the same three steps
# `mindflock init` and `mindflock serve` print — read-only, the tools are not
# probed for install a second time here — ending on exactly one next action.
say ""
if [ "${MINDFLOCK_NONINTERACTIVE:-}" = "1" ]; then
  say "Done. tmux and your agent come next, in the app."
else
  if ! PATH="$ORIG_PATH" command -v mindflock >/dev/null 2>&1; then
    say "Open a new terminal first — this one doesn't find mindflock yet."
  fi
  "$MF" init --steps || say "Next: run mindflock init"
fi
