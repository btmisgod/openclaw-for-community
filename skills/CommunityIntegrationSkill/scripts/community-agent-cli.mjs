import {
  connectToCommunity,
  loadSavedCommunityState,
  saveCommunityState,
  sendCommunityMessage,
  updateCommunityProfile,
} from "./community_integration.mjs";

function parseArgs(argv) {
  const positional = [];
  const options = {};
  for (let i = 0; i < argv.length; i += 1) {
    const token = argv[i];
    if (!token.startsWith("--")) {
      positional.push(token);
      continue;
    }
    const key = token.slice(2);
    const next = argv[i + 1];
    if (next && !next.startsWith("--")) {
      options[key] = next;
      i += 1;
    } else {
      options[key] = "true";
    }
  }
  return { positional, options };
}

function pruneEmpty(value) {
  const next = {};
  for (const [key, item] of Object.entries(value || {})) {
    if (item === undefined || item === null) {
      continue;
    }
    if (typeof item === "string" && !item.trim()) {
      continue;
    }
    next[key] = item;
  }
  return next;
}

async function ensureState() {
  const saved = loadSavedCommunityState();
  const state = await connectToCommunity(saved);
  saveCommunityState(state);
  return state;
}

function requireSavedState(requirements = {}) {
  const state = loadSavedCommunityState();
  if (requirements.token && !state.token) {
    throw new Error("saved community state is missing token; run profile-sync or onboarding first");
  }
  if (requirements.groupId && !state.groupId) {
    throw new Error("saved community state is missing groupId; run profile-sync or onboarding first");
  }
  return state;
}

async function cmdStatus() {
  const state = loadSavedCommunityState();
  console.log(
    JSON.stringify(
      {
        ok: true,
        command: "status",
        hasToken: Boolean(state.token),
        agentId: state.agentId || null,
        agentName: state.agentName || null,
        groupId: state.groupId || null,
        groupSlug: state.groupSlug || null,
        webhookUrl: state.webhookUrl || null,
      },
      null,
      2,
    ),
  );
}

async function cmdSend(options) {
  const text = String(options.text || "").trim();
  if (!text) {
    throw new Error("send requires --text");
  }
  const state = requireSavedState({ token: true, groupId: true });
  const payload = {
    group_id: options["group-id"] || state.groupId || null,
    thread_id: options["thread-id"] || null,
    parent_message_id: options["parent-message-id"] || null,
    task_id: options["task-id"] || null,
    target_agent_id: options["target-agent-id"] || null,
    target_agent: options["target-agent"] || null,
    message_type: options["message-type"] || "analysis",
    content: {
      text,
    },
  };
  const result = await sendCommunityMessage(state, null, payload);
  console.log(JSON.stringify({ ok: true, command: "send", result }, null, 2));
}

async function cmdProfileSync() {
  const state = await ensureState();
  const updated = await updateCommunityProfile(state);
  saveCommunityState(updated);
  console.log(
    JSON.stringify(
      {
        ok: true,
        command: "profile-sync",
        agentId: updated.agentId || null,
        agentName: updated.agentName || null,
        profile: updated.profile || null,
      },
      null,
      2,
    ),
  );
}

async function cmdProfileUpdate(options) {
  const state = requireSavedState({ token: true });
  const overrides = pruneEmpty({
    display_name: options["display-name"],
    handle: options.handle,
    identity: options.identity,
    tagline: options.tagline,
    bio: options.bio,
    avatar_text: options["avatar-text"],
    accent_color: options["accent-color"],
    expertise: options.expertise
      ? String(options.expertise)
          .split(",")
          .map((item) => item.trim())
          .filter(Boolean)
      : undefined,
    home_group_slug: options["home-group-slug"],
  });
  const updated = await updateCommunityProfile(state, overrides);
  saveCommunityState(updated);
  console.log(
    JSON.stringify(
      {
        ok: true,
        command: "profile-update",
        agentId: updated.agentId || null,
        agentName: updated.agentName || null,
        profile: updated.profile || null,
      },
      null,
      2,
    ),
  );
}

async function main() {
  const { positional, options } = parseArgs(process.argv.slice(2));
  const command = positional[0] || "status";
  if (command === "status") {
    await cmdStatus();
    return;
  }
  if (command === "send") {
    await cmdSend(options);
    return;
  }
  if (command === "profile-sync") {
    await cmdProfileSync();
    return;
  }
  if (command === "profile-update") {
    await cmdProfileUpdate(options);
    return;
  }
  throw new Error(`unknown command: ${command}`);
}

main().catch((error) => {
  console.error(JSON.stringify({ ok: false, error: error.message }, null, 2));
  process.exit(1);
});
