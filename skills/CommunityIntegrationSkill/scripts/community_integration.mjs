import crypto from "node:crypto";
import fs from "node:fs";
import http from "node:http";
import os from "node:os";
import path from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);
const SKILL_HOME = path.resolve(__dirname, "..");

function slugifyHandle(value) {
  const base = String(value || "")
    .trim()
    .toLowerCase()
    .replace(/[^a-z0-9_-]+/g, "-")
    .replace(/^-+|-+$/g, "");
  return base || `agent-${Date.now().toString().slice(-6)}`;
}

function shortSocketPath(ingressHome, agentSlug) {
  const normalizedSlug = slugifyHandle(agentSlug);
  const slugPrefix = normalizedSlug.slice(0, 24) || "agent";
  const hash = crypto.createHash("sha256").update(normalizedSlug).digest("hex").slice(0, 12);
  return path.join(ingressHome, "sockets", `${slugPrefix}-${hash}.sock`);
}

const WORKSPACE = process.env.WORKSPACE_ROOT || "/root/.openclaw/workspace";
const TEMPLATE_HOME =
  process.env.COMMUNITY_TEMPLATE_HOME || path.join(WORKSPACE, ".openclaw", "community-agent-template");
const INGRESS_HOME = process.env.COMMUNITY_INGRESS_HOME || "/root/.openclaw/community-ingress";
const BASE_URL = process.env.COMMUNITY_BASE_URL || "http://127.0.0.1:8000/api/v1";
const GROUP_SLUG = process.env.COMMUNITY_GROUP_SLUG || "public-lobby";
const AGENT_NAME = process.env.COMMUNITY_AGENT_NAME || `openclaw-agent-${os.hostname()}`;
const AGENT_SLUG = slugifyHandle(process.env.COMMUNITY_AGENT_HANDLE || AGENT_NAME);
const AGENT_DESCRIPTION = process.env.COMMUNITY_AGENT_DESCRIPTION || "OpenClaw community-enabled agent";
const TRANSPORT_MODE = process.env.COMMUNITY_TRANSPORT || "unix_socket";
const LISTEN_HOST = process.env.COMMUNITY_WEBHOOK_HOST || "0.0.0.0";
const LISTEN_PORT = Number(process.env.COMMUNITY_WEBHOOK_PORT || "8848");
const WEBHOOK_PATH = process.env.COMMUNITY_WEBHOOK_PATH || `/webhook/${AGENT_SLUG}`;
const SEND_PATH = process.env.COMMUNITY_SEND_PATH || `/send/${AGENT_SLUG}`;
const AGENT_SOCKET_PATH =
  process.env.COMMUNITY_AGENT_SOCKET_PATH || shortSocketPath(INGRESS_HOME, AGENT_SLUG);
const WEBHOOK_PUBLIC_HOST = process.env.COMMUNITY_WEBHOOK_PUBLIC_HOST || "127.0.0.1";
const WEBHOOK_PUBLIC_URL = process.env.COMMUNITY_WEBHOOK_PUBLIC_URL || "";
const RESET_STATE_ON_START = process.env.COMMUNITY_RESET_STATE_ON_START === "1";

const STATE_PATH = path.join(TEMPLATE_HOME, "state", "community-webhook-state.json");
const CHANNEL_CONTEXT_PATH = path.join(TEMPLATE_HOME, "state", "community-channel-contexts.json");
const WORKFLOW_CONTRACT_PATH = path.join(TEMPLATE_HOME, "state", "community-workflow-contracts.json");
const PROTOCOL_VIOLATION_PATH = path.join(TEMPLATE_HOME, "state", "community-protocol-violations.json");
const OUTBOUND_RECEIPTS_PATH = path.join(TEMPLATE_HOME, "state", "community-outbound-receipts.json");
const OUTBOUND_DEBUG_PATH = path.join(TEMPLATE_HOME, "state", "community-outbound-debug.json");
const OUTBOUND_GUARD_PATH = path.join(TEMPLATE_HOME, "state", "community-outbound-guard.json");
const INVALID_OUTBOUND_WINDOW_MS = Number(process.env.COMMUNITY_INVALID_OUTBOUND_WINDOW_MS || "60000");
const INVALID_OUTBOUND_THRESHOLD = Number(process.env.COMMUNITY_INVALID_OUTBOUND_THRESHOLD || "3");
const INVALID_OUTBOUND_PAUSE_MS = Number(process.env.COMMUNITY_INVALID_OUTBOUND_PAUSE_MS || "120000");
const ASSETS_DIR = path.join(TEMPLATE_HOME, "assets");
const BUNDLED_RUNTIME_PATH = path.join(SKILL_HOME, "assets", "community-runtime-v0.mjs");
const WORKSPACE_RUNTIME_PATH = path.join(WORKSPACE, "scripts", "community-runtime-v0.mjs");
const BUNDLED_AGENT_PROTOCOL_PATH = path.join(SKILL_HOME, "assets", "AGENT_PROTOCOL.md");
const INSTALLED_AGENT_PROTOCOL_PATH = path.join(ASSETS_DIR, "AGENT_PROTOCOL.md");
let runtimeModulePromise = null;

function ensureDir(filePath) {
  fs.mkdirSync(path.dirname(filePath), { recursive: true });
}

function loadJson(filePath, fallback = null) {
  try {
    return JSON.parse(fs.readFileSync(filePath, "utf8"));
  } catch {
    return fallback;
  }
}

function saveJson(filePath, value) {
  ensureDir(filePath);
  fs.writeFileSync(filePath, `${JSON.stringify(value, null, 2)}
`);
}

function appendJsonArray(filePath, entry, limit = 100) {
  const current = loadJson(filePath, []);
  const list = Array.isArray(current) ? current : [];
  list.push(entry);
  saveJson(filePath, list.slice(-limit));
  return entry;
}

function outboundRequestId() {
  if (typeof crypto.randomUUID === "function") {
    return crypto.randomUUID();
  }
  return crypto.randomBytes(16).toString("hex");
}

function loadOutboundGuard() {
  return (
    loadJson(OUTBOUND_GUARD_PATH, {
      invalid_attempts: [],
      paused_until: null,
      last_error: null,
      updated_at: null,
    }) || {}
  );
}

function saveOutboundGuard(state) {
  saveJson(OUTBOUND_GUARD_PATH, state || {});
  return state || {};
}

function assertOutboundSendAllowed() {
  const guard = loadOutboundGuard();
  const pausedUntil = String(guard?.paused_until || "").trim();
  if (pausedUntil) {
    const pausedAt = Date.parse(pausedUntil);
    if (Number.isFinite(pausedAt) && pausedAt > Date.now()) {
      throw new Error(`automatic outbound sending paused until ${pausedUntil}`);
    }
  }
}

function recordInvalidOutbound(reason, details = {}) {
  const now = Date.now();
  const cutoff = now - INVALID_OUTBOUND_WINDOW_MS;
  const guard = loadOutboundGuard();
  const attempts = Array.isArray(guard?.invalid_attempts)
    ? guard.invalid_attempts.filter((item) => Date.parse(item?.timestamp || "") >= cutoff)
    : [];
  const entry = {
    timestamp: new Date(now).toISOString(),
    reason,
    details,
  };
  attempts.push(entry);
  const next = {
    invalid_attempts: attempts,
    paused_until:
      attempts.length >= INVALID_OUTBOUND_THRESHOLD ? new Date(now + INVALID_OUTBOUND_PAUSE_MS).toISOString() : null,
    last_error: entry,
    updated_at: new Date(now).toISOString(),
  };
  saveOutboundGuard(next);
  console.error(
    JSON.stringify(
      { ok: false, outbound_guard: "invalid_outbound", reason, details, pausedUntil: next.paused_until },
      null,
      2,
    ),
  );
  return next;
}

function resetOutboundGuard() {
  const guard = loadOutboundGuard();
  saveOutboundGuard({
    invalid_attempts: Array.isArray(guard?.invalid_attempts) ? guard.invalid_attempts.slice(-10) : [],
    paused_until: null,
    last_error: null,
    updated_at: new Date().toISOString(),
  });
}

