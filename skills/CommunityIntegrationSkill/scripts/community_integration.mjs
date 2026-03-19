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

const WORKSPACE = process.env.WORKSPACE_ROOT || "/root/.openclaw/workspace";
const TEMPLATE_HOME =
  process.env.COMMUNITY_TEMPLATE_HOME || path.join(WORKSPACE, ".openclaw", "community-agent-template");
const BASE_URL = process.env.COMMUNITY_BASE_URL || "http://127.0.0.1:8000/api/v1";
const GROUP_SLUG = process.env.COMMUNITY_GROUP_SLUG || "public-lobby";
const AGENT_NAME = process.env.COMMUNITY_AGENT_NAME || `openclaw-agent-${os.hostname()}`;
const AGENT_SLUG = slugifyHandle(process.env.COMMUNITY_AGENT_HANDLE || AGENT_NAME);
const AGENT_DESCRIPTION = process.env.COMMUNITY_AGENT_DESCRIPTION || "OpenClaw community-enabled agent";
const TRANSPORT_MODE = process.env.COMMUNITY_TRANSPORT || "tcp";
const LISTEN_HOST = process.env.COMMUNITY_WEBHOOK_HOST || "0.0.0.0";
const LISTEN_PORT = Number(process.env.COMMUNITY_WEBHOOK_PORT || "8848");
const WEBHOOK_PATH = process.env.COMMUNITY_WEBHOOK_PATH || `/webhook/${AGENT_SLUG}`;
const SEND_PATH = process.env.COMMUNITY_SEND_PATH || `/send/${AGENT_SLUG}`;
const AGENT_SOCKET_PATH =
  process.env.COMMUNITY_AGENT_SOCKET_PATH || path.join(TEMPLATE_HOME, "run", `${AGENT_SLUG}.sock`);
const WEBHOOK_PUBLIC_HOST = process.env.COMMUNITY_WEBHOOK_PUBLIC_HOST || "127.0.0.1";
const WEBHOOK_PUBLIC_URL = process.env.COMMUNITY_WEBHOOK_PUBLIC_URL || "";
const RESET_STATE_ON_START = process.env.COMMUNITY_RESET_STATE_ON_START === "1";

const STATE_PATH = path.join(TEMPLATE_HOME, "state", "community-webhook-state.json");
const CHANNEL_CONTEXT_PATH = path.join(TEMPLATE_HOME, "state", "community-channel-contexts.json");
const WORKFLOW_CONTRACT_PATH = path.join(TEMPLATE_HOME, "state", "community-workflow-contracts.json");
const PROTOCOL_VIOLATION_PATH = path.join(TEMPLATE_HOME, "state", "community-protocol-violations.json");
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
  fs.writeFileSync(filePath, `${JSON.stringify(value, null, 2)}\n`);
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

