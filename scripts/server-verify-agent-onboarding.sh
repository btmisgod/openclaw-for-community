#!/usr/bin/env bash
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TEMPLATE_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
TEST_ROOT="${1:-/root/openclaw-server-verify-agent-onboarding}"
WORKSPACE_ROOT="${TEST_ROOT}/workspace"
INGRESS_SERVICE_NAME="${COMMUNITY_INGRESS_SERVICE_NAME:-openclaw-community-ingress.service}"
INGRESS_BASE_URL="${COMMUNITY_INGRESS_BASE_URL:-http://127.0.0.1:8848}"
HEALTHZ_URL="${COMMUNITY_INGRESS_HEALTHZ_URL:-${INGRESS_BASE_URL}/healthz}"
FAILURES=0
WARNINGS=0

log_pass() {
  echo "PASS $1"
}

log_fail() {
  echo "FAIL $1"
  FAILURES=$((FAILURES + 1))
}

log_warn() {
  echo "WARN $1"
  WARNINGS=$((WARNINGS + 1))
}

json_get() {
  local file="$1"
  local key="$2"
  if command -v jq >/dev/null 2>&1; then
    jq -r --arg key "$key" '.[$key] // empty' "$file"
    return
  fi
  python3 - "$file" "$key" <<'PY'
import json
import sys

path = sys.argv[1]
key = sys.argv[2]
with open(path, "r", encoding="utf-8") as fh:
    data = json.load(fh)
print(data.get(key, "") or "")
PY
}

registry_socket_path() {
  local registry_path="$1"
  local slug="$2"
  python3 - "$registry_path" "$slug" <<'PY'
import json
import sys

path = sys.argv[1]
slug = sys.argv[2]
with open(path, "r", encoding="utf-8") as fh:
    data = json.load(fh)
route = (data.get("agents") or {}).get(slug) or {}
print(route.get("socket_path", "") or "")
PY
}

state_token_ready() {
  local state_path="$1"
  python3 - "$state_path" <<'PY'
import json
import sys

path = sys.argv[1]
with open(path, "r", encoding="utf-8") as fh:
    data = json.load(fh)
if not data.get("token"):
    raise SystemExit(1)
if not data.get("groupId"):
    raise SystemExit(1)
if not data.get("agentId"):
    raise SystemExit(1)
PY
}

cli_status_has_token() {
  local cli_output="$1"
  python3 - "$cli_output" <<'PY'
import json
import sys

path = sys.argv[1]
with open(path, "r", encoding="utf-8") as fh:
    data = json.load(fh)
if data.get("hasToken") is not True:
    raise SystemExit(1)
PY
}

cleanup_agent_service() {
  local service_name="$1"
  local service_path="/etc/systemd/system/${service_name}"
  if systemctl list-unit-files "$service_name" >/dev/null 2>&1; then
    systemctl stop "$service_name" >/dev/null 2>&1 || true
    systemctl disable "$service_name" >/dev/null 2>&1 || true
  fi
  rm -f "$service_path"
}

remove_route_slug() {
  local registry_path="$1"
  local slug="$2"
  if [[ ! -f "$registry_path" ]]; then
    return 0
  fi
  python3 - "$registry_path" "$slug" <<'PY'
import json
import sys

path = sys.argv[1]
slug = sys.argv[2]
with open(path, "r", encoding="utf-8") as fh:
    data = json.load(fh)
agents = data.get("agents") or {}
if slug in agents:
    del agents[slug]
with open(path, "w", encoding="utf-8") as fh:
    json.dump(data, fh, ensure_ascii=False, indent=2)
    fh.write("\n")
PY
}

wait_for_socket() {
  local socket_path="$1"
  local attempts="${2:-120}"
  local delay="${3:-0.5}"
  local i
  for ((i=1; i<=attempts; i+=1)); do
    if [[ -S "$socket_path" ]]; then
      SOCKET_WAIT_POLLS="$i"
      SOCKET_WAIT_SECONDS="$(awk "BEGIN { printf \"%.1f\", ${i} * ${delay} }")"
      return 0
    fi
    sleep "$delay"
  done
  SOCKET_WAIT_POLLS="$attempts"
  SOCKET_WAIT_SECONDS="$(awk "BEGIN { printf \"%.1f\", ${attempts} * ${delay} }")"
  return 1
}

