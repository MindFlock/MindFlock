#!/usr/bin/env bash
cd /WORKDIR
exec bash -ilc 'export TESTMON_ENV=shared
mf_seed_prompt() {
  [ -n "$TMUX_PANE" ] || return 0
  local prev= cur= i=0
  while [ $i -lt 60 ]; do
    sleep 1
    cur=$(tmux capture-pane -p -t "$TMUX_PANE" 2>/dev/null | tr -dc "[:graph:]")
    [ -n "$cur" ] && [ "$cur" = "$prev" ] && break
    prev=$cur
    i=$((i+1))
  done
  tmux load-buffer -b mf_prompt .mindflock_prompt.md 2>/dev/null || return 0
  tmux paste-buffer -b mf_prompt -p -d -t "$TMUX_PANE" 2>/dev/null || return 0
  sleep 1
  tmux send-keys -t "$TMUX_PANE" Enter 2>/dev/null || true
}
if [ -f .mindflock_started ]; then
  goose session -r || { sleep 3; goose session -r; } || { echo "[mindflock] resume failed twice; starting a fresh session WITHOUT re-sending the ticket prompt (kept in .mindflock_prompt.md)"; goose session; }
else
  : > .mindflock_started
  mf_seed_prompt & goose session
fi
while true; do
  cs_code=$?
  case "$cs_code" in 0|130) break;; esac
  echo "[agent died (code $cs_code) — resuming in 3s; press Ctrl-C for a shell]"
  sleep 3 || break
  goose session -r || { sleep 3; goose session -r; } || { echo "[mindflock] resume failed twice; starting a fresh session WITHOUT re-sending the ticket prompt (kept in .mindflock_prompt.md)"; goose session; }
done
exec bash -i
'
