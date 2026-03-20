const MESSAGE_PROTOCOL_V2_ENABLED = ["1", "true", "yes", "on"].includes(String(process.env.MESSAGE_PROTOCOL_V2 || "").trim().toLowerCase());
const WEBHOOK_RECEIPT_V2_ENABLED = ["1", "true", "yes", "on"].includes(String(process.env.WEBHOOK_RECEIPT_V2 || "").trim().toLowerCase());

function lower(value) {
  return String(value || "").trim().toLowerCase();
}

function dictOf(value) {
  return value && typeof value === "object" && !Array.isArray(value) ? value : {};
}

function listOf(value) {
  return Array.isArray(value) ? value : [];
}

function firstText(...values) {
  for (const value of values) {
    const text = String(value || "").trim();
    if (text) {
      return text;
    }
  }
  return "";
}

function isCanonicalV2Message(message) {
  const item = dictOf(message);
  return Boolean(item.container || item.author || item.relations || item.body || item.semantics || item.routing || item.extensions);
}

export function normalizeMessageProtocol(message) {
  const source = dictOf(message);
  if (!MESSAGE_PROTOCOL_V2_ENABLED || !source || !Object.keys(source).length) {
    return source;
  }

  if (!isCanonicalV2Message(source)) {
    return source;
  }

  const container = dictOf(source.container);
  const author = dictOf(source.author);
  const relations = dictOf(source.relations);
  const body = dictOf(source.body);
  const semantics = dictOf(source.semantics);
  const routing = dictOf(source.routing);
  const target = dictOf(routing.target);
  const extensions = dictOf(source.extensions);
  const custom = dictOf(extensions.custom);
  const metadata = {
    ...custom,
  };

  if (firstText(target.agent_id)) {
    metadata.target_agent_id = firstText(target.agent_id);
  }
  if (firstText(target.agent_label)) {
    metadata.target_agent = firstText(target.agent_label);
  }
  if (listOf(routing.assignees).length) {
    metadata.assignees = listOf(routing.assignees);
  }
  if (firstText(semantics.intent)) {
    metadata.intent = firstText(semantics.intent);
  }
  if (firstText(extensions.client_request_id)) {
    metadata.client_request_id = firstText(extensions.client_request_id);
  }
  if (firstText(extensions.outbound_correlation_id)) {
    metadata.outbound_correlation_id = firstText(extensions.outbound_correlation_id);
  }
  if (firstText(extensions.source)) {
    metadata.source = firstText(extensions.source);
  }

  const normalized = {
    id: source.id || null,
    group_id: firstText(container.group_id) || null,
    agent_id: firstText(author.agent_id) || null,
    task_id: firstText(relations.task_id) || null,
    parent_message_id: firstText(relations.parent_message_id) || null,
    thread_id: firstText(relations.thread_id) || null,
    message_type: firstText(semantics.kind) || null,
    content: {
      text: firstText(body.text) || undefined,
      mentions: listOf(routing.mentions),
      metadata,
    },
  };
  if (firstText(semantics.intent)) {
    normalized.content.intent = firstText(semantics.intent);
  }
  return normalized;
}

export function normalizeWebhookEvent(event) {
  if (!MESSAGE_PROTOCOL_V2_ENABLED && !WEBHOOK_RECEIPT_V2_ENABLED) {
    return event;
  }
  const source = dictOf(event);
  const entity = dictOf(source.entity);
  const envelope = dictOf(source.event);
  const normalized = { ...source };
  if (entity.message) {
    normalized.entity = { ...entity, message: normalizeMessageProtocol(entity.message) };
  }
  if (dictOf(envelope.payload).message) {
    normalized.event = {
      ...envelope,
      payload: {
        ...dictOf(envelope.payload),
        message: normalizeMessageProtocol(dictOf(envelope.payload).message),
      },
    };
  }
  return normalized;
}

function textOf(message) {
  return String(message?.content?.text || "").trim();
}

function metadataOf(message) {
  const metadata = message?.content?.metadata;
  return metadata && typeof metadata === "object" ? metadata : {};
}

function flowTypeOf(message) {
  const metadata = metadataOf(message);
  return String(message?.content?.flow_type || metadata.flow_type || "").trim();
}

function intentOf(message) {
  const metadata = metadataOf(message);
  return String(message?.content?.intent || metadata.intent || "").trim();
}

function targetAgentOf(message) {
  const metadata = metadataOf(message);
  return String(metadata.target_agent || "").trim();
}

function targetAgentIdOf(message) {
  const metadata = metadataOf(message);
  return String(metadata.target_agent_id || "").trim();
}

function mentionsOf(message) {
  const raw = message?.content?.mentions;
  return Array.isArray(raw) ? raw : [];
}

function assigneesOf(message) {
  const metadata = metadataOf(message);
  const raw = metadata.assignees || metadata.assignment || metadata.targets;
  return Array.isArray(raw) ? raw : [];
}

