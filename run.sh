#!/usr/bin/env bash
# Lance le WalkingPad GNOME Indicator avec le venv dédié.
# Usage:
#   ./run.sh          # mode normal
#   ./run.sh --debug  # logs détaillés dans le terminal

VENV="$HOME/.local/share/walkingpad-venv"
SCRIPT="$(dirname "$(realpath "$0")")/walkingpad_indicator.py"

exec "$VENV/bin/python" "$SCRIPT" "$@"
