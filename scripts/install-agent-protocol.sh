#!/usr/bin/env bash
set -euo pipefail

WORKSPACE_ROOT="${WORKSPACE_ROOT:-/root/.openclaw/workspace}"
SKILL_ROOT="${SKILL_ROOT:-${WORKSPACE_ROOT}/skills/CommunityIntegrationSkill}"
TEMPLATE_HOME="${COMMUNITY_TEMPLATE_HOME:-${WORKSPACE_ROOT}/.openclaw/community-agent-template}"
SOURCE_PROTOCOL="${SKILL_ROOT}/assets/AGENT_PROTOCOL.md"
TARGET_PROTOCOL="${TEMPLATE_HOME}/assets/AGENT_PROTOCOL.md"

if [[ ! -f "${SOURCE_PROTOCOL}" ]]; then
  echo "missing agent protocol asset: ${SOURCE_PROTOCOL}" >&2
  exit 1
fi

mkdir -p "$(dirname "${TARGET_PROTOCOL}")"
install -m 0644 "${SOURCE_PROTOCOL}" "${TARGET_PROTOCOL}"
echo "installed agent protocol -> ${TARGET_PROTOCOL}"
