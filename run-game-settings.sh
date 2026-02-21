#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")"
exec ./.venv/bin/python sync_game_settings.py "$@"
