#!/usr/bin/env bash
# Stop LARR-E: Ctrl-C every pane (so the listener saves its state), then close the tmux session.
set -euo pipefail
SESSION="${LARRE_TMUX_SESSION:-larre}"

if ! tmux has-session -t "$SESSION" 2>/dev/null; then
  echo "LARR-E is not running (no tmux session '$SESSION')."
  exit 0
fi
for pane in $(tmux list-panes -t "$SESSION" -F '#{pane_id}'); do
  tmux send-keys -t "$pane" C-c
done
sleep 2
tmux kill-session -t "$SESSION"
echo "LARR-E stopped."
