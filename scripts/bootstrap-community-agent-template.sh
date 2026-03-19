#!/usr/bin/env bash
set -euo pipefail

TEMPLATE_ROOT="/root/openclaw-community-agent-template"
TARGET_WORKSPACE="${1:-/root/.openclaw/workspace}"
TARGET_SCRIPTS="${TARGET_WORKSPACE}/scripts"
TARGET_STATE_DIR="${TARGET_WORKSPACE}/.openclaw"
TARGET_TEMPLATE_HOME="${TARGET_STATE_DIR}/community-agent-template"
TARGET_ASSETS="${TARGET_TEMPLATE_HOME}/assets"
TARGET_SKILLS="${TARGET_WORKSPACE}/skills"

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

if [[ ! -f "${TARGET_STATE_DIR}/community-agent.env" ]]; then
  install -m 0644 "${TEMPLATE_ROOT}/community-agent.env.example" "${TARGET_STATE_DIR}/community-agent.env"
fi

if ! grep -q '^COMMUNITY_TEMPLATE_HOME=' "${TARGET_STATE_DIR}/community-agent.env"; then
  printf '\nCOMMUNITY_TEMPLATE_HOME=%s\n' "${TARGET_TEMPLATE_HOME}" >>"${TARGET_STATE_DIR}/community-agent.env"
fi

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
  1. Edit ${TARGET_STATE_DIR}/community-agent.env
  2. Run: bash ${TARGET_SCRIPTS}/install-community-webhook-service.sh

Important:
  - Set COMMUNITY_WEBHOOK_PUBLIC_HOST to an address reachable from the community server
  - Webhook uses unified port 8848 by default
  - Open TCP port 8848 in firewall/security group before testing webhook delivery
EOF
