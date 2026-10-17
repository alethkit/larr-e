#!/usr/bin/env bash
# Start (or restart) LARR-E in one tmux window with four panes:
#   listener | API | 3DS bridge | Slack bot
#
#   ./up.sh               real Pioreactor (real.env)
#   ./up.sh rehearsal     local broker + demo data (rehearsal.env)
#   ./up.sh path/to.env   any settings file
#   ./up.sh real --no-attach   start in the background
#
# Detach with Ctrl-b d (everything keeps running); come back with `tmux attach -t larre`.
# Stop everything with ./down.sh.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SESSION="${LARRE_TMUX_SESSION:-larre}"
SECRETS="${LARRE_SECRETS:-$HOME/.config/larr-e/secrets.env}"

case "${1:-real}" in
  real) ENV_FILE="$ROOT/real.env" ;;
  rehearsal) ENV_FILE="$ROOT/rehearsal.env" ;;
  *) ENV_FILE="$(realpath "$1")" ;;
esac
ATTACH=1
[[ "${2:-}" == "--no-attach" || "${1:-}" == "--no-attach" ]] && ATTACH=0
[[ -f "$ENV_FILE" ]] || { echo "Settings file not found: $ENV_FILE" >&2; exit 1; }
command -v tmux >/dev/null || { echo "tmux is not installed (sudo dnf install tmux)" >&2; exit 1; }

# Restart cleanly if it's already running.
if tmux has-session -t "$SESSION" 2>/dev/null; then
  echo "Restarting LARR-E (session '$SESSION')..."
  "$ROOT/down.sh" >/dev/null
fi

# Every pane loads the same settings; secrets are optional (Slack and the LLM need them).
LOAD="source '$ENV_FILE' && { [ -f '$SECRETS' ] && source '$SECRETS' || true; }"
WAIT_API='echo "waiting for the API..."; until curl -s -o /dev/null "http://127.0.0.1:${LARRE_API_PORT:-8787}/"; do sleep 1; done'

LISTENER="larr-e listen"
API="larr-e api"
BRIDGE="$WAIT_API; cd '$ROOT/pocket3ds' && bun run bridge"
SLACK="if [ -n \"\${SLACK_BOT_TOKEN:-}\" ] && [ -n \"\${SLACK_APP_TOKEN:-}\" ]; then $WAIT_API; larr-e slack; else echo 'Slack tokens not set: Slack bot skipped.'; fi"

tmux new-session -d -s "$SESSION" -n larr-e -x 220 -y 50
tmux split-window -h -t "$SESSION:larr-e"
tmux split-window -v -t "$SESSION:larr-e.0"
tmux split-window -v -t "$SESSION:larr-e.2"
tmux select-layout -t "$SESSION:larr-e" tiled >/dev/null
tmux set-option -t "$SESSION" pane-border-status top >/dev/null
tmux set-option -t "$SESSION" mouse on >/dev/null   # click a pane to focus it, scroll with the wheel

start() {  # pane title command
  tmux select-pane -t "$SESSION:larr-e.$1" -T "$2"
  # Typed into an interactive shell, so the pane survives a crash and ↑ + Enter reruns the command.
  tmux send-keys -t "$SESSION:larr-e.$1" "clear; $LOAD && $3" Enter
}
start 0 "1 listener" "$LISTENER"
start 1 "2 API" "$API"
start 2 "3 3DS bridge" "$BRIDGE"
start 3 "4 Slack" "$SLACK"

echo "LARR-E started with $(basename "$ENV_FILE") in tmux session '$SESSION'."
echo "Detach: Ctrl-b d   |   Reattach: tmux attach -t $SESSION   |   Stop: $ROOT/down.sh"
if [[ $ATTACH -eq 1 ]]; then
  if [[ -n "${TMUX:-}" ]]; then tmux switch-client -t "$SESSION"; else tmux attach -t "$SESSION"; fi
fi
