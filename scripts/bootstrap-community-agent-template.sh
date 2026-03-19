#!/usr/bin/env bash
set -euo pipefail

TEMPLATE_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BOOTSTRAP_CONFIG="${TEMPLATE_ROOT}/community-bootstrap.env"
TARGET_WORKSPACE="${1:-/root/.openclaw/workspace}"
TARGET_SCRIPTS="${TARGET_WORKSPACE}/scripts"
TARGET_STATE_DIR="${TARGET_WORKSPACE}/.openclaw"
TARGET_TEMPLATE_HOME="${TARGET_STATE_DIR}/community-agent-template"
TARGET_ASSETS="${TARGET_TEMPLATE_HOME}/assets"
TARGET_SKILLS="${TARGET_WORKSPACE}/skills"
BOOTSTRAP_METADATA_PATH="${TARGET_STATE_DIR}/community-agent.bootstrap.json"
ENV_FILE_PATH="${TARGET_STATE_DIR}/community-agent.env"
AGENT_RUN_DIR="${TARGET_STATE_DIR}/run"
INGRESS_HOME_VALUE="${COMMUNITY_INGRESS_HOME:-/root/.openclaw/community-ingress}"

if [[ -f "${BOOTSTRAP_CONFIG}" ]]; then
  # shellcheck disable=SC1090
  source "${BOOTSTRAP_CONFIG}"
fi

derive_agent_slug() {
  local candidate
  candidate="$(basename "${TARGET_WORKSPACE}")"
  if [[ "${candidate}" == "workspace" ]]; then
    candidate="$(basename "$(dirname "${TARGET_WORKSPACE}")")"
  fi
  candidate="$(printf '%s' "${candidate}" | tr '[:upper:]' '[:lower:]' | sed -E 's/[^a-z0-9_-]+/-/g; s/^-+//; s/-+$//')"
  if [[ -z "${candidate}" ]]; then
    candidate="openclaw-agent"
  fi
  printf '%s' "${candidate}"
}

hash_text() {
  local value="${1}"
  if command -v sha256sum >/dev/null 2>&1; then
    printf '%s' "${value}" | sha256sum | awk '{print substr($1, 1, 12)}'
    return
  fi
  if command -v shasum >/dev/null 2>&1; then
    printf '%s' "${value}" | shasum -a 256 | awk '{print substr($1, 1, 12)}'
    return
  fi
  python3 - "${value}" <<'PY'
import hashlib
import sys

print(hashlib.sha256(sys.argv[1].encode("utf-8")).hexdigest()[:12])
PY
}

compute_socket_path() {
  local ingress_home="${1}"
  local agent_slug="${2}"
  local slug_prefix hash socket_name
  slug_prefix="$(printf '%s' "${agent_slug}" | cut -c1-24)"
  hash="$(hash_text "${agent_slug}")"
  socket_name="${slug_prefix}-${hash}.sock"
  printf '%s/sockets/%s' "${ingress_home}" "${socket_name}"
}

quote_env_value() {
  local value="${1-}"
  value="${value//\'/\'\\\'\'}"
  printf "'%s'" "${value}"
}

AGENT_SLUG="${COMMUNITY_AGENT_HANDLE:-$(derive_agent_slug)}"
AGENT_NAME="${COMMUNITY_AGENT_NAME:-${AGENT_SLUG}}"
SOCKET_PATH_VALUE="$(compute_socket_path "${INGRESS_HOME_VALUE}" "${AGENT_SLUG}")"
COMMUNITY_SERVICE_NAME_VALUE="${COMMUNITY_SERVICE_NAME:-openclaw-community-webhook-${AGENT_SLUG}.service}"
COMMUNITY_BASE_URL_VALUE="${COMMUNITY_BASE_URL:-http://127.0.0.1:8000/api/v1}"
COMMUNITY_GROUP_SLUG_VALUE="${COMMUNITY_GROUP_SLUG:-public-lobby}"
COMMUNITY_WEBHOOK_HOST_VALUE="${COMMUNITY_WEBHOOK_HOST:-0.0.0.0}"
COMMUNITY_WEBHOOK_PORT_VALUE="${COMMUNITY_WEBHOOK_PORT:-8848}"
COMMUNITY_WEBHOOK_PUBLIC_HOST_VALUE="${COMMUNITY_WEBHOOK_PUBLIC_HOST:-host.docker.internal}"
COMMUNITY_WEBHOOK_PATH_VALUE="${COMMUNITY_WEBHOOK_PATH:-/webhook/${AGENT_SLUG}}"
COMMUNITY_SEND_PATH_VALUE="${COMMUNITY_SEND_PATH:-/send/${AGENT_SLUG}}"
COMMUNITY_WEBHOOK_PUBLIC_URL_VALUE="${COMMUNITY_WEBHOOK_PUBLIC_URL:-http://${COMMUNITY_WEBHOOK_PUBLIC_HOST_VALUE}:${COMMUNITY_WEBHOOK_PORT_VALUE}${COMMUNITY_WEBHOOK_PATH_VALUE}}"
COMMUNITY_AGENT_DESCRIPTION_VALUE="${COMMUNITY_AGENT_DESCRIPTION:-OpenClaw community-connected agent}"
COMMUNITY_AGENT_DISPLAY_NAME_VALUE="${COMMUNITY_AGENT_DISPLAY_NAME:-${AGENT_NAME}}"
COMMUNITY_AGENT_IDENTITY_VALUE="${COMMUNITY_AGENT_IDENTITY:-OpenClaw community agent}"
COMMUNITY_AGENT_TAGLINE_VALUE="${COMMUNITY_AGENT_TAGLINE:-Connected to the shared community ingress}"
MODEL_BASE_URL_VALUE="${MODEL_BASE_URL:-}"
MODEL_API_KEY_VALUE="${MODEL_API_KEY:-}"
MODEL_ID_VALUE="${MODEL_ID:-}"

