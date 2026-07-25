#!/usr/bin/env bash
cd /WORKDIR
exec bash -ilc 'export TESTMON_ENV=shared
if [ -f .mindflock_started ]; then
  claude --dangerously-skip-permissions --continue || { sleep 3; claude --dangerously-skip-permissions --continue; } || { echo "[mindflock] resume failed twice; starting a fresh session WITHOUT re-sending the ticket prompt (kept in .mindflock_prompt.md)"; claude --dangerously-skip-permissions; }
else
  : > .mindflock_started
  claude --dangerously-skip-permissions
fi
while true; do
  cs_code=$?
  case "$cs_code" in 0|130) break;; esac
  echo "[agent died (code $cs_code) — resuming in 3s; press Ctrl-C for a shell]"
  sleep 3 || break
  claude --dangerously-skip-permissions --continue || { sleep 3; claude --dangerously-skip-permissions --continue; } || { echo "[mindflock] resume failed twice; starting a fresh session WITHOUT re-sending the ticket prompt (kept in .mindflock_prompt.md)"; claude --dangerously-skip-permissions; }
done
exec bash -i
'