function signalWithTimeout(ms = 30000) {
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
  const handle = slugifyHandle(firstNonEmpty(process.env.COMMUNITY_AGENT_HANDLE, displayName));
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

async function ensureRegisteredAgent(state) {
  if (state.token) {
    try {
      const me = await request("/agents/me", { method: "GET", token: state.token });
      return { ...state, agentId: me.id, agentName: me.name };
    } catch (error) {
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
  nextState = await ensureProfile(nextState);
  nextState = await ensureGroupMembership(nextState);
  nextState = await ensurePresence(nextState);
  nextState = await ensureAgentWebhook(nextState);
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

function runtimeInstructions(runtimeContext) {
  if (!runtimeContext || typeof runtimeContext !== "object") {
    return "当前未能从 Community 获取运行时上下文。你只能基于明确任务，给出简洁、公开、可回到社区的执行结果。";
  }
  return [
    "以下是 Community 侧返回的运行时上下文摘要。你只需要据此完成当前任务，不要复述协议。",
    JSON.stringify(runtimeContext, null, 2),
  ].join("\n\n");
}

function installedAgentProtocolText() {
  return loadText(INSTALLED_AGENT_PROTOCOL_PATH) || loadText(BUNDLED_AGENT_PROTOCOL_PATH);
}

function workflowContractInstructions(groupId) {
  const stored = storedPayloadForGroup(WORKFLOW_CONTRACT_PATH, groupId);
  const contract = stored?.contract || null;
  if (!contract) {
    return "";
  }
  return [
    "以下是当前执行阶段的临时 workflow contract。它只在本次任务执行上下文中生效，不是永久身份设定。",
    JSON.stringify(contract, null, 2),
  ].join("\n\n");
}

function channelContextInstructions(groupId) {
  const stored = storedPayloadForGroup(CHANNEL_CONTEXT_PATH, groupId);
  if (!stored) {
    return "";
  }
  return [
    "以下是当前频道的本地 channel context 缓存摘要。仅在当前执行中参考。",
    JSON.stringify(stored, null, 2),
  ].join("\n\n");
}

function buildExecutionPrompt(message, state, runtimeContext) {
  const identity = loadText(path.join(ASSETS_DIR, "IDENTITY.md"));
  const soul = loadText(path.join(ASSETS_DIR, "SOUL.md"));
  const user = loadText(path.join(ASSETS_DIR, "USER.md"));
  const agentProtocol = installedAgentProtocolText();

  return [
    {
      role: "system",
      content: [
        `你是 OpenClaw 社区协作 agent：${state.profile?.display_name || state.agentName}。`,
        "你当前是被 Agent Community 的 webhook 推送触发的，不是主动轮询。",
        "你只负责产出公开可回传到社区频道的执行结果，不要输出内部推理过程。",
        "不要虚构消息来源。当前消息来源就是 Agent Community webhook。",
        agentProtocol,
        runtimeInstructions(runtimeContext),
        channelContextInstructions(message?.group_id),
        workflowContractInstructions(message?.group_id),
        "以下是你的身份和工作上下文：",
        identity,
        soul,
        user,
      ]
        .filter(Boolean)
        .join("\n\n"),
    },
    {
      role: "user",
      content: `请根据下面这条社区消息，生成一条适合公开回到同一讨论串里的中文结果正文，只输出结果正文。\n\n消息类型: ${message.message_type}\n消息内容: ${JSON.stringify(message.content, null, 2)}`,
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

async function fetchRuntimeContext(groupId, state) {
  const [protocolData, channelData] = await Promise.all([
    request(`/groups/${groupId}/protocol`, { method: "GET", token: state.token }),
    request(`/groups/${groupId}/channel-context`, { method: "GET", token: state.token }),
  ]);
  await loadChannelContext(state, groupId, channelData);
  const protocol = protocolData?.protocol || protocolData || null;
  const channel = channelData?.channel_protocol || channelData?.channel || channelData || null;
  const channelConfig = channel?.channel || {};
  return {
    protocol_version: protocol?.version || protocol?.protocol_version || "unknown",
    group_slug: channelData?.group_slug || channelConfig?.group_slug || "",
    channel_summary: channel?.summary || protocol?.channel?.summary || "",
    channel_boundaries: channel?.boundaries || protocol?.channel?.boundaries || [],
    channel_roles: channel?.roles || protocol?.channel?.roles || [],
    applicable_rule_ids: protocolData?.applicable_rule_ids || [],
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
  return {
    group_id: payload?.group_id || incomingMessage?.group_id || state.groupId,
    thread_id: payload?.thread_id || incomingMessage?.thread_id || incomingMessage?.id || null,
    parent_message_id: payload?.parent_message_id || incomingMessage?.id || null,
    task_id: payload?.task_id || incomingMessage?.task_id || null,
    target_agent_id: payload?.target_agent_id || metadata.target_agent_id || incomingMessage?.agent_id || null,
    target_agent:
      payload?.target_agent ||
      metadata.target_agent ||
      incomingMessage?.agent_name ||
      incomingMessage?.source_agent_name ||
      null,
    assignees: Array.isArray(payload?.assignees)
      ? payload.assignees
      : Array.isArray(metadata.assignees)
        ? metadata.assignees
        : null,
  };
}

export function buildCommunityMessage(state, sendContext, payload) {
  const baseContent = payload?.content && typeof payload.content === "object" ? { ...payload.content } : {};
  const metadata = baseContent.metadata && typeof baseContent.metadata === "object" ? { ...baseContent.metadata } : {};
  const messageType = normalizeOutboundMessageType(payload?.message_type || "analysis");
  const text = String(baseContent.text || "");

  baseContent.metadata = metadata;
  const targetAgentId =
    sendContext?.target_agent_id && sendContext.target_agent_id !== state.agentId ? sendContext.target_agent_id : null;
  const targetAgent =
    firstNonEmpty(sendContext?.target_agent, metadata.target_agent, payload?.target_agent) || undefined;

  if (targetAgentId) {
    metadata.target_agent_id = metadata.target_agent_id || targetAgentId;
  }
  if (targetAgent) {
    metadata.target_agent = metadata.target_agent || targetAgent;
  }
  if (targetAgentId || targetAgent) {
    metadata.assignees =
      Array.isArray(metadata.assignees) && metadata.assignees.length
        ? metadata.assignees
        : Array.isArray(sendContext?.assignees) && sendContext.assignees.length
          ? sendContext.assignees
          : [targetAgentId || targetAgent];
  }

  const mention = structuredMentionForTarget(targetAgentId, targetAgent);
  if (mention) {
    baseContent.mentions = Array.isArray(baseContent.mentions) ? [...baseContent.mentions] : [];
    if (!baseContent.mentions.some((item) => item && item.mention_id === mention.mention_id)) {
      baseContent.mentions.push(mention);
    }
  }

  const intent = metadata.intent || inferIntentFromText(text);
  metadata.intent = intent;
  metadata.flow_type = metadata.flow_type || inferFlowType(messageType, intent);
  metadata.message_type = metadata.message_type || messageType;
  baseContent.intent = baseContent.intent || metadata.intent;
  baseContent.flow_type = baseContent.flow_type || metadata.flow_type;
  if (baseContent.mentions && metadata.mentions == null) {
    metadata.mentions = baseContent.mentions;
  }

  return { message_type: messageType, content: baseContent };
}

export function buildDirectedCollaborationMessage(state, sendContext, payload) {
  const normalizedPayload = {
    ...payload,
    message_type: normalizeOutboundMessageType(payload?.message_type || "analysis"),
    content: {
      ...(payload?.content || {}),
      metadata: {
        ...((payload?.content?.metadata && typeof payload.content.metadata === "object") ? payload.content.metadata : {}),
        intent: payload?.content?.metadata?.intent || "request_action",
        flow_type: payload?.content?.metadata?.flow_type || "task",
      },
    },
  };
  return buildCommunityMessage(state, sendContext, normalizedPayload);
}

export async function sendCommunityMessage(state, incomingMessage, payload) {
  const sendContext = buildSendContext(state, incomingMessage, payload);
  const structuredPayload = buildCommunityMessage(state, sendContext, payload);
  const requestBody = pruneNullish({
    group_id: sendContext.group_id,
    thread_id: sendContext.thread_id,
    parent_message_id: sendContext.parent_message_id,
    task_id: sendContext.task_id,
    message_type: structuredPayload.message_type,
    content: {
      ...(structuredPayload.content || {}),
      source: "CommunityIntegrationSkill",
      reply_to: sendContext.parent_message_id,
    },
  });
  console.log(JSON.stringify({ ok: true, outbound_structured_message: true, body: requestBody }, null, 2));
  return request("/messages", {
    method: "POST",
    token: state.token,
    body: JSON.stringify(requestBody),
  });
}

function parseActiveSendPayload(raw) {
  const payload = raw && typeof raw === "object" ? raw : {};
  const content = payload.content && typeof payload.content === "object" ? { ...payload.content } : {};
  return {
    group_id: payload.group_id || null,
    thread_id: payload.thread_id || null,
    parent_message_id: payload.parent_message_id || null,
    task_id: payload.task_id || null,
    target_agent_id: payload.target_agent_id || null,
    target_agent: payload.target_agent || null,
    assignees: Array.isArray(payload.assignees) ? payload.assignees : null,
    message_type: payload.message_type || "analysis",
    content,
  };
}

async function handleActiveSend(state, payload) {
  const normalized = parseActiveSendPayload(payload);
  if (!normalized.group_id) {
    throw new Error("community-send requires group_id");
  }
  if (!String(normalized.content?.text || "").trim()) {
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
  const runtimeModule = await loadRuntimeModule();
  return runtimeModule.handleRuntimeEvent(
    {
      fetchRuntimeContext,
      executeTask,
      postCommunityMessage: sendCommunityMessage,
      handleProtocolViolation,
      loadWorkflowContract,
      loadChannelContext,
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
  saveJson(STATE_PATH, state);
  return state;
}

export async function startCommunityIntegration() {
  const state = await bootstrapState();
  const server = http.createServer(async (req, res) => {
    if (req.method === "GET" && req.url === "/healthz") {
      res.writeHead(200, { "Content-Type": "application/json" });
      res.end(
        JSON.stringify({
          status: "ok",
          agent: state.agentName,
          agentId: state.agentId,
          webhookPath: WEBHOOK_PATH,
          listen: TRANSPORT_MODE === "unix_socket" ? AGENT_SOCKET_PATH : `${LISTEN_HOST}:${LISTEN_PORT}`,
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
      const rawBody = Buffer.concat(chunks);
      const signature = req.headers["x-community-webhook-signature"];
      if (typeof signature !== "string" || !verifySignature(state.webhookSecret, rawBody, signature)) {
        res.writeHead(401).end("invalid signature");
        return;
      }

      try {
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

  const onListening = () => {
    console.log(
      JSON.stringify(
        {
          ok: true,
          listening: true,
          agentName: state.agentName,
          groupSlug: state.groupSlug,
          webhookUrl: state.webhookUrl,
          webhookPath: WEBHOOK_PATH,
          sendPath: SEND_PATH,
          skill: "CommunityIntegrationSkill",
          mode: TRANSPORT_MODE === "unix_socket" ? "agent_socket" : "agent_webhook",
          socketPath: TRANSPORT_MODE === "unix_socket" ? AGENT_SOCKET_PATH : undefined,
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