function isOutboundReceiptEventType(eventType) {
  return ["message.accepted", "message.rejected", "message.projected", "message.delivery_failed"].includes(
    String(eventType || "").trim(),
  );
}

function isOutboundDebugEventType(eventType) {
  return String(eventType || "").trim() === "outbound.canonicalized";
}

function receiptPayloadOf(event) {
  return event?.entity?.receipt || event?.event?.payload?.receipt || {};
}

function handleOutboundReceiptEvent(state, event) {
  const eventType = String(event?.event?.event_type || "").trim();
  const receipt = receiptPayloadOf(event);
  appendJsonArray(
    OUTBOUND_RECEIPTS_PATH,
    {
      received_at: new Date().toISOString(),
      event_type: eventType,
      receipt,
      group_id: event?.group_id || event?.event?.group_id || null,
      agent_id: state?.agentId || null,
    },
    200,
  );
  console.log(
    JSON.stringify(
      {
        ok: true,
        outbound_receipt: true,
        event_type: eventType,
        status: receipt?.status || null,
        clientRequestId: receipt?.client_request_id || null,
        communityMessageId: receipt?.community_message_id || null,
      },
      null,
      2,
    ),
  );
  return {
    ignored: false,
    handled: true,
    category: "outbound_receipt",
    non_intake: true,
    event_type: eventType,
    status: receipt?.status || null,
    client_request_id: receipt?.client_request_id || null,
    community_message_id: receipt?.community_message_id || null,
  };
}

function handleOutboundCanonicalizedEvent(state, event) {
  const receipt = receiptPayloadOf(event);
  const canonicalizedMessage = event?.entity?.canonicalized_message || event?.event?.payload?.canonicalized_message || null;
  appendJsonArray(
    OUTBOUND_DEBUG_PATH,
    {
      received_at: new Date().toISOString(),
      event_type: "outbound.canonicalized",
      receipt,
      canonicalized_message: canonicalizedMessage,
      group_id: event?.group_id || event?.event?.group_id || null,
      agent_id: state?.agentId || null,
    },
    100,
  );
  console.log(
    JSON.stringify(
      {
        ok: true,
        outbound_debug: true,
        event_type: "outbound.canonicalized",
        clientRequestId: receipt?.client_request_id || null,
        communityMessageId: receipt?.community_message_id || null,
      },
      null,
      2,
    ),
  );
  return {
    ignored: false,
    handled: true,
    category: "outbound_debug",
    non_intake: true,
    event_type: "outbound.canonicalized",
    client_request_id: receipt?.client_request_id || null,
    community_message_id: receipt?.community_message_id || null,
  };
}

function persistCommunityState(state, stage) {
  try {
    console.log(
      JSON.stringify(
        {
          ok: true,
          community_state: "writing",
          stage,
          statePath: STATE_PATH,
          hasToken: Boolean(state?.token),
          agentId: state?.agentId || null,
          groupId: state?.groupId || null,
        },
        null,
        2,
      ),
    );
    saveJson(STATE_PATH, state || {});
    console.log(
      JSON.stringify(
        {
          ok: true,
          community_state: "write_success",
          stage,
          statePath: STATE_PATH,
          hasToken: Boolean(state?.token),
          agentId: state?.agentId || null,
          groupId: state?.groupId || null,
        },
        null,
        2,
      ),
    );
  } catch (error) {
    console.error(
      JSON.stringify(
        {
          ok: false,
          community_state: "write_failure",
          stage,
          statePath: STATE_PATH,
          error: error.message,
        },
        null,
        2,
      ),
    );
    throw error;
  }
  return state || {};
}

function loadText(filePath) {
  try {
    return fs.readFileSync(filePath, "utf8").trim();
  } catch {
    return "";
  }
}

function randomSecret() {
  return crypto.randomBytes(24).toString("hex");
}

function deleteFileIfExists(filePath) {
  try {
    fs.unlinkSync(filePath);
  } catch {}
}

function signalWithTimeout(ms = Number(process.env.COMMUNITY_HTTP_TIMEOUT_MS || "90000")) {
  return AbortSignal.timeout(ms);
}

async function request(pathname, options = {}) {
  const headers = { "Content-Type": "application/json", ...(options.headers || {}) };
  if (options.token) {
    headers["X-Agent-Token"] = options.token;
  }
  const response = await fetch(`${BASE_URL}${pathname}`, {
    ...options,
    headers,
    signal: options.signal || signalWithTimeout(),
  });
  const text = await response.text();
  let payload;
  try {
    payload = JSON.parse(text);
  } catch {
    throw new Error(`Non-JSON response from ${pathname}: ${text}`);
  }
  if (!response.ok || payload.success === false) {
    throw new Error(`Request failed for ${pathname}: ${payload.message || response.status}`);
  }
  return payload.data;
}

function firstNonEmpty(...values) {
  for (const value of values) {
    const text = String(value || "").trim();
    if (text) {
      return text;
    }
  }
  return "";
}

function buildProfile() {
  const identityDoc = loadText(path.join(ASSETS_DIR, "IDENTITY.md"));
  const soulDoc = loadText(path.join(ASSETS_DIR, "SOUL.md"));
  const displayName = firstNonEmpty(process.env.COMMUNITY_AGENT_DISPLAY_NAME, AGENT_NAME);
  const requestedHandle = slugifyHandle(firstNonEmpty(process.env.COMMUNITY_AGENT_HANDLE, displayName));
  const handle = requestedHandle.slice(0, 40).replace(/-+$/g, "") || slugifyHandle(`agent-${Date.now()}`).slice(0, 40);
  const identity = firstNonEmpty(process.env.COMMUNITY_AGENT_IDENTITY, "OpenClaw 协作 Agent");
  const tagline = firstNonEmpty(process.env.COMMUNITY_AGENT_TAGLINE, AGENT_DESCRIPTION, "已接入社区协作总线");
  const bio = firstNonEmpty(
    process.env.COMMUNITY_AGENT_BIO,
    identityDoc.slice(0, 280),
    soulDoc.slice(0, 280),
    AGENT_DESCRIPTION,
  );
  const avatarText = firstNonEmpty(process.env.COMMUNITY_AGENT_AVATAR_TEXT, displayName.slice(0, 2).toUpperCase());
  const accentColor = firstNonEmpty(process.env.COMMUNITY_AGENT_ACCENT_COLOR, "");
  const expertise = String(process.env.COMMUNITY_AGENT_EXPERTISE || "")
    .split(",")
    .map((item) => item.trim())
    .filter(Boolean);

  return {
    display_name: displayName,
    handle,
    identity,
    tagline,
    bio,
    avatar_text: avatarText,
    accent_color: accentColor || undefined,
    expertise,
    home_group_slug: GROUP_SLUG,
  };
}

export function loadSavedCommunityState() {
  return loadJson(STATE_PATH, {}) || {};
}

export function saveCommunityState(state) {
  saveJson(STATE_PATH, state || {});
  return state || {};
}

function buildWebhookUrl() {
  if (WEBHOOK_PUBLIC_URL.trim()) {
    return WEBHOOK_PUBLIC_URL.trim();
  }
  return `http://${WEBHOOK_PUBLIC_HOST}:${LISTEN_PORT}${WEBHOOK_PATH}`;
}

function validateWebhookUrl(url) {
  if (!url) {
    throw new Error("webhook public url is empty");
  }
  if (url.includes("127.0.0.1") || url.includes("localhost")) {
    console.warn(
      JSON.stringify(
        {
          ok: false,
          warning: "webhook_public_url_is_loopback",
          webhookUrl: url,
          note: "Community server usually cannot deliver to loopback. Use a reachable host or domain.",
        },
        null,
        2,
      ),
    );
  }
}

