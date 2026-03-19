#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TEMPLATE_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
TEST_ROOT="${1:-/root/openclaw-two-agent-smoke}"
AGENT1_ROOT="${TEST_ROOT}/agent-a"
AGENT2_ROOT="${TEST_ROOT}/agent-b"
WORKSPACE1="${AGENT1_ROOT}/workspace"
WORKSPACE2="${AGENT2_ROOT}/workspace"
BOOTSTRAP1="${WORKSPACE1}/.openclaw/community-agent.bootstrap.json"
BOOTSTRAP2="${WORKSPACE2}/.openclaw/community-agent.bootstrap.json"
INGRESS_SERVICE_NAME="openclaw-community-ingress.service"

rm -rf "${TEST_ROOT}"
mkdir -p "${AGENT1_ROOT}" "${AGENT2_ROOT}"

bash "${TEMPLATE_ROOT}/scripts/bootstrap-community-agent-template.sh" "${WORKSPACE1}"
bash "${WORKSPACE1}/scripts/install-community-webhook-service.sh"

bash "${TEMPLATE_ROOT}/scripts/bootstrap-community-agent-template.sh" "${WORKSPACE2}"
bash "${WORKSPACE2}/scripts/install-community-webhook-service.sh"

json_get() {
  local file="${1}"
  local key="${2}"
  if command -v jq >/dev/null 2>&1; then
    jq -r --arg key "${key}" '.[$key] // empty' "${file}"
    return
  fi
  python3 - "${file}" "${key}" <<'PY'
import json
import sys
path = sys.argv[1]
key = sys.argv[2]
with open(path, "r", encoding="utf-8") as fh:
    data = json.load(fh)
print(data.get(key, "") or "")
PY
}

SERVICE1="$(json_get "${BOOTSTRAP1}" service_name)"
SERVICE2="$(json_get "${BOOTSTRAP2}" service_name)"
WEBHOOK1="$(json_get "${BOOTSTRAP1}" webhook_path)"
WEBHOOK2="$(json_get "${BOOTSTRAP2}" webhook_path)"
SEND1="$(json_get "${BOOTSTRAP1}" send_path)"
SEND2="$(json_get "${BOOTSTRAP2}" send_path)"
SOCKET1="$(json_get "${BOOTSTRAP1}" socket_path)"
SOCKET2="$(json_get "${BOOTSTRAP2}" socket_path)"

for _ in $(seq 1 50); do
  if [[ -S "${SOCKET1}" && -S "${SOCKET2}" ]]; then
    break
  fi
  sleep 0.2
done

for _ in $(seq 1 50); do
  if curl -sS http://127.0.0.1:8848/healthz >/dev/null 2>&1; then
    break
  fi
  sleep 0.2
done

systemctl status "${INGRESS_SERVICE_NAME}" --no-pager
systemctl status "${SERVICE1}" --no-pager
systemctl status "${SERVICE2}" --no-pager

curl -sS -o /tmp/agent1.send.out -w "%{http_code}" \
  -H 'Content-Type: application/json' \
  -X POST "http://127.0.0.1:8848${SEND1}" \
  -d '{"group_id":"54b12c32-dbd3-46d8-97ee-22bf8a499709","content":{"text":"agent-a routed send smoke"}}' \
  >/tmp/agent1.send.code

curl -sS -o /tmp/agent2.send.out -w "%{http_code}" \
  -H 'Content-Type: application/json' \
  -X POST "http://127.0.0.1:8848${SEND2}" \
  -d '{"group_id":"54b12c32-dbd3-46d8-97ee-22bf8a499709","content":{"text":"agent-b routed send smoke"}}' \
  >/tmp/agent2.send.code

curl -sS -o /tmp/agent1.webhook.out -w "%{http_code}" \
  -H 'Content-Type: application/json' \
  -H 'x-community-webhook-signature: invalid' \
  -X POST "http://127.0.0.1:8848${WEBHOOK1}" \
  -d '{}' >/tmp/agent1.webhook.code

curl -sS -o /tmp/agent2.webhook.out -w "%{http_code}" \
  -H 'Content-Type: application/json' \
  -H 'x-community-webhook-signature: invalid' \
  -X POST "http://127.0.0.1:8848${WEBHOOK2}" \
  -d '{}' >/tmp/agent2.webhook.code

echo "agent1 send HTTP $(cat /tmp/agent1.send.code)"
echo "agent2 send HTTP $(cat /tmp/agent2.send.code)"
echo "agent1 webhook HTTP $(cat /tmp/agent1.webhook.code)"
echo "agent2 webhook HTTP $(cat /tmp/agent2.webhook.code)"
