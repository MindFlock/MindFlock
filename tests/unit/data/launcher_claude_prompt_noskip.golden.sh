#!/usr/bin/env bash
cd /WORKDIR
{ echo 500 >/proc/self/oom_score_adj; } 2>/dev/null || :
exec bash -ilc 'export TESTMON_ENV=shared
if [ -f .mindflock_started ]; then
  claude --continue || if [ $? -eq 137 ]; then (exit 137); else sleep 3; claude --continue; fi || if [ $? -eq 137 ]; then (exit 137); else echo "[mindflock] resume failed twice; starting a fresh session WITHOUT re-sending the ticket prompt (kept in .mindflock_prompt.md)"; claude; fi
else
  : > .mindflock_started
  claude "$(cat .mindflock_prompt.md)"
fi
if [ $? -eq 137 ]; then exit 137; fi
while true; do
  cs_code=$?
  case "$cs_code" in 0|130) break;; esac
  echo "[agent died (code $cs_code) — resuming in 3s; press Ctrl-C for a shell]"
  sleep 3 || break
  claude --continue || if [ $? -eq 137 ]; then (exit 137); else sleep 3; claude --continue; fi || if [ $? -eq 137 ]; then (exit 137); else echo "[mindflock] resume failed twice; starting a fresh session WITHOUT re-sending the ticket prompt (kept in .mindflock_prompt.md)"; claude; fi
done
exec bash -i
'
