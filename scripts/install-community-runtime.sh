#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEFAULT_WORKSPACE_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
WORKSPACE_ROOT="${WORKSPACE_ROOT:-${DEFAULT_WORKSPACE_ROOT}}"
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