export function installRuntime() {
  if (fs.existsSync(WORKSPACE_RUNTIME_PATH)) {
    runtimeModulePromise = null;
    return { installed: true, runtimePath: WORKSPACE_RUNTIME_PATH, source: "workspace" };
  }
  if (!fs.existsSync(BUNDLED_RUNTIME_PATH)) {
    throw new Error(`Bundled runtime asset missing: ${BUNDLED_RUNTIME_PATH}`);
  }
  ensureDir(WORKSPACE_RUNTIME_PATH);
  fs.copyFileSync(BUNDLED_RUNTIME_PATH, WORKSPACE_RUNTIME_PATH);
  runtimeModulePromise = null;
  return { installed: true, runtimePath: WORKSPACE_RUNTIME_PATH, source: "skill_asset" };
}

export function installAgentProtocol() {
  if (!fs.existsSync(BUNDLED_AGENT_PROTOCOL_PATH)) {
    throw new Error(`Bundled agent protocol asset missing: ${BUNDLED_AGENT_PROTOCOL_PATH}`);
  }
  ensureDir(INSTALLED_AGENT_PROTOCOL_PATH);
  fs.copyFileSync(BUNDLED_AGENT_PROTOCOL_PATH, INSTALLED_AGENT_PROTOCOL_PATH);
  return { installed: true, protocolPath: INSTALLED_AGENT_PROTOCOL_PATH, source: "skill_asset" };
}

function isAuthFailure(error) {
  const message = String(error?.message || "").toLowerCase();
  return (
    message.includes("invalid bearer token") ||
    message.includes("invalid_token") ||
    message.includes("invalid agent token") ||
    message.includes("stale agent token") ||
    message.includes("request failed for /agents/me: 401") ||
    message.includes("request failed for /agents/me: unauthorized")
  );
}

async function ensureRegisteredAgent(state) {
  if (state.token) {
    try {
      const me = await request("/agents/me", { method: "GET", token: state.token });
      return { ...state, agentId: me.id, agentName: me.name };
    } catch (error) {
      if (!isAuthFailure(error)) {
        throw error;
      }
      console.warn(
        JSON.stringify(
          { ok: false, warning: "stale_agent_token_detected", message: String(error.message) },
          null,
          2,
        ),
      );
    }
  }

  let requestedName = AGENT_NAME;
  for (let attempt = 0; attempt < 3; attempt += 1) {
    try {
      const registration = await request("/agents", {
        method: "POST",
        body: JSON.stringify({
          name: requestedName,
          description: AGENT_DESCRIPTION,
          metadata_json: {
            source: "community-integration-skill",
            workspace: WORKSPACE,
            bridge: "CommunityIntegrationSkill",
          },
          is_moderator: false,
        }),
      });
      console.log(
        JSON.stringify(
          {
            ok: true,
            token_received: true,
            agentId: registration.agent.id,
            agentName: registration.agent.name,
            statePath: STATE_PATH,
          },
          null,
          2,
        ),
      );
      return {
        ...state,
        token: registration.token,
        agentId: registration.agent.id,
        agentName: registration.agent.name,
      };
    } catch (error) {
      if (!String(error.message).includes("agent name already exists")) {
        throw error;
      }
      requestedName = `${AGENT_NAME}-${Date.now()}`;
    }
  }
  throw new Error("Unable to register agent after repeated name conflicts");
}

async function ensureProfile(state) {
  const profile = buildProfile();
  const updated = await request("/agents/me/profile", {
    method: "PATCH",
    token: state.token,
    body: JSON.stringify({ profile }),
  });
  return { ...state, profileCompleted: true, profile, agentId: updated.id, agentName: updated.name };
}

export async function updateCommunityProfile(state, profileOverrides = null) {
  const baseProfile = buildProfile();
  const profile =
    profileOverrides && typeof profileOverrides === "object"
      ? {
          ...baseProfile,
          ...profileOverrides,
        }
      : baseProfile;
  const updated = await request("/agents/me/profile", {
    method: "PATCH",
    token: state.token,
    body: JSON.stringify({ profile }),
  });
  return { ...state, profileCompleted: true, profile, agentId: updated.id, agentName: updated.name };
}

async function ensureGroupMembership(state) {
  const result = await request(`/groups/by-slug/${GROUP_SLUG}/join`, {
    method: "POST",
    token: state.token,
    body: JSON.stringify({ role: "member" }),
  });
  return { ...state, groupId: result.group.id, groupSlug: result.group.slug };
}

async function ensurePresence(state) {
  await request("/presence", {
    method: "POST",
    token: state.token,
    body: JSON.stringify({
      group_id: state.groupId,
      state: "online",
      note: "Community Integration Skill active",
    }),
  });
  return state;
}

async function ensureAgentWebhook(state) {
  const webhookSecret = state.webhookSecret || randomSecret();
  const webhookUrl = buildWebhookUrl();
  validateWebhookUrl(webhookUrl);
  await request("/agents/me/webhook", {
    method: "POST",
    token: state.token,
    body: JSON.stringify({
      target_url: webhookUrl,
      secret: webhookSecret,
      description: `CommunityIntegrationSkill webhook for ${AGENT_NAME}`,
    }),
  });
  return { ...state, webhookSecret, webhookUrl };
}

export async function connectToCommunity(state) {
  let nextState = { ...(state || {}) };
  nextState = await ensureRegisteredAgent(nextState);
  persistCommunityState(nextState, "registered");
  nextState = await ensureProfile(nextState);
  persistCommunityState(nextState, "profile_synced");
  nextState = await ensureGroupMembership(nextState);
  persistCommunityState(nextState, "group_joined");
  nextState = await ensurePresence(nextState);
  persistCommunityState(nextState, "presence_synced");
  nextState = await ensureAgentWebhook(nextState);
  persistCommunityState(nextState, "webhook_registered");
  return nextState;
}

function storeByGroup(filePath, groupId, payload) {
  const state = loadJson(filePath, {}) || {};
  state[groupId] = {
    updated_at: new Date().toISOString(),
    payload,
  };
  saveJson(filePath, state);
  return state[groupId];
}

function storedPayloadForGroup(filePath, groupId) {
  const state = loadJson(filePath, {}) || {};
  return state[String(groupId || "").trim()]?.payload || null;
}

export async function loadChannelContext(state, groupId, payload = null) {
  const effectiveGroupId = String(groupId || "").trim();
  if (!effectiveGroupId) {
    return null;
  }
  let data = payload;
  if (!data) {
    data = await request(`/groups/${effectiveGroupId}/channel-context`, { method: "GET", token: state.token });
  }
  return storeByGroup(CHANNEL_CONTEXT_PATH, effectiveGroupId, data);
}

export function loadWorkflowContract(groupId, contract, source = "event") {
  const effectiveGroupId = String(groupId || "").trim();
  if (!effectiveGroupId || !contract || typeof contract !== "object") {
    return null;
  }
  return storeByGroup(WORKFLOW_CONTRACT_PATH, effectiveGroupId, { source, contract });
}

export function handleProtocolViolation(state, event) {
  const payload = event?.entity?.message?.content?.metadata?.protocol_violation || event?.entity?.protocol_violation || null;
  if (!payload || typeof payload !== "object") {
    return { ignored: true, category: "protocol_violation", reason: "missing_payload" };
  }
  const history = loadJson(PROTOCOL_VIOLATION_PATH, []) || [];
  history.push({
    agent_id: state.agentId,
    received_at: new Date().toISOString(),
    payload,
  });
  saveJson(PROTOCOL_VIOLATION_PATH, history.slice(-50));
  return {
    ignored: false,
    handled: true,
    category: "protocol_violation",
    reason: payload.violation_type || "protocol_violation",
    requires_resend: payload.action_required === "resend_corrected_message",
  };
}

function loadModelConfig() {
  const baseUrl = String(process.env.MODEL_BASE_URL || "").trim();
  const apiKey = String(process.env.MODEL_API_KEY || "").trim();
  const modelId = String(process.env.MODEL_ID || "").trim();
  if (!baseUrl || !apiKey || !modelId) {
    throw new Error("MODEL_BASE_URL, MODEL_API_KEY, and MODEL_ID must be set in the template env file");
  }
  return { baseUrl: baseUrl.replace(/\/$/, ""), apiKey, modelId };
}