wait_for_saved_state() {
  local state_path="$1"
  local attempts="${2:-240}"
  local delay="${3:-0.5}"
  local i
  for ((i=1; i<=attempts; i+=1)); do
    if [[ -f "$state_path" ]] && state_token_ready "$state_path"; then
      STATE_WAIT_POLLS="$i"
      STATE_WAIT_SECONDS="$(awk "BEGIN { printf \"%.1f\", ${i} * ${delay} }")"
      return 0
    fi
    sleep "$delay"
  done
  STATE_WAIT_POLLS="$attempts"
  STATE_WAIT_SECONDS="$(awk "BEGIN { printf \"%.1f\", ${attempts} * ${delay} }")"
  return 1
}

wait_for_http() {
  local url="$1"
  local attempts="${2:-50}"
  local delay="${3:-0.2}"
  local output_path="$4"
  local i
  for ((i=1; i<=attempts; i+=1)); do
    if curl -fsS "$url" >"$output_path" 2>/dev/null; then
      return 0
    fi
    sleep "$delay"
  done
  return 1
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
  local service_name="$1"
  systemctl show --property MainPID --value "$service_name" 2>/dev/null || true
}

check_ingress_owns_8848() {
  local listener_pid ingress_pid
  listener_pid="$(listener_pid_8848)"
  ingress_pid="$(service_main_pid "$INGRESS_SERVICE_NAME")"
  [[ -n "$listener_pid" && "$listener_pid" != "0" && -n "$ingress_pid" && "$ingress_pid" != "0" && "$listener_pid" == "$ingress_pid" ]]
}

verify_ingress_healthz() {
  local healthz_path="$1"
  python3 - "$healthz_path" <<'PY'
import json
import sys

path = sys.argv[1]
with open(path, "r", encoding="utf-8") as fh:
    data = json.load(fh)
if data.get("mode") != "community_ingress":
    raise SystemExit(1)
if not data.get("listen"):
    raise SystemExit(1)
PY
}

run_step() {
  local label="$1"
  shift
  if "$@"; then
    log_pass "$label"
    return 0
  fi
  log_fail "$label"
  return 1
}

mkdir -p "$TEST_ROOT"

TEST_SLUG="$(basename "$TEST_ROOT" | tr '[:upper:]' '[:lower:]' | sed -E 's/[^a-z0-9_-]+/-/g; s/^-+//; s/-+$//')"
[[ -n "$TEST_SLUG" ]] || TEST_SLUG="openclaw-server-verify"
AGENT_SERVICE_NAME="openclaw-community-webhook-${TEST_SLUG}.service"
INGRESS_HOME="${COMMUNITY_INGRESS_HOME:-/root/.openclaw/community-ingress}"
ROUTE_REGISTRY="${INGRESS_HOME}/route-registry.json"
SOCKET_PATH="${TEST_ROOT}/workspace/.openclaw/run/${TEST_SLUG}.sock"
BOOTSTRAP_METADATA="${WORKSPACE_ROOT}/.openclaw/community-agent.bootstrap.json"
ENV_FILE="${WORKSPACE_ROOT}/.openclaw/community-agent.env"
TEMPLATE_HOME="${WORKSPACE_ROOT}/.openclaw/community-agent-template"
STATE_PATH="${TEMPLATE_HOME}/state/community-webhook-state.json"
CLI_STATUS_OUTPUT="/tmp/${TEST_SLUG}.cli-status.json"
SEND_RESPONSE_BODY="/tmp/${TEST_SLUG}.send.out"
WEBHOOK_RESPONSE_BODY="/tmp/${TEST_SLUG}.webhook.out"
HEALTHZ_RESPONSE_BODY="/tmp/${TEST_SLUG}.healthz.json"
INSTALL_LOG="/tmp/${TEST_SLUG}.install.log"
REGISTRY_SOCKET_PATH=""
EXPECTED_SOCKET_PATH=""
ACTUAL_SOCKET_PATH="missing"
SOCKET_READY=0
INSTALL_EXIT_CODE=0
INSTALL_WINDOW_MISSED=0
SOCKET_WAIT_POLLS=0
SOCKET_WAIT_SECONDS=0
STATE_WAIT_POLLS=0
STATE_WAIT_SECONDS=0

