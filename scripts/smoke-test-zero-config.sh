#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TEMPLATE_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
TEST_ROOT="${1:-/root/openclaw-zero-config-smoke}"
WORKSPACE_ROOT="${TEST_ROOT}/workspace"
BOOTSTRAP_METADATA="${WORKSPACE_ROOT}/.openclaw/community-agent.bootstrap.json"

rm -rf "${TEST_ROOT}"
mkdir -p "${TEST_ROOT}"

bash "${TEMPLATE_ROOT}/scripts/bootstrap-community-agent-template.sh" "${WORKSPACE_ROOT}"
bash "${WORKSPACE_ROOT}/scripts/install-community-webhook-service.sh"

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
print(data.get(key, "") or "")
PY
}

SERVICE_NAME="$(json_get service_name)"
if [[ -z "${SERVICE_NAME}" ]]; then
  echo "missing service_name in ${BOOTSTRAP_METADATA}" >&2
  exit 1
fi

systemctl status "${SERVICE_NAME}" --no-pager
echo "zero-config smoke test completed for ${SERVICE_NAME}"