function channelRolesOf(runtimeContext) {
  const roles = runtimeContext?.channel_roles;
  return Array.isArray(roles) ? roles : [];
}

function tokenizedAliases(value) {
  return String(value || "")
    .toLowerCase()
    .split(/[^a-z0-9_\u4e00-\u9fff]+/)
    .map((item) => item.trim())
    .filter((item) => item.length >= 2);
}

function selfNeedles(state) {
  return Array.from(
    new Set(
      [
        lower(state?.agentId),
        lower(state?.agentName),
        lower(state?.profile?.display_name),
        lower(state?.profile?.handle),
        ...tokenizedAliases(state?.agentName),
        ...tokenizedAliases(state?.profile?.display_name),
        ...tokenizedAliases(state?.profile?.handle),
      ].filter(Boolean),
    ),
  );
}

function selfRoleEntries(state, runtimeContext) {
  const needles = new Set(selfNeedles(state));
  return channelRolesOf(runtimeContext).filter((item) => {
    const agent = lower(item?.agent);
    return agent && needles.has(agent);
  });
}

function selfRoleNeedles(state, runtimeContext) {
  return selfRoleEntries(state, runtimeContext)
    .flatMap((item) => [lower(item?.agent), lower(item?.role)])
    .filter(Boolean);
}

function includesAny(haystack, needles) {
  const source = lower(haystack);
  return needles.some((needle) => source.includes(needle));
}

function executionSegments(text) {
  return lower(text)
    .split(/[。\n!！?？;；]/)
    .map((item) => item.trim())
    .filter(Boolean);
}

function hasExecutionIntent(text) {
  return /请|执行|处理|跟进|负责|收集|整理|汇总|分析|确认|完成|提交|开始/.test(lower(text));
}

function hasDirectedMention(text, state, runtimeContext) {
  const needles = [...selfNeedles(state), ...selfRoleNeedles(state, runtimeContext)].filter(Boolean);
  const source = lower(text);
  return needles.some((needle) => source.includes(`@${needle}`));
}

function hasRoleExecutionMatch(text, state, runtimeContext) {
  const roleNeedles = selfRoleNeedles(state, runtimeContext);
  if (!roleNeedles.length) {
    return false;
  }
  return executionSegments(text).some(
    (segment) => hasExecutionIntent(segment) && roleNeedles.some((needle) => segment.includes(needle)),
  );
}

function hasStructuredTaskSignal(message) {
  const metadata = metadataOf(message);
  return Boolean(
    message?.task_id ||
      metadata.task_id ||
      metadata.target_agent_id ||
      metadata.target_agent ||
      mentionsOf(message).length ||
      assigneesOf(message).length
  );
}

function structuredSignalsOf(message) {
  const type = lower(message?.message_type);
  const flowType = lower(flowTypeOf(message));
  const intent = lower(intentOf(message));
  const targetAgent = targetAgentOf(message);
  const targetAgentId = targetAgentIdOf(message);
  const mentions = mentionsOf(message);
  const assignees = assigneesOf(message);
  const metadata = metadataOf(message);
  return {
    messageType: type,
    flowType,
    intent,
    targetAgent,
    targetAgentId,
    mentions,
    assignees,
    taskId: message?.task_id || metadata.task_id || null,
    hasExplicitAssignment: Boolean(targetAgent || targetAgentId || mentions.length || assignees.length),
  };
}

function hasTaskKeywordSignal(message) {
  return /任务|执行|处理|跟进|收集|整理|汇总|完成|提交/.test(textOf(message));
}

function looksLikeTask(message) {
  const type = lower(message?.message_type);
  if (["proposal", "handoff", "claim"].includes(type)) {
    return true;
  }
  if (hasStructuredTaskSignal(message)) {
    return true;
  }
  return hasTaskKeywordSignal(message);
}

function looksLikeResponse(message) {
  const type = lower(message?.message_type);
  if (["question", "analysis"].includes(type)) {
    return true;
  }
  if (message?.parent_message_id) {
    return true;
  }
  const text = textOf(message);
  return /请回复|请确认|怎么看|能否|是否|\?|？/.test(text);
}

function extractGroupId(event, message) {
  return (
    message?.group_id ||
    event?.entity?.group_id ||
    event?.event?.payload?.group_id ||
    event?.entity?.message?.group_id ||
    null
  );
}

function extractPayload(event) {
  if (event?.entity?.message) {
    return event.entity.message;
  }
  return event?.event?.payload || event?.entity || null;
}

