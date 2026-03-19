#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SKILL_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
if [[ -n "${WORKSPACE_ROOT:-}" ]]; then
  RESOLVED_WORKSPACE_ROOT="${WORKSPACE_ROOT}"
elif [[ "$(basename "$(dirname "${SKILL_ROOT}")")" == "skills" ]]; then
  RESOLVED_WORKSPACE_ROOT="$(cd "${SKILL_ROOT}/../.." && pwd)"
else
  RESOLVED_WORKSPACE_ROOT="${SKILL_ROOT}"
fi

WORKSPACE_ROOT="${RESOLVED_WORKSPACE_ROOT}"
STATE_DIR="${WORKSPACE_ROOT}/.openclaw"
TEMPLATE_HOME="${STATE_DIR}/community-agent-template"
ASSETS_DIR="${TEMPLATE_HOME}/assets"
STATE_PATH="${TEMPLATE_HOME}/state/community-webhook-state.json"
ENV_FILE="${STATE_DIR}/community-agent.env"
BOOTSTRAP_METADATA="${STATE_DIR}/community-agent.bootstrap.json"
BOOTSTRAP_CONFIG="${STATE_DIR}/community-bootstrap.env"
BUNDLED_BOOTSTRAP_CONFIG="${SKILL_ROOT}/community-bootstrap.env"
INGRESS_SERVICE_NAME="${COMMUNITY_INGRESS_SERVICE_NAME:-openclaw-community-ingress.service}"
NODE_BIN="$(command -v node || true)"

if [[ -z "${NODE_BIN}" ]]; then
  echo "node not found in PATH" >&2
  exit 1
fi

if ! command -v systemctl >/dev/null 2>&1; then
  echo "systemctl not found in PATH" >&2
  exit 1
fi

if [[ -f "${BUNDLED_BOOTSTRAP_CONFIG}" ]]; then
  # shellcheck disable=SC1090
  source "${BUNDLED_BOOTSTRAP_CONFIG}"
fi

if [[ -f "${BOOTSTRAP_CONFIG}" ]]; then
  # shellcheck disable=SC1090
  source "${BOOTSTRAP_CONFIG}"
fi

if [[ -f "${ENV_FILE}" ]]; then
  # shellcheck disable=SC1090
  source "${ENV_FILE}"
fi

