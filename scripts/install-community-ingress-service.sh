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

listener_pid_8848() {
  if command -v ss >/dev/null 2>&1; then
    ss -ltnp '( sport = :8848 )' 2>/dev/null | sed -n 's/.*pid=\([0-9][0-9]*\).*/\1/p' | head -n 1
    return
  fi
  if command -v lsof >/dev/null 2>&1; then
    lsof -nP -iTCP:8848 -sTCP:LISTEN -t 2>/dev/null | head -n 1
    return
  fi
}

service_main_pid() {
  local unit_name="${1}"
  systemctl show --property MainPID --value "${unit_name}" 2>/dev/null || true
}

stop_legacy_agent_listener_on_8848() {
  local listener_pid ingress_pid unit_name unit_pid
  listener_pid="$(listener_pid_8848)"
  if [[ -z "${listener_pid}" || "${listener_pid}" == "0" ]]; then
    return 0
  fi

  ingress_pid="$(service_main_pid "${SERVICE_NAME}")"
  if [[ -n "${ingress_pid}" && "${ingress_pid}" != "0" && "${listener_pid}" == "${ingress_pid}" ]]; then
    return 0
  fi

  while IFS= read -r unit_name; do
    [[ -n "${unit_name}" ]] || continue
    unit_pid="$(service_main_pid "${unit_name}")"
    if [[ -n "${unit_pid}" && "${unit_pid}" != "0" && "${unit_pid}" == "${listener_pid}" ]]; then
      echo "stopping legacy agent listener on 8848: ${unit_name}" >&2
      systemctl stop "${unit_name}" || true
      return 0
    fi
  done < <(systemctl list-units --type=service --all 'openclaw-community-webhook*.service' --no-legend --plain | awk '{print $1}')
}

INGRESS_HOME="${COMMUNITY_INGRESS_HOME:-$(json_get ingress_home)}"
INGRESS_HOME="${INGRESS_HOME:-/root/.openclaw/community-ingress}"
INGRESS_SCRIPT="${INGRESS_HOME}/community-ingress-server.mjs"
INGRESS_ENV="${INGRESS_HOME}/community-ingress.env"
ROUTE_REGISTRY="${INGRESS_HOME}/route-registry.json"
SERVICE_NAME="${COMMUNITY_INGRESS_SERVICE_NAME:-openclaw-community-ingress.service}"
SERVICE_PATH="/etc/systemd/system/${SERVICE_NAME}"

mkdir -p "${INGRESS_HOME}"
mkdir -p "${INGRESS_HOME}/sockets"
install -m 0644 "${WORKSPACE_ROOT}/scripts/community-ingress-server.mjs" "${INGRESS_SCRIPT}"

cat >"${INGRESS_ENV}" <<EOF
COMMUNITY_INGRESS_HOME='${INGRESS_HOME}'
COMMUNITY_ROUTE_REGISTRY='${ROUTE_REGISTRY}'
COMMUNITY_INGRESS_HOST='0.0.0.0'
COMMUNITY_INGRESS_PORT='8848'
EOF

if [[ ! -f "${ROUTE_REGISTRY}" ]]; then
  cat >"${ROUTE_REGISTRY}" <<'EOF'
{
  "agents": {}
}
EOF
fi

cat >"${SERVICE_PATH}" <<UNIT
[Unit]
Description=OpenClaw Community Ingress
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=root
WorkingDirectory=${INGRESS_HOME}
EnvironmentFile=-${INGRESS_ENV}
ExecStart=${NODE_BIN} ${INGRESS_SCRIPT}
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
stop_legacy_agent_listener_on_8848
systemctl restart "${SERVICE_NAME}" || systemctl start "${SERVICE_NAME}"
systemctl status "${SERVICE_NAME}" --no-pager
