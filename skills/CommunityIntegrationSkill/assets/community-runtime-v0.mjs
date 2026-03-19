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

export async function dispatchByMode(adapter, state, input, runtimeContext, modeDecision, relevance) {
  if (modeDecision.mode === "ignore") {
    return {
      ignored: true,
      mode: "ignore",
      category: input.category,
      reason: modeDecision.reason,
      relevant: relevance.relevant,
    };
  }

  if (modeDecision.mode === "protocol_violation") {
    if (typeof adapter.handleProtocolViolation === "function") {
      const result = await adapter.handleProtocolViolation(state, input.event);
      return { ignored: false, mode: "protocol_violation", category: "protocol_violation", handled: true, result };
    }
    return { ignored: false, mode: "protocol_violation", category: "protocol_violation", handled: false, payload: input.payload };
  }

  if (modeDecision.mode === "workflow_contract") {
    if (typeof adapter.loadWorkflowContract === "function") {
      const groupId = input.groupId || input.payload?.group_id || null;
      const contract = input.payload?.workflow_contract || input.payload?.contract || input.payload;
      const result = await adapter.loadWorkflowContract(groupId, contract, "runtime_event");
      return { ignored: false, mode: "workflow_contract", category: "workflow_contract", handled: true, result };
    }
    return { ignored: false, mode: "workflow_contract", category: "workflow_contract", handled: false, payload: input.payload };
  }

  if (modeDecision.mode === "channel_context") {
    if (typeof adapter.loadChannelContext === "function") {
      const groupId = input.groupId || input.payload?.group_id || null;
      const contextPayload = input.payload?.channel_context || input.payload;
      const result = await adapter.loadChannelContext(state, groupId, contextPayload);
      return { ignored: false, mode: "channel_context", category: "channel_context", handled: true, result };
    }
    return { ignored: false, mode: "channel_context", category: "channel_context", handled: false, payload: input.payload };
  }

  if (modeDecision.mode === "system") {
    if (typeof adapter.handleSystemEvent === "function") {
      const result = await adapter.handleSystemEvent(state, input.event);
      return { ignored: false, mode: "system", category: "system", handled: true, result };
    }
    return { ignored: false, mode: "system", category: "system", handled: false, event_type: input.eventType };
  }

  if (modeDecision.mode === "task") {
    if (!relevance.relevant) {
      if (typeof adapter.handleTaskEnvelope === "function") {
        const result = await adapter.handleTaskEnvelope(input.message, state, runtimeContext, {
          reason: modeDecision.reason,
          relevant: relevance.relevant,
        });
        return {
          ignored: false,
          mode: "task",
          category: input.category,
          handled: true,
          result,
          reason: modeDecision.reason,
          relevant: relevance.relevant,
        };
      }
      return {
        ignored: false,
        mode: "task",
        category: input.category,
        handled: false,
        reason: modeDecision.reason,
        relevant: relevance.relevant,
        payload: input.message,
      };
    }
    const resultText = await adapter.executeTask(input.message, state, runtimeContext);
    const reply = await adapter.postCommunityMessage(state, input.message, {
      message_type: "analysis",
      content: {
        text: resultText,
        metadata: {
          runtime_dispatch: {
            mode: "task",
            relevance_reason: relevance.reason,
          },
        },
      },
    });
    return {
      ignored: false,
      mode: "task",
      category: input.category,
      executed: true,
      replyId: reply?.id || null,
      reason: modeDecision.reason,
      relevance_reason: relevance.reason,
    };
  }

  if (modeDecision.mode === "discussion" || modeDecision.mode === "decision" || modeDecision.mode === "chat") {
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
      });
      return {
        ignored: false,
        mode: modeDecision.mode,
        category: input.category,
        handled: true,
        result,
        reason: modeDecision.reason,
        relevant: relevance.relevant,
      };
    }
    if (typeof adapter.generateReply === "function") {
      const replyText = await adapter.generateReply(input.message, state, runtimeContext);
      const reply = await adapter.postCommunityMessage(state, input.message, {
        message_type: "analysis",
        content: {
          text: replyText,
          metadata: {
            runtime_dispatch: {
              mode: modeDecision.mode,
              relevance_reason: relevance.reason,
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
        reason: modeDecision.reason,
        relevance_reason: relevance.reason,
      };
    }
    return {
      ignored: false,
      mode: modeDecision.mode,
      category: input.category,
      handled: false,
      reason: modeDecision.reason,
      relevant: relevance.relevant,
      payload: input.message,
    };
  }

  if (modeDecision.mode === "status" || modeDecision.mode === "unknown") {
    const localHandlerName = modeDecision.mode === "status" ? "handleStatus" : "handleUnknown";
    if (typeof adapter[localHandlerName] === "function") {
      const result = await adapter[localHandlerName](input.message || input.payload, state, runtimeContext, {
        reason: modeDecision.reason,
        relevant: relevance.relevant,
      });
      return {
        ignored: false,
        mode: modeDecision.mode,
        category: input.category,
        handled: true,
        result,
        reason: modeDecision.reason,
        relevant: relevance.relevant,
      };
    }
    return {
      ignored: false,
      mode: modeDecision.mode,
      category: input.category,
      handled: false,
      reason: modeDecision.reason,
      relevant: relevance.relevant,
      payload: input.message || input.payload,
    };
  }

  return {
    ignored: true,
    mode: "ignore",
    category: input.category,
    reason: "unknown_mode",
    relevant: relevance.relevant,
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

  const shouldLoadContext = Boolean(input.groupId && ["task", "discussion", "decision", "chat", "status"].includes(input.category));
  const runtimeContext = shouldLoadContext
    ? await adapter.fetchRuntimeContext(input.groupId, state)
    : {};
  const relevance = checkRelevance(input, state, runtimeContext);
  const modeDecision = decideMode(input, relevance);

  return dispatchByMode(adapter, state, input, runtimeContext, modeDecision, relevance);
}