function normalizeRecord(value) {
  return value && typeof value === "object" && !Array.isArray(value) ? value : {};
}

function normalizeList(value) {
  return Array.isArray(value) ? value.filter((item) => item !== null && item !== undefined && item !== "") : [];
}

function compactText(value, maxLength = 180) {
  const text = String(value || "")
    .replace(/\s+/g, " ")
    .trim();
  if (!text) {
    return "";
  }
  return text.length > maxLength ? `${text.slice(0, maxLength - 1)}?` : text;
}

function compactList(value, maxItems = 8, itemMaxLength = 64) {
  return normalizeList(value)
    .slice(0, maxItems)
    .map((item) => compactText(item, itemMaxLength))
    .filter(Boolean);
}

function compactRecord(record) {
  return Object.fromEntries(
    Object.entries(normalizeRecord(record)).filter(([, value]) => {
      if (value === null || value === undefined || value === "") {
        return false;
      }
      if (Array.isArray(value)) {
        return value.length > 0;
      }
      if (value && typeof value === "object") {
        return Object.keys(value).length > 0;
      }
      return true;
    }),
  );
}

function unwrapSuccessPayload(payload) {
  const record = normalizeRecord(payload);
  if (Object.prototype.hasOwnProperty.call(record, "success") && Object.prototype.hasOwnProperty.call(record, "data")) {
    return record.data;
  }
  return record;
}

function normalizeProtocolPayload(payload) {
  const record = normalizeRecord(unwrapSuccessPayload(payload));
  return normalizeRecord(record.group_protocol || record.protocol || record);
}

function normalizeSessionPayload(payload) {
  const record = normalizeRecord(unwrapSuccessPayload(payload));
  return normalizeRecord(record.group_session || record.session || record);
}

function resolveWorkflowDefinition(protocol, workflowId) {
  const workflow = normalizeRecord(protocol?.workflow);
  const bootstrap = normalizeRecord(workflow.bootstrap_workflow);
  const formal = normalizeRecord(workflow.formal_workflow);
  const targetWorkflowId = String(workflowId || "").trim();
  if (targetWorkflowId && String(bootstrap.workflow_id || "").trim() === targetWorkflowId) {
    return bootstrap;
  }
  if (targetWorkflowId && String(formal.workflow_id || "").trim() === targetWorkflowId) {
    return formal;
  }
  return bootstrap.workflow_id ? bootstrap : formal;
}

function resolveExecutionSpec(protocol, workflowId) {
  const targetWorkflowId = String(workflowId || "").trim();
  const topLevel = normalizeRecord(protocol?.execution_spec);
  if (targetWorkflowId && String(topLevel.workflow_id || "").trim() === targetWorkflowId) {
    return topLevel;
  }
  const formalTemplate = normalizeRecord(protocol?.workflow?.formal_workflow?.execution_spec_template);
  if (targetWorkflowId && String(formalTemplate.workflow_id || "").trim() === targetWorkflowId) {
    return formalTemplate;
  }
  return topLevel.execution_spec_id ? topLevel : formalTemplate;
}

function resolveTaskBrief(protocol, workflowDefinition) {
  const workflowBrief = normalizeRecord(workflowDefinition?.bootstrap_task_brief || workflowDefinition?.task_brief);
  if (Object.keys(workflowBrief).length > 0) {
    return workflowBrief;
  }
  return compactRecord({
    task_goal: protocol?.group_identity?.group_objective,
    role_assignments: protocol?.members?.role_assignments,
  });
}

function resolveStageDefinition(workflowDefinition, currentStage) {
  const stages = normalizeRecord(workflowDefinition?.stages);
  return normalizeRecord(stages[currentStage]);
}

function agentAliases(state) {
  const aliases = new Set();
  const candidates = [state?.agentName, state?.profile?.handle, state?.profile?.display_name, state?.profile?.identity];
  for (const candidate of candidates) {
    const text = String(candidate || "").trim();
    if (!text) {
      continue;
    }
    aliases.add(text);
    aliases.add(text.toLowerCase());
    if (text.toLowerCase().startsWith("openclaw-")) {
      aliases.add(text.slice("openclaw-".length));
      aliases.add(text.slice("openclaw-".length).toLowerCase());
    }
  }
  return aliases;
}

function resolveCurrentRole(state, runtimeContext) {
  const agentId = String(state?.agentId || "").trim();
  const aliases = agentAliases(state);
  const roleDirectory = normalizeRecord(runtimeContext?.role_directory);
  const managerIds = normalizeList(roleDirectory.manager_agent_ids).map((item) => String(item).trim());
  const workerIds = normalizeList(roleDirectory.worker_agent_ids).map((item) => String(item).trim());
  const roleAssignments = normalizeRecord(runtimeContext?.role_assignments);
  if (managerIds.includes(agentId)) {
    return { group_role: "manager", assignment: "manager" };
  }
  if (workerIds.includes(agentId)) {
    for (const [assignment, detail] of Object.entries(roleAssignments)) {
      const configuredAgent = String(normalizeRecord(detail).agent_id || "").trim();
      if (!configuredAgent) {
        continue;
      }
      if (aliases.has(configuredAgent) || aliases.has(configuredAgent.toLowerCase())) {
        return { group_role: "worker", assignment };
      }
    }
    const stageWorkerAgent = String(runtimeContext?.stage_worker_agent_id || "").trim();
    if (stageWorkerAgent && (aliases.has(stageWorkerAgent) || aliases.has(stageWorkerAgent.toLowerCase()))) {
      return { group_role: "worker", assignment: stageWorkerAgent };
    }
    return { group_role: "worker", assignment: "worker" };
  }
  return { group_role: "observer", assignment: "unknown" };
}

function runtimeRuleCard(runtimeContext) {
  const transitionRules = normalizeRecord(runtimeContext?.transition_rules);
  const roleRules = normalizeRecord(runtimeContext?.role_rules);
  return compactRecord({
    manager_only_transition:
      transitionRules.manager_is_single_formal_transition_authority !== false &&
      roleRules.manager_holds_decision_authority_for_step_transition !== false,
    worker_evidence_only:
      transitionRules.worker_inputs_are_evidence_not_transition_gates !== false &&
      roleRules.workers_may_submit_evidence_but_must_not_directly_advance_workflow !== false,
    plain_text_no_progress:
      transitionRules.plain_text_cannot_replace_manager_formal_signal !== false &&
      runtimeContext?.plain_text_requires_formal_signal !== false,
    server_projection_authoritative: true,
  });
}

function taskBriefCard(runtimeContext) {
  const brief = normalizeRecord(runtimeContext?.task_brief);
  const deliveryDefinition = normalizeRecord(brief.delivery_definition || brief.delivery_contract);
  return compactRecord({
    task_goal: compactText(brief.task_goal, 160),
    time_scope: brief.time_scope,
    topic_scope: brief.topic_scope,
    target_count: brief.target_count,
    per_item_brief_length: brief.per_item_brief_length,
    per_item_requires_image: brief.per_item_requires_image,
    renderable_body_any_of: compactList(deliveryDefinition.final_body_required_any_of, 4, 32),
    per_item_required_fields: compactList(deliveryDefinition.per_item_required_fields, 6, 24),
    role_assignments: compactRecord(brief.role_assignments),
  });
}

function stateCard(runtimeContext) {
  return compactRecord({
    group_slug: runtimeContext?.group_slug,
    workflow_id: runtimeContext?.workflow_id,
    execution_spec_id: runtimeContext?.execution_spec_id,
    current_stage: runtimeContext?.current_stage,
    group_session_version: runtimeContext?.group_session_version,
  });
}

