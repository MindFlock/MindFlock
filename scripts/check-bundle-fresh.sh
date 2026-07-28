#!/bin/sh
# Assert that backend/web/static/ is the build of frontend/src.
#
# This matters more than it looks: `uv build` copies backend/web/static/app.js
# into the wheel verbatim and electron-builder packages the same tree — neither
# ever runs vite. So the COMMITTED bundle is what ships, and a frontend/src edit
# without `npm run build` releases a UI that differs from its own source.
#
# One implementation, three callers (CI's "frontend bundle is current" job, the
# shared pre-push hook in .pre-commit-config.yaml, and the personal pre-push
# gate) so they cannot drift apart.
#
# Rebuilds in place: on failure the corrected bundle is left in the working
# tree, ready to commit. Exits 0 when the tree is already correct.
set -eu

ROOT="$(git rev-parse --show-toplevel)"
cd "$ROOT"

if ! command -v npm > /dev/null 2>&1; then
    echo "error: npm is required to verify the shipped bundle" >&2
    exit 1
fi

cd frontend
# `npm ci` (not install) so a lockfile that cannot resolve — e.g. a plugin major
# that outgrew the pinned vite — fails here rather than at release time.
[ -d node_modules ] || npm ci --no-audit --no-fund > /dev/null
# Runs tsc --noEmit first, so a type error fails this check too.
npm run build > /dev/null
cd "$ROOT"

if git diff --quiet -- backend/web/static/; then
    echo "bundle is current."
    exit 0
fi

echo "error: backend/web/static/ is stale — it has been rebuilt for you; commit the result." >&2
git diff --stat -- backend/web/static/ >&2
exit 1
