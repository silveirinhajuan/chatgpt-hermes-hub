#!/usr/bin/env bash
set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$DIR"

export HERMES_HUB_DB="${HERMES_HUB_DB:-$DIR/data/hub.db}"
exec "$DIR/.venv/bin/python" -m hermes_hub serve