mkdir -p "${TARGET_SCRIPTS}" "${TARGET_STATE_DIR}" "${TARGET_TEMPLATE_HOME}" "${TARGET_ASSETS}" "${TARGET_SKILLS}"
mkdir -p "${AGENT_RUN_DIR}"

install -m 0644 "${TEMPLATE_ROOT}/scripts/community-webhook-server.mjs" "${TARGET_SCRIPTS}/community-webhook-server.mjs"
install -m 0644 "${TEMPLATE_ROOT}/scripts/community-ingress-server.mjs" "${TARGET_SCRIPTS}/community-ingress-server.mjs"
install -m 0755 "${TEMPLATE_ROOT}/scripts/install-community-webhook-service.sh" "${TARGET_SCRIPTS}/install-community-webhook-service.sh"
install -m 0755 "${TEMPLATE_ROOT}/scripts/install-community-ingress-service.sh" "${TARGET_SCRIPTS}/install-community-ingress-service.sh"
install -m 0755 "${TEMPLATE_ROOT}/scripts/install-community-runtime.sh" "${TARGET_SCRIPTS}/install-community-runtime.sh"
install -m 0755 "${TEMPLATE_ROOT}/scripts/install-agent-protocol.sh" "${TARGET_SCRIPTS}/install-agent-protocol.sh"
install -m 0644 "${TEMPLATE_ROOT}/community-agent.env.example" "${TARGET_STATE_DIR}/community-agent.env.example"
install -m 0644 "${TEMPLATE_ROOT}/assets/IDENTITY.md" "${TARGET_ASSETS}/IDENTITY.md"
install -m 0644 "${TEMPLATE_ROOT}/assets/SOUL.md" "${TARGET_ASSETS}/SOUL.md"
install -m 0644 "${TEMPLATE_ROOT}/assets/USER.md" "${TARGET_ASSETS}/USER.md"
rm -rf "${TARGET_SKILLS}/CommunityIntegrationSkill"
cp -R "${TEMPLATE_ROOT}/skills/CommunityIntegrationSkill" "${TARGET_SKILLS}/CommunityIntegrationSkill"
install -m 0644 "${TEMPLATE_ROOT}/community-agent.env.example" "${TARGET_STATE_DIR}/community-agent.env.example"

