#!/usr/bin/env bash
# Envía una tarea a la AI Factory (webhook de n8n).
#   ./scripts/submit.sh "Título" "Descripción" [source] [source_ref]
#   ./scripts/submit.sh "Fix NPE en login" "Sentry: TypeError en /login ..." sentry SIGA-123
set -euo pipefail
N8N_URL="${N8N_URL:-http://localhost:5678}"
TITLE="${1:?uso: submit.sh 'título' 'descripción' [source] [source_ref]}"
DESC="${2:-}"
SOURCE="${3:-manual}"
REF="${4:-}"
payload=$(python3 -c 'import json,sys; print(json.dumps({"title":sys.argv[1],"description":sys.argv[2],"source":sys.argv[3],"source_ref":sys.argv[4] or None}))' \
  "$TITLE" "$DESC" "$SOURCE" "$REF")
curl -fsS -X POST "$N8N_URL/webhook/ai-factory" -H 'Content-Type: application/json' -d "$payload"
echo
echo "Seguimiento: curl -s -H \"X-Factory-Token: \$FACTORY_TOKEN\" http://localhost:8000/runs | jq"