function roleCard(state, runtimeContext) {
  const currentRole = resolveCurrentRole(state, runtimeContext);
  const roleLabel =
    currentRole.group_role === "manager" ? "formal_controller" : currentRole.group_role === "worker" ? "evidence_submitter" : "observer";
  const forbidden =
    currentRole.group_role === "manager"
      ? ["do_not_use_plain_text_as_formal_close", "do_not_skip_server_projection"]
      : currentRole.group_role === "worker"
        ? ["do_not_directly_advance_stage", "do_not_emit_manager_signal"]
        : ["do_not_invent_authority"];
  return compactRecord({
    agent_id: state?.agentId,
    agent_name: state?.agentName || state?.profile?.display_name,
    group_role: currentRole.group_role,
    assignment: currentRole.assignment,
    output_mode: roleLabel,
    forbidden,
  });
}

function stageCard(state, runtimeContext) {
  const currentRole = resolveCurrentRole(state, runtimeContext);
  const stage = normalizeRecord(runtimeContext?.current_stage_spec);
  const workerHints = normalizeRecord(stage.worker_instruction_hint);
  const aliasList = Array.from(agentAliases(state));
  const matchingHint = workerHints[currentRole.assignment] || aliasList.map((alias) => workerHints[alias]).find(Boolean) || "";
  return compactRecord({
    stage_id: runtimeContext?.current_stage,
    goal: compactText(stage.goal, 180),
    owner: stage.owner,
    output_consumer: stage.output_consumer,
    current_job:
      currentRole.group_role === "manager"
        ? compactText(stage.manager_formal_output || stage.manager_formal_signal, 72)
        : compactText(matchingHint || stage.goal, 120),
    manager_formal_signal: stage.manager_formal_signal,
    worker_evidence_expected: compactList(stage.worker_evidence_expected, 4, 32),
  });
}

function schemaCard(state, runtimeContext) {
  const currentRole = resolveCurrentRole(state, runtimeContext);
  const stage = normalizeRecord(runtimeContext?.current_stage_spec);
  const statusFields = compactList(runtimeContext?.status_block_required_fields, 10, 24);
  if (currentRole.group_role === "manager") {
    const managerContract = normalizeRecord(stage.manager_formal_payload_contract);
    return compactRecord({
      status_block_required_fields: statusFields,
      lifecycle_phase: stage.manager_formal_signal === "task_start" ? "start" : "result",
      step_status: stage.manager_formal_signal,
      payload_kind: managerContract.kind,
      payload_required_fields: compactList(managerContract.required_fields, 12, 28),
    });
  }
  const workerContract = normalizeRecord(stage.worker_evidence_payload_contract);
  return compactRecord({
    status_block_required_fields: statusFields,
    lifecycle_phase: "run",
    step_status: compactList(stage.worker_evidence_expected, 1, 48)[0],
    payload_kind: workerContract.kind,
    payload_required_fields: compactList(workerContract.required_fields, 12, 28),
  });
}

function buildRuntimeCardsText(runtimeContext, state) {
  const cards = [
    ["???????", runtimeRuleCard(runtimeContext)],
    ["?????", taskBriefCard(runtimeContext)],
    ["????", stateCard(runtimeContext)],
    ["???", roleCard(state, runtimeContext)],
    ["?????", stageCard(state, runtimeContext)],
    ["?? schema ?", schemaCard(state, runtimeContext)],
  ]
    .map(([title, payload]) => {
      const compactPayload = compactRecord(payload);
      return Object.keys(compactPayload).length > 0 ? `?${title}?\n${JSON.stringify(compactPayload)}` : "";
    })
    .filter(Boolean);
  if (!cards.length) {
    return "????? Community ???????????????????????????????";
  }
  return ["????????????????????????????????????", ...cards].join("\n\n");
}

function runtimeInstructions(runtimeContext) {
  if (!runtimeContext || typeof runtimeContext !== "object") {
    return "????? Community ???????????????????????????????";
  }
  return buildRuntimeCardsText(runtimeContext, runtimeContext?.agent_state || {});
}

function installedAgentProtocolText() {
  return loadText(INSTALLED_AGENT_PROTOCOL_PATH) || loadText(BUNDLED_AGENT_PROTOCOL_PATH);
}

function workflowContractInstructions(groupId) {
  void groupId;
  return "";
}

function channelContextInstructions(groupId) {
  void groupId;
  return "";
}

export function buildExecutionPrompt(message, state, runtimeContext) {
  const identity = loadText(path.join(ASSETS_DIR, "IDENTITY.md"));
  const soul = loadText(path.join(ASSETS_DIR, "SOUL.md"));
  const user = loadText(path.join(ASSETS_DIR, "USER.md"));
  const agentProtocol = installedAgentProtocolText();
  const runtimeContextWithState = {
    ...normalizeRecord(runtimeContext),
    agent_state: {
      agentId: state?.agentId || "",
      agentName: state?.agentName || "",
      profile: normalizeRecord(state?.profile),
    },
  };

  return [
    {
      role: "system",
      content: [
        `?? OpenClaw ???? agent?${state.profile?.display_name || state.agentName}?`,
        "????? Agent Community ? webhook ?????????????",
        "?????????????????????????????????",
        "????????????????? Agent Community webhook?",
        agentProtocol,
        runtimeInstructions(runtimeContextWithState),
        "??????????????",
        identity,
        soul,
        user,
      ]
        .filter(Boolean)
        .join("\n\n"),
    },
    {
      role: "user",
      content: `????????????????????????????????????????????\n\n????: ${message.message_type}\n????: ${JSON.stringify(message.content, null, 2)}`,
    },
  ];
}

async function executeTask(message, state, runtimeContext) {
  const model = loadModelConfig();
  const response = await fetch(`${model.baseUrl}/chat/completions`, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      Authorization: `Bearer ${model.apiKey}`,
    },
    body: JSON.stringify({
      model: model.modelId,
      messages: buildExecutionPrompt(message, state, runtimeContext),
      temperature: 0.4,
    }),
    signal: signalWithTimeout(60000),
  });
  const payload = await response.json();
  if (!response.ok) {
    throw new Error(`Model request failed: ${JSON.stringify(payload)}`);
  }
  return payload.choices?.[0]?.message?.content?.trim() || "我已收到社区消息，正在跟进。";
}

export async function fetchRuntimeContext(groupId, state) {
  const [protocolData, sessionData] = await Promise.all([
    request(`/groups/${groupId}/protocol`, { method: "GET", token: state.token }),
    request(`/groups/${groupId}/session`, { method: "GET", token: state.token }),
  ]);
  const protocolEnvelope = normalizeRecord(unwrapSuccessPayload(protocolData));
  const sessionEnvelope = normalizeRecord(unwrapSuccessPayload(sessionData));
  const protocol = normalizeProtocolPayload(protocolData);
  const session = normalizeSessionPayload(sessionData);
  const workflowId = String(session.workflow_id || protocol?.execution_spec?.workflow_id || "").trim();
  const executionSpec = resolveExecutionSpec(protocol, workflowId);
  const workflowDefinition = resolveWorkflowDefinition(protocol, workflowId);
  const currentStage = String(session.current_stage || executionSpec.initial_stage || "").trim();
  const stageDefinition = resolveStageDefinition(workflowDefinition, currentStage);
  const taskBrief = resolveTaskBrief(protocol, workflowDefinition);
  return {
    protocol_version:
      session.protocol_version || protocolEnvelope.protocol_version || protocol?.protocol_meta?.protocol_version || protocol?.protocol_meta?.protocol_id || "unknown",
    group_slug:
      session.group_slug ||
      sessionEnvelope.group_slug ||
      protocolEnvelope.group_slug ||
      protocol?.group_identity?.group_slug ||
      state?.groupSlug ||
      "",
    workflow_id: workflowId,
    execution_spec_id: String(executionSpec.execution_spec_id || "").trim(),
    current_stage: currentStage,
    group_session_version: String(session.group_session_version || "").trim(),
    role_directory: normalizeRecord(executionSpec.role_directory),
    role_assignments: normalizeRecord(taskBrief.role_assignments || protocol?.members?.role_assignments),
    task_brief: compactRecord({
      task_goal: taskBrief.task_goal,
      time_scope: taskBrief.time_scope,
      topic_scope: taskBrief.topic_scope,
      target_count: taskBrief.target_count,
      per_item_brief_length: taskBrief.per_item_brief_length,
      per_item_requires_image: taskBrief.per_item_requires_image,
      delivery_definition: normalizeRecord(taskBrief.delivery_definition || taskBrief.delivery_contract),
      role_assignments: normalizeRecord(taskBrief.role_assignments || protocol?.members?.role_assignments),
    }),
    current_stage_spec: compactRecord({
      stage_id: currentStage,
      goal: stageDefinition.goal,
      owner: stageDefinition.owner,
      output_consumer: stageDefinition.output_consumer,
      worker_agent_id: stageDefinition.worker_agent_id,
      worker_instruction_hint: normalizeRecord(stageDefinition.worker_instruction_hint),
      worker_evidence_expected: normalizeList(stageDefinition.worker_evidence_expected),
      worker_evidence_payload_contract: normalizeRecord(stageDefinition.worker_evidence_payload_contract),
      manager_formal_output: stageDefinition.manager_formal_output,
      manager_formal_signal: stageDefinition.manager_formal_signal,
      manager_formal_payload_contract: normalizeRecord(stageDefinition.manager_formal_payload_contract),
    }),
    status_block_required_fields: normalizeList(protocol?.status_block_rules?.required_fields),
    transition_rules: normalizeRecord(protocol?.transition_rules),
    role_rules: normalizeRecord(protocol?.members?.role_rules),
    plain_text_requires_formal_signal: protocol?.communication_rules?.plain_text_is_allowed_for_collaboration_but_not_for_formal_transition !== false,
    applicable_rule_ids: compactList(protocolEnvelope.applicable_rule_ids, 8, 48),
    stage_worker_agent_id: stageDefinition.worker_agent_id || "",
  };
}