cleanup_agent_service "$AGENT_SERVICE_NAME"
remove_route_slug "$ROUTE_REGISTRY" "$TEST_SLUG"
rm -rf "$TEST_ROOT"
rm -f "$SOCKET_PATH" "$SEND_RESPONSE_BODY" "$WEBHOOK_RESPONSE_BODY" "$HEALTHZ_RESPONSE_BODY" "$INSTALL_LOG" "$CLI_STATUS_OUTPUT"
systemctl daemon-reload >/dev/null 2>&1 || true
log_pass "fresh workspace prepared"

if bash "$TEMPLATE_ROOT/scripts/bootstrap-community-agent-template.sh" "$WORKSPACE_ROOT" >/tmp/${TEST_SLUG}.bootstrap.log 2>&1; then
  log_pass "bootstrap template"
else
  log_fail "bootstrap template"
fi

if [[ -f "$BOOTSTRAP_METADATA" ]]; then
  AGENT_SERVICE_NAME="$(json_get "$BOOTSTRAP_METADATA" service_name)"
  TEST_SLUG="$(json_get "$BOOTSTRAP_METADATA" agent_slug)"
  SOCKET_PATH="$(json_get "$BOOTSTRAP_METADATA" socket_path)"
  EXPECTED_SOCKET_PATH="$SOCKET_PATH"
  SEND_PATH="$(json_get "$BOOTSTRAP_METADATA" send_path)"
  WEBHOOK_PATH="$(json_get "$BOOTSTRAP_METADATA" webhook_path)"
  INGRESS_HOME="$(json_get "$BOOTSTRAP_METADATA" ingress_home)"
  INGRESS_HOME="${INGRESS_HOME:-/root/.openclaw/community-ingress}"
  ROUTE_REGISTRY="${INGRESS_HOME}/route-registry.json"
else
  SEND_PATH=""
  WEBHOOK_PATH=""
fi

if [[ -f "$ENV_FILE" ]]; then
  # shellcheck disable=SC1090
  source "$ENV_FILE"
fi
TEMPLATE_HOME="${COMMUNITY_TEMPLATE_HOME:-$TEMPLATE_HOME}"
STATE_PATH="${TEMPLATE_HOME}/state/community-webhook-state.json"

if bash "$WORKSPACE_ROOT/scripts/install-community-webhook-service.sh" >"$INSTALL_LOG" 2>&1; then
  log_pass "install community webhook service"
else
  INSTALL_EXIT_CODE=$?
  log_warn "install community webhook service exited ${INSTALL_EXIT_CODE}; continuing to final readiness checks"
fi

if grep -q 'install window missed' "$INSTALL_LOG" 2>/dev/null; then
  INSTALL_WINDOW_MISSED=1
  log_warn "socket not ready during install window"
fi

run_step "ingress service" systemctl is-active --quiet "$INGRESS_SERVICE_NAME"
run_step "agent service" systemctl is-active --quiet "$AGENT_SERVICE_NAME"
run_step "ingress owns 8848" check_ingress_owns_8848

if [[ -n "$TEST_SLUG" && -f "$ROUTE_REGISTRY" ]]; then
  REGISTRY_SOCKET_PATH="$(registry_socket_path "$ROUTE_REGISTRY" "$TEST_SLUG")"
  if [[ -n "$REGISTRY_SOCKET_PATH" && "$REGISTRY_SOCKET_PATH" == "$EXPECTED_SOCKET_PATH" ]]; then
    log_pass "route registry"
  else
    log_fail "route registry missing slug"
  fi
else
  log_fail "route registry missing slug"
fi

if wait_for_socket "$EXPECTED_SOCKET_PATH" "${VERIFY_SOCKET_WAIT_ATTEMPTS:-120}" "${VERIFY_SOCKET_WAIT_DELAY:-0.5}"; then
  SOCKET_READY=1
  ACTUAL_SOCKET_PATH="$EXPECTED_SOCKET_PATH"
  if [[ "$INSTALL_WINDOW_MISSED" == "1" ]]; then
    log_warn "install window missed; eventual readiness confirmed"
  fi
  log_pass "socket eventually ready after ${SOCKET_WAIT_SECONDS}s (${SOCKET_WAIT_POLLS} polls)"
else
  if systemctl is-active --quiet "$AGENT_SERVICE_NAME"; then
    log_fail "agent service running but socket not created"
  else
    log_fail "socket missing"
  fi
fi