function canonicalMessageV2(message) {
  const source = dictOf(message);
  if (!source || !Object.keys(source).length) {
    return null;
  }

  if (isCanonicalV2Message(source)) {
    const container = dictOf(source.container);
    const author = dictOf(source.author);
    const relations = dictOf(source.relations);
    const body = dictOf(source.body);
    const semantics = dictOf(source.semantics);
    const routing = dictOf(source.routing);
    const target = dictOf(routing.target);
    const extensions = dictOf(source.extensions);
    return {
      id: source.id || null,
      container: { group_id: firstText(container.group_id) || null },
      author: { agent_id: firstText(author.agent_id) || null },
      relations: {
        thread_id: firstText(relations.thread_id) || null,
        parent_message_id: firstText(relations.parent_message_id) || null,
        task_id: firstText(relations.task_id) || null,
      },
      body: {
        text: firstText(body.text) || null,
        blocks: listOf(body.blocks),
        attachments: listOf(body.attachments),
      },
      semantics: {
        kind: firstText(semantics.kind) || null,
        intent: firstText(semantics.intent) || null,
        topic: firstText(semantics.topic) || null,
      },
      routing: {
        target: {
          scope: firstText(target.scope) || null,
          agent_id: firstText(target.agent_id) || null,
          agent_label: firstText(target.agent_label) || null,
        },
        mentions: listOf(routing.mentions),
        assignees: listOf(routing.assignees),
      },
      extensions: {
        client_request_id: firstText(extensions.client_request_id) || null,
        outbound_correlation_id: firstText(extensions.outbound_correlation_id) || null,
        source: firstText(extensions.source) || null,
        custom: dictOf(extensions.custom),
      },
    };
  }

  const content = dictOf(source.content);
  const metadata = dictOf(content.metadata);
  return {
    id: source.id || null,
    container: { group_id: firstText(source.group_id) || null },
    author: { agent_id: firstText(source.agent_id) || null },
    relations: {
      thread_id: firstText(source.thread_id) || null,
      parent_message_id: firstText(source.parent_message_id) || null,
      task_id: firstText(source.task_id, metadata.task_id) || null,
    },
    body: {
      text: firstText(content.text) || null,
      blocks: listOf(content.blocks),
      attachments: listOf(content.attachments),
    },
    semantics: {
      kind: firstText(source.message_type, metadata.message_type) || null,
      intent: firstText(content.intent, metadata.intent) || null,
      topic: firstText(metadata.topic) || null,
    },
    routing: {
      target: {
        scope: firstText(metadata.target_agent_id, source.target_agent_id) ? "agent" : null,
        agent_id: firstText(metadata.target_agent_id, source.target_agent_id) || null,
        agent_label: firstText(metadata.target_agent, source.target_agent) || null,
      },
      mentions: listOf(content.mentions || metadata.mentions),
      assignees: listOf(metadata.assignees || metadata.assignment || metadata.targets),
    },
    extensions: {
      client_request_id: firstText(metadata.client_request_id) || null,
      outbound_correlation_id: firstText(metadata.outbound_correlation_id, metadata.idempotency_key) || null,
      source: firstText(content.source, metadata.source) || null,
      custom: { ...metadata },
    },
  };
}

function buildEventSummary(event, eventType, groupId) {
  return {
    type: eventType || null,
    id: event?.event?.event_id || event?.event?.id || null,
    group_id: groupId || event?.event?.group_id || event?.group_id || null,
    aggregate_type: event?.event?.aggregate_type || null,
    aggregate_id: event?.event?.aggregate_id || null,
    actor_agent_id: event?.event?.actor_agent_id || null,
    created_at: event?.event?.created_at || null,
  };
}

function buildContextSummary(messageFacts, groupId) {
  return {
    group_id: groupId || messageFacts?.container?.group_id || null,
    thread_id: messageFacts?.relations?.thread_id || null,
    task_id: messageFacts?.relations?.task_id || null,
    parent_message_id: messageFacts?.relations?.parent_message_id || null,
    author_agent_id: messageFacts?.author?.agent_id || null,
  };
}

export function extractMessage(event) {
  const normalizedEvent = normalizeWebhookEvent(event);
  const eventType = String(normalizedEvent?.event?.event_type || "").trim();
  const sourceMessage = normalizedEvent?.entity?.message || normalizedEvent?.event?.payload?.message || null;
  const message = sourceMessage ? normalizeMessageProtocol(sourceMessage) : null;
  const messageFacts = sourceMessage ? canonicalMessageV2(sourceMessage) : null;
  const payload = extractPayload(normalizedEvent);
  const groupId = extractGroupId(normalizedEvent, message) || messageFacts?.container?.group_id || null;
  return {
    eventType,
    message,
    messageFacts,
    payload,
    groupId,
    normalizedEvent,
    eventSummary: buildEventSummary(normalizedEvent, eventType, groupId),
    context: buildContextSummary(messageFacts, groupId),
  };
}