function inferIntentFromText(text) {
  const source = String(text || "");
  if (/请|开始|继续|执行|修复|确认|提交|补充|跟进/.test(source)) {
    return "request_action";
  }
  if (/验收|concluded|关闭|闭环|决定|批准|授权/.test(source)) {
    return "decide";
  }
  return "inform";
}

function inferFlowType(messageType, intent) {
  const loweredType = String(messageType || "").trim().toLowerCase();
  if (intent === "request_action") {
    return "task";
  }
  if (loweredType === "decision" || intent === "decide") {
    return "decision";
  }
  if (["summary", "progress"].includes(loweredType)) {
    return "status";
  }
  if (["question", "chat"].includes(loweredType)) {
    return "chat";
  }
  return "discussion";
}

function normalizeOutboundMessageType(messageType) {
  const loweredType = String(messageType || "").trim().toLowerCase();
  const allowed = new Set([
    "proposal",
    "analysis",
    "question",
    "claim",
    "progress",
    "handoff",
    "review",
    "decision",
    "summary",
    "meta",
  ]);
  if (allowed.has(loweredType)) {
    return loweredType;
  }
  if (loweredType === "chat") {
    return "analysis";
  }
  return "analysis";
}

function structuredMentionForTarget(targetAgentId, targetAgent) {
  if (!targetAgentId) {
    return null;
  }
  const displayText = `@${String(targetAgent || targetAgentId).trim()}`;
  return {
    mention_type: "agent",
    mention_id: targetAgentId,
    display_text: displayText,
  };
}

function responseModeLabel(mode) {
  return (
    {
      task: "??",
      status: "??",
      discussion: "??",
      decision: "??",
      chat: "??",
      unknown: "??",
      system: "????",
      protocol_violation: "????",
      workflow_contract: "????",
      channel_context: "?????",
    }[String(mode || "").trim()] || "??"
  );
}

function dictValue(value) {
  return value && typeof value === "object" && !Array.isArray(value) ? value : {};
}

function listValue(value) {
  return Array.isArray(value) ? value : [];
}

function canonicalMessageFromPayload(sendContext, payload, state) {
  const source = payload && typeof payload === "object" ? payload : {};
  const body = dictValue(source.body);
  const semantics = dictValue(source.semantics);
  const routing = dictValue(source.routing);
  const target = dictValue(routing.target);
  const extensions = dictValue(source.extensions);
  const custom = dictValue(extensions.custom);

  const legacyContent = dictValue(source.content);
  const legacyMetadata = dictValue(legacyContent.metadata);
  const legacyCustom = { ...legacyMetadata };
  delete legacyCustom.target_agent_id;
  delete legacyCustom.target_agent;
  delete legacyCustom.assignees;
  delete legacyCustom.assignment;
  delete legacyCustom.targets;
  delete legacyCustom.intent;
  delete legacyCustom.flow_type;
  delete legacyCustom.message_type;
  delete legacyCustom.client_request_id;
  delete legacyCustom.outbound_correlation_id;
  delete legacyCustom.idempotency_key;
  delete legacyCustom.source;
  delete legacyCustom.mentions;
  delete legacyCustom.task_id;
  delete legacyCustom.topic;
  delete legacyCustom.reply_to;
  const normalizedText = firstNonEmpty(body.text, legacyContent.text);
  const normalizedKind = normalizeOutboundMessageType(source.message_type || semantics.kind || "analysis");
  const normalizedIntent = firstNonEmpty(semantics.intent, legacyContent.intent, legacyMetadata.intent, inferIntentFromText(normalizedText));
  const outboundCorrelationId = firstNonEmpty(
    extensions.outbound_correlation_id,
    extensions.client_request_id,
    custom.idempotency_key,
    legacyMetadata.outbound_correlation_id,
    legacyMetadata.client_request_id,
    legacyMetadata.idempotency_key,
    outboundRequestId(),
  );

  const targetAgentId =
    firstNonEmpty(target.agent_id, source.target_agent_id, legacyMetadata.target_agent_id, sendContext?.target_agent_id) || null;
  const targetAgentLabel =
    firstNonEmpty(target.agent_label, source.target_agent, legacyMetadata.target_agent, sendContext?.target_agent) || null;
  const assignees = listValue(routing.assignees).length
    ? listValue(routing.assignees)
    : listValue(source.assignees).length
      ? listValue(source.assignees)
      : listValue(legacyMetadata.assignees).length
        ? listValue(legacyMetadata.assignees)
        : listValue(sendContext?.assignees).length
          ? listValue(sendContext.assignees)
          : targetAgentId || targetAgentLabel
            ? [targetAgentId || targetAgentLabel]
            : [];

  const mentions = listValue(routing.mentions).length ? [...listValue(routing.mentions)] : [...listValue(legacyContent.mentions)];
  const mention = structuredMentionForTarget(targetAgentId, targetAgentLabel);
  if (mention && !mentions.some((item) => item && item.mention_id === mention.mention_id)) {
    mentions.push(mention);
  }

  return pruneNullish({
    container: {
      group_id: sendContext.group_id,
    },
    author: {
      agent_id: state?.agentId || null,
    },
    relations: {
      thread_id: sendContext.thread_id,
      parent_message_id: sendContext.parent_message_id,
      task_id: sendContext.task_id,
    },
    body: {
      text: normalizedText,
      blocks: listValue(body.blocks),
      attachments: listValue(body.attachments),
    },
    semantics: {
      kind: normalizedKind,
      intent: normalizedIntent,
      topic: firstNonEmpty(semantics.topic, legacyMetadata.topic) || null,
    },
    routing: {
      target: {
        scope: targetAgentId || targetAgentLabel ? firstNonEmpty(target.scope, "agent") : null,
        agent_id: targetAgentId,
        agent_label: targetAgentLabel,
      },
      mentions,
      assignees,
    },
    extensions: {
      client_request_id: firstNonEmpty(extensions.client_request_id, legacyMetadata.client_request_id, outboundCorrelationId),
      outbound_correlation_id: outboundCorrelationId,
      source: firstNonEmpty(extensions.source, legacyContent.source, legacyMetadata.source, "CommunityIntegrationSkill"),
      custom: {
        ...legacyCustom,
        ...custom,
        ...(firstNonEmpty(legacyMetadata.reply_to, sendContext.parent_message_id)
          ? { reply_to: firstNonEmpty(legacyMetadata.reply_to, sendContext.parent_message_id) }
          : {}),
      },
    },
  });
}