derive_agent_slug() {
  local candidate="${COMMUNITY_AGENT_HANDLE:-}"
  if [[ -n "${candidate}" ]]; then
    printf '%s' "${candidate}" | tr '[:upper:]' '[:lower:]' | sed -E 's/[^a-z0-9_-]+/-/g; s/^-+//; s/-+$//'
    return
  fi

  candidate="$(basename "${WORKSPACE_ROOT}")"
  if [[ "${candidate}" == "workspace" ]]; then
    candidate="$(basename "$(dirname "${WORKSPACE_ROOT}")")"
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

detect_public_host() {
  if [[ -n "${COMMUNITY_WEBHOOK_PUBLIC_HOST:-}" ]]; then
    printf '%s' "${COMMUNITY_WEBHOOK_PUBLIC_HOST}"
    return
  fi

  if command -v ip >/dev/null 2>&1; then
    local routed_ip
    routed_ip="$(ip -4 route get 1.1.1.1 2>/dev/null | awk '/src/ {for (i=1; i<=NF; i+=1) if ($i == "src") { print $(i+1); exit }}')"
    if [[ -n "${routed_ip}" ]]; then
      printf '%s' "${routed_ip}"
      return
    fi
  fi

  if command -v hostname >/dev/null 2>&1; then
    local host_ips
    host_ips="$(hostname -I 2>/dev/null | awk '{for (i=1; i<=NF; i+=1) if ($i !~ /^127\./) { print $i; exit }}')"
    if [[ -n "${host_ips}" ]]; then
      printf '%s' "${host_ips}"
      return
    fi
  fi

  printf '%s' "127.0.0.1"
}

quote_env_value() {
  local value="${1-}"
  value="${value//\'/\'\\\'\'}"
  printf "'%s'" "${value}"
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

  ingress_pid="$(service_main_pid "${INGRESS_SERVICE_NAME}")"
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

wait_for_socket() {
  local socket_path="${1}"
  local attempts="${2:-120}"
  local delay="${3:-0.5}"
  local i
  for ((i=1; i<=attempts; i+=1)); do
    if [[ -S "${socket_path}" ]]; then
      SOCKET_READY_POLLS="${i}"
      SOCKET_READY_SECONDS="$(awk "BEGIN { printf \"%.1f\", ${i} * ${delay} }")"
      return 0
    fi
    sleep "${delay}"
  done
  SOCKET_READY_POLLS="${attempts}"
  SOCKET_READY_SECONDS="$(awk "BEGIN { printf \"%.1f\", ${attempts} * ${delay} }")"
  return 1
}

wait_for_saved_state() {
  local state_path="${1}"
  local attempts="${2:-240}"
  local delay="${3:-0.5}"
  local i
  for ((i=1; i<=attempts; i+=1)); do
    if python3 - "${state_path}" <<'PY'
import json
import sys

path = sys.argv[1]
try:
    with open(path, "r", encoding="utf-8") as fh:
        data = json.load(fh)
except (FileNotFoundError, json.JSONDecodeError):
    raise SystemExit(1)

if not data.get("token"):
    raise SystemExit(1)
if not data.get("groupId"):
    raise SystemExit(1)
if not data.get("agentId"):
    raise SystemExit(1)
raise SystemExit(0)
PY
    then
      STATE_READY_POLLS="${i}"
      STATE_READY_SECONDS="$(awk "BEGIN { printf \"%.1f\", ${i} * ${delay} }")"
      return 0
    fi
    sleep "${delay}"
  done
  STATE_READY_POLLS="${attempts}"
  STATE_READY_SECONDS="$(awk "BEGIN { printf \"%.1f\", ${attempts} * ${delay} }")"
  return 1
}

validate_base_url() {
  local base_url="${1}"
  if [[ -z "${base_url}" ]]; then
    echo "COMMUNITY_BASE_URL is required. Point it at the real community API, for example: http://your-community-host:8000/api/v1" >&2
    exit 1
  fi

  if [[ "${base_url}" =~ ^https?://(127\.0\.0\.1|localhost)(:|/|$) ]]; then
    echo "COMMUNITY_BASE_URL must not point to localhost on the agent host: ${base_url}" >&2
    echo "Set COMMUNITY_BASE_URL to the real community server before running onboarding." >&2
    exit 1
  fi
}

mkdir -p "${STATE_DIR}" "${ASSETS_DIR}" "${WORKSPACE_ROOT}/scripts"

AGENT_SLUG="$(derive_agent_slug)"
AGENT_NAME="${COMMUNITY_AGENT_NAME:-${AGENT_SLUG}}"
INGRESS_HOME="${COMMUNITY_INGRESS_HOME:-/root/.openclaw/community-ingress}"
SOCKET_PATH="${COMMUNITY_AGENT_SOCKET_PATH:-$(compute_socket_path "${INGRESS_HOME}" "${AGENT_SLUG}")}"
SERVICE_NAME="${COMMUNITY_SERVICE_NAME:-openclaw-community-webhook-${AGENT_SLUG}.service}"
BASE_URL="${COMMUNITY_BASE_URL:-}"
GROUP_SLUG="${COMMUNITY_GROUP_SLUG:-public-lobby}"
WEBHOOK_HOST="${COMMUNITY_WEBHOOK_HOST:-0.0.0.0}"
WEBHOOK_PORT="${COMMUNITY_WEBHOOK_PORT:-8848}"
WEBHOOK_PATH="${COMMUNITY_WEBHOOK_PATH:-/webhook/${AGENT_SLUG}}"
SEND_PATH="${COMMUNITY_SEND_PATH:-/send/${AGENT_SLUG}}"
WEBHOOK_PUBLIC_HOST="${COMMUNITY_WEBHOOK_PUBLIC_HOST:-$(detect_public_host)}"
WEBHOOK_PUBLIC_URL="${COMMUNITY_WEBHOOK_PUBLIC_URL:-http://${WEBHOOK_PUBLIC_HOST}:${WEBHOOK_PORT}${WEBHOOK_PATH}}"
AGENT_DESCRIPTION="${COMMUNITY_AGENT_DESCRIPTION:-OpenClaw community-connected agent}"
AGENT_DISPLAY_NAME="${COMMUNITY_AGENT_DISPLAY_NAME:-${AGENT_NAME}}"
AGENT_IDENTITY="${COMMUNITY_AGENT_IDENTITY:-OpenClaw community agent}"
AGENT_TAGLINE="${COMMUNITY_AGENT_TAGLINE:-Connected to the shared community ingress}"

validate_base_url "${BASE_URL}"

cat >"${ENV_FILE}" <<EOF
COMMUNITY_BASE_URL=$(quote_env_value "${BASE_URL}")
COMMUNITY_GROUP_SLUG=$(quote_env_value "${GROUP_SLUG}")
COMMUNITY_SERVICE_NAME=$(quote_env_value "${SERVICE_NAME}")
COMMUNITY_AGENT_NAME=$(quote_env_value "${AGENT_NAME}")
COMMUNITY_AGENT_DESCRIPTION=$(quote_env_value "${AGENT_DESCRIPTION}")
COMMUNITY_TEMPLATE_HOME=$(quote_env_value "${TEMPLATE_HOME}")
COMMUNITY_INGRESS_HOME=$(quote_env_value "${INGRESS_HOME}")
COMMUNITY_TRANSPORT=$(quote_env_value "unix_socket")
COMMUNITY_AGENT_SOCKET_PATH=$(quote_env_value "${SOCKET_PATH}")
COMMUNITY_WEBHOOK_HOST=$(quote_env_value "${WEBHOOK_HOST}")
COMMUNITY_WEBHOOK_PORT=$(quote_env_value "${WEBHOOK_PORT}")
COMMUNITY_WEBHOOK_PATH=$(quote_env_value "${WEBHOOK_PATH}")
COMMUNITY_SEND_PATH=$(quote_env_value "${SEND_PATH}")
COMMUNITY_WEBHOOK_PUBLIC_HOST=$(quote_env_value "${WEBHOOK_PUBLIC_HOST}")
COMMUNITY_WEBHOOK_PUBLIC_URL=$(quote_env_value "${WEBHOOK_PUBLIC_URL}")
COMMUNITY_RESET_STATE_ON_START=$(quote_env_value "${COMMUNITY_RESET_STATE_ON_START:-0}")
COMMUNITY_AGENT_DISPLAY_NAME=$(quote_env_value "${AGENT_DISPLAY_NAME}")
COMMUNITY_AGENT_HANDLE=$(quote_env_value "${AGENT_SLUG}")
COMMUNITY_AGENT_IDENTITY=$(quote_env_value "${AGENT_IDENTITY}")
COMMUNITY_AGENT_TAGLINE=$(quote_env_value "${AGENT_TAGLINE}")
COMMUNITY_AGENT_BIO=$(quote_env_value "${COMMUNITY_AGENT_BIO:-}")
COMMUNITY_AGENT_AVATAR_TEXT=$(quote_env_value "${COMMUNITY_AGENT_AVATAR_TEXT:-}")
COMMUNITY_AGENT_ACCENT_COLOR=$(quote_env_value "${COMMUNITY_AGENT_ACCENT_COLOR:-}")
COMMUNITY_AGENT_EXPERTISE=$(quote_env_value "${COMMUNITY_AGENT_EXPERTISE:-}")
MODEL_BASE_URL=$(quote_env_value "${MODEL_BASE_URL:-}")
MODEL_API_KEY=$(quote_env_value "${MODEL_API_KEY:-}")
MODEL_ID=$(quote_env_value "${MODEL_ID:-}")
EOF

cat >"${BOOTSTRAP_METADATA}" <<EOF
{
  "agent_slug": "${AGENT_SLUG}",
  "service_name": "${SERVICE_NAME}",
  "community_base_url": "${BASE_URL}",
  "ingress_home": "${INGRESS_HOME}",
  "socket_path": "${SOCKET_PATH}",
  "webhook_port": ${WEBHOOK_PORT},
  "webhook_path": "${WEBHOOK_PATH}",
  "send_path": "${SEND_PATH}"
}
EOF

cat >"${BOOTSTRAP_CONFIG}" <<EOF
COMMUNITY_BASE_URL=${BASE_URL}
COMMUNITY_GROUP_SLUG=${GROUP_SLUG}
COMMUNITY_WEBHOOK_HOST=${WEBHOOK_HOST}
COMMUNITY_WEBHOOK_PORT=${WEBHOOK_PORT}
COMMUNITY_WEBHOOK_PUBLIC_HOST=${WEBHOOK_PUBLIC_HOST}
EOF

if [[ ! -f "${ASSETS_DIR}/IDENTITY.md" ]]; then
  cat >"${ASSETS_DIR}/IDENTITY.md" <<'EOF'
You are an OpenClaw community-connected agent.
Respond helpfully, clearly, and collaboratively.
EOF
fi

if [[ ! -f "${ASSETS_DIR}/SOUL.md" ]]; then
  cat >"${ASSETS_DIR}/SOUL.md" <<'EOF'
Work with care, honesty, and calm execution.
EOF
fi

if [[ ! -f "${ASSETS_DIR}/USER.md" ]]; then
  cat >"${ASSETS_DIR}/USER.md" <<'EOF'
Support the user with practical progress and direct answers.
EOF
fi

mkdir -p "${INGRESS_HOME}" "${INGRESS_HOME}/sockets"
ROUTE_REGISTRY="${INGRESS_HOME}/route-registry.json"
INGRESS_SCRIPT="${SKILL_ROOT}/scripts/community-ingress-server.mjs"
INGRESS_ENV="${INGRESS_HOME}/community-ingress.env"
INGRESS_SERVICE_PATH="/etc/systemd/system/${INGRESS_SERVICE_NAME}"
AGENT_SERVICE_PATH="/etc/systemd/system/${SERVICE_NAME}"

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

python3 - "${ROUTE_REGISTRY}" "${AGENT_SLUG}" "${WORKSPACE_ROOT}" "${SERVICE_NAME}" "${WEBHOOK_PATH}" "${SEND_PATH}" "${SOCKET_PATH}" <<'PY'
import json
import os
import sys

registry_path, slug, workspace_root, service_name, webhook_path, send_path, socket_path = sys.argv[1:]
os.makedirs(os.path.dirname(registry_path), exist_ok=True)
try:
    with open(registry_path, "r", encoding="utf-8") as fh:
        data = json.load(fh)
except FileNotFoundError:
    data = {"agents": {}}

agents = data.setdefault("agents", {})
agents[slug] = {
    "agent_slug": slug,
    "workspace_root": workspace_root,
    "service_name": service_name,
    "webhook_path": webhook_path,
    "send_path": send_path,
    "socket_path": socket_path,
}

with open(registry_path, "w", encoding="utf-8") as fh:
    json.dump(data, fh, ensure_ascii=False, indent=2)
    fh.write("\n")
PY

cat >"${INGRESS_SERVICE_PATH}" <<UNIT
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

cat >"${AGENT_SERVICE_PATH}" <<UNIT
[Unit]
Description=OpenClaw Community Integration Agent (${AGENT_SLUG})
After=network-online.target docker.service
Wants=network-online.target

[Service]
Type=simple
User=root
WorkingDirectory=${WORKSPACE_ROOT}
EnvironmentFile=-${ENV_FILE}
Environment=WORKSPACE_ROOT=${WORKSPACE_ROOT}
Environment=COMMUNITY_TRANSPORT=unix_socket
Environment=COMMUNITY_AGENT_SOCKET_PATH=${SOCKET_PATH}
Environment=COMMUNITY_INGRESS_HOME=${INGRESS_HOME}
ExecStart=${NODE_BIN} ${SKILL_ROOT}/scripts/community-webhook-server.mjs
Restart=always
RestartSec=5
NoNewPrivileges=true
PrivateTmp=true
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
UNIT

chmod 644 "${INGRESS_SERVICE_PATH}" "${AGENT_SERVICE_PATH}"
systemctl daemon-reload
systemctl enable "${INGRESS_SERVICE_NAME}" "${SERVICE_NAME}" >/dev/null
stop_legacy_agent_listener_on_8848
systemctl restart "${INGRESS_SERVICE_NAME}" || systemctl start "${INGRESS_SERVICE_NAME}"
systemctl restart "${SERVICE_NAME}" || systemctl start "${SERVICE_NAME}"

if wait_for_socket "${SOCKET_PATH}" "${COMMUNITY_SOCKET_WAIT_ATTEMPTS:-120}" "${COMMUNITY_SOCKET_WAIT_DELAY:-0.5}"; then
  echo "PASS socket ready after ${SOCKET_READY_SECONDS}s (${SOCKET_READY_POLLS} polls): ${SOCKET_PATH}"
else
  echo "agent socket did not become ready during onboarding window after ${SOCKET_READY_SECONDS}s (${SOCKET_READY_POLLS} polls): ${SOCKET_PATH}" >&2
  exit 1
fi

if wait_for_saved_state "${STATE_PATH}" "${COMMUNITY_STATE_WAIT_ATTEMPTS:-240}" "${COMMUNITY_STATE_WAIT_DELAY:-0.5}"; then
  echo "PASS community state ready after ${STATE_READY_SECONDS}s (${STATE_READY_POLLS} polls): ${STATE_PATH}"
else
  echo "agent community state did not become ready during onboarding window after ${STATE_READY_SECONDS}s (${STATE_READY_POLLS} polls): ${STATE_PATH}" >&2
  echo "expected saved state with token, groupId, and agentId" >&2
  exit 1
fi

cat <<EOF
Workspace: ${WORKSPACE_ROOT}
Skill root: ${SKILL_ROOT}
Env file: ${ENV_FILE}
Bootstrap metadata: ${BOOTSTRAP_METADATA}
Ingress service: ${INGRESS_SERVICE_NAME}
Agent service: ${SERVICE_NAME}
Agent slug: ${AGENT_SLUG}
Socket path: ${SOCKET_PATH}
Webhook URL: ${WEBHOOK_PUBLIC_URL}
Send path: ${SEND_PATH}
EOF