export function classifyIncoming(eventType, message, payload, state = {}) {
  if (eventType === "protocol_violation") {
    return { category: "protocol_violation", reason: "protocol_violation_event" };
  }
  if (eventType === "workflow_contract") {
    return { category: "workflow_contract", reason: "workflow_contract_event" };
  }
  if (eventType === "channel_context") {
    return { category: "channel_context", reason: "channel_context_event" };
  }
  if (!message) {
    return { category: "unknown", reason: "missing_message" };
  }
  if (lower(message.agent_id) && lower(message.agent_id) === lower(state.agentId)) {
    return { category: "self_message", reason: "self_echo" };
  }
  if (eventType && eventType !== "message.posted") {
    return { category: "system_event", reason: eventType };
  }

  const signals = structuredSignalsOf(message);
  const type = signals.messageType;
  const metadata = metadataOf(message);

  if (type === "meta" || metadata.system_event) {
    return { category: "admin_message", reason: "admin_signal" };
  }
  if (signals.flowType === "status" || ["progress", "claim", "summary", "review"].includes(type)) {
    return { category: "status", reason: "status_signal" };
  }
  if (signals.flowType === "task") {
    return { category: "task", reason: "task_flow" };
  }
  if (["assign", "handoff", "request_action", "followup", "authorize"].includes(signals.intent)) {
    return { category: "task", reason: "task_intent" };
  }
  if (signals.flowType === "decision" || type === "decision") {
    return { category: "decision", reason: "decision_signal" };
  }
  if (type === "chat") {
    return { category: "chat", reason: "chat_kind" };
  }
  if (signals.flowType === "discussion" || ["question", "analysis"].includes(type)) {
    return { category: "discussion", reason: "discussion_signal" };
  }
  if (signals.hasExplicitAssignment || looksLikeTask(message)) {
    return { category: "task", reason: "task_shape" };
  }
  if (looksLikeResponse(message)) {
    return { category: "discussion", reason: "discussion_shape" };
  }
  return { category: "unknown", reason: "unclassified_message" };
}

export function isIgnorableMessage(input) {
  if (!input.eventType && !input.message && !input.payload) {
    return { ignorable: true, reason: "malformed_input" };
  }
  if (input.eventType === "message.posted" && !input.message) {
    return { ignorable: true, reason: "missing_message_object" };
  }
  return { ignorable: false, reason: null };
}

export function checkRelevance(input, state, runtimeContext) {
  if (input.category === "self_message") {
    return { relevant: false, reason: "self_echo" };
  }
  if (["protocol_violation", "workflow_contract", "channel_context", "admin_message", "system_event"].includes(input.category)) {
    return { relevant: true, reason: "system_scope" };
  }

  const message = input.message;
  if (!message) {
    return { relevant: false, reason: "missing_message" };
  }

  const signals = structuredSignalsOf(message);
  const text = textOf(message);
  const self = selfNeedles(state);
  const selfRoles = selfRoleNeedles(state, runtimeContext);

  if (lower(signals.targetAgentId) === lower(state.agentId)) {
    return { relevant: true, reason: "target_agent_id" };
  }
  if (includesAny(signals.targetAgent, self) || includesAny(signals.targetAgent, selfRoles)) {
    return { relevant: true, reason: "target_agent" };
  }
  if (signals.assignees.some((item) => includesAny(typeof item === "string" ? item : JSON.stringify(item), self))) {
    return { relevant: true, reason: "assignee" };
  }
  if (signals.assignees.some((item) => includesAny(typeof item === "string" ? item : JSON.stringify(item), selfRoles))) {
    return { relevant: true, reason: "assignee_role" };
  }
  if (signals.mentions.some((item) => includesAny(typeof item === "string" ? item : JSON.stringify(item), self))) {
    return { relevant: true, reason: "mention" };
  }
  if (signals.mentions.some((item) => includesAny(typeof item === "string" ? item : JSON.stringify(item), selfRoles))) {
    return { relevant: true, reason: "mention_role" };
  }
  if (includesAny(text, self) || hasDirectedMention(text, state, runtimeContext)) {
    return { relevant: true, reason: "text_addressing" };
  }
  if (hasRoleExecutionMatch(text, state, runtimeContext)) {
    return { relevant: true, reason: "role_execution_match" };
  }

  return { relevant: false, reason: "not_targeted" };
}

export function decideMode(input, relevance) {
  if (input.category === "self_message") {
    return { mode: "self_message" };
  }
  if (["protocol_violation", "workflow_contract", "channel_context", "admin_message", "system_event"].includes(input.category)) {
    return { mode: input.category };
  }
  if (input.category === "decision") {
    const signals = structuredSignalsOf(input.message);
    if (signals.hasExplicitAssignment || ["approve", "authorize"].includes(signals.intent)) {
      return { mode: "task" };
    }
  }
  if (!relevance.relevant && input.category === "unknown") {
    return { mode: "observe" };
  }
  return { mode: input.category || "unknown" };
}

function hasQuestionSignal(message) {
  if (!message) {
    return false;
  }
  const signals = structuredSignalsOf(message);
  return signals.messageType === "question" || signals.intent === "question" || /[??]/.test(textOf(message));
}

function hasNeedAckSignal(message) {
  if (!message) {
    return false;
  }
  const metadata = metadataOf(message);
  const signals = structuredSignalsOf(message);
  return Boolean(
    metadata.need_ack ||
      metadata.required_ack ||
      metadata.ack_required ||
      metadata.require_ack ||
      ["claim", "review", "progress", "summary"].includes(signals.messageType),
  );
}

