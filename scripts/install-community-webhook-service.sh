#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEFAULT_WORKSPACE_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
WORKSPACE_ROOT="${WORKSPACE_ROOT:-${DEFAULT_WORKSPACE_ROOT}}"
ENV_FILE="${WORKSPACE_ROOT}/.openclaw/community-agent.env"
BOOTSTRAP_METADATA="${WORKSPACE_ROOT}/.openclaw/community-agent.bootstrap.json"
NODE_BIN="$(command -v node)"

if [[ -z "${NODE_BIN}" ]]; then
  echo "node not found in PATH" >&2
  exit 1
fi

if [[ ! -f "${ENV_FILE}" ]]; then
  echo "missing env file: ${ENV_FILE}" >&2
  exit 1
fi

if [[ ! -f "${BOOTSTRAP_METADATA}" ]]; then
  echo "missing bootstrap metadata: ${BOOTSTRAP_METADATA}" >&2
  exit 1
fi

json_get() {
  local key="${1}"
  if command -v jq >/dev/null 2>&1; then
    jq -r --arg key "${key}" '.[$key] // empty' "${BOOTSTRAP_METADATA}"
    return
  fi
  python3 - "${BOOTSTRAP_METADATA}" "${key}" <<'PY'
import json
import sys

path = sys.argv[1]
key = sys.argv[2]
with open(path, "r", encoding="utf-8") as fh:
    data = json.load(fh)
value = data.get(key, "")
if value is None:
    value = ""
print(value)
PY
}

SERVICE_NAME="${SERVICE_NAME:-$(json_get service_name)}"
AGENT_SLUG="$(json_get agent_slug)"
WEBHOOK_PATH="$(json_get webhook_path)"
SEND_PATH="$(json_get send_path)"
SOCKET_PATH="$(json_get socket_path)"
INGRESS_HOME="$(json_get ingress_home)"
if [[ -z "${SERVICE_NAME}" ]]; then
  SERVICE_NAME="openclaw-community-webhook.service"
fi
if [[ -z "${AGENT_SLUG}" ]]; then
  echo "missing agent_slug in ${BOOTSTRAP_METADATA}" >&2
  exit 1
fi
if [[ -z "${SOCKET_PATH}" ]]; then
  echo "missing socket_path in ${BOOTSTRAP_METADATA}" >&2
  exit 1
fi
INGRESS_HOME="${INGRESS_HOME:-/root/.openclaw/community-ingress}"
ROUTE_REGISTRY="${INGRESS_HOME}/route-registry.json"
SERVICE_PATH="/etc/systemd/system/${SERVICE_NAME}"

if grep -Eq '^COMMUNITY_WEBHOOK_PUBLIC_HOST=(127\.0\.0\.1|localhost)?$' "${ENV_FILE}"; then
  echo "warning: COMMUNITY_WEBHOOK_PUBLIC_HOST is loopback. webhook delivery will fail unless community server is on the same host." >&2
fi

bash "${WORKSPACE_ROOT}/scripts/install-community-ingress-service.sh"

python3 - "${ROUTE_REGISTRY}" "${AGENT_SLUG}" "${WORKSPACE_ROOT}" "${SERVICE_NAME}" "${WEBHOOK_PATH}" "${SEND_PATH}" "${SOCKET_PATH}" <<'PY'
import json
import os
import sys

registry_path, slug, workspace_root, service_name, webhook_path, send_path, socket_path = sys.argv[1:]
os.makedirs(os.path.dirname(registry_path), exist_ok=True)
try:
    with open(registry_path, "r", encoding="utf-8") as fh:
        data = json.load(fh)
except FileNotFoundError:
    data = {"agents": {}}

agents = data.setdefault("agents", {})
existing = agents.get(slug)
next_route = {
    "agent_slug": slug,
    "workspace_root": workspace_root,
    "service_name": service_name,
    "webhook_path": webhook_path,
    "send_path": send_path,
    "socket_path": socket_path,
}
if existing and existing != next_route:
    raise SystemExit(f"route conflict for agent_slug={slug}")
agents[slug] = next_route

with open(registry_path, "w", encoding="utf-8") as fh:
    json.dump(data, fh, ensure_ascii=False, indent=2)
    fh.write("\n")
PY

cat >"${SERVICE_PATH}" <<UNIT
[Unit]
Description=OpenClaw Community Integration Agent (${AGENT_SLUG})
After=network-online.target docker.service
Wants=network-online.target

[Service]
Type=simple
User=root
WorkingDirectory=${WORKSPACE_ROOT}
EnvironmentFile=-${ENV_FILE}
Environment=WORKSPACE_ROOT=${WORKSPACE_ROOT}
Environment=COMMUNITY_TRANSPORT=unix_socket
Environment=COMMUNITY_AGENT_SOCKET_PATH=${SOCKET_PATH}
Environment=COMMUNITY_INGRESS_HOME=${INGRESS_HOME}
ExecStart=${NODE_BIN} ${WORKSPACE_ROOT}/scripts/community-webhook-server.mjs
Restart=always
RestartSec=5
NoNewPrivileges=true
PrivateTmp=true
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
UNIT

chmod 644 "${SERVICE_PATH}"
systemctl daemon-reload
systemctl enable "${SERVICE_NAME}"
systemctl restart "${SERVICE_NAME}" || systemctl start "${SERVICE_NAME}"
for _ in $(seq 1 50); do
  if [[ -S "${SOCKET_PATH}" ]]; then
    break
  fi
  sleep 0.2
done
if [[ ! -S "${SOCKET_PATH}" ]]; then
  echo "agent socket did not become ready: ${SOCKET_PATH}" >&2
  exit 1
fi
systemctl status "${SERVICE_NAME}" --no-pager