function decideCommunityResponse(obligation, mode, decisionContext = {}) {
  const contextFlags = decisionContext?.contextFlags || {};

  if (mode === "task" && obligation !== "observe_only" && (contextFlags.targeted_self || contextFlags.assigned_self || contextFlags.authorize)) {
    return { action: "task_execution", reason: "directed_task" };
  }

  if (obligation === "required") {
    return { action: mode === "task" ? "task_execution" : contextFlags.question ? "full_reply" : "brief_reply", reason: "required_obligation" };
  }
  if (obligation === "required_ack") {
    return { action: contextFlags.question ? "brief_reply" : "ack", reason: "required_ack" };
  }
  if (obligation === "optional") {
    if (["discussion", "decision", "chat"].includes(mode)) {
      return { action: contextFlags.question ? "full_reply" : "brief_reply", reason: "optional_dialogue" };
    }
    if (["status", "unknown", "system"].includes(mode)) {
      return {
        action: contextFlags.addressed || contextFlags.question || contextFlags.need_ack ? "ack" : "observe_only",
        reason: "optional_signal",
      };
    }
    if (mode === "task") {
      return {
        action: contextFlags.addressed || contextFlags.assignment ? "brief_reply" : "observe_only",
        reason: "optional_task",
      };
    }
  }
  return { action: "observe_only", reason: "observe_only_default" };
}

function buildFallbackReplyText(message, state, runtimeContext, dispatchContext = {}) {
  const responseDecision = dispatchContext?.responseDecision || { action: "observe_only" };
  const contextFlags = dispatchContext?.contextFlags || {};
  const mode = dispatchContext?.mode || dispatchContext?.category || "unknown";
  const label = responseModeLabel(mode);
  const displayName = state?.profile?.display_name || state?.agentName || "OpenClaw Agent";

  if (responseDecision.action === "ack") {
    return `?????${label}???????????????????????? ${displayName} ??????`;
  }
  if (responseDecision.action === "brief_reply") {
    if (contextFlags.question) {
      return `?????${label}??????????????????????????????????????????`;
    }
    return `?????${label}??????????????????????????????`;
  }
  if (responseDecision.action === "full_reply") {
    return `?????${label}?????????????????????????????????????`;
  }
  return "";
}

async function generateCommunityReply(message, state, runtimeContext, dispatchContext = {}) {
  return buildFallbackReplyText(message, state, runtimeContext, dispatchContext);
}

function pruneNullish(value) {

  if (Array.isArray(value)) {
    return value
      .map((item) => pruneNullish(item))
      .filter((item) => item !== undefined);
  }
  if (value && typeof value === "object") {
    const entries = Object.entries(value)
      .map(([key, item]) => [key, pruneNullish(item)])
      .filter(([, item]) => item !== undefined);
    if (!entries.length) {
      return undefined;
    }
    return Object.fromEntries(entries);
  }
  if (value === null || value === undefined) {
    return undefined;
  }
  return value;
}

function buildSendContext(state, incomingMessage, payload) {
  const metadata = payload?.content?.metadata && typeof payload.content.metadata === "object" ? payload.content.metadata : {};
  const relations = dictValue(payload?.relations);
  const container = dictValue(payload?.container);
  const routing = dictValue(payload?.routing);
  const target = dictValue(routing.target);
  return {
    group_id: payload?.group_id || container.group_id || incomingMessage?.group_id || state.groupId,
    thread_id: payload?.thread_id || relations.thread_id || incomingMessage?.thread_id || incomingMessage?.id || null,
    parent_message_id: payload?.parent_message_id || relations.parent_message_id || incomingMessage?.id || null,
    task_id: payload?.task_id || relations.task_id || incomingMessage?.task_id || null,
    target_agent_id: payload?.target_agent_id || target.agent_id || metadata.target_agent_id || incomingMessage?.agent_id || null,
    target_agent:
      payload?.target_agent ||
      target.agent_label ||
      metadata.target_agent ||
      incomingMessage?.agent_name ||
      incomingMessage?.source_agent_name ||
      null,
    assignees: Array.isArray(payload?.assignees)
      ? payload.assignees
      : Array.isArray(routing.assignees)
        ? routing.assignees
        : Array.isArray(metadata.assignees)
          ? metadata.assignees
          : null,
  };
}

export function buildCommunityMessage(state, sendContext, payload) {
  return canonicalMessageFromPayload(sendContext, payload, state);
}

export function buildDirectedCollaborationMessage(state, sendContext, payload) {
  const normalizedPayload = {
    ...(payload && typeof payload === "object" ? payload : {}),
    semantics: {
      ...(dictValue(payload?.semantics)),
      kind: normalizeOutboundMessageType(payload?.message_type || payload?.semantics?.kind || "analysis"),
      intent: firstNonEmpty(payload?.semantics?.intent, payload?.content?.metadata?.intent, "request_action"),
    },
    routing: {
      ...(dictValue(payload?.routing)),
      target: {
        ...(dictValue(payload?.routing?.target)),
        scope:
          firstNonEmpty(payload?.routing?.target?.scope) ||
          (firstNonEmpty(payload?.target_agent_id, payload?.routing?.target?.agent_id) ? "agent" : null),
      },
    },
  };
  return buildCommunityMessage(state, sendContext, normalizedPayload);
}

export async function sendCommunityMessage(state, incomingMessage, payload) {
  assertOutboundSendAllowed();
  const sendContext = buildSendContext(state, incomingMessage, payload);
  const requestBody = buildCommunityMessage(state, sendContext, payload);
  const outboundText = String(requestBody?.body?.text || "").trim();
  if (!requestBody?.container?.group_id || !outboundText) {
    recordInvalidOutbound("invalid_outbound_payload", {
      group_id: requestBody?.container?.group_id || null,
      has_text: Boolean(outboundText),
      message_type: requestBody?.semantics?.kind || null,
      client_request_id: requestBody?.extensions?.client_request_id || null,
    });
    throw new Error("invalid outbound community message payload");
  }
  console.log(JSON.stringify({ ok: true, outbound_structured_message: true, body: requestBody }, null, 2));
  const result = await request("/messages", {
    method: "POST",
    token: state.token,
    body: JSON.stringify(requestBody),
  });
  resetOutboundGuard();
  return result;
}

function parseActiveSendPayload(raw) {
  const payload = raw && typeof raw === "object" ? raw : {};
  const content = payload.content && typeof payload.content === "object" ? { ...payload.content } : {};
  return {
    group_id: payload.group_id || payload.container?.group_id || null,
    thread_id: payload.thread_id || payload.relations?.thread_id || null,
    parent_message_id: payload.parent_message_id || payload.relations?.parent_message_id || null,
    task_id: payload.task_id || payload.relations?.task_id || null,
    target_agent_id: payload.target_agent_id || payload.routing?.target?.agent_id || null,
    target_agent: payload.target_agent || payload.routing?.target?.agent_label || null,
    assignees: Array.isArray(payload.assignees) ? payload.assignees : Array.isArray(payload.routing?.assignees) ? payload.routing.assignees : null,
    message_type: payload.message_type || payload.semantics?.kind || "analysis",
    semantics: dictValue(payload.semantics),
    routing: dictValue(payload.routing),
    extensions: dictValue(payload.extensions),
    body: dictValue(payload.body),
    content,
  };
}

async function handleActiveSend(state, payload) {
  const normalized = parseActiveSendPayload(payload);
  if (!normalized.group_id) {
    throw new Error("community-send requires group_id");
  }
  if (!String(normalized.body?.text || normalized.content?.text || "").trim()) {
    throw new Error("community-send requires content.text");
  }
  return sendCommunityMessage(state, null, normalized);
}

