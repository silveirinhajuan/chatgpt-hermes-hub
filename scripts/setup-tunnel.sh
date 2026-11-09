#!/usr/bin/env bash
set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

if [[ $# -lt 1 ]]; then
  echo "uso: CONTROL_PLANE_API_KEY=sk-... $0 tunnel_XXXXXXXX" >&2
  exit 2
fi

if [[ -z "${CONTROL_PLANE_API_KEY:-}" ]]; then
  echo "defina CONTROL_PLANE_API_KEY (chave com Tunnels Read + Use)" >&2
  exit 2
fi

if ! command -v tunnel-client >/dev/null 2>&1; then
  echo "tunnel-client não encontrado. Baixe em https://github.com/openai/tunnel-client/releases" >&2
  exit 1
fi

TUNNEL_ID="$1"

tunnel-client init \
  --profile hermes-hub \
  --tunnel-id "$TUNNEL_ID" \
  --mcp-command "$DIR/scripts/run-server.sh" \
  --force

tunnel-client doctor --profile hermes-hub --explain

cat <<EOF

Perfil criado. Para subir o túnel:

  tunnel-client run --profile hermes-hub

Depois, no ChatGPT:
  Settings → Security and login → Developer mode
  Plugins → Add → Create MCP App
  Connection: Tunnel → hermes-hub
  Authentication: None
EOF
