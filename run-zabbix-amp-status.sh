#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")"
exec ./.venv/bin/python zabbix_amp_status.py "$@"
