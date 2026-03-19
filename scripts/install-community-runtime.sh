#!/usr/bin/env bash
set -euo pipefail

WORKSPACE_ROOT="${WORKSPACE_ROOT:-/root/.openclaw/workspace}"
SKILL_ROOT="${SKILL_ROOT:-${WORKSPACE_ROOT}/skills/CommunityIntegrationSkill}"
SOURCE_RUNTIME="${SKILL_ROOT}/assets/community-runtime-v0.mjs"
TARGET_RUNTIME="${WORKSPACE_ROOT}/scripts/community-runtime-v0.mjs"

if [[ ! -f "${SOURCE_RUNTIME}" ]]; then
  echo "missing runtime asset: ${SOURCE_RUNTIME}" >&2
  exit 1
fi

mkdir -p "$(dirname "${TARGET_RUNTIME}")"
install -m 0644 "${SOURCE_RUNTIME}" "${TARGET_RUNTIME}"
echo "installed runtime -> ${TARGET_RUNTIME}"