function verifySignature(secret, rawBody, signature) {
  const expected = crypto.createHmac("sha256", secret).update(rawBody).digest("hex");
  if (!signature || signature.length !== expected.length) {
    return false;
  }
  return crypto.timingSafeEqual(Buffer.from(expected), Buffer.from(signature || ""));
}

async function loadRuntimeModule() {
  if (!fs.existsSync(WORKSPACE_RUNTIME_PATH)) {
    installRuntime();
  }
  if (!runtimeModulePromise) {
    runtimeModulePromise = import(pathToFileURL(WORKSPACE_RUNTIME_PATH).href);
  }
  return runtimeModulePromise;
}

export async function receiveCommunityEvent(state, event) {
  const eventType = String(event?.event?.event_type || "").trim();
  if (isOutboundReceiptEventType(eventType)) {
    return handleOutboundReceiptEvent(state, event);
  }
  if (isOutboundDebugEventType(eventType)) {
    return handleOutboundCanonicalizedEvent(state, event);
  }

  const runtimeModule = await loadRuntimeModule();
  return runtimeModule.handleRuntimeEvent(
    {
      fetchRuntimeContext,
      executeTask,
      postCommunityMessage: sendCommunityMessage,
      handleProtocolViolation,
      loadWorkflowContract,
      loadChannelContext,
      decideResponse: decideCommunityResponse,
      generateReply: generateCommunityReply,
      buildFallbackReplyText,
    },
    state,
    event,
  );
}

async function bootstrapState() {
  if (RESET_STATE_ON_START) {
    deleteFileIfExists(STATE_PATH);
  }
  installRuntime();
  installAgentProtocol();
  let state = loadJson(STATE_PATH, {}) || {};
  state = await connectToCommunity(state);
  persistCommunityState(state, "bootstrap_completed");
  return state;
}

export async function startCommunityIntegration() {
  let currentState = null;
  let bootstrapReady = false;
  let bootstrapFailure = null;

  const statePromise = bootstrapState();
  statePromise.then(
    (state) => {
      currentState = state;
      bootstrapReady = true;
      console.log(
        JSON.stringify(
          {
            ok: true,
            bootstrap: "completed",
            agentName: state.agentName,
            agentId: state.agentId,
            socketPath: TRANSPORT_MODE === "unix_socket" ? AGENT_SOCKET_PATH : undefined,
          },
          null,
          2,
        ),
      );
    },
    (error) => {
      bootstrapFailure = error;
      console.error(
        JSON.stringify(
          {
            ok: false,
            phase: "bootstrap_state",
            error: error.message,
            transport: TRANSPORT_MODE,
            socketPath: TRANSPORT_MODE === "unix_socket" ? AGENT_SOCKET_PATH : undefined,
          },
          null,
          2,
        ),
      );
      process.exitCode = 1;
      setImmediate(() => process.exit(1));
    },
  );

  const server = http.createServer(async (req, res) => {
    if (req.method === "GET" && req.url === "/healthz") {
      res.writeHead(200, { "Content-Type": "application/json" });
      res.end(
        JSON.stringify({
          status: "ok",
          ready: bootstrapReady,
          agent: currentState?.agentName || AGENT_NAME,
          agentId: currentState?.agentId || null,
          webhookPath: WEBHOOK_PATH,
          listen: TRANSPORT_MODE === "unix_socket" ? AGENT_SOCKET_PATH : `${LISTEN_HOST}:${LISTEN_PORT}`,
          socketPath: TRANSPORT_MODE === "unix_socket" ? AGENT_SOCKET_PATH : undefined,
          bootstrapError: bootstrapFailure?.message || null,
          skill: "CommunityIntegrationSkill",
          runtimePath: WORKSPACE_RUNTIME_PATH,
          agentProtocolPath: INSTALLED_AGENT_PROTOCOL_PATH,
          timestamp: new Date().toISOString(),
        }),
      );
      return;
    }

    if (req.method === "POST" && req.url === SEND_PATH) {
      const chunks = [];
      req.on("data", (chunk) => chunks.push(chunk));
      req.on("end", async () => {
        try {
          const state = await statePromise;
          const payload = JSON.parse(Buffer.concat(chunks).toString("utf8"));
          const result = await handleActiveSend(state, payload);
          res.writeHead(202, { "Content-Type": "application/json" });
          res.end(JSON.stringify({ ok: true, result }));
        } catch (error) {
          console.error(JSON.stringify({ ok: false, active_send_error: error.message }, null, 2));
          res.writeHead(400, { "Content-Type": "application/json" });
          res.end(JSON.stringify({ ok: false, error: error.message }));
        }
      });
      return;
    }

    if (req.method !== "POST" || req.url !== WEBHOOK_PATH) {
      res.writeHead(404).end("not found");
      return;
    }

    const chunks = [];
    req.on("data", (chunk) => chunks.push(chunk));
    req.on("end", async () => {
      try {
        const state = await statePromise;
        const rawBody = Buffer.concat(chunks);
        const signature = req.headers["x-community-webhook-signature"];
        if (typeof signature !== "string" || !verifySignature(state.webhookSecret, rawBody, signature)) {
          res.writeHead(401).end("invalid signature");
          return;
        }

        const payload = JSON.parse(rawBody.toString("utf8"));
        const result = await receiveCommunityEvent(state, payload);
        console.log(JSON.stringify({ ok: true, webhook: true, event_type: payload?.event?.event_type || "", result }, null, 2));
        res.writeHead(202, { "Content-Type": "application/json" });
        res.end(JSON.stringify({ ok: true }));
      } catch (error) {
        console.error(JSON.stringify({ ok: false, error: error.message }, null, 2));
        res.writeHead(500, { "Content-Type": "application/json" });
        res.end(JSON.stringify({ ok: false, error: error.message }));
      }
    });
  });

  server.on("error", (error) => {
    console.error(
      JSON.stringify(
        {
          ok: false,
          listening: false,
          transport: TRANSPORT_MODE,
          socketPath: TRANSPORT_MODE === "unix_socket" ? AGENT_SOCKET_PATH : undefined,
          listen: TRANSPORT_MODE === "unix_socket" ? AGENT_SOCKET_PATH : `${LISTEN_HOST}:${LISTEN_PORT}`,
          error: error.message,
        },
        null,
        2,
      ),
    );
    process.exit(1);
  });

  const onListening = () => {
    console.log(
      JSON.stringify(
        {
          ok: true,
          listening: true,
          agentName: currentState?.agentName || AGENT_NAME,
          groupSlug: currentState?.groupSlug || GROUP_SLUG,
          webhookUrl: currentState?.webhookUrl || buildWebhookUrl(),
          webhookPath: WEBHOOK_PATH,
          sendPath: SEND_PATH,
          skill: "CommunityIntegrationSkill",
          mode: TRANSPORT_MODE === "unix_socket" ? "agent_socket" : "agent_webhook",
          socketPath: TRANSPORT_MODE === "unix_socket" ? AGENT_SOCKET_PATH : undefined,
          message: TRANSPORT_MODE === "unix_socket" ? `listening on socket_path=${AGENT_SOCKET_PATH}` : `listening on ${LISTEN_HOST}:${LISTEN_PORT}`,
        },
        null,
        2,
      ),
    );
  };

  if (TRANSPORT_MODE === "unix_socket") {
    ensureDir(AGENT_SOCKET_PATH);
    deleteFileIfExists(AGENT_SOCKET_PATH);
    server.listen(AGENT_SOCKET_PATH, onListening);
    process.on("exit", () => deleteFileIfExists(AGENT_SOCKET_PATH));
    process.on("SIGINT", () => {
      deleteFileIfExists(AGENT_SOCKET_PATH);
      process.exit(0);
    });
    process.on("SIGTERM", () => {
      deleteFileIfExists(AGENT_SOCKET_PATH);
      process.exit(0);
    });
    return;
  }

  server.listen(LISTEN_PORT, LISTEN_HOST, onListening);
}

