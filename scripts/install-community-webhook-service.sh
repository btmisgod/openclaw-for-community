#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEFAULT_WORKSPACE_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
WORKSPACE_ROOT="${WORKSPACE_ROOT:-${DEFAULT_WORKSPACE_ROOT}}"
ENV_FILE="${WORKSPACE_ROOT}/.openclaw/community-agent.env"
NODE_BIN="$(command -v node)"

if [[ -z "${NODE_BIN}" ]]; then
  echo "node not found in PATH" >&2
  exit 1
fi

if [[ ! -f "${ENV_FILE}" ]]; then
  echo "missing env file: ${ENV_FILE}" >&2
  exit 1
fi

# shellcheck disable=SC1090
source "${ENV_FILE}"

SERVICE_NAME="${SERVICE_NAME:-${COMMUNITY_SERVICE_NAME:-openclaw-community-webhook.service}}"
SERVICE_PATH="/etc/systemd/system/${SERVICE_NAME}"

if grep -Eq '^COMMUNITY_WEBHOOK_PUBLIC_HOST=(127\.0\.0\.1|localhost)?$' "${ENV_FILE}"; then
  echo "warning: COMMUNITY_WEBHOOK_PUBLIC_HOST is loopback. webhook delivery will fail unless community server is on the same host." >&2
fi

cat >"${SERVICE_PATH}" <<UNIT
[Unit]
Description=OpenClaw Community Integration Receiver
After=network-online.target docker.service
Wants=network-online.target

[Service]
Type=simple
User=root
WorkingDirectory=${WORKSPACE_ROOT}
Environment=WORKSPACE_ROOT=${WORKSPACE_ROOT}
EnvironmentFile=-${ENV_FILE}
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
systemctl status "${SERVICE_NAME}" --no-pager
