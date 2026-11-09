#!/usr/bin/env bash
set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$DIR"

if [[ $# -lt 1 ]]; then
  echo "uso: $0 <export.zip|conversations.json|diretório> [opções]" >&2
  exit 2
fi

export HERMES_HUB_DB="${HERMES_HUB_DB:-$DIR/data/hub.db}"
exec "$DIR/.venv/bin/python" -m hermes_hub index-chatgpt "$@"