if wait_for_saved_state "$STATE_PATH" "${VERIFY_STATE_WAIT_ATTEMPTS:-240}" "${VERIFY_STATE_WAIT_DELAY:-0.5}"; then
  log_pass "community state ready after ${STATE_WAIT_SECONDS}s (${STATE_WAIT_POLLS} polls)"
else
  log_fail "community state not ready"
fi

if node "$WORKSPACE_ROOT/skills/CommunityIntegrationSkill/scripts/community-agent-cli.mjs" status >"$CLI_STATUS_OUTPUT" 2>/dev/null && cli_status_has_token "$CLI_STATUS_OUTPUT"; then
  log_pass "cli status has token"
else
  log_fail "cli status missing token"
fi

if wait_for_http "$HEALTHZ_URL" 50 0.2 "$HEALTHZ_RESPONSE_BODY" && verify_ingress_healthz "$HEALTHZ_RESPONSE_BODY"; then
  log_pass "ingress healthz"
else
  log_fail "healthz not served by ingress"
fi

SEND_CODE="000"
if [[ "$SOCKET_READY" == "1" && -n "$SEND_PATH" ]]; then
  SEND_CODE="$(curl -sS -o "$SEND_RESPONSE_BODY" -w "%{http_code}" \
    -H 'Content-Type: application/json' \
    -X POST "${INGRESS_BASE_URL}${SEND_PATH}" \
    -d '{"group_id":"54b12c32-dbd3-46d8-97ee-22bf8a499709","content":{"text":"server verify onboarding smoke"}}' || true)"
fi
if [[ "$SOCKET_READY" != "1" ]]; then
  log_fail "send route skipped because socket never became ready"
elif [[ "$SEND_CODE" == "202" ]]; then
  log_pass "send route"
else
  log_fail "send route expected 202 got ${SEND_CODE}"
fi

WEBHOOK_CODE="000"
if [[ "$SOCKET_READY" == "1" && -n "$WEBHOOK_PATH" ]]; then
  WEBHOOK_CODE="$(curl -sS -o "$WEBHOOK_RESPONSE_BODY" -w "%{http_code}" \
    -H 'Content-Type: application/json' \
    -H 'x-community-webhook-signature: invalid' \
    -X POST "${INGRESS_BASE_URL}${WEBHOOK_PATH}" \
    -d '{}' || true)"
fi
if [[ "$SOCKET_READY" != "1" ]]; then
  log_fail "webhook invalid signature skipped because socket never became ready"
elif [[ "$WEBHOOK_CODE" == "401" ]]; then
  log_pass "webhook invalid signature"
else
  log_fail "webhook invalid signature expected 401 got ${WEBHOOK_CODE}"
fi

if [[ "$FAILURES" -eq 0 ]]; then
  log_pass "onboarding end-to-end"
fi

echo "INFO commit $(git -C "$TEMPLATE_ROOT" rev-parse HEAD 2>/dev/null || echo unknown)"
echo "INFO test_root ${TEST_ROOT}"
echo "INFO ingress_service ${INGRESS_SERVICE_NAME}"
echo "INFO agent_service ${AGENT_SERVICE_NAME}"
echo "INFO route_registry ${ROUTE_REGISTRY}"
echo "INFO socket_path_expected ${EXPECTED_SOCKET_PATH}"
echo "INFO socket_path_registry ${REGISTRY_SOCKET_PATH}"
echo "INFO socket_path_filesystem ${ACTUAL_SOCKET_PATH}"
echo "INFO state_path ${STATE_PATH}"
echo "INFO healthz_url ${HEALTHZ_URL}"
echo "INFO install_exit_code ${INSTALL_EXIT_CODE}"
echo "INFO install_window_missed ${INSTALL_WINDOW_MISSED}"
echo "INFO verify_socket_wait_seconds ${SOCKET_WAIT_SECONDS}"
echo "INFO verify_socket_wait_polls ${SOCKET_WAIT_POLLS}"
echo "INFO verify_state_wait_seconds ${STATE_WAIT_SECONDS}"
echo "INFO verify_state_wait_polls ${STATE_WAIT_POLLS}"
echo "INFO warnings ${WARNINGS}"

if [[ "$FAILURES" -eq 0 ]]; then
  echo "RESULT PASS"
  exit 0
fi

echo "RESULT FAIL (${FAILURES} checks failed)"
exit 1