export function buildContextFlags(input, state, runtimeContext, relevance) {
  const message = input.message;
  const signals = message
    ? structuredSignalsOf(message)
    : {
        messageType: "",
        flowType: "",
        intent: "",
        targetAgent: "",
        targetAgentId: "",
        mentions: [],
        assignees: [],
        taskId: null,
        hasExplicitAssignment: false,
      };
  const self = selfNeedles(state);
  const selfRoles = selfRoleNeedles(state, runtimeContext);
  const text = textOf(message);
  const targetMatchesSelf =
    lower(signals.targetAgentId) === lower(state.agentId) ||
    includesAny(signals.targetAgent, self) ||
    includesAny(signals.targetAgent, selfRoles);
  const assigneeMatchesSelf =
    signals.assignees.some((item) => includesAny(typeof item === "string" ? item : JSON.stringify(item), self)) ||
    signals.assignees.some((item) => includesAny(typeof item === "string" ? item : JSON.stringify(item), selfRoles));
  const mentionMatchesSelf =
    signals.mentions.some((item) => includesAny(typeof item === "string" ? item : JSON.stringify(item), self)) ||
    signals.mentions.some((item) => includesAny(typeof item === "string" ? item : JSON.stringify(item), selfRoles)) ||
    hasDirectedMention(text, state, runtimeContext);
  const question = hasQuestionSignal(message);
  const needAck = hasNeedAckSignal(message);
  const authorize = ["authorize", "approve"].includes(signals.intent);

  return {
    mention: mentionMatchesSelf,
    target: Boolean(signals.targetAgent || signals.targetAgentId),
    targeted_self: targetMatchesSelf,
    assignment: Boolean(signals.hasExplicitAssignment || signals.taskId),
    assigned_self: assigneeMatchesSelf,
    question,
    need_ack: needAck,
    authorize,
    task_signal: looksLikeTask(message),
    addressed: relevance.relevant || targetMatchesSelf || assigneeMatchesSelf || mentionMatchesSelf,
  };
}

export function decideObligation(input, relevance, contextFlags) {
  if (input.category === "self_message") {
    return { obligation: "observe_only", reason: "state_sync_only" };
  }
  if (["protocol_violation", "workflow_contract", "channel_context"].includes(input.category)) {
    return { obligation: "required", reason: `${input.category}_required` };
  }
  if (["admin_message", "system_event"].includes(input.category)) {
    if (contextFlags.need_ack || contextFlags.question || contextFlags.addressed) {
      return { obligation: "required_ack", reason: "system_ack_requested" };
    }
    return { obligation: "observe_only", reason: "system_notice" };
  }

  if (input.category === "task") {
    if (contextFlags.targeted_self || contextFlags.assigned_self || contextFlags.authorize) {
      return { obligation: "required", reason: "task_directed_to_self" };
    }
    if (relevance.relevant || contextFlags.assignment || contextFlags.task_signal) {
      return { obligation: "optional", reason: "task_visible_but_not_required" };
    }
    return { obligation: "observe_only", reason: "task_not_targeted" };
  }

  if (["discussion", "decision", "chat"].includes(input.category)) {
    if (contextFlags.need_ack && (contextFlags.addressed || contextFlags.question)) {
      return { obligation: "required_ack", reason: "conversation_requires_ack" };
    }
    return {
      obligation: "optional",
      reason: relevance.relevant || contextFlags.question ? "conversation_relevant" : "conversation_optional",
    };
  }

  if (["status", "unknown"].includes(input.category)) {
    if (contextFlags.need_ack) {
      return { obligation: "required_ack", reason: `${input.category}_ack_requested` };
    }
    if (contextFlags.addressed || contextFlags.question) {
      return { obligation: "optional", reason: `${input.category}_addressed` };
    }
    return { obligation: "observe_only", reason: `${input.category}_default_observe` };
  }

  return {
    obligation: relevance.relevant ? "optional" : "observe_only",
    reason: relevance.relevant ? "relevant_default" : "observe_only_default",
  };
}

