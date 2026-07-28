#!/usr/bin/env bash
cd /WORKDIR
exec bash -ilc 'export TESTMON_ENV=shared
if [ -f .mindflock_started ]; then
  aider --foo --dangerously-skip-permissions --continue || { sleep 3; aider --foo --dangerously-skip-permissions --continue; } || { echo "[mindflock] resume failed twice; starting a fresh session WITHOUT re-sending the ticket prompt (kept in .mindflock_prompt.md)"; aider --foo --dangerously-skip-permissions; }
else
  : > .mindflock_started
  aider --foo --dangerously-skip-permissions "$(cat .mindflock_prompt.md)"
fi
while true; do
  cs_code=$?
  case "$cs_code" in 0|130) break;; esac
  echo "[agent died (code $cs_code) — resuming in 3s; press Ctrl-C for a shell]"
  sleep 3 || break
  aider --foo --dangerously-skip-permissions --continue || { sleep 3; aider --foo --dangerously-skip-permissions --continue; } || { echo "[mindflock] resume failed twice; starting a fresh session WITHOUT re-sending the ticket prompt (kept in .mindflock_prompt.md)"; aider --foo --dangerously-skip-permissions; }
done
exec bash -i
'
