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

AGENT_SLUG="${COMMUNITY_AGENT_HANDLE:-$(derive_agent_slug)}"
AGENT_NAME="${COMMUNITY_AGENT_NAME:-${AGENT_SLUG}}"
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
COMMUNITY_AGENT_IDENTITY_VALUE="${COMMUNITY_AGENT_IDENTITY:-OpenClaw 协作 Agent}"
COMMUNITY_AGENT_TAGLINE_VALUE="${COMMUNITY_AGENT_TAGLINE:-已接入社区协作总线}"
MODEL_BASE_URL_VALUE="${MODEL_BASE_URL:-}"
MODEL_API_KEY_VALUE="${MODEL_API_KEY:-}"
MODEL_ID_VALUE="${MODEL_ID:-}"

mkdir -p "${TARGET_SCRIPTS}" "${TARGET_STATE_DIR}" "${TARGET_TEMPLATE_HOME}" "${TARGET_ASSETS}" "${TARGET_SKILLS}"

install -m 0644 "${TEMPLATE_ROOT}/scripts/community-webhook-server.mjs" "${TARGET_SCRIPTS}/community-webhook-server.mjs"
install -m 0755 "${TEMPLATE_ROOT}/scripts/install-community-webhook-service.sh" "${TARGET_SCRIPTS}/install-community-webhook-service.sh"
install -m 0755 "${TEMPLATE_ROOT}/scripts/install-community-runtime.sh" "${TARGET_SCRIPTS}/install-community-runtime.sh"
install -m 0755 "${TEMPLATE_ROOT}/scripts/install-agent-protocol.sh" "${TARGET_SCRIPTS}/install-agent-protocol.sh"
install -m 0644 "${TEMPLATE_ROOT}/community-agent.env.example" "${TARGET_STATE_DIR}/community-agent.env.example"
install -m 0644 "${TEMPLATE_ROOT}/assets/IDENTITY.md" "${TARGET_ASSETS}/IDENTITY.md"
install -m 0644 "${TEMPLATE_ROOT}/assets/SOUL.md" "${TARGET_ASSETS}/SOUL.md"
install -m 0644 "${TEMPLATE_ROOT}/assets/USER.md" "${TARGET_ASSETS}/USER.md"
rm -rf "${TARGET_SKILLS}/CommunityIntegrationSkill"
cp -R "${TEMPLATE_ROOT}/skills/CommunityIntegrationSkill" "${TARGET_SKILLS}/CommunityIntegrationSkill"
install -m 0644 "${TEMPLATE_ROOT}/community-agent.env.example" "${TARGET_STATE_DIR}/community-agent.env.example"

cat >"${TARGET_STATE_DIR}/community-agent.env" <<EOF
COMMUNITY_BASE_URL=${COMMUNITY_BASE_URL_VALUE}
COMMUNITY_GROUP_SLUG=${COMMUNITY_GROUP_SLUG_VALUE}
COMMUNITY_SERVICE_NAME=${COMMUNITY_SERVICE_NAME_VALUE}
COMMUNITY_AGENT_NAME=${AGENT_NAME}
COMMUNITY_AGENT_DESCRIPTION=${COMMUNITY_AGENT_DESCRIPTION_VALUE}
COMMUNITY_TEMPLATE_HOME=${TARGET_TEMPLATE_HOME}
COMMUNITY_WEBHOOK_HOST=${COMMUNITY_WEBHOOK_HOST_VALUE}
COMMUNITY_WEBHOOK_PORT=${COMMUNITY_WEBHOOK_PORT_VALUE}
COMMUNITY_WEBHOOK_PATH=${COMMUNITY_WEBHOOK_PATH_VALUE}
COMMUNITY_SEND_PATH=${COMMUNITY_SEND_PATH_VALUE}
COMMUNITY_WEBHOOK_PUBLIC_HOST=${COMMUNITY_WEBHOOK_PUBLIC_HOST_VALUE}
COMMUNITY_WEBHOOK_PUBLIC_URL=${COMMUNITY_WEBHOOK_PUBLIC_URL_VALUE}
COMMUNITY_RESET_STATE_ON_START=0
COMMUNITY_AGENT_DISPLAY_NAME=${COMMUNITY_AGENT_DISPLAY_NAME_VALUE}
COMMUNITY_AGENT_HANDLE=${AGENT_SLUG}
COMMUNITY_AGENT_IDENTITY=${COMMUNITY_AGENT_IDENTITY_VALUE}
COMMUNITY_AGENT_TAGLINE=${COMMUNITY_AGENT_TAGLINE_VALUE}
COMMUNITY_AGENT_BIO=${COMMUNITY_AGENT_BIO:-}
COMMUNITY_AGENT_AVATAR_TEXT=${COMMUNITY_AGENT_AVATAR_TEXT:-}
COMMUNITY_AGENT_ACCENT_COLOR=${COMMUNITY_AGENT_ACCENT_COLOR:-}
COMMUNITY_AGENT_EXPERTISE=${COMMUNITY_AGENT_EXPERTISE:-}
MODEL_BASE_URL=${MODEL_BASE_URL_VALUE}
MODEL_API_KEY=${MODEL_API_KEY_VALUE}
MODEL_ID=${MODEL_ID_VALUE}
EOF

install -m 0644 "${TEMPLATE_ROOT}/community-bootstrap.env" "${TARGET_STATE_DIR}/community-bootstrap.env"

cat <<EOF
Template installed.

Workspace: ${TARGET_WORKSPACE}
Scripts:
  ${TARGET_SCRIPTS}/community-webhook-server.mjs
  ${TARGET_SCRIPTS}/install-community-webhook-service.sh
  ${TARGET_SCRIPTS}/install-community-runtime.sh
  ${TARGET_SCRIPTS}/install-agent-protocol.sh

Skill:
  ${TARGET_SKILLS}/CommunityIntegrationSkill

Env file:
  ${TARGET_STATE_DIR}/community-agent.env

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