export function defaultResponseDecision(mode, obligationDecision, contextFlags) {
  const obligation = obligationDecision?.obligation || "observe_only";

  if (mode === "self_message") {
    return { action: "observe_only", reason: "self_message_no_reply" };
  }

  if (mode === "task" && obligation !== "observe_only" && (contextFlags.targeted_self || contextFlags.assigned_self || contextFlags.authorize)) {
    return { action: "task_execution", reason: "direct_task_execution" };
  }

  if (obligation === "required") {
    return { action: mode === "task" ? "task_execution" : contextFlags.question ? "full_reply" : "brief_reply", reason: "required_obligation" };
  }
  if (obligation === "required_ack") {
    return { action: contextFlags.question ? "brief_reply" : "ack", reason: "required_ack_obligation" };
  }
  if (obligation === "optional") {
    if (["discussion", "decision", "chat"].includes(mode)) {
      return { action: contextFlags.question ? "full_reply" : "brief_reply", reason: "optional_conversation" };
    }
    if (["status", "unknown", "admin_message", "system_event"].includes(mode)) {
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

function normalizeResponseDecision(decision, mode, obligationDecision, contextFlags) {
  if (!decision) {
    return defaultResponseDecision(mode, obligationDecision, contextFlags);
  }
  if (typeof decision === "string") {
    return { action: decision, reason: "adapter_string_decision" };
  }
  if (typeof decision === "object" && decision.action) {
    return decision;
  }
  return defaultResponseDecision(mode, obligationDecision, contextFlags);
}

function defaultFallbackReplyText(input, modeDecision, obligationDecision, contextFlags, responseDecision) {
  const modeLabel = {
    task: "??",
    status: "??",
    discussion: "??",
    decision: "??",
    chat: "??",
    unknown: "??",
    admin_message: "????",
    system_event: "????",
    self_message: "????",
  }[modeDecision.mode] || "??";

  if (responseDecision.action === "ack") {
    return `?????${modeLabel}????????????????????????`;
  }
  if (responseDecision.action === "brief_reply") {
    if (contextFlags.question) {
      return `?????${modeLabel}???????????????????????????`;
    }
    return `?????${modeLabel}???????????????`;
  }
  if (responseDecision.action === "full_reply") {
    return `?????${modeLabel}?????????????????`;
  }
  if (responseDecision.action === "task_execution") {
    return `?????${modeLabel}??????????????`;
  }
  return "";
}

function buildRuntimeActions(category, responseDecision) {
  if (category === "self_message") {
    return {
      decision: "observe_only",
      should_reply: false,
      should_execute: false,
      should_sync_state: true,
      reason: "self_message_no_intake",
    };
  }
  const action = responseDecision?.action || "observe_only";
  return {
    decision: action,
    should_reply: ["ack", "brief_reply", "full_reply"].includes(action),
    should_execute: action === "task_execution",
    should_sync_state: false,
    reason: responseDecision?.reason || null,
  };
}

function buildWebhookResultV2(input, runtimeContext, relevance, obligationDecision, modeDecision, responseDecision, outcome = {}) {
  return {
    event: input.eventSummary,
    context: {
      ...input.context,
      runtime_context_loaded: Boolean(runtimeContext && Object.keys(runtimeContext).length),
      flags: outcome.contextFlags || {},
    },
    message: input.messageFacts || null,
    runtime: {
      category: input.category,
      mode: modeDecision.mode,
      reason: input.reason,
      relevance: {
        value: relevance.relevant,
        reason: relevance.reason,
      },
      obligation: {
        value: obligationDecision.obligation,
        reason: obligationDecision.reason,
      },
      actions: buildRuntimeActions(input.category, responseDecision),
      handled: Boolean(outcome.handled),
      executed: Boolean(outcome.executed),
      observed: Boolean(outcome.observed),
      ignored: Boolean(outcome.ignored),
      reply_id: outcome.replyId || null,
      result: outcome.result || null,
      payload: outcome.payload || null,
      required_unfulfilled: Boolean(outcome.required_unfulfilled),
    },
    ignored: Boolean(outcome.ignored),
    handled: Boolean(outcome.handled),
    executed: Boolean(outcome.executed),
    observed: Boolean(outcome.observed),
    replyId: outcome.replyId || null,
  };
}

async function resolveResponseDecision(adapter, modeDecision, input, runtimeContext, relevance, obligationDecision, contextFlags) {
  if (typeof adapter.decideResponse === "function") {
    const decision = await adapter.decideResponse(obligationDecision.obligation, modeDecision.mode, {
      category: input.category,
      reason: input.reason,
      relevance,
      obligation: obligationDecision,
      contextFlags,
      runtimeContext,
      message: input.message,
      payload: input.payload,
      eventType: input.eventType,
    });
    return normalizeResponseDecision(decision, modeDecision.mode, obligationDecision, contextFlags);
  }
  return defaultResponseDecision(modeDecision.mode, obligationDecision, contextFlags);
}

async function fallbackDispatch(adapter, state, input, runtimeContext, modeDecision, relevance, obligationDecision, contextFlags) {
  const responseDecision = await resolveResponseDecision(
    adapter,
    modeDecision,
    input,
    runtimeContext,
    relevance,
    obligationDecision,
    contextFlags,
  );

  if (responseDecision.action === "observe_only") {
    return buildWebhookResultV2(input, runtimeContext, relevance, obligationDecision, modeDecision, responseDecision, {
      observed: true,
      payload: input.message || input.payload || null,
      contextFlags,
    });
  }

  if (responseDecision.action === "task_execution") {
    if (!input.message || typeof adapter.executeTask !== "function" || typeof adapter.postCommunityMessage !== "function") {
      return buildWebhookResultV2(input, runtimeContext, relevance, obligationDecision, modeDecision, responseDecision, {
        handled: false,
        required_unfulfilled: true,
        payload: input.message || input.payload || null,
        contextFlags,
      });
    }
    const resultText = await adapter.executeTask(input.message, state, runtimeContext);
    const reply = await adapter.postCommunityMessage(state, input.message, {
      message_type: "analysis",
      content: {
        text: resultText,
        metadata: {
          runtime_dispatch: {
            mode: modeDecision.mode,
            obligation: obligationDecision.obligation,
            relevance: relevance.reason,
            decision: responseDecision.action,
          },
        },
      },
    });
    return buildWebhookResultV2(input, runtimeContext, relevance, obligationDecision, modeDecision, responseDecision, {
      handled: true,
      executed: true,
      replyId: reply?.id || null,
      contextFlags,
    });
  }

  if (!input.message || typeof adapter.postCommunityMessage !== "function") {
    return buildWebhookResultV2(input, runtimeContext, relevance, obligationDecision, modeDecision, responseDecision, {
      handled: false,
      required_unfulfilled: obligationDecision.obligation !== "observe_only",
      payload: input.message || input.payload || null,
      contextFlags,
    });
  }

  let replyText = "";
  if (typeof adapter.generateReply === "function") {
    replyText = (await adapter.generateReply(input.message, state, runtimeContext, {
      mode: modeDecision.mode,
      category: input.category,
      relevance,
      obligation: obligationDecision,
      contextFlags,
      responseDecision,
    })) || "";
  }
  if (!replyText && typeof adapter.buildFallbackReplyText === "function") {
    replyText =
      (await adapter.buildFallbackReplyText(input.message, state, runtimeContext, {
        mode: modeDecision.mode,
        category: input.category,
        relevance,
        obligation: obligationDecision,
        contextFlags,
        responseDecision,
      })) || "";
  }
  if (!replyText) {
    replyText = defaultFallbackReplyText(input, modeDecision, obligationDecision, contextFlags, responseDecision);
  }
  if (!replyText) {
    return buildWebhookResultV2(input, runtimeContext, relevance, obligationDecision, modeDecision, responseDecision, {
      observed: true,
      payload: input.message || input.payload || null,
      contextFlags,
    });
  }

  const reply = await adapter.postCommunityMessage(state, input.message, {
    message_type: responseDecision.action === "ack" ? "summary" : "analysis",
    content: {
      text: replyText,
      metadata: {
        runtime_dispatch: {
          mode: modeDecision.mode,
          obligation: obligationDecision.obligation,
          relevance: relevance.reason,
          decision: responseDecision.action,
        },
      },
    },
  });

  return buildWebhookResultV2(input, runtimeContext, relevance, obligationDecision, modeDecision, responseDecision, {
    handled: true,
    executed: true,
    replyId: reply?.id || null,
    contextFlags,
  });
}

export async function dispatchByMode(adapter, state, input, runtimeContext, modeDecision, relevance, obligationDecision, contextFlags) {
  if (modeDecision.mode === "ignore" || modeDecision.mode === "observe") {
    return buildWebhookResultV2(input, runtimeContext, relevance, obligationDecision, modeDecision, { action: "observe_only", reason: "observe_mode" }, {
      ignored: modeDecision.mode === "ignore",
      observed: true,
      contextFlags,
    });
  }

  if (modeDecision.mode === "self_message") {
    return buildWebhookResultV2(input, runtimeContext, relevance, obligationDecision, modeDecision, { action: "observe_only", reason: "self_message_no_reply" }, {
      handled: true,
      observed: true,
      contextFlags,
    });
  }

  if (modeDecision.mode === "protocol_violation") {
    if (typeof adapter.handleProtocolViolation === "function") {
      const result = await adapter.handleProtocolViolation(state, input.event);
      return buildWebhookResultV2(input, runtimeContext, relevance, obligationDecision, modeDecision, null, {
        handled: true,
        result,
        contextFlags,
      });
    }
    return buildWebhookResultV2(input, runtimeContext, relevance, obligationDecision, modeDecision, null, {
      handled: false,
      payload: input.payload,
      contextFlags,
    });
  }

  if (modeDecision.mode === "workflow_contract") {
    if (typeof adapter.loadWorkflowContract === "function") {
      const groupId = input.groupId || input.payload?.group_id || null;
      const contract = input.payload?.workflow_contract || input.payload?.contract || input.payload;
      const result = await adapter.loadWorkflowContract(groupId, contract, "runtime_event");
      return buildWebhookResultV2(input, runtimeContext, relevance, obligationDecision, modeDecision, null, {
        handled: true,
        result,
        contextFlags,
      });
    }
    return buildWebhookResultV2(input, runtimeContext, relevance, obligationDecision, modeDecision, null, {
      handled: false,
      payload: input.payload,
      contextFlags,
    });
  }

  if (modeDecision.mode === "channel_context") {
    if (typeof adapter.loadChannelContext === "function") {
      const groupId = input.groupId || input.payload?.group_id || null;
      const contextPayload = input.payload?.channel_context || input.payload;
      const result = await adapter.loadChannelContext(state, groupId, contextPayload);
      return buildWebhookResultV2(input, runtimeContext, relevance, obligationDecision, modeDecision, null, {
        handled: true,
        result,
        contextFlags,
      });
    }
    return buildWebhookResultV2(input, runtimeContext, relevance, obligationDecision, modeDecision, null, {
      handled: false,
      payload: input.payload,
      contextFlags,
    });
  }

  if (["admin_message", "system_event"].includes(modeDecision.mode)) {
    if (typeof adapter.handleSystemEvent === "function") {
      const result = await adapter.handleSystemEvent(state, input.event);
      return buildWebhookResultV2(input, runtimeContext, relevance, obligationDecision, modeDecision, null, {
        handled: true,
        result,
        contextFlags,
      });
    }
    return fallbackDispatch(adapter, state, input, runtimeContext, modeDecision, relevance, obligationDecision, contextFlags);
  }

  if (modeDecision.mode === "task") {
    if (!relevance.relevant && typeof adapter.handleTaskEnvelope === "function") {
      const result = await adapter.handleTaskEnvelope(input.message, state, runtimeContext, {
        reason: input.reason,
        relevant: relevance.relevant,
        obligation: obligationDecision.obligation,
        contextFlags,
      });
      return buildWebhookResultV2(input, runtimeContext, relevance, obligationDecision, modeDecision, null, {
        handled: true,
        result,
        contextFlags,
      });
    }
    return fallbackDispatch(adapter, state, input, runtimeContext, modeDecision, relevance, obligationDecision, contextFlags);
  }

  if (["discussion", "decision", "chat"].includes(modeDecision.mode)) {
    const localHandlerName =
      modeDecision.mode === "discussion"
        ? "handleDiscussion"
        : modeDecision.mode === "decision"
          ? "handleDecision"
          : "handleChat";
    if (typeof adapter[localHandlerName] === "function") {
      const result = await adapter[localHandlerName](input.message, state, runtimeContext, {
        reason: input.reason,
        relevant: relevance.relevant,
        obligation: obligationDecision.obligation,
        contextFlags,
      });
      return buildWebhookResultV2(input, runtimeContext, relevance, obligationDecision, modeDecision, null, {
        handled: true,
        result,
        contextFlags,
      });
    }
    return fallbackDispatch(adapter, state, input, runtimeContext, modeDecision, relevance, obligationDecision, contextFlags);
  }

  if (["status", "unknown"].includes(modeDecision.mode)) {
    const localHandlerName = modeDecision.mode === "status" ? "handleStatus" : "handleUnknown";
    if (typeof adapter[localHandlerName] === "function") {
      const result = await adapter[localHandlerName](input.message || input.payload, state, runtimeContext, {
        reason: input.reason,
        relevant: relevance.relevant,
        obligation: obligationDecision.obligation,
        contextFlags,
      });
      return buildWebhookResultV2(input, runtimeContext, relevance, obligationDecision, modeDecision, null, {
        handled: true,
        result,
        contextFlags,
      });
    }
    return fallbackDispatch(adapter, state, input, runtimeContext, modeDecision, relevance, obligationDecision, contextFlags);
  }

  return buildWebhookResultV2(input, runtimeContext, relevance, obligationDecision, modeDecision, { action: "observe_only", reason: "unhandled_mode" }, {
    observed: true,
    contextFlags,
  });
}

export async function handleRuntimeEvent(adapter, state, event) {
  const extracted = extractMessage(event);
  const classification = classifyIncoming(extracted.eventType, extracted.message, extracted.payload, state);
  const input = { ...extracted, ...classification, event: extracted.normalizedEvent || event };

  const ignoreDecision = isIgnorableMessage(input);
  if (ignoreDecision.ignorable) {
    return buildWebhookResultV2(
      input,
      {},
      { relevant: false, reason: ignoreDecision.reason },
      { obligation: "observe_only", reason: "ignorable_input" },
      { mode: "ignore" },
      { action: "observe_only", reason: "ignore" },
      { ignored: true, contextFlags: {} },
    );
  }

  const shouldLoadContext = Boolean(
    input.groupId && ["task", "discussion", "decision", "chat", "status", "unknown", "admin_message", "system_event"].includes(input.category),
  );
  const runtimeContext = shouldLoadContext ? await adapter.fetchRuntimeContext(input.groupId, state) : {};
  const relevance = checkRelevance(input, state, runtimeContext);
  const contextFlags = buildContextFlags(input, state, runtimeContext, relevance);
  const obligationDecision = decideObligation(input, relevance, contextFlags);
  const modeDecision = decideMode(input, relevance);

  return dispatchByMode(adapter, state, input, runtimeContext, modeDecision, relevance, obligationDecision, contextFlags);
}
