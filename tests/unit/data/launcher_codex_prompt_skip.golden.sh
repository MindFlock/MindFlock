#!/usr/bin/env bash
cd /WORKDIR
exec bash -ilc 'export TESTMON_ENV=shared
if [ -f .mindflock_started ]; then
  codex --dangerously-bypass-approvals-and-sandbox resume --last || { sleep 3; codex --dangerously-bypass-approvals-and-sandbox resume --last; } || { echo "[mindflock] resume failed twice; starting a fresh session WITHOUT re-sending the ticket prompt (kept in .mindflock_prompt.md)"; codex --dangerously-bypass-approvals-and-sandbox; }
else
  : > .mindflock_started
  codex --dangerously-bypass-approvals-and-sandbox "$(cat .mindflock_prompt.md)"
fi
while true; do
  cs_code=$?
  case "$cs_code" in 0|130) break;; esac
  echo "[agent died (code $cs_code) — resuming in 3s; press Ctrl-C for a shell]"
  sleep 3 || break
  codex --dangerously-bypass-approvals-and-sandbox resume --last || { sleep 3; codex --dangerously-bypass-approvals-and-sandbox resume --last; } || { echo "[mindflock] resume failed twice; starting a fresh session WITHOUT re-sending the ticket prompt (kept in .mindflock_prompt.md)"; codex --dangerously-bypass-approvals-and-sandbox; }
done
exec bash -i
'
