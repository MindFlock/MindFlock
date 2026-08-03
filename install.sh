#!/bin/sh
# MindFlock installer — safe to re-run, no root, no repo clone, no Python setup
# needed beforehand.
#
#   curl -LsSf https://raw.githubusercontent.com/MindFlock/MindFlock/main/install.sh | sh
#
# What it does (and prints as it goes):
#   1. Checks the platform (Linux, macOS, or WSL; native Windows is refused
#      with a pointer to WSL2).
#   2. Installs `uv` (Astral's Python manager) via its official installer if
#      it isn't already present — into ~/.local/bin, no root. The installer is
#      version-pinned and sha256-verified before it runs (no blind curl|sh).
#   3. `uv tool install "mindflock[web]"` straight from GitHub — the requested
#      branch/tag is first resolved to a full commit SHA (printed, and pinned
#      for the install so what you audited is what you get). uv fetches a
#      suitable Python by itself, builds in an isolated venv, and links the
#      `mindflock` command into ~/.local/bin. Re-running upgrades in place.
#   4. Runs `mindflock doctor` so you immediately see which runtime
#      dependencies (git, tmux, claude — plus the optional gh) still need
#      installing — with the exact install command for your platform. gh is
#      listed as optional on purpose: pushing is plain `git push` over the
#      remote you already have (SSH or HTTPS), and only PR create/merge prefer
#      gh. When a terminal is attached
#      it runs `doctor --fix`, which offers to run each of those commands
#      for you (y/n per item) so a fresh machine ends the install ready to go.
#
# Overrides (env vars):
#   MINDFLOCK_INSTALL_REPO   git URL to install from
#                            (default https://github.com/MindFlock/MindFlock)
#   MINDFLOCK_INSTALL_REF    branch / tag / commit to install (default main)
#   MINDFLOCK_INSTALL_LOCAL  path to a local checkout — install from disk
#                            instead of git (used by CI's cold-install job)
#   MINDFLOCK_UV_VERSION     uv version to install (default pinned below).
#                            Overriding skips the checksum (no known hash) —
#                            a warning is printed.
#   MINDFLOCK_NONINTERACTIVE set to 1 to force the read-only `mindflock doctor`
#                            report instead of the guided `--fix` prompts. The
#                            desktop app sets this when it runs this script
#                            from its offline page: there is no terminal behind
#                            a GUI process, so --fix would block on y/n forever.
set -eu

# The uv installer we run if uv is absent: version-pinned + checksum-verified.
# Bump both together (sha256 of https://astral.sh/uv/<version>/install.sh).
UV_PINNED_VERSION="0.12.1"
UV_INSTALLER_SHA256="d3f5412d38c99f9d024901843bf98206f0d2c6dbe64df40d0b740e2751ca62c1"  # pragma: allowlist secret

say()  { printf '\033[1;36m[mindflock]\033[0m %s\n' "$*"; }
fail() { printf '\033[1;31m[mindflock] ERROR:\033[0m %s\n' "$*" >&2; exit 1; }

REPO="${MINDFLOCK_INSTALL_REPO:-https://github.com/MindFlock/MindFlock}"
REF="${MINDFLOCK_INSTALL_REF:-main}"

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
if [ "$OS" = "Darwin" ] && ! xcode-select -p >/dev/null 2>&1; then
  if has_tty; then
    say "the Xcode Command Line Tools (which provide git) are missing — opening Apple's installer…"
    xcode-select --install >/dev/null 2>&1 || true
  fi
  fail "the Xcode Command Line Tools are required (they provide git).
Run  xcode-select --install , finish Apple's installer, then re-run this script."
fi

command -v curl >/dev/null 2>&1 || fail "curl is required to install — install it with your package manager (e.g. apt/dnf/pacman/zypper) and re-run."

# --- 2. uv ------------------------------------------------------------------
# ~/.local/bin is where both uv and the mindflock entry point land.
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
if [ -n "${MINDFLOCK_INSTALL_LOCAL:-}" ]; then
  SPEC="mindflock[web] @ file://$MINDFLOCK_INSTALL_LOCAL"
  say "installing MindFlock from local checkout $MINDFLOCK_INSTALL_LOCAL…"
else
  # Resolve the branch/tag to a full commit SHA and install THAT, so the ref
  # can't move between "you read the code" and "it runs on your machine" —
  # and the printed SHA is an audit trail. A 40-hex REF is already a SHA.
  command -v git >/dev/null 2>&1 || fail "git is required to install from $REPO — install it with your package manager (e.g. apt/dnf/pacman/zypper) and re-run."
  PINNED=""
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
uv tool install --force --python 3.12 "$SPEC"

command -v mindflock >/dev/null 2>&1 || fail "install finished but \`mindflock\` is not on PATH.
Add ~/.local/bin to PATH (uv prints the exact line), then re-run: mindflock doctor"
say "installed: $(command -v mindflock)"

# Make sure FUTURE shells find it too: `uv tool update-shell` appends the
# ~/.local/bin PATH line to the shell rc if (and only if) it's missing.
# Best-effort — a failure here never fails the install.
uv tool update-shell >/dev/null 2>&1 || true

# --- 4. doctor ---------------------------------------------------------------
say "checking runtime dependencies (git, tmux, agent CLI; gh optional)…"
# Doctor exits 1 when something required is missing — that's information for
# the user, not an installer failure, so don't abort on it. Under `curl | sh`
# stdin is the script pipe, so the guided --fix mode reads its y/n prompts
# from the controlling terminal; with no terminal (CI, or the desktop app's
# in-window install) fall back to the plain read-only report.
if has_tty; then
  mindflock doctor --fix </dev/tty || true
else
  mindflock doctor || true
fi

say ""
say "Done. Next steps:"
say "  1. fix anything ✗ above (each line shows the exact command, or re-run: mindflock doctor --fix)"
say "  2. cd into a git repo you want to work on"
say "  3. run: mindflock serve   →  open http://127.0.0.1:8765"