{
  printf 'COMMUNITY_BASE_URL=%s\n' "$(quote_env_value "${COMMUNITY_BASE_URL_VALUE}")"
  printf 'COMMUNITY_GROUP_SLUG=%s\n' "$(quote_env_value "${COMMUNITY_GROUP_SLUG_VALUE}")"
  printf 'COMMUNITY_SERVICE_NAME=%s\n' "$(quote_env_value "${COMMUNITY_SERVICE_NAME_VALUE}")"
  printf 'COMMUNITY_AGENT_NAME=%s\n' "$(quote_env_value "${AGENT_NAME}")"
  printf 'COMMUNITY_AGENT_DESCRIPTION=%s\n' "$(quote_env_value "${COMMUNITY_AGENT_DESCRIPTION_VALUE}")"
  printf 'COMMUNITY_TEMPLATE_HOME=%s\n' "$(quote_env_value "${TARGET_TEMPLATE_HOME}")"
  printf 'COMMUNITY_INGRESS_HOME=%s\n' "$(quote_env_value "${INGRESS_HOME_VALUE}")"
  printf 'COMMUNITY_TRANSPORT=%s\n' "$(quote_env_value "unix_socket")"
  printf 'COMMUNITY_AGENT_SOCKET_PATH=%s\n' "$(quote_env_value "${SOCKET_PATH_VALUE}")"
  printf 'COMMUNITY_WEBHOOK_HOST=%s\n' "$(quote_env_value "${COMMUNITY_WEBHOOK_HOST_VALUE}")"
  printf 'COMMUNITY_WEBHOOK_PORT=%s\n' "$(quote_env_value "${COMMUNITY_WEBHOOK_PORT_VALUE}")"
  printf 'COMMUNITY_WEBHOOK_PATH=%s\n' "$(quote_env_value "${COMMUNITY_WEBHOOK_PATH_VALUE}")"
  printf 'COMMUNITY_SEND_PATH=%s\n' "$(quote_env_value "${COMMUNITY_SEND_PATH_VALUE}")"
  printf 'COMMUNITY_WEBHOOK_PUBLIC_HOST=%s\n' "$(quote_env_value "${COMMUNITY_WEBHOOK_PUBLIC_HOST_VALUE}")"
  printf 'COMMUNITY_WEBHOOK_PUBLIC_URL=%s\n' "$(quote_env_value "${COMMUNITY_WEBHOOK_PUBLIC_URL_VALUE}")"
  printf 'COMMUNITY_RESET_STATE_ON_START=%s\n' "$(quote_env_value "0")"
  printf 'COMMUNITY_AGENT_DISPLAY_NAME=%s\n' "$(quote_env_value "${COMMUNITY_AGENT_DISPLAY_NAME_VALUE}")"
  printf 'COMMUNITY_AGENT_HANDLE=%s\n' "$(quote_env_value "${AGENT_SLUG}")"
  printf 'COMMUNITY_AGENT_IDENTITY=%s\n' "$(quote_env_value "${COMMUNITY_AGENT_IDENTITY_VALUE}")"
  printf 'COMMUNITY_AGENT_TAGLINE=%s\n' "$(quote_env_value "${COMMUNITY_AGENT_TAGLINE_VALUE}")"
  printf 'COMMUNITY_AGENT_BIO=%s\n' "$(quote_env_value "${COMMUNITY_AGENT_BIO:-}")"
  printf 'COMMUNITY_AGENT_AVATAR_TEXT=%s\n' "$(quote_env_value "${COMMUNITY_AGENT_AVATAR_TEXT:-}")"
  printf 'COMMUNITY_AGENT_ACCENT_COLOR=%s\n' "$(quote_env_value "${COMMUNITY_AGENT_ACCENT_COLOR:-}")"
  printf 'COMMUNITY_AGENT_EXPERTISE=%s\n' "$(quote_env_value "${COMMUNITY_AGENT_EXPERTISE:-}")"
  printf 'MODEL_BASE_URL=%s\n' "$(quote_env_value "${MODEL_BASE_URL_VALUE}")"
  printf 'MODEL_API_KEY=%s\n' "$(quote_env_value "${MODEL_API_KEY_VALUE}")"
  printf 'MODEL_ID=%s\n' "$(quote_env_value "${MODEL_ID_VALUE}")"
} >"${ENV_FILE_PATH}"

cat >"${BOOTSTRAP_METADATA_PATH}" <<EOF
{
  "agent_slug": "${AGENT_SLUG}",
  "service_name": "${COMMUNITY_SERVICE_NAME_VALUE}",
  "community_base_url": "${COMMUNITY_BASE_URL_VALUE}",
  "ingress_home": "${INGRESS_HOME_VALUE}",
  "socket_path": "${SOCKET_PATH_VALUE}",
  "webhook_port": ${COMMUNITY_WEBHOOK_PORT_VALUE},
  "webhook_path": "${COMMUNITY_WEBHOOK_PATH_VALUE}",
  "send_path": "${COMMUNITY_SEND_PATH_VALUE}"
}
EOF

install -m 0644 "${TEMPLATE_ROOT}/community-bootstrap.env" "${TARGET_STATE_DIR}/community-bootstrap.env"

cat <<EOF
Template installed.

Workspace: ${TARGET_WORKSPACE}
Scripts:
  ${TARGET_SCRIPTS}/community-webhook-server.mjs
  ${TARGET_SCRIPTS}/community-ingress-server.mjs
  ${TARGET_SCRIPTS}/install-community-webhook-service.sh
  ${TARGET_SCRIPTS}/install-community-ingress-service.sh
  ${TARGET_SCRIPTS}/install-community-runtime.sh
  ${TARGET_SCRIPTS}/install-agent-protocol.sh

Skill:
  ${TARGET_SKILLS}/CommunityIntegrationSkill

Env file:
  ${ENV_FILE_PATH}

Bootstrap metadata:
  ${BOOTSTRAP_METADATA_PATH}

Template home:
  ${TARGET_TEMPLATE_HOME}

Assets:
  ${TARGET_ASSETS}/IDENTITY.md
  ${TARGET_ASSETS}/SOUL.md
  ${TARGET_ASSETS}/USER.md

Next:
  1. Run: bash ${TARGET_SCRIPTS}/install-community-webhook-service.sh

Important:
  - Current bootstrap config came from ${BOOTSTRAP_CONFIG}
  - Override values by editing ${TARGET_STATE_DIR}/community-bootstrap.env before rerunning bootstrap
  - Webhook uses unified port 8848 by default
  - Open TCP port 8848 in firewall/security group before testing webhook delivery
EOF
