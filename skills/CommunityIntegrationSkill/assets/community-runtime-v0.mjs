function lower(value) {
  return String(value || "").trim().toLowerCase();
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

export function extractMessage(event) {
  const eventType = String(event?.event?.event_type || "").trim();
  const message = event?.entity?.message || event?.event?.payload?.message || null;
  const payload = extractPayload(event);
  const groupId = extractGroupId(event, message);
  return { eventType, message, payload, groupId };
}

export function classifyIncoming(eventType, message, payload) {
  if (eventType === "protocol_violation") {
    return { category: "protocol_violation", reason: "protocol_violation_event" };
  }
  if (eventType === "workflow_contract") {
    return { category: "workflow_contract", reason: "workflow_contract_event" };
  }
  if (eventType === "channel_context") {
    return { category: "channel_context", reason: "channel_context_event" };
  }
  if (eventType && eventType !== "message.posted") {
    return { category: "system", reason: eventType };
  }
  if (!message) {
    return { category: "unknown", reason: "missing_message" };
  }

  const signals = structuredSignalsOf(message);
  const type = signals.messageType;
  const metadata = metadataOf(message);

  if (type === "meta" || metadata.system_event) {
    return { category: "system", reason: "system_message" };
  }
  if (signals.flowType === "status" || ["progress", "claim", "summary", "review"].includes(type)) {
    return { category: "status", reason: "status_signal" };
  }
  if (signals.flowType === "task") {
    return { category: "task", reason: "flow_type_task" };
  }
  if (["assign", "handoff", "request_action", "followup", "authorize"].includes(signals.intent)) {
    return { category: "task", reason: "intent_task" };
  }
  if (signals.flowType === "decision" || type === "decision") {
    return { category: "decision", reason: "decision_signal" };
  }
  if (type === "chat") {
    return { category: "chat", reason: "message_type_chat" };
  }
  if (signals.flowType === "discussion" || ["question", "analysis"].includes(type)) {
    return { category: "discussion", reason: "discussion_signal" };
  }
  if (signals.hasExplicitAssignment || looksLikeTask(message)) {
    return { category: "task", reason: "task_signal" };
  }
  if (looksLikeResponse(message)) {
    return { category: "discussion", reason: "response_signal" };
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
  if (["protocol_violation", "workflow_contract", "channel_context", "system"].includes(input.category)) {
    return { relevant: true, reason: `${input.category}_event` };
  }

  const message = input.message;
  if (!message || message.agent_id === state.agentId) {
    return { relevant: false, reason: "self_or_missing" };
  }

  const signals = structuredSignalsOf(message);
  const text = textOf(message);
  const self = selfNeedles(state);
  const selfRoles = selfRoleNeedles(state, runtimeContext);

  if (lower(signals.targetAgentId) === lower(state.agentId)) {
    return { relevant: true, reason: "target_agent_id" };
  }
  if (includesAny(signals.targetAgent, self)) {
    return { relevant: true, reason: "target_agent" };
  }
  if (includesAny(signals.targetAgent, selfRoles)) {
    return { relevant: true, reason: "target_agent_role" };
  }
  if (signals.assignees.some((item) => includesAny(typeof item === "string" ? item : JSON.stringify(item), self))) {
    return { relevant: true, reason: "assignees" };
  }
  if (
    signals.assignees.some((item) => includesAny(typeof item === "string" ? item : JSON.stringify(item), selfRoles))
  ) {
    return { relevant: true, reason: "assignees_role" };
  }
  if (signals.mentions.some((item) => includesAny(typeof item === "string" ? item : JSON.stringify(item), self))) {
    return { relevant: true, reason: "mentions" };
  }
  if (
    signals.mentions.some((item) => includesAny(typeof item === "string" ? item : JSON.stringify(item), selfRoles))
  ) {
    return { relevant: true, reason: "mentions_role" };
  }
  if (includesAny(text, self) || hasDirectedMention(text, state, runtimeContext)) {
    return { relevant: true, reason: "text_match" };
  }
  if (hasRoleExecutionMatch(text, state, runtimeContext)) {
    return { relevant: true, reason: "role_match" };
  }

  return { relevant: false, reason: "not_targeted" };
}

export function decideMode(input, relevance) {
  if (input.category === "protocol_violation") {
    return { mode: "protocol_violation", reason: input.reason };
  }
  if (input.category === "workflow_contract") {
    return { mode: "workflow_contract", reason: input.reason };
  }
  if (input.category === "channel_context") {
    return { mode: "channel_context", reason: input.reason };
  }
  if (input.category === "system") {
    return { mode: "system", reason: input.reason };
  }

  if (!relevance.relevant) {
    if (input.category === "task") {
      return { mode: "task", reason: `weak_${relevance.reason}` };
    }
    if (input.category === "status") {
      return { mode: "status", reason: relevance.reason };
    }
    if (["discussion", "decision", "chat", "unknown"].includes(input.category)) {
      return { mode: input.category, reason: relevance.reason };
    }
    return { mode: "unknown", reason: relevance.reason };
  }

  if (input.category === "task") {
    return { mode: "task", reason: input.reason };
  }
  if (input.category === "status") {
    return { mode: "status", reason: input.reason };
  }
  if (input.category === "discussion") {
    return { mode: "discussion", reason: input.reason };
  }
  if (input.category === "decision") {
    const signals = structuredSignalsOf(input.message);
    if (signals.hasExplicitAssignment || ["approve", "authorize"].includes(signals.intent)) {
      return { mode: "task", reason: "decision_actionable" };
    }
    return { mode: "decision", reason: input.reason };
  }
  if (input.category === "chat") {
    return { mode: "chat", reason: input.reason };
  }
  return { mode: "unknown", reason: input.reason };
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
  if (["protocol_violation", "workflow_contract", "channel_context"].includes(input.category)) {
    return { obligation: "required", reason: `${input.category}_required` };
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

  if (input.category === "system") {
    if (contextFlags.need_ack || contextFlags.question || contextFlags.addressed) {
      return { obligation: "required_ack", reason: "system_ack_requested" };
    }
    return { obligation: "observe_only", reason: "system_default_observe" };
  }

  return {
    obligation: relevance.relevant ? "optional" : "observe_only",
    reason: relevance.relevant ? "relevant_default" : "observe_only_default",
  };
}

export function defaultResponseDecision(mode, obligationDecision, contextFlags) {
  const obligation = obligationDecision?.obligation || "observe_only";

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

function dispatchMeta(modeDecision, relevance, obligationDecision, contextFlags, responseDecision = null) {
  return {
    reason: modeDecision.reason,
    relevant: relevance.relevant,
    relevance_reason: relevance.reason,
    obligation: obligationDecision.obligation,
    obligation_reason: obligationDecision.reason,
    context_flags: contextFlags,
    response_decision: responseDecision?.action || null,
    response_decision_reason: responseDecision?.reason || null,
  };
}

function defaultFallbackReplyText(input, modeDecision, obligationDecision, contextFlags, responseDecision) {
  const modeLabel = {
    task: "??",
    status: "??",
    discussion: "??",
    decision: "??",
    chat: "??",
    unknown: "??",
    system: "????",
  }[modeDecision.mode] || "??";

  if (responseDecision.action === "ack") {
    return `?????${modeLabel}??????????????????????????????`;
  }
  if (responseDecision.action === "brief_reply") {
    if (contextFlags.question) {
      return `?????${modeLabel}??????????????????????????????????????????`;
    }
    return `?????${modeLabel}??????????????????????????????`;
  }
  if (responseDecision.action === "full_reply") {
    return `?????${modeLabel}????????????????????????????????????`;
  }
  if (responseDecision.action === "task_execution") {
    return `?????${modeLabel}????????????????`;
  }
  return "";
}

async function resolveResponseDecision(adapter, modeDecision, input, runtimeContext, relevance, obligationDecision, contextFlags) {
  if (typeof adapter.decideResponse === "function") {
    const decision = await adapter.decideResponse(obligationDecision.obligation, modeDecision.mode, {
      category: input.category,
      reason: modeDecision.reason,
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
  const meta = dispatchMeta(modeDecision, relevance, obligationDecision, contextFlags, responseDecision);

  if (responseDecision.action === "observe_only") {
    return {
      ignored: false,
      mode: modeDecision.mode,
      category: input.category,
      observed: true,
      handled: false,
      ...meta,
      payload: input.message || input.payload || null,
    };
  }

  if (responseDecision.action === "task_execution") {
    if (!input.message || typeof adapter.executeTask !== "function" || typeof adapter.postCommunityMessage !== "function") {
      return {
        ignored: false,
        mode: modeDecision.mode,
        category: input.category,
        handled: false,
        required_unfulfilled: true,
        ...meta,
        payload: input.message || input.payload || null,
      };
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
            relevance_reason: relevance.reason,
            response_decision: responseDecision.action,
          },
        },
      },
    });
    return {
      ignored: false,
      mode: modeDecision.mode,
      category: input.category,
      executed: true,
      replyId: reply?.id || null,
      ...meta,
    };
  }

  if (!input.message || typeof adapter.postCommunityMessage !== "function") {
    return {
      ignored: false,
      mode: modeDecision.mode,
      category: input.category,
      handled: false,
      required_unfulfilled: obligationDecision.obligation !== "observe_only",
      ...meta,
      payload: input.message || input.payload || null,
    };
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
    return {
      ignored: false,
      mode: modeDecision.mode,
      category: input.category,
      observed: true,
      handled: false,
      ...meta,
      payload: input.message || input.payload || null,
    };
  }

  const reply = await adapter.postCommunityMessage(state, input.message, {
    message_type: responseDecision.action === "ack" ? "summary" : "analysis",
    content: {
      text: replyText,
      metadata: {
        runtime_dispatch: {
          mode: modeDecision.mode,
          obligation: obligationDecision.obligation,
          relevance_reason: relevance.reason,
          response_decision: responseDecision.action,
        },
      },
    },
  });

  return {
    ignored: false,
    mode: modeDecision.mode,
    category: input.category,
    executed: true,
    replyId: reply?.id || null,
    ...meta,
  };
}

export async function dispatchByMode(adapter, state, input, runtimeContext, modeDecision, relevance, obligationDecision, contextFlags) {
  if (modeDecision.mode === "ignore") {
    return {
      ignored: true,
      mode: "ignore",
      category: input.category,
      ...dispatchMeta(modeDecision, relevance, obligationDecision, contextFlags),
    };
  }

  if (modeDecision.mode === "protocol_violation") {
    if (typeof adapter.handleProtocolViolation === "function") {
      const result = await adapter.handleProtocolViolation(state, input.event);
      return {
        ignored: false,
        mode: "protocol_violation",
        category: "protocol_violation",
        handled: true,
        result,
        ...dispatchMeta(modeDecision, relevance, obligationDecision, contextFlags),
      };
    }
    return {
      ignored: false,
      mode: "protocol_violation",
      category: "protocol_violation",
      handled: false,
      payload: input.payload,
      ...dispatchMeta(modeDecision, relevance, obligationDecision, contextFlags),
    };
  }

  if (modeDecision.mode === "workflow_contract") {
    if (typeof adapter.loadWorkflowContract === "function") {
      const groupId = input.groupId || input.payload?.group_id || null;
      const contract = input.payload?.workflow_contract || input.payload?.contract || input.payload;
      const result = await adapter.loadWorkflowContract(groupId, contract, "runtime_event");
      return {
        ignored: false,
        mode: "workflow_contract",
        category: "workflow_contract",
        handled: true,
        result,
        ...dispatchMeta(modeDecision, relevance, obligationDecision, contextFlags),
      };
    }
    return {
      ignored: false,
      mode: "workflow_contract",
      category: "workflow_contract",
      handled: false,
      payload: input.payload,
      ...dispatchMeta(modeDecision, relevance, obligationDecision, contextFlags),
    };
  }

  if (modeDecision.mode === "channel_context") {
    if (typeof adapter.loadChannelContext === "function") {
      const groupId = input.groupId || input.payload?.group_id || null;
      const contextPayload = input.payload?.channel_context || input.payload;
      const result = await adapter.loadChannelContext(state, groupId, contextPayload);
      return {
        ignored: false,
        mode: "channel_context",
        category: "channel_context",
        handled: true,
        result,
        ...dispatchMeta(modeDecision, relevance, obligationDecision, contextFlags),
      };
    }
    return {
      ignored: false,
      mode: "channel_context",
      category: "channel_context",
      handled: false,
      payload: input.payload,
      ...dispatchMeta(modeDecision, relevance, obligationDecision, contextFlags),
    };
  }

  if (modeDecision.mode === "system") {
    if (typeof adapter.handleSystemEvent === "function") {
      const result = await adapter.handleSystemEvent(state, input.event);
      return {
        ignored: false,
        mode: "system",
        category: "system",
        handled: true,
        result,
        ...dispatchMeta(modeDecision, relevance, obligationDecision, contextFlags),
      };
    }
    return fallbackDispatch(adapter, state, input, runtimeContext, modeDecision, relevance, obligationDecision, contextFlags);
  }

  if (modeDecision.mode === "task") {
    if (!relevance.relevant && typeof adapter.handleTaskEnvelope === "function") {
      const result = await adapter.handleTaskEnvelope(input.message, state, runtimeContext, {
        reason: modeDecision.reason,
        relevant: relevance.relevant,
        obligation: obligationDecision.obligation,
        contextFlags,
      });
      return {
        ignored: false,
        mode: "task",
        category: input.category,
        handled: true,
        result,
        ...dispatchMeta(modeDecision, relevance, obligationDecision, contextFlags),
      };
    }
    if (relevance.relevant && obligationDecision.obligation === "required") {
      return fallbackDispatch(adapter, state, input, runtimeContext, modeDecision, relevance, obligationDecision, contextFlags);
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
        reason: modeDecision.reason,
        relevant: relevance.relevant,
        obligation: obligationDecision.obligation,
        contextFlags,
      });
      return {
        ignored: false,
        mode: modeDecision.mode,
        category: input.category,
        handled: true,
        result,
        ...dispatchMeta(modeDecision, relevance, obligationDecision, contextFlags),
      };
    }
    return fallbackDispatch(adapter, state, input, runtimeContext, modeDecision, relevance, obligationDecision, contextFlags);
  }

  if (["status", "unknown"].includes(modeDecision.mode)) {
    const localHandlerName = modeDecision.mode === "status" ? "handleStatus" : "handleUnknown";
    if (typeof adapter[localHandlerName] === "function") {
      const result = await adapter[localHandlerName](input.message || input.payload, state, runtimeContext, {
        reason: modeDecision.reason,
        relevant: relevance.relevant,
        obligation: obligationDecision.obligation,
        contextFlags,
      });
      return {
        ignored: false,
        mode: modeDecision.mode,
        category: input.category,
        handled: true,
        result,
        ...dispatchMeta(modeDecision, relevance, obligationDecision, contextFlags),
      };
    }
    return fallbackDispatch(adapter, state, input, runtimeContext, modeDecision, relevance, obligationDecision, contextFlags);
  }

  return {
    ignored: true,
    mode: "ignore",
    category: input.category,
    ...dispatchMeta(modeDecision, relevance, obligationDecision, contextFlags),
  };
}

export async function handleRuntimeEvent(adapter, state, event) {
  const extracted = extractMessage(event);
  const classification = classifyIncoming(extracted.eventType, extracted.message, extracted.payload);
  const input = { ...extracted, ...classification, event };

  const ignoreDecision = isIgnorableMessage(input);
  if (ignoreDecision.ignorable) {
    return {
      ignored: true,
      mode: "ignore",
      category: input.category,
      reason: ignoreDecision.reason,
    };
  }

  const shouldLoadContext = Boolean(
    input.groupId && ["task", "discussion", "decision", "chat", "status", "unknown"].includes(input.category),
  );
  const runtimeContext = shouldLoadContext ? await adapter.fetchRuntimeContext(input.groupId, state) : {};
  const relevance = checkRelevance(input, state, runtimeContext);
  const contextFlags = buildContextFlags(input, state, runtimeContext, relevance);
  const obligationDecision = decideObligation(input, relevance, contextFlags);
  const modeDecision = decideMode(input, relevance);

  return dispatchByMode(adapter, state, input, runtimeContext, modeDecision, relevance, obligationDecision, contextFlags);
}
